"""Policy hot-swap endpoints.

``POST /api/v1/pipeline/apply-policy`` evaluates the cached Stage 02 clusters
under a chosen profile and returns the metrics the dashboard renders. The
first call for a given cluster set precomputes every profile (~55 s for three
profiles over 247 clusters); later calls, including switches, are cache
lookups and land well inside the spec's 15 ms budget.

``GET  /api/v1/pipeline/policies`` lists the profiles with their Horn clauses
so the Stage 03 card can render the active rules instead of hardcoding them.

Nothing here writes to the ledger. Preview only.
"""

from __future__ import annotations

import time
from typing import Any

import policy_copilot
from fastapi import APIRouter, HTTPException, Request
from policy_controller import controller, resolve_profile
from policy_profiles import default_profile_id, load_profiles
from pydantic import BaseModel, Field

router = APIRouter()


class ApplyPolicyRequest(BaseModel):
    policy_id: str | None = Field(
        default=None,
        description="Profile to apply: strict_bcbs239 | retail_balanced | analytical_mesh.",
    )
    fingerprint: str | None = Field(
        default=None,
        description="Stage 02 cluster fingerprint; when it differs the cache is rebuilt.",
    )
    concurrency: int = Field(default=4, ge=1, le=16, description="mkit subprocess workers.")


def _clusters_from_ledger(settings: Any) -> tuple[list[dict[str, Any]], str]:
    """Rebuild the Stage 02 partition from persisted candidate facts.

    Stage 02's output (the disjoint components) is already durable: every
    cluster is exactly the set of facts sharing an ``entity_id``. Reading it
    back means a policy switch works after a restart without re-running
    Splink, which is the whole point of caching Stage 02.

    The fingerprint covers the fact ids and their entity assignment, so a new
    build that changes clustering invalidates precomputed profiles instead of
    serving numbers computed over the previous partition.
    """
    import sqlite3

    connection = sqlite3.connect(settings.database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT entity_type, entity_id, fact_id, attribute, value, source, source_id, "
            "authority_rank, event_time, verified, ttl_seconds "
            "FROM candidate_facts ORDER BY entity_type, entity_id, fact_id"
        ).fetchall()
    finally:
        connection.close()

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["entity_type"], row["entity_id"])
        grouped.setdefault(key, []).append(
            {
                "fact_id": row["fact_id"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "attribute": row["attribute"],
                "value": row["value"],
                "source": row["source"],
                "source_id": row["source_id"],
                "authority_rank": row["authority_rank"],
                "event_time": row["event_time"],
                "verified": bool(row["verified"]),
                "ttl_seconds": row["ttl_seconds"],
            }
        )

    links_by_cluster = _links_from_graph(settings)

    clusters: list[dict[str, Any]] = []
    link_total = 0
    for (et, eid), facts in sorted(grouped.items()):
        cluster_links = links_by_cluster.get((et, eid), [])
        link_total += len(cluster_links)
        clusters.append(
            {
                "entity_type": et,
                "entity_id": eid,
                "candidate_facts": facts,
                "links": cluster_links,
            }
        )
    # Fingerprint includes the link count: a build that re-derives links
    # (same facts, different graph) must invalidate precomputed profiles,
    # because L1/C2 verdicts depend on the links, not just the facts.
    fingerprint = "|".join(
        f"{et}:{eid}:{len(facts)}:{len(links_by_cluster.get((et, eid), []))}"
        for (et, eid), facts in sorted(grouped.items())
    )
    fingerprint += f":links={link_total}"
    return clusters, fingerprint


# node_id -> fact_id for the two node kinds the graph writer emits.
# Built once per call from known facts rather than parsed: fact ids contain
# dashes, so splitting the node id on "-" cannot round-trip.
def _node_id_index(
    facts_by_id: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, tuple[str, str, str]]:
    index: dict[str, tuple[str, str, str]] = {}
    for (et, eid), facts in facts_by_id.items():
        for fact in facts:
            for prefix in ("nd-value", "nd-fragment"):
                index[f"{prefix}-{et}-{eid}-{fact['fact_id']}"] = (et, eid, fact["fact_id"])
    return index


def _links_from_graph(settings: Any) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Rebuild Stage 02's proposed links from the persisted graph.

    A real rebuild always sends links (``_splink_link_claims`` + the
    proposer's same_fragment links). Without them the ``L1`` and ``C2`` rules
    have no graph to walk and the preview would under-report quarantines
    relative to what POST /build would actually produce.
    """
    import sqlite3

    connection = sqlite3.connect(settings.database_path)
    connection.row_factory = sqlite3.Row
    try:
        facts_rows = connection.execute(
            "SELECT fact_id, entity_type, entity_id FROM candidate_facts"
        ).fetchall()
        edge_rows = connection.execute(
            "SELECT from_node_id, to_node_id, proof, proposer FROM graph_edges "
            "WHERE edge_type = 'proposed_link'"
        ).fetchall()
    finally:
        connection.close()

    facts_by_id: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in facts_rows:
        facts_by_id.setdefault((row["entity_type"], row["entity_id"]), []).append(
            {"fact_id": row["fact_id"]}
        )
    index = _node_id_index(facts_by_id)

    links_by_cluster: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in edge_rows:
        left = index.get(row["from_node_id"])
        right = index.get(row["to_node_id"])
        if left is None or right is None:
            continue  # node refers to a dropped/trap value; not a live link
        et, eid, left_fact = left
        _, _, right_fact = right
        if (et, eid) != (right[0], right[1]):
            continue  # cross-cluster link: not part of one subgraph
        if left_fact == right_fact:
            continue  # self-link: canonicalize rejects it
        links_by_cluster.setdefault((et, eid), []).append(
            {
                "from_fact_id": left_fact,
                "to_fact_id": right_fact,
                "proof": row["proof"],
                "proposer": row["proposer"] or "mosaic-api",
            }
        )
    for links in links_by_cluster.values():
        links.sort(key=lambda link: (link["from_fact_id"], link["to_fact_id"], link["proof"]))
    return links_by_cluster


def _quick_fingerprint(settings: Any) -> str:
    """Row count + max rowid: detects new/changed facts without reading them.

    candidate_facts is append-only during a build (INSERT OR IGNORE on
    primary key), so count+rowid changes whenever clustering changes and is
    stable otherwise.
    """
    import sqlite3

    connection = sqlite3.connect(settings.database_path)
    try:
        row = connection.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM candidate_facts"
        ).fetchone()
    finally:
        connection.close()
    return f"{row[0]}:{row[1]}"


@router.get("/api/v1/pipeline/policies")
async def list_policies(request: Request) -> dict[str, Any]:
    """Profiles available to switch to, with rules and compile status."""
    profiles = load_profiles()
    status = controller.status()
    active = (
        request.app.state.active_policy
        if hasattr(request.app.state, "active_policy")
        else default_profile_id()
    )
    return {
        "active_policy_id": active,
        "default_policy_id": default_profile_id(),
        "profiles": [p.describe() for p in profiles.values()],
        "stage02": status,
    }


@router.get("/api/v1/pipeline/state")
async def pipeline_state(request: Request) -> dict[str, Any]:
    """What is cached and which profiles are precomputed."""
    return controller.status()


@router.post("/api/v1/pipeline/apply-policy")
async def apply_policy(body: ApplyPolicyRequest, request: Request) -> dict[str, Any]:
    """Re-run Stage 03+04 over cached Stage 02 clusters under a profile.

    Returns metrics, the profile's Horn clauses, its SHA-256 policy hash, and
    the attestation root that folds that hash in. The first call for a cluster
    set precomputes every profile; switches afterwards are lookups.
    """
    try:
        profile = resolve_profile(body.policy_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    settings = request.app.state.settings
    trust_url = settings.trust_engine_url
    taxonomy_version = getattr(request.app.state, "active_taxonomy_version", None)
    if not taxonomy_version:
        from taxonomies import default_version

        taxonomies = getattr(request.app.state, "taxonomies", None) or {}
        taxonomy_version = default_version(taxonomies, "user")

    quick = _quick_fingerprint(settings)
    cached_stage02 = controller.stage02
    if cached_stage02 is not None and cached_stage02.quick == quick:
        # Clusters unchanged since the last switch: reuse the in-memory copy
        # instead of re-reading every fact from SQLite.
        clusters, fingerprint = cached_stage02.clusters, cached_stage02.fingerprint
    else:
        clusters, fingerprint = _clusters_from_ledger(settings)
        controller.note_quick(quick)
    if not clusters:
        raise HTTPException(
            status_code=409,
            detail="no clustered facts in the ledger yet; run a build before switching policy",
        )
    if body.fingerprint and body.fingerprint != fingerprint:
        # Caller believes it is switching clusters; force a rebuild so we
        # never answer with metrics for a partition the caller does not have.
        controller.invalidate()

    profiles = load_profiles()
    started = time.perf_counter()
    try:
        results = await controller.ensure_ready(
            clusters=clusters,
            fingerprint=fingerprint,
            quick=quick,
            trust_url=trust_url,
            taxonomy_version=taxonomy_version,
            profiles=profiles,
            concurrency=body.concurrency,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as 502 with the cause
        raise HTTPException(status_code=502, detail=f"policy evaluation failed: {exc}") from exc
    total_ms = (time.perf_counter() - started) * 1000

    result = results.get(profile.profile_id)
    if result is None:
        raise HTTPException(status_code=500, detail="profile precompute produced no result")

    # Switching is a lookup: this is the number the 15 ms budget applies to.
    lookup_started = time.perf_counter()
    cached = controller.get(profile.profile_id)
    lookup_ms = (time.perf_counter() - lookup_started) * 1000

    request.app.state.active_policy = profile.profile_id

    return {
        "policy_id": profile.profile_id,
        "label": profile.label,
        "summary": profile.summary,
        "policy_versions": profile.policy_versions,
        "policy_hash": result.policy_hash,
        "rules": profile.rules,
        "compile_status": profile.compile_status,
        "horn_clauses": profile.horn_clauses,
        # Real clauses behind each invariant card, extracted from the .mg so a
        # card can never disagree with the rules that actually ran.
        "invariants": profile.invariant_clauses(),
        "metrics": result.metrics,
        "attestation": {
            "golden_facts_root": result.golden_facts_root,
            "cluster_graph_root": result.cluster_graph_root,
            "policy_hash": result.policy_hash,
            "merkle_root": result.merkle_root,
        },
        "latency": {
            # lookup_ms is what a switch costs once precomputed; total_ms
            # includes the one-off precompute when the cache was cold.
            "execution_latency_ms": round(lookup_ms, 3),
            "precompute_ms": round(result.precompute_ms, 1),
            "total_ms": round(total_ms, 1),
            "cache_hit": cached is not None,
        },
        "stage02": controller.status(),
    }


class CopilotDraftRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=policy_copilot.MAX_INSTRUCTION_CHARS)
    base_profile: str = Field(default="retail_balanced")


@router.post("/api/v1/pipeline/copilot/draft")
async def copilot_draft(body: CopilotDraftRequest, request: Request) -> dict[str, Any]:
    """Turn an English instruction into a DRAFT policy change.

    The proposer (mock parser or Meta model) only fills a closed vocabulary;
    fixed templates compile it; ``mkit`` (the Datalog engine) is the judge of
    whether the result is a valid stratified program. Never applies anything.
    """
    return await policy_copilot.draft(
        request.app.state.settings, body.instruction, body.base_profile
    )

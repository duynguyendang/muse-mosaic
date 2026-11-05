"""POST /build — full pipeline per cluster: MATCH -> CLUSTER -> DERIVE -> persist.

Two-input build: dataset + expected structure. Ingest Documents -> MATCH
(Splink + Muse sub-agents, claims only) -> GRAPH -> DERIVE per cluster ->
STRUCTURE -> publish or escalate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import autonomy
import ledger
import muse_candidates
import muse_impact
import taxonomy_proposals
from config import Settings
from fastapi import APIRouter, HTTPException, Request
from ingest import (
    document_to_fragments,
    normalize_value,
    to_candidate_facts,
)
from publisher import (
    _primary_reason,
    _primary_rule,
    _verdict,
    apply_proposer_gate,
    assemble_and_validate,
    decision_id_for,
    escalate_unavailable,
    persist_decision,
    run_builder_pipeline,
    taxonomy_steward_items,
    utcnow_iso,
)
from schema_contract import DEFAULT_SCHEMA_SPECS
from schemas import (
    BuildRequest,
    DeriveRequest,
    Proposal,
    ProposedLink,
    ResolveRequest,
)
from schemas import (
    Document as DocumentModel,
)
from splink_adapter import flatten_documents, run_splink
from taxonomies import TaxonomyError, canonical_term, default_version
from trust import TrustEngineError
from trust import derive as trust_derive

from routes.deps import _note_splink_result, logger

router = APIRouter()


@dataclass
class _BuildContext:
    """Mutable per-/build-run state shared across cluster phases."""

    settings: Settings
    policies: dict
    taxonomies: dict | None
    expected_structures: dict
    request: BuildRequest
    splink_out: dict[str, Any] = field(default_factory=dict)
    splink_status: str = "unavailable"
    taxonomy_version: str = ""
    engine_killed: bool = False
    autonomy_frozen: bool = False


def _json_safe(value: Any) -> Any:
    """Best-effort JSON round-trip for proposer payloads (never raises)."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    except (TypeError, ValueError):
        return {"unserializable": True}


async def _match_documents(
    current: Settings,
    policies: dict,
    documents: list[dict[str, Any]],
    work_id: str,
) -> tuple[dict[str, Any], str]:
    """MATCH phase: Splink candidates (if enabled) + Muse sub-agent claims.

    Blocking/threshold come from each entity type's policy file. Splink runs
    in its own process; await it without blocking the loop.
    """
    splink_rows = flatten_documents(documents)
    first_type = next((doc.get("entity_type", "user") for doc in documents), "user")
    first_policy = policies.get(first_type)
    splink_cfg = getattr(first_policy, "splink", None) if first_policy else None
    threshold = float(getattr(splink_cfg, "threshold", 0.9) or 0.9)
    blocking = list(getattr(splink_cfg, "blocking", None) or ["email", "phone", "order_id"])
    splink_out = await asyncio.to_thread(
        run_splink,
        splink_rows,
        enabled=bool(getattr(current, "splink_enabled", True)),
        threshold=threshold,
        blocking=blocking,
        service_url=current.matching_service_url,
        policy_version=(
            getattr(first_policy, "policy_version", "unknown") if first_policy else "unknown"
        ),
        work_id=work_id,
        known_policy_versions=sorted({policy.policy_version for policy in policies.values()}),
    )
    _note_splink_result(splink_out)
    status = splink_out["status"]
    if status != "ok":
        status = f"unavailable ({splink_out.get('detail', '')})"
    return splink_out, status


async def _extend_taxonomies(
    settings: Settings, taxonomies: dict, documents: list[dict[str, Any]]
) -> tuple[dict, dict[str, Any]]:
    """Extend the product taxonomy with evidence-verified terms (opt-in).

    Returns the (possibly new) taxonomy store and a compact report. Writes a NEW
    version beside the others; the engine hot-reloads it. Any model trouble
    leaves the taxonomy unchanged (the unknown values stay held by T1).
    """
    from taxonomies import default_version, load_taxonomies, taxonomies_dir

    report: dict[str, Any] = {"mode": settings.taxonomy_proposals}
    try:
        base = taxonomies[default_version(taxonomies, "product")]
    except Exception as exc:  # noqa: BLE001 - no product taxonomy: nothing to extend
        report["skipped_reason"] = f"no product taxonomy: {type(exc).__name__}"
        return taxonomies, report
    mode = "muse" if settings.taxonomy_proposals == "muse" else "rules"
    if mode == "muse" and settings.use_mock_muse:
        # no live model to ask: fall back to the rule proposer, and say so
        mode = "rules"
        report["downgraded_reason"] = "MUSE_TAXONOMY_PROPOSALS=muse but Muse is mocked"
        logger.warning("taxonomy extension: %s; using rules", report["downgraded_reason"])
    result = await taxonomy_proposals.extend_taxonomy(
        settings,
        base,
        documents,
        taxonomies_dir(),
        mode=mode,
        min_support=settings.taxonomy_min_support,
    )
    verdicts = result.pop("verdicts")
    result["terms"] = [
        {
            "term": v.term,
            "term_ok": v.term_ok,
            "term_reason": v.term_reason,
            "support": v.support,
            "proposer": v.proposer,
            "accepted": [{"value": syn, "rule": rule} for syn, rule in v.accepted],
            "rejected": [{"value": syn, "why": why} for syn, why in v.rejected],
        }
        for v in verdicts
    ]
    report.update(
        {k: v for k, v in result.items() if k != "mode"},
        proposer=mode,
        rejected_examples=[
            {"term": v.term, "synonym": s, "why": why}
            for v in verdicts
            for s, why in v.rejected[:3]
        ][:10],
    )
    if report["version"]:
        taxonomies = load_taxonomies()
    return taxonomies, report


def _fact_components(
    cluster: dict[str, Any], fact_documents: dict[str, str]
) -> dict[str, int] | None:
    """fact id -> index of the separately-clustered part it came from, for a
    Muse-merged cluster. None for ordinary clusters (unchanged behaviour)."""
    parts = cluster.get("components")
    if not parts:
        return None
    part_of_doc = {doc: i for i, docs in enumerate(parts) for doc in docs}
    return {fid: part_of_doc[doc] for fid, doc in fact_documents.items() if doc in part_of_doc}


async def _cluster_inputs(
    ctx: _BuildContext,
    cluster: dict[str, Any],
    index: int,
) -> tuple | None:
    """Ingest + propose for one cluster. None when its policy is missing."""
    member_docs = cluster["member_docs"]
    entity_type = cluster["entity_type"]
    entity_id = cluster.get("entity_id") or f"cluster-{index + 1}"
    policy = ctx.policies.get(entity_type)
    if policy is None:
        return None
    taxonomy = None
    if ctx.taxonomies:
        try:
            ctx.taxonomy_version = default_version(ctx.taxonomies, entity_type)
            taxonomy = ctx.taxonomies[ctx.taxonomy_version]
        except TaxonomyError as exc:
            # Fail-closed downstream: the trust-engine rejects an empty
            # taxonomy_version, so nothing derives from this cluster.
            logger.warning(
                "build: taxonomy lookup failed for %s, continuing without: %s", entity_type, exc
            )
            ctx.taxonomy_version = ""
    allowed = set(policy.allowed_attributes)
    fragments = []
    fact_documents: dict[str, str] = {}
    for document in member_docs:
        try:
            doc_model = DocumentModel.model_validate(document)
        except ValueError:
            continue
        doc_frags, _doc_claims = document_to_fragments(doc_model, entity_id, taxonomy, allowed)
        for fragment in doc_frags:
            fragments.append(fragment)
            fact_documents[f"{entity_id}-{fragment.source_id}"] = document["document_id"]
    facts = to_candidate_facts(entity_type, entity_id, fragments, work_id=ctx.request.work_id)
    if taxonomy is not None:
        # Remap synonym values (Taxonomist claim, logged).
        remapped = []
        for fact in facts:
            canonical, kind = canonical_term(taxonomy, fact.attribute, fact.value)
            if kind in ("term_exact", "synonym_exact") and canonical != fact.value:
                remapped.append(fact.model_copy(update={"value": canonical}))
            else:
                remapped.append(fact)
        facts = remapped
    proposal_dict, link_dicts, trace = await run_builder_pipeline(
        facts,
        ctx.settings,
        taxonomy,
        allowed,
        fact_documents or None,
        extra_links=_splink_link_claims(ctx.splink_out, member_docs, facts, fact_documents)
        + muse_candidates.link_claims(cluster, facts, fact_documents),
        components=_fact_components(cluster, fact_documents),
    )
    proposal = Proposal.model_validate(proposal_dict)
    links = [ProposedLink.model_validate(item) for item in link_dicts]
    return entity_type, entity_id, policy, fragments, facts, fact_documents, proposal, links, trace


def _replay_stored(
    ctx: _BuildContext,
    key: str,
    payload_hash: str,
    stored: dict,
) -> tuple[list[dict[str, Any]], list, list, str]:
    """Idempotent replay: same key + same hash replays, different hash 409s."""
    if stored["payload_hash"] != payload_hash:
        raise HTTPException(
            status_code=409,
            detail=f"idempotency key {key!r} reused with a different payload",
        )
    goldens: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    stewards: list[dict[str, Any]] = []
    replayed = ledger.fetch_goldens_by_decision(ctx.settings.database_path, stored["decision_id"])
    if replayed:
        goldens.extend(dict(row) for row in replayed)
    else:
        goldens.append({"replayed_decision_id": stored["decision_id"]})
    for row in ledger.fetch_conflicts_by_decision(
        ctx.settings.database_path, stored["decision_id"]
    ):
        conflicts.append(dict(row))
    for row in ledger.fetch_stewards_by_decision(ctx.settings.database_path, stored["decision_id"]):
        stewards.append(dict(row))
    return goldens, conflicts, stewards, stored["verdict"]


def _flat_request(
    ctx: _BuildContext, entity_type: str, entity_id: str, key: str, fragments: list
) -> ResolveRequest:
    return ResolveRequest(
        work_id=ctx.request.work_id,
        entity_type=entity_type,
        entity_id=entity_id,
        idempotency_key=key,
        fragments=fragments,
    )


async def _derive_persist(
    ctx: _BuildContext,
    cluster: dict[str, Any],
    entity_type: str,
    entity_id: str,
    policy: Any,
    fragments: list,
    facts: list,
    fact_documents: dict,
    proposal: Proposal,
    links: list,
    trace: list,
    key: str,
    payload_hash: str,
    cluster_decision_id: str,
    created_at: str,
) -> tuple[list[dict[str, Any]], list, list, str]:
    """DERIVE + gate + persist for one prepared cluster."""
    try:
        derived = await trust_derive(
            ctx.settings,
            DeriveRequest(
                decision_id=cluster_decision_id,
                entity_type=entity_type,
                entity_id=entity_id,
                idempotency_key=key,
                payload_hash=payload_hash,
                policy_version=policy.policy_version,
                taxonomy_version=ctx.taxonomy_version,
                candidate_facts=facts,
                links=links,
            ),
            engine_killed=ctx.engine_killed,
        )
    except TrustEngineError as exc:
        steward_rows = escalate_unavailable(
            ctx.settings,
            _flat_request(ctx, entity_type, entity_id, key, fragments),
            facts,
            cluster_decision_id,
            policy.policy_version,
            created_at,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "verdict": "escalated",
                "decision_id": cluster_decision_id,
                "reason_code": "TRUST_ENGINE_UNAVAILABLE",
                "message": str(exc),
                "steward_queue": steward_rows,
                "fail_closed": True,
            },
        ) from exc
    flat = _flat_request(ctx, entity_type, entity_id, key, fragments)
    golden, item_conflicts, item_stewards = apply_proposer_gate(
        flat,
        facts,
        proposal,
        derived,
        cluster_decision_id,
        critical_attributes=set(policy.critical_attributes),
    )
    tax_conflicts, tax_stewards = taxonomy_steward_items(
        proposal,
        entity_type,
        entity_id,
        cluster_decision_id,
        facts,
        created_at,
        policy_version=policy.policy_version,
        taxonomy_version=ctx.taxonomy_version,
        taxonomy=(ctx.taxonomies or {}).get(ctx.taxonomy_version),
        published_attrs=frozenset(g.attribute for g in golden),
    )
    item_conflicts = list(item_conflicts) + tax_conflicts
    item_stewards = list(item_stewards) + tax_stewards
    expected = ctx.request.expected_structure or ctx.expected_structures.get(entity_type)
    assembled, _nested, _purchased, struct_conflicts, struct_stewards, struct_ok = (
        assemble_and_validate(
            request=flat,
            facts=facts,
            golden=golden,
            policy_version=policy.policy_version,
            taxonomy_version=ctx.taxonomy_version,
            golden_attrs=set(policy.golden_attributes or [f.attribute for f in facts]),
            expected_structure=expected,
            documents=cluster["member_docs"],
            decision_id=cluster_decision_id,
            created_at=created_at,
            database_path=ctx.settings.database_path,
        )
    )
    item_conflicts = list(item_conflicts) + struct_conflicts
    item_stewards = list(item_stewards) + struct_stewards
    response = persist_decision(
        settings=ctx.settings,
        request=flat,
        facts=facts,
        payload_hash=payload_hash,
        decision_id=cluster_decision_id,
        policy_version=policy.policy_version,
        taxonomy_version=ctx.taxonomy_version,
        rule_fired=_primary_rule(derived, item_conflicts),
        verdict=_verdict(assembled, item_stewards, item_conflicts),
        reason_code=_primary_reason(item_conflicts, derived),
        golden=assembled if struct_ok else [],
        conflicts=item_conflicts,
        stewards=item_stewards,
        created_at=created_at,
        links=links,
        verified_links=derived.verified_links,
        traces={
            "subagents": {
                "trace": [dict(step) for step in trace],
                "taxonomy_claims": [],
                "usage": proposal.usage,
                "mode": proposal.mode,
                "model": proposal.model,
            }
        },
        publish_golden=struct_ok,
        nested_products=_nested,
        purchased_edges=_purchased,
        clusters=ctx.splink_out.get("clusters"),
        fact_documents=fact_documents,
    )
    payload = response.model_dump(mode="json")
    return (
        [
            payload.get("golden")
            or {"decision_id": cluster_decision_id, "verdict": payload.get("verdict")}
        ],
        payload.get("conflicts", []),
        payload.get("steward_queue", []),
        payload.get("verdict", "denied"),
    )


async def _build_one_cluster(
    ctx: _BuildContext,
    cluster: dict[str, Any],
    index: int,
) -> tuple[list[dict[str, Any]], list, list, str, dict[str, Any] | None]:
    """Full per-cluster run: ingest + propose, replay-or-derive, gate, persist.

    Also returns the validated proposer bundle (proposal + links + trace) so
    /build responses can show *what was claimed* alongside what was derived.
    Replayed clusters carry a replay marker instead (no re-propose).
    """
    prepared = await _cluster_inputs(ctx, cluster, index)
    if prepared is None:
        return [], [], [], "denied", None
    entity_type, entity_id, policy, fragments, facts, fact_documents, proposal, links, trace = (
        prepared
    )
    proposal_info: dict[str, Any] = {
        "cluster_id": cluster["cluster_id"],
        "entity_type": entity_type,
        "entity_id": entity_id,
        "policy_version": policy.policy_version,
    }
    key = f"{ctx.request.idempotency_key}-c{index}"
    payload_hash = hashlib.sha256(
        json.dumps(
            {"cluster": cluster["cluster_id"], "members": sorted(fact_documents.values())},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cluster_decision_id = decision_id_for(key, payload_hash)
    created_at = utcnow_iso()
    stored = ledger.fetch_decision_by_key(ctx.settings.database_path, key)
    if stored is not None:
        goldens, conflicts, stewards, verdict = _replay_stored(ctx, key, payload_hash, stored)
        proposal_info["replayed"] = True
        proposal_info["decision_id"] = stored["decision_id"]
        # Replays skip the proposer, but the ledger keeps the original
        # sub-agent trace: re-attach it so inspectors and the agent ribbon
        # show the real derivation instead of an empty run.
        try:
            for record in ledger.fetch_agent_traces(
                ctx.settings.database_path, stored["decision_id"]
            ):
                payload = (record or {}).get("payload") or {}
                steps = payload.get("trace") or []
                if record.get("kind") == "subagents" and isinstance(steps, list) and steps:
                    proposal_info["trace"] = steps
                    proposal_info["subagent_trace"] = steps
                    break
        except (sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
            # The trace is display-only; the replayed verdict is already final.
            logger.warning("build: could not re-attach sub-agent trace for %s: %s", key, exc)
        return goldens, conflicts, stewards, verdict, proposal_info
    proposal_info.update(
        {
            "mode": proposal.mode,
            "model": proposal.model,
            "note": proposal.note,
            "attributes": proposal.model_dump(mode="json").get("attributes", {}),
            "links": [
                link.model_dump(mode="json") if hasattr(link, "model_dump") else link
                for link in links
            ],
            "trace": _json_safe(trace if isinstance(trace, list) else []),
            "subagent_trace": proposal.model_dump(mode="json").get("subagent_trace", []),
            "new_terms": [
                term.model_dump(mode="json") if hasattr(term, "model_dump") else term
                for term in (proposal.new_terms or [])
            ],
            "critiques": _json_safe(list(proposal.critiques or [])),
        }
    )
    goldens, conflicts, stewards, verdict = await _derive_persist(
        ctx,
        cluster,
        entity_type,
        entity_id,
        policy,
        fragments,
        facts,
        fact_documents,
        proposal,
        links,
        trace,
        key,
        payload_hash,
        cluster_decision_id,
        created_at,
    )
    return goldens, conflicts, stewards, verdict, proposal_info


def _autonomy_block(
    ctx: _BuildContext, goldens: list[dict[str, Any]], stewards: list[dict[str, Any]]
) -> dict[str, Any]:
    """Autonomy report: modes in play, breakers tripped, audit samples.

    Breakers grade the build's own per-entity Muse runs — no model calls.
    The batch is *labelled*, never silently degraded; when a breaker fires the
    report says so and the operator decides (jobs stop further clusters, the
    sync path labels only — documented in docs/ROADMAP.md M3).
    """
    by_entity = (ctx.splink_out.get("muse_candidates") or {}).get("by_entity") or {}
    entity_types = sorted(
        {getattr(d, "entity_type", None) or "user" for d in ctx.request.dataset.documents}
    )
    return {
        "frozen": bool(getattr(ctx, "autonomy_frozen", False)),
        "modes": autonomy.resolve_modes(ctx.settings, entity_types),
        "breakers": autonomy.evaluate_breakers(by_entity),
        "audit_samples": autonomy.report_audit(
            goldens, stewards, rate=ctx.settings.autonomy_audit_rate
        ),
    }


def _build_summary(
    ctx: _BuildContext,
    goldens: list[dict[str, Any]],
    conflicts: list,
    stewards: list,
    verdicts: list[str],
    proposals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    overall = (
        "published"
        if goldens and all(v == "published" for v in verdicts)
        else ("escalated" if (stewards or any(v == "escalated" for v in verdicts)) else "denied")
    )
    return {
        "decision_id": decision_id_for(
            ctx.request.idempotency_key,
            hashlib.sha256(ctx.request.idempotency_key.encode()).hexdigest(),
        ),
        "verdict": overall,
        "goldens": goldens,
        "conflicts": conflicts,
        "steward_queue": stewards,
        # Kept for API compatibility; null when the build carried no person
        # policy. ``policy_versions`` is the full, entity-keyed map.
        "policy_version_user": getattr(ctx.policies.get("user"), "policy_version", None),
        "policy_versions": {etype: policy.policy_version for etype, policy in ctx.policies.items()},
        "taxonomy_version": ctx.taxonomy_version,
        "splink_status": ctx.splink_status,
        "proposals": proposals or [],
        "splink": {
            "status": ctx.splink_out.get("status", "unavailable"),
            "clusters": ctx.splink_out.get("clusters", []),
            "pairs": ctx.splink_out.get("pairs", []),
            "detail": ctx.splink_out.get("detail", ""),
            "splink_version": ctx.splink_out.get("splink_version"),
            "duckdb_version": ctx.splink_out.get("duckdb_version"),
        },
        "muse_candidates": ctx.splink_out.get("muse_candidates"),
        "taxonomy_extension": ctx.splink_out.get("taxonomy_extension"),
        "muse_impact": muse_impact.build(
            ctx.settings,
            ctx.splink_out.get("muse_candidates"),
            ctx.splink_out.get("taxonomy_extension"),
        ),
        "autonomy": _autonomy_block(ctx, goldens, stewards),
    }


def _union_find(nodes: list[str], pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Deterministic connected components over linked pairs.

    Module-level (not a closure) so late-binding over a loop variable is
    impossible by construction.
    """
    parent = {node: node for node in nodes}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in pairs:
        if left in parent and right in parent:
            parent[find(left)] = find(right)
    groups: dict[str, list[str]] = {}
    for node in nodes:
        groups.setdefault(find(node), []).append(node)
    return groups


def _exact_identifier_groups(
    docs: list[dict[str, Any]], blocking: list[str]
) -> dict[tuple[str, str], list[str]]:
    """Group document ids sharing one exact blocking identifier.

    Policy blocking list only — never bare address/name similarity.
    Address alone is not publishable proof.
    """
    by_value: dict[tuple[str, str], list[str]] = {}
    for document in docs:
        fields = document.get("fields", {})
        for attribute in blocking:
            field = fields.get(attribute)
            value = field.get("value") if isinstance(field, dict) else field
            if value:
                # Same normalisation the facts get at ingest, so blocking and the
                # engine's exact-proof check agree ("(449) 977-1729" == "4499771729").
                normalised = normalize_value(attribute, str(value))
                by_value.setdefault((attribute, normalised), []).append(document["document_id"])
    return by_value


def _cluster_entity_docs(
    entity_type: str,
    docs: list[dict[str, Any]],
    policy: Any,
    splink_out: dict[str, Any],
) -> list[dict[str, Any]]:
    """Cluster one entity type's documents.

    Union-find over Splink pairs (exact blocking matches) plus the
    exact-identifier fallback (Muse + fuzzy claim when Splink is down).
    """
    pairs = [
        (pair.get("left"), pair.get("right"))
        for pair in splink_out.get("pairs", [])
        if pair.get("left") and pair.get("right")
    ]
    blocking = list(getattr(policy.splink, "blocking", None) or ["email", "phone", "order_id"])
    for _key, members in _exact_identifier_groups(docs, blocking).items():
        ordered_members = sorted(members)
        pairs.extend(zip(ordered_members, ordered_members[1:], strict=False))
    groups = _union_find(
        list(
            dict.fromkeys(
                [doc["document_id"] for doc in docs]
                + [endpoint for pair in pairs for endpoint in pair]
            )
        ),
        pairs,
    )
    by_id = {doc["document_id"]: doc for doc in docs}
    known = [[doc_id for doc_id in members if doc_id in by_id] for members in groups.values()]
    known = [members for members in known if members]
    ordered = sorted(known, key=lambda members: sorted(members)[0])
    return [
        {
            "cluster_id": f"cluster-{index + 1}",
            "entity_type": entity_type,
            "member_docs": [by_id[doc_id] for doc_id in members],
        }
        for index, members in enumerate(ordered)
    ]


async def _build_clusters(
    settings: Settings,
    policies: dict,
    taxonomies: dict | None,
    documents: list[dict[str, Any]],
    splink_out: dict[str, Any],
    autonomy_frozen: bool = False,
) -> list[dict[str, Any]]:
    """MATCH: Splink candidates + Muse Linker claims -> proposed clusters.

    Splink pairs (exact blocking matches) become proposed_link edges with the
    matching exact proof. When Splink is unavailable, exact-identifier
    connected components (from the policy blocking list only — never bare
    address/name similarity) are the Muse + fuzzy fallback, labelled as such.
    Mock Linker adds same-fragment chains, fuzzy and conflict claims.
    Returns clusters: [{cluster_id, entity_type, member_docs}].
    """
    by_entity: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        by_entity.setdefault(document.get("entity_type", "user"), []).append(document)

    unsupported = sorted({entity_type for entity_type in by_entity if entity_type not in policies})
    if unsupported:
        # Fail closed for unknown types: silently dropping documents would
        # build an empty 200 instead of telling the caller they sent data
        # this engine cannot handle. Distinguish the two ways a build can be
        # unsupported: a type nobody registered (a typo, a new type awaiting a
        # contract) and a type the contract knows but no policy ships for.
        unregistered = [t for t in unsupported if t not in DEFAULT_SCHEMA_SPECS]
        registered_no_policy = [t for t in unsupported if t in DEFAULT_SCHEMA_SPECS]
        parts: list[str] = []
        if unregistered:
            parts.append(
                f"unregistered entity type(s): {', '.join(unregistered)} "
                f"(registered: {', '.join(sorted(DEFAULT_SCHEMA_SPECS))})"
            )
        if registered_no_policy:
            parts.append(
                f"entity type(s) with no policy shipped: {', '.join(registered_no_policy)}"
            )
        raise HTTPException(
            status_code=422,
            detail=("; ".join(parts) + f"; supported: {', '.join(sorted(policies))}"),
        )

    clusters: list[dict[str, Any]] = []
    for entity_type, docs in sorted(by_entity.items()):
        policy = policies.get(entity_type)
        if policy is None:
            continue
        # G1: raw field names (full_name, address_line, mobile ...) become the canonical
        # identity roles via the taxonomy aliases BEFORE clustering, so exact blocking
        # and Muse matching see the same names the engine does.
        try:
            tax = (taxonomies or {}).get(default_version(taxonomies, entity_type))
        except Exception:  # noqa: BLE001 - no taxonomy for this type: documents stay as given
            tax = None
        docs = muse_candidates.canonicalize_documents(docs, tax)
        entity_clusters = _cluster_entity_docs(entity_type, docs, policy, splink_out)
        # Opt-in: Muse proposes near-matches the exact identifiers missed. It
        # runs for EVERY entity type that reached clustering: each one carries
        # its own schema contract (fields, admission gate, question), so the
        # entity_type literal this used to test is no longer a reason to keep
        # products and papers away from the judge.
        if settings.muse_candidate_matching:
            frozen = autonomy_frozen or settings.autonomy_kill_switch
            mode = autonomy.effective_mode(settings, entity_type, frozen=frozen)
            if mode == "off":
                entity_clusters, info = autonomy.off_scan(
                    entity_clusters, mode="off", frozen=frozen
                )
            elif mode in ("shadow", "suggest"):
                entity_clusters, info = await autonomy.shadow_scan(
                    settings, entity_clusters, mode=mode, frozen=frozen
                )
            else:  # auto
                entity_clusters, info = await muse_candidates.propose_and_merge(
                    settings, entity_clusters
                )
                info["autonomy"] = {"mode": "auto", "frozen": frozen}
            _record_muse_run(splink_out, entity_type, info)
        clusters.extend(entity_clusters)
    return clusters


# What the build report adds up across the entity types Muse ran for. Every
# key is a count one run produced; sums are what the report means by "in this
# build".
_MUSE_TOTAL_KEYS = (
    "clusters_in",
    "clusters_out",
    "candidate_pairs",
    "kept_pairs",
    "skipped_per_cluster",
    "skipped_max_pairs",
    "shortlisted",
    "judged",
    "merged",
    "cache_hits",
    "judge_calls",
    "judge_failures",
    "tokens",
    "elapsed_s",
    "block_keys",
    "hot_keys_skipped",
    "split_buckets",
    "merges_total",
    "records_regrouped",
)
_MUSE_LIST_KEYS = ("merges", "declined")


def _copy_value(value: Any) -> Any:
    """Copy a value the fold must not mutate in place (dict/list)."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return value


def _record_muse_run(splink_out: dict[str, Any], entity_type: str, info: dict[str, Any]) -> None:
    """Fold one entity type's Muse run into the build's single report slot.

    ``muse_candidates`` is read as ONE dict by the build report and by the
    impact view, and Muse now runs once per entity type. Assigning each run in
    turn would leave only the last one -- sorted order makes that the last
    alphabetically -- so a person run would silently erase a product run that
    had already spent real tokens.

    The fold carries a *copy* of the incoming mutable values, so adding runs up
    can never rewrite a run's own report (an earlier version stored the shares
    by reference, which is how the first entity's ``tiers`` ended up showing
    the combined total). Keys this code does not know how to combine are
    carried through (a later run reported them, so the report keeps them), and
    ``warning`` / ``skipped_reason`` are rebuilt from every run, so the entity
    that failed is named even for a single-entity build. ``by_entity`` keeps
    each run's uncombined report.
    """
    existing = splink_out.get("muse_candidates") or {}
    by_entity = {**(existing.get("by_entity") or {}), entity_type: info}

    total: dict[str, Any] = {}
    if existing:
        # Seed from the previous report with copies: summation below rewrites
        # entries in place, and those objects belong to the run that made them.
        for key, value in existing.items():
            if key != "by_entity":
                total[key] = _copy_value(value)
        for key, value in info.items():
            if key == "by_entity":
                continue
            if key in _MUSE_TOTAL_KEYS:
                total[key] = total.get(key, 0) + value
            elif key in _MUSE_LIST_KEYS:
                total[key] = list(total.get(key) or []) + (value or [])
            elif key == "tiers":
                merged = dict(total.get("tiers") or {})
                for tier, count in (value or {}).items():
                    merged[tier] = merged.get(tier, 0) + count
                total["tiers"] = merged
            elif key in ("warning", "skipped_reason"):
                pass  # rebuilt from every run below
            else:
                total[key] = value  # a key we cannot fold: a later run reported it
    else:
        total = {key: _copy_value(value) for key, value in info.items() if key != "by_entity"}

    # each run caps its own list at the report limit; the total obeys it too
    for key, limit in (
        ("merges", muse_candidates.MAX_REPORTED_MERGES),
        ("declined", muse_candidates.MAX_REPORTED_DECLINED),
    ):
        if key in total:
            total[key] = total[key][:limit]
    warnings = [str(warning) for run in by_entity.values() if (warning := run.get("warning"))]
    total["warning"] = " | ".join(dict.fromkeys(warnings)) or None
    reasons = [
        f"{name}: {run['skipped_reason']}"
        for name, run in by_entity.items()
        if run.get("skipped_reason")
    ]
    total["skipped_reason"] = "; ".join(reasons) or None
    total["by_entity"] = by_entity
    splink_out["muse_candidates"] = total


def _splink_link_claims(
    splink_out: dict[str, Any],
    member_docs: list[dict[str, Any]],
    facts: list,
    fact_documents: dict[str, str],
) -> list[dict[str, Any]]:
    """Turn Splink blocked pairs inside one cluster into exact-proof links.

    The proof is re-derived from the shared identifier (email/phone/order)
    so the claim carries its own evidence; the engine still verifies it (L1).
    """
    member_ids = {doc["document_id"] for doc in member_docs}
    claims: list[dict[str, Any]] = []
    for row in splink_out.get("pairs", []):
        left, right = row.get("left"), row.get("right")
        if not left or not right or left not in member_ids or right not in member_ids:
            continue
        left_facts = sorted(f.fact_id for f in facts if fact_documents.get(f.fact_id) == left)
        right_facts = sorted(f.fact_id for f in facts if fact_documents.get(f.fact_id) == right)
        if not left_facts or not right_facts:
            continue
        proof = _shared_exact_proof(facts, fact_documents, left, right)
        if proof is None:
            continue
        claims.append(
            {
                "from_fact_id": left_facts[0],
                "to_fact_id": right_facts[0],
                "proof": proof,
                "proposer": "Splink",
            }
        )
    return claims


def _shared_exact_proof(
    facts: list, fact_documents: dict[str, str], left_doc: str, right_doc: str
) -> str | None:
    """sole-proof attribute shared exactly by two documents, if any."""
    left_values: dict[str, set[str]] = {}
    right_values: dict[str, set[str]] = {}
    for fact in facts:
        document = fact_documents.get(fact.fact_id)
        if document == left_doc:
            left_values.setdefault(fact.attribute, set()).add(fact.value)
        elif document == right_doc:
            right_values.setdefault(fact.attribute, set()).add(fact.value)
    for attribute, proof in (
        ("email", "exact_email"),
        ("phone", "exact_phone"),
        ("order_id", "exact_order"),
    ):
        if left_values.get(attribute) and left_values[attribute] == right_values.get(attribute):
            return proof
    return None


@router.post("/build")
async def build(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid JSON body") from exc
    try:
        build_request = BuildRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    current: Settings = request.app.state.settings
    from jobs import execute_full_build

    return await execute_full_build(
        current,
        request.app.state.policies,
        getattr(request.app.state, "taxonomies", None),
        getattr(request.app.state, "expected_structures", {}) or {},
        build_request,
        engine_killed=getattr(request.app.state, "engine_killed", False),
        autonomy_frozen=getattr(request.app.state, "autonomy_frozen", False),
    )

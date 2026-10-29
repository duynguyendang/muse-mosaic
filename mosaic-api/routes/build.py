"""POST /build — full pipeline per cluster: MATCH -> CLUSTER -> DERIVE -> persist.

Two-input build: dataset + expected structure. Ingest Documents -> MATCH
(Splink + Muse sub-agents, claims only) -> GRAPH -> DERIVE per cluster ->
STRUCTURE -> publish or escalate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import ledger
from config import Settings
from fastapi import APIRouter, HTTPException, Request
from ingest import (
    document_to_fragments,
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
from taxonomies import canonical_term, default_version
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
        except Exception as exc:
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
        extra_links=_splink_link_claims(ctx.splink_out, member_docs, facts, fact_documents),
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
        "policy_version_user": ctx.policies["user"].policy_version,
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
                by_value.setdefault((attribute, str(value)), []).append(document["document_id"])
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

    clusters: list[dict[str, Any]] = []
    for entity_type, docs in sorted(by_entity.items()):
        policy = policies.get(entity_type)
        if policy is None:
            continue
        clusters.extend(_cluster_entity_docs(entity_type, docs, policy, splink_out))
    return clusters


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
    )

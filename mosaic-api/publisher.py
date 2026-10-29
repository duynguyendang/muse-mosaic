"""Publisher: gated writes + resolve/steward orchestration (CSD 12.5).

Rules enforced here, in code:
- publish only derived GoldenFact objects, never proposer values;
- INSERT decision once per idempotency key; same key + same payload hash
  replays the stored response without calling the trust-engine again;
  same key + different hash is a 409, never a write;
- golden versions increment with an optimistic-lock UNIQUE on
  (entity_type, entity_id, version); a version race retries once, never
  silently overwrites;
- golden + profile embedding in one transaction (embedding columns are NULL
  in v1: no embedding model is pinned, so no vectors exist to write);
- loser facts become tombstones, never deletions;
- proposer low-confidence/abstain on a critical attribute downgrades an
  engine publish to an escalation (fail-closed override: publish -> escalate
  is safe; the reverse is forbidden and never happens here).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import ledger
from config import Settings
from graph import GraphEdge, derive_purchased_edges, insert_graph_rows, plan_graph
from ingest import to_candidate_facts
from policies import PolicyError, active_version, check_attributes
from proposer import ProposerError
from schemas import (
    CandidateFact,
    ConflictFact,
    DeriveRequest,
    DeriveResponse,
    GoldenFact,
    GoldenRecord,
    Proposal,
    ProposedLink,
    ResolveRequest,
    ResolveResponse,
    StewardItem,
    VerifiedLink,
)
from subagents import validate_links
from taxonomies import default_version
from trust import TrustEngineError
from trust import derive as trust_derive

NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

CRITICAL_DOWNGRADE_CONFIDENCES = {"low", "abstain"}


class IdempotencyConflictError(RuntimeError):
    """Same idempotency key, different payload. No write happened."""


class EngineUnavailableError(RuntimeError):
    """A required backend cannot be used. Escalation recorded, nothing published."""

    def __init__(
        self,
        message: str,
        *,
        decision_id: str,
        steward_items: list[dict[str, Any]],
        reason_code: str = "TRUST_ENGINE_UNAVAILABLE",
    ):
        super().__init__(message)
        self.decision_id = decision_id
        self.steward_items = steward_items
        self.reason_code = reason_code


def canonical_payload_hash(request: ResolveRequest) -> str:
    """Hash the resolve payload canonically (sorted fragments)."""
    fragments = sorted(
        (fragment.model_dump(mode="json") for fragment in request.fragments),
        key=lambda item: (
            item["source"],
            item["source_id"],
            item["attribute"],
            item["value"],
            item["event_time"],
        ),
    )
    canonical = json.dumps(
        {
            "entity_type": request.entity_type,
            "entity_id": request.entity_id,
            "fragments": fragments,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decision_id_for(idempotency_key: str, payload_hash: str) -> str:
    """Deterministic decision id: same key + same payload always replays."""
    return str(uuid.uuid5(NAMESPACE, f"mosaic:{idempotency_key}:{payload_hash}"))


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _steward_fallback(attribute: str) -> str:
    return {
        "phone": "do not use the new phone for OTP or payment notification",
        "email": "do not send notifications to the new email",
        "address": "do not ship to the new address",
        "category": "do not list under the new category",
    }.get(attribute, "do not use the new value downstream")


def _deadline_from_facts(facts: list[CandidateFact]) -> str:
    latest = max(facts, key=lambda fact: fact.event_time).event_time
    return (latest + timedelta(hours=24)).isoformat()


def _batch_links(
    facts: list[CandidateFact],
    fact_documents: dict[str, str] | None,
    linker_links: list[ProposedLink],
) -> list[dict[str, Any]]:
    """Links for one derive call: batch co-location + Linker claims.

    Same-document facts are linked as a chain (Scout co-location claim,
    grounding-only). Cross-document links come from the Linker. Returned as
    derive-request dicts.
    """
    seen: set[tuple[str, str, str]] = set()
    links: list[dict[str, Any]] = []

    def add(from_id: str, to_id: str, proof: str, proposer: str) -> None:
        if from_id == to_id:
            return
        key = (min(from_id, to_id), max(from_id, to_id), proof)
        if key in seen:
            return
        seen.add(key)
        links.append(
            {
                "from_fact_id": from_id,
                "to_fact_id": to_id,
                "proof": proof,
                "proposer": proposer,
            }
        )

    if fact_documents:
        by_doc: dict[str, list[str]] = {}
        for fact in facts:
            document = fact_documents.get(fact.fact_id)
            if document:
                by_doc.setdefault(document, []).append(fact.fact_id)
        ordered_docs = sorted(by_doc)
        if len(ordered_docs) == 1:
            ids = sorted(by_doc[ordered_docs[0]])
        else:
            ids = sorted(fact.fact_id for fact in facts)
        for first, second in zip(ids, ids[1:], strict=False):
            add(first, second, "same_fragment", "mosaic-api")
    else:
        ids = sorted(fact.fact_id for fact in facts)
        for first, second in zip(ids, ids[1:], strict=False):
            add(first, second, "same_fragment", "mosaic-api")
    for link in linker_links:
        add(link.from_fact_id, link.to_fact_id, link.proof, link.proposer)
    return links


async def run_builder_pipeline(
    facts: list[CandidateFact],
    settings: Settings,
    taxonomy,
    allowed_attributes: set[str],
    fact_documents: dict[str, str] | None = None,
    extra_links: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the five sub-agents (mock deterministic, or real Muse).

    Returns (proposal_dict, validated_link_dicts, trace_dicts). Linker links
    (plus caller-supplied extra links, e.g. Splink pairs) are validated
    against known fact ids and the closed proof vocabulary; any invalid link
    fails the whole proposal (fail closed, never silently dropped).
    Real mode failures raise ProposerError.
    """
    from subagents import run_mock_pipeline as mock_pipeline
    from subagents import run_real_pipeline as real_pipeline

    if settings.use_mock_muse:
        proposal, _trace = await mock_pipeline(facts, taxonomy, allowed_attributes, fact_documents)
    else:
        proposal, _trace = await real_pipeline(facts, settings, taxonomy, fact_documents)
    fact_ids = {fact.fact_id for fact in facts}
    combined = list(proposal.get("links", [])) + list(extra_links or [])
    valid_links, link_errors = validate_links(combined, fact_ids)
    if link_errors:
        from proposer import ProposerError as _ProposerError

        raise _ProposerError(
            "invalid-links",
            f"Linker produced invalid links: {link_errors[0]}",
        )
    batch = _batch_links(facts, fact_documents, valid_links)
    return proposal, batch, proposal.get("subagent_trace", [])


async def resolve_flow(
    request: ResolveRequest,
    settings: Settings,
    policies: dict,
    taxonomies: dict | None = None,
    *,
    now_iso: str | None = None,
    fact_documents: dict[str, str] | None = None,
    taxonomy_version: str | None = None,
    expected_structure: dict[str, Any] | None = None,
    documents: list[dict[str, Any]] | None = None,
    engine_killed: bool = False,
) -> tuple[ResolveResponse, Proposal]:
    """Run ingest -> MATCH (Splink + Muse sub-agents) -> GRAPH -> DERIVE ->
    STRUCTURE -> publish-or-escalate. Only derived + validated values publish.

    fact_documents maps fact_id -> document_id for same-document co-location
    links (/build). For /resolve the whole batch is one observation.
    taxonomy_version defaults to the entity default; expected_structure
    defaults to the baked-in demo template for the entity type.
    """
    policy, taxonomy, taxonomy_version_resolved = _resolve_policy(
        request, policies, taxonomies, taxonomy_version
    )
    payload_hash = canonical_payload_hash(request)
    decision_id = decision_id_for(request.idempotency_key, payload_hash)
    created_at = now_iso or utcnow_iso()
    facts = to_candidate_facts(
        request.entity_type,
        request.entity_id,
        request.fragments,
        work_id=request.work_id,
    )
    facts, taxonomy_claims = apply_taxonomy_mappings(facts, taxonomy)
    proposal, link_dicts, trace = await _run_proposal(
        settings,
        request,
        facts,
        policy,
        taxonomy,
        fact_documents,
        decision_id,
        created_at,
    )
    stored = ledger.fetch_decision_by_key(settings.database_path, request.idempotency_key)
    if stored is not None:
        if stored["payload_hash"] != payload_hash:
            raise IdempotencyConflictError(
                f"idempotency key {request.idempotency_key!r} was already used "
                "with a different payload; refusing to write"
            )
        return replay_response(settings, stored["decision_id"]), proposal
    links = [ProposedLink.model_validate(item) for item in link_dicts]
    derived = await _run_derive(
        settings,
        request,
        facts,
        links,
        policy,
        taxonomy_version_resolved,
        payload_hash,
        decision_id,
        created_at,
        engine_killed=engine_killed,
    )
    golden, conflicts, steward_queue, nested, purchased = _gate_structure(
        request,
        facts,
        proposal,
        derived,
        decision_id,
        policy,
        taxonomy_version_resolved,
        taxonomy_claims,
        created_at,
        settings,
        expected_structure,
        documents or [],
    )
    response = persist_decision(
        settings=settings,
        request=request,
        facts=facts,
        payload_hash=payload_hash,
        decision_id=decision_id,
        policy_version=policy.policy_version,
        taxonomy_version=taxonomy_version_resolved,
        rule_fired=_primary_rule(derived, conflicts),
        verdict=_verdict(golden, steward_queue, conflicts),
        reason_code=_primary_reason(conflicts, derived),
        golden=golden,
        conflicts=conflicts,
        stewards=steward_queue,
        created_at=created_at,
        links=links,
        verified_links=[
            VerifiedLink(**link) if isinstance(link, dict) else link
            for link in derived.verified_links
        ],
        purchased_edges=purchased,
        nested_products=nested,
        publish_golden=bool(golden),
        traces={
            "subagents": {
                "trace": [
                    step.model_dump() if hasattr(step, "model_dump") else step
                    for step in proposal.subagent_trace
                ],
                "taxonomy_claims": taxonomy_claims,
                "usage": proposal.usage,
                "mode": proposal.mode,
                "model": proposal.model,
            }
        },
    )
    # Wonder adjudicates escalations only, and only if configured.
    wonder_notes = await _maybe_adjudicate(settings, steward_queue, request, decision_id)
    if wonder_notes:
        ledger.store_agent_trace(
            settings.database_path,
            decision_id,
            "wonder",
            {"notes": wonder_notes},
            created_at,
        )
    return response, proposal


def _resolve_policy(
    request: ResolveRequest,
    policies: dict,
    taxonomies: dict | None,
    taxonomy_version: str | None,
):
    """Policy + taxonomy for the request; unknown taxonomy fails closed."""
    policy = active_version(policies, request.entity_type)
    check_attributes(policy, [fragment.attribute for fragment in request.fragments])
    taxonomy = None
    resolved = ""
    if taxonomies:
        resolved = taxonomy_version or default_version(taxonomies, request.entity_type)
        taxonomy = taxonomies.get(resolved)
        if taxonomy is None:
            raise PolicyError(f"no taxonomy {resolved!r}; fail closed")
    return policy, taxonomy, resolved


async def _run_proposal(
    settings: Settings,
    request: ResolveRequest,
    facts: list,
    policy,
    taxonomy,
    fact_documents: dict[str, str] | None,
    decision_id: str,
    created_at: str,
):
    """Muse propose phase; proposer failure escalates, never publishes."""
    try:
        builder_out, link_dicts, trace = await run_builder_pipeline(
            facts,
            settings,
            taxonomy,
            set(policy.allowed_attributes),
            fact_documents,
        )
        proposal = Proposal.model_validate(builder_out)
    except ProposerError as exc:
        steward_rows = escalate_unavailable(
            settings, request, facts, decision_id, policy.policy_version, created_at
        )
        raise EngineUnavailableError(
            f"proposer unavailable ({exc.kind}): {exc}",
            decision_id=decision_id,
            steward_items=steward_rows,
            reason_code="PROPOSER_UNAVAILABLE",
        ) from exc
    except ValueError as exc:
        steward_rows = escalate_unavailable(
            settings, request, facts, decision_id, policy.policy_version, created_at
        )
        raise EngineUnavailableError(
            f"proposal failed validation: {exc}",
            decision_id=decision_id,
            steward_items=steward_rows,
            reason_code="PROPOSER_UNAVAILABLE",
        ) from exc
    return proposal, link_dicts, trace


async def _run_derive(
    settings: Settings,
    request: ResolveRequest,
    facts: list,
    links: list,
    policy,
    taxonomy_version: str,
    payload_hash: str,
    decision_id: str,
    created_at: str,
    engine_killed: bool = False,
):
    """Trust-engine derive; engine failure escalates, never publishes."""
    try:
        return await trust_derive(
            settings,
            DeriveRequest(
                decision_id=decision_id,
                entity_type=request.entity_type,
                entity_id=request.entity_id,
                idempotency_key=request.idempotency_key,
                payload_hash=payload_hash,
                policy_version=policy.policy_version,
                taxonomy_version=taxonomy_version,
                candidate_facts=facts,
                links=links,
            ),
            engine_killed=engine_killed,
        )
    except TrustEngineError as exc:
        steward_rows = escalate_unavailable(
            settings, request, facts, decision_id, policy.policy_version, created_at
        )
        raise EngineUnavailableError(
            str(exc), decision_id=decision_id, steward_items=steward_rows
        ) from exc


def _gate_structure(
    request: ResolveRequest,
    facts: list,
    proposal: Proposal,
    derived,
    decision_id: str,
    policy,
    taxonomy_version: str,
    taxonomy_claims: list,
    created_at: str,
    settings: Settings,
    expected_structure: dict[str, Any] | None,
    documents: list[dict[str, Any]],
):
    """Proposer gate + taxonomy items + whole-document structure gate."""
    golden, conflicts, stewards = apply_proposer_gate(
        request,
        facts,
        proposal,
        derived,
        decision_id,
        critical_attributes=set(policy.critical_attributes),
        taxonomy_version=taxonomy_version,
    )
    # Taxonomy steward items for proposed new terms (steward-only Accept).
    tax_conflicts, tax_stewards = taxonomy_steward_items(
        proposal,
        request.entity_type,
        request.entity_id,
        decision_id,
        facts,
        created_at,
        policy_version=policy.policy_version,
        taxonomy_version=taxonomy_version,
    )
    conflicts.extend(tax_conflicts)
    steward_queue = list(stewards) + tax_stewards
    assembled, nested_purchased, purchased_edges, struct_conflicts, struct_stewards, struct_ok = (
        assemble_and_validate(
            request=request,
            facts=facts,
            golden=golden,
            policy_version=policy.policy_version,
            taxonomy_version=taxonomy_version,
            golden_attrs=set(policy.golden_attributes or [f.attribute for f in facts]),
            expected_structure=expected_structure,
            documents=documents,
            decision_id=decision_id,
            created_at=created_at,
            database_path=settings.database_path,
        )
    )
    conflicts.extend(struct_conflicts)
    steward_queue.extend(struct_stewards)
    return (
        (assembled if struct_ok else []),
        conflicts,
        steward_queue,
        nested_purchased,
        purchased_edges,
    )


def escalate_unavailable(
    settings: Settings,
    request: ResolveRequest,
    facts: list[CandidateFact],
    decision_id: str,
    policy_version: str,
    created_at: str,
) -> list[dict[str, Any]]:
    """Record steward items when the trust-engine cannot be used.

    No decisions row is written: there is no derivation to record, and the
    reserved deterministic decision id stays free so a retry after recovery
    derives under the same id. Steward rows reference the reserved id.
    """
    connection = ledger.connect(settings.database_path)
    try:
        with connection:
            rows = []
            for attribute in sorted({fact.attribute for fact in facts}):
                steward_id = f"st-{decision_id}-{attribute}-unavailable"
                connection.execute(
                    """
                    INSERT OR IGNORE INTO steward_queue
                      (steward_id, entity_type, entity_id, attribute, owner,
                       deputy, deadline, fallback, status, decision_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        steward_id,
                        request.entity_type,
                        request.entity_id,
                        attribute,
                        "steward-001",
                        "steward-002",
                        _deadline_from_facts(facts),
                        _steward_fallback(attribute),
                        "pending",
                        decision_id,
                    ),
                )
                rows.append(
                    {
                        "steward_id": steward_id,
                        "attribute": attribute,
                        "status": "pending",
                        "decision_id": decision_id,
                    }
                )
        return rows
    finally:
        connection.close()


def taxonomy_steward_items(
    proposal: Proposal,
    entity_type: str,
    entity_id: str,
    decision_id: str,
    facts: list[CandidateFact],
    created_at: str,
    policy_version: str = "",
    taxonomy_version: str = "",
) -> tuple[list[ConflictFact], list[StewardItem]]:
    """Steward items for Taxonomist new-term proposals (steward-only Accept)."""
    conflicts: list[ConflictFact] = []
    stewards: list[StewardItem] = []
    attribute = "category" if entity_type == "product" else "display_name"
    for term in proposal.new_terms:
        stewards.append(
            StewardItem(
                steward_id=f"st-{decision_id}-tax-{_slug(term.raw_value)}",
                entity_type=entity_type,
                entity_id=entity_id,
                attribute=attribute,
                owner="steward-001",
                deputy="steward-002",
                deadline=_deadline_from_facts(facts),
                fallback="do not use the new term until a steward accepts it",
                status="pending",
                decision_id=decision_id,
                suggested_action=term.suggested_action,
            )
        )
        conflicts.append(
            ConflictFact(
                entity_type=entity_type,
                entity_id=entity_id,
                attribute=attribute,
                reason_code="TAXONOMY_NEW_TERM_PROPOSED",
                rule_fired="T1",
                fact_ids=[],
                policy_version=policy_version,
                taxonomy_version=taxonomy_version,
            )
        )
    return conflicts, stewards


def apply_proposer_gate(
    request: ResolveRequest,
    facts: list[CandidateFact],
    proposal: Proposal,
    derived: DeriveResponse,
    decision_id: str,
    *,
    critical_attributes: set[str],
    taxonomy_version: str = "",
) -> tuple[list[GoldenFact], list[ConflictFact], list[StewardItem]]:
    """Downgrade engine publishes the proposer flags as low-confidence.

    Only critical attributes are gated, and only in the safe direction:
    an engine publish becomes an escalation. Engine escalations and denials
    are never overridden.
    """
    golden: list[GoldenFact] = []
    conflicts: list[ConflictFact] = list(derived.conflicts)
    stewards: list[StewardItem] = list(derived.steward_queue)
    escalated_attrs = {item.attribute for item in stewards}

    for fact in derived.golden_facts:
        prop = proposal.attributes.get(fact.attribute)
        low_confidence = (
            prop is not None
            and prop.confidence in CRITICAL_DOWNGRADE_CONFIDENCES
            and fact.attribute in critical_attributes
            and fact.attribute not in escalated_attrs
        )
        untrusted_claim = (
            fact.attribute in critical_attributes
            and fact.attribute not in escalated_attrs
            and _has_conflicting_unverified_ticket_claim(facts, fact.attribute, fact.value)
        )
        if not (low_confidence or untrusted_claim):
            golden.append(fact)
            continue
        reason = "UNVERIFIED_CRITICAL_CLAIM" if untrusted_claim else "PROPOSER_LOW_CONFIDENCE"
        conflicts.append(
            ConflictFact(
                entity_type=fact.entity_type,
                entity_id=fact.entity_id,
                attribute=fact.attribute,
                reason_code=reason,
                rule_fired="R2",
                fact_ids=sorted({fact.winner_fact_id, *fact.loser_fact_ids}),
                policy_version=fact.policy_version,
                taxonomy_version=taxonomy_version,
            )
        )
        stewards.append(
            StewardItem(
                steward_id=f"st-{decision_id}-{fact.attribute}-proposer",
                entity_type=fact.entity_type,
                entity_id=fact.entity_id,
                attribute=fact.attribute,
                owner="steward-001",
                deputy="steward-002",
                deadline=_deadline_from_facts(facts),
                fallback=_steward_fallback(fact.attribute),
                status="pending",
                decision_id=decision_id,
            )
        )
    return golden, conflicts, stewards


def apply_taxonomy_mappings(
    facts: list[CandidateFact], taxonomy
) -> tuple[list[CandidateFact], list[dict[str, str]]]:
    """Apply exact taxonomy synonym mappings (Taxonomist claim, logged).

    Only whole-value exact matches (normalized) are mapped; fuzzy-only raw
    values stay untouched and escalate at the engine (T1). Returns the
    (possibly remapped) facts plus mapping claims for the subagent trace.
    """
    if taxonomy is None:
        return facts, []
    from taxonomies import canonical_term

    claims: list[dict[str, str]] = []
    remapped: list[CandidateFact] = []
    for fact in facts:
        canonical, kind = canonical_term(taxonomy, fact.attribute, fact.value)
        if kind in ("term_exact", "synonym_exact") and canonical != fact.value:
            claims.append(
                {
                    "kind": kind,
                    "raw": fact.value,
                    "canonical": canonical,
                    "attribute": fact.attribute,
                }
            )
            remapped.append(fact.model_copy(update={"value": canonical}))
        else:
            if kind == "unmapped" and any(
                term.term == fact.value or canonical_term(taxonomy, fact.attribute, "")[0]
                for term in []
            ):
                pass
            remapped.append(fact)
    return remapped, claims


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value.lower())[:32].strip("-") or "term"


def _default_structure(entity_type: str) -> dict[str, Any]:
    """Baked-in expected golden structure (demo default, CSD 12.6)."""
    from pathlib import Path

    here = Path(__file__).resolve().parent
    candidates = [
        here / "templates" / f"expected_{entity_type}_golden.json",
        here.parent / "data" / "mock" / "templates" / f"expected_{entity_type}_golden.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return {"type": "object"}


def assemble_nested_golden(
    golden_attrs: dict[str, dict[str, Any]],
    purchased: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the nested golden document for whole-structure validation."""
    nested: dict[str, Any] = {attr: value["value"] for attr, value in golden_attrs.items()}
    if purchased:
        nested["purchased_products"] = purchased
    return nested


def validate_structure(
    assembled: dict[str, Any], expected_structure: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Validate the assembled golden as a whole. Returns error descriptors."""
    if not expected_structure:
        return []
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return [{"path": [], "reason": "jsonschema not installed; cannot validate", "fatal": True}]
    validator = Draft202012Validator(expected_structure)
    errors = []
    for error in validator.iter_errors(assembled):
        errors.append(
            {
                "path": list(error.absolute_path),
                "reason": error.message,
                "validator": error.validator,
            }
        )
    return errors


def _structure_downgrades(
    errors: list[dict[str, Any]],
    entity_type: str,
    entity_id: str,
    policy_version: str,
    decision_id: str,
    deadline: str,
) -> tuple[set[str], list[int], list[ConflictFact], list[StewardItem], bool]:
    """Map structure errors to per-attribute / per-element escalations.

    Returns (dropped_attributes, dropped_purchased_indexes, conflicts,
    stewards, blocked). blocked=True means a required field is missing even
    after drops: the whole golden is withheld (no insert).
    """
    import re as _re

    dropped_attrs: set[str] = set()
    dropped_nested: list[int] = []
    for error in errors:
        if error.get("fatal"):
            return set(), [], [], [], True
        path = error.get("path", [])
        if not path and error.get("validator") == "required":
            match = _re.search(r"'([^']+)' is a required property", error.get("reason", ""))
            if match:
                dropped_attrs.add(match.group(1))
            continue
        if path and isinstance(path[0], str):
            if path[0] == "purchased_products" and len(path) > 1 and isinstance(path[1], int):
                if path[1] not in dropped_nested:
                    dropped_nested.append(path[1])
            else:
                dropped_attrs.add(path[0])
    conflicts: list[ConflictFact] = []
    stewards: list[StewardItem] = []
    for attribute in sorted(dropped_attrs):
        conflicts.append(
            ConflictFact(
                entity_type=entity_type,
                entity_id=entity_id,
                attribute=attribute,
                reason_code="STRUCTURE_MISMATCH",
                rule_fired="STRUCT",
                fact_ids=[],
                policy_version=policy_version,
            )
        )
        stewards.append(
            StewardItem(
                steward_id=f"st-{decision_id}-{attribute}-structure",
                entity_type=entity_type,
                entity_id=entity_id,
                attribute=attribute,
                owner="steward-001",
                deputy="steward-002",
                deadline=deadline,
                fallback="do not use this value downstream: it failed structure validation",
                status="pending",
                decision_id=decision_id,
            )
        )
    # One conflict row per dropped nested index (ConflictFact carries no
    # index, so rows for the same attribute are identical by contract).
    for _index in sorted(dropped_nested, reverse=True):
        conflicts.append(
            ConflictFact(
                entity_type=entity_type,
                entity_id=entity_id,
                attribute="purchased_products",
                reason_code="STRUCTURE_MISMATCH_NESTED",
                rule_fired="STRUCT",
                fact_ids=[],
                policy_version=policy_version,
            )
        )
    return dropped_attrs, dropped_nested, conflicts, stewards, False


async def _maybe_adjudicate(
    settings: Settings,
    stewards: list[StewardItem],
    request: ResolveRequest,
    decision_id: str,
) -> list[dict[str, Any]]:
    """Wonder adjudicates escalations only, and only if configured.

    Returns note dicts (also persisted to agent_traces by the caller).
    Unconfigured -> labelled stub, never simulated as real (CSD 2.9).
    """
    model_id = getattr(settings, "wonder_model_id", "") or ""
    notes: list[dict[str, Any]] = []
    if not stewards:
        return notes
    if not model_id or settings.use_mock_muse:
        for item in stewards[:1]:
            notes.append(
                {
                    "status": "not_available",
                    "attribute": item.attribute,
                    "note": "Wonder: not available in this run (no verified model id).",
                }
            )
        return notes
    from meta_tooling import wonder_adjudicate

    for item in stewards:
        notes.append(
            await wonder_adjudicate(
                settings,
                attribute=item.attribute,
                entity_type=item.entity_type,
                entity_id=item.entity_id,
                evidence={"decision_id": decision_id, "fallback": item.fallback},
                wonder_model_id=model_id,
            )
        )
    return notes


def assemble_and_validate(
    *,
    request: ResolveRequest,
    facts: list[CandidateFact],
    golden: list[GoldenFact],
    policy_version: str,
    taxonomy_version: str,
    golden_attrs: set[str],
    expected_structure: dict[str, Any] | None,
    documents: list[dict[str, Any]],
    decision_id: str,
    created_at: str,
    database_path: str,
    purchased_override: list[dict[str, Any]] | None = None,
) -> tuple[
    list[GoldenFact],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[ConflictFact],
    list[StewardItem],
    bool,
]:
    """Assemble nested golden, validate whole document, gate publishing.

    Only golden_attrs enter the record (order_id stays linkage proof).
    purchased_products are embedded from verified purchased graph edges.
    On field errors: drop the field, record STRUCTURE_MISMATCH + steward,
    re-validate. If a required field is missing after drops, the whole
    golden is withheld (ok=False): no insert, verdict escalates.

    Returns (golden_filtered, purchased_items, purchased_edges,
    conflicts, stewards, ok).
    """
    kept = [fact for fact in golden if fact.attribute in golden_attrs]
    purchased_items, purchased_edges = _verified_purchased_nested(
        request,
        documents,
        decision_id,
        policy_version,
        taxonomy_version,
        created_at,
        database_path,
    )
    if purchased_override is not None:
        purchased_items = purchased_override
    nested = assemble_nested_golden(
        {fact.attribute: {"value": fact.value} for fact in kept}, purchased_items
    )
    if expected_structure is None:
        return kept, purchased_items, purchased_edges, [], [], True

    import copy as _copy

    working = _copy.deepcopy(nested)
    working_kept = list(kept)
    working_purchased = list(purchased_items)
    conflicts: list[ConflictFact] = []
    stewards: list[StewardItem] = []
    deadline = _deadline_from_facts(facts) if facts else created_at

    def _escalate_field(attribute: str, reason: str) -> None:
        conflicts.append(
            ConflictFact(
                entity_type=request.entity_type,
                entity_id=request.entity_id,
                attribute=attribute,
                reason_code=reason,
                rule_fired="STRUCT",
                fact_ids=[],
                policy_version=policy_version,
                taxonomy_version=taxonomy_version,
            )
        )
        stewards.append(
            StewardItem(
                entity_type=request.entity_type,
                entity_id=request.entity_id,
                attribute=attribute,
                owner="steward-001",
                deputy="steward-002",
                deadline=deadline,
                fallback="do not use this value downstream: it failed structure validation",
                status="pending",
                decision_id=decision_id,
                steward_id=f"st-{decision_id}-{attribute}-structure",
            )
        )

    for _ in range(len(working_kept) + len(working_purchased) + 2):
        errors = validate_structure(working, expected_structure)
        if not errors:
            return working_kept, working_purchased, purchased_edges, conflicts, stewards, True
        progressed = False
        for error in errors:
            path = error.get("path", [])
            if not path and error.get("validator") == "required":
                import re as _re

                match = _re.search(r"'([^']+)' is a required property", error.get("reason", ""))
                if match and match.group(1) in working:
                    working.pop(match.group(1), None)
                    working_kept = [f for f in working_kept if f.attribute != match.group(1)]
                    _escalate_field(match.group(1), "STRUCTURE_MISMATCH")
                    progressed = True
                continue
            if path and isinstance(path[0], str):
                if path[0] == "purchased_products" and len(path) > 1 and isinstance(path[1], int):
                    index = path[1]
                    if 0 <= index < len(working_purchased):
                        working_purchased.pop(index)
                        working["purchased_products"] = working_purchased
                        _escalate_field("purchased_products", "STRUCTURE_MISMATCH_NESTED")
                        progressed = True
                    continue
                attribute = path[0]
                if attribute in working:
                    working.pop(attribute, None)
                    working_kept = [f for f in working_kept if f.attribute != attribute]
                    _escalate_field(attribute, "STRUCTURE_MISMATCH")
                    progressed = True
        if not progressed:
            break

    errors = validate_structure(working, expected_structure)
    if errors:
        conflicts.append(
            ConflictFact(
                entity_type=request.entity_type,
                entity_id=request.entity_id,
                attribute="golden",
                reason_code="STRUCTURE_MISMATCH",
                rule_fired="STRUCT",
                fact_ids=[],
                policy_version=policy_version,
                taxonomy_version=taxonomy_version,
            )
        )
        stewards.append(
            StewardItem(
                steward_id=f"st-{decision_id}-golden-structure",
                entity_type=request.entity_type,
                entity_id=request.entity_id,
                attribute="golden",
                owner="steward-001",
                deputy="steward-002",
                deadline=deadline,
                fallback="do not use any value from this golden: structure invalid",
                status="pending",
                decision_id=decision_id,
            )
        )
        return [], [], [], conflicts, stewards, False
    return working_kept, working_purchased, purchased_edges, conflicts, stewards, True


def _verified_purchased_nested(
    request,
    documents,
    decision_id,
    policy_version,
    taxonomy_version,
    created_at,
    database_path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify purchased edges and assemble nestable product goldens.

    Returns (purchased_items, purchased_edges). An edge verifies only when
    an order_id is shared and the target product has a published golden;
    the embedded item carries category + lineage + decision proof.
    """
    product_ids: set[str] = set()
    for document in documents:
        fields = document.get("fields", {})
        ref = fields.get("product_ref")
        if isinstance(ref, dict) and ref.get("value"):
            product_ids.add(str(ref["value"]))
    product_goldens: dict[str, dict[str, Any]] = {}
    for product_id in sorted(product_ids):
        golden = ledger.fetch_latest_golden(database_path, "product", product_id)
        if golden is not None:
            product_goldens[product_id] = golden
    edges, _nodes = derive_purchased_edges(
        documents,
        product_goldens,
        decision_id=decision_id,
        policy_version=policy_version,
        taxonomy_version=taxonomy_version,
        created_at=created_at,
        entity_type=getattr(request, "entity_type", None) or "user",
        entity_id=getattr(request, "entity_id", None) or "",
    )
    items: list[dict[str, Any]] = []
    edge_dicts: list[dict[str, Any]] = []
    for edge in edges:
        if edge.edge_type != "purchased":
            continue
        edge_dicts.append(edge)
    for edge in edges:
        if edge.edge_type != "purchased":
            continue
        for product_id, golden in product_goldens.items():
            # to_node_id is nd-golden-product-{product_id}-{version} (see
            # derive_purchased_edges); match on that exact prefix.
            if edge.to_node_id.startswith(f"nd-golden-product-{product_id}-"):
                attributes = golden.get("attributes", {})
                category = (attributes.get("category") or {}).get("value")
                if category is None and isinstance(attributes.get("category"), str):
                    category = attributes["category"]
                items.append(
                    {
                        "name": product_id,
                        "category": category,
                        "_lineage": {
                            "proof": edge.proof,
                            "rule_fired": edge.rule_fired,
                            "policy_version": edge.policy_version,
                            "taxonomy_version": edge.taxonomy_version,
                            "decision_id": edge.decision_id,
                            "product_decision_id": golden.get("decision_id"),
                        },
                    }
                )
    return items, edge_dicts


def _has_conflicting_unverified_ticket_claim(
    facts: list[CandidateFact], attribute: str, winner_value: str
) -> bool:
    """An unverified ticket fragment against a critical winner is a claim.

    Per CSD Section 9, unverified ticket output is logged as a claim, never
    as a trusted fact. When such a claim conflicts with the derived winner
    on a critical attribute, the attribute escalates for steward review even
    though the engine could publish the authoritative value.
    """
    return any(
        fact.attribute == attribute
        and fact.source == "ticket"
        and not fact.verified
        and fact.value != winner_value
        for fact in facts
    )


def _verdict(
    golden: list[GoldenFact],
    stewards: list[StewardItem],
    conflicts: list[ConflictFact],
) -> str:
    if stewards:
        return "escalated"
    if golden:
        return "published"
    return "denied"


def _primary_rule(derived: DeriveResponse, conflicts: list[ConflictFact]) -> str | None:
    rules = [item.rule_fired for item in conflicts]
    order = {"STRUCT": -1, "R2": 0, "R3": 1, "L1": 2, "C1": 3, "C2": 4, "T1": 5, "R4": 6, "R1": 7}
    best: str | None = None
    best_sev = 8
    for rule in rules:
        sev = order.get(rule, 9)
        if best is None or sev < best_sev:
            best, best_sev = rule, sev
    if best is None:
        for fact in derived.golden_facts:
            return fact.rule_fired
        return "R1"
    return best


def _primary_reason(conflicts: list[ConflictFact], derived: DeriveResponse) -> str | None:
    if conflicts:
        order = {"STRUCT": -1, "R2": 0, "R3": 1, "L1": 2, "C1": 3, "C2": 4, "T1": 5, "R4": 6}
        ranked = sorted(
            conflicts,
            key=lambda item: (order.get(item.rule_fired, 9), item.reason_code),
        )
        return ranked[0].reason_code
    if derived.golden_facts:
        return None
    return "ALL_DENIED"


@dataclass
class _PersistCtx:
    """Everything one atomic decision write needs, including the connection."""

    settings: Settings
    request: ResolveRequest
    facts: list[CandidateFact]
    payload_hash: str
    decision_id: str
    policy_version: str
    taxonomy_version: str
    rule_fired: str | None
    verdict: str
    reason_code: str | None
    golden: list[GoldenFact]
    conflicts: list[ConflictFact]
    stewards: list[StewardItem]
    created_at: str
    links: list | None = None
    verified_links: list | None = None
    traces: dict[str, dict[str, Any]] | None = None
    publish_golden: bool = True
    purchased_edges: list | None = None
    nested_products: list[dict[str, Any]] | None = None
    clusters: list[dict[str, Any]] | None = None
    fact_documents: dict[str, str] | None = None
    connection: Any = None
    golden_id: str = ""
    new_version: int = 0
    merged: dict[str, Any] = field(default_factory=dict)


def _write_facts(ctx: _PersistCtx) -> None:
    """INSERT-OR-IGNORE every candidate fact (idempotent by fact_id)."""
    for fact in ctx.facts:
        ctx.connection.execute(
            """
            INSERT OR IGNORE INTO candidate_facts
              (fact_id, entity_type, entity_id, attribute, value,
               source, source_id, authority_rank, event_time,
               verified, ttl_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.fact_id,
                fact.entity_type,
                fact.entity_id,
                fact.attribute,
                fact.value,
                fact.source,
                fact.source_id,
                fact.authority_rank,
                fact.event_time.isoformat(),
                1 if fact.verified else 0,
                fact.ttl_seconds,
            ),
        )


def _write_decision_row(ctx: _PersistCtx) -> None:
    """INSERT the decision row (UNIQUE idempotency key; race -> 409)."""
    ctx.connection.execute(
        """
        INSERT INTO decisions
          (decision_id, work_id, idempotency_key, payload_hash,
           rule_fired, verdict, reason_code, policy_version,
           taxonomy_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ctx.decision_id,
            ctx.request.work_id,
            ctx.request.idempotency_key,
            ctx.payload_hash,
            ctx.rule_fired,
            ctx.verdict,
            ctx.reason_code,
            ctx.policy_version,
            ctx.taxonomy_version,
            ctx.created_at,
        ),
    )


def _write_golden_and_tombstones(ctx: _PersistCtx) -> None:
    """Merge golden version, INSERT it, tombstone losers and withdrawals.

    publish_golden=False writes nothing: the previous golden stays latest.
    Sets ctx.golden_id/new_version/merged for the graph + response.
    """
    previous = ledger.fetch_latest_golden(
        ctx.settings.database_path, ctx.request.entity_type, ctx.request.entity_id
    )
    previous_attrs: dict[str, Any] = (
        json.loads(json.dumps(previous["attributes"])) if previous else {}
    )
    previous_version = previous["version"] if previous else 0

    ctx.golden_id = f"golden-{ctx.decision_id}"
    ctx.new_version = previous_version + 1
    ctx.merged: dict[str, Any] = {}
    if ctx.publish_golden:
        published_attrs = {fact.attribute for fact in ctx.golden}
        withdrawn = [attr for attr in previous_attrs if attr not in published_attrs]

        ctx.merged = dict(previous_attrs)
        for fact in ctx.golden:
            ctx.merged[fact.attribute] = {
                "value": fact.value,
                "winner_fact_id": fact.winner_fact_id,
                "loser_fact_ids": fact.loser_fact_ids,
                "rule_fired": fact.rule_fired,
                "policy_version": fact.policy_version,
                "taxonomy_version": ctx.taxonomy_version,
                "lineage": fact.lineage,
            }
        if ctx.nested_products:
            ctx.merged["purchased_products"] = ctx.nested_products
        else:
            # No verified purchase this run: drop any stale nesting
            # rather than carrying forward an unproven link.
            ctx.merged.pop("purchased_products", None)
        for attr in withdrawn:
            if attr == "purchased_products":
                continue  # handled above; nested data is not a loser fact
            ctx.merged.pop(attr, None)

        ctx.connection.execute(
            """
            INSERT INTO golden_records
              (golden_id, entity_type, entity_id, version, attributes,
               decision_id, policy_version, taxonomy_version,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ctx.golden_id,
                ctx.request.entity_type,
                ctx.request.entity_id,
                ctx.new_version,
                json.dumps(ctx.merged, sort_keys=True, ensure_ascii=False),
                ctx.decision_id,
                ctx.policy_version,
                ctx.taxonomy_version,
                ctx.created_at,
                ctx.created_at,
            ),
        )

        for fact in ctx.golden:
            for loser_id in fact.loser_fact_ids:
                ledger.insert_tombstone(
                    ctx.connection,
                    f"tb-{ctx.decision_id}-{loser_id}",
                    loser_id,
                    ctx.golden_id,
                    ctx.decision_id,
                    "R1-superseded",
                )
            prev_value = previous_attrs.get(fact.attribute) or {}
            prev_winner = prev_value.get("winner_fact_id") if isinstance(prev_value, dict) else None
            if prev_winner and prev_winner != fact.winner_fact_id:
                ledger.insert_tombstone(
                    ctx.connection,
                    f"tb-{ctx.decision_id}-{prev_winner}-prev",
                    prev_winner,
                    ctx.golden_id,
                    ctx.decision_id,
                    "superseded-by-new-version",
                )
        published_names = {fact.attribute for fact in ctx.golden}
        for attr in [attr_ for attr_ in previous_attrs if attr_ not in published_names]:
            # Nested assembled products have no single winner to tombstone:
            # their history lives in golden versions, not loser facts.
            # Guard against non-dict values (e.g. the purchased_products
            # list) so a withdrawn nested field can never crash the write.
            prev_value = previous_attrs.get(attr)
            prev_winner = prev_value.get("winner_fact_id") if isinstance(prev_value, dict) else None
            if prev_winner:
                ledger.insert_tombstone(
                    ctx.connection,
                    f"tb-{ctx.decision_id}-{prev_winner}-withdrawn",
                    prev_winner,
                    ctx.golden_id,
                    ctx.decision_id,
                    "escalated-no-trusted-value",
                )


def _write_conflicts(ctx: _PersistCtx) -> None:
    """INSERT-OR-IGNORE every conflict row for this decision."""
    for conflict in ctx.conflicts:
        ctx.connection.execute(
            """
            INSERT OR IGNORE INTO conflicts
              (conflict_id, decision_id, entity_type, entity_id,
               attribute, reason_code, rule_fired, fact_ids,
               policy_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"cf-{ctx.decision_id}-{conflict.attribute}-{conflict.reason_code}",
                ctx.decision_id,
                conflict.entity_type,
                conflict.entity_id,
                conflict.attribute,
                conflict.reason_code,
                conflict.rule_fired,
                json.dumps(sorted(conflict.fact_ids)),
                conflict.policy_version,
            ),
        )


def _write_stewards(ctx: _PersistCtx) -> None:
    """INSERT-OR-IGNORE every steward queue row for this decision."""
    for item in ctx.stewards:
        ctx.connection.execute(
            """
            INSERT OR IGNORE INTO steward_queue
              (steward_id, entity_type, entity_id, attribute, owner,
               deputy, deadline, fallback, status, decision_id,
               suggested_action)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.steward_id,
                item.entity_type,
                item.entity_id,
                item.attribute,
                item.owner,
                item.deputy,
                item.deadline.isoformat() if hasattr(item.deadline, "isoformat") else item.deadline,
                item.fallback,
                item.status,
                item.decision_id,
                item.suggested_action,
            ),
        )


def _write_traces(ctx: _PersistCtx) -> None:
    """Persist agent traces (subagents, wonder, prompt guard)."""
    for kind, payload in (ctx.traces or {}).items():
        ledger.insert_agent_trace(
            ctx.connection,
            ctx.decision_id,
            kind,
            payload if isinstance(payload, dict) else {"payload": payload},
            ctx.created_at,
        )


def _write_graph(ctx: _PersistCtx) -> dict | None:
    """Plan + INSERT graph rows in the same transaction; return the golden record dict."""
    golden_record = None
    if ctx.publish_golden:
        nodes, edges = plan_graph(
            entity_type=ctx.request.entity_type,
            entity_id=ctx.request.entity_id,
            facts=ctx.facts,
            proposed_links=ctx.links or [],
            verified_links=ctx.verified_links or [],
            golden_version=ctx.new_version,
            decision_id=ctx.decision_id,
            policy_version=ctx.policy_version,
            taxonomy_version=ctx.taxonomy_version,
            created_at=ctx.created_at,
            clusters=ctx.clusters,
            fact_documents=ctx.fact_documents,
        )
        for purchased_edge in ctx.purchased_edges or []:
            if isinstance(purchased_edge, dict):
                purchased_edge = GraphEdge.model_validate(purchased_edge)
            edges.append(purchased_edge)
        insert_graph_rows(ctx.connection, nodes, edges, ctx.decision_id)
        golden_record = {
            "golden_id": ctx.golden_id,
            "entity_type": ctx.request.entity_type,
            "entity_id": ctx.request.entity_id,
            "version": ctx.new_version,
            "attributes": ctx.merged,
            "decision_id": ctx.decision_id,
            "policy_version": ctx.policy_version,
            "taxonomy_version": ctx.taxonomy_version,
            "created_at": ctx.created_at,
            "updated_at": ctx.created_at,
        }
    return golden_record


def persist_decision(
    *,
    settings: Settings,
    request: ResolveRequest,
    facts: list[CandidateFact],
    payload_hash: str,
    decision_id: str,
    policy_version: str,
    taxonomy_version: str,
    rule_fired: str | None,
    verdict: str,
    reason_code: str | None,
    golden: list[GoldenFact],
    conflicts: list[ConflictFact],
    stewards: list[StewardItem],
    created_at: str,
    links: list | None = None,
    verified_links: list | None = None,
    traces: dict[str, dict[str, Any]] | None = None,
    publish_golden: bool = True,
    purchased_edges: list | None = None,
    nested_products: list[dict[str, Any]] | None = None,
    clusters: list[dict[str, Any]] | None = None,
    fact_documents: dict[str, str] | None = None,
) -> ResolveResponse:
    """Write one decision atomically: facts, decision, golden (if validated),
    tombstones, conflicts, stewards, graph, agent traces.

    publish_golden=False (whole-document structure failure) writes everything
    except the golden record: the previous golden stays latest, nothing is
    silently published. Graph nodes/edges are written in the same transaction
    as the golden (CSD 3.1 [STRUCTURE] gate).
    """
    ctx = _PersistCtx(
        settings=settings,
        request=request,
        facts=facts,
        payload_hash=payload_hash,
        decision_id=decision_id,
        policy_version=policy_version,
        taxonomy_version=taxonomy_version,
        rule_fired=rule_fired,
        verdict=verdict,
        reason_code=reason_code,
        golden=golden,
        conflicts=conflicts,
        stewards=stewards,
        created_at=created_at,
        links=links,
        verified_links=verified_links,
        traces=traces,
        publish_golden=publish_golden,
        purchased_edges=purchased_edges,
        nested_products=nested_products,
        clusters=clusters,
        fact_documents=fact_documents,
    )
    ctx.connection = ledger.connect(settings.database_path)
    try:
        with ctx.connection:
            _write_facts(ctx)
            _write_decision_row(ctx)
            _write_golden_and_tombstones(ctx)
            _write_conflicts(ctx)
            _write_stewards(ctx)
            _write_traces(ctx)
            golden_record = _write_graph(ctx)
    except sqlite3.IntegrityError as exc:
        raise IdempotencyConflictError(
            "decision write conflict (duplicate key or version race); no partial write was kept"
        ) from exc
    finally:
        ctx.connection.close()
    return ResolveResponse(
        decision_id=decision_id,
        entity_type=request.entity_type,
        entity_id=request.entity_id,
        verdict=verdict,  # type: ignore[arg-type]
        golden=GoldenRecord.model_validate(golden_record) if golden_record else None,
        conflicts=conflicts,
        steward_queue=stewards,
        policy_version=policy_version,
        taxonomy_version=taxonomy_version,
    )


def replay_response(settings: Settings, decision_id: str) -> ResolveResponse:
    """Rebuild a stored response from the ledger. No engine call, no write."""

    decision = ledger.fetch_decision(settings.database_path, decision_id)
    if decision is None:
        raise KeyError(f"decision {decision_id} not found")
    goldens = ledger.fetch_goldens_by_decision(settings.database_path, decision_id)
    latest = goldens[-1] if goldens else None
    conflicts = [
        ConflictFact.model_validate(
            {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "attribute": row["attribute"],
                "reason_code": row["reason_code"],
                "rule_fired": row["rule_fired"],
                "fact_ids": row["fact_ids"],
                "policy_version": row["policy_version"],
            }
        )
        for row in ledger.fetch_conflicts_by_decision(settings.database_path, decision_id)
    ]
    stewards = [
        StewardItem.model_validate(
            {
                "steward_id": row["steward_id"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "attribute": row["attribute"],
                "owner": row["owner"],
                "deputy": row.get("deputy"),
                "deadline": row["deadline"],
                "fallback": row["fallback"],
                "status": row["status"],
                "decision_id": row["decision_id"],
            }
        )
        for row in ledger.fetch_stewards_by_decision(settings.database_path, decision_id)
    ]
    entity_type = entity_id = ""
    if latest:
        entity_type, entity_id = latest["entity_type"], latest["entity_id"]
    if not entity_type and stewards:
        entity_type, entity_id = stewards[0].entity_type, stewards[0].entity_id
    if not entity_type and conflicts:
        entity_type, entity_id = conflicts[0].entity_type, conflicts[0].entity_id
    return ResolveResponse(
        decision_id=decision_id,
        entity_type=entity_type,
        entity_id=entity_id,
        verdict=decision["verdict"],
        golden=GoldenRecord.model_validate(latest) if latest else None,
        conflicts=conflicts,
        steward_queue=stewards,
        policy_version=decision["policy_version"],
        taxonomy_version=decision.get("taxonomy_version"),
    )

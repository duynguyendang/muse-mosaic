"""GET /explain — replay a decision: inputs, proposal, tree, traces.

Read-only: reassembles the proposal deterministically (the live Muse text
is never stored) and rebuilds the derivation tree from the graph.
"""

from __future__ import annotations

import json
from typing import Any

import ledger
from config import Settings
from fastapi import APIRouter, HTTPException, Request
from graph import derivation_tree as build_tree
from graph import fetch_graph
from proposer import mock_proposal
from schemas import CandidateFact, ConflictFact, ExplainResponse, StewardItem

from routes.deps import _row_to_fact, logger

router = APIRouter()


def _explain_inputs(
    decision: dict[str, Any],
    goldens: list[dict[str, Any]],
    conflicts: list[ConflictFact],
    stewards: list[StewardItem],
) -> tuple[str, str, str | None, list[str], list[str]]:
    """Who/what went into the decision: entity, winner, losers, input ids."""
    entity_type = entity_id = ""
    winner_fact_id: str | None = None
    loser_fact_ids: list[str] = []
    if goldens:
        latest = goldens[-1]
        entity_type, entity_id = latest["entity_type"], latest["entity_id"]
        for attribute in sorted(latest["attributes"]):
            value = latest["attributes"][attribute]
            if winner_fact_id is None:
                winner_fact_id = value.get("winner_fact_id")
            loser_fact_ids.extend(value.get("loser_fact_ids", []))
    if not entity_type and stewards:
        entity_type = stewards[0].entity_type
        entity_id = stewards[0].entity_id
    if not entity_type and conflicts:
        entity_type = conflicts[0].entity_type
        entity_id = conflicts[0].entity_id
    input_ids = sorted(
        {winner_fact_id, *loser_fact_ids}
        | {fact for conflict in conflicts for fact in conflict.fact_ids} - {None}
    )
    return entity_type, entity_id, winner_fact_id, loser_fact_ids, input_ids


def _row_to_conflict(row: dict[str, Any]) -> ConflictFact:
    """Ledger conflict row -> ConflictFact (drop storage ids, parse JSON lists)."""
    fact_ids = row.get("fact_ids")
    if isinstance(fact_ids, str):
        try:
            fact_ids = json.loads(fact_ids)
        except ValueError:
            fact_ids = []
    return ConflictFact(
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        attribute=row["attribute"],
        reason_code=row["reason_code"],
        rule_fired=row["rule_fired"],
        fact_ids=list(fact_ids or []),
        policy_version=row["policy_version"],
        taxonomy_version=row.get("taxonomy_version"),
    )


def _row_to_steward(row: dict[str, Any]) -> StewardItem:
    """Ledger steward row -> StewardItem (drop storage-only columns)."""
    return StewardItem(
        steward_id=row["steward_id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        attribute=row["attribute"],
        owner=row["owner"],
        deputy=row.get("deputy"),
        deadline=row["deadline"],
        fallback=row["fallback"],
        status=row["status"],
        decision_id=row["decision_id"],
        suggested_action=row.get("suggested_action"),
    )


def _replay_proposal(current: Settings, entity_type: str, entity_id: str) -> dict[str, Any] | None:
    """Deterministic proposal replay (the live Muse text is never stored)."""
    if not (entity_type and entity_id):
        return None
    all_facts = ledger.fetch_candidate_facts(current.database_path, entity_type, entity_id)
    if not all_facts:
        return None
    # Replay needs full CandidateFacts (fact_id included); _row_to_fact alone
    # is Fragment-shaped and would fail validation (fact_id required).
    replay_facts = [
        CandidateFact.model_validate(
            {
                **_row_to_fact(row),
                "fact_id": row["fact_id"],
                "entity_type": entity_type,
                "entity_id": entity_id,
            }
        )
        for row in all_facts
    ]
    proposal = mock_proposal(replay_facts)
    if not current.use_mock_muse:
        # The live Muse proposal was returned at resolve time and
        # is not stored; replay reconstructs it deterministically
        # so the shape stays comparable. Never presented as the
        # original model text.
        proposal["note"] = (
            "Reconstructed deterministic proposal for replay; the "
            "original Muse Spark proposal was returned live at "
            "resolve time and is not stored in the ledger."
        )
    return proposal


def _derivation_tree(
    current: Settings,
    decision_id: str,
    entity_type: str,
    entity_id: str,
    winner_fact_id: str | None,
) -> list[str]:
    """Verified/proposed link tree from the graph; [] when unbuildable."""
    try:
        node_rows, edge_rows = fetch_graph(current.database_path, entity_type, entity_id)
        _ = node_rows
        return build_tree(
            [],
            [
                {
                    "from_fact_id": edge["from_node_id"],
                    "to_fact_id": edge["to_node_id"],
                    "proof": edge.get("proof") or "",
                }
                for edge in edge_rows
                if edge.get("edge_type") in ("verified_link", "proposed_link")
            ],
            winner_fact_id,
        )
    except Exception as exc:
        logger.warning("explain: derivation tree failed for %s, omitting: %s", decision_id, exc)
        return []


@router.get("/explain/{decision_id}")
def explain(decision_id: str, request: Request) -> dict[str, Any]:
    current: Settings = request.app.state.settings
    decision = ledger.fetch_decision(current.database_path, decision_id)
    if decision is None:
        raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")
    goldens = ledger.fetch_goldens_by_decision(current.database_path, decision_id)
    conflicts = [
        _row_to_conflict(dict(row))
        for row in ledger.fetch_conflicts_by_decision(current.database_path, decision_id)
    ]
    stewards = [
        _row_to_steward(dict(row))
        for row in ledger.fetch_stewards_by_decision(current.database_path, decision_id)
    ]
    entity_type, entity_id, winner, losers, input_ids = _explain_inputs(
        decision, goldens, conflicts, stewards
    )
    fact_rows = ledger.fetch_candidate_facts(
        current.database_path, entity_type, entity_id, input_ids or None
    )
    authority_ranks = {row["fact_id"]: row["authority_rank"] for row in fact_rows}
    event_times = {row["fact_id"]: row["event_time"] for row in fact_rows}
    proposal = _replay_proposal(current, entity_type, entity_id)
    traces = {
        row["kind"]: row["payload"]
        for row in ledger.fetch_agent_traces(current.database_path, decision_id)
    }
    subagent_trace = traces.get("subagents", {}).get("trace", [])
    wonder_notes = traces.get("wonder", {}).get("notes", [])
    wonder_note = wonder_notes[0] if wonder_notes else None
    prompt_flags = traces.get("prompt_guard", {})
    derivation_tree = _derivation_tree(current, decision_id, entity_type, entity_id, winner)
    return ExplainResponse(
        decision_id=decision_id,
        entity_type=entity_type,
        entity_id=entity_id,
        verdict=decision["verdict"],
        rule_fired=decision.get("rule_fired"),
        winner_fact_id=winner,
        loser_fact_ids=sorted(set(losers)),
        authority_ranks=authority_ranks,
        event_times=event_times,
        input_fact_ids=input_ids,
        policy_version=decision["policy_version"],
        taxonomy_version=decision.get("taxonomy_version"),
        proposal=proposal,
        conflicts=conflicts,  # type: ignore[arg-type]
        steward_queue=stewards,  # type: ignore[arg-type]
        derivation_tree=derivation_tree,
        subagent_trace=subagent_trace,
        wonder_note=wonder_note,
        prompt_guard_flag=prompt_flags or None,
    ).model_dump(mode="json")

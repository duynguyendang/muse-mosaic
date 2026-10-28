"""Muse Mosaic public API.

Implemented (fail-closed where noted):
- GET /health — mosaic-api status + trust-engine reachability + active policy
  versions (read from the bundled policy mirror, and from trust-engine when up).
- GET /fragments/{entity_type}/{entity_id} — labelled synthetic fixtures.
- POST /resolve — ingest + mock propose + trust-engine derive + gated publish.
  Engine unreachable -> 503 with steward escalation, no golden, no decision row.
- GET /golden/{entity_type}/{entity_id} — latest golden or 404.
- GET /explain/{decision_id} — replay from the ledger (never vector search).
- POST /steward/{id}/approve|reject — steward decision as a new fact.
- POST /steward/{id}/split — restore a prior golden version from history.
- GET /steward/pending — steward queue rebuilt from SQLite on each request.
- GET /similar — structured similar search (vector recall skipped in v1).
- GET / — static two-column UI shell.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

import ledger
from config import Settings, get_settings
from ingest import document_to_fragments, load_mock_fragments, load_mock_tickets
from policies import PolicyError, active_version, load_policies
from publisher import (
    EngineUnavailableError,
    IdempotencyConflictError,
    _primary_reason,
    _primary_rule,
    _verdict,
    escalate_unavailable,
    resolve_flow,
)
from schemas import (
    Document as DocumentModel,
    FragmentsResponse,
    GoldenRecord,
    HealthResponse,
    ResolveRequest,
    SimilarResponse,
    StewardAcceptRequest,
    StewardApproveRequest,
    StewardSplitRequest,
)


ENTITY_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Last observed matching-engine failure (S-05). /health reports splink
# unavailable while this is set — even if a probe succeeds — until a later
# /match succeeds and clears it. Process-local last-known state: a recorded
# match failure is never hidden by a passing probe.
_SPLINK_LAST_ERROR: str | None = None


def _note_splink_result(out: dict[str, Any]) -> None:
    """Record or clear the sticky matching-engine failure."""
    global _SPLINK_LAST_ERROR
    if out.get("status") == "ok":
        _SPLINK_LAST_ERROR = None
    else:
        _SPLINK_LAST_ERROR = str(out.get("detail", "unavailable"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    ledger.init_db(settings.database_path)
    try:
        app.state.policies = load_policies()
    except PolicyError as exc:
        raise RuntimeError(f"cannot start without policies: {exc}") from exc
    try:
        from taxonomies import load_taxonomies

        app.state.taxonomies = load_taxonomies()
    except Exception as exc:
        raise RuntimeError(f"cannot start without taxonomies: {exc}") from exc
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    app_settings.validate_startup()

    app = FastAPI(
        title="Muse Mosaic",
        version="0.2.0",
        description=(
            "Many fragments, one trusted picture, with proof for every piece. "
            "Muse proposes; the manglekit trust-engine derives; "
            "the publisher writes only derived values."
        ),
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    try:
        app.state.policies = load_policies()
    except PolicyError as exc:
        raise RuntimeError(f"cannot start without policies: {exc}") from exc
    try:
        from taxonomies import default_version, load_taxonomies

        app.state.taxonomies = load_taxonomies()
        app.state.expected_structures = _load_expected_structures()
    except Exception as exc:
        raise RuntimeError(f"cannot start without taxonomies: {exc}") from exc

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        current: Settings = request.app.state.settings
        trust_engine = await _trust_engine_health(current.health_url())
        splink_status, splink_versions = await _matching_engine_health(current)
        policies = request.app.state.policies
        user_version = policies["user"].policy_version
        product_version = policies["product"].policy_version
        if trust_engine is not None:
            user_version = trust_engine.get("policy_version_user") or user_version
            product_version = (
                trust_engine.get("policy_version_product") or product_version
            )
        return HealthResponse(
            mosaic_api="ok",
            trust_engine="ok" if trust_engine is not None else "down",
            splink=splink_status,
            splink_version=splink_versions.get("splink_version"),
            duckdb_version=splink_versions.get("duckdb_version"),
            policy_version_user=user_version,
            policy_version_product=product_version,
        )

    @app.get(
        "/fragments/{entity_type}/{entity_id}", response_model=FragmentsResponse
    )
    def get_fragments(
        entity_type: str, entity_id: str, request: Request
    ) -> FragmentsResponse:
        _check_entity(entity_type)
        current: Settings = request.app.state.settings
        try:
            fragments = load_mock_fragments(
                current.mock_data_dir, entity_type, entity_id
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        tickets = load_mock_tickets(current.mock_data_dir, entity_type, entity_id)
        return FragmentsResponse(
            synthetic=True,
            notice=(
                "SYNTHETIC DEMO DATA. No real people, systems or integrations. "
                "All sources are labelled '-like'."
            ),
            entity_type=entity_type,
            entity_id=entity_id,
            fragments=fragments,
            tickets=tickets,
        )

    @app.post("/resolve")
    async def resolve(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid JSON body") from exc
        try:
            resolve_request = ResolveRequest.model_validate(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        current: Settings = request.app.state.settings
        try:
            # /resolve is the single-entity flat path: no expected structure,
            # so no whole-document validation (that gate lives on /build).
            response, proposal = await resolve_flow(
                resolve_request, current, request.app.state.policies,
                request.app.state.taxonomies,
            )
        except PolicyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EngineUnavailableError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "verdict": "escalated",
                    "decision_id": exc.decision_id,
                    "reason_code": exc.reason_code,
                    "message": str(exc),
                    "steward_queue": exc.steward_items,
                    "fail_closed": True,
                },
            ) from exc
        payload = response.model_dump(mode="json")
        payload["proposal"] = proposal.model_dump(mode="json")
        return payload

    @app.get("/golden/{entity_type}/{entity_id}", response_model=GoldenRecord)
    def get_golden(entity_type: str, entity_id: str, request: Request) -> GoldenRecord:
        _check_entity(entity_type)
        current: Settings = request.app.state.settings
        record = ledger.fetch_latest_golden(
            current.database_path, entity_type, entity_id
        )
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"no golden record for {entity_type}/{entity_id}",
            )
        try:
            return GoldenRecord.model_validate(record)
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"ledger record failed the golden contract: {exc}",
            ) from exc

    @app.get("/explain/{decision_id}")
    def explain(decision_id: str, request: Request) -> dict[str, Any]:
        from proposer import mock_proposal
        from schemas import ExplainResponse

        current: Settings = request.app.state.settings
        decision = ledger.fetch_decision(current.database_path, decision_id)
        if decision is None:
            raise HTTPException(
                status_code=404, detail=f"decision {decision_id} not found"
            )
        goldens = ledger.fetch_goldens_by_decision(
            current.database_path, decision_id
        )
        conflicts = ledger.fetch_conflicts_by_decision(
            current.database_path, decision_id
        )
        stewards = ledger.fetch_stewards_by_decision(
            current.database_path, decision_id
        )
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
            entity_type = stewards[0]["entity_type"]
            entity_id = stewards[0]["entity_id"]
        if not entity_type and conflicts:
            entity_type = conflicts[0]["entity_type"]
            entity_id = conflicts[0]["entity_id"]

        input_ids = sorted(
            {winner_fact_id, *loser_fact_ids}
            | {fact for conflict in conflicts for fact in conflict["fact_ids"]}
            - {None}
        )
        fact_rows = ledger.fetch_candidate_facts(
            current.database_path, entity_type, entity_id, input_ids or None
        )
        authority_ranks = {
            row["fact_id"]: row["authority_rank"] for row in fact_rows
        }
        event_times = {row["fact_id"]: row["event_time"] for row in fact_rows}

        proposal = None
        if entity_type and entity_id:
            all_facts = ledger.fetch_candidate_facts(
                current.database_path, entity_type, entity_id
            )
            if all_facts:
                from schemas import CandidateFact

                proposal = mock_proposal(
                    [CandidateFact.model_validate(_row_to_fact(row)) for row in all_facts]
                )
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

        traces = {
            row["kind"]: row["payload"]
            for row in ledger.fetch_agent_traces(current.database_path, decision_id)
        }
        subagent_trace = traces.get("subagents", {}).get("trace", [])
        wonder_notes = traces.get("wonder", {}).get("notes", [])
        wonder_note = wonder_notes[0] if wonder_notes else None
        prompt_flags = traces.get("prompt_guard", {})

        derivation_tree: list[str] = []
        try:
            from graph import derivation_tree as build_tree, fetch_graph

            node_rows, edge_rows = fetch_graph(
                current.database_path, entity_type, entity_id
            )
            _ = node_rows
            derivation_tree = build_tree([], [
                {"from_fact_id": edge["from_node_id"],
                 "to_fact_id": edge["to_node_id"],
                 "proof": edge.get("proof") or ""}
                for edge in edge_rows
                if edge.get("edge_type") in ("verified_link", "proposed_link")
            ], winner_fact_id)
        except Exception:
            derivation_tree = []

        return ExplainResponse(
            decision_id=decision_id,
            entity_type=entity_type,
            entity_id=entity_id,
            verdict=decision["verdict"],
            rule_fired=decision.get("rule_fired"),
            winner_fact_id=winner_fact_id,
            loser_fact_ids=sorted(set(loser_fact_ids)),
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

    @app.get("/steward/pending")
    def steward_pending(request: Request) -> dict[str, Any]:
        current: Settings = request.app.state.settings
        return {
            "pending": ledger.fetch_pending_stewards(current.database_path),
        }

    @app.post("/steward/{steward_id}/approve")
    async def steward_approve(steward_id: str, request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid JSON body") from exc
        try:
            approval = StewardApproveRequest.model_validate(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        current: Settings = request.app.state.settings
        item = ledger.fetch_steward(current.database_path, steward_id)
        if item is None:
            raise HTTPException(
                status_code=404, detail=f"steward item {steward_id} not found"
            )
        if item["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"steward item {steward_id} is {item['status']}, not pending",
            )
        return await _steward_derive(
            request, item, approval.value, "approved",
            source="steward-approved",
            source_id=f"steward-{steward_id}",
        )

    @app.post("/steward/{steward_id}/reject")
    def steward_reject(steward_id: str, request: Request) -> dict[str, Any]:
        current: Settings = request.app.state.settings
        item = ledger.fetch_steward(current.database_path, steward_id)
        if item is None:
            raise HTTPException(
                status_code=404, detail=f"steward item {steward_id} not found"
            )
        if item["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"steward item {steward_id} is {item['status']}, not pending",
            )
        ledger.update_steward_status(
            current.database_path, steward_id, "rejected"
        )
        return {"steward_id": steward_id, "status": "rejected"}

    @app.post("/steward/{steward_id}/split")
    async def steward_split(steward_id: str, request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="invalid JSON body")
        return _do_split(steward_id, request, body)

    @app.get("/similar", response_model=SimilarResponse)
    def similar(
        request: Request,
        entity_type: str = Query(min_length=1),
        entity_id: str = Query(min_length=1),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> SimilarResponse:
        from similar_search import similar as run_similar

        _check_entity(entity_type)
        current: Settings = request.app.state.settings
        result = run_similar(
            current.database_path,
            entity_type=entity_type,
            entity_id=entity_id,
            limit=limit,
        )
        return SimilarResponse.model_validate(result)

    @app.post("/build")
    async def build(request: Request) -> dict[str, Any]:
        """Two-input build: dataset + expected structure (CSD 3.6, 12.4).

        Ingest Documents -> MATCH (Splink + Muse sub-agents, claims only) ->
        GRAPH -> DERIVE per cluster -> STRUCTURE -> publish or escalate.
        """
        from ingest import document_to_fragments
        from schemas import BuildRequest, ProposedLink

        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid JSON body") from exc
        try:
            build_request = BuildRequest.model_validate(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        current: Settings = request.app.state.settings
        policies = request.app.state.policies
        taxonomies = getattr(request.app.state, "taxonomies", None)
        expected_structures = getattr(request.app.state, "expected_structures", {}) or {}

        documents = [doc.model_dump() for doc in build_request.dataset.documents]
        for document in documents:
            document["content_hash"] = document.get("content_hash") or document["document_id"]

        # MATCH: Splink candidates (if enabled) + Muse sub-agent claims.
        # Blocking/threshold come from each entity type's policy file.
        from splink_adapter import flatten_documents, run_splink

        splink_rows = flatten_documents(documents)
        first_type = next((doc.get("entity_type", "user") for doc in documents), "user")
        first_policy = policies.get(first_type)
        splink_cfg = getattr(first_policy, "splink", None) if first_policy else None
        threshold = float(getattr(splink_cfg, "threshold", 0.9) or 0.9)
        blocking = list(getattr(splink_cfg, "blocking", None) or ["email", "phone", "order_id"])
        # Splink runs in its own process; await it without blocking the loop.
        splink_out = await asyncio.to_thread(
            run_splink,
            splink_rows,
            enabled=bool(getattr(current, "splink_enabled", True)),
            threshold=threshold,
            blocking=blocking,
            service_url=current.matching_service_url,
            policy_version=getattr(first_policy, "policy_version", "unknown") if first_policy else "unknown",
            work_id=build_request.work_id,
            known_policy_versions=sorted({policy.policy_version for policy in policies.values()}),
        )
        _note_splink_result(splink_out)
        splink_status = splink_out["status"]
        if splink_status != "ok":
            splink_status = f"unavailable ({splink_out.get('detail', '')})"

        # Cluster: Splink clusters when available, else exact-identifier
        # connected components (Muse + fuzzy fallback).

        clusters = await _build_clusters(
            current, policies=policies, taxonomies=taxonomies,
            documents=documents, splink_out=splink_out,
        )

        goldens: list[dict[str, Any]] = []
        conflicts_all: list[dict[str, Any]] = []
        stewards_all: list[dict[str, Any]] = []
        verdicts: list[str] = []
        policy_version = ""
        taxonomy_version = ""
        for index, cluster in enumerate(clusters):
            member_docs = cluster["member_docs"]
            entity_type = cluster["entity_type"]
            entity_id = cluster.get("entity_id") or f"cluster-{index + 1}"
            policy = policies.get(entity_type)
            if policy is None:
                continue
            policy_version = policy.policy_version
            taxonomy = None
            if taxonomies:
                from taxonomies import default_version

                try:
                    taxonomy_version = default_version(taxonomies, entity_type)
                    taxonomy = taxonomies[taxonomy_version]
                except Exception:
                    taxonomy_version = ""
            allowed = set(policy.allowed_attributes)
            fragments = []
            fact_documents: dict[str, str] = {}
            for document in member_docs:
                try:
                    doc_model = DocumentModel.model_validate(document)
                except ValueError:
                    continue
                doc_frags, _doc_claims = document_to_fragments(
                    doc_model, entity_id, taxonomy, allowed
                )
                for fragment in doc_frags:
                    fragments.append(fragment)
                    fact_documents[f"{entity_id}-{fragment.source_id}"] = document["document_id"]
            from ingest import to_candidate_facts
            from publisher import (
                apply_proposer_gate, assemble_and_validate,
                decision_id_for, escalate_unavailable, persist_decision,
                run_builder_pipeline, utcnow_iso,
            )
            from trust import TrustEngineError, derive as trust_derive

            key = f"{build_request.idempotency_key}-c{index}"
            facts = to_candidate_facts(entity_type, entity_id, fragments, work_id=build_request.work_id)
            # Remap synonym values (Taxonomist claim, logged).
            if taxonomy is not None:
                from taxonomies import canonical_term

                remapped = []
                for fact in facts:
                    canonical, kind = canonical_term(taxonomy, fact.attribute, fact.value)
                    if kind in ("term_exact", "synonym_exact") and canonical != fact.value:
                        remapped.append(fact.model_copy(update={"value": canonical}))
                    else:
                        remapped.append(fact)
                facts = remapped
            proposal_dict, link_dicts, trace = await run_builder_pipeline(
                facts, current, taxonomy, allowed, fact_documents or None,
                extra_links=_splink_link_claims(
                    splink_out, member_docs, facts, fact_documents
                ),
            )
            from schemas import DeriveRequest, Proposal

            proposal = Proposal.model_validate(proposal_dict)
            links = [ProposedLink.model_validate(item) for item in link_dicts]
            payload_hash = hashlib.sha256(
                json.dumps({"cluster": cluster["cluster_id"],
                            "members": sorted(fact_documents.values())},
                           sort_keys=True).encode()
            ).hexdigest()
            cluster_decision_id = decision_id_for(key, payload_hash)
            created_at = utcnow_iso()
            stored = ledger.fetch_decision_by_key(current.database_path, key)
            if stored is not None:
                if stored["payload_hash"] == payload_hash:
                    replayed = ledger.fetch_goldens_by_decision(
                        current.database_path, stored["decision_id"]
                    )
                    if replayed:
                        goldens.extend(dict(row) for row in replayed)
                    else:
                        goldens.append({"replayed_decision_id": stored["decision_id"]})
                    for row in ledger.fetch_conflicts_by_decision(
                        current.database_path, stored["decision_id"]
                    ):
                        conflicts_all.append(dict(row))
                    for row in ledger.fetch_stewards_by_decision(
                        current.database_path, stored["decision_id"]
                    ):
                        stewards_all.append(dict(row))
                    verdicts.append(stored["verdict"])
                    continue
                raise HTTPException(
                    status_code=409,
                    detail=f"idempotency key {key!r} reused with a different payload",
                )
            derive_request = DeriveRequest(
                decision_id=cluster_decision_id,
                entity_type=entity_type,
                entity_id=entity_id,
                idempotency_key=key,
                payload_hash=payload_hash,
                policy_version=policy.policy_version,
                taxonomy_version=taxonomy_version,
                candidate_facts=facts,
                links=links,
            )
            try:
                derived = await trust_derive(current, derive_request)
            except TrustEngineError as exc:
                steward_rows = escalate_unavailable(
                    current,
                    ResolveRequest(work_id=build_request.work_id, entity_type=entity_type,
                                   entity_id=entity_id, idempotency_key=key, fragments=fragments),
                    facts, cluster_decision_id, policy.policy_version, created_at,
                )
                raise HTTPException(
                    status_code=503,
                    detail={"verdict": "escalated", "decision_id": cluster_decision_id,
                            "reason_code": "TRUST_ENGINE_UNAVAILABLE", "message": str(exc),
                            "steward_queue": steward_rows, "fail_closed": True},
                ) from exc
            golden, item_conflicts, item_stewards = apply_proposer_gate(
                ResolveRequest(work_id=build_request.work_id, entity_type=entity_type,
                               entity_id=entity_id, idempotency_key=key, fragments=fragments),
                facts, proposal, derived, cluster_decision_id,
                critical_attributes=set(policy.critical_attributes),
            )
            from publisher import taxonomy_steward_items

            tax_conflicts, tax_stewards = taxonomy_steward_items(
                proposal, entity_type, entity_id, cluster_decision_id, facts,
                created_at, policy_version=policy.policy_version,
                taxonomy_version=taxonomy_version,
            )
            item_conflicts = list(item_conflicts) + tax_conflicts
            item_stewards = list(item_stewards) + tax_stewards
            expected = (build_request.expected_structure
                        or expected_structures.get(entity_type))
            (assembled, _nested, _purchased, struct_conflicts,
             struct_stewards, struct_ok) = assemble_and_validate(
                request=ResolveRequest(work_id=build_request.work_id, entity_type=entity_type,
                                       entity_id=entity_id, idempotency_key=key, fragments=fragments),
                facts=facts, golden=golden, policy_version=policy.policy_version,
                taxonomy_version=taxonomy_version,
                golden_attrs=set(policy.golden_attributes or [f.attribute for f in facts]),
                expected_structure=expected, documents=member_docs,
                decision_id=cluster_decision_id, created_at=created_at,
                database_path=current.database_path,
            )
            item_conflicts = list(item_conflicts) + struct_conflicts
            item_stewards = list(item_stewards) + struct_stewards
            response = persist_decision(
                settings=current,
                request=ResolveRequest(work_id=build_request.work_id, entity_type=entity_type,
                                       entity_id=entity_id, idempotency_key=key, fragments=fragments),
                facts=facts, payload_hash=payload_hash, decision_id=cluster_decision_id,
                policy_version=policy.policy_version, taxonomy_version=taxonomy_version,
                rule_fired=_primary_rule(derived, item_conflicts),
                verdict=_verdict(assembled, item_stewards, item_conflicts),
                reason_code=_primary_reason(item_conflicts, derived),
                golden=assembled if struct_ok else [],
                conflicts=item_conflicts, stewards=item_stewards, created_at=created_at,
                links=links, verified_links=derived.verified_links,
                traces={"subagents": {"trace": [dict(step) for step in trace],
                                      "taxonomy_claims": [], "usage": proposal.usage,
                                      "mode": proposal.mode, "model": proposal.model}},
                publish_golden=struct_ok,
                nested_products=_nested,
                purchased_edges=_purchased,
                clusters=splink_out.get("clusters"),
                fact_documents=fact_documents,
            )
            payload = response.model_dump(mode="json")
            goldens.append(payload.get("golden") or {"decision_id": cluster_decision_id,
                                                     "verdict": payload.get("verdict")})
            conflicts_all.extend(payload.get("conflicts", []))
            stewards_all.extend(payload.get("steward_queue", []))
            verdicts.append(payload.get("verdict", "denied"))

        overall = "published" if goldens and all(v == "published" for v in verdicts) else (
            "escalated" if (stewards_all or any(v == "escalated" for v in verdicts)) else "denied"
        )
        return {
            "decision_id": decision_id_for(build_request.idempotency_key,
                                           hashlib.sha256(build_request.idempotency_key.encode()).hexdigest()),
            "verdict": overall,
            "goldens": goldens,
            "conflicts": conflicts_all,
            "steward_queue": stewards_all,
            "policy_version_user": policies["user"].policy_version,
            "taxonomy_version": taxonomy_version,
            "splink_status": splink_status,
            "splink": {
                "status": splink_out.get("status", "unavailable"),
                "clusters": splink_out.get("clusters", []),
                "pairs": splink_out.get("pairs", []),
                "detail": splink_out.get("detail", ""),
                "splink_version": splink_out.get("splink_version"),
                "duckdb_version": splink_out.get("duckdb_version"),
            },
        }

    @app.post("/upload")
    async def upload(request: Request) -> dict[str, Any]:
        """Upload JSON fragments (CSD 12.4). Upload provides fragments only;
        every fragment goes through the same validation, MATCH, graph, derive,
        publisher gate and ledger write as fixed fragments. Nothing uploads
        directly to a golden."""
        from ingest import detect_entity_type, parse_upload
        from schemas import UploadResponse

        form = await request.form()
        file = form.get("file")
        if file is None or not hasattr(file, "filename") or not hasattr(file, "read"):
            raise HTTPException(status_code=422, detail="multipart field 'file' is required")
        raw = (await file.read()).decode("utf-8", errors="replace")
        try:
            fragments, rejected = parse_upload(file.filename or "upload.json", raw)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        entity_type, confidence = detect_entity_type(
            {"fragments": fragments} if fragments else {}
        )
        if entity_type == "unknown":
            entity_type = "user_profile"
        link_proposals: list[dict[str, Any]] = []
        try:
            from subagents import mock_linker

            current: Settings = request.app.state.settings
            from schemas import CandidateFact

            facts = to_candidate_facts(
                "user" if entity_type == "user_profile" else "product",
                "upload-preview",
                [Fragment.model_validate(item) for item in fragments],
                work_id="upload-preview",
            )
            by_fact = {fact.fact_id: fact for fact in facts}
            _ = by_fact
            links, _trace = mock_linker(facts, None)
            link_proposals = [link.model_dump() for link in links]
        except Exception:
            link_proposals = []
        return UploadResponse(
            file_name=file.filename or "upload.json",
            detected_entity_type=entity_type,
            detected_confidence=confidence,  # type: ignore[arg-type]
            fragments_parsed=len(fragments),
            fragments=fragments,
            fragments_rejected=rejected,
            link_proposals=link_proposals,  # type: ignore[arg-type]
        ).model_dump(mode="json")

    @app.get("/graph/{entity_type}/{entity_id}")
    def graph(
        request: Request,
        entity_type: str,
        entity_id: str,
        path_from: str | None = None,
        path_to: str | None = None,
        impact: str | None = None,
    ) -> dict[str, Any]:
        """Render the persisted Data Graph (canvas reads this).

        Optional pathfinder (?path_from=&path_to=) and blast radius
        (?impact=<field>) run on the same graph.
        """
        from graph import fetch_graph, impact as blast_radius, pathfinder
        from schemas import GraphResponse

        _check_entity(entity_type)
        current: Settings = request.app.state.settings
        nodes, edges = fetch_graph(current.database_path, entity_type, entity_id)
        path: list[str] = []
        impact_result: dict[str, Any] = {}
        if path_from and path_to:
            path = pathfinder(nodes, edges, path_from, path_to)
        if impact:
            impact_result = blast_radius(nodes, edges, impact)
        return GraphResponse(
            entity_type=entity_type,
            entity_id=entity_id,
            nodes=nodes,  # type: ignore[arg-type]
            edges=edges,  # type: ignore[arg-type]
            path=path,
            impact=impact_result,
        ).model_dump(mode="json")

    @app.get("/downstream/{agent}")
    def downstream(
        agent: str,
        request: Request,
        entity_type: str = Query(min_length=1),
        entity_id: str = Query(min_length=1),
        field: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Downstream two-answer mock: a golden, or No trusted value.

        Agents: otp, refund, search. No fragment fallback, no score fallback —
        a downstream that cannot accept a stop answer must not consume goldens.
        """
        from schemas import DownstreamAnswer

        if agent not in ("otp", "refund", "search"):
            raise HTTPException(status_code=404, detail=f"unknown downstream agent {agent!r}")
        _check_entity(entity_type)
        current: Settings = request.app.state.settings
        wanted = field or ("category" if entity_type == "product" else "phone")
        golden = ledger.fetch_latest_golden(current.database_path, entity_type, entity_id)
        if golden is None:
            return DownstreamAnswer(
                answer="no_trusted_value", agent=agent,
                entity_type=entity_type, entity_id=entity_id, field=wanted,
                reason="MISSING_GOLDEN",
                fallback=f"do not act on {wanted}: no golden published",
            ).model_dump(mode="json")
        value = golden["attributes"].get(wanted)
        if value is None:
            return DownstreamAnswer(
                answer="no_trusted_value", agent=agent,
                entity_type=entity_type, entity_id=entity_id, field=wanted,
                reason="FIELD_WITHHELD",
                fallback=f"do not act on {wanted}: escalated or denied, no trusted value",
            ).model_dump(mode="json")
        return DownstreamAnswer(
            answer="golden", agent=agent,
            entity_type=entity_type, entity_id=entity_id, field=wanted,
            value=value.get("value") if isinstance(value, dict) else str(value),
            decision_id=golden.get("decision_id"),
            policy_version=golden.get("policy_version"),
            taxonomy_version=golden.get("taxonomy_version"),
            lineage=value.get("lineage") if isinstance(value, dict) else None,
        ).model_dump(mode="json")

    @app.post("/steward/{steward_id}/accept")
    async def steward_accept(steward_id: str, request: Request) -> dict[str, Any]:
        """Accept a suggested taxonomy action (steward-only, single-use).

        Bound to one exact suggested action and one taxonomy version; creates
        the new taxonomy version and triggers an explicit re-derive. AI may
        propose but never accepts its own suggestion.
        """
        from schemas import StewardAcceptRequest

        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid JSON body") from exc
        try:
            acceptance = StewardAcceptRequest.model_validate(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await _steward_accept_taxonomy(steward_id, acceptance, request)

    app.mount(
        "/",
        StaticFiles(directory=str(app_settings.static_dir), html=True),
        name="static",
    )
    return app


def _check_entity(entity_type: str) -> None:
    if not ENTITY_TYPE_RE.fullmatch(entity_type):
        raise HTTPException(status_code=422, detail="invalid entity_type")


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
        # Union-find over Splink pairs (exact blocking matches) plus the
        # exact-identifier fallback (Muse + fuzzy claim when Splink is down).
        parent: dict[str, str] = {doc["document_id"]: doc["document_id"] for doc in docs}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: str, right: str) -> None:
            parent[find(left)] = find(right)

        for pair in splink_out.get("pairs", []):
            left, right = pair.get("left"), pair.get("right")
            if left and right:
                union(left, right)
        # Fallback: exact matches on blocking identifiers only (policy
        # splink.blocking). Bare address/name similarity never merges —
        # address alone is not publishable proof (CSD link levels).
        blocking = list(getattr(policy.splink, "blocking", None) or ["email", "phone", "order_id"])
        by_value: dict[tuple[str, str], list[str]] = {}
        for document in docs:
            fields = document.get("fields", {})
            for attribute in blocking:
                field = fields.get(attribute)
                value = field.get("value") if isinstance(field, dict) else field
                if value:
                    by_value.setdefault((attribute, str(value)), []).append(document["document_id"])
        for _key, members in by_value.items():
            for first, second in zip(sorted(members), sorted(members)[1:]):
                union(first, second)
        groups: dict[str, list[dict[str, Any]]] = {}
        for document in docs:
            groups.setdefault(find(document["document_id"]), []).append(document)
        ordered = sorted(groups.values(), key=lambda members: sorted(d["document_id"] for d in members)[0])
        for index, members in enumerate(ordered):
            clusters.append({
                "cluster_id": f"cluster-{index + 1}",
                "entity_type": entity_type,
                "member_docs": members,
            })
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
        left_facts = sorted(
            f.fact_id for f in facts if fact_documents.get(f.fact_id) == left
        )
        right_facts = sorted(
            f.fact_id for f in facts if fact_documents.get(f.fact_id) == right
        )
        if not left_facts or not right_facts:
            continue
        proof = _shared_exact_proof(facts, fact_documents, left, right)
        if proof is None:
            continue
        claims.append({
            "from_fact_id": left_facts[0],
            "to_fact_id": right_facts[0],
            "proof": proof,
            "proposer": "Splink",
        })
    return claims


def _shared_exact_proof(    facts: list, fact_documents: dict[str, str], left_doc: str, right_doc: str
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
    for attribute, proof in (("email", "exact_email"), ("phone", "exact_phone"),
                             ("order_id", "exact_order")):
        if left_values.get(attribute) and left_values[attribute] == right_values.get(attribute):
            return proof
    return None


def _splink_status(settings: Settings) -> str:
    """Legacy import-probe status (kept for reference; /health uses the
    live service probe below and never assumes)."""
    if not settings.splink_enabled:
        return "unavailable"
    try:
        import splink  # noqa: F401

        return "ok"
    except ImportError:
        return "unavailable"


async def _matching_engine_health(
    settings: Settings,
) -> tuple[str, dict[str, str | None]]:
    """Probe the standalone matching-engine: (status, versions).

    Never raises and never blocks the API: unreachable/slow/invalid means
    "unavailable" with no versions. A recorded match failure (S-05) keeps
    reporting unavailable until a later /match succeeds, even if a probe
    succeeds in the meantime — the fallback is never hidden. The UI renders
    this as "Matching Engine: DOWN — fallback to Muse + fuzzy".
    """
    if not settings.splink_enabled:
        return "unavailable", {"splink_version": None, "duckdb_version": None}
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(settings.matching_health_url())
        if response.status_code != 200:
            return "unavailable", {"splink_version": None, "duckdb_version": None}
        payload = response.json()
    except Exception:
        return "unavailable", {"splink_version": None, "duckdb_version": None}
    if not isinstance(payload, dict) or payload.get("splink_service") != "ok":
        return "unavailable", {"splink_version": None, "duckdb_version": None}
    if payload.get("splink_enabled") is False:
        return "unavailable", {"splink_version": None, "duckdb_version": None}
    versions = {
        key: (payload.get(key) if isinstance(payload.get(key), str) else None)
        for key in ("splink_version", "duckdb_version")
    }
    from splink_adapter import EXPECTED_DUCKDB_VERSION, EXPECTED_SPLINK_VERSION

    if (versions["splink_version"], versions["duckdb_version"]) != (
        EXPECTED_SPLINK_VERSION, EXPECTED_DUCKDB_VERSION,
    ):
        return "unavailable", versions
    if _SPLINK_LAST_ERROR is not None:
        return "unavailable", versions
    return "ok", versions


def _load_expected_structures() -> dict[str, dict[str, Any]]:
    """Baked-in default expected structures per entity type (CSD 12.6)."""
    from pathlib import Path

    structures: dict[str, dict[str, Any]] = {}
    candidates = [
        Path(__file__).resolve().parent / "templates",
        Path(__file__).resolve().parent.parent / "data" / "mock" / "templates",
    ]
    base = next((path for path in candidates if path.is_dir()), None)
    if base is None:
        return structures
    import json as _json

    for name in ("user", "product"):
        path = base / f"expected_{name}_golden.json"
        if path.is_file():
            structures[name] = _json.loads(path.read_text(encoding="utf-8"))
    return structures


async def _steward_accept_taxonomy(steward_id: str, acceptance, request: Request) -> dict[str, Any]:
    """Accept a suggested taxonomy term (steward-only, single-use).

    Bound to one exact suggested action and one taxonomy version. Creates the
    new taxonomy version file, then runs an explicit re-derive under it.
    AI may propose but never accepts its own suggestion; a missing or
    mismatched binding fails closed.
    """
    import json as _json

    current: Settings = request.app.state.settings
    item = ledger.fetch_steward(current.database_path, steward_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"steward item {steward_id} not found")
    if item["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"steward item {steward_id} is {item['status']}, not pending",
        )
    if not item.get("suggested_action") or item["suggested_action"] != acceptance.action:
        raise HTTPException(
            status_code=422,
            detail="acceptance is not bound to this item's exact suggested action",
        )
    from taxonomies import default_version, load_taxonomies

    taxonomies = getattr(request.app.state, "taxonomies", None) or load_taxonomies()
    base = taxonomies.get(default_version(taxonomies, item["entity_type"]))
    if base is None:
        raise HTTPException(status_code=422, detail="no taxonomy to extend")
    try:
        new_version = _write_taxonomy_version(
            taxonomies, base, acceptance.action, acceptance.taxonomy_version
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=f"cannot create taxonomy version: {exc}") from exc
    # Refresh the in-memory store so the re-derive sees the new version.
    request.app.state.taxonomies = load_taxonomies()

    # Explicit re-derive under the new taxonomy version (same facts, new terms).
    from ingest import to_candidate_facts
    from publisher import decision_id_for, utcnow_iso

    stored_facts = ledger.fetch_candidate_facts(
        current.database_path, item["entity_type"], item["entity_id"]
    )
    fragments = []
    for row in stored_facts:
        from schemas import Fragment as _Fragment

        fragments.append(_Fragment(
            source=row["source"], source_id=row["source_id"], attribute=row["attribute"],
            value=row["value"], authority_rank=row["authority_rank"],
            event_time=row["event_time"], verified=bool(row["verified"]),
            ttl_seconds=row["ttl_seconds"],
        ))
    from schemas import ResolveRequest as _ResolveRequest

    entity_type, entity_id = item["entity_type"], item["entity_id"]
    re_request = _ResolveRequest(
        work_id=f"steward-{steward_id}-accept",
        entity_type=entity_type, entity_id=entity_id,
        idempotency_key=f"{item['decision_id']}-accept-{steward_id}",
        fragments=fragments,
    )
    from publisher import resolve_flow as _resolve_flow

    try:
        response, _proposal = await _resolve_flow(
            re_request, current, request.app.state.policies,
            request.app.state.taxonomies,
            taxonomy_version=new_version,
            expected_structure=request.app.state.expected_structures.get(entity_type),
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"re-derive failed: {exc}") from exc
    ledger.update_steward_status(current.database_path, steward_id, "accepted")
    payload = response.model_dump(mode="json")
    payload["taxonomy_version"] = new_version
    payload["accepted_action"] = acceptance.action
    return payload


def _write_taxonomy_version(
    taxonomies: dict, base, suggested_action: str, new_version: str
) -> str:
    """Create a new taxonomy version file from one accepted suggestion.

    Supported: 'Create as new term "X"' (adds the term, empty synonyms) and
    'Map "X" to "Y"' (adds X as a synonym of existing term Y). Anything else
    fails closed. The new version id must be fresh.
    """
    import re as _re

    from schemas import Taxonomy as _Taxonomy

    if not new_version or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", new_version):
        raise ValueError(f"invalid taxonomy version {new_version!r}")
    if new_version in taxonomies:
        raise ValueError(f"taxonomy version {new_version!r} already exists")
    create_match = _re.fullmatch(r'Create as new term "(.*)"', suggested_action)
    map_match = _re.fullmatch(r'Map "(.*)" to "(.*)"', suggested_action)
    terms = [{"term": term.term, "synonyms": list(term.synonyms)} for term in base.terms]
    if create_match:
        raw = create_match.group(1).strip()
        if not raw:
            raise ValueError("empty term")
        if any(term["term"].lower() == raw.lower() for term in terms):
            raise ValueError(f"term {raw!r} already exists")
        terms.append({"term": raw, "synonyms": []})
    elif map_match:
        raw, target = map_match.group(1).strip(), map_match.group(2).strip()
        hit = next((term for term in terms if term["term"] == target), None)
        if hit is None:
            raise ValueError(f"target term {target!r} does not exist")
        if raw not in hit["synonyms"]:
            hit["synonyms"] = sorted(set(hit["synonyms"]) | {raw})
    else:
        raise ValueError(f"unsupported suggested action {suggested_action!r}")
    payload = {
        "synthetic": True,
        "taxonomy_version": new_version,
        "entity_type": base.entity_type,
        "notice": f"Extended by steward accept from {base.taxonomy_version}.",
        "attribute_aliases": base.attribute_aliases,
        "terms": terms,
    }
    directory = _taxonomy_write_dir()
    path = directory / f"{new_version}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    validated = _Taxonomy.model_validate(payload)
    _ = validated
    return new_version


def _taxonomy_write_dir() -> Path:
    """Writable taxonomy directory the engine also watches (hot-reload).

    Uses taxonomies_dir() (TAXONOMY_DIR-aware). If it is read-only, fail
    closed — never write somewhere the engine cannot see.
    """
    from taxonomies import taxonomies_dir

    directory = taxonomies_dir()
    probe = directory / ".write-test"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise OSError(
            f"taxonomy directory {directory} is not writable; "
            "cannot accept new terms here"
        ) from exc
    return directory


def _row_to_fact(row: dict[str, Any]) -> dict[str, Any]:
    return {
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


async def _steward_derive(
    request: Request,
    item: dict[str, Any],
    value: str,
    new_status: str,
    *,
    source: str,
    source_id: str,
) -> dict[str, Any]:
    """A steward decision is a new fact, triggering re-derivation."""
    from datetime import datetime, timezone

    from ingest import normalize_value, to_candidate_facts
    from schemas import CandidateFact, Fragment, ResolveRequest

    current: Settings = request.app.state.settings
    entity_type, entity_id = item["entity_type"], item["entity_id"]
    stored_facts = ledger.fetch_candidate_facts(
        current.database_path, entity_type, entity_id
    )
    fragments = [
        Fragment(
            source=row["source"],
            source_id=row["source_id"],
            attribute=row["attribute"],
            value=row["value"],
            authority_rank=row["authority_rank"],
            event_time=row["event_time"],
            verified=bool(row["verified"]),
            ttl_seconds=row["ttl_seconds"],
        )
        for row in stored_facts
    ]
    fragments.append(
        Fragment(
            source=source,
            source_id=source_id,
            attribute=item["attribute"],
            value=normalize_value(item["attribute"], value),
            authority_rank=1,
            event_time=datetime.now(timezone.utc).isoformat(),
            verified=True,
            ttl_seconds=86400,
        )
    )
    steward_request = ResolveRequest(
        work_id=f"steward-{item['steward_id']}",
        entity_type=entity_type,
        entity_id=entity_id,
        idempotency_key=f"{item['decision_id']}-{new_status}-{item['steward_id']}",
        fragments=fragments,
    )
    try:
        # Flat re-derive like /resolve (no expected structure): the approve
        # path republishes what the engine derives; whole-document validation
        # lives on /build, which supplies an explicit structure.
        response, proposal = await resolve_flow(
            steward_request, current, request.app.state.policies,
            getattr(request.app.state, "taxonomies", None),
        )
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EngineUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "verdict": "escalated",
                "decision_id": exc.decision_id,
                "reason_code": exc.reason_code,
                "message": str(exc),
                "fail_closed": True,
            },
        ) from exc
    ledger.update_steward_status(
        current.database_path, item["steward_id"], new_status
    )
    payload = response.model_dump(mode="json")
    payload["proposal"] = proposal.model_dump(mode="json")
    payload["steward_item"] = item["steward_id"]
    return payload


def _do_split(
    steward_id: str, request: Request, body: dict[str, Any]
) -> dict[str, Any]:
    import json as jsonlib

    from publisher import persist_decision, utcnow_iso
    from schemas import CandidateFact

    try:
        split_request = StewardSplitRequest.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    current: Settings = request.app.state.settings
    item = ledger.fetch_steward(current.database_path, steward_id)
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"steward item {steward_id} not found"
        )
    if item["status"] not in ("pending", "approved"):
        raise HTTPException(
            status_code=409,
            detail=f"steward item {steward_id} is {item['status']}; "
            "split requires a pending or approved item",
        )
    entity_type, entity_id = item["entity_type"], item["entity_id"]
    target = ledger.fetch_latest_golden(current.database_path, entity_type, entity_id)
    versions: list[dict[str, Any]] = []
    connection = ledger.connect(current.database_path)
    try:
        rows = connection.execute(
            """
            SELECT * FROM golden_records
            WHERE entity_type = ? AND entity_id = ? ORDER BY version
            """,
            (entity_type, entity_id),
        ).fetchall()
        versions = [dict(row) for row in rows]
    finally:
        connection.close()
    wanted = [
        row for row in versions if row["version"] == split_request.restore_version
    ]
    if not wanted:
        raise HTTPException(
            status_code=404,
            detail=f"version {split_request.restore_version} not found for "
            f"{entity_type}/{entity_id}",
        )
    restored_attrs = wanted[0]["attributes"]
    if isinstance(restored_attrs, str):
        restored_attrs = jsonlib.loads(restored_attrs)
    created_at = utcnow_iso()
    decision_seq = f"split-{steward_id}-v{split_request.restore_version}"
    from publisher import decision_id_for

    decision_id = decision_id_for(decision_seq, decision_seq)
    policy = active_version(request.app.state.policies, entity_type)
    taxonomy_version = (target or {}).get("taxonomy_version")
    if not taxonomy_version:
        try:
            from taxonomies import default_version, load_taxonomies

            taxonomies = getattr(request.app.state, "taxonomies", None) or load_taxonomies()
            taxonomy_version = default_version(taxonomies, entity_type)
        except Exception:
            taxonomy_version = ""
    restored: dict[str, Any] = {}
    for attribute, value in restored_attrs.items():
        restored[attribute] = {
            "value": value["value"],
            "winner_fact_id": f"split-{steward_id}",
            "loser_fact_ids": [],
            "rule_fired": "SPLIT",
            "policy_version": policy.policy_version,
            "taxonomy_version": taxonomy_version,
            "lineage": (
                f"split restore v{split_request.restore_version} "
                f"via {steward_id}"
            ),
        }
    latest_attrs = target["attributes"] if target else {}
    if isinstance(latest_attrs, str):
        latest_attrs = jsonlib.loads(latest_attrs)
    withdrawn = [attr for attr in latest_attrs if attr not in restored]

    from schemas import GoldenFact

    golden = [
        GoldenFact(
            entity_type=entity_type,
            entity_id=entity_id,
            attribute=attribute,
            value=value["value"],
            winner_fact_id=value["winner_fact_id"],
            loser_fact_ids=[],
            rule_fired="SPLIT",
            policy_version=policy.policy_version,
            taxonomy_version=taxonomy_version or None,
            lineage=value["lineage"],
        )
        for attribute, value in restored.items()
    ]
    connection = ledger.connect(current.database_path)
    try:
        with connection:
            latest_version = target["version"] if target else 0
            golden_id = f"golden-{decision_id}"
            connection.execute(
                """
                INSERT INTO golden_records
                  (golden_id, entity_type, entity_id, version, attributes,
                   decision_id, policy_version, taxonomy_version,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    golden_id,
                    entity_type,
                    entity_id,
                    latest_version + 1,
                    jsonlib.dumps(restored, sort_keys=True, ensure_ascii=False),
                    decision_id,
                    policy.policy_version,
                    taxonomy_version or None,
                    created_at,
                    created_at,
                ),
            )
            for attribute, value in latest_attrs.items():
                prev_winner = (value or {}).get("winner_fact_id")
                if not prev_winner:
                    continue
                current_winner = (restored.get(attribute) or {}).get(
                    "winner_fact_id"
                )
                if current_winner != prev_winner or attribute in withdrawn:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO tombstones
                          (tombstone_id, loser_fact_id, survivor_golden_id,
                           decision_id, reason)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            f"tb-{decision_id}-{prev_winner}-split",
                            prev_winner,
                            golden_id,
                            decision_id,
                            "superseded-by-split",
                        ),
                    )
    finally:
        connection.close()
    ledger.update_steward_status(current.database_path, steward_id, "split")
    return {
        "decision_id": decision_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "verdict": "published",
        "golden_version": (target["version"] if target else 0) + 1,
        "restored_version": split_request.restore_version,
        "steward_id": steward_id,
        "policy_version": policy.policy_version,
        "taxonomy_version": taxonomy_version,
    }


async def _trust_engine_health(url: str) -> dict[str, Any] | None:
    """Return the trust-engine health payload, or None when unavailable."""
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    return payload


app = create_app()

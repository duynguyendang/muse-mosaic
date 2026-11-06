# Muse Mosaic — Architecture Design Document (ADD)

Status: living document, mirrors the code on `main`. It describes how the
system is actually built, container by container, and *why* — each significant
choice is recorded as a decision with its alternatives and consequences (§7).
If this document and the code disagree, the code wins and this document is
stale: fix it, do not work around it.

Conventions: the three service names are fixed — `mosaic-api`,
`trust-engine`, `matching-engine` (do not invent others). All demo data is
synthetic and `-like` suffixed unless stated otherwise. No accuracy, scale, or
production-readiness claim is made anywhere in this document.

---

## 1. Problem and solution shape

Fragmented source systems (CRM-like, SAP-like, CIAM-like, tickets, orders)
each hold part of the truth about a person or product, and they disagree.
Downstream agents (OTP, refund, search) need exactly one of two answers:

1. a **golden** — one versioned record per entity where every field carries
   its winning source, the rule that fired, the policy and taxonomy versions,
   and full lineage; or
2. **`No trusted value`** — the request escalates to a human steward (owner,
   deadline, fallback) and downstream must stop, never guess.

The system therefore separates *proposing* (cheap, parallel, allowed to be
wrong) from *deriving* (deterministic, versioned, auditable). Three services
exist because there are three independent failure domains, not three code
folders: proposing must stay up when deriving is down, and matching must be
replaceable without touching the gate.

---

## 2. Constraints that shaped the architecture

- **Trust boundary (see the guardrails list in the README).** No proposer
  publishes; only the deterministic Datalog engine derives; the publisher
  writes only after whole-document validation. Every layer enforces this
  independently — removing any single check must still leave the system
  fail-closed.
- **Demo on a laptop, then on one judging host.** Docker Compose with three
  containers, all state in one SQLite file, no external services at runtime
  (Meta API only in real-proposer mode, never in tests).
- **Determinism and replay.** Same input + same policy version must always
  yield the same decision id and the same golden. Uploads, retries, and
  restarts must be safe by construction (idempotency keys, single
  transaction, append-only ledger).
- **No invented data, anywhere.** Confidence numbers are Muse claims, never
  facts. EM match weights are candidate signals, never verdicts. Test data
  is synthetic and labelled as such.
- **UX is a separate static frontend** (`web/`, no build step) served at `/`
  in dev and deployable to any static host via `window.MOSAIC_API_BASE`.
  It holds no secrets and calls no engine directly — only `mosaic-api`.

---

## 3. System context (C4 level 1)

```
                       ┌────────────────────┐
                       │  Demo presenter /  │  drives the UI, answers the
                       │  judge (browser)   │  steward queue in the demo
                       └─────────┬──────────┘
                                 │ HTTPS (dev :8000)
                    ┌────────────▼────────────┐
                    │       Muse Mosaic       │◄──── Meta Model API
                    │  (3 containers + UI)    │  (real-proposer mode only;
                    └────────────┬────────────┘   never in tests)
                                 │ two answers only:
                    ┌────────────▼────────────┐
                    │ Downstream agents       │
                    │ (OTP / refund / search) │  act on goldens,
                    │                         │  stop on No trusted value
                    └─────────────────────────┘
```

Out of scope by design: real CRM/ERP/CIAM connectors, user identity and
access control, multi-tenancy, analytics dashboards, mobile/responsive UI.

---

## 4. Containers (C4 level 2)

| Container | Runtime | Responsibility (one job) | Must never |
|---|---|---|---|
| `web/` (static files, served by `mosaic-api` at `/` in dev; separately deployable) | Any static host | Display only: the studio cockpit (`index.html` + `studio.js` + prebuilt `studio.css`); read live ledger numbers, never derive them in the browser | Call an engine directly, hold secrets, decide a publish |
| `mosaic-api` (Python/FastAPI + uvicorn, `:8000` published) | `mosaic-api/` image | Orchestrate: ingest → match → graph → derive → structure → answer → steward loop; merge proposer outputs; enforce the publisher gate | Run Splink matching, run Datalog derivation, publish without a decision id, write a golden bypassing the ledger |
| `matching-engine` (Python/FastAPI + DuckDB, `:8081` internal only) | `matching-engine/` image | Statistical matching only: blocking, comparisons, EM weights/probabilities, connected-component clusters via `POST /match` | Publish, choose a winner, decide authority, create taxonomy terms, write anywhere |
| `trust-engine` (Go + pinned manglekit binary, `:8080` internal only) | `trust-engine/` image | Derivation only: authority, verified, event/time/TTL, link proof, cluster rules, taxonomy check via `POST /derive` | Assemble JSON, validate documents, write the ledger, serve a public port |

Shared infrastructure: one SQLite file (`DATABASE_PATH`, default
`/app/state/mosaic.db` on the `mosaic-state` volume) owned exclusively by
`mosaic-api`; one shared taxonomy volume (`mosaic-taxonomy`) that
steward-Accept writes and `trust-engine` hot-reloads without restart.

Traffic rules: browsers and downstream clients talk only to `mosaic-api`.
`mosaic-api` calls `trust-engine` (`TRUST_ENGINE_URL`) and
`matching-engine` (`MATCHING_ENGINE_URL`). The engines never call each other
and accept no inbound traffic except from `mosaic-api` on the Compose
network. Health is the only cross-service introspection:
`GET /health` on each service; `mosaic-api` aggregates all three plus
policy versions and Splink/embedding status.

---

## 5. Components (C4 level 3, per container)

### 5.1 `mosaic-api`

| Module | Owns |
|---|---|
| `ingest.py` | Fragment/document intake, JSON shape validation, type detection, blocking-key flattening |
| `proposer.py` + `subagents.py` (Scout, Linker, Cluster, Taxonomist, Critic) | Muse boundary: strict `json_schema` calls, claims only, abstain-never-guess; mock mode deterministic for tests |
| `meta_client.py`, `meta_tooling.py` | Bounded retry (429/500/503, `Retry-After`, ≤3), 30 s circuit breaker; conditional Wonder (pinned: Copilot mapping + escalation adjudication, else labelled stub), Prompt Guard labelled heuristic otherwise |
| `splink_adapter.py` (+ in-process fallback `splink_local.py`) | Matching-engine HTTP client; `SPLINK_UNAVAILABLE` fallback to Muse + fuzzy; never publishes on fallback alone |
| `graph.py` | Source + Derived graph, Pathfinder, blast-radius, orphan/stale/duplicate analysers, cluster Entity nodes + `cluster_member` edges |
| `trust.py`, `policies.py` | Fail-closed derive client (20 s timeout, strict re-validation); read-only versioned policy mirror + allow-list |
| `publisher.py`, `ledger.py`, `schema.sql` | Gated writes, whole-document validation, nested `purchased_products` assembly, idempotency, tombstones, steward queue, `agent_traces` |
| `similar_search.py` | Structured similar search; vector recall explicitly skipped until an embedding is pinned |
| `main.py`, `config.py` | Route wiring; env-only configuration; fail-fast startup (no key/model/HTTPS base in real mode = refuse to boot) |

### 5.2 `trust-engine`

| File | Owns |
|---|---|
| `main.go` | HTTP surface (`GET /health`, `POST /derive`), fail-closed envelopes on every error path |
| `adapter.go` | Policy + taxonomy loading and validation, SIGHUP policy reload (validate-then-swap, failed reload keeps old policies), request contract validation |
| `derive.go` | Time canonicalization (whole minutes since 2026-01-01+07:00), three `mkit eval` queries, golden/conflict/steward/verified-link mapping |
| `policies/*.{json,mg}` | Authority matrices, critical attributes, rule set R1–R4 + L1/C1/C2/T1/I1 — versioned, the only place ranks live |

### 5.3 `matching-engine`

| File | Owns |
|---|---|
| `main.py` | `GET /health` (service + `splink_version`/`duckdb_version`/`splink_enabled`), `POST /match` contract validation (unknown blocking/comparison/policy → 422; disabled → 503) |
| `splink_engine.py` | Deterministic exact-identifier blocking + connected components; EM weights best-effort; per-pair `blocking_rule` evidence and `meets_threshold` flag |

---

## 6. Runtime: the canonical flows

### 6.1 `/build` — the full pipeline, per cluster

```
upload/dataset → ingest (validate) → MATCH, in parallel as proposers:
    ├─ matching-engine POST /match  -> candidate clusters + per-pair evidence
    └─ Muse sub-agents              -> links, values, taxonomy claims (or abstain)
  → Data Graph: source nodes kept, proposed vs verified links, cluster nodes
  → trust-engine POST /derive (whole cluster, one call)
  → publisher: downgrade check (proposer gate, fail-closed direction only)
  → assemble nested golden + validate whole document against expected schema
  → persist in ONE transaction (facts, decision, golden, tombstones,
     conflicts, steward rows, graph, traces)  →  golden or escalate/deny
```

`/resolve` is the same pipeline for a single entity without the Splink step
(declared, not an omission: single-entity resolve needs no blocking/matching).
`/upload` only validates and proposes links — it never publishes, never
derives, never writes the ledger.

### 6.2 Steward loop (the only human path)

`approve` / `reject` / `split` / `Accept` never edit in place. Approve
records a new verified rank-1 fact and re-derives; `split` restores a prior
golden version as a new version with tombstones kept; `Accept` writes a new
taxonomy version file (single-use, bound to one exact suggested action) and
re-derives under it — the new term only becomes real after a successful
re-derive, observed live on the Kaggle run (§9).

### 6.3 Downstream contract

`GET /downstream/{otp|refund|search}` returns either a golden
(with `decision_id`, lineage, versions) or `{"answer": "no_trusted_value"}`
with a reason and a do-not-act fallback. There is no third response shape,
no fragment fallback, and no score fallback.

---

## 7. Architecture decisions (ADRs)

**D1 — Three containers, not one process and not N microservices.**
Proposing, deriving, and matching fail independently and version
independently (Splink `5.0.0`/DuckDB `2.0.0` vs manglekit pinned by commit
vs Muse model id), so each gets a container and a health endpoint.
*Rejected:* one process (a compromised proposer could bypass the gate;
kill-service demo impossible) and per-module services (a golden, its graph,
tombstones and traces must commit atomically — splitting them across HTTP
would trade a transaction for a distributed saga).

**D2 — Matching is a separate service with an in-process twin.**
`matching-engine` owns the only Splink+DuckDB dependency in the system;
`mosaic-api` never imports Splink (verified: no `import splink` outside the
two matcher modules). The in-process fallback
(`mosaic-api/splink_local.py`) is the *same logic moved verbatim*, selected
by empty `MATCHING_ENGINE_URL`, and a Liskov parity test pins identical
clustering on the fixture set — a fallback that behaves differently is a
second implementation, not a fallback.

**D3 — Fail-closed health with sticky failure.**
`GET /health` reports `splink: unavailable` on disabled/unreachable/invalid/
version-mismatched responses, *and* remembers the last failed match until a
later match succeeds — a passing probe must not hide a failed run (this is
what makes S-05 observable from outside). Fail-closed always resolves toward
escalation, never toward silent success.

**D4 — Version pinning is a contract, not a comment.**
manglekit (Go source + Dockerfile + test fixture, cross-checked by test),
Splink/DuckDB (service `/health` vs client-side expected constants — mismatch
fails the run, it does not warn and continue), Muse model id (refused at
startup in real mode, never defaulted).

**D5 — One transaction per publish, append-only history.**
Golden versions increment under a `UNIQUE(entity_type, entity_id, version)`
optimistic lock; losers become tombstones; steward decisions become new rows;
nothing is ever updated in place (a failed engine-down attempt writes no
decision row, so its reserved id stays free for retry). Idempotency is
same-key + same-payload → replay, same key + different payload → 409.

**D6 — Nested golden with verified-only edges.**
`purchased_products` assembles only from `verified_link` hops (exact
identifier + shared order), each item carrying the product golden's version
and the deriving decision id. The graph stores the same edges
(`cluster_member` for clusters, `purchased` for verified links) so the
canvas, Explain, and the golden all tell one story.

**D7 — Sticky-fallback over silent-null.**
Unavailable engines, malformed responses, and version mismatches all funnel
into one labelled state (`SPLINK_UNAVAILABLE` /
`TRUST_ENGINE_UNAVAILABLE`), counted in tests, shown in the UI, never hidden
behind a generic error or an empty success.

**D8 — Web UI is replaceable.**
The frontend is static files + `fetch`; the API contract is the whole
integration surface. Any UI (including the Stitch-generated mockups in
`.stitch/`) can be swapped without touching a service.

---

## 8. Data design (summary)

SQLite, one file, `sqlite3` stdlib only. Tables: `golden_records`
(append-only, versioned), `candidate_facts` (+ unused-until-pinned vector
columns), `decisions` (unique idempotency key), `steward_queue`,
`tombstones`, `conflicts`, `agent_traces`, `graph_nodes`, `graph_edges`
(edge types: `has_attribute`, `proposed_link`, `verified_link`,
`cluster_member`, `purchased`). Foreign keys are *not* enforced in DDL;
integrity comes from the single-writer transaction in `persist_decision`
(verified by idempotency + replay tests, not by constraints).

Event times are timezone-aware ISO-8601, canonicalized to whole minutes
since 2026-01-01T00:00+07:00; TTLs are whole minutes; times outside the
mangle number bound (±1e6) are rejected, not wrapped. A real Kaggle import
hit exactly this bound (2013–2018 dates) and now documents the batch-timestamp
adaptation (local-only `docs/internal/Kaggle-test-dataset.md`, finding F1).

---

## 9. Interface contracts (summary)

| Call | In | Out | Failure semantics |
|---|---|---|---|
| `POST /match` | flattened fragments, `policy_version`, `blocking`, `threshold` | clusters + per-pair `match_weight/match_probability/blocking_rule`, `meets_threshold` | unavailable set, never partial clusters presented as complete |
| `POST /derive` | candidate facts, policy + taxonomy versions, links | golden facts, conflicts, steward items, verified links | 4xx contract/version/link errors, 5xx engine errors — always `fail_closed: true`, never partial goldens |
| `POST /build`, `POST /resolve` | dataset / fragments (+ idempotency key) | golden(s) or escalate/deny + steward queue + decision id | 422 bad input, 409 key reuse, 503 engine/proposer down (no partial writes) |
| `GET /health` (mosaic-api) | — | api/engine/splink status, versions, active policy versions | never 500s on downstream failure; reports `down`/`unavailable` instead |

---

## 10. Test strategy (what proves what)

- **Unit/contract** (`test_contract.py`): Pydantic boundaries (provenance-less
  and `embedding`-bearing facts rejected), DDL gate columns, `mg ↔ JSON`
  coherence, fail-closed API with the engine down.
- **Rules** (`test_rules_user.py`, `test_rules_product.py`): G-01..G-20,
  P-01..P-06 + T1 through a live trust-engine (`mkit eval`, three queries
  per derive).
- **Hidden connection** (`test_hidden.py`): H-01 chain publishes with verified
  links, H-02 broken chain escalates explicitly (C2), H-03 trap not merged
  (C1 on the exact-tie).
- **Matching** (`test_splink.py`, `test_matching_engine.py`): S-01..S-05
  against the live service or a stub — found chain, split trap, Muse-only
  link, down-service fallback, malformed-response fail-closed — plus
  version-mismatch and service-vs-fallback Liskov parity.
- **Whole-system** (`test_build.py`, `test_flows.py`, `test_kill_service.py`,
  `test_idempotency.py`, `test_split.py`, `test_structure.py`,
  `test_taxonomy*.py`): end-to-end build, Flow A/B, engine-down retry with
  the same decision id, same-key replay vs same-key-different-hash 409,
  split restore, structure gate, steward Accept.
- **Real-dirt complement** (`scripts/kaggle_e2e.py`, manual run, not CI):
  frozen 200-row Kaggle Customer 360 sample through all three live services —
  measured outcomes in the local-only `docs/internal/Kaggle-test-dataset.md`. Not a
  benchmark (no ground-truth labels); complements, never replaces, the suite.

---

## 11. Deployment

Compose topology: `mosaic-api` (`:8000` published) + `trust-engine`
(internal `:8080`, no published port) + `matching-engine` (internal
`:8081`, no published port). Named volumes: `mosaic-state` (ledger),
`mosaic-taxonomy` (shared, hot-reloaded by trust-engine, writable by
mosaic-api for Accept). Env-only config (`USE_MOCK_MUSE`,
`META_MODEL_API_KEY`, `MUSE_MODEL_ID`, `WONDER_MODEL_ID`,
`SPLINK_ENABLED`, `TRUST_ENGINE_URL`, `MATCHING_ENGINE_URL`,
`DATABASE_PATH`, `TAXONOMY_DIR`, `POLICY_DIR`,
`EMBEDDING_MODEL/VERSION/DIM`); images pin
`fastapi/pydantic/uvicorn/httpx`, `splink==5.0.0`, `duckdb==2.0.0`,
`go 1.25`, and the manglekit commit. `docker compose config` validates the
tree; the Go service additionally honours SIGHUP for validated
policy reloads (failed reload keeps serving the old policy).

---

## 12. Open items and non-goals

- Demo video recording and submission checklist.
- Single-container manglekit-subprocess fallback (only the Splink half of
  the fallback topology is implemented).
- Embedding model/version/dimension unpinned → vector recall stays skipped;
  `display_name` authority matrix assigned in `pol-2026-10-28-v1` (agreeing names publish R1; v1 still escalates, pinned by tests).
- What this document is *not*: a benchmark report, a production hardening
  guide, or a claim about real customer data — all demo inputs are synthetic
  fixtures labelled `-like`, and the Kaggle run is explicitly a scale-and-dirt
  complement, not a benchmark.

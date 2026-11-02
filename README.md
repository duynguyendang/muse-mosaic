# Muse Mosaic

> Many fragments, one trusted picture, with proof for every piece.

Muse Mosaic is a **trusted source for agents that act on fragmented data**.
The problem it solves is not matching — it is the decision *after* matching:
given conflicting fragments about one person or product, may a downstream
agent (OTP, refund, search) act on a merged value right now, on what proof,
and if not, why is the proof missing? Every answer the system gives has
exactly one of two forms, and no third form exists:

1. a **golden** — one versioned record per entity, where every field carries
   its winner, its rule, its policy version, its taxonomy version and its
   full lineage; or
2. **`No trusted value`** — the request escalates to a human steward with an
   owner, a deadline and a fallback, and downstream is told to stop.

No proposer ever publishes. Only a deterministic Datalog engine
(`trust-engine`, Go + manglekit) derives; only the publisher writes, and only
after the whole assembled document validates against the expected schema.
Splink proposes candidate clusters at scale, five Muse sub-agents propose
links, values and taxonomy mappings as claims, and a human steward decides
*only* on escalations — approving a config or a value is never approving a
golden. That separation is the whole product: matching at scale, proof per
field, and a visible fail-closed boundary everywhere a guess could leak in.

## Proven twice: synthetic suite + real dirty data

**1. Synthetic correctness gate — 200+ tests** (`python3 -m pytest -q`; plus `go test ./...` in `trust-engine/`, both run in CI):
G-01..G-21 user rules (G21: agreeing display_name publishes under v2) plus C1, P-01..P-06 product rules plus T1, H-01..H-03
hidden-connection chain/trap, S-01..S-05 matching-engine tests against the
*live* service (S-04 with the service down, S-05 with a malformed response),
plus kill-service, idempotency-key (same key + new payload → 409),
steward approve/reject/split/Accept, and whole-document structure gates.
Every test runs through the real derive path (`mkit eval`), never a mock.

**2. Real dirty data — Kaggle Customer 360, frozen 200-row sample**
(generated locally via `scripts/`, git-ignored test data — pull with
`kagglehub`, never committed; full write-up in the local-only
`docs/internal/Kaggle-test-dataset.md`, not published): 2,554 documents built from 200 CRM
rows + 818 kept orders + 389 products, with a staged authority matrix
(`crm/1, orders/1, catalog/1`) as a new policy version — the repo's SAP/CIAM
ranks were *not* reused blindly. Measured, not claimed:
170 Splink clusters from 7,781 candidate pairs; **163 goldens published**
(phone/email/address unanimous-verified by R1, 20 name conflicts escalated
via C1, 7 withheld for no email consensus); 7 goldens nest verified
`purchased_products` with L1 lineage after a steward Accept created a new
taxonomy term; downstream OTP acted on a golden phone and product search acted
on the accepted CLOTHING golden, while user search correctly stopped
(`FIELD_WITHHELD` — user goldens carry no top-level category, by design).
Findings include real engine
limits hit on the way in (event-time spread vs the mangle number bound) and
three real bugs this run caught and fixed, all with regression tests.

Working implementation of the Conceptual Solution Design v2.5 (2026-10-28).
The CSD lives in `docs/internal/` (local-only working documents, not published);
the committed design record is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
The v2.3/v1.1 CSDs are superseded and kept for history only.

---

## Status: v2.5 pipeline working (matching-engine split), fail-closed defaults intact

CSD §12.1 steps 1–8 are implemented and proven by tests (step 8 Splink service
done, checkpoint gate open; steps 9–11 remain). What the system does today:

| Area | State | Behaviour |
|------|-------|-----------|
| Repository scaffold, config, SQLite DDL + graph tables | Implemented | `mosaic-api/schema.sql` matches CSD 12.5 gate columns, plus `graph_nodes`, `graph_edges`, `taxonomy_version` columns, Splink candidate columns, `suggested_action`, `conflicts` + `agent_traces` audit tables (new tables only; no gate column touched) |
| Pydantic fact/golden/graph contracts | Implemented | Provenance-less facts and embedding fields rejected before derivation; links validated against a closed proof vocabulary |
| Versioned user + product policies | Implemented | JSON twins + **runnable** `.mg` Datalog (R1–R4 + L1/C1/C2/T1/I1), matrix coherence tested |
| Versioned taxonomies | Implemented | `data/mock/taxonomy/*.json`, engine-enforced membership (T1), steward-only Accept creates new versions |
| Trust-engine derive (Go + manglekit subprocess) | Implemented | Real `mkit eval` over flattened JSON facts; unknown/rank-mismatched inputs, unknown taxonomy and bad links rejected; engine errors fail closed |
| Matching engine (Splink, proposer only) | Implemented | `matching-engine/` FastAPI + DuckDB: deterministic exact-identifier blocking + connected components; EM weights best-effort as candidate signals only; `SPLINK_UNAVAILABLE` fallback never publishes alone |
| Five Muse sub-agents | Implemented | Scout/Linker/Cluster/Taxonomist/Critic, mock by default; real mode via strict JSON schemas, no invented outputs accepted |
| Conditional Meta tooling | Stubbed honestly | Wonder adjudicates escalations only if a model id is pinned, else a labelled `not_available` stub; Prompt Guard falls back to Pydantic + claim logging, labelled as such (never simulated as real) |
| Data Graph (SQLite) | Implemented | Source + Derived layers, proposed vs verified links, Pathfinder, blast radius, orphan/stale/duplicate analysers |
| Publisher + ledger + nested golden + structure gate | Implemented | Gated writes, whole-document validation, nested `purchased_products` assembly, idempotent replay, steward approve/reject/split/Accept |
| Mosaic UI | Implemented | Graph canvas, builder panel with sub-agent trace + Matching Engine status (version from /health, clusters/pairs/weights per build), engine panel with derivation tree, structured golden panel, upload, downstream two-answer demo, steward actions incl. Accept |
| 35 assertions across 34 cases (G + P + H + S) + service | **Passing** | G-01..G-21, P-01..P-06 + taxonomy T1, H-01..H-03 chain/trap, S-01..S-05 matching-engine against the live service (S-04 with the service down, S-05 malformed response fail-closed) — all through the real derive path, not a mock |
| Kill-service, idempotency, split/rollback, structure | **Passing** | Full-stack tests against a live trust-engine |
| Single-container subprocess fallback | **Partial: Splink in-process only** | `mosaic-api/splink_local.py` runs the verbatim matching logic in-process behind the same match interface when `MATCHING_ENGINE_URL` is empty (Liskov parity-tested). The Go service still shells out to the pinned mkit binary; a Python mkit-subprocess fallback for derive is not implemented, so the engine container is still required. |

**No measured result is claimed.** No real CRM/ERP/CIAM integration, no real
customer data and no accuracy number exists in this repository. All source
records are synthetic fixtures labelled `-like`. All "targets" in CSD Section 7
are acceptance criteria for the synthetic test set, verified by running it —
not production results.

---

## Repository layout

```text
muse-mosaic/
├── README.md
├── docker-compose.yml
├── .env.example
├── .gitignore
├── .dockerignore
├── docs/
│   └── internal/             # unpublished: CSD v2.5 (build source) + v2.3/v1.1 history,
│                             # UX 2.0/2.1, as-built context catalog, demo script
├── mosaic-api/
│   ├── Dockerfile            # bundles policies + mock data + taxonomy
│   ├── requirements.txt      # fastapi, pydantic, uvicorn, httpx, jsonschema (no Splink: separate service)
│   ├── requirements-dev.txt
│   ├── schema.sql            # CSD 12.5 DDL + graph/audit tables
│   ├── main.py               # /resolve /build /upload /graph /golden /explain
│   │                         #   /similar /downstream/* /steward/* /health
│   ├── config.py             # env-only configuration; no invented ids
│   ├── schemas.py            # fact/golden/graph/taxonomy/build/upload contracts
│   ├── ingest.py             # Documents, aliases, canonicalization, upload parsing
│   ├── taxonomies.py         # versioned taxonomy store + exact synonym mapping
│   ├── splink_adapter.py     # Splink HTTP client (proposer only) + SPLINK_UNAVAILABLE fallback
│   ├── subagents.py          # Scout/Linker/Cluster/Taxonomist/Critic (mock + real)
│   ├── meta_tooling.py       # Wonder (conditional) + Prompt Guard fallback
│   ├── meta_client.py        # strict json_schema caller, retry, breaker, usage
│   ├── graph.py              # Data Graph: build, pathfinder, impact, analysers
│   ├── proposer.py           # Muse boundary (delegates to sub-agent pipeline)
│   ├── trust.py              # trust-engine HTTP client, fail-closed
│   ├── policies.py           # read-only policy mirror (version + allow-list)
│   ├── publisher.py          # gated writes, structure gate, nested assembly
│   ├── ledger.py             # SQLite schema + exact reads/writes
│   ├── similar_search.py     # structured similar search (vector deferred)
├── matching-engine/           # standalone matching engine (proposer only, internal)
│   ├── Dockerfile            # python:3.12-slim image, no published port
│   ├── requirements.txt      # fastapi, uvicorn, splink==5.0.0, duckdb==2.0.0, pydantic
│   ├── main.py               # GET /health (versions) + POST /match (candidates only)
│   └── README.md             # contract + local run
├── web/                      # separate frontend app (static, no build step)
│   ├── README.md             # deploy: static host + MOSAIC_API_BASE
│   ├── index.html            # loop strip, graph canvas, builder/engine, golden, steward drawer
│   └── app.js                # all interaction; API base via window.MOSAIC_API_BASE
├── trust-engine/
│   ├── Dockerfile            # builds pinned mkit + Go service + taxonomy
│   ├── go.mod
│   ├── main.go               # /health + /derive (200 derive, 4xx/500 fail-closed)
│   ├── adapter.go            # policy + taxonomy loading (hot-reload), validation
│   ├── derive.go             # canonicalize -> mkit eval x3 -> map output
│   ├── policies/
│   │   ├── user/pol-2026-10-27-v1.{json,mg}        # R1-R4 + L1/C1/C2/T1/I1
│   │   ├── user/pol-2026-10-28-v1.{json,mg}        # +display_name ranks (G21)
│   │   └── product/pol-product-2026-10-27-v1.{json,mg}
│   └── taxonomy -> ../../data/mock/taxonomy (copied into the image)
├── scripts/                    # Kaggle e2e harness (frozen sample + run script)
│   ├── kaggle_customer360_sample.py  # deterministic 200-row sampler -> data/kaggle/
│   └── kaggle_e2e.py               # composes scratch env, runs 3 services, records results
├── data/mock/
│   ├── users.json  products.json  tickets.json
│   ├── hidden/{f1,f2,f3,f4,f5,f6_trap}.json   # hidden-connection scene
│   ├── taxonomy/tax-{user,product}-2026-10-27-v1.json
│   └── templates/{user_profile,purchased_product,expected_*_golden}.json
├── data/kaggle/                # LOCAL-ONLY, git-ignored (regenerate via scripts/)
│   ├── customer360_sample_200.json  # 2,554 docs (generated, not committed)
│   └── policies/user|product/pol-kaggle-2026-10-28-v1.{json,mg}  # (generated) crm/orders/catalog ranks
└── tests/
    ├── conftest.py        # mkit build, trust-engine + API fixtures
    ├── test_contract.py   # contracts, DDL, mg<->json, fail-closed API
    ├── test_rules_user.py # G-01..G-21 (+C1 unit) through live mkit
    ├── test_rules_product.py # P-01..P-06 + T1 through live mkit
    ├── test_hidden.py     # H-01..H-03 chain/trap through live mkit
    ├── test_splink.py     # S-01..S-05 vs live/stub service (S-04 down, S-05 malformed) + mismatch + Liskov
    ├── test_matching_engine.py # service contract: pins, blocking/comparison/policy validation
    ├── test_subagents.py  # five agents: claims only, never publish
    ├── test_meta_tooling.py # conditional tools stubbed, never simulated
    ├── test_taxonomy.py   # exact synonyms only; fuzzy stays unmapped
    ├── test_taxonomy_accept.py # Accept creates version + re-derive publishes
    ├── test_structure.py  # whole-document gate: drop field / block golden
    ├── test_flows.py      # Flow A/B, product gate, reject, fragments, similar
    ├── test_build.py      # /build end-to-end (cluster, graph, upload, downstream)
    ├── test_proposer_real.py # fail-fast + strict validation (no network)
    ├── test_idempotency.py   # same key/hash replay, same key/diff hash 409
    ├── test_kill_service.py  # engine down -> 503, retry -> same decision
    └── test_split.py      # approve new phone, split restores v1
```

Service names are exactly `mosaic-api`, `trust-engine` and `matching-engine`, as required by CSD 12.2.

---

## Quick start

### Local, without Docker

Requires Python 3.12+, Go 1.25+, and the pinned manglekit binary
(`ManglekitVersion` in `trust-engine/adapter.go` = `ARG MANGLEKIT_VERSION` in
`trust-engine/Dockerfile` = `MANGLEKIT_VERSION` in `tests/conftest.py`).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r mosaic-api/requirements-dev.txt

# Full suite: 34-case parity + graph + matching-engine + sub-agents + taxonomy +
# structure + flows + idempotency + kill-service + split (builds mkit + engine)
python3 -m pytest -q

# Terminal 1: trust-engine (needs the mkit binary on PATH or MANGLEKIT_BIN)
cd trust-engine
MANGLEKIT_BIN=/path/to/mkit POLICY_DIR=./policies TAXONOMY_DIR=../data/mock/taxonomy go run .

# Terminal 2: mosaic-api
cd mosaic-api
USE_MOCK_MUSE=true uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>: graph canvas on top, builder (sub-agent trace +
Matching Engine status) left, engine (derivation tree) right, structured golden below,
steward queue with approve/reject/split/Accept, downstream OTP/search demo.

### Docker Compose (three containers: mosaic-api, trust-engine, matching-engine)

```bash
cp .env.example .env       # keep USE_MOCK_MUSE=true for mock runs
docker compose up --build
```

- UI and API: <http://127.0.0.1:8000>
- Trust-engine: reachable only on the Compose network, **no published port**
  (CSD Section 9). The image builds the pinned mkit binary in, plus policies
  and the baked taxonomy.
- Ledger: named volume `mosaic-state`, path `/app/state/mosaic.db`. Use a fresh
  ledger for the demo recording (`docker compose down -v` first): `/build`
  cluster ids (`cluster-1`, …) are per-build, so reusing one ledger across
  different datasets mixes entities on re-derive.
- Taxonomy: named volume `mosaic-taxonomy` shared by both services
  (`TAXONOMY_DIR=/app/taxonomy`), seeded from the baked copy on first start.
  Steward Accept writes the new version there, and the trust-engine
  hot-reloads it — no restart. Policy files reload on `SIGHUP` (`kill -HUP`):
  the new policy directory is fully validated before the swap, and a failed
  reload keeps the previous policies in force. Never bind-mount the repo taxonomy dir
  instead: Accept writes would pollute the repo.
- `docker compose config` validates in the current tree.

### Real Muse mode

1. Put your key in `.env` (`META_MODEL_API_KEY=` — Meta docs call the same
   value `MODEL_API_KEY`). `.env` is gitignored and never committed.
2. Keep `USE_MOCK_MUSE=false` and `MUSE_MODEL_ID=muse-spark-1.3` (verified
   against dev.meta.ai docs on 2026-10-27; change only if your access pins a
   different model id). `META_MODEL_API_BASE_URL` defaults to the documented
   `https://api.meta.ai/v1`. Optional: `WONDER_MODEL_ID=muse-wonder-1.0`
   enables Wonder adjudication on escalations only; unset means a labelled
   stub, never a simulated adjudication. `SPLINK_ENABLED=false` forces the
   Muse + fuzzy fallback path (labelled `SPLINK_UNAVAILABLE`).
3. `docker compose up --build` (compose reads `.env`), or export it for a
   local run.

What real mode does: each sub-agent call goes through `meta_client` with a
strict `json_schema` contract (`strict: true`), `reasoning_effort` and
`prompt_cache_key` per agent, bounded transient retry (429/500/503, exp
backoff + jitter, honors `Retry-After`, max 3) plus a circuit breaker
(3 consecutive failures open it for 30s). Every field is Pydantic-validated;
unknown attributes, invented values, or cited-unknown fact ids reject the
whole proposal. Omitted attributes become `abstain` entries, never guesses.
Any remaining failure fails closed (`PROPOSER_UNAVAILABLE` → 503, steward
items recorded, nothing published). Per-call usage (prompt/completion,
cached/reasoning tokens) is logged for the README claim check. Muse is
called only on propose/resolve/build, sub-agents only on candidate clusters,
Wonder only on escalations — never on page load, never in tests.

---

## What is real in the current API

```text
GET  /health                                  -> mosaic_api, trust_engine, splink (+ versions), policy versions
POST /build {dataset, expected_structure?}    -> full canonical flow per cluster; golden(s) or escalate
POST /upload (multipart JSON)                 -> fragments + type detection + link proposals; never publishes
GET  /graph/{type}/{id}[?path_from&path_to]  -> nodes/edges + optional pathfinder/impact
GET  /downstream/{otp|refund|search}          -> golden, or No trusted value (never a guess)
POST /resolve                                 -> 200 resolve | 409 key reuse | 422 bad input | 503 engine/proposer down
GET  /golden/{type}/{id}                      -> latest nested golden, or 404
GET  /explain/{decision_id}                   -> replay: rule(s), winners, ranks, taxonomy, derivation tree, subagent trace, Wonder/Prompt-Guard notes
GET  /steward/pending                         -> steward queue rebuilt from SQLite
POST /steward/{id}/approve {"value"}          -> steward fact + re-derivation + new golden version
POST /steward/{id}/reject                     -> status change, no side effects
POST /steward/{id}/split {"restore_version"}  -> history restore as new version, tombstones kept
POST /steward/{id}/accept {action,taxonomy_version} -> new taxonomy version + explicit re-derive
GET  /similar?entity_type=&entity_id=          -> structured similar search; vector recall skipped (no embedding pinned)
GET  /                                        -> graph canvas + builder/engine panels + golden + steward UI
POST /derive (trust-engine, internal only)    -> 200 derive (+verified_links) | 400 contract/policy/taxonomy/link | 500 engine error (fail-closed)
```

The trust-engine rejects unknown entity types, unknown policy/taxonomy
versions, disallowed attributes, sources that contradict their matrix rank,
bad links (unknown endpoints, out-of-vocabulary proofs, self-links), missing
provenance, missing `verified`, naive datetimes, non-positive or sub-minute
TTLs, times outside the mangle number bound, duplicate fact ids, and any
unknown JSON field — including an `embedding` field. That last rejection is
structural: embeddings must never become derivation inputs (CSD 5.3a / 12.7a).

Time canonicalization (mechanical, not derivation): event times become whole
minutes since 2026-01-01T00:00+07:00; expiry adds the TTL; evaluation time is
the max event time in the request (hermetic and replayable). Sub-minute TTLs
are rejected rather than coarsened; the mangle lexer cannot parse scientific
notation, so magnitudes ≥ 1e6 are rejected fail-closed. These bounds are
tested (G-20) and documented in the `.mg` headers. The mangle dialect needs
care: zero-arity predicates are rejected (use a dummy argument), `==` against
constants is rejected (use unification), and unbound negated variables are
rejected (wrap in named predicates). Verified in `tests/test_rules_*.py`.

The proposer downgrade (mosaic-api, fail-closed direction only): an engine
**publish** on a **critical** attribute becomes an escalation when the mock
proposal has low confidence/abstain, or when an unverified ticket-sourced
fragment conflicts with the derived winner (`UNVERIFIED_CRITICAL_CLAIM`).
Engine escalations and denials are never overridden.

Taxonomy (CSD 3.5): alias/synonym mapping is a Taxonomist claim, applied only
on exact normalized matches; the engine checks membership (T1) and escalates
unknown terms. New terms are never auto-created — the steward Accept flow
verifies the exact suggested action and version, writes the new taxonomy
file, and re-derives explicitly. The trust-engine hot-reloads taxonomy files
so no restart is needed.

---

## Contract guardrails

Enforced in code and tests; must not be relaxed:

1. **No fact without provenance.** `source`, `source_id`, `authority_rank`,
   `event_time` (timezone-aware), `verified` and `ttl_seconds` are required.
2. **No golden without proof.** `winner_fact_id`, `rule_fired`, `policy_version`,
   `taxonomy_version` and `lineage` are required; a winner cannot also be listed as a loser.
3. **No embedding in derivation.** `CandidateFact` and the Go derive request both
   reject the field.
4. **No policy or taxonomy guess.** Unknown entity type, unknown attribute, unknown
   policy/taxonomy version, or rank contradicting the matrix means deny/escalate, never publish.
5. **No proposer publishes.** `/resolve` and `/build` cannot write without a
   trust-engine decision; Muse/Splink output is validated and never published directly.
6. **No unverified link publishes.** Only sole-proof hops verified against live
   facts become verified links; fuzzy/vector links are candidates, conflict
   links escalate (C1); a broken chain escalates the cut-off side (C2), never
   silently publishes a partial golden.
7. **No unvalidated golden.** The assembled nested document must validate as a
   whole; failing fields escalate, and a missing required field withholds the
   golden (STRUCTURE_MISMATCH), never publishes a partial.
8. **No new term without a steward.** AI proposes; only a human Accept creates a
   taxonomy version, bound to one exact action and version, single-use.
9. **Replay is exact.** Decisions replay by id from the ledger (same key+hash
   never re-derives); vector nearest neighbour is never identity.

Demo authority matrices (CSD 12.7, v1 demo parameters, not production
recommendations): user phone SAP-like 1 / Salesforce-like 2 / CIAM-like 3 /
ticket 3; user email CIAM-like 1 / Salesforce-like 2 / SAP-like 3 / ticket 3;
user address ticket-verified 1 / Salesforce-like 2 / SAP-like 3 / CIAM-like 3;
user order_id order-like 1 / marketplace-like 2 / ticket 3; product category
PIM-like 1 / ERP-like 2 / marketplace-like 3. `critical_attributes` is
`["phone"]` for users; empty for products (weak product claims deny via R4,
P-04; unknown terms escalate via T1).

---

## Tests

Current run:

```text
143 passed
```

- `test_contract.py` — fact/golden/graph contracts, policy + taxonomy coherence
  (incl. `.mg` matrix == JSON matrix, L1/C1/C2/T1 wired), synthetic fixture
  shape, DDL gate columns, fail-closed API behaviour with the engine down,
  manglekit version pin coherence.
- `test_rules_user.py` — G-01..G-21 through a live trust-engine (`mkit eval`,
  three queries per derive), incl. a C1 exact-tie unit case.
- `test_rules_product.py` — P-01..P-06 + T1 unknown-term escalation.
- `test_hidden.py` — H-01..H-03: chain publishes with verified links, broken
  hop escalates explicitly (C2), trap not merged (C1 on the phone tie).
- `test_splink.py` — S-01..S-05 against the live matching-engine: hidden cluster found, F6 separate, Muse-only
  semantic link, S-04 with the real service down and S-05 with a malformed response (both unavailable fallback with no silent publish), plus version-mismatch and service-vs-fallback Liskov parity.
- `test_matching_engine.py` — service contract: /health pins + enabled flag, /match candidates with blocking evidence, work_id/policy echo, 422 on unknown blocking/comparisons/policy.
- `test_subagents.py` / `test_meta_tooling.py` — five agents emit claims only;
  Wonder/Prompt Guard are labelled stubs, never simulated as real.
- `test_taxonomy.py` / `test_taxonomy_accept.py` — exact synonym mapping only;
  Accept creates a new version and re-derivation publishes.
- `test_structure.py` — whole-document gate: drop field vs block golden.
- `test_flows.py` — Flow A publish, Flow B injection escalation + phone
  withdrawal, product gate, steward reject, fragments labelling, similar
  evidence.
- `test_build.py` — /build end-to-end (cluster, graph, upload, downstream), incl. nested purchased-golden verification and rebuild-over-nested-golden regression.
- `test_proposer_real.py` — fail-fast + strict validation (no network).
- `test_idempotency.py`, `test_kill_service.py`, `test_split.py` — full-stack
  against a live trust-engine, incl. kill scene and same-decision retry.

A failing case must be reported as a failure with its threshold, never edited
into a pass.

Run the Go checks:

```bash
cd trust-engine
gofmt -l .
go vet ./...
go build ./...
```

---

## Open questions

The CSD leaves these unresolved. Per A-15/A-17, code uses the fail-closed
option and records the question instead of inventing an answer.

1. **Blocking — A-11: application theme/track/idea match.** The project must be
   checked against what was submitted on 26 October 2026 before final
   submission (A-18: do not write submitted track/theme/idea as fact).
2. **Splink at scale.** S-01..S-05 prove the service contract on synthetic
   data with deterministic exact linkage + best-effort EM weights. No
   Splink-beating accuracy and no end-to-end massive-scale golden benchmark
   are claimed (CSD guardrail). If Splink integration slips, ship the core
   alone (CSD 12.1 step 8 checkpoint).
3. **Wonder / Prompt Guard verification (A-19, A-20).** Both are conditional.
   Code paths exist and are tested as labelled stubs; a verified model id or
   connector flips them to real calls. Until then they must not be claimed.
4. **Embedding model/version/dimension.** All three are empty. Similar search
   runs structured-only with an explicit `vector_recall: skipped` note, and
   profile embeddings are NULL. Vectors from different models/versions must
   never be mixed once pinning lands.
5. **`display_name` authority matrix — fixed in `pol-2026-10-28-v1`.** `display_name`
   was allowed but had no matrix ranks, so the engine escalated it
   (`NO_AUTHORITY_RANKS`) — this piled up the P-001 steward queue with one item
   per run even though all sources agreed. v2 assigns sap 1 / salesforce 2 /
   ciam 3 / ticket 3 (mirroring fixture ranks); agreeing `display_name` now
   publishes R1 (regression test G21; v1 behaviour pinned by G17). v1 files untouched.
6. **Product critical attributes.** Empty per CSD silence; weak product claims
   deny via R4 (P-04), unknown terms escalate via T1. Confirm whether a
   low-confidence product conflict should instead escalate.
7. **manglekit version.** Pinned at `v0.0.0-20260330150740-81e7572ffb31` in
   `trust-engine/adapter.go`, `trust-engine/Dockerfile` and
   `tests/conftest.py`; `/health` reports it. If upstream moves, update all
   three and re-run parity before recording.
8. **`conflicts` + `agent_traces` tables.** CSD 12.5 fixes gate columns
   (untouched) but defines no home for per-attribute conflict reasons or
   subagent traces. The new tables store derive-output and proposer audit
   rows so replay never re-derives and never re-calls Muse.
9. **Single-container subprocess fallback.** Partially in the repo: unset `MATCHING_ENGINE_URL` runs the verbatim in-process matcher (`mosaic-api/splink_local.py`, parity-tested vs the service). A Python mkit-subprocess fallback for derive is still missing, so the trust-engine container is still required. The Go service already shells out to the pinned mkit binary (same contract, documented adapter).
10. **TTL policy keys.** `detector-like`/`inventory-like` windows from the CSD
    are carried in policy JSON but not mapped to attributes; per-fact
    `ttl_seconds` drives R3. Confirm the intended mapping before using the
    windows.
11. **`ticket-verified` provenance — confirmed in code.** Legitimate
    `ticket-verified` (address rank 1, verified) wins via R1 (G-03); bare
    `ticket` claims are unverified and escalate on critical attributes
    (Flow B). The distinction is tested, not just documented.

---

## Next build steps (CSD 12.1 order)

- [x] 1. Scaffold, config, SQLite DDL (incl. graph tables)
- [x] 2. Pydantic schemas + fact/golden/graph contract tests
- [x] 3. Mock data: users, products, hidden cluster, taxonomy, templates
- [x] 4. Endpoints with mock Muse first + sub-agent stubs (claims only)
- [x] 5. Trust-engine adapter + policies (R1-R4 + L1/C1/C2/T1) + parity tests
- [x] 6. Publisher, ledger + graph writes, Explain/replay, steward queue, similar search
- [x] 7. Mosaic UI (canvas, builder/engine panels, structured golden, tiles, upload, downstream)
- [x] 8. Splink service (after 29 non-Splink cases passed) + S tests against the live service
- [x] 9. Meta tooling integration (conditional Wonder/Prompt Guard, or labelled stubs)
- [ ] 10. Demo video recording (~2 min, hidden connection 80s + Splink + kill-engine) + README claims traced to decision/test ids
- [ ] 11. Submission checklist, early submission on 4 November

---

## Security and data boundary

- All demo records are synthetic; no real PII, Salesforce, SAP, CIAM or ticket
  integration exists. Uploads carry the no-real-PII-in-demo warning; uploaded
  content goes to the Muse builder only in real mode.
- The Meta Model API key is server-side only. It is not in the repository, the
  frontend, test fixtures or Compose constants. `.env` is ignored.
- The trust-engine has no published port in Compose; mosaic-api reaches it on
  the internal network.
- The SQLite ledger is append-only in effect: golden versions are retained,
  loser facts become tombstones, decisions are never updated in place (a
  failed engine-down attempt writes no decision row, so its reserved id stays
  free for the retry).
- The synthetic injection ticket is a labelled fixture for a demo scene, not a
  real attack or a claimed vulnerability.
- Unverified ticket output is logged as a claim and escalated on critical
  attributes; it is never published, even when the engine could derive a
  value around it.
- A steward decision (approve/reject/split/Accept) is a new fact or a new
  taxonomy version + explicit re-derive — never a hand edit. Accept is bound
  to one exact suggested action and version, single-use.


## Operating notes

- **API token.** Set `MOSAIC_API_TOKEN` to require `Authorization: Bearer <token>`
  on every state-changing request (steward approve/reject, jobs, uploads, policy
  apply). Reads stay open. Unset = open, for local development only; the server
  logs a warning. The cockpit asks for the token once on a 401 and keeps it in
  `sessionStorage`.
- **Policy Copilot** (`POST /api/v1/pipeline/copilot/draft`). English in, a *draft*
  out. The proposer (a deterministic parser in mock mode, the Meta model in live
  mode) can only set a source's trust tier or mark an attribute multivalued;
  anything else is refused. Fixed templates compile the change and `mkit` decides
  whether the result is a valid program. Nothing is applied or published.
- **Proposer model.** `/health` reports `proposer_mode` (`mock`|`live`) and the
  model id; the cockpit shows it on Stage 01 instead of naming a model it is not
  running.
- **Styling.** `web/studio.css` is built from `web/tailwind.config.js` with
  `scripts/build_css.sh` and committed; no runtime Tailwind CDN. Fonts and icons
  still load from Google Fonts, so the demo needs network for those.

# Test and verification report

Snapshot of the verification state of this repository. Rerun the commands
before quoting any number; this page is evidence of one run, not a guarantee.

| | |
| --- | --- |
| Date | 2026-11-03 |
| Commit | branch `g1-g2-slice` (G1/G2 + house-number guard); baseline `7721f95` |
| Python | 3.12.3 |
| pytest | 9.1.1 |
| Go | 1.25.1 |
| manglekit | `v0.0.0-20260330150740-81e7572ffb31` (pinned in `tests/conftest.py`) |
| Scope | Full suite, lint, format, Go checks, and the measured benchmarks listed below |

## Verdict

| Check | Command | Result |
| --- | --- | --- |
| Python tests | `python3 -m pytest -q` | **298 passed, 0 failed**, 96.47 s |
| Lint | `ruff check mosaic-api matching-engine scripts tests shared` | **clean** |
| Format | `black --check mosaic-api matching-engine scripts tests shared` | **clean**, 86 files unchanged |
| Go format | `gofmt -l .` | **clean**, no output |
| Go vet | `go vet ./...` | **clean**, no output |
| Go tests | `go test -count=1 ./...` | **ok**, 0.209 s |

The suite is not a mock-only suite. `tests/conftest.py` builds the pinned
`mkit` binary, starts the real trust-engine on a free port, and starts the
Splink matching-engine; rule cases run through the real `mkit eval` path. The
run needs no external network and no Meta API key.

```text
$ python3 -m pytest -q
........................................................................ [ 25%]
........................................................................ [ 50%]
........................................................................ [ 75%]
.....................................................................    [100%]
298 passed in 96.47s (0:01:36)

$ python3 -m ruff check mosaic-api matching-engine scripts tests shared
All checks passed!

$ python3 -m black --check mosaic-api matching-engine scripts tests shared
All done! 86 files would be left unchanged.

$ cd trust-engine && gofmt -l . && go vet ./... && go test -count=1 ./...
ok  github.com/duynguyendang/muse-mosaic/trust-engine  0.209s
```

## Suite inventory

298 collected tests across 30 files (13 added on `g1-g2-slice`, all in `test_muse_candidates.py`).

| Area | Tests | Files | What it proves |
| --- | --- | --- | --- |
| Contract, rules, trust gate | 88 | 9 | Facts and goldens carry provenance; user/product authority, freshness, TTL and conflict rules hold; S1/S2 semantic evidence and the S3 guard behave; split, rollback and replay are exact; killing the engine publishes nothing |
| Taxonomy | 41 | 3 | Versioned vocabulary, exact-match mapping, steward Accept creates a new version and re-derives, evidence-verified extension accepts only what the data attests |
| Muse and model layer | 78 | 5 | Five sub-agents emit claims only; real-proposer fail-fast and strict validation; candidate shortlist, cache, concurrency, partial failure; Copilot closed vocabulary |
| Pipeline, API, jobs, UI | 91 | 13 | `/build`, `/upload`, `/resolve`, export, inbox and jobs; Splink and matching-engine contracts; auth; analyser; static UI wiring; Olist end-to-end |

Per file:

| File | Tests | Covers |
| --- | --- | --- |
| `test_contract.py` | 35 | Fact/golden schema, provenance, policy and taxonomy rejection, DDL, mock-data shape |
| `test_taxonomy_proposals.py` | 34 | Evidence checks (ground, attest, not-a-variant, relate, unique), digit-safe skeleton, version lineage, atomic writes, idempotency |
| `test_muse_candidates.py` | 44 | Shortlist scoring and caps, no global cap, verdict cache (reuse, invalidation, 1,200-key chunking), parallel judge, partial and total failure, merge limits |
| `test_rules_user.py` | 21 | G-01..G-20 user survivorship through the live engine |
| `test_ui_wiring.py` | 19 | DOM ids referenced by `studio.js` exist in `index.html`; file split guards |
| `test_semantic_rules.py` | 15 | S1 `semantic_phone`, S2 `semantic_email`, S3 guard, against the live engine |
| `test_policy_copilot.py` | 14 | Closed vocabulary, deterministic compile, engine verdict, refusal with a reason |
| `test_jobs.py` | 10 | Enqueue, run, replay, resume, retry, cancel, budgets |
| `test_proposer_real.py` | 9 | Fail-fast config and strict output validation without network or credit |
| `test_inbox.py` | 9 | File and signed-webhook intake queues jobs, never derives |
| `test_splink.py` | 8 | S-01..S-04: hidden chain found, trap separate, malformed response fails closed |
| `test_flows.py` | 8 | Demo flows A/B/C: authority beats recency, hidden chain, fail closed |
| `test_subagents.py` | 7 | Five agents, claims only, never publish (mock mode) |
| `test_rules_product.py` | 7 | P-01..P-06 product category through the live engine |
| `test_export.py` | 7 | Export shape, lineage per field, policy/taxonomy versions, verify page |
| `test_build.py` | 7 | End-to-end `/build`, `/upload`, `/graph`, `/downstream`, Accept |
| `test_reference.py` | 6 | Read-only reference endpoints and demo kill switch |
| `test_taxonomy_accept.py` | 4 | Steward-only new term, new version, re-derive |
| `test_meta_tooling.py` | 4 | Wonder and Prompt Guard are conditional and honestly stubbed |
| `test_auth.py` | 4 | Token gates every write; reads stay open |
| `test_analyser.py` | 4 | Orphan, stale and duplicate findings become steward items; overdue scan |
| `test_taxonomy.py` | 3 | Versioned vocabulary and exact synonym mapping |
| `test_structure.py` | 3 | Whole-document validation; mismatch withholds the golden |
| `test_olist_brazilian.py` | 3 | Real Olist data end to end on an isolated stack |
| `test_matching_engine.py` | 3 | Health, `/match` contract, proposer-only behaviour |
| `test_hidden.py` | 3 | H-01..H-03 hidden-connection scene at the gate |
| `test_csv_upload.py` | 3 | CSV parsing and `/upload` |
| `test_idempotency.py` | 2 | Same key and hash replays; different hash does not |
| `test_split.py` | 1 | Merge, approve, split back to the first version |
| `test_kill_service.py` | 1 | Trust-engine down: no new golden, fail closed |

## Claim-to-evidence matrix

| README claim | Evidence | Status |
| --- | --- | --- |
| No fact without provenance, no golden without proof | `test_contract.py` (35), `test_structure.py` (3) | verified |
| Proposers never publish | `test_flows.py`, `test_subagents.py`, `test_build.py` | verified |
| User and product rules hold against the real engine | `test_rules_user.py` (21), `test_rules_product.py` (7) | verified |
| A model's say-so never verifies an identifier | `test_semantic_rules.py` (15) | verified |
| Fail closed when the engine is down | `test_kill_service.py`, `test_flows.py`, `test_jobs.py` | verified |
| Split, rollback and replay are exact | `test_split.py`, `test_idempotency.py` | verified |
| Splink proposes only; malformed output fails closed | `test_splink.py` (8), `test_matching_engine.py` (3) | verified |
| Candidate matching scales without a global cap | `test_muse_candidates.py` (44) | verified |
| Verdict cache never changes a decision, prompt edits invalidate | `test_muse_candidates.py` | verified |
| Taxonomy extension accepts only attested, related terms | `test_taxonomy_proposals.py` (34) | verified |
| Copilot cannot write policy or Datalog | `test_policy_copilot.py` (14) | verified |
| Conditional Meta tools are not simulated | `test_meta_tooling.py` (4) | verified |
| UI references only existing DOM ids | `test_ui_wiring.py` (19) | verified |

## Measured benchmarks

These are model or scale measurements, not unit tests. They need a live key for
the Muse arms; the numbers below are the ones recorded in `README.md`.

| Benchmark | Arm | Result | Status |
| --- | --- | --- | --- |
| `eval_muse_scale.py`, 10k people | String rules | 255/600 semantic links, 116 false merges | measured (mock) |
| `eval_muse_scale.py`, 10k people | Muse `high` | 584/600, **0 false merges**, 225 calls, 366 s, ~790k tokens | measured (live) |
| `eval_muse_scale.py`, 10k people | Muse `medium` | 590/600, **0 false merges** | measured (live, cached) |
| `eval_muse_scale.py`, 10k people | Oracle on same candidates | 600/600, 0 false merges | measured |
| `eval_muse_scale.py`, 100k people | String rules | 516/1,200 semantic links, 4,688 false merges | measured (mock) |
| `eval_muse_scale.py`, 100k people | Oracle | 1,195/1,200, 0 false merges | measured |
| `eval_muse_scale.py`, 100k people | Muse | not run (~6,600 calls) | **not verified** |
| `eval_hard_names.py`, 479 pairs | String rules | 203/319, 3 false merges | measured (mock) |
| `eval_hard_names.py`, 479 pairs | Muse `high` | 279/319, 0 false merges | measured (live) |
| `eval_hard_names.py`, 479 pairs | Muse `medium` | 318/319, 0 false merges | measured (live) |
| `eval_vn_blocking.py`, 5k Vietnamese people | `token` blocking | 0 candidates, 0/250 | measured |
| `eval_vn_blocking.py`, 5k Vietnamese people | `rarest_pair` + Muse + house-number guard | 247/250 reach Muse, 238/250 recovered, 0 false merges (3 before the guard), 1.53M tokens | measured (live) |
| `eval_muse_candidates.py`, 282 Kaggle variants | Muse | 227 links vs 225 deterministic, 0 false merges | measured (live) |
| `eval_taxonomy_gap.py`, Kaggle 389 products | Rules / Muse + evidence | 389 unified, 0 wrong, 0 held | measured (live engine) |
| `eval_taxonomy_gap.py`, semantic 88 records | Muse + evidence | 58 unified, 0 fragmented, 0 wrong | measured (live engine, pre-digit-fix caveat) |

Reproduce:

```bash
python3 scripts/eval_muse_scale.py --people 10000 --judge mock
python3 scripts/eval_muse_scale.py --people 10000 --judge live   # needs .env
python3 scripts/eval_muse_scale.py --people 100000 --judge mock
python3 scripts/eval_hard_names.py --judge mock|live
python3 scripts/eval_muse_candidates.py
python3 scripts/eval_taxonomy_gap.py --dataset semantic --arms A B C D
```

## Not verified

Stated here so the passing suite is not read as a broader claim:

- **The 100k Muse arm has not been run.** The 100k table is rules and oracle
  only. Budgeting ~6,600 judge calls is a product decision, not a test result.
- **Generalisation to another schema or script is not verified.** Candidate
  matching reads `display_name` / `address` / `phone` / `email` and runs only for
  `entity_type == "user"`. A 10k Vietnamese-name set with realistic surname
  distribution returns 0 candidate pairs because every token is hot. Taxonomy
  verification is Latin-only (`skeleton()` keeps `[a-z0-9]`).
- **The taxonomy semantic-set numbers predate the digit fix.** Rerun
  `eval_taxonomy_gap.py --dataset semantic --arms A B C D` before quoting them;
  the script now also prints `vocab`.
- **Wonder and Prompt Guard are not pinned.** They are tested as labelled stubs,
  which is what `test_meta_tooling.py` proves, not that they work.
- **A Copilot draft cannot be staged as a live profile.** The test proves the
  draft is closed-vocabulary and engine-validated, not that it can be promoted.
- **No production accuracy claim.** Benchmarks use synthetic generators, Kaggle
  samples and hand-written variants. One live run, one model: verdicts can vary
  between runs, which is why `--dump` / `--from-dump` exist.
- **No massive-scale benchmark.** Splink is proven on sample data; the 100k
  numbers above are candidate-generation measurements on a laptop, not a
  production throughput claim.

## How the suite is isolated

- The trust-engine binary is started per test session on a free port; the
  matching-engine is started the same way; both are stopped at teardown.
- `mkit` is built from the version pinned in `tests/conftest.py`. If upstream
  moves, update `trust-engine/adapter.go`, `trust-engine/Dockerfile` and
  `tests/conftest.py` together, then re-run parity.
- Background jobs run through `run_job_once` with background tasks disabled, so
  tests are deterministic and do not race the scheduler.
- Olist runs on a scratch policy directory copied from the repo policies, so the
  suite never mutates repository fixtures.
- The ledger is per-test-session; golden versions and tombstones are append-only.
- No test calls the Meta API. Live model benchmarks are separate scripts and
  require `META_MODEL_API_KEY` in `.env` (not auto-loaded; export it first).

# Test and verification report

Snapshot of the verification state of this repository. Rerun the commands
before quoting any number; this page is evidence of one run, not a guarantee.

| | |
| --- | --- |
| Date | 2026-11-06 |
| Commit | branch `main` (2026-11-06: breaker enforcement, M4 ladder, trap suite, publication policy; see per-change logs under `docs/runs/`) |
| Python | 3.12.3 |
| pytest | 9.1.1 |
| Go | 1.25.1 |
| manglekit | `v0.0.0-20260330150740-81e7572ffb31` (pinned in `tests/conftest.py`) |
| Scope | Full suite, lint, format, Go checks, and the measured benchmarks listed below |

## Verdict

| Check | Command | Result |
| --- | --- | --- |
| Python tests | `python3 -m pytest -q` | **437 passed, 0 failed**, 131.90 s (2026-11-06; log `docs/runs/20261106T040824Z-pytest-05e1db3cc4fa.txt`) |
| Lint | `ruff check mosaic-api matching-engine scripts tests shared` | **clean** |
| Format | `black --check mosaic-api matching-engine scripts tests shared` | **clean**, 101 files unchanged |
| Go format | `gofmt -l .` | **clean**, no output |
| Go vet | `go vet ./...` | **clean**, no output |
| Go tests | `go test -count=1 ./...` | **ok**, 0.037 s |

The suite is not a mock-only suite. `tests/conftest.py` builds the pinned
`mkit` binary, starts the real trust-engine on a free port, and starts the
Splink matching-engine; rule cases run through the real `mkit eval` path. The
run needs no external network and no Meta API key.

```text
$ python3 -m pytest -q
437 passed in 131.90s (0:02:11)

$ python3 -m ruff check mosaic-api matching-engine scripts tests shared
All checks passed!

$ python3 -m black --check mosaic-api matching-engine scripts tests shared
All done! 113 files would be left unchanged.

$ cd trust-engine && gofmt -l . && go vet ./... && go test -count=1 ./...
ok  github.com/duynguyendang/muse-mosaic/trust-engine  0.046s
```

## Suite inventory

437 collected tests across 46 files. Since the 2026-11-05 snapshot (381):
ten new files — `test_traps.py` (the six release-gate traps),
`test_engine_router.py` (deterministic matcher router),
`test_auto_witness.py` (the auto floor gate, 12 cases),
`test_run_logs.py` (run-log discipline), `test_onboarding_review.py` (the
human-review gate), `test_suggestions.py` (G4 review queue + canary
predicate), `test_publication_policy.py`, and `test_policy_thresholds.py`
(borderline band + M3 breaker knobs as versioned policy data); `test_jobs.py`
grew the two breaker-enforcement integration tests, `test_autonomy.py` grew
the `enforce_breakers` unit tests, `test_onboarding.py` now approves before
loading (the pending-review gate), and `test_taxonomy.py` tracks the shipped
publication taxonomy.

| Area | Tests | Files | What it proves |
| --- | --- | --- | --- |
| Contract, rules, trust gate | 88 | 9 | Facts and goldens carry provenance; user/product authority, freshness, TTL and conflict rules hold; S1/S2 semantic evidence and the S3 guard behave; split, rollback and replay are exact; killing the engine publishes nothing |
| Taxonomy | 41 | 3 | Versioned vocabulary, exact-match mapping, steward Accept creates a new version and re-derives, evidence-verified extension accepts only what the data attests |
| Muse and model layer | 89 | 5 | Five sub-agents emit claims only; real-proposer fail-fast and strict validation; candidate shortlist, cache, concurrency, partial failure, report fold across entity types; Copilot closed vocabulary |
| Pipeline, API, jobs, UI | 83 | 13 | `/build`, `/upload`, `/resolve`, export, inbox and jobs; Splink and matching-engine contracts; auth; analyser; static UI wiring; Olist end-to-end; breaker stops the build (2 new cases) |
| Entity contracts and onboarding | 63 | 5 | Specs ship as validated data; the onboarding compiler derives a bundle or refuses; unknown entity types fail closed; the DeepMatcher harness pins metric maths and raw counts |
| Autonomy controller (M3) | 21 | 1 | Per-entity modes (off/shadow/suggest/auto), kill switch, breakers (now enforced before derivation), deterministic audit sampling, defaults fail closed, policy-borne breaker knobs |
| Reliability and flexibility | 52 | 8 | The six traps (handler bypass, tampered stored field, expired approval, replay, policy-version mismatch, house-number neighbour); auto witness gate (10); deterministic engine router (8); G4 review queue + canary (8); run-log lint (2); publication policy ships a data drop; onboarding review gate; policy-borne thresholds fail closed |

Per file:

| File | Tests | Covers |
| --- | --- | --- |
| `test_contract.py` | 35 | Fact/golden schema, provenance, policy and taxonomy rejection, DDL, mock-data shape |
| `test_taxonomy_proposals.py` | 34 | Evidence checks (ground, attest, not-a-variant, relate, unique), digit-safe skeleton, version lineage, atomic writes, idempotency |
| `test_muse_candidates.py` | 55 | Shortlist scoring and caps, no global cap, verdict cache (reuse, invalidation, 1,200-key chunking), parallel judge, partial and total failure, merge limits |
| `test_rules_user.py` | 21 | G-01..G-20 user survivorship through the live engine |
| `test_ui_wiring.py` | 7 | DOM ids referenced by `studio.js` exist in `index.html`; file split guards |
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
| `test_reference.py` | 8 | Read-only reference endpoints and demo kill switch |
| `test_taxonomy_accept.py` | 4 | Steward-only new term, new version, re-derive |
| `test_meta_tooling.py` | 4 | Wonder and Prompt Guard are conditional and honestly stubbed |
| `test_auth.py` | 4 | Token gates every write; reads stay open |
| `test_analyser.py` | 4 | Orphan, stale and duplicate findings become steward items; overdue scan |
| `test_taxonomy.py` | 3 | Versioned vocabulary and exact synonym mapping |
| `test_entity_judging.py` | 19 | Per-entity judging, grouping prompt, admission contract |
| `test_onboarding.py` | 16 | Profiling, mapping refusals, taxonomy bootstrap, policy derivation, all-or-nothing bundles |
| `test_spec_files.py` | 11 | Shipped specs load from data; invalid specs fail closed; a new entity is a data drop |
| `test_schema_contract.py` | 10 | Roles, normalizers, prompt compiler, privacy gate, verdict cache keys |
| `test_eval_deepmatcher.py` | 7 | Metric maths, raw-data counts, truth construction for the ground-truth harness |
| `test_autonomy.py` | 19 | Modes, freeze demotes auto->shadow, breakers, audit sampling, off/shadow end-to-end via /build |
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
| Candidate matching scales without a global cap | `test_muse_candidates.py` (55) | verified |
| Verdict cache never changes a decision, prompt edits invalidate | `test_muse_candidates.py` | verified |
| Taxonomy extension accepts only attested, related terms | `test_taxonomy_proposals.py` (34) | verified |
| Copilot cannot write policy or Datalog | `test_policy_copilot.py` (14) | verified |
| Conditional Meta tools are not simulated | `test_meta_tooling.py` (4) | verified |
| UI references only existing DOM ids | `test_ui_wiring.py` (7) | verified |
| Entity schema contracts ship as validated data; a bad spec never half-loads | `test_spec_files.py` (11) | verified |
| Unknown entity types fail closed (422 / raise), never silently judged | `test_muse_candidates.py` (55), `test_schema_contract.py` (10) | verified |
| The onboarding compiler derives a bundle or refuses; nothing is half-derived | `test_onboarding.py` (16) | verified |
| A merged product golden never publishes guessed identity | `test_muse_candidates.py` (55) | verified |
| Auto-apply is opt-in per entity; default is fail-closed; kill switch works | `test_autonomy.py` (21), `test_auto_witness.py` (10) | verified |
| Breakers stop the build before derivation; deliberate off/shadow never masquerade as failure | `test_jobs.py` (12), `test_autonomy.py` (21) | verified |
| The six release traps hold: handler bypass, tampered stored field, expired approval, replay, policy-version mismatch, house-number neighbour | `test_traps.py` (6) | verified |
| Suggested merges are labelled, never applied; accept/reject is exactly-once | `test_suggestions.py` (8) | verified |
| Auto never starts without a witnessed live floor | `test_auto_witness.py` (10) | verified |
| Engine selection is a deterministic rule of shape and coverage, never of benchmarks | `test_engine_router.py` (8) | verified |
| An unapproved onboarding bundle never loads; approval binds the review digest | `test_onboarding_review.py` (9) | verified |
| Publication ships as data: policy + taxonomy; a new entity needs no code | `test_publication_policy.py` (2), `test_spec_files.py` (11) | verified |
| Measured claims carry run logs with SHA/date/model id; bare claims fail the suite | `test_run_logs.py` (2), `docs/runs/` | verified |

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

### Ground-truth accuracy (labelled pair sets, `eval_deepmatcher.py`)

`eval_deepmatcher.py` scores blocking and judge arms on labelled pair sets —
nothing sampled, truth from union-find over the published pairs. Mock rows
reproduce deterministically and were re-run and logged 2026-11-06
(`docs/runs/20261106T020340Z-deepmatcher-all-mock-05e1db3cc4fa.json`). The
live Muse rows have **no run log**: they were never recorded with SHA, date
and model id, so they are unlogged claims, not measurements, until a live
witness run is executed and written to `docs/runs/`.

| Benchmark | Arm | Result | Status |
| --- | --- | --- | --- |
| Amazon-Google (4,589 docs, 1,167 positive pairs) | String rules (T) | F1 15.2% | measured (mock; reproduced + logged 2026-11-06, `docs/runs/20261106T020340Z-deepmatcher-all-mock-05e1db3cc4fa.json`) |
| Amazon-Google | Token candidate recall | 93.9% | measured (mock; reproduced + logged 2026-11-06) |
| Amazon-Google | Oracle over token candidates | F1 96.9% | measured (mock; reproduced + logged 2026-11-06) |
| Amazon-Google | Muse on top of B | F1 50.4% | **unlogged claim** — no run log with SHA/date/model id; do not cite until a live witness run is executed |
| DBLP-ACM dirty (4,910 docs, 2,220 positive pairs) | String rules (T) | F1 23.9% | measured (mock; reproduced + logged 2026-11-06) |
| DBLP-ACM dirty | Token candidate recall | 99.2% | measured (mock; reproduced + logged 2026-11-06) |
| DBLP-ACM dirty | Oracle over token candidates | F1 99.6% | measured (mock; reproduced + logged 2026-11-06) |
| DBLP-ACM dirty | Muse on top of B | F1 85.8% | **unlogged claim** — no run log with SHA/date/model id; do not cite until a live witness run is executed |
| Kaggle messy-customers (10,666 rows, 666 gold pairs) | Exact name, raw / normalised | recall 100% but precision 0.1% / 0.0% — 50 / 20 impure mega-clusters | measured (mock; reproduced + logged 2026-11-06) |
| Kaggle messy-customers | Token block | 0 candidates: every name in the 30-name pool is hot | measured (mock; reproduced + logged 2026-11-06) |
| Kaggle messy-customers | Exact email (identity hint) | precision **100%**, recall 91.9% (612/666), F1 **95.8%**, 0 impure clusters | measured (mock; reproduced + logged 2026-11-06) |
| Kaggle messy-customers | M arm on identifier base (mock) | identical to the E arm; the judge is never called — there is no middle band to decide | measured (mock; reproduced + logged 2026-11-06) |

Reading the messy-customers rows: the name pool is 30 distinct values across
10,666 rows, so name-only matching merges strangers (precision ~0) and token
blocking yields nothing — exactly the failure the fail-closed design refuses to
paper over. Exact email recovers every gold pair that carries one (612/666);
the 54 misses all share a placeholder or missing email (N/A, `-`, none, null,
`?`) and are correctly withheld rather than guessed. This set measures recall
behaviour on real-world dirty dirt, not semantic discrimination — every gold
pair is exact after normalisation.

### Autonomy calibration (M3, mock judge, 2026-11-05)

`scripts/eval_calibration.py` measures precision/recall/F1/impure clusters at
both shipped confidence floors, on the same docs for each arm. Mock rows
below reproduce deterministically (re-run + logged 2026-11-06:
`docs/runs/20261106T020739Z-calibration-all-mock-05e1db3cc4fa.json`); the
mock judge is **not calibrated** and over-merges the product/publication
suites — a mock row is plumbing evidence only, never a floor witness. The
live floor is **unwitnessed** until a live run is approved, executed and
logged to `docs/runs/`; `AUTONOMY_MODE=auto` now refuses to start without it
(`MUSE_AUTO_WITNESS`, see `.env.example`).

| Benchmark | Arm | Result | Status |
| --- | --- | --- | --- |
| Amazon-Google | Muse mock, high & medium | precision 12.3%, recall 8.9%, F1 10.3%, 161 merges (over-merge — not a witness) | measured (mock, 2026-11-05) |
| DBLP-ACM dirty | Muse mock, high & medium | precision 39.9%, recall 3.6%, F1 6.5%, 26 merges (over-merge — not a witness) | measured (mock, 2026-11-05) |
| Kaggle messy-customers | Muse mock, high & medium | precision **100%**, recall 91.9%, F1 95.8%, 0 impure | measured (mock, 2026-11-05) |
| Precision at the auto floor | any | **not witnessed** — needs the live arm (approx. 18.5k judged pairs across both suites) | **not verified** |

The breaker/kill-switch/audit pieces are unit- and integration-tested instead:
auto demotes to shadow under freeze, deliberate off never raises a breaker,
and the 1% audit sampler plus 100% of withheld decisions is deterministic
across runs.

### Scale runs (end-to-end stacks, 2026-11-05)

Full three-service stacks (real trust-engine + Splink 5.0.0 + mosaic-api),
mock proposer, fresh scratch ledger per run.

| Run | Result | Status |
| --- | --- | --- |
| Kaggle Customer 360, 5,000-row sample (43,377 docs: 42,907 user, 470 product), `scripts/kaggle_e2e.py` | Users: 1,772 s, 4,928 clusters / 71,918 candidate pairs, **4,791 goldens published** (email 4,791, display_name 4,757, address 4,775, phone 4,775), 4,823 steward items, overall verdict `escalated` (137 clusters withheld); downstream OTP check on a golden: pass. Products: 8 s, 39 clusters / 8,804 pairs, 0 goldens — every category escalates T1 (no product taxonomy seeded), 548 steward items | measured (mock proposer, 2026-11-05) |
| Kaggle Customer 360, frozen 1,000-row sample | previously recorded: 9,426 docs, 170 clusters from 7,781 pairs, 163 goldens (source: `docs/internal/` write-up, git-ignored) | measured (mock proposer, recorded run) |
| Olist large frozen sample (130 docs, 15 repeat-customer clusters + 1 trap), `OLIST_SAMPLE=data/olist/sample_olist_large.json pytest tests/test_olist_brazilian.py` | 3/3 pass in 29.13 s — verbatim values, L1 exact-order proofs, trap never merged | measured (live engine, 2026-11-05) |
| Olist frozen 26-doc sample (default) | 3/3 pass in 9.42 s | measured (live engine, 2026-11-05) |

Reproduce:

```bash
python3 scripts/eval_deepmatcher.py --dataset all --judge mock
python3 scripts/eval_deepmatcher.py --dataset messy-customers --judge live
python3 scripts/eval_muse_scale.py --people 10000 --judge mock
python3 scripts/eval_muse_scale.py --people 10000 --judge live   # needs .env
python3 scripts/eval_muse_scale.py --people 100000 --judge mock
python3 scripts/eval_hard_names.py --judge mock|live
python3 scripts/eval_muse_candidates.py
python3 scripts/eval_taxonomy_gap.py --dataset semantic --arms A B C D
python3 scripts/kaggle_e2e.py --sample data/kaggle/customer360_sample_5000.json
python3 scripts/eval_calibration.py                      # mock; --judge live for the real floor
OLIST_SAMPLE=data/olist/sample_olist_large.json python3 -m pytest tests/test_olist_brazilian.py
```

## What the guards now enforce (2026-11-06)

The flexibility/reliability work landed in this snapshot; each item below is
code-enforced, not reported:

- **M3 breakers halt the build.** `jobs.execute_full_build` calls
  `autonomy.enforce_breakers` after clustering and before any golden is
  derived: zero candidates at scale or judge failures > 10% stop the job
  (`failed`, never retried) and make the sync `/build` answer 409. The
  constants ride the policy file (`thresholds.m3_breakers`) and are
  fail-closed validated at load.
- **Auto-apply refuses to start without a witnessed floor.**
  `MUSE_AUTO_WITNESS` must point at a live calibration run log (precision
  ≥ 0.995 by default on every measured dataset, judged pairs > 0); anything
  missing, mock-judged, or below the floor is refused at startup. The full
  path is covered by `test_auto_witness.py`.
- **Suggest/canary produce a labelled queue.** `suggest` persists
  would-merge pairs to `merge_proposals`; humans label them at
  `/api/v1/suggestions` (accept/reject, exactly-once, audit fields);
  `AUTONOMY_CANARY_RATE` restricts auto to a deterministic hash slice and
  holds the rest for the same queue. Labels are never applied to goldens.
- **Onboarding bundles require human sign-off.** Derived terms and ranks
  land as pending review; `load_policies`/`load_taxonomies`/the run CLI all
  refuse an unapproved bundle (tamper evidence: approval binds the review
  digest; a changed review is rejected). `--accept` records reviewer and
  timestamp; the engine reloads only the approved bundle.
- **Write-layer gate at the ledger's edge.** `persist_decision` refuses a
  golden outside the policy's `golden_attributes` or one derived under a
  no-longer-active policy version — nothing is written, ever (the
  "handler bypass" trap).
- **Stored goldens are integrity-checked.** New rows carry `content_sha256`;
  `/golden` recomputes before serving and answers 409 on a mismatch
  (the "tampered stored field" trap). Legacy rows predate the hash and are
  served unverified by design.
- **Every measured claim is logged.** `docs/runs/` holds the run records
  cited on this page; `tests/test_run_logs.py` fails the suite if a quoted
  claim loses its evidence or an old untraceable provenance phrase returns.

## Not verified

Stated here so the passing suite is not read as a broader claim:

- **The 100k Muse arm has not been run.** The 100k table is rules and oracle
  only. Budgeting ~6,600 judge calls is a product decision, not a test result.
- **Schema generalisation is verified only on these datasets.** Since M0–M2
  the entity contract is data (`data/schemas/*.json`), Muse runs per entity
  type, and the onboarding compiler derives working bundles for the DeepMatcher
  product/publication sets and the Kaggle person sets (deterministic replay,
  engine-validated policies). Still not verified: a brand-new entity type
  driven end-to-end through `/build` on live data, and taxonomy verification
  beyond Latin (`skeleton()` keeps `[a-z0-9]`).
- **The new Kaggle runs are mock-proposer runs.** The Customer-360 5k e2e and
  the messy-customers arms run with `USE_MOCK_MUSE=true`
  (`proposer_mode=mock`) — the same methodology as the documented 1k Kaggle
  run. They measure pipeline plumbing, fail-closed behaviour and blocking
  recall at scale, not model accuracy. Live-Muse evidence stays the DeepMatcher
  and hard-names rows above.
- **The messy-customers live Muse arm is deliberately unrun.** All 666 gold
  pairs are exact after normalisation, so there is no middle band for a judge
  to decide; live calls would add nothing measurable. The 54 gold pairs with
  placeholder emails are unmergeable from this data by construction.
- **Kaggle products publish nothing in these runs (0 goldens, expected).** No
  product taxonomy is seeded for the 5k run, so T1 escalates every category —
  fail-closed behaviour, not a pass.
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

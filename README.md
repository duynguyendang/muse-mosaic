# Muse Mosaic

> Many fragments, one trusted picture — with proof for every piece, and
> `No trusted value` when proof runs out.

**Muse Mosaic turns fragmented records into records an agent may act on.**

The same person appears in a CRM, an order system and a support tool with
different phones, emails, addresses and spellings of their name. An agent that
reads whichever copy it finds first will send an OTP to the wrong number, refund
the wrong account, or ship to the wrong address. Better matching alone does not
fix that. Someone still has to decide: *may we act on this merged value, right
now, and on what proof?*

Mosaic answers that question with exactly two outputs:

| Output | Meaning |
| --- | --- |
| **Golden record** | One versioned record per entity. Every field carries its winner, the rule that chose it, the policy version, the taxonomy version and its lineage. |
| **`No trusted value`** | The request is held, with a reason. Downstream is told to stop — not to guess, not to fall back to a fragment, not to fall back to a score. |

That is the product: **Splink gives scale, Muse gives understanding, and a
deterministic engine gives permission to act.**

---

## Why each part exists

Remove any one part and the system loses something it cannot get elsewhere.

| Part | What it does | What breaks without it |
| --- | --- | --- |
| **Splink** (matching-engine) | Blocks on identifiers and groups records into candidate clusters in seconds | Comparing all pairs: 2,554 documents is already ~3.3M pairs; real portfolios reach trillions |
| **Muse** (mosaic-api) | Judges the pairs identifiers cannot settle — nicknames, initials, reordered names, changed contact details — and proposes links, values and taxonomy terms with reasons | Everything Splink cannot prove stays split into duplicate goldens, or gets merged by spelling rules that cannot tell a spouse from a namesake |
| **Trust engine** (Go + manglekit Datalog) | Verifies every link, derives every value, refuses what it cannot prove | A model score becomes the decision, and there is no proof to replay |
| **Publisher** | Assembles the golden and validates the *whole* document against the expected schema before writing | Agents receive a cluster file instead of a record they can consume |

The division is deliberate: **Muse proposes, the engine decides, the publisher
writes.** No model output is ever published directly.

```text
fragments ──► Splink            candidate clusters (scale)
          ──► Muse              semantic links, values, taxonomy terms (understanding)
                     │          claims only — nothing here can publish
                     ▼
          trust-engine          verifies links, derives values (proof)
                     ▼
          publisher             validates the whole document, then writes
                     ▼
          golden ───────────────┴─── held / denied, with a reason
```

---

## Proof: Muse earns its place

These are measured runs, not targets. Each number names its dataset, its arm
and whether Muse was live. The full run — 285 tests, lint, format and Go
checks — is in **[docs/TEST-REPORT.md](docs/TEST-REPORT.md)**.

### 1. Semantic linking at 10k people (live Muse)

`scripts/eval_muse_scale.py` generates 11k people and 12k records with known
truth: 1,000 labelled second records that exact identifiers or spelling cannot
link, plus 500 same-name-different-person pairs that must **not** merge. All
arms judge the same 4,496 candidate pairs.

| Arm | reformat | moved¹ | nickname | initial | dropped middle | False merges |
| --- | --- | --- | --- | --- | --- | --- |
| Exact identifiers, no model | 200/200 | 0/200 | 0/200 | 0/200 | 0/200 | 0 |
| String rules | 200/200 | 200/200 | 100/200 | 19/200 | 136/200 | **116** |
| **Muse, `high`** (product default) | 200/200 | 200/200 | **188/200** | **198/200** | **198/200** | **0** |
| **Muse, `medium`** | 200/200 | 200/200 | **190/200** | **200/200** | **200/200** | **0** |
| Oracle on the same candidates | 200/200 | 200/200 | 200/200 | 200/200 | 200/200 | 0 |

¹ New phone *and* new email: only name and address agree.

Read the semantic rows only (nickname, initial, dropped middle — 600 pairs):

- **String rules recover 255/600 and merge 116 wrong pairs.**
- **Muse recovers 584/600 at `high` and 590/600 at `medium`, with 0 false merges.**
- The oracle reaches 600/600 on the same candidates, so the shortlist was not
  the limit. The judgement was.

Cost of that run: 225 judge calls, 366 s, ~790k tokens for 4,496 pairs. Verdicts
are cached, so a rerun of the same pairs finished in 1.4 s. The one remaining
Muse merge joined two people with the *same name and the same address* — counted
separately as `indistinguishable`, because no judge can separate them from the
fields it is given.

At 100k people (104k records, 131,101 candidates) the rules arm recovers
516/1,200 semantic links and makes 4,688 false merges, while the oracle recovers
1,195/1,200 with 0. **The 100k Muse arm has not been run** (~6,600 calls);
see [Scale and cost](#scale-and-cost).

### 2. Hard names: where spelling stops working (live Muse)

`scripts/eval_hard_names.py` judges 479 labelled pairs directly: nicknames
(Robert/Bob), initials (`R. Brown`), surname-first order, dropped middle names,
Vietnamese name order and diacritics, city aliases (`TP.HCM`/`Sài Gòn`), plus
hard negatives — a spouse at the same address, the same name at another address.

| Judge | Merged right (of 319) | False merges (of 160) | Precision | Recall |
| --- | --- | --- | --- | --- |
| String-similarity rules | 203 | 3 | 99% | 64% |
| Rules that know every nickname in the test² | 310 | 0 | 100% | 97% |
| **Muse, merging at `high`** (product default) | 279 | **0** | **100%** | 87% |
| **Muse, merging at `medium` or `high`** | **318** | **0** | **100%** | **100%** |

² An upper bound, not a baseline: it is built from the same nickname table that
generated the test. A rule set you maintain yourself has to cover nicknames,
name orders and city aliases in every language you see. The model needs none of
that — it costs ~0.75 s per pair instead of microseconds.

Muse also got every hard negative right: `different` for a spouse, `unsure` for
a namesake, never `same`. The 39 pairs it marked `medium` are why
`MUSE_CANDIDATE_MIN_CONFIDENCE` exists; the default stays `high` until you have
measured your own data, because 160 negatives is a small sample.

### 3. Where Muse adds nothing — and we say so

On the Kaggle variant benchmark (`scripts/eval_muse_candidates.py`, 282
labelled variants of 100 real customers), normalising identifiers recovers every
formatting variant with no model at all, and Muse finds 227 links against the
deterministic judge's 225. Formatting dirt is a code problem and code solves it
for free.

Muse's value is the residue code cannot reach: meaning. Knowing where that
boundary sits is what keeps the model bill worth paying.

### 4. Taxonomy: Muse consolidates, evidence verifies

A category the taxonomy does not contain (`clo`, `3l3ctronics`, `Foldable Phone`)
is held by rule T1. With `MUSE_TAXONOMY_PROPOSALS=muse`, Muse proposes new terms
and synonyms, but a proposal is accepted only for the part the data verifies:
every cited synonym must really occur, a new term must be attested by at least
`TAXONOMY_MIN_SUPPORT` records, no term may be a mere prefix or typo variant of
another, and no synonym may be claimed by two terms.

Measured through `/build` against a live engine
(`scripts/eval_taxonomy_gap.py`):

| Dataset | Arm | Unified | Fragmented | Wrong | Held |
| --- | --- | --- | --- | --- | --- |
| Kaggle, 389 real products, 8 categories in 37 spellings | Fixed taxonomy | 0 | 0 | 0 | 389 |
|  | Rules + evidence check | 389 | 0 | 0 | 0 |
|  | Muse + evidence check | 389 | 0 | 0 | 0 |
| Semantic set, 88 records, 6 classes | Rules + evidence check | 58 | 28 | 0 | 2 |
|  | **Muse + evidence check** | **58** | **0** | **0** | 30 |

On real Kaggle data the dirt is string-level, so rules alone reach 100% and
Muse adds nothing. On the semantic set both arms unify 58 records, but rules
split them across **11 terms** (`iPhone 14` and `Galaxy Fold 5` each become a
"category") while Muse produces **3 clean terms**. That is the difference
between a vocabulary and a pile of strings.

The check also refuses what it cannot verify: `điện thoại gập` really is
`Foldable Phone`, but no rule can prove it, so those records stay held. Accepting
them unchecked would buy coverage with the model's say-so. We do not.

---

## Pushing Muse to its limit — safely

Muse is used at four depths, each with a hard boundary.

**1. A five-role proposing pipeline.** Scout cleans and detects, Linker proposes
links, Cluster groups them, Taxonomist maps values to canonical terms, Critic
attacks the result (lookalike names, shared addresses, one-digit phone traps,
injection content, authority and TTL violations). In live mode Linker and Critic
are real model calls with strict JSON schemas; Scout, Cluster and Taxonomist are
deterministic derivations from validated claims. Every proposal is a *claim*;
invalid output rejects the whole batch.

**2. A semantic judge over Splink's residue.** After Splink clusters on exact
identifiers, a model-free shortlist picks the cluster pairs worth judging, code
computes near-identifier evidence (same last 9 phone digits, an email one typo
away), and Muse rules `same` / `different` / `unsure` with a confidence and a
reason. Only `same` at the configured confidence merges, capped at 20 documents
per cluster. `unsure` never merges.

**3. A taxonomy proposer behind an evidence gate.** Muse may name the category;
only attested, verifiable synonyms are written, as a new taxonomy version with
an evidence log — never an edit, never on a model's confidence.

**4. A policy copilot that cannot write policy.** `POST
/api/v1/pipeline/copilot/draft` turns an English instruction into a *draft*
change. The model may only fill a closed vocabulary (a source's trust tier, a
multivalued flag); fixed templates compile it to Horn clauses, `mkit` validates
it, and nothing is staged or applied. A request outside the vocabulary is
refused with a reason.

Three engineering choices let Muse run at 10k–100k scale instead of on a demo
subset:

- **No global candidate cap.** A cap of 200 pairs reviewed 0.2% of plausible
  pairs at 100k clusters. The default is now no cap
  (`MUSE_CANDIDATE_MAX_PAIRS=0`); `MUSE_CANDIDATE_PER_CLUSTER` (3) bounds the
  work per cluster, which is the control that actually scales.
- **Cache, batch, parallel.** Verdicts are cached on model + prompt hash + exact
  name/address input, so prompt edits invalidate automatically and reruns are
  nearly free. Batches of 20 run concurrently (`MUSE_JUDGE_CONCURRENCY`,
  default 8). One failed batch is skipped and reported; if every batch fails,
  the step is labelled `Muse unavailable` rather than silently downgraded.
- **Cost is in the build report.** `candidate_pairs`, `shortlisted`,
  `cache_hits`, `judge_calls`, `judge_failures`, `tokens`, `elapsed_s` — you can
  see what the model cost before you turn it on for a bigger dataset.

And one privacy boundary: the candidate judge receives display names and
addresses only, plus boolean hint flags. Phone numbers and emails never leave
the process; code computes the hint, the model never sees the identifier.

---

## What the engine guarantees

Muse's judgement is never the proof. The trust engine computes mechanical
features it can check itself, and the policy decides:

| Rule | Guarantee |
| --- | --- |
| **S1** `semantic_phone` | A link is a verified hop when both phones share their last 9 digits, extension dropped |
| **S2** `semantic_email` | A link is a verified hop when both emails share a deletion key (one typo, local part ≥ 8 chars) |
| **S3** guard | A names-only merge that leaves a winner cut off from its anchor escalates (`SEMANTIC_MERGE_UNVERIFIED`) instead of publishing |

So a Muse merge lands in one of three tiers: **verified** (identifier evidence
grounds the chain; the merged golden publishes normally), **names-only**
(held; base values are never silently replaced), or **no claim** (nothing
changes).

Contract guardrails, enforced in code and tests:

1. **No fact without provenance** — source, source id, authority rank,
   timezone-aware event time, `verified`, TTL.
2. **No golden without proof** — winner, rule, policy version, taxonomy
   version, lineage.
3. **No proposer publishes** — Splink and Muse output is validated, never
   written directly.
4. **No embedding in derivation.** The engine rejects an `embedding` field
   structurally; vectors may rank candidates, never decide.
5. **No unverified link publishes** — only hops verified against live facts
   count; a broken chain escalates the cut-off side.
6. **No unvalidated golden** — the whole nested document must validate, or it
   is withheld (`STRUCTURE_MISMATCH`).
7. **No new term without verifiable evidence** (see §4 above).
8. **Replay is exact** — same key and hash never re-derives; nearest-neighbour
   is never identity.

**Fail closed everywhere a guess could leak in:** engine down, malformed
response, unknown policy, a rank contradicting the authority matrix, a broken
link chain — deny or escalate, never publish.

### Policy profiles

A policy is a manglekit pair (`.json` metadata + `.mg` Horn clauses). Three
profiles ship under `data/<profile>/policies/`; switching re-runs only the
derive stage from cached clusters. Buckets are disjoint:
`certified + held + denied = clusters`.

| Profile | Intent |
| --- | --- |
| `strict_bcbs239` | Zero leakage: any winner disagreement quarantines the cluster |
| `retail_balanced` | Source hierarchy (ERP 3 > order/OTP 2 > CRM 1) plus freshness; only cross-system disputes quarantine |
| `analytical_mesh` | Multi-valued lineage for feature stores; no quarantine rule, unattested output (OTP and dossier gated in the UI) |

Measured on the bundled Customer 360 sample (247 clusters), last run:

| Profile | Certified | Held (of which partial) | Denied |
| --- | --- | --- | --- |
| `strict_bcbs239` | 2 | 244 (76) | 1 |
| `retail_balanced` | 242 | 4 (4) | 1 |
| `analytical_mesh` | 247 | 0 | 0 |

Rerun on your data before quoting any figure.

---

## Scale and cost

Candidate matching is opt-in (`MUSE_CANDIDATE_MATCHING=true`) because it changes
cluster counts. Identifiers are normalised before blocking with the same
function facts get at ingest, so `(449) 977-1729` and `449.977.1729` cluster
together with no model involved.

| Clusters | Candidate pairs | Judged (`per_cluster=3`) | Judged under the old 200 cap | Funnel time |
| --- | --- | --- | --- | --- |
| 10k | 44,215 | 8,457 | 2.4% | 3.8 s |
| 100k | 267,434 | 48,018 | 0.4% | 23.9 s |

Synthetic name/address data, Zipf-distributed names. Counts are
data-dependent: the 10k benchmark below judged 4,496 pairs; a 100k run of the
benchmark generator produced 131,101.

**Budget the first run; the cache makes reruns cheap.** At 100k, ~131k pairs is
roughly 6,600 judge calls, about 30× the measured 10k run. Measure tokens on
your own data before enabling it, and read `judge_calls` / `tokens` in the build
report afterwards.

Two limits are real today (both are roadmap items, see
[Limits](#limits-today)):

- **Blocking drops hot tokens.** A token in more than 50 clusters blocks
  nothing, which at 10k+ is true of common surnames — and, with realistic
  Vietnamese surname distributions, can empty the shortlist entirely.
- **Muse judges name and address only.** Records that differ in both cannot be
  linked by the judge, however good it is.

---

## Quick start

Requires Python 3.12+, Go 1.25+ and the pinned manglekit binary
(`ManglekitVersion` in `trust-engine/adapter.go` = `MANGLEKIT_VERSION` in
`trust-engine/Dockerfile` and `tests/conftest.py`).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r mosaic-api/requirements-dev.txt

python3 -m pytest -q                      # full suite (builds mkit + engine)
(cd trust-engine && go vet ./... && go test ./...)

# Terminal 1: trust-engine
cd trust-engine
MANGLEKIT_BIN=/path/to/mkit POLICY_DIR=./policies TAXONOMY_DIR=../data/mock/taxonomy go run .

# Terminal 2: matching-engine (optional; without it Splink falls back, labelled)
cd matching-engine && uvicorn main:app --port 8081

# Terminal 3: mosaic-api
cd mosaic-api && USE_MOCK_MUSE=true uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>.

### Docker Compose

```bash
cp .env.example .env        # keep USE_MOCK_MUSE=true for mock runs
docker compose up --build
```

- The trust-engine has **no published port**; mosaic-api reaches it on the
  Compose network.
- The ledger lives in the `mosaic-state` volume. Use a fresh ledger for a
  recording (`docker compose down -v`): `/build` cluster ids are per-build, so
  reusing one ledger across datasets mixes entities.
- Taxonomy lives in a volume shared by both services. Never bind-mount the repo
  taxonomy directory: taxonomy writes would pollute the repo.

### Live Muse mode

Set in `.env` (git-ignored, never committed):

```text
USE_MOCK_MUSE=false
META_MODEL_API_KEY=...        # server-side only
MUSE_MODEL_ID=...             # no default in code; startup fails fast if empty
```

`META_MODEL_API_BASE_URL` defaults to `https://api.meta.ai/v1`. Every agent call
uses a strict `json_schema` (`additionalProperties: false` on every object),
bounded retry with backoff, a circuit breaker, and Pydantic validation; an
invalid proposal is rejected whole. Any remaining failure fails closed
(`PROPOSER_UNAVAILABLE`, 503, nothing published). `GET /health` reports
`proposer_mode` and the model id. Optional: `WONDER_MODEL_ID` enables Wonder
adjudication on escalations; unset means a labelled stub, never a simulated one.

### Configuration

| Variable | Purpose |
| --- | --- |
| `USE_MOCK_MUSE` | `true`: deterministic local agents. `false`: Meta Model API. |
| `META_MODEL_API_KEY`, `MUSE_MODEL_ID`, `META_MODEL_API_BASE_URL` | Live-mode credentials and model. |
| `TRUST_ENGINE_URL`, `MANGLEKIT_BIN` | Where to derive; which `mkit` to run. |
| `MATCHING_ENGINE_URL`, `SPLINK_ENABLED` | Splink service; empty URL runs the in-process matcher. |
| `DATABASE_PATH`, `TAXONOMY_DIR` | Ledger and taxonomy locations. |
| `MOSAIC_API_TOKEN` | If set, every write needs `Authorization: Bearer <token>`. Reads stay open. Unset = open (local dev only). |
| `MUSE_CANDIDATE_MATCHING` | `true`: Muse judges near-match cluster pairs. Default `false`. |
| `MUSE_CANDIDATE_MAX_PAIRS` | Global judge cap. `0` (default) = no cap. |
| `MUSE_CANDIDATE_PER_CLUSTER` | Max judged pairs per cluster (default 3) — the main cost control. |
| `MUSE_CANDIDATE_MIN_CONFIDENCE` | `high` (default) or `medium`: lowest judge confidence that may merge. |
| `MUSE_JUDGE_CONCURRENCY` | Judge batches in parallel (default 8). |
| `MUSE_VERDICT_CACHE` | `true` (default): reuse verdicts for identical name+address inputs. |
| `MUSE_TAXONOMY_PROPOSALS` | `off` (default), `rules` or `muse`: evidence-verified taxonomy extension. |
| `TAXONOMY_MIN_SUPPORT` | Records a new term needs behind it (default 3). |
| `EMBEDDING_MODEL/VERSION/DIM` | Empty by default; similar search runs structured-only. |

---

## API surface

```text
GET  /health                                   services, versions, proposer mode/model
POST /build {dataset, expected_structure?}     full flow per cluster: golden(s) or escalate
POST /upload                                   fragments + type detection + link proposals; never publishes
POST /resolve                                  200 | 409 key reuse | 422 | 503 engine/proposer down
GET  /golden/{type}/{id}                       latest nested golden, or 404
GET  /explain/{decision_id}                    replay: rules, winners, ranks, derivation tree, traces
GET  /downstream/{otp|refund|search}           golden, or No trusted value (never a guess)
GET  /graph/{type}/{id}                        nodes/edges, pathfinder, blast radius
GET  /steward/pending                          held items, rebuilt from SQLite
POST /steward/{id}/approve|reject|split|accept optional hand-off API; not used by the cockpit
GET  /api/v1/pipeline/policies                 profiles with Horn clauses and compile status
POST /api/v1/pipeline/apply-policy             evaluate cached clusters under a profile (preview only)
POST /api/v1/pipeline/copilot/draft            English -> draft policy change (never applied)
POST /derive            (trust-engine, internal)  200 | 400 contract/policy/taxonomy | 500 fail-closed
POST /metrics           (trust-engine, internal)  per-profile cluster metrics
```

Mosaic **classifies; it does not resolve.** A held cluster stays held until
someone outside the cockpit settles it. A steward decision is a new fact or
taxonomy version plus an explicit re-derive — never a hand edit on a golden.

The trust-engine rejects unknown entity types, policy or taxonomy versions,
disallowed attributes, sources contradicting their matrix rank, bad links,
missing provenance, naive datetimes, bad TTLs, out-of-bound numbers, duplicate
fact ids and any unknown JSON field, including `embedding`.

---

## Reproduce the numbers

```bash
python3 scripts/eval_muse_scale.py --people 10000 --judge mock    # rules + oracle arms
python3 scripts/eval_muse_scale.py --people 10000 --judge live    # + Muse (needs .env)
python3 scripts/eval_muse_scale.py --people 100000 --judge mock   # 100k rules + oracle
python3 scripts/eval_hard_names.py --judge mock|live              # hard names
python3 scripts/eval_muse_candidates.py                           # Kaggle variants
python3 scripts/eval_taxonomy_gap.py --dataset semantic --arms A B C D
```

Every benchmark prints its arm, its candidate count and (live) its calls,
failures, tokens and elapsed time. One live run, one model: verdicts can differ
between runs, so `--dump` / `--from-dump` rescore a saved run without API calls.

---

## Limits today

Stated plainly, because the roadmap is built on them:

- **Candidate matching is coupled to one schema and one entity type.** It reads
  `display_name` / `address` / `phone` / `email` and runs only for
  `entity_type == "user"`. A source using `full_name` / `address_line` produces
  empty profiles and zero candidates. Taxonomy aliases are applied later, at
  fragment level, so they do not rescue candidate generation.
- **Blocking is Latin- and space-oriented.** Tokens shorter than 3 characters
  are dropped; tokens in more than 50 clusters block nothing; 2-character CJK
  names block nothing. Measured: a 10k Vietnamese-name set with realistic
  surname distribution returns **0 candidate pairs**.
- **Taxonomy verification is Latin-only.** `skeleton()` folds to `[a-z0-9]`, so
  CJK, Cyrillic and Arabic terms normalise to empty and cannot be verified.
  Taxonomy extension is wired for product + `category` only.
- **Semantic evidence is narrow on purpose.** S1 treats equal last-9 digits as
  the same phone, so a shared household line can verify a merge that Muse's
  name check must then reject. S2 covers exactly one typo. Transpositions, two
  typos, domain typos and name abbreviations are not evidence.
- **The taxonomy verifier accepts frequent strings.** Any value occurring
  verbatim at least `TAXONOMY_MIN_SUPPORT` times can become a term, so a
  rules-only run can mint `iPhone 14` as a category. Muse consolidates better;
  the check itself would still accept a frequent unrelated string it proposed.
- **Splink is proven on synthetic and sample data** with deterministic
  exact-identifier linkage plus best-effort EM weights. No massive-scale or
  production accuracy claim is made, and the 100k Muse arm has not been run.
- **Wonder and Prompt Guard are conditional**: labelled stubs until a model id
  or connector is pinned, never claimed otherwise.
- **A Copilot draft cannot yet be staged as a live profile**; that needs profile
  staging and an engine reload.
- **Co-location chaining in ordinary `/build` clusters.** Clusters Muse did
  not merge chain every fact with `same_fragment`, which grounds all facts and
  means rule C2 cannot fire there. Muse-merged clusters chain only inside their
  original parts, which is what makes S3 effective.
- There is no Python fallback for derive, so the trust-engine container is
  always required. Splink has an in-process fallback (`splink_local.py`).
- manglekit is pinned in three places (`adapter.go`, `Dockerfile`,
  `tests/conftest.py`); if upstream moves, update all three and re-run parity.

---

## Roadmap

The MVP proves Splink + Muse. The plan from here is in
[docs/ROADMAP.md](docs/ROADMAP.md):

1. **Generic input** — identity roles instead of hardcoded field names,
   canonicalisation before clustering, no `entity_type == "user"` literal.
2. **Generic blocking** — rarest-token and n-gram strategies, hot-token limits
   as configuration, candidate-recall telemetry that fails loud on zero.
3. **Review and labels** — accept/reject decisions persisted in the ledger, so
   the system can learn what it got wrong.
4. **Rule induction** — rules mined from labels inside a closed vocabulary,
   promoted through shadow, canary and replay gates.
5. **Bounded, then full autonomy** — per-entity and per-rule modes, budgets,
   drift detection, rollback. Zero human actions per routine batch;
   auto-withhold stays a valid outcome.

---

## Repository layout

```text
mosaic-api/        FastAPI: routes/, publisher, ledger (SQLite), sub-agents, Meta client,
                   policy_profiles / policy_controller / policy_copilot
trust-engine/      Go service shelling out to the pinned mkit; policies/ (.json + .mg); tests
matching-engine/   Splink + DuckDB candidate service (proposer only, internal)
web/               Static cockpit: index.html + studio.js + prebuilt studio.css
                   (legacy/ is an archived previous UI, not served)
data/              mock/ fixtures and taxonomy, kaggle/ and olist/ samples,
                   strict-bcbs239/ retail-balanced/ analytical-mesh/ policy profiles
scripts/           eval_muse_scale.py, eval_hard_names.py, eval_muse_candidates.py,
                   eval_taxonomy_gap.py, Kaggle e2e harness, build_css.sh
tests/             contract, rules, hidden-connection, Splink, flows, auth, copilot, UI wiring
docs/              ARCHITECTURE.md (design record), ROADMAP.md (post-MVP plan),
                   TEST-REPORT.md (verified run); internal/ is local-only
```

Three services, fixed names: `mosaic-api`, `trust-engine`, `matching-engine`.
The design record is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Frontend

`web/` is static: `index.html` + `studio.js` + `studio.css`. Styling is a
committed Tailwind build; after changing markup or classes run
`scripts/build_css.sh` (needs Node). There is no runtime Tailwind CDN. Google
Fonts and Material Symbols still load from the network, so offline the icons
fall back to their text names. See [web/README.md](web/README.md) for the data
source behind each panel.

## Tests and CI

Full run, environment and what is *not* verified:
**[docs/TEST-REPORT.md](docs/TEST-REPORT.md)**. Last verified commit
`7721f95`: **285 passed**, ruff and black clean, `gofmt`/`go vet`/`go test`
clean.

```bash
python3 -m pytest -q                              # full suite, live trust-engine + matching-engine
ruff check mosaic-api matching-engine scripts tests shared
black --check mosaic-api matching-engine scripts tests shared
cd trust-engine && gofmt -l . && go vet ./... && go test ./...
```

CI (`.github/workflows/checks.yml`) runs a Python job (ruff, black, pytest) and
a Go job (gofmt, vet, test). The rule cases (G-01..G-21, P-01..P-06,
H-01..H-03, S-01..S-05) run through the real `mkit eval` path, never a mock. A
failing case is reported as a failure, never edited into a pass.

## Security and data boundary

- Demo records are synthetic fixtures labelled `-like`. Kaggle and Olist samples
  are generated locally and git-ignored. No real CRM, ERP or CIAM integration
  exists, and **no production accuracy claim is made**.
- The Meta key is server-side only: not in the repo, the frontend, fixtures or
  Compose constants. `.env` is ignored.
- The candidate judge receives names and addresses plus boolean hint flags —
  never phone numbers or emails.
- State-changing routes can be token-gated (`MOSAIC_API_TOKEN`). Without it,
  anyone who can reach the API can approve steward escalations, so set it for
  anything beyond local use.
- The ledger is append-only in effect: golden versions are kept, losers become
  tombstones, decisions are never updated in place.
- Unverified ticket output is logged as a claim and escalated on critical
  attributes; it is never published.

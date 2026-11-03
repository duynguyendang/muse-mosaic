# Muse Mosaic

> Many fragments, one trusted picture, with proof for every piece.

Muse Mosaic is a **trusted source for agents that act on fragmented data**.
The hard problem is not matching, it is the decision *after* matching: given
conflicting fragments about one person or product, may a downstream agent
(OTP, refund, search) act on a merged value right now, on what proof, and if
not, why is the proof missing?

Every answer has exactly one of two forms, and no third form exists:

1. a **golden**: one versioned record per entity, where every field carries
   its winner, its rule, its policy version, its taxonomy version and its full
   lineage; or
2. **`No trusted value`**: the request is **held**, with a reason, an owner,
   a deadline and a fallback recorded, and downstream is told to stop.

Mosaic **classifies; it does not resolve.** It sorts every cluster into
certified, held or denied and hands the held ones to whoever owns data
governance in your organisation. The cockpit has no steward workflow.

## Why this is hard

A customer is rarely one row. The same person shows up as a CRM record, a few
orders, a support ticket and a catalogue-linked purchase, each with its own id
and its own idea of the phone number. Downstream agents (OTP, refund, search)
then act on whichever copy they happened to read.

The first obstacle is **scale, not judgement**. Finding which records belong
together means comparing every record with every other: `n(n-1)/2` pairs. Our
own small Customer 360 sample, 2,554 documents, is already about 3.3 million
pairs. Real portfolios hold millions of records, so the pair count runs into the
trillions. No person or team can review that, and nobody can eyeball near
duplicates across it in a useful time.

So the work is split by what each part is good at:

- **Splink narrows the search.** It blocks on identifiers from the policy
  (email, phone, order id), so only plausible pairs are scored, then groups the
  connected ones into candidate clusters. That turns an impossible search into
  one a machine finishes in seconds.
- **Muse finds what exact matching misses.** A customer whose phone *and*
  email changed shares no identifier with their old record, so Splink leaves
  them as two clusters and the system would certify two goldens for one person.
  A cheap blocking step shortlists the few cluster pairs that look alike, and
  Muse judges only those from name and address (no email or phone is sent). See
  [Muse candidate matching](#muse-candidate-matching).
- **Muse reads inside each cluster.** Its five sub-agents look at the
  fragments of one cluster and propose links, values and taxonomy mappings,
  with a reason for each.
- **The engine decides.** Neither proposal publishes. The Datalog engine
  verifies each link and derives each value, and whatever it cannot settle is
  held rather than guessed.

The point of the AI is to make the search small enough to trust, not to make
the decision.

## How it works

```text
fragments ──► matching-engine (Splink)  proposes candidate clusters
          ──► five Muse sub-agents       propose links, values, taxonomy terms
                       │  (claims only; none of them can publish)
                       ▼
              trust-engine (Go + manglekit Datalog)   the only thing that derives
                       │
                       ▼
              mosaic-api publisher   validates the WHOLE document, then writes
                       │
              golden ─────────────┴───────────── held / denied ► handed off
```

- **Proposers never publish.** Splink and the Muse sub-agents emit claims.
  Only the deterministic Datalog engine derives, and only the publisher writes,
  and only after the assembled document validates against the expected schema.
- **Mosaic classifies, it does not resolve.** A held cluster stays held until
  someone outside the cockpit settles it. The API still has optional steward
  endpoints (approve, reject, split, accept) for that hand-off. A decision there
  is a new fact or taxonomy version plus an explicit re-derive, never a hand
  edit, and the cockpit does not use them.
- **Fail closed everywhere a guess could leak in.** Engine down, bad response,
  unknown policy, rank contradicting the matrix, broken link chain: deny or
  escalate, never publish.

Three services, fixed names: `mosaic-api`, `trust-engine`, `matching-engine`.
The design record is [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Policy profiles

A policy is a pair of manglekit files (`.json` metadata + `.mg` Horn clauses).
Three profiles ship under `data/<profile>/policies/`, and switching between
them re-runs only Stage 03/04 from cached Stage 02 clusters:

| Profile | Intent |
| --- | --- |
| `strict_bcbs239` | Zero leakage: any winner disagreement quarantines the cluster. |
| `retail_balanced` | Source hierarchy (ERP 3 > order/OTP 2 > CRM 1) plus freshness. Only cross-system disputes quarantine. |
| `analytical_mesh` | Multi-valued lineage for feature stores. No quarantine rule exists, so zero-quarantine holds by construction. Output is unattested, so OTP and the BCBS 239 dossier are gated in the UI. |

Cluster buckets are **disjoint**: `certified + held + denied = clusters`. A
cluster with some attributes published and some held counts once, as *held*,
and is reported additionally as a *partial* golden.

Measured on the bundled Customer 360 sample (247 clusters), as of the last run:

| Profile | Certified | Held (of which partial) | Denied |
| --- | --- | --- | --- |
| `strict_bcbs239` | 2 | 244 (76) | 1 |
| `retail_balanced` | 242 | 4 (4) | 1 |
| `analytical_mesh` | 247 | 0 | 0 |

Rerun on your data before quoting any figure: these depend on the dataset and
the profile files.

### Muse candidate matching

Opt-in (`MUSE_CANDIDATE_MATCHING=true`, off by default because it changes
cluster counts). After Splink clusters on exact identifiers:

1. **Shortlist, no model.** Blocking on name tokens and on *near* identifiers
   (same last 9 phone digits, an email one typo away), plus cheap string
   scores, picks a bounded set of cluster pairs (200 by default). Judging every
   pair is O(n²); this is what makes the search feasible.
2. **Code computes identifier evidence.** `identifier_hints` marks pairs whose
   phone or email is nearly, but not exactly, the same. The model is told a
   hint exists, never the phone or email itself: no contact identifiers leave
   the process.
3. **Muse judges the shortlist** from display name and address (plus the hint
   flags): same / different / unsure, a confidence and a reason. Strict
   schema; invalid output or an unknown pair rejects the whole batch.
4. **Only `same` with `high` confidence merges** two clusters (capped at 20
   documents). The link is tagged `semantic_verified` when code found
   identifier evidence, else `semantic_only`.

Identifiers are also **normalised before blocking** with the same function the
facts get at ingest, so `(449) 977-1729` and `449.977.1729` cluster together
without any model.

A merge is a *proposal to cluster*, not a verified identity. What the engine
does with it depends on the evidence; see
[Semantic matching rules](#semantic-matching-rules-engine). If Muse is down the
step is skipped and labelled in the build summary
(`muse_candidates.skipped_reason`).

**Measured** with `scripts/eval_muse_candidates.py`: 100 real Kaggle Customer
360 customers, each given 2 to 4 labelled variants (282 in all), plus 25
same-name-different-person distractors. The Kaggle sample itself has no
near-duplicates, so the variants are injected. Each variant is scored on whether
it lands in the same cluster as its origin customer.

| Arm | `format` (same digits, other format) | `country_code` (+1, email typo) | `moved` (new phone and email) | All (282) | Clusters containing two people |
| --- | --- | --- | --- | --- | --- |
| A. Exact blocking, raw values | 0 / 95 | 0 / 92 | 0 / 95 | 0 | 0 |
| B. Exact blocking, normalised (no model) | 95 / 95 | 0 / 92 | 0 / 95 | 95 | 0 |
| C. B + candidates, deterministic judge | 95 / 95 | 78 / 92 | 52 / 95 | 225 | 1 |
| C. B + candidates, Muse (`muse-spark-1.3-contributor`) | 95 / 95 | 78 / 92 | 54 / 95 | 227 | 0 |

What this does and does not show:

- Normalising identifiers recovers every `format` variant with no model.
- Most of the rest is recovered by the **shortlist and code-computed hints**.
  Muse found 2 more records than the deterministic judge and avoided its one
  false merge ("K. Brown" vs "L. Brown"), but on this benchmark a simple
  threshold gets most of the way. The hard-name benchmark below is where the
  judge matters.
- The variants were written by us (typos, case, word order, abbreviations,
  accents, new identifiers). The numbers show the mechanism works, not how it
  performs on real fragmented data.
- The shortlist is capped at 200 pairs and saw the cap here; recall is bounded
  by it.
- End to end through `/build` on a 428-record Kaggle subset (25 customers, 72
  variants), turning the feature on merged 31 cluster pairs (96 to 65 clusters)
  and raised conflicts from 50 to 133: merged people are held, not certified.

#### Hard names: where a model judge differs from string rules

The first benchmark used typos and case, which string similarity handles. A
second one, `scripts/eval_hard_names.py`, uses what it does not: nicknames
(Robert / Bob), initials (`R. Brown`), surname-first order, dropped middle names,
Vietnamese word order and diacritics, city aliases (`TP.HCM` / `Sài Gòn`), and
**hard negatives**: a spouse at the same address, the same name at another
address. It judges labelled pairs directly (so it measures judgement, not the
shortlist) and sends only name and address, as the product does.

479 pairs: 319 same person, 160 different. English persons are real Kaggle
Customer 360 customers with name variants written by us; the Vietnamese set is
synthetic (20 people). Same pairs, same rule for every judge: merge on `same`.

| Judge | Merged right (of 319) | False merges (of 160) | Precision | Recall |
| --- | --- | --- | --- | --- |
| String-similarity rules (`mock_judge`) | 203 | 3 | 99% | 64% |
| Rules that know every nickname in the test ("oracle") | 310 | 0 | 100% | 97% |
| Muse, merging at `high` confidence (product default) | 279 | 0 | 100% | 87% |
| Muse, merging at `medium` or `high` | 318 | 0 | 100% | 100% |

Reading it honestly:

- **Muse clearly beats plain string rules** on the hard variants: initials
  7/60 correct vs 51/60 (60/60 at `medium`), Vietnamese given-name-first 0/20 vs
  4/20 (20/20 at `medium`). It also got every household and same-name-other-
  address negative right: it answered `different` for a spouse and `unsure` for
  a namesake, and never `same`.
- **The "oracle" is not a fair real-world baseline.** It is hand-built from the
  exact nickname table that generated the test, so it is an upper bound for a
  dictionary approach. A rule set you maintain yourself would need to cover
  nicknames, name orders and city aliases in every language you see; the model
  needs none of that, but costs about 0.75 s per pair against microseconds.
- **The default threshold leaves recall on the table.** Muse answered `same` for
  318 of 319 true pairs, but 39 at `medium` confidence, which the default rule
  drops. `MUSE_CANDIDATE_MIN_CONFIDENCE=medium` merges them. That gave 0 false
  merges here, but 160 easy-to-moderate negatives is a small sample with no
  lookalike relatives or twins, so the default stays `high` until you have
  measured your own data. A merge is still only a proposal: without
  engine-verifiable identifier evidence it cannot certify a value.
- One live run, one model; verdicts can differ between runs. Rescore a saved run
  without API calls with `--dump` and `--from-dump`.

### Semantic matching rules (engine)

The trust-engine verifies a semantic claim instead of trusting it. Datalog
cannot compute string distance, so the engine computes **mechanical features**
per fact (like it turns `event_time` into minutes) and the policy decides:

| Feature (`trust-engine/semantic.go`) | Meaning |
| --- | --- |
| `phone_key` | last 9 digits of the phone, extension dropped |
| `email_dels` | the email plus its local part with each single character deleted. Two emails on the same domain share a key exactly when they are within edit distance 1. Locals under 8 characters emit none. |

| Rule | What it does |
| --- | --- |
| **S1** `semantic_phone` | a link between two phone facts is a verified hop when their `phone_key`s are equal (`+1 449…` vs `(449) …`) |
| **S2** `semantic_email` | a link between two email facts is a verified hop when they share a deletion key (one typo) |
| **S3** guard | a cluster carrying a names-only `fuzzy_name_address` claim escalates any winner that is still cut off from the anchor (`SEMANTIC_MERGE_UNVERIFIED`) |

So the three tiers are:

1. **Verified semantic hop (S1/S2).** Grounds the chain like an exact hop. The
   merged person publishes normally: freshness and authority decide between
   differing values, exactly as for any exact-identifier cluster.
2. **Names only (`semantic_only`).** No hop, so the new values cannot win. They
   are held; the base values are never silently replaced.
3. **No claim.** Nothing changes.

Muse's judgement that the *names* are compatible is still the proposer's. What
the engine verifies is the identifier evidence, never a model's say-so. A claim
with no matching evidence, on the wrong attribute, with two edits, or with a
short local part verifies nothing (all covered in `tests/test_semantic_rules.py`
against the live engine).

Policy files: `pol-2026-11-02-v1` is a new user policy, additive over
`pol-2026-10-28-v1` (matrix unchanged); the older versions stay immutable and
ignore semantic claims. The three profile policies carry the same blocks, copied
verbatim from `trust-engine/policies/semantic/*.mg` by
`scripts/apply_semantic_block.py`; a test fails if a copy drifts.
`analytical_mesh` takes S1/S2 but not S3, because it has no escalation rules by
design.

**End to end through `/build` on Kaggle** (25 real customers, 70 to 78 labelled
later-dated variants each, mock Muse for the sub-agents, `pol-kaggle`; identity
attributes are name, phone, email and address, because `order_id` conflicts are
a baseline of that policy and are identical with the feature on or off):

| Variant kind | Clusters merged | Tier | Identity attributes held in merged clusters | Variant values certified inside merged clusters |
| --- | --- | --- | --- | --- |
| `country_code` (+1 prefix, email typo) | 20 | `semantic_verified` | 0 | 40 |
| `moved` (new phone and email) | 29 | `semantic_only` | 29 (all) | 0 |

Verified evidence promotes the newer value; names-only evidence promotes
nothing.

### Evidence-verified taxonomy extension

A categorical value the taxonomy does not contain (`clo`, `3l3ctronics`,
`Foldable Phone`) makes the engine hold `category` (rule T1). Opt-in
(`MUSE_TAXONOMY_PROPOSALS=rules|muse`, off by default) extends the taxonomy
before deriving. A proposer, rules or Muse, only **proposes**; each proposal is
accepted for the part the data verifies (`taxonomy_proposals.py`):

| Check | Meaning |
| --- | --- |
| ground | every cited synonym is a value that really occurs |
| attest | the canonical term is an existing term, or occurs verbatim with at least `TAXONOMY_MIN_SUPPORT` records, so it is not an invented name |
| not a variant | a "new" term that is only a prefix, typo or case variant of another term is refused (`clo` is not a category) |
| relate | each synonym relates to the term by case, punctuation or leet folding, a unique prefix of 3+ letters, or one typo on a word of 5+ letters |
| unique | a synonym claimed by two terms is refused for both |

Anything unverifiable is not accepted, however plausible, and stays held.
Accepted terms are written as a **new** taxonomy version (`tax-product-evidence-<hash>`,
idempotent, never an edit) with an evidence log beside it, log first and the
taxonomy file atomically last. Each derived version records `parent_version` and
`created_at`, and the newest `created_at` is the default, so a later steward
version is never outranked by an evidence one. A model outage changes nothing.

**Measured** with `scripts/eval_taxonomy_gap.py` through `/build` and a live
engine. *Unified* = certified and grouped with its synonyms; *fragmented* = certified
but the class was split over several terms; *wrong* = merged into another class.

Kaggle Customer 360, the 389 real products (8 categories written 37 ways):

| Arm | Unified | Fragmented | Wrong | Held | Terms added |
| --- | --- | --- | --- | --- | --- |
| A. Fixed taxonomy | 0 | 0 | 0 | 389 (100%) | 0 |
| B. Rules + evidence check (no model) | 389 (100%) | 0 | 0 | 0 | 8 |
| C. Muse + evidence check | 389 (100%) | 0 | 0 | 0 | 8 |
| D. Muse, no check (risk only) | 389 (100%) | 0 | 0 | 0 | 8 |

Synthetic semantic set (88 records, 6 classes: Galaxy Fold, `điện thoại gập`, iPad
Pro, Surface Pro ...), one run:

| Arm | Unified | Fragmented | Wrong | Held | Terms added |
| --- | --- | --- | --- | --- | --- |
| A. Fixed taxonomy | 21 (24%) | 0 | 0 | 67 (76%) | 0 |
| B. Rules + evidence check | 58 (66%) | 28 (32%) | 0 | 2 (2%) | 11 |
| C. Muse + evidence check | 58 (66%) | 0 | 0 | 30 (34%) | 3 |
| D. Muse, no check (risk only) | 56 (64%) | 0 | 0 | 32 (36%) | 3 |

What this does and does not show:

- **On the real Kaggle data Muse adds nothing.** The dirt is string-level (case,
  leet-speak, truncation), so rules plus the evidence check reach 100% alone, and
  Muse's proposals were all accepted and identical.
- **The taxonomy gap is real and large:** a fixed taxonomy certifies none of the
  389 products' categories.
- **Where Muse helps is consolidation, not coverage.** On the semantic set the rule
  proposer certifies 86 records but splits them over 11 terms (`iPhone 14`,
  `Galaxy Fold 5` each become a "category"); Muse produced 3 clean terms. Both
  unify 58 records.
- **The check refuses what it cannot verify.** `điện thoại gập` = `Foldable Phone`
  is right, but no rule can verify it, so those records stay held (34%).
  Accepting them unchecked (arm D) gained about 6 records in an earlier run and
  nothing in this one; the model's proposals vary between runs, and neither
  produced a wrong merge here. The check costs coverage and buys a guarantee that
  the model's say-so never creates vocabulary the engine certifies.
- The Kaggle labels were assigned by hand; the semantic set is small, synthetic
  and written by us. It shows the mechanism and the trade-off, not field accuracy.

### Policy Copilot

`POST /api/v1/pipeline/copilot/draft` turns an English instruction into a
**draft** policy change. It keeps the same rule as the rest of the system:

1. A proposer (a small deterministic parser in mock mode, the Meta model in
   live mode) may only fill a **closed vocabulary**: set a source's trust tier
   (1 to 3) or mark an attribute multivalued. Anything else, such as time
   windows or new rule logic, is refused with a reason.
2. Fixed templates compile the change into Horn text. The model never writes
   Datalog.
3. `mkit` decides whether the result is a valid program. If no engine is
   reachable the draft is `unverified`, never `compiled`.

Nothing is staged, applied or published from a draft.

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
- Taxonomy lives in a volume shared by both services. A taxonomy Accept (optional steward API) writes
  a new version there and the engine hot-reloads it. Policy files reload on
  `SIGHUP`, and a failed reload keeps the previous policies. Never bind-mount
  the repo taxonomy directory: Accept writes would pollute the repo.

### Live Muse mode

Set in `.env` (git-ignored, never committed):

```text
USE_MOCK_MUSE=false
META_MODEL_API_KEY=...        # server-side only
MUSE_MODEL_ID=...             # no default in code; startup fails fast if empty
```

`META_MODEL_API_BASE_URL` defaults to `https://api.meta.ai/v1`. Each agent call
uses a strict `json_schema` (every object needs `additionalProperties: false`),
bounded retry with backoff, and a circuit breaker; every field is
Pydantic-validated and an invalid proposal is rejected whole. Any remaining
failure fails closed (`PROPOSER_UNAVAILABLE`, 503, nothing published).
`GET /health` reports `proposer_mode` and the model id, and the cockpit shows
them on Stage 01. Optional: `WONDER_MODEL_ID` enables Wonder adjudication on
escalations; unset means a labelled stub, never a simulated one.

### Configuration

| Variable | Purpose |
| --- | --- |
| `USE_MOCK_MUSE` | `true`: deterministic local agents. `false`: Meta Model API. |
| `META_MODEL_API_KEY`, `MUSE_MODEL_ID`, `META_MODEL_API_BASE_URL` | Live mode credentials and model. |
| `TRUST_ENGINE_URL`, `MANGLEKIT_BIN` | Where to derive; which `mkit` to run. |
| `MATCHING_ENGINE_URL`, `SPLINK_ENABLED` | Splink service; empty URL runs the in-process matcher. |
| `DATABASE_PATH`, `TAXONOMY_DIR` | Ledger and taxonomy locations. |
| `MOSAIC_API_TOKEN` | If set, every POST/PUT/PATCH/DELETE needs `Authorization: Bearer <token>`. Reads stay open. Unset = open (local dev only; a warning is logged). |
| `MUSE_CANDIDATE_MATCHING` | `true`: Muse proposes near-match cluster merges (see above). Default `false`. |
| `MUSE_TAXONOMY_PROPOSALS` | `off` (default), `rules` (no model) or `muse`: extend the product taxonomy with evidence-verified terms before deriving. |
| `TAXONOMY_MIN_SUPPORT` | records a new term needs behind it (default 3). |
| `MUSE_CANDIDATE_MIN_CONFIDENCE` | `high` (default) or `medium`: lowest judge confidence that may merge. |
| `EMBEDDING_MODEL/VERSION/DIM` | Empty by default; similar search runs structured-only (`vector_recall: skipped`). |

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

The trust-engine rejects unknown entity types, policy or taxonomy versions,
disallowed attributes, sources that contradict their matrix rank, bad links,
missing provenance, naive datetimes, bad TTLs, out-of-bound numbers, duplicate
fact ids and any unknown JSON field, including an `embedding` field. That last
rejection is structural: embeddings must never become derivation inputs.

## Contract guardrails

Enforced in code and tests; do not relax:

1. **No fact without provenance.** `source`, `source_id`, `authority_rank`,
   timezone-aware `event_time`, `verified` and `ttl_seconds` are required.
2. **No golden without proof.** Winner, rule, policy version, taxonomy version
   and lineage are required.
3. **No embedding in derivation.**
4. **No policy or taxonomy guess.** Unknown means deny or escalate.
5. **No proposer publishes.** Muse and Splink output is validated and never
   written directly.
6. **No unverified link publishes.** Only sole-proof hops verified against live
   facts count; conflicts escalate (C1); a broken chain escalates the cut-off
   side (C2).
7. **No unvalidated golden.** The whole nested document must validate, or the
   golden is withheld (`STRUCTURE_MISMATCH`).
8. **No new term without an explicit Accept, or verifiable evidence.** Accept is
   single-use and bound to one exact action and version. The opt-in
   [taxonomy extension](#evidence-verified-taxonomy-extension) accepts a term only
   for what the data verifies, never on a model's confidence.
9. **Replay is exact.** Same key and hash never re-derives; nearest-neighbour is
   never identity.

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
scripts/           sample builders, Kaggle e2e harness, build_css.sh
tests/             contract, rules, hidden-connection, Splink, flows, auth, copilot, UI wiring
docs/              ARCHITECTURE.md (design record); internal/ is local-only
```

## Frontend

`web/` is static: `index.html` + `studio.js` + `studio.css`. Styling is a
committed Tailwind build; after changing markup or classes run
`scripts/build_css.sh` (needs Node). There is no runtime Tailwind CDN. Google
Fonts and Material Symbols still load from the network, so offline the icons
fall back to their text names. See [web/README.md](web/README.md) for the data
source behind each panel.

## Tests and CI

```bash
python3 -m pytest -q                              # ~200 tests, live trust-engine + matching-engine
ruff check mosaic-api matching-engine scripts tests shared
black --check mosaic-api matching-engine scripts tests shared
cd trust-engine && gofmt -l . && go vet ./... && go test ./...
```

CI (`.github/workflows/checks.yml`) runs the Python job (ruff, black, pytest)
and a Go job (gofmt, vet, test). The rule cases (G-01..G-21, P-01..P-06,
H-01..H-03, S-01..S-05) run through the real `mkit eval` path, never a mock. A
failing case is reported as a failure, never edited into a pass.

## Security and data boundary

- The demo records are synthetic fixtures labelled `-like`. The Kaggle and
  Olist samples are generated locally and git-ignored. No real CRM, ERP or CIAM
  integration exists, and **no production accuracy claim is made**.
- The Meta key is server-side only: not in the repo, the frontend, fixtures or
  Compose constants. `.env` is ignored.
- State-changing routes can be token-gated (`MOSAIC_API_TOKEN`). Without it,
  anyone who can reach the API can approve steward escalations, so set it for
  anything beyond local use.
- The ledger is append-only in effect: golden versions are kept, losers become
  tombstones, decisions are never updated in place.
- Unverified ticket output is logged as a claim and escalated on critical
  attributes; it is never published.

## Known limits

- **Muse candidate matching is opt-in and judges name and address only.** It
  cannot link records whose names and addresses both differ, and the shortlist
  can miss pairs (2 of 40 in our test). It runs in `/build` and jobs; the
  cockpit has no button for it.
- **Semantic evidence is narrow on purpose.** S1 treats equal last-9 digits as
  the same phone, so a shared household or office line can verify a merge that
  Muse's name check must then reject; S2 covers one typo on a local part of 8+
  characters. Transpositions, two typos, domain typos and abbreviations of a
  name are not evidence, so they stay `semantic_only`.
- **The taxonomy verifier is permissive about frequent strings.** Any value that
  occurs verbatim at least `TAXONOMY_MIN_SUPPORT` times can become a term, so a
  rules-only run can mint `iPhone 14` as a category. Support counts distinct
  entities (`product_ref`, else the document), so copies of one product do not
  add up, but distinct documents with no `product_ref` each count. Muse
  consolidates better here, but the check itself would still accept a frequent
  unrelated string it proposes.
- **Evidence-extension numbers predate the digit fix.** Digits are now kept in
  matching (`iPhone 13` is not `iPhone 14`), which can lower the rules-only
  arm's term count on the semantic set. Rerun `scripts/eval_taxonomy_gap.py
  --dataset semantic --arms A B C D`; it now also prints `vocab` (certified
  terms per true class, 1.0 is clean) before trusting those two tables.
- **Co-location chaining in ordinary `/build` clusters.** For a cluster of
  several documents that Muse did not merge, `publisher._batch_links` chains
  every fact with `same_fragment`, which grounds all facts and means the
  weakest-link rule (C2) cannot fire there. Muse-merged clusters chain only
  inside their original parts, which is what makes S3 effective. The behaviour
  for ordinary clusters is unchanged and worth revisiting.
- Splink is proven on synthetic and sample data with deterministic exact-identifier
  linkage plus best-effort EM weights. No accuracy or massive-scale benchmark is claimed.
- Wonder and Prompt Guard are conditional: labelled stubs until a model id or
  connector is pinned, and they must not be claimed otherwise.
- A Copilot draft cannot yet be staged as a live profile; that needs profile
  staging and an engine reload.
- There is no Python fallback for derive, so the trust-engine container is
  always required. Splink has an in-process fallback (`splink_local.py`).
- manglekit is pinned in three places (`adapter.go`, `Dockerfile`,
  `tests/conftest.py`). If upstream moves, update all three and re-run parity.
- The mangle dialect needs care: zero-arity predicates, `==` against constants
  and unbound negated variables are rejected, and numbers at or above 1e6 are
  rejected fail-closed. These bounds are tested (G-20) and noted in the `.mg` headers.

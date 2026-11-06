# Roadmap: from a proven MVP to generic, autonomous record linkage

Status: draft for review · Last updated: 2026-11-03

The MVP proved one thing with numbers: on the same candidate pairs, string rules
recover 655/1,000 labelled links and make 116 false merges, while Muse recovers
984/1,000 at `high` (990/1,000 at `medium`) with 0 false merges. That claim is
scoped to the synthetic benchmark in `README.md` ("Does the judge matter at
scale?"). It does not yet claim that the pipeline generalises to another schema,
another script, or another entity type.

This roadmap turns that MVP into a product that does, and then removes the human
from the routine loop without weakening the trust boundary.

## 0. Where we are

**G1-lite and G2-lite landed on branch `g1-g2-slice`** (2026-11-03): identity
fields map through taxonomy aliases before clustering, a read-only mapping panel
shows what was found, `MUSE_BLOCKING_MODE=rarest_pair` blocks on token pairs and
address, and a zero-candidate build now reports a warning. Not done: editable
per-dataset mapping, entity-type registry, phonetic / n-gram blocking,
calibration on a second dataset. Vietnamese 10k candidate recall: 0/500 ->
481/500 with `rarest_pair`; the live Muse arm has not been rerun under it.

**M3 (autonomy controller v1) landed on `main` (2026-11-05).** The ladder is
real per entity type: `AUTONOMY_MODE` (build-wide) and
`AUTONOMY_MODE_<ENTITY>` choose off/shadow/suggest/auto; **the default is
`off`** (fail-closed). `shadow`/`suggest` judge and report without merging;
`auto` applies as before. A process-local kill switch
(`/api/v1/autonomy/freeze`) demotes auto -> shadow without stopping reads;
breakers (zero candidates at scale, judge failure > 10%, entity run failed)
**enforce**: `jobs.execute_full_build` calls `autonomy.enforce_breakers`
after clustering and before any golden is derived — a tripped breaker stops
the build (jobs land `failed`, never retried; the sync `/build` path returns
409 with the breaker detail). The summary keeps the (now always-empty)
breaker list for post-mortem compatibility; a deterministic 1% audit sample
plus all withheld decisions is reported. **Evidence discipline:** measured
claims re-run and logged under `docs/runs/` (see
`docs/runs/20261106T020340Z-deepmatcher-all-mock-05e1db3cc4fa.json`); the
live Muse rows are now logged too (SHA `556c8df`, model
`muse-spark-1.3-contributor`, `docs/runs/20261106T044952Z-calibration-all-live-556c8dfef636.json`).
`AUTONOMY_MODE=auto` refuses to start without a witnessed floor
(`MUSE_AUTO_WITNESS`, verified in `mosaic-api/auto_witness.py`). Integration tests pin the default, shadow, freeze and deliberate
off paths end to end. Not landed: *meeting* the live precision floor — the
2026-11-06 live witness run measured 42.6% (Amazon-Google) / 92.1%
(DBLP-ACM) precision at `high`, below the 99.5% requirement, so its artifact
does not unlock `MUSE_AUTO_WITNESS` and auto stays refused (the mock judge
over-merges and is never a witness — see `scripts/eval_calibration.py`).
The review queue for merge proposals (G4) is **landed**: `suggest` builds persist
would-merge pairs to `merge_proposals`, labelled accepted/rejected at
`/api/v1/suggestions` — labels are recorded, never applied to goldens. Canary
auto-apply is in (`AUTONOMY_CANARY_RATE`): only a deterministic hash slice of
pairs is applied, the rest are held for the same queue.

**M0–M2 (zero-touch onboarding slice) landed on `main` (2026-11-05).** Entity
schema contracts are versioned data (`data/schemas/*.json`, loaded and
fail-closed validated at startup), policy and expected-structure loaders are
generic (no `("user","product")` hard-code; `jobs.py` dataset hacks removed),
and the Go trust-engine discovers entity policy dirs the same way (it no
longer auto-loads staged dataset policies; they compose via `POLICY_DIR`).
`scripts/onboard.py` derives a self-contained bundle from just **sources +
expected result** — schema mapping (deterministic aliases first, then a
mock/live proposer that may only *propose*; anything identity-shaped that
maps nowhere refuses loudly), a bootstrap taxonomy from observed values, and
an engine-validated policy (observed mode ranks; strict conflicts) — and the
build then runs against it via `--bundle`. DeepMatcher Amazon-Google and
DBLP-ACM (4,589 / 4,910 raw rows, zero model calls under the mock judge)
derive and run end-to-end with deterministic replay. Not yet: an HTTP
onboarding endpoint, shadow/canary promotion (M3/M4), gold-pair score cards
and the release gate (M5).

Working today:

- Splink exact-identifier blocking, a Muse semantic judge on shortlisted pairs,
  deterministic merge application, verdict cache, per-cluster cost control.
- A trust engine that derives and publishes, and refuses when proof is missing.
- A ledger, steward items, a policy store, a taxonomy store, and a Policy
  Copilot that can draft a policy change inside a closed vocabulary.
- A labelled 10k benchmark with four arms (exact, rules, Muse, oracle) and a
  100k rules/oracle run.

Known coupling (the audit behind this roadmap):

| Coupling | Effect today |
| --- | --- |
| Field names `display_name` / `address` / `phone` / `email` | Another schema yields empty profiles and 0 candidates |
| `entity_type == "user"` only | Product / organisation records never reach Muse matching |
| Aliases applied at fragment level, after clustering | A `full_name` alias does not rescue candidate generation |
| Tokens shorter than 3 chars dropped; tokens in >50 clusters drop blocking | Vietnamese names at 10k produce **0 candidate pairs** |
| `skeleton()` keeps `[a-z0-9]` only | CJK / Cyrillic / Arabic taxonomy terms cannot be verified |
| Taxonomy extension hardcodes product + `category` | Other taxonomies cannot be extended |

The target is not "remove every constant". It is: **a new data source is
onboarded by configuration and evidence, not by editing Python**, and the system
can keep running correctly when nobody is watching.

## 1. North star, and what "100% autonomous" means

**North star.** Upload any dataset with an identity-shaped entity, confirm (once)
what the fields mean, and get either verified links and golden records with
proof, or a clean refusal. No per-batch human click. No silent guess.

**100% autonomous, defined.** A routine batch completes with zero human
actions, and every decision is one of:

1. auto-applied, above the calibrated threshold, with proof in the ledger;
2. auto-withheld / quarantined, with a machine-readable reason; or
3. auto-resolved by an evidence-backed rule that was itself promoted by a gate.

Humans still own policy, budgets and audits. They do not own individual merges.
"Autonomous" never means "publishes without proof": the trust engine stays the
decider and stays fail-closed.

**Non-goals.**

- A model writing Datalog or arbitrary code. Models may only fill a closed,
  versioned rule vocabulary; templates compile it and `mkit` validates it.
- Auto-publishing a critical field on a low-confidence claim.
- Removing the steward concept from the architecture. It becomes an audit and
  exception surface, not a per-run queue.
- Claiming 100k live numbers before they are run.

### Autonomy ladder

| Level | Name | Human role | Merge policy | Rule policy |
| --- | --- | --- | --- | --- |
| A0 | Manual (today) | Runs the build, approves config | Opt-in, config-gated | Hand-written |
| A1 | Review loop | Accepts / rejects proposed links and terms | Auto only for exact proof; everything else queued | Hand-written |
| A2 | Rule-assisted | Approves a rule once, then it runs | Approved rules auto-apply; new cases queued | Induced rules run in shadow, human promotes |
| A3 | Bounded autonomy | Handles exceptions and spot audits | Auto above threshold; exceptions auto-withheld | Rules auto-promote if gates pass, canary first |
| A4 | Full autonomy | Sets policy, audits samples, owns rollback | Zero per-batch actions; auto-withhold on doubt | Continuous induction, auto-rollback on drift |

The ladder is per entity type and per rule, not a global switch. Product can be
at A2 while user is at A4.

## 2. Guardrails that do not change

1. **Proposer ≠ decider.** Splink, Muse, the rule miner and the Copilot propose.
   The trust engine derives. The publisher validates the whole structure.
2. **Closed vocabulary.** Every automated change is a structured value from a
   known set (tier, threshold, blocking key, normalizer, comparison, synonym).
   No free-form logic, no model-authored Datalog.
3. **Evidence before action.** A merge carries its pair evidence; a rule carries
   support, counterexamples and a shadow replay; a taxonomy term carries support
   and a verdict.
4. **Version everything.** Policy, taxonomy, prompt, rule set and threshold each
   have a version; every decision records them.
5. **Fail closed, and fail loud.** Zero candidates at scale is an incident, not a
   quiet no-op. A missing model is `Muse unavailable`, never a silent rules
   downgrade without a label.
6. **No claim without a run.** Every release note states which dataset, arm and
   date produced each number.

## 3. Workstreams

Each workstream has a measurable exit criterion. Nothing is "done" because the
code merges; it is done when the number moves on a named dataset.

### G1 — Generic data contract

**Problem.** Candidate generation reads four hardcoded field names and only
`entity_type == "user"`.

**Deliverables.**

- Identity roles: `name`, `address`, `phone`, `email`, `identifier`, declared per
  entity type in the taxonomy or in settings.
- Schema inference on upload: map raw fields to roles, show the proposal once,
  store the confirmed mapping as a versioned profile. Reuse the Copilot's
  draft/confirm pattern.
- Canonicalise documents **before** clustering, so blocking, the judge and the
  engine all see the same field names.
- Entity-type registry; remove the `"user"` literal from the matching path.

**Exit.** A dataset using `full_name` / `address_line` / `mobile` / `email_address`
links with configuration only. Three schemas pass the same test with no Python
edit.

**Touchpoints.** `mosaic-api/ingest.py`, `muse_candidates.py` (`cluster_profile`,
`cluster_identifiers`), `routes/build.py`, `config.py`, `schemas.py`.

### G2 — Generic blocking and scale

**Problem.** Tokens shorter than 3 characters are dropped, tokens in more than 50
clusters block nothing, and there is no strategy for scripts without spaces.
Vietnamese names at 10k currently return 0 candidates.

**Deliverables.**

- Pluggable blocking strategies per entity type and field role: rarest-token,
  token-pair, address-number, phonetic key, character n-gram (CJK), and
  identifier neighbourhood (already present).
- Stop dropping hot tokens outright: choose the rarest tokens per cluster and
  bound comparisons per cluster deterministically.
- Candidate-recall telemetry in every build report: pairs considered, pairs
  blocked, pairs judged, and a warning when a blocking strategy yields zero
  pairs at scale.
- Config: hot-token limit, per-cluster cap, strategy order, all env-driven.

**Exit.** Vietnamese-name 10k gold set: candidate recall >= 90% (today 0/500).
100k: judged pairs within budget, no silent zero, oracle recall >= 99%.

**Touchpoints.** `muse_candidates.py` (`shortlist`, `COMMON_TOKEN_CLUSTERS`),
new `blocking.py`, `config.py`, `scripts/eval_muse_scale.py`.

### G3 — Generic judge

**Problem.** The prompt hardcodes "customer records ... display names and
addresses". It is a person judge, not a generic entity judge.

**Deliverables.**

- Prompt templates per entity type and field-role set: person, product,
  organisation. The judge sees role names, not raw column names.
- Abstain is a first-class verdict; `unsure` never merges.
- Threshold calibration per entity type and per source, on held-out data.
- Multi-field context (identifiers as hints, not proof) and multilingual
  evaluation (Vietnamese, English, CJK smoke tests).

**Exit.** Two entity types, two datasets: precision at the auto threshold
>= 99.5%, semantic recall >= 90%, 0 false merges on the namesake negatives.

**Touchpoints.** `muse_candidates.py` (`SYSTEM_PROMPT`, `JUDGE_SCHEMA`),
`config.py`, `scripts/eval_hard_names.py`, `scripts/eval_muse_scale.py`.

### G4 — Review loop and labels

**Problem.** There is a steward queue for field conflicts, but no review surface
for proposed links, and no persisted label when a human agrees or disagrees. The
system cannot learn what it got wrong.

**Deliverables.**

- Review queue for: proposed merges, proposed taxonomy terms, proposed rules,
  and auto-withheld decisions. Every card shows the evidence and the pair.
- Accept / reject / edit, with a reason code; decisions stored as labels keyed by
  a stable pair or term hash, in the ledger, replayable.
- Review sampling: all decisions until N labels, then uncertainty sampling
  (near-threshold, high-impact, disagreement between rules and judge).
- Metrics: review load per 1k records, agreement rate, time-to-decision,
  label coverage per entity type.
- UI: exception card, not a main panel; counts visible, queue collapsed.

**Exit.** Every auto decision traces to a label or to a rule with evidence.
Review load is measured, and a 10k batch produces a reusable label set.

**Touchpoints.** `ledger.py`, `analyser.py` (steward items), `routes/pipeline.py`,
new `review.py` and label tables, `web/`.

### G5 — Rule induction and lifecycle

**Problem.** Rules are hand-written. The system can propose taxonomy terms, but
not matching rules.

**Deliverables.**

- Closed rule vocabulary, compiled by templates:
  - `normalize`: phone E.164, address abbreviations, Vietnamese name order,
    diacritics, whitespace.
  - `block`: token set, token pair, prefix, phonetic key, n-gram, address number.
  - `compare`: exact, normalized, token-set, edit distance, nickname map,
    initials, transliteration.
  - `threshold`: per entity type, source and field role.
  - `synonym` / `alias`: taxonomy terms and attribute aliases.
- Miner: from G4 labels, propose rules with support, counterexamples and the
  expected lift. Example: "in 412 accepted pairs, `Nguyen` and `Nguyễn` never
  disagreed; normalize diacritics before blocking."
- Replay gate: run a candidate rule against the ledger's past decisions and a
  held-out slice. No promotion on training labels alone.
- Lifecycle: `draft -> shadow -> canary -> active -> retired`. Each state change
  is a policy version with evidence attached. Rollback is a pointer change.
- Copilot extension: an instruction such as "treat missing middle names as the
  same person" becomes a draft rule, not code.

**Exit.** At least three induced rules promoted with shadow precision >= 99.9%
and a measured recall lift; zero regressions on the replay set.

**Touchpoints.** `policy_copilot.py` (closed vocabulary, templates),
`policies.py`, `taxonomy_proposals.py`, new `rule_induction.py` and
`rule_store.py`.

### G6 — Autonomy controller

**Problem.** Autonomy today is a set of flags, not a controller with budgets,
canaries and rollback.

**Deliverables.**

- Modes per entity type and per rule: `off`, `shadow`, `suggest`, `auto`.
- Canary: a new rule or threshold runs on 1-5% of clusters first.
- Drift monitor: precision proxy (review agreement), false-merge reports,
  exception rate, candidate recall, cost. A drop beyond the budget stops auto
  mode and opens an incident, with the last good version restored.
- Budgets: tokens per job, per day, per entity type; hard stop, not a warning.
- Kill switch: one endpoint and one UI action that freezes auto-apply without
  stopping reads.
- Audit: sampled decisions, each replayable from the ledger.

**Exit.** Ten consecutive 10k batches with zero human actions; exception rate
< 1%; a rollback drill that restores the previous policy within one batch.

**Touchpoints.** `jobs.py`, `config.py`, `main.py`, new `autonomy.py`, `web/`.

### G7 — Evaluation and release gates

**Problem.** The current benchmark is one synthetic generator with deliberately
diverse surnames; it measures the judge, not generalisation.

**Deliverables.**

- Gold sets: the synthetic generator (judge), Kaggle Customer 360 (schema and
  scale), a Vietnamese-name set with realistic surname distribution (blocking),
  a product-taxonomy set (terms and extension).
- Per-source report: candidate recall, semantic recall, false merges,
  indistinguishable merges, review load, tokens, elapsed time.
- CI gate: a release fails if precision drops below the floor or candidate
  recall falls under the target on any gold set.
- Published benchmark policy: every number names dataset, arm, date and whether
  it was live or mock.

**Exit.** Every release ships with a table; no claim without a run. The 100k
Muse arm has a budgeted run and a published result.

**Touchpoints.** `scripts/eval_*.py`, `tests/`, CI config, `README.md`.

## 4. Phases

Estimates assume one engineer familiar with the codebase; they are for ordering,
not commitments.

| Phase | Duration | Workstreams | Exit gate |
| --- | --- | --- | --- |
| P0 — Freeze the MVP | 2-3 days | Commit scale work, document limits, tag the baseline | Clean tree; README states what is measured and what is not |
| P1 — Generic input and blocking | 2 weeks | G1, G2 | Schema swap without code; VN 10k candidate recall >= 90% |
| P2 — Labels and review | 2 weeks | G4, G7 (first gold sets) | 10k batch produces labels; review load measured |
| P3 — Rules from labels | 2-3 weeks | G5 | Three induced rules promoted with shadow evidence |
| P4 — Bounded autonomy | 2 weeks | G6 | A3 on user and product; rollback drill passes |
| P5 — Full autonomy and breadth | ongoing | G6, G3, G7, new entity types | 10 consecutive batches, zero human actions |

Sequencing matters: **G1 and G2 before any 100k live run**, because a bigger
model bill on a candidate set that can be empty is not evidence. G4 before G5,
because rule induction without labels is guesswork.

## 5. Metrics and release gates

Targets are initial; calibrate them on the first real dataset and record the
change.

| Metric | Target at A3 | Target at A4 | Where measured |
| --- | --- | --- | --- |
| False merges | 0 per 10k accepted merges | 0 per 10k, sampled audit | gold sets |
| Precision at auto threshold | >= 99.5% | >= 99.9% | per source |
| Semantic recall (hard pairs) | >= 90% | >= 95% | generator + real set |
| Candidate recall | >= 95% | >= 98% | gold pairs |
| Exception / review rate | < 1% of records | < 0.1%, auto-withheld | build report |
| Cost per 10k | within budget, cache hit > 80% on reruns | same | build report |
| Drift | < 0.5 pp precision drop | same, auto-rollback | rolling window |

A release is blocked by any of: precision below floor, candidate recall below
target, a silent zero-candidate build, or a rule active without shadow evidence.

## 6. Rule lifecycle, concretely

```
label / instruction
      |
      v
  draft rule  --(support >= N, counterexample rate <= e)-->  shadow
      |                                                        |
      |                                          replay + held-out precision
      |                                                        v
      |                                                     canary 1-5%
      |                                                        |
      |                                          drift <= 0.2 pp, no incident
      |                                                        v
      +---------------------------------------------------- active
                                                               |
                                              drift / incident v
                                                          rolled back
```

Rules are data, not code: a rule row holds its type, parameters, evidence,
version and state. The compiler turns it into the policy form the engine already
accepts, and `mkit` validates the result. A model may propose the row; only the
gate promotes it.

## 7. Autonomy controller, concretely

- **Per-scope switches.** `user: auto`, `product: suggest`, `rule:vn-name-order:
  canary`. A global "autonomous" badge is forbidden; the UI shows what is
  actually auto.
- **Budgets.** `MUSE_JOB_TOKEN_BUDGET`, `MUSE_DAILY_TOKEN_BUDGET`, max pairs per
  cluster, max judge calls per batch. Exceeding a budget stops the batch and
  labels it; it never silently truncates.
- **Circuit breakers.** Zero candidates at scale, judge failure rate > 10%. These are enforced: the build stops before derivation when one fires (`autonomy.enforce_breakers`, called in `jobs.execute_full_build`). Precision proxy below floor stops auto mode once the witnessed floor lands (see M3 status above).
- **Audit sampling.** At A4, a random 1% of auto decisions plus 100% of
  auto-withheld decisions are replayable with one click; an auditor can open an
  incident, which rolls back the responsible rule or threshold.
- **Human surface.** Exceptions, incidents, policy and audits. No per-merge
  queue in the steady state.

## 8. Risks

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| Review fatigue | Labels stop flowing, rules never improve | Sample, active learning, show evidence, cap queue size |
| Rule overfit | A rule fits the labels it was mined from | Replay + held-out by source and time; canary; rollback |
| Silent recall loss | 0 candidates looks like "nothing to merge" | Candidate-recall telemetry and a hard incident on zero |
| Model cost | 100k live is ~30x the 10k run (131k vs 4.5k pairs) | Cache, per-cluster cap, budgets, mock-first benchmarks |
| Drift in real data | Names, addresses and sources change | Rolling precision proxy, auto-rollback, scheduled re-eval |
| Autonomy without proof | The product's core promise breaks | Trust engine stays the decider; auto-withhold is a valid outcome |
| Claim inflation | A benchmark becomes marketing | Every number names dataset, arm, date, live/mock |

## 9. Open decisions

1. **Autonomy definition.** Confirm the A4 definition above: zero human clicks
   per batch, auto-withhold on doubt, humans set policy and audit. Alternative:
   keep a human approval for critical fields only.
2. **Rule promotion.** At A3/A4, may a rule auto-promote when the gates pass, or
   does a human always approve the first version of a new rule type?
3. **Entity type order.** Product next (already half-built, has a taxonomy), or
   organisation (new policy, no taxonomy yet)?
4. **First real dataset.** Which source do we calibrate on: the Kaggle set, a
   Vietnamese CRM export, or both?
5. **Timeline and team.** The phase table assumes one engineer; adjust before
   committing dates.

## 10. Immediate next five tasks

1. Commit the scale benchmark and add the "this measures the judge, not
   generalisation" note to `README.md`.
2. Add candidate-recall and blocked-pair telemetry to the build report; fail loud
   on a zero-candidate build at scale.
3. G1: identity roles + canonicalisation before clustering; test
   `full_name` / `address_line`.
4. G2: rarest-token and address-number blocking, configurable hot-token limit,
   CJK n-grams; re-run the Vietnamese 10k probe (today 0/500 candidates).
5. G4: persist accept/reject labels from a minimal review queue, so rule
   induction has data.

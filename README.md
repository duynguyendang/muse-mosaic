![Muse Mosaic Logo](web/logo.png)

# Muse Mosaic

**Autonomous Entity Resolution & Data Assembly with Deterministic Proof**  
*Many fragments, one trusted picture — with mathematical proof for every attribute, and `No trusted value` when proof runs out.*

[Overview](#what-is-muse-mosaic) • [Architecture](#core-architecture-the-3-pillars) • [Policy Profiles](#policy-profiles) • [Quick Start](#quick-start) • [Benchmarks](#key-verified-benchmarks) • [Demo Video](#demo-video) • [Architecture Doc](docs/ARCHITECTURE.md)

---

## What is Muse Mosaic?

**Muse Mosaic turns fragmented, conflicting enterprise records into golden customer data that autonomous AI agents can safely act on.**

When an AI agent executes a refund, dispatches an order, or delivers a one-time passcode (OTP), it requires **zero-hallucination data**. In real enterprises, customer identities are scattered across CRM (Salesforce), ERP (SAP), and Identity (CIAM) with conflicting phone numbers, transposed names, nicknames, and diacritics:

- **Pairwise Combinatorial Explosion:** 2,554 records naively generate **~3.3 million** comparisons; enterprise portfolios reach trillions.
- **The "Guessing" Hazard:** Traditional LLMs or fuzzy heuristics make plausible guesses. If an AI agent acts on a guessed record, it leaks data or refunds the wrong account.

**Muse Mosaic solves this through a strict, fail-closed division of labor:**
> **Splink proposes at scale. Muse proposes semantic meaning. The Trust Engine deterministically decides.**  
> *No model output is ever published directly.*

| System Output | Meaning | Downstream Action |
| :--- | :--- | :--- |
| **Golden Record** | A unified, versioned entity record. Every field carries its winning source, ranking rule, lineage, and policy proof. | **Authorized to act** (e.g., OTP sent, refund processed). |
| **`No trusted value`** | Held/quarantined dispute with an explicit proof reason when rules fail to settle conflicts. | **Fail-closed stop** — never guess, never fall back to fragments. |

---

## Core Architecture: The 3 Pillars

```text
  Enterprise Fragments (SAP, Salesforce, CIAM)
                       │
       ┌───────────────┴───────────────┐
       ▼                               ▼
 [1. Splink Engine]             [2. Muse AI Proposer]
 Identifier Blocking & Scale     Semantic Reasoning (Nicknames, Diacritics)
 (3.3M pairs ➔ 207 clusters)     (Bounded prompts, schema-enforced claims)
       │                               │
       └───────────────┬───────────────┘
                       │ (Proposals & Claims only)
                       ▼
             [3. Trust Engine]
             Manglekit Horn Clauses Datalog (Go)
             Deterministic O(V+E) Fixpoint Proof
                       │
         ┌─────────────┴─────────────┐
         ▼                           ▼
  [Golden Record]            [Quarantine / Hold]
  (lineage + proof)          (No trusted value)
```

1. **Splink (Scale):** High-speed identifier blocking on emails and phones. Slashes the O(N²) search space down to candidate clusters in milliseconds (under 4ms for 2,554 shards).
2. **Muse (Understanding):** LLM agent pipeline operating on bounded candidate pairs. Disentangles hard semantic variations (nicknames, transposed names, Vietnamese diacritics, taxonomy mapping) with strict JSON schemas.
3. **Trust Engine (Proof):** Pure Go + Manglekit Datalog engine. Formulates mathematical Horn clauses over candidate claims, resolving winners in 9ms. If authority, freshness, or provenance checks fail, the engine refuses to guess and quarantines the record.

---

## Policy Profiles

Policies are written as declarative Datalog Horn clauses (`.mg` + `.json`). Switching profiles evaluates cached clusters instantly:

| Profile | Strategy | Production Use Case |
| :--- | :--- | :--- |
| **`strict_bcbs239`** | **Zero-Tolerance Quarantine**: Any attribute conflict between source systems flags a dispute. | Capital markets, banking compliance, financial reporting. |
| **`retail_balanced`** | **Hierarchical Freshness**: Source hierarchy (ERP 3 > Order 2 > CRM 1) + latest event timestamp; only unresolvable conflicts quarantine. | E-commerce, customer 360, automated OTP/dispatch. |
| **`analytical_mesh`** | **Multi-Valued Lineage**: Preserves all competing values without quarantine. | Feature stores, business intelligence, data lakes. |

---

## Quick Start

### Prerequisites
- Python 3.12+
- Go 1.25+
- Pinned `mkit` binary (or use Docker)

### Run with Docker Compose
```bash
cp .env.example .env
docker compose up --build
```
Open **http://localhost:8000** to access the interactive Autonomous Data Assembly Studio.

### Local Development
```bash
# 1. Setup Python virtual environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r mosaic-api/requirements-dev.txt

# 2. Terminal 1: Trust Engine (Go)
cd trust-engine
MANGLEKIT_BIN=/path/to/mkit POLICY_DIR=./policies TAXONOMY_DIR=../data/mock/taxonomy go run .

# 3. Terminal 2: Splink Matching Engine
cd matching-engine && uvicorn main:app --port 8081

# 4. Terminal 3: Mosaic API & UI Studio
cd mosaic-api && USE_MOCK_MUSE=true uvicorn main:app --reload --port 8000
```

---

## Key Verified Benchmarks

Comprehensive verified benchmark results across 437 automated tests:

- **Semantic Disambiguation (10,000 entities, 4,496 pairs):**
  - *Standard string rules:* 255/600 recovered, **116 false merges**.
  - *Muse Mosaic:* **584/600** recovered with **0 false merges** (100% precision).
- **DeepMatcher Ground Truth (DBLP-ACM dirty + Amazon-Google):**
  - *DBLP-ACM dirty:* **F1 23.9% (rules) → 85.8% (Muse, live arm)** — oracle ceiling 99.6%.
  - *Amazon-Google:* **F1 15.2% (rules) → 50.4% (Muse, live arm)** — oracle ceiling 96.9%.
  - Candidate recall of the pipeline's own blocking index: **93.9% / 99.2%** — blocking is not the weak point; judge visibility was, and it is now fixed.
  - The deterministic rules/oracle rows **reproduce** and are logged in [`docs/runs/20261106T020340Z-deepmatcher-all-mock-05e1db3cc4fa.json`](docs/runs/20261106T020340Z-deepmatcher-all-mock-05e1db3cc4fa.json) (T F1 15.2% / 23.9%, oracle F1 96.9% / 99.6%, mock judge, 2026-11-06).
  - The live Muse rows (50.4% / 85.8%) are **unlogged claims**: no run log with SHA, date and model id exists yet — they stay until a live witness run is executed and recorded (see `docs/TEST-REPORT.md`).
  - *Rules → Muse (mock arm):* impure clusters 360 → 244 (Amazon-Google), 62 → 44 (DBLP-ACM); the precision floor for auto-apply is witnessed by the live arm only — per-dataset calibration lives in `scripts/eval_calibration.py` and `docs/TEST-REPORT.md`.
- **Kaggle messy customers (10,666 rows, 666 labelled duplicate pairs):**
  - *Name-only blocking:* recall 100% but precision ~0% — the name pool has 30 distinct values, so it merges strangers (this is the failure the fail-closed design refuses to paper over).
  - *Identity blocking (exact email):* **precision 100%**, recall 91.9% — every miss shares a placeholder/missing email and is withheld rather than guessed.
- **Hard Names (Nicknames, Transpositions, Vietnamese Diacritics):**
  - **100% precision** across 479 labelled test pairs. Zero spouses or namesakes falsely merged.
- **Fixpoint Convergence Speed:**
  - O(V+E) monotonic fixpoint evaluated in **9 milliseconds**.
- **Human Steward Touch:**
  - **0.0% manual touch** needed on clean runs; automatic fail-closed isolation on conflicts.

*(Full benchmark logs and methodology: [`docs/TEST-REPORT.md`](docs/TEST-REPORT.md))*

---

## Demo Video

- **Video Walkthrough (YouTube):** [Watch the 3-minute Demo Walkthrough on YouTube](https://www.youtube.com/watch?v=dA8yXEgUzsA)
- **Interactive Architecture Slide:** Open [`web/intro.html`](web/intro.html) locally or directly via the Studio.

---

## Repository Layout

```text
├── mosaic-api/        # FastAPI core: orchestration, API endpoints, SQLite ledger, Muse agents
├── trust-engine/      # High-performance Go service executing Manglekit Datalog Horn clauses
├── matching-engine/   # Splink + DuckDB candidate clustering service
├── web/               # Studio UI (HTML/Tailwind/JS), brand assets, and architecture slides
├── data/              # Policy profiles (strict_bcbs239, retail_balanced, analytical_mesh), mock data, entity schema contracts (data/schemas/)
├── docs/              # ARCHITECTURE.md, TEST-REPORT.md, ROADMAP.md
└── scripts/           # Scale and evaluation harnesses; zero-touch onboarding (onboard.py)
```

---

## Contract & Safety Guardrails

1. **No Fact Without Provenance:** Source, source ID, authority tier, and timezone-aware event timestamp are strictly required.
2. **No Model Publishes Directly:** Muse proposals are untrusted claims until verified by the Datalog solver.
3. **Fail-Closed by Design:** Any missing rule, unknown policy, or data inconsistency produces `No trusted value` rather than a guess.
4. **Exact Deterministic Replay:** Every decision carries its derivation tree, rule name, and policy version for full regulatory auditability.
5. **Autonomy Is Opt-In Per Entity** (M3): the default is `AUTONOMY_MODE=off` — Muse never applies without an explicit grant per entity type. `shadow`/`suggest` only observe; `auto` applies; a process-local kill switch (`POST /api/v1/autonomy/freeze`) freezes every auto-apply without stopping reads; every batch reports modes, circuit breakers and a deterministic 1% audit sample (withheld decisions always included).

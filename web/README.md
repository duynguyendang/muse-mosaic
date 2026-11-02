# web/ — Muse Mosaic cockpit (single screen)

Static: `index.html` + `studio.js` + prebuilt `studio.css` (rebuild with `scripts/build_css.sh` after changing markup/classes). Screen source of truth:
`docs/internal/screen/` (`DESIGN.md` tokens, `code.html` reference markup,
`screen.png` target render). The screen is an executive cockpit over the
mosaic-api ledger — every number is read live, never derived in the browser.

| Zone | Source of truth |
|---|---|
| Header dataset pill / nav counts | `GET /datasets/summary`, `GET /datasets/kaggle` (document count) |
| Autonomy pill + steward KPIs | `GET /steward/pending` |
| Certified goldens / zero-touch | `GET /datasets/summary` (`clean_records`, `clean_pct`) |
| Rule R2 quarantine / disputes | `GET /datasets/summary` (`needs_review`) |
| Fixpoint convergence + run ID | latest `GET /jobs` (`created_at` → `updated_at`, `job_id`) |
| Stage 01 source rows | `/datasets/kaggle` documents grouped by `source` |
| Stage 03 rule rows | rules R1/L1 static copy; R2 count from `needs_review` |
| Diagnostics bar | `GET /health`, latest job |
| Dispatch deck | `/datasets/summary`, `GET /downstream/{agent}`, `GET /export/{type}` |
| Policy Copilot (header CTA) | `POST /api/v1/pipeline/copilot/draft` (draft only; nothing is applied) |
| Dataset pill (header) | Opens the dataset modal: `GET /datasets/catalog`, `GET /datasets/{id}`, `POST /upload` (JSON/CSV fragments) or native `{documents:[...]}` JSON; selection is session-only |
| Expected output | Draft 2020-12 JSON Schema, saved to `localStorage`, sent as `expected_structure` on `POST /jobs` |
| Deck: BCBS 239 card → Audit Manifest | composed health/summary/steward/jobs manifest |
| Deck: Feature Store card → Export Goldens | `GET /export/{user,product}?format=json&scope=published` |

## Selecting or uploading another source

The dataset pill in the header is the single entry point: clicking it opens the
dataset modal, where you can **view** the active dataset, **select** any dataset
already loaded in this session, or **upload** a new one.

- **Available datasets** lists every bundled dataset from
  `GET /datasets/catalog` plus every dataset uploaded in this browser session.
  Clicking a row makes it active: the header pill, Stage 01 source breakdown,
  shard count follow it.

  Bundled benchmarks ship in `data/` and are registered in
  `mosaic-api/routes/reference.py` (`_BUNDLED_DATASETS`):

  | id | dataset | docs | trust policy |
  |---|---|---|---|
  | `kaggle` | Kaggle Customer 360 | 2,554 | `data/kaggle/policies/` |
  | `olist` | Kaggle Olist E-Commerce | 26 | `data/olist/policies/` |

  A dataset's own policy and expected structure are composed at run time from
  its `data/<dataset>/` directory; nothing is auto-loaded. The trust-engine
  discovers staged policies under `data/*/policies/`, so a new dataset needs
  no engine change.
- **Fragments**: JSON (one fragment, an array, or `{"fragments":[...]}`) or a
  columnar `attribute,value` CSV → `POST /upload`, then grouped into source
  documents by `(source, source_id)`.
- **Native documents**: `{"documents":[...]}` in the exact `POST /jobs`
  contract → used as-is (validated client-side, then by the API on enqueue).
- **Use this dataset** adds the parsed file to the list and activates it.
  Uploads are session-only and never publish anything by themselves.

The modal also defines the **expected output**: a Draft 2020-12 JSON Schema
sent as `expected_structure` with the job. It is remembered across reloads
(`mosaic.expected-output-v1`) and shown as `CUSTOM EXPECTED OUTPUT · <type>`
in the diagnostics bar. The editor's *Load default schema* text follows the
active dataset (`olist` requires `customer_unique_id`, not the Kaggle `email`
shape) and never overwrites a saved custom schema. When disabled, mosaic-api
applies its own default for the dataset — `data/mock/templates/` normally, or
the dataset's `data/<dataset>/templates/` when it ships one. A golden that
fails the schema is escalated by the publisher, never published.

## Policy profiles (Stage 03 + 04 hot-swap)

The header's policy selector switches which Datalog rules the pipeline runs.
Switching re-executes **only Stage 03 (Manglekit Datalog) and Stage 04
(monotonic fixpoint)** over clusters cached from Stage 02 — Stage 01 intake and
Stage 02 Splink partitioning are never repeated.

| profile | label | behaviour |
|---|---|---|
| `strict_bcbs239` | Strict BCBS 239 (Zero Tolerance) | Any winner disagreement quarantines (C1). The pre-feature baseline: 244/247 quarantined on this dataset. |
| `retail_balanced` | Enterprise Commercial (Retail Balanced) | Source hierarchy (ERP 3 > order/OTP 2 > CRM 1) + freshness. Only **cross-system** disputes quarantine; in-feed duplicates collapse. `order_id` is `multivalued`, so a repeatable business key is never uniqueness-constrained. → 4 quarantined, 15 stewards. |
| `analytical_mesh` | Analytical Mesh (Multi-Valued) | Every distinct observed value publishes. No `esc`/`den` rules exist in the file, so zero-quarantine is guaranteed **by construction**. → 0 quarantined, 247 published. |

Profiles are data, not code: each is a `.json` + `.mg` pair under
`data/<profile>/policies/{user,product}/`, a layout the trust-engine already
discovers for staged policies. Adding a fourth is a file drop.

Endpoints (all preview-only — **nothing here writes to the ledger**; only
`POST /build` and `POST /jobs` publish):

| endpoint | purpose |
|---|---|
| `GET /api/v1/pipeline/policies` | Profiles, rule codes, Horn clauses, compile status |
| `POST /api/v1/pipeline/apply-policy` | `{policy_id}` → metrics + `policy_hash` + attestation root |
| `GET /api/v1/pipeline/state` | Stage 02 cache / precompute status |

`GET /export/{type}` includes `policy_id`, `policy_hash`, `policy_rules` and
`merkle_root` in `export_metadata`, so a manifest names the rules that
produced it.

### Latency

Measured, not assumed:

* `mkit eval` floors at **20.26 ms** per invocation (spawn + parse + teardown)
  and scales **quadratically** with facts (100 → 50 ms, 1000 → 2192 ms), so
  batching all clusters into one call is slower, not faster (it timed out past
  120 s).
* The Datalog solver is manglekit's `internal/engine`, unimportable from Go's
  `internal/` rule, and `mkit serve` is an agent-governance API rather than an
  eval endpoint — so the engine cannot be embedded to skip the spawn.

Per-cluster subprocess cost is therefore irreducible, and the spec's
"re-evaluate in ≤ 15 ms" cannot be met by re-executing 247 clusters. The
design that meets the *user-visible* goal is **precompute once, switch by
lookup**: the first switch for a cluster set evaluates all three profiles
(~55–65 s); every switch after that is a cache read reporting
`execution_latency_ms ≈ 0.001` (HTTP wall ~25 ms). Both numbers are in the
response so the distinction is visible rather than implied.

Attestation: `MerkleRoot = SHA256(GoldenFactsRoot ‖ ClusterGraphRoot ‖ PolicyHash)`
— folding the policy hash in means editing one Horn clause changes the root
even when no fact changes. The API and trust-engine compute this hash
independently (Python `hashlib` / Go `crypto/sha256`) and agree on all three
profiles.

## Demo fallback (explicit, never silent)

If the ledger is empty (`total_records == 0`) or mosaic-api is unreachable,
the screen renders the design's demo snapshot (`2,554 / 170 / 163 / 7 / …`)
and labels it:

- diagnostics bar: `LIVE API · DEMO SNAPSHOT` or `API UNREACHABLE · DEMO`
- autonomy pill: `API UNREACHABLE · showing demo snapshot`
- dispatch socket + footer verification flip to warning/error colours

Live ledger values replace the demo numbers as soon as data exists; no value
is ever a fabricated derivation.

## Local / demo

mosaic-api serves this folder at `/`:

```bash
cd mosaic-api
USE_MOCK_MUSE=true uvicorn main:app --reload
# open http://127.0.0.1:8000
```

## Separate frontend deploy

Upload this folder (all files, same directory) to any static host. Point it at
the API by declaring the base URL before `studio.js`:

```html
<script>window.MOSAIC_API_BASE = "https://api.example.com"</script>
<script src="./studio.js"></script>
```

Same-origin default keeps local/demo working. The API host must allow the
frontend origin (CORS) — configure that on the API side.

## Stack note

Per `docs/internal/screen/`, the screen follows the mockup: Tailwind is
prebuilt into `studio.css` (tokens in `tailwind.config.js`), while Google Fonts
(Inter, JetBrains Mono) and Material Symbols still load from the network, so
icons fall back to their text names offline. Vendor those fonts if the demo must
survive a network loss.

`web/legacy/` holds archived copies of the previous Friendly/Detail
frontend (`index.html`, `app.js`, `drawers.js`, `chat.js`, `config.js`) for
reference. It is not served by the new screen and is not maintained.

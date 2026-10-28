# matching-engine — Splink matching engine (proposer only)

Standalone FastAPI service. Stateless: no ledger, no graph writes, no golden
writes. Internal only (Compose exposes but never publishes the port).

## Contract

`POST /match` — input fragments (already flattened by mosaic-api), policy
version, blocking + threshold from the policy file:

```json
{
  "fragments": [{"fragment_id": "F1", "entity_type": "user", "email": "a@x.io"}],
  "policy_version": "pol-2026-10-27-v1",
  "blocking": ["email", "phone", "order_id"],   // user; product uses ["category"]
  "comparisons": ["exact", "jaro_winkler"],
  "threshold": 0.9
}
```

Output: clusters (deterministic exact linkage) + per-pair EM
`match_weight`/`match_probability` as candidate signals only, each pair with
its `blocking_rule` evidence, `proposer: "Splink"`, and `meets_threshold` (EM probability unknown or at/above the policy `threshold`; deterministic exact pairs are always reported since EM is degenerate on demo-scale data — the gate decides, Splink proposes). Any failure returns
`status: "unavailable"` with empty candidates — never raises, never publishes.

`GET /health` — `{splink_service: "ok", splink_version, duckdb_version, splink_enabled}`.

Pins (do not invent versions): `splink==5.0.0`, `duckdb==2.0.0`
(`requirements.txt`).

## Layout

* `main.py` — FastAPI app, contracts, `GET /health`, `POST /match` validation.
* `splink_engine.py` — matching core (moved verbatim; the mosaic-api in-process fallback mirrors it — Liskov parity test keeps them honest).

## Local run

```bash
pip install -r requirements.txt
uvicorn main:app --port 8081
curl localhost:8081/health
```

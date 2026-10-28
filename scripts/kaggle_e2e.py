#!/usr/bin/env python3
"""End-to-end run of the frozen Kaggle Customer 360 sample.

Composes an isolated runtime (scratch policy dir = repo policies + the new
kaggle versions, scratch taxonomy copy, scratch SQLite ledger), starts the
three services, and runs: /health -> /build products -> /build users ->
downstream checks. Prints a JSON summary and writes it to --out.

Nothing here touches the repo: no fixture, policy, or taxonomy file is
modified; the Accept demo (if run) writes only into the scratch taxonomy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "mosaic-api"
TE_DIR = ROOT / "trust-engine"
ME_DIR = ROOT / "matching-engine"


def find_mkit() -> Path:
    """Locate the pinned mkit binary: env, PATH, then known build outputs."""
    env = os.environ.get("MANGLEKIT_BIN", "").strip()
    candidates = ([Path(env)] if env else []) + [
        Path(shutil.which("mkit") or ""),
        ROOT.parent / "mkitbuild" / "mkit",
        Path("/tmp/mkitbuild/mkit"),
    ]
    for path in candidates:
        if path and path.is_file():
            return path
    tried = ", ".join(str(p) for p in candidates if p)
    raise SystemExit(
        "mkit binary not found. Set MANGLEKIT_BIN, put mkit on PATH, or "
        f"build it first. Tried: {tried or '(nothing)'}"
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url: str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=5)
            if response.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(1.0)
    raise RuntimeError(f"service never healthy at {url}: {last}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="data/kaggle/customer360_sample_200.json")
    parser.add_argument("--out", default="/tmp/kaggle_run.json")
    parser.add_argument("--workdir", default="/tmp/kag")
    args = parser.parse_args()

    workdir = Path(args.workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    pol_dir = workdir / "policies"
    for entity in ("user", "product"):
        target = pol_dir / entity
        target.mkdir(parents=True)
        for src in (TE_DIR / "policies" / entity).glob("*.json"):
            shutil.copy(src, target / src.name)
        for src in (TE_DIR / "policies" / entity).glob("*.mg"):
            shutil.copy(src, target / src.name)
    for src in (ROOT / "data" / "kaggle" / "policies" / "user").glob("*"):
        shutil.copy(src, pol_dir / "user" / src.name)
    for src in (ROOT / "data" / "kaggle" / "policies" / "product").glob("*"):
        shutil.copy(src, pol_dir / "product" / src.name)
    tax_dir = workdir / "taxonomy"
    shutil.copytree(ROOT / "data" / "mock" / "taxonomy", tax_dir)
    db_path = workdir / "db"
    db_path.mkdir(parents=True)
    ledger = db_path / "mosaic.db"

    sample = json.loads((ROOT / args.sample).read_text())
    user_docs = [d for d in sample["documents"] if d["entity_type"] == "user"]
    product_docs = [d for d in sample["documents"] if d["entity_type"] == "product"]
    print(f"sample: {len(user_docs)} user docs, {len(product_docs)} product docs", flush=True)

    mkit = find_mkit()
    print(f"mkit: {mkit}", flush=True)
    te_bin = workdir / "trust-engine"
    print("building trust-engine...", flush=True)
    subprocess.run(
        ["go", "build", "-o", str(te_bin), "."],
        cwd=TE_DIR,
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )

    te_port, me_port, api_port = free_port(), free_port(), free_port()
    procs = []
    try:
        te_env = dict(
            os.environ,
            MANGLEKIT_BIN=str(mkit),
            PORT=str(te_port),
            POLICY_DIR=str(pol_dir),
            TAXONOMY_DIR=str(tax_dir),
        )
        procs.append(
            subprocess.Popen(
                [str(te_bin)], env=te_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        )
        me_env = dict(os.environ, PORT=str(me_port))
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(me_port),
                ],
                cwd=ME_DIR,
                env=me_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        api_env = dict(
            os.environ,
            USE_MOCK_MUSE="true",
            DATABASE_PATH=str(ledger),
            TAXONOMY_DIR=str(tax_dir),
            TRUST_ENGINE_URL=f"http://127.0.0.1:{te_port}/derive",
            MATCHING_ENGINE_URL=f"http://127.0.0.1:{me_port}",
            POLICY_DIR=str(pol_dir),
        )
        # NOTE: POLICY_DIR is read by mosaic-api at startup (policies_dir()).
        api_env["POLICY_DIR"] = str(pol_dir)
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                cwd=API_DIR,
                env=api_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        api = f"http://127.0.0.1:{api_port}"
        print("waiting for services...", flush=True)
        wait_http(f"http://127.0.0.1:{te_port}/health")
        wait_http(f"http://127.0.0.1:{me_port}/health")
        wait_http(f"{api}/health")

        health = httpx.get(f"{api}/health", timeout=10).json()
        print("health:", json.dumps(health), flush=True)

        summary: dict = {
            "health": health,
            "products": None,
            "users": None,
            "downstream": [],
            "meta": sample.get("meta", {}),
        }

        print("building products...", flush=True)
        t0 = time.time()
        response = httpx.post(
            f"{api}/build",
            json={
                "work_id": "kag-products",
                "idempotency_key": "kag-products",
                "dataset": {"documents": product_docs},
            },
            timeout=600,
        )
        response.raise_for_status()
        products = response.json()
        summary["products"] = _summarize_build(products, "products")
        print(
            f"products done in {time.time()-t0:.0f}s:", json.dumps(summary["products"]), flush=True
        )

        print("building users...", flush=True)
        t0 = time.time()
        response = httpx.post(
            f"{api}/build",
            json={
                "work_id": "kag-users",
                "idempotency_key": "kag-users",
                "dataset": {"documents": user_docs},
            },
            timeout=600,
        )
        response.raise_for_status()
        users = response.json()
        summary["users"] = _summarize_build(users, "users")
        print(f"users done in {time.time()-t0:.0f}s:", json.dumps(summary["users"]), flush=True)

        user_goldens = [g for g in users.get("goldens", []) if g.get("golden_id")]
        product_goldens = [g for g in products.get("goldens", []) if g.get("golden_id")]
        # Pair each downstream agent with the entity type it reads: OTP acts
        # on a user golden's phone; product search reads a product golden's
        # category. Querying search/category against a user golden is a
        # category error, not a downstream verdict.
        checks = []
        if user_goldens:
            first = user_goldens[0]
            checks.append(("otp", first["entity_type"], first["entity_id"], "phone"))
        if product_goldens:
            first = product_goldens[0]
            checks.append(("search", first["entity_type"], first["entity_id"], "category"))
        for agent, entity_type, entity_id, field in checks:
            try:
                downstream = httpx.get(
                    f"{api}/downstream/{agent}?entity_type={entity_type}"
                    f"&entity_id={entity_id}&field={field}",
                    timeout=30,
                ).json()
                summary["downstream"].append(downstream)
                print(f"downstream {agent}: {downstream.get('answer')}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"downstream {agent} failed: {type(exc).__name__}: {exc}", flush=True)

        Path(args.out).write_text(json.dumps(summary, indent=1) + "\n")
        print(f"wrote {args.out}", flush=True)
        return 0
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


def _summarize_build(payload: dict, label: str) -> dict:
    goldens = [g for g in payload.get("goldens", []) if g.get("golden_id")]
    fields: dict[str, int] = {}
    for golden in goldens:
        for name in golden.get("attributes", {}):
            fields[name] = fields.get(name, 0) + 1
    stewards = payload.get("steward_queue", [])
    reasons: dict[str, int] = {}
    for item in payload.get("conflicts", []) or []:
        reasons[item.get("reason_code", "?")] = reasons.get(item.get("reason_code", "?"), 0) + 1
    return {
        "verdict": payload.get("verdict"),
        "decision_id": payload.get("decision_id"),
        "splink": payload.get("splink", {}).get("status"),
        "splink_clusters": len((payload.get("splink") or {}).get("clusters", [])),
        "splink_pairs": len((payload.get("splink") or {}).get("pairs", [])),
        "goldens": len(goldens),
        "golden_fields": fields,
        "steward_items": len(stewards),
        "steward_attributes": sorted({item.get("attribute", "?") for item in stewards}),
    }


if __name__ == "__main__":
    raise SystemExit(main())

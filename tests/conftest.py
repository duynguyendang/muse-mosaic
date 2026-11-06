"""Shared pytest setup: imports, manglekit binary, trust-engine, API client."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "mosaic-api"
TE_DIR = ROOT / "trust-engine"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Contract tests must never require a real model. Respect an explicit
# USE_MOCK_MUSE=false only if the caller set it deliberately.
os.environ.setdefault("USE_MOCK_MUSE", "true")

MANGLEKIT_VERSION = "v0.0.0-20260330150740-81e7572ffb31"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_witness(tmp_path) -> str:
    """A minimal but valid live calibration witness for auto-mode tests.

    The auto gate (auto_witness.verify_auto_witness) requires a live-judge
    run log with precision >= floor on every dataset; this stands in for
    one without spending a single token.
    """
    import json

    path = Path(tmp_path) / "witness.json"
    path.write_text(
        json.dumps(
            {
                "harness": "scripts/eval_calibration.py",
                "date_utc": "20261106T000000Z",
                "git_sha": "0" * 40,
                "judge": "live",
                "floor": 0.995,
                "model_id": "test-live-model",
                "max_pairs": 0,
                "datasets": [
                    {
                        "dataset": "dblp-acm-dirty",
                        "entity_type": "publication",
                        "documents": 4910,
                        "pairs_positive": 2220,
                        "floor": 0.995,
                        "best_precision": 0.999,
                        "arms": [
                            {
                                "arm": "M Muse (live, min_confidence=high)",
                                "confidence": "high",
                                "precision": 0.999,
                                "recall": 0.9,
                                "f1": 0.94,
                                "clusters": 2708,
                                "impure_clusters": 0,
                                "judged": 18500,
                                "merged": 1000,
                                "judge_calls": 18500,
                                "cache_hits": 0,
                                "tokens": 1000000,
                            }
                        ],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return str(path)


_COMPOSED_DIR: Path | None = None


def _composed_policy_dir() -> Path:
    """Scratch policy tree for the engine: repo defaults + staged profiles.

    Tests compose explicitly instead of relying on any auto-discovery: the
    engine only serves directories it was pointed at. Same lexical convention
    as production: the staged file sorts last and wins per entity type.
    """
    global _COMPOSED_DIR
    if _COMPOSED_DIR is not None:
        return _COMPOSED_DIR
    import shutil
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="mosaic-policies-"))
    for source_dir in (TE_DIR / "policies",):
        # every shipped entity policy directory is mirrored: a new policy
        # file must not need a conftest edit (same rule as the engine)
        for entity_dir in source_dir.iterdir():
            if not entity_dir.is_dir() or not any(entity_dir.glob("*.json")):
                continue
            dest = base / entity_dir.name
            dest.mkdir(parents=True, exist_ok=True)
            for path in entity_dir.glob("*"):
                shutil.copy(path, dest / path.name)
    for entity in ("user", "product"):
        dest = base / entity
        dest.mkdir(exist_ok=True)
        for profile in ("retail-balanced", "analytical-mesh"):
            staged = ROOT / "data" / profile / "policies" / entity
            if staged.is_dir():
                for path in staged.glob("*"):
                    shutil.copy(path, dest / path.name)
    _COMPOSED_DIR = base
    return base


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"trust-engine did not start on {port}")


@pytest.fixture(scope="session")
def mkit_bin(tmp_path_factory) -> Path:
    """Provide the pinned manglekit binary, building offline if needed."""
    override = os.environ.get("MANGLEKIT_BIN")
    if override and Path(override).exists():
        return Path(override)
    cached = Path("/tmp/mkitbuild/mkit")
    if cached.exists():
        return cached
    workdir = tmp_path_factory.mktemp("mkitbuild")
    subprocess.run(
        ["go", "mod", "init", "tmpmkit"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["go", "mod", "edit", f"-require=github.com/duynguyendang/manglekit@{MANGLEKIT_VERSION}"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )
    env = dict(os.environ, GOPROXY="off", GOFLAGS="-mod=mod")
    binary = workdir / "mkit"
    result = subprocess.run(
        ["go", "build", "-o", str(binary), "github.com/duynguyendang/manglekit/cmd/mkit"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    if result.returncode != 0 or not binary.exists():
        pytest.skip(f"manglekit binary unavailable: {result.stderr[-500:]}")
    return binary


@pytest.fixture(scope="session")
def trust_engine_binary(tmp_path_factory) -> Path:
    """Build the trust-engine Go service once per session."""
    binary = tmp_path_factory.mktemp("te") / "trust-engine"
    result = subprocess.run(
        ["go", "build", "-o", str(binary), "."],
        cwd=TE_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return binary


class TrustEngine:
    """A managed trust-engine subprocess."""

    def __init__(self, binary: Path, mkit: Path, policy_dir: Path | None = None):
        self.binary = binary
        self.mkit = mkit
        self.port = _free_port()
        self.policy_dir = policy_dir
        self.process: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/derive"

    def start(self) -> TrustEngine:
        policy_dir = self.policy_dir if self.policy_dir is not None else _composed_policy_dir()
        env = dict(
            os.environ,
            MANGLEKIT_BIN=str(self.mkit),
            PORT=str(self.port),
            POLICY_DIR=str(policy_dir),
        )
        env.setdefault("TAXONOMY_DIR", str(ROOT / "data" / "mock" / "taxonomy"))
        self.process = subprocess.Popen(
            [str(self.binary)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_port(self.port)
        return self

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    def __enter__(self) -> TrustEngine:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


class MatchingEngine:
    """A managed matching-engine subprocess (real matching engine over HTTP)."""

    def __init__(self):
        self.port = _free_port()
        self.process: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> MatchingEngine:
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=ROOT / "matching-engine",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_http_health(f"{self.url}/health")
        return self

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    def __enter__(self) -> MatchingEngine:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def _wait_for_http_health(url: str, timeout: float = 60.0) -> None:
    """Wait until a JSON /health endpoint answers HTTP 200."""
    import json as _json
    import urllib.request as _request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _request.urlopen(url, timeout=2) as response:
                if response.status == 200 and _json.load(response).get("status") == "ok":
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"service did not become healthy at {url}")


@pytest.fixture(scope="module")
def matching_engine():
    """A running matching-engine for matcher tests (module-scoped)."""
    pytest.importorskip("splink", reason="splink not installed")
    service = MatchingEngine().start()
    yield service.url
    service.stop()


@pytest.fixture(scope="module")
def te_url(trust_engine_binary, mkit_bin):
    """A running trust-engine for derive-path tests (module-scoped)."""
    engine = TrustEngine(trust_engine_binary, mkit_bin).start()
    yield engine.url
    engine.stop()


@pytest.fixture()
def api_client(te_url, tmp_path):
    """A mosaic-api TestClient wired to a live trust-engine and temp ledger."""
    with make_api_client(te_url, tmp_path) as client:
        yield client


def make_api_client(te_derive_url: str, tmp_path, matching_service_url: str | None = None):
    """Build a TestClient against any trust-engine URL (kill-test helper)."""
    import dataclasses

    from config import Settings
    from fastapi.testclient import TestClient
    from main import create_app

    settings = Settings(
        use_mock_muse=True,
        meta_model_api_key="",
        muse_model_id="",
        embedding_model="",
        embedding_version="",
        embedding_dim=None,
        trust_engine_url=te_derive_url,
        manglekit_bin="",
        database_path=str(Path(tmp_path) / "mosaic.db"),
        enable_background_tasks=False,
    )
    if matching_service_url:
        settings = dataclasses.replace(settings, matching_service_url=matching_service_url)
    return TestClient(create_app(settings))

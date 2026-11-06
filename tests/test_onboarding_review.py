"""The onboarding human-review gate.

A derived bundle must never load before a person signs off on the terms it
minted and the ranks it derived. These tests pin the gate end to end:
pending -> refused -> approve -> loads, and every consistency failure mode.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import onboarding
import pytest
from bundle_review import ReviewPending, check_bundle_approved
from config import Settings
from policies import PolicyError, load_policies
from taxonomies import TaxonomyError, load_taxonomies


def _settings() -> Settings:
    return Settings(
        use_mock_muse=True,
        meta_model_api_key="",
        muse_model_id="",
        embedding_model="",
        embedding_version="",
        embedding_dim=None,
        trust_engine_url="http://127.0.0.1:1/derive",
        manglekit_bin="",
        database_path="/tmp/onboarding-review-test.db",
        enable_background_tasks=False,
    )


def _doc(doc_id: str, source: str, entity_type: str = "product", **fields) -> dict:
    return {
        "document_id": doc_id,
        "entity_type": entity_type,
        "source": source,
        "synthetic": True,
        "fields": {
            name: {
                "value": value,
                "authority_rank": 2,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            }
            for name, value in fields.items()
        },
    }


_BRANDS = ["Intuit", "Adobe", "Intuit", "Microsoft", "Adobe", "Intuit"] * 2
_TYPES = ["accounting", "design", "accounting", "design", "accounting", "accounting"] * 2


def _product_docs():
    docs = []
    for i, (brand, product_type) in enumerate(zip(_BRANDS, _TYPES, strict=True)):
        docs.append(
            _doc(
                f"d{i}",
                "pim" if i % 2 == 0 else "marketplace",
                prod_title=f"QuickBooks 2007 num{i}",
                manufacturer=brand,
                product_type=product_type,
            )
        )
    return docs


_EXPECTED = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "manufacturer": {"type": "string"},
        "product_type": {"type": "string"},
    },
}


@pytest.fixture()
def ok_engine(monkeypatch):
    monkeypatch.setattr(
        "policy_copilot.verify_with_engine",
        lambda horn: {"engine_available": True, "compiles": True, "error": None},
    )


def _derive(tmp_path: Path):
    return asyncio.run(
        onboarding.assemble_bundle(
            _product_docs(),
            entity_type="product",
            expected_structure=_EXPECTED,
            out_root=tmp_path,
            settings=_settings(),
        )
    )


def test_fresh_bundle_is_pending_and_carries_the_review(tmp_path: Path, ok_engine) -> None:
    manifest = _derive(tmp_path)
    bundle = Path(manifest["bundle_dir"])
    assert manifest["review"]["status"] == "pending"
    review = json.loads((bundle / "review.json").read_text(encoding="utf-8"))
    assert review["status"] == "pending"
    assert review["documents_sha256"] == manifest["inputs"]["documents_sha256"]
    # the review names what was minted and the evidence behind it
    assert review["terms"], "expected minted taxonomy terms in the review"
    assert all("support" in term and "variants" in term for term in review["terms"])
    assert review["authority_matrix"], "expected derived authority ranks in the review"
    assert all("votes" in row and "rank" in row for row in review["authority_matrix"])


def test_loaders_refuse_pending_bundle(tmp_path: Path, ok_engine) -> None:
    manifest = _derive(tmp_path)
    bundle = Path(manifest["bundle_dir"])
    with pytest.raises(PolicyError, match="refused"):
        load_policies(bundle / "policies")
    with pytest.raises(TaxonomyError, match="refused"):
        load_taxonomies(bundle / "taxonomy")
    with pytest.raises(ReviewPending):
        check_bundle_approved(bundle)


def test_approved_bundle_loads(tmp_path: Path, ok_engine) -> None:
    manifest = _derive(tmp_path)
    bundle = Path(manifest["bundle_dir"])
    approval = onboarding.accept_review(bundle, reviewer="steward-jane", note="looks right")
    assert approval["reviewer"] == "steward-jane"
    assert (bundle / "approval.json").is_file()
    assert check_bundle_approved(bundle)["entity_type"] == "product"
    policies = load_policies(bundle / "policies")
    taxonomies = load_taxonomies(bundle / "taxonomy")
    assert "product" in policies
    assert taxonomies
    review = json.loads((bundle / "review.json").read_text(encoding="utf-8"))
    assert review["status"] == "approved"
    updated = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert updated["review"]["status"] == "approved"


def test_tampered_review_after_approval_is_refused(tmp_path: Path, ok_engine) -> None:
    manifest = _derive(tmp_path)
    bundle = Path(manifest["bundle_dir"])
    onboarding.accept_review(bundle, reviewer="steward-jane")
    review_path = bundle / "review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["terms"][0]["term"] = "tampered"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(ReviewPending, match="digest mismatch"):
        check_bundle_approved(bundle)


def test_approval_binds_to_the_derivation_input(tmp_path: Path, ok_engine) -> None:
    manifest = _derive(tmp_path)
    bundle = Path(manifest["bundle_dir"])
    onboarding.accept_review(bundle, reviewer="steward-jane")
    # simulate a different dataset being swapped into the bundle afterwards
    approval_path = bundle / "approval.json"
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["documents_sha256"] = "0" * 64
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    with pytest.raises(ReviewPending, match="does not match"):
        check_bundle_approved(bundle)


def test_reaccept_by_a_different_reviewer_refused(tmp_path: Path, ok_engine) -> None:
    manifest = _derive(tmp_path)
    bundle = Path(manifest["bundle_dir"])
    first = onboarding.accept_review(bundle, reviewer="steward-jane")
    assert first["already_approved"] is False
    again = onboarding.accept_review(bundle, reviewer="steward-jane")
    assert again["already_approved"] is True
    with pytest.raises(ReviewPending, match="already approved"):
        onboarding.accept_review(bundle, reviewer="mallory")


def test_approve_non_bundle_refuses(tmp_path: Path, ok_engine) -> None:
    with pytest.raises(ReviewPending, match="manifest"):
        onboarding.accept_review(tmp_path, reviewer="steward-jane")


def test_non_bundle_directories_pass_the_gate(tmp_path: Path) -> None:
    # plain (non-bundle) policy trees and empty dirs are unaffected
    assert check_bundle_approved(tmp_path) is None


def test_cli_accept_and_show_review(tmp_path: Path, ok_engine) -> None:
    """The operator-facing approval commands: show the review, accept it."""
    import os
    import subprocess
    import sys

    ROOT = Path(__file__).resolve().parents[1]
    manifest = _derive(tmp_path)
    bundle = Path(manifest["bundle_dir"])
    env = dict(os.environ, PYTHONPATH=f"{ROOT / 'mosaic-api'}{os.pathsep}{ROOT}")

    show = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "onboard.py"),
            "--show-review",
            str(bundle),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert show.returncode == 0, show.stderr
    assert '"status": "pending"' in show.stdout

    accept = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "onboard.py"),
            "--accept",
            str(bundle),
            "--reviewer",
            "cli-steward",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert accept.returncode == 0, accept.stderr
    assert "approved" in accept.stdout
    check = json.loads((bundle / "approval.json").read_text(encoding="utf-8"))
    assert check["reviewer"] == "cli-steward"

    # the CLI refuses to re-approve under a different reviewer
    again = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "onboard.py"),
            "--accept",
            str(bundle),
            "--reviewer",
            "mallory",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert again.returncode == 2
    assert "already approved" in (again.stdout + again.stderr).lower()

"""Onboarding bundle human-review gate (shared, dependency-free).

An onboarding bundle is derived artifacts plus a checksummed review: the
taxonomy terms that were minted from observed strings, and the authority
ranks that were derived from observed declarations. A majority-wrong source
must not become policy just because it was frequent, so **nothing loads
from a bundle until a human approves the review**:

- ``policies.load_policies`` / ``taxonomies.load_taxonomies`` call
  :func:`refuse_pending_bundle`;
- ``scripts/onboard.py --bundle`` refuses to build;
- ``scripts/onboard.py --accept <bundle> --reviewer <id>`` records the sign-off
  (``approval.json``) and flips the bundle to approved.

Directories that are not onboarding bundles (the repo defaults) pass
through untouched. Anything bundle-shaped that is missing, unreadable or
unapproved fails closed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ONBOARDING_BUNDLE = "muse-mosaic onboarding"


class ReviewPending(RuntimeError):
    """The bundle's review is pending (or inconsistent); refusing to load."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ReviewPending(f"{path} is unreadable: {exc}") from exc


def check_bundle_approved(base: str | Path) -> dict[str, Any] | None:
    """Validate base (or its parent) if it is an onboarding bundle.

    Returns the manifest when the directory is a bundle and its review is
    approved; ``None`` when it is not a bundle at all. Raises
    :class:`ReviewPending` for anything bundle-shaped that is unapproved.
    """
    base_path = Path(base)
    for candidate in (base_path, base_path.parent):
        manifest_path = candidate / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = _read_json(manifest_path)
        if not isinstance(manifest, dict):
            raise ReviewPending(f"{manifest_path} is not a JSON object; refusing to load")
        if manifest.get("derived_by") != ONBOARDING_BUNDLE and manifest.get("review") is None:
            # bundle-shaped, but not a recognised (reviewed) bundle: fail closed
            raise ReviewPending(
                f"{manifest_path} looks like an onboarding bundle but carries no review "
                f"block; re-derive and approve it with scripts/onboard.py --accept"
            )
        inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), dict) else {}
        review = manifest.get("review")
        if not isinstance(review, dict):
            raise ReviewPending(f"{manifest_path} carries no review block; refusing to load")
        if str(review.get("status")) != "approved":
            raise ReviewPending(
                f"onboarding bundle review is {review.get('status')!r}, not approved. "
                f"Approve it with: python3 scripts/onboard.py --accept {candidate} "
                f"--reviewer <id>"
            )
        approval_path = candidate / "approval.json"
        review_path = candidate / "review.json"
        if not approval_path.is_file() or not review_path.is_file():
            raise ReviewPending(f"{candidate} is missing approval.json/review.json")
        approval = _read_json(approval_path)
        if not isinstance(approval, dict):
            raise ReviewPending(f"{approval_path} is not a JSON object; refusing to load")
        if approval.get("documents_sha256") != inputs.get("documents_sha256"):
            raise ReviewPending(
                "approval.json does not match the bundle's derivation input; refusing to load"
            )
        digest = hashlib.sha256(review_path.read_bytes()).hexdigest()
        if approval.get("review_sha256") != digest:
            raise ReviewPending(
                "review.json changed after approval (digest mismatch); refusing to load"
            )
        return manifest
    return None


def refuse_pending_bundle(base: str | Path) -> None:
    """Raise :class:`ReviewPending` when base is an unapproved bundle."""
    check_bundle_approved(base)

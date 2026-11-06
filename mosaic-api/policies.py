"""Versioned policy loading for mosaic-api (read-only mirror).

mosaic-api bundles the trust-engine policy files read-only so it can select
the active policy version per entity type and reject disallowed attributes
before calling the trust-engine. It never derives from them: ranks, TTLs and
verdicts are the engine's job. The .mg rules are the engine's; the JSON here
is only version selection + attribute allow-listing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from schemas import Policy


class PolicyError(RuntimeError):
    """No usable policy for the requested entity type."""


def policies_dir() -> Path:
    """Locate the bundled trust-engine policies directory.

    POLICY_DIR (env) wins so test/demo runs can point at a composed policy
    tree (e.g. repo policies plus a staged new version) without touching the
    baked-in directory. Otherwise: mosaic-api/policies, then
    trust-engine/policies.
    """
    override = os.environ.get("POLICY_DIR", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_dir():
            return candidate
        raise PolicyError(f"POLICY_DIR {override!r} is not a directory")
    here = Path(__file__).resolve().parent
    candidates = [
        here / "policies",
        here.parent / "trust-engine" / "policies",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise PolicyError("trust-engine policies directory not found")


def _entity_dirs(base: Path) -> list[Path]:
    """Every directory directly under the policy root is an entity type.

    A directory is a valid entity namespace iff its name is a lower-case
    slug (``user``, ``product``, ...) and it contains at least one ``*.json``.
    Anything else (``semantic``, stray files) is ignored. When two staged
    policy versions exist, the one whose ``policy_version`` sorts last wins —
    that is the documented composition convention: overlay a staged dataset
    policy (e.g. ``pol-user-olist-...``) on top of the repo defaults in one
    composed ``POLICY_DIR`` and the staged one wins.
    """
    if not base.is_dir():
        return []
    slug = re.compile(r"^[a-z][a-z0-9_]*$")
    out: list[Path] = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and slug.fullmatch(child.name) and any(child.glob("*.json")):
            out.append(child)
    return out


def load_policies(policy_dir: Path | None = None) -> dict[str, Policy]:
    """Load the latest policy version per entity type.

    Every entity type with at least one policy JSON under the root is loaded —
    no hard-coded entity list. Selection among staged versions is lexical on
    ``policy_version`` (the composition convention documented above). Fail
    closed when the root is missing or yields nothing — and fail closed when
    the root is (or sits under) an onboarding bundle whose review is
    pending human approval.
    """
    base = Path(policy_dir) if policy_dir else policies_dir()
    from bundle_review import ReviewPending, refuse_pending_bundle

    try:
        refuse_pending_bundle(base)
    except ReviewPending as exc:
        raise PolicyError(f"policy bundle refused: {exc}") from exc
    dirs = _entity_dirs(base)
    if not dirs:
        raise PolicyError(f"no entity policy directories under {base}")
    active: dict[str, Policy] = {}
    for entity_dir in dirs:
        versions: list[Policy] = []
        for path in sorted(entity_dir.glob("*.json")):
            versions.append(Policy.model_validate_json(path.read_text(encoding="utf-8")))
        if not versions:  # pragma: no cover - _entity_dirs already filtered this
            raise PolicyError(f"no policy JSON for {entity_dir.name}")
        versions.sort(key=lambda policy: policy.policy_version)
        active[entity_dir.name] = versions[-1]
    return active


def active_version(policies: dict[str, Policy], entity_type: str) -> Policy:
    """Return the active policy or fail closed on unknown entity types."""
    policy = policies.get(entity_type)
    if policy is None:
        raise PolicyError(f"no policy for entity_type {entity_type!r}; fail closed")
    return policy


def check_attributes(policy: Policy, attributes: list[str]) -> None:
    """Reject disallowed attributes before any fact crosses the boundary."""
    allowed = set(policy.allowed_attributes)
    for attribute in attributes:
        if attribute not in allowed:
            raise PolicyError(
                f"attribute {attribute!r} is not allowed by "
                f"policy {policy.policy_version}; fail closed"
            )

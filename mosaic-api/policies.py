"""Versioned policy loading for mosaic-api (read-only mirror).

mosaic-api bundles the trust-engine policy files read-only so it can select
the active policy version per entity type and reject disallowed attributes
before calling the trust-engine. It never derives from them: ranks, TTLs and
verdicts are the engine's job. The .mg rules are the engine's; the JSON here
is only version selection + attribute allow-listing.
"""

from __future__ import annotations

from pathlib import Path

from schemas import Policy


class PolicyError(RuntimeError):
    """No usable policy for the requested entity type."""


def policies_dir() -> Path:
    """Locate the bundled trust-engine policies directory."""
    here = Path(__file__).resolve().parent
    candidates = [
        here / "policies",
        here.parent / "trust-engine" / "policies",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise PolicyError("trust-engine policies directory not found")


def load_policies(policy_dir: Path | None = None) -> dict[str, Policy]:
    """Load the latest policy version per entity type (user, product)."""
    base = Path(policy_dir) if policy_dir else policies_dir()
    active: dict[str, Policy] = {}
    for entity_type in ("user", "product"):
        entity_dir = base / entity_type
        if not entity_dir.is_dir():
            raise PolicyError(f"no policy directory for {entity_type}")
        versions: list[Policy] = []
        for path in sorted(entity_dir.glob("*.json")):
            versions.append(
                Policy.model_validate_json(path.read_text(encoding="utf-8"))
            )
        if not versions:
            raise PolicyError(f"no policy JSON for {entity_type}")
        versions.sort(key=lambda policy: policy.policy_version)
        active[entity_type] = versions[-1]
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

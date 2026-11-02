"""Dynamic policy profiles: discovery, hashing, and the attestation root.

A profile is a pair of Manglekit files (``.json`` metadata + ``.mg`` Horn
clauses) staged under ``data/<profile>/policies/<entity_type>/``. That layout
is the one the trust-engine already discovers for staged auxiliary policies,
so adding a profile is a data drop: no Go change, no restart beyond SIGHUP.

Three profiles ship:

``strict_bcbs239``
    Zero-leakage compliance. Any winner disagreement quarantines (C1). This is
    the behaviour the dashboard showed before this feature: 244 of 247
    clusters quarantined on this dataset.

``retail_balanced``
    Commercial: source hierarchy (ERP 3 > verified order/OTP 2 > CRM 1) plus
    freshness inside the winning tier. A conflict only quarantines when two
    DIFFERENT systems assert it; duplicate rows inside one feed collapse to a
    golden. Repeatable business keys (``order_id``) are declared multivalued
    so they are never uniqueness-constrained -- that single declaration is
    what removes the mass quarantine.

``analytical_mesh``
    Feature store: every distinct observed value publishes. No ``esc``/``den``
    rules exist in the file, so zero-quarantine is guaranteed by construction
    rather than by hoping the rules do not fire.

Nothing here derives. Metrics come from the trust-engine's ``/metrics``
endpoint; this module only locates files, hashes them, and rolls results up.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# data/<dir> -> profile id. Ordered: the list order is the picker order.
_PROFILE_DIRS: dict[str, str] = {
    "strict-bcbs239": "strict_bcbs239",
    "retail-balanced": "retail_balanced",
    "analytical-mesh": "analytical_mesh",
}

_DEFAULT_PROFILE = "strict_bcbs239"

# Human-facing labels for the UI selector. Kept here (not in the frontend) so
# the label travels with the profile definition it describes.
_LABELS: dict[str, dict[str, str]] = {
    "strict_bcbs239": {
        "label": "Strict BCBS 239 (Zero Tolerance)",
        "summary": "Zero-leakage compliance: any winner disagreement quarantines.",
    },
    "retail_balanced": {
        "label": "Enterprise Commercial (Retail Balanced)",
        "summary": "Source hierarchy + freshness; only cross-system disputes quarantine.",
    },
    "analytical_mesh": {
        "label": "Analytical Mesh (Multi-Valued)",
        "summary": "Non-destructive multi-value lineage; zero quarantine by construction.",
    },
}


def repo_root() -> Path:
    """Repo root (mosaic-api/../)."""
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ProfilePolicy:
    """One entity type's half of a profile."""

    entity_type: str
    policy_version: str
    json_path: Path
    mg_path: Path

    @property
    def horn_clauses(self) -> str:
        return self.mg_path.read_text(encoding="utf-8")

    @property
    def policy_hash(self) -> str:
        """SHA-256 of the Horn clause text (PolicyHash in the spec)."""
        return hashlib.sha256(self.mg_path.read_bytes()).hexdigest()

    @property
    def metadata(self) -> dict[str, Any]:
        return json.loads(self.json_path.read_text(encoding="utf-8"))


def _clause_lines(horn: str, head: str) -> list[str]:
    """Every rule line in the Horn text whose head predicate is ``head``.

    Rules are matched on the head of the clause (before ``:-``) rather than
    by substring, so a comment mentioning ``esc(`` does not get picked up.
    """
    out: list[str] = []
    for line in horn.splitlines():
        stripped = line.strip()
        if stripped.startswith("%") or ":-" not in stripped:
            continue
        lhs = stripped.split(":-", 1)[0].strip()
        if lhs.startswith(head + "("):
            out.append(stripped)
    return out


@dataclass
class Profile:
    """A switchable policy profile spanning one or more entity types."""

    profile_id: str
    label: str
    summary: str
    policies: dict[str, ProfilePolicy] = field(default_factory=dict)

    @property
    def default_entity_type(self) -> str:
        return "user" if "user" in self.policies else next(iter(self.policies))

    @property
    def policy_versions(self) -> dict[str, str]:
        return {et: p.policy_version for et, p in self.policies.items()}

    @property
    def policy_hash(self) -> str:
        """Digest over every entity type's Horn clause hash.

        One value per profile even though a profile pins a different .mg per
        entity type, so the attestation root stays a single field.
        """
        digest_input = "".join(
            f"{et}={self.policies[et].policy_hash}\n" for et in sorted(self.policies)
        )
        return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()

    @property
    def horn_clauses(self) -> str:
        """All Horn clauses, concatenated per entity type (for the UI)."""
        return "\n".join(
            f"% ---- {et} ----\n{self.policies[et].horn_clauses}" for et in sorted(self.policies)
        )

    @property
    def rules(self) -> list[str]:
        """Distinct rule codes declared by the profile's metadata."""
        seen: list[str] = []
        for policy in self.policies.values():
            for rule in policy.metadata.get("rules", []):
                if rule not in seen:
                    seen.append(rule)
        return seen

    @property
    def compile_status(self) -> str:
        """What the UI shows as 'Compiled: N Stratified Horn Clauses'."""
        clauses = sum(
            1
            for line in self.horn_clauses.splitlines()
            if line.strip().endswith(".") and not line.strip().startswith("%")
        )
        return f"Compiled: {clauses} Stratified Horn Clauses"

    def invariant_clauses(self) -> dict[str, Any]:
        """The real clause behind each invariant card, extracted from the .mg.

        The cards used to show mockup fiction (``golden(ID) :- sap(...)``)
        that no profile ever contained. Pulling the actual clause means the
        card can never disagree with the rules that ran. ``quarantine`` is
        ``null`` when the profile defines no ``esc/`` rule at all --
        analytical_mesh is exactly that case, and the card must say so rather
        than show a rule that does not exist.
        """
        horn = self.horn_clauses
        esc_rules = _clause_lines(horn, "esc")
        # The Circuit Breaker card is about value conflicts, so prefer the
        # conflict rule over, say, the expiry rule (R3), which would be a
        # misleading exhibit for that invariant.
        conflict_markers = ("CLUSTER_CONFLICT", "WINNER_TIE_AMBIGUOUS", "CRITICAL_LOW_CONFIDENCE")
        quarantine = next(
            (r for r in esc_rules if any(marker in r for marker in conflict_markers)),
            esc_rules[0] if esc_rules else [],
        )
        transitive = _clause_lines(horn, "grounded")
        return {
            "authority": (_clause_lines(horn, "better") or [None])[0],
            "quarantine": quarantine or None,
            "transitive": (
                transitive[-1] if transitive else (_clause_lines(horn, "verified_hop") or [None])[0]
            ),
            "has_quarantine_rules": bool(esc_rules),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "policy_id": self.profile_id,
            "label": self.label,
            "summary": self.summary,
            "policy_versions": self.policy_versions,
            "policy_hash": self.policy_hash,
            "rules": self.rules,
            "compile_status": self.compile_status,
            "horn_clauses": self.horn_clauses,
            "entity_types": sorted(self.policies),
            "invariants": self.invariant_clauses(),
        }


# Parsed profiles are cached because load_profiles() reads six files off a
# (slow) mount and runs on every policy-switch request: measured 114 ms, which
# would dominate a switch that is otherwise a 0.001 ms lookup.
#
# Revalidation is time-based, not stat-per-call: listing and statting the
# profile tree costs about as much as reading it (also ~115 ms here), so
# checking mtime on every request would defeat the cache. A short TTL means a
# policy edit takes effect within _PROFILE_TTL seconds while switches inside
# that window cost nothing.
_PROFILE_TTL = 1.0
_profile_cache: dict[tuple, dict[str, Profile]] = {}
_profile_cache_at: dict[tuple, float] = {}


def _profile_stamp(root: Path | None) -> tuple:
    base = (root or repo_root()) / "data"
    stamp: list[tuple] = []
    for dirname in _PROFILE_DIRS:
        profile_dir = base / dirname / "policies"
        if not profile_dir.is_dir():
            stamp.append((dirname, None))
            continue
        for path in sorted(profile_dir.rglob("*")):
            if path.is_file():
                try:
                    st = path.stat()
                except OSError:
                    continue
                stamp.append((str(path), st.st_mtime_ns, st.st_size))
    return tuple(stamp)


def load_profiles(root: Path | None = None) -> dict[str, Profile]:
    """Discover every profile staged under ``data/``.

    A profile directory that is missing either half for an entity type is
    skipped rather than half-registered: a profile that resolves for ``user``
    but not ``product`` would silently evaluate mixed batches under the wrong
    policy.
    """
    now = time.monotonic()
    cached = _profile_cache.get(None) if root is None else None
    if cached is not None and now - _profile_cache_at.get(None, 0.0) < _PROFILE_TTL:
        return cached

    stamp = _profile_stamp(root)
    cached = _profile_cache.get(stamp)
    if cached is not None:
        _profile_cache[None if root is None else stamp] = cached
        _profile_cache_at[None if root is None else stamp] = now
        return cached

    base = (root or repo_root()) / "data"
    profiles: dict[str, Profile] = {}
    for dirname, profile_id in _PROFILE_DIRS.items():
        profile_dir = base / dirname / "policies"
        if not profile_dir.is_dir():
            continue
        policies: dict[str, ProfilePolicy] = {}
        for entity_dir in sorted(p for p in profile_dir.iterdir() if p.is_dir()):
            entity_type = entity_dir.name
            mg_files = sorted(entity_dir.glob("*.mg"))
            json_files = sorted(entity_dir.glob("*.json"))
            if not mg_files or not json_files:
                continue
            mg_path = mg_files[0]
            json_path = json_files[0]
            metadata = json.loads(json_path.read_text(encoding="utf-8"))
            policies[entity_type] = ProfilePolicy(
                entity_type=entity_type,
                policy_version=metadata.get("policy_version", mg_path.stem),
                json_path=json_path,
                mg_path=mg_path,
            )
        if not policies:
            continue
        label = _LABELS.get(profile_id, {})
        profiles[profile_id] = Profile(
            profile_id=profile_id,
            label=label.get("label", profile_id),
            summary=label.get("summary", ""),
            policies=policies,
        )
    key = None if root is None else stamp
    _profile_cache.clear()
    _profile_cache[key] = profiles
    _profile_cache_at.clear()
    _profile_cache_at[key] = now
    return profiles


def get_profile(profile_id: str, root: Path | None = None) -> Profile | None:
    return load_profiles(root).get(profile_id)


def default_profile_id() -> str:
    return _DEFAULT_PROFILE


def attestation_root(golden_facts_root: str, cluster_graph_root: str, policy_hash: str) -> str:
    """MerkleRoot = SHA256(GoldenFactsRoot || ClusterGraphRoot || PolicyHash).

    Folding the policy hash in means editing a single Horn clause changes the
    root even when no fact changes -- an attestation that stayed put across a
    policy edit would not attest to the policy that produced it.
    """
    material = f"{golden_facts_root}|{cluster_graph_root}|{policy_hash}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def stable_hash(value: Any) -> str:
    """SHA-256 over canonical JSON (sorted keys, no whitespace variance)."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

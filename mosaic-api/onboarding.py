"""Onboarding compiler: raw sources + expected result -> a derived bundle.

Zero-touch onboarding means the *only* inputs are the sources and the
expected result (an expected golden structure, plus optionally gold pairs).
Every artifact a human used to author is derived here, behind deterministic
gates:

1. **Field profiling** — column stats and value samples, straight from the
   input rows. No model involved.
2. **Schema mapping** (raw field -> role/canonical name). Two proposers feed
   one decision layer: (a) the deterministic alias match against the entity
   schema's ``rename_map``, (b) a model proposer (live Muse, or the mock
   heuristic when ``USE_MOCK_MUSE=true``). Fail-closed rules, always applied:
   an identity-shaped field that maps to nothing refuses the build
   (``unmapped_identity_field``); two fields claiming the primary label
   refuse (``ambiguous_primary_label``); a low-confidence identity field
   refuses (``low_confidence_identity_field``); anything else below the
   floor is withheld, never silently used.
3. **Taxonomy bootstrap** — observed values of categorical attributes become
   a versioned taxonomy (terms + synonyms), so T1 can pass on real runs and
   unknown terms still escalate. Written under the bundle, in the same
   versioned format inkind as steward-written taxonomies.
4. **Policy derivation** — the shipped rule core with a derived
   ``auth_rank`` block (every allowed attribute x observed source, equal
   ranks — a cross-source conflict therefore *always* quarantines, which is
   the correct conservative default under no evidence) and derived
   ``critical`` / ``categorical`` facts. The engine still decides; nothing
   here publishes anything.
5. **Bundle assembly** — manifest with every decision, hash, and artifact.

Nothing is ever auto-loaded from a bundle: the caller runs the build with
``POLICY_DIR``/``TAXONOMY_DIR`` (or the equivalent in-process arguments)
pointing at it, exactly like a staged policy profile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import REPO_ROOT, Settings
from schema_contract import EntitySchemaSpec, FieldMapping, get_schema_spec

logger = logging.getLogger(__name__)
REFUSAL_CODES = frozenset(
    {
        "unmapped_identity_field",
        "ambiguous_primary_label",
        "low_confidence_identity_field",
        "missing_rule_core",
        "ambiguous_entity_type",
        "missing_source",
    }
)


class OnboardRefused(Exception):
    """Machine-readable refusal. Never partial: nothing is half-derived."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        assert code in REFUSAL_CODES, code
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# 1. Profiling (no model)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldProfile:
    field: str
    non_null: int
    distinct: int
    distinct_ratio: float
    type_kind: str  # "number" | "boolean" | "string"
    samples: tuple[str, ...]


def _kind(values: list[str]) -> str:
    def looks_number(v: str) -> bool:
        try:
            float(v.replace(",", ""))
            return True
        except ValueError:
            return False

    def looks_bool(v: str) -> bool:
        return v.strip().lower() in {"true", "false", "0", "1", "yes", "no"}

    sample = values[:50]
    if sample and all(looks_number(v) for v in sample):
        return "number"
    if sample and all(looks_bool(v) for v in sample):
        return "boolean"
    return "string"


def profile_documents(
    documents: list[dict[str, Any]], *, max_samples: int = 5, max_scan: int = 2000
) -> dict[str, FieldProfile]:
    """Column stats straight from mosaic-format documents."""
    counters: dict[str, Counter[str]] = {}
    totals: Counter[str] = Counter()
    kinds: dict[str, list[str]] = {}
    for document in documents[:max_scan]:
        fields = document.get("fields") or {}
        for raw, payload in fields.items():
            value = payload.get("value") if isinstance(payload, dict) else payload
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            totals[raw] += 1
            counters.setdefault(raw, Counter())[text] += 1
            kinds.setdefault(raw, []).append(text)
    profiles: dict[str, FieldProfile] = {}
    for raw, counter in counters.items():
        samples = tuple(value for value, _ in counter.most_common(max_samples))
        distinct = len(counter)
        profiles[raw] = FieldProfile(
            field=raw,
            non_null=totals[raw],
            distinct=distinct,
            distinct_ratio=distinct / max(totals[raw], 1),
            type_kind=_kind(kinds[raw]),
            samples=samples,
        )
    return profiles


# ---------------------------------------------------------------------------
# 2. Schema mapping (proposers + one decision layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldProposal:
    raw: str
    canonical: str | None  # None => the proposer found nothing
    role: str | None
    confidence: float
    evidence: str
    source: str  # "spec_alias" | "taxonomy_alias" | "model"


@dataclass
class MappingResult:
    entity_type: str
    mapping: dict[str, str]  # raw -> canonical
    withheld: list[str]  # mapped below the floor: reported, not used
    proposals: list[FieldProposal] = field(default_factory=list)


_MODEL_MAPPING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "canonical_name", "confidence", "evidence"],
                "properties": {
                    "field": {"type": "string"},
                    "canonical_name": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "string"},
                },
            },
        },
    },
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.casefold()))


def _mock_propose(profile: FieldProfile, spec: EntitySchemaSpec) -> FieldProposal | None:
    """Deterministic stand-in for the model proposer.

    Token overlap between the raw field name and every alias/canonical name
    in the spec; the best scoring field wins. No fuzzy family names, no
    embeddings: honest and brittle by construction — unseen synonyms fall
    back to unmapped, which the decision layer handles explicitly.
    """
    raw_tokens = _tokens(profile.field)
    if not raw_tokens:
        return None
    scored: list[tuple[float, int, FieldMapping]] = []
    for mapping in spec.fields:
        if mapping.is_projection:
            continue  # projections derive from their source field; never proposed
        aliases = set(mapping.source_aliases) | {mapping.canonical_name}
        for alias in aliases:
            alias_tokens = _tokens(alias)
            if not alias_tokens:
                continue
            overlap = len(raw_tokens & alias_tokens)
            if overlap:
                scored.append((overlap / len(alias_tokens), overlap, mapping))
    if not scored:
        return FieldProposal(profile.field, None, None, 0.0, "no alias overlap", "model")
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    top_score = scored[0][0]
    top_targets = {item[2].canonical_name for item in scored if item[0] == top_score}
    if len(top_targets) > 1:
        # a tie across different contract fields is a coin flip, not a mapping
        return FieldProposal(
            profile.field,
            None,
            None,
            0.0,
            f"ambiguous alias overlap across {sorted(top_targets)}",
            "model",
        )
    score, overlap, mapping = scored[0]
    return FieldProposal(
        profile.field,
        mapping.canonical_name,
        mapping.role,
        min(0.95, 0.6 + 0.3 * score),
        f"token overlap {score:.2f} with alias of {mapping.canonical_name}",
        "model",
    )


def _model_mapping_prompt(profile_rows: list[FieldProfile], spec: EntitySchemaSpec) -> str:
    lines = [
        f"You are mapping columns of a {spec.entity_noun} dataset onto a fixed contract.",
        "Return JSON only, matching the schema. For each column, say which canonical",
        "contract field it holds, or null when none fits. Be conservative: an",
        "approximate guess is worse than null, and the caller refuses rather",
        "than guess.",
        "",
        "Contract fields and their roles:",
    ]
    for mapping in spec.visible_to_judge():
        lines.append(f"- {mapping.canonical_name} ({mapping.role})")
    hidden = [f.canonical_name for f in spec.fields if f.role == "private_identifier"]
    lines.append("Private identifiers you must also map when asked: " + ", ".join(hidden))
    lines.append("")
    lines.append("Columns (stats and samples):")
    for row in profile_rows:
        lines.append(
            f"- {row.field}: type={row.type_kind} n={row.non_null} "
            f"distinct={row.distinct} samples={list(row.samples)}"
        )
    return "\n".join(lines)


async def _live_propose(
    profile_rows: list[FieldProfile], spec: EntitySchemaSpec, settings: Settings
) -> dict[str, FieldProposal]:
    from meta_client import chat_json  # local import: model client is optional at rest

    parsed, _usage = await chat_json(
        settings,
        model=settings.muse_model_id,
        system=_model_mapping_prompt(profile_rows, spec),
        user="Map the columns.",
        schema_name="mosaic_onboarding_mapping",
        schema=_MODEL_MAPPING_SCHEMA,
        reasoning_effort="low",
        prompt_cache_key="mosaic-onboarding-mapping-v1",
    )
    by_canonical = {m.canonical_name: m for m in spec.fields}
    proposals: dict[str, FieldProposal] = {}
    for item in parsed.get("proposals", []):
        raw = item.get("field")
        canonical = item.get("canonical_name")
        if not raw:
            continue
        if canonical is not None and canonical not in by_canonical:
            canonical = None  # a canonical outside the contract never lands
        proposals[raw] = FieldProposal(
            raw,
            canonical,
            by_canonical[canonical].role if canonical else None,
            float(item.get("confidence", 0.0) or 0.0),
            str(item.get("evidence", ""))[:300],
            "model",
        )
    return proposals


def _key(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def _decision_layer(
    profiles: dict[str, FieldProfile],
    spec: EntitySchemaSpec,
    model: dict[str, FieldProposal],
    taxonomy_aliases: dict[str, list[str]] | None,
    confidence_floor: float,
) -> MappingResult:
    primary = spec.primary_label.canonical_name
    by_contract = {f.canonical_name: f for f in spec.fields}
    mapping: dict[str, str] = {}
    withheld: list[str] = []
    proposals: list[FieldProposal] = []

    # Documents that already speak canonical names need no mapping.
    for raw in sorted(profiles):
        if raw in by_contract:
            mapping[raw] = raw

    # 1) deterministic alias pass (taxonomy overrides spec aliases)
    spec_lookup = spec.rename_map()
    tax_lookup: dict[str, str] = {}
    for canonical, aliases in (taxonomy_aliases or {}).items():
        for alias in aliases or []:
            tax_lookup[_key(alias)] = canonical
    for raw in sorted(profiles):
        if raw in mapping:
            continue
        key = _key(raw)
        hit = tax_lookup.get(key) or spec_lookup.get(key)
        if hit:
            mapping[raw] = hit
            proposals.append(
                FieldProposal(
                    raw,
                    hit,
                    by_contract[hit].role,
                    1.0,
                    "exact alias match in the contract",
                    "spec_alias",
                )
            )

    # 2) model proposer for the rest (mock heuristic or live Muse)
    for raw in sorted(profiles):
        if raw in mapping:
            continue
        proposal = model.get(raw) or FieldProposal(
            raw, None, None, 0.0, "proposer said nothing", "model"
        )
        proposals.append(proposal)
        if proposal.canonical is None:
            continue
        if proposal.confidence < confidence_floor:
            withheld.append(raw)
            continue
        mapping[raw] = proposal.canonical

    # 3) fail-closed identity rules
    claimed_primary = {raw: c for raw, c in mapping.items() if c == primary}
    if len(claimed_primary) > 1:
        raise OnboardRefused(
            "ambiguous_primary_label",
            "two columns claim the primary label; refusing to pick one",
            details={"fields": sorted(claimed_primary), "entity_type": spec.entity_type},
        )

    identity_below_floor = [
        p
        for p in proposals
        if p.canonical == primary and p.confidence < confidence_floor and p.raw in withheld
    ]
    if identity_below_floor:
        raise OnboardRefused(
            "low_confidence_identity_field",
            "a column looks like the primary label but cannot be mapped with "
            "confidence; refusing rather than grouping on a guess",
            details={
                "fields": [p.raw for p in identity_below_floor],
                "entity_type": spec.entity_type,
            },
        )

    primary_present = primary in mapping.values() or primary in profiles
    if not primary_present:
        raise OnboardRefused(
            "unmapped_identity_field",
            "no column maps onto the primary label; the build cannot group "
            "these records without guessing an identity",
            details={
                "entity_type": spec.entity_type,
                "contract_primary": primary,
                "columns": sorted(profiles),
            },
        )
    return MappingResult(
        entity_type=spec.entity_type,
        mapping=mapping,
        withheld=withheld,
        proposals=proposals,
    )


async def derive_mapping(
    documents: list[dict[str, Any]],
    *,
    entity_type: str | None,
    settings: Settings,
    taxonomy_aliases: dict[str, list[str]] | None = None,
) -> MappingResult:
    """Derive the raw->canonical mapping; raise OnboardRefused on any doubt.

    ``entity_type`` may be omitted only when every document declares the same
    one; mixed or missing declarations refuse rather than guess.
    """
    declared = {doc.get("entity_type") for doc in documents}
    declared.discard(None)
    if entity_type is None:
        if len(declared) != 1:
            raise OnboardRefused(
                "ambiguous_entity_type",
                "documents declare mixed or missing entity types; pass " "--entity-type explicitly",
                details={"declared": sorted(declared)},
            )
        entity_type = declared.pop()
    get_schema_spec(entity_type)  # raises for unregistered types — fail closed
    spec = get_schema_spec(entity_type)
    docs = [d for d in documents if (d.get("entity_type") or entity_type) == entity_type]
    profiles = profile_documents(docs)
    if not profiles:
        raise OnboardRefused(
            "unmapped_identity_field",
            "no non-empty field was found in the input documents",
            details={"entity_type": entity_type},
        )
    if settings.use_mock_muse:
        model = {}
        for raw, prof in profiles.items():
            proposal = _mock_propose(prof, spec)
            if proposal is not None:
                model[raw] = proposal
    else:
        model = await _live_propose(list(profiles.values()), spec, settings)
    floor = 0.5 if settings.use_mock_muse else 0.7
    return _decision_layer(profiles, spec, model, taxonomy_aliases, floor)


# ---------------------------------------------------------------------------
# 3. Taxonomy bootstrap (observed values become a versioned taxonomy)
# ---------------------------------------------------------------------------


def _is_number(value: str) -> bool:
    try:
        float(value.replace(",", ""))
        return True
    except ValueError:
        return False


def _matching_document_fields(
    documents: list[dict[str, Any]], mapping: dict[str, str]
) -> dict[str, Counter[str]]:
    counters: dict[str, Counter[str]] = {}
    for document in documents:
        fields = document.get("fields") or {}
        for raw, canonical in mapping.items():
            payload = fields.get(raw)
            if not isinstance(payload, dict):
                continue
            value = payload.get("value")
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            counters.setdefault(canonical, Counter())[text] += 1
    return counters


def derive_taxonomy_bootstrap(
    documents: list[dict[str, Any]],
    *,
    entity_type: str,
    mapping: dict[str, str],
    expected_structure: dict[str, Any] | None,
    max_terms: int = 500,
) -> dict[str, Any]:
    """Build the bootstrap taxonomy for the bundle.

    A field is categorical when it is low-cardinality empirically (few
    distinct values relative to its population) or declared with ``enum`` in
    the expected structure. Observed values become terms (the most common
    spelling per case-insensitive group wins; the others become synonyms) --
    so the run derives green on the values it saw, and unknown values still
    escalate under T1 as they should.
    """
    enum_attrs: set[str] = set()
    if isinstance(expected_structure, dict):
        for name, schema in (expected_structure.get("properties") or {}).items():
            if isinstance(schema, dict) and schema.get("enum"):
                enum_attrs.add(name)

    counters = _matching_document_fields(documents, mapping)
    attribute_aliases: dict[str, list[str]] = {}
    for raw, canonical in mapping.items():
        attribute_aliases.setdefault(canonical, []).append(raw)

    chosen: dict[str, Counter[str]] = {}
    for canonical, counter in counters.items():
        if not counter:
            continue
        distinct = len(counter)
        total = sum(counter.values())
        sample = list(counter.elements())[:50]
        all_numeric = all(_is_number(v) for v in sample)
        low_cardinality = (
            distinct <= max_terms
            and distinct / max(total, 1) <= 0.25
            and not all_numeric  # a year is not a taxonomy label
        )
        if low_cardinality or canonical in enum_attrs:
            chosen[canonical] = counter

    terms: list[dict[str, Any]] = []
    attributes: list[str] = []
    enum_values: dict[str, list[str]] = {}
    if isinstance(expected_structure, dict):
        for name, schema in (expected_structure.get("properties") or {}).items():
            if isinstance(schema, dict) and isinstance(schema.get("enum"), list):
                enum_values[name] = [str(v) for v in schema["enum"]]
    for canonical in sorted(chosen):
        counter = chosen[canonical]
        declared_enum = enum_values.get(canonical, [])
        variants: dict[str, list[str]] = {}
        for value in declared_enum or list(counter):
            variants.setdefault(_key(value), []).append(value)
        attr_terms: list[dict[str, str]] = []
        for group in variants.values():
            by_count = sorted(group, key=lambda v: counter.get(v, 0), reverse=True)
            head, rest = by_count[0], by_count[1:]
            attr_terms.append({"term": head, "synonyms": rest})
            if len(attr_terms) >= max_terms:
                break
        attributes.append(canonical)
        terms.extend(attr_terms)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    digest = hashlib.sha256(
        json.dumps(sorted(terms, key=lambda t: t["term"]), ensure_ascii=False).encode()
    ).hexdigest()[:8]
    return {
        "file": {
            "taxonomy_version": f"tax-{entity_type}-onboard-{stamp}-{digest}",
            "entity_type": entity_type,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "attribute_aliases": attribute_aliases,
            "terms": terms,
        },
        # meta lives in the manifest only; the engine's strict decode rejects
        # any extra field in the taxonomy artifact
        "categorical_attributes": attributes,
    }


# ---------------------------------------------------------------------------
# 4. Policy derivation (shipped rule core + derived facts)
# ---------------------------------------------------------------------------

_SPL = {column: i for i, column in enumerate(("email", "phone", "order_id", "category", "name"))}


def _rule_core_path() -> Path:
    candidates = [
        REPO_ROOT / "trust-engine" / "policies" / "user" / "pol-2026-11-02-v1.mg",
        Path(__file__).resolve().parent / "policies" / "user" / "pol-2026-11-02-v1.mg",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise OnboardRefused(
        "missing_rule_core",
        "the shared rule core (pol-2026-11-02-v1.mg) is not available to derive "
        "the policy from; refusing rather than inventing rules",
        details={"tried": [str(c) for c in candidates]},
    )


def derive_policy(
    documents: list[dict[str, Any]],
    *,
    entity_type: str,
    mapping: dict[str, str],
    expected_structure: dict[str, Any] | None,
    categorical_attributes: list[str],
    policy_version: str,
) -> dict[str, Any]:
    """Derive the policy JSON + compiled .mg (in memory)."""
    sources = sorted({d.get("source") for d in documents if d.get("source")})
    if not sources:
        raise OnboardRefused(
            "missing_source",
            "no document carries a 'source'; authority ranks cannot be derived "
            "and every claim would be refused by the engine",
        )
    properties = {}
    required: list[str] = []
    if isinstance(expected_structure, dict):
        properties = expected_structure.get("properties") or {}
        required = list(expected_structure.get("required") or [])

    canon = sorted(set(mapping.values()))
    allowed = sorted(set(properties) | set(canon) | set(required))
    if not allowed:
        allowed = canon
    # Authority matrix from observed ranks (mode per attribute x source),
    # ties broken to the *lower* rank (stronger authority). A source the run
    # never declared a rank for defaults to 1. This keeps the engine's
    # fact-vs-matrix check coherent while preserving any rank ordering the
    # caller actually declared in the documents.
    raw_to_canon = dict(mapping)
    votes: dict[tuple[str, str], Counter[int]] = {}
    for document in documents:
        source = document.get("source")
        if not source:
            continue
        for raw, payload in (document.get("fields") or {}).items():
            canonical = raw_to_canon.get(raw) or (raw if raw in canon else None)
            if canonical is None or not isinstance(payload, dict):
                continue
            rank = payload.get("authority_rank")
            if isinstance(rank, int) and not isinstance(rank, bool) and rank >= 1:
                votes.setdefault((canonical, source), Counter())[rank] += 1

    def mode_rank(attr: str, source: str) -> int:
        counter = votes.get((attr, source))
        if not counter:
            return 1
        top = max(counter.values())
        return min(rank for rank, count in counter.items() if count == top)

    authority_matrix = {
        attr: {source: mode_rank(attr, source) for source in sources} for attr in allowed
    }
    golden = list(allowed)  # matches both shipped profiles' semantics
    critical = sorted(set(required) & set(allowed))
    blocking = [c for c in ("email", "phone", "order_id", "category", "name") if c in allowed][:3]

    policy = {
        "policy_version": policy_version,
        "entity_type": entity_type,
        "allowed_attributes": allowed,
        "golden_attributes": golden,
        "authority_matrix": authority_matrix,
        "critical_attributes": critical,
        "rules": ["R1", "R2", "R3", "R4", "L1", "C1", "C2", "T1", "I1", "S1", "S2", "S3"],
        "thresholds": {
            "note": "onboarding-derived: equal authority tiers, strict conflicts; "
            "tune with evidence before scale",
        },
        "ttls_seconds": {},
        "splink": {"threshold": 0.9, "blocking": blocking},
    }

    core = _rule_core_path().read_text(encoding="utf-8")
    mg_lines = []
    inserted = False
    for line in core.splitlines():
        if line.startswith("auth_rank("):
            if not inserted:
                for attr in allowed:
                    for source in sources:
                        mg_lines.append(
                            f'auth_rank("{attr}", "{source}", {authority_matrix[attr][source]}).'
                        )
                for attr in critical:
                    mg_lines.append(f'critical("{attr}").')
                inserted = True
            continue
        if line.startswith("critical("):
            continue  # derived critical() lines were emitted with auth_rank
        if line.startswith('categorical("__none__").'):
            if categorical_attributes:
                for attr in categorical_attributes:
                    mg_lines.append(f'categorical("{attr}").')
            else:
                mg_lines.append('categorical("__none__").')
            continue
        line = _replace_header(line, policy_version, entity_type)
        mg_lines.append(line)
    mg = "\n".join(mg_lines) + "\n"
    return {"policy_json": policy, "mg": mg, "blocking": blocking, "sources": sources}


def _replace_header(line: str, policy_version: str, entity_type: str) -> str:
    if line.startswith("% policy_version:"):
        return f"% policy_version: {policy_version}"
    if line.startswith("% entity_type:"):
        return f"% entity_type: {entity_type}"
    if line.startswith("% derived_by:") or line.startswith("% derived:"):
        return line
    return line


# ---------------------------------------------------------------------------
# 5. Bundle assembly (all-or-nothing; nothing written on refusal)
# ---------------------------------------------------------------------------


def _stable_digest(documents: list[dict[str, Any]]) -> str:
    blob = json.dumps(documents, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def assemble_bundle(
    documents: list[dict[str, Any]],
    *,
    entity_type: str | None = None,
    expected_structure: dict[str, Any] | None = None,
    out_root: Path | str = REPO_ROOT / "data" / "onboarding",
    settings: Settings,
    taxonomy_aliases: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Derive and write a self-contained onboarding bundle.

    Raises :class:`OnboardRefused` on any doubt; in that case **no file
    under ``out_root`` is created**. On success returns the manifest dict.
    """
    from taxonomies import write_text_atomic

    if not documents:
        raise OnboardRefused("unmapped_identity_field", "no documents supplied", details={})
    docs_sha = _stable_digest(documents)
    structure_sha = (
        hashlib.sha256(
            json.dumps(expected_structure, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if expected_structure is not None
        else None
    )

    mapping_result = await derive_mapping(
        documents,
        entity_type=entity_type,
        settings=settings,
        taxonomy_aliases=taxonomy_aliases,
    )
    entity_type = mapping_result.entity_type
    mapping = mapping_result.mapping

    taxonomy_bundle = derive_taxonomy_bootstrap(
        documents,
        entity_type=entity_type,
        mapping=mapping,
        expected_structure=expected_structure,
    )
    taxonomy = taxonomy_bundle["file"]
    categorical_attributes = taxonomy_bundle["categorical_attributes"]

    digest = docs_sha[:8]
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    policy_name = f"pol-{entity_type}-onboard-{stamp}-{digest}"
    policy = derive_policy(
        documents,
        entity_type=entity_type,
        mapping=mapping,
        expected_structure=expected_structure,
        categorical_attributes=categorical_attributes,
        policy_version=policy_name,
    )

    from policy_copilot import verify_with_engine

    engine_check = verify_with_engine(policy["mg"])
    if engine_check.get("engine_available") and engine_check.get("compiles") is False:
        raise OnboardRefused(
            "missing_rule_core",
            "the derived policy does not compile under the engine; refusing "
            "to ship a policy that cannot be validated",
            details={"error": engine_check.get("error")},
        )

    out = Path(out_root)
    bundle = out / f"{entity_type}-{stamp}-{digest}"
    if bundle.exists():
        # replay determinism: re-deriving the identical input reuses the
        # existing bundle (verified by content digest) instead of refusing
        manifest_path = bundle / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = None
            if (
                manifest
                and manifest.get("inputs", {}).get("documents_sha256") == docs_sha
                and manifest.get("inputs", {}).get("expected_structure_sha256") == structure_sha
                and manifest.get("entity_type") == entity_type
            ):
                manifest["bundle_dir"] = str(bundle)
                manifest["reused"] = True
                return manifest
        raise OnboardRefused(
            "missing_rule_core",
            f"bundle directory already exists but was not derived from this " f"input: {bundle}",
            details={"path": str(bundle)},
        )

    manifest = {
        "bundle_version": 1,
        "derived_by": "muse-mosaic onboarding",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "entity_type": entity_type,
        "inputs": {
            "document_count": len(documents),
            "documents_sha256": docs_sha,
            "expected_structure_sha256": structure_sha,
        },
        "mapping": {
            "map": mapping,
            "withheld": mapping_result.withheld,
            "confidence_floor": 0.5 if settings.use_mock_muse else 0.7,
            "taxonomy_aliases": taxonomy_aliases is not None,
        },
        "taxonomy": {
            "taxonomy_version": taxonomy["taxonomy_version"],
            "terms": len(taxonomy["terms"]),
            "categorical_attributes": categorical_attributes,
        },
        "policy": {
            "policy_version": policy_name,
            "authority_tiers": "observed mode per attribute x source "
            "(ties to the lower rank); unseen sources default to 1",
            "conflict_semantics": "strict (any winners_disagree quarantines)",
            "splink_blocking": policy["blocking"],
            "critical_attributes": policy["policy_json"]["critical_attributes"],
            "engine_check": engine_check,
        },
        "expected_structure": "expected_structure.json" if expected_structure else None,
        "artifacts": {
            "policy_json": f"policies/{entity_type}/{policy_name}.json",
            "policy_mg": f"policies/{entity_type}/{policy_name}.mg",
            "taxonomy": f"taxonomy/{taxonomy['taxonomy_version']}.json",
        },
    }

    bundle.mkdir(parents=True, exist_ok=False)
    (bundle / "policies" / entity_type).mkdir(parents=True)
    (bundle / "taxonomy").mkdir()
    try:
        write_text_atomic(
            bundle / "policies" / entity_type / f"{policy_name}.json",
            json.dumps(policy["policy_json"], indent=2, ensure_ascii=False) + "\n",
        )
        write_text_atomic(bundle / "policies" / entity_type / f"{policy_name}.mg", policy["mg"])
        write_text_atomic(
            bundle / "taxonomy" / f"{taxonomy['taxonomy_version']}.json",
            json.dumps(taxonomy, indent=2, ensure_ascii=False) + "\n",
        )
        if expected_structure is not None:
            write_text_atomic(
                bundle / "expected_structure.json",
                json.dumps(expected_structure, indent=2, ensure_ascii=False) + "\n",
            )
        write_text_atomic(
            bundle / "manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        )
    except Exception:
        # all-or-nothing: remove a partial bundle so a failed derive never
        # occupies the bundle namespace
        import shutil

        shutil.rmtree(bundle, ignore_errors=True)
        raise
    manifest["bundle_dir"] = str(bundle)
    return manifest

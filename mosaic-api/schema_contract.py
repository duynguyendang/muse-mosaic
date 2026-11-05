"""Declarative entity schema contract (G1 + G3 generalization).

Two stages, both data:

1. :class:`EntitySchemaSpec` says what each field of an entity *is*: its role
   in a matching decision, how its values are normalised, and how permissive
   admission is for that entity type.
2. :func:`compile_judge_prompt` turns that declaration into the question the
   judge is asked, so a new entity type is a file drop rather than a new
   prompt string in Python.

Two rules live here rather than at the call sites:

* A field with ``role="private_identifier"`` (phone, email) is never visible to
  the judge. :meth:`EntitySchemaSpec.visible_to_judge` is the single gate, used
  both to build the profile payload and to compile the prompt, so the boundary
  cannot be forgotten at one of the two call sites.
* A field whose normalizer is a *projection* (``edition_tokens``) derives its
  text from another field; ``source_aliases`` names where that text lives. Such
  a field never claims an alias as a rename target and is never read by
  canonical name -- otherwise ``title`` would map to both ``name`` and
  ``edition_hints``. Alias resolution for every other field is
  ``canonicalize_documents``' job, which is why reading a raw ``full_name``
  straight out of a document still yields nothing.

Verdict cache keys carry ``schema_version`` plus the hash of the prompt that
was actually used (``muse_candidates._pair_key``), so editing a role or a
prompt can never reuse a verdict computed under the old contract.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

RoleType = Literal[
    "primary_label",
    "anchor_group",
    "hard_discriminator",
    "corroborating",
    "private_identifier",
    "ignored",
]

NormalizerType = Literal[
    "text_fold",
    "preserve_punct",
    "root_brand_token",
    "edition_tokens",
    "phone_e164",
    "email_lower",
]

# A projection derives its value from another field; its source_aliases say
# which field, and it never participates in alias->canonical renaming.
PROJECTION_NORMALIZERS: frozenset[str] = frozenset({"edition_tokens"})

# Stripped by root_brand_token: corporate suffixes that do not change which
# brand a record belongs to ("Adobe Systems Inc" and "Adobe" are one anchor).
BRAND_STOP_WORDS: frozenset[str] = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "ltd",
        "llc",
        "co",
        "company",
        "systems",
        "software",
        "international",
    }
)

# Edition / platform / licence-tier markers that separate two products which
# share a title ("CS3 Upgrade (Mac)" is not "CS3 Full (Windows)").
_EDITION_RE = re.compile(
    r"\b(upgrade|upg|academic|edu|oem|retail|full|mac|windows|win|linux|pro|"
    r"professional|standard|std|enterprise|ent|home|student|server|deluxe|"
    r"premier|suite|bundle|\d+\s*-?\s*user|\d+\s*-?\s*pc|\d+\s*-?\s*license|"
    r"v\d+(?:\.\d+)?|cs\d+|20\d\d)\b",
    re.IGNORECASE,
)

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_PRESERVE_KEEP = re.compile(r"[^\w.\-/ $]", re.UNICODE)


@dataclass(frozen=True)
class FieldMapping:
    """One field of an entity: where it comes from and what it may decide."""

    canonical_name: str
    source_aliases: tuple[str, ...]
    role: RoleType
    normalizer: NormalizerType
    judge_instruction: str | None = None

    @property
    def is_projection(self) -> bool:
        return self.normalizer in PROJECTION_NORMALIZERS


@dataclass(frozen=True)
class AdmissionPolicy:
    """How permissive the shortlist is for one entity type.

    Person admission corroborates the label with an address (``min_label_with_
    discriminator`` + ``min_discriminator_score``), because shared namesakes
    are the trap. Products and publications have no address to corroborate
    with, so they are admitted on the label floor alone and the *judge* -- not
    a string threshold -- decides the middle band. That floor is deliberately
    low: measured true-pair similarity sits at a 0.69 / 0.75 median on
    Amazon-Google / DBLP-ACM, so a 0.6 gate discards a third of the truth.
    """

    min_label_score: float
    min_label_with_discriminator: float
    min_discriminator_score: float
    per_cluster_cap: int = 3
    split_bucket_at: int = 8
    require_house_number_guard: bool = False


@dataclass(frozen=True)
class EntitySchemaSpec:
    """The contract for one entity type: fields, admission, and the question."""

    schema_version: str
    entity_type: str
    entity_noun: str
    link_proof_type: str
    admission: AdmissionPolicy
    fields: tuple[FieldMapping, ...]

    # --- queries -------------------------------------------------------

    def fields_by_role(self, role: RoleType) -> tuple[FieldMapping, ...]:
        return tuple(f for f in self.fields if f.role == role)

    @property
    def primary_label(self) -> FieldMapping:
        first = self.fields_by_role("primary_label")
        if not first:
            raise ValueError(f"schema {self.schema_version} declares no primary_label")
        return first[0]

    @property
    def anchor_group(self) -> FieldMapping | None:
        anchors = self.fields_by_role("anchor_group")
        return anchors[0] if anchors else None

    @property
    def projections(self) -> tuple[FieldMapping, ...]:
        return tuple(f for f in self.fields if f.is_projection)

    def visible_to_judge(self) -> tuple[FieldMapping, ...]:
        """Every field the judge may see -- and no others.

        This is the PII boundary: private identifiers and ignored fields are
        excluded by construction, for both the profile payload and the prompt.
        """
        return tuple(f for f in self.fields if f.role not in ("private_identifier", "ignored"))

    def field_named(self, canonical_name: str) -> FieldMapping | None:
        for mapping in self.fields:
            if mapping.canonical_name == canonical_name:
                return mapping
        return None

    def rename_map(self) -> dict[str, str]:
        """alias -> canonical name, for canonicalize_documents.

        Projections are excluded (they do not own their aliases) and a
        canonical name never maps to itself.
        """
        lookup: dict[str, str] = {}
        for mapping in self.fields:
            if mapping.is_projection:
                continue
            for alias in mapping.source_aliases:
                lookup[_key(alias)] = mapping.canonical_name
        lookup.pop(_key(self.primary_label.canonical_name), None)
        for mapping in self.fields:
            lookup.pop(_key(mapping.canonical_name), None)
        return lookup


def _key(value: str) -> str:
    """Alias comparison key: case- and separator-insensitive."""
    return value.strip().lower().replace(" ", "_")


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def apply_normalizer(value: str, mode: NormalizerType) -> str:
    """Apply one declared normalizer. Pure and total: never raises on input.

    ``text_fold`` is the matching function (punctuation becomes space);
    ``preserve_punct`` keeps the characters a judge must read intact -- a price
    of ``$19.99`` stays ``19.99``, it does not become ``19 99``.
    """
    if mode == "text_fold":
        return _WS.sub(" ", _NON_WORD.sub(" ", _fold(value))).strip()
    if mode == "preserve_punct":
        kept = _WS.sub(" ", _PRESERVE_KEEP.sub(" ", _fold(value))).strip()
        # a currency SIGN before a number is not part of the number
        return kept.lstrip("$").strip()
    if mode == "root_brand_token":
        tokens = [
            t for t in apply_normalizer(value, "text_fold").split() if t not in BRAND_STOP_WORDS
        ]
        return " ".join(tokens)
    if mode == "edition_tokens":
        found = {_WS.sub("", match.group(0)).lower() for match in _EDITION_RE.finditer(value)}
        return " ".join(sorted(found))
    if mode == "phone_e164":
        head = re.split(r"[xX]", value, maxsplit=1)[0]
        digits = re.sub(r"\D", "", head)
        return f"+{digits}" if digits else ""
    if mode == "email_lower":
        return value.strip().lower()
    raise ValueError(f"unknown normalizer {mode!r}")


# ---------------------------------------------------------------------------
# The shipped specs (as data)
#
# Each entity's CONTRACT is a versioned JSON file under data/schemas/ — a new
# entity type is a data drop, not a code edit (G1). Python keeps only the
# machinery: normalizers, the prompt compiler, the privacy gate, and the
# loader that validates every spec in the directory and fails closed on any
# defect (a bad spec never half-loads; importing this module raises).
#
# `user` and `product` ship a /build policy plus a golden template, so a
# document list in those types is judgeable end-to-end. `publication` ships a
# spec so the DeepMatcher benchmark (scripts/eval_deepmatcher.py) can
# measure the arm; no publication policy ships yet, so a publication dataset
# never reaches clustering in /build and Muse there is unreachable by design.
# ---------------------------------------------------------------------------


class SchemaContractError(RuntimeError):
    """A spec file is missing, unreadable or incoherent. Fail closed."""


_TOKEN_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ROLE_SET = frozenset(get_args(RoleType))
_NORMALIZER_SET = frozenset(get_args(NormalizerType))


def specs_dir() -> Path:
    """Locate the specs directory: SPECS_DIR env, repo data/schemas, or image copy."""
    override = os.environ.get("SPECS_DIR", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_dir():
            return candidate
        raise SchemaContractError(f"SPECS_DIR {override!r} is not a directory")
    here = Path(__file__).resolve().parent
    candidates = [here.parent / "data" / "schemas", here / "data" / "schemas"]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise SchemaContractError(
        "schemas directory not found (looked in SPECS_DIR and repo data/schemas)"
    )


def _mapping_from(raw_field: object, path: Path) -> FieldMapping:
    if not isinstance(raw_field, dict):
        raise SchemaContractError(f"{path}: each field must be a JSON object")
    name = raw_field.get("canonical_name")
    if not isinstance(name, str) or not _TOKEN_SLUG.fullmatch(name):
        raise SchemaContractError(f"{path}: canonical_name {name!r} is not a lower-case slug")
    raw_aliases = raw_field.get("source_aliases")
    if not isinstance(raw_aliases, list) or not all(
        isinstance(a, str) and a.strip() for a in raw_aliases
    ):
        raise SchemaContractError(
            f"{path}: field {name!r} needs source_aliases as a list of strings"
        )
    role = raw_field.get("role")
    if role not in _ROLE_SET:
        raise SchemaContractError(f"{path}: field {name!r} has unknown role {role!r}")
    normalizer = raw_field.get("normalizer")
    if normalizer not in _NORMALIZER_SET:
        raise SchemaContractError(f"{path}: field {name!r} has unknown normalizer {normalizer!r}")
    instruction = raw_field.get("judge_instruction")
    if instruction is not None and not isinstance(instruction, str):
        raise SchemaContractError(f"{path}: field {name!r} judge_instruction must be a string")
    if normalizer in PROJECTION_NORMALIZERS and not raw_aliases:
        raise SchemaContractError(
            f"{path}: field {name!r} is a projection and declares no source_aliases to derive from"
        )
    return FieldMapping(
        canonical_name=name,
        source_aliases=tuple(raw_aliases),
        role=role,  # type: ignore[arg-type]
        normalizer=normalizer,  # type: ignore[arg-type]
        judge_instruction=instruction,
    )


def _admission_from(raw_admission: object, path: Path) -> AdmissionPolicy:
    if not isinstance(raw_admission, dict):
        raise SchemaContractError(f"{path}: admission must be a JSON object")

    def _score(key: str) -> float:
        value = raw_admission.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise SchemaContractError(f"{path}: admission.{key} must be a number")
        if not 0.0 <= float(value) <= 1.0:
            raise SchemaContractError(f"{path}: admission.{key} must lie in [0, 1]")
        return float(value)

    def _positive_int(key: str) -> int:
        value = raw_admission.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SchemaContractError(f"{path}: admission.{key} must be an integer >= 1")
        return value

    guard = raw_admission.get("require_house_number_guard", False)
    if not isinstance(guard, bool):
        raise SchemaContractError(f"{path}: admission.require_house_number_guard must be a boolean")
    return AdmissionPolicy(
        min_label_score=_score("min_label_score"),
        min_label_with_discriminator=_score("min_label_with_discriminator"),
        min_discriminator_score=_score("min_discriminator_score"),
        per_cluster_cap=_positive_int("per_cluster_cap"),
        split_bucket_at=_positive_int("split_bucket_at"),
        require_house_number_guard=guard,
    )


def _parse_spec(path: Path) -> EntitySchemaSpec:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaContractError(f"{path}: cannot read spec: {exc}") from exc
    if not isinstance(raw, dict):
        raise SchemaContractError(f"{path}: spec must be a JSON object")

    entity_type = raw.get("entity_type")
    filename = path.stem
    if entity_type != filename:
        raise SchemaContractError(
            f"{path}: entity_type {entity_type!r} must match the filename stem {filename!r}"
        )
    if not _TOKEN_SLUG.fullmatch(str(entity_type)):
        raise SchemaContractError(f"{path}: entity_type {entity_type!r} is not a lower-case slug")
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, str) or not _VERSION_SLUG.fullmatch(schema_version):
        raise SchemaContractError(f"{path}: schema_version {schema_version!r} is not a safe slug")
    entity_noun = raw.get("entity_noun")
    if not isinstance(entity_noun, str) or not entity_noun.strip():
        raise SchemaContractError(f"{path}: entity_noun must be a non-empty string")
    link_proof_type = raw.get("link_proof_type")
    if not isinstance(link_proof_type, str) or not _VERSION_SLUG.fullmatch(link_proof_type):
        raise SchemaContractError(f"{path}: link_proof_type {link_proof_type!r} is not a safe slug")

    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise SchemaContractError(f"{path}: fields must be a non-empty list")
    fields = tuple(_mapping_from(item, path) for item in raw_fields)

    names = [f.canonical_name for f in fields]
    if len(set(names)) != len(names):
        raise SchemaContractError(f"{path}: duplicate canonical_name in fields")
    labels = [f for f in fields if f.role == "primary_label"]
    if len(labels) != 1:
        raise SchemaContractError(
            f"{path}: exactly one primary_label is required, got {len(labels)}"
        )
    projections = {f.canonical_name for f in fields if f.is_projection}
    seen_aliases: dict[str, str] = {}
    for field in fields:
        if field.is_projection:
            continue  # a projection shares names with its source by design
        for alias in field.source_aliases:
            key = _key(alias)
            owner = seen_aliases.setdefault(key, field.canonical_name)
            if owner != field.canonical_name:
                raise SchemaContractError(
                    f"{path}: alias {alias!r} is claimed by both {owner!r} and {field.canonical_name!r}"
                )
    if projections and not any(f.role == "primary_label" for f in fields):
        raise SchemaContractError(f"{path}: a projection needs a primary_label to derive from")

    return EntitySchemaSpec(
        schema_version=schema_version,
        entity_type=entity_type,
        entity_noun=entity_noun.strip(),
        link_proof_type=link_proof_type,
        admission=_admission_from(raw.get("admission"), path),
        fields=fields,
    )


def load_specs(directory: str | Path | None = None) -> dict[str, EntitySchemaSpec]:
    """Load and validate every spec in a directory. Fail closed on any defect."""
    base = Path(directory) if directory is not None else specs_dir()
    paths = sorted(base.glob("*.json"))
    if not paths:
        raise SchemaContractError(f"no schema specs in {base}")
    specs: dict[str, EntitySchemaSpec] = {}
    versions: dict[str, Path] = {}
    for path in paths:
        spec = _parse_spec(path)
        owner = versions.get(spec.schema_version)
        if owner is not None and owner != path:
            raise SchemaContractError(
                f"{path}: schema_version {spec.schema_version!r} already used by {owner}"
            )
        versions[spec.schema_version] = path
        specs[spec.entity_type] = spec
    return specs


DEFAULT_SCHEMA_SPECS = load_specs()


def _required(name: str) -> EntitySchemaSpec:
    try:
        return DEFAULT_SCHEMA_SPECS[name]
    except KeyError:
        raise SchemaContractError(
            f"the {name!r} spec is missing from the specs directory; "
            f"registered: {', '.join(sorted(DEFAULT_SCHEMA_SPECS))}"
        ) from None


# The shipped specs, by entity (muse_candidates imports these names).
USER_SPEC = _required("user")
PRODUCT_SPEC = _required("product")
PUBLICATION_SPEC = _required("publication")


def get_schema_spec(entity_type: str | None) -> EntitySchemaSpec:
    """The spec for one entity type.

    ``None`` (a document or cluster that never declared an entity type, e.g. a
    legacy fixture) falls back to the person contract. Any other unregistered
    string RAISES: otherwise a misspelled or new type would be quietly judged
    as a person -- empty profile, 0 candidates, no error -- which is the exact
    silent-wrong-answer failure the trust engine is built to prevent.
    """
    if entity_type is None:
        return USER_SPEC
    spec = DEFAULT_SCHEMA_SPECS.get(entity_type)
    if spec is None:
        raise ValueError(
            f"no schema registered for entity type {entity_type!r}; "
            f"registered: {', '.join(sorted(DEFAULT_SCHEMA_SPECS))}"
        )
    return spec


# ---------------------------------------------------------------------------
# Prompt compiler
# ---------------------------------------------------------------------------

_ROLE_EXPLANATION: dict[RoleType, str] = {
    "primary_label": "core label",
    "anchor_group": "anchor",
    "hard_discriminator": "strict discriminator",
    "corroborating": "supporting evidence",
}


def _field_role_lines(spec: EntitySchemaSpec) -> list[str]:
    """One prompt line per judge-visible field (the shared contract section)."""
    lines: list[str] = []
    for mapping in spec.visible_to_judge():
        line = f"- {mapping.canonical_name} ({_ROLE_EXPLANATION[mapping.role]})"
        if mapping.judge_instruction:
            line += f": {mapping.judge_instruction}"
        lines.append(line)
    return lines


def _discriminator_paragraph(spec: EntitySchemaSpec) -> str | None:
    discriminator = spec.fields_by_role("hard_discriminator")
    if not discriminator:
        return None
    names = ", ".join(m.canonical_name for m in discriminator)
    return (
        f"A conflict on {names} means the answer is 'different': "
        f"similar {spec.entity_noun}s still differ when their "
        f"{discriminator[0].role.replace('_', ' ')} values disagree."
    )


def compile_judge_prompt(spec: EntitySchemaSpec) -> str:
    """The question for this entity, generated from its spec.

    Deterministic: the same spec always compiles to the same text, which is
    what makes hashing it into the verdict cache key meaningful. Only fields
    returned by ``spec.visible_to_judge()`` are named, so a private identifier
    can never appear in a prompt even by accident.
    """
    lines = [
        f"You decide whether two {spec.entity_type} records describe the SAME "
        f"{spec.entity_noun}, using only the fields given.",
        "",
        "Field roles in the input JSON:",
    ]
    lines += _field_role_lines(spec)
    discriminator = _discriminator_paragraph(spec)
    if discriminator:
        lines.append(discriminator)
    lines += [
        "",
        f"Say 'same' only when the records plausibly describe one {spec.entity_noun}. "
        "Say 'different' when the fields point to different ones. A shared "
        "label alone never makes two records the same.",
        "Use nothing that is not in the input. Give one short reason and name "
        "the field that decided it.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Grouping proposer (C6b): the bucket-level question
# ---------------------------------------------------------------------------

GROUPING_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["groups", "unresolved"],
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["members", "confidence", "evidence"],
                "properties": {
                    "members": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium"]},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["field", "note"],
                            "properties": {
                                "field": {"type": "string"},
                                "note": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
}


def compile_grouping_prompt(spec: EntitySchemaSpec) -> str:
    """The bucket-partitioning question for this entity (C6b).

    Same contract as :func:`compile_judge_prompt`: deterministic, compiled
    only from judge-visible fields, and hashed into cache keys by callers, so
    a contract change can never reuse a partition made under an old one. The
    model *proposes* a partition; a deterministic validator drops members
    that violate hard guards, and the trust engine still refuses any link
    it cannot verify. Model confidence routes the dispatch (skip the
    pairwise check at ``high``); it never grants publish rights.
    """
    lines = [
        f"You decide how a small bucket of {spec.entity_type} records partitions"
        f" into distinct real-world {spec.entity_noun}s, using only the fields given.",
        "",
        "Field roles in the input JSON:",
    ]
    lines += _field_role_lines(spec)
    discriminator = spec.fields_by_role("hard_discriminator")
    if discriminator:
        names = ", ".join(m.canonical_name for m in discriminator)
        lines.append(
            f"A conflict on {names} means the records are different "
            f"{spec.entity_noun}s: keep them in separate groups."
        )
    lines += [
        "",
        "Return JSON only, matching the required schema exactly.",
        f"- groups: arrays of record_id values of 2 or more records that plausibly "
        f"describe the same {spec.entity_noun}. Every record_id appears at most once "
        "across all groups.",
        "- evidence: one short note per group naming the decisive field.",
        "- confidence: 'high' only when the records are overwhelmingly the same "
        "entity; otherwise 'medium'.",
        "- unresolved: record_ids you cannot place with confidence.",
        "A shared label alone never makes two records the same. "
        "Use nothing that is not in the input.",
    ]
    return "\n".join(lines)

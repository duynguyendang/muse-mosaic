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

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

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
# The shipped specs
# ---------------------------------------------------------------------------

USER_SPEC = EntitySchemaSpec(
    schema_version="sch-user-v1",
    entity_type="user",
    entity_noun="person",
    # The engine already knows this proof: names alone never verify it, which
    # is exactly what we want for a merge a model proposed. New entity types
    # may declare their own once the trust-engine learns to check it.
    link_proof_type="fuzzy_name_address",
    admission=AdmissionPolicy(
        min_label_score=0.8,
        min_label_with_discriminator=0.6,
        min_discriminator_score=0.8,
        per_cluster_cap=3,
        split_bucket_at=8,
        require_house_number_guard=True,
    ),
    fields=(
        FieldMapping(
            canonical_name="display_name",
            source_aliases=("full_name", "customer_name", "user_name"),
            role="primary_label",
            normalizer="text_fold",
        ),
        FieldMapping(
            canonical_name="address",
            source_aliases=("address_line", "street_address", "shipping_address"),
            role="hard_discriminator",
            normalizer="text_fold",
            judge_instruction=(
                "Must plausibly match; different house numbers or streets mean " "different people."
            ),
        ),
        FieldMapping(
            canonical_name="phone",
            source_aliases=("mobile", "telephone", "cell"),
            role="private_identifier",
            normalizer="phone_e164",
        ),
        FieldMapping(
            canonical_name="email",
            source_aliases=("email_address", "e_mail"),
            role="private_identifier",
            normalizer="email_lower",
        ),
    ),
)

PRODUCT_SPEC = EntitySchemaSpec(
    schema_version="sch-product-v1",
    entity_type="product",
    entity_noun="product",
    link_proof_type="fuzzy_name_address",
    admission=AdmissionPolicy(
        min_label_score=0.4,
        min_label_with_discriminator=0.6,
        min_discriminator_score=0.8,
        per_cluster_cap=5,
        split_bucket_at=8,
    ),
    fields=(
        FieldMapping(
            canonical_name="name",
            source_aliases=("title", "product_name", "item_name", "prod_title"),
            role="primary_label",
            normalizer="text_fold",
        ),
        FieldMapping(
            canonical_name="manufacturer",
            source_aliases=("brand", "maker", "vendor"),
            role="anchor_group",
            normalizer="root_brand_token",
            judge_instruction=(
                "Maker/brand of the product. Missing on one side is acceptable, "
                "but two different makers mean different products."
            ),
        ),
        FieldMapping(
            canonical_name="edition_hints",
            source_aliases=("name", "title"),
            role="hard_discriminator",
            normalizer="edition_tokens",
            judge_instruction=(
                "Extracted edition, platform (Mac vs Win), version, or license "
                "tier (Upgrade vs Full, 1-User vs 5-User). If both sides carry "
                "conflicting edition/platform/tier tokens, they are DIFFERENT "
                "products."
            ),
        ),
        FieldMapping(
            canonical_name="price",
            source_aliases=("list_price", "msrp", "cost"),
            role="corroborating",
            normalizer="preserve_punct",
            judge_instruction=(
                "Price value. Large divergences (e.g. >3x difference) often "
                "indicate Upgrade vs Full version or single vs bundle."
            ),
        ),
        FieldMapping(
            canonical_name="year",
            source_aliases=("release_year",),
            role="corroborating",
            normalizer="preserve_punct",
        ),
        FieldMapping(
            canonical_name="category",
            source_aliases=("product_type", "department"),
            role="corroborating",
            normalizer="text_fold",
        ),
    ),
)

PUBLICATION_SPEC = EntitySchemaSpec(
    schema_version="sch-publication-v1",
    entity_type="publication",
    entity_noun="paper",
    link_proof_type="fuzzy_name_address",
    admission=AdmissionPolicy(
        min_label_score=0.4,
        min_label_with_discriminator=0.6,
        min_discriminator_score=0.8,
        per_cluster_cap=5,
        split_bucket_at=8,
    ),
    fields=(
        FieldMapping(
            canonical_name="title",
            source_aliases=("name", "paper_title"),
            role="primary_label",
            normalizer="text_fold",
        ),
        FieldMapping(
            canonical_name="venue",
            source_aliases=("journal", "conference", "booktitle"),
            role="anchor_group",
            normalizer="preserve_punct",
        ),
        FieldMapping(
            canonical_name="year",
            source_aliases=("pub_year",),
            role="hard_discriminator",
            normalizer="preserve_punct",
            judge_instruction=(
                "Publication year. A 1-year shift can occur between workshop and "
                "journal, but multi-year gaps indicate different papers."
            ),
        ),
        FieldMapping(
            canonical_name="authors",
            source_aliases=("author_list", "creator"),
            role="corroborating",
            normalizer="text_fold",
        ),
    ),
)

DEFAULT_SCHEMA_SPECS: dict[str, EntitySchemaSpec] = {
    USER_SPEC.entity_type: USER_SPEC,
    PRODUCT_SPEC.entity_type: PRODUCT_SPEC,
    PUBLICATION_SPEC.entity_type: PUBLICATION_SPEC,
}


def get_schema_spec(entity_type: str | None) -> EntitySchemaSpec:
    """The spec for one entity type. Unknown types fall back to the person spec.

    Falling back (rather than raising) keeps an unregistered type on the
    conservative, most-tested contract instead of crashing a build.
    """
    return DEFAULT_SCHEMA_SPECS.get(entity_type or "user", USER_SPEC)


# ---------------------------------------------------------------------------
# Prompt compiler
# ---------------------------------------------------------------------------

_ROLE_EXPLANATION: dict[RoleType, str] = {
    "primary_label": "core label",
    "anchor_group": "anchor",
    "hard_discriminator": "strict discriminator",
    "corroborating": "supporting evidence",
}


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
    for mapping in spec.visible_to_judge():
        line = f"- {mapping.canonical_name} ({_ROLE_EXPLANATION[mapping.role]})"
        if mapping.judge_instruction:
            line += f": {mapping.judge_instruction}"
        lines.append(line)
    discriminator = spec.fields_by_role("hard_discriminator")
    if discriminator:
        names = ", ".join(m.canonical_name for m in discriminator)
        lines.append(
            f"A conflict on {names} means the answer is 'different': "
            f"similar {spec.entity_noun}s still differ when their "
            f"{discriminator[0].role.replace('_', ' ')} values disagree."
        )
    lines += [
        "",
        f"Say 'same' only when the records plausibly describe one {spec.entity_noun}. "
        "Say 'different' when the fields point to different ones. A shared "
        "label alone never makes two records the same.",
        "Use nothing that is not in the input. Give one short reason and name "
        "the field that decided it.",
    ]
    return "\n".join(lines)

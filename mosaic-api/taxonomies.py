"""Versioned taxonomy store (CSD 3.5).

Taxonomy is a permanent, versioned input: canonical attribute aliases plus
canonical terms with exact-match synonyms. Baked in from data/mock/taxonomy;
steward Accept creates a new version file (see publisher). The trust-engine
loads the same files directly and enforces membership (T1); mosaic-api only
selects versions and applies exact alias/synonym mappings as Taxonomist
claims. AI never auto-creates a term.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from schemas import Taxonomy


class TaxonomyError(RuntimeError):
    """No usable taxonomy for the requested entity type or version."""


def taxonomies_dir(mock_data_dir: str | Path | None = None) -> Path:
    """Locate the taxonomy directory: TAXONOMY_DIR env, baked dir, or repo.

    TAXONOMY_DIR (env) wins so tests can isolate writes and Docker can point
    at a writable image layer. The trust-engine watches the same directory.
    """
    override = os.environ.get("TAXONOMY_DIR", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_dir():
            return candidate
        raise TaxonomyError(f"TAXONOMY_DIR {override!r} is not a directory")
    if mock_data_dir is not None:
        candidate = Path(mock_data_dir) / "taxonomy"
        if candidate.is_dir():
            return candidate
    here = Path(__file__).resolve().parent
    for candidate in (
        here / "taxonomy",
        here.parent / "data" / "mock" / "taxonomy",
    ):
        if candidate.is_dir():
            return candidate
    raise TaxonomyError("taxonomy directory not found")


def load_taxonomies(taxonomy_dir: str | Path | None = None) -> dict[str, Taxonomy]:
    """Load every taxonomy JSON, keyed by taxonomy_version."""
    base = Path(taxonomy_dir) if taxonomy_dir else taxonomies_dir()
    loaded: dict[str, Taxonomy] = {}
    for path in sorted(base.glob("*.json")):
        taxonomy = Taxonomy.model_validate_json(path.read_text(encoding="utf-8"))
        if taxonomy.taxonomy_version in loaded:
            raise TaxonomyError(
                f"duplicate taxonomy version {taxonomy.taxonomy_version}"
            )
        loaded[taxonomy.taxonomy_version] = taxonomy
    if not loaded:
        raise TaxonomyError(f"no taxonomy JSON under {base}")
    return loaded


def default_version(taxonomies: dict[str, Taxonomy], entity_type: str) -> str:
    """Latest taxonomy version for an entity type (demo default input)."""
    versions = sorted(
        version
        for version, taxonomy in taxonomies.items()
        if taxonomy.entity_type == entity_type
    )
    if not versions:
        raise TaxonomyError(f"no taxonomy for entity_type {entity_type!r}")
    return versions[-1]


def normalize_text(value: str) -> str:
    """Lowercase, strip accents and spaces for exact synonym matching."""
    folded = unicodedata.normalize("NFKD", value.casefold())
    stripped = "".join(
        character for character in folded if not unicodedata.combining(character)
    )
    # Đ/đ (U+0110/U+0111) have no NFKD decomposition; map them explicitly.
    stripped = stripped.replace("đ", "d")
    return "".join(stripped.split())


def canonical_attribute(
    taxonomy: Taxonomy, raw_attribute: str
) -> tuple[str, bool]:
    """Map a raw field name to its canonical attribute via aliases.

    Returns (canonical, mapped). Unmapped names return unchanged with False.
    """
    cleaned = raw_attribute.strip().lower().replace(" ", "_")
    for canonical, aliases in taxonomy.attribute_aliases.items():
        if cleaned == canonical or cleaned in [alias.lower() for alias in aliases]:
            return canonical, cleaned != canonical
    return raw_attribute, False


def canonical_term(
    taxonomy: Taxonomy, attribute: str, value: str
) -> tuple[str, str]:
    """Map a raw categorical value to its canonical term via exact synonyms.

    Returns (canonical_value, mapping_kind) where mapping_kind is one of
    "alias_exact" (attribute alias), "synonym_exact" (term synonym),
    "term_exact" (already canonical), or "unmapped".
    """
    for term in taxonomy.terms:
        if value == term.term:
            return value, "term_exact"
    wanted = normalize_text(value)
    for term in taxonomy.terms:
        for synonym in term.synonyms:
            if normalize_text(synonym) == wanted:
                return term.term, "synonym_exact"
    return value, "unmapped"

"""Shared route dependencies: app-state accessors and small helpers.

No routes live here — only the cross-cutting pieces every router needs:
entity validation, the sticky Splink-failure flag, engine health probes,
taxonomy file helpers, and the expected-structure loader.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx
from config import Settings
from fastapi import HTTPException, Request
from schemas import Taxonomy
from taxonomies import default_version, load_taxonomies, taxonomies_dir

logger = logging.getLogger("mosaic-api")

ENTITY_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Last observed matching-engine failure (S-05). /health reports splink
# unavailable while this is set — even if a probe succeeds — until a later
# /match succeeds and clears it. Process-local last-known state: a recorded
# match failure is never hidden by a passing probe.
_SPLINK_LAST_ERROR: str | None = None


def _note_splink_result(out: dict[str, Any]) -> None:
    """Record or clear the sticky matching-engine failure."""
    global _SPLINK_LAST_ERROR
    if out.get("status") == "ok":
        _SPLINK_LAST_ERROR = None
    else:
        _SPLINK_LAST_ERROR = str(out.get("detail", "unavailable"))


def _check_entity(entity_type: str) -> None:
    if not ENTITY_TYPE_RE.fullmatch(entity_type):
        raise HTTPException(status_code=422, detail="invalid entity_type")


async def _matching_engine_health(
    settings: Settings,
) -> tuple[str, dict[str, str | None]]:
    """Probe the standalone matching-engine: (status, versions).

    Never raises and never blocks the API: unreachable/slow/invalid means
    "unavailable" with no versions; a version mismatch against the pinned
    EXPECTED_* constants also fails closed (never silently use a mismatched
    engine). A recorded match failure (S-05) keeps reporting unavailable
    until a later /match succeeds, even if a probe succeeds in the meantime
    — the fallback is never hidden. The UI renders this as
    "Matching Engine: DOWN — fallback to Muse + fuzzy".
    """
    from splink_adapter import EXPECTED_DUCKDB_VERSION, EXPECTED_SPLINK_VERSION

    if not settings.splink_enabled:
        return "unavailable", {"splink_version": None, "duckdb_version": None}
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(settings.matching_health_url())
        if response.status_code != 200:
            return "unavailable", {"splink_version": None, "duckdb_version": None}
        payload = response.json()
    except Exception as exc:
        logger.debug("matching-engine health probe failed: %s", exc)
        return "unavailable", {"splink_version": None, "duckdb_version": None}
    if not isinstance(payload, dict) or payload.get("splink_service") != "ok":
        return "unavailable", {"splink_version": None, "duckdb_version": None}
    if payload.get("splink_enabled") is False:
        return "unavailable", {"splink_version": None, "duckdb_version": None}
    versions = {
        key: (payload.get(key) if isinstance(payload.get(key), str) else None)
        for key in ("splink_version", "duckdb_version")
    }
    if (versions["splink_version"], versions["duckdb_version"]) != (
        EXPECTED_SPLINK_VERSION,
        EXPECTED_DUCKDB_VERSION,
    ):
        return "unavailable", versions
    if _SPLINK_LAST_ERROR is not None:
        return "unavailable", versions
    return "ok", versions


def _load_expected_structures() -> dict[str, dict[str, Any]]:
    """Baked-in default expected structures per entity type (CSD 12.6)."""
    from config import API_DIR, REPO_ROOT

    structures: dict[str, dict[str, Any]] = {}
    candidates = [
        REPO_ROOT / "data" / "mock" / "templates",
        API_DIR / "data" / "mock" / "templates",
    ]
    base = next((path for path in candidates if path.is_dir()), None)
    if base is None:
        return structures
    for name in ("user", "product"):
        path = base / f"expected_{name}_golden.json"
        if path.is_file():
            structures[name] = json.loads(path.read_text(encoding="utf-8"))
    return structures


def _write_taxonomy_version(taxonomies: dict, base, suggested_action: str, new_version: str) -> str:
    """Create a new taxonomy version file from one accepted suggestion.

    Supported (case-insensitive verb, both quote styles kept verbatim):
    'Create as new term "X"' / 'create new term "X"' (adds the term, empty
    synonyms) and 'Map "X" to "Y"' / 'map "X" → "Y"' (adds X as a synonym
    of existing term Y). Anything else fails closed. The new version id
    must be fresh.
    """
    if not new_version or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", new_version):
        raise ValueError(f"invalid taxonomy version {new_version!r}")
    if new_version in taxonomies:
        raise ValueError(f"taxonomy version {new_version!r} already exists")
    create_match = re.fullmatch(r'[Cc]reate as new term "(.*)"', suggested_action) or re.fullmatch(
        r'create new term "(.*)"', suggested_action
    )
    map_match = re.fullmatch(r'[Mm]ap "(.*)" to "(.*)"', suggested_action) or re.fullmatch(
        r'map "(.*)" → "(.*)"', suggested_action
    )
    terms = [{"term": term.term, "synonyms": list(term.synonyms)} for term in base.terms]
    if create_match:
        raw = create_match.group(1).strip()
        if not raw:
            raise ValueError("empty term")
        if any(term["term"].lower() == raw.lower() for term in terms):
            raise ValueError(f"term {raw!r} already exists")
        terms.append({"term": raw, "synonyms": []})
    elif map_match:
        raw, target = map_match.group(1).strip(), map_match.group(2).strip()
        hit = next((term for term in terms if term["term"] == target), None)
        if hit is None:
            raise ValueError(f"target term {target!r} does not exist")
        if raw not in hit["synonyms"]:
            hit["synonyms"] = sorted(set(hit["synonyms"]) | {raw})
    else:
        raise ValueError(f"unsupported suggested action {suggested_action!r}")
    payload = {
        "synthetic": True,
        "taxonomy_version": new_version,
        "entity_type": base.entity_type,
        "notice": f"Extended by steward accept from {base.taxonomy_version}.",
        "attribute_aliases": base.attribute_aliases,
        "terms": terms,
    }
    directory = _taxonomy_write_dir()
    path = directory / f"{new_version}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    validated = Taxonomy.model_validate(payload)
    _ = validated
    return new_version


def _taxonomy_write_dir() -> Path:
    """Writable taxonomy directory the engine also watches (hot-reload).

    Uses taxonomies_dir() (TAXONOMY_DIR-aware). If it is read-only, fail
    closed — never write somewhere the engine cannot see.
    """
    directory = taxonomies_dir()
    probe = directory / ".write-test"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        raise OSError(
            f"taxonomy directory {directory} is not writable; " "cannot accept new terms here"
        ) from exc
    return directory


def _row_to_fact(row: dict[str, Any]) -> dict[str, Any]:
    """Ledger candidate_fact row -> Fragment-shaped dict for re-derivation."""
    return {
        "source": row["source"],
        "source_id": row["source_id"],
        "attribute": row["attribute"],
        "value": row["value"],
        "authority_rank": row["authority_rank"],
        "event_time": row["event_time"],
        "verified": bool(row["verified"]),
        "ttl_seconds": row["ttl_seconds"],
    }


async def _trust_engine_health(url: str) -> dict[str, Any] | None:
    """Return the trust-engine health payload, or None when unavailable."""
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return None
    return payload


def refresh_taxonomies(request: Request) -> None:
    """Hot-reload the in-memory taxonomy store after a steward Accept."""
    request.app.state.taxonomies = load_taxonomies()


def active_taxonomy(request: Request, entity_type: str) -> tuple[str, Any]:
    """Best taxonomy version for an entity type, defaulting when missing."""
    taxonomies = request.app.state.taxonomies
    try:
        version = default_version(taxonomies, entity_type)
    except Exception as exc:
        logger.warning("taxonomy lookup failed for %s, continuing without: %s", entity_type, exc)
        return "", None
    return version, taxonomies[version]

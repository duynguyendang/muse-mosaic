"""Ingest + clean layer (CSD build order step 4, v2.3: Documents + taxonomy).

Reads synthetic JSON fixtures, normalizes values deterministically (no model
calls, no golden decisions), maps raw field names through the taxonomy alias
table (Scout claim, logged), and converts fragments to candidate facts with
deterministic fact ids: ``{entity_id}-{source_id}``. Deterministic ids keep
retries idempotent and let the ledger join facts across decisions.

v2.3: input may also be Documents (hidden cluster, uploads). A Document is a
raw field bag with a content hash for dedup; invalid fragments are rejected
with a reason, never silently dropped. Categorical values map through exact
taxonomy synonyms (Taxonomist claim); fuzzy-only matches stay unmapped and
escalate at the engine (T1).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from schemas import CandidateFact, Document, DocumentField, Fragment
from taxonomies import canonical_attribute, canonical_term

_WS = re.compile(r"\s+")
_PHONE_STRIP = re.compile(r"[\s\-.()]+")
_INJECTION_MARKERS = (
    "ignore previous instructions",
    "system override",
    "override:",
    "delete the record",
    "delete the old",
)


def normalize_value(attribute: str, value: str) -> str:
    """Normalize one attribute value without making a golden decision."""
    cleaned = _WS.sub(" ", value).strip()
    if attribute == "email":
        return cleaned.lower()
    if attribute == "phone":
        return _PHONE_STRIP.sub("", cleaned)
    return cleaned


def is_injection_bearing(text: str) -> bool:
    """Detect crude instruction-override content in a synthetic fixture.

    This is a demo-scene tripwire, not a security control: anything flagged
    here is logged as an untrusted claim and forces the proposer to abstain.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _INJECTION_MARKERS)


def _fixture_files(mock_data_dir: Path) -> dict[str, Path]:
    return {
        "user": mock_data_dir / "users.json",
        "product": mock_data_dir / "products.json",
    }


def load_mock_fragments(mock_data_dir: Path, entity_type: str, entity_id: str) -> list[Fragment]:
    """Load synthetic fragments for one entity from data/mock/*.json."""
    path = _fixture_files(Path(mock_data_dir)).get(entity_type)
    if path is None or not path.exists():
        raise FileNotFoundError(f"no synthetic fixture for {entity_type}/{entity_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    for entity in data.get("entities", []):
        if entity.get("entity_id") == entity_id:
            return [Fragment.model_validate(item) for item in entity["fragments"]]
    raise FileNotFoundError(f"no synthetic fixture for {entity_type}/{entity_id}")


def load_mock_tickets(mock_data_dir: Path, entity_type: str, entity_id: str) -> list[dict]:
    """Load synthetic ticket fixtures scoped to one entity."""
    path = Path(mock_data_dir) / "tickets.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        ticket
        for ticket in data.get("tickets", [])
        if ticket.get("entity_type") == entity_type and ticket.get("entity_id") == entity_id
    ]


def list_mock_entities(mock_data_dir: Path) -> list[dict]:
    """List definable synthetic entities from data/mock fixtures.

    One entry per entity in users.json / products.json: the entity dropdown
    is populated from here, so defining a new entity is adding it to a
    fixture file — no UI or code change needed.
    """
    found: list[dict] = []
    for entity_type, filename in (("user", "users.json"), ("product", "products.json")):
        path = Path(mock_data_dir) / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for entity in data.get("entities", []):
            entity_id = entity.get("entity_id")
            if not entity_id:
                continue
            found.append(
                {
                    "entity_type": entity.get("entity_type", entity_type),
                    "entity_id": entity_id,
                    "fragments": len(entity.get("fragments", [])),
                }
            )
    return found


def document_hash(document: dict) -> str:
    """Content hash for upload dedup (CSD 3.2: Document, hash, dedup)."""
    canonical = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_hidden_documents(mock_data_dir: Path) -> list[Document]:
    """Load the synthetic hidden-cluster documents F1..F6 (Section 3.4)."""
    hidden = Path(mock_data_dir) / "hidden"
    documents = []
    for path in sorted(hidden.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        fields = {
            name: DocumentField.model_validate(item) for name, item in raw.get("fields", {}).items()
        }
        documents.append(
            Document(
                document_id=raw.get("document_id", path.stem),
                entity_type=raw.get("entity_type", "user"),
                source=raw.get("source", "unknown"),
                synthetic=bool(raw.get("synthetic", True)),
                trap=bool(raw.get("trap", False)),
                fields=fields,
                product_ref=(fields["product_ref"].value if "product_ref" in fields else None),
                content_hash=document_hash(raw),
            )
        )
    documents.sort(key=lambda doc: doc.document_id)
    return documents


def document_to_fragments(
    document: Document,
    entity_id: str,
    taxonomy=None,
    allowed_attributes: set[str] | None = None,
) -> tuple[list[Fragment], list[dict[str, str]]]:
    """Flatten one Document to Fragments via taxonomy alias mapping.

    Returns (fragments, mapping_claims). Fields whose names map to no known
    attribute stay graph-only evidence (e.g. product_ref) and are reported
    as claims, never silently dropped. Categorical values map through exact
    synonyms only; fuzzy-only matches stay raw and escalate at T1.
    """
    fragments: list[Fragment] = []
    claims: list[dict[str, str]] = []
    for raw_name, field in document.fields.items():
        attribute, aliased = raw_name, False
        if taxonomy is not None:
            attribute, aliased = canonical_attribute(taxonomy, raw_name)
        if aliased:
            claims.append(
                {
                    "kind": "alias_exact",
                    "raw": raw_name,
                    "canonical": attribute,
                }
            )
        if allowed_attributes is not None and attribute not in allowed_attributes:
            claims.append(
                {
                    "kind": "graph_only",
                    "raw": raw_name,
                    "reason": f"{attribute!r} is not a derive attribute; graph evidence only",
                }
            )
            continue
        value, mapping = field.value, "unmapped"
        if taxonomy is not None:
            value, mapping = canonical_term(taxonomy, attribute, field.value)
            if mapping != "unmapped":
                claims.append(
                    {
                        "kind": mapping,
                        "raw": field.value,
                        "canonical": value,
                    }
                )
        try:
            fragments.append(
                Fragment(
                    source=document.source,
                    source_id=f"{document.document_id}-{raw_name}",
                    attribute=attribute,
                    value=value,
                    authority_rank=field.authority_rank,
                    event_time=field.event_time,
                    verified=field.verified,
                    ttl_seconds=field.ttl_seconds,
                )
            )
        except ValueError as exc:
            claims.append(
                {
                    "kind": "rejected",
                    "raw": raw_name,
                    "reason": str(exc),
                }
            )
    return fragments, claims


def detect_entity_type(raw: dict) -> tuple[str, str]:
    """Propose an entity type from JSON keys (upload organic path).

    Returns (entity_type, confidence). v1 types: user_profile,
    purchased_product, ticket, product. Unknown shapes are flagged, never
    silently accepted. A caller may correct the type before Resolve.
    """
    keys = set()
    if isinstance(raw, dict):
        if isinstance(raw.get("fragments"), list):
            for item in raw["fragments"]:
                if isinstance(item, dict) and "attribute" in item:
                    keys.add(str(item["attribute"]))
        else:
            keys = {str(key).lower() for key in raw.keys()}
    user_keys = {
        "phone",
        "email",
        "address",
        "display_name",
        "customer_phone",
        "customer_email",
        "mobile",
        "dien_thoai",
        "sdt",
    }
    product_keys = {"category", "product_category", "cat"}
    order_keys = {"order_id", "order", "product_ref"}
    if keys & product_keys and not keys & user_keys:
        return "product", "high"
    if keys & user_keys:
        if "ticket" in keys or any("ticket" in key for key in keys):
            return "ticket", "medium"
        return "user_profile", "high" if len(keys & user_keys) >= 2 else "medium"
    if keys & order_keys:
        return "user_profile", "low"
    return "unknown", "low"


def _parse_csv_fragments(raw_text: str) -> tuple[list[dict], list[dict]]:
    """Parse a simple columnar CSV into fragment dicts (+rejections).

    Documented shape (header row required): ``attribute,value`` plus any of
    ``source,source_id,authority_rank,event_time,verified,ttl_seconds``.
    Missing optional columns fall back to explicit demo defaults
    (unverified, rank 3) — never to invented provenance. Rows missing
    ``attribute`` or ``value`` are rejected, never guessed.
    """
    import csv

    try:
        rows = list(csv.DictReader(raw_text.splitlines()))
    except (csv.Error, ValueError) as exc:
        raise ValueError(f"Upload is not valid CSV: {exc}") from exc
    if not rows:
        raise ValueError(
            "File must be CSV with a header row: attribute,value plus optional "
            "source,source_id,authority_rank,event_time,verified,ttl_seconds."
        )
    header = {(key or "").strip().lower() for key in (rows[0].keys() or [])}
    if "attribute" not in header or "value" not in header:
        raise ValueError(
            "File must be CSV with a header row: attribute,value plus optional "
            "source,source_id,authority_rank,event_time,verified,ttl_seconds."
        )
    now_iso = datetime.now(UTC).isoformat()
    fragments: list[dict] = []
    rejected: list[dict] = []

    def cell(row: dict, key: str, default: str = "") -> str:
        for variant in (key, key.title(), key.upper()):
            if row.get(variant) not in (None, ""):
                return str(row.get(variant))
        return default

    for index, row in enumerate(rows):
        attribute = cell(row, "attribute").strip()
        value = cell(row, "value").strip()
        if not attribute or not value:
            rejected.append({"index": index, "reason": "missing attribute or value"})
            continue
        try:
            authority_rank = int(cell(row, "authority_rank", "3") or 3)
        except (TypeError, ValueError):
            rejected.append({"index": index, "reason": "authority_rank must be an integer"})
            continue
        try:
            ttl_seconds = int(cell(row, "ttl_seconds", "3600") or 3600)
        except (TypeError, ValueError):
            rejected.append({"index": index, "reason": "ttl_seconds must be an integer"})
            continue
        verified_raw = cell(row, "verified").strip().lower()
        verified = verified_raw in ("true", "1", "yes", "y")
        item = {
            "source": cell(row, "source") or "uploaded-csv",
            "source_id": cell(row, "source_id") or f"csv-{index}",
            "attribute": attribute,
            "value": value,
            "authority_rank": authority_rank,
            "event_time": cell(row, "event_time") or now_iso,
            "verified": verified,
            "ttl_seconds": ttl_seconds,
        }
        try:
            fragments.append(Fragment.model_validate(item).model_dump(mode="json"))
        except ValueError as exc:
            rejected.append(
                {
                    "index": index,
                    "reason": str(exc),
                    "item": {k: str(v)[:120] for k, v in item.items()},
                }
            )
    return fragments, rejected


def parse_upload(filename: str, raw_text: str) -> tuple[list[dict], list[dict]]:
    """Parse an uploaded JSON or CSV file into fragment dicts (+rejections).

    Accepted shapes: one fragment object, an array of fragments, an
    object with a ``fragments`` array — or a columnar CSV with an
    ``attribute,value`` header row. Anything else is rejected with an
    exact message, never silently mapped (CSD 12.4).
    """
    name = filename.lower()
    if name.endswith(".csv"):
        return _parse_csv_fragments(raw_text)
    if not (name.endswith(".json") or name.endswith(".txt")):
        raise ValueError(f"Unsupported upload type {filename!r}: JSON or CSV only in v1.")
    try:
        data = json.loads(raw_text)
    except ValueError as exc:
        raise ValueError(f"Upload {filename!r} is not valid JSON: {exc}") from exc
    if isinstance(data, dict) and isinstance(data.get("fragments"), list):
        items = data["fragments"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = [data]
    else:
        raise ValueError(
            "File must be JSON: one fragment, an array of fragments, "
            "or an object with a fragments array."
        )
    fragments: list[dict] = []
    rejected: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            rejected.append({"index": index, "reason": "not an object", "item": str(item)[:200]})
            continue
        try:
            fragments.append(Fragment.model_validate(item).model_dump(mode="json"))
        except ValueError as exc:
            rejected.append(
                {
                    "index": index,
                    "reason": str(exc),
                    "item": {k: str(v)[:120] for k, v in item.items()},
                }
            )
    return fragments, rejected


def to_candidate_facts(
    entity_type: str,
    entity_id: str,
    fragments: list[Fragment],
    *,
    work_id: str,
) -> list[CandidateFact]:
    """Normalize fragments and assign deterministic fact ids.

    work_id is accepted for forward compatibility with multi-work tracing and
    does not influence fact ids: ids stay stable across retries so the ledger
    and the idempotency gate can join on them.
    """
    _ = work_id
    facts: list[CandidateFact] = []
    for fragment in fragments:
        facts.append(
            CandidateFact(
                fact_id=f"{entity_id}-{fragment.source_id}",
                entity_type=entity_type,
                entity_id=entity_id,
                attribute=fragment.attribute,
                value=normalize_value(fragment.attribute, fragment.value),
                source=fragment.source,
                source_id=fragment.source_id,
                authority_rank=fragment.authority_rank,
                event_time=fragment.event_time,
                verified=fragment.verified,
                ttl_seconds=fragment.ttl_seconds,
            )
        )
    facts.sort(key=lambda fact: fact.fact_id)
    return facts

"""Evidence-verified taxonomy extension.

Problem: a categorical value the taxonomy does not contain (``clo``, ``3l3ctronics``,
``Foldable Phone``) makes the engine hold that attribute (rule T1). Someone has
to extend the taxonomy. Letting a model's confidence score decide would let the
model create the vocabulary the engine then certifies, so here a model may only
PROPOSE, and the proposal is accepted only for the part the DATA can verify:

* ``ground``   every synonym the proposal cites is a value that really occurs
* ``attest``   the canonical term occurs verbatim in the data (or already exists)
               with enough records behind it, so the name is not invented
* ``relate``   each synonym is related to the term by a deterministic rule:
               same word after case/punctuation/leet folding, a unique prefix,
               or one typo (edit distance 1) on a long enough word
* ``unique``   no synonym is claimed by two different terms
* ``support``  the accepted group covers at least ``min_support`` records

A synonym that passes none of these is NOT accepted, however plausible. Its
records stay held (T1), which is the safe outcome. Semantic equivalences with no
lexical link (``điện thoại gập`` = ``foldable phone``) therefore cannot be
verified here; a model can suggest them, but they stay suggestions.

Accepted terms are written as a NEW taxonomy version (never an edit), with an
evidence log beside it.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import Settings
from schemas import Taxonomy
from taxonomies import canonical_term, write_text_atomic

LEET = str.maketrans(
    {"0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
MIN_PREFIX = 3  # shortest prefix that may stand for a term
MIN_TYPO_LEN = 5  # shortest word on which one typo is accepted


# --------------------------------------------------------------------------
# Folding and the relation rules (pure, deterministic)
# --------------------------------------------------------------------------


def skeleton(value: str) -> str:
    """Case, accent, punctuation and leet-speak folded away; real digits kept.

    A word is leet-folded only when that turns it entirely into letters
    ("hom3" -> "home", "3l3ctronics" -> "electronics"). A word that keeps a
    digit after folding is a model number or size, not leet ("b12", "13"), so
    its digits stay: "iPhone 13" and "iPhone 14" must not look like one word.
    """
    folded = unicodedata.normalize("NFKD", value.casefold().replace("đ", "d"))
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    words = []
    for word in re.split(r"[\s_\-/,.;:]+", folded):
        if re.search(r"[a-z]", word) and re.search(r"\d|@|\$", word):
            leeted = word.translate(LEET)
            if not re.search(r"\d", leeted):
                word = leeted
        words.append(re.sub(r"[^a-z0-9]", "", word))
    return "".join(words)


def _digits(skel: str) -> str:
    return "".join(c for c in skel if c.isdigit())


def edit_distance_le1(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long_[i]:
        i += 1
    return short[i:] == long_[i + 1 :]


def relation(synonym: str, term: str, all_terms: list[str]) -> str | None:
    """Name of the rule that relates ``synonym`` to ``term``, else None.

    ``all_terms`` are every term in play, so a prefix or typo that could belong
    to more than one term is refused instead of guessed.
    """
    s, t = skeleton(synonym), skeleton(term)
    if not s or not t:
        return None
    if s == t:
        return "same_word"
    others = [skeleton(x) for x in all_terms if skeleton(x) != t]
    if len(s) >= MIN_PREFIX and t.startswith(s) and not any(o.startswith(s) for o in others):
        return "unique_prefix"
    if (
        len(s) >= MIN_TYPO_LEN
        and len(t) >= MIN_TYPO_LEN
        and _digits(s) == _digits(t)  # a changed digit is another model, not a typo
        and edit_distance_le1(s, t)
        and not any(edit_distance_le1(s, o) and _digits(s) == _digits(o) for o in others)
    ):
        return "one_typo"
    return None


# --------------------------------------------------------------------------
# Observing the gap
# --------------------------------------------------------------------------


def observe(
    documents: list[dict[str, Any]], taxonomy: Taxonomy, entity_type: str, attribute: str
) -> dict[str, dict[str, Any]]:
    """Raw values of ``attribute`` that the taxonomy does not map: count + sources.

    ``count`` is the number of distinct entities (``product_ref``, else the
    document id) carrying the value, so copies of one product do not manufacture
    the support a new term needs.
    """
    seen: dict[str, dict[str, Any]] = {}
    counted: set[tuple[str, str]] = set()
    for document in documents:
        if document.get("entity_type") != entity_type:
            continue
        raw = document.get("fields", {}).get(attribute)
        value = raw.get("value") if isinstance(raw, dict) else raw
        if not value:
            continue
        value = str(value).strip()
        if canonical_term(taxonomy, attribute, value)[1] != "unmapped":
            continue
        entry = seen.setdefault(value, {"count": 0, "sources": set()})
        entry["sources"].add(document.get("source", ""))
        key = (
            value,
            str(document.get("product_ref") or document.get("document_id") or id(document)),
        )
        if key not in counted:
            counted.add(key)
            entry["count"] += 1
    return seen


# --------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------


@dataclass
class Proposal:
    term: str
    synonyms: list[str]
    reason: str = ""
    proposer: str = ""


def rule_proposer(
    observed: dict[str, dict[str, Any]], existing: list[str], min_support: int
) -> list[Proposal]:
    """Deterministic baseline: group values by the same relation rules.

    A value that relates to an EXISTING term becomes a synonym proposal for it.
    Otherwise a "head" is a value with enough records that is not itself a
    variant of a more frequent value, and the remaining values attach to the
    head they relate to. No model: this is what plain string rules can do.
    """
    by_count = sorted(observed, key=lambda v: (-observed[v]["count"], v))
    to_existing: dict[str, list[str]] = {}
    pool: list[str] = []
    for value in by_count:
        for term in existing:
            if relation(value, term, existing):
                to_existing.setdefault(term, []).append(value)
                break
        else:
            pool.append(value)
    heads: list[str] = []
    for value in pool:
        if observed[value]["count"] < min_support:
            continue
        if any(relation(value, h, existing + heads) for h in heads):
            continue  # a variant (case, prefix, typo) of a more frequent head
        heads.append(value)
    groups: dict[str, list[str]] = {h: [] for h in heads}
    terms = existing + heads
    for value in pool:
        if value in groups:
            continue
        for head in heads:
            if relation(value, head, terms):
                groups[head].append(value)
                break
    proposals = [
        Proposal(t, sorted(v), "relates to an existing term", "rules")
        for t, v in to_existing.items()
    ]
    proposals += [Proposal(h, sorted(set(v)), "rule-grouped", "rules") for h, v in groups.items()]
    return proposals


MUSE_SYSTEM = (
    "You normalise a product category vocabulary. You get raw category values "
    "(with how many records use each) that a taxonomy does not know. Group the "
    "values that mean the same category and name the canonical term for each "
    "group. RULES: the canonical term must be copied exactly from one of the "
    "raw values when one is a clean name for it; never invent a term the data "
    "does not contain; every synonym must be one of the raw values, verbatim; "
    "use an existing term when a value belongs to one; omit values you cannot "
    "place instead of guessing."
)
MUSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "term": {"type": "string"},
                    "synonyms": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["term", "synonyms", "reason"],
            },
        }
    },
    "required": ["proposals"],
}


async def muse_proposer(
    settings: Settings, observed: dict[str, dict[str, Any]], existing: list[str]
) -> list[Proposal]:
    from meta_client import chat_json

    payload = {
        "existing_terms": existing,
        "raw_values": [{"value": v, "records": observed[v]["count"]} for v in sorted(observed)],
    }
    parsed, _ = await chat_json(
        settings,
        model=settings.muse_model_id,
        system=MUSE_SYSTEM,
        user=json.dumps(payload, ensure_ascii=False, indent=1),
        schema_name="mosaic_taxonomy_proposals",
        schema=MUSE_SCHEMA,
        reasoning_effort="low",
        prompt_cache_key="mosaic-taxonomy-proposer-v1",
    )
    out = []
    for item in parsed.get("proposals", []):
        out.append(
            Proposal(
                str(item["term"]).strip(),
                [str(s) for s in item["synonyms"]],
                str(item["reason"]),
                "muse",
            )
        )
    return out


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


@dataclass
class Verdict:
    term: str
    proposer: str
    term_ok: bool = False
    term_reason: str = ""
    accepted: list[tuple[str, str]] = field(default_factory=list)  # (synonym, rule)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (synonym, why)
    support: int = 0


def verify(
    proposals: list[Proposal],
    observed: dict[str, dict[str, Any]],
    existing: list[str],
    min_support: int = 3,
) -> list[Verdict]:
    """Accept only what the data supports. Pure function of its arguments."""
    terms_in_play = list(dict.fromkeys(existing + [p.term for p in proposals if p.term]))
    claims: dict[str, set[str]] = defaultdict(set)  # synonym -> terms claiming it
    for p in proposals:
        for s in p.synonyms:
            claims[s].add(p.term)

    verdicts: list[Verdict] = []
    for p in proposals:
        v = Verdict(p.term, p.proposer)
        is_existing = p.term in existing
        # a "new" term that is only a variant (prefix, typo, case) of a term that
        # already exists, or of a better-supported one, is not a new category
        rivals = [o for o in terms_in_play if o != p.term]
        variant_of = next(
            (
                o
                for o in rivals
                if relation(p.term, o, rivals)
                and (
                    o in existing
                    or observed.get(o, {}).get("count", 0)
                    > observed.get(p.term, {}).get("count", 0)
                )
            ),
            None,
        )
        # attest: the name is an existing term, or occurs verbatim with support
        if is_existing:
            v.term_ok = True
        elif variant_of is not None:
            v.term_reason = f"term is a variant of {variant_of!r}, not a new category"
        elif p.term in observed and observed[p.term]["count"] >= min_support:
            v.term_ok = True
        elif p.term in observed:
            v.term_reason = (
                f"term has only {observed[p.term]['count']} record(s), need {min_support}"
            )
        else:
            v.term_reason = "term does not occur in the data (invented name)"
        for s in dict.fromkeys(p.synonyms):
            if s == p.term:
                continue
            if s not in observed:
                v.rejected.append((s, "not a value in the data"))  # ground
            elif len(claims[s]) > 1:
                v.rejected.append((s, f"claimed by several terms: {sorted(claims[s])}"))  # unique
            elif not v.term_ok:
                v.rejected.append((s, "term not verified"))
            elif (rule := relation(s, p.term, terms_in_play)) is None:
                v.rejected.append((s, "no verifiable relation to the term"))  # relate
            else:
                v.accepted.append((s, rule))
        own = observed.get(p.term, {}).get("count", 0) if not is_existing else 0
        v.support = own + sum(observed[s]["count"] for s, _ in v.accepted)
        if v.term_ok and not is_existing and v.support < min_support:  # support
            v.rejected += [(s, "group support below minimum") for s, _ in v.accepted]
            v.accepted, v.term_ok, v.term_reason = [], False, "group support below minimum"
        verdicts.append(v)
    return verdicts


# --------------------------------------------------------------------------
# Writing a new version
# --------------------------------------------------------------------------


def write_version(base: Taxonomy, verdicts: list[Verdict], directory: Path) -> str | None:
    """Write base + accepted terms as a NEW version. Returns its id, or None if nothing to add.

    The id is a hash of the content, so the same acceptance yields the same version
    (idempotent). Which version is the default is decided by ``created_at`` (see
    ``taxonomies.default_version``), not by the name. The evidence log is written
    first and the taxonomy file last, atomically: the engine loads every ``*.json``,
    so the file appearing means the log already exists and is complete.
    """
    terms = {t.term: set(t.synonyms) for t in base.terms}
    log: list[dict[str, Any]] = []
    for v in verdicts:
        if not v.term_ok or (not v.accepted and v.term in terms):
            continue
        terms.setdefault(v.term, set())
        if v.term not in {t.term for t in base.terms}:
            log.append(
                {"event": "new_term", "term": v.term, "proposer": v.proposer, "support": v.support}
            )
        for synonym, rule in v.accepted:
            if synonym not in terms[v.term]:
                terms[v.term].add(synonym)
                log.append(
                    {
                        "event": "synonym",
                        "term": v.term,
                        "synonym": synonym,
                        "rule": rule,
                        "proposer": v.proposer,
                    }
                )
    if not log:
        return None
    payload_terms = [{"term": t, "synonyms": sorted(s)} for t, s in terms.items()]
    digest = hashlib.sha256(
        json.dumps(payload_terms, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:8]
    version = f"tax-{base.entity_type}-evidence-{digest}"
    path = directory / f"{version}.json"
    if not path.exists():
        payload = {
            "synthetic": True,
            "taxonomy_version": version,
            "parent_version": base.taxonomy_version,
            "created_at": datetime.now(UTC).isoformat(),
            "entity_type": base.entity_type,
            "notice": f"Extended from {base.taxonomy_version} by evidence-verified proposals; see {version}.evidence.jsonl.",
            "attribute_aliases": base.attribute_aliases,
            "terms": payload_terms,
        }
        write_text_atomic(
            directory / f"{version}.evidence.jsonl",
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in log),
        )
        write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return version


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


async def extend_taxonomy(
    settings: Settings,
    base: Taxonomy,
    documents: list[dict[str, Any]],
    directory: Path,
    *,
    attribute: str = "category",
    mode: str = "rules",
    min_support: int = 3,
    check: bool = True,
) -> dict[str, Any]:
    """Observe, propose, verify, write. Never raises for model trouble.

    ``mode`` is ``rules`` (no model) or ``muse``. ``check=False`` skips
    verification: it exists ONLY so a benchmark can show what unverified
    acceptance would do; the product path always verifies.
    """
    observed = observe(documents, base, base.entity_type, attribute)
    existing = [t.term for t in base.terms]
    report: dict[str, Any] = {
        "mode": mode,
        "unknown_values": len(observed),
        "unknown_records": sum(v["count"] for v in observed.values()),
        "version": None,
        "accepted": 0,
        "rejected": 0,
        "skipped_reason": None,
        "verdicts": [],
    }
    if not observed:
        return report
    try:
        if mode == "muse":
            proposals = await muse_proposer(settings, observed, existing)
        else:
            proposals = rule_proposer(observed, existing, min_support)
    except Exception as exc:  # noqa: BLE001 - a model outage must never block or loosen a build
        report["skipped_reason"] = f"proposer unavailable: {type(exc).__name__}"
        return report
    if check:
        verdicts = verify(proposals, observed, existing, min_support)
    else:  # benchmark only: trust the proposer
        verdicts = [
            Verdict(
                p.term,
                p.proposer,
                True,
                "",
                [(s, "unchecked") for s in p.synonyms if s in observed],
                [],
                0,
            )
            for p in proposals
        ]
    report["verdicts"] = verdicts
    report["accepted"] = sum(len(v.accepted) for v in verdicts)
    report["rejected"] = sum(len(v.rejected) for v in verdicts)
    report["version"] = write_version(base, verdicts, directory)
    return report

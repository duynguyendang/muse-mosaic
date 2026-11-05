"""Muse candidate matching: find clusters the exact identifiers missed.

Splink groups records that share an exact identifier (email, phone, order id).
A customer whose phone AND email changed shares none, so exact blocking leaves
them as two clusters, and the system would certify two goldens for one person.
This step looks for exactly those near-matches.

Scale is the point. Judging every pair of clusters is O(n^2), so a machine
narrows first and the model only judges what is left:

1. ``shortlist``: token blocking + cheap string scores pick the candidate
   cluster pairs, capped per cluster (not globally by default). No model runs.
2. ``judge``: Muse (or a deterministic mock) rules on each shortlisted pair
   from display name and address ONLY (no email/phone are sent) as
   same / different / unsure with a confidence and a reason.
3. ``select_merges``: only ``same`` with ``high`` confidence merges clusters.

A merge is a PROPOSAL to cluster, never a verified identity. The link carries
proof ``fuzzy_name_address``, which the trust-engine does not accept as a hop,
so a merged cluster whose values disagree is held by the engine (C1/C2), not
certified. A wrong merge can therefore make a result stricter, never looser.
Any model failure skips the step: no merges, labelled in the build summary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from time import perf_counter
from typing import Any, Literal

from config import Settings
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from schema_contract import (
    DEFAULT_SCHEMA_SPECS,
    PRODUCT_SPEC,
    PUBLICATION_SPEC,
    USER_SPEC,
    EntitySchemaSpec,
    apply_normalizer,
    compile_judge_prompt,
    get_schema_spec,
)

# The link proof each shipped spec declares lives in schema_contract
# (``link_proof_type``); there is no second copy of that string here.
JUDGE_BATCH = 20
MAX_MERGED_DOCS = 20  # never let chained merges build a giant cluster
COMMON_TOKEN_CLUSTERS = 50  # tokens in more clusters than this block nothing
BLOCKING_MODES = ("token", "rarest_pair")
ZERO_CANDIDATE_WARN_CLUSTERS = 200  # fewer clusters than this: zero candidates is unremarkable

# The shipped default bucket threshold, kept for callers that only need the
# number. Which threshold applies to a bucket -- and which field splits it,
# and how far the admission gate opens -- is read from the entity's
# EntitySchemaSpec in schema_contract, never from a table here.
SPLIT_BUCKET_AT = USER_SPEC.admission.split_bucket_at


def _has_cjk(text: str) -> bool:
    return any("぀" <= ch <= "鿿" or "가" <= ch <= "힯" for ch in text)


def _alias_key(value: str) -> str:
    """Alias comparison key: case- and separator-insensitive."""
    return value.strip().lower().replace(" ", "_")


def canonicalize_documents(
    documents: list[dict[str, Any]],
    taxonomy: Any | None = None,
    specs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rename raw field names to each entity type's canonical names.

    Two alias sources, the dynamic one applied last so it wins: the entity's
    ``EntitySchemaSpec`` (``title`` -> ``name`` for a product, ``full_name`` ->
    ``display_name`` for a person) and ``taxonomy.attribute_aliases``, which a
    steward can change at runtime. Every entity type declared in ``specs`` is
    mapped -- not just the four identity roles -- and each document is mapped
    with the spec for ITS entity type, so a product's ``title`` and a paper's
    ``name`` never collide.

    Only alias-shaped keys are renamed: the first non-empty value wins, a
    canonical field is never overwritten, and a field that loses the race is
    kept rather than dropped. Documents with nothing to rename come back as
    the same object (no copy). Never raises.
    """
    specs = specs or DEFAULT_SCHEMA_SPECS
    tax_lookup: dict[str, str] = {}
    if taxonomy is not None:
        for canonical, aliases in (taxonomy.attribute_aliases or {}).items():
            for alias in aliases or []:
                tax_lookup[_alias_key(alias)] = canonical
    fallback = specs.get("user") or USER_SPEC
    out: list[dict[str, Any]] = []
    for document in documents:
        spec = specs.get(document.get("entity_type") or "user") or fallback
        lookup = {**spec.rename_map(), **tax_lookup}  # taxonomy overrides spec
        canonical_names = {mapping.canonical_name for mapping in spec.fields}
        fields = document.get("fields") or {}
        renames = {
            raw: lookup[_alias_key(raw)]
            for raw in fields
            if raw not in canonical_names and _alias_key(raw) in lookup
        }
        if not renames:
            out.append(document)
            continue
        new_fields = dict(fields)
        for raw, role in renames.items():
            value = fields[raw]
            present = new_fields.get(role)
            empty = present is None or not (
                present.get("value") if isinstance(present, dict) else present
            )
            if empty and (value.get("value") if isinstance(value, dict) else value):
                new_fields[role] = value
                del new_fields[raw]  # moved; a field that lost the race stays as it was
        out.append({**document, "fields": new_fields})
    return out


SYSTEM_PROMPT = (
    "You decide whether two customer records describe the SAME real person, "
    "using only the display names and addresses given. Different people can "
    "share a name, so a matching name with a different or missing address is "
    "'unsure', never 'same'. Say 'same' only when both the names and the "
    "addresses plausibly match after typos, casing, abbreviation or word "
    "order. Say 'different' when the evidence points to different people. "
    "Each pair may carry 'identifier_hints' computed by code (a phone or email "
    "that is nearly, but not exactly, the same). A hint is supporting evidence, "
    "never enough on its own: the names must still be compatible. "
    "Use nothing that is not in the input. Give one short reason."
)

# The person question is FROZEN, verbatim: it is hashed into every verdict
# cache key that exists today, and its wording is itself the privacy contract
# (a phone or email is only ever a HINT, never proof). Every other entity's
# question is compiled from its EntitySchemaSpec by schema_contract, so a new
# entity type is a data drop rather than a new prompt string in this file.
PRODUCT_PROMPT = compile_judge_prompt(PRODUCT_SPEC)
PUBLICATION_PROMPT = compile_judge_prompt(PUBLICATION_SPEC)

# schema_version -> prompt text shipped for that version. A spec version not
# listed here gets its question compiled on demand from the spec.
FROZEN_PROMPTS: dict[str, str] = {USER_SPEC.schema_version: SYSTEM_PROMPT}


def system_prompt_for(entity_type: str | None) -> str:
    """The question to ask about one pair, taken from that entity's spec.

    Unknown entity types fall back to the person contract: the conservative,
    most-tested one, never a prompt we never validated.
    """
    spec = get_schema_spec(entity_type)
    frozen = FROZEN_PROMPTS.get(spec.schema_version)
    return frozen if frozen is not None else compile_judge_prompt(spec)


JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "pair_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["same", "different", "unsure"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "reason": {"type": "string"},
                },
                "required": ["pair_id", "verdict", "confidence", "reason"],
            },
        }
    },
    "required": ["verdicts"],
}


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair_id: str
    verdict: Literal["same", "different", "unsure"]
    confidence: Literal["high", "medium", "low"]
    reason: str = Field(min_length=1)


@dataclass
class CandidatePair:
    pair_id: str
    left: int  # index into the cluster list
    right: int
    name_score: float
    address_score: float
    left_profile: dict[str, list[str]] = field(default_factory=dict)
    right_profile: dict[str, list[str]] = field(default_factory=dict)
    hints: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    entity_type: str = "user"  # which prompt and admission gate this pair is judged under


# --------------------------------------------------------------------------
# Profiles and string scores
# --------------------------------------------------------------------------


def _norm(text: str) -> str:
    """Matching normalisation: accents stripped, punctuation to space.

    Delegated to the contract's ``text_fold`` so the matcher and the judge can
    never drift on what "the same text" means. Anything a human reads as a
    value (a price, a venue) is normalised with ``preserve_punct`` instead --
    see schema_contract.apply_normalizer.
    """
    return apply_normalizer(text, "text_fold")


def _field_value(document: dict[str, Any], name: str) -> str | None:
    raw = document.get("fields", {}).get(name)
    value = raw.get("value") if isinstance(raw, dict) else raw
    return str(value) if value else None


def cluster_profile(
    cluster: dict[str, Any], spec: EntitySchemaSpec | None = None
) -> dict[str, list[str]]:
    """Exactly what this entity's schema says may leave the process.

    The primary label lands under ``"names"``, the address under
    ``"addresses"`` (only a person spec declares an address, so every other
    type keeps an empty list and the payload shape stays uniform), and every
    other visible field under its own canonical name, normalised by the
    normalizer that field declares -- ``preserve_punct`` keeps a price of
    ``$19.99`` readable, ``edition_tokens`` derives edition/platform/tier
    hints from the title.

    The privacy boundary is ``spec.visible_to_judge()``: a ``private_
    identifier`` is never read here, for any entity type, so phone and email
    are structurally unable to reach the model rather than merely omitted.
    """
    spec = spec or get_schema_spec(cluster.get("entity_type"))
    label_field = spec.primary_label.canonical_name
    collected: dict[str, set[str]] = {}
    for document in cluster["member_docs"]:
        for mapping in spec.visible_to_judge():
            # A projection (edition_hints) derives from the field its aliases
            # name; every other field is read by canonical name, because
            # alias resolution is canonicalize_documents' job.
            sources = mapping.source_aliases if mapping.is_projection else (mapping.canonical_name,)
            bucket = collected.setdefault(mapping.canonical_name, set())
            for source in sources:
                value = _field_value(document, source)
                if not value:
                    continue
                normalised = apply_normalizer(value, mapping.normalizer)
                if not normalised:
                    continue
                if mapping.normalizer == "edition_tokens":
                    bucket.update(normalised.split())
                else:
                    bucket.add(normalised)
    profile: dict[str, list[str]] = {
        "names": sorted(collected.get(label_field, set())),
        "addresses": sorted(collected.get("address", set())),
    }
    for canonical in sorted(collected):
        if canonical in (label_field, "address") or not collected[canonical]:
            continue
        profile[canonical] = sorted(collected[canonical])
    return profile


def phone_key(value: str) -> str:
    """Last 9 digits, extension dropped. Same definition as trust-engine semantic.go."""
    digits = re.sub(r"\D", "", re.split(r"[xX]", value)[0])
    return digits[-9:] if len(digits) >= 9 else ""


def email_deletion_keys(value: str) -> set[str]:
    """Deletion neighbourhood (edit distance <= 1). Same as trust-engine semantic.go."""
    value = value.strip().lower()
    local, at, domain = value.rpartition("@")
    if not at or not local or not domain or not 8 <= len(local) <= 64:
        return set()
    suffix = "@" + domain
    return {value} | {local[:i] + local[i + 1 :] + suffix for i in range(len(local))}


def cluster_identifiers(cluster: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Per-document phone keys and emails. Used locally; never sent to a model."""
    phones: dict[str, str] = {}  # document id -> phone digits (extension dropped)
    emails: dict[str, str] = {}  # document id -> email
    for document in cluster["member_docs"]:
        doc_id = document["document_id"]
        if (phone := _field_value(document, "phone")) and phone_key(phone):
            phones[doc_id] = re.sub(r"\D", "", re.split(r"[xX]", phone)[0])
        if (email := _field_value(document, "email")) and email_deletion_keys(email):
            emails[doc_id] = email.strip().lower()
    return {"phones": phones, "emails": emails}


def identifier_evidence(
    a: dict[str, dict[str, str]], b: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    """Near-identical (not exactly equal) phone/email pairs, computed by code.

    These mirror the trust-engine's S1/S2 features exactly, so every item here
    is something the engine can verify. Exact matches are excluded: those
    clusters would already be one cluster.
    """
    found: list[dict[str, str]] = []
    for da, pa in sorted(a["phones"].items()):
        for db, pb in sorted(b["phones"].items()):
            if pa != pb and pa[-9:] == pb[-9:]:
                found.append({"proof": "semantic_phone", "doc_a": da, "doc_b": db})
    for da, ea in sorted(a["emails"].items()):
        for db, eb in sorted(b["emails"].items()):
            if ea != eb and email_deletion_keys(ea) & email_deletion_keys(eb):
                found.append({"proof": "semantic_email", "doc_a": da, "doc_b": db})
    return found


def identifier_hints(a: dict[str, dict[str, str]], b: dict[str, dict[str, str]]) -> list[str]:
    """Hint labels for the model (no values): which kinds of evidence exist."""
    return sorted(
        {
            "phone_suffix_match" if e["proof"] == "semantic_phone" else "email_near_match"
            for e in identifier_evidence(a, b)
        }
    )


def _ratio(a: str, b: str) -> float:
    direct = SequenceMatcher(None, a, b).ratio()
    sorted_form = SequenceMatcher(
        None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    ).ratio()
    return max(direct, sorted_form)


def _best(left: list[str], right: list[str]) -> float:
    return max((_ratio(x, y) for x in left for y in right), default=0.0)


def _split_bucket(
    members: list[int], clusters: list[dict[str, Any]], profiles: list[dict[str, list[str]]]
) -> list[list[int]]:
    """Split an oversized token bucket on the entity's declared anchor.

    A bucket of dozens of documents must not be judged as one block: merged
    transitively, one wrong verdict would bridge every member into a single
    entity. The anchor comes from the entity's schema (``anchor_group``: the
    maker for a product, the venue for a publication) and is already
    normalised into the profile, so ``Adobe`` and ``Adobe Systems Inc`` share
    one subgroup while ``Microsoft`` separates -- because ``root_brand_token``
    strips the corporate suffixes that do not change the brand.

    A member WITHOUT the anchor is a wildcard and joins every sub-group: an
    absent value is not evidence of a different entity, and dropping it
    quietly costs matches (splitting on name tokens instead kept only
    47.6% / 41.4% of the golden pairs on the two DeepMatcher sets, against
    89.5% / 73.5% for this rule). Because each sub-group already contains the
    whole wildcard set, missing members meet each other inside the first
    sub-group, so they are NOT emitted again as a trailing group -- that would
    only re-evaluate pairs the wildcard already produced.

    Buckets at or below the spec's ``split_bucket_at``, and entities that
    declare no anchor at all (a person: the address is already the judge's own
    discriminator, and house_number_conflict is the code-level guard), come
    back untouched.
    """
    spec = get_schema_spec(clusters[members[0]].get("entity_type"))
    anchor = spec.anchor_group
    if anchor is None or len(members) <= spec.admission.split_bucket_at:
        return [members]
    key = anchor.canonical_name
    present = [m for m in members if profiles[m].get(key)]
    if not present:
        return [members]
    missing = [m for m in members if not profiles[m].get(key)]
    subgroups: dict[tuple[str, ...], list[int]] = {}
    for member in present:
        subgroups.setdefault(tuple(profiles[member][key]), []).append(member)
    if not missing:
        return [list(group) for group in subgroups.values()]
    return [group + missing for group in subgroups.values()]


# --------------------------------------------------------------------------
# 1. Shortlist (no model)
# --------------------------------------------------------------------------


def shortlist(
    clusters: list[dict[str, Any]],
    *,
    max_pairs: int | None = None,
    per_cluster: int | None = None,
    min_name: float = 0.8,
    min_name_with_address: float = 0.6,
    min_address: float = 0.8,
    blocking: str = "token",
    max_block: int | None = None,
    stats: dict[str, Any] | None = None,
) -> list[CandidatePair]:
    """Candidate pairs. Blocks on shared name tokens, then scores.

    Two limits apply. ``per_cluster`` keeps one cluster from being paired with
    everything (the main cost control at 10k-100k). ``max_pairs`` is optional:
    ``None`` (the default) judges every candidate, because a global cap of 200
    left 99%+ of the candidates unjudged at 100k clusters.

    Admission is declared per entity type (``EntitySchemaSpec.admission``).
    A person pair must clear ``min_name`` and be corroborated by an address or
    an identifier hint, because shared namesakes are the trap. A product or a
    paper has no address to corroborate with, so it is admitted from its much
    lower ``min_label_score`` and the judge -- not a string threshold --
    decides the middle band; measured true pairs sit at 0.69 / 0.75 median on
    Amazon-Google / DBLP-ACM, so a person-strength floor would discard a third
    of the truth. Pairs whose entity types differ are never scored at all:
    a person and a product are not in competition for the same cluster.

    ``per_cluster``, when given, is the operator's global bound and wins
    outright. When it is ``None`` each cluster falls back to its own schema's
    ``per_cluster_cap``.

    ``blocking="token"`` (default) indexes single name tokens only, so a token
    shared by more than ``max_block`` clusters blocks nothing. That is what
    empties the shortlist for names built from a few very common words (most
    Vietnamese names). ``"rarest_pair"`` also indexes every *pair* of tokens in
    a name (order-free, so ``Nguyen Thi Lan`` meets ``Lan Nguyen``) and the
    exact address: a pair of common words is rare even when each word is not.
    """
    if blocking not in BLOCKING_MODES:
        raise ValueError(f"blocking must be one of {BLOCKING_MODES}, got {blocking!r}")
    limit = max_block or COMMON_TOKEN_CLUSTERS
    entity_of = [cluster.get("entity_type") or "user" for cluster in clusters]
    admission_of = [get_schema_spec(entity).admission for entity in entity_of]
    # Person floors come from the caller (defaults equal sch-user-v1); every
    # other type uses its declared label floor -- 0.4 for product/publication.
    floor_of = [
        min_name if entity == "user" else admission_of[i].min_label_score
        for i, entity in enumerate(entity_of)
    ]
    cap_of = [
        per_cluster if per_cluster is not None else admission_of[i].per_cluster_cap
        for i in range(len(clusters))
    ]
    profiles = [cluster_profile(c) for c in clusters]
    idents = [cluster_identifiers(c) for c in clusters]
    index: dict[str, set[int]] = {}
    for i, profile in enumerate(profiles):
        for name in profile["names"]:
            tokens = name.split()
            for token in tokens:
                if len(token) >= 3 or (_has_cjk(token) and len(token) >= 2):
                    index.setdefault(token, set()).add(i)
            if blocking == "rarest_pair":
                usable = sorted({t for t in tokens if len(t) >= 2})[:6]
                for a_pos, first in enumerate(usable):
                    for second in usable[a_pos + 1 :]:
                        index.setdefault(f"pair:{first}|{second}", set()).add(i)
        if blocking == "rarest_pair":
            for address in profile["addresses"]:
                index.setdefault(f"ad:{address}", set()).add(i)
        # Near-identifier blocking keys (phone tail, email domain + local prefix).
        for digits in set(idents[i]["phones"].values()):
            index.setdefault("ph:" + digits[-9:], set()).add(i)
        for email in idents[i]["emails"].values():
            local, _, domain = email.partition("@")
            index.setdefault(f"em:{domain}:{local[:4]}", set()).add(i)

    seen: set[tuple[int, int]] = set()
    scored: list[CandidatePair] = []
    hot_keys = 0
    block_keys = 0
    split_buckets = 0
    for members in index.values():
        if len(members) < 2:
            continue
        if len(members) > limit:
            hot_keys += 1
            continue
        block_keys += 1
        groups = _split_bucket(sorted(members), clusters, profiles)
        if len(groups) > 1:
            split_buckets += 1
        for group in groups:
            for a_pos, a in enumerate(group):
                for b in group[a_pos + 1 :]:
                    if (a, b) in seen:
                        continue
                    seen.add((a, b))
                    if entity_of[a] != entity_of[b]:
                        # Different entity types are never scored against each
                        # other: a product and a paper share no cluster, and a
                        # shared token is not evidence otherwise.
                        continue
                    name = _best(profiles[a]["names"], profiles[b]["names"])
                    address = _best(profiles[a]["addresses"], profiles[b]["addresses"])
                    evidence = identifier_evidence(idents[a], idents[b])
                    hints = identifier_hints(idents[a], idents[b])
                    admitted = (
                        name >= floor_of[a]
                        or (name >= min_name_with_address and address >= min_address)
                        or bool(hints)
                    )
                    if admitted:
                        scored.append(
                            CandidatePair(
                                "",
                                a,
                                b,
                                name,
                                address,
                                profiles[a],
                                profiles[b],
                                hints,
                                evidence,
                                entity_of[a],
                            )
                        )

    scored.sort(key=lambda p: (-(p.name_score + p.address_score + len(p.hints)), p.left, p.right))
    kept: list[CandidatePair] = []
    load: dict[int, int] = {}
    skipped_per_cluster = 0
    skipped_max_pairs = 0
    for pair in scored:
        if (
            load.get(pair.left, 0) >= cap_of[pair.left]
            or load.get(pair.right, 0) >= cap_of[pair.right]
        ):
            skipped_per_cluster += 1
            continue
        load[pair.left] = load.get(pair.left, 0) + 1
        load[pair.right] = load.get(pair.right, 0) + 1
        pair.pair_id = f"p{len(kept) + 1}"
        kept.append(pair)
        if max_pairs is not None and len(kept) >= max_pairs:
            skipped_max_pairs = len(scored) - len(kept) - skipped_per_cluster
            break
    if stats is not None:
        stats["candidate_pairs"] = len(scored)
        stats["kept_pairs"] = len(kept)
        stats["skipped_per_cluster"] = skipped_per_cluster
        stats["skipped_max_pairs"] = skipped_max_pairs
        stats["blocking"] = blocking
        stats["block_keys"] = block_keys
        stats["hot_keys_skipped"] = hot_keys
        stats["split_buckets"] = split_buckets
        # A silent zero is the failure that matters: say so instead of "nothing to merge".
        if not scored and len(clusters) >= ZERO_CANDIDATE_WARN_CLUSTERS:
            named = sum(1 for p in profiles if p["names"])
            stats["warning"] = (
                f"0 candidate pairs from {len(clusters)} clusters ({named} with a name; "
                f"{hot_keys} blocking keys skipped as too common). "
                + (
                    "No cluster has a recognised name field: check the field mapping."
                    if not named
                    else "Common names are blocking nothing: try MUSE_BLOCKING_MODE=rarest_pair."
                )
            )
    return kept


# --------------------------------------------------------------------------
# 2. Judge (mock or Muse)
# --------------------------------------------------------------------------


def mock_judge(pairs: list[CandidatePair]) -> list[Verdict]:
    """Deterministic stand-in. Not a model: strict string thresholds.

    A person pair needs a name AND an address (or an identifier hint) to agree,
    because the namesake trap is real. A product or publication pair has no
    address, so it is judged on the name together with the entity's declared
    anchor (``anchor_group``: the maker or the venue) -- the first thing a
    human reads. The middle band (a good name with an anchor the threshold
    cannot reconcile) comes back ``unsure`` and never merges: that band is
    precisely what the live model is asked to decide, and a threshold must not
    pretend otherwise.
    """
    out: list[Verdict] = []
    for p in pairs:
        if p.entity_type == "user":
            if p.name_score >= 0.85 and (p.address_score >= 0.85 or p.hints):
                out.append(
                    Verdict(
                        pair_id=p.pair_id,
                        verdict="same",
                        confidence="high",
                        reason=(
                            f"name {p.name_score:.2f} matches with "
                            + (
                                "a near-identical " + "/".join(p.hints)
                                if p.hints
                                else f"address {p.address_score:.2f}"
                            )
                        ),
                    )
                )
            elif p.name_score >= 0.85:
                out.append(
                    Verdict(
                        pair_id=p.pair_id,
                        verdict="unsure",
                        confidence="low",
                        reason="name matches but the address does not confirm it",
                    )
                )
            else:
                out.append(
                    Verdict(
                        pair_id=p.pair_id,
                        verdict="different",
                        confidence="medium",
                        reason="names are not close enough",
                    )
                )
            continue
        spec = get_schema_spec(p.entity_type)
        anchor = spec.anchor_group
        strong_field = anchor.canonical_name if anchor else None
        strong = bool(
            strong_field
            and p.left_profile.get(strong_field)
            and set(p.left_profile[strong_field]) & set(p.right_profile.get(strong_field, []))
        )
        if p.name_score >= 0.85 and (strong or p.hints):
            out.append(
                Verdict(
                    pair_id=p.pair_id,
                    verdict="same",
                    confidence="high",
                    reason=f"name {p.name_score:.2f} with the same {strong_field}",
                )
            )
        elif p.name_score >= 0.85:
            out.append(
                Verdict(
                    pair_id=p.pair_id,
                    verdict="unsure",
                    confidence="low",
                    reason=f"name matches but the {strong_field} does not confirm it",
                )
            )
        elif p.name_score >= spec.admission.min_label_score:
            out.append(
                Verdict(
                    pair_id=p.pair_id,
                    verdict="unsure",
                    confidence="low",
                    reason=(
                        f"name {p.name_score:.2f} is in the band a threshold cannot "
                        "decide; a model would read the remaining fields"
                    ),
                )
            )
        else:
            out.append(
                Verdict(
                    pair_id=p.pair_id,
                    verdict="different",
                    confidence="medium",
                    reason="names are not close enough",
                )
            )
    return out


CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS muse_verdicts (
  pair_key TEXT PRIMARY KEY,
  verdict TEXT NOT NULL,
  confidence TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
)
"""


def _pair_key(settings: Settings, pair: CandidatePair) -> str:
    """Stable key for a verdict: model, schema contract, prompt, and input.

    Both ``schema_version`` and the hash of the prompt this pair was actually
    asked under belong in the key: changing a role, a normalizer or a question
    must invalidate every verdict computed under the old contract, instead of
    silently reusing a judgement the new contract would never have made.
    """
    spec = get_schema_spec(getattr(pair, "entity_type", "user"))
    prompt = system_prompt_for(spec.entity_type)
    payload = json.dumps(
        [
            settings.muse_model_id,
            spec.schema_version,
            hashlib.sha256(prompt.encode()).hexdigest()[:12],
            pair.left_profile,
            pair.right_profile,
            sorted(pair.hints),
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


CACHE_QUERY_CHUNK = 500  # SQLite caps bound parameters (~999); stay well under


def _cache_get(connection, keys: list[str]) -> dict[str, dict[str, str]]:
    rows = []
    for start in range(0, len(keys), CACHE_QUERY_CHUNK):
        chunk = keys[start : start + CACHE_QUERY_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows.extend(
            connection.execute(
                "SELECT pair_key, verdict, confidence, reason FROM muse_verdicts"
                f" WHERE pair_key IN ({marks})",
                chunk,
            ).fetchall()
        )
    return {
        row["pair_key"]: {
            "verdict": row["verdict"],
            "confidence": row["confidence"],
            "reason": row["reason"],
        }
        for row in rows
    }


def _cache_put(connection, items: list[tuple[str, Verdict]]) -> None:
    now = datetime.now(UTC).isoformat()
    connection.executemany(
        "INSERT OR REPLACE INTO muse_verdicts (pair_key, verdict, confidence, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [(key, v.verdict, v.confidence, v.reason, now) for key, v in items],
    )
    connection.commit()


async def _judge_batch(
    settings: Settings, batch: list[CandidatePair], semaphore: asyncio.Semaphore
) -> tuple[list[Verdict], int]:
    """Judge one batch. Returns (verdicts, tokens used).

    A batch can mix entity types, and each type is asked its own question
    (the cache key is computed from that question too), so it is split by
    prompt before anything leaves the process.
    """
    from meta_client import chat_json

    by_prompt: dict[str, list[CandidatePair]] = {}
    for pair in batch:
        by_prompt.setdefault(system_prompt_for(pair.entity_type), []).append(pair)

    out: list[Verdict] = []
    tokens = 0
    for prompt, group in by_prompt.items():
        payload = [
            {
                "pair_id": p.pair_id,
                "a": p.left_profile,
                "b": p.right_profile,
                "identifier_hints": p.hints,
            }
            for p in group
        ]
        async with semaphore:
            parsed, usage = await chat_json(
                settings,
                model=settings.muse_model_id,
                system=prompt,
                user="Pairs:\n" + _dump(payload),
                schema_name="mosaic_candidate_verdicts",
                schema=JUDGE_SCHEMA,
                reasoning_effort="low",
                prompt_cache_key="mosaic-candidate-judge-v1",
            )
        sent = {p.pair_id for p in group}
        got: set[str] = set()
        for item in parsed.get("verdicts", []):
            verdict = Verdict.model_validate(item)
            if verdict.pair_id not in sent:
                raise ValueError(f"verdict for unknown pair {verdict.pair_id!r}")
            if verdict.pair_id in got:
                raise ValueError(f"duplicate verdict for {verdict.pair_id!r}")
            got.add(verdict.pair_id)
            out.append(verdict)
        # A pair the model skipped simply gets no verdict, so it never merges.
        tokens += int(usage.get("total_tokens", 0) or 0)
    return out, tokens


async def live_judge(
    settings: Settings, pairs: list[CandidatePair], stats: dict[str, Any] | None = None
) -> list[Verdict]:
    """Judge all pairs, reusing cached verdicts and running batches in parallel.

    A batch that fails is skipped (its pairs get no verdict, so they never
    merge). The step fails only when every batch that had work failed, which
    ``propose_and_merge`` reports as Muse unavailable.
    """
    stats = stats if stats is not None else {}
    stats.setdefault("cache_hits", 0)
    stats.setdefault("judge_calls", 0)
    stats.setdefault("judge_failures", 0)
    stats.setdefault("tokens", 0)
    if not pairs:
        return []

    connection = None
    if settings.muse_verdict_cache:
        try:
            import ledger

            connection = ledger.connect(settings.database_path)
            connection.execute(CACHE_SCHEMA)
        except Exception:  # noqa: BLE001 - a cache must never block judging
            connection = None

    try:
        keys = {p.pair_id: _pair_key(settings, p) for p in pairs}
        cached = _cache_get(connection, list(keys.values())) if connection else {}
        by_pairs_id: dict[str, Verdict] = {}
        for pair in pairs:
            hit = cached.get(keys[pair.pair_id])
            if hit:
                by_pairs_id[pair.pair_id] = Verdict(pair_id=pair.pair_id, **hit)
        stats["cache_hits"] = len(by_pairs_id)
        missing = [p for p in pairs if p.pair_id not in by_pairs_id]

        semaphore = asyncio.Semaphore(settings.muse_judge_concurrency)
        batches = [missing[i : i + JUDGE_BATCH] for i in range(0, len(missing), JUDGE_BATCH)]
        stats["judge_calls"] = len(batches)
        results = await asyncio.gather(
            *(_judge_batch(settings, b, semaphore) for b in batches), return_exceptions=True
        )

        failures = [r for r in results if isinstance(r, BaseException)]
        fresh: list[Verdict] = []
        for result in results:
            if isinstance(result, BaseException):
                continue
            verdicts, tokens = result
            stats["tokens"] += tokens
            fresh.extend(verdicts)
        stats["judge_failures"] = len(failures)
        if failures and not fresh and not by_pairs_id:
            raise failures[0]

        if connection:
            pairs_by_id = {p.pair_id: p for p in missing}
            _cache_put(
                connection,
                [(keys[v.pair_id], v) for v in fresh if v.pair_id in pairs_by_id],
            )
        for verdict in fresh:
            by_pairs_id[verdict.pair_id] = verdict
        return [by_pairs_id[p.pair_id] for p in pairs if p.pair_id in by_pairs_id]
    finally:
        if connection is not None:
            connection.close()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------
# 3. Select merges and apply them
# --------------------------------------------------------------------------


CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def select_merges(
    pairs: list[CandidatePair], verdicts: list[Verdict], min_confidence: str = "high"
) -> list[tuple[CandidatePair, Verdict]]:
    """Pairs to merge: verdict ``same`` at or above ``min_confidence``.

    Default ``high`` is deliberately conservative. On the hard-name benchmark
    Muse answered ``same`` at ``medium`` for 39 of 318 true pairs and never said
    ``same`` for a different person, so ``medium`` is supported by the data, but
    that is a small synthetic test (see README).
    """
    if min_confidence not in ("medium", "high"):  # "low" must never merge
        raise ValueError(f"min_confidence must be medium or high, got {min_confidence!r}")
    floor = CONFIDENCE_RANK[min_confidence]
    by_id = {p.pair_id: p for p in pairs}

    def guarded(pair: CandidatePair) -> bool:
        """The house-number guard applies where the schema declares it.

        It is a person rule: a model reads "116" vs "110" as a typo. Products
        and papers declare ``require_house_number_guard=False`` -- they have no
        street to disagree about, and their profile carries no address anyway.
        """
        if not get_schema_spec(
            getattr(pair, "entity_type", "user")
        ).admission.require_house_number_guard:
            return True
        return not house_number_conflict(pair)

    return [
        (by_id[v.pair_id], v)
        for v in verdicts
        if v.verdict == "same"
        and CONFIDENCE_RANK.get(v.confidence, -1) >= floor  # unknown confidence never merges
        and v.pair_id in by_id
        and guarded(by_id[v.pair_id])
    ]


def house_number_conflict(pair: CandidatePair) -> bool:
    """True when the two sides give different house numbers and no identifier backs the link.

    A model reads "116" vs "110" as a plausible typo and merges two neighbours who
    share a street and a common name (3 false merges in 5,250 Vietnamese records).
    Numbers are the one part of an address a typo model cannot be trusted with, so
    code refuses: if every address on one side has numbers, none of them appears on
    the other side, and code found no phone or email evidence, the pair stays apart.
    Addresses without digits, or a number shared by any address, are never a conflict.
    """
    if pair.evidence:
        return False

    def numbers(addresses: list[str]) -> set[str]:
        return {n.lstrip("0") or "0" for a in addresses for n in re.findall(r"\d+", a)}

    left, right = numbers(pair.left_profile.get("addresses", [])), numbers(
        pair.right_profile.get("addresses", [])
    )
    return bool(left) and bool(right) and left.isdisjoint(right)


def apply_merges(
    clusters: list[dict[str, Any]], merges: list[tuple[CandidatePair, Verdict]]
) -> tuple[list[dict[str, Any]], int]:
    """Union clusters on accepted merges. Returns (clusters, merges_applied).

    Each merged cluster gets ``muse_links``: one claim per applied merge, to be
    emitted as a ``fuzzy_name_address`` link. A merge that would exceed
    MAX_MERGED_DOCS is skipped rather than building a giant cluster.
    """
    parent = list(range(len(clusters)))
    size = [len(c["member_docs"]) for c in clusters]
    links: dict[int, list[dict[str, Any]]] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    applied = 0
    for pair, verdict in merges:
        a, b = find(pair.left), find(pair.right)
        if a == b or size[a] + size[b] > MAX_MERGED_DOCS:
            continue
        parent[b] = a
        size[a] += size[b]
        links.setdefault(a, []).extend(links.pop(b, []))
        links.setdefault(a, []).append(
            {
                "doc_a": _representative(clusters[pair.left]),
                "doc_b": _representative(clusters[pair.right]),
                "confidence": verdict.confidence,
                "reason": verdict.reason,
                # semantic_verified: code found a near-identical phone/email as
                # supporting evidence. semantic_only: names/address alone.
                "tier": "semantic_verified" if pair.evidence else "semantic_only",
                "hints": pair.hints,
                # Engine-verifiable identifier pairs; each becomes a
                # semantic_phone / semantic_email link the policy checks (S1/S2).
                "evidence": pair.evidence,
                "name_score": round(pair.name_score, 3),
                "address_score": round(pair.address_score, 3),
            }
        )
        applied += 1

    groups: dict[int, list[int]] = {}
    for i in range(len(clusters)):
        groups.setdefault(find(i), []).append(i)
    merged: list[dict[str, Any]] = []
    for members in sorted(groups.values(), key=lambda m: m[0]):
        base = clusters[members[0]]
        docs = [d for i in members for d in clusters[i]["member_docs"]]
        merged.append(
            {
                "cluster_id": base["cluster_id"],
                "entity_type": base["entity_type"],
                "member_docs": docs,
                **(
                    {
                        "muse_links": links.get(find(members[0]), []),
                        # The separately-clustered parts, so co-location chains
                        # do not bridge a merge the engine has not verified.
                        "components": [
                            [d["document_id"] for d in clusters[i]["member_docs"]] for i in members
                        ],
                    }
                    if len(members) > 1
                    else {}
                ),
            }
        )
    for index, cluster in enumerate(merged):
        cluster["cluster_id"] = f"cluster-{index + 1}"
    return merged, applied


def _representative(cluster: dict[str, Any]) -> str:
    """The document that names this cluster in a link: prefer one carrying a label."""
    spec = get_schema_spec(cluster.get("entity_type"))
    label_fields = tuple(mapping.canonical_name for mapping in spec.fields_by_role("primary_label"))
    for document in sorted(cluster["member_docs"], key=lambda d: d["document_id"]):
        if any(_field_value(document, field_name) for field_name in label_fields):
            return document["document_id"]
    return sorted(cluster["member_docs"], key=lambda d: d["document_id"])[0]["document_id"]


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


async def propose_and_merge(
    settings: Settings, clusters: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run the three steps. Never raises: failure means no merges, labelled."""
    mode = "mock" if settings.use_mock_muse else "live"
    info: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "clusters_in": len(clusters),
        "candidate_pairs": 0,
        "shortlisted": 0,
        "judged": 0,
        "merged": 0,
        "cache_hits": 0,
        "judge_calls": 0,
        "judge_failures": 0,
        "tokens": 0,
        "elapsed_s": 0.0,
        "skipped_reason": None,
    }
    started = perf_counter()
    pairs = shortlist(
        clusters,
        max_pairs=settings.muse_candidate_max_pairs or None,
        per_cluster=settings.muse_candidate_per_cluster or None,
        blocking=settings.muse_blocking_mode,
        max_block=settings.muse_blocking_max_block or None,
        stats=info,
    )
    info["shortlisted"] = len(pairs)
    if not pairs:
        info["elapsed_s"] = round(perf_counter() - started, 1)
        return clusters, info
    try:
        verdicts = (
            mock_judge(pairs)
            if settings.use_mock_muse
            else await live_judge(settings, pairs, stats=info)
        )
    except (ValidationError, ValueError) as exc:
        info["skipped_reason"] = f"Muse output rejected: {exc}"
        info["elapsed_s"] = round(perf_counter() - started, 1)
        return clusters, info
    except Exception as exc:  # noqa: BLE001 - a model outage must never block or loosen a build
        # The type alone hides why (429 vs 413 vs breaker), which turns a
        # 9-minute run into an unexplained zero. The message stays appended;
        # callers still match on "Muse unavailable".
        kind = getattr(exc, "kind", None)
        info["skipped_reason"] = (
            f"Muse unavailable: {type(exc).__name__}" + (f"[{kind}]" if kind else "") + f": {exc}"
        )
        info["elapsed_s"] = round(perf_counter() - started, 1)
        return clusters, info
    info["judged"] = len(verdicts)
    merges = select_merges(pairs, verdicts, settings.muse_candidate_min_confidence)
    merged, applied = apply_merges(clusters, merges)
    info["merged"] = applied
    info["clusters_out"] = len(merged)
    info["elapsed_s"] = round(perf_counter() - started, 1)
    tiers: dict[str, int] = {}
    for cluster in merged:
        for link in cluster.get("muse_links", []):
            tiers[link["tier"]] = tiers.get(link["tier"], 0) + 1
    info["tiers"] = tiers
    info.update(merge_report(merged))
    info["declined"] = declined_report(pairs, verdicts, merges)
    return merged, info


MAX_REPORTED_DECLINED = 50


def declined_report(
    pairs: list[CandidatePair],
    verdicts: list[Verdict],
    merges: list[tuple[CandidatePair, Verdict]],
) -> list[dict[str, Any]]:
    """Pairs the judge looked at and did NOT join, with its reason.

    Seeing these is how a person decides whether the threshold is too strict
    (``same`` at medium confidence) or the judge is right to hold back.
    """
    joined = {pair.pair_id for pair, _ in merges}
    by_id = {p.pair_id: p for p in pairs}
    rows: list[dict[str, Any]] = []
    for verdict in verdicts:
        pair = by_id.get(verdict.pair_id)
        if pair is None or pair.pair_id in joined:
            continue
        refused_by_code = verdict.verdict == "same" and house_number_conflict(pair)
        rows.append(
            {
                "a": {
                    "name": "; ".join(pair.left_profile.get("names", [])),
                    "address": "; ".join(pair.left_profile.get("addresses", [])),
                },
                "b": {
                    "name": "; ".join(pair.right_profile.get("names", [])),
                    "address": "; ".join(pair.right_profile.get("addresses", [])),
                },
                "verdict": verdict.verdict,
                "confidence": verdict.confidence,
                "reason": verdict.reason,
                "why_not_joined": (
                    "house numbers differ and no phone or email backs the link"
                    if refused_by_code
                    else (
                        "judged same, but below the confidence needed to join"
                        if verdict.verdict == "same"
                        else f"judge said {verdict.verdict}"
                    )
                ),
            }
        )
    rows.sort(key=lambda r: (r["verdict"] != "same", r["verdict"] != "unsure"))  # near-misses first
    return rows[:MAX_REPORTED_DECLINED]


MAX_REPORTED_MERGES = 100


def _side(by_id: dict[str, dict[str, Any]], doc_id: str) -> dict[str, Any]:
    """One side of a reported merge, labelled by that entity's schema.

    This used to read ``display_name`` unconditionally, so every product and
    publication merge was reported with an empty name -- a report a person
    cannot check. The primary label comes from the spec; an address is only
    reported for an entity whose schema declares one.
    """
    doc = by_id.get(doc_id, {})
    spec = get_schema_spec(doc.get("entity_type"))
    label: str | None = None
    for mapping in spec.fields_by_role("primary_label"):
        label = _field_value(doc, mapping.canonical_name)
        if label:
            break
    return {
        "document_id": doc_id,
        "name": label,
        "address": _field_value(doc, "address") if spec.field_named("address") else None,
    }


def merge_report(merged: list[dict[str, Any]]) -> dict[str, Any]:
    """What Muse did, in terms a person can check: who was joined, and why.

    Names and addresses are exactly what the judge was sent, so this exposes
    nothing new. Capped, with the true total kept.
    """
    rows: list[dict[str, Any]] = []
    total = 0
    records = 0
    for cluster in merged:
        links = cluster.get("muse_links") or []
        if not links:
            continue
        records += len(cluster["member_docs"])
        by_id = {d["document_id"]: d for d in cluster["member_docs"]}
        for link in links:
            total += 1
            if len(rows) >= MAX_REPORTED_MERGES:
                continue
            rows.append(
                {
                    "cluster_id": cluster["cluster_id"],
                    "a": _side(by_id, link["doc_a"]),
                    "b": _side(by_id, link["doc_b"]),
                    "confidence": link["confidence"],
                    "reason": link["reason"],
                    "tier": link["tier"],
                    "evidence": sorted({e["proof"] for e in link.get("evidence", [])}),
                }
            )
    return {"merges": rows, "merges_total": total, "records_regrouped": records}


def link_claims(
    cluster: dict[str, Any], facts: list, fact_documents: dict[str, str]
) -> list[dict[str, Any]]:
    """Link claims for a Muse-merged cluster.

    The name-style claim carries the proof type the entity's schema declares
    (``link_proof_type``; every shipped spec uses ``fuzzy_name_address``, so a
    names-only link never verifies -- names alone are not proof). Plus one
    ``semantic_phone`` / ``semantic_email`` claim per code-found identifier pair;
    the engine verifies those against its own features (policy rules S1/S2).
    """

    def fact_of(doc: str, attribute: str) -> str | None:
        ids = sorted(
            f.fact_id
            for f in facts
            if f.attribute == attribute and fact_documents.get(f.fact_id) == doc
        )
        return ids[0] if ids else None

    def first_fact(doc: str) -> str | None:
        ids = sorted(f.fact_id for f in facts if fact_documents.get(f.fact_id) == doc)
        return ids[0] if ids else None

    claims: list[dict[str, Any]] = []
    # The proof type is declared by the entity's schema. Every shipped spec
    # uses the proof the trust-engine already knows how to refuse-to-certify;
    # a new entity may declare its own once the engine verifies it.
    link_proof = get_schema_spec(cluster.get("entity_type")).link_proof_type
    for link in cluster.get("muse_links", []):
        a, b = first_fact(link["doc_a"]), first_fact(link["doc_b"])
        if a and b:
            claims.append(
                {"from_fact_id": a, "to_fact_id": b, "proof": link_proof, "proposer": "Muse"}
            )
        for item in link.get("evidence", []):
            attribute = "phone" if item["proof"] == "semantic_phone" else "email"
            fa, fb = fact_of(item["doc_a"], attribute), fact_of(item["doc_b"], attribute)
            if fa and fb:
                claims.append(
                    {
                        "from_fact_id": fa,
                        "to_fact_id": fb,
                        "proof": item["proof"],
                        "proposer": "Muse",
                    }
                )
    return claims

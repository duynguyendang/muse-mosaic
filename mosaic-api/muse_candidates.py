"""Muse candidate matching: find clusters the exact identifiers missed.

Splink groups records that share an exact identifier (email, phone, order id).
A customer whose phone AND email changed shares none, so exact blocking leaves
them as two clusters, and the system would certify two goldens for one person.
This step looks for exactly those near-matches.

Scale is the point. Judging every pair of clusters is O(n^2), so a machine
narrows first and the model only judges what is left:

1. ``shortlist``: token blocking + cheap string scores pick a bounded set of
   candidate cluster pairs. No model is involved.
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

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal

from config import Settings
from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROOF = "fuzzy_name_address"
JUDGE_BATCH = 20
MAX_MERGED_DOCS = 20  # never let chained merges build a giant cluster
COMMON_TOKEN_CLUSTERS = 50  # tokens in more clusters than this block nothing

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


# --------------------------------------------------------------------------
# Profiles and string scores
# --------------------------------------------------------------------------


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", folded)).strip()


def _field_value(document: dict[str, Any], name: str) -> str | None:
    raw = document.get("fields", {}).get(name)
    value = raw.get("value") if isinstance(raw, dict) else raw
    return str(value) if value else None


def cluster_profile(cluster: dict[str, Any]) -> dict[str, list[str]]:
    """Normalised names and addresses of one cluster. Nothing else leaves."""
    names: set[str] = set()
    addresses: set[str] = set()
    for document in cluster["member_docs"]:
        if (name := _field_value(document, "display_name")) and _norm(name):
            names.add(_norm(name))
        if (address := _field_value(document, "address")) and _norm(address):
            addresses.add(_norm(address))
    return {"names": sorted(names), "addresses": sorted(addresses)}


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


# --------------------------------------------------------------------------
# 1. Shortlist (no model)
# --------------------------------------------------------------------------


def shortlist(
    clusters: list[dict[str, Any]],
    *,
    max_pairs: int = 200,
    per_cluster: int = 3,
    min_name: float = 0.8,
    min_name_with_address: float = 0.6,
    min_address: float = 0.8,
) -> list[CandidatePair]:
    """Bounded candidate pairs. Blocks on shared name tokens, then scores."""
    profiles = [cluster_profile(c) for c in clusters]
    idents = [cluster_identifiers(c) for c in clusters]
    index: dict[str, set[int]] = {}
    for i, profile in enumerate(profiles):
        for name in profile["names"]:
            for token in name.split():
                if len(token) >= 3:
                    index.setdefault(token, set()).add(i)
        # Near-identifier blocking keys (phone tail, email domain + local prefix).
        for digits in set(idents[i]["phones"].values()):
            index.setdefault("ph:" + digits[-9:], set()).add(i)
        for email in idents[i]["emails"].values():
            local, _, domain = email.partition("@")
            index.setdefault(f"em:{domain}:{local[:4]}", set()).add(i)

    seen: set[tuple[int, int]] = set()
    scored: list[CandidatePair] = []
    for members in index.values():
        if len(members) < 2 or len(members) > COMMON_TOKEN_CLUSTERS:
            continue
        ordered = sorted(members)
        for a_pos, a in enumerate(ordered):
            for b in ordered[a_pos + 1 :]:
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                name = _best(profiles[a]["names"], profiles[b]["names"])
                address = _best(profiles[a]["addresses"], profiles[b]["addresses"])
                evidence = identifier_evidence(idents[a], idents[b])
                hints = identifier_hints(idents[a], idents[b])
                if (
                    name >= min_name
                    or (name >= min_name_with_address and address >= min_address)
                    or hints
                ):
                    scored.append(
                        CandidatePair(
                            "", a, b, name, address, profiles[a], profiles[b], hints, evidence
                        )
                    )

    scored.sort(key=lambda p: (-(p.name_score + p.address_score + len(p.hints)), p.left, p.right))
    kept: list[CandidatePair] = []
    load: dict[int, int] = {}
    for pair in scored:
        if load.get(pair.left, 0) >= per_cluster or load.get(pair.right, 0) >= per_cluster:
            continue
        load[pair.left] = load.get(pair.left, 0) + 1
        load[pair.right] = load.get(pair.right, 0) + 1
        pair.pair_id = f"p{len(kept) + 1}"
        kept.append(pair)
        if len(kept) >= max_pairs:
            break
    return kept


# --------------------------------------------------------------------------
# 2. Judge (mock or Muse)
# --------------------------------------------------------------------------


def mock_judge(pairs: list[CandidatePair]) -> list[Verdict]:
    """Deterministic stand-in. Not a model: strict string thresholds."""
    out: list[Verdict] = []
    for p in pairs:
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
    return out


async def live_judge(settings: Settings, pairs: list[CandidatePair]) -> list[Verdict]:
    from meta_client import chat_json

    verdicts: list[Verdict] = []
    for start in range(0, len(pairs), JUDGE_BATCH):
        batch = pairs[start : start + JUDGE_BATCH]
        payload = [
            {
                "pair_id": p.pair_id,
                "a": p.left_profile,
                "b": p.right_profile,
                "identifier_hints": p.hints,
            }
            for p in batch
        ]
        parsed, _usage = await chat_json(
            settings,
            model=settings.muse_model_id,
            system=SYSTEM_PROMPT,
            user="Pairs:\n" + _dump(payload),
            schema_name="mosaic_candidate_verdicts",
            schema=JUDGE_SCHEMA,
            reasoning_effort="low",
            prompt_cache_key="mosaic-candidate-judge-v1",
        )
        sent = {p.pair_id for p in batch}
        got: set[str] = set()
        for item in parsed.get("verdicts", []):
            verdict = Verdict.model_validate(item)
            if verdict.pair_id not in sent:
                raise ValueError(f"verdict for unknown pair {verdict.pair_id!r}")
            if verdict.pair_id in got:
                raise ValueError(f"duplicate verdict for {verdict.pair_id!r}")
            got.add(verdict.pair_id)
            verdicts.append(verdict)
        # A pair the model skipped simply gets no verdict, so it never merges.
    return verdicts


def _dump(value: Any) -> str:
    import json

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
    return [
        (by_id[v.pair_id], v)
        for v in verdicts
        if v.verdict == "same"
        and CONFIDENCE_RANK.get(v.confidence, -1) >= floor  # unknown confidence never merges
        and v.pair_id in by_id
    ]


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
    for document in sorted(cluster["member_docs"], key=lambda d: d["document_id"]):
        if _field_value(document, "display_name"):
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
        "shortlisted": 0,
        "judged": 0,
        "merged": 0,
        "skipped_reason": None,
    }
    pairs = shortlist(clusters)
    info["shortlisted"] = len(pairs)
    if not pairs:
        return clusters, info
    try:
        verdicts = (
            mock_judge(pairs) if settings.use_mock_muse else await live_judge(settings, pairs)
        )
    except (ValidationError, ValueError) as exc:
        info["skipped_reason"] = f"Muse output rejected: {exc}"
        return clusters, info
    except Exception as exc:  # noqa: BLE001 - a model outage must never block or loosen a build
        info["skipped_reason"] = f"Muse unavailable: {type(exc).__name__}"
        return clusters, info
    info["judged"] = len(verdicts)
    merges = select_merges(pairs, verdicts, settings.muse_candidate_min_confidence)
    merged, applied = apply_merges(clusters, merges)
    info["merged"] = applied
    info["clusters_out"] = len(merged)
    tiers: dict[str, int] = {}
    for cluster in merged:
        for link in cluster.get("muse_links", []):
            tiers[link["tier"]] = tiers.get(link["tier"], 0) + 1
    info["tiers"] = tiers
    return merged, info


def link_claims(
    cluster: dict[str, Any], facts: list, fact_documents: dict[str, str]
) -> list[dict[str, Any]]:
    """Link claims for a Muse-merged cluster.

    Always a ``fuzzy_name_address`` claim (names alone: never verifies). Plus one
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
    for link in cluster.get("muse_links", []):
        a, b = first_fact(link["doc_a"]), first_fact(link["doc_b"])
        if a and b:
            claims.append({"from_fact_id": a, "to_fact_id": b, "proof": PROOF, "proposer": "Muse"})
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

"""Entity-aware judging: profile, prompt, admission gate, bucket split.

These are the three fixes the DeepMatcher benchmark demanded, each guarded on
its own so a regression is named rather than discovered as a lower score:

1. the judge is shown what the entity actually has (a product's maker, a
   paper's venue) instead of two empty person-shaped lists;
2. it is asked the question for that entity, and admitted from a low name
   gate so the 0.6-0.85 band reaches it rather than being thresholded away;
3. an oversized token bucket is split on a strong field first, so one wrong
   verdict cannot bridge the whole bucket -- without dropping pairs whose
   strong field is simply absent.

And the wiring the whole contract is worthless without: the build path runs
Muse for every entity type that declares a schema, and one build reports what
each of them did instead of letting the last entity type overwrite the rest.
"""

from __future__ import annotations

import asyncio
import dataclasses

import muse_candidates as mc
import pytest
from config import Settings


def product(doc_id, name, manufacturer=None, price=None, source="amazon"):
    fields = {"name": {"value": name}}
    if manufacturer:
        fields["manufacturer"] = {"value": manufacturer}
    if price:
        fields["price"] = {"value": price}
    return {
        "document_id": doc_id,
        "entity_type": "product",
        "source": source,
        "fields": fields,
    }


def paper(doc_id, title, authors=None, venue=None, year=None, source="dblp"):
    fields = {"name": {"value": title}}
    if authors:
        fields["authors"] = {"value": authors}
    if venue:
        fields["venue"] = {"value": venue}
    if year:
        fields["year"] = {"value": year}
    return {
        "document_id": doc_id,
        "entity_type": "publication",
        "source": source,
        "fields": fields,
    }


def pc(index, *docs, entity_type="product"):
    return {
        "cluster_id": f"cluster-{index}",
        "entity_type": entity_type,
        "member_docs": list(docs),
    }


# --- 1. the judge sees the entity's own fields ------------------------------


def test_product_profile_carries_maker_and_price():
    profile = mc.cluster_profile(pc(1, product("a", "ClickArt Premier", "Broderbund", "19.99")))
    assert profile["names"] == ["clickart premier"]
    assert profile["manufacturer"] == ["broderbund"]
    assert profile["price"] == ["19.99"]
    # addresses always exist because shortlist indexes and scores them
    assert profile["addresses"] == []


def test_publication_profile_carries_authors_venue_and_year():
    """Raw aliases are canonicalised first; the profile reads canonical fields.

    Reading ``name`` straight from a raw document is exactly what the G1 test
    forbids (raw schema -> empty profile -> 0 candidates), so this goes through
    canonicalize_documents, which now applies the spec's aliases even with no
    taxonomy. ``authors`` is text_fold, so punctuation is stripped -- the
    characters a price or venue needs are preserve_punct's job, not this one.
    """
    raw = paper("a", "Semantic Integration", "Poosala, Ioannidis", "sigmod record", "1999")
    doc = mc.canonicalize_documents([raw], None)[0]
    assert "title" in doc["fields"] and "name" not in doc["fields"]  # alias applied
    profile = mc.cluster_profile(pc(1, doc, entity_type="publication"))
    assert profile["names"] == ["semantic integration"]
    assert profile["authors"] == ["poosala ioannidis"]
    assert profile["venue"] == ["sigmod record"]
    assert profile["year"] == ["1999"]


def test_price_keeps_its_decimal_point_for_the_judge():
    """_norm is for matching; the judge must not be shown '19 99'."""
    profile = mc.cluster_profile(pc(1, product("a", "ClickArt", "Broderbund", "19.99")))
    assert profile["price"] == ["19.99"]


def test_person_profile_is_still_only_names_and_addresses():
    """The privacy contract: no phone, no email, for the person type."""
    doc = {
        "document_id": "u1",
        "entity_type": "user",
        "source": "crm",
        "fields": {
            "display_name": {"value": "Maria Day"},
            "address": {"value": "12 Elm Street"},
            "email": {"value": "e@x.com"},
            "phone": {"value": "0900"},
        },
    }
    profile = mc.cluster_profile(pc(1, doc))
    assert set(profile) == {"names", "addresses"}
    assert "e@x.com" not in str(profile) and "0900" not in str(profile)


def test_unknown_entity_type_fails_closed():
    """An unregistered type must raise, never be silently judged as a person."""
    doc = {"document_id": "x", "entity_type": "vehicle", "fields": {"name": {"value": "abc"}}}
    with pytest.raises(ValueError, match="no schema registered"):
        mc.cluster_profile({"cluster_id": "c", "entity_type": "vehicle", "member_docs": [doc]})


# --- 2. the right question, and the middle band gets through ---------------


def test_each_entity_gets_its_own_question():
    assert mc.system_prompt_for("user") is mc.SYSTEM_PROMPT
    assert mc.system_prompt_for(None) is mc.SYSTEM_PROMPT
    product_prompt = mc.system_prompt_for("product")
    publication_prompt = mc.system_prompt_for("publication")
    assert "SAME product" in product_prompt and "person" not in product_prompt
    assert "SAME paper" in publication_prompt and "person" not in publication_prompt
    assert product_prompt != publication_prompt
    # an unregistered type must never be silently asked the person question
    with pytest.raises(ValueError, match="no schema registered"):
        mc.system_prompt_for("vehicle")


def test_a_product_is_never_keyed_to_a_person_verdict():
    """Same pair, different entity -> different cache key (prompt is hashed)."""
    settings = Settings()
    kwargs = dict(
        pair_id="p1",
        left=0,
        right=1,
        name_score=0.7,
        address_score=0.0,
        left_profile={"names": ["a"], "addresses": []},
        right_profile={"names": ["a"], "addresses": []},
    )
    person = mc.CandidatePair(**kwargs)
    thing = mc.CandidatePair(entity_type="product", **kwargs)
    assert mc._pair_key(settings, person) != mc._pair_key(settings, thing)
    # two products share their key: same model, same question, same input
    assert mc._pair_key(settings, thing) == mc._pair_key(
        settings, mc.CandidatePair(entity_type="product", **kwargs)
    )


def test_middle_band_reaches_the_judge_for_products_but_not_people():
    """0.5-ish name similarity: admitted for a product, withheld for a person."""
    product_clusters = [
        pc(1, product("a", "QuickBooks 2007", "Intuit")),
        pc(2, product("b", "Quickbooks two thousand seven", "Intuit")),
    ]
    person_clusters = [
        pc(
            1,
            {
                "document_id": "u1",
                "entity_type": "user",
                "source": "crm",
                "fields": {"display_name": {"value": "Maria Day"}},
            },
        ),
        pc(
            2,
            {
                "document_id": "u2",
                "entity_type": "user",
                "source": "crm",
                "fields": {"display_name": {"value": "Mariam Daya"}},
            },
        ),
    ]
    product_pairs = mc.shortlist(product_clusters)
    assert product_pairs, "a product pair in the middle band must be judged"
    assert all(p.name_score < 0.8 for p in product_pairs)
    assert all(p.entity_type == "product" for p in product_pairs)

    person_pairs = mc.shortlist(person_clusters)
    # whatever the person shortlist produced, nothing below the person gate got in
    assert all(p.name_score >= 0.8 for p in person_pairs)


def test_mock_judge_needs_the_strong_field_to_confirm_a_product():
    pair = mc.CandidatePair(
        pair_id="p1",
        left=0,
        right=1,
        name_score=0.9,
        address_score=0.0,
        left_profile={"names": ["quickbooks 2007"], "addresses": [], "manufacturer": ["intuit"]},
        right_profile={"names": ["quickbooks 2007"], "addresses": [], "manufacturer": ["intuit"]},
        entity_type="product",
    )
    assert mc.mock_judge([pair])[0].verdict == "same"
    # same title, a different maker: the mock must not confirm it
    pair.right_profile["manufacturer"] = ["oracle"]
    undecided = mc.mock_judge([pair])[0]
    assert undecided.verdict == "unsure"  # never 'same', never auto-merged


def test_mock_judge_keeps_the_middle_band_unmerged():
    """The mock must not pretend to decide what only a model can."""
    pair = mc.CandidatePair(
        pair_id="p1",
        left=0,
        right=1,
        name_score=0.7,
        address_score=0.0,
        left_profile={"names": ["alpha product"], "addresses": []},
        right_profile={"names": ["alpha prodkt"], "addresses": []},
        entity_type="product",
    )
    verdict = mc.mock_judge([pair])[0]
    assert verdict.verdict == "unsure"
    assert mc.select_merges([pair], [verdict], "medium") == []


def test_medium_confidence_is_still_the_thing_that_merges():
    """min-confidence stays the switch: high is default, low never merges."""
    pair = mc.CandidatePair(
        pair_id="p1",
        left=0,
        right=1,
        name_score=0.9,
        address_score=0.0,
        left_profile={"names": ["a"], "addresses": []},
        right_profile={"names": ["a"], "addresses": []},
        entity_type="product",
    )
    same_medium = mc.Verdict(pair_id="p1", verdict="same", confidence="medium", reason="maker+name")
    same_low = mc.Verdict(pair_id="p1", verdict="same", confidence="low", reason="weak")
    assert mc.select_merges([pair], [same_medium]) == []  # default floor is high
    assert len(mc.select_merges([pair], [same_medium], "medium")) == 1
    assert mc.select_merges([pair], [same_low], "medium") == []


# --- 3. an oversized bucket is split before anyone decides -----------------


def _bucket(members, entity_type="product", strong=None):
    """Clusters whose only shared key is one common token."""
    clusters = []
    for i, member in enumerate(members):
        fields = {"name": {"value": f"{member} widget"}}
        if strong and strong[i]:
            fields["manufacturer"] = {"value": strong[i]}
        clusters.append(
            {
                "cluster_id": f"c{i}",
                "entity_type": entity_type,
                "member_docs": [
                    {
                        "document_id": f"d{i}",
                        "entity_type": entity_type,
                        "source": "amazon",
                        "fields": fields,
                    }
                ],
            }
        )
    return clusters


def test_a_large_product_bucket_splits_on_the_maker():
    names = [f"Widget {i}" for i in range(10)]
    makers = ["acme"] * 5 + ["globex"] * 5
    clusters = _bucket(names, strong=makers)
    groups = mc._split_bucket(list(range(10)), clusters, [mc.cluster_profile(c) for c in clusters])
    assert len(groups) == 2
    assert sorted(len(g) for g in groups) == [5, 5]


def test_a_missing_strong_field_is_a_wildcard_not_a_lost_pair():
    """Absent maker is not evidence of a different entity."""
    clusters = _bucket([f"Widget {i}" for i in range(10)], strong=["acme"] * 5 + [None] * 5)
    profiles = [mc.cluster_profile(c) for c in clusters]
    groups = mc._split_bucket(list(range(10)), clusters, profiles)
    # the 5 with a maker split off from each other, the 5 without meet everyone
    all_pairs = {frozenset((a, b)) for g in groups for i, a in enumerate(g) for b in g[i + 1 :]}
    for with_maker in range(5):
        for without in range(5, 10):
            assert (
                frozenset((with_maker, without)) in all_pairs
            ), "missing maker must not cost a pair"


def test_small_buckets_and_people_are_left_alone():
    # small: untouched even for a product
    count = mc.SPLIT_BUCKET_AT
    clusters = _bucket([f"W {i}" for i in range(count)], strong=["a"] * count)
    profiles = [mc.cluster_profile(c) for c in clusters]
    assert mc._split_bucket(list(range(count)), clusters, profiles) == [list(range(count))]

    # people: never split, whatever the size (addresses are already the
    # judge's own discriminator, and house_number_conflict is the guard)
    people = []
    for i in range(mc.SPLIT_BUCKET_AT * 3):
        people.append(
            {
                "cluster_id": f"c{i}",
                "entity_type": "user",
                "member_docs": [
                    {
                        "document_id": f"u{i}",
                        "entity_type": "user",
                        "source": "crm",
                        "fields": {"display_name": {"value": f"Person{i} Common"}},
                    }
                ],
            }
        )
    profiles = [mc.cluster_profile(c) for c in people]
    whole = list(range(len(people)))
    assert mc._split_bucket(whole, people, profiles) == [whole]


def test_a_split_bucket_still_reports_itself():
    clusters = _bucket([f"Widget {i}" for i in range(12)], strong=["acme"] * 6 + ["globex"] * 6)
    stats = {}
    pairs = mc.shortlist(clusters, stats=stats)
    assert stats["split_buckets"] == 1
    assert pairs  # splitting must not empty the shortlist


def test_live_judge_batches_are_grouped_by_prompt(monkeypatch):
    """A mixed batch must ask each entity its own question, and key on it."""
    import json as _json

    seen = []

    async def fake(settings, *, model, system, user, schema_name, schema, **kwargs):
        payload = _json.loads(user.split("\n", 1)[1])
        seen.append(system)
        return (
            {
                "verdicts": [
                    {
                        "pair_id": p["pair_id"],
                        "verdict": "same",
                        "confidence": "high",
                        "reason": "ok",
                    }
                    for p in payload
                ]
            },
            {"total_tokens": 10},
        )

    import meta_client

    monkeypatch.setattr(meta_client, "chat_json", fake)
    batch = [
        mc.CandidatePair(
            pair_id="p1", left=0, right=1, name_score=0.9, address_score=0.0, entity_type="product"
        ),
        mc.CandidatePair(pair_id="p2", left=2, right=3, name_score=0.9, address_score=0.9),
    ]
    verdicts, tokens = asyncio.run(
        mc._judge_batch(Settings(use_mock_muse=False), batch, asyncio.Semaphore(2))
    )
    assert len(verdicts) == 2 and tokens == 20
    assert set(seen) == {mc.SYSTEM_PROMPT, mc.PRODUCT_PROMPT}


def test_outage_message_still_reads_as_unavailable(monkeypatch):
    async def boom(settings, pairs, stats=None):
        raise RuntimeError("down")

    monkeypatch.setattr(mc, "live_judge", boom)
    clusters = [pc(1, product("a", "Widget", "Acme")), pc(2, product("b", "Widget", "Acme"))]
    settings = dataclasses.replace(Settings(), use_mock_muse=False)
    out, info = asyncio.run(mc.propose_and_merge(settings, clusters))
    assert out == clusters and info["merged"] == 0
    assert "unavailable" in info["skipped_reason"]
    assert "down" in info["skipped_reason"]  # the reason is no longer hidden


# --- 4. the build path runs Muse for every declared entity type -------------


def _through_build(*docs):
    """The real matching path: canonicalise -> cluster -> Muse, as /build does."""
    from policies import load_policies
    from routes.build import _build_clusters
    from taxonomies import load_taxonomies

    settings = dataclasses.replace(Settings(), use_mock_muse=True, muse_candidate_matching=True)
    out: dict = {}
    clusters = asyncio.run(
        _build_clusters(settings, load_policies(), load_taxonomies(), list(docs), out)
    )
    return clusters, out


def person(doc_id, name, address, source="crm"):
    return {
        "document_id": doc_id,
        "entity_type": "user",
        "source": source,
        "fields": {"display_name": {"value": name}, "address": {"value": address}},
    }


def test_a_product_build_reaches_muse_matching_too():
    """G1 exit criterion: no ``entity_type == "user"`` left in the matching path.

    Two products share no exact identifier (the product policy blocks on
    ``category``, which neither carries), so only Muse can link them -- and
    the schema contract has given it a product question, a product admission
    gate and a maker to check. Refusing to run it for a product is the
    coupling this work was meant to remove.
    """
    clusters, out = _through_build(
        product("a", "QuickBooks 2007", "Intuit", source="amazon"),
        product("b", "quickbooks 2007", "Intuit", source="google"),
    )
    info = out.get("muse_candidates")
    assert info, "candidate matching did not run for a product build"
    assert info["merged"] == 1
    assert sorted(len(c["member_docs"]) for c in clusters) == [2]


def test_one_build_reports_every_entity_types_muse_run():
    """A mixed build must not let the last entity type overwrite the rest.

    ``splink_out["muse_candidates"]`` is one dict, and it is what the build
    report and the impact view read. Written once per entity type in sorted
    order it would show only the last type's numbers -- the impact view would
    then claim a model that judged one entity never touched the other.
    """
    clusters, out = _through_build(
        product("a", "QuickBooks 2007", "Intuit", source="amazon"),
        product("b", "quickbooks 2007", "Intuit", source="google"),
        person("u1", "Maria Day", "12 Elm Street Hanoi"),
        person("u2", "maria  DAY", "12 elm st hanoi"),
    )
    info = out["muse_candidates"]
    assert info["merged"] == 2  # one product merge AND one person merge
    assert sorted(len(c["member_docs"]) for c in clusters) == [2, 2]
    assert set(info["by_entity"]) == {"product", "user"}
    assert info["by_entity"]["product"]["merged"] == 1
    assert info["by_entity"]["user"]["merged"] == 1

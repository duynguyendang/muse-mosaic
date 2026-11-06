"""The schema contract: PII boundary, brand split, edition hints, aliases.

Five properties the contract must hold, each checked without a model:

1. a ``private_identifier`` never appears in a profile or in a compiled prompt;
2. a large bucket splits on the declared anchor once brand suffixes are
   rooted, with absent makers kept as wildcards;
3. edition/platform/tier tokens are extracted into the judge payload;
4. raw aliases canonicalise to the contract's field names, with values like
   ``$19.99`` surviving as ``19.99``;
5. a verdict cache key carries the schema version, so a contract change can
   never reuse a verdict computed under the old one.
"""

from __future__ import annotations

import dataclasses

import muse_candidates as mc
import pytest
from config import Settings
from schema_contract import (
    DEFAULT_SCHEMA_SPECS,
    PRODUCT_SPEC,
    PUBLICATION_SPEC,
    USER_SPEC,
    AdmissionPolicy,
    EntitySchemaSpec,
    FieldMapping,
    apply_normalizer,
    compile_judge_prompt,
    get_schema_spec,
)


def _product(doc_id, title, maker=None, price=None, source="amazon"):
    fields = {"name": {"value": title}}
    if maker:
        fields["manufacturer"] = {"value": maker}
    if price:
        fields["price"] = {"value": price}
    return {
        "document_id": doc_id,
        "entity_type": "product",
        "source": source,
        "fields": fields,
    }


def _cluster(entity_type, *docs):
    return {
        "cluster_id": "cluster-1",
        "entity_type": entity_type,
        "member_docs": list(docs),
    }


# --- 1. PII boundary --------------------------------------------------------


def test_private_identifiers_never_reach_profile_or_prompt():
    """Structural, not procedural: the spec's visible set IS the boundary."""
    person = {
        "document_id": "u1",
        "entity_type": "user",
        "source": "crm",
        "fields": {
            "display_name": {"value": "Maria Day"},
            "address": {"value": "12 Elm Street"},
            "phone": {"value": "0900111222"},
            "email": {"value": "maria@example.com"},
        },
    }
    profile = mc.cluster_profile(_cluster("user", person))
    assert set(profile) == {"names", "addresses"}
    assert "0900111222" not in str(profile) and "maria@example.com" not in str(profile)

    for spec in DEFAULT_SCHEMA_SPECS.values():
        visible = {mapping.canonical_name for mapping in spec.visible_to_judge()}
        declared = {mapping.canonical_name for mapping in spec.fields}
        private = {
            mapping.canonical_name
            for mapping in spec.fields
            if mapping.role == "private_identifier"
        }
        assert visible.isdisjoint(private), spec.schema_version
        assert private <= declared
        prompt = compile_judge_prompt(spec)
        for name in private:
            assert name not in prompt, f"{spec.schema_version} prompt names {name}"


def test_ignored_fields_are_also_invisible():
    spec = dataclasses.replace(
        USER_SPEC,
        schema_version="sch-test-ignored",
        entity_type="ignored_type",
        fields=USER_SPEC.fields
        + (
            FieldMapping(
                canonical_name="internal_note",
                source_aliases=("note",),
                role="ignored",
                normalizer="text_fold",
            ),
        ),
    )
    visible = {m.canonical_name for m in spec.visible_to_judge()}
    assert "internal_note" not in visible
    assert "internal_note" not in compile_judge_prompt(spec)


def test_frozen_person_prompt_carries_no_values():
    """The contract question may NAME the hint concept; never a value."""
    prompt = mc.system_prompt_for("user")
    assert prompt == USER_SPEC.question
    assert "@" not in prompt and "0900" not in prompt


# --- 2. brand rooting drives the split --------------------------------------


def test_brand_rooting_groups_adobe_and_splits_microsoft():
    assert apply_normalizer("Adobe", "root_brand_token") == "adobe"
    assert apply_normalizer("Adobe Systems Inc", "root_brand_token") == "adobe"
    assert apply_normalizer("Microsoft Corp", "root_brand_token") == "microsoft"

    docs = []
    makers = ["Adobe"] * 3 + ["Adobe Systems Inc"] * 3 + ["Microsoft Corp"] * 3 + [None] * 5
    for i, maker in enumerate(makers):
        docs.append(_product(f"p{i}", f"Graphics Suite {i}", maker))
    clusters = [_cluster("product", doc) for doc in docs]
    profiles = [mc.cluster_profile(c) for c in clusters]
    members = list(range(len(clusters)))

    groups = mc._split_bucket(members, clusters, profiles)
    assert len(groups) == 2, "brand roots, not raw strings, decide the groups"

    adobe = {i for i, m in enumerate(makers) if m and m.startswith("Adobe")}
    microsoft = {i for i, m in enumerate(makers) if m and m.startswith("Microsoft")}
    missing = {i for i, m in enumerate(makers) if m is None}

    adobe_group = next(g for g in groups if adobe <= set(g))
    assert not (set(adobe_group) & microsoft)
    # absent maker is a wildcard: it joins every group rather than being dropped
    assert all(missing <= set(g) for g in groups)
    # ...so no trailing missing-only group is emitted alongside them
    assert not any(set(g) == missing for g in groups)

    stats: dict = {}
    pairs = mc.shortlist(clusters, stats=stats)
    # both "graphics" and "suite" carry the whole bucket, so both split
    assert stats["split_buckets"] == 2
    assert pairs, "splitting must leave the bucket judgeable"
    # and no Adobe<->Microsoft pair was ever scored
    for pair in pairs:
        assert not ({pair.left, pair.right} & adobe and {pair.left, pair.right} & microsoft)


def test_a_bucket_under_the_threshold_is_not_split():
    docs = [_product(f"p{i}", f"Widget {i}", "Acme" if i % 2 else "Globex") for i in range(4)]
    clusters = [_cluster("product", doc) for doc in docs]
    profiles = [mc.cluster_profile(c) for c in clusters]
    assert mc._split_bucket(list(range(4)), clusters, profiles) == [list(range(4))]


# --- 3. edition discriminators ----------------------------------------------


def test_edition_tokens_split_upgrade_from_full():
    upgrade = mc.cluster_profile(
        _cluster("product", _product("a", "Adobe Photoshop CS3 Upgrade (Mac)"))
    )
    full = mc.cluster_profile(
        _cluster("product", _product("b", "Adobe Photoshop CS3 Full Version (Windows)"))
    )
    assert upgrade["edition_hints"] == ["cs3", "mac", "upgrade"]
    assert full["edition_hints"] == ["cs3", "full", "windows"]
    # both are visible to the judge: this is the discriminator it is told to read
    names = {m.canonical_name for m in PRODUCT_SPEC.visible_to_judge()}
    assert "edition_hints" in names
    assert "conflicting edition/platform/tier tokens" in compile_judge_prompt(PRODUCT_SPEC)


# --- 4. aliases canonicalise, values survive --------------------------------


def test_raw_product_aliases_canonicalise_with_values_preserved():
    raw = {
        "document_id": "x",
        "entity_type": "product",
        "source": "shop",
        "fields": {
            "prod_title": {"value": "ClickArt Premier"},
            "brand": {"value": "Broderbund"},
            "msrp": {"value": "$19.99"},
        },
    }
    doc = mc.canonicalize_documents([raw], None)[0]
    assert set(doc["fields"]) == {"name", "manufacturer", "price"}
    assert doc["fields"]["price"]["value"] == "$19.99"  # renaming never edits a value

    profile = mc.cluster_profile(
        {"cluster_id": "c", "entity_type": "product", "member_docs": [doc]}
    )
    assert profile["names"] == ["clickart premier"]
    assert profile["manufacturer"] == ["broderbund"]
    assert profile["price"] == ["19.99"]  # the currency sign is not the number


def test_taxonomy_still_overrides_the_static_aliases():
    class _Tax:
        attribute_aliases = {"name": ["sku_title"]}  # dynamic beats spec

    doc = {
        "document_id": "y",
        "entity_type": "product",
        "fields": {"sku_title": {"value": "X"}, "title": {"value": "Y"}},
    }
    out = mc.canonicalize_documents([doc], _Tax())[0]
    # the taxonomy alias wins the rename race to `name`
    assert out["fields"]["name"]["value"] == "X"
    # the spec's own alias lost the race, so its raw field is KEPT, not dropped
    assert out["fields"]["title"] == {"value": "Y"}


# --- 5. the cache key carries the contract ----------------------------------


def test_cache_key_carries_the_schema_version(monkeypatch):
    """Identical prompt + identical input, different schema -> different key."""
    first = EntitySchemaSpec(
        schema_version="sch-twin-a",
        entity_type="twin_a",
        entity_noun="thing",
        link_proof_type="fuzzy_name_address",
        admission=AdmissionPolicy(0.5, 0.6, 0.8),
        fields=PRODUCT_SPEC.fields,
    )
    second = dataclasses.replace(first, schema_version="sch-twin-b", entity_type="twin_b")
    monkeypatch.setitem(DEFAULT_SCHEMA_SPECS, first.entity_type, first)
    monkeypatch.setitem(DEFAULT_SCHEMA_SPECS, second.entity_type, second)

    def pair(entity_type):
        return mc.CandidatePair(
            pair_id="p1",
            left=0,
            right=1,
            name_score=0.9,
            address_score=0.0,
            left_profile={"names": ["a"], "addresses": []},
            right_profile={"names": ["a"], "addresses": []},
            entity_type=entity_type,
        )

    settings = Settings()
    # Freeze the question for both twins so the schema version is the ONLY
    # thing that can separate the two keys. If it were not hashed in, these
    # would collide and a contract change would reuse old verdicts.
    monkeypatch.setattr(mc, "system_prompt_for", lambda entity_type: "fixed question")
    key_a = mc._pair_key(settings, pair(first.entity_type))
    key_b = mc._pair_key(settings, pair(second.entity_type))
    assert key_a != key_b

    # same contract, same prompt, same model, same input -> same key.
    again = mc._pair_key(settings, pair(first.entity_type))
    assert again == key_a


def test_shipped_specs_are_registered_and_unregistered_fails_closed():
    assert set(DEFAULT_SCHEMA_SPECS) == {"user", "product", "publication"}
    with pytest.raises(ValueError, match="no schema registered"):
        get_schema_spec("vehicle")
    assert get_schema_spec(None) is USER_SPEC
    assert PUBLICATION_SPEC.entity_noun == "paper"
    # the compiler is deterministic: the same spec always asks the same question
    assert compile_judge_prompt(PRODUCT_SPEC) == compile_judge_prompt(PRODUCT_SPEC)
    assert mc.system_prompt_for("product") == mc.PRODUCT_PROMPT
    assert mc.system_prompt_for("publication") == mc.PUBLICATION_PROMPT

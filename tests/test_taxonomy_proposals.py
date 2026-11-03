"""Evidence-verified taxonomy extension: a model may propose, the data decides."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
import taxonomy_proposals as tp
from schemas import Taxonomy
from taxonomies import default_version, load_taxonomies

BASE = Taxonomy.model_validate(
    {
        "taxonomy_version": "tax-product-2026-10-27-v1",
        "entity_type": "product",
        "terms": [
            {"term": "Smartphone", "synonyms": ["mobile phone"]},
            {"term": "Laptop", "synonyms": []},
        ],
    }
)
EXISTING = ["Smartphone", "Laptop"]


def obs(**counts):
    return {
        k.replace("_", " ") if k.startswith("x") else k: {"count": c, "sources": {"catalog"}}
        for k, c in counts.items()
    }


def observed():
    return {
        "clothing": {"count": 45, "sources": {"catalog"}},
        "CLOTHING": {"count": 5, "sources": {"catalog"}},
        "clo": {"count": 4, "sources": {"catalog"}},
        "clothing-": {"count": 3, "sources": {"catalog"}},
        "3l3ctronics": {"count": 6, "sources": {"catalog"}},
        "electronics": {"count": 34, "sources": {"catalog"}},
        "ele": {"count": 2, "sources": {"catalog"}},
        "hom": {"count": 4, "sources": {"catalog"}},
        "home": {"count": 34, "sources": {"catalog"}},
        "hom3": {"count": 5, "sources": {"catalog"}},
    }


# --- relation rules ---------------------------------------------------------


@pytest.mark.parametrize(
    "synonym, term, rule",
    [
        ("CLOTHING", "clothing", "same_word"),
        ("clothing_", "clothing", "same_word"),
        ("3l3ctronics", "electronics", "same_word"),
        ("hom3", "home", "same_word"),
        ("clo", "clothing", "unique_prefix"),
        ("kit", "kitchen", "unique_prefix"),
        ("automotiv", "automotive", "unique_prefix"),
        ("kitchn", "kitchen", "one_typo"),
    ],
)
def test_relation_accepts_verifiable_variants(synonym, term, rule):
    terms = ["clothing", "electronics", "home", "kitchen", "automotive", "beauty", "toys", "sports"]
    assert tp.relation(synonym, term, terms) == rule


@pytest.mark.parametrize(
    "synonym, term",
    [
        ("điện thoại gập", "Foldable Phone"),  # no lexical link: semantic only
        ("notebook", "Laptop"),
        ("ho", "home"),  # prefix shorter than 3
        ("toyz", "toys"),  # typo on a 4-letter word: too short to trust
        ("electronic", "kitchen"),
    ],
)
def test_relation_refuses_what_the_data_cannot_verify(synonym, term):
    assert (
        tp.relation(synonym, term, ["home", "Foldable Phone", "Laptop", "kitchen", "toys"]) is None
    )


def test_model_numbers_are_not_folded_together():
    terms = ["iPhone 13", "iPhone 14", "Pixel 7", "Pixel 8", "Vitamin B12", "Vitamin B6"]
    assert tp.skeleton("iPhone 13") != tp.skeleton("iPhone 14")
    assert tp.skeleton("Vitamin B12") != tp.skeleton("Vitamin B6")
    for a, b in [("iPhone 13", "iPhone 14"), ("Pixel 7", "Pixel 8"), ("Vitamin B12", "Vitamin B6")]:
        assert tp.relation(a, b, terms) is None
        assert tp.relation(b, a, terms) is None
    # genuine variants of a model name still relate
    assert tp.relation("IPHONE 13", "iPhone 13", terms) == "same_word"
    assert tp.relation("iphone-13", "iPhone 13", terms) == "same_word"
    assert tp.relation("Vitamin B12", "vitamin b12", terms) == "same_word"


def test_ambiguous_prefix_is_refused_not_guessed():
    # "bea" is a prefix of both terms: it cannot be attributed to either
    assert tp.relation("bea", "beach", ["beach", "beauty"]) is None
    assert tp.relation("bea", "beauty", ["beach", "beauty"]) is None


def test_edit_distance_le1():
    assert tp.edit_distance_le1("kitchen", "kitchn")
    assert tp.edit_distance_le1("kitchen", "kitchan")
    assert not tp.edit_distance_le1("kitchen", "kitchan2x")
    assert not tp.edit_distance_le1("toys", "tops1")


# --- verification -----------------------------------------------------------


def test_verifier_accepts_only_what_the_data_supports():
    props = [
        tp.Proposal("clothing", ["clo", "CLOTHING", "clothing-"], "", "muse"),
        tp.Proposal("electronics", ["3l3ctronics", "ele"], "", "muse"),
        tp.Proposal("home", ["hom", "hom3"], "", "muse"),
    ]
    verdicts = tp.verify(props, observed(), EXISTING, 3)
    assert all(v.term_ok for v in verdicts)
    assert {s for v in verdicts for s, _ in v.accepted} == {
        "clo",
        "CLOTHING",
        "clothing-",
        "3l3ctronics",
        "ele",
        "hom",
        "hom3",
    }


def test_invented_term_is_rejected_even_if_plausible():
    (v,) = tp.verify(
        [tp.Proposal("Garments", ["clothing", "clo"], "", "muse")], observed(), EXISTING, 3
    )
    assert not v.term_ok and "invented" in v.term_reason
    assert v.accepted == []


def test_a_variant_cannot_be_proposed_as_a_new_category():
    v, _ = tp.verify(
        [tp.Proposal("clo", [], "", "muse"), tp.Proposal("clothing", ["clothing-"], "", "muse")],
        observed(),
        EXISTING,
        3,
    )
    assert not v.term_ok and "variant" in v.term_reason


def test_synonym_not_in_the_data_is_rejected():
    (v,) = tp.verify([tp.Proposal("clothing", ["frocks"], "", "muse")], observed(), EXISTING, 3)
    assert v.accepted == [] and v.rejected == [("frocks", "not a value in the data")]


def test_synonym_claimed_by_two_terms_is_rejected_for_both():
    props = [tp.Proposal("clothing", ["clo"], "", "muse"), tp.Proposal("home", ["clo"], "", "muse")]
    verdicts = tp.verify(props, observed(), EXISTING, 3)
    assert all("clo" not in [s for s, _ in v.accepted] for v in verdicts)


def test_term_below_minimum_support_is_rejected():
    o = {"gizmo": {"count": 2, "sources": set()}, "gizmo-": {"count": 1, "sources": set()}}
    (v,) = tp.verify([tp.Proposal("gizmo", ["gizmo-"], "", "muse")], o, EXISTING, 3)
    assert not v.term_ok


def test_semantic_synonym_without_a_lexical_link_is_not_accepted():
    o = {
        "Laptop": {"count": 9, "sources": set()},
        "notebook computer": {"count": 5, "sources": set()},
    }
    (v,) = tp.verify([tp.Proposal("Laptop", ["notebook computer"], "", "muse")], o, EXISTING, 3)
    assert v.accepted == [] and v.rejected[0][1] == "no verifiable relation to the term"


def test_rule_proposer_groups_variants_without_creating_fake_terms():
    props = tp.rule_proposer(observed(), EXISTING, 3)
    terms = {p.term for p in props}
    assert terms == {"clothing", "electronics", "home"}  # clo/hom are variants, not categories
    verdicts = tp.verify(props, observed(), EXISTING, 3)
    assert all(not v.rejected for v in verdicts)


# --- writing a version --------------------------------------------------------


def test_write_version_is_new_idempotent_and_loadable(tmp_path):
    shutil.copy(
        Path(__file__).resolve().parents[1]
        / "data/mock/taxonomy/taxonomy-product-2026-10-27-v1.json",
        tmp_path,
    )
    verdicts = tp.verify(tp.rule_proposer(observed(), EXISTING, 3), observed(), EXISTING, 3)
    first = tp.write_version(BASE, verdicts, tmp_path)
    again = tp.write_version(BASE, verdicts, tmp_path)
    assert first == again and first.startswith("tax-product-evidence-")
    loaded = load_taxonomies(tmp_path)
    assert default_version(loaded, "product") == first, "newest version becomes the default"
    assert loaded[first].parent_version == BASE.taxonomy_version
    mapped = {t.term: set(t.synonyms) for t in loaded[first].terms}
    assert {"clo", "CLOTHING", "clothing-"} <= mapped["clothing"]
    assert "Smartphone" in mapped  # base terms are kept
    log = (tmp_path / f"{first}.evidence.jsonl").read_text().splitlines()
    assert json.loads(log[0])["event"] == "new_term"
    assert BASE.taxonomy_version in loaded  # the base file is untouched


def test_nothing_accepted_writes_nothing(tmp_path):
    verdicts = tp.verify([tp.Proposal("Garments", ["clo"], "", "muse")], observed(), EXISTING, 3)
    assert tp.write_version(BASE, verdicts, tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_model_outage_changes_nothing(tmp_path, monkeypatch):
    from config import Settings

    async def boom(settings, observed_, existing):
        raise RuntimeError("down")

    monkeypatch.setattr(tp, "muse_proposer", boom)
    docs = [
        {"entity_type": "product", "source": "catalog", "fields": {"category": {"value": "clo"}}}
    ] * 4
    report = asyncio.run(tp.extend_taxonomy(Settings(), BASE, docs, tmp_path, mode="muse"))
    assert report["version"] is None and "unavailable" in report["skipped_reason"]
    assert list(tmp_path.iterdir()) == []


def test_muse_schema_is_strict_mode_compatible():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["properties"]) == set(node["required"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(tp.MUSE_SCHEMA)


def test_config_validates_mode():
    from config import ConfigurationError, Settings

    Settings(taxonomy_proposals="rules").validate_startup()
    with pytest.raises(ConfigurationError):
        Settings(taxonomy_proposals="auto").validate_startup()


# --- end to end through /build with the live engine ---------------------------


def product(doc_id, value):
    return {
        "document_id": doc_id,
        "entity_type": "product",
        "source": "catalog",
        "synthetic": True,
        "fields": {
            "category": {
                "value": value,
                "authority_rank": 1,
                "verified": True,
                "event_time": "2026-10-27T09:00:00+07:00",
                "ttl_seconds": 3600,
            }
        },
    }


def run_build(binary, mkit, tmp_path, monkeypatch, mode):
    from config import Settings
    from conftest import TrustEngine
    from fastapi.testclient import TestClient
    from main import create_app

    tax_dir = tmp_path / f"taxonomy-{mode}"
    tax_dir.mkdir()
    for path in (Path(__file__).resolve().parents[1] / "data/mock/taxonomy").glob("*.json"):
        shutil.copy(path, tax_dir / path.name)
    monkeypatch.setenv("TAXONOMY_DIR", str(tax_dir))
    values = (
        ["clothing"] * 4 + ["CLOTHING", "clo", "clothing-", "toys"] * 1 + ["toys"] * 3 + ["toy"]
    )
    docs = [product(f"p{i}", v) for i, v in enumerate(values)]
    engine = TrustEngine(binary, mkit).start()
    try:
        settings = Settings(
            use_mock_muse=True,
            trust_engine_url=engine.url,
            database_path=str(tmp_path / f"{mode}.db"),
            enable_background_tasks=False,
            splink_enabled=False,
            taxonomy_proposals=mode,
        )
        with TestClient(create_app(settings)) as client:
            out = client.post(
                "/build",
                json={"work_id": mode, "idempotency_key": mode, "dataset": {"documents": docs}},
            ).json()
    finally:
        engine.stop()
    return out, tax_dir


def test_off_holds_every_unknown_category(trust_engine_binary, mkit_bin, tmp_path, monkeypatch):
    out, _ = run_build(trust_engine_binary, mkit_bin, tmp_path, monkeypatch, "off")
    certified = [g for g in out["goldens"] if g.get("golden_id")]
    assert not certified, "no category is in the taxonomy, so T1 must hold them all"
    assert out["taxonomy_extension"] is None


def test_rules_mode_certifies_categories_the_data_verifies(
    trust_engine_binary, mkit_bin, tmp_path, monkeypatch
):
    out, tax_dir = run_build(trust_engine_binary, mkit_bin, tmp_path, monkeypatch, "rules")
    ext = out["taxonomy_extension"]
    assert ext["version"] and ext["accepted"] >= 4 and ext["rejected"] == 0
    values = sorted(
        g["attributes"]["category"]["value"] for g in out["goldens"] if g.get("golden_id")
    )
    # 12 documents, 6 distinct contents (the build merges identical ones): every
    # variant was folded into its term and certified
    assert values == ["clothing"] * 4 + ["toys"] * 2, values
    assert (tax_dir / f"{ext['version']}.json").exists()
    # the impact report names every term, its evidence and who proposed it
    impact = out["muse_impact"]["taxonomy"]
    assert impact["status"] == "rules" and impact["model"] is None  # no model did this
    assert impact["accepted"] == ext["accepted"] and impact["version"] == ext["version"]
    clothing = next(t for t in impact["terms"] if t["term"] == "clothing")
    assert clothing["term_ok"] and clothing["accepted"] and clothing["accepted"][0]["rule"]


# --- lineage, atomic writes, support ---------------------------------------


def _two_versions(tmp_path):
    shutil.copy(
        Path(__file__).resolve().parents[1]
        / "data/mock/taxonomy/taxonomy-product-2026-10-27-v1.json",
        tmp_path,
    )
    verdicts = tp.verify(tp.rule_proposer(observed(), EXISTING, 3), observed(), EXISTING, 3)
    return tp.write_version(BASE, verdicts, tmp_path)


def test_later_steward_version_outranks_an_evidence_version(tmp_path):
    evidence = _two_versions(tmp_path)
    steward = json.loads((tmp_path / f"{evidence}.json").read_text())
    steward.update(
        taxonomy_version="tax-product-2026-12-01-v2",
        parent_version=evidence,
        created_at="2999-01-01T00:00:00+00:00",
    )
    (tmp_path / "steward.json").write_text(json.dumps(steward))
    # by name "…-2026-…" < "…-evidence-…", yet the newer steward version must win
    assert default_version(load_taxonomies(tmp_path), "product") == "tax-product-2026-12-01-v2"


def test_write_leaves_no_temp_files_and_log_exists_with_version(tmp_path):
    evidence = _two_versions(tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert not [n for n in names if n.endswith(".tmp")]
    assert f"{evidence}.json" in names and f"{evidence}.evidence.jsonl" in names


def test_identical_copies_of_one_product_do_not_manufacture_support():
    base = Taxonomy.model_validate({**BASE.model_dump(), "terms": [{"term": "home"}]})
    docs = [
        {
            "document_id": f"d{i}",
            "entity_type": base.entity_type,
            "source": "s",
            "product_ref": "sku-1",
            "fields": {"category": {"value": "Bogus", "event_time": "t"}},
        }
        for i in range(5)
    ]
    assert tp.observe(docs, base, base.entity_type, "category")["Bogus"]["count"] == 1
    for d in docs:
        d.pop("product_ref")  # no ref: each document is its own entity
    assert tp.observe(docs, base, base.entity_type, "category")["Bogus"]["count"] == 5

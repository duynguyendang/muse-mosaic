"""The onboarding compiler: raw sources + expected result -> derived bundle.

Pins the zero-touch contract: deterministic alias hits map without any
model, the model can only *propose*, and any doubt refuses the build
loudly (nothing is half-derived and nothing is guessed).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import onboarding
import pytest
from config import Settings
from policies import load_policies
from schemas import Policy
from taxonomies import load_taxonomies


def _settings() -> Settings:
    return Settings(
        use_mock_muse=True,
        meta_model_api_key="",
        muse_model_id="",
        embedding_model="",
        embedding_version="",
        embedding_dim=None,
        trust_engine_url="http://127.0.0.1:1/derive",
        manglekit_bin="",
        database_path="/tmp/onboarding-test.db",
        enable_background_tasks=False,
    )


def _doc(doc_id: str, source: str, entity_type: str = "product", **fields) -> dict:
    return {
        "document_id": doc_id,
        "entity_type": entity_type,
        "source": source,
        "synthetic": True,
        "fields": {
            name: {
                "value": value,
                "authority_rank": 2,
                "event_time": "2026-10-27T09:00:00+07:00",
                "verified": True,
                "ttl_seconds": 3600,
            }
            for name, value in fields.items()
        },
    }


_BRANDS = ["Intuit", "Adobe", "Intuit", "Microsoft", "Adobe", "Intuit"] * 2
_TYPES = ["accounting", "design", "accounting", "design", "accounting", "accounting"] * 2


def _product_docs():
    docs = []
    for i, (brand, product_type) in enumerate(zip(_BRANDS, _TYPES, strict=True)):
        docs.append(
            _doc(
                f"d{i}",
                "pim" if i % 2 == 0 else "marketplace",
                prod_title=f"QuickBooks 2007 num{i}",
                manufacturer=brand,
                msrp=str(100 + i),
                product_type=product_type,
            )
        )
    return docs


_EXPECTED = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "manufacturer": {"type": "string"},
        "price": {"type": "string"},
        "product_type": {"type": "string"},
    },
}


def test_profile_documents_counts_and_samples() -> None:
    profiles = onboarding.profile_documents(_product_docs())
    assert profiles["manufacturer"].non_null == 12
    assert profiles["manufacturer"].distinct == 3
    assert profiles["msrp"].type_kind == "number"


def _mapping(docs, entity_type="product"):
    return asyncio.run(
        onboarding.derive_mapping(docs, entity_type=entity_type, settings=_settings())
    )


def test_derive_mapping_hits_aliases_without_a_model() -> None:
    docs = _product_docs()

    result = _mapping(docs)

    assert result.mapping == {
        "prod_title": "name",
        "manufacturer": "manufacturer",  # the raw name IS the canonical name
        "msrp": "price",
        "product_type": "category",
    }
    assert result.withheld == []
    # every hit came from the contract itself; the model never proposed anything
    assert [p for p in result.proposals if p.source == "model"] == []


def test_unknown_entity_type_fails_closed() -> None:
    docs = _product_docs()
    with pytest.raises(ValueError):
        _mapping(docs, entity_type="hovercraft")


def test_no_identity_column_refuses_loudly() -> None:
    docs = [
        _doc("d1", "pim", zzz_col_a="alpha", zzz_col_b="beta"),
        _doc("d2", "pim", zzz_col_a="gamma", zzz_col_b="delta"),
    ]
    with pytest.raises(onboarding.OnboardRefused) as excinfo:
        _mapping(docs)
    assert excinfo.value.code == "unmapped_identity_field"


def test_two_columns_claiming_the_primary_refuse() -> None:
    docs = [
        _doc("d1", "pim", title="QuickBooks 2007", product_name="QuickBooks 2007"),
    ]
    with pytest.raises(onboarding.OnboardRefused) as excinfo:
        _mapping(docs)
    assert excinfo.value.code == "ambiguous_primary_label"


def test_mixed_entity_types_refuse() -> None:
    docs = _product_docs() + [
        _doc("u1", "crm", entity_type="user", full_name="Maria Day", address_line="1 Main St")
    ]
    with pytest.raises(onboarding.OnboardRefused) as excinfo:
        asyncio.run(onboarding.derive_mapping(docs, entity_type=None, settings=_settings()))
    assert excinfo.value.code == "ambiguous_entity_type"


def test_low_confidence_identity_field_refuses() -> None:
    proposal = onboarding.FieldProposal("weirdcol", "name", "primary_label", 0.3, "unsure", "model")
    profiles = {
        "weirdcol": onboarding.FieldProfile(
            "weirdcol",
            non_null=3,
            distinct=3,
            distinct_ratio=1.0,
            type_kind="string",
            samples=("a", "b", "c"),
        )
    }
    spec = onboarding.get_schema_spec("product")
    with pytest.raises(onboarding.OnboardRefused) as excinfo:
        onboarding._decision_layer(profiles, spec, {"weirdcol": proposal}, None, 0.7)
    assert excinfo.value.code == "low_confidence_identity_field"


def test_taxonomy_bootstrap_builds_terms_and_aliases() -> None:
    docs = _product_docs()
    mapping = {
        d: c
        for d, c in (
            ("prod_title", "name"),
            ("manufacturer", "manufacturer"),
            ("msrp", "price"),
            ("product_type", "category"),
        )
    }
    bundle = onboarding.derive_taxonomy_bootstrap(
        docs,
        entity_type="product",
        mapping=mapping,
        expected_structure=_EXPECTED,
    )
    file = bundle["file"]
    assert file["entity_type"] == "product"
    assert set(file["attribute_aliases"]) == {"name", "manufacturer", "price", "category"}
    assert "manufacturer" in bundle["categorical_attributes"]
    assert (
        "price" not in bundle["categorical_attributes"]
    )  # numeric fields never become taxonomy terms
    # the artifact is a valid Taxonomy
    from schemas import Taxonomy

    Taxonomy.model_validate(file)


def test_taxonomy_bootstrap_excludes_numeric_low_cardinality() -> None:
    docs = [
        _doc(f"d{i}", "pim", release_year=str(2000 + i % 4), product_type="accounting")
        for i in range(20)
    ]
    mapping = {"release_year": "year", "product_type": "category"}
    expected = {
        "type": "object",
        "properties": {
            "year": {"type": "string"},
            "category": {"type": "string", "enum": ["accounting", "design"]},
        },
    }
    bundle = onboarding.derive_taxonomy_bootstrap(
        docs,
        entity_type="product",
        mapping=mapping,
        expected_structure=expected,
    )
    assert "year" not in bundle["categorical_attributes"]
    assert "category" in bundle["categorical_attributes"]


def test_policy_matrix_uses_observed_mode() -> None:
    docs = _product_docs()  # helper pins authority_rank=2 everywhere
    # all docs carry authority_rank=2 from the helper -> matrix ranks 2
    derived = onboarding.derive_policy(
        docs,
        entity_type="product",
        mapping={"prod_title": "name", "manufacturer": "manufacturer"},
        expected_structure=_EXPECTED,
        categorical_attributes=["manufacturer"],
        policy_version="pol-test-v1",
    )
    matrix = derived["policy_json"]["authority_matrix"]
    assert matrix["name"]["pim"] == 2
    assert matrix["name"]["marketplace"] == 2
    assert 'auth_rank("name", "pim", 2).' in derived["mg"]
    assert 'categorical("manufacturer").' in derived["mg"]
    # json policy validates against the Policy model
    Policy.model_validate(derived["policy_json"])


def test_policy_mode_tie_breaks_to_lower_rank() -> None:
    docs = [
        _doc("d1", "pim", prod_title="A"),
        _doc("d2", "pim", prod_title="B"),
    ]
    # make one doc claim rank 1 and one rank 2 through the helper override
    docs[0]["fields"]["prod_title"]["authority_rank"] = 1
    docs[1]["fields"]["prod_title"]["authority_rank"] = 2
    derived = onboarding.derive_policy(
        docs,
        entity_type="product",
        mapping={"prod_title": "name"},
        expected_structure=_EXPECTED,
        categorical_attributes=[],
        policy_version="pol-test-v1",
    )
    assert derived["policy_json"]["authority_matrix"]["name"]["pim"] == 1


@pytest.fixture()
def ok_engine(monkeypatch):
    monkeypatch.setattr(
        "policy_copilot.verify_with_engine",
        lambda horn: {"engine_available": True, "compiles": True, "error": None},
    )


def test_assemble_bundle_round_trip(tmp_path: Path, ok_engine) -> None:
    docs = _product_docs()

    manifest = asyncio.run(
        onboarding.assemble_bundle(
            docs,
            entity_type="product",
            expected_structure=_EXPECTED,
            out_root=tmp_path,
            settings=_settings(),
        )
    )

    bundle = Path(manifest["bundle_dir"])
    assert (bundle / "manifest.json").is_file()
    assert (bundle / "expected_structure.json").is_file()
    # derived artifacts load through the real loaders (they validate schemas)
    policies = load_policies(bundle / "policies")
    taxonomies = load_taxonomies(bundle / "taxonomy")
    assert "product" in policies
    assert policies["product"].policy_version == manifest["policy"]["policy_version"]
    assert list(taxonomies.values())[0].entity_type == "product"


def test_assemble_refuses_and_writes_nothing_on_doubt(tmp_path: Path, ok_engine) -> None:
    docs = [
        _doc("d1", "pim", zzz_col_a="alpha", zzz_col_b="beta"),
    ]
    with pytest.raises(onboarding.OnboardRefused):
        asyncio.run(
            onboarding.assemble_bundle(
                docs,
                entity_type="product",
                expected_structure=_EXPECTED,
                out_root=tmp_path,
                settings=_settings(),
            )
        )
    assert list(tmp_path.iterdir()) == []


def test_assemble_is_deterministic_on_replay(tmp_path: Path, ok_engine) -> None:
    docs = _product_docs()

    first = asyncio.run(
        onboarding.assemble_bundle(
            docs,
            entity_type="product",
            expected_structure=_EXPECTED,
            out_root=tmp_path,
            settings=_settings(),
        )
    )
    second = asyncio.run(
        onboarding.assemble_bundle(
            docs,
            entity_type="product",
            expected_structure=_EXPECTED,
            out_root=tmp_path,
            settings=_settings(),
        )
    )

    assert first["policy"]["policy_version"] == second["policy"]["policy_version"]
    assert second.get("reused") is True
    assert len(list(tmp_path.iterdir())) == 1


def test_assemble_refuses_document_without_source(tmp_path: Path, ok_engine) -> None:
    doc = _doc("d1", "pim", prod_title="QuickBooks 2007")
    doc["source"] = ""
    with pytest.raises(onboarding.OnboardRefused) as excinfo:
        asyncio.run(
            onboarding.assemble_bundle(
                [doc],
                entity_type="product",
                expected_structure=_EXPECTED,
                out_root=tmp_path,
                settings=_settings(),
            )
        )
    assert excinfo.value.code == "missing_source"
    assert list(tmp_path.iterdir()) == []


def test_assemble_fails_closed_when_rule_core_is_missing(
    tmp_path: Path, monkeypatch, ok_engine
) -> None:
    def boom() -> Path:
        raise onboarding.OnboardRefused("missing_rule_core", "nope", details={"tried": ["x"]})

    monkeypatch.setattr(onboarding, "_rule_core_path", boom)
    docs = _product_docs()
    with pytest.raises(onboarding.OnboardRefused) as excinfo:
        asyncio.run(
            onboarding.assemble_bundle(
                docs,
                entity_type="product",
                expected_structure=_EXPECTED,
                out_root=tmp_path,
                settings=_settings(),
            )
        )
    assert excinfo.value.code == "missing_rule_core"
    assert list(tmp_path.iterdir()) == []

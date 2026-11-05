"""The entity schema contracts ship as data (data/schemas/*.json).

Pins the G1 exit property: a new entity type is a data drop validated by
``load_specs`` -- no Python edit -- and an invalid spec fails closed at load
time instead of half-registering.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import schema_contract as sc

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "data" / "schemas"

_VEHICLE = {
    "schema_version": "sch-vehicle-v1",
    "entity_type": "vehicle",
    "entity_noun": "vehicle",
    "link_proof_type": "fuzzy_name_address",
    "admission": {
        "min_label_score": 0.5,
        "min_label_with_discriminator": 0.6,
        "min_discriminator_score": 0.8,
        "per_cluster_cap": 4,
        "split_bucket_at": 8,
        "require_house_number_guard": False,
    },
    "fields": [
        {
            "canonical_name": "vin_union",
            "source_aliases": ["vehicle_label", "car_name"],
            "role": "primary_label",
            "normalizer": "text_fold",
        },
        {
            "canonical_name": "segment",
            "source_aliases": ["body_type"],
            "role": "anchor_group",
            "normalizer": "text_fold",
        },
        {
            "canonical_name": "plate",
            "source_aliases": ["plate_number"],
            "role": "private_identifier",
            "normalizer": "email_lower",
        },
    ],
}


def _shipped_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "schemas"
    shutil.copytree(SHIPPED, dest)
    return dest


def test_shipped_specs_load_with_declared_values() -> None:
    specs = sc.load_specs(SHIPPED)
    assert set(specs) == {"user", "product", "publication"}
    assert specs["user"].admission.min_label_score == 0.8
    assert specs["user"].admission.require_house_number_guard is True
    assert specs["product"].admission.per_cluster_cap == 5
    assert specs["product"].rename_map()["title"] == "name"
    assert specs["publication"].primary_label.canonical_name == "title"


def test_a_new_entity_type_is_a_data_drop_not_a_code_edit(tmp_path: Path) -> None:
    dest = _shipped_copy(tmp_path)
    (dest / "vehicle.json").write_text(json.dumps(_VEHICLE), encoding="utf-8")
    specs = sc.load_specs(dest)
    vehicle = specs["vehicle"]
    assert vehicle.admission.per_cluster_cap == 4
    prompt = sc.compile_judge_prompt(vehicle)
    assert "vehicle" in prompt
    visible = [f.canonical_name for f in vehicle.visible_to_judge()]
    assert "plate" not in visible  # private_identifier is structurally unreadable
    assert "plate" not in prompt


def test_default_loader_fails_closed_on_an_empty_directory(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(sc.SchemaContractError):
        sc.load_specs(empty)


def _write(tmp_path: Path, name: str, spec: dict) -> None:
    (tmp_path / name).write_text(json.dumps(spec), encoding="utf-8")


def _valid_spec(entity_type: str, version: str) -> dict:
    return {
        "schema_version": version,
        "entity_type": entity_type,
        "entity_noun": entity_type,
        "link_proof_type": "fuzzy_name_address",
        "admission": {
            "min_label_score": 0.5,
            "min_label_with_discriminator": 0.6,
            "min_discriminator_score": 0.8,
            "per_cluster_cap": 3,
            "split_bucket_at": 8,
            "require_house_number_guard": False,
        },
        "fields": [
            {
                "canonical_name": "label",
                "source_aliases": [f"{entity_type}_name"],
                "role": "primary_label",
                "normalizer": "text_fold",
            }
        ],
    }


def test_entity_type_must_match_filename(tmp_path: Path) -> None:
    spec = _valid_spec("car", "sch-car-v1")
    _write(tmp_path, "truck.json", spec)
    with pytest.raises(sc.SchemaContractError, match="filename stem"):
        sc.load_specs(tmp_path)


def test_exactly_one_primary_label(tmp_path: Path) -> None:
    spec = _valid_spec("car", "sch-car-v1")
    spec["fields"].append(
        {
            "canonical_name": "second",
            "source_aliases": ["alias_x"],
            "role": "primary_label",
            "normalizer": "text_fold",
        }
    )
    _write(tmp_path, "car.json", spec)
    with pytest.raises(sc.SchemaContractError, match="one primary_label"):
        sc.load_specs(tmp_path)


def test_unknown_role_and_normalizer_are_rejected(tmp_path: Path) -> None:
    for bad in ("role", "normalizer"):
        spec = _valid_spec("car", "sch-car-v1")
        spec["fields"][0][bad] = "not_a_thing"
        _write(tmp_path, "car.json", spec)
        with pytest.raises(sc.SchemaContractError):
            sc.load_specs(tmp_path)
        (tmp_path / "car.json").unlink()


def test_duplicate_alias_across_fields_is_rejected(tmp_path: Path) -> None:
    spec = _valid_spec("car", "sch-car-v1")
    spec["fields"].append(
        {
            "canonical_name": "trim",
            "source_aliases": ["car_name"],
            "role": "corroborating",
            "normalizer": "text_fold",
        }
    )
    _write(tmp_path, "car.json", spec)
    with pytest.raises(sc.SchemaContractError, match="claimed by both"):
        sc.load_specs(tmp_path)


def test_schema_version_must_be_unique_across_files(tmp_path: Path) -> None:
    _write(tmp_path, "car.json", _valid_spec("car", "sch-shared-v1"))
    _write(tmp_path, "bus.json", _valid_spec("bus", "sch-shared-v1"))
    with pytest.raises(sc.SchemaContractError, match="schema_version"):
        sc.load_specs(tmp_path)


def test_out_of_range_admission_is_rejected(tmp_path: Path) -> None:
    spec = _valid_spec("car", "sch-car-v1")
    spec["admission"]["min_label_score"] = 1.5
    _write(tmp_path, "car.json", spec)
    with pytest.raises(sc.SchemaContractError, match="min_label_score"):
        sc.load_specs(tmp_path)


def test_zero_cap_is_rejected(tmp_path: Path) -> None:
    spec = _valid_spec("car", "sch-car-v1")
    spec["admission"]["per_cluster_cap"] = 0
    _write(tmp_path, "car.json", spec)
    with pytest.raises(sc.SchemaContractError, match="per_cluster_cap"):
        sc.load_specs(tmp_path)


def test_specs_dir_env_override_is_honoured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dest = _shipped_copy(tmp_path)
    monkeypatch.setenv("SPECS_DIR", str(dest))
    assert sc.specs_dir() == dest
    assert set(sc.load_specs()) == {"user", "product", "publication"}

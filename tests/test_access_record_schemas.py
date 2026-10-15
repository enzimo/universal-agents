"""The saved approval records keep tracked, valid JSON Schemas.

``scripts/export_access_record_schemas.py`` renders them; these tests fail when
a model changes without re-exporting, and prove every fixture validates against
both its model and its schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from scripts.export_access_record_schemas import (
    DEFAULT_OUTPUT_DIR,
    FIXTURE_MODELS,
    render_all_files,
    render_fixture_files,
)


def test_tracked_access_record_files_match_the_models() -> None:
    rendered = render_all_files()

    tracked = {str(path.relative_to(DEFAULT_OUTPUT_DIR)) for path in DEFAULT_OUTPUT_DIR.rglob("*.json")}
    assert tracked == set(rendered)
    for name, expected in rendered.items():
        assert (DEFAULT_OUTPUT_DIR / name).read_text(encoding="utf-8") == expected


def test_manifest_lists_the_two_records_in_draft_2020_12() -> None:
    manifest = json.loads((DEFAULT_OUTPUT_DIR / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["contract"] == "universal-agents-access-records"
    assert manifest["json_schema_draft"] == "2020-12"
    assert {entry["model"] for entry in manifest["schemas"]} == {
        "GeneratedAgentProvisioningRecordV1",
        "ChangeApprovalRecordV1",
    }
    assert all(len(entry["sha256"]) == 64 for entry in manifest["schemas"])


@pytest.mark.parametrize("fixture_name", sorted(FIXTURE_MODELS))
def test_each_fixture_round_trips_its_model_and_validates_against_its_schema(fixture_name: str) -> None:
    payload = json.loads(render_fixture_files()[f"fixtures/{fixture_name}"])
    model = FIXTURE_MODELS[fixture_name]
    schema = json.loads((DEFAULT_OUTPUT_DIR / f"{model.__name__}.schema.json").read_text(encoding="utf-8"))

    assert model.model_validate(payload).model_dump(mode="json", exclude_none=True) == payload
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(payload)


def test_schema_refuses_an_unknown_approval_mode() -> None:
    payload = json.loads(render_fixture_files()["fixtures/change_approval.json"])
    payload["mode"] = "decision_id"
    schema = json.loads((DEFAULT_OUTPUT_DIR / "ChangeApprovalRecordV1.schema.json").read_text(encoding="utf-8"))

    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(schema).validate(payload)


def test_access_record_schemas_are_repository_owned_data() -> None:
    root = Path(__file__).resolve().parents[1]

    assert DEFAULT_OUTPUT_DIR.is_relative_to(root / "data" / "schemas")

"""The team transfer file keeps tracked, valid JSON Schemas.

``scripts/export_team_transfer_schemas.py`` renders them. These tests fail when
a transfer model changes without re-exporting, so every change to the format
between versions is deliberate (``team_transfer`` module docstring).
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from scripts.export_team_transfer_schemas import (
    DEFAULT_OUTPUT_DIR,
    render_all_files,
    stale_files,
    transfer_models,
    section_contract_digests,
)


def test_tracked_team_transfer_schemas_match_the_models() -> None:
    assert stale_files(DEFAULT_OUTPUT_DIR) == []


def test_payload_contract_changes_require_a_new_section_version() -> None:
    # The schema exporter deliberately does not regenerate this ledger.
    # Keep earlier fingerprints: bump the section version and add its converter
    # rather than replacing an existing version's contract.
    frozen = json.loads(
        (DEFAULT_OUTPUT_DIR.parent / "section-contracts.json").read_text()
    )
    for version, digest in section_contract_digests().items():
        assert frozen.get(version) == digest, (
            f"{version} changed: bump the section version, add a converter, and append the new fingerprint"
        )


def test_every_schema_is_valid_draft_2020_12_and_listed_in_the_manifest() -> None:
    rendered = render_all_files()
    manifest = json.loads(rendered["manifest.json"])

    assert manifest["contract"] == "universal-agents-team-transfer"
    assert [entry["model"] for entry in manifest["schemas"]] == [
        model.__name__ for model in transfer_models()
    ]
    for entry in manifest["schemas"]:
        Draft202012Validator.check_schema(json.loads(rendered[entry["filename"]]))

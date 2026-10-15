#!/usr/bin/env python3
"""Export/check the JSON Schemas of saved approval records.

Two records outlived the old permission system and are saved on disk:

- ``GeneratedAgentProvisioningRecordV1``: how a generated specialist was
  approved and created, with the default tools the person approved;
- ``ChangeApprovalRecordV1``: how a staged platform change was approved
  (``access_control.changes``), also embedded in the record above.

Each schema has a fixture that must validate against both the model and the
schema.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sys

from pydantic import BaseModel


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_agents.generated_agent_runtime import GeneratedAgentProvisioningRecordV1  # noqa: E402
from custom_messages.contracts.access_v2 import ChangeApprovalMode, ChangeApprovalRecordV1  # noqa: E402
from custom_messages.contracts.common_v1 import canonical_contract_json  # noqa: E402
from scripts.contract_schema_export import (  # noqa: E402
    canonical_json_text,
    render_contract_schema_files,
)


DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "schemas" / "access-records" / "v1"
SCHEMA_BASE_ID = "https://universal-agents.dev/schemas/access-records/v1"
MODELS: tuple[type[BaseModel], ...] = (
    GeneratedAgentProvisioningRecordV1,
    ChangeApprovalRecordV1,
)
# Fixture file name -> the model it holds. ChangeApprovalRecordV1 has no
# record_type field, so fixtures are named here rather than by their content.
FIXTURE_MODELS: dict[str, type[BaseModel]] = {
    "generated_agent_provisioning.json": GeneratedAgentProvisioningRecordV1,
    "change_approval.json": ChangeApprovalRecordV1,
}


def render_schema_files() -> dict[str, str]:
    return render_contract_schema_files(
        models=MODELS,
        schema_base_id=SCHEMA_BASE_ID,
        contract="universal-agents-access-records",
    )


def render_fixture_files() -> dict[str, str]:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    approval = ChangeApprovalRecordV1(
        mode=ChangeApprovalMode.AGREEMENT,
        approved_by="usr_fixture_owner",
        approved_at_utc=now + timedelta(seconds=1),
    )
    fixtures: dict[str, BaseModel] = {
        "generated_agent_provisioning.json": GeneratedAgentProvisioningRecordV1(
            provisioning_id="generated_agent_provisioning_fixture",
            change_id="generated_agent_0123456789abcdef0123",
            plan_digest="2" * 64,
            status="ready",
            agent_type="FixtureResearchAgent",
            team_id="team_fixture",
            owner_canonical_user_id="usr_fixture_owner",
            actor_principal_id="agent_fixture_lead",
            default_tools=("code:python", "fn:schedules"),
            catalog_sha256="4" * 64,
            approval=approval,
            created_at_utc=now,
            applied_at_utc=now + timedelta(seconds=1),
        ),
        "change_approval.json": approval,
    }
    assert set(fixtures) == set(FIXTURE_MODELS)
    rendered = {
        f"fixtures/{name}": canonical_contract_json(record) + "\n"
        for name, record in fixtures.items()
    }
    rendered["fixtures/manifest.json"] = canonical_json_text(
        {
            "schema_version": 1,
            "fixtures": [
                {
                    "filename": filename.removeprefix("fixtures/"),
                    "model": FIXTURE_MODELS[filename.removeprefix("fixtures/")].__name__,
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
                for filename, content in sorted(rendered.items())
            ],
        }
    )
    return rendered


def render_all_files() -> dict[str, str]:
    return {**render_schema_files(), **render_fixture_files()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    rendered = render_all_files()
    if args.check:
        mismatches = [
            name
            for name, expected in rendered.items()
            if not (output_dir / name).exists()
            or (output_dir / name).read_text(encoding="utf-8") != expected
        ]
        unexpected = sorted(
            str(path.relative_to(output_dir))
            for path in output_dir.rglob("*.json")
            if str(path.relative_to(output_dir)) not in rendered
        )
        if mismatches or unexpected:
            parser.error("access record schemas are stale: " + ", ".join(sorted([*mismatches, *unexpected])))
        return 0

    for name, content in rendered.items():
        target = output_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

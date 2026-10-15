#!/usr/bin/env python3
"""Export/check the JSON Schemas of the team transfer file (``team_transfer``).

The transfer file is the one contract between versions, so every model in it
has a tracked schema under ``data/schemas/team-transfer/v1``: the manifest, the
section document, and the record model of every registered section. A test
fails when a model changes without re-exporting, which makes a format change a
deliberate act: bump the section's ``schema_version`` and add the conversion
from the previous release's version (``team_transfer.sections.SECTION_UPGRADES``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.contract_schema_export import render_contract_schema_files  # noqa: E402
from team_transfer.format import SectionDocumentV1, TransferManifestV1  # noqa: E402
from team_transfer.registry import TRANSFER_SECTIONS  # noqa: E402
from team_transfer.review import ImportReviewV1  # noqa: E402


def section_payload_models() -> dict[str, tuple[type[BaseModel], ...]]:
    """Track persisted contracts inside generic records and their section ownership."""
    from assistant_gateway.config import GatewayInstanceConfig
    from custom_agents.agent_catalog import AgentCatalogDocument
    from custom_agents.generated_agent_runtime import GeneratedAgentProvisioningRecordV1
    from custom_agents.objective_work_config import ObjectiveWorkExecutionConfigLayer
    from custom_agents.pinned_memory_transfer import ExportedFactV1
    from custom_agents.team_composition import TeamCompositionConfig
    from custom_messages.contracts.access_v2 import (
        AccountLinkV1,
        AccountSharingV1,
        DataFolderRecordV1,
        GrantV1,
    )
    from custom_messages.contracts.installed_permissions_v1 import (
        InstalledPermissionDefinitionV1,
    )
    from custom_messages.contracts.person_settings_v1 import PersonSettingsV1
    from custom_messages.contracts.schedule_v1 import ScheduledMessage
    from custom_messages.contracts.schedule_worker_v1 import ScheduleWorkerBindingV1
    from custom_messages.records.objectives import (
        ObjectiveRecord,
        ObjectiveWorkInstalledProfile,
        ObjectiveWorkPolicy,
    )
    from mcp_control.hosting import MCPHostedServerDefinitionV1
    from mcp_control.operation_levels import MCPOperationLevelsV1
    from mcp_control.registry import MCPConnectionRecordV1, MCPStaticBaselineV1
    from secure_tool_broker.credential_catalog import CatalogCredential
    from secure_tool_broker.model_keys import StoredModelKey
    from storage.worker_pool_repository import WorkerPoolLimitV1

    return {
        "settings": (GatewayInstanceConfig,),
        "control": (
            AgentCatalogDocument,
            TeamCompositionConfig,
            GeneratedAgentProvisioningRecordV1,
            ObjectiveWorkExecutionConfigLayer,
        ),
        "access": (GrantV1, DataFolderRecordV1, AccountLinkV1, PersonSettingsV1, AccountSharingV1),
        "objectives": (
            ObjectiveRecord,
            ObjectiveWorkInstalledProfile,
            ObjectiveWorkPolicy,
        ),
        "schedules": (ScheduledMessage, ScheduleWorkerBindingV1, WorkerPoolLimitV1),
        "pinned_memory": (ExportedFactV1,),
        "broker_credentials": (CatalogCredential, StoredModelKey),
        "mcp": (
            MCPConnectionRecordV1,
            MCPStaticBaselineV1,
            MCPHostedServerDefinitionV1,
            MCPOperationLevelsV1,
        ),
        "procedures": (InstalledPermissionDefinitionV1,),
    }


DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "schemas" / "team-transfer" / "v1"
SCHEMA_BASE_ID = "https://universal-agents.dev/schemas/team-transfer/v1"


def transfer_models() -> tuple[type[BaseModel], ...]:
    section_models = tuple(
        dict.fromkeys(section.record_model for section in TRANSFER_SECTIONS)
    )
    payload_models = tuple(
        model for models in section_payload_models().values() for model in models
    )
    return tuple(
        dict.fromkeys(
            (
                TransferManifestV1,
                SectionDocumentV1,
                ImportReviewV1,
                *section_models,
                *payload_models,
            )
        )
    )


def render_all_files() -> dict[str, str]:
    rendered = render_contract_schema_files(
        models=transfer_models(),
        schema_base_id=SCHEMA_BASE_ID,
        contract="universal-agents-team-transfer",
    )
    manifest = json.loads(rendered["manifest.json"])
    payloads = section_payload_models()
    manifest["sections"] = [
        {
            "name": section.name,
            "schema_version": section.schema_version,
            "record_model": section.record_model.__name__,
            "payload_models": [
                model.__name__ for model in payloads.get(section.name, ())
            ],
        }
        for section in TRANSFER_SECTIONS
    ]
    rendered["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return rendered


def section_contract_digests() -> dict[str, str]:
    """Versioned fingerprints are frozen separately from regenerated schemas."""
    payloads = section_payload_models()
    result = {}
    for section in TRANSFER_SECTIONS:
        contracts = [section.record_model, *payloads.get(section.name, ())]
        value = {model.__name__: model.model_json_schema() for model in contracts}
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        result[f"{section.name}/{section.schema_version}"] = hashlib.sha256(
            data
        ).hexdigest()
    return result


def stale_files(output_dir: Path) -> list[str]:
    rendered = render_all_files()
    mismatches = [
        name
        for name, expected in rendered.items()
        if not (output_dir / name).exists()
        or (output_dir / name).read_text(encoding="utf-8") != expected
    ]
    unexpected = [
        str(path.relative_to(output_dir))
        for path in output_dir.rglob("*.json")
        if str(path.relative_to(output_dir)) not in rendered
    ]
    return sorted([*mismatches, *unexpected])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if args.check:
        stale = stale_files(output_dir)
        if stale:
            parser.error("team transfer schemas are stale: " + ", ".join(stale))
        return 0
    for path in output_dir.rglob("*.json"):
        path.unlink()
    for name, content in render_all_files().items():
        target = output_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

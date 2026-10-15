"""Export/check the transport and recovery contracts used by runtime adapters."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_messages.contracts.envelope_v1 import MessageEnvelope  # noqa: E402
from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3  # noqa: E402
from custom_messages.contracts.schedule_worker_v1 import (  # noqa: E402
    AgentLaunchV1, AgentCreationResultV1, ScheduleWorkerBindingV1,
    ScheduleReadinessV1, ScheduleOccurrenceV1, WorkerDefinitionRecordV1,
)
from scripts.contract_schema_export import render_contract_schema_files  # noqa: E402
from pydantic import BaseModel  # noqa: E402


def render_schema_files() -> dict[str, str]:
    files = {}
    # Each area is rendered in one call so its manifest lists every schema.
    contracts: list[tuple[str, int, tuple[type[BaseModel], ...]]] = [
        ("transport", 1, (MessageEnvelope,)),
        ("runtime-snapshots", 3, (RuntimeSnapshotV3,)),
        ("scheduled-workers", 1, (
            AgentLaunchV1, AgentCreationResultV1, ScheduleWorkerBindingV1,
            ScheduleReadinessV1, ScheduleOccurrenceV1,
            WorkerDefinitionRecordV1,
        )),
    ]
    for area, version, models in contracts:
        files.update({
            f"{area}/v{version}/{name}": content
            for name, content in render_contract_schema_files(
                models=models, schema_base_id=f"https://universal-agents.dev/schemas/{area}/v{version}",
                contract=f"universal-agents-{area}", schema_version=version,
            ).items()
        })
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for name, content in render_schema_files().items():
        path = ROOT / "data/schemas" / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if stale:
        parser.error("Stale runtime schemas: " + ", ".join(stale))


if __name__ == "__main__":
    main()

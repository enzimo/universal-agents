#!/usr/bin/env python3
"""Preview or archive incompatible active task snapshots with the runtime stopped."""

# Direct execution bootstraps the repository path before project imports.
# ruff: noqa: E402
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from assistant_gateway.runtime_config import load_runtime_config_file
from config_loader import resolve_agent_type_settings
from default_values import AGENT_DEFAULTS
from env_secrets import materialize_secret_file_envs
from storage.config import (
    StorageSettings,
    SqliteStorageSettings,
    resolve_storage_settings,
)
from storage.runtime_snapshot_maintenance import (
    inspect_runtime_snapshots,
    retire_runtime_snapshots,
)

LOGGER = logging.getLogger("retire_runtime_snapshots")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--config", help="Generic config.yaml or onboarded instance_config.yaml."
    )
    source.add_argument("--sqlite-path", help="Explicit SQLite manager database path.")
    parser.add_argument(
        "--env", help="Env file to load before resolving configured storage."
    )
    parser.add_argument("--agent-type", default=AGENT_DEFAULTS.named_agent_type.value)
    parser.add_argument("--team-id", required=True)
    parser.add_argument(
        "--owner-agent-id",
        action="append",
        default=[],
        help="Exact owner from --dry-run; repeat to select multiple owners.",
    )
    parser.add_argument(
        "--reason", help="Operator's reason for abandoning these active snapshots."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate storage headers and all runtime records; list incompatible owners without changing data.",
    )
    mode.add_argument(
        "--yes",
        action="store_true",
        help="Confirm affected runtimes are stopped and selected work may be abandoned.",
    )
    args = parser.parse_args()
    if args.yes and (not args.owner_agent_id or not args.reason):
        parser.error(
            "--yes requires --owner-agent-id and --reason. Inspect with --dry-run first."
        )
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.env:
        env_path = Path(args.env).expanduser().resolve()
        if not env_path.is_file():
            parser.error("The specified --env file does not exist.")
        load_dotenv(env_path, override=True)
    materialize_secret_file_envs()
    if args.sqlite_path:
        storage = StorageSettings(
            backend="sqlite", sqlite=SqliteStorageSettings(path=args.sqlite_path)
        )
    else:
        runtime = resolve_agent_type_settings(
            load_runtime_config_file(args.config), args.agent_type
        )
        storage = resolve_storage_settings(
            {
                "backend": runtime["storage_backend"],
                "sqlite": {"path": runtime["storage_sqlite_path"]},
                "postgres": {"dsn": runtime["storage_postgres_dsn"]},
            },
            base_dir=runtime["instance_root"],
        )
    if args.dry_run:
        rows = inspect_runtime_snapshots(storage, team_id=args.team_id)
        LOGGER.info("Unsupported runtime snapshot owners: %s", json.dumps(rows))
        LOGGER.info(
            "Preview only. Retirement archives selected snapshots; their work will not resume."
        )
    else:
        ids = retire_runtime_snapshots(
            storage,
            team_id=args.team_id,
            owner_agent_ids=args.owner_agent_id,
            reason=args.reason,
        )
        LOGGER.info(
            "Archived %d snapshot(s) in retired_task_runtime: %s",
            len(ids),
            ", ".join(ids),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

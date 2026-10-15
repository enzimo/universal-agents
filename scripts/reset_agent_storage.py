#!/usr/bin/env python3
"""Reset manager and gateway persistence stores for clean local testing."""

# Direct execution bootstraps the repository path before project imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    load_dotenv = None

from assistant_gateway.config import GatewayInstanceConfig
from config_loader import load_config, resolve_agent_type_settings
from storage.backends import (
    STORAGE_BACKEND_POSTGRES,
    STORAGE_BACKEND_SQLITE,
)
from default_values import AGENT_DEFAULTS, STORAGE_DEFAULTS
from storage.config import StorageSettings, resolve_storage_settings
from storage.postgres_connection import connect_postgres, redact_postgres_dsn


@dataclass(frozen=True)
class ResetTarget:
    label: str
    storage: StorageSettings


def _load_env_file(path: str | None) -> Optional[Path]:
    if not path:
        return None
    env_path = Path(path).expanduser().resolve()
    if not env_path.exists():
        raise FileNotFoundError(f"Env file not found: {env_path}")
    if load_dotenv is not None:
        load_dotenv(env_path, override=True)
        return env_path

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ[key] = value
    return env_path


def _sqlite_sidecars(path: Path) -> list[Path]:
    return [path, Path(f"{path}-wal"), Path(f"{path}-shm")]


def _reset_sqlite(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    for candidate in _sqlite_sidecars(path):
        if candidate.exists():
            candidate.unlink()


def _reset_postgres(dsn: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    with connect_postgres(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        conn.execute("GRANT ALL ON SCHEMA public TO PUBLIC")
        conn.commit()


def _storage_key(storage: StorageSettings) -> tuple[str, str]:
    if storage.backend == STORAGE_BACKEND_POSTGRES:
        return storage.backend, storage.postgres_dsn or ""
    return storage.backend, storage.sqlite_path or ""


def _storage_description(storage: StorageSettings) -> str:
    if storage.backend == STORAGE_BACKEND_POSTGRES:
        return redact_postgres_dsn(storage.postgres_dsn or "")
    return storage.sqlite_path or ""


def _resolve_generic_storage(config_path: str, agent_type: str) -> list[ResetTarget]:
    cfg = load_config(config_path)
    agent_runtime_settings = resolve_agent_type_settings(cfg, agent_type)
    instance_root = agent_runtime_settings.get("instance_root")
    sqlite_path = (
        agent_runtime_settings.get("storage_sqlite_path")
        or agent_runtime_settings.get("task_history_db_path")
    )
    storage = resolve_storage_settings(
        {
            "backend": agent_runtime_settings.get("storage_backend", STORAGE_DEFAULTS.backend.value),
            STORAGE_BACKEND_SQLITE: {"path": sqlite_path},
            STORAGE_BACKEND_POSTGRES: {"dsn": agent_runtime_settings.get("storage_postgres_dsn")},
        },
        base_dir=instance_root,
        sqlite_fallback_path=sqlite_path,
    )
    return [ResetTarget(label=f"{agent_type} manager storage", storage=storage)]


def _resolve_instance_targets(
    instance_config_path: str,
    *,
    include_manager: bool,
    include_gateway_state: bool,
) -> list[ResetTarget]:
    config = GatewayInstanceConfig.load_for_process(instance_config_path)
    if not os.getenv("RESET_STORAGE_ENV_PATH") and config.launch.env_file_path:
        loaded = _load_env_file(config.launch.env_file_path)
        if loaded is not None:
            os.environ["RESET_STORAGE_ENV_PATH"] = str(loaded)

    targets: list[ResetTarget] = []
    if include_manager:
        targets.append(
            ResetTarget(
                label="instance manager storage",
                storage=config.resolve_manager_storage_settings(env=os.environ),
            )
        )
    if include_gateway_state:
        targets.append(
            ResetTarget(
                label="instance gateway state storage",
                storage=config.resolve_state_storage_settings(env=os.environ),
            )
        )
    return targets


def _coalesce_targets(targets: Iterable[ResetTarget]) -> list[tuple[list[str], StorageSettings]]:
    grouped: dict[tuple[str, str], tuple[list[str], StorageSettings]] = {}
    for target in targets:
        key = _storage_key(target.storage)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = ([target.label], target.storage)
        else:
            existing[0].append(target.label)
    return list(grouped.values())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reset manager and gateway persistence stores for clean testing.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--config",
        help="Generic config.yaml path. Resets the resolved shared manager store for one agent type.",
    )
    group.add_argument(
        "--instance-config",
        help="Onboarded instance_config.yaml path. Resets instance manager storage and gateway state by default.",
    )
    parser.add_argument(
        "--env",
        help="Optional env file to load before resolving storage. In instance mode, the script also falls back to launch.env_file_path.",
    )
    parser.add_argument(
        "--agent-type",
        default=AGENT_DEFAULTS.named_agent_type.value,
        help="Agent type used to resolve storage from generic config.yaml (default: TeamArchitect).",
    )
    parser.add_argument(
        "--manager-only",
        action="store_true",
        help="In instance-config mode, reset only manager storage.",
    )
    parser.add_argument(
        "--gateway-only",
        action="store_true",
        help="In instance-config mode, reset only gateway state storage.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be reset without deleting or dropping anything.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the destructive reset. Required unless --dry-run is used.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.dry_run and not args.yes:
        parser.error("Pass --yes to perform the reset, or use --dry-run to inspect targets.")
    if args.config and (args.manager_only or args.gateway_only):
        parser.error("--manager-only and --gateway-only are only valid with --instance-config.")
    if args.manager_only and args.gateway_only:
        parser.error("Choose at most one of --manager-only or --gateway-only.")

    loaded_env = _load_env_file(args.env)
    if loaded_env is not None:
        os.environ["RESET_STORAGE_ENV_PATH"] = str(loaded_env)

    if args.config:
        targets = _resolve_generic_storage(args.config, args.agent_type)
    else:
        targets = _resolve_instance_targets(
            args.instance_config,
            include_manager=not args.gateway_only,
            include_gateway_state=not args.manager_only,
        )

    operations = _coalesce_targets(targets)
    action = "Would reset" if args.dry_run else "Resetting"
    for labels, storage in operations:
        print(f"{action} {', '.join(labels)}: {storage.backend} -> {_storage_description(storage)}")
        if storage.backend == STORAGE_BACKEND_POSTGRES:
            _reset_postgres(storage.postgres_dsn or "", dry_run=args.dry_run)
        else:
            _reset_sqlite(Path(storage.sqlite_path or "").expanduser().resolve(), dry_run=args.dry_run)

    print(f"Completed {len(operations)} reset operation(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Give the gateway rows a single-user instance recorded without an owner to one person.

Gateway startup does the same when multi-user mode is on; this runs it by hand
(``storage.multi_user_owner_migration``). The manager's store is never changed:
a conversation, archived task or schedule with no owner is work for no person.
"""

# Direct execution bootstraps the repository path before project imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import load_config
from storage.backends import STORAGE_BACKEND_SQLITE
from default_values import STORAGE_DEFAULTS
from storage.config import StorageSettings, resolve_storage_settings
from storage.multi_user_owner_migration import migrate_legacy_null_owner_rows


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Assign gateway rows with NULL owner_canonical_user_id to an explicit canonical user. "
            "The manager's conversations, archived tasks and schedules are never changed. "
            "Run only after confirming the old single-user data belongs to that admin."
        )
    )
    parser.add_argument(
        "--owner-canonical-user-id",
        required=True,
        help="Canonical user id that should own legacy single-user gateway rows, usually the bootstrap admin.",
    )
    parser.add_argument(
        "--config",
        default=str(PROJECT_ROOT / "config.yaml"),
        help="Generic config.yaml path used to resolve gateway storage. Defaults to the project config.yaml.",
    )
    parser.add_argument(
        "--env-file",
        default=str(PROJECT_ROOT / "debug.env"),
        help="Optional dotenv file loaded before config resolution. Defaults to debug.env when present.",
    )
    parser.add_argument(
        "--gateway-db",
        default=None,
        help=(
            "Optional gateway-state SQLite DB path. When omitted, gateway.storage from config is used if present, "
            "otherwise dbs/gateway_state.db relative to the project root."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional JSON report path. The report is always also printed to stdout.",
    )
    return parser.parse_args()


def _load_dotenv_file(path: str | None) -> None:
    if not path:
        return
    env_path = Path(path).expanduser()
    if env_path.exists():
        load_dotenv(env_path)


def _resolve_gateway_storage(config_path: str, gateway_db: str | None) -> StorageSettings:
    if gateway_db:
        raw_storage: dict[str, Any] = {
            "backend": STORAGE_BACKEND_SQLITE,
            STORAGE_BACKEND_SQLITE: {"path": gateway_db},
        }
        return resolve_storage_settings(
            raw_storage,
            base_dir=str(PROJECT_ROOT),
            sqlite_fallback_path=STORAGE_DEFAULTS.gateway_state_db_path.value,
        )

    config = load_config(config_path)
    raw_gateway_storage = config.get("gateway.storage")
    if isinstance(raw_gateway_storage, dict):
        return resolve_storage_settings(
            raw_gateway_storage,
            base_dir=str(PROJECT_ROOT),
            sqlite_fallback_path=STORAGE_DEFAULTS.gateway_state_db_path.value,
        )

    return resolve_storage_settings(
        {
            "backend": STORAGE_BACKEND_SQLITE,
            STORAGE_BACKEND_SQLITE: {"path": STORAGE_DEFAULTS.gateway_state_db_path.value},
        },
        base_dir=str(PROJECT_ROOT),
        sqlite_fallback_path=STORAGE_DEFAULTS.gateway_state_db_path.value,
    )


def _require_existing_sqlite_file(label: str, settings: StorageSettings) -> None:
    if settings.backend != STORAGE_BACKEND_SQLITE:
        return
    db_path = Path(str(settings.sqlite_path or "")).expanduser()
    if not db_path.exists():
        raise FileNotFoundError(f"{label} SQLite DB does not exist: {db_path}")


def main() -> int:
    args = _parse_args()
    _load_dotenv_file(args.env_file)

    gateway_settings = _resolve_gateway_storage(args.config, args.gateway_db)
    _require_existing_sqlite_file("gateway", gateway_settings)

    report = migrate_legacy_null_owner_rows(
        owner_canonical_user_id=args.owner_canonical_user_id,
        gateway_storage_settings=gateway_settings,
    ).to_dict()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).expanduser().write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

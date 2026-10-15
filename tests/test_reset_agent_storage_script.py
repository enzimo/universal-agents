from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from assistant_gateway.config import GatewayInstanceConfig


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "reset_agent_storage.py"


def _clean_storage_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "TASK_HISTORY_DB_PATH",
        "STORAGE_BACKEND",
        "SQLITE_DB_PATH",
        "POSTGRES_DSN",
        "POSTGRES_DSN_FILE",
    ):
        env.pop(key, None)
    return env


def test_reset_agent_storage_dry_run_resolves_generic_sqlite_path(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agent": {
                    "default_team_id": "team_test",
                    "workspace_dir": str(workspace_dir),
                },
                "storage": {
                    "backend": "sqlite",
                    "sqlite": {
                        "path": "runtime/test_manager.db",
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--config",
            str(config_path),
            "--dry-run",
        ],
        cwd=str(PROJECT_ROOT),
        check=True,
        capture_output=True,
        text=True,
        env=_clean_storage_env(),
    )

    expected_path = (workspace_dir / "runtime" / "test_manager.db").resolve()
    assert str(expected_path) in completed.stdout
    assert "Would reset TeamArchitect manager storage" in completed.stdout


def test_reset_agent_storage_instance_mode_removes_manager_and_gateway_sqlite_files(tmp_path: Path) -> None:
    instance_root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="reset_live_instance",
        instance_root=str(instance_root),
        team_id="team_test",
        timezone="America/Chicago",
        heartbeat_enabled=False,
        manager_storage_backend="sqlite",
        manager_storage_sqlite_path="data/task_history.db",
        state_storage_backend="sqlite",
        state_storage_sqlite_path="gateway_state.db",
    )
    config_path = config.save(instance_root / "instance_config.yaml")

    manager_db = Path(config.state.storage.sqlite.path or "")
    gateway_db = Path(config.gateway.storage.sqlite.path or "")
    manager_db.parent.mkdir(parents=True, exist_ok=True)
    gateway_db.parent.mkdir(parents=True, exist_ok=True)

    for target in (
        manager_db,
        Path(f"{manager_db}-wal"),
        Path(f"{manager_db}-shm"),
        gateway_db,
        Path(f"{gateway_db}-wal"),
        Path(f"{gateway_db}-shm"),
    ):
        target.write_text("seed", encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--instance-config",
            str(config_path),
            "--yes",
        ],
        cwd=str(PROJECT_ROOT),
        check=True,
        capture_output=True,
        text=True,
        env=_clean_storage_env(),
    )

    for target in (
        manager_db,
        Path(f"{manager_db}-wal"),
        Path(f"{manager_db}-shm"),
        gateway_db,
        Path(f"{gateway_db}-wal"),
        Path(f"{gateway_db}-shm"),
    ):
        assert not target.exists()

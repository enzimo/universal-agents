from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.heartbeat_store import HeartbeatStore
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_tools.providers.heartbeat import (
    add_heartbeat_item,
    disable_heartbeat_item,
    enable_heartbeat_item,
    list_heartbeat_items,
    remove_heartbeat_item,
    update_heartbeat_item,
)
from tests.task_work_support import set_agent_ref
from storage.config import resolve_storage_settings


class _FakeTaskManager:
    def __init__(self, db_path: str, meta: MessageMeta, *, storage_settings=None):
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self._db_path = db_path
        self.storage_settings = storage_settings or resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": db_path}},
            base_dir=str(Path(db_path).parent),
        )
        self.current_task_id = "task_current"
        self._meta = meta

    def get_task_info(self, task_id: str):
        if task_id != "task_current":
            return None
        return SimpleNamespace(meta=self._meta)


class _FakeAgent:
    def __init__(
        self,
        db_path: str,
        meta: MessageMeta,
        *,
        storage_settings=None,
        instance_config_path: str | None = None,
        workspace_dir: str | None = None,
    ):
        self.task_manager = _FakeTaskManager(
            db_path, meta, storage_settings=storage_settings
        )
        self.instance_config_path = instance_config_path
        self.workspace_dir = workspace_dir


def _write_instance_config(
    tmp_path: Path,
    *,
    instance_name: str = "nova",
    heartbeat_enabled: bool,
) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name=instance_name,
        instance_root=str(tmp_path / "instance"),
        team_id="test_team",
        timezone="America/Chicago",
        heartbeat_enabled=heartbeat_enabled,
    )
    config.state.storage.sqlite.path = str(tmp_path / "task_history.db")
    config.save(config.instance_root_path / "instance_config.yaml")
    return config


def test_heartbeat_item_tools_manage_canonical_items(tmp_path: Path):
    db_path = str(tmp_path / "task_history.db")
    store = HeartbeatStore(db_path=db_path)
    store.ensure_source(source_id="hb_source_test", title="Heartbeat Test")
    meta = MessageMeta(
        gateway_context=GatewayContext(
            heartbeat_source_id="hb_source_test",
            session_key="heartbeat",
        )
    )
    set_agent_ref(_FakeAgent(db_path, meta))

    created = json.loads(add_heartbeat_item("Check alerts", title="Alerts"))
    assert created["source_id"] == "hb_source_test"
    assert created["title"] == "Alerts"
    assert created["instruction"] == "Check alerts"
    assert created["conversation_id"].startswith("conversation_id_")
    heartbeat_item_id = created["heartbeat_item_id"]

    listed = json.loads(list_heartbeat_items())
    assert listed["returned"] == 1
    assert listed["items"][0]["heartbeat_item_id"] == heartbeat_item_id
    assert listed["items"][0]["version"] == created["version"]

    updated = json.loads(
        update_heartbeat_item(
            heartbeat_item_id, created["version"], instruction="Check alerts and anomalies"
        )
    )
    assert updated["instruction"] == "Check alerts and anomalies"
    assert updated["version"] != created["version"]

    disabled = json.loads(disable_heartbeat_item(heartbeat_item_id, updated["version"]))
    assert disabled["enabled"] is False
    # Turning it off is not something the version covers.
    assert disabled["version"] == updated["version"]

    enabled = json.loads(enable_heartbeat_item(heartbeat_item_id, updated["version"]))
    assert enabled["enabled"] is True

    removed = json.loads(remove_heartbeat_item(heartbeat_item_id, updated["version"]))
    assert removed["removed"] is True
    assert store.list_items("hb_source_test") == []


def test_heartbeat_item_tools_use_task_manager_storage_settings(
    monkeypatch, tmp_path: Path
):
    meta = MessageMeta(
        gateway_context=GatewayContext(
            heartbeat_source_id="hb_source_test",
            session_key="heartbeat",
        )
    )
    storage_settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    agent = _FakeAgent(
        str(tmp_path / "ignored.db"), meta, storage_settings=storage_settings
    )
    set_agent_ref(agent)

    captured: dict[str, object] = {}

    class _FakeHeartbeatStore:
        def __init__(self, db_path: str, storage_settings=None):
            captured["db_path"] = db_path
            captured["storage_settings"] = storage_settings

        def get_source(self, source_id: str):
            return SimpleNamespace(
                source_id=source_id,
                title="Heartbeat Test",
                enabled=True,
                updated_at="2026-01-01T00:00:00+00:00",
            )

        def list_items(self, source_id: str, enabled_only: bool = False):
            return []

    monkeypatch.setattr(
        "custom_tools.providers.heartbeat.HeartbeatStore",
        _FakeHeartbeatStore,
    )

    listed = json.loads(list_heartbeat_items())

    assert listed["source_id"] == "hb_source_test"
    assert captured["db_path"] == str(tmp_path / "ignored.db")
    assert captured["storage_settings"] == storage_settings


def test_list_heartbeat_items_reports_disabled_config_when_source_is_unseeded(
    tmp_path: Path,
):
    config = _write_instance_config(tmp_path, heartbeat_enabled=False)
    db_path = str(tmp_path / "task_history.db")
    meta = MessageMeta(
        gateway_context=GatewayContext(
            heartbeat_source_id=config.heartbeat.source_id,
            session_key="heartbeat",
        )
    )
    set_agent_ref(
        _FakeAgent(
            db_path,
            meta,
            instance_config_path=str(config.config_path),
            workspace_dir=str(config.workspace_path),
        )
    )

    listed = json.loads(list_heartbeat_items())

    assert "error" not in listed
    assert listed["status"] == "disabled"
    assert listed["source_exists"] is False
    assert listed["heartbeat_enabled"] is False
    assert listed["configured_source_id"] == "hb_source_nova"
    assert listed["returned"] == 0
    assert listed["items"] == []
    assert "will not fire" in " ".join(listed["notes"])

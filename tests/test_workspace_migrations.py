from __future__ import annotations

from pathlib import Path

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.manager_bootstrap_runtime import (
    LEGACY_USER_MD_SOURCE_KEY,
    ONBOARDING_USER_CONTEXT_SOURCE_KEY,
    migrate_legacy_heartbeat_markdown,
    migrate_legacy_user_markdown,
    report_obsolete_tools_markdown,
    seed_onboarding_user_context,
)
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.pinned_memory_runtime import (
    PinnedMemoryCommand,
    PinnedMemoryRequestContext,
    PinnedMemoryService,
)
from storage.pinned_memory_repository import USERMEM_BANK, create_pinned_memory_repository


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="workspace_migration",
        instance_root=str(tmp_path / "instance"),
        team_id="team_workspace_migration",
        timezone="America/Chicago",
        display_name="Owner",
    )
    config.state.storage.sqlite.path = str(tmp_path / "manager.db")
    config.gateway.storage.sqlite.path = str(tmp_path / "gateway.db")
    config.workspace_path.mkdir(parents=True, exist_ok=True)
    return config


def _memory_service(config: GatewayInstanceConfig) -> PinnedMemoryService:
    repository = create_pinned_memory_repository(config.manager_storage_settings)
    return PinnedMemoryService(repository, config=dict(config.agent.memory or {}))


def test_onboarding_user_context_is_owner_scoped_and_idempotently_updated(tmp_path: Path) -> None:
    config = _config(tmp_path)

    service = _memory_service(config)
    context = {
        "service": service,
        "team_id": config.gateway.team_id,
        "canonical_user_id": config.user.canonical_user_id,
        "request_timezone": config.user.timezone,
        "instance_name": config.instance_name,
    }
    first = seed_onboarding_user_context(
        **context,
        user_context="Prefers concise, high-signal updates.",
    )
    second = seed_onboarding_user_context(
        **context,
        user_context="A retry must not duplicate this fact.",
    )
    rows = create_pinned_memory_repository(config.manager_storage_settings).list_usermem(
        canonical_user_id=config.user.canonical_user_id,
    )

    assert first is not None
    assert second is not None
    assert first["memory_id"] == second["memory_id"]
    assert len(rows) == 1
    assert rows[0].fact_text == "A retry must not duplicate this fact."
    assert rows[0].source_key == ONBOARDING_USER_CONTEXT_SOURCE_KEY
    assert rows[0].metadata["source_kind"] == "onboarding"


def test_legacy_user_markdown_is_preserved_when_source_key_content_conflicts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    service = _memory_service(config)
    service.execute(
        PinnedMemoryCommand(
            bank=USERMEM_BANK,
            action="create",
            fact_text="Previously imported content.",
            source_key=LEGACY_USER_MD_SOURCE_KEY,
            metadata={"source_hash": "different"},
        ),
        context=PinnedMemoryRequestContext(
            team_id=config.gateway.team_id,
            canonical_user_id=config.user.canonical_user_id,
        ),
    )
    user_path = config.workspace_path / "USER.md"
    user_path.write_text("New legacy content.\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="verification failed"):
        migrate_legacy_user_markdown(
            workspace_dir=config.workspace_path,
            instance_root=config.instance_root_path,
            service=service,
            team_id=config.gateway.team_id,
            canonical_user_id=config.user.canonical_user_id,
            request_timezone=config.user.timezone,
        )

    assert user_path.exists()


def test_legacy_user_markdown_migrates_once_and_archives_source(tmp_path: Path) -> None:
    config = _config(tmp_path)
    user_path = config.workspace_path / "USER.md"
    user_path.write_text(
        "# User Profile\n\nName: Owner\n\n## Initial Context\nPrefers concise updates.\n",
        encoding="utf-8",
    )

    migration_args = {
        "workspace_dir": config.workspace_path,
        "instance_root": config.instance_root_path,
        "service": _memory_service(config),
        "team_id": config.gateway.team_id,
        "canonical_user_id": config.user.canonical_user_id,
        "request_timezone": config.user.timezone,
    }
    first = migrate_legacy_user_markdown(**migration_args)
    second = migrate_legacy_user_markdown(**migration_args)
    rows = create_pinned_memory_repository(config.manager_storage_settings).list_usermem(
        canonical_user_id=config.user.canonical_user_id,
    )

    assert first.status == "migrated"
    assert first.archive_path is not None and first.archive_path.exists()
    assert not user_path.exists()
    assert second.status == "not_present"
    assert len(rows) == 1
    assert rows[0].source_key == LEGACY_USER_MD_SOURCE_KEY
    assert "Prefers concise updates." in rows[0].fact_text


def test_obsolete_tools_markdown_is_reported_but_preserved(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools_path = config.workspace_path / "TOOLS.md"
    tools_path.write_text("# My retained notes\n", encoding="utf-8")

    result = report_obsolete_tools_markdown(workspace_dir=config.workspace_path)

    assert result.status == "obsolete_user_review_required"
    assert tools_path.exists()


def test_legacy_heartbeat_markdown_migrates_and_archives_source(tmp_path: Path) -> None:
    config = _config(tmp_path)
    heartbeat_path = config.workspace_path / "HEARTBEAT.md"
    heartbeat_path.write_text(
        "# Heartbeat Tasks\n\n## Active Tasks\n\n- Check inbox.\n- Review reminders.\n\n## Completed\n",
        encoding="utf-8",
    )
    store = HeartbeatStore(storage_settings=config.manager_storage_settings)
    store.ensure_source(source_id="hb_source_test", title="Heartbeat")
    counter = iter(("conversation_1", "conversation_2"))

    result = migrate_legacy_heartbeat_markdown(
        workspace_dir=config.workspace_path,
        instance_root=config.instance_root_path,
        heartbeat_store=store,
        source_id="hb_source_test",
        conversation_id_factory=lambda: next(counter),
    )
    items = store.list_items("hb_source_test")

    assert result.status == "migrated"
    assert result.imported_count == 2
    assert result.archive_path is not None and result.archive_path.exists()
    assert not heartbeat_path.exists()
    assert {item.instruction for item in items} == {"Check inbox.", "Review reminders."}
    assert all(str(item.source_key or "").startswith("legacy_heartbeat_md_v1:") for item in items)

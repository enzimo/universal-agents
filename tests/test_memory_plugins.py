from __future__ import annotations

from pathlib import Path

import pytest

from custom_agents.context_engine import ContextAssemblyRequest
from custom_agents.memory_plugins.config import (
    normalize_memory_config,
)
from custom_agents.memory_plugins.hydration import CorememHydrationPlugin, UsermemHydrationPlugin
from custom_agents.memory_plugins.manager import MemoryPluginManager
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from storage.pinned_memory_repository import PinnedMemoryFactRow
from storage.pinned_memory_repository import SqlitePinnedMemoryRepository


def _request(*, canonical_user_id: str | None = "user_123") -> ContextAssemblyRequest:
    task = Task(
        task_id="task_plugin_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Use memory plugins.",
    )
    return ContextAssemblyRequest(
        incoming_content=task,
        execution_task_id=task.task_id,
        incoming_header=MessageHeader(),
        incoming_meta=MessageMeta(
            conversation_id="conversation_plugin_1",
            gateway_context=GatewayContext(canonical_user_id=canonical_user_id),
        ),
        identity_meta=MessageMeta(
            conversation_id="conversation_plugin_1",
            gateway_context=GatewayContext(canonical_user_id=canonical_user_id),
        ),
        incoming_message_json="{}",
    )


def _row(*, bank: str, team_id: str = "", canonical_user_id: str = "", memory_id: int = 1, text: str = "Pinned fact") -> PinnedMemoryFactRow:
    return PinnedMemoryFactRow(
        bank=bank,
        team_id=team_id,
        canonical_user_id=canonical_user_id,
        memory_id=memory_id,
        fact_uuid=f"fact_{memory_id}",
        fact_text=text,
        valid_from_utc="2026-01-01T00:00:00+00:00",
        invalid_after_utc=None,
        tags_json='["tag"]',
        metadata_json='{"source":"test"}',
        source_conversation_id=None,
        source_task_id=None,
        source_turn_index=None,
        source_gateway_session_key=None,
        source_channel_name=None,
        source_chat_id=None,
        source_thread_id=None,
        created_by_kind="test",
        created_by_id=None,
        created_by_display_name=None,
        created_at_utc="2026-01-01T00:00:00+00:00",
        updated_at_utc="2026-01-01T00:00:00+00:00",
    )


class _PinnedRepo:
    def list_coremem(self, *, team_id: str, now_utc: str | None = None, sort: str = "memory_id"):
        assert team_id == "team_test"
        return [_row(bank="coremem", team_id=team_id, text="Team fact")]

    def list_usermem(self, *, canonical_user_id: str, now_utc: str | None = None, sort: str = "memory_id"):
        assert canonical_user_id == "user_123"
        return [_row(bank="usermem", canonical_user_id=canonical_user_id, text="User fact")]


def test_memreview_prompt_requires_event_id_undo_notice() -> None:
    prompt = Path("ua_definitions/agent_definitions/prompts/memory_review.md").read_text(encoding="utf-8")

    assert "using the `notify_user(target=...)` target given in the review job" in prompt
    assert "returned event id" in prompt
    assert "/coremem undo <event_id>" in prompt
    assert "/usermem undo <event_id>" in prompt


def test_unknown_memory_plugin_id_fails_config_validation() -> None:
    with pytest.raises(ValueError, match="Unknown memory plugin"):
        normalize_memory_config({"plugins": {"order": ["coremem", "not_a_plugin"]}})


def test_invalid_memory_notification_delivery_target_fails_config_validation() -> None:
    with pytest.raises(ValueError, match="agent.memory.notifications.delivery_target"):
        normalize_memory_config({"notifications": {"delivery_target": "discord"}})


@pytest.mark.parametrize("plugin_id", [
    "user_profile_summary", "team_profile_summary",
    "long_term_profile_full", "memory_graph_retrieval", "unknown_plugin",
])
@pytest.mark.parametrize("location", ["order", "enabled_settings", "disabled_settings"])
def test_unsupported_memory_plugins_are_rejected(plugin_id: str, location: str) -> None:
    plugins = (
        {"order": ["coremem", plugin_id]}
        if location == "order"
        else {plugin_id: {"enabled": location == "enabled_settings"}}
    )

    with pytest.raises(ValueError, match=f"Unknown memory plugin id\\(s\\): {plugin_id}"):
        normalize_memory_config({"plugins": plugins})


@pytest.mark.parametrize("owner", ["defaults", "override", "agent"])
def test_configuration_rejects_removed_plugins_even_with_hydration_disabled(owner):
    from custom_agents.strands_agent import UniversalAgent
    from runtime_settings.agents import AgentRuntimeDefaultsSettings, AgentRuntimeOverrideSettings

    memory = {
        "plugin_manager": {"enabled": False},
        "plugins": {"memory_graph_retrieval": {"enabled": False}},
    }
    with pytest.raises(ValueError, match="Unknown memory plugin id"):
        if owner == "agent":
            UniversalAgent(role="validation", tools=[], memory_config=memory)
        else:
            model = AgentRuntimeDefaultsSettings if owner == "defaults" else AgentRuntimeOverrideSettings
            model(memory=memory)


def test_coremem_and_usermem_hydration_enforce_scope() -> None:
    # Stands in for the verified person (tests/test_memory_verified_person.py checks the real one).
    manager = MemoryPluginManager(
        agent_id="agent_1",
        team_id="team_test",
        named_agent_type="TeamArchitect",
        verified_person_of=lambda meta: meta.gateway_context.canonical_user_id,
        pinned_memory_repository=_PinnedRepo(),
        hydration_plugins=[CorememHydrationPlugin(), UsermemHydrationPlugin()],
    )

    sections = manager.hydrate(_request())
    unregistered_sections = manager.hydrate(_request(canonical_user_id=None))

    assert [section.name for section in sections] == ["coremem", "usermem"]
    assert "Team fact" in sections[0].text
    assert "User fact" in sections[1].text
    assert [section.name for section in unregistered_sections] == ["coremem"]


def test_memory_plugin_manager_prunes_expired_pinned_memory(tmp_path: Path) -> None:
    repository = SqlitePinnedMemoryRepository(str(tmp_path / "memory.db"))
    repository.create_coremem(
        team_id="team_test",
        fact_text="Expired team fact",
        capacity=5,
        invalid_after_utc="2026-01-01T00:00:00+00:00",
    )
    manager = MemoryPluginManager(
        agent_id="agent_1",
        team_id="team_test",
        named_agent_type="TeamArchitect",
        verified_person_of=lambda _meta: None,
        pinned_memory_repository=repository,
        hydration_plugins=[],
    )

    results = manager.prune("2026-06-10T00:00:00+00:00")

    assert any(result.plugin_id == "pinned_memory_pruner" and result.pruned_count == 1 for result in results)
    assert repository.list_coremem(team_id="team_test") == []

from __future__ import annotations

from pathlib import Path

from custom_agents.conversation_store import ConversationStore
from custom_agents.memory_plugins.base import MemoryPluginContext
from custom_agents.memory_plugins.config import normalize_memory_config
from custom_agents.memory_plugins.hydration import DomainTopicRecallPlugin
from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType


def _store(tmp_path: Path) -> ConversationStore:
    store = ConversationStore(str(tmp_path / "conversation.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    store.ensure_thread(
        conversation_id="conversation_cars",
        team_id="team_test",
        owner_canonical_user_id="user_a",
        owner_agent_id="agent_1",
        owner_agent_name="TeamArchitect",
        requester_name="Alice",
        requester_id="user_a",
        root_task_id="task_cars",
        subject_preview="Car repair brake troubleshooting",
    )
    store.ensure_thread(
        conversation_id="conversation_private",
        team_id="team_test",
        owner_canonical_user_id="user_a",
        owner_agent_id="agent_1",
        owner_agent_name="TeamArchitect",
        requester_name="Alice",
        requester_id="user_a",
        root_task_id="task_private",
        subject_preview="Private project topic",
    )
    store.ensure_thread(
        conversation_id="conversation_other_team",
        team_id="other_team",
        owner_canonical_user_id="user_b",
        owner_agent_id="agent_2",
        owner_agent_name="TeamArchitect",
        requester_name="Bob",
        requester_id="user_b",
        root_task_id="task_other",
        subject_preview="Other team car repair",
    )
    store.tag_thread(conversation_id="conversation_cars", tag="car repair", team_id="team_test")
    store.tag_thread(
        conversation_id="conversation_private",
        tag="private project",
        team_id="team_test",
        canonical_user_id="user_a",
    )
    store.tag_thread(conversation_id="conversation_other_team", tag="car repair", team_id="other_team")
    return store


def _context(store: ConversationStore, *, text: str, canonical_user_id: str | None = "user_a") -> MemoryPluginContext:
    task = Task(
        task_id="task_current",
        task_type=MessageMetaType.REQUEST,
        problem_statement=text,
    )
    return MemoryPluginContext(
        agent_id="agent_current",
        team_id="team_test",
        named_agent_type="TeamArchitect",
        task=task,
        msg_meta=MessageMeta(conversation_id="conversation_current"),
        canonical_user_id=canonical_user_id,
        request_timezone="UTC",
        current_utc="2026-06-10T00:00:00+00:00",
        conversation_id="conversation_current",
        gateway_context=None,
        conversation_store=store,
        config=normalize_memory_config({}),
    )


def test_conversation_store_topic_tags_filter_by_team_and_user_scope(tmp_path: Path) -> None:
    store = _store(tmp_path)

    known_for_user = store.list_known_thread_tags(team_id="team_test", canonical_user_id="user_a")
    known_shared = store.list_known_thread_tags(team_id="team_test", canonical_user_id=None)
    car_matches = store.find_threads_by_tags(tags=["car repair"], team_id="team_test", canonical_user_id="user_a")
    private_matches_for_other = store.find_threads_by_tags(
        tags=["private project"],
        team_id="team_test",
        canonical_user_id="user_b",
    )

    assert {item["tag"] for item in known_for_user} == {"car-repair", "private-project"}
    assert {item["tag"] for item in known_shared} == {"car-repair"}
    assert [item["conversation_id"] for item in car_matches] == ["conversation_cars"]
    assert private_matches_for_other == []


def test_domain_topic_recall_plugin_injects_compact_hints_only_when_relevant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plugin = DomainTopicRecallPlugin()

    sections = plugin.hydrate(_context(store, text="Can we revisit car repair options?"))
    unrelated = plugin.hydrate(_context(store, text="Plan a gardening calendar."))

    assert len(sections) == 1
    assert sections[0].name == "domain_topic_recall"
    assert "conversation_id=conversation_cars" in sections[0].text
    assert "Car repair brake troubleshooting" in sections[0].text
    assert "Other team car repair" not in sections[0].text
    assert unrelated == []

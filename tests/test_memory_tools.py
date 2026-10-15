"""The merged memory tools (custom_tools.providers.memory): memory and topic_tags.

Each replaces several functions, one action per call. These tests use the real
pinned memory service and conversation store (SQLite) and, for access, real
Cedar with the access hook's own decision: every action does what the function
it replaced did, the inputs each action takes are checked, and access is
decided per action, so a block can still stop exactly one of them.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents import access_runtime
from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.access_v2 import SubjectKind, SubjectRefV1, TargetKind, TargetRefV1
from custom_tools.providers.memory import memory, topic_tags
from custom_tools.runtime_context import set_agent_ref
from tests.test_access_request_tools import BINARY, OWNER, TEAM, make_agent, owner_service
from tests.test_access_request_tools import storage as access_storage
from tests.test_strands_user_scope_tools import _FakeAgent, _signed_task_contexts  # noqa: F401 - signing keys

storage = access_storage  # the shared fixture: a manager database and verify keys


@pytest.fixture
def agent(tmp_path: Path):
    agent = _FakeAgent(tmp_path / "memory.db", "usr_a")
    agent.memory_config = {"coremem": {"max_facts_per_team": 10}, "usermem": {"max_facts_per_user": 10}}
    store = ConversationStore(str(tmp_path / "conversation.db"), artifacts_dir=tmp_path / "artifacts")
    for conversation_id, owner in (("conv_mine", "usr_a"), ("conv_theirs", "usr_b")):
        store.ensure_thread(
            conversation_id=conversation_id, team_id=agent.team_id, owner_canonical_user_id=owner,
            owner_agent_id="agent_manager", owner_agent_name="TeamArchitect", requester_name=owner,
            requester_id=owner, root_task_id=f"task_{conversation_id}", subject_preview="Trip planning",
        )
    agent._conversation_store = store
    set_agent_ref(agent)
    yield agent
    set_agent_ref(None)


def call(tool, **inputs) -> dict:
    return json.loads(tool(**inputs))


# -- memory --------------------------------------------------------------------------


@pytest.mark.parametrize("bank", ["coremem", "usermem"])
def test_every_memory_action_works_on_both_banks(agent, bank) -> None:
    created = call(memory, action="create", bank=bank, fact_text="Prefers trains.", tags=["travel"])
    assert created["ok"] is True, created
    memory_id = created["fact"]["memory_id"]
    if bank == "usermem":
        # The person the task works for, from its signed context.
        [row] = agent._pinned_memory_repository.list_usermem(canonical_user_id="usr_a")
        assert row.fact_text == "Prefers trains."

    replaced = call(memory, action="replace", bank=bank, memory_id=memory_id, fact_text="Prefers night trains.")
    listed = call(memory, action="list", bank=bank, sort="newest", start=1, end=5)
    assert replaced["ok"] and [fact["fact_text"] for fact in listed["facts"]] == ["Prefers night trains."]

    undone = call(memory, action="undo", bank=bank, event_id=replaced["event_id"])
    assert undone["ok"], undone
    assert [fact["fact_text"] for fact in call(memory, action="list", bank=bank)["facts"]] == ["Prefers trains."]

    deleted = call(memory, action="delete", bank=bank, memory_id=memory_id)
    assert deleted["ok"] and call(memory, action="list", bank=bank)["facts"] == []


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"action": "create", "bank": "coremem"}, "memory(action='create') needs fact_text."),
        ({"action": "delete", "bank": "usermem"}, "memory(action='delete') needs memory_id."),
        ({"action": "undo", "bank": "coremem"}, "memory(action='undo') needs event_id."),
        (
            {"action": "delete", "bank": "coremem", "memory_id": 1, "fact_text": "x"},
            "memory(action='delete') does not take fact_text.",
        ),
        ({"action": "list", "bank": "coremem", "event_id": "e1"}, "memory(action='list') does not take event_id."),
        ({"action": "purge", "bank": "coremem"}, "memory has no action 'purge'; use one of: list, create, replace, delete, undo."),
        ({"action": "list", "bank": "teammem"}, "memory takes bank coremem or usermem, not 'teammem'."),
    ],
)
def test_memory_says_which_inputs_an_action_takes_and_changes_nothing(agent, inputs, message) -> None:
    result = call(memory, **inputs)

    assert (result["ok"], result["error_code"], result["message"]) == (False, "invalid_input", message)
    assert call(memory, action="list", bank="coremem")["facts"] == []


def test_empty_inputs_count_as_left_out(agent) -> None:
    # Models often send "" or [] for an input they do not mean to use.
    created = call(memory, action="create", bank="coremem", fact_text="Ships on Fridays.", event_id="", tags=[])

    assert created["ok"] is True, created


# -- topic tags ----------------------------------------------------------------------


def test_every_topic_tags_action_works_for_the_tasks_person(agent) -> None:
    thread = call(topic_tags, action="tag_thread", conversation_id="conv_mine", tags=["Travel", "trains"])
    turn = call(topic_tags, action="tag_turn", conversation_id="conv_mine", turn_index=2, tags=["tickets"])
    assert [row["tag"] for row in thread["tags"]] == ["travel", "trains"] and turn["turn_index"] == 2

    assert {"travel", "trains"} <= {row["tag"] for row in call(topic_tags, action="list")["tags"]}
    found = call(topic_tags, action="find", tags=["trains", "boats"], limit=5)
    assert [thread["conversation_id"] for thread in found["threads"]] == ["conv_mine"]

    removed = call(topic_tags, action="untag", conversation_id="conv_mine", tags=["trains"])
    assert removed["removed"] == [{"tag": "trains", "deleted": True}]
    assert call(topic_tags, action="find", tags=["trains"])["threads"] == []


def test_topic_tags_refuses_another_persons_conversation_and_checks_inputs(agent) -> None:
    refused = call(topic_tags, action="tag_thread", conversation_id="conv_theirs", tags=["travel"])
    missing = call(topic_tags, action="tag_turn", conversation_id="conv_mine", tags=["x"])
    unused = call(topic_tags, action="list", conversation_id="conv_mine")

    assert "error" in refused and call(topic_tags, action="find", tags=["travel"])["threads"] == []
    assert missing["error"] == "topic_tags(action='tag_turn') needs turn_index."
    assert unused["error"] == "topic_tags(action='list') does not take conversation_id."


# -- access is decided per action ------------------------------------------------------


@pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")
def test_access_is_decided_per_action_so_a_block_stops_exactly_one(storage, tmp_path) -> None:
    lead = make_agent(storage, tmp_path, agent_type="TeamArchitect", lead=True)
    worker = make_agent(storage, tmp_path / "worker", agent_type="WebResearchAgent")
    service = owner_service(storage)
    try:
        for action in ("list", "create", "replace", "delete", "undo"):
            decision = access_runtime.decide_call(lead, "memory", {"action": action, "bank": "coremem"})
            assert decision.allowed, (action, decision.reason)
            assert decision.operation == f"memory.{action}"
        assert not access_runtime.decide_call(worker, "memory", {"action": "list", "bank": "coremem"}).allowed

        from access_control.requests import AccessRequests

        AccessRequests(service=service, inbox=SimpleNamespace(), lead_agent_type="TeamArchitect").block(
            OWNER,
            subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/TeamArchitect"),
            target=TargetRefV1(kind=TargetKind.OPERATION, id="fn:memory#memory.delete"),
            reason="No deleting facts this week",
        )

        assert not access_runtime.decide_call(lead, "memory", {"action": "delete", "bank": "usermem"}).allowed
        assert access_runtime.decide_call(lead, "memory", {"action": "create", "bank": "usermem"}).allowed
        assert access_runtime.decide_call(lead, "topic_tags", {"action": "tag_thread"}).allowed
    finally:
        service.cedar.close()
        access_runtime._runtime_access.cache_clear()

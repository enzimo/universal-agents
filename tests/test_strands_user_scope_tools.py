from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from access_control.ownership import PRIVATE_UPDATE, private_access
from custom_messages.contracts.routing_v1 import GatewayContext
from custom_tools.providers.context_memory import compact_conversation_context
from custom_tools.providers.memory import memory, topic_tags
from custom_tools.runtime_context import set_agent_ref
from custom_tools.tool_results import result_text
from storage.config import resolve_storage_settings
from storage.pinned_memory_repository import SqlitePinnedMemoryRepository
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts

TEAM = "team_test"


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Private-data tools act for the person in the task's gateway-signed context."""

    use_signed_contexts(monkeypatch)


class _FakeTaskManager:
    current_task_id = "task_current"

    def __init__(self, canonical_user_id: str) -> None:
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self._canonical_user_id = canonical_user_id
        self._db_path = "task_history.db"

    def get_task_info(self, task_id: str):
        if task_id != self.current_task_id:
            return None
        return SimpleNamespace(
            meta=SimpleNamespace(
                conversation_id="conversation_current",
                request_timezone="UTC",
                gateway_context=GatewayContext(
                    source="test",
                    user_id=self._canonical_user_id,
                    canonical_user_id=self._canonical_user_id,
                    channel_name="telegram",
                    chat_id="chat_test",
                    task_context=context_signer().issue(
                        team_id=TEAM, person_id=self._canonical_user_id, person_roles=("member",)
                    ),
                ),
            )
        )


class _FakeAgent:
    team_id = TEAM
    id = "agent_manager"
    name = "Manager"
    named_agent_type = "TeamArchitect"
    memory_config = {"usermem": {"max_facts_per_user": 10}}
    # The per-call access hook is not part of these tests.
    authorization_config = {"enabled": False}

    def __init__(self, db_path: Path, canonical_user_id: str) -> None:
        self._pinned_memory_repository = SqlitePinnedMemoryRepository(str(db_path))
        self.task_manager = _FakeTaskManager(canonical_user_id)
        self.task_manager.storage_settings = resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": str(db_path)}}
        )


def test_no_memory_input_names_another_person() -> None:
    # usermem and topic tags belong to the person in the task's signed context.
    for function in (memory, topic_tags):
        assert "canonical_user_id" not in function.tool_spec["inputSchema"]["json"]["properties"]


def test_usermem_without_a_registered_person_is_refused(tmp_path: Path) -> None:
    set_agent_ref(_FakeAgent(tmp_path / "memory.db", ""))

    result = memory(action="list", bank="usermem")
    payload = json.loads(result_text(result))

    assert result["status"] == "error"
    assert payload["ok"] is False
    assert payload["error_code"] == "canonical_user_required"


def test_usermem_tool_uses_current_canonical_user_scope(tmp_path: Path) -> None:
    agent = _FakeAgent(tmp_path / "memory.db", "usr_a")
    set_agent_ref(agent)

    create_payload = json.loads(memory(action="create", bank="usermem", fact_text="Prefers terse responses."))
    list_payload = json.loads(memory(action="list", bank="usermem"))

    assert create_payload["ok"] is True
    assert list_payload["ok"] is True
    assert list_payload["facts"][0]["canonical_user_id"] == "usr_a"
    assert list_payload["facts"][0]["fact_text"] == "Prefers terse responses."


def _agent_with_conversation_owner(
    tmp_path: Path,
    *,
    current_owner: str,
    conversation_owner: str,
):
    agent = _FakeAgent(tmp_path / "memory.db", current_owner)
    agent._conversation_store = SimpleNamespace(
        get_thread=lambda conversation_id: {
            "conversation_id": conversation_id,
            "owner_canonical_user_id": conversation_owner,
            "team_id": agent.team_id,
        }
    )
    agent._conversation_compactor = MagicMock()
    agent._conversation_compactor.compact_if_needed.return_value = SimpleNamespace(
        compacted=True
    )
    return agent


def test_compact_conversation_context_hides_cross_owner_conversation(
    tmp_path: Path,
) -> None:
    agent = _agent_with_conversation_owner(
        tmp_path,
        current_owner="usr_a",
        conversation_owner="usr_b",
    )
    set_agent_ref(agent)

    payload = json.loads(
        compact_conversation_context(conversation_id="conversation_private")
    )

    assert payload == {"error": "Conversation conversation_private not found"}
    agent._conversation_compactor.compact_if_needed.assert_not_called()


def test_compact_conversation_context_authorizes_private_update(tmp_path: Path) -> None:
    agent = _agent_with_conversation_owner(
        tmp_path,
        current_owner="usr_a",
        conversation_owner="usr_a",
    )
    set_agent_ref(agent)

    with patch(
        "custom_tools.providers._user_scope.private_access",
        wraps=private_access,
    ) as authorize:
        payload = json.loads(
            compact_conversation_context(conversation_id="conversation_owned")
        )

    assert payload["conversation_id"] == "conversation_owned"
    assert authorize.call_args.kwargs["action"] == PRIVATE_UPDATE
    agent._conversation_compactor.compact_if_needed.assert_called_once()

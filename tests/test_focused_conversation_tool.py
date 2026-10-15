from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.conversation_store import ConversationStore
from custom_messages.contracts.routing_v1 import GatewayContext
from custom_tools.providers.context_memory import create_focused_conversation
from custom_tools.runtime_context import set_agent_ref
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts

TEAM = "team_test"


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Conversation tools act for the person in the task's gateway-signed context."""

    use_signed_contexts(monkeypatch)


OWNER_A = "usr_owner_a"
OWNER_B = "usr_owner_b"


class _FakeTaskManager:
    current_task_id = "task_current"
    _db_path = "task_history.db"

    def __init__(self, canonical_user_id: str = OWNER_A) -> None:
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self._canonical_user_id = canonical_user_id

    def get_task_info(self, task_id: str):
        if task_id != "task_current":
            return None
        return SimpleNamespace(
            meta=SimpleNamespace(
                conversation_id="conv_source",
                from_agent_name="User",
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
    named_agent_type = "TeamArchitect"
    # The per-call access hook is not part of these tests.
    authorization_config = {"enabled": False}

    def __init__(
        self, conversation_store: ConversationStore, canonical_user_id: str = OWNER_A
    ) -> None:
        self._conversation_store = conversation_store
        self.task_manager = _FakeTaskManager(canonical_user_id)
        self.task_manager.storage_settings = resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": str(conversation_store._db_path)}}
        )


def _seed_source_thread(
    store: ConversationStore, owner_canonical_user_id: str = OWNER_A
) -> None:
    store.ensure_thread(
        conversation_id="conv_source",
        team_id="team_test",
        owner_canonical_user_id=owner_canonical_user_id,
        owner_agent_id="agent_manager",
        owner_agent_name="TeamArchitect",
        requester_name="User",
        requester_id="user_1",
        root_task_id="task_1",
        subject_preview="Long-running repair conversation",
    )
    store.append_turn(
        conversation_id="conv_source",
        task_id="task_1",
        followup_to_task_id=None,
        owner_agent_id="agent_manager",
        requester_name="User",
        requester_id="user_1",
        correlation_id="corr_1",
        problem_text="Talk about unrelated workshop storage.",
        owner_canonical_user_id=owner_canonical_user_id,
    )
    store.update_turn_completion(
        task_id="task_1",
        task_status="completed",
        final_answer_text="Storage answer.",
        turn_summary_text="Discussed storage.",
        user_request_summary="Asked about workshop storage.",
        assistant_response_summary="Suggested storage bins.",
    )
    store.append_turn(
        conversation_id="conv_source",
        task_id="task_2",
        followup_to_task_id="task_1",
        owner_agent_id="agent_manager",
        requester_name="User",
        requester_id="user_1",
        correlation_id="corr_2",
        problem_text="Repair a single-phase electric motor.",
        owner_canonical_user_id=owner_canonical_user_id,
    )
    store.update_turn_completion(
        task_id="task_2",
        task_status="completed",
        final_answer_text="Motor repair answer.",
        turn_summary_text="Discussed motor repair safety and bearings.",
        user_request_summary="Asked about repairing a single-phase electric motor.",
        assistant_response_summary="Explained lockout, inspection, and bearing matching.",
    )


def test_create_focused_conversation_seeds_new_thread_with_selected_turn_context(
    tmp_path: Path,
) -> None:
    store = ConversationStore(str(tmp_path / "task_history.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    _seed_source_thread(store)
    set_agent_ref(_FakeAgent(store))

    result = create_focused_conversation(
        title="Electric Motor Repair Website",
        summary="Use only the motor-repair context.",
        source_turn_indices_json="[2]",
    )
    payload = json.loads(result)

    assert payload["status"] == "success"
    assert payload["source_conversation_id"] == "conv_source"
    assert payload["source_turn_indices"] == [2]
    thread = store.get_thread(payload["conversation_id"])
    epoch = store.get_context_epoch(payload["context_epoch_id"])
    assert thread is not None
    assert thread["subject_preview"] == "Electric Motor Repair Website"
    assert epoch is not None
    assert thread["owner_canonical_user_id"] == OWNER_A
    assert "Electric Motor Repair Website" in epoch["summary_markdown"]
    assert "single-phase electric motor" in epoch["summary_markdown"]
    assert "Suggested storage bins" not in epoch["summary_markdown"]


def test_create_focused_conversation_hides_other_users_source_thread(
    tmp_path: Path,
) -> None:
    store = ConversationStore(str(tmp_path / "task_history.db"), artifacts_dir=tmp_path / "conversation_artifacts")
    _seed_source_thread(store, owner_canonical_user_id=OWNER_A)
    set_agent_ref(_FakeAgent(store, canonical_user_id=OWNER_B))

    result = create_focused_conversation(
        title="Electric Motor Repair Website",
        summary="Use only the motor-repair context.",
        source_conversation_id="conv_source",
        source_turn_indices_json="[2]",
    )
    payload = json.loads(result)

    assert payload == {"error": "Conversation conv_source not found"}

import json

import pytest

import strands_main
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from storage.config import resolve_storage_settings
from storage.runtime_state_repository import create_runtime_state_repository


@pytest.mark.parametrize("archive_fails", [True, False])
def test_operator_deferred_failure_keeps_custody_until_archive_commits(
    tmp_path, monkeypatch, archive_fails
):
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "state.db")}}
    )
    repository = create_runtime_state_repository(settings)
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="pending",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Pending",
        ),
        msg_header=MessageHeader(),
        msg_meta=MessageMeta(),
    )
    repository.add_deferred_message(
        owner_agent_id="worker",
        owner_agent_name="Worker",
        team_id="team",
        owner_named_agent_type="Worker",
        message_id=envelope.msg_header.message_id,
        envelope_json=json.dumps(envelope.model_dump(mode="json")),
        first=False,
        expect_existing=False,
    )
    original = repository.load_deferred_messages_by_owner("worker")

    def archive(**kwargs):
        assert repository.load_deferred_messages_by_owner("worker") == original
        if archive_fails:
            raise RuntimeError("injected archive failure")
        return kwargs["task_request"].task_id

    monkeypatch.setattr(strands_main, "_archive_failed_envelope_task", archive)
    context = strands_main.OperatorContext(
        storage_settings=settings,
        db_path=settings.sqlite_path,
        team_id="team",
        named_agent_type="Worker",
        is_team_lead=False,
        owner_agent_name="Worker",
    )
    if archive_fails:
        with pytest.raises(RuntimeError, match="injected archive failure"):
            strands_main._fail_latest_deferred_task(
                context=context, owner_agent_id="worker", reason="Operator stop"
            )
        assert repository.load_deferred_messages_by_owner("worker") == original
    else:
        outcome = strands_main._fail_latest_deferred_task(
            context=context, owner_agent_id="worker", reason="Operator stop"
        )
        assert outcome["task_id"] == "pending"
        assert (
            json.loads(
                repository.load_deferred_messages_by_owner("worker")[0].messages_json
            )
            == []
        )


def test_operator_missing_task_does_not_remove_snapshot(tmp_path):
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "state.db")}}
    )
    repository = create_runtime_state_repository(settings)
    repository.save_task_runtime(
        expect_existing=False,
        owner_agent_id="worker",
        owner_agent_name="Worker",
        team_id="team",
        owner_named_agent_type="Worker",
        runtime_state_json='{"schema_version":3,"tasks":[]}',
    )
    original = repository.load_task_runtime_by_owner("worker")
    with pytest.raises(RuntimeError, match="not present"):
        strands_main._fail_persisted_runtime_task(
            storage_settings=settings,
            db_path=settings.sqlite_path,
            owner_agent_id="worker",
            task_id="missing",
            reason="Operator stop",
        )
    assert repository.load_task_runtime_by_owner("worker") == original

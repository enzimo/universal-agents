"""Real versioned snapshot data for storage and custody checks."""

import json

from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3, TaskSnapshotRecord
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from storage.runtime_snapshot_layout import canonical_json


def snapshot_json(task_id: str = "work") -> str:
    snapshot = RuntimeSnapshotV3(
        schema_version=3,
        tasks=[TaskSnapshotRecord(
            task_id=task_id, state=ProgressState.ACCEPTED,
            task=Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement="Retain custody"),
            header=MessageHeader(message_id=f"message_{task_id}", timestamp="2026-09-07T00:00:00+00:00"),
            meta=MessageMeta(),
            owner_person_id=None,
        )],
        task_queue=[task_id],
    )
    return canonical_json(snapshot.model_dump(mode="json"))


def deferred_message_json(message_id: str) -> str:
    """A stored deferred message: only its id matters to the repository."""
    return json.dumps({"msg_header": {"message_id": message_id}, "msg_content": {"task_id": message_id}})


def seed_deferred(repo, owner: str, message_id: str, *, team_id: str = "team", expect_existing: bool = False) -> None:
    """Write one deferred message for ``owner`` the way the inbox does."""
    repo.add_deferred_message(
        owner_agent_id=owner, owner_agent_name=owner, team_id=team_id, owner_named_agent_type="Worker",
        message_id=message_id, envelope_json=deferred_message_json(message_id), first=False,
        expect_existing=expect_existing,
    )

"""Real versioned snapshot data for storage and custody checks."""

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

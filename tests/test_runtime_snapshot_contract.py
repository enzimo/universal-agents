"""Portable recovery behavior, independent of transport, databases and the SDK."""

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from custom_agents.runtime_snapshot_codec import SnapshotSource, restore_snapshot_state
from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot
from custom_messages.transport_codec import decode_envelope, encode_envelope
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from tests.runtime_snapshot_fixtures import snapshot_json


CORPUS = json.loads((Path(__file__).resolve().parents[1] / "data/contracts/runtime-snapshots/v3/recovery.json").read_text())


@pytest.mark.parametrize("case", CORPUS["cases"], ids=lambda case: case["id"])
def test_language_neutral_recovery_corpus(case):
    if case.get("reject"):
        with pytest.raises(ValueError):
            restore(case["snapshots"], cancelled=case["cancelled"])
        return
    state = restore(case["snapshots"], cancelled=case["cancelled"])
    assert {
        "tasks": sorted(state.task_state.tasks),
        "queue": list(state.task_state.task_queue),
        "outbox": {key: record.status for key, record in state.execution_state.turn_action_outbox.items()},
        "mailbox": {key: event.status for key, event in state.execution_state.runtime_mailbox_events.items()},
    } == case["expected"]


def restore(documents, *, cancelled=()):
    sources = [SnapshotSource(f"owner_{i}", "2026-09-07T00:00:00+00:00", decode_runtime_snapshot(json.dumps(doc)))
               for i, doc in enumerate(documents)]
    return restore_snapshot_state(sources, cancelled=set(cancelled), plans_enabled=True,
                                  objectives_enabled=True, logger=logging.getLogger(__name__))


@pytest.mark.parametrize("corrupt", [
    lambda doc: doc.update(schema_version=2),
    lambda doc: doc.update(unknown_state={}),
    lambda doc: doc["tasks"][0].update(task_id="different_identity"),
    lambda doc: doc.update(turn_action_outbox={"broken": {"action_id": "broken"}}),
    lambda doc: doc.update(turn_action_batches={"wrong_key": {"batch_id": "batch", "source_task_id": "work"}}),
])
def test_invalid_snapshot_rejects_whole_document_instead_of_dropping_records(corrupt):
    snapshot = json.loads(snapshot_json())
    corrupt(snapshot)
    with pytest.raises(ValueError):
        decode_runtime_snapshot(json.dumps(snapshot))


def test_wire_decode_preserves_authored_identity_and_rejects_missing_identity():
    envelope = MessageEnvelope(
        msg_header=MessageHeader(message_id="authored", timestamp="2026-09-07T00:00:00+00:00"),
        msg_content=Task(task_type=MessageMetaType.REQUEST, task_id="work"),
    )
    assert encode_envelope(decode_envelope(encode_envelope(envelope))) == encode_envelope(envelope)
    document = json.loads(encode_envelope(envelope))
    del document["msg_header"]["message_id"]
    with pytest.raises(ValueError, match="original message_id"):
        decode_envelope(json.dumps(document))


def test_contract_and_policy_imports_do_not_load_strands():
    subprocess.run([sys.executable, "-c", """
import sys
from custom_messages.transport_codec import decode_envelope
from custom_agents.runtime_snapshot_codec import restore_snapshot_state
assert not any(name == 'strands' or name.startswith('strands.') for name in sys.modules)
"""], check=True, capture_output=True, text=True)


def test_tracked_transport_and_snapshot_schemas_match_contracts():
    from scripts.export_runtime_contract_schemas import ROOT, render_schema_files

    for name, expected in render_schema_files().items():
        assert (ROOT / "data/schemas" / name).read_text() == expected

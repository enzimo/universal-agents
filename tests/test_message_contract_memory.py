"""Wire bodies preserve the existing SQLite memory service's inputs and results.

The explicit mappings check contract readiness; the protocol dispatcher is not
live yet. They exercise actual storage rather than a simulated service result.
"""

from __future__ import annotations

import json
from datetime import datetime

from custom_agents.pinned_memory_runtime import PinnedMemoryRequestContext, PinnedMemoryService
from custom_messages.wire.frame import format_timestamp
from storage.pinned_memory_repository import SqlitePinnedMemoryRepository
from tests.test_message_contract import checked


def fact_payload(fact):
    payload = {
        "memory_id": fact["memory_id"], "fact_text": {"type": "inline", "text": fact["fact_text"]},
        "tags": fact["tags"], "metadata_json": json.dumps(fact["metadata"]),
        "created_at": format_timestamp(datetime.fromisoformat(fact["created_at_utc"])),
        "updated_at": format_timestamp(datetime.fromisoformat(fact["updated_at_utc"])),
        "seen_version": fact["record_digest"],
    }
    for stored, wire in [("valid_from_utc", "valid_from"), ("invalid_after_utc", "expires")]:
        if fact[stored]:
            payload[wire] = format_timestamp(datetime.fromisoformat(fact[stored]))
    return payload


def test_memory_commands_keep_long_text_and_metadata_through_partial_edits(tmp_path):
    service = PinnedMemoryService(SqlitePinnedMemoryRepository(str(tmp_path / "memory.db")), command_origin="agent_tool")
    context = PinnedMemoryRequestContext(team_id="team_example", canonical_user_id="person_7c1")
    draft = checked("memory.create", {
        "request_id": "memory_create", "bank": "coremem", "fact_text": {"type": "inline", "text": "x" * 12001},
        "metadata_json": '{"resume_section":"deployment"}', "expires": "2099-01-01T00:00:00.000Z",
    })
    created = service.create(
        bank=draft.bank, context=context, fact_text=draft.fact_text.text,
        metadata=json.loads(draft.metadata_json), invalid_after_utc=draft.expires,
    )
    checked("memory.create.reply", {"outcome": "ok", "event_id": created["event_id"], "fact": fact_payload(created["fact"])})
    edit = checked("memory.replace", {
        "request_id": "memory_edit", "bank": "coremem", "memory_id": created["fact"]["memory_id"],
        "seen_version": created["fact"]["record_digest"], "tags": ["operations"], "clear_expiry": True,
    })
    changed = service.replace(
        bank=edit.bank, context=context, memory_id=edit.memory_id,
        expected_record_digest=edit.seen_version, tags=edit.tags, invalid_after_utc="never",
    )
    reply = checked("memory.replace.reply", {
        "outcome": "ok", "event_id": changed["event_id"], "fact": fact_payload(changed["fact"]),
    })
    assert reply.fact.fact_text.text == draft.fact_text.text
    assert json.loads(reply.fact.metadata_json) == json.loads(draft.metadata_json)
    assert reply.fact.tags == ["operations"] and changed["fact"]["invalid_after_utc"] is None


def test_an_expiry_event_can_be_undone_and_reported_without_losing_the_restored_fact(tmp_path):
    repository = SqlitePinnedMemoryRepository(str(tmp_path / "memory.db"))
    service = PinnedMemoryService(repository, command_origin="agent_tool")
    context = PinnedMemoryRequestContext(team_id="team_example", canonical_user_id="person_7c1")
    service.create(bank="coremem", context=context, fact_text="Temporary note.", invalid_after_utc="2000-01-01T00:00:00Z")
    repository.prune_expired()
    with repository._connect() as connection:
        event = connection.execute("SELECT event_id FROM pinned_memory_fact_events WHERE event_type='expire'").fetchone()
    command = checked("memory.undo", {"request_id": "memory_undo", "bank": "coremem", "event_id": event["event_id"]})
    restored = service.undo(bank=command.bank, context=context, event_id=command.event_id)
    reply = checked("memory.undo.reply", {
        "outcome": "ok", "event_id": restored["event_id"], "undone_event_id": restored["undone_event_id"],
        "undone_event_type": restored["undone_event_type"], "deleted": restored["deleted"], "fact": fact_payload(restored["fact"]),
    })
    assert reply.undone_event_type == "expire" and not reply.deleted
    assert reply.fact.fact_text.text == "Temporary note."

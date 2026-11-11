"""Readers outside storage get only the public views of a pinned-memory fact.

A stored fact records where it came from: source chat (on Signal, a phone
number), session key, conversation, task, creator id and display name, and an
internal UUID. Manager-control replies, agent memory tools and memory reviews
used to receive the whole row; only Pydantic silently dropping undeclared keys
kept provenance out of the Settings reply, and agent tools returned all of it to
the model. ``public_pinned_memory_fact`` (agents, reviews: with metadata) and
``team_services.memory.wire_fact`` (people's ``memory.*`` replies: without) now
build the views field by field.
The agent view also carries ``source_key``, the key trusted imports use to stay
idempotent (an importer name, not a person or chat).
"""

from __future__ import annotations

from tests.memory_support import SYSTEM_CHANGE_WRITER
import json
from pathlib import Path

import pytest

from custom_agents.pinned_memory_runtime import (
    COREMEM_BANK,
    USERMEM_BANK,
    PinnedMemoryActor,
    PinnedMemoryCommand,
    PinnedMemoryRequestContext,
    PinnedMemoryService,
    PinnedMemorySource,
    public_pinned_memory_fact,
)
from custom_messages.contracts.control_v1 import PinnedMemoryFactPayload
from team_services.memory import wire_fact
from storage.config import SqliteStorageSettings, StorageSettings
from storage.pinned_memory_repository import create_pinned_memory_repository

PHONE = "+15555550123"
SESSION = "session_private_key"
CREATOR = "creator_person_id"
CREATOR_NAME = "Creator Display Name"
SOURCE_TASK = "task_source_private"
SOURCE_CONVERSATION = "conv_source_private"
PROVENANCE_VALUES = (
    PHONE,
    SESSION,
    CREATOR,
    CREATOR_NAME,
    SOURCE_TASK,
    SOURCE_CONVERSATION,
)
AGENT_FIELDS = set(PinnedMemoryFactPayload.model_fields) | {"metadata", "source_key"}
# A people's reply fact (the contract's memory.* replies); "expires" only when one is set.
WIRE_FIELDS = {"memory_id", "fact_text", "valid_from", "tags", "created_at", "updated_at", "seen_version"}


def _service(tmp_path: Path) -> PinnedMemoryService:
    settings = StorageSettings(
        backend="sqlite",
        sqlite=SqliteStorageSettings(path=str(tmp_path / "manager.db")),
    )
    return PinnedMemoryService(create_pinned_memory_repository(settings))


def _context(bank: str) -> PinnedMemoryRequestContext:
    return PinnedMemoryRequestContext(
        writer=SYSTEM_CHANGE_WRITER,
        team_id="team_view",
        canonical_user_id="person_view" if bank == USERMEM_BANK else None,
        source=PinnedMemorySource(
            conversation_id=SOURCE_CONVERSATION,
            task_id=SOURCE_TASK,
            turn_index=3,
            gateway_session_key=SESSION,
            channel_name="signal",
            chat_id=PHONE,
            thread_id="thread_private",
        ),
        actor=PinnedMemoryActor(kind="user", id=CREATOR, display_name=CREATOR_NAME),
    )


def _facts(result: dict) -> list[dict]:
    return [fact for fact in [result.get("fact"), *(result.get("facts") or [])] if fact]


def _assert_public(result: dict) -> None:
    text = json.dumps(result, default=str)
    for value in PROVENANCE_VALUES:
        assert value not in text
    for fact in _facts(result):
        assert set(fact) == AGENT_FIELDS
    for fact in _facts(result):
        wire = wire_fact(fact)
        assert set(wire) - {"expires"} == WIRE_FIELDS
        for value in PROVENANCE_VALUES:
            assert value not in json.dumps(wire)


@pytest.mark.parametrize("bank", [COREMEM_BANK, USERMEM_BANK])
def test_every_service_result_shows_only_the_public_view(
    tmp_path: Path, bank: str
) -> None:
    service = _service(tmp_path)
    context = _context(bank)

    def run(action: str, **values):
        return service.execute(
            PinnedMemoryCommand(bank=bank, action=action, **values), context=context
        )

    created = run(
        "create",
        fact_text="Onboarding guide progress: completed through step_2.",
        tags=["onboarding_guide"],
        metadata={
            "kind": "onboarding_guide_progress",
            "resume_section": "step_3",
            "imported_from": {
                "created_by_id": CREATOR,
                "created_by_display_name": CREATOR_NAME,
            },
        },
    )
    memory_id = created["memory_id"]
    replaced = run(
        "replace",
        memory_id=memory_id,
        fact_text="Onboarding guide progress: completed through step_3.",
    )
    shown = run("show", memory_id=memory_id)
    listed = run("list")
    undone = run("undo", event_id=replaced["event_id"])
    deleted = run("delete", memory_id=memory_id)

    for result in (created, replaced, shown, listed, undone, deleted):
        _assert_public(result)
    assert shown["fact"]["fact_text"].endswith("step_3.")
    assert undone["fact"]["fact_text"].endswith("step_2.")
    assert listed["facts"][0]["record_digest"] == shown["fact"]["record_digest"]
    # Agents read back what they attached (the onboarding guide resumes from it).
    assert listed["facts"][0]["metadata"]["resume_section"] == "step_3"
    assert "metadata_json" not in wire_fact(listed["facts"][0])
    # Delete moves the original metadata into its undo snapshot without exposing it.
    assert run("list")["facts"] == []
    restored = run("undo", event_id=deleted["event_id"])
    _assert_public(restored)
    rows = (
        service.repository.list_coremem(team_id="team_view")
        if bank == COREMEM_BANK
        else service.repository.list_usermem(canonical_user_id="person_view")
    )
    row = next(row for row in rows if row.memory_id == memory_id)
    assert row.metadata["imported_from"]["created_by_id"] == CREATOR


def test_the_public_view_is_built_from_named_fields_only(tmp_path: Path) -> None:
    service = _service(tmp_path)
    context = _context(USERMEM_BANK)
    service.execute(
        PinnedMemoryCommand(
            bank=USERMEM_BANK, action="create", fact_text="Lives in UTC+1."
        ),
        context=context,
    )
    row = service.repository.list_usermem(canonical_user_id="person_view")[0]
    # The stored row keeps its provenance; the view leaves it in storage.
    assert row.source_chat_id == PHONE and row.created_by_display_name == CREATOR_NAME

    view = public_pinned_memory_fact(row)

    assert set(view) == AGENT_FIELDS
    assert view["fact_text"] == "Lives in UTC+1." and view["memory_id"] == row.memory_id
    for value in PROVENANCE_VALUES:
        assert value not in json.dumps(view)

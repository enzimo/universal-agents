from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from custom_agents.deferred_message_runtime import (
    DeferredInboxFull,
    DeferredInboxOwner,
    DeferredMessageInbox,
)


from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from storage.runtime_state_repository import (
    RuntimeRecoveryConflict,
    SqliteRuntimeStateRepository,
)


@pytest.mark.asyncio
async def test_capacity_preserves_durable_custody_and_allows_duplicate_ack(inbox):
    inbox.capacity = 1
    first = _envelope("first")
    await inbox.admit(first)
    before = inbox.repository.load_deferred_messages_by_owner("worker")
    await inbox.admit(first)
    with pytest.raises(DeferredInboxFull):
        await inbox.admit(_envelope("second"))
    assert len(inbox) == 1
    assert inbox.repository.load_deferred_messages_by_owner("worker") == before


@pytest.mark.asyncio
async def test_lowered_recovery_limit_preserves_existing_custody_but_rejects_transfer(
    inbox, caplog
):
    await inbox.admit(_envelope("first"))
    await inbox.admit(_envelope("second"))
    sources = inbox.repository.load_deferred_messages_by_owner("worker")
    replacement = DeferredMessageInbox(
        owner=DeferredInboxOwner("replacement", "Worker", "team", "Worker"),
        repository=inbox.repository,
        is_execution_cancelled=AsyncMock(return_value=False),
        logger=inbox.logger,
        capacity=1,
    )
    with pytest.raises(DeferredInboxFull, match="source custody retained"):
        await replacement.restore_rows(sources)
    assert len(replacement) == 0
    assert inbox.repository.load_deferred_messages_by_owner("worker") == sources
    inbox.capacity = 1
    with caplog.at_level(logging.WARNING):
        assert await inbox.restore()
    assert len(inbox) == 2
    assert any(
        getattr(record, "event", None) == "deferred_restore_above_capacity"
        for record in caplog.records
    )
    with pytest.raises(DeferredInboxFull):
        await inbox.admit(_envelope("third"))


@pytest.fixture
def inbox(tmp_path):
    return DeferredMessageInbox(
        owner=DeferredInboxOwner("worker", "Worker", "team", "Worker"),
        repository=SqliteRuntimeStateRepository(str(tmp_path / "runtime.db")),
        is_execution_cancelled=AsyncMock(return_value=False),
        logger=logging.getLogger("test.deferred_inbox"),
    )


def _envelope(task_id="task_test"):
    return MessageEnvelope(
        msg_content=Task(
            task_id=task_id,
            task_type=MessageMetaType.REQUEST,
            problem_statement="durable work",
        ),
        msg_header=MessageHeader(from_agent_id="sender", to_routing_path="worker"),
        msg_meta=MessageMeta(),
    )


@pytest.mark.asyncio
async def test_cancelled_dispatch_keeps_the_durable_message(inbox):
    await inbox.admit(_envelope())
    with pytest.raises(asyncio.CancelledError):
        await inbox.drain(
            dispatch=AsyncMock(side_effect=asyncio.CancelledError),
            available=lambda: True,
        )
    assert len(inbox) == 1
    assert len(inbox.repository.load_deferred_messages_by_owner("worker")) == 1


@pytest.mark.asyncio
async def test_only_the_committed_replacement_exposes_recovered_messages(inbox):
    await inbox.admit(_envelope())
    sources = inbox.repository.load_deferred_messages_by_owner("worker")
    replacements = [
        DeferredMessageInbox(
            owner=DeferredInboxOwner(
                f"replacement-{index}", "Worker", "team", "Worker"
            ),
            repository=inbox.repository,
            is_execution_cancelled=AsyncMock(return_value=False),
            logger=inbox.logger,
        )
        for index in range(2)
    ]
    assert await replacements[0].restore_rows(sources)
    with pytest.raises(RuntimeRecoveryConflict):
        await replacements[1].restore_rows(sources)
    assert len(replacements[1]) == 0
    assert inbox.repository.load_deferred_messages_by_owner("worker") == []
    assert len(inbox.repository.load_deferred_messages_by_owner("replacement-0")) == 1


@pytest.mark.asyncio
async def test_malformed_recovery_keeps_its_original_durable_owner(inbox):
    inbox.repository.save_deferred_messages(
        owner_agent_id="worker",
        owner_agent_name="Worker",
        team_id="team",
        owner_named_agent_type="Worker",
        messages_json='{"not":"a message list"}',
    )
    with pytest.raises(ValueError, match="message list"):
        await inbox.restore()
    assert len(inbox) == 0
    assert len(inbox.repository.load_deferred_messages_by_owner("worker")) == 1

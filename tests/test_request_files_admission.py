"""A request's files at admission: the task and its turn keep the same files, and dropped ones are recorded.

A top-level request goes through one admission rule
(``conversation_attachments.admit_request_files``): its files stay only when
the gateway vouches it sent them for this request and each lies in this
host's upload folders. Before this change the two checks ran apart: a record
outside the upload folders stayed among the task's own files (listed, sent
with a delegation that names no ids, refused when opened) while its turn
dropped it, and a turn whose request files were dropped was saved as "request
files known, none", so nothing told the model to ask for them again (review
FAIL-2, DATA-1 and CRIT-1).

Each test admits Ann's request through the real admission path
(``task_message_runtime._handle_task_request``) with the task manager and the
conversation store on SQLite and on PostgreSQL (``LIVE_TEST_POSTGRES_DSN``, a
schema of its own per test), and reads the stored records back. Covers:

- a request the gateway signed before the update (its context names no task)
  keeps no file, and its turn is saved as not knowing its request's files;
- of a request's two files, one outside the upload folders is dropped from
  the task, its turn, its own files and a default delegation alike;
- the count of dropped files is saved with the task when it is admitted, so it
  is there before any turn runs and after a restart;
- a request whose files are all kept is marked as knowing them, with no count;
- a count entry a request arrives with is dropped, as only admission writes it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from access_control.identity import ENV_VERIFY_KEYS
from custom_agents import task_message_runtime
from custom_agents.conversation_attachments import (
    REQUEST_FILES_NOT_KEPT_EVENT,
    REQUEST_FILES_NOT_KEPT_KEY,
    TaskAttachments,
    delegated_files,
)
from custom_agents.conversation_store import ConversationStore
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskHistoryEntry
from custom_messages.enums import MessageMetaType, RequesterType
from runtime_settings.instance_layout import InstanceLayout
from storage.config import StorageSettings, resolve_storage_settings
from storage.postgres_connection import connect_postgres
from tests.broker_access_support import context_signer, context_verify_keys, forget_task_context_keys
from tests.test_conversation_attachments_regression import ANN, CONVERSATION, SESSION, TEAM, _admit
from tests.test_turn_files_recording import _outside, _upload

pytestmark = pytest.mark.integration

TASK = "task_photo"
FOLLOW_UP = "task_follow_up"


@pytest.fixture(autouse=True)
def _verify_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV_VERIFY_KEYS, context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


@pytest.fixture(autouse=True)
def _no_background_work(monkeypatch: pytest.MonkeyPatch):
    # No turn runs: the tests read what admission stored.
    monkeypatch.setattr(task_message_runtime, "schedule_pending_work", lambda _agent: None)


@pytest.fixture
def layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    return layout


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres]),
    ]
)
def storage(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[StorageSettings]:
    """The storage the task manager and the conversation store share: SQLite, or a fresh PostgreSQL schema."""

    if request.param == "sqlite":
        yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "tasks.db")}})
        return
    from psycopg.conninfo import make_conninfo

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a disposable database to run the PostgreSQL admission tests.")
    schema = "request_files_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def _task_manager(tmp_path: Path, storage: StorageSettings) -> TaskManager:
    tasks = TaskManager(db_path=str(tmp_path / "tasks.db"), storage_settings=storage)
    tasks.set_owner_identity("lead", "Lead", TEAM, "TeamArchitect")
    return tasks


def _lead(tmp_path: Path, layout: InstanceLayout, storage: StorageSettings) -> Any:
    """The team lead as the admission path sees it, with both stores on ``storage``."""

    from tests.test_turn_action_outbox import _make_agent

    agent = _make_agent(tmp_path, task_manager=_task_manager(tmp_path, storage), instance_root=str(layout.root))
    agent._conversation_store = ConversationStore(
        str(tmp_path / "tasks.db"), artifacts_dir=tmp_path / "conversation_artifacts", storage_settings=storage
    )
    return agent


async def _admit_signed_before_the_update(agent: Any, attachments: list[Any]) -> None:
    """Ann's request as a gateway from before the update signed it: team, person and roles, no task."""

    task = Task(task_id=TASK, task_type=MessageMetaType.REQUEST, problem_statement="What is in it?", attachments=attachments)
    signed = context_signer().issue(team_id=TEAM, person_id=ANN, person_roles=("member",))
    meta = MessageMeta(
        conversation_id=CONVERSATION,
        msg_correlation_id="corr_queued",
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=GatewayContext(canonical_user_id=ANN, session_key=SESSION, task_context=signed),
    )
    await task_message_runtime._handle_task_request(
        agent, task, MessageHeader(from_agent_id="gateway", response_routing_key="gateway_route"), meta
    )


def _not_kept_counts(task: Task) -> list[int]:
    return [
        entry.details[REQUEST_FILES_NOT_KEPT_KEY]
        for entry in task.execution_history
        if entry.details.get("event") == REQUEST_FILES_NOT_KEPT_EVENT
    ]


def _restored(tmp_path: Path, storage: StorageSettings, task_id: str) -> Task:
    """The task as a restarted lead reads it back: what admission saved, before any turn ran."""

    tasks = _task_manager(tmp_path, storage)
    assert tasks.restore_runtime_state() is not None
    return tasks.get_task_info(task_id).task


def _turn(agent: Any, task_id: str = TASK) -> dict[str, Any]:
    return agent._conversation_store.get_turn_by_task_id(task_id)


@pytest.mark.asyncio
async def test_a_request_signed_before_the_update_keeps_no_file_and_its_turn_does_not_know_them(
    tmp_path: Path, layout: InstanceLayout, storage: StorageSettings, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _lead(tmp_path, layout, storage)
    photo = _upload(layout, "photo.png")

    with caplog.at_level(logging.INFO, logger="custom_agents.conversation_attachments"):
        await _admit_signed_before_the_update(agent, [photo])

    info = agent.task_manager.get_task_info(TASK)
    assert info.owner_person_id == ANN  # the request itself is admitted
    assert info.task.attachments == []
    turn = _turn(agent)
    assert (turn["attachments"], turn["attachments_recorded"]) == ([], False)
    # The count is saved with the task when it is admitted, before any turn runs.
    assert _not_kept_counts(info.task) == [1]
    assert _not_kept_counts(_restored(tmp_path, storage, TASK)) == [1]
    assert any(f"event=request_files_not_kept task_id={TASK}" in record.getMessage() for record in caplog.records)
    # A later request in the conversation finds that turn among those whose request files are not all known.
    await _admit(agent, task_id=FOLLOW_UP, text="What was in the photo?", attachments=[])
    later = TaskAttachments.load(agent.task_manager.get_task_info(FOLLOW_UP), agent._conversation_store)
    assert later.last_unrecorded_turn() == turn["turn_index"]
    assert later.find([photo.attachment_id]) == ([], [photo.attachment_id])


@pytest.mark.asyncio
async def test_a_record_outside_the_upload_folders_is_dropped_from_the_task_and_its_turn_alike(
    tmp_path: Path, layout: InstanceLayout, storage: StorageSettings
) -> None:
    """CRIT-1: the gateway vouches for a record whose path lies outside the folders the agent trusts."""

    agent = _lead(tmp_path, layout, storage)
    page = _upload(layout, "page one.png")

    await _admit(agent, task_id=TASK, text="Read this recipe.", attachments=[page, _outside(layout)])

    info = agent.task_manager.get_task_info(TASK)
    assert info.task.attachments == [page]
    turn = _turn(agent)
    assert [item["attachment_id"] for item in turn["attachments"]] == [page.attachment_id]
    assert turn["attachments_recorded"] is False
    scope = TaskAttachments.load(info, agent._conversation_store)
    assert scope.own == (page,)
    assert delegated_files(scope, None) == [page]  # a delegation naming no ids sends only the kept file
    assert _not_kept_counts(_restored(tmp_path, storage, TASK)) == [1]


@pytest.mark.asyncio
async def test_a_request_whose_files_are_all_kept_knows_them(
    tmp_path: Path, layout: InstanceLayout, storage: StorageSettings
) -> None:
    agent = _lead(tmp_path, layout, storage)
    page = _upload(layout, "page one.png")

    await _admit(agent, task_id=TASK, text="Read this recipe.", attachments=[page])

    info = agent.task_manager.get_task_info(TASK)
    assert info.task.attachments == [page]
    turn = _turn(agent)
    assert ([item["attachment_id"] for item in turn["attachments"]], turn["attachments_recorded"]) == (
        [page.attachment_id],
        True,
    )
    assert _not_kept_counts(info.task) == []


@pytest.mark.asyncio
async def test_a_count_entry_a_request_arrives_with_is_dropped(
    tmp_path: Path, layout: InstanceLayout, caplog: pytest.LogCaptureFixture
) -> None:
    """Only admission writes the count of a request's dropped files; a sender cannot add one."""

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "tasks.db")}})
    agent = _lead(tmp_path, layout, storage)
    forged = TaskHistoryEntry(
        summary="2 file(s) sent with this request were not accepted.",
        details={"event": REQUEST_FILES_NOT_KEPT_EVENT, REQUEST_FILES_NOT_KEPT_KEY: 2},
    )
    task = Task(task_id=TASK, task_type=MessageMetaType.REQUEST, problem_statement="Hello.", execution_history=[forged])

    with caplog.at_level(logging.WARNING, logger="custom_agents.conversation_attachments"):
        await task_message_runtime._handle_task_request(
            agent,
            task,
            MessageHeader(from_agent_id="worker", response_routing_key="worker_route"),
            MessageMeta(conversation_id=CONVERSATION, msg_correlation_id="corr_forged"),
        )

    assert _not_kept_counts(agent.task_manager.get_task_info(TASK).task) == []
    assert _turn(agent)["attachments_recorded"] is True
    assert any("event=incoming_task_files_entries_dropped" in record.getMessage() for record in caplog.records)

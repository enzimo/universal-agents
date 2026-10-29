"""A model's schedule action only creates, and schedule edits need the version the model read.

Real SQLite and PostgreSQL schedule stores, the real turn-action outbox of a
TeamArchitect and the real schedule tools. Before this change:

- the model's ``schedule_message`` action accepted a ``schedule_id`` and saved
  over the stored schedule with that id, owner included, so one person's
  request could replace another person's schedule (a worker's model could
  send it too);
- the schedule tools started from the schedule as it was when the call ran,
  so a change a model based on an earlier read silently overwrote a newer
  change another request had made meanwhile.

Now the action always creates a new schedule under an id the runtime assigns,
and never changes a stored one. The schedule tools return each schedule's
``record_digest`` (``schedule_listing_digest``) and update, pause, resume,
disable and remove require it back as ``expected_record_digest``; a schedule
that changed since is refused with ``changed_since_read``. The digest leaves
out what the scheduler changes on its own (a repeating schedule's next run
time, run notes, claims and the remaining count), so runs never make a read
stale.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from custom_agents.conversation_store import ConversationStore
from custom_agents.schedule_store import AdvanceOutcome, ScheduleStore, schedule_record_digest
from custom_agents.structured_llm_messages import (
    CompatibleModelAgentTurnDecision,
    CompatibleWorkerTurnDecision,
    FireworksModelAgentTurnDecision,
    ModelAgentTurnDecision,
    ModelScheduledMessagePayload,
    ModelWorkerTurnDecision,
)
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    ChangeRefusal,
    MessageMetaType,
    ProgressState,
    RequesterType,
    ScheduleManagementClass,
)
from custom_tools.providers.schedules import (
    create_scheduled_job,
    disable_scheduled_job,
    list_scheduled_jobs,
    pause_scheduled_job,
    record_scheduled_run,
    remove_scheduled_job,
    resume_scheduled_job,
    update_scheduled_job,
)
from tests.task_work_support import set_agent_ref
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts
from tests.tool_result_support import tool_json

TEAM = "team_1"
PERSON_A = "person_a"
PERSON_B = "person_b"
HOUR = timedelta(hours=1)


@pytest.fixture(autouse=True)
def _signed_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    use_signed_contexts(monkeypatch)
    import custom_tools.registry  # noqa: F401  (the trusted tool registry the tools normalize against)


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres]),
    ]
)
def storage(tmp_path: Path, request: pytest.FixtureRequest):
    """The schedule store's settings: SQLite, or a fresh schema in a disposable PostgreSQL database."""

    if request.param == "sqlite":
        yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "schedules.db")}})
        return
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set LIVE_TEST_POSTGRES_DSN to a disposable database to run the PostgreSQL schedule edit checks.")
    schema = "schedule_edits_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield resolve_storage_settings(
            {"backend": "postgres", "postgres": {"dsn": make_conninfo(dsn, options=f"-csearch_path={schema}")}}
        )
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def store(storage) -> ScheduleStore:
    return ScheduleStore(storage_settings=storage, default_cron_timezone="UTC")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _person_schedule(owner: str, instruction: str, **changes: Any) -> ScheduledMessage:
    fields: dict[str, Any] = dict(
        management_class=ScheduleManagementClass.USER,
        payload=Task(task_type=MessageMetaType.REQUEST, problem_statement=instruction),
        payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=owner)),
        interval_ms=3_600_000,
        repeat_count=None,
        next_fire_at=(_now() + HOUR).isoformat(),
        claim_team_id=TEAM,
        claim_named_agent_type="TeamArchitect",
    )
    fields.update(changes)
    return ScheduledMessage(**fields)


# -- the model's schedule_message action only creates ----------------------------------


def _lead(tmp_path: Path, store: ScheduleStore):
    """A TeamArchitect with a real task manager and outbox; its model is never called."""

    from custom_agents.strands_agent import UniversalAgent

    manager = TaskManager(db_path=str(tmp_path / "manager.db"), logger=logging.getLogger("test.schedule_edits"))
    manager.set_owner_identity("agent_lead", agent_name="lead", team_id=TEAM, named_agent_type="TeamArchitect")
    with (
        patch("custom_agents.strands_agent.Agent"),
        patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
    ):
        agent = UniversalAgent(
            role="Manager",
            tools=[],
            team_id=TEAM,
            named_agent_type="TeamArchitect",
            is_team_lead=True,
            task_manager=manager,
            id="agent_lead",
        )
    agent.team_comms = SimpleNamespace(send_message=AsyncMock(return_value=1))
    agent._schedule_store = store
    return agent


def _persons_running_task(agent: Any, person: str) -> tuple[Task, MessageHeader, MessageMeta]:
    task = Task(task_id=f"task_{person}", task_type=MessageMetaType.REQUEST, problem_statement="Set a reminder.")
    header = MessageHeader(from_agent_id="gateway", response_routing_key="gateway")
    meta = MessageMeta(
        from_agent_name=person,
        requester_type=RequesterType.USER_REQUESTER,
        is_response_requested=True,
        msg_correlation_id=f"corr_{person}",
        conversation_id=f"conv_{person}",
        gateway_context=GatewayContext(canonical_user_id=person, session_key=f"session_{person}"),
    )
    manager = agent.task_manager
    assert manager.add_task(task, header, meta, owner_person_id=None)
    assert manager.get_next_task_to_process() == task.task_id
    assert manager.update_main_task_state(task.task_id, ProgressState.IN_PROGRESS, _now().isoformat())
    return task, header, meta


def _schedule_action(**scheduled_message: Any) -> dict[str, Any]:
    return {
        "actions": [
            {
                "action_type": "schedule_message",
                "scheduled_message": {
                    "payload": {"problem_statement": "Remind person B to call the bank."},
                    "interval_ms": 60_000,
                    **scheduled_message,
                },
            },
            {"action_type": "task_response", "task": {"answer": "Saved the reminder."}},
        ]
    }


def _owned_by(store: ScheduleStore, person: str) -> list[ScheduledMessage]:
    rows = [row for row in store.get_all() if row.get("owner_canonical_user_id") == person]
    return [store.get_scheduled_message(row["schedule_id"]) for row in rows]


@pytest.mark.parametrize(
    "decision_model",
    [
        ModelAgentTurnDecision,
        ModelWorkerTurnDecision,
        CompatibleModelAgentTurnDecision,
        CompatibleWorkerTurnDecision,
        FireworksModelAgentTurnDecision,
    ],
)
def test_the_model_schedule_action_cannot_name_a_schedule(decision_model: type) -> None:
    """Every turn answer a lead or a worker gives refuses a schedule id; the schema offers none."""

    assert "schedule_id" not in ModelScheduledMessagePayload.model_json_schema()["properties"]
    with pytest.raises(ValidationError, match="schedule_id"):
        decision_model.model_validate(_schedule_action(schedule_id="sched_person_a"))


def test_a_schedule_action_never_replaces_a_stored_schedule(tmp_path: Path, store: ScheduleStore, caplog) -> None:
    """Even a runtime action that carries another person's schedule id leaves that schedule as it is.

    The model can no longer give an id, so this action reaches the outbox only
    through the runtime's own decision forms; the outbox still never saves over
    a stored schedule.
    """

    agent = _lead(tmp_path, store)
    schedule_a = _person_schedule(PERSON_A, "Remind person A to water the plants.")
    store.add(schedule_a)
    before = schedule_record_digest(store.get_scheduled_message(schedule_a.schedule_id))
    task, header, meta = _persons_running_task(agent, PERSON_B)
    runtime_form = _schedule_action(schedule_id=schedule_a.schedule_id)
    runtime_form["actions"][0]["scheduled_message"]["payload"]["task_type"] = "request"
    runtime_form["actions"][1]["task"]["task_type"] = "response"
    decision = agent._turn_decision_from_structured_output(runtime_form)

    with caplog.at_level(logging.WARNING):
        asyncio.run(
            agent._execute_turn_decision_outbox(
                decision=decision, incoming_content=task, incoming_header=header, incoming_meta=meta,
            )
        )

    kept = store.get_scheduled_message(schedule_a.schedule_id)
    assert kept.payload.problem_statement == "Remind person A to water the plants."
    assert store.get_row(schedule_a.schedule_id)["owner_canonical_user_id"] == PERSON_A
    assert schedule_record_digest(kept) == before
    assert _owned_by(store, PERSON_B) == []
    assert "event=schedule_action_id_in_use" in caplog.text


def test_the_model_schedule_action_creates_a_new_schedule_once(tmp_path: Path, store: ScheduleStore) -> None:
    """The runtime names the new schedule before the outbox saves the action, so a retry creates no second one."""

    agent = _lead(tmp_path, store)
    schedule_a = _person_schedule(PERSON_A, "Remind person A to water the plants.")
    store.add(schedule_a)
    task, header, meta = _persons_running_task(agent, PERSON_B)
    # The lead asks the person something after saving, so the task stays open
    # and its saved actions stay available to run again.
    model_output = _schedule_action()
    model_output["actions"][1] = {
        "action_type": "task_query",
        "task_query": {"query_type": "request", "message": "Should I also remind you by email?"},
    }
    decision = agent._turn_decision_from_structured_output(ModelAgentTurnDecision.model_validate(model_output))
    assigned = decision.actions[0].scheduled_message.schedule_id
    assert assigned and assigned.startswith("sched_")
    batches: list[Any] = []
    save_batch = agent.task_manager.create_turn_action_batch
    agent.task_manager.create_turn_action_batch = lambda *args, **kwargs: batches.append(save_batch(*args, **kwargs)) or batches[-1]

    asyncio.run(
        agent._execute_turn_decision_outbox(
            decision=decision, incoming_content=task, incoming_header=header, incoming_meta=meta,
        )
    )
    created = _owned_by(store, PERSON_B)
    assert [schedule.schedule_id for schedule in created] == [assigned]
    assert created[0].payload.problem_statement == "Remind person B to call the bank."
    assert store.get_scheduled_message(schedule_a.schedule_id).payload.problem_statement.startswith("Remind person A")

    # The outbox runs a saved action again after a restart or a failed later step.
    [record] = [
        record
        for record in agent.task_manager.list_turn_action_records(batches[0].batch_id)
        if record.action_type == "schedule_message"
    ]
    replayed = agent._turn_action_from_outbox_record(record)
    assert replayed.scheduled_message.schedule_id == assigned
    asyncio.run(
        agent._execute_turn_action_record(
            record_id=record.action_id, action=replayed, incoming_content=task,
            incoming_header=header, incoming_meta=meta,
        )
    )
    assert [schedule.schedule_id for schedule in _owned_by(store, PERSON_B)] == [assigned]


# -- the schedule tools need the version the model read --------------------------------


class _ToolTaskManager:
    def __init__(self, storage_settings: Any, task_info: Any) -> None:
        from custom_agents.task_operation_runtime import TaskOperationRuntime

        self.operations_runtime = TaskOperationRuntime()
        self.storage_settings = storage_settings
        self.current_task_id = "task_current"
        self._task_info = task_info

    def get_task_info(self, task_id: str):
        return self._task_info if task_id == "task_current" else None

    def append_history_entry(self, *args: Any, **kwargs: Any) -> None:
        return None

    def get_task_objective_id(self, task_id: str) -> None:
        return None  # the request is not part of an Objective


class _ToolAgent:
    """TeamArchitect working on one person's gateway-signed request (one request of that person)."""

    def __init__(self, tmp_path: Path, store: ScheduleStore, person: str = PERSON_A) -> None:
        context = GatewayContext(
            canonical_user_id=person,
            user_id=person,
            session_key=f"session_{person}",
            chat_id=f"chat_{person}",
            channel_name="telegram",
            task_context=context_signer().issue(team_id=TEAM, person_id=person, person_roles=("owner",)),
        )
        task_info = SimpleNamespace(
            header=MessageHeader(from_agent_id="gateway", to_routing_path="agent_lead", response_routing_key="gateway", agent_signature=""),
            meta=MessageMeta(
                from_agent_name=person,
                conversation_id=f"conv_{person}",
                requester_type=RequesterType.USER_REQUESTER,
                gateway_context=context,
            ),
            task=Task(task_id="task_current", task_type=MessageMetaType.REQUEST, problem_statement="Schedules"),
        )
        self.task_manager = _ToolTaskManager(store._storage_settings, task_info)
        # The tool calls below are this request's own turn.
        self.test_task_id = "task_current"
        self._schedule_store = store
        self._conversation_store = ConversationStore(
            db_path=str(tmp_path / "conversations.db"), artifacts_dir=tmp_path / "conversation_artifacts"
        )
        self._scheduler_wake = None
        self.id = "agent_lead"
        self.name = "lead"
        self.team_id = TEAM
        # TeamArchitect's own changes are not reported to itself.
        self.named_agent_type = "TeamArchitect"
        self.is_team_lead = True
        self.authorization_config = {"enabled": False}
        self._logger = logging.getLogger("test.schedule_edits.tools")


def _call(agent: _ToolAgent, tool: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    set_agent_ref(agent)
    return tool_json(tool(*args, **kwargs))


def _listed(agent: _ToolAgent, schedule_id: str) -> dict[str, Any]:
    listed = _call(agent, list_scheduled_jobs, schedule_id=schedule_id)
    assert listed["returned"] == 1, listed
    return listed["schedules"][0]


_UNTIL = (_now() + timedelta(days=1)).replace(microsecond=0).isoformat()
CHANGES = {
    "update": (update_scheduled_job, {"repeat_count": 3}, "updated"),
    "pause": (pause_scheduled_job, {"until_iso": _UNTIL}, "paused"),
    "resume": (resume_scheduled_job, {}, "resumed"),
    "disable": (disable_scheduled_job, {}, "disabled"),
    "remove": (remove_scheduled_job, {}, "removed"),
}


@pytest.mark.parametrize("action", sorted(CHANGES))
def test_a_change_based_on_an_old_read_is_refused(tmp_path: Path, store: ScheduleStore, action: str) -> None:
    """Request 1 read the schedule, request 2 changed it, then request 1 sends its change: refused."""

    request_1 = _ToolAgent(tmp_path, store)
    request_2 = _ToolAgent(tmp_path, store)
    schedule = _person_schedule(PERSON_A, "Check the feed hourly.")
    store.add(schedule)
    read_by_request_1 = _listed(request_1, schedule.schedule_id)["record_digest"]

    newer = _call(
        request_2, update_scheduled_job, schedule.schedule_id,
        expected_record_digest=read_by_request_1, instruction="Check the feed hourly and email me.",
    )
    assert newer["updated"] is True, newer

    tool, arguments, flag = CHANGES[action]
    set_agent_ref(request_1)
    raw = tool(schedule.schedule_id, expected_record_digest=read_by_request_1, **arguments)
    refused = tool_json(raw)

    assert raw["status"] == "error"
    assert refused[flag] is False and refused["reason_code"] == ChangeRefusal.CHANGED_SINCE_READ
    assert "list_scheduled_jobs" in refused["error"]
    kept = store.get_scheduled_message(schedule.schedule_id)
    assert kept is not None and kept.payload.problem_statement == "Check the feed hourly and email me."
    assert (kept.enabled, kept.paused_until, kept.repeat_count) == (True, None, None)


def test_changes_based_on_a_fresh_read_apply_and_return_the_next_version(tmp_path: Path, store: ScheduleStore) -> None:
    """Each change returns the saved schedule's record_digest, which the next change uses."""

    agent = _ToolAgent(tmp_path, store)
    created = _call(agent, create_scheduled_job, instruction="Check the feed hourly.", interval_seconds=3600)
    assert "schedule_id" in created, created
    schedule_id = created["schedule_id"]
    assert created["record_digest"] == _listed(agent, schedule_id)["record_digest"]

    updated = _call(
        agent, update_scheduled_job, schedule_id,
        expected_record_digest=created["record_digest"], instruction="Check the feed every hour.",
    )
    paused = _call(agent, pause_scheduled_job, schedule_id, expected_record_digest=updated["record_digest"], until_iso=_UNTIL)
    resumed = _call(agent, resume_scheduled_job, schedule_id, expected_record_digest=paused["record_digest"])
    disabled = _call(agent, disable_scheduled_job, schedule_id, expected_record_digest=resumed["record_digest"])
    assert (updated["updated"], paused["paused"], resumed["resumed"], disabled["disabled"]) == (True, True, True, True)
    assert disabled["record_digest"] == _listed(agent, schedule_id)["record_digest"]

    removed = _call(agent, remove_scheduled_job, schedule_id, expected_record_digest=disabled["record_digest"])
    assert removed["removed"] is True and "record_digest" not in removed
    assert store.get_scheduled_message(schedule_id) is None


def test_scheduler_runs_do_not_make_a_fresh_read_stale(tmp_path: Path, store: ScheduleStore) -> None:
    """Claims, run notes, a repeating schedule's next run time and the remaining count leave the version alone."""

    agent = _ToolAgent(tmp_path, store)
    schedule = _person_schedule(
        PERSON_A, "Check the feed hourly.", repeat_count=5, next_fire_at=(_now() - timedelta(seconds=5)).isoformat(),
    )
    store.add(schedule)
    digest = _listed(agent, schedule.schedule_id)["record_digest"]
    timing_revision = store.get_scheduled_message(schedule.schedule_id).timing_revision

    claimed = store.claim_next_due(claimer_agent_id="agent_lead", claimer_named_agent_type="TeamArchitect", claimer_team_id=TEAM)
    assert claimed is not None and claimed["schedule_id"] == schedule.schedule_id
    noted = _call(agent, record_scheduled_run, schedule.schedule_id, outcome="changed", state_summary="Two new posts.")
    assert noted["recorded"] is True, noted
    assert store.update_after_fire(
        schedule.schedule_id, claim_token=claimed["claim_token"], claimer_agent_id="agent_lead", due_at=claimed["next_fire_at"],
    ) == AdvanceOutcome.ADVANCED
    ran = store.get_row(schedule.schedule_id)
    assert ran["remaining_count"] == 4 and ran["next_fire_at"] != claimed["next_fire_at"]
    assert store.get_scheduled_message(schedule.schedule_id).timing_revision == timing_revision

    paused = _call(agent, pause_scheduled_job, schedule.schedule_id, expected_record_digest=digest, until_iso=_UNTIL)
    assert paused["paused"] is True, paused
    updated = _call(
        agent, update_scheduled_job, schedule.schedule_id,
        expected_record_digest=paused["record_digest"], instruction="Check the feed every hour.",
    )
    assert updated["updated"] is True, updated
    kept = store.get_scheduled_message(schedule.schedule_id)
    assert kept.schedule_memory.state.summary == "Two new posts."  # the run's notes stay


def test_an_old_read_cannot_overwrite_an_explicit_retime_of_a_repeating_schedule(tmp_path: Path, store: ScheduleStore) -> None:
    agent = _ToolAgent(tmp_path, store)
    schedule = _person_schedule(PERSON_A, "Check the feed hourly.")
    store.add(schedule)
    old_digest = _listed(agent, schedule.schedule_id)["record_digest"]
    first_time = (_now() + 2 * HOUR).replace(microsecond=0).isoformat()
    first = _call(
        agent, update_scheduled_job, schedule.schedule_id,
        expected_record_digest=old_digest, next_fire_at=first_time,
    )
    assert first["updated"] is True
    # The guard belongs to the saved schedule, not this process's memory.
    store = ScheduleStore(storage_settings=store._storage_settings, default_cron_timezone="UTC")
    agent._schedule_store = store
    assert store.get_scheduled_message(schedule.schedule_id).timing_revision == 1

    stale = _call(
        agent, update_scheduled_job, schedule.schedule_id,
        expected_record_digest=old_digest,
        next_fire_at=(_now() + 3 * HOUR).replace(microsecond=0).isoformat(),
    )

    assert stale.get("reason_code") == ChangeRefusal.CHANGED_SINCE_READ
    assert store.get_scheduled_message(schedule.schedule_id).next_fire_at == first_time


def test_an_authoritative_replacement_cannot_reset_the_saved_timing_revision(store: ScheduleStore) -> None:
    schedule = _person_schedule(PERSON_A, "Check the feed hourly.")
    store.add(schedule)
    old_digest = schedule_record_digest(store.get_scheduled_message(schedule.schedule_id))
    schedule.next_fire_at = (_now() + 2 * HOUR).isoformat()
    store.add(schedule)
    assert store.get_scheduled_message(schedule.schedule_id).timing_revision == 1
    # The install producer still holds revision zero, which cannot reset the stored revision.
    store.add(schedule)
    saved = store.get_scheduled_message(schedule.schedule_id)
    assert saved.timing_revision == 1
    assert schedule_record_digest(saved) != old_digest


def test_a_one_time_schedule_keeps_its_version_while_it_is_claimed(tmp_path: Path, store: ScheduleStore) -> None:
    """A claim before a one-time run changes nothing a person reads; a new run time does."""

    agent = _ToolAgent(tmp_path, store)
    schedule = _person_schedule(
        PERSON_A, "Call the dentist.", interval_ms=None, repeat_count=1,
        next_fire_at=(_now() - timedelta(seconds=5)).isoformat(),
    )
    store.add(schedule)
    digest = _listed(agent, schedule.schedule_id)["record_digest"]
    claimed = store.claim_next_due(claimer_agent_id="agent_lead", claimer_named_agent_type="TeamArchitect", claimer_team_id=TEAM)
    assert claimed is not None

    moved = _call(
        _ToolAgent(tmp_path, store), update_scheduled_job, schedule.schedule_id,
        expected_record_digest=digest, next_fire_at=(_now() + HOUR).replace(microsecond=0).isoformat(),
    )
    assert moved["updated"] is True, moved
    refused = _call(agent, disable_scheduled_job, schedule.schedule_id, expected_record_digest=digest)
    assert refused["reason_code"] == ChangeRefusal.CHANGED_SINCE_READ
    assert store.get_scheduled_message(schedule.schedule_id).enabled is True


@pytest.mark.parametrize("given", ["", "not-a-digest", "A" * 64])
def test_a_change_without_the_version_read_is_refused(tmp_path: Path, store: ScheduleStore, given: str) -> None:
    agent = _ToolAgent(tmp_path, store)
    schedule = _person_schedule(PERSON_A, "Check the feed hourly.")
    store.add(schedule)

    set_agent_ref(agent)
    raw = pause_scheduled_job(schedule.schedule_id, expected_record_digest=given, duration_minutes=30)

    assert raw["status"] == "error"
    assert "record_digest" in tool_json(raw)["error"]
    assert store.get_scheduled_message(schedule.schedule_id).paused_until is None


def test_another_persons_schedule_stays_not_found_with_its_version(tmp_path: Path, store: ScheduleStore) -> None:
    """Knowing a digest gives no way into another person's schedule."""

    schedule = _person_schedule(PERSON_B, "Person B's reminder.")
    store.add(schedule)
    digest = schedule_record_digest(store.get_scheduled_message(schedule.schedule_id))

    refused = _call(_ToolAgent(tmp_path, store, PERSON_A), remove_scheduled_job, schedule.schedule_id, expected_record_digest=digest)

    assert refused == {"schedule_id": schedule.schedule_id, "removed": False, "error": "schedule not found"}
    assert store.get_scheduled_message(schedule.schedule_id) is not None

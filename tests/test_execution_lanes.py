"""Requests sent in a parallel thread run in execution lanes of their own.

The team lead runs a request a person sent in a thread started with Web Chat's
Launch parallel thread beside its other work; everything else shares the main
lane, one turn at a time (custom_agents.execution_lanes,
runtime_hosting.agent_execution).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace

import pytest

from custom_agents.execution_lanes import (
    MAX_PARALLEL_LANES,
    executing_parallel_task_ids,
    is_parallel_request,
    lane_for_message,
    lane_for_task,
    parallel_lane,
)
from custom_agents import execution_lanes_runtime
from custom_agents.execution_lanes_runtime import ParallelLanes
from custom_agents.pending_work_runtime import schedule_parallel_lanes
from custom_agents.schedule_store import ScheduleStore
from custom_agents.task_manager import TaskManager
from custom_agents.task_queue_safety import queued_parallel_lanes
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import MessageMetaType, ProgressState, RequesterType
from custom_tools.providers.schedules import _build_schedule_payload_meta
from runtime_hosting.agent_execution import MAIN_LANE, AgentExecutionLanes, current_lane


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meta(*, parallel: bool, delivery_mode: str = "interactive") -> MessageMeta:
    return MessageMeta(
        conversation_id="conversation",
        gateway_context=GatewayContext(
            canonical_user_id="owner", delivery_mode=delivery_mode, parallel_thread=parallel
        ),
    )


def _manager(tmp_path, *, lead: bool = True) -> TaskManager:
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("lead", "Lead", "team", "TeamArchitect")
    manager.parallel_lanes_enabled = lead
    return manager


def _add(manager: TaskManager, task_id: str, *, parallel: bool, delivery_mode: str = "interactive") -> None:
    assert manager.add_task(
        Task(task_id=task_id, task_type=MessageMetaType.REQUEST, problem_statement=task_id),
        MessageHeader(from_agent_id="gateway"),
        _meta(parallel=parallel, delivery_mode=delivery_mode),
        owner_person_id="owner",
    )


# -- the lanes ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_lanes_run_at_once_and_one_lane_runs_one_owner_at_a_time():
    lanes = AgentExecutionLanes()
    inside: list[str] = []
    release = asyncio.Event()

    async def hold(lane: str, name: str) -> None:
        async with lanes.hold(lane):
            inside.append(name)
            assert current_lane() == lane
            await release.wait()

    main = asyncio.create_task(hold(MAIN_LANE, "main"))
    parallel = asyncio.create_task(hold("parallel:a", "parallel"))
    waiting = asyncio.create_task(hold("parallel:a", "same request"))
    await asyncio.sleep(0.01)

    assert inside == ["main", "parallel"]
    assert lanes.busy and lanes.main_busy and lanes.held_lanes() == [MAIN_LANE, "parallel:a"]
    release.set()
    await asyncio.gather(main, parallel, waiting)
    assert inside == ["main", "parallel", "same request"]
    assert not lanes.busy
    # A parallel lane's lock goes with its last user; the main one stays.
    assert set(lanes._locks) == {MAIN_LANE}


@pytest.mark.asyncio
async def test_an_owner_shares_its_lane_inline_but_never_takes_a_second_one():
    lanes = AgentExecutionLanes()
    async with lanes.hold("parallel:a"):
        async with lanes.hold("parallel:a"):
            assert lanes.owned_by_current_task("parallel:a")
        with pytest.raises(RuntimeError, match="one lane at a time"):
            async with lanes.hold(MAIN_LANE):
                pass
    assert not lanes.busy


@pytest.mark.asyncio
async def test_the_release_callback_names_the_lane():
    released = []
    lanes = AgentExecutionLanes(on_release=lambda owner, lane: released.append(lane))
    async with lanes.hold("parallel:a"):
        pass
    async with lanes.hold(MAIN_LANE):
        pass
    assert released == ["parallel:a", MAIN_LANE]


# -- which lane --------------------------------------------------------------------


def test_only_a_team_leads_interactive_request_from_a_parallel_thread_gets_a_lane(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "parallel", parallel=True)
    _add(manager, "ordinary", parallel=False)
    _add(manager, "maintenance", parallel=True, delivery_mode="autonomous")

    assert lane_for_task(manager, "parallel") == parallel_lane("parallel")
    assert lane_for_task(manager, "ordinary") == MAIN_LANE
    assert lane_for_task(manager, "maintenance") == MAIN_LANE, "only interactive requests"
    assert lane_for_task(manager, "unknown") == MAIN_LANE
    assert lane_for_task(_manager(tmp_path / "worker", lead=False), "parallel") == MAIN_LANE
    assert not is_parallel_request(_meta(parallel=False))


def test_each_run_of_a_schedule_made_in_a_parallel_thread_gets_a_lane_of_its_own(tmp_path):
    """Owner, 2026-10-28: work set up from a parallel thread runs in parallel too.

    The schedule keeps the thread's request context (``schedules._build_schedule_payload_meta``),
    so its runs count as parallel requests; a schedule made in another chat runs in the main lane.
    """

    manager = _manager(tmp_path)
    _add(manager, "parallel", parallel=True)
    _add(manager, "ordinary", parallel=False)
    lead = SimpleNamespace(id="lead")
    for source, parallel in (("parallel", True), ("ordinary", False)):
        header, meta = _build_schedule_payload_meta(lead, f"schedule-{source}", task_info=manager.get_task_info(source))
        assert meta.requester_type == RequesterType.TIMER_REQUESTER
        run_id = f"run-of-{source}"
        assert manager.add_task(
            Task(task_id=run_id, task_type=MessageMetaType.REQUEST, problem_statement="Scheduled run"),
            header,
            meta,
            owner_person_id="owner",
        )
        assert lane_for_task(manager, run_id) == (parallel_lane(run_id) if parallel else MAIN_LANE)


def test_a_schedule_set_up_in_a_parallel_thread_is_claimed_while_the_main_lane_is_busy(tmp_path):
    """Owner, 2026-10-29: such runs start independently of whatever else runs.

    The schedule row records that its runs are parallel requests
    (``schedule_store.runs_in_parallel``), so a claim limited to them takes it
    and leaves an earlier ordinary one for the main lane
    (``scheduled_runtime._schedule_claim_scope``).
    """

    manager = _manager(tmp_path)
    _add(manager, "parallel", parallel=True)
    _add(manager, "ordinary", parallel=False)
    store = ScheduleStore(db_path=str(tmp_path / "schedules.db"))
    lead = SimpleNamespace(id="lead")
    now = datetime.now(timezone.utc)
    due = {"ordinary": now - timedelta(minutes=2), "parallel": now - timedelta(minutes=1)}
    for source, due_at in due.items():
        schedule_id = f"schedule-{source}"
        header, meta = _build_schedule_payload_meta(lead, schedule_id, task_info=manager.get_task_info(source))
        store.add(
            ScheduledMessage(
                schedule_id=schedule_id,
                next_fire_at=due_at.isoformat(),
                payload=Task(task_id=f"run-of-{source}", task_type=MessageMetaType.REQUEST, problem_statement="Run"),
                payload_header=header,
                payload_meta=meta,
                interval_ms=3_600_000,
            )
        )
    claim = partial(
        store.claim_next_due, claimer_agent_id="lead", claimer_named_agent_type=None,
        claimer_team_id=None, allow_unscoped=True,
    )

    assert store.get_earliest_fire_time(parallel_thread=True) == due["parallel"].isoformat()
    assert store.get_earliest_fire_time() == due["ordinary"].isoformat()
    assert claim(parallel_thread=True)["schedule_id"] == "schedule-parallel"
    assert claim(parallel_thread=True) is None, "the ordinary schedule waits for the main lane"
    assert claim()["schedule_id"] == "schedule-ordinary"


def test_a_message_goes_to_the_lane_of_the_task_it_concerns(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "parallel", parallel=True)
    question = TaskQuery(parent_task_id="parallel", query_type=MessageMetaType.REQUEST, message="Which account?")
    result = Task(task_id="child", parent_task_id="parallel", task_type=MessageMetaType.RESPONSE, answer="done")

    assert lane_for_message(manager, question) == parallel_lane("parallel")
    assert lane_for_message(manager, result) == parallel_lane("parallel")
    assert lane_for_message(manager, Task(task_id="x", task_type=MessageMetaType.RESPONSE)) == MAIN_LANE


# -- the task manager --------------------------------------------------------------


def test_a_parallel_task_neither_takes_nor_preempts_the_main_slot(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "ordinary", parallel=False)
    _add(manager, "parallel", parallel=True)

    # The main lane takes ordinary work only; the parallel lane its own request.
    assert queued_parallel_lanes(manager) == [parallel_lane("parallel")]
    assert manager.get_next_task_to_process() == "ordinary"
    assert manager.update_main_task_state("ordinary", ProgressState.IN_PROGRESS, _now())
    assert manager.get_next_task_to_process(parallel_lane("parallel")) == "parallel"
    assert manager.update_main_task_state("parallel", ProgressState.IN_PROGRESS, _now())

    assert manager.get_task_info("ordinary").state == ProgressState.IN_PROGRESS, "not paused by the parallel task"
    assert manager.current_task_id == "ordinary"
    assert executing_parallel_task_ids(manager) == ["parallel"]
    assert manager.update_main_task_state("parallel", ProgressState.WAITING, _now())
    assert manager.current_task_id == "ordinary"
    assert executing_parallel_task_ids(manager) == []


def test_the_main_lane_still_waits_for_its_slot_while_a_parallel_task_runs(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "first", parallel=False)
    _add(manager, "second", parallel=False)
    assert manager.get_next_task_to_process() == "first"
    assert manager.update_main_task_state("first", ProgressState.IN_PROGRESS, _now())

    assert manager.get_next_task_to_process() is None


# -- the limit ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_most_the_limit_of_parallel_lanes_start_and_a_freed_one_lets_the_next_start(monkeypatch):
    release = asyncio.Event()
    started: list[str] = []
    waiting = [parallel_lane(f"request-{index}") for index in range(MAX_PARALLEL_LANES + 1)]

    async def run_lane(_agent, lane: str) -> None:
        started.append(lane)
        await release.wait()

    async def with_work(_agent):
        return [lane for lane in waiting if lane not in started]

    monkeypatch.setattr(execution_lanes_runtime, "run_parallel_lane", run_lane)
    monkeypatch.setattr(execution_lanes_runtime, "parallel_lanes_with_work", with_work)
    host = SimpleNamespace(
        id="lead",
        paused=False,
        _should_exit=asyncio.Event(),
        _owned_tasks=set(),
        _parallel_lanes=ParallelLanes(),
        _execution_lanes=AgentExecutionLanes(),
        _logger=logging.getLogger("test_execution_lanes"),
        task_manager=SimpleNamespace(parallel_lanes_enabled=True),
    )

    await execution_lanes_runtime.start_parallel_lanes(host)
    await asyncio.sleep(0)
    assert started == waiting[:MAX_PARALLEL_LANES]

    release.set()
    await asyncio.gather(*host._parallel_lanes.workers.values())
    await execution_lanes_runtime.start_parallel_lanes(host)
    await asyncio.sleep(0)
    assert started == waiting
    await asyncio.gather(*host._parallel_lanes.workers.values())


@pytest.mark.asyncio
async def test_the_agent_reads_a_tasks_lane_on_the_operations_lane(tmp_path):
    manager = _manager(tmp_path)
    _add(manager, "parallel", parallel=True)
    agent = SimpleNamespace(task_manager=manager)
    assert await execution_lanes_runtime.lane_of(agent, "parallel") == parallel_lane("parallel")
    assert await execution_lanes_runtime.lane_of(agent, None) == MAIN_LANE


# -- starting and restarting lane workers ----------------------------------------------


class _Operations:
    async def run(self, function, *args, **kwargs):
        await asyncio.sleep(0)
        return function(*args, **kwargs)


class _Mailbox:
    """A runtime coordinator whose events, if any, fail every time."""

    def __init__(self, roots: list[str]) -> None:
        self.attempts = 0
        self.runtime = SimpleNamespace(
            list_deferred_runtime_event_root_ids=lambda: list(roots),
            get_active_runtime_lease=lambda _root: None,
        )

    def enabled(self) -> bool:
        return True

    def available(self) -> bool:
        return True

    async def drain_deferred_events(self, _root: str) -> bool:
        self.attempts += 1
        return False


def _lane_host(monkeypatch, *, queued: list[str], next_task, mailbox: _Mailbox | None = None) -> SimpleNamespace:
    """An agent with the real lane gate, release callback, start pass and lane workers.

    ``queued`` lists the lanes with a queued task; ``next_task(lane)`` stands for
    starting the lane's next task (``_check_and_process_next_task``).
    """

    lanes = AgentExecutionLanes()
    host = SimpleNamespace(
        id="lead",
        paused=False,
        _should_exit=asyncio.Event(),
        _owned_tasks=set(),
        _parallel_lanes=ParallelLanes(),
        _logger=logging.getLogger("test_execution_lanes"),
        task_manager=SimpleNamespace(
            parallel_lanes_enabled=True, operations_runtime=_Operations(), due_turn_action_retry_records=list
        ),
        _pending_interrupt_responses={},
        deferred_inbox=None,
        _pending_work_task=None,
        _pending_work_requested=False,
        _pending_work_timers=set(),
        _execution_lanes=lanes,
        _check_and_process_next_task=next_task,
    )
    coordinator = mailbox or _Mailbox([])
    host._runtime_coordinator = lambda: coordinator

    async def no_due_actions(_lane):
        return False

    host._drain_due_turn_action_outbox = no_due_actions
    host._start_parallel_lanes = partial(execution_lanes_runtime.start_parallel_lanes, host)

    @contextlib.asynccontextmanager
    async def scope(lane: str = MAIN_LANE):
        try:
            async with lanes.hold(lane):
                yield
        finally:
            # Like UniversalAgent._execution_scope, which reports the agent's
            # state after the release, so the worker ends a little later.
            await asyncio.sleep(0.01)

    host._execution_scope = scope
    lanes.on_release = partial(execution_lanes_runtime.release_lane, host)

    async def lane_of(_agent, task_id):
        return parallel_lane(task_id) if task_id else MAIN_LANE

    monkeypatch.setattr(execution_lanes_runtime, "lane_of", lane_of)
    monkeypatch.setattr(execution_lanes_runtime, "queued_parallel_lanes", lambda _manager: list(queued))
    return host


async def _stop(host: SimpleNamespace) -> None:
    host._should_exit.set()
    for timer in host._pending_work_timers:
        timer.cancel()
    tasks = [task for task in (*host._owned_tasks, host._parallel_lanes.task) if task is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_lane_whose_mailbox_event_keeps_failing_waits_before_it_starts_again(monkeypatch):
    """Regression (PR #57 review): the lane's release started it again at once, about
    20,000 failed attempts in half a second."""

    async def no_task(_lane):
        return False

    mailbox = _Mailbox(["request"])
    host = _lane_host(monkeypatch, queued=[], next_task=no_task, mailbox=mailbox)
    schedule_parallel_lanes(host)
    await asyncio.sleep(0.2)
    try:
        assert mailbox.attempts == 1
        assert host._parallel_lanes.failures == {parallel_lane("request"): 1}
        assert host._parallel_lanes.retry_after[parallel_lane("request")] > asyncio.get_running_loop().time()
        assert len(host._pending_work_timers) == 1, "a timer starts the lane again after the delay"
    finally:
        await _stop(host)


@pytest.mark.asyncio
async def test_a_lane_that_waited_for_the_limit_starts_when_another_lane_ends(monkeypatch):
    lanes = [parallel_lane(f"request-{index}") for index in range(MAX_PARALLEL_LANES + 1)]
    queued = list(lanes)
    ran: list[str] = []
    finish = asyncio.Event()

    async def next_task(lane):
        if lane not in queued:
            return False
        queued.remove(lane)
        ran.append(lane)
        await finish.wait()
        return True

    host = _lane_host(monkeypatch, queued=queued, next_task=next_task)
    schedule_parallel_lanes(host)
    await asyncio.sleep(0.05)
    try:
        assert ran == lanes[:MAX_PARALLEL_LANES]
        finish.set()  # Nothing else wakes the pass: the lanes' releases must.
        await asyncio.sleep(0.05)
        assert ran == lanes
        assert not host._execution_lanes.busy
    finally:
        await _stop(host)


@pytest.mark.asyncio
async def test_work_that_arrives_as_a_lane_worker_ends_is_not_lost(monkeypatch):
    lane = parallel_lane("request")
    queued = [lane]
    turns: list[str] = []

    async def next_task(_lane):
        if queued:
            queued.clear()
            turns.append("turn")
            return True
        if len(turns) == 1:
            # The worker found nothing left; work arrives before it ends.
            queued.append(lane)
            schedule_parallel_lanes(host)
        return False

    host = _lane_host(monkeypatch, queued=queued, next_task=next_task)
    schedule_parallel_lanes(host)
    await asyncio.sleep(0.1)
    try:
        assert turns == ["turn", "turn"]
    finally:
        await _stop(host)


@pytest.mark.asyncio
async def test_at_the_limit_a_request_under_way_goes_before_a_new_one_and_other_holders_count(monkeypatch):
    """PR #57 review: the limit counted only lane workers, and new requests took freed lanes
    first while requests under way waited for their results to be handled."""

    started: list[str] = []
    finish = asyncio.Event()

    async def next_task(lane):
        started.append(lane)
        await finish.wait()
        return False

    new_requests = [parallel_lane(f"new-{index}") for index in range(MAX_PARALLEL_LANES)]
    host = _lane_host(monkeypatch, queued=new_requests, next_task=next_task)
    # A worker's result waits for the request under way.
    host._pending_interrupt_responses = {"under-way": object()}

    async def resume(_task_id):
        host._pending_interrupt_responses.clear()
        started.append(parallel_lane("under-way"))

    host._resume_interrupted_task_turn = resume
    # The scheduler's resume of another request holds its lane outside any worker.
    held, release_held = asyncio.Event(), asyncio.Event()

    async def scheduler_resume():
        async with host._execution_lanes.hold(parallel_lane("resumed")):
            held.set()
            await release_held.wait()

    holder = asyncio.create_task(scheduler_resume())
    await held.wait()
    schedule_parallel_lanes(host)
    await asyncio.sleep(0.05)
    try:
        assert started[0] == parallel_lane("under-way"), "the request under way first"
        # Three workers plus the scheduler's holder make the limit of four.
        assert len(execution_lanes_runtime.lanes_in_use(host)) == MAX_PARALLEL_LANES
        assert parallel_lane("new-3") not in started
        assert host._parallel_lanes.limited
    finally:
        finish.set()
        release_held.set()
        await holder
        await _stop(host)


@pytest.mark.asyncio
async def test_a_lane_held_outside_its_worker_drops_its_strands_agent_on_release(monkeypatch):
    """PR #57 review: a lane's Strands agent built for the scheduler's or a delivery's resume
    of a request stayed in memory; only a lane worker dropped it."""

    host = _lane_host(monkeypatch, queued=[], next_task=None)
    lane = parallel_lane("resumed")
    async with host._execution_lanes.hold(lane):
        host._parallel_lanes.parts[lane] = object()
    assert lane not in host._parallel_lanes.parts


def test_a_lanes_rebuilt_strands_agent_keeps_the_requests_loaded_tool_groups():
    """PR #57 review: after a model rebuild a lane's new Strands agent lost the tool groups its
    request had loaded with use_tools; the main agent keeps them (keep_loaded_tools)."""

    from custom_agents.tool_loading import LoadedTools

    built: list[object] = []

    def new_parts(loaded=None):
        built.append(loaded)
        return SimpleNamespace(generation=2, replace_before_next_turn=False, loaded=loaded or LoadedTools())

    lane = parallel_lane("request")
    old = SimpleNamespace(generation=1, replace_before_next_turn=False, loaded=LoadedTools())
    host = SimpleNamespace(
        _strands_parts=object(),
        _parallel_lanes=ParallelLanes(parts={lane: old}),
        _interrupted_turn_sessions={},
        _strands_generation=2,
        _new_strands_parts=new_parts,
        _logger=logging.getLogger("test_execution_lanes"),
        id="lead",
    )
    rebuilt = execution_lanes_runtime.strands_parts_for_lane(host, lane)
    assert rebuilt.loaded is old.loaded

    # A paused turn keeps its parts and loaded groups; the next turn starts with its own.
    host._interrupted_turn_sessions = {"paused": SimpleNamespace(strands=rebuilt)}
    fresh = execution_lanes_runtime.strands_parts_for_lane(host, lane)
    assert built[-1] is None and fresh.loaded is not rebuilt.loaded


@pytest.mark.asyncio
async def test_a_lane_listed_for_work_no_step_handles_waits_before_it_starts_again(monkeypatch, caplog):
    """A lane listed with work its worker cannot do (a defect) must not restart in a tight loop."""

    starts = 0

    async def nothing_to_start(_lane):
        nonlocal starts
        starts += 1
        return False

    # Listed as queued, but the worker never finds the task.
    host = _lane_host(monkeypatch, queued=[parallel_lane("request")], next_task=nothing_to_start)
    with caplog.at_level(logging.WARNING):
        schedule_parallel_lanes(host)
        await asyncio.sleep(0.2)
    try:
        assert starts == 2
        assert "event=parallel_lane_started_without_work" in caplog.text
    finally:
        await _stop(host)

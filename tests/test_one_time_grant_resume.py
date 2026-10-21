"""A one-time grant serves the one tool call that used it, also when that call pauses and resumes.

A tool that waits for a person inside itself (``apply_change``,
``set_up_tools``, the access workflow and proposal tools) pauses with a
Strands interrupt, and Strands runs the call again from its start when it
resumes, its before-call hooks included. The access hook
(``custom_agents.access_runtime``) therefore decides the same call twice.
These tests check that the resumed call keeps the use it took, and that no
other call passes on that use: not a sibling call in the same model step, not
a later call, and not the fresh call a task makes after a restart (a paused
call lives only in its process, so a restart never resumes it).

Real pieces: Cedar, the access store (SQLite; PostgreSQL with
``LIVE_TEST_POSTGRES_DSN``), the access service, the access hook, and Strands
agents on the locked SDK version with the runtime's tool executor and
Strands' own interrupt and resume. Only the model is scripted; no provider is
called.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from strands import Agent, tool
from strands.models.model import Model
from strands.types.tools import ToolContext

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import PersonPrincipal
from custom_agents import access_runtime
from custom_agents.access_runtime import AccessDecisionHook
from custom_agents.default_tools import builtin_tool_catalog
from custom_agents.sdk.strands.changes_first import ChangesFirstToolExecutor
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantLifetime,
    GrantV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer, context_verify_keys

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
OWNER = PersonPrincipal("user-1", TEAM, ("owner",))
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
WORKER_TYPE = "WebResearchAgent"
WORKER_ID = f"agent-{WORKER_TYPE}"
TOOL_IDS = {"set_up_tools": "fn:tool_setup", "apply_change": "fn:changes"}

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")

# What each scripted tool did, in order: "<function>:<toolUseId>:<step>".
RAN: list[str] = []


def _wait_for_a_person(function: str, tool_context: ToolContext) -> str:
    call = tool_context.tool_use["toolUseId"]
    RAN.append(f"{function}:{call}:start")
    answer = tool_context.interrupt(f"wait-{function}", reason={"why": "a person decides"})
    RAN.append(f"{function}:{call}:{answer}")
    return f"{function} finished"


# Named like the catalog functions, so the access hook decides them as those
# tools; each pauses inside itself the way the real ones wait for a person.
@tool(context=True)
def set_up_tools(tool_context: ToolContext) -> str:
    """Wait until the tools a request needs are set up."""
    return _wait_for_a_person("set_up_tools", tool_context)


@tool(context=True)
def apply_change(tool_context: ToolContext) -> str:
    """Apply a staged change once a person approves it."""
    return _wait_for_a_person("apply_change", tool_context)


class ScriptedModel(Model):
    """Asks for the given calls, one list per model step; then answers in text."""

    def __init__(self, steps: list[list[tuple[str, str]]]) -> None:
        self.steps = steps
        self.step = 0

    def update_config(self, **config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "scripted"}

    async def structured_output(self, *args: Any, **kwargs: Any):
        raise AssertionError("No structured output call expected")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.step += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.step <= len(self.steps):
            for function, call in self.steps[self.step - 1]:
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": call, "name": function}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "done"}}}
        yield {"contentBlockStop": {"contentBlockIndex": 0}}
        yield {"messageStop": {"stopReason": "end_turn"}}


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    access_runtime._runtime_access.cache_clear()
    yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    access_runtime._runtime_access.cache_clear()


def worker(storage: Any) -> SimpleNamespace:
    """A worker running one task for OWNER, with the task's signed context (as in production)."""

    signed = context_signer().issue(team_id=TEAM, person_id=OWNER.person_id, person_roles=OWNER.roles)
    meta = SimpleNamespace(gateway_context=SimpleNamespace(task_context=signed), conversation_id="conv-1")
    task_manager = SimpleNamespace(
        storage_settings=storage,
        current_task_id="task-1",
        get_task_info=lambda _task_id: SimpleNamespace(meta=meta),
        add_task_terminal_callback=lambda _callback: None,
        add_task_release_callback=lambda _callback: None,
    )
    return SimpleNamespace(
        id=WORKER_ID,
        named_agent_type=WORKER_TYPE,
        team_id=TEAM,
        is_team_lead=False,
        access_role="worker",
        authorization_config={"enabled": True, "cedar_binary_path": str(BINARY)},
        task_manager=task_manager,
        instance_root=None,
        work_id=signed.context.context_id,
    )


def strands_agent(ua_agent: SimpleNamespace, steps: list[list[tuple[str, str]]]) -> Agent:
    return Agent(
        model=ScriptedModel(steps),
        tools=[set_up_tools, apply_change],
        callback_handler=None,
        hooks=[AccessDecisionHook(ua_agent)],
        tool_executor=ChangesFirstToolExecutor(builtin_tool_catalog().record_kind),
    )


def give_one_use(storage: Any, ua_agent: SimpleNamespace, function: str) -> GrantV1:
    """The person gives the worker's task one use of the tool behind ``function``."""

    service = AccessService(
        store=AccessRepository(storage), catalog=load_catalog(None), cedar=CedarAccessClient(binary=str(BINARY)), team_id=TEAM
    )
    try:
        return service.grant_by_person(
            OWNER,
            GrantV1(
                grant_id="new",
                team_id=TEAM,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{WORKER_TYPE}"),
                target=TargetRefV1(kind=TargetKind.TOOL, id=TOOL_IDS[function]),
                level=AccessLevel.WRITE,
                lifetime=GrantLifetime.ONE_TIME,
                task_id=ua_agent.work_id,
                uses_remaining=1,
                for_person_id=OWNER.person_id,
                granted_by=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id),
                created_at_utc=datetime.now(timezone.utc),
            ),
        )
    finally:
        service.cedar.close()


def step_results(agent: Agent) -> list[dict[str, Mapping[str, Any]]]:
    """The tool results the model saw, one map per model step, by tool use id."""

    steps = []
    for message in agent.messages:
        found = {block["toolResult"]["toolUseId"]: block["toolResult"] for block in message["content"] if "toolResult" in block}
        if found:
            steps.append(found)
    return steps


def uses_taken(storage: Any, grant: GrantV1) -> int:
    return [event.kind for event in AccessRepository(storage).events(TEAM, grant_id=grant.grant_id)].count(
        "grant_consumed"
    )


def refused(result: Mapping[str, Any]) -> bool:
    if result["status"] != "error":
        return False
    try:
        return json.loads(result["content"][0]["text"]).get("status") == "access_denied"
    except (ValueError, AttributeError):
        return False


def resume(agent: Agent, paused: Any, answer: str = "approved") -> Any:
    [interrupt] = paused.interrupts
    return asyncio.run(agent.invoke_async([{"interruptResponse": {"interruptId": interrupt.id, "response": answer}}]))


@pytest.mark.parametrize(
    ("function", "later_call"),
    [
        ("set_up_tools", "call-2"),
        ("apply_change", "call-2"),
        # Some model servers repeat their tool use ids in later steps.
        ("set_up_tools", "call-1"),
    ],
)
def test_a_paused_call_resumes_under_its_one_time_grant_and_a_later_call_is_refused(
    storage, function: str, later_call: str
) -> None:
    RAN.clear()
    ua_agent = worker(storage)
    grant = give_one_use(storage, ua_agent, function)
    agent = strands_agent(ua_agent, [[(function, "call-1")], [(function, later_call)]])

    paused = asyncio.run(agent.invoke_async("go"))

    assert paused.stop_reason == "interrupt"
    assert RAN == [f"{function}:call-1:start"]
    assert AccessRepository(storage).get(grant.grant_id).uses_remaining == 0

    resume(agent, paused)

    # Strands runs the paused call again from its start, and it finishes under the use it took.
    assert RAN == [f"{function}:call-1:start", f"{function}:call-1:start", f"{function}:call-1:approved"]
    first_step, later_step = step_results(agent)
    assert first_step["call-1"]["status"] == "success", first_step["call-1"]
    # The model's next step calls the tool again: a different call, so the used grant does not cover it.
    assert refused(later_step[later_call]), later_step[later_call]
    assert uses_taken(storage, grant) == 1


def test_a_sibling_call_in_the_same_step_cannot_pass_on_the_use(storage) -> None:
    RAN.clear()
    ua_agent = worker(storage)
    grant = give_one_use(storage, ua_agent, "set_up_tools")
    # set_up_tools changes no record, so Strands decides and runs both calls side by side.
    agent = strands_agent(
        ua_agent, [[("set_up_tools", "call-a"), ("set_up_tools", "call-b")], [("set_up_tools", "call-c")]]
    )

    paused = asyncio.run(agent.invoke_async("go"))

    assert paused.stop_reason == "interrupt"
    [started] = [entry.split(":")[1] for entry in RAN]
    sibling = ({"call-a", "call-b"} - {started}).pop()

    resume(agent, paused)

    assert RAN == [f"set_up_tools:{started}:start", f"set_up_tools:{started}:start", f"set_up_tools:{started}:approved"]
    first_step, later_step = step_results(agent)
    assert first_step[started]["status"] == "success", first_step[started]
    assert refused(first_step[sibling]), first_step[sibling]
    assert refused(later_step["call-c"]), later_step["call-c"]
    assert uses_taken(storage, grant) == 1


def test_after_a_restart_the_tasks_fresh_call_cannot_use_what_the_paused_call_took(storage) -> None:
    """A paused call lives only in its process (no Strands session is stored), so a restart never resumes it.

    The task then continues with a fresh model turn (``custom_agents.task_steering_runtime``), whose call
    is another call, even when the model server repeats the tool use id.
    """

    RAN.clear()
    ua_agent = worker(storage)
    grant = give_one_use(storage, ua_agent, "apply_change")
    paused = asyncio.run(strands_agent(ua_agent, [[("apply_change", "call-1")]]).invoke_async("go"))
    assert paused.stop_reason == "interrupt"

    # The process stops: its paused Strands agent and its access service are gone.
    access_runtime._runtime_access.cache_clear()
    fresh = strands_agent(ua_agent, [[("apply_change", "call-1")]])
    asyncio.run(fresh.invoke_async("Continue the task."))

    [[result]] = [list(step.values()) for step in step_results(fresh)]
    assert refused(result), result
    assert RAN == ["apply_change:call-1:start"]
    assert uses_taken(storage, grant) == 1


# -- the store and the access service, on SQLite and PostgreSQL ------------------------------------


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=[pytest.mark.postgres, pytest.mark.live, pytest.mark.integration]),
    ]
)
def store_settings(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "sqlite":
        yield resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
        return
    from tests.test_access_store_postgres import postgres_storage

    with postgres_storage() as settings:
        yield settings


def decide_with(service: AccessService, operation_id: str | None, work_id: str):
    from access_control.entities import AgentPrincipal

    return service.decide(
        agent=AgentPrincipal(WORKER_ID, WORKER_TYPE, "worker", TEAM, work_id=work_id),
        person=OWNER,
        resolution=service.catalog.resolve_function("apply_change", {}),
        task_id=work_id,
        operation_id=operation_id,
    )


def test_a_use_belongs_to_the_call_that_took_it_also_after_the_store_is_opened_again(store_settings) -> None:
    ua_agent = worker(store_settings)
    grant = give_one_use(store_settings, ua_agent, "apply_change")
    cedar = CedarAccessClient(binary=str(BINARY))
    try:
        service = AccessService(store=AccessRepository(store_settings), catalog=load_catalog(None), cedar=cedar, team_id=TEAM)

        first = decide_with(service, "tool_call:one", ua_agent.work_id)
        again = decide_with(service, "tool_call:one", ua_agent.work_id)

        assert first.allowed and first.consumed_grant_ids == (grant.grant_id,)
        assert again.allowed and again.consumed_grant_ids == (grant.grant_id,)
        assert not decide_with(service, "tool_call:two", ua_agent.work_id).allowed
        assert not decide_with(service, None, ua_agent.work_id).allowed
        # The record is in the store, not in the process that took the use.
        reopened = AccessService(store=AccessRepository(store_settings), catalog=load_catalog(None), cedar=cedar, team_id=TEAM)
        assert decide_with(reopened, "tool_call:one", ua_agent.work_id).allowed
        assert not decide_with(reopened, "tool_call:three", ua_agent.work_id).allowed
        # Only one use was ever taken, and the log names the call that took it.
        consumed = [e for e in reopened.store.events(TEAM, grant_id=grant.grant_id) if e.kind == "grant_consumed"]
        assert [event.detail for event in consumed] == [{"operation_id": "tool_call:one"}]
        assert reopened.store.get(grant.grant_id).uses_remaining == 0
        # Revoking the grant while the call waits stops it when it resumes.
        reopened.store.revoke(grant.grant_id, actor=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id), at=NOW)
        assert not decide_with(reopened, "tool_call:one", ua_agent.work_id).allowed
    finally:
        cedar.close()


def test_two_calls_decided_at_once_never_share_one_use(store_settings) -> None:
    import threading

    ua_agent = worker(store_settings)
    grant = give_one_use(store_settings, ua_agent, "apply_change")
    start = threading.Barrier(2)
    allowed: dict[str, bool] = {}

    def decide(operation_id: str) -> None:
        cedar = CedarAccessClient(binary=str(BINARY))
        try:
            service = AccessService(
                store=AccessRepository(store_settings), catalog=load_catalog(None), cedar=cedar, team_id=TEAM
            )
            start.wait()
            allowed[operation_id] = decide_with(service, operation_id, ua_agent.work_id).allowed
        finally:
            cedar.close()

    threads = [threading.Thread(target=decide, args=(f"tool_call:{name}",)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(allowed.values()) == [False, True]
    assert uses_taken(store_settings, grant) == 1


@pytest.mark.parametrize("held", [False, True], ids=["first-use", "held-use"])
@pytest.mark.parametrize("revocation", ["revoke", "end-task"])
def test_revocation_after_eligibility_refuses_consumption(
    store_settings, monkeypatch: pytest.MonkeyPatch, held: bool, revocation: str
) -> None:
    """A held use cannot bypass a revocation committed before the consumption write lock."""

    ua_agent = worker(store_settings)
    grant = give_one_use(store_settings, ua_agent, "apply_change")
    repo = AccessRepository(store_settings)
    other_writer = AccessRepository(store_settings)
    cedar = CedarAccessClient(binary=str(BINARY))
    try:
        service = AccessService(store=repo, catalog=load_catalog(None), cedar=cedar, team_id=TEAM)
        operation_id = "tool_call:one"
        if held:
            assert decide_with(service, operation_id, ua_agent.work_id).allowed

        read_grant = repo.get
        consume = repo.consume

        def read_then_revoke(grant_id: str):
            snapshot = read_grant(grant_id)
            assert snapshot is not None and snapshot.status.value == "active"
            if revocation == "revoke":
                other_writer.revoke(
                    grant_id, actor=SubjectRefV1(kind=SubjectKind.USER, id=OWNER.person_id), at=NOW
                )
            else:
                assert grant_id in other_writer.end_task_grants(TEAM, [ua_agent.work_id], at=NOW)
            assert other_writer.get(grant_id).status.value == "revoked"
            return snapshot

        def consume_after_revocation(grant_id: str, *, at: datetime, operation_id: str | None = None):
            # Eligibility has already passed. Keep even consume's pre-lock
            # read stale, so the status check must run under the write lock.
            with monkeypatch.context() as patch:
                patch.setattr(repo, "get", read_then_revoke)
                return consume(grant_id, at=at, operation_id=operation_id)

        monkeypatch.setattr(repo, "consume", consume_after_revocation)
        result = decide_with(service, operation_id, ua_agent.work_id)

        assert not result.allowed
        assert not result.consumed_grant_ids
        assert uses_taken(store_settings, grant) == int(held)
    finally:
        cedar.close()

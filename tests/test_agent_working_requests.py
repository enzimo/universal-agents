"""Which person's request an agent's model turn runs for (custom_agents.working_requests).

Web Chat's agent trail (A1, agent side): a turn reports the root task id of a
gateway-signed person context, decided once when the turn starts (plan review
C01). Scheduled runs, system work and anything unverifiable report nothing,
and nothing here may raise into the turn.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from access_control.authority_verifier import AuthorityTokenVerifier
from custom_agents import turn_registry, working_requests
from custom_messages.contracts.routing_v1 import GatewayContext
from custom_messages.wire.authority_bindings import AuthorityTokenRecord
from custom_tools.runtime_context import task_work
from custom_transport.protocol1.authority_tokens import root_task_token
from custom_transport.protocol1.host_keys import HostKeys
from custom_transport.protocol1.identity import HostIdentity
from tests.broker_access_support import (
    TEST_TEAM as TEAM,
    broker_authority_verifier,
    broker_gateway_identity,
    broker_people_store,
    broker_scheduler_identity,
)

NOW = datetime.now(timezone.utc)
GATEWAY = broker_gateway_identity()
SCHEDULER = broker_scheduler_identity()
ANN = broker_people_store().register(TEAM, "person_ann", roles=("member",))
VERIFIER: AuthorityTokenVerifier = broker_authority_verifier()


def _person_token(
    issuer: HostIdentity = GATEWAY,
    *,
    team: str = TEAM,
    person_ref: str | None = ANN.person_ref,
    root_task_id: str = "task_request_1",
    request_id: str = "task_context_1",
    schedule_id: str | None = None,
) -> AuthorityTokenRecord:
    raw = root_task_token(
        issuer,
        team=team,
        root_task_id=root_task_id,
        request_id=request_id,
        person_ref=person_ref,
        schedule_id=schedule_id,
        issued=NOW,
    )
    return AuthorityTokenRecord.model_validate(raw)


class _Tasks:
    def __init__(self, metas: dict[str, Any]) -> None:
        self.metas = metas

    def get_task_info(self, task_id: str) -> Any:
        meta = self.metas.get(task_id)
        return SimpleNamespace(meta=meta) if meta is not None else None


def _agent(metas: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(id="agent_1", task_manager=_Tasks(metas), _running_turns={})


def _meta(**gateway: Any) -> SimpleNamespace:
    return SimpleNamespace(gateway_context=GatewayContext(**gateway))


@pytest.fixture(autouse=True)
def _access(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The agent's verifier, without task storage; records each use."""

    used: list[Any] = []

    def runtime_access(agent: Any) -> SimpleNamespace:
        used.append(agent)
        return SimpleNamespace(verifier=VERIFIER, team_id=TEAM)

    monkeypatch.setattr("custom_agents.access_runtime.runtime_access", runtime_access)
    return used


def test_a_turn_for_a_persons_request_reports_its_root_task() -> None:
    agent = _agent({"task_1": _meta(task_context=_person_token())})
    assert working_requests.turn_request_id(agent, "task_1") == "task_request_1"


def test_a_delegated_task_carries_the_same_context_and_reports_the_same_request() -> None:
    parent = GatewayContext(task_context=_person_token())
    child = SimpleNamespace(gateway_context=parent.model_copy(deep=True))  # delegation_runtime copies it
    assert working_requests.turn_request_id(_agent({"child": child}), "child") == "task_request_1"


@pytest.mark.parametrize(
    "context",
    [
        pytest.param(lambda: _person_token(SCHEDULER), id="scheduler-signed run"),
        pytest.param(
            lambda: _person_token(SCHEDULER, schedule_id="schedule_7"),
            id="scheduler-signed scheduled job",
        ),
        pytest.param(
            lambda: _person_token(SCHEDULER, person_ref=None, schedule_id="schedule_sys"),
            id="system work",
        ),
        pytest.param(lambda: _person_token(team="team_other"), id="another team"),
        pytest.param(
            lambda: _person_token(
                HostIdentity(
                    HostKeys(GATEWAY.runtime, GATEWAY.keys.key_id, Ed25519PrivateKey.generate(), b"0" * 32),
                    GATEWAY.certificate,
                )
            ),
            id="unknown key",
        ),
    ],
)
def test_contexts_that_are_no_verified_persons_request_report_nothing(context: Any) -> None:
    agent = _agent({"task_1": _meta(task_context=context())})
    assert working_requests.turn_request_id(agent, "task_1") is None


def test_a_forged_context_reports_nothing() -> None:
    signed = _person_token()
    forged = signed.model_copy(update={"bound_to": signed.bound_to.model_copy(update={"id": "task_other"})})
    assert working_requests.turn_request_id(_agent({"task_1": _meta(task_context=forged)}), "task_1") is None


def test_a_scheduled_run_or_a_task_without_a_context_never_reaches_the_resolver(_access: list[Any]) -> None:
    run = SimpleNamespace(gateway_context=SimpleNamespace(task_context=None, schedule_run=object()))
    agent = _agent({"run": run, "plain": _meta(), "none": SimpleNamespace(gateway_context=None)})
    assert [working_requests.turn_request_id(agent, task) for task in ("run", "plain", "none", "missing", None)] == [
        None] * 5
    assert _access == []  # no verifier, and no schedule lookup in the store


def test_an_unexpected_failure_is_logged_and_reports_nothing(monkeypatch, caplog) -> None:
    def broken(**_: Any) -> Any:
        raise OSError("store unreadable")

    monkeypatch.setattr(working_requests, "resolve_task_identity", broken)
    agent = _agent({"task_1": _meta(task_context=_person_token())})
    with caplog.at_level(logging.WARNING):
        assert working_requests.turn_request_id(agent, "task_1") is None
    assert "event=working_request_unresolved agent_id=agent_1 task_id=task_1" in caplog.text


@pytest.mark.asyncio
async def test_a_turn_decides_once_when_it_starts_and_reports_at_start_and_end(monkeypatch) -> None:
    reports: list[tuple[str, ...]] = []
    agent = _agent({"task_1": _meta(task_context=_person_token())})
    agent._report_record_state = lambda: reports.append(working_requests.working_request_ids(agent))
    monkeypatch.setattr("custom_agents.person_model_runtime.restore_team_model", lambda *args: None)

    async def turn() -> None:
        with task_work("task_1") as work:
            turn_registry.start_turn(agent, "task_1")
            assert work.request_task_id == "task_request_1"
            turn_registry.end_turn(agent)

    await asyncio.create_task(turn())
    assert reports == [("task_request_1",), ()]


def test_the_working_requests_are_the_running_turns_each_once() -> None:
    agent = _agent({})
    agent._running_turns = {
        "one": SimpleNamespace(request_task_id="task_b"),
        "two": SimpleNamespace(request_task_id=None),
        "three": SimpleNamespace(request_task_id="task_a"),
        "four": SimpleNamespace(request_task_id="task_b"),
    }
    assert working_requests.working_request_ids(agent) == ("task_b", "task_a")

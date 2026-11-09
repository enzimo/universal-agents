"""Every running agent holds a session (custom_agents.agent_host; plan 13B.5).

The process's host and the team services are stand-ins here: each test scripts the team
services' answers to ``sessions.open`` and ``sessions.renew`` and checks what the host asks,
how often, what it logs and what its host record lists. tests/test_live_agent_hosts.py
runs the same on a real NATS server with the real authority and team services.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional

import pytest

from custom_agents import agent_host
from custom_agents.agent_host import (
    MORE_AGENTS_CODE,
    MORE_AGENTS_COMPONENT,
    AgentProcessHost,
    _HostedAgent,
    refused_session_component,
    session_renewal_id,
)
from custom_messages.wire.models import HostStateComponentsItem
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.host_runtime import Rejoined
from custom_transport.protocol1.joining import HostProfile
from custom_transport.protocol1.live import ServiceUnavailable
from custom_transport.protocol1.state_records import StoredRecord

TEAM = "team_one"
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
DEFINITION = "DQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0NDQ0"
RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")
TOKEN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


class _Identity:
    def __init__(self, agent_types: Iterable[str], sessions: Optional[dict[str, str]] = None) -> None:
        self.certificate = SimpleNamespace(agent_types=frozenset(agent_types), installation="install_one")
        self.installation = "install_one"
        self.sessions = dict(sessions or {})

    def with_session(self, agent: str, session: str) -> "_Identity":
        return _Identity(self.certificate.agent_types, {**self.sessions, agent: session})

    def without_session(self, agent: str) -> "_Identity":
        return _Identity(self.certificate.agent_types, {k: v for k, v in self.sessions.items() if k != agent})


class _Trust:
    def __init__(self) -> None:
        self.added: list[bytes] = []
        self.sequence = 1
        self.store = SimpleNamespace(held_record=lambda record, key: (self.sequence, "digest"))

    def add(self, data: bytes, *, now: datetime) -> bool:
        self.added.append(data)
        return True


class _Subscription:
    async def unsubscribe(self) -> None:
        return None


class _Live:
    def __init__(self, agent_types: Iterable[str]) -> None:
        self.identity = _Identity(agent_types)
        self.trust = _Trust()
        self.served: list[str] = []

    async def serve(self, address: str, receiver: Any, handler: Any, *, agent: Optional[str] = None) -> _Subscription:
        self.served.append(str(agent))
        return _Subscription()


class _Records:
    """Stands in for ``StateRecords``: keeps the agent records written and removed."""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.values: dict[str, dict[str, Any]] = {}

    def signed(self, record: str, key: str, body: dict[str, Any], *, sequence: int, issued_at: datetime,
               expires_at: Optional[datetime] = None) -> dict:
        return {"record": record, "key": key, "body": body}

    async def put(self, record: str, key: str, document: dict[str, Any]) -> int:
        self.written.append(document)
        self.values[key] = document
        return 1

    async def read_report(self, record: str, key: str, *, now: datetime):
        if key not in self.values:
            return None
        return StoredRecord(1, SimpleNamespace(signer=SimpleNamespace(runtime="lead")))

    async def delete(self, record: str, key: str, *, last_revision: int) -> bool:
        self.removed.append(key)
        self.values.pop(key, None)
        return True


class _Host:
    """Stands in for ``MessageHost``: joined, with a certificate naming ``agent_types``."""

    def __init__(self, agent_types: Iterable[str], *, grant: bool = True) -> None:
        self.live = _Live(agent_types)
        self.records: Any = _Records()
        self.rejoined: list[list[str]] = []
        self.grant = grant
        self.reports = 0

    @property
    def identity(self) -> _Identity:
        return self.live.identity

    def clock(self) -> datetime:
        return NOW

    async def rejoin(self, agent_types: Iterable[str]) -> Rejoined:
        asked = list(agent_types)
        self.rejoined.append(asked)
        current = self.live.identity.certificate
        if not self.grant:
            return Rejoined(current, frozenset(), refused=frozenset(asked), refusal="denied: not listed")
        identity = _Identity(current.agent_types | set(asked), self.live.identity.sessions)
        self.live.identity = identity
        return Rejoined(identity.certificate, frozenset(asked))

    async def report_now(self) -> None:
        self.reports += 1


def _session(agent: str, name: str) -> dict[str, Any]:
    return {"record": "agent_session", "body": {"session": name, "agent": agent}}


class _Answers:
    """The team services' answers in order; each request's kind and body are kept."""

    def __init__(self, process: AgentProcessHost, answers: list[Any]) -> None:
        self.process = process
        self.answers = list(answers)
        self.asked: list[tuple[str, dict[str, Any]]] = []
        self.components_when_asked: list[list[dict[str, str]]] = []

    async def __call__(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        self.asked.append((kind, body))
        self.components_when_asked.append(self.process._components())
        answer = self.answers.pop(0) if self.answers else {"outcome": "limit_reached", "message": "full"}
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return answer(body)
        return answer

    def ids(self, kind: str = "sessions.open") -> list[str]:
        return [body["request_id"] for asked, body in self.asked if asked == kind]


def _process(agent_types: Iterable[str] = ("Researcher",), *, catalog: Optional[Callable[[], Iterable[str]]] = None,
             grant: bool = True, retry: tuple[float, float] = (5.0, 300.0)) -> tuple[AgentProcessHost, list[float]]:
    profile = HostProfile("lead", (TEAM,), ("lead_host", "worker_host"), tuple(agent_types), 4)
    process = AgentProcessHost(None, profile, TEAM, catalog_types=catalog, refused_retry_seconds=retry)  # type: ignore[arg-type]
    process.host = _Host(agent_types, grant=grant)  # type: ignore[assignment]
    process.loop = asyncio.get_running_loop()
    process._joined, process._stop = asyncio.Event(), asyncio.Event()
    process._joined.set()
    pauses: list[float] = []

    async def pause(hosted: _HostedAgent, seconds: float) -> None:
        pauses.append(seconds)
        await asyncio.sleep(0)

    process._pause = pause  # type: ignore[method-assign]
    return process, pauses


def _hosted(agent_id: str, agent_type: str = "Researcher") -> _HostedAgent:
    return _HostedAgent(SimpleNamespace(id=agent_id, named_agent_type=agent_type), agent_type, DEFINITION)


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if f"event={event} " in record.getMessage()]


@pytest.mark.asyncio
async def test_a_supervised_opening_retry_cannot_reattach_an_agent_that_left(monkeypatch) -> None:
    from custom_agents import agent_host

    monkeypatch.setattr(agent_host, "RETRY_SECONDS", (0.02,))
    process, _ = _process()
    answers = _Answers(process, [{"outcome": "ok", "session": _session("departed", "s-departed-1")},
                                 {"outcome": "ok"}])
    process._request = answers
    failed = asyncio.Event()

    async def fail_to_serve(*_args, **_kwargs):
        failed.set()
        raise ConnectionError("subscription interrupted")

    process.host.live.serve = fail_to_serve
    hosted = _hosted("departed")
    opening = process._spawn(lambda: process._open(hosted), "open_session")
    try:
        await asyncio.wait_for(failed.wait(), 1)
        await process._close("departed", "exited")
        await asyncio.wait_for(opening, 1)
        assert hosted.left.is_set()
        assert not process.agents and not process._pending_closes
        assert len(answers.ids()) == 1
        assert len(answers.ids("sessions.close")) == 1
    finally:
        await process.stop()


@pytest.mark.asyncio
async def test_a_refused_session_is_asked_for_again_until_it_opens_and_each_change_is_logged_once(caplog) -> None:
    process, pauses = _process()
    answers = _Answers(process, [
        {"outcome": "limit_reached", "message": "full"}, {"outcome": "limit_reached", "message": "full"},
        {"outcome": "limit_reached", "message": "full"}, {"outcome": "limit_reached", "message": "full"},
        {"outcome": "runtime_error", "message": "The placement changed at the same time; ask again."},
        lambda body: {"outcome": "ok", "session": _session("a1", "s-a1-9")},
    ])
    process._request = answers  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger=agent_host.__name__):
        await process._open(_hosted("a1"))
        await asyncio.sleep(0)  # the host record's writes run

    hosted = process.agents["a1"]
    assert hosted.session == "s-a1-9" and process.host.identity.sessions == {"a1": "s-a1-9"}  # type: ignore[union-attr]
    assert process.host.live.served == ["a1"]  # type: ignore[union-attr]
    # 5 seconds after the first refusal, twice as long after each further one, never more than 5 minutes.
    assert pauses == [5.0, 10.0, 20.0, 40.0, 80.0]
    # Every opening that got an answer is followed by another request, with its own id.
    assert answers.ids() == [f"open-a1-{n}" for n in range(1, 7)]
    refused = _events(caplog, "agent_host_session_refused")
    assert len(refused) == 2
    assert "code=session_limit_reached" in refused[0] and "code=session_runtime_error" in refused[1]
    opened = _events(caplog, "agent_host_session_opened")
    assert len(opened) == 1 and "refusals_before=5" in opened[0]
    # The host record listed the agent while it had no session, and lists nothing once it opened.
    assert answers.components_when_asked[0] == []
    assert answers.components_when_asked[1] == [{"component": "Researcher-a1", "state": "unhealthy",
                                                 "code": "session_limit_reached"}]
    assert answers.components_when_asked[5][0]["code"] == "session_runtime_error"
    assert process._components() == [] and hosted.refusal is None
    assert process.host.reports == 3  # type: ignore[union-attr]  # listed, its code changed, gone


@pytest.mark.asyncio
async def test_the_wait_between_refusals_stops_growing_at_five_minutes() -> None:
    process, pauses = _process()
    answers = _Answers(process, [{"outcome": "already_placed", "host": "agent-Researcher"}] * 9 + [
        lambda body: {"outcome": "ok", "session": _session("a1", "s-a1-3")}])
    process._request = answers  # type: ignore[method-assign]
    await process._open(_hosted("a1"))
    assert pauses == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]


@pytest.mark.asyncio
async def test_an_opening_nobody_answered_yet_keeps_its_request_id_and_is_no_finding(caplog) -> None:
    process, pauses = _process()
    unanswered = ServiceUnavailable("sessions.open", "svc.team", "no responders")
    answers = _Answers(process, [unanswered, unanswered, lambda body: {"outcome": "ok", "session": _session("a1", "s-1")}])
    process._request = answers  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger=agent_host.__name__):
        await process._open(_hosted("a1"))
    assert answers.ids() == ["open-a1-1"] * 3
    assert pauses == [0.5, 1.0]
    assert answers.components_when_asked == [[], [], []] and not _events(caplog, "agent_host_session_refused")


@pytest.mark.asyncio
async def test_a_type_the_certificate_lacks_is_joined_again_for_when_the_catalog_has_it(caplog) -> None:
    catalog = {"TeamArchitect"}
    process, pauses = _process(("TeamArchitect",), catalog=lambda: set(catalog))

    def opened_if_certified(body: dict[str, Any]) -> dict[str, Any]:
        if body["agent_type"] not in process.host.identity.certificate.agent_types:  # type: ignore[union-attr]
            return {"outcome": "not_allowed", "message": "This host's certificate does not allow agent type Analyst."}
        return {"outcome": "ok", "session": _session("x1", "s-x1-4")}

    answers = _Answers(process, [opened_if_certified] * 4)
    process._request = answers  # type: ignore[method-assign]

    async def specialist_created_while_refused(hosted: _HostedAgent, seconds: float) -> None:
        pauses.append(seconds)
        catalog.add("Analyst")  # the team creates the specialist; no restart

    process._pause = specialist_created_while_refused  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger=agent_host.__name__):
        await process._open(_hosted("x1", "Analyst"))

    # Not in the catalog yet: refused, and asked again. Then in it: joined again, and opened at once.
    assert pauses == [5.0]
    assert process.host.rejoined == [["Analyst"]]  # type: ignore[union-attr]
    assert answers.ids() == ["open-x1-1", "open-x1-2", "open-x1-3"]
    assert answers.components_when_asked[1][0]["code"] == "session_not_allowed"
    assert process.agents["x1"].session == "s-x1-4" and process._components() == []
    assert len(_events(caplog, "agent_host_session_refused")) == 1


@pytest.mark.asyncio
async def test_a_type_the_authority_does_not_grant_is_listed_and_asked_for_again() -> None:
    process, pauses = _process(("TeamArchitect",), catalog=lambda: {"TeamArchitect", "Analyst"}, grant=False)
    answers = _Answers(process, [{"outcome": "not_allowed", "message": "no"}] * 2 + [
        lambda body: {"outcome": "ok", "session": _session("x1", "s-x1-5")}])
    process._request = answers  # type: ignore[method-assign]
    await process._open(_hosted("x1", "Analyst"))
    assert process.host.rejoined == [["Analyst"], ["Analyst"]]  # type: ignore[union-attr]
    assert pauses == [5.0, 10.0]
    assert answers.components_when_asked[1] == [{"component": "Analyst-x1", "state": "unhealthy",
                                                 "code": "session_type_not_granted"}]


@pytest.mark.asyncio
async def test_a_type_no_certificate_can_name_is_listed_and_never_asked_for(caplog) -> None:
    process, pauses = _process()
    answers = _Answers(process, [])
    process._request = answers  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger=agent_host.__name__):
        await process._open(_hosted("y1", "Market research specialist"))
    assert answers.asked == [] and pauses == []
    assert process._components() == [{"component": "Market_research_speciali-y1", "state": "unhealthy",
                                      "code": "agent_type_not_certifiable"}]
    refused = _events(caplog, "agent_host_session_refused")
    assert len(refused) == 1 and "not asked for" in refused[0]
    # It leaves: the host record no longer lists it.
    await process._close("y1", "exited")
    assert process._components() == []


@pytest.mark.asyncio
async def test_asking_again_stops_at_once_when_the_agent_leaves() -> None:
    process, _ = _process()
    del process._pause  # the real wait: 5 seconds unless the agent leaves first
    answers = _Answers(process, [])
    process._request = answers  # type: ignore[method-assign]
    opening = asyncio.create_task(process._open(_hosted("a1")))
    while not answers.asked:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.01)
    await process._close("a1", "exited")
    await asyncio.wait_for(opening, timeout=1)
    assert len(answers.asked) == 1 and process._components() == []


@pytest.mark.asyncio
async def test_an_agent_has_one_opening_at_a_time() -> None:
    process, _ = _process()
    release = asyncio.Event()

    async def slow_ok(kind: str, body: dict[str, Any]) -> dict[str, Any]:
        asked.append(body["request_id"])
        await release.wait()
        return {"outcome": "ok", "session": _session("a1", "s-a1-1")}

    asked: list[str] = []
    process._request = slow_ok  # type: ignore[method-assign]
    hosted = _hosted("a1")
    first = asyncio.create_task(process._open(hosted))
    await asyncio.sleep(0.01)
    await process._open(_hosted("a1"))  # attached again: the one opening goes on
    await process._hold_session(hosted)  # a refused renewal while it opens: nothing more
    release.set()
    await first
    assert asked == ["open-a1-1"] and process.host.live.served == ["a1"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_more_agents_without_a_session_than_a_host_record_lists() -> None:
    process, _ = _process()
    for n in range(20):
        hosted = _hosted(f"agent_id_{n:012d}")
        process.agents[str(hosted.agent.id)] = hosted
        hosted.refusal = agent_host._Refusal("session_limit_reached", "full", NOW.replace(second=n))
    components = process._components()
    assert len(components) == 16
    assert components[0]["component"] == "Researcher-agent_id_000000000000"
    assert components[-1] == {"component": MORE_AGENTS_COMPONENT, "state": "unhealthy", "code": MORE_AGENTS_CODE}
    for item in components:
        HostStateComponentsItem.model_validate(item)


def test_a_refused_agents_component_names_its_type_and_id_as_one_token() -> None:
    assert refused_session_component("Researcher", "agent_id_0123456789ab") == "Researcher-agent_id_0123456789ab"
    for agent_type, agent_id in [("Market research specialist", "agent_id_0123456789ab"), ("_x", "a"),
                                 ("T" * 24, "A" * 32), ("Ünïcode type!", "b")]:
        name = refused_session_component(agent_type, agent_id)
        assert TOKEN_ID.fullmatch(name), name
        assert name.endswith(agent_id)


@pytest.mark.asyncio
async def test_each_renewal_has_its_own_id_and_a_retry_keeps_it() -> None:
    """A repeated request id gets the first renewal's stored answer back (README "Stored outcomes")."""

    process, _ = _process()
    hosted = _hosted("a1")
    hosted.session = "s-a1-1"
    process.agents["a1"] = hosted
    trust = process.host.live.trust  # type: ignore[union-attr]

    def renewed(body: dict[str, Any]) -> dict[str, Any]:
        trust.sequence += 1  # the host keeps the renewed record
        return {"outcome": "ok", "session": _session("a1", "s-a1-1")}

    answers = _Answers(process, [ServiceUnavailable("sessions.renew", "svc.team", "no answer in time"), renewed, renewed])
    process._request = answers  # type: ignore[method-assign]
    for _ in range(3):
        await process._renew(hosted)
    first, retried, second = answers.ids("sessions.renew")
    assert first == retried != second
    assert first == session_renewal_id("s-a1-1", 1) and second == session_renewal_id("s-a1-1", 2)
    longest = session_renewal_id("s-" + "a" * 32 + "-" + "9" * 16, 2 ** 53 - 1)
    assert RECORD_ID.fullmatch(longest) and len(longest) <= 128


@pytest.mark.asyncio
async def test_a_refused_renewal_opens_a_new_session(caplog) -> None:
    process, _ = _process()
    hosted = _hosted("a1")
    hosted.session, hosted.left = "s-a1-1", asyncio.Event()
    process.agents["a1"] = hosted
    answers = _Answers(process, [{"outcome": "not_allowed", "message": "Session s-a1-1 has ended; open a new one."},
                                 lambda body: {"outcome": "ok", "session": _session("a1", "s-a1-2")}])
    process._request = answers  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger=agent_host.__name__):
        await process._renew(hosted)
        for _ in range(5):
            await asyncio.sleep(0)
    assert [kind for kind, _ in answers.asked] == ["sessions.renew", "sessions.open"]
    assert hosted.session == "s-a1-2" and process.host.identity.sessions == {"a1": "s-a1-2"}  # type: ignore[union-attr]
    assert _events(caplog, "agent_host_session_renew_refused")


@pytest.mark.asyncio
async def test_cleanup_uses_verified_sessions_even_after_the_placement_revision_changed(caplog) -> None:
    process, _ = _process()
    sessions = {
        "s-old_1-7": SimpleNamespace(runtime="lead", agent="old_1", session="s-old_1-7"),
        "elsewhere": SimpleNamespace(runtime="agent-x", agent="elsewhere", session="elsewhere"),
        "live": SimpleNamespace(runtime="lead", agent="a1", session="live"),
    }
    current = _hosted("a1")
    current.session = "live"
    process.agents["a1"] = current

    async def keys(record, prefix):
        return [prefix + key for key in ["forged", "broken", "removed", *sessions]]

    async def take(record, key, *, now):
        name = key.rsplit(".", 1)[-1]
        if name == "forged":
            raise FrameRefused("wrong_issuer", at="/writer")
        if name == "broken":
            raise RuntimeError("nats: timeout")

    process.host.records = SimpleNamespace(keys=keys, take_trust_record=take)
    process.host.live.trust.store.agent_session = lambda installation, team, session: sessions.get(session)
    answers = _Answers(process, [{"outcome": "ok"}])
    process._request = answers
    with caplog.at_level(logging.WARNING):
        await process._close_earlier_sessions()
    assert answers.asked == [("sessions.close", {"request_id": "close-s-old_1-7", "session": "s-old_1-7", "reason": "failed"})]
    assert process._cleanup_needed
    assert "code=wrong_issuer" in caplog.text
    assert "event=agent_host_stale_session_close_failed" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_close_is_retried_while_the_host_keeps_running(caplog) -> None:
    process, _ = _process()
    hosted = _hosted("a1")
    hosted.session = "s-a1-1"
    process.agents["a1"] = hosted
    answers = _Answers(process, [RuntimeError("nats: timeout"), {"outcome": "ok"}])
    process._request = answers
    with caplog.at_level(logging.WARNING):
        await process._close("a1", "exited")
    assert "a1" not in process.agents and "s-a1-1" in process._pending_closes
    await process._try_close("s-a1-1")
    assert not process._pending_closes
    assert answers.ids("sessions.close") == ["close-s-a1-1", "close-s-a1-1"]
    assert "upkeep will try again" in caplog.text


@pytest.mark.asyncio
async def test_refusal_backoff_keeps_running_after_four_days() -> None:
    process, pauses = _process()
    answers = _Answers(process, [{"outcome": "limit_reached"}] * 1100 + [{"outcome": "ok", "session": _session("a1", "s-1")}])
    process._request = answers
    await process._open(_hosted("a1"))
    assert len(pauses) == 1100 and max(pauses) == 300
    assert process.agents["a1"].session == "s-1"


def test_startup_host_ids_are_bounded_and_distinguish_replica_labels():
    from custom_agents.agent_host import agent_host_runtime, startup_agent_host_profile

    labels = ["Writer#2", "Writer-2", "A" * 64, "A" * 63 + "B"]
    runtimes = [agent_host_runtime(label) for label in labels]
    assert len(set(runtimes)) == len(labels)
    assert all(TOKEN_ID.fullmatch(runtime) for runtime in runtimes)
    with pytest.raises(ValueError, match="cannot have a message session"):
        startup_agent_host_profile(TEAM, runtimes[2], "A" * 64)


def test_production_host_factory_uses_the_launchers_limit_and_the_current_catalog(tmp_path, monkeypatch):
    from custom_agents.agent_host import process_host_for, startup_agent_host_profile
    from custom_agents import agent_catalog
    from runtime_settings.instance_layout import InstanceLayout
    from docker.message_hosts import expected_host
    from installation_authority.expected_hosts import write_expected_hosts

    layout = InstanceLayout.at(tmp_path / "instance")
    catalog = {"TeamArchitect", "Researcher"}
    monkeypatch.setattr(agent_catalog, "load_agent_catalog", lambda: SimpleNamespace(agent_types=set(catalog)))
    profile = HostProfile("lead", (TEAM,), ("lead_host", "worker_host"), (), 7)
    write_expected_hosts(layout.trust_expected_hosts_path, {"lead": expected_host(layout, profile, agent_types_from_catalog=True)})
    lead = process_host_for("lead", layout, TEAM, is_lead=True, agent_type="TeamArchitect", worker_pools={})
    assert lead.profile.session_limit == 7 and set(lead.profile.agent_types) == catalog
    assert {"tasks.list", "tasks.list_finished", "tasks.remove", "tasks.purge", "debug.dump", "agent.status",
            "agent.pause", "agent.resume", "agent.exit", "task.probe"} == lead.handlers.keys()
    assert {"skills.changed"} == lead.event_handlers.keys()
    catalog.add("Analyst")
    assert set(lead._catalog_types()) == catalog
    worker = process_host_for("agent-test", layout, TEAM, is_lead=False, agent_type="Researcher", worker_pools={})
    assert worker.profile == startup_agent_host_profile(TEAM, "agent-test", "Researcher")


@pytest.mark.asyncio
async def test_a_start_removes_only_the_earlier_agent_records_that_verify_as_its_own(caplog) -> None:
    process, _ = _process()
    mine = SimpleNamespace(signer=SimpleNamespace(runtime="lead"))
    theirs = SimpleNamespace(signer=SimpleNamespace(runtime="agent-Researcher"))
    stored: dict[str, Any] = {
        f"{TEAM}.forged": FrameRefused("bad_signature", at="/signature"),
        f"{TEAM}.removed": None,
        f"{TEAM}.broken": RuntimeError("nats: timeout"),
        f"{TEAM}.old_1": StoredRecord(4, mine),  # type: ignore[arg-type]
        f"{TEAM}.other_host": StoredRecord(2, theirs),  # type: ignore[arg-type]
    }
    records = process.host.records  # type: ignore[union-attr]

    async def keys(record: str, prefix: str) -> list[str]:
        return [*stored, "other_team.z"]

    async def read_report(record: str, key: str, *, now: datetime) -> Optional[StoredRecord]:
        assert record == "agent"
        found = stored[key]
        if isinstance(found, Exception):
            raise found
        return found

    records.keys, records.read_report = keys, read_report
    with caplog.at_level(logging.WARNING, logger=agent_host.__name__):
        await process._remove_earlier_records()
    assert records.removed == [f"{TEAM}.old_1"]
    assert "event=agent_host_record_refused" in caplog.text and f"key={TEAM}.forged" in caplog.text
    assert "event=agent_record_stale_remove_failed" in caplog.text

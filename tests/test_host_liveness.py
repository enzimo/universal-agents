"""How often a host writes its liveness record and how long each lasts (custom_transport.protocol1.host_runtime).

A crashed host must show as gone about 30 seconds after its last write, as the presence
heartbeat did, while a running host never looks gone because one write was lost. The
liveness loop runs here on a fake clock: its waits return at once and move the clock on.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_transport.protocol1 import host_runtime
from custom_transport.protocol1.host_runtime import LIVENESS_INTERVAL, LIVENESS_LIFETIME, MessageHost
from custom_transport.protocol1.joining import HostProfile

START = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)


class _Records:
    """Stands in for ``StateRecords``: keeps what the host signs and writes."""

    def __init__(self) -> None:
        self.written: list[dict] = []
        self.after_write = lambda: None

    def signed(self, record, key, body, *, sequence, issued_at, expires_at):
        return {"record": record, "key": key, "state": body["state"], "sequence": sequence,
                "issued_at": issued_at, "expires_at": expires_at}

    async def put(self, record, key, document):
        self.written.append(document)
        self.after_write()


def _host(now: list[datetime]) -> tuple[MessageHost, _Records]:
    records = _Records()
    host = MessageHost(layout=None, profile=SimpleNamespace(runtime="lead-host"), nc=None, clock=lambda: now[0])
    host.records = records
    host.liveness_body = lambda state: {"state": state}  # type: ignore[method-assign]

    async def nothing() -> None:
        return None

    host.refresh_trust = nothing  # type: ignore[method-assign]
    host.renew_if_due = nothing  # type: ignore[method-assign]
    return host, records


def _fake_waits(monkeypatch, now: list[datetime]) -> list[float]:
    """The liveness loop's waits return at once and move the clock on; the trust upkeep idles."""

    waits: list[float] = []

    async def stopped_within(stop: asyncio.Event, interval: timedelta) -> bool:
        if stop.is_set():
            return True
        waits.append(interval.total_seconds())
        now[0] += interval
        await asyncio.sleep(0)
        return stop.is_set()

    async def idle(self, stop: asyncio.Event) -> None:
        await stop.wait()

    monkeypatch.setattr(host_runtime, "_stopped_within", stopped_within)
    monkeypatch.setattr(MessageHost, "_trust_loop", idle)
    return waits


@pytest.mark.asyncio
async def test_a_host_writes_its_liveness_every_10_seconds_and_each_record_lasts_30(monkeypatch) -> None:
    now = [START]
    waits = _fake_waits(monkeypatch, now)
    host, records = _host(now)
    stop = asyncio.Event()
    records.after_write = lambda: len(records.written) == 4 and stop.set()

    await host.keep_up(stop)

    assert waits == [10.0] * 4
    accepting = [document for document in records.written if document["state"] == "accepting"]
    assert [document["issued_at"] - START for document in accepting] == [timedelta(seconds=10 * n) for n in (1, 2, 3, 4)]
    assert all(document["expires_at"] - document["issued_at"] == timedelta(seconds=30) for document in records.written)
    # Each write lands while the one before still holds, with two more writes' time to spare.
    for earlier, later in zip(accepting, accepting[1:]):
        assert later["expires_at"] > earlier["expires_at"]
        assert earlier["expires_at"] - later["issued_at"] == 2 * LIVENESS_INTERVAL
    assert records.written[-1]["state"] == "drained"


@pytest.mark.asyncio
async def test_a_host_that_stops_writing_shows_as_gone_30_seconds_after_its_last_write() -> None:
    now = [START]
    host, records = _host(now)
    await host.publish_liveness("accepting")
    last = records.written[-1]

    # Crashed right after writing: the record holds for 30 seconds, then readers drop it.
    assert last["expires_at"] == START + LIVENESS_LIFETIME == START + timedelta(seconds=30)
    assert LIVENESS_LIFETIME == 3 * LIVENESS_INTERVAL


class _RenewingLive:
    """Stands in for ``LiveHost``: keeps each renewal body and answers it with a refusal."""

    def __init__(self, certificate) -> None:
        self.identity = SimpleNamespace(certificate=certificate)
        self.bodies: list[dict] = []

    async def request(self, kind, body, audience, *, team):
        self.bodies.append(body)
        return SimpleNamespace(raw={"body": {"outcome": "denied"}})


async def _renewal_id(certificate, contract_sha256: str = host_runtime.CONTRACT_SHA256) -> str:
    host = MessageHost(layout=None, profile=HostProfile(certificate.runtime, ("team_one",), ("worker_host",)), nc=None,
                       contract_sha256=contract_sha256)
    host.live = _RenewingLive(certificate)  # type: ignore[assignment]
    await host.renew()
    return host.live.bodies[-1]["renew_id"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_renewal_keeps_its_id_on_retries_and_takes_a_new_one_after_an_upgrade_in_place(monkeypatch) -> None:
    """README "Contract digest": a host upgraded in place renews with a new renew_id, since a repeated one gets
    its stored outcome back ("Stored outcomes")."""

    certificate = SimpleNamespace(installation="install_one", runtime="worker-host-1", certificate_id="cert.worker-host-1.3",
                                  membership_epoch=3)
    first = await _renewal_id(certificate)
    assert await _renewal_id(certificate) == first  # a retry of the same renewal
    monkeypatch.setattr(host_runtime, "release_version", lambda: "99.0.0")
    upgraded = await _renewal_id(certificate)
    new_contract = await _renewal_id(certificate, "0" * 64)
    assert len({first, upgraded, new_contract}) == 3
    # The longest runtime id and epoch still fit the contract's record id, whole.
    longest = SimpleNamespace(installation="install_one", runtime="r" * 64, certificate_id="cert." + "r" * 64 + ".1",
                              membership_epoch=2 ** 53 - 1)
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", await _renewal_id(longest))


def _reporting_host(components) -> MessageHost:
    """A host whose record body is built for real, from a stand-in certificate and trust store."""

    certificate = SimpleNamespace(installation="install_one", runtime="lead", certificate_id="cert.lead.1",
                                  membership_epoch=1, session_limit=4, revocation_epoch=1)
    host = MessageHost(layout=None, profile=HostProfile("lead", ("team_one",), ("lead_host",)), nc=None,
                       clock=lambda: START, components=lambda: components)
    host.live = SimpleNamespace(identity=SimpleNamespace(certificate=certificate),  # type: ignore[assignment]
                                trust=SimpleNamespace(store=SimpleNamespace(held_record=lambda record, key: (1, "A" * 43))))
    return host


def test_a_host_lists_the_components_that_are_not_healthy_in_its_record() -> None:
    """Plan 13B.5: the Status page's findings come from the hosts' records; the contract allows 16 components."""

    from custom_messages.wire.kinds import STATE_RECORDS

    healthy = _reporting_host([]).liveness_body("accepting")
    assert (healthy["components"], healthy["health"]) == ([], "healthy")

    refused = [{"component": f"Researcher-agent_{n}", "state": "unhealthy", "code": "session_limit_reached"}
               for n in range(20)]
    body = _reporting_host(refused).liveness_body("accepting")
    assert body["components"] == refused[:host_runtime.HOST_COMPONENTS_MAX] and len(body["components"]) == 16
    assert body["health"] == "degraded"  # the host itself still serves its other agents
    STATE_RECORDS["host"].body_model.validate_python(body)


def test_a_host_whose_components_cannot_be_read_still_reports_its_liveness(caplog) -> None:
    def broken():
        raise RuntimeError("no components")

    body = _reporting_host(broken).liveness_body("accepting")  # type: ignore[arg-type]
    assert (body["components"], body["state"]) == ([], "accepting")
    assert "event=protocol1_host_components_failed" in caplog.text


@pytest.mark.asyncio
async def test_a_host_writes_its_record_again_at_once_when_asked_and_never_after_it_drained() -> None:
    now = [START]
    host, records = _host(now)
    await host.report_now()
    assert records.written == []  # not reported yet: the first record is the host's own
    await host.publish_liveness("accepting")
    await host.report_now()
    assert [document["state"] for document in records.written] == ["accepting", "accepting"]
    assert records.written[1]["sequence"] > records.written[0]["sequence"]
    await host.publish_liveness("drained")
    await host.report_now()
    assert [document["state"] for document in records.written] == ["accepting", "accepting", "drained"]


OTHER_CONTRACT = "0" * 64


class _MismatchLive:
    """Stands in for ``LiveHost``: answers the first renewal ``protocol_mismatch`` and every later one ``ok``."""

    def __init__(self, certificate) -> None:
        self.identity = SimpleNamespace(certificate=certificate, with_certificate=lambda renewed: SimpleNamespace(
            certificate=renewed))
        self.bodies: list[dict] = []
        self.trust = SimpleNamespace(add=lambda data, now: True, store=SimpleNamespace(
            host_certificate=lambda installation, runtime, epoch: SimpleNamespace(
                **{**vars(certificate), "membership_epoch": epoch, "agent_types": frozenset(), "session_limit": 0})))

    async def request(self, kind, body, audience, *, team):
        self.bodies.append(body)
        if len(self.bodies) == 1:
            return SimpleNamespace(raw={"body": {"outcome": "protocol_mismatch", "accepted": "1.0-pre",
                                                 "accepted_contract_sha256": OTHER_CONTRACT}})
        return SimpleNamespace(raw={"body": {"outcome": "ok", "certificate": {"body": {"membership_epoch": 4}}}})


@pytest.mark.asyncio
async def test_a_host_that_learns_it_runs_another_contract_drains_and_renews_only_to_give_up_its_agents(caplog) -> None:
    """README "Versions": a host on another contract learns it from ``protocol_mismatch``, says ``draining`` and then
    ``drained`` in its record, and renews only to give up its agents (``HostProfile.giving_up``)."""

    now = [START]
    profile = HostProfile("worker-host-1", ("team_one",), ("worker_host",), ("researcher",), 2)
    certificate = SimpleNamespace(installation="install_one", runtime="worker-host-1", certificate_id="cert.worker-host-1.3",
                                  membership_epoch=3, issued_at=START - timedelta(days=21),
                                  expires_at=START + timedelta(days=9), agent_types=frozenset({"researcher"}),
                                  session_limit=2)
    host, records = _host(now)
    host.profile = profile
    host.renew_if_due = MessageHost.renew_if_due.__get__(host)  # type: ignore[method-assign]
    live = _MismatchLive(certificate)
    host.live = live  # type: ignore[assignment]
    learned: list[str] = []
    host.on_other_contract(learned.append)
    await host.publish_liveness("accepting")

    await host.renew_if_due()  # due: two thirds of its life are gone
    assert learned == [OTHER_CONTRACT] and host.installation_contract == OTHER_CONTRACT
    assert live.bodies[0]["profile_sha256"] == profile.digest()
    assert caplog.text.count("event=protocol1_host_other_contract") == 1

    # While it drains it asks for no renewal: only one that gives up every agent is answered.
    await host.renew_if_due()
    assert len(live.bodies) == 1
    await host.drain()
    assert records.written[-1]["state"] == "draining"
    host.learned_other_contract(OTHER_CONTRACT, "team_services_record")
    assert learned == [OTHER_CONTRACT]  # told once

    await host.finish_drain()
    assert records.written[-1]["state"] == "drained"
    give_up = live.bodies[-1]
    assert give_up["profile_sha256"] == profile.giving_up().digest() != profile.digest()
    assert give_up["renew_id"] != live.bodies[0]["renew_id"]
    assert host.identity.certificate.membership_epoch == 4 and host.identity.certificate.session_limit == 0
    await host.report_now()
    assert records.written[-1]["state"] == "drained"


def test_a_host_record_names_the_contract_the_host_runs() -> None:
    host = _reporting_host([])
    assert host.liveness_body("accepting")["contract_sha256"] == host_runtime.CONTRACT_SHA256
    host.contract_sha256 = OTHER_CONTRACT
    assert host.liveness_body("draining")["contract_sha256"] == OTHER_CONTRACT


@pytest.mark.asyncio
async def test_a_renewal_that_gives_up_the_agents_and_gets_no_answer_is_asked_again_until_answered(caplog) -> None:
    """A drained host on another contract must still give its agents up; the upkeep asks again, then stops."""

    from custom_transport.protocol1.live import ServiceUnavailable

    now = [START]
    profile = HostProfile("worker-host-1", ("team_one",), ("worker_host",), ("researcher",), 2)
    certificate = SimpleNamespace(installation="install_one", runtime="worker-host-1", certificate_id="cert.worker-host-1.3",
                                  membership_epoch=3, issued_at=START, expires_at=START + timedelta(days=30),
                                  agent_types=frozenset({"researcher"}), session_limit=2)
    host, records = _host(now)
    host.profile = profile
    host.renew_if_due = MessageHost.renew_if_due.__get__(host)  # type: ignore[method-assign]
    live = _MismatchLive(certificate)
    answers = [ServiceUnavailable("host.renew", "subject", "no answer in time")]
    answer = live.request

    async def request(kind, body, audience, *, team):
        if answers:
            live.bodies.append(body)
            raise answers.pop(0)
        return await answer(kind, body, audience, team=team)

    live.request = request  # type: ignore[method-assign]
    host.live = live  # type: ignore[assignment]
    host.installation_contract = OTHER_CONTRACT
    await host.publish_liveness("draining")

    await host.finish_drain()
    assert "event=protocol1_give_up_renewal_failed" in caplog.text
    assert records.written[-1]["state"] == "drained" and len(live.bodies) == 1
    await host.renew_if_due()  # the next upkeep, though no renewal is due: answered now (protocol_mismatch)
    await host.renew_if_due()
    assert len(live.bodies) == 2
    assert all(body["profile_sha256"] == profile.giving_up().digest() for body in live.bodies)


# --- liveness never waits for the trust and certificate upkeep ------------------------------------


def _quick_upkeep(monkeypatch) -> None:
    """Real waits, short: liveness every 20 ms, the trust upkeep looking every 5 ms."""

    monkeypatch.setattr(host_runtime, "LIVENESS_INTERVAL", timedelta(milliseconds=20))
    monkeypatch.setattr(host_runtime, "UPKEEP_CHECK_INTERVAL", timedelta(milliseconds=5))


@pytest.mark.asyncio
@pytest.mark.parametrize("renewal", ["hangs", "fails"])
async def test_a_renewal_that_waits_or_fails_never_stops_the_liveness_writes(monkeypatch, caplog, renewal: str) -> None:
    """A renewal waits up to 30 s for the authority, a join for more agent types holds the certificate's lock for
    up to 120 s, and a refused renewal is asked again every pass: none of it may let the 30-second record lapse."""

    _quick_upkeep(monkeypatch)
    now = [START]
    host, records = _host(now)
    host._last_trust_refresh = host._last_forget = START  # only the renewal is due
    renewals: list[int] = []

    async def renew_if_due() -> None:
        renewals.append(1)
        if renewal == "hangs":
            await asyncio.Event().wait()
        raise RuntimeError("the authority refused the renewal")

    host.renew_if_due = renew_if_due  # type: ignore[method-assign]
    stop = asyncio.Event()
    keeping = asyncio.create_task(host.keep_up(stop))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(keeping, timeout=1)

    accepting = [document for document in records.written if document["state"] == "accepting"]
    assert renewals and len(accepting) >= 4, "liveness kept its cadence while the renewal waited or failed"
    assert records.written[-1]["state"] == "drained"


@pytest.mark.asyncio
async def test_a_stopped_host_writes_no_more_records_while_a_renewal_still_waits(monkeypatch) -> None:
    """A team services instance that lost its lock stops its upkeep: a renewal still waiting is cut off rather than
    waited for, and no record follows that would overwrite the one of the instance serving now."""

    _quick_upkeep(monkeypatch)
    now = [START]
    host, records = _host(now)
    host._last_trust_refresh = host._last_forget = START
    waiting = asyncio.Event()

    async def renew_if_due() -> None:
        waiting.set()
        await asyncio.Event().wait()

    host.renew_if_due = renew_if_due  # type: ignore[method-assign]
    stop = asyncio.Event()
    keeping = asyncio.create_task(host.keep_up(stop, drained=lambda: False))
    await waiting.wait()
    stop.set()
    written = len(records.written)
    await asyncio.wait_for(keeping, timeout=1)  # not held up by the renewal
    await asyncio.sleep(0.05)
    assert len(records.written) == written


@pytest.mark.asyncio
async def test_a_renewal_that_is_not_due_does_not_wait_for_the_certificates_lock() -> None:
    """A join for more agent types may hold the certificate's lock for minutes; a renewal check that is not due
    must not queue behind it."""

    now = [START]
    host, _records = _host(now)
    host.renew_if_due = MessageHost.renew_if_due.__get__(host)  # type: ignore[method-assign]
    certificate = SimpleNamespace(issued_at=START, expires_at=START + timedelta(days=30))
    host.live = SimpleNamespace(identity=SimpleNamespace(certificate=certificate))  # type: ignore[assignment]
    async with host._certificate_change:  # as rejoin() holds it
        await asyncio.wait_for(host.renew_if_due(), timeout=0.5)


@pytest.mark.asyncio
async def test_the_upkeep_forgets_trust_records_nothing_can_still_need(monkeypatch) -> None:
    """README "Trust over time": a receiver calls TrustStore.forget at least daily, so its trust store stays bounded."""

    _quick_upkeep(monkeypatch)
    now = [START]
    host, _records = _host(now)
    host._last_trust_refresh = START
    forgotten: list[datetime] = []
    host.live = SimpleNamespace(trust=SimpleNamespace(store=SimpleNamespace(  # type: ignore[assignment]
        forget=lambda moment: forgotten.append(moment) or 2)))
    stop = asyncio.Event()
    keeping = asyncio.create_task(host.keep_up(stop, drained=lambda: False))
    await asyncio.sleep(0.05)
    assert forgotten == [START], "forgotten once, then not again until FORGET_INTERVAL has passed"
    now[0] = START + host_runtime.FORGET_INTERVAL
    await asyncio.sleep(0.05)
    stop.set()
    await keeping
    assert forgotten == [START, START + host_runtime.FORGET_INTERVAL]
    assert host_runtime.FORGET_INTERVAL <= timedelta(days=1)


def test_a_component_the_contract_cannot_carry_is_left_out_and_logged(caplog) -> None:
    """One bad component, such as an id longer than the contract's 64 characters, must not make every host
    record fail its schema check and the host disappear from every view."""

    from custom_messages.wire.kinds import STATE_RECORDS

    good = {"component": "Researcher-agent_1", "state": "unhealthy", "code": "session_limit_reached"}
    bad = [{"component": "r" * 73, "state": "unhealthy", "code": "session_limit_reached"},
           {"component": "Researcher-agent_2", "state": "broken"}]
    host = _reporting_host([good, *bad])
    body = host.liveness_body("accepting")
    assert body["components"] == [good]
    STATE_RECORDS["host"].body_model.validate_python(body)
    assert caplog.text.count("event=protocol1_host_components_invalid") == 1
    host.liveness_body("accepting")
    assert caplog.text.count("event=protocol1_host_components_invalid") == 1, "logged once per new set"


# --- looking up records a frame names ---------------------------------------------------------------


class _LookupRecords:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.asked: list[str] = []

    async def take_trust_record(self, record, key, *, now):
        self.asked.append(key)
        raise self.error


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("nats: timeout"), OSError("connection reset")])
async def test_a_lookup_that_fails_takes_nothing_and_is_logged(caplog, error: BaseException) -> None:
    """A NATS failure while reading a certificate a frame names must end as the frame's own refusal, answered at
    once, not as an error that leaves the requester waiting or ends the contract watch."""

    import json

    host = MessageHost(layout=None, profile=SimpleNamespace(runtime="lead-host"), nc=None, clock=lambda: START)
    records = _LookupRecords(error)
    host.records = records  # type: ignore[assignment]
    host._trust = SimpleNamespace(store=SimpleNamespace(installation="install_one"))  # type: ignore[assignment]
    frame = {"issuer": {"installation": "install_one", "runtime": "worker-host-1", "membership_epoch": 3, "team": "t",
                        "session": "s-researcher-1-4"}}

    assert await host.fetch_trust(json.dumps(frame).encode()) is False
    assert records.asked == ["host.install_one.worker-host-1.3", "session.install_one.t.s-researcher-1-4"]
    assert caplog.text.count("event=protocol1_trust_lookup_failed") == 2


@pytest.mark.asyncio
async def test_the_contract_watch_keeps_running_through_a_failed_watch(monkeypatch, caplog) -> None:
    monkeypatch.setattr(host_runtime, "_FOLLOW_RETRY_SECONDS", 0.0)
    host = MessageHost(layout=None, profile=SimpleNamespace(runtime="lead-host"), nc=None, clock=lambda: START)
    watches: list[int] = []
    seen: list[bytes] = []

    class _Watching:
        async def watch(self, record, key):
            watches.append(1)
            if len(watches) == 1:
                raise TimeoutError("nats: timeout")
            yield key, b"record"
            await asyncio.Event().wait()

    async def contract_record(key, raw, runtime) -> None:
        seen.append(raw)

    host.records = _Watching()  # type: ignore[assignment]
    host.live = SimpleNamespace()  # type: ignore[assignment]
    host._contract_record = contract_record  # type: ignore[method-assign]
    following = asyncio.create_task(host.follow_contract())
    while not seen:
        await asyncio.sleep(0.01)
    following.cancel()
    assert len(watches) == 2 and seen == [b"record"]
    assert "event=protocol1_contract_watch_failed" in caplog.text

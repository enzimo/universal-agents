"""How often a host writes its liveness record and how long each lasts (custom_transport.protocol1.host_runtime).

A crashed host must show as gone about 30 seconds after its last write, as the presence
heartbeat did, while a running host never looks gone because one write was lost. The
upkeep loop runs here on a fake clock: its waits return at once and move the clock on.
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


@pytest.mark.asyncio
async def test_a_host_writes_its_liveness_every_10_seconds_and_each_record_lasts_30(monkeypatch) -> None:
    now = [START]
    waits: list[float] = []

    async def wait_for(awaitable, timeout):
        awaitable.close()  # the stop event's wait, never started
        waits.append(timeout)
        now[0] += timedelta(seconds=timeout)
        raise asyncio.TimeoutError

    monkeypatch.setattr(host_runtime, "asyncio", SimpleNamespace(wait_for=wait_for, TimeoutError=asyncio.TimeoutError))
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

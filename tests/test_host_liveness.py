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


async def _renewal_id(certificate) -> str:
    host = MessageHost(layout=None, profile=HostProfile(certificate.runtime, ("team_one",), ("worker_host",)), nc=None)
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
    monkeypatch.setattr(host_runtime, "contract_sha256", lambda: "0" * 64)
    new_contract = await _renewal_id(certificate)
    assert len({first, upgraded, new_contract}) == 3
    # The longest runtime id and epoch still fit the contract's record id, whole.
    longest = SimpleNamespace(installation="install_one", runtime="r" * 64, certificate_id="cert." + "r" * 64 + ".1",
                              membership_epoch=2 ** 53 - 1)
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", await _renewal_id(longest))

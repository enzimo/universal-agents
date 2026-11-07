"""The team services process around its lock (team_services.process): what it does before serving, how it ends.

The process on a real NATS server is covered by tests/test_live_team_services.py and
tests/test_live_launcher_message_services.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from team_services import process
from team_services.lock import LockConnectionClosed, LockLost


@pytest.mark.asyncio
async def test_a_takeover_reads_the_newest_certificate_and_the_current_revocations_before_serving() -> None:
    """A standby may wait for days: the serving instance may have renewed their certificate, and the revocations
    record the standby read when it started lasts a day."""

    steps: list[str] = []

    async def certificate() -> None:
        steps.append("certificate")

    async def revocations() -> None:
        steps.append("revocations")

    await process._prepare(SimpleNamespace(take_newest_certificate=certificate, refresh_trust=revocations))
    assert steps == ["certificate", "revocations"]


class _Adapter:
    def __init__(self, **_kwargs) -> None:
        self.nats_connection = None
        self.closed = False

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", [LockLost("another instance took the lock"), LockConnectionClosed("NATS closed")])
async def test_losing_the_lock_ends_the_process_with_exit_code_1(monkeypatch, caplog, lost: LockLost) -> None:
    """The launcher's supervisor sees the exit and stops the team, rather than two instances serving."""

    async def serve_team(_host, _team, _stop, _families) -> None:
        raise lost

    monkeypatch.setattr(process, "NatsTransportAdapter", _Adapter)
    monkeypatch.setattr(process, "MessageHost", lambda *_args, **_kwargs: SimpleNamespace(clock=None))
    monkeypatch.setattr(process, "team_operations", lambda _config, _clock: [])
    monkeypatch.setattr(process, "serve_team", serve_team)
    transport = SimpleNamespace(nats_servers=["nats://127.0.0.1:4222"], nats_url=None, subject_prefix="ua",
                                nats_user=None, nats_password=None, credentials_file=None)
    config = SimpleNamespace(manager=SimpleNamespace(team_id="team_one"), transport=transport, instance_name="test",
                             layout=None)
    with pytest.raises(SystemExit) as exited:
        await process.run_team_services(config)  # type: ignore[arg-type]
    assert exited.value.code == 1
    assert "event=team_services_stopped" in caplog.text

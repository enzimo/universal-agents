from __future__ import annotations

import asyncio
import signal
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from custom_agents.strands_agent import _direct_consumer_scope
from custom_transport.base import (
    ConsumerScope,
)
from custom_transport.nats_consumer_reconciler import NatsConsumerReconciler
from custom_transport.nats_consumer_state import consumer_listening_state
from custom_transport.nats_pull import CONSUMER_METADATA_SCHEMA
from custom_transport.subjects import NatsSubjectMapper
from scripts.reconcile_nats_consumers import reconcile_nats_consumers, resolve_reconciliation_team_id


def test_direct_consumer_scope_follows_agent_identity_lifetime() -> None:
    assert _direct_consumer_scope(configured_fields={"id"}) == ConsumerScope.ROUTE
    assert _direct_consumer_scope(configured_fields=set()) == ConsumerScope.PROCESS


def test_reconciler_resolves_normalized_instance_config_team_id() -> None:
    config = {
        "agent.default_team_id": "team_ua",
        "agent.bootstrap.team_id": "team_stale",
    }

    assert resolve_reconciliation_team_id(config, environment={}) == "team_ua"


def test_reconciler_rejects_an_unscoped_account() -> None:
    with pytest.raises(RuntimeError, match="Refusing to scan an unscoped"):
        resolve_reconciliation_team_id({}, environment={})


def _consumer_info(
    *,
    name: str,
    filter_subject: str,
    push_bound: bool | None = False,
    metadata: dict[str, str] | None = None,
    deliver_subject: str | None = None,
    age_seconds: float = 7200.0,
    num_ack_pending: int | None = 1,
    num_waiting: int | None = None,
):
    return SimpleNamespace(
        name=name,
        created=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        config=SimpleNamespace(
            filter_subject=filter_subject,
            metadata=metadata,
            deliver_subject=deliver_subject,
        ),
        push_bound=push_bound,
        num_pending=3,
        num_ack_pending=num_ack_pending,
        num_waiting=num_waiting,
    )


class _PagedJetStream:
    def __init__(self, pages_by_stream) -> None:
        self.pages_by_stream = pages_by_stream
        self.offsets = []
        self.deleted = []

    async def consumers_info(self, stream_name, *, offset=0):
        self.offsets.append((stream_name, offset))
        consumers = self.pages_by_stream.get(stream_name, [])
        return consumers[offset : offset + 256]

    async def delete_consumer(self, stream_name, consumer_name):
        self.deleted.append((stream_name, consumer_name))


@pytest.mark.asyncio
async def test_legacy_reconciler_paginates_and_fails_closed() -> None:
    mapper = NatsSubjectMapper(team_id="team_test")
    legacy_consumers = []
    for index in range(257):
        agent_id = f"agent_id_{index:012x}"
        legacy_consumers.append(
            _consumer_info(
                name=mapper.durable_name(f"agent_{agent_id}"),
                filter_subject=mapper.agent_work_subject(agent_id),
            )
        )
    live_agent_id = "agent_id_ffffffffffff"
    legacy_consumers.append(
        _consumer_info(
            name=mapper.durable_name(f"agent_{live_agent_id}"),
            filter_subject=mapper.agent_work_subject(live_agent_id),
            push_bound=True,
        )
    )
    unknown_binding_id = "agent_id_eeeeeeeeeeee"
    legacy_consumers.append(
        _consumer_info(
            name=mapper.durable_name(f"agent_{unknown_binding_id}"),
            filter_subject=mapper.agent_work_subject(unknown_binding_id),
            push_bound=None,
            num_ack_pending=None,
        )
    )
    shared = _consumer_info(
        name=mapper.durable_name("workers_CodeWritingAgent"),
        filter_subject=mapper.named_type_work_subject("CodeWritingAgent"),
    )
    legacy_consumers.append(shared)
    managed_agent_id = "agent_id_dddddddddddd"
    legacy_consumers.append(
        _consumer_info(
            name=mapper.durable_name(f"agent_{managed_agent_id}"),
            filter_subject=mapper.agent_work_subject(managed_agent_id),
            metadata={
                "ua.managed": "true",
                "ua.schema": CONSUMER_METADATA_SCHEMA,
                "ua.team_id": "team_test",
                "ua.consumer_scope": "process",
                "ua.consumer_role": "agent_direct",
                "ua.owner_agent_id": managed_agent_id,
            },
        )
    )
    malformed_agent_id = "agent_id_cccccccccccc"
    legacy_consumers.append(
        _consumer_info(
            name=mapper.durable_name(f"agent_{malformed_agent_id}"),
            filter_subject=mapper.agent_work_subject(malformed_agent_id),
            metadata={
                "ua.managed": "true",
                "ua.schema": CONSUMER_METADATA_SCHEMA,
                "ua.team_id": "team_test",
                "ua.consumer_scope": "process",
                "ua.owner_agent_id": malformed_agent_id,
            },
        )
    )
    jetstream = _PagedJetStream({mapper.work_stream_name(): legacy_consumers})
    reconciler = NatsConsumerReconciler(
        jetstream=jetstream,
        mapper=mapper,
        stable_agent_ids={"agent_id_000000000001"},
    )

    result = await reconciler.reconcile(
        apply=True,
        minimum_age_seconds=3600.0,
    )

    assert result.scanned_count == 262
    assert result.deleted_count == 257
    assert len(jetstream.deleted) == 257
    assert (mapper.work_stream_name(), 256) in jetstream.offsets
    reasons = Counter(decision.reason for decision in result.decisions)
    assert reasons == {
        "abandoned_legacy_process_consumer": 256,
        "abandoned_managed_process_consumer": 1,
        "configured_stable_agent": 1,
        "live_subscription": 1,
        "listening_unknown": 1,
        "unmanaged_or_persistent": 2,
    }


def test_pull_consumers_are_live_while_a_receiver_asks_or_holds_messages() -> None:
    def pull(waiting, held):
        return _consumer_info(
            name="n", filter_subject="s", push_bound=None, num_waiting=waiting, num_ack_pending=held
        )

    assert consumer_listening_state(pull(1, 0)) is True
    assert consumer_listening_state(pull(0, 1)) is True
    assert consumer_listening_state(pull(0, 0)) is False
    # An unbound push consumer left from an earlier release is not listening.
    leftover = _consumer_info(name="n", filter_subject="s", push_bound=None, deliver_subject="d")
    assert consumer_listening_state(leftover) is False


@pytest.mark.asyncio
async def test_reconciler_deletes_an_abandoned_pull_consumer_and_keeps_a_live_one() -> None:
    mapper = NatsSubjectMapper(team_id="team_test")

    def managed(agent_id, *, waiting):
        return _consumer_info(
            name=mapper.durable_name(f"agent_{agent_id}"),
            filter_subject=mapper.agent_work_subject(agent_id),
            push_bound=None,
            num_waiting=waiting,
            num_ack_pending=0,
            metadata={
                "ua.managed": "true",
                "ua.schema": CONSUMER_METADATA_SCHEMA,
                "ua.team_id": "team_test",
                "ua.consumer_scope": "process",
                "ua.consumer_role": "agent_direct",
                "ua.owner_agent_id": agent_id,
            },
        )

    jetstream = _PagedJetStream(
        {mapper.work_stream_name(): [managed("agent_id_aaaaaaaaaaaa", waiting=0), managed("agent_id_bbbbbbbbbbbb", waiting=1)]}
    )
    result = await NatsConsumerReconciler(jetstream=jetstream, mapper=mapper).reconcile(
        apply=True, minimum_age_seconds=60.0
    )
    assert [decision.reason for decision in result.decisions] == [
        "abandoned_managed_process_consumer",
        "live_subscription",
    ]
    assert jetstream.deleted == [(mapper.work_stream_name(), mapper.durable_name("agent_agent_id_aaaaaaaaaaaa"))]


def _closed_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_the_consumer_report_gives_up_on_an_unreachable_server_quickly_and_names_the_error(
    tmp_path: Path, monkeypatch
) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    # Credentials may also be supplied in a URL. The failure must not echo it.
    server = f"nats://alice:example-password@127.0.0.1:{_closed_local_port()}"
    for name in ("NATS_USER", "NATS_PASSWORD", "NATS_CREDENTIALS_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NATS_SERVERS", server)
    monkeypatch.setenv("NATS_URL", server)
    config_path = GatewayInstanceConfig.create(
        instance_name="consumercheck",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    ).save(tmp_path / "instance" / "instance_config.yaml")

    started = time.monotonic()
    with pytest.raises(RuntimeError) as raised:
        # nats-py's defaults retry the first connection for about two minutes.
        await asyncio.wait_for(
            reconcile_nats_consumers(config_path=config_path, apply=False, minimum_age_seconds=0),
            timeout=10,
        )

    assert time.monotonic() - started < 10
    message = str(raised.value)
    assert "Could not connect to NATS:" in message
    assert "example-password" not in message
    assert "ConnectionRefusedError" in message


def test_sigterm_reaches_agent_cleanup_in_subprocess() -> None:
    script = textwrap.dedent(
        """
        import asyncio
        from strands_main import run_agent_with_signal_handlers

        class Agent:
            id = "agent_id_signal_subprocess"

            def __init__(self):
                self.exit_event = asyncio.Event()

            def set_exit_flag(self):
                self.exit_event.set()

            async def connect(self):
                print("READY", flush=True)
                try:
                    await self.exit_event.wait()
                finally:
                    print("CLEANUP", flush=True)

        async def main():
            await run_agent_with_signal_handlers(Agent())

        asyncio.run(main())
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "READY"

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 0, stderr
    assert "CLEANUP" in stdout


def test_spawned_agent_registry_requests_exit_and_joins(monkeypatch) -> None:
    from custom_agents import strands_team

    class FakeFuture:
        def __init__(self) -> None:
            self.waited = False

        def result(self, *, timeout) -> None:
            assert timeout > 0
            self.waited = True

    class FakeThread:
        def __init__(self) -> None:
            self.joined = False

        def join(self, *, timeout) -> None:
            assert timeout > 0
            self.joined = True

        def is_alive(self) -> bool:
            return not self.joined

    class FakeLoop:
        @staticmethod
        def is_running() -> bool:
            return True

    future = FakeFuture()
    thread = FakeThread()

    async def exit_agent() -> None:
        return None

    def fake_run_coroutine_threadsafe(coroutine, loop):
        assert isinstance(loop, FakeLoop)
        coroutine.close()
        return future

    monkeypatch.setattr(
        strands_team.asyncio,
        "run_coroutine_threadsafe",
        fake_run_coroutine_threadsafe,
    )
    agent = SimpleNamespace(id="agent_id_spawned", exit_agent=exit_agent)
    handle = strands_team._SpawnedAgentHandle(
        agent=agent,
        thread=thread,
        team_id="team_test",
        runtime_loop=FakeLoop(),
    )
    with strands_team._SPAWNED_AGENT_LOCK:
        strands_team._SPAWNED_AGENTS[agent.id] = handle
    try:
        strands_team.shutdown_spawned_agents(
            "team_test",
            "agent_id_manager",
            1.0,
        )
    finally:
        with strands_team._SPAWNED_AGENT_LOCK:
            strands_team._SPAWNED_AGENTS.clear()

    assert future.waited is True
    assert thread.joined is True

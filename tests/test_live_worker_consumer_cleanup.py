"""A real team lead starts on a work stream that leftover consumers filled to NATS's limit.

Incident (2026-11-19): every worker started from a start record kept its
mailbox and control consumer on the team's work stream after it exited, until
the stream held NATS 2.15's default limit of 1,000 consumers and every new
consumer was refused (error 10026). The team lead now removes those leftovers
when it starts, before it creates its own consumers and before anything can
start a worker (``custom_agents.worker_consumer_cleanup``).

Review of 2026-11-20 (FAIL-1, FAIL-3, EVD-3, CRIT-1): the first version swept
after the first worker starts, kept leftovers whose start record had been
pruned, never listed push-era consumers, and no test ran the sweep against
real NATS or inside a real lead start. On a server with a per-stream limit
(NATS 2.15 and newer) the stream is filled until NATS refuses, and without the
start sweep this lead cannot start at all: removing the call makes this test
fail with error 10026 on the lead's own mailbox. NATS 2.10 (the oldest
supported, and the one the CI live job runs) has no default limit; there the
stream gets the full number of leftovers, and removing the call fails the test
on the single sweep line instead.

Real NATS server, real start-record and status stores, the real liveness rule,
a real ``UniversalAgent`` lead with a team runtime started through
``connect()``; only the model is replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from config_loader import Config
from custom_agents.agent_team_runtime import LocalAgentTeamRuntime
from custom_messages.contracts.schedule_worker_v1 import AgentLaunchState
from custom_transport.base import AGENT_CONTROL_ROLE, AGENT_DIRECT_ROLE, ConsumerScope
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.nats_consumer_state import server_version_of, version_label
from custom_transport.nats_pull import PUSH_CONSUMER_METADATA_SCHEMA, ensure_pull_consumer
from custom_transport.subjects import NatsSubjectMapper
from id_generation import new_id
from live_nats_client import nats_transport_config_from_env
from storage.agent_launch_repository import AgentLaunchRepository, LaunchCapacity
from test_live_multi_agent_state_boundaries import FakeStrandsAgent, _agent, _stop_agents
from test_schedule_worker_transactions import make_launch

pytestmark = [pytest.mark.asyncio, pytest.mark.live, pytest.mark.nats]
log = logging.getLogger("test.live_worker_consumer_cleanup")

UNCHECKABLE = "agent_unchecked_run"  # a start of another run that cannot be proven gone
STARTUP_AGENT = "agent_id_0123456789ab"  # process-owned consumers, no status row yet
ASKING = "agent_hand_run"  # not configured, but a request is open
CONFIGURED = "agent_configured"  # agent.agent_id in the saved configuration
PUSH_ERA = "agent_push_era"  # a push-delivery release's started worker
MAIL_OWNER = "agent_pruned_0"  # its mailbox holds a message when it is removed
# More than NATS 2.15's default limit allows (1,000 consumers per stream).
PRUNED_OWNERS = 600


def _names(mapper: NatsSubjectMapper, owner: str, role: str) -> tuple[str, str]:
    if role == AGENT_DIRECT_ROLE:
        return mapper.agent_work_subject(owner), mapper.durable_name(f"agent_{owner}")
    return mapper.agent_control_subject(owner), mapper.durable_name(f"agent_{owner}_control")


def _config(adapter: NatsTransportAdapter, owner: str, role: str, scope: ConsumerScope):
    subject, durable = _names(adapter.mapper, owner, role)
    return adapter._consumer_config(  # noqa: SLF001 - the config the runtime itself creates
        durable_name=durable,
        filter_subject=subject,
        deliver_last_per_subject=False,
        consumer_scope=scope,
        consumer_role=role,
        owner_agent_id=owner,
        ack_wait_seconds=float(adapter.config.ack_wait_seconds),
        max_deliver=int(adapter.config.max_deliver or 1),
    )


async def _leftover(adapter: NatsTransportAdapter, owner: str, scope: ConsumerScope = ConsumerScope.ROUTE) -> None:
    for role in (AGENT_DIRECT_ROLE, AGENT_CONTROL_ROLE):
        await ensure_pull_consumer(
            adapter._js,  # noqa: SLF001
            stream_name=adapter.mapper.work_stream_name(),
            config=_config(adapter, owner, role, scope),
            logger=log,
        )


async def _push_era_leftover(adapter: NatsTransportAdapter, owner: str) -> None:
    """A lasting push consumer as the releases before pull delivery created them."""

    for role in (AGENT_DIRECT_ROLE, AGENT_CONTROL_ROLE):
        config = _config(adapter, owner, role, ConsumerScope.ROUTE)
        config.deliver_subject = f"deliver.{new_id('push')}"
        config.metadata = {**config.metadata, "ua.schema": PUSH_CONSUMER_METADATA_SCHEMA}
        await adapter._js.add_consumer(adapter.mapper.work_stream_name(), config=config)  # noqa: SLF001


def _team_runtime(spawn_agent: MagicMock) -> LocalAgentTeamRuntime:
    return LocalAgentTeamRuntime(
        agent_types=lambda: ("TeamArchitect",),
        spawn_agent=spawn_agent,
        shutdown_agents=lambda *args, **kwargs: None,
        resolve_definition=lambda agent_type: None,
        # Starts of the previous process are gone, except one whose run cannot be checked.
        inspect_launch=lambda record: "unknown" if record.agent_id == UNCHECKABLE else "absent",
        stop_launch=lambda record: True,
    )


async def _wait_for_open_request(adapter: NatsTransportAdapter, owner: str) -> None:
    _, durable = _names(adapter.mapper, owner, AGENT_DIRECT_ROLE)
    async with asyncio.timeout(10):
        while not (await adapter._js.consumer_info(adapter.mapper.work_stream_name(), durable)).num_waiting:  # noqa: SLF001
            await asyncio.sleep(0.1)


# What the lead logs at the end of its start, after creating both loops.
LEAD_RUNNING = "NATS transport ready; waiting for messages until exit."


def _logged(caplog, text: str) -> bool:
    return any(text in record.getMessage() for record in caplog.records)


def _events(records) -> list[str]:
    markers = (
        ("event=nats_agent_consumer_deleted", "deleted"),
        ("event=worker_consumer_cleanup_done", "sweep_done"),
        ("event=worker_consumer_cleanup_failed", "sweep_failed"),
        ("NATS direct route ready", "lead_mailbox_ready"),
    )
    events = [next((name for marker, name in markers if marker in r.getMessage()), None) for r in records]
    return list(dict.fromkeys(event for event in events if event))


async def test_live_a_lead_starts_on_a_work_stream_full_of_leftovers(tmp_path, monkeypatch, caplog) -> None:
    team_id = new_id("team_full")
    mapper = NatsSubjectMapper(team_id=team_id)
    stream = mapper.work_stream_name()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inspector = NatsTransportAdapter(config=nats_transport_config_from_env(), mapper=mapper)
    asker = NatsTransportAdapter(config=nats_transport_config_from_env(), mapper=mapper)
    with (
        patch("custom_agents.strands_agent.Agent", side_effect=[FakeStrandsAgent()]),
        patch("custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()),
    ):
        lead = _agent(
            agent_id=new_id("manager"), name="manager", team_id=team_id, named_agent_type="TeamArchitect",
            db_path=tmp_path / "lead.db", workspace_dir=workspace, is_lead=True,
        )
    # Checked at the end: the lead's loops log their own errors, so a raising
    # stand-in could not fail the test.
    spawn_agent = MagicMock()
    lead._local_team_runtime = _team_runtime(spawn_agent)
    # The saved configuration names one agent id (the real helpers read it).
    monkeypatch.setattr(
        "custom_agents.worker_consumer_cleanup.current_launch_config",
        lambda agent: Config({"agent": {"agent_id": CONFIGURED}}),
    )
    repository = AgentLaunchRepository(lead.task_manager.storage_settings)

    def record(agent_id: str, state: AgentLaunchState) -> None:
        launch, _ = repository.reserve(
            make_launch(agent_id=agent_id, team_id=team_id, named_agent_type="Worker", source="pool"),
            LaunchCapacity.defaults(),
            join=False,
        )
        launch = repository.update(launch, state=AgentLaunchState.STARTING)
        if state is not AgentLaunchState.STARTING:
            repository.update(launch, state=state)

    agent_tasks = []
    try:
        await inspector.connect(reconcile_topology=True)
        await asker.connect()
        record(UNCHECKABLE, AgentLaunchState.READY)
        await _leftover(inspector, UNCHECKABLE)
        await _leftover(inspector, STARTUP_AGENT, ConsumerScope.PROCESS)
        await _leftover(inspector, CONFIGURED)
        await _push_era_leftover(inspector, PUSH_ERA)
        async def unused(delivery) -> None:
            raise AssertionError("nothing is sent to this mailbox")

        await asker.subscribe_route(
            name=f"{ASKING}:direct", routing_key=ASKING, handler=unused,
            durable_name=mapper.durable_name(f"agent_{ASKING}"), consumer_role=AGENT_DIRECT_ROLE,
            owner_agent_id=ASKING, ready=lambda: True,
        )
        for index in range(3):
            record(f"agent_retired_{index}", AgentLaunchState.RETIRED)
            await _leftover(inspector, f"agent_retired_{index}")
        # Start records pruned (no record, no status row): fill until NATS
        # refuses, or to 600 owners (1,200 consumers) on a server without a limit.
        refused = None
        for index in range(PRUNED_OWNERS):
            try:
                await _leftover(inspector, f"agent_pruned_{index}")
            except Exception as exc:  # NATS 10026, the stream's consumer limit
                refused = exc
                break
        # NATS 2.15 refuses at its default per-stream limit (error 10026);
        # NATS 2.10 has no default limit and accepts every leftover.
        assert refused is None or "10026" in str(refused), refused
        before = await inspector.agent_consumers()
        # What the server reports: on 2.15 its default of 1,000 applies (no limit is
        # configured); a server without the default (2.10) reports no limit.
        capacity = await inspector.stream_consumer_capacity()
        if refused is not None:
            assert (capacity.consumer_limit, capacity.limit_source) == (1000, "nats_default")
            assert capacity.server_version >= (2, 15) and capacity.near_limit
        else:
            assert capacity.consumer_limit is None
        assert len(before) > 3 * 256  # more than three listing pages
        await inspector._nc.publish(mapper.agent_work_subject(MAIL_OWNER), b"left in the mailbox")  # noqa: SLF001
        await inspector._nc.flush()  # noqa: SLF001
        messages_before = (await inspector._js.stream_info(stream)).state.messages  # noqa: SLF001
        await _wait_for_open_request(inspector, ASKING)

        with caplog.at_level(logging.INFO):
            began = time.monotonic()
            task = asyncio.create_task(lead.connect())
            agent_tasks.append((lead, task))
            # Wait past the creation of the schedule and scheduler loops (logged
            # after them), so a second start sweep would be counted below.
            while not _logged(caplog, LEAD_RUNNING) and not task.done() and time.monotonic() - began < 90:
                await asyncio.sleep(0.2)
        assert not task.done() and lead._worker_ready, task.exception() if task.done() else "not ready"
        assert lead._local_schedule_task is not None and lead._scheduler_task is not None

        done = [r.getMessage() for r in caplog.records if "event=worker_consumer_cleanup_done" in r.getMessage()]
        assert len(done) == 1 and "sweep=start" in done[0], done
        pruned = {c.owner_agent_id for c in before if c.owner_agent_id.startswith("agent_pruned_")}
        gone = pruned | {PUSH_ERA} | {f"agent_retired_{index}" for index in range(3)}
        deleted = sum(c.owner_agent_id in gone for c in before)
        assert (
            f"agent_consumers={len(before)} lasting_consumers={len(before) - 2} push_era_consumers=2 "
            f"configured_agents=1 kept_listening=1 kept_not_gone=1 kept_evidence={{'launch_active': 1}} "
            f"gone_with_start_record=3 "
            f"gone_without_start_record={len(pruned) + 1} deleted={deleted} next_sweep_in_seconds=3600.0"
        ) in done[0]
        # The lead reads the stream again after its deletions, before its own consumers exist.
        assert (
            f"stream_consumer_count={capacity.consumer_count - deleted} stream_consumer_limit={capacity.consumer_limit} "
        ) in done[0]
        assert _events(caplog.records)[:3] == ["deleted", "sweep_done", "lead_mailbox_ready"]
        remaining = {consumer.owner_agent_id for consumer in await inspector.agent_consumers()}
        assert remaining == {UNCHECKABLE, STARTUP_AGENT, CONFIGURED, ASKING, lead.id}

        # The deleted mailbox's message is still in the stream, and a consumer
        # created again with the same name and filter delivers it.
        assert (await inspector._js.stream_info(stream)).state.messages == messages_before  # noqa: SLF001
        await ensure_pull_consumer(
            inspector._js, stream_name=stream,  # noqa: SLF001
            config=_config(inspector, MAIL_OWNER, AGENT_DIRECT_ROLE, ConsumerScope.PROCESS), logger=log,
        )
        _, durable = _names(mapper, MAIL_OWNER, AGENT_DIRECT_ROLE)
        again = await inspector._js.pull_subscribe_bind(durable=durable, stream=stream)  # noqa: SLF001
        (message,) = await again.fetch(1, timeout=5)
        assert message.data == b"left in the mailbox"
        await message.ack()
        spawn_agent.assert_not_called()
    finally:
        await _stop_agents(agent_tasks)
        await asker.close()
        if inspector._js is not None:  # noqa: SLF001
            with contextlib.suppress(Exception):
                await inspector._js.delete_stream(stream)  # noqa: SLF001
        await inspector.close()


async def test_live_the_start_up_check_removes_lasting_leftovers_and_keeps_their_messages(tmp_path) -> None:
    """The launcher's check before any agent starts, run as the launcher runs it.

    The script runs as a separate process with the launcher's own arguments
    (``REMOVE_LASTING_LEFTOVERS_ARGS``) and reads the configured agent id from
    its configuration, so the whole path is covered: arguments, which kinds are
    deleted, and which ids are protected. Only the minimum age is lowered,
    because these consumers are seconds old. A standalone ``config.yaml`` is
    used because an instance configuration has no ``agent.agent_id``.

    A worker that died while handling a message leaves its lasting mailbox with
    that message delivered and unacknowledged for good (NATS 2.15.3), so a check
    that counted held messages as a live receiver kept every such mailbox.
    """

    import os
    import subprocess
    import sys
    from pathlib import Path

    import yaml

    from config_loader import CONFIG_SCHEMA_VERSION
    from docker.launch_headless import REMOVE_LASTING_LEFTOVERS_ARGS

    team_id = new_id("team_check")
    mapper = NatsSubjectMapper(team_id=team_id)
    stream = mapper.work_stream_name()
    inspector = NatsTransportAdapter(config=nats_transport_config_from_env(), mapper=mapper)
    try:
        await inspector.connect(reconcile_topology=True)
        for owner in ("agent_died_holding", CONFIGURED, "agent_id_0123456789ab"):
            scope = ConsumerScope.PROCESS if owner.startswith("agent_id_") else ConsumerScope.ROUTE
            await _leftover(inspector, owner, scope)
        await _push_era_leftover(inspector, PUSH_ERA)
        # The dead worker had received a message and not acknowledged it.
        subject, durable = _names(mapper, "agent_died_holding", AGENT_DIRECT_ROLE)
        await inspector._nc.publish(subject, b"held when it died")  # noqa: SLF001
        await inspector._nc.flush()  # noqa: SLF001
        holder = await inspector._js.pull_subscribe_bind(durable=durable, stream=stream)  # noqa: SLF001
        assert len(await holder.fetch(1, timeout=5)) == 1  # delivered, never acknowledged
        await holder.unsubscribe()
        info = await inspector._js.consumer_info(stream, durable)  # noqa: SLF001
        assert (info.num_waiting, info.num_ack_pending) == (0, 1)

        root = Path(__file__).resolve().parents[1]
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": CONFIG_SCHEMA_VERSION,
                    "agent": {"agent_id": CONFIGURED, "default_team_id": team_id},
                    "transport": {"nats_servers": list(nats_transport_config_from_env().servers)},
                }
            )
        )
        run = subprocess.run(
            [
                sys.executable,
                str(root / "scripts" / "reconcile_nats_consumers.py"),
                "--config",
                str(config_path),
                *REMOVE_LASTING_LEFTOVERS_ARGS,
                "--minimum-age-seconds",
                "0",
            ],
            cwd=root,
            env={**os.environ, "TEAM_LOCAL_NATS_ENABLED": "false"},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert run.returncode == 0, run.stderr[-2000:]
        assert "deleted_count=4" in run.stderr, run.stderr[-2000:]  # the dead worker's and the push-era pair
        # The script hands the server's version on, which decides NATS's default
        # stream limit (this line holds on NATS 2.10 too, where no default applies).
        version = server_version_of(inspector._nc)  # noqa: SLF001
        assert f"nats_server_version={version_label(version)} " in run.stderr, run.stderr[-2000:]
        if version >= (2, 15):
            assert "stream_consumer_limit=1000 stream_limit_source=nats_default" in run.stderr, run.stderr[-2000:]

        remaining = sorted((c.owner_agent_id, c.consumer_role) for c in await inspector.agent_consumers())
        assert remaining == sorted(
            (owner, role)
            for owner in (CONFIGURED, "agent_id_0123456789ab")  # configured; process-owned, only reported
            for role in (AGENT_DIRECT_ROLE, AGENT_CONTROL_ROLE)
        )
        # Its message is still in the stream and comes back with the consumer.
        await ensure_pull_consumer(
            inspector._js, stream_name=stream,  # noqa: SLF001
            config=_config(inspector, "agent_died_holding", AGENT_DIRECT_ROLE, ConsumerScope.PROCESS), logger=log,
        )
        again = await inspector._js.pull_subscribe_bind(durable=durable, stream=stream)  # noqa: SLF001
        (message,) = await again.fetch(1, timeout=5)
        assert message.data == b"held when it died"
        await message.ack()
    finally:
        if inspector._js is not None:  # noqa: SLF001
            with contextlib.suppress(Exception):
                await inspector._js.delete_stream(stream)  # noqa: SLF001
        await inspector.close()

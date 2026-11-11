"""An announced exit is recognised when the team lead's connection is down at the time (nats-server 2.15.1).

The worker's host writes its agent record as exiting, closes the session (the team services
re-sign it with its end and delete the placement) and removes the record, within a few
milliseconds. UA1_AGENTS keeps one value per key, so a reader that was not connected at that
moment sees only the deletion. The lead's reader still learns the worker left for good from the
closed session (``custom_transport.protocol1.team_agents``), and fails the subtask it sent to
the worker's own mailbox that nobody took (``custom_agents.team_directory``). Control: the same
exit with the connection up. Moved into the suite from the review's probe (FAIL-2).

A restarted lead learns of the exit from its first reading of the view, which tells nobody; it
fails the subtask through its own check once it restored its saved work, also when that is after
the view was read (F2). And with the lead's real delegated-task records and the team's real work
stream, only the subtask nobody took is withdrawn and failed (F5).
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import nats
import pytest

from custom_agents import agent_requests
from custom_agents.agent_host import (
    LEAD_HOST_RUNTIME,
    AgentProcessHost,
    lead_host_profile,
    report_restored_work,
    startup_agent_host_profile,
)
from custom_agents.live_controls import LIVE_CONTROL_HANDLERS
from custom_transport.protocol1.host_runtime import MessageHost
from runtime_settings.instance_layout import InstanceLayout
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from tests.test_live_agent_controls import DEFINITION, WORKER_RUNTIME, _until, stand_in
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401 - broker is a fixture

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


class _Proxy:
    """A TCP proxy to the broker whose connections can be cut, and refused for a while."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self.target = (target_host, target_port)
        self.open: list[asyncio.StreamWriter] = []
        self.refusing = False

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"nats://127.0.0.1:{port}"

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.refusing:
            writer.close()
            return
        up_reader, up_writer = await asyncio.open_connection(*self.target)
        self.open += [writer, up_writer]

        async def pump(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
            try:
                while data := await source.read(65536):
                    target.write(data)
                    await target.drain()
            except Exception:
                pass
            finally:
                target.close()

        await asyncio.gather(pump(reader, up_writer), pump(up_reader, writer))

    def cut(self) -> None:
        self.refusing = True
        for writer in self.open:
            writer.transport.abort()
        self.open.clear()


async def _exit_while(broker_url: str, tmp_path: Path, *, connection_down: bool, process_down: bool = False,
                      restored_after_view: bool = False) -> int:
    """How many subtasks the lead failed after its worker exited, with its connection down or up meanwhile.

    ``process_down``: the lead's process stopped while the worker exited, and a new lead starts.
    ``restored_after_view``: that new lead restores its saved subtask only after its process read
    the team's view, as a real one does (its view is read within milliseconds of attaching, its
    saved work later in ``UniversalAgent._connect_nats``).
    """

    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect", "Researcher"), 2)
    worker_profile = startup_agent_host_profile(TEAM, WORKER_RUNTIME, "Researcher")
    stop = asyncio.Event()
    host, port = broker_url.removeprefix("nats://").split(":")
    proxy = _Proxy(host, int(port))
    proxy_url = await proxy.start()
    async with AsyncExitStack() as stack:
        await installation(stack, broker_url, layout, team_services_profile(TEAM), lead_profile, worker_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM),
                                    await connect(stack, broker_url, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))
        # Asked of Researcher-1 by name: sent to its own mailbox, and no worker took it (no record).
        subtask = SimpleNamespace(task=SimpleNamespace(task_id="sub_1"), header=SimpleNamespace(to_routing_path="worker_1"),
                                  meta=SimpleNamespace(to_agent_name="Researcher-1"))
        lead = stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True, pending=[subtask])
        worker = stand_in("worker_1", "Researcher-1", "Researcher", is_lead=False)
        lead_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        worker_host = AgentProcessHost(layout, worker_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        lead_connection = await nats.connect(proxy_url, inbox_prefix=f"_INBOX.{LEAD_HOST_RUNTIME}".encode(),
                                             allow_reconnect=True, reconnect_time_wait=0.5, max_reconnect_attempts=-1)
        stack.push_async_callback(lead_connection.close)
        try:
            lead_host.attach(lead, agent_type="TeamArchitect", definition=DEFINITION, nc=lead_connection,
                             state=("idle", True))
            worker_host.attach(worker, agent_type="Researcher", definition=DEFINITION,
                               nc=await connect(stack, broker_url, WORKER_RUNTIME), state=("idle", True))
            await _until(lambda: "worker_1" in lead.known_agents.agent_map, timeout=30)
            if process_down:
                await lead_host.stop()
            if connection_down:
                proxy.cut()
                await asyncio.sleep(0.2)
            await asyncio.wrap_future(worker_host.detach("worker_1"))  # exiting, session closed, record removed
            if process_down:
                # A fresh host and requester have no in-memory history of the worker.
                # Pending subtasks are the work recovered by the restarted lead, which then
                # checks them as UniversalAgent._connect_nats does.
                lead = stand_in("lead_2", "TeamLead", "TeamArchitect", is_lead=True,
                                pending=[] if restored_after_view else [subtask])
                lead_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
                agent_requests.configure_process_host(lead_host)
                lead_host.attach(lead, agent_type="TeamArchitect", definition=DEFINITION,
                                 nc=await connect(stack, broker_url, LEAD_HOST_RUNTIME), state=("idle", True))
                if restored_after_view:
                    await _until(lambda: lead_host.team_agents is not None and lead_host.team_agents.ready.is_set(),
                                 timeout=30)
                    await asyncio.sleep(0.3)  # the view's first reading is over; nothing was told then
                    lead.task_manager.get_pending_subtasks = lambda: [subtask]
                report_restored_work(lead)
            if connection_down:
                await asyncio.sleep(2.0)
                proxy.refusing = False
            await _until(lambda: "worker_1" not in lead.known_agents.agent_map, timeout=60)
            await _until(lambda: lead._fail_subtask_and_reconcile_parent.await_count > 0, timeout=30)
            await asyncio.sleep(1.0)  # nothing more arrives
            return lead._fail_subtask_and_reconcile_parent.await_count
        finally:
            # Stopped also when a check above fails, so the test ends rather than hangs.
            agent_requests.configure_process_host(None)
            await lead_host.stop()
            await worker_host.stop()
            stop.set()
            await asyncio.gather(serving, return_exceptions=True)
            proxy.server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("connection_down", "process_down", "restored_after_view"), [
    (False, False, False), (True, False, False), (False, True, False),
    # F2, A2: the restarted lead restores its by-name subtask after its view was read.
    (False, True, True),
])
async def test_the_lead_fails_what_a_worker_left_untaken_also_when_it_missed_the_exit(
    broker: str, tmp_path: Path, connection_down: bool, process_down: bool,  # noqa: F811 - shared fixture
    restored_after_view: bool,
) -> None:
    assert await _exit_while(broker, tmp_path, connection_down=connection_down, process_down=process_down,
                             restored_after_view=restored_after_view) == 1


@pytest.mark.asyncio
async def test_the_lead_withdraws_and_fails_only_what_nobody_took_from_a_departed_workers_mailbox(
    broker: str, tmp_path: Path, caplog,  # noqa: F811 - shared fixture
) -> None:
    """F5: with the lead's real delegated-task records and the team's real work stream.

    The lead asked Researcher-1 for two tasks by name. The worker took the first from its
    mailbox (acknowledged, so the stream no longer holds it) and its claim has not reached the
    lead; the second still waits there. When the worker leaves, the lead withdraws the second
    from the stream and fails it, and keeps the first for custody recovery and the watchdog.
    """

    import logging

    from custom_transport.base import TransportPublishOptions
    from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
    from custom_transport.subjects import NatsSubjectMapper
    from storage.config import resolve_storage_settings
    from storage.delegated_task_repository import DelegatedTaskRepository
    from tests.test_live_nats_replacement_semantics import _subscribe_queue, _task

    layout = InstanceLayout.at(tmp_path / "instance")
    lead_profile = lead_host_profile(TEAM, ("TeamArchitect", "Researcher"), 2)
    worker_profile = startup_agent_host_profile(TEAM, WORKER_RUNTIME, "Researcher")
    stop = asyncio.Event()
    mapper = NatsSubjectMapper(team_id=TEAM)
    lead_work = NatsTransportAdapter(config=NatsTransportConfig(servers=[broker], name="lead-work"), mapper=mapper)
    worker_work = NatsTransportAdapter(config=NatsTransportConfig(servers=[broker], name="worker-work"), mapper=mapper)
    records = DelegatedTaskRepository(resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "team.db")}}))

    async def delegate(task_id: str) -> int:
        """As the lead's delegation does: record the task, publish it to the worker's mailbox, keep its sequence."""

        ack = await lead_work.publish(envelope=_task("worker_1", f"part {task_id}", task_id=task_id),
                                      routing_key="worker_1",
                                      options=TransportPublishOptions(durable=True, idempotency_key=task_id))
        records.record_published(team_id=TEAM, task_id=task_id, parent_task_id="root", root_task_id="root",
                                 requester_agent_id="lead_1", named_agent_type="Researcher",
                                 target_agent_name="Researcher-1", publish_sequence=ack.seq)
        return int(ack.seq)

    def by_name(task_id: str) -> SimpleNamespace:
        return SimpleNamespace(task=SimpleNamespace(task_id=task_id), header=SimpleNamespace(to_routing_path="worker_1"),
                               meta=SimpleNamespace(to_agent_name="Researcher-1"))

    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), lead_profile, worker_profile)
        services_host = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services_host, TEAM, stop))
        await lead_work.connect(reconcile_topology=True)
        stack.push_async_callback(lead_work.close)
        await worker_work.connect()
        stack.push_async_callback(worker_work.close)

        taken = await delegate("sub_taken")
        mailbox = await _subscribe_queue(worker_work, name="worker_1:direct", routing_key="worker_1",
                                         durable_name=mapper.durable_name("agent_worker_1"))
        delivery = await asyncio.wait_for(mailbox.get(), 10)
        assert delivery.envelope.msg_content.task_id == "sub_taken"
        await delivery.ack()
        await worker_work.close()  # it takes nothing more
        untaken = await delegate("sub_untaken")

        lead = stand_in("lead_1", "TeamLead", "TeamArchitect", is_lead=True,
                        pending=[by_name("sub_taken"), by_name("sub_untaken")])
        lead._delegated_task_repository = records
        lead.team_comms = SimpleNamespace(delete_work_message=lead_work.delete_work_message)
        worker = stand_in("worker_1", "Researcher-1", "Researcher", is_lead=False)
        lead_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        worker_host = AgentProcessHost(layout, worker_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
        try:
            lead_host.attach(lead, agent_type="TeamArchitect", definition=DEFINITION,
                             nc=await connect(stack, broker, LEAD_HOST_RUNTIME), state=("idle", True))
            worker_host.attach(worker, agent_type="Researcher", definition=DEFINITION,
                               nc=await connect(stack, broker, WORKER_RUNTIME), state=("idle", True))
            await _until(lambda: "worker_1" in lead.known_agents.agent_map, timeout=30)

            with caplog.at_level(logging.INFO):
                await asyncio.wrap_future(worker_host.detach("worker_1"))
                await _until(lambda: lead._fail_subtask_and_reconcile_parent.await_count > 0, timeout=30)
                await asyncio.sleep(1.0)  # nothing more arrives

            [failed] = lead._fail_subtask_and_reconcile_parent.await_args_list
            assert failed.kwargs["subtask_info"].task.task_id == "sub_untaken"
            assert "event=team_agent_left_subtask_taken_unclaimed" in caplog.text
            assert "subtask_id=sub_taken" in caplog.text
            # Neither is in the work stream any more: the worker took one, the lead withdrew the other.
            assert not await lead_work.delete_work_message(untaken)
            assert not await lead_work.delete_work_message(taken)
        finally:
            # Stopped also when a check above fails, so the test ends rather than hangs.
            await lead_host.stop()
            await worker_host.stop()
            stop.set()
            await asyncio.gather(serving, return_exceptions=True)

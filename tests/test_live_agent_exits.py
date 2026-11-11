"""An announced exit is recognised when the team lead's connection is down at the time (nats-server 2.15.1).

The worker's host writes its agent record as exiting, closes the session (the team services
re-sign it with its end and delete the placement) and removes the record, within a few
milliseconds. UA1_AGENTS keeps one value per key, so a reader that was not connected at that
moment sees only the deletion. The lead's reader still learns the worker left for good from the
closed session (``custom_transport.protocol1.team_agents``), and fails the subtask it sent to
the worker's own mailbox that nobody took (``custom_agents.team_directory``). Control: the same
exit with the connection up. Moved into the suite from the review's probe (FAIL-2).
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


async def _exit_while(broker_url: str, tmp_path: Path, *, connection_down: bool, process_down: bool = False) -> int:
    """How many subtasks the lead failed after its worker exited, with its connection down or up meanwhile."""

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
                lead = stand_in("lead_2", "TeamLead", "TeamArchitect", is_lead=True, pending=[subtask])
                lead_host = AgentProcessHost(layout, lead_profile, TEAM, handlers=dict(LIVE_CONTROL_HANDLERS))
                agent_requests.configure_process_host(lead_host)
                lead_host.attach(lead, agent_type="TeamArchitect", definition=DEFINITION,
                                 nc=await connect(stack, broker_url, LEAD_HOST_RUNTIME), state=("idle", True))
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
@pytest.mark.parametrize(("connection_down", "process_down"), [(False, False), (True, False), (False, True)])
async def test_the_lead_fails_what_a_worker_left_untaken_also_when_it_missed_the_exit(
    broker: str, tmp_path: Path, connection_down: bool, process_down: bool,  # noqa: F811 - shared fixture
) -> None:
    assert await _exit_while(broker, tmp_path, connection_down=connection_down, process_down=process_down) == 1

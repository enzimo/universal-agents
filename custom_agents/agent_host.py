"""An agent process as a protocol 1 host: one certificate, a session for each agent it runs.

The launcher starts the team lead's process as host ``lead`` (roles ``lead_host`` and
``worker_host``: the lead and the workers it starts as threads) and each startup agent's
process as host ``agent-<label>`` (role ``worker_host``: its one agent), and lists them so
the installation authority approves them (``docker.message_hosts``). The process learns its
host id from ``strands_main.py --host-runtime``.

Why it is shaped this way:

- **One host per process, on the first agent's connection.** Every agent in the team lead's
  process has its own NATS connection on its own thread and event loop (thread workers run
  ``asyncio.run`` each). The host joins once, on the connection and loop of the first agent
  that attaches (the lead, or the startup agent), and keeps its upkeep there.
- **A session for each agent.** A request addressed to an agent is answered by that agent,
  signed with its session (README "Placement and sessions"). Each agent asks for one when
  it connects (``sessions.open``) and closes it when it leaves (``sessions.close``). Agent
  ids are new at every start, so the host first closes the sessions its earlier run left
  behind, which would otherwise count against its session limit.
- **Never in the way of the agent.** Attaching returns at once; joining and opening
  sessions retry in the background until the installation authority and the team services
  answer. While an agent has no session, requests to it are simply not answered yet; the
  agent's own work, on today's queues, does not wait for any of this.
- **The agent's own loop does the work.** A request for a thread worker arrives on the
  host's loop and runs on the worker's loop (``run_coroutine_threadsafe``), where its task
  manager lives.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional

from custom_messages.wire.frame import Receiver, VerifiedFrame
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.joining import HostProfile
from custom_transport.protocol1.live import ServiceUnavailable, Unsupported
from release_version import release_version
from runtime_settings.instance_layout import InstanceLayout

# The team lead's process: the lead and the workers it starts as threads.
LEAD_HOST_RUNTIME = "lead"
LEAD_HOST_ROLES = ("lead_host", "worker_host")
STARTUP_AGENT_HOST_ROLES = ("worker_host",)
AGENT_ADDRESS = "ctl.agent.{agent}"
# How long a retry waits while the authority or the team services are not answering yet.
RETRY_SECONDS = (0.5, 1.0, 2.0, 5.0, 10.0)
# Renew each agent's session daily; sessions last 7 days (team_services.sessions).
SESSION_RENEW_SECONDS = 24 * 3600.0
_PLACEMENT_BUCKET = "UA1_PLACEMENT"
_LOGGER = logging.getLogger(__name__)

# What an agent answers on its address: the operation's kind, and the handler that runs on
# the agent's own loop with the agent and the verified request.
AgentHandler = Callable[[Any, VerifiedFrame], Awaitable[dict[str, Any]]]


def agent_host_runtime(label: str) -> str:
    """A startup agent process's host id, from its label (``Type`` or ``Type#<n>``): ``agent-Type-<n>``."""

    return "agent-" + label.replace("#", "-")


def lead_host_profile(team: str, agent_types: Iterable[str], session_limit: int) -> HostProfile:
    """The team lead's process: the instance's agent types, and a session for the lead and each worker."""

    return HostProfile(LEAD_HOST_RUNTIME, (team,), LEAD_HOST_ROLES, agent_types=tuple(sorted(set(agent_types))),
                       session_limit=session_limit,
                       software={"name": "universal-agents", "version": release_version()})


def startup_agent_host_profile(team: str, runtime: str, agent_type: str) -> HostProfile:
    """One startup agent process: its one agent."""

    return HostProfile(runtime, (team,), STARTUP_AGENT_HOST_ROLES, agent_types=(agent_type,), session_limit=1,
                       software={"name": "universal-agents", "version": release_version()})


def definition_sha256(launch_digest: str) -> str:
    """The contract's definition digest (unpadded base64url SHA-256) from the agent's launch digest.

    ``launch_digest`` is ``custom_agents.schedule_definition_identity.launch_definition_digest``: the
    hex SHA-256 of everything the agent runs with, as expanded by the code.
    """

    return base64.urlsafe_b64encode(bytes.fromhex(launch_digest)).rstrip(b"=").decode()


@dataclass
class _HostedAgent:
    agent: Any
    agent_type: str
    definition: str
    session: Optional[str] = None
    subscription: Any = None


@dataclass
class AgentProcessHost:
    """This process's host and the agents it runs (module docstring)."""

    layout: InstanceLayout
    profile: HostProfile
    team: str
    handlers: dict[str, AgentHandler] = field(default_factory=dict)
    logger: logging.Logger = _LOGGER
    host: Optional[MessageHost] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    agents: dict[str, _HostedAgent] = field(default_factory=dict)
    _joined: Optional[asyncio.Event] = None
    _stop: Optional[asyncio.Event] = None
    _task: Optional[asyncio.Task[None]] = None

    # -- the agent's side (called on the agent's own loop) --------------------------------

    def attach(self, agent: Any, *, agent_type: str, definition: str, nc: Any) -> None:
        """Start the host on the first agent's connection, and open this agent's session; returns at once."""

        if self.loop is None:
            self.loop = asyncio.get_running_loop()
            self._joined, self._stop = asyncio.Event(), asyncio.Event()
            self.host = MessageHost(self.layout, self.profile, nc, logger=self.logger,
                                    sessions_active=lambda: sum(1 for item in self.agents.values() if item.session))
            self._task = self.loop.create_task(self._run(), name="agent_process_host")
        hosted = _HostedAgent(agent, agent_type, definition)
        self.loop.call_soon_threadsafe(self._spawn, self._open(hosted))

    def detach(self, agent_id: str, *, failed: bool = False) -> Optional[concurrent.futures.Future[None]]:
        """Close this agent's session; the caller may wait on the returned future while its connection is up."""

        if self.loop is None or self.loop.is_closed():
            return None
        return asyncio.run_coroutine_threadsafe(self._close(agent_id, "failed" if failed else "exited"), self.loop)

    async def stop(self) -> None:
        """On the host's loop, when the process ends: close every session and stop the upkeep."""

        for agent_id in list(self.agents):
            await self._close(agent_id, "exited")
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    # -- the host's side (on the host's loop) --------------------------------------------

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        assert self.loop is not None
        self.loop.create_task(coroutine)

    async def _run(self) -> None:
        assert self.host is not None and self._joined is not None and self._stop is not None
        if not await self._retrying("join", self.host.start):
            return
        await self._close_earlier_sessions()
        self._joined.set()
        self.logger.info("This process joined as a protocol 1 host. event=agent_host_joined runtime=%s",
                         self.profile.runtime)
        renewals = asyncio.get_running_loop().create_task(self._renew_sessions())
        try:
            await self.host.keep_up(self._stop)
        finally:
            renewals.cancel()

    async def _retrying(self, what: str, step: Callable[[], Awaitable[Any]]) -> bool:
        """Run ``step`` until it succeeds or the host stops; False when it stopped first."""

        assert self._stop is not None
        attempt = 0
        while not self._stop.is_set():
            try:
                await step()
                return True
            except _Refused:
                raise
            except Exception as error:  # noqa: BLE001 - logged, and tried again
                delay = RETRY_SECONDS[min(attempt, len(RETRY_SECONDS) - 1)]
                log = self.logger.info if isinstance(error, ServiceUnavailable) else self.logger.warning
                log("A protocol 1 host step did not succeed yet; trying again. event=agent_host_retry step=%s "
                    "runtime=%s retry_in_seconds=%s error=%s", what, self.profile.runtime, delay, error)
                attempt += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        return False

    def _audience(self) -> dict[str, str]:
        assert self.host is not None
        return {"installation": self.host.identity.installation, "team": self.team, "target": "team_services",
                "id": self.team}

    async def _request(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        assert self.host is not None and self.host.live is not None
        reply = await self.host.live.request(kind, body, self._audience(), team=self.team)
        return dict(reply.raw["body"])

    async def _close_earlier_sessions(self) -> None:
        """Close the sessions this host's earlier run left behind (module docstring)."""

        assert self.host is not None
        try:
            bucket = await self.host.nc.jetstream().key_value(_PLACEMENT_BUCKET)
            keys = await bucket.keys()
        except Exception as error:  # an empty bucket raises too
            self.logger.debug("No earlier placements to read. runtime=%s error=%s", self.profile.runtime, error)
            return
        for key in keys:
            if not key.startswith(f"{self.team}."):
                continue
            entry = await bucket.get(key)
            body = json.loads(entry.value)["body"]
            if body.get("host") != self.profile.runtime or body["agent"] in self.agents:
                continue
            session = f"s-{body['agent']}-{entry.revision}"
            try:
                reply = await self._request("sessions.close", {"request_id": f"close-{session}"[:128],
                                                               "session": session, "reason": "failed"})
            except Exception:
                self.logger.exception("An earlier session could not be closed. event=agent_host_stale_session_close_failed "
                                      "runtime=%s session=%s", self.profile.runtime, session)
                continue
            self.logger.info("Closed a session an earlier run of this host left behind. event=agent_host_stale_session_closed "
                             "runtime=%s agent=%s outcome=%s", self.profile.runtime, body["agent"], reply.get("outcome"))

    async def _open(self, hosted: _HostedAgent) -> None:
        assert self._joined is not None and self.host is not None
        agent_id = str(hosted.agent.id)
        self.agents[agent_id] = hosted
        await self._joined.wait()

        async def open_session() -> None:
            body = await self._request("sessions.open", {
                "request_id": f"open-{agent_id}", "agent": agent_id, "agent_type": hosted.agent_type,
                "definition_sha256": hosted.definition})
            if body["outcome"] != "ok":
                raise _Refused(body)
            session = body["session"]
            assert self.host is not None and self.host.live is not None
            self.host.live.trust.add(json.dumps(session).encode(), now=self.host.clock())
            hosted.session = str(session["body"]["session"])
            self.host.live.identity = self.host.live.identity.with_session(agent_id, hosted.session)

        try:
            if not await self._retrying("session", open_session):
                return
        except _Refused as refused:
            self.logger.error("An agent got no protocol 1 session; requests to it are not answered. "
                              "event=agent_host_session_refused runtime=%s agent=%s agent_type=%s outcome=%s message=%s",
                              self.profile.runtime, agent_id, hosted.agent_type, refused.body.get("outcome"),
                              refused.body.get("message"))
            return
        if agent_id not in self.agents:  # the agent left while its session was opening
            await self._close(agent_id, "exited", hosted=hosted)
            return
        assert self.host.live is not None
        receiver = Receiver(self.host.identity.installation, self.team, "agent", agent_id, self.profile.runtime)
        hosted.subscription = await self.host.live.serve(AGENT_ADDRESS, receiver, self._handler_for(hosted),
                                                         agent=agent_id)
        self.logger.info("An agent has its protocol 1 session. event=agent_host_session_opened runtime=%s agent=%s "
                         "agent_type=%s", self.profile.runtime, agent_id, hosted.agent_type)

    def _handler_for(self, hosted: _HostedAgent) -> Callable[[VerifiedFrame], Awaitable[dict[str, Any]]]:
        async def handle(verified: VerifiedFrame) -> dict[str, Any]:
            handler = self.handlers.get(verified.frame.kind)
            if handler is None:
                raise Unsupported(verified.frame.kind)
            agent_loop = getattr(hosted.agent, "_runtime_loop", None)
            if agent_loop is None or agent_loop is asyncio.get_running_loop():
                return await handler(hosted.agent, verified)
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(handler(hosted.agent, verified),
                                                                               agent_loop))

        return handle

    async def _close(self, agent_id: str, reason: str, *, hosted: Optional[_HostedAgent] = None) -> None:
        hosted = hosted or self.agents.pop(agent_id, None)
        self.agents.pop(agent_id, None)
        if hosted is None or self.host is None or self.host.live is None:
            return
        if hosted.subscription is not None:
            try:
                await hosted.subscription.unsubscribe()
            except Exception:
                self.logger.debug("The agent's address was already closed. agent=%s", agent_id, exc_info=True)
        if hosted.session is None:
            return
        try:
            await self._request("sessions.close", {"request_id": f"close-{hosted.session}"[:128],
                                                   "session": hosted.session, "reason": reason})
        except Exception:
            # The next start of this host closes it (module docstring).
            self.logger.warning("An agent's session could not be closed; the host's next start closes it. "
                                "event=agent_host_session_close_failed runtime=%s agent=%s", self.profile.runtime,
                                agent_id, exc_info=True)
        self.host.live.identity = self.host.live.identity.without_session(agent_id)

    async def _renew_sessions(self) -> None:
        while True:
            await asyncio.sleep(SESSION_RENEW_SECONDS)
            for agent_id, hosted in list(self.agents.items()):
                if hosted.session is None:
                    continue
                try:
                    body = await self._request("sessions.renew", {"request_id": f"renew-{hosted.session}"[:128],
                                                                  "session": hosted.session})
                    if body["outcome"] == "ok" and self.host is not None and self.host.live is not None:
                        self.host.live.trust.add(json.dumps(body["session"]).encode(), now=self.host.clock())
                except Exception:
                    self.logger.exception("An agent's session could not be renewed. event=agent_host_session_renew_failed "
                                          "runtime=%s agent=%s", self.profile.runtime, agent_id)


class _Refused(Exception):
    """The team services refused a session; not retried."""

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__(str(body.get("outcome")))
        self.body = body


_PROCESS_HOST: Optional[AgentProcessHost] = None


def configure_process_host(host: Optional[AgentProcessHost]) -> None:
    """Set this process's host (``strands_main.py``); ``None`` when the launcher gave it no host id."""

    global _PROCESS_HOST
    _PROCESS_HOST = host


def process_host() -> Optional[AgentProcessHost]:
    return _PROCESS_HOST


def agent_definition_sha256(agent: Any) -> str:
    """The agent's definition digest for its placement: the launch digest of its type's definition."""

    from custom_agents.schedule_definition_identity import launch_definition_digest

    runtime = getattr(agent, "_local_team_runtime", None)
    definition = runtime.resolve_definition(agent.named_agent_type) if runtime is not None else None
    if not isinstance(definition, dict):
        # Every type the team runs has a definition; without one the placement names the type alone.
        definition = {"named_agent_type": agent.named_agent_type}
    return definition_sha256(launch_definition_digest(definition))


def attach_agent(agent: Any, adapter: Any) -> None:
    """After the agent's connection opens: join this process's host with it (returns at once)."""

    host = process_host()
    if host is None:
        return
    try:
        host.attach(agent, agent_type=str(agent.named_agent_type), definition=agent_definition_sha256(agent),
                    nc=adapter.nats_connection)
    except Exception:
        host.logger.exception("An agent could not join this process's protocol 1 host. event=agent_host_attach_failed "
                              "agent=%s", getattr(agent, "id", None))


async def detach_agent(agent: Any, *, timeout_seconds: float = 5.0) -> None:
    """Before the agent's connection closes: close its session; the host's own agent stops the host."""

    host = process_host()
    if host is None or host.loop is None:
        return
    try:
        if host.loop is asyncio.get_running_loop():
            if getattr(agent, "_runtime_loop", None) is host.loop and len(host.agents) <= 1:
                await asyncio.wait_for(host.stop(), timeout=timeout_seconds)
            else:
                await asyncio.wait_for(host._close(str(agent.id), "exited"), timeout=timeout_seconds)
            return
        future = host.detach(str(agent.id))
        if future is not None:
            await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout_seconds)
    except Exception:
        host.logger.warning("An agent's protocol 1 session was not closed as it left; the host's next start closes it. "
                            "event=agent_host_detach_failed agent=%s", getattr(agent, "id", None), exc_info=True)


def process_host_for(runtime: str, layout: InstanceLayout, team: str, *, is_lead: bool, agent_type: str,
                     worker_pools: Any) -> AgentProcessHost:
    """This agent process's host, as the launcher listed it (``docker.message_hosts``).

    The team lead's host asks for the instance's agent types (the catalog this process
    loads) and a session for the lead and each worker; a startup agent's host for its one
    agent. Relayed people's commands are answered with ``custom_agents.relayed_task_controls``.
    """

    from custom_agents.agent_catalog import load_agent_catalog
    from custom_agents.relayed_task_controls import RELAYED_HANDLERS
    from custom_transport.protocol1.joining import certificate_agent_types
    from installation_authority.expected_hosts import read_expected_hosts
    from runtime_settings.worker_pools import WorkerPoolsConfig

    if is_lead:
        types = certificate_agent_types(load_agent_catalog().agent_types, _LOGGER)
        # The limit the launcher listed (the expected-hosts file names public keys only), so the join
        # never asks for more than was listed; computed the launcher's way when there is no entry yet.
        listed = read_expected_hosts(layout.trust_expected_hosts_path).get(runtime)
        limit = listed.session_limit if listed is not None else lead_session_limit(
            WorkerPoolsConfig.model_validate(dict(worker_pools or {})))
        profile = lead_host_profile(team, types, limit)
    else:
        profile = startup_agent_host_profile(team, runtime, agent_type)
    if profile.runtime != runtime:
        raise ValueError(f"the launcher named this host {runtime!r}, but its profile is {profile.runtime!r}")
    return AgentProcessHost(layout, profile, team, handlers=dict(RELAYED_HANDLERS))


def lead_session_limit(pools: Any) -> int:
    """Sessions the team lead's host may hold: the lead and every worker the team may run."""

    return int(pools.max_total_workers_per_team) + 1


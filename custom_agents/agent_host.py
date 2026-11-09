"""An agent process as a protocol 1 host: one certificate, a session for each agent it runs.

The launcher starts the team lead's process as host ``lead`` (roles ``lead_host`` and
``worker_host``: the lead and the workers it starts as threads) and each startup agent's
process as a bounded host id derived from its label (role ``worker_host``: its one agent), and lists them so
the installation authority approves them (``docker.message_hosts``). The process learns its
host id from ``strands_main.py --host-runtime``.

Why it is shaped this way:

- **One host per process, on the first agent's connection.** Every agent in the team lead's
  process has its own NATS connection on its own thread and event loop (thread workers run
  ``asyncio.run`` each). The host joins once, on the connection and loop of the first agent
  that attaches (the lead, or the startup agent), and keeps its upkeep there.
- **A session for each agent.** A request addressed to an agent is answered by that agent,
  signed with its session (README "Agent sessions"). Each agent asks for one when
  it connects (``sessions.open``) and closes it when it leaves (``sessions.close``). Agent
  ids are new at every start, so the host first closes the sessions its earlier run left
  behind, which would otherwise count against its session limit.
- **Never in the way of the agent.** Attaching returns at once; joining and opening
  sessions retry in the background until the installation authority and the team services
  answer. While an agent has no session, requests to it are simply not answered yet; the
  agent's own work, on today's queues, does not wait for any of this.
- **Every running agent holds a session.** A refused session is asked for again 5 seconds
  after the refusal and twice as long after each further one, up to every 5 minutes, for as
  long as the agent runs, one opening at a time. Each change of refusal is logged once
  (``event=agent_host_session_refused``), and so is the opening that finally succeeds.
  Meanwhile the host record lists the agent as an unhealthy component named
  ``<agent type>-<agent id>`` with why as its code (``REFUSAL_CODES``), which the gateway
  shows as a Status page finding; the wait for the first answer at start is not listed. A
  type the certificate lacks (``not_allowed``), such as a specialist the team created while
  the process runs, is looked up in the team's agent catalog again: when it is there, the
  host joins again for it (``MessageHost.rejoin``) and opens the session at once. A type
  no certificate can carry is never asked for and stays listed until the agent exits. A
  refused renewal opens a new session the same way.
- **Request ids.** Each opening that got an answer is followed by one with a new request
  id, and each renewal has its own (the session and the sequence of the record it renews),
  stable only across retries of that one request. Session operations keep no stored reply:
  repeating an open replaces its placement and session; repeating a close finishes its cleanup.
- **Only verified records.** The placements read at start, to close the sessions an
  earlier run left, are verified like every record (signed by the team services); one that does not verify counts as absent and is logged,
  and one bad key never keeps the agents from their sessions.
- **The agent's own loop does the work.** A request for a thread worker arrives on the
  host's loop and runs on the worker's loop (``run_coroutine_threadsafe``), where its task
  manager lives.
- **The team as each agent sees it.** After joining, the host reads the team's live agents
  from their records (``custom_transport.protocol1.team_agents``), once for the whole
  process, as one of its supervised steps that stops with the host, and keeps every
  attached agent's directory in step with it on the agent's own loop
  (``custom_agents.team_directory``). It also receives the team's events
  (``skills.changed``) once and hands each to every agent it runs. Agents ask each other
  through it (``custom_agents.agent_requests``: ``ask_agent``, ``announce``): requests are
  signed with the asking agent's session.
- **Agent records.** State changes are coalesced within two seconds; an unchanged record
  is refreshed every minute and expires within three. Each agent has its own supervised
  refresh and exit jobs. A refused session repair cannot starve another agent's refresh.
  Exits write `exiting`, close every session returned to the attachment, and conditionally
  remove its verified record. Cancellation retains that work for upkeep.
- **A process on another contract drains.** While the contract's label is a pre-release,
  hosts of one installation refuse a peer whose contract digest differs (contract README
  "Versions"). The host learns that its process runs another contract from the team
  services' host record, which it follows from its join on, or from ``protocol_mismatch``
  (``MessageHost.on_other_contract``). A join refused that way is not asked for again. The
  host then opens no session, asks for no refused one again, and drains every agent it runs
  on the agent's own loop (``custom_agents.contract_drain``): no new work, the running
  turns finish, and what was not finished goes back where this release has a way. Its
  record says ``draining``; its agents' sessions are still renewed, so requests about the
  tasks they hold still reach them. Once no agent holds anything, it closes their sessions
  (``exited``, removing their placements), its record says ``drained`` and it renews only to give its agents up
  (``MessageHost.finish_drain``). A process that never joined drains its agents the same
  way, with no record to write.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Coroutine, Iterable, Mapping, Optional

import rfc8785

from custom_messages.wire.encoding import b64url, format_timestamp
from custom_messages.wire.kinds import STATE_RECORDS
from custom_agents.contract_drain import drain_agent, holds_work
from custom_messages.wire.frame import Receiver, VerifiedFrame
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.host_runtime import HOST_COMPONENTS_MAX, MessageHost
from custom_transport.protocol1.joining import AGENT_TYPE_PATTERN, HostProfile
from custom_transport.protocol1.live import ServiceUnavailable, Unsupported
from custom_agents.agent_requests import AgentUnreachable, AskBlocked, SessionlessAgent, process_host
from custom_transport.protocol1.team_agents import TeamAgent, TeamAgents
from release_version import release_version
from runtime_settings.instance_layout import InstanceLayout

# The team lead's process: the lead and the workers it starts as threads.
LEAD_HOST_RUNTIME = "lead"
LEAD_HOST_ROLES = ("lead_host", "worker_host")
STARTUP_AGENT_HOST_ROLES = ("worker_host",)
AGENT_ADDRESS = "ctl.agent.{agent}"
# The team's events, received once per process and handed to every agent it runs.
SKILLS_EVENT_ADDRESS = "evt.skills_changed"
# How long a retry waits while the authority or the team services are not answering yet.
RETRY_SECONDS = (0.5, 1.0, 2.0, 5.0, 10.0)
# How long a refused session waits before it is asked for again: the first, doubling up to the last.
REFUSED_RETRY_SECONDS = (5.0, 300.0)
# Why an agent has no session, as its component's code in the host record says (module docstring).
# The gateway explains each on the Status page (``assistant_gateway.team_view``).
REFUSAL_CODES = {"not_allowed": "session_not_allowed", "already_placed": "session_already_placed",
                 "limit_reached": "session_limit_reached", "runtime_error": "session_runtime_error"}
SESSION_REFUSED_CODE = "session_refused"  # an outcome this release does not know
# Its type is in the catalog, but the installation authority did not add it to this host's certificate.
TYPE_NOT_GRANTED_CODE = "session_type_not_granted"
# Its type's name does not fit a certificate (``joining.AGENT_TYPE_PATTERN``).
TYPE_NOT_CERTIFIABLE_CODE = "agent_type_not_certifiable"
# More agents without a session than a host record lists: the last component stands for the rest.
MORE_AGENTS_COMPONENT, MORE_AGENTS_CODE = "more-agents", "sessions_refused"
# Each code above in words, for people who see an agent without a session (a restart's review).
WITHOUT_SESSION_REASONS = {
    REFUSAL_CODES["not_allowed"]: "its type is not allowed on its host",
    REFUSAL_CODES["already_placed"]: "it is placed on another host",
    REFUSAL_CODES["limit_reached"]: "its host already runs as many agents as it may",
    REFUSAL_CODES["runtime_error"]: "the team services could not open its session",
    SESSION_REFUSED_CODE: "its session was refused",
    TYPE_NOT_GRANTED_CODE: "its host was not granted its type",
    TYPE_NOT_CERTIFIABLE_CODE: "its type's name cannot have a session",
    MORE_AGENTS_CODE: "more agents without a session than its host lists by name",
}
_NOT_IN_TOKEN = re.compile(r"[^A-Za-z0-9_-]")
# Renew each agent's session daily; sessions last 7 days (team_services.sessions).
SESSION_RENEW_SECONDS = 24 * 3600.0
# An agent's record is written when its state changes, at most this often (README "State records").
AGENT_RECORD_INTERVAL_SECONDS = 2.0
# Refresh unchanged records with current certificates and repair a removed record.
_AGENT_RECORD_TTL = STATE_RECORDS["agent"].ttl_seconds
assert _AGENT_RECORD_TTL is not None
AGENT_RECORD_LIFETIME = timedelta(seconds=_AGENT_RECORD_TTL)
AGENT_RECORD_REFRESH_SECONDS = _AGENT_RECORD_TTL / 3
AGENT_RECORD_CLEANUP_SECONDS = 10.0
# While the process drains, how often it checks whether its agents still hold work (module docstring).
DRAIN_POLL_SECONDS = 5.0
SESSION_CLEANUP_SECONDS = 10.0
_LOGGER = logging.getLogger(__name__)

# What an agent answers on its address: the operation's kind, and the handler that runs on
# the agent's own loop with the agent and the verified request.
AgentHandler = Callable[[Any, VerifiedFrame], Coroutine[Any, Any, dict[str, Any]]]


def agent_host_runtime(label: str) -> str:
    """A startup agent process's host id, from its label (``Type`` or ``Type#<n>``): ``agent-Type-<n>``."""

    # Labels use '#' for replicas; replacing it with '-' collided with real type names.
    digest = hashlib.sha256(label.encode()).hexdigest()[:16]
    readable = _NOT_IN_TOKEN.sub("_", label)[:40]
    return f"agent-{readable}-{digest}"


def lead_host_profile(team: str, agent_types: Iterable[str], session_limit: int) -> HostProfile:
    """The team lead's process: the instance's agent types, and a session for the lead and each worker."""

    return HostProfile(LEAD_HOST_RUNTIME, (team,), LEAD_HOST_ROLES, agent_types=tuple(sorted(set(agent_types))),
                       session_limit=session_limit,
                       software={"name": "universal-agents", "version": release_version()})


def startup_agent_host_profile(team: str, runtime: str, agent_type: str) -> HostProfile:
    """One startup agent process: its one agent."""

    if not AGENT_TYPE_PATTERN.fullmatch(agent_type):
        raise ValueError(f"Startup agent type {agent_type!r} cannot have a message session. "
                         "Use 1 to 24 letters, digits, '-' or '_', starting with a letter or digit.")
    return HostProfile(runtime, (team,), STARTUP_AGENT_HOST_ROLES, agent_types=(agent_type,), session_limit=1,
                       software={"name": "universal-agents", "version": release_version()})


def refused_session_component(agent_type: str, agent_id: str) -> str:
    """The host record's component for an agent without a session: ``<agent type>-<agent id>``.

    It reads as it stands on the Status page, and is one token of at most 64 characters (the
    contract's ``token_id``): the type cut to the 24 characters a certificate allows, any other
    character written as ``_``.
    """

    name = f"{_NOT_IN_TOKEN.sub('_', agent_type)[:24]}-{_NOT_IN_TOKEN.sub('_', agent_id)}"
    return (name if name[0].isalnum() else "agent" + name)[:64]


def session_renewal_id(session: str, sequence: int) -> str:
    """The request id of one renewal: the session and the sequence of the session record it renews.

    The next renewal renews a later sequence and so gets another id, while a retry of this
    one keeps it (module docstring). A digest keeps every id whole within the contract's 128
    characters, where cutting a long one could repeat another's.
    """

    digest = b64url(hashlib.sha256(rfc8785.dumps([session, sequence])).digest())
    return f"renew-{sequence}-{digest}"


def definition_sha256(launch_digest: str) -> str:
    """The contract's definition digest (unpadded base64url SHA-256) from the agent's launch digest.

    ``launch_digest`` is ``custom_agents.schedule_definition_identity.launch_definition_digest``: the
    hex SHA-256 of everything the agent runs with, as expanded by the code.
    """

    return base64.urlsafe_b64encode(bytes.fromhex(launch_digest)).rstrip(b"=").decode()


@dataclass(frozen=True)
class _Refusal:
    """Why the agent has no session, and since when."""

    code: str
    message: str
    since: datetime


@dataclass
class _HostedAgent:
    agent: Any
    agent_type: str
    definition: str
    display_name: str = ""
    session: Optional[str] = None
    subscription: Any = None
    subscription_failures: int = 0
    # Why its session was last refused, until one opens (module docstring).
    refusal: Optional[_Refusal] = None
    # Openings that got an answer: the next one has a new request id.
    openings: int = 0
    # A session is being opened for it now; never two at once.
    opening: bool = False
    # Its drain started (the process runs another contract); whether asking if it holds work failed already.
    draining: bool = False
    holds_work_failed: bool = False
    # Set when it leaves, so a wait to ask again ends at once.
    left: Optional[asyncio.Event] = None
    # The agent's state as last reported, and what its record last said (module docstring).
    state: str = "starting"
    accepting_work: bool = True
    since: Optional[datetime] = None
    written: Optional[tuple[str, bool, str]] = None
    written_at: float = float("-inf")
    record_sequence: int = 0
    write_due: Optional[asyncio.TimerHandle] = None
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    write_failures: int = 0
    # Closure belongs to one session; a replacement may arrive during an await.
    closing_sessions: set[str] = field(default_factory=set)
    closed_sessions: set[str] = field(default_factory=set)
    close_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def key(self, team: str) -> str:
        return f"{team}.{self.agent.id}"


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
    # The agent types the team's catalog has now (the team lead's process reads it again on
    # each refusal for a type its certificate lacks); a startup agent's process has its own.
    catalog_types: Optional[Callable[[], Iterable[str]]] = None
    refused_retry_seconds: tuple[float, float] = REFUSED_RETRY_SECONDS
    # Drains one agent on its own loop, and says whether it still holds work, when this process runs another
    # contract than its installation (module docstring); tests give stand-ins.
    drain_agent: Callable[[Any, str], Coroutine[Any, Any, None]] = drain_agent
    holds_work: Callable[[Any], Coroutine[Any, Any, bool]] = holds_work
    drain_poll_seconds: float = DRAIN_POLL_SECONDS
    _draining: Optional[asyncio.Task[None]] = None
    _joined: Optional[asyncio.Event] = None
    # Set when the drain starts: an agent waiting for the join stops waiting then.
    _drain_started: asyncio.Event = field(default_factory=asyncio.Event)
    _stop: Optional[asyncio.Event] = None
    _task: Optional[asyncio.Task[None]] = None
    _pending_departures: dict[str, tuple[_HostedAgent, str]] = field(default_factory=dict)
    _record_cleanup_needed: bool = False
    _stale_refusals: set[tuple[str, str]] = field(default_factory=set)
    _record_jobs: dict[tuple[str, str], asyncio.Task[Any]] = field(default_factory=dict)
    _background: set[asyncio.Task[Any]] = field(default_factory=set)
    _pending_closes: dict[str, tuple[str, str]] = field(default_factory=dict)
    _cleanup_needed: bool = True
    # The team's live agents (module docstring), and the view the agents' directories last got.
    team_agents: Optional[TeamAgents] = None
    _view_before: Mapping[str, TeamAgent] = field(default_factory=dict)
    event_handlers: dict[str, AgentHandler] = field(default_factory=dict)

    # -- the agent's side (called on the agent's own loop) --------------------------------

    def attach(self, agent: Any, *, agent_type: str, definition: str, nc: Any,
               state: tuple[str, bool] = ("starting", True)) -> None:
        """Start the host on the first agent's connection, and open this agent's session; returns at once."""

        if self.loop is None:
            self.loop = asyncio.get_running_loop()
            self._joined, self._stop = asyncio.Event(), asyncio.Event()
            self.host = MessageHost(self.layout, self.profile, nc, logger=self.logger,
                                    sessions_active=lambda: sum(1 for item in self.agents.values() if item.session),
                                    components=self._components)
            self.host.on_other_contract(self._other_contract)
            self._task = self.loop.create_task(self._run(), name="agent_process_host")
        hosted = _HostedAgent(agent, agent_type, definition, display_name=str(getattr(agent, "name", "") or "")[:80],
                              state=state[0], accepting_work=state[1])
        self.loop.call_soon_threadsafe(self._spawn, lambda: self._open(hosted), "open_session")

    def report(self, agent_id: str, state: str, accepting_work: bool) -> None:
        """The agent's state now, from any thread; its record follows (module docstring)."""

        if self.loop is None or self.loop.is_closed():
            return
        try:
            self.loop.call_soon_threadsafe(self._noted, agent_id, state, accepting_work)
        except RuntimeError:  # the host's loop closed meanwhile
            return


    def detach(self, agent_id: str, *, failed: bool = False) -> Optional[concurrent.futures.Future[None]]:
        """Close this agent's session; the caller may wait on the returned future while its connection is up."""

        if self.loop is None or self.loop.is_closed():
            return None
        return asyncio.run_coroutine_threadsafe(self._close(agent_id, "failed" if failed else "exited"), self.loop)

    async def stop(self) -> None:
        """On the host's loop, when the process ends: close every session and stop the upkeep."""

        try:
            for agent_id in list(self.agents):
                await self._close(agent_id, "exited")
        finally:
            if self._stop is not None:
                self._stop.set()
            for task in list(self._background):
                task.cancel()
            await asyncio.gather(*self._background, return_exceptions=True)
            if self._task is not None:
                await asyncio.gather(self._task, return_exceptions=True)

    # -- the host's side (on the host's loop) --------------------------------------------

    def _spawn(self, work: Callable[[], Awaitable[Any]], what: str = "host_update") -> asyncio.Task[Any]:
        assert self.loop is not None
        task = self.loop.create_task(self._supervise(what, work), name=f"agent_host_{what}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _supervise(self, what: str, work: Callable[[], Awaitable[Any]]) -> None:
        """Retry a failed background step; cancellation of this task still stops it."""
        assert self._stop is not None
        attempt = 0
        while not self._stop.is_set():
            try:
                await work()
                return
            except asyncio.CancelledError:
                current = asyncio.current_task()
                assert current is not None
                if current.cancelling():
                    raise
                self.logger.warning("An agent host step was interrupted; trying again. "
                                    "event=agent_host_step_interrupted runtime=%s step=%s", self.profile.runtime, what)
            except Exception:
                self.logger.exception("An agent host step failed; trying again. "
                                      "event=agent_host_step_failed runtime=%s step=%s", self.profile.runtime, what)
            delay = RETRY_SECONDS[min(attempt, len(RETRY_SECONDS) - 1)]
            attempt += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _run(self) -> None:
        assert self.host is not None and self._joined is not None and self._stop is not None
        if not await self._retrying("join", self.host.start):
            return
        await self._close_earlier_sessions()
        await self._remove_earlier_records()
        # The team's view and its events run as supervised steps and stop with the host (module docstring).
        team_agents = self.team_agents = TeamAgents(self.host, self.team, logger=self.logger)
        team_agents.listen(self._team_changed)
        stop = self._stop
        self._spawn(lambda: team_agents.run(stop), "team_view")
        self._spawn(self._serve_events, "team_events")
        self._joined.set()
        self.logger.info("This process joined as a protocol 1 host. event=agent_host_joined runtime=%s",
                         self.profile.runtime)
        renewals = self._spawn(self._renew_sessions, "renew_sessions")
        following = self._spawn(self.host.follow_contract, "follow_contract")
        cleanup = self._spawn(self._session_cleanup, "session_cleanup")
        records = self._spawn(self._record_upkeep, "record_upkeep")
        try:
            await self.host.keep_up(self._stop)
        finally:
            renewals.cancel()
            following.cancel()
            cleanup.cancel()
            records.cancel()
            if self._draining is not None:
                self._draining.cancel()
            tasks = [*self._background]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _retrying(self, what: str, step: Callable[[], Awaitable[Any]]) -> bool:
        """Run ``step`` until it succeeds or the host stops; False when it stopped first."""

        assert self._stop is not None
        attempt = 0
        while not self._stop.is_set():
            try:
                await step()
                return True
            except Exception as error:  # noqa: BLE001 - logged, and tried again
                if self.host is not None and self.host.installation_contract is not None:
                    return False  # another contract: asking again cannot change it (logged by the host)
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

    async def _serve_events(self) -> None:
        assert self.host is not None and self.host.live is not None
        if not self.event_handlers:
            return
        receiver = Receiver(self.host.identity.installation, self.team, "team", self.team, self.profile.runtime)
        await self.host.live.serve(SKILLS_EVENT_ADDRESS, receiver, self._event)

    async def _event(self, verified: VerifiedFrame) -> dict[str, Any]:
        """A team event: every agent this process runs gets it, on its own loop."""

        handler = self.event_handlers.get(verified.frame.kind)
        if handler is None:
            raise Unsupported(verified.frame.kind)
        for hosted in list(self.agents.values()):
            self._on_agent_loop(hosted, lambda agent=hosted.agent: handler(agent, verified), what=verified.frame.kind)
        return {}

    def _team_changed(self, changed: frozenset[str]) -> None:
        """On the host's loop: bring every attached agent's directory up to date (``custom_agents.team_directory``).

        An agent that left the view left saying so when its record's last state was exiting
        (``TeamAgents.departure_reason``).
        """

        from custom_agents.team_directory import apply_team_change

        assert self.team_agents is not None
        before, after = self._view_before, self.team_agents.live()
        self._view_before = after
        exited = frozenset(agent_id for agent_id in changed if agent_id not in after
                           and self.team_agents.departure_reason(agent_id) == "exiting")
        for hosted in list(self.agents.values()):
            self._on_agent_loop(hosted, lambda agent=hosted.agent: apply_team_change(agent, before, after, changed, exited),
                                what="team_directory")

    def _sync_directory(self, hosted: _HostedAgent) -> None:
        """A newly attached agent gets the whole current view."""

        from custom_agents.team_directory import apply_team_change

        if self.team_agents is None or not self.team_agents.ready.is_set():
            return  # the view's first reading tells every attached agent
        live = self.team_agents.live()
        self._on_agent_loop(hosted, lambda: apply_team_change(hosted.agent, {}, live, frozenset(live), frozenset()),
                            what="team_directory")

    def _on_agent_loop(self, hosted: _HostedAgent, work: Callable[[], Awaitable[Any]], *, what: str) -> None:
        agent_loop = getattr(hosted.agent, "_runtime_loop", None)

        def done(future: Any) -> None:
            if not future.cancelled() and future.exception() is not None:
                self.logger.error("An agent could not take a team update. event=agent_host_agent_update_failed "
                                  "runtime=%s agent=%s what=%s", self.profile.runtime, hosted.agent.id, what,
                                  exc_info=future.exception())

        if agent_loop is None or agent_loop is asyncio.get_running_loop():
            task = asyncio.get_running_loop().create_task(work())
            task.add_done_callback(done)
        elif not agent_loop.is_closed():
            asyncio.run_coroutine_threadsafe(work(), agent_loop).add_done_callback(done)

    # -- asking other agents (from an agent's own loop) ---------------------------------

    def _agent_audience(self, agent_id: str) -> dict[str, str]:
        assert self.host is not None
        return {"installation": self.host.identity.installation, "team": self.team, "target": "agent", "id": agent_id}

    async def _on_host_loop(self, coroutine: Awaitable[Any]) -> Any:
        assert self.loop is not None
        if asyncio.get_running_loop() is self.loop:
            return await coroutine
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, self.loop))  # type: ignore[arg-type]

    def cannot_ask(self, sender_agent: str) -> Optional[AskBlocked]:
        """Why this process cannot sign a request for ``sender_agent`` now, or None (``agent_requests.cannot_ask``).

        Read from any thread: it only reads the host's state. Waiting for the join or for the
        agent's session clears by itself; a process on another contract never joins or opens
        a session again, and a type no certificate can carry never gets one (module docstring).
        """

        if self._draining is not None or (self.host is not None and self.host.installation_contract is not None):
            return AskBlocked("this process runs another message contract than its installation and is draining",
                              clears=False)
        if self.host is None or self.host.live is None or self._joined is None or not self._joined.is_set():
            return AskBlocked("this process has not joined the team's messaging yet", clears=True)
        sender = self.agents.get(sender_agent)
        if sender is None:
            return AskBlocked(f"agent {sender_agent} is not attached to this process's host yet", clears=True)
        if sender.session is None:
            if sender.refusal is not None and sender.refusal.code == TYPE_NOT_CERTIFIABLE_CODE:
                return AskBlocked(f"agent {sender_agent} cannot have a session: {sender.refusal.message}",
                                  clears=False)
            return AskBlocked(f"agent {sender_agent} has no session yet", clears=True)
        return None

    async def agents_without_session(self) -> list[SessionlessAgent]:
        """The agents the team's live hosts list as running without a session (their records' components).

        Read on the host's loop, where the team's view is kept (``TeamAgents.host_components``);
        callable from any loop. Empty before the view is read.
        """

        async def read() -> list[SessionlessAgent]:
            if self.team_agents is None:
                return []
            return [SessionlessAgent(item.host, item.component, str(item.code), WITHOUT_SESSION_REASONS[str(item.code)])
                    for item in self.team_agents.host_components() if item.code in WITHOUT_SESSION_REASONS]

        return await self._on_host_loop(read())

    def _sender_session(self, sender_agent: str) -> None:
        blocked = self.cannot_ask(sender_agent)
        if blocked is not None:
            raise AgentUnreachable(blocked.reason, clears=blocked.clears)

    async def ask(self, kind: str, body: Mapping[str, Any], target_agent: str, *, sender_agent: str,
                  timeout: Optional[float] = None) -> dict[str, Any]:
        """A request to another agent, signed as ``sender_agent``; the reply's body.

        Raises ``AgentUnreachable`` when this process cannot send yet, and the live host's
        ``ServiceUnavailable`` (nobody answered) or ``RequestRefused``.
        """

        self._sender_session(sender_agent)
        assert self.host is not None and self.host.live is not None
        reply = await self._on_host_loop(self.host.live.request(kind, body, self._agent_audience(target_agent),
                                                                team=self.team, agent=sender_agent, timeout=timeout))
        return dict(reply.raw["body"])

    async def announce(self, kind: str, body: Mapping[str, Any], *, sender_agent: str) -> None:
        """A team event, signed as ``sender_agent``."""

        self._sender_session(sender_agent)
        assert self.host is not None and self.host.live is not None
        audience = {"installation": self.host.identity.installation, "team": self.team, "target": "team",
                    "id": self.team}
        await self._on_host_loop(self.host.live.publish(kind, body, audience, team=self.team, agent=sender_agent))

    def _audience(self) -> dict[str, str]:
        assert self.host is not None
        return {"installation": self.host.identity.installation, "team": self.team, "target": "team_services",
                "id": self.team}

    async def _request(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        assert self.host is not None and self.host.live is not None
        reply = await self.host.live.request(kind, body, self._audience(), team=self.team)
        return dict(reply.raw["body"])

    async def _close_earlier_sessions(self) -> None:
        """Find earlier sessions by their verified records, never a placement's mutable KV revision."""
        assert self.host is not None and self.host.records is not None and self.host.live is not None
        prefix = f"session.{self.host.identity.installation}.{self.team}."
        self._cleanup_needed = False
        try:
            keys = await self.host.records.keys("agent_session", prefix)
        except Exception:
            self._cleanup_needed = True
            self.logger.exception("Earlier sessions could not be listed; trying again during upkeep. "
                                  "event=agent_host_session_listing_failed runtime=%s", self.profile.runtime)
            return
        for key in keys:
            try:
                await self._close_earlier_session(key)
            except FrameRefused as refused:
                self.logger.warning("A stored session does not verify and counts as absent. "
                                    "event=agent_host_record_refused runtime=%s record=agent_session key=%s code=%s at=%s",
                                    self.profile.runtime, key, refused.code, refused.at)
            except Exception:
                self._cleanup_needed = True
                self.logger.exception("An earlier session could not be read; trying again during upkeep. "
                                      "event=agent_host_stale_session_close_failed runtime=%s key=%s",
                                      self.profile.runtime, key)

    async def _close_earlier_session(self, key: str) -> None:
        assert self.host is not None and self.host.records is not None and self.host.live is not None
        await self.host.records.take_trust_record("agent_session", key, now=self.host.clock())
        session_id = key.rsplit(".", 1)[-1]
        session = self.host.live.trust.store.agent_session(self.host.identity.installation, self.team, session_id)
        if session is None or session.runtime != self.profile.runtime:
            return
        hosted = self.agents.get(session.agent)
        if hosted is not None:
            # An opening can already be stored before its reply fills hosted.session.
            # Agent ids are new for each run; an attached id belongs to this run.
            return
        # A completed close is cheap to repeat and also finishes a failed placement removal.
        self._pending_closes[session.session] = (session.agent, "failed")
        await self._try_close(session.session)

    async def _try_close(self, session: str) -> None:
        pending = self._pending_closes.get(session)
        if pending is None:
            return
        agent_id, reason = pending
        try:
            reply = await self._request("sessions.close", {"request_id": f"close-{session}"[:128],
                                                          "session": session, "reason": reason})
            outcome = reply.get("outcome")
            if outcome not in {"ok", "not_found"}:
                raise RuntimeError(f"Session close was refused: {outcome}")
        except Exception:
            self.logger.warning("An agent session could not be closed; upkeep will try again. "
                                "event=agent_host_session_close_failed runtime=%s agent=%s session=%s",
                                self.profile.runtime, agent_id, session, exc_info=True)
            return
        self._pending_closes.pop(session, None)
        self.logger.info("An agent session close finished. event=agent_host_session_closed "
                         "runtime=%s agent=%s session=%s outcome=%s", self.profile.runtime, agent_id, session, outcome)

    async def _session_cleanup(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            if self._cleanup_needed:
                await self._close_earlier_sessions()
            for session in list(self._pending_closes):
                await self._try_close(session)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SESSION_CLEANUP_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _open(self, hosted: _HostedAgent) -> None:
        assert self._joined is not None and self.host is not None
        agent_id = str(hosted.agent.id)
        if hosted.left is not None and hosted.left.is_set():
            return  # A supervised retry must never reattach an agent that left.
        if agent_id in self.agents and self.agents[agent_id] is not hosted:
            self.logger.debug("The agent is attached already; its session is kept or being opened. runtime=%s agent=%s",
                              self.profile.runtime, agent_id)
            return
        if hosted.left is None:
            hosted.left = asyncio.Event()
        self.agents[agent_id] = hosted
        if self._draining is None:
            waits = [asyncio.ensure_future(self._joined.wait()), asyncio.ensure_future(self._drain_started.wait())]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits:
                    wait.cancel()
        if self._draining is not None:
            # The process runs another contract: no new agent gets a session here (module docstring).
            await self._drain_one(hosted)
            return
        self._sync_directory(hosted)
        await self._hold_session(hosted)

    async def _hold_session(self, hosted: _HostedAgent) -> None:
        """Open the agent's session, asking again until it opens, the agent leaves or the host stops; then serve it."""

        assert self.host is not None
        if hosted.opening:
            return
        hosted.opening = True
        try:
            opened = await self._open_session(hosted)
        finally:
            hosted.opening = False
        agent_id = str(hosted.agent.id)
        if not opened:
            return
        if self.agents.get(agent_id) is not hosted:  # the agent left while its session was opening
            await self._close(agent_id, "exited", hosted=hosted)
            return
        assert self.host.live is not None
        if hosted.subscription is None:
            receiver = Receiver(self.host.identity.installation, self.team, "agent", agent_id, self.profile.runtime)
            hosted.subscription = await self.host.live.serve(AGENT_ADDRESS, receiver, self._handler_for(hosted),
                                                             agent=agent_id)
        if self.agents.get(agent_id) is not hosted:
            # Detach may have completed while serve was subscribing. Close that new
            # subscription too and never publish a record after the removal.
            await self._close(agent_id, "exited", hosted=hosted)
            return
        hosted.written = None  # its record names the session it holds now
        await self._write_record(hosted)

    def _attached(self, hosted: _HostedAgent) -> bool:
        """Whether to keep asking for the agent's session: it is attached, the host runs and does not drain."""

        assert self._stop is not None
        return self.agents.get(str(hosted.agent.id)) is hosted and not self._stop.is_set() and self._draining is None

    async def _open_session(self, hosted: _HostedAgent) -> bool:
        """Ask for a session until one opens (True), or the agent leaves, the host stops or no certificate can name it."""

        assert self.host is not None
        agent_id = str(hosted.agent.id)
        refusals = unanswered = 0
        delay = self.refused_retry_seconds[0]
        while self._attached(hosted):
            if not AGENT_TYPE_PATTERN.match(hosted.agent_type):
                self._refused(hosted, TYPE_NOT_CERTIFIABLE_CODE,
                              "Its type's name is longer than 24 characters or has characters other than letters, "
                              "digits, '-' and '_', so no host certificate can name it.", retry_in=None)
                return False
            signed_with = self.host.identity.certificate
            try:
                body = await self._request("sessions.open", {
                    "request_id": f"open-{agent_id}-{hosted.openings + 1}", "agent": agent_id,
                    "agent_type": hosted.agent_type, "definition_sha256": hosted.definition})
            except Exception as error:  # noqa: BLE001 - not answered yet (the gap at start): logged, and asked again
                delay = RETRY_SECONDS[min(unanswered, len(RETRY_SECONDS) - 1)]
                log = self.logger.info if isinstance(error, ServiceUnavailable) else self.logger.warning
                log("A protocol 1 host step did not succeed yet; trying again. event=agent_host_retry step=session "
                    "runtime=%s agent=%s retry_in_seconds=%s error=%s", self.profile.runtime, agent_id, delay, error)
                unanswered += 1
                await self._pause(hosted, delay)
                continue
            unanswered = 0
            hosted.openings += 1  # answered: the next opening is another request
            if body["outcome"] == "ok":
                self._take_session(hosted, body["session"], refusals)
                return True
            refusal = await self._refusal(hosted, body, signed_with.agent_types)
            if refusal is None:
                continue  # joined again for its type: open at once
            refusals += 1
            if refusals > 1:
                delay = min(delay * 2, self.refused_retry_seconds[1])
            self._refused(hosted, *refusal, retry_in=delay)
            await self._pause(hosted, delay)
        return False

    def _take_session(self, hosted: _HostedAgent, session: dict[str, Any], refusals: int) -> None:
        assert self.host is not None and self.host.live is not None
        agent_id = str(hosted.agent.id)
        self.host.live.trust.add(json.dumps(session).encode(), now=self.host.clock())
        hosted.session = str(session["body"]["session"])
        self.host.live.identity = self.host.live.identity.with_session(agent_id, hosted.session)
        if hosted.refusal is not None:
            hosted.refusal = None
            self._components_changed()
        self.logger.info("An agent has its protocol 1 session. event=agent_host_session_opened runtime=%s agent=%s "
                         "agent_type=%s session=%s refusals_before=%s", self.profile.runtime, agent_id, hosted.agent_type,
                         hosted.session, refusals)

    async def _refusal(self, hosted: _HostedAgent, body: dict[str, Any],
                       certified: frozenset[str]) -> Optional[tuple[str, str]]:
        """The refusal's code and message; None when the host joined again for the agent's type, to open at once.

        ``certified`` are the types of the certificate the refused request was signed with.
        """

        assert self.host is not None
        outcome = str(body.get("outcome"))
        message = str(body.get("message") or outcome)
        if outcome == "already_placed":
            message = f"{message} (host {body.get('host')})"
        if outcome != "not_allowed" or hosted.agent_type in certified:
            return REFUSAL_CODES.get(outcome, SESSION_REFUSED_CODE), message
        try:
            catalog = frozenset(await asyncio.to_thread(self._catalog_types))
        except Exception:
            self.logger.exception("The team's agent catalog could not be read. event=agent_host_catalog_unreadable "
                                  "runtime=%s", self.profile.runtime)
            catalog = frozenset()
        if hosted.agent_type not in catalog:
            return REFUSAL_CODES["not_allowed"], f"{message} Its type is not in the team's agent catalog."
        try:
            rejoined = await self.host.rejoin([hosted.agent_type])
        except Exception as error:  # noqa: BLE001 - the authority did not answer: asked again later
            return TYPE_NOT_GRANTED_CODE, f"Joining again for agent type {hosted.agent_type} did not succeed: {error}"
        if hosted.agent_type in rejoined.granted:
            return None
        return TYPE_NOT_GRANTED_CODE, (rejoined.refusal
                                       or f"The installation authority did not grant agent type {hosted.agent_type}.")

    def _catalog_types(self) -> Iterable[str]:
        return self.catalog_types() if self.catalog_types is not None else self.profile.agent_types

    def _refused(self, hosted: _HostedAgent, code: str, message: str, *, retry_in: Optional[float]) -> None:
        """Note why the agent has no session: logged once per change, listed in the host record (module docstring)."""

        assert self.host is not None
        agent_id = str(hosted.agent.id)
        earlier = hosted.refusal
        if earlier is not None and earlier.code == code:
            self.logger.debug("An agent's session was refused again. runtime=%s agent=%s code=%s retry_in_seconds=%s",
                              self.profile.runtime, agent_id, code, retry_in)
            return
        hosted.refusal = _Refusal(code, message, earlier.since if earlier is not None else self.host.clock())
        then = ("its session is not asked for while it runs" if retry_in is None
                else "its session is asked for again until it opens")
        self.logger.error("An agent got no protocol 1 session, so requests to it are not answered; %s. "
                          "event=agent_host_session_refused runtime=%s agent=%s agent_type=%s code=%s message=%s "
                          "retry_in_seconds=%s", then, self.profile.runtime, agent_id, hosted.agent_type, code, message,
                          retry_in)
        self._components_changed()

    async def _pause(self, hosted: _HostedAgent, seconds: float) -> None:
        """Wait ``seconds``, or less when the agent leaves or the host stops."""

        assert self._stop is not None
        events = [self._stop] + ([hosted.left] if hosted.left is not None else [])
        waits = [asyncio.ensure_future(event.wait()) for event in events]
        try:
            await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()

    def _components(self) -> list[dict[str, str]]:
        """The host record's components: each agent without a session, and why (module docstring)."""

        refused = sorted(((hosted.refusal, hosted) for hosted in self.agents.values() if hosted.refusal is not None),
                         key=lambda item: (item[0].since, str(item[1].agent.id)))
        components = [{"component": refused_session_component(hosted.agent_type, str(hosted.agent.id)),
                       "state": "unhealthy", "code": refusal.code} for refusal, hosted in refused]
        if len(components) > HOST_COMPONENTS_MAX:
            components = components[:HOST_COMPONENTS_MAX - 1] + [
                {"component": MORE_AGENTS_COMPONENT, "state": "unhealthy", "code": MORE_AGENTS_CODE}]
        return components

    def _components_changed(self) -> None:
        """Write the host record again now, so the Status page follows (``MessageHost.report_now``)."""

        if self.host is not None and self.host.records is not None and self._stop is not None \
                and not self._stop.is_set():
            self._spawn(self.host.report_now, "report")

    def _handler_for(self, hosted: _HostedAgent) -> Callable[[VerifiedFrame], Awaitable[dict[str, Any]]]:
        async def handle(verified: VerifiedFrame) -> dict[str, Any]:
            if self.agents.get(str(hosted.agent.id)) is not hosted:
                raise ServiceUnavailable("the addressed agent has detached")
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
        self._pending_departures[agent_id] = (hosted, reason)
        hosted.state, hosted.accepting_work, hosted.since = "exiting", False, self.host.clock()
        if hosted.write_due is not None:
            hosted.write_due.cancel()
            hosted.write_due = None
        if hosted.left is not None:
            hosted.left.set()  # a wait to ask for its session again ends at once
        if hosted.refusal is not None:
            self._components_changed()  # it is no longer listed
        # Readers learn it is leaving before its session ends (the lead fails what it was asked).
        try:
            await self._finish_departure(agent_id)
        finally:
            self.host.live.identity = self.host.live.identity.without_session(agent_id)

    async def _finish_departure(self, agent_id: str) -> None:
        """Finish an exit in order; cancellation leaves the complete job for upkeep."""

        assert self.host is not None and self.host.live is not None
        pending = self._pending_departures.get(agent_id)
        if pending is None:
            return
        hosted, reason = pending
        async with hosted.close_lock:
            try:
                while True:
                    session = hosted.session
                    if not await self._finish_session(hosted, session, reason):
                        return
                    if hosted.session == session:
                        break
                await self._remove_record(hosted.key(self.team), agent_id)
                if hosted.subscription is not None:
                    return  # its session is closed; upkeep still removes the address
            except Exception:
                self.logger.warning("An agent's exit did not finish; upkeep will try again. "
                                    "event=agent_record_exit_pending runtime=%s agent=%s", self.profile.runtime,
                                    agent_id, exc_info=True)
                return
            self._pending_departures.pop(agent_id, None)

    async def _finish_session(self, hosted: _HostedAgent, session: Optional[str], reason: str) -> bool:
        """Close the captured session, also when a replacement arrives during an await."""

        if hosted.subscription is not None:
            try:
                await hosted.subscription.unsubscribe()
                hosted.subscription = None
                hosted.subscription_failures = 0
            except Exception:
                hosted.subscription_failures += 1
                if hosted.subscription_failures == 1:
                    self.logger.warning("An agent's address could not be removed; its session still closes and "
                                        "upkeep will retry the address. event=agent_record_unsubscribe_pending "
                                        "runtime=%s agent=%s", self.profile.runtime, hosted.agent.id, exc_info=True)
        if session is None or session in hosted.closed_sessions:
            return True
        if session not in hosted.closing_sessions:
            await self._write_record(hosted)
            self._pending_closes[session] = (str(hosted.agent.id), reason)
            hosted.closing_sessions.add(session)
        await self._try_close(session)
        if session in self._pending_closes:
            return False
        hosted.closed_sessions.add(session)
        return True

    def _noted(self, agent_id: str, state: str, accepting_work: bool) -> None:
        hosted = self.agents.get(agent_id)
        if hosted is None or hosted.state == "exiting" or (state, accepting_work) == (hosted.state, hosted.accepting_work):
            return
        hosted.state, hosted.accepting_work = state, accepting_work
        hosted.since = self.host.clock() if self.host is not None else None
        self._record_soon(hosted)

    def _record_soon(self, hosted: _HostedAgent) -> None:
        """Write the record now, or when the interval since the last write has passed."""

        assert self.loop is not None
        if hosted.session is None or hosted.write_due is not None:
            return  # the session's opening writes it, or a write is already due
        wait = hosted.written_at + AGENT_RECORD_INTERVAL_SECONDS - self.loop.time()
        if wait <= 0:
            self._spawn(lambda: self._write_record(hosted), "write_agent_record")
            return

        def due() -> None:
            hosted.write_due = None
            self._spawn(lambda: self._write_record(hosted), "write_agent_record")

        hosted.write_due = self.loop.call_later(wait, due)

    async def _write_record(self, hosted: _HostedAgent) -> None:
        """Sign and store the agent's record as its state is now; a failed write is tried again."""

        assert self.host is not None and self.host.records is not None and self.loop is not None
        async with hosted.write_lock:
            current = (hosted.state, hosted.accepting_work, hosted.session)
            if hosted.session is None or hosted.session in hosted.closed_sessions:
                return
            attached = self.agents.get(str(hosted.agent.id))
            if attached is not None and attached is not hosted:
                return  # a replacement attachment owns this id now
            elapsed = self.loop.time() - hosted.written_at
            if current == hosted.written and elapsed < AGENT_RECORD_REFRESH_SECONDS:
                return
            if hosted.state != "exiting" and elapsed < AGENT_RECORD_INTERVAL_SECONDS:
                self._record_soon(hosted)
                return
            now = self.host.clock()
            # Above every earlier sequence for the key, also if the agent's id were ever used again.
            hosted.record_sequence = max(hosted.record_sequence + 1, int(now.timestamp() * 1000))
            body: dict[str, Any] = {"team": self.team, "agent": str(hosted.agent.id), "agent_type": hosted.agent_type,
                                    "session": hosted.session, "state": hosted.state,
                                    "accepting_work": hosted.accepting_work,
                                    "since": format_timestamp(hosted.since or now)}
            if hosted.display_name:
                body["display_name"] = hosted.display_name
            key = hosted.key(self.team)
            try:
                document = self.host.records.signed("agent", key, body, sequence=hosted.record_sequence, issued_at=now,
                                                    expires_at=now + AGENT_RECORD_LIFETIME)
                await self.host.records.put("agent", key, document)
            except Exception:
                self._record_failed(hosted)
                return
            if hosted.write_failures:
                self.logger.info("An agent's record can be written again. event=agent_record_write_recovered "
                                 "runtime=%s agent=%s failures=%s", self.profile.runtime, hosted.agent.id,
                                 hosted.write_failures)
                hosted.write_failures = 0
            hosted.written, hosted.written_at = current, self.loop.time()
        if (hosted.state, hosted.accepting_work, hosted.session) != current:
            self._record_soon(hosted)  # it changed while this one was written

    def _record_failed(self, hosted: _HostedAgent) -> None:
        assert self.loop is not None
        hosted.write_failures += 1
        if hosted.write_failures == 1:
            self.logger.warning("An agent's record could not be written; trying again. "
                                "event=agent_record_write_failed runtime=%s agent=%s state=%s",
                                self.profile.runtime, hosted.agent.id, hosted.state, exc_info=True)
        if hosted.state != "exiting" and hosted.write_due is None:
            delay = min(60.0, AGENT_RECORD_INTERVAL_SECONDS * 2 ** min(hosted.write_failures - 1, 5))
            hosted.write_due = self.loop.call_later(delay, lambda: self._retry_record(hosted))

    def _retry_record(self, hosted: _HostedAgent) -> None:
        hosted.write_due = None
        if str(hosted.agent.id) in self.agents:
            self._spawn(lambda: self._write_record(hosted), "write_agent_record")

    async def _remove_record(self, key: str, agent_id: str) -> None:
        assert self.host is not None and self.host.records is not None
        if agent_id in self.agents:
            return  # a new attachment using this id owns its record now
        stored = await self.host.records.read_report("agent", key, now=self.host.clock())
        if stored is None or stored.record.signer is None or stored.record.signer.runtime != self.profile.runtime:
            return
        if not await self.host.records.delete("agent", key, last_revision=stored.revision):
            raise RuntimeError("the agent record changed during removal")

    async def _record_upkeep(self) -> None:
        """Retry interrupted exits and startup cleanup, and refresh unchanged records."""

        assert self._stop is not None and self.loop is not None
        while not self._stop.is_set():
            for agent_id in list(self._pending_departures):
                self._start_record_job(("exit", agent_id), lambda agent_id=agent_id: self._finish_departure(agent_id))
            if self._record_cleanup_needed:
                self._start_record_job(("cleanup", ""), self._remove_earlier_records)
            for hosted in list(self.agents.values()):
                if self.loop.time() - hosted.written_at >= AGENT_RECORD_REFRESH_SECONDS and hosted.write_due is None:
                    self._start_record_job(("refresh", str(hosted.agent.id)),
                                           lambda hosted=hosted: self._refresh_record(hosted))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=AGENT_RECORD_CLEANUP_SECONDS)
            except asyncio.TimeoutError:
                pass

    def _start_record_job(self, key: tuple[str, str], work: Callable[[], Awaitable[Any]]) -> None:
        """One job per agent and operation; a refused repair never holds another agent's refresh."""

        current = self._record_jobs.get(key)
        if current is not None and not current.done():
            return
        task = self._spawn(work, f"record_{key[0]}")
        self._record_jobs[key] = task

        def finished(done: asyncio.Task[Any]) -> None:
            if self._record_jobs.get(key) is done:
                self._record_jobs.pop(key, None)

        task.add_done_callback(finished)

    async def _refresh_record(self, hosted: _HostedAgent) -> None:
        """Repair a removed placement through the service that owns it, then refresh our report."""

        assert self.host is not None and self.host.records is not None
        if hosted.session is None or hosted.state == "exiting":
            return
        try:
            placement = await self.host.records.read_report("placement", hosted.key(self.team), now=self.host.clock())
            if placement is None:
                self.logger.warning("An attached agent's placement is missing; opening its session again. "
                                    "event=agent_record_placement_missing runtime=%s agent=%s",
                                    self.profile.runtime, hosted.agent.id)
                await self._hold_session(hosted)
            elif placement.record.body.host != self.profile.runtime:
                self.logger.warning("An attached agent is placed on another host; this host will not overwrite its record. "
                                    "event=agent_record_placement_changed runtime=%s agent=%s placed_host=%s",
                                    self.profile.runtime, hosted.agent.id, placement.record.body.host)
                return
            self._record_soon(hosted)
        except Exception:
            self.logger.warning("An agent's record upkeep did not finish; trying again during upkeep. "
                                "event=agent_record_refresh_failed runtime=%s agent=%s",
                                self.profile.runtime, hosted.agent.id, exc_info=True)

    async def _remove_earlier_records(self) -> None:
        """Remove the records of agents this host ran before it restarted (module docstring)."""

        assert self.host is not None and self.host.records is not None
        self._record_cleanup_needed = False
        try:
            keys = await self.host.records.keys("agent", f"{self.team}.")
        except Exception:
            self._record_cleanup_needed = True
            self.logger.warning("Earlier agent records could not be listed; upkeep will try again. "
                                "event=agent_record_listing_failed runtime=%s", self.profile.runtime, exc_info=True)
            return
        for key in keys:
            agent_id = key.removeprefix(f"{self.team}.")
            if agent_id == key or agent_id in self.agents:
                continue
            try:
                await self._remove_earlier_record(key, agent_id)
            except Exception:
                self._record_cleanup_needed = True
                # One record that cannot be read or removed never keeps the agents from their sessions.
                self.logger.exception("An earlier agent record could not be removed. "
                                      "event=agent_record_stale_remove_failed runtime=%s key=%s", self.profile.runtime, key)

    async def _remove_earlier_record(self, key: str, agent_id: str) -> None:
        """Remove the record when it verifies as one this host wrote; one that does not verify is left and logged."""

        assert self.host is not None and self.host.records is not None
        try:
            stored = await self.host.records.read_report("agent", key, now=self.host.clock())
        except FrameRefused as refused:
            self._record_cleanup_needed = refused.code in {"unknown_key", "revocations_not_current"} or self._record_cleanup_needed
            if (key, refused.code) not in self._stale_refusals:
                self._stale_refusals.add((key, refused.code))
                self.logger.warning("A stored record does not verify and counts as absent. event=agent_host_record_refused "
                                    "runtime=%s record=agent key=%s code=%s at=%s", self.profile.runtime, key, refused.code,
                                    refused.at)
            return
        if stored is None or stored.record.signer is None or stored.record.signer.runtime != self.profile.runtime:
            return
        if agent_id in self.agents:
            return
        await self._remove_record(key, agent_id)
        self.logger.info("Removed an agent record an earlier run of this host left behind. "
                         "event=agent_record_stale_removed runtime=%s agent=%s", self.profile.runtime, agent_id)

    async def _renew_sessions(self) -> None:
        while True:
            await asyncio.sleep(SESSION_RENEW_SECONDS)
            for hosted in list(self.agents.values()):
                if hosted.session is not None:
                    await self._renew(hosted)

    async def _renew(self, hosted: _HostedAgent) -> None:
        """Renew the agent's session; a refused renewal opens a new session (module docstring)."""

        assert self.host is not None and self.host.live is not None and hosted.session is not None
        agent_id, session = str(hosted.agent.id), hosted.session
        key = f"session.{self.host.identity.installation}.{self.team}.{session}"
        held = self.host.live.trust.store.held_record("agent_session", key)
        try:
            body = await self._request("sessions.renew", {"request_id": session_renewal_id(session, held[0] if held else 0),
                                                          "session": session})
        except Exception:
            # Asked again at the next renewal, with the same id: it renews the same record.
            self.logger.exception("An agent's session could not be renewed. event=agent_host_session_renew_failed "
                                  "runtime=%s agent=%s", self.profile.runtime, agent_id)
            return
        if body["outcome"] == "ok":
            self.host.live.trust.add(json.dumps(body["session"]).encode(), now=self.host.clock())
            return
        self.logger.warning("An agent's session was not renewed; the host opens a new one. "
                            "event=agent_host_session_renew_refused runtime=%s agent=%s session=%s outcome=%s message=%s",
                            self.profile.runtime, agent_id, session, body.get("outcome"), body.get("message"))
        if self.agents.get(agent_id) is hosted and self._draining is None:
            self._spawn(lambda: self._hold_session(hosted), "reopen_session")

    # -- another contract (module docstring) ---------------------------------------------

    def _other_contract(self, theirs: str) -> None:
        """On the host's loop: the process runs another contract than the installation's ``theirs``; drain once."""

        if self._draining is None and self.loop is not None:
            self._draining = self._spawn(lambda: self._drain(theirs), "drain")
            self._drain_started.set()

    async def _drain(self, theirs: str) -> None:
        """Drain every agent, then give them up once none holds work (module docstring)."""

        assert self.host is not None and self._stop is not None
        joined = self.host.live is not None
        reason = (f"Its process runs another message contract ({self.host.contract_sha256}) than its installation "
                  f"({theirs}).")
        for hosted in list(self.agents.values()):
            await self._drain_one(hosted, reason)
        # Stop taking work even when publishing the host's status fails.
        if joined:
            await self.host.drain()
        while not self._stop.is_set():
            holding = [str(hosted.agent.id) for hosted in list(self.agents.values()) if await self._still_holds(hosted)]
            if not holding:
                break
            self.logger.debug("Agents still hold work while the process drains. runtime=%s agents=%s",
                              self.profile.runtime, ",".join(holding))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.drain_poll_seconds)
            except asyncio.TimeoutError:
                pass
        if self._stop.is_set():
            return
        if not joined:
            self.logger.warning("The process drained; it never joined, so it holds no session. "
                                "event=agent_host_drained runtime=%s", self.profile.runtime)
            return
        for agent_id in list(self.agents):
            await self._close(agent_id, "exited")
        for session in list(self._pending_closes):
            await self._try_close(session)
        if self._pending_closes:
            raise RuntimeError("Session closes still need to finish before this host gives up its agents.")
        await self.host.finish_drain()
        self.logger.warning("The process drained and gave up its agents. event=agent_host_drained runtime=%s",
                            self.profile.runtime)

    async def _drain_one(self, hosted: _HostedAgent, reason: Optional[str] = None) -> None:
        """Start draining one agent on its own loop, once."""

        if hosted.draining:
            return
        assert self.host is not None
        reason = reason or (f"Its process runs another message contract ({self.host.contract_sha256}) than its "
                            f"installation ({self.host.installation_contract}).")
        await self._on_agent_loop_result(hosted, lambda: self.drain_agent(hosted.agent, reason))
        hosted.draining = True

    async def _still_holds(self, hosted: _HostedAgent) -> bool:
        """Whether the agent holds work, asked on its own loop; an agent that cannot tell counts as holding it."""

        try:
            return bool(await self._on_agent_loop_result(hosted, lambda: self.holds_work(hosted.agent)))
        except (Exception, asyncio.CancelledError):
            current = asyncio.current_task()
            assert current is not None
            if current.cancelling():
                raise
            exited = getattr(hosted.agent, "_should_exit", None)
            if exited is not None and exited.is_set():
                return False
            if not hosted.holds_work_failed:
                hosted.holds_work_failed = True
                self.logger.exception("Whether an agent still holds work could not be read; the process keeps it "
                                      "until it can. event=agent_host_holds_work_failed runtime=%s agent=%s",
                                      self.profile.runtime, hosted.agent.id)
            return True

    async def _on_agent_loop_result(self, hosted: _HostedAgent, work: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        agent_loop = getattr(hosted.agent, "_runtime_loop", None)
        if agent_loop is None or agent_loop is asyncio.get_running_loop():
            return await work()
        if agent_loop.is_closed() or not agent_loop.is_running():
            raise RuntimeError("The agent's event loop is no longer running.")
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(work(), agent_loop))


def agent_record_state(agent: Any) -> tuple[str, bool]:
    """The agent's state for its record: paused, busy or idle, and whether it accepts work.

    A worker winding down after a lost launch lease takes no work, nor does an agent whose
    process runs another message contract (``custom_agents.contract_drain``).
    """

    from custom_agents.contract_drain_state import is_contract_draining
    from custom_agents.lease_loss_wind_down import is_winding_down

    if getattr(agent, "paused", False):
        state = "paused"
    elif getattr(agent, "is_executing", False):
        state = "busy"
    else:
        state = "idle"
    if not getattr(agent, "_worker_ready", True):
        return "starting", False
    return state, state != "paused" and not is_winding_down(agent) and not is_contract_draining(agent)


def report_agent_state(agent: Any) -> None:
    """Tell this process's host the agent's state (it writes the record when it changed)."""

    host = process_host()
    if host is None:
        return
    try:
        host.report(str(agent.id), *agent_record_state(agent))
    except Exception:
        host.logger.warning("An agent's state could not be reported to its host. event=agent_state_report_failed "
                            "agent=%s", getattr(agent, "id", None), exc_info=True)


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
                    nc=adapter.nats_connection, state=agent_record_state(agent))
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
    loads, read again for a type a session was refused for) and a session for the lead and
    each worker; a startup agent's host for its one agent. Relayed people's commands are answered with ``custom_agents.relayed_task_controls``.
    """

    from custom_agents.agent_catalog import load_agent_catalog
    from custom_agents.live_controls import EVENT_HANDLERS, LIVE_CONTROL_HANDLERS
    from custom_agents.relayed_task_controls import RELAYED_HANDLERS
    from custom_transport.protocol1.joining import certificate_agent_types
    from installation_authority.expected_hosts import read_expected_hosts
    from runtime_settings.worker_pools import WorkerPoolsConfig

    catalog_types: Optional[Callable[[], Iterable[str]]] = None
    if is_lead:
        types = certificate_agent_types(load_agent_catalog().agent_types, _LOGGER)
        # The limit the launcher listed (the expected-hosts file names public keys only), so the join
        # never asks for more than was listed; computed the launcher's way when there is no entry yet.
        listed = read_expected_hosts(layout.trust_expected_hosts_path).get(runtime)
        limit = listed.session_limit if listed is not None else lead_session_limit(
            WorkerPoolsConfig.model_validate(dict(worker_pools or {})))
        profile = lead_host_profile(team, types, limit)
        # Read again when a type is refused: specialists created while the process runs.
        catalog_types = lambda: load_agent_catalog().agent_types  # noqa: E731
    else:
        profile = startup_agent_host_profile(team, runtime, agent_type)
    if profile.runtime != runtime:
        raise ValueError(f"the launcher named this host {runtime!r}, but its profile is {profile.runtime!r}")
    return AgentProcessHost(layout, profile, team, handlers={**RELAYED_HANDLERS, **LIVE_CONTROL_HANDLERS},
                            event_handlers=dict(EVENT_HANDLERS), catalog_types=catalog_types)


def lead_session_limit(pools: Any) -> int:
    """Sessions the team lead's host may hold: the lead and every worker the team may run."""

    return int(pools.max_total_workers_per_team) + 1


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
  stable only across retries of that one request: a repeated id gets the first answer back
  (README "Stored outcomes").
- **Only verified records.** The placements read at start, to close the sessions an
  earlier run left, are verified like every record (signed by the team services and
  stored under their own key); one that does not verify counts as absent and is logged,
  and one bad key never keeps the agents from their sessions.
- **The agent's own loop does the work.** A request for a thread worker arrives on the
  host's loop and runs on the worker's loop (``run_coroutine_threadsafe``), where its task
  manager lives.
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
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Optional

import rfc8785

from custom_messages.wire.encoding import b64url
from custom_messages.wire.frame import Receiver, VerifiedFrame
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.host_runtime import HOST_COMPONENTS_MAX, MessageHost
from custom_transport.protocol1.joining import AGENT_TYPE_PATTERN, HostProfile
from custom_transport.protocol1.live import ServiceUnavailable, Unsupported
from custom_transport.protocol1.state_records import StoredRecord
from release_version import release_version
from runtime_settings.instance_layout import InstanceLayout

# The team lead's process: the lead and the workers it starts as threads.
LEAD_HOST_RUNTIME = "lead"
LEAD_HOST_ROLES = ("lead_host", "worker_host")
STARTUP_AGENT_HOST_ROLES = ("worker_host",)
AGENT_ADDRESS = "ctl.agent.{agent}"
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
_NOT_IN_TOKEN = re.compile(r"[^A-Za-z0-9_-]")
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
    session: Optional[str] = None
    subscription: Any = None
    # Why its session was last refused, until one opens (module docstring).
    refusal: Optional[_Refusal] = None
    # Openings that got an answer: the next one has a new request id.
    openings: int = 0
    # A session is being opened for it now; never two at once.
    opening: bool = False
    # Set when it leaves, so a wait to ask again ends at once.
    left: Optional[asyncio.Event] = None


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
                                    sessions_active=lambda: sum(1 for item in self.agents.values() if item.session),
                                    components=self._components)
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
            try:
                await self._close_earlier_session(key)
            except Exception:
                # One placement that cannot be read or closed never keeps the agents from their sessions.
                self.logger.exception("An earlier session could not be closed. event=agent_host_stale_session_close_failed "
                                      "runtime=%s key=%s", self.profile.runtime, key)

    async def _close_earlier_session(self, key: str) -> None:
        stored = await self._placement(key)
        if stored is None:
            return
        placed = stored.record.body
        if placed.host != self.profile.runtime or placed.agent in self.agents:
            return
        session = f"s-{placed.agent}-{stored.revision}"
        reply = await self._request("sessions.close", {"request_id": f"close-{session}"[:128], "session": session,
                                                       "reason": "failed"})
        self.logger.info("Closed a session an earlier run of this host left behind. event=agent_host_stale_session_closed "
                         "runtime=%s agent=%s outcome=%s", self.profile.runtime, placed.agent, reply.get("outcome"))

    async def _placement(self, key: str) -> Optional[StoredRecord]:
        """A placement as verified (the registry lets only the team services sign one); None when absent or refused."""

        assert self.host is not None and self.host.records is not None
        try:
            return await self.host.records.read_report("placement", key, now=self.host.clock())
        except FrameRefused as refused:
            self.logger.warning("A stored record does not verify and counts as absent. event=agent_host_record_refused "
                                "runtime=%s record=placement key=%s code=%s at=%s", self.profile.runtime, key,
                                refused.code, refused.at)
            return None

    async def _open(self, hosted: _HostedAgent) -> None:
        assert self._joined is not None and self.host is not None
        agent_id = str(hosted.agent.id)
        if agent_id in self.agents:
            self.logger.debug("The agent is attached already; its session is kept or being opened. runtime=%s agent=%s",
                              self.profile.runtime, agent_id)
            return
        hosted.left = asyncio.Event()
        self.agents[agent_id] = hosted
        await self._joined.wait()
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

    def _attached(self, hosted: _HostedAgent) -> bool:
        assert self._stop is not None
        return self.agents.get(str(hosted.agent.id)) is hosted and not self._stop.is_set()

    async def _open_session(self, hosted: _HostedAgent) -> bool:
        """Ask for a session until one opens (True), or the agent leaves, the host stops or no certificate can name it."""

        assert self.host is not None
        agent_id = str(hosted.agent.id)
        refusals = unanswered = 0
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
            first, last = self.refused_retry_seconds
            delay = min(first * 2 ** (refusals - 1), last)
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
            self._spawn(self.host.report_now())

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
        if hosted.left is not None:
            hosted.left.set()  # a wait to ask for its session again ends at once
        if hosted.refusal is not None:
            self._components_changed()  # it is no longer listed
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
        if self.agents.get(agent_id) is hosted:
            self._spawn(self._hold_session(hosted))


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
    loads, read again for a type a session was refused for) and a session for the lead and
    each worker; a startup agent's host for its one agent. Relayed people's commands are answered with ``custom_agents.relayed_task_controls``.
    """

    from custom_agents.agent_catalog import load_agent_catalog
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
    return AgentProcessHost(layout, profile, team, handlers=dict(RELAYED_HANDLERS), catalog_types=catalog_types)


def lead_session_limit(pools: Any) -> int:
    """Sessions the team lead's host may hold: the lead and every worker the team may run."""

    return int(pools.max_total_workers_per_team) + 1


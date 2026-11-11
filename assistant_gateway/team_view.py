"""The team as the gateway sees it: who is live and who leads, from the team's signed agent records.

Until step 7c the gateway learned this from the presence heartbeat, asked for the team
lead on the operational subject at start ("discovery"), and polled the lead's consumer and
its heartbeat's age every 5 seconds. Now the gateway's own host reads the team's live
agents (``custom_transport.protocol1.team_agents``), and this module turns that view into
what the gateway already acts on:

- **The team lead.** ``TeamLeadFollower`` compares the gateway's route to the lead with
  the records, at start and on every change, and runs the manager client's existing
  "available" and "unavailable" transitions when they differ (its docstring says how).
  The lead saying it leaves makes it unavailable at once. A lead whose host stopped
  reporting (a crash) is unavailable at most 35 seconds after the host's last liveness
  write: the record's 30 seconds plus the reader's 5-second check, about the 30 seconds
  after which the presence heartbeat counted the lead gone.
- **The team's members.** Each live agent's state goes to the Team page and the Status
  page's records (``GatewayTeamPresence``) as it changes. What the records do not carry
  (skills, Status page findings, whether a busy agent could start more) is asked with
  ``agent.status`` from each live agent every 20 seconds, the old heartbeat's period, and
  merged in.
- **Held guidance** is woken when the lead's record changes, as the lead's heartbeat did.
- **Agents without a session.** Every running agent holds a session (plan 13B.5); one whose
  session the team services refused has no record and no address, so it cannot report
  findings itself. Its host lists it in the host record's ``components`` instead
  (``custom_agents.agent_host``), and ``host_findings`` turns those into Status page
  problems: which agents cannot run their work, why, and what to do. A finding goes once
  the agent's session opens or the agent stops; the wait for the first answer at start is
  not listed.
- **A host on another contract** (contract README "Versions"). Its agents stay listed, so
  people's commands about the tasks they hold still reach them, but they take no new work:
  the Team page shows them as not accepting work, and a team lead whose host runs another
  contract counts as unavailable, so chats are not sent to it. ``host_findings`` adds a
  Status page problem for each such host, naming both digests and what to do.

Everything here runs on the gateway's own loop. Each part of the view (the reader, the
details) runs supervised, as does the gateway's lead follower: one that fails, or ends before the gateway stops, is
logged and started again, so it never stops the others silently. Nothing waits for the
records to be read without also stopping when the gateway stops. A member whose departure
is only the reader stopping (``READER_STOPPED``) has not left, so nothing is said about it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Protocol

from custom_agents.agent_host import (
    LEAD_HOST_RUNTIME,
    MORE_AGENTS_CODE,
    REFUSAL_CODES,
    TYPE_NOT_CERTIFIABLE_CODE,
    TYPE_NOT_GRANTED_CODE,
)
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity
from custom_messages.enums import MessageMetaType
from custom_messages.wire.kinds import CONTRACT_SHA256
from custom_transport.protocol1.live import RequestRefused, ServiceUnavailable
from custom_transport.protocol1.team_agents import READER_STOPPED, HostComponent, TeamAgent, TeamAgents

# The old heartbeat's period: how often the gateway asks each live agent for its details.
DETAILS_POLL_SECONDS = 20.0
DETAILS_TIMEOUT_SECONDS = 5.0
# How long a view task that failed, or ended before the gateway stopped, waits before it starts again: the
# first wait, doubling up to the second (``_supervised``). A task that ran this long starts from the first again.
RESTART_SECONDS = (5.0, 300.0)
# How long the gateway's start waits for a team lead in the records before it decides (``TeamLeadFollower.start``):
# the old discovery's wait.
LEAD_START_SECONDS = 15.0
# How often the lead follower checks the route against the records when nothing changed (``TeamLeadFollower.run``).
LEAD_CHECK_SECONDS = 5.0
SOURCE = "agent_records"
_LOGGER = logging.getLogger(__name__)

# Why agents have no session, by their component's code in the host record, and what an administrator
# does about it (module docstring). ``{where}`` names the process they run in.
_WITHOUT_SESSION: dict[str, tuple[str, str]] = {
    REFUSAL_CODES["limit_reached"]: (
        "{where} already runs as many agents as the team's worker limit allows.",
        "Raise the worker limit (agent.worker_pools.max_total_workers_per_team) and make a team-wide restart, or "
        "wait: when another worker finishes, the agent gets its session without a restart."),
    REFUSAL_CODES["not_allowed"]: (
        "The agent type is not among the types {where} may run, and the team's agent catalog no longer has it.",
        "Make a team-wide restart, so the process starts with the current agent catalog."),
    TYPE_NOT_GRANTED_CODE: (
        "The agent type is in the team's agent catalog, but the installation authority did not add it to the "
        "types {where} may run.",
        "Make a team-wide restart. If this comes back, send the logs with event=protocol1_rejoin_refused."),
    TYPE_NOT_CERTIFIABLE_CODE: (
        "The agent type's name is longer than 24 characters or has characters other than letters, digits, '-' "
        "and '_', so no process can be allowed to run it.",
        "Rename the agent type to at most 24 letters, digits, '-' or '_', and start its agents again."),
    REFUSAL_CODES["already_placed"]: (
        "The team services have each placed on another process.",
        "Make a team-wide restart. If this comes back, send the logs with event=agent_host_session_refused."),
    MORE_AGENTS_CODE: (
        "{where} runs more agents without a session than its record can list; its log names them all.",
        "Look for event=agent_host_session_refused in that process's log for why, and act on it."),
}
_WITHOUT_SESSION_OTHERWISE = (
    "The team services could not give a session.",
    "Sessions are asked for again every few minutes. If this lasts, make a team-wide restart and send the logs "
    "with event=agent_host_session_refused.")
# What an agent without a session cannot do, whatever the reason.
_WITHOUT_SESSION_MEANS = (
    "An agent without a session answers no requests, other agents and the Team page do not see it, and its exit "
    "goes unseen; if it is TeamLead, chats are answered as unavailable. It still works through tasks already "
    "queued for it.")
_AGENTS_NAMED = 10


def host_findings(components: list[HostComponent], other_contracts: Optional[Mapping[str, str]] = None,
                  contract_sha256: str = CONTRACT_SHA256) -> list[StatusFindingV1]:
    """Status page problems from the hosts' unhealthy components, one per host and reason, and from each host
    that runs another contract than ``contract_sha256``, the gateway's (module docstring).

    Each agent is named by its component, ``<agent type>-<agent id>``. A component of
    another kind (none yet) is shown by its name and code.
    """

    findings = [_other_contract_finding(host, theirs, contract_sha256)
                for host, theirs in sorted((other_contracts or {}).items())]
    grouped: dict[tuple[str, str, str], list[str]] = {}
    for item in components:
        grouped.setdefault((item.host, item.code or "", item.state), []).append(item.component)
    for (host, code, state), names in sorted(grouped.items()):
        where = "the team lead's process" if host == LEAD_HOST_RUNTIME else f"process {host}"
        place = _capitalized(where)
        details = f"host: {host}; code: {code or 'none'}; components: {', '.join(names)}"[:2000]
        if not (code in _WITHOUT_SESSION or code.startswith("session_") or code == TYPE_NOT_CERTIFIABLE_CODE):
            findings.append(StatusFindingV1(
                finding_id=f"host_component:{host}:{','.join(sorted(names))}"[:200],
                severity=StatusSeverity.PROBLEM if state == "unhealthy" else StatusSeverity.WARNING,
                title=f"Part of {where} is {state}"[:200],
                summary=f"{place} reports {', '.join(names)} as {state}{f' ({code})' if code else ''}."[:2000],
                action="Check that process's log, or make a team-wide restart.", source=host, details=details))
            continue
        why, action = _WITHOUT_SESSION.get(code, _WITHOUT_SESSION_OTHERWISE)
        if code == MORE_AGENTS_CODE:
            title, named = f"More agents in {where} cannot run their work", ""
        else:
            shown = ", ".join(names[:_AGENTS_NAMED]) + (f" and {len(names) - _AGENTS_NAMED} more"
                                                         if len(names) > _AGENTS_NAMED else "")
            title = (f"Agent {names[0]} cannot run its work" if len(names) == 1
                     else f"{len(names)} agents in {where} cannot run their work")
            named = f"{shown} in {where} got no session from the team services. "
        findings.append(StatusFindingV1(
            finding_id=f"agent_without_session:{host}:{code}"[:200], severity=StatusSeverity.PROBLEM,
            title=title[:200], summary=f"{named}{_capitalized(why.format(where=where))} {_WITHOUT_SESSION_MEANS}"[:2000],
            action=action[:1000], source=place[:120], details=details))
    return findings


def _other_contract_finding(host: str, theirs: str, ours: str) -> StatusFindingV1:
    where = "the team lead's process" if host == LEAD_HOST_RUNTIME else f"process {host}"
    return StatusFindingV1(
        finding_id=f"host_other_contract:{host}"[:200], severity=StatusSeverity.PROBLEM,
        title=f"{_capitalized(where)} runs another release's message contract"[:200],
        summary=(f"{_capitalized(where)} runs another message contract than the rest of the team, so it takes no "
                 "new work and no agent is placed on it; requests about the tasks it holds still reach it while it "
                 "finishes or hands them back, and it then gives its agents up. If it is the team lead's, chats are "
                 "answered as unavailable.")[:2000],
        action=("Install the same release for every process of the instance and restart the whole team, so they "
                "all run one message contract.")[:1000],
        source=_capitalized(where)[:120],
        details=f"host: {host}; its contract: {theirs}; this gateway's contract: {ours}"[:2000])


def _capitalized(text: str) -> str:
    return text[:1].upper() + text[1:]


async def _supervised(name: str, start: Callable[[], Awaitable[None]], stop: asyncio.Event,
                      logger: logging.Logger, delays: tuple[float, float]) -> None:
    """Run ``start()`` until ``stop``. A failure, or an end before ``stop``, is logged and it starts again after a
    wait that doubles from ``delays[0]`` to ``delays[1]``, so one broken part of the view never stops the rest
    silently and for good (review F1)."""

    first, longest = delays
    delay = first
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        began = loop.time()
        error: Optional[Exception] = None
        try:
            await start()
        except Exception as failure:
            error = failure
        else:
            if stop.is_set():
                return
        if loop.time() - began >= longest:
            delay = first
        if error is not None:
            logger.error("A part of the gateway's team view failed; it starts again in %s seconds. "
                         "event=gateway_team_view_task_failed task=%s error_type=%s", delay, name,
                         type(error).__name__, exc_info=error)
        else:
            logger.error("A part of the gateway's team view ended before the gateway stopped; it starts again in "
                         "%s seconds. event=gateway_team_view_task_ended task=%s", delay, name)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        delay = min(delay * 2, longest)


async def _either(first: asyncio.Event, second: asyncio.Event) -> None:
    """Wait until either event is set (a readiness wait must also end when the gateway stops, review F2)."""

    waits = [asyncio.create_task(first.wait()), asyncio.create_task(second.wait())]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()
        await asyncio.gather(*waits, return_exceptions=True)


@dataclass(frozen=True)
class JoinRefusal:
    """The installation's final answer to the gateway's join (``assistant_gateway.message_host``): its outcome and
    what a person's command says about it."""

    outcome: str
    text: str


StatusObserver = Callable[[AgentStatus, Optional[datetime], str], None]


def record_status(item: TeamAgent, team_id: str, *, details: Optional[Mapping[str, Any]] = None,
                  exiting: bool = False) -> AgentStatus:
    """A member as the Team page and the manager client read it, from its record and its last answered details."""

    found = dict(details or {})
    findings = []
    for finding in found.get("findings", []):
        try:
            findings.append(StatusFindingV1.model_validate(finding))
        except ValueError:
            _LOGGER.warning("An agent's Status page finding could not be read. event=gateway_agent_finding_invalid "
                            "agent=%s", item.agent)
    return AgentStatus(
        self_id=item.agent, team_id=team_id, name=item.display_name or f"Agent_{item.agent}",
        named_agent_type=item.agent_type, agent_role="team_lead" if item.is_lead else "worker",
        is_team_lead=item.is_lead, is_paused=item.state == "paused", is_executing=item.state == "busy",
        is_busy=item.state == "busy", accepting_work=item.takes_work and not exiting,
        has_execution_capacity=found.get("has_capacity"), is_exiting=exiting,
        available_skills=list(found.get("skills", [])), status_findings=findings,
        last_seen_at=datetime.now(timezone.utc).isoformat(), status_type=MessageMetaType.RESPONSE,
    )


class GatewayTeamView:
    """The gateway's view of its team (module docstring)."""

    def __init__(self, team_id: str, *, on_status: StatusObserver, logger: Optional[logging.Logger] = None,
                 details_seconds: float = DETAILS_POLL_SECONDS,
                 restart_seconds: tuple[float, float] = RESTART_SECONDS) -> None:
        self.team_id = team_id
        self._on_status = on_status
        self._logger = logger or _LOGGER
        self._details_seconds = details_seconds
        self._restart_seconds = restart_seconds
        self._agents: Optional[TeamAgents] = None
        self._host: Any = None
        self._details: dict[str, dict[str, Any]] = {}
        self._before: Mapping[str, TeamAgent] = {}
        self.ready = asyncio.Event()
        # Set on every member change, when the records are read or stop being read, and on a refused join; only
        # the lead follower clears it (``TeamLeadFollower``).
        self.changed = asyncio.Event()
        # Why the gateway cannot join, while that lasts (``refuse``).
        self.refusal: Optional[JoinRefusal] = None

    @property
    def agents(self) -> Optional[TeamAgents]:
        return self._agents

    async def run(self, host: Any, stop: asyncio.Event) -> None:
        """Read the team through the gateway's joined host until ``stop``; returns as soon as ``stop`` is set.

        The reader and the details each run supervised (``_supervised``).
        """

        self._host = host
        self._agents = TeamAgents(host, self.team_id, logger=self._logger)
        self._agents.listen(self._changed)
        parts: dict[str, Callable[[], Awaitable[None]]] = {
            "gateway_team_agents": lambda: self._read(stop),
            "gateway_team_details": lambda: self._ask_details(stop),
        }
        tasks = [asyncio.create_task(_supervised(name, start, stop, self._logger, self._restart_seconds), name=name)
                 for name, start in parts.items()]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _read(self, stop: asyncio.Event) -> None:
        """Run the reader; the view is ready while the reader has caught up. Never waits for the catch-up alone:
        a reader that stops before it (a watch that keeps failing, a quick stop) ends this too (review F2)."""

        assert self._agents is not None
        reading = asyncio.create_task(self._agents.run(stop), name="gateway_team_agents_run")
        caught_up = asyncio.create_task(self._agents.ready.wait())
        try:
            await asyncio.wait({reading, caught_up}, return_when=asyncio.FIRST_COMPLETED)
            if self._agents.ready.is_set():
                self.refusal = None  # it joined, so a refusal from before no longer holds
                self.ready.set()
                self.changed.set()
            await reading
        finally:
            self.ready.clear()
            self.changed.set()
            for task in (reading, caught_up):
                task.cancel()
            await asyncio.gather(reading, caught_up, return_exceptions=True)

    def live_ids(self) -> frozenset[str]:
        return frozenset(self._agents.live()) if self._agents is not None else frozenset()

    def live(self) -> Mapping[str, TeamAgent]:
        """Every live member, on any contract, as of the reader's last change or check; none until the records
        are read."""

        return self._agents.live() if self._agents is not None and self.ready.is_set() else {}

    def departure_reason(self, agent_id: str) -> Optional[str]:
        return self._agents.departure_reason(agent_id) if self._agents is not None else None

    def refuse(self, outcome: str, text: str) -> None:
        """The installation refused the gateway's join for good: nobody can see the team (review F4)."""

        self.refusal = JoinRefusal(outcome, text)
        self.changed.set()

    def joining(self) -> None:
        """The gateway's host joins again (``GatewayMessageHost.start``): an earlier refusal no longer holds."""

        self.refusal = None
        self.changed.set()

    def host_findings(self) -> list[StatusFindingV1]:
        """The Status page's findings from the team's host records, as of now (``host_findings``)."""

        if self._agents is None or not self.ready.is_set():
            return []
        return host_findings(self._agents.host_components(), self._agents.other_contract_hosts(),
                             self._host.live.trust.store.contract_sha256)

    # -- following the records ------------------------------------------------------------

    def _changed(self, changed: frozenset[str]) -> None:
        assert self._agents is not None
        if not self._agents.ready.is_set():
            # The reader stopped: it clears its readiness, then publishes an empty view. Nobody may read that
            # view as "no lead" in between (``_read`` clears the view's readiness only once it resumes).
            self.ready.clear()
        before, after = self._before, self._agents.live()
        self._before = after
        for agent_id in sorted(changed):
            now, earlier = after.get(agent_id), before.get(agent_id)
            if now is not None:
                status = record_status(now, self.team_id, details=self._details.get(agent_id))
            elif earlier is not None:
                reason = self._agents.departure_reason(agent_id)
                if reason == READER_STOPPED:
                    # Nobody left: the reader stopped (the gateway stops, or the reader starts again).
                    continue
                self._logger.info("A team member left the team's records. event=gateway_team_member_left agent=%s "
                                  "lead=%s reason=%s", agent_id, earlier.is_lead, reason)
                self._details.pop(agent_id, None)
                status = record_status(earlier, self.team_id, exiting=True)
            else:
                continue
            self._on_status(status, None, SOURCE)
        self.changed.set()

    async def _ask_details(self, stop: asyncio.Event) -> None:
        """Each live agent's skills, findings and capacity, every ``details_seconds`` (module docstring)."""

        while not stop.is_set():
            if not self.ready.is_set():
                await _either(self.ready, stop)
                continue
            assert self._agents is not None
            live = sorted(self._agents.live())
            answers = await asyncio.gather(*(self._ask(agent_id) for agent_id in live))
            for agent_id, answer in zip(live, answers):
                current = self._agents.agent(agent_id)
                if answer is None or current is None:
                    continue
                self._details[agent_id] = answer
                self._on_status(record_status(current, self.team_id, details=answer), None, SOURCE)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._details_seconds)
            except asyncio.TimeoutError:
                pass

    async def _ask(self, agent_id: str) -> Optional[dict[str, Any]]:
        """One agent's ``agent.status`` answer, or None. Any failure stays with this agent (review F1): a reply that
        fails verification, a connection error or a malformed body is logged, and the other agents' answers count."""

        host = self._host
        audience = {"installation": host.identity.installation, "team": self.team_id, "target": "agent",
                    "id": agent_id}
        try:
            reply = await host.live.request("agent.status", {"level": "basic"}, audience, team=self.team_id,
                                            timeout=DETAILS_TIMEOUT_SECONDS)
            body = dict(reply.raw["body"])
        except (ServiceUnavailable, RequestRefused) as error:
            self._logger.info("An agent did not give its status. event=gateway_agent_status_unanswered agent=%s "
                              "reason=%s", agent_id, error)
            return None
        except Exception as error:
            self._logger.warning("An agent's status answer could not be used. event=gateway_agent_status_failed "
                                 "agent=%s error_type=%s error=%s", agent_id, type(error).__name__, error)
            return None
        return body if body.get("outcome") == "ok" else None


class LeadRoute(Protocol):
    """The manager client's side of the route to the team lead (``GatewayManagerClient``)."""

    @property
    def manager_id(self) -> Optional[str]: ...

    @property
    def pending_response_ids(self) -> tuple[str, ...]: ...

    async def set_lead(self, lead_id: str, *, source: str) -> None: ...

    async def mark_manager_unavailable(self, *, expected_manager_id: Optional[str] = None, reason: str,
                                       source: str = ..., keep_waiting: bool = False) -> None: ...


class WaitingReplies(Protocol):
    """Failing people's waiting replies on their own (``GatewayManagerTransitionApplication.fail_waiting``)."""

    async def fail_waiting(self, correlation_ids: Iterable[str]) -> None: ...


class TeamLeadFollower:
    """The one path that sets the gateway's route to the team lead from the team's records.

    It compares the route with the records instead of following events, so a change it did
    not hear, a records view that is read late, a clock that steps back or a failed attempt
    cannot leave a dead or wrong route: the next check repairs it (review F3, F12). It is the
    route's only writer while the gateway runs.

    - **Which lead.** Among the live leads whose process runs this gateway's contract, the
      current route while it is one of them (a second lead-role session never takes the
      chats over, F14); otherwise the one whose record changed last (a crashed run's lead
      has not changed since the crash), by id on a tie (F13). Several leads are logged once
      per set (``event=gateway_multiple_leads``).
    - **At start** (``start``) it waits up to ``LEAD_START_SECONDS`` for a lead to follow, or
      a refused join, and then decides once. A lead is set directly, also in place of a saved
      route from the previous run: nothing was sent to that one yet. No lead in records that
      were read clears the route with one unavailable transition. Records not read yet keep
      the saved route. Waiting for a lead, and not only for the records, keeps a normal
      team start, where the gateway reads the records before the lead has written its own,
      from marking the lead unavailable and available again (F5).
    - **Afterwards** (``run``) it checks on every change and every ``LEAD_CHECK_SECONDS``. A
      route that differs from the records goes through the usual transitions: the current
      lead unavailable (with ``expected_manager_id``), then the chosen one available, so
      replies waiting on a dead route are failed. While the records are not read it keeps
      the route: it cannot judge.
    - **A refused join**, whenever it comes, means nobody can see a lead: the route is cleared
      (F4). It no longer holds once the host joins again.
    - **A lead whose process runs another contract** takes no new chats, but finishes the
      tasks it holds: the route is cleared with ``keep_waiting`` (F16). The replies people
      were waiting for then are remembered, and those still waiting when that lead leaves
      the records are failed then, with no second notice (critique C01).
    """

    def __init__(self, view: GatewayTeamView, route: LeadRoute, waiting: WaitingReplies, *,
                 logger: Optional[logging.Logger] = None, check_seconds: float = LEAD_CHECK_SECONDS) -> None:
        self._view = view
        self._route = route
        self._waiting = waiting
        self._logger = logger or _LOGGER
        self._check_seconds = check_seconds
        # The lead on another contract whose people's waiting replies were kept, and those replies.
        self._kept: Optional[tuple[str, frozenset[str]]] = None
        self._leads_logged: frozenset[str] = frozenset()

    @property
    def view(self) -> GatewayTeamView:
        return self._view

    async def start(self, limit: Optional[float] = None) -> None:
        """Wait up to ``limit`` (``LEAD_START_SECONDS``) for a lead to follow or a refused join, then decide once
        (class docstring)."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + (LEAD_START_SECONDS if limit is None else limit)
        while not self._start_settled():
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._view.changed.wait(), timeout=remaining)
            self._view.changed.clear()
        if not self._view.ready.is_set() and self._view.refusal is None:
            self._logger.warning("The team's agent records were not read in time; the gateway keeps its saved route to "
                                 "the team lead and checks it against the records as soon as they are read. "
                                 "event=gateway_team_view_not_ready manager_id=%s", self._route.manager_id)
        await self._check(at_start=True)

    async def run(self, stop: asyncio.Event) -> None:
        """Check the route against the records on every change and every ``check_seconds`` until ``stop``,
        supervised like the view's own parts."""

        await _supervised("gateway_team_lead", lambda: self._follow_changes(stop), stop, self._logger, RESTART_SECONDS)

    async def _follow_changes(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._view.changed.wait(), timeout=self._check_seconds)
            self._view.changed.clear()
            if not stop.is_set():
                await self._check(at_start=False)

    def _start_settled(self) -> bool:
        if self._view.refusal is not None:
            return True
        return self._view.ready.is_set() and self._choose(self._route.manager_id, self._view.live()) is not None

    async def _check(self, *, at_start: bool) -> None:
        """One comparison of the route with the records; a failure is logged and the next check repairs it."""

        try:
            await self._end_kept_waits()
            await self._follow(at_start=at_start)
        except Exception:
            self._logger.exception("The route to the team lead could not be checked against the records; it is "
                                   "checked again. event=gateway_lead_follow_failed manager_id=%s",
                                   self._route.manager_id)

    async def _follow(self, *, at_start: bool) -> None:
        current = self._route.manager_id
        refusal = self._view.refusal
        if refusal is not None:
            if current is not None:
                await self._route.mark_manager_unavailable(
                    expected_manager_id=current, source=SOURCE,
                    reason=f"the installation refused the gateway's join ({refusal.outcome}), so no lead can be seen")
            return
        if not self._view.ready.is_set():
            return
        live = self._view.live()
        chosen = self._choose(current, live)
        if chosen == current:
            return
        if current is not None and (chosen is None or not at_start):
            await self._leave(current, live.get(current))
        if chosen is not None:
            await self._route.set_lead(chosen, source=SOURCE)

    async def _leave(self, current: str, record: Optional[TeamAgent]) -> None:
        """Mark the current lead unavailable: gone from the records, or live on another contract."""

        if record is not None and record.other_contract is not None:
            self._kept = (current, frozenset(self._route.pending_response_ids))
            await self._route.mark_manager_unavailable(
                expected_manager_id=current, source=SOURCE, keep_waiting=True,
                reason="the team lead's process runs another message contract; it finishes the tasks it holds "
                       "but takes no new chats")
            return
        why = self._view.departure_reason(current) if record is None else "it is no longer the team lead"
        await self._route.mark_manager_unavailable(
            expected_manager_id=current, source=SOURCE,
            reason=f"the team lead is not live in the team's records ({why or 'not seen since the gateway started'})")

    async def _end_kept_waits(self) -> None:
        """Fail the kept waiting replies once their lead left the records (critique C01)."""

        if self._kept is None or not self._view.ready.is_set():
            return
        lead, kept = self._kept
        if lead in self._view.live():
            return
        self._kept = None
        waiting = set(self._route.pending_response_ids)
        ended = sorted(kept & waiting)
        self._logger.info("A team lead that ran another contract left; the replies still waiting for it are failed. "
                          "event=gateway_kept_waits_ended manager_id=%s waiting=%d", lead, len(ended))
        await self._waiting.fail_waiting(ended)

    def _choose(self, current: Optional[str], live: Mapping[str, TeamAgent]) -> Optional[str]:
        """The lead to route chats to (class docstring, "Which lead"), or None."""

        leads = sorted((item for item in live.values() if item.is_lead and item.other_contract is None),
                       key=lambda item: (item.since, item.agent))
        ids = frozenset(item.agent for item in leads)
        chosen = current if current in ids else (leads[-1].agent if leads else None)
        if len(leads) > 1 and ids != self._leads_logged:
            self._logger.warning("Several agents hold the team lead's role; the gateway keeps routing chats to one of "
                                 "them. event=gateway_multiple_leads leads=%s kept=%s", ",".join(sorted(ids)), chosen)
        self._leads_logged = ids if len(leads) > 1 else frozenset()
        return chosen


"""The team as the gateway sees it: who is live and who leads, from the team's signed agent records.

Until step 7c the gateway learned this from the presence heartbeat, asked for the team
lead on the operational subject at start ("discovery"), and polled the lead's consumer and
its heartbeat's age every 5 seconds. Now the gateway's own host reads the team's live
agents (``custom_transport.protocol1.team_agents``), and this module turns that view into
what the gateway already acts on:

- **The team lead.** Each change to the lead's record, its arrival, or its leaving reaches
  the manager client's ``apply_manager_status`` in order, through one queue, so the
  existing "available" and "unavailable" transitions run as before. The lead saying it
  leaves makes it unavailable at once. A lead whose host stopped reporting (a crash) is
  unavailable at most 35 seconds after the host's last liveness write: the record's 30
  seconds plus the reader's 5-second check, about the 30 seconds after which the presence
  heartbeat counted the lead gone.
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

Everything here runs on the gateway's own loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

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
from custom_transport.protocol1.team_agents import HostComponent, TeamAgent, TeamAgents

# The old heartbeat's period: how often the gateway asks each live agent for its details.
DETAILS_POLL_SECONDS = 20.0
DETAILS_TIMEOUT_SECONDS = 5.0
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


StatusObserver = Callable[[AgentStatus, Optional[datetime], str], None]
LeadObserver = Callable[[AgentStatus], Awaitable[None]]


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

    def __init__(self, team_id: str, *, on_status: StatusObserver, on_lead: LeadObserver,
                 logger: Optional[logging.Logger] = None, details_seconds: float = DETAILS_POLL_SECONDS) -> None:
        self.team_id = team_id
        self._on_status = on_status
        self._on_lead = on_lead
        self._logger = logger or _LOGGER
        self._details_seconds = details_seconds
        self._agents: Optional[TeamAgents] = None
        self._host: Any = None
        self._details: dict[str, dict[str, Any]] = {}
        self._before: Mapping[str, TeamAgent] = {}
        self._leads: asyncio.Queue[AgentStatus] = asyncio.Queue()
        self.ready = asyncio.Event()

    @property
    def agents(self) -> Optional[TeamAgents]:
        return self._agents

    async def run(self, host: Any, stop: asyncio.Event) -> None:
        """Read the team through the gateway's joined host until ``stop``."""

        self._host = host
        self._agents = TeamAgents(host, self.team_id, logger=self._logger)
        self._agents.listen(self._changed)
        tasks = [asyncio.create_task(self._agents.run(stop), name="gateway_team_agents"),
                 asyncio.create_task(self._apply_leads(stop), name="gateway_team_leads"),
                 asyncio.create_task(self._ask_details(stop), name="gateway_team_details")]
        try:
            await self._agents.ready.wait()
            self.ready.set()
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_ready(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def lead(self) -> Optional[TeamAgent]:
        """The live team lead whose host runs this gateway's contract; one on another contract takes no chats."""

        lead = self._agents.lead() if self._agents is not None and self.ready.is_set() else None
        return lead if lead is not None and lead.other_contract is None else None

    def live_ids(self) -> frozenset[str]:
        return frozenset(self._agents.live()) if self._agents is not None else frozenset()

    def host_findings(self) -> list[StatusFindingV1]:
        """The Status page's findings from the team's host records, as of now (``host_findings``)."""

        if self._agents is None or not self.ready.is_set():
            return []
        return host_findings(self._agents.host_components(), self._agents.other_contract_hosts(),
                             self._host.live.trust.store.contract_sha256)

    # -- following the records ------------------------------------------------------------

    def _changed(self, changed: frozenset[str]) -> None:
        assert self._agents is not None
        before, after = self._before, self._agents.live()
        self._before = after
        for agent_id in sorted(changed):
            now, earlier = after.get(agent_id), before.get(agent_id)
            if now is not None:
                status = record_status(now, self.team_id, details=self._details.get(agent_id))
            elif earlier is not None:
                self._details.pop(agent_id, None)
                status = record_status(earlier, self.team_id, exiting=True)
            else:
                continue
            self._on_status(status, None, SOURCE)
            if now is not None and now.is_lead and now.other_contract is not None:
                # It takes no chats: for the manager client it is leaving (module docstring).
                self._leads.put_nowait(record_status(now, self.team_id, details=self._details.get(agent_id),
                                                     exiting=True))
            elif (now is not None and now.is_lead) or (now is None and earlier is not None and earlier.is_lead):
                self._leads.put_nowait(status)

    async def _apply_leads(self, stop: asyncio.Event) -> None:
        """The lead's changes, in order, to the manager client (module docstring)."""

        while not stop.is_set():
            status = await self._leads.get()
            try:
                await self._on_lead(status)
            except Exception:
                self._logger.exception("The team lead's change could not be applied. event=gateway_lead_apply_failed "
                                       "agent=%s", status.self_id)

    async def _ask_details(self, stop: asyncio.Event) -> None:
        """Each live agent's skills, findings and capacity, every ``details_seconds`` (module docstring)."""

        await self.ready.wait()
        while not stop.is_set():
            assert self._agents is not None
            live = dict(self._agents.live())
            answers = await asyncio.gather(*(self._ask(agent_id) for agent_id in sorted(live)))
            for agent_id, answer in zip(sorted(live), answers):
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
        host = self._host
        audience = {"installation": host.identity.installation, "team": self.team_id, "target": "agent",
                    "id": agent_id}
        try:
            reply = await host.live.request("agent.status", {"level": "basic"}, audience, team=self.team_id,
                                            timeout=DETAILS_TIMEOUT_SECONDS)
        except (ServiceUnavailable, RequestRefused) as error:
            self._logger.info("An agent did not give its status. event=gateway_agent_status_unanswered agent=%s "
                              "reason=%s", agent_id, error)
            return None
        body = dict(reply.raw["body"])
        return body if body.get("outcome") == "ok" else None

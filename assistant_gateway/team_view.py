"""The team as the gateway sees it: who is live and who leads, from the team's signed agent records.

Until step 7c the gateway learned this from the presence heartbeat, asked for the team
lead on the operational subject at start ("discovery"), and polled the lead's consumer and
its heartbeat's age every 5 seconds. Now the gateway's own host reads the team's live
agents (``custom_transport.protocol1.team_agents``), and this module turns that view into
what the gateway already acts on:

- **The team lead.** Each change to the lead's record, its arrival, or its leaving reaches
  the manager client's ``apply_manager_status`` in order, through one queue, so the
  existing "available" and "unavailable" transitions run as before. The lead leaving, or
  its host no longer reporting, makes it unavailable within the reader's 5-second check.
- **The team's members.** Each live agent's state goes to the Team page and the Status
  page's records (``GatewayTeamPresence``) as it changes. What the records do not carry
  (skills, Status page findings, whether a busy agent could start more) is asked with
  ``agent.status`` from each live agent every 20 seconds, the old heartbeat's period, and
  merged in.
- **Held guidance** is woken when the lead's record changes, as the lead's heartbeat did.

Everything here runs on the gateway's own loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.status_v1 import StatusFindingV1
from custom_messages.enums import MessageMetaType
from custom_transport.protocol1.live import RequestRefused, ServiceUnavailable
from custom_transport.protocol1.team_agents import TeamAgent, TeamAgents

# The old heartbeat's period: how often the gateway asks each live agent for its details.
DETAILS_POLL_SECONDS = 20.0
DETAILS_TIMEOUT_SECONDS = 5.0
SOURCE = "agent_records"
_LOGGER = logging.getLogger(__name__)

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
        is_busy=item.state == "busy", accepting_work=item.accepting_work and not exiting,
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
        return self._agents.lead() if self._agents is not None and self.ready.is_set() else None

    def live_ids(self) -> frozenset[str]:
        return frozenset(self._agents.live()) if self._agents is not None else frozenset()

    # -- following the records ------------------------------------------------------------

    def _changed(self, changed: frozenset[str], exited: frozenset[str]) -> None:
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
            if (now is not None and now.is_lead) or (now is None and earlier is not None and earlier.is_lead):
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

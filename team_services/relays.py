"""People's commands the team services pass on to the host running an agent (``ctl.agent.<agent>``).

``tasks.list``, ``tasks.list_finished``, ``tasks.remove``, ``tasks.purge`` and ``debug.dump``
need the agent itself: its queue lives in its process, and the context dump setting is the
team lead's (README "Relays to an agent's host"). The gateway sends them here; the team
services find the agent's host in its placement (``UA1_PLACEMENT``) and pass the same body,
with the gateway's command token unchanged, to the agent's address, then answer with the
agent's reply. The agent decides for the person the token names
(``custom_agents.relayed_task_controls``); the team services never sign tokens.

``debug.dump`` without an agent goes to the team lead (the placement of the lead's type).
A missing agent and an unanswered or refused command get distinct plain runtime_error replies.

A placement is used only when it verifies (``StateRecords.read_report``: signed by a team
services host, which the registry's signer roles check, and stored under its own key), as
the session service reads them (``team_services.sessions``): every process shares one NATS
login and can write ``UA1_PLACEMENT``. One that does not verify counts as absent and is
logged (``event=team_services_record_refused``), so a forged or stale placement never sends
a person's command to another agent, and one bad key never hides the lead's placement.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from custom_messages.wire.frame import VerifiedFrame
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.live import NO_RESPONDERS, RequestRefused, ServiceUnavailable

RELAYED_KINDS = ("tasks.list", "tasks.list_finished", "tasks.remove", "tasks.purge", "debug.dump")
_PLACEMENT_BUCKET = "UA1_PLACEMENT"
AGENT_UNAVAILABLE_TEXT = "That agent is not running now. Refresh, and try again when it is."
RELAY_TIMEOUT_SECONDS = 10.0
AGENT_SLOW_TEXT = "That agent did not answer in time. The command may still finish. Refresh to check its result."
AGENT_REFUSED_TEXT = "That agent could not handle this request. Refresh and try again."


class AgentUnavailable(RuntimeError):
    """The agent runs on no host, or its host did not answer."""


class Relays:
    def __init__(self, host: MessageHost, team: str, lead_agent_type: str, logger: logging.Logger) -> None:
        # The team services' host: it relays with its live host and reads placements with its records once joined.
        self._host = host
        self._team = team
        self._lead_agent_type = lead_agent_type
        self._logger = logger

    def handlers(self) -> dict[str, Callable[[VerifiedFrame], Any]]:
        return {kind: self.handle for kind in RELAYED_KINDS}

    def family(self) -> Any:
        from team_services.service import OperationFamily

        return OperationFamily(self.handlers(), side_by_side=frozenset(RELAYED_KINDS))

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        kind = verified.frame.kind
        body = verified.raw["body"]
        try:
            agent = str(body.get("agent") or await self._lead_agent())
            return await self._relay(verified, agent)
        except AgentUnavailable as unavailable:
            self._logger.warning("A relayed command found no agent to answer it. event=team_relay_unavailable kind=%s "
                                 "agent=%s reason=%s", kind, body.get("agent"), unavailable)
            return {"outcome": "runtime_error", "message": str(unavailable)}

    async def _relay(self, verified: VerifiedFrame, agent: str) -> dict[str, Any]:
        if await self._placement(f"{self._team}.{agent}") is None:
            raise AgentUnavailable(AGENT_UNAVAILABLE_TEXT)
        live = self._host.live
        assert live is not None, "the team services serve only after joining"
        audience = {"installation": live.identity.installation, "team": self._team, "target": "agent", "id": agent}
        try:
            reply = await live.request(verified.frame.kind, verified.raw["body"], audience, team=self._team,
                                       authority=verified.raw["authority"], timeout=RELAY_TIMEOUT_SECONDS)
        except ServiceUnavailable as error:
            text = AGENT_UNAVAILABLE_TEXT if error.reason == NO_RESPONDERS else AGENT_SLOW_TEXT
            raise AgentUnavailable(text) from error
        except (RequestRefused, FrameRefused) as error:
            self._logger.warning("An agent refused a relayed command. event=team_relay_refused kind=%s agent=%s code=%s",
                                 verified.frame.kind, agent, error.code)
            raise AgentUnavailable(AGENT_REFUSED_TEXT) from error
        return dict(reply.raw["body"])

    async def _placement(self, key: str) -> Optional[Any]:
        """The placement under ``key`` when it verifies; None when absent or refused (module docstring)."""

        records = self._host.records
        assert records is not None, "the team services serve only after joining"
        try:
            stored = await records.read_report("placement", key, now=self._host.clock())
        except FrameRefused as refused:
            self._logger.warning("A stored record does not verify and counts as absent. "
                                 "event=team_services_record_refused record=placement key=%s code=%s at=%s", key,
                                 refused.code, refused.at)
            return None
        return None if stored is None else stored.record.body

    async def _lead_agent(self) -> str:
        """The newest live lead session, on a lead host, matching its current placement."""
        records, live = self._host.records, self._host.live
        assert records is not None and live is not None
        now = self._host.clock()
        candidates = []
        prefix = f"session.{live.identity.installation}.{self._team}."
        for key in await records.keys("agent_session", prefix):
            try:
                await records.take_trust_record("agent_session", key, now=now)
                session = live.trust.store.agent_session(live.identity.installation, self._team, key.rsplit(".", 1)[-1])
                if session is None or session.agent_role != "lead" or session.agent_type != self._lead_agent_type \
                        or session.ends_at <= now:
                    continue
                placement = await self._placement(f"{self._team}.{session.agent}")
                if placement is None or placement.host != session.runtime or placement.placed_at != format_timestamp(session.started_at):
                    continue
                host = await records.read_report("host", session.runtime, now=now)
                if host is None or host.record.other_contract or host.record.body.state not in {"accepting", "draining"}:
                    continue
                candidates.append((host.record.body.state == "accepting", session.started_at, session.agent))
            except FrameRefused as refused:
                self._logger.warning("A stored lead record does not verify. event=team_services_record_refused "
                                     "record=agent_session key=%s code=%s at=%s", key, refused.code, refused.at)
            except Exception:
                self._logger.exception("A lead session could not be read; looking at the others. "
                                       "event=team_relay_placement_unreadable key=%s", key)
        if candidates:
            return str(max(candidates)[2])
        raise AgentUnavailable(AGENT_UNAVAILABLE_TEXT)

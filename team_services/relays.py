"""People's commands the team services pass on to the host running an agent (``ctl.agent.<agent>``).

``tasks.list``, ``tasks.list_finished``, ``tasks.remove``, ``tasks.purge`` and ``debug.dump``
need the agent itself: its queue lives in its process, and the context dump setting is the
team lead's (README "Relays to an agent's host"). The gateway sends them here; the team
services find the agent's host in its placement (``UA1_PLACEMENT``) and pass the same body,
with the gateway's command token unchanged, to the agent's address, then answer with the
agent's reply. The agent decides for the person the token names
(``custom_agents.relayed_task_controls``); the team services never sign tokens.

``debug.dump`` without an agent goes to the team lead (the placement of the lead's type).
An agent that runs nowhere, or does not answer, is answered ``runtime_error``
(``debug.dump`` has no such outcome, so it is refused as ``handler_failed``).

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
from custom_messages.wire.refusals import FrameRefused
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.live import RequestRefused, ServiceUnavailable

RELAYED_KINDS = ("tasks.list", "tasks.list_finished", "tasks.remove", "tasks.purge", "debug.dump")
_PLACEMENT_BUCKET = "UA1_PLACEMENT"
AGENT_UNAVAILABLE_TEXT = "That agent is not running now. Refresh, and try again when it is."
RELAY_TIMEOUT_SECONDS = 15.0


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

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        kind = verified.frame.kind
        body = verified.raw["body"]
        try:
            agent = str(body.get("agent") or await self._lead_agent())
            return await self._relay(verified, agent)
        except AgentUnavailable as unavailable:
            self._logger.warning("A relayed command found no agent to answer it. event=team_relay_unavailable kind=%s "
                                 "agent=%s reason=%s", kind, body.get("agent"), unavailable)
            if kind == "debug.dump":
                raise
            return {"outcome": "runtime_error", "message": AGENT_UNAVAILABLE_TEXT}

    async def _relay(self, verified: VerifiedFrame, agent: str) -> dict[str, Any]:
        if await self._placement(f"{self._team}.{agent}") is None:
            raise AgentUnavailable(f"{agent} has no placement")
        live = self._host.live
        assert live is not None, "the team services serve only after joining"
        audience = {"installation": live.identity.installation, "team": self._team, "target": "agent", "id": agent}
        try:
            reply = await live.request(verified.frame.kind, verified.raw["body"], audience, team=self._team,
                                       authority=verified.raw["authority"], timeout=RELAY_TIMEOUT_SECONDS)
        except (ServiceUnavailable, RequestRefused) as error:
            raise AgentUnavailable(str(error)) from error
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
        """The team lead's agent: the placement of the lead's type on a lead host."""

        try:
            keys = await (await self._host.nc.jetstream().key_value(_PLACEMENT_BUCKET)).keys()
        except Exception as error:  # an empty bucket raises too
            raise AgentUnavailable("no placements") from error
        for key in keys:
            if not key.startswith(f"{self._team}."):
                continue
            try:
                placed = await self._placement(key)
            except Exception:
                self._logger.exception("A placement could not be read; looking at the others. "
                                       "event=team_relay_placement_unreadable key=%s", key)
                continue
            if placed is not None and placed.agent_type == self._lead_agent_type:
                return str(placed.agent)
        raise AgentUnavailable("the team lead has no placement")

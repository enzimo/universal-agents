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
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from custom_messages.wire.frame import VerifiedFrame
from custom_transport.protocol1.live import LiveHost, RequestRefused, ServiceUnavailable

RELAYED_KINDS = ("tasks.list", "tasks.list_finished", "tasks.remove", "tasks.purge", "debug.dump")
_PLACEMENT_BUCKET = "UA1_PLACEMENT"
AGENT_UNAVAILABLE_TEXT = "That agent is not running now. Refresh, and try again when it is."
RELAY_TIMEOUT_SECONDS = 15.0


class AgentUnavailable(RuntimeError):
    """The agent runs on no host, or its host did not answer."""


class Relays:
    def __init__(self, live: Callable[[], LiveHost], js: Any, team: str, lead_agent_type: str,
                 logger: logging.Logger) -> None:
        self._live = live
        self._js = js
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
        if await self._placement(agent) is None:
            raise AgentUnavailable(f"{agent} has no placement")
        live = self._live()
        audience = {"installation": live.identity.installation, "team": self._team, "target": "agent", "id": agent}
        try:
            reply = await live.request(verified.frame.kind, verified.raw["body"], audience, team=self._team,
                                       authority=verified.raw["authority"], timeout=RELAY_TIMEOUT_SECONDS)
        except (ServiceUnavailable, RequestRefused) as error:
            raise AgentUnavailable(str(error)) from error
        return dict(reply.raw["body"])

    async def _placement(self, agent: str) -> Optional[dict[str, Any]]:
        bucket = await self._js.key_value(_PLACEMENT_BUCKET)
        try:
            entry = await bucket.get(f"{self._team}.{agent}")
        except Exception:  # nats raises KeyNotFoundError for an absent key
            return None
        return json.loads(entry.value)["body"] if entry is not None and entry.value else None

    async def _lead_agent(self) -> str:
        """The team lead's agent: the placement of the lead's type on a lead host."""

        bucket = await self._js.key_value(_PLACEMENT_BUCKET)
        try:
            keys = await bucket.keys()
        except Exception as error:  # an empty bucket raises too
            raise AgentUnavailable("no placements") from error
        for key in keys:
            if not key.startswith(f"{self._team}."):
                continue
            entry = await bucket.get(key)
            body = json.loads(entry.value)["body"]
            if body.get("agent_type") == self._lead_agent_type:
                return str(body["agent"])
        raise AgentUnavailable("the team lead has no placement")

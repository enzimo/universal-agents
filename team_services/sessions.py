"""Placing agents and signing their sessions (README "Agent sessions"; plan 13B.5).

A host signs frames for an agent only while a session places that agent on it. The
team services decide:

- ``sessions.open``: the host's certificate allows the agent's type and has room under
  its session limit; the agent is not placed on another host (agents stay on their host
  and move only by a handover, decision 51), else ``already_placed`` names that host. The
  placement is written with compare-and-set and the next sequence, and the session, with
  the placement's revision, is signed into ``UA1_CERTS`` for every receiver to read. Only
  the team lead's type (``TeamArchitect``) on a lead host gets the ``lead`` role.
- ``sessions.renew``: a session that places the agent on the asking host, re-signed for
  another 7 days. Only one that has not ended and whose placement revision is the
  agent's current placement: a session closed earlier must not come back because its
  agent was placed on the same host again (``not_allowed``; the host opens a new one).
- ``sessions.close``: the session's actual end; the placement is removed when the agent
  exited or failed, and kept when it is handed over (the new host's open replaces it).

Every process shares one NATS login in this phase, so any of them can write
``UA1_PLACEMENT`` and ``UA1_CERTS``. The team services use a placement or session they
read there only when it verifies: signed by a team services host (the registry's signer
roles, which the reader checks) and stored under its own key. One that does not verify
counts as absent and is logged with its key and the refusal
(``event=team_services_record_refused``); a renewal or close re-signs the session the
team services hold, never what another process wrote.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from nats.js.errors import KeyNotFoundError, KeyWrongLastSequenceError, NoKeysError, NotFoundError

from custom_messages.contracts.runtime_v1 import MANAGER_AGENT_TYPE
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import AgentSession, VerifiedRecord
from custom_transport.protocol1.state_records import StateRecords
from custom_transport.protocol1.trust_state import PersistentTrustStore

SESSION_LIFETIME = timedelta(days=7)
CERTIFICATES_BUCKET, PLACEMENT_BUCKET = "UA1_CERTS", "UA1_PLACEMENT"
LEAD_ROLE, WORKER_ROLE = "lead", "worker"
_LOGGER = logging.getLogger(__name__)


def placement_key(team: str, agent: str) -> str:
    return f"{team}.{agent}"


def session_key(installation: str, team: str, session: str) -> str:
    return f"session.{installation}.{team}.{session}"


@dataclass
class SessionService:
    js: Any
    records: StateRecords
    trust: PersistentTrustStore
    installation: str
    clock: Callable[[], datetime]
    logger: logging.Logger = _LOGGER

    async def _entry(self, bucket: str, key: str) -> Optional[Any]:
        try:
            entry = await (await self.js.key_value(bucket)).get(key)
        except KeyNotFoundError:
            return None
        return entry if entry.value else None

    def _refused(self, record: str, key: str, refused: FrameRefused) -> None:
        self.logger.warning("A stored record does not verify and counts as absent. event=team_services_record_refused "
                            "record=%s key=%s code=%s at=%s", record, key, refused.code, refused.at)

    async def _placement(self, key: str) -> tuple[int, Optional[VerifiedRecord]]:
        """The key's revision (0 when absent) and its placement, ``None`` when absent or it does not verify.

        The revision of a record that does not verify still lets the next placement
        replace it with compare-and-set.
        """

        try:
            stored = await self.records.read_report("placement", key, now=self.clock())
        except FrameRefused as refused:
            self._refused("placement", key, refused)
            entry = await self._entry(PLACEMENT_BUCKET, key)
            return (int(entry.revision) if entry is not None else 0), None
        return (0, None) if stored is None else (stored.revision, stored.record)

    async def _host_placements(self, team: str, runtime: str) -> list[str]:
        """The agents of ``team`` placed on ``runtime``."""

        bucket = await self.js.key_value(PLACEMENT_BUCKET)
        try:
            keys = [key for key in await bucket.keys() if key.startswith(f"{team}.")]
        except (NoKeysError, NotFoundError):
            return []
        agents = []
        for key in keys:
            _revision, placement = await self._placement(key)
            if placement is not None and placement.body.host == runtime:
                agents.append(key.split(".", 1)[1])
        return agents

    async def open(self, verified: VerifiedFrame) -> dict[str, Any]:
        certificate = verified.certificate
        assert certificate is not None  # hosts send it with their certificate
        team, body = verified.frame.issuer.team, verified.raw["body"]
        agent, agent_type = str(body["agent"]), str(body["agent_type"])
        if agent_type not in certificate.agent_types:
            return {"outcome": "not_allowed", "message": f"This host's certificate does not allow agent type {agent_type}."}
        key = placement_key(team, agent)
        current, previous = await self._placement(key)
        if previous is not None and previous.body.host != certificate.runtime:
            return {"outcome": "already_placed", "host": previous.body.host,
                    "message": f"{agent} runs on another host; it moves only by a handover."}
        if previous is None and len(await self._host_placements(team, certificate.runtime)) >= certificate.session_limit:
            return {"outcome": "limit_reached", "message": "This host already runs as many agents as its certificate allows."}
        now = self.clock()
        placement = {"team": team, "agent": agent, "agent_type": agent_type, "host": certificate.runtime,
                     "host_epoch": certificate.membership_epoch, "definition_sha256": str(body["definition_sha256"]),
                     "placed_at": format_timestamp(now), "reason": "initial"}
        if previous is not None:
            sequence = previous.document.sequence + 1
        else:
            # Over a record that did not verify, at least the time in milliseconds: a reader
            # still holding the placement it replaced must not take this one for an older copy.
            sequence = max(1, int(now.timestamp() * 1000)) if current else 1
        try:
            revision = await self.records.put("placement", key, self.records.signed(
                "placement", key, placement, sequence=sequence, issued_at=now), last_revision=current)
        except KeyWrongLastSequenceError:
            # Another open for this agent won; the asking host may ask again and learn where it went.
            return {"outcome": "runtime_error", "message": "The placement changed at the same time; ask again."}
        role = LEAD_ROLE if agent_type == MANAGER_AGENT_TYPE and "lead_host" in certificate.roles else WORKER_ROLE
        session = f"s-{agent}-{revision}"
        record = self.records.signed("agent_session", session_key(self.installation, team, session), {
            "session": session, "installation": self.installation, "team": team, "agent": agent,
            "agent_type": agent_type, "agent_role": role, "runtime": certificate.runtime, "placement_revision": revision,
            "started_at": format_timestamp(now), "ends_at": format_timestamp(now + SESSION_LIFETIME)},
            sequence=1, issued_at=now)
        await self._store_session(team, session, record)
        self.logger.info("Placed an agent and signed its session. event=agent_session_opened team=%s agent=%s host=%s "
                         "session=%s role=%s", team, agent, certificate.runtime, session, role)
        return {"outcome": "ok", "session": record}

    async def _store_session(self, team: str, session: str, record: dict[str, Any]) -> None:
        await self.records.put("agent_session", session_key(self.installation, team, session), record)
        self.trust.add(json.dumps(record).encode(), now=self.clock())

    async def _session(self, team: str, session: str) -> Optional[AgentSession]:
        """The session as the team services' trust store holds it, after taking the bucket's record when it verifies.

        A newer record in ``UA1_CERTS`` that verifies is taken; one that does not is
        logged, and the one held stays (none after a restart: the session counts as absent).
        """

        key = session_key(self.installation, team, session)
        try:
            await self.records.take_trust_record("agent_session", key, now=self.clock())
        except FrameRefused as refused:
            self._refused("agent_session", key, refused)
        return self.trust.store.agent_session(self.installation, team, session)

    async def _own_session(self, verified: VerifiedFrame) -> tuple[Optional[AgentSession], Optional[dict[str, Any]]]:
        """The session a renewal or close names, when it places an agent on the asking host; else a refusal."""

        certificate = verified.certificate
        assert certificate is not None
        team, session = verified.frame.issuer.team, str(verified.raw["body"]["session"])
        stored = await self._session(team, session)
        if stored is None or stored.runtime != certificate.runtime:
            return None, {"outcome": "not_found", "message": f"No session {session} places an agent on this host."}
        return stored, None

    def _signed_again(self, stored: AgentSession, ends_at: datetime, now: datetime) -> dict[str, Any]:
        """The held session signed again with another end, under the next sequence."""

        key = session_key(stored.installation, stored.team, stored.session)
        held = self.trust.store.held_record("agent_session", key)
        assert held is not None  # _session returned it from the store
        body = {"session": stored.session, "installation": stored.installation, "team": stored.team, "agent": stored.agent,
                "agent_type": stored.agent_type, "agent_role": stored.agent_role, "runtime": stored.runtime,
                "placement_revision": stored.placement_revision, "started_at": format_timestamp(stored.started_at),
                "ends_at": format_timestamp(ends_at)}
        return self.records.signed("agent_session", key, body, sequence=held[0] + 1, issued_at=now)

    async def renew(self, verified: VerifiedFrame) -> dict[str, Any]:
        stored, refusal = await self._own_session(verified)
        if refusal is not None:
            return refusal
        assert stored is not None
        now = self.clock()
        if stored.ends_at <= now:
            return {"outcome": "not_allowed", "message": f"Session {stored.session} has ended; open a new one."}
        revision, placement = await self._placement(placement_key(stored.team, stored.agent))
        if placement is None or placement.body.host != stored.runtime:
            return {"outcome": "not_allowed", "message": "The agent is no longer placed on this host."}
        if revision != stored.placement_revision:
            # The agent was placed again after this session (a new session names that placement).
            return {"outcome": "not_allowed",
                    "message": f"Session {stored.session} names an earlier placement of the agent; open a new one."}
        renewed = self._signed_again(stored, now + SESSION_LIFETIME, now)
        await self._store_session(stored.team, stored.session, renewed)
        return {"outcome": "ok", "session": renewed}

    async def close(self, verified: VerifiedFrame) -> dict[str, Any]:
        stored, refusal = await self._own_session(verified)
        if refusal is not None:
            return refusal
        assert stored is not None
        now = self.clock()
        ended = max(now, stored.started_at + timedelta(milliseconds=1))
        closed = self._signed_again(stored, ended, now)
        await self._store_session(stored.team, stored.session, closed)
        if verified.raw["body"]["reason"] != "handed_over":
            await self._remove_placement(stored.team, stored.agent, stored.runtime)
        self.logger.info("Recorded an agent session's end. event=agent_session_closed team=%s agent=%s session=%s reason=%s",
                         stored.team, stored.agent, stored.session, verified.raw["body"]["reason"])
        return {"outcome": "ok", "ended_at": format_timestamp(ended)}

    async def _remove_placement(self, team: str, agent: str, runtime: str) -> None:
        revision, placement = await self._placement(placement_key(team, agent))
        if placement is None or placement.body.host != runtime:
            return
        try:
            await (await self.js.key_value(PLACEMENT_BUCKET)).delete(placement_key(team, agent), last=revision)
        except KeyWrongLastSequenceError:
            self.logger.warning("A placement changed while its session closed; leaving it. event=placement_kept "
                                "team=%s agent=%s", team, agent)

"""Placing agents and signing their sessions (README "Agent sessions"; plan 13B.5).

A host signs frames for an agent only while a session places that agent on it. The
team services decide:

- ``sessions.open``: the host's certificate allows the agent's type and has room under
  its session limit; the agent is not placed on another host (agents stay on their host
  and move only by a handover, decision 51), else ``already_placed`` names that host. The
  placement is written with compare-and-set, and the session, with the placement's
  revision, is signed into ``UA1_CERTS`` for every receiver to read. Only the team lead's
  type (``TeamArchitect``) on a lead host gets the ``lead`` role. At most one
  unended lead session exists per team. A second lead is refused; reopening the
  same agent ends its earlier lead sessions before a replacement is signed.
- ``sessions.renew``: a session that places the agent on the asking host, re-signed for
  another 7 days. Only one that has not ended and whose placement is still the agent's
  current one: a session closed earlier must not come back because its agent was placed
  on the same host again (``not_allowed``; the host opens a new one).
- ``sessions.close``: the session's actual end. The placement is removed when the agent
  exited or failed and kept when it is handed over. A session that has already ended is
  not signed again: the answer is its recorded end. Only the session's own placement is
  ever removed, so a repeated or late close never removes the placement of a newer
  session of the same agent, and a retry still finishes a close whose removal failed.
- **A host on another contract** (README "Versions"): while the label is a pre-release,
  hosts of one installation refuse a peer whose contract digest differs. A host whose
  record names another digest than the team services' gets no new placement
  (``sessions.open`` answers ``not_allowed``), logged with both digests. Its sessions are
  still renewed and closed: it keeps receiving control messages for the tasks it holds
  while it finishes or hands them back, and renewing an agent it already runs places
  nothing new.

**Which placement a session names.** A session records its placement's revision when it
started (``placement_revision``), and its ``started_at`` is the placement's ``placed_at``.
The team services sign their placements again under a current certificate after they
renew (``maintain``; README "State reports outlive certificates"), which gives the same
placement a new revision. So a session's placement is recognised by its host and
``placed_at``, which each new placement of an agent moves later, never by the revision.

**Handovers** to another host come with phase 3's custody windows (README "Custody
windows"): a placement kept by a ``handed_over`` close still names the old host, so
another host's ``sessions.open`` is refused ``already_placed`` until then. The same host
may place the agent again. Its placement is marked before the session ends, so deleting
the retained session cannot erase an intentional handover. Other placements without a
retained matching session are abandoned writes or forgotten sessions and are removed
under the mutation lock before opening and during upkeep. All session reads finish
before removal, and compare-and-set protects a newer placement.

Every refused request is logged with its outcome (``event=agent_session_refused``), as
plan 13B.12 asks, so an operator can see why an agent got no session.

Every process shares one NATS login in this phase, so any of them can write
``UA1_PLACEMENT`` and ``UA1_CERTS``. The team services use a placement or session they
read there only when it verifies: signed by a team services host (the registry's signer
roles, which the reader checks) and stored under its own key. One that does not verify,
or a key that holds no record at all, counts as absent and is logged with its key and
the refusal (``event=team_services_record_refused``); its revision still lets the next
placement replace it. A renewal or close re-signs the session the team services hold,
never what another process wrote.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from nats.js.errors import KeyWrongLastSequenceError

from custom_messages.contracts.runtime_v1 import MANAGER_AGENT_TYPE
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import AgentSession, VerifiedRecord, kept_for
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.state_records import StateRecords, rising_sequence
from custom_transport.protocol1.trust_state import PersistentTrustStore

SESSION_LIFETIME = timedelta(days=7)
LEAD_ROLE, WORKER_ROLE = "lead", "worker"
PLACEMENT, AGENT_SESSION = "placement", "agent_session"
HANDED_OVER = "handed_over"
# One placement of an agent later than the one before it, so ``placed_at`` names each placement.
_PLACED_STEP = timedelta(milliseconds=1)
_LOGGER = logging.getLogger(__name__)


def placement_key(team: str, agent: str) -> str:
    return f"{team}.{agent}"


def session_key(installation: str, team: str, session: str) -> str:
    return f"session.{installation}.{team}.{session}"


def _places(placement: Optional[VerifiedRecord], session: AgentSession) -> bool:
    """Whether ``placement`` is the one ``session`` was opened for: the same host and ``placed_at`` (module docstring)."""

    return (placement is not None and placement.body.host == session.runtime
            and placement.body.placed_at == format_timestamp(session.started_at))


@dataclass
class SessionService:
    records: StateRecords
    trust: PersistentTrustStore
    installation: str
    clock: Callable[[], datetime]
    logger: logging.Logger = _LOGGER
    # Requests run one at a time (``LiveHost.serve``); ``maintain`` takes turns with them, so no
    # compare-and-set of a request loses to the team services' own rewrite of a placement.
    _changing: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _refused(self, record: str, key: str, refused: FrameRefused) -> None:
        self.logger.warning("A stored record does not verify and counts as absent. event=team_services_record_refused "
                            "record=%s key=%s code=%s at=%s", record, key, refused.code, refused.at)

    def _refusal(self, kind: str, outcome: str, message: str, asked: dict[str, str],
                 answer: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """A refused session request's answer, logged with its outcome and who asked for what (module docstring)."""

        self.logger.warning("A session request was refused. event=agent_session_refused kind=%s outcome=%s team=%s "
                            "agent=%s host=%s message=%s", kind, outcome, asked["team"], asked["agent"], asked["host"],
                            message)
        return {"outcome": outcome, "message": message, **(answer or {})}

    async def _placement(self, key: str) -> tuple[int, Optional[VerifiedRecord]]:
        """The key's revision (0 when absent) and its placement, ``None`` when absent or it does not verify.

        The revision of a record that does not verify, or of a key that holds no record,
        still lets the next placement replace it with compare-and-set.
        """

        try:
            stored = await self.records.read_report(PLACEMENT, key, now=self.clock())
        except FrameRefused as refused:
            self._refused(PLACEMENT, key, refused)
            return await self.records.revision(PLACEMENT, key), None
        if stored is not None:
            return stored.revision, stored.record
        revision = await self.records.revision(PLACEMENT, key)
        if revision:
            self.logger.warning("A placement key holds no record and counts as absent. "
                                "event=team_services_record_refused record=placement key=%s code=empty", key)
        return revision, None

    async def _host_placements(self, team: str, runtime: str) -> list[str]:
        """The agents of ``team`` placed on ``runtime``."""

        agents = []
        for key in await self.records.keys(PLACEMENT, f"{team}.", team=team):
            _revision, placement = await self._placement(key)
            if placement is not None and placement.body.host == runtime:
                agents.append(key.split(".", 1)[1])
        return agents

    async def _other_contract(self, runtime: str) -> Optional[str]:
        """The contract digest ``runtime``'s host record names when it is not the team services' own; else ``None``.

        A record that is absent or does not verify says nothing about it (the host joined
        on this contract, which the installation authority checked).
        """

        try:
            stored = await self.records.read_report("host", runtime, now=self.clock())
        except FrameRefused as refused:
            self._refused("host", runtime, refused)
            return None
        if stored is None or not stored.record.other_contract:
            return None
        return stored.record.their_contract_sha256

    async def open(self, verified: VerifiedFrame) -> dict[str, Any]:
        async with self._changing:
            return await self._open(verified)

    async def _open(self, verified: VerifiedFrame) -> dict[str, Any]:
        certificate = verified.certificate
        assert certificate is not None  # hosts send it with their certificate
        team, body = verified.frame.issuer.team, verified.raw["body"]
        agent, agent_type = str(body["agent"]), str(body["agent_type"])
        asked = {"team": team, "agent": agent, "host": certificate.runtime}
        if agent_type not in certificate.agent_types:
            return self._refusal("sessions.open", "not_allowed",
                                 f"This host's certificate does not allow agent type {agent_type}.", asked)
        theirs = await self._other_contract(certificate.runtime)
        if theirs is not None:
            self.logger.warning("A host on another contract asked to run an agent; it gets no new placement. "
                                "event=agent_session_refused_other_contract team=%s agent=%s host=%s "
                                "their_contract_sha256=%s contract_sha256=%s", team, agent, certificate.runtime, theirs,
                                self.trust.store.contract_sha256)
            return {"outcome": "not_allowed", "message": "This host runs another message contract than the team "
                    "services, so no agent is placed on it; install the installation's release there."}
        await self._remove_orphan_placements(team, runtime=certificate.runtime)
        key = placement_key(team, agent)
        current, previous = await self._placement(key)
        if previous is not None and previous.body.host != certificate.runtime:
            return self._refusal("sessions.open", "already_placed",
                                 f"{agent} runs on another host; it moves only by a handover.", asked,
                                 {"host": previous.body.host})
        if previous is None and len(await self._host_placements(team, certificate.runtime)) >= certificate.session_limit:
            return self._refusal("sessions.open", "limit_reached",
                                 "This host already runs as many agents as its certificate allows.", asked)
        role = LEAD_ROLE if agent_type == MANAGER_AGENT_TYPE and "lead_host" in certificate.roles else WORKER_ROLE
        earlier_leads = await self._lead_sessions(team) if role == LEAD_ROLE else []
        if any(session.agent != agent or session.runtime != certificate.runtime for session in earlier_leads):
            return self._refusal("sessions.open", "not_allowed",
                                 "This team already has a lead session; close it before starting another lead.", asked)
        now = parse_timestamp(format_timestamp(self.clock()))  # the protocol's millisecond precision
        if previous is not None and parse_timestamp(previous.body.placed_at) >= now:
            # Each placement of an agent later than the one before, so its placed_at names it (module docstring).
            now = parse_timestamp(previous.body.placed_at) + _PLACED_STEP
        placement = {"team": team, "agent": agent, "agent_type": agent_type, "host": certificate.runtime,
                     "host_epoch": certificate.membership_epoch, "definition_sha256": str(body["definition_sha256"]),
                     "placed_at": format_timestamp(now), "reason": "initial"}
        # Never below an earlier placement of the agent, also after a close deleted it (README "Sequences never go back").
        sequence = rising_sequence(previous.document.sequence if previous is not None else 0, now)
        try:
            revision = await self.records.put(PLACEMENT, key, self.records.signed(
                PLACEMENT, key, placement, sequence=sequence, issued_at=now), last_revision=current)
        except KeyWrongLastSequenceError:
            # Another open for this agent won; the asking host may ask again and learn where it went.
            return self._refusal("sessions.open", "runtime_error", "The placement changed at the same time; ask again.",
                                 asked)
        # Reopening the same agent may replace a lost open reply or a refused renewal.
        # End its old authority before signing the replacement; never two lead sessions.
        for earlier in earlier_leads:
            await self._store_session(team, earlier.session, self._signed_again(earlier, now, now))
            self.logger.info("Ended the earlier session before reopening the lead. "
                             "event=lead_session_replaced team=%s agent=%s session=%s", team, agent, earlier.session)
        session = f"s-{agent}-{revision}"
        record = self.records.signed(AGENT_SESSION, session_key(self.installation, team, session), {
            "session": session, "installation": self.installation, "team": team, "agent": agent,
            "agent_type": agent_type, "agent_role": role, "runtime": certificate.runtime, "placement_revision": revision,
            "started_at": format_timestamp(now), "ends_at": format_timestamp(now + SESSION_LIFETIME)},
            sequence=1, issued_at=now)
        await self._store_session(team, session, record)
        self.logger.info("Placed an agent and signed its session. event=agent_session_opened team=%s agent=%s host=%s "
                         "session=%s role=%s", team, agent, certificate.runtime, session, role)
        return {"outcome": "ok", "session": record}

    async def _lead_sessions(self, team: str) -> list[AgentSession]:
        """All unended lead sessions, under the same mutation lock as open and close.

        Read retained signed records, so a team-services restart cannot forget the
        lead. A restarting lead host closes its earlier sessions before opening a
        new one (AgentProcessHost._close_earlier_sessions); silence alone is not a close.
        """

        prefix = f"session.{self.installation}.{team}."
        sessions = []
        for key in await self.records.keys(AGENT_SESSION, prefix):
            session = await self._session(team, key[len(prefix):], require_readable=True)
            if session is not None and session.agent_role == LEAD_ROLE and session.ends_at > self.clock():
                sessions.append(session)
        return sessions

    async def _store_session(self, team: str, session: str, record: dict[str, Any]) -> None:
        await self.records.put(AGENT_SESSION, session_key(self.installation, team, session), record)
        self.trust.add(json.dumps(record).encode(), now=self.clock())

    async def _session(self, team: str, session: str, *, require_readable: bool = False) -> Optional[AgentSession]:
        """The session as the team services' trust store holds it, after taking the bucket's record when it verifies.

        A newer record in ``UA1_CERTS`` that verifies is taken; one that does not is
        logged, and the one held stays (none after a restart: the session counts as absent).
        Opening a lead requires every retained session to be readable, so that check
        raises instead: unreadable evidence never authorizes a second lead.
        """

        key = session_key(self.installation, team, session)
        try:
            await self.records.take_trust_record(AGENT_SESSION, key, now=self.clock())
        except FrameRefused as refused:
            self._refused(AGENT_SESSION, key, refused)
            if require_readable:
                # A missing verification dependency is not proof that no lead exists.
                # Opening another lead must wait until the retained sessions are readable.
                raise
        return self.trust.store.agent_session(self.installation, team, session)

    async def _own_session(self, verified: VerifiedFrame) -> tuple[Optional[AgentSession], Optional[dict[str, Any]]]:
        """The session a renewal or close names, when it places an agent on the asking host; else a refusal."""

        certificate = verified.certificate
        assert certificate is not None
        team, session = verified.frame.issuer.team, str(verified.raw["body"]["session"])
        stored = await self._session(team, session)
        if stored is None or stored.runtime != certificate.runtime:
            asked = {"team": team, "agent": stored.agent if stored is not None else "-", "host": certificate.runtime}
            return None, self._refusal(verified.frame.kind, "not_found",
                                       f"No session {session} places an agent on this host.", asked)
        return stored, None

    def _signed_again(self, stored: AgentSession, ends_at: datetime, now: datetime) -> dict[str, Any]:
        """The held session signed again with another end, under the next sequence."""

        key = session_key(stored.installation, stored.team, stored.session)
        held = self.trust.store.held_record(AGENT_SESSION, key)
        assert held is not None  # _session returned it from the store
        body = {"session": stored.session, "installation": stored.installation, "team": stored.team, "agent": stored.agent,
                "agent_type": stored.agent_type, "agent_role": stored.agent_role, "runtime": stored.runtime,
                "placement_revision": stored.placement_revision, "started_at": format_timestamp(stored.started_at),
                "ends_at": format_timestamp(ends_at)}
        return self.records.signed(AGENT_SESSION, key, body, sequence=held[0] + 1, issued_at=now)

    async def renew(self, verified: VerifiedFrame) -> dict[str, Any]:
        async with self._changing:
            return await self._renew(verified)

    async def _renew(self, verified: VerifiedFrame) -> dict[str, Any]:
        stored, refusal = await self._own_session(verified)
        if refusal is not None:
            return refusal
        assert stored is not None
        asked = {"team": stored.team, "agent": stored.agent, "host": stored.runtime}
        now = self.clock()
        if stored.ends_at <= now:
            return self._refusal("sessions.renew", "not_allowed", f"Session {stored.session} has ended; open a new one.",
                                 asked)
        _revision, placement = await self._placement(placement_key(stored.team, stored.agent))
        if placement is None or placement.body.host != stored.runtime:
            return self._refusal("sessions.renew", "not_allowed", "The agent is no longer placed on this host.", asked)
        if not _places(placement, stored):
            # The agent was placed again after this session (a new session names that placement).
            return self._refusal("sessions.renew", "not_allowed",
                                 f"Session {stored.session} names an earlier placement of the agent; open a new one.",
                                 asked)
        renewed = self._signed_again(stored, now + SESSION_LIFETIME, now)
        await self._store_session(stored.team, stored.session, renewed)
        return {"outcome": "ok", "session": renewed}

    async def close(self, verified: VerifiedFrame) -> dict[str, Any]:
        async with self._changing:
            return await self._close(verified)

    async def _close(self, verified: VerifiedFrame) -> dict[str, Any]:
        stored, refusal = await self._own_session(verified)
        if refusal is not None:
            return refusal
        assert stored is not None
        reason = verified.raw["body"]["reason"]
        now = self.clock()
        if reason == HANDED_OVER:
            # Persist the reason before ending the session. Its eventual retention
            # deletion must not turn an intentional handover into an orphan.
            await self._mark_handover(stored)
        if stored.ends_at <= now:
            # Already ended (closed, or past its end): not signed again, so it covers nothing more
            # (README "Agent sessions"). An earlier close whose removal failed is finished here.
            ended = stored.ends_at
            self.logger.info("A session that has already ended was closed again; its end stays. "
                             "event=agent_session_close_repeated team=%s agent=%s session=%s ended_at=%s",
                             stored.team, stored.agent, stored.session, format_timestamp(ended))
        else:
            ended = max(now, stored.started_at + timedelta(milliseconds=1))
            closed = self._signed_again(stored, ended, now)
            await self._store_session(stored.team, stored.session, closed)
            self.logger.info("Recorded an agent session's end. event=agent_session_closed team=%s agent=%s session=%s "
                             "reason=%s", stored.team, stored.agent, stored.session, reason)
        if reason != HANDED_OVER:
            await self._remove_placement(stored)
        return {"outcome": "ok", "ended_at": format_timestamp(ended)}

    async def _mark_handover(self, session: AgentSession) -> None:
        key = placement_key(session.team, session.agent)
        revision, placement = await self._placement(key)
        if placement is None or not _places(placement, session) or placement.body.handed_over is True:
            return
        now = self.clock()
        document = self.records.signed(PLACEMENT, key, {**placement.document.body, "handed_over": True},
                                       sequence=rising_sequence(placement.document.sequence, now), issued_at=now)
        await self.records.put(PLACEMENT, key, document, last_revision=revision)

    async def _remove_placement(self, session: AgentSession) -> None:
        """Remove the placement ``session`` was opened for; a newer placement of the agent stays."""

        key = placement_key(session.team, session.agent)
        revision, placement = await self._placement(key)
        if not _places(placement, session):
            return
        if not await self.records.delete(PLACEMENT, key, last_revision=revision):
            self.logger.warning("A placement changed while its session closed; leaving it. event=placement_kept "
                                "team=%s agent=%s session=%s", session.team, session.agent, session.session)

    # -- upkeep (module docstring) ----------------------------------------------------------------

    async def maintain(self, team: str) -> None:
        """Sign placements again under the current certificate, and delete sessions receivers have forgotten.

        README "State reports outlive certificates": a receiver that forgot the certificate
        that signed a placement refuses it, so after the team services renew, each placement
        an earlier certificate signed is written again, the same placement under the next
        sequence. README "Trust over time": writers delete a record from ``UA1_CERTS`` once
        receivers forget it (``kept_for``). Each step is logged; one that fails is tried
        again at the next pass.
        """

        async with self._changing:
            await self._sign_placements_again(team)
            await self._delete_forgotten_sessions(team)
            await self._remove_orphan_placements(team)

    async def _remove_orphan_placements(self, team: str, *, runtime: Optional[str] = None) -> None:
        """Remove verified abandoned placements; a marked handover stays without its ended session.

        Opening writes placement before session because the session names its KV
        revision. The shared mutation lock keeps this scan outside that write gap.
        Finish every session read before deleting anything: a failed read is not
        evidence that a session is missing. The placement CAS protects newer work.
        """
        candidates = []
        for key in await self.records.keys(PLACEMENT, f"{team}.", team=team):
            revision, placement = await self._placement(key)
            if placement is not None and placement.body.handed_over is not True and (
                runtime is None or placement.body.host == runtime
            ):
                candidates.append((key, revision, placement))
        if not candidates:
            return
        sessions = set()
        prefix = f"session.{self.installation}.{team}."
        for key in await self.records.keys(AGENT_SESSION, prefix):
            session = await self._session(team, key[len(prefix):])
            if session is not None:
                sessions.add((session.runtime, session.agent, format_timestamp(session.started_at)))
        for key, revision, placement in candidates:
            if (placement.body.host, placement.body.agent, placement.body.placed_at) in sessions:
                continue
            if await self.records.delete(PLACEMENT, key, last_revision=revision):
                self.logger.warning("Removed an abandoned placement with no retained session. "
                                    "event=orphan_placement_removed team=%s agent=%s host=%s revision=%s",
                                    team, placement.body.agent, placement.body.host, revision)

    async def _sign_placements_again(self, team: str) -> None:
        epoch = self.records.writer().certificate.membership_epoch
        for key in await self.records.keys(PLACEMENT, f"{team}.", team=team):
            revision, placement = await self._placement(key)
            if placement is None or placement.signer is None or placement.signer.membership_epoch == epoch:
                continue
            now = self.clock()
            # The same placement, body unchanged (its placed_at names it), under the next sequence.
            document = self.records.signed(PLACEMENT, key, dict(placement.document.body),
                                           sequence=rising_sequence(placement.document.sequence, now), issued_at=now)
            try:
                await self.records.put(PLACEMENT, key, document, last_revision=revision)
            except KeyWrongLastSequenceError:
                continue  # another write came first; the next pass looks again
            self.logger.info("Signed a placement again under the current certificate. event=placement_signed_again "
                             "key=%s previous_epoch=%s membership_epoch=%s", key,
                             placement.signer.membership_epoch, epoch)

    async def _delete_forgotten_sessions(self, team: str) -> None:
        kept = kept_for(AGENT_SESSION)
        assert kept is not None
        now = self.clock()
        for key in await self.records.keys(AGENT_SESSION, f"session.{self.installation}.{team}."):
            entry = await self.records.raw(AGENT_SESSION, key)
            if entry is None:
                continue
            revision, data = entry
            try:
                written = parse_timestamp(json.loads(data)["issued_at"])
            except (ValueError, KeyError, TypeError):
                continue  # not a record these services wrote; it verifies nowhere
            if now - written < kept:
                continue
            if await self.records.delete(AGENT_SESSION, key, last_revision=revision):
                self.logger.info("Deleted a session receivers have forgotten. event=agent_session_deleted key=%s "
                                 "written_at=%s", key, format_timestamp(written))

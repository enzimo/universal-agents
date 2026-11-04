"""Signed live messages over NATS: requests, their replies, and the addresses a host serves.

A host signs every frame with its own key for the issuer its certificate names
(``identity``), sends requests to the receiving service's address, and verifies each
reply against the request it answers before anyone reads it. A host that serves an
address verifies each request with the receiver's checks, in the registry's order,
answers it with a signed reply addressed as ``reply_audience`` says, and answers a
refused request at once with the refusal's code and known pointer
(README "Refused requests"), never its detail.

Requests use NATS request/reply with this host's own inbox prefix
(``_INBOX.<runtime>``), which the connection sets (``NatsTransportAdapter``). Every
frame is checked before a handler sees it; a handler receives only a
``VerifiedFrame``. Events and queued work are not sent here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import (
    HEADER_ID,
    HEADER_KIND,
    REPLY_SUFFIX,
    Receiver,
    VerifiedFrame,
    expected_idempotency_key,
    reply_audience,
    sign_frame,
    verify_frame,
)
from custom_messages.wire.kinds import (
    INSTALLATION_ADDRESS_PREFIX,
    INSTALLATION_SCOPE,
    KINDS,
    PROTOCOL,
    SUBJECT_PREFIX,
)
from custom_messages.wire.models import Frame
from custom_messages.wire.refusals import (
    HANDLER_FAILED,
    REFUSAL_AT_HEADER,
    REFUSAL_HEADER,
    UNSUPPORTED,
    FrameRefused,
    refusal_headers,
)
from custom_messages.wire.spec import KindSpec
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.ids import new_message_id
from custom_transport.protocol1.replay import LiveReplayRecord
from custom_transport.protocol1.trust_state import PersistentTrustStore

_SUBJECT_ROOT = SUBJECT_PREFIX.split("{team}", 1)[0]
Handler = Callable[[VerifiedFrame], Awaitable[Mapping[str, Any]]]
# Reads the certificate or session a frame names from UA1_CERTS into the trust store;
# returns whether it took anything new (README "Who sent it": receivers look records up).
TrustFetcher = Callable[[bytes], Awaitable[bool]]
# Refusals another record could cure: a certificate or session this receiver does not hold yet.
_CURABLE = frozenset({"unknown_key", "wrong_issuer"})
# A relayed frame carries a token another host signed (a gateway's command token); a receiver
# that has not met that host yet reads its certificate too (MessageHost.fetch_trust).
_TOKEN_ISSUER_AT = "/authority/issuer"


class ServiceUnavailable(RuntimeError):
    """No receiver answered: nobody serves the address ("no responders"), or no answer came in time."""

    def __init__(self, kind: str, subject: str, reason: str) -> None:
        super().__init__(f"{kind} to {subject}: {reason}")
        self.kind, self.subject, self.reason = kind, subject, reason


class RequestRefused(RuntimeError):
    """The receiver refused the request (README "Refused requests"): its code and known pointer, never more."""

    def __init__(self, kind: str, code: str, at: str) -> None:
        super().__init__(f"{kind} refused: {code} at {at or '/'}")
        self.kind, self.code, self.at = kind, code, at


class Unsupported(Exception):
    """Raised by a handler for a kind it does not serve; answered with ``unsupported``."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _spec(kind: str) -> tuple[KindSpec, bool]:
    is_reply = kind.endswith(REPLY_SUFFIX) and kind[: -len(REPLY_SUFFIX)] in KINDS
    spec = KINDS.get(kind[: -len(REPLY_SUFFIX)] if is_reply else kind)
    if spec is None:
        raise ValueError(f"{kind} is not a kind of protocol {PROTOCOL}")
    return spec, is_reply


def request_subject(kind: str, audience: Mapping[str, Any], sender: str) -> str:
    """The subject a request of ``kind`` to ``audience`` goes to, sent by runtime ``sender``."""

    spec, _ = _spec(kind)
    address = spec.addresses.get(str(audience["target"]))
    if address is None:
        raise ValueError(f"{kind} is not delivered to a {audience['target']} audience")
    audience_id = str(audience["id"])
    filled = address.format(agent=audience_id, pool=audience_id, edge=audience_id, sender=sender)
    if address.startswith(INSTALLATION_ADDRESS_PREFIX.split("{", 1)[0]):
        return _SUBJECT_ROOT + filled
    return SUBJECT_PREFIX.format(team=audience["team"], sender=sender) + "." + filled


def serving_subject(address: str, team: str, receiver_id: str) -> str:
    """The subject a host subscribes to for one of its addresses: any sender, this team and receiver."""

    filled = address.format(agent=receiver_id, pool=receiver_id, edge=receiver_id, sender="*")
    if address.startswith(INSTALLATION_ADDRESS_PREFIX.split("{", 1)[0]):
        return _SUBJECT_ROOT + filled
    return SUBJECT_PREFIX.format(team=team, sender="*") + "." + filled


def build_frame(kind: str, body: Mapping[str, Any], audience: Mapping[str, Any], *, issuer: Mapping[str, Any],
                issued: datetime, subject: str, authority: Optional[Mapping[str, Any]] = None,
                answering: Optional[Frame] = None, lifetime_seconds: Optional[int] = None) -> dict[str, Any]:
    """An unsigned frame of ``kind`` from ``issuer``, its body checked against the kind's schema first.

    A sender's mistake is caught here rather than refused by the receiver. The caller
    signs it with the key the issuer names (``sign_frame``).
    """

    spec, is_reply = _spec(kind)
    model = spec.reply_model if is_reply else spec.body_model
    assert model is not None  # replies exist only for operations with a reply schema
    model.validate_python(dict(body))
    lifetime = min(lifetime_seconds or spec.expiry_seconds, spec.expiry_seconds)
    frame: dict[str, Any] = {
        "protocol": PROTOCOL,
        "kind": kind,
        "message_id": new_message_id(int(issued.timestamp() * 1000)),
        "issuer": dict(issuer),
        "audience": dict(audience),
        "subject": subject,
        "issued_at": format_timestamp(issued),
        "expires_at": format_timestamp(issued + timedelta(seconds=lifetime)),
    }
    if authority is not None:
        frame["authority"] = dict(authority)
    frame["algorithm"] = "ed25519"
    frame["body"] = dict(body)
    if answering is not None:
        frame["causation_id"] = answering.message_id
    frame["idempotency_key"] = expected_idempotency_key(frame, spec, is_reply, answering)
    return frame


def encode(frame: Mapping[str, Any]) -> tuple[bytes, dict[str, str]]:
    """A frame's bytes and the header copies every publish sets (README "NATS headers")."""

    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), {
        HEADER_KIND: str(frame["kind"]), HEADER_ID: str(frame["message_id"])}


def answer_headers(message: Any) -> dict[str, str]:
    return dict(message.headers or {})


def raise_if_refused(kind: str, subject: str, message: Any, logger: logging.Logger) -> None:
    """Raise ``RequestRefused`` when the answer is a refusal (README "Refused requests")."""

    headers = answer_headers(message)
    if REFUSAL_HEADER in headers:
        code, at = headers[REFUSAL_HEADER], headers.get(REFUSAL_AT_HEADER, "")
        logger.warning("A request was refused. event=protocol1_request_refused kind=%s subject=%s code=%s at=%s",
                       kind, subject, code, at)
        raise RequestRefused(kind, code, at)


@dataclass
class LiveHost:
    """One host's live messaging: it signs as ``identity`` and checks with ``trust``."""

    nc: Any
    identity: HostIdentity
    trust: PersistentTrustStore
    clock: Callable[[], datetime] = utc_now
    logger: logging.Logger = logging.getLogger(__name__)

    fetch_trust: Optional[TrustFetcher] = None

    def __post_init__(self) -> None:
        self.replay = LiveReplayRecord()

    async def verify(self, data: bytes, **checks: Any) -> VerifiedFrame:
        """``verify_frame``, reading once from the buckets a certificate or session this host does not hold yet."""

        try:
            return verify_frame(data, trust=self.trust.store, **checks)
        except FrameRefused as refused:
            curable = refused.code in _CURABLE or (refused.code == "bad_authority"
                                                    and refused.known_at == _TOKEN_ISSUER_AT)
            if not curable or self.fetch_trust is None or not await self.fetch_trust(data):
                raise
            return verify_frame(data, trust=self.trust.store, **checks)

    # -- signing --------------------------------------------------------------------------------

    def sign(self, kind: str, body: Mapping[str, Any], audience: Mapping[str, Any], *, team: str,
             agent: Optional[str] = None, authority: Optional[Mapping[str, Any]] = None,
             subject: Optional[str] = None, answering: Optional[Frame] = None,
             lifetime_seconds: Optional[int] = None) -> dict[str, Any]:
        """A signed frame of ``kind``; a reply names the request it answers and goes to its reply subject.

        The body is checked against the kind's schema first, so a sender's mistake is
        caught here rather than refused by the receiver.
        """

        frame = build_frame(kind, body, audience, issuer=self.identity.issuer(team, agent), issued=self.clock(),
                            subject=subject or request_subject(kind, audience, self.identity.runtime),
                            authority=authority, answering=answering, lifetime_seconds=lifetime_seconds)
        return sign_frame(frame, self.identity.keys.application_key)

    encode = staticmethod(encode)

    # -- requesting -----------------------------------------------------------------------------

    def reply_receiver(self, request: Frame) -> Receiver:
        """How this host receives the reply to its own request."""

        named = reply_audience(request)
        return Receiver(named["installation"], named["team"], named["target"], named["id"], named["runtime"])

    async def request(self, kind: str, body: Mapping[str, Any], audience: Mapping[str, Any], *, team: str,
                      agent: Optional[str] = None, authority: Optional[Mapping[str, Any]] = None,
                      timeout: Optional[float] = None) -> VerifiedFrame:
        """Send a request and return its verified reply.

        Raises ``ServiceUnavailable`` when nobody serves the address or no answer came
        within the request's lifetime, ``RequestRefused`` when the receiver refused it,
        and ``FrameRefused`` when the reply itself fails verification.
        """

        frame = self.sign(kind, body, audience, team=team, agent=agent, authority=authority)
        data, headers = self.encode(frame)
        spec, _ = _spec(kind)
        wait = min(timeout if timeout is not None else spec.expiry_seconds, spec.expiry_seconds)
        try:
            message = await self.nc.request(frame["subject"], data, timeout=wait, headers=headers)
        except (NoRespondersError, NatsTimeoutError) as error:
            reason = "no responders" if isinstance(error, NoRespondersError) else "no answer in time"
            self.logger.warning("A request got no answer. event=protocol1_request_unanswered kind=%s subject=%s reason=%s",
                                kind, frame["subject"], reason)
            raise ServiceUnavailable(kind, frame["subject"], reason) from error
        raise_if_refused(kind, frame["subject"], message, self.logger)
        request = Frame.model_validate(frame)
        now = self.clock()
        verified = await self.verify(message.data, receiver=self.reply_receiver(request), arrived_on=message.subject,
                                     now=now, replay=self.replay.lookup, headers=answer_headers(message),
                                     answering=request)
        self.replay.admit(verified, now)
        return verified

    # -- serving --------------------------------------------------------------------------------

    async def serve(self, address: str, receiver: Receiver, handler: Handler, *,
                    agent: Optional[str] = None) -> Any:
        """Answer requests at one of this host's addresses; returns the NATS subscription.

        ``receiver`` is who this host receives as there (the team services of a team,
        an agent it runs, the installation service); ``agent`` signs the replies for
        that agent when the address is an agent's.
        """

        subject = serving_subject(address, receiver.team, receiver.id)

        async def on_message(message: Any) -> None:
            await self._answer(message, receiver, handler, agent)

        return await self.nc.subscribe(subject, cb=on_message)

    async def _answer(self, message: Any, receiver: Receiver, handler: Handler, agent: Optional[str]) -> None:
        now = self.clock()
        try:
            verified = await self.verify(message.data, receiver=receiver, arrived_on=message.subject, now=now,
                                         replay=self.replay.lookup, headers=dict(message.headers or {}))
        except FrameRefused as refused:
            self.logger.warning("A request was refused. event=protocol1_frame_refused subject=%s code=%s at=%s detail=%s",
                                message.subject, refused.code, refused.known_at, refused.detail)
            await self._refuse(message, refused)
            return
        self.replay.admit(verified, now)
        frame = verified.frame
        try:
            body = await handler(verified)
        except Unsupported:
            await self._refuse(message, FrameRefused(UNSUPPORTED, at="/kind"))
            return
        except Exception:
            self.logger.exception("A request's handler failed. event=protocol1_handler_failed kind=%s message_id=%s",
                                  frame.kind, frame.message_id)
            await self._refuse(message, FrameRefused(HANDLER_FAILED))
            return
        if not message.reply:
            return
        team = INSTALLATION_SCOPE if frame.issuer.team == INSTALLATION_SCOPE else receiver.team
        try:
            reply = self.sign(f"{frame.kind}{REPLY_SUFFIX}", body, reply_audience(frame), team=team, agent=agent,
                              subject=message.reply, answering=frame)
        except Exception:
            # A reply the schema refuses is this host's defect; the requester still stops waiting.
            self.logger.exception("A reply could not be built. event=protocol1_reply_failed kind=%s message_id=%s",
                                  frame.kind, frame.message_id)
            await self._refuse(message, FrameRefused(HANDLER_FAILED))
            return
        data, headers = self.encode(reply)
        await self.nc.publish(message.reply, data, headers=headers)

    async def _refuse(self, message: Any, refused: FrameRefused) -> None:
        """Answer a refused request at once, with only its code and known pointer (README "Refused requests")."""

        if message.reply:
            await self.nc.publish(message.reply, b"", headers=refusal_headers(refused))

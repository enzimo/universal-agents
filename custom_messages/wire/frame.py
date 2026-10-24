"""Sign and verify protocol 1 frames (contracts/messages/v1).

A frame is signed with Ed25519 over ``SIGNING_DOMAIN`` followed by the canonical
JSON of the frame without its signature. ``verify_frame`` runs the receiver's
checks in the registry's order and refuses with the first failing code, so every
implementation refuses the same frame the same way.

Who sent a frame is decided only by the receiver's ``TrustStore``: the host
certificate for the issuer's installation, runtime and membership epoch, and the
agent session the frame names (README, "Who sent it"). Nothing the frame says about
its sender counts until those records confirm it.

Verification has no side effects. A receiver that admits a frame records
``VerifiedFrame.replay_key`` with ``VerifiedFrame.frame_digest`` in the same
transaction as the admission, so a redelivery is recognised and other content under
the same id is refused as ``replayed``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import MISSING, ValidationError

from custom_messages.wire.canonical import CanonicalJsonError, canonical_bytes, parse
from custom_messages.wire.encoding import b64url, b64url_decode, body_digest, format_timestamp, signed_bytes
from custom_messages.wire.kinds import (
    AUTHORITY_TOKEN_DOMAIN,
    AUTHORITY_TOKEN_MAX_SECONDS,
    BETWEEN_INSTALLATIONS,
    FRAME_SHAPE,
    KINDS,
    MAX_FRAME_BYTES,
    PEER_SENDER_SLOT,
    PROTOCOL,
    REPLY_INBOX_PREFIX,
    SIGNING_DOMAIN,
    SUBJECT_PREFIX,
)
from custom_messages.wire.models import AuthorityToken, Frame
from custom_messages.wire.refusals import FrameRefused, pointer
from custom_messages.wire.spec import KindSpec, prune
from custom_messages.wire.trust import SKEW, AgentSession, HostCertificate, TrustStore, within
from custom_messages.wire.values import parse_timestamp

__all__ = [
    "FrameRefused", "Receiver", "ReplayKey", "ReplayLookup", "VerifiedAuthority", "VerifiedFrame",
    "b64url", "b64url_decode", "body_digest", "body_model", "format_timestamp", "frame_digest",
    "idempotency_key", "sign_authority_token", "sign_frame", "signing_bytes", "verify_frame",
]

REPLY_SUFFIX = ".reply"
# Hosts whose certificates may sign authority tokens (README, "On behalf of a person").
AUTHORITY_ISSUER_ROLES = frozenset({"gateway", "scheduler"})
# Audience targets an address delivers to as a whole team (audience id is the team id).
TEAM_WIDE_TARGETS = frozenset({"team", "team_services"})
# NATS headers that copy frame fields (README, "NATS headers").
HEADER_KIND, HEADER_ID, HEADER_MSG_ID = "UA-Kind", "UA-Id", "Nats-Msg-Id"
DURABLE = "durable"


def idempotency_key(kind: str, *parts: str) -> str:
    """A bounded key for a tuple of ids; JSON preserves their boundaries and order.

    ``kind`` is a registry kind, and each part is a schema-validated record id.
    Even a maximum-length kind and tuple produce at most 140 characters.
    """

    digest = hashlib.sha256(canonical_bytes(list(parts))).digest()
    return f"{kind}:{b64url(digest)}"


def signing_bytes(frame: Mapping[str, Any]) -> bytes:
    """What a frame's signature covers: the domain prefix and the canonical frame without its signature."""

    return signed_bytes(SIGNING_DOMAIN, frame)


def frame_digest(frame: Mapping[str, Any]) -> str:
    """Identifies a frame's content for the replay record: SHA-256 of what its signature covers."""

    return b64url(hashlib.sha256(signing_bytes(frame)).digest())


def authority_signing_bytes(token: Mapping[str, Any]) -> bytes:
    return signed_bytes(AUTHORITY_TOKEN_DOMAIN, token)


def sign_authority_token(unsigned: Mapping[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Return ``unsigned`` with its algorithm and signature filled in; sign it before the frame that carries it."""

    token = {key: value for key, value in unsigned.items() if key != "signature"}
    token["algorithm"] = "ed25519"
    token["signature"] = b64url(private_key.sign(authority_signing_bytes(token)))
    return token


def sign_frame(unsigned: Mapping[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Return ``unsigned`` with its body digest and signature filled in."""

    frame = {key: value for key, value in unsigned.items() if key not in {"signature", "body_digest"}}
    frame["algorithm"] = "ed25519"
    frame["body_digest"] = body_digest(frame["body"])
    frame["signature"] = b64url(private_key.sign(signing_bytes(frame)))
    return frame


@dataclass(frozen=True)
class Receiver:
    """Who is receiving: the audience a frame must name, and the host it runs on."""

    installation: str
    team: str
    target: str
    id: str
    runtime: str


@dataclass(frozen=True)
class ReplayKey:
    installation: str
    runtime: str
    membership_epoch: int
    message_id: str


# Returns the frame digest the receiver recorded when it admitted a frame under this key, if any.
ReplayLookup = Callable[[ReplayKey], Optional[str]]


@dataclass(frozen=True)
class VerifiedAuthority:
    """An authority token whose signature, issuer, audience and validity were checked."""

    token: AuthorityToken
    certificate: HostCertificate

    def require_bound_to(self, bound_type: str, record_id: str) -> None:
        """The handler's last authority check: the token is bound to the root task or command it holds."""

        if (self.token.bound_to.type, self.token.bound_to.id) != (bound_type, record_id):
            raise FrameRefused("bad_authority", "the token is bound to another root task or command",
                               at="/authority/bound_to")


@dataclass(frozen=True)
class VerifiedFrame:
    frame: Frame
    body: Any
    certificate: HostCertificate
    session: Optional[AgentSession]
    authority: Optional[VerifiedAuthority]
    replay_key: ReplayKey
    frame_digest: str
    raw: dict[str, Any] = field(repr=False)


def protocol_minor(label: str) -> int:
    return int(label.split(".", 1)[1].split("-", 1)[0])


def _newer_minor(raw: Mapping[str, Any]) -> bool:
    label = raw.get("protocol")
    try:
        return isinstance(label, str) and label.startswith("1.") and protocol_minor(label) > protocol_minor(PROTOCOL)
    except ValueError:
        return False


def _kind_spec(kind: str) -> tuple[Optional[KindSpec], bool]:
    """The registry entry a kind belongs to, and whether the frame is that operation's reply.

    ``<kind>.reply`` exists only for request/reply operations; for any other kind
    it is unknown, like any name the registry lacks.
    """

    if kind in KINDS:
        return KINDS[kind], False
    if kind.endswith(REPLY_SUFFIX):
        spec = KINDS.get(kind.removesuffix(REPLY_SUFFIX))
        if spec is not None and spec.reply_model is not None:
            return spec, True
    return None, False


def body_model(kind: str) -> tuple[Any, Any]:
    """The body model and shape for a kind, or for an operation's reply (``<operation>.reply``)."""

    spec, is_reply = _kind_spec(kind)
    if spec is None:
        raise FrameRefused("unknown_kind", kind, at="/kind")
    return (spec.reply_model, spec.reply_shape) if is_reply else (spec.body_model, spec.body_shape)


def _parse(data: bytes) -> tuple[dict[str, Any], Frame, bool]:
    try:
        raw = parse(data)
        newer_minor = _newer_minor(raw)
        frame = Frame.model_validate(prune(raw, FRAME_SHAPE) if newer_minor else raw)
    except CanonicalJsonError as error:
        raise FrameRefused("malformed", str(error)) from error
    except ValidationError as error:
        raise FrameRefused("malformed", str(error).splitlines()[0], at=pointer(error.errors()[0], FRAME_SHAPE)) from error
    return raw, frame, newer_minor


def _issuer_certificate(frame: Frame, trust: TrustStore, issued_at: datetime) -> HostCertificate:
    issuer = frame.issuer
    if issuer.installation != trust.installation and trust.agreement(issuer.installation) is None:
        raise FrameRefused("unknown_key", f"no agreement with installation {issuer.installation}", at="/issuer/installation")
    certificate = trust.host_certificate(issuer.installation, issuer.runtime, issuer.membership_epoch)
    if certificate is None:
        raise FrameRefused("unknown_key", "no certificate for the issuer's installation, runtime and membership epoch",
                           at="/issuer/membership_epoch")
    if certificate.key_id != issuer.key_id:
        raise FrameRefused("unknown_key", "the issuer's certificate names another key", at="/issuer/key_id")
    if not certificate.valid_at(issued_at):
        raise FrameRefused("unknown_key", "the issuer's certificate was not valid when the frame was issued", at="/issued_at")
    return certificate


def _verify_signature(raw: Mapping[str, Any], frame: Frame, certificate: HostCertificate) -> None:
    try:
        certificate.public_key.verify(b64url_decode(frame.signature), signing_bytes(raw))
    except (InvalidSignature, ValueError) as error:
        raise FrameRefused("bad_signature", at="/signature") from error


def _session(frame: Frame, trust: TrustStore) -> Optional[AgentSession]:
    if frame.issuer.session is MISSING:
        return None
    return trust.agent_session(frame.issuer.installation, frame.issuer.team, frame.issuer.session)


def _verify_revocation(certificate: HostCertificate, session: Optional[AgentSession], trust: TrustStore) -> None:
    # A revoked certificate stops every frame not yet admitted, whatever issue time
    # the frame gives: the sender chooses that time.
    if trust.revoked(certificate.installation, certificate.certificate_id):
        raise FrameRefused("revoked", "the issuer's certificate is revoked", at="/issuer/key_id")
    if session is not None and trust.revoked(session.signer.installation, session.signer.certificate_id):
        raise FrameRefused("revoked", "the certificate that signed the agent session is revoked", at="/issuer/session")


def _verify_epochs(certificate: HostCertificate, trust: TrustStore) -> None:
    membership, revocation = trust.minimum_epochs(certificate.installation, certificate.runtime)
    if certificate.membership_epoch < membership:
        raise FrameRefused("epoch_too_old", "the issuer's membership epoch is below the receiver's minimum",
                           at="/issuer/membership_epoch")
    if certificate.revocation_epoch < revocation:
        raise FrameRefused("epoch_too_old", "the issuer's revocation epoch is below the receiver's minimum",
                           at="/issuer/revocation_epoch")


def _verify_issuer(frame: Frame, certificate: HostCertificate, session: Optional[AgentSession], issued_at: datetime,
                   is_reply: bool, answering: Optional[Frame]) -> None:
    """The frame's issuer fields agree with its certificate and session; a reply comes from the agent asked."""

    issuer = frame.issuer
    if issuer.revocation_epoch != certificate.revocation_epoch:
        raise FrameRefused("wrong_issuer", "the revocation epoch differs from the certificate's", at="/issuer/revocation_epoch")
    if issuer.team not in certificate.teams:
        raise FrameRefused("wrong_issuer", "the certificate does not serve the issuer's team", at="/issuer/team")
    if (issuer.agent is MISSING) != (issuer.session is MISSING):
        raise FrameRefused("wrong_issuer", "a frame names an agent and its session together", at="/issuer/session")
    if issuer.agent is not MISSING:
        if session is None:
            raise FrameRefused("wrong_issuer", "no such agent session", at="/issuer/session")
        if (session.agent, session.runtime) != (issuer.agent, issuer.runtime):
            raise FrameRefused("wrong_issuer", "the session places another agent or host", at="/issuer/agent")
        if not session.covers(issued_at):
            raise FrameRefused("wrong_issuer", "the session did not cover the frame's issue time", at="/issued_at")
        if session.agent_type not in certificate.agent_types:
            raise FrameRefused("wrong_issuer", "the host's certificate does not allow the agent's type", at="/issuer/agent")
    if not is_reply:
        return
    if answering is None:
        raise FrameRefused("wrong_issuer", "a reply is checked against the request it answers", at="/causation_id")
    if frame.kind != answering.kind + REPLY_SUFFIX or frame.causation_id != answering.message_id:
        raise FrameRefused("wrong_issuer", "the reply answers another request", at="/causation_id")
    asked = answering.audience
    if (issuer.installation, issuer.team) != (asked.installation, asked.team):
        raise FrameRefused("wrong_issuer", "the reply comes from another team than the one asked", at="/issuer/team")
    if asked.target == "agent" and issuer.agent != asked.id:
        # Results and answers are accepted only from the agent that holds the work.
        raise FrameRefused("wrong_issuer", "the reply comes from another agent than the one asked", at="/issuer/agent")


def _verify_sender(frame: Frame, certificate: HostCertificate, spec: Optional[KindSpec], is_reply: bool,
                   trust: TrustStore, issued_at: datetime) -> None:
    if spec is None:
        return  # an unknown kind is refused later, as unknown_kind
    allowed = spec.receivers if is_reply else spec.senders
    if not certificate.roles & set(allowed):
        raise FrameRefused("wrong_sender", f"{frame.kind} comes only from {', '.join(allowed)} hosts", at="/kind")
    if frame.issuer.installation == trust.installation:
        return
    agreement = trust.agreement(frame.issuer.installation)
    assert agreement is not None  # _issuer_certificate refused installations without one
    if is_reply or spec.delivery_class not in BETWEEN_INSTALLATIONS or frame.kind not in agreement.kinds:
        raise FrameRefused("wrong_sender", f"the agreement with {agreement.peer} does not allow {frame.kind}", at="/kind")
    if frame.audience.team not in agreement.teams:
        raise FrameRefused("wrong_sender", f"the agreement with {agreement.peer} does not allow this team", at="/audience/team")
    if not agreement.covers(issued_at):
        raise FrameRefused("wrong_sender", "the agreement did not cover the frame's issue time", at="/issued_at")


def _verify_audience(frame: Frame, receiver: Receiver) -> None:
    audience = frame.audience
    if audience.installation != receiver.installation:
        raise FrameRefused("wrong_team", "the frame is for another installation", at="/audience/installation")
    if audience.team != receiver.team:
        raise FrameRefused("wrong_team", at="/audience/team")
    if audience.target != "team" and (audience.target, audience.id) != (receiver.target, receiver.id):
        raise FrameRefused("wrong_audience", at="/audience/id")
    if audience.runtime is not MISSING and audience.runtime != receiver.runtime:
        raise FrameRefused("wrong_audience", "the frame names another receiving host", at="/audience/runtime")


def _verify_subject(frame: Frame, spec: Optional[KindSpec], is_reply: bool, arrived_on: str,
                    headers: Optional[Mapping[str, str]]) -> None:
    audience, issuer = frame.audience, frame.issuer
    if is_reply:
        inbox = REPLY_INBOX_PREFIX.format(runtime=audience.runtime) + "."
        if audience.runtime is MISSING or frame.causation_id is MISSING or not frame.subject.startswith(inbox):
            raise FrameRefused("subject_mismatch", "a reply names its request and the audience runtime's inbox", at="/subject")
    else:
        local = issuer.installation == audience.installation
        slot = issuer.runtime if local else PEER_SENDER_SLOT.format(installation=issuer.installation)
        prefix = SUBJECT_PREFIX.format(team=audience.team, sender=slot) + "."
        if not frame.subject.startswith(prefix):
            raise FrameRefused("subject_mismatch", "the subject names the audience's team and the sender's slot", at="/subject")
        if spec is not None:
            if audience.target != spec.audience_target:
                raise FrameRefused("subject_mismatch", f"{frame.kind} is for a {spec.audience_target} audience",
                                   at="/audience/target")
            if spec.audience_target in TEAM_WIDE_TARGETS and audience.id != audience.team:
                raise FrameRefused("subject_mismatch", "a team-wide audience names its team", at="/audience/id")
            if frame.subject != prefix + spec.address.format(agent=audience.id, pool=audience.id, edge=audience.id):
                raise FrameRefused("subject_mismatch", "the address must match the kind and audience", at="/subject")
    if arrived_on != frame.subject:
        raise FrameRefused("subject_mismatch", "the frame arrived on another subject", at="/subject")
    if headers is not None:
        _verify_headers(frame, spec, is_reply, headers)


def _verify_headers(frame: Frame, spec: Optional[KindSpec], is_reply: bool, headers: Mapping[str, str]) -> None:
    """NATS headers copy frame fields for the transport; a copy that disagrees is refused."""

    expected = {HEADER_KIND: (frame.kind, "/kind"), HEADER_ID: (frame.message_id, "/message_id")}
    durable = spec is not None and not is_reply and spec.delivery_class == DURABLE
    if durable or HEADER_MSG_ID in headers:
        expected[HEADER_MSG_ID] = (frame.idempotency_key, "/idempotency_key")
    for name, (value, at) in expected.items():
        if headers.get(name) != value:
            raise FrameRefused("subject_mismatch", f"the {name} header must copy the frame", at=at)


def _verify_time(frame: Frame, spec: Optional[KindSpec], now: datetime, issued_at: datetime) -> None:
    """Both ends of the window allow the clock tolerance (transport.yaml clock_skew_seconds).

    Every comparison is between differences, so a timestamp near the end of year
    9999 cannot overflow datetime.
    """

    if now - parse_timestamp(frame.expires_at) >= SKEW:
        raise FrameRefused("expired", at="/expires_at")
    # The receiver's registry caps the sender's expiry, so a sender cannot outlive
    # the replay record.
    if spec is not None and now - issued_at >= timedelta(seconds=spec.expiry_seconds) + SKEW:
        raise FrameRefused("expired", f"{frame.kind} lasts {spec.expiry_seconds} seconds", at="/issued_at")
    if issued_at - now > SKEW:
        raise FrameRefused("not_yet_valid", at="/issued_at")
    if frame.not_before is not MISSING and parse_timestamp(frame.not_before) - now > SKEW:
        raise FrameRefused("not_yet_valid", at="/not_before")


def _verify_authority(raw: Mapping[str, Any], frame: Frame, spec: KindSpec, is_reply: bool,
                      trust: TrustStore) -> Optional[VerifiedAuthority]:
    mode = "none" if is_reply else spec.authority
    token = frame.authority
    if token is MISSING:
        if mode == "required":
            raise FrameRefused("bad_authority", f"{frame.kind} requires an authority token", at="/authority")
        return None
    if mode == "none":
        raise FrameRefused("bad_authority", f"{frame.kind} takes no authority token", at="/authority")
    issuer, audience = token.issuer, token.audience
    if not issuer.installation == audience.installation == frame.issuer.installation:
        raise FrameRefused("bad_authority", "a token travels only within the installation that issued it",
                           at="/authority/issuer/installation")
    if audience.team != frame.issuer.team:
        raise FrameRefused("bad_authority", "only the team the person asked presents the token", at="/authority/audience/team")
    certificate = trust.host_certificate(issuer.installation, issuer.runtime, issuer.membership_epoch)
    token_issued, token_expires = parse_timestamp(token.issued_at), parse_timestamp(token.expires_at)
    if (certificate is None or certificate.key_id != issuer.key_id or not certificate.valid_at(token_issued)
            or trust.revoked(certificate.installation, certificate.certificate_id)):
        raise FrameRefused("bad_authority", "no unrevoked certificate for the token's issuer", at="/authority/issuer")
    if not certificate.roles & AUTHORITY_ISSUER_ROLES or audience.team not in certificate.teams:
        raise FrameRefused("bad_authority", "only a gateway or scheduler of the team issues its tokens", at="/authority/issuer")
    try:
        # Over the token as received: a newer minor's unknown fields are signed too.
        certificate.public_key.verify(b64url_decode(token.signature), authority_signing_bytes(raw["authority"]))
    except (InvalidSignature, ValueError) as error:
        raise FrameRefused("bad_authority", "the token's signature does not verify", at="/authority/signature") from error
    if token_expires - token_issued > timedelta(seconds=AUTHORITY_TOKEN_MAX_SECONDS):
        raise FrameRefused("bad_authority", "the token lasts too long", at="/authority/expires_at")
    if not within(token_issued, token_expires, parse_timestamp(frame.issued_at)):
        raise FrameRefused("bad_authority", "the token was not valid when the frame was issued", at="/authority/expires_at")
    return VerifiedAuthority(token=token, certificate=certificate)


def verify_frame(
    data: bytes | bytearray | memoryview,
    *,
    trust: TrustStore,
    receiver: Receiver,
    arrived_on: str,
    now: datetime,
    replay: ReplayLookup,
    headers: Optional[Mapping[str, str]] = None,
    answering: Optional[Frame] = None,
) -> VerifiedFrame:
    """Check one received frame in the registry's order; raise ``FrameRefused`` on the first failure.

    ``headers`` are the NATS headers the frame arrived with, ``{}`` when it arrived
    with none (nats-py reports that as None), or None only for a frame read from
    storage. ``answering`` is the request a reply answers; replies are refused
    without it. ``now`` must name its time zone.
    """

    if isinstance(data, str):
        raise TypeError("frames are verified as the bytes received")
    if now.tzinfo is None:
        raise ValueError("now must name its time zone")
    if trust.installation != receiver.installation:
        raise ValueError("the trust store belongs to another installation than the receiver")
    data = bytes(data)
    if len(data) > MAX_FRAME_BYTES:
        raise FrameRefused("too_large", f"{len(data)} bytes")
    raw, frame, newer_minor = _parse(data)
    issued_at = parse_timestamp(frame.issued_at)
    spec, is_reply = _kind_spec(frame.kind)
    certificate = _issuer_certificate(frame, trust, issued_at)
    _verify_signature(raw, frame, certificate)
    if body_digest(raw["body"]) != frame.body_digest:
        raise FrameRefused("bad_digest", at="/body_digest")
    session = _session(frame, trust)
    _verify_revocation(certificate, session, trust)
    _verify_epochs(certificate, trust)
    _verify_issuer(frame, certificate, session, issued_at, is_reply, answering)
    _verify_sender(frame, certificate, spec, is_reply, trust, issued_at)
    _verify_audience(frame, receiver)
    _verify_subject(frame, spec, is_reply, arrived_on, headers)
    _verify_time(frame, spec, now, issued_at)
    replay_key = ReplayKey(frame.issuer.installation, frame.issuer.runtime, frame.issuer.membership_epoch, frame.message_id)
    digest = frame_digest(raw)
    earlier = replay(replay_key)
    if earlier is not None and earlier != digest:
        raise FrameRefused("replayed", "this message id was admitted with other content", at="/message_id")
    if spec is None:
        raise FrameRefused("unknown_kind", frame.kind, at="/kind")
    model, shape = body_model(frame.kind)
    try:
        body = model.validate_python(prune(raw["body"], shape) if newer_minor else raw["body"])
    except ValidationError as error:
        raise FrameRefused("invalid_body", str(error).splitlines()[0],
                           at=pointer(error.errors()[0], shape, prefix="/body")) from error
    authority = _verify_authority(raw, frame, spec, is_reply, trust)
    return VerifiedFrame(frame=frame, body=body, certificate=certificate, session=session, authority=authority,
                         replay_key=replay_key, frame_digest=digest, raw=raw)

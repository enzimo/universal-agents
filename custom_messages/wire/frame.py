"""Sign and verify protocol 1 frames (contracts/messages/v1).

A frame is signed with Ed25519 over ``SIGNING_DOMAIN`` followed by the canonical
JSON of the frame without its signature. ``verify_frame`` runs the receiver's
checks in the registry's order and refuses with the first failing code, so every
implementation refuses the same frame the same way.

Who sent a frame is decided only by the receiver's ``TrustStore``: the host
certificate for the issuer's installation, runtime and membership epoch, and the
agent session the frame names (README, "Who sent it"). Nothing the frame says about
its sender counts until those records confirm it.

A join or renewal from a host on another pre-release contract (README, "Versions") is
read for its contract digest before the rest of its body, so that host learns
``protocol_mismatch`` instead of a refusal of a body another contract shaped:
``VerifiedFrame.other_contract`` marks it, like ``TrustStore.verify`` marks a host record.

Verification has no side effects. A receiver that admits a frame records
``VerifiedFrame.replay_key`` with ``VerifiedFrame.frame_digest`` in the same
transaction as the admission, so a redelivery is recognised and other content under
the same id is refused as ``replayed``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import MISSING, ValidationError

from custom_messages.wire.canonical import CanonicalJsonError, canonical_bytes, parse
from custom_messages.wire.encoding import b64url, b64url_decode, body_digest, format_timestamp, signed_bytes
from custom_messages.wire.kinds import (
    AUTHORITY_TOKEN_DOMAIN,
    AUTHORITY_TOKEN_MAX_SECONDS,
    COMMAND_TOKEN_MAX_SECONDS,
    BETWEEN_INSTALLATIONS,
    FRAME_SHAPE,
    INSTALLATION_ADDRESS_PREFIX,
    INSTALLATION_SCOPE,
    KINDS,
    MAX_FRAME_BYTES,
    PEER_SENDER_SLOT,
    PROTOCOL,
    RENEW_KEY_PROOF_DOMAIN,
    REPLY_INBOX_PREFIX,
    SIGNING_DOMAIN,
    SUBJECT_PREFIX,
)
from custom_messages.wire.models import AuthorityToken, Frame
from custom_messages.wire.refusals import FrameRefused, refused_by
from custom_messages.wire.spec import KindSpec, newer_minor, prune
from custom_messages.wire.trust import (
    RESERVED_RUNTIME_PREFIXES,
    SKEW,
    AgentSession,
    HostCertificate,
    TrustStore,
    ed25519_public_key,
    within,
)
from custom_messages.wire.values import parse_timestamp

__all__ = [
    "FrameRefused", "Receiver", "ReplayKey", "ReplayLookup", "VerifiedAuthority", "VerifiedFrame",
    "answer_subject", "b64url", "b64url_decode", "body_digest", "body_model", "expected_idempotency_key", "format_timestamp",
    "frame_digest", "idempotency_key", "renew_key_proof_bytes", "sign_authority_token", "sign_frame",
    "sign_renew_key_proof", "signing_bytes", "verify_authority_token", "verify_frame",
]

REPLY_SUFFIX = ".reply"
# Hosts whose certificates may sign authority tokens (README, "On behalf of a person").
AUTHORITY_ISSUER_ROLES = frozenset({"gateway", "scheduler"})
# The only role whose certificate may sign a token naming a schedule, and so the only
# one that signs a token naming no person: system work, which a schedule runs.
SCHEDULE_ISSUER_ROLE = "scheduler"
# Where a frame carries its authority token.
AUTHORITY_AT = "/authority"
# The token type bound to one command; it lasts at most COMMAND_TOKEN_MAX_SECONDS.
COMMAND_BINDING = "command"
# The host role that speaks for outside agents (A2A); its frames reach only the lead.
OUTSIDE_ROLE = "edge_adapter"
# Who speaks for a team beyond it, to an outside agent or another installation: the lead
# agent, or the team services without an agent.
LEAD_ROLE, SERVICES_ROLE = "lead", "team_services"
# Audience targets an address delivers to as a whole team (audience id is the team id).
TEAM_WIDE_TARGETS = frozenset({"team", "team_services"})
# Replies to a request a host sent for no agent (sessions.*) name the host itself: its
# runtime id is both the audience id and the audience runtime. No request goes to a host.
HOST_TARGET = "host"
# NATS headers that copy frame fields (README, "NATS headers").
HEADER_KIND, HEADER_ID, HEADER_MSG_ID = "UA-Kind", "UA-Id", "Nats-Msg-Id"
DURABLE = "durable"
# Classes whose frames wait in a stream: judged by the certificate valid when they
# were issued, so the receiver's minimum epochs do not apply to them.
QUEUED_CLASSES = frozenset({"durable", "dead_letter"})
# Subjects start with this root, then the team token (transport.yaml subject_prefix).
# Installation-wide traffic (joining, renewing) names INSTALLATION_SCOPE as its team, and
# its address (_install.{sender}.svc) follows the root directly.
SUBJECT_ROOT = SUBJECT_PREFIX.split("{team}", 1)[0]
# A join is signed by the key it presents, since its host has no certificate yet.
PROOF_OF_POSSESSION = "proof_of_possession"
_HOST_RENEW = "host.renew"
_KEY_ROTATION = "application_key_rotation"
# Where a join or renewal reply carries the certificate the authority issued.
ISSUED_CERTIFICATE_AT = "/body/certificate"
# Where a sessions.open or sessions.renew reply carries the session the team services signed.
ISSUED_SESSION_AT = "/body/session"
_SESSIONS_OPEN, _SESSIONS_RENEW = "sessions.open", "sessions.renew"


def idempotency_key(kind: str, *parts: str | int | None) -> str:
    """A bounded key for a tuple of values; JSON preserves their boundaries and order.

    ``kind`` is a registry kind (at most 84 characters), and each part is a value
    the registry's ``idempotency`` list names, ``None`` where an optional field is
    absent. The key is at most 128 characters, whatever the ids' lengths
    (README, "Idempotency keys").
    """

    digest = hashlib.sha256(canonical_bytes(list(parts))).digest()
    return f"{kind}:{b64url(digest)}"


def _pointed(document: Mapping[str, Any], pointer: str) -> Any:
    """The value a JSON pointer names, or None when a step is absent."""

    value: Any = document
    for step in pointer[1:].split("/"):
        if not isinstance(value, Mapping) or step not in value:
            return None
        value = value[step]
    return value


def expected_idempotency_key(raw: Mapping[str, Any], spec: KindSpec, is_reply: bool,
                             answering: Optional[Frame]) -> str:
    """The key a frame of this kind must carry (README, "Idempotency keys").

    A request/reply operation's reply derives its key from the request's key. A
    durable copy (registry ``copy_to``) adds its audience target as a last part, so
    the broker, which drops repeats of one key across the whole stream, keeps both
    the original and the copy.
    """

    if is_reply:
        assert answering is not None  # _verify_issuer refused replies without their request
        return idempotency_key(f"{spec.name}{REPLY_SUFFIX}", answering.idempotency_key)
    parts = [_pointed(raw, pointer) for pointer in spec.idempotency]
    target = raw["audience"]["target"]
    if target in spec.copies:
        parts.append(target)
    return idempotency_key(spec.name, *parts)


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


def renew_key_proof_bytes(frame: Mapping[str, Any]) -> bytes:
    """What the new key's proof in a key-rotating ``host.renew`` covers.

    The whole renewal, without the frame's signature, its body digest and the proof
    itself, after RENEW_KEY_PROOF_DOMAIN and a newline. The old key then signs the
    finished frame, proof included, so neither signature depends on the other.
    """

    if frame["kind"] != _HOST_RENEW:
        raise ValueError("a renewal key proof belongs only to host.renew")
    unsigned = {key: value for key, value in frame.items() if key not in {"signature", "body_digest"}}
    unsigned["algorithm"] = "ed25519"
    body = dict(frame["body"])
    body[_KEY_ROTATION] = {key: value for key, value in body[_KEY_ROTATION].items() if key != "proof"}
    unsigned["body"] = body
    return RENEW_KEY_PROOF_DOMAIN.encode("ascii") + b"\n" + canonical_bytes(unsigned)


def sign_renew_key_proof(frame: Mapping[str, Any], new_private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Fill the new key's proof; then sign the frame with the old key (``sign_frame``)."""

    body = dict(frame["body"])
    body[_KEY_ROTATION] = dict(body[_KEY_ROTATION], proof=b64url(new_private_key.sign(renew_key_proof_bytes(frame))))
    return dict(frame, body=body)


def sign_frame(unsigned: Mapping[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Return ``unsigned`` with its body digest and signature filled in."""

    frame = {key: value for key, value in unsigned.items() if key not in {"signature", "body_digest"}}
    frame["algorithm"] = "ed25519"
    frame["body_digest"] = body_digest(frame["body"])
    frame["signature"] = b64url(private_key.sign(signing_bytes(frame)))
    return frame


@dataclass(frozen=True)
class Receiver:
    """Who is receiving: the audience a frame must name, and the host it runs on.

    ``agent_role`` is the receiving agent's place in its team (``lead`` or
    ``worker``) when the receiver is an agent; only the lead receives frames from
    outside agents. ``outside`` is true for an edge adapter receiving for an outside
    agent, which accepts frames only from the lead and the team services
    (team-topologies.yaml fixed rules).
    """

    installation: str
    team: str
    target: str
    id: str
    runtime: str
    agent_role: Optional[str] = None
    outside: bool = False


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
        """The token is bound to this record, or the frame is refused as ``bad_authority``.

        The receiver calls it for bindings with ``at`` (the body names the record).
        For a binding without ``at`` the handler calls it with the root task it holds
        for the frame, before its access decision.
        """

        if (self.token.bound_to.type, self.token.bound_to.id) != (bound_type, record_id):
            raise FrameRefused("bad_authority", f"the token is not bound to the {bound_type} {record_id}",
                               at="/authority/bound_to")


@dataclass(frozen=True)
class VerifiedFrame:
    """A frame that passed the receiver's checks.

    ``other_contract`` marks a join or renewal whose host runs another pre-release contract
    (README, "Versions"): everything before its body was checked (``verify_frame``), but its
    body was not read, so ``body`` is None and ``their_contract_sha256`` names that contract.
    """

    frame: Frame
    body: Any
    certificate: Optional[HostCertificate]  # None for a join, signed by the key it presents
    session: Optional[AgentSession]
    authority: Optional[VerifiedAuthority]
    replay_key: ReplayKey
    frame_digest: str
    raw: dict[str, Any] = field(repr=False)
    other_contract: bool = False
    their_contract_sha256: Optional[str] = None


def _newer_minor(raw: Mapping[str, Any]) -> bool:
    return newer_minor(raw, PROTOCOL)


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
        raise refused_by("malformed", error, FRAME_SHAPE) from error
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


def _presented_key(raw: Mapping[str, Any], frame: Frame, trust: TrustStore) -> Ed25519PublicKey:
    """A join's key: the one its body presents, which must be the key it names and signs with.

    A key id the trust store already knows must keep its public key, so a join can
    retry after its key became known (to recover a lost reply), but cannot take over
    another host's key id. A revoked known key stops the join.
    """

    issuer = frame.issuer
    if issuer.installation != trust.installation:
        raise FrameRefused("unknown_key", "a host joins only its own installation", at="/issuer/installation")
    presented = raw["body"].get("application_key")
    if not isinstance(presented, dict) or presented.get("key_id") != issuer.key_id:
        raise FrameRefused("unknown_key", "a join names the key it presents", at="/issuer/key_id")
    if trust.names_authority_key(issuer.installation, issuer.key_id):
        raise FrameRefused("unknown_key", "an authority key id is not a host's key", at="/issuer/key_id")
    try:
        public_key = ed25519_public_key(str(presented.get("public_key", "")))
    except ValueError as error:
        raise FrameRefused("unknown_key", "the presented key is not an Ed25519 public key a signature can bind",
                           at="/body/application_key/public_key") from error
    for certificate in trust.certificates_with_key(issuer.installation, issuer.key_id):
        if certificate.public_key.public_bytes_raw() != public_key.public_bytes_raw():
            raise FrameRefused("unknown_key", "a known key id must present the same public key", at="/issuer/key_id")
    return public_key


def _verify_signature(raw: Mapping[str, Any], frame: Frame, public_key: Ed25519PublicKey) -> None:
    try:
        public_key.verify(b64url_decode(frame.signature), signing_bytes(raw))
    except (InvalidSignature, ValueError) as error:
        raise FrameRefused("bad_signature", at="/signature") from error


def _verify_join(raw: Mapping[str, Any], frame: Frame, spec: KindSpec, trust: TrustStore, now: datetime, *,
                 read_body: bool = True) -> None:
    """A join speaks for its own new host only (README, "Installation traffic").

    Its key must not be revoked, by its key id or by its public key under any key id,
    and the receiver needs a current revocations record to know that. It names the installation scope, no agent, epoch zero and an unreserved
    runtime id; its body asks for that runtime in that installation, and only for roles a
    joining host may hold (the kind's senders, which the receiver cannot check against a
    certificate the host does not have yet). The body is not read (``read_body=False``) for
    a join of another contract, except the key it presents, which its signature needed.
    """

    issuer = frame.issuer
    if trust.key_revoked(issuer.installation, issuer.runtime, issuer.key_id) or any(
            trust.revoked(certificate) for certificate in trust.certificates_with_key(issuer.installation, issuer.key_id)):
        raise FrameRefused("revoked", "the presented key's certificate is revoked", at="/issuer/key_id")
    # A key id is only a label: the revoked key itself stays refused under a new one.
    if trust.public_key_revoked(issuer.installation, ed25519_public_key(raw["body"]["application_key"]["public_key"])):
        raise FrameRefused("revoked", "the presented public key belongs to a revoked certificate",
                           at="/body/application_key/public_key")
    if not trust.revocations_current(issuer.installation, now):
        raise FrameRefused("revocations_not_current", "no current revocations record of this installation",
                           at="/issuer/installation")
    if issuer.team != INSTALLATION_SCOPE:
        raise FrameRefused("wrong_issuer", "a join names the installation scope", at="/issuer/team")
    if issuer.agent is not MISSING or issuer.session is not MISSING:
        raise FrameRefused("wrong_issuer", "a join speaks for its host, not an agent", at="/issuer/agent")
    for field_name in ("membership_epoch", "revocation_epoch"):
        if getattr(issuer, field_name) != 0:
            raise FrameRefused("wrong_issuer", "a host joins at epoch zero", at=f"/issuer/{field_name}")
    if issuer.runtime.startswith(RESERVED_RUNTIME_PREFIXES):
        raise FrameRefused("wrong_issuer", "runtime ids starting with x- or _ are reserved", at="/issuer/runtime")
    if not read_body:
        return
    body = raw["body"]
    for field_name in ("runtime", "installation"):
        if body.get(field_name) != getattr(issuer, field_name):
            raise FrameRefused("wrong_issuer", f"a join asks for its own {field_name}", at=f"/body/{field_name}")
    roles = body.get("roles")
    if isinstance(roles, list) and not all(isinstance(role, str) and role in spec.senders for role in roles):
        raise FrameRefused("wrong_sender", f"a joining host asks only for {', '.join(spec.senders)} roles", at="/body/roles")


def _installation_operation(spec: Optional[KindSpec]) -> bool:
    """Whether the kind is installation-wide (joining, renewing): its address sits in the installation scope."""

    return spec is not None and any(address.startswith(INSTALLATION_ADDRESS_PREFIX) for address in spec.addresses.values())


def _session(frame: Frame, trust: TrustStore) -> Optional[AgentSession]:
    if frame.issuer.session is MISSING:
        return None
    return trust.agent_session(frame.issuer.installation, frame.issuer.team, frame.issuer.session)


def _verify_revocation(frame: Frame, certificate: HostCertificate, session: Optional[AgentSession], trust: TrustStore,
                       now: datetime) -> None:
    """Neither certificate is revoked, and the receiver's revocations record of the issuer's installation is current.

    A revoked certificate, or key, stops every frame not yet admitted, whatever issue
    time the frame gives: the sender chooses that time. The session's signer belongs to
    the issuer's installation, so one current record covers both. Only installation
    traffic (the installation scope) from the receiver's own authority needs no current
    record, so a host whose record expired can still renew.
    """

    if trust.revoked(certificate):
        raise FrameRefused("revoked", "the issuer's certificate is revoked", at="/issuer/key_id")
    if session is not None and trust.revoked(session.signer):
        raise FrameRefused("revoked", "the certificate that signed the agent session is revoked", at="/issuer/session")
    trust.require_current_revocations(certificate, now, at="/issuer/installation",
                                      installation_traffic=frame.issuer.team == INSTALLATION_SCOPE)


def _verify_epochs(certificate: HostCertificate, spec: Optional[KindSpec], trust: TrustStore) -> None:
    if spec is not None and spec.delivery_class in QUEUED_CLASSES:
        return  # queued work is judged by the certificate valid when it was issued
    membership, revocation = trust.minimum_epochs(certificate.installation, certificate.runtime)
    if certificate.membership_epoch < membership:
        raise FrameRefused("epoch_too_old", "the issuer's membership epoch is below the receiver's minimum",
                           at="/issuer/membership_epoch")
    if certificate.revocation_epoch < revocation:
        raise FrameRefused("epoch_too_old", "the issuer's revocation epoch is below the receiver's minimum",
                           at="/issuer/revocation_epoch")


def _verify_issuer(frame: Frame, spec: Optional[KindSpec], certificate: HostCertificate, session: Optional[AgentSession],
                   issued_at: datetime, is_reply: bool, answering: Optional[Frame]) -> None:
    """The frame's issuer fields agree with its certificate and session; a reply comes from the agent asked.

    Installation operations and their replies speak for a host in the installation
    scope; every other kind names a team the certificate serves (team ids never start
    with ``_``, so no certificate serves the installation scope).
    """

    issuer = frame.issuer
    if issuer.revocation_epoch != certificate.revocation_epoch:
        raise FrameRefused("wrong_issuer", "the revocation epoch differs from the certificate's", at="/issuer/revocation_epoch")
    if _installation_operation(spec):
        if issuer.team != INSTALLATION_SCOPE:
            raise FrameRefused("wrong_issuer", "an installation operation names the installation scope", at="/issuer/team")
        if issuer.agent is not MISSING or issuer.session is not MISSING:
            raise FrameRefused("wrong_issuer", "an installation operation speaks for its host, not an agent", at="/issuer/agent")
    elif issuer.team not in certificate.teams:
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


def _other_contract(raw: Mapping[str, Any], spec: Optional[KindSpec], is_reply: bool, trust: TrustStore) -> Optional[str]:
    """The contract digest a join or renewal names when it is not the receiver's own (README, "Versions").

    Only requests whose body carries the host's digest (``contract_sha256``: joins and
    renewals) are read this way; it is read on its own, as ``TrustStore.other_contract``
    reads a host record's.
    """

    if is_reply or spec is None or "contract_sha256" not in spec.body_shape.get("properties", {}):
        return None
    return trust.other_contract(raw.get("body"))


def _verify_renewal(raw: Mapping[str, Any], frame: Frame, certificate: HostCertificate) -> None:
    """A renewal renews the certificate that signs it: its body names that certificate and epoch."""

    if frame.kind != _HOST_RENEW:
        return
    for field_name, value in (("certificate_id", certificate.certificate_id), ("membership_epoch", certificate.membership_epoch)):
        named = raw["body"].get(field_name)
        if type(named) is not type(value) or named != value:
            raise FrameRefused("wrong_issuer", "a renewal names the certificate that signs it", at=f"/body/{field_name}")


def _verify_sender(frame: Frame, certificate: HostCertificate, session: Optional[AgentSession], spec: Optional[KindSpec],
                   is_reply: bool, trust: TrustStore, issued_at: datetime, receiver: Receiver) -> None:
    if spec is None:
        return  # an unknown kind is refused later, as unknown_kind
    target = frame.audience.target
    allowed = spec.receivers if is_reply else spec.senders_by_target.get(target, spec.senders)
    roles = certificate.roles & set(allowed)
    if not roles:
        raise FrameRefused("wrong_sender", f"{frame.kind} to a {target} comes only from {', '.join(allowed)} hosts", at="/kind")
    _verify_outside_boundary(frame, roles, session, receiver)
    if frame.issuer.installation == trust.installation:
        # Within an installation a host speaks inside the team it names; reaching
        # another team needs an agreement between teams, which this contract does not define yet.
        if frame.audience.team != frame.issuer.team:
            raise FrameRefused("wrong_sender", "a frame stays within its issuer's team", at="/audience/team")
        return
    agreement = trust.agreement(frame.issuer.installation)
    assert agreement is not None  # _issuer_certificate refused installations without one
    if is_reply or spec.delivery_class not in BETWEEN_INSTALLATIONS or frame.kind not in agreement.kinds:
        raise FrameRefused("wrong_sender", f"the agreement with {agreement.peer} does not allow {frame.kind}", at="/kind")
    if frame.audience.team not in agreement.teams:
        raise FrameRefused("wrong_sender", f"the agreement with {agreement.peer} does not allow this team", at="/audience/team")
    if not agreement.covers(issued_at):
        raise FrameRefused("wrong_sender", "the agreement did not cover the frame's issue time", at="/issued_at")
    if not _speaks_for_team(roles, session):
        raise FrameRefused("wrong_sender", "between teams only the lead or the team services talk", at="/issuer")


def _speaks_for_team(roles: frozenset[str] | set[str], session: Optional[AgentSession]) -> bool:
    """Whether the frame comes from the lead agent, or from the team services speaking for no agent."""

    lead = session is not None and session.agent_role == LEAD_ROLE
    services = session is None and SERVICES_ROLE in roles
    return lead or services


def _verify_outside_boundary(frame: Frame, roles: frozenset[str] | set[str], session: Optional[AgentSession], receiver: Receiver) -> None:
    """Contact with outside agents goes through the lead (team-topologies.yaml fixed rules).

    An outside agent's adapter reaches only the lead's inbox (or the team services),
    and an adapter receiving for an outside agent accepts frames only from a lead
    agent or the team services.
    """

    if roles == {OUTSIDE_ROLE} and not (frame.audience.target == "team_services"
                                       or (frame.audience.target == "agent" and receiver.agent_role == LEAD_ROLE)):
        raise FrameRefused("wrong_sender", "an outside agent reaches only the team's lead", at="/audience/id")
    if receiver.outside and not _speaks_for_team(roles, session):
        raise FrameRefused("wrong_sender", "only the lead or the team services contact an outside agent", at="/issuer")


def reply_audience(request: Frame) -> dict[str, str]:
    """Whom the reply to ``request`` names (README, "Replies"): the requester as it receives.

    The agent, when the request names one; the requesting runtime as the installation
    service's caller, for an installation operation; otherwise the requesting host
    itself. The reply goes to that runtime's inbox in every case.
    """

    issuer = request.issuer
    base = {"installation": issuer.installation, "team": issuer.team, "runtime": issuer.runtime}
    if issuer.agent is not MISSING:
        return {**base, "target": "agent", "id": str(issuer.agent)}
    if issuer.team == INSTALLATION_SCOPE:
        return {**base, "target": "installation_service", "id": issuer.runtime}
    return {**base, "target": HOST_TARGET, "id": issuer.runtime}


# A sender slot or runtime id: one subject token of id characters (common.schema.json ids).
_SLOT_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
# A NATS subject token that is not a wildcard and holds no whitespace.
_PLAIN_TOKEN = re.compile(r"[^\s*>]+")
# Tokens of the subject root (ua.v1); the team token and the sender slot follow it.
_ROOT_TOKENS = len(SUBJECT_ROOT.rstrip(".").split("."))


def answer_subject(request_subject: str, reply_subject: Optional[str]) -> Optional[str]:
    """The NATS subject to answer a request on, or ``None`` when the receiver must not answer (README, "Replies").

    NATS ``allow_responses`` lets a receiver publish one answer to whatever reply subject
    the request names, past the receiver's own grants. A requester could otherwise name a
    JetStream API or key-value subject and have the answer, even an empty refusal,
    written there with the receiver's rights (an empty message on
    ``$JS.API.STREAM.DELETE.<stream>`` deletes the stream). So a receiver answers, with a
    signed reply or a refusal, only on the requester's own inbox:
    ``_INBOX.<sender slot>.<token>...``, with at least one token after the slot and no
    wildcard, where the sender slot is the one in the subject the request arrived on
    (``ua.v1.<team>.<slot>.`` or ``ua.v1._install.<slot>.svc``). The requester's NATS
    publish permissions fix that slot to its own runtime id, so it can only name its
    own inbox. A peer installation's slot (``x-<installation>``) gets no answer, since
    request/reply stays inside one installation. A receiver that gets ``None`` drops the
    request without answering and logs it.
    """

    if not reply_subject or not request_subject.startswith(SUBJECT_ROOT):
        return None
    tokens = request_subject.split(".")
    if len(tokens) < _ROOT_TOKENS + 3:  # the team, the sender slot and at least one address token
        return None
    slot = tokens[_ROOT_TOKENS + 1]
    if not _SLOT_TOKEN.fullmatch(slot) or slot.startswith(RESERVED_RUNTIME_PREFIXES):
        return None
    inbox = REPLY_INBOX_PREFIX.format(runtime=slot) + "."
    if not reply_subject.startswith(inbox):
        return None
    rest = reply_subject[len(inbox):].split(".")
    if not all(_PLAIN_TOKEN.fullmatch(token) for token in rest):
        return None
    return reply_subject


def _verify_audience(frame: Frame, receiver: Receiver, answering: Optional[Frame] = None) -> None:
    if answering is not None:
        expected = reply_audience(answering)
        named = frame.audience
        for field_name in ("target", "id", "runtime"):
            if getattr(named, field_name) != expected[field_name]:
                raise FrameRefused("wrong_audience", "a reply names its requester as it receives (README, \"Replies\")",
                                   at=f"/audience/{field_name}")
    audience = frame.audience
    if audience.installation != receiver.installation:
        raise FrameRefused("wrong_team", "the frame is for another installation", at="/audience/installation")
    if audience.team != receiver.team:
        raise FrameRefused("wrong_team", at="/audience/team")
    if audience.target != "team" and (audience.target, audience.id) != (receiver.target, receiver.id):
        raise FrameRefused("wrong_audience", at="/audience/id")
    if audience.runtime is not MISSING and audience.runtime != receiver.runtime:
        raise FrameRefused("wrong_audience", "the frame names another receiving host", at="/audience/runtime")


def _expected_subject(address: str, team: str, audience_id: str, slot: str, body: Mapping[str, Any]) -> str:
    """The subject a kind's address gives for this audience and sender slot.

    ``{agent}``, ``{pool}`` and ``{edge}`` are the audience id and ``{copy}`` the body's
    copy_id. An installation address names its scope and sender itself; other
    addresses follow ``ua.v1.<audience team>.<sender slot>.``.
    """

    filled = address.format(agent=audience_id, pool=audience_id, edge=audience_id, sender=slot,
                            copy=str(body.get("copy_id")))
    if address.startswith(INSTALLATION_ADDRESS_PREFIX):
        return SUBJECT_ROOT + filled
    return SUBJECT_PREFIX.format(team=team, sender=slot) + "." + filled


def _verify_subject(raw: Mapping[str, Any], frame: Frame, spec: Optional[KindSpec], is_reply: bool, arrived_on: str,
                    headers: Optional[Mapping[str, str]]) -> None:
    audience, issuer = frame.audience, frame.issuer
    if is_reply:
        inbox = REPLY_INBOX_PREFIX.format(runtime=audience.runtime) + "."
        if audience.runtime is MISSING or frame.causation_id is MISSING or not frame.subject.startswith(inbox):
            raise FrameRefused("subject_mismatch", "a reply names its request and the audience runtime's inbox", at="/subject")
        if audience.target == HOST_TARGET and audience.id != audience.runtime:
            raise FrameRefused("subject_mismatch", "a reply to a host names that host as its audience id", at="/audience/id")
    else:
        local = issuer.installation == audience.installation
        slot = issuer.runtime if local else PEER_SENDER_SLOT.format(installation=issuer.installation)
        prefix = SUBJECT_PREFIX.format(team=audience.team, sender=slot) + "."
        if not frame.subject.startswith(prefix):
            raise FrameRefused("subject_mismatch", "the subject names the audience's team and the sender's slot", at="/subject")
        if spec is not None:
            address = spec.addresses.get(audience.target)
            if address is None:
                raise FrameRefused("subject_mismatch", f"{frame.kind} is not delivered to a {audience.target} audience",
                                   at="/audience/target")
            if audience.target in TEAM_WIDE_TARGETS and audience.id != audience.team:
                raise FrameRefused("subject_mismatch", "a team-wide audience names its team", at="/audience/id")
            if frame.subject != _expected_subject(address, audience.team, audience.id, slot, raw["body"]):
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


def _verify_renew_key_proof(raw: Mapping[str, Any], certificate: Optional[HostCertificate], trust: TrustStore) -> None:
    """A renewal that rotates keys names a new key id and proves possession of that key over the whole renewal.

    The key id is new to the installation: not the signing certificate's, not another
    host's, not an authority key. So is the public key: no held certificate holds it
    under another key id, so a revoked key cannot return under a new label. The one
    exception is the key of the certificate that already replaced the signing one, with
    the same public key: what a retry after a lost reply names. The authority answers it
    with that certificate.
    """

    if raw["kind"] != _HOST_RENEW or _KEY_ROTATION not in raw["body"]:
        return
    rotation = raw["body"][_KEY_ROTATION]
    if not isinstance(rotation, dict):
        raise FrameRefused("invalid_body", "application_key_rotation holds a key and a proof", at=f"/body/{_KEY_ROTATION}")
    assert certificate is not None  # a renewal is signed by the certificate it renews
    key = rotation.get("key")
    if isinstance(key, dict) and isinstance(key.get("key_id"), str):
        key_id = key["key_id"]
        used = trust.certificates_with_key(certificate.installation, key_id)
        retried = [other for other in used
                   if other.replaces == certificate.certificate_id and other.runtime == certificate.runtime
                   and b64url(other.public_key.public_bytes_raw()) == key.get("public_key")]
        if (key_id == certificate.key_id or trust.names_authority_key(certificate.installation, key_id)
                or (used and not retried)):
            raise FrameRefused("invalid_body", "a rotation names a key id the installation does not use yet",
                               at=f"/body/{_KEY_ROTATION}/key/key_id")
        try:
            held = trust.certificates_with_public_key(certificate.installation, ed25519_public_key(str(key.get("public_key"))))
        except ValueError:
            held = []  # the proof check below refuses a key that is not one
        if any(other.key_id != key_id for other in held):
            raise FrameRefused("invalid_body", "a rotation names a public key the installation does not use yet",
                               at=f"/body/{_KEY_ROTATION}/key/public_key")
    try:
        public_key = ed25519_public_key(rotation["key"]["public_key"])
        public_key.verify(b64url_decode(rotation["proof"]), renew_key_proof_bytes(raw))
    except (InvalidSignature, ValueError, KeyError, TypeError) as error:
        raise FrameRefused("invalid_body", "the renewal's new-key proof does not verify",
                           at=f"/body/{_KEY_ROTATION}/proof") from error


def _verify_authority(raw: Mapping[str, Any], frame: Frame, spec: KindSpec, is_reply: bool, trust: TrustStore,
                      sender: Optional[HostCertificate], receiver: Receiver, now: datetime,
                      stored_at: Optional[datetime]) -> Optional[VerifiedAuthority]:
    """The frame's token, if any: allowed on the kind at this address, valid on its own, and of a type this sender may present here.

    ``sender`` is None only for a join, which takes no token (registry ``authority: none``).
    A kind's ``authority_by_target`` narrows it for some addresses: a task for a worker
    pool carries no token, since every host running the pool reads its queue. A person's
    token never reaches an outside agent: an adapter receiving for one refuses it.
    Queued work's token must still be valid when the stream stored the frame
    (``stored_at``), other frames' when they are received.
    """

    target = frame.audience.target
    mode = "none" if is_reply or sender is None else spec.authority_by_target.get(target, spec.authority)
    token = frame.authority
    if token is MISSING:
        if mode == "required":
            raise FrameRefused("bad_authority", f"{frame.kind} requires an authority token", at=AUTHORITY_AT)
        return None
    if mode == "none":
        raise FrameRefused("bad_authority", f"{frame.kind} to a {target} takes no authority token", at=AUTHORITY_AT)
    if receiver.outside:
        raise FrameRefused("bad_authority", "a person's token never goes to an outside agent", at=AUTHORITY_AT)
    assert sender is not None  # a frame without a certificate takes no token (mode none above)
    issuer, audience = token.issuer, token.audience
    if not issuer.installation == audience.installation == frame.issuer.installation:
        raise FrameRefused("bad_authority", "a token travels only within the installation that issued it",
                           at="/authority/issuer/installation")
    if audience.team != frame.issuer.team:
        raise FrameRefused("bad_authority", "only the team the person asked presents the token", at="/authority/audience/team")
    # Over the token as received: a newer minor's unknown fields are signed too.
    queued = stored_at is not None and spec.delivery_class in QUEUED_CLASSES
    verified = verify_authority_token(token, raw["authority"], trust, valid_at=parse_timestamp(frame.issued_at), now=now,
                                      reached_at=stored_at if queued else None)
    return _verify_binding(raw, frame, spec, sender, verified)


def verify_authority_token(token: AuthorityToken, fields: Mapping[str, Any], trust: TrustStore, *,
                           valid_at: datetime, now: datetime, reached_at: Optional[datetime] = None,
                           at: str = AUTHORITY_AT) -> VerifiedAuthority:
    """Check an authority token on its own; raise ``FrameRefused`` (``bad_authority``) at ``at`` when it fails.

    ``token`` is the validated token and ``fields`` its JSON object as received,
    which its signature covers. It must be valid both at ``valid_at``, the issue time
    of the frame that carries it, and at ``reached_at``, when the frame reached the
    receiver: for queued work the time the stream stored it, which the server sets and
    the sender cannot, and by default ``now``, the receiver's clock. So a frame dated
    back cannot carry a token that had expired when it was sent, while queued work that
    waits past its token's end is still accepted. Both allow the clock tolerance; the
    receiver still needs a current revocations record at ``now``. The checks (README,
    "On behalf of a person"):
    an unrevoked gateway or scheduler certificate of its audience team signed it;
    only a scheduler names a schedule; a token names its person, unless it is
    system work, which names its schedule instead; it lasts at most
    ``AUTHORITY_TOKEN_MAX_SECONDS``, or ``COMMAND_TOKEN_MAX_SECONDS`` when bound to
    a command. ``verify_frame`` adds the checks that depend on the frame: who
    presents the token, on which kind, and the record the body names.
    """

    signed = _SignedToken(at, fields, authority_signing_bytes(fields))
    certificate = _token_certificate(signed, trust, AUTHORITY_ISSUER_ROLES, token.audience.team, "a gateway or scheduler", now)
    if token.schedule_id is not MISSING and SCHEDULE_ISSUER_ROLE not in certificate.roles:
        raise FrameRefused("bad_authority", "only a scheduler names the schedule a run belongs to", at=at + "/schedule_id")
    if token.person is MISSING and token.schedule_id is MISSING:
        raise FrameRefused("bad_authority", "a token names its person, or for system work its schedule", at=at + "/person")
    command = token.bound_to.type == COMMAND_BINDING
    _verify_token_signature_and_time(signed, certificate, valid_at, now if reached_at is None else reached_at,
                                     COMMAND_TOKEN_MAX_SECONDS if command else AUTHORITY_TOKEN_MAX_SECONDS)
    return VerifiedAuthority(token=token, certificate=certificate)


@dataclass(frozen=True)
class _SignedToken:
    """An authority token as received: where it sits, its fields, and what its signature covers."""

    at: str
    fields: Mapping[str, Any]
    signing_bytes: bytes

    @property
    def issued_at(self) -> datetime:
        return parse_timestamp(self.fields["issued_at"])

    @property
    def expires_at(self) -> datetime:
        return parse_timestamp(self.fields["expires_at"])


def _token_certificate(token: _SignedToken, trust: TrustStore, roles: frozenset[str], team: str,
                       signers: str, now: datetime) -> HostCertificate:
    """The unrevoked certificate, valid when the token was issued, of a host in one of ``roles`` serving ``team``.

    The receiver needs a current revocations record of its installation to know it is unrevoked.
    """

    issuer = token.fields["issuer"]
    certificate = trust.host_certificate(issuer["installation"], issuer["runtime"], issuer["membership_epoch"])
    if (certificate is None or certificate.key_id != issuer["key_id"] or not certificate.valid_at(token.issued_at)
            or trust.revoked(certificate) or not trust.revocations_current(certificate.installation, now)):
        raise FrameRefused("bad_authority", "no certificate known to be unrevoked for the token's issuer",
                           at=token.at + "/issuer")
    if not certificate.roles & roles or team not in certificate.teams:
        raise FrameRefused("bad_authority", f"only {signers} of the team signs this token", at=token.at + "/issuer")
    return certificate


def _verify_token_signature_and_time(token: _SignedToken, certificate: HostCertificate, valid_at: datetime,
                                     reached_at: datetime, max_seconds: int) -> None:
    """The signature verifies, the token lasts at most ``max_seconds``, and it is valid at ``valid_at`` and at ``reached_at``."""

    try:
        certificate.public_key.verify(b64url_decode(token.fields["signature"]), token.signing_bytes)
    except (InvalidSignature, ValueError) as error:
        raise FrameRefused("bad_authority", "the token's signature does not verify", at=token.at + "/signature") from error
    if token.expires_at - token.issued_at > timedelta(seconds=max_seconds):
        raise FrameRefused("bad_authority", f"the token lasts longer than {max_seconds} seconds", at=token.at + "/expires_at")
    if not within(token.issued_at, token.expires_at, valid_at):
        raise FrameRefused("bad_authority", "the token was not valid when the frame was issued", at=token.at + "/expires_at")
    if not within(token.issued_at, token.expires_at, reached_at):
        raise FrameRefused("bad_authority", "the token was not valid when the frame reached the receiver",
                           at=token.at + "/expires_at")


def _verify_binding(raw: Mapping[str, Any], frame: Frame, spec: KindSpec, sender: HostCertificate,
                    verified: VerifiedAuthority) -> VerifiedAuthority:
    """The token is of a type the kind takes, from a host that may present it, and bound to the record its body names.

    ``sender`` is the frame issuer's certificate. A binding's senders narrow, within
    the senders of the frame's address, which hosts present that type: a gateway's
    command token is not accepted from an agent host that received it. Where the
    binding names no body field (``at``), the handler checks the record it holds.
    """

    bound = verified.token.bound_to
    binding = next((item for item in spec.authority_binding if item.type == bound.type), None)
    if binding is None:
        raise FrameRefused("bad_authority", f"{spec.name} takes no token bound to a {bound.type}", at="/authority/bound_to/type")
    if binding.senders is not None:
        presenters = set(binding.senders) & set(spec.senders_by_target.get(frame.audience.target, spec.senders))
        if not sender.roles & presenters:
            allowed = f"only {', '.join(sorted(presenters))} hosts present" if presenters else "no host presents"
            raise FrameRefused("bad_authority", f"{allowed} a token bound to a {bound.type} here",
                               at="/authority/bound_to/type")
    if binding.at is not None:
        verified.require_bound_to(binding.type, str(_pointed(raw, binding.at)))
    return verified


def _repeats(raw: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    items = _pointed(raw, rule["unique"])
    if not isinstance(items, list):
        return False
    by = rule.get("by")
    values = [canonical_bytes(item[by] if by is not None and isinstance(item, Mapping) else item) for item in items]
    return len(set(values)) != len(values)


def _lacks_required(raw: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    applies = all(_pointed(raw, condition["at"]) == condition["is"] for condition in rule["when"])
    return applies and _pointed(raw, rule["requires"]) is None


def _says_nothing(raw: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    return all(_pointed(raw, pointer) is None for pointer in rule["any_of"])


def _holds_other(raw: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    """Whether the field holds a value outside ``values`` (each item, for a list) while every condition holds."""

    if not all(_pointed(raw, condition["at"]) == condition["is"] for condition in rule["when"]):
        return False
    value = _pointed(raw, rule["allowed"])
    if value is None:
        return False
    return any(item not in rule["values"] for item in (value if isinstance(value, list) else [value]))


def _ends_late(raw: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    """Whether the time at ``ends_within`` is more than ``seconds`` after the time at ``of``; an absent time passes.

    Both are timestamps the schema and the frame's own form already checked.
    """

    end, start = _pointed(raw, rule["ends_within"]), _pointed(raw, rule["of"])
    if end is None or start is None:
        return False
    return parse_timestamp(end) - parse_timestamp(start) > timedelta(seconds=rule["seconds"])


# Each body rule form (README, "What bodies carry"): the key that names it, the check that
# finds a defect, the refusal's detail, and the place the refusal names.
_BodyRuleForm = tuple[str, Callable[[Mapping[str, Any], Mapping[str, Any]], bool],
                      Callable[[Mapping[str, Any]], str], Callable[[Mapping[str, Any]], str]]
_BODY_RULE_FORMS: tuple[_BodyRuleForm, ...] = (
    ("unique", _repeats, lambda rule: "a list holds the same value twice", lambda rule: rule["unique"]),
    ("requires", _lacks_required, lambda rule: "a field its other fields need is missing", lambda rule: rule["requires"]),
    ("any_of", _says_nothing, lambda rule: "the body needs one of " + ", ".join(rule["any_of"]),
     lambda rule: rule["any_of"][0].rsplit("/", 1)[0]),
    ("allowed", _holds_other, lambda rule: "a value its other fields do not allow here", lambda rule: rule["allowed"]),
    ("ends_within", _ends_late, lambda rule: f"ends more than {rule['seconds']} seconds after {rule['of']}",
     lambda rule: rule["ends_within"]),
)


def _check_body_rules(raw: Mapping[str, Any], spec: KindSpec) -> None:
    """What the schema cannot say (registry body_rules; README, "What bodies carry")."""

    for rule in spec.body_rules:
        for key, defective, detail, place in _BODY_RULE_FORMS:
            if key in rule and defective(raw, rule):
                raise FrameRefused("invalid_body", detail(rule), at=place(rule))


def _verify_issued_certificate(raw: Mapping[str, Any], answering: Frame, trust: TrustStore) -> None:
    """The certificate a join or renewal reply carries (README, "Installation traffic").

    It verifies against this installation's authority keys and certifies the host that
    asked: its runtime, and the key and NATS key it presented or kept. A join's
    certificate grants at most the teams, roles, agent types, sessions and tools it asked
    for, with the connection class it asked for. A renewal's has a higher membership
    epoch than the certificate it replaces and keeps its connection class and tools.
    The connection credential that comes with it ends no later than it does.
    """

    document = _pointed(raw, ISSUED_CERTIFICATE_AT)
    if not isinstance(document, Mapping):
        return  # only success replies carry one, and their schema requires it
    try:
        issued = trust.issued_certificate(document)
    except FrameRefused as refused:
        raise FrameRefused("invalid_body", refused.detail, at=ISSUED_CERTIFICATE_AT + refused.at,
                           known_at=ISSUED_CERTIFICATE_AT + refused.known_at) from refused

    def refuse(field_name: str, detail: str) -> FrameRefused:
        return FrameRefused("invalid_body", detail, at=f"{ISSUED_CERTIFICATE_AT}/body/{field_name}")

    asked = answering.body
    if issued.installation != trust.installation:
        raise refuse("installation", "the certificate belongs to another installation")
    if issued.runtime != answering.issuer.runtime:
        raise refuse("runtime", "the certificate is for another host than the one that asked")
    if answering.kind == _HOST_RENEW:
        _verify_renewed_certificate(issued, answering, asked, trust, refuse)
    else:
        _verify_joined_certificate(issued, asked, refuse)
    # NATS checks a credential only when the host connects, so it must not outlive the
    # certificate that says the host may serve (README, "Grants for the agents a host runs").
    connection = raw["body"].get("connection")
    if isinstance(connection, Mapping) and parse_timestamp(str(connection["expires_at"])) > issued.expires_at:
        raise FrameRefused("invalid_body", "the connection credential ends after the certificate it comes with",
                           at="/body/connection/expires_at")


def _verify_issued_session(raw: Mapping[str, Any], frame: Frame, answering: Frame, trust: TrustStore) -> None:
    """The session a sessions.open or sessions.renew reply carries (README, "Agent sessions").

    It verifies against the replying team services host's certificate, belongs to the
    team the request was for, and places on the host that asked the agent and type an
    open asked for, or renews the session a renewal named.
    """

    document = _pointed(raw, ISSUED_SESSION_AT)
    if not isinstance(document, Mapping):
        return  # only success replies carry one, and their schema requires it
    try:
        issued = trust.issued_session(document)
    except FrameRefused as refused:
        raise FrameRefused("invalid_body", refused.detail, at=ISSUED_SESSION_AT + refused.at,
                           known_at=ISSUED_SESSION_AT + refused.known_at) from refused

    def refuse(field_name: str, detail: str) -> FrameRefused:
        return FrameRefused("invalid_body", detail, at=f"{ISSUED_SESSION_AT}/body/{field_name}")

    if issued.signer.runtime != frame.issuer.runtime:
        raise FrameRefused("invalid_body", "the session is signed by another host than the one that replied",
                           at=f"{ISSUED_SESSION_AT}/writer/runtime")
    if issued.team != answering.audience.team:
        raise refuse("team", "the session is for another team than the one asked")
    if issued.runtime != answering.issuer.runtime:
        raise refuse("runtime", "the session places the agent on another host than the one that asked")
    asked = answering.body
    if answering.kind == _SESSIONS_OPEN:
        if issued.agent != asked.get("agent"):
            raise refuse("agent", "the session is for another agent than the one asked for")
        if issued.agent_type != asked.get("agent_type"):
            raise refuse("agent_type", "the session names another agent type than the one asked for")
    elif issued.session != asked.get("session"):
        raise refuse("session", "the renewal returned another session than the one it named")


def _verify_joined_certificate(issued: HostCertificate, asked: Mapping[str, Any],
                               refuse: Callable[[str, str], FrameRefused]) -> None:
    """A join's certificate names the keys and connection class it presented, and grants at most what it asked for."""

    key = asked.get("application_key") or {}
    if (issued.key_id, b64url(issued.public_key.public_bytes_raw())) != (key.get("key_id"), key.get("public_key")):
        raise refuse("application_key", "the certificate names another key than the join presented")
    if issued.nats_public_key != asked.get("nats_public_key"):
        raise refuse("nats_public_key", "the certificate names another NATS key than the join presented")
    for field_name in ("teams", "roles", "agent_types"):
        if not getattr(issued, field_name) <= set(asked.get(field_name) or ()):
            raise refuse(field_name, f"the certificate grants {field_name} the join did not ask for")
    if issued.session_limit > asked.get("session_limit", 0):
        raise refuse("session_limit", "the certificate allows more sessions than the join asked for")
    if issued.connection_class != asked.get("connection_class"):
        raise refuse("connection_class", "the certificate names another connection class than the join asked for")
    if issued.tools is not None and not issued.tools <= set(asked.get("tools") or ()):
        raise refuse("tools", "the certificate names tools the join did not ask for")


def _verify_renewed_certificate(issued: HostCertificate, answering: Frame, asked: Mapping[str, Any], trust: TrustStore,
                                refuse: Callable[[str, str], FrameRefused]) -> None:
    """A renewal's certificate replaces the one that signed the renewal, at a higher epoch, with the rotated or kept keys."""

    renewing = answering.issuer
    current = trust.host_certificate(renewing.installation, renewing.runtime, renewing.membership_epoch)
    if current is None:
        raise refuse("replaces", "the receiver does not hold the certificate this one renews")
    if issued.replaces != current.certificate_id:
        raise refuse("replaces", "the certificate does not replace the one that signed the renewal")
    if issued.membership_epoch <= current.membership_epoch:
        raise refuse("membership_epoch", "a renewed certificate has a higher membership epoch")
    rotation = asked.get(_KEY_ROTATION)
    key_id, public_key = ((rotation["key"]["key_id"], rotation["key"]["public_key"]) if rotation is not None
                          else (current.key_id, b64url(current.public_key.public_bytes_raw())))
    if (issued.key_id, b64url(issued.public_key.public_bytes_raw())) != (key_id, public_key):
        raise refuse("application_key", "the certificate names another key than the renewal kept or rotated to")
    if issued.nats_public_key != asked.get("new_nats_public_key", current.nats_public_key):
        raise refuse("nats_public_key", "the certificate names another NATS key than the renewal kept or asked for")
    # A renewal carries neither: changing what the host is approved for needs a new join.
    if issued.connection_class != current.connection_class:
        raise refuse("connection_class", "a renewed certificate keeps the connection class of the one it replaces")
    if issued.tools != current.tools:
        raise refuse("tools", "a renewed certificate keeps the tools of the one it replaces")


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
    stored_at: Optional[datetime] = None,
) -> VerifiedFrame:
    """Check one received frame in the registry's order; raise ``FrameRefused`` on the first failure.

    A join or renewal that names another pre-release contract's digest is checked for
    everything before its body (its signature, which for a join needs the key it presents,
    its issuer, sender, audience, subject, times and replay) and returned with
    ``other_contract`` set and no body (module docstring). ``headers`` are the NATS headers the frame arrived with, ``{}`` when it arrived
    with none (nats-py reports that as None), or None only for a frame read from
    storage. ``answering`` is the request a reply answers; replies are refused
    without it. ``stored_at`` is when a stream stored a queued frame: JetStream's
    message timestamp, which the server sets, not the sender. A queued frame's token
    must be valid then, and without it at ``now``. ``now`` and ``stored_at`` must name
    their time zone.
    """

    if isinstance(data, str):
        raise TypeError("frames are verified as the bytes received")
    if now.tzinfo is None or (stored_at is not None and stored_at.tzinfo is None):
        raise ValueError("now and stored_at must name their time zone")
    if trust.installation != receiver.installation:
        raise ValueError("the trust store belongs to another installation than the receiver")
    data = bytes(data)
    if len(data) > MAX_FRAME_BYTES:
        raise FrameRefused("too_large", f"{len(data)} bytes")
    raw, frame, newer_minor = _parse(data)
    issued_at = parse_timestamp(frame.issued_at)
    spec, is_reply = _kind_spec(frame.kind)
    joining = spec is not None and not is_reply and spec.verification == PROOF_OF_POSSESSION
    certificate: Optional[HostCertificate] = None
    session: Optional[AgentSession] = None
    if joining:
        _verify_signature(raw, frame, _presented_key(raw, frame, trust))
    else:
        certificate = _issuer_certificate(frame, trust, issued_at)
        _verify_signature(raw, frame, certificate.public_key)
    if body_digest(raw["body"]) != frame.body_digest:
        raise FrameRefused("bad_digest", at="/body_digest")
    theirs = _other_contract(raw, spec, is_reply, trust)
    if certificate is None:
        assert spec is not None  # only a known kind proves possession
        _verify_join(raw, frame, spec, trust, now, read_body=theirs is None)
    else:
        session = _session(frame, trust)
        _verify_revocation(frame, certificate, session, trust, now)
        _verify_epochs(certificate, spec, trust)
        _verify_issuer(frame, spec, certificate, session, issued_at, is_reply, answering)
        if not is_reply and theirs is None:
            _verify_renewal(raw, frame, certificate)
        _verify_sender(frame, certificate, session, spec, is_reply, trust, issued_at, receiver)
    _verify_audience(frame, receiver, answering if is_reply else None)
    _verify_subject(raw, frame, spec, is_reply, arrived_on, headers)
    _verify_time(frame, spec, now, issued_at)
    replay_key = ReplayKey(frame.issuer.installation, frame.issuer.runtime, frame.issuer.membership_epoch, frame.message_id)
    digest = frame_digest(raw)
    earlier = replay(replay_key)
    if earlier is not None and earlier != digest:
        raise FrameRefused("replayed", "this message id was admitted with other content", at="/message_id")
    if spec is None:
        raise FrameRefused("unknown_kind", frame.kind, at="/kind")
    if theirs is not None:
        return VerifiedFrame(frame=frame, body=None, certificate=certificate, session=session, authority=None,
                             replay_key=replay_key, frame_digest=digest, raw=raw, other_contract=True,
                             their_contract_sha256=theirs)
    model, shape = body_model(frame.kind)
    # The body as this version knows it: a newer minor's unknown optional fields are
    # dropped. Checks that look inside the body start from a field only when the schema
    # knows it, and verify signatures over the field as received.
    known = prune(raw["body"], shape) if newer_minor else raw["body"]
    try:
        body = model.validate_python(known)
    except ValidationError as error:
        raise refused_by("invalid_body", error, shape, prefix="/body") from error
    if not is_reply:
        _check_body_rules(raw, spec)
    if frame.idempotency_key != expected_idempotency_key(raw, spec, is_reply, answering):
        raise FrameRefused("invalid_body", "the idempotency key is not the one the kind's key parts give",
                           at="/idempotency_key")
    # The remaining invalid_body checks compare the body with records; bad_authority comes after them.
    if not is_reply:
        _verify_renew_key_proof(raw, certificate, trust)
    elif _installation_operation(spec) and ISSUED_CERTIFICATE_AT.rsplit("/", 1)[1] in known:
        assert answering is not None  # _verify_issuer refused replies without their request
        _verify_issued_certificate(raw, answering, trust)
    elif spec.name in {_SESSIONS_OPEN, _SESSIONS_RENEW} and ISSUED_SESSION_AT.rsplit("/", 1)[1] in known:
        assert answering is not None  # _verify_issuer refused replies without their request
        _verify_issued_session(raw, frame, answering, trust)
    authority = _verify_authority(raw, frame, spec, is_reply, trust, certificate, receiver, now, stored_at)
    return VerifiedFrame(frame=frame, body=body, certificate=certificate, session=session, authority=authority,
                         replay_key=replay_key, frame_digest=digest, raw=raw)

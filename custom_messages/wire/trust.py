"""What a receiver trusts: its installation's authority keys and the signed records that chain to them.

Identity in protocol 1 (contracts/messages/v1/README.md, "Who sent it"):

- Each installation has authority keys. A receiver is given its own installation's
  keys when it joins; another installation's keys come only from a federation
  agreement that the receiver's own authority signed.
- An authority signs host certificates (a host's key, teams, roles, agent types and
  validity), the list of revoked certificates, and agreements.
- The team services sign agent session certificates: this agent runs on this host
  from one time to another. Hosts sign frames for the agents they run; there are
  no per-agent private keys, because agents in one process could not keep keys
  from each other.

``TrustStore.add`` verifies one signed state record and keeps it. Load agreements
first, then certificates, then revocations, then sessions: a record's signer must
already be known. A record with a sequence no higher than the one held for its key
is ignored; the same sequence with other content is refused as ``replayed``.
Lookups return only verified records. Revocation is checked when a
frame is verified, so a revocation that arrives later still stops every frame that
was not yet admitted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import MISSING, ValidationError

from custom_messages.wire.canonical import CanonicalJsonError, parse
from custom_messages.wire.encoding import b64url, b64url_decode, body_digest, signed_bytes
from custom_messages.wire.kinds import (
    BETWEEN_INSTALLATIONS,
    CLOCK_SKEW_SECONDS,
    KINDS,
    MAX_FRAME_BYTES,
    STATE_RECORDS,
    STATE_SIGNING_DOMAIN,
)
from custom_messages.wire.models import State, StateWriterAuthority, StateWriterHost
from custom_messages.wire.refusals import FrameRefused, pointer
from custom_messages.wire.spec import StateSpec
from custom_messages.wire.values import parse_timestamp

SKEW = timedelta(seconds=CLOCK_SKEW_SECONDS)
# Runtime ids that would read as another installation's sender slot or a system id.
RESERVED_RUNTIME_PREFIXES = ("x-", "_")
# Record names (registry state_records). The first four vouch for keys and agents and are
# kept by the trust store; the others report state and are verified when read.
CERTIFICATE, REVOCATIONS, AGENT_SESSION, AGREEMENT = "certificate", "revocations", "agent_session", "agreement"
HOST, AGENT, PLACEMENT, TEAM_VIEW = "host", "agent", "placement", "team_view"
TRUST_RECORDS = frozenset({CERTIFICATE, REVOCATIONS, AGENT_SESSION, AGREEMENT})
WRITER_AUTHORITY = "authority"


def within(start: datetime, end: datetime, moment: datetime) -> bool:
    """Whether ``moment`` falls in [start, end), allowing the clock tolerance at both ends.

    Differences, not sums, so times near the end of year 9999 cannot overflow.
    """

    return start - moment <= SKEW and moment - end < SKEW


def state_signing_bytes(document: Mapping[str, Any]) -> bytes:
    return signed_bytes(STATE_SIGNING_DOMAIN, document)


def sign_state(document: Mapping[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Return ``document`` with its body digest and signature filled in."""

    signed = {key: value for key, value in document.items() if key not in {"signature", "body_digest"}}
    signed["algorithm"] = "ed25519"
    signed["body_digest"] = body_digest(signed["body"])
    signed["signature"] = b64url(private_key.sign(state_signing_bytes(signed)))
    return signed


@dataclass(frozen=True)
class AuthorityKey:
    """One of an installation's authority keys, given to a receiver as a starting point of trust."""

    installation: str
    key_id: str
    public_key: Ed25519PublicKey


@dataclass(frozen=True)
class HostCertificate:
    certificate_id: str
    installation: str
    runtime: str
    teams: frozenset[str]
    roles: frozenset[str]
    agent_types: frozenset[str]
    membership_epoch: int
    revocation_epoch: int
    key_id: str
    public_key: Ed25519PublicKey
    issued_at: datetime
    expires_at: datetime

    def valid_at(self, moment: datetime) -> bool:
        return within(self.issued_at, self.expires_at, moment)


@dataclass(frozen=True)
class AgentSession:
    session: str
    installation: str
    team: str
    agent: str
    agent_type: str
    agent_role: str
    runtime: str
    placement_revision: int
    started_at: datetime
    ends_at: datetime
    signer: HostCertificate

    def covers(self, moment: datetime) -> bool:
        return within(self.started_at, self.ends_at, moment)


@dataclass(frozen=True)
class Agreement:
    agreement_id: str
    peer: str
    authority_keys: Mapping[str, Ed25519PublicKey]
    teams: frozenset[str]
    kinds: frozenset[str]
    issued_at: datetime
    expires_at: datetime

    def covers(self, moment: datetime) -> bool:
        return within(self.issued_at, self.expires_at, moment)


@dataclass(frozen=True)
class VerifiedRecord:
    """A state record whose signature, signer and record rules were checked."""

    document: State
    body: Any
    signer: Optional[HostCertificate]  # None when an authority key signed it
    digest: str


@dataclass(frozen=True)
class _Revocations:
    revocation_epoch: int
    certificate_ids: frozenset[str]


class TrustStore:
    """One receiver's view of who may sign what. Not shared between installations."""

    def __init__(self, installation: str, authority_keys: Iterable[AuthorityKey]) -> None:
        self.installation = installation
        self._anchors: dict[str, Ed25519PublicKey] = {}
        for key in authority_keys:
            if key.installation != installation:
                raise ValueError("another installation's authority keys come only from an agreement")
            self._anchors[key.key_id] = key.public_key
        self._sequences: dict[tuple[str, str], tuple[int, str]] = {}
        self._certificates: dict[tuple[str, str, int], HostCertificate] = {}
        self._revocations: dict[str, _Revocations] = {}
        self._sessions: dict[tuple[str, str, str], AgentSession] = {}
        self._agreements: dict[str, Agreement] = {}
        self._minimums: dict[tuple[str, Optional[str]], tuple[int, int]] = {}

    # -- lookups ---------------------------------------------------------------------------------

    def host_certificate(self, installation: str, runtime: str, membership_epoch: int) -> Optional[HostCertificate]:
        return self._certificates.get((installation, runtime, membership_epoch))

    def agent_session(self, installation: str, team: str, session: str) -> Optional[AgentSession]:
        return self._sessions.get((installation, team, session))

    def agreement(self, peer: str) -> Optional[Agreement]:
        return self._agreements.get(peer)

    def certificates_with_key(self, installation: str, key_id: str) -> list[HostCertificate]:
        """Every held certificate of the installation that names this key id."""

        return [certificate for certificate in self._certificates.values()
                if certificate.installation == installation and certificate.key_id == key_id]

    def revoked(self, installation: str, certificate_id: str) -> bool:
        revocations = self._revocations.get(installation)
        return revocations is not None and certificate_id in revocations.certificate_ids

    def require_epochs(self, installation: str, runtime: Optional[str] = None, *,
                       membership: int = 0, revocation: int = 0) -> None:
        """Refuse certificates below these epochs, for one runtime or (``runtime=None``) a whole installation."""

        held = self._minimums.get((installation, runtime), (0, 0))
        self._minimums[(installation, runtime)] = (max(held[0], membership), max(held[1], revocation))

    def minimum_epochs(self, installation: str, runtime: str) -> tuple[int, int]:
        whole = self._minimums.get((installation, None), (0, 0))
        one = self._minimums.get((installation, runtime), (0, 0))
        return max(whole[0], one[0]), max(whole[1], one[1])

    # -- adding records ---------------------------------------------------------------------------

    def verify(self, data: bytes | bytearray | memoryview) -> VerifiedRecord:
        """Verify one signed state record of any kind without keeping it.

        Readers use this for the records that report state (host liveness, agent
        state, placement, team view); ``add`` uses it for the records it keeps.
        """

        data = bytes(data)
        if len(data) > MAX_FRAME_BYTES:
            raise FrameRefused("too_large", f"{len(data)} bytes")
        try:
            raw = parse(data)
            document = State.model_validate(raw)
        except (CanonicalJsonError, ValidationError) as error:
            at = pointer(error.errors()[0]) if isinstance(error, ValidationError) else ""
            raise FrameRefused("malformed", str(error).splitlines()[0], at=at) from error
        spec = STATE_RECORDS[document.record]
        issued_at = parse_timestamp(document.issued_at)
        public_key, signer = self._signer(document, spec, issued_at)
        try:
            public_key.verify(b64url_decode(document.signature), state_signing_bytes(raw))
        except (InvalidSignature, ValueError) as error:
            raise FrameRefused("bad_signature", at="/signature") from error
        if body_digest(raw["body"]) != document.body_digest:
            raise FrameRefused("bad_digest", at="/body_digest")
        # A state report is used as soon as it is read; a kept record's signer is checked
        # again whenever the record is used, so a later revocation still counts.
        if (document.record not in TRUST_RECORDS and signer is not None
                and self.revoked(signer.installation, signer.certificate_id)):
            raise FrameRefused("revoked", "the writer's certificate is revoked", at="/writer/key_id")
        try:
            body = spec.body_model.validate_python(raw["body"])
        except ValidationError as error:
            raise FrameRefused("invalid_body", str(error).splitlines()[0],
                               at=pointer(error.errors()[0], prefix="/body")) from error
        if document.expires_at is not MISSING and parse_timestamp(document.expires_at) <= issued_at:
            raise FrameRefused("invalid_body", "a record cannot expire before it is written", at="/expires_at")
        if document.record not in TRUST_RECORDS:
            self._check_state_report(document, body, signer)
        return VerifiedRecord(document=document, body=body, signer=signer,
                              digest=b64url(hashlib.sha256(state_signing_bytes(raw)).digest()))

    def add(self, data: bytes | bytearray | memoryview) -> bool:
        """Verify one trust record and keep it; return False when a newer one is already held."""

        record = self.verify(data)
        document = record.document
        if document.record not in TRUST_RECORDS:
            raise ValueError(f"{document.record} records report state; verify them when read instead")
        slot = (document.record, document.key)
        held_sequence, held_digest = self._sequences.get(slot, (0, ""))
        if held_sequence == document.sequence and held_digest != record.digest:
            raise FrameRefused("replayed", "the writer used this sequence for other content", at="/sequence")
        if held_sequence >= document.sequence:
            return False
        keeper = {CERTIFICATE: self._keep_certificate, REVOCATIONS: self._keep_revocations,
                  AGENT_SESSION: self._keep_session, AGREEMENT: self._keep_agreement}[document.record]
        keeper(document, record.body, record.signer)
        self._sequences[slot] = (document.sequence, record.digest)
        return True

    def _signer(self, document: State, spec: StateSpec, issued_at: datetime) -> tuple[Ed25519PublicKey, Optional[HostCertificate]]:
        """The key that must have signed the record, from an authority key or the writer's certificate."""

        writer = document.writer
        if spec.signer == WRITER_AUTHORITY:
            if not isinstance(writer, StateWriterAuthority):
                raise FrameRefused("wrong_issuer", f"an authority signs {document.record} records", at="/writer/type")
            return self._authority_key(writer.installation, writer.key_id, document.record), None
        if not isinstance(writer, StateWriterHost):
            raise FrameRefused("wrong_issuer", f"a host signs {document.record} records", at="/writer/type")
        certificate = self.host_certificate(writer.installation, writer.runtime, writer.membership_epoch)
        if certificate is None or certificate.key_id != writer.key_id or not certificate.valid_at(issued_at):
            raise FrameRefused("unknown_key", "no certificate for the writer valid when the record was written",
                               at="/writer/key_id")
        if not certificate.roles & set(spec.signer_roles):
            raise FrameRefused("wrong_issuer", f"only {', '.join(spec.signer_roles)} hosts sign {document.record} records",
                               at="/writer")
        return certificate.public_key, certificate

    def _authority_key(self, installation: str, key_id: str, record: str) -> Ed25519PublicKey:
        if installation == self.installation:
            key = self._anchors.get(key_id)
        elif record == AGREEMENT:
            raise FrameRefused("wrong_issuer", "only this installation's authority records agreements", at="/writer/installation")
        else:
            agreement = self._agreements.get(installation)
            key = agreement.authority_keys.get(key_id) if agreement else None
        if key is None:
            raise FrameRefused("unknown_key", f"{key_id} is not an authority key of {installation}", at="/writer/key_id")
        return key

    @staticmethod
    def _expect_key(document: State, expected: str) -> None:
        if document.key != expected:
            raise FrameRefused("invalid_body", f"a {document.record} record belongs under key {expected}", at="/key")

    @staticmethod
    def _same_installation(document: State, installation: str) -> None:
        if installation != document.writer.installation:
            raise FrameRefused("wrong_issuer", "an installation vouches only for its own hosts and agents",
                               at="/body/installation")

    def _check_state_report(self, document: State, body: Any, signer: Optional[HostCertificate]) -> None:
        """A host writes only its own liveness; team records name a team the writer's certificate serves."""

        assert signer is not None  # state reports are host-signed (registry signer: host)
        if document.record == HOST:
            self._expect_key(document, body.runtime)
            if body.runtime != signer.runtime:
                raise FrameRefused("invalid_body", "a host writes only its own liveness record", at="/body/runtime")
            return
        team = document.key.split(".", 1)[0] if document.record == TEAM_VIEW else body.team
        if team not in signer.teams:
            raise FrameRefused("wrong_team", "the writer's certificate does not serve the record's team",
                               at="/key" if document.record == TEAM_VIEW else "/body/team")
        if document.record == TEAM_VIEW:
            if not document.key.startswith(f"{team}.{body.entry}."):
                raise FrameRefused("invalid_body", "a team view key names its team and entry", at="/key")
        else:
            self._expect_key(document, f"{body.team}.{body.agent}")

    def _keep_certificate(self, document: State, body: Any, _signer: Optional[HostCertificate]) -> None:
        self._same_installation(document, body.installation)
        if body.runtime.startswith(RESERVED_RUNTIME_PREFIXES):
            raise FrameRefused("invalid_body", "runtime ids starting with x- or _ are reserved", at="/body/runtime")
        self._expect_key(document, f"host.{body.installation}.{body.runtime}.{body.membership_epoch}")
        issued_at, expires_at = parse_timestamp(body.issued_at), parse_timestamp(body.expires_at)
        if expires_at <= issued_at:
            raise FrameRefused("invalid_body", "a certificate must end after it starts", at="/body/expires_at")
        self._certificates[(body.installation, body.runtime, body.membership_epoch)] = HostCertificate(
            certificate_id=body.certificate_id, installation=body.installation, runtime=body.runtime,
            teams=frozenset(body.teams), roles=frozenset(body.roles), agent_types=frozenset(body.agent_types),
            membership_epoch=body.membership_epoch, revocation_epoch=body.revocation_epoch,
            key_id=body.application_key.key_id,
            public_key=Ed25519PublicKey.from_public_bytes(b64url_decode(body.application_key.public_key)),
            issued_at=issued_at, expires_at=expires_at,
        )

    def _keep_revocations(self, document: State, body: Any, _signer: Optional[HostCertificate]) -> None:
        self._same_installation(document, body.installation)
        self._expect_key(document, f"revocations.{body.installation}")
        held = self._revocations.get(body.installation)
        listed = frozenset(entry.certificate_id for entry in body.entries)
        if held is not None and (body.revocation_epoch < held.revocation_epoch or not held.certificate_ids <= listed):
            raise FrameRefused("invalid_body", "the revocation epoch only grows and revoked certificates stay listed",
                               at="/body/revocation_epoch")
        self._revocations[body.installation] = _Revocations(body.revocation_epoch, listed)

    def _keep_session(self, document: State, body: Any, signer: Optional[HostCertificate]) -> None:
        assert signer is not None  # sessions are host-signed (registry signer: host)
        self._same_installation(document, body.installation)
        if body.team not in signer.teams:
            raise FrameRefused("wrong_issuer", "the team services certificate does not serve the session's team",
                               at="/body/team")
        self._expect_key(document, f"session.{body.installation}.{body.team}.{body.session}")
        started_at, ends_at = parse_timestamp(body.started_at), parse_timestamp(body.ends_at)
        if ends_at <= started_at:
            raise FrameRefused("invalid_body", "a session must end after it starts", at="/body/ends_at")
        self._sessions[(body.installation, body.team, body.session)] = AgentSession(
            session=body.session, installation=body.installation, team=body.team, agent=body.agent,
            agent_type=body.agent_type, agent_role=body.agent_role, runtime=body.runtime,
            placement_revision=body.placement_revision, started_at=started_at, ends_at=ends_at, signer=signer,
        )

    def _keep_agreement(self, document: State, body: Any, _signer: Optional[HostCertificate]) -> None:
        if body.installation != self.installation or body.peer == self.installation:
            raise FrameRefused("wrong_issuer", "an agreement names this installation and another one", at="/body/peer")
        self._expect_key(document, f"agreement.{body.peer}")
        for index, kind in enumerate(body.kinds):
            spec = KINDS.get(kind)
            if spec is not None and spec.delivery_class not in BETWEEN_INSTALLATIONS:
                raise FrameRefused("invalid_body", f"{kind} cannot travel between installations", at=f"/body/kinds/{index}")
        issued_at, expires_at = parse_timestamp(body.issued_at), parse_timestamp(body.expires_at)
        if expires_at <= issued_at:
            raise FrameRefused("invalid_body", "an agreement must end after it starts", at="/body/expires_at")
        self._agreements[body.peer] = Agreement(
            agreement_id=body.agreement_id, peer=body.peer,
            authority_keys={key.key_id: Ed25519PublicKey.from_public_bytes(b64url_decode(key.public_key))
                            for key in body.authority_keys},
            teams=frozenset(body.teams), kinds=frozenset(body.kinds), issued_at=issued_at, expires_at=expires_at,
        )

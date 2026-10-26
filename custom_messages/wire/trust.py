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

``TrustStore.verify`` reads a state report (host liveness, agent state, placement,
team view): reports are not kept, and are checked on every read against the reader's
clock, minimum epochs and the record it already holds for the key.

A renewed certificate (a renewal or a key rotation) has a higher membership epoch
than the one it ``replaces``, so both stay in the store and queued frames still find
the certificate valid when they were issued. The replaced certificate signs new
frames only until ``CERTIFICATE_OVERLAP_SECONDS`` after its replacement was issued
(authorization plan 12.5).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import MISSING, ValidationError

from custom_messages.wire.canonical import CanonicalJsonError, parse
from custom_messages.wire.encoding import b64url, b64url_decode, body_digest, signed_bytes
from custom_messages.wire.kinds import (
    BETWEEN_INSTALLATIONS,
    CERTIFICATE_OVERLAP_SECONDS,
    CLOCK_SKEW_SECONDS,
    INSTALLATION_SCOPE,
    KINDS,
    MAX_FRAME_BYTES,
    PEER_SENDER_SLOT,
    PROTOCOL,
    STATE_RECORDS,
    STATE_SHAPE,
    STATE_SIGNING_DOMAIN,
)
from custom_messages.wire.models import State, StateWriterAuthority, StateWriterHost
from custom_messages.wire.refusals import FrameRefused, refused_by
from custom_messages.wire.spec import StateSpec, newer_minor, prune
from custom_messages.wire.values import parse_timestamp

SKEW = timedelta(seconds=CLOCK_SKEW_SECONDS)
OVERLAP = timedelta(seconds=CERTIFICATE_OVERLAP_SECONDS)
# Runtime ids that would read as another installation's sender slot (x-<installation>)
# or a system id such as the installation scope (_install); transport.yaml defines both.
RESERVED_RUNTIME_PREFIXES = (PEER_SENDER_SLOT.split("{", 1)[0], INSTALLATION_SCOPE[:1])
# Where each trust record's body says when it ends; the record's own expiry, when it
# has one, must say the same, so the record has one validity window.
BODY_END = {"certificate": "expires_at", "agent_session": "ends_at", "agreement": "expires_at"}
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
    nats_public_key: str
    session_limit: int
    issued_at: datetime
    expires_at: datetime
    replaces: Optional[str] = None
    # When a certificate replacing this one was issued; it then signs new frames for
    # OVERLAP more (differences, not sums, so times near year 9999 cannot overflow).
    replaced_at: Optional[datetime] = None

    def valid_at(self, moment: datetime) -> bool:
        return within(self.issued_at, self.expires_at, moment) and (
            self.replaced_at is None or moment - self.replaced_at < OVERLAP + SKEW)


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
        # (installation, runtime, replaced certificate id) -> when the earliest replacement was issued.
        self._replacements: dict[tuple[str, str, str], datetime] = {}

    # -- lookups ---------------------------------------------------------------------------------

    def host_certificate(self, installation: str, runtime: str, membership_epoch: int) -> Optional[HostCertificate]:
        certificate = self._certificates.get((installation, runtime, membership_epoch))
        return None if certificate is None else self._with_replacement(certificate)

    def _with_replacement(self, certificate: HostCertificate) -> HostCertificate:
        replaced_at = self._replacements.get((certificate.installation, certificate.runtime, certificate.certificate_id))
        return certificate if replaced_at is None else replace(certificate, replaced_at=replaced_at)

    def agent_session(self, installation: str, team: str, session: str) -> Optional[AgentSession]:
        return self._sessions.get((installation, team, session))

    def agreement(self, peer: str) -> Optional[Agreement]:
        return self._agreements.get(peer)

    def certificates_with_key(self, installation: str, key_id: str) -> list[HostCertificate]:
        """Every held certificate of the installation that names this key id."""

        return [self._with_replacement(certificate) for certificate in self._certificates.values()
                if certificate.installation == installation and certificate.key_id == key_id]

    def names_authority_key(self, installation: str, key_id: str) -> bool:
        """Whether ``key_id`` is one of the installation's authority keys: this one's own, or a peer's from its agreement."""

        if installation == self.installation:
            return key_id in self._anchors
        agreement = self._agreements.get(installation)
        return agreement is not None and key_id in agreement.authority_keys

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

    def verify(self, data: bytes | bytearray | memoryview, *, record: str, now: datetime,
               held: Optional[tuple[int, str]] = None) -> VerifiedRecord:
        """Verify one state report (host liveness, agent state, placement, team view) as a reader reads it.

        Reports are used as soon as they are read, like requests, so besides the
        signer's certificate and the record's rules (README, "State records"):
        - only hosts of this installation write them (``wrong_issuer``);
        - the writer's certificate meets the receiver's minimum epochs (``epoch_too_old``);
        - the record has not expired and is not dated ahead of ``now`` beyond the
          clock tolerance (``expired``, ``not_yet_valid``);
        - ``held`` is the sequence and digest of the record the reader holds for the
          same key, if any: an older record, or other content under that sequence, is
          refused (``replayed``), so a stored copy written back is never read as newer.
        Trust records are kept with ``add`` instead.
        """

        if now.tzinfo is None:
            raise ValueError("now must name its time zone")
        if record in TRUST_RECORDS or record not in STATE_RECORDS:
            raise ValueError(f"verify reads state reports; {record} records are kept with add")
        return self._checked(self._parsed(data), reading=(record, now, held))

    @staticmethod
    def _parsed(data: bytes | bytearray | memoryview) -> dict[str, Any]:
        data = bytes(data)
        if len(data) > MAX_FRAME_BYTES:
            raise FrameRefused("too_large", f"{len(data)} bytes")
        try:
            return parse(data)
        except CanonicalJsonError as error:
            raise FrameRefused("malformed", str(error).splitlines()[0]) from error

    def issued_certificate(self, raw: Mapping[str, Any]) -> HostCertificate:
        """Verify a certificate record carried in a join or renewal reply, without keeping it.

        ``raw`` is the record's JSON object as received inside the frame, which its
        signature covers. The receiver keeps it afterwards with ``add``.
        """

        if raw.get("record") != CERTIFICATE:
            raise FrameRefused("invalid_body", "a join or renewal returns a certificate record", at="/record")
        record = self._checked(raw)
        return self._certificate(record.document, record.body)

    def _checked(self, raw: Mapping[str, Any],
                 reading: Optional[tuple[str, datetime, Optional[tuple[int, str]]]] = None) -> VerifiedRecord:
        """Check a parsed record in the registry's refusal order (README, "State records").

        ``reading`` is the record type the reader reads, its clock and its held record for
        a state report (``verify``), or None for a trust record (``add``). A record of
        another type than the one read is refused as ``malformed``: whoever can write to
        a bucket chooses what it holds. A record from a newer minor loses the
        unknown optional fields this version does not know before strict validation; its
        signature still covers them (README, "Versions").
        """

        newer = newer_minor(raw, PROTOCOL)
        try:
            document = State.model_validate(prune(raw, STATE_SHAPE) if newer else raw)
        except ValidationError as error:
            raise refused_by("malformed", error) from error
        spec = STATE_RECORDS[document.record]
        report = document.record not in TRUST_RECORDS
        if reading is not None and document.record != reading[0]:
            raise FrameRefused("malformed", f"a {reading[0]} record was read, not a {document.record} record", at="/record")
        if reading is None and report:
            raise FrameRefused("malformed", f"{document.record} records report state; readers verify them, the trust store "
                               "keeps only trust records", at="/record")
        issued_at = parse_timestamp(document.issued_at)
        public_key, signer = self._signer(document, spec, issued_at)
        try:
            public_key.verify(b64url_decode(document.signature), state_signing_bytes(raw))
        except (InvalidSignature, ValueError) as error:
            raise FrameRefused("bad_signature", at="/signature") from error
        if body_digest(raw["body"]) != document.body_digest:
            raise FrameRefused("bad_digest", at="/body_digest")
        digest = b64url(hashlib.sha256(state_signing_bytes(raw)).digest())
        if report:
            assert signer is not None and reading is not None  # state reports are host-signed (registry signer: host)
            self._check_report_writer(signer)
        if signer is not None and not signer.roles & set(spec.signer_roles):
            raise FrameRefused("wrong_issuer", f"only {', '.join(spec.signer_roles)} hosts sign {document.record} records",
                               at="/writer")
        if report:
            assert signer is not None and reading is not None
            self._check_report_scope(raw, document, signer)
            self._check_report_time(document, digest, *reading[1:])
        try:
            body = spec.body_model.validate_python(prune(raw["body"], spec.body_shape) if newer else raw["body"])
        except ValidationError as error:
            raise refused_by("invalid_body", error, prefix="/body") from error
        self._check_record_dates(document, spec, body, issued_at)
        if report:
            assert signer is not None
            self._check_state_report(document, body, signer)
        return VerifiedRecord(document=document, body=body, signer=signer, digest=digest)

    def _check_report_writer(self, signer: HostCertificate) -> None:
        """A state report is used as soon as it is read, so its writer's certificate is not revoked and meets the minimum epochs.

        A kept trust record's signer is checked whenever the record is used instead, so a
        later revocation still counts.
        """

        if self.revoked(signer.installation, signer.certificate_id):
            raise FrameRefused("revoked", "the writer's certificate is revoked", at="/writer/key_id")
        membership, revocation = self.minimum_epochs(signer.installation, signer.runtime)
        if signer.membership_epoch < membership or signer.revocation_epoch < revocation:
            raise FrameRefused("epoch_too_old", "the writer's certificate is below the receiver's minimum epochs",
                               at="/writer/membership_epoch")

    def _check_report_scope(self, raw: Mapping[str, Any], document: State, signer: HostCertificate) -> None:
        """Hosts of this installation write reports, of teams their certificate serves.

        The team comes from the key (team view) or the body's team as received, before
        the body schema: a missing team fails here.
        """

        if signer.installation != self.installation:
            # Agreements carry durable work only; another installation reports no state here.
            raise FrameRefused("wrong_issuer", "only hosts of this installation write state reports", at="/writer/installation")
        if document.record == HOST:
            return
        team = document.key.split(".", 1)[0] if document.record == TEAM_VIEW else raw["body"].get("team")
        if not isinstance(team, str) or team not in signer.teams:
            raise FrameRefused("wrong_team", "the writer's certificate does not serve the record's team",
                               at="/key" if document.record == TEAM_VIEW else "/body/team")

    @staticmethod
    def _check_report_time(document: State, digest: str, now: datetime, held: Optional[tuple[int, str]]) -> None:
        """Not expired, not dated ahead, and not older than the record the reader holds for the key."""

        if document.expires_at is not MISSING and now - parse_timestamp(document.expires_at) >= SKEW:
            raise FrameRefused("expired", at="/expires_at")
        if parse_timestamp(document.issued_at) - now > SKEW:
            raise FrameRefused("not_yet_valid", at="/issued_at")
        if held is not None and (document.sequence < held[0] or (document.sequence == held[0] and digest != held[1])):
            raise FrameRefused("replayed", "an older record than the one the reader holds, or other content under its sequence",
                               at="/sequence")

    @staticmethod
    def _check_record_dates(document: State, spec: StateSpec, body: Any, issued_at: datetime) -> None:
        """A record expires after it is written, within its bucket's lifetime, and when its body says it ends."""

        if document.expires_at is not MISSING and parse_timestamp(document.expires_at) <= issued_at:
            raise FrameRefused("invalid_body", "a record cannot expire before it is written", at="/expires_at")
        if spec.ttl_seconds is not None and (document.expires_at is MISSING or parse_timestamp(document.expires_at) - issued_at
                                             > timedelta(seconds=spec.ttl_seconds)):
            raise FrameRefused("invalid_body", f"a {document.record} record expires within its bucket's {spec.ttl_seconds} seconds",
                               at="/expires_at")
        end = BODY_END.get(document.record)
        if end is not None and document.expires_at is not MISSING and document.expires_at != getattr(body, end):
            raise FrameRefused("invalid_body", f"the record expires when its body's {end} says", at="/expires_at")

    def add(self, data: bytes | bytearray | memoryview) -> bool:
        """Verify one trust record and keep it; return False when a newer one is already held."""

        record = self._checked(self._parsed(data))
        document = record.document
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

    def _check_state_report(self, document: State, body: Any, signer: HostCertificate) -> None:
        """A report's key and body rules: a host's own liveness, an agent's or placement's key, the team view's key form."""

        if document.record == HOST:
            self._check_host_record(document, body, signer)
            return
        if document.record == TEAM_VIEW:
            parts = document.key.split(".")
            if len(parts) != 3 or parts[1] != body.entry:
                raise FrameRefused("invalid_body", "a team view key is <team>.<entry>.<id>", at="/key")
        else:
            self._expect_key(document, f"{body.team}.{body.agent}")

    def _check_host_record(self, document: State, body: Any, signer: HostCertificate) -> None:
        """A host writes only its own liveness, naming the certificate it signs with and no more sessions than it allows.

        The certificate ref's digest is the one ``add`` recorded for that certificate
        record: SHA-256 of its signing bytes (README, "State records").
        """

        self._expect_key(document, body.runtime)
        if body.runtime != signer.runtime:
            raise FrameRefused("invalid_body", "a host writes only its own liveness record", at="/body/runtime")
        held = self._sequences.get((CERTIFICATE, f"host.{signer.installation}.{signer.runtime}.{signer.membership_epoch}"))
        named = body.certificate
        for field_name, expected in (("certificate_id", signer.certificate_id), ("membership_epoch", signer.membership_epoch),
                                     ("sha256", held[1] if held else None)):
            if getattr(named, field_name) != expected:
                raise FrameRefused("invalid_body", "a host record names the certificate it is signed with",
                                   at=f"/body/certificate/{field_name}")
        if body.session_limit > signer.session_limit:
            raise FrameRefused("invalid_body", "a host runs at most the sessions its certificate allows", at="/body/session_limit")
        if body.sessions_active > body.session_limit:
            raise FrameRefused("invalid_body", "a host runs no more sessions than its limit", at="/body/sessions_active")

    def _certificate(self, document: State, body: Any) -> HostCertificate:
        """The certificate a verified certificate record states, after its own rules."""

        self._same_installation(document, body.installation)
        if body.runtime.startswith(RESERVED_RUNTIME_PREFIXES):
            raise FrameRefused("invalid_body", "runtime ids starting with x- or _ are reserved", at="/body/runtime")
        self._expect_key(document, f"host.{body.installation}.{body.runtime}.{body.membership_epoch}")
        issued_at, expires_at = parse_timestamp(body.issued_at), parse_timestamp(body.expires_at)
        if expires_at <= issued_at:
            raise FrameRefused("invalid_body", "a certificate must end after it starts", at="/body/expires_at")
        return HostCertificate(
            certificate_id=body.certificate_id, installation=body.installation, runtime=body.runtime,
            teams=frozenset(body.teams), roles=frozenset(body.roles), agent_types=frozenset(body.agent_types),
            membership_epoch=body.membership_epoch, revocation_epoch=body.revocation_epoch,
            key_id=body.application_key.key_id,
            public_key=Ed25519PublicKey.from_public_bytes(b64url_decode(body.application_key.public_key)),
            nats_public_key=body.nats_public_key, session_limit=body.session_limit, issued_at=issued_at, expires_at=expires_at,
            replaces=None if body.replaces is MISSING else body.replaces,
        )

    def _keep_certificate(self, document: State, body: Any, _signer: Optional[HostCertificate]) -> None:
        certificate = self._certificate(document, body)
        held = [other for other in self._certificates.values()
                if (other.installation, other.runtime) == (certificate.installation, certificate.runtime)]
        # A renewal keeps the old certificate for queued work, so it needs its own key
        # (host.<installation>.<runtime>.<membership_epoch>): its epoch is higher.
        for other in held:
            replaced, replacing = ((other, certificate) if certificate.replaces == other.certificate_id
                                   else (certificate, other) if other.replaces == certificate.certificate_id else (None, None))
            if replaced is not None and replacing is not None and replacing.membership_epoch <= replaced.membership_epoch:
                raise FrameRefused("invalid_body", "a renewed certificate has a higher membership epoch than the one it replaces",
                                   at="/body/membership_epoch")
        if certificate.replaces is not None:
            slot = (certificate.installation, certificate.runtime, certificate.replaces)
            earlier = self._replacements.get(slot)
            self._replacements[slot] = certificate.issued_at if earlier is None else min(earlier, certificate.issued_at)
        self._certificates[(certificate.installation, certificate.runtime, certificate.membership_epoch)] = certificate

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

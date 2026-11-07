"""The installation authority's work: topology, certificates, revocations and the bootstrap bundle.

Rules from the message contract's README ("Installation traffic", "Trust over time",
"Stored outcomes") and the registry's write rules for ``certificate`` and ``revocations``:

- **Certificates** last 30 days and name what the join asked for, never more. A host
  that asks again with the keys its newest certificate holds, for the same grants, gets
  that certificate back while more than a third of its life is left; otherwise, when it
  asks for other grants, and on a renewal, it gets a new one with the next membership epoch
  that ``replaces`` the old one. A renewal's certificate replaces the certificate that
  signed the renewal. A host whose key is revoked gets none. A renewal whose profile gives
  up every agent (``HostProfile.giving_up``) gets a certificate that grants no agent types
  and no sessions. Every certificate states the profile it grants (``profile_sha256``).
- **The launcher's list decides.** Only a host the launcher listed in
  ``expected-hosts.json`` (runtime, key, connection class, deployment) gets a certificate,
  for no more than the list allows: at its join, and again at each renewal, so a host the
  list no longer covers cannot extend its old grants (``not_allowed``). A renewal that only
  gives up agents is answered without the list: it needs no approval to hold less.
- **Renewals are carried out once.** A renewal's answer is stored under its retry key (the
  host's runtime and its ``renew_id``) for 7 days, before the reply; a host whose reply was
  lost retries with the same id and gets the same certificate. A renewal that rotates to the
  key of the certificate that already replaced the signing one gets that certificate.
- **Another contract** (README "Versions"). While the label is a pre-release, a join or
  renewal that names another contract digest is read for that digest before the rest of
  its body (``verify_frame`` marks it ``other_contract``) and answered ``protocol_mismatch``,
  naming the digest the authority runs, so the host learns why rather than a refusal of a
  body another contract shaped. The one exception is a renewal that only gives up the
  host's agents: the host then hands its agents over, so it is answered when this
  revision can read it (its body fits this contract, names the certificate that signs it
  and keeps its keys). There are no worker pools yet, so there is nothing else to give up.
- **Kept current by the wall clock.** Every minute the authority checks its records by the
  wall clock, not by how long it slept (a laptop or a paused virtual machine sleeps): it
  signs the revocations record again once it is six hours old (it lasts 24 hours, with a
  sequence above every one before it and at least its issue time in milliseconds), renews
  its own certificate at two thirds of its life and signs with the new one from then on,
  and forgets old certificates once a day, deleting them from ``UA1_CERTS`` (revoked ones
  stay). A step that fails is tried again a minute later. When its NATS connection has
  closed for good it stops (``AuthorityDisconnected``), so its supervisor sees it.
- **Only its own records count.** Every process shares one NATS login in this phase, so
  any of them can write ``UA1_CERTS``. The authority uses a record it reads there (the
  revocations record to sign again, a certificate to hand back) only when it verifies
  against the authority's own key; it never signs again what does not verify. It signs
  the next revocations record from the newest one it verified: the bucket's, the
  bootstrap bundle's, the copy it keeps (``ledger.IssueLedger``) or the one it signed
  last. When the bucket holds a record that does not verify and it has verified none, it
  does not start (``RevocationsUnverified``): an empty list would accept every revoked
  host again.
- **What it keeps itself.** The bucket can lose a key while the trust folder survives, so
  the authority keeps the highest epoch it issued per runtime, the renewals' answers and the
  newest revocations record in its own trust folder (``ledger.IssueLedger``). Each record
  is checked by its own trust store, then kept there, and only then written to the bucket,
  so nothing the authority would refuse itself is ever published.
- **The bootstrap bundle** carries the authority keys, the authority's own
  certificate and the newest revocations record, so a host can check a join reply
  before it has a certificate. The authority writes it just before it serves, so every
  reply it signs comes from a certificate the bundle already holds; a host that joins in
  between waits for the authority to answer (``joining.join``).
- **Topology.** The authority creates the key-value buckets, with the operator's replica
  count. The queued work streams come with queued work, and each team's dead-letter stream
  with the dead letters of phase 3 (plan decision 71).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional

import rfc8785
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from nats.js.api import StorageType, StreamConfig
from nats.js.errors import KeyNotFoundError, NoKeysError, NotFoundError
from pydantic import ValidationError

from custom_messages.wire.buckets import TEAM_PLACEHOLDER, bucket_name, bucket_stream
from custom_messages.wire.encoding import b64url, format_timestamp
from custom_messages.wire.frame import Receiver, VerifiedFrame, body_model, expected_idempotency_key
from custom_messages.wire.kinds import CONTRACT_SHA256, INSTALLATION_SCOPE, KINDS, PROTOCOL
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import CERTIFICATE, AuthorityKey, HostCertificate, TrustStore, kept_for, sign_state
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.bootstrap import BootstrapBundle, read_bundle, write_bundle
from custom_transport.protocol1.contract_files import transport
from custom_transport.protocol1.host_keys import AuthorityKeys, HostKeys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.joining import AUTHORITY_SERVICE_ID, HostProfile
from custom_transport.protocol1.live import LiveHost, Unsupported, utc_now
from custom_transport.protocol1.trust_state import PersistentTrustStore
from installation_authority.expected_hosts import ExpectedHost, read_expected_hosts
from installation_authority.ledger import IssueLedger
from release_version import release_version
from runtime_settings.instance_layout import InstanceLayout

AUTHORITY_RUNTIME = "authority"
AUTHORITY_ROLE = "installation_authority"
CERTIFICATE_LIFETIME = timedelta(days=30)
# Plan 13B.3: certificates are renewed at two thirds of their life; the authority renews its own the same way.
CERTIFICATE_RENEWAL_SHARE = 2 / 3
REVOCATIONS_LIFETIME = timedelta(hours=24)
REVOCATIONS_RESIGN_INTERVAL = timedelta(hours=6)
# How often the authority checks its records by the wall clock, and so how soon a failed
# step is tried again and how late after waking from sleep a stale record is signed again.
UPKEEP_INTERVAL = timedelta(minutes=1)
# README "Forgetting": a receiver forgets at least daily.
FORGET_INTERVAL = timedelta(days=1)
CERTIFICATES_BUCKET = "UA1_CERTS"
# common.schema.json "epoch".
MAX_EPOCH = 2**53 - 1
_INSTALLATION_ADDRESS = "_install.{sender}.svc"
_STORAGE = {"file": StorageType.FILE, "memory": StorageType.MEMORY}
_LOGGER = logging.getLogger(__name__)


def certificate_key(installation: str, runtime: str, epoch: int) -> str:
    return f"host.{installation}.{runtime}.{epoch}"


def revocations_key(installation: str) -> str:
    return f"revocations.{installation}"


class AuthorityStartRefused(RuntimeError):
    """The authority cannot start without risking trust; the message says what the operator can do."""


class RevocationsUnverified(AuthorityStartRefused):
    """The stored revocations record does not verify and the authority has verified none, so it cannot sign the next one."""


class AuthorityClockBehind(AuthorityStartRefused):
    """The newest revocations record was signed later than this machine's clock says it is now."""


class AuthorityDisconnected(RuntimeError):
    """The authority's NATS connection closed for good, so it can no longer answer or keep its records current."""


@dataclass(frozen=True)
class CertificateRequest:
    """What a certificate grants: the host's keys and the grants decided for it."""

    runtime: str
    teams: tuple[str, ...]
    roles: tuple[str, ...]
    agent_types: tuple[str, ...]
    session_limit: int
    connection_class: str
    key_id: str
    public_key: str
    nats_public_key: str
    deployment: str = "local"

    def profile_sha256(self) -> str:
        """The digest of the profile this certificate grants (README "Identifiers": the certificate states it)."""

        return HostProfile(self.runtime, self.teams, self.roles, self.agent_types, self.session_limit,
                           self.connection_class).digest()


class InstallationAuthority:
    """One installation's authority: it signs with ``authority`` and speaks as the host ``host_keys``."""

    def __init__(self, layout: InstanceLayout, authority: AuthorityKeys, host_keys: HostKeys, *,
                 clock: Callable[[], datetime] = utc_now, logger: logging.Logger = _LOGGER,
                 contract_sha256: str = CONTRACT_SHA256, replicas: int = 1) -> None:
        """``contract_sha256`` is the contract this authority runs: its code's, unless a test gives another.

        ``replicas`` is the operator's JetStream replica count (``transport.replicas``) for the buckets.
        """

        if host_keys.runtime != AUTHORITY_RUNTIME:
            raise ValueError(f"the authority speaks as host {AUTHORITY_RUNTIME!r}")
        self.contract_sha256 = contract_sha256
        self.layout = layout
        self.authority = authority
        self.host_keys = host_keys
        self.clock = clock
        self.logger = logger
        self.replicas = max(1, int(replicas))
        self.live: Optional[LiveHost] = None
        self._nc: Any = None
        self._js: Any = None
        self._own: dict[str, Any] = {}
        self._revocations: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._forgotten_at: Optional[datetime] = None
        self._ledger = IssueLedger(layout.host_trust_dir(AUTHORITY_RUNTIME) / "issued.json", self.installation,
                                   logger=logger)
        # The authority host's trust store, opened first at start: it keeps the newest
        # revocations record across restarts and serves the live host afterwards.
        self._trust: Optional[PersistentTrustStore] = None

    @property
    def installation(self) -> str:
        return self.authority.installation

    # -- signed records --------------------------------------------------------------------------

    def certificate_record(self, request: CertificateRequest, *, epoch: int, revocation_epoch: int, issued: datetime,
                           replaces: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "certificate_id": f"cert.{request.runtime}.{epoch}",
            "installation": self.installation,
            "runtime": request.runtime,
            "teams": sorted(request.teams),
            "roles": sorted(request.roles),
            "membership_epoch": epoch,
            "revocation_epoch": revocation_epoch,
            "application_key": {"key_id": request.key_id, "public_key": request.public_key},
            "nats_public_key": request.nats_public_key,
            "agent_types": sorted(request.agent_types),
            "session_limit": request.session_limit,
            "connection_class": request.connection_class,
            "protocol": PROTOCOL,
            "deployment": request.deployment,
            "profile_sha256": request.profile_sha256(),
            "software": {"name": "universal-agents", "version": release_version()},
            "issued_at": format_timestamp(issued),
            "expires_at": format_timestamp(issued + CERTIFICATE_LIFETIME),
        }
        if replaces is not None:
            body["replaces"] = replaces
        document = {"record": "certificate", "protocol": PROTOCOL,
                    "key": certificate_key(self.installation, request.runtime, epoch),
                    "writer": {"type": "authority", "installation": self.installation, "key_id": self.authority.key_id},
                    "sequence": 1, "issued_at": format_timestamp(issued), "body": body}
        return sign_state(document, self.authority.private_key)

    def revocations_record(self, *, sequence: int, revocation_epoch: int, entries: Iterable[Mapping[str, Any]],
                           issued: datetime) -> dict[str, Any]:
        document = {"record": "revocations", "protocol": PROTOCOL, "key": revocations_key(self.installation),
                    "writer": {"type": "authority", "installation": self.installation, "key_id": self.authority.key_id},
                    "sequence": sequence, "issued_at": format_timestamp(issued),
                    "body": {"installation": self.installation, "revocation_epoch": revocation_epoch,
                             "entries": [dict(entry) for entry in entries],
                             "expires_at": format_timestamp(issued + REVOCATIONS_LIFETIME)}}
        return sign_state(document, self.authority.private_key)

    # -- starting -------------------------------------------------------------------------------

    async def start(self, nc: Any, *, teams: Iterable[str]) -> None:
        """Set up the topology for ``teams``, the authority's own certificate and revocations, then answer joins.

        The bundle is written before the authority serves, so it holds the certificate
        of every reply; the launcher waits for it.
        """

        self._nc = nc
        self._js = nc.jetstream()
        teams = sorted(set(teams))
        self._ledger = IssueLedger.open(self.layout.host_trust_dir(AUTHORITY_RUNTIME), self.installation,
                                        logger=self.logger)
        await self.ensure_topology(teams)
        now = self.clock()
        trust = self._trust = PersistentTrustStore.open(read_bundle(self.layout.trust_bootstrap_path),
                                                        self.layout.host_trust_dir(AUTHORITY_RUNTIME), now)
        trust.store.contract_sha256 = self.contract_sha256
        revocations = await self._sign_revocations(now)
        self._own = await self._own_certificate(teams, int(revocations["body"]["revocation_epoch"]), now)
        certificate = self._held_certificate(self._own)
        self._publish_bundle()
        self.live = LiveHost(nc, HostIdentity(self.host_keys, certificate), trust, clock=self.clock, logger=self.logger)
        await self._take_issued_certificates(now)
        await self.live.serve(_INSTALLATION_ADDRESS, Receiver(self.installation, INSTALLATION_SCOPE, "installation_service",
                                                              AUTHORITY_SERVICE_ID, AUTHORITY_RUNTIME), self.handle)
        await nc.flush()
        self.logger.info("The installation authority is serving. event=installation_authority_ready installation=%s "
                         "membership_epoch=%s teams=%s", self.installation, certificate.membership_epoch, ",".join(teams))

    async def ensure_topology(self, teams: Iterable[str]) -> None:
        """Create what the contract's topology owner owns in phase 2: the key-value buckets."""

        spec = transport()
        teams = tuple(teams)
        for bucket in spec["key_value"]:
            for team in teams if TEAM_PLACEHOLDER in bucket else (None,):
                await self._ensure_bucket(spec, bucket, team)

    async def _ensure_bucket(self, spec: Mapping[str, Any], bucket: str, team: Optional[str]) -> None:
        """Create or reconcile the contract's stream, without bucket-wide rollup or deletion.

        The client's bucket-creation helper enables rollup headers, which would
        let a writer of one key erase other keys. Bind it as a bucket only after
        creating its stream with the contract's restrictions.
        """

        settings = bucket_stream(spec, bucket, team)
        settings["storage"] = _STORAGE[settings["storage"]]
        settings["num_replicas"] = self.replicas
        config = StreamConfig(**settings)
        try:
            await self._js.stream_info(config.name)
        except NotFoundError:
            await self._js.add_stream(config=config)
            self.logger.info("Created a key-value bucket. event=protocol1_bucket_created bucket=%s replicas=%s",
                             bucket_name(bucket, team), self.replicas)
        else:
            await self._js.update_stream(config=config)

    async def _records(self) -> Any:
        return await self._js.key_value(CERTIFICATES_BUCKET)

    async def _stored_data(self, key: str) -> Optional[bytes]:
        try:
            entry = await (await self._records()).get(key)
        except KeyNotFoundError:
            return None
        return entry.value or None

    async def _bucket_keys(self, prefix: str) -> list[str]:
        try:
            return [key for key in await (await self._records()).keys() if key.startswith(prefix)]
        except (NoKeysError, NotFoundError):  # the bucket holds no keys yet
            return []

    def _own_record(self, data: bytes, record: str, key: str, now: datetime) -> dict[str, Any]:
        """``data`` as a ``record`` under ``key`` that this authority signed; raises ``FrameRefused`` otherwise.

        It is checked as every receiver checks a trust record (``TrustStore.add``), by a
        store that trusts the authority's own key alone, so nothing another process wrote
        to the bucket is used or signed again.
        """

        own_key = AuthorityKey(self.installation, self.authority.key_id, self.authority.private_key.public_key())
        TrustStore(self.installation, [own_key]).add(data, now=now)
        document: dict[str, Any] = json.loads(data)
        if document["record"] != record or document["key"] != key:
            raise FrameRefused("invalid_body", f"a {document['record']} record of {document['key']} is stored under {key}",
                               at="/key")
        return document

    async def _stored(self, record: str, key: str, now: datetime) -> Optional[dict[str, Any]]:
        """The ``record`` this authority wrote under ``key``; ``None`` when absent or when it does not verify (logged)."""

        data = await self._stored_data(key)
        if data is None:
            return None
        try:
            return self._own_record(data, record, key, now)
        except FrameRefused as refused:
            self.logger.error("A stored record does not verify against the authority's key and is not used. "
                              "event=stored_record_refused key=%s code=%s at=%s", key, refused.code, refused.at)
            return None

    # -- revocations ------------------------------------------------------------------------------

    def _newest_revocations(self, stored: Optional[bytes], now: datetime) -> Optional[dict[str, Any]]:
        """The newest revocations record this authority signed: the one it signed last, its kept copy, the bundle's
        or the bucket's (``stored``).

        Returns ``None`` only when there is no record at all, on the first start. Raises
        ``AuthorityClockBehind`` when its newest records were signed later than this
        machine's clock says it is, and ``RevocationsUnverified`` when the bucket holds a
        record that does not verify and none verified: signing an empty list would accept
        every revoked host again.
        """

        key = revocations_key(self.installation)
        held: list[Mapping[str, Any]] = [record for record in (self._revocations, self._ledger.revocations) if record]
        held += [record for record in read_bundle(self.layout.trust_bootstrap_path).records
                 if record.get("record") == "revocations"]
        verified: list[dict[str, Any]] = []
        ahead: list[Mapping[str, Any]] = []
        for candidate in held:
            self._sort_revocations(_encoded(candidate), now, verified, ahead, from_bucket=False)
        refused_stored = None if stored is None else self._sort_revocations(stored, now, verified, ahead,
                                                                             from_bucket=True)
        if verified:
            return max(verified, key=lambda record: int(record["sequence"]))
        if ahead:
            signed_at = max(str(record.get("issued_at", "")) for record in ahead)
            raise AuthorityClockBehind(
                f"The newest revocations record {key} was signed at {signed_at}, later than this machine's clock "
                f"({format_timestamp(now)}). Correct the clock; the authority starts once the clock has passed that "
                "time. Nothing needs to be deleted.")
        if refused_stored is not None:
            raise RevocationsUnverified(
                f"The stored revocations record {key} does not verify ({refused_stored.code}) and the authority holds no "
                "verified copy (its issue record, the bootstrap bundle and the bucket), so it cannot tell which hosts "
                "are revoked and does not start. Restore the instance's trust folder from the backup taken with the "
                f"NATS data, or delete {key} from the {CERTIFICATES_BUCKET} bucket to start an empty list, which "
                "forgets every revocation.")
        return None

    def _sort_revocations(self, data: bytes, now: datetime, verified: list[dict[str, Any]],
                          ahead: list[Mapping[str, Any]], *, from_bucket: bool) -> Optional[FrameRefused]:
        """Add a revocations record to ``verified`` when this authority signed it, or to ``ahead`` when it is dated
        later than ``now``; return why it was refused, logged."""

        key = revocations_key(self.installation)
        try:
            verified.append(self._own_record(data, "revocations", key, now))
            return None
        except FrameRefused as refused:
            if refused.code == "not_yet_valid":
                ahead.append(json.loads(data))
            if from_bucket:
                self.logger.error("The stored revocations record does not verify against the authority's key and is "
                                  "not signed again. event=stored_revocations_refused key=%s code=%s at=%s held=%s",
                                  key, refused.code, refused.at, bool(verified))
            else:
                self.logger.warning("A held revocations record does not verify and is not used. "
                                    "event=held_revocations_refused key=%s code=%s at=%s", key, refused.code, refused.at)
            return refused

    async def _sign_revocations(self, now: datetime) -> dict[str, Any]:
        """The next revocations record: the newest verified one's entries, a higher sequence, and a day's life.

        Its own trust store takes it first and the issue record keeps it, then the bucket
        gets it: a record that would lower what the authority holds is never published.
        """

        async with self._lock:
            held = self._newest_revocations(await self._stored_data(revocations_key(self.installation)), now)
            entries = held["body"]["entries"] if held else []
            epoch = int(held["body"]["revocation_epoch"]) if held else 1
            sequence = max(int(held["sequence"]) + 1 if held else 1, int(now.timestamp() * 1000))
            record = self.revocations_record(sequence=sequence, revocation_epoch=epoch, entries=entries, issued=now)
            assert self._trust is not None, "start() opens the trust store first"
            try:
                kept = self._trust.add(_encoded(record), now=now)
            except FrameRefused as refused:
                kept, reason = False, refused.code
            else:
                reason = "older_than_held"
            if not kept:
                raise AuthorityStartRefused(
                    f"The authority's own trust store holds a newer revocations record than any copy it can read "
                    f"({reason}), so signing from those copies could lift a revocation. Restore the instance's trust "
                    "folder from the backup taken with the NATS data.")
            self._ledger.keep_revocations(record)
            self._ledger.save(now)
            await (await self._records()).put(revocations_key(self.installation), _encoded(record))
            self._revocations = record
            if self._own:
                self._publish_bundle()
            return record

    def _publish_bundle(self) -> BootstrapBundle:
        """Write the bundle with the authority's certificate and the newest revocations record."""

        bundle = read_bundle(self.layout.trust_bootstrap_path).with_records([self._own, self._revocations])
        write_bundle(self.layout.trust_bootstrap_path, bundle)
        return bundle

    # -- keeping current --------------------------------------------------------------------------

    async def keep_current(self, *, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        """Check the authority's records every minute by the wall clock, until its NATS connection closes for good."""

        while True:
            await sleep(UPKEEP_INTERVAL.total_seconds())
            await self.upkeep(self.clock())

    async def upkeep(self, now: datetime) -> None:
        """Sign what is due at ``now``: revocations six hours old, its own certificate at two thirds of its life,
        and the daily forgetting. A step that fails is logged and tried again at the next check.

        Raises ``AuthorityDisconnected`` once the NATS client gave up reconnecting.
        """

        if self._nc is not None and self._nc.is_closed:
            raise AuthorityDisconnected("The installation authority's NATS connection closed and will not reconnect.")
        steps: tuple[tuple[bool, Callable[[datetime], Any], str], ...] = (
            (self._revocations_due(now), self._sign_revocations, "revocations_resign_failed"),
            (self._own_certificate_due(now), self._renew_own_certificate, "authority_certificate_renewal_failed"),
            (self._forget_due(now), self.forget, "authority_forget_failed"),
        )
        for due, step, event in steps:
            if not due:
                continue
            try:
                await step(now)
            except Exception:
                self.logger.exception("An authority upkeep step failed; it is tried again in %s seconds. event=%s "
                                      "installation=%s", int(UPKEEP_INTERVAL.total_seconds()), event, self.installation)

    def _revocations_due(self, now: datetime) -> bool:
        if not self._revocations:
            return True
        return now - parse_timestamp(self._revocations["issued_at"]) >= REVOCATIONS_RESIGN_INTERVAL

    def _own_certificate_due(self, now: datetime) -> bool:
        if not self._own:
            return False
        issued, expires = parse_timestamp(self._own["body"]["issued_at"]), parse_timestamp(self._own["body"]["expires_at"])
        return now - issued >= (expires - issued) * CERTIFICATE_RENEWAL_SHARE

    def _forget_due(self, now: datetime) -> bool:
        return self._forgotten_at is None or now - self._forgotten_at >= FORGET_INTERVAL

    async def _renew_own_certificate(self, now: datetime) -> None:
        """Replace the authority's own certificate and sign with the new one from now on.

        It goes to ``UA1_CERTS``, where running hosts read a certificate a frame names and
        they do not hold yet, and to the bootstrap bundle, which joining hosts read. The
        old one still signs for the overlap (5 minutes), so replies on their way stay valid.
        """

        assert self.live is not None, "start() first"
        current = self._own
        request = self._own_request(tuple(current["body"]["teams"]))
        record = await self._issue(request, replaces=str(current["body"]["certificate_id"]),
                                   after_epoch=int(current["body"]["membership_epoch"]),
                                   revocation_epoch=int(self._revocations["body"]["revocation_epoch"]), now=now)
        self._own = record
        self.live.identity = self.live.identity.with_certificate(self._held_certificate(record))
        self._publish_bundle()
        self.logger.info("The authority renewed its own certificate. event=authority_certificate_renewed "
                         "membership_epoch=%s expires_at=%s", record["body"]["membership_epoch"],
                         record["body"]["expires_at"])

    async def forget(self, now: datetime) -> int:
        """Forget old trust records, and delete the certificates receivers forget from ``UA1_CERTS``; return how many.

        README "Forgetting": writers delete a record once receivers forget it and keep revoked
        certificates. Epochs never come back: the issue record keeps the highest one issued.
        """

        assert self._trust is not None, "start() first"
        self._forgotten_at = now
        forgotten = self._trust.store.forget(now)
        kept = kept_for(CERTIFICATE)
        assert kept is not None
        bucket = await self._records()
        deleted = 0
        for key in await self._bucket_keys(f"host.{self.installation}."):
            data = await self._stored_data(key)
            try:
                document = json.loads(data or b"")
                issued, body = parse_timestamp(str(document["issued_at"])), document["body"]
            except (ValueError, KeyError, TypeError):
                continue  # not a record this authority could have written; it reads nothing from it
            if document.get("record") != CERTIFICATE or now - issued < kept or self._listed(body):
                continue
            await bucket.delete(key)
            deleted += 1
        self.logger.info("The authority forgot old trust records. event=authority_records_forgotten forgotten=%s "
                         "deleted=%s", forgotten, deleted)
        return deleted

    def _listed(self, body: Mapping[str, Any]) -> bool:
        """Whether the revocations record lists this certificate or its key."""

        key_id = body.get("application_key", {}).get("key_id") if isinstance(body.get("application_key"), dict) else None
        for entry in self._revocations.get("body", {}).get("entries", []):
            if entry.get("certificate_id") == body.get("certificate_id") or (
                    entry.get("runtime") == body.get("runtime") and entry.get("key_id") == key_id):
                return True
        return False

    # -- certificates -----------------------------------------------------------------------------

    async def _take_issued_certificates(self, now: datetime) -> None:
        """Hold every certificate this authority issued, so it recognises the hosts that renew with them."""

        assert self.live is not None
        for key in await self._bucket_keys(f"host.{self.installation}."):
            stored = await self._stored("certificate", key, now)
            if stored is None:
                continue
            try:
                self.live.trust.add(_encoded(stored), now=now)
            except FrameRefused as refused:
                # An ended or revoked certificate no longer counts; renewals with it are refused.
                self.logger.info("A stored certificate is not taken. event=stored_certificate_skipped key=%s code=%s",
                                 key, refused.code)

    async def _latest_certificate(self, runtime: str, now: datetime) -> Optional[dict[str, Any]]:
        """The stored certificate of ``runtime`` with the highest membership epoch that this authority signed.

        A key whose record does not verify is skipped and counts for nothing: anyone with
        the shared login can write one, at any epoch.
        """

        prefix = f"host.{self.installation}.{runtime}."
        epochs = sorted((int(key.rsplit(".", 1)[1]) for key in await self._bucket_keys(prefix)
                         if key.rsplit(".", 1)[1].isdigit()), reverse=True)
        for epoch in epochs:
            stored = await self._stored("certificate", certificate_key(self.installation, runtime, epoch), now)
            if stored is not None:
                return stored
        return None

    def _newest_epoch(self, runtime: str, *records: Optional[Mapping[str, Any]]) -> int:
        """The highest membership epoch issued to ``runtime``: of ``records`` and of the issue record."""

        return max([self._ledger.highest_epoch(runtime),
                    *(int(record["body"]["membership_epoch"]) for record in records if record is not None)])

    def _reusable(self, stored: Optional[Mapping[str, Any]], key_id: str, public_key: str, nats_public_key: str,
                  now: datetime) -> bool:
        """Whether a host asking again can keep its newest certificate: same keys, and over a third of its life left.

        Only the newest certificate issued to the runtime is handed back: an older one was
        replaced, and the authority itself refuses it once the overlap has passed.
        """

        if stored is None:
            return False
        body = stored["body"]
        issued, expires = parse_timestamp(body["issued_at"]), parse_timestamp(body["expires_at"])
        return ((body["application_key"]["key_id"], body["application_key"]["public_key"]) == (key_id, public_key)
                and body["nats_public_key"] == nats_public_key
                and int(body["membership_epoch"]) >= self._ledger.highest_epoch(str(body["runtime"]))
                and expires - now > (expires - issued) / 3)

    def _own_request(self, teams: Iterable[str]) -> CertificateRequest:
        return CertificateRequest(runtime=AUTHORITY_RUNTIME, teams=tuple(sorted(teams)), roles=(AUTHORITY_ROLE,),
                                  agent_types=(), session_limit=0, connection_class="connected",
                                  key_id=self.host_keys.key_id, public_key=self.host_keys.public_key_text,
                                  nats_public_key=self.host_keys.nats_public_key)

    async def _own_certificate(self, teams: list[str], revocation_epoch: int, now: datetime) -> dict[str, Any]:
        """The authority's certificate at start: its newest one while reusable, or a new one.

        The newest is the bucket's or the bundle's: a bucket that lost it gets it back.
        """

        stored = await self._latest_certificate(AUTHORITY_RUNTIME, now)
        bundled = self._bundled_own_certificates(now)
        held = max([record for record in (stored, *bundled) if record is not None],
                   key=lambda record: int(record["body"]["membership_epoch"]), default=None)
        if self._reusable(held, self.host_keys.key_id, self.host_keys.public_key_text, self.host_keys.nats_public_key,
                          now) and held is not None and set(held["body"]["teams"]) == set(teams or held["body"]["teams"]):
            try:
                await self._store_certificate(held)
            except FrameRefused as refused:
                # Its trust store holds another certificate under that epoch (an older bundle):
                # a new epoch keeps every holder's copy unambiguous.
                self.logger.warning("The authority's newest certificate conflicts with one it holds; it gets a new "
                                    "one. event=authority_certificate_conflict key=%s code=%s", held["key"], refused.code)
            else:
                if held is not stored:
                    self.logger.warning("The bucket lacked the authority's certificate; it was written again from the "
                                        "bundle. event=authority_certificate_restored key=%s", held["key"])
                return held
        replaces = held["body"]["certificate_id"] if held and parse_timestamp(held["body"]["expires_at"]) > now else None
        return await self._issue(self._own_request(teams or (held["body"]["teams"] if held else ())),
                                 replaces=replaces, after_epoch=self._newest_epoch(AUTHORITY_RUNTIME, held),
                                 revocation_epoch=revocation_epoch, now=now)

    def _bundled_own_certificates(self, now: datetime) -> list[dict[str, Any]]:
        """The authority's own certificates in the bootstrap bundle that it signed and that have not ended."""

        bundled = []
        for record in read_bundle(self.layout.trust_bootstrap_path).records:
            body = record.get("body")
            if record.get("record") != CERTIFICATE or not isinstance(body, dict) or body.get("runtime") != AUTHORITY_RUNTIME:
                continue
            try:
                bundled.append(self._own_record(_encoded(record), CERTIFICATE, str(record.get("key")), now))
            except FrameRefused:
                continue  # an ended or foreign certificate in the bundle is not the authority's to reuse
        return bundled

    def _held_certificate(self, record: Mapping[str, Any]) -> HostCertificate:
        assert self._trust is not None
        body = record["body"]
        certificate = self._trust.store.host_certificate(self.installation, str(body["runtime"]),
                                                         int(body["membership_epoch"]))
        assert certificate is not None  # it verified against the authority's own key when it was taken
        return certificate

    async def _store_certificate(self, record: Mapping[str, Any]) -> None:
        """Write a certificate this authority signed to the bucket, unless the bucket holds exactly it."""

        assert self._trust is not None
        self._trust.add(_encoded(record), now=self.clock())
        key = str(record["key"])
        data = await self._stored_data(key)
        try:
            same = data is not None and json.loads(data) == dict(record)
        except ValueError:
            same = False
        if not same:
            await (await self._records()).put(key, _encoded(record))

    async def _issue(self, request: CertificateRequest, *, replaces: Optional[str], after_epoch: int,
                     revocation_epoch: int, now: datetime, renewal: Optional[tuple[str, str]] = None) -> dict[str, Any]:
        """Sign a certificate at the epoch above ``after_epoch`` and every one issued, keep it, then publish it.

        Its own trust store takes it, the issue record keeps its epoch (and a renewal's
        answer under ``renewal``: retry key and body digest), and only then the bucket
        gets it, so a lost reply or a lost bucket never makes an epoch name two certificates.
        """

        assert self._trust is not None, "start() opens the trust store first"
        epoch = max(after_epoch, self._ledger.highest_epoch(request.runtime)) + 1
        if epoch > MAX_EPOCH:
            raise ValueError(f"no membership epoch is left for {request.runtime}")
        record = self.certificate_record(request, epoch=epoch, revocation_epoch=revocation_epoch, issued=now,
                                         replaces=replaces)
        self._ledger.take_epoch(request.runtime, epoch)
        self._trust.add(_encoded(record), now=now)
        if renewal is not None:
            self._ledger.store_renewal(renewal[0], record, renewal[1], now)
        self._ledger.save(now)
        await (await self._records()).put(certificate_key(self.installation, request.runtime, epoch), _encoded(record))
        self.logger.info("Issued a host certificate. event=host_certificate_issued runtime=%s membership_epoch=%s "
                         "key_id=%s replaces=%s", request.runtime, epoch, request.key_id, replaces)
        return record

    # -- answering ------------------------------------------------------------------------------

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        if verified.other_contract:
            return await self.answer_other_contract(verified)
        if verified.frame.kind == "host.join":
            return await self.answer_join(verified)
        if verified.frame.kind == "host.renew":
            return await self.answer_renew(verified)
        raise Unsupported(verified.frame.kind)

    def _protocol_mismatch(self) -> dict[str, Any]:
        """The answer to a host on another pre-release contract: the digest this authority runs, and what to install."""

        return {"outcome": "protocol_mismatch", "accepted": PROTOCOL, "accepted_contract_sha256": self.contract_sha256,
                "install": f"universal-agents {release_version()}",
                "message": "This host runs another pre-release contract than the installation; install the same release."}

    async def answer_other_contract(self, verified: VerifiedFrame) -> dict[str, Any]:
        """A join or renewal from a host on another contract: ``protocol_mismatch``, unless it only gives up agents."""

        runtime = verified.frame.issuer.runtime
        if verified.frame.kind == "host.renew" and self._readable_give_up(verified):
            self.logger.warning("A host on another contract gave up its agents; its renewal is answered. "
                                "event=host_renewal_gave_up_agents runtime=%s their_contract_sha256=%s "
                                "contract_sha256=%s", runtime, verified.their_contract_sha256, self.contract_sha256)
            return await self._renew(verified, give_up=True)
        self.logger.warning("A host on another contract was turned away. event=host_protocol_mismatch runtime=%s kind=%s "
                            "their_contract_sha256=%s contract_sha256=%s", runtime, verified.frame.kind,
                            verified.their_contract_sha256, self.contract_sha256)
        return self._protocol_mismatch()

    def _readable_give_up(self, verified: VerifiedFrame) -> bool:
        """Whether a renewal of another contract is one this revision reads and that only gives up agents.

        Its body was not read when it was verified, so it is read here: it fits this
        contract's renewal (schema and retry key), names the certificate that signs it, keeps
        the host's keys and asks for the profile that gives up every agent.
        """

        certificate = verified.certificate
        assert certificate is not None  # a renewal is signed by the certificate it renews
        body = verified.raw["body"]
        try:
            body_model("host.renew")[0].validate_python(body)
        except ValidationError:
            return False
        return (verified.frame.idempotency_key == expected_idempotency_key(verified.raw, KINDS["host.renew"], False, None)
                and body["certificate_id"] == certificate.certificate_id
                and body["membership_epoch"] == certificate.membership_epoch
                and "application_key_rotation" not in body
                and body.get("new_nats_public_key", certificate.nats_public_key) == certificate.nats_public_key
                and _gives_up(certificate, body))

    async def answer_join(self, verified: VerifiedFrame) -> dict[str, Any]:
        body = verified.raw["body"]
        runtime, key = str(body["runtime"]), body["application_key"]
        expected = read_expected_hosts(self.layout.trust_expected_hosts_path).get(runtime)
        denial = _denial(expected, body)
        if denial is not None:
            self.logger.warning("A join was refused. event=host_join_denied runtime=%s reason=%s key_id=%s "
                                "connection_class=%s deployment=%s", runtime, denial, key["key_id"],
                                body["connection_class"], body["deployment"])
            return {"outcome": "denied", "reason": denial,
                    "message": "This installation takes only the hosts its launcher starts, as the launcher lists them."}
        # verify_frame already refused a join presenting a revoked key (README "Joining").
        now = self.clock()
        stored = await self._latest_certificate(runtime, now)
        if self._reusable(stored, key["key_id"], key["public_key"], str(body["nats_public_key"]), now) \
                and stored is not None and _grants_exactly(stored["body"], body):
            return {"outcome": "ok", "certificate": stored}
        request = CertificateRequest(runtime=runtime, teams=tuple(body["teams"]), roles=tuple(body["roles"]),
                                     agent_types=tuple(body["agent_types"]), session_limit=int(body["session_limit"]),
                                     connection_class=str(body["connection_class"]), key_id=key["key_id"],
                                     public_key=key["public_key"], nats_public_key=str(body["nats_public_key"]),
                                     deployment=str(body["deployment"]))
        if request.profile_sha256() != body["profile_sha256"]:
            self.logger.info("A join's profile digest differs from the profile granted; the certificate states the "
                             "granted one. event=host_join_profile_differs runtime=%s", runtime)
        replaces = stored["body"]["certificate_id"] if stored and parse_timestamp(stored["body"]["expires_at"]) > now \
            else None
        record = await self._issue(request, replaces=replaces, after_epoch=self._newest_epoch(runtime, stored),
                                   revocation_epoch=int(self._revocations["body"]["revocation_epoch"]), now=now)
        return {"outcome": "ok", "certificate": record}

    async def answer_renew(self, verified: VerifiedFrame) -> dict[str, Any]:
        certificate = verified.certificate
        assert certificate is not None  # a renewal is signed by the certificate it renews
        return await self._renew(verified, give_up=_gives_up(certificate, verified.raw["body"]))

    async def _renew(self, verified: VerifiedFrame, *, give_up: bool) -> dict[str, Any]:
        """The renewed certificate: the grants of the one that signed the renewal, or none of its agents when it gives
        them up; the stored answer when the renewal was already carried out."""

        body = verified.raw["body"]
        certificate = verified.certificate
        assert certificate is not None and self.live is not None  # a renewal is signed by the certificate it renews
        now = self.clock()
        retry_key, body_sha256 = verified.frame.idempotency_key, _body_sha256(body)
        stored = self._ledger.renewal(retry_key, now)
        if stored is not None:
            if stored.body_sha256 != body_sha256:
                # README "Stored outcomes": a sender defect; the stored answer stands.
                self.logger.warning("A renewal repeated its retry key with another body; the stored answer is given. "
                                    "event=host_renewal_retry_other_body runtime=%s stored_body_sha256=%s "
                                    "body_sha256=%s", certificate.runtime, stored.body_sha256, body_sha256)
            await self._store_certificate(stored.certificate)
            return {"outcome": "ok", "certificate": dict(stored.certificate)}
        rotation = body.get("application_key_rotation")
        if rotation:
            issued = await self._rotation_already_issued(certificate, rotation["key"], now)
            if issued is not None:
                return {"outcome": "ok", "certificate": issued}
        key = rotation["key"] if rotation else {"key_id": certificate.key_id, "public_key": _public_text(certificate)}
        expected = read_expected_hosts(self.layout.trust_expected_hosts_path).get(certificate.runtime)
        if not give_up:
            refusal = _renewal_refusal(expected, certificate, key)
            if refusal is not None:
                self.logger.warning("A renewal was refused: the launcher's list does not cover it. "
                                    "event=host_renewal_not_allowed runtime=%s membership_epoch=%s reason=%s",
                                    certificate.runtime, certificate.membership_epoch, refusal)
                return {"outcome": "not_allowed", "message": refusal}
        agent_types = () if give_up else tuple(sorted(certificate.agent_types))
        request = CertificateRequest(runtime=certificate.runtime, teams=tuple(sorted(certificate.teams)),
                                     roles=tuple(sorted(certificate.roles)), agent_types=agent_types,
                                     session_limit=0 if give_up else certificate.session_limit,
                                     connection_class=certificate.connection_class,
                                     key_id=key["key_id"], public_key=key["public_key"],
                                     nats_public_key=str(body.get("new_nats_public_key") or certificate.nats_public_key),
                                     deployment=expected.deployment if expected is not None else "local")
        if request.profile_sha256() != body["profile_sha256"]:
            self.logger.info("A renewal asked for another profile than the one it keeps; the certificate states the "
                             "granted one. event=host_renewal_profile_differs runtime=%s", certificate.runtime)
        latest = await self._latest_certificate(certificate.runtime, now)
        record = await self._issue(request, replaces=certificate.certificate_id,
                                   after_epoch=max(certificate.membership_epoch, self._newest_epoch(certificate.runtime, latest)),
                                   revocation_epoch=int(self._revocations["body"]["revocation_epoch"]), now=now,
                                   renewal=(retry_key, body_sha256))
        return {"outcome": "ok", "certificate": record}

    async def _rotation_already_issued(self, certificate: HostCertificate, key: Mapping[str, Any],
                                       now: datetime) -> Optional[dict[str, Any]]:
        """The certificate that already replaced ``certificate`` with the rotated ``key``, as stored; ``None`` otherwise.

        README "Application key rotation": a retry after a lost reply names that key, and the
        authority answers it with that certificate.
        """

        assert self._trust is not None
        for other in self._trust.store.certificates_with_key(certificate.installation, str(key.get("key_id"))):
            if (other.runtime, other.replaces) != (certificate.runtime, certificate.certificate_id) \
                    or _public_text(other) != key.get("public_key"):
                continue
            stored = await self._stored(CERTIFICATE, certificate_key(self.installation, other.runtime,
                                                                     other.membership_epoch), now)
            if stored is not None:
                self.logger.info("A rotation retry gets the certificate already issued. event=host_rotation_retried "
                                 "runtime=%s membership_epoch=%s", other.runtime, other.membership_epoch)
                return stored
        return None


def _encoded(record: Mapping[str, Any]) -> bytes:
    return json.dumps(record).encode()


def _body_sha256(body: Mapping[str, Any]) -> str:
    return b64url(hashlib.sha256(rfc8785.dumps(dict(body))).digest())


def _gives_up(certificate: HostCertificate, body: Mapping[str, Any]) -> bool:
    """Whether a renewal asks for the certificate's profile without any agent (``HostProfile.giving_up``)."""

    granted = HostProfile(certificate.runtime, tuple(certificate.teams), tuple(certificate.roles),
                          tuple(certificate.agent_types), certificate.session_limit, certificate.connection_class)
    return body.get("profile_sha256") == granted.giving_up().digest()


def _public_text(certificate: HostCertificate) -> str:
    return b64url(certificate.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw))


def _denial(expected: Optional[ExpectedHost], body: Mapping[str, Any]) -> Optional[str]:
    """Why a join is refused, as the reply's reason, or ``None`` when the launcher listed exactly this host."""

    if expected is None:
        return "not_approved"
    key = body["application_key"]
    if (expected.key_id, expected.public_key) != (key["key_id"], key["public_key"]):
        return "key_mismatch"
    if not set(body["teams"]) <= expected.teams:
        return "team_not_allowed"
    if not set(body["roles"]) <= expected.roles or not set(body["agent_types"]) <= expected.agent_types:
        return "role_not_allowed"
    if int(body["session_limit"]) > expected.session_limit:
        return "over_limit"
    # README "Connection class and tools": an administrator approves the connection class;
    # here the launcher's list does, and the deployment it starts the host in.
    if (body["connection_class"], body["deployment"]) != (expected.connection_class, expected.deployment):
        return "not_approved"
    return None


def _renewal_refusal(expected: Optional[ExpectedHost], certificate: HostCertificate,
                     key: Mapping[str, Any]) -> Optional[str]:
    """Why the launcher's list no longer covers a renewal, as the reply's message; ``None`` when it does.

    A renewal keeps the grants of the certificate it replaces (README "Renewing"), so they
    are checked against the list as a join's are: a host the list no longer covers must
    join again, for what the list now allows.
    """

    if expected is None:
        return "The launcher no longer lists this host; it must join again."
    if (expected.key_id, expected.public_key) != (key.get("key_id"), key.get("public_key")):
        return "The launcher lists another key for this host; it must join again with that key."
    if not (certificate.teams <= expected.teams and certificate.roles <= expected.roles
            and certificate.agent_types <= expected.agent_types and certificate.session_limit <= expected.session_limit
            and certificate.connection_class == expected.connection_class):
        return "The launcher now lists fewer grants than this certificate holds; the host must join again."
    return None


def _grants_exactly(certificate: Mapping[str, Any], asked: Mapping[str, Any]) -> bool:
    """Whether a stored certificate grants just what a join now asks for, so it may be handed back.

    A join asking for more (a new specialist type for the team lead's host) or for less
    gets a new certificate with the next membership epoch, which replaces the old one.
    """

    return (set(certificate["teams"]) == set(asked["teams"]) and set(certificate["roles"]) == set(asked["roles"])
            and set(certificate["agent_types"]) == set(asked["agent_types"])
            and int(certificate["session_limit"]) == int(asked["session_limit"])
            and certificate["connection_class"] == asked["connection_class"]
            and certificate["deployment"] == asked["deployment"])

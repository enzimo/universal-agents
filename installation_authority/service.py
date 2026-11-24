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
  stay). A step that fails is tried again a minute later, except the forgetting, which
  notes the day it ran before it starts and so runs again the next day. When its NATS
  connection has closed for good it stops (``AuthorityDisconnected``), so its supervisor sees it.
- **Only its own records count.** Every process shares one NATS login in this phase, so
  any of them can write ``UA1_CERTS``. The authority uses a record it reads there (the
  revocations record to sign again, a certificate to hand back) only when it verifies
  against the authority's own key; it never signs again what does not verify. It signs
  the next revocations record from the newest one it verified: the bucket's, the
  bootstrap bundle's, the copy it keeps (``ledger.IssueLedger``), the ones its trust
  store keeps (``trust-state.json``) or the one it signed last. When the bucket holds a record that does not verify and it has verified none, it
  does not start (``RevocationsUnverified``): an empty list would accept every revoked
  host again.
- **What it keeps itself.** The bucket can lose a key while the trust folder survives, so
  the authority keeps the highest epoch it issued per runtime, the renewals' answers and the
  newest revocations record in its own trust folder (``ledger.IssueLedger``). Each record
  is checked by its own trust store, then kept there, and only then written to the bucket,
  so nothing the authority would refuse itself is ever published.
- **The bootstrap bundle** carries the authority keys, the installation's partition
  count, the authority's own certificate and the newest revocations record, so a host can
  check a join reply before it has a certificate. The launcher writes the count from the
  saved settings; the authority refuses to start when its own setting differs, before it
  changes anything (``PartitionCountMismatch``). The authority writes its records into the
  bundle just before it serves, so every reply it signs comes from a certificate the
  bundle already holds; a host that joins in between waits for the authority to answer
  (``joining.join``).
- **Topology.** Before anything else changes, the authority creates and keeps current
  every team's work and dead-letter streams and their team consumers (``queues``), with the
  instance's byte limits, per-subject cap and replica count, and refuses to start when they
  cannot be (another partition count, a lowered cap that would drop messages, no room);
  then the key-value buckets, with the same replica count. Agents' consumers follow
  placements; nothing publishes to the streams yet.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence

import rfc8785
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from nats.js.errors import KeyNotFoundError
from pydantic import ValidationError

from custom_messages.wire.encoding import b64url, format_timestamp
from custom_messages.wire.frame import Receiver, VerifiedFrame, body_model, expected_idempotency_key
from custom_messages.wire.kinds import (
    CLOCK_SKEW_SECONDS,
    CONTRACT_SHA256,
    INSTALLATION_SCOPE,
    KINDS,
    PROTOCOL,
    SINGLE_TEAM_ROLES,
)
from custom_messages.wire.partitions import OWNED, RELEASED, check_partition_count
from custom_messages.wire.permissions import HostPlacement, roles_permissions
from custom_messages.wire.queues import QueueLimits
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import (
    CERTIFICATE,
    PARTITION_OWNER,
    PLACEMENT,
    AuthorityKey,
    HostCertificate,
    TrustStore,
    kept_for,
    sign_state,
)
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.bootstrap import BootstrapBundle, read_bundle, write_bundle
from custom_transport.protocol1.bucket_reads import list_keys
from custom_transport.protocol1.contract_files import max_credential_bytes, role_permissions, transport
from custom_transport.protocol1.mapping_check import PartitionMappingError, check_partition_mapping
from custom_transport.protocol1.host_keys import AuthorityKeys, HostKeys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.joining import AUTHORITY_SERVICE_ID, HostProfile
from custom_transport.protocol1.live import LiveHost, ReplyWithAfterFlush, Unsupported, utc_now
from custom_transport.protocol1.nats_jwt import NatsSigningKey, encode_user_jwt, user_credential_bytes
from custom_transport.protocol1.nkeys import public_key_bytes
from custom_transport.protocol1.state_records import StateRecords, require_record_key
from custom_transport.protocol1.trust_state import PersistentTrustStore
from default_values import TRANSPORT_DEFAULTS
from installation_authority.expected_hosts import ExpectedHost, read_expected_hosts
from installation_authority.ledger import ISSUED_FILENAME, IssueLedger
from installation_authority.placements import PlacedConsumers, ReleasedPartitionWatcher
from installation_authority.queues import QueueTopology
from installation_authority.refusals import AuthorityStartRefused
from installation_authority.revocation import LifetimeWaitRevoker, NatsRevoker, RevocationOutcome
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
PLACEMENT_BUCKET = "UA1_PLACEMENT"
_PARTITION_OWNING_ROLES = frozenset({"lead_host", "worker_host"})
# revocations.schema.json entry reason enum (C13).
_REVOCATION_REASONS = frozenset({"compromised", "decommissioned", "quarantined"})
_RELEASED_WATCH_RETRY_SECONDS = 5.0
# common.schema.json "epoch".
MAX_EPOCH = 2**53 - 1
_INSTALLATION_ADDRESS = "_install.{sender}.svc"
_LOGGER = logging.getLogger(__name__)


def certificate_key(installation: str, runtime: str, epoch: int) -> str:
    return f"host.{installation}.{runtime}.{epoch}"


def revocations_key(installation: str) -> str:
    return f"revocations.{installation}"


class RevocationsUnverified(AuthorityStartRefused):
    """The stored revocations record does not verify and the authority has verified none, so it cannot sign the next one."""


class AuthorityClockBehind(AuthorityStartRefused):
    """The newest revocations record was signed later than this machine's clock says it is now."""


class PartitionCountMismatch(AuthorityStartRefused):
    """The bootstrap bundle's partition count, which every host uses, is not the authority's setting."""


class PartitionMappingRefused(AuthorityStartRefused):
    """The NATS server does not map agents' addresses at the installation's partition count."""


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

    def __init__(
        self,
        layout: InstanceLayout,
        authority: AuthorityKeys,
        host_keys: HostKeys,
        *,
        partitions: int,
        queues: QueueLimits,
        clock: Callable[[], datetime] = utc_now,
        logger: logging.Logger = _LOGGER,
        contract_sha256: str = CONTRACT_SHA256,
        catalog_agent_types: Callable[[], frozenset[str]] = frozenset,
        credential_lifetime_seconds: int = TRANSPORT_DEFAULTS.nats_credential_lifetime_seconds.value,
        nats_account_public_key: Optional[str] = None,
        nats_account_signing_key: Optional[NatsSigningKey] = None,
        revoker: Optional[NatsRevoker] = None,
    ) -> None:
        """``contract_sha256`` is the contract this authority runs: its code's, unless a test gives another.

        ``partitions`` is the installation's partition count (``transport.queues.partitions``),
        which the launcher also writes into the bootstrap bundle every host starts from.
        ``queues`` holds the streams' byte limits, the partition streams' per-subject cap and the
        operator's JetStream replica count (``transport.queues``, ``transport.replicas``), which
        the buckets use too. When ``nats_account_public_key`` and ``nats_account_signing_key``
        are provided, join and renewal replies carry a NATS user JWT ``connection`` credential
        ending after ``credential_lifetime_seconds`` (capped at the certificate's end); otherwise
        hosts keep the shared login while credential size limits are still enforced.
        """

        if host_keys.runtime != AUTHORITY_RUNTIME:
            raise ValueError(f"the authority speaks as host {AUTHORITY_RUNTIME!r}")
        if (nats_account_signing_key is None) != (nats_account_public_key is None):
            raise ValueError("nats_account_public_key and nats_account_signing_key must be set together")
        if nats_account_signing_key is not None and nats_account_signing_key.role != "account":
            raise ValueError("nats_account_signing_key must be a NATS account signing key")
        self.partitions = check_partition_count(partitions)
        self.contract_sha256 = contract_sha256
        self.layout = layout
        self.authority = authority
        self.host_keys = host_keys
        self.clock = clock
        self.logger = logger
        # The instance's agent types, read at each join of a host listed with agent_types_from_catalog.
        self.catalog_agent_types = catalog_agent_types
        self.queues = queues
        self.credential_lifetime_seconds = int(credential_lifetime_seconds)
        self._nats_account_public_key = nats_account_public_key
        self._nats_account_signing_key = nats_account_signing_key
        self._revoker: NatsRevoker = revoker if revoker is not None else LifetimeWaitRevoker()
        self.live: Optional[LiveHost] = None
        self._nc: Any = None
        self._js: Any = None
        self._teams: tuple[str, ...] = ()
        self._started = False
        # The teams' streams, buckets and consumers on the authority's connection (set up at start).
        self._topology: Optional[QueueTopology] = None
        # Each placed agent's consumers, while ``follow_placements`` runs.
        self.placements: Optional[PlacedConsumers] = None
        self._own: dict[str, Any] = {}
        self._revocations: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._forgotten_at: Optional[datetime] = None
        self._ledger = IssueLedger(layout.host_trust_dir(AUTHORITY_RUNTIME) / ISSUED_FILENAME, self.installation,
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
        of every reply; the launcher waits for it. Before anything changes, the bundle's
        partition count must be the authority's (``PartitionCountMismatch``) and the NATS
        server must map agents' addresses at that count (``PartitionMappingRefused``). The
        teams' streams come next, before the authority's own records change, so a start the
        streams refuse (``queues``) changes nothing else either.
        """

        bundle = self._bundle_with_own_count()
        try:
            await check_partition_mapping(nc, sender=AUTHORITY_RUNTIME, count=self.partitions)
        except PartitionMappingError as error:
            raise PartitionMappingRefused(str(error)) from error
        self._nc = nc
        self._js = nc.jetstream()
        teams = sorted(set(teams))
        self._teams = tuple(teams)
        self._started = True
        await self.ensure_topology(teams)
        self._ledger = IssueLedger.open(self.layout.host_trust_dir(AUTHORITY_RUNTIME), self.installation,
                                        logger=self.logger)
        now = self.clock()
        trust = self._trust = PersistentTrustStore.open(bundle, self.layout.host_trust_dir(AUTHORITY_RUNTIME), now)
        trust.store.contract_sha256 = self.contract_sha256
        revocations = await self._sign_revocations(now)
        self._own = await self._own_certificate(teams, int(revocations["body"]["revocation_epoch"]), now)
        certificate = self._held_certificate(self._own)
        self._publish_bundle()
        self.live = LiveHost(nc, HostIdentity(self.host_keys, certificate), trust, clock=self.clock, logger=self.logger)
        await self._take_issued_certificates(now)
        if self._unconfirmed_nats_revocations(now):
            try:
                await self._reconcile_nats_revocations(now)
            except Exception:
                self.logger.warning(
                    "Startup reconciliation of unconfirmed NATS key revocations failed; upkeep will retry. "
                    "event=nats_revocation_startup_reconcile_failed installation=%s",
                    self.installation,
                    exc_info=True,
                )
        await self.live.serve(_INSTALLATION_ADDRESS, Receiver(self.installation, INSTALLATION_SCOPE, "installation_service",
                                                              AUTHORITY_SERVICE_ID, AUTHORITY_RUNTIME), self._handle_live)
        await nc.flush()
        self.logger.info("The installation authority is serving. event=installation_authority_ready installation=%s "
                         "membership_epoch=%s teams=%s", self.installation, certificate.membership_epoch, ",".join(teams))

    def _bundle_with_own_count(self) -> BootstrapBundle:
        """The bootstrap bundle, when it holds this authority's partition count; refuse the start otherwise.

        The launcher writes the saved count into the bundle before it starts the authority, and
        every host computes agents' partitions with the bundle's count, so the two differ only
        when the settings changed while the instance started or the authority was started by
        hand. Streams and consumers made with one count and hosts working with another would
        put an agent's queues where its host does not read them.
        """

        bundle = read_bundle(self.layout.trust_bootstrap_path)
        if bundle.partitions != self.partitions:
            raise PartitionCountMismatch(
                f"The bootstrap bundle {self.layout.trust_bootstrap_path} holds partition count {bundle.partitions}, "
                f"but this authority's transport.queues.partitions is {self.partitions}. The launcher writes the setting "
                "into the bundle "
                "before it starts the authority; restart the instance so both use the saved setting.")
        return bundle

    async def ensure_topology(self, teams: Iterable[str]) -> None:
        """Create what the contract's topology owner owns: every team's streams, the buckets and the team consumers.

        One plan for all of them (``queues.QueueTopology``): its checks refuse a start before
        anything changes, and a stream whose settings already match is not sent to NATS.
        """

        self._topology = QueueTopology(self._nc, self._js, transport=transport(), limits=self.queues,
                                       partitions=self.partitions, logger=self.logger)
        await self._topology.ensure(teams)

    async def follow_placements(self, stop: asyncio.Event) -> None:
        """Keep each placed agent's inbox and control consumers in step with its team's placements,
        and watch ``host_lost`` released partition ownership records to revoke cut-off hosts' NATS keys (C01)."""

        assert self._topology is not None and self._trust is not None and self.live is not None, "start() first"
        trust, live = self._trust, self.live
        records = StateRecords(self._js, lambda: live.identity, trust)
        self.placements = PlacedConsumers(
            records, self._topology, teams=self._teams, logger=self.logger,
            verify=lambda raw: trust.store.verify(raw, record=PLACEMENT, now=self.clock()))
        release_watcher = ReleasedPartitionWatcher(
            records,
            verify=lambda raw: trust.store.verify(raw, record=PARTITION_OWNER, now=self.clock()),
            on_host_lost=lambda release: self.revoke_cut_off_host_nats_key(
                release.host,
                host_epoch=release.host_epoch,
                partition=release.partition,
                team=release.team,
                release_sequence=release.sequence,
                released_at=release.issued_at,
            ),
            teams=self._teams,
            logger=self.logger,
        )
        await asyncio.gather(
            self.placements.run(stop),
            release_watcher.run(stop),
        )

    async def revoke_cut_off_host_nats_key(
        self,
        runtime: str,
        *,
        host_epoch: int,
        partition: int,
        team: Optional[str] = None,
        release_sequence: Optional[int] = None,
        released_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> list[RevocationOutcome]:
        """Revoke the NATS user key(s) of a cut-off host that could have held ``partition`` at ``host_epoch`` (C11, C12, C17).

        Selects the certificate at ``host_epoch`` plus any later certificate that was actually issued
        with ``partition`` from an ownership record strictly older than ``release_sequence``
        (``owner_sequence < release_sequence``). A recovered host that rejoined (``{}``), renewed
        without ``partition``, or was reassigned ``partition`` at a higher ownership sequence after
        the release is never revoked by late or replayed ``host_lost`` records, even within
        ``CLOCK_SKEW_SECONDS``. Each targeted key's ``revoke_until`` covers the latest expiry across
        all unexpired certificates of ``runtime`` sharing that NATS key.
        """

        ts = self.clock() if now is None else now
        all_certs = await self._host_certificates_for_revocation(
            runtime, ts, min_epoch=1, include_epoch=host_epoch
        )
        skew = timedelta(seconds=CLOCK_SKEW_SECONDS)
        targeted: list[HostCertificate] = []
        for cert in all_certs:
            if team is not None and team not in cert.teams:
                continue
            if cert.membership_epoch == host_epoch:
                targeted.append(cert)
            elif cert.membership_epoch > host_epoch:
                granted = self._ledger.certificate_partitions(cert.certificate_id)
                if granted is not None:
                    owner_seq = granted.get(partition)
                    if owner_seq is None:
                        continue
                    if release_sequence is not None:
                        if owner_seq < release_sequence:
                            targeted.append(cert)
                    elif released_at is None or cert.issued_at <= released_at + skew:
                        targeted.append(cert)
                elif released_at is not None and cert.issued_at <= released_at + skew:
                    targeted.append(cert)

        revoke_until_by_key = self._max_revoke_until_by_nats_key(all_certs)
        target_keys = sorted({cert.nats_public_key for cert in targeted})
        outcomes: list[RevocationOutcome] = []
        for nats_key in target_keys:
            revoke_until = revoke_until_by_key[nats_key]
            if self._ledger.is_nats_revocation_confirmed(nats_key, min_revoke_until=revoke_until):
                continue
            outcomes.append(
                await self._revoke_nats_key(
                    nats_key,
                    revoke_until=revoke_until,
                    now=ts,
                )
            )
        return outcomes

    # -- NATS key lifecycle and host retirement (A5, A7, CR-1, S1, C01, C09, C11-C14) -----------

    @property
    def supports_instant_revocation(self) -> bool:
        return self._revoker.supports_instant_revocation

    def is_nats_key_revocation_confirmed(
        self,
        nats_public_key: str,
        *,
        min_revoke_until: Optional[int] = None,
    ) -> bool:
        """Whether instant revocation of ``nats_public_key`` was confirmed on the NATS server (C01/C08/C12)."""

        return self._ledger.is_nats_revocation_confirmed(nats_public_key, min_revoke_until=min_revoke_until)

    def register_bootstrap_nats_key(self, nats_public_key: str, *, now: Optional[datetime] = None) -> None:
        """Record a bootstrap bundle's NATS user key so no join or renewal can ever reuse it (S1)."""

        public_key_bytes(nats_public_key)
        self._ledger.record_bootstrap_nats_key(nats_public_key)
        self._ledger.save(self.clock() if now is None else now)

    @staticmethod
    def _max_revoke_until_by_nats_key(certs: Iterable[HostCertificate]) -> dict[str, int]:
        """Latest ``expires_at`` unix timestamp per ``nats_public_key`` across ``certs`` (C12)."""

        by_key: dict[str, int] = {}
        for cert in certs:
            exp_ts = int(cert.expires_at.timestamp())
            by_key[cert.nats_public_key] = max(by_key.get(cert.nats_public_key, 0), exp_ts)
        return by_key

    def _max_nats_key_revoke_until(
        self,
        runtime: str,
        nats_public_key: str,
        *,
        fallback_expires_at: datetime,
        now: datetime,
    ) -> int:
        """Latest ``expires_at`` unix timestamp across all unexpired certificates of ``runtime`` holding ``nats_public_key`` (C12)."""

        best = int(fallback_expires_at.timestamp())
        if self._trust is not None:
            for cert in self._trust.store.certificates_of_runtime(self.installation, runtime):
                if cert.nats_public_key == nats_public_key and cert.expires_at > now:
                    best = max(best, int(cert.expires_at.timestamp()))
        return best

    def _unconfirmed_nats_revocations(self, now: datetime) -> dict[str, int]:
        """Revoked NATS keys within their JWT validity window whose server confirmation is pending (C14)."""

        if not self.supports_instant_revocation:
            return {}
        cutoff = int(now.timestamp()) - CLOCK_SKEW_SECONDS
        return {
            key: revoke_until
            for key, revoke_until in self._ledger.revoked_nats_keys.items()
            if revoke_until >= cutoff
            and not self._ledger.is_nats_revocation_confirmed(key, min_revoke_until=revoke_until)
        }

    async def _reconcile_nats_revocations(self, now: datetime) -> None:
        """Retry pushing unconfirmed NATS key revocations to the server (C14)."""

        pending = self._unconfirmed_nats_revocations(now)
        if not pending:
            return
        for key in sorted(pending):
            revoke_until = pending[key]
            if self._ledger.is_nats_revocation_confirmed(key, min_revoke_until=revoke_until):
                continue
            outcome = await self._revoke_nats_key(key, revoke_until=revoke_until, now=now)
            if not outcome.revoked:
                break

    async def _revoke_nats_key(
        self,
        nats_public_key: str,
        *,
        revoke_until: int,
        now: datetime,
    ) -> RevocationOutcome:
        """Permanently ban ``nats_public_key`` in the ledger and push revocation to the server if supported."""

        public_key_bytes(nats_public_key)
        ts = int(revoke_until)
        self._ledger.record_revoked_nats_key(nats_public_key, ts)
        self._ledger.save(now)
        prune_before = int(now.timestamp()) - CLOCK_SKEW_SECONDS
        outcome = await self._revoker.revoke(
            nats_public_key,
            revoke_until=ts,
            all_revocations=self._ledger.revoked_nats_keys,
            prune_before=prune_before,
        )
        if outcome.revoked:
            for confirmed_key, confirmed_ts in outcome.confirmed_revocations.items():
                self._ledger.record_confirmed_nats_revocation(confirmed_key, confirmed_ts)
            self._ledger.record_confirmed_nats_revocation(nats_public_key, outcome.revoked_at)
            self._ledger.save(now)
        return outcome

    async def _host_certificates_for_revocation(
        self,
        runtime: str,
        now: datetime,
        *,
        min_epoch: int = 1,
        include_epoch: Optional[int] = None,
    ) -> list[HostCertificate]:
        """Unexpired certificates issued to ``runtime`` at ``min_epoch`` or later."""

        found: dict[int, HostCertificate] = {}
        if self._trust is not None:
            for cert in self._trust.store.certificates_of_runtime(self.installation, runtime):
                if cert.membership_epoch >= min_epoch and cert.expires_at > now:
                    found[cert.membership_epoch] = cert
        if self._js is not None and self._trust is not None:
            latest = await self._latest_certificate(runtime, now)
            if latest is not None:
                try:
                    self._trust.add(_encoded(latest), now=now)
                except FrameRefused:
                    pass
                latest_cert = self._trust.store.host_certificate(
                    self.installation, runtime, int(latest["body"]["membership_epoch"])
                )
                if latest_cert is not None and latest_cert.membership_epoch >= min_epoch and latest_cert.expires_at > now:
                    found[latest_cert.membership_epoch] = latest_cert
            if include_epoch is not None and include_epoch >= min_epoch and include_epoch not in found:
                stored_epoch = await self._stored(
                    CERTIFICATE, certificate_key(self.installation, runtime, include_epoch), now
                )
                if stored_epoch is not None:
                    try:
                        self._trust.add(_encoded(stored_epoch), now=now)
                    except FrameRefused:
                        pass
                    epoch_cert = self._trust.store.host_certificate(self.installation, runtime, include_epoch)
                    if epoch_cert is not None and epoch_cert.expires_at > now:
                        found[epoch_cert.membership_epoch] = epoch_cert
        return [found[epoch] for epoch in sorted(found)]

    async def retire_host(
        self,
        runtime: str,
        *,
        reason: str = "decommissioned",
        now: Optional[datetime] = None,
    ) -> list[RevocationOutcome]:
        """Revoke a host's active certificate(s) in ``UA1_CERTS`` and revoke its NATS user key(s) (decision 100, C12, C13)."""

        if reason not in _REVOCATION_REASONS:
            raise ValueError(f"revocation reason must be one of {sorted(_REVOCATION_REASONS)}, got {reason!r}")
        assert self._trust is not None, "start() first"
        ts = self.clock() if now is None else now
        certs = await self._host_certificates_for_revocation(runtime, ts, min_epoch=1)
        if certs:
            # revocations.schema.json: key_expires_at is the latest expires_at across every
            # certificate of this runtime with this key_id, or of the installation with this public key.
            max_key_expires: dict[str, datetime] = {}
            for cert in certs:
                cert_pub_raw = cert.public_key.public_bytes_raw()
                matching = [
                    *self._trust.store.certificates_of_runtime(self.installation, cert.runtime),
                    *self._trust.store.certificates_with_key(self.installation, cert.key_id),
                    *self._trust.store.certificates_with_public_key(self.installation, cert.public_key),
                ]
                latest_exp = cert.expires_at
                for other in matching:
                    if (
                        (other.runtime == cert.runtime and other.key_id == cert.key_id)
                        or other.public_key.public_bytes_raw() == cert_pub_raw
                    ):
                        if other.expires_at > latest_exp:
                            latest_exp = other.expires_at
                max_key_expires[cert.certificate_id] = latest_exp

            async with self._lock:
                held = self._newest_revocations(await self._stored_data(revocations_key(self.installation)), ts)
                entries = list(held["body"]["entries"]) if held else []
                existing_ids = {str(e.get("certificate_id")) for e in entries}
                added = False
                for cert in certs:
                    if cert.certificate_id in existing_ids:
                        continue
                    entries.append(
                        {
                            "certificate_id": cert.certificate_id,
                            "runtime": cert.runtime,
                            "key_id": cert.key_id,
                            "key_expires_at": format_timestamp(max_key_expires[cert.certificate_id]),
                            "membership_epoch": cert.membership_epoch,
                            "revoked_at": format_timestamp(ts),
                            "reason": reason,
                        }
                    )
                    existing_ids.add(cert.certificate_id)
                    added = True
                if added:
                    epoch = (int(held["body"]["revocation_epoch"]) if held else 1) + 1
                    sequence = max(int(held["sequence"]) + 1 if held else 1, int(ts.timestamp() * 1000))
                    record = self.revocations_record(
                        sequence=sequence,
                        revocation_epoch=epoch,
                        entries=entries,
                        issued=ts,
                    )
                    self._trust.add(_encoded(record), now=ts)
                    self._ledger.keep_revocations(record)
                    self._ledger.save(ts)
                    await (await self._records()).put(revocations_key(self.installation), _encoded(record))
                    self._revocations = record
                    if self._own:
                        self._publish_bundle()
        outcomes: list[RevocationOutcome] = []
        revoke_until_by_key = self._max_revoke_until_by_nats_key(certs)
        for nats_key, revoke_until in sorted(revoke_until_by_key.items()):
            outcomes.append(
                await self._revoke_nats_key(
                    nats_key,
                    revoke_until=revoke_until,
                    now=ts,
                )
            )
        return outcomes

    async def _records(self) -> Any:
        return await self._js.key_value(CERTIFICATES_BUCKET)

    async def _stored_data(self, key: str) -> Optional[bytes]:
        try:
            entry = await (await self._records()).get(key)
        except KeyNotFoundError:
            return None
        return entry.value or None

    async def _bucket_keys(self, prefix: str) -> list[str]:
        """Every key in UA1_CERTS that starts with ``prefix``, or an error (``bucket_reads.list_keys``).

        The authority creates the bucket before it reads it, so a missing bucket is an error too.
        """

        return await list_keys(self._js, CERTIFICATES_BUCKET, prefix)

    def _own_record(self, data: bytes, record: str, key: str, now: datetime) -> dict[str, Any]:
        """``data`` as a ``record`` under ``key`` that this authority signed; raises ``FrameRefused`` otherwise.

        It is checked as every receiver checks a trust record (``TrustStore.add``), by a
        store that trusts the authority's own key alone, so nothing another process wrote
        to the bucket is used or signed again.
        """

        own_key = AuthorityKey(self.installation, self.authority.key_id, self.authority.private_key.public_key())
        TrustStore(self.installation, [own_key], partition_count=self.partitions).add(data, now=now)
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
        """The newest revocations record this authority signed: the one it signed last, its kept copy, the ones its
        trust store keeps, the bundle's or the bucket's (``stored``).

        Returns ``None`` only when there is no record at all, on the first start. Raises
        ``AuthorityClockBehind`` when its newest records were signed later than this
        machine's clock says it is, and ``RevocationsUnverified`` when the bucket holds a
        record that does not verify and none verified: signing an empty list would accept
        every revoked host again.
        """

        key = revocations_key(self.installation)
        held: list[Mapping[str, Any]] = [record for record in (self._revocations, self._ledger.revocations) if record]
        if self._trust is not None:
            held += self._trust.kept_revocations(self.installation)
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
                "verified copy (its issue record, its trust state, the bootstrap bundle and the bucket), so it cannot "
                "tell which hosts "
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
        and the daily forgetting. A step that fails is logged and tried again at the next check; the forgetting,
        which notes the day it ran before it starts, the next day.

        Raises ``AuthorityDisconnected`` once the NATS client gave up reconnecting.
        """

        if self._nc is not None and self._nc.is_closed:
            raise AuthorityDisconnected("The installation authority's NATS connection closed and will not reconnect.")
        # When a failed step runs again: at the next check, or for the forgetting, which notes
        # the day it ran before it starts, once its interval has passed.
        next_check = f"in {int(UPKEEP_INTERVAL.total_seconds())} seconds"
        steps: tuple[tuple[bool, Callable[[datetime], Any], str, str], ...] = (
            (self._revocations_due(now), self._sign_revocations, "revocations_resign_failed", next_check),
            (self._own_certificate_due(now), self._renew_own_certificate, "authority_certificate_renewal_failed",
             next_check),
            (bool(self._unconfirmed_nats_revocations(now)), self._reconcile_nats_revocations,
             "nats_revocation_reconcile_failed", next_check),
            (self._forget_due(now), self.forget, "authority_forget_failed",
             f"in {int(FORGET_INTERVAL.total_seconds() // 3600)} hours"),
        )
        for due, step, event, again in steps:
            if not due:
                continue
            try:
                await step(now)
            except Exception:
                self.logger.exception("An authority upkeep step failed; it is tried again %s. event=%s installation=%s",
                                      again, event, self.installation)

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

        It goes to ``UA1_CERTS`` before it signs anything: a host reads a certificate a frame
        names and it does not hold yet from there, a joining host too (``joining.join``,
        ``fetch_trust``), since its own copy of the bootstrap bundle, on another machine made
        when it was set up, never learns of the new one. The bundle on this machine is written
        again for hosts that start later. The old one still signs for the overlap (5
        minutes), so replies on their way stay valid.
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
            if isinstance(body, Mapping) and body.get("certificate_id"):
                self._ledger.forget_certificate_partitions(str(body["certificate_id"]))
            deleted += 1
        if deleted:
            self._ledger.save(now)
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

    async def _issue(
        self,
        request: CertificateRequest,
        *,
        replaces: Optional[str],
        after_epoch: int,
        revocation_epoch: int,
        now: datetime,
        renewal: Optional[tuple[str, str]] = None,
        partitions: tuple[int, ...] = (),
        partition_sequences: Optional[Mapping[int, int]] = None,
        connection: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Sign a certificate at the epoch above ``after_epoch`` and every one issued, keep it, then publish it.

        Its own trust store takes it, the issue record keeps its epoch (and a renewal's
        answer under ``renewal``: retry key, body digest, partitions and connection credential),
        and only then the bucket gets it, so a lost reply or a lost bucket never makes an epoch
        name two certificates.
        """

        assert self._trust is not None, "start() opens the trust store first"
        epoch = max(after_epoch, self._ledger.highest_epoch(request.runtime)) + 1
        if epoch > MAX_EPOCH:
            raise ValueError(f"no membership epoch is left for {request.runtime}")
        record = self.certificate_record(request, epoch=epoch, revocation_epoch=revocation_epoch, issued=now,
                                         replaces=replaces)
        self._ledger.take_epoch(request.runtime, epoch)
        self._ledger.record_certificate_partitions(
            str(record["body"]["certificate_id"]),
            partition_sequences if partition_sequences is not None else {},
        )
        self._trust.add(_encoded(record), now=now)
        if renewal is not None:
            self._ledger.store_renewal(
                renewal[0],
                record,
                renewal[1],
                now,
                partitions=partitions,
                connection=connection,
            )
        self._ledger.save(now)
        await (await self._records()).put(certificate_key(self.installation, request.runtime, epoch), _encoded(record))
        self.logger.info("Issued a host certificate. event=host_certificate_issued runtime=%s membership_epoch=%s "
                         "key_id=%s replaces=%s", request.runtime, epoch, request.key_id, replaces)
        return record

    # -- partition ownership reads and credential sizing -----------------------------------------

    def _known_teams(self) -> Optional[frozenset[str]]:
        """Teams whose message streams exist on this authority (FA-6), or ``None`` before ``start``."""

        if self._started or self._teams:
            return frozenset(self._teams)
        return None

    async def _read_host_partition_ownership(
        self,
        runtime: str,
        teams: Iterable[str],
        now: datetime,
    ) -> tuple[dict[str, dict[int, int]], dict[str, set[int]]]:
        """Read verified ``partition_owner`` records for ``runtime`` across ``teams`` from ``UA1_PLACEMENT``.

        Returns ``(owned_by_team, released_by_team)`` where ``owned_by_team[team]`` maps each owned
        partition number to its verified ``partition_owner`` record sequence (C17). Listing failures
        propagate fail-closed; records that fail signature or key verification are logged and ignored.
        """

        if self._js is None or self._trust is None:
            return {}, {}
        owned: dict[str, dict[int, int]] = {}
        released: dict[str, set[int]] = {}
        bucket = await self._js.key_value(PLACEMENT_BUCKET)
        for team in sorted(set(teams)):
            keys = await list_keys(self._js, PLACEMENT_BUCKET, f"{team}.partition.")
            for key in keys:
                try:
                    entry = await bucket.get(key)
                except KeyNotFoundError:
                    continue
                if not entry.value:
                    continue
                try:
                    verified = self._trust.store.verify(entry.value, record=PARTITION_OWNER, now=now)
                    require_record_key(verified, key)
                    rec_body: Any = verified.body
                    if rec_body.team != team:
                        raise FrameRefused("invalid_body", "partition_owner team differs from key prefix",
                                           at="/body/team")
                except FrameRefused as refused:
                    self.logger.warning(
                        "A partition ownership record did not verify and is ignored during renewal. "
                        "event=authority_partition_owner_refused runtime=%s team=%s key=%s code=%s at=%s",
                        runtime,
                        team,
                        key,
                        refused.code,
                        refused.at,
                    )
                    continue
                if rec_body.host != runtime:
                    continue
                if rec_body.state == OWNED:
                    owned.setdefault(team, {})[int(rec_body.partition)] = int(verified.document.sequence)
                elif rec_body.state == RELEASED:
                    released.setdefault(team, set()).add(int(rec_body.partition))
        return owned, released

    def _validate_requested_partitions(
        self,
        certificate: HostCertificate,
        asked: Any,
        *,
        give_up: bool,
        owned_by_team: Mapping[str, Mapping[int, int]],
        released_by_team: Mapping[str, set[int]],
    ) -> tuple[int, ...]:
        """Validate ``host.renew`` ``partitions`` against verified ``owned`` records (A5)."""

        if asked is None:
            return ()
        if not isinstance(asked, list):
            raise FrameRefused("invalid_body", "partitions must be a list of partition numbers", at="/body/partitions")
        if not asked:
            return ()
        if len(set(asked)) != len(asked):
            raise FrameRefused("invalid_body", "partitions must not repeat a partition number", at="/body/partitions")
        for number in asked:
            if type(number) is not int or not 0 <= number < self.partitions:
                raise FrameRefused(
                    "invalid_body",
                    f"partition {number!r} is outside 0..{self.partitions - 1}",
                    at="/body/partitions",
                )
        if give_up:
            raise FrameRefused(
                "invalid_body",
                "a renewal giving up agents cannot ask for partitions",
                at="/body/partitions",
            )
        if not (certificate.roles & _PARTITION_OWNING_ROLES):
            raise FrameRefused(
                "invalid_body",
                "only lead_host or worker_host runtimes own partitions",
                at="/body/partitions",
            )
        if len(certificate.teams) != 1:
            raise FrameRefused(
                "invalid_body",
                "a partition-owning host must serve exactly one team",
                at="/body/partitions",
            )
        team = next(iter(certificate.teams))
        owned = owned_by_team.get(team, {})
        released = released_by_team.get(team, set())
        for number in asked:
            if number in released or number not in owned:
                raise FrameRefused(
                    "invalid_body",
                    f"host {certificate.runtime} does not hold an owned partition record for partition {number}",
                    at="/body/partitions",
                )
        return tuple(sorted(asked))

    def _prepare_connection_credential(
        self,
        *,
        runtime: str,
        teams: Sequence[str],
        roles: Sequence[str],
        agent_types: Sequence[str],
        nats_public_key: str,
        partitions: Sequence[int],
        expected: Optional[ExpectedHost],
        now: datetime,
        cert_expires_at: datetime,
    ) -> tuple[Optional[dict[str, Any]], int, int]:
        """Compute exact NATS user JWT byte size and optionally mint the signed ``connection`` credential.

        Always uses ``roles_permissions`` (S3 per-role partition filling) and ``max_credential_bytes``
        so team services and the authority agree on credential fit even under shared NATS login.
        """

        sorted_teams = tuple(sorted(set(teams)))
        sorted_roles = tuple(sorted(set(roles)))
        sorted_pools = tuple(sorted(set(agent_types)))
        partitions_by_team: dict[str, tuple[int, ...]] = {}
        if partitions:
            sorted_parts = tuple(sorted(set(partitions)))
            for team in sorted_teams:
                partitions_by_team[team] = sorted_parts
        pools_by_team = {team: sorted_pools for team in sorted_teams} if sorted_pools else {}
        edges_by_team = (
            {team: expected.edges[team] for team in sorted_teams if team in expected.edges}
            if expected is not None and expected.edges
            else {}
        )
        placement = HostPlacement(
            runtime=runtime,
            teams=sorted_teams,
            partitions=partitions_by_team,
            pools=pools_by_team,
            edges=edges_by_team,
            installation=self.installation,
        )
        limit = max_credential_bytes()
        if self._nats_account_signing_key is not None and self._nats_account_public_key is not None:
            filled = roles_permissions(
                [role_permissions(role) for role in sorted_roles],
                placement,
                partition_count=self.partitions,
            )
            raw_expiry = min(now + timedelta(seconds=self.credential_lifetime_seconds), cert_expires_at)
            exp_ts = int(raw_expiry.timestamp())
            iat_ts = int(now.timestamp())
            token = encode_user_jwt(
                self._nats_account_signing_key,
                self._nats_account_public_key,
                nats_public_key,
                runtime,
                filled,
                issued_at=iat_ts,
                expires_at=exp_ts,
            )
            size_bytes = len(token.encode("ascii"))
            connection: Optional[dict[str, Any]] = {
                "type": "nats_user_jwt",
                "jwt": token,
                "expires_at": format_timestamp(datetime.fromtimestamp(exp_ts, tz=timezone.utc)),
            }
            return connection, size_bytes, limit

        valid_user_key: Optional[str] = None
        try:
            public_key_bytes(nats_public_key)
            valid_user_key = nats_public_key
        except ValueError:
            valid_user_key = None
        size_bytes = user_credential_bytes(
            sorted_roles,
            placement,
            partition_count=self.partitions,
            user_public_key=valid_user_key,
            account_public_key=self._nats_account_public_key,
        )
        return None, size_bytes, limit

    def _schedule_post_flush(
        self,
        deferred: Optional[list[Callable[[], Awaitable[None]]]],
        action: Callable[[], Awaitable[None]],
    ) -> Optional[Awaitable[None]]:
        if deferred is not None:
            deferred.append(action)
            return None
        return action()

    # -- answering ------------------------------------------------------------------------------

    async def _handle_live(self, verified: VerifiedFrame) -> dict[str, Any] | ReplyWithAfterFlush:
        """LiveHost request handler: flushes the reply before running any NATS key revocations (C01)."""

        deferred: list[Callable[[], Awaitable[None]]] = []
        body = await self._dispatch(verified, deferred=deferred)
        if not deferred:
            return body

        async def _after_flush() -> None:
            for action in deferred:
                await action()

        return ReplyWithAfterFlush(body=body, after_flush=_after_flush)

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        return await self._dispatch(verified, deferred=None)

    async def _dispatch(
        self,
        verified: VerifiedFrame,
        *,
        deferred: Optional[list[Callable[[], Awaitable[None]]]],
    ) -> dict[str, Any]:
        if verified.other_contract:
            return await self.answer_other_contract(verified, deferred=deferred)
        if verified.frame.kind == "host.join":
            return await self.answer_join(verified, deferred=deferred)
        if verified.frame.kind == "host.renew":
            return await self.answer_renew(verified, deferred=deferred)
        raise Unsupported(verified.frame.kind)

    def _protocol_mismatch(self) -> dict[str, Any]:
        """The answer to a host on another pre-release contract: the digest this authority runs, and what to install."""

        return {"outcome": "protocol_mismatch", "accepted": PROTOCOL, "accepted_contract_sha256": self.contract_sha256,
                "install": f"universal-agents {release_version()}",
                "message": "This host runs another pre-release contract than the installation; install the same release."}

    async def answer_other_contract(
        self,
        verified: VerifiedFrame,
        *,
        deferred: Optional[list[Callable[[], Awaitable[None]]]] = None,
    ) -> dict[str, Any]:
        """A join or renewal from a host on another contract: ``protocol_mismatch``, unless it only gives up agents."""

        runtime = verified.frame.issuer.runtime
        if verified.frame.kind == "host.renew" and self._readable_give_up(verified):
            self.logger.warning("A host on another contract gave up its agents; its renewal is answered. "
                                "event=host_renewal_gave_up_agents runtime=%s their_contract_sha256=%s "
                                "contract_sha256=%s", runtime, verified.their_contract_sha256, self.contract_sha256)
            return await self._renew(verified, give_up=True, deferred=deferred)
        self.logger.warning("A host on another contract was turned away. event=host_protocol_mismatch runtime=%s kind=%s "
                            "their_contract_sha256=%s contract_sha256=%s", runtime, verified.frame.kind,
                            verified.their_contract_sha256, self.contract_sha256)
        return self._protocol_mismatch()

    def _readable_give_up(self, verified: VerifiedFrame) -> bool:
        """Whether a renewal of another contract is one this revision reads and that only gives up agents.

        Its body was not read when it was verified, so it is read here: it fits this
        contract's renewal (schema and retry key), names the certificate that signs it, asks
        for no partitions, does not rotate the application key, and asks for the profile that
        gives up every agent. ``new_nats_public_key`` is allowed (and checked/required by
        ``_renew`` when giving up partitions/pools or after NATS key revocation, C05).
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
                and not body.get("partitions")
                and _gives_up(certificate, body))

    async def answer_join(
        self,
        verified: VerifiedFrame,
        *,
        deferred: Optional[list[Callable[[], Awaitable[None]]]] = None,
    ) -> dict[str, Any]:
        body = verified.raw["body"]
        runtime, key = str(body["runtime"]), body["application_key"]
        expected = read_expected_hosts(self.layout.trust_expected_hosts_path).get(runtime)
        # verify_frame already refused a join presenting a revoked key (README "Joining").
        now = self.clock()
        bootstrap_key = expected.bootstrap_nats_public_key if expected is not None else None
        if bootstrap_key:
            self._ledger.record_bootstrap_nats_key(bootstrap_key)
        join_nats_key = str(body["nats_public_key"])
        if self._ledger.is_nats_key_revoked(join_nats_key) or self._ledger.is_bootstrap_nats_key(join_nats_key):
            raise FrameRefused(
                "invalid_body",
                "this NATS user key is revoked or belongs to a bootstrap bundle and cannot be used",
                at="/body/nats_public_key",
            )
        stored = await self._latest_certificate(runtime, now)
        allowed = self._allowed_agent_types(expected) | _held_agent_types(stored, key, now)
        denial = _denial(expected, body, allowed, known_teams=self._known_teams())
        if denial is not None:
            self.logger.warning("A join was refused. event=host_join_denied runtime=%s reason=%s key_id=%s "
                                "connection_class=%s deployment=%s", runtime, denial, key["key_id"],
                                body["connection_class"], body["deployment"])
            return {"outcome": "denied", "reason": denial,
                    "message": "This installation takes only the hosts its launcher starts, as the launcher lists them."}

        reusable = (
            self._reusable(stored, key["key_id"], key["public_key"], join_nats_key, now)
            and stored is not None
            and _grants_exactly(stored["body"], body)
        )
        cert_expires_at = (
            parse_timestamp(str(stored["body"]["expires_at"]))
            if reusable and stored is not None
            else now + CERTIFICATE_LIFETIME
        )
        connection, size_bytes, limit = self._prepare_connection_credential(
            runtime=runtime,
            teams=tuple(body["teams"]),
            roles=tuple(body["roles"]),
            agent_types=tuple(body["agent_types"]),
            nats_public_key=join_nats_key,
            partitions=(),
            expected=expected,
            now=now,
            cert_expires_at=cert_expires_at,
        )
        if size_bytes > limit:
            self.logger.warning(
                "A join was refused because its NATS credential exceeds max_credential_bytes. "
                "event=host_join_credential_too_large runtime=%s size_bytes=%s max_bytes=%s",
                runtime,
                size_bytes,
                limit,
            )
            return {
                "outcome": "denied",
                "reason": "credential_too_large",
                "message": (
                    f"This host's NATS credential ({size_bytes} bytes) exceeds the {limit}-byte limit "
                    "(max_credential_bytes); assign fewer pools, teams, or routes."
                ),
            }

        bootstrap_revoke_until: Optional[int] = None
        if bootstrap_key:
            if expected is not None and expected.bootstrap_nats_expires_at is not None:
                bootstrap_revoke_until = int(expected.bootstrap_nats_expires_at.timestamp())
            else:
                # Conservative upper bound when no explicit bootstrap JWT expiry was recorded (C12).
                bootstrap_revoke_until = max(
                    int((now + CERTIFICATE_LIFETIME).timestamp()),
                    int(now.timestamp()) + self.credential_lifetime_seconds + CLOCK_SKEW_SECONDS,
                )

        async def _revoke_bootstrap_after_join() -> None:
            if bootstrap_key and bootstrap_revoke_until is not None:
                await self._revoke_nats_key(
                    bootstrap_key,
                    revoke_until=bootstrap_revoke_until,
                    now=now,
                )

        if reusable and stored is not None:
            reply: dict[str, Any] = {"outcome": "ok", "certificate": stored}
            if connection is not None:
                reply["connection"] = connection
            if (
                bootstrap_key
                and bootstrap_revoke_until is not None
                and not self._ledger.is_nats_revocation_confirmed(
                    bootstrap_key, min_revoke_until=bootstrap_revoke_until
                )
            ):
                self._ledger.record_revoked_nats_key(bootstrap_key, bootstrap_revoke_until)
                self._ledger.save(now)
                pending = self._schedule_post_flush(deferred, _revoke_bootstrap_after_join)
                if pending is not None:
                    await pending
            return reply

        request = CertificateRequest(runtime=runtime, teams=tuple(body["teams"]), roles=tuple(body["roles"]),
                                     agent_types=tuple(body["agent_types"]), session_limit=int(body["session_limit"]),
                                     connection_class=str(body["connection_class"]), key_id=key["key_id"],
                                     public_key=key["public_key"], nats_public_key=join_nats_key,
                                     deployment=str(body["deployment"]))
        if request.profile_sha256() != body["profile_sha256"]:
            self.logger.info("A join's profile digest differs from the profile granted; the certificate states the "
                             "granted one. event=host_join_profile_differs runtime=%s", runtime)
        replaces = stored["body"]["certificate_id"] if stored and parse_timestamp(stored["body"]["expires_at"]) > now \
            else None
        record = await self._issue(request, replaces=replaces, after_epoch=self._newest_epoch(runtime, stored),
                                   revocation_epoch=int(self._revocations["body"]["revocation_epoch"]), now=now)
        reply = {"outcome": "ok", "certificate": record}
        if connection is not None:
            reply["connection"] = connection
        if (
            bootstrap_key
            and bootstrap_revoke_until is not None
            and not self._ledger.is_nats_revocation_confirmed(
                bootstrap_key, min_revoke_until=bootstrap_revoke_until
            )
        ):
            self._ledger.record_revoked_nats_key(bootstrap_key, bootstrap_revoke_until)
            self._ledger.save(now)
            pending = self._schedule_post_flush(deferred, _revoke_bootstrap_after_join)
            if pending is not None:
                await pending
        return reply

    def _allowed_agent_types(self, expected: Optional[ExpectedHost]) -> frozenset[str]:
        """The agent types a listed host may ask for: its own, and the catalog's when it is listed that way."""

        if expected is None:
            return frozenset()
        if not expected.agent_types_from_catalog:
            return expected.agent_types
        try:
            return expected.agent_types | self.catalog_agent_types()
        except Exception:
            self.logger.exception("The agent catalog could not be read; only the listed agent types are allowed. "
                                  "event=authority_catalog_unreadable runtime=%s", expected.runtime)
            return expected.agent_types

    async def answer_renew(
        self,
        verified: VerifiedFrame,
        *,
        deferred: Optional[list[Callable[[], Awaitable[None]]]] = None,
    ) -> dict[str, Any]:
        certificate = verified.certificate
        assert certificate is not None  # a renewal is signed by the certificate it renews
        return await self._renew(verified, give_up=_gives_up(certificate, verified.raw["body"]), deferred=deferred)

    async def _answer_stored_renewal(
        self,
        stored: Any,
        retry_key: str,
        certificate: HostCertificate,
        expected: Optional[ExpectedHost],
        now: datetime,
        deferred: Optional[list[Callable[[], Awaitable[None]]]],
    ) -> dict[str, Any]:
        """Answer a retried renewal from ``StoredRenewal``, refreshing an expired/near-expiry JWT (C03)."""

        await self._store_certificate(stored.certificate)
        cert_body = stored.certificate["body"]
        stored_nats_key = str(cert_body["nats_public_key"])
        reply: dict[str, Any] = {"outcome": "ok", "certificate": dict(stored.certificate)}

        if self._nats_account_signing_key is not None and self._nats_account_public_key is not None:
            min_remaining = timedelta(seconds=min(60, max(1, self.credential_lifetime_seconds // 3)))
            stored_conn = stored.connection
            needs_refresh = True
            if stored_conn is not None:
                try:
                    conn_exp = parse_timestamp(str(stored_conn["expires_at"]))
                    needs_refresh = (conn_exp - now) <= min_remaining
                except (KeyError, ValueError):
                    needs_refresh = True
            if not needs_refresh and stored_conn is not None:
                reply["connection"] = dict(stored_conn)
            else:
                cert_expires = parse_timestamp(str(cert_body["expires_at"]))
                held_cert = (
                    self._trust.store.host_certificate(
                        self.installation, str(cert_body["runtime"]), int(cert_body["membership_epoch"])
                    )
                    if self._trust is not None
                    else None
                )
                if (
                    self._ledger.is_nats_key_revoked(stored_nats_key)
                    or (held_cert is not None and self._trust is not None and self._trust.store.revoked(held_cert))
                ):
                    return {
                        "outcome": "revoked",
                        "revocation_epoch": int(self._revocations["body"]["revocation_epoch"]),
                        "message": "The renewed certificate or its NATS user key has since been revoked.",
                    }
                if cert_expires <= now:
                    return {
                        "outcome": "not_allowed",
                        "message": "The stored renewal certificate has expired; join again.",
                    }
                fresh_conn, size_bytes, limit = self._prepare_connection_credential(
                    runtime=str(cert_body["runtime"]),
                    teams=tuple(cert_body["teams"]),
                    roles=tuple(cert_body["roles"]),
                    agent_types=tuple(cert_body["agent_types"]),
                    nats_public_key=stored_nats_key,
                    partitions=stored.partitions,
                    expected=expected,
                    now=now,
                    cert_expires_at=cert_expires,
                )
                if size_bytes > limit:
                    return {
                        "outcome": "not_allowed",
                        "message": (
                            f"This host's NATS credential ({size_bytes} bytes) exceeds the {limit}-byte limit "
                            "(max_credential_bytes)."
                        ),
                    }
                if fresh_conn is not None:
                    self._ledger.store_renewal(
                        retry_key,
                        stored.certificate,
                        stored.body_sha256,
                        stored.stored_at,
                        partitions=stored.partitions,
                        connection=fresh_conn,
                    )
                    self._ledger.save(now)
                    reply["connection"] = fresh_conn

        # If the old NATS key rotation wasn't confirmed revoked yet (e.g. transient NATS error), retry after flush.
        if stored_nats_key != certificate.nats_public_key:
            old_key = certificate.nats_public_key
            old_exp = self._max_nats_key_revoke_until(
                certificate.runtime,
                old_key,
                fallback_expires_at=certificate.expires_at,
                now=now,
            )
            if not self._ledger.is_nats_revocation_confirmed(old_key, min_revoke_until=old_exp):
                async def _retry_revoke() -> None:
                    await self._revoke_nats_key(old_key, revoke_until=old_exp, now=now)

                pending = self._schedule_post_flush(deferred, _retry_revoke)
                if pending is not None:
                    await pending
        return reply

    async def _renew(
        self,
        verified: VerifiedFrame,
        *,
        give_up: bool,
        deferred: Optional[list[Callable[[], Awaitable[None]]]] = None,
    ) -> dict[str, Any]:
        """The renewed certificate: the grants of the one that signed the renewal, or none of its agents when it gives
        them up; the stored answer when the renewal was already carried out."""

        body = verified.raw["body"]
        certificate = verified.certificate
        assert certificate is not None and self.live is not None  # a renewal is signed by the certificate it renews
        now = self.clock()
        expected = read_expected_hosts(self.layout.trust_expected_hosts_path).get(certificate.runtime)
        if expected is not None and expected.bootstrap_nats_public_key:
            self._ledger.record_bootstrap_nats_key(expected.bootstrap_nats_public_key)

        retry_key, body_sha256 = verified.frame.idempotency_key, _body_sha256(body)
        stored = self._ledger.renewal(retry_key, now)
        if stored is not None:
            if stored.body_sha256 != body_sha256:
                # README "Stored outcomes": a sender defect; the stored answer stands.
                self.logger.warning("A renewal repeated its retry key with another body; the stored answer is given. "
                                    "event=host_renewal_retry_other_body runtime=%s stored_body_sha256=%s "
                                    "body_sha256=%s", certificate.runtime, stored.body_sha256, body_sha256)
            return await self._answer_stored_renewal(stored, retry_key, certificate, expected, now, deferred)

        rotation = body.get("application_key_rotation")
        if rotation:
            issued = await self._rotation_already_issued(certificate, rotation["key"], now)
            if issued is not None:
                return {"outcome": "ok", "certificate": issued}

        key = rotation["key"] if rotation else {"key_id": certificate.key_id, "public_key": _public_text(certificate)}
        known_teams = self._known_teams()
        if give_up:
            # A renewal that gives its agents up keeps its teams, so it too is refused for a
            # certificate that breaks the one-team rule or names a team without streams (FA-6).
            if _serves_too_many_teams(certificate.roles, certificate.teams):
                refusal: Optional[str] = _MULTI_TEAM_RENEWAL
            elif known_teams is not None and not certificate.teams <= known_teams:
                refusal = "This certificate names a team without message streams on this installation (team_not_allowed)."
            else:
                refusal = None
        else:
            refusal = _renewal_refusal(
                expected,
                certificate,
                key,
                self._allowed_agent_types(expected) | (
                    certificate.agent_types if expected is not None and expected.agent_types_from_catalog
                    else frozenset()
                ),
                known_teams=known_teams,
            )
        if refusal is not None:
            self.logger.warning("A renewal was refused: the launcher's list or the one-team rule does not cover it. "
                                "event=host_renewal_not_allowed runtime=%s membership_epoch=%s teams=%s reason=%s",
                                certificate.runtime, certificate.membership_epoch, sorted(certificate.teams), refusal)
            return {"outcome": "not_allowed", "message": refusal}

        # Only partition-owning roles can hold or release partition ownership records (C15).
        if certificate.roles & _PARTITION_OWNING_ROLES:
            owned_by_team, released_by_team = await self._read_host_partition_ownership(
                certificate.runtime, certificate.teams, now
            )
        else:
            owned_by_team, released_by_team = {}, {}
        granted_partitions = self._validate_requested_partitions(
            certificate,
            body.get("partitions"),
            give_up=give_up,
            owned_by_team=owned_by_team,
            released_by_team=released_by_team,
        )

        supplied_new_nats = body.get("new_nats_public_key")
        if supplied_new_nats is not None:
            new_key_str = str(supplied_new_nats)
            if self._ledger.is_nats_key_revoked(new_key_str) or self._ledger.is_bootstrap_nats_key(new_key_str):
                raise FrameRefused(
                    "invalid_body",
                    "this NATS user key is revoked or belongs to a bootstrap bundle and cannot be used",
                    at="/body/new_nats_public_key",
                )
        dropping_partition = any(released_by_team.values())
        dropping_pool = give_up and bool(certificate.agent_types)
        current_key_unusable = (
            self._ledger.is_nats_key_revoked(certificate.nats_public_key)
            or self._ledger.is_bootstrap_nats_key(certificate.nats_public_key)
        )
        if dropping_partition or dropping_pool or current_key_unusable:
            if not supplied_new_nats or str(supplied_new_nats) == certificate.nats_public_key:
                raise FrameRefused(
                    "invalid_body",
                    "a renewal giving up a partition or pool, or renewing after NATS key revocation, requires a new NATS user key",
                    at="/body/new_nats_public_key",
                )
        target_nats_key = str(supplied_new_nats) if supplied_new_nats else certificate.nats_public_key

        agent_types = () if give_up else tuple(sorted(certificate.agent_types))
        connection, size_bytes, limit = self._prepare_connection_credential(
            runtime=certificate.runtime,
            teams=tuple(sorted(certificate.teams)),
            roles=tuple(sorted(certificate.roles)),
            agent_types=agent_types,
            nats_public_key=target_nats_key,
            partitions=granted_partitions,
            expected=expected,
            now=now,
            cert_expires_at=now + CERTIFICATE_LIFETIME,
        )
        if size_bytes > limit:
            self.logger.warning(
                "A renewal was refused because its NATS credential exceeds max_credential_bytes. "
                "event=host_renewal_credential_too_large runtime=%s size_bytes=%s max_bytes=%s partitions=%s",
                certificate.runtime,
                size_bytes,
                limit,
                list(granted_partitions),
            )
            return {
                "outcome": "not_allowed",
                "message": (
                    f"This host's NATS credential ({size_bytes} bytes) exceeds the {limit}-byte limit "
                    "(max_credential_bytes); renew with fewer partitions, pools, teams, or routes."
                ),
            }

        request = CertificateRequest(runtime=certificate.runtime, teams=tuple(sorted(certificate.teams)),
                                     roles=tuple(sorted(certificate.roles)), agent_types=agent_types,
                                     session_limit=0 if give_up else certificate.session_limit,
                                     connection_class=certificate.connection_class,
                                     key_id=key["key_id"], public_key=key["public_key"],
                                     nats_public_key=target_nats_key,
                                     deployment=expected.deployment if expected is not None else "local")
        if request.profile_sha256() != body["profile_sha256"]:
            self.logger.info("A renewal asked for another profile than the one it keeps; the certificate states the "
                             "granted one. event=host_renewal_profile_differs runtime=%s", certificate.runtime)
        latest = await self._latest_certificate(certificate.runtime, now)
        granted_partition_sequences: dict[int, int] = {}
        if granted_partitions:
            team = next(iter(certificate.teams))
            team_owned = owned_by_team.get(team, {})
            granted_partition_sequences = {p: team_owned[p] for p in granted_partitions}
        record = await self._issue(
            request,
            replaces=certificate.certificate_id,
            after_epoch=max(certificate.membership_epoch, self._newest_epoch(certificate.runtime, latest)),
            revocation_epoch=int(self._revocations["body"]["revocation_epoch"]),
            now=now,
            renewal=(retry_key, body_sha256),
            partitions=granted_partitions,
            partition_sequences=granted_partition_sequences,
            connection=connection,
        )

        reply: dict[str, Any] = {"outcome": "ok", "certificate": record}
        if connection is not None:
            reply["connection"] = connection

        if target_nats_key != certificate.nats_public_key:
            old_key = certificate.nats_public_key
            old_exp = self._max_nats_key_revoke_until(
                certificate.runtime,
                old_key,
                fallback_expires_at=certificate.expires_at,
                now=now,
            )
            # Persist the permanent ban before replying so no concurrent or post-crash call reissues old_key.
            self._ledger.record_revoked_nats_key(old_key, old_exp)
            self._ledger.save(now)

            async def _revoke_old_key_after_flush() -> None:
                await self._revoke_nats_key(old_key, revoke_until=old_exp, now=now)

            pending = self._schedule_post_flush(deferred, _revoke_old_key_after_flush)
            if pending is not None:
                await pending
        return reply

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


def _held_agent_types(stored: Optional[Mapping[str, Any]], key: Mapping[str, Any], now: datetime) -> frozenset[str]:
    """The agent types the host's current certificate grants, when the join presents that certificate's key.

    A join from the host that holds them keeps them, as a renewal does (module docstring): a
    type removed from the catalog leaves its running agents running, and the host asks for its
    certificate's types again with every new one, so a removed type would otherwise make every
    later join of that host refused.
    """

    if stored is None:
        return frozenset()
    body = stored["body"]
    if (body["application_key"]["key_id"], body["application_key"]["public_key"]) != (key["key_id"], key["public_key"]):
        return frozenset()
    if parse_timestamp(body["expires_at"]) <= now:
        return frozenset()
    return frozenset(body["agent_types"])


_MULTI_TEAM_RENEWAL = ("This certificate names more than one team for a role that serves one team; the host must "
                       "join again, once for each team.")


def _serves_too_many_teams(roles: Iterable[str], teams: Iterable[str]) -> bool:
    """Whether a certificate with these roles and teams breaks the one-team rule (transport.yaml single_team_roles).

    Every role but the gateway and the authority serves one team per runtime (owner decision of
    2026-11-17). The authority grants such a role only with one team at join, and refuses to renew
    a certificate that names more, such as one issued before the rule or from a hand-edited
    expected-hosts entry; hosts refuse a reply that carries one.
    """

    return bool(set(roles) & set(SINGLE_TEAM_ROLES)) and len(set(teams)) > 1


def _denial(
    expected: Any,
    body: Mapping[str, Any],
    agent_types: frozenset[str],
    *,
    known_teams: Optional[frozenset[str]] = None,
) -> Optional[str]:
    """Why a join is refused, as the reply's reason, or ``None`` when the launcher listed exactly this host.

    ``agent_types`` are the types the host may ask for: those it is listed for or the catalog has
    (``_allowed_agent_types``), and those its current certificate grants (``_held_agent_types``).
    ``known_teams`` are the teams whose message streams exist on this authority (FA-6).
    """

    if expected is None:
        return "not_approved"
    key = body["application_key"]
    if (expected.key_id, expected.public_key) != (key["key_id"], key["public_key"]):
        return "key_mismatch"
    asked_teams = set(body["teams"])
    if not asked_teams <= expected.teams:
        return "team_not_allowed"
    if known_teams is not None and not asked_teams <= known_teams:
        return "team_not_allowed"
    if _serves_too_many_teams(body["roles"], body["teams"]):
        return "team_not_allowed"
    edges = getattr(expected, "edges", None) or {}
    if edges and not set(edges) <= expected.teams:
        return "team_not_allowed"
    if not set(body["roles"]) <= expected.roles or not set(body["agent_types"]) <= agent_types:
        return "role_not_allowed"
    if int(body["session_limit"]) > expected.session_limit:
        return "over_limit"
    # README "Connection class and tools": an administrator approves the connection class;
    # here the launcher's list does, and the deployment it starts the host in.
    if (body["connection_class"], body["deployment"]) != (expected.connection_class, expected.deployment):
        return "not_approved"
    return None


def _renewal_refusal(
    expected: Optional[ExpectedHost],
    certificate: HostCertificate,
    key: Mapping[str, Any],
    agent_types: frozenset[str],
    *,
    known_teams: Optional[frozenset[str]] = None,
) -> Optional[str]:
    """Why the launcher's list no longer covers a renewal, as the reply's message; ``None`` when it does.

    A renewal keeps the grants of the certificate it replaces (README "Renewing"), so they
    are checked against the list as a join's are: a host the list no longer covers must
    join again, for what the list now allows. So must a host whose certificate names more
    than one team for a role that serves one team (``_serves_too_many_teams``) or names a
    team without message streams (FA-6).
    """

    if _serves_too_many_teams(certificate.roles, certificate.teams):
        return _MULTI_TEAM_RENEWAL
    if known_teams is not None and not certificate.teams <= known_teams:
        return "This certificate names a team without message streams on this installation (team_not_allowed)."
    if expected is None:
        return "The launcher no longer lists this host; it must join again."
    if expected.edges and not set(expected.edges) <= expected.teams:
        return "The launcher lists gateway routes for a team this host is not allowed to serve."
    if (expected.key_id, expected.public_key) != (key.get("key_id"), key.get("public_key")):
        return "The launcher lists another key for this host; it must join again with that key."
    if not (certificate.teams <= expected.teams and certificate.roles <= expected.roles
            and certificate.agent_types <= agent_types and certificate.session_limit <= expected.session_limit
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

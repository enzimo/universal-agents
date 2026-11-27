"""One process's life as a protocol 1 host: join, keep its trust current, renew, and report liveness.

Every host does the same around its own work (README "Installation traffic", "Trust
over time", "State records"):

- **join** with the key the launcher made for it, and keep the certificate it gets;
- **keep a current revocations record**: read the authority's newest one from
  ``UA1_CERTS`` at start and every half hour (the authority signs one every six hours
  and each lasts a day), so the host never refuses other hosts' frames for lack of one;
- **forget** the trust records nothing can still need, at least daily
  (``TrustStore.forget``, README "Trust over time"), so its trust store stays bounded;
- **renew** its certificate when two thirds of its life are gone;
- **take the newest certificate** when another process runs as the same host
  (``take_newest_certificate``): every team services instance is host ``team-services``,
  and a standby that waited for the lock may hold a certificate the serving instance has
  since renewed;
- **join again** for agent types its certificate does not name yet (``rejoin``), such as a
  specialist type the team created while the host runs, so every running agent can hold
  a session;
- **report liveness**: write its own host record every 10 seconds, each lasting 30, so a
  host that stops shows as gone within 30 seconds of its last write, as the presence
  heartbeat did, and one or two lost writes do not make a running host look gone.
  Nothing moves because of that. Liveness has a loop of its own: the trust and
  certificate upkeep, whose requests to the authority may wait 30 seconds or fail for
  days, runs beside it and never delays or skips a write. The record lists the parts of
  the host that are not healthy (``components``, each with a code saying why), such as an
  agent whose session the team services refused; the gateway shows them as Status page
  findings (plan 13B.5). Each is checked against the contract first, and one it cannot
  carry is left out and logged, so a bad component never stops the record. The host is
  then ``degraded``: it still serves everything else. When they change, the host writes
  its record again at once (``report_now``) rather than at the next interval;
- **look up unknown records**: a frame or record that names a certificate or session the
  host does not hold yet is checked again after reading that record from ``UA1_CERTS``
  (``fetch_trust``), joins included: a join reply signed by the authority's renewed
  certificate is checked that way. A lookup that fails is logged and counts as nothing
  new, so the frame is refused as it was;
- **notice another contract** (README "Versions"). While the label is a pre-release, hosts
  of one installation refuse a peer whose contract digest differs. A host learns that it
  runs another contract than the installation from the team services' host record (signed
  by the team services, ``follow_contract``), which it reads when it starts and whenever
  it changes, or from a ``protocol_mismatch`` answer to its join or renewal. It logs both digests once and tells
  its listeners (``on_other_contract``), which drain its work; its record then says
  ``draining`` (``drain``) and, once it holds nothing, ``drained`` (``finish_drain``). From
  then on it renews only to give up its agents (``HostProfile.giving_up``): a renewal for
  more is turned away, so it asks for none while it drains. The host's own digest is its
  code's (``kinds.CONTRACT_SHA256``); a test may run a host on another one
  (``contract_sha256``).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import rfc8785
from pydantic import ValidationError

from custom_messages.wire.authority_bindings import token_issuer_lookup_bytes
from custom_messages.wire.encoding import b64url, format_timestamp
from custom_messages.wire.kinds import CONTRACT_SHA256, INSTALLATION_SCOPE, PROTOCOL
from custom_messages.wire.models import HostStateComponentsItem
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import HostCertificate
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.bootstrap import read_bundle
from custom_transport.protocol1.host_keys import (
    discard_pending_nats_key,
    load_host_keys,
    promote_pending_nats_key,
    rotate_nats_key,
    stage_pending_nats_key,
)
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.nats_jwt import NatsSigningKey
from custom_transport.protocol1.joining import (
    AUTHORITY_SERVICE_ID,
    HostProfile,
    JoinRefused,
    certificate_agent_types,
    join,
)
from custom_transport.protocol1.live import LiveHost, RequestRefused, utc_now
from custom_transport.protocol1.state_records import StateRecords, rising_sequence, require_record_key
from custom_transport.protocol1.trust_state import PersistentTrustStore
from release_version import release_version
from runtime_settings.instance_layout import InstanceLayout

# How often a host writes its liveness record, and how long each record lasts (module docstring).
LIVENESS_INTERVAL = timedelta(seconds=10)
LIVENESS_LIFETIME = timedelta(seconds=30)
TRUST_REFRESH_INTERVAL = timedelta(minutes=30)
# How often a host forgets trust records nothing can still need (README "Trust over time": at least daily).
FORGET_INTERVAL = timedelta(hours=6)
# How often the trust and certificate upkeep looks whether something is due.
UPKEEP_CHECK_INTERVAL = timedelta(seconds=10)
# How long the team services' host record is watched again after the watch failed.
_FOLLOW_RETRY_SECONDS = 5.0
# The most components a host record lists (the contract's host record, ``components``).
HOST_COMPONENTS_MAX = 16
# The team services' host id and role: their host record names the contract the installation runs (``follow_contract``).
TEAM_SERVICES_RUNTIME = "team-services"
TEAM_SERVICES_ROLE = "team_services"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rejoined:
    """What joining again for more agent types gave (``MessageHost.rejoin``)."""

    # The certificate the host signs with now: the new one, or the one it kept.
    certificate: HostCertificate
    # The requested types that certificate names: sessions for them may be opened.
    granted: frozenset[str]
    # Requested types the authority did not grant; ``refusal`` says why.
    refused: frozenset[str] = frozenset()
    # Requested names no certificate can carry (``joining.AGENT_TYPE_PATTERN``); never asked for.
    not_certifiable: frozenset[str] = frozenset()
    # The join's outcome and message when the authority refused it, as "<outcome>: <message>".
    refusal: Optional[str] = None


@dataclass(frozen=True)
class RenewalOutcome:
    """Result of a ``host.renew`` call (``MessageHost.renew``)."""

    ok: bool
    outcome: str
    message: str = ""
    certificate: Optional[HostCertificate] = None
    connection: Optional[dict[str, Any]] = None
    partitions: frozenset[int] = frozenset()
    credential_too_large: bool = False
    rotated_nats_key: bool = False


@dataclass
class MessageHost:
    """A process's host identity and its upkeep; ``live`` and ``records`` serve its own work."""

    layout: InstanceLayout
    profile: HostProfile
    nc: Any
    clock: Callable[[], datetime] = utc_now
    logger: logging.Logger = _LOGGER
    live: Optional[LiveHost] = None
    records: Optional[StateRecords] = None
    sessions_active: Callable[[], int] = field(default=lambda: 0)
    # The parts of the host that are not healthy, as the host record lists them (module docstring):
    # each ``{"component": <token id>, "state": "degraded" or "unhealthy", "code": <why>}``.
    components: Callable[[], Sequence[Mapping[str, str]]] = field(default=lambda: ())
    # The contract this host runs (module docstring): its code's, unless a test runs it on another.
    contract_sha256: str = CONTRACT_SHA256
    # What its record says: accepting, then draining and drained once it runs another contract than the installation.
    state: str = "accepting"
    # The installation's contract digest, once this host learned that it runs another one (module docstring).
    installation_contract: Optional[str] = None
    _contract_listeners: list[Callable[[str], None]] = field(default_factory=list)
    # Whether the renewal that gives up its agents got an answer; until then the upkeep asks again.
    _give_up_answered: bool = False
    # Why the team services' record did not verify last time, so each new reason is logged once.
    _contract_record_refused: Optional[str] = None
    _started_at: Optional[datetime] = None
    _liveness_sequence: int = 0
    # The state the last host record said, None before the first; records are written one at a time.
    _reported_state: Optional[str] = None
    _reporting: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_trust_refresh: Optional[datetime] = None
    _last_forget: Optional[datetime] = None
    # The trust store the host checks with, from its first join on; join replies are checked with it too.
    _trust: Optional[PersistentTrustStore] = None
    # The components the last record left out, so each new set of them is logged once.
    _components_left_out: tuple[str, ...] = ()
    # Held while the certificate changes (a renewal, a new join), so one never overwrites the other's.
    _certificate_change: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Ephemeral staged NATS key used only when layout is None in unit tests without keys.json.
    _ephemeral_pending_nats_key: Optional[NatsSigningKey] = None
    # Active NATS user connection credential issued by the authority (None under shared NATS login).
    connection: Optional[dict[str, Any]] = None
    credential_partitions: frozenset[int] = frozenset()
    credentials_issued: bool = False
    _connection_issued_at: Optional[datetime] = None
    _join_connection_recorded: Optional[dict[str, Any]] = None
    # Optional callbacks supplied by AgentProcessHost to supply owned partitions on routine renewals,
    # pause routine renewals during handover step 2, and react to updated NATS connection credentials.
    renewal_partitions: Optional[Callable[[], Sequence[int]]] = None
    renewal_paused: Optional[Callable[[], bool]] = None
    on_credential: Optional[Callable[[dict[str, Any], frozenset[int], bool], Any]] = None

    @property
    def identity(self) -> HostIdentity:
        """The one identity this host signs frames and records with (``live.identity``)."""

        assert self.live is not None, "start() first"
        return self.live.identity

    async def start(self, *, report: bool = True) -> LiveHost:
        """Join, read the current trust records and report this host as accepting work.

        ``report=False`` leaves the first host record to the caller: the team services
        write theirs only once they hold their lock and serve.
        """

        now = self.clock()
        keys = load_host_keys(self.layout, self.profile.runtime)
        trust = PersistentTrustStore.open(read_bundle(self.layout.trust_bootstrap_path),
                                          self.layout.host_trust_dir(self.profile.runtime), now)
        # The contract the host's join names, and against which it reads other hosts' records.
        trust.store.contract_sha256 = self.contract_sha256
        self._trust = trust
        # One identity: records are signed with what the live host signs frames with, so a
        # renewal or a new join switches both at once. The records exist before the join, whose
        # reply may name a certificate the host reads from UA1_CERTS (fetch_trust).
        self.records = StateRecords.on_connection(self.nc, lambda: self.identity, trust, fetch_trust=self.fetch_trust)
        self._join_connection_recorded = None
        try:
            certificate, keys = await self._join_recovering_revoked_nats_key(keys, trust)
        except JoinRefused as refused:
            if refused.outcome == "protocol_mismatch":
                self.learned_other_contract(refused.accepted_contract_sha256, "join")
            raise
        self.live = LiveHost(self.nc, HostIdentity(keys, certificate), trust, clock=self.clock, logger=self.logger,
                             fetch_trust=self.fetch_trust)
        self._started_at = now
        join_conn = self._join_connection_recorded
        self._join_connection_recorded = None
        if join_conn is not None:
            await self._notify_credential_change(join_conn, self.credential_partitions, rotated_nats_key=False)
        await self.refresh_trust()
        if report:
            await self.publish_liveness("accepting")
        return self.live

    def _record_join_connection(self, connection: Mapping[str, Any]) -> None:
        conn = dict(connection)
        self.connection = conn
        self._connection_issued_at = self.clock()
        self.credentials_issued = True
        self.credential_partitions = frozenset()
        self._join_connection_recorded = conn

    async def _notify_credential_change(
        self,
        connection: dict[str, Any],
        partitions: frozenset[int],
        *,
        rotated_nats_key: bool,
    ) -> None:
        if self.on_credential is None:
            return
        hook_result = self.on_credential(connection, partitions, rotated_nats_key)
        if inspect.isawaitable(hook_result):
            await hook_result

    async def _join_recovering_revoked_nats_key(
        self,
        keys: Any,
        trust: PersistentTrustStore,
        *,
        profile: Optional[HostProfile] = None,
    ) -> tuple[HostCertificate, Any]:
        """Join the installation, rotating a revoked stored NATS user key once (or promoting pending first) (C16)."""

        target_profile = self.profile if profile is None else profile
        try:
            certificate = await join(self.nc, keys, trust, target_profile, clock=self.clock, logger=self.logger,
                                     fetch_trust=self.fetch_trust, on_connection=self._record_join_connection)
            return certificate, keys
        except RequestRefused as refused:
            if refused.code != "invalid_body" or refused.at != "/body/nats_public_key":
                raise
            had_pending = keys.pending_nats_public_key is not None
            self.logger.warning(
                "Join refused the stored NATS user key; rotating the NATS user key and retrying join. "
                "event=protocol1_join_nats_key_rotated runtime=%s used_pending=%s",
                target_profile.runtime,
                had_pending,
            )
            keys = rotate_nats_key(self.layout, target_profile.runtime, prefer_pending=True)
            try:
                certificate = await join(self.nc, keys, trust, target_profile, clock=self.clock, logger=self.logger,
                                         fetch_trust=self.fetch_trust, on_connection=self._record_join_connection)
                return certificate, keys
            except RequestRefused as retry_refused:
                if (
                    not had_pending
                    or retry_refused.code != "invalid_body"
                    or retry_refused.at != "/body/nats_public_key"
                ):
                    raise
                self.logger.warning(
                    "Join also refused the promoted pending NATS user key; generating a fresh NATS user key and retrying once. "
                    "event=protocol1_join_nats_key_regenerated runtime=%s",
                    target_profile.runtime,
                )
                keys = rotate_nats_key(self.layout, target_profile.runtime, prefer_pending=False)
                certificate = await join(self.nc, keys, trust, target_profile, clock=self.clock, logger=self.logger,
                                         fetch_trust=self.fetch_trust, on_connection=self._record_join_connection)
                return certificate, keys

    async def fetch_trust(self, data: bytes) -> bool:
        """Read the certificate and session a frame or a host's record of this installation names.

        Certificates and sessions are published in ``UA1_CERTS`` for exactly this: a frame
        names its issuer's certificate and session, a state report its writer's
        certificate. Anything from another installation, or whose ids do not parse, takes
        nothing.
        """

        assert self.records is not None and self._trust is not None, "start() first"
        try:
            document = json.loads(data)
            issuer = document["issuer"] if "issuer" in document else document["writer"]
            if issuer.get("type", "host") != "host":
                return False
            installation, runtime = str(issuer["installation"]), str(issuer["runtime"])
            team, epoch = str(issuer.get("team", "")), int(issuer["membership_epoch"])
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
        if installation != self._trust.store.installation:
            return False
        now = self.clock()
        wanted = [("certificate", f"host.{installation}.{runtime}.{epoch}")]
        session = issuer.get("session")
        if isinstance(session, str):
            wanted.append(("agent_session", f"session.{installation}.{team}.{session}"))
        # Relayed commands also name the gateway that signed their authority token.
        authority = document.get("authority")
        token_issuer = authority.get("issuer") if isinstance(authority, dict) else None
        if isinstance(token_issuer, dict) and token_issuer.get("installation") == installation:
            try:
                wanted.append(("certificate", f"host.{installation}.{token_issuer['runtime']}.{int(token_issuer['membership_epoch'])}"))
            except (KeyError, TypeError, ValueError):
                pass
        check_revocations = getattr(self._trust.store, "revocations_current", None)
        if callable(check_revocations):
            with getattr(self._trust, "lock", nullcontext()):
                revocations_current = bool(check_revocations(installation, now))
            if not revocations_current:
                wanted.append(("revocations", f"revocations.{installation}"))
        taken = False
        for record, key in wanted:
            try:
                taken = await self.records.take_trust_record(record, key, now=now) or taken
            except FrameRefused as refused:
                self.logger.warning("A record named by a frame or record does not verify and is not taken. "
                                    "event=protocol1_trust_lookup_refused runtime=%s key=%s code=%s",
                                    self.profile.runtime, key, refused.code)
            except Exception as error:  # noqa: BLE001 - a lookup that fails takes nothing; the caller refuses as before
                self.logger.warning("A record named by a frame or record could not be read. "
                                    "event=protocol1_trust_lookup_failed runtime=%s key=%s error=%s: %s",
                                    self.profile.runtime, key, type(error).__name__, error)
        return taken

    def trust_state(self) -> Optional[tuple[Any, Any]]:
        """This host's ``TrustStore`` and lock once started, or ``None`` before ``start()``."""

        if self.live is not None:
            return self.live.trust_state()
        if self._trust is not None:
            return self._trust.trust_state()
        return None

    async def fetch_token_issuer(self, token: Any) -> bool:
        """Fetch the issuer certificate named by an authority token (and revocations if stale).

        Used before or on retry of authority token verification when a token arrives outside
        a protocol 1 frame (for example inside ``GatewayContext``) or after the issuer renewed
        to a newer membership epoch.
        """

        if self.records is None or self._trust is None:
            return False
        payload = token_issuer_lookup_bytes(token, self._trust.store.installation)
        if payload is None:
            return False
        return await self.fetch_trust(payload)

    async def refresh_trust(self) -> None:
        """Take the authority's newest revocations record; log, never stop, when it cannot be read."""

        assert self.records is not None
        installation = self.identity.installation
        try:
            await self.records.take_trust_record("revocations", f"revocations.{installation}", now=self.clock())
        except Exception:
            self.logger.exception("The current revocations record could not be read. event=protocol1_trust_refresh_failed "
                                  "runtime=%s", self.profile.runtime)
        self._last_trust_refresh = self.clock()

    def liveness_body(self, state: str) -> dict[str, Any]:
        identity = self.identity
        certificate = identity.certificate
        held = self.live.trust.store.held_record(  # type: ignore[union-attr]
            "certificate", f"host.{certificate.installation}.{certificate.runtime}.{certificate.membership_epoch}")
        assert held is not None  # the join kept it
        now = self.clock()
        components = self._components()
        return {
            "runtime": certificate.runtime,
            "certificate": {"certificate_id": certificate.certificate_id, "membership_epoch": certificate.membership_epoch,
                            "sha256": held[1]},
            "session_limit": certificate.session_limit,
            "sessions_active": min(self.sessions_active(), certificate.session_limit),
            "protocol": PROTOCOL,
            "contract_sha256": self.contract_sha256,
            "state": state,
            "health": "healthy" if all(item["state"] == "healthy" for item in components) else "degraded",
            "components": components,
            "placement_labels": [],
            "outbox_depth": 0,
            "revocation_epoch_seen": certificate.revocation_epoch,
            "started_at": format_timestamp(self._started_at or now),
            "heartbeat_at": format_timestamp(now),
            "software": {"name": "universal-agents", "version": release_version()},
        }

    def _components(self) -> list[dict[str, str]]:
        """The components to list, at most ``HOST_COMPONENTS_MAX``; none, logged, when they cannot be read.

        The caller decides which fit (an agent host sums up those beyond the limit); a
        failure here must never stop the host reporting its liveness.
        """

        try:
            listed = [dict(item) for item in self.components()]
        except Exception:
            self.logger.exception("The host's components could not be read; its record lists none. "
                                  "event=protocol1_host_components_failed runtime=%s", self.profile.runtime)
            return []
        kept: list[dict[str, str]] = []
        left_out: list[str] = []
        for item in listed:
            try:
                HostStateComponentsItem.model_validate(item)
            except ValidationError:
                left_out.append(str(item.get("component", "?"))[:80])
                continue
            kept.append(item)
        if tuple(left_out) != self._components_left_out:
            self._components_left_out = tuple(left_out)
            if left_out:
                self.logger.warning("Host components the contract cannot carry are left out of the host record. "
                                    "event=protocol1_host_components_invalid runtime=%s components=%s",
                                    self.profile.runtime, ",".join(left_out))
        return kept[:HOST_COMPONENTS_MAX]

    async def publish_liveness(self, state: str = "accepting") -> None:
        """Write this host's own record, which expires after ``LIVENESS_LIFETIME`` unless written again."""

        async with self._reporting:
            await self._publish(state)

    async def report_now(self) -> None:
        """Write the host record again now, as its components or its state changed; nothing before the first or after
        "drained"."""

        async with self._reporting:
            if self._reported_state is not None and self._reported_state != "drained":
                await self._publish(self.state)

    async def _publish(self, state: str) -> None:
        assert self.records is not None
        now = self.clock()
        # A sequence above every earlier one, also across restarts: at least the time in milliseconds.
        self._liveness_sequence = rising_sequence(self._liveness_sequence, now)
        runtime = self.profile.runtime
        document = self.records.signed("host", runtime, self.liveness_body(state), sequence=self._liveness_sequence,
                                       issued_at=now, expires_at=now + LIVENESS_LIFETIME)
        await self.records.put("host", runtime, document)
        self._reported_state = state

    async def renew_if_due(self) -> None:
        """Renew the certificate once two thirds of its life are gone; on another contract, only once drained."""

        if not self._renewal_due():
            return  # checked before the certificate's lock, which a join for more types may hold for minutes
        async with self._certificate_change:
            if self._renewal_due():
                await self._renew()

    def _renewal_due(self) -> bool:
        if self.renewal_paused is not None and self.renewal_paused():
            return False
        if self.state == "drained":
            return not self._give_up_answered  # the renewal that gives up its agents, until it is answered
        now = self.clock()
        certificate = self.identity.certificate
        life = certificate.expires_at - certificate.issued_at
        cert_due = now >= certificate.issued_at + life * 2 / 3
        conn_due = False
        if self.connection is not None and "expires_at" in self.connection:
            try:
                conn_exp = parse_timestamp(str(self.connection["expires_at"]))
                conn_start = self._connection_issued_at or certificate.issued_at
                conn_life = conn_exp - conn_start
                conn_due = conn_life <= timedelta(0) or now >= conn_start + conn_life * 2 / 3
            except ValueError:
                conn_due = True
        if not cert_due and not conn_due:
            return False
        if self.installation_contract is not None:
            # Only a renewal that gives up every agent is answered (module docstring).
            self.logger.debug("A host on another contract renews only once it drained. runtime=%s", self.profile.runtime)
            return False
        return True

    async def take_newest_certificate(self) -> None:
        """Sign with this host's newest certificate, which another process of the same host may hold; renew it if due.

        Every team services instance runs as host ``team-services`` with the same keys. A
        standby joined when it started and may wait for the lock for weeks, while the serving
        instance renews their certificate (day 20 of 30). A replaced certificate signs
        nothing new from ``certificate_overlap_seconds`` after its replacement was issued
        (README "Trust over time"): readers then refuse what it signs as ``unknown_key``, and
        the authority refuses its renewal the same way, so the first upkeep could not repair
        it. The authority stores each certificate in ``UA1_CERTS`` before it answers, under
        ``host.<installation>.<runtime>.<membership epoch>`` at the epoch above every stored
        one, so this reads the epochs above its own until one is missing and signs with the
        newest that verifies. A newest certificate for another key is not this process's to
        sign with (``IdentityError``). A renewal that is due but fails now is logged and left
        to the upkeep: the newest certificate still signs.
        """

        assert self.live is not None and self.records is not None, "start() first"
        async with self._certificate_change:
            current = self.identity.certificate
            newest, epoch = current, current.membership_epoch
            while True:
                epoch += 1
                key = f"host.{current.installation}.{current.runtime}.{epoch}"
                try:
                    await self.records.take_trust_record("certificate", key, now=self.clock())
                except FrameRefused as refused:
                    # Any process with the shared NATS login can write there; the authority stored the next above it.
                    self.logger.warning("A stored certificate of this host does not verify and is skipped. "
                                        "event=protocol1_stored_certificate_refused runtime=%s key=%s code=%s",
                                        current.runtime, key, refused.code)
                    continue
                found = self.live.trust.store.host_certificate(current.installation, current.runtime, epoch)
                if found is None:  # no certificate under this epoch: the one below is the newest
                    break
                newest = found
            if newest.membership_epoch != current.membership_epoch:
                self.live.identity = self.live.identity.with_certificate(newest)
                self.logger.warning("Another process of this host renewed its certificate; signing with the newest. "
                                    "event=protocol1_certificate_caught_up runtime=%s membership_epoch=%s previous=%s",
                                    current.runtime, newest.membership_epoch, current.membership_epoch)
        try:
            await self.renew_if_due()
        except Exception:
            self.logger.warning("The certificate is due for renewal but could not be renewed now; the upkeep asks "
                                "again. event=protocol1_renew_failed runtime=%s", self.profile.runtime, exc_info=True)

    async def renew(
        self,
        *,
        partitions: Optional[Sequence[int]] = None,
        rotate_nats_key: bool = False,
    ) -> RenewalOutcome:
        """Renew the certificate now; keep the old one until the new one is held."""

        async with self._certificate_change:
            return await self._renew(partitions=partitions, rotate_nats_key=rotate_nats_key)

    def renewal_profile(self) -> HostProfile:
        """The profile a renewal asks for: the host's own, or none of its agents once it drained (module docstring)."""

        return self.profile.giving_up() if self.state == "drained" else self.profile

    def _stage_new_nats_public_key(self, runtime: str, *, fresh: bool = False) -> str:
        """Stage and persist a new NATS user key before a key-rotating renewal (C03)."""

        if self.layout is not None:
            if fresh:
                discard_pending_nats_key(self.layout, runtime)
            staged = stage_pending_nats_key(self.layout, runtime)
            assert staged.pending_nats_public_key is not None
            return staged.pending_nats_public_key
        if fresh or self._ephemeral_pending_nats_key is None:
            self._ephemeral_pending_nats_key = NatsSigningKey.generate("user")
        return self._ephemeral_pending_nats_key.public_key

    def _promote_nats_key_if_needed(self, runtime: str, expected_public_key: Optional[str]) -> None:
        if expected_public_key is None or self.live is None:
            return
        if self.layout is not None:
            promoted = promote_pending_nats_key(self.layout, runtime, expected_public_key=expected_public_key)
            if hasattr(self.live.identity, "with_keys"):
                self.live.identity = self.live.identity.with_keys(promoted)
            return
        self._ephemeral_pending_nats_key = None

    async def _renew(
        self,
        *,
        partitions: Optional[Sequence[int]] = None,
        rotate_nats_key: bool = False,
    ) -> RenewalOutcome:
        certificate = self.identity.certificate
        assert self.live is not None
        giving_up_agents = self.state == "drained" and bool(getattr(certificate, "agent_types", ()))
        if partitions is None and self.renewal_partitions is not None and self.state != "drained":
            partitions = self.renewal_partitions()
        requested_partitions = () if self.state == "drained" else tuple(sorted(set(partitions or ())))
        giving_up_partitions = bool(self.credential_partitions - frozenset(requested_partitions))
        needs_new_nats_key = rotate_nats_key or giving_up_agents or giving_up_partitions

        async def _send(*, rotate: bool, fresh_key: bool = False) -> tuple[Any, Optional[str]]:
            new_key = self._stage_new_nats_public_key(certificate.runtime, fresh=fresh_key) if rotate else None
            body_payload: dict[str, Any] = {
                "renew_id": self.renewal_id(
                    certificate,
                    partitions=requested_partitions,
                    new_nats_public_key=new_key,
                ),
                "certificate_id": certificate.certificate_id,
                "membership_epoch": certificate.membership_epoch,
                "profile_sha256": self.renewal_profile().digest(),
                "contract_sha256": self.contract_sha256,
            }
            if requested_partitions:
                body_payload["partitions"] = list(requested_partitions)
            if new_key is not None:
                body_payload["new_nats_public_key"] = new_key
            response = await self.live.request(  # type: ignore[union-attr]
                "host.renew",
                body_payload,
                {"installation": certificate.installation, "team": INSTALLATION_SCOPE, "target": "installation_service",
                 "id": AUTHORITY_SERVICE_ID},
                team=INSTALLATION_SCOPE,
            )
            return response, new_key

        try:
            reply, new_nats_public_key = await _send(rotate=needs_new_nats_key, fresh_key=False)
        except RequestRefused as refused:
            # F1/F2: Recover when the authority requires a new NATS user key (e.g. out-of-band host_lost
            # key revocation, ledger reset, or rejected staged key) by rotating and retrying once.
            if refused.code == "invalid_body" and refused.at == "/body/new_nats_public_key":
                self.logger.warning(
                    "Renewal required a fresh NATS user key; rotating the NATS user key and retrying once. "
                    "event=protocol1_renew_nats_key_regenerated runtime=%s was_rotating=%s",
                    certificate.runtime,
                    needs_new_nats_key,
                )
                reply, new_nats_public_key = await _send(rotate=True, fresh_key=needs_new_nats_key)
            else:
                raise

        body = reply.raw["body"]
        outcome = str(body["outcome"])
        message = str(body.get("message") or "")
        if self.state == "drained":
            self._give_up_answered = True
        if outcome == "protocol_mismatch":
            self.learned_other_contract(body.get("accepted_contract_sha256"), "renewal")
            return RenewalOutcome(ok=False, outcome=outcome, message=message)
        if outcome != "ok":
            too_large = outcome == "not_allowed" and "max_credential_bytes" in message.lower()
            self.logger.error("The certificate could not be renewed. event=protocol1_renew_refused runtime=%s outcome=%s "
                              "message=%s", certificate.runtime, outcome, message)
            return RenewalOutcome(ok=False, outcome=outcome, message=message, credential_too_large=too_large)
        self.live.trust.add(json.dumps(body["certificate"]).encode(), now=self.clock())
        renewed = self.live.trust.store.host_certificate(certificate.installation, certificate.runtime,
                                                         int(body["certificate"]["body"]["membership_epoch"]))
        assert renewed is not None
        if new_nats_public_key is not None:
            self._promote_nats_key_if_needed(
                certificate.runtime,
                getattr(renewed, "nats_public_key", new_nats_public_key),
            )
        self.live.identity = self.live.identity.with_certificate(renewed)
        conn: Optional[dict[str, Any]] = None
        if isinstance(body.get("connection"), Mapping):
            conn = dict(body["connection"])
            self.connection = conn
            self._connection_issued_at = self.clock()
            self.credentials_issued = True
            self.credential_partitions = frozenset(requested_partitions)
            await self._notify_credential_change(
                conn,
                self.credential_partitions,
                rotated_nats_key=new_nats_public_key is not None,
            )
        self.logger.info("Renewed the host certificate. event=protocol1_renewed runtime=%s membership_epoch=%s "
                         "agent_types=%s session_limit=%s partitions=%s", certificate.runtime, renewed.membership_epoch,
                         ",".join(sorted(renewed.agent_types)), renewed.session_limit, len(requested_partitions))
        return RenewalOutcome(
            ok=True,
            outcome="ok",
            certificate=renewed,
            connection=conn,
            partitions=frozenset(requested_partitions),
            rotated_nats_key=new_nats_public_key is not None,
        )

    async def rejoin(self, agent_types: Iterable[str]) -> Rejoined:
        """Join again asking for ``agent_types`` as well, and sign everything with the certificate it gives.

        For a host whose certificate lacks a type it now runs, such as a specialist the
        team created while it runs, whose ``sessions.open`` was refused ``not_allowed``.
        The join asks for the types the host's certificate and profile already name plus
        the requested ones, never fewer, so no running agent loses its type. The authority
        keeps the types the host's certificate grants, as a renewal does, also one since
        removed from the catalog; it grants a join only whole: when the launcher's list and
        the catalog both lack a new type asked for, it refuses the join and the host keeps
        its certificate. A granted join gets a
        certificate at the next membership epoch that replaces the old one; frames and
        records are signed with it from then on, and later renewals and joins ask for the
        same types. Requested types the certificate already names need no join. Names no
        certificate can carry are left out, with a warning, and reported. Raises
        ``ServiceUnavailable`` when the authority does not answer in time; the caller may
        ask again later.
        """

        requested = frozenset(agent_types)
        certifiable = certificate_agent_types(requested, self.logger)
        not_certifiable = requested - certifiable
        rejoin_conn: Optional[dict[str, Any]] = None
        async with self._certificate_change:
            assert self.live is not None, "start() first"
            current = self.identity.certificate
            if certifiable <= current.agent_types:
                return Rejoined(current, certifiable, not_certifiable=not_certifiable)
            # F12/CR-5: Never rejoin while a partition handover is draining (would wipe credential_partitions).
            if self.renewal_paused is not None and self.renewal_paused():
                return Rejoined(
                    current,
                    certifiable & current.agent_types,
                    refused=certifiable - current.agent_types,
                    not_certifiable=not_certifiable,
                    refusal="deferred: partition handover in progress",
                )
            asked = frozenset(self.profile.agent_types) | current.agent_types | certifiable
            profile = replace(self.profile, agent_types=tuple(sorted(asked)))
            self._join_connection_recorded = None
            old_keys = self.identity.keys
            try:
                certificate, new_keys = await self._join_recovering_revoked_nats_key(
                    old_keys, self.live.trust, profile=profile
                )
            except JoinRefused as refused:
                self._join_connection_recorded = None
                self.logger.warning("The authority did not grant more agent types; the host keeps its certificate. "
                                    "event=protocol1_rejoin_refused runtime=%s outcome=%s agent_types=%s message=%s",
                                    current.runtime, refused.outcome, ",".join(sorted(certifiable - current.agent_types)),
                                    refused.message)
                return Rejoined(current, certifiable & current.agent_types, refused=certifiable - current.agent_types,
                                not_certifiable=not_certifiable, refusal=f"{refused.outcome}: {refused.message}")
            self.profile = profile
            rotated_nats = new_keys.nats_public_key != old_keys.nats_public_key
            if rotated_nats and hasattr(self.live.identity, "with_keys"):
                self.live.identity = self.live.identity.with_keys(new_keys)
            self.live.identity = self.live.identity.with_certificate(certificate)
            rejoin_conn = self._join_connection_recorded
            self._join_connection_recorded = None
            # F12/CR-5: Notify inside _certificate_change so async on_credential hooks stay ordered with renew().
            if rejoin_conn is not None:
                await self._notify_credential_change(
                    rejoin_conn, self.credential_partitions, rotated_nats_key=rotated_nats
                )
        self.logger.info("Joined again for more agent types. event=protocol1_rejoined runtime=%s membership_epoch=%s "
                         "agent_types=%s", certificate.runtime, certificate.membership_epoch,
                         ",".join(sorted(certificate.agent_types)))
        return Rejoined(certificate, certifiable & certificate.agent_types, refused=certifiable - certificate.agent_types,
                        not_certifiable=not_certifiable)

    def renewal_id(
        self,
        certificate: HostCertificate,
        *,
        partitions: Sequence[int] = (),
        new_nats_public_key: Optional[str] = None,
    ) -> str:
        """The id of this host's renewal of ``certificate``: kept on every retry, new after an upgrade in place.

        A repeated renewal id gets its stored outcome back (README "Contract digest",
        "Stored outcomes"), so the id names what renews as well as what is renewed: the
        certificate, the profile asked for, the software release, the contract digest, and
        any requested partitions or rotated NATS key (C03).
        A digest of them keeps every id whole within the contract's 128 characters, where
        cutting a long one could repeat another's.
        """

        renewing: list[Any] = [certificate.runtime, certificate.certificate_id, certificate.membership_epoch,
                               self.renewal_profile().digest(), release_version(), self.contract_sha256]
        sorted_partitions = sorted(set(partitions))
        if sorted_partitions or new_nats_public_key is not None:
            renewing.extend([sorted_partitions, new_nats_public_key or ""])
        return f"renew-{certificate.membership_epoch}-{b64url(hashlib.sha256(rfc8785.dumps(renewing)).digest())}"

    # -- another contract (module docstring) ------------------------------------------------------

    def on_other_contract(self, listener: Callable[[str], None]) -> None:
        """Call ``listener`` with the installation's digest once this host learns that it runs another contract."""

        self._contract_listeners.append(listener)

    def learned_other_contract(self, theirs: Optional[str], learned_from: str) -> None:
        """This host runs another contract than the installation's ``theirs``: log it once and tell the listeners."""

        if self.installation_contract is not None or not theirs or theirs == self.contract_sha256:
            return
        self.installation_contract = theirs
        self.logger.error("This host runs another message contract than its installation, so it takes no new work, "
                          "finishes or hands back what it holds and then gives up its agents. Install the "
                          "installation's release here and start this process again. "
                          "event=protocol1_host_other_contract runtime=%s contract_sha256=%s "
                          "installation_contract_sha256=%s learned_from=%s", self.profile.runtime,
                          self.contract_sha256, theirs, learned_from)
        for listener in list(self._contract_listeners):
            try:
                listener(theirs)
            except Exception:
                self.logger.exception("A listener for another contract failed. event=protocol1_contract_listener_failed "
                                      "runtime=%s", self.profile.runtime)

    async def follow_contract(self, runtime: str = TEAM_SERVICES_RUNTIME) -> None:
        """Read ``runtime``'s host record (the team services') when it starts and whenever it changes; runs until cancelled.

        A record that the team services signed and that names another digest than this
        host's means the installation runs another contract (``learned_other_contract``).
        A record that does not verify, or that another host signed (every process shares one
        NATS login in this phase, so any can write the key), says nothing about it and is
        logged. A watch that fails is started again a few seconds later.
        """

        assert self.records is not None and self.live is not None, "start() first"
        while True:
            try:
                async for key, raw in self.records.watch("host", runtime):
                    if raw is not None:
                        await self._contract_record(key, raw, runtime)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the watch keeps running through a NATS failure
                self.logger.warning("Watching the team services' host record failed; watching again. "
                                    "event=protocol1_contract_watch_failed runtime=%s error=%s: %s",
                                    self.profile.runtime, type(error).__name__, error)
            await asyncio.sleep(_FOLLOW_RETRY_SECONDS)

    async def _contract_record(self, key: str, raw: bytes, runtime: str) -> None:
        """Learn the installation's contract from one version of ``runtime``'s host record (``follow_contract``)."""

        assert self.live is not None
        try:
            try:
                record = self.live.trust.store.verify(raw, record="host", now=self.clock())
            except FrameRefused as refused:
                if refused.code != "unknown_key" or not await self.fetch_trust(raw):
                    raise
                record = self.live.trust.store.verify(raw, record="host", now=self.clock())
            require_record_key(record, key)
            signer = record.signer
            if record.document.key != runtime or signer is None or signer.runtime != runtime \
                    or TEAM_SERVICES_ROLE not in signer.roles:
                raise FrameRefused("wrong_issuer", f"only {runtime} itself writes its host record", at="/writer")
        except FrameRefused as refused:
            if refused.code != self._contract_record_refused:  # each new reason once, not at every write
                self._contract_record_refused = refused.code
                self.logger.info("A host record did not verify; it says nothing about the installation's "
                                 "contract. event=protocol1_contract_record_refused runtime=%s key=%s code=%s",
                                 self.profile.runtime, key, refused.code)
            return
        self._contract_record_refused = None
        if record.other_contract:
            self.learned_other_contract(record.their_contract_sha256, "team_services_record")

    async def drain(self) -> None:
        """Say ``draining`` in the host record now: the host takes no new work and finishes or hands back what it holds."""

        if self.state == "drained":
            return
        self.state = "draining"
        await self.report_now()

    async def finish_drain(self) -> None:
        """Say ``drained`` once the host holds nothing, and renew to give up its agents (module docstring)."""

        self.state = "drained"
        if self.records is not None and self._reported_state is not None:
            await self.publish_liveness("drained")
        if self.live is not None:
            try:
                async with self._certificate_change:
                    await self._renew()
            except Exception:
                self.logger.warning("The renewal that gives up this host's agents got no answer; the upkeep asks "
                                    "again. event=protocol1_give_up_renewal_failed runtime=%s", self.profile.runtime,
                                    exc_info=True)

    async def keep_up(self, stop: asyncio.Event, *, drained: Callable[[], bool] = lambda: True) -> None:
        """Liveness every 10 seconds, and beside it trust, forgetting and renewal when due, until ``stop`` is set.

        The two run apart (module docstring): a renewal that waits for the authority or a
        join that holds the certificate's lock never delays a liveness write. Once ``stop``
        is set no liveness write goes out, and a trust step still waiting (a renewal may wait
        30 seconds) is cancelled rather than waited for, so a stopping process releases what
        it holds at once; a renewal cut off that way is asked again with the same id. The
        last record says the host drained, unless ``drained()`` says otherwise: a team
        services instance that lost its lock leaves the record to the one serving now.
        """

        trust = asyncio.create_task(self._trust_loop(stop))
        try:
            await self._liveness_loop(stop)
        finally:
            trust.cancel()
            await asyncio.gather(trust, return_exceptions=True)
        if drained():
            await self.publish_liveness("drained")

    async def _liveness_loop(self, stop: asyncio.Event) -> None:
        while not await _stopped_within(stop, LIVENESS_INTERVAL):
            try:
                await self.publish_liveness(self.state)
            except Exception:
                self.logger.exception("The host record could not be written; trying again at the next interval. "
                                      "event=protocol1_host_liveness_failed runtime=%s", self.profile.runtime)

    async def _trust_loop(self, stop: asyncio.Event) -> None:
        while not await _stopped_within(stop, UPKEEP_CHECK_INTERVAL):
            try:
                now = self.clock()
                if self._last_trust_refresh is None or now - self._last_trust_refresh >= TRUST_REFRESH_INTERVAL:
                    await self.refresh_trust()
                if self._last_forget is None or now - self._last_forget >= FORGET_INTERVAL:
                    self._forget(now)
                await self.renew_if_due()
            except Exception:
                self.logger.exception("A host upkeep step failed; trying again. event=protocol1_host_upkeep_failed "
                                      "runtime=%s", self.profile.runtime)

    def _forget(self, now: datetime) -> None:
        """Forget the trust records nothing can still need (README "Trust over time", at least daily)."""

        assert self.live is not None
        self._last_forget = now
        with getattr(self.live.trust, "lock", nullcontext()):
            forgotten = self.live.trust.store.forget(now)
        if forgotten:
            self.logger.info("Forgot trust records nothing can still need. event=protocol1_trust_forgotten runtime=%s "
                             "records=%s", self.profile.runtime, forgotten)


async def _stopped_within(stop: asyncio.Event, interval: timedelta) -> bool:
    """Wait ``interval`` or until ``stop``; whether ``stop`` is set."""

    try:
        await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
    except asyncio.TimeoutError:
        pass
    return stop.is_set()

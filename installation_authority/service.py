"""The installation authority's work: topology, certificates, revocations and the bootstrap bundle.

Rules from the message contract's README ("Installation traffic", "Trust over time")
and the registry's write rules for ``certificate`` and ``revocations``:

- **Certificates** last 30 days and name what the join asked for, never more. A host
  that asks again with the key its certificate holds gets that certificate back while
  more than a third of its life is left; otherwise, and on a renewal, it gets a new one
  with the next membership epoch that ``replaces`` the old one. A host whose key is
  revoked gets none.
- **The revocations record** is signed again every 6 hours and lasts 24 hours, with a
  sequence above every one before it and at least its issue time in milliseconds, so an
  authority restored from a backup never writes a lower one.
- **The bootstrap bundle** carries the authority keys, the authority's own
  certificate and the newest revocations record, so a host can check a join reply
  before it has a certificate.
- **Topology.** The authority creates the key-value buckets and each team's
  dead-letter stream and consumer; the queued work streams come with queued work.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional

from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import BucketNotFoundError, KeyNotFoundError, NoKeysError, NotFoundError

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from custom_messages.wire.encoding import b64url, format_timestamp
from custom_messages.wire.frame import Receiver, VerifiedFrame
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.kinds import CONTRACT_SHA256, INSTALLATION_SCOPE, PROTOCOL
from custom_messages.wire.trust import HostCertificate, sign_state
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.bootstrap import BootstrapBundle, read_bundle, write_bundle
from custom_transport.protocol1.contract_files import transport
from custom_transport.protocol1.host_keys import AuthorityKeys, HostKeys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.joining import AUTHORITY_SERVICE_ID, HostProfile
from custom_transport.protocol1.live import LiveHost, Unsupported, utc_now
from custom_transport.protocol1.trust_state import PersistentTrustStore
from installation_authority.expected_hosts import read_expected_hosts
from release_version import release_version
from runtime_settings.instance_layout import InstanceLayout

AUTHORITY_RUNTIME = "authority"
CERTIFICATE_LIFETIME = timedelta(days=30)
REVOCATIONS_LIFETIME = timedelta(hours=24)
REVOCATIONS_RESIGN_INTERVAL = timedelta(hours=6)
CERTIFICATES_BUCKET = "UA1_CERTS"
_INSTALLATION_ADDRESS = "_install.{sender}.svc"
_RETENTION = {"workqueue": RetentionPolicy.WORK_QUEUE, "limits": RetentionPolicy.LIMITS}
_STORAGE = {"file": StorageType.FILE, "memory": StorageType.MEMORY}
_LOGGER = logging.getLogger(__name__)


def certificate_key(installation: str, runtime: str, epoch: int) -> str:
    return f"host.{installation}.{runtime}.{epoch}"


def revocations_key(installation: str) -> str:
    return f"revocations.{installation}"


@dataclass(frozen=True)
class CertificateRequest:
    """What a certificate grants: the host's keys and what its join asked for."""

    runtime: str
    teams: tuple[str, ...]
    roles: tuple[str, ...]
    agent_types: tuple[str, ...]
    session_limit: int
    connection_class: str
    key_id: str
    public_key: str
    nats_public_key: str
    profile_sha256: str
    deployment: str = "local"


class InstallationAuthority:
    """One installation's authority: it signs with ``authority`` and speaks as the host ``host_keys``."""

    def __init__(self, layout: InstanceLayout, authority: AuthorityKeys, host_keys: HostKeys, *,
                 clock: Callable[[], datetime] = utc_now, logger: logging.Logger = _LOGGER) -> None:
        if host_keys.runtime != AUTHORITY_RUNTIME:
            raise ValueError(f"the authority speaks as host {AUTHORITY_RUNTIME!r}")
        self.layout = layout
        self.authority = authority
        self.host_keys = host_keys
        self.clock = clock
        self.logger = logger
        self.live: Optional[LiveHost] = None
        self._js: Any = None
        self._own: dict[str, Any] = {}
        self._revocations: dict[str, Any] = {}
        self._lock = asyncio.Lock()

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
            "profile_sha256": request.profile_sha256,
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
        """Set up the topology for ``teams``, the authority's own certificate and revocations, then answer joins."""

        self._js = nc.jetstream()
        teams = sorted(set(teams))
        await self.ensure_topology(teams)
        now = self.clock()
        revocations = await self._sign_revocations(now)
        own = await self._own_certificate(teams, revocations["body"]["revocation_epoch"], now)
        self._own = own
        bundle = self._publish_bundle()
        trust = PersistentTrustStore.open(bundle, self.layout.host_trust_dir(AUTHORITY_RUNTIME), now)
        certificate = trust.store.host_certificate(self.installation, AUTHORITY_RUNTIME,
                                                   int(own["body"]["membership_epoch"]))
        assert certificate is not None  # just signed with the bundle's authority key
        self.live = LiveHost(nc, HostIdentity(self.host_keys, certificate), trust, clock=self.clock, logger=self.logger)
        await self._take_issued_certificates(now)
        await self.live.serve(_INSTALLATION_ADDRESS, Receiver(self.installation, INSTALLATION_SCOPE, "installation_service",
                                                              AUTHORITY_SERVICE_ID, AUTHORITY_RUNTIME), self.handle)
        await nc.flush()
        self.logger.info("The installation authority is serving. event=installation_authority_ready installation=%s "
                         "membership_epoch=%s teams=%s", self.installation, certificate.membership_epoch, ",".join(teams))

    async def ensure_topology(self, teams: Iterable[str]) -> None:
        """Create what the contract's topology owner owns in phase 2: the buckets and each team's dead letters."""

        spec = transport()
        for bucket, settings in spec["key_value"].items():
            try:
                await self._js.key_value(bucket)
            except BucketNotFoundError:
                await self._js.create_key_value(bucket=bucket, ttl=settings["per_key_ttl_seconds"] or None,
                                                storage=StorageType.FILE, history=1)
                self.logger.info("Created a key-value bucket. event=protocol1_bucket_created bucket=%s", bucket)
        dead_letters = spec["streams"]["UA1_DLQ_{team}"]
        consumer = spec["consumers"]["dead_letters"]
        for team in teams:
            name = "UA1_DLQ_{team}".format(team=team)
            config = StreamConfig(name=name, subjects=[subject.format(team=team) for subject in dead_letters["subjects"]],
                                  retention=_RETENTION[dead_letters["retention"]], storage=_STORAGE[dead_letters["storage"]],
                                  max_age=dead_letters["max_age_seconds"], max_msg_size=dead_letters["max_message_bytes"],
                                  max_consumers=dead_letters["max_consumers"])
            try:
                await self._js.stream_info(name)
                await self._js.update_stream(config=config)
            except NotFoundError:
                await self._js.add_stream(config=config)
                self.logger.info("Created a team's dead-letter stream. event=protocol1_stream_created stream=%s", name)
            await self._js.add_consumer(name, config=ConsumerConfig(
                durable_name=consumer["name"].format(team=team), filter_subject=consumer["filter"].format(team=team),
                ack_policy=AckPolicy.EXPLICIT, ack_wait=consumer["ack_wait_seconds"], max_deliver=consumer["max_deliveries"]))

    async def _records(self) -> Any:
        return await self._js.key_value(CERTIFICATES_BUCKET)

    async def _stored(self, key: str) -> Optional[dict[str, Any]]:
        try:
            entry = await (await self._records()).get(key)
        except KeyNotFoundError:
            return None
        return json.loads(entry.value) if entry.value else None

    async def _sign_revocations(self, now: datetime) -> dict[str, Any]:
        """The next revocations record: the stored one's entries, a higher sequence, and a day's life."""

        async with self._lock:
            stored = await self._stored(revocations_key(self.installation)) or self._revocations or None
            entries = stored["body"]["entries"] if stored else []
            epoch = int(stored["body"]["revocation_epoch"]) if stored else 1
            sequence = max(int(stored["sequence"]) + 1 if stored else 1, int(now.timestamp() * 1000))
            record = self.revocations_record(sequence=sequence, revocation_epoch=epoch, entries=entries, issued=now)
            await (await self._records()).put(revocations_key(self.installation), json.dumps(record).encode())
            self._revocations = record
            if self.live is not None:
                self.live.trust.add(json.dumps(record).encode(), now=now)
                self._publish_bundle()
            return record

    def _publish_bundle(self) -> BootstrapBundle:
        """Write the bundle with the authority's certificate and the newest revocations record."""

        bundle = read_bundle(self.layout.trust_bootstrap_path).with_records([self._own, self._revocations])
        write_bundle(self.layout.trust_bootstrap_path, bundle)
        return bundle

    async def resign_revocations_forever(self) -> None:
        """Sign the revocations record again every six hours, so it never stops being current."""

        while True:
            await asyncio.sleep(REVOCATIONS_RESIGN_INTERVAL.total_seconds())
            try:
                await self._sign_revocations(self.clock())
            except Exception:
                self.logger.exception("The revocations record could not be signed again; hosts refuse other hosts' "
                                      "frames once it expires. event=revocations_resign_failed installation=%s",
                                      self.installation)

    async def _take_issued_certificates(self, now: datetime) -> None:
        """Hold every certificate this authority issued, so it recognises the hosts that renew with them."""

        assert self.live is not None
        try:
            keys = [key for key in await (await self._records()).keys() if key.startswith(f"host.{self.installation}.")]
        except (NoKeysError, NotFoundError):  # no key in the bucket yet
            return
        for key in keys:
            stored = await self._stored(key)
            if stored is None:
                continue
            try:
                self.live.trust.add(json.dumps(stored).encode(), now=now)
            except FrameRefused as refused:
                # An ended or revoked certificate no longer counts; renewals with it are refused.
                self.logger.info("A stored certificate is not taken. event=stored_certificate_skipped key=%s code=%s",
                                 key, refused.code)

    async def _latest_certificate(self, runtime: str) -> Optional[dict[str, Any]]:
        """The stored certificate of ``runtime`` with the highest membership epoch."""

        prefix = f"host.{self.installation}.{runtime}."
        try:
            keys = [key for key in await (await self._records()).keys() if key.startswith(prefix)]
        except (NoKeysError, NotFoundError):  # the bucket holds no keys yet
            keys = []
        epochs = sorted((int(key.rsplit(".", 1)[1]) for key in keys if key.rsplit(".", 1)[1].isdigit()), reverse=True)
        for epoch in epochs:
            stored = await self._stored(certificate_key(self.installation, runtime, epoch))
            if stored is not None:
                return stored
        return None

    def _reusable(self, stored: Optional[Mapping[str, Any]], key_id: str, public_key: str, now: datetime) -> bool:
        """Whether a host asking again can keep its certificate: same key, and over a third of its life left."""

        if stored is None:
            return False
        body = stored["body"]
        issued, expires = parse_timestamp(body["issued_at"]), parse_timestamp(body["expires_at"])
        return ((body["application_key"]["key_id"], body["application_key"]["public_key"]) == (key_id, public_key)
                and expires - now > (expires - issued) / 3)

    async def _own_certificate(self, teams: list[str], revocation_epoch: int, now: datetime) -> dict[str, Any]:
        stored = await self._latest_certificate(AUTHORITY_RUNTIME)
        if self._reusable(stored, self.host_keys.key_id, self.host_keys.public_key_text, now) and stored is not None \
                and set(stored["body"]["teams"]) == set(teams or stored["body"]["teams"]):
            return stored
        request = CertificateRequest(runtime=AUTHORITY_RUNTIME, teams=tuple(teams), roles=("installation_authority",),
                                     agent_types=(), session_limit=0, connection_class="connected",
                                     key_id=self.host_keys.key_id, public_key=self.host_keys.public_key_text,
                                     nats_public_key=self.host_keys.nats_public_key, profile_sha256=_profile_of(teams))
        return await self._issue(request, stored, revocation_epoch, now)

    async def _issue(self, request: CertificateRequest, previous: Optional[Mapping[str, Any]], revocation_epoch: int,
                     now: datetime) -> dict[str, Any]:
        epoch = int(previous["body"]["membership_epoch"]) + 1 if previous else 1
        replaces = previous["body"]["certificate_id"] if previous and parse_timestamp(previous["body"]["expires_at"]) > now \
            else None
        record = self.certificate_record(request, epoch=epoch, revocation_epoch=revocation_epoch, issued=now,
                                         replaces=replaces)
        await (await self._records()).put(certificate_key(self.installation, request.runtime, epoch), json.dumps(record).encode())
        if self.live is not None:
            self.live.trust.add(json.dumps(record).encode(), now=now)
        self.logger.info("Issued a host certificate. event=host_certificate_issued runtime=%s membership_epoch=%s "
                         "key_id=%s replaces=%s", request.runtime, epoch, request.key_id, replaces)
        return record

    # -- answering ------------------------------------------------------------------------------

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        if verified.frame.kind == "host.join":
            return await self.answer_join(verified)
        if verified.frame.kind == "host.renew":
            return await self.answer_renew(verified)
        raise Unsupported(verified.frame.kind)

    def _protocol_mismatch(self, digest: str) -> Optional[dict[str, Any]]:
        """The answer to a join or renewal from a host on another pre-release contract (README "Versions").

        It names the digest this authority runs, so the host learns why it was turned away.
        """

        if PROTOCOL.endswith("-pre") and digest != CONTRACT_SHA256:
            return {"outcome": "protocol_mismatch", "accepted": PROTOCOL, "accepted_contract_sha256": CONTRACT_SHA256,
                    "install": f"universal-agents {release_version()}",
                    "message": "This host runs another pre-release contract than the installation; install the same release."}
        return None

    async def answer_join(self, verified: VerifiedFrame) -> dict[str, Any]:
        body = verified.raw["body"]
        mismatch = self._protocol_mismatch(str(body["contract_sha256"]))
        if mismatch is not None:
            return mismatch
        runtime, key = str(body["runtime"]), body["application_key"]
        expected = read_expected_hosts(self.layout.trust_expected_hosts_path).get(runtime)
        denial = _denial(expected, body)
        if denial is not None:
            self.logger.warning("A join was refused. event=host_join_denied runtime=%s reason=%s key_id=%s",
                                runtime, denial, key["key_id"])
            return {"outcome": "denied", "reason": denial,
                    "message": "This installation takes only the hosts its launcher starts."}
        # verify_frame already refused a join presenting a revoked key (README "Joining").
        now = self.clock()
        stored = await self._latest_certificate(runtime)
        if self._reusable(stored, key["key_id"], key["public_key"], now) and stored is not None \
                and _grants_at_most(stored["body"], body):
            return {"outcome": "ok", "certificate": stored}
        request = CertificateRequest(runtime=runtime, teams=tuple(body["teams"]), roles=tuple(body["roles"]),
                                     agent_types=tuple(body["agent_types"]), session_limit=int(body["session_limit"]),
                                     connection_class=str(body["connection_class"]), key_id=key["key_id"],
                                     public_key=key["public_key"], nats_public_key=str(body["nats_public_key"]),
                                     profile_sha256=str(body["profile_sha256"]), deployment=str(body["deployment"]))
        record = await self._issue(request, stored, int(self._revocations["body"]["revocation_epoch"]), now)
        return {"outcome": "ok", "certificate": record}

    async def answer_renew(self, verified: VerifiedFrame) -> dict[str, Any]:
        body = verified.raw["body"]
        mismatch = self._protocol_mismatch(str(body["contract_sha256"]))
        if mismatch is not None:
            return mismatch
        certificate = verified.certificate
        assert certificate is not None and self.live is not None  # a renewal is signed by the certificate it renews
        rotation = body.get("application_key_rotation")
        key = rotation["key"] if rotation else {"key_id": certificate.key_id, "public_key": _public_text(certificate)}
        request = CertificateRequest(runtime=certificate.runtime, teams=tuple(sorted(certificate.teams)),
                                     roles=tuple(sorted(certificate.roles)), agent_types=tuple(sorted(certificate.agent_types)),
                                     session_limit=certificate.session_limit, connection_class=certificate.connection_class,
                                     key_id=key["key_id"], public_key=key["public_key"],
                                     nats_public_key=str(body.get("new_nats_public_key") or certificate.nats_public_key),
                                     profile_sha256=str(body["profile_sha256"]))
        stored = await self._latest_certificate(certificate.runtime)
        record = await self._issue(request, stored, int(self._revocations["body"]["revocation_epoch"]), self.clock())
        return {"outcome": "ok", "certificate": record}


def _profile_of(teams: Iterable[str]) -> str:
    return HostProfile(AUTHORITY_RUNTIME, tuple(teams), ("installation_authority",)).digest()


def _public_text(certificate: HostCertificate) -> str:
    return b64url(certificate.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw))


def _denial(expected: Any, body: Mapping[str, Any]) -> Optional[str]:
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
    return None


def _grants_at_most(certificate: Mapping[str, Any], asked: Mapping[str, Any]) -> bool:
    """Whether a stored certificate grants no more than a join now asks for, so it may be handed back."""

    return (set(certificate["teams"]) <= set(asked["teams"]) and set(certificate["roles"]) <= set(asked["roles"])
            and set(certificate["agent_types"]) <= set(asked["agent_types"])
            and int(certificate["session_limit"]) <= int(asked["session_limit"])
            and certificate["connection_class"] == asked["connection_class"])

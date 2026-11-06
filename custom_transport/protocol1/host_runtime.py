"""One process's life as a protocol 1 host: join, keep its trust current, renew, and report liveness.

Every host does the same around its own work (README "Installation traffic", "Trust
over time", "State records"):

- **join** with the key the launcher made for it, and keep the certificate it gets;
- **keep a current revocations record**: read the authority's newest one from
  ``UA1_CERTS`` at start and every half hour (the authority signs one every six hours
  and each lasts a day), so the host never refuses other hosts' frames for lack of one;
- **renew** its certificate when two thirds of its life are gone;
- **join again** for agent types its certificate does not name yet (``rejoin``), such as a
  specialist type the team created while the host runs, so every running agent can hold
  a session;
- **report liveness**: write its own host record every 10 seconds, each lasting 30, so a
  host that stops shows as gone within 30 seconds of its last write, as the presence
  heartbeat did, and one or two lost writes do not make a running host look gone.
  Nothing moves because of that. The record lists the parts of the host that are not
  healthy (``components``, each with a code saying why), such as an agent whose session
  the team services refused; the gateway shows them as Status page findings (plan 13B.5).
  The host is then ``degraded``: it still serves everything else. When they change, the
  host writes its record again at once (``report_now``) rather than at the next interval;
- **notice another contract** (README "Versions"). While the label is a pre-release, hosts
  of one installation refuse a peer whose contract digest differs. A host learns that it
  runs another contract than the installation from the team services' host record, which
  it reads when it starts and whenever it changes (``follow_contract``), or from a
  ``protocol_mismatch`` answer to its join or renewal. It logs both digests once and tells
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
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import rfc8785

from custom_messages.wire.encoding import b64url, format_timestamp
from custom_messages.wire.kinds import CONTRACT_SHA256, INSTALLATION_SCOPE, PROTOCOL
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import HostCertificate
from custom_transport.protocol1.bootstrap import read_bundle
from custom_transport.protocol1.host_keys import load_host_keys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.joining import (
    AUTHORITY_SERVICE_ID,
    HostProfile,
    JoinRefused,
    certificate_agent_types,
    join,
)
from custom_transport.protocol1.live import LiveHost, utc_now
from custom_transport.protocol1.state_records import StateRecords
from custom_transport.protocol1.trust_state import PersistentTrustStore
from release_version import release_version
from runtime_settings.instance_layout import InstanceLayout

# How often a host writes its liveness record, and how long each record lasts (module docstring).
LIVENESS_INTERVAL = timedelta(seconds=10)
LIVENESS_LIFETIME = timedelta(seconds=30)
TRUST_REFRESH_INTERVAL = timedelta(minutes=30)
# The most components a host record lists (the contract's host record, ``components``).
HOST_COMPONENTS_MAX = 16
# The team services' host id: their host record names the contract the installation runs (``follow_contract``).
TEAM_SERVICES_RUNTIME = "team-services"
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
    _started_at: Optional[datetime] = None
    _liveness_sequence: int = 0
    # The state the last host record said, None before the first; records are written one at a time.
    _reported_state: Optional[str] = None
    _reporting: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_trust_refresh: Optional[datetime] = None
    # Held while the certificate changes (a renewal, a new join), so one never overwrites the other's.
    _certificate_change: asyncio.Lock = field(default_factory=asyncio.Lock)

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
        try:
            certificate = await join(self.nc, keys, trust, self.profile, clock=self.clock, logger=self.logger)
        except JoinRefused as refused:
            if refused.outcome == "protocol_mismatch":
                self.learned_other_contract(refused.accepted_contract_sha256, "join")
            raise
        self.live = LiveHost(self.nc, HostIdentity(keys, certificate), trust, clock=self.clock, logger=self.logger,
                             fetch_trust=self.fetch_trust)
        # One identity: records are signed with what the live host signs frames with, so a
        # renewal or a new join switches both at once.
        self.records = StateRecords(self.nc.jetstream(), lambda: self.identity, trust, fetch_trust=self.fetch_trust)
        self._started_at = now
        await self.refresh_trust()
        if report:
            await self.publish_liveness("accepting")
        return self.live

    async def fetch_trust(self, data: bytes) -> bool:
        """Read the certificate and session a frame or a host's record of this installation names.

        Certificates and sessions are published in ``UA1_CERTS`` for exactly this: a frame
        names its issuer's certificate and session, a state report its writer's
        certificate. Anything from another installation, or whose ids do not parse, takes
        nothing.
        """

        assert self.records is not None
        try:
            document = json.loads(data)
            issuer = document["issuer"] if "issuer" in document else document["writer"]
            if issuer.get("type", "host") != "host":
                return False
            installation, runtime = str(issuer["installation"]), str(issuer["runtime"])
            team, epoch = str(issuer.get("team", "")), int(issuer["membership_epoch"])
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
        if installation != self.identity.installation:
            return False
        now = self.clock()
        taken = await self.records.take_trust_record("certificate", f"host.{installation}.{runtime}.{epoch}", now=now)
        session = issuer.get("session")
        if isinstance(session, str):
            taken = await self.records.take_trust_record("agent_session", f"session.{installation}.{team}.{session}",
                                                         now=now) or taken
        return await self._fetch_token_issuer(document, now) or taken

    async def _fetch_token_issuer(self, document: Any, now: datetime) -> bool:
        """The certificate of the host that signed a frame's authority token (a relayed command's gateway)."""

        assert self.records is not None
        try:
            token_issuer = document["authority"]["issuer"]
            installation, runtime = str(token_issuer["installation"]), str(token_issuer["runtime"])
            epoch = int(token_issuer["membership_epoch"])
        except (KeyError, TypeError, ValueError):
            return False
        if installation != self.identity.installation:
            return False
        return await self.records.take_trust_record("certificate", f"host.{installation}.{runtime}.{epoch}", now=now)

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
            return [dict(item) for item in self.components()][:HOST_COMPONENTS_MAX]
        except Exception:
            self.logger.exception("The host's components could not be read; its record lists none. "
                                  "event=protocol1_host_components_failed runtime=%s", self.profile.runtime)
            return []

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
        self._liveness_sequence = max(self._liveness_sequence + 1, int(now.timestamp() * 1000))
        runtime = self.profile.runtime
        document = self.records.signed("host", runtime, self.liveness_body(state), sequence=self._liveness_sequence,
                                       issued_at=now, expires_at=now + LIVENESS_LIFETIME)
        await self.records.put("host", runtime, document)
        self._reported_state = state

    async def renew_if_due(self) -> None:
        """Renew the certificate once two thirds of its life are gone; on another contract, only once drained."""

        async with self._certificate_change:
            certificate = self.identity.certificate
            life = certificate.expires_at - certificate.issued_at
            if self.clock() < certificate.issued_at + life * 2 / 3:
                return
            if self.installation_contract is not None and self.state != "drained":
                # Only a renewal that gives up every agent is answered (module docstring).
                self.logger.debug("A host on another contract renews only once it drained. runtime=%s",
                                  self.profile.runtime)
                return
            await self._renew()

    async def renew(self) -> None:
        """Renew the certificate now; keep the old one until the new one is held."""

        async with self._certificate_change:
            await self._renew()

    def renewal_profile(self) -> HostProfile:
        """The profile a renewal asks for: the host's own, or none of its agents once it drained (module docstring)."""

        return self.profile.giving_up() if self.state == "drained" else self.profile

    async def _renew(self) -> None:
        certificate = self.identity.certificate
        assert self.live is not None
        reply = await self.live.request("host.renew", {
            "renew_id": self.renewal_id(certificate),
            "certificate_id": certificate.certificate_id, "membership_epoch": certificate.membership_epoch,
            "profile_sha256": self.renewal_profile().digest(), "contract_sha256": self.contract_sha256},
            {"installation": certificate.installation, "team": INSTALLATION_SCOPE, "target": "installation_service",
             "id": AUTHORITY_SERVICE_ID}, team=INSTALLATION_SCOPE)
        body = reply.raw["body"]
        if body["outcome"] == "protocol_mismatch":
            self.learned_other_contract(body.get("accepted_contract_sha256"), "renewal")
            return
        if body["outcome"] != "ok":
            self.logger.error("The certificate could not be renewed. event=protocol1_renew_refused runtime=%s outcome=%s",
                              certificate.runtime, body["outcome"])
            return
        self.live.trust.add(json.dumps(body["certificate"]).encode(), now=self.clock())
        renewed = self.live.trust.store.host_certificate(certificate.installation, certificate.runtime,
                                                         int(body["certificate"]["body"]["membership_epoch"]))
        assert renewed is not None
        self.live.identity = self.live.identity.with_certificate(renewed)
        self.logger.info("Renewed the host certificate. event=protocol1_renewed runtime=%s membership_epoch=%s "
                         "agent_types=%s session_limit=%s", certificate.runtime, renewed.membership_epoch,
                         ",".join(sorted(renewed.agent_types)), renewed.session_limit)

    async def rejoin(self, agent_types: Iterable[str]) -> Rejoined:
        """Join again asking for ``agent_types`` as well, and sign everything with the certificate it gives.

        For a host whose certificate lacks a type it now runs, such as a specialist the
        team created while it runs, whose ``sessions.open`` was refused ``not_allowed``.
        The join asks for the types the host's certificate and profile already name plus
        the requested ones, never fewer, so no running agent loses its type. The authority
        grants a join only whole: when the launcher's list or the catalog lacks any type
        asked for, it refuses it and the host keeps its certificate. A granted join gets a
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
        async with self._certificate_change:
            assert self.live is not None, "start() first"
            current = self.identity.certificate
            if certifiable <= current.agent_types:
                return Rejoined(current, certifiable, not_certifiable=not_certifiable)
            asked = frozenset(self.profile.agent_types) | current.agent_types | certifiable
            profile = replace(self.profile, agent_types=tuple(sorted(asked)))
            try:
                certificate = await join(self.nc, self.identity.keys, self.live.trust, profile, clock=self.clock,
                                         logger=self.logger)
            except JoinRefused as refused:
                self.logger.warning("The authority did not grant more agent types; the host keeps its certificate. "
                                    "event=protocol1_rejoin_refused runtime=%s outcome=%s agent_types=%s message=%s",
                                    current.runtime, refused.outcome, ",".join(sorted(certifiable - current.agent_types)),
                                    refused.message)
                return Rejoined(current, certifiable & current.agent_types, refused=certifiable - current.agent_types,
                                not_certifiable=not_certifiable, refusal=f"{refused.outcome}: {refused.message}")
            self.profile = profile
            self.live.identity = self.live.identity.with_certificate(certificate)
        self.logger.info("Joined again for more agent types. event=protocol1_rejoined runtime=%s membership_epoch=%s "
                         "agent_types=%s", certificate.runtime, certificate.membership_epoch,
                         ",".join(sorted(certificate.agent_types)))
        return Rejoined(certificate, certifiable & certificate.agent_types, refused=certifiable - certificate.agent_types,
                        not_certifiable=not_certifiable)

    def renewal_id(self, certificate: HostCertificate) -> str:
        """The id of this host's renewal of ``certificate``: kept on every retry, new after an upgrade in place.

        A repeated renewal id gets its stored outcome back (README "Contract digest",
        "Stored outcomes"), so the id names what renews as well as what is renewed: the
        certificate, the profile asked for, the software release and the contract digest.
        A digest of them keeps every id whole within the contract's 128 characters, where
        cutting a long one could repeat another's.
        """

        renewing: list[str | int] = [certificate.runtime, certificate.certificate_id, certificate.membership_epoch,
                                     self.renewal_profile().digest(), release_version(), self.contract_sha256]
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

        A record that names another digest than this host's means the installation runs
        another contract (``learned_other_contract``). A record that does not verify says
        nothing about it and is logged.
        """

        assert self.records is not None and self.live is not None, "start() first"
        async for key, raw in self.records.watch("host", runtime):
            if raw is None:
                continue
            try:
                record = self.live.trust.store.verify(raw, record="host", now=self.clock())
            except FrameRefused as refused:
                if refused.code != "unknown_key" or not await self.fetch_trust(raw):
                    self.logger.info("A host record did not verify; it says nothing about the installation's contract. "
                                     "event=protocol1_contract_record_refused runtime=%s key=%s code=%s",
                                     self.profile.runtime, key, refused.code)
                    continue
                try:
                    record = self.live.trust.store.verify(raw, record="host", now=self.clock())
                except FrameRefused:
                    continue
            if record.other_contract:
                self.learned_other_contract(record.their_contract_sha256, "team_services_record")

    async def drain(self) -> None:
        """Say ``draining`` in the host record now: the host takes no new work and finishes or hands back what it holds."""

        if self.state != "accepting":
            return
        self.state = "draining"
        await self.report_now()

    async def finish_drain(self) -> None:
        """Say ``drained`` once the host holds nothing, and renew to give up its agents (module docstring)."""

        if self.state == "drained":
            return
        self.state = "drained"
        if self.records is not None and self._reported_state is not None:
            await self.publish_liveness("drained")
        if self.live is not None:
            async with self._certificate_change:
                await self._renew()

    async def keep_up(self, stop: asyncio.Event, *, drained: Callable[[], bool] = lambda: True) -> None:
        """Liveness every 10 seconds, trust every half hour and renewal when due, until ``stop`` is set.

        The last record says the host drained, unless ``drained()`` says otherwise: a team
        services instance that lost its lock leaves the record to the one serving now.
        """

        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=LIVENESS_INTERVAL.total_seconds())
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            try:
                if self._last_trust_refresh is None or self.clock() - self._last_trust_refresh >= TRUST_REFRESH_INTERVAL:
                    await self.refresh_trust()
                await self.renew_if_due()
                await self.publish_liveness(self.state)
            except Exception:
                self.logger.exception("A host upkeep step failed; trying again. event=protocol1_host_upkeep_failed "
                                      "runtime=%s", self.profile.runtime)
        if drained():
            await self.publish_liveness("drained")

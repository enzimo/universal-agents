"""One process's life as a protocol 1 host: join, keep its trust current, renew, and report liveness.

Every host does the same around its own work (README "Installation traffic", "Trust
over time", "State records"):

- **join** with the key the launcher made for it, and keep the certificate it gets;
- **keep a current revocations record**: read the authority's newest one from
  ``UA1_CERTS`` at start and every half hour (the authority signs one every six hours
  and each lasts a day), so the host never refuses other hosts' frames for lack of one;
- **renew** its certificate when two thirds of its life are gone;
- **report liveness**: write its own host record every 20 seconds, expiring within 60, so
  a host that stops shows as gone within a minute. Nothing moves because of that.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.kinds import INSTALLATION_SCOPE, PROTOCOL
from custom_transport.protocol1.bootstrap import read_bundle
from custom_transport.protocol1.contract_files import contract_sha256
from custom_transport.protocol1.host_keys import load_host_keys
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.joining import AUTHORITY_SERVICE_ID, HostProfile, join
from custom_transport.protocol1.live import LiveHost, utc_now
from custom_transport.protocol1.state_records import StateRecords
from custom_transport.protocol1.trust_state import PersistentTrustStore
from release_version import release_version
from runtime_settings.instance_layout import InstanceLayout

LIVENESS_INTERVAL = timedelta(seconds=20)
LIVENESS_LIFETIME = timedelta(seconds=60)
TRUST_REFRESH_INTERVAL = timedelta(minutes=30)
_LOGGER = logging.getLogger(__name__)


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
    _started_at: Optional[datetime] = None
    _liveness_sequence: int = 0
    _last_trust_refresh: Optional[datetime] = None

    @property
    def identity(self) -> HostIdentity:
        assert self.live is not None, "start() first"
        return self.live.identity

    async def start(self) -> LiveHost:
        """Join, read the current trust records and report this host as accepting work."""

        now = self.clock()
        keys = load_host_keys(self.layout, self.profile.runtime)
        trust = PersistentTrustStore.open(read_bundle(self.layout.trust_bootstrap_path),
                                          self.layout.host_trust_dir(self.profile.runtime), now)
        certificate = await join(self.nc, keys, trust, self.profile, clock=self.clock, logger=self.logger)
        identity = HostIdentity(keys, certificate)
        self.live = LiveHost(self.nc, identity, trust, clock=self.clock, logger=self.logger, fetch_trust=self.fetch_trust)
        self.records = StateRecords(self.nc.jetstream(), identity, trust, fetch_trust=self.fetch_trust)
        self._started_at = now
        await self.refresh_trust()
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
        return {
            "runtime": certificate.runtime,
            "certificate": {"certificate_id": certificate.certificate_id, "membership_epoch": certificate.membership_epoch,
                            "sha256": held[1]},
            "session_limit": certificate.session_limit,
            "sessions_active": min(self.sessions_active(), certificate.session_limit),
            "protocol": PROTOCOL,
            "contract_sha256": contract_sha256(),
            "state": state,
            "health": "healthy",
            "components": [],
            "placement_labels": [],
            "outbox_depth": 0,
            "revocation_epoch_seen": certificate.revocation_epoch,
            "started_at": format_timestamp(self._started_at or now),
            "heartbeat_at": format_timestamp(now),
            "software": {"name": "universal-agents", "version": release_version()},
        }

    async def publish_liveness(self, state: str = "accepting") -> None:
        """Write this host's own record, which expires within a minute unless written again."""

        assert self.records is not None
        now = self.clock()
        # A sequence above every earlier one, also across restarts: at least the time in milliseconds.
        self._liveness_sequence = max(self._liveness_sequence + 1, int(now.timestamp() * 1000))
        runtime = self.profile.runtime
        document = self.records.signed("host", runtime, self.liveness_body(state), sequence=self._liveness_sequence,
                                       issued_at=now, expires_at=now + LIVENESS_LIFETIME)
        await self.records.put("host", runtime, document)

    async def renew_if_due(self) -> None:
        """Renew the certificate once two thirds of its life are gone; keep the old one until the new one is held."""

        certificate = self.identity.certificate
        life = certificate.expires_at - certificate.issued_at
        if self.clock() < certificate.issued_at + life * 2 / 3:
            return
        assert self.live is not None
        reply = await self.live.request("host.renew", {
            "renew_id": f"renew-{certificate.runtime}-{certificate.membership_epoch}",
            "certificate_id": certificate.certificate_id, "membership_epoch": certificate.membership_epoch,
            "profile_sha256": self.profile.digest(), "contract_sha256": contract_sha256()},
            {"installation": certificate.installation, "team": INSTALLATION_SCOPE, "target": "installation_service",
             "id": AUTHORITY_SERVICE_ID}, team=INSTALLATION_SCOPE)
        body = reply.raw["body"]
        if body["outcome"] != "ok":
            self.logger.error("The certificate could not be renewed. event=protocol1_renew_refused runtime=%s outcome=%s",
                              certificate.runtime, body["outcome"])
            return
        self.live.trust.add(json.dumps(body["certificate"]).encode(), now=self.clock())
        renewed = self.live.trust.store.host_certificate(certificate.installation, certificate.runtime,
                                                         int(body["certificate"]["body"]["membership_epoch"]))
        assert renewed is not None
        self.live.identity = self.live.identity.with_certificate(renewed)
        self.logger.info("Renewed the host certificate. event=protocol1_renewed runtime=%s membership_epoch=%s",
                         certificate.runtime, renewed.membership_epoch)

    async def keep_up(self, stop: asyncio.Event) -> None:
        """Liveness every 20 seconds, trust every half hour and renewal when due, until ``stop`` is set."""

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
                await self.publish_liveness("accepting")
            except Exception:
                self.logger.exception("A host upkeep step failed; trying again. event=protocol1_host_upkeep_failed "
                                      "runtime=%s", self.profile.runtime)
        await self.publish_liveness("drained")

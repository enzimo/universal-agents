"""A host joins its installation and gets its certificate (README "Installation traffic").

A new host has no certificate, so it signs ``host.join`` with the key it presents
(``verification: proof_of_possession``). The installation authority answers with a
signed certificate, which the host checks against the authority keys of its bootstrap
bundle (``verify_frame`` does, with the reply's request) and keeps in its trust store.
A local host is approved at once when the launcher listed it; ``pending`` makes it wait
and ask again with the same join id, as the contract says. A host renews with
``host.renew`` signed by the certificate it renews (``LiveHost.request``); this module
only joins.

The authority renews its own certificate while it runs, and stores the new one in
``UA1_CERTS`` before it signs with it. A host whose bundle copy is older, then joins (or
joins again for more agent types), gets a reply signed by a certificate it does not hold
yet (``unknown_key``). ``join`` then reads that certificate from ``UA1_CERTS`` through
the caller's lookup (``fetch_trust``, ``MessageHost.fetch_trust``: the key
``host.<installation>.<runtime>.<membership epoch>`` the reply's issuer names), which the
trust store checks against the bundle's authority keys, and checks the reply again, once.
The bundle file is never read again: a host on another machine holds its own copy, made
when it was set up.

The contract's pre-join permissions (``generated/permissions/joining_host.json``) do not
let a host that has not joined yet read ``UA1_CERTS``; every process shares one NATS login
in this phase, so the lookup works for every host here. A host that joins again already
holds the reader grants.

A join names the contract the host runs (``contract_sha256``): its trust store's, which is
the one its code was generated from (``custom_messages.wire.kinds.CONTRACT_SHA256``)
unless a test gives another. While the label is a pre-release, an authority on another
contract answers ``protocol_mismatch`` and names its own (README "Versions").
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

import rfc8785
from nats.errors import NoRespondersError, TimeoutError as NatsTimeoutError

from custom_messages.wire.encoding import b64url
from custom_messages.wire.frame import Receiver, reply_audience, sign_frame, verify_frame
from custom_messages.wire.kinds import CONTRACT_SHA256, INSTALLATION_SCOPE, PROTOCOL
from custom_messages.wire.models import Frame
from custom_messages.wire.refusals import FrameRefused
from custom_messages.wire.trust import HostCertificate
from custom_transport.protocol1.host_keys import HostKeys
from custom_transport.protocol1.ids import new_message_id
from custom_transport.protocol1.live import (
    ServiceUnavailable,
    answer_headers,
    build_frame,
    encode,
    raise_if_refused,
    request_subject,
    utc_now,
)
from custom_transport.protocol1.replay import LiveReplayRecord
from custom_transport.protocol1.trust_state import PersistentTrustStore
from custom_transport.send_barrier import before_send

# The installation authority's service id, which joins and renewals name as their audience.
AUTHORITY_SERVICE_ID = "authority"
_LOGGER = logging.getLogger(__name__)


# The contract's agent_type: until grouped queues, at most 24 characters (plan decision 56).
AGENT_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,23}$")


def certificate_agent_types(agent_types: Iterable[str], logger: logging.Logger) -> frozenset[str]:
    """The agent types a certificate can name; a type it cannot is left out, with a warning."""

    allowed = frozenset(agent_type for agent_type in agent_types if AGENT_TYPE_PATTERN.match(agent_type))
    for left_out in sorted(set(agent_types) - allowed):
        logger.warning("An agent type is too long or has characters a host certificate cannot carry; its agents "
                       "get no protocol 1 session. event=protocol1_agent_type_not_certifiable agent_type=%s", left_out)
    return allowed


@dataclass(frozen=True)
class HostProfile:
    """What a host asks to be: the teams, roles and agent types it serves and how many sessions it runs."""

    runtime: str
    teams: tuple[str, ...]
    roles: tuple[str, ...]
    agent_types: tuple[str, ...] = ()
    session_limit: int = 0
    connection_class: str = "connected"
    deployment: str = "local"
    software: Mapping[str, str] = field(default_factory=dict)

    def digest(self) -> str:
        """``profile_sha256``: the unpadded base64url SHA-256 of the profile's RFC 8785 JSON."""

        profile: dict[str, Any] = {"runtime": self.runtime, "teams": sorted(self.teams), "roles": sorted(self.roles),
                   "agent_types": sorted(self.agent_types), "session_limit": self.session_limit,
                   "connection_class": self.connection_class}
        return b64url(hashlib.sha256(rfc8785.dumps(profile)).digest())

    def giving_up(self) -> "HostProfile":
        """The profile a host renews with to give up all its agents: the same, with no agent types or sessions.

        A host on another contract renews only this way (README "Versions"); the authority
        answers it on any contract and grants no agents (``installation_authority``).
        """

        return replace(self, agent_types=(), session_limit=0)


class JoinRefused(RuntimeError):
    """The authority answered the join with ``denied``, ``protocol_mismatch`` or ``not_allowed``.

    ``accepted_contract_sha256`` is the authority's contract digest when it answered
    ``protocol_mismatch``.
    """

    def __init__(self, outcome: str, message: str, accepted_contract_sha256: Optional[str] = None) -> None:
        super().__init__(f"join {outcome}: {message}")
        self.outcome, self.message = outcome, message
        self.accepted_contract_sha256 = accepted_contract_sha256


def join_body(keys: HostKeys, installation: str, profile: HostProfile, join_id: str,
              contract_sha256: str = CONTRACT_SHA256) -> dict[str, Any]:
    body: dict[str, Any] = {
        "join_id": join_id,
        "installation": installation,
        "runtime": profile.runtime,
        "teams": sorted(profile.teams),
        "roles": sorted(profile.roles),
        "application_key": {"key_id": keys.key_id, "public_key": keys.public_key_text},
        "nats_public_key": keys.nats_public_key,
        "agent_types": sorted(profile.agent_types),
        "session_limit": profile.session_limit,
        "connection_class": profile.connection_class,
        "protocol": PROTOCOL,
        "contract_sha256": contract_sha256,
        "deployment": profile.deployment,
        "profile_sha256": profile.digest(),
    }
    if profile.software:
        body["software"] = dict(profile.software)
    return body


def sign_join(keys: HostKeys, installation: str, profile: HostProfile, *, join_id: str, issued: datetime,
              contract_sha256: str = CONTRACT_SHA256) -> dict[str, Any]:
    """A join frame, signed with the key it presents: the issuer names epoch zero and no certificate."""

    issuer = {"installation": installation, "team": INSTALLATION_SCOPE, "runtime": profile.runtime,
              "membership_epoch": 0, "revocation_epoch": 0, "key_id": keys.key_id}
    audience = {"installation": installation, "team": INSTALLATION_SCOPE, "target": "installation_service",
                "id": AUTHORITY_SERVICE_ID}
    frame = build_frame("host.join", join_body(keys, installation, profile, join_id, contract_sha256), audience,
                        issuer=issuer,
                        issued=issued, subject=request_subject("host.join", audience, profile.runtime))
    return sign_frame(frame, keys.application_key)


async def join(nc: Any, keys: HostKeys, trust: PersistentTrustStore, profile: HostProfile, *,
               clock: Callable[[], datetime] = utc_now, deadline_seconds: float = 120.0,
               logger: logging.Logger = _LOGGER, sleep: Callable[[float], Any] = asyncio.sleep,
               fetch_trust: Optional[Callable[[bytes], Awaitable[bool]]] = None) -> HostCertificate:
    """Join the installation and return this host's certificate, kept in ``trust``.

    The join names the contract ``trust`` runs. Waits for an authority that is still
    starting ("no responders") and for a ``pending`` approval, up to ``deadline_seconds``;
    raises ``JoinRefused`` when the authority refuses the join and ``ServiceUnavailable``
    when the deadline passes. ``fetch_trust`` reads a certificate the reply names that
    ``trust`` lacks, such as the authority's renewed one, from ``UA1_CERTS`` (module docstring).
    """

    installation = trust.store.installation
    join_id = f"join-{new_message_id()}"
    replay = LiveReplayRecord()
    loop = asyncio.get_running_loop()
    give_up = loop.time() + deadline_seconds
    pause = 0.5
    while True:
        frame = sign_join(keys, installation, profile, join_id=join_id, issued=clock(),
                          contract_sha256=trust.store.contract_sha256)
        data, headers = encode(frame)
        await before_send()
        try:
            message = await nc.request(frame["subject"], data, timeout=30, headers=headers)
        except (NoRespondersError, NatsTimeoutError) as error:
            # The authority is still starting, or busy: ask again until the deadline.
            if loop.time() + pause > give_up:
                raise ServiceUnavailable("host.join", frame["subject"], "the installation authority did not answer") from error
            logger.info("Waiting for the installation authority. event=protocol1_join_waiting runtime=%s", profile.runtime)
            await sleep(pause)
            pause = min(pause * 2, 5.0)
            continue
        raise_if_refused("host.join", frame["subject"], message, logger)
        request = Frame.model_validate(frame)
        now = clock()
        reply = await _verified_reply(message, request, trust, now, replay, fetch_trust)
        body = reply.raw["body"]
        outcome = body["outcome"]
        if outcome == "ok":
            trust.add(json.dumps(body["certificate"]).encode(), now=now)
            certificate_body = body["certificate"]["body"]
            certificate = trust.store.host_certificate(installation, profile.runtime, int(certificate_body["membership_epoch"]))
            assert certificate is not None  # verify_frame checked the certificate and add() kept it
            logger.info("Joined the installation. event=protocol1_joined runtime=%s membership_epoch=%s expires_at=%s",
                        profile.runtime, certificate.membership_epoch, certificate.expires_at.isoformat())
            return certificate
        if outcome == "pending":
            wait = float(body.get("retry_after_seconds", 5))
            if loop.time() + wait > give_up:
                raise JoinRefused("pending", str(body.get("message", "the join waits for an administrator's approval")))
            logger.warning("The join waits for approval. event=protocol1_join_pending runtime=%s review_code=%s",
                           profile.runtime, body.get("review_code"))
            await sleep(wait)
            continue
        raise JoinRefused(outcome, str(body.get("message", "")), body.get("accepted_contract_sha256"))


async def _verified_reply(message: Any, request: Frame, trust: PersistentTrustStore, now: datetime,
                          replay: LiveReplayRecord, fetch_trust: Optional[Callable[[bytes], Awaitable[bool]]]) -> Any:
    """The authority's reply to ``request``, verified; checked again once after reading a certificate ``trust`` lacks."""

    named = reply_audience(request)

    def verified() -> Any:
        return verify_frame(message.data, trust=trust.store,
                            receiver=Receiver(named["installation"], named["team"], named["target"], named["id"],
                                              named["runtime"]),
                            arrived_on=message.subject, now=now, replay=replay.lookup,
                            headers=answer_headers(message), answering=request)

    try:
        return verified()
    except FrameRefused as refused:
        if refused.code != "unknown_key" or fetch_trust is None or not await fetch_trust(message.data):
            raise
        return verified()

"""NATS user credential revocation adapters for the installation authority.

contracts/messages/v1/README.md ("NATS users and the account", owner decision 100):
- On the bundled server in operator mode, the authority holds an operator signing key and
  pushes a full, operator-signed account JWT (keeping all partition mappings, JetStream
  limits, account signing keys, and the cumulative revocation map) to the server over a
  system-account connection via ``$SYS.REQ.CLAIMS.UPDATE``. nats-server 2.15.1 closes open
  connections of the revoked key immediately and rejects reconnects.
- On an external server where the server operator gives UA no operator key, the authority
  cannot push account updates; ``LifetimeWaitRevoker`` records that instant revocation is
  unavailable so handovers wait ``transport.nats.credential_lifetime_seconds +
  clock_skew_seconds`` instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol

from custom_transport.protocol1.nats_jwt import NatsSigningKey, encode_account_jwt
from custom_transport.protocol1.nkeys import account_public_key_bytes, public_key_bytes

logger = logging.getLogger(__name__)

CLAIMS_UPDATE_SUBJECT = "$SYS.REQ.CLAIMS.UPDATE"
DEFAULT_CLAIMS_UPDATE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class RevocationOutcome:
    """Result of asking the NATS server to revoke a user key."""

    revoked: bool
    revoked_at: int
    detail: str = ""


@dataclass(frozen=True)
class AccountBaseClaims:
    """Authoritative non-revocation claims of the installation's NATS account.

    Every account JWT update rebuilds from these base claims plus the full cumulative
    revocation map so a revocation never drops partition mappings, signing keys, JetStream
    limits, or earlier revocations.
    """

    account_public_key: str
    account_name: str = "installation"
    signing_keys: tuple[str, ...] = ()
    mappings: Mapping[str, str] = field(default_factory=dict)
    limits: Optional[Mapping[str, Any]] = None
    jetstream_tiers: Optional[Mapping[str, Mapping[str, int]]] = None

    def __post_init__(self) -> None:
        account_public_key_bytes(self.account_public_key)
        for key in self.signing_keys:
            account_public_key_bytes(key)


class NatsRevoker(Protocol):
    """Revokes a host's or bootstrap bundle's NATS user key on the server, if supported."""

    @property
    def supports_instant_revocation(self) -> bool:
        ...

    async def revoke(
        self,
        nats_public_key: str,
        *,
        revoke_until: int,
        all_revocations: Mapping[str, int] = ...,
    ) -> RevocationOutcome:
        ...


class LifetimeWaitRevoker:
    """External-server fallback: no operator key is held, so credentials end at expiry."""

    @property
    def supports_instant_revocation(self) -> bool:
        return False

    async def revoke(
        self,
        nats_public_key: str,
        *,
        revoke_until: int,
        all_revocations: Mapping[str, int] = (),  # type: ignore[assignment]
    ) -> RevocationOutcome:
        public_key_bytes(nats_public_key)
        return RevocationOutcome(
            revoked=False,
            revoked_at=int(revoke_until),
            detail="instant revocation unavailable; waiting for credential lifetime expiry",
        )


class OperatorModeRevoker:
    """Pushes cumulative operator-signed account JWT updates via ``$SYS.REQ.CLAIMS.UPDATE``.

    ``system_nc`` must be a NATS connection authenticated as a user of the server's system
    account (a connection on the installation account gets no responders on
    ``$SYS.REQ.CLAIMS.UPDATE``). Updates are serialized with an ``asyncio.Lock`` so
    concurrent revocations cannot overwrite a newer account JWT with an older one.
    """

    def __init__(
        self,
        operator_signing_key: NatsSigningKey,
        account: AccountBaseClaims,
        system_nc: Any,
        *,
        timeout: float = DEFAULT_CLAIMS_UPDATE_TIMEOUT_SECONDS,
    ) -> None:
        if operator_signing_key.role != "operator":
            raise ValueError("OperatorModeRevoker requires an operator key or operator signing key")
        self._operator_signing_key = operator_signing_key
        self._account = account
        self._nc = system_nc
        self._timeout = float(timeout)
        self._lock = asyncio.Lock()
        self._applied_revocations: dict[str, int] = {}

    @property
    def supports_instant_revocation(self) -> bool:
        return True

    @property
    def applied_revocations(self) -> dict[str, int]:
        """Snapshot of the cumulative revocation map last confirmed by the server."""

        return dict(self._applied_revocations)

    async def revoke(
        self,
        nats_public_key: str,
        *,
        revoke_until: int,
        all_revocations: Mapping[str, int] = {},  # noqa: B006
    ) -> RevocationOutcome:
        public_key_bytes(nats_public_key)
        target_ts = int(revoke_until)
        async with self._lock:
            merged = dict(self._applied_revocations)
            for key, timestamp in all_revocations.items():
                public_key_bytes(key)
                merged[key] = max(merged.get(key, 0), int(timestamp))
            merged[nats_public_key] = max(merged.get(nats_public_key, 0), target_ts)

            account_jwt = encode_account_jwt(
                self._operator_signing_key,
                self._account.account_public_key,
                self._account.account_name,
                signing_keys=self._account.signing_keys,
                mappings=self._account.mappings,
                revocations=merged,
                limits=self._account.limits,
                jetstream_tiers=self._account.jetstream_tiers,
                issued_at=int(time.time()),
            )
            try:
                reply = await self._nc.request(
                    CLAIMS_UPDATE_SUBJECT,
                    account_jwt.encode("ascii"),
                    timeout=self._timeout,
                )
            except Exception as exc:
                logger.warning(
                    "NATS account claims update for revocation failed. "
                    "event=nats_revocation_failed nats_key=%s error_type=%s error=%s",
                    nats_public_key,
                    type(exc).__name__,
                    exc,
                )
                return RevocationOutcome(
                    revoked=False,
                    revoked_at=merged[nats_public_key],
                    detail=f"{type(exc).__name__}: {exc}",
                )

            ok, detail = _parse_claims_update_reply(reply.data, self._account.account_public_key)
            if not ok:
                logger.warning(
                    "NATS server rejected account claims update for revocation. "
                    "event=nats_revocation_rejected nats_key=%s detail=%s",
                    nats_public_key,
                    detail,
                )
                return RevocationOutcome(
                    revoked=False,
                    revoked_at=merged[nats_public_key],
                    detail=detail,
                )

            self._applied_revocations = merged
            logger.info(
                "Revoked NATS user key via operator account claims update. "
                "event=nats_revocation_confirmed nats_key=%s revoke_until=%s total_revoked=%s",
                nats_public_key,
                merged[nats_public_key],
                len(merged),
            )
            return RevocationOutcome(
                revoked=True,
                revoked_at=merged[nats_public_key],
                detail=detail,
            )


def _parse_claims_update_reply(payload: bytes, expected_account: str) -> tuple[bool, str]:
    """Check a ``$SYS.REQ.CLAIMS.UPDATE`` JSON response for ``code == 200``."""

    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"invalid claims update reply: {exc}"
    if not isinstance(document, dict):
        return False, "claims update reply was not a JSON object"
    error = document.get("error")
    if error:
        if isinstance(error, dict):
            return False, f"code={error.get('code')} description={error.get('description', '')}"
        return False, str(error)
    data = document.get("data")
    if not isinstance(data, dict):
        return False, "claims update reply missing data object"
    code = data.get("code")
    account = data.get("account")
    message = str(data.get("message", ""))
    if code != 200:
        return False, f"code={code} message={message}"
    if account and account != expected_account:
        return False, f"claims update confirmed unexpected account {account}"
    return True, message or "jwt updated"

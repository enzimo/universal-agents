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
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol

from custom_transport.protocol1.nats_jwt import NatsSigningKey, encode_account_jwt
from custom_transport.protocol1.nkeys import account_public_key_bytes, public_key_bytes

logger = logging.getLogger(__name__)

CLAIMS_UPDATE_SUBJECT = "$SYS.REQ.CLAIMS.UPDATE"
ACCOUNTZ_SUBJECT = "$SYS.REQ.SERVER.PING.ACCOUNTZ"
DEFAULT_CLAIMS_UPDATE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class RevocationOutcome:
    """Result of asking the NATS server to revoke a user key."""

    revoked: bool
    revoked_at: int
    detail: str = ""
    confirmed_revocations: Mapping[str, int] = field(default_factory=dict)


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
        prune_before: Optional[int] = None,
    ) -> RevocationOutcome:
        """Revoke ``nats_public_key`` through ``revoke_until`` (whole unix seconds).

        ``revoke_until`` is the latest possible expiry (``exp`` or certificate end) of any
        credential issued for ``nats_public_key``. NATS refuses every user JWT for that key
        issued at or before ``revoke_until``. Once wall-clock time passes
        ``revoke_until + clock_skew_seconds``, NATS already rejects every JWT of that key via
        ``exp`` and callers may pass ``prune_before`` to drop older entries from the account
        JWT while keeping the permanent reissue ban in ``IssueLedger``.
        """
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
        prune_before: Optional[int] = None,
    ) -> RevocationOutcome:
        public_key_bytes(nats_public_key)
        return RevocationOutcome(
            revoked=False,
            revoked_at=int(revoke_until),
            detail="instant revocation unavailable; waiting for credential lifetime expiry",
        )


class OperatorModeRevoker:
    """Pushes cumulative operator-signed account JWT updates via ``$SYS.REQ.CLAIMS.UPDATE``
    and confirms activation via ``$SYS.REQ.SERVER.PING.ACCOUNTZ``.

    ``system_nc`` must be a NATS connection authenticated as a user of the server's system
    account (a connection on the installation account gets no responders on
    ``$SYS.REQ.CLAIMS.UPDATE``). Updates are serialized with an ``asyncio.Lock`` so
    concurrent revocations cannot overwrite a newer account JWT with an older one.

    Confirmation via ``ACCOUNTZ`` is required because nats-server's ``DirResolver`` replies
    ``code=200`` to ``$SYS.REQ.CLAIMS.UPDATE`` even when nats-server rejects the account JWT
    during account validation (for example a rotated/mismatched operator signing key) and leaves
    existing connections open (C08).
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
        """Snapshot of the active revocation map last confirmed on the server."""

        return dict(self._applied_revocations)

    async def revoke(
        self,
        nats_public_key: str,
        *,
        revoke_until: int,
        all_revocations: Mapping[str, int] = {},  # noqa: B006
        prune_before: Optional[int] = None,
    ) -> RevocationOutcome:
        """Push an updated account JWT revoking ``nats_public_key`` through ``revoke_until``.

        ``revoke_until`` is the latest possible expiry (``exp`` or certificate end, in whole
        unix seconds) of any credential issued for ``nats_public_key``. When ``prune_before``
        is given, previously applied entries with timestamp strictly below ``prune_before``
        (other than ``nats_public_key`` itself) are dropped from the pushed account JWT and
        from ``applied_revocations`` so the account JWT stays bounded while the caller's
        ``IssueLedger`` retains the permanent ban against reissuing credentials (C09).
        """

        public_key_bytes(nats_public_key)
        target_ts = int(revoke_until)
        cutoff = None if prune_before is None else int(prune_before)
        async with self._lock:
            merged = dict(self._applied_revocations)
            for key, timestamp in all_revocations.items():
                public_key_bytes(key)
                merged[key] = max(merged.get(key, 0), int(timestamp))
            merged[nats_public_key] = max(merged.get(nats_public_key, 0), target_ts)
            if cutoff is not None:
                merged = {
                    key: timestamp
                    for key, timestamp in merged.items()
                    if key == nats_public_key or timestamp >= cutoff
                }

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

            # DirResolver replies code=200 even when nats-server rejects the JWT during account
            # validation (C08). Confirm the live account actually loaded this JWT and revocation.
            try:
                accountz_reply = await self._nc.request(
                    ACCOUNTZ_SUBJECT,
                    json.dumps({"account": self._account.account_public_key}).encode("utf-8"),
                    timeout=self._timeout,
                )
            except Exception as exc:
                logger.warning(
                    "NATS ACCOUNTZ confirmation after revocation update failed. "
                    "event=nats_revocation_unconfirmed nats_key=%s error_type=%s error=%s",
                    nats_public_key,
                    type(exc).__name__,
                    exc,
                )
                return RevocationOutcome(
                    revoked=False,
                    revoked_at=merged[nats_public_key],
                    detail=f"ACCOUNTZ confirmation failed: {type(exc).__name__}: {exc}",
                )

            confirmed, confirm_detail = _parse_accountz_confirmation(
                accountz_reply.data,
                expected_account=self._account.account_public_key,
                expected_jwt=account_jwt,
                nats_public_key=nats_public_key,
                min_timestamp=merged[nats_public_key],
            )
            if not confirmed:
                logger.warning(
                    "NATS server did not activate the pushed revocation account JWT. "
                    "event=nats_revocation_unconfirmed nats_key=%s detail=%s",
                    nats_public_key,
                    confirm_detail,
                )
                return RevocationOutcome(
                    revoked=False,
                    revoked_at=merged[nats_public_key],
                    detail=confirm_detail,
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
                confirmed_revocations=dict(merged),
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


def _parse_revocation_timestamp(value: Any) -> Optional[int]:
    """Parse an ACCOUNTZ ``revoked_user`` timestamp (RFC3339 string or unix seconds)."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return int(datetime.fromisoformat(text).timestamp())
        except ValueError:
            return None
    return None


def _parse_accountz_confirmation(
    payload: bytes,
    *,
    expected_account: str,
    expected_jwt: str,
    nats_public_key: str,
    min_timestamp: int,
) -> tuple[bool, str]:
    """Confirm via ``$SYS.REQ.SERVER.PING.ACCOUNTZ`` that the live account loaded ``expected_jwt``
    and revokes ``nats_public_key`` through at least ``min_timestamp``."""

    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"invalid ACCOUNTZ reply: {exc}"
    if not isinstance(document, dict):
        return False, "ACCOUNTZ reply was not a JSON object"
    error = document.get("error")
    if error:
        if isinstance(error, dict):
            return False, f"ACCOUNTZ error code={error.get('code')} description={error.get('description', '')}"
        return False, f"ACCOUNTZ error: {error}"
    data = document.get("data")
    if not isinstance(data, dict):
        return False, "ACCOUNTZ reply missing data object"
    detail = data.get("account_detail")
    if not isinstance(detail, dict):
        return False, "ACCOUNTZ reply missing account_detail"
    account_name = detail.get("account_name")
    if account_name and account_name != expected_account:
        return False, f"ACCOUNTZ returned unexpected account {account_name}"
    active_jwt = detail.get("jwt")
    if active_jwt != expected_jwt:
        return False, "live account JWT does not match pushed revocation JWT (account validation likely failed)"
    revoked_users = detail.get("revoked_user")
    if not isinstance(revoked_users, dict) or nats_public_key not in revoked_users:
        return False, f"live account revoked_user map does not contain {nats_public_key}"
    observed = _parse_revocation_timestamp(revoked_users[nats_public_key])
    if observed is None or observed < min_timestamp:
        return False, (
            f"live account revocation timestamp for {nats_public_key} ({revoked_users[nats_public_key]!r}) "
            f"is below requested {min_timestamp}"
        )
    return True, "confirmed via ACCOUNTZ"

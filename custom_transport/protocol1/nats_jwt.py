"""NATS JWT v2 (``ed25519-nkey``) encoding, inspection, and credential sizing.

contracts/messages/v1/README.md ("NATS users and the account" and "Credential size"):
every process in operator mode logs in with a NATS user JWT signed by the installation
account's signing key, and the installation authority signs account JWT updates (with
revocations) using its operator signing key. Both team services and the installation
authority compute a host's exact credential byte length through ``user_credential_bytes``
so their size checks use the exact same templates (``roles_permissions``) and JWT encoding.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional, Sequence

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from custom_messages.wire.encoding import b64url, b64url_decode
from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT
from custom_messages.wire.permissions import HostPlacement, roles_permissions
from custom_transport.protocol1 import nkeys
from custom_transport.protocol1.contract_files import max_credential_bytes, role_permissions

NKeyRole = Literal["operator", "account", "user"]

# An account without limits, JetStream included (operator mode enables JetStream per account).
UNLIMITED_ACCOUNT_LIMITS: dict[str, Any] = {
    "subs": -1,
    "data": -1,
    "payload": -1,
    "imports": -1,
    "exports": -1,
    "wildcards": True,
    "conn": -1,
    "leaf": -1,
    "mem_storage": -1,
    "disk_storage": -1,
    "streams": -1,
    "consumer": -1,
}
SYSTEM_ACCOUNT_LIMITS: dict[str, Any] = {
    key: value
    for key, value in UNLIMITED_ACCOUNT_LIMITS.items()
    if not key.endswith(("storage", "streams", "consumer"))
}
_JETSTREAM_LIMITS = ("mem_storage", "disk_storage", "streams", "consumer")
_SECONDS = re.compile(r"(\d+)s")
_NANOSECONDS = 1_000_000_000
# Deterministic 10-digit unix timestamps used only for size estimation when no real
# issue/expiry time is given; all 10-digit timestamps serialize to the same byte length.
_ESTIMATE_ISSUED_AT = 1_999_999_000
_ESTIMATE_EXPIRES_AT = 1_999_999_900

_PUBLIC_ENCODERS: dict[str, Callable[[bytes], str]] = {
    "operator": nkeys.operator_public_key,
    "account": nkeys.account_public_key,
    "user": nkeys.user_public_key,
}
_SEED_ENCODERS: dict[str, Callable[[bytes], str]] = {
    "operator": nkeys.operator_seed,
    "account": nkeys.account_seed,
    "user": nkeys.user_seed,
}


@dataclass(frozen=True)
class NatsSigningKey:
    """An operator, account, or user Ed25519 NKey pair."""

    role: NKeyRole
    private_key: Ed25519PrivateKey = field(repr=False)
    public_key: str

    @classmethod
    def generate(cls, role: NKeyRole) -> NatsSigningKey:
        private_key = Ed25519PrivateKey.generate()
        return cls.from_private_key(role, private_key)

    @classmethod
    def from_private_key(cls, role: NKeyRole, private_key: Ed25519PrivateKey) -> NatsSigningKey:
        if role not in _PUBLIC_ENCODERS:
            raise ValueError(f"unsupported NKey role: {role!r}")
        raw_public = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return cls(role=role, private_key=private_key, public_key=_PUBLIC_ENCODERS[role](raw_public))

    @classmethod
    def from_seed(cls, seed_text: str, *, expected_role: Optional[NKeyRole] = None) -> NatsSigningKey:
        role_str, raw_seed = nkeys.seed_role_and_bytes(seed_text)
        if expected_role is not None and role_str != expected_role:
            raise ValueError(f"expected a NATS {expected_role} seed, got {role_str}")
        private_key = Ed25519PrivateKey.from_private_bytes(raw_seed)
        return cls.from_private_key(role_str, private_key)  # type: ignore[arg-type]

    def seed(self) -> str:
        raw_seed = self.private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        return _SEED_ENCODERS[self.role](raw_seed)

    def sign(self, data: bytes) -> bytes:
        return self.private_key.sign(data)

    def login_options(self, jwt_token: str) -> dict[str, Callable[..., bytes]]:
        """``nats.connect`` options authenticating with this user key and its signed JWT."""

        if self.role != "user":
            raise ValueError("only a user NKey can log in to NATS")
        return {
            "user_jwt_cb": lambda: jwt_token.encode("ascii"),
            "signature_cb": lambda nonce: b64url(self.sign(nonce.encode("ascii"))).encode("ascii"),
        }


# Fixed dummy keys used by user_credential_bytes when measuring fit without real signing keys.
_ESTIMATE_SIGNING_KEY = NatsSigningKey.from_private_key(
    "account", Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
)
_ESTIMATE_ACCOUNT_PUBLIC = nkeys.account_public_key(b"\x02" * 32)
_ESTIMATE_USER_PUBLIC = nkeys.user_public_key(b"\x03" * 32)


def _compact_json(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def encode_jwt(
    issuer: NatsSigningKey,
    subject: str,
    name: str,
    claims: Mapping[str, Any],
    *,
    issued_at: Optional[int] = None,
    expires_at: Optional[int] = None,
) -> str:
    """Encode and sign a NATS JWT v2 token (``ed25519-nkey``)."""

    iat = int(time.time()) if issued_at is None else int(issued_at)
    payload: dict[str, Any] = {
        "iat": iat,
        "iss": issuer.public_key,
        "name": name,
        "sub": subject,
        "nats": dict(claims),
    }
    if expires_at is not None:
        payload["exp"] = int(expires_at)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).digest()
    payload["jti"] = base64.b32encode(digest).decode("ascii").rstrip("=")
    head = b64url(_compact_json({"typ": "JWT", "alg": "ed25519-nkey"}))
    body = b64url(_compact_json(payload))
    signing_input = f"{head}.{body}".encode("ascii")
    return f"{head}.{body}.{b64url(issuer.sign(signing_input))}"


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode the JSON payload of a NATS JWT without verifying its signature."""

    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a three-part NATS JWT")
    payload = json.loads(b64url_decode(parts[1]).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("NATS JWT payload must be a JSON object")
    return payload


def user_claims(
    permissions: Optional[Mapping[str, Any]],
    *,
    issuer_account: Optional[str] = None,
) -> dict[str, Any]:
    """Build NATS user JWT claims from filled permissions (``roles_permissions``); ``None`` for unlimited."""

    claims: dict[str, Any] = {
        "subs": -1,
        "data": -1,
        "payload": -1,
        "type": "user",
        "version": 2,
    }
    if issuer_account is not None:
        nkeys.account_public_key_bytes(issuer_account)
        claims["issuer_account"] = issuer_account
    if permissions is None:
        return claims
    # NATS interprets an empty allow list as unrestricted access; emit an explicit deny-all instead.
    pub_allow = list(permissions["publish"])
    sub_allow = list(permissions["subscribe"])
    claims["pub"] = {"allow": pub_allow} if pub_allow else {"deny": [">"]}
    claims["sub"] = {"allow": sub_allow} if sub_allow else {"deny": [">"]}
    responses = permissions.get("allow_responses")
    if responses is not None:
        match = _SECONDS.fullmatch(str(responses["expires"]))
        if match is None:
            raise ValueError(f"allow_responses expires in whole seconds, not {responses['expires']!r}")
        claims["resp"] = {"max": int(responses["max"]), "ttl": int(match.group(1)) * _NANOSECONDS}
    return claims


def encode_operator_jwt(
    operator_key: NatsSigningKey,
    name: str,
    *,
    system_account: str,
    signing_keys: Sequence[str] = (),
    issued_at: Optional[int] = None,
) -> str:
    """Encode an operator JWT naming the system account and optional operator signing keys."""

    if operator_key.role != "operator":
        raise ValueError("an operator JWT must be signed by an operator NKey")
    nkeys.account_public_key_bytes(system_account)
    for key in signing_keys:
        nkeys.operator_public_key_bytes(key)
    claims: dict[str, Any] = {
        "system_account": system_account,
        "type": "operator",
        "version": 2,
    }
    if signing_keys:
        claims["signing_keys"] = list(signing_keys)
    return encode_jwt(operator_key, operator_key.public_key, name, claims, issued_at=issued_at)


def encode_account_jwt(
    signer: NatsSigningKey,
    account_public_key: str,
    name: str,
    *,
    signing_keys: Sequence[str] = (),
    mappings: Optional[Mapping[str, str]] = None,
    revocations: Optional[Mapping[str, int]] = None,
    limits: Optional[Mapping[str, Any]] = None,
    jetstream_tiers: Optional[Mapping[str, Mapping[str, int]]] = None,
    issued_at: Optional[int] = None,
) -> str:
    """Encode an account JWT signed by an operator key or operator signing key.

    ``mappings`` maps source subjects to already-expanded destination subjects (for example
    from ``custom_messages.wire.partitions.account_mappings``). ``revocations`` maps user
    public keys (``U...``) to whole unix seconds; nats-server closes matching connections
    and refuses user JWTs issued at or before that timestamp.
    """

    if signer.role != "operator":
        raise ValueError("an account JWT must be signed by an operator key or operator signing key")
    nkeys.account_public_key_bytes(account_public_key)
    for key in signing_keys:
        nkeys.account_public_key_bytes(key)

    account_limits: dict[str, Any] = dict(UNLIMITED_ACCOUNT_LIMITS if limits is None else limits)
    if jetstream_tiers is not None:
        account_limits = {key: value for key, value in account_limits.items() if key not in _JETSTREAM_LIMITS}
        account_limits["tiered_limits"] = {
            tier: {**{key: -1 for key in _JETSTREAM_LIMITS}, **values}
            for tier, values in jetstream_tiers.items()
        }

    claims: dict[str, Any] = {
        "limits": account_limits,
        "type": "account",
        "version": 2,
    }
    if signing_keys:
        claims["signing_keys"] = list(signing_keys)
    if mappings:
        claims["mappings"] = {
            source: [{"subject": target, "weight": 100}]
            for source, target in sorted(mappings.items())
        }
    if revocations:
        validated: dict[str, int] = {}
        for user_key, timestamp in sorted(revocations.items()):
            nkeys.public_key_bytes(user_key)
            validated[user_key] = int(timestamp)
        claims["revocations"] = validated

    return encode_jwt(signer, account_public_key, name, claims, issued_at=issued_at)


def encode_user_jwt(
    signing_key: NatsSigningKey,
    account_public_key: str,
    user_public_key: str,
    name: str,
    permissions: Optional[Mapping[str, Any]],
    *,
    issued_at: Optional[int] = None,
    expires_at: Optional[int] = None,
) -> str:
    """Encode a user JWT signed by the account's signing key."""

    if signing_key.role != "account":
        raise ValueError("a user JWT must be signed by an account key or account signing key")
    nkeys.account_public_key_bytes(account_public_key)
    nkeys.public_key_bytes(user_public_key)
    claims = user_claims(
        permissions,
        issuer_account=account_public_key if signing_key.public_key != account_public_key else None,
    )
    return encode_jwt(
        signing_key,
        user_public_key,
        name,
        claims,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def user_credential_bytes(
    roles: Sequence[str],
    placement: HostPlacement,
    *,
    partition_count: int = DEFAULT_PARTITION_COUNT,
    user_public_key: Optional[str] = None,
    account_public_key: Optional[str] = None,
    issued_at: Optional[int] = None,
    expires_at: Optional[int] = None,
) -> int:
    """Exact byte length of the NATS user JWT for ``roles`` and ``placement``.

    Uses the same ``roles_permissions`` templates and ``encode_user_jwt`` encoder as
    credential issuance, so team services (which hold no signing key) and the installation
    authority agree on whether a credential fits ``max_credential_bytes``.
    """

    documents = [role_permissions(role) for role in roles]
    filled = roles_permissions(documents, placement, partition_count=partition_count)
    token = encode_user_jwt(
        _ESTIMATE_SIGNING_KEY,
        account_public_key or _ESTIMATE_ACCOUNT_PUBLIC,
        user_public_key or _ESTIMATE_USER_PUBLIC,
        placement.runtime,
        filled,
        issued_at=_ESTIMATE_ISSUED_AT if issued_at is None else issued_at,
        expires_at=_ESTIMATE_EXPIRES_AT if expires_at is None else expires_at,
    )
    return len(token.encode("ascii"))


def fits_credential_limit(
    roles: Sequence[str],
    placement: HostPlacement,
    *,
    partition_count: int = DEFAULT_PARTITION_COUNT,
    max_bytes: Optional[int] = None,
) -> bool:
    """Whether the filled NATS user JWT for ``roles`` and ``placement`` fits ``max_credential_bytes``."""

    limit = max_credential_bytes() if max_bytes is None else max_bytes
    return user_credential_bytes(roles, placement, partition_count=partition_count) <= limit

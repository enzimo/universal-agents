"""NATS operator mode for live tests: the installation's account and its users as signed JWTs.

contracts/messages/v1/README.md, "NATS users and the account": every process logs in with its
own user JWT, signed by the account's signing key, and the account JWT carries the partition
mappings (``partitions.account_mappings``, ``COUNT`` the installation's count). The launcher
and the installation authority that write these for an installation come with the release
that installs operator mode (13C.10 step 2); until then the live contract tests sign the same
JWTs here, so the grants, the mappings and the credential size are checked on a real server.

The formats are nats-io/nkeys (public keys: a prefix byte and the Ed25519 public key, with a
CRC-16, in unpadded base32) and nats-io/jwt v2 (``alg`` ``ed25519-nkey``; nats-server 2.15
refuses the older ``ed25519-nk``). The wire library's ``cryptography`` package signs them, so
the tests need no nkeys package. nats-py logs in with a JWT through its ``user_jwt_cb`` and
``signature_cb`` options, which sign the server's challenge with the user's private key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# nkeys prefix bytes: the first base32 letter of a public key names its kind (O, A, U).
_PREFIX = {"operator": 14 << 3, "account": 0, "user": 20 << 3}
# An account without limits, JetStream included (operator mode enables JetStream per account).
_UNLIMITED = {"subs": -1, "data": -1, "payload": -1, "imports": -1, "exports": -1, "wildcards": True, "conn": -1,
              "leaf": -1, "mem_storage": -1, "disk_storage": -1, "streams": -1, "consumer": -1}
_SECONDS = re.compile(r"(\d+)s")
_NANOSECONDS = 1_000_000_000


def _crc16(data: bytes) -> int:
    """CRC-16/XMODEM, which nkeys appends to every encoded key."""

    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@dataclass(frozen=True)
class NKey:
    """An operator, account or user key pair."""

    kind: str
    private: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate, repr=False)

    @property
    def public(self) -> str:
        body = bytes([_PREFIX[self.kind]]) + self.private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b32encode(body + _crc16(body).to_bytes(2, "little")).decode().rstrip("=")

    def sign(self, data: bytes) -> bytes:
        return self.private.sign(data)


def encode_jwt(issuer: NKey, subject: str, name: str, claims: Mapping[str, Any]) -> str:
    """A nats-io/jwt v2 token: ``claims`` under ``nats``, signed by ``issuer``."""

    payload: dict[str, Any] = {"iat": int(time.time()), "iss": issuer.public, "name": name, "sub": subject,
                               "nats": dict(claims)}
    payload["jti"] = base64.b32encode(hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).digest()) \
        .decode().rstrip("=")
    head = b64url(json.dumps({"typ": "JWT", "alg": "ed25519-nkey"}, separators=(",", ":")).encode())
    body = b64url(json.dumps(payload, separators=(",", ":")).encode())
    return f"{head}.{body}.{b64url(issuer.sign(f'{head}.{body}'.encode()))}"


def user_claims(permissions: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """A user JWT's claims for a filled permission document (``host_permissions``); None for a user without limits.

    The document's ``publish`` and ``subscribe`` lists become the JWT's allow lists, and
    ``allow_responses`` (one answer per request within its expiry) its ``resp``.
    """

    claims: dict[str, Any] = {"subs": -1, "data": -1, "payload": -1, "type": "user", "version": 2}
    if permissions is None:
        return claims
    claims["pub"] = {"allow": list(permissions["publish"])}
    claims["sub"] = {"allow": list(permissions["subscribe"])}
    responses = permissions.get("allow_responses")
    if responses is not None:
        expires = _SECONDS.fullmatch(responses["expires"])
        if expires is None:
            raise ValueError(f"allow_responses expires in whole seconds, not {responses['expires']!r}")
        claims["resp"] = {"max": responses["max"], "ttl": int(expires.group(1)) * _NANOSECONDS}
    return claims


@dataclass(frozen=True)
class NatsUser:
    """A user's JWT and private key, and the nats-py options that log in with them."""

    name: str
    jwt: str
    key: NKey = field(repr=False)

    def login(self) -> dict[str, Callable[..., bytes]]:
        return {"user_jwt_cb": lambda: self.jwt.encode(),
                "signature_cb": lambda nonce: b64url(self.key.sign(nonce.encode())).encode()}


@dataclass
class OperatorAccount:
    """One installation's account in operator mode, with the partition mappings at ``partition_count``.

    A system account is required for JetStream in operator mode; no test logs in to it.
    """

    mappings: Mapping[str, str]
    partition_count: int
    operator: NKey = field(default_factory=lambda: NKey("operator"))
    system: NKey = field(default_factory=lambda: NKey("account"))
    account: NKey = field(default_factory=lambda: NKey("account"))
    signing: NKey = field(default_factory=lambda: NKey("account"))

    def account_jwt(self) -> str:
        """The account JWT the launcher writes: JetStream, the mappings, and the signing key that issues users."""

        mappings = {source: [{"subject": target.replace("COUNT", str(self.partition_count)), "weight": 100}]
                    for source, target in self.mappings.items()}
        return encode_jwt(self.operator, self.account.public, "installation", {
            "limits": _UNLIMITED, "signing_keys": [self.signing.public], "mappings": mappings,
            "type": "account", "version": 2})

    def user(self, name: str, permissions: Optional[Mapping[str, Any]]) -> NatsUser:
        """A user of the installation's account, issued with the account's signing key."""

        key = NKey("user")
        claims = {**user_claims(permissions), "issuer_account": self.account.public}
        return NatsUser(name, encode_jwt(self.signing, key.public, name, claims), key)

    def server_settings(self, folder: Path, max_credential_bytes: int, jetstream: Mapping[str, Any]) -> dict[str, Any]:
        """nats-server settings trusting this operator, with the account and the system account preloaded.

        ``max_control_line`` leaves room for the largest credential plus the rest of the
        client's CONNECT line (README, "Credential size").
        """

        operator = folder / "operator.jwt"
        operator.write_text(encode_jwt(self.operator, self.operator.public, "operator", {
            "system_account": self.system.public, "type": "operator", "version": 2}))
        system = encode_jwt(self.operator, self.system.public, "system", {
            "limits": {key: value for key, value in _UNLIMITED.items() if not key.endswith(("storage", "streams", "consumer"))},
            "type": "account", "version": 2})
        return {"operator": str(operator), "system_account": self.system.public, "resolver": "MEMORY",
                "resolver_preload": {self.account.public: self.account_jwt(), self.system.public: system},
                "max_control_line": max_credential_bytes + 4096, "jetstream": dict(jetstream)}

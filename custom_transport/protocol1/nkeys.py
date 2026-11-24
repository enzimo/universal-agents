"""NATS keys (NKeys) in their text form, without the ``nkeys`` package.

A host presents its NATS user public key when it joins (contract ``nats_user_key``):
the base32 text of a prefix byte, the 32-byte Ed25519 public key and a CRC-16 of
both, which starts with ``U``. Its seed has two prefix bytes and starts with ``SU``.
Operator keys start with ``O``/``SO`` and account keys start with ``A``/``SA``.
nats-server checks these forms when it loads them, so a malformed key fails at the
server. The encoding follows the NKeys specification (nats-io/nkeys): prefix bytes
are the role's letter number shifted left by three, the CRC is CRC-16/XMODEM, stored
little-endian, and the base32 text has no padding.
"""

from __future__ import annotations

import base64

# Letter numbers in base32: A (account) is 0, O (operator) is 14, S (seed) is 18, U (user) is 20.
_ACCOUNT_PREFIX = 0 << 3
_OPERATOR_PREFIX = 14 << 3
_SEED_PREFIX = 18 << 3
_USER_PREFIX = 20 << 3
_KEY_BYTES = 32
_ROLE_PREFIXES: dict[str, int] = {
    "operator": _OPERATOR_PREFIX,
    "account": _ACCOUNT_PREFIX,
    "user": _USER_PREFIX,
}
_PREFIX_TO_ROLE: dict[int, str] = {prefix: role for role, prefix in _ROLE_PREFIXES.items()}


def _crc16(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _encode(raw: bytes) -> str:
    return base64.b32encode(raw + _crc16(raw).to_bytes(2, "little")).decode("ascii").rstrip("=")


def _decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 8)
    try:
        data = base64.b32decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise ValueError("not an NKey") from error
    raw, checksum = data[:-2], data[-2:]
    if len(data) < 3 or _crc16(raw).to_bytes(2, "little") != checksum:
        raise ValueError("an NKey whose checksum does not match")
    return raw


def _public_key(prefix: int, public_key: bytes) -> str:
    if len(public_key) != _KEY_BYTES:
        raise ValueError("an Ed25519 public key has 32 bytes")
    return _encode(bytes([prefix]) + public_key)


def _seed(prefix: int, seed: bytes) -> str:
    if len(seed) != _KEY_BYTES:
        raise ValueError("an Ed25519 seed has 32 bytes")
    first = _SEED_PREFIX | (prefix >> 5)
    second = (prefix & 31) << 3
    return _encode(bytes([first, second]) + seed)


def _decode_seed(text: str, expected_prefix: int, label: str) -> bytes:
    raw = _decode(text)
    if len(raw) != 2 + _KEY_BYTES or raw[0] & 0xF8 != _SEED_PREFIX:
        raise ValueError("not an NKey seed")
    role = ((raw[0] & 0x07) << 5) | ((raw[1] & 0xF8) >> 3)
    if role != expected_prefix:
        raise ValueError(f"not a NATS {label} seed")
    return raw[2:]


def _decode_public_key(text: str, expected_prefix: int, label: str) -> bytes:
    raw = _decode(text)
    if len(raw) != 1 + _KEY_BYTES or raw[0] != expected_prefix:
        raise ValueError(f"not a NATS {label} public key")
    return raw[1:]


def user_public_key(public_key: bytes) -> str:
    """The ``U...`` text of a NATS user's Ed25519 public key."""

    return _public_key(_USER_PREFIX, public_key)


def user_seed(seed: bytes) -> str:
    """The ``SU...`` text of a NATS user's 32-byte Ed25519 seed."""

    return _seed(_USER_PREFIX, seed)


def seed_bytes(text: str) -> bytes:
    """The 32-byte seed of a ``SU...`` text; refuses another role or a damaged text."""

    return _decode_seed(text, _USER_PREFIX, "user")


def public_key_bytes(text: str) -> bytes:
    """The 32-byte public key of a ``U...`` text; refuses another role or a damaged text."""

    return _decode_public_key(text, _USER_PREFIX, "user")


def operator_public_key(public_key: bytes) -> str:
    """The ``O...`` text of a NATS operator's Ed25519 public key."""

    return _public_key(_OPERATOR_PREFIX, public_key)


def operator_seed(seed: bytes) -> str:
    """The ``SO...`` text of a NATS operator's 32-byte Ed25519 seed."""

    return _seed(_OPERATOR_PREFIX, seed)


def operator_seed_bytes(text: str) -> bytes:
    """The 32-byte seed of a ``SO...`` text; refuses another role or a damaged text."""

    return _decode_seed(text, _OPERATOR_PREFIX, "operator")


def operator_public_key_bytes(text: str) -> bytes:
    """The 32-byte public key of an ``O...`` text; refuses another role or a damaged text."""

    return _decode_public_key(text, _OPERATOR_PREFIX, "operator")


def account_public_key(public_key: bytes) -> str:
    """The ``A...`` text of a NATS account's Ed25519 public key."""

    return _public_key(_ACCOUNT_PREFIX, public_key)


def account_seed(seed: bytes) -> str:
    """The ``SA...`` text of a NATS account's 32-byte Ed25519 seed."""

    return _seed(_ACCOUNT_PREFIX, seed)


def account_seed_bytes(text: str) -> bytes:
    """The 32-byte seed of a ``SA...`` text; refuses another role or a damaged text."""

    return _decode_seed(text, _ACCOUNT_PREFIX, "account")


def account_public_key_bytes(text: str) -> bytes:
    """The 32-byte public key of an ``A...`` text; refuses another role or a damaged text."""

    return _decode_public_key(text, _ACCOUNT_PREFIX, "account")


def seed_role_and_bytes(text: str) -> tuple[str, bytes]:
    """Decode any operator, account, or user NKey seed to ``(role, 32-byte seed)``."""

    raw = _decode(text)
    if len(raw) != 2 + _KEY_BYTES or raw[0] & 0xF8 != _SEED_PREFIX:
        raise ValueError("not an NKey seed")
    prefix = ((raw[0] & 0x07) << 5) | ((raw[1] & 0xF8) >> 3)
    role = _PREFIX_TO_ROLE.get(prefix)
    if role is None:
        raise ValueError("unsupported NKey seed role")
    return role, raw[2:]


def public_key_role_and_bytes(text: str) -> tuple[str, bytes]:
    """Decode any operator, account, or user NKey public key to ``(role, 32-byte key)``."""

    raw = _decode(text)
    if len(raw) != 1 + _KEY_BYTES:
        raise ValueError("not an NKey public key")
    role = _PREFIX_TO_ROLE.get(raw[0])
    if role is None:
        raise ValueError("unsupported NKey public key role")
    return role, raw[1:]

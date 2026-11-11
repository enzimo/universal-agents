"""NATS user keys (NKeys) in their text form, without the ``nkeys`` package.

A host presents its NATS user public key when it joins (contract ``nats_user_key``):
the base32 text of a prefix byte, the 32-byte Ed25519 public key and a CRC-16 of
both, which starts with ``U``. Its seed has two prefix bytes and starts with ``SU``.
nats-server checks both forms when it loads them, so a malformed key fails at the
server. The encoding follows the NKeys specification (nats-io/nkeys): prefix bytes
are the role's letter number shifted left by three, the CRC is CRC-16/XMODEM, stored
little-endian, and the base32 text has no padding.
"""

from __future__ import annotations

import base64

# Letter numbers in base32: S (seed) is 18, U (user) is 20.
_SEED_PREFIX = 18 << 3
_USER_PREFIX = 20 << 3
_KEY_BYTES = 32


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


def user_public_key(public_key: bytes) -> str:
    """The ``U...`` text of a NATS user's Ed25519 public key."""

    if len(public_key) != _KEY_BYTES:
        raise ValueError("an Ed25519 public key has 32 bytes")
    return _encode(bytes([_USER_PREFIX]) + public_key)


def user_seed(seed: bytes) -> str:
    """The ``SU...`` text of a NATS user's 32-byte Ed25519 seed."""

    if len(seed) != _KEY_BYTES:
        raise ValueError("an Ed25519 seed has 32 bytes")
    first = _SEED_PREFIX | (_USER_PREFIX >> 5)
    second = (_USER_PREFIX & 31) << 3
    return _encode(bytes([first, second]) + seed)


def seed_bytes(text: str) -> bytes:
    """The 32-byte seed of a ``SU...`` text; refuses another role or a damaged text."""

    raw = _decode(text)
    if len(raw) != 2 + _KEY_BYTES or raw[0] & 0xF8 != _SEED_PREFIX:
        raise ValueError("not an NKey seed")
    role = ((raw[0] & 0x07) << 5) | ((raw[1] & 0xF8) >> 3)
    if role != _USER_PREFIX:
        raise ValueError("not a NATS user seed")
    return raw[2:]


def public_key_bytes(text: str) -> bytes:
    """The 32-byte public key of a ``U...`` text; refuses another role or a damaged text."""

    raw = _decode(text)
    if len(raw) != 1 + _KEY_BYTES or raw[0] != _USER_PREFIX:
        raise ValueError("not a NATS user public key")
    return raw[1:]

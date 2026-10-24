"""Byte encodings shared by frames, authority tokens and state records (contracts/messages/v1)."""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from custom_messages.wire.canonical import canonical_bytes

_UNPADDED_BASE64URL = re.compile(r"[A-Za-z0-9_-]*")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Decode unpadded base64url, refusing every other spelling of the same bytes.

    Python's decoder ignores the unused low bits of the last character, which gave
    each signature 16 accepted spellings; other common decoders refuse them. Only
    the spelling that encoding the bytes produces is accepted.
    """

    if not isinstance(text, str) or not _UNPADDED_BASE64URL.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not unpadded base64url")
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64url(data) != text:
        raise ValueError("not the canonical base64url spelling of its bytes")
    return data


def body_digest(body: Mapping[str, Any]) -> str:
    """SHA-256 of the RFC 8785 canonical JSON of a body, in unpadded base64url."""

    return b64url(hashlib.sha256(canonical_bytes(dict(body))).digest())


def signed_bytes(domain: str, document: Mapping[str, Any]) -> bytes:
    """What a signature covers: the domain, a newline and the canonical document without its signature."""

    unsigned = {key: value for key, value in document.items() if key != "signature"}
    return domain.encode("ascii") + b"\n" + canonical_bytes(unsigned)


def format_timestamp(moment: datetime) -> str:
    """UTC with exactly three fractional digits, as the protocol requires."""

    if moment.tzinfo is None:
        raise ValueError("a protocol time must name its time zone")
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"

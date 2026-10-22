"""Canonical JSON for protocol 1 (contracts/messages/v1/README.md).

Frames are signed over the RFC 8785 canonical form of the JSON as received. Before
that form is computed, input is held to the protocol's stricter rules: one JSON
object per document, no repeated keys, and integers only, within the range that
the canonical form represents exactly (±(2^53 - 1)).
"""

from __future__ import annotations

import json
from typing import Any

import rfc8785

MAX_SAFE_INTEGER = 2**53 - 1


class CanonicalJsonError(ValueError):
    """The document breaks a canonical-form rule; the frame is refused as ``malformed``."""


def _object_without_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise CanonicalJsonError(f"repeated key {key!r}")
        document[key] = value
    return document


def _refuse_float(text: str) -> float:
    raise CanonicalJsonError(f"non-integer number {text}")


def _refuse_constant(text: str) -> float:
    raise CanonicalJsonError(f"non-JSON number {text}")


def check_values(value: Any, *, at: str = "") -> None:
    """Refuse floats and integers outside the safe range anywhere in ``value``."""

    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise CanonicalJsonError(f"integer outside ±(2^53 - 1) at {at or '/'}")
        return
    if isinstance(value, float):
        raise CanonicalJsonError(f"non-integer number at {at or '/'}")
    if isinstance(value, list):
        for index, item in enumerate(value):
            check_values(item, at=f"{at}/{index}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJsonError(f"non-string key at {at or '/'}")
            check_values(item, at=f"{at}/{key}")
        return
    raise CanonicalJsonError(f"unsupported value of type {type(value).__name__} at {at or '/'}")


def parse(document: bytes | str) -> dict[str, Any]:
    """Parse one protocol document, refusing repeated keys, non-integers and non-objects."""

    try:
        value = json.loads(
            document,
            object_pairs_hook=_object_without_repeated_keys,
            parse_float=_refuse_float,
            parse_constant=_refuse_constant,
        )
    except CanonicalJsonError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CanonicalJsonError(f"not JSON: {error}") from error
    if not isinstance(value, dict):
        raise CanonicalJsonError("a protocol document is one JSON object")
    check_values(value)
    return value


def canonical_bytes(value: Any) -> bytes:
    """The RFC 8785 canonical form of ``value``."""

    check_values(value)
    return rfc8785.dumps(value)

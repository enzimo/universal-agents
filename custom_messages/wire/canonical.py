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

from custom_messages.wire.kinds import MAX_NESTING_DEPTH

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


def _safe_integer(text: str) -> int:
    # Refuse before Python's configurable integer-digit limit can raise its own
    # exception. Every permitted integer has at most sixteen decimal digits.
    if len(text.lstrip("-")) > len(str(MAX_SAFE_INTEGER)):
        raise CanonicalJsonError("integer outside ±(2^53 - 1)")
    return int(text)


def _check_nesting(document: str) -> None:
    """Bound containers before the JSON decoder recurses; brackets in strings do not count."""

    depth = 0
    in_string = escaped = False
    for character in document:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_NESTING_DEPTH:
                raise CanonicalJsonError(f"nesting exceeds {MAX_NESTING_DEPTH} containers")
        elif character in "]}":
            depth -= 1


def check_values(value: Any, *, at: str = "", depth: int = 0) -> None:
    """Refuse floats and integers outside the safe range anywhere in ``value``."""

    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise CanonicalJsonError(f"invalid Unicode at {at or '/'}") from error
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise CanonicalJsonError(f"integer outside ±(2^53 - 1) at {at or '/'}")
        return
    if isinstance(value, float):
        raise CanonicalJsonError(f"non-integer number at {at or '/'}")
    if isinstance(value, list):
        if depth >= MAX_NESTING_DEPTH:
            raise CanonicalJsonError(f"nesting exceeds {MAX_NESTING_DEPTH} containers")
        for index, item in enumerate(value):
            check_values(item, at=f"{at}/{index}", depth=depth + 1)
        return
    if isinstance(value, dict):
        if depth >= MAX_NESTING_DEPTH:
            raise CanonicalJsonError(f"nesting exceeds {MAX_NESTING_DEPTH} containers")
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJsonError(f"non-string key at {at or '/'}")
            check_values(key, at=at)
            check_values(item, at=f"{at}/{key}", depth=depth + 1)
        return
    raise CanonicalJsonError(f"unsupported value of type {type(value).__name__} at {at or '/'}")


def parse(document: bytes | str) -> dict[str, Any]:
    """Parse one protocol document, refusing repeated keys, non-integers and non-objects."""

    try:
        text = document.decode("utf-8") if isinstance(document, bytes) else document
        _check_nesting(text)
        value = json.loads(
            text,
            object_pairs_hook=_object_without_repeated_keys,
            parse_float=_refuse_float,
            parse_constant=_refuse_constant,
            parse_int=_safe_integer,
        )
    except CanonicalJsonError:
        raise
    except (ValueError, RecursionError) as error:
        raise CanonicalJsonError(f"not JSON: {error}") from error
    if not isinstance(value, dict):
        raise CanonicalJsonError("a protocol document is one JSON object")
    check_values(value)
    return value


def canonical_bytes(value: Any) -> bytes:
    """The RFC 8785 canonical form of ``value``."""

    check_values(value)
    return rfc8785.dumps(value)

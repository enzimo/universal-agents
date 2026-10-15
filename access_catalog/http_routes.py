"""Classify requests to web API tools (``http:<name>``) by access level.

The level comes from a named route when one matches, otherwise from the method:
``GET``/``HEAD``/``OPTIONS`` read, ``POST``/``PUT``/``PATCH`` write, and
everything else (``DELETE``, unknown methods) full. Routes match whole path
segments; ``{name}`` matches exactly one non-empty segment.

A path that is not in canonical form (dot segments, empty segments, encoded
slashes or dots, backslashes) always needs ``full``: otherwise a traversal such
as ``/api/public/../admin`` could make a restricted route look like an
unmatched request with a lower default level.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from custom_messages.contracts.access_v2 import (
    AccessLevel,
    ClassificationSource,
    HttpRouteV1,
    OperationResolutionV1,
    ToolDefinitionV1,
)


METHOD_DEFAULT_LEVELS: dict[str, AccessLevel] = {
    "GET": AccessLevel.READ,
    "HEAD": AccessLevel.READ,
    "OPTIONS": AccessLevel.READ,
    "POST": AccessLevel.WRITE,
    "PUT": AccessLevel.WRITE,
    "PATCH": AccessLevel.WRITE,
    "DELETE": AccessLevel.FULL,
}
_PLACEHOLDER = re.compile(r"^\{[A-Za-z_][A-Za-z0-9_]*\}$")
_AMBIGUOUS_ESCAPES = ("%2f", "%5c", "%2e", "%00")


def canonical_path_segments(path: str) -> tuple[str, ...] | None:
    """Segments of an absolute, canonical path; ``None`` if it is ambiguous."""

    if not path.startswith("/") or "\\" in path:
        return None
    lowered = path.lower()
    if any(escape in lowered for escape in _AMBIGUOUS_ESCAPES):
        return None
    segments = path[1:].split("/")
    if segments == [""]:
        return ()
    if segments[-1] == "":
        segments = segments[:-1]  # one trailing slash is harmless
    if any(segment in {"", ".", ".."} for segment in segments):
        return None
    return tuple(segments)


def _route_matches(route: HttpRouteV1, method: str, segments: tuple[str, ...]) -> bool:
    if route.method != method:
        return False
    pattern = tuple(route.path[1:].rstrip("/").split("/")) if route.path != "/" else ()
    if len(pattern) != len(segments):
        return False
    return all(
        _PLACEHOLDER.fullmatch(expected) or expected == actual
        for expected, actual in zip(pattern, segments)
    )


def classify_http_request(tool: ToolDefinitionV1, method: str, url_or_path: str) -> OperationResolutionV1:
    """The operation and level one request to ``tool`` needs."""

    normalized_method = str(method or "").strip().upper()
    path = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path.split("?", 1)[0]
    segments = canonical_path_segments(path or "/")
    if segments is None:
        return OperationResolutionV1(
            tool_id=tool.tool_id,
            operation=f"{normalized_method.lower() or 'request'}:unrecognized_path",
            level=AccessLevel.FULL,
            source=ClassificationSource.UNREVIEWED,
            detail="The request path is not in canonical form.",
        )
    for route in tool.routes:
        if _route_matches(route, normalized_method, segments):
            return OperationResolutionV1(
                tool_id=tool.tool_id,
                operation=f"route:{route.name}",
                level=route.level,
                source=ClassificationSource.TRUSTED,
            )
    default = METHOD_DEFAULT_LEVELS.get(normalized_method)
    if default is None:
        return OperationResolutionV1(
            tool_id=tool.tool_id,
            operation=f"{normalized_method.lower() or 'request'}",
            level=AccessLevel.FULL,
            source=ClassificationSource.UNREVIEWED,
            detail="Unrecognized HTTP method.",
        )
    return OperationResolutionV1(
        tool_id=tool.tool_id,
        operation=normalized_method.lower(),
        level=default,
        source=ClassificationSource.TRUSTED,
    )


__all__ = ["METHOD_DEFAULT_LEVELS", "canonical_path_segments", "classify_http_request"]

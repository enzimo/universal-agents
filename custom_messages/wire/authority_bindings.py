"""Shared format constants and helpers for protocol 1 root-task and schedule-permit tokens.

Kept lightweight (no ``custom_messages.wire.models`` import) so both ``custom_transport``
and ``access_control`` can share token-binding rules without pulling generated wire models
into CLI startup or introducing a dependency from ``custom_transport`` to ``access_control``.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, Optional

ROOT_TASK_BINDING = "root_task"
GATEWAY_ISSUER_ROLE = "gateway"
SCHEDULER_ISSUER_ROLE = "scheduler"
# Binding id prefixes for scheduler-signed schedule permits (C01). Kept distinct from real
# root-task ids so bare permits are always refused when presented as task tokens.
SCHEDULE_PERMIT_PREFIX = "schedule_permit:"
SCHEDULE_OWNER_PERMIT_PREFIX = "schedule_owner_permit:"
# Request id prefix required on ScheduleRunIdentityV1 wrappers so permit runs can never
# collide with or adopt gateway message_context_* or task_context_* request ids and grants.
SCHEDULE_RUN_CONTEXT_PREFIX = "schedule_run_"


def schedule_permit_binding_id(schedule_id: str, *, owner_permit: bool = False) -> str:
    """The ``bound_to.id`` / ``bound_to.request_id`` carried by a schedule's permit token."""

    prefix = SCHEDULE_OWNER_PERMIT_PREFIX if owner_permit else SCHEDULE_PERMIT_PREFIX
    return f"{prefix}{schedule_id}"


def is_schedule_permit_binding(bound_id: str) -> bool:
    """Whether ``bound_id`` identifies a per-schedule permit rather than a single root task."""

    return bound_id.startswith((SCHEDULE_PERMIT_PREFIX, SCHEDULE_OWNER_PERMIT_PREFIX))


def extract_token_issuer(raw: Any) -> Optional[tuple[str, str, int]]:
    """Return ``(installation, runtime, membership_epoch)`` from a token or permit wrapper, or ``None``.

    Accepts a protocol 1 ``AuthorityToken``, a raw token mapping, a ``ScheduleRunIdentityV1``
    wrapper (duck-typed via ``.permit``), or a raw mapping containing ``"permit"``.
    """

    if raw is None:
        return None
    # Unwrap ScheduleRunIdentityV1 or dict wrapper carrying a permit token.
    if isinstance(raw, Mapping) and "permit" in raw and "issuer" not in raw:
        raw = raw.get("permit")
    elif not isinstance(raw, Mapping) and hasattr(raw, "permit") and not hasattr(raw, "issuer"):
        raw = getattr(raw, "permit", None)
    if raw is None:
        return None

    issuer = raw.get("issuer") if isinstance(raw, Mapping) else getattr(raw, "issuer", None)
    if issuer is None:
        return None
    if isinstance(issuer, Mapping):
        try:
            installation = str(issuer["installation"])
            runtime = str(issuer["runtime"])
            epoch = int(issuer["membership_epoch"])
        except (KeyError, TypeError, ValueError):
            return None
        return (installation, runtime, epoch) if installation and runtime else None
    try:
        installation = str(getattr(issuer, "installation"))
        runtime = str(getattr(issuer, "runtime"))
        epoch = int(getattr(issuer, "membership_epoch"))
    except (AttributeError, TypeError, ValueError):
        return None
    return (installation, runtime, epoch) if installation and runtime else None


def token_issuer_lookup_bytes(raw: Any, expected_installation: str) -> Optional[bytes]:
    """Minimal JSON lookup document for ``MessageHost.fetch_trust``, or ``None`` if not this installation."""

    extracted = extract_token_issuer(raw)
    if extracted is None:
        return None
    installation, runtime, epoch = extracted
    if installation != expected_installation:
        return None
    return json.dumps(
        {
            "issuer": {
                "type": "host",
                "installation": installation,
                "runtime": runtime,
                "membership_epoch": epoch,
            }
        },
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "GATEWAY_ISSUER_ROLE",
    "ROOT_TASK_BINDING",
    "SCHEDULER_ISSUER_ROLE",
    "SCHEDULE_OWNER_PERMIT_PREFIX",
    "SCHEDULE_PERMIT_PREFIX",
    "SCHEDULE_RUN_CONTEXT_PREFIX",
    "extract_token_issuer",
    "is_schedule_permit_binding",
    "schedule_permit_binding_id",
    "token_issuer_lookup_bytes",
]

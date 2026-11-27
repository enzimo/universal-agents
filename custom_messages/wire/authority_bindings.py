"""Shared format constants, lightweight token model, and helpers for protocol 1 authority tokens.

Kept lightweight (no ``custom_messages.wire.models`` import) so ``custom_messages.contracts``,
``custom_transport``, and ``access_control`` can share authority-token validation and binding
rules without pulling the full generated wire models into CLI startup or introducing a
dependency from ``custom_transport`` to ``access_control``.

Note: ``GATEWAY_ISSUER_ROLE``, ``SCHEDULER_ISSUER_ROLE``, ``AUTHORITY_ISSUER_ROLES``, and
``ROOT_TASK_BINDING`` come from the protocol 1 contract (``contracts/messages/v1/README.md``).
The ``SCHEDULE_PERMIT_PREFIX``, ``SCHEDULE_OWNER_PERMIT_PREFIX``, and
``SCHEDULE_RUN_CONTEXT_PREFIX`` strings are application conventions outside the protocol 1
contract used while queued work still travels on the old wrapper (Step 5).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import MISSING, AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from custom_messages.wire.values import validate_timestamp

ROOT_TASK_BINDING = "root_task"
GATEWAY_ISSUER_ROLE = "gateway"
SCHEDULER_ISSUER_ROLE = "scheduler"
# Hosts whose certificates may sign authority tokens (README, "On behalf of a person").
AUTHORITY_ISSUER_ROLES = frozenset({GATEWAY_ISSUER_ROLE, SCHEDULER_ISSUER_ROLE})
# Maximum authority token lifetime from contracts/messages/v1/transport.yaml (7 days, verified in tests).
AUTHORITY_TOKEN_MAX_LIFETIME_SECONDS = 7 * 24 * 60 * 60

# Application conventions outside the protocol 1 contract (C01, C13, F2):
# - Binding id prefixes for scheduler-signed schedule permits, kept distinct from real
#   root-task ids so bare permits are always refused when presented as task tokens.
SCHEDULE_PERMIT_PREFIX = "schedule_permit:"
SCHEDULE_OWNER_PERMIT_PREFIX = "schedule_owner_permit:"
SCHEDULE_UNVOUCHED_PERMIT_PREFIX = "schedule_unvouched_permit:"
# - Request id prefix required on ScheduleRunIdentityV1 wrappers so permit runs can never
#   collide with or adopt gateway message_context_* or task_context_* request ids and grants.
SCHEDULE_RUN_CONTEXT_PREFIX = "schedule_run_"
# - Request id prefix for scheduler-signed runs of an unvouched person's schedule (and
#   converted 0.9 roleless scheduler contexts): receivers always resolve person_roles=().
UNVOUCHED_RUN_CONTEXT_PREFIX = "unvouched_run_"

# Wire field constraints matching contracts/messages/v1/frame.schema.json#/$defs/authority_token
# (verified against custom_messages.wire.models.AuthorityToken in unit tests).
_TokenId = Annotated[str, StringConstraints(pattern="^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
_InstallationId = Annotated[str, StringConstraints(pattern="^[A-Za-z0-9][A-Za-z0-9_-]{0,61}$")]
_TeamId = Annotated[str, StringConstraints(pattern="^[A-Za-z0-9][A-Za-z0-9_-]{0,23}$")]
_RecordId = Annotated[str, StringConstraints(pattern="^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")]
_MessageId = Annotated[
    str,
    StringConstraints(pattern="^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
]
_Timestamp = Annotated[
    Annotated[
        str,
        StringConstraints(
            pattern="^(19[7-9][0-9]|[2-9][0-9]{3})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
            "T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\\.[0-9]{3}Z$"
        ),
    ],
    AfterValidator(validate_timestamp),
]
_Epoch = Annotated[int, Field(ge=0, le=9007199254740991)]
_KeyId = Annotated[str, StringConstraints(pattern="^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
_Ed25519Signature = Annotated[str, StringConstraints(pattern="^[A-Za-z0-9_-]{86}$")]
_PersonRef = Annotated[str, StringConstraints(pattern="^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]


class _StrictTokenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AuthorityTokenIssuerRecord(_StrictTokenModel):
    installation: _InstallationId
    runtime: _TokenId
    membership_epoch: _Epoch
    key_id: _KeyId


class AuthorityTokenAudienceRecord(_StrictTokenModel):
    installation: _InstallationId
    team: _TeamId


class AuthorityTokenBoundToRootTaskRecord(_StrictTokenModel):
    type: Literal["root_task"]
    id: _RecordId
    request_id: _RecordId


class AuthorityTokenBoundToCommandRecord(_StrictTokenModel):
    type: Literal["command"]
    id: _RecordId


class AuthorityTokenBoundToAccessRequestRecord(_StrictTokenModel):
    type: Literal["access_request"]
    id: _RecordId


class AuthorityTokenRecord(_StrictTokenModel):
    """Lightweight Pydantic model with the exact wire shape of ``custom_messages.wire.models.AuthorityToken``.

    Used on old-wrapper contracts (``GatewayContext.task_context``, ``ScheduleRunIdentityV1.permit``,
    ``MCPInvocationRequestV1.task_context``) so importing gateway or CLI modules does not load the
    full generated ``custom_messages.wire.models`` module while still rejecting legacy signed
    contexts and preserving exact protocol 1 JSON serialization.
    """

    token_id: _MessageId
    issuer: AuthorityTokenIssuerRecord
    audience: AuthorityTokenAudienceRecord
    person: Union[_PersonRef, MISSING] = Field(default=MISSING)
    bound_to: Annotated[
        Union[
            AuthorityTokenBoundToRootTaskRecord,
            AuthorityTokenBoundToCommandRecord,
            AuthorityTokenBoundToAccessRequestRecord,
        ],
        Field(discriminator="type"),
    ]
    schedule_id: Union[_RecordId, MISSING] = Field(default=MISSING)
    issued_at: _Timestamp
    expires_at: _Timestamp
    algorithm: Literal["ed25519"]
    signature: _Ed25519Signature

    @model_validator(mode="before")
    @classmethod
    def _coerce_generated_authority_token(cls, value: Any) -> Any:
        if isinstance(value, BaseModel) and not isinstance(value, cls):
            if type(value).__name__ == "AuthorityToken" and type(value).__module__ == "custom_messages.wire.models":
                return value.model_dump(mode="json")
        return value


def schedule_permit_binding_id(
    schedule_id: str,
    *,
    owner_permit: bool = False,
    unvouched: bool = False,
) -> str:
    """The ``bound_to.id`` / ``bound_to.request_id`` carried by a schedule's permit token."""

    if owner_permit and unvouched:
        raise ValueError("An owner schedule permit cannot be unvouched.")
    if owner_permit:
        prefix = SCHEDULE_OWNER_PERMIT_PREFIX
    elif unvouched:
        prefix = SCHEDULE_UNVOUCHED_PERMIT_PREFIX
    else:
        prefix = SCHEDULE_PERMIT_PREFIX
    return f"{prefix}{schedule_id}"


def is_schedule_permit_binding(bound_id: str) -> bool:
    """Whether ``bound_id`` identifies a per-schedule permit rather than a single root task."""

    return bound_id.startswith(
        (SCHEDULE_PERMIT_PREFIX, SCHEDULE_OWNER_PERMIT_PREFIX, SCHEDULE_UNVOUCHED_PERMIT_PREFIX)
    )


def is_unvouched_permit_binding(bound_id: str) -> bool:
    """Whether ``bound_id`` is a standing permit for an unvouched person's schedule (no roles)."""

    return bound_id.startswith(SCHEDULE_UNVOUCHED_PERMIT_PREFIX)


def is_unvouched_authority_binding(bound_id: str, request_id: str) -> bool:
    """Whether a signed token or permit carries the unvouched 'no roles' marker (F2)."""

    return (
        request_id.startswith(UNVOUCHED_RUN_CONTEXT_PREFIX)
        or is_unvouched_permit_binding(bound_id)
        or is_unvouched_permit_binding(request_id)
    )


def _unwrap_token_object(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, Mapping) and "permit" in raw and "issuer" not in raw:
        return raw.get("permit")
    if not isinstance(raw, Mapping) and hasattr(raw, "permit") and not hasattr(raw, "issuer"):
        return getattr(raw, "permit", None)
    return raw


def extract_token_issuer(raw: Any) -> Optional[tuple[str, str, int]]:
    """Return ``(installation, runtime, membership_epoch)`` from a token or permit wrapper, or ``None``.

    Accepts a protocol 1 ``AuthorityToken``, an ``AuthorityTokenRecord``, a raw token mapping,
    a ``ScheduleRunIdentityV1`` wrapper (duck-typed via ``.permit``), or a raw mapping containing ``"permit"``.
    """

    token = _unwrap_token_object(raw)
    if token is None:
        return None

    issuer = token.get("issuer") if isinstance(token, Mapping) else getattr(token, "issuer", None)
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


def extract_token_log_fields(raw: Any) -> tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
    """Return ``(issuer_runtime, membership_epoch, audience_team, request_id)`` for structured logs.

    Intentionally omits ``person`` / ``person_ref`` so refusal logs carry operational identifiers
    without recording personal references (C10).
    """

    token = _unwrap_token_object(raw)
    if token is None:
        return None, None, None, None
    extracted = extract_token_issuer(token)
    runtime = extracted[1] if extracted is not None else None
    epoch = extracted[2] if extracted is not None else None

    audience = token.get("audience") if isinstance(token, Mapping) else getattr(token, "audience", None)
    team: Optional[str] = None
    if isinstance(audience, Mapping):
        raw_team = audience.get("team")
        team = str(raw_team) if raw_team is not None else None
    elif audience is not None:
        raw_team = getattr(audience, "team", None)
        team = str(raw_team) if raw_team is not None else None

    bound_to = token.get("bound_to") if isinstance(token, Mapping) else getattr(token, "bound_to", None)
    request_id: Optional[str] = None
    if isinstance(bound_to, Mapping):
        raw_req = bound_to.get("request_id") or bound_to.get("id")
        request_id = str(raw_req) if raw_req is not None else None
    elif bound_to is not None:
        raw_req = getattr(bound_to, "request_id", None) or getattr(bound_to, "id", None)
        request_id = str(raw_req) if raw_req is not None else None
    return runtime, epoch, team, request_id


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
    "AUTHORITY_ISSUER_ROLES",
    "AUTHORITY_TOKEN_MAX_LIFETIME_SECONDS",
    "AuthorityTokenAudienceRecord",
    "AuthorityTokenBoundToAccessRequestRecord",
    "AuthorityTokenBoundToCommandRecord",
    "AuthorityTokenBoundToRootTaskRecord",
    "AuthorityTokenIssuerRecord",
    "AuthorityTokenRecord",
    "GATEWAY_ISSUER_ROLE",
    "ROOT_TASK_BINDING",
    "SCHEDULER_ISSUER_ROLE",
    "SCHEDULE_OWNER_PERMIT_PREFIX",
    "SCHEDULE_PERMIT_PREFIX",
    "SCHEDULE_RUN_CONTEXT_PREFIX",
    "SCHEDULE_UNVOUCHED_PERMIT_PREFIX",
    "UNVOUCHED_RUN_CONTEXT_PREFIX",
    "extract_token_issuer",
    "extract_token_log_fields",
    "is_schedule_permit_binding",
    "is_unvouched_authority_binding",
    "is_unvouched_permit_binding",
    "schedule_permit_binding_id",
    "token_issuer_lookup_bytes",
]

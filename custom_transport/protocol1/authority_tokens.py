"""Authority tokens: proof that a message or task acts for a person (README "On behalf of a person").

Only gateway and scheduler hosts sign them, with their own certificate's key:

- **Command tokens** (``command_token``): bound to one command id (the body field the
  contract names for the kind, ``command_binding_id``) and valid for at most
  ``COMMAND_TOKEN_MAX_SECONDS``.
- **Root-task tokens** (``root_task_token``): bound to one root task and its request id
  (``bound_to.id`` and ``bound_to.request_id``, carrying today's ``message_context_*``,
  ``task_context_*`` or ``schedule_run_*`` id) and valid for at most
  ``AUTHORITY_TOKEN_MAX_SECONDS``. System work (a scheduler-signed run for no person)
  names its ``schedule_id`` and omits ``person``.
- **Schedule permit tokens** (``schedule_permit_token``): one standing scheduler-signed
  statement per schedule (``bound_to.id = "schedule_permit:<schedule_id>"`` or
  ``"schedule_owner_permit:<schedule_id>"``), wrapped in ``ScheduleRunIdentityV1`` with a
  fresh ``schedule_run_<hex>`` id per run and checked live against the schedule store by
  ``access_control.task_identity.resolve_task_identity``. Bare permit tokens are refused
  anywhere a root-task token is required.

Every person-bearing token names the person only by their opaque reference
(``TeamPersonV1.person_ref``, plan decision 64), never by their id, which can be a name
or a chat handle: anything else is refused before it is signed.

What receivers must do with one: verify the signature, issuer certificate, revocations,
lifetime and binding (``verify_frame`` or ``access_control.authority_verifier``), then
look up the person through the people copy (``AccessRepository.team_person_by_ref``,
which returns people of any standing) and make their own access decision for them,
refusing a person who is not active or lacks the roles the operation needs.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Optional

from custom_messages.contracts.access_v2 import is_person_ref
from custom_messages.wire.authority_bindings import (
    AUTHORITY_ISSUER_ROLES,
    GATEWAY_ISSUER_ROLE,
    ROOT_TASK_BINDING,
    SCHEDULER_ISSUER_ROLE,
    SCHEDULE_OWNER_PERMIT_PREFIX,
    SCHEDULE_PERMIT_PREFIX,
    SCHEDULE_RUN_CONTEXT_PREFIX,
    extract_token_issuer,
    is_schedule_permit_binding,
    schedule_permit_binding_id,
)
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import (
    COMMAND_BINDING,
    sign_authority_token,
)
from custom_messages.wire.kinds import AUTHORITY_TOKEN_MAX_SECONDS, COMMAND_TOKEN_MAX_SECONDS, KINDS
from custom_messages.wire.models import AuthorityToken
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.ids import new_message_id

_BODY_POINTER = "/body/"


def command_binding_id(kind: str, body: Mapping[str, Any]) -> str:
    """The command id a token for ``kind`` must be bound to: the body field the contract names for it.

    Most commands bind ``/body/request_id``; ``tasks.steer`` binds ``/body/steer_id``.
    Raises ``ValueError`` for a kind that takes no command token, or a body without that field.
    """

    spec = KINDS.get(kind)
    binding = next((item for item in spec.authority_binding if item.type == COMMAND_BINDING), None) if spec else None
    if binding is None or binding.at is None or not binding.at.startswith(_BODY_POINTER):
        raise ValueError(f"{kind} takes no command token bound to a field of its body.")
    value: Any = body
    for step in binding.at.removeprefix(_BODY_POINTER).split("/"):
        value = value.get(step) if isinstance(value, Mapping) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{kind} binds its command token to {binding.at}, which the body does not carry.")
    return value


def require_person_ref(person_ref: str) -> None:
    """Refuse anything but an opaque person reference, before an authority token is signed or waited for."""

    if not is_person_ref(person_ref):
        # Not echoed: what was passed may be the person's name or chat handle.
        raise ValueError("An authority token names a person only by their opaque reference (\"p-\" and 22 characters).")


def _resolve_expiry(
    issued: datetime,
    *,
    max_seconds: int,
    lifetime_seconds: int,
    expires_at: Optional[datetime],
) -> datetime:
    """Compute a bounded token expiry strictly after ``issued`` and at most ``max_seconds`` long."""

    cap = issued + timedelta(seconds=max_seconds)
    if expires_at is not None:
        if expires_at <= issued:
            raise ValueError("An authority token must end after it is issued.")
        return min(expires_at, cap)
    if lifetime_seconds <= 0:
        raise ValueError("An authority token lifetime must be positive.")
    return issued + timedelta(seconds=min(lifetime_seconds, max_seconds))


def _check_signing_certificate(
    identity: HostIdentity,
    *,
    team: str,
    require_scheduler: bool,
) -> None:
    """Refuse to sign an authority token when this host's certificate lacks the role or team."""

    certificate = identity.certificate
    if not (certificate.roles & AUTHORITY_ISSUER_ROLES):
        raise ValueError("Only a gateway or scheduler host certificate may sign an authority token.")
    if require_scheduler and SCHEDULER_ISSUER_ROLE not in certificate.roles:
        raise ValueError("Only a scheduler host certificate may sign a schedule or system-work token.")
    if team not in certificate.teams:
        raise ValueError(f"Host {certificate.runtime} certificate does not serve team {team}.")


def _build_and_sign(
    identity: HostIdentity,
    *,
    team: str,
    bound_to: dict[str, Any],
    issued: datetime,
    expires: datetime,
    person_ref: Optional[str] = None,
    schedule_id: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble, sign and schema-validate one authority token dict."""

    certificate = identity.certificate
    unsigned: dict[str, Any] = {
        "token_id": new_message_id(int(issued.timestamp() * 1000)),
        "issuer": {
            "installation": certificate.installation,
            "runtime": certificate.runtime,
            "membership_epoch": certificate.membership_epoch,
            "key_id": identity.keys.key_id,
        },
        "audience": {"installation": certificate.installation, "team": team},
        "bound_to": bound_to,
        "issued_at": format_timestamp(issued),
        "expires_at": format_timestamp(expires),
    }
    if person_ref is not None:
        unsigned["person"] = person_ref
    if schedule_id is not None:
        unsigned["schedule_id"] = schedule_id
    signed = sign_authority_token(unsigned, identity.keys.application_key)
    AuthorityToken.model_validate(signed)
    return signed


def command_token(
    identity: HostIdentity,
    *,
    team: str,
    person_ref: str,
    command_id: str,
    issued: datetime,
    lifetime_seconds: int = COMMAND_TOKEN_MAX_SECONDS,
) -> dict[str, Any]:
    """A token for one person's command, bound to ``command_id``, signed by this gateway or scheduler host."""

    require_person_ref(person_ref)
    _check_signing_certificate(identity, team=team, require_scheduler=False)
    expires = _resolve_expiry(
        issued,
        max_seconds=COMMAND_TOKEN_MAX_SECONDS,
        lifetime_seconds=lifetime_seconds,
        expires_at=None,
    )
    return _build_and_sign(
        identity,
        team=team,
        bound_to={"type": COMMAND_BINDING, "id": command_id},
        issued=issued,
        expires=expires,
        person_ref=person_ref,
    )


def root_task_token(
    identity: HostIdentity,
    *,
    team: str,
    root_task_id: str,
    request_id: str,
    issued: datetime,
    person_ref: Optional[str] = None,
    schedule_id: Optional[str] = None,
    lifetime_seconds: int = AUTHORITY_TOKEN_MAX_SECONDS,
    expires_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """A token for one root task, bound to ``root_task_id`` and ``request_id``.

    Gateway hosts sign person tasks (``person_ref`` set, ``schedule_id`` omitted).
    Scheduler hosts sign scheduled runs (``schedule_id`` set, with ``person_ref`` for an
    owner schedule or omitted for system work). Passing a permit binding prefix here is
    refused; use ``schedule_permit_token`` instead.
    """

    if person_ref is None and schedule_id is None:
        raise ValueError("A root-task token must name a person, or for system work its schedule.")
    if person_ref is not None:
        require_person_ref(person_ref)
    if is_schedule_permit_binding(root_task_id) or is_schedule_permit_binding(request_id):
        raise ValueError("Use schedule_permit_token to sign a schedule permit.")
    _check_signing_certificate(
        identity,
        team=team,
        require_scheduler=(schedule_id is not None or person_ref is None),
    )
    expires = _resolve_expiry(
        issued,
        max_seconds=AUTHORITY_TOKEN_MAX_SECONDS,
        lifetime_seconds=lifetime_seconds,
        expires_at=expires_at,
    )
    return _build_and_sign(
        identity,
        team=team,
        bound_to={"type": ROOT_TASK_BINDING, "id": root_task_id, "request_id": request_id},
        issued=issued,
        expires=expires,
        person_ref=person_ref,
        schedule_id=schedule_id,
    )


def schedule_permit_token(
    identity: HostIdentity,
    *,
    team: str,
    schedule_id: str,
    issued: datetime,
    person_ref: Optional[str] = None,
    owner_permit: bool = False,
    lifetime_seconds: int = AUTHORITY_TOKEN_MAX_SECONDS,
    expires_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """A scheduler-signed standing permit token for one schedule (owner or system work).

    Stored on the schedule row (``run_permit_json``, or ``owner_permit_json`` when
    ``owner_permit=True`` for a system-work schedule with a vouched administrator owner).
    Wrapped in ``ScheduleRunIdentityV1`` with a fresh ``schedule_run_<hex>`` id per run.
    """

    if not schedule_id:
        raise ValueError("A schedule permit token must name its schedule.")
    if owner_permit and person_ref is None:
        raise ValueError("An owner permit token must name the schedule's vouched owner.")
    if person_ref is not None:
        require_person_ref(person_ref)
    _check_signing_certificate(identity, team=team, require_scheduler=True)
    binding_id = schedule_permit_binding_id(schedule_id, owner_permit=owner_permit)
    expires = _resolve_expiry(
        issued,
        max_seconds=AUTHORITY_TOKEN_MAX_SECONDS,
        lifetime_seconds=lifetime_seconds,
        expires_at=expires_at,
    )
    return _build_and_sign(
        identity,
        team=team,
        bound_to={"type": ROOT_TASK_BINDING, "id": binding_id, "request_id": binding_id},
        issued=issued,
        expires=expires,
        person_ref=person_ref,
        schedule_id=schedule_id,
    )


__all__ = [
    "GATEWAY_ISSUER_ROLE",
    "ROOT_TASK_BINDING",
    "SCHEDULER_ISSUER_ROLE",
    "SCHEDULE_OWNER_PERMIT_PREFIX",
    "SCHEDULE_PERMIT_PREFIX",
    "SCHEDULE_RUN_CONTEXT_PREFIX",
    "command_binding_id",
    "command_token",
    "extract_token_issuer",
    "is_schedule_permit_binding",
    "require_person_ref",
    "root_task_token",
    "schedule_permit_binding_id",
    "schedule_permit_token",
]

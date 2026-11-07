"""Authority tokens: proof that a message acts for a person (README "On behalf of a person").

Only gateway and scheduler hosts sign them, with their own certificate's key. A command
token is bound to one command id (the body field the contract names for the kind,
``command_binding_id``) and lasts at most ``command_token_max_seconds``; it names the
person only by their opaque reference (``TeamPersonV1.person_ref``, plan decision 64),
never by their id, which can be a name or a chat handle: anything else is refused before
it is signed.

What receivers must do with one: ``verify_frame`` checks the signature, issuer,
lifetime and binding; the receiver then finds the person through the people copy it
reads (``AccessRepository.team_person_by_ref``, which returns people of any standing)
and makes its own access decision for them, refusing a person who is not active or
lacks the roles the operation needs, as it would if they asked in person.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from custom_messages.contracts.access_v2 import is_person_ref
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import COMMAND_BINDING, sign_authority_token
from custom_messages.wire.kinds import COMMAND_TOKEN_MAX_SECONDS, KINDS
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
    """Refuse anything but an opaque person reference, before a command token is signed or waited for."""

    if not is_person_ref(person_ref):
        # Not echoed: what was passed may be the person's name or chat handle.
        raise ValueError("A command token names a person only by their opaque reference (\"p-\" and 22 characters).")


def command_token(identity: HostIdentity, *, team: str, person_ref: str, command_id: str, issued: datetime,
                  lifetime_seconds: int = COMMAND_TOKEN_MAX_SECONDS) -> dict[str, Any]:
    """A token for one person's command, bound to ``command_id``, signed by this gateway or scheduler host."""

    require_person_ref(person_ref)
    certificate = identity.certificate
    unsigned = {
        "token_id": new_message_id(int(issued.timestamp() * 1000)),
        "issuer": {"installation": certificate.installation, "runtime": certificate.runtime,
                   "membership_epoch": certificate.membership_epoch, "key_id": identity.keys.key_id},
        "audience": {"installation": certificate.installation, "team": team},
        "person": person_ref,
        "bound_to": {"type": COMMAND_BINDING, "id": command_id},
        "issued_at": format_timestamp(issued),
        "expires_at": format_timestamp(issued + timedelta(seconds=min(lifetime_seconds, COMMAND_TOKEN_MAX_SECONDS))),
    }
    return sign_authority_token(unsigned, identity.keys.application_key)

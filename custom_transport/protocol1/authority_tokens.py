"""Authority tokens: proof that a message acts for a person (README "On behalf of a person").

Only gateway and scheduler hosts sign them, with their own certificate's key. A command
token is bound to one command id and lasts at most ``command_token_max_seconds``; it
names the person by their opaque reference (``TeamPersonV1.person_ref``), never by a
name. Receivers check the signature, issuer, lifetime and binding (``verify_frame``)
and then make their own access decision for that person.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import sign_authority_token
from custom_messages.wire.kinds import COMMAND_TOKEN_MAX_SECONDS
from custom_transport.protocol1.identity import HostIdentity
from custom_transport.protocol1.ids import new_message_id


def command_token(identity: HostIdentity, *, team: str, person_ref: str, command_id: str, issued: datetime,
                  lifetime_seconds: int = COMMAND_TOKEN_MAX_SECONDS) -> dict[str, Any]:
    """A token for one person's command, bound to ``command_id``, signed by this gateway or scheduler host."""

    certificate = identity.certificate
    unsigned = {
        "token_id": new_message_id(int(issued.timestamp() * 1000)),
        "issuer": {"installation": certificate.installation, "runtime": certificate.runtime,
                   "membership_epoch": certificate.membership_epoch, "key_id": identity.keys.key_id},
        "audience": {"installation": certificate.installation, "team": team},
        "person": person_ref,
        "bound_to": {"type": "command", "id": command_id},
        "issued_at": format_timestamp(issued),
        "expires_at": format_timestamp(issued + timedelta(seconds=min(lifetime_seconds, COMMAND_TOKEN_MAX_SECONDS))),
    }
    return sign_authority_token(unsigned, identity.keys.application_key)

"""Gateway identity-domain persistence records."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class GatewayUserRecord:
    canonical_user_id: str
    display_name: str
    timezone: str
    default_channel_name: str | None
    default_chat_id: str | None
    default_thread_id: str | None
    metadata_json: str
    status: str
    created_at_utc: str
    updated_at_utc: str
    last_seen_at_utc: str | None

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(json.loads(self.metadata_json or "{}"))


@dataclass(frozen=True)
class GatewayUserAppearanceRecord:
    """One server-owned appearance override for a canonical user."""

    canonical_user_id: str
    appearance_json: str
    created_at_utc: str
    updated_at_utc: str

    @property
    def appearance(self) -> dict[str, Any]:
        return dict(json.loads(self.appearance_json or "{}"))


@dataclass
class GatewayUserChannelBindingRecord:
    binding_id: str
    canonical_user_id: str
    channel_name: str
    external_user_id: str
    chat_scope: str
    status: str
    created_at_utc: str
    updated_at_utc: str
    last_seen_at_utc: str | None
    replaced_at_utc: str | None
    replaced_by_binding_id: str | None
    metadata_json: str

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(json.loads(self.metadata_json or "{}"))


@dataclass
class GatewayUserWebTokenRecord:
    token_id: str
    token_hash: str
    canonical_user_id: str
    label: str
    status: str
    created_at_utc: str
    last_used_at_utc: str | None
    expires_at_utc: str | None
    metadata_json: str

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(json.loads(self.metadata_json or "{}"))


@dataclass
class GatewayUserWebInviteRecord:
    invite_id: str
    invite_hash: str
    canonical_user_id: str
    label: str
    created_at_utc: str
    expires_at_utc: str
    consumed_at_utc: str | None
    created_by_canonical_user_id: str
    metadata_json: str

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(json.loads(self.metadata_json or "{}"))


@dataclass
class GatewayUserTeamMembershipRecord:
    canonical_user_id: str
    team_id: str
    role: str
    status: str
    created_at_utc: str
    updated_at_utc: str


@dataclass(frozen=True)
class GatewayTeamPersonRecord:
    """One person with a membership in a team (any status), and their own status."""

    canonical_user_id: str
    team_id: str
    role: str
    membership_status: str
    person_status: str


@dataclass(frozen=True)
class GatewayUserSignInRevocation:
    """What removing a person ended: Web Chat tokens, unused invites and link codes."""

    web_tokens: int
    web_invites: int
    link_codes: int


@dataclass(frozen=True)
class GatewayPersonRecordsDeleted:
    """What removing a person deleted from the gateway's records (``forget_person_records``).

    ``attachment_paths`` are the chat upload files of the deleted attachment
    rows, for the caller to delete from the uploads folder.
    """

    sessions: int
    transcript_messages: int
    sign_ins: int
    attachment_paths: tuple[str, ...] = ()


class TeamRoleHolderRequired(ValueError):
    """A role change or removal would leave the team with nobody holding one of ``roles``.

    Raised by the identity store, which checks and changes the membership in
    one statement (see ``access_control.people.ROLES_THAT_KEEP_A_HOLDER``).
    """

    def __init__(self, roles: tuple[str, ...]) -> None:
        super().__init__(f"The team must keep someone with the role {' or '.join(roles)}.")
        self.roles = roles


@dataclass
class GatewayUserLinkCodeRecord:
    link_code_hash: str
    canonical_user_id: str
    expires_at_utc: str
    consumed_at_utc: str | None
    created_at_utc: str
    created_by_channel: str | None
    metadata_json: str

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(json.loads(self.metadata_json or "{}"))

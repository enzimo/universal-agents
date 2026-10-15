"""Gateway identity use cases over narrow authorization and repository ports."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from access_control.people import (
    MEMBERSHIP_STATUS_ACTIVE,
    MEMBERSHIP_STATUS_REMOVED,
    PERSON_ROLE_MEMBER,
    PERSON_ROLE_OWNER,
    PERSON_STATUS_ACTIVE,
    PERSON_STATUS_DISABLED,
    REMOVAL_NEEDS_ADMINISTRATOR,
    ROLE_WORDS,
    ROLES_THAT_KEEP_A_HOLDER,
    PersonStanding,
    is_owner,
    parse_person_role,
    person_standing,
    team_model_keys_refusal,
    team_work_owner,
)
from assistant_gateway.application.command_help import (
    USER_LIST_USAGE,
    USER_ROLE_USAGE,
)
from assistant_gateway.application.ports import (
    GatewayClock,
    GatewayIdentityAuthorizer,
    GatewayIdentitySecrets,
    GatewayPeopleDirectory,
)
from assistant_gateway.domain.identity import (
    GatewayUserChannelBindingRecord,
    GatewayUserRecord,
    GatewayUserTeamMembershipRecord,
    GatewayUserWebInviteRecord,
    GatewayUserWebTokenRecord,
    TeamRoleHolderRequired,
)
from assistant_gateway.ports.repositories import (
    IdentityRepository,
    IdentityUnitOfWorkFactory,
)
from time_presentation import format_user_timestamp

_ADMIN_ONLY_INVITE = "Only an admin can create Web Chat invitations."
# Appended to a reply when the access store's copy could not be written
# (assistant_gateway.people_directory_sync); the change itself stands.
SCHEDULER_NOT_TOLD = (
    "Note: the scheduler's copy of this person could not be updated (see the "
    "gateway log), so their scheduled runs use the previous copy until the "
    "gateway restarts."
)
# /user list [filter]: where the person stands in this team.
_LIST_FILTERS = {
    "active": PersonStanding.ACTIVE,
    "disabled": PersonStanding.DISABLED,
    "removed": PersonStanding.REMOVED,
}
_STANDING_WORDS = {
    PersonStanding.ACTIVE: "active",
    PersonStanding.DISABLED: "disabled",
    PersonStanding.REMOVED: "removed from the team",
    None: "not a member of this team",
}
_LIST_SHOWN = 100
# The store returns at most this many people; the list filters them by standing.
_LIST_SCANNED = 500


@dataclass(frozen=True)
class GatewayIdentitySettings:
    """Stable identity policy needed by application use cases."""

    enabled: bool
    allow_newuser_registration: bool
    web_user_tokens_enabled: bool
    web_user_token_ttl_days: int
    web_user_invite_ttl_seconds: int
    link_code_ttl_seconds: int
    default_timezone: str
    team_id: str
    web_chat_url: str


@dataclass(frozen=True)
class ResolvedGatewayUser:
    """Active canonical user and current team memberships."""

    user: GatewayUserRecord
    memberships: tuple[GatewayUserTeamMembershipRecord, ...]
    web_invite_id: str | None = None
    web_invite_label: str | None = None

    def has_role(self, team_id: str, *roles: str) -> bool:
        allowed = {role.strip().lower() for role in roles if role.strip()}
        return any(
            membership.team_id == team_id
            and membership.status == "active"
            and membership.role.strip().lower() in allowed
            for membership in self.memberships
        )


@dataclass(frozen=True)
class PersonChange:
    """What an administration command did to one person, and the reply so far.

    ``person_id`` is set when the command applied to a person, including an
    authorized repeat (then ``standing`` equals ``previous``); it is None when
    the command was refused or changed nobody's standing. The follow-up (what
    happens to the person's work and records) is
    ``assistant_gateway.application.person_lifecycle``.
    """

    text: str
    person_id: str | None = None
    display_name: str = ""
    standing: PersonStanding | None = None
    previous: PersonStanding | None = None
    # How the person was added, for the onboarding report ("telegram identity 123", "a Web Chat invite").
    added_through: str = ""

    @property
    def repeated(self) -> bool:
        return self.person_id is not None and self.standing is self.previous

    @property
    def became_active(self) -> bool:
        return self.standing is PersonStanding.ACTIVE and self.previous is not PersonStanding.ACTIVE


@dataclass(frozen=True)
class IssuedGatewayWebToken:
    """A one-time cleartext token paired with its persisted record."""

    raw_token: str
    record: GatewayUserWebTokenRecord


class GatewayIdentityService:
    """Own canonical-user lookup, administration, tokens, and channel linking."""

    def __init__(
        self,
        *,
        repository: IdentityRepository,
        unit_of_work_factory: IdentityUnitOfWorkFactory,
        authorizer: GatewayIdentityAuthorizer,
        secrets: GatewayIdentitySecrets,
        clock: GatewayClock,
        settings: GatewayIdentitySettings,
        people_directory: GatewayPeopleDirectory,
        ensure_default_identity: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        # Told after every committed change to a person's membership, role or
        # status, so the scheduler sees their current standing.
        self._people_directory = people_directory
        self._unit_of_work_factory = unit_of_work_factory
        self._authorizer = authorizer
        self._secrets = secrets
        self._clock = clock
        self._settings = settings
        self._ensure_default_identity = ensure_default_identity
        self._logger = logger or logging.getLogger(__name__)

    def hash_secret(self, raw_secret: str) -> str:
        """Hash a gateway-owned credential using the configured adapter."""

        return self._secrets.hash_secret(raw_secret)

    def resolve_canonical_user(
        self,
        *,
        channel_name: str | None,
        external_user_id: str | None,
        chat_scope: str = "",
        seed_default_if_missing: bool = False,
    ) -> ResolvedGatewayUser | None:
        if not self._settings.enabled:
            return None
        channel = str(channel_name or "").strip()
        external = str(external_user_id or "").strip()
        if not channel or not external:
            return None
        binding = self._repository.get_active_channel_binding(
            channel_name=channel,
            external_user_id=external,
            chat_scope=chat_scope,
        )
        if binding is None and seed_default_if_missing:
            binding = self._seed_default_binding(
                channel=channel, external=external, chat_scope=chat_scope
            )
        if binding is None:
            return None
        resolved = self.resolve_user_by_id(binding.canonical_user_id)
        if resolved is None:
            return None
        self._repository.update_gateway_user_last_seen(resolved.user.canonical_user_id)
        return resolved

    def _seed_default_binding(
        self,
        *,
        channel: str,
        external: str,
        chat_scope: str,
    ) -> GatewayUserChannelBindingRecord | None:
        """Seed the instance's user (the local console), then look the binding up again."""

        if self._ensure_default_identity is None:
            return None
        self._ensure_default_identity()
        binding = self._repository.get_active_channel_binding(
            channel_name=channel,
            external_user_id=external,
            chat_scope=chat_scope,
        )
        if binding is not None:
            self._people_directory.record(binding.canonical_user_id)
        return binding

    def resolve_user_by_id(
        self,
        canonical_user_id: str,
    ) -> ResolvedGatewayUser | None:
        """The person when they may use this gateway, else None.

        They must be active and hold an active membership in this gateway's
        team: a disabled person, a person removed from the team and a person
        of another team all resolve to None. Every entry point (chat channels,
        Web Chat tokens, invites and sessions, signed task contexts) resolves
        people here.
        """

        user = self._repository.get_gateway_user(canonical_user_id)
        if user is None or user.status != PERSON_STATUS_ACTIVE:
            return None
        memberships = tuple(
            self._repository.list_team_memberships(
                user.canonical_user_id,
                status=MEMBERSHIP_STATUS_ACTIVE,
            )
        )
        if not any(item.team_id == self._settings.team_id for item in memberships):
            return None
        return ResolvedGatewayUser(user=user, memberships=memberships)

    def lookup_bound_user(
        self,
        *,
        channel_name: str | None,
        external_user_id: str | None,
        chat_scope: str = "",
    ) -> tuple[GatewayUserChannelBindingRecord | None, GatewayUserRecord | None]:
        if not self._settings.enabled:
            return None, None
        channel = str(channel_name or "").strip()
        external = str(external_user_id or "").strip()
        if not channel or not external:
            return None, None
        binding = self._repository.get_active_channel_binding(
            channel_name=channel,
            external_user_id=external,
            chat_scope=chat_scope,
        )
        if binding is None:
            return None, None
        return binding, self._repository.get_gateway_user(binding.canonical_user_id)

    def timezone_for_user(
        self,
        canonical_user_id: str | None,
    ) -> str:
        normalized = str(canonical_user_id or "").strip()
        if normalized:
            user = self._repository.get_gateway_user(normalized)
            if user is not None and user.timezone:
                return user.timezone
        return self._settings.default_timezone

    def register_user_for_channel(
        self,
        *,
        actor: ResolvedGatewayUser | None,
        display_name: str | None,
        channel_name: str | None,
        external_user_id: str | None,
        chat_scope: str,
        thread_id: str | None,
        replace: bool = False,
    ) -> PersonChange:
        """Add a person for a chat identity, or add a removed person back.

        A new identity becomes a new person (a member). An identity already
        bound to someone who was removed from the team (or has no membership
        in it) adds that same person back as a member, so their history and
        files are theirs again; ``--replace`` instead binds the identity to a
        new person.
        """

        if not self._settings.enabled or not self._settings.allow_newuser_registration:
            return PersonChange("New user registration is disabled for this gateway.")
        if actor is None or not self._can_create_user(actor):
            return PersonChange("Only an admin can register new users.")
        channel = str(channel_name or "").strip()
        external = str(external_user_id or "").strip()
        if not channel or not external:
            return PersonChange(
                "This channel does not expose a user identity that can be registered."
            )
        if not replace:
            existing = self._repository.get_active_channel_binding(
                channel_name=channel,
                external_user_id=external,
                chat_scope=chat_scope,
            )
            if existing is not None:
                return self._add_back_or_explain(
                    actor=actor, binding=existing, channel=channel
                )
        rendered_name = str(display_name or external or "User").strip()
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            existing = identity.get_active_channel_binding(
                channel_name=channel,
                external_user_id=external,
                chat_scope=chat_scope,
            )
            if existing is not None and not replace:
                return PersonChange(self._already_bound(channel, existing))
            user = identity.create_gateway_user(
                display_name=rendered_name,
                timezone=self._settings.default_timezone,
                metadata={"registered_from_channel": channel},
            )
            identity.create_team_membership(
                canonical_user_id=user.canonical_user_id,
                team_id=self._settings.team_id,
                role=PERSON_ROLE_MEMBER,
                status=MEMBERSHIP_STATUS_ACTIVE,
            )
            if existing is None:
                binding = identity.create_channel_binding(
                    canonical_user_id=user.canonical_user_id,
                    channel_name=channel,
                    external_user_id=external,
                    chat_scope=chat_scope,
                    metadata={"registered_from_thread_id": thread_id},
                )
            else:
                binding = identity.replace_channel_binding(
                    canonical_user_id=user.canonical_user_id,
                    channel_name=channel,
                    external_user_id=external,
                    chat_scope=chat_scope,
                    metadata={
                        "registered_from_thread_id": thread_id,
                        "replace": True,
                    },
                )
            identity.record_gateway_user_event(
                event_type=(
                    "newuser_replace" if existing is not None else "newuser_create"
                ),
                canonical_user_id=user.canonical_user_id,
                payload={
                    "actor_canonical_user_id": actor.user.canonical_user_id,
                    "channel_name": channel,
                    "external_user_id": external,
                    "chat_scope": chat_scope,
                    "binding_id": binding.binding_id,
                },
            )
            unit_of_work.commit()
        written = self._people_directory.record(user.canonical_user_id)
        return PersonChange(
            _with_scheduler_note(
                f"Registered user {user.canonical_user_id} ({user.display_name}).\n"
                f"Bound {channel} identity {external}.",
                written,
            ),
            person_id=user.canonical_user_id,
            display_name=user.display_name,
            standing=PersonStanding.ACTIVE,
            added_through=f"{channel} identity {external}",
        )

    @staticmethod
    def _already_bound(channel: str, binding: GatewayUserChannelBindingRecord) -> str:
        return (
            f"This {channel} identity is already bound to user "
            f"{binding.canonical_user_id}.\n"
            f'Use /user --replace "Name" or /user link <code> '
            "--replace to rebind it."
        )

    def _add_back_or_explain(
        self,
        *,
        actor: ResolvedGatewayUser,
        binding: GatewayUserChannelBindingRecord,
        channel: str,
    ) -> PersonChange:
        """Add the bound person back when they are off the team; otherwise say the identity is taken."""

        person = self._repository.get_gateway_user(binding.canonical_user_id)
        if person is None or not self._off_team(person.canonical_user_id):
            return PersonChange(self._already_bound(channel, binding))
        return self._add_back_committed(actor=actor, person=person, via=channel)

    def _add_back_committed(
        self, *, actor: ResolvedGatewayUser, person: GatewayUserRecord, via: str
    ) -> PersonChange:
        """Add someone who is off the team back, in its own transaction, and copy their standing."""

        previous = self.team_standing(person.canonical_user_id)
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            person = self._add_back(
                unit_of_work.identity, actor=actor, person=person, via=via
            )
            unit_of_work.commit()
        written = self._people_directory.record(person.canonical_user_id)
        return PersonChange(
            _with_scheduler_note(self._added_back_text(person), written),
            person_id=person.canonical_user_id,
            display_name=person.display_name,
            standing=PersonStanding.ACTIVE,
            previous=previous,
            added_through=f"their {via} identity",
        )

    def _add_back(
        self,
        identity: IdentityRepository,
        *,
        actor: ResolvedGatewayUser,
        person: GatewayUserRecord,
        via: str,
    ) -> GatewayUserRecord:
        """Make a person who is off the team an active member again, in the caller's transaction.

        Adding someone back means they can use the team again, so a person
        who was also disabled is enabled. Returns the person as they are now.
        """

        identity.create_team_membership(
            canonical_user_id=person.canonical_user_id,
            team_id=self._settings.team_id,
            role=PERSON_ROLE_MEMBER,
            status=MEMBERSHIP_STATUS_ACTIVE,
        )
        was_disabled = person.status != PERSON_STATUS_ACTIVE
        if was_disabled:
            person = (
                identity.set_gateway_user_status(
                    person.canonical_user_id, PERSON_STATUS_ACTIVE
                )
                or person
            )
        identity.record_gateway_user_event(
            event_type="team_membership_add_back",
            canonical_user_id=person.canonical_user_id,
            payload={
                "actor_canonical_user_id": actor.user.canonical_user_id,
                "team_id": self._settings.team_id,
                "role": PERSON_ROLE_MEMBER,
                "was_disabled": was_disabled,
                "via": via,
            },
        )
        self._logger.info(
            "A person was added back to the team. event=team_member_added_back "
            "actor=%s target=%s team_id=%s via=%s was_disabled=%s",
            actor.user.canonical_user_id,
            person.canonical_user_id,
            self._settings.team_id,
            via,
            was_disabled,
        )
        return person

    @staticmethod
    def _added_back_text(person: GatewayUserRecord) -> str:
        return (
            f"Added {person.display_name} ({person.canonical_user_id}) back to the "
            "team as a member."
        )

    def render_user_status(
        self,
        *,
        channel_name: str | None,
        external_user_id: str | None,
        chat_scope: str,
        resolved_user: ResolvedGatewayUser | None = None,
    ) -> str:
        channel = str(channel_name or "").strip() or "unknown"
        external = str(external_user_id or "").strip() or "-"
        binding, bound_user = self.lookup_bound_user(
            channel_name=channel_name,
            external_user_id=external_user_id,
            chat_scope=chat_scope,
        )
        lines = [
            "Channel identity:",
            f"Channel: {channel}",
            f"External user id: {external}",
            f"Chat scope: {chat_scope or '(global)'}",
        ]
        if resolved_user is not None:
            if (
                bound_user is not None
                and bound_user.canonical_user_id != resolved_user.user.canonical_user_id
            ):
                raise PermissionError(
                    "The channel identity does not match the authenticated user."
                )
            bound_user = resolved_user.user
            if binding is None:
                lines.append("Channel binding: authenticated Web Chat principal")
        if bound_user is None:
            lines.append("No canonical user is registered for this channel identity.")
            return "\n".join(lines)
        memberships = (
            ", ".join(
                f"{item.team_id}:{item.role}:{item.status}"
                for item in self._repository.list_team_memberships(
                    bound_user.canonical_user_id
                )
            )
            or "-"
        )
        lines.extend(
            [
                f"Canonical user: {bound_user.canonical_user_id}",
                f"Display name: {bound_user.display_name}",
                f"Status: {bound_user.status}",
                f"Timezone: {bound_user.timezone}",
                f"Team memberships: {memberships}",
                (
                    "Default notification target: "
                    f"{self._render_default_target(bound_user, empty='none')}"
                ),
            ]
        )
        return "\n".join(lines)

    def render_user_list(
        self,
        *,
        actor: ResolvedGatewayUser | None,
        status: str | None = None,
    ) -> str:
        """List people with where they stand in this team; ``status`` filters by it."""

        if not self._can_list_users(actor):
            return "Only an admin can list users."
        normalized_status = str(status or "").strip().lower()
        if normalized_status and normalized_status not in _LIST_FILTERS:
            return USER_LIST_USAGE
        standings = self._team_standings()
        users = self._repository.list_gateway_users(limit=_LIST_SCANNED)
        if normalized_status:
            wanted = _LIST_FILTERS[normalized_status]
            users = [
                user
                for user in users
                if standings.get(user.canonical_user_id) is wanted
            ]
        heading = "Registered users"
        if normalized_status:
            heading += f" ({normalized_status})"
        lines = [f"{heading}: {len(users)}"]
        timezone_name = (
            actor.user.timezone
            if actor is not None
            else self._settings.default_timezone
        )
        for index, user in enumerate(users[:_LIST_SHOWN], start=1):
            lines.extend(
                self._render_user_entry(
                    index,
                    user,
                    standing=standings.get(user.canonical_user_id),
                    timezone_name=timezone_name,
                )
            )
        if len(users) > _LIST_SHOWN:
            lines.append(f"Showing first {_LIST_SHOWN} users.")
        return "\n".join(lines)

    def _team_standings(self) -> dict[str, PersonStanding | None]:
        """Where everyone with a membership in this team stands (``access_control.people.person_standing``)."""

        return {
            row.canonical_user_id: person_standing(
                membership_status=row.membership_status,
                person_status=row.person_status,
            )
            for row in self._repository.list_team_people(self._settings.team_id)
        }

    def _render_user_entry(
        self,
        index: int,
        user: GatewayUserRecord,
        *,
        standing: PersonStanding | None,
        timezone_name: str,
    ) -> list[str]:
        memberships = (
            ", ".join(
                f"{membership.team_id}:{membership.role}:{membership.status}"
                for membership in self._repository.list_team_memberships(
                    user.canonical_user_id
                )
            )
            or "-"
        )
        bindings = self._render_bindings(
            self._repository.list_channel_bindings_for_user(
                user.canonical_user_id,
                status="active",
            )
        )
        return [
            f"{index}. {user.display_name}",
            f"   canonical_user_id: {user.canonical_user_id}",
            f"   status: {_STANDING_WORDS[standing]}",
            f"   memberships: {memberships}",
            f"   channel_bindings: {bindings}",
            (
                "   default_notification_target: "
                f"{self._render_default_target(user)}"
            ),
            (
                "   last_seen: "
                f"{format_user_timestamp(user.last_seen_at_utc, timezone_name=timezone_name, empty='Never')}"
            ),
        ]

    def set_user_enabled_status(
        self,
        *,
        actor: ResolvedGatewayUser | None,
        target: str,
        enabled: bool,
        channel_name: str | None,
        chat_scope: str,
    ) -> PersonChange:
        """Disable a person (reversible) or enable them again.

        A disabled person keeps their membership and cannot use the team; what
        happens to their work and records follows in ``person_lifecycle``. A
        person removed from the team is added back with the add flow or
        ``/user restore`` instead (``remove_user``). Repeating the command for
        someone already in that state returns the person again, so the
        follow-up runs again.
        """

        if not self._can_create_user(actor):
            return PersonChange("Only an admin can enable or disable users.")
        target_user = self._resolve_admin_target(
            target=target,
            channel_name=channel_name,
            chat_scope=chat_scope,
        )
        if target_user is None:
            return PersonChange(f"User not found: {target}")
        refusal = self._status_change_refusal(actor, target_user, enabled=enabled)
        if refusal:
            return PersonChange(refusal)
        new_status = PERSON_STATUS_ACTIVE if enabled else PERSON_STATUS_DISABLED
        standing = PersonStanding.ACTIVE if enabled else PersonStanding.DISABLED
        name = f"{target_user.display_name} ({target_user.canonical_user_id})"
        if target_user.status == new_status:
            return PersonChange(
                f"{name} is already {new_status}.",
                person_id=target_user.canonical_user_id,
                display_name=target_user.display_name,
                standing=standing,
                previous=standing,
            )
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            updated = identity.set_gateway_user_status(
                target_user.canonical_user_id,
                new_status,
            )
            if updated is None:
                return PersonChange(f"User not found: {target}")
            identity.record_gateway_user_event(
                event_type="newuser_enable" if enabled else "newuser_disable",
                canonical_user_id=updated.canonical_user_id,
                payload={
                    "actor_canonical_user_id": (
                        actor.user.canonical_user_id if actor else None
                    ),
                    "previous_status": target_user.status,
                    "new_status": updated.status,
                },
            )
            unit_of_work.commit()
        written = self._people_directory.record(updated.canonical_user_id)
        return PersonChange(
            _with_scheduler_note(_status_changed_text(updated, enabled=enabled), written),
            person_id=updated.canonical_user_id,
            display_name=updated.display_name,
            standing=standing,
            previous=PersonStanding.DISABLED if enabled else PersonStanding.ACTIVE,
        )

    def _status_change_refusal(
        self,
        actor: ResolvedGatewayUser | None,
        target_user: GatewayUserRecord,
        *,
        enabled: bool,
    ) -> str | None:
        """Why this enable or disable cannot happen, or None."""

        name = f"{target_user.display_name} ({target_user.canonical_user_id})"
        membership = self._membership(target_user.canonical_user_id)
        if membership is not None and membership.status == MEMBERSHIP_STATUS_REMOVED:
            if not enabled:
                return f"{name} was removed from the team, so there is nothing to disable."
            return (
                f"{name} was removed from the team; /user enable is only for "
                f"disabled people. To bring them back with their held data, run /user restore "
                f"{target_user.canonical_user_id}; to add them back, run /user <their chat user id> "
                f'"Name" on their chat app, or /user invite {target_user.canonical_user_id} '
                "for Web Chat."
            )
        if (
            not enabled
            and actor is not None
            and target_user.canonical_user_id == actor.user.canonical_user_id
        ):
            return (
                "Refusing to disable the current admin user from the current channel."
            )
        # Only an owner disables or re-enables an owner (access_control.people).
        return self._authorizer.status_change_refusal(
            actor_roles=self._active_roles(actor),
            target_role=self._team_role(target_user.canonical_user_id),
        )

    def set_user_role(
        self,
        *,
        actor: ResolvedGatewayUser | None,
        target: str,
        role: str,
        channel_name: str | None,
        chat_scope: str,
    ) -> str:
        """Change a person's role in this team (owner, admin or member).

        The authorizer decides who may make the change; the store refuses a
        change that would leave the team without an owner or without an
        administrator. The change is recorded with who made it, and applies at
        the person's next action because roles are read at every decision.
        """

        new_role = parse_person_role(role)
        if new_role is None:
            return USER_ROLE_USAGE
        if actor is None or not self._can_create_user(actor):
            return "Only an owner or an administrator can change someone's role."
        target_user = self._resolve_admin_target(
            target=target,
            channel_name=channel_name,
            chat_scope=chat_scope,
        )
        if target_user is None:
            return f"User not found: {target}"
        team_id = self._settings.team_id
        current_role = self._team_role(target_user.canonical_user_id)
        if current_role is None:
            return f"{target_user.display_name} is not an active member of this team."
        if current_role == new_role:
            return f"{target_user.display_name} is already {ROLE_WORDS[new_role]}."
        refusal = self._authorizer.role_change_refusal(
            actor_roles=self._active_roles(actor),
            current_role=current_role,
            new_role=new_role,
            team_has_owner=any(
                is_owner((member.role,))
                for member in self._repository.list_team_members(team_id)
            ),
        )
        if refusal:
            return refusal
        stored_refusal = self._store_role_change(
            actor=actor, target_user=target_user, current_role=current_role, new_role=new_role
        )
        if stored_refusal:
            return stored_refusal
        written = self._people_directory.record(target_user.canonical_user_id)
        self._logger.info(
            "Team role changed. event=team_role_changed actor=%s target=%s "
            "team_id=%s previous_role=%s new_role=%s",
            actor.user.canonical_user_id,
            target_user.canonical_user_id,
            team_id,
            current_role,
            new_role,
        )
        return _with_scheduler_note(
            f"{target_user.display_name} ({target_user.canonical_user_id}) is now "
            f"{ROLE_WORDS[new_role]} (was {ROLE_WORDS.get(current_role, current_role)}). "
            "It applies from their next action.",
            written,
        )

    def _store_role_change(
        self,
        *,
        actor: ResolvedGatewayUser,
        target_user: GatewayUserRecord,
        current_role: str,
        new_role: str,
    ) -> str | None:
        """Change the role and record who did it in one transaction.

        Returns why the store refused, or None when the role changed.
        """

        team_id = self._settings.team_id
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            try:
                updated = identity.change_team_membership_role(
                    canonical_user_id=target_user.canonical_user_id,
                    team_id=team_id,
                    role=new_role,
                    keep_holders_of=ROLES_THAT_KEEP_A_HOLDER,
                )
            except TeamRoleHolderRequired as exc:
                self._logger.info(
                    "Team role change refused: it would remove the last holder. "
                    "event=team_role_change_refused actor=%s target=%s previous_role=%s "
                    "new_role=%s kept_roles=%s",
                    actor.user.canonical_user_id,
                    target_user.canonical_user_id,
                    current_role,
                    new_role,
                    ",".join(exc.roles),
                )
                return _last_holder_refusal(exc)
            if updated is None:
                return f"{target_user.display_name} is not an active member of this team."
            identity.record_gateway_user_event(
                event_type="team_role_change",
                canonical_user_id=updated.canonical_user_id,
                payload={
                    "actor_canonical_user_id": actor.user.canonical_user_id,
                    "team_id": team_id,
                    "previous_role": current_role,
                    "new_role": new_role,
                },
            )
            unit_of_work.commit()
        return None

    def remove_user(
        self,
        *,
        actor: ResolvedGatewayUser | None,
        target: str,
        channel_name: str | None,
        chat_scope: str,
    ) -> PersonChange:
        """Take a person off this team (``access_control.people.removal_refusal``).

        Their membership is marked removed and kept for history, their Web
        Chat tokens are revoked and unused invites and link codes expire, and
        the access store's copy says they were removed. What happens to their
        work and records follows in ``person_lifecycle``. Repeating the command
        for someone already removed returns the person again, so the follow-up
        runs again. ``/user restore`` or the add flow
        (``register_user_for_channel``, ``create_web_invite``) adds them back.
        """

        if actor is None or not self._can_create_user(actor):
            return PersonChange(REMOVAL_NEEDS_ADMINISTRATOR)
        target_user = self._resolve_admin_target(
            target=target,
            channel_name=channel_name,
            chat_scope=chat_scope,
        )
        if target_user is None:
            return PersonChange(f"User not found: {target}")
        name = f"{target_user.display_name} ({target_user.canonical_user_id})"
        membership = self._membership(target_user.canonical_user_id)
        if membership is None:
            return PersonChange(f"{name} is not a member of this team.")
        refusal = self._authorizer.removal_refusal(
            actor_id=actor.user.canonical_user_id,
            actor_roles=self._active_roles(actor),
            target_id=target_user.canonical_user_id,
            target_role=membership.role,
        )
        if refusal:
            return PersonChange(refusal)
        removed = PersonChange(
            f"{name} was already removed from the team.",
            person_id=target_user.canonical_user_id,
            display_name=target_user.display_name,
            standing=PersonStanding.REMOVED,
            previous=PersonStanding.REMOVED,
        )
        if membership.status == MEMBERSHIP_STATUS_REMOVED:
            return removed
        previous = self.team_standing(target_user.canonical_user_id)
        stored_refusal = self._store_removal(
            actor=actor, target_user=target_user, previous_role=membership.role
        )
        if stored_refusal:
            return PersonChange(stored_refusal)
        written = self._people_directory.record(target_user.canonical_user_id)
        return PersonChange(
            _with_scheduler_note(
                f"Removed {name} from the team. They can no longer use it, and their sign-ins ended.",
                written,
            ),
            person_id=removed.person_id,
            display_name=removed.display_name,
            standing=PersonStanding.REMOVED,
            previous=previous,
        )

    def team_keys_target(
        self,
        *,
        actor: ResolvedGatewayUser | None,
        target: str,
        channel_name: str | None,
        chat_scope: str,
    ) -> tuple[GatewayUserRecord | None, str | None]:
        """The member whose use of the team's model keys the actor may decide, or why not.

        Who may decide is ``access_control.people.team_model_keys_refusal``;
        the setting itself lives in the access store (``access_control.person_settings``).
        """

        refusal = team_model_keys_refusal(actor_roles=self._active_roles(actor))
        if refusal is not None:
            return None, refusal
        person = self._resolve_admin_target(target=target, channel_name=channel_name, chat_scope=chat_scope)
        if person is None:
            return None, f"User not found: {target}"
        if self._off_team(person.canonical_user_id):
            return None, f"{person.display_name} is not a member of this team."
        return person, None

    def restorable_person(
        self,
        *,
        actor: ResolvedGatewayUser | None,
        target: str,
        channel_name: str | None,
        chat_scope: str,
    ) -> GatewayUserRecord | str:
        """The removed person ``/user restore`` may bring back, or why not.

        The same people may restore as may add someone (administrators); only
        someone removed from this team can be restored.
        """

        if actor is None or not self._can_create_user(actor):
            return "Only an owner or an administrator can restore people."
        target_user = self._resolve_admin_target(
            target=target,
            channel_name=channel_name,
            chat_scope=chat_scope,
        )
        if target_user is None:
            return f"User not found: {target}"
        standing = self.team_standing(target_user.canonical_user_id)
        if standing is not PersonStanding.REMOVED:
            name = f"{target_user.display_name} ({target_user.canonical_user_id})"
            return (
                f"{name} was not removed from the team, so there is nothing to restore. "
                "/user restore brings back someone removed with /user remove; use /user enable "
                "for someone disabled."
            )
        return target_user

    def restore_user(
        self, *, actor: ResolvedGatewayUser, person: GatewayUserRecord
    ) -> PersonChange:
        """Add a removed person back as a member (``/user restore``); their held data follows in ``person_lifecycle``."""

        return self._add_back_committed(actor=actor, person=person, via="restore")

    def team_standing(self, canonical_user_id: str) -> PersonStanding | None:
        """Where the person stands in this team now (``access_control.people.person_standing``)."""

        person = self._repository.get_gateway_user(canonical_user_id)
        membership = self._membership(canonical_user_id)
        return person_standing(
            membership_status=membership.status if membership else None,
            person_status=person.status if person else None,
        )

    def team_work_owner(self, preferred: str) -> str:
        """Who the runtime's own team schedules run for (``access_control.people.team_work_owner``).

        ``preferred`` (the instance's first user) while they are active;
        otherwise an active owner or administrator. When nobody qualifies the
        preferred person stays (logged): the schedules then do not run.
        """

        people = (
            (
                row.canonical_user_id,
                person_standing(membership_status=row.membership_status, person_status=row.person_status)
                is PersonStanding.ACTIVE,
                (row.role,),
            )
            for row in self._repository.list_team_people(self._settings.team_id)
        )
        owner = team_work_owner(people, preferred=preferred)
        if owner is None:
            self._logger.error(
                "No active owner or administrator can own the team's schedules. "
                "event=team_work_owner_unavailable preferred=%s team_id=%s",
                preferred,
                self._settings.team_id,
            )
            return preferred
        return owner

    def _store_removal(
        self,
        *,
        actor: ResolvedGatewayUser,
        target_user: GatewayUserRecord,
        previous_role: str,
    ) -> str | None:
        """Remove the membership, end the person's sign-ins and record who did it, in one transaction.

        Returns why the store refused, or None when the person was removed.
        """

        team_id = self._settings.team_id
        person_id = target_user.canonical_user_id
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            try:
                removed = identity.remove_team_membership(
                    canonical_user_id=person_id,
                    team_id=team_id,
                    keep_holders_of=ROLES_THAT_KEEP_A_HOLDER,
                )
            except TeamRoleHolderRequired as exc:
                self._logger.info(
                    "Removal refused: it would remove the last holder. "
                    "event=team_member_removal_refused actor=%s target=%s role=%s kept_roles=%s",
                    actor.user.canonical_user_id,
                    person_id,
                    previous_role,
                    ",".join(exc.roles),
                )
                return _last_holder_refusal(exc)
            if removed is None:
                return f"{target_user.display_name} is not an active member of this team."
            ended = identity.revoke_gateway_user_sign_ins(
                person_id, now_utc=self._clock.now_utc().isoformat()
            )
            identity.record_gateway_user_event(
                event_type="team_membership_remove",
                canonical_user_id=person_id,
                payload={
                    "actor_canonical_user_id": actor.user.canonical_user_id,
                    "team_id": team_id,
                    "previous_role": previous_role,
                    "web_tokens_revoked": ended.web_tokens,
                    "web_invites_expired": ended.web_invites,
                    "link_codes_expired": ended.link_codes,
                },
            )
            unit_of_work.commit()
        self._logger.info(
            "A person was removed from the team. event=team_member_removed actor=%s "
            "target=%s team_id=%s previous_role=%s web_tokens_revoked=%d "
            "web_invites_expired=%d link_codes_expired=%d",
            actor.user.canonical_user_id,
            person_id,
            team_id,
            previous_role,
            ended.web_tokens,
            ended.web_invites,
            ended.link_codes,
        )
        return None

    def create_web_token(
        self,
        *,
        resolved_user: ResolvedGatewayUser | None,
        label: str,
        channel_name: str | None,
    ) -> str:
        if not self._settings.web_user_tokens_enabled:
            return "Web Chat user tokens are disabled for this gateway."
        if resolved_user is None:
            return (
                "Register this channel first with /user before creating a "
                "Web Chat token."
            )
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            issued = self._issue_web_token(
                repository=identity,
                canonical_user_id=resolved_user.user.canonical_user_id,
                label=label,
                metadata={"created_from_channel": channel_name},
            )
            identity.record_gateway_user_event(
                event_type="newuser_token_create",
                canonical_user_id=resolved_user.user.canonical_user_id,
                payload={"token_id": issued.record.token_id, "label": label},
            )
            unit_of_work.commit()
        expiry_line = (
            "\nExpires: "
            f"{format_user_timestamp(issued.record.expires_at_utc, timezone_name=resolved_user.user.timezone)}"
            if issued.record.expires_at_utc
            else ""
        )
        return (
            "Web Chat user token for "
            f"{resolved_user.user.canonical_user_id}:\n{issued.raw_token}"
            f"{expiry_line}\nThis token is shown only once."
        )

    def web_token_expires_at_utc(self) -> str | None:
        if self._settings.web_user_token_ttl_days <= 0:
            return None
        return (
            self._clock.now_utc()
            + timedelta(days=self._settings.web_user_token_ttl_days)
        ).isoformat()

    def issue_web_token(
        self,
        *,
        canonical_user_id: str,
        label: str,
        status: str = "active",
        expires_at_utc: str | None = None,
        metadata: dict | None = None,
    ) -> IssuedGatewayWebToken | None:
        if not self._settings.web_user_tokens_enabled:
            return None
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            issued = self._issue_web_token(
                repository=unit_of_work.identity,
                canonical_user_id=canonical_user_id,
                label=label,
                status=status,
                expires_at_utc=expires_at_utc,
                metadata=metadata,
            )
            unit_of_work.commit()
        return issued

    def _issue_web_token(
        self,
        *,
        repository: IdentityRepository,
        canonical_user_id: str,
        label: str,
        status: str = "active",
        expires_at_utc: str | None = None,
        metadata: dict | None = None,
    ) -> IssuedGatewayWebToken:
        raw_token = self._secrets.new_web_user_token()
        resolved_expiry = expires_at_utc
        if status == "active" and expires_at_utc is None:
            resolved_expiry = self.web_token_expires_at_utc()
        record = repository.create_web_user_token(
            token_hash=self._secrets.hash_secret(raw_token),
            canonical_user_id=canonical_user_id,
            label=label,
            status=status,
            expires_at_utc=resolved_expiry,
            metadata=metadata,
        )
        return IssuedGatewayWebToken(raw_token=raw_token, record=record)

    def issue_enrollment_token(
        self,
        *,
        canonical_user_id: str,
        label: str,
        pending_expires_at_utc: str,
        replacement_token_id: str | None = None,
    ) -> IssuedGatewayWebToken | None:
        if not self.is_web_chat_user_active(canonical_user_id):
            return None
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            issued = self._issue_web_token(
                repository=identity,
                canonical_user_id=canonical_user_id,
                label=(str(label or "Initial browser").strip() or "Initial browser"),
                status="pending",
                expires_at_utc=pending_expires_at_utc,
                metadata={"created_from": "web_invite_enrollment"},
            )
            if replacement_token_id and not identity.revoke_web_user_token(
                replacement_token_id,
                canonical_user_id=canonical_user_id,
                required_status="pending",
            ):
                return None
            identity.record_gateway_user_event(
                event_type="newuser_web_token_pending",
                canonical_user_id=canonical_user_id,
                payload={
                    "token_id": issued.record.token_id,
                    "label": issued.record.label,
                    "replaced_token_id": replacement_token_id,
                },
            )
            unit_of_work.commit()
        return issued

    def activate_enrollment_token(
        self,
        *,
        canonical_user_id: str,
        token_id: str,
    ) -> GatewayUserWebTokenRecord | None:
        if not self._settings.web_user_tokens_enabled:
            return None
        if not self.is_web_chat_user_active(canonical_user_id):
            return None
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            token = identity.activate_web_user_token(
                token_id=token_id,
                canonical_user_id=canonical_user_id,
                expires_at_utc=self.web_token_expires_at_utc(),
                now_utc=self._clock.now_utc().isoformat(),
            )
            if token is None:
                return None
            identity.record_gateway_user_event(
                event_type="newuser_web_token_activate",
                canonical_user_id=canonical_user_id,
                payload={"token_id": token.token_id, "label": token.label},
            )
            unit_of_work.commit()
        return token

    def revoke_pending_enrollment_token(
        self,
        *,
        canonical_user_id: str,
        token_id: str,
    ) -> bool:
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            revoked = identity.revoke_web_user_token(
                token_id,
                canonical_user_id=canonical_user_id,
                required_status="pending",
            )
            if not revoked:
                return False
            identity.record_gateway_user_event(
                event_type="newuser_web_token_pending_revoke",
                canonical_user_id=canonical_user_id,
                payload={"token_id": token_id},
            )
            unit_of_work.commit()
        return True

    def create_web_invite(
        self,
        *,
        actor: ResolvedGatewayUser | None,
        target: str | None,
        display_name: str | None,
        label: str,
        create_new: bool,
        channel_name: str | None,
        chat_scope: str,
    ) -> PersonChange:
        """Create a one-time Web Chat invite for a person, or a new web-only person with one.

        Inviting someone who was removed from the team (or has no membership
        in it) adds them back as a member first, so people who use only Web
        Chat can come back too.
        """

        refusal = self._web_invite_refusal(
            actor, target=target, display_name=display_name, create_new=create_new
        )
        if refusal is not None or actor is None:
            return PersonChange(refusal or _ADMIN_ONLY_INVITE)
        target_user: GatewayUserRecord | None = None
        previous: PersonStanding | None = None
        if not create_new:
            target_user = self._resolve_admin_target(
                target=str(target or ""),
                channel_name=channel_name,
                chat_scope=chat_scope,
            )
            if target_user is None:
                return PersonChange(f"User not found: {target}")
            refusal = self._invite_target_refusal(target_user)
            if refusal is not None:
                return PersonChange(refusal)
            previous = self.team_standing(target_user.canonical_user_id)
        raw_invite = self._secrets.new_web_user_invite()
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            target_user, added_back = self._store_web_invite(
                unit_of_work.identity,
                actor=actor,
                target_user=target_user,
                display_name=str(display_name or "").strip(),
                label=str(label or "Initial browser").strip() or "Initial browser",
                raw_invite=raw_invite,
                channel_name=channel_name,
            )
            unit_of_work.commit()
        written = True
        if create_new or added_back:
            written = self._people_directory.record(target_user.canonical_user_id)
        text = self._web_invite_text(target_user, raw_invite, create_new=create_new)
        if added_back:
            text = f"{self._added_back_text(target_user)}\n{text}"
        joined = create_new or added_back
        return PersonChange(
            _with_scheduler_note(text, written),
            person_id=target_user.canonical_user_id if joined else None,
            display_name=target_user.display_name,
            standing=PersonStanding.ACTIVE if joined else None,
            previous=previous,
            added_through="a one-time Web Chat invite",
        )

    def _web_invite_refusal(
        self,
        actor: ResolvedGatewayUser | None,
        *,
        target: str | None,
        display_name: str | None,
        create_new: bool,
    ) -> str | None:
        """Why no invite can be made at all, before looking at the person; None when it can."""

        if not self._settings.enabled or not self._settings.web_user_tokens_enabled:
            return "Web Chat user authentication is disabled for this gateway."
        if create_new and not self._settings.allow_newuser_registration:
            return "New user registration is disabled for this gateway."
        authorization_target = None if create_new else str(target or "").strip() or None
        if not self._can_create_web_invite(
            actor,
            target_canonical_user_id=authorization_target,
        ):
            return _ADMIN_ONLY_INVITE
        if create_new and not str(display_name or "").strip():
            return (
                'Usage: /user invite --new "Display Name" [--label "Initial browser"]'
            )
        return None

    def _invite_target_refusal(self, target_user: GatewayUserRecord) -> str | None:
        """Why this person cannot be invited, or None (someone off the team is added back)."""

        if self._off_team(target_user.canonical_user_id):
            return None
        if target_user.status != PERSON_STATUS_ACTIVE:
            return f"User {target_user.canonical_user_id} is disabled."
        return None

    def _store_web_invite(
        self,
        identity: IdentityRepository,
        *,
        actor: ResolvedGatewayUser,
        target_user: GatewayUserRecord | None,
        display_name: str,
        label: str,
        raw_invite: str,
        channel_name: str | None,
    ) -> tuple[GatewayUserRecord, bool]:
        """Store the invite (and the new person, or add someone back) in the caller's transaction.

        Returns the invited person, as they are now, and whether they were added back.
        """

        expires_at_utc = (
            self._clock.now_utc()
            + timedelta(seconds=self._settings.web_user_invite_ttl_seconds)
        ).isoformat()
        invite_hash = self._secrets.hash_secret(raw_invite)
        actor_user_id = actor.user.canonical_user_id
        added_back = False
        invite: GatewayUserWebInviteRecord
        if target_user is None:
            target_user, _, invite = identity.create_gateway_user_with_web_invite(
                display_name=display_name,
                timezone=self._settings.default_timezone,
                user_metadata={"registered_from": "web_invite"},
                team_id=self._settings.team_id,
                invite_hash=invite_hash,
                invite_label=label,
                invite_expires_at_utc=expires_at_utc,
                created_by_canonical_user_id=actor_user_id,
                invite_metadata={"created_from_channel": channel_name},
            )
            event_type = "newuser_web_only_invite_create"
        else:
            if self._off_team(target_user.canonical_user_id):
                target_user = self._add_back(
                    identity, actor=actor, person=target_user, via="web_invite"
                )
                added_back = True
            invite = identity.create_web_user_invite(
                invite_hash=invite_hash,
                canonical_user_id=target_user.canonical_user_id,
                label=label,
                expires_at_utc=expires_at_utc,
                created_by_canonical_user_id=actor_user_id,
                metadata={"created_from_channel": channel_name},
            )
            event_type = "newuser_web_invite_create"
        identity.record_gateway_user_event(
            event_type=event_type,
            canonical_user_id=target_user.canonical_user_id,
            payload={
                "actor_canonical_user_id": actor_user_id,
                "invite_id": invite.invite_id,
                "label": label,
                "expires_at_utc": expires_at_utc,
            },
        )
        return target_user, added_back

    def _web_invite_text(
        self, target_user: GatewayUserRecord, raw_invite: str, *, create_new: bool
    ) -> str:
        ttl_seconds = self._settings.web_user_invite_ttl_seconds
        ttl_text = (
            f"{ttl_seconds // 60} minute(s)"
            if ttl_seconds % 60 == 0
            else f"{ttl_seconds} seconds"
        )
        creation_line = "Registered web-only user.\n" if create_new else ""
        return (
            f"{creation_line}Web Chat invitation for "
            f"{target_user.canonical_user_id} ({target_user.display_name}).\n"
            f"Web Chat: {self._settings.web_chat_url}\n"
            f"Invite code: {raw_invite}\n"
            f"Expires in {ttl_text} and can be used once.\n"
            "The invite code is shown only once. Send the URL and code through "
            "a trusted channel.\n"
            "After redeeming it, the user must generate, copy, and confirm a "
            "reusable Web Chat token."
        )

    def consume_web_invite(
        self,
        raw_invite: str,
    ) -> ResolvedGatewayUser | None:
        if not self._settings.web_user_tokens_enabled:
            return None
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            invite = identity.consume_web_user_invite(
                invite_hash=self._secrets.hash_secret(raw_invite),
                team_id=self._settings.team_id,
                now_utc=self._clock.now_utc().isoformat(),
            )
            if invite is None:
                return None
            # Only an active member of this team resolves (resolve_user_by_id).
            resolved = self.resolve_user_by_id(invite.canonical_user_id)
            if resolved is None:
                return None
            identity.update_gateway_user_last_seen(resolved.user.canonical_user_id)
            identity.record_gateway_user_event(
                event_type="newuser_web_invite_consume",
                canonical_user_id=resolved.user.canonical_user_id,
                payload={"invite_id": invite.invite_id, "label": invite.label},
            )
            unit_of_work.commit()
        return ResolvedGatewayUser(
            user=resolved.user,
            memberships=resolved.memberships,
            web_invite_id=invite.invite_id,
            web_invite_label=invite.label,
        )

    def is_web_chat_user_active(self, canonical_user_id: str) -> bool:
        """Whether the person may keep using Web Chat: active and an active member of this team."""

        return self.resolve_user_by_id(canonical_user_id) is not None

    def get_web_chat_appearance(
        self,
        canonical_user_id: str,
    ) -> dict[str, object] | None:
        """Return one canonical user's saved browser appearance."""

        record = self._repository.get_gateway_user_appearance(canonical_user_id)
        return record.appearance if record is not None else None

    def save_web_chat_appearance(
        self,
        *,
        canonical_user_id: str,
        appearance: dict[str, object],
    ) -> dict[str, object]:
        """Persist a validated appearance for one canonical user."""

        record = self._repository.upsert_gateway_user_appearance(
            canonical_user_id=canonical_user_id,
            appearance=appearance,
        )
        return record.appearance

    def reset_web_chat_appearance(self, canonical_user_id: str) -> bool:
        """Remove one canonical user's override and expose the instance baseline."""

        return self._repository.delete_gateway_user_appearance(canonical_user_id)

    def create_link_code(
        self,
        *,
        resolved_user: ResolvedGatewayUser | None,
        channel_name: str | None,
    ) -> str:
        if resolved_user is None:
            return "Register this channel first with /user before creating a link code."
        code = self._secrets.new_link_code()
        expires_at = (
            self._clock.now_utc()
            + timedelta(seconds=self._settings.link_code_ttl_seconds)
        ).isoformat()
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            identity.create_link_code(
                link_code_hash=self._secrets.hash_secret(code),
                canonical_user_id=resolved_user.user.canonical_user_id,
                expires_at_utc=expires_at,
                created_by_channel=channel_name,
            )
            identity.record_gateway_user_event(
                event_type="newuser_link_code_create",
                canonical_user_id=resolved_user.user.canonical_user_id,
                payload={"created_by_channel": channel_name},
            )
            unit_of_work.commit()
        return (
            f"Link code for {resolved_user.user.canonical_user_id}: {code}\n"
            "Expires: "
            f"{format_user_timestamp(expires_at, timezone_name=resolved_user.user.timezone)}"
        )

    def link_code_for_channel(
        self,
        *,
        code: str,
        channel_name: str | None,
        external_user_id: str | None,
        chat_scope: str,
        thread_id: str | None,
        replace: bool = False,
    ) -> str:
        channel = str(channel_name or "").strip()
        external = str(external_user_id or "").strip()
        if not channel or not external:
            return "This channel does not expose a user identity that can be linked."
        with self._unit_of_work_factory.identity_unit_of_work() as unit_of_work:
            identity = unit_of_work.identity
            existing = identity.get_active_channel_binding(
                channel_name=channel,
                external_user_id=external,
                chat_scope=chat_scope,
            )
            if existing is not None and not replace:
                return (
                    f"This {channel} identity is already bound to user "
                    f"{existing.canonical_user_id}.\n"
                    "Use /user link <code> --replace to rebind it."
                )
            link = identity.consume_link_code(
                link_code_hash=self._secrets.hash_secret(code),
                now_utc=self._clock.now_utc().isoformat(),
            )
            if link is None:
                return "Link code is invalid, expired, or already used."
            if existing is None:
                binding = identity.create_channel_binding(
                    canonical_user_id=link.canonical_user_id,
                    channel_name=channel,
                    external_user_id=external,
                    chat_scope=chat_scope,
                    metadata={"linked_from_thread_id": thread_id},
                )
            else:
                binding = identity.replace_channel_binding(
                    canonical_user_id=link.canonical_user_id,
                    channel_name=channel,
                    external_user_id=external,
                    chat_scope=chat_scope,
                    metadata={
                        "linked_from_thread_id": thread_id,
                        "replace": True,
                    },
                )
            identity.record_gateway_user_event(
                event_type=("newuser_link_replace" if existing else "newuser_link"),
                canonical_user_id=link.canonical_user_id,
                payload={
                    "channel_name": channel,
                    "external_user_id": external,
                    "binding_id": binding.binding_id,
                },
            )
            unit_of_work.commit()
        return f"Linked {channel} identity {external} to user {link.canonical_user_id}."

    def set_default_channel(
        self,
        *,
        resolved_user: ResolvedGatewayUser | None,
        channel_name: str | None,
        chat_id: str | None,
        thread_id: str | None,
        clear: bool = False,
    ) -> str:
        if resolved_user is None:
            return (
                "Register this channel first with /user before changing the "
                "default channel."
            )
        canonical_user_id = resolved_user.user.canonical_user_id
        if clear:
            self._repository.set_gateway_user_default_channel(
                canonical_user_id=canonical_user_id,
                channel_name=None,
                chat_id=None,
                thread_id=None,
            )
            return f"Cleared default notification target for {canonical_user_id}."
        if not channel_name or not chat_id:
            return (
                "The current channel does not have enough delivery metadata "
                "to become a default target."
            )
        self._repository.set_gateway_user_default_channel(
            canonical_user_id=canonical_user_id,
            channel_name=channel_name,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return (
            f"Default notification target set for {canonical_user_id}: "
            f"{channel_name}:{chat_id}"
        )

    def resolve_web_token(
        self,
        raw_token: str,
    ) -> ResolvedGatewayUser | None:
        if not self._settings.web_user_tokens_enabled:
            return None
        token = self._repository.resolve_web_user_token(
            token_hash=self._secrets.hash_secret(raw_token),
            now_utc=self._clock.now_utc().isoformat(),
        )
        if token is None:
            return None
        resolved = self.resolve_user_by_id(token.canonical_user_id)
        if resolved is None:
            return None
        self._repository.update_gateway_user_last_seen(resolved.user.canonical_user_id)
        return resolved

    def _resolve_admin_target(
        self,
        *,
        target: str,
        channel_name: str | None,
        chat_scope: str,
    ) -> GatewayUserRecord | None:
        normalized = str(target or "").strip()
        if not normalized:
            return None
        canonical_user = self._repository.get_gateway_user(normalized)
        if canonical_user is not None:
            return canonical_user
        binding, user = self.lookup_bound_user(
            channel_name=channel_name,
            external_user_id=normalized,
            chat_scope=chat_scope,
        )
        return user if binding is not None else None

    def _membership(
        self, canonical_user_id: str
    ) -> GatewayUserTeamMembershipRecord | None:
        """The person's membership in this team whatever its status, or None."""

        return next(
            (
                membership
                for membership in self._repository.list_team_memberships(
                    canonical_user_id
                )
                if membership.team_id == self._settings.team_id
            ),
            None,
        )

    def _off_team(self, canonical_user_id: str) -> bool:
        """Whether the person was removed from this team or never had a membership in it."""

        membership = self._membership(canonical_user_id)
        return membership is None or membership.status == MEMBERSHIP_STATUS_REMOVED

    def _team_role(self, canonical_user_id: str) -> str | None:
        """The person's active role in this team, or None when not a member."""

        return next(
            (
                membership.role.strip().lower()
                for membership in self._repository.list_team_memberships(
                    canonical_user_id, status="active"
                )
                if membership.team_id == self._settings.team_id
            ),
            None,
        )

    def _active_roles(
        self,
        actor: ResolvedGatewayUser | None,
    ) -> tuple[str, ...]:
        if actor is None:
            return ()
        return tuple(
            membership.role
            for membership in actor.memberships
            if membership.team_id == self._settings.team_id
            and membership.status == "active"
        )

    def _can_create_user(
        self,
        actor: ResolvedGatewayUser | None,
    ) -> bool:
        if actor is None:
            return False
        return self._authorizer.can_create_user(
            actor_canonical_user_id=actor.user.canonical_user_id,
            team_id=self._settings.team_id,
            roles=self._active_roles(actor),
        )

    def _can_list_users(
        self,
        actor: ResolvedGatewayUser | None,
    ) -> bool:
        if actor is None:
            return False
        return self._authorizer.can_list_users(
            actor_canonical_user_id=actor.user.canonical_user_id,
            team_id=self._settings.team_id,
            roles=self._active_roles(actor),
        )

    def _can_create_web_invite(
        self,
        actor: ResolvedGatewayUser | None,
        *,
        target_canonical_user_id: str | None,
    ) -> bool:
        if actor is None:
            return False
        return self._authorizer.can_create_web_invite(
            actor_canonical_user_id=actor.user.canonical_user_id,
            target_canonical_user_id=target_canonical_user_id,
            team_id=self._settings.team_id,
            roles=self._active_roles(actor),
        )

    @staticmethod
    def _render_bindings(
        bindings: list[GatewayUserChannelBindingRecord],
    ) -> str:
        rendered: list[str] = []
        for binding in bindings:
            item = f"{binding.channel_name}:{binding.external_user_id}"
            if binding.chat_scope:
                item += f" scope={binding.chat_scope}"
            rendered.append(item)
        return ", ".join(rendered) or "-"

    @staticmethod
    def _render_default_target(
        user: GatewayUserRecord,
        *,
        empty: str = "-",
    ) -> str:
        if not user.default_channel_name or not user.default_chat_id:
            return empty
        value = f"{user.default_channel_name}:{user.default_chat_id}"
        if user.default_thread_id:
            value += f":{user.default_thread_id}"
        return value


def _with_scheduler_note(text: str, written: bool) -> str:
    """The reply, with a note when the scheduler's copy could not be updated."""

    return text if written else f"{text}\n{SCHEDULER_NOT_TOLD}"


def _status_changed_text(person: GatewayUserRecord, *, enabled: bool) -> str:
    name = f"{person.display_name} ({person.canonical_user_id})"
    if enabled:
        return f"Enabled {name}. They can use the team again and must sign in to Web Chat again."
    return f"Disabled {name}. They cannot use the team until an administrator enables them again."


def _last_holder_refusal(exc: TeamRoleHolderRequired) -> str:
    """Plain words for a change the store refused because it would leave no owner or administrator."""

    if exc.roles == (PERSON_ROLE_OWNER,):
        return "The team must keep at least one owner. Make someone else an owner first."
    return (
        "The team must keep at least one owner or administrator. Make someone "
        "else an administrator first."
    )

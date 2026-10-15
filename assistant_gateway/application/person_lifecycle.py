"""What happens to a person's work and records when their standing in the team changes.

The identity service changes who the person is in the team (disabled,
removed, active again) and copies their standing to the access store
(``GatewayIdentityService``, ``access_control.people``). This service does
what follows, after that change is committed, and says plainly in the reply
what stopped, what was deleted and what is held until when:

- **Disable** (reversible): waiting access requests for their work or made by
  them are withdrawn here; the manager cancels their running and queued
  work, stops their objectives and deletes their schedules
  (``PersonStandingCommandV1``, ``custom_agents.person_departure_runtime``).
  Their folder, memory, conversations, grants and linked accounts are kept.
- **Remove**: the same, and here the gateway records the hold of their data
  and moves their folder to the holding area
  (``access_control.held_person_folders``), deletes their chat sessions, Web
  Chat history, sign-ins and preferences, revokes the grants for their work
  and the grants they gave, revokes their personal account links, deletes
  their requests, notices and starter-profile choice, and asks the broker to
  delete their stored credentials, their GitHub, Google and X sign-ins and
  their private credentials folder (``secure_tool_broker.departed_person_credentials``).
  The manager deletes their task records,
  objectives, memory-review jobs and sandbox, and holds their personal memory
  and conversations. The folder, memory, own knowledge pages and conversations are purged after
  ``gateway.users.removed_data_retention_days`` by a maintenance run.
- **Added** (new, or added back): the administrator's choices are stored and
  folders created (``GatewayPersonSetup``, ``assistant_gateway.application.
  person_setup``), and the reply ends with a report of how the person is set
  up: role, how they were added, timezone, folders, whether their work may use
  the team's model keys, and their own model.
- **Active again** (enabled, added, added back, restored): their per-person
  system schedules (memory review) are installed (``GatewayPersonSchedules``).
  A removed person who comes back while their data is still held gets it
  back: their folder here, their memory, own knowledge pages and conversations
  in the manager. Tasks, schedules, objectives, stored credentials and sign-ins
  never come back: they sign in again with ``/link``.

Repeating ``/user disable`` or ``/user remove`` for someone already in that
state runs the follow-up again, which is how an administrator retries a step
that failed. Everything here applies with authorization on and off: it is
about people, not access checks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from assistant_gateway.application.identity_service import (
    GatewayIdentityService,
    PersonChange,
    ResolvedGatewayUser,
)
from assistant_gateway.application.person_setup import (
    GatewayPersonSetup,
    PersonSetupChoices,
    settings_lines,
    settings_report,
)
from assistant_gateway.application.ports import (
    GatewayClock,
    GatewayPersonSchedules,
    GatewaySecureBroker,
)
from custom_messages.contracts.access_v2 import (
    CredentialsFolderOutcome,
    PersonCredentialDeletionResultV1,
    PersonCredentialDeletionV1,
    PersonStanding,
    SignInService,
)
from custom_messages.contracts.control_v1 import PersonStandingCommandV1, PersonWorkOutcomeV1

_DATE_FORMAT = "%Y-%m-%d %H:%M UTC"
_MANAGER_NOT_CONFIRMED = (
    "The manager has not confirmed yet, so their work may still be running; it stops as soon as "
    "the manager answers, and at the latest at its next start or maintenance run."
)
_SIGN_IN_AGAIN = (
    "Their stored credentials and GitHub, Google and X sign-ins were deleted when they were removed; "
    "they sign in again with /link."
)
_KEPT_ON_REMOVAL = (
    "Kept: audit records, what they put in the shared team/ and agents/ folders, and files they "
    "sent to other people."
)


@dataclass(frozen=True)
class HeldPersonData:
    """A removed person's held folder, memory, own knowledge pages and conversations (``PersonDepartureRepository``)."""

    hold_until: datetime
    restored: bool = False
    purged: bool = False
    folder_moved: bool = False  # this call moved or restored the folder
    problem: str | None = None

    def restorable(self, now: datetime) -> bool:
        return not self.restored and not self.purged and now < self.hold_until


@dataclass(frozen=True)
class ForgottenRecords:
    """What the gateway deleted or revoked at once for a removed person."""

    sessions: int = 0
    transcript_messages: int = 0
    sign_ins: int = 0
    grants_revoked: int = 0
    links_revoked: int = 0
    requests_deleted: int = 0
    notices_deleted: int = 0
    credential_keys: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


@runtime_checkable
class GatewayPersonRecords(Protocol):
    """The gateway's side of a person's records (``assistant_gateway.person_departure``)."""

    def withdraw_requests(self, person_id: str) -> int:
        """Withdraw waiting access requests for the person's work, made by them or for them to decide."""

    def hold(self, person_id: str, *, until: datetime) -> HeldPersonData:
        """Record the hold (keeping an earlier one that is still running) and move the folder."""

    def forget(self, person_id: str, *, actor_id: str) -> ForgottenRecords:
        """Delete and revoke what the gateway holds for the person (module docstring); call after withdrawing."""

    def held(self, person_id: str) -> HeldPersonData | None:
        """The person's hold, or None when nothing was ever held."""

    def restore(self, person_id: str) -> HeldPersonData:
        """Move the held folder back and mark the hold restored."""


@runtime_checkable
class GatewayPersonWork(Protocol):
    """Tell the manager a person's standing changed (``GatewayManagerCommandService``)."""

    async def request_person_standing(
        self, command: PersonStandingCommandV1, *, actor_person_id: str
    ) -> PersonStandingCommandV1:
        """Send the command, signed for the acting administrator, and wait for the manager's answer."""


@dataclass(frozen=True)
class GatewayPersonLifecycleSettings:
    team_id: str
    removed_data_retention_days: int


@dataclass
class _Reply:
    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [line for line in self.lines if line]
        if self.problems:
            lines.append("Not done (see the gateway and manager logs): " + " ".join(self.problems))
        return "\n".join(lines)


class GatewayPersonLifecycle:
    """The follow-up of ``/user disable|enable|remove|restore`` and of adding people (module docstring)."""

    def __init__(
        self,
        *,
        settings: GatewayPersonLifecycleSettings,
        identity: GatewayIdentityService,
        records: GatewayPersonRecords,
        work: GatewayPersonWork,
        broker: GatewaySecureBroker | None,
        person_schedules: GatewayPersonSchedules | None,
        clock: GatewayClock,
        setup: GatewayPersonSetup | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._setup = setup
        self._settings = settings
        self._identity = identity
        self._records = records
        self._work = work
        self._broker = broker
        self._person_schedules = person_schedules
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    # -- the commands ------------------------------------------------------------------

    async def set_enabled(
        self,
        *,
        actor: ResolvedGatewayUser | None,
        target: str,
        enabled: bool,
        channel_name: str | None,
        chat_scope: str,
    ) -> str:
        change = self._identity.set_user_enabled_status(
            actor=actor, target=target, enabled=enabled, channel_name=channel_name, chat_scope=chat_scope
        )
        if change.person_id is None or actor is None:
            return change.text
        if enabled:
            return await self._activated(actor, change)
        return await self._disabled(actor, change)

    async def remove(
        self, *, actor: ResolvedGatewayUser | None, target: str, channel_name: str | None, chat_scope: str
    ) -> str:
        change = self._identity.remove_user(actor=actor, target=target, channel_name=channel_name, chat_scope=chat_scope)
        if change.person_id is None or actor is None:
            return change.text
        return await self._removed(actor, change)

    async def restore(
        self, *, actor: ResolvedGatewayUser | None, target: str, channel_name: str | None, chat_scope: str
    ) -> str:
        person = self._identity.restorable_person(
            actor=actor, target=target, channel_name=channel_name, chat_scope=chat_scope
        )
        if isinstance(person, str) or actor is None:
            return person if isinstance(person, str) else ""
        person_id = person.canonical_user_id
        held = self._records.held(person_id)
        if held is None or not held.restorable(self._clock.now_utc()):
            return _nothing_to_restore(person.display_name, person_id, held)
        # The folder first: if it cannot move back, nothing changes and the
        # person stays removed with their data held.
        try:
            restored = self._records.restore(person_id)
        except Exception:  # noqa: BLE001 - reported; nothing changed
            self._logger.exception(
                "Could not restore a removed person's held folder. event=person_folder_restore_failed person_id=%s",
                person_id,
            )
            restored = HeldPersonData(hold_until=held.hold_until, problem="Their held folder could not be moved back.")
        if restored.problem:
            return f"{restored.problem} Nothing was restored; {person.display_name} is still removed."
        change = self._identity.restore_user(actor=actor, person=person)
        return await self._activated(actor, change, restored_folder=restored.folder_moved)

    async def after_added(
        self,
        actor: ResolvedGatewayUser | None,
        change: PersonChange,
        choices: PersonSetupChoices | None = None,
    ) -> str:
        """The follow-up of adding someone (``/user <id>``, ``/user invite``): new, or added back."""

        if actor is None or change.person_id is None or not change.became_active:
            return change.text
        text = await self._activated(actor, change)
        if self._setup is None:
            return text
        view = self._setup.set_up(
            change.person_id,
            choices or PersonSetupChoices(),
            actor_id=actor.user.canonical_user_id,
            new_person=change.previous is None,
        )
        report = settings_report(
            view,
            display_name=change.display_name,
            role="member",
            added_through=change.added_through,
            timezone=self._identity.timezone_for_user(change.person_id),
        )
        return "\n".join([text, "", *report])

    def set_team_keys(
        self,
        *,
        actor: ResolvedGatewayUser | None,
        target: str,
        allowed: bool,
        channel_name: str | None,
        chat_scope: str,
    ) -> str:
        """``/user team-keys <person> on|off``: may their work use the team's model keys (administrators decide)."""

        person, refusal = self._identity.team_keys_target(
            actor=actor, target=target, channel_name=channel_name, chat_scope=chat_scope
        )
        if refusal is not None or person is None or actor is None:
            return refusal or f"User not found: {target}"
        if self._setup is None:
            return "This gateway has no access store, so people's model settings cannot be changed here."
        view = self._setup.set_team_keys(person.canonical_user_id, allowed, actor_id=actor.user.canonical_user_id)
        effect = (
            "Their agents may use the team's model keys from their next request."
            if allowed
            else "From their next request, their agents use only their own model or keys; without one, their "
            "requests do not run and they are told to set it up with /auth model me."
        )
        return "\n".join([f"Updated {person.display_name} ({person.canonical_user_id}). {effect}", *settings_lines(view)])

    def settings_lines(self, person_id: str) -> list[str]:
        """How the person is set up, for ``/user status`` (empty without an access store)."""

        return settings_lines(self._setup.describe(person_id)) if self._setup is not None else []

    # -- what follows ------------------------------------------------------------------------

    @property
    def _retention_days(self) -> int:
        return max(0, int(self._settings.removed_data_retention_days))

    async def _disabled(self, actor: ResolvedGatewayUser, change: PersonChange) -> str:
        person_id = str(change.person_id)
        reply = _Reply([change.text])
        withdrawn = self._withdraw(person_id, reply)
        outcome = await self._tell_manager(actor, person_id, PersonStanding.DISABLED, reply)
        reply.lines += _work_lines(outcome, removed=False, withdrawn=withdrawn)
        reply.lines.append(
            "Kept: their folder, memory, conversations, access grants, linked accounts and sign-ins. Enabling "
            "them again reinstalls their memory-review schedules; the schedules deleted now do not come back."
        )
        return reply.text()

    async def _removed(self, actor: ResolvedGatewayUser, change: PersonChange) -> str:
        person_id = str(change.person_id)
        reply = _Reply([change.text])
        until = self._clock.now_utc() + timedelta(days=self._retention_days)
        held = self._hold(person_id, until, reply)
        withdrawn = self._withdraw(person_id, reply)
        forgotten = self._forget(actor, person_id, reply)
        credentials = await self._delete_credentials(person_id, forgotten, reply)
        outcome = await self._tell_manager(actor, person_id, PersonStanding.REMOVED, reply)
        reply.lines += _work_lines(outcome, removed=True, withdrawn=withdrawn)
        reply.lines.append(_deleted_line(forgotten, credentials))
        reply.lines.append(_broker_line(credentials))
        if held is not None:
            reply.lines.append(
                f"Held until {held.hold_until.strftime(_DATE_FORMAT)}, then deleted: their folder, personal "
                f"memory, own knowledge pages and conversations. /user restore {person_id} brings them back until then."
            )
        reply.lines.append(_KEPT_ON_REMOVAL)
        return reply.text()

    async def _activated(
        self, actor: ResolvedGatewayUser, change: PersonChange, *, restored_folder: bool = False
    ) -> str:
        person_id = str(change.person_id)
        reply = _Reply([change.text])
        if restored_folder:
            reply.lines.append("Restored their folder.")
        elif change.previous is PersonStanding.REMOVED:
            self._restore_held(person_id, reply)
        if change.previous is PersonStanding.REMOVED:
            # Only a removed person has records the manager holds.
            outcome = await self._tell_manager(actor, person_id, PersonStanding.ACTIVE, reply)
            if outcome is not None and outcome.restored_record_count:
                reply.lines.append(
                    f"Restored {outcome.restored_record_count} record(s) of their personal memory, own knowledge pages and conversations."
                )
        if self._person_schedules is not None:
            await self._person_schedules.ensure_person_schedules(person_id)
            reply.lines.append("Their memory-review schedules are installed.")
        if change.previous in (PersonStanding.DISABLED, PersonStanding.REMOVED):
            reply.lines.append("Tasks, schedules and objectives stopped when they left do not come back.")
        if change.previous is PersonStanding.REMOVED:
            reply.lines.append(_SIGN_IN_AGAIN)
        return reply.text()

    # -- steps --------------------------------------------------------------------------------

    def _withdraw(self, person_id: str, reply: _Reply) -> int:
        try:
            return self._records.withdraw_requests(person_id)
        except Exception:  # noqa: BLE001 - reported in the reply
            self._logger.exception(
                "Could not withdraw a departing person's access requests. "
                "event=person_requests_withdraw_failed person_id=%s",
                person_id,
            )
            reply.problems.append("Their waiting access requests could not be withdrawn.")
            return 0

    def _hold(self, person_id: str, until: datetime, reply: _Reply) -> HeldPersonData | None:
        try:
            held = self._records.hold(person_id, until=until)
        except Exception:  # noqa: BLE001 - reported; the manager then keeps memory, own knowledge pages and conversations in place
            self._logger.exception(
                "Could not record the hold of a removed person's data. event=person_hold_failed person_id=%s",
                person_id,
            )
            reply.problems.append(
                f"Their data could not be put on hold, so their folder, memory, own knowledge pages and conversations stay in place; "
                f"run /user remove {person_id} again."
            )
            return None
        if held.problem:
            reply.problems.append(held.problem)
        return held

    def _forget(self, actor: ResolvedGatewayUser, person_id: str, reply: _Reply) -> ForgottenRecords:
        try:
            forgotten = self._records.forget(person_id, actor_id=actor.user.canonical_user_id)
        except Exception:  # noqa: BLE001 - reported in the reply
            self._logger.exception(
                "Could not delete a removed person's gateway records. event=person_records_forget_failed person_id=%s",
                person_id,
            )
            reply.problems.append(f"Their sessions, grants and links could not be deleted; run /user remove {person_id} again.")
            return ForgottenRecords()
        reply.problems.extend(forgotten.problems)
        return forgotten

    async def _delete_credentials(
        self, person_id: str, forgotten: ForgottenRecords, reply: _Reply
    ) -> PersonCredentialDeletionResultV1 | None:
        broker = self._broker
        if broker is None or not broker.available:
            if forgotten.credential_keys:
                reply.problems.append("The secure broker is not available, so their stored credentials were not deleted.")
            return None
        request = PersonCredentialDeletionV1(
            team_id=self._settings.team_id, person_id=person_id, credential_keys=forgotten.credential_keys
        )
        try:
            result = PersonCredentialDeletionResultV1.model_validate(await broker.delete_person_credentials(request))
        except Exception:  # noqa: BLE001 - reported in the reply
            self._logger.exception(
                "The broker could not delete a removed person's credentials. "
                "event=person_credentials_delete_failed person_id=%s",
                person_id,
            )
            reply.problems.append(f"Their stored credentials could not be deleted; run /user remove {person_id} again.")
            return None
        reply.problems.extend(_credential_problems(result, person_id))
        return result

    def _restore_held(self, person_id: str, reply: _Reply) -> None:
        held = self._records.held(person_id)
        if held is None or not held.restorable(self._clock.now_utc()):
            return
        try:
            restored = self._records.restore(person_id)
        except Exception:  # noqa: BLE001 - reported in the reply
            self._logger.exception(
                "Could not restore a person's held folder. event=person_folder_restore_failed person_id=%s",
                person_id,
            )
            reply.problems.append("Their held folder could not be moved back.")
            return
        if restored.problem:
            reply.problems.append(restored.problem)
        elif restored.folder_moved:
            reply.lines.append("Restored their folder.")

    async def _tell_manager(
        self, actor: ResolvedGatewayUser, person_id: str, standing: PersonStanding, reply: _Reply
    ) -> PersonWorkOutcomeV1 | None:
        command = PersonStandingCommandV1(
            person_id=person_id,
            standing=standing,
        )
        try:
            response = await self._work.request_person_standing(
                command, actor_person_id=actor.user.canonical_user_id
            )
        except (RuntimeError, TimeoutError) as exc:
            self._logger.warning(
                "The manager did not confirm a person standing change yet. event=person_standing_unconfirmed "
                "person_id=%s standing=%s command_id=%s error=%s",
                person_id,
                standing.value,
                command.command_id,
                exc,
            )
            if standing is not PersonStanding.ACTIVE:
                reply.lines.append(_MANAGER_NOT_CONFIRMED)
            return None
        if not response.success:
            self._logger.warning(
                "The manager reported problems with a person standing change. event=person_standing_problems "
                "person_id=%s standing=%s message=%s",
                person_id,
                standing.value,
                response.message,
            )
        outcome = response.outcome
        if outcome is None:
            reply.problems.append(f"The manager refused the change ({response.message}).")
            return None
        reply.problems.extend(outcome.problems)
        return outcome


# -- reply text ------------------------------------------------------------------------------


def _work_lines(outcome: PersonWorkOutcomeV1 | None, *, removed: bool, withdrawn: int) -> list[str]:
    lines: list[str] = []
    if outcome is not None:
        objectives = "stopped and deleted" if removed else "stopped; kept, paused"
        lines.append(
            f"Stopped: {outcome.tasks_stopped} running or queued task(s) and {outcome.objectives_stopped} "
            f"objective(s) ({objectives}). Deleted: {outcome.schedules_deleted} schedule(s)."
        )
        if removed:
            sandbox = " and their OpenShell sandbox" if outcome.sandbox_deleted else ""
            lines.append(
                f"Deleted by the manager: {outcome.task_records_deleted} task record(s), "
                f"{outcome.objectives_deleted} objective(s), {outcome.memory_review_jobs_deleted} memory-review "
                f"job(s){sandbox}; {outcome.held_record_count} record(s) of memory, own knowledge pages and conversations are held."
            )
        if outcome.team_schedules_moved:
            names = ", ".join(item.schedule_name or item.schedule_id for item in outcome.team_schedules_moved)
            owner = outcome.team_schedules_moved[0].new_owner_person_id
            lines.append(f"The team schedules they owned now run for {owner}: {names}.")
    if withdrawn:
        lines.append(f"Withdrawn: {withdrawn} waiting access request(s).")
    return lines


def _deleted_line(forgotten: ForgottenRecords, credentials: PersonCredentialDeletionResultV1 | None) -> str:
    deleted_credentials = len(credentials.deleted) if credentials is not None else 0
    return (
        f"Deleted now: {forgotten.sessions} chat session(s) and their Web Chat history and sign-ins, "
        f"{forgotten.grants_revoked} grant(s) for their work or given by them, {forgotten.links_revoked} personal "
        f"account link(s) and {deleted_credentials} stored credential(s), {forgotten.requests_deleted} request(s), "
        f"{forgotten.notices_deleted} notice(s) and their starter-profile choice."
    )


def _services(services: tuple[SignInService, ...]) -> str:
    labels = [service.label for service in services]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]


def _broker_line(credentials: PersonCredentialDeletionResultV1 | None) -> str:
    """What the broker deleted besides stored credentials; empty when it deleted nothing more."""

    if credentials is None:
        return ""
    deleted: list[str] = []
    if credentials.sign_ins_deleted:
        deleted.append(f"their {_services(credentials.sign_ins_deleted)} sign-in(s)")
    if credentials.folder is CredentialsFolderOutcome.DELETED:
        deleted.append("their private credentials folder")
    return f"Deleted by the broker: {' and '.join(deleted)}." if deleted else ""


def _credential_problems(result: PersonCredentialDeletionResultV1, person_id: str) -> list[str]:
    problems: list[str] = []
    if result.failed:
        problems.append(f"{len(result.failed)} of their stored credentials could not be deleted.")
    if result.sign_ins_failed:
        problems.append(
            f"Their {_services(result.sign_ins_failed)} sign-in(s) could not be deleted, so their private "
            f"credentials folder is kept for now; run /user remove {person_id} again."
        )
    if result.folder is CredentialsFolderOutcome.FAILED:
        problems.append(f"Their private credentials folder could not be deleted; run /user remove {person_id} again.")
    return problems


def _nothing_to_restore(name: str, person_id: str, held: HeldPersonData | None) -> str:
    fresh = f'Adding them back (/user <external_user_id> "{name}" or /user invite {person_id}) starts fresh.'
    if held is None or held.restored:
        return f"No data is held for {name} ({person_id}), so there is nothing to restore. {fresh}"
    return (
        f"The folder, memory, own knowledge pages and conversations {name} ({person_id}) had when they were removed were held until "
        f"{held.hold_until.strftime(_DATE_FORMAT)}; they can no longer be restored, and the next maintenance run "
        f"deletes them if it has not already. {fresh}"
    )


__all__ = [
    "ForgottenRecords",
    "GatewayPersonLifecycle",
    "GatewayPersonLifecycleSettings",
    "GatewayPersonRecords",
    "GatewayPersonWork",
    "HeldPersonData",
]

"""The ``people`` section: the team's people, their roles, chat accounts and Web Chat sign-ins.

What travels for each person with a membership in the team that was not
removed: their id (kept, so every other record that names them still
matches), name, time zone, standing (active or disabled), default delivery
target, role in the team, appearance, the chat accounts linked to them, and
their active Web Chat sign-in tokens.

Why these choices:

- People removed from the team stay behind: their data is held only until it
  is deleted (owner's decision for the pinned memory export, kept here).
- Chat accounts (``gateway_user_channel_bindings``) are what admits a person
  who writes from Telegram, Slack and the others; with the same bot tokens
  they keep chatting after the move. Command-line links are left out: their
  scope names the old instance.
- Web Chat tokens are stored only as unsalted SHA-256 hashes with no instance
  key, so carrying the hash keeps a browser at the same address signed in
  (owner's decision 2026-10-05). The raw token never leaves the browser.
  Invites and link codes are one-time and stay behind.
- The gateway's copy of the people in the access store (``access_people``) is
  rebuilt at every gateway start, so it is not carried.

The import refuses the whole section when no active owner would remain, and
refuses a person whose role this version does not know or whose chat account
another imported person already holds.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field, JsonValue

from team_transfer.format import TransferModel
from team_transfer.sections import (
    ExportSource,
    ImportContext,
    PlannedRecord,
    RecordOutcome,
    SectionExport,
    SectionFiles,
    SectionRecords,
)

logger = logging.getLogger(__name__)

# Command-line links name the old instance in their scope (request_context).
_CLI_CHANNEL = "cli"


class DeliveryTargetV1(TransferModel):
    channel_name: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None


class ChatAccountV1(TransferModel):
    channel_name: str = Field(min_length=1, max_length=120)
    external_user_id: str = Field(min_length=1, max_length=500)
    chat_scope: str = Field(default="", max_length=500)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class WebSignInV1(TransferModel):
    token_hash: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=500)
    expires_at_utc: Optional[str] = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class PersonV1(TransferModel):
    person_id: str = Field(min_length=1, max_length=240)
    display_name: str = Field(min_length=1, max_length=500)
    timezone: str = Field(min_length=1, max_length=120)
    status: str = Field(min_length=1, max_length=40)
    role: str = Field(min_length=1, max_length=40)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    default_target: Optional[DeliveryTargetV1] = None
    appearance: Optional[dict[str, JsonValue]] = None
    chat_accounts: tuple[ChatAccountV1, ...] = ()
    web_sign_ins: tuple[WebSignInV1, ...] = ()


def _gateway_repository(storage_settings: Any) -> Any:
    from storage.gateway_state_repository import create_gateway_state_repository

    return create_gateway_state_repository(storage_settings)


def _gateway_storage(layout: Any) -> Any:
    from assistant_gateway.config import GatewayInstanceConfig

    return GatewayInstanceConfig.load_for_process(
        layout.instance_config_path
    ).state_storage_settings


def _not_expired(expires_at_utc: Optional[str], now: datetime) -> bool:
    if not expires_at_utc:
        return True
    try:
        return datetime.fromisoformat(expires_at_utc.replace("Z", "+00:00")) > now
    except ValueError:
        return False


class PeopleSection:
    name: ClassVar[str] = "people"
    schema_version: ClassVar[int] = 1
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = PersonV1

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        from access_control.people import MEMBERSHIP_STATUS_REMOVED

        repository = _gateway_repository(source.config.state_storage_settings)
        now = datetime.now(timezone.utc)
        records = []
        for member in repository.list_team_people(source.team_id):
            if member.membership_status == MEMBERSHIP_STATUS_REMOVED:
                continue
            records.append(
                self._person(repository, member, now).model_dump(mode="json")
            )
        return SectionExport(records=records)

    def _person(self, repository: Any, member: Any, now: datetime) -> PersonV1:
        from access_control.people import (
            MEMBERSHIP_STATUS_ACTIVE,
            PERSON_STATUS_DISABLED,
        )

        user = repository.get_gateway_user(member.canonical_user_id)
        appearance = repository.get_gateway_user_appearance(member.canonical_user_id)
        target = None
        if user.default_channel_name or user.default_chat_id:
            target = DeliveryTargetV1(
                channel_name=user.default_channel_name,
                chat_id=user.default_chat_id,
                thread_id=user.default_thread_id,
            )
        accounts = [
            ChatAccountV1(
                channel_name=binding.channel_name,
                external_user_id=binding.external_user_id,
                chat_scope=binding.chat_scope,
                metadata=json.loads(binding.metadata_json or "{}"),
            )
            for binding in repository.list_channel_bindings_for_user(
                member.canonical_user_id, status="active"
            )
            if binding.channel_name != _CLI_CHANNEL
        ]
        sign_ins = [
            WebSignInV1(
                token_hash=token.token_hash,
                label=token.label,
                expires_at_utc=token.expires_at_utc,
                metadata=token.metadata,
            )
            for token in repository.list_web_user_tokens(
                member.canonical_user_id, status="active"
            )
            if _not_expired(token.expires_at_utc, now)
        ]
        return PersonV1(
            person_id=user.canonical_user_id,
            display_name=user.display_name,
            timezone=user.timezone,
            status=user.status
            if member.membership_status == MEMBERSHIP_STATUS_ACTIVE
            else PERSON_STATUS_DISABLED,
            role=member.role,
            metadata=user.metadata,
            default_target=target,
            appearance=json.loads(appearance.appearance_json)
            if appearance is not None
            else None,
            chat_accounts=tuple(accounts),
            web_sign_ins=tuple(sign_ins),
        )

    # -- import ----------------------------------------------------------------

    def plan(
        self, records: SectionRecords, context: ImportContext
    ) -> list[PlannedRecord]:
        people = [PersonV1.model_validate(record) for record in records]
        existing = [
            PersonV1.model_validate(raw) for raw in context.baseline.get(self.name, ())
        ]
        claimed = _destination_accounts(context)
        planned = [self._plan_person(person, claimed) for person in people]
        accepted = {
            record.key
            for record in planned
            if record.outcome is not RecordOutcome.REFUSED
        }
        _require_active_owner(
            [*existing, *[person for person in people if person.person_id in accepted]]
        )
        return planned

    def _plan_person(
        self, person: PersonV1, claimed: dict[tuple[str, str, str], str]
    ) -> PlannedRecord:
        problem = _person_problem(person, claimed)
        if problem:
            return PlannedRecord(
                self.name, person.person_id, RecordOutcome.REFUSED, problem
            )
        appearance_problem = _appearance_problem(person.appearance)
        if appearance_problem:
            return PlannedRecord(
                self.name, person.person_id, RecordOutcome.ATTENTION, appearance_problem
            )
        return PlannedRecord(self.name, person.person_id, RecordOutcome.IMPORT)

    def apply(
        self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext
    ) -> None:
        outcomes = {record.key: record.outcome for record in plan}
        repository = _gateway_repository(_gateway_storage(context.layout))
        team_id = context.reader.manifest.team_id
        for raw in records:
            person = PersonV1.model_validate(raw)
            outcome = outcomes.get(person.person_id)
            if outcome is not None and outcome is not RecordOutcome.REFUSED:
                _write_person(
                    repository,
                    person,
                    team_id,
                    keep_appearance=outcome is RecordOutcome.IMPORT,
                )
        logger.info(
            "Team people imported. event=team_transfer_people_imported people=%s refused=%s",
            sum(
                1
                for outcome in outcomes.values()
                if outcome is not RecordOutcome.REFUSED
            ),
            sum(1 for outcome in outcomes.values() if outcome is RecordOutcome.REFUSED),
        )


def _destination_accounts(context: ImportContext) -> dict[tuple[str, str, str], str]:
    """Every active binding occupies an account, even after its person leaves."""
    if not context.existing:
        return {}
    from storage.read_only import read_only_repositories
    from team_transfer.settings import planned_config, storage_environment

    config = context.scratch.get("settings.original_config") or planned_config(context)
    with storage_environment(context), read_only_repositories():
        repository = _gateway_repository(config.state_storage_settings)
        return {
            (binding.channel_name, binding.external_user_id, binding.chat_scope): binding.canonical_user_id
            for binding in repository.list_active_channel_bindings()
        }


def _person_problem(person: PersonV1, claimed: dict[tuple[str, str, str], str]) -> str:
    """Why this version refuses the person ("" when it accepts them); records their chat accounts as claimed."""

    from access_control.people import (
        PERSON_ROLES,
        PERSON_STATUS_ACTIVE,
        PERSON_STATUS_DISABLED,
    )

    problem = ""
    if person.role not in PERSON_ROLES:
        problem = f"this version has no role {person.role!r}"
    elif person.status not in {PERSON_STATUS_ACTIVE, PERSON_STATUS_DISABLED}:
        problem = f"this version has no person standing {person.status!r}"
    for account in person.chat_accounts:
        key = (account.channel_name, account.external_user_id, account.chat_scope)
        if not problem and key in claimed:
            problem = (
                f"its {account.channel_name} account is also linked to {claimed[key]}"
            )
        claimed.setdefault(key, person.person_id)
    return problem


def _require_active_owner(people: list[PersonV1]) -> None:
    from access_control.people import PERSON_ROLE_OWNER, PERSON_STATUS_ACTIVE

    if not any(
        person.role == PERSON_ROLE_OWNER and person.status == PERSON_STATUS_ACTIVE
        for person in people
    ):
        from team_transfer.errors import TransferError

        raise TransferError(
            "No active owner would be imported, so nobody could administer the new instance."
        )


def _write_person(
    repository: Any, person: PersonV1, team_id: str, *, keep_appearance: bool
) -> None:
    """Write one person in the order the gateway requires: appearance only while active, standing last."""

    from access_control.people import PERSON_STATUS_ACTIVE

    repository.create_gateway_user(
        canonical_user_id=person.person_id,
        display_name=person.display_name,
        timezone=person.timezone,
        metadata=dict(person.metadata),
        status=PERSON_STATUS_ACTIVE,
    )
    if person.default_target is not None:
        repository.set_gateway_user_default_channel(
            canonical_user_id=person.person_id,
            channel_name=person.default_target.channel_name,
            chat_id=person.default_target.chat_id,
            thread_id=person.default_target.thread_id,
        )
    if person.appearance is not None and keep_appearance:
        repository.upsert_gateway_user_appearance(
            canonical_user_id=person.person_id, appearance=dict(person.appearance)
        )
    repository.create_team_membership(
        canonical_user_id=person.person_id, team_id=team_id, role=person.role
    )
    _write_accounts(repository, person)
    if person.status != PERSON_STATUS_ACTIVE:
        repository.set_gateway_user_status(person.person_id, person.status)


def _write_accounts(repository: Any, person: PersonV1) -> None:
    from access_control.people import PERSON_STATUS_ACTIVE

    for account in person.chat_accounts:
        repository.create_channel_binding(
            canonical_user_id=person.person_id,
            channel_name=account.channel_name,
            external_user_id=account.external_user_id,
            chat_scope=account.chat_scope,
            metadata=dict(account.metadata),
        )
    # A disabled person cannot sign in; their sign-ins are not carried over.
    for sign_in in person.web_sign_ins if person.status == PERSON_STATUS_ACTIVE else ():
        repository.create_web_user_token(
            token_hash=sign_in.token_hash,
            canonical_user_id=person.person_id,
            label=sign_in.label,
            expires_at_utc=sign_in.expires_at_utc,
            metadata=dict(sign_in.metadata),
        )


def _appearance_problem(appearance: Optional[dict[str, Any]]) -> str:
    """Why this version's appearance model refuses the saved appearance ("" when it accepts it)."""

    if appearance is None:
        return ""
    from pydantic import ValidationError

    from assistant_gateway.application.website_application import WebAppearanceV1

    try:
        WebAppearanceV1.model_validate(appearance)
    except ValidationError as exc:
        return f"their appearance is reset to the default; this version refuses it: {exc.errors()[0]['msg']}"
    return ""


__all__ = [
    "ChatAccountV1",
    "DeliveryTargetV1",
    "PeopleSection",
    "PersonV1",
    "WebSignInV1",
]

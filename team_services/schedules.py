"""People's schedule commands at the team's address: ``schedules.list``, ``.pause``, ``.resume``,
``.disable`` and ``.remove``.

Until phase 2 the gateway sent these to the team lead's process as a durable
``ManagerScheduleCommand``; they now run here on the same schedule store, with the same
rules (``custom_agents.person_schedule_controls``):

- the person comes only from the command's authority token (``team_services.people``),
  and sees and changes only the schedules they own;
- a system schedule changes only for people who could approve a ``system_schedule`` change
  (the access service, with the person's current roles from the people copy); with
  authorization off, any active member may, as before;
- a change names the listing digest the person saw (``seen_version``), checked with the
  permit in the store's write transaction, and its reply, with the version it saved, is
  stored in that same transaction (``storage.operation_outcomes``).

A chat's own list names the chat by its opaque delivery reference
(``custom_transport.protocol1.delivery_refs``), compared here with the reference of each
schedule's stored session. Schedule creation stays on today's queued path until phase 3.

The team lead's scheduler wakes for a change it makes itself; a change made here reaches
it at its next pass, within a minute (``MAX_SCHEDULER_SLEEP_SECONDS``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from access_control.ownership import PRIVATE_DELETE, PRIVATE_READ, PRIVATE_UPDATE
from access_control.people_directory import PeopleDirectory
from custom_agents.person_schedule_controls import (
    apply_person_schedule_change,
    schedule_owned_by,
    schedule_worker_assignment,
)
from custom_agents.schedule_management import SystemScheduleMutationDenied, is_system_managed_schedule
from custom_agents.schedule_store import ScheduleStore, ScheduleWriteConflict, schedule_record_digest
from custom_messages.contracts.schedule_v1 import SYSTEM_SCHEDULE_CHANGE_KIND, ScheduledMessage
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame
from custom_transport.protocol1.delivery_refs import DeliveryRefs
from storage.operation_outcomes import CommandOutcome
from team_services.outcomes import carried_out_once
from team_services.people import CommandPerson, command_person

SCHEDULE_KINDS = ("schedules.list", "schedules.pause", "schedules.resume", "schedules.disable", "schedules.remove")
_STATES = {"pause": "paused", "resume": "resumed", "disable": "disabled", "remove": "removed"}
_MESSAGE_MAX = 500
_PREVIEW_MAX = 500
NOT_FOUND_TEXT = "That schedule was not found."
CHANGED_TEXT = "The schedule changed since you listed it. Refresh and try again."
SYSTEM_NOT_ALLOWED_TEXT = "Only people who may change the team's system schedules can change this one."


class ScheduleOperations:
    """Answers people's schedule commands for one team (module docstring)."""

    def __init__(self, store: ScheduleStore, people: PeopleDirectory, delivery_refs: Optional[DeliveryRefs],
                 may_change_system_schedules: Callable[[CommandPerson], bool], clock: Callable[[], datetime],
                 logger: logging.Logger) -> None:
        self._store = store
        self._people = people
        self._delivery_refs = delivery_refs
        self._may_change_system = may_change_system_schedules
        self._clock = clock
        self._logger = logger

    def handlers(self) -> dict[str, Callable[[VerifiedFrame], Any]]:
        return {kind: self.handle for kind in SCHEDULE_KINDS}

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        return await asyncio.to_thread(self.answer, verified)

    def answer(self, verified: VerifiedFrame) -> dict[str, Any]:
        action = verified.frame.kind.removeprefix("schedules.")
        body = verified.raw["body"]
        person, reason = command_person(verified, self._people, self._logger)
        if person is None:
            return _message("not_allowed", reason)
        if action == "list":
            try:
                return self._list(person, body)
            except Exception:
                self._logger.exception("A schedule list failed. event=team_schedule_list_failed person=%s",
                                       person.person_id)
                return _message("runtime_error", "The schedules could not be listed.")
        outcome = CommandOutcome(
            team_id=person.team_id, retry_key=str(verified.raw["idempotency_key"]), kind=verified.frame.kind,
            body_digest=str(verified.raw["body_digest"]), now=self._clock(),
            reply_for=lambda write: _changed_reply(action, body["schedule_id"], write), logger=self._logger,
        )
        return carried_out_once(self._store.command_outcomes, outcome, lambda: self._change(person, action, body, outcome),
                                refusal_for=lambda error: self._refusal(person, action, body, error),
                                logger=self._logger)

    # -- list -------------------------------------------------------------------------

    def _list(self, person: CommandPerson, body: dict[str, Any]) -> dict[str, Any]:
        wanted = body.get("delivery_ref")
        if wanted is not None and self._delivery_refs is None:
            raise RuntimeError("This team services instance has no delivery reference key.")
        rows = []
        for row in self._store.get_all():
            schedule_id = str(row.get("schedule_id") or "").strip()
            schedule = self._store.get_scheduled_message(schedule_id) if schedule_id else None
            if schedule is None or not schedule_owned_by(schedule, person_id=person.person_id, action=PRIVATE_READ):
                continue
            if wanted is not None:
                session_key = getattr(getattr(schedule.payload_meta, "gateway_context", None), "session_key", None)
                if not session_key or self._delivery_refs.ref(str(session_key)) != wanted:  # type: ignore[union-attr]
                    continue
            rows.append(schedule)
        offset, limit = int(body["offset"]), int(body["limit"])
        shown = [self._wire_schedule(schedule, person) for schedule in rows[offset:offset + limit]]
        reply: dict[str, Any] = {"outcome": "ok", "schedules": shown, "total": len(rows)}
        if shown and offset + len(shown) < len(rows):
            reply["next_offset"] = offset + len(shown)
        return reply

    def _wire_schedule(self, schedule: ScheduledMessage, person: CommandPerson) -> dict[str, Any]:
        worker = schedule_worker_assignment(self._store, schedule, person.person_id).model_dump(mode="json",
                                                                                                exclude_none=True)
        if "not_running_since" in worker:
            worker["not_running_since"] = _timestamp(worker["not_running_since"])
        if "not_running_explanation" in worker:
            worker["not_running_explanation"] = {"type": "inline", "text": worker["not_running_explanation"]}
        row: dict[str, Any] = {
            "schedule_id": schedule.schedule_id,
            "management_class": schedule.management_class.value,
            # A stored schedule's payload is always a task for a model turn (ScheduledMessage.payload);
            # no stored schedule sends a fixed message.
            "action": {"type": "task"},
            "timing": _timing(schedule),
            "enabled": bool(schedule.enabled),
            "timing_revision": int(getattr(schedule, "timing_revision", 0) or 0),
            "seen_version": schedule_record_digest(schedule),
            "worker": worker,
        }
        if schedule.management_class.value == "system" and schedule.effective_system_schedule_kind is not None:
            row["system_schedule_kind"] = schedule.effective_system_schedule_kind.value
        if schedule.schedule_name:
            row["name"] = schedule.schedule_name
        if schedule.objective_id:
            row["objective_id"] = schedule.objective_id
        preview = " ".join(str(schedule.payload.problem_statement or "").split())
        if preview:
            row["instruction_preview"] = preview[:_PREVIEW_MAX]
        for field, wire in (("next_fire_at", "next_fire_at"), ("paused_until", "paused_until")):
            value = getattr(schedule, field, None)
            if value:
                row[wire] = _timestamp(value)
        expires = getattr(schedule, "expires_at_utc", None)
        if expires:
            row["expires_at"] = _timestamp(expires)
        if getattr(schedule, "repeat_count", None) is not None:
            row["repeat_count"] = int(schedule.repeat_count)
        return row

    # -- changes ----------------------------------------------------------------------

    def _change(self, person: CommandPerson, action: str, body: dict[str, Any], outcome: CommandOutcome) -> dict[str, Any]:
        schedule = self._store.get_scheduled_message(body["schedule_id"])
        needed = PRIVATE_DELETE if action == "remove" else PRIVATE_UPDATE
        if not schedule_owned_by(schedule, person_id=person.person_id, action=needed):
            raise LookupError(NOT_FOUND_TEXT)
        assert schedule is not None
        if is_system_managed_schedule(schedule) and not self._may_change_system(person):
            self._logger.warning("Refused a person's change to a system schedule. event=team_system_schedule_change_refused "
                                 "schedule_id=%s action=%s person=%s", schedule.schedule_id, action, person.person_id)
            raise PermissionError(SYSTEM_NOT_ALLOWED_TEXT)
        replies: list[dict[str, Any]] = []
        applied = apply_person_schedule_change(
            self._store, action, schedule, person_id=person.person_id, team_id=person.team_id,
            expected_record_digest=body["seen_version"], paused_until=body.get("paused_until"),
            command_outcome=_capturing(outcome, replies),
        )
        if not applied:
            raise LookupError(NOT_FOUND_TEXT)
        self._logger.info("A person changed a schedule. event=team_schedule_changed schedule_id=%s action=%s person=%s",
                          schedule.schedule_id, action, person.person_id)
        return replies[-1]

    def _refusal(self, person: CommandPerson, action: str, body: dict[str, Any], error: Exception) -> dict[str, Any]:
        if isinstance(error, LookupError):
            return _message("not_found", str(error))
        if isinstance(error, PermissionError):
            return _message("not_allowed", str(error))
        if isinstance(error, SystemScheduleMutationDenied):
            # The person may make the change (checked above); the store's permit, built from the schedule as
            # read, no longer matches it: it changed in between, like a stale listed version.
            self._logger.warning("A system schedule change was refused. event=team_schedule_change_refused schedule_id=%s "
                                 "action=%s person=%s reason=%s", body.get("schedule_id"), action, person.person_id,
                                 error.reason_code)
        if isinstance(error, (ScheduleWriteConflict, SystemScheduleMutationDenied)):
            current = self._store.get_scheduled_message(body["schedule_id"])
            if current is None:
                return _message("not_found", NOT_FOUND_TEXT)
            return {"outcome": "changed_since_read", "current_version": schedule_record_digest(current),
                    "message": CHANGED_TEXT}
        self._logger.error("A schedule change failed. event=team_schedule_change_failed schedule_id=%s action=%s "
                           "person=%s error_type=%s", body.get("schedule_id"), action, person.person_id,
                           type(error).__name__, exc_info=error)
        return _message("runtime_error", "The schedule was not changed.")


def _capturing(outcome: CommandOutcome, replies: list[dict[str, Any]]) -> CommandOutcome:
    """The same outcome, keeping the reply it stores so the change's caller returns exactly it."""

    from dataclasses import replace

    def reply_for(write: Any) -> dict[str, Any]:
        reply = outcome.reply_for(write)
        replies.append(reply)
        return reply

    return replace(outcome, reply_for=reply_for)


def _changed_reply(action: str, schedule_id: str, write: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {"outcome": "ok", "state": _STATES[action], "schedule_id": schedule_id}
    if action != "remove":
        digest = getattr(write, "listed_digest", None)
        reply["version"] = {"state": "available", "seen_version": digest} if digest else {"state": "unavailable"}
    return reply


def _timing(schedule: ScheduledMessage) -> dict[str, Any]:
    if schedule.interval_ms:
        return {"kind": "interval", "interval_ms": int(schedule.interval_ms)}
    if schedule.cron_expression:
        return {"kind": "cron", "expression": schedule.cron_expression, "time_zone": schedule.cron_timezone or "UTC"}
    return {"kind": "once", "at": _timestamp(schedule.next_fire_at or datetime.now(timezone.utc).isoformat())}


def _timestamp(value: Any) -> str:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return format_timestamp(moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc))


def _message(outcome: str, message: str) -> dict[str, Any]:
    text = (message or "").strip() or "No details."
    return {"outcome": outcome, "message": text if len(text) <= _MESSAGE_MAX else text[: _MESSAGE_MAX - 1] + "…"}


class _PeopleCopyRoles:
    """Current roles from the gateway's copy of the team's people (``MembershipRoles`` for the team services)."""

    def __init__(self, people: PeopleDirectory) -> None:
        self._people = people

    def roles_of(self, person_id: str, team_id: str) -> tuple[str, ...] | None:
        now = self._people.person(person_id) if team_id == self._people.team_id else None
        return now.roles if now is not None and now.active and now.roles else None


class SystemScheduleCheck:
    """Whether a person may change the team's system schedules: who could approve a ``system_schedule`` change.

    The gateway's own rule (``assistant_gateway.access_setup.PersonChangeCheck``) with the person's current
    roles from the people copy: with authorization on the access service decides, with it off any active
    member may. The access service starts on the first decision and stays open for the process.
    """

    def __init__(self, config: Any, people: PeopleDirectory) -> None:
        from assistant_gateway.access_setup import PersonChangeCheck

        self._check = PersonChangeCheck(config, _PeopleCopyRoles(people))

    def __call__(self, person: CommandPerson) -> bool:
        return self._check.may_change(person.person_id, SYSTEM_SCHEDULE_CHANGE_KIND)

    def close(self) -> None:
        self._check.close()

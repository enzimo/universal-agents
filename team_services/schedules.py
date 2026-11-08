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
import json
import logging
from datetime import datetime, timezone
from threading import Event, Lock
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
from custom_messages.wire.frame import VerifiedFrame, body_model
from custom_messages.wire.sizes import REPLY_BODY_MAX_BYTES, encoded_size
from custom_transport.protocol1.delivery_refs import DeliveryRefs
from storage.operation_outcomes import CommandOutcome
from team_services.outcomes import CommandOnce
from team_services.people import CommandPerson, command_person
from team_services.service import OperationFamily

SCHEDULE_KINDS = ("schedules.list", "schedules.pause", "schedules.resume", "schedules.disable", "schedules.remove")
_STATES = {"pause": "paused", "resume": "resumed", "disable": "disabled", "remove": "removed"}
_MESSAGE_MAX = 500
_PREVIEW_MAX = 500
# The schedule list schema's name and run-note string limit.
_ROW_TEXT_MAX = 16_384
NOT_FOUND_TEXT = "That schedule was not found."
CHANGED_TEXT = "The schedule changed since you listed it. Refresh and try again."
SYSTEM_NOT_ALLOWED_TEXT = "Only people who may change the team's system schedules can change this one."


class ScheduleNotFound(LookupError):
    """An absent schedule or one outside the command person's team and ownership."""


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

    def family(self) -> OperationFamily:
        return OperationFamily({kind: self.handle for kind in SCHEDULE_KINDS}, frozenset(SCHEDULE_KINDS),
                               self.purge_outcomes, self.close)

    async def purge_outcomes(self) -> None:
        await asyncio.to_thread(self._store.command_outcomes.purge_command_outcomes, self._clock())

    def close(self) -> None:
        if isinstance(self._may_change_system, SystemScheduleCheck):
            self._may_change_system.close()

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        cancelled = Event()
        running = asyncio.create_task(asyncio.to_thread(self.answer, verified, cancelled=cancelled))
        cancellation = None
        # Retain the same worker through overlapping stop and shutdown requests.
        # Its write must finish before the host counts this handler as drained.
        while not running.done():
            try:
                await asyncio.shield(running)
            except asyncio.CancelledError as error:
                cancelled.set()
                cancellation = error
        try:
            return running.result()
        finally:
            if cancellation is not None:
                raise cancellation

    def answer(self, verified: VerifiedFrame, *, cancelled: Optional[Event] = None) -> dict[str, Any]:
        action = verified.frame.kind.removeprefix("schedules.")
        body = verified.raw["body"]
        check = command_person(verified, self._people, self._logger)
        once = None
        if action != "list" and check.person_id is not None:
            outcome = CommandOutcome(
                team_id=self._people.team_id, person_id=check.person_id,
                retry_key=str(verified.raw["idempotency_key"]), kind=verified.frame.kind,
                body_digest=str(verified.raw["body_digest"]), now=self._clock(),
                reply_for=lambda write: _checked_change_reply(action, body["schedule_id"], write), logger=self._logger,
            )
            once = CommandOnce(
                self._store.command_outcomes, outcome, reply_from_receipt=lambda reply: reply,
                key_taken=lambda error: _message("not_allowed", str(error)),
                not_stored=_message("runtime_error", "The schedule command could not be answered. "
                                    "Check the schedule before sending it again."), logger=self._logger,
            )
            stored = once.stored_reply()
            if stored is not None:
                return stored
        person = check.person
        if person is None:
            reply = _message("not_allowed", check.reason)
            return once.final(reply) if once is not None else reply
        if action == "list":
            try:
                return self._list(person, body)
            except Exception:
                self._logger.exception("A schedule list failed. event=team_schedule_list_failed person=%s",
                                       person.person_id)
                return _message("runtime_error", "The schedules could not be listed.")
        assert once is not None
        return once.run(lambda: self._change(person, action, body, once.outcome, cancelled),
                        refusal_for=lambda error: self._refusal(person, action, body, error))

    # -- list -------------------------------------------------------------------------

    def _list(self, person: CommandPerson, body: dict[str, Any]) -> dict[str, Any]:
        wanted = body.get("delivery_ref")
        if wanted is not None and self._delivery_refs is None:
            self._logger.warning("A chat's schedule list needs the delivery reference key. "
                                 "event=team_schedule_delivery_ref_unavailable")
            return _message("runtime_error", "This chat's schedules cannot be listed until an administrator "
                            "restarts the team through the launcher to prepare its chat reference key.")
        rows = self._visible_schedules(person, wanted)
        return self._list_page(rows, person, offset=int(body["offset"]), limit=int(body["limit"]))

    def _visible_schedules(self, person: CommandPerson, wanted: Optional[str]) -> list[ScheduledMessage]:
        """The person's rows in stable id order, optionally restricted to one opaque chat reference."""

        rows = []
        for schedule in self._store.list_scheduled_messages(team_id=person.team_id):
            if not schedule_owned_by(schedule, person_id=person.person_id, action=PRIVATE_READ):
                continue
            if wanted is not None:
                session_key = getattr(getattr(schedule.payload_meta, "gateway_context", None), "session_key", None)
                if not session_key or self._delivery_refs.ref(str(session_key)) != wanted:  # type: ignore[union-attr]
                    continue
            rows.append(schedule)
        return rows

    def _list_page(self, rows: list[ScheduledMessage], person: CommandPerson, *, offset: int, limit: int) -> dict[str, Any]:
        """Stop before the next row would exceed the signed reply's byte budget."""

        shown = []
        size = encoded_size({"outcome": "ok", "schedules": [], "total": len(rows), "next_offset": len(rows)})
        for schedule in rows[offset:offset + limit]:
            wire = self._wire_schedule(schedule, person)
            size += encoded_size(wire) + 1
            if size > REPLY_BODY_MAX_BYTES:
                if not shown:
                    raise ValueError("One schedule row exceeds the reply size limit.")
                break
            shown.append(wire)
        reply: dict[str, Any] = {"outcome": "ok", "schedules": shown, "total": len(rows)}
        if shown and offset + len(shown) < len(rows):
            reply["next_offset"] = offset + len(shown)
        body_model("schedules.list.reply")[0].validate_python(reply)
        return reply

    def _wire_schedule(self, schedule: ScheduledMessage, person: CommandPerson) -> dict[str, Any]:
        worker = schedule_worker_assignment(self._store, schedule, person.person_id, now=self._clock()).model_dump(mode="json",
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
            row["name"] = self._bounded(schedule, "name", schedule.schedule_name)
        if schedule.objective_id:
            row["objective_id"] = schedule.objective_id
        preview = " ".join(str(schedule.payload.problem_statement or "").split())
        if preview:
            row["instruction_preview"] = preview[:_PREVIEW_MAX]
        self._wire_times(schedule, row, worker)
        if getattr(schedule, "repeat_count", None) is not None:
            row["repeat_count"] = int(schedule.repeat_count)
        self._wire_notes(schedule, row)
        return row

    def _wire_times(self, schedule: ScheduledMessage, row: dict[str, Any], worker: dict[str, Any]) -> None:
        """Invalid saved optional times remain visible as a warning, without preventing removal."""

        for field, wire in (("next_fire_at", "next_fire_at"), ("paused_until", "paused_until"),
                             ("expires_at_utc", "expires_at")):
            value = getattr(schedule, field, None)
            if value:
                parsed = self._stored_timestamp(schedule, field, value)
                if parsed is not None:
                    row[wire] = parsed
                else:
                    row.setdefault("invalid_time_fields", []).append(field)
                    worker["not_running_explanation"] = {
                        "type": "inline", "text": "A saved schedule time is invalid. Resume the schedule to clear "
                        "an invalid pause, or remove it and create it again.",
                    }
    def _wire_notes(self, schedule: ScheduledMessage, row: dict[str, Any]) -> None:
        """Run-note settings and state, while keeping even one large row within the frame budget."""

        if schedule.schedule_memory is not None:
            row["run_notes"] = self._run_notes(schedule)
            if encoded_size(row) > REPLY_BODY_MAX_BYTES - 1024:
                del row["run_notes"]
                self._logger.warning("The run notes do not fit beside their schedule in one reply. "
                                     "event=team_schedule_notes_omitted schedule_id=%s field=run_notes",
                                     schedule.schedule_id)

    def _bounded(self, schedule: ScheduledMessage, field: str, value: str) -> str:
        if len(value) > _ROW_TEXT_MAX:
            self._logger.warning("Shortened a schedule listing field; its stored value is unchanged. "
                                 "event=team_schedule_text_shortened schedule_id=%s field=%s",
                                 schedule.schedule_id, field)
        return value[:_ROW_TEXT_MAX]

    def _stored_timestamp(self, schedule: ScheduledMessage, field: str, value: Any) -> Optional[str]:
        try:
            return _timestamp(value)
        except (ValueError, TypeError):
            # Older pause commands accepted arbitrary text. Keep the row available to
            # resume or remove; never rewrite its stored value while listing it.
            self._logger.warning("A saved schedule time is invalid; omitted it from the list. "
                                 "event=team_schedule_invalid_time schedule_id=%s field=%s",
                                 schedule.schedule_id, field)
            return None

    def _run_notes(self, schedule: ScheduledMessage) -> dict[str, Any]:
        memory = schedule.schedule_memory
        assert memory is not None
        policy = memory.policy.model_dump(exclude={"custom"})
        policy["evidence_required"] = self._bounded(schedule, "evidence_required", policy["evidence_required"])
        state = {field: self._bounded(schedule, field, getattr(memory.state, field))
                 for field in ("summary", "confidence")}
        for field in ("last_observed_at", "last_changed_at"):
            value = getattr(memory.state, field)
            if value:
                parsed = self._stored_timestamp(schedule, field, value)
                if parsed is not None:
                    state[field] = parsed
        notes = {"schedule_kind": self._bounded(schedule, "schedule_kind", memory.schedule_kind),
                 "policy": policy, "state": state}
        for target, field, value in ((policy, "custom_json", memory.policy.custom),
                                     (state, "domain_json", memory.state.domain),
                                     (notes, "domain_schema_hint_json", memory.domain_schema_hint)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if len(text) <= _ROW_TEXT_MAX:
                target[field] = text
            else:
                self._logger.warning("Omitted oversized structured run notes from a schedule list. "
                                     "event=team_schedule_notes_omitted schedule_id=%s field=%s",
                                     schedule.schedule_id, field)
        return notes

    # -- changes ----------------------------------------------------------------------

    def _change(self, person: CommandPerson, action: str, body: dict[str, Any], outcome: CommandOutcome,
                cancelled: Optional[Event]) -> dict[str, Any]:
        schedule = self._store.read_scheduled_message(body["schedule_id"])
        needed = PRIVATE_DELETE if action == "remove" else PRIVATE_UPDATE
        if schedule is None or schedule.claim_team_id != person.team_id or not schedule_owned_by(
            schedule, person_id=person.person_id, action=needed,
        ):
            raise ScheduleNotFound(NOT_FOUND_TEXT)
        assert schedule is not None
        if is_system_managed_schedule(schedule) and not self._may_change_system(person):
            self._logger.warning("Refused a person's change to a system schedule. event=team_system_schedule_change_refused "
                                 "schedule_id=%s action=%s person=%s", schedule.schedule_id, action, person.person_id)
            raise PermissionError(SYSTEM_NOT_ALLOWED_TEXT)
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError("The schedule command was cancelled before its write.")
        replies: list[dict[str, Any]] = []
        applied = apply_person_schedule_change(
            self._store, action, schedule, person_id=person.person_id, team_id=person.team_id,
            expected_record_digest=body["seen_version"], paused_until=body.get("paused_until"),
            command_outcome=_capturing(outcome, replies),
        )
        if not applied:
            raise ScheduleNotFound(NOT_FOUND_TEXT)
        self._logger.info("A person changed a schedule. event=team_schedule_changed schedule_id=%s action=%s person=%s",
                          schedule.schedule_id, action, person.person_id)
        return replies[-1]

    def _refusal(self, person: CommandPerson, action: str, body: dict[str, Any], error: Exception) -> dict[str, Any]:
        if isinstance(error, ScheduleNotFound):
            return _message("not_found", str(error))
        if isinstance(error, SystemScheduleMutationDenied):
            # The person may make the change (checked above); the store's permit, built from the schedule as
            # read, no longer matches it: it changed in between, like a stale listed version.
            self._logger.warning("A system schedule change was refused. event=team_schedule_change_refused schedule_id=%s "
                                 "action=%s person=%s reason=%s", body.get("schedule_id"), action, person.person_id,
                                 error.reason_code)
        if isinstance(error, (ScheduleWriteConflict, SystemScheduleMutationDenied)):
            try:
                current = self._store.read_scheduled_message(body["schedule_id"])
            except Exception as failure:
                return self._refusal(person, action, body, failure)
            if current is None or current.claim_team_id != person.team_id or not schedule_owned_by(
                current, person_id=person.person_id, action=PRIVATE_READ,
            ):
                return _message("not_found", NOT_FOUND_TEXT)
            if isinstance(error, SystemScheduleMutationDenied) and schedule_record_digest(current) == body["seen_version"]:
                return _message("not_allowed", "The schedule changed while this command was checked. Refresh and try again.")
            return {"outcome": "changed_since_read", "current_version": schedule_record_digest(current),
                    "message": CHANGED_TEXT}
        if isinstance(error, PermissionError):
            return _message("not_allowed", str(error))
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


def _checked_change_reply(action: str, schedule_id: str, write: Any) -> dict[str, Any]:
    reply = _changed_reply(action, schedule_id, write)
    body_model(f"schedules.{action}.reply")[0].validate_python(reply)
    return reply


def _timing(schedule: ScheduledMessage) -> dict[str, Any]:
    if schedule.interval_ms:
        return {"kind": "interval", "interval_ms": int(schedule.interval_ms)}
    if schedule.cron_expression:
        return {"kind": "cron", "expression": schedule.cron_expression, "time_zone": schedule.cron_timezone or "UTC"}
    return {"kind": "once", "at": _timestamp(schedule.next_fire_at or schedule.created_at)}


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
        self._lock = Lock()

    def __call__(self, person: CommandPerson) -> bool:
        with self._lock:
            return self._check.may_change(person.person_id, SYSTEM_SCHEDULE_CHANGE_KIND)

    def close(self) -> None:
        with self._lock:
            self._check.close()

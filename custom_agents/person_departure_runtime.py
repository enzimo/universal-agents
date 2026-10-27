"""End a departed person's work and bring the manager's records in line with their standing.

When an administrator disables or removes a person, the gateway changes its
own records, copies the person's standing to the access store
(``access_control.people_directory``) and sends the manager a
``PersonStandingCommandV1``. The manager then acts on the standing it reads
from that copy (``settle_person``), so a late or repeated command never acts
on an old standing, and doing it twice changes nothing more:

- **Disabled** (reversible): their running and queued task trees are cancelled
  (``control_runtime.stop_task_execution``, which records restart barriers);
  their open objectives are stopped (kept, paused, no further runs); their
  schedules are deleted, their own and their per-person system schedules
  (``scope`` ``PERSON`` in ``custom_messages.system_schedules``: memory review
  of their facts and conversations, objective work sessions,
  recovery checkpoints, retries, reviews of after-action reports about their
  work). Their memory, conversations, files, grants, linked accounts and
  pending after-action reports are kept.
- **Removed**: all of the above, and their task records (after-action report
  reviews included), objectives and memory-review jobs are deleted, their
  OpenShell sandbox is deleted, and their personal memory, own knowledge pages and conversations
  move to the holding table
  (``storage.person_departure_repository``) until they are restored or the
  hold expires. The gateway writes the hold record (with its end) before it
  sends the command; without one the memory and conversations stay where they
  are and the problem is reported, so nothing is deleted on a guess.
- **Active again** (enabled, added back, restored): when the gateway restored
  the person's held data, their held memory and conversations move back.

The runtime's own team schedules (maintenance, the core-facts memory audit,
the heartbeat launcher and every other system schedule that is not someone's
own) never leave with a person: they move to another active administrator
(``access_control.people.team_work_owner``) under a trusted-runtime permit
(``custom_agents.schedule_management``), and administrators get an Inbox
notice. A moved schedule, and every item of a moved heartbeat launcher,
continues in a new conversation, because a conversation belongs to one person
(``custom_agents.conversation_runtime``). All heartbeat items get new ids before
the launcher changes owner; a failed reset keeps its inactive owner so the
next departure sweep retries it safely. Without an active administrator they
stay (they do not run while their owner is inactive) and the problem is reported.

Safety net: the same function runs for everyone the people copy lists as
disabled or removed at manager start and before each maintenance run
(``settle_departed_people``), which also purges held data past its time
(``purge_expired_holds``). This covers a command lost while the manager was
down, and a removal the gateway applied while the manager was stopped.

Only the gateway may send the command: the manager requires the
gateway-signed task context of an active administrator on the envelope
(``_sender_refusal``), and agents cannot send manager control messages
(``custom_tools.providers.communication``). This works the same with
authorization on and off: it is about people, not access checks.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable

from access_control.held_person_folders import purge_folder, purge_held_artifact_files
from access_control.people import PERSON_ROLE_ADMIN, is_administrator, team_work_owner
from access_control.people_directory import PersonNow
from custom_agents.access_runtime import send_runtime_notice_once
from custom_agents.control_runtime import stop_task_execution
from custom_agents.manager_control_runtime import (
    CommandPerson,
    _apply_objective_control,
    _runtime_work_roots,
    _send_durable_response,
    verified_command_person,
)
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    build_schedule_mutation_permit,
    effective_system_schedule_kind,
    is_system_managed_schedule,
)
from custom_agents.heartbeat_store import HeartbeatStore, is_heartbeat_launcher
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_agents.task_context_runtime import people_directory
from custom_messages.contracts.access_v2 import AccessNoticeKind, PersonStanding
from custom_messages.contracts.control_v1 import (
    ManagerRuntimeWorkCommand,
    MovedTeamScheduleV1,
    PersonStandingCommandV1,
    PersonWorkOutcomeV1,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import MessageMetaType, ObjectiveStatus
from custom_messages.system_schedules import is_person_system_schedule
from custom_tools.openshell_sandboxes import OpenShellSandboxError, delete_person_sandbox
from id_generation import new_id
from storage.config import StorageSettings
from storage.person_departure_repository import PersonDepartureRepository

logger = logging.getLogger(__name__)

# Objectives in these states still plan more work; departure stops them.
_RUNNING_OBJECTIVE_STATES = frozenset({ObjectiveStatus.PROPOSED, ObjectiveStatus.ACTIVE, ObjectiveStatus.BLOCKED})
# The person and chat fields of a schedule's gateway context. A moved team
# schedule keeps none of the departed person's: the gateway's next refresh
# fills them in for the new owner (routing_v1.PERSON_GATEWAY_FIELDS).
_DEPARTED_ROUTE_FIELDS = ("task_context", "user_id", "session_key", "channel_name", "chat_id", "thread_id")
_STANDING_WORDS = {
    PersonStanding.DISABLED: "was disabled",
    PersonStanding.REMOVED: "was removed from the team",
}
TEAM_SCHEDULES_MOVED_NOTICE_PREFIX = "team_schedules_moved"
SECURITY_LAYER_MODE_ENV = "SECURITY_LAYER_MODE"
OPENSHELL_MODE = "openshell"


@lru_cache(maxsize=4)
def _departures(storage: StorageSettings) -> PersonDepartureRepository:
    return PersonDepartureRepository(storage)


def departure_store(agent: Any) -> PersonDepartureRepository:
    return _departures(agent.task_manager.storage_settings)


# -- the command -------------------------------------------------------------------


def _sender_refusal(agent: Any, command: PersonStandingCommandV1, msg_meta: MessageMeta) -> str | None:
    """Why this command is not from the gateway for an active administrator, or None.

    The gateway signs the acting administrator's context with its own key
    (``access_control.identity``); nothing else holds that key. The person and
    roles come only from that signed context
    (``manager_control_runtime.verified_command_person``).
    """

    if not getattr(agent, "is_team_lead", False):
        return "manager_required"
    person, refusal = verified_command_person(agent, msg_meta)
    if person is None:
        return refusal
    if not is_administrator(person.roles):
        return "administrator_required"
    return None


async def handle_person_standing_command(
    agent: Any,
    *,
    command: PersonStandingCommandV1,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    """Handle the gateway's command (module docstring) and answer with what changed."""

    if command.command_type == MessageMetaType.RESPONSE:
        return
    response = command.model_copy(deep=True)
    response.command_type = MessageMetaType.RESPONSE
    refusal = _sender_refusal(agent, command, msg_meta)
    if refusal:
        logger.warning(
            "Refused a person standing command that is not from the gateway for an administrator. "
            "event=person_standing_command_refused command_id=%s person_id=%s reason=%s",
            command.command_id,
            command.person_id,
            refusal,
        )
        response.success = False
        response.message = refusal
    else:
        outcome = await settle_person(agent, command.person_id, command_id=command.command_id)
        if outcome.standing is not command.standing:
            logger.warning(
                "The gateway's people copy and its command disagree about a person; the manager acted on the "
                "copy. event=person_standing_mismatch command_id=%s person_id=%s command_standing=%s copy_standing=%s",
                command.command_id,
                command.person_id,
                command.standing.value,
                outcome.standing.value if outcome.standing else None,
            )
        response.success = not outcome.problems
        response.outcome = outcome
        response.message = "settled" if response.success else "settled_with_problems"
    await _send_durable_response(agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta)


# -- one person ------------------------------------------------------------------------


async def settle_person(agent: Any, person_id: str, *, command_id: str | None = None) -> PersonWorkOutcomeV1:
    """Bring the manager's records in line with the person's current standing (module docstring). Idempotent."""

    person = await asyncio.to_thread(people_directory(agent).person, person_id)
    outcome = PersonWorkOutcomeV1(standing=person.standing)
    if person.standing is PersonStanding.ACTIVE:
        await _restore_held_records(agent, person_id, outcome)
        return outcome
    if person.standing not in _STANDING_WORDS:
        return outcome
    reason = f"The person this work was for {_STANDING_WORDS[person.standing]}."
    run_id = command_id or f"person_departure:{person_id}"
    await _stop_task_trees(agent, person_id, outcome, reason=reason, command_id=run_id)
    await _stop_objectives(agent, person_id, outcome, reason=reason, command_id=run_id)
    await asyncio.to_thread(_clear_schedules, agent, person_id, outcome)
    if person.standing is PersonStanding.REMOVED:
        await asyncio.to_thread(_forget_records, agent, person_id, outcome)
    logger.info(
        "Brought a departed person's work in line with their standing. event=person_departure_settled "
        "person_id=%s standing=%s tasks_stopped=%d objectives_stopped=%d schedules_deleted=%d "
        "team_schedules_moved=%d task_records_deleted=%d objectives_deleted=%d memory_review_jobs_deleted=%d "
        "held_records=%d problems=%d",
        person_id,
        person.standing.value,
        outcome.tasks_stopped,
        outcome.objectives_stopped,
        outcome.schedules_deleted,
        len(outcome.team_schedules_moved),
        outcome.task_records_deleted,
        outcome.objectives_deleted,
        outcome.memory_review_jobs_deleted,
        outcome.held_record_count,
        len(outcome.problems),
    )
    return outcome


async def _stop_task_trees(
    agent: Any, person_id: str, outcome: PersonWorkOutcomeV1, *, reason: str, command_id: str
) -> None:
    for root in await _runtime_work_roots(agent, person_id):
        try:
            result = await stop_task_execution(
                agent,
                task_info=root,
                reason=reason,
                cascade_to_subtasks=True,
                canonical_user_id=person_id,
                command_id=command_id,
                send_response=False,
            )
        except Exception:  # noqa: BLE001 - reported; the other trees still stop
            logger.exception(
                "Could not stop a departed person's task. event=person_departure_task_stop_failed person_id=%s "
                "root_task_id=%s",
                person_id,
                root.task.task_id,
            )
            outcome.problems.append(f"Task {root.task.task_id} could not be stopped.")
            continue
        outcome.tasks_stopped += 1
        if not result.get("durable_cancellation_recorded"):
            outcome.problems.append(f"Task {root.task.task_id} stopped, but its restart barrier was not saved.")


async def _stop_objectives(
    agent: Any, person_id: str, outcome: PersonWorkOutcomeV1, *, reason: str, command_id: str
) -> None:
    """Stop the person's running objectives through the objective controls, for the owner."""

    objectives = await agent.task_manager.operations_runtime.run(
        agent.task_manager.list_user_owned_objectives,
        owner_canonical_user_id=person_id,
        team_id=agent.team_id,
        limit=1000,
        include_closed=False,
    )
    # Trusted runtime work for the departing person's own objectives.
    owner = CommandPerson(canonical_user_id=person_id, team_id=str(agent.team_id), roles=())
    for objective in objectives:
        if objective.status not in _RUNNING_OBJECTIVE_STATES:
            continue
        command = ManagerRuntimeWorkCommand(
            command_id=f"{command_id}:{objective.objective_id}"[:200],
            action="stop_objective",
            scope="objective",
            target_objective_id=objective.objective_id,
            reason=reason,
        )
        response = command.model_copy(deep=True)
        await _apply_objective_control(
            agent, command=command, response=response, summary_override=reason, person=owner
        )
        if response.success:
            outcome.objectives_stopped += 1
        else:
            outcome.problems.append(f"Objective {objective.objective_id} could not be fully stopped ({response.message}).")


# -- schedules ---------------------------------------------------------------------------


@dataclass
class _ScheduleMoves:
    new_owner: str | None
    moved: list[MovedTeamScheduleV1] = field(default_factory=list)


def _people_rows(people: dict[str, PersonNow]) -> Iterable[tuple[str, bool, tuple[str, ...]]]:
    return ((person_id, person.active, person.roles) for person_id, person in people.items())


def _clear_schedules(agent: Any, person_id: str, outcome: PersonWorkOutcomeV1) -> None:
    """Delete the person's own schedules and move the team's to another administrator."""

    store = agent._ensure_schedule_store()
    people = people_directory(agent).people()
    moves = _ScheduleMoves(new_owner=team_work_owner(_people_rows(people), preferred=None))
    for row in store.all_rows():
        schedule = store.get_scheduled_message(str(row.get("schedule_id") or ""))
        if schedule is None or owner_canonical_user_id_for_schedule(schedule) != person_id:
            continue
        if _is_team_schedule(schedule):
            _move_team_schedule(agent, store, schedule, person_id, moves, outcome)
        else:
            _delete_schedule(agent, store, schedule, person_id, outcome)
    outcome.team_schedules_moved.extend(moves.moved)
    if moves.moved:
        _tell_administrators_about_moves(agent, person_id, moves)


def _is_team_schedule(schedule: Any) -> bool:
    """A system schedule that is not one person's own work (module docstring).

    The system schedule table says which kinds are one person's (``scope``);
    a system schedule of no known kind is the team's.
    """

    return is_system_managed_schedule(schedule) and not is_person_system_schedule(schedule)


def _delete_schedule(agent: Any, store: Any, schedule: Any, person_id: str, outcome: PersonWorkOutcomeV1) -> None:
    permit = build_schedule_mutation_permit(
        schedule=schedule,
        operation=ScheduleMutationOperation.DELETE,
        authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
        actor_principal_id=str(agent.id),
        owner_canonical_user_id=person_id,
        team_id=str(agent.team_id),
    )
    try:
        removed = store.remove(schedule.schedule_id, system_mutation_permit=permit)
    except Exception:  # noqa: BLE001 - reported; the other schedules are still handled
        logger.exception(
            "Could not delete a departed person's schedule. event=person_departure_schedule_delete_failed "
            "person_id=%s schedule_id=%s",
            person_id,
            schedule.schedule_id,
        )
        removed = False
    if removed:
        outcome.schedules_deleted += 1
    else:
        outcome.problems.append(f"Schedule {schedule.schedule_id} could not be deleted.")


def _move_team_schedule(
    agent: Any,
    store: Any,
    schedule: Any,
    person_id: str,
    moves: _ScheduleMoves,
    outcome: PersonWorkOutcomeV1,
) -> None:
    if moves.new_owner is None:
        logger.error(
            "No active administrator can take over a team schedule, so it stays with its departed owner and "
            "does not run. event=team_schedule_owner_unavailable person_id=%s schedule_id=%s",
            person_id,
            schedule.schedule_id,
        )
        outcome.problems.append(
            f"Team schedule {schedule.schedule_id} has no active administrator to take it over; it does not run."
        )
        return
    # Reset every item before committing the new owner. The old owner is
    # inactive, so a failed or interrupted reset keeps runs stopped and leaves
    # this schedule selected by the next departure sweep, even after its old
    # conversations have been held. Partial item writes are safe to repeat.
    if not _hand_over_heartbeat_items(agent, schedule, person_id, outcome):
        return
    candidate = schedule.model_copy(deep=True)
    context = candidate.payload_meta.gateway_context
    context.canonical_user_id = moves.new_owner
    for name in _DEPARTED_ROUTE_FIELDS:
        setattr(context, name, None)
    # The schedule's conversation is the departed person's (the session it was
    # installed with). The new owner's runs continue in a new conversation they
    # own, so they never read the departed person's turns nor add theirs to the
    # departed person's conversation, which a removal holds and a restore puts
    # back by id (custom_agents.conversation_runtime).
    candidate.payload_meta.conversation_id = new_id("conversation_id")
    candidate.payload_meta.followup_to_task_id = None
    permit = build_schedule_mutation_permit(
        schedule=schedule,
        operation=ScheduleMutationOperation.UPDATE,
        authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
        actor_principal_id=str(agent.id),
        owner_canonical_user_id=person_id,
        team_id=str(agent.team_id),
        candidate=candidate,
    )
    try:
        # Saved against the schedule as read: refused (and retried by the next
        # sweep) when it changed or went away meanwhile; never recreated.
        store.update(candidate, base=schedule, system_mutation_permit=permit)
    except Exception:  # noqa: BLE001 - reported; it keeps its old owner and does not run
        logger.exception(
            "Could not move a team schedule to another administrator. event=team_schedule_move_failed "
            "person_id=%s schedule_id=%s new_owner=%s",
            person_id,
            schedule.schedule_id,
            moves.new_owner,
        )
        outcome.problems.append(f"Team schedule {schedule.schedule_id} could not be moved to another administrator.")
        return
    logger.info(
        "Moved a team schedule from a departed person to another administrator. event=team_schedule_moved "
        "schedule_id=%s kind=%s from_person=%s to_person=%s conversation_id=%s",
        schedule.schedule_id,
        effective_system_schedule_kind(schedule),
        person_id,
        moves.new_owner,
        candidate.payload_meta.conversation_id,
    )
    moves.moved.append(
        MovedTeamScheduleV1(
            schedule_id=schedule.schedule_id,
            schedule_name=str(schedule.schedule_name or ""),
            new_owner_person_id=moves.new_owner,
        )
    )


def _hand_over_heartbeat_items(agent: Any, launcher: Any, person_id: str, outcome: PersonWorkOutcomeV1) -> bool:
    """Prepare fresh item conversations before changing the launcher owner; False stops the move.

    Done before the launcher moves, and not only when an item runs
    (``heartbeat_store.item_conversation_for_run``): a removal holds the
    departed person's conversations, so at run time they look unused and the
    new owner's runs would recreate them under the same ids, into which a
    restore would then put the departed person's turns back.
    """

    if not is_heartbeat_launcher(launcher):
        return True
    source_id = launcher.payload_meta.gateway_context.heartbeat_source_id
    try:
        store = HeartbeatStore(storage_settings=agent.task_manager.storage_settings, logger_=logger)
        changed = store.start_new_source_conversations(source_id)
    except Exception:  # noqa: BLE001 - reported; the launcher keeps its inactive owner for retry
        logger.exception(
            "Could not give a departing heartbeat launcher's items new conversations; its owner was not changed "
            "and its runs stay stopped until a retry succeeds. event=heartbeat_items_handover_failed "
            "person_id=%s schedule_id=%s source_id=%s",
            person_id,
            launcher.schedule_id,
            source_id,
        )
        outcome.problems.append(
            f"The heartbeat items of {launcher.schedule_id} could not get new conversations, so its owner was not changed."
        )
        return False
    logger.info(
        "Gave a departing heartbeat launcher's items new conversations before changing its owner. "
        "event=heartbeat_items_handed_over "
        "person_id=%s schedule_id=%s source_id=%s items=%d",
        person_id,
        launcher.schedule_id,
        source_id,
        changed,
    )
    return True


def _tell_administrators_about_moves(agent: Any, person_id: str, moves: _ScheduleMoves) -> None:
    lines = "\n".join(f"- {item.schedule_name or item.schedule_id} ({item.schedule_id})" for item in moves.moved)
    content = f"{person_id}:{moves.new_owner}:" + ",".join(sorted(item.schedule_id for item in moves.moved))
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
    send_runtime_notice_once(
        agent,
        notice_id=f"{TEAM_SCHEDULES_MOVED_NOTICE_PREFIX}:{digest}",
        kind=AccessNoticeKind.SCHEDULES_TO_REVIEW,
        roles=(PERSON_ROLE_ADMIN,),
        title=f"{len(moves.moved)} team schedule(s) now belong to {moves.new_owner}",
        summary=(
            f"{person_id} can no longer use the team, so the runtime's own schedules they owned now run for "
            f"{moves.new_owner}, an active administrator:\n{lines}\n\n"
            "Their findings and escalations now reach that person, and they continue in new conversations, so "
            f"the earlier conversation history stays with {person_id}. Nothing else changed."
        ),
    )


# -- records ------------------------------------------------------------------------------


def _forget_records(agent: Any, person_id: str, outcome: PersonWorkOutcomeV1) -> None:
    """Removal: delete task records, objectives, memory-review jobs and the sandbox; hold memory and conversations."""

    store = departure_store(agent)
    # In the manager's operation lane: an Objective change in progress loads
    # and saves within one operation, so it cannot save the person's
    # Objective (or plan) back after this deletes it.
    deleted = agent.task_manager.operations_runtime.run_sync(store.delete_person_records, person_id)
    outcome.task_records_deleted += deleted.get("completed_tasks", 0)
    outcome.objectives_deleted += deleted.get("objectives", 0)
    outcome.memory_review_jobs_deleted += store.delete_memory_review_jobs(person_id)
    _delete_sandbox(person_id, outcome)
    _hold_records(agent, store, person_id, outcome)


def _hold_records(agent: Any, store: PersonDepartureRepository, person_id: str, outcome: PersonWorkOutcomeV1) -> None:
    """Hold the person's memory and conversations under the gateway's hold record.

    Rows that appear after the hold was purged are deleted at once: the
    retention time already passed.
    """

    team_id = str(agent.team_id)
    hold = store.hold_of(team_id, person_id)
    if hold is None or hold.restored_at is not None:
        logger.warning(
            "A removed person has no hold record, so their memory and conversations stay in place until "
            "an administrator runs /user remove again. event=person_departure_hold_missing person_id=%s",
            person_id,
        )
        outcome.problems.append("Their memory and conversations were not held: the gateway's hold record is missing.")
        return
    held = store.hold_person_records(team_id, person_id, at=datetime.now(timezone.utc))
    if hold.purged_at is not None:
        store.delete_held_rows(team_id, person_id)
        return
    outcome.held_record_count += held


def _delete_sandbox(person_id: str, outcome: PersonWorkOutcomeV1) -> None:
    if str(os.environ.get(SECURITY_LAYER_MODE_ENV) or "").strip().lower() != OPENSHELL_MODE:
        return
    openshell = shutil.which("openshell")
    if not openshell:
        outcome.problems.append("Their OpenShell sandbox was not deleted: the openshell command is not available.")
        return
    try:
        outcome.sandbox_deleted = delete_person_sandbox(openshell, person_id, os.environ)
    except (OpenShellSandboxError, OSError) as exc:
        logger.error(
            "Could not delete a removed person's OpenShell sandbox. event=person_departure_sandbox_delete_failed "
            "person_id=%s error=%s",
            person_id,
            exc,
        )
        outcome.problems.append("Their OpenShell sandbox could not be deleted.")


async def _restore_held_records(agent: Any, person_id: str, outcome: PersonWorkOutcomeV1) -> None:
    """Move held memory and conversations back once the gateway restored the person (module docstring)."""

    store = departure_store(agent)
    hold = await asyncio.to_thread(store.hold_of, str(agent.team_id), person_id)
    if hold is None or hold.restored_at is None or hold.purged_at is not None:
        return
    restored = await asyncio.to_thread(store.restore_person_records, str(agent.team_id), person_id)
    outcome.restored_record_count += restored
    if restored:
        logger.info(
            "Restored a person's held memory and conversations. event=person_records_restored person_id=%s rows=%d",
            person_id,
            restored,
        )


# -- everyone: the safety net and the purge ----------------------------------------------------


@dataclass
class DepartureSweep:
    """What one pass over every departed person did (manager start and each maintenance run)."""

    people: int = 0
    purged: int = 0
    problems: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    def instruction_sentence(self) -> str:
        """Counts only, for the maintenance run's instructions: no model sees anyone's records."""

        if self.skipped_reason:
            return f"Trusted code did not check departed people before this review: {self.skipped_reason}."
        return (
            f"Trusted code ended the work of {self.people} disabled or removed person(s) and deleted the held data "
            f"of {self.purged} person(s) whose retention time passed. People are handled there, so do not look "
            "for or report them."
        )


async def settle_departed_people(agent: Any, *, now: datetime | None = None) -> DepartureSweep:
    """Run ``settle_person`` for everyone disabled or removed, then purge expired holds (module docstring)."""

    directory = people_directory(agent)
    if not await asyncio.to_thread(directory.synced):
        return DepartureSweep(skipped_reason="the gateway has not recorded the team's people yet")
    sweep = DepartureSweep()
    people = await asyncio.to_thread(directory.people)
    for person_id, person in sorted(people.items()):
        if person.standing not in _STANDING_WORDS:
            continue
        outcome = await settle_person(agent, person_id)
        sweep.people += 1
        sweep.problems.extend(f"{person_id}: {problem}" for problem in outcome.problems)
    sweep.purged = await asyncio.to_thread(purge_expired_holds, agent, now=now)
    return sweep


def purge_expired_holds(agent: Any, *, now: datetime | None = None) -> int:
    """Delete held folders and records whose retention time passed; people purged."""

    from custom_tools.workspace_scope import current_instance_layout

    moment = now or datetime.now(timezone.utc)
    store = departure_store(agent)
    layout = current_instance_layout(agent)
    purged = 0
    for hold in store.expired_holds(str(agent.team_id), moment):
        if layout is None:
            logger.warning(
                "Held data was not purged because this runtime has no instance folder layout. "
                "event=person_hold_purge_skipped reason=no_instance_layout person_id=%s",
                hold.person_id,
            )
            continue
        try:
            # The files first: their records name them, so a failure keeps the records for the next run.
            purge_held_artifact_files(layout, hold.person_id, store.held_artifact_paths(hold.team_id, hold.person_id))
            purge_folder(layout, hold.person_id)
            rows = store.delete_held_rows(hold.team_id, hold.person_id)
            store.mark_purged(hold.team_id, hold.person_id, at=moment)
        except Exception:  # noqa: BLE001 - kept; the next maintenance run tries again
            logger.exception(
                "Could not purge a removed person's held data. event=person_hold_purge_failed person_id=%s",
                hold.person_id,
            )
            continue
        purged += 1
        logger.info(
            "Deleted a removed person's held data after its retention time. event=person_hold_purged "
            "person_id=%s held_until=%s rows=%d",
            hold.person_id,
            hold.hold_until.isoformat(),
            rows,
        )
    return purged


async def settle_departed_people_safely(agent: Any, *, when: str) -> str:
    """``settle_departed_people`` for manager start and maintenance; failures are logged, never raised."""

    try:
        sweep = await settle_departed_people(agent)
    except Exception:  # noqa: BLE001 - the manager keeps starting; the next run tries again
        logger.exception(
            "Could not end the work of disabled or removed people. event=person_departure_sweep_failed when=%s",
            when,
        )
        return (
            "Ending the work of disabled or removed people failed before this review (manager log "
            "event=person_departure_sweep_failed); report it to TeamArchitect."
        )
    if sweep.problems:
        logger.warning(
            "Ended departed people's work with problems. event=person_departure_sweep_problems when=%s problems=%s",
            when,
            " | ".join(sweep.problems),
        )
    return sweep.instruction_sentence()


__all__ = [
    "DepartureSweep",
    "departure_store",
    "handle_person_standing_command",
    "purge_expired_holds",
    "settle_departed_people",
    "settle_departed_people_safely",
    "settle_person",
]

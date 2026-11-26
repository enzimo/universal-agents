"""Schedule operations at the team's address: ``schedules.list``, ``.install``, ``.pause``,
``.resume``, ``.disable`` and ``.remove``.

``schedules.install`` accepts either a gateway command token (for people's own installs and
gateway maintenance schedules) or an agent host's root-task authority token (for agent schedule
tools and turn actions), records ``owner_vouched = 1`` under the 13C.5 vouching rules, stores
its reply atomically with the write transaction (``storage.operation_outcomes``), and triggers
synchronous initial run-permit issuance via ``team_services.scheduler``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from access_control.ownership import PRIVATE_DELETE, PRIVATE_READ, PRIVATE_UPDATE
from access_control.people_directory import PeopleDirectory
from custom_agents.person_schedule_controls import (
    apply_person_schedule_change,
    person_schedule_permit,
    schedule_owned_by,
    schedule_worker_assignment,
)
from custom_agents.schedule_conversation_context import schedule_conversation_seed_task_id
from custom_agents.schedule_execution_identity import follow_authorized_change
from custom_agents.schedule_management import (
    ScheduleMutationAuthorityMode,
    ScheduleMutationOperation,
    SystemScheduleMutationDenied,
    build_schedule_mutation_permit,
    is_system_managed_schedule,
)
from custom_agents.schedule_store import (
    SCHEDULE_INSTALLED,
    SCHEDULE_REFRESHED,
    SCHEDULE_REPLACED,
    ScheduleInstallRefused,
    ScheduleInstallResult,
    ScheduleStore,
    ScheduleWriteConflict,
    compute_next_fire_at,
    owner_canonical_user_id_for_schedule,
    schedule_record_digest,
    schedule_stopped_reason,
)
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import (
    SYSTEM_SCHEDULE_CHANGE_KIND,
    ScheduleMemoryPolicy,
    ScheduleMemoryState,
    ScheduleTaskMemory,
    ScheduledMessage,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    RoutingType,
    ScheduleManagementClass,
    SystemScheduleKind,
)
from custom_messages.system_schedules import SystemScheduleScope, system_schedule_spec
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame, body_model
from custom_messages.wire.sizes import REPLY_BODY_MAX_BYTES, encoded_size
from custom_messages.wire.values import parse_timestamp
from custom_transport.protocol1.blobs import BlobCorrupted, BlobTooLarge
from custom_transport.protocol1.delivery_refs import DeliveryRefs
from custom_transport.protocol1.knowledge_blobs import (
    KnowledgeBlobUnavailable,
    decode_text_content,
)
from storage.operation_outcomes import CommandOutcome
from team_services.outcomes import CommandOnce
from team_services.people import CommandPerson, InstallActorCheck, command_person, schedule_install_actor
from team_services.service import OperationFamily

SCHEDULE_KINDS = (
    "schedules.list",
    "schedules.install",
    "schedules.pause",
    "schedules.resume",
    "schedules.disable",
    "schedules.remove",
)
_STATES = {"pause": "paused", "resume": "resumed", "disable": "disabled", "remove": "removed"}
_AGENT_PERSON_SYSTEM_KINDS = frozenset({
    SystemScheduleKind.OBJECTIVE_WORK_SESSION,
    SystemScheduleKind.ISOLATED_RECOVERY_CHECKPOINT,
})
_INSTRUCTION_MAX_CHARS = 2_000_000
_MESSAGE_MAX = 500
_PREVIEW_MAX = 500
# The schedule list schema's name and run-note string limit.
_ROW_TEXT_MAX = 16_384
NOT_FOUND_TEXT = "That schedule was not found."
CHANGED_TEXT = "The schedule changed since you listed it. Refresh and try again."
SYSTEM_NOT_ALLOWED_TEXT = "Only people who may change the team's system schedules can change this one."


@dataclass(frozen=True)
class ScheduleInstallDefaults:
    """Instance defaults applied when ``schedules.install`` omits optional routing or worker fields."""

    lead_agent_type: str = "TeamArchitect"
    response_route: Optional[str] = None
    instance_name: str = ""
    heartbeat_source_id: Optional[str] = None
    default_timezone: Optional[str] = None


class ScheduleNotFound(LookupError):
    """An absent schedule or one outside the command person's team and ownership."""


class ScheduleOperations:
    """Answers schedule operations for one team (module docstring)."""

    def __init__(
        self,
        store: ScheduleStore,
        people: PeopleDirectory,
        delivery_refs: Optional[DeliveryRefs],
        may_change_system_schedules: Callable[[CommandPerson], bool],
        clock: Callable[[], datetime],
        logger: logging.Logger,
        *,
        blobs: Any = None,
        on_schedule_installed: Optional[Callable[[str], Any]] = None,
        conversations: Any = None,
        install_defaults: Optional[ScheduleInstallDefaults] = None,
    ) -> None:
        self._store = store
        self._people = people
        self._delivery_refs = delivery_refs
        self._may_change_system = may_change_system_schedules
        self._clock = clock
        self._logger = logger
        self._blobs = blobs
        self._on_schedule_installed = on_schedule_installed
        self._conversations = conversations
        self._install_defaults = install_defaults or ScheduleInstallDefaults()

    def family(self) -> OperationFamily:
        return OperationFamily({kind: self.handle for kind in SCHEDULE_KINDS}, frozenset(SCHEDULE_KINDS),
                               self.purge_outcomes, self.close)

    async def purge_outcomes(self) -> None:
        await asyncio.to_thread(self._store.command_outcomes.purge_command_outcomes, self._clock())

    def close(self) -> None:
        if isinstance(self._may_change_system, SystemScheduleCheck):
            self._may_change_system.close()

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        resolved_instruction: Optional[str] = None
        if verified.frame.kind == "schedules.install":
            body = verified.raw["body"]
            action_spec = body.get("action")
            if isinstance(action_spec, dict) and action_spec.get("type") == "task":
                instruction_content = action_spec.get("instruction")
                if isinstance(instruction_content, dict) and instruction_content.get("type") == "blob":
                    try:
                        resolved_instruction = await self._decode_blob_instruction(instruction_content)
                    except (BlobCorrupted, BlobTooLarge) as exc:
                        return _message("invalid_request", str(exc))
                    except KnowledgeBlobUnavailable as exc:
                        return _message("runtime_error", str(exc))
        cancelled = Event()
        running = asyncio.create_task(
            asyncio.to_thread(
                self.answer,
                verified,
                cancelled=cancelled,
                resolved_instruction=resolved_instruction,
            )
        )
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

    async def _decode_blob_instruction(self, content: dict[str, Any]) -> str:
        from team_services.knowledge import _as_blob_store

        blob_store = _as_blob_store(self._blobs)
        if blob_store is None:
            raise KnowledgeBlobUnavailable("The team's blob store is not available to read this schedule instruction.")
        return await decode_text_content(content, blob_store, max_chars=_INSTRUCTION_MAX_CHARS)

    def answer(
        self,
        verified: VerifiedFrame,
        *,
        cancelled: Optional[Event] = None,
        resolved_instruction: Optional[str] = None,
    ) -> dict[str, Any]:
        action = verified.frame.kind.removeprefix("schedules.")
        body = verified.raw["body"]
        if action == "install":
            return self._answer_install(verified, body, cancelled=cancelled, resolved_instruction=resolved_instruction)
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

    # -- install ----------------------------------------------------------------------

    def _answer_install(
        self,
        verified: VerifiedFrame,
        body: dict[str, Any],
        *,
        cancelled: Optional[Event],
        resolved_instruction: Optional[str],
    ) -> dict[str, Any]:
        actor = schedule_install_actor(verified, self._people, self._logger)
        once = None
        if actor.person_id is not None:
            outcome = CommandOutcome(
                team_id=self._people.team_id,
                person_id=actor.person_id,
                retry_key=str(verified.raw["idempotency_key"]),
                kind=verified.frame.kind,
                body_digest=str(verified.raw["body_digest"]),
                now=self._clock(),
                reply_for=lambda write: _checked_install_reply(write.result),
                logger=self._logger,
            )
            once = CommandOnce(
                self._store.command_outcomes,
                outcome,
                reply_from_receipt=lambda reply: reply,
                key_taken=lambda error: _message("not_allowed", str(error)),
                not_stored=_message(
                    "runtime_error",
                    "The schedule install could not be answered. Check the schedule before sending it again.",
                ),
                logger=self._logger,
            )
            stored = once.stored_reply()
            if stored is not None:
                return stored
        person = actor.person
        if person is None:
            reply = _message("not_allowed", actor.reason)
            return once.final(reply) if once is not None else reply
        assert once is not None
        captured: list[ScheduleInstallResult] = []
        reply = once.run(
            lambda: self._install(
                actor,
                body,
                once.outcome,
                cancelled,
                resolved_instruction=resolved_instruction,
                captured=captured,
            ),
            refusal_for=lambda error: self._install_refusal(person, body, error),
        )
        if captured:
            result = captured[-1]
            if result.state == SCHEDULE_REFRESHED and result.previous is not None and result.saved is not None:
                follow_authorized_change(
                    self._store.worker_records,
                    person.team_id,
                    result.previous,
                    result.saved,
                    source="gateway_context_refresh",
                )
            if (
                result.state in (SCHEDULE_INSTALLED, SCHEDULE_REPLACED, SCHEDULE_REFRESHED)
                and self._on_schedule_installed is not None
            ):
                try:
                    self._on_schedule_installed(result.schedule_id)
                except Exception:
                    self._logger.exception(
                        "Could not issue an initial run permit right after schedules.install; upkeep will retry. "
                        "event=team_schedule_initial_permit_failed schedule_id=%s",
                        result.schedule_id,
                    )
        return reply

    def _check_permit_source(self, actor: InstallActorCheck, now: datetime) -> None:
        if actor.permit_schedule_id is None or actor.person is None:
            return
        source = self._store.read_scheduled_message(actor.permit_schedule_id)
        if (
            source is None
            or source.claim_team_id != actor.person.team_id
            or owner_canonical_user_id_for_schedule(source) != actor.person.person_id
            or schedule_stopped_reason(source, now) is not None
            or not (self._store.is_owner_vouched(actor.permit_schedule_id) or is_system_managed_schedule(source))
        ):
            raise ScheduleInstallRefused(
                "not_allowed",
                "The schedule permit backing this task is no longer active or vouched.",
            )

    def _authorize_install(
        self,
        actor: InstallActorCheck,
        body: dict[str, Any],
        management_class: ScheduleManagementClass,
        system_kind: Optional[SystemScheduleKind],
    ) -> bool:
        """Check system-schedule installation authority; returns whether team-owner takeover is allowed."""

        assert actor.person is not None
        person = actor.person
        if management_class != ScheduleManagementClass.SYSTEM:
            return False
        mode = str(body["mode"])
        spec = system_schedule_spec(system_kind) if system_kind is not None else None
        if actor.from_gateway:
            if mode in ("ensure", "refresh") and spec is not None and spec.scope is SystemScheduleScope.PERSON:
                return False
            if not self._may_change_system(person):
                raise ScheduleInstallRefused("not_allowed", SYSTEM_NOT_ALLOWED_TEXT)
            return bool(spec is not None and spec.scope is SystemScheduleScope.TEAM)
        if system_kind in _AGENT_PERSON_SYSTEM_KINDS:
            if system_kind == SystemScheduleKind.OBJECTIVE_WORK_SESSION and not body.get("objective_id"):
                raise ScheduleInstallRefused(
                    "invalid_request", "An objective_work_session schedule requires objective_id."
                )
            return False
        if not self._may_change_system(person):
            raise ScheduleInstallRefused("not_allowed", SYSTEM_NOT_ALLOWED_TEXT)
        return False

    def _build_schedule_from_install(
        self,
        actor: InstallActorCheck,
        body: dict[str, Any],
        *,
        resolved_instruction: Optional[str],
    ) -> tuple[ScheduledMessage, Optional[str]]:
        assert actor.person is not None
        person = actor.person
        schedule_id = str(body["schedule_id"])
        management_class = ScheduleManagementClass(body["management_class"])
        system_kind: Optional[SystemScheduleKind] = None
        if management_class == ScheduleManagementClass.SYSTEM:
            system_kind = SystemScheduleKind(body.get("system_schedule_kind") or "unclassified")

        action_spec = body["action"]
        if action_spec["type"] == "fixed_message":
            raise ScheduleInstallRefused(
                "invalid_request",
                "Fixed-message schedules without a model turn are not supported by this runtime yet.",
            )
        instruction_content = action_spec["instruction"]
        if instruction_content["type"] == "inline":
            instruction = str(instruction_content["text"])
        else:
            if resolved_instruction is None:
                raise ScheduleInstallRefused(
                    "invalid_request", "Blob-backed schedule instructions must be resolved before saving."
                )
            instruction = resolved_instruction
        if not instruction.strip():
            raise ScheduleInstallRefused("invalid_request", "Schedule instruction must not be empty.")

        timing = body["timing"]
        timing_kind = timing["kind"]
        interval_ms: Optional[int] = None
        cron_expression: Optional[str] = None
        cron_timezone: Optional[str] = None
        next_fire_at: Optional[str] = None
        now = self._clock().astimezone(timezone.utc)
        if timing_kind == "interval":
            interval_ms = int(timing["interval_ms"])
            try:
                next_fire_at = compute_next_fire_at(interval_ms=interval_ms, from_time=now)
            except (OverflowError, ValueError) as exc:
                raise ScheduleInstallRefused("invalid_request", f"Invalid interval timing: {exc}") from exc
        elif timing_kind == "cron":
            cron_expression = str(timing["expression"])
            cron_timezone = str(timing["time_zone"])
            try:
                ZoneInfo(cron_timezone)
                next_fire_at = compute_next_fire_at(
                    cron_expression=cron_expression,
                    cron_timezone=cron_timezone,
                    from_time=now,
                )
            except (ZoneInfoNotFoundError, ValueError, KeyError, OverflowError) as exc:
                raise ScheduleInstallRefused("invalid_request", f"Invalid cron schedule or time zone: {exc}") from exc
        elif timing_kind == "once":
            try:
                next_fire_at = parse_timestamp(str(timing["at"])).isoformat()
            except ValueError as exc:
                raise ScheduleInstallRefused("invalid_request", f"Invalid one-time timestamp: {exc}") from exc
        else:
            raise ScheduleInstallRefused("invalid_request", f"Unsupported timing kind: {timing_kind!r}")

        raw_repeat = body.get("repeat_count")
        if timing_kind == "once":
            if raw_repeat is not None and int(raw_repeat) not in (0, 1):
                raise ScheduleInstallRefused("invalid_request", "A one-time schedule runs once (repeat_count 0 or 1).")
            repeat_count: Optional[int] = 1
        else:
            repeat_count = None if raw_repeat is None or int(raw_repeat) == 0 else int(raw_repeat)

        expires_at_utc = parse_timestamp(str(body["expires_at"])).isoformat() if "expires_at" in body else None
        paused_until = parse_timestamp(str(body["paused_until"])).isoformat() if "paused_until" in body else None

        worker = body.get("worker") or {}
        agent_type = worker.get("agent_type")
        agent_id = worker.get("agent_id")
        if not agent_type and not agent_id:
            spec = system_schedule_spec(system_kind) if system_kind is not None else None
            agent_type = (spec.agent_type if spec is not None and spec.agent_type else None) or self._install_defaults.lead_agent_type

        deliver_to = body.get("deliver_to")
        delivery_ref = str(deliver_to["delivery_ref"]) if isinstance(deliver_to, dict) and deliver_to.get("delivery_ref") else None
        conversation_id = str(deliver_to["conversation_id"]) if isinstance(deliver_to, dict) and deliver_to.get("conversation_id") else None
        followup_to_task_id: Optional[str] = None
        if conversation_id is None and self._conversations is not None:
            seed_task_id = schedule_conversation_seed_task_id(schedule_id)
            existing_seed = self._conversations.get_turn_by_task_id(seed_task_id)
            if existing_seed and existing_seed.get("conversation_id"):
                conversation_id = str(existing_seed["conversation_id"])
                followup_to_task_id = seed_task_id
        schedule_name = str(body["name"]) if "name" in body and body["name"] != "" else None
        if system_kind == SystemScheduleKind.ISOLATED_RECOVERY_CHECKPOINT and schedule_name:
            parts = schedule_name.split(":", 2)
            if len(parts) == 3 and parts[1]:
                followup_to_task_id = parts[1]
        request_timezone = (
            (str(deliver_to["time_zone"]) if isinstance(deliver_to, dict) and deliver_to.get("time_zone") else None)
            or cron_timezone
            or self._install_defaults.default_timezone
        )

        schedule_memory = _parse_run_notes(body.get("run_notes"))
        delivery_mode = _delivery_mode_for(management_class, system_kind)
        heartbeat_source_id = (
            self._install_defaults.heartbeat_source_id
            if system_kind == SystemScheduleKind.HEARTBEAT_LAUNCHER
            else None
        )
        is_response_requested = (
            management_class == ScheduleManagementClass.USER
            or system_kind in (SystemScheduleKind.HEARTBEAT_LAUNCHER, SystemScheduleKind.ISOLATED_RECOVERY_CHECKPOINT)
        )
        response_route = self._install_defaults.response_route
        payload_header = MessageHeader(
            from_agent_id=response_route or "team_services",
            to_routing_path=agent_id or RoutingType.SELF_ROUTE.value,
            response_routing_key=response_route,
            agent_signature="",
        )
        payload_meta = MessageMeta(
            from_agent_name=(
                f"{self._install_defaults.instance_name}_gateway"
                if actor.from_gateway and self._install_defaults.instance_name
                else "team_services"
            ),
            conversation_id=conversation_id,
            followup_to_task_id=followup_to_task_id,
            objective_id=body.get("objective_id"),
            request_timezone=request_timezone,
            requester_type=RequesterType.TIMER_REQUESTER,
            is_response_requested=is_response_requested,
            to_named_agent_type=agent_type if agent_type and agent_type != self._install_defaults.lead_agent_type else None,
            gateway_context=GatewayContext(
                gateway_instance_name=self._install_defaults.instance_name or None,
                canonical_user_id=person.person_id,
                delivery_mode=delivery_mode,
                scheduled_job_id=schedule_id,
                heartbeat_source_id=heartbeat_source_id,
            ),
        )
        schedule = ScheduledMessage(
            schedule_id=schedule_id,
            schedule_name=schedule_name,
            management_class=management_class,
            system_schedule_kind=system_kind,
            objective_id=body.get("objective_id"),
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement=instruction.strip(),
            ),
            payload_header=payload_header,
            payload_meta=payload_meta,
            interval_ms=interval_ms,
            cron_expression=cron_expression,
            cron_timezone=cron_timezone,
            repeat_count=repeat_count,
            next_fire_at=next_fire_at,
            paused_until=paused_until,
            expires_at_utc=expires_at_utc,
            enabled=bool(body["enabled"]),
            schedule_memory=schedule_memory,
            claim_team_id=person.team_id,
            claim_named_agent_type=agent_type,
            claim_agent_id=agent_id,
        )
        return schedule, delivery_ref

    def _install(
        self,
        actor: InstallActorCheck,
        body: dict[str, Any],
        outcome: CommandOutcome,
        cancelled: Optional[Event],
        *,
        resolved_instruction: Optional[str],
        captured: list[ScheduleInstallResult],
    ) -> dict[str, Any]:
        assert actor.person is not None
        person = actor.person
        now = self._clock().astimezone(timezone.utc)
        self._check_permit_source(actor, now)
        schedule, delivery_ref = self._build_schedule_from_install(
            actor, body, resolved_instruction=resolved_instruction
        )
        allow_team_owner_change = self._authorize_install(
            actor, body, schedule.management_class, schedule.system_schedule_kind
        )
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError("The schedule install was cancelled before its write.")

        def _check_existing(current: ScheduledMessage) -> None:
            if not is_system_managed_schedule(current):
                return
            if actor.from_gateway:
                spec = system_schedule_spec(current.effective_system_schedule_kind)
                if body["mode"] == "refresh" and spec is not None and spec.scope is SystemScheduleScope.PERSON:
                    return
                if not self._may_change_system(person):
                    raise ScheduleInstallRefused("not_allowed", SYSTEM_NOT_ALLOWED_TEXT)
                return
            if current.effective_system_schedule_kind in _AGENT_PERSON_SYSTEM_KINDS:
                if (
                    current.effective_system_schedule_kind == SystemScheduleKind.OBJECTIVE_WORK_SESSION
                    and current.objective_id != schedule.objective_id
                ):
                    raise ScheduleInstallRefused(
                        "not_allowed",
                        "The existing schedule id is not the protected schedule for this Objective.",
                    )
                return
            if not self._may_change_system(person):
                raise ScheduleInstallRefused("not_allowed", SYSTEM_NOT_ALLOWED_TEXT)

        def _permit_for(current: ScheduledMessage, candidate: ScheduledMessage) -> Any:
            if body["mode"] == "refresh":
                return build_schedule_mutation_permit(
                    schedule=current,
                    operation=ScheduleMutationOperation.UPDATE,
                    authority_mode=ScheduleMutationAuthorityMode.TRUSTED_RUNTIME,
                    actor_principal_id=person.person_id,
                    owner_canonical_user_id=owner_canonical_user_id_for_schedule(current) or person.person_id,
                    team_id=person.team_id,
                    candidate=candidate,
                )
            if not actor.from_gateway:
                return build_schedule_mutation_permit(
                    schedule=current,
                    operation=ScheduleMutationOperation.UPDATE,
                    authority_mode=ScheduleMutationAuthorityMode.HELD_ACCESS,
                    actor_principal_id=str(actor.agent_id or person.person_id),
                    owner_canonical_user_id=owner_canonical_user_id_for_schedule(current) or person.person_id,
                    team_id=person.team_id,
                    candidate=candidate,
                )
            return person_schedule_permit(
                current,
                person_id=person.person_id,
                team_id=person.team_id,
                operation=ScheduleMutationOperation.UPDATE,
                candidate=candidate,
            )

        replies: list[dict[str, Any]] = []
        result = self._store.install(
            schedule,
            mode=str(body["mode"]),
            team_id=person.team_id,
            owner_id=person.person_id,
            owner_vouched=True,
            delivery_ref=delivery_ref,
            expected_record_digest=body.get("seen_version"),
            require_seen_version_on_update=actor.from_agent,
            allow_team_owner_change=allow_team_owner_change,
            check_existing=_check_existing,
            system_mutation_permit_fn=_permit_for,
            command_outcome=_capturing(outcome, replies),
        )
        captured.append(result)
        self._logger.info(
            "Installed schedule via schedules.install. event=team_schedule_installed schedule_id=%s "
            "mode=%s state=%s person=%s from_gateway=%s",
            result.schedule_id,
            body["mode"],
            result.state,
            person.person_id,
            actor.from_gateway,
        )
        return replies[-1]

    def _install_refusal(self, person: CommandPerson, body: dict[str, Any], error: Exception) -> dict[str, Any]:
        if isinstance(error, ScheduleInstallRefused):
            if error.outcome == "changed_since_read" and error.current_version:
                return {
                    "outcome": "changed_since_read",
                    "current_version": error.current_version,
                    "message": str(error),
                }
            return _message(error.outcome, str(error))
        if isinstance(error, (ScheduleWriteConflict, SystemScheduleMutationDenied)):
            try:
                current = self._store.read_scheduled_message(str(body["schedule_id"]))
            except Exception as failure:
                return self._install_refusal(person, body, failure)
            if current is None or current.claim_team_id != person.team_id or not schedule_owned_by(
                current, person_id=person.person_id, action=PRIVATE_READ
            ):
                return _message("not_found", NOT_FOUND_TEXT)
            return {
                "outcome": "changed_since_read",
                "current_version": schedule_record_digest(current),
                "message": CHANGED_TEXT,
            }
        if isinstance(error, PermissionError):
            return _message("not_allowed", str(error))
        if isinstance(error, ValueError):
            return _message("invalid_request", str(error))
        self._logger.error(
            "A schedule install failed. event=team_schedule_install_failed schedule_id=%s mode=%s "
            "person=%s error_type=%s",
            body.get("schedule_id"),
            body.get("mode"),
            person.person_id,
            type(error).__name__,
            exc_info=error,
        )
        return _message("runtime_error", "The schedule was not installed.")

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
                stored_ref = self._store.delivery_ref_of(schedule.schedule_id)
                if stored_ref == wanted:
                    rows.append(schedule)
                    continue
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


def _checked_install_reply(result: ScheduleInstallResult) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "outcome": "ok",
        "state": result.state,
        "schedule_id": result.schedule_id,
        "timing_revision": int(result.timing_revision),
        "version": (
            {"state": "available", "seen_version": result.seen_version}
            if result.seen_version
            else {"state": "unavailable"}
        ),
    }
    if result.next_fire_at:
        reply["next_fire_at"] = _timestamp(result.next_fire_at)
    body_model("schedules.install.reply")[0].validate_python(reply)
    return reply


def _parse_json_object(raw: Optional[str], *, field_name: str) -> dict[str, Any]:
    if raw is None or not str(raw).strip():
        return {}
    try:
        parsed = json.loads(str(raw))
    except ValueError as exc:
        raise ScheduleInstallRefused("invalid_request", f"{field_name} must be valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ScheduleInstallRefused("invalid_request", f"{field_name} must be a JSON object.")
    return parsed


def _parse_run_notes(raw: Any) -> Optional[ScheduleTaskMemory]:
    if not isinstance(raw, dict):
        return None
    policy_raw = raw.get("policy") or {}
    state_raw = raw.get("state") or {}
    policy = ScheduleMemoryPolicy(
        notify=policy_raw.get("notify", "on_change"),
        notify_on_failure=bool(policy_raw.get("notify_on_failure", False)),
        evidence_required=str(policy_raw.get("evidence_required") or ""),
        custom=_parse_json_object(policy_raw.get("custom_json"), field_name="run_notes.policy.custom_json"),
    )
    state = ScheduleMemoryState(
        summary=str(state_raw.get("summary") or ""),
        confidence=str(state_raw.get("confidence") or "unknown"),
        last_observed_at=state_raw.get("last_observed_at"),
        last_changed_at=state_raw.get("last_changed_at"),
        domain=_parse_json_object(state_raw.get("domain_json"), field_name="run_notes.state.domain_json"),
    )
    return ScheduleTaskMemory(
        schedule_kind=str(raw.get("schedule_kind") or ""),
        policy=policy,
        state=state,
        domain_schema_hint=_parse_json_object(
            raw.get("domain_schema_hint_json"), field_name="run_notes.domain_schema_hint_json"
        ),
    )


def _delivery_mode_for(
    management_class: ScheduleManagementClass,
    system_kind: Optional[SystemScheduleKind],
) -> str:
    if management_class == ScheduleManagementClass.SYSTEM and system_kind is not None:
        spec = system_schedule_spec(system_kind)
        if spec is not None and spec.delivery_mode:
            return str(spec.delivery_mode)
    return "cron"


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

"""Gateway application for manager-owned maintenance schedule installation.

The gateway installs the runtime's maintenance schedules and signs their
context for the person they belong to
(``GatewayRequestContextService.build_gateway_context``). That context also
says where to reach the person (their chat session), so at each gateway start
and whenever the manager becomes available again the gateway sends a
``refresh`` for each schedule: the manager replaces only the stored person and
session fields and keeps pause, enabled state, cadence and next fire time
(``custom_agents.schedule_context_refresh``). Scheduled runs take the owner's
roles from the gateway's copy of the team's people instead
(``access_control.people_directory``), read at every run.

- Specialist schedules (``GatewaySpecialistScheduleSettings``; today memory
  review): the system schedules a local specialist runs, installed here with
  ``ensure`` (created once, never replaced, so an operator's pause or disable
  survives restarts) and then refreshed. Team schedules (the core-facts
  audit) use the team's proactive session. Per-person schedules (the
  personal-facts audit and the stale-conversation review) are installed once
  for each active person, owned by them and delivered to their own chat
  (``assistant_gateway.application.person_maintenance``), because a run works
  for its owner and reviews only that person's facts and conversations
  (``custom_agents.memory_review_runtime``). ``ensure_all`` installs every
  active person's; ``ensure_person_schedules`` installs one person's when they
  are added, re-enabled or restored. A departing person's are deleted by the
  manager with their other schedules.
- Internal-improvement maintenance: created by the manager when an
  InternalImprovementAgent starts or memory-review cleanup is pending
  (``custom_agents.internal_improvement_runtime``); the gateway only refreshes
  it, so its escalations reach the schedule's owner with current roles.
- Heartbeat launcher: installed once with ``ensure`` and then refreshed.

Which agent type runs a schedule, its runs' delivery mode, the name prefix of
a per-person schedule and whether a kind is per-person come from the system
schedule table (``custom_messages.system_schedules``); the settings give only
what is configured: ids, cadence, instructions and whether a set is on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Literal

from assistant_gateway.application.delivery_service import GatewayDeliveryService
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.person_maintenance import (
    GatewayPersonMaintenanceSessions,
)
from assistant_gateway.application.ports import GatewayIdGenerator
from assistant_gateway.application.proactive_delivery import (
    GatewayProactiveSessionResolver,
)
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    ScheduleManagementClass,
    SystemScheduleKind,
)
from custom_messages.system_schedules import (
    SystemScheduleKindSpec,
    SystemScheduleScope,
    person_system_schedule_id,
    person_system_schedule_name,
    system_schedule_spec,
)


@dataclass(frozen=True)
class GatewayHeartbeatScheduleSettings:
    """Stable inputs for the manager-owned heartbeat launcher schedule."""

    enabled: bool
    source_id: str
    schedule_id: str
    interval_seconds: int


def _spec(kind: SystemScheduleKind) -> SystemScheduleKindSpec:
    spec = system_schedule_spec(kind)
    if spec is None:
        raise ValueError(f"{kind} has no row in the system schedule table")
    return spec


def _require_specialist_kind(kind: SystemScheduleKind, *, per_person: bool) -> None:
    """Refuse a kind the table does not let a local specialist run, or of the wrong scope."""

    spec = _spec(kind)
    if not (spec.local_worker and spec.agent_type and spec.delivery_mode and spec.name_prefix):
        raise ValueError(f"{spec.kind} is not a system schedule a local specialist runs")
    if (spec.scope is SystemScheduleScope.PERSON) is not per_person:
        raise ValueError(f"{spec.kind} is a {spec.scope} schedule in the system schedule table")


@dataclass(frozen=True)
class GatewaySystemScheduleDefinition:
    """One protected system schedule executed by a local specialist (a team one, or one person's)."""

    schedule_id: str
    schedule_name: str
    interval_seconds: int
    instruction: str
    system_schedule_kind: SystemScheduleKind


@dataclass(frozen=True)
class GatewayPersonSystemScheduleDefinition:
    """One system schedule installed once per active person (module docstring).

    ``for_person`` gives the person's own schedule: its id from
    ``schedule_id_prefix``, its name from the kind's ``name_prefix`` in the
    system schedule table.
    """

    schedule_id_prefix: str
    interval_seconds: int
    instruction: str
    system_schedule_kind: SystemScheduleKind

    def for_person(self, instance_name: str, person_id: str) -> GatewaySystemScheduleDefinition:
        name_prefix = str(_spec(self.system_schedule_kind).name_prefix)
        return GatewaySystemScheduleDefinition(
            schedule_id=person_system_schedule_id(self.schedule_id_prefix, instance_name, person_id),
            schedule_name=person_system_schedule_name(name_prefix, instance_name, person_id),
            interval_seconds=self.interval_seconds,
            instruction=self.instruction,
            system_schedule_kind=self.system_schedule_kind,
        )


@dataclass(frozen=True)
class GatewaySpecialistScheduleSettings:
    """The system schedules one local specialist runs, turned on and off together (memory review).

    ``name`` names them in log messages and events ("memory review" gives
    ``event=gateway_memory_review_schedule_disabled``). ``instruction_frame``
    is the stored task text, with ``{schedule_id}`` and ``{instruction}``
    filled in. ``definitions`` are team schedules (the coremem audit);
    ``person_definitions`` are installed once for each active person. Every
    kind must be one the table says a local worker runs, and a per-person
    definition's kind must be per-person (a team one's must not), so a wrong
    setting fails when the gateway starts.
    """

    name: str
    enabled: bool
    instruction_frame: str
    definitions: tuple[GatewaySystemScheduleDefinition, ...]
    person_definitions: tuple[GatewayPersonSystemScheduleDefinition, ...] = ()

    def __post_init__(self) -> None:
        for definition in self.definitions:
            _require_specialist_kind(definition.system_schedule_kind, per_person=False)
        for person_definition in self.person_definitions:
            _require_specialist_kind(person_definition.system_schedule_kind, per_person=True)

    @property
    def event_name(self) -> str:
        return self.name.replace(" ", "_")


@dataclass(frozen=True)
class GatewayInternalImprovementScheduleSettings:
    """The team's internal-improvement maintenance schedule, which the gateway refreshes."""

    schedule_id: str
    schedule_name: str
    interval_seconds: int


@dataclass(frozen=True)
class GatewayMaintenanceScheduleSettings:
    """Gateway identity and maintenance schedule configuration."""

    instance_name: str
    response_route: str
    user_display_name: str
    request_timezone: str
    default_owner_canonical_user_id: str
    heartbeat: GatewayHeartbeatScheduleSettings
    specialist_schedules: tuple[GatewaySpecialistScheduleSettings, ...]
    internal_improvement: GatewayInternalImprovementScheduleSettings


@dataclass(frozen=True)
class _MaintenanceScheduleDefinition:
    schedule_id: str
    schedule_name: str
    interval_seconds: int
    instruction: str
    delivery_mode: str
    system_schedule_kind: SystemScheduleKind
    response_requested: bool
    target_agent_type: str | None = None
    install_mode: Literal["replace", "ensure", "refresh"] = "replace"


class GatewayMaintenanceScheduleApplication:
    """Own construction and durable installation of maintenance schedules."""

    def __init__(
        self,
        *,
        settings: GatewayMaintenanceScheduleSettings,
        proactive_sessions: GatewayProactiveSessionResolver,
        person_sessions: GatewayPersonMaintenanceSessions,
        request_context: GatewayRequestContextService,
        delivery: GatewayDeliveryService,
        manager: GatewayManagerClient,
        id_generator: GatewayIdGenerator,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._proactive_sessions = proactive_sessions
        self._person_sessions = person_sessions
        self._request_context = request_context
        self._delivery = delivery
        self._manager = manager
        self._id_generator = id_generator
        self._logger = logger or logging.getLogger(__name__)

    async def ensure_all(self) -> None:
        """Ensure every enabled manager-owned maintenance schedule."""

        await self.ensure_heartbeat()
        await self.ensure_specialist_schedules()
        await self.refresh_internal_improvement()

    async def ensure_heartbeat(self) -> None:
        """Install the heartbeat launcher when enabled and routable."""

        heartbeat = self._settings.heartbeat
        if not heartbeat.enabled:
            return
        if self._manager.manager_id is None:
            self._logger.info(
                "Gateway skipped heartbeat schedule installation because the "
                "manager is not currently available. "
                "event=gateway_heartbeat_schedule_manager_unavailable"
            )
            return
        definition = _MaintenanceScheduleDefinition(
            schedule_id=heartbeat.schedule_id,
            schedule_name=f"heartbeat:{self._settings.instance_name}",
            interval_seconds=max(1, int(heartbeat.interval_seconds)),
            instruction=(
                f"Heartbeat launcher tick for source_id={heartbeat.source_id}. "
                "The runtime will expand enabled heartbeat items into "
                "independent top-level tasks."
            ),
            delivery_mode=str(_spec(SystemScheduleKind.HEARTBEAT_LAUNCHER).delivery_mode),
            system_schedule_kind=SystemScheduleKind.HEARTBEAT_LAUNCHER,
            response_requested=True,
            install_mode="ensure",
        )
        target = self._team_target()
        await self._publish(definition, *target)
        await self._publish(replace(definition, install_mode="refresh"), *target)

    async def ensure_specialist_schedules(self) -> None:
        """Install every specialist schedule set: its team schedules and every active person's.

        Each schedule is installed with ``ensure`` and then refreshed (see
        ``_install_specialist_schedule``). One person's failure is logged and
        does not stop the others; a set that is off or cannot be installed now
        is logged and skipped.
        """

        for schedules in self._settings.specialist_schedules:
            if not self._ready(schedules, event=f"gateway_{schedules.event_name}_schedule_manager_unavailable"):
                continue
            session, owner = self._team_target()
            for definition in schedules.definitions:
                await self._install_specialist_schedule(schedules, definition, session, owner)
            if not schedules.person_definitions:
                continue
            for person_id in self._person_sessions.active_person_ids():
                await self._install_person_schedules_logged(schedules, person_id)

    async def ensure_person_schedules(self, person_id: str) -> None:
        """Install one active person's per-person system schedules; safe to call again.

        For when a person is added, enabled again, added back or restored
        (``GatewayPersonSchedules`` port). For each set it does nothing, logged,
        when the set is off, the manager is unavailable (the next
        ``ensure_all`` installs them) or the person is not active. A failure
        is logged and never raised to the caller.
        """

        for schedules in self._settings.specialist_schedules:
            if not schedules.person_definitions:
                continue
            if not self._ready(schedules, event=f"gateway_person_{schedules.event_name}_manager_unavailable"):
                continue
            await self._install_person_schedules_logged(schedules, person_id, check_active=True)

    def _ready(self, schedules: GatewaySpecialistScheduleSettings, *, event: str) -> bool:
        """Whether a set is on and the manager can take schedules now (logged when not)."""

        if not schedules.enabled:
            self._logger.info(
                "Gateway did not install %s schedules because %s is off. event=gateway_%s_schedule_disabled",
                schedules.name,
                schedules.name,
                schedules.event_name,
            )
            return False
        if self._manager.manager_id is None:
            self._logger.info(
                "Gateway skipped %s schedule installation because "
                "the manager is not currently available. event=%s",
                schedules.name,
                event,
            )
            return False
        return True

    async def _install_person_schedules_logged(
        self, schedules: GatewaySpecialistScheduleSettings, person_id: str, *, check_active: bool = False
    ) -> None:
        try:
            await self._install_person_schedules(schedules, person_id, check_active=check_active)
        except Exception:
            self._logger.exception(
                "Gateway could not install a person's %s schedules; the next gateway "
                "start or manager return tries again. event=gateway_person_%s_failed "
                "person_id=%s",
                schedules.name,
                schedules.event_name,
                person_id,
            )

    async def _install_person_schedules(
        self, schedules: GatewaySpecialistScheduleSettings, person_id: str, *, check_active: bool
    ) -> None:
        if check_active and not self._person_sessions.is_active(person_id):
            self._logger.info(
                "Gateway did not install per-person %s schedules for a person who is not "
                "active in the team. event=gateway_person_%s_not_active person_id=%s",
                schedules.name,
                schedules.event_name,
                person_id,
            )
            return
        target = self._person_sessions.resolve(person_id, manager_agent_id=self._manager.manager_id)
        for definition in schedules.person_definitions:
            await self._install_specialist_schedule(
                schedules,
                definition.for_person(self._settings.instance_name, person_id),
                target.session,
                person_id,
            )
        self._logger.info(
            "Gateway sent a person's %s schedules. event=gateway_person_%s_sent "
            "person_id=%s session_key=%s route=%s channel_name=%s",
            schedules.name,
            schedules.event_name,
            person_id,
            target.session.session_key,
            target.route_source.value,
            target.session.channel_name or "-",
        )

    async def _install_specialist_schedule(
        self,
        schedules: GatewaySpecialistScheduleSettings,
        definition: GatewaySystemScheduleDefinition,
        session: SessionRecord,
        owner_canonical_user_id: str,
    ) -> None:
        """Install one specialist schedule once, then refresh its person and session.

        ``ensure`` creates a missing schedule and never replaces a stored one,
        so an operator's pause, disable or cadence survives gateway restarts;
        the following ``refresh`` updates only the stored person and session
        (module docstring). Configured intervals and instructions therefore
        apply to new installs only. An interval of zero or less turns the
        schedule's installation off. The agent type that claims it and its
        runs' delivery mode come from the system schedule table.
        """

        if definition.interval_seconds <= 0:
            return
        spec = _spec(definition.system_schedule_kind)
        schedule = _MaintenanceScheduleDefinition(
            schedule_id=definition.schedule_id,
            schedule_name=definition.schedule_name,
            interval_seconds=definition.interval_seconds,
            instruction=schedules.instruction_frame.format(
                schedule_id=definition.schedule_id, instruction=definition.instruction
            ),
            delivery_mode=str(spec.delivery_mode),
            target_agent_type=spec.agent_type,
            system_schedule_kind=definition.system_schedule_kind,
            response_requested=False,
        )
        await self._publish(replace(schedule, install_mode="ensure"), session, owner_canonical_user_id)
        await self._publish(replace(schedule, install_mode="refresh"), session, owner_canonical_user_id)

    async def refresh_internal_improvement(self) -> None:
        """Refresh the person and roles on the team's internal-improvement maintenance schedule.

        The manager creates this schedule; the refresh changes nothing when it
        does not exist yet (module docstring). With the schedule's installation
        turned off (interval zero or less) there is nothing to refresh.
        """

        schedule = self._settings.internal_improvement
        if schedule.interval_seconds <= 0:
            return
        maintenance = _spec(SystemScheduleKind.INTERNAL_IMPROVEMENT)
        if self._manager.manager_id is None:
            self._logger.info(
                "Gateway skipped the internal improvement schedule refresh because "
                "the manager is not currently available. "
                "event=gateway_internal_improvement_schedule_manager_unavailable"
            )
            return
        await self._publish(
            _MaintenanceScheduleDefinition(
                schedule_id=schedule.schedule_id,
                schedule_name=schedule.schedule_name,
                interval_seconds=schedule.interval_seconds,
                instruction="Refresh the person and roles of the internal improvement maintenance schedule.",
                delivery_mode=str(maintenance.delivery_mode),
                target_agent_type=maintenance.agent_type,
                system_schedule_kind=SystemScheduleKind.INTERNAL_IMPROVEMENT,
                response_requested=False,
                install_mode="refresh",
            ),
            *self._team_target(),
        )
        self._logger.info(
            "Gateway sent the internal improvement schedule refresh. "
            "event=gateway_internal_improvement_schedule_refresh_sent schedule_id=%s",
            schedule.schedule_id,
        )

    def _team_target(self) -> tuple[SessionRecord, str]:
        """The team schedules' session (the proactive one) and owner (the first user)."""

        session = self._proactive_sessions.resolve_session()
        return session, session.owner_canonical_user_id or self._settings.default_owner_canonical_user_id

    async def _publish(
        self,
        definition: _MaintenanceScheduleDefinition,
        session: SessionRecord,
        owner_canonical_user_id: str,
    ) -> None:
        """Send one schedule to the manager, for this owner, delivered through this session."""

        manager_id = self._manager.manager_id
        if manager_id is None:
            raise RuntimeError("The manager route became unavailable.")
        correlation_id = self._id_generator.new_correlation_id()
        payload_header = self._manager_header(manager_id)
        payload_meta = self._message_meta(
            session=session,
            manager_id=manager_id,
            owner_canonical_user_id=owner_canonical_user_id,
            correlation_id=correlation_id,
            definition=definition,
            requester_type=RequesterType.TIMER_REQUESTER,
            response_requested=definition.response_requested,
        )
        if definition.target_agent_type:
            payload_meta.to_named_agent_type = definition.target_agent_type
            payload_meta.to_agent_name = None
        schedule_message = ScheduledMessage(
            schedule_id=definition.schedule_id,
            schedule_name=definition.schedule_name,
            claim_named_agent_type=definition.target_agent_type,
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=definition.system_schedule_kind,
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement=definition.instruction,
            ),
            payload_header=payload_header,
            payload_meta=payload_meta,
            interval_ms=definition.interval_seconds * 1000,
            repeat_count=None,
            install_mode=definition.install_mode,
        )
        envelope = MessageEnvelope(
            msg_content=schedule_message,
            msg_meta=self._message_meta(
                session=session,
                manager_id=manager_id,
                owner_canonical_user_id=owner_canonical_user_id,
                correlation_id=correlation_id,
                definition=definition,
                requester_type=RequesterType.USER_REQUESTER,
                response_requested=False,
            ),
            msg_header=self._manager_header(manager_id),
        )
        self._delivery.upsert_scheduled_target(
            scheduled_job_id=definition.schedule_id,
            session_key=session.session_key,
            conversation_id=session.conversation_id,
            owner_canonical_user_id=owner_canonical_user_id,
        )
        await self._manager.publish_durable(envelope, routing_key=manager_id)

    def _message_meta(
        self,
        *,
        session: SessionRecord,
        manager_id: str,
        owner_canonical_user_id: str,
        correlation_id: str,
        definition: _MaintenanceScheduleDefinition,
        requester_type: RequesterType,
        response_requested: bool,
    ) -> MessageMeta:
        return MessageMeta(
            from_agent_name=(
                f"{self._settings.instance_name}_gateway"
                if requester_type == RequesterType.TIMER_REQUESTER
                else self._settings.user_display_name
            ),
            msg_correlation_id=correlation_id,
            conversation_id=session.conversation_id,
            request_timezone=self._settings.request_timezone,
            requester_type=requester_type,
            is_response_requested=response_requested,
            gateway_context=self._request_context.build_gateway_context(
                session_key=session.session_key,
                delivery_mode=definition.delivery_mode,
                scheduled_job_id=definition.schedule_id,
                route=GatewayRequestRoute(
                    channel_name=session.channel_name,
                    user_id=session.user_id,
                    chat_id=session.chat_id,
                    thread_id=session.thread_id,
                    canonical_user_id=owner_canonical_user_id,
                ),
                manager_agent_id=manager_id,
            ),
        )

    def _manager_header(self, manager_id: str) -> MessageHeader:
        return MessageHeader(
            from_agent_id=self._settings.response_route,
            to_routing_path=manager_id,
            response_routing_key=self._settings.response_route,
            agent_signature="",
        )

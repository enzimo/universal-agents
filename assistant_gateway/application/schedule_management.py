"""Gateway-owned user schedule creation and manager-backed control."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from assistant_gateway.application.delivery_service import GatewayDeliveryService
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.manager_commands import (
    GatewayManagerCommandRequest,
    GatewayManagerCommandService,
)
from assistant_gateway.application.ports import GatewayIdGenerator, GatewayPersonChangeCheck
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import (
    SYSTEM_SCHEDULE_CHANGE_KIND,
    ManagerScheduleCommand,
    ScheduledMessage,
    ScheduleTaskMemory,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import (
    MessageMetaType,
    RequesterType,
    ScheduleManagementClass,
)


@dataclass(frozen=True)
class GatewayScheduleSettings:
    """Stable gateway settings required for user schedule operations."""

    instance_name: str
    response_route: str
    user_display_name: str
    request_timezone: str
    default_session_key: str
    default_owner_canonical_user_id: str
    manager_unavailable_text: str


@dataclass(frozen=True)
class GatewayScheduleCreateRequest:
    """One normalized request to create a user-owned recurring schedule."""

    name: str
    instruction: str
    session_key: str | None = None
    interval_seconds: int | None = None
    cron_expression: str | None = None
    route: GatewayRequestRoute = GatewayRequestRoute()


@dataclass(frozen=True)
class GatewayScheduleCommandRequest:
    """One manager-backed schedule query or mutation."""

    action: Literal["list", "pause", "resume", "disable", "remove"]
    schedule_id: str | None = None
    paused_until: str | None = None
    session_key: str | None = None
    route: GatewayRequestRoute = GatewayRequestRoute()
    expected_record_digest: str | None = None
    owner_wide: bool = False


class GatewayScheduleApplication:
    """Own user schedule envelopes, targets, and control operations."""

    def __init__(
        self,
        *,
        settings: GatewayScheduleSettings,
        sessions: GatewaySessionService,
        request_context: GatewayRequestContextService,
        delivery: GatewayDeliveryService,
        manager: GatewayManagerClient,
        manager_commands: GatewayManagerCommandService,
        id_generator: GatewayIdGenerator,
        person_changes: GatewayPersonChangeCheck | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._request_context = request_context
        self._delivery = delivery
        self._manager = manager
        self._manager_commands = manager_commands
        self._id_generator = id_generator
        # Decides who may change schedules the runtime manages; without it
        # nobody may (the manager refuses those changes).
        self._person_changes = person_changes

    def may_change_system_schedules(self, route: GatewayRequestRoute = GatewayRequestRoute()) -> bool:
        """Whether the signed-in person may change schedules the runtime manages."""

        return self._may_change_system_schedules(self._request_context.canonical_user_id(route))

    def may_change_schedule(self, schedule: ScheduledMessage, route: GatewayRequestRoute = GatewayRequestRoute()) -> bool:
        """Whether the signed-in person may change this listed schedule of theirs.

        A person's own schedule, yes; a schedule the runtime manages only when
        they may make ``system_schedule`` changes.
        """

        if schedule.management_class != ScheduleManagementClass.SYSTEM:
            return True
        return self.may_change_system_schedules(route)

    def _may_change_system_schedules(self, person_id: str | None) -> bool:
        if self._person_changes is None or not person_id:
            return False
        return bool(self._person_changes.may_change(person_id, SYSTEM_SCHEDULE_CHANGE_KIND))

    async def add(self, request: GatewayScheduleCreateRequest) -> str:
        """Create and durably publish one user-managed recurring schedule."""

        manager_id = self._require_manager_id()
        session = self._resolve_session(request.session_key, request.route)
        schedule_id = self._id_generator.new_scheduled_job_id()
        correlation_id = self._id_generator.new_correlation_id()
        schedule_conversation_id = self._id_generator.new_conversation_id()
        resolved_route = self._resolved_route(request.route, session)
        payload_header = self._manager_header(manager_id)
        # The person's own time zone: the manager reads the schedule's cron
        # expression in it (the manager fills the schedule's cron_timezone from it).
        person_timezone = self._request_context.requester_presentation_for_session(session, resolved_route).timezone
        payload_meta = MessageMeta(
            from_agent_name=f"{self._settings.instance_name}_gateway",
            msg_correlation_id=correlation_id,
            conversation_id=schedule_conversation_id,
            request_timezone=person_timezone or self._settings.request_timezone,
            requester_type=RequesterType.TIMER_REQUESTER,
            is_response_requested=True,
            gateway_context=self._request_context.build_gateway_context(
                session_key=session.session_key,
                delivery_mode="cron",
                scheduled_job_id=schedule_id,
                route=resolved_route,
                manager_agent_id=manager_id,
            ),
        )
        schedule_message = ScheduledMessage(
            schedule_id=schedule_id,
            schedule_name=request.name,
            management_class=ScheduleManagementClass.USER,
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement=request.instruction.strip(),
            ),
            payload_header=payload_header,
            payload_meta=payload_meta,
            interval_ms=(
                request.interval_seconds * 1000
                if request.interval_seconds is not None
                else None
            ),
            cron_expression=request.cron_expression,
            repeat_count=None,
            schedule_memory=ScheduleTaskMemory(),
        )
        envelope = MessageEnvelope(
            msg_content=schedule_message,
            msg_meta=MessageMeta(
                from_agent_name=self._settings.user_display_name,
                msg_correlation_id=correlation_id,
                conversation_id=session.conversation_id,
                request_timezone=self._settings.request_timezone,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=False,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=session.session_key,
                    delivery_mode="cron",
                    scheduled_job_id=schedule_id,
                    route=resolved_route,
                    manager_agent_id=manager_id,
                ),
            ),
            msg_header=self._manager_header(manager_id),
        )
        self._delivery.register_scheduled_request(
            scheduled_job_id=schedule_id,
            correlation_id=correlation_id,
            session_key=session.session_key,
            request_kind="cron",
            request_text=request.instruction,
            conversation_id=schedule_conversation_id,
            followup_to_task_id=None,
        )
        await self._manager.publish_durable(envelope, routing_key=manager_id)
        return schedule_id

    async def list(
        self,
        *,
        session_key: str | None = None,
        route: GatewayRequestRoute = GatewayRequestRoute(),
        owner_wide: bool = False,
    ) -> list[ScheduledMessage]:
        response = await self.list_with_worker_assignments(
            session_key=session_key, route=route, owner_wide=owner_wide,
        )
        return list(response.schedules) if response.success else []

    async def list_with_worker_assignments(
        self,
        *,
        session_key: str | None = None,
        route: GatewayRequestRoute = GatewayRequestRoute(),
        owner_wide: bool = False,
    ) -> ManagerScheduleCommand:
        """Read schedules and their owner-filtered policy projection from the manager."""
        return await self._execute(
            GatewayScheduleCommandRequest(
                action="list",
                session_key=session_key,
                route=route,
                owner_wide=owner_wide,
            )
        )

    async def pause(
        self,
        schedule_id: str,
        *,
        paused_until: str,
        session_key: str | None = None,
        expected_record_digest: str | None = None,
    ) -> bool:
        response = await self._execute(
            GatewayScheduleCommandRequest(
                action="pause",
                schedule_id=schedule_id,
                paused_until=paused_until,
                session_key=session_key,
                expected_record_digest=expected_record_digest,
            )
        )
        return bool(response.success)

    async def resume(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
        expected_record_digest: str | None = None,
    ) -> bool:
        response = await self._execute(
            GatewayScheduleCommandRequest(
                action="resume",
                schedule_id=schedule_id,
                session_key=session_key,
                expected_record_digest=expected_record_digest,
            )
        )
        return bool(response.success)

    async def disable(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
        expected_record_digest: str | None = None,
    ) -> bool:
        response = await self._execute(
            GatewayScheduleCommandRequest(
                action="disable",
                schedule_id=schedule_id,
                session_key=session_key,
                expected_record_digest=expected_record_digest,
            )
        )
        return bool(response.success)

    async def remove(
        self,
        schedule_id: str,
        *,
        session_key: str | None = None,
        expected_record_digest: str | None = None,
    ) -> bool:
        response = await self._execute(
            GatewayScheduleCommandRequest(
                action="remove",
                schedule_id=schedule_id,
                session_key=session_key,
                expected_record_digest=expected_record_digest,
            )
        )
        if response.success:
            self._delivery.remove_scheduled_target(schedule_id)
        return bool(response.success)

    async def _execute(
        self,
        request: GatewayScheduleCommandRequest,
    ) -> ManagerScheduleCommand:
        session = self._resolve_session(request.session_key, request.route)
        resolved_route = self._resolved_route(request.route, session)
        # The signed-in person must be an active team member; the manager
        # reads the person only from the command's signed context and decides
        # changes to system-managed schedules itself.
        person = self._request_context.manager_principal(resolved_route)
        command = ManagerScheduleCommand(
            action=request.action,
            schedule_id=request.schedule_id,
            session_key=(
                None
                if request.owner_wide
                else session.session_key
                if request.action == "list"
                else None
            ),
            paused_until=request.paused_until,
            expected_record_digest=request.expected_record_digest,
        )
        response = await self._manager_commands.request(
            GatewayManagerCommandRequest(
                command=command,
                session=session,
                person=person,
                route=resolved_route,
            )
        )
        assert isinstance(response, ManagerScheduleCommand)
        return response

    def _resolve_session(
        self,
        session_key: str | None,
        route: GatewayRequestRoute,
    ) -> SessionRecord:
        owner_canonical_user_id = self._request_context.canonical_user_id(route)
        return self._sessions.resolve_session(
            requested_session_key=session_key,
            default_session_key=self._settings.default_session_key,
            manager_agent_id=self._manager.manager_id,
            owner_canonical_user_id=owner_canonical_user_id,
            default_owner_canonical_user_id=(
                self._settings.default_owner_canonical_user_id
            ),
            route=GatewaySessionRoute(
                channel_name=route.channel_name,
                user_id=route.user_id,
                chat_id=route.chat_id,
                thread_id=route.thread_id,
            ),
            enforce_owner_scope=self._request_context.users_enabled,
        )

    @staticmethod
    def _resolved_route(
        route: GatewayRequestRoute,
        session: SessionRecord,
    ) -> GatewayRequestRoute:
        return GatewayRequestRoute(
            channel_name=route.channel_name or session.channel_name,
            user_id=route.user_id or session.user_id,
            chat_id=route.chat_id or session.chat_id,
            chat_scope=route.chat_scope,
            thread_id=route.thread_id or session.thread_id,
            original_message_id=route.original_message_id,
            canonical_user_id=(
                session.owner_canonical_user_id or route.canonical_user_id
            ),
        )

    def _manager_header(self, manager_id: str) -> MessageHeader:
        return MessageHeader(
            from_agent_id=self._settings.response_route,
            to_routing_path=manager_id,
            response_routing_key=self._settings.response_route,
            agent_signature="",
        )

    def _require_manager_id(self) -> str:
        manager_id = self._manager.manager_id
        if manager_id is None:
            raise RuntimeError(self._settings.manager_unavailable_text)
        return manager_id

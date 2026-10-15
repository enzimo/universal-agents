"""Application-owned autonomous and automation task submission."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.ports import GatewayClock, GatewayIdGenerator
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from assistant_gateway.automation import (
    AutomationDeliveryPolicy,
    AutomationSessionRoute,
    build_automation_submission,
)
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType


@dataclass(frozen=True)
class GatewayAutomatedSubmissionSettings:
    """Stable runtime settings required for background task submission."""

    instance_name: str
    response_route: str
    default_timezone: str
    default_session_key: str
    default_owner_canonical_user_id: str


@dataclass(frozen=True)
class GatewayAutonomousTaskRequest:
    """One manager-bound autonomous task request."""

    instruction: str
    session_key: str
    origin_kind: str
    deliver_quiet_hours: bool = True
    route: GatewayRequestRoute = GatewayRequestRoute()


@dataclass(frozen=True)
class GatewayAutomationRequest:
    """One inline automation decision request with its original envelope."""

    automation_id: str
    hook_name: str
    original_envelope: MessageEnvelope
    rendered_original_user_text: str
    allowed_decisions: tuple[str, ...]
    decision_instructions: str
    suggested_later_time: str | None = None
    session_key: str | None = None
    conversation_id: str | None = None
    followup_to_task_id: str | None = None
    route: GatewayRequestRoute = GatewayRequestRoute()


class GatewayAutomatedSubmissionApplication:
    """Own persistence and publication for non-interactive gateway work."""

    def __init__(
        self,
        *,
        settings: GatewayAutomatedSubmissionSettings,
        delivery_policy: AutomationDeliveryPolicy,
        sessions: GatewaySessionService,
        request_context: GatewayRequestContextService,
        requests: GatewayRequestService,
        manager: GatewayManagerClient,
        clock: GatewayClock,
        id_generator: GatewayIdGenerator,
        refresh_delivery_preferences: Callable[[], None],
    ) -> None:
        self._settings = settings
        self._delivery_policy = delivery_policy
        self._sessions = sessions
        self._request_context = request_context
        self._requests = requests
        self._manager = manager
        self._clock = clock
        self._id_generator = id_generator
        self._refresh_delivery_preferences = refresh_delivery_preferences

    async def submit_autonomous_task(
        self,
        request: GatewayAutonomousTaskRequest,
    ) -> str:
        self._refresh_delivery_preferences()
        manager_id = self._require_manager_id()
        session = self._resolve_session(request.session_key, request.route)
        correlation_id = self._id_generator.new_correlation_id()
        request_kind = (
            request.origin_kind if request.deliver_quiet_hours else "interactive"
        )
        delivery_mode = (
            request.origin_kind if request.deliver_quiet_hours else "interactive"
        )
        self._requests.record(
            correlation_id=correlation_id,
            session_key=session.session_key,
            request_kind=request_kind,
            request_text=request.instruction,
            conversation_id=session.conversation_id,
            followup_to_task_id=None,
        )
        route = self._resolved_route(request.route, session)
        envelope = MessageEnvelope(
            msg_content=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement=request.instruction.strip(),
            ),
            msg_meta=MessageMeta(
                from_agent_name=f"{self._settings.instance_name}_gateway",
                msg_correlation_id=correlation_id,
                conversation_id=session.conversation_id,
                request_timezone=self._settings.default_timezone,
                requester_type=RequesterType.TIMER_REQUESTER,
                is_response_requested=True,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=session.session_key,
                    delivery_mode=delivery_mode,
                    route=route,
                    manager_agent_id=manager_id,
                ),
            ),
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=manager_id,
                response_routing_key=self._settings.response_route,
                agent_signature="",
            ),
        )
        await self._manager.publish_durable(envelope, routing_key=manager_id)
        return correlation_id

    async def submit_automation(self, request: GatewayAutomationRequest) -> str:
        manager_id = self._require_manager_id()
        snapshot_context = (
            request.original_envelope.msg_meta.gateway_context
            if request.original_envelope.msg_meta is not None
            else None
        )
        route = GatewayRequestRoute(
            channel_name=request.route.channel_name
            or (snapshot_context.channel_name if snapshot_context else None),
            user_id=request.route.user_id
            or (snapshot_context.user_id if snapshot_context else None),
            chat_id=request.route.chat_id
            or (snapshot_context.chat_id if snapshot_context else None),
            thread_id=request.route.thread_id
            or (snapshot_context.thread_id if snapshot_context else None),
            original_message_id=request.route.original_message_id
            or (snapshot_context.original_message_id if snapshot_context else None),
            canonical_user_id=(
                request.route.canonical_user_id
                or (snapshot_context.canonical_user_id if snapshot_context else None)
            ),
        )
        session_key = request.session_key or (
            snapshot_context.session_key if snapshot_context else None
        )
        session = self._resolve_session(session_key, route)
        submission = build_automation_submission(
            instance_name=self._settings.instance_name,
            response_queue_name=self._settings.response_route,
            manager_agent_id=manager_id,
            default_timezone=self._settings.default_timezone,
            delivery_settings=self._delivery_policy,
            now_utc=self._clock.now_utc(),
            correlation_id=self._id_generator.new_correlation_id(),
            session=AutomationSessionRoute(
                session_key=session.session_key,
                conversation_id=session.conversation_id,
            ),
            gateway_context=self._request_context.build_gateway_context(
                session_key=session.session_key,
                delivery_mode="automation",
                route=self._resolved_route(route, session),
                manager_agent_id=manager_id,
            ),
            automation_id=request.automation_id,
            hook_name=request.hook_name,
            original_envelope=request.original_envelope,
            rendered_original_user_text=request.rendered_original_user_text,
            allowed_decisions=list(request.allowed_decisions),
            decision_instructions=request.decision_instructions,
            suggested_later_time=request.suggested_later_time,
            conversation_id=request.conversation_id,
            followup_to_task_id=request.followup_to_task_id,
        )
        self._requests.record(
            correlation_id=submission.correlation_id,
            session_key=submission.session_key,
            request_kind="automation",
            request_text=submission.request_text,
            conversation_id=submission.conversation_id,
            followup_to_task_id=submission.followup_to_task_id,
        )
        await self._manager.publish_durable(
            submission.envelope,
            routing_key=submission.routing_key,
        )
        return submission.correlation_id

    def _resolve_session(
        self,
        session_key: str | None,
        route: GatewayRequestRoute,
    ) -> SessionRecord:
        return self._sessions.resolve_session(
            requested_session_key=session_key,
            default_session_key=self._settings.default_session_key,
            manager_agent_id=self._manager.manager_id,
            owner_canonical_user_id=self._request_context.canonical_user_id(route),
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
            canonical_user_id=route.canonical_user_id,
        )

    def _require_manager_id(self) -> str:
        manager_id = self._manager.manager_id
        if not manager_id:
            raise RuntimeError("No TeamArchitect agent is currently available.")
        return manager_id

"""Out-of-band task progress application over the manager client."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.local_responses import (
    GatewayLocalResponseFactory,
)
from assistant_gateway.application.ports import GatewayIdGenerator
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import TaskProgressChild, TaskProgressStatus
from custom_messages.enums import MessageMetaType, RequesterType


@dataclass(frozen=True)
class GatewayTaskProgressSettings:
    """Stable configuration required by progress-status requests."""

    instance_name: str
    response_route: str
    manager_named_agent_type: str
    request_timezone: str
    default_session_key: str
    default_owner_canonical_user_id: str
    timeout_seconds: float


@dataclass(frozen=True)
class GatewayTaskProgressRequest:
    """One authenticated request for the active conversation's progress."""

    session_key: str | None = None
    conversation_id: str | None = None
    route: GatewayRequestRoute = GatewayRequestRoute()


class GatewayTaskProgressService:
    """Resolve, request, validate, and render task-progress status."""

    def __init__(
        self,
        *,
        settings: GatewayTaskProgressSettings,
        sessions: GatewaySessionService,
        request_context: GatewayRequestContextService,
        manager: GatewayManagerClient,
        id_generator: GatewayIdGenerator,
        local_responses: GatewayLocalResponseFactory,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._request_context = request_context
        self._manager = manager
        self._id_generator = id_generator
        self._local_responses = local_responses
        self._logger = logger or logging.getLogger(__name__)

    @staticmethod
    def is_progress_request(
        text: str,
        attachments: list[UserTaskAttachment] | None = None,
    ) -> bool:
        return str(text or "").strip() == "?" and not attachments

    @staticmethod
    def render(status: TaskProgressStatus) -> str:
        if status.status == "idle":
            return "No task is currently running for this conversation."
        if status.status == "unauthorized":
            return "Task status is not available for this conversation."
        if status.status != "active":
            return "Task status is temporarily unavailable."
        state = getattr(status.root_state, "value", status.root_state) or "in progress"
        lines = [f"Task status: {str(state).replace('_', ' ')}."]
        if status.plan_phase:
            lines.append(f"Current phase: {status.plan_phase}")
        if status.children:
            lines.append("Delegated work: " + _delegated_work_summary(status.children))
        if status.last_activity:
            lines.append(f"Latest update: {status.last_activity}")
        if status.blocker:
            lines.append(f"Waiting on: {status.blocker}")
        return "\n".join(lines)

    async def request(self, request: GatewayTaskProgressRequest) -> GatewayResponse:
        route = request.route
        owner_canonical_user_id = self._request_context.canonical_user_id(route)
        session = self._sessions.resolve_session(
            requested_session_key=request.session_key,
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
        resolved_conversation_id = request.conversation_id or session.conversation_id
        resolved_route = GatewayRequestRoute(
            channel_name=route.channel_name or session.channel_name,
            user_id=route.user_id or session.user_id,
            chat_id=route.chat_id or session.chat_id,
            thread_id=route.thread_id or session.thread_id,
            original_message_id=route.original_message_id,
            canonical_user_id=(
                session.owner_canonical_user_id or route.canonical_user_id
            ),
        )
        principal = self._request_context.manager_principal(resolved_route)
        manager_id = self._manager.manager_id
        if manager_id is None or not self._manager.connected:
            return self._local_responses.create(
                text=(
                    "Task status is temporarily unavailable because the team lead "
                    "is offline."
                ),
                message_type="task_progress_status",
                conversation_id=resolved_conversation_id,
            )
        correlation_id = self._id_generator.new_correlation_id()
        progress_request = TaskProgressStatus(
            status_type=MessageMetaType.REQUEST,
            target_agent_id=manager_id,
            target_named_agent_type=self._settings.manager_named_agent_type,
            canonical_user_id=principal.canonical_user_id,
            session_key=session.session_key,
            conversation_id=resolved_conversation_id,
        )
        envelope = MessageEnvelope(
            msg_content=progress_request,
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=manager_id,
                response_routing_key=self._settings.response_route,
                agent_signature="",
                idempotency_key=progress_request.progress_status_id,
            ),
            msg_meta=MessageMeta(
                from_agent_name=f"{self._settings.instance_name}_gateway",
                msg_correlation_id=correlation_id,
                conversation_id=resolved_conversation_id,
                request_timezone=self._settings.request_timezone,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=session.session_key,
                    delivery_mode="task_progress_status",
                    route=resolved_route,
                    manager_agent_id=manager_id,
                ),
            ),
        )
        async with self._manager.control_channel(correlation_id) as queue:
            try:
                await self._manager.publish_operational(envelope)
                response_envelope = await asyncio.wait_for(
                    queue.get(),
                    timeout=self._settings.timeout_seconds,
                )
                response = response_envelope.msg_content
                if (
                    response_envelope.msg_header.from_agent_id != manager_id
                    or not isinstance(response, TaskProgressStatus)
                    or response.progress_status_id
                    != progress_request.progress_status_id
                ):
                    raise RuntimeError(
                        "Manager returned an invalid task progress response."
                    )
                return GatewayResponse(
                    correlation_id=correlation_id,
                    conversation_id=resolved_conversation_id,
                    task_id=response.root_task_id,
                    message_type="task_progress_status",
                    text=self.render(response),
                    raw_envelope=response_envelope,
                )
            except (asyncio.TimeoutError, RuntimeError):
                self._logger.warning(
                    "Gateway task progress request timed out. "
                    "event=gateway_task_progress_timeout session_key=%s "
                    "conversation_id=%s manager_id=%s",
                    session.session_key,
                    resolved_conversation_id,
                    manager_id,
                )
                return self._local_responses.create(
                    text=(
                        "Task status is temporarily unavailable. "
                        "The active task was not interrupted."
                    ),
                    message_type="task_progress_status",
                    correlation_id=correlation_id,
                    conversation_id=resolved_conversation_id,
                )


_QUEUE_STATUS_WORDS = {
    "queued": "waiting for a free worker",
    "running": "running",
    "finished": "done",
    "failed": "failed",
    "cancelled": "stopped",
}


def _delegated_work_summary(children: list[TaskProgressChild]) -> str:
    """Up to four delegated tasks one by one; more as counts ("9 done, 4 running")."""

    if len(children) <= 4:
        parts = []
        for child in children:
            role = child.named_agent_type or "specialist"
            if child.queue_status:
                where = _QUEUE_STATUS_WORDS.get(child.queue_status, child.queue_status)
            else:
                where = str(getattr(child.state, "value", child.state)).replace("_", " ")
            parts.append(f"{role}: {where}")
        return "; ".join(parts)
    counts: dict[str, int] = {}
    for child in children:
        key = child.queue_status or str(getattr(child.state, "value", child.state))
        counts[key] = counts.get(key, 0) + 1
    order = ["finished", "running", "queued", "failed", "cancelled"]
    rendered = [
        f"{counts[key]} {_QUEUE_STATUS_WORDS.get(key, key)}"
        for key in order + sorted(set(counts) - set(order))
        if counts.get(key)
    ]
    return f"{len(children)} tasks: " + ", ".join(rendered)

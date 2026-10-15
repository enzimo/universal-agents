"""Pending clarification lifecycle and manager reply application."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.ports import GatewayIdGenerator
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestRoute,
)
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.application.response_coordinator import (
    GatewayResponseCoordinator,
)
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.application.session_service import (
    GatewaySessionRoute,
    GatewaySessionService,
)
from assistant_gateway.domain.request_state import PendingQueryRecord
from assistant_gateway.ports.repositories import SessionRepository
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.replied_message_v1 import RepliedMessage
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import (
    TaskQuery,
    TaskQueryChoiceOption,
    TaskStatus,
)
from custom_messages.enums import MessageMetaType, RequesterType


@dataclass(frozen=True)
class GatewayClarificationSettings:
    """Stable configuration for pending-query validation and replies."""

    instance_name: str
    response_route: str
    manager_named_agent_type: str
    request_timezone: str
    default_session_key: str
    default_owner_canonical_user_id: str
    pending_timeout_seconds: float | None
    parent_probe_timeout_seconds: float
    manager_unavailable_text: str


def restore_task_query(
    payload_json: str | None,
    fallback: TaskQuery,
    *,
    logger: logging.Logger | None = None,
) -> TaskQuery:
    """Restore a persisted structured query, using its trusted fallback."""

    if not payload_json:
        return fallback
    try:
        return TaskQuery.model_validate_json(payload_json)
    except Exception:
        (logger or logging.getLogger(__name__)).warning(
            "Failed to restore structured TaskQuery payload from pending query state.",
            exc_info=True,
        )
        return fallback


def _choice_maps(
    choices: Sequence[TaskQueryChoiceOption],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    by_id = {choice.option_id.lower(): choice.option_id for choice in choices}
    by_number = {
        str(index): choice.option_id for index, choice in enumerate(choices, start=1)
    }
    by_label = {
        choice.label.lower(): choice.option_id for choice in choices if choice.label
    }
    return by_id, by_number, by_label


def _normalized_choice_token(value: str) -> str:
    return str(value or "").strip().strip("`'\"()[]{}.:").lower()


def _selected_ids_from_tokens(
    response_text: str,
    *,
    by_id: dict[str, str],
    by_number: dict[str, str],
    by_label: dict[str, str],
) -> list[str]:
    selected: list[str] = []
    for raw_token in re.split(r"[\s,;]+", str(response_text or "")):
        token = _normalized_choice_token(raw_token)
        if not token:
            continue
        option_id = by_id.get(token) or by_number.get(token) or by_label.get(token)
        if option_id and option_id not in selected:
            selected.append(option_id)
    return selected


def selected_choice_ids_from_text(
    task_query: TaskQuery,
    response_text: str,
) -> list[str]:
    """Resolve stable choice identifiers from one user response."""

    choices = list(task_query.choices or [])
    if not choices:
        return []
    by_id, by_number, by_label = _choice_maps(choices)
    normalized_response = _normalized_choice_token(response_text)
    if normalized_response in by_id:
        return [by_id[normalized_response]]
    if normalized_response in by_label:
        return [by_label[normalized_response]]
    selected = _selected_ids_from_tokens(
        response_text,
        by_id=by_id,
        by_number=by_number,
        by_label=by_label,
    )
    if task_query.choice_mode != "multiple" and len(selected) > 1:
        return selected[:1]
    return selected


class GatewayClarificationCore:
    """Own pending-query validity and manager reply publication."""

    def __init__(
        self,
        *,
        settings: GatewayClarificationSettings,
        requests: GatewayRequestService,
        sessions: GatewaySessionService,
        session_repository: SessionRepository,
        request_context: GatewayRequestContextService,
        manager: GatewayManagerClient,
        id_generator: GatewayIdGenerator,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._requests = requests
        self._sessions = sessions
        self._session_repository = session_repository
        self._request_context = request_context
        self._manager = manager
        self._id_generator = id_generator
        self._logger = logger or logging.getLogger(__name__)

    def expire_stale_on_startup(self) -> int:
        expired_count = self._requests.expire_stale_pending_queries(
            timeout_seconds=self._settings.pending_timeout_seconds
        )
        if expired_count and self._settings.pending_timeout_seconds is not None:
            self._logger.warning(
                "Gateway expired %s pending TaskQuery record(s) older than %.0fs "
                "during startup.",
                expired_count,
                self._settings.pending_timeout_seconds,
            )
        return expired_count

    async def resolve_pending_query(
        self,
        session_key: str,
    ) -> PendingQueryRecord | None:
        pending_query = self._requests.get_pending_query(session_key)
        if pending_query is None:
            return None
        if self._requests.pending_query_is_expired(
            pending_query,
            timeout_seconds=self._settings.pending_timeout_seconds,
        ):
            self._requests.clear_pending_query(session_key, status="expired")
            self._logger.warning(
                "Gateway cleared expired pending TaskQuery %s for session %s.",
                pending_query.task_query_id,
                session_key,
            )
            return None
        if not await self._parent_is_active(pending_query):
            self._requests.clear_pending_query(
                session_key,
                status="stale_parent_missing",
            )
            self._logger.warning(
                "Gateway cleared pending TaskQuery %s for session %s because "
                "parent task %s is no longer active.",
                pending_query.task_query_id,
                session_key,
                pending_query.parent_task_id,
            )
            return None
        return pending_query

    async def _parent_is_active(self, pending_query: PendingQueryRecord) -> bool:
        parent_task_id = pending_query.parent_task_id
        if not parent_task_id:
            return False
        manager_id = self._manager.manager_id
        if manager_id is None or not self._manager.connected:
            return True
        session = self._session_repository.get_session(pending_query.session_key)
        canonical_user_id = str(
            getattr(session, "owner_canonical_user_id", "") or ""
        ).strip()
        correlation_id = self._id_generator.new_correlation_id()
        request = TaskStatus(
            status_type=MessageMetaType.REQUEST,
            task_id=parent_task_id,
            target_agent_id=manager_id,
            target_named_agent_type=self._settings.manager_named_agent_type,
            correlation_id=correlation_id,
        )
        async with self._manager.control_channel(correlation_id) as queue:
            try:
                envelope = MessageEnvelope(
                    msg_content=request,
                    msg_header=MessageHeader(
                        from_agent_id=self._settings.response_route,
                        to_routing_path=manager_id,
                        response_routing_key=self._settings.response_route,
                        agent_signature="",
                        idempotency_key=request.task_status_id,
                    ),
                    msg_meta=MessageMeta(
                        from_agent_name=f"{self._settings.instance_name}_gateway",
                        msg_correlation_id=correlation_id,
                        conversation_id=pending_query.conversation_id,
                        request_timezone=self._settings.request_timezone,
                        requester_type=RequesterType.USER_REQUESTER,
                        is_response_requested=True,
                        gateway_context=(
                            self._request_context.build_gateway_context(
                                session_key=pending_query.session_key,
                                delivery_mode="task_status_probe",
                                route=GatewayRequestRoute(
                                    canonical_user_id=(canonical_user_id or None)
                                ),
                                manager_agent_id=manager_id,
                            )
                        ),
                    ),
                )
                await self._manager.publish_operational(envelope)
                response_envelope = await asyncio.wait_for(
                    queue.get(),
                    timeout=self._settings.parent_probe_timeout_seconds,
                )
                response = response_envelope.msg_content
                if (
                    response_envelope.msg_header.from_agent_id != manager_id
                    or not isinstance(response, TaskStatus)
                    or response.task_status_id != request.task_status_id
                ):
                    return True
                return bool(response.is_known_task)
            except (asyncio.TimeoutError, RuntimeError) as exc:
                self._logger.warning(
                    "Gateway could not validate parent task %s for pending "
                    "TaskQuery %s; preserving pending query: %s",
                    parent_task_id,
                    pending_query.task_query_id,
                    exc,
                )
                return True

    async def reply_to_task_query(
        self,
        task_query: TaskQuery,
        *,
        correlation_id: str,
        response_text: str,
        session_key: str,
        conversation_id: str | None,
        replied_message: RepliedMessage | None = None,
    ) -> None:
        manager_id = self._manager.manager_id
        if manager_id is None:
            raise RuntimeError(self._settings.manager_unavailable_text)
        session = self._sessions.resolve_session(
            requested_session_key=session_key,
            default_session_key=self._settings.default_session_key,
            manager_agent_id=manager_id,
            owner_canonical_user_id=(self._settings.default_owner_canonical_user_id),
            default_owner_canonical_user_id=(
                self._settings.default_owner_canonical_user_id
            ),
            route=GatewaySessionRoute(),
            enforce_owner_scope=False,
        )
        presentation = self._request_context.requester_presentation_for_session(session)
        envelope = MessageEnvelope(
            msg_content=TaskQuery(
                task_query_id=task_query.task_query_id,
                query_type=MessageMetaType.RESPONSE,
                message=response_text,
                replied_message=replied_message,
                parent_task_id=task_query.parent_task_id,
                choice_mode=task_query.choice_mode,
                choices=task_query.choices,
                allow_free_text=task_query.allow_free_text,
                selected_choice_ids=selected_choice_ids_from_text(
                    task_query,
                    response_text,
                ),
            ),
            msg_meta=MessageMeta(
                from_agent_name=presentation.display_name,
                msg_correlation_id=correlation_id,
                conversation_id=conversation_id,
                request_timezone=presentation.timezone,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=session.session_key,
                    delivery_mode="interactive",
                    route=GatewayRequestRoute(
                        channel_name=session.channel_name,
                        user_id=session.user_id,
                        chat_id=session.chat_id,
                        thread_id=session.thread_id,
                        canonical_user_id=session.owner_canonical_user_id,
                    ),
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
        self._requests.record(
            correlation_id=correlation_id,
            session_key=session.session_key,
            request_kind="interactive_query_response",
            request_text=response_text,
            conversation_id=conversation_id,
            followup_to_task_id=task_query.parent_task_id,
            status="responded",
        )
        await self._manager.publish_durable(envelope, routing_key=manager_id)


class GatewayClarificationApplication:
    """Reply to one valid pending query and await its terminal response."""

    def __init__(
        self,
        *,
        core: GatewayClarificationCore,
        requests: GatewayRequestService,
        manager: GatewayManagerClient,
        responses: GatewayResponseCoordinator,
    ) -> None:
        self._core = core
        self._requests = requests
        self._manager = manager
        self._responses = responses

    async def has_pending_query(self, session_key: str) -> bool:
        return await self._core.resolve_pending_query(session_key) is not None

    async def reply(
        self,
        *,
        session_key: str,
        response_text: str,
        replied_message: RepliedMessage | None = None,
    ) -> GatewayResponse:
        pending_query = await self._core.resolve_pending_query(session_key)
        if pending_query is None:
            raise ValueError(f"No pending query for session_key={session_key}")

        correlation_id = pending_query.correlation_id
        async with self._manager.response_channel(correlation_id):
            if self._manager.manager_id is None:
                await self._responses.queue_manager_unavailable(correlation_id)
                response = await self._responses.await_terminal_response(
                    correlation_id=correlation_id,
                    session_key=session_key,
                    query_handler=None,
                )
                self._requests.clear_pending_query(session_key)
                return response
            await self._core.reply_to_task_query(
                restore_task_query(
                    pending_query.query_payload_json,
                    TaskQuery(
                        task_query_id=pending_query.task_query_id,
                        query_type=MessageMetaType.REQUEST,
                        message=pending_query.query_text,
                        parent_task_id=pending_query.parent_task_id,
                    ),
                ),
                correlation_id=correlation_id,
                response_text=response_text,
                session_key=session_key,
                conversation_id=pending_query.conversation_id,
                replied_message=replied_message,
            )
            # Publication consumes this answer. Waiting may persist a new question,
            # or time out while the manager continues; neither permits replaying it.
            self._requests.clear_pending_query(session_key)
            response = await self._responses.await_terminal_response(
                correlation_id=correlation_id,
                session_key=session_key,
                query_handler=None,
            )
            return response

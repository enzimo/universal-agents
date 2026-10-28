"""Gateway-owned operational controls sent to the active manager runtime."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from assistant_gateway.application.debug_controls import normalize_context_dump_mode
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.manager_commands import (
    GatewayManagerCommandRequest,
    GatewayManagerCommandService,
)
from assistant_gateway.application.objective_control_text import (
    objective_control_text,
    objective_status_words,
)
from assistant_gateway.application.ports import GatewayIdGenerator
from assistant_gateway.application.request_context import (
    GatewayCommandPerson,
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
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.control_v1 import PAUSE_OBJECTIVE, ControlInstruction, Instruction, ManagerRuntimeWorkCommand, RuntimeWorkSummary, SystemStatusMessage
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import DELIVERY_MODE_INTERACTIVE, MessageHeader, MessageMeta
from custom_messages.enums import ControlType, ObjectiveStatus, RequesterType, RoutingType


@dataclass(frozen=True)
class GatewayOperationalControlSettings:
    """Stable configuration required by operational manager controls."""

    response_route: str
    manager_named_agent_type: str
    default_session_key: str
    default_owner_canonical_user_id: str


@dataclass(frozen=True)
class GatewayStopControlRequest:
    """One authenticated request to stop work for a gateway conversation."""

    session_key: str | None = None
    conversation_id: str | None = None
    action: str = "inventory"
    scope: str = "user"
    target_agent_id: str | None = None
    target_task_id: str | None = None
    target_objective_id: str | None = None
    expected_objective_digest: str | None = None
    inventory_digest: str | None = None
    task_offset: int = 0
    task_limit: int = 20
    command_text: str = "/stop"
    route: GatewayRequestRoute = GatewayRequestRoute()
    reason: str = "Stop requested by user."


@dataclass(frozen=True)
class GatewayContextDebugControlRequest:
    """One authenticated request to change manager context-dump behavior."""

    enabled: bool
    mode: str
    session_key: str | None = None
    conversation_id: str | None = None
    route: GatewayRequestRoute = GatewayRequestRoute()


@runtime_checkable
class GatewayStopControl(Protocol):
    """Narrow stop-control capability used by interactive commands."""

    async def stop(self, request: GatewayStopControlRequest) -> GatewayResponse:
        """Stop active work selected by the authenticated gateway route."""


@dataclass(frozen=True)
class _ResolvedOperationalControl:
    """Trusted session, identity, and correlation state for one control."""

    correlation_id: str
    session_key: str
    conversation_id: str | None
    requester_display_name: str
    request_timezone: str
    route: GatewayRequestRoute
    session: SessionRecord


def _append_objective_control_status(
    lines: list[str],
    response: ManagerRuntimeWorkCommand,
) -> None:
    if response.objective_status is None or not response.target_objective_id:
        return
    state = str(getattr(response.objective_status, "value", response.objective_status))
    lines.append(f"Objective {response.target_objective_id} is now {objective_status_words(state)}.")
    # Only a pause that finished stops every future managed run.
    if (
        response.action == PAUSE_OBJECTIVE
        and response.success
        and response.objective_status == ObjectiveStatus.PAUSED
    ):
        lines.append(
            "Future managed runs are paused; any currently executing task "
            "must be cancelled separately with /stop."
        )


def _append_objectives(
    lines: list[str],
    response: ManagerRuntimeWorkCommand,
) -> None:
    if response.action != "list_objectives":
        return
    if not response.objectives:
        lines.append("No Objectives are owned by this user.")
        return
    lines.append("Objectives:")
    for objective in response.objectives:
        lines.append(
            f"- {objective.objective_id}: {objective.status.value} | "
            f"{objective.title}"
        )
        if objective.current_focus:
            lines.append(f"  Current focus: {objective.current_focus}")


def _append_cancellation_results(
    lines: list[str],
    response: ManagerRuntimeWorkCommand,
) -> None:
    if not response.cancellation_receipts:
        return
    lines.append("Cancellation results:")
    for receipt in response.cancellation_receipts:
        state = "cancelled" if receipt.confirmed else "not confirmed"
        detail = (
            f"{receipt.cancelled_task_count} local task(s), "
            f"{receipt.unconfirmed_worker_count} remote worker(s) awaiting confirmation"
        )
        lines.append(f"- {receipt.root_task_id}: {state} ({detail})")
        if receipt.message:
            lines.append(f"  {receipt.message}")


def _append_control_failure(
    lines: list[str],
    response: ManagerRuntimeWorkCommand,
) -> None:
    if response.success is not False:
        return
    objective_text = objective_control_text(response)
    if objective_text:
        lines.append(objective_text)
        return
    detail = " ".join(str(response.message or "control incomplete").split())
    lines.append(f"Control incomplete: {detail}")


def _append_active_work(
    lines: list[str],
    response: ManagerRuntimeWorkCommand,
) -> None:
    if not response.roots:
        if not response.cancellation_receipts:
            lines.append("No active work is currently owned by this user.")
        return
    lines.append("Active work:")
    for root in response.roots:
        _append_active_work_root(lines, root)


def _append_active_work_root(lines: list[str], root: RuntimeWorkSummary) -> None:
    state_value = root.state
    state = str(getattr(state_value, "value", state_value)).replace("_", " ")
    scope = ", ".join(
        value
        for value in (
            f"session {root.session_key}" if root.session_key else None,
            f"conversation {root.conversation_id}" if root.conversation_id else None,
            f"Objective {root.objective_id}" if root.objective_id else None,
        )
        if value
    )
    lines.append(
        f"- {root.root_task_id}: {state}" + (f" ({scope})" if scope else "")
    )
    if root.blocker:
        lines.append(f"  Waiting on: {root.blocker}")
    if root.children:
        worker_labels = [
            child.target_agent_name
            or child.target_named_agent_type
            or child.task_id
            for child in root.children
        ]
        lines.append("  Attached work: " + ", ".join(worker_labels[:8]))


def _append_scheduled_work(
    lines: list[str],
    response: ManagerRuntimeWorkCommand,
    *,
    review_url: str | None = None,
) -> None:
    if not response.schedules:
        return
    # An objective may own several schedules; count the objective only once.
    objective_count = len(
        {schedule.objective_id for schedule in response.schedules if schedule.objective_id}
    )
    item_count = sum(not schedule.objective_id for schedule in response.schedules)
    counts = []
    if item_count:
        counts.append(f"{item_count} scheduled item{'s' if item_count != 1 else ''}")
    if objective_count:
        counts.append(
            f"{objective_count} scheduled objective{'s' if objective_count != 1 else ''}"
        )
    lines.extend(["", "Scheduled work: " + " and ".join(counts) + "."])
    lines.append("Future scheduled work is separate and was not stopped.")
    if review_url:
        lines.append(f"[Review schedules and objectives]({review_url})")
    else:
        lines.append("Review schedules with /schedules and objectives with /objectives.")


class GatewayOperationalControlApplication:
    """Own stop/debug control targeting, persistence, exchange, and waiting."""

    def __init__(
        self,
        *,
        settings: GatewayOperationalControlSettings,
        sessions: GatewaySessionService,
        request_context: GatewayRequestContextService,
        requests: GatewayRequestService,
        manager: GatewayManagerClient,
        manager_commands: GatewayManagerCommandService,
        responses: GatewayResponseCoordinator,
        id_generator: GatewayIdGenerator,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._request_context = request_context
        self._requests = requests
        self._manager = manager
        self._manager_commands = manager_commands
        self._responses = responses
        self._id_generator = id_generator
        self._logger = logger or logging.getLogger(__name__)

    async def stop(self, request: GatewayStopControlRequest) -> GatewayResponse:
        """List or cancel authenticated work through the manager control plane.

        The database work (finding the session, recording the request) runs in
        a worker thread: Web Chat's Tasks panel asks every 10 seconds while it
        is open, and the gateway's loop also runs the chat apps
        (website_handlers.off_loop). Waiting for the agent stays on the loop.
        """

        resolved, command, person = await asyncio.to_thread(self._prepare_stop, request)
        return await self._send_stop(request, resolved, command, person)

    def _prepare_stop(
        self, request: GatewayStopControlRequest
    ) -> tuple[_ResolvedOperationalControl, ManagerRuntimeWorkCommand, GatewayCommandPerson]:
        resolved = self._resolve(
            session_key=request.session_key,
            conversation_id=request.conversation_id,
            route=request.route,
        )
        self._requests.record(
            correlation_id=resolved.correlation_id,
            session_key=resolved.session_key,
            request_kind="interactive_control",
            request_text=request.command_text,
            conversation_id=resolved.conversation_id,
            followup_to_task_id=request.target_task_id,
        )
        # The signed-in person must be an active team member; it goes on the
        # request for the gateway's checks and the signed context, not in the command.
        person = self._request_context.manager_principal(resolved.route)
        command = ManagerRuntimeWorkCommand(
            action=request.action,
            scope=request.scope,
            target_task_id=request.target_task_id,
            target_objective_id=request.target_objective_id,
            expected_objective_digest=request.expected_objective_digest,
            inventory_digest=request.inventory_digest,
            task_offset=request.task_offset,
            task_limit=request.task_limit,
            session_key=resolved.session_key,
            conversation_id=resolved.conversation_id,
            reason=request.reason,
        )
        return resolved, command, person

    async def _send_stop(
        self,
        request: GatewayStopControlRequest,
        resolved: _ResolvedOperationalControl,
        command: ManagerRuntimeWorkCommand,
        person: GatewayCommandPerson,
    ) -> GatewayResponse:
        if self._manager.manager_id is None and request.target_agent_id is None:
            self._logger.warning(
                "Gateway cannot inspect or cancel runtime work because no manager "
                "route is currently known. "
                "event=gateway_runtime_work_control_no_manager_route "
                "correlation_id=%s conversation_id=%s session_key=%s",
                resolved.correlation_id,
                resolved.conversation_id,
                resolved.session_key,
            )
            return await self._manager_unavailable(resolved)
        responder = "The agent" if request.target_agent_id else "The TeamLead"
        try:
            response = await self._manager_commands.request(
                GatewayManagerCommandRequest(
                    command=command,
                    person=person,
                    target_agent_id=request.target_agent_id,
                    session=resolved.session,
                    route=resolved.route,
                )
            )
        except TimeoutError:
            self._logger.warning(
                "Manager control response timed out; the outcome is unconfirmed. "
                "event=gateway_runtime_work_control_timeout correlation_id=%s "
                "command_id=%s action=%s",
                resolved.correlation_id, command.command_id, command.action,
            )
            await asyncio.to_thread(self._requests.update_status, resolved.correlation_id, "timed_out")
            message = (
                f"{responder} did not reply in time. The result is not confirmed; "
                "the request may still be processing. Refresh Tasks or run /stop "
                "to check the current state before trying again."
            )
            return GatewayResponse(
                correlation_id=resolved.correlation_id,
                conversation_id=resolved.conversation_id,
                task_id=request.target_task_id,
                message_type="system_status",
                message_level="warning",
                text=message,
                raw_envelope=MessageEnvelope(
                    msg_content=SystemStatusMessage(system_status=message),
                    msg_header=MessageHeader(from_agent_id=self._settings.response_route),
                    msg_meta=MessageMeta(msg_correlation_id=resolved.correlation_id),
                ),
            )
        except RuntimeError as exc:
            self._logger.warning(
                "Gateway runtime-work control could not reach the manager. "
                "event=gateway_runtime_work_control_manager_unavailable "
                "correlation_id=%s conversation_id=%s session_key=%s error=%s",
                resolved.correlation_id,
                resolved.conversation_id,
                resolved.session_key,
                exc,
            )
            if request.target_agent_id:
                raise RuntimeError("The selected agent could not be reached. Refresh Tasks to retry.") from exc
            return await self._manager_unavailable(resolved)
        if not isinstance(response, ManagerRuntimeWorkCommand):
            raise RuntimeError("Manager returned an invalid runtime-work response.")
        raw_envelope = MessageEnvelope(
            msg_content=response,
            msg_header=MessageHeader(
                from_agent_id=request.target_agent_id or self._manager.manager_id or "manager",
                to_routing_path=self._settings.response_route,
                agent_signature="",
            ),
            msg_meta=MessageMeta(
                msg_correlation_id=resolved.correlation_id,
                conversation_id=resolved.conversation_id,
            ),
        )
        return GatewayResponse(
            correlation_id=resolved.correlation_id,
            conversation_id=resolved.conversation_id,
            task_id=request.target_task_id,
            message_type="runtime_work_control",
            text=self._render_runtime_work_response(
                response,
                review_url=(
                    "/webchat?view=objectives"
                    if resolved.route.channel_name == "website"
                    else None
                ),
            ),
            raw_envelope=raw_envelope,
        )

    @staticmethod
    def _render_runtime_work_response(
        response: ManagerRuntimeWorkCommand, *, review_url: str | None = None
    ) -> str:
        lines: list[str] = []
        _append_objective_control_status(lines, response)
        _append_objectives(lines, response)
        _append_cancellation_results(lines, response)
        _append_control_failure(lines, response)
        _append_active_work(lines, response)
        _append_scheduled_work(lines, response, review_url=review_url)
        if response.roots:
            lines.extend(
                [
                    "To cancel work, use one of:",
                    "/stop task <task_id>",
                    "/stop conversation",
                    "/stop session",
                    "/stop all",
                ]
            )
        return "\n".join(lines)

    async def set_context_debug_dump(
        self,
        request: GatewayContextDebugControlRequest,
    ) -> GatewayResponse:
        """Publish one correlated context-dump settings control."""

        normalized_mode = normalize_context_dump_mode(request.mode)
        resolved = self._resolve(
            session_key=request.session_key,
            conversation_id=request.conversation_id,
            route=request.route,
        )
        async with self._manager.response_channel(resolved.correlation_id):
            self._requests.record(
                correlation_id=resolved.correlation_id,
                session_key=resolved.session_key,
                request_kind="interactive_control",
                request_text=(
                    "/debug context_dump "
                    f"enabled={str(bool(request.enabled)).lower()} "
                    f"mode={normalized_mode}"
                ),
                conversation_id=resolved.conversation_id,
                followup_to_task_id=None,
            )
            if self._manager.manager_id is None:
                self._logger.warning(
                    "Gateway cannot publish context debug control because no manager "
                    "route is currently known. "
                    "event=gateway_context_debug_control_no_manager_route "
                    "correlation_id=%s conversation_id=%s session_key=%s "
                    "enabled=%s mode=%s",
                    resolved.correlation_id,
                    resolved.conversation_id,
                    resolved.session_key,
                    bool(request.enabled),
                    normalized_mode,
                )
                return await self._manager_unavailable(resolved)

            envelope = self._build_envelope(
                resolved,
                content=ControlInstruction(
                    type=ControlType.CONTROL_MSG,
                    instruction=Instruction(instruction="debug_context_dump"),
                    reason="Web Chat debug context dump control.",
                    cascade_to_subtasks=False,
                    runtime_settings={
                        "enabled": bool(request.enabled),
                        "mode": normalized_mode,
                    },
                ),
            )
            self._logger.info(
                "Gateway publishing context debug control. "
                "event=gateway_context_debug_control_publish correlation_id=%s "
                "conversation_id=%s session_key=%s cached_manager_id=%s "
                "enabled=%s mode=%s",
                resolved.correlation_id,
                resolved.conversation_id,
                resolved.session_key,
                self._manager.manager_id,
                bool(request.enabled),
                normalized_mode,
            )
            await self._manager.publish_operational(envelope)
            return await self._await_response(resolved)

    def _resolve(
        self,
        *,
        session_key: str | None,
        conversation_id: str | None,
        route: GatewayRequestRoute,
    ) -> _ResolvedOperationalControl:
        session = self._sessions.resolve_session(
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
        presentation = self._request_context.requester_presentation_for_session(
            session,
            route,
        )
        resolved_route = GatewayRequestRoute(
            channel_name=route.channel_name or session.channel_name,
            user_id=route.user_id or session.user_id,
            chat_id=route.chat_id or session.chat_id,
            chat_scope=route.chat_scope,
            thread_id=route.thread_id or session.thread_id,
            original_message_id=route.original_message_id,
            canonical_user_id=presentation.canonical_user_id,
        )
        return _ResolvedOperationalControl(
            correlation_id=self._id_generator.new_correlation_id(),
            session_key=session.session_key,
            conversation_id=conversation_id or session.conversation_id,
            requester_display_name=presentation.display_name,
            request_timezone=presentation.timezone,
            route=resolved_route,
            session=session,
        )

    def _build_envelope(
        self,
        resolved: _ResolvedOperationalControl,
        *,
        content: ControlInstruction,
        followup_to_task_id: str | None = None,
    ) -> MessageEnvelope:
        return MessageEnvelope(
            msg_content=content,
            msg_meta=MessageMeta(
                from_agent_name=resolved.requester_display_name,
                to_named_agent_type=self._settings.manager_named_agent_type,
                msg_correlation_id=resolved.correlation_id,
                conversation_id=resolved.conversation_id,
                followup_to_task_id=followup_to_task_id,
                request_timezone=resolved.request_timezone,
                requester_type=RequesterType.USER_REQUESTER,
                is_response_requested=True,
                gateway_context=self._request_context.build_gateway_context(
                    session_key=resolved.session_key,
                    delivery_mode=DELIVERY_MODE_INTERACTIVE,
                    route=resolved.route,
                    manager_agent_id=self._manager.manager_id,
                ),
            ),
            msg_header=MessageHeader(
                from_agent_id=self._settings.response_route,
                to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
                response_routing_key=self._settings.response_route,
                agent_signature="",
            ),
        )

    async def _manager_unavailable(
        self,
        resolved: _ResolvedOperationalControl,
    ) -> GatewayResponse:
        has_pending_response = getattr(
            self._manager,
            "has_pending_response",
            lambda _correlation_id: False,
        )
        if not has_pending_response(resolved.correlation_id):
            async with self._manager.response_channel(resolved.correlation_id):
                await self._responses.queue_manager_unavailable(
                    resolved.correlation_id
                )
                return await self._await_response(resolved)
        await self._responses.queue_manager_unavailable(resolved.correlation_id)
        return await self._await_response(resolved)

    async def _await_response(
        self,
        resolved: _ResolvedOperationalControl,
    ) -> GatewayResponse:
        return await self._responses.await_terminal_response(
            correlation_id=resolved.correlation_id,
            session_key=resolved.session_key,
            query_handler=None,
        )

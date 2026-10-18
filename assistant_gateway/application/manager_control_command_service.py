"""Interactive manager-control command application."""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from assistant_gateway.application.commands import (
    ManagerControlCommandRequest,
    ManagerControlCommandResult,
)
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.operational_control import (
    GatewayStopControl,
    GatewayStopControlRequest,
)
from assistant_gateway.application.request_context import GatewayRequestRoute
from assistant_gateway.application.schedule_management import GatewayScheduleApplication
from assistant_gateway.application.website_application import (
    WebHostedMCPServerListRequestV1,
    WebHostedMCPServerListV1,
    WebMCPConnectionListRequestV1,
    WebMCPConnectionListV1,
    WebMCPLifecycleRequestV1,
    WebMCPLifecycleResultV1,
)

LOGGER = logging.getLogger(__name__)
STOP_USAGE = (
    "Usage: /stop, /stop task <task_id>, /stop conversation, "
    "/stop session, /stop all, or /stop objective <objective_id>"
)
OBJECTIVE_USAGE = (
    "Usage: /objectives, /pause objective <objective_id>, "
    "/resume objective <objective_id>, /stop objective <objective_id>, "
    "/abandon objective <objective_id>, or /delete objective <objective_id>"
)
SCHEDULE_USAGE = (
    "Usage: /schedules, /pause schedule <schedule_id> <until_iso>, "
    "/resume schedule <schedule_id>, /disable schedule <schedule_id>, "
    "or /delete schedule <schedule_id>"
)
MCP_USAGE = (
    "Usage: /mcp, /mcp connection disable|remove <connection_id>, or "
    "/mcp hosted install|start|stop|remove <hosted_server_id>"
)
MCPControlAction = Literal["disable", "remove", "install", "start", "stop"]


class GatewayMCPControls(Protocol):
    """Authenticated MCP inventory and lifecycle surface used by commands."""

    def list_web_chat_mcp_connections(
        self,
        request: WebMCPConnectionListRequestV1,
    ) -> WebMCPConnectionListV1: ...

    def list_web_chat_hosted_mcp_servers(
        self,
        request: WebHostedMCPServerListRequestV1,
    ) -> WebHostedMCPServerListV1: ...

    async def control_web_chat_mcp_lifecycle(
        self,
        request: WebMCPLifecycleRequestV1,
    ) -> WebMCPLifecycleResultV1: ...


@dataclass(frozen=True)
class _RuntimeControlSelection:
    action: str
    scope: str
    target_task_id: str | None = None
    target_objective_id: str | None = None


@dataclass(frozen=True)
class _ScheduleControlSelection:
    action: str
    schedule_id: str | None = None
    paused_until: str | None = None


@dataclass(frozen=True)
class _MCPControlTarget:
    resource_kind: Literal["connection", "hosted_server"]
    resource_id: str
    action: MCPControlAction
    digest: str
    lifecycle: str


def _render_mcp_inventory(connections: Any, hosted: Any) -> str:
    lines = ["MCP resources:"]
    for connection in connections:
        actions = ", ".join(connection.available_actions) or "view only"
        lines.append(
            f"- connection {connection.connection_id}: "
            f"{connection.connection_name} ({connection.lifecycle}; {actions})"
        )
    for server in hosted:
        actions = ", ".join(server.available_actions) or "view only"
        lines.append(
            f"- hosted {server.hosted_server_id}: "
            f"{server.server_name} ({server.lifecycle}; {actions})"
        )
    if len(lines) == 1:
        lines.append("No MCP resources are registered.")
    return "\n".join(lines)


def _unavailable_mcp_action(
    *,
    action: str,
    resource_id: str,
    lifecycle: str,
) -> str:
    return (
        f"MCP action {action} is not available for {resource_id} "
        f"in lifecycle {lifecycle}."
    )


def _resolve_connection_control_target(
    *,
    action: str,
    resource_id: str,
    connections: Any,
) -> _MCPControlTarget | str:
    resource = next(
        (item for item in connections if item.connection_id == resource_id),
        None,
    )
    if resource is None:
        return f"MCP resource {resource_id} was not found for this user."
    available = set(resource.available_actions)
    if action not in {"disable", "remove"} or action not in available:
        return _unavailable_mcp_action(
            action=action,
            resource_id=resource_id,
            lifecycle=resource.lifecycle,
        )
    return _MCPControlTarget(
        resource_kind="connection",
        resource_id=resource_id,
        action=cast(MCPControlAction, action),
        digest=resource.record_digest,
        lifecycle=resource.lifecycle,
    )


def _resolve_hosted_control_target(
    *,
    action: str,
    resource_id: str,
    hosted: Any,
) -> _MCPControlTarget | str:
    resource = next(
        (item for item in hosted if item.hosted_server_id == resource_id),
        None,
    )
    if resource is None:
        return f"MCP resource {resource_id} was not found for this user."
    if action not in {"install", "start", "stop", "remove"} or action not in set(
        resource.available_actions
    ):
        return _unavailable_mcp_action(
            action=action,
            resource_id=resource_id,
            lifecycle=resource.lifecycle,
        )
    return _MCPControlTarget(
        resource_kind="hosted_server",
        resource_id=resource_id,
        action=cast(MCPControlAction, action),
        digest=resource.definition_digest,
        lifecycle=resource.lifecycle,
    )


def _resolve_mcp_control_target(
    *,
    parts: list[str],
    connections: Any,
    hosted: Any,
) -> _MCPControlTarget | str:
    if len(parts) != 4 or parts[1] not in {"connection", "hosted"}:
        return MCP_USAGE
    resource_kind, action, resource_id = parts[1:4]
    if resource_kind == "connection":
        return _resolve_connection_control_target(
            action=action,
            resource_id=resource_id,
            connections=connections,
        )
    return _resolve_hosted_control_target(
        action=action,
        resource_id=resource_id,
        hosted=hosted,
    )


def _parse_schedule_control(command: str) -> _ScheduleControlSelection | str | None:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Invalid schedule command: {exc}") from exc
    if not parts:
        return None
    if parts[0] == "/schedules":
        return (
            _ScheduleControlSelection(action="list")
            if len(parts) == 1
            else SCHEDULE_USAGE
        )
    if len(parts) >= 2 and parts[1] != "schedule":
        return None
    if parts[0] == "/pause":
        if len(parts) != 4:
            return SCHEDULE_USAGE
        return _ScheduleControlSelection(
            action="pause",
            schedule_id=parts[2],
            paused_until=parts[3],
        )
    actions = {
        "/resume": "resume",
        "/disable": "disable",
        "/delete": "remove",
    }
    if parts[0] in actions:
        if len(parts) != 3:
            return SCHEDULE_USAGE
        return _ScheduleControlSelection(
            action=actions[parts[0]],
            schedule_id=parts[2],
        )
    return None


def _parse_objective_control(parts: list[str]) -> _RuntimeControlSelection | str | None:
    if not parts:
        return None
    if parts[0] == "/objectives":
        if len(parts) != 1:
            return OBJECTIVE_USAGE
        return _RuntimeControlSelection(
            action="list_objectives",
            scope="objective",
        )
    if parts[0] not in {"/pause", "/resume", "/abandon", "/delete"}:
        return None
    if len(parts) != 3 or parts[1] != "objective":
        return OBJECTIVE_USAGE
    actions = {
        "/pause": "pause_objective",
        "/resume": "resume_objective",
        "/abandon": "abandon_objective",
        "/delete": "delete_objective",
    }
    return _RuntimeControlSelection(
        action=actions[parts[0]],
        scope="objective",
        target_objective_id=parts[2],
    )


def _parse_runtime_control(command: str) -> _RuntimeControlSelection | str:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Invalid stop command: {exc}") from exc
    if not parts or parts[0] not in {
        "/stop",
        "/pause",
        "/resume",
        "/objectives",
        "/abandon",
        "/delete",
    }:
        raise ValueError(f"Unsupported manager-control command: {command}")
    objective_control = _parse_objective_control(parts)
    if objective_control is not None:
        return objective_control
    if len(parts) == 1:
        return _RuntimeControlSelection(action="inventory", scope="user")
    if parts[1] == "objective" and len(parts) == 3:
        return _RuntimeControlSelection(
            action="stop_objective",
            scope="objective",
            target_objective_id=parts[2],
        )
    if parts[1] == "task" and len(parts) == 3:
        return _RuntimeControlSelection(
            action="cancel",
            scope="task",
            target_task_id=parts[2],
        )
    if parts[1] in {"conversation", "session"} and len(parts) == 2:
        return _RuntimeControlSelection(action="cancel", scope=parts[1])
    if parts[1] == "all" and len(parts) == 2:
        return _RuntimeControlSelection(action="cancel", scope="user")
    return STOP_USAGE


def _render_schedule_inventory(schedules: Any) -> str:
    lines = ["Schedules:"] if schedules else ["No schedules are owned by this user."]
    for schedule in schedules:
        state = "enabled" if schedule.enabled else "disabled"
        if schedule.paused_until:
            state = f"paused until {schedule.paused_until}"
        label = schedule.schedule_name or schedule.schedule_id
        next_fire = f", next {schedule.next_fire_at}" if schedule.next_fire_at else ""
        lines.append(f"- {schedule.schedule_id}: {label} ({state}{next_fire})")
    return "\n".join(lines)


class GatewayManagerControlCommandService:
    """Own commands that emit operational manager control."""

    def __init__(
        self,
        controls: GatewayStopControl,
        schedules: GatewayScheduleApplication | None = None,
        mcp_controls: GatewayMCPControls | None = None,
    ) -> None:
        self._controls = controls
        self._schedules = schedules
        self._mcp_controls = mcp_controls

    async def execute(
        self,
        request: ManagerControlCommandRequest,
    ) -> ManagerControlCommandResult:
        command = request.command.strip()
        if command == "/mcp" or command.startswith("/mcp "):
            return await self._execute_mcp_control(request)
        schedule_selection = _parse_schedule_control(command)
        if schedule_selection is not None:
            return await self._execute_schedule_control(
                request,
                schedule_selection,
            )
        selection = _parse_runtime_control(command)
        if isinstance(selection, str):
            return ManagerControlCommandResult(
                session_key=request.current_session_key,
                text=selection,
            )

        actor = request.actor
        LOGGER.info(
            "Interactive runtime control received. event=session_runtime_control_received "
            "session_key=%s channel_name=%s user_id=%s chat_id=%s thread_id=%s",
            request.current_session_key,
            actor.channel_name,
            actor.user_id,
            actor.chat_id,
            actor.thread_id,
        )
        expected_objective_digest = None
        if selection.target_objective_id:
            inventory = await self._controls.stop(
                GatewayStopControlRequest(
                    session_key=request.current_session_key,
                    action="list_objectives",
                    scope="objective",
                    command_text="/objectives",
                    route=GatewayRequestRoute(
                        channel_name=actor.channel_name,
                        user_id=actor.user_id,
                        chat_id=actor.chat_id,
                        thread_id=actor.thread_id,
                        canonical_user_id=actor.canonical_user_id,
                    ),
                    reason="Resolve exact Objective state for an interactive control.",
                )
            )
            inventory_content = getattr(inventory.raw_envelope, "msg_content", None)
            objectives = getattr(inventory_content, "objectives", None)
            if not isinstance(objectives, (list, tuple)):
                return ManagerControlCommandResult(
                    session_key=request.current_session_key,
                    text="Objective inventory was unavailable; no action was applied.",
                )
            objective = next(
                (
                    item
                    for item in objectives
                    if getattr(item, "objective_id", None)
                    == selection.target_objective_id
                ),
                None,
            )
            if objective is None:
                return ManagerControlCommandResult(
                    session_key=request.current_session_key,
                    text=(
                        "Objective was not found for this user: "
                        f"{selection.target_objective_id}."
                    ),
                )
            expected_objective_digest = str(objective.record_digest)
        response = await self._controls.stop(
            GatewayStopControlRequest(
                session_key=request.current_session_key,
                action=selection.action,
                scope=selection.scope,
                target_task_id=selection.target_task_id,
                target_objective_id=selection.target_objective_id,
                expected_objective_digest=expected_objective_digest,
                command_text=command,
                route=GatewayRequestRoute(
                    channel_name=actor.channel_name,
                    user_id=actor.user_id,
                    chat_id=actor.chat_id,
                    thread_id=actor.thread_id,
                    canonical_user_id=actor.canonical_user_id,
                ),
            )
        )
        return ManagerControlCommandResult(
            session_key=request.current_session_key,
            text=response.text,
        )

    @staticmethod
    def _route_context(
        request: ManagerControlCommandRequest,
    ) -> GatewayRouteContextV1:
        actor = request.actor
        return GatewayRouteContextV1(
            session_key=str(request.current_session_key or ""),
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            canonical_user_id=actor.canonical_user_id,
        )

    async def _execute_mcp_control(
        self,
        request: ManagerControlCommandRequest,
    ) -> ManagerControlCommandResult:
        if self._mcp_controls is None:
            return ManagerControlCommandResult(
                session_key=request.current_session_key,
                text="MCP lifecycle controls are unavailable in this gateway.",
            )
        try:
            parts = shlex.split(request.command)
        except ValueError as exc:
            return ManagerControlCommandResult(
                session_key=request.current_session_key,
                text=f"Invalid MCP command: {exc}",
            )
        context = self._route_context(request)
        connections = self._mcp_controls.list_web_chat_mcp_connections(
            WebMCPConnectionListRequestV1(context=context)
        ).connections
        hosted = self._mcp_controls.list_web_chat_hosted_mcp_servers(
            WebHostedMCPServerListRequestV1(context=context)
        ).servers
        if parts in (["/mcp"], ["/mcp", "list"]):
            return ManagerControlCommandResult(
                session_key=request.current_session_key,
                text=_render_mcp_inventory(connections, hosted),
            )
        target = _resolve_mcp_control_target(
            parts=parts,
            connections=connections,
            hosted=hosted,
        )
        if isinstance(target, str):
            return ManagerControlCommandResult(
                session_key=request.current_session_key,
                text=target,
            )
        result = await self._mcp_controls.control_web_chat_mcp_lifecycle(
            WebMCPLifecycleRequestV1(
                context=context,
                resource_kind=target.resource_kind,
                resource_id=target.resource_id,
                action=target.action,
                expected_digest=target.digest,
                expected_lifecycle=target.lifecycle,
            )
        )
        return ManagerControlCommandResult(
            session_key=request.current_session_key,
            text=(
                f"MCP {result.resource_kind} {result.resource_id} "
                f"is now {result.status}."
            ),
        )

    async def _execute_schedule_control(
        self,
        request: ManagerControlCommandRequest,
        selection: _ScheduleControlSelection | str,
    ) -> ManagerControlCommandResult:
        if isinstance(selection, str):
            return ManagerControlCommandResult(
                session_key=request.current_session_key,
                text=selection,
            )
        if self._schedules is None:
            return ManagerControlCommandResult(
                session_key=request.current_session_key,
                text="Schedule controls are unavailable in this gateway.",
            )
        actor = request.actor
        route = GatewayRequestRoute(
            channel_name=actor.channel_name,
            user_id=actor.user_id,
            chat_id=actor.chat_id,
            thread_id=actor.thread_id,
            canonical_user_id=actor.canonical_user_id,
        )
        if selection.action == "list":
            schedules = await self._schedules.list(
                session_key=request.current_session_key,
                route=route,
                owner_wide=True,
            )
            return ManagerControlCommandResult(
                session_key=request.current_session_key,
                text=_render_schedule_inventory(schedules),
            )
        schedules = await self._schedules.list(
            session_key=request.current_session_key,
            route=route,
            owner_wide=True,
        )
        schedule = next(
            (item for item in schedules if item.schedule_id == selection.schedule_id),
            None,
        )
        if schedule is None:
            return ManagerControlCommandResult(
                session_key=request.current_session_key,
                text=f"Schedule was not found for this user: {selection.schedule_id}.",
            )
        if not self._schedules.may_change_schedule(schedule, route):
            return ManagerControlCommandResult(
                session_key=request.current_session_key,
                text=(
                    f"Schedule {selection.schedule_id} is managed by the runtime; only a team "
                    "administrator can change it."
                ),
            )
        method: Any = getattr(self._schedules, selection.action)
        kwargs: dict[str, Any] = {
            "session_key": request.current_session_key,
            # The digest the manager's store checks (one shared function).
            "expected_record_digest": self._schedules.listing_digest(schedule),
        }
        if selection.action == "pause":
            kwargs["paused_until"] = selection.paused_until
        success = await method(selection.schedule_id or "", **kwargs)
        state = "removed" if selection.action == "remove" else f"{selection.action}d"
        text = (
            f"Schedule {selection.schedule_id} {state}."
            if success
            else "Schedule control failed or the schedule was not found: "
            f"{selection.schedule_id}."
        )
        return ManagerControlCommandResult(
            session_key=request.current_session_key,
            text=text,
        )

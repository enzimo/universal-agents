"""
Helpers for synthesizing message metadata, headers, and routing keys.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Union

from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SecureCredentialCollectionResult,
    ToolSetupRequestV1,
    ToolSetupResultV1,
    SignupAutomationRequest,
    SignupAutomationResult,
)
from custom_messages.contracts.envelope_v1 import CommsMessage, ContentType
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import AfterActionReport, Task, TaskQuery
from custom_messages.enums import MessageMetaType, RequesterType, RoutingType

logger = logging.getLogger(__name__)

RepairRecorder = Callable[..., Awaitable[None]]


class OutgoingRoutingError(RuntimeError):
    """Raised when an outgoing requester-bound message has no safe destination."""


def is_response_content(content: Optional[ContentType]) -> bool:
    """Return True when the content represents a requester-routed response/follow-up."""
    return (
        (
            hasattr(content, "task_type")
            and getattr(content, "task_type") == MessageMetaType.RESPONSE
        )
        or (
            hasattr(content, "query_type")
            and getattr(content, "query_type") == MessageMetaType.RESPONSE
        )
        or (
            hasattr(content, "result_type")
            and getattr(content, "result_type") == MessageMetaType.RESPONSE
        )
        or isinstance(content, AfterActionReport)
    )


def _content_id(content: Optional[ContentType]) -> Optional[str]:
    if content is None:
        return None
    if hasattr(content, "get_id"):
        try:
            return content.get_id()
        except Exception:
            return None
    return getattr(content, "task_id", None)


def _related_task_id(content: Optional[ContentType]) -> Optional[str]:
    if isinstance(content, Task):
        return content.task_id
    if isinstance(content, TaskQuery):
        return content.parent_task_id
    if isinstance(
        content,
        (
            SecureCredentialCollectionRequest,
            SecureCredentialCollectionResult,
            ToolSetupRequestV1,
            ToolSetupResultV1,
        ),
    ):
        return content.parent_task_id
    if isinstance(content, (SignupAutomationRequest, SignupAutomationResult)):
        return content.parent_task_id
    if isinstance(content, AfterActionReport):
        return content.parent_task_id or content.source_task_id
    return None


def _is_requester_bound_request(content: ContentType) -> bool:
    if isinstance(content, TaskQuery):
        return content.query_type == MessageMetaType.REQUEST
    if isinstance(
        content,
        (
            SecureCredentialCollectionRequest,
            ToolSetupRequestV1,
            SignupAutomationRequest,
        ),
    ):
        return content.request_type == MessageMetaType.REQUEST
    return False


def requester_bound_primary_route(
    content: ContentType,
    incoming_header: MessageHeader,
    *,
    instance_config_path: str | None = None,
    team_id: str | None = None,
    requester_type: RequesterType | None = None,
    sender_is_team_lead: bool = False,
) -> str | None:
    """Give typed user handoffs their trusted reply route before header creation.

    Setup pages, and the team lead's credential forms and signups, go to the
    person's chat: when TeamArchitect works on a specialist's escalation the
    immediate requester is that specialist, and only the gateway can show the
    person a form. Specialists have no credential-form or signup actions
    (``structured_llm_messages.ModelWorkerTurnDecision``); a stray request of
    theirs goes to the agent that delegated the task, which refuses it
    (``task_message_runtime.refuse_credential_request_from_delegate``).
    """
    if not _is_requester_bound_request(content):
        return None
    if isinstance(content, ToolSetupRequestV1) or (
        sender_is_team_lead
        and isinstance(content, (SecureCredentialCollectionRequest, SignupAutomationRequest))
    ):
        return person_chat_route(
            incoming_header, instance_config_path=instance_config_path, team_id=team_id, requester_type=requester_type
        )
    return incoming_header.response_routing_key or incoming_header.from_agent_id or None


def person_chat_route(
    incoming_header: MessageHeader,
    *,
    instance_config_path: str | None,
    team_id: str | None,
    requester_type: RequesterType | None,
) -> str | None:
    """The gateway route that reaches the chat of the person a task works for.

    Setup pages and the setup workflow's notice (``tool_setup_run_runtime``)
    go here. The immediate requester can be a specialist that escalated a
    missing sign-in or account to TeamArchitect; only the gateway can show the
    person anything, so with an instance configuration the message goes
    straight to its configured route.
    """

    if instance_config_path:
        from assistant_gateway.config import GatewayInstanceConfig

        config = GatewayInstanceConfig.load_for_process(instance_config_path)
        route = str(config.manager.response_queue_name or "").strip()
        if config.manager.team_id != team_id or not route:
            raise OutgoingRoutingError(
                "A message to the person's chat needs this team's configured gateway response route."
            )
        return route
    if requester_type == RequesterType.AGENT_REQUESTER:
        raise OutgoingRoutingError(
            "A message to the person's chat from a delegated task needs an instance configuration "
            "with a gateway response route."
        )
    return incoming_header.response_routing_key or incoming_header.from_agent_id or None


def _require_response_route(
    requester_routing_key: str | None,
    *,
    content: ContentType,
    incoming_header: MessageHeader,
    incoming_meta: MessageMeta | None,
    logger: logging.Logger,
    agent_id: str,
) -> str:
    if requester_routing_key:
        return requester_routing_key
    content_id = _content_id(content)
    logger.error(
        "[%s] Cannot route requester-bound %s because no requester route could be resolved. "
        "event=outgoing_response_route_unresolved content_id=%s correlation_id=%s "
        "conversation_id=%s incoming_from_agent_id=%s incoming_response_routing_key=%s",
        agent_id,
        type(content).__name__,
        content_id,
        getattr(incoming_meta, "msg_correlation_id", None),
        getattr(incoming_meta, "conversation_id", None),
        incoming_header.from_agent_id,
        incoming_header.response_routing_key,
    )
    raise OutgoingRoutingError(
        f"Cannot route requester-bound {type(content).__name__} {content_id or 'unknown'}: "
        "the incoming message has no resolvable requester route."
    )


async def _record_repair(
    repair_recorder: Optional[RepairRecorder],
    event: str,
    summary: str,
    *,
    related_task_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    severity: str = "warning",
) -> None:
    if repair_recorder is None:
        return
    try:
        await repair_recorder(
            event,
            summary,
            severity=severity,
            related_task_id=related_task_id,
            details=details or {},
        )
    except Exception:
        logger.exception("Runtime repair anomaly recorder failed for event=%s.", event)


async def build_outgoing_meta(
    *,
    llm_meta: Optional[MessageMeta],
    incoming_meta: Optional[MessageMeta],
    content: Optional[ContentType],
    from_agent_name: str,
    repair_recorder: Optional[RepairRecorder] = None,
) -> MessageMeta:
    """
    Build response metadata for an outgoing LLM-generated message.

    The LLM may omit msg_meta entirely for straightforward Task/TaskQuery
    responses. In that case we still need correlation and conversation ids from
    the originating request.
    """
    outgoing_meta = llm_meta.model_copy(deep=True) if llm_meta else MessageMeta()

    if not outgoing_meta.from_agent_name:
        outgoing_meta.from_agent_name = from_agent_name

    if (
        not outgoing_meta.msg_correlation_id
        and incoming_meta
        and incoming_meta.msg_correlation_id
    ):
        outgoing_meta.msg_correlation_id = incoming_meta.msg_correlation_id

    if (
        not outgoing_meta.conversation_id
        and incoming_meta
        and incoming_meta.conversation_id
    ):
        outgoing_meta.conversation_id = incoming_meta.conversation_id

    if (
        not outgoing_meta.request_timezone
        and incoming_meta
        and incoming_meta.request_timezone
    ):
        outgoing_meta.request_timezone = incoming_meta.request_timezone

    if (
        outgoing_meta.gateway_context is None
        and incoming_meta
        and incoming_meta.gateway_context is not None
    ):
        log_message = (
            "Outgoing response metadata omitted gateway_context; preserving inbound gateway_context "
            "at runtime for correlation_id=%s conversation_id=%s."
        )
        log_args = (
            getattr(incoming_meta, "msg_correlation_id", None),
            getattr(incoming_meta, "conversation_id", None),
        )
        if llm_meta is None:
            logger.debug(log_message, *log_args)
        else:
            logger.warning(log_message, *log_args)
            await _record_repair(
                repair_recorder,
                "outgoing_meta_gateway_context_preserved",
                "Outgoing metadata omitted gateway context; runtime preserved the inbound gateway context.",
                related_task_id=_related_task_id(content),
                details={
                    "content_type": type(content).__name__
                    if content is not None
                    else None,
                    "content_id": _content_id(content),
                    "correlation_id": getattr(
                        incoming_meta, "msg_correlation_id", None
                    ),
                    "conversation_id": getattr(incoming_meta, "conversation_id", None),
                    "from_agent_name": from_agent_name,
                },
            )
        outgoing_meta.gateway_context = incoming_meta.gateway_context.model_copy(
            deep=True
        )

    if is_response_content(content):
        if (
            not outgoing_meta.requester_type
            and incoming_meta
            and incoming_meta.requester_type
        ):
            outgoing_meta.requester_type = incoming_meta.requester_type
        outgoing_meta.is_response_requested = False
        outgoing_meta.to_named_agent_type = None
        outgoing_meta.to_agent_name = None
        outgoing_meta.followup_to_task_id = None

    return outgoing_meta


async def resolve_requester_routing_key(
    *,
    incoming_header: MessageHeader,
    incoming_meta: Optional[MessageMeta],
    agent_id_by_name: Dict[str, str],
    named_agent_type_queue_map: Dict[str, str],
    team_id: Optional[str],
    logger: logging.Logger,
    agent_id: str,
    repair_recorder: Optional[RepairRecorder] = None,
    related_task_id: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve the original requester's reply route.

    Prefer explicit response routing, then sender id. Some callers omit
    `from_agent_id` and rely on `from_agent_name` matching a known route.
    """
    if incoming_header.response_routing_key:
        return incoming_header.response_routing_key

    if incoming_header.from_agent_id:
        return incoming_header.from_agent_id

    requester_name = getattr(incoming_meta, "from_agent_name", None)
    if not requester_name:
        return None

    routing_key = agent_id_by_name.get(requester_name)
    if routing_key:
        logger.info(
            f"[{agent_id}] Resolved requester routing via from_agent_name "
            f"'{requester_name}' -> '{routing_key}'."
        )
        await _record_repair(
            repair_recorder,
            "requester_route_resolved_from_agent_name",
            "Requester reply route was missing from headers; runtime resolved it from msg_meta.from_agent_name.",
            related_task_id=related_task_id,
            details={
                "requester_name": requester_name,
                "resolved_route": routing_key,
                "correlation_id": getattr(incoming_meta, "msg_correlation_id", None),
                "conversation_id": getattr(incoming_meta, "conversation_id", None),
            },
        )
        return routing_key

    routing_key = named_agent_type_queue_map.get(requester_name)
    if routing_key:
        logger.info(
            f"[{agent_id}] Resolved requester routing via queue map key "
            f"'{requester_name}' -> '{routing_key}'."
        )
        await _record_repair(
            repair_recorder,
            "requester_route_resolved_from_queue_map",
            "Requester reply route was missing from headers; runtime resolved it from the named agent route map.",
            related_task_id=related_task_id,
            details={
                "requester_name": requester_name,
                "resolved_route": routing_key,
                "correlation_id": getattr(incoming_meta, "msg_correlation_id", None),
                "conversation_id": getattr(incoming_meta, "conversation_id", None),
            },
        )
        return routing_key

    team_scoped_name = f"{team_id}_{requester_name}" if team_id else requester_name
    routing_key = named_agent_type_queue_map.get(team_scoped_name)
    if routing_key:
        logger.info(
            f"[{agent_id}] Resolved requester routing via team-scoped name "
            f"'{team_scoped_name}' -> '{routing_key}'."
        )
        await _record_repair(
            repair_recorder,
            "requester_route_resolved_from_team_scoped_name",
            "Requester reply route was missing from headers; runtime resolved it from a team-scoped named route.",
            related_task_id=related_task_id,
            details={
                "requester_name": requester_name,
                "team_scoped_name": team_scoped_name,
                "resolved_route": routing_key,
                "correlation_id": getattr(incoming_meta, "msg_correlation_id", None),
                "conversation_id": getattr(incoming_meta, "conversation_id", None),
            },
        )
        return routing_key

    return None


async def resolve_outgoing_routing(
    *,
    content: ContentType,
    header: MessageHeader,
    incoming_header: MessageHeader,
    incoming_meta: Optional[MessageMeta],
    agent_id_by_name: Dict[str, str],
    named_agent_type_queue_map: Dict[str, str],
    team_id: Optional[str],
    logger: logging.Logger,
    agent_id: str,
    repair_recorder: Optional[RepairRecorder] = None,
) -> str:
    """
    Resolve the routing key for an outgoing message.

    For Task/TaskQuery responses, the original request header controls the
    destination so replies reliably return to the requesting user/agent.
    """
    if isinstance(header.to_routing_path, RoutingType):
        routing_key = header.to_routing_path.value
    else:
        routing_key = header.to_routing_path or RoutingType.NULL_ROUTE.value
    header.to_routing_path = routing_key
    if is_response_content(content):
        requester_routing_key = await resolve_requester_routing_key(
            incoming_header=incoming_header,
            incoming_meta=incoming_meta,
            agent_id_by_name=agent_id_by_name,
            named_agent_type_queue_map=named_agent_type_queue_map,
            team_id=team_id,
            logger=logger,
            agent_id=agent_id,
            repair_recorder=repair_recorder,
            related_task_id=_related_task_id(content),
        )
        routing_key = _require_response_route(
            requester_routing_key,
            content=content,
            incoming_header=incoming_header,
            incoming_meta=incoming_meta,
            logger=logger,
            agent_id=agent_id,
        )
        header.to_routing_path = routing_key
        header.response_routing_key = None
    elif (
        _is_requester_bound_request(content)
        and routing_key == RoutingType.NULL_ROUTE.value
    ):
        fallback_routing_key = await resolve_requester_routing_key(
            incoming_header=incoming_header,
            incoming_meta=incoming_meta,
            agent_id_by_name=agent_id_by_name,
            named_agent_type_queue_map=named_agent_type_queue_map,
            team_id=team_id,
            logger=logger,
            agent_id=agent_id,
            repair_recorder=repair_recorder,
            related_task_id=_related_task_id(content),
        )
        if fallback_routing_key:
            content_id = content.get_id() if hasattr(content, "get_id") else "unknown"
            logger.info(
                f"[{agent_id}] Routing requester-bound {type(content).__name__} {content_id} "
                f"back to the original requester via {fallback_routing_key}."
            )
            await _record_repair(
                repair_recorder,
                "requester_bound_request_route_repaired",
                "Requester-bound request had no target route; runtime routed it back to the original requester.",
                related_task_id=_related_task_id(content),
                details={
                    "content_type": type(content).__name__,
                    "content_id": content_id,
                    "fallback_route": fallback_routing_key,
                    "correlation_id": getattr(
                        incoming_meta, "msg_correlation_id", None
                    ),
                    "conversation_id": getattr(incoming_meta, "conversation_id", None),
                },
            )
            routing_key = fallback_routing_key
            header.to_routing_path = fallback_routing_key
    return routing_key


async def construct_header(
    *,
    comms_message: CommsMessage,
    explicit_to_path: Optional[Union[str, RoutingType]],
    route_response_to_self: bool,
    agent_id: str,
    agent_name: str,
    team_id: Optional[str],
    named_agent_type_queue_map: Dict[str, str],
    agent_id_by_name: Dict[str, str],
    logger: logging.Logger,
    repair_recorder: Optional[RepairRecorder] = None,
) -> MessageHeader:
    """
    Construct a MessageHeader for an outgoing message.

    Routing priority:
    1. explicit routing path
    2. `to_named_agent_type`
    3. `to_agent_name`
    4. `NULL_ROUTE`
    """
    target_routing_path: Optional[str] = None

    if explicit_to_path:
        if isinstance(explicit_to_path, RoutingType):
            if explicit_to_path == RoutingType.ALL_AGENTS_ROUTE:
                target_routing_path = team_id
            elif explicit_to_path == RoutingType.SELF_ROUTE:
                target_routing_path = agent_id
            elif explicit_to_path == RoutingType.NULL_ROUTE:
                target_routing_path = RoutingType.NULL_ROUTE.value
            else:
                logger.warning(
                    f"[{agent_id}] Encountered an unhandled RoutingType '{explicit_to_path}' in explicit_to_path. "
                    "Defaulting to NULL_ROUTE."
                )
                await _record_repair(
                    repair_recorder,
                    "explicit_routing_type_defaulted_to_null",
                    "Unhandled explicit RoutingType was converted to NULL_ROUTE.",
                    related_task_id=_related_task_id(comms_message.msg_content),
                    details={
                        "routing_type": str(explicit_to_path),
                        "content_type": type(comms_message.msg_content).__name__,
                        "content_id": _content_id(comms_message.msg_content),
                    },
                )
                target_routing_path = RoutingType.NULL_ROUTE.value
        else:
            target_routing_path = explicit_to_path

    elif comms_message.msg_meta:
        llm_meta = comms_message.msg_meta
        if llm_meta.to_named_agent_type:
            target_routing_path = named_agent_type_queue_map.get(
                llm_meta.to_named_agent_type
            )

            if isinstance(comms_message.msg_content, Task):
                if comms_message.msg_content.task_type == MessageMetaType.RESPONSE:
                    logger.warning(
                        f"[agent: {agent_name}] Detected a Task response but routing for "
                        f"`to_named_agent_type`: ({llm_meta.to_named_agent_type}) was set!"
                    )
                    await _record_repair(
                        repair_recorder,
                        "response_target_named_agent_type_ignored",
                        "Task response included a target named agent type; runtime ignored it so the reply route stays requester-owned.",
                        related_task_id=_related_task_id(comms_message.msg_content),
                        details={
                            "content_type": type(comms_message.msg_content).__name__,
                            "content_id": _content_id(comms_message.msg_content),
                            "to_named_agent_type": llm_meta.to_named_agent_type,
                        },
                    )
                    target_routing_path = None

            if not target_routing_path:
                target_routing_path = f"{team_id}_{llm_meta.to_named_agent_type}"
                logger.info(
                    f"[{agent_id}] Queue for '{llm_meta.to_named_agent_type}' not in map. Using fallback: "
                    f"{target_routing_path}"
                )
        if llm_meta.to_agent_name and not target_routing_path:
            target_routing_path = agent_id_by_name.get(llm_meta.to_agent_name)
            if not target_routing_path:
                target_routing_path = named_agent_type_queue_map.get(
                    llm_meta.to_agent_name
                )
            if not target_routing_path:
                logger.warning(
                    f"[{agent_id}] Could not find agent ID for name '{llm_meta.to_agent_name}' from LLM meta. "
                    "Routing will be NULL."
                )
                await _record_repair(
                    repair_recorder,
                    "to_agent_name_unresolved",
                    "Outgoing metadata named an agent that could not be resolved; runtime left the route as NULL.",
                    related_task_id=_related_task_id(comms_message.msg_content),
                    details={
                        "content_type": type(comms_message.msg_content).__name__,
                        "content_id": _content_id(comms_message.msg_content),
                        "to_agent_name": llm_meta.to_agent_name,
                    },
                )

    if target_routing_path is None:
        content_type_name = (
            type(comms_message.msg_content).__name__
            if comms_message
            else "Unknown (No CommsMessage)"
        )
        if is_response_content(comms_message.msg_content):
            logger.debug(
                f"[{agent_id}] Deferring routing-path resolution for outgoing response message type: {content_type_name}."
            )
        else:
            logger.info(
                f"[{agent_id}] No target routing path determined for message content type: {content_type_name}. "
                "Defaulting to NULL_ROUTE."
            )
        target_routing_path = RoutingType.NULL_ROUTE.value

    return MessageHeader(
        from_agent_id=agent_id,
        to_routing_path=target_routing_path,
        response_routing_key=agent_id if route_response_to_self else None,
        agent_signature="",
    )

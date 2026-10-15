"""User scope support for the trusted Strands tool surface."""

from access_control.ownership import (
    PRIVATE_READ,
    RESOURCE_CONVERSATION,
    RESOURCE_USER_MEMORY,
    private_access,
)
import logging
from typing import TYPE_CHECKING, Any, Optional

from custom_agents.pinned_memory_runtime import (
    PinnedMemoryActor,
    PinnedMemoryRequestContext,
    PinnedMemoryService,
    PinnedMemorySource,
)
from custom_tools.runtime_context import (
    active_task_context as _active_task_context,
)

if TYPE_CHECKING:
    from custom_agents.strands_agent import UniversalAgent


logger = logging.getLogger(__name__)


def _pinned_memory_service_for_agent(agent: "UniversalAgent") -> PinnedMemoryService:
    repository = getattr(agent, "_pinned_memory_repository", None)
    if repository is None:
        raise ValueError("Pinned memory repository is not initialized.")
    return PinnedMemoryService(
        repository,
        config=dict(getattr(agent, "memory_config", {}) or {}),
        command_origin="agent_tool",
        logger=getattr(agent, "_logger", None),
    )


def _pinned_memory_context_for_agent(
    agent: "UniversalAgent",
    *,
    canonical_user_id: str = "",
) -> PinnedMemoryRequestContext:
    current_task_id, task_meta = _active_task_context(agent)
    gateway_context = (
        getattr(task_meta, "gateway_context", None) if task_meta is not None else None
    )
    resolved_user_id = str(canonical_user_id or "").strip() or _verified_owner(agent)
    return PinnedMemoryRequestContext(
        team_id=getattr(agent, "team_id", None),
        canonical_user_id=resolved_user_id,
        request_timezone=getattr(task_meta, "request_timezone", None)
        if task_meta is not None
        else None,
        source=PinnedMemorySource(
            conversation_id=getattr(task_meta, "conversation_id", None)
            if task_meta is not None
            else None,
            task_id=current_task_id,
            gateway_session_key=getattr(gateway_context, "session_key", None),
            channel_name=getattr(gateway_context, "channel_name", None),
            chat_id=getattr(gateway_context, "chat_id", None),
            thread_id=getattr(gateway_context, "thread_id", None),
        ),
        actor=PinnedMemoryActor(
            kind="agent",
            id=getattr(agent, "id", None),
            display_name=getattr(agent, "name", None)
            or getattr(agent, "named_agent_type", None),
        ),
    )


def _verified_owner(agent: "UniversalAgent") -> Optional[str]:
    """The person the agent's current task works for, from its signed context only."""

    from custom_agents.access_runtime import verified_person_id

    return verified_person_id(agent)


def _private_user_scope(
    agent: "UniversalAgent",
    explicit_canonical_user_id: str = "",
    *,
    resource_name: str,
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    current_owner = _verified_owner(agent)
    if not current_owner:
        return None, {
            "error": "canonical_user_required",
            "message": f"{resource_name} is available only for registered users.",
        }
    requested_owner = str(explicit_canonical_user_id or "").strip()
    if requested_owner:
        decision = private_access(
            person_id=current_owner,
            owner_id=requested_owner,
            resource_type=RESOURCE_USER_MEMORY,
            resource_id=requested_owner,
            action=PRIVATE_READ,
        )
        if decision.allowed:
            return current_owner, None
        return None, {
            "error": "cross_user_scope_denied",
            "message": f"{resource_name} cannot be accessed for another canonical_user_id.",
        }
    return current_owner, None


def _pinned_memory_scope_denial_payload(denial: dict[str, Any]) -> dict[str, Any]:
    error = str(denial.get("error") or "canonical_user_required")
    message = str(
        denial.get("message") or "User memory is available only for registered users."
    )
    return {"ok": False, "error_code": error, "message": message, "error": message}


def _conversation_store_for_agent(agent: "UniversalAgent") -> Any:
    store = getattr(agent, "_conversation_store", None)
    if store is None:
        raise ValueError("ConversationStore is not initialized.")
    return store


def _current_private_history_owner(agent: "UniversalAgent") -> Optional[str]:
    return _verified_owner(agent)


def _conversation_owner_denial(
    agent: "UniversalAgent",
    *,
    conversation_id: str,
    action: str = PRIVATE_READ,
) -> Optional[dict[str, Any]]:
    current_owner = _current_private_history_owner(agent)
    if not current_owner:
        return {
            "error": "conversation_history_requires_registered_user",
            "message": "Private conversation history is available only for registered users.",
        }
    thread = _conversation_store_for_agent(agent).get_thread(conversation_id)
    if not thread:
        return {"error": f"Conversation {conversation_id} not found"}
    decision = private_access(
        person_id=current_owner,
        owner_id=thread.get("owner_canonical_user_id"),
        resource_type=RESOURCE_CONVERSATION,
        resource_id=conversation_id,
        action=action,
        team_id=thread.get("team_id"),
    )
    if not decision.allowed:
        return {"error": f"Conversation {conversation_id} not found"}
    return None

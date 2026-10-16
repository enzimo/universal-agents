"""Review abandoned memory claims without exposing private memory content."""

import json
from strands import tool

from custom_agents.memory_review_cleanup_service import MemoryReviewCleanupService
from custom_tools.provider_contract import ToolProvider
from custom_tools.runtime_context import active_task_context, get_agent_ref


@tool
def inspect_memory_review_cleanup(limit: int = 100) -> str:
    """List this team's expired or archived review metadata for internal cleanup.

    This is not a list of recurring schedules and never exposes review contents.
    """
    try:
        agent = get_agent_ref()
        if agent is None:
            raise ValueError("Agent reference not set")
        return json.dumps(MemoryReviewCleanupService(agent).inspect(limit), default=str)
    except (ValueError, PermissionError) as exc:
        return json.dumps({"error": str(exc)})


@tool
def maintain_memory_review_jobs(
    review_job_ids: list[str], operation: str, reason: str
) -> str:
    """Archive exact expired reviews, or purge archived content after retention.

    Access is checked on every call. If the call is refused with access_denied,
    follow its next_step. Call request_access with the tool_id and level it
    names only when the next step says to; a block is not lifted by asking.
    Do not ask again after a denial. Neither operation completes or retries
    reviews, changes facts, nor deletes schedules.
    """
    try:
        agent = get_agent_ref()
        if agent is None:
            raise ValueError("Agent reference not set")
        task_id, _ = active_task_context(agent)
        return json.dumps(
            MemoryReviewCleanupService(agent).maintain(
                review_job_ids,
                operation,
                reason,
                task_id or "",
            ),
            default=str,
        )
    except (ValueError, PermissionError) as exc:
        return json.dumps({"error": str(exc), "changed": False})


PROVIDER = ToolProvider(
    "memory_review_maintenance",
    (inspect_memory_review_cleanup, maintain_memory_review_jobs),
)

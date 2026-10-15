"""Paged job metadata and bounded exact-job inspection for memory reviewers.

A review job is made for one person (its source's ``canonical_user_id``) and
holds that person's conversation turns or memory facts. So these tools, and
``resolve_memory_review_job``, show and resolve only jobs made for the person
the task works for, taken from its signed context (``access_control.ownership``,
``RESOURCE_MEMORY_REVIEW``); work for no person sees none. That is what lets
members hold the inspection tools (catalog ``fn:memory_review_inspection``):
MemReviewAgent works for the review schedule's owner or the conversation's
person, and an agent never exceeds the person it works for.
"""

import json
from typing import Any, Optional

from strands import tool

from access_control.ownership import PRIVATE_READ, RESOURCE_MEMORY_REVIEW, private_access
from custom_tools.provider_contract import ToolProvider
from custom_tools.result_pages import page_size, text_page
from custom_tools.runtime_context import get_agent_ref

# Rows read per query while looking for the person's pending jobs: the person
# is inside each job's saved source, so the filter runs here, page by page.
_SCAN_PAGE = 200
_NOT_FOUND = "Review job not found for this team, agent type and person"


def _context() -> tuple[Any, Any]:
    agent = get_agent_ref()
    if (
        agent is None
        or not getattr(agent, "team_id", None)
        or not getattr(agent, "named_agent_type", None)
    ):
        raise ValueError("An identified team worker is required")
    store = getattr(agent, "_memory_review_store", None)
    if store is None:
        raise ValueError("Memory review store not initialized")
    return agent, store


def _task_person(agent: Any) -> Optional[str]:
    from custom_agents.access_runtime import verified_person_id

    return verified_person_id(agent)


def review_job_person(job: Any) -> Optional[str]:
    """The person a review job was made for."""

    return str((job.source_payload or {}).get("canonical_user_id") or "").strip() or None


def review_job_in_scope(agent: Any, job: Any, *, action: str = PRIVATE_READ) -> bool:
    """Whether the task's person may see (read) or resolve (update) this review job."""

    return private_access(
        person_id=_task_person(agent),
        owner_id=review_job_person(job),
        resource_type=RESOURCE_MEMORY_REVIEW,
        resource_id=job.review_job_id,
        action=action,
    ).allowed


def _pending_jobs_for(agent: Any, store: Any, person_id: Optional[str], *, offset: int, count: int) -> list[Any]:
    """Pending jobs of this team and agent type made for ``person_id``, after the first ``offset`` of them.

    Listing compares the job's person directly, the same rule as
    ``private_access``, so other people's jobs are left out without a log
    line each.
    """

    if not person_id:
        return []
    matched: list[Any] = []
    scanned = 0
    while len(matched) < offset + count:
        page = store.list_review_jobs(
            limit=_SCAN_PAGE,
            offset=scanned,
            owner_team_id=agent.team_id,
            owner_named_agent_type=agent.named_agent_type,
            pending_only=True,
        )
        matched.extend(job for job in page if review_job_person(job) == person_id)
        if len(page) < _SCAN_PAGE:
            break
        scanned += len(page)
    return matched[offset : offset + count]


def _summary(job: Any, agent: Any) -> dict[str, Any]:
    return {
        "review_job_id": job.review_job_id,
        "task_id": job.task_id,
        "conversation_id": job.conversation_id,
        "turn_index": job.turn_index,
        "status": job.status,
        "claim_owner_agent_id": job.claim_owner_agent_id,
        "claim_expires_at": job.claim_expires_at,
        "claimed_by_this_agent": job.status == "claimed"
        and job.claim_owner_agent_id == agent.id,
        "has_error": bool(job.error_text),
    }


@tool
def list_pending_memory_review_jobs(limit: int = 20, offset: int = 0) -> str:
    """Page queued/claimed job metadata for this team, agent type and person.

    Only jobs made for the person this task works for are listed. Follow
    next_offset for more jobs. Read a supplied job ID with get_memory_review_job;
    a short page is not evidence that an exact job vanished. Completed, failed,
    skipped, expired and archived reviews are excluded.
    """
    try:
        size = page_size(limit, offset)
        agent, store = _context()
        jobs = _pending_jobs_for(agent, store, _task_person(agent), offset=offset, count=size + 1)
        return json.dumps(
            {
                "team_id": agent.team_id,
                "named_agent_type": agent.named_agent_type,
                "returned": min(len(jobs), size),
                "offset": offset,
                "next_offset": offset + size if len(jobs) > size else None,
                "jobs": [_summary(job, agent) for job in jobs[:size]],
            },
            default=str,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


@tool
def get_memory_review_job(
    review_job_id: str, offset: int = 0, max_chars: int = 4000
) -> str:
    """Read exact job status and a bounded JSON excerpt of its source and error.

    Only jobs made for the person this task works for can be read. Reading
    changes nothing; a queued job (claim_owner_agent_id null) is still yours to
    do, and resolve_memory_review_job claims it when you record the outcome.
    Follow detail.next_offset, keeping its sha256 unchanged across pages. Never
    process a job claimed by another worker.
    """
    try:
        agent, store = _context()
        job = store.get_review_job(review_job_id)
        if (
            job is None
            or job.owner_team_id != agent.team_id
            or job.owner_named_agent_type != agent.named_agent_type
            or not review_job_in_scope(agent, job)
        ):
            return json.dumps({"error": _NOT_FOUND})
        detail = json.dumps(
            {"source_payload": job.source_payload, "error_text": job.error_text},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return json.dumps(
            {
                **_summary(job, agent),
                "detail": text_page(detail, offset=offset, max_chars=max_chars),
            },
            ensure_ascii=False,
            default=str,
        )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


PROVIDER = ToolProvider(
    "memory_review_inspection", (list_pending_memory_review_jobs, get_memory_review_job)
)

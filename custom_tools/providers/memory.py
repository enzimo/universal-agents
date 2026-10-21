"""The team lead's and the memory review agent's memory tools (catalog tool ``fn:memory``).

- ``memory``: pinned memory facts, the team's (coremem) or the person's
  (usermem), one action per call (list, create, replace, delete, undo).
- ``topic_tags``: topic tags on conversations and their turns.
- ``resolve_memory_review_job``: record the outcome of one memory review job.

``memory`` and ``topic_tags`` each replace several functions (owner's decision
2026-10-10): a model request may carry only so many tools, and these were
fourteen. The catalog keeps a level per action (``argument: action``), so the
access hook decides ``memory(action="delete")`` at full as it did
``delete_coremem``, and each tool checks which inputs its action takes
(``custom_tools.action_inputs``). usermem and topic tags always belong to the
person the task works for, from its signed context
(``custom_tools.providers._user_scope``); no input names another person.

A memory change can arrive long after the model read the fact, and another
request may have changed the fact, or deleted it and given its number to a new
fact, meanwhile. So ``replace`` and ``delete`` take the fact's
``record_digest`` from the result the model read (a list, or its own create
or replace), and the store acts only on that exact version
(``storage.pinned_memory_repository``). A change refused because the fact is
no longer that version, and an undo refused because the fact changed after
the event, are failed results with the shared reason ``changed_since_read``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Literal, Optional

from strands import tool

from access_control.ownership import PRIVATE_UPDATE
from custom_agents.pinned_memory_runtime import PINNED_MEMORY_BANKS, PinnedMemoryCommand
from custom_messages.enums import ChangeRefusal
from custom_tools.action_inputs import actions, input_problem
from custom_tools.provider_contract import ToolProvider
from custom_tools.providers._user_scope import (
    _conversation_owner_denial,
    _conversation_store_for_agent,
    _pinned_memory_context_for_agent,
    _pinned_memory_scope_denial_payload,
    _pinned_memory_service_for_agent,
    _private_user_scope,
)
from custom_tools.providers.memory_review_inspection import review_job_in_scope
from custom_tools.runtime_context import get_agent_ref
from custom_tools.tool_results import failed
from storage.conversation_repository import normalize_topic_tag
from storage.pinned_memory_repository import USERMEM_BANK, PinnedMemoryConflictError

logger = logging.getLogger(__name__)

MemoryAction = Literal["list", "create", "replace", "delete", "undo"]
MemoryBank = Literal["coremem", "usermem"]
MemorySort = Literal["memory_id", "newest", "valid_from_utc", "invalid_after_utc"]
TopicTagsAction = Literal["list", "tag_thread", "tag_turn", "untag", "find"]

_FACT_DETAILS = ("tags", "valid_from_utc", "invalid_after_utc", "metadata")
MEMORY_ACTIONS = actions(
    list=((), ("start", "end", "sort")),
    create=(("fact_text",), _FACT_DETAILS),
    replace=(("memory_id", "record_digest"), ("fact_text", *_FACT_DETAILS)),
    delete=(("memory_id", "record_digest"), ()),
    undo=(("event_id",), ()),
)
TOPIC_TAGS_ACTIONS = actions(
    list=((), ("limit",)),
    tag_thread=(("conversation_id", "tags"), ()),
    tag_turn=(("conversation_id", "turn_index", "tags"), ()),
    untag=(("conversation_id", "tags"), ()),
    find=(("tags",), ("limit",)),
)


# -- pinned memory facts ----------------------------------------------------------


def _memory_error(error_code: str, message: str) -> dict[str, Any]:
    return failed({"ok": False, "error_code": error_code, "message": message, "error": message})


def _changed_since_read(action: str, bank: str, memory_id: Optional[int], error: PinnedMemoryConflictError) -> dict[str, Any]:
    """The failed result of a change or undo refused because the fact is no longer the version it was based on."""

    if action == "undo":
        message = f"{error} Nothing was undone. List {bank} again to see the fact as it is now."
    else:
        message = (
            f"{bank} #{memory_id} changed after you read it, so nothing was changed. List {bank} again and, "
            "if the change is still right, send it with the fact's current record_digest."
        )
    return _memory_error(ChangeRefusal.CHANGED_SINCE_READ.value, message)


@tool
def memory(
    action: MemoryAction,
    bank: MemoryBank,
    memory_id: Optional[int] = None,
    record_digest: Optional[str] = None,
    fact_text: Optional[str] = None,
    tags: Optional[List[str]] = None,
    valid_from_utc: Optional[str] = None,
    invalid_after_utc: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    event_id: Optional[str] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    sort: Optional[MemorySort] = None,
) -> str | dict[str, Any]:
    """Read and change pinned memory facts: the team's (coremem) or the person's (usermem).

    coremem holds facts every person on the team sees; usermem holds durable
    facts about the person this task works for. Each fact has a numeric
    memory_id and a record_digest that names its current version; each change
    returns an event_id that undo reverses. A deleted fact's number is given
    to the next new fact.

    Actions:
    - `list`: the active facts; `start` and `end` pick rows (1 to 50 by default), `sort` orders them.
    - `create`: add one fact (`fact_text`), optionally with `tags`, `valid_from_utc`, `invalid_after_utc`
      and `metadata`.
    - `replace`: change fact `memory_id`: any of `fact_text`, `tags`, `valid_from_utc`, `invalid_after_utc`
      and `metadata`; `invalid_after_utc="never"` clears its expiry. Needs `record_digest`.
    - `delete`: remove fact `memory_id`. Needs `record_digest`.
    - `undo`: reverse a create, replace, delete or expiry by its `event_id`, while the fact is still as that
      change left it.

    `record_digest` is the fact's record_digest in the result you based the change on: a `list`, or your own
    create or replace. Facts shown in your instructions carry none; list them first. If the fact changed
    after you read it (another request edited it, or deleted it and its number now names another fact),
    the change is refused with `changed_since_read` and nothing changes: list again and decide with the
    current fact.

    Args:
        action: What to do; see Actions.
        bank: coremem for the team's facts, usermem for the person's.
        memory_id: The fact to replace or delete.
        record_digest: The record_digest of fact memory_id as you read it (replace and delete).
        fact_text: The fact, as one durable statement.
        tags: Topic tags for the fact.
        valid_from_utc: When the fact starts to hold, ISO 8601 UTC.
        invalid_after_utc: When the fact stops holding, ISO 8601 UTC ("never" with replace clears it).
        metadata: Structured details to keep with the fact.
        event_id: The change to undo, from an earlier result.
        start: The first row to list, from 1.
        end: The last row to list.
        sort: How listed facts are ordered.
    """

    agent = get_agent_ref()
    if agent is None:
        return _memory_error("agent_ref_missing", "Agent reference not set")
    inputs = {
        "memory_id": memory_id,
        "record_digest": record_digest,
        "fact_text": fact_text,
        "tags": tags,
        "valid_from_utc": valid_from_utc,
        "invalid_after_utc": invalid_after_utc,
        "metadata": metadata,
        "event_id": event_id,
        "start": start,
        "end": end,
        "sort": sort,
    }
    problem = input_problem("memory", action, MEMORY_ACTIONS, inputs)
    if problem is None and bank not in PINNED_MEMORY_BANKS:
        problem = f"memory takes bank {' or '.join(sorted(PINNED_MEMORY_BANKS))}, not {bank!r}."
    if problem is not None:
        return _memory_error("invalid_input", problem)
    try:
        person = ""
        if bank == USERMEM_BANK:
            # The person the task works for; usermem has no other owner here.
            resolved, denial = _private_user_scope(agent, resource_name="User memory")
            if denial is not None:
                return failed(_pinned_memory_scope_denial_payload(denial))
            person = resolved or ""
        command = PinnedMemoryCommand(
            bank=bank,
            action=action,
            memory_id=memory_id,
            event_id=event_id or None,
            fact_text=fact_text or None,
            tags=tags,
            metadata=metadata,
            valid_from_utc=valid_from_utc or None,
            invalid_after_utc=invalid_after_utc or None,
            sort=sort or "memory_id",
            range_start=start or 1,
            range_end=end or 50,
            expected_record_digest=record_digest or None,
        )
        result = _pinned_memory_service_for_agent(agent).execute(
            command, context=_pinned_memory_context_for_agent(agent, canonical_user_id=person)
        )
        return json.dumps(result, default=str)
    except PinnedMemoryConflictError as exc:
        return _changed_since_read(action, bank, memory_id, exc)
    except Exception as exc:  # noqa: BLE001 - the model gets the reason and may try again
        return _memory_error("pinned_memory_error", str(exc))


# -- topic tags -------------------------------------------------------------------


def _list_tags(agent: Any, person: Optional[str], **inputs: Any) -> dict[str, Any]:
    tags = _conversation_store_for_agent(agent).list_known_thread_tags(
        team_id=getattr(agent, "team_id", None), canonical_user_id=person, limit=inputs["limit"] or 200
    )
    return {"team_id": getattr(agent, "team_id", None), "tags": tags}


def _tag_thread(agent: Any, person: Optional[str], **inputs: Any) -> dict[str, Any]:
    store = _conversation_store_for_agent(agent)
    rows = [
        store.tag_thread(
            conversation_id=inputs["conversation_id"],
            tag=tag,
            team_id=getattr(agent, "team_id", None),
            canonical_user_id=person,
            source="agent_tool",
            created_by_agent_id=getattr(agent, "id", None),
        )
        for tag in inputs["tags"]
    ]
    return {"conversation_id": inputs["conversation_id"], "tags": rows}


def _tag_turn(agent: Any, person: Optional[str], **inputs: Any) -> dict[str, Any]:
    store = _conversation_store_for_agent(agent)
    turn_index = int(inputs["turn_index"])
    rows = [
        store.tag_turn(
            conversation_id=inputs["conversation_id"],
            turn_index=turn_index,
            tag=tag,
            team_id=getattr(agent, "team_id", None),
            canonical_user_id=person,
            source="agent_tool",
            created_by_agent_id=getattr(agent, "id", None),
        )
        for tag in inputs["tags"]
    ]
    return {"conversation_id": inputs["conversation_id"], "turn_index": turn_index, "tags": rows}


def _untag(agent: Any, person: Optional[str], **inputs: Any) -> dict[str, Any]:
    store = _conversation_store_for_agent(agent)
    removed = [
        {
            "tag": normalize_topic_tag(tag),
            "deleted": store.untag_thread(conversation_id=inputs["conversation_id"], tag=normalize_topic_tag(tag)),
        }
        for tag in inputs["tags"]
    ]
    return {"conversation_id": inputs["conversation_id"], "removed": removed}


def _find(agent: Any, person: Optional[str], **inputs: Any) -> dict[str, Any]:
    threads = _conversation_store_for_agent(agent).find_threads_by_tags(
        tags=list(inputs["tags"]),
        team_id=getattr(agent, "team_id", None),
        canonical_user_id=person,
        limit=inputs["limit"] or 10,
    )
    return {"tags": [normalize_topic_tag(tag) for tag in inputs["tags"]], "threads": threads}


# Per action: its handler, whether it needs the task's person (tags are
# private to them), and whether it changes the conversation (its owner check).
_TOPIC_TAG_HANDLERS: dict[str, tuple[Callable[..., dict[str, Any]], bool, bool]] = {
    "list": (_list_tags, True, False),
    "tag_thread": (_tag_thread, True, True),
    "tag_turn": (_tag_turn, True, True),
    "untag": (_untag, False, True),
    "find": (_find, True, False),
}


@tool
def topic_tags(
    action: TopicTagsAction,
    conversation_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    turn_index: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    """See and change the topic tags on conversations, for the person this task works for.

    Actions:
    - `list`: the topic tags in use (up to `limit`, 200 by default).
    - `tag_thread`: add `tags` to conversation `conversation_id`.
    - `tag_turn`: add `tags` to turn `turn_index` of conversation `conversation_id`.
    - `untag`: remove `tags` from conversation `conversation_id`.
    - `find`: conversations tagged with any of `tags` (up to `limit`, 10 by default).

    Args:
        action: What to do; see Actions.
        conversation_id: The conversation to tag or untag.
        tags: Topic tags, such as "travel" or "taxes".
        turn_index: The turn of the conversation to tag.
        limit: How many tags or conversations to return at most.
    """

    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    inputs = {"conversation_id": conversation_id, "tags": tags, "turn_index": turn_index, "limit": limit}
    problem = input_problem("topic_tags", action, TOPIC_TAGS_ACTIONS, inputs)
    if problem is not None:
        return json.dumps({"error": problem, "error_code": "invalid_input"})
    handler, needs_person, changes_conversation = _TOPIC_TAG_HANDLERS[action]
    try:
        person = None
        if needs_person:
            person, denial = _private_user_scope(agent, resource_name="Topic tags")
            if denial is not None:
                return json.dumps(denial)
        if changes_conversation:
            denial = _conversation_owner_denial(agent, conversation_id=str(conversation_id), action=PRIVATE_UPDATE)
            if denial is not None:
                return json.dumps(denial)
        return json.dumps(handler(agent, person, **inputs), default=str)
    except Exception as exc:  # noqa: BLE001 - the model gets the reason and may try again
        return json.dumps({"error": str(exc)})


# -- memory review jobs -------------------------------------------------------------


def _memory_review_job_error_payload(
    *,
    review_job_id: str,
    error_code: str,
    error: str,
    job: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": error,
        "error_code": error_code,
        "review_job_id": review_job_id,
    }
    if job is not None:
        payload.update(
            {
                "status": getattr(job, "status", None),
                "owner_team_id": getattr(job, "owner_team_id", None),
                "owner_named_agent_type": getattr(job, "owner_named_agent_type", None),
                "claim_owner_agent_id": getattr(job, "claim_owner_agent_id", None),
                "claim_expires_at": getattr(job, "claim_expires_at", None),
            }
        )
    return payload


def _memory_review_claim_error_code(job: Any, agent: Any) -> str:
    status = getattr(job, "status", None)
    claim_owner_agent_id = getattr(job, "claim_owner_agent_id", None)
    if status in {"completed", "skipped", "failed"}:
        return "already_terminal"
    if (
        status == "claimed"
        and claim_owner_agent_id
        and claim_owner_agent_id != getattr(agent, "id", None)
    ):
        return "claimed_by_other_agent"
    if status == "queued" and not claim_owner_agent_id:
        return "queued_but_not_claimable"
    return "not_claimable"



_REVIEW_JOB_STATUSES = ("completed", "skipped", "failed")


def _claimed_review_job(agent: Any, store: Any, review_job_id: str) -> tuple[Any, Optional[dict[str, Any]]]:
    """The job, claimed by this agent, or the error payload saying why not.

    Refused when the job is missing, belongs to another team or agent type, is
    made for a person other than the task's (reads as not found), or cannot be
    claimed.
    """

    job = store.get_review_job(review_job_id)
    if job is None:
        return None, _not_found_payload(review_job_id)
    if job.owner_team_id != agent.team_id or job.owner_named_agent_type != agent.named_agent_type:
        return None, _memory_review_job_error_payload(
            review_job_id=review_job_id,
            error_code="wrong_owner",
            error="Memory review job is owned by a different team or role.",
            job=job,
        )
    # A job made for another person is not in view (memory_review_inspection).
    if not review_job_in_scope(agent, job, action=PRIVATE_UPDATE):
        return None, _not_found_payload(review_job_id)
    if job.status == "claimed" and job.claim_owner_agent_id == agent.id:
        return job, None
    claimed_job = store.claim_review_job(
        review_job_id,
        owner_team_id=agent.team_id,
        owner_named_agent_type=agent.named_agent_type,
        claimer_agent_id=agent.id,
    )
    if claimed_job is not None:
        return claimed_job, None
    refreshed = store.get_review_job(review_job_id)
    return None, _memory_review_job_error_payload(
        review_job_id=review_job_id,
        error_code=_memory_review_claim_error_code(refreshed, agent),
        error="Memory review job is not claimable by this agent.",
        job=refreshed,
    )


def _not_found_payload(review_job_id: str) -> dict[str, Any]:
    return _memory_review_job_error_payload(
        review_job_id=review_job_id, error_code="not_found", error="Memory review job not found."
    )


def _finish_review_job(agent: Any, store: Any, review_job_id: str, status: str, error_text: str) -> None:
    if status == "completed":
        store.mark_review_job_completed(review_job_id, claimer_agent_id=agent.id)
    elif status == "skipped":
        store.mark_review_job_skipped(review_job_id, claimer_agent_id=agent.id, error_text=error_text or None)
    else:
        store.mark_review_job_failed(
            review_job_id, claimer_agent_id=agent.id, error_text=error_text or "Memory review failed."
        )


def _mark_reviewed_turns(agent: Any, job: Any, review_job_id: str, status: str) -> None:
    """Record a conversation-window job's outcome on the turns it covered."""

    source_payload = dict(job.source_payload or {})
    range_payload = source_payload.get("reviewed_turn_range")
    conversation_store = getattr(agent, "_conversation_store", None)
    if (
        not isinstance(range_payload, dict)
        or source_payload.get("review_kind") != "conversation_window"
        or conversation_store is None
    ):
        return
    start_turn = int(range_payload.get("start") or 0)
    end_turn = int(range_payload.get("end") or 0)
    if start_turn <= 0 or end_turn < start_turn:
        return
    if status == "failed":
        conversation_store.mark_turns_memory_review_failed(
            conversation_id=job.conversation_id, start_turn=start_turn, end_turn=end_turn, review_job_id=review_job_id
        )
        return
    conversation_store.mark_turns_memory_reviewed(
        conversation_id=job.conversation_id,
        start_turn=start_turn,
        end_turn=end_turn,
        review_job_id=review_job_id,
        reviewed_by_agent_id=getattr(agent, "id", None),
    )


@tool
def resolve_memory_review_job(
    review_job_id: str, status: str = "completed", error_text: str = ""
) -> str:
    """
    Record the outcome of one memory review job for this role: completed, skipped, or failed.

    A queued job (claim_owner_agent_id is null) is claimed for you here, so a
    job your task supplied needs no separate claim step. A job already claimed
    by another worker is refused.

    Only jobs made for the person this task works for can be resolved.
    """
    agent = get_agent_ref()
    if agent is None:
        return json.dumps({"error": "Agent reference not set"})
    store = getattr(agent, "_memory_review_store", None)
    if store is None:
        return json.dumps({"error": "Memory review store not initialized"})
    normalized_status = str(status or "completed").strip().lower()
    if normalized_status not in _REVIEW_JOB_STATUSES:
        return json.dumps(
            {
                "error": f"Unknown memory review job status: {status}",
                "allowed_statuses": list(_REVIEW_JOB_STATUSES),
            }
        )
    try:
        job, refusal = _claimed_review_job(agent, store, review_job_id)
        if refusal is not None:
            return json.dumps(refusal)
        _finish_review_job(agent, store, review_job_id, normalized_status, error_text)
        _mark_reviewed_turns(agent, job, review_job_id, normalized_status)
        updated = store.get_review_job(review_job_id)
        return json.dumps(
            {
                "review_job_id": review_job_id,
                "status": getattr(updated, "status", normalized_status),
                "owner_team_id": job.owner_team_id,
                "owner_named_agent_type": job.owner_named_agent_type,
            },
            default=str,
        )
    except Exception as exc:
        return json.dumps({"error": str(exc)})


PROVIDER = ToolProvider(name="memory", tools=(memory, topic_tags, resolve_memory_review_job))

__all__ = [
    "MEMORY_ACTIONS",
    "PROVIDER",
    "TOPIC_TAGS_ACTIONS",
    "memory",
    "resolve_memory_review_job",
    "topic_tags",
]

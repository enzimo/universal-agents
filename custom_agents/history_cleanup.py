"""TeamLead cleans up a person's own chat history (owner's decision 2026-10-27).

A person's chat transcripts and finished tasks are kept until they are
deleted. A person can ask TeamLead to delete their own: by age (everything
from before a date), by named conversations, or both, and for chats,
finished tasks or both. Nothing else deletes them, apart from removing the
person from the team.

- **Seeing the history.** ``get_history_overview`` (``fn:conversations``,
  read) lists the person's conversations with their size and dates, and how
  much is older than 30, 90, 180 and 365 days. Sizes come from the turn text
  the manager keeps; the chat transcripts are in the gateway's database,
  which this process never opens, and hold about the same text.
- **Staging.** ``plan_history_cleanup`` (``fn:conversations``, write) stages
  an exact plan for the person the task verifiably works for, never for a
  person named in the call: named conversations must be theirs
  (``access_control.ownership``). It leaves out the conversation the request
  was asked in and conversations with work still running, and deletes
  nothing.
- **Applying.** ``apply_change`` applies it as the ``history_cleanup`` change
  of ``fn:history_cleanup``, each person's own tool (``OwnerScope.PERSON``):
  at once when the person gave TeamLead that tool for their own work (in
  Settings > Access, or with ``/access grant``), otherwise after the person
  approves the exact plan in the Inbox (``access_control.changes``); with
  authorization off, after they agree in the conversation. Only the person
  decides the review, workers never get the tool, and in a scheduled run
  only a schedule the person made themselves may stage one.
- **Deleting.** The manager deletes its own records in one transaction
  (``storage.history_cleanup_repository``) and the files of the conversation
  artifacts, then leaves the order for the gateway, which deletes the chat
  transcripts and their copies, sends the person a notice with what was
  deleted and closes the order (``assistant_gateway.history_cleanup``).
  Both sides record an access event. Remembered facts (memory) are kept;
  the person manages them under Memories.

The review card names the conversations by the start of their first request
(up to 60 characters) so the person knows what they approve; once the
request is decided it keeps only ids, dates and counts
(``ChangeProposalV1.effect_after_decision``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

from access_control.entities import AGENT_ROLE_TEAM_LEAD, agent_type_id
from access_control.ownership import PRIVATE_DELETE, RESOURCE_CONVERSATION, private_access
from access_control.changes import ChangeApproval
from custom_agents.governance.authorized_operations import model_digest
from custom_messages.contracts.access_v2 import (
    ChangeApprovalMode,
    ChangeProposalV1,
    GrantKind,
    GrantStatus,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    summary_line,
)
from custom_messages.enums import ScheduleManagementClass
from id_generation import new_id
from storage.access_repository import AccessRepository
from storage.config import StorageSettings
from storage.history_cleanup_repository import (
    MEMORIES_KEPT,
    ConversationSelectionV1,
    HistoryCleanupOrder,
    HistoryCleanupPlanV1,
    HistoryCleanupRepository,
    OrderStatus,
    TaskRow,
    TurnRow,
    remove_recorded_files,
    select_conversation_turns,
    size_words,
    task_family,
    task_ids_to_delete,
)

logger = logging.getLogger(__name__)

HISTORY_CLEANUP_CHANGE_KIND = "history_cleanup"
HISTORY_CLEANUP_TOOL_ID = "fn:history_cleanup"
CHANGE_ID_PREFIX = "history_cleanup"
_CHANGE_ID_PATTERN = re.compile(r"history_cleanup_[0-9a-f]{12}")
PLAN_TTL = timedelta(hours=24)
# How much of a conversation's first request names it on the review card.
TITLE_CHARS = 60
# Conversations listed one per line on the review card; the rest are counted.
LISTED_CONVERSATIONS = 25
MAX_NAMED_CONVERSATIONS = 200
OVERVIEW_AGE_DAYS = (30, 90, 180, 365)
WHAT_CHOICES = ("conversations", "tasks", "both")
AUTHORITY_PERSON_APPROVAL = "person_approval"
AUTHORITY_AGREEMENT = "agreement"
_STALE = (
    "The history this clean-up covers changed after it was staged (for example a conversation got a new "
    "message, or another clean-up ran). Stage it again."
)


class HistoryCleanupRefused(ValueError):
    """A clean-up cannot be staged or applied as asked; the message says why and what to do."""


@lru_cache(maxsize=4)
def _repository(storage: StorageSettings) -> HistoryCleanupRepository:
    return HistoryCleanupRepository(storage)


def cleanup_store(agent: Any) -> HistoryCleanupRepository:
    return _repository(agent.task_manager.storage_settings)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day(value: str) -> str:
    return str(value or "")[:10]


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _title(text: str) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= TITLE_CHARS:
        return collapsed or "(no text)"
    return collapsed[: TITLE_CHARS - 1].rstrip() + "…"


# -- who is asking ------------------------------------------------------------------------


def _actor(agent: Any) -> Any:
    from custom_agents.access_runtime import acting_agent

    actor = acting_agent(agent)
    if actor.agent.role != AGENT_ROLE_TEAM_LEAD:
        raise HistoryCleanupRefused("Only TeamLead cleans up a person's history; workers never do.")
    if not actor.person_id or not actor.task_id:
        raise HistoryCleanupRefused(
            "Chat history belongs to a person, and this task works for nobody. A clean-up is asked for by a person."
        )
    return actor


def _require_own_schedule(agent: Any, person_id: str) -> None:
    """In a scheduled run, only a schedule the person made themselves may clean up their history.

    A schedule people make for themselves is user-managed and owned by them
    (``ScheduleManagementClass.USER``); the runtime's own schedules
    (heartbeat, maintenance, memory review) never clean up anyone's history
    (owner's decision 2026-10-27).
    """

    from custom_agents.access_runtime import verified_task_context
    from custom_agents.schedule_store import owner_canonical_user_id_for_schedule

    context = verified_task_context(agent)
    schedule_id = getattr(context, "schedule_id", None) if context is not None else None
    if not schedule_id:
        return
    store = agent._ensure_schedule_store()
    schedule = store.get_scheduled_message(str(schedule_id))
    if (
        schedule is None
        or schedule.management_class != ScheduleManagementClass.USER
        or owner_canonical_user_id_for_schedule(schedule) != person_id
    ):
        logger.warning(
            "A scheduled run asked for a history clean-up from a schedule the person did not make. "
            "event=history_cleanup_schedule_refused schedule_id=%s person_id=%s",
            schedule_id,
            person_id,
        )
        raise HistoryCleanupRefused(
            "A scheduled run cleans up history only from a schedule the person made themselves."
        )


def _current_conversations(agent: Any) -> set[str]:
    from custom_agents.access_runtime import verified_task_context
    from custom_tools.runtime_context import active_task_context

    found: set[str] = set()
    _task_id, meta = active_task_context(agent)
    conversation = getattr(meta, "conversation_id", None)
    if conversation:
        found.add(str(conversation))
    context = verified_task_context(agent)
    if context is not None and context.conversation_id:
        found.add(str(context.conversation_id))
    return found


def _authority_mode(agent: Any) -> str:
    from custom_agents.access_runtime import runtime_access

    return AUTHORITY_PERSON_APPROVAL if runtime_access(agent).enabled else AUTHORITY_AGREEMENT


# -- seeing the history ---------------------------------------------------------------------


def _conversation_rows(turns: Iterable[TurnRow]) -> list[dict[str, Any]]:
    """One row per conversation (newest first): title, turns, first and last turn, characters."""

    conversations: dict[str, dict[str, Any]] = {}
    for turn in turns:
        entry = conversations.setdefault(
            turn.conversation_id,
            {
                "conversation_id": turn.conversation_id,
                "title": _title(turn.subject_preview),
                "turns": 0,
                "first_at": turn.created_at,
                "last_at": turn.created_at,
                "chars": 0,
            },
        )
        entry["turns"] += 1
        entry["chars"] += turn.chars
        entry["first_at"] = min(entry["first_at"], turn.created_at)
        entry["last_at"] = max(entry["last_at"], turn.created_at)
    return sorted(conversations.values(), key=lambda item: item["last_at"], reverse=True)


def _older_than(
    days: int, now: datetime, turns: list[TurnRow], tasks: list[TaskRow], listed: list[dict[str, Any]]
) -> dict[str, Any]:
    cut = (now - timedelta(days=days)).isoformat()
    old_turns = [turn for turn in turns if turn.created_at < cut]
    old_tasks = [task for task in tasks if task.timestamp < cut]
    return {
        "older_than_days": days,
        "conversations_entirely_older": sum(1 for item in listed if item["last_at"] < cut),
        "turns": len(old_turns),
        "turn_size": size_words(sum(turn.chars for turn in old_turns)),
        "finished_tasks": len(old_tasks),
        "finished_task_size": size_words(sum(task.chars for task in old_tasks)),
    }


def history_overview(agent: Any, *, limit: int = 20) -> dict[str, Any]:
    """The person's conversations and finished tasks, with sizes and ages (module docstring)."""

    actor = _actor(agent)
    store = cleanup_store(agent)
    person_id = str(actor.person_id)
    now = _now()
    turns = store.person_turns(person_id)
    tasks = store.person_tasks(person_id)
    listed = _conversation_rows(turns)
    shown = max(1, min(int(limit or 20), 100))
    current = _current_conversations(agent)
    return {
        "conversations_total": len(listed),
        "turns_total": len(turns),
        "conversation_size": size_words(sum(turn.chars for turn in turns)),
        "finished_tasks_total": len(tasks),
        "finished_task_size": size_words(sum(task.chars for task in tasks)),
        "by_age": [_older_than(days, now, turns, tasks, listed) for days in OVERVIEW_AGE_DAYS],
        "conversations": [
            {
                **{key: value for key, value in item.items() if key != "chars"},
                "first_at": _day(item["first_at"]),
                "last_at": _day(item["last_at"]),
                "size": size_words(item["chars"]),
                "this_conversation": item["conversation_id"] in current,
            }
            for item in listed[:shown]
        ],
        "more_conversations": max(len(listed) - shown, 0),
        "note": (
            "Sizes are the text of each turn; the chat transcripts hold about the same text and are deleted "
            "with their conversation. " + MEMORIES_KEPT
        ),
    }


# -- staging --------------------------------------------------------------------------------


def _parse_older_than(value: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError as exc:
        raise HistoryCleanupRefused(f"older_than must be a date such as 2026-04-01, not {text!r}.") from exc
    if parsed > _now().date():
        raise HistoryCleanupRefused("older_than cannot be in the future.")
    return parsed


def _checked_conversations(person_id: str, store: HistoryCleanupRepository, raw: Iterable[str]) -> tuple[str, ...]:
    """The named conversations, each of which must be the person's own."""

    wanted = tuple(sorted({str(item).strip() for item in raw or () if str(item).strip()}))
    if len(wanted) > MAX_NAMED_CONVERSATIONS:
        raise HistoryCleanupRefused(f"Name at most {MAX_NAMED_CONVERSATIONS} conversations in one clean-up.")
    owned = store.person_conversation_ids(person_id, wanted)
    for conversation_id in wanted:
        decision = private_access(
            person_id=person_id,
            owner_id=person_id if conversation_id in owned else None,
            resource_type=RESOURCE_CONVERSATION,
            resource_id=conversation_id,
            action=PRIVATE_DELETE,
        )
        if not decision.allowed:
            raise HistoryCleanupRefused(
                f"There is no conversation {conversation_id} of this person to delete. Use the ids "
                "get_history_overview lists."
            )
    return wanted


def _selection(
    store: HistoryCleanupRepository,
    *,
    person_id: str,
    named: tuple[str, ...],
    limit_at: str,
    selected_at: str,
    excluded: Iterable[str],
) -> tuple[list[ConversationSelectionV1], list[str]]:
    return select_conversation_turns(
        # Named conversations only, or every conversation of the person.
        store.person_turns(person_id, named or None),
        limit_at=limit_at,
        selected_at=selected_at,
        excluded=excluded,
    )


def _task_selection(
    store: HistoryCleanupRepository, plan: HistoryCleanupPlanV1, *, selections: Iterable[ConversationSelectionV1]
) -> tuple[list[TaskRow], set[str]]:
    """The finished tasks the plan deletes, and the tasks of its deleted turns (with those delegated from them)."""

    person_tasks = store.person_tasks(plan.person_id)
    through = {item.conversation_id: item.through_turn_index for item in selections}
    turn_tasks = {
        turn.task_id
        for turn in store.person_turns(plan.person_id, list(through))
        if turn.turn_index <= through.get(turn.conversation_id, -1) and turn.created_at < plan.selected_at
    }
    excluded_tasks = {turn.task_id for turn in store.person_turns(plan.person_id, list(plan.excluded_conversation_ids))}
    family = task_family(person_tasks, turn_tasks)
    if not plan.deletes_tasks:
        return [], family
    roots = turn_tasks if plan.named_conversation_ids else None
    return task_ids_to_delete(person_tasks, limit_at=plan.limit_at, roots=roots, excluded_roots=excluded_tasks), family


def stage_history_cleanup(
    agent: Any, *, what: str, older_than: str = "", conversation_ids: Iterable[str] = ()
) -> dict[str, Any]:
    """Stage a clean-up of the person's own history; deletes nothing (module docstring)."""

    choice = str(what or "").strip().lower()
    if choice not in WHAT_CHOICES:
        raise HistoryCleanupRefused("what must be conversations, tasks or both.")
    actor = _actor(agent)
    person_id = str(actor.person_id)
    _require_own_schedule(agent, person_id)
    store = cleanup_store(agent)
    cutoff = _parse_older_than(older_than)
    named = _checked_conversations(person_id, store, conversation_ids)
    if cutoff is None and not named:
        raise HistoryCleanupRefused("Say what to delete: older_than (a date), conversation_ids, or both.")
    current = _current_conversations(agent)
    if current & set(named):
        raise HistoryCleanupRefused(
            "This conversation cannot be deleted while TeamLead is answering in it. Ask from another conversation."
        )
    now = _now()
    selected_at = now.isoformat()
    draft = HistoryCleanupPlanV1(
        order_id=new_id(CHANGE_ID_PREFIX),
        team_id=str(actor.agent.team_id),
        person_id=person_id,
        actor_agent_id=actor.agent.agent_id,
        actor_agent_type=actor.agent.agent_type,
        source_task_id=str(actor.task_id),
        authority_mode=_authority_mode(agent),
        what=choice,
        older_than=cutoff,
        named_conversation_ids=named,
        created_at=selected_at,
        expires_at=(now + PLAN_TTL).isoformat(),
        selected_at=selected_at,
    )
    selections, left_out = _selection(
        store,
        person_id=person_id,
        named=named,
        limit_at=draft.limit_at,
        selected_at=selected_at,
        excluded=current,
    )
    running = sorted(set(left_out) & set(named))
    if running:
        raise HistoryCleanupRefused(
            f"Work is still running in {', '.join(running)}; ask again once it has finished."
        )
    draft = draft.model_copy(
        update={
            "excluded_conversation_ids": tuple(sorted(set(left_out) | current)),
            "conversations": tuple(selections),
            "gateway_kept_conversation_ids": tuple(sorted(
                {turn.conversation_id for turn in store.person_turns(person_id)}
                - {item.conversation_id for item in selections} - set(left_out) - current
            )) if cutoff and not named and draft.deletes_conversations else (),
        }
    )
    tasks, _family = _task_selection(store, draft, selections=selections)
    artifacts = store.artifact_rows(selections, selected_at=selected_at) if draft.deletes_conversations else []
    plan = draft.model_copy(
        update={
            "task_count": len(tasks),
            "task_chars": sum(task.chars for task in tasks),
            "artifact_count": len(artifacts),
        }
    )
    # An age-based clean-up of chats always has something to do: the gateway
    # may hold older chat messages outside the manager's conversations.
    chats = plan.deletes_conversations and bool(plan.conversations or (plan.older_than and not named))
    if not chats and not (plan.deletes_tasks and plan.task_count):
        return {"status": "nothing_to_delete", "message": "Nothing of this person's history matches; nothing was staged."}
    plan = plan.model_copy(update={"plan_digest": model_digest(plan)})
    store.save_plan(plan, at=now)
    titles = store.conversation_titles(person_id, [item.conversation_id for item in plan.conversations])
    logger.info(
        "Staged a history clean-up. event=history_cleanup_staged order_id=%s person_id=%s what=%s conversations=%d "
        "tasks=%d older_than=%s",
        plan.order_id,
        person_id,
        plan.what,
        len(plan.conversations),
        plan.task_count,
        plan.older_than,
    )
    person_review = plan.authority_mode == AUTHORITY_PERSON_APPROVAL
    return {
        "status": "staged",
        "change_id": plan.order_id,
        "title": _proposal_title(plan),
        "effect": list(effect_lines(plan, titles)),
        "left_out_conversations": list(plan.excluded_conversation_ids),
        "next_step": (
            "Call apply_change(change_id, reason). If the person let you clean up their history without asking, "
            "it applies at once; otherwise they approve this exact clean-up in the Inbox and your task waits."
            if person_review
            else "Explain the clean-up, get the person's agreement in the conversation, then call "
            "apply_change(change_id, reason, confirmed=true)."
        ),
    }


# -- describing it ---------------------------------------------------------------------------


def _proposal_title(plan: HistoryCleanupPlanV1) -> str:
    return "Delete part of your chat history"


def _summary(plan: HistoryCleanupPlanV1) -> str:
    parts: list[str] = []
    if plan.deletes_conversations and plan.conversations:
        parts.append(_plural(len(plan.conversations), "conversation"))
    if plan.deletes_conversations and plan.older_than and not plan.named_conversation_ids:
        parts.append(f"chat messages from before {plan.older_than.isoformat()}")
    if plan.deletes_tasks and plan.task_count:
        parts.append(_plural(plan.task_count, "finished task"))
    what = " and ".join(parts) if parts else "nothing"
    return summary_line(f"Delete {what} of yours. Cannot be undone.")


def _conversation_line(item: ConversationSelectionV1, name: str) -> str:
    first, last = _day(item.first_at), _day(item.last_at)
    dates = first if first == last else f"{first} to {last}"
    turns = _plural(item.turns, "turn")
    if item.whole:
        return f"{name}: {turns}, {dates}, {size_words(item.text_chars)}."
    return f"{name}: the oldest {turns}, {dates}, {size_words(item.text_chars)}; newer turns stay."


def _conversation_lines(plan: HistoryCleanupPlanV1, titles: dict[str, str] | None) -> list[str]:
    """One line per conversation (the first ``LISTED_CONVERSATIONS``), named by title or, without titles, by id."""

    whole = sum(1 for item in plan.conversations if item.whole)
    lines = [
        f"Deletes {_plural(len(plan.conversations), 'conversation')} ({whole} entirely, the rest only their "
        "oldest turns), with their chat transcripts in every chat app, summaries and saved tool outputs:"
    ]
    for item in plan.conversations[:LISTED_CONVERSATIONS]:
        name = (
            f"“{_title(titles.get(item.conversation_id, ''))}”"
            if titles is not None
            else f"Conversation {item.conversation_id}"
        )
        lines.append(_conversation_line(item, name))
    rest = plan.conversations[LISTED_CONVERSATIONS:]
    if rest:
        lines.append(
            f"…and {_plural(len(rest), 'more conversation')} ({_plural(sum(item.turns for item in rest), 'turn')})."
        )
    return lines


def _task_line(plan: HistoryCleanupPlanV1) -> str:
    if plan.named_conversation_ids:
        scope = "of these conversations"
    else:
        scope = f"from before {plan.older_than.isoformat() if plan.older_than else _day(plan.selected_at)}"
    return (
        f"Deletes {_plural(plan.task_count, 'finished task')} {scope}, with the tasks delegated from them "
        f"({size_words(plan.task_chars)})."
    )


def effect_lines(plan: HistoryCleanupPlanV1, titles: dict[str, str] | None) -> tuple[str, ...]:
    """What the review card says; with ``titles`` None, the lines kept once the request is decided."""

    lines: list[str] = []
    if plan.deletes_conversations and plan.conversations:
        lines += _conversation_lines(plan, titles)
    if plan.deletes_conversations and plan.older_than and not plan.named_conversation_ids:
        lines.append(
            f"Also deletes your other chat messages from before {plan.older_than.isoformat()}, including the "
            "Updates and Memory review threads."
        )
    if plan.deletes_tasks:
        lines.append(_task_line(plan))
    if plan.excluded_conversation_ids:
        lines.append(
            f"Keeps {_plural(len(plan.excluded_conversation_ids), 'conversation')}: the one this was asked in and "
            "any with work still running."
        )
    lines.append(MEMORIES_KEPT)
    lines.append(
        "Deleting cannot be undone. If any of this history changes before you approve, TeamLead stages the "
        "clean-up again."
    )
    return tuple(line[:1000] for line in lines[:40])


class HistoryCleanupService:
    """Describe and apply staged clean-ups (``apply_change``'s adapter calls these)."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.store = cleanup_store(agent)

    def _load(self, change_id: str) -> HistoryCleanupOrder:
        normalized = str(change_id or "").strip()
        if not _CHANGE_ID_PATTERN.fullmatch(normalized):
            raise ValueError("That is not a history clean-up id; use the change_id plan_history_cleanup returned.")
        order = self.store.get(normalized)
        if order is None:
            raise FileNotFoundError(f"There is no staged history clean-up {normalized}; stage it again.")
        plan = order.plan
        if plan.plan_digest != model_digest(plan):
            raise PermissionError("This history clean-up's plan does not match its digest; stage it again.")
        if order.status is not OrderStatus.PLANNED:
            raise FileExistsError(f"History clean-up {normalized} was already applied.")
        if plan.expires_at <= _now().isoformat():
            raise PermissionError("This history clean-up expired before it was applied. Stage it again.")
        actor = _actor(self.agent)
        if (
            plan.actor_agent_id != actor.agent.agent_id
            or plan.team_id != str(actor.agent.team_id)
            or plan.person_id != actor.person_id
            or plan.source_task_id != str(actor.task_id)
        ):
            raise PermissionError("This history clean-up was staged by another agent, person or task.")
        if plan.authority_mode != _authority_mode(self.agent):
            raise PermissionError("Authorization was turned on or off after staging. Stage the clean-up again.")
        return order

    def _verify_current(self, plan: HistoryCleanupPlanV1) -> None:
        """The same turns would be deleted now (before review and again when applying)."""

        selections, _left_out = _selection(
            self.store,
            person_id=plan.person_id,
            named=plan.named_conversation_ids,
            limit_at=plan.limit_at,
            selected_at=plan.selected_at,
            excluded=plan.excluded_conversation_ids,
        )
        tasks, _family = _task_selection(self.store, plan, selections=selections)
        if tuple(selections) != plan.conversations or len(tasks) != plan.task_count:
            raise PermissionError(_STALE)

    def describe(self, change_id: str) -> ChangeProposalV1:
        plan = self._load(change_id).plan
        self._verify_current(plan)
        titles = self.store.conversation_titles(plan.person_id, [item.conversation_id for item in plan.conversations])
        return ChangeProposalV1(
            kind=HISTORY_CLEANUP_CHANGE_KIND,
            change_id=plan.order_id,
            plan_digest=plan.plan_digest,
            title=_proposal_title(plan),
            summary=_summary(plan),
            effect=effect_lines(plan, titles),
            effect_after_decision=effect_lines(plan, None),
            owner_person_id=plan.person_id,
        )

    def apply(self, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        """Apply an approved clean-up (only the change mechanism calls this)."""

        order = self._load(change_id)
        plan = order.plan
        agreement = approval.mode is ChangeApprovalMode.AGREEMENT
        if agreement != (plan.authority_mode == AUTHORITY_AGREEMENT):
            raise PermissionError("This clean-up needs the person's approval, not agreement (or the reverse).")
        self._verify_current(plan)
        artifacts = self.store.artifact_rows(plan.conversations, selected_at=plan.selected_at) if plan.deletes_conversations else []
        record = {**approval.record(), "grant_id": self._standing_grant_id(plan, approval)}
        if not self.store.start_deleting(
            plan.order_id, approval=record, artifact_paths=[path for _id, path in artifacts if path], at=_now()
        ):
            raise FileExistsError(f"History clean-up {plan.order_id} was already applied.")
        stored = self.store.get(plan.order_id)
        assert stored is not None
        result = carry_out_manager_part(self.agent, stored)
        return {
            "status": "applied",
            "change_id": plan.order_id,
            "deleted": result,
            "message": (
                "Deleted from the task history. The chat transcripts are deleted by the gateway within a few "
                "seconds, and the person gets an Inbox notice with what was deleted. " + MEMORIES_KEPT
            ),
        }

    def _standing_grant_id(self, plan: HistoryCleanupPlanV1, approval: ChangeApproval) -> str | None:
        """The person's grant TeamLead used, for the notice's one-click removal; None after a review."""

        if approval.mode is not ChangeApprovalMode.HELD_ACCESS:
            return None
        from custom_agents.access_runtime import runtime_access

        service = runtime_access(self.agent).service
        if service is None:
            return None
        lead = SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(plan.team_id, plan.actor_agent_type))
        for grant in service.store.for_subjects(service.team_id, [lead], statuses=(GrantStatus.ACTIVE,)):
            if (
                grant.kind is GrantKind.ACCESS
                and grant.target.kind is TargetKind.TOOL
                and grant.target.id == HISTORY_CLEANUP_TOOL_ID
                and grant.for_person_id == plan.person_id
            ):
                return grant.grant_id
        return None


# -- deleting -----------------------------------------------------------------------------


def _artifacts_dir(agent: Any) -> Path | None:
    folder = getattr(getattr(agent, "_conversation_store", None), "_artifacts_dir", None)
    return Path(folder) if folder else None


def carry_out_manager_part(agent: Any, order: HistoryCleanupOrder) -> dict[str, Any]:
    """Delete the manager's records an approved order covers, and hand the order to the gateway.

    Runs again safely after a failure (``finish_unfinished_history_cleanups``):
    one transaction deletes the records and journals their result and pending
    vector ids; retries only finish file/vector effects, never replay the
    deletion against a conversation that has started afresh.
    """

    store = cleanup_store(agent)
    plan = order.plan
    saved = store.get(plan.order_id)
    assert saved is not None
    if "rows" not in saved.manager_result:
        tasks, turn_family = _task_selection(store, plan, selections=plan.conversations)
        agent.task_manager.operations_runtime.run_sync(
            store.delete_manager_records,
            plan,
            task_ids=[task.task_id for task in tasks],
            turn_task_ids=sorted(turn_family) if plan.deletes_conversations else [],
            task_chars=sum(task.chars for task in tasks),
        )
        saved = store.get(plan.order_id)
        assert saved is not None
    counts = saved.manager_result["rows"]
    files, size, failed = remove_recorded_files(_artifacts_dir(agent), order.artifact_paths)
    if failed:
        raise OSError(f"{failed} conversation artifact files could not be removed; the clean-up will retry.")
    _forget_vectors(agent, saved.manager_result.get("case_memory_ids", []))
    _end_task_grants(agent, set(saved.manager_result.get("task_grant_ids", [])))
    result = {
        "conversations": sum(1 for item in plan.conversations if item.whole) if plan.deletes_conversations else 0,
        "conversations_partly": sum(1 for item in plan.conversations if not item.whole) if plan.deletes_conversations else 0,
        "turns": counts.get("conversation_task_links", 0),
        "turn_chars": sum(item.text_chars for item in plan.conversations) if plan.deletes_conversations else 0,
        "finished_tasks": counts.get("completed_tasks", 0),
        "finished_task_chars": saved.manager_result["finished_task_chars"],
        "artifact_files": len(order.artifact_paths),
        "artifact_bytes": size,
        "artifact_files_not_removed": failed,
        "rows": counts,
    }
    if store.finish_manager_part(plan.order_id, result=result, at=_now()):
        _record_event(agent, order, result)
        logger.info(
            "Deleted the manager's part of a history clean-up. event=history_cleanup_manager_done order_id=%s "
            "person_id=%s turns=%d finished_tasks=%d artifact_files=%d",
            plan.order_id,
            plan.person_id,
            result["turns"],
            result["finished_tasks"],
            files,
        )
    return result


def _forget_vectors(agent: Any, case_memory_ids: list[str]) -> None:
    if not case_memory_ids:
        return
    repository = getattr(agent, "_memory_graph_repository", None)
    forget = getattr(repository, "forget_case_memory_vectors", None)
    if not callable(forget):
        logger.warning(
            "Case memories of a cleaned-up history have vectors this process cannot open; the clean-up will "
            "retry. event=history_cleanup_vectors_skipped count=%d",
            len(case_memory_ids),
        )
        raise RuntimeError("The memory graph is unavailable; the clean-up will retry its pending vector deletion.")
    forget(case_memory_ids)


def _end_task_grants(agent: Any, task_ids: set[str]) -> None:
    """Access bound to a deleted task can never be used again; end it so it is not listed."""

    from custom_agents.access_runtime import runtime_access

    if not task_ids:
        return
    try:
        access = runtime_access(agent)
        if access.enabled and access.service is not None:
            access.service.end_task_grants(sorted(task_ids))
    except Exception:  # noqa: BLE001 - the grants end anyway with their task context
        logger.exception("Could not end access bound to deleted tasks. event=history_cleanup_task_grants_failed")


def _record_event(agent: Any, order: HistoryCleanupOrder, result: dict[str, Any]) -> None:
    plan = order.plan
    try:
        AccessRepository(agent.task_manager.storage_settings).record_history_cleanup(
            plan.team_id,
            {
                "order_id": plan.order_id,
                "person_id": plan.person_id,
                "side": "manager",
                "what": plan.what,
                "older_than": plan.older_than.isoformat() if plan.older_than else None,
                "conversation_ids": [item.conversation_id for item in plan.conversations],
                "approval": order.approval,
                "counts": {key: value for key, value in result.items() if key != "rows"},
            },
            actor=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(plan.team_id, plan.actor_agent_type)),
            at=_now(),
        )
    except Exception:  # noqa: BLE001 - the log informs; the clean-up already happened
        logger.exception(
            "Could not log a history clean-up in the access log. event=history_cleanup_event_failed order_id=%s",
            plan.order_id,
        )


def finish_unfinished_history_cleanups(agent: Any) -> int:
    """Finish the manager's part of approved clean-ups a stopped run left (manager start and maintenance)."""

    try:
        store = cleanup_store(agent)
        orders = store.deleting(str(agent.team_id))
    except Exception:  # noqa: BLE001 - tried again at the next maintenance run
        logger.exception("Could not list unfinished history clean-ups. event=history_cleanup_resume_failed")
        return 0
    finished = 0
    for order in orders:
        try:
            carry_out_manager_part(agent, order)
            finished += 1
        except Exception:  # noqa: BLE001 - kept; the next run tries again
            logger.exception(
                "Could not finish a history clean-up. event=history_cleanup_resume_failed order_id=%s",
                order.plan.order_id,
            )
    if finished:
        logger.warning(
            "Finished history clean-ups a stopped run had left. event=history_cleanup_resumed count=%d", finished
        )
    return finished


async def resume_history_cleanups(agent: Any) -> int:
    """``finish_unfinished_history_cleanups`` in a worker thread (manager start and maintenance)."""

    return await asyncio.to_thread(finish_unfinished_history_cleanups, agent)


def followup_conversation_deleted(agent: Any, person_id: Optional[str], conversation_id: str) -> bool:
    """Whether an approved clean-up deleted this whole conversation of this person.

    The gateway names a conversation's last task when the person writes in it
    again; until it has applied its part of the clean-up (seconds), that task
    may already be gone here. The conversation then starts afresh instead of
    failing (``custom_agents.conversation_runtime``).
    """

    if not person_id or not conversation_id:
        return False
    try:
        return cleanup_store(agent).conversation_deleted(str(agent.team_id), person_id, conversation_id)
    except Exception:  # noqa: BLE001 - unknown means not deleted; the follow-up then fails as before
        logger.exception("Could not check history clean-ups. event=history_cleanup_lookup_failed")
        return False


__all__ = [
    "CHANGE_ID_PREFIX",
    "HISTORY_CLEANUP_CHANGE_KIND",
    "HISTORY_CLEANUP_TOOL_ID",
    "HistoryCleanupRefused",
    "HistoryCleanupService",
    "MEMORIES_KEPT",
    "carry_out_manager_part",
    "cleanup_store",
    "effect_lines",
    "finish_unfinished_history_cleanups",
    "followup_conversation_deleted",
    "history_overview",
    "resume_history_cleanups",
    "stage_history_cleanup",
]

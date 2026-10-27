"""The gateway's part of a person's history clean-up (``custom_agents.history_cleanup``).

TeamLead's process deletes the manager's records of an approved clean-up and
leaves its order in the manager's database
(``storage.history_cleanup_repository``), which the gateway already opens.
A loop here, running in both authorization modes, applies each waiting order:

1. delete the person's chat records the order covers in the gateway's
   database, in one transaction (``delete_history_records``): transcripts,
   finished requests, closed questions, attachments and delivered messages;
   sessions stop following up the deleted conversations' last tasks, and Web
   Chat threads left empty are removed;
2. delete the chat upload files those attachments named, only inside the
   uploads folder and never through a link;
3. send the person one Inbox notice with what both sides deleted, how it was
   approved and, when TeamLead used the person's standing permission, that
   grant for one-click removal;
4. close the order and log the gateway's side in the access event log.

Every step can run again: the deletion is bounded by the plan, the notice
has the order's id, and only the call that closes the order logs it. The
loop also removes old orders (``HistoryCleanupRepository.prune``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from access_control.entities import agent_type_id
from assistant_gateway.application.web_chat_threads import WebChatThreadKind, thread_kind
from assistant_gateway.application.website_application import WEB_CHAT_CHAT_ID
from assistant_gateway.domain.history import ConversationCut, GatewayHistoryDeleted, GatewayHistoryScope
from assistant_gateway.domain.sessions import SessionRecord
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccessNoticeV1,
    ChangeApprovalMode,
    SubjectKind,
    SubjectRefV1,
)
from storage.access_repository import AccessRepository
from storage.config import StorageSettings
from storage.history_cleanup_repository import (
    MEMORIES_KEPT,
    HistoryCleanupOrder,
    HistoryCleanupPlanV1,
    HistoryCleanupRepository,
    remove_recorded_files,
    size_words,
)

logger = logging.getLogger(__name__)

HISTORY_CLEANUP_INTERVAL_SECONDS = 5.0
PRUNE_INTERVAL = timedelta(hours=1)


@lru_cache(maxsize=4)
def _orders(storage: StorageSettings) -> HistoryCleanupRepository:
    return HistoryCleanupRepository(storage)


def gateway_scope(plan: HistoryCleanupPlanV1) -> GatewayHistoryScope | None:
    """The chat records a plan covers in the gateway's database; None when it deletes only finished tasks."""

    if not plan.deletes_conversations:
        return None
    return GatewayHistoryScope(
        person_id=plan.person_id,
        selected_at=plan.selected_at,
        conversations=tuple(
            ConversationCut(
                conversation_id=item.conversation_id,
                until=item.keep_from or plan.selected_at,
                whole=item.whole,
                latest_task_id=item.latest_task_id,
                keep_correlation_id=item.keep_correlation_id,
            )
            for item in plan.conversations
        ),
        # With no named conversations, every other chat record from before
        # the date goes too (Updates and Memory review threads included).
        older_than=plan.cutoff_at if not plan.named_conversation_ids else None,
        kept_conversation_ids=plan.excluded_conversation_ids,
    )


def _count(count: int, word: str) -> str:
    return f"{count:,} {word if count == 1 else word + 's'}"


def notice_text(order: HistoryCleanupOrder, deleted: GatewayHistoryDeleted) -> tuple[str, str]:
    """The notice's title and summary: what was deleted and how it was approved, never the content."""

    plan, manager = order.plan, order.manager_result
    parts: list[str] = []
    whole = int(manager.get("conversations") or 0)
    partly = int(manager.get("conversations_partly") or 0)
    if whole or partly:
        detail = f"{whole} entirely" + (f", {partly} only their oldest turns" if partly else "")
        parts.append(f"{_count(whole + partly, 'conversation')} ({detail})")
    if deleted.transcript_messages:
        parts.append(f"{_count(deleted.transcript_messages, 'chat message')} ({size_words(deleted.transcript_chars)})")
    if manager.get("finished_tasks"):
        tasks = int(manager["finished_tasks"])
        parts.append(f"{_count(tasks, 'finished task')} ({size_words(int(manager.get('finished_task_chars') or 0))})")
    files = int(manager.get("artifact_files") or 0) + deleted.attachments
    if files:
        parts.append(f"{_count(files, 'saved file')} (tool outputs and uploads)")
    what = "; ".join(parts) if parts else "nothing (it had already been deleted)"
    lines = [f"TeamLead deleted, as you asked: {what}."]
    if plan.older_than:
        lines.append(f"It covered what was from before {plan.older_than.isoformat()}.")
    if plan.excluded_conversation_ids:
        lines.append(
            f"{_count(len(plan.excluded_conversation_ids), 'conversation')} stayed: the one you asked in and any "
            "with work still running."
        )
    mode = str(order.approval.get("mode") or "")
    if mode == ChangeApprovalMode.HELD_ACCESS.value:
        lines.append(
            "TeamLead used the permission you gave it to clean up your history without asking. You can remove "
            "that permission here or in Settings > Access."
        )
    elif mode == ChangeApprovalMode.PERSON_APPROVAL.value:
        lines.append("You approved this clean-up in the Inbox.")
    else:
        lines.append("You agreed to this clean-up in the conversation.")
    lines.append(MEMORIES_KEPT)
    return "TeamLead deleted part of your chat history", " ".join(lines)


class GatewayHistoryCleanup:
    """Apply the gateway's part of waiting clean-up orders (module docstring)."""

    def __init__(
        self,
        *,
        team_id: str,
        storage: StorageSettings,
        state_store: Any,
        uploads_dir: Path,
        proactive_session_key: str,
        send_notice_once: Callable[[AccessNoticeV1], bool],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._team_id = team_id
        self._storage = storage
        self._state_store = state_store
        self._uploads_dir = uploads_dir
        self._proactive_session_key = proactive_session_key
        self._send_notice_once = send_notice_once
        self._clock = clock
        self._last_prune: datetime | None = None

    def removable_thread(self, session: SessionRecord) -> bool:
        """A Web Chat thread the person started (not one of the gateway's delivery threads)."""

        if session.chat_id != WEB_CHAT_CHAT_ID:
            return False
        return thread_kind(session.session_key, proactive_session_key=self._proactive_session_key) is (
            WebChatThreadKind.CONVERSATION
        )

    def apply(self, order: HistoryCleanupOrder) -> GatewayHistoryDeleted:
        """Apply one order's gateway part, tell the person and close it (safe to run again)."""

        plan = order.plan
        scope = gateway_scope(plan)
        deleted = (
            self._state_store.delete_history_records(scope, removable_thread=self.removable_thread)
            if scope is not None
            else GatewayHistoryDeleted()
        )
        files, size, failed = remove_recorded_files(self._uploads_dir, deleted.attachment_paths)
        title, summary = notice_text(order, deleted)
        grant_id = order.approval.get("grant_id") if order.approval.get("mode") == ChangeApprovalMode.HELD_ACCESS.value else None
        self._send_notice_once(
            AccessNoticeV1(
                notice_id=plan.order_id,
                team_id=plan.team_id,
                kind=AccessNoticeKind.HISTORY_CLEANED_UP,
                recipient_person_id=plan.person_id,
                title=title,
                summary=summary[:4000],
                grant_id=grant_id,
                created_at_utc=self._clock(),
            )
        )
        result = {**deleted.counts(), "upload_files": files, "upload_bytes": size, "upload_files_not_removed": failed}
        if _orders(self._storage).finish_gateway_part(plan.order_id, result=result, at=self._clock()):
            self._record(order, result)
            logger.info(
                "Deleted the gateway's part of a history clean-up and told the person. "
                "event=history_cleanup_gateway_done order_id=%s person_id=%s transcript_messages=%d "
                "sessions_removed=%d upload_files=%d",
                plan.order_id,
                plan.person_id,
                deleted.transcript_messages,
                deleted.sessions_removed,
                files,
            )
        return deleted

    def _record(self, order: HistoryCleanupOrder, result: dict[str, Any]) -> None:
        plan = order.plan
        try:
            AccessRepository(self._storage).record_history_cleanup(
                plan.team_id,
                {
                    "order_id": plan.order_id,
                    "person_id": plan.person_id,
                    "side": "gateway",
                    "approval": order.approval,
                    "counts": result,
                },
                actor=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=agent_type_id(plan.team_id, plan.actor_agent_type)),
                at=self._clock(),
            )
        except Exception:  # noqa: BLE001 - the log informs; the clean-up already happened
            logger.exception(
                "Could not log a history clean-up in the access log. event=history_cleanup_event_failed order_id=%s",
                plan.order_id,
            )

    def run_once_sync(self) -> int:
        """Apply every waiting order; returns how many were applied. Failures are logged and retried."""

        store = _orders(self._storage)
        applied = 0
        for order in store.waiting_for_gateway(self._team_id):
            try:
                self.apply(order)
                applied += 1
            except Exception:  # noqa: BLE001 - kept waiting; the next pass tries again
                logger.exception(
                    "Could not apply a history clean-up in the gateway. event=history_cleanup_gateway_failed "
                    "order_id=%s",
                    order.plan.order_id,
                )
        now = self._clock()
        if self._last_prune is None or now - self._last_prune >= PRUNE_INTERVAL:
            self._last_prune = now
            removed = store.prune(self._team_id, now=now)
            if removed:
                logger.info("Removed old history clean-up orders. event=history_cleanup_orders_pruned count=%d", removed)
        return applied

    async def run(self, stop_event: asyncio.Event, *, interval: float = HISTORY_CLEANUP_INTERVAL_SECONDS) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(self.run_once_sync)
            except Exception:  # noqa: BLE001 - keep the loop alive; the next pass tries again
                logger.exception("History clean-up pass failed. event=history_cleanup_pass_failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


__all__ = ["GatewayHistoryCleanup", "HISTORY_CLEANUP_INTERVAL_SECONDS", "gateway_scope", "notice_text"]

"""Track open and recently finished proposal account-link forms in gateway memory.

When a person approves an access proposal that asks for account links, the
gateway opens one or more broker account-link forms for the approver and watches
them until each finishes or expires (``GatewayToolSetups.after_proposal_links``).

This module keeps the live follow-up state per ``request_id``, scoped strictly
to the approving person:

- ``register`` records the opened forms in proposal order for ``approver_person_id``.
- ``record_ended`` removes one form as soon as its broker watch ends and stores
  its plain-language outcome line (never URLs, tokens or session IDs).
- ``snapshot`` returns a ``FollowUpSnapshot`` (``links``, ``outcomes``,
  ``settling``, ``settling_until_utc``) for a viewer: only the approving person
  ever sees their links, settling state or outcomes. Each active link includes
  ``expires_at_utc`` when known so the browser hides the button as soon as the
  form expires. Between ``expires_at_utc`` and
  ``expires_at_utc + WATCH_GRACE + SETTLING_BACKUP_BUFFER`` the link is omitted
  from ``links`` while ``settling`` stays ``True`` so the browser keeps polling
  until ``record_ended`` reports a late completion (including from the broker
  watch's final poll right after ``WATCH_GRACE``). Only if the backup deadline
  passes without ``record_ended`` does the tracker record the plain expiry line
  and log a warning.
- ``prune`` clears finished entries older than ``FINISHED_RETENTION`` and caps
  total tracked requests at ``MAX_TRACKED_REQUESTS``.

All methods are thread-safe because ``GatewayAccessInbox._review`` and
``_inbox`` read on worker threads (``asyncio.to_thread``) while ``decide`` and
form-end callbacks write on the event loop.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import threading
from typing import Any

from assistant_gateway.application.broker_session_watch import WATCH_GRACE
from assistant_gateway.application.tool_setup import account_link_form_expired


_LOGGER = logging.getLogger(__name__)
FINISHED_RETENTION = timedelta(hours=1)
MAX_TRACKED_REQUESTS = 500
# Margin beyond ``WATCH_GRACE`` before the tracker's own expiry fires as a backup.
# ``watch_broker_session`` checks ``now < deadline`` and then sleeps one poll
# interval (2 s by default in ``GatewayCredentialWorkflowSettings``) before its
# final status read (up to the 10 s read timeout in
# ``HttpSecureBroker.get_account_link_status``), so its last report can arrive
# up to about 12 s after ``expires_at_utc + WATCH_GRACE``. Keep this buffer
# above their sum so ``record_ended`` always wins in normal operation while
# keeping a bounded fallback if a watch ever vanishes without reporting.
SETTLING_BACKUP_BUFFER = timedelta(seconds=15)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_utc(value: str | datetime | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to UTC, or return None when missing/invalid."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_utc(value)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _to_utc(parsed)


def form_key(form: Any, tool_id: str, owner: str) -> str:
    """Stable key identifying one opened account-link form within a proposal."""

    session_id = str(getattr(form, "session_id", "") or "").strip()
    if session_id:
        return f"session:{session_id}"
    form_url = str(getattr(form, "form_url", "") or "").strip()
    return f"tool:{tool_id}:{owner}:{form_url}"


@dataclass(frozen=True)
class PendingFollowUpLink:
    """One open proposal follow-up credential link awaiting the approver."""

    key: str
    tool_id: str
    tool_name: str
    label: str
    url: str
    expires_at_utc: datetime | None = None

    def to_view(self) -> dict[str, str]:
        view: dict[str, str] = {"label": self.label, "url": self.url}
        if self.expires_at_utc is not None:
            view["expires_at_utc"] = self.expires_at_utc.isoformat()
        return view


@dataclass(frozen=True)
class FollowUpSnapshot:
    """Viewer-scoped snapshot of a proposal's follow-up credential forms."""

    links: list[dict[str, str]] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    settling: bool = False
    settling_until_utc: str | None = None


@dataclass
class _TrackedProposal:
    approver_person_id: str
    pending: list[PendingFollowUpLink]
    outcomes_by_key: dict[str, str] = field(default_factory=dict)
    updated_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ProposalFollowUpTracker:
    """Thread-safe in-memory tracker for proposal follow-up credential forms."""

    def __init__(
        self,
        *,
        finished_retention: timedelta = FINISHED_RETENTION,
        max_tracked_requests: int = MAX_TRACKED_REQUESTS,
        watch_grace: timedelta = WATCH_GRACE,
        backup_buffer: timedelta = SETTLING_BACKUP_BUFFER,
        logger: logging.Logger | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _TrackedProposal] = {}
        self._finished_retention = finished_retention
        self._max_tracked = max(1, int(max_tracked_requests))
        self._watch_grace = max(timedelta(0), watch_grace)
        self._backup_buffer = max(timedelta(0), backup_buffer)
        self._settling_window = self._watch_grace + self._backup_buffer
        self._logger = logger or _LOGGER

    def register(
        self,
        request_id: str,
        *,
        approver_person_id: str,
        links: Iterable[PendingFollowUpLink],
        now: datetime,
    ) -> None:
        """Record newly opened forms for ``request_id``, scoped to ``approver_person_id``."""

        items = list(links)
        if not request_id or not approver_person_id or not items:
            return
        at = _to_utc(now)
        with self._lock:
            self._entries[request_id] = _TrackedProposal(
                approver_person_id=approver_person_id,
                pending=items,
                outcomes_by_key={},
                updated_at_utc=at,
            )
            self._prune_locked(at)

    def record_ended(self, request_id: str, *, key: str, message: str, now: datetime) -> bool:
        """Remove one finished form and store its plain-language outcome line."""

        at = _to_utc(now)
        clean = " ".join(str(message or "").split())
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None:
                return False
            entry.pending = [item for item in entry.pending if item.key != key]
            if clean:
                entry.outcomes_by_key[key] = clean
            entry.updated_at_utc = at
            self._prune_locked(at)
            return True

    def snapshot(
        self,
        request_id: str,
        *,
        viewer_id: str,
        now: datetime,
    ) -> FollowUpSnapshot:
        """Return the ``FollowUpSnapshot`` visible to ``viewer_id``."""

        if not request_id or not viewer_id:
            return FollowUpSnapshot()
        at = _to_utc(now)
        with self._lock:
            entry = self._entries.get(request_id)
            if entry is None or entry.approver_person_id != viewer_id:
                return FollowUpSnapshot()
            self._expire_entry_locked(request_id, entry, at)
            links: list[dict[str, str]] = []
            latest_settling_deadline: datetime | None = None
            for item in entry.pending:
                if item.expires_at_utc is not None and item.expires_at_utc <= at:
                    deadline = item.expires_at_utc + self._settling_window
                    if latest_settling_deadline is None or deadline > latest_settling_deadline:
                        latest_settling_deadline = deadline
                else:
                    links.append(item.to_view())
            outcomes = list(entry.outcomes_by_key.values())
            return FollowUpSnapshot(
                links=links,
                outcomes=outcomes,
                settling=latest_settling_deadline is not None,
                settling_until_utc=(
                    latest_settling_deadline.isoformat() if latest_settling_deadline is not None else None
                ),
            )

    def prune(self, now: datetime) -> None:
        """Expire elapsed pending forms and evict stale or excess tracked entries."""

        at = _to_utc(now)
        with self._lock:
            self._prune_locked(at)

    def _expire_entry_locked(self, request_id: str, entry: _TrackedProposal, at: datetime) -> None:
        still_pending: list[PendingFollowUpLink] = []
        latest_deadline: datetime | None = None
        for item in entry.pending:
            if item.expires_at_utc is not None:
                deadline = item.expires_at_utc + self._settling_window
                if deadline <= at:
                    self._logger.warning(
                        "Proposal follow-up form expired via tracker backup before the broker watch reported an ending. "
                        "event=gateway_proposal_follow_up_backup_expired request_id=%s form_key=%s tool_id=%s",
                        request_id,
                        item.key,
                        item.tool_id,
                    )
                    entry.outcomes_by_key.setdefault(item.key, account_link_form_expired(item.tool_name))
                    if latest_deadline is None or deadline > latest_deadline:
                        latest_deadline = deadline
                    continue
            still_pending.append(item)
        if latest_deadline is not None:
            entry.pending = still_pending
            entry.updated_at_utc = max(entry.updated_at_utc, latest_deadline)

    def _prune_locked(self, at: datetime) -> None:
        for req_id, entry in self._entries.items():
            self._expire_entry_locked(req_id, entry, at)
        cutoff = at - self._finished_retention
        stale_ids = [
            req_id
            for req_id, entry in self._entries.items()
            if not entry.pending and entry.updated_at_utc <= cutoff
        ]
        for req_id in stale_ids:
            self._entries.pop(req_id, None)
        if len(self._entries) <= self._max_tracked:
            return
        while len(self._entries) > self._max_tracked:
            finished = [
                (entry.updated_at_utc, req_id)
                for req_id, entry in self._entries.items()
                if not entry.pending
            ]
            pool = finished or [(entry.updated_at_utc, req_id) for req_id, entry in self._entries.items()]
            _, victim = min(pool, key=lambda pair: pair[0])
            self._entries.pop(victim, None)


__all__ = [
    "FINISHED_RETENTION",
    "FollowUpSnapshot",
    "MAX_TRACKED_REQUESTS",
    "PendingFollowUpLink",
    "ProposalFollowUpTracker",
    "SETTLING_BACKUP_BUFFER",
    "form_key",
    "parse_utc",
]

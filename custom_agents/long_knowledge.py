"""Knowledge too long for Web Chat, as a note on administrators' Status page.

Web Chat's Knowledge page carries text inline, at most
``knowledge_base.records.INLINE_TEXT_MAX`` (32,768) characters, while agents
read and write whole pages up to ``agent.knowledge.limits.max_body_chars``
(at most 500,000); and one page, with its open notes, links and backlinks, must
fit in one reply (``custom_messages.wire.reply_size``). The team services refuse
what Web Chat cannot show and change nothing (``team_services.knowledge``), so no
text is lost or cut; this note tells administrators how much stored knowledge
people cannot open in Web Chat: pages, earlier revisions, proposals waiting for
review and open notes with longer text, and pages probably too large for one
reply (their text and open notes together; links, backlinks and JSON escapes are
not counted, so it is an estimate). Counts only, never names or text: personal
pages and proposals belong to their person.

Who counts, and why the team lead. The knowledge base is in the manager's
database, which the team services serve to Web Chat. The team services have no
way to report to the Status page, and the gateway does not open the knowledge
store (people's knowledge requests go to the team services). Every agent
process opens it for the knowledge tools, but each would report the same
count; the team lead already reports the team-wide findings (schedules to
check, saved output retention), so it alone counts.

How often. Findings travel in every status report, every 20 seconds
(``custom_agents.status_findings``), and building one runs on the agent's event
loop. Counting reads every stored text, so it runs at most every
``COUNT_INTERVAL`` and in a thread of its own; each report carries the latest
finished count. The first report after a start has none yet, and a count that
fails is logged and tried again after the interval, keeping the last count.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity
from custom_messages.wire.reply_size import REPLY_BODY_MAX_BYTES
from knowledge_base.records import INLINE_TEXT_MAX, LongTextCounts
from runtime_settings.knowledge import KnowledgeWriteLimits

logger = logging.getLogger(__name__)

COUNT_INTERVAL_SECONDS = 600.0
FINDING_ID = "knowledge_too_long_for_web_chat"
_ATTRIBUTE = "_long_knowledge_count"


def _in_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="long_knowledge_count", daemon=True).start()


class LongKnowledgeCount:
    """The latest count of one team's knowledge longer than Web Chat can show (module docstring)."""

    def __init__(
        self,
        repository: Any,
        team_id: str,
        *,
        interval_seconds: float = COUNT_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        start: Callable[[Callable[[], None]], None] = _in_thread,
    ) -> None:
        self._repository = repository
        self._team_id = team_id
        self._interval = interval_seconds
        self._clock = clock
        self._start = start
        self._lock = threading.Lock()
        self._counts: Optional[LongTextCounts] = None
        self._counted_at: Optional[float] = None
        self._counting = False

    def latest(self) -> Optional[LongTextCounts]:
        """The latest finished count; starts a new one when none runs and the last is older than the interval."""

        with self._lock:
            due = not self._counting and (
                self._counted_at is None or self._clock() - self._counted_at >= self._interval
            )
            if due:
                self._counting = True
        if due:
            try:
                self._start(self._count)
            except Exception:  # noqa: BLE001 - logged; the next report tries again
                logger.exception(
                    "Could not start counting the knowledge too long for Web Chat. "
                    "event=long_knowledge_count_not_started team_id=%s",
                    self._team_id,
                )
                with self._lock:
                    self._counting = False
        with self._lock:
            return self._counts

    def _count(self) -> None:
        counts: Optional[LongTextCounts] = None
        try:
            counts = self._repository.count_long_texts(team_id=self._team_id, longer_than=INLINE_TEXT_MAX,
                                                       larger_than_bytes=REPLY_BODY_MAX_BYTES)
        except Exception:  # noqa: BLE001 - logged; the last count stays and the next interval tries again
            logger.exception(
                "Could not count the knowledge too long for Web Chat; the Status page keeps the last count. "
                "event=long_knowledge_count_failed team_id=%s",
                self._team_id,
            )
        with self._lock:
            if counts is not None:
                self._counts = counts
            self._counted_at = self._clock()
            self._counting = False


def long_knowledge_findings(agent: Any, agent_type: str) -> list[StatusFindingV1]:
    """The team lead's note on knowledge Web Chat cannot open; other agents report nothing."""

    repository = getattr(agent, "_knowledge_repository", None)
    if repository is None or not bool(getattr(agent, "is_team_lead", False)):
        return []
    count = getattr(agent, _ATTRIBUTE, None)
    if count is None:
        count = LongKnowledgeCount(repository, str(getattr(agent, "team_id", "") or ""))
        setattr(agent, _ATTRIBUTE, count)
    counts = count.latest()
    if counts is None or counts.total == 0:
        return []
    return [long_knowledge_finding(counts, source=agent_type)]


def long_knowledge_finding(counts: LongTextCounts, *, source: str) -> StatusFindingV1:
    """The note for ``counts`` (at least one): what Web Chat cannot open, and that nothing was cut."""

    default_limit = KnowledgeWriteLimits.model_fields["max_body_chars"].default
    parts = [
        _counted(counts.pages, "page", "pages"),
        _counted(counts.earlier_revisions, "earlier revision of a page", "earlier revisions of pages"),
        _counted(counts.pending_contributions, "proposal waiting for review", "proposals waiting for review"),
        _counted(counts.open_notes, "open note", "open notes"),
    ]
    found = [part for part in parts if part]
    long_total = counts.total - counts.too_large_pages
    findings = []
    if found:
        listed = ", ".join(found[:-1]) + (" and " if len(found) > 1 else "") + found[-1]
        findings.append(f"{listed} {'have' if long_total > 1 else 'has'} longer text.")
    if counts.too_large_pages:
        large = counts.too_large_pages
        findings.append(f"{_counted(large, 'page is', 'pages are')} probably larger than that with "
                        f"{'its' if large == 1 else 'their'} open notes.")
    several = counts.total > 1
    return StatusFindingV1(
        finding_id=FINDING_ID,
        severity=StatusSeverity.NOTE,
        title="Some knowledge is too long to open in Web Chat",
        summary=(
            f"Web Chat's Knowledge page shows at most {INLINE_TEXT_MAX:,} characters of text, and a page with its "
            f"open notes, links and backlinks of at most about {round(REPLY_BODY_MAX_BYTES / 1000):,} KB. "
            f"{' '.join(findings)} People cannot open, change or decide {'them' if several else 'it'} in Web Chat: "
            "such requests are refused and change nothing. Nothing was cut: agents still read and change the whole "
            "text with their knowledge tools, the knowledge curator can still publish or reject proposals, and a "
            "team transfer carries all of it."
        ),
        action=(
            "Nothing is required. To make a page open in Web Chat, ask an agent to split it into smaller linked "
            "pages, or to resolve its open notes. Page texts stay within Web Chat's character limit while "
            f"agent.knowledge.limits.max_body_chars is {INLINE_TEXT_MAX:,} or less (the default is "
            f"{default_limit:,}); text in scripts that take several bytes per character, and long open notes, can "
            "still make a page too large."
        ),
        source=source,
    )


def _counted(number: int, one: str, many: str) -> str:
    if number <= 0:
        return ""
    return f"1 {one}" if number == 1 else f"{number:,} {many}"


__all__ = [
    "COUNT_INTERVAL_SECONDS",
    "FINDING_ID",
    "LongKnowledgeCount",
    "long_knowledge_finding",
    "long_knowledge_findings",
]

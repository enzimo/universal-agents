"""The Status page's note on knowledge too long to open in Web Chat (custom_agents.long_knowledge).

Only the team lead counts, at most every ten minutes and in a thread of its own; every
status report carries the latest finished count; the note gives counts only, says Web
Chat cannot open them and that nothing was cut.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_agents.long_knowledge import COUNT_INTERVAL_SECONDS, FINDING_ID, LongKnowledgeCount, long_knowledge_finding
from custom_agents.status_findings import agent_status_findings
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.status_v1 import StatusSeverity
from custom_messages.enums import MessageMetaType
from custom_messages.wire.reply_size import REPLY_BODY_MAX_BYTES
from knowledge_base.records import INLINE_TEXT_MAX, LongTextCounts
from knowledge_base.service import DraftInput, KnowledgeService
from runtime_settings.knowledge import KnowledgeSettings, KnowledgeWriteLimits
from storage.config import resolve_storage_settings
from storage.knowledge_repository import create_knowledge_repository
from tests.knowledge_support import TEAM, agent


def _now_inline(work) -> None:
    work()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Counts:
    """A store that answers the count and records how often it was asked."""

    def __init__(self, counts: LongTextCounts) -> None:
        self.counts = counts
        self.calls: list[tuple[str, int, int]] = []
        self.fail = False

    def count_long_texts(self, *, team_id: str, longer_than: int, larger_than_bytes: int = 0) -> LongTextCounts:
        self.calls.append((team_id, longer_than, larger_than_bytes))
        if self.fail:
            raise RuntimeError("database is locked")
        return self.counts


def _lead(repository, *, is_team_lead: bool = True) -> SimpleNamespace:
    return SimpleNamespace(named_agent_type="TeamArchitect", team_id=TEAM, is_team_lead=is_team_lead,
                           _knowledge_repository=repository)


def test_the_team_lead_reports_long_knowledge_from_the_store(tmp_path: Path) -> None:
    repository = create_knowledge_repository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    )
    service = KnowledgeService(repository, settings=KnowledgeSettings(limits=KnowledgeWriteLimits(max_body_chars=500_000)))
    page = service.write(agent("sys"), "team/handbook", DraftInput(title="Handbook", body="h" * 100_000)).page
    service.write(agent("sys"), "team/handbook", DraftInput(body="h" * 90_000), base_revision=page.revision)
    service.write(agent("w", person="alice"), "team/handbook", DraftInput(body="p" * 40_000), base_revision=2)
    service.write(agent("sys"), "team/short", DraftInput(title="Short", body="Fits."))
    lead = _lead(repository)
    lead._long_knowledge_count = LongKnowledgeCount(repository, TEAM, start=_now_inline)

    [finding] = [item for item in agent_status_findings(lead) if item.finding_id == FINDING_ID]

    assert finding.severity is StatusSeverity.NOTE and finding.source == "TeamArchitect"
    assert finding.title == "Some knowledge is too long to open in Web Chat"
    assert finding.summary == (
        "Web Chat's Knowledge page shows at most 32,768 characters of text, and a page with its open notes, links "
        "and backlinks of at most about 254 KB. 1 page, 1 earlier revision of a page and 1 proposal waiting for "
        "review have longer text. People cannot open, change or decide them in Web Chat: such requests are refused "
        "and change nothing. Nothing was cut: agents still read and change the whole text with their knowledge "
        "tools, the knowledge curator can still publish or reject proposals, and a team transfer carries all of it."
    )
    assert "agent.knowledge.limits.max_body_chars is 32,768 or less" in finding.action
    # It travels in the agent's status report like the other findings.
    AgentStatus(self_id="lead-1", team_id=TEAM, named_agent_type="TeamArchitect", name="Nova", is_paused=False,
                agent_role="lead", is_team_lead=True, status_type=MessageMetaType.RESPONSE, status_findings=[finding])


def test_only_the_team_lead_counts_and_nothing_long_reports_nothing() -> None:
    store = _Counts(LongTextCounts(pages=2, earlier_revisions=0, pending_contributions=0, open_notes=0))
    worker = _lead(store, is_team_lead=False)
    no_store = _lead(None)
    empty = _lead(_Counts(LongTextCounts(0, 0, 0, 0)))
    empty._long_knowledge_count = LongKnowledgeCount(empty._knowledge_repository, TEAM, start=_now_inline)

    assert [item for item in agent_status_findings(worker) if item.finding_id == FINDING_ID] == []
    assert store.calls == []
    assert [item for item in agent_status_findings(no_store) if item.finding_id == FINDING_ID] == []
    assert [item for item in agent_status_findings(empty) if item.finding_id == FINDING_ID] == []
    assert empty._knowledge_repository.calls == [(TEAM, INLINE_TEXT_MAX, REPLY_BODY_MAX_BYTES)]


def test_the_count_runs_at_most_once_an_interval_and_a_failure_keeps_the_last_count(caplog) -> None:
    store = _Counts(LongTextCounts(pages=1, earlier_revisions=0, pending_contributions=0, open_notes=0))
    clock = _Clock()
    count = LongKnowledgeCount(store, TEAM, clock=clock, start=_now_inline)

    first = count.latest()
    clock.now += COUNT_INTERVAL_SECONDS - 1
    store.counts = LongTextCounts(pages=5, earlier_revisions=0, pending_contributions=0, open_notes=0)
    cached = count.latest()
    clock.now += 1
    fresh = count.latest()
    store.fail = True
    clock.now += COUNT_INTERVAL_SECONDS
    with caplog.at_level(logging.ERROR, logger="custom_agents.long_knowledge"):
        kept = count.latest()

    assert (first.pages, cached.pages, fresh.pages, kept.pages) == (1, 1, 5, 5)
    assert len(store.calls) == 3
    assert "event=long_knowledge_count_failed" in caplog.text


def test_a_status_report_never_waits_for_the_count() -> None:
    """The count runs in a thread of its own; the report carries the latest finished one."""

    release = threading.Event()
    one_page = LongTextCounts(pages=1, earlier_revisions=0, pending_contributions=0, open_notes=0)

    class _SlowStore:
        def count_long_texts(self, *, team_id: str, longer_than: int, larger_than_bytes: int = 0) -> LongTextCounts:
            release.wait(timeout=10)
            return one_page

    count = LongKnowledgeCount(_SlowStore(), TEAM)

    # The count is held, and the report is not.
    assert count.latest() is None
    release.set()
    deadline = time.monotonic() + 10
    while count.latest() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert count.latest() == one_page


@pytest.mark.parametrize(
    ("counts", "listed"),
    [
        (LongTextCounts(1, 0, 0, 0), "1 page has longer text. People cannot open, change or decide it"),
        (LongTextCounts(0, 3, 0, 0), "3 earlier revisions of pages have longer text."),
        (LongTextCounts(2, 0, 1, 1200), "2 pages, 1 proposal waiting for review and 1,200 open notes have longer"),
        (LongTextCounts(0, 0, 0, 0, too_large_pages=1),
         "about 254 KB. 1 page is probably larger than that with its open notes. People cannot open, change or "
         "decide it"),
        (LongTextCounts(1, 0, 0, 0, too_large_pages=2),
         "1 page has longer text. 2 pages are probably larger than that with their open notes. People cannot open, "
         "change or decide them"),
    ],
)
def test_the_note_lists_only_what_is_long(counts: LongTextCounts, listed: str) -> None:
    assert listed in long_knowledge_finding(counts, source="TeamArchitect").summary

"""Preparing KnowledgeCuratorAgent's scheduled runs (system schedule table preparer ``knowledge_curation``).

The gateway installs one curation schedule per active person (signed for that
person) and one team schedule that works for no person
(``custom_messages.system_schedules``). When one fires on the curator, this
checks whether there is anything to review and writes the run's task text:

- a person's run: their pending team contributions and the open notes on their
  own pages;
- the team run: open notes on team pages, broken links, exact duplicates, frozen pages, and
  refused agent writes in the last day.

With nothing to review the run is skipped and the curator is not started for
it (``None``), so the knowledge base costs no model calls while it is quiet.
Counts only reach the task text, never page content.

The team run's preparation also tells administrators, in one Inbox notice,
about team pages frozen in the last day (after an edit war, or by a curator
or person), even if their freeze expired or was lifted before the run, and
agent types with many refused writes (``NOTICE_REFUSED_WRITES``).
Trusted code sends it, not the curator, so the notice does not depend on a
model deciding to report, and it lists page names and counts only. Its id is
derived from the findings and the day, so a repeated run sends it once.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from access_control.people import PERSON_ROLE_ADMIN
from custom_agents.access_runtime import send_runtime_notice_once
from custom_agents.schedule_management import effective_system_schedule_kind
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_messages.contracts.access_v2 import AccessNoticeKind
from custom_messages.contracts.schedule_v1 import ScheduledMessage
from custom_messages.enums import SystemScheduleKind
from knowledge_base.records import ActivityReport, RecentFreeze
from knowledge_base.vocabulary import ContributionStatus, KnowledgeSpace

logger = logging.getLogger(__name__)

_COUNT_LIMIT = 100
_ACTIVITY_HOURS = 24
_STALE_DAYS = 180
# Refused knowledge writes by one agent type in a day that administrators hear
# about. A fixed number, not a setting: a new configuration setting changes the
# settings section of the team transfer, and nobody has needed to tune this.
NOTICE_REFUSED_WRITES = 20
KNOWLEDGE_NOTICE_PREFIX = "knowledge_attention"


def prepare_knowledge_curation_schedule(
    agent: Any, schedule_id: str, scheduled_message: ScheduledMessage
) -> Optional[ScheduledMessage]:
    """The run's task text, or ``None`` to skip a run with nothing to review."""

    repository = getattr(agent, "_knowledge_repository", None)
    if repository is None:
        logger.info(
            "Skipped a knowledge curation run: the knowledge base is off. "
            "event=knowledge_curation_skipped reason=knowledge_off schedule_id=%s",
            schedule_id,
        )
        return None
    team_id = str(getattr(agent, "team_id", "") or "")
    kind = effective_system_schedule_kind(scheduled_message)
    if kind == SystemScheduleKind.KNOWLEDGE_CURATION_PERSON.value:
        text = _person_run(repository, team_id, owner_canonical_user_id_for_schedule(scheduled_message))
    else:
        text = _team_run(agent, repository, team_id)
    if text is None:
        logger.info(
            "Skipped a knowledge curation run with nothing to review. "
            "event=knowledge_curation_skipped reason=nothing_to_review schedule_id=%s kind=%s",
            schedule_id,
            kind,
        )
        return None
    prepared = scheduled_message.model_copy(deep=True)
    prepared.payload.problem_statement = text
    return prepared


def _person_run(repository: Any, team_id: str, person_id: Optional[str]) -> Optional[str]:
    if not person_id:
        raise ValueError("knowledge_curation_schedule_owner_missing")
    pending = repository.list_contributions(
        team_id=team_id, owner_person_id=person_id, status=ContributionStatus.PENDING, limit=_COUNT_LIMIT
    )
    notes = repository.list_open_notes(
        team_id=team_id, space=KnowledgeSpace.PERSONAL, owner_person_id=person_id, limit=_COUNT_LIMIT
    )
    if not pending and not notes:
        return None
    return (
        "Scheduled knowledge curation for the person this run works for. "
        f"Pending team contributions from their work: {len(pending)}. "
        f"Open notes on their own pages: {len(notes)}.\n"
        "Review every pending contribution (list_pending_contributions, read_contribution): publish what helps the "
        "whole team with everything private to this person removed, or reject it with a reason. Then work the open "
        "notes into their personal pages. Follow your instructions for a run for one person."
    )


def _team_run(agent: Any, repository: Any, team_id: str) -> Optional[str]:
    now = datetime.now(timezone.utc)
    notes = repository.list_open_notes(team_id=team_id, space=KnowledgeSpace.TEAM, owner_person_id="", limit=_COUNT_LIMIT)
    problems = repository.page_problems(team_id=team_id, stale_before=now - timedelta(days=_STALE_DAYS), limit=_COUNT_LIMIT)
    activity = repository.activity(team_id=team_id, since=now - timedelta(hours=_ACTIVITY_HOURS), now=now, limit=20)
    _tell_administrators(agent, activity, now)
    refused = sum(sum(item.refused.values()) for item in activity.by_agent_type)
    counts = {
        "open notes on team pages": len(notes),
        "broken links": len(problems.broken_links),
        "stale pages": len(problems.stale_pages),
        "duplicate pages": len(problems.duplicate_pages),
        "frozen pages": len(activity.frozen_pages),
        f"refused agent writes in the last {_ACTIVITY_HOURS} hours": refused,
    }
    if not any(counts.values()):
        return None
    listed = "; ".join(f"{label}: {count}" for label, count in counts.items())
    return (
        "Scheduled knowledge curation of the team pages; this run works for no person. "
        f"{listed}.\n"
        "Use knowledge_activity_report, list_open_knowledge_notes and knowledge_problems, and follow your instructions "
        "for a run for nobody."
    )


def _tell_administrators(agent: Any, activity: ActivityReport, now: datetime) -> bool:
    """One Inbox notice for administrators when pages froze or agents were refused often; True when stored."""

    frozen = [_freeze_notice_line(item, now) for item in activity.recently_frozen]
    refused = [
        f"- {item.agent_type or 'an agent'}: {sum(item.refused.values())} refused ("
        + ", ".join(f"{reason.replace('_', ' ')} {count}" for reason, count in sorted(item.refused.items()))
        + ")"
        for item in activity.by_agent_type
        if sum(item.refused.values()) >= NOTICE_REFUSED_WRITES
    ]
    if not frozen and not refused:
        return False
    sections = []
    if frozen:
        sections.append("Team pages frozen in the last day and their current state:\n" + "\n".join(frozen))
    if refused:
        sections.append(
            f"Agent types with {NOTICE_REFUSED_WRITES} or more refused knowledge writes in the last "
            f"{_ACTIVITY_HOURS} hours:\n" + "\n".join(refused)
        )
    findings = "\n\n".join(sections)
    digest = hashlib.sha256(f"{now.date().isoformat()}\n{findings}".encode("utf-8")).hexdigest()[:32]
    return send_runtime_notice_once(
        agent,
        notice_id=f"{KNOWLEDGE_NOTICE_PREFIX}:{digest}",
        kind=AccessNoticeKind.KNOWLEDGE_NEEDS_ATTENTION,
        title="The knowledge base needs a look",
        summary=(
            f"{findings}\n\nCheck whether an agent "
            "type's instructions or the knowledge write limits (agent.knowledge.limits) need changing, and whether "
            "a frozen page needs a person to decide what it should say."
        ),
        roles=(PERSON_ROLE_ADMIN,),
    )


def _freeze_notice_line(item: RecentFreeze, now: datetime) -> str:
    """Name the event without implying an expired or lifted freeze still blocks edits."""

    until = item.frozen_until_utc
    if until is None:
        state = "freeze was lifted"
    elif datetime.fromisoformat(until) <= now:
        state = f"freeze expired at {until}"
    else:
        state = f"frozen until {until}; agents add notes instead of editing it"
    line = f"- {item.title} ({item.reference}), {state}"
    if item.after_edit_war:
        line += "; a freeze followed repeated edits by several agents"
    return line


__all__ = ["NOTICE_REFUSED_WRITES", "prepare_knowledge_curation_schedule"]

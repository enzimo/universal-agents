"""Saving a lesson from an after-action report review as a knowledge page (``saved_to_knowledge``).

The team lead reviews reports about one person's work in a task signed for
that person, and reports about work for nobody in a task signed for nobody
(``custom_agents.after_action_report_identity``). A lesson worth keeping
beyond a short memory fact becomes a knowledge page through the ordinary
knowledge rules (``knowledge_base.service.KnowledgeService.write``), with
who is acting built from verified identity only:

- in a review of a person's work, a team page change becomes a pending
  contribution of that person's work, published only by the curator working
  for that person or by the person, so their private details never reach the
  team unreviewed;
- in a review of work for nobody, it is published directly, under the rules
  for system work (flagged personal details are refused).

Trusted code checks that the review's person is the person the task works
for before writing, decides the write as the ``knowledge_write`` operation
(the same access decision as the agent's own knowledge tool, so a block on the
knowledge base also stops this), and words the note recorded on the review.
The page and a review-bound receipt commit together before the review is
resolved. A retry or restart reuses that receipt after checking the same
identity, access and input digest, so an interrupted resolution saves no
second lesson. A refused write leaves the review pending for another try.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Optional

from custom_agents.access_runtime import decide_call, refusal_text
from custom_messages.records.reviews import AfterActionReportReviewRecord
from custom_tools.providers.knowledge import knowledge_service, knowledge_actor, knowledge_write
from knowledge_base.records import KnowledgeError, KnowledgeWriteKey, KnowledgeWriteReceipt
from knowledge_base.service import DraftInput

logger = logging.getLogger(__name__)

# The change summary of a lesson's page revision, from trusted code.
LESSON_CHANGE_SUMMARY = "Lesson from an after-action report review"
CODE_PERSON_MISMATCH = "review_person_mismatch"


@dataclass(frozen=True)
class KnowledgeLesson:
    """The page a review saves: its name, title, summary and text, and the revision edited (0 creates)."""

    page: str = ""
    title: str = ""
    # Omitted keeps an existing page's summary; an empty string clears it.
    summary: Optional[str] = None
    body: str = ""
    base_revision: int = 0

    def given(self) -> bool:
        return self.summary is not None or any((self.page, self.title, self.body, self.base_revision))


class LessonRefused(Exception):
    """The lesson was not saved; ``payload`` is the tool's error answer."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("message") or payload.get("error") or "refused"))
        self.payload = payload


def lesson_problem(saving: bool, lesson: KnowledgeLesson) -> str:
    """Why the lesson fields do not fit the chosen status, or an empty string."""

    if not saving:
        return "knowledge_page, knowledge_title, knowledge_summary and knowledge_body are only for saved_to_knowledge." if lesson.given() else ""
    if not lesson.page.strip() or not lesson.body.strip():
        return "saved_to_knowledge needs knowledge_page (such as team/release-lessons) and knowledge_body."
    return ""


def save_review_lesson(agent: Any, review: AfterActionReportReviewRecord, lesson: KnowledgeLesson) -> str:
    """Write the lesson for the review's person; the note to record on the review. Raises ``LessonRefused``."""

    actor = knowledge_actor(agent)
    if actor.work_person_id != (review.person_id or ""):
        logger.warning(
            "Refused to save a review's lesson for another person's work. event=aar_knowledge_person_mismatch "
            "review_id=%s report_id=%s task_id=%s review_has_person=%s task_has_person=%s",
            review.review_id,
            review.report_id,
            actor.task_id,
            bool(review.person_id),
            bool(actor.work_person_id),
        )
        raise LessonRefused(
            {
                "error": "This review is about other work than the work this task does, so its lesson was not saved.",
                "error_code": CODE_PERSON_MISMATCH,
            }
        )
    decision = decide_call(agent, knowledge_write.tool_name, {"page": lesson.page})
    if decision is not None and not decision.allowed:
        raise LessonRefused(json.loads(refusal_text(decision)))
    # The stored review's identity, not a model-supplied operation id, binds
    # retries. A changed lesson never consumes the previous lesson's receipt.
    payload = json.dumps(asdict(lesson), sort_keys=True, separators=(",", ":"))
    write_key = KnowledgeWriteKey(
        operation_id=f"aar:{review.owner_agent_id}:{review.review_id}",
        payload_digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
    try:
        result = knowledge_service(agent).write(
            actor,
            lesson.page,
            DraftInput(title=lesson.title, body=lesson.body, summary=lesson.summary),
            base_revision=int(lesson.base_revision or 0),
            change_summary=LESSON_CHANGE_SUMMARY,
            write_key=write_key,
        )
    except KnowledgeError as error:
        raise LessonRefused(error.payload()) from error
    assert result.receipt is not None
    note = _decision_note(result.receipt)
    logger.info(
        "Saved a review's lesson to the knowledge base. event=aar_knowledge_saved review_id=%s report_id=%s "
        "pending=%s",
        review.review_id,
        review.report_id,
        result.pending,
    )
    return note


def _decision_note(receipt: KnowledgeWriteReceipt) -> str:
    if receipt.contribution_id:
        return (
            f"Saved to knowledge as a proposed change to {receipt.reference} "
            f"(contribution {receipt.contribution_id}); the knowledge curator publishes it "
            "without the person's private details."
        )
    return f"Saved to knowledge as {receipt.reference} (revision {receipt.revision})."


__all__ = [
    "CODE_PERSON_MISMATCH",
    "KnowledgeLesson",
    "LESSON_CHANGE_SUMMARY",
    "LessonRefused",
    "lesson_problem",
    "save_review_lesson",
]

"""AfterActionReport coordination for TaskManager.

Every review and batch records whom the reported work was for
(``ReportedWorkIdentity``, from verified identity only; see
``custom_agents.after_action_report_identity``). Batches are kept per person,
so two people's reports never share a review, and the review tools see only
the reports of the person their review works for. Records saved before the
person was recorded are never reviewed: their batches are closed unreviewed
when they come up (``close_unverified_batch``).
"""

from __future__ import annotations

from custom_agents.task_manager_ports import AfterActionReportCoordinatorOperations
from custom_agents.task_runtime_state import RuntimeOwnerIdentity
import logging
from storage.after_action_report_repository import AfterActionReportRepository

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from custom_agents.task_records import AfterActionReportRecordResult, ReportedWorkIdentity
from custom_messages.contracts.task_v1 import AfterActionReport, Task, TaskHistoryEntry
from custom_messages.enums import AfterActionReportReviewStatus, MessageMetaType, TaskHistorySource, TaskHistoryType
from custom_messages.records.reviews import AfterActionReportReviewBatchRecord, AfterActionReportReviewRecord
from id_generation import new_id

# Why reviews saved before the person was recorded are closed unreviewed.
UNVERIFIED_IDENTITY_STALE_REASON = "recorded_without_verified_person"


class AfterActionReportCoordinator:
    """Manager-owned AfterActionReport review and batching workflow."""

    def __init__(
        self,
        *,
        operations: AfterActionReportCoordinatorOperations,
        identity: RuntimeOwnerIdentity,
        logger: logging.Logger,
        after_action_report_repository: AfterActionReportRepository,
    ) -> None:
        self.operations = operations
        self.identity = identity
        self._logger = logger
        self.after_action_report_repository = after_action_report_repository


    def record_after_action_report(
        self,
        report: AfterActionReport,
        *,
        identity: ReportedWorkIdentity,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Optional[AfterActionReportRecordResult]:
        """Record the report for review, for whom ``identity`` says the reported work was."""

        target_task_id = report.parent_task_id or report.source_task_id
        if not target_task_id:
            self._logger.warning("Cannot record AfterActionReport %s without task linkage.", report.report_id)
            return None

        now = timestamp or datetime.now(timezone.utc).isoformat()
        active_task = self.operations.get_task_info(target_task_id)
        archived_task = None if active_task else self.operations.get_task_from_db(target_task_id)
        if active_task is None and archived_task is None:
            self._logger.warning(
                "Cannot record AfterActionReport %s because task %s is neither active nor archived.",
                report.report_id,
                target_task_id,
            )
            return None

        review = self._build_after_action_report_review(report, now, identity)

        details = {
            "report_id": report.report_id,
            "review_id": review.review_id if review else None,
            "source_task_id": report.source_task_id,
            "parent_task_id": report.parent_task_id,
            "source_agent_id": report.source_agent_id,
            "source_agent_name": report.source_agent_name,
            "source_named_agent_type": report.source_named_agent_type,
            "durable_context_candidates": list(report.durable_context_candidates or []),
            "risks_or_followups": list(report.risks_or_followups or []),
            "evidence_refs": list(report.evidence_refs or []),
            "recommended_importance": report.recommended_importance.value,
            "review_state": "pending_manager_review",
            "review_status": "pending_manager_review",
        }
        summary = f"AfterActionReport pending manager review: {report.summary}"

        if active_task is None and review is not None:
            return self._record_late_after_action_report_for_archived_task(
                report=report,
                review=review,
                target_task_id=target_task_id,
                now=now,
                identity=identity,
            )

        batch: Optional[AfterActionReportReviewBatchRecord] = None
        if active_task is not None and review is not None:
            batch = self._get_or_create_after_action_report_batch_for_active_task(
                target_task_id,
                now=now,
                identity=identity,
            )
            review.batch_id = batch.batch_id
            details["batch_id"] = batch.batch_id

        writer = self.operations.append_history_entry if active_task is not None else self.operations.append_history_entry_to_archived_task
        entry = writer(
            target_task_id,
            summary,
            source=TaskHistorySource.AGENT,
            entry_type=TaskHistoryType.AGENT_NOTE,
            importance=report.recommended_importance,
            timestamp=timestamp,
            agent_name=agent_name or report.source_agent_name,
            agent_id=agent_id or report.source_agent_id,
            related_task_id=report.source_task_id,
            details=details,
        )
        if entry is None:
            return None

        if not self.identity.owner_agent_id:
            self._logger.warning(
                "AfterActionReport %s recorded in history but no owner_agent_id is available for review queue.",
                report.report_id,
            )
            return AfterActionReportRecordResult(
                target_task_id=target_task_id,
                history_entry=entry,
                accepted=True,
                batch=batch,
                reason="history_recorded_without_review_owner",
            )

        try:
            stored = self.after_action_report_repository.upsert_review(review)
            entry.details["review_id"] = stored.review_id
            entry.details["review_status"] = stored.status.value
            if active_task is not None:
                self.operations.persist_runtime_state()
            return AfterActionReportRecordResult(
                target_task_id=target_task_id,
                history_entry=entry,
                review=stored,
                batch=batch,
                accepted=True,
            )
        except Exception as exc:
            self._logger.warning(
                "Failed to enqueue AfterActionReport %s for manager review: %s",
                report.report_id,
                exc,
            )
            return AfterActionReportRecordResult(
                target_task_id=target_task_id,
                history_entry=entry,
                review=review,
                batch=batch,
                accepted=True,
                reason="review_enqueue_failed",
            )

    def _build_after_action_report_review(
        self,
        report: AfterActionReport,
        timestamp: str,
        identity: ReportedWorkIdentity,
    ) -> Optional[AfterActionReportReviewRecord]:
        if not self.identity.owner_agent_id:
            return None
        return AfterActionReportReviewRecord(
            report_id=report.report_id,
            owner_agent_id=self.identity.owner_agent_id,
            owner_agent_name=self.identity.owner_agent_name,
            team_id=self.identity.team_id,
            owner_named_agent_type=self.identity.owner_named_agent_type,
            source_task_id=report.source_task_id,
            parent_task_id=report.parent_task_id,
            source_agent_id=report.source_agent_id,
            source_agent_name=report.source_agent_name,
            source_named_agent_type=report.source_named_agent_type,
            person_id=identity.person_id,
            identity_verified=True,
            summary=report.summary,
            durable_context_candidates=list(report.durable_context_candidates or []),
            risks_or_followups=list(report.risks_or_followups or []),
            evidence_refs=list(report.evidence_refs or []),
            recommended_importance=report.recommended_importance,
            status=AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def _parse_utc_timestamp(self, timestamp: Optional[str]) -> datetime:
        if not timestamp:
            return datetime.now(timezone.utc)
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _record_late_after_action_report_for_archived_task(
        self,
        *,
        report: AfterActionReport,
        review: AfterActionReportReviewRecord,
        target_task_id: str,
        now: str,
        identity: ReportedWorkIdentity,
    ) -> AfterActionReportRecordResult:
        now_dt = self._parse_utc_timestamp(now)
        latest_batch = self.after_action_report_repository.get_latest_batch_for_task(
            target_task_id,
            owner_agent_id=self.identity.owner_agent_id,
            person_id=identity.person_id,
        )
        open_batch = self.after_action_report_repository.get_open_batch_for_task(
            target_task_id,
            owner_agent_id=self.identity.owner_agent_id,
            person_id=identity.person_id,
        )

        schedule_required = False
        batch = None
        if open_batch is not None and now_dt <= self._parse_utc_timestamp(open_batch.accept_until):
            batch = open_batch
        elif latest_batch is not None:
            review.status = AfterActionReportReviewStatus.IGNORED
            review.stale_reason = "arrived_after_late_after_action_report_accept_window"
            review.decision_summary = "Ignored because it arrived after the late-AAR collection window closed."
            review.reviewed_at = now
            review.updated_at = now
            review.batch_id = latest_batch.batch_id
            stored = self.after_action_report_repository.upsert_review(review)
            self._logger.info(
                "Dropped stale AfterActionReport %s for archived task %s after batch %s accept window.",
                report.report_id,
                target_task_id,
                latest_batch.batch_id,
            )
            return AfterActionReportRecordResult(
                target_task_id=target_task_id,
                review=stored,
                batch=latest_batch,
                accepted=False,
                stale=True,
                reason=review.stale_reason,
            )
        else:
            accept_until = now_dt + timedelta(seconds=30)
            scheduled_review_at = now_dt + timedelta(seconds=35)
            batch = AfterActionReportReviewBatchRecord(
                owner_agent_id=self.identity.owner_agent_id,
                owner_agent_name=self.identity.owner_agent_name,
                team_id=self.identity.team_id,
                owner_named_agent_type=self.identity.owner_named_agent_type,
                parent_task_id=target_task_id,
                person_id=identity.person_id,
                identity_verified=True,
                opened_at=now,
                accept_until=accept_until.isoformat(),
                scheduled_review_at=scheduled_review_at.isoformat(),
                created_at=now,
                updated_at=now,
            )
            batch = self.after_action_report_repository.upsert_batch(batch)
            schedule_required = True

        review.batch_id = batch.batch_id
        review.status = AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW
        review.updated_at = now
        stored = self.after_action_report_repository.upsert_review(review)
        self._logger.info(
            "Accepted late AfterActionReport %s for archived task %s into batch %s.",
            report.report_id,
            target_task_id,
            batch.batch_id,
        )
        return AfterActionReportRecordResult(
            target_task_id=target_task_id,
            review=stored,
            batch=batch,
            accepted=True,
            schedule_required=schedule_required,
        )

    def _get_or_create_after_action_report_batch_for_active_task(
        self,
        target_task_id: str,
        *,
        now: str,
        identity: ReportedWorkIdentity,
    ) -> AfterActionReportReviewBatchRecord:
        open_batch = self.after_action_report_repository.get_open_batch_for_task(
            target_task_id,
            owner_agent_id=self.identity.owner_agent_id,
            person_id=identity.person_id,
        )
        if open_batch is not None:
            return open_batch

        batch = AfterActionReportReviewBatchRecord(
            owner_agent_id=self.identity.owner_agent_id,
            owner_agent_name=self.identity.owner_agent_name,
            team_id=self.identity.team_id,
            owner_named_agent_type=self.identity.owner_named_agent_type,
            parent_task_id=target_task_id,
            person_id=identity.person_id,
            identity_verified=True,
            opened_at=now,
            accept_until=now,
            scheduled_review_at=now,
            created_at=now,
            updated_at=now,
        )
        return self.after_action_report_repository.upsert_batch(batch)

    def begin_after_action_report_completion_window(
        self,
        task_id: str,
        *,
        timestamp: str,
    ) -> List[AfterActionReportReviewBatchRecord]:
        """Open the late-report window of the task's unscheduled batches; the ones to schedule a review for.

        A task works for one person, so it normally has one open batch. A
        batch saved before the person was recorded is closed unreviewed here
        instead (``close_unverified_batch``).
        """

        if not self.identity.owner_agent_id:
            return []
        completion_dt = self._parse_utc_timestamp(timestamp)
        windowed: List[AfterActionReportReviewBatchRecord] = []
        for batch in self.after_action_report_repository.list_open_batches_for_task(
            task_id,
            owner_agent_id=self.identity.owner_agent_id,
        ):
            if not batch.identity_verified:
                self.close_unverified_batch(batch, timestamp=timestamp)
                continue
            if batch.scheduled_message_id:
                continue
            batch.accept_until = (completion_dt + timedelta(seconds=30)).isoformat()
            batch.scheduled_review_at = (completion_dt + timedelta(seconds=35)).isoformat()
            batch.updated_at = timestamp
            windowed.append(self.after_action_report_repository.upsert_batch(batch))
        return windowed

    def close_unverified_batch(
        self,
        batch: AfterActionReportReviewBatchRecord,
        *,
        timestamp: Optional[str] = None,
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        """Close a batch saved before the person was recorded, without a review (logged).

        Nothing verifiable says whose work its reports describe, so no review
        may run for anyone; its pending reports are marked ignored.
        """

        now = timestamp or datetime.now(timezone.utc).isoformat()
        owner_agent_id = self.identity.owner_agent_id
        if not owner_agent_id:
            return None
        ignored = self.after_action_report_repository.ignore_unverified_pending_reviews(
            batch.batch_id,
            owner_agent_id=owner_agent_id,
            stale_reason=UNVERIFIED_IDENTITY_STALE_REASON,
            decision_summary="Closed unreviewed: it was saved before the runtime recorded whose work it describes.",
            reviewed_at=now,
        )
        reviews = self.after_action_report_repository.list_reviews_for_batch(
            batch.batch_id, owner_agent_id=owner_agent_id
        )
        self._logger.warning(
            "Closed an after-action report batch saved without a verified person; its reports are not reviewed. "
            "event=after_action_report_batch_unverified_closed batch_id=%s parent_task_id=%s owner_agent_id=%s "
            "ignored_reports=%d",
            batch.batch_id,
            batch.parent_task_id,
            owner_agent_id,
            ignored,
        )
        return self.after_action_report_repository.close_batch(
            batch.batch_id,
            owner_agent_id=owner_agent_id,
            final_history_entry_id=None,
            report_count=0,
            stale_report_count=len(reviews),
            closed_at=now,
        )

    def mark_after_action_report_batch_scheduled(
        self,
        batch_id: str,
        *,
        scheduled_message_id: str,
        scheduled_review_task_id: str,
        timestamp: Optional[str] = None,
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        if not self.identity.owner_agent_id:
            return None
        return self.after_action_report_repository.mark_batch_scheduled(
            batch_id,
            owner_agent_id=self.identity.owner_agent_id,
            scheduled_message_id=scheduled_message_id,
            scheduled_review_task_id=scheduled_review_task_id,
            updated_at=timestamp or datetime.now(timezone.utc).isoformat(),
        )

    def build_after_action_report_batch_review_task(
        self,
        batch_id: str,
        *,
        task_id: Optional[str] = None,
    ) -> Optional[Task]:
        if not self.identity.owner_agent_id:
            return None
        batch = self.after_action_report_repository.get_batch(
            batch_id,
            owner_agent_id=self.identity.owner_agent_id,
        )
        if batch is None:
            return None
        reviews = self.after_action_report_repository.list_reviews_for_batch(
            batch_id,
            owner_agent_id=self.identity.owner_agent_id,
        )
        accepted_reviews = [review for review in reviews if not review.stale_reason]
        pending_reviews = [
            review
            for review in accepted_reviews
            if review.status == AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW
        ]
        stale_reviews = [review for review in reviews if review.stale_reason]
        resolved_count = len(accepted_reviews) - len(pending_reviews)
        archived = self.operations.get_task_from_db(batch.parent_task_id)
        parent_summary = "The original parent task is archived."
        if archived and archived.get("main_task"):
            parent_task = archived["main_task"]
            parent_summary = parent_task.problem_statement or parent_summary

        lines = [
            "Review the AfterActionReports collected for a recently completed parent task.",
            "",
            "Context:",
            f"- Parent task summary: {parent_summary}",
            f"- AAR collection opened at: {batch.opened_at}",
            f"- Collection accepts reports until: {batch.accept_until}",
            f"- This scheduled review was set for: {batch.scheduled_review_at}",
            f"- Accepted report count: {len(accepted_reviews)}",
            f"- Pending accepted report count: {len(pending_reviews)}",
            f"- Already resolved accepted report count: {resolved_count}",
            f"- Ignored stale report count: {len(stale_reviews)}",
            "",
            "Collected AfterActionReports:",
        ]
        if not pending_reviews:
            lines.append("- No pending reports were accepted into this collection window.")
        for review in pending_reviews:
            lines.extend(
                [
                    f"- report_id: {review.report_id}",
                    f"  status: {review.status.value}",
                    f"  source: {review.source_agent_name or 'unknown'} ({review.source_named_agent_type or 'unknown type'})",
                    f"  source_task_id: {review.source_task_id or 'unknown'}",
                    f"  summary: {review.summary}",
                    f"  recommended_importance: {review.recommended_importance.value}",
                ]
            )
            if review.durable_context_candidates:
                lines.append("  durable_context_candidates:")
                lines.extend(
                    f"  - {candidate}" for candidate in review.durable_context_candidates
                )
            if review.risks_or_followups:
                lines.append("  risks_or_followups:")
                lines.extend(f"  - {risk}" for risk in review.risks_or_followups)
            if review.evidence_refs:
                lines.append("  evidence_refs:")
                lines.extend(f"  - {evidence}" for evidence in review.evidence_refs)

        lines.extend(
            [
                "",
                "For each pending report, decide whether it should be ignored, kept in task history, summarized to task history, promoted to pinned memory, or marked as needing follow-up.",
                "Use resolve_after_action_report_review with the report_id shown above. You do not need to call list_pending_after_action_reports unless you need to refresh or debug the review set.",
                "If promoting durable lessons, store the memory before or immediately after resolving the report.",
                "When all useful reports are resolved, complete this scheduled review task with a concise summary of the decisions.",
            ]
        )
        return Task(
            task_id=task_id or batch.scheduled_review_task_id or new_id("task_aar_review"),
            task_type=MessageMetaType.REQUEST,
            problem_statement="\n".join(lines),
        )

    def count_pending_after_action_report_reviews_for_batch(self, batch_id: str) -> int:
        if not self.identity.owner_agent_id:
            return 0
        reviews = self.after_action_report_repository.list_reviews_for_batch(
            batch_id,
            owner_agent_id=self.identity.owner_agent_id,
        )
        return sum(
            1
            for review in reviews
            if not review.stale_reason
            and review.status == AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW
        )

    def close_after_action_report_batch_collection_for_review(
        self,
        batch_id: str,
        *,
        scheduled_review_task_id: str,
        timestamp: Optional[str] = None,
    ) -> Optional[AfterActionReportReviewBatchRecord]:
        if not self.identity.owner_agent_id:
            return None
        now = timestamp or datetime.now(timezone.utc).isoformat()
        batch = self.after_action_report_repository.claim_batch_for_finalizing(
            batch_id,
            owner_agent_id=self.identity.owner_agent_id,
            updated_at=now,
        )
        if batch is None:
            return None
        if not batch.identity_verified:
            # Saved before the person was recorded: closed without a review;
            # the caller skips the run (scheduled_runtime).
            return self.close_unverified_batch(batch, timestamp=now)
        reviews = self.after_action_report_repository.list_reviews_for_batch(
            batch_id,
            owner_agent_id=self.identity.owner_agent_id,
        )
        accepted_reviews = [review for review in reviews if not review.stale_reason]
        stale_reviews = [review for review in reviews if review.stale_reason]
        batch.scheduled_review_task_id = scheduled_review_task_id
        self.after_action_report_repository.upsert_batch(batch)
        return self.after_action_report_repository.close_batch(
            batch_id,
            owner_agent_id=self.identity.owner_agent_id,
            final_history_entry_id=None,
            report_count=len(accepted_reviews),
            stale_report_count=len(stale_reviews),
            closed_at=now,
        )

    def list_after_action_report_reviews(
        self,
        *,
        person_id: Optional[str],
        status: Optional[AfterActionReportReviewStatus] = AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW,
        limit: int = 20,
    ) -> List[AfterActionReportReviewRecord]:
        """Reviews of reports about ``person_id``'s work (None: work for nobody), from verified records only."""

        if not self.identity.owner_agent_id:
            return []
        return self.after_action_report_repository.list_reviews(
            owner_agent_id=self.identity.owner_agent_id,
            person_id=person_id,
            status=status,
            limit=limit,
        )

    def get_after_action_report_review(
        self, review_id: str, *, person_id: Optional[str]
    ) -> Optional[AfterActionReportReviewRecord]:
        if not self.identity.owner_agent_id:
            return None
        return self.after_action_report_repository.get_review(
            review_id,
            owner_agent_id=self.identity.owner_agent_id,
            person_id=person_id,
        )

    def get_after_action_report_review_by_report_id(
        self, report_id: str, *, person_id: Optional[str]
    ) -> Optional[AfterActionReportReviewRecord]:
        if not self.identity.owner_agent_id:
            return None
        return self.after_action_report_repository.get_review_by_report_id(
            report_id,
            owner_agent_id=self.identity.owner_agent_id,
            person_id=person_id,
        )

    def resolve_after_action_report_review(
        self,
        review_id: Optional[str],
        *,
        person_id: Optional[str],
        report_id: Optional[str] = None,
        status: AfterActionReportReviewStatus,
        decision_summary: str,
        review_rationale: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[AfterActionReportReviewRecord]:
        if not self.identity.owner_agent_id:
            raise ValueError("AfterActionReport review resolution requires owner_agent_id.")
        if status == AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW:
            raise ValueError("AfterActionReport review resolution status cannot remain pending_manager_review.")
        if not decision_summary or not decision_summary.strip():
            raise ValueError("decision_summary is required.")

        # Only a review of this person's work (or, for work for nobody, of work
        # for nobody) can be found, so a review never decides another person's reports.
        review = None
        if review_id:
            review = self.after_action_report_repository.get_review(
                review_id,
                owner_agent_id=self.identity.owner_agent_id,
                person_id=person_id,
            )
        if review is None and report_id:
            review = self.after_action_report_repository.get_review_by_report_id(
                report_id,
                owner_agent_id=self.identity.owner_agent_id,
                person_id=person_id,
            )
        if review is None:
            return None
        review_id = review.review_id

        target_task_id = review.parent_task_id or review.source_task_id
        timestamp = datetime.now(timezone.utc).isoformat()
        history_entry: Optional[TaskHistoryEntry] = None
        if target_task_id:
            active_task = self.operations.get_task_info(target_task_id)
            details = {
                "review_id": review.review_id,
                "report_id": review.report_id,
                "batch_id": review.batch_id,
                "review_status": status.value,
                "review_rationale": review_rationale,
                "source_task_id": review.source_task_id,
                "parent_task_id": review.parent_task_id,
                "source_agent_id": review.source_agent_id,
                "source_agent_name": review.source_agent_name,
                "source_named_agent_type": review.source_named_agent_type,
            }
            summary = f"AfterActionReport reviewed ({status.value}): {decision_summary.strip()}"
            writer = (
                self.operations.append_history_entry
                if active_task is not None
                else self.operations.append_history_entry_to_archived_task
            )
            history_entry = writer(
                target_task_id,
                summary,
                source=TaskHistorySource.AGENT,
                entry_type=TaskHistoryType.AGENT_NOTE,
                importance=review.recommended_importance,
                timestamp=timestamp,
                agent_name=agent_name or self.identity.owner_agent_name,
                agent_id=agent_id or self.identity.owner_agent_id,
                related_task_id=review.source_task_id,
                details=details,
            )
            if history_entry is None:
                self._logger.warning(
                    "Resolved AfterActionReport review %s but could not append task-history decision entry.",
                    review_id,
                )

        return self.after_action_report_repository.resolve_review(
            review_id,
            owner_agent_id=self.identity.owner_agent_id,
            person_id=person_id,
            status=status,
            decision_summary=decision_summary.strip(),
            review_rationale=review_rationale or None,
            resolution_history_entry_id=history_entry.entry_id if history_entry else None,
            reviewed_at=timestamp,
        )

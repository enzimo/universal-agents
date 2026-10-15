"""reviews contracts, independent of runtime hosts and SDKs."""

from custom_messages.enums import AfterActionReportReviewBatchStatus
from custom_messages.enums import AfterActionReportReviewStatus
from custom_messages.enums import TaskHistoryImportance
from datetime import datetime
from datetime import timezone
from id_generation import new_id
from pydantic import BaseModel
from pydantic import Field
from typing import List
from typing import Literal
from typing import Optional


class AfterActionReportReviewRecord(BaseModel):
    """Runtime-owned review state for a specialist AfterActionReport."""

    review_id: str = Field(
        default_factory=lambda: new_id("aar_review"),
        description="Runtime-owned stable handle for the manager review record.",
    )
    message_class: Literal["AfterActionReportReviewRecord"] = Field(
        default="AfterActionReportReviewRecord",
        description="After Action Report Review Record",
    )
    report_id: str = Field(description="AfterActionReport id being reviewed.")
    batch_id: Optional[str] = Field(
        default=None,
        description="Runtime-owned late-AAR batch id when the report arrived after parent task archival.",
    )
    owner_agent_id: Optional[str] = Field(
        default=None, description="Manager agent id that owns this review."
    )
    owner_agent_name: Optional[str] = Field(
        default=None, description="Manager agent name that owns this review."
    )
    team_id: Optional[str] = Field(
        default=None, description="Team id for the owning manager."
    )
    owner_named_agent_type: Optional[str] = Field(
        default=None, description="Owning manager named agent type."
    )
    source_task_id: Optional[str] = Field(
        default=None, description="Task completed by the reporting specialist."
    )
    parent_task_id: Optional[str] = Field(
        default=None, description="Delegating parent task id."
    )
    source_agent_id: Optional[str] = Field(
        default=None, description="Reporting specialist agent id."
    )
    source_agent_name: Optional[str] = Field(
        default=None, description="Reporting specialist name."
    )
    source_named_agent_type: Optional[str] = Field(
        default=None, description="Reporting specialist named agent type."
    )
    person_id: Optional[str] = Field(
        default=None,
        description=(
            "The person the reported work was for, from verified identity only "
            "(custom_agents.after_action_report_identity); None for work for nobody (system work)."
        ),
    )
    identity_verified: bool = Field(
        default=False,
        description=(
            "True when person_id was taken from verified identity as the report was recorded. "
            "Records saved before that have False: nobody can say whose work they describe, "
            "so they are never reviewed."
        ),
    )
    summary: str = Field(description="Report summary proposed by the specialist.")
    durable_context_candidates: List[str] = Field(
        default_factory=list, description="Candidate durable lessons."
    )
    risks_or_followups: List[str] = Field(
        default_factory=list, description="Risks or follow-up recommendations."
    )
    evidence_refs: List[str] = Field(
        default_factory=list,
        description="Evidence references supplied by the specialist.",
    )
    recommended_importance: TaskHistoryImportance = Field(
        default=TaskHistoryImportance.NORMAL,
        description="Specialist-recommended review importance.",
    )
    status: AfterActionReportReviewStatus = Field(
        default=AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW,
        description="Manager review status.",
    )
    decision_summary: Optional[str] = Field(
        default=None, description="Short final manager decision summary."
    )
    review_rationale: Optional[str] = Field(
        default=None, description="Manager rationale for the final decision."
    )
    stale_reason: Optional[str] = Field(
        default=None,
        description="Runtime reason this AAR was not accepted into a late-AAR batch.",
    )
    resolution_history_entry_id: Optional[str] = Field(
        default=None,
        description="Task history entry id created when the review was resolved.",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    reviewed_at: Optional[str] = Field(
        default=None, description="Timestamp when manager resolved the review."
    )

    def get_id(self) -> str:
        return self.review_id


class AfterActionReportReviewBatchRecord(BaseModel):
    """Runtime-owned collection window for late AfterActionReport records."""

    batch_id: str = Field(
        default_factory=lambda: new_id("aar_batch"),
        description="Runtime-owned stable handle for the late-AAR review batch.",
    )
    message_class: Literal["AfterActionReportReviewBatchRecord"] = Field(
        default="AfterActionReportReviewBatchRecord",
        description="After Action Report Review Batch Record",
    )
    owner_agent_id: Optional[str] = Field(
        default=None, description="Manager agent id that owns this batch."
    )
    owner_agent_name: Optional[str] = Field(
        default=None, description="Manager agent name that owns this batch."
    )
    team_id: Optional[str] = Field(
        default=None, description="Team id for the owning manager."
    )
    owner_named_agent_type: Optional[str] = Field(
        default=None, description="Owning manager named agent type."
    )
    parent_task_id: str = Field(
        description="Archived parent task whose late AARs are being collected."
    )
    person_id: Optional[str] = Field(
        default=None,
        description=(
            "The person every report in this batch was for; the scheduled review works for them. "
            "None for work for nobody (system work). Batches are kept per person."
        ),
    )
    identity_verified: bool = Field(
        default=False,
        description=(
            "True when person_id was taken from verified identity. Batches saved before that "
            "have False and are closed unreviewed."
        ),
    )
    status: AfterActionReportReviewBatchStatus = Field(
        default=AfterActionReportReviewBatchStatus.OPEN,
        description="Late-AAR batch lifecycle status.",
    )
    opened_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    accept_until: str = Field(
        description="Timestamp after which new AARs for this batch are ignored."
    )
    scheduled_review_at: str = Field(
        description="Timestamp for the scheduled manager AAR review task."
    )
    scheduled_message_id: Optional[str] = Field(
        default=None,
        description="ScheduledMessage id for the normal manager AAR review task.",
    )
    scheduled_review_task_id: Optional[str] = Field(
        default=None,
        description="Task id of the scheduled manager AAR review task.",
    )
    final_history_entry_id: Optional[str] = Field(
        default=None,
        description="Legacy field retained for older late-AAR batch history flows.",
    )
    report_count: int = Field(
        default=0, description="Accepted AAR review count at finalization time."
    )
    stale_report_count: int = Field(
        default=0, description="Dropped stale AAR count known at finalization time."
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    closed_at: Optional[str] = Field(
        default=None,
        description="Timestamp when the late-AAR collection window was closed.",
    )

    def get_id(self) -> str:
        return self.batch_id

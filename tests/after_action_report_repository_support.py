"""The same real-record checks for SQLite and live PostgreSQL."""

from custom_messages.enums import AfterActionReportReviewStatus
from custom_messages.records.reviews import AfterActionReportReviewRecord
from storage.after_action_report_repository import AfterActionReportRepository


def assert_report_person_scoping(repository: AfterActionReportRepository) -> None:
    owner = "manager_aar_scoping_validation"
    records = {}
    for person in ("usr_ann", "usr_bob", None):
        record = AfterActionReportReviewRecord(
            owner_agent_id=owner, person_id=person, identity_verified=True,
            report_id="aar_reused_model_id", summary=f"Lesson for {person}.",
        )
        stored = repository.upsert_review(record)
        repeated = repository.upsert_review(record.model_copy(update={"review_id": f"repeat_{person}"}))
        assert repeated.review_id == stored.review_id
        [listed] = repository.list_reviews(owner_agent_id=owner, person_id=person, status=None, limit=20)
        assert listed.person_id == person and listed.summary == f"Lesson for {person}."
        records[person] = stored
    assert len({record.review_id for record in records.values()}) == 3
    ann = records["usr_ann"]
    repository.resolve_review(
        ann.review_id, owner_agent_id=owner, person_id="usr_ann",
        status=AfterActionReportReviewStatus.IGNORED, decision_summary="Reviewed Ann's report.",
        review_rationale=None, resolution_history_entry_id=None, reviewed_at="2026-10-11T00:00:00Z",
    )
    assert repository.upsert_review(records["usr_bob"]).status is AfterActionReportReviewStatus.PENDING_MANAGER_REVIEW
    forged = records["usr_bob"].model_copy(update={"review_id": ann.review_id, "report_id": "aar_forged_id"})
    try:
        repository.upsert_review(forged)
    except ValueError:
        pass
    else:
        raise AssertionError("A stored review accepted another person's record.")
    unchanged = repository.get_review(ann.review_id, owner_agent_id=owner, person_id="usr_ann")
    assert unchanged is not None and unchanged.summary == ann.summary
    assert unchanged.status is AfterActionReportReviewStatus.IGNORED

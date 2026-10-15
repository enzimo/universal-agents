"""Person-scoped report IDs survive the previous unique-index layout."""

import sqlite3
from pathlib import Path

from storage.after_action_report_repository import SqliteAfterActionReportRepository
from tests.after_action_report_repository_support import assert_report_person_scoping


def test_report_ids_are_unique_per_person_after_reopening_the_old_index(tmp_path: Path) -> None:
    path = str(tmp_path / "reports.db")
    SqliteAfterActionReportRepository(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_aar_reviews_owner_person_report")
        conn.execute("DROP INDEX idx_aar_reviews_owner_nobody_report")
        conn.execute(
            "CREATE UNIQUE INDEX idx_aar_reviews_owner_report ON after_action_report_reviews(owner_agent_id, report_id)"
        )
    assert_report_person_scoping(SqliteAfterActionReportRepository(path))

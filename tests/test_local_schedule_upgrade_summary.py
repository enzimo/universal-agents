"""Operator summaries separate old claims without changing upgrade decisions."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.summarize_local_schedule_upgrade import load_report, summarize


NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


@pytest.fixture
def report():
    return {
        "team_id": "team_ua",
        "review_work": {
            "queued_review_jobs": [{"review_job_id": "queued"}],
            "claimed_review_jobs": [
                {
                    "review_job_id": "june",
                    "claim_owner_agent_id": "old-worker",
                    "claim_expires_at": "2026-06-21T03:40:33.183785+00:00",
                },
                {
                    "review_job_id": "july",
                    "claim_owner_agent_id": "old-worker",
                    "claim_expires_at": "2026-07-20T16:43:52.653394+00:00",
                },
                {
                    "review_job_id": "current",
                    "claim_owner_agent_id": "current-worker",
                    "claim_expires_at": "2026-09-19T01:00:00Z",
                },
                {"review_job_id": "naive", "claim_expires_at": "2026-06-01T00:00:00"},
                {"review_job_id": "invalid", "claim_expires_at": "invalid"},
            ],
            "unfinished_review_tasks": [
                {"owner_agent_id": "current-worker", "task_id": "task"}
            ],
            "unsupported_review_runtimes": [{"owner_agent_id": "old-worker"}],
            "blocks_upgrade": True,
        },
        "schedules": [
            {
                "schedule_id": "blocked",
                "action": "blocked",
                "reason": "active_schedule_claim",
            },
            {"schedule_id": "ready", "action": "upgrade"},
        ],
    }


def test_groups_old_claims_without_hiding_current_blockers_or_changing_state(report):
    before = deepcopy(report)
    result = summarize(report, now=NOW)
    assert result["counts"] == {
        "queued_jobs_left_unchanged": 1,
        "awaiting_cleanup_review": 0,
        "unexpired_claims": 1,
        "expired_claims": 2,
        "claims_with_unknown_expiry": 2,
        "unfinished_tasks": 1,
        "unsupported_runtimes": 1,
        "blocked_schedules": 1,
    }
    assert result["upgrade_reports_blocked"]
    group = result["expired_claims_by_worker"]["items"][0]
    assert group["expired_claims"] == 2
    assert group["worker_id"] == "old-worker"
    assert group["has_saved_runtime_in_report"]
    assert group["oldest_expiry"].startswith("2026-06-21")
    assert group["newest_expiry"].startswith("2026-07-20")
    assert "expired_claim_details" not in result
    assert (
        result["look_at_first"]["unexpired_claims"]["items"][0]["review_job_id"]
        == "current"
    )
    assert report == before


def test_large_backlog_is_counted_and_details_are_bounded(report):
    job = report["review_work"]["claimed_review_jobs"][0]
    report["review_work"]["claimed_review_jobs"] = [
        dict(job, review_job_id=f"job-{n}") for n in range(2000)
    ]
    result = summarize(report, now=NOW, worker="old-worker", limit=2)
    assert result["counts"]["expired_claims"] == 2000
    assert len(result["expired_claims_by_worker"]["items"]) == 1
    assert result["expired_claim_details"]["omitted"] == 1998
    assert len(result["expired_claim_details"]["items"]) == 2
    assert len(json.dumps(result)) < 4000
    assert (
        summarize(report, now=NOW, worker="different")["expired_claim_details"]["total"]
        == 0
    )


def test_expiry_offsets_and_exact_boundary(report):
    report["review_work"]["claimed_review_jobs"] = [
        {
            "review_job_id": "boundary",
            "claim_owner_agent_id": "worker",
            "claim_expires_at": "2026-09-19T02:00:00+02:00",
        },
        {
            "review_job_id": "earlier",
            "claim_owner_agent_id": "worker",
            "claim_expires_at": "2026-09-18T23:00:00+00:00",
        },
    ]
    result = summarize(report, now=NOW, show_expired=True)
    assert result["counts"]["expired_claims"] == 2
    assert result["expired_claims_by_worker"]["items"][0]["oldest_expiry"].startswith(
        "2026-09-18"
    )


def test_loads_complete_report_surrounded_by_logs(report):
    text = (
        "Starting preview\n"
        + json.dumps(report, indent=2)
        + "\nUpgrade blocked; no changes were made.\n"
    )
    assert load_report(text) == report


@pytest.mark.parametrize(
    "source", ["", '{"review_work":', "[]", '{"review_work":{},"schedules":[]}']
)
def test_incomplete_input_is_not_presented_as_no_blockers(source):
    with pytest.raises(ValueError):
        load_report(source)


def test_multiple_reports_require_explicit_input_selection(report):
    with pytest.raises(ValueError, match="one complete"):
        load_report(json.dumps(report) + "\n" + json.dumps(report))


def test_standalone_cli_reads_saved_report_and_stdin_without_modifying_file(
    report, tmp_path
):
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts/summarize_local_schedule_upgrade.py"
    )
    source = tmp_path / "preview.log"
    content = json.dumps(report) + "\nUpgrade blocked; no changes were made.\n"
    source.write_text(content)
    for argument, stdin in ((str(source), None), ("-", content)):
        result = subprocess.run(
            [sys.executable, str(script), argument, "--limit", "1"],
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout)["team_id"] == "team_ua"
    assert source.read_text() == content

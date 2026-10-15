#!/usr/bin/env python3
"""Summarize a saved local-schedule upgrade report without opening the database."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


def load_report(text: str) -> dict[str, Any]:
    """Accept the complete JSON report with optional surrounding command logs."""
    decoder = json.JSONDecoder()
    reports = []
    for offset, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, offset)
        except ValueError:
            continue
        if isinstance(value, dict) and "review_work" in value and "schedules" in value:
            reports.append(value)
    if len(reports) != 1:
        raise ValueError(
            "Expected one complete upgrade report. Capture the original command's "
            "full output, including stderr; a pasted or truncated list is not enough."
        )
    report = reports[0]
    review = report["review_work"]
    if not isinstance(review, dict) or not isinstance(report["schedules"], list):
        raise ValueError("Invalid upgrade report structure.")
    for key in (
        "queued_review_jobs",
        "claimed_review_jobs",
        "unfinished_review_tasks",
        "unsupported_review_runtimes",
    ):
        if not isinstance(review.get(key), list):
            raise ValueError(f"Missing or invalid review_work.{key} in report.")
        if not all(isinstance(row, dict) for row in review[key]):
            raise ValueError("Report entries must be JSON objects.")
    if not all(isinstance(row, dict) for row in report["schedules"]):
        raise ValueError("Schedule entries must be JSON objects.")
    return report


def claim_expiry(job: dict[str, Any]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(job.get("claim_expires_at", "")))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def page(rows: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    return {
        "total": len(rows),
        "omitted": max(0, len(rows) - limit),
        "items": rows[:limit],
    }


def summarize(
    report: dict[str, Any],
    *,
    now: datetime,
    limit: int = 10,
    worker: str | None = None,
    show_expired: bool = False,
) -> dict[str, Any]:
    review = report["review_work"]
    unexpired, expired, unknown = [], [], []
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in review["claimed_review_jobs"]:
        expiry = claim_expiry(job)
        if expiry is None:
            unknown.append(job)
        elif expiry > now:
            unexpired.append(job)
        else:
            expired.append(job)
            groups[job.get("claim_owner_agent_id") or "<unknown>"].append(job)
    tasks = review["unfinished_review_tasks"]
    unsupported = review["unsupported_review_runtimes"]
    saved_workers = {row.get("owner_agent_id") for row in tasks + unsupported}
    grouped = []
    for owner, jobs in groups.items():
        dates = [job["claim_expires_at"] for job in jobs]
        # Sort by actual UTC time; ISO offsets need not sort chronologically.
        dates.sort(key=lambda value: datetime.fromisoformat(value))
        grouped.append(
            {
                "worker_id": owner,
                "expired_claims": len(jobs),
                "oldest_expiry": dates[0],
                "newest_expiry": dates[-1],
                "has_saved_runtime_in_report": owner in saved_workers,
                "example_job_ids": [job.get("review_job_id") for job in jobs[:3]],
            }
        )
    grouped.sort(key=lambda row: (-row["expired_claims"], row["worker_id"]))
    blocked_schedules = [
        row for row in report["schedules"] if row.get("action") == "blocked"
    ]
    result = {
        "team_id": report.get("team_id"),
        "checked_at_utc": now.isoformat(),
        "upgrade_reports_blocked": bool(
            review.get("blocks_upgrade") or blocked_schedules
        ),
        "counts": {
            "queued_jobs_left_unchanged": len(review["queued_review_jobs"]),
            "awaiting_cleanup_review": review.get("awaiting_cleanup_review", 0),
            "unexpired_claims": len(unexpired),
            "expired_claims": len(expired),
            "claims_with_unknown_expiry": len(unknown),
            "unfinished_tasks": len(tasks),
            "unsupported_runtimes": len(unsupported),
            "blocked_schedules": len(blocked_schedules),
        },
        "look_at_first": {
            "unexpired_claims": page(unexpired, limit),
            "claims_with_unknown_expiry": page(unknown, limit),
            "unfinished_tasks": page(tasks, limit),
            "unsupported_runtimes": page(unsupported, limit),
            "blocked_schedules": page(blocked_schedules, limit),
        },
        "expired_claims_by_worker": page(grouped, limit),
        "notes": [
            "Read-only summary. Claims set aside for cleanup review are inactive; other expired claims require explicit retirement before upgrade.",
            "No saved runtime in this report does not prove a worker or NATS delivery is absent.",
            "Expiry is not evidence of completion or permission to retry old work.",
        ],
    }
    if show_expired or worker:
        result["expired_claim_details"] = page(
            [
                job
                for job in expired
                if worker is None or job.get("claim_owner_agent_id") == worker
            ],
            limit,
        )
    if report.get("expired_claim_retirement"):
        result["expired_claim_retirement"] = report["expired_claim_retirement"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="Saved full output file, or - for stdin")
    parser.add_argument(
        "--limit", type=int, default=10, help="Maximum rows per section, default 10"
    )
    parser.add_argument(
        "--worker", help="Also show expired claims for this exact worker ID"
    )
    parser.add_argument(
        "--show-expired",
        action="store_true",
        help="Also show individual expired claims",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    try:
        source = (
            sys.stdin.read() if args.report == "-" else Path(args.report).read_text()
        )
        report = load_report(source)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    result = summarize(
        report,
        now=datetime.now(timezone.utc),
        limit=args.limit,
        worker=args.worker,
        show_expired=args.show_expired,
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

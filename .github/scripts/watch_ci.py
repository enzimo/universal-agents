#!/usr/bin/env python3
"""Wait for exact GitHub runs without an agent, retaining evidence and failures."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import time


def gh(*args: str) -> str:
    return subprocess.run(["rtk", "proxy", "gh", *args], check=True,
                          capture_output=True, text=True, timeout=60).stdout


def outcome(runs: list[dict], expected: set[str]) -> str:
    if any(run.get("conclusion") not in (None, "", "success") for run in runs):
        return "failure"
    if any(job.get("conclusion") not in (None, "", "success", "skipped")
           for run in runs for job in run.get("jobs", [])):
        return "failure"
    if expected - {run["name"] for run in runs}:
        return "waiting"
    if not runs or any(run["status"] != "completed" for run in runs):
        return "waiting"
    if any(run.get("conclusion") != "success" or not run.get("jobs") or
           any(job["status"] != "completed" or not job.get("conclusion") for job in run["jobs"])
           for run in runs):
        return "waiting"
    if any(not any(job["conclusion"] == "success" for job in run["jobs"]) for run in runs):
        return "failure"
    return "success"


def snapshot(repo: str, run_ids: list[int], sha: str | None,
             workflows: set[str], output: Path) -> list[dict]:
    if sha:
        available = json.loads(gh("api", f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100"))["workflow_runs"]
        # Keep the newest run of each requested workflow, never another commit.
        selected = {}
        for run in sorted(available, key=lambda row: row["id"], reverse=True):
            if run["head_sha"] == sha and run["name"] in workflows:
                selected.setdefault(run["name"], run["id"])
        run_ids = list(selected.values())
    runs = []
    for run_id in run_ids:
        run = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}"))
        if sha and run["head_sha"] != sha:
            raise ValueError("GitHub returned a different source commit")
        jobs, page = [], 1
        while True:
            batch = json.loads(gh("api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&page={page}"))["jobs"]
            jobs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        run["jobs"] = jobs
        for job in jobs:
            path = output / f"job-{job['id']}.log"
            if job["status"] == "completed" and job.get("conclusion") and not path.exists():
                try:
                    path.write_text(gh("api", f"repos/{repo}/actions/jobs/{job['id']}/logs"))
                except subprocess.CalledProcessError as error:
                    # GitHub can report completion before its log endpoint is
                    # ready. Preserve status and retry the download next poll.
                    job["log_pending"] = error.stderr.strip()[:300]
        runs.append({key: run.get(key) for key in
                     ("id", "name", "head_sha", "status", "conclusion", "html_url",
                      "created_at", "run_started_at", "updated_at", "jobs")})
    return runs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repository")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", type=int, action="append", default=[])
    source.add_argument("--sha")
    parser.add_argument("--workflow", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-minutes", type=float, default=45)
    parser.add_argument("--poll-seconds", type=float, default=30)
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        parser.error("--repo must be owner/repository")
    if args.sha and not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        parser.error("--sha must be a full commit SHA")
    if args.sha and not args.workflow:
        parser.error("--sha requires at least one expected --workflow")
    if args.timeout_minutes <= 0 or not 1 <= args.poll_seconds <= 60:
        parser.error("Use a positive deadline and a polling interval between 1 and 60 seconds")
    args.output.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.timeout_minutes * 60
    previous, errors = None, 0
    while True:
        try:
            runs = snapshot(args.repo, args.run, args.sha, set(args.workflow), args.output)
            errors = 0
        except (subprocess.SubprocessError, ValueError, KeyError) as error:
            errors += 1
            if errors >= 3 or time.monotonic() >= deadline:
                (args.output / "summary.json").write_text(json.dumps(
                    {"result": "monitor_error", "error": str(error)}, indent=2) + "\n")
                print("CI monitor could not read GitHub after bounded retries; see summary.json", flush=True)
                return 3
            time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
            continue
        result = outcome(runs, set(args.workflow))
        if result == "waiting" and time.monotonic() >= deadline:
            result = "timeout"
        summary = {"repository": args.repo, "result": result,
                   "checked_at": datetime.now(timezone.utc).isoformat(), "runs": runs}
        temporary = args.output / "summary.json.tmp"
        temporary.write_text(json.dumps(summary, indent=2) + "\n")
        temporary.replace(args.output / "summary.json")
        progress = [(run["name"], run["status"], run["conclusion"],
                     [(job["name"], job["status"], job["conclusion"]) for job in run["jobs"]])
                    for run in runs]
        if progress != previous:
            completed = sum(job["status"] == "completed" for run in runs for job in run["jobs"])
            total = sum(len(run["jobs"]) for run in runs)
            print(f"{args.repo}: {completed}/{total} jobs finished; {result}", flush=True)
            previous = progress
        if result != "waiting":
            print(f"CI {result}. Evidence: {args.output / 'summary.json'}", flush=True)
            if any(job.get("log_pending") for run in runs for job in run["jobs"]):
                print("Some GitHub logs are not available yet; rerun this command to retrieve them.", flush=True)
            for run in runs:
                for job in run["jobs"]:
                    if job.get("conclusion") in (None, "", "success", "skipped"):
                        continue
                    steps = [step["name"] for step in job["steps"] if step.get("conclusion") == "failure"]
                    print(f"FAILED {run['name']} / {job['name']}: {', '.join(steps)}\n{job['html_url']}", flush=True)
            return {"success": 0, "failure": 1, "timeout": 2}[result]
        time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())

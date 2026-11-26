#!/usr/bin/env python3
"""Summarize the live tool presentation comparison (tests/test_live_tool_presentation.py).

Reads the JSON Lines report and prints, per model and presentation: runs,
successes, mean input and output tokens per task, mean model calls, failed and
unknown tool calls, and calls made from scripts; then the change in input
tokens against ``full_definitions``, and each task's success per presentation.

Run: ``.venv/bin/python scripts/summarize_tool_presentation_report.py <report.jsonl> [--markdown]``
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

BASELINE = "full_definitions"
ORDER = ("full_definitions", "index", "index_and_scripts")


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["model"], row["presentation"])].append(row)
    summary = []
    for (model, presentation), items in sorted(groups.items(), key=lambda kv: (kv[0][0], ORDER.index(kv[0][1]))):
        measured = [row for row in items if "inputTokens" in row]
        summary.append({
            "model": model,
            "presentation": presentation,
            "runs": len(items),
            "successes": sum(1 for row in items if row.get("success")),
            "input_tokens": round(mean(row["inputTokens"] for row in measured)) if measured else 0,
            "output_tokens": round(mean(row["outputTokens"] for row in measured)) if measured else 0,
            "model_calls": round(mean(row.get("model_calls") or 0 for row in items), 1),
            "failed_calls": sum(row.get("failed_calls") or 0 for row in items),
            "unknown_calls": sum(row.get("unknown_calls") or 0 for row in items),
            "script_calls": sum(row.get("script_calls") or 0 for row in items),
            "seconds": round(mean(row.get("seconds") or 0 for row in items), 1),
        })
    baseline = {row["model"]: row["input_tokens"] for row in summary if row["presentation"] == BASELINE}
    for row in summary:
        base = baseline.get(row["model"])
        row["input_vs_full"] = f"{(row['input_tokens'] - base) / base:+.0%}" if base else "n/a"
    return summary


def per_task(rows: list[dict]) -> dict[tuple[str, str], dict[str, str]]:
    table: dict[tuple[str, str], dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        table[(row["model"], row["task"])][row["presentation"]].append(bool(row.get("success")))
    return {
        key: {presentation: f"{sum(values)}/{len(values)}" for presentation, values in by_presentation.items()}
        for key, by_presentation in sorted(table.items())
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    rows = load(Path(argv[1]))
    columns = ["model", "presentation", "runs", "successes", "input_tokens", "input_vs_full", "output_tokens",
               "model_calls", "failed_calls", "unknown_calls", "script_calls", "seconds"]
    summary = summarize(rows)
    if "--markdown" in argv:
        print("| " + " | ".join(columns) + " |")
        print("|" + "---|" * len(columns))
        for row in summary:
            print("| " + " | ".join(str(row[column]) for column in columns) + " |")
    else:
        for row in summary:
            print("  ".join(f"{column}={row[column]}" for column in columns))
    print()
    for (model, task), cells in per_task(rows).items():
        print(f"{model:<36} {task:<18} " + "  ".join(f"{p}={cells.get(p, '-')}" for p in ORDER))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

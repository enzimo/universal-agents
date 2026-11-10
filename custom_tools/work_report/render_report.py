#!/usr/bin/env python3
"""Render the coder-critic work report from the statistics cct.py computes.

    render_report.py STATS.json --mode page --out report.html
    render_report.py STATS.json --mode t3   --out report.t3.html

The output is one self-contained HTML file with inline CSS and no scripts, so
it also opens where scripts are blocked. `page` uses the report's own light
and dark palette; `t3` uses T3 Code's theme variables and sets no page
background, for display inside a T3 thread. `--detail full` carries every
critique and finding in full (the default for `page`); `--detail compact`
keeps every section and chart but shortens the long texts, so the file stays
small enough to show inline (the default for `t3`). Design rules:
references/report-design.md. Standard library only; Python 3.9 or newer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CSS = Path(__file__).resolve().parent.parent / "assets" / "report.css"

LENS_LABELS = {
    "intent": "Intent", "correctness": "Correctness", "security": "Security",
    "scalability": "Scalability", "modularity": "Modularity",
    "complexity": "Unnecessary complexity", "concurrency": "Concurrency",
    "failure_handling": "Failure handling", "testing": "Testing",
    "data_integrity": "Data integrity", "compatibility": "Compatibility",
    "operability": "Operability",
}
SEVERITY_LABELS = {"blocking": "Blocking", "should_fix": "Should fix", "consider": "Consider"}
FIRST_LABELS = {"accept": "accepted", "partial": "partly accepted", "reject": "rejected"}
OUTCOME_LABELS = {"accepted": "accepted", "partial": "partly accepted", "rejected": "rejected",
                  "withdrawn": "withdrawn by the critic", "open": "not settled"}
STATUS_LABELS = {"ready_to_merge": "Ready to merge", "needs_attention": "Needs attention",
                 "blocked": "Blocked", "abandoned": "Abandoned", "in_progress": "In progress"}
VERDICT_LABELS = {"approve": "approve", "approve_with_changes": "approve with changes",
                  "needs_rework": "needs rework", "goal_unclear": "goal unclear"}
CRITERION_MARKS = {"met": ("✓", "met"), "partial": ("◐", "partly met"),
                   "unmet": ("✗", "not met"), "unverified": ("?", "not verified")}
LANES = [("lead", "Lead"), ("coder", "Coder"), ("critic", "Critic"), ("review", "Review")]
SEV_ORDER = {"blocking": 0, "should_fix": 1, "consider": 2}
FINDING_STATUS = ["open", "fixed", "issue_filed", "proposed", "accepted_risk", "wont_fix"]
# How the findings bar groups the statuses: label, style, statuses.
FINDING_GROUPS = [("fixed", "k-fixed", ("fixed",)), ("open", "k-open", ("open",)),
                  ("follow-up", "k-follow", ("issue_filed", "proposed")),
                  ("accepted or won't fix", "k-kept", ("accepted_risk", "wont_fix"))]
STAGES = ["after_pr", "before_run"]
LIST_OPEN_LIMIT = 8  # longer lists of criteria and findings start closed


# ---------------------------------------------------------------- validation
# The renderer may run where stats.json comes from an agent, so check every
# field it reads before rendering anything.

class StatsError(ValueError):
    """stats.json is not in the shape cct.py writes; the message names the field."""


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _get(obj: Any, key: str, path: str, kind: Any, required: bool = True,
         allowed: Optional[List[Any]] = None) -> Any:
    if not isinstance(obj, dict):
        raise StatsError(f"{path} must be an object")
    value = obj.get(key)
    where = f"{path}.{key}"
    if value is None:
        if required:
            raise StatsError(f"{where} is missing")
        return None
    ok = _is_num(value) if kind == "num" else (
        isinstance(value, int) and not isinstance(value, bool) if kind is int else isinstance(value, kind))
    if not ok:
        raise StatsError(f"{where} has the wrong type")
    if allowed is not None and value not in allowed:
        raise StatsError(f"{where} is not one of {', '.join(map(str, allowed))}")
    return value


def _strs(obj: Any, key: str, path: str, required: bool = True) -> List[str]:
    values = _get(obj, key, path, list, required) or []
    if not all(isinstance(v, str) for v in values):
        raise StatsError(f"{path}.{key} must list strings")
    return values


def _objs(obj: Any, key: str, path: str, required: bool = True) -> List[Dict[str, Any]]:
    values = _get(obj, key, path, list, required) or []
    for n, v in enumerate(values):
        if not isinstance(v, dict):
            raise StatsError(f"{path}.{key}[{n}] must be an object")
    return values


def upgrade(s: Any) -> Any:
    """Read schema 1 (one `review` holding its findings) as schema 2."""
    if not isinstance(s, dict) or s.get("schema") != 1:
        return s
    s = dict(s)
    review = s.pop("review", None)
    if review is not None and not isinstance(review, dict):
        raise StatsError("stats.review must be an object")
    findings = (review or {}).get("findings") or []
    if not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings):
        raise StatsError("review.findings must list objects")
    s["findings"] = [{**f, "stage": "after_pr"} for f in findings]
    s["reviews"] = [{**review, "stage": "after_pr",
                     "findings": [f.get("id") for f in findings]}] if review else []
    s["schema"] = 2
    return s


def validate_stats(s: Any) -> None:
    """Raise StatsError unless s has every field the renderer reads, typed.

    Accepts both schemas; callers that validate before they render need no
    upgrade step of their own.
    """
    s = upgrade(s)
    if not isinstance(s, dict) or s.get("schema") != 2:
        raise StatsError("stats must be an object with schema 1 or 2")
    _get(s, "generated_at", "stats", str)
    run = _get(s, "run", "stats", dict)
    for key in ("title", "outcome", "started_at"):
        _get(run, key, "run", str)
    _get(run, "close_note", "run", str, required=False)
    _get(run, "head_sha", "run", str, required=False)
    _get(run, "status", "run", str, allowed=list(STATUS_LABELS))
    _get(run, "elapsed_min", "run", "num")
    _get(run, "events", "run", int)
    try:
        day(run["started_at"])
    except ValueError as exc:
        raise StatsError("run.started_at is not an ISO 8601 time") from exc
    ctx = _get(run, "context", "run", dict, required=False) or {}
    for key in ("objective", "plan_step"):
        if ctx.get(key) is not None and not isinstance(ctx[key], (str, int, float)):
            raise StatsError(f"run.context.{key} has the wrong type")
    repo = _get(run, "repo", "run", dict)
    for key in ("name", "base", "branch", "base_sha"):
        _get(repo, key, "run.repo", str, required=False)
    pr = _get(run, "pr", "run", dict, required=False)
    if pr:
        _get(pr, "url", "run.pr", str, required=False)
        _get(pr, "number", "run.pr", int, required=False)
    workers = _get(run, "workers", "run", dict)
    for role in ("coder", "critic"):
        w = _get(workers, role, "run.workers", dict, required=False) or {}
        for key in ("model", "effort", "runtime"):
            _get(w, key, f"run.workers.{role}", str, required=False)
    narrative = _get(s, "narrative", "stats", dict)
    _get(narrative, "summary", "narrative", str, required=False)
    _strs(narrative, "next_steps", "narrative", required=False)
    for n, c in enumerate(_objs(s, "acceptance", "stats")):
        path = f"acceptance[{n}]"
        for key in ("id", "text"):
            _get(c, key, path, str)
        _get(c, "status", path, str, allowed=list(CRITERION_MARKS))
        _get(c, "evidence", path, str, required=False)
    counts = _get(s, "counts", "stats", dict)
    for key in ("critic_turns", "reviews", "rebuttals", "checkpoints", "interventions"):
        _get(counts, key, "counts", int)
    items = _get(s, "items", "stats", dict)
    _get(items, "total", "items", int)
    _get(items, "lead_rulings", "items", int)
    dec = _get(items, "by_decision", "items", dict)
    for key in ("accepted", "partial", "rejected", "withdrawn"):
        _get(dec, key, "items.by_decision", int)
    for lens, value in _get(items, "by_lens", "items", dict).items():
        if lens not in LENS_LABELS or not isinstance(value, dict):
            raise StatsError(f"items.by_lens has an unknown lens {lens!r}")
    for n, r in enumerate(_objs(s, "rounds", "stats")):
        path = f"rounds[{n}]"
        _get(r, "key", path, str)
        _get(r, "label", path, str)
        _get(r, "start_min", path, "num")
        _get(r, "end_min", path, "num")
        for q in _get(r, "interventions", path, list, required=False) or []:
            if not isinstance(q, int) or isinstance(q, bool):
                raise StatsError(f"{path}.interventions must list integers")
        coder = _get(r, "coder", path, dict, required=False)
        if coder:
            kind = _get(coder, "kind", f"{path}.coder", str, allowed=["plan", "checkpoint"])
            if kind == "plan":
                _get(coder, "summary", f"{path}.coder", str)
                _strs(coder, "units", f"{path}.coder")
                _strs(coder, "checkpoints", f"{path}.coder")
            else:
                _get(coder, "title", f"{path}.coder", str)
                _get(coder, "notes", f"{path}.coder", str, required=False)
                _strs(coder, "deviations", f"{path}.coder", required=False)
                _strs(coder, "addresses", f"{path}.coder", required=False)
                _check_tests(_get(coder, "tests", f"{path}.coder", dict, required=False), f"{path}.coder.tests")
                g = _get(coder, "git", f"{path}.coder", dict)
                for key in ("from", "head"):
                    _get(g, key, f"{path}.coder.git", str)
                for key in ("adds", "dels", "commits", "files"):
                    _get(g, key, f"{path}.coder.git", int)
        critic = _get(r, "critic", path, dict, required=False)
        if critic:
            _get(critic, "summary", f"{path}.critic", str)
            _strs(critic, "items", f"{path}.critic")
    for n, i in enumerate(_objs(s, "item_list", "stats")):
        path = f"item_list[{n}]"
        for key in ("id", "round", "title"):
            _get(i, key, path, str)
        _get(i, "lens", path, str, allowed=list(LENS_LABELS))
        _get(i, "severity", path, str, allowed=list(SEVERITY_LABELS))
        _get(i, "outcome", path, str, allowed=list(OUTCOME_LABELS))
        _get(i, "first_decision", path, str, required=False, allowed=list(FIRST_LABELS))
        for key in ("detail", "suggestion", "where", "rationale", "final_rationale", "decided_by"):
            _get(i, key, path, str, required=False)
        _strs(i, "matched_by_review", path, required=False)
        _strs(i, "relates_to_findings", path, required=False)
        rb = _get(i, "rebuttal", path, dict, required=False)
        if rb:
            _get(rb, "position", f"{path}.rebuttal", str)
            for key in ("argument", "amended_suggestion"):
                _get(rb, key, f"{path}.rebuttal", str, required=False)
        ruling = _get(i, "ruling", path, dict, required=False)
        if ruling:
            _get(ruling, "rationale", f"{path}.ruling", str)
    for n, iv in enumerate(_objs(s, "interventions", "stats")):
        _get(iv, "seq", f"interventions[{n}]", int)
        _get(iv, "reason", f"interventions[{n}]", str)
        _get(iv, "summary", f"interventions[{n}]", str)
        _strs(iv, "actions", f"interventions[{n}]", required=False)
    tl = _get(s, "timeline", "stats", dict)
    _get(tl, "total_min", "timeline", "num")
    for n, sp in enumerate(_objs(tl, "spans", "timeline")):
        _get(sp, "lane", f"timeline.spans[{n}]", str, allowed=[lane for lane, _ in LANES])
        _get(sp, "start", f"timeline.spans[{n}]", "num")
        _get(sp, "end", f"timeline.spans[{n}]", "num")
        _get(sp, "label", f"timeline.spans[{n}]", str)
    for n, mk in enumerate(_objs(tl, "marks", "timeline")):
        _get(mk, "at", f"timeline.marks[{n}]", "num")
        _get(mk, "label", f"timeline.marks[{n}]", str)
    for n, ph in enumerate(_objs(tl, "phases", "timeline")):
        _get(ph, "label", f"timeline.phases[{n}]", str)
        _get(ph, "start", f"timeline.phases[{n}]", "num")
        _get(ph, "end", f"timeline.phases[{n}]", "num")
    diff = _get(s, "diff", "stats", dict)
    for key in ("adds", "dels", "files", "commits"):
        _get(diff, key, "diff", int)
    for kind, v in _get(diff, "by_kind", "diff", dict).items():
        if kind not in KINDS:
            raise StatsError(f"diff.by_kind has an unknown kind {kind!r}")
        for key in ("adds", "dels"):
            _get(v, key, f"diff.by_kind.{kind}", int)
    for n, f in enumerate(_objs(diff, "files_list", "diff")):
        _get(f, "path", f"diff.files_list[{n}]", str)
        _get(f, "adds", f"diff.files_list[{n}]", int)
        _get(f, "dels", f"diff.files_list[{n}]", int)
    _check_tests(_get(s, "tests", "stats", dict, required=False), "tests")
    for n, r in enumerate(_objs(s, "reviews", "stats")):
        path = f"reviews[{n}]"
        _get(r, "stage", path, str, allowed=STAGES)
        _strs(r, "findings", path, required=False)
        for key in ("head_sha", "source"):
            _get(r, key, path, str, required=False)
        if _get(r, "ran", path, bool):
            _get(r, "verdict", path, str, allowed=list(VERDICT_LABELS))
            _strs(r, "reviewers", path)
            _strs(r, "skipped", path, required=False)
        else:
            _get(r, "reason", path, str)
    for n, f in enumerate(_objs(s, "findings", "stats")):
        path = f"findings[{n}]"
        for key in ("id", "title"):
            _get(f, key, path, str)
        _get(f, "stage", path, str, allowed=STAGES)
        _get(f, "severity", path, str, allowed=list(SEVERITY_LABELS))
        _get(f, "status", path, str, allowed=FINDING_STATUS)
        for key in ("where", "scenario", "direction", "evidence", "note", "matches_item",
                    "issue_url", "ref"):
            _get(f, key, path, str, required=False)
        _strs(f, "addressed_in", path, required=False)
    _strs(s, "warnings", "stats")


def _check_tests(t: Optional[Dict[str, Any]], path: str) -> None:
    if not t:
        return
    if "not_run" in t:
        _get(t, "not_run", path, str)
        return
    _get(t, "passed", path, int)
    _get(t, "failed", path, int)


KINDS = ("source", "tests", "docs", "other")


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def url(value: Optional[str]) -> Optional[str]:
    """Only plain web links become anchors."""
    if value and (value.startswith("https://") or value.startswith("http://")):
        return value
    return None


def link(value: Optional[str], text: str) -> str:
    target = url(value)
    return f'<a href="{e(target)}">{e(text)}</a>' if target else e(text)


def dur(minutes: float) -> str:
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if m else f"{h}h"


def clock(minutes: float) -> str:
    minutes = int(round(minutes))
    return f"{minutes // 60}:{minutes % 60:02d}"


def pct(value: float, total: float) -> str:
    return f"{max(0.0, min(100.0, 100.0 * float(value) / float(total))):.2f}%"


def plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def day(ts: str) -> str:
    d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return f"{d.day} {d.strftime('%B %Y')}"


def shorten(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(",;:.—- ") + "…"


def id_key(value: str) -> Tuple[str, int]:
    """Order C2 before C10 and F2 before F10."""
    match = re.match(r"([A-Za-z]*)(\d+)", value)
    return (match.group(1), int(match.group(2))) if match else (value, 0)


def fit(text: Optional[str], limit: int, compact: bool) -> str:
    """The text, cut to the limit in the compact view only."""
    text = text or ""
    return shorten(text, limit) if compact else text


def review_flags(item: Dict[str, Any]) -> str:
    out = ""
    if item.get("matched_by_review"):
        out += ('<span class="flag">Project review raised this again: '
                f'{e(", ".join(item["matched_by_review"]))}</span>')
    if item.get("relates_to_findings"):
        out += ('<span class="flag flag--note">Same problem as review finding '
                f'{e(", ".join(item["relates_to_findings"]))}</span>')
    return out


def decided(item: Dict[str, Any]) -> str:
    """One line for the glyph tooltip: the outcome, who settled it and why."""
    outcome = item["outcome"]
    words = OUTCOME_LABELS[outcome].capitalize()
    if outcome in ("accepted", "partial", "rejected"):
        words += " by the lead" if item.get("decided_by") == "lead" else " by the coder"
    reason = ((item.get("ruling") or {}).get("rationale") or item.get("final_rationale")
              or item.get("rationale") or "")
    return f"{words}: {shorten(reason)}" if reason else words


def glyph(item: Dict[str, Any], tip: bool = True) -> str:
    """A critique mark with a hover summary. CSS only: the report runs no script."""
    label = (f"{item['id']}: {SEVERITY_LABELS[item['severity']].lower()}, "
             f"{LENS_LABELS[item['lens']]}, {OUTCOME_LABELS[item['outcome']]}")
    if not tip:  # next to the critique's own text, a summary would only repeat it
        return (f'<span class="g g--{e(item["outcome"])} s--{e(item["severity"])}" '
                f'role="img" aria-label="{e(label)}"></span>')
    head = f"{item['id']} · {SEVERITY_LABELS[item['severity']]} · {LENS_LABELS[item['lens']]}"
    tip = (f'<span class="tip" aria-hidden="true"><span class="tip-k">{e(head)}</span>'
           f'<span class="tip-t">{e(shorten(item["title"], 140))}</span>'
           f'<span class="tip-d">{e(decided(item))}</span></span>')
    return (f'<span class="gw"><span class="g g--{e(item["outcome"])} s--{e(item["severity"])}" '
            f'role="img" aria-label="{e(label)}"></span>{tip}</span>')


def bar(parts: List[Tuple[str, int, str]], label: str) -> str:
    total = sum(int(n) for _, n, _ in parts) or 1
    spans = "".join(f'<span class="{e(cls)}" style="flex:{int(n)}" title="{e(name)}: {int(n)}"></span>'
                    for cls, n, name in parts if int(n) > 0)
    return f'<div class="bar" role="img" aria-label="{e(label)}">{spans}</div>' if total else ""


# ------------------------------------------------------------------ sections

def finding_counts(findings: List[Dict[str, Any]]) -> List[Tuple[str, str, int]]:
    """Label, style and count for each status group that has findings."""
    out = []
    for label, cls, statuses in FINDING_GROUPS:
        n = sum(1 for f in findings if f["status"] in statuses)
        if n:
            out.append((label, cls, n))
    return out


def review_name(r: Dict[str, Any], several: bool) -> str:
    if r["stage"] == "before_run":
        return "Earlier review"
    return "Review of the pull request" if several else "Project review"


def masthead(s: Dict[str, Any]) -> str:
    run = s["run"]
    repo = run["repo"].get("name") or run["repo"].get("branch")
    bits = ["Work report", e(repo)]
    if run.get("pr"):
        num = run["pr"].get("number")
        bits.append("PR " + link(run["pr"].get("url"), f"#{num}" if num else "link"))
    bits.append(e(day(run["started_at"])))
    ctx = run.get("context") or {}
    if ctx.get("objective"):
        step = f", plan step {e(ctx['plan_step'])}" if ctx.get("plan_step") else ""
        bits.append(f"Objective: {e(ctx['objective'])}{step}")
    summary = s["narrative"].get("summary") or run["outcome"]
    status = run["status"]
    verdict = [f'<span class="status status--{e(status)}">{e(STATUS_LABELS.get(status, status))}</span>']
    crit = s["acceptance"]
    counts = {k: sum(1 for c in crit if c["status"] == k) for k in CRITERION_MARKS}
    parts = [f"{counts['met']} met"] + [f"{counts[k]} {CRITERION_MARKS[k][1]}"
                                         for k in ("partial", "unmet", "unverified") if counts[k]]
    verdict.append(f"<span>Acceptance criteria: <b>{e(' · '.join(parts))}</b></span>")
    several = len(s["reviews"]) > 1
    for r in s["reviews"]:
        name = review_name(r, several)
        if not r.get("ran"):
            verdict.append(f"<span>{e(name)}: <b>not run</b></span>")
            continue
        mine = [f for f in s["findings"] if f["stage"] == r["stage"]]
        extra = [f"{n} {label}" for label, _, n in finding_counts(mine)]
        verdict.append(f"<span>{e(name)}: <b>{e(VERDICT_LABELS[r['verdict']])}</b>"
                       + (f' <span class="muted">· {e(plural(len(mine), "finding"))}: '
                          f'{e(", ".join(extra))}</span>' if mine else "")
                       + "</span>")
    out = [f'<header><p class="kicker">{" · ".join(bits)}</p>',
           f'<h1 class="title">{e(run["title"])}</h1>',
           f'<p class="summary">{e(summary)}</p>',
           f'<div class="verdict">{"".join(verdict)}</div>']
    if run.get("close_note"):
        out.append(f'<p class="muted" style="margin:10px 0 0">Closing note: {e(run["close_note"])}</p>')
    if s.get("warnings"):
        out.append('<div class="warnings"><b>Gaps in this report:</b> '
                   + e("; ".join(s["warnings"])) + ".</div>")
    out.append("</header>")
    return "".join(out)


def criteria(s: Dict[str, Any], compact: bool = False) -> str:
    """One row per criterion. A long list starts closed, except what is not met."""
    crit = s["acceptance"]
    long_list = len(crit) > LIST_OPEN_LIMIT
    rows = []
    for c in crit:
        mark, word = CRITERION_MARKS[c["status"]]
        if c["status"] == "partial":
            mark = '<span class="g g--partial s--should_fix"></span>'
        attention = c["status"] != "met"
        text = c["text"] if attention else fit(c["text"], 160, compact)
        evidence = c.get("evidence") or ""
        evidence = evidence if attention else fit(evidence, 180, compact)
        body = f'<div class="ev">{e(evidence)}</div>' if evidence else ""
        opened = " open" if attention or not long_list else ""
        rows.append(
            f'<details class="ac"{opened}><summary><span class="mark mark--{e(c["status"])}" '
            f'role="img" aria-label="{e(word)}">{mark}</span>'
            f'<span class="mono muted">{e(c["id"])}</span><span class="tx">{e(text)}</span></summary>'
            f'{body}</details>')
    hint = "checked by the lead" + (" · open a row for its evidence" if long_list else "")
    return ('<section aria-labelledby="h-criteria"><h2 id="h-criteria">Acceptance criteria'
            f'<small>{e(hint)}</small></h2>'
            f'<div class="criteria">{"".join(rows)}</div></section>')


def stat(value: str, label: str, sub: str = "", extra: str = "") -> str:
    return (f'<div class="stat"><div class="v">{value}</div><div class="l">{e(label)}</div>'
            f'{extra}' + (f'<div class="l">{e(sub)}</div>' if sub else "") + "</div>")


def stats_strip(s: Dict[str, Any]) -> str:
    c, items, diff = s["counts"], s["items"], s["diff"]
    dec = items["by_decision"]
    tiles = [
        stat(e(dur(s["run"]["elapsed_min"])),
             "Brief to close" if s["run"]["status"] != "in_progress" else "Elapsed so far",
             f"{plural(c['checkpoints'], 'checkpoint')}"),
        stat(str(c["critic_turns"]), "Critic rounds",
             f"{plural(c['reviews'], 'review')} · {plural(c['rebuttals'], 'last word')}"),
        stat(str(items["total"]), "Critiques",
             f"{dec['accepted']} accepted · {items['total'] - dec['accepted']} not in full",
             bar([("k-accepted", dec["accepted"], "accepted"), ("k-partial", dec["partial"], "partly"),
                  ("k-rejected", dec["rejected"], "rejected"), ("k-withdrawn", dec["withdrawn"], "withdrawn")],
                 f"{dec['accepted']} accepted, {dec['partial']} partly, {dec['rejected']} rejected, "
                 f"{dec['withdrawn']} withdrawn")),
        stat(str(c["interventions"]), "Lead interventions",
             plural(items["lead_rulings"], "ruling")),
        stat(f'<span class="add">+{diff["adds"]:,}</span> <small class="del">−{diff["dels"]:,}</small>',
             "Lines changed", f"{plural(diff['files'], 'file')} · {plural(diff['commits'], 'commit')}",
             bar([(f"k-{k}", v["adds"] + v["dels"], k) for k, v in sorted(diff["by_kind"].items(),
                  key=lambda kv: ["source", "tests", "docs", "other"].index(kv[0])
                  if kv[0] in ("source", "tests", "docs", "other") else 9)],
                 ", ".join(f"{k} +{v['adds']} −{v['dels']}" for k, v in diff["by_kind"].items()))),
    ]
    findings = s["findings"]
    if any(r.get("ran") for r in s["reviews"]):
        groups = finding_counts(findings)
        blocking = sum(1 for f in findings if f["severity"] == "blocking")
        words = [f"{blocking} blocking"] + [f"{n} {label}" for label, _, n in groups]
        tiles.append(stat(str(len(findings)), "Review findings", " · ".join(words),
                          bar([(cls, n, label) for label, cls, n in groups],
                              ", ".join(f"{n} {label}" for label, _, n in groups))
                          if findings else ""))
    else:
        tiles.append(stat("—", "Review findings", "review not run"))
    return f'<section class="stats" aria-label="At a glance">{"".join(tiles)}</section>'


def timeline(s: Dict[str, Any]) -> str:
    tl = s["timeline"]
    total = max(float(tl["total_min"]), 1.0)
    used = {sp["lane"] for sp in tl["spans"]}
    rows = []
    for lane, name in LANES:
        if lane not in used:
            continue
        spans = "".join(
            f'<span class="tl-span lane-{lane}" style="left:{pct(sp["start"], total)};'
            f'width:{pct(sp["end"] - sp["start"], total)}" '
            f'title="{e(name)} {e(clock(sp["start"]))}–{e(clock(sp["end"]))}: {e(sp["label"])}"></span>'
            for sp in tl["spans"] if sp["lane"] == lane)
        rows.append(f'<div class="tl-row"><span>{e(name)}</span><div class="tl-track">{spans}</div></div>')
    overlay = []
    for ph in tl["phases"]:
        # A short last phase has no room for its label; hang the label from its right edge.
        end = " tl-phase--end" if float(ph["start"]) / total > 0.88 else ""
        overlay.append(f'<div class="tl-phase{end}" style="left:{pct(ph["start"], total)};'
                       f'width:{pct(ph["end"] - ph["start"], total)}"><b>{e(ph["label"])}</b></div>')
    last = -100.0
    for mk in tl["marks"]:
        pos = max(0.0, min(100.0, 100.0 * float(mk["at"]) / total))
        text = ""
        if pos - last >= 5.5 and pos <= 96:
            text, last = f'<i>{e(mk["label"])}</i>', pos
        overlay.append(f'<div class="tl-mark" style="left:{pos:.2f}%" title="{e(mk["label"])} at '
                       f'{e(clock(mk["at"]))}">{text}</div>')
    step = next((st for st in (10, 15, 30, 60, 120, 240, 480, 720, 1440, 2880, 10080)
                 if total / st <= 8), 1440 * max(1, math.ceil(total / 1440 / 8)))
    ticks = "".join(f'<span style="left:{pct(t, total)}">{e(clock(t))}</span>'
                    for t in range(0, int(total) + 1, step) if t <= total - step * 0.4 or t == 0)
    ticks += f'<span style="left:100%;transform:translateX(-100%)">{e(clock(total))}</span>'
    legend = "".join(f'<span><i class="sw lane-{lane}"></i>{e(name)}</span>'
                     for lane, name in LANES if lane in used)
    aria = (f"Timeline over {dur(total)}: " +
            "; ".join(f"{name} worked {dur(sum(sp['end'] - sp['start'] for sp in tl['spans'] if sp['lane'] == lane))}"
                      for lane, name in LANES if lane in used))
    return ('<section aria-labelledby="h-tl"><h2 id="h-tl">Timeline<small>time between recorded turns, '
            f'{e(dur(total))} in all</small></h2>'
            f'<div class="tl" role="img" aria-label="{e(aria)}"><div class="tl-overlay">{"".join(overlay)}</div>'
            f'{"".join(rows)}<div class="tl-axis">{ticks}</div></div>'
            f'<div class="legend">{legend}</div></section>')


def tests_text(t: Optional[Dict[str, Any]]) -> str:
    if not t:
        return ""
    if "not_run" in t:
        return f"tests not run ({t['not_run']})"
    return f"{t['passed']} passed, {t['failed']} failed"


def critique_li(item: Dict[str, Any], compact: bool = False) -> str:
    meta = f"{item['id']} · {SEVERITY_LABELS[item['severity']]} · {LENS_LABELS[item['lens']]}"
    if item.get("where") and not compact:
        meta += f" · {item['where']}"
    says = []
    if compact:
        # The compact view names each critique and how it ended; the page has the exchange.
        meta += f" · {decided(item)}"
    else:
        if item.get("detail") or item.get("suggestion"):
            says.append(e(" ".join(x for x in (item.get("detail"), item.get("suggestion")) if x)))
        first = FIRST_LABELS.get(item.get("first_decision") or "", "")
        if first:
            coder = f'<b class="who--coder">Coder</b> {e(first)}'
            if item.get("rationale"):
                coder += f" — {e(item['rationale'])}"
            says.append(coder)
        if item.get("rebuttal"):
            r = item["rebuttal"]
            says.append(f'<b class="who--critic">Last word</b> {e(r["position"])}'
                        + (f" — {e(r['argument'])}" if r.get("argument") else "")
                        + (f" Amended: {e(r['amended_suggestion'])}" if r.get("amended_suggestion") else ""))
        if item.get("final_rationale"):
            says.append(f'<b class="who--coder">Final call</b> {e(item["final_rationale"])}')
        if item.get("ruling"):
            says.append(f'<b class="who--lead">Lead ruling</b> {e(item["ruling"]["rationale"])}')
    flags = review_flags(item)
    if flags:
        says.append(flags)
    return (f'<li><div class="head">{glyph(item, tip=False)}<div><div>{e(item["title"])}</div>'
            f'<div class="meta">{e(meta)}</div></div></div>'
            + "".join(f'<div class="says">{x}</div>' for x in says) + "</li>")


def rounds(s: Dict[str, Any], compact: bool = False) -> str:
    by_id = {i["id"]: i for i in s["item_list"]}
    interventions = {iv["seq"]: iv for iv in s["interventions"]}
    rows = []
    for rnd in s["rounds"]:
        coder, critic = rnd.get("coder"), rnd.get("critic")
        items = [by_id[i] for i in (critic or {}).get("items") or [] if i in by_id]
        if coder and coder["kind"] == "plan":
            title = coder["summary"]
            sub = f"{plural(len(coder['units']), 'unit')} · {plural(len(coder['checkpoints']), 'checkpoint')} proposed"
        elif coder:
            g = coder["git"]
            title = coder["title"]
            sub = f"+{g['adds']} −{g['dels']} · {plural(g['commits'], 'commit')}"
            if tests_text(coder.get("tests")):
                sub += f" · {tests_text(coder['tests'])}"
            if coder.get("addresses"):
                sub += f" · fixes {', '.join(coder['addresses'])}"
        else:
            title, sub = "Follow-up decisions", ""
        glyphs = "".join(glyph(i) for i in items) if items else (
            '<span class="muted">no critiques</span>' if critic else "")
        body_left = ['<div><div class="who who--coder">Coder</div>']
        if coder and coder["kind"] == "plan":
            body_left.append(f"<p>{e(fit(coder['summary'], 240, compact))}</p>")
            if compact:
                body_left.append(f'<p class="muted">{e(plural(len(coder["units"]), "unit"))}: '
                                 + e(shorten(", ".join(coder["units"]), 300)) + "</p>")
            else:
                body_left.append("<ul>" + "".join(f"<li><code>{e(u)}</code></li>"
                                                  for u in coder["units"]) + "</ul>")
                body_left.append('<p class="muted" style="margin-top:8px">Checkpoints: '
                                 + e("; ".join(coder["checkpoints"])) + "</p>")
        elif coder:
            g = coder["git"]
            if coder.get("notes"):
                body_left.append(f"<p>{e(fit(coder['notes'], 240, compact))}</p>")
            body_left.append(f'<p class="muted"><code>{e(g["from"][:8])}..{e(g["head"][:8])}</code> · '
                             f'{e(plural(g["files"], "file"))} · {e(tests_text(coder.get("tests")))}</p>')
            for dev in coder.get("deviations") or []:
                body_left.append(f"<p>Deviation: {e(fit(dev, 140, compact))}</p>")
        body_left.append("</div>")
        body_right = ['<div><div class="who who--critic">Critic</div>']
        if critic:
            body_right.append(f"<p>{e(fit(critic['summary'], 240, compact))}</p>")
            if items:
                body_right.append('<ul class="crit">' + "".join(critique_li(i, compact) for i in items)
                                  + "</ul>")
        body_right.append("</div>")
        notes = "".join(
            f'<div class="lead-note"><b class="who--lead">Lead stepped in</b> '
            f'({e(interventions[q]["reason"].replace("_", " "))}): {e(interventions[q]["summary"])}'
            + "".join(f'<div class="muted">· {e(fit(a, 300, compact))}</div>'
                      for a in interventions[q].get("actions") or [])
            + "</div>"
            for q in rnd.get("interventions") or [] if q in interventions)
        rows.append(
            f'<details class="round"><summary><span class="rk">{e(rnd["label"])}'
            f'<small>{e(clock(rnd["start_min"]))}–{e(clock(rnd["end_min"]))}</small></span>'
            f'<span class="rt"><div class="rtt">{e(fit(title, 200, compact))}</div>'
            f'<div class="sub">{e(sub)}</div></span>'
            f'<span class="glyphs">{glyphs}</span></summary>'
            f'<div class="round-body">{"".join(body_left)}{"".join(body_right)}</div>'
            + (f'<div style="padding:0 0 14px 24px">{notes}</div>' if notes else "") + "</details>")
    legend = ('<div class="legend"><span><i class="g g--accepted"></i>accepted</span>'
              '<span><i class="g g--partial"></i>partly</span><span><i class="g g--rejected"></i>rejected</span>'
              '<span><i class="g g--withdrawn"></i>withdrawn</span>'
              '<span><i class="g g--accepted s--blocking"></i>blocking</span>'
              '<span><i class="g g--accepted s--should_fix"></i>should fix</span>'
              '<span><i class="g g--accepted s--consider"></i>consider</span></div>')
    hint = ("open a round for its critiques; the page version has each exchange in full"
            if compact else "open a round for the exchange")
    loose = [iv for iv in s["interventions"] if not iv.get("round")]
    aside = "".join(
        f'<div class="lead-note"><b class="who--lead">Lead stepped in</b> at {e(clock(iv.get("at_min") or 0))} '
        f'({e(iv["reason"].replace("_", " "))}): {e(iv["summary"])}'
        + "".join(f'<div class="muted">· {e(fit(a, 300, compact))}</div>' for a in iv.get("actions") or [])
        + "</div>" for iv in loose)
    return ('<section aria-labelledby="h-rounds"><h2 id="h-rounds">Rounds'
            f'<small>{e(hint)}</small></h2>'
            f'<div class="rounds">{"".join(rows)}</div>{legend}{aside}</section>')


def decisions(s: Dict[str, Any], compact: bool = False) -> str:
    picks = [i for i in s["item_list"]
             if i["outcome"] in ("rejected", "partial", "open") or i.get("decided_by") == "lead"]
    picks.sort(key=lambda i: (SEV_ORDER[i["severity"]], id_key(i["id"])))
    if not picks:
        body = '<p class="muted">The coder accepted every critique.</p>'
    else:
        rnd_label = {r["key"]: r["label"] for r in s["rounds"]}
        cut = lambda text: e(fit(text, 480, compact))  # noqa: E731
        blocks = []
        for i in picks:
            dl = []
            claim = " ".join(x for x in (i.get("detail"), i.get("suggestion")) if x)
            if claim:
                dl.append(f'<dt class="who--critic">Critic</dt><dd>{cut(claim)}</dd>')
            if i.get("rationale"):
                dl.append(f'<dt class="who--coder">Coder</dt><dd>{cut(i["rationale"])}</dd>')
            if i.get("rebuttal"):
                r = i["rebuttal"]
                dl.append(f'<dt class="who--critic">Last word</dt><dd>{e(r["position"].capitalize())}'
                          + (f" — {cut(r['argument'])}" if r.get("argument") else "")
                          + (f" Amended: {cut(r['amended_suggestion'])}" if r.get("amended_suggestion") else "")
                          + "</dd>")
            if i.get("final_rationale"):
                dl.append(f'<dt class="who--coder">Final call</dt><dd>{cut(i["final_rationale"])}</dd>')
            if i.get("ruling"):
                dl.append(f'<dt class="who--lead">Lead</dt><dd>{e(OUTCOME_LABELS[i["outcome"]].capitalize())}'
                          f' — {cut(i["ruling"]["rationale"])}</dd>')
            blocks.append(
                f'<div class="decision">{glyph(i)}<div><h3>{e(i["title"])}{review_flags(i)}</h3>'
                f'<div class="meta">{e(i["id"])} · {e(SEVERITY_LABELS[i["severity"]])} · '
                f'{e(LENS_LABELS[i["lens"]])} · {e(rnd_label.get(i["round"], i["round"]))} · '
                f'{e(OUTCOME_LABELS[i["outcome"]])}'
                + (" by the lead" if i.get("decided_by") == "lead" else "") + "</div></div>"
                f'<dl>{"".join(dl)}</dl></div>')
        body = f'<div class="decisions">{"".join(blocks)}</div>'
    return ('<section aria-labelledby="h-dec"><h2 id="h-dec">Decisions to check'
            '<small>critiques the coder did not take in full, and lead rulings</small></h2>' + body + "</section>")


def lens_grid(s: Dict[str, Any]) -> str:
    keys = [r["key"] for r in s["rounds"] if r.get("critic")]
    short = {k: ("plan" if k == "plan" else k) for k in keys}
    cells: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for i in s["item_list"]:
        cells.setdefault((i["lens"], i["round"]), []).append(i)
    head = "".join(f'<th scope="col">{e(short[k])}</th>' for k in keys)
    rows = []
    for lens in s["items"]["by_lens"]:
        tds = []
        total = 0
        for k in keys:
            got = cells.get((lens, k), [])
            total += len(got)
            if got:
                level = min(len(got), 3)
                x = " x" if any(i["outcome"] != "accepted" for i in got) else ""
                title = f"{LENS_LABELS[lens]} · {k}: " + ", ".join(i["id"] for i in got)
                tds.append(f'<td><span class="c{level}{x}" title="{e(title)}">{len(got)}</span></td>')
            else:
                tds.append("<td><span></span></td>")
        rows.append(f'<tr><th scope="row">{e(LENS_LABELS[lens])}</th>{"".join(tds)}'
                    f'<td class="t">{total}</td></tr>')
    foot = "".join(f'<td class="t" style="text-align:center">{sum(1 for i in s["item_list"] if i["round"] == k)}</td>'
                   for k in keys)
    return ('<div><h2>Where the critique landed<small>outlined: not fully taken</small></h2>'
            f'<table class="lens"><thead><tr><th></th>{head}<th class="t">Σ</th></tr></thead>'
            f'<tbody>{"".join(rows)}<tr><th scope="row" class="muted">All lenses</th>{foot}'
            f'<td class="t">{len(s["item_list"])}</td></tr></tbody></table></div>')


def files(s: Dict[str, Any]) -> str:
    diff = s["diff"]
    order = ["source", "tests", "docs", "other"]
    kinds = sorted(diff["by_kind"].items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 9)
    seg = bar([(f"k-{k}", v["adds"] + v["dels"], k) for k, v in kinds],
              ", ".join(f"{k}: +{v['adds']} −{v['dels']}" for k, v in kinds))
    legend = "".join(f'<span><i class="sw k-{e(k)}"></i>{e(k)} +{v["adds"]} −{v["dels"]}</span>'
                     for k, v in kinds)
    top = diff["files_list"][:8]
    rows = "".join(f'<tr><td class="p">{e(f["path"])}</td><td class="n add">+{f["adds"]}</td>'
                   f'<td class="n del">−{f["dels"]}</td></tr>' for f in top)
    more = len(diff["files_list"]) - len(top)
    if more > 0:
        rows += f'<tr><td class="muted" colspan="3">and {plural(more, "more file")}</td></tr>'
    return (f'<div><h2>What changed<small>{e(plural(diff["commits"], "commit"))}</small></h2>{seg}'
            f'<div class="legend" style="margin-bottom:10px">{legend}</div>'
            f'<table class="files"><tbody>{rows}</tbody></table></div>')


STATUS_TEXT = {"fixed": "fixed", "open": "open", "proposed": "proposed issue",
               "accepted_risk": "accepted risk", "wont_fix": "won't fix", "issue_filed": "issue filed"}


def finding_row(f: Dict[str, Any], rnd_label: Dict[str, str], opened: bool, compact: bool) -> str:
    if f["status"] == "issue_filed":
        target = url(f.get("issue_url"))
        num = target.rstrip("/").rsplit("/", 1)[-1] if target else ""
        st = link(target, f"issue #{num}" if num.isdigit() else "issue")
    else:
        st = e(STATUS_TEXT[f["status"]])
    attention = f["status"] == "open"
    cut = lambda text, limit: e(text if attention else fit(text, limit, compact))  # noqa: E731
    body = []
    if f.get("where"):
        body.append(f'<div><code>{cut(f["where"], 90)}</code></div>')
    if f.get("scenario"):
        body.append(f'<div>{cut(f["scenario"], 180)}</div>')
    if f.get("evidence"):
        body.append(f'<div><b>{"Checked" if f["status"] != "fixed" else "Fix verified"}:</b> '
                    f'{cut(f["evidence"], 200)}</div>')
    if f.get("note"):
        label = "Still open" if attention else "Note"
        body.append(f'<div><b>{label}:</b> {cut(f["note"], 200)}</div>')
    tail = []
    if f.get("addressed_in"):
        tail.append("Addressed in " + ", ".join(rnd_label.get(k, k) for k in f["addressed_in"]) + ".")
    if f.get("matches_item"):
        tail.append(f"The critic raised the same problem as {f['matches_item']}.")
    if tail:
        body.append(f'<div>{e(" ".join(tail))}</div>')
    ident = f["id"] + (f" ({f['ref']})" if f.get("ref") else "")
    return (f'<details class="finding"{" open" if opened else ""}><summary>'
            f'<span class="sev s--{e(f["severity"])}">{e(SEVERITY_LABELS[f["severity"]])}</span>'
            f'<span class="ft"><span class="mono">{e(ident)}</span> · {e(f["title"])}</span>'
            f'<span class="st st--{e(f["status"])}">{st}</span></summary>'
            + (f'<div class="sub">{"".join(body)}</div>' if body else "") + "</details>")


def review(s: Dict[str, Any], compact: bool = False) -> str:
    reviews = s["reviews"]
    if not reviews:
        return ('<section><h2>Project review</h2>'
                '<p class="muted">The project review has not run.</p></section>')
    rnd_label = {r["key"]: r["label"] for r in s["rounds"]}
    several = len(reviews) > 1
    out = []
    for n, r in enumerate(sorted(reviews, key=lambda r: r["stage"] != "before_run")):
        name = review_name(r, several)
        if not r.get("ran"):
            out.append(f'<section><h2>{e(name)}</h2><p>The project review did not run: '
                       f'{e(r["reason"])}</p></section>')
            continue
        mine = sorted((f for f in s["findings"] if f["stage"] == r["stage"]),
                      key=lambda f: (SEV_ORDER[f["severity"]], id_key(f["id"])))
        long_list = len(mine) > LIST_OPEN_LIMIT
        rows = [finding_row(f, rnd_label, f["status"] == "open" or not long_list, compact)
                for f in mine]
        who = ", ".join(r.get("reviewers") or [])
        meta = f"{plural(len(r.get('reviewers', [])), 'reviewer')}: {who}"
        if r.get("skipped"):
            meta += f". Skipped: {', '.join(r['skipped'])}"
        if r.get("source"):
            meta += f". Source: {r['source']}"
        if r["stage"] == "before_run":
            small = "ran before this run" + (f", at {r['head_sha'][:10]}" if r.get("head_sha") else "")
            small += " · statuses as they stand after the fixes"
            verdict = "Verdict at the time"
        else:
            small = "project-review-process on the pull request"
            verdict = "Verdict"
        groups = finding_counts(mine)
        tally = ""
        if mine:
            tally = (bar([(cls, k, label) for label, cls, k in groups],
                         ", ".join(f"{k} {label}" for label, _, k in groups))
                     + '<div class="legend" style="margin-bottom:10px">'
                     + "".join(f'<span><i class="sw {e(cls)}"></i>{k} {e(label)}</span>'
                               for label, cls, k in groups) + "</div>")
        body = (f'<p style="margin:0 0 10px">{e(verdict)}: <b>{e(VERDICT_LABELS[r["verdict"]])}</b>. '
                f'<span class="muted">{e(fit(meta, 420, compact))}.</span></p>' + tally
                + (f'<div class="findings">{"".join(rows)}</div>' if rows else
                   '<p class="muted">No findings.</p>'))
        out.append(f'<section aria-labelledby="h-review-{n}"><h2 id="h-review-{n}">{e(name)}'
                   f'<small>{e(small)}</small></h2>{body}</section>')
    return "".join(out)


def next_steps(s: Dict[str, Any]) -> str:
    steps = s["narrative"].get("next_steps") or []
    if not steps:
        return ""
    return ('<section><h2>Next steps</h2><ol class="steps">'
            + "".join(f"<li>{e(x)}</li>" for x in steps) + "</ol></section>")


def footer(s: Dict[str, Any], compact: bool = False) -> str:
    run = s["run"]
    w = run["workers"]

    def who(role: str) -> str:
        x = w.get(role) or {}
        bits = [x.get("model") or "unknown model"]
        if x.get("effort"):
            bits.append(f"effort {x['effort']}")
        if x.get("runtime"):
            bits.append(x["runtime"])
        return f"{role.capitalize()}: {', '.join(bits)}"

    base, head = run["repo"].get("base_sha") or "", run.get("head_sha") or ""
    tests = s.get("tests")
    parts = [f"Built from the run ledger ({run['events']} events) and "
             f"<code>git diff {e(base[:8])}...{e(head[:8])}</code>.",
             e(f"{who('coder')}. {who('critic')}."),
             "Times are the gaps between recorded turns."]
    if tests and "not_run" not in tests:
        parts.append(e(f"Test counts are the coder's last reported run: {tests_text(tests)}."))
    if compact:
        parts.append("This is the compact view: long texts are shortened. The page version "
                     "has every critique, decision and finding in full.")
    parts.append(e(f"Generated {s['generated_at']}."))
    return f'<footer>{" ".join(parts)}</footer>'


def slim_css(css: str) -> str:
    """Drop comments and indentation; the rules stay as written."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = "".join(line.strip() for line in css.splitlines())
    return re.sub(r"\s*([{};,])\s*", r"\1", css).replace(": ", ":")


def render(stats: Dict[str, Any], mode: str, css: Optional[str] = None,
           detail: Optional[str] = None) -> str:
    """The report as one HTML document; raises StatsError for malformed stats."""
    if mode not in ("page", "t3"):
        raise ValueError("mode must be 'page' or 't3'")
    detail = detail or ("compact" if mode == "t3" else "full")
    if detail not in ("full", "compact"):
        raise ValueError("detail must be 'full' or 'compact'")
    compact = detail == "compact"
    stats = upgrade(stats)
    validate_stats(stats)
    css = CSS.read_text(encoding="utf-8") if css is None else css
    if compact:
        css = slim_css(css)
    body = "".join([
        masthead(stats), stats_strip(stats), timeline(stats), criteria(stats, compact),
        rounds(stats, compact), decisions(stats, compact),
        f'<section class="cols">{lens_grid(stats)}{files(stats)}</section>',
        review(stats, compact), next_steps(stats), footer(stats, compact),
    ])
    root = ' class="cct-page"' if mode == "page" else ""
    return ('<!doctype html>\n'
            f'<html lang="en"{root}><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{e(stats["run"]["title"])} · work report</title>'
            f'<style>{css}</style></head><body>'
            f'<main class="cct" data-mode="{mode}" data-detail="{detail}">{body}</main></body></html>\n')


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="render_report.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("stats", help="stats.json written by 'cct.py stats'")
    parser.add_argument("--mode", choices=["page", "t3"], default="page")
    parser.add_argument("--detail", choices=["full", "compact"],
                        help="full: every text in full (default for page). compact: every section, "
                             "long texts shortened (default for t3)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    stats = json.loads(Path(args.stats).read_text(encoding="utf-8"))
    try:
        html_text = render(stats, args.mode, detail=args.detail)
    except StatsError as exc:
        print(f"Refused: {exc}; regenerate it with cct.py stats", file=sys.stderr)
        return 2
    Path(args.out).write_text(html_text, encoding="utf-8")
    print(f"Wrote {args.out} ({len(html_text.encode('utf-8')) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

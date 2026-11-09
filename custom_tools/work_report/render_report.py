#!/usr/bin/env python3
"""Render the coder-critic work report from the statistics cct.py computes.

    render_report.py STATS.json --mode page --out report.html
    render_report.py STATS.json --mode t3   --out report.t3.html

The output is one self-contained HTML file with inline CSS and no scripts, so
it also opens where scripts are blocked. `page` uses the report's own light
and dark palette; `t3` uses T3 Code's theme variables and sets no page
background, for display inside a T3 thread. Design rules:
references/report-design.md. Standard library only; Python 3.9 or newer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
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


def validate_stats(s: Any) -> None:
    """Raise StatsError unless s has every field the renderer reads, typed."""
    if not isinstance(s, dict) or s.get("schema") != 1:
        raise StatsError("stats must be an object with schema 1")
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
    review = _get(s, "review", "stats", dict, required=False)
    if review:
        if _get(review, "ran", "review", bool):
            _get(review, "verdict", "review", str, allowed=list(VERDICT_LABELS))
            _strs(review, "reviewers", "review")
            _strs(review, "skipped", "review", required=False)
            sev = _get(review, "by_severity", "review", dict)
            for key in SEVERITY_LABELS:
                _get(sev, key, "review.by_severity", int)
            for n, f in enumerate(_objs(review, "findings", "review")):
                path = f"review.findings[{n}]"
                for key in ("id", "title"):
                    _get(f, key, path, str)
                _get(f, "severity", path, str, allowed=list(SEVERITY_LABELS))
                _get(f, "status", path, str,
                     allowed=["open", "fixed", "issue_filed", "proposed", "accepted_risk", "wont_fix"])
                for key in ("where", "scenario", "evidence", "matches_item", "issue_url"):
                    _get(f, key, path, str, required=False)
        else:
            _get(review, "reason", "review", str)
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


def decided(item: Dict[str, Any]) -> str:
    """One line for the glyph tooltip: the outcome, who settled it and why."""
    outcome = item["outcome"]
    words = OUTCOME_LABELS[outcome].capitalize()
    if outcome in ("accepted", "partial", "rejected"):
        words += " by the lead" if item.get("decided_by") == "lead" else " by the coder"
    reason = ((item.get("ruling") or {}).get("rationale") or item.get("final_rationale")
              or item.get("rationale") or "")
    return f"{words}: {shorten(reason)}" if reason else words


def glyph(item: Dict[str, Any]) -> str:
    """A critique mark with a hover summary. CSS only: the report runs no script."""
    label = (f"{item['id']}: {SEVERITY_LABELS[item['severity']].lower()}, "
             f"{LENS_LABELS[item['lens']]}, {OUTCOME_LABELS[item['outcome']]}")
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

def masthead(s: Dict[str, Any]) -> str:
    run, review = s["run"], s.get("review")
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
    if review and review.get("ran"):
        fixed = sum(1 for f in review["findings"] if f["status"] == "fixed")
        filed = sum(1 for f in review["findings"] if f["status"] == "issue_filed")
        extra = []
        if fixed:
            extra.append(f"{fixed} fixed")
        if filed:
            extra.append(f"{plural(filed, 'follow-up issue')} filed")
        verdict.append(f"<span>Project review: <b>{e(VERDICT_LABELS[review['verdict']])}</b>"
                       + (f' <span class="muted">· {e(" · ".join(extra))}</span>' if extra else "")
                       + "</span>")
    elif review:
        verdict.append('<span>Project review: <b>not run</b></span>')
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


def criteria(s: Dict[str, Any]) -> str:
    rows = []
    for c in s["acceptance"]:
        mark, word = CRITERION_MARKS[c["status"]]
        if c["status"] == "partial":
            mark = '<span class="g g--partial s--should_fix"></span>'
        rows.append(f'<li><span class="mark mark--{e(c["status"])}" role="img" aria-label="{e(word)}">'
                    f'{mark}</span><span class="mono muted">{e(c["id"])}</span><span>{e(c["text"])}</span>'
                    + (f'<span class="ev">{e(c["evidence"])}</span>' if c.get("evidence") else "")
                    + "</li>")
    return ('<section aria-labelledby="h-criteria"><h2 id="h-criteria">Acceptance criteria'
            '<small>checked by the lead</small></h2>'
            f'<ul class="criteria">{"".join(rows)}</ul></section>')


def stat(value: str, label: str, sub: str = "", extra: str = "") -> str:
    return (f'<div class="stat"><div class="v">{value}</div><div class="l">{e(label)}</div>'
            f'{extra}' + (f'<div class="l">{e(sub)}</div>' if sub else "") + "</div>")


def stats_strip(s: Dict[str, Any]) -> str:
    c, items, diff, review = s["counts"], s["items"], s["diff"], s.get("review")
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
    if review and review.get("ran"):
        sev = review["by_severity"]
        tiles.append(stat(str(len(review["findings"])), "Review findings",
                          f"{sev['blocking']} blocking · {sev['should_fix']} should fix · {sev['consider']} consider"))
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
        overlay.append(f'<div class="tl-phase" style="left:{pct(ph["start"], total)};'
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


def critique_li(item: Dict[str, Any]) -> str:
    meta = f"{item['id']} · {SEVERITY_LABELS[item['severity']]} · {LENS_LABELS[item['lens']]}"
    if item.get("where"):
        meta += f" · {item['where']}"
    says = []
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
    if item.get("matched_by_review"):
        says.append(f'<span class="flag">Project review raised this again: '
                    f'{e(", ".join(item["matched_by_review"]))}</span>')
    return (f'<li><div class="head">{glyph(item)}<div><div>{e(item["title"])}</div>'
            f'<div class="meta">{e(meta)}</div></div></div>'
            + "".join(f'<div class="says">{x}</div>' for x in says) + "</li>")


def rounds(s: Dict[str, Any]) -> str:
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
        else:
            title, sub = "Follow-up decisions", ""
        glyphs = "".join(glyph(i) for i in items) if items else (
            '<span class="muted">no critiques</span>' if critic else "")
        body_left = ['<div><div class="who who--coder">Coder</div>']
        if coder and coder["kind"] == "plan":
            body_left.append(f"<p>{e(coder['summary'])}</p><ul>"
                             + "".join(f"<li><code>{e(u)}</code></li>" for u in coder["units"]) + "</ul>")
            body_left.append('<p class="muted" style="margin-top:8px">Checkpoints: '
                             + e("; ".join(coder["checkpoints"])) + "</p>")
        elif coder:
            g = coder["git"]
            if coder.get("notes"):
                body_left.append(f"<p>{e(coder['notes'])}</p>")
            body_left.append(f'<p class="muted"><code>{e(g["from"][:8])}..{e(g["head"][:8])}</code> · '
                             f'{e(plural(g["files"], "file"))} · {e(tests_text(coder.get("tests")))}</p>')
            for dev in coder.get("deviations") or []:
                body_left.append(f"<p>Deviation: {e(dev)}</p>")
        body_left.append("</div>")
        body_right = ['<div><div class="who who--critic">Critic</div>']
        if critic:
            body_right.append(f"<p>{e(critic['summary'])}</p>")
            if items:
                body_right.append('<ul class="crit">' + "".join(critique_li(i) for i in items) + "</ul>")
        body_right.append("</div>")
        notes = "".join(
            f'<div class="lead-note"><b class="who--lead">Lead stepped in</b> '
            f'({e(interventions[q]["reason"].replace("_", " "))}): {e(interventions[q]["summary"])}</div>'
            for q in rnd.get("interventions") or [] if q in interventions)
        rows.append(
            f'<details class="round"><summary><span class="rk">{e(rnd["label"])}'
            f'<small>{e(clock(rnd["start_min"]))}–{e(clock(rnd["end_min"]))}</small></span>'
            f'<span class="rt"><div>{e(title)}</div><div class="sub">{e(sub)}</div></span>'
            f'<span class="glyphs">{glyphs}</span></summary>'
            f'<div class="round-body">{"".join(body_left)}{"".join(body_right)}</div>'
            + (f'<div style="padding:0 0 14px 24px">{notes}</div>' if notes else "") + "</details>")
    legend = ('<div class="legend"><span><i class="g g--accepted"></i>accepted</span>'
              '<span><i class="g g--partial"></i>partly</span><span><i class="g g--rejected"></i>rejected</span>'
              '<span><i class="g g--withdrawn"></i>withdrawn</span>'
              '<span><i class="g g--accepted s--blocking"></i>blocking</span>'
              '<span><i class="g g--accepted s--should_fix"></i>should fix</span>'
              '<span><i class="g g--accepted s--consider"></i>consider</span></div>')
    return ('<section aria-labelledby="h-rounds"><h2 id="h-rounds">Rounds<small>open a round for the '
            'exchange</small></h2>' f'<div class="rounds">{"".join(rows)}</div>{legend}</section>')


def decisions(s: Dict[str, Any]) -> str:
    picks = [i for i in s["item_list"]
             if i["outcome"] in ("rejected", "partial", "open") or i.get("decided_by") == "lead"]
    picks.sort(key=lambda i: (SEV_ORDER[i["severity"]], i["id"]))
    if not picks:
        body = '<p class="muted">The coder accepted every critique.</p>'
    else:
        rnd_label = {r["key"]: r["label"] for r in s["rounds"]}
        blocks = []
        for i in picks:
            dl = []
            claim = " ".join(x for x in (i.get("detail"), i.get("suggestion")) if x)
            if claim:
                dl.append(f'<dt class="who--critic">Critic</dt><dd>{e(claim)}</dd>')
            if i.get("rationale"):
                dl.append(f'<dt class="who--coder">Coder</dt><dd>{e(i["rationale"])}</dd>')
            if i.get("rebuttal"):
                r = i["rebuttal"]
                dl.append(f'<dt class="who--critic">Last word</dt><dd>{e(r["position"].capitalize())}'
                          + (f" — {e(r['argument'])}" if r.get("argument") else "")
                          + (f" Amended: {e(r['amended_suggestion'])}" if r.get("amended_suggestion") else "")
                          + "</dd>")
            if i.get("final_rationale"):
                dl.append(f'<dt class="who--coder">Final call</dt><dd>{e(i["final_rationale"])}</dd>')
            if i.get("ruling"):
                dl.append(f'<dt class="who--lead">Lead</dt><dd>{e(OUTCOME_LABELS[i["outcome"]].capitalize())}'
                          f' — {e(i["ruling"]["rationale"])}</dd>')
            flag = (f'<span class="flag">Project review raised this again: '
                    f'{e(", ".join(i["matched_by_review"]))}</span>' if i.get("matched_by_review") else "")
            blocks.append(
                f'<div class="decision">{glyph(i)}<div><h3>{e(i["title"])}{flag}</h3>'
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


def review(s: Dict[str, Any]) -> str:
    r = s.get("review")
    if not r:
        body = '<p class="muted">The project review has not run.</p>'
        return f'<section><h2>Project review</h2>{body}</section>'
    if not r.get("ran"):
        return ('<section><h2>Project review</h2><p>The project review did not run: '
                f'{e(r["reason"])}</p></section>')
    status_text = {"fixed": "fixed", "open": "open", "proposed": "proposed issue",
                   "accepted_risk": "accepted risk", "wont_fix": "won't fix"}
    rows = []
    for f in sorted(r["findings"], key=lambda f: (SEV_ORDER[f["severity"]], f["id"])):
        if f["status"] == "issue_filed":
            target = url(f.get("issue_url"))
            num = target.rstrip("/").rsplit("/", 1)[-1] if target else ""
            st = link(target, f"issue #{num}" if num.isdigit() else "issue")
        else:
            st = e(status_text.get(f["status"], f["status"]))
        sub = []
        if f.get("where"):
            sub.append(f"<code>{e(f['where'])}</code>")
        if f.get("scenario"):
            sub.append(e(f["scenario"]))
        if f.get("evidence"):
            sub.append("Fix verified: " + e(f["evidence"]))
        if f.get("matches_item"):
            sub.append(f"The critic raised this as {e(f['matches_item'])}.")
        rows.append(f'<div class="finding"><span class="sev s--{e(f["severity"])}">'
                    f'{e(SEVERITY_LABELS[f["severity"]])}</span><span>{e(f["id"])} · {e(f["title"])}</span>'
                    f'<span class="st st--{e(f["status"])}">{st}</span>'
                    + (f'<span class="sub">{" · ".join(sub)}</span>' if sub else "") + "</div>")
    who = ", ".join(r.get("reviewers") or [])
    skipped = r.get("skipped") or []
    meta = f"{plural(len(r.get('reviewers', [])), 'reviewer')}: {who}"
    if skipped:
        meta += f". Skipped: {', '.join(skipped)}"
    body = (f'<p style="margin:0 0 10px">Verdict: <b>{e(VERDICT_LABELS[r["verdict"]])}</b>. '
            f'<span class="muted">{e(meta)}.</span></p>'
            + (f'<div class="findings">{"".join(rows)}</div>' if rows else
               '<p class="muted">No findings.</p>'))
    return ('<section aria-labelledby="h-review"><h2 id="h-review">Project review'
            '<small>project-review-process on the pull request</small></h2>' + body + "</section>")


def next_steps(s: Dict[str, Any]) -> str:
    steps = s["narrative"].get("next_steps") or []
    if not steps:
        return ""
    return ('<section><h2>Next steps</h2><ol class="steps">'
            + "".join(f"<li>{e(x)}</li>" for x in steps) + "</ol></section>")


def footer(s: Dict[str, Any]) -> str:
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
    parts.append(e(f"Generated {s['generated_at']}."))
    return f'<footer>{" ".join(parts)}</footer>'


def render(stats: Dict[str, Any], mode: str, css: Optional[str] = None) -> str:
    """The report as one HTML document; raises StatsError for malformed stats."""
    if mode not in ("page", "t3"):
        raise ValueError("mode must be 'page' or 't3'")
    validate_stats(stats)
    css = CSS.read_text(encoding="utf-8") if css is None else css
    body = "".join([
        masthead(stats), criteria(stats), stats_strip(stats), timeline(stats), rounds(stats),
        decisions(stats), f'<section class="cols">{lens_grid(stats)}{files(stats)}</section>',
        review(stats), next_steps(stats), footer(stats),
    ])
    root = ' class="cct-page"' if mode == "page" else ""
    return ('<!doctype html>\n'
            f'<html lang="en"{root}><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{e(stats["run"]["title"])} · work report</title>'
            f'<style>{css}</style></head><body>'
            f'<main class="cct" data-mode="{mode}">{body}</main></body></html>\n')


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="render_report.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("stats", help="stats.json written by 'cct.py stats'")
    parser.add_argument("--mode", choices=["page", "t3"], default="page")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    stats = json.loads(Path(args.stats).read_text(encoding="utf-8"))
    try:
        html_text = render(stats, args.mode)
    except StatsError as exc:
        print(f"Refused: {exc}; regenerate it with cct.py stats", file=sys.stderr)
        return 2
    Path(args.out).write_text(html_text, encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

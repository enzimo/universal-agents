"""The coder-critic work report, rendered for the ``report`` page kind.

``publish_single_page_site`` publishes the work report of a coder-critic-team
run as a page of kind ``report``. The page uses the report's own design rather
than the Universal Agents theme, because that design is the report: its layout,
glyphs and palette are specified once, in the canonical ``coder-critic-team``
skill of the sibling ua-skills repository (``references/report-design.md``),
and every runtime that shows the report must show the same page.

``render_report.py`` and ``report.css`` here are therefore byte-identical
copies of that skill's ``scripts/render_report.py`` and ``assets/report.css``.
Do not edit them in this repository: change them in ua-skills, then run
``python -m scripts.sync_work_report_renderer`` (``--check`` verifies the
copies without writing).

The copied module is safe to run on statistics an agent passes in: it checks
every field it reads (``validate_stats``, raising ``StatsError``), escapes
every string, makes only http(s) links into anchors, and writes one HTML
document with inline CSS and no scripts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from custom_tools.work_report.render_report import StatsError, render, validate_stats

# The copied stylesheet, read and passed explicitly: the copied module's own
# default (``CSS``) points at the skill's ``assets/`` folder, which this
# package does not have.
WORK_REPORT_CSS_PATH = Path(__file__).with_name("report.css")


def render_work_report_page(stats: dict[str, Any]) -> str:
    """The report as a standalone page (the report's own light and dark palette).

    Raises ``StatsError`` (a ``ValueError``) when ``stats`` is not in the shape
    ``cct.py stats`` writes.
    """

    return render(stats, "page", css=WORK_REPORT_CSS_PATH.read_text(encoding="utf-8"))


__all__ = ["StatsError", "WORK_REPORT_CSS_PATH", "render_work_report_page", "validate_stats"]

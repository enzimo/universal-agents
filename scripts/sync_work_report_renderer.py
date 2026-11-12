#!/usr/bin/env python3
"""Copy the coder-critic work report renderer from ua-skills, or verify the copy.

The ``report`` page kind of ``publish_single_page_site`` renders with
byte-identical copies of two files of the canonical ``coder-critic-team``
skill (see ``custom_tools/work_report/__init__.py``). Change them in the
sibling ua-skills checkout, then run ``python -m scripts.sync_work_report_renderer``
and repeat it with ``--check``. ``--check`` writes nothing; it fails when the
canonical skill is missing or a copy differs.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANONICAL_ROOT = ROOT.parent / "ua-skills" / "skills" / "coder-critic-team"
DESTINATION = ROOT / "custom_tools" / "work_report"
# File in the canonical skill -> its copy in DESTINATION.
COPIED_FILES = {
    "scripts/render_report.py": "render_report.py",
    "assets/report.css": "report.css",
}


def canonical_skill_missing(canonical_root: Path) -> list[str]:
    """The canonical files that are not there (all of them when the skill is absent)."""

    return [
        relative
        for relative in ("SKILL.md", *COPIED_FILES)
        if not (canonical_root / relative).is_file()
    ]


def stale_copies(canonical_root: Path, destination: Path = DESTINATION) -> list[str]:
    """The copies whose bytes differ from the canonical files, or that are missing."""

    return [
        copied
        for relative, copied in COPIED_FILES.items()
        if not (destination / copied).is_file()
        or (destination / copied).read_bytes() != (canonical_root / relative).read_bytes()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Fail when a copy is stale; write nothing.")
    parser.add_argument("--canonical-root", type=Path, default=DEFAULT_CANONICAL_ROOT)
    args = parser.parse_args(argv)
    missing = canonical_skill_missing(args.canonical_root)
    if missing:
        logging.error("Canonical skill is missing: %s (no %s)", args.canonical_root, ", ".join(missing))
        return 1
    stale = stale_copies(args.canonical_root)
    if stale and not args.check:
        for relative, copied in COPIED_FILES.items():
            if copied in stale:
                shutil.copyfile(args.canonical_root / relative, DESTINATION / copied)
        stale = stale_copies(args.canonical_root)
    if stale:
        logging.error(
            "The work report copies differ from %s: %s. Run: python -m scripts.sync_work_report_renderer",
            args.canonical_root,
            ", ".join(stale),
        )
        return 1
    logging.info("The work report copies match %s.", args.canonical_root)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())

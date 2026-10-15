#!/usr/bin/env python3
"""Report cognitive complexity for changed, repository-owned Python files."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from typing import Sequence


LOGGER = logging.getLogger("cognitive_complexity_advisory")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "complexipy.toml"


def _configured_paths(config_path: Path = CONFIG_PATH) -> tuple[Path, ...]:
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    return tuple(Path(str(value)) for value in config.get("paths", []))


def _is_in_scope(path: Path, configured_paths: Sequence[Path]) -> bool:
    if path.suffix != ".py" or path.is_absolute():
        return False
    for configured in configured_paths:
        if configured.suffix == ".py":
            if path == configured:
                return True
            continue
        if path == configured or configured in path.parents:
            return True
    return False


def _git_changed_paths(base_ref: str) -> list[Path]:
    changed_result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base_ref, "--"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    untracked_result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    configured_paths = _configured_paths()
    changed_paths: set[Path] = set()
    for raw_path in (*changed_result.stdout.splitlines(), *untracked_result.stdout.splitlines()):
        value = raw_path.strip()
        if not value:
            continue
        path = Path(value)
        if _is_in_scope(path, configured_paths):
            changed_paths.add(path)
    return sorted(changed_paths)


def _base_ref_exists(base_ref: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{base_ref}^{{commit}}"],
        cwd=PROJECT_ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _complexipy_executable() -> str | None:
    sibling = Path(sys.executable).with_name("complexipy")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("complexipy")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Advisory Complexipy report for Python files changed from a Git baseline."
    )
    parser.add_argument(
        "base_ref",
        nargs="?",
        default="origin/main",
        help="Git commit or ref to compare with the current working tree (default: origin/main).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not _base_ref_exists(args.base_ref):
        LOGGER.warning(
            "Cognitive complexity advisory skipped because baseline ref %r is unavailable.",
            args.base_ref,
        )
        return 0

    changed_paths = _git_changed_paths(args.base_ref)
    if not changed_paths:
        LOGGER.info("No changed repository-owned Python files require complexity analysis.")
        return 0

    executable = _complexipy_executable()
    if executable is None:
        LOGGER.error("Complexipy is not installed; install the locked development requirements.")
        return 2

    command = [
        executable,
        *(path.as_posix() for path in changed_paths),
        "--diff-only",
        args.base_ref,
    ]
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

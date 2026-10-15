"""Canaries must start as standalone scripts, as their CI commands invoke them."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CANARIES = sorted((ROOT / "tests").glob("live_*canary.py"))


@pytest.mark.parametrize("script", CANARIES, ids=lambda script: script.stem)
def test_canary_help_without_repository_pythonpath(script: Path, tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()

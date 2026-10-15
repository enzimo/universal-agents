"""Run the dark background renderer's mesh checks (tests/metallic_mesh_geometry.test.cjs)."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_dark_background_mesh_keeps_faces_broad_and_in_proportion() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to run the dark background renderer")
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [node, "--test", "tests/metallic_mesh_geometry.test.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-2000:]

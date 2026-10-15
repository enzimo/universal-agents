from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docker.python_entrypoints import resolve_python_entrypoint


def test_resolve_python_entrypoint_uses_legacy_pyc_when_source_is_stripped(tmp_path: Path) -> None:
    source = tmp_path / "assistant_cli.py"
    compiled = tmp_path / "assistant_cli.pyc"
    compiled.write_bytes(b"compiled")

    assert resolve_python_entrypoint(source) == compiled


def test_resolve_python_entrypoint_errors_when_source_and_pyc_are_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_python_entrypoint(tmp_path / "assistant_cli.py")

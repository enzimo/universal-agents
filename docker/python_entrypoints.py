"""Resolve Docker Python entrypoints from source or stripped bytecode images."""

from __future__ import annotations

from pathlib import Path


def resolve_python_entrypoint(source_path: Path) -> Path:
    """Return a runnable source path, or its legacy .pyc path after stripping."""
    source_path = Path(source_path)
    if source_path.is_file():
        return source_path

    if source_path.suffix == ".py":
        compiled_path = source_path.with_suffix(".pyc")
    else:
        compiled_path = source_path.with_name(f"{source_path.name}.pyc")
    if compiled_path.is_file():
        return compiled_path

    raise FileNotFoundError(f"Python entrypoint not found: {source_path} or {compiled_path}")

from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docker.python_entrypoints import resolve_python_entrypoint

REPO_ROOT = Path(__file__).resolve().parents[1]
STRIP_SCRIPT = REPO_ROOT / "docker" / "strip_python_sources.sh"
CONTAINER_ENTRYPOINT = REPO_ROOT / "docker" / "entrypoint.sh"
# The Python modules that start other Python programs by file path. Images
# keep only compiled files, so each must find its programs through
# resolve_python_entrypoint, never by naming a .py file directly.
LAUNCHER_MODULES = ("docker/launch_headless.py", "docker/run_bundle.py")
RESOLVER_NAME = resolve_python_entrypoint.__name__
CONSUMER_CHECK_PROGRAM = "scripts/reconcile_nats_consumers.py"


def test_resolve_python_entrypoint_uses_legacy_pyc_when_source_is_stripped(tmp_path: Path) -> None:
    source = tmp_path / "assistant_cli.py"
    compiled = tmp_path / "assistant_cli.pyc"
    compiled.write_bytes(b"compiled")

    assert resolve_python_entrypoint(source) == compiled


def test_resolve_python_entrypoint_errors_when_source_and_pyc_are_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_python_entrypoint(tmp_path / "assistant_cli.py")


def _required_entrypoints() -> list[str]:
    """The programs docker/strip_python_sources.sh refuses to build an image without."""

    script = STRIP_SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"required_entrypoints=\((.*?)\n\)", script, re.DOTALL)
    assert block, "docker/strip_python_sources.sh no longer lists required_entrypoints"
    return re.findall(r'"\$\{app_root\}/([^"]+)"', block.group(1))


def _path_parts(node: ast.expr) -> list[str]:
    """``REPO_ROOT / "docker" / "run_bundle.py"`` -> ``["docker", "run_bundle.py"]``."""

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _path_parts(node.left) + _path_parts(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return []


def _launcher_programs(relative_module: str) -> tuple[set[str], list[str]]:
    """The programs one launcher module resolves, and any .py file it names outside the resolver."""

    tree = ast.parse((REPO_ROOT / relative_module).read_text(encoding="utf-8"))
    programs: set[str] = set()
    resolved_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == RESOLVER_NAME:
            programs.add("/".join(_path_parts(node.args[0])))
            resolved_nodes.update(id(child) for child in ast.walk(node.args[0]))
    unresolved = [
        f"{relative_module}:{node.lineno} {node.value}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.endswith(".py")
        and id(node) not in resolved_nodes
    ]
    return programs, unresolved


def _container_entrypoint_programs() -> set[str]:
    """The programs docker/entrypoint.sh starts through python_program_path."""

    script = CONTAINER_ENTRYPOINT.read_text(encoding="utf-8")
    programs = set(re.findall(r'exec_python_entrypoint "\$APP_ROOT/([^"]+\.py)"', script))
    # The secret-file check sits next to the docker/ folder the script runs from.
    programs.update(re.findall(r'ENV_FILE_CHECK_SCRIPT="\$\(dirname "\$0"\)/\.\./([^"]+\.py)"', script))
    return programs


def _programs_started_by_path() -> set[str]:
    programs = _container_entrypoint_programs()
    for module in LAUNCHER_MODULES:
        programs.update(_launcher_programs(module)[0])
    return programs


def _strip(app_root: Path, shim_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the image's own strip script, with this interpreter as its ``python``."""

    shim_dir.mkdir(parents=True, exist_ok=True)
    python_shim = shim_dir / "python"
    if not python_shim.exists():
        python_shim.symlink_to(sys.executable)
    env = {**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    return subprocess.run(
        ["bash", str(STRIP_SCRIPT), str(app_root)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _copy_sources(app_root: Path, relative_paths: list[str]) -> None:
    for relative in relative_paths:
        target = app_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, target)


def test_launchers_name_python_programs_only_through_the_resolver() -> None:
    unresolved = [entry for module in LAUNCHER_MODULES for entry in _launcher_programs(module)[1]]

    assert unresolved == [], (
        "Images keep only compiled .pyc files; start these programs with "
        f"{RESOLVER_NAME}(REPO_ROOT / ...) and list them in docker/strip_python_sources.sh"
    )


def test_every_program_the_image_starts_runs_from_a_source_stripped_tree(tmp_path: Path) -> None:
    # Build the tree the image build guarantees: only the required entry
    # points, compiled and stripped by the build's own script.
    app_root = tmp_path / "app"
    _copy_sources(app_root, _required_entrypoints())
    result = _strip(app_root, tmp_path / "bin")
    assert result.returncode == 0, result.stderr
    assert not list(app_root.rglob("*.py"))

    programs = _programs_started_by_path()
    assert CONSUMER_CHECK_PROGRAM in programs
    for relative in sorted(programs):
        source = app_root / relative
        assert source.with_suffix(".pyc").is_file(), (
            f"{relative} is started by path but is not in required_entrypoints "
            "in docker/strip_python_sources.sh"
        )
        assert resolve_python_entrypoint(source) == source.with_suffix(".pyc")


def test_the_image_build_stops_when_a_required_entrypoint_is_missing(tmp_path: Path) -> None:
    app_root = tmp_path / "app"
    present = [relative for relative in _required_entrypoints() if relative != CONSUMER_CHECK_PROGRAM]
    _copy_sources(app_root, present)

    result = _strip(app_root, tmp_path / "bin")

    assert result.returncode == 2
    assert "Missing compiled Python entrypoint" in result.stderr
    assert str(app_root / "scripts" / "reconcile_nats_consumers.pyc") in result.stderr
    # The build stops before deleting anything, so the sources stay for diagnosis.
    assert (app_root / present[0]).is_file()

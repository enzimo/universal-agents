from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_provider_safety import sanitize_provider_failure
from conftest import pytest_runtest_makereport


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "run_tests.sh"


def _run_profile(tmp_path: Path, *arguments: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    capture_path = tmp_path / "pytest-arguments.txt"
    fake_pytest = tmp_path / "pytest"
    fake_pytest.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' \"$@\" > \"${CAPTURE_FILE}\"\n"
        "printf '%s\\n' \"${LITELLM_LOCAL_MODEL_COST_MAP}\"\n",
        encoding="utf-8",
    )
    fake_pytest.chmod(0o755)
    completed = subprocess.run(
        ["bash", str(SCRIPT), *arguments],
        cwd=PROJECT_ROOT,
        env={
            **os.environ, "PYTEST": str(fake_pytest), "CAPTURE_FILE": str(capture_path),
            "LITELLM_LOCAL_MODEL_COST_MAP": "false",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    captured = capture_path.read_text(encoding="utf-8").splitlines() if capture_path.exists() else []
    return completed, captured


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ((), ["-m", "not live and not external"]),
        (("fast", "-q"), ["-m", "not live and not external", "-q"]),
        (("live",), ["-m", "live and not external"]),
        (("live-nats",), ["-m", "live and nats and not external"]),
        (
            ("external",),
            ["-m", "external", "--tb=short", "--show-capture=no"],
        ),
        (
            ("full", "tests/test_config.py"),
            ["--tb=short", "--show-capture=no", "tests/test_config.py"],
        ),
        (("collect", "-q"), ["--collect-only", "-q"]),
    ],
)
def test_test_suite_runner_selects_expected_pytest_arguments(
    tmp_path: Path,
    arguments: tuple[str, ...],
    expected: list[str],
) -> None:
    completed, captured = _run_profile(tmp_path, *arguments)

    assert completed.returncode == 0, completed.stderr
    assert captured == expected
    profile = arguments[0] if arguments else "fast"
    assert completed.stdout.strip() == ("true" if profile in {"fast", "collect"} else "false")


def test_test_suite_runner_rejects_unknown_profile(tmp_path: Path) -> None:
    completed, captured = _run_profile(tmp_path, "unknown")

    assert completed.returncode == 2
    assert captured == []
    assert "Usage:" in completed.stderr


def test_live_provider_failure_sanitizer_removes_environment_credentials() -> None:
    secret = "provider-secret-value-123456789"
    error = RuntimeError(f"provider rejected api_key={secret}\nrequest failed")

    rendered = sanitize_provider_failure(
        error,
        environ={"LITELLM_API_KEY": secret},
    )

    assert secret not in rendered
    assert "[REDACTED]" in rendered
    assert "\n" not in rendered


def test_external_failure_report_omits_credentials_and_captured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "provider-secret-value-987654321"
    monkeypatch.setenv("LITELLM_API_KEY", secret)
    report = SimpleNamespace(
        failed=True,
        longrepr=f"unsafe traceback {secret}",
        sections=[("Captured log", f"unsafe log {secret}")],
    )
    outcome = SimpleNamespace(get_result=lambda: report)
    item = SimpleNamespace(
        location=("tests/fake_external.py", 4, "test_failure"),
        get_closest_marker=lambda name: object() if name == "external" else None,
    )
    call = SimpleNamespace(
        excinfo=SimpleNamespace(value=RuntimeError(f"provider rejected {secret}"))
    )
    hook = pytest_runtest_makereport(item, call)

    next(hook)
    with pytest.raises(StopIteration):
        hook.send(outcome)

    assert secret not in report.longrepr
    assert "[REDACTED]" in report.longrepr
    assert report.sections == []


def test_live_pytest_modules_declare_live_marker() -> None:
    missing = [
        path.name
        for path in sorted((PROJECT_ROOT / "tests").glob("test_live_*.py"))
        if "pytest.mark.live" not in path.read_text(encoding="utf-8")
    ]

    assert missing == []


def test_standalone_live_modules_do_not_hide_pytest_tests() -> None:
    hidden: list[str] = []
    for path in sorted((PROJECT_ROOT / "tests").glob("live_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                hidden.append(f"{path.name}:{node.lineno}:{node.name}")

    assert hidden == []

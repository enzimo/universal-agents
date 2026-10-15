from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts import report_cognitive_complexity


def test_is_in_scope_uses_repository_owned_configured_paths() -> None:
    configured = (Path("custom_agents"), Path("assistant_cli.py"))

    assert report_cognitive_complexity._is_in_scope(
        Path("custom_agents/runtime.py"), configured
    )
    assert report_cognitive_complexity._is_in_scope(Path("assistant_cli.py"), configured)
    assert not report_cognitive_complexity._is_in_scope(Path("tests/test_runtime.py"), configured)
    assert not report_cognitive_complexity._is_in_scope(
        Path("custom_agents/runtime.js"), configured
    )


def test_main_invokes_complexipy_only_for_changed_in_scope_files(monkeypatch) -> None:
    monkeypatch.setattr(report_cognitive_complexity, "_base_ref_exists", lambda _ref: True)
    monkeypatch.setattr(
        report_cognitive_complexity,
        "_git_changed_paths",
        lambda _ref: [Path("custom_agents/runtime.py"), Path("assistant_cli.py")],
    )
    monkeypatch.setattr(
        report_cognitive_complexity,
        "_complexipy_executable",
        lambda: "/tools/complexipy",
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(report_cognitive_complexity.subprocess, "run", fake_run)

    assert report_cognitive_complexity.main(["base-sha"]) == 0
    assert calls == [
        (
            [
                "/tools/complexipy",
                "custom_agents/runtime.py",
                "assistant_cli.py",
                "--diff-only",
                "base-sha",
            ],
            {"cwd": report_cognitive_complexity.PROJECT_ROOT, "check": False},
        )
    ]


def test_git_changed_paths_includes_untracked_python_files(monkeypatch) -> None:
    monkeypatch.setattr(
        report_cognitive_complexity,
        "_configured_paths",
        lambda: (Path("custom_agents"), Path("scripts")),
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "diff":
            return SimpleNamespace(stdout="custom_agents/runtime.py\ntests/test_runtime.py\n")
        return SimpleNamespace(stdout="scripts/new_check.py\nnotes.md\n")

    monkeypatch.setattr(report_cognitive_complexity.subprocess, "run", fake_run)

    assert report_cognitive_complexity._git_changed_paths("base-sha") == [
        Path("custom_agents/runtime.py"),
        Path("scripts/new_check.py"),
    ]
    assert calls == [
        ["git", "diff", "--name-only", "--diff-filter=ACMR", "base-sha", "--"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]


def test_main_skips_clean_or_unavailable_baselines(monkeypatch) -> None:
    monkeypatch.setattr(report_cognitive_complexity, "_base_ref_exists", lambda _ref: False)
    assert report_cognitive_complexity.main(["missing"]) == 0

    monkeypatch.setattr(report_cognitive_complexity, "_base_ref_exists", lambda _ref: True)
    monkeypatch.setattr(report_cognitive_complexity, "_git_changed_paths", lambda _ref: [])
    assert report_cognitive_complexity.main(["clean"]) == 0

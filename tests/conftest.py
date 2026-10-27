from __future__ import annotations

import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from live_provider_safety import sanitize_provider_failure  # noqa: E402
from tests.postgres_marker_guard import unmarked_postgres_cases  # noqa: E402


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Refuse PostgreSQL cases that no profile would ever run (see postgres_marker_guard)."""

    missing = unmarked_postgres_cases(items)
    if missing:
        listed = "\n  ".join(missing[:20])
        raise pytest.UsageError(
            f"{len(missing)} PostgreSQL test case(s) lack the postgres marker, so the PostgreSQL CI "
            "job never selects them. Parametrize the backend with "
            "pytest.param(\"postgres\", marks=[pytest.mark.live, pytest.mark.postgres]) or reuse "
            f"tests.knowledge_support.BACKENDS:\n  {listed}"
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo[Any],
) -> Generator[None, None, None]:
    """Replace external-test failure reports before pytest renders credentials."""

    outcome = yield
    report = outcome.get_result()
    if (
        report.failed
        and call.excinfo is not None
        and item.get_closest_marker("external") is not None
    ):
        exc = call.excinfo.value
        detail = sanitize_provider_failure(exc, environ=os.environ)
        path, line, _domain = item.location
        report.longrepr = (
            f"{path}:{line + 1}: external test failed "
            f"({type(exc).__name__}): {detail}"
        )
        report.sections = []


@pytest.fixture(autouse=True)
def isolate_agent_context_between_tests():
    """Prevent one test's runtime agent from changing later tool boundaries."""

    from custom_tools.runtime_context import set_agent_ref

    set_agent_ref(None)
    try:
        yield
    finally:
        set_agent_ref(None)


@pytest.fixture(autouse=True)
def isolate_task_work_between_tests():
    """Start every test outside any task's handling (tests.task_work_support.bind_task)."""

    from custom_tools.runtime_context import _task_work

    token = _task_work.set(None)
    try:
        yield
    finally:
        _task_work.reset(token)


@pytest.fixture(autouse=True)
def isolate_task_context_keys_between_tests():
    """Keep cached identity keys aligned with each test's own environment."""
    from tests.broker_access_support import forget_task_context_keys

    forget_task_context_keys()
    try:
        yield
    finally:
        forget_task_context_keys()


@pytest.fixture(autouse=True)
def restore_process_environment_between_tests():
    """Undo process environment changes a test made outside ``monkeypatch``.

    Creating an agent writes process-wide settings such as
    ``TEAM_INSTANCE_ROOT`` into ``os.environ`` (``custom_agents.strands_team``;
    one instance per process in production). A test that creates real agents
    would otherwise make every later test resolve that instance.
    """

    saved = dict(os.environ)
    try:
        yield
    finally:
        if os.environ != saved:
            os.environ.clear()
            os.environ.update(saved)


@pytest.fixture
def commands_without_agent():
    """Let the shell and Python tools run commands with no agent behind them.

    The runtime refuses such commands (custom_agents.code_access); tests that
    call the tools directly opt in explicitly through this fixture.
    """

    from custom_agents.code_access import embedded_commands

    with embedded_commands():
        yield

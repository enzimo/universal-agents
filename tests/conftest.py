from __future__ import annotations

import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    # Imported where the fixture runs: it loads nats-py, which unmarked tests do not need.
    from tests.live_nats_support import OwnedStreams

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from live_provider_safety import sanitize_provider_failure  # noqa: E402
from tests.postgres_marker_guard import unmarked_postgres_cases  # noqa: E402
from tests.upgrade_schemas import (  # noqa: E402,F401 - pytest hooks: --upgrade-from, --capture-schema
    pytest_addoption,
    pytest_configure,
    pytest_runtest_setup,
    pytest_unconfigure,
)
import storage.sqlite_connection as _sqlite_connection  # noqa: E402

# The product commits with synchronous=FULL, so a commit survives a power loss
# (storage.sqlite_connection). No test cuts the power, and the extra disk flushes
# made the store tests about six times slower, so tests in this process commit with
# NORMAL. tests/test_sqlite_connection.py checks the product's setting in a fresh
# process; child processes the tests start keep the product's setting.
_sqlite_connection.SQLITE_SYNCHRONOUS = "NORMAL"


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
def isolate_rtk_tracking_between_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real RTK commands independent of usage-history disk writes.

    RTK records statistics after a command exits. SQLite commits on a busy
    build disk can exceed shell cancellation and execution deadlines even for
    ``printf``. Tests need the real commands and sandbox, but no usage history.
    Each RTK process gets its own in-memory database instead.
    """

    monkeypatch.setenv("RTK_DB_PATH", ":memory:")


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


@pytest.fixture(autouse=True)
def delete_streams_a_live_nats_test_created(
    request: pytest.FixtureRequest,
) -> Generator[OwnedStreams | None, None, None]:
    """Remove the streams a test marked ``nats`` created on the shared server, passed or failed.

    Yields the recorder (``None`` for other tests). See
    ``tests.live_nats_support.deleting_created_streams``.
    """

    if request.node.get_closest_marker("nats") is None:
        yield None
        return
    from tests.live_nats_support import deleting_created_streams

    with deleting_created_streams() as ownership:
        yield ownership


@pytest.fixture
def commands_without_agent():
    """Let the shell and Python tools run commands with no agent behind them.

    The runtime refuses such commands (custom_agents.code_access); tests that
    call the tools directly opt in explicitly through this fixture.
    """

    from custom_agents.code_access import embedded_commands

    with embedded_commands():
        yield

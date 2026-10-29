"""Requests keep default code apart; explicit names allow deliberate reuse."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import python_env_lifecycle as lifecycle
from access_control.identity import ContextSigner, ISSUER_SCHEDULER
from custom_agents import access_runtime
from custom_tools import python_env_tool
from custom_tools.tool_stop import ToolStop
from tests.broker_access_support import use_scheduler_key
from tests.task_work_support import set_agent_ref, use_agent
from tests.test_workspace_scope_policy import layout as _scope_layout, task_agent
from tests.tool_stop_support import tool_context

layout = _scope_layout


def _fake_create(env_name, env_dir, **kwargs):
    # File isolation does not depend on pip or the host's venv implementation.
    if kwargs.get("recreate"):
        python_env_tool.file_access.remove_tree(env_dir)
    python_env_tool._ensure_env_dirs(env_dir)
    python_env_tool.file_access.make_directories(env_dir / ".venv")
    return {"manifest": {}, "command_result": None}


def test_two_requests_with_default_settings_keep_their_code_apart(layout, monkeypatch):
    monkeypatch.setattr(python_env_tool, "_create_env", _fake_create)
    first, second = task_agent(layout, "alice"), task_agent(layout, "alice")
    task_infos = {
        "request-a": first.task_manager.get_task_info("task-1"),
        "request-b": second.task_manager.get_task_info("task-1"),
    }
    first.task_manager.get_task_info = task_infos.get
    first.task_manager.current_task_id = "request-a"  # the main lane stays on A
    paths = []
    for task_id, text in (("request-a", "print('request A')\n"), ("request-b", "print('request B')\n")):
        # Parallel lanes share the agent object and bind their own task work.
        use_agent(first, task_id)
        result = python_env_tool.python_env._tool_func(
            tool_context=tool_context(), action="write_file", content=text,
        )
        assert result["status"] == "success", result
        paths.append(python_env_tool._env_dir(None))
    assert (paths[0] / "src/main.py").read_text() == "print('request A')\n"
    assert paths[0] != paths[1]


def test_separate_defaults_run_their_own_code_in_real_sandboxes(layout):
    requests = [task_agent(layout, "alice"), task_agent(layout, "alice")]
    for index, agent in enumerate(requests):
        set_agent_ref(agent)
        written = python_env_tool.python_env._tool_func(
            tool_context=tool_context(), action="write_file", content=f"print('request-{index}')\n",
        )
        assert written["status"] == "success", written
    for index, agent in enumerate(requests):
        set_agent_ref(agent)
        result = python_env_tool.python_env._tool_func(tool_context=tool_context(), action="run")
        assert result["status"] == "success", result
        payload = json.loads(result["content"][1]["text"])
        assert payload["command_result"]["sandbox"] == "landlock"
        assert payload["command_result"]["output"].strip() == f"request-{index}"


def _write(agent, *, env_name=None):
    set_agent_ref(agent)
    result = python_env_tool.python_env._tool_func(
        tool_context=tool_context(), action="write_file", content="value = 1\n", env_name=env_name,
    )
    assert result["status"] == "success", result
    return python_env_tool._env_dir(env_name)


def _age(path, *, days=8):
    manifest = json.loads((path / lifecycle.MANIFEST_FILENAME).read_text())
    manifest[lifecycle.LAST_USED_FIELD] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    (path / lifecycle.MANIFEST_FILENAME).write_text(json.dumps(manifest))


@pytest.mark.parametrize("scheduled,person", [(False, "alice"), (True, "alice"), (True, None)])
def test_helpers_share_one_default_but_separate_runs_do_not(layout, monkeypatch, scheduled, person):
    monkeypatch.setattr(python_env_tool, "_create_env", _fake_create)
    first, second = task_agent(layout, person), task_agent(layout, person)
    if scheduled:
        use_scheduler_key(monkeypatch)
        access_runtime._runtime_access.cache_clear()
        signer = ContextSigner.from_env(ISSUER_SCHEDULER)
        for agent in (first, second):
            agent.task_manager.get_task_info("task-1").meta.gateway_context = SimpleNamespace(task_context=signer.issue(
                team_id=agent.team_id, person_id=person, schedule_id="same-schedule",
            ))
    first_path = _write(first)
    worker = SimpleNamespace(**vars(first))
    worker.id = "helper"
    worker.test_task_id = "delegated-task"
    assert _write(worker) == first_path
    assert _write(second) != first_path


def test_cleanup_removes_whole_defaults_and_preserves_named_and_recent_envs(layout, monkeypatch):
    monkeypatch.setattr(python_env_tool, "_create_env", _fake_create)
    old = _write(task_agent(layout, "alice"))
    named = _write(task_agent(layout, "alice"), env_name="report-tools")
    recent = _write(task_agent(layout, "alice"))
    for path in (old, named):
        _age(path)
    # Recent artifacts inside an unused environment go with the whole env.
    (old / "artifacts/result.txt").write_text("temporary result")
    legacy = layout.person_dir("alice") / "python_envs/default"
    legacy.mkdir()
    (legacy / "keep.txt").write_text("existing named environment")

    assert lifecycle.prune_request_environments(layout) == 1
    assert not old.exists()
    assert (named / "src/main.py").exists()
    assert (recent / "src/main.py").exists()
    assert (legacy / "keep.txt").exists()


@pytest.mark.parametrize("next_action", ["write_file", "recreate", "failed_install"])
def test_explicitly_reusing_a_default_keeps_it_until_deleted(layout, monkeypatch, next_action):
    monkeypatch.setattr(python_env_tool, "_create_env", _fake_create)
    original_request = task_agent(layout, "alice")
    path = _write(original_request)
    assert _write(task_agent(layout, "alice"), env_name=path.name) == path
    # The original request or a helper can still use its implicit default.
    # Explicit retention must survive both manifest replacement and recreation.
    set_agent_ref(original_request)
    if next_action == "write_file":
        assert _write(original_request) == path
    else:
        result = python_env_tool.python_env._tool_func(
            tool_context=tool_context(),
            action="create" if next_action == "recreate" else "install",
            recreate=next_action == "recreate",
        )
        assert result["status"] == ("success" if next_action == "recreate" else "error")
    _age(path)
    assert lifecycle.prune_request_environments(layout) == 0
    assert path.exists()


@pytest.mark.parametrize("recreate", [False, True])
def test_retention_survives_a_failed_final_activity_write_and_retry(layout, monkeypatch, recreate):
    original_request = task_agent(layout, "alice")
    with monkeypatch.context() as setup:
        setup.setattr(python_env_tool, "_create_env", _fake_create)
        path = _write(original_request)
        _write(task_agent(layout, "alice"), env_name=path.name)
    set_agent_ref(original_request)
    original_action = python_env_tool._python_env_in_directory

    def refuse_activity_write(*args, **kwargs):
        raise OSError("activity write unavailable")

    def complete_action_then_fail_activity(*args, **kwargs):
        result = original_action(*args, **kwargs)
        failing.setattr(python_env_tool, "_write_manifest", refuse_activity_write)
        return result

    def create_without_pip(*args, **kwargs):
        python_env_tool.file_access.make_directories(path / ".venv")
        return {"status": "success"}

    with monkeypatch.context() as failing:
        # Install the failure only after the action has saved its own manifest.
        # This leaves the real finally block to encounter a metadata-save error.
        failing.setattr(python_env_tool, "_python_env_in_directory", complete_action_then_fail_activity)
        failing.setattr(python_env_tool, "execute_non_interactive_command", create_without_pip)
        result = python_env_tool.python_env._tool_func(
            tool_context=tool_context(), action="create" if recreate else "write_file",
            recreate=recreate, content="value = 2\n",
        )
    assert result["status"] == "error"
    assert "activity write unavailable" in result["content"][0]["text"]
    assert _write(original_request) == path
    _age(path)
    assert lifecycle.prune_request_environments(layout) == 0
    assert path.exists()


def test_cleanup_skips_active_code_and_rechecks_activity_on_the_next_pass(layout, monkeypatch):
    monkeypatch.setattr(python_env_tool, "_create_env", _fake_create)
    agent = task_agent(layout, "alice")
    path = _write(agent)
    _age(path)
    running, finish, pruning = threading.Event(), threading.Event(), threading.Event()
    original_lock = lifecycle.environment_lock

    def blocked_run(*args, **kwargs):
        running.set()
        assert finish.wait(5)
        return {"command_result": {"status": "success"}}

    def cleanup_lock(*args, **kwargs):
        pruning.set()
        return original_lock(*args, **kwargs)

    monkeypatch.setattr(python_env_tool, "_run", blocked_run)
    monkeypatch.setattr(lifecycle, "environment_lock", cleanup_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        context = copy_context()
        run = pool.submit(context.run, lambda: python_env_tool.python_env._tool_func(
            tool_context=tool_context(), action="run",
        ))
        try:
            assert running.wait(5)
            cleanup = pool.submit(lifecycle.prune_request_environments, layout)
            assert pruning.wait(5)
            # Maintenance must return while the command is still running.
            assert cleanup.result(timeout=2) == 0
            assert path.exists()
        finally:
            finish.set()
        assert run.result()["status"] == "success"
    assert lifecycle.prune_request_environments(layout) == 0
    assert (path / "src/main.py").exists()
    _age(path)
    assert lifecycle.prune_request_environments(layout) == 1


def test_cleanup_skips_a_busy_named_environment_and_still_prunes_other_defaults(layout, monkeypatch):
    monkeypatch.setattr(python_env_tool, "_create_env", _fake_create)
    named = _write(task_agent(layout, "alice"))
    _write(task_agent(layout, "alice"), env_name=named.name)
    expired = _write(task_agent(layout, "alice"))
    _age(expired)
    # A real cross-process contender must finish before this holder releases.
    with lifecycle.environment_lock(layout, named):
        removed = subprocess.run(
            [sys.executable, "-c", _PRUNE_PROBE, str(layout.root)],
            capture_output=True, text=True, timeout=5, check=True,
        )
    assert removed.stdout.strip() == "1"
    assert named.exists()
    assert not expired.exists()


_PRUNE_PROBE = """
import sys
from python_env_lifecycle import prune_request_environments
from runtime_settings.instance_layout import InstanceLayout
print(prune_request_environments(InstanceLayout.at(sys.argv[1])))
"""


def test_cleanup_refuses_a_pipe_manifest_and_continues(layout, monkeypatch):
    monkeypatch.setattr(python_env_tool, "_create_env", _fake_create)
    malformed = _write(task_agent(layout, "alice"))
    manifest = malformed / lifecycle.MANIFEST_FILENAME
    manifest.unlink()
    os.mkfifo(manifest)
    expired = _write(task_agent(layout, "alice"))
    _age(expired)

    # A subprocess deadline makes a regression fail without stranding a thread.
    removed = subprocess.run(
        [sys.executable, "-c", _PRUNE_PROBE, str(layout.root)],
        capture_output=True, text=True, timeout=5, check=True,
    )

    assert removed.stdout.strip() == "1"
    assert "python_env_prune_skipped" in removed.stderr
    assert "Not a regular file" in removed.stderr
    assert malformed.exists()
    assert not expired.exists()


def test_cleanup_never_follows_links_and_reports_bad_activity(layout, monkeypatch, caplog):
    monkeypatch.setattr(python_env_tool, "_create_env", _fake_create)
    path = _write(task_agent(layout, "alice"))
    _age(path)
    protected = layout.control_dir / "keep.txt"
    protected.write_text("keep")
    outside_link = path.parent / "request-linked-folder"
    outside_link.symlink_to(layout.control_dir, target_is_directory=True)
    (path / "linked-control").symlink_to(layout.control_dir, target_is_directory=True)
    assert lifecycle.prune_request_environments(layout) == 1
    assert protected.read_text() == "keep"
    assert outside_link.is_symlink()
    path = _write(task_agent(layout, "alice"))
    (path / lifecycle.MANIFEST_FILENAME).write_text(json.dumps({"request_default": True}))
    with caplog.at_level(logging.WARNING):
        assert lifecycle.prune_request_environments(layout) == 0
    assert path.exists()
    assert "python_env_prune_skipped" in caplog.text


def test_missing_verified_identity_requires_an_explicit_name(layout, monkeypatch):
    monkeypatch.setattr(python_env_tool, "_create_env", _fake_create)
    set_agent_ref(task_agent(layout, "alice", signed=False))
    result = python_env_tool.python_env._tool_func(tool_context=tool_context(), action="create")
    assert result["status"] == "error"
    assert "verified request" in result["content"][0]["text"]


def test_a_stopped_tool_does_not_wait_for_another_environment_user(layout, monkeypatch):
    monkeypatch.setattr(python_env_tool, "_create_env", _fake_create)
    path = _write(task_agent(layout, "alice"))
    stop = ToolStop(agent_id="helper", task_id="waiting-task")
    attempted = threading.Event()
    original_lock = python_env_tool.environment_lock

    def waiting_lock(*args, **kwargs):
        attempted.set()
        return original_lock(*args, **kwargs)

    monkeypatch.setattr(python_env_tool, "environment_lock", waiting_lock)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with lifecycle.environment_lock(layout, path):
            context = copy_context()
            pending = pool.submit(context.run, lambda: python_env_tool.python_env._tool_func(
                tool_context=tool_context(stop), action="write_file", content="value = 2\n",
            ))
            assert attempted.wait(5)
            stop.request("person stopped the task")
            result = pending.result(timeout=2)
            assert result["status"] == "error"
            assert "stopped" in result["content"][0]["text"]
    finally:
        pool.shutdown(wait=True)
    assert (path / "src/main.py").read_text() == "value = 1\n"

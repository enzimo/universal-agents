"""Agent-run code sandbox: real Landlock enforcement plus fail-closed behavior.

The Landlock tests execute real commands through the production shell path.
They need a Linux kernel with Landlock enabled; the fail-closed, policy, and
packing tests run everywhere.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import custom_tools.code_sandbox as code_sandbox
from tests.task_work_support import set_agent_ref
from custom_tools.code_sandbox import (
    CodeSandboxUnavailable,
    landlock_abi,
    prepare_sandboxed_command,
    sandbox_policy,
)
from custom_tools.code_sandbox_exec import SANDBOX_FAILURE_EXIT_CODE, ruleset_attr
from custom_tools.non_interactive_shell import _execute_single_command
from runtime_settings.code_sandbox_contract import (
    CODE_SANDBOX_OFF,
    ENV_CODE_SANDBOX_EXTRA_READ_PATHS,
    ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS,
    ENV_CODE_SANDBOX_MODE,
)
from runtime_settings.instance_layout import InstanceLayout
from tests.change_approval_support import use_signed_contexts
from tests.test_workspace_scope_policy import task_agent

requires_landlock = pytest.mark.skipif(
    landlock_abi() < 1, reason="Linux Landlock is not available on this kernel"
)


@pytest.fixture
def instance(tmp_path: Path, monkeypatch, commands_without_agent) -> dict[str, Path]:
    """An instance whose ``workspaces/`` folders are the only area code may use.

    ``workspace`` is the default folder of work that has no person
    (``workspaces/agents``).
    """

    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    root = layout.root
    (root / "team.env").write_text("SECURE_TOOL_BROKER_TOKEN=runtime-secret\n", encoding="utf-8")
    layout.task_database_path.write_text("permission rows\n", encoding="utf-8")
    layout.soul_path.write_text("team lead soul\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(root))
    # Tasks name their person only through a gateway-signed context.
    use_signed_contexts(monkeypatch)
    monkeypatch.delenv(ENV_CODE_SANDBOX_MODE, raising=False)
    monkeypatch.delenv(ENV_CODE_SANDBOX_EXTRA_READ_PATHS, raising=False)
    monkeypatch.delenv(ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS, raising=False)
    monkeypatch.setattr(code_sandbox, "_status_logged", False)
    yield {"root": root, "workspace": layout.agents_dir, "layout": layout}
    set_agent_ref(None)


def _run(command: str, workspace: Path) -> dict:
    return _execute_single_command(command, default_work_dir=str(workspace), default_timeout=60)


def _dynamic(path: Path) -> str:
    """Build the path at run time so command-text path checks cannot see it."""

    text = str(path)
    middle = len(text) // 2
    return f'"{text[:middle]}"+"{text[middle:]}"'


@requires_landlock
def test_sandboxed_command_uses_workspace_and_private_home(instance) -> None:
    result = _run("echo hello > note.txt && cat note.txt && echo HOME=$HOME", instance["workspace"])

    assert result["status"] == "success", result
    assert result["sandbox"] == "landlock"
    assert "hello" in result["output"]
    home = result["output"].split("HOME=")[1].strip()
    assert home != str(instance["workspace"])
    # The scratch home is deleted once the command finishes.
    assert not Path(home).exists()
    assert (instance["workspace"] / "note.txt").read_text() == "hello\n"


@requires_landlock
@pytest.mark.parametrize("target", ["team.env", "dbs/task_history.db", "control/SOUL.md"])
def test_sandboxed_code_cannot_read_instance_secrets_or_state(instance, target: str) -> None:
    secret = instance["root"] / target
    result = _run(f"python3 -c 'print(open({_dynamic(secret)}).read())'", instance["workspace"])

    assert result["exit_code"] != 0
    assert "Permission denied" in result["error"]
    assert "runtime-secret" not in result["output"]
    assert "permission rows" not in result["output"]
    assert "team lead soul" not in result["output"]
    assert "sandbox" in result.get("sandbox_note", "")


@requires_landlock
def test_code_for_one_person_cannot_reach_another_persons_folder(instance) -> None:
    layout: InstanceLayout = instance["layout"]
    bob = layout.person_dir("bob")
    bob.mkdir(parents=True)
    (bob / "diary.txt").write_text("bob private\n", encoding="utf-8")
    set_agent_ref(task_agent(layout, "alice"))
    alice = layout.person_dir("alice")
    alice.mkdir(parents=True)

    wrote = _run(
        "echo mine > mine.txt && echo shared > "
        f"{layout.team_dir}/shared.txt && echo work > {layout.agents_dir}/work.txt && pwd",
        alice,
    )
    read_bob = _run(f"python3 -c 'print(open({_dynamic(bob / 'diary.txt')}).read())'", alice)
    write_bob = _run(f"python3 -c 'open({_dynamic(bob / 'x.txt')}, \"w\").write(\"x\")'", alice)

    assert wrote["status"] == "success", wrote
    assert (alice / "mine.txt").read_text() == "mine\n"
    assert (layout.team_dir / "shared.txt").read_text() == "shared\n"
    assert (layout.agents_dir / "work.txt").read_text() == "work\n"
    assert read_bob["exit_code"] != 0 and "bob private" not in read_bob["output"]
    assert write_bob["exit_code"] != 0 and not (bob / "x.txt").exists()


@requires_landlock
def test_skill_scripts_run_but_skills_cannot_be_changed(instance) -> None:
    layout: InstanceLayout = instance["layout"]
    script = layout.skills_dir / "probe" / "scripts" / "hello.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('skill script ran')\n", encoding="utf-8")
    set_agent_ref(task_agent(layout, "alice"))
    alice = layout.person_dir("alice")
    alice.mkdir(parents=True)

    ran = _run('python3 "${SKILLS_DIR}/probe/scripts/hello.py"', alice)
    changed = _run(f"python3 -c 'open({_dynamic(script)}, \"a\").write(\"x\")'", alice)

    assert ran["status"] == "success", ran
    assert "skill script ran" in ran["output"]
    assert changed["exit_code"] != 0
    assert script.read_text() == "print('skill script ran')\n"


@requires_landlock
def test_sandboxed_code_cannot_write_outside_workspace(instance) -> None:
    outside = instance["root"] / "planted.txt"
    result = _run(f"python3 -c 'open({_dynamic(outside)}, \"w\").write(\"x\")'", instance["workspace"])

    assert result["exit_code"] != 0
    assert not outside.exists()


@requires_landlock
def test_sandboxed_code_cannot_read_the_runtime_environment(instance, monkeypatch) -> None:
    monkeypatch.setenv("UA_TEST_RUNTIME_SECRET", "runtime-process-secret")
    runtime_environ = Path(f"/proc/{os.getpid()}/environ")
    result = _run(f"python3 -c 'print(open({_dynamic(runtime_environ)}).read())'", instance["workspace"])

    assert result["exit_code"] != 0
    assert "runtime-process-secret" not in result["output"]
    assert "Permission denied" in result["error"]


@requires_landlock
def test_operator_extra_paths_are_granted(instance, tmp_path: Path, monkeypatch) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "data.txt").write_text("reference data\n", encoding="utf-8")
    output = tmp_path / "exports"
    output.mkdir()
    monkeypatch.setenv(ENV_CODE_SANDBOX_EXTRA_READ_PATHS, json.dumps([str(reference)]))
    monkeypatch.setenv(ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS, json.dumps([str(output)]))

    result = _run(
        f"python3 -c 'data = open({_dynamic(reference / 'data.txt')}).read(); "
        f"open({_dynamic(output / 'copy.txt')}, \"w\").write(data)'",
        instance["workspace"],
    )

    assert result["status"] == "success", result
    assert (output / "copy.txt").read_text() == "reference data\n"


@requires_landlock
def test_python_env_runs_inside_the_sandbox(instance) -> None:
    from custom_tools.python_env_tool import python_env
    from tests.tool_stop_support import tool_context

    created = python_env._tool_func(tool_context=tool_context(), action="create", env_name="probe")
    assert created["status"] == "success", created
    secret = instance["root"] / "team.env"
    written = python_env._tool_func(
        tool_context=tool_context(),
        action="write_file",
        env_name="probe",
        path="src/main.py",
        content=f"print(open({_dynamic(secret)}).read())\n",
    )
    assert written["status"] == "success", written
    ran = python_env._tool_func(tool_context=tool_context(), action="run", env_name="probe", path="src/main.py")
    payload = json.loads(ran["content"][-1]["text"])

    assert payload["command_result"]["status"] == "error"
    assert payload["command_result"]["sandbox"] == "landlock"
    assert "Permission denied" in payload["command_result"]["error"]
    assert "runtime-secret" not in payload["command_result"]["output"]


def test_required_sandbox_refuses_commands_when_unavailable(instance, monkeypatch, caplog) -> None:
    monkeypatch.setattr(code_sandbox, "landlock_abi", lambda: 0)
    marker = instance["workspace"] / "ran.txt"

    with caplog.at_level(logging.ERROR, logger="custom_tools.code_sandbox"):
        result = _run("touch ran.txt", instance["workspace"])

    assert result["exit_code"] == 126
    assert result["reason_code"] == "code_sandbox_unavailable"
    assert "security_layer.code_execution.sandbox" in result["error"]
    assert not marker.exists()
    assert "event=code_sandbox_unavailable" in caplog.text


def test_off_mode_runs_unconfined_and_warns(instance, monkeypatch, caplog) -> None:
    monkeypatch.setenv(ENV_CODE_SANDBOX_MODE, CODE_SANDBOX_OFF)
    secret = instance["root"] / "team.env"

    with caplog.at_level(logging.WARNING, logger="custom_tools.code_sandbox"):
        result = _run(f"python3 -c 'print(open({_dynamic(secret)}).read())'", instance["workspace"])

    assert result["sandbox"] == CODE_SANDBOX_OFF
    assert "runtime-secret" in result["output"]
    assert "event=code_sandbox_disabled" in caplog.text


def test_unknown_mode_fails_closed_to_required(monkeypatch) -> None:
    monkeypatch.setenv(ENV_CODE_SANDBOX_MODE, "shadow")

    assert code_sandbox.configured_mode() == "required"


def test_policy_never_grants_parents_of_protected_roots(tmp_path: Path) -> None:
    root = tmp_path / "instance"
    workspace = root / "workspaces" / "agents"
    workspace.mkdir(parents=True)
    env = {
        "TEAM_INSTANCE_ROOT": str(root),
        "PATH": os.pathsep.join([str(tmp_path), str(root), "/usr/bin"]),
        ENV_CODE_SANDBOX_EXTRA_READ_PATHS: json.dumps([str(root)]),
        ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS: json.dumps([str(tmp_path)]),
    }

    policy = sandbox_policy(task_roots=[str(workspace)], scratch_dir=str(tmp_path / "scratch"), env=env)

    granted = policy["read"] + policy["write"]
    assert str(root) not in granted
    assert str(tmp_path) not in granted
    assert "/usr/bin" in policy["read"]
    assert policy["write"][0] == str(workspace.resolve())


def test_operator_paths_inside_the_instance_are_never_granted(tmp_path: Path) -> None:
    layout = InstanceLayout.at(tmp_path / "instance")
    env = {
        "TEAM_INSTANCE_ROOT": str(layout.root),
        ENV_CODE_SANDBOX_EXTRA_READ_PATHS: json.dumps([str(layout.control_dir)]),
        ENV_CODE_SANDBOX_EXTRA_WRITE_PATHS: json.dumps([str(layout.state_dir)]),
    }

    policy = sandbox_policy(
        task_roots=[str(layout.agents_dir)],
        scratch_dir=str(tmp_path / "scratch"),
        env=env,
        read_only_roots=[str(path) for path in layout.agent_readable_skill_dirs],
    )

    assert str(layout.control_dir) not in policy["read"]
    assert str(layout.state_dir) not in policy["write"]
    # Skill folders are readable and never writable.
    for skill_dir in layout.agent_readable_skill_dirs:
        assert str(skill_dir) in policy["read"]
        assert str(skill_dir) not in policy["write"]


def test_relative_extra_paths_are_rejected(instance, monkeypatch) -> None:
    monkeypatch.setenv(ENV_CODE_SANDBOX_EXTRA_READ_PATHS, json.dumps(["relative/dir"]))
    monkeypatch.setattr(code_sandbox, "landlock_abi", lambda: 4)

    with pytest.raises(CodeSandboxUnavailable, match="absolute"):
        prepare_sandboxed_command(("true",), task_roots=[str(instance["workspace"])], env={}, network=True)


def test_launcher_fails_closed_on_invalid_policy(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    helper = Path(code_sandbox.__file__).with_name("code_sandbox_exec.py")

    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(helper), "{not json", "touch", str(marker)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == SANDBOX_FAILURE_EXIT_CODE
    assert "invalid policy" in completed.stderr
    assert not marker.exists()


def test_ruleset_attr_packs_network_rights_from_abi4_and_scopes_from_abi6() -> None:
    import struct

    assert len(ruleset_attr(3, 0x7FFF)) == 8
    assert struct.unpack("=QQ", ruleset_attr(4, 0x7FFF, 0b11)) == (0x7FFF, 0b11)
    abi6 = ruleset_attr(6, 0x7FFF)
    assert len(abi6) == 24
    handled_fs, handled_net, scoped = struct.unpack("=QQQ", abi6)
    assert (handled_fs, handled_net, scoped) == (0x7FFF, 0, 0b11)


@requires_landlock
def test_git_works_when_runtime_points_config_outside_the_sandbox(instance, tmp_path: Path, monkeypatch) -> None:
    outside_config = tmp_path / "outside-home" / ".config" / "git" / "config"
    outside_config.parent.mkdir(parents=True)
    outside_config.write_text("[user]\n\tname = Outside\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(outside_config))
    monkeypatch.setenv("UA_TEST_SECRET_FILE", "/run/secrets/example-token")

    result = _run("git init -q && git status --short && env", instance["workspace"])

    assert result["status"] == "success", result
    assert "GIT_CONFIG_GLOBAL" not in result["output"]
    assert "UA_TEST_SECRET_FILE" not in result["output"]


def test_path_variables_inside_granted_areas_are_kept(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = sandbox_policy(task_roots=[str(workspace)], scratch_dir=str(tmp_path / "scratch"), env={})
    kept, dropped = code_sandbox._without_ungranted_path_variables(
        {
            "PROJECT_FILE": str(workspace / "project.toml"),
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_GLOBAL": str(tmp_path / "elsewhere" / "gitconfig"),
            "GREETING": "hello",
        },
        policy,
    )

    assert dropped == ["GIT_CONFIG_GLOBAL"]
    assert set(kept) == {"PROJECT_FILE", "SSL_CERT_FILE", "PATH", "GREETING"}


def test_a_read_only_granted_folder_may_hold_a_writable_one(tmp_path: Path) -> None:
    """Read on data/reports with write on data/reports/2026 keeps both; the instance stays out."""

    reports = tmp_path / "reports"
    (reports / "2026").mkdir(parents=True)
    instance = tmp_path / "instance"
    instance.mkdir()

    policy = sandbox_policy(
        task_roots=[str(reports / "2026")],
        scratch_dir=str(tmp_path / "scratch"),
        env={"TEAM_INSTANCE_ROOT": str(instance)},
        granted_read_roots=[str(reports), str(tmp_path)],
    )

    assert str(reports) in policy["read"]
    assert str(reports / "2026") in policy["write"]
    assert str(tmp_path) not in policy["read"], "a folder holding the instance is never granted"

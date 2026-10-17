import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.data_folder_support import agent_with_data_folders, sqlite_storage

from custom_tools.non_interactive_shell import _build_env, _execute_single_command
from custom_tools.path_policy import (
    DATA_FOLDER_READ_ONLY,
    PROTECTED_RUNTIME_CONTROL_DENIED,
    WORKSPACE_PATH_DENIED,
    PathPolicyError,
    ensure_allowed_path,
)
from custom_tools.openshell_sandboxes import sandbox_name
from custom_tools.runtime_context import set_agent_ref
from custom_tools.shell_execution import ProcessResult
from runtime_settings.instance_layout import InstanceLayout

CONTROL_FILE_DENIAL = "Team control files cannot be opened through generic tools"
DATABASE_DENIAL = "The runtime databases cannot be opened through agent tools"
RUNTIME_AREA_DENIAL = "This runtime area is closed to agent tools"
SHELL_CONTROL_DENIAL = (
    "Team control files, databases, and runtime records cannot be accessed through shell"
)
OUTSIDE_TASK_FOLDERS = "Path is outside this task's folders"


@pytest.fixture
def layout(tmp_path, monkeypatch) -> InstanceLayout:
    """An instance whose ``workspaces/`` area is the agent's working area.

    With no agent reference the policy reads ``TEAM_INSTANCE_ROOT`` and
    ``AGENT_WORKSPACE_DIR``; the task works for no person, so its default
    folder is ``workspaces/agents``. The instance sits in ``tmp_path/instance``
    so ``tmp_path`` itself is outside the instance.
    """

    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    for name in (
        "TEAM_INSTANCE_CONFIG",
        "SECURITY_LAYER_MODE",
        "OPENSHELL_WORKSPACE_MOUNT",
        "OPENSHELL_SANDBOX_WORKSPACE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    set_agent_ref(None)
    yield layout
    set_agent_ref(None)


@pytest.fixture
def workspace(layout, commands_without_agent) -> Path:
    """The default folder of a task that works for no person; commands run without an agent."""

    return layout.agents_dir


def test_path_policy_blocks_outside_workspace(tmp_path, layout):
    with pytest.raises(PathPolicyError, match=OUTSIDE_TASK_FOLDERS) as raised:
        ensure_allowed_path(str(tmp_path / "outside.txt"))

    assert raised.value.reason_code == WORKSPACE_PATH_DENIED


def test_path_policy_blocks_sensitive_workspace_files(layout):
    with pytest.raises(PermissionError):
        ensure_allowed_path(".env")

    with pytest.raises(PermissionError):
        ensure_allowed_path("id_rsa.pem")


@pytest.mark.parametrize(
    "name",
    ["id_ed25519", "copy/team.env", ".envrc", ".ssh/config", "notes.env", "keys/.env.local"],
)
def test_path_policy_blocks_every_shared_credential_name(name, layout):
    # The names come from access_control.sensitive_paths, the list the
    # attachments and the workspace browser use too.
    with pytest.raises(PermissionError, match="Sensitive"):
        ensure_allowed_path(name)


@pytest.mark.parametrize(
    ("relative_to_root", "message"),
    [
        ("control/SOUL.md", CONTROL_FILE_DENIAL),
        ("control/LLM_OVERRIDES.yaml", CONTROL_FILE_DENIAL),
        ("control/TEAM_AGENTS.yaml", CONTROL_FILE_DENIAL),
        ("control/TEAM_TEMPLATES.yaml", CONTROL_FILE_DENIAL),
        (
            "control/agent_bundles/team_lead_generated/PlanningCriticAgent/agent.yaml",
            CONTROL_FILE_DENIAL,
        ),
        ("dbs/task_history.db", DATABASE_DENIAL),
        ("instance_config.yaml", RUNTIME_AREA_DENIAL),
        ("runtime/mcp-connections/records/mcp_conn_example.json", RUNTIME_AREA_DENIAL),
        ("runtime/mcp-hosting/state/mcp_host_example.json", RUNTIME_AREA_DENIAL),
    ],
)
def test_generic_file_write_blocks_runtime_control_paths(
    relative_to_root, message, layout
):
    # The absolute path and a relative path that climbs out of the task's
    # default folder (workspaces/agents) must both be refused.
    candidates = (
        str(layout.root / relative_to_root),
        str(Path("..", "..", relative_to_root)),
    )
    for candidate in candidates:
        for for_write in (True, False):
            with pytest.raises(PathPolicyError, match=message) as raised:
                ensure_allowed_path(candidate, for_write=for_write)
            assert raised.value.reason_code == PROTECTED_RUNTIME_CONTROL_DENIED


@pytest.mark.parametrize(
    "command",
    [
        "printf changed > {root}/control/LLM_OVERRIDES.yaml",
        "rm -f ../../control/TEAM_AGENTS.yaml",
        "sh -c 'printf changed > ../../control/TEAM_TEMPLATES.yaml'",
        'python -c \'open("{root}/instance_config.yaml", "w").write("x")\'',
        "rm -rf ../../control/agent_bundles/team_lead_generated",
        "rm -rf ../../runtime/mcp-connections",
        "printf changed > {root}/runtime/mcp-hosting/definitions/server.json",
        "sed -n 1p {root}/team.env",
        "cat ../../dbs/task_history.db",
    ],
)
def test_shell_blocks_runtime_control_references(command, layout, workspace):
    result = _execute_single_command(
        command.format(root=layout.root),
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 126
    assert SHELL_CONTROL_DENIAL in result["error"]
    assert result["reason_code"] == PROTECTED_RUNTIME_CONTROL_DENIED


def _agent_with_data_folders(layout: InstanceLayout, *folders: dict) -> SimpleNamespace:
    """An agent in single-person mode whose team registered data folders (in the access store)."""

    return agent_with_data_folders(sqlite_storage(layout.root.parent / "access.db"), layout, *folders)


def test_an_instance_config_outside_the_root_is_guarded(tmp_path, layout, monkeypatch):
    """The operator's instance config stays closed even when it sits outside the root.

    MCP folders are derived from the instance root (runtime/mcp-connections,
    runtime/mcp-hosting), so they are closed with the rest of the root; the
    configuration file is the one control location that may live elsewhere.
    """

    operator_dir = tmp_path / "operator"
    operator_dir.mkdir()
    instance_config = operator_dir / "instance_config.yaml"
    instance_config.write_text("mcp:\n  enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("TEAM_INSTANCE_CONFIG", str(instance_config))
    reports = operator_dir / "reports"
    reports.mkdir()
    set_agent_ref(_agent_with_data_folders(layout, {"name": "reports", "path": str(reports)}))

    for for_write in (True, False):
        with pytest.raises(PathPolicyError, match=RUNTIME_AREA_DENIAL) as raised:
            ensure_allowed_path(str(instance_config), for_write=for_write)
        # Refused as a control file, not merely as a path outside the task's folders.
        assert raised.value.reason_code == PROTECTED_RUNTIME_CONTROL_DENIED

    neighbour = reports / "notes.txt"
    assert ensure_allowed_path(str(neighbour)) == neighbour.resolve()


def test_paths_outside_every_registered_folder_are_refused(tmp_path, layout):
    broker_credentials = tmp_path / "broker_credentials"
    broker_credentials.mkdir()

    for outside in (broker_credentials / "gh" / "hosts.yml", tmp_path):
        with pytest.raises(PathPolicyError) as raised:
            ensure_allowed_path(str(outside))
        assert raised.value.reason_code == WORKSPACE_PATH_DENIED


def test_a_read_only_data_folder_is_read_but_never_written(tmp_path, layout):
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "prices.csv").write_text("a,b\n", encoding="utf-8")
    set_agent_ref(_agent_with_data_folders(layout, {"name": "reference", "path": str(reference), "writable": False}))

    assert ensure_allowed_path(str(reference / "prices.csv")) == (reference / "prices.csv").resolve()
    with pytest.raises(PathPolicyError) as raised:
        ensure_allowed_path(str(reference / "prices.csv"), for_write=True)
    assert raised.value.reason_code == DATA_FOLDER_READ_ONLY


def test_a_data_folder_that_reaches_the_instance_is_left_out(tmp_path, layout, caplog):
    set_agent_ref(_agent_with_data_folders(layout, {"name": "everything", "path": str(tmp_path)}))

    with caplog.at_level(logging.ERROR):
        with pytest.raises(PathPolicyError) as raised:
            ensure_allowed_path(str(tmp_path / "notes.txt"))

    assert raised.value.reason_code == WORKSPACE_PATH_DENIED
    assert "event=data_folder_refused" in caplog.text


def test_path_policy_blocks_broker_credentials_by_name(layout, workspace):
    broker_credentials = workspace / "broker_credentials"
    broker_credentials.mkdir(parents=True)

    with pytest.raises(PermissionError):
        ensure_allowed_path("broker_credentials/gh/hosts.yml")


@pytest.mark.parametrize(
    "command",
    [
        "gh auth status",
        "gog auth status",
        "bw status",
        "op item list",
        "keepassxc-cli ls database.kdbx",
        "vault kv get secret/app",
    ],
)
def test_shell_blocks_direct_credentialed_cli_entrypoints(command, workspace):
    result = _execute_single_command(
        command,
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 126
    assert "secure_cli" in result["error"]


def test_shell_blocks_direct_github_login_attempts(workspace):
    result = _execute_single_command(
        "gh auth login --web",
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 126
    assert "secure_cli" in result["error"]


def test_shell_blocks_gh_project_with_graphql_guidance(workspace):
    result = _execute_single_command(
        "gh project item-list 1 --owner example-owner --format json",
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 126
    assert "raw `gh project ...` is not available" in result["error"]
    assert "action='api.post'" in result["error"]
    assert "params.path='graphql'" in result["error"]


def test_shell_blocks_outside_workspace_path_arguments(tmp_path, workspace):
    result = _execute_single_command(
        f"sed -n 1p {tmp_path / 'team.env'}",
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 126
    assert OUTSIDE_TASK_FOLDERS in result["error"]
    assert result["reason_code"] == WORKSPACE_PATH_DENIED


def test_shell_blocks_nested_outside_workspace_path_arguments(tmp_path, workspace):
    result = _execute_single_command(
        # A non-sensitive name, so the outer scan of the whole ``sh -c`` string
        # does not stop at the sensitive-name check before the nested scan.
        f"sh -c 'cat {tmp_path / 'outside.txt'}'",
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 126
    assert OUTSIDE_TASK_FOLDERS in result["error"]
    assert result["reason_code"] == WORKSPACE_PATH_DENIED


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        # Bare credential names without a "/" are still judged as paths.
        "cat id_rsa",
        "cat team.env",
        "ls .ssh",
        "cp .envrc copy.txt",
    ],
)
def test_shell_blocks_sensitive_workspace_path_arguments(command, workspace):
    result = _execute_single_command(
        command,
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 126
    assert "Sensitive" in result["error"]


def test_shell_blocks_broker_credential_path_arguments_outside_the_task_folders(
    tmp_path, workspace
):
    broker_credentials = tmp_path / "broker_credentials"
    broker_credentials.mkdir()

    result = _execute_single_command(
        f"cat {broker_credentials / 'gh' / 'hosts.yml'}",
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 126
    assert result["reason_code"] == WORKSPACE_PATH_DENIED


def test_shell_blocks_nested_credentialed_cli_entrypoints(workspace):
    result = _execute_single_command(
        "sh -c 'gh auth status'",
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 126
    assert "secure_cli" in result["error"]


@pytest.mark.parametrize(
    "command",
    [
        "curl http://127.0.0.1:8766/v1/tools",
        "wget -qO- http://localhost:8766/health",
        "nc 127.0.0.1 4222",
        "curl http://0.0.0.0:8766/health",
        "python -c 'import urllib.request; urllib.request.urlopen(\"http://[::1]:8766/health\")'",
        "sh -c 'curl http://localhost:8766/health'",
    ],
)
def test_shell_blocks_explicit_loopback_service_targets(command, workspace):
    result = _execute_single_command(
        command,
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "error"
    assert result["exit_code"] == 126
    assert "loopback services is blocked" in result["error"]
    assert "secure_cli" in result["error"]


def test_shell_allows_non_network_text_mentions_of_loopback(workspace):
    result = _execute_single_command(
        "printf '%s\\n' 127.0.0.1",
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "success"
    assert result["output"] == "127.0.0.1\n"


def test_shell_strips_secret_like_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://secret")
    monkeypatch.setenv("NORMAL_SETTING", "visible")

    env = _build_env()

    assert "GITHUB_TOKEN" not in env
    assert "POSTGRES_DSN" not in env
    assert env["NORMAL_SETTING"] == "visible"


def test_openshell_mode_routes_shell_through_sandbox_exec(workspace, monkeypatch):
    workdir = workspace / "subdir"
    workdir.mkdir(parents=True)
    captured = {}

    def fake_which(name, path=None):
        assert name == "openshell"
        return "/usr/local/bin/openshell"

    def fake_run(request, **kwargs):
        captured["command"] = list(request.argv)
        captured["request"] = request
        captured["kwargs"] = dict(kwargs)
        return ProcessResult(
            exit_code=0,
            stdout="ok\n",
            stderr="",
            termination_reason="exited",
            duration_seconds=0.01,
        )

    monkeypatch.setenv("SECURITY_LAYER_MODE", "openshell")
    monkeypatch.setenv("OPENSHELL_SANDBOX_NAME", "team-sandbox")
    monkeypatch.setattr("custom_tools.non_interactive_shell.shutil.which", fake_which)
    monkeypatch.setattr("custom_tools.non_interactive_shell.run_process", fake_run)
    monkeypatch.setattr(
        "custom_tools.non_interactive_shell.ensure_sandbox",
        lambda _openshell, scope, _env: sandbox_name("team-sandbox", scope.person_id),
    )

    result = _execute_single_command(
        {"command": "ls -la", "work_dir": "subdir"},
        default_work_dir=str(workspace),
        default_timeout=5,
    )

    assert result["status"] == "success"
    assert captured["command"][:3] == ["/usr/local/bin/openshell", "sandbox", "exec"]
    assert "--no-tty" in captured["command"]
    # Each person's sandbox holds its folders at the same places below /workspace;
    # work for no person runs in the shared sandbox.
    assert (
        captured["command"][captured["command"].index("--workdir") + 1]
        == "/workspace/agents/subdir"
    )
    assert captured["command"][captured["command"].index("-n") + 1] == "team-sandbox-shared"
    assert captured["command"][-4] == "--"
    assert captured["command"][-3] == "sh"
    assert captured["command"][-2] == "-lc"
    assert "rtk rewrite" in captured["command"][-1]
    assert "rtk proxy" in captured["command"][-1]
    assert captured["request"].timeout_seconds == 20

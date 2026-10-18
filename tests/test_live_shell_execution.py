"""Opt-in live validation for the project-owned shell execution boundary."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from dotenv import load_dotenv

from assistant_gateway.runtime_config import load_runtime_config_file
from custom_tools.non_interactive_shell import _execute_single_command
from runtime_settings.instance_layout import InstanceLayout


PROJECT_ROOT = Path(__file__).resolve().parent.parent


pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
]


def _load_live_configuration() -> None:
    config_path = (
        Path(os.getenv("UA_LIVE_CONFIG", str(PROJECT_ROOT / "config.yaml")))
        .expanduser()
        .resolve()
    )
    env_path = (
        Path(os.getenv("UA_LIVE_ENV_FILE", str(PROJECT_ROOT / "debug.env")))
        .expanduser()
        .resolve()
    )
    if not config_path.is_file():
        raise AssertionError(f"Live config does not exist: {config_path}")
    if not env_path.is_file():
        raise AssertionError(f"Live env does not exist: {env_path}")
    load_dotenv(env_path, override=True)
    load_runtime_config_file(config_path)


def test_live_host_shell_execution_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, commands_without_agent
) -> None:
    """Exercise real RTK, stream separation, stdin closure, and timeout cleanup."""

    _load_live_configuration()
    if shutil.which("rtk") is None:
        raise AssertionError("RTK is required for the live shell execution test")
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("SECURITY_LAYER_MODE", "broker")
    work_dir = str(layout.agents_dir)

    init_result = _execute_single_command(
        "git init -q",
        default_work_dir=work_dir,
        default_timeout=10,
    )
    assert init_result["status"] == "success", init_result
    assert init_result["rtk_mode"] in {"rewrite", "proxy"}, init_result

    status_result = _execute_single_command(
        "git status --short",
        default_work_dir=work_dir,
        default_timeout=10,
    )
    assert status_result["status"] == "success", status_result
    assert status_result["rtk_mode"] == "rewrite", status_result

    stream_result = _execute_single_command(
        "printf ua-live-stdout; printf ua-live-stderr >&2",
        default_work_dir=work_dir,
        default_timeout=10,
    )
    assert stream_result["status"] == "success", stream_result
    assert stream_result["output"] == "ua-live-stdout"
    assert stream_result["error"] == "ua-live-stderr"
    assert stream_result["rtk_mode"] == "proxy"

    stdin_result = _execute_single_command(
        "if read value; then exit 9; else printf stdin-eof; fi",
        default_work_dir=work_dir,
        default_timeout=10,
    )
    assert stdin_result["status"] == "success", stdin_result
    assert stdin_result["output"] == "stdin-eof"

    timeout_result = _execute_single_command(
        "sleep 60",
        default_work_dir=work_dir,
        default_timeout=1,
    )
    assert timeout_result["exit_code"] == 124, timeout_result
    assert timeout_result["termination_reason"] == "timeout"


@pytest.mark.skipif(
    os.getenv("UA_RUN_OPENSHELL_LIVE", "").strip().lower() not in {"1", "true", "yes"},
    reason="set UA_RUN_OPENSHELL_LIVE=1 for a provisioned OpenShell sandbox",
)
def test_live_openshell_shell_execution_boundary(
    monkeypatch: pytest.MonkeyPatch, commands_without_agent
) -> None:
    """Exercise RTK rewriting and separated streams through a real OpenShell sandbox."""

    _load_live_configuration()
    if shutil.which("openshell") is None:
        raise AssertionError("openshell CLI is required when UA_RUN_OPENSHELL_LIVE=1")
    if not os.getenv("OPENSHELL_SANDBOX_NAME"):
        raise AssertionError(
            "OPENSHELL_SANDBOX_NAME is required when UA_RUN_OPENSHELL_LIVE=1"
        )
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(PROJECT_ROOT))
    monkeypatch.setenv("SECURITY_LAYER_MODE", "openshell")
    sandbox_workspace = os.getenv("UA_OPENSHELL_LIVE_WORKSPACE", "/workspace")
    monkeypatch.setenv("OPENSHELL_WORKSPACE_MOUNT", sandbox_workspace)

    result = _execute_single_command(
        "git status --short; printf ua-openshell-stdout; printf ua-openshell-stderr >&2",
        default_work_dir=str(PROJECT_ROOT),
        default_timeout=30,
    )

    assert result["status"] == "success", result
    assert result["rtk_mode"] == "rewrite", result
    assert "ua-openshell-stdout" in result["output"]
    assert "ua-openshell-stderr" in result["error"]

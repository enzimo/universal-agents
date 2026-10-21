from __future__ import annotations

import os
from pathlib import Path
import subprocess


INSTALL_SCRIPT = Path.cwd() / "install.sh"


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "docker-calls.txt"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        "docker_command=\"${1:-}\"\n"
        "{ printf '%s' \"$docker_command\"; shift; for arg in \"$@\"; do printf '\\t%s' \"$arg\"; done; printf '\\n'; } >> \"$DOCKER_CAPTURE\"\n"
        "case \"$docker_command\" in\n"
        "  info|pull|run|start|rm) exit 0 ;;\n"
        "  container)\n"
        "    [ \"${1:-}\" = inspect ] || exit 0\n"
        "    [ \"${FAKE_CONTAINER_EXISTS:-false}\" = true ] || exit 1\n"
        "    case \"$*\" in\n"
        "      *io.universal-agents.installer.managed*) printf '%s\\n' \"${FAKE_MANAGED_LABEL:-true}\" ;;\n"
        "      *.State.Running*) printf '%s\\n' \"${FAKE_CONTAINER_RUNNING:-true}\" ;;\n"
        "    esac\n"
        "    exit 0\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir, capture


def _run_installer(tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    bin_dir, capture = _fake_docker(tmp_path)
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DOCKER_CAPTURE": str(capture),
        **(extra_env or {}),
    }
    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), *args],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    result.docker_capture = capture  # type: ignore[attr-defined]
    return result


def test_install_help_recommends_bundle_and_labels_native_experimental() -> None:
    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "strongly recommended" in result.stdout
    assert "--native" in result.stdout
    assert "Experimental host-native" in result.stdout
    assert "ghcr.io/enzimo/universal-agents:latest" in result.stdout


def test_docker_install_pulls_bundle_and_always_publishes_web_ui(tmp_path: Path) -> None:
    data_dir = tmp_path / "instance data"
    result = _run_installer(tmp_path, "--data-dir", str(data_dir))

    assert result.returncode == 0, result.stderr
    calls = result.docker_capture.read_text(encoding="utf-8").splitlines()  # type: ignore[attr-defined]
    assert "pull\tghcr.io/enzimo/universal-agents:latest" in calls
    run_call = next(call for call in calls if call.startswith("run\t"))
    assert "\t--publish\t127.0.0.1:18090:18090" in run_call
    assert "\t--publish\t127.0.0.1:18600:18600" in run_call
    assert "\t--label\tio.universal-agents.installer.managed=true" in run_call
    assert f"\t--volume\t{data_dir.resolve()}:/data" in run_call
    assert "\t--env\tINTERNAL_WEBSITE_PUBLIC_PORT=18090" in run_call
    assert "\t--env\tSECURE_TOOL_BROKER_PUBLIC_PORT=18600" in run_call
    assert "\tghcr.io/enzimo/universal-agents:latest\trun" in run_call
    assert "Web UI / onboarding" in result.stdout


def test_docker_install_forwards_skill_refresh_options(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        "--refresh-external-skills",
        "--skills-repository",
        "https://github.com/enzimo/ua-skills",
    )

    assert result.returncode == 0, result.stderr
    calls = result.docker_capture.read_text(encoding="utf-8").splitlines()  # type: ignore[attr-defined]
    run_call = next(call for call in calls if call.startswith("run\t"))
    assert "\trun\t--refresh-external-skills" in run_call
    assert "\t--skills-repository\thttps://github.com/enzimo/ua-skills" in run_call


def test_no_start_only_pulls_image(tmp_path: Path) -> None:
    result = _run_installer(tmp_path, "--no-start", "--image", "ghcr.io/enzimo/universal-agents:test-branch")

    assert result.returncode == 0, result.stderr
    calls = result.docker_capture.read_text(encoding="utf-8").splitlines()  # type: ignore[attr-defined]
    assert "pull\tghcr.io/enzimo/universal-agents:test-branch" in calls
    assert not any(call.startswith("run\t") for call in calls)


def test_replace_refuses_container_not_managed_by_installer(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        "--replace",
        extra_env={"FAKE_CONTAINER_EXISTS": "true", "FAKE_MANAGED_LABEL": "false"},
    )

    assert result.returncode == 1
    assert "was not created by this installer" in result.stderr


def test_native_mode_contract_includes_required_local_services() -> None:
    content = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "install_native_nats" in content
    assert "ua-cedar-authorizer" in content
    assert "sealbox-cli" in content
    assert "sealbox-server" in content
    assert 'cargo build --locked --release --manifest-path "$sealbox_dir/Cargo.toml"' in content
    assert "Node.js 24.5+ was not found" in content
    assert "major === 24 && minor >= 5" in content
    assert "${INTERNAL_WEBSITE_BIND_PORT:-18090}" in content
    assert "docker/launch_headless.py" in content


def test_install_script_has_valid_bash_syntax() -> None:
    subprocess.run(["bash", "-n", str(INSTALL_SCRIPT)], check=True)

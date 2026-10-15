from __future__ import annotations

from pathlib import Path
from packaging.requirements import Requirement

from assistant_gateway.config import GatewayInstanceConfig

ROOT = Path(__file__).resolve().parents[1]
SKILLSPECTOR_COMMIT = "3f11bfa4117ed8beff02d45f660f557489f6bbe5"
SKILLSPECTOR_ARCHIVE_SHA256 = (
    "f8f625d5294b29a8ee9dfdfdf4082eb8b5a14e0556db223b1c6ec499ccba3143"
)


def _locked_versions(name: str) -> set[tuple[str, str]]:
    return {
        (requirement.name, str(requirement.specifier))
        for line in (ROOT / name).read_text().splitlines()
        if line and not line.startswith(("#", " "))
        for requirement in [Requirement(line.removesuffix(" \\"))]
    }


def test_development_lock_shares_runtime_versions_without_polluting_runtime() -> None:
    runtime = _locked_versions("requirements.lock.txt")
    development = _locked_versions("requirements-dev.lock.txt")
    assert runtime <= development
    runtime_names = {name for name, _version in runtime}
    development_names = {name for name, _version in development}
    tools = {"complexipy", "coverage", "mkdocs", "mypy", "pip-audit", "pytest", "ruff", "vulture", "uv"}
    assert not runtime_names & tools
    assert tools <= development_names
    assert "jsonschema" in runtime_names


def test_all_python_runtime_images_use_python_313() -> None:
    standard = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    bundle = (ROOT / "Dockerfile.bundle").read_text(encoding="utf-8")
    mcp = (ROOT / "mcp" / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.13-slim-trixie" in standard
    assert "FROM python:3.13-slim-trixie" in bundle
    assert "FROM python:3.13-slim" in mcp
    assert "FROM python:3.11" not in standard + bundle + mcp
    assert "FROM python:3.12" not in standard + bundle + mcp


def test_python_313_audio_compatibility_is_a_direct_locked_dependency() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    lock = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")

    marker = 'audioop-lts>=0.2.2; python_version >= "3.13"'
    locked_marker = "audioop-lts==0.2.2 ; python_full_version >= '3.13'"
    assert marker in requirements
    assert locked_marker in lock


def test_runtime_images_include_the_iana_timezone_database() -> None:
    for filename in ("Dockerfile", "Dockerfile.bundle"):
        dockerfile = (ROOT / filename).read_text(encoding="utf-8")
        assert "        tzdata \\\n" in dockerfile
        assert "from time_presentation import format_user_timestamp" in dockerfile
        assert "timezone_name='America/Chicago'" in dockerfile


def test_skillspector_is_locked_and_isolated_in_supported_installations() -> None:
    requirement = (ROOT / "requirements.skillspector.txt").read_text(encoding="utf-8")
    lock = (ROOT / "requirements.skillspector.lock.txt").read_text(encoding="utf-8")
    install_script = (ROOT / "install.sh").read_text(encoding="utf-8")

    archive_url = (
        "https://github.com/NVIDIA/skillspector/archive/"
        f"{SKILLSPECTOR_COMMIT}.tar.gz#sha256={SKILLSPECTOR_ARCHIVE_SHA256}"
    )
    assert archive_url in requirement
    assert archive_url in lock
    assert f"--hash=sha256:{SKILLSPECTOR_ARCHIVE_SHA256}" in lock
    assert "requirements.skillspector.lock.txt" in install_script
    assert "--require-hashes" in install_script
    assert '"$app_dir/.skillspector/bin/skillspector" --version' in install_script
    assert (
        'ln -sfn "$app_dir/.skillspector/bin/skillspector" "$bin_dir/skillspector"'
        in install_script
    )
    assert ".skillspector/bin:$UA_NATIVE_APP_DIR/.venv/bin" not in install_script
    assert "Python 3.12 or 3.13" in install_script

    for filename in ("Dockerfile", "Dockerfile.bundle"):
        dockerfile = (ROOT / filename).read_text(encoding="utf-8")
        assert "python -m venv /opt/skillspector" in dockerfile
        assert "requirements.skillspector.lock.txt" in dockerfile
        assert "pip install --require-hashes" in dockerfile
        assert (
            "ln -s /opt/skillspector/bin/skillspector /usr/local/bin/skillspector"
            in dockerfile
        )
        assert "skillspector --version" in dockerfile
        assert "PATH=/opt/skillspector/bin:" not in dockerfile


def test_python_ci_and_runtime_config_default_to_313_packaged_scanner() -> None:
    for workflow in (
        ROOT / ".github" / "workflows" / "tests.yml",
        ROOT / ".github" / "workflows" / "security-quality.yml",
        ROOT / ".github" / "workflows" / "dependency-audit.yml",
    ):
        text = workflow.read_text(encoding="utf-8")
        assert 'python-version: "3.13"' in text
        assert 'python-version: "3.11"' not in text
        assert 'python-version: "3.12"' not in text

    config = GatewayInstanceConfig.create(
        instance_name="packaging-test",
        instance_root="/tmp/ua-packaging-test",
        team_id="team_packaging_test",
        timezone="UTC",
    )
    assert config.workspace.skill_installation.scanner_executable == "skillspector"

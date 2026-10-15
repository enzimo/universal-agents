from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from scripts.release_versions import ReleaseKind, semver_key
from scripts.validate_release_tag import classify_release_tag, validate_release_tag
from scripts.validate_release_tag import main as validate_release_tag_main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_public_release_versions_are_synchronized() -> None:
    expected = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", expected)
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"Current public version: **{expected}**" in readme

    workspace = tomllib.loads((PROJECT_ROOT / "Cargo.toml").read_text(encoding="utf-8"))
    assert workspace["workspace"]["package"]["version"] == expected

    cedar = tomllib.loads(
        (PROJECT_ROOT / "rust" / "ua-cedar-authorizer" / "Cargo.toml").read_text(
            encoding="utf-8"
        )
    )
    assert cedar["package"]["version"]["workspace"] is True

    sidecar_root = PROJECT_ROOT / "assistant_gateway" / "interactive_apps" / "photon_sidecar"
    package = json.loads((sidecar_root / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((sidecar_root / "package-lock.json").read_text(encoding="utf-8"))
    assert package["version"] == expected
    assert package_lock["version"] == expected
    assert package_lock["packages"][""]["version"] == expected


# Install instructions people copy name one release: the tag install.sh is
# fetched from, the image it starts, and the offline guide's release. Each must
# be VERSION, or a new install starts an old release (they stayed at 0.4.5 and
# 0.5.0 for several releases before this check existed).
INSTALL_PIN_FILES = (
    "QUICKSTART.md",
    "user_docs_source/quickstart.md",
    "user_docs_source/docker.md",
    "docker-compose-setup.html",
)
INSTALL_PIN = re.compile(
    r"universal-agents/v(?P<script>\d+\.\d+\.\d+)/install\.sh"
    r"|install\.sh\?ref=v(?P<ref>\d+\.\d+\.\d+)"
    r"|ghcr\.io/enzimo/universal-agents:(?P<image>\d+\.\d+\.\d+)"
    r"|data-runtime-version=\"(?P<guide>\d+\.\d+\.\d+)\""
    r"|Universal Agents (?P<footer>\d+\.\d+\.\d+) · Desktop"
)


@pytest.mark.parametrize("relative_path", INSTALL_PIN_FILES)
def test_install_instructions_name_the_current_release(relative_path: str) -> None:
    expected = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    text = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    pins = {
        version
        for match in INSTALL_PIN.finditer(text)
        for version in match.groupdict().values()
        if version
    }
    assert pins, f"{relative_path} names no release in its install instructions"
    assert pins == {expected}, f"{relative_path} installs {sorted(pins)}, not {expected}"


def test_offline_setup_guide_names_the_release_default_model() -> None:
    from default_values import LLM_DEFAULTS

    guide = (PROJECT_ROOT / "docker-compose-setup.html").read_text(encoding="utf-8")
    assert f"A fresh install defaults to <code>{LLM_DEFAULTS.model_id.value}</code>" in guide


def test_release_tag_validator_accepts_only_the_version_tag(tmp_path: Path) -> None:
    version_path = tmp_path / "VERSION"
    version_path.write_text("1.2.3\n", encoding="utf-8")

    assert validate_release_tag("v1.2.3", version_path=version_path) == "1.2.3"

    for invalid_tag in ("1.2.3", "v1.2.4", "v1.2.3-rc1", "v01.2.3"):
        with pytest.raises(ValueError):
            validate_release_tag(invalid_tag, version_path=version_path)


@pytest.mark.parametrize(
    ("tag", "version", "kind"),
    [
        ("v1.2.4-beta.1", "1.2.4-beta.1", ReleaseKind.BETA),
        ("v1.3.0-rc.12", "1.3.0-rc.12", ReleaseKind.RC),
        ("v2.0.0-beta.3", "2.0.0-beta.3", ReleaseKind.BETA),
        ("v1.2.4-nightly.202610120317", "1.2.4-nightly.202610120317", ReleaseKind.NIGHTLY),
    ],
)
def test_release_tag_validator_accepts_prereleases_of_the_next_release(
    tmp_path: Path, tag: str, version: str, kind: ReleaseKind
) -> None:
    # VERSION names the newest published release, so a pre-release leads up
    # to the release after it and sorts after VERSION.
    version_path = tmp_path / "VERSION"
    version_path.write_text("1.2.3\n", encoding="utf-8")

    release = classify_release_tag(tag, version_path=version_path)

    assert validate_release_tag(tag, version_path=version_path) == version
    assert release.kind is kind
    assert release.prerelease
    assert semver_key(version) > semver_key("1.2.3")


@pytest.mark.parametrize(
    "tag",
    [
        "v1.2.3-beta.1",  # VERSION 1.2.3 is already published
        "v1.2.5-beta.1",  # skips 1.2.4
        "v1.4.0-rc.1",  # skips 1.3.0
        "v1.2.4-beta.0",
        "v1.2.4-beta.01",
        "v1.2.4-beta",
        "v1.2.4-alpha.1",
        "v1.2.4-rc.1.2",
        "v1.2.4-nightly.20261012",  # not a full minute stamp
        "v1.2.4-nightly.202613120317",  # month 13
        "v1.2.4-nightly.202610120317.1",
        "v1.2.4-NIGHTLY.202610120317",
        "v1.2.4+build.1",
    ],
)
def test_release_tag_validator_rejects_other_prerelease_tags(tmp_path: Path, tag: str) -> None:
    version_path = tmp_path / "VERSION"
    version_path.write_text("1.2.3\n", encoding="utf-8")

    with pytest.raises(ValueError):
        validate_release_tag(tag, version_path=version_path)


# https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string
OFFICIAL_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


@pytest.mark.parametrize(
    "tag", ["v1.2.3", "v1.2.4-beta.7", "v1.3.0-rc.1", "v2.0.0-nightly.209912312359"]
)
def test_accepted_tags_are_semantic_versions_for_image_tags(tmp_path: Path, tag: str) -> None:
    # docker/metadata-action applies its pre-release rules only to valid semver.
    version_path = tmp_path / "VERSION"
    version_path.write_text("1.2.3\n", encoding="utf-8")

    assert OFFICIAL_SEMVER.fullmatch(validate_release_tag(tag, version_path=version_path))


@pytest.mark.parametrize(
    ("tag", "printed"),
    [
        ("v1.2.3", "stable"),
        ("v1.2.4-beta.1", "beta"),
        ("v1.2.4-rc.2", "rc"),
        ("v1.2.4-nightly.202610120317", "nightly"),
    ],
)
def test_release_tag_cli_prints_version_or_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tag: str, printed: str
) -> None:
    version_path = tmp_path / "VERSION"
    version_path.write_text("1.2.3\n", encoding="utf-8")

    assert validate_release_tag_main(["validate", "--version-file", str(version_path), tag]) == 0
    assert capsys.readouterr().out == f"{tag[1:]}\n"
    assert (
        validate_release_tag_main(["validate", "--kind", "--version-file", str(version_path), tag])
        == 0
    )
    assert capsys.readouterr().out == f"{printed}\n"


def test_release_tag_cli_reports_mismatch_without_printing_a_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    version_path = tmp_path / "VERSION"
    version_path.write_text("1.2.3\n", encoding="utf-8")

    assert validate_release_tag_main(["validate", "--version-file", str(version_path), "v1.2.3-beta.1"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Release validation failed" in captured.err
    with pytest.raises(SystemExit) as usage:
        validate_release_tag_main(["validate"])
    assert usage.value.code == 2


def test_release_tag_validator_rejects_noncanonical_version(tmp_path: Path) -> None:
    version_path = tmp_path / "VERSION"
    version_path.write_text("01.2.3\n", encoding="utf-8")

    with pytest.raises(ValueError):
        validate_release_tag("v01.2.3", version_path=version_path)


def test_repository_declares_apache_license() -> None:
    license_text = (PROJECT_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text


def test_github_checkouts_do_not_persist_credentials() -> None:
    workflow_dir = PROJECT_ROOT / ".github" / "workflows"

    for workflow_path in workflow_dir.glob("*.yml"):
        workflow = yaml.load(
            workflow_path.read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )
        for job in workflow.get("jobs", {}).values():
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/checkout@"):
                    assert step.get("with", {}).get("persist-credentials") == "false"

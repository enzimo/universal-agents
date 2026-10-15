from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "publish-release.yml"
RELEASE_CONFIG_PATH = PROJECT_ROOT / ".github" / "release.yml"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_release_workflow_is_tag_only_and_can_publish_assets() -> None:
    workflow = _workflow()

    assert workflow["on"] == {"push": {"tags": ["v*.*.*"]}}
    assert workflow["permissions"] == {"contents": "write"}
    assert list(workflow["jobs"]) == ["publish-release"]


def test_release_workflow_validates_archives_and_publishes_generated_notes() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["publish-release"]["steps"]
    checkout = next(step for step in steps if step.get("name") == "Check out tagged source")
    validate = next(step for step in steps if step.get("name") == "Validate release tag")
    archive = next(
        step for step in steps if step.get("name") == "Build tracked-source release archive"
    )
    publish = next(step for step in steps if step.get("name") == "Publish GitHub Release")

    assert checkout["with"]["persist-credentials"] == "false"
    assert 'scripts/validate_release_tag.py "${GITHUB_REF_NAME}"' in validate["run"]
    assert "git archive" in archive["run"]
    assert '--prefix="universal-agents-${RELEASE_VERSION}/"' in archive["run"]
    assert 'sha256sum "universal-agents-${RELEASE_VERSION}.zip"' in archive["run"]
    assert 'cp docker-compose-setup.html dist/setup-guide.html' in archive["run"]
    assert archive["env"]["RELEASE_KIND"] == "${{ steps.release.outputs.kind }}"
    assert publish["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert 'gh release create "${RELEASE_TAG}"' in publish["run"]
    assert 'gh release view "${RELEASE_TAG}"' in publish["run"]
    assert 'gh release upload "${RELEASE_TAG}" "${assets[@]}"' in publish["run"]
    assert "--clobber" in publish["run"]
    assert "--verify-tag" in publish["run"]
    assert "--fail-on-no-commits" in publish["run"]
    assert "--generate-notes" in publish["run"]
    assert '"${RELEASE_ARCHIVE}#Universal Agents ${RELEASE_VERSION} source bundle"' in publish["run"]


def test_prerelease_tags_publish_github_prereleases_that_never_become_latest() -> None:
    steps = _workflow()["jobs"]["publish-release"]["steps"]
    validate = next(step for step in steps if step.get("name") == "Validate release tag")
    publish = next(step for step in steps if step.get("name") == "Publish GitHub Release")

    assert 'scripts/validate_release_tag.py --kind "${GITHUB_REF_NAME}"' in validate["run"]
    assert 'echo "kind=${kind}" >> "${GITHUB_OUTPUT}"' in validate["run"]
    # The archive name carries the full version, suffix included.
    assert 'archive="dist/universal-agents-${version}.zip"' in validate["run"]
    assert publish["env"]["RELEASE_KIND"] == "${{ steps.release.outputs.kind }}"
    assert (
        'if [ "${RELEASE_KIND}" != "stable" ]; then\n'
        "  release_options+=(--prerelease --latest=false)\n"
        "fi"
    ) in publish["run"]
    assert '"${release_options[@]}"' in publish["run"]


def test_generated_notes_start_at_the_previous_stable_release() -> None:
    # Without an explicit start, nightlies and betas could become the comparison
    # point and shorten a stable release's notes.
    steps = _workflow()["jobs"]["publish-release"]["steps"]
    publish = next(step for step in steps if step.get("name") == "Publish GitHub Release")

    assert "--exclude-drafts --exclude-pre-releases" in publish["run"]
    assert 'python3 scripts/release_versions.py previous-stable "${RELEASE_VERSION}"' in publish["run"]
    assert 'release_options+=(--notes-start-tag "${previous_stable}")' in publish["run"]
    assert (PROJECT_ROOT / "scripts" / "release_versions.py").exists()


def test_release_notes_have_noteworthy_change_categories_and_catch_all() -> None:
    config = yaml.safe_load(RELEASE_CONFIG_PATH.read_text(encoding="utf-8"))
    categories = config["changelog"]["categories"]
    titles = [category["title"] for category in categories]

    assert titles[:5] == [
        "Breaking Changes",
        "Security",
        "Features",
        "Fixes",
        "Documentation",
    ]
    assert categories[-1] == {
        "title": "Other Noteworthy Changes",
        "labels": ["*"],
    }


@pytest.mark.parametrize("kind", ["stable", "beta", "rc"])
@pytest.mark.parametrize("exists", [False, True])
def test_publication_assets_and_metadata_match_the_release_kind(
    tmp_path: Path, kind: str, exists: bool
) -> None:
    """Execute both workflow scripts, including repair of a misclassified release."""
    version = "0.9.2" if kind == "stable" else f"0.9.3-{kind}.1"
    environment = {
        **os.environ,
        "RELEASE_KIND": kind,
        "RELEASE_VERSION": version,
        "RELEASE_TAG": f"v{version}",
        "RELEASE_ARCHIVE": f"dist/universal-agents-{version}.zip",
        "GITHUB_REPOSITORY": "enzimo/universal-agents",
        "GITHUB_SHA": "HEAD",
        "REVIEW_RELEASE_STATE": str(tmp_path / "release-state.json"),
        "REVIEW_RELEASE_CALLS": str(tmp_path / "release-calls.jsonl"),
    }
    # A real tracked-source archive proves the guide is copied only when appropriate.
    (tmp_path / "docker-compose-setup.html").write_text("Stable guide for 0.9.2\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/release_versions.py").write_text(
        (PROJECT_ROOT / "scripts/release_versions.py").read_text()
    )
    for arguments in (
        ["init", "-q"],
        ["add", "docker-compose-setup.html"],
        ["-c", "user.name=Release Test", "-c", "user.email=release@example.invalid", "commit", "-qm", "fixture"],
    ):
        subprocess.run(["rtk", "proxy", "git", *arguments], cwd=tmp_path, check=True, capture_output=True)
    # The API stand-in starts with an ordinary Latest release and stale guide assets.
    state_path = Path(environment["REVIEW_RELEASE_STATE"])
    state_path.write_text(json.dumps({
        "exists": exists, "prerelease": False, "latest": True,
        "assets": ["setup-guide.html", "setup-guide.html.sha256", "container-image.txt"] if exists else [],
    }))
    commands = tmp_path / "commands"
    commands.mkdir()
    fake_gh = commands / "gh"
    fake_gh.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
path = Path(os.environ["REVIEW_RELEASE_STATE"])
state = json.loads(path.read_text())
with open(os.environ["REVIEW_RELEASE_CALLS"], "a") as calls:
    calls.write(json.dumps(args) + "\\n")
operation = args[1]
if operation == "list":
    print("v0.9.1")
elif operation == "view":
    if not state["exists"]:
        sys.exit(1)
    if "--json" in args:
        for asset in state["assets"]:
            if asset in ("setup-guide.html", "setup-guide.html.sha256"):
                print(asset)
elif operation in ("create", "edit"):
    state["exists"] = True
    if "--prerelease" in args:
        state["prerelease"] = True
    if "--latest=false" in args:
        state["latest"] = False
    if operation == "create":
        state["assets"] = [Path(arg.split("#")[0]).name for arg in args if arg.startswith("dist/")]
elif operation == "delete-asset":
    state["assets"].remove(args[3])
elif operation == "upload":
    for arg in args:
        if arg.startswith("dist/"):
            name = Path(arg.split("#")[0]).name
            if name not in state["assets"]:
                state["assets"].append(name)
else:
    raise AssertionError(args)
path.write_text(json.dumps(state))
''')
    fake_gh.chmod(0o755)
    environment["PATH"] = str(commands) + os.pathsep + environment["PATH"]
    steps = _workflow()["jobs"]["publish-release"]["steps"]
    for name in ("Build tracked-source release archive", "Publish GitHub Release", "Publish GitHub Release"):
        script = next(step["run"] for step in steps if step.get("name") == name)
        result = subprocess.run(
            ["rtk", "proxy", "bash", "-e", "-o", "pipefail", "-c", script],
            cwd=tmp_path, env=environment, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
    state = json.loads(state_path.read_text())
    calls = [json.loads(line) for line in Path(environment["REVIEW_RELEASE_CALLS"]).read_text().splitlines()]
    assert Path(tmp_path / environment["RELEASE_ARCHIVE"]).exists()
    assert f"universal-agents-{version}.zip" in state["assets"]
    assert f"universal-agents-{version}.zip.sha256" in state["assets"]
    assert (tmp_path / "dist/setup-guide.html").exists() == (kind == "stable")
    assert ("setup-guide.html" in state["assets"]) == (kind == "stable")
    assert ("setup-guide.html.sha256" in state["assets"]) == (kind == "stable")
    assert state["prerelease"] == (kind != "stable")
    assert state["latest"] == (kind == "stable")
    if exists:
        assert "container-image.txt" in state["assets"]
        if kind != "stable":
            edit = next(i for i, call in enumerate(calls) if call[:2] == ["release", "edit"])
            upload = next(i for i, call in enumerate(calls) if call[:2] == ["release", "upload"])
            assert edit < upload

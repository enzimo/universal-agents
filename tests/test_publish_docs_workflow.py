from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml


WORKFLOW_PATH = Path.cwd() / ".github" / "workflows" / "publish-docs.yml"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_docs_workflow_builds_for_stable_tags_but_deploys_manually() -> None:
    workflow = _workflow()

    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    assert workflow["on"]["push"] == {"tags": ["v*.*.*", "!v*.*.*-*"]}
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}

    deploy = workflow["jobs"]["deploy"]
    assert deploy["if"] == (
        "github.event.repository.private && "
        "(github.event_name != 'pull_request' || "
        "github.event.pull_request.head.repo.full_name == github.repository) && "
        "(github.event_name == 'workflow_dispatch')"
    )
    assert deploy["needs"] == "build"
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["environment"]["name"] == "github-pages"
    assert deploy["concurrency"] == {
        "group": "github-pages",
        "cancel-in-progress": "false",
    }


def test_docs_workflow_builds_and_uploads_a_pages_safe_artifact() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["build"]["steps"]
    checkout = next(step for step in steps if step.get("name") == "Check out source")
    setup_python = next(step for step in steps if step.get("name") == "Set up Python")
    validate = next(step for step in steps if step.get("name") == "Validate release tag")
    pages = next(step for step in steps if step.get("id") == "pages")
    install = next(
        step
        for step in steps
        if step.get("name") == "Install documentation dependencies"
    )
    build = next(
        step for step in steps if step.get("name") == "Build GitHub Pages artifact"
    )
    upload = next(
        step for step in steps if step.get("name") == "Upload GitHub Pages artifact"
    )
    deploy_steps = workflow["jobs"]["deploy"]["steps"]
    assert "UA_CI_HOOK_VERSION" in deploy_steps[0]["run"]
    deployment = next(step for step in deploy_steps if step.get("id") == "deployment")

    assert (
        checkout["uses"]
        == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    )
    assert checkout["with"]["persist-credentials"] == "false"
    assert (
        setup_python["uses"]
        == "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
    )
    assert setup_python["with"]["python-version"] == "3.13"
    assert validate["if"] == "github.ref_type == 'tag'"
    assert 'scripts/validate_release_tag.py "${GITHUB_REF_NAME}"' in validate["run"]
    assert 'scripts/validate_release_tag.py --kind "${GITHUB_REF_NAME}"' in validate["run"]
    assert 'if [ "${kind}" != "stable" ]; then' in validate["run"]
    assert "exit 1" in validate["run"]
    assert (
        pages["uses"]
        == "actions/configure-pages@983d7736d9b0ae728b81ab479565c72886d7745b"
    )
    assert pages["if"] == "github.event_name == 'workflow_dispatch'"
    assert install["run"] == (
        "python -m pip install --require-hashes -r requirements-dev.lock.txt"
    )
    expected_site_url = (
        "${{ steps.pages.outputs.base_url || "
        "format('https://{0}.github.io/{1}', "
        "github.repository_owner, github.event.repository.name) }}/"
    )
    assert build["env"] == {
        "UA_DOCS_HOMEPAGE": expected_site_url,
        "UA_DOCS_SITE_URL": expected_site_url,
    }
    assert "--site-dir user_docs/pages" in build["run"]
    assert "--github-pages" in build["run"]
    assert (
        upload["uses"]
        == "actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b"
    )
    assert upload["with"]["path"] == "user_docs/pages"
    # Only an actual Pages deployment needs stored output; automatic CI still builds it.
    assert "if" not in build
    assert upload["if"] == pages["if"] == "github.event_name == 'workflow_dispatch'"
    assert upload["with"]["retention-days"] == "7"
    assert "continue-on-error" not in upload
    assert (
        deployment["uses"]
        == "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e"
    )


def _tag_starts_docs(tag: str, patterns: list[str]) -> bool:
    # GitHub applies tag filters in order; a later "!" pattern excludes a match.
    # Tags contain no "/", so fnmatch's "*" behaves like GitHub's here.
    selected = False
    for pattern in patterns:
        if pattern.startswith("!"):
            if fnmatchcase(tag, pattern[1:]):
                selected = False
        elif fnmatchcase(tag, pattern):
            selected = True
    return selected


@pytest.mark.parametrize(
    ("tag", "starts"),
    [
        ("v0.9.1", True),
        ("v10.20.30", True),
        ("v0.9.2-beta.1", False),
        ("v0.9.2-rc.2", False),
        ("v0.9.2-nightly.202610120317", False),
    ],
)
def test_prerelease_tags_never_build_or_publish_docs(tag: str, starts: bool) -> None:
    patterns = _workflow()["on"]["push"]["tags"]

    assert _tag_starts_docs(tag, patterns) is starts

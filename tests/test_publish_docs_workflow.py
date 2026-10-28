from __future__ import annotations

from fnmatch import fnmatchcase
import json
from pathlib import Path
import subprocess

import pytest
import yaml


WORKFLOW_PATH = Path.cwd() / ".github" / "workflows" / "publish-docs.yml"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_docs_workflow_builds_public_contributions_and_publishes_trusted_refs() -> None:
    workflow = _workflow()

    assert "pull_request" in workflow["on"]
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["on"]["push"]["tags"] == ["v*.*.*", "!v*.*.*-*"]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}

    build = workflow["jobs"]["build"]
    assert "if" not in build
    assert build["runs-on"] == "ubuntu-24.04"
    assert build["permissions"] == {"contents": "read", "pages": "read"}
    deploy = workflow["jobs"]["deploy"]
    assert deploy["runs-on"] == "ubuntu-24.04"
    assert deploy["needs"] == "build"
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["environment"]["name"] == "github-pages"
    assert deploy["concurrency"] == {
        "group": "github-pages",
        "cancel-in-progress": "false",
    }


def test_docs_publish_conditions_allow_only_main_and_stable_releases() -> None:
    """Execute the workflow's boolean expressions against event/ref combinations.

    The expressions use the JavaScript-compatible subset of GitHub's language;
    pass its two string functions explicitly so these are the actual conditions.
    """
    workflow = _workflow()
    steps = workflow["jobs"]["build"]["steps"]
    conditions = [workflow["jobs"]["deploy"]["if"]] + [
        step["if"] for step in steps
        if step.get("name") in {"Configure GitHub Pages", "Upload GitHub Pages artifact"}
    ]
    assert len(conditions) == 3
    cases = [
        ("push", "refs/heads/main", True),
        ("push", "refs/tags/v0.9.1", True),
        ("workflow_dispatch", "refs/heads/main", True),
        ("workflow_dispatch", "refs/tags/v0.9.1", True),
        ("pull_request", "refs/pull/123/merge", False),
        ("pull_request", "refs/heads/main", False),
        ("workflow_dispatch", "refs/heads/feature/docs", False),
        ("workflow_dispatch", "refs/tags/v0.9.2-beta.1", False),
        ("workflow_dispatch", "refs/tags/v0.9.2-rc.1", False),
        ("workflow_dispatch", "refs/tags/v0.9.2-nightly.202610280317", False),
    ]
    contexts = [
        {"event_name": event, "ref": ref, "event": {"repository": {"private": private}}}
        for private in (False, True) for event, ref, _ in cases
    ]
    completed = subprocess.run(
        [
            "node", "--eval",
            "const {conditions, contexts} = JSON.parse(process.argv[1]);"
            "const result = conditions.map(condition => {"
            "  const decide = new Function('github', 'startsWith', 'contains', 'return (' + condition + ')');"
            "  return contexts.map(github => decide(github, (s, p) => s.startsWith(p), (s, p) => s.includes(p)));"
            "}); process.stdout.write(JSON.stringify(result));",
            json.dumps({"conditions": conditions, "contexts": contexts}),
        ],
        check=True, capture_output=True, text=True,
    )
    expected = [allowed for _, _, allowed in cases] * 2
    assert json.loads(completed.stdout) == [expected] * len(conditions)


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
    # Validation always builds, but only trusted refs can upload or publish.
    assert "if" not in build
    assert upload["if"] == pages["if"] == workflow["jobs"]["deploy"]["if"]
    assert upload["with"]["retention-days"] == "7"
    assert "continue-on-error" not in upload
    assert (
        deployment["uses"]
        == "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e"
    )


def test_docs_bootstraps_rtk_before_any_shell_step() -> None:
    workflow = _workflow()
    assert workflow["defaults"]["run"]["shell"].startswith("rtk proxy bash ")
    steps = workflow["jobs"]["build"]["steps"]
    bootstrap = next(step for step in steps if step.get("name") == "Install pinned RTK")
    assert bootstrap["uses"] == "actions/github-script@ed597411d8f924073f98dfc5c65a23a2325f34cd"
    script = bootstrap["with"]["script"]
    assert "createHash('sha256')" in script
    assert script.index("RTK archive checksum mismatch") < script.index("await exec.exec('tar'")
    assert "core.addPath(directory)" in script
    first_shell = next(step for step in steps if "run" in step)
    assert steps.index(bootstrap) < steps.index(first_shell)
    assert first_shell["run"] == "rtk --version"


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

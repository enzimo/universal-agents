from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "nightly.yml"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_nightly_runs_daily_or_by_hand_from_main_only() -> None:
    workflow = _workflow()

    assert workflow["on"]["schedule"] == [{"cron": "17 3 * * *"}]
    assert "workflow_dispatch" in workflow["on"]
    assert "push" not in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "nightly-pre-release",
        "cancel-in-progress": "false",
    }
    job = workflow["jobs"]["nightly"]
    assert job["if"] == "github.event.repository.private && github.ref == 'refs/heads/main'"
    assert job["runs-on"] == ["self-hosted", "Linux", "X64", "ua-build", "ua-linux"]
    # Releases and tags need contents: write; starting publish-container needs actions: write.
    assert job["permissions"] == {"contents": "write", "actions": "write"}


def test_nightly_job_is_a_thin_call_to_the_tested_script() -> None:
    steps = _workflow()["jobs"]["nightly"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    publish = steps[-1]

    assert "UA_CI_HOOK_VERSION" in steps[0]["run"]
    assert checkout["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert checkout["with"] == {"persist-credentials": "false"}
    assert publish["env"] == {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "TARGET_SHA": "${{ github.sha }}",
    }
    assert publish["run"].split() == [
        "python3",
        "scripts/nightly_release.py",
        "--repository",
        '"$GITHUB_REPOSITORY"',
        "--target-sha",
        '"$TARGET_SHA"',
        "--dispatch-ref",
        "main",
        "--keep",
        "7",
    ]
    assert (PROJECT_ROOT / "scripts" / "nightly_release.py").exists()


def test_nightly_dispatch_target_accepts_a_release_tag() -> None:
    container = yaml.load(
        (WORKFLOW_PATH.parent / "publish-container.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert "release_tag" in container["on"]["workflow_dispatch"]["inputs"]

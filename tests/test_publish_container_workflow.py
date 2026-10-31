from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


WORKFLOW_PATH = Path.cwd() / ".github" / "workflows" / "publish-container.yml"


def _workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_publish_workflow_requires_a_version_tag_or_manual_dispatch() -> None:
    workflow = _workflow()

    assert workflow["on"]["push"]["tags"] == ["v*.*.*"]
    assert "branches" not in workflow["on"]["push"]
    assert "workflow_dispatch" in workflow["on"]
    assert "image_tag" in workflow["on"]["workflow_dispatch"]["inputs"]

    steps = workflow["jobs"]["build-and-push"]["steps"]
    validate = next(step for step in steps if step.get("name") == "Validate release tag")
    assert validate["if"] == "env.RELEASE_TAG != ''"
    assert 'scripts/validate_release_tag.py "$RELEASE_TAG"' in validate["run"]


def test_publish_workflow_emits_release_branch_and_commit_tags() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["build-and-push"]["steps"]
    metadata = next(step for step in steps if step.get("id") == "metadata")
    tags = metadata["with"]["tags"]

    assert metadata["with"]["flavor"] == "latest=auto\n"
    assert "type=semver,pattern={{version}}" in tags
    assert "type=semver,pattern={{major}}.{{minor}}" in tags
    assert "type=semver,pattern={{major}}" in tags
    assert (
        "type=raw,value={{branch}},enable=${{ github.event_name == 'workflow_dispatch' && inputs.release_tag == '' }}"
        in tags
    )
    assert "type=sha,prefix=sha-" in tags
    assert "inputs.image_tag" in tags


WAIVED = (
    "!inputs.build_only && env.RELEASE_TAG != '' && github.event.repository.private && "
    "(vars.PRIVATE_RELEASE_ATTESTATION_WAIVER == env.RELEASE_TAG || "
    "(steps.release.outputs.kind == 'nightly' && vars.PRIVATE_NIGHTLY_ATTESTATION_WAIVER == 'true'))"
)
ATTESTED = (
    "!inputs.build_only && (env.RELEASE_TAG == '' || github.event.repository.private == false || "
    "(vars.PRIVATE_RELEASE_ATTESTATION_WAIVER != env.RELEASE_TAG && "
    "(steps.release.outputs.kind != 'nightly' || vars.PRIVATE_NIGHTLY_ATTESTATION_WAIVER != 'true')))"
)


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("release_tag", ["", "v0.9.1", "v0.9.2-beta.1", "v0.9.2-nightly.202610120317"])
@pytest.mark.parametrize("exact_waiver", ["", "v0.9.1", "v0.9.2-nightly.202610120317"])
@pytest.mark.parametrize("nightly_waiver", ["", "true", "yes"])
def test_every_published_image_is_either_attested_or_visibly_waived(
    private: bool, release_tag: str, exact_waiver: str, nightly_waiver: str
) -> None:
    # Python mirror of the two step conditions (build_only runs never reach this job).
    kind = "" if not release_tag else ("nightly" if "-nightly." in release_tag else "other")
    waived = bool(release_tag) and private and (
        exact_waiver == release_tag or (kind == "nightly" and nightly_waiver == "true")
    )
    attested = release_tag == "" or not private or (
        exact_waiver != release_tag and (kind != "nightly" or nightly_waiver != "true")
    )

    assert waived != attested
    if waived and kind != "nightly":
        assert exact_waiver == release_tag  # betas, rcs and stable need the exact tag
    if not private:
        assert attested


def test_publish_workflow_targets_the_source_repository_package() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["build-and-push"]["steps"]
    checkout = next(step for step in steps if step.get("name") == "Check out source")
    login = next(step for step in steps if step.get("name") == "Log in to GHCR")
    metadata = next(step for step in steps if step.get("id") == "metadata")
    publication = workflow["jobs"]["publish-manifest"]["steps"]
    waiver_warning = next(
        step
        for step in publication
        if step.get("name") == "Warn when private-release attestation is waived"
    )
    attestation = next(
        step for step in publication if step.get("name") == "Attest published image"
    )

    assert workflow["env"]["REGISTRY"] == "ghcr.io"
    assert workflow["env"]["IMAGE_NAME"] == "${{ github.repository }}"
    assert checkout["with"]["persist-credentials"] == "false"
    assert login["with"]["registry"] == "${{ env.REGISTRY }}"
    assert login["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"
    assert metadata["with"]["images"] == "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}"
    assert (
        "org.opencontainers.image.source=${{ github.server_url }}/${{ github.repository }}"
        in metadata["with"]["labels"]
    )
    assert (
        attestation["uses"]
        == "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6"
    )
    assert waiver_warning["if"] == WAIVED
    assert "::warning title=Image attestation waived::" in waiver_warning["run"]
    assert "an explicit exact-tag waiver" in waiver_warning["run"]
    assert "the standing nightly waiver PRIVATE_NIGHTLY_ATTESTATION_WAIVER" in waiver_warning["run"]
    assert waiver_warning["env"] == {
        "RELEASE_REF": "${{ env.RELEASE_TAG }}",
        "REPOSITORY": "${{ github.repository }}",
        "EXACT_TAG_WAIVER": "${{ vars.PRIVATE_RELEASE_ATTESTATION_WAIVER }}",
    }
    assert attestation["if"] == ATTESTED
    assert (
        attestation["with"]["subject-name"]
        == "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}"
    )


def test_publish_workflow_builds_full_multi_platform_personal_bundle() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["build-and-push"]
    steps = job["steps"]
    build = next(step for step in steps if step.get("id") == "push")
    build_args = build["with"]["build-args"]

    assert job["permissions"]["packages"] == "write"
    assert (
        build["uses"]
        == "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a"
    )
    assert build["with"]["file"] == "Dockerfile.bundle"
    assert build["with"]["platforms"] == "linux/${{ matrix.arch }}"
    matrix = job["strategy"]["matrix"]["include"]
    assert [(entry["arch"], entry["docker_arch"]) for entry in matrix] == [("amd64", "x86_64"), ("arm64", "aarch64")]
    assert "ua-linux" in matrix[0]["runner"]
    assert "ua-macos" in matrix[1]["runner"]
    assert "push-by-digest=true" in build["with"]["outputs"]
    assert build["with"]["tags"] == "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}"
    assert workflow["jobs"]["publish-manifest"]["needs"] == "build-and-push"
    assert build["with"]["push"] == "${{ !inputs.build_only }}"
    assert "INSTALL_NATS=true" in build_args
    assert "INSTALL_POSTGRES=false" in build_args
    assert "INSTALL_SQLITE=true" in build_args
    assert "INSTALL_NODEJS=true" in build_args
    assert "INSTALL_CREDENTIAL_CLIS=sealbox" in build_args
    assert "STRIP_SOURCE_CODE=true" in build_args


def test_build_only_does_not_publish_images_caches_or_attestations() -> None:
    workflow = _workflow()
    option = workflow["on"]["workflow_dispatch"]["inputs"]["build_only"]
    assert option["type"] == "boolean"
    assert option["default"] == "false"
    steps = workflow["jobs"]["build-and-push"]["steps"]
    build = next(step for step in steps if step.get("id") == "push")
    for key in ("push", "cache-from", "cache-to"):
        assert "!inputs.build_only" in build["with"][key]
    assert "!inputs.build_only" in workflow["jobs"]["publish-manifest"]["if"]
    for name in ("Log in to GHCR", "Record immutable platform digest"):
        step = next(step for step in steps if step.get("name") == name)
        assert "!inputs.build_only" in step["if"]


def test_platform_digests_do_not_depend_on_artifact_storage() -> None:
    workflow = _workflow()
    build = workflow["jobs"]["build-and-push"]
    assert build["outputs"] == {
        "amd64": "${{ steps.digest.outputs.amd64 }}",
        "arm64": "${{ steps.digest.outputs.arm64 }}",
    }
    record = next(step for step in build["steps"] if step.get("id") == "digest")
    assert record["env"]["IMAGE_ARCH"] == "${{ matrix.arch }}"
    assert '"$IMAGE_ARCH" "$IMAGE_DIGEST" >> "$GITHUB_OUTPUT"' in record["run"]
    steps = workflow["jobs"]["publish-manifest"]["steps"]
    restore = next(s for s in steps if s.get("name") == "Restore platform digests from job outputs")
    assert restore["env"] == {
        "AMD64_DIGEST": "${{ needs.build-and-push.outputs.amd64 }}",
        "ARM64_DIGEST": "${{ needs.build-and-push.outputs.arm64 }}",
    }
    assert workflow["env"]["DOCKER_BUILD_RECORD_UPLOAD"] == "false"
    assert all("artifact@" not in s.get("uses", "") for s in build["steps"] + steps)


def test_recovery_builds_tagged_source_and_attaches_verified_reference() -> None:
    workflow = _workflow()
    assert "release_tag" in workflow["on"]["workflow_dispatch"]["inputs"]
    for job in workflow["jobs"].values():
        checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"]["ref"] == "${{ inputs.release_tag || github.ref }}"
        metadata = next(s for s in job["steps"] if s.get("id") == "metadata")
        assert metadata["with"]["context"] == "git"
        assert "value=${{ env.RELEASE_TAG }}" in metadata["with"]["tags"]
    publish = workflow["jobs"]["publish-manifest"]
    assert publish["permissions"]["contents"] == "write"
    attachment = next(
        s for s in publish["steps"] if s.get("name") == "Attach verified container reference to release"
    )
    assert attachment["if"] == "env.RELEASE_TAG != ''"
    assert attachment["env"]["IMAGE_DIGEST"] == "${{ steps.merge.outputs.digest }}"
    assert 'gh release upload "$RELEASE_TAG"' in attachment["run"]
    assert "sha256sum container-image.txt" in attachment["run"]
    assert "git rev-parse HEAD" in attachment["run"]
    assert publish["steps"].index(attachment) > next(
        i for i, s in enumerate(publish["steps"]) if s.get("name") == "Attest published image"
    )


PIN_STEP = "Keep registry logins in this job's Docker settings"
CONFIRM_STEP = "Confirm the registry login stayed in this job's Docker settings"


def _login_jobs() -> list[tuple[str, str, list[dict]]]:
    """Every job in every workflow that logs in to a container registry."""
    jobs = []
    for path in sorted(WORKFLOW_PATH.parent.glob("*.yml")):
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        for name, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            if any("docker/login-action" in step.get("uses", "") for step in steps):
                jobs.append((path.name, name, steps))
    return jobs


def _uses_docker(step: dict) -> bool:
    return "docker" in step.get("run", "") or step.get("uses", "").startswith("docker/")


def test_registry_logins_stay_in_each_jobs_private_docker_settings() -> None:
    jobs = _login_jobs()
    assert {(workflow, job) for workflow, job, _ in jobs} == {
        ("publish-container.yml", "build-and-push"),
        ("publish-container.yml", "publish-manifest"),
    }
    pins, confirmations = set(), set()
    for _, _, steps in jobs:
        names = [step.get("name") for step in steps]
        pin = names.index(PIN_STEP)
        # Docker must not run, and so cannot write its own settings, before the pin.
        assert not any(
            _uses_docker(step)
            for step in steps[:pin]
            if step.get("name") != "Record Docker cleanup ownership"
        )
        for index, step in enumerate(steps):
            if "docker/login-action" in step.get("uses", ""):
                assert pin < index
                confirmation = steps[index + 1]
                assert confirmation["name"] == CONFIRM_STEP
                assert confirmation.get("if") == step.get("if")
        pins.add(steps[pin]["run"])
        confirmations.add(steps[names.index(CONFIRM_STEP)]["run"])
    # Both jobs run the same reviewed scripts.
    assert len(pins) == 1 and len(confirmations) == 1


def _step_script(name: str) -> str:
    steps = _workflow()["jobs"]["build-and-push"]["steps"]
    return next(step for step in steps if step.get("name") == name)["run"]


def _run_pin(cache: Path, docker_config: Path):
    environment = {
        "PATH": os.environ["PATH"],
        "UA_CI_CACHE": str(cache),
        "DOCKER_CONFIG": str(docker_config),
        "REGISTRY": "ghcr.io",
    }
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step_script(PIN_STEP)],
        env=environment,
        capture_output=True,
        text=True,
    )


def _job_folders(tmp_path: Path) -> tuple[Path, Path]:
    cache = tmp_path / "cache"
    docker_config = cache / "docker"
    docker_config.mkdir(parents=True)
    return cache, docker_config


def test_pin_writes_a_private_file_store_setting_once(tmp_path: Path) -> None:
    cache, docker_config = _job_folders(tmp_path)
    docker_config.chmod(0o755)

    result = _run_pin(cache, docker_config)

    assert result.returncode == 0, result.stderr
    settings = docker_config / "config.json"
    # An empty helper keeps Docker from choosing an installed system helper.
    assert json.loads(settings.read_text()) == {"credHelpers": {"ghcr.io": ""}}
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert stat.S_IMODE(docker_config.stat().st_mode) == 0o700
    # Settings something else already wrote are refused, not overwritten.
    assert _run_pin(cache, docker_config).returncode != 0


def test_pin_refuses_a_settings_folder_the_hooks_did_not_prepare(
    tmp_path: Path,
) -> None:
    cache, docker_config = _job_folders(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _run_pin(cache, elsewhere).returncode != 0

    docker_config.rmdir()
    docker_config.symlink_to(elsewhere)
    result = _run_pin(cache, docker_config)
    assert result.returncode != 0
    assert "private folder" in result.stdout
    assert not (elsewhere / "config.json").exists()

    linked_file = tmp_path / "linked"
    real_folder = cache / "docker"
    real_folder.unlink()
    real_folder.mkdir()
    (real_folder / "config.json").symlink_to(linked_file)
    assert _run_pin(cache, real_folder).returncode != 0
    assert not linked_file.exists()


def _confirm(docker_config: Path):
    environment = {
        "PATH": os.environ["PATH"],
        "DOCKER_CONFIG": str(docker_config),
        "REGISTRY": "ghcr.io",
    }
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step_script(CONFIRM_STEP)],
        env=environment,
        capture_output=True,
        text=True,
    )


def test_confirmation_requires_the_login_in_the_private_file(tmp_path: Path) -> None:
    cache, docker_config = _job_folders(tmp_path)
    assert _run_pin(cache, docker_config).returncode == 0
    settings = docker_config / "config.json"

    # A helper received the login: the file holds no credential for the registry.
    assert _confirm(docker_config).returncode != 0

    # What Docker's file store writes after a login (dummy value).
    stored = {
        "auths": {"ghcr.io": {"auth": "dXNlcjpkdW1teQ=="}},
        "credHelpers": {"ghcr.io": ""},
    }
    settings.write_text(json.dumps(stored))
    assert _confirm(docker_config).returncode == 0

    settings.chmod(0o644)
    assert _confirm(docker_config).returncode != 0

    settings.chmod(0o600)
    stored["credHelpers"] = {"ghcr.io": "osxkeychain"}
    settings.write_text(json.dumps(stored))
    assert _confirm(docker_config).returncode != 0


ATTESTATION_HOME_STEP = "Point the attestation at this job's registry login"


def _point_attestation(cache: Path, docker_config: Path, output: Path):
    script = next(
        step for step in _steps("publish-manifest") if step.get("name") == ATTESTATION_HOME_STEP
    )["run"]
    environment = {
        "PATH": os.environ["PATH"],
        "UA_CI_CACHE": str(cache),
        "DOCKER_CONFIG": str(docker_config),
        "GITHUB_OUTPUT": str(output),
    }
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
    )


def test_the_attestation_uses_this_jobs_private_registry_login() -> None:
    # v0.9.9 published its images but failed to attest them: actions/attest
    # reads logins only from $HOME/.docker/config.json, and the login lives in
    # the job's private DOCKER_CONFIG folder.
    steps = _steps("publish-manifest")
    names = [step.get("name") for step in steps]
    attestation = steps[names.index("Attest published image")]
    pointer = steps[names.index("Attest published image") - 1]

    assert pointer["name"] == ATTESTATION_HOME_STEP
    assert pointer["id"] == "attestation-home"
    assert pointer["if"] == attestation["if"] == ATTESTED
    assert attestation["env"] == {"HOME": "${{ steps.attestation-home.outputs.home }}"}
    assert attestation["with"]["push-to-registry"] == "true"


def test_the_attestation_home_holds_the_logged_in_settings_file(tmp_path: Path) -> None:
    cache, docker_config = _job_folders(tmp_path)
    assert _run_pin(cache, docker_config).returncode == 0
    output = tmp_path / "github_output"

    result = _point_attestation(cache, docker_config, output)

    assert result.returncode == 0, result.stdout + result.stderr
    home = Path(output.read_text().strip().removeprefix("home="))
    # The file the attestation reads is the job's own settings file, not a copy,
    # in a folder only this account can open, inside the job's own cache.
    assert home.parent == cache
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert (home / ".docker" / "config.json").resolve() == (docker_config / "config.json").resolve()
    # A second run in the same job is refused rather than reusing the folder.
    assert _point_attestation(cache, docker_config, output).returncode != 0


def test_the_attestation_home_refuses_settings_the_hooks_did_not_prepare(
    tmp_path: Path,
) -> None:
    cache, docker_config = _job_folders(tmp_path)
    output = tmp_path / "github_output"

    # No login settings written yet.
    assert _point_attestation(cache, docker_config, output).returncode != 0

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "config.json").write_text("{}")
    assert _point_attestation(cache, elsewhere, output).returncode != 0

    (docker_config / "config.json").symlink_to(elsewhere / "config.json")
    assert _point_attestation(cache, docker_config, output).returncode != 0
    assert not output.exists()


def _steps(job: str) -> list[dict]:
    return _workflow()["jobs"][job]["steps"]


def test_prerelease_tags_get_only_their_exact_version_image_tag() -> None:
    """Pre-release images never move ``latest``, ``X.Y`` or ``X``.

    docker/metadata-action (pinned dc80280, ``Meta.procSemver``) renders every
    ``type=semver`` pattern of a pre-release as ``{{version}}`` and sets latest
    only for a non-pre-release; its README states the same ("Pre-release (rc,
    beta, alpha) will only extend {{version}}"). That holds only while every
    version tag is ``type=semver`` with ``latest=auto`` and nothing else
    produces ``latest`` or a version-shaped tag, which this test checks.
    """
    for job in ("build-and-push", "publish-manifest"):
        metadata = next(s for s in _steps(job) if s.get("id") == "metadata")
        assert metadata["uses"] == "docker/metadata-action@dc802804100637a589fabce1cb79ff13a1411302"
        assert metadata["with"]["flavor"] == "latest=auto\n"
        lines = [line.strip() for line in metadata["with"]["tags"].splitlines() if line.strip()]
        semver = [line for line in lines if line.startswith("type=semver,")]
        assert semver == [
            "type=semver,pattern={{version}},value=${{ env.RELEASE_TAG }}",
            "type=semver,pattern={{major}}.{{minor}},value=${{ env.RELEASE_TAG }}",
            "type=semver,pattern={{major}},value=${{ env.RELEASE_TAG }}",
        ]
        others = [line for line in lines if line not in semver]
        assert all(line.startswith(("type=raw,", "type=sha,")) for line in others)
        assert not any("latest" in line for line in lines)
        assert not any("{{major}}" in line or "{{version}}" in line for line in others)


def test_publication_classifies_the_tag_with_the_workflows_own_tools() -> None:
    steps = _steps("publish-manifest")
    checkouts = [s for s in steps if s.get("uses", "").startswith("actions/checkout@")]
    tooling = next(s for s in steps if s.get("name") == "Check out release tooling")
    classify = next(s for s in steps if s.get("name") == "Classify release tag")

    assert checkouts[0]["with"]["ref"] == "${{ inputs.release_tag || github.ref }}"
    assert tooling["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    assert tooling["with"] == {
        "persist-credentials": "false",
        "ref": "${{ github.sha }}",
        "path": ".release-tooling",
        "sparse-checkout": "scripts",
    }
    assert classify["id"] == "release"
    assert classify["if"] == "env.RELEASE_TAG != ''"
    assert (
        'python3 .release-tooling/scripts/validate_release_tag.py --kind --version-file VERSION "$RELEASE_TAG"'
        in classify["run"]
    )
    assert steps.index(classify) < next(
        i for i, s in enumerate(steps) if s.get("name") == "Warn when private-release attestation is waived"
    )


def test_stable_releases_propose_the_launcher_compatibility_entry() -> None:
    steps = _steps("publish-manifest")
    attachment = next(s for s in steps if s.get("name") == "Attach verified container reference to release")
    propose = steps[-1]

    assert propose["name"] == "Propose launcher compatibility entry"
    assert steps.index(propose) > steps.index(attachment)
    # Not for pre-releases or untagged builds; build_only never runs this job.
    assert propose["if"] == "env.RELEASE_TAG != '' && steps.release.outputs.kind == 'stable'"
    assert "!inputs.build_only" in _workflow()["jobs"]["publish-manifest"]["if"]
    assert propose["env"] == {
        "GH_TOKEN": "${{ secrets.DESKTOP_COMPATIBILITY_TOKEN }}",
        "IMAGE": "${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}",
        "IMAGE_DIGEST": "${{ steps.merge.outputs.digest }}",
    }
    assert attachment["env"]["IMAGE_DIGEST"] == propose["env"]["IMAGE_DIGEST"]
    run = propose["run"]
    missing_secret = run.index('if [ -z "${GH_TOKEN}" ]; then')
    assert "::warning title=Launcher compatibility PR not opened::" in run
    assert "python3 scripts/propose_desktop_compatibility.py ${RELEASE_TAG}" in run
    assert run.index("exit 0") > missing_secret
    assert 'python3 .release-tooling/scripts/propose_desktop_compatibility.py "$RELEASE_TAG"' in run
    assert '--digest "$IMAGE_DIGEST"' in run
    assert '--image-repository "$IMAGE"' in run
    assert '--container-image-file "$UA_CI_CACHE/container-image.txt"' in run
    # The token is only tested for presence, never echoed or passed as an argument.
    assert run.count("${GH_TOKEN}") == 1
    assert "$GH_TOKEN" not in run


def test_nightly_dispatch_inputs_build_and_attach_to_the_nightly_release() -> None:
    # nightly.yml dispatches this workflow with only release_tag set.
    workflow = _workflow()
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["build_only"]["default"] == "false"
    assert inputs["image_tag"]["required"] == "false"
    assert workflow["env"]["RELEASE_TAG"] == (
        "${{ inputs.release_tag || (github.ref_type == 'tag' && github.ref_name) || '' }}"
    )

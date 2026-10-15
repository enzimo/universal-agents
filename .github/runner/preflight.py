"""Verify local tools and create disposable resources exercised by the hooks."""

import logging
import os
import platform
import subprocess
import sys
from pathlib import Path

LOG = logging.getLogger("ua.ci.preflight")


def run(*args):
    subprocess.run(["rtk", "proxy", *args], check=True, timeout=180)


def main():
    logging.basicConfig(level=logging.INFO)
    if os.environ.get("UA_CI_HOOK_VERSION") != "1":
        raise RuntimeError(
            "Install the reviewed runner hooks before assigning this label"
        )
    if sys.version_info < (3, 12):
        raise RuntimeError("System Python 3.12+ is required for the runner hooks")
    LOG.info("Native platform: %s %s", platform.system(), platform.machine())
    run("git", "--version")
    run("node", "-e", "if (+process.versions.node.split('.')[0] < 22) process.exit(1)")
    run("cargo", "+1.95.0", "--version")
    run("cargo", "+1.95.0", "clippy", "--version")
    run("rustup", "run", "1.95.0", "rustfmt", "--version")
    run(sys.executable, "-m", "unittest", "discover", "-s", ".github/runner", "-v")
    run(sys.executable, ".github/runner/cache_smoke.py")
    if platform.system() in {"Darwin", "Linux"}:
        run("gh", "--version")
        release_help = subprocess.check_output(
            ["rtk", "proxy", "gh", "release", "create", "--help"], text=True
        )
        if "--fail-on-no-commits" not in release_help:
            raise RuntimeError("Upgrade GitHub CLI for the existing release workflow")
    if platform.system() == "Darwin":
        run("xcrun", "--find", "clang")
        targets = subprocess.check_output(
            [
                "rtk",
                "proxy",
                "rustup",
                "target",
                "list",
                "--installed",
                "--toolchain",
                "1.95.0",
            ],
            text=True,
        )
        for target in ("aarch64-apple-darwin", "x86_64-apple-darwin"):
            if target not in targets.splitlines():
                raise RuntimeError(f"Missing universal app target: {target}")
    if platform.system() == "Linux":
        run("pkg-config", "--modversion", "webkit2gtk-4.1")
    if platform.system() in {"Linux", "Darwin"}:
        cache = Path(os.environ["UA_CI_CACHE"])
        (cache / "docker-used").touch()
        run("docker", "info", "--format", "{{.OSType}} {{.Architecture}}")
        builder = os.environ["UA_CI_BUILDER"]
        job_id = os.environ["UA_CI_ID"]
        context = cache / "docker-smoke"
        context.mkdir()
        (context / "Dockerfile").write_text(
            'FROM busybox:1.37\nRUN echo UA_CI_OK > /marker\nCMD ["cat", "/marker"]\n'
        )
        run(
            "docker",
            "buildx",
            "create",
            "--name",
            builder,
            "--driver",
            "docker-container",
        )
        architectures = ("amd64", "arm64") if platform.system() == "Linux" else ("arm64",)
        for architecture in architectures:
            run(
                "docker",
                "buildx",
                "build",
                "--builder",
                builder,
                "--platform",
                f"linux/{architecture}",
                "--load",
                "--tag",
                f"ua-ci-smoke:{job_id}-{architecture}",
                "--label",
                f"com.universalagents.ci.job={job_id}",
                str(context),
            )
            run(
                "docker",
                "run",
                "--rm",
                "--platform",
                f"linux/{architecture}",
                "--label",
                f"com.universalagents.ci.job={job_id}",
                f"ua-ci-smoke:{job_id}-{architecture}",
            )
        # Leave the image and builder for the completed hook to reclaim.


if __name__ == "__main__":
    main()

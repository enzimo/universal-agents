"""Exercise installed hooks in a temporary runner, including real Docker on Linux.

This does not register an Actions runner or use GitHub credentials. Run this
with system Python and the same PATH that the runner account will use.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    logging.basicConfig(level=logging.INFO)
    source = Path(__file__).resolve().parent
    original_env = os.environ.copy()
    original_cwd = Path.cwd()
    # macOS's per-user temporary path can exceed its Unix socket path limit.
    root = Path(tempfile.mkdtemp(
        prefix="ua-ci-hook-test-", dir="/tmp" if sys.platform == "darwin" else None,
    ))
    try:
        runner = root / "runner"
        work = runner / "_work"
        work.mkdir(parents=True)
        (runner / ".runner").write_text(json.dumps({"workFolder": "_work"}))
        destination = root / "hooks"
        reserve_args = (
            ["--reserve-path", "/mnt/c"] if os.environ.get("WSL_DISTRO_NAME") else []
        )
        subprocess.run(
            [
                "rtk",
                "proxy",
                sys.executable,
                str(source / "install_hooks.py"),
                "--sccache",
                os.environ.get("RUSTC_WRAPPER") or shutil.which("sccache") or "sccache",
                "--runner",
                str(runner),
                "--destination",
                str(destination),
                "--repository",
                "enzimo/universal-agents-desktop",
                "--public-repository",
                "enzimo/universal-agents-desktop",
            ]
            + reserve_args,
            check=True,
        )
        for line in (runner / ".env").read_text().splitlines():
            key, value = line.split("=", 1)
            os.environ[key] = value
        workspace = work / "universal-agents-desktop" / "universal-agents-desktop"
        event = root / "event.json"
        event.write_text(json.dumps({"repository": {
            "private": False, "full_name": "enzimo/universal-agents-desktop",
        }}))
        env_file = root / "env"
        path_file = root / "path"
        os.environ.update(
            {
                "GITHUB_REPOSITORY": "enzimo/universal-agents-desktop",
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_WORKSPACE": str(workspace),
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_RUN_ID": "1",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_JOB": "preflight",
                "RUNNER_NAME": "temporary-validation",
                "GITHUB_ENV": str(env_file),
                "GITHUB_PATH": str(path_file),
            }
        )

        def hook(operation):
            extension = "ps1" if os.name == "nt" else "sh"
            wrapper = str(destination / f"{operation}.{extension}")
            command = (
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    ". '" + wrapper.replace("'", "''") + "'",
                ]
                if os.name == "nt"
                else ["bash", wrapper]
            )
            subprocess.run(
                ["rtk", "proxy", *command], check=True, timeout=240,
                # Windows keeps the RTK parent's working directory open even
                # after the PowerShell wrapper moves its own directory.
                cwd=root,
            )

        try:
            hook("begin")
            for line in env_file.read_text().splitlines():
                key, value = line.split("=", 1)
                os.environ[key] = value
            os.environ["PATH"] = (
                os.pathsep.join(path_file.read_text().splitlines())
                + os.pathsep
                + os.environ["PATH"]
            )
            shutil.copytree(
                source,
                workspace / ".github" / "runner",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            # Runner tests also cover the sibling monitoring/promotion helpers.
            shutil.copytree(source.parent / "scripts", workspace / ".github" / "scripts")
            os.chdir(workspace)
            subprocess.run(
                ["rtk", "proxy", sys.executable, ".github/runner/preflight.py"],
                check=True,
                timeout=900,
            )
        finally:
            os.chdir(root)
            try:
                hook("cleanup")
            finally:
                os.environ.clear()
                os.environ.update(original_env)
                os.chdir(original_cwd)
        assert not workspace.exists(), "Checkout was not reclaimed"
        assert not (destination / "state" / "cache").exists(), (
            "Caches were not reclaimed"
        )
        assert not (destination / "state" / "job.json").exists(), (
            "Manifest was not cleared"
        )
        logging.getLogger("ua.ci").info("Installed hook preflight and cleanup passed")
    except Exception:
        logging.getLogger("ua.ci").exception(
            "Validation failed; retained evidence at %s", root
        )
        raise
    else:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()

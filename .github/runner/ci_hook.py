"""Installed runner hooks. One private state directory per runner process.

Install a reviewed copy outside the checkout and runner application directory.
The completed hook runs after action post steps; the started hook retries cleanup
after a crash. Never share a work directory or state directory between runners.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import build_cache

LOG = logging.getLogger("ua.ci")
LABEL = "com.universalagents.ci.job"
VERSION = "1"


def run(*args: str, cwd: str | None = None) -> str:
    try:
        return subprocess.run(
            ["rtk", "proxy", *args],
            check=True,
            text=True,
            capture_output=True,
            timeout=120,
            cwd=cwd,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        LOG.error("Cleanup command failed: %s", error.stderr.strip())
        raise


def child(path: Path, parent: Path) -> Path:
    """Reject escapes, including symlinks/junctions, before recursive deletion."""
    resolved = path.resolve()
    if resolved == parent.resolve() or not resolved.is_relative_to(parent.resolve()):
        raise ValueError(f"Path escapes CI directory: {path}")
    return resolved


def remove(path: Path) -> None:
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        # Cargo source trees can exceed MAX_PATH even when the runner root is
        # short. Use extended paths without changing the machine's registry.
        absolute = str(path.absolute())
        path = Path(
            "\\\\?\\UNC\\" + absolute[2:]
            if absolute.startswith("\\\\")
            else "\\\\?\\" + absolute
        )
    if path.is_symlink():
        path.unlink()
    elif path.exists():

        def writable(func, name, _error):
            os.chmod(name, 0o700)
            func(name)

        shutil.rmtree(path, onerror=writable)


def cleanup(config: dict, *, before_job: bool = False) -> None:
    state = Path(config["state"])
    manifest = state / "job.json"
    if not manifest.exists():
        return
    job = json.loads(manifest.read_text())
    workspace = child(Path(job["workspace"]), Path(config["work"]))
    if workspace.parent.name != workspace.name or workspace.name not in {
        repo.split("/")[1] for repo in config["repositories"]
    }:
        raise ValueError("Refusing to clean an unexpected checkout")
    job_id = job["id"]
    if len(job_id) != 24 or any(c not in "0123456789abcdef" for c in job_id):
        raise ValueError("Invalid CI job identity")
    if job.get("compiler_env"):
        build_cache.stop(job["compiler_env"])
    # An intent file is written before the first Docker mutation. A broken daemon
    # fails cleanup and preserves the manifest, blocking the next job until fixed.
    if (state / "cache" / "docker-used").exists():

        def docker(*args):
            return run("docker", "--config", str(state / "cache" / "docker"), *args)

        selector = f"label={LABEL}={job_id}"
        for container in docker("ps", "-aq", "--filter", selector).split():
            docker("rm", "--force", "--volumes", container)
        for image in set(docker("image", "ls", "-q", "--filter", selector).split()):
            docker("image", "rm", image)
        # Buildx has already run its post hook in the normal completion path.
        # After interruption, remove only the builder recorded for this job.
        builders = docker("buildx", "ls", "--format", "{{.Name}}")
        if f"ua-ci-{job_id}" in builders.splitlines():
            docker("buildx", "rm", "--force", f"ua-ci-{job_id}")
        if config.get("colima_profile"):
            run("colima", "--profile", config["colima_profile"], "ssh", "--",
                "sudo", "fstrim", "/var/lib/docker", cwd="/")
    if os.name == "nt":
        script = (
            f"$tasks = Get-ScheduledTask -TaskName 'UA-CI-{job_id}-*' "
            "-ErrorAction SilentlyContinue; $tasks | Stop-ScheduledTask; "
            "$tasks | Unregister-ScheduledTask -Confirm:$false"
        )
        run("powershell", "-NoProfile", "-Command", script)
    LOG.info("Reclaiming job %s: %s", job_id, workspace)
    # Never remove the current directory of the hook on Windows.
    os.chdir(state)
    remove(workspace)
    remove(child(state / "cache", state))
    # Each runner owns its work directory. Hooks use system Python/RTK, so
    # setup-action runtimes and downloaded action versions can now be removed.
    work = Path(config["work"])
    # Actions for the incoming job can already be downloaded when its started
    # hook runs. Recovery must not delete those newly prepared action files.
    for directory in ("_tool",) if before_job else ("_tool", "_actions"):
        remove(child(work / directory, work))
    if config.get("build_cache"):
        build_cache.prune(config, remove)
    manifest.unlink()


def begin(config: dict) -> None:
    state = Path(config["state"])
    state.mkdir(parents=True, exist_ok=True)
    cleanup(config, before_job=True)
    repository = os.environ["GITHUB_REPOSITORY"]
    if repository not in config["repositories"]:
        raise ValueError(f"Runner is not configured for {repository}")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if not event.get("repository", {}).get("private"):
        raise ValueError("These local runners accept private repositories only")
    if (
        "pull_request" in event
        and event["pull_request"]["head"]["repo"]["full_name"] != repository
    ):
        raise ValueError("Fork PRs must not execute on personal build hosts")
    workspace = child(Path(os.environ["GITHUB_WORKSPACE"]), Path(config["work"]))
    name = repository.split("/")[1]
    if workspace != Path(config["work"]).resolve() / name / name:
        raise ValueError("Unexpected checkout layout")
    if config.get("build_cache"):
        build_cache.prune(config, remove)
    reserve_paths = [state, Path(config["work"])]
    reserve_paths.extend(Path(path) for path in config.get("reserve_paths", []))
    for path in reserve_paths:
        if shutil.disk_usage(path).free < config.get("min_free_gib", 30) * 1024**3:
            raise RuntimeError(f"Less than required free disk space at {path}")
    identity = (
        "-".join(
            os.environ[key]
            for key in (
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "GITHUB_JOB",
                "RUNNER_NAME",
            )
        )
        + f"-{time.time_ns()}"
    )
    job_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
    # Record ownership before creating anything that requires reclamation.
    (state / "job.json").write_text(
        json.dumps({"id": job_id, "workspace": str(workspace)})
    )
    # Recovery may remove the directory that GitHub prepared for this job.
    # Every step, including the pre-checkout guard, starts with it as cwd.
    workspace.mkdir(parents=True, exist_ok=True)
    cache = state / "cache"
    cache.mkdir()
    for directory in ("cargo", "npm", "pip", "xdg", "tmp", "docker"):
        (cache / directory).mkdir()
    env = {
        "UA_CI_HOOK_VERSION": VERSION,
        "UA_CI_PYTHON": sys.executable,
        "UA_CI_ID": job_id,
        "UA_CI_CACHE": str(cache),
        "UA_CI_BUILDER": f"ua-ci-{job_id}",
        "NATS_DOCKER_CONTAINER": f"ua-ci-{job_id}-nats",
        "UA_CI_POSTGRES": f"ua-ci-{job_id}-postgres",
        "CARGO_HOME": str(cache / "cargo"),
        "RUSTUP_TOOLCHAIN": "1.95.0",
        "CARGO_INCREMENTAL": "0",
        "CARGO_BUILD_JOBS": "2",
        "npm_config_cache": str(cache / "npm"),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
        "DOCKER_CONFIG": str(cache / "docker"),
        "TMPDIR": str(cache / "tmp"),
        "TMP": str(cache / "tmp"),
        "TEMP": str(cache / "tmp"),
        "RTK_TEE_DIR": str(cache / "rtk-tee"),
    }
    if config.get("build_cache"):
        compiler_env = build_cache.prepare(config)
        if build_cache.listening(compiler_env):
            raise RuntimeError("Dedicated CI compiler-cache endpoint is already occupied")
        # Persist server ownership before startup so interrupted jobs recover.
        (state / "job.json").write_text(json.dumps({
            "id": job_id, "workspace": str(workspace), "compiler_env": compiler_env,
        }))
        build_cache.start(compiler_env)
        env.update(compiler_env)
    with Path(os.environ["GITHUB_ENV"]).open("a") as stream:
        stream.writelines(f"{key}={value}\n" for key, value in env.items())
    with Path(os.environ["GITHUB_PATH"]).open("a") as stream:
        stream.write(str(cache / "cargo" / "bin") + "\n")
    # Runner diagnostics are bounded by age. Toolchains are provisioned with
    # fixed versions and managed separately from disposable job data.
    diagnostics = Path(config["runner"]) / "_diag"
    for log in diagnostics.glob("*.log"):
        if log.is_file() and log.stat().st_mtime < time.time() - 7 * 86400:
            log.unlink()
    LOG.info("Prepared isolated CI job %s", job_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("begin", "cleanup"))
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    state = Path(config["state"]).resolve()
    work = Path(config["work"]).resolve()
    if state == work or state.is_relative_to(work) or work.is_relative_to(state):
        raise ValueError("State and work directories must be separate")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    (begin if args.operation == "begin" else cleanup)(config)


if __name__ == "__main__":
    main()

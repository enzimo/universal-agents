"""Install reviewed hooks on an idle, dedicated runner; restart it afterward."""

import argparse
import json
import os
import shlex
import shutil
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--repository", action="append", required=True)
    parser.add_argument(
        "--public-repository", action="append", default=[],
        help="Explicitly approve a public repository already named by --repository",
    )
    parser.add_argument(
        "--reserve-path",
        action="append",
        default=[],
        type=Path,
        help="Also check this filesystem's free space; required for the Windows VHD drive on WSL",
    )
    parser.add_argument("--docker-host", help="Docker endpoint for this runner process")
    parser.add_argument("--colima-profile", help="Dedicated CI profile to trim after Docker cleanup")
    parser.add_argument("--sccache", required=True, type=Path, help="Provisioned sccache 0.17.0 binary")
    parser.add_argument("--cache-gib", type=int, default=20, help="Total retained cache budget per runner")
    parser.add_argument("--cache-days", type=int, default=7, help="Expire scopes unused for this many days")
    parser.add_argument("--cache-server-port", type=int, default=48227, help="Dedicated Windows sccache port")
    args = parser.parse_args()
    if not set(args.public_repository).issubset(args.repository):
        parser.error("Every public repository must also be named by --repository")
    if not args.sccache.is_file() or args.cache_gib < 1 or args.cache_days < 1:
        parser.error("Provide an installed sccache binary and positive cache limits")
    if not 1024 <= args.cache_server_port <= 65535:
        parser.error("Cache server port must be between 1024 and 65535")
    if args.colima_profile == "default":
        parser.error("Use a dedicated Colima CI profile, never the default profile")
    if os.environ.get("WSL_DISTRO_NAME") and not args.reserve_path:
        parser.error(
            "On WSL, pass --reserve-path for the Windows drive holding the VHD, such as /mnt/c"
        )
    for path in args.reserve_path:
        if not path.is_dir():
            parser.error(f"Reserve path does not exist: {path}")
    runner = args.runner.resolve()
    destination = args.destination.resolve()
    if destination.is_relative_to(runner) or runner.is_relative_to(destination):
        parser.error("Hooks must be outside the runner application directory")
    registration = json.loads((runner / ".runner").read_text(encoding="utf-8-sig"))
    work = (runner / registration["workFolder"]).resolve()
    if destination.is_relative_to(work) or work.is_relative_to(destination):
        parser.error("Hooks must be outside the work directory")
    if Path(sys.executable).resolve().is_relative_to(work):
        parser.error(
            "Run the installer with system Python, outside the disposable work directory"
        )
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("ci_hook.py"), destination / "ci_hook.py")
    shutil.copyfile(Path(__file__).with_name("build_cache.py"), destination / "build_cache.py")
    config = destination / "config.json"
    config.write_text(
        json.dumps(
            {
                "runner": str(runner),
                "work": str(work),
                "state": str(destination / "state"),
                "repositories": args.repository,
                "public_repositories": args.public_repository,
                "min_free_gib": 30,
                "colima_profile": args.colima_profile,
                "reserve_paths": [str(path.resolve()) for path in args.reserve_path],
                "build_cache": {
                    "sccache": str(args.sccache.resolve()),
                    "max_bytes": args.cache_gib * 1024**3,
                    "max_age_days": args.cache_days,
                    "server_port": args.cache_server_port,
                },
            },
            indent=2,
        )
        + "\n"
    )
    rtk = shutil.which("rtk")
    if not rtk:
        parser.error("Install RTK and add it to the runner PATH first")
    # The runner's hook shell does not add -ExecutionPolicy. Limit the override
    # to this runner and its children instead of changing account-wide policy.
    settings = {"PSExecutionPolicyPreference": "Bypass"} if sys.platform == "win32" else {}
    if args.docker_host:
        settings["DOCKER_HOST"] = args.docker_host
    for operation, variable in (("begin", "STARTED"), ("cleanup", "COMPLETED")):
        command = [
            rtk,
            "proxy",
            sys.executable,
            str(destination / "ci_hook.py"),
            operation,
            str(config),
        ]
        if sys.platform == "win32":
            wrapper = destination / f"{operation}.ps1"
            quoted_destination = "'" + str(destination).replace("'", "''") + "'"
            body = (
                "$ErrorActionPreference = 'Stop'\n"
                f"[Environment]::CurrentDirectory = {quoted_destination}\n"
                f"Set-Location -LiteralPath {quoted_destination}\n& "
            ) + " ".join(
                "'" + arg.replace("'", "''") + "'" for arg in command
            )
            body += "\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
        else:
            wrapper = destination / f"{operation}.sh"
            body = "#!/bin/sh\nexec " + shlex.join(command) + "\n"
        wrapper.write_text(body)
        wrapper.chmod(0o700)
        settings[f"ACTIONS_RUNNER_HOOK_JOB_{variable}"] = str(wrapper)
    env_file = runner / ".env"
    existing = env_file.read_text() if env_file.exists() else ""
    lines = [
        line for line in existing.splitlines() if line.split("=", 1)[0] not in settings
    ]
    lines.extend(f"{key}={value}" for key, value in settings.items())
    env_file.write_text("\n".join(lines) + "\n")
    print("Installed hooks. Restart the idle runner, then run Local runner preflight.")


if __name__ == "__main__":
    main()

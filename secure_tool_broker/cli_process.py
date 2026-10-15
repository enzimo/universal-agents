"""Private CLI environment and redacted process failures shared by broker providers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

class CliFailure(Exception):
    """Carry a safe diagnostic without reflecting upstream stdout or secrets."""

    def __init__(self, code: str, *, write: bool = False) -> None:
        self.code = code
        self.write = write
        super().__init__(code)


def child_environment(home: Path) -> dict[str, str]:
    """An allowlist prevents ambient provider tokens, proxies and plugins crossing in."""
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
        if key in os.environ
    }
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "HF_HOME": str(home / "hf"),
            "HF_HUB_DISABLE_UPDATE_CHECK": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_DISABLE_PROGRESS_BARS": "1",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
            "NO_COLOR": "1",
        }
    )
    return env


def run_cli(command: list[str], env: dict[str, str], *, write: bool = False) -> str:
    """No shell, interactive stdin, command logging, or automatic write retries."""
    try:
        result = subprocess.run(
            command,
            env=env,
            cwd=env["HOME"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CliFailure(
            "outcome_unknown" if write else "timeout", write=write
        ) from None
    if result.returncode:
        # X sometimes exits successfully with an HTTP error JSON body; callers
        # also validate the expected response. Never reflect token-bearing output.
        output = (result.stderr + result.stdout).lower()
        code = "provider_error"
        if "401" in output or "unauthorized" in output:
            code = "auth_required"
        elif "403" in output or "forbidden" in output:
            code = "permission_denied"
        elif "429" in output:
            code = "rate_limited"
        elif write:
            code = "outcome_unknown"
        raise CliFailure(code, write=write)
    return result.stdout

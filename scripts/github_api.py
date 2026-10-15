"""GitHub REST calls through the authenticated ``gh`` command, for release tools.

``gh`` reads its token from ``GH_TOKEN``; nothing here reads or prints it.
Tests replace ``GhCliApi`` with an in-memory fake that has the same
``request`` method.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable, Protocol


HTTP_STATUS_PATTERN = re.compile(r"\(HTTP (?P<status>\d{3})\)")


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None) -> None:
        super().__init__(message)
        self.status = status


class GitHubApi(Protocol):
    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """Call a GitHub REST path; raise ``GitHubError`` on an error status."""


class GhCliApi:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        self._runner = runner

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        command = ["gh", "api", "--method", method, path]
        if body is not None:
            command += ["--input", "-"]
        result = self._runner(
            command,
            input=json.dumps(body) if body is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            status = HTTP_STATUS_PATTERN.search(message)
            raise GitHubError(
                f"{method} {path} failed: {message}",
                int(status["status"]) if status else None,
            )
        return json.loads(result.stdout) if result.stdout.strip() else None


def get_optional(api: GitHubApi, path: str) -> Any | None:
    """``GET path``, or ``None`` when GitHub answers 404."""

    try:
        return api.request("GET", path)
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise

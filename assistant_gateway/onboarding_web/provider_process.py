"""Own the lifetime of onboarding's blocking model connection checks.

The shared checks also serve synchronous callers and sign-in providers. A
thread cannot cancel their blocking HTTP requests, and asyncio.run waits for
its executor threads before exiting. Each onboarding check therefore runs in
one child process. Keys travel as typed JSON on stdin, never in arguments or
files. On web shutdown the service cancels its checks and reaps their children
before aiohttp waits for request handlers, within Docker's default stop grace.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from assistant_gateway.onboarding_web.provider_test import ProviderTestResult, test_provider_connection

LOGGER = logging.getLogger(__name__)
STOP_GRACE_SECONDS = 1.0
MAX_REQUEST_BYTES = 512 * 1024
MAX_ACTIVE_CHECKS = 2
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ProviderCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    endpoint: str
    token: str = Field(repr=False)
    model: str
    reasoning_effort: str = ""
    reasoning_summary: str | None = None
    litellm_api_mode: str = "chat_completions"
    stream: bool | None = None
    xai_oauth: dict[str, Any] | None = Field(default=None, repr=False)
    instance_root: str | None = None


class ProviderChecksBusy(RuntimeError):
    """The onboarding page already has its maximum number of live checks."""


class ProviderChecks:
    def __init__(self) -> None:
        self._pending: set[asyncio.Task[ProviderTestResult]] = set()
        self._stopping = False

    async def run(self, **kwargs: Any) -> ProviderTestResult:
        if self._stopping:
            raise asyncio.CancelledError
        # Onboarding is reachable before sign-in. Slow requests must not be
        # able to start an unbounded number of Python processes.
        if len(self._pending) >= MAX_ACTIVE_CHECKS:
            LOGGER.warning(
                "The onboarding page is already checking its maximum number of providers. "
                "event=onboarding_provider_process_limit active_checks=%s",
                len(self._pending),
            )
            raise ProviderChecksBusy(f"{MAX_ACTIVE_CHECKS} model connection checks are already running. Wait for one to finish.")
        task = asyncio.create_task(run_provider_check(ProviderCheck(**kwargs)))
        self._pending.add(task)
        try:
            return await task
        finally:
            self._pending.discard(task)

    async def close(self, _app: object) -> None:
        self._stopping = True
        pending = tuple(self._pending)
        for task in pending:
            # A request may already be canceling and reaping its child. A second
            # cancellation must not interrupt that cleanup.
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def run_provider_check(check: ProviderCheck) -> ProviderTestResult:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "assistant_gateway.onboarding_web.provider_process",
        cwd=PROJECT_ROOT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await process.communicate(check.model_dump_json().encode("utf-8"))
        if process.returncode != 0:
            LOGGER.error(
                "The model connection check process failed. event=onboarding_provider_process_failed pid=%s exit_code=%s",
                process.pid,
                process.returncode,
            )
            return ProviderTestResult(ok=False, provider=check.provider, message="The model connection check stopped unexpectedly.")
        try:
            return TypeAdapter(ProviderTestResult).validate_json(stdout)
        except ValidationError:
            # A malformed reply could contain a key, so never log its bytes or
            # the validation exception's copy of the input.
            LOGGER.error(
                "The model connection check returned an invalid response. "
                "event=onboarding_provider_process_invalid_response pid=%s",
                process.pid,
            )
            return ProviderTestResult(ok=False, provider=check.provider, message="The model connection check returned an invalid response.")
    finally:
        await _stop_process(process)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=STOP_GRACE_SECONDS)
    except TimeoutError:
        LOGGER.warning(
            "The model connection check did not stop on SIGTERM; killing it. "
            "event=onboarding_provider_process_killed pid=%s",
            process.pid,
        )
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
    LOGGER.info("Stopped the model connection check. event=onboarding_provider_process_stopped pid=%s", process.pid)


def _main() -> None:
    check = ProviderCheck.model_validate_json(sys.stdin.buffer.read(MAX_REQUEST_BYTES))
    try:
        result = test_provider_connection(**check.model_dump())
    except Exception as exc:  # noqa: BLE001 - unexpected errors cross the private pipe without credentials
        result = ProviderTestResult(
            ok=False, provider=check.provider,
            message=f"The model connection check failed unexpectedly ({type(exc).__name__}).",
        )
    # stdout is the private response pipe, not a runtime log.
    sys.stdout.write(json.dumps(asdict(result)))


if __name__ == "__main__":
    _main()

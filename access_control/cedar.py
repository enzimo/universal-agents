"""Client for the access-model Cedar evaluator (``ua-cedar-authorizer serve-access``).

One long-lived process per client, one JSON request per line. Every failure
(missing binary, timeout, protocol mismatch, evaluation error) is a denial:
callers get ``AccessEvaluationError`` and must refuse the operation.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

ACCESS_PROTOCOL_VERSION = 3
AUTHORIZER_BINARY = "ua-cedar-authorizer"
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = _REPO_ROOT / "ua_definitions" / "access" / "cedar" / "schema.cedarschema"
DEFAULT_POLICY_PATH = _REPO_ROOT / "ua_definitions" / "access" / "cedar" / "policies.cedar"


class AccessEvaluationError(RuntimeError):
    """The evaluator could not decide; the operation must be refused."""


@dataclass(frozen=True)
class CedarDecision:
    allowed: bool
    determining_policy_ids: tuple[str, ...]
    errors: tuple[str, ...]


def _resolve_binary(explicit: str | None) -> str:
    candidates: list[str | None] = [explicit] if explicit else []
    candidates += [
        os.environ.get("UA_CEDAR_AUTHORIZER_BINARY"),
        shutil.which(AUTHORIZER_BINARY),
        str(_REPO_ROOT / "target" / "release" / AUTHORIZER_BINARY),
        str(_REPO_ROOT / "target" / "debug" / AUTHORIZER_BINARY),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise AccessEvaluationError(
        f"{AUTHORIZER_BINARY} is not installed; build it with cargo build --workspace."
    )


@dataclass
class CedarAccessClient:
    """Thread-safe client; restarts the evaluator after any failure."""

    binary: str | None = None
    schema_path: Path = DEFAULT_SCHEMA_PATH
    policy_path: Path = DEFAULT_POLICY_PATH
    timeout_seconds: float = 2.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _process: subprocess.Popen[str] | None = field(default=None, repr=False)
    _lines: "queue.Queue[str | None]" = field(default_factory=queue.Queue, repr=False)
    _counter: int = 0
    policy_digest: str = ""

    def __post_init__(self) -> None:
        atexit.register(self.close)

    def decide(
        self,
        *,
        links: Sequence[Mapping[str, Any]],
        entities: Sequence[Mapping[str, Any]],
        principal: Mapping[str, str],
        action: str,
        resource: Mapping[str, str],
        context: Mapping[str, Any] | None = None,
        extra_policies: str | None = None,
    ) -> CedarDecision:
        with self._lock:
            self._ensure_started()
            self._counter += 1
            request_id = f"access-{os.getpid()}-{self._counter}"
            payload: dict[str, Any] = {
                "request_id": request_id,
                "links": list(links),
                "entities": list(entities),
                "principal": dict(principal),
                "action": action,
                "resource": dict(resource),
                "context": dict(context or {}),
            }
            if extra_policies:
                payload["extra_policies"] = extra_policies
            response = self._exchange(json.dumps(payload, separators=(",", ":")))
            if response.get("request_id") != request_id or response.get("protocol_version") != ACCESS_PROTOCOL_VERSION:
                self._stop()
                raise AccessEvaluationError("The access evaluator returned a mismatched response.")
            if not response.get("ok") and response.get("error_code"):
                raise AccessEvaluationError(
                    f"{response.get('error_code')}: {response.get('error_message') or 'evaluation failed'}"
                )
            return CedarDecision(
                allowed=bool(response.get("allowed")) and not response.get("errors"),
                determining_policy_ids=tuple(response.get("determining_policy_ids") or ()),
                errors=tuple(response.get("errors") or ()),
            )

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._stop()
        command = [
            _resolve_binary(self.binary),
            "serve-access",
            "--policy",
            str(self.policy_path),
            "--schema",
            str(self.schema_path),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise AccessEvaluationError(f"The access evaluator failed to start: {exc}") from exc
        self._process = process
        self._lines = queue.Queue()
        threading.Thread(target=self._read_stdout, args=(process, self._lines), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(process,), daemon=True).start()
        ready = self._next(process)
        if ready.get("message_type") != "access_ready" or ready.get("protocol_version") != ACCESS_PROTOCOL_VERSION:
            self._stop()
            raise AccessEvaluationError("The access evaluator did not start correctly.")
        self.policy_digest = str(ready.get("policy_digest") or "")

    def _exchange(self, line: str) -> Mapping[str, Any]:
        process = self._process
        if process is None or process.stdin is None:
            raise AccessEvaluationError("The access evaluator is not running.")
        try:
            process.stdin.write(line + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._stop()
            raise AccessEvaluationError(f"The access evaluator request failed: {exc}") from exc
        return self._next(process)

    def _next(self, process: subprocess.Popen[str]) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.timeout_seconds
        try:
            raw = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty as exc:
            self._stop()
            raise AccessEvaluationError(f"The access evaluator timed out after {self.timeout_seconds}s.") from exc
        if raw is None:
            self._stop()
            raise AccessEvaluationError("The access evaluator exited.")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._stop()
            raise AccessEvaluationError("The access evaluator returned invalid JSON.") from exc
        if not isinstance(value, dict):
            self._stop()
            raise AccessEvaluationError("The access evaluator returned an unexpected response.")
        return value

    @staticmethod
    def _read_stdout(process: subprocess.Popen[str], lines: "queue.Queue[str | None]") -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    @staticmethod
    def _read_stderr(process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            logger.warning("Access evaluator: %s event=access_evaluator_stderr", line.rstrip()[:500])

    def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


__all__ = ["AccessEvaluationError", "CedarAccessClient", "CedarDecision"]

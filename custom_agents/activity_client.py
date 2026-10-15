"""Non-blocking HTTP client for curated TaskActivityEvent submission."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import Deque, Optional

import aiohttp

from custom_agents.otel_runtime import current_trace_context
from custom_messages.contracts.activity_v1 import TaskActivityEvent
from env_secrets import get_env_with_file_fallback


@dataclass(frozen=True)
class ActivityCollectorClientSettings:
    base_url: Optional[str]
    connect_timeout_seconds: float = 1.0
    request_timeout_seconds: float = 2.0
    batch_max_events: int = 25
    flush_interval_seconds: float = 1.0
    max_buffered_events: int = 500
    drop_when_unavailable: bool = True
    ingest_token_env_var: str = "ACTIVITY_COLLECTOR_INGEST_TOKEN"
    warning_interval_seconds: float = 30.0

    @property
    def enabled(self) -> bool:
        return bool(str(self.base_url or "").strip())


class ActivityCollectorClient:
    """Best-effort async batching client that never blocks task execution."""

    def __init__(
        self,
        settings: ActivityCollectorClientSettings,
        *,
        logger: logging.Logger,
    ) -> None:
        self.settings = settings
        self._logger = logger
        self._buffer: Deque[TaskActivityEvent] = deque()
        self._sequence = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._ingest_token: Optional[str] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._periodic_task: Optional[asyncio.Task] = None
        self._last_warning_at: Optional[float] = None
        self._dropped_events = 0
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    async def start(self) -> None:
        if not self.enabled or self._closed:
            return
        if self._loop is not None:
            return
        self._ingest_token = self._resolve_ingest_token()
        if not self._ingest_token:
            self._log_warning(
                "Activity collector submission is enabled, but the configured ingest token "
                f"env var {self.settings.ingest_token_env_var!r} is not set; disabling submissions."
            )
            return
        self._loop = asyncio.get_running_loop()
        timeout = aiohttp.ClientTimeout(
            total=self.settings.request_timeout_seconds,
            connect=self.settings.connect_timeout_seconds,
        )
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._periodic_task = asyncio.create_task(
            self._periodic_flush_loop(),
            name="activity_collector_periodic_flush",
        )

    async def close(self) -> None:
        self._closed = True
        if self._periodic_task is not None:
            self._periodic_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._periodic_task
        self._periodic_task = None
        if self._flush_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
        self._flush_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._ingest_token = None

    def emit(self, event: TaskActivityEvent) -> None:
        if not self.enabled or self._closed:
            return
        trace_id, span_id = current_trace_context()
        if not event.trace_id:
            event.trace_id = trace_id
        if not event.span_id:
            event.span_id = span_id
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._enqueue_event, event)

    def _enqueue_event(self, event: TaskActivityEvent) -> None:
        if self._closed:
            return
        if len(self._buffer) >= self.settings.max_buffered_events:
            self._dropped_events += 1
            self._log_warning(
                "Dropping activity event because the collector buffer is full "
                f"(max_buffered_events={self.settings.max_buffered_events}, dropped={self._dropped_events})."
            )
            return
        self._buffer.append(event)
        if len(self._buffer) >= self.settings.batch_max_events:
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = asyncio.create_task(self._flush_once(), name="activity_collector_flush")

    async def _periodic_flush_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.settings.flush_interval_seconds)
            if self._buffer:
                self._schedule_flush()

    async def _flush_once(self) -> None:
        if self._session is None or not self._buffer or not self._ingest_token:
            return
        batch: list[TaskActivityEvent] = []
        while self._buffer and len(batch) < self.settings.batch_max_events:
            batch.append(self._buffer.popleft())
        if not batch:
            return
        payload = [event.model_dump(mode="json") for event in batch]
        try:
            async with self._session.post(
                f"{self.settings.base_url}/api/activity/batch",
                json={"events": payload},
                headers={"Authorization": f"Bearer {self._ingest_token}"},
            ) as response:
                if response.status >= 300:
                    raise RuntimeError(f"collector returned HTTP {response.status}")
        except Exception as exc:
            if not self.settings.drop_when_unavailable:
                while batch and len(self._buffer) < self.settings.max_buffered_events:
                    self._buffer.appendleft(batch.pop())
                if batch:
                    self._dropped_events += len(batch)
                self._log_warning(
                    "Activity collector submission failed; re-queued buffered events where possible "
                    f"and dropped {len(batch)} overflow event(s): {exc}"
                )
                return
            self._dropped_events += len(batch)
            self._log_warning(
                f"Activity collector submission failed; dropped {len(batch)} event(s): {exc}"
            )

    def _log_warning(self, message: str) -> None:
        now = monotonic()
        if (
            self._last_warning_at is not None
            and now - self._last_warning_at < self.settings.warning_interval_seconds
        ):
            return
        self._last_warning_at = now
        self._logger.warning(message)

    def _resolve_ingest_token(self) -> Optional[str]:
        env_var = str(self.settings.ingest_token_env_var or "").strip() or "ACTIVITY_COLLECTOR_INGEST_TOKEN"
        return get_env_with_file_fallback(env_var)

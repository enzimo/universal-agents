"""Bound how long one model call may go without producing output.

Why this exists: provider HTTP clients (the OpenAI SDK, LiteLLM, httpx) only
time out when no *bytes* arrive for ``llm.request_timeout_seconds``. A proxy
that keeps an idle stream open with keep-alive pings (LiteLLM's proxy can send
SSE ``: ping`` comments or ``ping`` events while an upstream is silent) resets
that timer forever, so a stalled model call never ended and the task sat in
progress until someone stopped it. Strands' ``cancel_signal`` is only checked
between stream events, so it cannot end a stream that sends none.

The watchdog wraps the Strands ``Model.stream`` async generator and counts only
the events Strands receives from the provider adapter: message start and stop,
text, reasoning and tool-call deltas, and usage. Keep-alive pings never reach
that level. Two limits apply:

- ``idle_timeout_seconds``: the longest wait for the next event, including the
  first one. Reasoning models can think silently between summary parts, so
  this default is minutes, not seconds.
- ``call_timeout_seconds``: the longest one model call may take in total, so a
  stream that trickles events forever still ends.

When a limit is reached the watchdog stops the provider stream (which closes
its HTTP connection), logs ``event=model_stream_timeout``, and raises
``ModelStreamTimeoutError``. A bare ``httpx`` timeout raised while reading an
open stream is reported the same way (``limit=read``): the OpenAI SDK does not
wrap mid-stream read errors, and the bare ``httpx.ReadTimeout('')`` otherwise
reached the person as an empty "Runtime error:". The turn runtime then treats
the error like any other provider timeout: it fails over to a configured
fallback model, or fails the task with the plain timeout message.

It is installed once, on every model ``build_strands_model`` returns, by
replacing ``stream`` on that model instance. Wrapping the instance, rather
than the class or a proxy object, keeps the provider class (and every
``isinstance`` check and ``update_config`` call) unchanged.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from custom_agents.provider_error_runtime import (
    MODEL_STREAM_CALL_LIMIT,
    MODEL_STREAM_IDLE_LIMIT,
    MODEL_STREAM_READ_LIMIT,
    ModelStreamTimeoutError,
)

logger = logging.getLogger(__name__)

_WATCHDOG_MARKER = "__ua_model_stream_watchdog__"


@dataclass(frozen=True)
class ModelStreamLimits:
    """Time limits for one model call, in seconds; both must be positive."""

    idle_timeout_seconds: float
    call_timeout_seconds: float

    def __post_init__(self) -> None:
        if self.idle_timeout_seconds <= 0 or self.call_timeout_seconds <= 0:
            raise ValueError("Model stream time limits must be greater than 0.")


@dataclass
class _StreamProgress:
    """What one watched call has seen so far, for limits and diagnostics."""

    model_id: str
    started: float
    events_received: int = 0

    def elapsed(self) -> float:
        return asyncio.get_running_loop().time() - self.started


def guard_model_stream(model: Any, limits: ModelStreamLimits) -> Any:
    """Install the watchdog on one Strands model instance and return the model."""

    original_stream = model.stream
    if getattr(original_stream, _WATCHDOG_MARKER, False):
        return model

    @functools.wraps(original_stream)
    async def stream(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        model_id = _model_id(model)
        async for event in watch_model_stream(
            original_stream(*args, **kwargs), limits, model_id=model_id
        ):
            yield event

    setattr(stream, _WATCHDOG_MARKER, True)
    model.stream = stream
    return model


async def watch_model_stream(
    events: AsyncIterator[Any],
    limits: ModelStreamLimits,
    *,
    model_id: str,
) -> AsyncGenerator[Any, None]:
    """Yield provider events, ending the call when it stops making progress."""

    progress = _StreamProgress(
        model_id=model_id, started=asyncio.get_running_loop().time()
    )
    try:
        while True:
            try:
                event = await _next_event(events, limits, progress)
            except StopAsyncIteration:
                return
            progress.events_received += 1
            yield event
    finally:
        # Ends the provider generator, which closes its HTTP stream. A
        # generator that already raised is closed, and this is a no-op.
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()


async def _next_event(
    events: AsyncIterator[Any],
    limits: ModelStreamLimits,
    progress: _StreamProgress,
) -> Any:
    remaining_call = limits.call_timeout_seconds - progress.elapsed()
    if remaining_call <= 0:
        raise _timeout(MODEL_STREAM_CALL_LIMIT, limits.call_timeout_seconds, progress)
    if limits.idle_timeout_seconds <= remaining_call:
        limit, limit_seconds = MODEL_STREAM_IDLE_LIMIT, limits.idle_timeout_seconds
    else:
        limit, limit_seconds = MODEL_STREAM_CALL_LIMIT, limits.call_timeout_seconds
    deadline = asyncio.timeout(min(limits.idle_timeout_seconds, remaining_call))
    try:
        async with deadline:
            return await anext(events)
    except TimeoutError:
        if deadline.expired():
            raise _timeout(limit, limit_seconds, progress) from None
        raise
    except httpx.TimeoutException as error:
        # The HTTP client's own read timeout, raised unwrapped mid-stream.
        raise _timeout(MODEL_STREAM_READ_LIMIT, None, progress) from error


def _timeout(
    limit: str, limit_seconds: float | None, progress: _StreamProgress
) -> ModelStreamTimeoutError:
    elapsed = progress.elapsed()
    logger.warning(
        "Stopped a model call that stopped making progress. "
        "event=model_stream_timeout limit=%s limit_seconds=%s elapsed_seconds=%.1f "
        "events_received=%s model_id=%s",
        limit,
        limit_seconds if limit_seconds is not None else "-",
        elapsed,
        progress.events_received,
        progress.model_id,
    )
    return ModelStreamTimeoutError(
        limit=limit,
        limit_seconds=float(limit_seconds or 0.0),
        elapsed_seconds=elapsed,
        events_received=progress.events_received,
        model_id=progress.model_id,
    )


def _model_id(model: Any) -> str:
    config = model.get_config()
    if isinstance(config, dict):
        return str(config.get("model_id") or "unknown")
    return str(getattr(config, "model_id", None) or "unknown")

"""Side-effect-free validation for user-requested runtime model switches."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from typing import Any, Callable, Coroutine, Literal, TypeVar

from pydantic import BaseModel
from strands import Agent
from strands.types.agent import Limits
from strands.types.exceptions import ContextWindowOverflowException, ModelThrottledException


MODEL_SWITCH_PROBE_SYSTEM_PROMPT = (
    "You are a model connectivity probe. Follow the requested structured-output "
    "schema exactly and do not perform any other work."
)
MODEL_SWITCH_PROBE_PROMPT = "Return status 'ok' using the requested structured output."


class ModelSwitchProbeOutput(BaseModel):
    """Minimal structured output proving the candidate can serve a Strands turn."""

    status: Literal["ok"]


class RuntimeModelSwitchProbeError(RuntimeError):
    """Raised when a candidate model cannot pass the temporary probe."""


_T = TypeVar("_T")


def _run_coroutine(
    coroutine_factory: Callable[[], Coroutine[Any, Any, _T]],
) -> _T:
    """Run a probe from either a synchronous tool thread or an active event loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine_factory())

    # Strands may execute synchronous tools while its event loop is active. Keep
    # the temporary agent on an isolated loop in that case.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="ua-model-switch-probe") as executor:
        return executor.submit(asyncio.run, coroutine_factory()).result()


TOOL_CAPACITY_PROBE_PROMPT = "Reply with the single word OK. Do not call any tool."


def probe_tool_capacity(model: Any, *, tool_count: int, timeout_seconds: float) -> dict[str, Any]:
    """Send one request that carries ``tool_count`` tool definitions; raises when the provider refuses it.

    Before a model's ``max_tools_per_request`` is raised
    (``custom_agents.model_tool_limit``): the only reliable way to know how
    many tools a provider takes is to send that many. The placeholder tools
    take no input and are never run.
    """

    resolved_timeout = float(timeout_seconds)
    if resolved_timeout <= 0:
        raise ValueError("The tool capacity probe timeout must be greater than zero.")
    tool_specs = [
        {
            "name": f"capacity_probe_{index}",
            "description": "Placeholder that tests how many tools one request may carry; never call it.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
        for index in range(int(tool_count))
    ]
    messages = [{"role": "user", "content": [{"text": TOOL_CAPACITY_PROBE_PROMPT}]}]

    async def _send() -> None:
        async def _drain() -> None:
            async for _event in model.stream(messages, tool_specs=tool_specs, system_prompt=TOOL_CAPACITY_PROBE_PROMPT):
                pass

        await asyncio.wait_for(_drain(), timeout=resolved_timeout)

    started_at = monotonic()
    try:
        _run_coroutine(_send)
    except TimeoutError as exc:
        raise RuntimeModelSwitchProbeError(
            f"The request with {tool_count} tools did not finish within {resolved_timeout:g} seconds."
        ) from exc
    # A busy provider or an oversized request says nothing about how many
    # tools the model takes, so neither is reported as a refusal.
    except ModelThrottledException as exc:
        raise RuntimeModelSwitchProbeError(
            f"The provider is limiting requests right now, so the test with {tool_count} tools did not run; "
            f"try again later: {exc}"
        ) from exc
    except ContextWindowOverflowException as exc:
        raise RuntimeModelSwitchProbeError(
            f"{tool_count} tool definitions do not fit in the model's context window: {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeModelSwitchProbeError(
            f"The model refused a request with {tool_count} tools ({type(exc).__name__}): {exc}"
        ) from exc
    return {
        "status": "passed",
        "duration_seconds": round(monotonic() - started_at, 3),
        "timeout_seconds": resolved_timeout,
        "tools_sent": int(tool_count),
    }


def probe_strands_model(model: Any, *, timeout_seconds: float) -> dict[str, Any]:
    """Run one bounded, tool-free structured-output turn against a candidate model."""

    resolved_timeout = float(timeout_seconds)
    if resolved_timeout <= 0:
        raise ValueError("Model switch probe timeout must be greater than zero.")

    probe_agent = Agent(
        system_prompt=MODEL_SWITCH_PROBE_SYSTEM_PROMPT,
        model=model,
        tools=[],
        callback_handler=None,
    )

    async def _invoke() -> Any:
        return await asyncio.wait_for(
            probe_agent.invoke_async(
                MODEL_SWITCH_PROBE_PROMPT,
                structured_output_model=ModelSwitchProbeOutput,
                limits=Limits(turns=1),
            ),
            timeout=resolved_timeout,
        )

    started_at = monotonic()
    try:
        result = _run_coroutine(_invoke)
    except TimeoutError as exc:
        raise RuntimeModelSwitchProbeError(
            f"Candidate model probe timed out after {resolved_timeout:g} seconds."
        ) from exc
    except Exception as exc:
        raise RuntimeModelSwitchProbeError(
            f"Candidate model failed its structured-output probe: {exc}"
        ) from exc

    structured_output = getattr(result, "structured_output", None)
    if not isinstance(structured_output, ModelSwitchProbeOutput) or structured_output.status != "ok":
        raise RuntimeModelSwitchProbeError(
            "Candidate model did not return the required structured-output probe response."
        )

    return {
        "status": "passed",
        "duration_seconds": round(monotonic() - started_at, 3),
        "timeout_seconds": resolved_timeout,
        "tools_exposed": 0,
    }

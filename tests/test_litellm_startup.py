"""Cold SDK imports must not depend on a pricing download or its retry thread."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINTS = {
    "adapter": "from custom_agents.reasoning_litellm_model import ReasoningAwareLiteLLMModel",
    "capabilities": (
        "from custom_agents.model_capability_checks import check_litellm_model_capabilities\n"
        "assert check_litellm_model_capabilities('gpt-4o')['status'] == 'found'"
    ),
}


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
@pytest.mark.parametrize("inherited_mode", [None, "false"])
def test_cold_litellm_import_uses_bundled_data_without_retry_threads(
    entry_point: str, inherited_mode: str | None,
) -> None:
    program = textwrap.dedent("""
        import httpx
        import os
        import threading
        import time

        http_attempts = []
        retry_threads = []
        def offline(*args, **kwargs):
            http_attempts.append(args)
            raise httpx.ConnectTimeout('forced offline startup test')
        httpx.get = offline
        original_start = threading.Thread.start
        def adversarial_start(thread, *args, **kwargs):
            result = original_start(thread, *args, **kwargs)
            if thread.name == 'litellm-model-cost-map-retry':
                retry_threads.append(thread.name)
                # Reproduce the observed race: its logging imports run before
                # the main import can emit the failed-download warning.
                time.sleep(0.2)
            return result
        threading.Thread.start = adversarial_start
    """) + ENTRY_POINTS[entry_point] + textwrap.dedent("""

        assert not http_attempts, http_attempts
        assert not retry_threads, retry_threads
        assert os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] == 'true'
        import litellm
        assert litellm.model_cost['gpt-4o']['max_input_tokens'] > 0
    """)
    environ = dict(os.environ)
    environ.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    if inherited_mode is not None:
        environ["LITELLM_LOCAL_MODEL_COST_MAP"] = inherited_mode
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=ROOT, env=environ,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

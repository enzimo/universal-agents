"""Read the saved worker pool settings (``agent.worker_pools``) on every call.

Like the scheduled-worker settings, worker pool settings change while the
runtime runs (an operator edits the configuration), so the controller, retries
and progress notes read the saved configuration each time instead of a copy
taken at startup. A malformed section raises ``ValueError``; callers log it and
keep running.
"""

from __future__ import annotations

from typing import Any

from custom_agents.saved_runtime_config import current_launch_config
from runtime_settings.worker_pools import WORKER_POOLS_PATH, WorkerPoolsConfig


def current_worker_pool_settings(agent: Any) -> WorkerPoolsConfig:
    configured = current_launch_config(agent).get(WORKER_POOLS_PATH)
    return WorkerPoolsConfig.model_validate(
        configured or getattr(agent, "worker_pools", None) or {}
    )


__all__ = ["current_worker_pool_settings"]

"""Drain state shared by admission checks without importing drain execution.

Turn admission must read this state without depending on the control and wind-down
code that executes a drain; importing those handlers would close an import cycle.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class ContractDrain:
    """Why an agent drains and how far its queue departure and wind-down have progressed."""

    reason: str
    started_at: datetime
    holds_back_unstarted: bool = False
    left_queues: int = 0
    announced: bool = False
    task: Optional[asyncio.Task[Any]] = field(default=None, repr=False)


def is_contract_draining(agent: Any) -> bool:
    return getattr(agent, "_contract_drain", None) is not None


def is_pool_worker(agent: Any) -> bool:
    """A launch-record worker can return unstarted tasks to the team lead during drain."""
    return getattr(agent, "_worker_launch", None) is not None

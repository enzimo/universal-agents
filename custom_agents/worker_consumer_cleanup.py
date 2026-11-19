"""The team lead removes the NATS consumers that gone started workers left behind.

Why: until 2026-11-19 a worker started from a start record (pool, scheduled,
replacement or manual start) got lasting consumers, because ``spawn_agent``
sets its id and an agent with an id set was treated as a configured, lasting
route (``custom_agents.strands_agent._direct_consumer_scope``). Every start left
its mailbox and control consumer on the team's work stream, nothing deleted
them, and the offline cleanup script skipped them as lasting. NATS 2.15 refuses
new consumers once a stream holds 1,000 (error 10026), so after about 500
worker starts no worker could start, and the pool controller restarted the
crashing worker until its hourly start limit refused it.

Started workers' consumers now belong to their process. This module removes
the ones earlier versions left, once per team lead process, before the worker
pool controller starts a worker (``worker_pool_controller.reconcile_worker_pools``).

Which consumers it removes: an agent's mailbox or control consumer on the work
stream (``NatsTransportAdapter.agent_consumers``) whose owner

- has a start record. Agents without one have an id from configuration
  (``agent.agent_id``); their mailbox is a lasting route and stays. The id's
  form proves nothing, so it is never checked here.
- is gone by the one liveness rule (``custom_agents.agent_liveness``): its start
  record ended, or its process is gone. A worker whose record is still active
  keeps its consumers, however long it has been silent.

Whether a consumer is asking for messages is not used: a busy worker asks for
none and would look gone.

Messages a removed mailbox still held stay in the work stream; nothing reads
them, as before, since their worker is gone.

A sweep that fails is logged and tried again after ``RETRY_SECONDS``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from custom_agents.agent_liveness import LivenessPurpose, gone_owner_ids
from storage.agent_launch_repository import AgentLaunchRepository

logger = logging.getLogger(__name__)

# How long a failed sweep waits before the team lead tries again.
RETRY_SECONDS = 300.0
# Why the consumers were deleted (``event=nats_agent_consumer_deleted reason=``).
GONE_WORKER_REASON = "started_worker_gone"


async def remove_gone_worker_consumers(agent: Any) -> int:
    """Delete gone started workers' mailbox and control consumers; returns how many were deleted."""

    adapter = agent._ensure_nats_transport_adapter()
    consumers = await adapter.agent_consumers()
    if not consumers:
        return 0
    launches = await agent.task_manager.operations_runtime.run(
        AgentLaunchRepository(agent.task_manager.storage_settings).list, agent.team_id
    )
    started = {record.agent_id for record in launches}
    owners = {consumer.owner_agent_id for consumer in consumers}
    candidates = sorted((owners & started) - {agent.id})
    gone = set(
        await gone_owner_ids(
            agent, candidates, purpose=LivenessPurpose.CONSUMER_CLEANUP, launches=launches
        )
    )
    removable = [consumer for consumer in consumers if consumer.owner_agent_id in gone]
    deleted: int = await adapter.delete_agent_consumers(removable, reason=GONE_WORKER_REASON)
    logger.info(
        "Removed the NATS consumers of gone started workers. "
        "event=worker_consumer_cleanup_done team_id=%s agent_consumers=%s deleted=%s "
        "gone_workers=%s running_workers=%s configured_agents=%s",
        agent.team_id,
        len(consumers),
        deleted,
        len(gone),
        len(candidates) - len(gone),
        len(owners - started),
    )
    return deleted


async def sweep_gone_worker_consumers_once(agent: Any) -> None:
    """Run ``remove_gone_worker_consumers`` once per process; retry a failed sweep after ``RETRY_SECONDS``.

    Failures are logged and never stop the caller: the worker pool controller
    still runs its pass, and a start the full stream refuses is reported there.
    """

    if agent._worker_consumers_swept or time.monotonic() < agent._worker_consumer_sweep_retry_at:
        return
    try:
        await remove_gone_worker_consumers(agent)
    except Exception:
        agent._worker_consumer_sweep_retry_at = time.monotonic() + RETRY_SECONDS
        logger.exception(
            "Could not remove the NATS consumers of gone started workers; trying again later. "
            "event=worker_consumer_cleanup_failed team_id=%s retry_in_seconds=%s",
            agent.team_id,
            RETRY_SECONDS,
        )
        return
    agent._worker_consumers_swept = True


__all__ = [
    "GONE_WORKER_REASON",
    "RETRY_SECONDS",
    "remove_gone_worker_consumers",
    "sweep_gone_worker_consumers_once",
]

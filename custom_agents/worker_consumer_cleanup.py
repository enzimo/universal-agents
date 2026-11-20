"""The team lead removes the lasting NATS consumers that gone started workers left behind.

Why: until 2026-11-19 a worker started from a start record (pool, scheduled,
replacement or manual start) got lasting consumers, because ``spawn_agent``
sets its id and an agent with an id set was treated as a configured, lasting
route (``custom_agents.strands_agent._direct_consumer_scope``). Every start left
its mailbox and control consumer on the team's work stream, nothing deleted
them, and the offline cleanup script skipped them as lasting. NATS 2.15 refuses
new consumers once a stream holds 1,000 (error 10026), so after about 500
worker starts no worker could start, and the pool controller restarted the
crashing worker until its hourly start limit refused it. The push-delivery
releases (2026-09-13 to 2026-10-05) left the same consumers with the push
schema; they are listed too (``nats_consumer_reconciler.AGENT_CONSUMER_SCHEMAS``).

Started workers' consumers now belong to their process. This module removes
the ones earlier versions left, once per team lead process, before the worker
pool controller starts a worker (``worker_pool_controller.reconcile_worker_pools``).

Which consumers it removes: an agent's lasting (route) mailbox or control
consumer on the work stream (``NatsTransportAdapter.agent_consumers``) whose
owner

- is not a configured agent. Only an id set in configuration
  (``agent.agent_id``) gives an agent lasting consumers: the supervisor starts
  the lead and every startup agent without one, so their ids are drawn per
  process. The configured agents are therefore the lead itself and that one
  setting, recognised positively; their consumers stay.
- has no request open on any of its lasting consumers. Only a running process
  can hold one, so it is kept whatever the next rule says.
- is gone by the one liveness rule (``custom_agents.agent_liveness``), with or
  without a start record: its start record ended or its process is gone, or,
  when the record was already pruned (``retention_days``, 30 days by default),
  its own process or heartbeat says so. Until this was fixed (review of
  2026-11-20), owners without a start record were kept as configured, so the
  leftovers of workers that ended more than 30 days earlier stayed for good.

Process-owned consumers are never deleted here. Their process deletes them at
exit, and NATS removes them after ``transport.process_consumer_inactive_threshold_seconds``
once nothing asks (after a held message's ack wait, when a dead receiver left
one). Judging them would mean judging owners that may not have written a
status row yet, such as a startup agent the supervisor starts at the same
moment as the lead, whose fresh consumers would look gone.

Messages a removed mailbox still held stay in the work stream; nothing reads
them, as before, since their worker is gone.

A sweep that fails is logged and tried again after ``RETRY_SECONDS``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from functools import partial
from typing import Any

from custom_agents.agent_liveness import LivenessPurpose, gone_owner_ids
from custom_agents.saved_runtime_config import current_launch_config
from custom_transport.base import ConsumerScope
from custom_transport.nats_consumer_reconciler import AgentConsumer, configured_agent_ids
from custom_transport.nats_pull import PUSH_CONSUMER_METADATA_SCHEMA
from storage.agent_launch_repository import AgentLaunchRepository

logger = logging.getLogger(__name__)

# How long a failed sweep waits before the team lead tries again.
RETRY_SECONDS = 300.0
# Why the consumers were deleted (``event=nats_agent_consumer_deleted reason=``).
GONE_WORKER_REASON = "started_worker_gone"


@dataclass(frozen=True)
class SweepResult:
    """What one sweep found and did; consumers and owners are counted separately."""

    agent_consumers: int = 0
    lasting_consumers: int = 0
    push_era_consumers: int = 0
    # Owners of lasting consumers, by what decided them.
    configured_agents: int = 0
    kept_listening: int = 0
    kept_not_gone: int = 0
    gone_with_start_record: int = 0
    gone_without_start_record: int = 0
    deleted: int = 0

    @property
    def kept_owners(self) -> int:
        """Owners that may be leftovers later: neither configured nor gone."""
        return self.kept_listening + self.kept_not_gone


def configured_owner_ids(agent: Any) -> frozenset[str]:
    """The agents whose lasting consumers always stay: the lead and ids set in the saved configuration.

    Reads the configuration file, so callers run it on the storage lane.
    """

    return frozenset({agent.id}) | configured_agent_ids(current_launch_config(agent))


async def remove_gone_worker_consumers(agent: Any) -> SweepResult:
    """Delete the lasting consumers of gone agents that are not configured (module docstring)."""

    adapter = agent._ensure_nats_transport_adapter()
    consumers: tuple[AgentConsumer, ...] = await adapter.agent_consumers()
    lasting = [consumer for consumer in consumers if consumer.consumer_scope == ConsumerScope.ROUTE.value]
    found = {
        "agent_consumers": len(consumers),
        "lasting_consumers": len(lasting),
        "push_era_consumers": sum(c.schema == PUSH_CONSUMER_METADATA_SCHEMA for c in consumers),
    }
    if not lasting:
        return _logged(agent, SweepResult(**found))
    run = agent.task_manager.operations_runtime.run
    # An unreadable configuration fails the sweep: nothing is deleted without
    # knowing which agents are configured.
    configured = await run(partial(configured_owner_ids, agent))
    owners = {consumer.owner_agent_id for consumer in lasting}
    listening = {consumer.owner_agent_id for consumer in lasting if consumer.asking} - configured
    candidates = sorted(owners - configured - listening)
    launches = await run(AgentLaunchRepository(agent.task_manager.storage_settings).list, agent.team_id)
    gone = set(
        await gone_owner_ids(agent, candidates, purpose=LivenessPurpose.CONSUMER_CLEANUP, launches=launches)
    )
    started = {record.agent_id for record in launches}
    removable = [consumer for consumer in lasting if consumer.owner_agent_id in gone]
    deleted: int = await adapter.delete_agent_consumers(removable, reason=GONE_WORKER_REASON)
    return _logged(
        agent,
        SweepResult(
            **found,
            configured_agents=len(owners & configured),
            kept_listening=len(listening),
            kept_not_gone=len(candidates) - len(gone),
            gone_with_start_record=len(gone & started),
            gone_without_start_record=len(gone - started),
            deleted=deleted,
        ),
    )


def _logged(agent: Any, result: SweepResult) -> SweepResult:
    logger.info(
        "Removed the lasting NATS consumers of gone agents. "
        "event=worker_consumer_cleanup_done team_id=%s agent_consumers=%s lasting_consumers=%s "
        "push_era_consumers=%s configured_agents=%s kept_listening=%s kept_not_gone=%s "
        "gone_with_start_record=%s gone_without_start_record=%s deleted=%s",
        agent.team_id,
        result.agent_consumers,
        result.lasting_consumers,
        result.push_era_consumers,
        result.configured_agents,
        result.kept_listening,
        result.kept_not_gone,
        result.gone_with_start_record,
        result.gone_without_start_record,
        result.deleted,
    )
    return result


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
    "SweepResult",
    "configured_owner_ids",
    "remove_gone_worker_consumers",
    "sweep_gone_worker_consumers_once",
]

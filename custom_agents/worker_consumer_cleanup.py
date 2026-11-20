"""The team lead removes the NATS consumers that gone started workers left behind.

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
the ones earlier versions left.

When: once when the team lead starts, before it creates its own consumers and
before its schedule loop, scheduler loop (with the takeover of a dead worker's
work) or any message can start a worker (``UniversalAgent._connect_nats``,
``sweep_worker_consumers_at_start``). A full stream refuses the lead's own
mailbox as well as every worker's, so without this sweep the lead could not
start at all. Then again every ``RESWEEP_SECONDS`` from the lead's schedule
loop, before that pass starts workers, while the last sweep kept lasting
consumers of owners that are neither configured nor gone, and
``RETRY_SECONDS`` after a sweep failed (``sweep_worker_consumers_if_due``).

A later sweep removes what an earlier one kept because its owner still had a
request open, reported a fresh heartbeat without a start record, or was judged
while heartbeat checks were paused; each clears within minutes. It never
removes leftovers whose start record names a run that cannot be checked from
here (``inspect_launch`` answers ``unknown`` when the run is not proven, and
that record stays active until it is removed): those stay, and the sweep keeps
repeating hourly, until the launcher's removal step or the offline script
(``scripts/reconcile_nats_consumers.py``) deletes them with the runtime stopped.

After each sweep, and every ``CAPACITY_CHECK_SECONDS`` while the lead runs,
it reads its work stream's consumer count against the limit NATS enforces and
warns near it (``custom_transport.nats_consumer_state``); the launcher's check
does the same at each start.

Which lasting (route) mailbox and control consumers it removes
(``NatsTransportAdapter.agent_consumers``): those whose owner

- is not a configured agent. Only an id set in configuration
  (``agent.agent_id``) or on the command line (``--agent-id``) gives an agent
  lasting consumers. The supervisor starts the lead and every startup agent
  with neither, so their ids are drawn per process. The configured agents are
  therefore the lead itself and ``agent.agent_id``, recognised positively;
  their consumers stay. An agent somebody runs by hand with its own
  ``--agent-id`` is not recognised: while it asks for messages the next rule
  keeps it, but if it is stopped or silent when the lead sweeps, its consumers
  are removed. Its messages stay in the stream and are delivered when it
  starts again and creates them anew. That deployment, agents outside the one
  supervisor run, is the one ``custom_agents.agent_liveness`` already lists as
  not supported.
- has no request open on any of its consumers. Only a running process can
  hold one, so it is kept whatever the next rule says.
- is gone by the one liveness rule (``custom_agents.agent_liveness``), with or
  without a start record: its start record ended or its process is gone, or,
  when the record was already pruned (``retention_days``, 30 days by default),
  its own process or heartbeat says so. Until this was fixed (review of
  2026-11-20), owners without a start record were kept as configured, so the
  leftovers of workers that ended more than 30 days earlier stayed for good.

Process-owned consumers are removed only when the owner's start record proves
it gone (ended, or its process absent); without a start record they are left
to their process and to NATS. An owner without a start record may simply not
have written its first status row yet, such as a startup agent the supervisor
starts at the same moment as the lead, whose fresh consumers would look gone.
NATS removes a process-owned consumer after
``transport.process_consumer_inactive_threshold_seconds`` once nothing asks,
but only after the ack wait of a message a dead receiver held
(``transport.ack_wait_seconds``, 3,600 s by default), so a worker that was busy
when its process died would otherwise keep its mailbox for about an hour.

Messages a removed mailbox still held stay in the work stream; nothing reads
them, as before, since their worker is gone.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from custom_agents.agent_liveness import LivenessPurpose, OwnerLiveness, owner_decisions
from custom_agents.saved_runtime_config import current_launch_config
from custom_transport.base import ConsumerScope
from custom_transport.nats_consumer_reconciler import AgentConsumer, configured_agent_ids
from custom_transport.nats_consumer_state import StreamConsumerCapacity, log_capacity_if_high, version_label
from custom_transport.nats_pull import PUSH_CONSUMER_METADATA_SCHEMA
from storage.agent_launch_repository import AgentLaunchRepository

logger = logging.getLogger(__name__)

# How long a failed sweep waits before the team lead tries again.
RETRY_SECONDS = 300.0
# How long the team lead waits before sweeping again while the last sweep kept
# lasting consumers of owners that are neither configured nor gone. Fixed: what
# a later sweep can still remove clears within minutes (a request that ended,
# a heartbeat that went stale), and owners it can never judge would otherwise
# cost a listing more often; an hourly sweep is a few NATS requests.
RESWEEP_SECONDS = 3600.0
# Why the consumers were deleted (``event=nats_agent_consumer_deleted reason=``).
GONE_WORKER_REASON = "started_worker_gone"
# Which sweep ran (``event=worker_consumer_cleanup_done sweep=``).
SWEEP_AT_START = "start"
SWEEP_AGAIN = "again"
# How often the lead reads its work stream's consumer count between sweeps, to
# warn before the stream reaches its limit (``nats_stream_consumer_capacity_high``).
# Fixed: one or two NATS requests an hour; the 2026-11-19 incident built up over weeks.
CAPACITY_CHECK_SECONDS = 3600.0
# Who reported a stream near its limit (``event=nats_stream_consumer_capacity_high reporter=``).
REPORTER_TEAM_LEAD = "team_lead"


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
    # Gone owners whose consumers were deleted (lasting ones, and process-owned
    # ones whose start record proves the owner gone).
    gone_with_start_record: int = 0
    gone_without_start_record: int = 0
    deleted: int = 0
    # Kept owners of lasting consumers by the liveness evidence that kept them
    # (``launch_active``, ``heartbeat_fresh``...); per-owner lines are DEBUG.
    kept_evidence: dict[str, int] = field(default_factory=dict)

    @property
    def kept_owners(self) -> int:
        """Owners of lasting consumers that may be leftovers later: neither configured nor gone."""
        return self.kept_listening + self.kept_not_gone


def configured_owner_ids(agent: Any) -> frozenset[str]:
    """The agents whose lasting consumers always stay: the lead and ids set in the saved configuration.

    Reads the configuration file, so callers run it on the storage lane.
    """

    return frozenset({agent.id}) | configured_agent_ids(current_launch_config(agent))


async def remove_gone_worker_consumers(agent: Any) -> SweepResult:
    """Delete the consumers of gone agents that are not configured (module docstring)."""

    adapter = agent._ensure_nats_transport_adapter()
    consumers: tuple[AgentConsumer, ...] = await adapter.agent_consumers()
    if not consumers:
        return SweepResult()
    run = agent.task_manager.operations_runtime.run
    # An unreadable configuration fails the sweep: nothing is deleted without
    # knowing which agents are configured.
    configured = await run(partial(configured_owner_ids, agent))
    launches = await run(AgentLaunchRepository(agent.task_manager.storage_settings).list, agent.team_id)
    started = {record.agent_id for record in launches}
    lasting = [consumer for consumer in consumers if consumer.consumer_scope == ConsumerScope.ROUTE.value]
    lasting_owners = {consumer.owner_agent_id for consumer in lasting} - configured
    asking = {consumer.owner_agent_id for consumer in consumers if consumer.asking}
    # Owners of process-owned consumers only when a start record can prove them gone.
    recorded_owners = {consumer.owner_agent_id for consumer in consumers} & started
    candidates = sorted((lasting_owners | recorded_owners) - configured - asking)
    decisions = owner_decisions(agent, candidates, purpose=LivenessPurpose.CONSUMER_CLEANUP, launches=launches)
    gone = {decision.owner_agent_id for decision in decisions if decision.gone}
    removable = [
        consumer
        for consumer in consumers
        if consumer.owner_agent_id in gone
        and (consumer.consumer_scope == ConsumerScope.ROUTE.value or consumer.owner_agent_id in started)
    ]
    deleted: int = await adapter.delete_agent_consumers(removable, reason=GONE_WORKER_REASON)
    return SweepResult(
        agent_consumers=len(consumers),
        lasting_consumers=len(lasting),
        push_era_consumers=sum(consumer.schema == PUSH_CONSUMER_METADATA_SCHEMA for consumer in consumers),
        configured_agents=len({consumer.owner_agent_id for consumer in lasting} & configured),
        kept_listening=len(lasting_owners & asking),
        kept_not_gone=len(lasting_owners - asking - gone),
        gone_with_start_record=len(gone & started),
        gone_without_start_record=len(gone - started),
        deleted=deleted,
        kept_evidence=_kept_evidence(decisions, lasting_owners),
    )


def _kept_evidence(decisions: list[OwnerLiveness], lasting_owners: set[str]) -> dict[str, int]:
    """Kept owners of lasting consumers, counted by the evidence that kept them."""

    kept = Counter(
        decision.evidence.value
        for decision in decisions
        if not decision.gone and decision.owner_agent_id in lasting_owners
    )
    return dict(sorted(kept.items()))


async def sweep_worker_consumers_at_start(agent: Any) -> SweepResult | None:
    """The team lead's sweep when it starts (module docstring); never raises."""

    return await _sweep(agent, SWEEP_AT_START)


async def sweep_worker_consumers_if_due(agent: Any) -> SweepResult | None:
    """Sweep again, or read the stream's capacity again, when its time has come; else nothing, not even NATS.

    The capacity read is independent of the re-sweep: every
    ``CAPACITY_CHECK_SECONDS`` from the start sweep on, also when no sweep is
    needed any more.
    """

    result = None
    due = getattr(agent, "_worker_consumer_sweep_at", None)
    if due is not None and time.monotonic() >= due:
        result = await _sweep(agent, SWEEP_AGAIN)
    check_at = getattr(agent, "_stream_capacity_check_at", None)
    if check_at is not None and time.monotonic() >= check_at:
        await _read_capacity(agent)
    return result


async def _read_capacity(agent: Any) -> StreamConsumerCapacity | None:
    """The work stream's count and limit, warned about near the limit; schedules the next read.

    Never raises: a failed read is logged and leaves the sweep's outcome alone.
    """

    agent._stream_capacity_check_at = time.monotonic() + CAPACITY_CHECK_SECONDS
    try:
        capacity: StreamConsumerCapacity | None = await agent._ensure_nats_transport_adapter().stream_consumer_capacity()
    except Exception as exc:
        logger.warning(
            "Could not read the work stream's consumer capacity. "
            "event=nats_consumer_capacity_unavailable team_id=%s error=%s",
            agent.team_id,
            exc,
        )
        return None
    log_capacity_if_high(logger, capacity, reporter=REPORTER_TEAM_LEAD)
    return capacity


async def _sweep(agent: Any, sweep: str) -> SweepResult | None:
    """One sweep; schedules the next one. Failures are logged and never reach the caller."""

    try:
        result = await remove_gone_worker_consumers(agent)
    except Exception:
        agent._worker_consumer_sweep_at = time.monotonic() + RETRY_SECONDS
        logger.exception(
            "Could not remove the NATS consumers of gone agents; trying again later. "
            "event=worker_consumer_cleanup_failed team_id=%s sweep=%s retry_in_seconds=%s",
            agent.team_id,
            sweep,
            RETRY_SECONDS,
        )
        return None
    again = RESWEEP_SECONDS if result.kept_owners else None
    agent._worker_consumer_sweep_at = None if again is None else time.monotonic() + again
    # After the deletions, outside the sweep's own failure handling.
    capacity = await _read_capacity(agent)
    logger.info(
        "Removed the NATS consumers of gone agents. "
        "event=worker_consumer_cleanup_done team_id=%s sweep=%s agent_consumers=%s lasting_consumers=%s "
        "push_era_consumers=%s configured_agents=%s kept_listening=%s kept_not_gone=%s kept_evidence=%s "
        "gone_with_start_record=%s gone_without_start_record=%s deleted=%s next_sweep_in_seconds=%s "
        "stream_consumers=%s stream_consumer_limit=%s nats_server_version=%s",
        agent.team_id,
        sweep,
        result.agent_consumers,
        result.lasting_consumers,
        result.push_era_consumers,
        result.configured_agents,
        result.kept_listening,
        result.kept_not_gone,
        result.kept_evidence,
        result.gone_with_start_record,
        result.gone_without_start_record,
        result.deleted,
        again,
        None if capacity is None else capacity.consumer_count,
        None if capacity is None else capacity.consumer_limit,
        version_label(None if capacity is None else capacity.server_version),
    )
    return result


__all__ = [
    "GONE_WORKER_REASON",
    "CAPACITY_CHECK_SECONDS",
    "RESWEEP_SECONDS",
    "RETRY_SECONDS",
    "SWEEP_AGAIN",
    "SWEEP_AT_START",
    "SweepResult",
    "configured_owner_ids",
    "remove_gone_worker_consumers",
    "sweep_worker_consumers_at_start",
    "sweep_worker_consumers_if_due",
]

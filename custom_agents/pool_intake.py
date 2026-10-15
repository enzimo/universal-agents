"""When a worker may take its next task from its type's shared queue.

Durable work is pulled, never pushed (ADR-019). Every worker of a type reads
one shared queue (``<prefix>.type.<AgentType>.work``) and asks NATS for a task
only while ``may_take_pool_task`` is true, that is while it could start the
task at once. Unstarted tasks therefore stay in the shared queue, where any
free worker of the type, on any host, takes them, and where the worker pool
controller counts them. Before 2026-10-04 NATS pushed each task to a worker it
picked; a busy worker stored the task in its own inbox, other workers sat idle,
and the requester's pickup check failed the waiting task.

The check reads in-memory state only; it runs before every request for work.
A worker that becomes busy while a request is open returns the task it is then
handed (``custom_transport.nats_pull``).
"""

from __future__ import annotations

import time
from typing import Any

from custom_agents.lease_loss_wind_down import is_winding_down


def provider_cooldown_active(agent: Any) -> bool:
    """Whether the worker waits out a model provider's limit (no new work meanwhile)."""

    until = getattr(agent, "_provider_cooldown_until", None)
    return until is not None and time.monotonic() < until


def may_take_pool_task(agent: Any) -> bool:
    """Whether this worker can start a task from its shared queue right now."""

    if not getattr(agent, "_worker_ready", False):
        return False
    if agent.get_exit_event().is_set() or getattr(agent, "paused", False):
        return False
    # Leaving: a lost launch lease, or the pool controller asked it to drain.
    if is_winding_down(agent) or getattr(agent, "_pool_draining", False):
        return False
    if provider_cooldown_active(agent):
        return False
    if agent.is_executing:
        return False
    manager = agent.task_manager
    state = manager.task_state
    if state.current_in_progress_task_id or state.task_queue:
        return False
    if getattr(agent, "_pending_interrupt_responses", None):
        return False
    if len(agent.deferred_inbox):
        return False
    return not manager.is_buffer_full()


def mailbox_has_room(agent: Any) -> bool:
    """Whether the agent's own mailbox may take another message.

    A busy agent stores messages that need its model in its deferred inbox.
    When that inbox is full the agent stops asking, and the broker keeps the
    messages until the agent can take them.
    """

    inbox = agent.deferred_inbox
    return bool(len(inbox) < inbox.capacity)

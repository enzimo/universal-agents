"""An agent's directory of its team, kept from the team's signed agent records.

Each agent keeps who else is on its team in memory: ``known_agents.agent_map`` (id to an
``AgentStatus`` summary) and ``_agent_id_by_name`` (the name the model and messages use, to
the id). Presence messages filled them until step 7b; now this process's host reads the
team's live agents from their records (``custom_transport.protocol1.team_agents``) and
hands each new view to every agent it runs (``custom_agents.agent_host``), on the agent's
own loop, here. Nothing else writes these two maps.

- **What an entry says.** Only what the records say: id, type, name, team lead or not,
  paused, busy, and whether it accepts work. ``last_seen_at`` is when this directory took
  the entry as it stands, not a time the other agent wrote: an entry is kept as it is
  while its record does not change. Details an agent reports about itself (its current
  task, tools, skills, findings) are asked with ``agent.status`` when needed.
- **In order, whole.** Each update replaces the directory with the whole view it was read
  from, synchronously, and the host hands the views to an agent's loop in the order it read
  them. So a later view always wins: an older update can never put back an agent that
  left, or remove one that came back (``sync_directory``).
- **One name, one agent.** A name held by two live agents names neither until one of them
  leaves, as ``TeamAgents.named`` refuses it; the collision is logged once with both ids.
  Nobody can take over another agent's name by giving a second agent the same one.
- **An agent whose host runs another message contract** (contract README "Versions") stays
  in the directory, so control requests about the tasks it holds (``task.probe``, pause,
  resume, exit, status) still go to it, but it takes no new work: its entry says it does
  not accept work, and delegation passes it over (``TeamAgent.takes_work``).
- **Leaving for good.** When an agent left for good (``TeamAgents.left_for_good``: it said it
  was exiting, or its session was closed), every agent that asked it for work by name
  fails, at once, each such subtask that still waits in the departed agent's own mailbox
  and that nobody ever took: nothing will read that mailbox again (agent ids are new at
  every start) and the subtask is in no saved state. The subtask's route names the
  mailbox (``header.to_routing_path`` is the agent id a by-name delegation was sent to).
  The requester's delegated-task record says whether a worker said it took it; since that
  claim is sent after the worker saved the task and may be lost or late, the requester
  also withdraws the message from the work stream (its ``publish_sequence``) and fails the
  subtask only when the withdrawal succeeds: a worker that took it removed it from the
  stream. A subtask a worker took is left alone: a restarted agent takes over what it held
  from saved state and reports it again, and one handed back goes to another worker;
  custody recovery and the subtask watchdog handle those, as they do a subtask whose record
  cannot be read or was never published. One narrow window stays: a worker that saved the
  task but crashed before removing it from the stream. A subtask delegated before records
  existed has no record and is failed as before. Only the subtask fails, as with the
  watchdog's timeout, a handback or a worker loss: the parent task and its other subtasks
  go on, and the parent's turn resumes to decide again (``_fail_untaken``). An agent whose
  host only stopped reporting has not left for good: the host may be unreachable for a
  while, and the watchdog handles work that never comes back.
- **Exits while the requester was down** (``check_departed_targets``). An exit is handed to
  the agents running when the reader learns of it (``agent_exited``). A requester that was
  down then restores its saved subtasks only during its start, after the view may already
  have been read, so it checks them itself: once it restored them and its process read the
  view (``agent_host.report_restored_work``), it reads its waiting subtasks once and fails
  those whose target is not live and left for good, the same way. The reader does not tell
  listeners about the exits it finds at its first reading, so a start costs one read per
  restored agent, not one per retained exit and agent. If an exit is also told while the
  check runs, a subtask is still failed once: a second failure is refused by the subtask's
  state change, and only one withdrawal of its message succeeds.
- **Finding a named agent to give work to** (``live_named_agent``). A by-name delegation goes
  to the agent's own mailbox, so the agent must be there to take it. The lookup waits up to
  ``NAMED_AGENT_START_SECONDS`` for the name to appear in the view, so an agent started just
  before (``instantiate_agent``) is found once its record arrives, and then asks it for its
  basic ``agent.status`` within the subtask probe's timeout. An agent that is still in the
  view but does not answer (its process crashed, and its host's record has not expired yet),
  that is exiting or that takes no work is refused at once, instead of the subtask waiting
  for the watchdog. Only the team lead asks: the contract lets only the lead's host, the
  team services and the gateway send ``agent.status``. Every other requester gets the same
  wait and the view's checks (it takes work, it is of the type asked for), without the ask.
- **No database writes.** The team lead no longer copies other agents' status into the
  shared ``agent_states`` table; every agent refreshes its own row there.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, NamedTuple, Optional

from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.enums import MessageMetaType
from custom_transport.protocol1.team_agents import TeamAgent

_LOGGER = logging.getLogger(__name__)
# How long a by-name lookup waits for the agent's record to reach the view: a thread worker the
# lead has just started connects, joins its host, opens its session and writes its record first
# (measured in tests/test_live_worker_pools.py; module docstring). How often the view is read meanwhile.
NAMED_AGENT_START_SECONDS = 10.0
VIEW_POLL_SECONDS = 0.2
# The fields an entry takes from the record; an entry whose fields are unchanged is kept as it is.
_RECORD_FIELDS = {"self_id", "team_id", "name", "named_agent_type", "agent_role", "is_team_lead", "is_paused",
                  "is_executing", "is_busy", "accepting_work", "is_exiting"}


def directory_status(item: TeamAgent, team_id: Optional[str]) -> AgentStatus:
    """The directory entry for one live agent, from its record (``last_seen_at``: when this directory took it)."""

    return AgentStatus(
        self_id=item.agent,
        team_id=team_id,
        name=item.display_name or f"Agent_{item.agent}",
        named_agent_type=item.agent_type,
        agent_role="team_lead" if item.is_lead else "worker",
        is_team_lead=item.is_lead,
        is_paused=item.state == "paused",
        is_executing=item.state == "busy",
        is_busy=item.state == "busy",
        accepting_work=item.takes_work,
        is_exiting=False,
        last_seen_at=datetime.now(timezone.utc).isoformat(),
        status_type=MessageMetaType.RESPONSE,
    )


def sync_directory(agent: Any, view: Mapping[str, TeamAgent]) -> None:
    """Make the agent's directory the whole ``view`` (module docstring); run on the agent's own loop."""

    own = str(agent.id)
    team_id = getattr(agent, "team_id", None)
    others = {agent_id: item for agent_id, item in view.items() if agent_id != own}
    directory = agent.known_agents.agent_map
    for agent_id in [agent_id for agent_id in directory if agent_id not in others]:
        del directory[agent_id]
    for agent_id, item in others.items():
        entry = directory_status(item, team_id)
        earlier = directory.get(agent_id)
        if earlier is None or earlier.model_dump(include=_RECORD_FIELDS) != entry.model_dump(include=_RECORD_FIELDS):
            directory[agent_id] = entry
    holders = _name_holders(others)
    names = agent._agent_id_by_name
    names.clear()
    names.update({name: ids[0] for name, ids in holders.items() if len(ids) == 1})
    _note_ambiguous_names(agent, {name: ids for name, ids in holders.items() if len(ids) > 1})


def _name_holders(view: Mapping[str, TeamAgent]) -> dict[str, tuple[str, ...]]:
    """Each display name in ``view`` with the ids of the live agents that hold it, in id order."""

    holders: dict[str, list[str]] = {}
    for agent_id, item in sorted(view.items()):
        if item.display_name:
            holders.setdefault(item.display_name, []).append(agent_id)
    return {name: tuple(ids) for name, ids in holders.items()}


def _note_ambiguous_names(agent: Any, ambiguous: dict[str, tuple[str, ...]]) -> None:
    """Log each name two live agents hold, once while it lasts, and when it names one agent again.

    The agent remembers them (``UniversalAgent._ambiguous_team_names``), written only here, on its own loop.
    """

    own = str(agent.id)
    earlier = getattr(agent, "_ambiguous_team_names", None)
    if earlier is None:  # a stand-in agent; UniversalAgent declares it
        earlier = agent._ambiguous_team_names = {}
    for name, ids in ambiguous.items():
        if earlier.get(name) != ids:
            agent._logger.warning("[%s] Live agents share a name, so it names none of them until only one is left. "
                                  "event=team_directory_name_ambiguous agent_id=%s name=%s agent_ids=%s",
                                  own, own, name, ",".join(ids))
    for name in earlier.keys() - ambiguous.keys():
        agent._logger.info("[%s] A shared name names one agent again. event=team_directory_name_unique agent_id=%s "
                           "name=%s", own, own, name)
    earlier.clear()
    earlier.update(ambiguous)


class _Untaken(NamedTuple):
    """A waiting subtask sent by name to an agent that left for good, with who that was and why."""

    subtask: Any
    departed_id: str
    label: str
    reason: str


async def agent_exited(agent: Any, departed: TeamAgent, reason: str) -> None:
    """``departed`` left for good: fail what this agent sent to its mailbox that nobody took (module docstring).

    Every agent of the process hears every exit, so the exit is logged only by an agent that
    had sent work there; each failed subtask is logged as a warning (``_fail_untaken``).
    """

    task_manager = agent.task_manager
    in_its_mailbox = [
        subtask_info
        for subtask_info in await task_manager.operations_runtime.run(task_manager.get_pending_subtasks)
        if getattr(getattr(subtask_info, "header", None), "to_routing_path", None) == departed.agent
    ]
    if not in_its_mailbox:
        return
    agent._logger.info("[%s] Agent '%s' (%s), which this agent asked for work by name, left for good. "
                       "event=team_agent_left_for_good agent_id=%s departed_agent_id=%s departed_agent_type=%s "
                       "reason=%s subtasks=%s", agent.id, departed.display_name or departed.agent, departed.agent,
                       agent.id, departed.agent, departed.agent_type, reason, len(in_its_mailbox))
    label = departed.display_name or departed.agent
    await _fail_untaken(agent, [_Untaken(item, departed.agent, label, reason) for item in in_its_mailbox])


async def check_departed_targets(agent: Any, team_agents: Any) -> None:
    """After a restart: fail the agent's waiting by-name subtasks whose target left for good meanwhile.

    Run once the agent restored its saved work and its process read the team's view
    (``agent_host.report_restored_work``): one read of its waiting subtasks, however many
    exits the view retained. A subtask's target counts when it is not live and left for good
    (``team_agents.agent`` and ``left_for_good``, read from this agent's thread: the view is
    replaced whole and the departures are single dictionary reads). The rest is as for an exit
    seen while the agent runs (``_fail_untaken``). A failed read leaves the work to the
    subtask watchdog.
    """

    task_manager = agent.task_manager
    try:
        pending = await task_manager.operations_runtime.run(task_manager.get_pending_subtasks)
    except Exception:
        agent._logger.exception("[%s] The agent's waiting subtasks could not be read to check them against agents "
                                "that left while it was down; they are left to the subtask watchdog. "
                                "event=team_exit_recovery_unread agent_id=%s", agent.id, agent.id)
        return
    untaken: list[_Untaken] = []
    for subtask_info in pending:
        target = getattr(getattr(subtask_info, "header", None), "to_routing_path", None)
        if not target or team_agents.agent(target) is not None or not team_agents.left_for_good(target):
            continue
        departed = team_agents.departed_agent(target)
        label = ((departed.display_name if departed is not None else None)
                 or getattr(getattr(subtask_info, "meta", None), "to_agent_name", None) or target)
        untaken.append(_Untaken(subtask_info, target, label, team_agents.departure_reason(target) or ""))
    if not untaken:
        agent._logger.debug("[%s] No waiting subtask was sent to an agent that left while this agent was down. "
                            "event=team_exit_recovery_checked agent_id=%s waiting=%s", agent.id, agent.id,
                            len(pending))
        return
    agent._logger.info("[%s] Subtasks this agent sent by name wait for agents that left for good while it was down. "
                       "event=team_exit_recovery_found agent_id=%s subtasks=%s departed_agent_ids=%s", agent.id,
                       agent.id, len(untaken), ",".join(sorted({item.departed_id for item in untaken})))
    await _fail_untaken(agent, untaken)


async def _fail_untaken(agent: Any, untaken: list[_Untaken]) -> None:
    """Fail each subtask that still waits untaken in its departed agent's mailbox (module docstring).

    Only the subtask fails, as when the subtask watchdog times one out, a worker hands one back
    or a delegated task keeps losing its workers: the parent task and its other subtasks go on,
    and the parent's turn resumes to decide again. That turn runs inside the failure step, so
    the subtasks of different requests (root tasks) are failed side by side: a restart can leave
    several people's requests waiting on one departed worker, and none should wait for another
    request's turn. One request's subtasks go one after another, so a request never runs two
    turns at once, and only its last failed subtask resumes its parent.
    """

    task_manager = agent.task_manager
    by_request: dict[str, list[_Untaken]] = {}
    for item in untaken:
        task_id = item.subtask.task.task_id
        try:
            request = await task_manager.operations_runtime.run(task_manager.resolve_root_task_id, task_id)
        except Exception as error:
            agent._logger.error("[%s] A subtask left in the mailbox of an agent that left for good could not be "
                                "failed (its request could not be read); it is left to the subtask watchdog. "
                                "event=team_agent_left_subtask_fail_error agent_id=%s subtask_id=%s "
                                "departed_agent_id=%s", agent.id, agent.id, task_id, item.departed_id, exc_info=error)
            continue
        by_request.setdefault(request or task_id, []).append(item)
    await asyncio.gather(*(_fail_in_order(agent, group) for group in by_request.values()))


async def _fail_in_order(agent: Any, untaken: list[_Untaken]) -> None:
    """One request's subtasks, one after another (``_fail_untaken``); one subtask's error does not stop the others."""

    task_manager = agent.task_manager
    for subtask_info, departed_id, label, reason in untaken:
        task_id = subtask_info.task.task_id
        parent_task_id: Optional[str] = None
        try:
            if not await _still_waiting(agent, task_id, departed_id):
                continue
            parent_task_id = await task_manager.operations_runtime.run(task_manager.get_parent_task_id, task_id)
            if not parent_task_id:
                continue
            agent._logger.warning("[%s] A subtask waited in the mailbox of an agent that left for good; it is failed "
                                  "and its parent task goes on. event=team_agent_left_subtask_failed agent_id=%s "
                                  "task_id=%s subtask_id=%s departed_agent_id=%s reason=%s", agent.id, agent.id,
                                  parent_task_id, task_id, departed_id, reason)
            await agent._fail_subtask_and_reconcile_parent(
                parent_task_id=parent_task_id,
                subtask_info=subtask_info,
                error_message=f"Delegated subtask failed because agent {label} left before taking it.",
                resume_parent=True,
                synthetic_context=(
                    f"Agent {label} left the team before it took delegated subtask {task_id}, so that subtask is "
                    "marked failed. Continue the parent task: delegate the work again, to the agent type or to "
                    "another named worker, do it another way, or tell the person."
                ),
            )
        except Exception as error:
            await _failure_step_failed(agent, task_id, parent_task_id, departed_id, error)


async def _failure_step_failed(agent: Any, task_id: str, parent_task_id: Optional[str], departed_id: str,
                               error: Exception) -> None:
    """Say what an error in the failure step left behind, and resume a parent it left waiting.

    The failure step marks the subtask failed first and resumes the parent last. An error before
    the subtask was failed leaves it waiting, and the subtask watchdog handles it. After that the
    watchdog no longer sees it, so the parent is resumed once more here, but only while it still
    waits for its subtasks (``WAITING``): a parent whose turn already began is not run again.
    """

    from custom_messages.enums import ProgressState

    task_manager = agent.task_manager
    run = task_manager.operations_runtime.run
    subtask_failed, parent_state = False, None
    if parent_task_id:
        try:
            subtask = await run(task_manager.get_task_info, task_id)
            subtask_failed = subtask is not None and bool(await run(task_manager.is_terminal_state, subtask.state))
            parent = await run(task_manager.get_task_info, parent_task_id)
            parent_state = parent.state if parent is not None else None
        except Exception:
            agent._logger.exception("[%s] After an error in the failure step, the subtask's state could not be read. "
                                    "event=team_agent_left_subtask_state_unread agent_id=%s subtask_id=%s",
                                    agent.id, agent.id, task_id)
    ids = (agent.id, task_id, parent_task_id, departed_id)
    if not subtask_failed:
        agent._logger.error("[%s] A subtask left in the mailbox of an agent that left for good could not be failed; it "
                            "is left to the subtask watchdog. event=team_agent_left_subtask_fail_error agent_id=%s "
                            "subtask_id=%s parent_task_id=%s departed_agent_id=%s", agent.id, *ids, exc_info=error)
        return
    if parent_state != ProgressState.WAITING:
        agent._logger.error("[%s] A subtask was failed after its agent left for good, but its parent task could not be "
                            "resumed, and the parent no longer waits (state=%s), so it is not run again. "
                            "event=team_agent_left_parent_not_resumed agent_id=%s subtask_id=%s parent_task_id=%s "
                            "departed_agent_id=%s", agent.id, getattr(parent_state, "value", parent_state), *ids,
                            exc_info=error)
        return
    agent._logger.error("[%s] A subtask was failed after its agent left for good, but its parent task could not be "
                        "resumed; it is resumed once more. event=team_agent_left_parent_resume_retry agent_id=%s "
                        "subtask_id=%s parent_task_id=%s departed_agent_id=%s", agent.id, *ids, exc_info=error)
    try:
        await agent._resume_parent_task_if_ready(parent_task_id)
    except Exception:
        agent._logger.exception("[%s] The parent task still could not be resumed; it waits until a person or the team "
                                "lead acts on it. event=team_agent_left_parent_resume_failed agent_id=%s subtask_id=%s "
                                "parent_task_id=%s departed_agent_id=%s", agent.id, *ids)


async def _still_waiting(agent: Any, task_id: str, departed_id: str) -> bool:
    """Whether the subtask still waits in the departed agent's mailbox, untaken: then it may be failed.

    True only when its record says nobody took it and its message could be withdrawn from the
    work stream, or when it has no record (delegated before records existed). Anything that
    cannot be told is left to the watchdog (module docstring).
    """

    from custom_agents.delegated_work_runtime import delegated_tasks

    def kept(why: str, event: str, holder: Optional[str] = None) -> bool:
        agent._logger.info("[%s] A subtask sent to the departed agent's mailbox is not failed: %s. event=%s "
                           "agent_id=%s subtask_id=%s departed_agent_id=%s holder_agent_id=%s", agent.id, why, event,
                           agent.id, task_id, departed_id, holder)
        return False

    try:
        record = await agent.task_manager.operations_runtime.run(delegated_tasks(agent).get, str(agent.team_id),
                                                                 task_id)
    except Exception:
        agent._logger.exception("[%s] Whether a subtask was taken could not be read; it is left to the subtask "
                                "watchdog. event=team_agent_left_subtask_unread agent_id=%s subtask_id=%s "
                                "departed_agent_id=%s", agent.id, agent.id, task_id, departed_id)
        return False
    if record is None:
        return True
    if record.holder_agent_id is not None or record.holders:
        return kept("a worker took it, so custody recovery and the watchdog handle it",
                    "team_agent_left_subtask_kept", record.holder_agent_id)
    if not record.publish_sequence:
        return kept("it is not published yet, so the watchdog handles it", "team_agent_left_subtask_unpublished")
    try:
        withdrawn = bool(await agent.team_comms.delete_work_message(record.publish_sequence))
    except Exception:
        agent._logger.exception("[%s] A subtask's message could not be withdrawn; it is left to the subtask watchdog. "
                                "event=team_agent_left_subtask_withdraw_failed agent_id=%s subtask_id=%s "
                                "departed_agent_id=%s sequence=%s", agent.id, agent.id, task_id, departed_id,
                                record.publish_sequence)
        return False
    if not withdrawn:
        return kept("its message was no longer in the work stream, so a worker took it and its claim has not "
                    "arrived", "team_agent_left_subtask_taken_unclaimed")
    return True


async def live_named_agent(agent: Any, named_agent_type: Optional[str], target_agent_name: str, *,
                           start_seconds: Optional[float] = None, ask: bool = True) -> Optional[AgentStatus]:
    """The named agent, when it is live, takes work and (with ``ask``) answers its status now; None otherwise.

    Module docstring: only the team lead may ask, so other requesters pass ``ask=False``.
    """

    from custom_agents.agent_requests import ask_agent

    start_seconds = NAMED_AGENT_START_SECONDS if start_seconds is None else start_seconds
    loop = asyncio.get_running_loop()
    deadline = loop.time() + start_seconds
    status = live_agent_status(agent, named_agent_type, target_agent_name, quiet=True)
    while status is None and loop.time() < deadline:
        await asyncio.sleep(VIEW_POLL_SECONDS)
        status = live_agent_status(agent, named_agent_type, target_agent_name, quiet=True)
    if status is None:
        live_agent_status(agent, named_agent_type, target_agent_name)  # logs when the view is not known
        agent._logger.info("[%s] No live agent has this name. event=named_agent_not_live agent_id=%s "
                           "target_agent_name=%s target_named_agent_type=%s waited_seconds=%s", agent.id, agent.id,
                           target_agent_name, named_agent_type, start_seconds)
        return None
    if not ask:
        return status
    timeout = max(0.1, float(getattr(agent, "subtask_status_probe_timeout_seconds", None) or 5.0))
    try:
        reply = await ask_agent(agent, "agent.status", {"level": "basic"}, str(status.self_id), timeout=timeout)
    except Exception as error:  # noqa: BLE001 - no answer, a refusal or a broken reply: it cannot take the task now
        why = f"{type(error).__name__}: {error}"
    else:
        if reply.get("outcome") != "ok":
            why = f"answered {reply.get('outcome')}"
        elif reply.get("exiting") or not reply.get("accepting_work"):
            why = "it is exiting" if reply.get("exiting") else "it takes no work now"
        elif named_agent_type and reply.get("agent_type") != named_agent_type:
            why = f"it is a {reply.get('agent_type')}"
        else:
            return status
    agent._logger.warning("[%s] A named agent is in the team's records but cannot take work now; the delegation "
                          "is refused. event=named_agent_not_answering agent_id=%s target_agent_id=%s "
                          "target_agent_name=%s reason=%s", agent.id, agent.id, status.self_id, target_agent_name, why)
    return None


def live_agent_status(agent: Any, named_agent_type: Optional[str],
                      target_agent_name: Optional[str] = None, *, quiet: bool = False) -> Optional[AgentStatus]:
    """A live agent of the type (or with the name) that accepts work, from the view; None when there is none.

    This answers from the records alone, so nothing is sent (``live_named_agent`` also asks a
    named agent). A name two live agents share finds nobody (``TeamAgents.named``). ``quiet``
    leaves out the warning that the view is not known yet, for callers that look again.
    """

    from custom_agents.agent_requests import team_view

    view = team_view()
    if view is None:
        if not quiet:
            agent._logger.warning("[%s] The team's agents are not known yet (this process has not joined, or runs "
                                  "without the launcher). event=team_directory_unavailable target_named_agent_type=%s "
                                  "target_agent_name=%s", agent.id, named_agent_type, target_agent_name)
        return None
    if target_agent_name:
        found = view.named(target_agent_name)
        candidates = [found] if found is not None else []
    else:
        candidates = view.of_type(str(named_agent_type or ""))
    for item in candidates:
        if item.agent == str(agent.id) or not item.takes_work:
            continue
        if named_agent_type and item.agent_type != named_agent_type:
            continue
        return directory_status(item, getattr(agent, "team_id", None))
    return None

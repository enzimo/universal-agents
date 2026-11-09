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
  existed has no record and is failed as before. An agent whose host only stopped
  reporting has not left for good: the host may be unreachable for a while, and the
  watchdog handles work that never comes back.
- **No database writes.** The team lead no longer copies other agents' status into the
  shared ``agent_states`` table; every agent refreshes its own row there.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.enums import MessageMetaType
from custom_transport.protocol1.team_agents import TeamAgent

_LOGGER = logging.getLogger(__name__)
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
    holders: dict[str, list[str]] = {}
    for agent_id, item in sorted(others.items()):
        if item.display_name:
            holders.setdefault(item.display_name, []).append(agent_id)
    names = agent._agent_id_by_name
    names.clear()
    names.update({name: ids[0] for name, ids in holders.items() if len(ids) == 1})
    _note_ambiguous_names(agent, {name: tuple(ids) for name, ids in holders.items() if len(ids) > 1})


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


async def agent_exited(agent: Any, departed: TeamAgent, reason: str) -> None:
    """``departed`` left for good: fail what this agent sent to its mailbox that nobody took (module docstring)."""

    agent._logger.warning("[%s] Agent '%s' (%s) left for good. event=team_agent_left_for_good agent_id=%s "
                          "departed_agent_id=%s departed_agent_type=%s reason=%s", agent.id,
                          departed.display_name or departed.agent, departed.agent, agent.id, departed.agent,
                          departed.agent_type, reason)
    task_manager = agent.task_manager
    in_its_mailbox = [
        subtask_info
        for subtask_info in await task_manager.operations_runtime.run(task_manager.get_pending_subtasks)
        if getattr(getattr(subtask_info, "header", None), "to_routing_path", None) == departed.agent
    ]
    for subtask_info in in_its_mailbox:
        task_id = subtask_info.task.task_id
        if not await _still_waiting(agent, task_id, departed):
            continue
        parent_task_id = await task_manager.operations_runtime.run(task_manager.get_parent_task_id, task_id)
        if not parent_task_id:
            continue
        agent._logger.warning("[%s] A subtask waited in the mailbox of an agent that left for good; it is failed. "
                              "event=team_agent_left_subtask_failed agent_id=%s task_id=%s subtask_id=%s "
                              "departed_agent_id=%s reason=%s", agent.id, agent.id, parent_task_id, task_id,
                              departed.agent, reason)
        await agent._fail_subtask_and_reconcile_parent(
            parent_task_id=parent_task_id,
            subtask_info=subtask_info,
            error_message=(f"Delegated subtask failed because agent {departed.display_name or departed.agent} "
                           "left before taking it."),
        )


async def _still_waiting(agent: Any, task_id: str, departed: TeamAgent) -> bool:
    """Whether the subtask still waits in the departed agent's mailbox, untaken: then it may be failed.

    True only when its record says nobody took it and its message could be withdrawn from the
    work stream, or when it has no record (delegated before records existed). Anything that
    cannot be told is left to the watchdog (module docstring).
    """

    from custom_agents.delegated_work_runtime import delegated_tasks

    def kept(why: str, event: str, holder: Optional[str] = None) -> bool:
        agent._logger.info("[%s] A subtask sent to the departed agent's mailbox is not failed: %s. event=%s "
                           "agent_id=%s subtask_id=%s departed_agent_id=%s holder_agent_id=%s", agent.id, why, event,
                           agent.id, task_id, departed.agent, holder)
        return False

    try:
        record = await agent.task_manager.operations_runtime.run(delegated_tasks(agent).get, str(agent.team_id),
                                                                 task_id)
    except Exception:
        agent._logger.exception("[%s] Whether a subtask was taken could not be read; it is left to the subtask "
                                "watchdog. event=team_agent_left_subtask_unread agent_id=%s subtask_id=%s "
                                "departed_agent_id=%s", agent.id, agent.id, task_id, departed.agent)
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
                                "departed_agent_id=%s sequence=%s", agent.id, agent.id, task_id, departed.agent,
                                record.publish_sequence)
        return False
    if not withdrawn:
        return kept("its message was no longer in the work stream, so a worker took it and its claim has not "
                    "arrived", "team_agent_left_subtask_taken_unclaimed")
    return True


def live_agent_status(agent: Any, named_agent_type: Optional[str],
                      target_agent_name: Optional[str] = None) -> Optional[AgentStatus]:
    """A live agent of the type (or with the name) that accepts work, from the view; None when there is none.

    This answers what the delegation probe asked over the operational subject until step 7b:
    the records already say who is live, so nothing is sent. A name two live agents share
    finds nobody (``TeamAgents.named``).
    """

    from custom_agents.agent_requests import team_view

    view = team_view()
    if view is None:
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

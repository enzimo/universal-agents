"""An agent's directory of its team, kept from the team's signed agent records.

Each agent keeps who else is on its team in memory: ``known_agents.agent_map`` (id to an
``AgentStatus`` summary) and ``_agent_id_by_name`` (the name the model and messages use, to
the id). Presence messages filled them until step 7b; now this process's host reads the
team's live agents from their records (``custom_transport.protocol1.team_agents``) and
hands every change to each agent it runs (``custom_agents.agent_host``), on the agent's own
loop, here.

- **What an entry says.** Only what the records say: id, type, name, team lead or not,
  paused, busy, and whether it accepts work. Details an agent reports about itself (its
  current task, tools, skills, findings) are asked with ``agent.status`` when needed.
- **An agent whose host runs another message contract** (contract README "Versions") stays
  in the directory, so control requests about the tasks it holds (``task.probe``, pause,
  resume, exit, status) still go to it, but it takes no new work: its entry says it does
  not accept work, and delegation passes it over (``TeamAgent.takes_work``).
- **Leaving.** An agent that drops out of the view leaves the directory. When its record
  said it was exiting, the team lead fails the subtasks it asked that agent for by name,
  as it did when presence announced an exit. An agent whose host only stopped reporting
  is not given up for: the host may be unreachable for a while, and the subtask watchdog
  handles work that never comes back.
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


def directory_status(item: TeamAgent, team_id: Optional[str]) -> AgentStatus:
    """The directory entry for one live agent, from its record."""

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


async def apply_team_change(agent: Any, before: Mapping[str, TeamAgent], after: Mapping[str, TeamAgent],
                            changed: frozenset[str], exited: frozenset[str]) -> None:
    """Bring the agent's directory up to date with the view (module docstring)."""

    directory = agent.known_agents.agent_map
    names = agent._agent_id_by_name
    for agent_id in sorted(changed):
        if agent_id == str(agent.id):
            continue
        now, earlier = after.get(agent_id), before.get(agent_id)
        if now is not None:
            directory[agent_id] = directory_status(now, getattr(agent, "team_id", None))
            if earlier is not None and earlier.display_name and earlier.display_name != now.display_name:
                names.pop(earlier.display_name, None)
            if now.display_name:
                names[now.display_name] = agent_id
            continue
        directory.pop(agent_id, None)
        if earlier is not None and earlier.display_name and names.get(earlier.display_name) == agent_id:
            names.pop(earlier.display_name, None)
        if agent_id in exited and earlier is not None:
            await agent_exited(agent, earlier)


async def agent_exited(agent: Any, departed: TeamAgent) -> None:
    """An agent said it was leaving: the team lead fails the subtasks it asked that agent for by name."""

    name = departed.display_name
    agent._logger.warning("[%s] Agent '%s' (%s) exited. event=team_agent_exited agent_type=%s", agent.id,
                          name or departed.agent, departed.agent, departed.agent_type)
    if not getattr(agent, "is_team_lead", False) or not name:
        return
    task_manager = agent.task_manager
    pending = [
        subtask_info
        for subtask_info in await task_manager.operations_runtime.run(task_manager.get_pending_subtasks)
        if getattr(subtask_info.meta, "to_agent_name", None) == name
    ]
    for subtask_info in pending:
        parent_task_id = await task_manager.operations_runtime.run(task_manager.get_parent_task_id,
                                                                   subtask_info.task.task_id)
        if not parent_task_id:
            continue
        await agent._fail_subtask_and_reconcile_parent(
            parent_task_id=parent_task_id,
            subtask_info=subtask_info,
            error_message=f"Delegated subtask failed because assigned agent {name} exited before replying.",
        )


def live_agent_status(agent: Any, named_agent_type: Optional[str],
                      target_agent_name: Optional[str] = None) -> Optional[AgentStatus]:
    """A live agent of the type (or with the name) that accepts work, from the view; None when there is none.

    This answers what the delegation probe asked over the operational subject until step 7b:
    the records already say who is live, so nothing is sent.
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

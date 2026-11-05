"""The team as a restart's capture reaches it, without NATS: the live agents and their status answers.

``FakeTeam`` stands in for this process's host (``custom_agents.agent_host.AgentProcessHost``)
behind ``custom_agents.agent_requests``: ``team_agents`` is the view of the team's records, and
``ask`` answers the team lead's ``agent.status`` request as each agent would now. Install it with
``configure_process_host``. ``status_answer`` builds the answer with the real handler's code
(``custom_agents.live_controls.status_reply``), so it has the contract's fields.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType, SimpleNamespace
from typing import Any, Optional

from custom_agents.live_controls import status_reply
from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.enums import MessageMetaType
from custom_transport.protocol1.team_agents import TeamAgent


def worker_status(agent_id: str, name: str, *, current_task_id: Optional[str] = None, age: int = 42,
                  queued: int = 0, deferred: int = 1, agent_type: str = "CodeWritingAgent") -> AgentStatus:
    """An agent's own status: running ``current_task_id`` for ``age`` seconds, or idle without one."""

    return AgentStatus(
        status_type=MessageMetaType.RESPONSE, self_id=agent_id, name=name, named_agent_type=agent_type,
        agent_role="worker", is_paused=False, is_executing=current_task_id is not None,
        current_task_id=current_task_id, current_task_age_seconds=age if current_task_id else None,
        top_level_queue_depth=queued, waiting_top_level_task_count=0, deferred_message_count=deferred,
    )


def status_answer(status: AgentStatus) -> dict[str, Any]:
    """The full ``agent.status`` answer an agent with this status gives the team lead."""

    return status_reply(SimpleNamespace(get_self_status=lambda info_type=None: status), full=True)


@dataclass
class _Answer:
    reply: Optional[dict[str, Any]] = None  # None: it never answers
    error: Optional[BaseException] = None
    delay: float = 0.0


@dataclass(frozen=True)
class Asked:
    kind: str
    body: dict[str, Any]
    target: str
    sender: str
    timeout: Optional[float]


class FakeTeam:
    """The team's live agents and what each answers (module docstring)."""

    def __init__(self) -> None:
        self.records: dict[str, TeamAgent] = {}
        self.answers: dict[str, _Answer] = {}
        self.asked: list[Asked] = []
        ready = asyncio.Event()
        ready.set()
        self.team_agents = SimpleNamespace(ready=ready, live=lambda: MappingProxyType(dict(self.records)))

    def join(self, agent_id: str, name: str, agent_type: str = "CodeWritingAgent", *, is_lead: bool = False) -> None:
        """The agent's record is live; until it is given an answer, it does not answer."""

        self.records[agent_id] = TeamAgent(
            agent=agent_id, agent_type=agent_type, display_name=name, state="idle", accepting_work=True,
            since=datetime.now(timezone.utc), host="lead", session=f"s-{agent_id}", is_lead=is_lead)

    def leave(self, agent_id: str) -> None:
        self.records.pop(agent_id, None)

    def answer(self, agent_id: str, status: AgentStatus, *, delay: float = 0.0) -> None:
        self.answers[agent_id] = _Answer(reply=status_answer(status), delay=delay)

    def reply(self, agent_id: str, reply: dict[str, Any]) -> None:
        """Answer with this exact body, such as a refusal (``not_allowed``)."""

        self.answers[agent_id] = _Answer(reply=dict(reply))

    def fail(self, agent_id: str, error: BaseException) -> None:
        """Asking the agent raises ``error``, as the host does when nobody answers or it refuses."""

        self.answers[agent_id] = _Answer(error=error)

    def silence(self, agent_id: str) -> None:
        self.answers.pop(agent_id, None)

    async def ask(self, kind: str, body: dict[str, Any], target_agent: str, *, sender_agent: str,
                  timeout: Optional[float] = None) -> dict[str, Any]:
        self.asked.append(Asked(kind, dict(body), target_agent, sender_agent, timeout))
        answer = self.answers.get(target_agent, _Answer())
        await asyncio.sleep(answer.delay)
        if answer.error is not None:
            raise answer.error
        if answer.reply is None:
            await asyncio.Event().wait()
        assert answer.reply is not None
        return dict(answer.reply)

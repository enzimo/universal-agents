"""Where web research tools save files, and which task they were saved for.

Research files live in the task's own folder: its person's folder, or
``agents/`` for work with no person (``custom_tools.workspace_scope``), so
another person's tasks cannot see them. Every research manifest records the
same identity fields. The tools write and read these folders only through the
agent file library (``custom_tools.file_access``), so a link planted in place
of ``research/`` cannot send files anywhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from custom_tools.runtime_context import active_task_context, get_agent_ref
from custom_tools.workspace_scope import current_workspace_scope
from runtime_settings.instance_layout import PERSON_RESEARCH_DIRNAME

NO_ACTIVE_TASK = "no_active_task"


def safe_slug(value: str, *, fallback: str = "artifact", max_len: int = 48) -> str:
    """A file-name-safe, lowercase slug."""

    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip().lower()).strip("-._")
    return (slug or fallback)[:max_len]


@dataclass(frozen=True)
class ResearchIdentity:
    research_root: Path
    team_id: str | None
    agent_id: str | None
    named_agent_type: str | None
    task_id: str
    conversation_id: str | None

    @property
    def task_folder(self) -> Path:
        """The folder of this task's research artifacts."""

        return self.research_root / safe_slug(self.task_id)

    def manifest_fields(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "agent_id": self.agent_id,
            "named_agent_type": self.named_agent_type,
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
        }


def research_identity() -> ResearchIdentity:
    """The calling agent's research folder and task, for the active tool call."""

    agent = get_agent_ref()
    task_id, meta = active_task_context(agent) if agent is not None else (None, None)
    return ResearchIdentity(
        research_root=current_workspace_scope(agent).home / PERSON_RESEARCH_DIRNAME,
        team_id=getattr(agent, "team_id", None),
        agent_id=getattr(agent, "id", None),
        named_agent_type=getattr(agent, "named_agent_type", None),
        task_id=task_id or NO_ACTIVE_TASK,
        conversation_id=getattr(meta, "conversation_id", None),
    )


__all__ = ["NO_ACTIVE_TASK", "ResearchIdentity", "research_identity", "safe_slug"]

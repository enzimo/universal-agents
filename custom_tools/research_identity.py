"""Where web research tools save files, and which task they were saved for.

Research files live in the task's own folder: its person's folder, or
``agents/`` for work with no person (``custom_tools.workspace_scope``), so
another person's tasks cannot see them. Every research manifest records the
same identity fields. The tools write and read these folders only through the
agent file library (``custom_tools.file_access``), so a link planted in place
of ``research/`` cannot send files anywhere else.

Layout: ``research/<task folder>/<result folder>/``, one task folder per task
(``task_folder_name``) and one new result folder per tool call
(``result_folder_name``). Both names keep different tasks and different calls
apart, because the tools write fixed file names (``manifest.json`` and others)
and the file library reuses a folder that already exists, so two calls sharing
a folder would overwrite each other's files. Nothing reads these folders by
working out a name again: the tools return the exact paths, and
``web_artifact_inspect`` accepts any path inside the person's ``research/``
folder, so folders named before this scheme stay readable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from custom_tools.runtime_context import active_task_context, get_agent_ref
from custom_tools.workspace_scope import current_workspace_scope
from id_generation import new_id_suffix
from runtime_settings.instance_layout import PERSON_RESEARCH_DIRNAME

NO_ACTIVE_TASK = "no_active_task"
SLUG_MAX_LEN = 48
# Every task id's folder name: a readable start, "-", then this many hex
# characters of the id's SHA-256, within SLUG_MAX_LEN in total.
TASK_FOLDER_DIGEST_LEN = 12
TASK_FOLDER_READABLE_LEN = SLUG_MAX_LEN - 1 - TASK_FOLDER_DIGEST_LEN


def safe_slug(value: str, *, fallback: str = "artifact", max_len: int = SLUG_MAX_LEN) -> str:
    """A file-name-safe, lowercase slug."""

    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or "").strip().lower()).strip("-._")
    return (slug or fallback)[:max_len]


def task_folder_name(task_id: str) -> str:
    """A stable, readable folder name, at most 48 characters, for a task id.

    Every id gets up to 35 slug characters, "-" and 12 hex characters of its
    SHA-256. The digest distinguishes ids shortened or changed by slugging.
    Hashing safe ids too prevents one from matching another id's generated
    folder name, such as ``Report-A`` and ``report-a-a4ff557d33e9``.
    """

    readable = safe_slug(task_id, max_len=TASK_FOLDER_READABLE_LEN).rstrip("-._")
    # surrogatepass: an id decoded from JSON may hold a lone surrogate.
    digest = hashlib.sha256(task_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"{readable}-{digest[:TASK_FOLDER_DIGEST_LEN]}"


def result_folder_name(stamp: str, *labels: str) -> str:
    """A new folder name for one tool call's files: ``<stamp>_<labels>_<12 random hex>``.

    The stamp and labels (service, operation, query) keep it readable and in
    time order. The random ending keeps two calls in one task apart when the
    rest matches: parallel calls in one turn, the same query with another
    offset or page, or queries that differ only in case, punctuation or after
    their first 48 characters. Without it the second call reused the first
    call's folder and replaced its files, so the first call's returned paths
    showed the second call's results.
    """

    return "_".join([stamp, *(safe_slug(label) for label in labels), new_id_suffix()])


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

        return self.research_root / task_folder_name(self.task_id)

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


__all__ = [
    "NO_ACTIVE_TASK",
    "ResearchIdentity",
    "research_identity",
    "result_folder_name",
    "safe_slug",
    "task_folder_name",
]

"""The manager's share of each person's stored data, measured at start and once a day.

People hear as their stored data grows (``assistant_gateway.storage_usage``).
Their data is split between the gateway and the manager, which do not read
each other's records, so each side measures its own and writes per-person
totals to a shared table in the manager's database
(``storage.person_storage_usage_repository``); the gateway adds both and
sends the notices. The manager measures, for each active person:

- the text of their conversations: turns, summaries and compaction summaries
  (``ConversationStore.stored_text_bytes_by_owner``), and their completed
  tasks (``TaskArchiveRepository.stored_text_bytes_by_owner``), one aggregate
  per table, byte sizes counted by the database (``SqlDialect.text_bytes``);
- the long texts saved as files beside their conversations
  (``dbs/conversation_artifacts/<conversation id>/``, owned through the
  conversation);
- their folder (``workspaces/users/<folder>/``), like the manager's other
  walks of people's folders (scratch and saved output retention), never
  following a link (``access_control.safe_open.folder_bytes``).

Where it runs: in the team lead's process, as its own task, started and kept
alive by the scheduler loop (``keep_storage_usage_measured``, called from
``scheduled_runtime._scheduler_pass``). The scheduler loop runs for the whole
life of the manager process, whatever the maintenance schedules are set to;
the internal-improvement maintenance schedule can be paused or disabled, and
then the totals would go stale without anyone noticing. The measurement runs
in a thread of its own, never on the manager's storage lane or inside the
pass, because reading every stored text can take a while and must not delay
due schedules or stuck-work reports. If the task ever ends, the next pass
starts it again.

If the manager cannot measure (no instance folder layout, no conversation
store, an unreadable folder, a depth limit or a failure), it writes nothing
and preserves the previous snapshot: the gateway then sees the totals age,
sends no notice on partial data, and the Status page says the totals are out
of date (``assistant_gateway.storage_usage``). Silence must not look like
"under the limit".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from access_control import safe_open
from runtime_settings.instance_layout import InstanceLayout, person_folder_name
from storage.person_storage_usage_repository import (
    CONVERSATION_FILES,
    FOLDER,
    TASKS,
    PersonStorageUsageRepository,
    StorageSide,
)

logger = logging.getLogger(__name__)

USAGE_INTERVAL = timedelta(hours=24)
# The team lead's measuring task, by agent id (one per process).
_LOOPS: dict[str, asyncio.Task[None]] = {}


class ConversationRecords(Protocol):
    """The manager's conversations (``custom_agents.conversation_store.ConversationStore``)."""

    def stored_text_bytes_by_owner(self) -> dict[str, int]: ...

    def conversation_owners(self) -> dict[str, str]: ...


class TaskRecords(Protocol):
    """The manager's completed tasks (``storage.task_archive_repository``)."""

    def stored_text_bytes_by_owner(self) -> dict[str, int]: ...


def measure_manager_side(
    people: Iterable[str],
    *,
    conversations: ConversationRecords,
    tasks: TaskRecords,
    layout: InstanceLayout,
) -> dict[str, dict[str, int]]:
    """Bytes by kind for each of ``people``; a person with no data gets zeros.

    Every database aggregate runs once for everyone; only these people's
    folders and their conversations' file folders are walked.
    """

    wanted = sorted(set(people))
    conversation_text = conversations.stored_text_bytes_by_owner()
    task_text = tasks.stored_text_bytes_by_owner()
    files = _conversation_file_bytes(layout.conversation_artifacts_dir, conversations.conversation_owners(), set(wanted))
    return {
        person: {
            TASKS: conversation_text.get(person, 0) + task_text.get(person, 0),
            CONVERSATION_FILES: files.get(person, 0),
            FOLDER: _person_folder_bytes(layout, person),
        }
        for person in wanted
    }


def _conversation_file_bytes(artifacts_dir: Path, owners: dict[str, str], people: set[str]) -> dict[str, int]:
    """Bytes of the files saved beside each person's conversations, one folder per conversation."""

    totals: dict[str, int] = {}
    for name, owner in owners.items():
        if owner not in people:
            continue
        try:
            size = safe_open.folder_bytes(artifacts_dir, (name,))
        except safe_open.UnsafePath as exc:
            if exc.reason not in {safe_open.REASON_LINK, safe_open.REASON_NOT_DIRECTORY}:
                raise
            continue  # an entry replaced by a link or file is excluded
        totals[owner] = totals.get(owner, 0) + size
    return totals


def _person_folder_bytes(layout: InstanceLayout, person: str) -> int:
    """The person's folder; one that is a link counts nothing and is logged (it is never followed)."""

    folder = person_folder_name(person)
    try:
        return safe_open.folder_bytes(layout.users_dir, (folder,))
    except safe_open.UnsafePath as exc:
        if exc.reason not in {safe_open.REASON_LINK, safe_open.REASON_NOT_DIRECTORY}:
            raise
        logger.warning(
            "A person's folder could not be measured, so it counts nothing. "
            "event=storage_usage_folder_unmeasured person_folder=%s reason=%s",
            folder,
            exc.reason,
        )
        return 0


def record_manager_usage(agent: Any, *, now: datetime | None = None) -> int | None:
    """Measure every active person's manager-side data and write it; how many people, None when nothing was written."""

    from custom_agents.task_context_runtime import people_directory
    from custom_tools.workspace_scope import current_instance_layout

    layout = current_instance_layout(agent)
    conversations: ConversationRecords | None = getattr(agent, "_conversation_store", None)
    task_manager = getattr(agent, "task_manager", None)
    tasks: TaskRecords | None = getattr(task_manager, "task_archive_repository", None)
    storage_settings = getattr(task_manager, "storage_settings", None)
    if layout is None or conversations is None or tasks is None or storage_settings is None:
        missing = [
            name
            for name, part in (
                ("instance_layout", layout),
                ("conversation_store", conversations),
                ("task_archive", tasks),
                ("storage_settings", storage_settings),
            )
            if part is None
        ]
        logger.warning(
            "People's stored data was not measured, so their totals will age and their storage notices stop. "
            "event=storage_usage_skipped agent_id=%s missing=%s",
            getattr(agent, "id", ""),
            ",".join(missing),
        )
        return None
    team_id = str(getattr(agent, "team_id", "") or "")
    people = [person for person, now_known in people_directory(agent).people().items() if now_known.active]
    try:
        usage = measure_manager_side(people, conversations=conversations, tasks=tasks, layout=layout)
    except OSError:
        logger.exception(
            "People's stored data could not be fully measured; previous totals are kept and will age. "
            "event=storage_usage_failed agent_id=%s",
            getattr(agent, "id", ""),
        )
        return None
    PersonStorageUsageRepository(storage_settings).record_side(
        team_id, StorageSide.MANAGER, usage, measured_at=now or datetime.now(timezone.utc)
    )
    logger.info(
        "Measured people's stored data in the manager. event=storage_usage_manager_measured agent_id=%s people=%s",
        getattr(agent, "id", ""),
        len(usage),
    )
    return len(usage)


async def storage_usage_loop(agent: Any, *, interval: timedelta = USAGE_INTERVAL) -> None:
    """Measure now and then every ``interval`` until the agent exits; a failed measurement is logged and retried."""

    stop: asyncio.Event = agent._should_exit
    while not stop.is_set():
        try:
            await asyncio.to_thread(record_manager_usage, agent)
        except Exception:  # noqa: BLE001 - logged; the totals age and the gateway reports that
            logger.exception(
                "Measuring people's stored data in the manager failed; it runs again later. "
                "event=storage_usage_failed agent_id=%s",
                getattr(agent, "id", ""),
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
        except TimeoutError:
            continue


def keep_storage_usage_measured(agent: Any) -> None:
    """Start the team lead's measuring task, or start it again if it ended; other agents do nothing.

    Called on every scheduler pass; cheap when the task is running.
    """

    if not getattr(agent, "is_team_lead", False) or not getattr(agent, "team_id", None):
        return
    if not isinstance(getattr(agent, "_should_exit", None), asyncio.Event):
        return  # not a running agent (the loop ends with the agent's exit event)
    key = str(getattr(agent, "id", "") or "")
    running = _LOOPS.get(key)
    if running is not None and not running.done():
        return
    if running is not None:
        logger.warning(
            "The manager's measuring of people's stored data had stopped; starting it again. "
            "event=storage_usage_restarted agent_id=%s",
            key,
        )
    _LOOPS[key] = asyncio.create_task(storage_usage_loop(agent), name=f"storage_usage_{key}")


__all__ = [
    "USAGE_INTERVAL",
    "keep_storage_usage_measured",
    "measure_manager_side",
    "record_manager_usage",
    "storage_usage_loop",
]

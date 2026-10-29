"""How much stored data each person has, and telling them as it grows.

Chat transcripts and completed tasks are kept indefinitely (owner's decision
of 2026-10-27), so a person's stored data only grows unless they delete files
from their own folder. Nothing may grow unnoticed, so each person gets an
Inbox notice each time their stored data passes another step:
``gateway.storage_warnings.person_step_gb`` gigabytes (5 by default; 1 GB is
1,000,000,000 bytes), so at 5, 10, 15 GB and so on; 0 turns this off.

Who measures what. The gateway and the manager do not read each other's
records, so each measures its own share and writes per-person totals to a
shared table in the manager's database
(``storage.person_storage_usage_repository``):

- the gateway (here, once a day): chat transcripts, requests and replies in
  its own database, by owner, and the chat uploads still kept in
  ``state/uploads``, from their recorded sizes;
- the manager (``custom_agents.storage_usage_runtime``, at its start and once
  a day): conversation turns, summaries and completed tasks, the long texts
  saved as files beside conversations, and the person's folder.

Text sizes are counted by the databases themselves in one aggregate per table
(``SqlDialect.text_bytes``: UTF-8 bytes on SQLite and PostgreSQL alike), so no
row is loaded. The result is approximate: it is what the data takes as text
and files, not the database pages or disk blocks that hold it.

Telling people. After writing its own row, the gateway adds the manager's row
and announces the highest step the total reached, if it is above the highest
step announced to that person before (kept on the person's gateway row):
someone first measured at 16 GB gets one notice, for 15 GB. When the total
falls below the announced step (files deleted from the folder, uploads or
saved outputs removed by their retention), the announced step is lowered to
the step actually reached, so passing it again is announced again. Each
announcement's notice id names the person's folder (stable and unique,
``person_folder_name``), the step and the announcement's number
(``usage_notice_id``). The notice is stored before the announced step is
written, and the access store keeps one notice per id, so a restart between
the two finds the same id and the step is still announced exactly once.

Out-of-date totals. When the manager's row for a person is missing or older
than ``STALE_AFTER``, the gateway sends that person no notice, because a
partial total could hide a step. It logs a warning naming the person's folder,
and the Status page shows that the totals are out of date. Status also checks
the gateway's rows, so repeated gateway measurement failures cannot remain
silent while the manager still measures normally
(``stale_finding``), so silence does not look like "under the limit". A
person whose standing changed less than ``STALE_AFTER`` ago (just added or
enabled again) and has no manager row yet is only waiting for the manager's
next daily measurement, so that is logged as information.

Inbox notices are silent (decision D3): they show under Settings > Inbox and
are not sent to a chat. The one place notices also go to a chat is stuck work
(``custom_agents.stuck_work``, by the owner's decision of 2026-10-04); storage
notices do not.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

from custom_messages.contracts.access_v2 import AccessNoticeKind, AccessNoticeV1, PersonStanding, TeamPersonV1
from custom_messages.contracts.status_v1 import StatusFindingV1, StatusSeverity
from runtime_settings.instance_layout import person_folder_name
from storage.person_storage_usage_repository import (
    CHATS,
    CONVERSATION_FILES,
    FOLDER,
    TASKS,
    UPLOADS,
    Announcement,
    SideUsage,
    StorageSide,
)

logger = logging.getLogger(__name__)

# 1 GB, as people read it in settings and notices.
GIGABYTE = 1_000_000_000
USAGE_CHECK_INTERVAL = timedelta(hours=24)
FIRST_CHECK_DELAY = timedelta(minutes=15)
# The manager measures daily; a total older than this means it stopped.
STALE_AFTER = timedelta(days=3)
NOTICE_ID_PREFIX = "storage_usage"
FILES_VIEW_PATH = "/webchat?view=files"
STALE_FINDING_ID = "person_storage_totals_stale"


class ChatRecords(Protocol):
    """The gateway database (``assistant_gateway.store.GatewayStateStore``)."""

    def chat_text_bytes_by_owner(self) -> dict[str, int]: ...

    def upload_bytes_by_owner(self, *, uploads_dir: str) -> dict[str, int]: ...


class UsageStore(Protocol):
    """The shared totals (``storage.person_storage_usage_repository.PersonStorageUsageRepository``)."""

    def record_side(
        self, team_id: str, side: StorageSide, usage: Mapping[str, Mapping[str, int]], *, measured_at: datetime
    ) -> None: ...

    def side_usage(self, team_id: str, side: StorageSide) -> dict[str, SideUsage]: ...

    def announcement(self, team_id: str, person_id: str) -> Announcement: ...

    def record_announcement(self, team_id: str, person_id: str, announcement: Announcement) -> bool: ...


@dataclass(frozen=True)
class StoredData:
    """Approximate bytes one person's data takes, by where it is kept (both sides added)."""

    chats: int = 0  # chat transcripts, requests and replies
    uploads: int = 0  # files they sent in chat that are still kept
    tasks: int = 0  # their conversations' turns and summaries, and completed tasks
    conversation_files: int = 0  # long texts saved as files beside their conversations
    folder: int = 0  # their own folder

    @classmethod
    def of(cls, gateway: Mapping[str, int], manager: Mapping[str, int]) -> "StoredData":
        return cls(
            chats=int(gateway.get(CHATS, 0)),
            uploads=int(gateway.get(UPLOADS, 0)),
            tasks=int(manager.get(TASKS, 0)),
            conversation_files=int(manager.get(CONVERSATION_FILES, 0)),
            folder=int(manager.get(FOLDER, 0)),
        )

    @property
    def total(self) -> int:
        return sum(getattr(self, item.name) for item in fields(self))


def measure_gateway_side(people: Iterable[str], *, chat: ChatRecords, uploads_dir: str) -> dict[str, dict[str, int]]:
    """The gateway's share for each of ``people``: one aggregate per table; a person with no data gets zeros."""

    chat_text = chat.chat_text_bytes_by_owner()
    uploads = chat.upload_bytes_by_owner(uploads_dir=uploads_dir)
    return {person: {CHATS: chat_text.get(person, 0), UPLOADS: uploads.get(person, 0)} for person in sorted(set(people))}


# -- the notice -------------------------------------------------------------------------------

# How each kind of stored data is named in a person's notice.
_KIND_LABELS = (
    ("folder", "Your folder"),
    ("chats", "Chats (your messages and the replies)"),
    ("tasks", "Completed tasks and conversation records"),
    ("conversation_files", "Long texts saved with your conversations"),
    ("uploads", "Files you sent in chat"),
)


def readable_bytes(size: int) -> str:
    """Bytes in decimal units with at most one decimal: 5 GB, 10.4 GB, 830 MB (1 GB = 1,000,000,000 bytes)."""

    for unit, scale in (("TB", 10**12), ("GB", GIGABYTE), ("MB", 10**6), ("kB", 10**3)):
        if size >= scale:
            return f"{size / scale:.1f}".removesuffix(".0") + f" {unit}"
    return f"{size} bytes"


def step_reached(total_bytes: int, step_bytes: int) -> int:
    """The highest whole multiple of ``step_bytes`` that ``total_bytes`` reached; 0 below the first or with steps off."""

    if step_bytes <= 0:
        return 0
    return (total_bytes // step_bytes) * step_bytes


def usage_notice_id(person_id: str, reached_bytes: int, number: int) -> str:
    """One id per announcement: the person's folder, the step and the announcement's number (at most 120 characters)."""

    return f"{NOTICE_ID_PREFIX}:{person_folder_name(person_id)}:{reached_bytes}:{number}"


def usage_notice(
    *,
    team_id: str,
    person_id: str,
    data: StoredData,
    reached_bytes: int,
    step_bytes: int,
    number: int,
    now: datetime,
) -> AccessNoticeV1:
    """The person's notice for the step they reached: what is stored, by kind, and when they hear next."""

    kinds = sorted(
        ((label, getattr(data, name)) for name, label in _KIND_LABELS if getattr(data, name) > 0),
        key=lambda item: -item[1],
    )
    lines = [f"The team now stores about {readable_bytes(data.total)} for you:"]
    lines.extend(f"- {label}: {readable_bytes(size)}" for label, size in kinds)
    lines += [
        "",
        "Chats and completed tasks are kept. You get the next notice when your stored data passes "
        f"{readable_bytes(reached_bytes + step_bytes)}.",
        f"To see what is in your folder, open Workspace Files ({FILES_VIEW_PATH}). You can ask your assistant "
        "to delete files there that you no longer need.",
    ]
    return AccessNoticeV1(
        notice_id=usage_notice_id(person_id, reached_bytes, number),
        team_id=team_id,
        kind=AccessNoticeKind.STORAGE_USAGE,
        recipient_person_id=person_id,
        title=f"The data stored for you passed {readable_bytes(reached_bytes)}",
        summary="\n".join(lines),
        created_at_utc=now,
    )


# -- the check ---------------------------------------------------------------------------------


class StorageUsageNotices:
    """Measures the gateway's share once a day, adds the manager's and tells each person once per step crossed.

    ``step_gb`` may change while it runs (Settings > Team); each check reads
    the current number, and 0 skips measuring altogether.
    """

    def __init__(
        self,
        *,
        team_id: str,
        step_gb: int,
        people: Callable[[], Iterable[TeamPersonV1]],
        measure: Callable[[list[str]], dict[str, dict[str, int]]],
        store: Callable[[], UsageStore],
        send_notice_once: Callable[[AccessNoticeV1], bool],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        interval: timedelta = USAGE_CHECK_INTERVAL,
        first_check_delay: timedelta = FIRST_CHECK_DELAY,
    ) -> None:
        self._team_id = team_id
        self.step_gb = step_gb
        self._people = people
        self._measure = measure
        self._store = store
        self._send_notice_once = send_notice_once
        self._now = now
        self._interval = interval
        self._first_check_delay = first_check_delay
        self._first_check_at = self._now() + first_check_delay

    async def run(self, stop_event: asyncio.Event) -> None:
        if await _stopped_within(stop_event, self._first_check_delay):
            return
        while not stop_event.is_set():
            await self.run_once()
            if await _stopped_within(stop_event, self._interval):
                return

    async def run_once(self) -> int | None:
        """One check off the gateway's loop; the number of notices stored, None when it failed."""

        try:
            return await asyncio.to_thread(self.check)
        except Exception:  # noqa: BLE001 - logged; the next check runs as usual
            logger.exception("Measuring people's stored data failed; it runs again later. event=storage_usage_failed")
            return None

    def check(self) -> int:
        """Measure and record the gateway's share for everyone active, then store the notices due; how many were new."""

        step_gb = self.step_gb
        if step_gb <= 0:
            logger.info("People's storage notices are turned off. event=storage_usage_disabled")
            return 0
        now = self._now()
        people = {person.person_id: person for person in self._active_people()}
        gateway = self._measure(sorted(people))
        store = self._store()
        store.record_side(self._team_id, StorageSide.GATEWAY, gateway, measured_at=now)
        manager = store.side_usage(self._team_id, StorageSide.MANAGER)
        stored = 0
        unmeasured = 0
        for person_id, person in sorted(people.items()):
            totals = manager.get(person_id)
            if totals is None or now - totals.measured_at > STALE_AFTER:
                unmeasured += 1
                _note_unmeasured(person, totals, now)
                continue
            data = StoredData.of(gateway.get(person_id, {}), totals.bytes_by_kind)
            stored += self._announce(store, person_id, data, step_gb * GIGABYTE, now)
        logger.info(
            "Measured people's stored data. event=storage_usage_measured people=%s notices=%s unmeasured=%s step_gb=%s",
            len(people),
            stored,
            unmeasured,
            step_gb,
        )
        return stored

    def stale_people(self) -> list[str]:
        """Folder names of active people with an out-of-date measurement on either side."""

        return sorted({folder for folders in self._stale_sides().values() for folder in folders})

    def _stale_sides(self) -> dict[StorageSide, list[str]]:
        now = self._now()
        store, people = self._store(), self._active_people()
        stale = {}
        for side in StorageSide:
            rows = store.side_usage(self._team_id, side)
            folders = sorted(
                person_folder_name(person.person_id)
                for person in people
                if _stale(person, rows.get(person.person_id), now)
                and not (side is StorageSide.GATEWAY and person.person_id not in rows and now < self._first_check_at)
            )
            if folders:
                stale[side] = folders
        return stale

    def stale_finding(self) -> StatusFindingV1 | None:
        """The Status page's warning when people's totals are out of date; None while they are current or notices are off."""

        if self.step_gb <= 0:
            return None
        stale = self._stale_sides()
        if not stale:
            return None
        people = {folder for folders in stale.values() for folder in folders}
        sources = " and ".join(side.value for side in stale)
        actions = []
        if StorageSide.GATEWAY in stale:
            actions.append("Check that the gateway is running and its log for event=storage_usage_failed. It measures 15 minutes after startup and once a day.")
        if StorageSide.MANAGER in stale:
            actions.append("Check that the manager is running and its log for event=storage_usage_failed or event=storage_usage_skipped. It measures when it starts and once a day.")
        return StatusFindingV1(
            finding_id=STALE_FINDING_ID,
            severity=StatusSeverity.WARNING,
            title=f"Stored-data totals are out of date for {len(people)} person(s)",
            summary=(
                f"Measurements from the {sources} are missing or more than {STALE_AFTER.days} days old. "
                "These people may miss storage notices even if their data grows past the next step."
            ),
            action=" ".join(actions),
            source="gateway",
            details="\n".join(f"{side.value.capitalize()} folders: {', '.join(folders)}" for side, folders in stale.items())[:2000],
        )

    def _active_people(self) -> list[TeamPersonV1]:
        return [person for person in self._people() if person.standing is PersonStanding.ACTIVE]

    def _announce(self, store: UsageStore, person_id: str, data: StoredData, step_bytes: int, now: datetime) -> int:
        """Tell the person about a newly reached step, or lower the announced step after a drop; 1 when a notice was stored."""

        announced = store.announcement(self._team_id, person_id)
        reached = step_reached(data.total, step_bytes)
        if reached < announced.step_bytes:
            # Below the announced step: passing it again is a new crossing.
            store.record_announcement(self._team_id, person_id, Announcement(reached, announced.count))
            return 0
        if reached == announced.step_bytes:
            return 0
        number = announced.count + 1
        notice = usage_notice(
            team_id=self._team_id,
            person_id=person_id,
            data=data,
            reached_bytes=reached,
            step_bytes=step_bytes,
            number=number,
            now=now,
        )
        # The notice first: a restart before the step is written finds the same id.
        new = self._send_notice_once(notice)
        store.record_announcement(self._team_id, person_id, Announcement(reached, number))
        if new:
            logger.info(
                "Told a person their stored data passed another step. event=storage_usage_notice "
                "person_folder=%s step_bytes=%s total_bytes=%s notice_id=%s",
                person_folder_name(person_id),
                reached,
                data.total,
                notice.notice_id,
            )
        return 1 if new else 0


def _stale(person: TeamPersonV1, totals: SideUsage | None, now: datetime) -> bool:
    if totals is not None:
        return now - totals.measured_at > STALE_AFTER
    # No row yet: someone just added or enabled again waits for the next daily measurement.
    return now - person.changed_at_utc > STALE_AFTER


def _note_unmeasured(person: TeamPersonV1, totals: SideUsage | None, now: datetime) -> None:
    folder = person_folder_name(person.person_id)
    measured_at = totals.measured_at.isoformat() if totals is not None else "never"
    if _stale(person, totals, now):
        logger.warning(
            "No storage notice for a person: the manager's totals for them are out of date, and a partial total "
            "could hide a step. event=storage_usage_totals_stale person_folder=%s manager_measured_at=%s",
            folder,
            measured_at,
        )
    else:
        logger.info(
            "No storage notice yet for a person the manager has not measured since they joined. "
            "event=storage_usage_not_measured_yet person_folder=%s",
            folder,
        )


async def _stopped_within(stop_event: asyncio.Event, wait: timedelta) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=wait.total_seconds())
    except TimeoutError:
        return False
    return True


def build_storage_usage_notices(
    config: Any, *, chat: ChatRecords, access_store: Callable[[], Any], send_notice_once: Callable[[AccessNoticeV1], bool]
) -> StorageUsageNotices:
    """The gateway's check: its own database, the shared totals beside the access store, and the team's people."""

    from storage.person_storage_usage_repository import PersonStorageUsageRepository

    team_id = config.manager.team_id
    uploads_dir = str(config.layout.uploads_dir.expanduser().resolve())
    return StorageUsageNotices(
        team_id=team_id,
        step_gb=config.gateway.storage_warnings.person_step_gb,
        people=lambda: access_store().team_people(team_id),
        measure=lambda people: measure_gateway_side(people, chat=chat, uploads_dir=uploads_dir),
        # Opened for each use: the table is created on first use, not at gateway start.
        store=lambda: PersonStorageUsageRepository(config.manager_storage_settings),
        send_notice_once=send_notice_once,
    )


__all__ = [
    "FIRST_CHECK_DELAY",
    "GIGABYTE",
    "STALE_AFTER",
    "STALE_FINDING_ID",
    "USAGE_CHECK_INTERVAL",
    "ChatRecords",
    "StorageUsageNotices",
    "StoredData",
    "UsageStore",
    "build_storage_usage_notices",
    "measure_gateway_side",
    "readable_bytes",
    "step_reached",
    "usage_notice",
    "usage_notice_id",
]

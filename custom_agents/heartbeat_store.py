"""Canonical heartbeat store backed by the selected storage repository."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from access_control.ownership import conversation_continuation
from storage.backends import STORAGE_BACKEND_SQLITE
from default_values import STORAGE_DEFAULTS
from id_generation import new_id
from storage.config import StorageSettings, resolve_storage_settings
from storage.heartbeat_repository import (
    HeartbeatItemRecord,
    HeartbeatSourceRecord,
    create_heartbeat_repository,
)
from custom_messages.enums import ScheduleManagementClass, SystemScheduleKind

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_active_heartbeat_tasks(content: str) -> list[str]:
    active_tasks: list[str] = []
    in_active_section = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.lower() == "## active tasks":
            in_active_section = True
            continue
        if line.startswith("## "):
            in_active_section = False
        if not in_active_section:
            continue
        if line.startswith("- "):
            task = line[2:].strip()
            if task:
                active_tasks.append(task)
    return active_tasks


def is_heartbeat_launcher(scheduled_message: Any) -> bool:
    """Recognize only the protected heartbeat launcher, never a person's chosen name."""

    gateway_context = getattr(scheduled_message.payload_meta, "gateway_context", None)
    return bool(
        scheduled_message.management_class == ScheduleManagementClass.SYSTEM
        and scheduled_message.system_schedule_kind == SystemScheduleKind.HEARTBEAT_LAUNCHER
        and gateway_context
        and gateway_context.heartbeat_source_id
        and scheduled_message.schedule_name
        and str(scheduled_message.schedule_name).startswith("heartbeat:")
    )


class HeartbeatStore:
    """Persists canonical heartbeat sources and per-item recurring definitions."""

    def __init__(
        self,
        db_path: str = STORAGE_DEFAULTS.task_history_db_path.value,
        logger_: logging.Logger | None = None,
        storage_settings: Optional[StorageSettings] = None,
    ):
        self._db_path = storage_settings.sqlite_path if storage_settings and storage_settings.sqlite_path else db_path
        self._logger = logger_ or logger
        self._storage_settings = storage_settings or resolve_storage_settings(
            {"backend": STORAGE_DEFAULTS.backend.value, STORAGE_BACKEND_SQLITE: {"path": db_path}}
        )
        self._repository = create_heartbeat_repository(
            self._storage_settings,
            logger=self._logger,
        )

    def get_source(self, source_id: str) -> Optional[HeartbeatSourceRecord]:
        return self._repository.get_source(source_id)

    def upsert_source(
        self,
        *,
        source_id: str,
        title: str,
        enabled: bool = True,
    ) -> HeartbeatSourceRecord:
        existing = self.get_source(source_id)
        created_at = existing.created_at if existing else utc_now_iso()
        updated_at = utc_now_iso()
        self._repository.upsert_source(
            source_id=source_id,
            title=title,
            enabled=enabled,
            created_at=created_at,
            updated_at=updated_at,
        )
        return self.get_source(source_id)  # type: ignore[return-value]

    def ensure_source(
        self,
        *,
        source_id: str,
        title: str = "Heartbeat",
        enabled: bool = True,
    ) -> HeartbeatSourceRecord:
        existing = self.get_source(source_id)
        if existing is not None:
            return existing
        return self.upsert_source(source_id=source_id, title=title, enabled=enabled)

    def list_items(self, source_id: str, *, enabled_only: bool = False) -> list[HeartbeatItemRecord]:
        return self._repository.list_items(source_id, enabled_only=enabled_only)

    def get_item(self, heartbeat_item_id: str) -> Optional[HeartbeatItemRecord]:
        return self._repository.get_item(heartbeat_item_id)

    def add_item(
        self,
        *,
        source_id: str,
        title: str,
        instruction: str,
        conversation_id: str,
        target_agent_type: Optional[str] = None,
        objective_id: Optional[str] = None,
        enabled: bool = True,
        heartbeat_item_id: Optional[str] = None,
        source_key: Optional[str] = None,
    ) -> HeartbeatItemRecord:
        normalized_source_key = str(source_key or "").strip() or None
        if normalized_source_key:
            existing = self._repository.get_item_by_source_key(source_id, normalized_source_key)
            if existing is not None:
                return existing
        item_id = heartbeat_item_id or new_id("heartbeat_item")
        now = utc_now_iso()
        self._repository.add_item(
            heartbeat_item_id=item_id,
            source_id=source_id,
            title=title,
            instruction=instruction,
            conversation_id=conversation_id,
            target_agent_type=target_agent_type,
            objective_id=objective_id,
            enabled=enabled,
            created_at=now,
            updated_at=now,
            source_key=normalized_source_key,
        )
        return self.get_item(item_id)  # type: ignore[return-value]

    def update_item(
        self,
        heartbeat_item_id: str,
        *,
        title: Optional[str] = None,
        instruction: Optional[str] = None,
        target_agent_type: Optional[str] = None,
        objective_id: Optional[str] = None,
        expected_version: Optional[str] = None,
    ) -> Optional[HeartbeatItemRecord]:
        """Change the given fields of the stored item (None leaves a field as it is); None when it is gone.

        The repository sets them on the stored row in one write transaction,
        so another update's fields are kept. ``expected_version`` is the
        version an agent read (``heartbeat_item_version``); the change is
        refused with ``ChangeRefused`` (``changed_since_read``) when the item
        changed since.
        """

        return self._repository.update_item(
            heartbeat_item_id,
            title=title,
            instruction=instruction,
            target_agent_type=target_agent_type,
            objective_id=objective_id,
            updated_at=utc_now_iso(),
            expected_version=expected_version,
        )

    def set_item_enabled(
        self, heartbeat_item_id: str, enabled: bool, *, expected_version: Optional[str] = None
    ) -> Optional[HeartbeatItemRecord]:
        """Turn the item on or off; None when it is gone.

        An agent passes the version it read; an Objective's lifecycle change
        switching its linked items is trusted and passes none.
        """

        updated = self._repository.set_item_enabled(
            heartbeat_item_id,
            enabled,
            updated_at=utc_now_iso(),
            expected_version=expected_version,
        )
        if not updated:
            return None
        return self.get_item(heartbeat_item_id)

    def start_new_item_conversation(self, heartbeat_item_id: str) -> Optional[str]:
        """Give the item a new conversation id, where its later runs continue; None when the item is gone.

        For when the item's runs now work for someone other than the owner of
        its conversation (``item_conversation_for_run``). The caller logs why.
        """

        conversation_id = new_id("conversation_id")
        if not self._repository.set_item_conversation(heartbeat_item_id, conversation_id, updated_at=utc_now_iso()):
            return None
        return conversation_id

    def start_new_source_conversations(self, source_id: str) -> int:
        """Give every item of a heartbeat source, enabled or not, a new conversation; returns how many changed."""

        return sum(
            1
            for item in self.list_items(source_id)
            if self.start_new_item_conversation(item.heartbeat_item_id) is not None
        )

    def remove_item(self, heartbeat_item_id: str, *, expected_version: Optional[str] = None) -> bool:
        """Delete the item; False when it is gone. An agent passes the version it read."""

        return self._repository.remove_item(heartbeat_item_id, expected_version=expected_version)

    def mark_item_run_started(self, heartbeat_item_id: str) -> None:
        timestamp = utc_now_iso()
        self._repository.mark_item_run_started(heartbeat_item_id, timestamp=timestamp)

    def mark_item_run_finished(self, heartbeat_item_id: str, *, success: bool) -> None:
        timestamp = utc_now_iso()
        self._repository.mark_item_run_finished(heartbeat_item_id, success=success, timestamp=timestamp)

    def seed_items_from_markdown(
        self,
        *,
        source_id: str,
        markdown_content: str,
        conversation_id_factory,
    ) -> list[HeartbeatItemRecord]:
        created: list[HeartbeatItemRecord] = []
        source_hash = hashlib.sha256((markdown_content or "").encode("utf-8")).hexdigest()
        for index, instruction in enumerate(extract_active_heartbeat_tasks(markdown_content or ""), start=1):
            source_key = f"legacy_heartbeat_md_v1:{source_hash}:{index}"
            existing = self._repository.get_item_by_source_key(source_id, source_key)
            if existing is not None:
                continue
            created.append(
                self.add_item(
                    source_id=source_id,
                    title=instruction[:80],
                    instruction=instruction,
                    conversation_id=conversation_id_factory(),
                    source_key=source_key,
                )
            )
        return created


def item_conversation_for_run(
    store: HeartbeatStore,
    conversation_store: Any,
    item: HeartbeatItemRecord,
    person_id: Optional[str],
) -> Optional[str]:
    """The conversation a run of this item for ``person_id`` continues; None when the item is gone.

    The item's own conversation, unless it belongs to someone else: the
    launcher now runs for another person (the first user came back after an
    administrator took over, or an older install moved the launcher without
    this check). Then the item continues in a new conversation, stored on the
    item, so one person's earlier item runs are never shown to another
    person's (``custom_agents.conversation_runtime``). Checked once for every
    run, before the run is handed to a worker or started by the manager
    (which records no conversation turn itself). A departure gives the items
    new conversations when the launcher moves
    (``custom_agents.person_departure_runtime``), because a removed person's
    conversations are held then and would look unused here.
    """

    thread = conversation_store.get_thread(item.conversation_id) if conversation_store is not None else None
    if thread is None:
        return item.conversation_id
    owner = thread.get("owner_canonical_user_id")
    decision = conversation_continuation(
        person_id=person_id, owner_id=owner, conversation_id=item.conversation_id
    )
    if decision.allowed:
        return item.conversation_id
    conversation_id = store.start_new_item_conversation(item.heartbeat_item_id)
    logger.warning(
        "A heartbeat item's conversation belongs to someone other than the person its run works for, so the "
        "item continues in a new conversation. event=heartbeat_item_conversation_handed_over "
        "heartbeat_item_id=%s previous_conversation_id=%s conversation_owner=%s person_id=%s conversation_id=%s",
        item.heartbeat_item_id,
        item.conversation_id,
        owner or "-",
        person_id or "-",
        conversation_id or "-",
    )
    return conversation_id

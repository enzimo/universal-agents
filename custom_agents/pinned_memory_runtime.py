"""Shared runtime helpers for first-class coremem/usermem management."""

from __future__ import annotations

import json
import hmac
import logging
import re
import shlex
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dateparser
from default_values import AGENT_DEFAULTS

from custom_agents.memory_plugins.config import bank_agent_write_tools_enabled, normalize_memory_config
from custom_agents.pinned_memory_metadata import public_pinned_memory_metadata
from custom_messages.contracts.common_v1 import contract_sha256
from custom_messages.contracts.control_v1 import (
    PINNED_MEMORY_SORT_NEWEST,
    PinnedMemoryCommandResultPayload,
    PinnedMemoryFactPayload,
)
from storage.pinned_memory_repository import (
    COREMEM_BANK,
    USERMEM_BANK,
    PinnedMemoryConflictError,
    PinnedMemoryFactRow,
    PinnedMemoryRepository,
)
from time_presentation import format_user_timestamp


PINNED_MEMORY_BANKS = {COREMEM_BANK, USERMEM_BANK}
SORT_ALIASES = {
    PINNED_MEMORY_SORT_NEWEST: PINNED_MEMORY_SORT_NEWEST,
    "id": "memory_id",
    "memory_id": "memory_id",
    "valid_from_utc": "valid_from_utc",
    "from": "valid_from_utc",
    "expiration": "invalid_after_utc",
    "expires": "invalid_after_utc",
    "invalid_after_utc": "invalid_after_utc",
}


@dataclass(frozen=True)
class PinnedMemoryActor:
    kind: str = "user"
    id: Optional[str] = None
    display_name: Optional[str] = None

    def payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "display_name": self.display_name}


@dataclass(frozen=True)
class PinnedMemorySource:
    conversation_id: Optional[str] = None
    task_id: Optional[str] = None
    turn_index: Optional[int] = None
    gateway_session_key: Optional[str] = None
    channel_name: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PinnedMemoryRequestContext:
    team_id: Optional[str]
    canonical_user_id: Optional[str] = None
    request_timezone: Optional[str] = None
    source: PinnedMemorySource = PinnedMemorySource()
    actor: PinnedMemoryActor = PinnedMemoryActor()


@dataclass(frozen=True)
class PinnedMemoryCommand:
    bank: str
    action: str
    memory_id: Optional[int] = None
    event_id: Optional[str] = None
    fact_text: Optional[str] = None
    tags: Optional[list[str]] = None
    metadata: Optional[dict[str, Any]] = None
    valid_from_utc: Optional[str] = None
    invalid_after_utc: Optional[str] = None
    timezone_name: Optional[str] = None
    sort: str = "memory_id"
    range_start: int = 1
    range_end: int = 50
    source_key: Optional[str] = None
    expected_record_digest: Optional[str] = None


class PinnedMemoryService:
    """Typed command/service layer over pinned memory repositories."""

    def __init__(
        self,
        repository: PinnedMemoryRepository,
        *,
        config: Optional[dict[str, Any]] = None,
        command_origin: str = "manual",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.repository = repository
        self.config = normalize_memory_config(config)
        self.command_origin = command_origin
        self.logger = logger or logging.getLogger(__name__)

    def execute_command(self, command_text: str, *, bank: str, context: PinnedMemoryRequestContext) -> str:
        command = parse_pinned_memory_command(command_text, bank=bank)
        try:
            result = self.execute(command, context=context)
        except Exception as exc:
            self.logger.warning(
                "Pinned memory command failed. event=pinned_memory_command_failed bank=%s action=%s",
                command.bank,
                command.action,
                exc_info=True,
            )
            return f"{_bank_label(command.bank)} command failed: {exc}"
        return render_pinned_memory_result(
            result,
            timezone_name=context.request_timezone,
        )

    def execute(self, command: PinnedMemoryCommand, *, context: PinnedMemoryRequestContext) -> dict[str, Any]:
        bank = _normalize_bank(command.bank)
        if command.action == "list":
            return self.list(bank=bank, context=context, sort=command.sort, range_start=command.range_start, range_end=command.range_end)
        if command.action == "show":
            if command.memory_id is None:
                raise ValueError(f"{bank} show requires a memory id.")
            return self.show(bank=bank, context=context, memory_id=command.memory_id)
        if command.action == "create":
            if not command.fact_text:
                raise ValueError(f"{bank} create requires fact_text.")
            return self.create(
                bank=bank,
                context=context,
                fact_text=command.fact_text,
                tags=command.tags,
                metadata=command.metadata,
                valid_from_utc=command.valid_from_utc,
                invalid_after_utc=command.invalid_after_utc,
                timezone_name=command.timezone_name,
                source_key=command.source_key,
            )
        if command.action == "replace":
            if command.memory_id is None:
                raise ValueError(f"{bank} replace requires a memory id.")
            return self.replace(
                bank=bank,
                context=context,
                memory_id=command.memory_id,
                fact_text=command.fact_text,
                tags=command.tags,
                metadata=command.metadata,
                valid_from_utc=command.valid_from_utc,
                invalid_after_utc=command.invalid_after_utc,
                timezone_name=command.timezone_name,
                expected_record_digest=command.expected_record_digest,
            )
        if command.action == "delete":
            if command.memory_id is None:
                raise ValueError(f"{bank} delete requires a memory id.")
            return self.delete(
                bank=bank,
                context=context,
                memory_id=command.memory_id,
                expected_record_digest=command.expected_record_digest,
            )
        if command.action == "undo":
            if not command.event_id:
                raise ValueError(f"{bank} undo requires a memory event id.")
            return self.undo(bank=bank, context=context, event_id=command.event_id)
        raise ValueError(f"Unsupported {bank} action: {command.action}")

    def list(
        self,
        *,
        bank: str,
        context: PinnedMemoryRequestContext,
        sort: str = "memory_id",
        range_start: int = 1,
        range_end: int = 50,
    ) -> dict[str, Any]:
        scope = self._resolve_scope(bank, context)
        rows = self._list_rows(bank=bank, scope=scope, sort=_normalize_sort(sort))
        start = max(1, int(range_start or 1))
        end = max(start, int(range_end or start))
        visible = rows[start - 1 : end]
        return {
            "ok": True,
            "bank": bank,
            "action": "list",
            "scope": scope,
            "range": [start, end],
            "sort": _normalize_sort(sort),
            "total": len(rows),
            "returned": len(visible),
            "facts": [public_pinned_memory_fact(row) for row in visible],
        }

    def show(self, *, bank: str, context: PinnedMemoryRequestContext, memory_id: int) -> dict[str, Any]:
        scope = self._resolve_scope(bank, context)
        rows = self._list_rows(bank=bank, scope=scope, sort="memory_id")
        for row in rows:
            if row.memory_id == int(memory_id):
                return {
                    "ok": True,
                    "bank": bank,
                    "action": "show",
                    "scope": scope,
                    "memory_id": int(memory_id),
                    "fact": public_pinned_memory_fact(row),
                }
        raise ValueError(f"{bank} #{memory_id} was not found.")

    def create(
        self,
        *,
        bank: str,
        context: PinnedMemoryRequestContext,
        fact_text: str,
        tags: Optional[Iterable[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        valid_from_utc: Optional[str] = None,
        invalid_after_utc: Optional[str] = None,
        timezone_name: Optional[str] = None,
        source_key: Optional[str] = None,
    ) -> dict[str, Any]:
        self._ensure_write_allowed(bank=bank, action="create")
        scope = self._resolve_scope(bank, context)
        tz_name = _resolve_timezone(timezone_name or context.request_timezone)
        parsed_valid_from_utc = parse_memory_timestamp(valid_from_utc, timezone_name=tz_name) if valid_from_utc else None
        parsed_invalid_after_utc = (
            parse_memory_timestamp(invalid_after_utc, timezone_name=tz_name) if invalid_after_utc else None
        )
        if bank == COREMEM_BANK:
            row = self.repository.create_coremem(
                team_id=scope["team_id"],
                fact_text=fact_text,
                capacity=self.coremem_capacity,
                valid_from_utc=parsed_valid_from_utc,
                invalid_after_utc=parsed_invalid_after_utc,
                tags=self._normalize_tags(bank, tags),
                metadata=self._normalize_metadata(bank, metadata),
                source=context.source.payload(),
                actor=context.actor.payload(),
                source_key=source_key,
            )
        else:
            row = self.repository.create_usermem(
                canonical_user_id=scope["canonical_user_id"],
                fact_text=fact_text,
                capacity=self.usermem_capacity,
                valid_from_utc=parsed_valid_from_utc,
                invalid_after_utc=parsed_invalid_after_utc,
                tags=self._normalize_tags(bank, tags),
                metadata=self._normalize_metadata(bank, metadata),
                source=context.source.payload(),
                actor=context.actor.payload(),
                source_key=source_key,
            )
        event = self._latest_event(bank=bank, scope=scope, memory_id=row.memory_id, event_type="create")
        return self._result_with_fact(bank=bank, action="create", scope=scope, row=row, event_id=event.event_id if event else None)

    def replace(
        self,
        *,
        bank: str,
        context: PinnedMemoryRequestContext,
        memory_id: int,
        fact_text: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        valid_from_utc: Optional[str] = None,
        invalid_after_utc: Optional[str] = None,
        timezone_name: Optional[str] = None,
        expected_record_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        self._ensure_write_allowed(bank=bank, action="replace")
        scope = self._resolve_scope(bank, context)
        expected_updated_at_utc = self._expected_updated_at_utc(
            bank=bank,
            scope=scope,
            memory_id=memory_id,
            expected_record_digest=expected_record_digest,
        )
        tz_name = _resolve_timezone(timezone_name or context.request_timezone)
        parsed_valid_from_utc = parse_memory_timestamp(valid_from_utc, timezone_name=tz_name) if valid_from_utc else None
        parsed_invalid_after_utc = (
            parse_memory_timestamp(invalid_after_utc, timezone_name=tz_name) if invalid_after_utc else None
        )
        clear_invalid_after_utc = invalid_after_utc is not None and parsed_invalid_after_utc is None
        if bank == COREMEM_BANK:
            row = self.repository.replace_coremem(
                team_id=scope["team_id"],
                memory_id=memory_id,
                fact_text=fact_text,
                valid_from_utc=parsed_valid_from_utc,
                invalid_after_utc=parsed_invalid_after_utc,
                clear_invalid_after_utc=clear_invalid_after_utc,
                tags=self._normalize_tags(bank, tags) if tags is not None else None,
                metadata=self._normalize_metadata(bank, metadata) if metadata is not None else None,
                source=context.source.payload(),
                actor=context.actor.payload(),
                expected_updated_at_utc=expected_updated_at_utc,
            )
        else:
            row = self.repository.replace_usermem(
                canonical_user_id=scope["canonical_user_id"],
                memory_id=memory_id,
                fact_text=fact_text,
                valid_from_utc=parsed_valid_from_utc,
                invalid_after_utc=parsed_invalid_after_utc,
                clear_invalid_after_utc=clear_invalid_after_utc,
                tags=self._normalize_tags(bank, tags) if tags is not None else None,
                metadata=self._normalize_metadata(bank, metadata) if metadata is not None else None,
                source=context.source.payload(),
                actor=context.actor.payload(),
                expected_updated_at_utc=expected_updated_at_utc,
            )
        if row is None:
            raise ValueError(f"{bank} #{memory_id} was not found.")
        event = self._latest_event(bank=bank, scope=scope, memory_id=row.memory_id, event_type="replace")
        return self._result_with_fact(bank=bank, action="replace", scope=scope, row=row, event_id=event.event_id if event else None)

    def delete(
        self,
        *,
        bank: str,
        context: PinnedMemoryRequestContext,
        memory_id: int,
        expected_record_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        self._ensure_write_allowed(bank=bank, action="delete")
        scope = self._resolve_scope(bank, context)
        expected_updated_at_utc = self._expected_updated_at_utc(
            bank=bank,
            scope=scope,
            memory_id=memory_id,
            expected_record_digest=expected_record_digest,
        )
        if bank == COREMEM_BANK:
            deleted = self.repository.delete_coremem(
                team_id=scope["team_id"],
                memory_id=memory_id,
                source=context.source.payload(),
                actor=context.actor.payload(),
                expected_updated_at_utc=expected_updated_at_utc,
            )
        else:
            deleted = self.repository.delete_usermem(
                canonical_user_id=scope["canonical_user_id"],
                memory_id=memory_id,
                source=context.source.payload(),
                actor=context.actor.payload(),
                expected_updated_at_utc=expected_updated_at_utc,
            )
        if not deleted:
            raise ValueError(f"{bank} #{memory_id} was not found.")
        event = self._latest_event(bank=bank, scope=scope, memory_id=memory_id, event_type="delete")
        return {
            "ok": True,
            "bank": bank,
            "action": "delete",
            "scope": scope,
            "memory_id": memory_id,
            "event_id": event.event_id if event else None,
            "deleted": True,
            "message": f"{_bank_label(bank)} deleted: {bank} #{memory_id}",
        }

    def undo(self, *, bank: str, context: PinnedMemoryRequestContext, event_id: str) -> dict[str, Any]:
        self._ensure_write_allowed(bank=bank, action="undo")
        scope = self._resolve_scope(bank, context)
        event = self.repository.get_event(event_id)
        if event is None:
            raise ValueError(f"{bank} event {event_id!r} was not found.")
        if event.bank != bank:
            raise ValueError(f"Event {event_id!r} belongs to {event.bank}, not {bank}.")
        if bank == COREMEM_BANK and event.team_id != scope["team_id"]:
            raise ValueError(f"Event {event_id!r} does not belong to this team.")
        if bank == USERMEM_BANK and event.canonical_user_id != scope["canonical_user_id"]:
            raise ValueError(f"Event {event_id!r} does not belong to this user.")
        result = self.repository.undo_memory_event(
            event_id,
            source=context.source.payload(),
            actor=context.actor.payload(),
        )
        fact_payload = result.get("fact")
        fact = public_pinned_memory_fact_from_dict(fact_payload) if isinstance(fact_payload, dict) else None
        return {
            "ok": True,
            "bank": bank,
            "action": "undo",
            "scope": scope,
            "memory_id": result.get("memory_id"),
            "event_id": result.get("event_id"),
            "undone_event_id": event_id,
            "undone_event_type": result.get("event_type"),
            "fact": fact,
            "deleted": bool(result.get("deleted")),
            "message": f"{_bank_label(bank)} undo recorded for event {event_id}.",
        }

    @property
    def coremem_capacity(self) -> int:
        return bank_capacity(self.config, COREMEM_BANK)

    @property
    def usermem_capacity(self) -> int:
        return bank_capacity(self.config, USERMEM_BANK)

    def _ensure_write_allowed(self, *, bank: str, action: str) -> None:
        if self.command_origin != "agent_tool":
            return
        if bank_agent_write_tools_enabled(self.config, bank):
            return
        raise ValueError(
            f"{bank} {action} is disabled for agent tools by "
            f"agent.memory.{bank}.agent_write_tools_enabled=false. Use the /{bank} command instead."
        )

    def _normalize_tags(self, bank: str, value: Optional[Iterable[str]]) -> list[str]:
        return normalize_bank_tags(self.config, bank, value)

    def _normalize_metadata(self, bank: str, value: Optional[dict[str, Any]]) -> dict[str, Any]:
        return normalize_bank_metadata(self.config, bank, value)

    def _latest_event(self, *, bank: str, scope: dict[str, str], memory_id: int, event_type: str):
        return self.repository.get_latest_event(
            bank=bank,
            team_id=scope.get("team_id", ""),
            canonical_user_id=scope.get("canonical_user_id", ""),
            memory_id=memory_id,
            event_type=event_type,
        )

    @staticmethod
    def _result_with_fact(
        *,
        bank: str,
        action: str,
        scope: dict[str, str],
        row: PinnedMemoryFactRow,
        event_id: Optional[str],
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "bank": bank,
            "action": action,
            "scope": scope,
            "memory_id": row.memory_id,
            "event_id": event_id,
            "fact_preview": row.fact_text[:160],
            "fact": public_pinned_memory_fact(row),
            "message": f"{_bank_label(bank)} {action}d: {bank} #{row.memory_id}",
        }

    def _resolve_scope(self, bank: str, context: PinnedMemoryRequestContext) -> dict[str, str]:
        if bank == COREMEM_BANK:
            team_id = str(context.team_id or "").strip()
            if not team_id:
                raise ValueError("coremem requires a team_id.")
            return {"team_id": team_id}
        canonical_user_id = str(context.canonical_user_id or "").strip()
        if not canonical_user_id:
            raise ValueError("usermem requires a registered canonical user id. Use /user first.")
        return {"canonical_user_id": canonical_user_id}

    def _list_rows(self, *, bank: str, scope: dict[str, str], sort: str) -> list[PinnedMemoryFactRow]:
        if bank == COREMEM_BANK:
            return self.repository.list_coremem(team_id=scope["team_id"], sort=sort)
        return self.repository.list_usermem(canonical_user_id=scope["canonical_user_id"], sort=sort)

    def _expected_updated_at_utc(
        self,
        *,
        bank: str,
        scope: dict[str, str],
        memory_id: int,
        expected_record_digest: Optional[str],
    ) -> Optional[str]:
        digest = str(expected_record_digest or "").strip()
        if not digest:
            return None
        row = next(
            (
                candidate
                for candidate in self._list_rows(
                    bank=bank,
                    scope=scope,
                    sort="memory_id",
                )
                if candidate.memory_id == int(memory_id)
            ),
            None,
        )
        if row is None:
            raise ValueError(f"{bank} #{memory_id} was not found.")
        if not hmac.compare_digest(pinned_memory_record_digest(row), digest):
            raise PinnedMemoryConflictError(
                f"{bank} #{memory_id} changed after it was loaded. Refresh and try again."
            )
        return row.updated_at_utc


_CAPACITY_SETTING = {COREMEM_BANK: "max_facts_per_team", USERMEM_BANK: "max_facts_per_user"}


def bank_capacity(config: dict[str, Any], bank: str) -> int:
    """How many active facts one team (coremem) or one person (usermem) may hold.

    ``config`` is the normalized ``agent.memory`` settings. The service and
    the memory import both use this, so an import stops at the same limit an
    agent or a slash command would.
    """

    setting = _CAPACITY_SETTING[bank]
    default = AGENT_DEFAULTS.memory.value[bank][setting]
    return max(1, int((config.get(bank) or {}).get(setting, default)))


def normalize_bank_tags(config: dict[str, Any], bank: str, value: Optional[Iterable[str]]) -> list[str]:
    """Tags checked against the bank's tag limits in the normalized memory settings."""

    settings = dict(config.get(bank) or {})
    return normalize_tags(
        value,
        max_tags=int(settings.get("max_tags", 20)),
        max_tag_chars=int(settings.get("max_tag_chars", 64)),
    )


def normalize_bank_metadata(config: dict[str, Any], bank: str, value: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Metadata checked against the bank's size limit in the normalized memory settings."""

    settings = dict(config.get(bank) or {})
    return normalize_metadata(value, max_chars=int(settings.get("max_metadata_chars", 2000)))


def parse_pinned_memory_command(command_text: str, *, bank: str) -> PinnedMemoryCommand:
    parts = _split_command(command_text)
    if not parts:
        return PinnedMemoryCommand(bank=bank, action="list")
    if parts[0].startswith("/"):
        parts = parts[1:]
    if not parts:
        return PinnedMemoryCommand(bank=bank, action="list")
    raw_action = parts[0].strip().lower()
    if raw_action.isdigit():
        memory_id = int(raw_action)
        if len(parts) < 2:
            return PinnedMemoryCommand(bank=bank, action="show", memory_id=memory_id)
        id_first_action = parts[1].strip().lower()
        if id_first_action in {"show", "list", "view"}:
            return PinnedMemoryCommand(bank=bank, action="show", memory_id=memory_id)
        if id_first_action in {"delete", "del", "remove", "rm"}:
            return PinnedMemoryCommand(bank=bank, action="delete", memory_id=memory_id)
        if id_first_action in {"replace", "edit", "update"}:
            options = _parse_options(parts[2:])
            fact_text = _collect_fact_text(parts[2:])
            metadata_value = parse_json_option(options.get("json", options.get("meta", options.get("metadata"))))
            return PinnedMemoryCommand(
                bank=bank,
                action="replace",
                memory_id=memory_id,
                fact_text=fact_text,
                tags=normalize_tags(options.get("tags")) if "tags" in options else None,
                metadata=normalize_metadata(metadata_value) if metadata_value is not None else None,
                valid_from_utc=_first_option(options, "from", "valid-from", "valid-after", "valid_from_utc", "valid-from-utc"),
                invalid_after_utc=_first_option(
                    options,
                    "until",
                    "expires",
                    "expire",
                    "expiration",
                    "valid-until",
                    "valid_until",
                    "invalid_after_utc",
                    "invalid-after-utc",
                ),
                timezone_name=str(options.get("tz") or options.get("timezone") or "").strip() or None,
            )
        raise ValueError(f"Unknown {bank} id-first action {id_first_action!r}. Use show, replace, or delete.")
    action = {
        "add": "create",
        "create": "create",
        "new": "create",
        "list": "list",
        "ls": "list",
        "show": "show",
        "view": "show",
        "replace": "replace",
        "edit": "replace",
        "update": "replace",
        "delete": "delete",
        "del": "delete",
        "remove": "delete",
        "rm": "delete",
        "undo": "undo",
    }.get(raw_action)
    if action is None:
        raise ValueError(f"Unknown {bank} action {raw_action!r}. Use list, add, replace, or delete.")
    args = parts[1:]
    options = _parse_options(args)
    if action == "list":
        range_start, range_end = _parse_range_option(options)
        return PinnedMemoryCommand(
            bank=bank,
            action=action,
            sort=_normalize_sort(str(options.get("sort") or "memory_id")),
            range_start=range_start,
            range_end=range_end,
        )
    if action == "show":
        if not args or not str(args[0]).isdigit():
            raise ValueError(f"Usage: /{bank} show <id>")
        return PinnedMemoryCommand(bank=bank, action=action, memory_id=int(args[0]))
    if action == "delete":
        if not args or not str(args[0]).isdigit():
            raise ValueError(f"Usage: /{bank} delete <id>")
        return PinnedMemoryCommand(bank=bank, action=action, memory_id=int(args[0]))
    if action == "undo":
        if not args:
            raise ValueError(f"Usage: /{bank} undo <event_id>")
        return PinnedMemoryCommand(bank=bank, action=action, event_id=str(args[0]).strip())
    memory_id: Optional[int] = None
    fact_args = args
    if action == "replace":
        if not args or not str(args[0]).isdigit():
            raise ValueError(f"Usage: /{bank} replace <id> \"fact\" [tags=a,b] [until=...]")
        memory_id = int(args[0])
        fact_args = args[1:]
    fact_text = _collect_fact_text(fact_args)
    metadata_value = parse_json_option(options.get("json", options.get("meta", options.get("metadata"))))
    return PinnedMemoryCommand(
        bank=bank,
        action=action,
        memory_id=memory_id,
        fact_text=fact_text,
        tags=normalize_tags(options.get("tags")) if "tags" in options else None,
        metadata=normalize_metadata(metadata_value) if metadata_value is not None else None,
        valid_from_utc=_first_option(options, "from", "valid-from", "valid-after", "valid_from_utc", "valid-from-utc"),
        invalid_after_utc=_first_option(
            options,
            "until",
            "expires",
            "expire",
            "expiration",
            "valid-until",
            "valid_until",
            "invalid_after_utc",
            "invalid-after-utc",
        ),
        timezone_name=str(options.get("tz") or options.get("timezone") or "").strip() or None,
    )


def parse_memory_timestamp(raw_value: Optional[str], *, timezone_name: Optional[str] = None) -> Optional[str]:
    text = str(raw_value or "").strip()
    if not text:
        return None
    if text.lower() == "never":
        return None
    tz_name = _resolve_timezone(timezone_name)
    tz = ZoneInfo(tz_name)
    now_utc = datetime.now(timezone.utc)
    if text.lower() == "now":
        return now_utc.isoformat()
    iso_candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(timezone.utc).isoformat()
    parsed = dateparser.parse(
        text,
        settings={
            "TIMEZONE": tz_name,
            "TO_TIMEZONE": "UTC",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now_utc.astimezone(tz),
        },
    )
    if parsed is None:
        raise ValueError(f"Could not parse timestamp {raw_value!r}. Include an ISO timestamp or tz=America/Chicago.")
    return parsed.astimezone(timezone.utc).isoformat()


def normalize_tags(value: Optional[Any], *, max_tags: int = 20, max_tag_chars: int = 64) -> list[str]:
    if value is None:
        return []
    raw_items: Iterable[Any]
    if isinstance(value, str):
        raw_items = value.split(",")
    else:
        raw_items = value
    tags: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        tag = _slugify_tag(str(raw or "").strip())
        if not tag or tag in seen:
            continue
        if len(tag) > max_tag_chars:
            raise ValueError(f"tag {tag!r} exceeds max length {max_tag_chars}.")
        tags.append(tag)
        seen.add(tag)
    if len(tags) > max_tags:
        raise ValueError(f"too many tags: maximum is {max_tags}.")
    return sorted(tags)


def normalize_metadata(value: Optional[dict[str, Any]], *, max_chars: Optional[int] = None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be a JSON object.")
    normalized = dict(value)
    if max_chars is not None and len(json.dumps(normalized, sort_keys=True)) > int(max_chars):
        raise ValueError(f"metadata JSON exceeds max length {max_chars}.")
    return normalized


def parse_json_option(value: Any) -> Optional[dict[str, Any]]:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        return value
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise ValueError("meta must be a JSON object.")
    return parsed


def public_pinned_memory_fact(row: PinnedMemoryFactRow) -> dict[str, Any]:
    """The public view of a stored fact, built field by field.

    Agent memory tools and memory reviews get this view: content, validity,
    tags, the metadata the person or agent attached (skills keep progress there,
    for example the onboarding guide's ``resume_section``), the importer key
    trusted imports use to stay idempotent (``source_key``, such as
    ``onboarding:user_context:v1``; ``manager_bootstrap_runtime`` finds its
    earlier imports by it), timestamps and the revision digest. Where the fact came from (source chat, session,
    conversation, task, creator) and its internal UUID stay in storage: a Signal
    chat id is a phone number. Manager-control replies narrow it further
    (``pinned_memory_reply``).
    """

    return _public_fact(
        bank=row.bank,
        team_id=row.team_id,
        canonical_user_id=row.canonical_user_id,
        memory_id=row.memory_id,
        fact_text=row.fact_text,
        valid_from_utc=row.valid_from_utc,
        invalid_after_utc=row.invalid_after_utc,
        tags=row.tags,
        metadata=row.metadata,
        source_key=row.source_key,
        created_at_utc=row.created_at_utc,
        updated_at_utc=row.updated_at_utc,
        record_digest=pinned_memory_record_digest(row),
    )


def _public_fact(*, metadata: dict[str, Any], source_key: Optional[str], **fields: Any) -> dict[str, Any]:
    view = PinnedMemoryFactPayload(**fields).model_dump(mode="json")
    view["metadata"] = public_pinned_memory_metadata(metadata)
    view["source_key"] = source_key
    return view


def pinned_memory_reply(result: dict[str, Any]) -> PinnedMemoryCommandResultPayload:
    """The manager-control reply for a service result, built from named fields.

    The Settings memory controls show facts without metadata
    (``PinnedMemoryFactPayload``), so each fact is narrowed to the reply
    model's declared fields here rather than by Pydantic dropping the rest.
    """

    def reply_fact(fact: Any) -> PinnedMemoryFactPayload | None:
        if not isinstance(fact, dict):
            return None
        return PinnedMemoryFactPayload(**{name: fact[name] for name in REPLY_FACT_FIELDS if name in fact})

    return PinnedMemoryCommandResultPayload(
        ok=bool(result.get("ok")),
        bank=result["bank"],
        action=result["action"],
        scope=result.get("scope") or {},
        memory_id=result.get("memory_id"),
        event_id=result.get("event_id"),
        fact_preview=result.get("fact_preview"),
        fact=reply_fact(result.get("fact")),
        facts=[fact for fact in (reply_fact(item) for item in result.get("facts") or []) if fact is not None],
        range=list(result.get("range") or []),
        sort=result.get("sort"),
        total=result.get("total"),
        returned=result.get("returned"),
        deleted=bool(result.get("deleted")),
        undone_event_id=result.get("undone_event_id"),
        undone_event_type=result.get("undone_event_type"),
        message=result.get("message"),
    )


# The fields a manager-control reply shows for each fact.
REPLY_FACT_FIELDS = tuple(PinnedMemoryFactPayload.model_fields)


def pinned_memory_record_digest(row: PinnedMemoryFactRow) -> str:
    """Bind a browser mutation to one exact active memory revision."""

    return _pinned_memory_record_digest_values(asdict(row))


def _pinned_memory_record_digest_values(row: dict[str, Any]) -> str:
    return contract_sha256(
        {
            "bank": row.get("bank"),
            "team_id": row.get("team_id"),
            "canonical_user_id": row.get("canonical_user_id"),
            "memory_id": row.get("memory_id"),
            "fact_uuid": row.get("fact_uuid"),
            "updated_at_utc": row.get("updated_at_utc"),
        }
    )


def public_pinned_memory_fact_from_dict(row: dict[str, Any]) -> dict[str, Any]:
    """``public_pinned_memory_fact`` for a stored row the repository returned as a dict."""

    return _public_fact(
        bank=row["bank"],
        team_id=row.get("team_id") or "",
        canonical_user_id=row.get("canonical_user_id") or "",
        memory_id=row["memory_id"],
        fact_text=row["fact_text"],
        valid_from_utc=row["valid_from_utc"],
        invalid_after_utc=row.get("invalid_after_utc"),
        tags=list(json.loads(str(row.get("tags_json") or "[]"))),
        metadata=dict(json.loads(str(row.get("metadata_json") or "{}"))),
        source_key=row.get("source_key"),
        created_at_utc=row["created_at_utc"],
        updated_at_utc=row["updated_at_utc"],
        record_digest=_pinned_memory_record_digest_values(row),
    )


def render_pinned_memory_result(
    result: dict[str, Any], *, timezone_name: str | None = None
) -> str:
    bank = str(result.get("bank") or "")
    label = _bank_label(bank)
    action = str(result.get("action") or "")
    if action == "list":
        facts = list(result.get("facts") or [])
        scope = result.get("scope") or {}
        scope_text = scope.get("team_id") or scope.get("canonical_user_id") or "unknown"
        lines = [
            (
                f"{label} for {scope_text}: {result.get('returned', 0)} of "
                f"{result.get('total', 0)} active facts "
                f"(range {result.get('range', ['?', '?'])[0]}-"
                f"{result.get('range', ['?', '?'])[1]}, sort {result.get('sort')})."
            )
        ]
        if not facts:
            lines.append("No active facts.")
            return "\n".join(lines)
        for fact in facts:
            lines.append(_render_fact_line(fact, timezone_name=timezone_name))
        return "\n".join(lines)
    if action == "show":
        fact = dict(result.get("fact") or {})
        return _render_fact_line(fact, timezone_name=timezone_name)
    if action == "create":
        fact = dict(result.get("fact") or {})
        event_id = result.get("event_id") or "unknown"
        return f"{label} saved: {bank} #{fact.get('memory_id')} (event {event_id})\nUndo: /{bank} undo {event_id}"
    if action == "replace":
        fact = dict(result.get("fact") or {})
        event_id = result.get("event_id") or "unknown"
        return f"{label} replaced: {bank} #{fact.get('memory_id')} (event {event_id})\nUndo: /{bank} undo {event_id}"
    if action == "delete":
        event_id = result.get("event_id") or "unknown"
        return f"{label} deleted: {bank} #{result.get('memory_id')} (event {event_id})\nUndo: /{bank} undo {event_id}"
    if action == "undo":
        return f"{label} undo recorded for event {result.get('undone_event_id')} (event {result.get('event_id')})."
    return json.dumps(result, default=str)


def _render_fact_line(
    fact: dict[str, Any], *, timezone_name: str | None
) -> str:
    bits = [f"#{fact.get('memory_id')}: {fact.get('fact_text')}"]
    tags = fact.get("tags") or []
    if tags:
        bits.append(f"tags={','.join(str(tag) for tag in tags)}")
    if fact.get("invalid_after_utc"):
        bits.append(
            "invalid_after="
            f"{format_user_timestamp(fact.get('invalid_after_utc'), timezone_name=timezone_name)}"
        )
    if fact.get("valid_from_utc"):
        bits.append(
            "valid_from="
            f"{format_user_timestamp(fact.get('valid_from_utc'), timezone_name=timezone_name)}"
        )
    return " | ".join(bits)


def _split_command(command_text: str) -> list[str]:
    try:
        return shlex.split(str(command_text or ""))
    except ValueError as exc:
        raise ValueError(f"Could not parse command: {exc}") from exc


def _parse_options(args: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    idx = 0
    while idx < len(args):
        token = args[idx]
        if "=" in token:
            key, value = token.split("=", 1)
            options[key.strip().lower().lstrip("-")] = value.strip()
        elif token.lower().lstrip("-") in _OPTION_WORDS and idx + 1 < len(args):
            options[token.lower().lstrip("-")] = args[idx + 1].strip()
            idx += 1
        idx += 1
    return options


def _collect_fact_text(args: list[str]) -> str:
    fact_parts: list[str] = []
    skip_next = False
    for token in args:
        lowered = token.lower().lstrip("-")
        if skip_next:
            skip_next = False
            continue
        if "=" in token:
            continue
        if lowered in _OPTION_WORDS:
            skip_next = True
            continue
        fact_parts.append(token)
    return " ".join(fact_parts).strip()


def _parse_range_option(options: dict[str, Any]) -> tuple[int, int]:
    raw = str(options.get("range") or "1-50").strip()
    if "-" in raw:
        left, right = raw.split("-", 1)
        start = int(left.strip())
        end = int(right.strip())
    else:
        start = 1
        end = int(raw)
    if start < 1 or end < start:
        raise ValueError("range must be positive and ascending, e.g. range=1-50.")
    return start, end


def _normalize_bank(bank: str) -> str:
    normalized = str(bank or "").strip().lower()
    if normalized not in PINNED_MEMORY_BANKS:
        raise ValueError(f"Unsupported pinned memory bank {bank!r}.")
    return normalized


def _normalize_sort(sort: str) -> str:
    return SORT_ALIASES.get(str(sort or "memory_id").strip().lower(), "memory_id")


def _first_option(options: dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = options.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _slugify_tag(value: str) -> str:
    lowered = value.strip().lower()
    lowered = re.sub(r"[^a-z0-9_-]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered)
    return lowered.strip("-_")


def _resolve_timezone(timezone_name: Optional[str]) -> str:
    candidate = str(timezone_name or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(candidate).key
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone {candidate!r}. Use an IANA timezone such as America/Chicago.") from exc


def _bank_label(bank: str) -> str:
    return "Core memory" if bank == COREMEM_BANK else "User memory"


_OPTION_WORDS = {
    "range",
    "sort",
    "tags",
    "from",
    "valid-from",
    "valid-after",
    "valid_from_utc",
    "valid-from-utc",
    "until",
    "expires",
    "expire",
    "expiration",
    "valid-until",
    "valid_until",
    "invalid_after_utc",
    "invalid-after-utc",
    "tz",
    "timezone",
    "json",
    "meta",
    "metadata",
}

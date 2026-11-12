"""The files a conversation turn received, as the conversation store keeps them.

Each turn row (``conversation_task_links``) keeps its files in the
``attachments_json`` column: a JSON list of full ``UserTaskAttachment``
records, one per ``attachment_id``, in the order they arrived (the request's
own files first, then files sent with guidance or answers while it ran).

Only trusted runtime code writes them, from the gateway's records on the task's
messages. The full records include where the bytes are stored, so they leave
the store only through ``list_turn_attachments`` for trusted callers. Generic
turn reads, which reach models through prompts and conversation tools, carry
the descriptive fields only (``TURN_ATTACHMENT_SUMMARY_FIELDS``).

A NULL column means the turn was saved before turns kept their files; ``[]``
means it had none. Models are told the difference, so they do not read an
unrecorded turn as one without files: turn reads carry
``attachments_recorded`` and the prompt's file list says up to which turn
files were not recorded (``last_turn_without_file_records``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from custom_messages.contracts.attachments_v1 import UserTaskAttachment

# The fields of a file record a model may see: no storage references, no checksum.
TURN_ATTACHMENT_SUMMARY_FIELDS = (
    "attachment_id",
    "filename",
    "content_type",
    "kind",
    "size_bytes",
    "created_at",
    "caption",
)


def attachment_summary(attachment: UserTaskAttachment) -> dict[str, Any]:
    """The descriptive fields of one file record, without where it is stored."""

    dumped = attachment.model_dump(mode="json")
    return {field: dumped.get(field) for field in TURN_ATTACHMENT_SUMMARY_FIELDS}


@dataclass(frozen=True)
class TurnAttachment:
    """One file a conversation turn received, with the turn it belongs to."""

    turn_index: int
    task_id: str
    attachment: UserTaskAttachment

    def summary(self) -> dict[str, Any]:
        """Descriptive fields and the turn's ids; never storage references."""

        return {**attachment_summary(self.attachment), "turn_index": self.turn_index, "task_id": self.task_id}


def encode_turn_attachments(attachments: Sequence[UserTaskAttachment]) -> str:
    return json.dumps([attachment.model_dump(mode="json") for attachment in attachments])


def decode_turn_attachments(
    raw: Any,
    *,
    logger: logging.Logger,
    conversation_id: str,
    task_id: str,
) -> list[UserTaskAttachment]:
    """The records stored in one turn's column; unreadable entries are logged and skipped.

    Only this module's encoder writes the column, so an unreadable value is a
    defect worth a warning, not a normal state.
    """

    if raw in (None, ""):
        return []
    try:
        items = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning(
            "event=conversation_turn_attachments_unreadable conversation_id=%s task_id=%s reason=invalid_json",
            conversation_id,
            task_id,
        )
        return []
    if not isinstance(items, list):
        logger.warning(
            "event=conversation_turn_attachments_unreadable conversation_id=%s task_id=%s reason=not_a_list",
            conversation_id,
            task_id,
        )
        return []
    records: list[UserTaskAttachment] = []
    for position, item in enumerate(items):
        try:
            records.append(UserTaskAttachment.model_validate(item))
        except ValidationError:
            attachment_id = item.get("attachment_id") if isinstance(item, dict) else None
            logger.warning(
                "event=conversation_turn_attachment_unreadable conversation_id=%s task_id=%s position=%d "
                "attachment_id=%s",
                conversation_id,
                task_id,
                position,
                attachment_id,
            )
    return records


def merge_unique(
    existing: Sequence[UserTaskAttachment],
    incoming: Iterable[UserTaskAttachment],
    *,
    logger: logging.Logger,
    conversation_id: str,
    task_id: str,
) -> tuple[list[UserTaskAttachment], int]:
    """``existing`` followed by the incoming records whose ids it lacks, and how many were added.

    The first record for an id wins. A later record with the same id but other
    details is not saved and is logged: the gateway gives each upload its own
    id, so a different record under a known id is an anomaly, not an update.
    """

    merged = list(existing)
    by_id = {attachment.attachment_id: attachment for attachment in merged}
    added = 0
    for attachment in incoming:
        known = by_id.get(attachment.attachment_id)
        if known is None:
            merged.append(attachment)
            by_id[attachment.attachment_id] = attachment
            added += 1
        elif known != attachment:
            logger.warning(
                "event=conversation_turn_attachment_conflict conversation_id=%s task_id=%s attachment_id=%s "
                "kept=first_record",
                conversation_id,
                task_id,
                attachment.attachment_id,
            )
    return merged, added


class NewestFilesFirst:
    """Picks a conversation's files from its turns, given newest turn first.

    Within a turn the file that arrived last comes first. Each id is kept once,
    with the newest turn that recorded it. ``attachment_ids`` keeps only those
    ids; ``limit`` stops after that many files. ``done`` says when reading
    older turns can stop.
    """

    def __init__(self, *, attachment_ids: Collection[str] | None = None, limit: int | None = None) -> None:
        self._wanted = None if attachment_ids is None else set(attachment_ids)
        self._limit = limit
        self._seen: set[str] = set()
        self.found: list[TurnAttachment] = []

    @property
    def done(self) -> bool:
        if self._limit is not None and len(self.found) >= self._limit:
            return True
        return self._wanted is not None and self._seen >= self._wanted

    def add_turn(self, turn_index: int, task_id: str, attachments: Sequence[UserTaskAttachment]) -> None:
        """Add one turn's files, given in the order they arrived."""

        for attachment in reversed(attachments):
            if self.done:
                return
            if attachment.attachment_id in self._seen:
                continue
            if self._wanted is not None and attachment.attachment_id not in self._wanted:
                continue
            self._seen.add(attachment.attachment_id)
            self.found.append(TurnAttachment(turn_index=turn_index, task_id=task_id, attachment=attachment))

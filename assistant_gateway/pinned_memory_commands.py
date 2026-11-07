"""The gateway's pinned-memory commands: the chat grammar, the contract's bodies and replies, and rendering.

People's memory commands go to the team services as protocol 1 requests
(``memory.list`` and the others, ``team_services.memory``). This adapter keeps the
manager's command grammar (``custom_agents.pinned_memory_runtime``) and its chat
rendering, and converts between them and the contract:

- times a person writes ("tomorrow 9am", or an ISO time without a zone) are read in
  their own time zone here, before sending, because the contract carries UTC times only;
  ``never`` as an expiry clears it (``clear_expiry``);
- a list asks for at most 100 facts (the contract's page size), from the range's start;
- a typed reply becomes the result view the Settings page and the chat renderer already
  use (``PinnedMemoryCommandResultPayload``).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Optional

from custom_agents.pinned_memory_runtime import (
    parse_memory_timestamp,
    parse_pinned_memory_command,
    render_pinned_memory_result,
)
from custom_messages.contracts.control_v1 import (
    PINNED_MEMORY_SORT_NEWEST,
    PinnedMemoryCommandPayload,
    PinnedMemoryCommandResultPayload,
    PinnedMemoryFactPayload,
)
from custom_messages.wire.encoding import format_timestamp

COREMEM = "coremem"
# The memory service's sort names, as the contract names them.
WIRE_SORTS = {
    "memory_id": "memory_id",
    PINNED_MEMORY_SORT_NEWEST: "newest",
    "valid_from_utc": "valid_from",
    "invalid_after_utc": "expires",
}
WIRE_PAGE_LIMIT = 100
_INLINE = "inline"


class RuntimePinnedMemoryCodec:
    """Adapt the manager's grammar and rendering to the gateway's memory commands (module docstring)."""

    def parse(self, command: str, *, bank: str) -> PinnedMemoryCommandPayload:
        return PinnedMemoryCommandPayload(**asdict(parse_pinned_memory_command(command, bank=bank)))

    def wire_command(self, command: PinnedMemoryCommandPayload, *, timezone_name: Optional[str]) -> tuple[str, dict[str, Any]]:
        """The contract's kind and body (without ``request_id``) for a parsed command."""

        zone = command.timezone_name or timezone_name
        body: dict[str, Any] = {"bank": command.bank}
        if command.action == "list":
            start = max(1, int(command.range_start or 1))
            end = max(start, int(command.range_end or start))
            body.update(sort=WIRE_SORTS.get(command.sort, "memory_id"), offset=start - 1,
                        limit=min(WIRE_PAGE_LIMIT, end - start + 1))
        elif command.action == "undo":
            if not command.event_id:
                raise ValueError(f"{command.bank} undo requires a memory event id.")
            body["event_id"] = command.event_id
        elif command.action != "create":
            if command.memory_id is None:
                raise ValueError(f"{command.bank} {command.action} requires a memory id.")
            body["memory_id"] = int(command.memory_id)
        if command.action in {"replace", "delete"} and command.expected_record_digest:
            body["seen_version"] = command.expected_record_digest
        if command.action in {"create", "replace"}:
            body.update(_fact_fields(command, zone))
        return f"memory.{command.action}", body

    def result(self, command: PinnedMemoryCommandPayload, reply: dict[str, Any], *, team_id: str,
               person_id: str) -> PinnedMemoryCommandResultPayload:
        """The result view for an ``ok`` reply."""

        scope = {"team_id": team_id} if command.bank == COREMEM else {"canonical_user_id": person_id}
        owner = {"team_id": team_id if command.bank == COREMEM else "",
                 "canonical_user_id": "" if command.bank == COREMEM else person_id}

        def fact(wire: Optional[dict[str, Any]]) -> Optional[PinnedMemoryFactPayload]:
            return None if wire is None else _fact_view(command.bank, owner, wire)

        values: dict[str, Any] = {"ok": True, "bank": command.bank, "action": command.action, "scope": scope}
        if command.action == "list":
            facts = [fact(item) for item in reply["facts"]]
            start = max(1, int(command.range_start or 1))
            values.update(facts=facts, total=reply["total"], returned=len(facts), sort=command.sort,
                          range=[start, max(start, int(command.range_end or start))])
        elif command.action == "undo":
            values.update(event_id=reply["event_id"], undone_event_id=reply["undone_event_id"],
                          undone_event_type=reply["undone_event_type"], deleted=reply["deleted"],
                          fact=fact(reply.get("fact")))
        elif command.action == "delete":
            values.update(memory_id=reply["memory_id"], event_id=reply["event_id"], deleted=True)
        else:
            shown = fact(reply["fact"])
            values.update(fact=shown, memory_id=shown.memory_id if shown else None, event_id=reply.get("event_id"),
                          fact_preview=shown.fact_text[:160] if shown else None)
        return PinnedMemoryCommandResultPayload(**values)

    def render(self, result: PinnedMemoryCommandResultPayload, *, timezone_name: Optional[str]) -> str:
        return render_pinned_memory_result(result.model_dump(mode="json"), timezone_name=timezone_name)


def _fact_fields(command: PinnedMemoryCommandPayload, zone: Optional[str]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if command.fact_text is not None:
        fields["fact_text"] = {"type": _INLINE, "text": command.fact_text}
    if command.tags is not None:
        fields["tags"] = list(command.tags)
    if command.metadata is not None:
        fields["metadata_json"] = json.dumps(command.metadata, separators=(",", ":"), sort_keys=True)
    if command.valid_from_utc:
        valid_from = parse_memory_timestamp(command.valid_from_utc, timezone_name=zone)
        if valid_from:
            fields["valid_from"] = _wire_time(valid_from)
    if command.invalid_after_utc is not None:
        expires = parse_memory_timestamp(command.invalid_after_utc, timezone_name=zone)
        if expires:
            fields["expires"] = _wire_time(expires)
        elif command.action == "replace":
            fields["clear_expiry"] = True
    return fields


def _wire_time(value: str) -> str:
    return format_timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _fact_view(bank: str, owner: dict[str, str], wire: dict[str, Any]) -> PinnedMemoryFactPayload:
    return PinnedMemoryFactPayload(
        bank=bank,
        team_id=owner["team_id"],
        canonical_user_id=owner["canonical_user_id"],
        memory_id=wire["memory_id"],
        fact_text=wire["fact_text"]["text"],
        valid_from_utc=wire["valid_from"],
        invalid_after_utc=wire.get("expires"),
        tags=list(wire.get("tags") or []),
        created_at_utc=wire["created_at"],
        updated_at_utc=wire["updated_at"],
        record_digest=wire["seen_version"],
    )

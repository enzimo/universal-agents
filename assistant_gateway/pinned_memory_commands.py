"""The gateway's pinned-memory commands: the chat grammar, the contract's bodies and replies, and rendering.

People's memory commands go to the team services as protocol 1 requests
(``memory.list`` and the others, ``team_services.memory``). This adapter keeps the
manager's command grammar (``custom_agents.pinned_memory_runtime``) and its chat
rendering, and converts between them and the contract:

- times a person writes ("tomorrow 9am", or an ISO time without a zone) are read in
  their own time zone here, before sending, because the contract carries UTC times only;
  ``never`` as an expiry clears it (``clear_expiry``);
- a list asks for at most 100 facts (the contract's page size), from the range's start;
  the team services may return fewer (README "Reply size"), and the result names the
  positions it shows;
- a body the contract would refuse is refused here, before sending, in plain words
  (``PinnedMemoryInputError``): an empty fact, a fact longer than a frame carries inline
  (``INLINE_TEXT_MAX_CHARS``; the person keeps their text and can shorten it, as plan
  decision 77 has it for knowledge), a replace that changes nothing, too many or too long
  tags;
- a typed reply becomes the result view the Settings page and the chat renderer already
  use (``PinnedMemoryCommandResultPayload``).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Optional

from pydantic import ValidationError

from custom_agents.pinned_memory_runtime import (
    parse_memory_timestamp,
    parse_pinned_memory_command,
    render_pinned_memory_result,
)
from custom_messages.contracts.control_v1 import (
    PINNED_MEMORY_WIRE_SORTS,
    PinnedMemoryCommandPayload,
    PinnedMemoryCommandResultPayload,
    PinnedMemoryFactPayload,
)
from storage.pinned_memory_repository import COREMEM_BANK, PinnedMemoryInputError

# The contract's page size for memory.list.
WIRE_PAGE_LIMIT = 100
# The contract's text_content type for text carried in the frame (custom_messages.wire.sizes.INLINE_TEXT_TYPE);
# the contract's models load only when a command is checked (_check_body), not when the gateway imports this.
_INLINE_TEXT_TYPE = "inline"
# The fields a memory.replace may change; it must name at least one (registry body rule).
_REPLACE_FIELDS = ("fact_text", "tags", "metadata_json", "valid_from", "expires", "clear_expiry")
# How a person knows each body field, for refusals in plain words.
_FIELD_NAMES = {
    "fact_text": "The fact",
    "tags": "The tags",
    "metadata_json": "The metadata",
    "valid_from": "The start time",
    "expires": "The expiry",
    "event_id": "The event id",
    "memory_id": "The memory number",
    "seen_version": "The memory version",
}


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
            body.update(sort=PINNED_MEMORY_WIRE_SORTS.get(command.sort, "memory_id"), offset=start - 1,
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
        kind = f"memory.{command.action}"
        _check_body(kind, body)
        return kind, body

    def result(self, command: PinnedMemoryCommandPayload, reply: dict[str, Any], *, team_id: str,
               person_id: str) -> PinnedMemoryCommandResultPayload:
        """The result view for an ``ok`` reply."""

        team_bank = command.bank == COREMEM_BANK
        scope = {"team_id": team_id} if team_bank else {"canonical_user_id": person_id}
        owner = {"team_id": team_id if team_bank else "", "canonical_user_id": "" if team_bank else person_id}

        def fact(wire: Optional[dict[str, Any]]) -> Optional[PinnedMemoryFactPayload]:
            return None if wire is None else _fact_view(command.bank, owner, wire)

        values: dict[str, Any] = {"ok": True, "bank": command.bank, "action": command.action, "scope": scope}
        if command.action == "list":
            facts = [fact(item) for item in reply["facts"]]
            start = max(1, int(command.range_start or 1))
            # The positions shown: the team services may return fewer facts than asked (module docstring).
            shown_end = start + len(facts) - 1 if facts else max(start, int(command.range_end or start))
            values.update(facts=facts, total=reply["total"], returned=len(facts), sort=command.sort,
                          range=[start, shown_end])
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


def _check_body(kind: str, body: dict[str, Any]) -> None:
    """Refuse in plain words a body the contract would refuse (module docstring)."""

    # Imported here: command-line runs import the gateway and this module, and should not load the
    # contract's generated models (only the gateway service sends memory commands).
    from custom_messages.wire.frame import body_model
    from custom_messages.wire.sizes import INLINE_TEXT_MAX_CHARS

    text = body.get("fact_text", {}).get("text")
    if kind == "memory.create" and not str(text or "").strip():
        raise PinnedMemoryInputError("The fact's text cannot be empty.")
    if text is not None and len(text) > INLINE_TEXT_MAX_CHARS:
        raise PinnedMemoryInputError(f"The fact is {len(text):,} characters long, and Web Chat accepts up to "
                                     f"{INLINE_TEXT_MAX_CHARS:,}. Shorten it and send it again.")
    if kind == "memory.replace" and not any(field in body for field in _REPLACE_FIELDS):
        raise PinnedMemoryInputError("Say what to change: new text, tags, a start time or an expiry.")
    try:
        body_model(kind)[0].validate_python({"request_id": "check", **body})
    except ValidationError as error:
        raise PinnedMemoryInputError(_plain_refusal(error)) from error


def _plain_refusal(error: ValidationError) -> str:
    """The first problem the contract's model found, in words a person knows."""

    problem = error.errors()[0]
    field = str(problem["loc"][0]) if problem["loc"] else ""
    name = _FIELD_NAMES.get(field, f"The field {field}")
    limit = (problem.get("ctx") or {}).get("max_length")
    kind = problem["type"]
    if kind == "string_too_short":
        return f"{name} cannot be empty."
    if kind == "string_too_long" and limit is not None:
        return f"{name} is too long: at most {limit:,} characters."
    if kind == "too_long" and limit is not None:
        return f"{name}: at most {limit:,} are allowed."
    return f"{name} is not something Web Chat can send ({problem['msg']})."


def _fact_fields(command: PinnedMemoryCommandPayload, zone: Optional[str]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if command.fact_text is not None:
        fields["fact_text"] = {"type": _INLINE_TEXT_TYPE, "text": command.fact_text}
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
    from custom_messages.wire.encoding import format_timestamp  # loads the contract's models: see _check_body

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

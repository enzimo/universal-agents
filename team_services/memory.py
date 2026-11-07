"""People's memory commands at the team's address: ``memory.list``, ``.show``, ``.create``,
``.replace``, ``.delete`` and ``.undo``.

Until phase 2 the gateway sent these to the team lead's process as a durable
``ManagerMemoryCommand``; they now run here, with no model, on the same database and the
same service (``custom_agents.pinned_memory_runtime.PinnedMemoryService``), so what a
command does is unchanged (plan 13B.4). What changed:

- **Who acts** comes from the command's authority token only (``team_services.people``),
  and the same ownership rule decides as before (``access_control.ownership``): anyone
  in the team reads team memory, administrators change it, and personal memory is the
  person's own.
- **Replies are typed** (the contract's ``memory.*.reply`` bodies); the gateway renders
  them for chats and the Settings page.
- **A change is carried out once per command.** Its reply is stored with the change, in
  the same transaction, under the command's retry key, and a repeat gets it back
  (``storage.operation_outcomes``); refusals are stored too.
- **Where a fact came from** is no longer recorded for people's commands: the command
  names no chat, so the fact's events record the person only.

The repository is synchronous, so each command runs in a worker thread, and the team
services' lock and host upkeep keep running meanwhile.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from access_control.ownership import (
    PRIVATE_CREATE,
    PRIVATE_DELETE,
    PRIVATE_READ,
    PRIVATE_UPDATE,
    RESOURCE_TEAM_MEMORY,
    RESOURCE_USER_MEMORY,
    private_access,
)
from access_control.people_directory import PeopleDirectory
from custom_agents.pinned_memory_runtime import (
    PinnedMemoryActor,
    PinnedMemoryCommand,
    PinnedMemoryRequestContext,
    PinnedMemoryService,
    public_pinned_memory_fact,
    public_pinned_memory_fact_from_dict,
)
from custom_messages.contracts.control_v1 import PINNED_MEMORY_SORT_NEWEST
from custom_messages.enums import ChangeRefusal
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame
from storage.change_ledger import ChangeRefused, ChangeWriter, ChangeWriterKind
from storage.operation_outcomes import CommandOutcome, OutcomeAlreadyStored
from storage.pinned_memory_repository import (
    COREMEM_BANK,
    PinnedMemoryCapacityError,
    PinnedMemoryConflictError,
    PinnedMemoryNotFoundError,
    PinnedMemoryRepository,
    PinnedMemoryWriteResult,
)
from team_services.people import CommandPerson, command_person

MEMORY_ACTIONS = ("list", "show", "create", "replace", "delete", "undo")
MEMORY_KINDS = tuple(f"memory.{action}" for action in MEMORY_ACTIONS)
_CHANGES = frozenset({"create", "replace", "delete", "undo"})
_ACCESS = {"list": PRIVATE_READ, "show": PRIVATE_READ, "create": PRIVATE_CREATE, "delete": PRIVATE_DELETE}
# The contract's sort names, as the memory service names them.
SERVICE_SORTS = {
    "memory_id": "memory_id",
    "newest": PINNED_MEMORY_SORT_NEWEST,
    "valid_from": "valid_from_utc",
    "expires": "invalid_after_utc",
}
# The contract's short_text: every reply message fits it.
_MESSAGE_MAX = 500
_INLINE = "inline"
_CLEAR_EXPIRY = "never"


class MemoryOperations:
    """Answers people's memory commands for one team (module docstring)."""

    def __init__(self, repository: PinnedMemoryRepository, people: PeopleDirectory, memory_config: dict[str, Any],
                 clock: Callable[[], datetime], logger: logging.Logger) -> None:
        self._repository = repository
        self._people = people
        self._service = PinnedMemoryService(repository, config=memory_config, command_origin="gateway", logger=logger)
        self._clock = clock
        self._logger = logger

    def handlers(self) -> dict[str, Callable[[VerifiedFrame], Any]]:
        return {kind: self.handle for kind in MEMORY_KINDS}

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        return await asyncio.to_thread(self.answer, verified)

    def answer(self, verified: VerifiedFrame) -> dict[str, Any]:
        """The reply to one memory command (blocking: the store is synchronous)."""

        action = verified.frame.kind.removeprefix("memory.")
        body = verified.raw["body"]
        person, reason = command_person(verified, self._people, self._logger)
        if person is None:
            return _message_reply("not_allowed", reason)
        decision = _memory_access(action, body, person)
        if not decision.allowed:
            self._logger.warning("Refused a memory command. event=team_memory_command_denied person=%s bank=%s "
                                 "action=%s reason=%s", person.person_id, body["bank"], action, decision.reason)
            return _message_reply("not_allowed", "Only administrators change team memory."
                                  if body["bank"] == COREMEM_BANK else "This memory is not yours.")
        replies: list[dict[str, Any]] = []
        outcome = None
        if action in _CHANGES:
            outcome = CommandOutcome(
                team_id=person.team_id, retry_key=str(verified.raw["idempotency_key"]), kind=verified.frame.kind,
                body_digest=str(verified.raw["body_digest"]), now=self._clock(),
                reply_for=lambda result: _kept(replies, _change_reply(action, result)), logger=self._logger,
            )
        try:
            command = _service_command(action, body)
            result = self._service.execute(command, context=_context(person), command_outcome=outcome)
        except OutcomeAlreadyStored as stored:
            return stored.reply
        except Exception as error:  # noqa: BLE001 - every failure is answered and logged in _refusal
            reply = self._refusal(action, body, person, error)
            if outcome is not None:
                self._store_refusal(outcome, reply)
            return reply
        if outcome is not None:
            return replies[-1]
        return _read_reply(action, body, result)

    def _refusal(self, action: str, body: dict[str, Any], person: CommandPerson, error: Exception) -> dict[str, Any]:
        if isinstance(error, PinnedMemoryNotFoundError):
            return _message_reply("not_found", str(error))
        if isinstance(error, PinnedMemoryConflictError) or (
            isinstance(error, ChangeRefused) and error.reason is ChangeRefusal.CHANGED_SINCE_READ
        ):
            return self._changed_since_read(action, body, person, str(error))
        if isinstance(error, PinnedMemoryCapacityError) and action == "create":
            return _message_reply("capacity_reached", str(error))
        if isinstance(error, ChangeRefused) and error.reason is ChangeRefusal.REQUEST_CLOSED:
            return _message_reply("request_closed", str(error))
        if isinstance(error, ChangeRefused) and error.reason is ChangeRefusal.NEEDS_PERSON_DECISION:
            return _message_reply("needs_person_decision", str(error))
        if isinstance(error, ValueError) and action in _CHANGES:
            return _message_reply("invalid_request", str(error))
        self._logger.error("A memory command failed. event=team_memory_command_failed person=%s bank=%s action=%s "
                           "error_type=%s", person.person_id, body.get("bank"), action, type(error).__name__,
                           exc_info=error)
        return _message_reply("runtime_error", "The memory command failed; nothing was changed.")

    def _changed_since_read(self, action: str, body: dict[str, Any], person: CommandPerson,
                            message: str) -> dict[str, Any]:
        """``changed_since_read`` names the fact's version now, which the person's next try can name."""

        if action == "undo":
            return _message_reply("changed_since_read", message)
        try:
            current = self._service.show(bank=body["bank"], context=_context(person), memory_id=int(body["memory_id"]))
        except PinnedMemoryNotFoundError as error:
            return _message_reply("not_found", str(error))
        return {"outcome": "changed_since_read", "current_version": current["fact"]["record_digest"],
                "message": _clip(message)}

    def _store_refusal(self, outcome: CommandOutcome, reply: dict[str, Any]) -> None:
        try:
            self._repository.save_command_refusal(outcome, reply)
        except Exception:
            # The refusal still answers; a retry is decided again, and refused again, without it.
            self._logger.exception("Could not store a refused command's reply. event=team_memory_refusal_not_stored "
                                   "kind=%s retry_key=%s outcome=%s", outcome.kind, outcome.retry_key,
                                   reply.get("outcome"))


def _kept(replies: list[dict[str, Any]], reply: dict[str, Any]) -> dict[str, Any]:
    replies.append(reply)
    return reply


def _memory_access(action: str, body: dict[str, Any], person: CommandPerson):
    """The rule the team lead's process applied (``_authorize_manager_memory_command`` until phase 2)."""

    team_memory = body["bank"] == COREMEM_BANK
    record = body.get("memory_id") or body.get("event_id") or "collection"
    return private_access(
        person_id=person.person_id,
        owner_id=None if team_memory else person.person_id,
        resource_type=RESOURCE_TEAM_MEMORY if team_memory else RESOURCE_USER_MEMORY,
        resource_id=f"{body['bank']}:{record}",
        action=_ACCESS.get(action, PRIVATE_UPDATE),
        team_id=person.team_id,
        roles=person.roles,
    )


def _context(person: CommandPerson) -> PinnedMemoryRequestContext:
    return PinnedMemoryRequestContext(
        team_id=person.team_id,
        # The person in their own controls: their edits count against requests.
        writer=ChangeWriter(ChangeWriterKind.PERSON, person_id=person.person_id),
        canonical_user_id=person.person_id,
        actor=PinnedMemoryActor(kind="user", id=person.person_id),
    )


def _service_command(action: str, body: dict[str, Any]) -> PinnedMemoryCommand:
    """The memory service's command for a contract body."""

    if action == "list":
        offset, limit = int(body["offset"]), int(body["limit"])
        return PinnedMemoryCommand(bank=body["bank"], action=action, sort=SERVICE_SORTS[body["sort"]],
                                   range_start=offset + 1, range_end=offset + limit)
    metadata = None
    if "metadata_json" in body:
        metadata = json.loads(body["metadata_json"])
        if not isinstance(metadata, dict):
            raise ValueError("metadata_json must hold a JSON object.")
    return PinnedMemoryCommand(
        bank=body["bank"],
        action=action,
        memory_id=body.get("memory_id"),
        event_id=body.get("event_id"),
        fact_text=_inline_text(body["fact_text"]) if "fact_text" in body else None,
        tags=body.get("tags"),
        metadata=metadata,
        valid_from_utc=body.get("valid_from"),
        invalid_after_utc=_CLEAR_EXPIRY if body.get("clear_expiry") else body.get("expires"),
        expected_record_digest=body.get("seen_version"),
    )


def _inline_text(content: dict[str, Any]) -> str:
    if content.get("type") != _INLINE:
        raise ValueError("Memory facts are sent inline.")
    return str(content["text"])


def _read_reply(action: str, body: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if action == "show":
        return {"outcome": "ok", "fact": wire_fact(result["fact"])}
    facts = [wire_fact(fact) for fact in result["facts"]]
    reply: dict[str, Any] = {"outcome": "ok", "facts": facts, "total": int(result["total"])}
    following = int(body["offset"]) + len(facts)
    if facts and following < reply["total"]:
        reply["next_offset"] = following
    return reply


def _change_reply(action: str, result: Any) -> dict[str, Any]:
    """A change's reply from what the store returned, inside its transaction."""

    if action == "undo":
        reply = {"outcome": "ok", "event_id": result["event_id"], "undone_event_id": result["undone_event_id"],
                 "undone_event_type": result["event_type"], "deleted": bool(result.get("deleted"))}
        if isinstance(result.get("fact"), dict):
            reply["fact"] = wire_fact(public_pinned_memory_fact_from_dict(result["fact"]))
        return reply
    assert isinstance(result, PinnedMemoryWriteResult)
    if result.event_id is None:
        raise ValueError("The store made no change for this command.")
    if action == "delete":
        return {"outcome": "ok", "memory_id": result.fact.memory_id, "event_id": result.event_id}
    return {"outcome": "ok", "fact": wire_fact(public_pinned_memory_fact(result.fact)), "event_id": result.event_id}


def wire_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """A fact as the contract's replies carry it; metadata and import keys stay out, as before."""

    wire = {
        "memory_id": int(fact["memory_id"]),
        "fact_text": {"type": _INLINE, "text": fact["fact_text"]},
        "valid_from": _timestamp(fact["valid_from_utc"]),
        "tags": list(fact.get("tags") or []),
        "created_at": _timestamp(fact["created_at_utc"]),
        "updated_at": _timestamp(fact["updated_at_utc"]),
        "seen_version": fact["record_digest"],
    }
    if fact.get("invalid_after_utc"):
        wire["expires"] = _timestamp(fact["invalid_after_utc"])
    return wire


def _timestamp(stored: str) -> str:
    return format_timestamp(datetime.fromisoformat(str(stored).replace("Z", "+00:00")))


def _message_reply(outcome: str, message: str) -> dict[str, Any]:
    return {"outcome": outcome, "message": _clip(message)}


def _clip(message: Optional[str]) -> str:
    text = (message or "").strip() or "No details."
    return text if len(text) <= _MESSAGE_MAX else text[: _MESSAGE_MAX - 1] + "…"

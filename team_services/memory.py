"""People's memory commands at the team's address: ``memory.list``, ``.show``, ``.create``,
``.replace``, ``.delete`` and ``.undo``.

Until phase 2 the gateway sent these to the team lead's process as a durable
``ManagerMemoryCommand``; they now run here, with no model, on the same database and the
same service (``custom_agents.pinned_memory_runtime.PinnedMemoryService``) with the team
lead's memory settings (``assistant_gateway.runtime_config.lead_memory_config``), so what
a command does is unchanged (plan 13B.4). What changed:

- **Who acts** comes from the command's authority token only (``team_services.people``),
  and the same ownership rule decides as before (``access_control.ownership``): anyone
  in the team reads team memory, administrators change it, and personal memory is the
  person's own.
- **Replies are typed** (the contract's ``memory.*.reply`` bodies); the gateway renders
  them for chats and the Settings page.
- **A change is carried out once per command.** Its reply is stored with the change, in
  the same transaction, under the command's retry key and person, and a repeat gets it
  back (``storage.operation_outcomes``). A repeat is looked up first, so it gets the
  stored reply even if its body would no longer pass the checks. Every final refusal is
  stored too: a person who may not act, a refused access, a wrong command, a change the
  store refused. When such a refusal cannot be stored, the answer is ``runtime_error``
  instead, so the sender tries again and the command is decided again rather than
  answered differently later.
- **Only the command's own mistakes are ``invalid_request``**
  (``PinnedMemoryInputError``); any other failure, such as a stored row the code cannot
  read, is logged and answered ``runtime_error``, and nothing is stored.
- **Replies fit the contract's limits** (README "Reply size"). A list returns as many
  facts as fit in one frame and says where to continue (``next_offset``). Fact text goes
  inline, at most ``INLINE_TEXT_MAX_CHARS`` characters, until phase 3 adds blob
  references; the gateway refuses longer text from people before sending it. A fact
  longer than that (only agents and imports can write one) is never cut or skipped, as
  plan decision 77 has it for knowledge: ``memory.show`` explains why it cannot be shown,
  a list page ends just before it (or, when it comes first, explains it and where the
  list continues), a change whose reply would have to carry it is refused, and an undo
  that restores it answers without the fact. A change's reply is checked against the
  contract before it is stored, so a stored reply can always be sent.
- **Where a fact came from** is no longer recorded for people's commands: the command
  names no chat, so the fact's events record the person only.

The repository is synchronous, so each command runs in a worker thread, and the team
services' lock and host upkeep keep running meanwhile. Memory commands run side by side
at the team's address (``SIDE_BY_SIDE_KINDS``, ``LiveHost.serve``): the store's write lock
and the stored outcomes order them, not their arrival, and a slow database must not hold
up the session renewals queued behind them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from access_control.ownership import (
    PRIVATE_CREATE,
    PRIVATE_DELETE,
    PRIVATE_READ,
    PRIVATE_UPDATE,
    RESOURCE_TEAM_MEMORY,
    RESOURCE_USER_MEMORY,
    OwnershipDecision,
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
from custom_messages.contracts.control_v1 import PINNED_MEMORY_CLEAR_EXPIRY, PINNED_MEMORY_WIRE_SORTS
from custom_messages.enums import ChangeRefusal
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame, body_model
from custom_messages.wire.sizes import INLINE_TEXT_MAX_CHARS, INLINE_TEXT_TYPE, REPLY_BODY_MAX_BYTES, encoded_size
from storage.change_ledger import ChangeRefused, ChangeWriter, ChangeWriterKind
from custom_messages.wire.kinds import RETRY_OUTCOMES
from storage.operation_outcomes import CommandOutcome, OutcomeAlreadyStored, OutcomeKeyTaken
from storage.pinned_memory_repository import (
    COREMEM_BANK,
    PinnedMemoryCapacityError,
    PinnedMemoryConflictError,
    PinnedMemoryInputError,
    PinnedMemoryNotFoundError,
    PinnedMemoryRepository,
    PinnedMemoryWriteResult,
)
from team_services.people import CommandPerson, command_person
from team_services.service import OperationFamily

# The reply model of memory.show, which checks one carried fact.
_FACT_REPLY = body_model("memory.show.reply")[0]

MEMORY_ACTIONS = ("list", "show", "create", "replace", "delete", "undo")
MEMORY_KINDS = tuple(f"memory.{action}" for action in MEMORY_ACTIONS)
# Run side by side at the team's address (module docstring).
SIDE_BY_SIDE_KINDS = frozenset(MEMORY_KINDS)
_CHANGES = frozenset({"create", "replace", "delete", "undo"})
_ACCESS = {"list": PRIVATE_READ, "show": PRIVATE_READ, "create": PRIVATE_CREATE, "delete": PRIVATE_DELETE}
# The contract's sort names, as the memory service names them.
SERVICE_SORTS = {wire: service for service, wire in PINNED_MEMORY_WIRE_SORTS.items()}
# The contract's short_text: every reply message fits it.
_MESSAGE_MAX = 500
_RUNTIME_ERROR = "The memory command failed; nothing was changed. Try again in a moment."


class MemoryOperations:
    """Answers people's memory commands for one team (module docstring)."""

    def __init__(self, repository: PinnedMemoryRepository, people: PeopleDirectory, memory_config: dict[str, Any],
                 clock: Callable[[], datetime], logger: logging.Logger) -> None:
        self._repository = repository
        self._people = people
        self._service = PinnedMemoryService(repository, config=memory_config, command_origin="gateway", logger=logger)
        self._clock = clock
        self._logger = logger

    def family(self) -> OperationFamily:
        """Its handlers at the team's address, run side by side, and its upkeep (module docstring)."""

        return OperationFamily({kind: self.handle for kind in MEMORY_KINDS}, SIDE_BY_SIDE_KINDS, self.purge_outcomes)

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        return await asyncio.to_thread(self.answer, verified)

    async def purge_outcomes(self) -> None:
        """Delete stored replies past their retention, also when no new command stores one."""

        purged = await asyncio.to_thread(self._repository.purge_command_outcomes, self._clock())
        if purged:
            self._logger.info("Deleted old stored replies. event=team_memory_outcomes_purged count=%s", purged)

    def answer(self, verified: VerifiedFrame) -> dict[str, Any]:
        """The reply to one memory command (blocking: the store is synchronous)."""

        action = verified.frame.kind.removeprefix("memory.")
        body = verified.raw["body"]
        check = command_person(verified, self._people, self._logger)
        replies: list[dict[str, Any]] = []
        outcome = None
        if check.person_id is not None:
            outcome = _outcome_for(verified, action, check.person_id, self._people.team_id, self._clock(),
                                   self._logger, replies)
        if outcome is not None:
            # A repeat gets its stored reply first, whatever else changed since (module docstring).
            try:
                stored = self._repository.stored_command_outcome(outcome)
                if stored is not None:
                    return outcome.answer_from(stored)
            except OutcomeKeyTaken as taken:
                return _message_reply("invalid_request", str(taken))
            except Exception as error:  # noqa: BLE001 - the store could not be read
                self._logger.exception("Could not look up a command's stored reply. "
                                       "event=team_memory_outcome_lookup_failed kind=%s retry_key=%s",
                                       outcome.kind, outcome.retry_key, exc_info=error)
                return _message_reply("runtime_error", _RUNTIME_ERROR)
        person = check.person
        if person is None:
            return self._final(outcome, _message_reply("not_allowed", check.reason))
        decision = _memory_access(action, body, person)
        if not decision.allowed:
            self._logger.warning("Refused a memory command. event=team_memory_command_denied person=%s bank=%s "
                                 "action=%s reason=%s", person.person_id, body["bank"], action, decision.reason)
            return self._final(outcome, _message_reply("not_allowed", "Only administrators change team memory."
                                                       if body["bank"] == COREMEM_BANK else "This memory is not yours."))
        try:
            command = _service_command(action, body)
            result = self._service.execute(command, context=_context(person), command_outcome=outcome)
            if outcome is not None:
                return replies[-1]
            return _read_reply(action, body, result)
        except OutcomeAlreadyStored as stored:
            return stored.reply
        except OutcomeKeyTaken as taken:
            return _message_reply("invalid_request", str(taken))
        except Exception as error:  # noqa: BLE001 - every failure is answered and logged in _refusal
            return self._final(outcome, self._refusal(action, body, person, error))

    def _final(self, outcome: Optional[CommandOutcome], reply: dict[str, Any]) -> dict[str, Any]:
        """A change's final refusal is stored before it is sent; when that fails, the sender is asked to retry.

        When another copy of the command was answered meanwhile, that stored reply is sent instead.
        """

        if outcome is None or reply.get("outcome") in RETRY_OUTCOMES:
            return reply
        try:
            stored = self._repository.save_command_refusal(outcome, reply)
            return reply if stored is None else outcome.answer_from(stored)
        except OutcomeKeyTaken as taken:
            return _message_reply("invalid_request", str(taken))
        except Exception:
            # Sent unstored, a retry could be decided differently (carried out after a refusal), so ask for one now.
            self._logger.exception("Could not store a refused command's reply; asking the sender to try again. "
                                   "event=team_memory_refusal_not_stored kind=%s retry_key=%s outcome=%s",
                                   outcome.kind, outcome.retry_key, reply.get("outcome"))
            return _message_reply("runtime_error", _RUNTIME_ERROR)

    def _refusal(self, action: str, body: dict[str, Any], person: CommandPerson, error: Exception) -> dict[str, Any]:
        if isinstance(error, PinnedMemoryNotFoundError):
            return _message_reply("not_found", str(error))
        if isinstance(error, PinnedMemoryConflictError) or (
            isinstance(error, ChangeRefused) and error.reason is ChangeRefusal.CHANGED_SINCE_READ
        ):
            return self._changed_since_read(action, body, person, str(error))
        if isinstance(error, PinnedMemoryCapacityError):
            # Only a create says "no room"; an undo that would bring a fact back into a full bank is not possible.
            return _message_reply("capacity_reached" if action == "create" else "invalid_request", str(error))
        if isinstance(error, ChangeRefused) and error.reason is ChangeRefusal.REQUEST_CLOSED:
            return _message_reply("request_closed", str(error))
        if isinstance(error, ChangeRefused) and error.reason is ChangeRefusal.NEEDS_PERSON_DECISION:
            return _message_reply("needs_person_decision", str(error))
        if isinstance(error, FactNotCarried):
            if action in _CHANGES:
                return _message_reply("invalid_request", f"{error} Nothing was changed.")
            return _message_reply("runtime_error", str(error))
        if isinstance(error, PinnedMemoryInputError) and action in _CHANGES:
            return _message_reply("invalid_request", str(error))
        self._logger.error("A memory command failed. event=team_memory_command_failed person=%s bank=%s action=%s "
                           "error_type=%s", person.person_id, body.get("bank"), action, type(error).__name__,
                           exc_info=error)
        return _message_reply("runtime_error", _RUNTIME_ERROR)

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


class FactNotCarried(ValueError):
    """A fact the contract cannot carry in a reply: its text is longer than inline text may be, or a field
    is outside what the contract allows. It is kept whole; Web Chat cannot show or change it (module docstring)."""

    def __init__(self, fact: dict[str, Any], problem: str) -> None:
        super().__init__(problem)
        self.memory_id = int(fact["memory_id"])


def _outcome_for(verified: VerifiedFrame, action: str, person_id: str, team_id: str, now: datetime,
                 logger: logging.Logger, replies: list[dict[str, Any]]) -> Optional[CommandOutcome]:
    """The stored-outcome record of a change command (None for a read)."""

    if action not in _CHANGES:
        return None
    kind = verified.frame.kind
    return CommandOutcome(
        team_id=team_id, retry_key=str(verified.raw["idempotency_key"]), person_id=person_id, kind=kind,
        body_digest=str(verified.raw["body_digest"]), now=now,
        reply_for=lambda result: _kept(replies, _checked(kind, _change_reply(action, result))), logger=logger,
    )


def _kept(replies: list[dict[str, Any]], reply: dict[str, Any]) -> dict[str, Any]:
    replies.append(reply)
    return reply


def _memory_access(action: str, body: dict[str, Any], person: CommandPerson) -> OwnershipDecision:
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
    if body.get("clear_expiry") and "expires" in body:
        # The contract: "Do not also send expires"; which one was meant is unknown, so neither is guessed.
        raise PinnedMemoryInputError("Send either an expiry or clear_expiry, not both.")
    metadata = None
    if "metadata_json" in body:
        try:
            metadata = json.loads(body["metadata_json"])
        except ValueError as error:
            raise PinnedMemoryInputError("metadata_json must hold JSON text.") from error
        if not isinstance(metadata, dict):
            raise PinnedMemoryInputError("metadata_json must hold a JSON object.")
    return PinnedMemoryCommand(
        bank=body["bank"],
        action=action,
        memory_id=body.get("memory_id"),
        event_id=body.get("event_id"),
        fact_text=_inline_text(body["fact_text"]) if "fact_text" in body else None,
        tags=body.get("tags"),
        metadata=metadata,
        valid_from_utc=body.get("valid_from"),
        invalid_after_utc=PINNED_MEMORY_CLEAR_EXPIRY if body.get("clear_expiry") else body.get("expires"),
        expected_record_digest=body.get("seen_version"),
    )


def _inline_text(content: dict[str, Any]) -> str:
    if content.get("type") != INLINE_TEXT_TYPE:
        raise PinnedMemoryInputError("Memory facts are sent inline.")
    return str(content["text"])


def _read_reply(action: str, body: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if action == "show":
        return {"outcome": "ok", "fact": carried_fact(result["fact"])}
    return _list_reply(int(body["offset"]), result)


def _list_reply(offset: int, result: dict[str, Any]) -> dict[str, Any]:
    """As many facts as fit in one frame, from ``offset``, with where to continue (module docstring)."""

    total = int(result["total"])
    facts: list[dict[str, Any]] = []
    # The reply without facts, with the largest numbers it can name.
    size = encoded_size({"outcome": "ok", "facts": [], "total": total, "next_offset": total})
    for position, fact in enumerate(result["facts"], start=offset + 1):
        try:
            wire = carried_fact(fact)
        except FactNotCarried as problem:
            if facts:
                break  # this page ends just before it; the next one starts with it and explains it
            after = f" To see the facts after it, list from position {position + 1}." if position < total else ""
            return _message_reply("runtime_error", f"{problem}{after}")
        size += encoded_size(wire) + 1  # and its comma
        if size > REPLY_BODY_MAX_BYTES:
            break
        facts.append(wire)
    reply: dict[str, Any] = {"outcome": "ok", "facts": facts, "total": total}
    following = offset + len(facts)
    if facts and following < total:
        reply["next_offset"] = following
    return reply


def _change_reply(action: str, result: Any) -> dict[str, Any]:
    """A change's reply from what the store returned, inside its transaction."""

    if action == "undo":
        reply = {"outcome": "ok", "event_id": result["event_id"], "undone_event_id": result["undone_event_id"],
                 "undone_event_type": result["event_type"], "deleted": bool(result.get("deleted"))}
        if isinstance(result.get("fact"), dict):
            try:
                reply["fact"] = carried_fact(public_pinned_memory_fact_from_dict(result["fact"]))
            except FactNotCarried:
                pass  # the undo stands; its reply leaves out a fact it cannot carry (module docstring)
        return reply
    assert isinstance(result, PinnedMemoryWriteResult)
    if result.event_id is None:
        raise RuntimeError("The store made no change for this command.")
    if action == "delete":
        return {"outcome": "ok", "memory_id": result.fact.memory_id, "event_id": result.event_id}
    fact = carried_fact(public_pinned_memory_fact(result.fact))
    return {"outcome": "ok", "fact": fact, "event_id": result.event_id}


def _checked(kind: str, reply: dict[str, Any]) -> dict[str, Any]:
    """The reply, checked against the contract before it is stored, so it can always be sent."""

    body_model(f"{kind}.reply")[0].validate_python(reply)
    if encoded_size(reply) > REPLY_BODY_MAX_BYTES:
        raise RuntimeError(f"A {kind} reply would not fit in a frame.")
    return reply


def carried_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """A fact as the contract's replies carry it, or ``FactNotCarried`` (module docstring)."""

    text = str(fact["fact_text"])
    if len(text) > INLINE_TEXT_MAX_CHARS:
        raise FactNotCarried(fact, f"Memory #{fact['memory_id']} is {len(text):,} characters long, more than Web Chat "
                                   f"can show ({INLINE_TEXT_MAX_CHARS:,}). It is kept whole, and agents still use it.")
    wire = wire_fact(fact)
    try:
        _FACT_REPLY.validate_python({"outcome": "ok", "fact": wire})
    except ValueError as error:
        raise FactNotCarried(fact, f"Memory #{fact['memory_id']} has tags or times Web Chat cannot show. It is kept "
                                   "whole, and agents still use it.") from error
    return wire


def wire_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """A fact as the contract's replies carry it; metadata and import keys stay out, as before."""

    wire = {
        "memory_id": int(fact["memory_id"]),
        "fact_text": {"type": INLINE_TEXT_TYPE, "text": fact["fact_text"]},
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
    """A stored time in the contract's form. The store keeps UTC; a time without a zone (written by
    imports before 0.10) is UTC too, as the store's own expiry checks and every earlier reader take it."""

    moment = datetime.fromisoformat(str(stored).replace("Z", "+00:00"))
    return format_timestamp(moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc))


def _message_reply(outcome: str, message: str) -> dict[str, Any]:
    return {"outcome": outcome, "message": _clip(message)}


def _clip(message: Optional[str]) -> str:
    text = (message or "").strip() or "No details."
    return text if len(text) <= _MESSAGE_MAX else text[: _MESSAGE_MAX - 1] + "…"

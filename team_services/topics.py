"""People's conversation-topic commands at the team's address: ``topics.list_tags``,
``topics.find_threads`` and ``topics.tag_thread`` (the chat commands /topics,
/tag_thread and /switch_topic).

Until phase 2 the gateway sent these to the team lead's process as a durable
``ManagerTopicCommand``; they now run here on the same conversation store
(``custom_agents.conversation_store``). The person comes only from the command's
authority token (``team_services.people``): their tags and threads are their own, and a
thread is tagged only by its owner (``access_control.ownership``), as before.

Tagging a thread can be done again with the same effect, so its stored outcome is saved
right after the change (``storage.operation_outcomes.OperationOutcomeStore``): a stop
between the two only means a retry tags the thread again and gets the same reply. The
store is synchronous, so each command runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from access_control.ownership import PRIVATE_UPDATE, RESOURCE_CONVERSATION, private_access
from access_control.people_directory import PeopleDirectory
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame
from storage.operation_outcomes import CommandOutcome, OperationOutcomeStore
from team_services.outcomes import carried_out_once
from team_services.people import CommandPerson, command_person

TOPIC_KINDS = ("topics.list_tags", "topics.find_threads", "topics.tag_thread")
# How many threads a search returns (the contract's limit, and the chat's).
FIND_LIMIT = 10
# Who tagged a thread, in the thread's tag records.
TAG_SOURCE = "gateway_command"
TAGGED_BY = "team_services"
_MESSAGE_MAX = 500
_PREVIEW_MAX = 500


class ThreadNotFound(LookupError):
    """The conversation does not exist, or is not the person's."""


class TopicOperations:
    """Answers people's topic commands for one team (module docstring)."""

    def __init__(self, conversations: Any, outcomes: OperationOutcomeStore, people: PeopleDirectory,
                 clock: Callable[[], datetime], logger: logging.Logger) -> None:
        self._conversations = conversations
        self._outcomes = outcomes
        self._people = people
        self._clock = clock
        self._logger = logger

    def handlers(self) -> dict[str, Callable[[VerifiedFrame], Any]]:
        return {kind: self.handle for kind in TOPIC_KINDS}

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        return await asyncio.to_thread(self.answer, verified)

    def answer(self, verified: VerifiedFrame) -> dict[str, Any]:
        kind = verified.frame.kind
        body = verified.raw["body"]
        person, reason = command_person(verified, self._people, self._logger)
        if person is None:
            # The topic operations have no not_allowed outcome; a refused sender gets nothing back.
            return _message("runtime_error", reason)
        if kind == "topics.list_tags":
            return self._read(kind, person, lambda: self._list_tags(person))
        if kind == "topics.find_threads":
            return self._read(kind, person, lambda: self._find_threads(person, body["tags"]))
        outcome = CommandOutcome(
            team_id=person.team_id, retry_key=str(verified.raw["idempotency_key"]), kind=kind,
            body_digest=str(verified.raw["body_digest"]), now=self._clock(), reply_for=lambda reply: reply,
            logger=self._logger,
        )
        return carried_out_once(self._outcomes, outcome, lambda: self._tag_thread(person, body, outcome),
                                refusal_for=lambda error: self._refusal(kind, person, error), logger=self._logger)

    def _read(self, kind: str, person: CommandPerson, read: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return read()
        except Exception as error:  # noqa: BLE001 - answered and logged in _refusal
            return self._refusal(kind, person, error)

    def _list_tags(self, person: CommandPerson) -> dict[str, Any]:
        tags = self._conversations.list_known_thread_tags(team_id=person.team_id, canonical_user_id=person.person_id)
        return {"outcome": "ok", "tags": [
            {"tag": item["tag"], "thread_count": int(item["thread_count"]), "updated_at": _timestamp(item["updated_at"])}
            for item in tags
        ]}

    def _find_threads(self, person: CommandPerson, tags: list[str]) -> dict[str, Any]:
        matches = self._conversations.find_threads_by_tags(tags=list(tags), team_id=person.team_id,
                                                           canonical_user_id=person.person_id, limit=FIND_LIMIT)
        return {"outcome": "ok", "matches": [_match(item) for item in matches]}

    def _tag_thread(self, person: CommandPerson, body: dict[str, Any], outcome: CommandOutcome) -> dict[str, Any]:
        conversation_id = body["conversation_id"]
        thread = self._conversations.get_thread(conversation_id)
        owner = str((thread or {}).get("owner_canonical_user_id") or "").strip()
        decision = private_access(person_id=person.person_id, owner_id=owner, resource_type=RESOURCE_CONVERSATION,
                                  resource_id=conversation_id, action=PRIVATE_UPDATE, team_id=person.team_id)
        if not thread or not decision.allowed:
            raise ThreadNotFound("That conversation was not found.")
        applied = [
            str(self._conversations.tag_thread(
                conversation_id=conversation_id, tag=tag, team_id=person.team_id,
                canonical_user_id=person.person_id if body["private"] else None,
                source=TAG_SOURCE, created_by_agent_id=TAGGED_BY,
            )["tag"])
            for tag in body["tags"]
        ]
        return self._outcomes.save_reply(outcome, {"outcome": "ok", "applied_tags": applied})

    def _refusal(self, kind: str, person: CommandPerson, error: Exception) -> dict[str, Any]:
        if isinstance(error, ThreadNotFound):
            return _message("not_found", str(error))
        if isinstance(error, ValueError) and kind != "topics.list_tags":
            return _message("invalid_request", str(error))
        self._logger.error("A topic command failed. event=team_topic_command_failed person=%s kind=%s error_type=%s",
                           person.person_id, kind, type(error).__name__, exc_info=error)
        return _message("runtime_error", "The topic command failed; nothing was changed.")


def _match(item: dict[str, Any]) -> dict[str, Any]:
    match: dict[str, Any] = {
        "conversation_id": item["conversation_id"], "status": str(item.get("status") or "undefined"),
        "updated_at": _timestamp(item["updated_at"]), "matched_tags": list(item["matched_tags"]),
    }
    if item.get("latest_task_id"):
        match["latest_task_id"] = item["latest_task_id"]
    preview = " ".join(str(item.get("subject_preview") or "").split())
    if preview:
        match["subject_preview"] = preview[:_PREVIEW_MAX]
    return match


def _timestamp(stored: Any) -> str:
    moment = datetime.fromisoformat(str(stored).replace("Z", "+00:00"))
    # The conversation store writes UTC times; one without a zone is UTC too.
    return format_timestamp(moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc))


def _message(outcome: str, message: str) -> dict[str, Any]:
    text = (message or "").strip() or "No details."
    return {"outcome": outcome, "message": text if len(text) <= _MESSAGE_MAX else text[: _MESSAGE_MAX - 1] + "…"}

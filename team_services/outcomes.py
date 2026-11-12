"""Carrying out a person's change command once, however often it is retried (storage.operation_outcomes).

The store checks for a stored outcome under its write lock and stores the new one with
the change. That alone is not enough: a service checks a command before it reaches the
store (a page's revision, whether a proposal still waits), and once the first try
committed those checks refuse a retry as stale. So, as the memory commands do
(``team_services.memory``):

- **A repeat is answered first.** The stored outcome is looked up before anything else
  is checked, so a repeat gets its stored answer however much changed since.
- **Every final answer is stored.** A refusal (a person who may not act, refused access,
  a change the service refused) is stored in a transaction of its own before it is sent,
  except outcomes that ask for a retry. When it cannot be stored, the sender is asked to
  try again (``runtime_error``), so the command is decided again rather than answered
  differently later.
- **A second look after a failure.** A failure can come after the commit: the commit's
  acknowledgement can be lost. So the outcome is looked up again after the command
  failed, before its refusal is stored.
- **Whose command it was.** A stored outcome answers only the same person's repeat;
  another person's command under a reused key is refused (``OutcomeKeyTaken``).

The team services run each family's commands side by side in worker threads, and
the stores serialize writes under their database locks. One team services process
holds the team's process lock; the gateway sends a command a second time, under the same id,
only when the first got no answer in time (``assistant_gateway.application.team_commands``).

A command whose store keeps a receipt (``storage.operation_outcomes.is_receipt``) has its
reply built from the receipt after the commit, on the first try and on every repeat. When
that fails, the change is already made: the failure is never answered as a refusal,
which would say nothing changed. ``AnswerUnknown`` is raised instead, which the host
answers ``handler_failed``, so the requester treats the result as unknown (contract
README "Refused requests").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from custom_messages.wire.kinds import RETRY_OUTCOMES
from storage.operation_outcomes import (
    CommandOutcome,
    OutcomeAlreadyStored,
    OutcomeKeyTaken,
    StoredOutcome,
    is_receipt,
)

Reply = dict[str, Any]


class AnswerUnknown(RuntimeError):
    """The change was carried out (or may have been), but its reply could not be built (module docstring)."""


class OutcomeStore(Protocol):
    def stored_command_outcome(self, command_outcome: CommandOutcome) -> Optional[StoredOutcome]: ...

    def save_command_refusal(self, command_outcome: CommandOutcome, reply: Reply) -> Optional[StoredOutcome]: ...


@dataclass(frozen=True)
class CommandOnce:
    """One change command's stored outcome: looked up, answered and stored (module docstring).

    ``reply_from_receipt`` builds the reply from a stored receipt and raises
    ``AnswerUnknown`` when it cannot; ``key_taken`` answers another person's command under
    a reused key; ``not_stored`` answers when a final refusal could not be stored, or the
    store could not be read before anything ran (both ask for a retry).
    """

    store: OutcomeStore
    outcome: CommandOutcome
    reply_from_receipt: Callable[[Reply], Reply]
    key_taken: Callable[[OutcomeKeyTaken], Reply]
    not_stored: Reply
    logger: logging.Logger

    def stored_reply(self) -> Optional[Reply]:
        """A repeat's answer from its stored outcome, before the command runs again; None when nothing is stored."""

        try:
            stored = self.store.stored_command_outcome(self.outcome)
        except Exception:  # noqa: BLE001 - nothing ran yet, so the sender may try again
            self.logger.exception("Could not look up a command's stored outcome. event=team_command_outcome_lookup_failed "
                                  "kind=%s retry_key=%s", self.outcome.kind, self.outcome.retry_key)
            return self.not_stored
        return None if stored is None else self._answer(stored)

    def run(self, attempt: Callable[[], Reply], refusal_for: Callable[[Exception], Reply]) -> Reply:
        """The command's reply: from ``attempt``, which stores its outcome with its change, or its final refusal.

        ``attempt`` raises ``AnswerUnknown`` when its change committed and its reply
        could not be built. Call ``stored_reply`` first.
        """

        try:
            return attempt()
        except OutcomeAlreadyStored as already:
            # raise_if_stored checked the person (CommandOutcome.answer_from).
            return self._answer_reply(already.reply)
        except OutcomeKeyTaken as taken:
            return self.key_taken(taken)
        except AnswerUnknown:
            raise
        except Exception as error:  # noqa: BLE001 - refusal_for answers every failure and logs the unexpected ones
            reply = refusal_for(error)
        try:
            stored = self.store.stored_command_outcome(self.outcome)
        except Exception as error:
            raise AnswerUnknown(f"{self.outcome.kind} failed, and whether it was carried out could not be read.") \
                from error
        if stored is not None:
            self.logger.warning("A command failed after its change was stored; its stored outcome answers it. "
                                "event=operation_outcome_found_after_failure kind=%s retry_key=%s", self.outcome.kind,
                                self.outcome.retry_key)
            return self._answer(stored)
        return self.final(reply)

    def final(self, reply: Reply) -> Reply:
        """A final refusal, stored before it is sent; what is stored under the key answers (module docstring)."""

        if reply.get("outcome") in RETRY_OUTCOMES:
            return reply
        try:
            stored = self.store.save_command_refusal(self.outcome, reply)
        except Exception:  # noqa: BLE001 - sent unstored, a repeat could be decided differently
            self.logger.exception("Could not store a refused command's reply; asking the sender to try again. "
                                  "event=team_command_refusal_not_stored kind=%s retry_key=%s outcome=%s",
                                  self.outcome.kind, self.outcome.retry_key, reply.get("outcome"))
            return self.not_stored
        return reply if stored is None else self._answer(stored)

    def _answer(self, stored: StoredOutcome) -> Reply:
        try:
            reply = self.outcome.answer_from(stored)
        except OutcomeKeyTaken as taken:
            return self.key_taken(taken)
        return self._answer_reply(reply)

    def _answer_reply(self, reply: Reply) -> Reply:
        if not is_receipt(reply):
            return reply
        self.logger.info("A repeated command was answered from its receipt. event=operation_outcome_from_receipt "
                         "kind=%s retry_key=%s", self.outcome.kind, self.outcome.retry_key)
        return self.reply_from_receipt(reply)


__all__ = ["AnswerUnknown", "CommandOnce"]

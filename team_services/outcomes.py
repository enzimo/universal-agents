"""Carrying out a person's change command once, however often it is retried (storage.operation_outcomes).

The store checks for a stored outcome under its write lock and stores the new one with
the change. That alone is not enough: a service checks a command before it reaches the
store (a page's revision, whether a proposal still waits), and once the first try
committed those checks refuse a retry as stale. So the stored outcome is looked up
before the command runs. It is looked up again after the command failed, because a
failure can come after the commit: the commit's acknowledgement can be lost, or the
reply built after the commit can fail. Only when nothing is stored is a refusal stored
as the command's outcome.

The team services' address answers its requests one at a time
(``custom_transport.protocol1.live.LiveHost.serve``), and one team services process
holds the team's lock, so two tries of one command do not run at the same time; the
store's own check under its write lock still covers a second process.

A command whose store keeps a receipt (``CommandOutcome.complete`` False) has its reply
built from the receipt after the commit, on the first try and on every retry. When that
fails, the change is already made: the failure is never answered as a refusal, which
would say nothing changed. ``AnswerUnknown`` is raised instead, which the host answers
``handler_failed``, so the requester treats the result as unknown (contract README
"Refused requests").
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Protocol

from storage.operation_outcomes import RETRY_OUTCOMES, CommandOutcome, OutcomeAlreadyStored

Reply = dict[str, Any]


class AnswerUnknown(RuntimeError):
    """The change was carried out (or may have been), but its reply could not be built (module docstring)."""


class OutcomeStore(Protocol):
    def stored_command_outcome(self, command_outcome: CommandOutcome) -> Optional[tuple[Reply, str, bool]]: ...

    def save_command_refusal(self, command_outcome: CommandOutcome, reply: Reply) -> None: ...


def carried_out_once(
    store: OutcomeStore,
    outcome: CommandOutcome,
    attempt: Callable[[], Reply],
    *,
    refusal_for: Callable[[Exception], Reply],
    reply_from_receipt: Optional[Callable[[Reply], Reply]] = None,
    logger: logging.Logger,
) -> Reply:
    """The command's reply: from its stored outcome, or from ``attempt``, which stores it with its change.

    ``reply_from_receipt`` builds the reply from a stored receipt (stores that keep
    receipts); ``attempt`` raises ``AnswerUnknown`` when its change committed and its reply
    could not be built.
    """

    stored = store.stored_command_outcome(outcome)
    if stored is not None:
        return _answer_stored(outcome, stored, reply_from_receipt, logger)
    try:
        return attempt()
    except OutcomeAlreadyStored as already:
        return _answer_stored(outcome, (already.reply, outcome.body_digest, already.complete), reply_from_receipt,
                              logger)
    except AnswerUnknown:
        raise
    except Exception as error:  # noqa: BLE001 - refusal_for answers every failure and logs the unexpected ones
        reply = refusal_for(error)
    stored = store.stored_command_outcome(outcome)
    if stored is not None:
        logger.warning("A command failed after its change was stored; its stored outcome answers it. "
                       "event=operation_outcome_found_after_failure kind=%s retry_key=%s", outcome.kind,
                       outcome.retry_key)
        return _answer_stored(outcome, stored, reply_from_receipt, logger)
    if reply.get("outcome") not in RETRY_OUTCOMES:
        try:
            store.save_command_refusal(outcome, reply)
        except Exception:
            # The refusal still answers; without it a retry is decided, and refused, again.
            logger.exception("Could not store a refused command's reply. event=team_command_refusal_not_stored "
                             "kind=%s retry_key=%s outcome=%s", outcome.kind, outcome.retry_key, reply.get("outcome"))
    return reply


def _answer_stored(outcome: CommandOutcome, stored: tuple[Reply, str, bool],
                   reply_from_receipt: Optional[Callable[[Reply], Reply]], logger: logging.Logger) -> Reply:
    reply, body_digest, complete = stored
    if body_digest != outcome.body_digest:
        logger.warning("A command was repeated with another body; the stored reply answers it. "
                       "event=operation_outcome_body_mismatch kind=%s retry_key=%s stored_body_digest=%s "
                       "body_digest=%s", outcome.kind, outcome.retry_key, body_digest, outcome.body_digest)
    if complete:
        return reply
    if reply_from_receipt is None:
        raise AnswerUnknown(f"{outcome.kind} stored a receipt but has no way to build its reply.")
    logger.info("A repeated command was answered from its receipt. event=operation_outcome_from_receipt kind=%s "
                "retry_key=%s", outcome.kind, outcome.retry_key)
    return reply_from_receipt(reply)


__all__ = ["AnswerUnknown", "carried_out_once"]

"""Carrying out a person's change command once, however often it is retried (storage.operation_outcomes).

The store checks for a stored outcome under its write lock and stores the new one with
the change. That alone is not enough: a service checks a command before it reaches the
store (a page's revision, whether a proposal still waits), and after the first try
committed those checks refuse a retry as stale. So the stored outcome is looked up
before the command runs, and again after a refusal, in case a retry running at the same
time carried it out meanwhile. Only then is a refusal stored as the command's outcome.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Protocol

from storage.operation_outcomes import RETRY_OUTCOMES, CommandOutcome, OutcomeAlreadyStored

Reply = dict[str, Any]


class OutcomeStore(Protocol):
    def stored_command_outcome(self, command_outcome: CommandOutcome) -> Optional[tuple[Reply, str, bool]]: ...

    def save_command_refusal(self, command_outcome: CommandOutcome, reply: Reply) -> None: ...

    def complete_command_outcome(self, command_outcome: CommandOutcome, reply: Reply) -> None: ...


def carried_out_once(
    store: OutcomeStore,
    outcome: CommandOutcome,
    attempt: Callable[[], Reply],
    *,
    refusal_for: Callable[[Exception], Reply],
    rebuild: Optional[Callable[[Reply], Reply]] = None,
    logger: logging.Logger,
) -> Reply:
    """The command's reply: stored, or from ``attempt`` (which stores it with its change).

    ``rebuild`` turns a stored receipt into the full reply (stores that save receipts).
    """

    stored = store.stored_command_outcome(outcome)
    if stored is not None:
        return _answer_stored(store, outcome, stored, rebuild, logger)
    try:
        return attempt()
    except OutcomeAlreadyStored as already:
        return _answer_stored(store, outcome, (already.reply, outcome.body_digest, already.complete), rebuild, logger)
    except Exception as error:  # noqa: BLE001 - refusal_for answers every failure and logs the unexpected ones
        reply = refusal_for(error)
    stored = store.stored_command_outcome(outcome)
    if stored is not None:
        return _answer_stored(store, outcome, stored, rebuild, logger)
    if reply.get("outcome") not in RETRY_OUTCOMES:
        try:
            store.save_command_refusal(outcome, reply)
        except Exception:
            # The refusal still answers; without it a retry is decided, and refused, again.
            logger.exception("Could not store a refused command's reply. event=team_command_refusal_not_stored "
                             "kind=%s retry_key=%s outcome=%s", outcome.kind, outcome.retry_key, reply.get("outcome"))
    return reply


def _answer_stored(store: OutcomeStore, outcome: CommandOutcome, stored: tuple[Reply, str, bool],
                   rebuild: Optional[Callable[[Reply], Reply]], logger: logging.Logger) -> Reply:
    reply, body_digest, complete = stored
    if body_digest != outcome.body_digest:
        logger.warning("A command was repeated with another body; the stored reply answers it. "
                       "event=operation_outcome_body_mismatch kind=%s retry_key=%s stored_body_digest=%s "
                       "body_digest=%s", outcome.kind, outcome.retry_key, body_digest, outcome.body_digest)
    if complete:
        return reply
    if rebuild is None:
        raise RuntimeError(f"{outcome.kind} stored a receipt but has no way to rebuild its reply.")
    logger.info("A retried command was answered from its receipt. event=operation_outcome_rebuilt kind=%s "
                "retry_key=%s", outcome.kind, outcome.retry_key)
    full = rebuild(reply)
    try:
        store.complete_command_outcome(outcome, full)
    except Exception:
        # The rebuilt reply still answers; the next retry rebuilds it again.
        logger.exception("Could not store a rebuilt reply. event=operation_outcome_completion_failed kind=%s "
                         "retry_key=%s", outcome.kind, outcome.retry_key)
    return full

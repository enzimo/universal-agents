"""Errors of the team transfer, in a module with no dependencies so every part can raise them."""

from __future__ import annotations


class TransferError(RuntimeError):
    """The export or import cannot go ahead; the message says what to do."""


class NewTeamIdRefused(TransferError):
    """The new team id asked for cannot be used, and another one can (``team_transfer.team_ids``).

    Raised for a typed id the rule refuses and for an id another team in the export already has,
    so onboarding's Import page shows the message by the id field and keeps what was typed,
    and the command line can tell it from a failure another id would not fix.
    """


__all__ = ["NewTeamIdRefused", "TransferError"]

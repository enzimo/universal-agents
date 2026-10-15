"""Errors of the team transfer, in a module with no dependencies so every part can raise them."""

from __future__ import annotations


class TransferError(RuntimeError):
    """The export or import cannot go ahead; the message says what to do."""


__all__ = ["TransferError"]

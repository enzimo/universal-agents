"""Shared exception for refused access proposals (acyclic leaf for proposal submodules)."""

from __future__ import annotations


class ProposalRefused(ValueError):
    """The proposal cannot be reviewed or applied as it is; the message says why."""


__all__ = ["ProposalRefused"]

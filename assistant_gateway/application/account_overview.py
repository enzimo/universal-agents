"""Whose account agents use for each service, as typed rows (``/accounts``).

The gateway builds these rows (``assistant_gateway.account_sharing``) and every
view of the team's account sharing formats them, so the views cannot
disagree. They are application-layer records: no storage types, no wire
format.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from custom_messages.contracts.access_v2 import AccountSharing


class AccountStatus(StrEnum):
    """Whether an account a service takes is set up (one ``ServiceAccounts`` field)."""

    LINKED = "linked"
    NOT_LINKED = "not_linked"
    # The team's account of a private service: linked, but agents do not use it.
    NOT_USED = "not_used"
    # A sign-in tool's own account: the person signs in with the link command.
    SIGN_IN = "sign_in"


@dataclass(frozen=True)
class ServiceAccounts:
    """One service in the ``/accounts`` list: its sharing and the accounts it takes.

    Every view of the team's account sharing is built from these rows, so
    they say the same thing. ``subject`` is the tool id, or
    ``ACCOUNTS_MODEL_SUBJECT`` for the team's model keys. ``team_account`` and
    ``own_account`` are ``None`` where the service takes no such account or
    the sharing words already say how to set it up. ``setup_command`` is what
    the person sends to set up their own account ("" when there is nothing to
    send); ``signs_in`` says whether that is a sign-in rather than a link.
    """

    subject: str
    name: str
    sharing: AccountSharing
    fixed: bool
    team_account: AccountStatus | None = None
    own_account: AccountStatus | None = None
    setup_command: str = ""
    signs_in: bool = False


class AccountsRefused(Exception):
    """The person may not see the team's accounts (not an active member); the text is for people."""


@dataclass(frozen=True)
class AccountsPage:
    """The web chat's Settings > Accounts tab for one person.

    ``services`` are the ``/accounts`` rows. ``can_change`` says whether the
    person may change sharing, which happens in chat with ``share_command`` or
    ``private_command`` followed by a tool id or ``model``.
    """

    services: tuple[ServiceAccounts, ...]
    can_change: bool
    share_command: str
    private_command: str


__all__ = ["AccountStatus", "AccountsPage", "AccountsRefused", "ServiceAccounts"]

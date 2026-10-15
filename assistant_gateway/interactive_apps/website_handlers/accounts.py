"""The HTTP route of the Web Chat's Settings > Accounts tab (read only).

It answers what ``/accounts`` answers in chat, for the signed-in person:
every service that uses an account, whether the team shares it, and whether
the team's and the person's own account are set up
(``GatewayAccountSharing.page``, a typed ``AccountsPage`` turned into JSON here). The person
is the session's user and their roles are looked up again on every read
(``AccessInboxApplication.person``); the browser names nobody. Changing the
sharing stays in chat (``/accounts share|private``), where administrators are
checked and told.

Like the other Web Chat routes it has no tool catalog entry: it is a person's
own view, protected by the signed-in session, not an agent operation.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from assistant_gateway.application.account_overview import AccountsPage, AccountsRefused, ServiceAccounts
from assistant_gateway.interactive_apps.website_handlers.access_inbox import SignedInAccessHandlers
from assistant_gateway.interactive_apps.website_handlers.off_loop import off_loop

LOGGER = logging.getLogger(__name__)


def service_payload(row: ServiceAccounts) -> dict[str, Any]:
    """One row as the tab reads it (accounts_view.mjs)."""

    return {
        "subject": row.subject,
        "name": row.name,
        "sharing": row.sharing.value,
        "fixed": row.fixed,
        "team_account": row.team_account.value if row.team_account else None,
        "own_account": row.own_account.value if row.own_account else None,
        "setup_command": row.setup_command,
        "signs_in": row.signs_in,
    }


def page_payload(page: AccountsPage) -> dict[str, Any]:
    return {
        "services": [service_payload(row) for row in page.services],
        "can_change": page.can_change,
        "share_command": page.share_command,
        "private_command": page.private_command,
    }


class WebsiteAccountsHandlers(SignedInAccessHandlers):
    @off_loop
    def view(self, request: web.Request) -> web.Response:
        # Membership and account reads are synchronous; keep both off the chat apps' shared loop.
        person_id = self._person(request, write=False)
        person = self.application.access_inbox.person(person_id)
        roles = tuple(person.roles) if person is not None else ()
        try:
            page = self.application.accounts.page(person_id=person_id, roles=roles)
        except AccountsRefused as exc:
            LOGGER.warning(
                "Someone who is not an active member opened the Accounts tab. event=web_accounts_refused person_id=%s",
                person_id,
            )
            return self.operations._permission_json_response({"status": "refused", "error": str(exc)}, status=403)
        return self.operations._permission_json_response({"status": "ok", **page_payload(page)})


__all__ = ["WebsiteAccountsHandlers", "page_payload", "service_payload"]

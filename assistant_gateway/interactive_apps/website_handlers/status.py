"""The Status page's route: administrators read the current findings.

Only administrators and owners see it (their roles come from the gateway's
copy of the team's people, never from the browser): findings name
configuration, environment variable names and other people's schedules.
The page reads only; every repair happens elsewhere (``/auth model``,
``team.env``, Settings).
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from access_control.people import is_administrator
from assistant_gateway.interactive_apps.website_handlers.access_inbox import SignedInAccessHandlers
from assistant_gateway.interactive_apps.website_handlers.pagination import list_page_query
from custom_messages.contracts.status_v1 import StatusSeverity

ADMINISTRATORS_ONLY_TEXT = "Only team administrators and owners can see the Status page."


class WebsiteStatusHandlers(SignedInAccessHandlers):
    refusal_event = "web_status_refused"

    def _may_see(self, request: web.Request) -> bool:
        person = self.application.access_inbox.person(self._person(request, write=False))
        return person is not None and is_administrator(tuple(person.roles))

    async def access(self, request: web.Request) -> web.Response:
        """Whether the signed-in person may open the page (the Settings rail shows its link only then)."""

        allowed = await asyncio.to_thread(self._may_see, request)
        return self.operations._permission_json_response({"status": "ok", "allowed": allowed})

    async def report(self, request: web.Request) -> web.Response:
        # Reads the person's roles from the database (website_handlers.off_loop).
        if not await asyncio.to_thread(self._may_see, request):
            return self.operations._permission_json_response(
                {"status": "forbidden", "error": ADMINISTRATORS_ONLY_TEXT}, status=403
            )
        page = list_page_query(request)
        report = await self.application.status.report()
        findings = report.model_dump(mode="json")["findings"]
        counts = {severity.value: 0 for severity in StatusSeverity}
        for finding in report.findings:
            counts[finding.severity.value] += 1
        return self.operations._permission_json_response(
            {
                "status": "ok",
                "checked_at": report.checked_at.isoformat(),
                "unavailable_sources": list(report.unavailable_sources),
                "counts": counts,
                "findings": findings[page["offset"] : page["offset"] + page["limit"]],
                "total": len(findings),
                "has_more": page["offset"] + page["limit"] < len(findings),
            }
        )


__all__ = ["ADMINISTRATORS_ONLY_TEXT", "WebsiteStatusHandlers"]

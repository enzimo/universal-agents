"""People's commands to the team services (protocol 1 request/reply on ``svc.team``).

The gateway sends a person's operation with a command token that names the person by
their opaque reference (plan decision 64) and returns the team services' typed reply
body. The team services decide whether the person may (``team_services.people``); the
gateway only renders the reply. Families move here from the old durable manager
commands one at a time (memory first).

Each command has a fresh request id (a UUIDv7, the contract's "Fresh ids"), which is
also its retry key at the team services. When no answer comes in time, the gateway sends
the same command once more under the same request id: if the first copy is still waiting
or already carried out, the team services answer the second from the stored reply
(``storage.operation_outcomes``), so a slow change is never carried out twice and the
person still gets its answer. When the second copy gets no answer either, the person is
told that a change may still be made, so they check before sending it again.

Ports keep the application free of NATS: ``GatewayTeamServices`` sends one command
(``assistant_gateway.team_services_client`` over the gateway's message host), and
``GatewayPersonReferences`` gives a person's reference from the gateway's copy of the
team's people in the access store, which the team services decide from: the gateway
first writes that copy when it differs from its own records
(``assistant_gateway.people_directory_sync.PeopleDirectorySync.person_ref``), so a
person who was just demoted, disabled or added is never judged by an old copy.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Protocol

# How many times one command is sent when no answer comes in time (module docstring).
SEND_ATTEMPTS = 2
# Commands that only read: when no answer comes, nothing can still be changed.
_READ_KINDS = frozenset({
    "memory.list", "memory.show", "topics.list_tags", "topics.find_threads",
    "knowledge.list", "knowledge.read", "knowledge.history", "knowledge.list_contributions",
    "knowledge.count_contributions", "knowledge.read_contribution",
    "tasks.list", "tasks.list_finished",
})


class TeamServicesUnavailable(RuntimeError):
    """The team services could not be asked, or did not answer; the message says what the person can do."""


class TeamServicesNoAnswer(TeamServicesUnavailable):
    """The command was sent but no answer came in time: it may still be carried out."""


class PersonCopyNotCurrent(TeamServicesUnavailable):
    """The team's copy of the person differs from the gateway's records and could not be written; nothing was sent."""

    def __init__(self) -> None:
        super().__init__("Your current roles could not be passed on to the team services, so the command was not "
                         "sent. Try again in a moment; if it keeps happening, an administrator can find the reason "
                         "in the gateway log.")


class TeamCommandRefused(RuntimeError):
    """The team services refused the request itself (README "Refused requests"): ``code`` names why."""

    def __init__(self, kind: str, code: str) -> None:
        super().__init__(_REFUSALS.get(code, _REFUSED.format(code=code)))
        self.kind = kind
        self.code = code


_REFUSED = ("The team services could not accept this command ({code}). Try again in a moment; if it keeps "
            "happening, an administrator can find the reason in the team services' log.")
_REFUSALS = {
    "unsupported": "The team services do not answer this command; they may be running an older version.",
    "handler_failed": "The team services could not answer this command. Try again in a moment; if it keeps "
                      "happening, an administrator can find the reason in the team services' log.",
}


class GatewayTeamServices(Protocol):
    async def command(self, kind: str, body: dict[str, Any], *, person_ref: str,
                      request_id: str) -> dict[str, Any]:
        """Send one command and return its reply body; raises the two errors above."""


class GatewayPersonReferences(Protocol):
    def person_ref(self, person_id: str) -> str | None:
        """The person's opaque reference, after making the team's copy of them current; None when they have
        no standing in this team. Raises ``PersonCopyNotCurrent`` when the copy could not be made current."""


class GatewayTeamCommandService:
    """Send one person's command to the team services and return the reply body."""

    def __init__(self, *, team_services: GatewayTeamServices, references: GatewayPersonReferences,
                 request_ids: Callable[[], str], logger: logging.Logger | None = None) -> None:
        self._team_services = team_services
        self._references = references
        self._request_ids = request_ids
        self._logger = logger or logging.getLogger(__name__)

    async def request(self, kind: str, body: dict[str, Any], *, person_id: str) -> dict[str, Any]:
        """``body`` without its ``request_id``: each call is a new command with a fresh id (module docstring)."""

        reference = await asyncio.to_thread(self._references.person_ref, person_id)
        if reference is None:
            self._logger.warning("A person's command has no person reference to send. "
                                 "event=gateway_team_command_person_unrecorded kind=%s person=%s", kind, person_id)
            raise TeamServicesUnavailable("You are not a member of this team, so the command was not sent.")
        request_id = self._request_ids()
        command = {"request_id": request_id, **body}
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                return await self._team_services.command(kind, command, person_ref=reference, request_id=request_id)
            except TeamServicesNoAnswer:
                self._logger.warning("A person's command got no answer in time. event=gateway_team_command_unanswered "
                                     "kind=%s request_id=%s attempt=%s of=%s", kind, request_id, attempt, SEND_ATTEMPTS)
        if kind in _READ_KINDS:
            raise TeamServicesNoAnswer("The team services did not answer in time. Try again in a moment.")
        raise TeamServicesNoAnswer("The team services did not answer in time, and the change may still be made. "
                                   "Check before sending it again.")

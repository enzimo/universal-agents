"""The Web Chat Knowledge page's use case: people's requests to the team's knowledge base.

The knowledge base lives in the team's database, which the team services serve: each
request becomes one ``knowledge.*`` command for the signed-in person, naming them by
their person reference (``assistant_gateway.application.team_commands``). The team
services decide whether the person may (``team_services.knowledge``); the gateway only
converts the request and the reply (``knowledge_wire``).

Before answering the browser, the gateway names who made each change: an agent by its
type, the signed-in person as "You", another person by their display name from the
gateway's people records. Person ids and references never reach the browser.

A list reply can hold fewer rows than asked for, when more would not fit in one reply
(contract README "Reply size"); it then says where to continue (``next_offset``). The
browser pages through fixed pages of 20 with a total, so the gateway asks again from
there until it has the page the browser asked for (``_whole_page``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable, Protocol

from pydantic import ValidationError

from assistant_gateway.application.knowledge_wire import (
    KnowledgeRequestNotSent,
    current_revision,
    knowledge_request,
    knowledge_view,
    refusal_code,
    review_flags,
)
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.team_commands import (
    GatewayTeamCommandService,
    TeamCommandRefused,
    TeamServicesNoAnswer,
    TeamServicesUnavailable,
)
from custom_messages.contracts.control_v1 import (
    KNOWLEDGE_READ_ACTIONS,
    KnowledgeCommandAction,
    KnowledgeCommandPayload,
    KnowledgeCommandResultPayload,
    KnowledgeReviewFlagPayload,
)

if TYPE_CHECKING:
    from custom_transport.protocol1.blobs import BlobOwner
    from custom_transport.protocol1.knowledge_blobs import BlobStore

LOGGER = logging.getLogger(__name__)

YOU_LABEL = "You"
TEAM_MEMBER_LABEL = "A team member"
SYSTEM_LABEL = "The system"
AGENT_LABEL = "An agent"
SIGN_IN_TEXT = "Sign in with a registered user account to use the Knowledge page."
KNOWLEDGE_OFF_TEXT = (
    "The knowledge base is turned off for this team (agent.knowledge.enabled). Ask an administrator to turn it on."
)
MISSING_TITLE_TEXT = "Give the new page a title."
NOT_SENDABLE_TEXT = (
    "Nothing was sent because the request was not valid (a value is missing, too long or of the wrong kind). "
    "Check the fields and try again."
)
BLOB_STORE_FULL_TEXT = (
    "The team's temporary blob store is full, so this long Knowledge text could not be saved right now. "
    "Ask an administrator to check transport.queues.max_bytes.blobs or agent.blob_retention_days."
)
BLOB_UNAVAILABLE_TEXT = (
    "Long Knowledge text needs the team's blob store, which is not available in this process."
)
BLOB_READ_FAILED_TEXT = (
    "The full text of this Knowledge page could not be loaded from temporary storage. "
    "Try again; if it keeps failing, an administrator can check the gateway log."
)
BLOB_CHANGE_REPLY_READ_FAILED_TEXT = (
    "Your change was saved, but the updated Knowledge text could not be loaded from temporary storage. "
    "Refresh the page to see it; if it keeps failing, an administrator can check the gateway log."
)
BLOB_WRITE_FAILED_TEXT = (
    "This long Knowledge text could not be stored for sending, so nothing was changed. "
    "Try again; if it keeps failing, an administrator can check the gateway log."
)
# Lists the gateway completes from their continuations (module docstring), and their rows.
CONTINUED_ROWS = {
    KnowledgeCommandAction.LIST: "pages",
    KnowledgeCommandAction.LIST_CONTRIBUTIONS: "contributions",
    KnowledgeCommandAction.HISTORY: "revisions",
}


class KnowledgeEditorReferences(Protocol):
    """Reads the gateway's copy of the team's people to name editors; never writes it."""

    def person_ref(self, person_id: str) -> str | None:
        """The person's opaque reference, or None when the copy does not hold them."""

    def person_id(self, person_ref: str) -> str | None:
        """Who a reference in a reply names, or None when no recorded person has it."""


class GatewayKnowledgeRefused(Exception):
    """The team services refused or could not run a Knowledge page request."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        review_flags: tuple[KnowledgeReviewFlagPayload, ...] = (),
        current_revision: int = 0,
        merged_into: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.review_flags = review_flags
        self.current_revision = current_revision
        self.merged_into = merged_into


class GatewayKnowledgePages:
    """Run Knowledge page requests for the signed-in person (module docstring)."""

    def __init__(
        self,
        *,
        team_commands: GatewayTeamCommandService,
        references: KnowledgeEditorReferences,
        display_name: Callable[[str], str],
        enabled: bool,
        blobs: BlobStore | None = None,
        max_body_chars: int = 500_000,
        max_note_chars: int = 50_000,
    ) -> None:
        self._team_commands = team_commands
        self._references = references
        self._display_name = display_name
        self._enabled = bool(enabled)
        self._blobs = blobs
        self._max_body_chars = int(max_body_chars)
        self._max_note_chars = int(max_note_chars)

    @property
    def enabled(self) -> bool:
        """Whether the team's knowledge base is on (``agent.knowledge.enabled``); the rail link shows only then."""

        return self._enabled

    @staticmethod
    def _check_text_limit(text: str | None, limit: int, label: str) -> None:
        length = len(text or "")
        if length > limit:
            raise GatewayKnowledgeRefused(
                "invalid_request",
                f"{label} is {length:,} characters; at most {limit:,} are allowed.",
            )

    def _check_before_blobs(self, payload: KnowledgeCommandPayload) -> None:
        """Refuse disabled or locally invalid requests before uploading blobs to ``OBJ_UA1_BLOBS`` (F7)."""

        if not self._enabled:
            raise GatewayKnowledgeRefused("knowledge_off", KNOWLEDGE_OFF_TEXT)
        if payload.action is KnowledgeCommandAction.WRITE:
            if not (payload.page or "").strip() and not (payload.title or "").strip():
                raise GatewayKnowledgeRefused("invalid_request", MISSING_TITLE_TEXT)
            self._check_text_limit(payload.body, self._max_body_chars, "Page text")
        elif payload.action is KnowledgeCommandAction.ADD_NOTE:
            self._check_text_limit(payload.note, self._max_note_chars, "Note text")
        elif payload.action is KnowledgeCommandAction.PUBLISH_CONTRIBUTION and payload.edited:
            self._check_text_limit(payload.body, self._max_body_chars, "Edited proposal text")

    async def run(
        self, context: GatewayRouteContextV1, payload: KnowledgeCommandPayload
    ) -> KnowledgeCommandResultPayload:
        """The team services' result with editors named; ``GatewayKnowledgeRefused`` when refused."""

        person_id = str(context.canonical_user_id or "").strip()
        if not person_id:
            raise PermissionError(SIGN_IN_TEXT)
        self._check_before_blobs(payload)
        try:
            kind, body = knowledge_request(payload)
        except KnowledgeRequestNotSent as error:
            raise GatewayKnowledgeRefused("invalid_request", str(error)) from error
        reply = await self._request(kind, body, person_id, payload.action)
        rows = CONTINUED_ROWS.get(payload.action)
        if rows is not None:
            reply = await self._whole_page(kind, body, person_id, payload.action, reply, rows)
        outcome = str(reply.get("outcome"))
        if outcome != "ok":
            raise GatewayKnowledgeRefused(
                refusal_code(outcome),
                str(reply.get("message") or ""),
                review_flags=review_flags(reply),
                current_revision=current_revision(reply),
                merged_into=str(reply.get("merged_into") or ""),
            )
        # Names come from the gateway's people records (database reads).
        return await asyncio.to_thread(self._view, payload, reply, person_id)

    async def _request(self, kind: str, body: dict[str, Any], person_id: str,
                       action: KnowledgeCommandAction) -> dict[str, Any]:
        # Imported lazily so command-line gateways never load protocol 1 wire models at import time.
        from custom_transport.protocol1.blobs import BlobOwner
        from custom_transport.protocol1.knowledge_blobs import reply_has_blobs, request_needs_blobs

        wire_body = body
        request_id: str | None = None
        person_ref: str | None = None
        if request_needs_blobs(body):
            person_ref = await self._team_commands.resolve_person_ref(person_id, kind=kind)
            request_id = self._team_commands.new_request_id()
            owner = BlobOwner(request_id=request_id, person_ref=person_ref)
            wire_body = await self._encode_request_blobs(body, owner, action)
        try:
            reply = await self._team_commands.request(
                kind, wire_body, person_id=person_id, request_id=request_id, person_ref=person_ref
            )
        except ValidationError as error:
            # The browser's request model let through a value the contract refuses: the gateway's defect.
            LOGGER.error("A Knowledge page request did not fit the message contract and was not sent. "
                         "event=web_knowledge_request_not_sendable action=%s errors=%s", action.value,
                         [".".join(str(part) for part in item["loc"]) for item in error.errors()])
            raise GatewayKnowledgeRefused("invalid_request", NOT_SENDABLE_TEXT) from error
        if reply.get("outcome") == "ok" and reply_has_blobs(reply):
            return await self._decode_reply_blobs(reply, action)
        return reply

    async def _encode_request_blobs(
        self, body: dict[str, Any], owner: "BlobOwner", action: KnowledgeCommandAction
    ) -> dict[str, Any]:
        from custom_transport.protocol1.blobs import BlobError, BlobStoreFull
        from custom_transport.protocol1.knowledge_blobs import KnowledgeBlobUnavailable, fit_request_body

        try:
            return await fit_request_body(body, self._blobs, owner)
        except PermissionError as error:
            raise GatewayKnowledgeRefused("authorization_denied", str(error)) from error
        except BlobStoreFull as error:
            LOGGER.warning("Blob store full while storing a Knowledge request. "
                           "event=web_knowledge_blob_store_full action=%s detail=%s", action.value, error)
            raise GatewayKnowledgeRefused("runtime_error", BLOB_STORE_FULL_TEXT) from error
        except KnowledgeBlobUnavailable as error:
            raise GatewayKnowledgeRefused("runtime_error", BLOB_UNAVAILABLE_TEXT) from error
        except TeamServicesNoAnswer as error:
            LOGGER.warning("Blob registration timed out before sending a Knowledge request. "
                           "event=web_knowledge_blob_register_unanswered action=%s detail=%s", action.value, error)
            raise GatewayKnowledgeRefused("runtime_error", BLOB_WRITE_FAILED_TEXT) from error
        except TeamServicesUnavailable as error:
            LOGGER.warning("Blob store or team services unavailable before sending a Knowledge request. "
                           "event=web_knowledge_blob_unavailable action=%s detail=%s", action.value, error)
            raise GatewayKnowledgeRefused("runtime_error", str(error)) from error
        except (TeamCommandRefused, BlobError) as error:
            LOGGER.error("Could not store a Knowledge request blob. event=web_knowledge_blob_write_failed "
                         "action=%s error_type=%s detail=%s", action.value, type(error).__name__, error)
            raise GatewayKnowledgeRefused("runtime_error", BLOB_WRITE_FAILED_TEXT) from error
        except Exception as error:  # noqa: BLE001 - NATS/JetStream transport errors must not leak HTTP 500
            LOGGER.error("Unexpected transport error while storing a Knowledge request blob. "
                         "event=web_knowledge_blob_write_failed action=%s error_type=%s detail=%s",
                         action.value, type(error).__name__, error, exc_info=error)
            raise GatewayKnowledgeRefused("runtime_error", BLOB_WRITE_FAILED_TEXT) from error

    async def _decode_reply_blobs(
        self, reply: dict[str, Any], action: KnowledgeCommandAction
    ) -> dict[str, Any]:
        from custom_transport.protocol1.knowledge_blobs import KnowledgeBlobUnavailable, resolve_reply_body

        is_read = action in KNOWLEDGE_READ_ACTIONS
        try:
            return await resolve_reply_body(reply, self._blobs)
        except KnowledgeBlobUnavailable as error:
            text = BLOB_UNAVAILABLE_TEXT if is_read else BLOB_CHANGE_REPLY_READ_FAILED_TEXT
            raise GatewayKnowledgeRefused("runtime_error", text) from error
        except Exception as error:  # noqa: BLE001 - blob and NATS/transport errors must not leak HTTP 500
            LOGGER.error("Could not load a Knowledge reply blob. event=web_knowledge_blob_read_failed "
                         "action=%s change=%s error_type=%s detail=%s",
                         action.value, not is_read, type(error).__name__, error, exc_info=error)
            text = BLOB_READ_FAILED_TEXT if is_read else BLOB_CHANGE_REPLY_READ_FAILED_TEXT
            raise GatewayKnowledgeRefused("runtime_error", text) from error

    async def _whole_page(self, kind: str, body: dict[str, Any], person_id: str, action: KnowledgeCommandAction,
                          reply: dict[str, Any], rows: str) -> dict[str, Any]:
        """The rows the browser asked for, asking again from each ``next_offset`` (module docstring)."""

        end = int(body["offset"]) + int(body["limit"])
        while reply.get("outcome") == "ok" and int(reply.get("next_offset", end)) < end:
            start = int(reply["next_offset"])
            more = await self._request(kind, {**body, "offset": start, "limit": end - start}, person_id, action)
            if more.get("outcome") != "ok":
                return more
            if not more.get(rows):
                LOGGER.warning("A Knowledge list continuation returned no rows; the browser gets the rows so far. "
                               "event=web_knowledge_continuation_empty action=%s offset=%s", action.value, start)
                return reply
            reply = {**more, rows: [*reply[rows], *more[rows]]}
        return reply

    def _view(self, payload: KnowledgeCommandPayload, reply: dict[str, Any], person_id: str) -> KnowledgeCommandResultPayload:
        own_reference = self._references.person_ref(person_id)
        names: dict[str, str] = {}

        def label(editor: dict[str, Any]) -> str:
            kind = editor.get("kind")
            if kind == "agent":
                return str(editor.get("agent_type") or AGENT_LABEL)
            if kind != "person":
                return SYSTEM_LABEL
            reference = str(editor.get("person") or "")
            if not reference:
                # Someone the team's people records no longer hold (contract README, "Knowledge").
                return TEAM_MEMBER_LABEL
            if reference == own_reference:
                return YOU_LABEL
            if reference not in names:
                names[reference] = self._person_name(reference)
            return names[reference]

        return knowledge_view(payload.action, reply, label=label)

    def _person_name(self, reference: str) -> str:
        editor_id = self._references.person_id(reference) if reference else None
        name = self._display_name(editor_id) if editor_id else ""
        # The people records answer with the id itself when they have no name.
        return name if name and name != editor_id else TEAM_MEMBER_LABEL


__all__ = ["GatewayKnowledgePages", "GatewayKnowledgeRefused"]

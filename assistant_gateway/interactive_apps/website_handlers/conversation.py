from __future__ import annotations
from assistant_gateway.interactive_apps.website_support import require_website_runtime
import json
import logging
from typing import Any
from aiohttp import web
from assistant_gateway.attachments import StagedAttachmentUnavailable
from assistant_gateway.application.command_help import (
    build_web_chat_help,
)
from assistant_gateway.application.transient_presentation import GatewayTransientAction
from assistant_gateway.application.commands.account_link import ACCOUNT_LINK_COMMAND
from assistant_gateway.domain.message_types import ACCOUNT_LINK_COMMAND_MESSAGE_TYPE
from assistant_gateway.application.gateway_application import (
    GatewayApplicationResponse,
    GatewaySession,
)
from assistant_gateway.application.transcript_service import TranscriptCursorError
from assistant_gateway.application.website_application import (
    GatewaySessionAliasConflict,
    WebTranscriptMessageV1,
    WebsiteGatewayApplication,
    web_chat_threads_revision,
)
from runtime_settings.interactive_apps import InternalWebsiteAppSettings
from assistant_gateway.inbound_attachments import (
    AttachmentValidationError,
    attachment_debug_summary,
)
from assistant_gateway.interactive_apps.host_ports import (
    InteractiveAppRuntimeContext,
)
from custom_messages.contracts.runtime_v1 import (
    DELIVERY_CHANNEL_WEBSITE,
)
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from assistant_gateway.interactive_apps.website_support import (
    LOGGER,
    _CLIENT_ID_PATTERN,
    _WEB_CHAT_ALLOWED_COMMANDS,
    _WEB_CHAT_COMMAND_NOT_AVAILABLE,
)
from .off_loop import off_loop
from .operations import WebsiteConversationHandlersOperations
from .event_stream import stream_events


class WebsiteConversationHandlers:
    """Own conversation HTTP behavior with explicit state and bound operations."""

    def __init__(
        self,
        *,
        application: WebsiteGatewayApplication,
        config: InternalWebsiteAppSettings,
        logger: logging.Logger | None,
        runtime: InteractiveAppRuntimeContext | None,
    ) -> None:
        self.operations: WebsiteConversationHandlersOperations
        self._logger = logger or LOGGER
        self.application = application
        self.channel_name = DELIVERY_CHANNEL_WEBSITE
        self.runtime = runtime
        self.settings = config

    @off_loop
    def _handle_web_chat_sessions(self, request: web.Request) -> web.Response:
        web_session = self.operations._require_web_chat_session(request)
        owner = self.operations._required_web_chat_owner(web_session)
        event_cursor = self.application.transcripts.snapshot_cursor()
        listed = self.application.sessions.list_web_chat_threads(
            owner_canonical_user_id=owner,
            limit=50,
        )
        # `title` is the thread's fixed name (web_chat_threads); null until
        # its first ordinary message, and the page shows "New Chat" then.
        sessions = [
            {
                "session_key": thread.session_key,
                "session_alias": thread.session_alias,
                "conversation_id": thread.conversation_id,
                "title": thread.title,
                "updated_at": thread.updated_at,
            }
            for thread in listed
        ]
        return web.json_response(
            {
                "status": "ok",
                "sessions": sessions,
                "event_cursor": event_cursor,
                # Event pages carry the same revision; a mismatch means reload.
                "threads_revision": web_chat_threads_revision(listed),
            }
        )

    async def _handle_web_chat_new_session(
        self,
        request: web.Request,
    ) -> web.Response:
        web_session = self.operations._require_web_chat_session(request)
        owner = self.operations._required_web_chat_owner(web_session)
        parallel_thread = await self._parallel_thread_requested(request)
        session = self.application.start_new_session(
            channel_name=self.channel_name,
            user_id=owner,
            chat_id="web-chat",
            thread_id=None,
            canonical_user_id=owner,
            parallel_thread=parallel_thread,
        )
        return web.json_response(
            {
                "status": "ok",
                "session": {
                    "session_key": session.session_key,
                    "session_alias": session.session_alias,
                    "conversation_id": session.conversation_id,
                    "title": None,
                    "updated_at": session.updated_at,
                    "parallel_thread": session.parallel_thread,
                },
                "event_cursor": self.application.transcripts.snapshot_cursor(),
            },
            status=201,
        )

    @off_loop
    def _handle_web_chat_messages(self, request: web.Request) -> web.Response:
        web_session = self.operations._require_web_chat_session(request)
        owner = self.operations._required_web_chat_owner(web_session)
        session_key = str(request.match_info.get("session_key") or "").strip()
        before_cursor = str(request.query.get("before") or "").strip() or None
        try:
            limit = int(str(request.query.get("limit") or "50"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text="limit must be an integer.") from exc
        try:
            page = self.application.transcripts.list_messages(
                owner_canonical_user_id=owner,
                session_key=session_key,
                before_cursor=before_cursor,
                limit=limit,
            )
        except TranscriptCursorError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        if page is None:
            raise web.HTTPNotFound(text="Thread not found.")
        return web.json_response(
            {
                "status": "ok",
                **page.model_dump(mode="json"),
                "event_cursor": self.application.transcripts.snapshot_cursor(),
            }
        )

    @off_loop
    def _handle_web_chat_events(self, request: web.Request) -> web.Response:
        return self._web_chat_events_response(
            request, str(request.query.get("after") or "").strip() or None
        )

    async def _handle_web_chat_event_stream(self, request: web.Request) -> web.StreamResponse:
        return await stream_events(
            request, lambda cursor: self._web_chat_events_response(request, cursor)
        )

    def _web_chat_events_response(
        self, request: web.Request, after_cursor: str | None
    ) -> web.Response:
        web_session = self.operations._require_web_chat_session(request)
        owner = self.operations._required_web_chat_owner(web_session)
        try:
            limit = int(str(request.query.get("limit") or "100"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text="limit must be an integer.") from exc
        try:
            page = self.application.transcripts.list_events(
                owner_canonical_user_id=owner,
                after_cursor=after_cursor,
                limit=limit,
            )
        except TranscriptCursorError as exc:
            self._logger.warning(
                "event=web_chat_transcript_cursor_reset error_category=invalid_cursor"
            )
            return web.json_response(
                {
                    "status": "cursor_reset_required",
                    "message": str(exc),
                    "event_cursor": self.application.transcripts.snapshot_cursor(),
                },
                status=409,
            )
        # Live credential actions are deliberately outside the durable page.
        # Never cache a response containing a bearer form URL.
        actions = self.application.transcripts.list_credential_actions(
            owner_canonical_user_id=owner
        )
        # Thread renames, archives, new threads and running tasks write no
        # transcript row, so every read also carries live thread-list state.
        thread_states = self.application.sessions.web_chat_thread_states(
            owner_canonical_user_id=owner
        )
        return web.json_response(
            {
                "status": "ok",
                **page.model_dump(mode="json"),
                **thread_states.model_dump(mode="json"),
                "team_activity": self._team_activity(web_session),
                "transient_actions": [
                    {
                        "session_key": item.session_key,
                        "kind": item.action.kind,
                        "item_id": f"{item.action.kind}:{item.request_id}",
                        "request_id": item.request_id,
                        "correlation_id": item.correlation_id,
                        "session_sequence": item.anchor.session_sequence,
                        "created_at_utc": item.anchor.created_at_utc,
                        "content": item.action.description,
                        "action": {
                            "label": item.action.label,
                            "url": item.action.url,
                            "expires_at_utc": item.action.expires_at_utc,
                        },
                    }
                    for item in actions
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    def _team_activity(self, web_session: Any) -> dict[str, Any] | None:
        """The team's live status for every live page (registered people only, as on /api/web-chat/team).

        The sidebar activity mesh, Settings → Team and the Tasks view all read
        this one copy, so an open page asks the gateway once every live page
        (two seconds) instead of once per view. Presence is kept in memory,
        so adding it to the page costs no database read.
        """

        if not web_session.canonical_user_id:
            return None
        team = self.application.operations.list_web_chat_team_members(presence_only=True)
        return {"members": team.model_dump(mode="json")["members"]}

    async def _handle_web_chat_attachment(
        self, request: web.Request
    ) -> web.StreamResponse:
        web_session = self.operations._require_web_chat_session(request)
        attachment_id = str(request.match_info.get("attachment_id") or "").strip()
        if not attachment_id:
            raise web.HTTPNotFound(text="Attachment not found.")

        record = self.application.sessions.get_web_chat_attachment(
            attachment_id=attachment_id,
            owner_canonical_user_id=self.operations._web_session_owner_scope(
                web_session
            ),
            allow_missing_session=web_session.auth_kind != "user",
        )
        if record is None:
            raise web.HTTPNotFound(text="Attachment not found.")
        # Serve exactly the bytes recorded when the file was staged or
        # uploaded: a copy that was cleaned up, replaced by a link, or changed
        # since then is "gone" rather than served from the Web Chat origin.
        try:
            data = (
                require_website_runtime(self.runtime)
                .outgoing_attachments.read_bytes(record.storage_reference, record.sha256)
            )
        except StagedAttachmentUnavailable as exc:
            self._logger.info(
                "A Web Chat attachment is no longer available. "
                "event=web_chat_attachment_gone attachment_id=%s reason=%s",
                record.attachment_id,
                exc.reason,
            )
            raise web.HTTPGone(text="This attachment is no longer available.") from exc
        disposition = (
            "inline" if record.kind in {"image", "audio", "video"} else "attachment"
        )
        return web.Response(
            body=data,
            headers={
                "Content-Type": record.content_type,
                "Content-Disposition": f'{disposition}; filename="{record.filename}"',
            },
        )

    async def _handle_web_chat_archive_session(
        self, request: web.Request
    ) -> web.Response:
        web_session = self.operations._require_web_chat_session(request)
        session_key = str(request.match_info.get("session_key") or "").strip()
        if not self.application.sessions.archive_web_chat_session(
            session_key=session_key,
            owner_canonical_user_id=self.operations._web_session_owner_scope(
                web_session
            ),
        ):
            raise web.HTTPNotFound(text="Thread not found.")
        return web.json_response({"status": "ok"})

    async def _handle_web_chat_rename_session(
        self, request: web.Request
    ) -> web.Response:
        web_session = self.operations._require_web_chat_session(request)
        session_key = str(request.match_info.get("session_key") or "").strip()
        payload = await self.operations._json_payload(request)
        session_alias = str(
            payload.get("session_alias") or payload.get("title") or ""
        ).strip()
        if not session_alias:
            raise web.HTTPBadRequest(text="Thread name is required.")
        if len(session_alias) > 80:
            raise web.HTTPBadRequest(text="Thread name must be 80 characters or fewer.")
        try:
            session = self.application.sessions.rename_web_chat_session(
                session_key=session_key,
                session_alias=session_alias,
                owner_canonical_user_id=self.operations._web_session_owner_scope(
                    web_session
                ),
            )
        except GatewaySessionAliasConflict:
            raise web.HTTPConflict(text=f"Thread name already exists: {session_alias}")
        if session is None:
            raise web.HTTPNotFound(text="Thread not found.")
        return web.json_response(
            {
                "status": "ok",
                "session_key": session.session_key,
                "session_alias": session_alias,
            }
        )

    async def _handle_web_chat_message(self, request: web.Request) -> web.Response:
        web_session = self.operations._require_web_chat_session(request)
        owner = self.operations._required_web_chat_owner(web_session)
        # Files are read here and saved only after every check below passes, so
        # a refused, resent or misaddressed send leaves no file behind, least of
        # all in another person's conversation.
        payload, uploads = await self._web_chat_payload_and_uploads(request)
        parallel_thread = self._parallel_thread_value(payload, multipart=request.content_type.startswith("multipart/"))
        text = str(payload.get("message") or "").strip()
        if not text and not uploads:
            raise web.HTTPBadRequest(text="message is required.")
        client_request_id = self._required_client_request_id(payload)
        session_key, existing_session = self._senders_web_chat_session(
            payload, owner=owner
        )
        conversation_id = existing_session.conversation_id
        task_text = text or "User sent attachment(s)."

        early_response = await self._web_chat_early_response(
            owner=owner,
            session_key=session_key,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            text=text,
            has_attachments=bool(uploads),
        )
        if early_response is not None:
            return early_response

        # A "?" with files is an ordinary message, not a progress question.
        waiting_question_id = (
            None
            if not uploads and self.application.is_task_progress_status_request(text)
            else await self.application.waiting_question_id(session_key)
        )
        if uploads and waiting_question_id:
            raise web.HTTPBadRequest(
                text="Attachments are not supported while answering a pending task query."
            )
        if parallel_thread:
            self._mark_parallel_reply(existing_session, owner=owner, text=text,
                                      has_attachments=bool(uploads), waiting_question_id=waiting_question_id)
        # Saved under the checked conversation's own key, which names its owner
        # (the browser may name the conversation by an alias).
        attachments = self._store_web_chat_uploads(
            uploads,
            session_key=existing_session.session_key,
            conversation_id=conversation_id,
        )
        if attachments:
            attachment_summary = [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"local_path", "local_exists", "local_size_bytes"}
                }
                for item in attachment_debug_summary(attachments)
            ]
            self._logger.info(
                "Web Chat inbound to manager: user_id=%s session=%s "
                "conversation=%s attachment_count=%d attachment_summary=%s",
                owner,
                session_key,
                conversation_id or "-",
                len(attachments),
                json.dumps(attachment_summary, sort_keys=True),
            )
        try:
            submission = await self._submit_web_chat_turn(
                owner=owner,
                session_key=session_key,
                conversation_id=conversation_id,
                client_request_id=client_request_id,
                text=text,
                task_text=task_text,
                attachments=attachments,
                waiting_question_id=waiting_question_id,
            )
        except TimeoutError as exc:
            raise web.HTTPGatewayTimeout(text=str(exc)) from exc
        if isinstance(submission, web.Response):
            return submission
        response, messages = submission
        response_payload: dict[str, Any] = {
            "status": "ok",
            "session_key": session_key,
            "conversation_id": response.conversation_id,
            "messages": [
                self._transcript_message_payload(message) for message in messages
            ],
            "event_cursor": self.application.transcripts.snapshot_cursor(),
        }
        transient_payload = self._transient_action_payload(response, messages=messages)
        if transient_payload is not None:
            response_payload["transient"] = transient_payload
            self._log_transient_action_included(
                response,
                session_key=session_key,
            )
        web_response = web.json_response(response_payload)
        web_response.headers["Cache-Control"] = "no-store"
        return web_response

    def _mark_parallel_reply(
        self, session: GatewaySession, *, owner: str, text: str,
        has_attachments: bool, waiting_question_id: str | None,
    ) -> None:
        """Mark ordinary replies; keep commands and task controls on their own paths."""

        if waiting_question_id or (not has_attachments and (
            text.startswith("/") or self.application.is_task_progress_status_request(text)
        )):
            return
        if not session.conversation_id:
            raise web.HTTPConflict(text="This chat has no conversation. Reload it and try again.")
        if not self.application.sessions.mark_web_chat_session_parallel(
            session_key=session.session_key, conversation_id=session.conversation_id,
            owner_canonical_user_id=owner,
        ):
            raise web.HTTPConflict(text="The chat changed before it could be marked for parallel work. Reload it and try again.")

    def _senders_web_chat_session(
        self,
        payload: dict[str, Any],
        *,
        owner: str,
    ) -> tuple[str, GatewaySession]:
        """The conversation a send names, as named and as stored, when it is the sender's."""

        session_key = str(payload.get("session_key") or "").strip()
        if not session_key:
            raise web.HTTPBadRequest(
                text="session_key is required; create a Web Chat session first."
            )
        existing_session = self.application.sessions.get_session_details(session_key)
        if (
            existing_session is None
            or not self.application.sessions.can_access_web_chat_session(
                session_key=session_key,
                owner_canonical_user_id=owner,
            )
        ):
            raise web.HTTPNotFound(text="Thread not found.")
        requested_conversation_id = (
            str(payload.get("conversation_id") or "").strip() or None
        )
        if (
            requested_conversation_id
            and existing_session.conversation_id
            and requested_conversation_id != existing_session.conversation_id
        ):
            raise web.HTTPConflict(
                text="conversation_id does not match the server session."
            )
        return session_key, existing_session

    async def _submit_web_chat_turn(
        self,
        *,
        owner: str,
        session_key: str,
        conversation_id: str | None,
        client_request_id: str,
        text: str,
        task_text: str,
        attachments: list[UserTaskAttachment],
        waiting_question_id: str | None,
    ) -> (
        tuple[GatewayApplicationResponse, tuple[WebTranscriptMessageV1, ...]]
        | web.Response
    ):
        if waiting_question_id is None:
            response = await self.application.send_user_message(
                task_text,
                session_key=session_key,
                conversation_id=conversation_id,
                channel_name=self.channel_name,
                user_id=owner,
                chat_id="web-chat",
                thread_id=None,
                original_message_id=client_request_id,
                canonical_user_id=owner,
                attachments=attachments,
            )
            exchange = self.application.transcripts.get_exchange(
                owner_canonical_user_id=owner,
                session_key=session_key,
                client_request_id=client_request_id,
            )
            if exchange is None:
                raise RuntimeError(
                    "Accepted Web Chat exchange was not present in the transcript."
                )
            return response, tuple(
                message for message in exchange if message is not None
            )
        # The handler refuses files while a question waits, before saving them.
        acceptance = self.application.transcripts.record_local_user_message(
            owner_canonical_user_id=owner,
            session_key=session_key,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            user_text=text,
        )
        if acceptance is None:
            raise web.HTTPNotFound(text="Thread not found.")
        accepted, created = acceptance
        if not created:
            exchange = self.application.transcripts.get_exchange(
                owner_canonical_user_id=owner,
                session_key=session_key,
                client_request_id=client_request_id,
            )
            if exchange is None:
                raise RuntimeError(
                    "Accepted Web Chat query reply was not present in the transcript."
                )
            return self._existing_exchange_response(
                session_key=session_key,
                conversation_id=conversation_id,
                exchange=exchange,
            )
        # The answer names the question found before the send was accepted.
        response = await self.application.reply_to_pending_query(
            session_key=session_key,
            task_query_id=waiting_question_id,
            response_text=text,
        )
        reply = self.application.transcripts.record_local_response(
            owner_canonical_user_id=owner,
            session_key=session_key,
            client_request_id=client_request_id,
            response=response,
        )
        return response, tuple(
            message for message in (accepted, reply) if message is not None
        )

    async def _web_chat_early_response(
        self,
        *,
        owner: str,
        session_key: str,
        conversation_id: str | None,
        client_request_id: str,
        text: str,
        has_attachments: bool,
    ) -> web.Response | None:
        if has_attachments and text.startswith("/"):
            raise web.HTTPBadRequest(text="Slash commands cannot include attachments.")
        existing_exchange = self.application.transcripts.get_exchange(
            owner_canonical_user_id=owner,
            session_key=session_key,
            client_request_id=client_request_id,
        )
        if existing_exchange is not None:
            return self._existing_exchange_response(
                session_key=session_key,
                conversation_id=conversation_id,
                exchange=existing_exchange,
            )
        return await self._web_chat_command_response(
            owner=owner,
            session_key=session_key,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            text=text,
        )

    async def _web_chat_command_response(
        self,
        *,
        owner: str,
        session_key: str,
        conversation_id: str | None,
        client_request_id: str,
        text: str,
    ) -> web.Response | None:
        if not text.startswith("/"):
            return None
        command_name = text.split(maxsplit=1)[0].strip().lower()
        if command_name == "/reply":
            return self._local_exchange_response(
                owner=owner,
                session_key=session_key,
                conversation_id=conversation_id,
                client_request_id=client_request_id,
                user_text=text,
                response_text="The /reply command is available only in the local CLI.",
                response_message_type="command",
            )
        if command_name not in _WEB_CHAT_ALLOWED_COMMANDS:
            return self._local_exchange_response(
                owner=owner,
                session_key=session_key,
                conversation_id=conversation_id,
                client_request_id=client_request_id,
                user_text=text,
                response_text=_WEB_CHAT_COMMAND_NOT_AVAILABLE,
                response_message_type="command",
            )
        acceptance = self.application.transcripts.record_local_user_message(
            owner_canonical_user_id=owner,
            session_key=session_key,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            user_text=self._safe_command_transcript_text(text),
        )
        if acceptance is None:
            raise web.HTTPNotFound(text="Thread not found.")
        accepted, created = acceptance
        if not created:
            exchange = self.application.transcripts.get_exchange(
                owner_canonical_user_id=owner,
                session_key=session_key,
                client_request_id=client_request_id,
            )
            if exchange is None:
                raise RuntimeError(
                    "Accepted Web Chat command was not present in the transcript."
                )
            return self._existing_exchange_response(
                session_key=session_key,
                conversation_id=conversation_id,
                exchange=exchange,
            )
        try:
            command_result = await self.application.execute_interactive_command(
                raw_text=text,
                current_session_key=session_key,
                channel_name=self.channel_name,
                user_id=owner,
                chat_id="web-chat",
                thread_id=None,
                canonical_user_id=owner,
                help_text=self._web_chat_help(),
                enable_debug_commands=False,
                enable_reply_command=False,
                enable_channel_binding_commands=False,
                correlation_id=client_request_id,
            )
        except PermissionError as exc:
            raise web.HTTPNotFound(text="Thread not found.") from exc
        if not command_result.handled:
            command_text = _WEB_CHAT_COMMAND_NOT_AVAILABLE
        else:
            command_text = command_result.text or ""
        response_message_type = self._command_message_type(text)
        action = command_result.transient_action
        reply = self.application.transcripts.record_local_response(
            owner_canonical_user_id=owner,
            session_key=session_key,
            client_request_id=client_request_id,
            response=GatewayApplicationResponse(
                text=command_text,
                conversation_id=conversation_id,
                correlation_id=client_request_id,
                message_type=response_message_type,
                # A card's own secret-free line replaces the reply in history.
                transcript_text=action.receipt if action is not None else None,
            ),
        )
        if reply is None:
            raise RuntimeError("Web Chat command response was not persisted.")
        return self._recorded_exchange_response(
            session_key=(command_result.session_key or session_key),
            conversation_id=conversation_id,
            exchange=(accepted, reply),
            response_text=command_text,
            response_message_type=response_message_type,
            transient_action=command_result.transient_action,
        )

    def _existing_exchange_response(
        self,
        *,
        session_key: str,
        conversation_id: str | None,
        exchange: tuple[WebTranscriptMessageV1, WebTranscriptMessageV1 | None],
    ) -> web.Response:
        messages = tuple(message for message in exchange if message is not None)
        return web.json_response(
            {
                "status": "ok" if exchange[1] is not None else "processing",
                "session_key": session_key,
                "conversation_id": conversation_id,
                "messages": [
                    self._transcript_message_payload(message) for message in messages
                ],
                "event_cursor": self.application.transcripts.snapshot_cursor(),
            }
        )

    def _local_exchange_response(
        self,
        *,
        owner: str,
        session_key: str,
        conversation_id: str | None,
        client_request_id: str,
        user_text: str,
        response_text: str,
        response_message_type: str,
        response_session_key: str | None = None,
    ) -> web.Response:
        exchange = self.application.transcripts.record_local_exchange(
            owner_canonical_user_id=owner,
            session_key=session_key,
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            user_text=user_text,
            response_text=response_text,
            response_message_type=response_message_type,
        )
        if exchange is None:
            raise web.HTTPNotFound(text="Thread not found.")
        return self._recorded_exchange_response(
            session_key=response_session_key or session_key,
            conversation_id=conversation_id,
            exchange=exchange,
            response_text=response_text,
            response_message_type=response_message_type,
        )

    def _recorded_exchange_response(
        self,
        *,
        session_key: str,
        conversation_id: str | None,
        exchange: tuple[WebTranscriptMessageV1, WebTranscriptMessageV1],
        response_text: str,
        response_message_type: str,
        transient_action: GatewayTransientAction | None = None,
    ) -> web.Response:
        payload: dict[str, Any] = {
            "status": "ok",
            "session_key": session_key,
            "conversation_id": conversation_id,
            "messages": [
                self._transcript_message_payload(message) for message in exchange
            ],
            "event_cursor": self.application.transcripts.snapshot_cursor(),
        }
        if response_message_type in {
            "command_auth",
            ACCOUNT_LINK_COMMAND_MESSAGE_TYPE,
            "command_user_sensitive",
        }:
            response_message = exchange[-1]
            payload["transient"] = {
                "kind": "sensitive_command_result",
                "item_id": f"sensitive_command_result:{response_message.message_id}",
                "session_sequence": response_message.session_sequence,
                "created_at_utc": response_message.model_dump(mode="json")[
                    "created_at_utc"
                ],
                "content": response_text,
            }
            if transient_action is not None:
                # Keep bearer links in the initiating response only, never history.
                payload["transient"] = self._transient_action_payload(
                    GatewayApplicationResponse(
                        text="",
                        conversation_id=conversation_id,
                        correlation_id=response_message.correlation_id,
                        message_type=response_message_type,
                        transient_action=transient_action,
                    ),
                    messages=exchange,
                )
        response = web.json_response(payload)
        response.headers["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _command_message_type(command: str) -> str:
        tokens = str(command or "").strip().lower().split()
        if tokens and tokens[0] == "/auth":
            return "command_auth"
        if tokens and tokens[0] == ACCOUNT_LINK_COMMAND:
            # The reply holds a one-time form link; keep it out of history.
            return ACCOUNT_LINK_COMMAND_MESSAGE_TYPE
        if (
            tokens
            and tokens[0] == "/user"
            and any(token in {"token", "invite", "code"} for token in tokens[1:])
        ):
            return "command_user_sensitive"
        return "command"

    @staticmethod
    def _safe_command_transcript_text(command: str) -> str:
        tokens = str(command or "").strip().split()
        normalized = [token.lower() for token in tokens]
        if len(normalized) >= 2 and normalized[:2] == ["/user", "link"]:
            suffix = " --replace" if "--replace" in normalized[2:] else ""
            return f"/user link [redacted]{suffix}"
        return command

    @staticmethod
    def _transcript_message_payload(
        message: WebTranscriptMessageV1,
    ) -> dict[str, Any]:
        return message.model_dump(mode="json")

    def _transient_action_payload(
        self,
        response: GatewayApplicationResponse,
        *,
        messages: tuple[WebTranscriptMessageV1, ...],
    ) -> dict[str, Any] | None:
        action = response.transient_action
        if action is None:
            return None
        correlation_id = str(response.correlation_id or "").strip()
        anchor = next(
            (
                message
                for message in reversed(messages)
                if message.correlation_id == correlation_id
                and message.message_type == response.message_type
            ),
            None,
        )
        if not correlation_id or anchor is None:
            self._logger.error(
                "Web Chat memory-only action omitted because its durable timeline "
                "position was unavailable. "
                "event=web_chat_transient_action_position_missing "
                "correlation_id=%s action_kind=%s",
                correlation_id or "-",
                action.kind,
            )
            return None
        payload: dict[str, Any] = {
            "kind": action.kind,
            "item_id": f"{action.kind}:{action.request_id or correlation_id}",
            "correlation_id": correlation_id,
            "session_sequence": anchor.session_sequence,
            "created_at_utc": anchor.model_dump(mode="json")["created_at_utc"],
            "content": action.description,
            "action": {
                "label": action.label,
                "url": action.url,
                "expires_at_utc": action.expires_at_utc,
            },
        }
        if action.request_id:
            payload["request_id"] = action.request_id
        return payload

    def _log_transient_action_included(
        self,
        response: GatewayApplicationResponse,
        *,
        session_key: str,
    ) -> None:
        action = response.transient_action
        if action is None:
            return
        self._logger.info(
            "Web Chat response includes a memory-only action. "
            "event=web_chat_transient_action_included "
            "correlation_id=%s session_key=%s action_kind=%s expires_at=%s",
            response.correlation_id,
            session_key,
            action.kind,
            action.expires_at_utc,
        )

    @staticmethod
    def _required_client_request_id(payload: dict[str, Any]) -> str:
        client_request_id = str(payload.get("client_request_id") or "").strip()
        if not client_request_id or not _CLIENT_ID_PATTERN.match(client_request_id):
            raise web.HTTPBadRequest(
                text=(
                    "client_request_id must contain 1-128 letters, digits, or "
                    "the characters . _ : -."
                )
            )
        return client_request_id

    async def _parallel_thread_requested(self, request: web.Request) -> bool:
        """Whether Web Chat launches the new thread as parallel: optional body ``{"parallel_thread": true}``.

        New Chat sends no body. A parallel thread's requests may run at the
        same time as the team lead's other work (custom_agents.execution_lanes).
        """

        if not request.can_read_body:
            return False
        payload = await self.operations._json_payload(request)
        return self._parallel_thread_value(payload)

    @staticmethod
    def _parallel_thread_value(payload: dict[str, Any], *, multipart: bool = False) -> bool:
        """JSON uses a boolean; multipart form fields carry its text spelling."""

        value = payload.get("parallel_thread", False)
        if multipart and value in ("true", "false"):
            value = value == "true"
        if not isinstance(value, bool):
            raise web.HTTPBadRequest(text="parallel_thread must be true or false.")
        return value

    async def _web_chat_payload_and_uploads(
        self,
        request: web.Request,
    ) -> tuple[dict[str, Any], list[Any]]:
        """The send's fields and its uploaded file parts, read but not saved."""

        if not request.content_type.startswith("multipart/"):
            return await self.operations._json_payload(request), []

        if not getattr(getattr(self.settings, "attachments", None), "enabled", True):
            raise web.HTTPBadRequest(text="Web Chat attachments are disabled.")
        form = await request.post()
        payload: dict[str, Any] = {}
        uploads: list[Any] = []
        for key, value in form.items():
            if hasattr(value, "file") and hasattr(value, "filename"):
                uploads.append(value)
            else:
                payload[str(key)] = str(value)
        return payload, uploads

    def _store_web_chat_uploads(
        self,
        uploads: list[Any],
        *,
        session_key: str,
        conversation_id: str | None,
    ) -> list[UserTaskAttachment]:
        """Save and record the files of a send that passed every check.

        Every file is checked before any is saved, so a refused file leaves the
        send's other files unsaved too. A whole send is at most one file's size
        limit plus 2 MB (the app's ``client_max_size``), so holding its files in
        memory is bounded.
        """

        if not uploads:
            return []
        store = require_website_runtime(self.runtime).attachments
        try:
            files = [_upload_file(upload) for upload in uploads]
            for data, filename, content_type in files:
                store.check_bytes(data=data, filename=filename, content_type=content_type)
            return [
                store.store_bytes(
                    data=data,
                    filename=filename,
                    content_type=content_type,
                    session_key=session_key,
                    conversation_id=conversation_id,
                    origin="web_chat",
                ).attachment
                for data, filename, content_type in files
            ]
        except AttachmentValidationError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    def _web_chat_help(self) -> str:
        return build_web_chat_help()


def _upload_file(upload: Any) -> tuple[bytes, str, str]:
    """An uploaded file part's bytes, name and content type."""

    filename = str(getattr(upload, "filename", None) or "attachment")
    source = getattr(upload, "file", None)
    if source is None:
        raise AttachmentValidationError(f"{filename} did not include file bytes.")
    return source.read(), filename, str(getattr(upload, "content_type", "") or "")

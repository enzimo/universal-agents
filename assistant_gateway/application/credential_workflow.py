"""Application-owned secure credential collection, and the owner of the broker page watchers.

Agents never see credential values. For credential collection the broker's
form stores what the person types; the gateway opens the form for the person
the task works for, polls the broker until the form is used or expires, and
reports a secret-free result back to the waiting task
(``secure_results.SecureResultPublisher``). As soon as the form ends it also
posts one secret-free line in the person's chat under the form's own id (the
credential request id), so Web Chat closes the form's card then rather than at
the agent's final answer.

This workflow also owns the other broker page watchers, so they share one
owner and close together: the tool setup pages tasks ask for
(``tool_setup.GatewayToolSetups``) and the pages people open themselves with
``/link`` and ``/auth ... team_client`` (``link_results.GatewayLinkResults``),
which tell a task waiting on the same setup when the person finished it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Callable

from assistant_gateway.application.account_links import GatewayAccountLinks
from assistant_gateway.application.broker_session_watch import (
    WATCH_FINISHED,
    BrokerSessionWatches,
    watch_broker_session,
)
from assistant_gateway.application.broker_sessions import (
    validated_broker_form_session,
    validated_user_form_url,
)
from assistant_gateway.application.broker_sign_in import GatewayBrokerSignIns
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.link_results import GatewayLinkResults
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.ports import (
    GatewayClock,
    GatewayEventOutput,
    GatewaySecureBroker,
)
from assistant_gateway.application.request_service import GatewayRequestService
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.application.secure_results import (
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT,
    GatewayCredentialWorkflowSettings,
    SecureResultPublisher,
)
from assistant_gateway.application.tool_setup import (
    REQUEST_KIND_TOOL_SETUP_RESULT,
    GatewayToolSetups,
)
from assistant_gateway.application.transient_presentation import (
    SECURE_CREDENTIAL_COLLECTION_READY_TEXT,
    GatewayTransientAction,
)
from assistant_gateway.domain.message_types import SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE
from assistant_gateway.domain.sessions import SessionRecord
from assistant_gateway.ports.repositories import SessionRepository, ToolSetupRepository
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SecureCredentialCollectionResult,
    SignupAutomationRequest,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope


REQUEST_KIND_SECURE_CREDENTIAL_COLLECTION_RESULT = (
    "secure_credential_collection_result"
)
# Chat requests whose reply is a task resumed from a page result.
REQUEST_KINDS_SECURE_CREDENTIAL_RESULTS = frozenset(
    {
        REQUEST_KIND_SECURE_CREDENTIAL_COLLECTION_RESULT,
        REQUEST_KIND_TOOL_SETUP_RESULT,
    }
)
_TERMINAL_COLLECTION_STATUSES = frozenset(
    {
        "credential_stored",
        "manual_signup_collected",
        "signup_completed",
        "credential_deleted",
        "credential_unavailable",
        "signup_failed",
    }
)
_RESULT_STATUSES = frozenset(
    {
        "signup_completed",
        "manual_signup_required",
        "manual_signup_collected",
        "signup_failed",
        "credential_stored",
        "credential_deleted",
        "credential_unavailable",
    }
)

TeamRolesResolver = Callable[[str], tuple[str, ...]]

# What the form's closing line says after "The secure form for `<key>` ...".
# Failure does not prove that a write or deletion made no partial changes.
_FORM_ENDINGS = {
    "credential_stored": "is done: the credential was stored.",
    "credential_deleted": "is done: the credential was deleted.",
    "manual_signup_collected": "is done: the sign-up details were stored.",
    "signup_completed": "is done: the sign-up finished and the credential was stored.",
}
_FORM_CHANGE_UNCONFIRMED = "The gateway cannot confirm whether anything was stored or changed."
_FORM_FAILED = f"could not finish. {_FORM_CHANGE_UNCONFIRMED}"
_FORM_UNCONFIRMED = f"could not be checked. {_FORM_CHANGE_UNCONFIRMED}"


def _mapping(value: object) -> dict[str, Any]:
    """A copy of a broker JSON object, or an empty one for anything else."""

    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def _collection_result_status(session_payload: dict[str, Any]) -> str:
    """Use the same allowed status for the chat receipt and the waiting task."""

    content = _mapping(_mapping(session_payload.get("result")).get("content"))
    status = str(content.get("status") or session_payload.get("status") or "credential_unavailable")
    return status if status in _RESULT_STATUSES else "credential_unavailable"


class GatewayCredentialWorkflow:
    """Own secure broker coordination and result publication."""

    def __init__(
        self,
        *,
        settings: GatewayCredentialWorkflowSettings,
        broker: GatewaySecureBroker,
        account_links: GatewayAccountLinks,
        team_roles: TeamRolesResolver,
        manager_client: GatewayManagerClient,
        session_repository: SessionRepository,
        tool_setup_repository: ToolSetupRepository,
        request_service: GatewayRequestService,
        setup_states: Any = None,
        clock: GatewayClock,
        event_output: GatewayEventOutput,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._broker = broker
        self._manager_client = manager_client
        self._session_repository = session_repository
        self._request_service = request_service
        self._clock = clock
        self._event_output = event_output
        self._logger = logger or logging.getLogger(__name__)
        self._watches = BrokerSessionWatches(owner="credential_workflow", logger=self._logger)
        self._publisher = SecureResultPublisher(
            settings=settings,
            manager_client=manager_client,
            request_service=request_service,
            logger=self._logger,
        )
        # The setup pages tasks ask for (sign-ins, account links, the team's
        # sign-in apps). ``team_roles`` gives a person's current team roles
        # from the gateway identity service; the broker needs them to decide
        # who may link the team's account or store its sign-in app.
        self.tool_setups = GatewayToolSetups(
            assistant_display_name=settings.assistant_display_name,
            poll_interval_seconds=lambda: self._settings.poll_interval_seconds,
            broker=broker,
            account_links=account_links,
            sign_ins=GatewayBrokerSignIns(secure_broker=broker, team_id=settings.team_id),
            team_roles=team_roles,
            publisher=self._publisher,
            sessions=session_repository,
            setups=tool_setup_repository,
            request_service=request_service,
            clock=clock,
            # People's last known tool setup states (access_control.tool_setup_states).
            states=setup_states,
            team_id=settings.team_id,
            event_output=event_output,
            logger=self._logger,
        )
        # Tells a person how a page they opened with /link or /auth ended, in
        # the same chat, and a task waiting on that setup that it is done.
        self.link_results = GatewayLinkResults(
            broker=broker,
            sessions=session_repository,
            event_output=event_output,
            clock=clock,
            tool_setups=self.tool_setups,
            logger=self._logger,
        )

    @property
    def pending_poll_count(self) -> int:
        return self._watches.count

    async def start_collection(
        self,
        *,
        request_content: SecureCredentialCollectionRequest,
        request_envelope: MessageEnvelope,
        correlation_id: str,
        conversation_id: str | None,
        session_key: str,
    ) -> GatewayResponse:
        if not self._broker.available:
            return await self._collection_start_failure(
                request_content=request_content,
                request_envelope=request_envelope,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                session_key=session_key,
                text=(
                    "Secure credential collection is unavailable because the "
                    "gateway cannot reach the secure tool broker. Configure "
                    "SECURE_TOOL_BROKER_INTERNAL_BASE_URL and "
                    "SECURE_TOOL_BROKER_TOKEN."
                ),
            )

        session = self._require_session(session_key)
        owner_canonical_user_id = str(session.owner_canonical_user_id or "").strip()
        if not owner_canonical_user_id:
            return await self._collection_start_failure(
                request_content=request_content,
                request_envelope=request_envelope,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                session_key=session_key,
                text=(
                    "Secure credential collection requires a registered user identity."
                ),
            )
        try:
            session_payload = await self._broker.create_collection_session(
                request=request_content.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
                context=self._broker_context(
                    owner_canonical_user_id=owner_canonical_user_id,
                    task_id=request_content.parent_task_id,
                    conversation_id=conversation_id,
                ),
            )
        except Exception as exc:
            self._logger.warning(
                "Secure credential collection session creation failed. "
                "event=gateway_credential_collection_create_failed "
                "correlation_id=%s session_key=%s credential_key=%s error=%s",
                correlation_id,
                session_key,
                request_content.credential_key,
                exc,
            )
            return await self._collection_start_failure(
                request_content=request_content,
                request_envelope=request_envelope,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                session_key=session_key,
                text="Secure credential collection session could not be created.",
            )

        try:
            broker_session = validated_broker_form_session(
                session_payload, now=self._clock.now_utc()
            )
            form_url = validated_user_form_url(
                self._broker.user_form_url(broker_session.form_url)
            )
        except ValueError as exc:
            self._logger.error(
                "Credential collection broker session response was invalid. "
                "event=gateway_credential_collection_session_invalid "
                "correlation_id=%s session_key=%s credential_key=%s error=%s",
                correlation_id,
                session_key,
                request_content.credential_key,
                exc,
            )
            return await self._collection_start_failure(
                request_content=request_content,
                request_envelope=request_envelope,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                session_key=session_key,
                text="Secure credential collection session could not be created.",
            )
        if request_content.parent_task_id:
            self._request_service.transition_with_session_lineage(
                correlation_id=correlation_id,
                status=REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
                session_key=session.session_key,
                conversation_id=conversation_id or session.conversation_id,
                last_task_id=request_content.parent_task_id,
                manager_agent_id=self._manager_client.manager_id,
            )
        else:
            self._request_service.update_status(
                correlation_id,
                REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
            )
        session_id = broker_session.session_id
        self._watches.start(
            self.poll_session(
                status_url=broker_session.status_url,
                session_id=session_id,
                request_content=request_content,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                session_key=session_key,
            ),
            name=f"gateway_credential_poll_{session_id[:12] or correlation_id}",
        )

        if self._is_deletion_request(request_content):
            text = (
                "Secure credential deletion confirmation required for "
                f"`{request_content.credential_key}`.\n\n"
                f"Open this broker form to confirm deletion: {form_url}\n\n"
                "The agent will receive only redacted deletion metadata after "
                "submission."
            )
            action_label = "Open deletion confirmation"
            action_description = (
                "Secure credential deletion confirmation is ready for "
                f"`{request_content.credential_key}`.\n\n"
                "Use the secure form below to review and confirm deletion. "
                "The agent will receive only redacted deletion metadata after "
                "submission."
            )
        else:
            text = (
                "Secure credential collection required for "
                f"`{request_content.credential_key}`.\n\n"
                f"Open this broker form to submit the requested fields: "
                f"{form_url}\n\n"
                "The agent will receive only redacted completion metadata "
                "after submission."
            )
            action_label = "Open secure credential form"
            action_description = (
                "Secure credential collection is ready for "
                f"`{request_content.credential_key}`.\n\n"
                "Use the secure form below to submit the requested fields. "
                "The agent will receive only redacted completion metadata after "
                "submission."
            )
        return self._response(
            request_content=request_content,
            request_envelope=request_envelope,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            text=text,
            transcript_text=SECURE_CREDENTIAL_COLLECTION_READY_TEXT,
            transient_action=GatewayTransientAction(
                kind="secure_credential_collection",
                label=action_label,
                description=action_description,
                url=form_url,
                expires_at_utc=broker_session.expires_at,
                # The form's own id: its closing line closes exactly this card.
                request_id=request_content.credential_request_id,
            ),
        )

    def is_manager_sender(self, envelope: MessageEnvelope) -> bool:
        """Only the current manager may ask the gateway to open a form."""

        return bool(
            envelope.msg_header
            and self._manager_client.manager_id
            and envelope.msg_header.from_agent_id == self._manager_client.manager_id
        )

    async def execute_approved_signup_probe(
        self,
        signup_request: SignupAutomationRequest,
        *,
        session_key: str,
        conversation_id: str | None,
    ) -> dict[str, Any] | None:
        if not self._broker.available:
            return None
        session = self._require_session(session_key)
        owner_canonical_user_id = str(session.owner_canonical_user_id or "").strip()
        if not owner_canonical_user_id:
            return None
        try:
            return await self._broker.execute_approved_signup(
                request=signup_request.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
                context=self._broker_context(
                    owner_canonical_user_id=owner_canonical_user_id,
                    task_id=signup_request.parent_task_id,
                    conversation_id=conversation_id,
                ),
            )
        except Exception as exc:
            self._logger.warning(
                "Approved signup probe errored. "
                "event=gateway_approved_signup_probe_failed "
                "credential_key=%s session_key=%s error=%s",
                signup_request.credential_key,
                session_key,
                exc,
            )
            return None

    async def fail_unmatched_collection_request(
        self,
        *,
        request_content: SecureCredentialCollectionRequest,
        request_envelope: MessageEnvelope,
        correlation_id: str,
        conversation_id: str | None,
        session_key: str,
    ) -> None:
        """Terminate a request that can no longer return through its live Web turn."""

        await self._collection_start_failure(
            request_content=request_content,
            request_envelope=request_envelope,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            session_key=session_key,
            text=(
                "Secure credential collection could not continue because its "
                "interactive response expired. Start the request again."
            ),
        )

    async def poll_session(
        self,
        *,
        status_url: str,
        session_id: str,
        request_content: SecureCredentialCollectionRequest,
        correlation_id: str,
        conversation_id: str | None,
        session_key: str,
    ) -> None:
        def read_failed(exc: Exception) -> None:
            self._logger.warning(
                "Credential collection status poll failed. "
                "event=gateway_credential_collection_poll_failed "
                "correlation_id=%s session_key=%s "
                "broker_session_id_prefix=%s error=%s",
                correlation_id,
                session_key,
                session_id[:12],
                exc,
            )

        outcome = await watch_broker_session(
            read_status=lambda: self._broker.get_collection_status(
                status_url=status_url, session_id=session_id
            ),
            is_final=lambda payload: str(payload.get("status") or "")
            in _TERMINAL_COLLECTION_STATUSES,
            deadline=self._clock.now_utc()
            + timedelta(seconds=self._broker.collection_ttl_seconds + 30),
            clock=self._clock,
            poll_interval_seconds=self._settings.poll_interval_seconds,
            on_read_failed=read_failed,
        )
        final_payload = outcome.payload if outcome.ending == WATCH_FINISHED else None
        if final_payload is None:
            final_payload = {
                "session_id": session_id,
                "status": "credential_unavailable",
                "result": {
                    "status": "error",
                    # A lost session or a deadline can follow a successful or
                    # still-running write, so neither proves no submission.
                    "error": "Credential collection result could not be confirmed.",
                    "content": {
                        "status": "credential_unavailable",
                        "message": _FORM_CHANGE_UNCONFIRMED,
                    },
                },
            }
        # Neither destination must wait for the other. Start the chat receipt
        # first, but keep both operations owned by this watcher through failure
        # and shutdown rather than leaving a detached notification task.
        results = await asyncio.gather(
            self._post_closing(
                request_content,
                _collection_result_status(final_payload),
                session_key,
                completion_confirmed=outcome.ending == WATCH_FINISHED,
            ),
            self.publish_result(
                request_content=request_content,
                session_payload=final_payload,
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                session_key=session_key,
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def publish_result(
        self,
        *,
        request_content: SecureCredentialCollectionRequest,
        session_payload: dict[str, Any],
        correlation_id: str,
        conversation_id: str | None,
        session_key: str,
    ) -> None:
        """Send the waiting task the form's secret-free result."""

        result_payload = _mapping(session_payload.get("result"))
        content_payload = _mapping(result_payload.get("content"))
        status = _collection_result_status(session_payload)
        metadata = _mapping(content_payload.get("metadata"))
        metadata.setdefault("flow", request_content.flow)
        metadata.setdefault("source", request_content.source)
        metadata.setdefault("source_item_id", request_content.source_item_id)
        metadata.setdefault(
            "source_item_name",
            request_content.source_item_name,
        )
        message = str(content_payload.get("message") or "")
        session = self._require_session(session_key)
        result = SecureCredentialCollectionResult(
            credential_request_id=request_content.credential_request_id,
            status=status,
            credential_key=request_content.credential_key,
            service_name=request_content.service_name,
            display_name=request_content.display_name,
            parent_task_id=request_content.parent_task_id,
            broker_session_id=str(session_payload.get("session_id") or ""),
            metadata=metadata,
            message=message,
            error=str(result_payload.get("error") or "") or None,
            continue_task=request_content.continue_task,
        )
        await self._publisher.publish(
            result,
            session=session,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            request_kind=REQUEST_KIND_SECURE_CREDENTIAL_COLLECTION_RESULT,
            request_text=message,
            parent_task_id=request_content.parent_task_id,
        )

    async def _post_closing(
        self, request_content: SecureCredentialCollectionRequest, status: str, session_key: str,
        *, completion_confirmed: bool,
    ) -> None:
        """One secret-free line in the form's chat, under the form's own id, so its card closes (logged).

        A failed post must not prevent sending the waiting task its result.
        The broker's confirmed status and an unreadable outcome stay distinct;
        neither a failure nor a lost session proves that no changes were saved.
        """

        session = self._session_repository.get_session(session_key)
        if session is None:
            self._logger.warning(
                "A secure credential form ended in a chat that no longer exists; no closing line posted. "
                "event=gateway_credential_form_closing_skipped credential_request_id=%s session_key=%s",
                request_content.credential_request_id,
                session_key,
            )
            return
        ending = _FORM_ENDINGS.get(status, _FORM_FAILED) if completion_confirmed else _FORM_UNCONFIRMED
        text = f"The secure form for `{request_content.credential_key}` {ending}"
        try:
            await self._event_output.emit(
                GatewayDeliveryEvent(
                    text=text,
                    session_key=session.session_key,
                    channel_name=session.channel_name,
                    user_id=session.user_id,
                    chat_id=session.chat_id,
                    thread_id=session.thread_id,
                    conversation_id=session.conversation_id,
                    scheduled_job_id=None,
                    correlation_id=request_content.credential_request_id,
                    original_message_id=None,
                    message_type=SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE,
                )
            )
        except Exception:  # noqa: BLE001 - task result publication must still proceed
            self._logger.exception(
                "Could not post how a secure credential form ended. event=gateway_credential_form_closing_failed "
                "credential_request_id=%s session_key=%s status=%s",
                request_content.credential_request_id,
                session.session_key,
                status,
            )
            return
        self._logger.info(
            "Posted how a secure credential form ended. event=gateway_credential_form_closing_posted "
            "credential_request_id=%s status=%s",
            request_content.credential_request_id,
            status,
        )

    async def close(self) -> None:
        await self._watches.close()
        await self.tool_setups.close()
        await self.link_results.close()

    def _require_session(self, session_key: str) -> SessionRecord:
        session = self._session_repository.get_session(session_key)
        if session is None:
            raise RuntimeError(f"Gateway session {session_key!r} is unavailable.")
        return session

    def _broker_context(
        self,
        *,
        owner_canonical_user_id: str,
        task_id: str | None,
        conversation_id: str | None,
    ) -> dict[str, Any]:
        return {
            "canonical_user_id": owner_canonical_user_id,
            "team_id": self._settings.team_id,
            "principal_type": "user",
            "task_id": task_id,
            "conversation_id": conversation_id,
        }

    @staticmethod
    def _is_deletion_request(
        request_content: SecureCredentialCollectionRequest,
    ) -> bool:
        return str(request_content.flow or "").strip().lower() in {
            "credential_deletion",
            "credential_delete",
            "delete_credential",
        }

    async def _collection_start_failure(
        self,
        *,
        request_content: SecureCredentialCollectionRequest,
        request_envelope: MessageEnvelope,
        correlation_id: str,
        conversation_id: str | None,
        session_key: str,
        text: str,
    ) -> GatewayResponse:
        try:
            await self.publish_result(
                request_content=request_content,
                session_payload={
                    "status": "credential_unavailable",
                    "result": {
                        "status": "error",
                        "error": text,
                        "content": {"status": "credential_unavailable"},
                    },
                },
                correlation_id=correlation_id,
                conversation_id=conversation_id,
                session_key=session_key,
            )
        except Exception:
            self._logger.exception(
                "Credential collection start failure could not be published. "
                "event=gateway_credential_collection_start_failure_publish_failed "
                "correlation_id=%s session_key=%s credential_key=%s",
                correlation_id,
                session_key,
                request_content.credential_key,
            )
        return self._response(
            request_content=request_content,
            request_envelope=request_envelope,
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            text=text,
        )

    @staticmethod
    def _response(
        *,
        request_content: SecureCredentialCollectionRequest,
        request_envelope: MessageEnvelope,
        correlation_id: str,
        conversation_id: str | None,
        text: str,
        transcript_text: str | None = None,
        transient_action: GatewayTransientAction | None = None,
    ) -> GatewayResponse:
        return GatewayResponse(
            correlation_id=correlation_id,
            conversation_id=conversation_id,
            task_id=request_content.parent_task_id,
            message_type="secure_credential_collection",
            text=text,
            raw_envelope=request_envelope,
            transcript_text=text if transcript_text is None else transcript_text,
            transient_action=transient_action,
        )


__all__ = [
    "GatewayCredentialWorkflow",
    "GatewayCredentialWorkflowSettings",
    "REQUEST_KINDS_SECURE_CREDENTIAL_RESULTS",
    "REQUEST_KIND_SECURE_CREDENTIAL_COLLECTION_RESULT",
    "REQUEST_STATUS_WAITING_FOR_CREDENTIAL",
    "REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT",
]

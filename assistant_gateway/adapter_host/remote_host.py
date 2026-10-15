"""Python plugin-host composition over the authenticated adapter protocol."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from assistant_gateway.adapter_host.client_state import AdapterHostClientStateStore
from assistant_gateway.adapter_host.replied_messages import replied_message_to_wire
from assistant_gateway.adapter_host.runtime import (
    AdapterDeliveryResult,
    AdapterHostRuntime,
    HostedAdapterSpec,
)
from assistant_gateway.adapter_host.transport import AdapterHostClient
from assistant_gateway.application.gateway_application import (
    GatewayCommandResult,
    GatewayDeliveryEvent,
    GatewayQueryHandler,
    GatewaySession,
)
from assistant_gateway.application.access_reviews import (
    AccessReviewDecisionRequestV1,
    AccessReviewDecisionResultV1,
    AccessReviewOutcome,
)
from runtime_settings.interactive_apps import INTERACTIVE_APP_EXECUTION_ADAPTER_HOST
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.contracts.adapter_host_v1 import (
    ACCESS_REVIEW_DECISION_COMMAND,
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterCapabilityV1,
    ChannelCommandV1,
    ChannelInboundMessageV1,
    DeliveryOutcomeV1,
    GatewayDeliveryEventV1,
)
from assistant_gateway.attachments import StagedAttachmentReader
from assistant_gateway.inbound_attachments import (
    InboundAttachmentStoragePolicy,
)
from assistant_gateway.interactive_apps.builtin_registry import (
    build_interactive_app_registry,
)
from assistant_gateway.interactive_apps.base import BaseInteractiveApp
from assistant_gateway.interactive_apps.contracts import InteractiveAppBuildContext
from assistant_gateway.interactive_apps.host_ports import (
    InteractiveAppRuntimeContext,
    LocalInteractiveAppAttachmentStore,
)
from assistant_gateway.interactive_apps.identifiers import (
    BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID,
)
from assistant_gateway.interactive_apps.registry import InteractiveAppRegistry
from assistant_gateway.translation import ChannelInboundMessage, ChannelOutboundMessage
from assistant_gateway.voice import build_voice_client
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment
from id_generation import new_id


LOGGER = logging.getLogger(__name__)


def _idempotency_key(*parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"adapter:{digest}"


class RemoteChannelBridge:
    """Submit normalized channel input without exposing gateway internals."""

    def __init__(
        self,
        runtime_supplier: Callable[[], AdapterHostRuntime],
        *,
        plugin_id: str,
        adapter_instance_id: str,
    ) -> None:
        self._runtime_supplier = runtime_supplier
        self._plugin_id = plugin_id
        self._adapter_instance_id = adapter_instance_id

    async def handle_inbound(
        self, inbound: ChannelInboundMessage
    ) -> list[ChannelOutboundMessage]:
        runtime = self._runtime_supplier()
        attachments = []
        for attachment in inbound.attachments:
            local_path = attachment.local_path()
            if not local_path:
                raise ValueError(
                    "Hosted adapter attachments require a host-local validated file."
                )
            payload = Path(local_path).read_bytes()
            attachments.append(
                await runtime.upload_attachment(
                    plugin_id=self._plugin_id,
                    adapter_instance_id=self._adapter_instance_id,
                    filename=attachment.filename,
                    content_type=attachment.content_type,
                    kind=attachment.kind,
                    payload=payload,
                    attachment_id=attachment.attachment_id,
                )
            )
        request_id = new_id("adapter_in")
        idempotency = (
            _idempotency_key(
                self._plugin_id,
                inbound.channel_name,
                inbound.user_id,
                inbound.chat_id,
                inbound.thread_id or "",
                inbound.message_id,
            )
            if inbound.message_id
            else new_id("adapter_inidem")
        )
        await runtime.submit(
            ChannelInboundMessageV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="channel.inbound.v1",
                request_id=request_id,
                idempotency_key=idempotency,
                plugin_id=self._plugin_id,
                adapter_instance_id=self._adapter_instance_id,
                session_epoch="pending",
                stream_position=1,
                channel_name=inbound.channel_name,
                external_user_id=inbound.user_id,
                chat_id=inbound.chat_id,
                thread_id=inbound.thread_id,
                external_message_id=inbound.message_id,
                reply_to_external_message_id=inbound.reply_to_message_id,
                replied_message=replied_message_to_wire(inbound.replied_message),
                text=inbound.text,
                attachments=tuple(attachments),
                received_at_utc=datetime.now(timezone.utc),
            )
        )
        # The gateway owns outbound delivery through its durable outbox. Sending
        # a synchronous copy here would violate idempotency and acknowledgement.
        return []


class RemoteGatewayApplication:
    """Small Python facade over protocol operations used directly by adapters."""

    def __init__(
        self,
        runtime_supplier: Callable[[], AdapterHostRuntime],
        *,
        plugin_id: str,
        adapter_instance_id: str,
        channel_name: str,
        default_session_key: str,
    ) -> None:
        self._runtime_supplier = runtime_supplier
        self._plugin_id = plugin_id
        self._adapter_instance_id = adapter_instance_id
        self._channel_name = channel_name
        self._default_session_key = default_session_key

    @property
    def default_session_key(self) -> str:
        return self._default_session_key

    @staticmethod
    def _route_session_key(
        *, channel_name: str, user_id: str, chat_id: str, thread_id: str | None
    ) -> str:
        digest = hashlib.sha256(
            "\0".join((channel_name, user_id, chat_id, thread_id or "")).encode("utf-8")
        ).hexdigest()[:24]
        return f"adapter_route_{digest}"

    def resolve_session(
        self,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        canonical_user_id: str | None = None,
    ) -> GatewaySession:
        del canonical_user_id
        route_session = session_key or self._route_session_key(
            channel_name=channel_name or self._channel_name,
            user_id=user_id or "unknown",
            chat_id=chat_id or "unknown",
            thread_id=thread_id,
        )
        return GatewaySession(
            session_key=route_session,
            conversation_id=None,
            channel_name=channel_name or self._channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
        )

    def get_active_session(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> GatewaySession | None:
        del channel_name, user_id, chat_id, thread_id
        return None

    def is_registered_user(
        self,
        *,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
    ) -> bool:
        del channel_name, external_user_id, chat_scope
        # Canonical identity is deliberately unavailable outside the gateway.
        return False

    def register_inbound_attachment(
        self,
        attachment: UserTaskAttachment,
        *,
        session_key: str,
        conversation_id: str | None,
        correlation_id: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        del attachment, session_key, conversation_id, correlation_id, expires_at
        # RemoteChannelBridge uploads and registers opaque references at admission.
        return None

    async def execute_interactive_command(
        self,
        *,
        raw_text: str,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None = None,
        current_session_key: str | None = None,
        canonical_user_id: str | None = None,
        help_text: str | None = None,
        enable_debug_commands: bool = True,
        enable_reply_command: bool = True,
        enable_channel_binding_commands: bool = True,
        query_handler: GatewayQueryHandler | None = None,
        correlation_id: str | None = None,
    ) -> GatewayCommandResult:
        # Remote adapters send their own ids; the host assigns the command's.
        del canonical_user_id, query_handler, correlation_id
        runtime = self._runtime_supplier()
        await runtime.submit(
            ChannelCommandV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="channel.command.v1",
                request_id=new_id("adapter_cmd"),
                idempotency_key=new_id("adapter_cmdidem"),
                plugin_id=self._plugin_id,
                adapter_instance_id=self._adapter_instance_id,
                session_epoch="pending",
                stream_position=1,
                channel_name=channel_name,
                external_user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                command_name="interactive.raw",
                arguments={
                    "raw_text": raw_text,
                    "current_session_key": current_session_key or "",
                    "help_text": help_text or "",
                    "enable_debug_commands": enable_debug_commands,
                    "enable_reply_command": enable_reply_command,
                    "enable_channel_binding_commands": enable_channel_binding_commands,
                },
                received_at_utc=datetime.now(timezone.utc),
            )
        )
        return GatewayCommandResult(
            handled=True,
            session_key=current_session_key,
            text=None,
            deferred=True,
        )

    async def decide_access_review(
        self, request: AccessReviewDecisionRequestV1
    ) -> AccessReviewDecisionResultV1:
        """Send a review button press to the gateway; the outcome arrives as a chat message."""

        runtime = self._runtime_supplier()
        await runtime.submit(
            ChannelCommandV1(
                protocol_version=ADAPTER_HOST_PROTOCOL_VERSION,
                message_type="channel.command.v1",
                request_id=new_id("adapter_cmd"),
                idempotency_key=_idempotency_key(
                    self._plugin_id,
                    ACCESS_REVIEW_DECISION_COMMAND,
                    request.request_id,
                    request.choice,
                    request.channel_name,
                    request.user_id,
                    request.chat_id,
                    request.thread_id or "",
                ),
                plugin_id=self._plugin_id,
                adapter_instance_id=self._adapter_instance_id,
                session_epoch="pending",
                stream_position=1,
                channel_name=request.channel_name,
                external_user_id=request.user_id,
                chat_id=request.chat_id,
                thread_id=request.thread_id,
                command_name=ACCESS_REVIEW_DECISION_COMMAND,
                arguments={"request_id": request.request_id, "choice": request.choice},
                received_at_utc=datetime.now(timezone.utc),
            )
        )
        return AccessReviewDecisionResultV1(
            outcome=AccessReviewOutcome.SUBMITTED,
            text="Your decision was sent. The result appears in this chat.",
        )


@dataclass(frozen=True)
class BuiltAdapterHostRuntime:
    runtime: AdapterHostRuntime
    plugin_ids: tuple[str, ...]


def _resolve_instance_path(instance_root: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (instance_root / path).resolve()


def build_python_adapter_host_runtime(
    config: GatewayInstanceConfig,
    *,
    registry: InteractiveAppRegistry | None = None,
) -> BuiltAdapterHostRuntime:
    """Construct only explicitly externalized plugins with no gateway repository access."""

    registry = registry or build_interactive_app_registry(config)
    holder: dict[str, AdapterHostRuntime] = {}

    def runtime_supplier() -> AdapterHostRuntime:
        runtime = holder.get("runtime")
        if runtime is None:
            raise RuntimeError("Adapter host runtime is not fully composed yet.")
        return runtime

    instance_root = config.instance_root_path
    host_settings = config.interactive_apps.adapter_host
    client = AdapterHostClient(host_settings, instance_root=instance_root)
    client_state_path = _resolve_instance_path(
        instance_root, host_settings.client_state_path
    )
    state = AdapterHostClientStateStore(client_state_path)
    local_attachment_dir = Path(
        os.path.realpath(client_state_path.parent / "adapter-host-attachments")
    )
    # Channels read downloaded gateway files only through this reader: the
    # download is checked against its SHA-256 again when a channel sends it.
    outgoing_attachments = StagedAttachmentReader(
        roots=(local_attachment_dir,),
        private_copies_dir=local_attachment_dir / "private-copies",
    )
    attachment_settings = config.interactive_apps.internal_website.attachments
    specs: list[HostedAdapterSpec] = []
    for descriptor in registry.descriptors:
        configured = config.interactive_apps.require_plugin(descriptor.plugin_id)
        if (
            not configured.enabled
            or configured.execution != INTERACTIVE_APP_EXECUTION_ADAPTER_HOST
        ):
            continue
        if descriptor.plugin_id == BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID:
            raise RuntimeError(
                "The internal Website is gateway-owned and cannot use execution=adapter_host."
            )
        configuration = descriptor.validate_configuration(configured.settings)
        dependency_report = descriptor.dependency_probe(configuration)
        if not dependency_report.available:
            raise RuntimeError(
                dependency_report.failure_message(
                    plugin_id=descriptor.plugin_id,
                    channel_name=descriptor.channel_name,
                )
            )
        adapter_instance_id = config.interactive_apps.resolved_adapter_instance_id(
            descriptor.plugin_id, config.instance_name
        )
        application = RemoteGatewayApplication(
            runtime_supplier,
            plugin_id=descriptor.plugin_id,
            adapter_instance_id=adapter_instance_id,
            channel_name=descriptor.channel_name,
            default_session_key=config.user.default_session_key,
        )
        bridge = RemoteChannelBridge(
            runtime_supplier,
            plugin_id=descriptor.plugin_id,
            adapter_instance_id=adapter_instance_id,
        )
        runtime_context = InteractiveAppRuntimeContext(
            request_timeout_seconds=config.manager.request_timeout_seconds,
            voice=config.voice,
            voice_client=build_voice_client(config.voice),
            authorization_presentation_mode=config.authorization.presentation.mode,
            attachments=LocalInteractiveAppAttachmentStore(
                InboundAttachmentStoragePolicy(
                    cache_dir=local_attachment_dir,
                    max_file_bytes=attachment_settings.max_file_bytes,
                    allowed_kinds=frozenset(attachment_settings.allowed_kinds),
                ),
                application,
                register_with_application=False,
            ),
            outgoing_attachments=outgoing_attachments,
        )
        app = registry.create(
            descriptor,
            InteractiveAppBuildContext(
                configuration=configuration,
                application=application,
                bridge=bridge,
                runtime=runtime_context,
            ),
        )

        async def deliver(
            event: GatewayDeliveryEventV1,
            *,
            _app: BaseInteractiveApp = app,
            _plugin_id: str = descriptor.plugin_id,
            _adapter_instance_id: str = adapter_instance_id,
        ) -> AdapterDeliveryResult:
            attachments: list[UserNotificationAttachment] = []
            for attachment in event.attachments:
                payload = await runtime_supplier().download_attachment(
                    plugin_id=_plugin_id,
                    adapter_instance_id=_adapter_instance_id,
                    attachment=attachment,
                )
                target = local_attachment_dir / attachment.attachment_id
                descriptor_fd = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
                )
                with os.fdopen(descriptor_fd, "wb") as handle:
                    handle.write(payload)
                attachments.append(
                    UserNotificationAttachment(
                        path=str(target),
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                        kind=(
                            attachment.kind
                            if attachment.kind
                            in {"image", "document", "audio", "video"}
                            else "auto"
                        ),
                        size_bytes=attachment.size_bytes,
                        sha256=attachment.sha256,
                    )
                )
            await _app.deliver_event(
                GatewayDeliveryEvent(
                    text=event.text,
                    session_key=event.session_key,
                    channel_name=event.channel_name,
                    user_id=event.external_user_id,
                    chat_id=event.chat_id,
                    thread_id=event.thread_id,
                    conversation_id=event.conversation_id,
                    scheduled_job_id=None,
                    correlation_id=event.correlation_id,
                    original_message_id=event.in_reply_to_external_message_id,
                    message_type=event.content_message_type,
                    review_url=event.review_url,
                    access_request_id=event.access_request_id,
                    review_choices=tuple(event.review_choices),
                    message_level=str(event.message_level),
                    attachments=tuple(attachments),
                )
            )
            return AdapterDeliveryResult(outcome=DeliveryOutcomeV1.DELIVERED)

        specs.append(
            HostedAdapterSpec(
                plugin_id=descriptor.plugin_id,
                adapter_instance_id=adapter_instance_id,
                plugin_api_version=descriptor.plugin_api_version,
                implementation_version=descriptor.implementation_version,
                channel_names=(descriptor.channel_name,),
                capabilities=tuple(
                    AdapterCapabilityV1(capability.value)
                    for capability in sorted(
                        descriptor.capabilities, key=lambda item: item.value
                    )
                ),
                configuration=configuration,
                deliver_event=deliver,
                start=app.start,
                stop=app.stop,
            )
        )
    if not specs:
        raise RuntimeError("No enabled interactive app uses execution=adapter_host.")
    runtime = AdapterHostRuntime(client, state, host_settings, specs)
    holder["runtime"] = runtime
    return BuiltAdapterHostRuntime(
        runtime=runtime,
        plugin_ids=tuple(spec.plugin_id for spec in specs),
    )


__all__ = [
    "BuiltAdapterHostRuntime",
    "RemoteChannelBridge",
    "RemoteGatewayApplication",
    "build_python_adapter_host_runtime",
]

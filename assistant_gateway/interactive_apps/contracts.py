"""Stable contracts for registry-backed interactive app adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from assistant_gateway.application.gateway_application import (
        GatewayCommandResult,
        GatewayQueryHandler,
        GatewaySession,
    )
    from assistant_gateway.application.access_reviews import (
        AccessReviewDecisionRequestV1,
        AccessReviewDecisionResultV1,
    )
    from assistant_gateway.interactive_apps.base import BaseInteractiveApp
    from assistant_gateway.interactive_apps.host_ports import (
        InteractiveAppRuntimeContext,
    )
    from assistant_gateway.translation import (
        ChannelInboundMessage,
        ChannelOutboundMessage,
    )
    from custom_messages.contracts.attachments_v1 import UserTaskAttachment


INTERACTIVE_APP_PLUGIN_API_VERSION = "1"


class InteractiveAppApplication(Protocol):
    """Exact gateway use cases available to a channel adapter plugin."""

    @property
    def default_session_key(self) -> str: ...

    def get_active_session(
        self,
        *,
        channel_name: str,
        user_id: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> GatewaySession | None: ...

    def is_registered_user(
        self,
        *,
        channel_name: str,
        external_user_id: str,
        chat_scope: str = "",
    ) -> bool: ...

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
    ) -> GatewayCommandResult: ...

    def resolve_session(
        self,
        *,
        session_key: str | None = None,
        channel_name: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        canonical_user_id: str | None = None,
    ) -> GatewaySession: ...

    def register_inbound_attachment(
        self,
        attachment: UserTaskAttachment,
        *,
        session_key: str,
        conversation_id: str | None,
        correlation_id: str | None = None,
        expires_at: str | None = None,
    ) -> None: ...

    async def decide_access_review(
        self,
        request: AccessReviewDecisionRequestV1,
    ) -> AccessReviewDecisionResultV1: ...


class InteractiveAppBridge(Protocol):
    """Normalized inbound submission available to a channel adapter plugin."""

    async def handle_inbound(
        self, inbound: ChannelInboundMessage
    ) -> list[ChannelOutboundMessage]: ...


class InteractiveAppCapability(StrEnum):
    """Channel features declared by an adapter descriptor."""

    INBOUND_TEXT = "inbound_text"
    OUTBOUND_TEXT = "outbound_text"
    PROACTIVE_DELIVERY = "proactive_delivery"
    ATTACHMENTS = "attachments"
    IMAGES = "images"
    AUDIO_VOICE = "audio_voice"
    THREADS_TOPICS = "threads_topics"
    GROUP_CONVERSATIONS = "group_conversations"
    NATIVE_COMMANDS = "native_commands"
    RICH_TEXT = "rich_text"
    NATIVE_ACTIVITY_INDICATOR = "native_activity_indicator"
    MESSAGE_EDIT_DELETE = "message_edit_delete"
    DELIVERY_RECEIPTS = "delivery_receipts"
    WEBHOOK_LIFECYCLE = "webhook_lifecycle"
    POLLING_LIFECYCLE = "polling_lifecycle"


class InteractiveAppLifecycleState(StrEnum):
    """Manager-observed lifecycle state for one registered plugin."""

    DISABLED = "disabled"
    NOT_SELECTED = "not_selected"
    REGISTERED = "registered"
    HOSTED_EXTERNALLY = "hosted_externally"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


class InteractiveAppPluginOrigin(StrEnum):
    """Trusted source through which a descriptor entered the registry."""

    BUNDLED = "bundled"
    PYTHON_ENTRY_POINT = "python_entry_point"


class InteractiveAppDiscoveryStatus(StrEnum):
    """Outcome for one configured optional package during discovery."""

    REGISTERED = "registered"
    DISABLED = "disabled"
    MISSING = "missing"
    PROVENANCE_MISMATCH = "provenance_mismatch"
    LOAD_FAILED = "load_failed"
    INVALID_DESCRIPTOR = "invalid_descriptor"


@dataclass(frozen=True)
class InteractiveAppBuildContext:
    """Dependencies supplied to an adapter factory by the composition root."""

    configuration: object
    application: InteractiveAppApplication
    bridge: InteractiveAppBridge
    runtime: InteractiveAppRuntimeContext


@dataclass(frozen=True)
class InteractiveAppDependencyReport:
    """Locally verifiable dependency status for one plugin."""

    available: bool
    checked: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    deferred_checks: tuple[str, ...] = ()
    installation_hint: str | None = None
    detail: str = ""

    def failure_message(self, *, plugin_id: str, channel_name: str) -> str:
        detail = self.detail.strip()
        if not detail and self.missing:
            detail = f"Missing dependencies: {', '.join(self.missing)}."
        if not detail:
            detail = "The dependency probe reported that the plugin is unavailable."
        hint = f" {self.installation_hint.strip()}" if self.installation_hint else ""
        return (
            f"Interactive app plugin {plugin_id!r} for channel {channel_name!r} "
            f"cannot start. {detail}{hint}"
        )


class InteractiveAppFactory(Protocol):
    def __call__(self, context: InteractiveAppBuildContext) -> BaseInteractiveApp:
        """Construct an adapter without starting its external lifecycle."""


class InteractiveAppDependencyProbe(Protocol):
    def __call__(self, configuration: object) -> InteractiveAppDependencyReport:
        """Check local dependencies without starting or importing the adapter SDK."""


@dataclass(frozen=True)
class InteractiveAppOnboardingMetadata:
    """Presentation metadata shared by onboarding and operator inspection."""

    summary: str
    setup_order: int
    configuration_title: str = "Settings"
    installation_hint: str = ""

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("Interactive app onboarding summary must not be empty.")
        if self.setup_order < 0:
            raise ValueError(
                "Interactive app onboarding setup_order must be non-negative."
            )


@dataclass(frozen=True)
class InteractiveAppPackageProvenance:
    """Host-derived package identity; plugin code cannot self-assert this data."""

    origin: InteractiveAppPluginOrigin
    distribution_name: str | None = None
    distribution_version: str | None = None
    entry_point_group: str | None = None
    entry_point_name: str | None = None
    entry_point_value: str | None = None


@dataclass(frozen=True)
class InteractiveAppDiscoveryDiagnostic:
    """Immutable discovery result for one configured optional plugin."""

    plugin_id: str
    distribution_name: str
    enabled: bool
    status: InteractiveAppDiscoveryStatus
    detail: str


@dataclass(frozen=True)
class InteractiveAppDescriptor:
    """Data and factories needed to register one interactive app plugin."""

    plugin_id: str
    channel_name: str
    display_name: str
    plugin_api_version: str
    implementation_version: str
    capabilities: frozenset[InteractiveAppCapability]
    configuration_model: type[object]
    dependency_probe: InteractiveAppDependencyProbe
    factory: InteractiveAppFactory
    onboarding: InteractiveAppOnboardingMetadata
    secret_reference_names: tuple[str, ...] = ()
    documentation_reference: str = ""

    def __post_init__(self) -> None:
        required_strings = {
            "plugin_id": self.plugin_id,
            "channel_name": self.channel_name,
            "display_name": self.display_name,
            "plugin_api_version": self.plugin_api_version,
            "implementation_version": self.implementation_version,
        }
        for field_name, value in required_strings.items():
            if not str(value or "").strip():
                raise ValueError(
                    f"Interactive app descriptor {field_name} must not be empty."
                )
            if str(value) != str(value).strip():
                raise ValueError(
                    f"Interactive app descriptor {field_name} must not have surrounding whitespace."
                )
        if not self.capabilities:
            raise ValueError(
                f"Interactive app descriptor {self.plugin_id!r} must declare at least one capability."
            )
        if any(
            not isinstance(capability, InteractiveAppCapability)
            for capability in self.capabilities
        ):
            raise TypeError(
                f"Interactive app descriptor {self.plugin_id!r} contains an invalid capability."
            )
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(
            self,
            "secret_reference_names",
            tuple(self.secret_reference_names),
        )
        model_validate = getattr(self.configuration_model, "model_validate", None)
        model_json_schema = getattr(self.configuration_model, "model_json_schema", None)
        if not callable(model_validate) or not callable(model_json_schema):
            raise TypeError(
                f"Interactive app descriptor {self.plugin_id!r} configuration_model "
                "must provide model_validate() and model_json_schema()."
            )
        configuration_schema = model_json_schema()
        properties = configuration_schema.get("properties", {})
        enabled_schema = (
            properties.get("enabled") if isinstance(properties, dict) else None
        )
        if (
            not isinstance(enabled_schema, dict)
            or enabled_schema.get("type") != "boolean"
        ):
            raise TypeError(
                f"Interactive app descriptor {self.plugin_id!r} configuration_model "
                "must declare an enabled boolean field."
            )
        if not callable(self.dependency_probe):
            raise TypeError(
                f"Interactive app descriptor {self.plugin_id!r} has no dependency probe."
            )
        if not callable(self.factory):
            raise TypeError(
                f"Interactive app descriptor {self.plugin_id!r} has no factory."
            )

    def validate_configuration(self, value: object) -> object:
        """Validate one plugin's settings without exposing aggregate host config."""
        return self.configuration_model.model_validate(value)  # type: ignore[attr-defined]

    def configuration_schema(self) -> dict[str, object]:
        """Return JSON-compatible settings metadata for onboarding/inspection."""
        return self.configuration_model.model_json_schema()  # type: ignore[attr-defined,no-any-return]


@dataclass(frozen=True)
class InteractiveAppHealth:
    """Immutable manager health snapshot for one registered plugin."""

    plugin_id: str
    channel_name: str
    display_name: str
    state: InteractiveAppLifecycleState
    configured_enabled: bool | None
    selected: bool
    capabilities: frozenset[InteractiveAppCapability]
    provenance: InteractiveAppPackageProvenance
    dependency_report: InteractiveAppDependencyReport | None = None
    error: str | None = None

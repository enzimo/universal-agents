"""Version-one JSON contract between the gateway and an adapter host.

The models in this module deliberately contain no gateway persistence,
transport, authorization-provider, broker, or platform-SDK types. External
identities are untrusted routing inputs; canonical ownership is resolved only
inside the gateway application.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)


ADAPTER_HOST_PROTOCOL_VERSION = 1
# The channel command a hosted adapter sends when a person presses an access
# review button; arguments are exactly {"request_id", "choice"}.
ACCESS_REVIEW_DECISION_COMMAND = "access.review_decision"

NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
Sha256Hex = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Fa-f0-9]{64}$"),
]


class AdapterHostContractError(ValueError):
    """Stable protocol error suitable for an HTTP or WebSocket error body."""

    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail


class AdapterCapabilityV1(StrEnum):
    """Capabilities that may be declared across the process boundary."""

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


class ChannelRequestStatusV1(StrEnum):
    """Immediate gateway admission result for an adapter request."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class GatewayRequestStatusV1(StrEnum):
    """Asynchronous gateway request lifecycle visible to an adapter host."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class AdapterHealthStateV1(StrEnum):
    """Adapter-host lifecycle state reported to the gateway."""

    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"


class DeliveryAcknowledgementStageV1(StrEnum):
    """Durable delivery boundary acknowledged by an adapter host."""

    RECEIVED = "received"
    COMPLETED = "completed"


class DeliveryOutcomeV1(StrEnum):
    """Final external-platform outcome reported after a delivery attempt."""

    DELIVERED = "delivered"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class DeliveryMessageLevelV1(StrEnum):
    """Portable attention level for one user-visible delivery."""

    NORMAL = "normal"
    ATTENTION = "attention"


class _AdapterHostModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )

    protocol_version: Literal[1]


class _TimestampMixin:
    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class AdapterProtocolLimitsV1(BaseModel):
    """Negotiated flow-control and payload limits for one registration epoch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_in_flight_events: int = Field(ge=1, le=10_000)
    max_payload_bytes: int = Field(ge=1_024, le=256 * 1024 * 1024)
    max_attachment_bytes: int = Field(ge=0, le=256 * 1024 * 1024)
    max_pending_events: int = Field(ge=1, le=1_000_000)
    heartbeat_interval_seconds: float = Field(ge=1.0, le=3_600.0)
    acknowledgement_timeout_seconds: float = Field(ge=1.0, le=86_400.0)
    retention_seconds: int = Field(ge=60, le=31_536_000)


class AdapterResumePositionV1(BaseModel):
    """Last durable positions known to the reconnecting adapter host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    inbound_committed_position: int = Field(default=0, ge=0)
    outbound_received_position: int = Field(default=0, ge=0)
    outbound_completed_position: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_outbound_positions(self) -> AdapterResumePositionV1:
        if self.outbound_completed_position > self.outbound_received_position:
            raise ValueError(
                "outbound_completed_position cannot exceed outbound_received_position"
            )
        return self


class AdapterAttachmentV1(BaseModel):
    """Attachment metadata with an opaque, non-filesystem storage reference."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    attachment_id: Identifier
    filename: NonEmptyString
    content_type: NonEmptyString
    kind: NonEmptyString
    size_bytes: Annotated[int, Field(ge=0, le=100 * 1024 * 1024)]
    sha256: Sha256Hex
    storage_reference: NonEmptyString

    @field_validator("storage_reference")
    @classmethod
    def validate_storage_reference(cls, value: str) -> str:
        scheme, separator, opaque_id = value.partition(":")
        if (
            not separator
            or scheme not in {"adapter-upload", "gateway-object"}
            or not opaque_id.strip()
            or any(character.isspace() for character in opaque_id)
        ):
            raise ValueError(
                "attachment storage_reference must use an opaque "
                "adapter-upload: or gateway-object: reference"
            )
        return value


class AdapterRepliedMessageV1(BaseModel):
    """The chat message an inbound message replies to, as the platform reported it.

    Mirrors ``custom_messages.contracts.replied_message_v1.RepliedMessage``
    (its docstring explains why agents need it and how they show it); the
    limits match, so the gateway rebuilds that model without cutting again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    author: Literal["assistant", "requester", "other"]
    author_name: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    text: Annotated[str, Field(min_length=1, max_length=16_000)]
    shortened: bool = False
    quoted_text: Annotated[str, Field(max_length=16_000)] | None = None
    sent_at_utc: AwareDatetime | None = None


class ChannelInboundMessageV1(_AdapterHostModel, _TimestampMixin):
    """Untrusted external text normalized by one adapter instance."""

    message_type: Literal["channel.inbound.v1"]
    request_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    session_epoch: Identifier
    stream_position: Annotated[int, Field(ge=1)]
    channel_name: Identifier
    external_user_id: NonEmptyString
    chat_id: NonEmptyString
    thread_id: NonEmptyString | None = None
    external_message_id: NonEmptyString | None = None
    reply_to_external_message_id: NonEmptyString | None = None
    replied_message: AdapterRepliedMessageV1 | None = None
    text: Annotated[str, Field(max_length=200_000)]
    attachments: tuple[AdapterAttachmentV1, ...] = ()
    received_at_utc: AwareDatetime

    @field_validator("received_at_utc")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)

    @model_validator(mode="after")
    def require_content(self) -> ChannelInboundMessageV1:
        if not self.text.strip() and not self.attachments:
            raise ValueError("an inbound message requires text or attachments")
        return self


class ChannelCommandV1(_AdapterHostModel, _TimestampMixin):
    """Platform-native input normalized to a gateway-owned command name."""

    message_type: Literal["channel.command.v1"]
    request_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    session_epoch: Identifier
    stream_position: Annotated[int, Field(ge=1)]
    channel_name: Identifier
    external_user_id: NonEmptyString
    chat_id: NonEmptyString
    thread_id: NonEmptyString | None = None
    external_message_id: NonEmptyString | None = None
    command_name: Identifier
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    received_at_utc: AwareDatetime

    @field_validator("received_at_utc")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)


class ChannelRequestAcceptedV1(_AdapterHostModel, _TimestampMixin):
    """Immediate admission result; terminal work arrives as later events."""

    message_type: Literal["channel.request_accepted.v1"]
    request_id: Identifier
    idempotency_key: Identifier
    status: ChannelRequestStatusV1
    correlation_id: Identifier | None = None
    accepted_at_utc: AwareDatetime
    error_code: Identifier | None = None
    detail: Annotated[str, Field(max_length=2_000)] | None = None

    @field_validator("accepted_at_utc")
    @classmethod
    def normalize_accepted_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)

    @model_validator(mode="after")
    def validate_result_shape(self) -> ChannelRequestAcceptedV1:
        if self.status == ChannelRequestStatusV1.REJECTED:
            if not self.error_code:
                raise ValueError("a rejected request requires error_code")
        elif not self.correlation_id:
            raise ValueError("an accepted or duplicate request requires correlation_id")
        return self


class GatewayDeliveryEventV1(_AdapterHostModel, _TimestampMixin):
    """User-visible delivery routed from the gateway to one channel plugin."""

    message_type: Literal["gateway.delivery.v1"]
    event_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    session_epoch: Identifier
    stream_position: Annotated[int, Field(ge=1)]
    channel_name: Identifier
    external_user_id: NonEmptyString | None = None
    chat_id: NonEmptyString
    thread_id: NonEmptyString | None = None
    in_reply_to_external_message_id: NonEmptyString | None = None
    session_key: Identifier
    conversation_id: Identifier | None = None
    correlation_id: Identifier | None = None
    content_message_type: NonEmptyString | None = None
    review_url: NonEmptyString | None = None
    # An access review announcement: the waiting request and the choices a
    # channel may show as buttons. A press is sent back as the command
    # "access.review_decision" with {"request_id", "choice"}.
    access_request_id: NonEmptyString | None = None
    review_choices: tuple[Identifier, ...] = ()
    text: Annotated[str, Field(max_length=200_000)]
    message_level: DeliveryMessageLevelV1 = DeliveryMessageLevelV1.NORMAL
    attachments: tuple[AdapterAttachmentV1, ...] = ()
    emitted_at_utc: AwareDatetime

    @field_validator("emitted_at_utc")
    @classmethod
    def normalize_emitted_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)


class GatewayRequestStateV1(_AdapterHostModel, _TimestampMixin):
    """Asynchronous request-state event with no canonical ownership data."""

    message_type: Literal["gateway.request_state.v1"]
    event_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    session_epoch: Identifier
    stream_position: Annotated[int, Field(ge=1)]
    channel_name: Identifier
    correlation_id: Identifier
    session_key: Identifier
    conversation_id: Identifier | None = None
    status: GatewayRequestStatusV1
    reason_code: Identifier | None = None
    detail: Annotated[str, Field(max_length=2_000)] | None = None
    updated_at_utc: AwareDatetime

    @field_validator("updated_at_utc")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)


class AdapterRegistrationV1(_AdapterHostModel, _TimestampMixin):
    """One adapter instance declaring routing and rendering capabilities."""

    message_type: Literal["adapter.registration.v1"]
    registration_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    plugin_api_version: Literal["1"]
    implementation_version: NonEmptyString
    configuration_digest: Sha256Hex
    channel_names: tuple[Identifier, ...]
    capabilities: tuple[AdapterCapabilityV1, ...]
    supported_limits: AdapterProtocolLimitsV1
    resume: AdapterResumePositionV1 = Field(default_factory=AdapterResumePositionV1)
    registered_at_utc: AwareDatetime

    @field_validator("registered_at_utc")
    @classmethod
    def normalize_registered_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)

    @field_validator("channel_names", "capabilities")
    @classmethod
    def require_unique_nonempty_values(
        cls, value: tuple[object, ...]
    ) -> tuple[object, ...]:
        if not value:
            raise ValueError("registration lists must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("registration lists must not contain duplicates")
        return value


class AdapterRegistrationAcceptedV1(_AdapterHostModel, _TimestampMixin):
    """Gateway acknowledgement of one authenticated adapter registration."""

    message_type: Literal["adapter.registration_accepted.v1"]
    registration_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    gateway_instance_id: Identifier
    accepted: bool
    session_epoch: Identifier | None = None
    lease_expires_at_utc: AwareDatetime | None = None
    negotiated_capabilities: tuple[AdapterCapabilityV1, ...] = ()
    negotiated_limits: AdapterProtocolLimitsV1 | None = None
    resume: AdapterResumePositionV1 = Field(default_factory=AdapterResumePositionV1)
    accepted_at_utc: AwareDatetime
    error_code: Identifier | None = None
    detail: Annotated[str, Field(max_length=2_000)] | None = None

    @field_validator("accepted_at_utc")
    @classmethod
    def normalize_accepted_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)

    @field_validator("lease_expires_at_utc")
    @classmethod
    def normalize_lease_expiry(cls, value: datetime | None) -> datetime | None:
        return cls._as_utc(value) if value is not None else None

    @model_validator(mode="after")
    def require_denial_error(self) -> AdapterRegistrationAcceptedV1:
        if not self.accepted and not self.error_code:
            raise ValueError("a rejected registration requires error_code")
        if self.accepted and (
            not self.session_epoch
            or self.lease_expires_at_utc is None
            or self.negotiated_limits is None
            or not self.negotiated_capabilities
        ):
            raise ValueError(
                "an accepted registration requires an epoch, lease, capabilities, and limits"
            )
        return self


class AdapterHealthV1(_AdapterHostModel, _TimestampMixin):
    """Redacted health reported by one authenticated adapter instance."""

    message_type: Literal["adapter.health.v1"]
    health_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    session_epoch: Identifier
    state: AdapterHealthStateV1
    inbound_committed_position: Annotated[int, Field(ge=0)] = 0
    outbound_received_position: Annotated[int, Field(ge=0)] = 0
    outbound_completed_position: Annotated[int, Field(ge=0)] = 0
    in_flight_events: Annotated[int, Field(ge=0)] = 0
    local_queue_depth: Annotated[int, Field(ge=0)] = 0
    observed_at_utc: AwareDatetime
    error_code: Identifier | None = None
    detail: Annotated[str, Field(max_length=2_000)] | None = None

    @field_validator("observed_at_utc")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)

    @model_validator(mode="after")
    def validate_positions(self) -> AdapterHealthV1:
        if self.outbound_completed_position > self.outbound_received_position:
            raise ValueError(
                "outbound_completed_position cannot exceed outbound_received_position"
            )
        return self


class AdapterHeartbeatV1(_AdapterHostModel, _TimestampMixin):
    """Lease renewal and durable cursor report for one adapter instance."""

    message_type: Literal["adapter.heartbeat.v1"]
    heartbeat_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    session_epoch: Identifier
    state: AdapterHealthStateV1
    inbound_committed_position: Annotated[int, Field(ge=0)] = 0
    outbound_received_position: Annotated[int, Field(ge=0)] = 0
    outbound_completed_position: Annotated[int, Field(ge=0)] = 0
    in_flight_events: Annotated[int, Field(ge=0)] = 0
    local_queue_depth: Annotated[int, Field(ge=0)] = 0
    observed_at_utc: AwareDatetime
    error_code: Identifier | None = None
    detail: Annotated[str, Field(max_length=2_000)] | None = None

    @field_validator("observed_at_utc")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)

    @model_validator(mode="after")
    def validate_positions(self) -> AdapterHeartbeatV1:
        if self.outbound_completed_position > self.outbound_received_position:
            raise ValueError(
                "outbound_completed_position cannot exceed outbound_received_position"
            )
        return self


class AdapterDeliveryAcknowledgementV1(_AdapterHostModel, _TimestampMixin):
    """Receipt or external-platform completion for one ordered delivery."""

    message_type: Literal["adapter.delivery_ack.v1"]
    acknowledgement_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    session_epoch: Identifier
    event_id: Identifier
    stream_position: Annotated[int, Field(ge=1)]
    stage: DeliveryAcknowledgementStageV1
    outcome: DeliveryOutcomeV1 | None = None
    provider_message_id: NonEmptyString | None = None
    observed_at_utc: AwareDatetime
    error_code: Identifier | None = None
    detail: Annotated[str, Field(max_length=2_000)] | None = None

    @field_validator("observed_at_utc")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)

    @model_validator(mode="after")
    def validate_stage(self) -> AdapterDeliveryAcknowledgementV1:
        if self.stage == DeliveryAcknowledgementStageV1.RECEIVED and self.outcome:
            raise ValueError("a receipt acknowledgement must not include an outcome")
        if self.stage == DeliveryAcknowledgementStageV1.COMPLETED and not self.outcome:
            raise ValueError("a completion acknowledgement requires an outcome")
        if self.outcome == DeliveryOutcomeV1.FAILED and not self.error_code:
            raise ValueError("a failed delivery outcome requires error_code")
        return self


class AdapterAttachmentUploadRequestV1(_AdapterHostModel, _TimestampMixin):
    """Request a bounded one-use upload target for inbound attachment bytes."""

    message_type: Literal["adapter.attachment_upload_request.v1"]
    upload_request_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    session_epoch: Identifier
    filename: NonEmptyString
    content_type: NonEmptyString
    kind: NonEmptyString
    size_bytes: Annotated[int, Field(ge=0, le=256 * 1024 * 1024)]
    sha256: Sha256Hex
    requested_at_utc: AwareDatetime

    @field_validator("requested_at_utc")
    @classmethod
    def normalize_requested_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)


class AdapterAttachmentUploadAcceptedV1(_AdapterHostModel, _TimestampMixin):
    """Opaque upload ticket; the path is relative to the authenticated API."""

    message_type: Literal["adapter.attachment_upload_accepted.v1"]
    upload_request_id: Identifier
    idempotency_key: Identifier
    plugin_id: Identifier
    adapter_instance_id: Identifier
    session_epoch: Identifier
    upload_id: Identifier
    upload_path: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=1_024,
            pattern=r"^/v1/adapter-host/uploads/[A-Za-z0-9._:/-]+$",
        ),
    ]
    storage_reference: NonEmptyString
    expires_at_utc: AwareDatetime
    max_bytes: Annotated[int, Field(ge=0, le=256 * 1024 * 1024)]

    @field_validator("expires_at_utc")
    @classmethod
    def normalize_expires_at(cls, value: datetime) -> datetime:
        return cls._as_utc(value)


class AdapterProtocolErrorV1(_AdapterHostModel):
    """Stable fail-closed response for HTTP and WebSocket transports."""

    message_type: Literal["adapter.error.v1"]
    error_id: Identifier
    error_code: Identifier
    detail: Annotated[str, Field(min_length=1, max_length=4_000)]
    retryable: bool = False
    request_id: Identifier | None = None
    expected_stream_position: Annotated[int, Field(ge=1)] | None = None


AdapterHostMessageV1: TypeAlias = (
    ChannelInboundMessageV1
    | ChannelCommandV1
    | ChannelRequestAcceptedV1
    | GatewayDeliveryEventV1
    | GatewayRequestStateV1
    | AdapterRegistrationV1
    | AdapterRegistrationAcceptedV1
    | AdapterHealthV1
    | AdapterHeartbeatV1
    | AdapterDeliveryAcknowledgementV1
    | AdapterAttachmentUploadRequestV1
    | AdapterAttachmentUploadAcceptedV1
    | AdapterProtocolErrorV1
)

_MESSAGE_MODELS: dict[str, type[_AdapterHostModel]] = {
    "channel.inbound.v1": ChannelInboundMessageV1,
    "channel.command.v1": ChannelCommandV1,
    "channel.request_accepted.v1": ChannelRequestAcceptedV1,
    "gateway.delivery.v1": GatewayDeliveryEventV1,
    "gateway.request_state.v1": GatewayRequestStateV1,
    "adapter.registration.v1": AdapterRegistrationV1,
    "adapter.registration_accepted.v1": AdapterRegistrationAcceptedV1,
    "adapter.health.v1": AdapterHealthV1,
    "adapter.heartbeat.v1": AdapterHeartbeatV1,
    "adapter.delivery_ack.v1": AdapterDeliveryAcknowledgementV1,
    "adapter.attachment_upload_request.v1": AdapterAttachmentUploadRequestV1,
    "adapter.attachment_upload_accepted.v1": AdapterAttachmentUploadAcceptedV1,
    "adapter.error.v1": AdapterProtocolErrorV1,
}
_FORBIDDEN_TRUST_FIELDS = frozenset(
    {
        "canonical_user_id",
        "owner_canonical_user_id",
        "roles",
        "nats_credentials",
        "broker_credentials",
    }
)


def _find_forbidden_trust_field(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = str(key)
            if normalized_key in _FORBIDDEN_TRUST_FIELDS:
                return normalized_key
            found = _find_forbidden_trust_field(nested)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _find_forbidden_trust_field(nested)
            if found:
                return found
    return None


def decode_adapter_host_message_v1(
    payload: Mapping[str, JsonValue],
) -> AdapterHostMessageV1:
    """Validate one protocol payload and return a typed version-one message."""

    forbidden_field = _find_forbidden_trust_field(payload)
    if forbidden_field:
        raise AdapterHostContractError(
            "trusted_identity_forbidden",
            f"adapter-host payloads must not contain {forbidden_field!r}",
        )
    message_type = str(payload.get("message_type") or "").strip()
    model = _MESSAGE_MODELS.get(message_type)
    if model is None:
        raise AdapterHostContractError(
            "unsupported_message_type",
            f"unsupported adapter-host message_type {message_type!r}",
        )
    try:
        validated = model.model_validate(payload)
        if not isinstance(
            validated,
            (
                ChannelInboundMessageV1,
                ChannelCommandV1,
                ChannelRequestAcceptedV1,
                GatewayDeliveryEventV1,
                GatewayRequestStateV1,
                AdapterRegistrationV1,
                AdapterRegistrationAcceptedV1,
                AdapterHealthV1,
                AdapterHeartbeatV1,
                AdapterDeliveryAcknowledgementV1,
                AdapterAttachmentUploadRequestV1,
                AdapterAttachmentUploadAcceptedV1,
                AdapterProtocolErrorV1,
            ),
        ):  # pragma: no cover - model registry invariant
            raise TypeError("adapter-host model registry returned an invalid type")
        return validated
    except ValidationError as exc:
        storage_reference_error = any(
            error.get("loc", ())[-1:] == ("storage_reference",)
            for error in exc.errors()
        )
        error_code = (
            "invalid_attachment_reference"
            if storage_reference_error
            else "invalid_payload"
        )
        raise AdapterHostContractError(error_code, str(exc)) from exc


def encode_adapter_host_message_v1(
    message: AdapterHostMessageV1,
) -> dict[str, JsonValue]:
    """Return the canonical JSON-compatible representation of a message."""

    payload: dict[str, JsonValue] = message.model_dump(mode="json", exclude_none=True)
    return payload

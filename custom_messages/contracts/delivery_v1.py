"""delivery v1 contracts, independent of runtime hosts and SDKs."""

from datetime import datetime
from datetime import timezone
from id_generation import new_id
from pydantic import BaseModel
from pydantic import Field
from typing import List
from typing import Literal
from typing import Optional


class DeliveryReceipt(BaseModel):
    """
    Structured acknowledgement that the gateway actually delivered a message to the user.

    This is separate from task completion: the manager may generate a response at one
    time, but only the gateway knows whether it was shown immediately, deferred, or
    later delivered in a digest.
    """

    receipt_id: str = Field(
        default_factory=lambda: new_id("deliv_rcpt"),
        description="Unique identifier for this delivery receipt.",
    )
    message_class: Literal["DeliveryReceipt"] = Field(
        default="DeliveryReceipt", description="Delivery Receipt"
    )
    task_id: Optional[str] = Field(
        default=None,
        description="Top-level task id whose user-visible response was delivered.",
    )
    conversation_id: Optional[str] = Field(
        default=None, description="Conversation thread containing the delivered turn."
    )
    correlation_id: Optional[str] = Field(
        default=None,
        description="Request-local correlation id associated with the delivered turn.",
    )
    scheduled_job_id: Optional[str] = Field(
        default=None,
        description="Scheduled job identifier when the delivered turn originated from scheduled work.",
    )
    session_key: Optional[str] = Field(
        default=None,
        description="Gateway session key to which the content was delivered.",
    )
    delivery_state: str = Field(
        default="delivered",
        description="Delivery state observed by the gateway, for example delivered or deferred.",
    )
    delivery_via: Optional[str] = Field(
        default=None,
        description="How the gateway delivered the content, for example immediate or digest.",
    )
    delivered_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="ISO 8601 UTC timestamp when user-visible delivery occurred.",
    )

    def get_id(self) -> str:
        return self.receipt_id


class UserNotificationAttachment(BaseModel):
    """Local file attachment reference for gateway-delivered user notifications."""

    path: str = Field(
        description="Absolute local file path readable by the gateway process."
    )
    filename: Optional[str] = Field(
        default=None,
        description="Optional user-visible filename. Defaults to the basename of path.",
    )
    content_type: Optional[str] = Field(
        default=None, description="Optional MIME content type, for example image/png."
    )
    caption: Optional[str] = Field(
        default=None, description="Optional per-attachment caption."
    )
    kind: Literal["auto", "image", "document", "audio", "video"] = Field(
        default="auto",
        description="Attachment kind. Use auto unless the content type is ambiguous.",
    )
    size_bytes: Optional[int] = Field(
        default=None, description="Resolved file size in bytes."
    )
    sha256: Optional[str] = Field(
        default=None,
        description=(
            "SHA-256 of the gateway's staged copy, set by the gateway when it copies the file. "
            "Every reader refuses a copy whose bytes no longer match; a value sent by an agent is ignored."
        ),
    )


class UserNotification(BaseModel):
    """
    User-visible notification emitted by an agent through the gateway.

    This is intentionally separate from Task responses: tasks can originate from
    GitHub, schedules, webhooks, or other automations while user communication
    still follows the gateway-owned preferred contact profile.
    """

    notification_id: str = Field(
        default_factory=lambda: new_id("notification"),
        description="Unique identifier for this notification.",
    )
    message_class: Literal["UserNotification"] = Field(
        default="UserNotification", description="User Notification"
    )
    text: str = Field(description="User-visible notification text.")
    urgency: Literal["normal", "urgent"] = Field(
        default="normal",
        description="Delivery urgency hint. Urgent messages can bypass quiet hours when policy allows it.",
    )
    attachments: List[UserNotificationAttachment] = Field(
        default_factory=list,
        description="Local files the gateway should attach when delivering this notification. The message stores file paths, not binary bytes.",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Short agent-facing reason for sending the notification.",
    )
    source_task_id: Optional[str] = Field(
        default=None,
        description="Task id active when this notification was emitted, if any.",
    )
    source_agent_id: Optional[str] = Field(
        default=None, description="Agent id that emitted this notification."
    )
    source_agent_name: Optional[str] = Field(
        default=None, description="Agent name that emitted this notification."
    )

    def get_id(self) -> str:
        return self.notification_id

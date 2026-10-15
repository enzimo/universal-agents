"""Outer adapter that checks and copies attachments before delivery.

The copies go to the person's ``scratch/sent/`` folder with their SHA-256;
channels and the web transcript read only those copies, through
``assistant_gateway.attachments.StagedAttachmentReader``.
"""

from __future__ import annotations

import logging

from assistant_gateway.application.delivery_presentation import (
    GatewayDeliveryAttachmentResolver,
)
from assistant_gateway.attachments import (
    stage_user_notification_attachments,
    validate_user_notification_attachments,
)
from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.delivery_v1 import UserNotification, UserNotificationAttachment


class ConfigGatewayDeliveryAttachmentResolver(GatewayDeliveryAttachmentResolver):
    """Check notification attachments for the person, then copy them for delivery."""

    def __init__(
        self,
        *,
        config: GatewayInstanceConfig,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)

    def resolve(
        self,
        content: object,
        *,
        person_id: str | None,
    ) -> tuple[UserNotificationAttachment, ...]:
        if not isinstance(content, UserNotification) or not content.attachments:
            return ()
        attachments, errors = validate_user_notification_attachments(
            content.attachments,
            config=self._config,
            person_id=person_id,
        )
        staged, copy_errors = stage_user_notification_attachments(
            attachments,
            config=self._config,
            person_id=person_id,
        )
        for error in (*errors, *copy_errors):
            self._logger.warning(
                "Skipping invalid user-notification attachment. event=outgoing_attachment_skipped "
                "person_id=%s detail=%s",
                person_id,
                error,
            )
        return tuple(staged)

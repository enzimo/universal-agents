"""Stage notify_user attachments the way the gateway does, for channel tests.

Channels send only a copy the gateway staged in the person's scratch/sent/
folder with its SHA-256 (``assistant_gateway.attachments``); tests build their
attachments through the same two steps.
"""

from __future__ import annotations

from pathlib import Path

from assistant_gateway.attachments import (
    stage_user_notification_attachments,
    validate_user_notification_attachments,
)
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment


def staged_attachment(
    config,
    *,
    data: bytes,
    filename: str,
    content_type: str,
    kind: str = "auto",
    caption: str | None = None,
    person_id: str | None = None,
) -> UserNotificationAttachment:
    """Write ``data`` into the person's folder, then check and stage it for delivery."""

    person = person_id or config.user.canonical_user_id
    source = config.layout.person_dir(person) / "outputs" / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(data)
    checked, errors = validate_user_notification_attachments(
        [
            {
                "path": str(source),
                "filename": filename,
                "content_type": content_type,
                "kind": kind,
                "caption": caption,
            }
        ],
        config=config,
        person_id=person,
    )
    assert errors == [], errors
    staged, errors = stage_user_notification_attachments(checked, config=config, person_id=person)
    assert errors == [], errors
    return staged[0]


def change_staged_copy(attachment: UserNotificationAttachment, data: bytes) -> Path:
    """Change a staged copy the way agent code in the person's folder could."""

    copy = Path(attachment.path)
    copy.write_bytes(data)
    return copy

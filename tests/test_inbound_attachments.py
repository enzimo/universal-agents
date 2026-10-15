from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.inbound_attachments import (
    AttachmentValidationError,
    sanitize_attachment_filename,
    store_web_chat_file_field,
)


def _config(tmp_path: Path, *, max_file_bytes: int = 1024) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="attachment-test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    )
    config.interactive_apps.internal_website.attachments.max_file_bytes = max_file_bytes
    return config


def _file_field(filename: str, content_type: str, data: bytes) -> SimpleNamespace:
    return SimpleNamespace(filename=filename, content_type=content_type, file=BytesIO(data))


@pytest.mark.parametrize(
    ("filename", "content_type", "data", "message"),
    [
        ("empty.txt", "text/plain", b"", "empty"),
        ("too-large.txt", "text/plain", b"x" * 20, "exceeds"),
        ("run.sh", "text/x-shellscript", b"echo nope", "blocked executable"),
        ("../../secret.txt", "text/plain", b"safe text", "not an allowed attachment filename"),
        (".env", "text/plain", b"SECRET=1", "sensitive filename"),
        # Credential names are the list every file check shares
        # (access_control.sensitive_paths).
        (".env.local", "text/plain", b"SECRET=1", "sensitive filename"),
        ("team.env", "text/plain", b"SECRET=1", "sensitive filename"),
        (".envrc", "text/plain", b"SECRET=1", "sensitive filename"),
        ("id_ed25519", "text/plain", b"KEY", "sensitive filename"),
        ("server.pem", "application/x-pem-file", b"KEY", "sensitive filename"),
        ("payload.bin", "application/octet-stream", b"bytes", "unsupported"),
    ],
)
def test_store_web_chat_file_field_rejects_invalid_uploads(
    tmp_path: Path,
    filename: str,
    content_type: str,
    data: bytes,
    message: str,
) -> None:
    with pytest.raises(AttachmentValidationError, match=message):
        store_web_chat_file_field(
            file_field=_file_field(filename, content_type, data),
            config=_config(tmp_path, max_file_bytes=10),
            session_key="website:web-chat:browser_1",
            conversation_id=None,
        )


def test_sanitize_attachment_filename_blocks_hidden_and_weird_names() -> None:
    assert sanitize_attachment_filename("../../.env") == "attachment.env"
    assert sanitize_attachment_filename("my<>file?.txt") == "my_file_.txt"

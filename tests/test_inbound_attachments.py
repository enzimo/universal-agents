from __future__ import annotations

from pathlib import Path

import pytest

from assistant_gateway.inbound_attachments import (
    AttachmentValidationError,
    InboundAttachmentStoragePolicy,
    check_inbound_attachment_bytes,
    sanitize_attachment_filename,
    store_inbound_attachment_bytes_with_policy,
)


def _policy(tmp_path: Path, *, max_file_bytes: int = 10) -> InboundAttachmentStoragePolicy:
    return InboundAttachmentStoragePolicy(
        cache_dir=tmp_path / "uploads", max_file_bytes=max_file_bytes
    )


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
def test_refused_uploads_are_refused_by_the_check_and_never_saved(
    tmp_path: Path,
    filename: str,
    content_type: str,
    data: bytes,
    message: str,
) -> None:
    policy = _policy(tmp_path)

    with pytest.raises(AttachmentValidationError, match=message):
        check_inbound_attachment_bytes(
            data=data, filename=filename, content_type=content_type, policy=policy
        )
    with pytest.raises(AttachmentValidationError, match=message):
        store_inbound_attachment_bytes_with_policy(
            data=data,
            filename=filename,
            content_type=content_type,
            policy=policy,
            session_key="website:web-chat:browser_1",
            conversation_id=None,
        )
    # Saving checks first: a refused upload never creates a file or a folder.
    assert not policy.cache_dir.exists()


def test_an_upload_is_saved_under_the_name_type_and_kind_its_check_gives(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path, max_file_bytes=1024)

    checked = check_inbound_attachment_bytes(
        data=b"hello", filename="my<>notes?.txt", policy=policy
    )
    assert not policy.cache_dir.exists()
    stored = store_inbound_attachment_bytes_with_policy(
        data=b"hello",
        filename="my<>notes?.txt",
        policy=policy,
        session_key="website:web-chat:browser_1",
        conversation_id=None,
    )

    attachment = stored.attachment
    assert (attachment.filename, attachment.content_type, attachment.kind) == (
        checked.filename,
        checked.content_type,
        checked.kind,
    )
    assert checked.filename == "my_notes_.txt"
    assert (checked.content_type, checked.kind) == ("text/plain", "document")
    assert stored.local_path.read_bytes() == b"hello"


def test_sanitize_attachment_filename_blocks_hidden_and_weird_names() -> None:
    assert sanitize_attachment_filename("../../.env") == "attachment.env"
    assert sanitize_attachment_filename("my<>file?.txt") == "my_file_.txt"

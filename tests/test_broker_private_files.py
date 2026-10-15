"""Interrupted credential files never become completed import identities."""

from __future__ import annotations

import base64
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from access_control import safe_open
from secure_tool_broker import credential_transfer, private_files
from secure_tool_broker.credential_state import write_private_broker_file


def _record(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "credentials"
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(root))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    overrides = tmp_path / "credential_template_overrides.yaml"
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH", str(overrides))
    owner = credential_transfer.CredentialOwnerV1(kind="person", owner_id="usr_review")
    folder = credential_transfer._owner_folder(owner)
    if kind == "owner_file":
        content = b"complete harmless session"
        return dict(kind=kind, owner=owner.model_dump(), path="bitwarden/session",
                    content_b64=base64.b64encode(content).decode()), folder / "bitwarden/session", content
    if kind == "stored_value":
        content = "complete harmless sign-in"
        return dict(kind=kind, owner=owner.model_dump(), name="github.sign_in", value=content), folder / "gh/token", (content + "\n").encode()
    content = "schema_version: 1\ntemplates: {}\n"
    return dict(kind=kind, owner=dict(kind="team", owner_id="team_review"), content=content), overrides, content.encode()


def _interrupt_first_write(monkeypatch: pytest.MonkeyPatch) -> None:
    original = os.fdopen
    interrupted = False

    @contextmanager
    def fdopen(descriptor, *args, **kwargs):
        nonlocal interrupted
        with original(descriptor, *args, **kwargs) as handle:
            if interrupted:
                yield handle
                return
            interrupted = True

            class PartialWriter:
                def write(self, content):
                    handle.write(content[:3])
                    handle.flush()
                    raise OSError("injected interrupted credential write")

            yield PartialWriter()

    monkeypatch.setattr(private_files.os, "fdopen", fdopen)


@pytest.mark.parametrize("kind", ["owner_file", "template_overrides", "stored_value"])
def test_interrupted_import_has_no_destination_and_retries_the_complete_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    raw, target, expected = _record(kind, tmp_path, monkeypatch)
    _interrupt_first_write(monkeypatch)

    [failed] = credential_transfer.import_credentials([raw], team_id="team_review")
    assert not failed.imported and failed.retryable
    assert not target.exists()
    assert not list(tmp_path.rglob(".credential-write-*"))

    [retried] = credential_transfer.import_credentials([raw], team_id="team_review")
    assert retried.imported
    assert target.read_bytes() == expected
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.rglob(".credential-write-*"))


@pytest.mark.parametrize("kind", ["owner_file", "template_overrides", "stored_value"])
def test_import_keeps_a_preexisting_destination_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    raw, target, _expected = _record(kind, tmp_path, monkeypatch)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"destination credential")

    [outcome] = credential_transfer.import_credentials([raw], team_id="team_review")
    assert not outcome.imported and not outcome.retryable
    assert target.read_bytes() == b"destination credential"
    assert not list(tmp_path.rglob(".credential-write-*"))


@pytest.mark.parametrize("kind", ["owner_file", "template_overrides", "stored_value"])
def test_import_keeps_a_destination_credential_created_during_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    raw, target, _expected = _record(kind, tmp_path, monkeypatch)
    original = os.link

    def competing_link(source, destination, **kwargs):
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=kwargs["dst_dir_fd"])
        try:
            os.write(descriptor, b"new destination credential")
        finally:
            os.close(descriptor)
        return original(source, destination, **kwargs)

    monkeypatch.setattr(private_files.os, "link", competing_link)
    [outcome] = credential_transfer.import_credentials([raw], team_id="team_review")

    assert not outcome.imported and not outcome.retryable
    assert target.read_bytes() == b"new destination credential"
    assert not list(tmp_path.rglob(".credential-write-*"))


def test_normal_broker_save_retains_the_complete_old_value_until_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "session"
    write_private_broker_file(target, "complete old session")
    _interrupt_first_write(monkeypatch)

    with pytest.raises(OSError, match="interrupted credential write"):
        write_private_broker_file(target, "complete new session")
    assert target.read_bytes() == b"complete old session\n"
    assert not list(tmp_path.rglob(".credential-write-*"))

    write_private_broker_file(target, "complete new session")
    assert target.read_bytes() == b"complete new session\n"
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("exclusive", [False, True])
@pytest.mark.parametrize("linked_folder", [False, True])
def test_private_writer_refuses_file_and_folder_links(
    tmp_path: Path, exclusive: bool, linked_folder: bool
) -> None:
    root = tmp_path / "credentials"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "session"
    sentinel.write_bytes(b"kept outside value")
    if linked_folder:
        (root / "bitwarden").symlink_to(outside, target_is_directory=True)
        parts = ("bitwarden", "session")
    else:
        (root / "session").symlink_to(sentinel)
        parts = ("session",)

    with pytest.raises(safe_open.UnsafePath) as refused:
        private_files.write_private_file(root, parts, b"replacement", exclusive=exclusive)
    assert refused.value.reason == safe_open.REASON_LINK
    assert sentinel.read_bytes() == b"kept outside value"
    assert not list(tmp_path.rglob(".credential-write-*"))


def test_publication_syncs_the_file_and_directory(tmp_path: Path, monkeypatch) -> None:
    synced = []
    original = os.fsync

    def fsync(descriptor):
        synced.append(os.fstat(descriptor).st_mode)
        return original(descriptor)

    monkeypatch.setattr(private_files.os, "fsync", fsync)
    private_files.write_private_file(tmp_path, ("bitwarden", "session"), b"complete", exclusive=True)

    assert any(stat.S_ISREG(mode) for mode in synced)
    assert stat.S_ISDIR(synced[-1])
    assert (tmp_path / "bitwarden/session").read_bytes() == b"complete"
    assert not list(tmp_path.rglob(".credential-write-*"))


def test_export_during_publication_omits_unpublished_session_files(tmp_path: Path, monkeypatch) -> None:
    raw, target, expected = _record("owner_file", tmp_path, monkeypatch)
    owner = credential_transfer.CredentialOwnerV1.model_validate(raw["owner"])
    during_publication = []
    original = os.link

    def export_then_publish(source, destination, **kwargs):
        during_publication.extend(credential_transfer._export_owner_files(owner))
        return original(source, destination, **kwargs)

    monkeypatch.setattr(private_files.os, "link", export_then_publish)
    [outcome] = credential_transfer.import_credentials([raw], team_id="team_review")

    assert outcome.imported and target.read_bytes() == expected
    assert during_publication == []
    [published] = credential_transfer._export_owner_files(owner)
    assert published.path == "bitwarden/session"
    assert base64.b64decode(published.content_b64) == expected

"""The source check must catch edits beyond the reviewed ZIP compatibility patch."""

import hashlib
import io
import tarfile

import pytest

from scripts.check_vendored_rust import verify_crate


def test_vendored_source_matches_archive_and_explicit_patch(tmp_path):
    buffer = io.BytesIO()
    original = {"Cargo.toml": b"old zip", "src/lib.rs": b"unchanged", "Cargo.lock": b"old lock"}
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative, contents in original.items():
            member = tarfile.TarInfo(f"fixture-1.0.0/{relative}")
            member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents))
    raw = buffer.getvalue()
    crate = tmp_path / "fixture-1.0.0"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_bytes(b"new zip")
    source_file = crate / "src/lib.rs"
    source_file.write_bytes(b"unchanged")
    source = {
        "name": "fixture",
        "version": "1.0.0",
        "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "patched_files": {"Cargo.toml": hashlib.sha256(b"new zip").hexdigest()},
    }
    verify_crate(tmp_path, source, raw, {"Cargo.lock"})
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_crate(tmp_path, source, raw + b"changed", {"Cargo.lock"})
    source_file.write_bytes(b"unreviewed edit")
    with pytest.raises(ValueError, match="unrecorded source change"):
        verify_crate(tmp_path, source, raw, {"Cargo.lock"})
    source_file.write_bytes(b"unchanged")
    (crate / "extra.rs").write_bytes(b"unreviewed addition")
    with pytest.raises(ValueError, match="file list differs"):
        verify_crate(tmp_path, source, raw, {"Cargo.lock"})

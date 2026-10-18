"""Compare the temporary Rust source patches with checksum-pinned crate archives."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import tarfile
import urllib.request
from pathlib import Path

LOGGER = logging.getLogger(__name__)
VENDOR_ROOT = Path(__file__).resolve().parents[1] / "rust" / "vendor"


def verify_crate(root: Path, source: dict, archive_bytes: bytes, omitted: set[str]) -> None:
    """Refuse added, missing or changed files outside the recorded patch."""
    name = f"{source['name']}-{source['version']}"
    if hashlib.sha256(archive_bytes).hexdigest() != source["archive_sha256"]:
        raise ValueError(f"{name}: registry archive checksum mismatch")
    patched = source["patched_files"]
    expected = set()
    changed = set()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                raise ValueError(f"{name}: unexpected non-file archive entry")
            prefix, relative = member.name.split("/", 1)
            if prefix != name or ".." in Path(relative).parts:
                raise ValueError(f"{name}: invalid archive path")
            if relative in omitted:
                continue
            expected.add(relative)
            original = archive.extractfile(member).read()
            copied = (root / name / relative).read_bytes()
            if copied != original:
                changed.add(relative)
                if hashlib.sha256(copied).hexdigest() != patched.get(relative):
                    raise ValueError(f"{name}/{relative}: unrecorded source change")
    if changed != set(patched):
        raise ValueError(f"{name}: patch file list does not match modified source")
    actual = set()
    for path in (root / name).rglob("*"):
        if path.is_symlink():
            raise ValueError(f"{name}: symlink in copied source")
        if path.is_file():
            actual.add(path.relative_to(root / name).as_posix())
    if actual != expected:
        raise ValueError(f"{name}: copied file list differs from registry archive")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    manifest = json.loads((VENDOR_ROOT / "sources.json").read_text())
    for source in manifest["crates"]:
        name = f"{source['name']}-{source['version']}"
        url = f"https://static.crates.io/crates/{source['name']}/{name}.crate"
        request = urllib.request.Request(url, headers={"User-Agent": "ua-vendor-check/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            archive_bytes = response.read()
        verify_crate(VENDOR_ROOT, source, archive_bytes, set(manifest["omitted_files"]))
        LOGGER.info("%s: source and recorded patch verified", name)


if __name__ == "__main__":
    main()

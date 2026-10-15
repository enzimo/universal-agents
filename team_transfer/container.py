"""Write and read transfer files (ZIP archives) without trusting their contents.

Writing: members go into a ZIP created next to the destination with mode
0600, then the finished file is published with ``os.link``, which never
replaces an existing file. The manifest is written last, after every member's
size and SHA-256 is known, and carries the MAC (``team_transfer.crypto``).

Reading checks the whole file before anything is used:

- entry names are relative, canonical, under a known root and unique, and
  every entry is a regular file stored or deflated (no links, no folders);
- the number of entries and their total unpacked size stay within limits,
  which stops a crafted file from filling the disk;
- the manifest validates, its MAC matches the passphrase, and it names exactly
  the other entries with their sizes and SHA-256 digests.

A member is checked again when it is read, so a file changed on disk after
opening is refused rather than half-used.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Final, Iterator

from pydantic import ValidationError

from team_transfer.crypto import (
    TransferKeys,
    canonical_json,
    derive_keys,
    manifest_mac,
    verify_manifest,
)
from team_transfer.format import (
    MANIFEST_MEMBER,
    MEMBER_ROOTS,
    TRANSFER_FORMAT,
    TRANSFER_FORMAT_VERSION,
    MemberV1,
    SectionDocumentV1,
    TransferManifestV1,
    section_member,
)

logger = logging.getLogger(__name__)

# Configuration only (owner's decision 2026-10-05): skills and installed CLI
# programs are the largest members, so these limits leave ample room while
# stopping a crafted archive from exhausting the disk or memory.
MAX_MEMBERS: Final = 50_000
MAX_TOTAL_BYTES: Final = 4 * 1024**3
MAX_MEMBER_BYTES: Final = 1024**3
MAX_MANIFEST_BYTES: Final = 64 * 1024**2
_CHUNK: Final = 1024 * 1024
_ALLOWED_COMPRESSION: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


class TransferFileError(ValueError):
    """The transfer file is malformed, too large, or does not match its manifest."""


def check_member_name(name: str) -> PurePosixPath:
    """A member name that cannot escape the archive or mean two things."""

    path = PurePosixPath(name)
    if (
        not name
        or name.endswith("/")
        or path.is_absolute()
        or "\\" in name
        or ":" in name
        or "\0" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or str(path) != name
    ):
        raise TransferFileError(f"The file contains an unsafe entry name: {name!r}.")
    return path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- writing ---------------------------------------------------------------------


@dataclass
class TransferWriter:
    """Collects members for one transfer file. Use ``create`` and ``publish``."""

    destination: Path
    _temporary: Path
    _handle: Any
    _archive: zipfile.ZipFile
    _members: list[MemberV1] = field(default_factory=list)
    _total_bytes: int = 0

    @classmethod
    def create(cls, destination: str | Path) -> "TransferWriter":
        target = Path(destination).expanduser().absolute()
        if target.exists() or target.is_symlink():
            raise TransferFileError(
                f"{target} already exists. Choose a new file name; an export never overwrites a file."
            )
        if not target.parent.is_dir():
            raise TransferFileError(f"The folder {target.parent} does not exist.")
        temporary = target.with_name(f".{target.name}.{os.getpid()}.partial")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        handle = os.fdopen(fd, "wb")
        archive = zipfile.ZipFile(
            handle, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        )
        return cls(target, temporary, handle, archive)

    @property
    def member_paths(self) -> frozenset[str]:
        return frozenset(member.path for member in self._members)

    def add_bytes(self, name: str, data: bytes) -> MemberV1:
        check_member_name(name)
        if name == MANIFEST_MEMBER or name.split("/", 1)[0] not in MEMBER_ROOTS:
            raise TransferFileError(f"A section may not write the member {name!r}.")
        if name in self.member_paths:
            raise TransferFileError(f"The member {name!r} was written twice.")
        if len(data) > MAX_MEMBER_BYTES:
            raise TransferFileError(
                f"The member {name!r} is larger than {MAX_MEMBER_BYTES} bytes."
            )
        if (
            len(self._members) >= MAX_MEMBERS
            or self._total_bytes + len(data) > MAX_TOTAL_BYTES
        ):
            raise TransferFileError(
                "The export exceeds the transfer archive's member or total-size limit."
            )
        self._archive.writestr(_entry(name), data)
        member = MemberV1(path=name, size=len(data), sha256=_sha256(data))
        self._members.append(member)
        self._total_bytes += len(data)
        return member

    def add_json(self, name: str, value: Any) -> MemberV1:
        return self.add_bytes(name, canonical_json(value))

    def add_section(self, document: SectionDocumentV1) -> MemberV1:
        return self.add_json(
            section_member(document.section), document.model_dump(mode="json")
        )

    def publish(
        self, manifest: TransferManifestV1, keys: TransferKeys
    ) -> TransferManifestV1:
        """Write the signed manifest and publish the file under its final name."""

        unsigned = manifest.model_copy(
            update={
                "members": tuple(sorted(self._members, key=lambda m: m.path)),
                "mac": "",
            }
        )
        signed = unsigned.model_copy(update={"mac": manifest_mac(unsigned, keys)})
        TransferManifestV1.model_validate(signed.model_dump(mode="json"))
        try:
            content = canonical_json(signed.model_dump(mode="json"))
            if (
                len(content) > MAX_MANIFEST_BYTES
                or self._total_bytes + len(content) > MAX_TOTAL_BYTES
            ):
                raise TransferFileError(
                    "The export exceeds the transfer archive's manifest or total-size limit."
                )
            self._archive.writestr(_entry(MANIFEST_MEMBER), content)
            self._archive.close()
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            os.link(self._temporary, self.destination)
        except FileExistsError as exc:
            raise TransferFileError(
                f"{self.destination} appeared while exporting. Choose a new file name; an export never overwrites a file."
            ) from exc
        finally:
            self.discard()
        logger.info(
            "Team transfer file written. event=team_transfer_written path=%s members=%s sections=%s",
            self.destination,
            len(signed.members),
            ",".join(entry.name for entry in signed.sections),
        )
        return signed

    def discard(self) -> None:
        """Remove the partial file (after a failure, or once published)."""

        for closer in (self._archive.close, self._handle.close):
            try:
                closer()
            except (OSError, ValueError):
                pass
        self._temporary.unlink(missing_ok=True)


def _entry(name: str) -> zipfile.ZipInfo:
    """A regular file entry with fixed time and mode, so exports differ only in content."""

    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


# -- reading ---------------------------------------------------------------------


@dataclass
class TransferReader:
    """An opened, verified transfer file. Use ``open`` as a context manager."""

    path: Path
    manifest: TransferManifestV1
    keys: TransferKeys = field(repr=False)
    _archive: zipfile.ZipFile = field(repr=False)

    @classmethod
    def open(cls, path: str | Path, passphrase: str) -> "TransferReader":
        source = Path(path).expanduser()
        if not source.is_file():
            raise TransferFileError(f"There is no transfer file at {source}.")
        try:
            archive = zipfile.ZipFile(source)
        except (zipfile.BadZipFile, OSError) as exc:
            raise TransferFileError(
                f"{source} is not a transfer file (not a ZIP archive)."
            ) from exc
        try:
            entries = _checked_entries(archive)
            manifest = _read_manifest(archive, entries)
            keys = derive_keys(passphrase, manifest.key_derivation)
            verify_manifest(manifest, keys)
            reader = cls(source, manifest, keys, archive)
            reader._check_members(entries)
        except BaseException:
            archive.close()
            raise
        return reader

    def __enter__(self) -> "TransferReader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._archive.close()

    def _member(self, name: str) -> MemberV1:
        for member in self.manifest.members:
            if member.path == name:
                return member
        raise TransferFileError(f"The file has no member {name!r}.")

    def has_member(self, name: str) -> bool:
        return any(member.path == name for member in self.manifest.members)

    def read_member(self, name: str) -> bytes:
        member = self._member(name)
        data = _read_bounded(self._archive, name, member.size)
        if len(data) != member.size or _sha256(data) != member.sha256:
            raise TransferFileError(
                f"The member {name!r} changed after the file was opened."
            )
        return data

    def members_under(self, prefix: str) -> list[str]:
        return sorted(
            member.path
            for member in self.manifest.members
            if member.path.startswith(prefix)
        )

    def section_document(self, name: str) -> SectionDocumentV1 | None:
        """The stored document of a plain section, or None when the file has none."""

        entry = self.manifest.section(name)
        if entry is None or entry.encrypted:
            return None
        try:
            document = SectionDocumentV1.model_validate_json(
                self.read_member(section_member(name))
            )
        except ValidationError as exc:
            raise TransferFileError(
                f"The section {name!r} is not valid: {exc.errors()[0]['msg']}"
            ) from exc
        if document.section != name or document.schema_version != entry.schema_version:
            raise TransferFileError(
                f"The section {name!r} does not match its manifest entry."
            )
        return document

    def _check_members(self, entries: dict[str, zipfile.ZipInfo]) -> None:
        listed = {member.path for member in self.manifest.members}
        present = set(entries) - {MANIFEST_MEMBER}
        if listed != present:
            extra = sorted(present - listed)[:3]
            missing = sorted(listed - present)[:3]
            raise TransferFileError(
                f"The file's contents do not match its manifest (extra: {extra}, missing: {missing})."
            )
        for member in self.manifest.members:
            if member.path.split("/", 1)[0] not in MEMBER_ROOTS:
                raise TransferFileError(
                    f"The member {member.path!r} is outside the known folders."
                )
            if entries[member.path].file_size != member.size:
                raise TransferFileError(
                    f"The member {member.path!r} has a different size than its manifest says."
                )
            self.read_member(member.path)
        for entry in self.manifest.sections:
            if not entry.encrypted and not self.has_member(section_member(entry.name)):
                raise TransferFileError(
                    f"The manifest names the section {entry.name!r}, but the file lacks it."
                )


def _checked_entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS + 1:
        raise TransferFileError(f"The file has more than {MAX_MEMBERS} entries.")
    if sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
        raise TransferFileError(
            f"The file unpacks to more than {MAX_TOTAL_BYTES} bytes."
        )
    entries: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        check_member_name(info.filename)
        if info.filename in entries:
            raise TransferFileError(
                f"The file contains the entry {info.filename!r} twice."
            )
        mode = info.external_attr >> 16
        if info.is_dir() or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
            raise TransferFileError(
                f"The entry {info.filename!r} is not a regular file."
            )
        if info.compress_type not in _ALLOWED_COMPRESSION:
            raise TransferFileError(
                f"The entry {info.filename!r} uses an unsupported compression method."
            )
        if info.file_size > MAX_MEMBER_BYTES and info.filename != MANIFEST_MEMBER:
            raise TransferFileError(
                f"The entry {info.filename!r} is larger than {MAX_MEMBER_BYTES} bytes."
            )
        entries[info.filename] = info
    return entries


def _read_manifest(
    archive: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo]
) -> TransferManifestV1:
    info = entries.get(MANIFEST_MEMBER)
    if info is None or info.file_size > MAX_MANIFEST_BYTES:
        raise TransferFileError(
            "The file has no manifest, or its manifest is too large."
        )
    raw = _read_bounded(archive, MANIFEST_MEMBER, info.file_size)
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise TransferFileError("The file's manifest is not valid JSON.") from exc
    if not isinstance(document, dict) or document.get("format") != TRANSFER_FORMAT:
        raise TransferFileError("This is not a Universal Agents team transfer file.")
    version = document.get("format_version")
    if version != TRANSFER_FORMAT_VERSION:
        raise TransferFileError(
            f"The file uses transfer format {version!r}; this version reads format {TRANSFER_FORMAT_VERSION}."
        )
    try:
        return TransferManifestV1.model_validate(document)
    except ValidationError as exc:
        raise TransferFileError(
            f"The file's manifest is not valid: {exc.errors()[0]['msg']}"
        ) from exc


def _read_bounded(archive: zipfile.ZipFile, name: str, expected: int) -> bytes:
    """Read one entry, refusing to unpack more than its stated size."""

    chunks: list[bytes] = []
    total = 0
    with archive.open(name) as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            total += len(chunk)
            if total > expected:
                raise TransferFileError(
                    f"The member {name!r} unpacks to more than its stated size."
                )
            chunks.append(chunk)
    return b"".join(chunks)


def iter_files(root: Path) -> Iterator[Path]:
    """Regular files below ``root`` in a stable order, refusing links.

    Sections carry trusted control files (``control/``, reviewed skill
    installs); a link there is an anomaly, so it stops the export instead of
    being followed or silently skipped.
    """

    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise TransferFileError(
                f"{path} is a link; the export does not follow links."
            )
        if path.is_file():
            yield path


__all__ = [
    "MAX_MEMBERS",
    "MAX_MEMBER_BYTES",
    "MAX_TOTAL_BYTES",
    "TransferFileError",
    "TransferReader",
    "TransferWriter",
    "check_member_name",
    "iter_files",
]

"""The transfer file's manifest and the shapes every section shares.

A transfer file is a ZIP archive (written by ``team_transfer.container``):

- ``manifest.json``: this module's ``TransferManifestV1``;
- ``sections/<name>.json``: one ``SectionDocumentV1`` per section;
- ``files/<section>/...``: documents and programs a section carries as files
  (control files, skill trees, installed CLI programs);
- ``credentials/<name>.enc``: encrypted sections (credentials).

The manifest lists the size and SHA-256 of every other member and carries a
MAC made with a key derived from the transfer passphrase, so a reader can tell
that nothing was added, removed or edited after the export.
"""

from __future__ import annotations

import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TRANSFER_FORMAT: Final = "universal-agents-team-transfer"
TRANSFER_FORMAT_VERSION: Final = 1
MANIFEST_MEMBER: Final = "manifest.json"
SECTIONS_DIR: Final = "sections"
FILES_DIR: Final = "files"
CREDENTIALS_DIR: Final = "credentials"
MEMBER_ROOTS: Final = frozenset({SECTIONS_DIR, FILES_DIR, CREDENTIALS_DIR})
# Section names are also folder names inside the file.
SECTION_NAME_PATTERN: Final = r"^[a-z][a-z0-9_]{0,63}$"
_SECTION_NAME = re.compile(SECTION_NAME_PATTERN)


def section_member(name: str) -> str:
    return f"{SECTIONS_DIR}/{name}.json"


def section_files_prefix(name: str) -> str:
    return f"{FILES_DIR}/{name}/"


class TransferModel(BaseModel):
    """Strict and immutable: an unknown field is a format change, never ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class KeyDerivationV1(TransferModel):
    """How the transfer passphrase becomes keys (``team_transfer.crypto``)."""

    algorithm: Literal["argon2id"] = "argon2id"
    salt_b64: str = Field(min_length=1, max_length=64)
    opslimit: int = Field(ge=1, le=10)
    # libsodium's minimum is 8 KiB; 1 GiB caps the memory an import can be made to spend.
    memlimit: int = Field(ge=8192, le=1024**3)


class MemberV1(TransferModel):
    """One file inside the archive."""

    path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class SectionEntryV1(TransferModel):
    """One section in the manifest."""

    name: str = Field(pattern=SECTION_NAME_PATTERN)
    schema_version: int = Field(ge=1)
    record_count: int = Field(ge=0)
    encrypted: bool = False


class ExporterV1(TransferModel):
    """Who exported the team.

    The host has no signed-in person: whoever runs the command holds the
    instance's root keys. ``--owner`` must name an active owner; the name is
    recorded here and in the notices people get after import. It is
    attribution, not sign-in.
    """

    owner_person_id: str = Field(min_length=1, max_length=240)


class TransferManifestV1(TransferModel):
    format: Literal["universal-agents-team-transfer"] = TRANSFER_FORMAT
    format_version: Literal[1] = TRANSFER_FORMAT_VERSION
    application_version: str = Field(min_length=1, max_length=64)
    exported_at_utc: str = Field(min_length=1, max_length=64)
    team_id: str = Field(min_length=1, max_length=120)
    instance_name: str = Field(min_length=1, max_length=200)
    exported_by: ExporterV1
    key_derivation: KeyDerivationV1
    sections: tuple[SectionEntryV1, ...] = ()
    members: tuple[MemberV1, ...] = ()
    # Hex MAC over the manifest without this field (``crypto.manifest_mac``).
    mac: str = Field(default="", pattern=r"^([a-f0-9]{64})?$")

    @field_validator("sections")
    @classmethod
    def _unique_sections(cls, value: tuple[SectionEntryV1, ...]) -> tuple[SectionEntryV1, ...]:
        names = [entry.name for entry in value]
        if len(names) != len(set(names)):
            raise ValueError("The manifest names a section twice.")
        return value

    @field_validator("members")
    @classmethod
    def _unique_members(cls, value: tuple[MemberV1, ...]) -> tuple[MemberV1, ...]:
        paths = [member.path for member in value]
        if len(paths) != len(set(paths)):
            raise ValueError("The manifest names a member twice.")
        return value

    def section(self, name: str) -> SectionEntryV1 | None:
        return next((entry for entry in self.sections if entry.name == name), None)

    def unsigned(self) -> dict[str, Any]:
        """The manifest as the MAC covers it: everything but the MAC."""

        return self.model_dump(mode="json", exclude={"mac"})


class SectionDocumentV1(TransferModel):
    """The JSON stored at ``sections/<name>.json``.

    ``records`` stays a plain list here; each section validates it with its
    own export models after any one-version-back conversion
    (``team_transfer.sections``).
    """

    section: str = Field(pattern=SECTION_NAME_PATTERN)
    schema_version: int = Field(ge=1)
    records: list[dict[str, Any]] = Field(default_factory=list)


def valid_section_name(name: str) -> bool:
    return bool(_SECTION_NAME.match(name))


__all__ = [
    "CREDENTIALS_DIR",
    "ExporterV1",
    "FILES_DIR",
    "KeyDerivationV1",
    "MANIFEST_MEMBER",
    "MEMBER_ROOTS",
    "MemberV1",
    "SECTIONS_DIR",
    "SECTION_NAME_PATTERN",
    "SectionDocumentV1",
    "SectionEntryV1",
    "TRANSFER_FORMAT",
    "TRANSFER_FORMAT_VERSION",
    "TransferManifestV1",
    "TransferModel",
    "section_files_prefix",
    "section_member",
    "valid_section_name",
]

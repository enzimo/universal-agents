"""What every section of a transfer file provides, and how older section versions are read.

A section is one kind of team state (settings, people, grants, schedules, ...).
Each section is a small class with three steps:

- ``export`` reads the running instance through its own repositories and
  services and returns plain records (and any files) for the transfer file;
- ``plan`` decides, for each record, what the import will do with it: import
  it, import it with something the operator must look at, or refuse it with
  the reason. It never writes. ``--dry-run`` shows exactly this plan;
- ``apply`` writes the planned records into the fresh instance through the new
  version's repositories.

Sections run in the order of ``team_transfer.registry.TRANSFER_SECTIONS``, so
a section may rely on the ones before it (people before grants, settings
before everything).

One release back (owner's decision 2026-10-05): a version reads its own
section versions and the ones the previous release exported. For a section
that changed since that release, ``SECTION_UPGRADES`` maps ``(section,
previous version)`` to a pure function that turns those records into this
version's. Steps are pruned by release, not by section: when a release ships,
delete every step whose source version is older than what that release
exports (a section that did not change keeps no step). Anything older, or
newer, is refused before anything is written, with import/export instructions
and the guide's release-specific paths (``readable_version_problem``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Protocol

from pydantic import BaseModel

from team_transfer.container import (
    TransferFileError,
    TransferReader,
    TransferWriter,
    check_member_name,
)
from team_transfer.crypto import TransferKeys, canonical_json, decrypt, encrypt
from team_transfer.format import (
    CREDENTIALS_DIR,
    SectionDocumentV1,
    section_files_prefix,
)

if TYPE_CHECKING:
    from assistant_gateway.config import GatewayInstanceConfig
    from runtime_settings.instance_layout import InstanceLayout

SectionRecords = list[dict[str, Any]]
SectionUpgrade = Callable[[SectionRecords], SectionRecords]

# (section name, source version) -> conversion to this release's version. Keep only the steps
# from the previous release's versions; delete older ones. Empty while no
# section has changed since the previous release.
SECTION_UPGRADES: dict[tuple[str, int], SectionUpgrade] = {}


class RecordOutcome(StrEnum):
    IMPORT = "import"
    # Imported, but the operator must look at it (for example a schedule
    # turned off because its one run passed during the move).
    ATTENTION = "attention"
    REFUSED = "refused"


@dataclass(frozen=True)
class PlannedRecord:
    """What the import does with one record. ``key`` names the record for people."""

    section: str
    key: str
    outcome: RecordOutcome
    reason: str = ""


@dataclass(frozen=True)
class ExportSource:
    """The running instance an export reads."""

    config: "GatewayInstanceConfig"
    layout: "InstanceLayout"
    team_id: str
    instance_name: str


@dataclass
class SectionFiles:
    """Lets one section add files under ``files/<section>/`` and nowhere else.

    ``keys`` is for encrypted sections only: they hand the encryption subkey to
    the process that holds the secrets (the broker), which returns ciphertext.
    """

    section: str
    writer: TransferWriter
    keys: TransferKeys = field(repr=False)

    def add(self, relative: str, data: bytes) -> str:
        check_member_name(relative)
        name = section_files_prefix(self.section) + relative
        self.writer.add_bytes(name, data)
        return name


@dataclass(frozen=True)
class DestinationChoices:
    """What the person importing decides about the new instance, over the carried settings.

    Only a first import into a fresh root uses them; later batches cannot move
    existing data or keys. ``secrets_source`` is ``launch.secrets_source``
    (``runtime_settings.secrets_source``): the old instance's choice travels
    in the ``settings`` section, but the new host may differ (for example a
    Docker Swarm service that gives every secret as a Docker secret), so
    onboarding's Import asks and its answer wins.
    """

    secrets_source: str | None = None


@dataclass
class SectionExport:
    """A section's records and, for encrypted sections, its ciphertext.

    ``record_count`` is for sections whose records this process never sees
    (the broker encrypts its credentials itself).
    """

    records: SectionRecords = field(default_factory=list)
    ciphertext: bytes | None = None
    record_count: int | None = None


@dataclass
class ImportContext:
    """What a section sees while planning or applying an import."""

    reader: TransferReader
    target_root: Path
    layout: "InstanceLayout"
    # This version's section versions, by name (from the section registry).
    versions: dict[str, int]
    # What a section's plan leaves for its apply (for example the validated
    # configuration), keyed by the section's own names.
    scratch: dict[str, Any] = field(default_factory=dict)
    # The plans of the sections planned so far, in order, so a section can
    # check what an earlier one accepts (grants on imported tools, people).
    planned: list[PlannedRecord] = field(default_factory=list)
    _records: dict[str, SectionRecords] = field(default_factory=dict)
    existing: bool = False
    baseline: dict[str, SectionRecords] = field(default_factory=dict)
    baseline_keys: dict[str, set[str]] = field(default_factory=dict)
    baseline_files: dict[str, bytes] = field(default_factory=dict, repr=False)
    destination: DestinationChoices = field(default_factory=DestinationChoices)

    def accepted(self, section: str) -> set[str]:
        """Keys of the records an earlier section imports."""

        return self.baseline_keys.get(section, set()) | {
            record.key
            for record in self.planned
            if record.section == section and record.outcome is not RecordOutcome.REFUSED
        }

    def records(self, section: str) -> SectionRecords:
        """A section's records (converted to this version).

        Empty when the file has none or the section is not part of this
        import: it then contributes nothing for other sections to rely on.
        """

        if section not in self.versions:
            return []
        if section not in self._records:
            self._records[section] = read_section_records(
                self.reader, section, self.versions[section]
            )
        return self._records[section]

    def file(self, section: str, relative: str, *, baseline: bool = False) -> bytes:
        name = section_files_prefix(section) + relative
        if baseline:
            return self.baseline_files[name]
        return self.reader.read_member(name)

    def files(self, section: str) -> list[str]:
        prefix = section_files_prefix(section)
        return [name[len(prefix) :] for name in self.reader.members_under(prefix)]


class TransferSection(Protocol):
    name: ClassVar[str]
    schema_version: ClassVar[int]
    # Encrypted sections are stored as ciphertext (``credentials/<name>.enc``).
    encrypted: ClassVar[bool]
    # Imported after the new instance's first start (``team import-credentials``),
    # because only the running broker may store its values.
    after_start: ClassVar[bool]
    # The export model each record validates against. Its JSON Schema is
    # checked in (``scripts/export_team_transfer_schemas.py``).
    record_model: ClassVar[type[BaseModel]]

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport: ...

    def plan(
        self, records: SectionRecords, context: ImportContext
    ) -> list[PlannedRecord]: ...

    def apply(
        self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext
    ) -> None: ...


def encrypted_member(section: str) -> str:
    return f"{CREDENTIALS_DIR}/{section}.enc"


def encrypt_section(
    section: str, schema_version: int, records: SectionRecords, keys: TransferKeys
) -> bytes:
    """Ciphertext of an encrypted section: its ``SectionDocumentV1``, read back by ``read_section_records``.

    Here rather than in ``team_transfer.service`` so encrypted sections can use
    it without importing the service that loads them.
    """

    document = SectionDocumentV1(
        section=section, schema_version=schema_version, records=records
    )
    return encrypt(canonical_json(document.model_dump(mode="json")), keys)


def read_section_records(
    reader: TransferReader, section: str, version: int
) -> SectionRecords:
    """The section's records at ``version``, converting the previous release's version.

    An encrypted section's plaintext is a ``SectionDocumentV1`` too; it is
    decrypted here and nowhere else, only when a section asks for it.
    """

    entry = reader.manifest.section(section)
    if entry is None:
        return []
    if entry.encrypted:
        plaintext = decrypt(reader.read_member(encrypted_member(section)), reader.keys)
        try:
            document = SectionDocumentV1.model_validate_json(plaintext)
        except ValueError as exc:
            raise TransferFileError(
                f"The encrypted section {section!r} is invalid; secret values are omitted from this error."
            ) from exc
        if (
            document.section != section
            or document.schema_version != entry.schema_version
        ):
            raise TransferFileError(
                f"The encrypted section {section!r} does not match its manifest entry."
            )
    else:
        found = reader.section_document(section)
        if found is None:
            return []
        document = found
    if len(document.records) != entry.record_count:
        raise TransferFileError(
            f"The section {section!r} record count does not match its manifest entry."
        )
    return upgrade_records(
        document, version, exported_by=reader.manifest.application_version
    )


def upgrade_records(
    document: SectionDocumentV1, version: int, *, exported_by: str | None = None
) -> SectionRecords:
    """The document's records at ``version``, through its conversion step when it is the previous release's.

    ``exported_by`` is the exporting release (from the manifest), named in the
    refusal when the file is too old or too new.
    """

    stored = document.schema_version
    problem = readable_version_problem(
        document.section, stored, version, exported_by=exported_by
    )
    if problem:
        raise TransferFileError(problem)
    if stored == version:
        return list(document.records)
    return SECTION_UPGRADES[(document.section, stored)](list(document.records))


def readable_version_problem(
    section: str, stored: int, version: int, *, exported_by: str | None = None
) -> str:
    """Why this version cannot read ``section`` at ``stored``, or empty when it can.

    It reads ``version`` and the older source versions with explicit conversion
    steps. A release can change the same section more than once, so a previous
    release's source version need not be numerically adjacent to this one.
    One message for every section, plain and encrypted, so the operator always
    learns how to continue. Older exports may need several intermediate
    releases; the transfer guide owns the release-specific paths.
    """

    if stored == version or (stored < version and (section, stored) in SECTION_UPGRADES):
        return ""
    accepted = sorted({version, *(source for name, source in SECTION_UPGRADES if name == section and source < version)})
    readable = f"version {version} only"
    if len(accepted) > 1:
        readable = "versions " + ", ".join(str(source) for source in accepted[:-1]) + f" and {accepted[-1]}"
    found = (
        f"This file's {section} section is version {stored}, but this version of Universal Agents reads "
        f"{readable}."
    )
    release = f" ({exported_by})" if exported_by else ""
    if stored > version:
        return f"{found} The file was exported by a newer release{release}: import it with that release or a later one."
    return (
        f"{found} The file was exported by an older release{release}: import it into a newer release that can read it "
        "and export the team again there. Repeat with newer releases until this release can read the file. "
        "See the 'Moving a team to a new version' guide for the required releases."
    )


__all__ = [
    "DestinationChoices",
    "ExportSource",
    "ImportContext",
    "PlannedRecord",
    "RecordOutcome",
    "SECTION_UPGRADES",
    "SectionExport",
    "SectionFiles",
    "SectionRecords",
    "TransferSection",
    "encrypt_section",
    "encrypted_member",
    "read_section_records",
    "readable_version_problem",
    "upgrade_records",
]

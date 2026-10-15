"""Private, editable review files bound to one authenticated backup and target."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from access_control import safe_open
from team_transfer.editable_yaml import load_editable_yaml
from team_transfer.errors import TransferError
from team_transfer.format import TransferModel
from team_transfer.sections import read_section_records
from team_transfer.selection import keyed_records

LOGGER = logging.getLogger(__name__)


class ReviewRecordV1(TransferModel):
    section: str
    key: str
    selected: bool = True
    file: str | None = None
    confirmed_digest: str | None = None


class ImportReviewV1(TransferModel):
    schema_version: int = 1
    source_mac: str
    target_root: str
    records: list[ReviewRecordV1] = Field(default_factory=list)


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _edit_path(section: str, key: str) -> str:
    folder, suffix = {
        "schedules": ("schedules", "md"),
        "settings": ("settings", "yaml"),
        "pinned_memory": ("memories", "md"),
    }[section]
    digest = text_digest(f"{section}\0{key}")[:24]
    return f"{folder}/{digest}.{suffix}"


def _text(raw: dict[str, Any], section: str) -> str | None:
    if section == "schedules" and raw.get("kind") == "schedule":
        return raw["schedule"]["markdown"]
    if section == "settings":
        return yaml.safe_dump(
            {"value": raw["value"]}, allow_unicode=True, sort_keys=False
        )
    if section == "pinned_memory":
        return raw["document"]["fact_text"]
    return None


def archive_records(reader) -> dict[str, dict[str, dict[str, Any]]]:
    from team_transfer.service import default_sections

    found = {}
    for section in default_sections():
        if reader.manifest.section(section.name) is None:
            continue
        if section.after_start:
            path = f"files/{section.name}/index.json"
            if path in reader.members_under(f"files/{section.name}/"):
                index = json.loads(reader.read_member(path))
                found[section.name] = {entry["key"]: entry for entry in index}
                if len(found[section.name]) != len(index):
                    raise TransferError("The credential index carries duplicate keys.")
            continue
        found[section.name] = keyed_records(
            section.name,
            read_section_records(reader, section.name, section.schema_version),
        )
    return found


def prepare_review(
    reader, target_root: str | Path, directory: str | Path
) -> "ReviewFiles":
    """Publish a complete review once; failed writes leave its name available."""

    root = _review_directory(directory)
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The parent is trusted and private. Hold one directory lock while checking,
    # recovering and publishing so another caller cannot replace a completed review.
    with _initialization_lock(root.parent):
        if safe_open.entry_status(root.parent, (root.name,)) is not None:
            if safe_open.entry_status(root.parent, (root.name, "review.json")) is not None:
                return ReviewFiles.open(reader, root, target_root=target_root)
            # Old initialization wrote the target folder before review.json. Only
            # a folder without that completion record may be discarded on retry.
            safe_open.remove_directory(root.parent, (root.name,))
            LOGGER.warning(
                "Removed an unfinished import review before retrying. "
                "event=team_transfer_review_initialization_recovered directory=%s source_mac=%s",
                root,
                reader.manifest.mac,
            )
        return _initialize_review(reader, target_root, root)


def _review_directory(directory: str | Path) -> Path:
    # Resolve the trusted parent, but let safe_open reject a linked review folder.
    path = Path(directory).expanduser().absolute()
    return path.parent.resolve() / path.name


@contextmanager
def _initialization_lock(parent: Path):
    descriptor = safe_open.open_root(parent)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _initialize_review(reader, target_root: str | Path, root: Path) -> "ReviewFiles":
    archive = archive_records(reader)
    staging = Path(tempfile.mkdtemp(prefix=f".{root.name}.init-", dir=root.parent))
    try:
        files = _write_review(reader, target_root, staging, archive)
        # A sibling rename is atomic. A crash may leave a private staging folder,
        # whose random name does not prevent another initialization from starting.
        safe_open.move_directory(root.parent, (staging.name,), root.parent, (root.name,))
        files.root = root
        return files
    finally:
        safe_open.remove_directory(root.parent, (staging.name,))


def _write_review(reader, target_root: str | Path, root: Path, archive) -> "ReviewFiles":
    records = []
    for section, items in archive.items():
        for key, raw in items.items():
            text = _text(raw, section)
            file = _edit_path(section, key) if text is not None else None
            if file:
                safe_open.write_bytes(
                    root,
                    tuple(file.split("/")),
                    text.encode("utf-8"),
                    create_directories=True,
                )
                (root / file).chmod(0o600)
            records.append(ReviewRecordV1(section=section, key=key, file=file))
    review = ImportReviewV1(
        source_mac=reader.manifest.mac,
        target_root=str(Path(target_root).expanduser().resolve()),
        records=records,
    )
    files = ReviewFiles(reader, root, review, archive)
    files.save(review)
    return files


class ReviewFiles:
    def __init__(self, reader, root: Path, review: ImportReviewV1, archive):
        self.reader = reader
        self.root = root
        self.review = review
        self.archive = archive
        self._texts: dict[str, str] | None = None

    @classmethod
    def open(cls, reader, directory: str | Path, *, target_root=None):
        root = _review_directory(directory)
        review = ImportReviewV1.model_validate_json(
            safe_open.read_bytes(root.parent, (root.name, "review.json"))
        )
        if review.schema_version != 1 or review.source_mac != reader.manifest.mac:
            raise TransferError(
                "This review belongs to a different backup or review format."
            )
        if (
            target_root is not None
            and Path(review.target_root) != Path(target_root).expanduser().resolve()
        ):
            raise TransferError("This review belongs to a different target instance.")
        archive = archive_records(reader)
        if (
            any(record.section == "broker_credentials" for record in review.records)
            and "broker_credentials" not in archive
        ):
            from team_transfer.broker_credentials import import_credentials

            report = import_credentials(
                reader,
                str(Path(review.target_root) / "instance_config.yaml"),
                dry_run=True,
            )
            archive["broker_credentials"] = {
                entry["key"]: entry for entry in report.outcomes
            }
        files = cls(reader, root, review, archive)
        files._check_records(review)
        return files

    def _check_records(self, review: ImportReviewV1):
        seen = set()
        for record in review.records:
            identity = (record.section, record.key)
            if identity in seen or record.key not in self.archive.get(
                record.section, {}
            ):
                raise TransferError("The review names a duplicate or unknown record.")
            seen.add(identity)
            raw = self.archive[record.section][record.key]
            expected = (
                _edit_path(*identity)
                if _text(raw, record.section) is not None
                else None
            )
            if record.file != expected:
                raise TransferError(
                    "The review's editable file path does not match its record."
                )

    def save(self, review: ImportReviewV1):
        self._check_records(review)
        safe_open.write_bytes(
            self.root,
            ("review.json",),
            review.model_dump_json(indent=2).encode("utf-8"),
        )
        (self.root / "review.json").chmod(0o600)
        self.review = review

    def text(self, record: ReviewRecordV1) -> str | None:
        if record.file and self._texts is not None:
            return self._texts[record.file]
        return (
            safe_open.read_bytes(self.root, tuple(record.file.split("/"))).decode(
                "utf-8"
            )
            if record.file
            else None
        )

    def edited_record(self, record: ReviewRecordV1):
        from team_transfer.schedule_markdown import (
            edited_schedule,
            parse_schedule,
            render_schedule,
        )

        raw = copy.deepcopy(self.archive[record.section][record.key])
        text = self.text(record)
        if text is None:
            return raw
        if record.section == "settings":
            value = load_editable_yaml(text)
            if not isinstance(value, dict) or set(value) != {"value"}:
                raise TransferError(
                    "A setting editor must contain exactly one value field."
                )
            raw["value"] = value["value"]
        elif record.section == "schedules":
            original = raw["schedule"]["markdown"]
            document = edited_schedule(original, text)
            if document["repeat_count"] != parse_schedule(original)["repeat_count"]:
                raw["schedule"]["remaining_count"] = document["repeat_count"]
            raw["schedule"]["markdown"] = render_schedule(document)
        elif record.section == "pinned_memory":
            raw["document"]["fact_text"] = text
        return raw

    def snapshot(self) -> "ReviewFiles":
        """Digest and compilation use the same bytes, including during editor saves."""
        snapshot = ReviewFiles(
            self.reader, self.root, self.review.model_copy(deep=True), self.archive
        )
        snapshot._texts = {
            record.file: self.text(record)
            for record in snapshot.review.records
            if record.file
        }
        return snapshot

    def choices(self, *, require_confirmation: bool):
        selected: dict[str, set[str]] = {}
        overrides: dict[str, dict[str, dict[str, Any]]] = {}
        for record in self.review.records:
            if not record.selected:
                continue
            selected.setdefault(record.section, set()).add(record.key)
            if record.section == "schedules" and record.file and require_confirmation:
                if record.confirmed_digest != text_digest(self.text(record)):
                    raise TransferError(
                        f"Confirm schedule {record.key} after its last edit before importing."
                    )
            if record.section != "broker_credentials":
                overrides.setdefault(record.section, {})[record.key] = (
                    self.edited_record(record)
                )
        return selected, overrides

    def digest(self) -> str:
        value = {
            "review": self.review.model_dump(mode="json"),
            "edits": {
                record.file: self.text(record)
                for record in self.review.records
                if record.file
            },
        }
        return text_digest(json.dumps(value, sort_keys=True, ensure_ascii=False))

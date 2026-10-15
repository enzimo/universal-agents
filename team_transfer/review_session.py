"""One reviewed import: select, edit, validate and import records of one backup.

Two pages serve the same review: the operator's loopback page
(``team_transfer.review_web``, ``assistant_cli team review``) and onboarding's
Import (``assistant_gateway.onboarding_web.team_import``). Each page checks
who may call it; this class holds what they share, so both apply the same
rules:

- edits and selections are saved to the review folder (``ReviewFiles``)
  under one lock;
- validation plans the exact saved review and remembers its digest only when
  nothing is refused;
- import writes only the review it validated, unchanged since (the digest
  must match the saved files again), so an edit after validation needs a new
  validation;
- the loopback page sends credentials to the running broker through
  ``store_credentials`` after the instance import. Onboarding instead leaves
  them for the first start through ``prepare_credentials``, before the
  instance's unfinished-import marker can be removed.

The page never receives secret values: operator secrets show their names and
credentials their key and owner only (``_public_record``).
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from pydantic import Field

from access_control import safe_open
from team_transfer.errors import TransferError
from team_transfer.format import TransferModel
from team_transfer.review import ReviewFiles, ReviewRecordV1, text_digest
from team_transfer.sections import DestinationChoices, PlannedRecord, RecordOutcome
from team_transfer.service import default_sections, import_team, plan_import
from team_transfer.titles import record_title, section_title

CREDENTIAL_SECTION = "broker_credentials"
# Prepares or stores selected credentials; returns one
# outcome per key: {"key", "imported", "reason"}.
CredentialStep = Callable[[ReviewFiles, Any, list[str]], list[dict[str, Any]]]


class ReviewUpdateV1(TransferModel):
    section: str
    key: str
    selected: bool
    text: str | None = Field(default=None, max_length=4 * 1024**2)
    confirmed: bool = False


class ReviewApplyV1(TransferModel):
    digest: str


def store_credentials_now(files: ReviewFiles, layout: Any, keys: list[str]) -> list[dict[str, Any]]:
    """The loopback review's step: the destination's broker stores them now."""

    from team_transfer.broker_credentials import import_credentials

    try:
        report = import_credentials(
            files.reader, str(layout.instance_config_path), selected_keys=keys
        )
    except TransferError as exc:
        return [{"key": key, "imported": False, "reason": str(exc)} for key in keys]
    return report.outcomes


class ReviewSession:
    def __init__(
        self,
        files: ReviewFiles,
        *,
        store_credentials: CredentialStep = store_credentials_now,
        prepare_credentials: CredentialStep | None = None,
        destination: DestinationChoices | None = None,
    ) -> None:
        self.files = files
        self._store_credentials = store_credentials
        self._prepare_credentials = prepare_credentials
        self._destination = destination
        self._validated_digest: str | None = None
        self._gate = asyncio.Lock()

    # -- reading -----------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        files = self.files
        order = [section.name for section in default_sections()]
        present = {record.section for record in files.review.records}
        return {
            "team_id": files.reader.manifest.team_id,
            "target_root": files.review.target_root,
            "review_directory": str(files.root),
            "sections": [
                {"name": name, "title": section_title(name)}
                for name in order
                if name in present
            ],
            "records": [_public_record(files, record) for record in files.review.records],
        }

    @property
    def destination(self) -> DestinationChoices | None:
        return self._destination

    async def set_destination(self, destination: DestinationChoices) -> None:
        """A changed destination changes the plan, so it needs a new validation."""

        async with self._gate:
            self._destination = destination
            self._validated_digest = None

    # -- changing the review -------------------------------------------------------

    async def update(self, payload: ReviewUpdateV1) -> dict[str, Any]:
        files = self.files
        async with self._gate:
            records = list(files.review.records)
            index = next(
                (
                    i
                    for i, r in enumerate(records)
                    if (r.section, r.key) == (payload.section, payload.key)
                ),
                None,
            )
            if index is None:
                raise TransferError("This record is absent from the review.")
            old = records[index]
            if payload.confirmed and old.section == "schedules" and old.file and payload.text is None:
                raise TransferError("Confirm the exact schedule text you reviewed.")
            if payload.text is not None:
                if old.file is None:
                    raise TransferError("This record cannot be edited in the review.")
                safe_open.write_bytes(
                    files.root, tuple(old.file.split("/")), payload.text.encode("utf-8")
                )
            confirmed = (
                text_digest(payload.text)
                if payload.confirmed and old.file and old.section == "schedules" and payload.text is not None
                else None
            )
            records[index] = ReviewRecordV1(
                section=old.section,
                key=old.key,
                selected=payload.selected,
                file=old.file,
                confirmed_digest=confirmed,
            )
            files.save(files.review.model_copy(update={"records": records}))
            self._validated_digest = None
        return {"saved": True}

    # -- validating and importing ----------------------------------------------------

    def _arguments(self, files: ReviewFiles) -> dict[str, Any]:
        choices, overrides = files.choices(require_confirmation=True)
        return dict(
            choices=choices,
            overrides=overrides,
            allow_existing=True,
            destination=self._destination,
        )

    async def validate(self) -> dict[str, Any]:
        files = self.files
        async with self._gate:
            snapshot = files.snapshot()
            arguments = self._arguments(snapshot)
            plan, context = await asyncio.to_thread(
                plan_import, files.reader, files.review.target_root, **arguments
            )
            plan.records.extend(_credential_plan(files, context, arguments["choices"]))
            refused = bool(plan.by_outcome(RecordOutcome.REFUSED))
            self._validated_digest = None if refused else snapshot.digest()
            archive = files.archive
            records = [
                {
                    "section": r.section,
                    "key": r.key,
                    "title": _title(archive, r.section, r.key),
                    "outcome": r.outcome.value,
                    "reason": r.reason,
                }
                for r in plan.records
            ]
        return {
            "records": records,
            "skipped": len(plan.skipped_records),
            "digest": self._validated_digest,
            "valid": not refused,
            "unknown_sections": plan.unknown_sections,
        }

    async def apply(self, payload: ReviewApplyV1) -> dict[str, Any]:
        files = self.files
        async with self._gate:
            snapshot = files.snapshot()
            if (
                not self._validated_digest
                or payload.digest != self._validated_digest
                or snapshot.digest() != self._validated_digest
            ):
                raise TransferError(
                    "The review changed. Validate the selected records again before importing."
                )
            arguments = self._arguments(snapshot)
            from runtime_settings.instance_layout import InstanceLayout
            from team_transfer.batches import previous_records
            from team_transfer.sections import ImportContext

            layout = InstanceLayout.at(files.review.target_root)
            previous = (
                previous_records(ImportContext(files.reader, layout.root, layout, {}))
                if layout.instance_config_path.is_file()
                else {}
            )
            pending = {
                section: keys - previous.get(section, set())
                for section, keys in arguments["choices"].items()
                if section != CREDENTIAL_SECTION
            }
            credential_keys = sorted(
                arguments["choices"].get(CREDENTIAL_SECTION, set())
                - previous.get(CREDENTIAL_SECTION, set())
            )
            outcomes: list[dict[str, Any]] = []

            def prepare(context: ImportContext) -> None:
                nonlocal outcomes
                if self._prepare_credentials is not None and credential_keys:
                    outcomes = self._prepare_credentials(snapshot, context.layout, credential_keys)

            plan = None
            if not layout.instance_config_path.is_file() or any(pending.values()):
                plan = await asyncio.to_thread(
                    import_team,
                    files.reader,
                    layout.root,
                    dry_run=False,
                    require_valid=True,
                    before_finish=prepare if self._prepare_credentials is not None else None,
                    **arguments,
                )
            if credential_keys and self._prepare_credentials is None:
                outcomes = await asyncio.to_thread(
                    self._store_credentials, snapshot, layout, credential_keys
                )
            self._validated_digest = None
        return {
            "imported": len(plan.records) if plan else 0,
            "credentials": outcomes,
        }


def _title(archive: dict[str, dict[str, Any]], section: str, key: str) -> str:
    raw = archive.get(section, {}).get(key)
    return record_title(section, key, raw, archive) if raw is not None else key


def _public_record(files: ReviewFiles, record: ReviewRecordV1) -> dict[str, Any]:
    raw = files.archive[record.section][record.key]
    details = (
        {"kind": raw["kind"], "name": raw["name"]}
        if record.section == "operator_secrets"
        else raw
    )
    if record.section == CREDENTIAL_SECTION:
        details = {key: raw[key] for key in ("key", "owner") if key in raw}
    elif record.section == "control" and raw.get("kind") == "llm_overrides":
        details = {
            "model_settings": {
                entry["path"]: files.reader.read_member(
                    "files/control/" + entry["path"]
                ).decode("utf-8")
                for entry in raw["files"]
            },
        }
    remaining = None
    if record.section == "schedules" and raw.get("kind") == "schedule":
        # Show saved progress even though the Markdown edits configuration only.
        remaining = raw["schedule"]["remaining_count"]
    return {
        **record.model_dump(mode="json"),
        "title": record_title(record.section, record.key, raw, files.archive),
        "section_title": section_title(record.section),
        "text": files.text(record),
        "details": details,
        "remaining_count": remaining,
    }


def _credential_plan(
    files: ReviewFiles, context: Any, choices: dict[str, set[str]]
) -> list[PlannedRecord]:
    from team_transfer.batches import previous_records

    previous = (
        previous_records(context).get(CREDENTIAL_SECTION, set())
        if context.existing
        else set()
    )
    selected = choices.get(CREDENTIAL_SECTION, set()) - previous
    planned = []
    for key in sorted(selected):
        owner = files.archive[CREDENTIAL_SECTION][key]["owner"]
        reason = "stored after startup by the destination broker; secret values stay encrypted during this review"
        outcome = RecordOutcome.ATTENTION
        if owner["kind"] == "person" and owner["owner_id"] not in context.accepted(
            "people"
        ):
            reason = f"it belongs to {owner['owner_id']}, who is not selected or present in the target"
            outcome = RecordOutcome.REFUSED
        planned.append(PlannedRecord(CREDENTIAL_SECTION, key, outcome, reason))
    return planned


__all__ = [
    "CredentialStep",
    "ReviewApplyV1",
    "ReviewSession",
    "ReviewUpdateV1",
    "store_credentials_now",
]

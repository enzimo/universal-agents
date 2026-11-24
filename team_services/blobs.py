"""Team-services blob index, retention, immediate departure deletion, and orphan sweeping.

Owns ``blobs.register`` and ``blobs.delete_person`` on ``svc.team`` plus startup recovery
and periodic maintenance for the team's objects in ``OBJ_UA1_BLOBS``:

- **CR-3 serialization:** One ``asyncio.Lock`` inside the single lock-holding
  ``team_services`` process serializes ownership registration against last-owner deletion,
  pending-delete retries, and unindexed metadata cleanup, while acquiring the lock per
  blob during batch sweeps so ``svc.team`` stays responsive (C07).
- **C01 startup and nuid safety:** ``prepare()`` retries durable
  ``protocol1_blob_pending_deletes`` before ``svc.team`` subscribes. Each pending delete
  records ``target_nuid`` before deleting so retries and concurrent registrations purge
  the exact departed/deleted chunk subject without touching newer live metadata under a
  fresh ``nuid``. Unindexed live metadata cleanup re-checks ``not has_owners`` under the
  lock and guards ``delete_blob`` with ``expected_seq`` and ``expected_nuid`` CAS.
- **C04 departed-person upload race:** If ``blobs.register`` refuses a disabled or
  removed person, it immediately removes any unindexed live metadata for
  ``{team}~{sha256}`` carrying that person's ``UA-Blob-Person`` header.
- **FA-6 sweep:** ``maintain()`` retries pending deletes, runs a safety-net pass for any
  departed person still indexed, expires owners past ``agent.blob_retention_days``, and
  runs ``sweep_orphan_chunks`` (which also sweeps chunks named only by ``deleted: true``
  markers).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Mapping, Optional

from access_control.people import is_administrator
from access_control.people_directory import PeopleDirectory, PersonNow
from custom_messages.contracts.access_v2 import PersonStanding
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame
from custom_transport.protocol1.blobs import (
    STREAM_NAME,
    BlobConcurrentWrite,
    BlobCorrupted,
    BlobError,
    BlobMetadata,
    BlobMetadataEntry,
    BlobOwner,
    check_chunks_present,
    default_clock_skew_seconds,
    default_orphan_grace_seconds,
    delete_blob,
    get_blob,
    purge_blob_chunks,
    purge_blob_metadata_keep_last,
    put_blob,
    sha256_unpadded,
    stat_blob,
    sweep_orphan_chunks,
    validate_sha256,
    validate_team_id,
)
from default_values import AGENT_DEFAULTS
from storage.blob_index_repository import BlobIndexRepository, BlobSizeMismatch, PendingBlobDelete
from team_services.people import GATEWAY_ROLE, command_person
from team_services.service import OperationFamily

LEAD_HOST_ROLE = "lead_host"
BLOB_KINDS = ("blobs.register", "blobs.delete_person")
SIDE_BY_SIDE_KINDS = frozenset(BLOB_KINDS)
MAX_PUT_REGISTER_ATTEMPTS = 3
_DEPARTED_STANDINGS = frozenset({PersonStanding.DISABLED, PersonStanding.REMOVED})
_SHORT_TEXT_MAX = 500


def _short_message(text: str) -> str:
    cleaned = text.strip() or "Blob operation failed."
    if len(cleaned) <= _SHORT_TEXT_MAX:
        return cleaned
    return cleaned[: _SHORT_TEXT_MAX - 3] + "..."


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class RegisterResult:
    """Outcome of ``BlobService.register``."""

    outcome: Literal["ok", "not_allowed", "runtime_error"]
    present: bool = False
    expires_at: Optional[datetime] = None
    message: Optional[str] = None


@dataclass(frozen=True)
class PersonDeleteResult:
    """Outcome of ``BlobService.delete_person``."""

    outcome: Literal["ok", "not_allowed", "runtime_error"]
    owners_removed: int = 0
    blobs_deleted: int = 0
    blobs_kept: int = 0
    message: Optional[str] = None


@dataclass(frozen=True)
class BlobMaintenanceReport:
    """Summary of one ``BlobService.maintain`` run."""

    pending_deletes_completed: int
    departed_people_cleaned: int
    expired_owners_removed: int
    expired_blobs_deleted: int
    orphan_chunks_purged: int
    unindexed_blobs_deleted: int


class BlobService:
    """Coordinates the persistent blob ownership index and ``OBJ_UA1_BLOBS`` mutations."""

    def __init__(
        self,
        *,
        repository: BlobIndexRepository,
        people: PeopleDirectory,
        js: Any,
        team_id: str,
        retention_days: int = AGENT_DEFAULTS.blob_retention_days.value,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        logger: Optional[logging.Logger] = None,
        orphan_grace_seconds: Optional[int] = None,
        clock_skew_seconds: Optional[int] = None,
    ) -> None:
        if retention_days < 1:
            raise ValueError(f"blob_retention_days must be at least 1, got {retention_days}")
        self._repository = repository
        self._people = people
        self._js = js
        self._team_id = validate_team_id(team_id)
        self._retention_days = int(retention_days)
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._orphan_grace_seconds = (
            default_orphan_grace_seconds() if orphan_grace_seconds is None else int(orphan_grace_seconds)
        )
        self._clock_skew_seconds = (
            default_clock_skew_seconds() if clock_skew_seconds is None else int(clock_skew_seconds)
        )
        self._lock = asyncio.Lock()

    @property
    def team_id(self) -> str:
        return self._team_id

    @property
    def retention_days(self) -> int:
        return self._retention_days

    def close(self) -> None:
        self._repository.close()

    async def prepare(self) -> None:
        """Finish pending deletes before ``svc.team`` subscribes to ``blobs.register`` (C01)."""

        await self.retry_pending_deletes()

    # -- registration & in-process put/get ---------------------------------------------------

    async def register(self, *, sha256: str, size: int, owner: BlobOwner) -> RegisterResult:
        """Verify standing, check live presence under ``_lock``, and commit ownership if live."""

        try:
            validated_sha = validate_sha256(sha256)
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError(f"size must be a non-negative integer, got {size!r}")
        except ValueError as exc:
            return RegisterResult(outcome="not_allowed", message=_short_message(str(exc)))

        async with self._lock:
            if owner.person_ref is not None:
                refusal = await self._check_active_person_locked(validated_sha, owner.person_ref)
                if refusal is not None:
                    return refusal

            pending = await asyncio.to_thread(self._repository.get_pending_delete, self._team_id, validated_sha)
            meta = await self._safe_stat(validated_sha)

            if pending is not None:
                settled = await self._reconcile_pending_during_register_locked(validated_sha, pending, meta)
                if settled is not None:
                    return settled
                # Pending delete had an older target_nuid already purged (or was expired/reusable)
                # and live metadata under a valid nuid remains; proceed to check and register it.

            if meta is None or meta.deleted:
                return RegisterResult(outcome="ok", present=False)

            if (
                meta.person_ref is not None
                and not await asyncio.to_thread(self._repository.has_owners, self._team_id, validated_sha)
                and await self._is_person_departed(meta.person_ref)
            ):
                try:
                    await delete_blob(
                        self._js,
                        self._team_id,
                        validated_sha,
                        expected_seq=meta.seq,
                        expected_nuid=meta.nuid,
                    )
                except BlobConcurrentWrite:
                    pass
                return RegisterResult(outcome="ok", present=False)

            if meta.size != size or not await check_chunks_present(self._js, meta):
                if pending is not None:
                    await asyncio.to_thread(
                        self._repository.complete_pending_delete,
                        self._team_id,
                        validated_sha,
                        expected_nuid=pending.target_nuid,
                    )
                return RegisterResult(outcome="ok", present=False)

            now = _utc(self._clock())
            expires_at = now + timedelta(days=self._retention_days)
            try:
                record = await asyncio.to_thread(
                    self._repository.register_owner,
                    team_id=self._team_id,
                    sha256=validated_sha,
                    owner_kind=owner.owner_kind,
                    owner_id=owner.owner_id,
                    person_ref=owner.person_ref,
                    size_bytes=size,
                    registered_at_utc=now,
                    expires_at_utc=expires_at,
                    clear_pending_delete=True,
                )
            except BlobSizeMismatch as exc:
                self._logger.warning(
                    "Refused blob registration with conflicting byte size. "
                    "event=blob_register_size_mismatch team=%s sha256=%s error=%s",
                    self._team_id,
                    validated_sha,
                    exc,
                )
                return RegisterResult(outcome="not_allowed", message=_short_message(str(exc)))
            return RegisterResult(outcome="ok", present=True, expires_at=record.expires_at_utc)

    async def put_and_register(
        self,
        data: bytes,
        owner: BlobOwner,
        *,
        media_type: str,
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        """In-process upload + CR-3 registration helper for ``team_services`` (e.g., Knowledge replies).

        Registers first (C04) so dedup hits take one lock pass and departed people are refused
        before uploading bytes. Chunk upload happens outside ``_lock``; retries on CR-3 races.
        """

        payload = bytes(data)
        sha256 = sha256_unpadded(payload)
        size = len(payload)

        for _attempt in range(MAX_PUT_REGISTER_ATTEMPTS):
            result = await self.register(sha256=sha256, size=size, owner=owner)
            if result.outcome == "not_allowed":
                raise PermissionError(result.message or "Blob registration was not allowed.")
            if result.outcome != "ok":
                raise BlobError(result.message or "Blob registration failed.")
            if result.present:
                ref: dict[str, Any] = {
                    "sha256": sha256,
                    "size": size,
                    "media_type": media_type,
                }
                if name is not None:
                    ref["name"] = name
                return ref
            await put_blob(self._js, self._team_id, payload, owner)

        raise BlobConcurrentWrite(
            f"Blob {self._team_id}~{sha256} could not be registered after {MAX_PUT_REGISTER_ATTEMPTS} attempts."
        )

    async def get(self, ref: Mapping[str, Any], *, max_bytes: Optional[int] = None) -> bytes:
        """Read and verify a blob referenced by a protocol 1 ``blob_ref`` mapping."""

        return await get_blob(
            self._js,
            self._team_id,
            str(ref["sha256"]),
            expected_size=int(ref["size"]),
            max_bytes=max_bytes,
        )

    # -- immediate person-departure deletion -------------------------------------------------

    async def delete_person(self, person_ref: str) -> PersonDeleteResult:
        """Remove a departed person's ownership rows and immediately delete unowned blobs (A4)."""

        person = await asyncio.to_thread(self._people.person_by_ref, person_ref)
        if person is None:
            return PersonDeleteResult(
                outcome="not_allowed",
                message="The person reference is not known in this team.",
            )
        if person.standing not in _DEPARTED_STANDINGS:
            self._logger.warning(
                "Refused blob deletion for a person who has not departed. "
                "event=blob_delete_person_active_refused team=%s person_id=%s standing=%s",
                self._team_id,
                person.person_id,
                person.describe(),
            )
            return PersonDeleteResult(
                outcome="not_allowed",
                message=f"Cannot delete blobs for a person whose standing is {person.describe()}.",
            )

        now = _utc(self._clock())
        async with self._lock:
            removal = await asyncio.to_thread(
                self._repository.remove_person_owners,
                self._team_id,
                person_ref,
                now,
            )
            prior_pending = await asyncio.to_thread(
                self._repository.list_pending_deletes,
                self._team_id,
                person_ref=person_ref,
            )

        # Combine newly unowned blobs and any prior failed departure deletes for idempotent retries.
        targets = sorted(set(removal.unowned_sha256s) | {item.sha256 for item in prior_pending})
        blobs_deleted = 0
        failed = 0
        for sha256 in targets:
            try:
                async with self._lock:
                    if await self._execute_pending_delete_locked(sha256):
                        blobs_deleted += 1
            except Exception:
                failed += 1
                self._logger.exception(
                    "Could not delete a departed person's blob; kept queued for retry. "
                    "event=blob_person_delete_failed team=%s person_id=%s sha256=%s",
                    self._team_id,
                    person.person_id,
                    sha256,
                )

        if failed:
            return PersonDeleteResult(
                outcome="runtime_error",
                owners_removed=removal.owners_removed,
                blobs_deleted=blobs_deleted,
                blobs_kept=len(removal.kept_sha256s),
                message=_short_message(
                    f"{failed} blob(s) could not be deleted from {STREAM_NAME} and remain queued for retry."
                ),
            )

        self._logger.info(
            "Processed departed person's blob ownership removal. "
            "event=blob_person_deleted team=%s person_id=%s owners_removed=%d blobs_deleted=%d blobs_kept=%d",
            self._team_id,
            person.person_id,
            removal.owners_removed,
            blobs_deleted,
            len(removal.kept_sha256s),
        )
        return PersonDeleteResult(
            outcome="ok",
            owners_removed=removal.owners_removed,
            blobs_deleted=blobs_deleted,
            blobs_kept=len(removal.kept_sha256s),
        )

    # -- retention expiry, pending-delete recovery, and FA-6 sweep ---------------------------

    async def retry_pending_deletes(self) -> int:
        """Retry every queued pending delete, acquiring ``_lock`` per blob (C01, C07)."""

        pending = await asyncio.to_thread(self._repository.list_pending_deletes, self._team_id)
        completed = 0
        for item in pending:
            try:
                async with self._lock:
                    if await self._execute_pending_delete_locked(item.sha256):
                        completed += 1
            except Exception:
                self._logger.exception(
                    "Could not finish a pending blob delete; keeping it queued for retry. "
                    "event=blob_pending_delete_failed team=%s sha256=%s target_nuid=%s reason=%s",
                    self._team_id,
                    item.sha256,
                    item.target_nuid,
                    item.reason,
                )
        return completed

    async def expire_blobs(self) -> tuple[int, int]:
        """Remove owners past ``expires_at_utc`` and delete blobs left with no owners (C07 per-blob lock)."""

        now = _utc(self._clock())
        async with self._lock:
            owners_removed, unowned = await asyncio.to_thread(
                self._repository.remove_expired_owners,
                self._team_id,
                now,
            )

        deleted = 0
        for sha256 in unowned:
            try:
                async with self._lock:
                    if await self._execute_pending_delete_locked(sha256):
                        deleted += 1
            except Exception:
                self._logger.exception(
                    "Could not delete an expired blob; kept queued for retry. "
                    "event=blob_expire_delete_failed team=%s sha256=%s",
                    self._team_id,
                    sha256,
                )

        if owners_removed or deleted:
            self._logger.info(
                "Expired blob ownership records. event=blob_retention_expired "
                "team=%s owners_removed=%d blobs_deleted=%d",
                self._team_id,
                owners_removed,
                deleted,
            )
        return owners_removed, deleted

    async def maintain(self) -> BlobMaintenanceReport:
        """Run startup/periodic maintenance: pending deletes, departed safety net, expiry, and FA-6 sweep."""

        pending_completed = await self.retry_pending_deletes()

        # Safety net: catch any departed person whose departure happened while team_services was down.
        departed_cleaned = 0
        indexed_refs = await asyncio.to_thread(self._repository.list_indexed_person_refs, self._team_id)
        for ref in indexed_refs:
            person = await asyncio.to_thread(self._people.person_by_ref, ref)
            if person is not None and person.standing in _DEPARTED_STANDINGS:
                res = await self.delete_person(ref)
                if res.outcome == "ok":
                    departed_cleaned += 1

        expired_owners, expired_deleted = await self.expire_blobs()

        now = _utc(self._clock())
        sweep = await sweep_orphan_chunks(
            self._js,
            now=now,
            grace_seconds=self._orphan_grace_seconds,
            clock_skew_seconds=self._clock_skew_seconds,
        )
        unindexed_deleted = await self._cleanup_unindexed_metadata(sweep.metadata_entries, now=now)

        return BlobMaintenanceReport(
            pending_deletes_completed=pending_completed,
            departed_people_cleaned=departed_cleaned,
            expired_owners_removed=expired_owners,
            expired_blobs_deleted=expired_deleted,
            orphan_chunks_purged=len(sweep.purged_chunk_subjects),
            unindexed_blobs_deleted=unindexed_deleted,
        )

    # -- internal locked helpers -------------------------------------------------------------

    async def _safe_stat(self, sha256: str) -> Optional[BlobMetadata]:
        try:
            return await stat_blob(self._js, self._team_id, sha256)
        except BlobCorrupted as exc:
            self._logger.warning(
                "Stored blob metadata failed validation. event=blob_metadata_corrupted "
                "team=%s sha256=%s error=%s",
                self._team_id,
                sha256,
                exc,
            )
            return None

    async def _is_person_departed(self, person_ref: str) -> bool:
        person = await asyncio.to_thread(self._people.person_by_ref, person_ref)
        return person is not None and person.standing in _DEPARTED_STANDINGS

    async def _check_active_person_locked(self, sha256: str, person_ref: str) -> Optional[RegisterResult]:
        person: Optional[PersonNow] = await asyncio.to_thread(self._people.person_by_ref, person_ref)
        if person is None:
            return RegisterResult(
                outcome="not_allowed",
                message="The person reference is not known in this team.",
            )
        if person.active:
            return None

        # C04: If a departed person raced an upload before registration was refused, delete any
        # unindexed live metadata carrying their UA-Blob-Person header immediately.
        if not await asyncio.to_thread(self._repository.has_owners, self._team_id, sha256):
            meta = await self._safe_stat(sha256)
            if meta is not None and not meta.deleted and meta.person_ref == person_ref:
                try:
                    await delete_blob(
                        self._js,
                        self._team_id,
                        sha256,
                        expected_seq=meta.seq,
                        expected_nuid=meta.nuid,
                    )
                    self._logger.info(
                        "Deleted unindexed blob uploaded by a departed person during refused registration. "
                        "event=blob_departed_raced_upload_deleted team=%s person_id=%s sha256=%s nuid=%s",
                        self._team_id,
                        person.person_id,
                        sha256,
                        meta.nuid,
                    )
                except BlobConcurrentWrite:
                    pass
                except Exception:
                    self._logger.exception(
                        "Could not delete unindexed blob uploaded by a departed person; upkeep will sweep it. "
                        "event=blob_departed_raced_upload_delete_failed team=%s person_id=%s sha256=%s",
                        self._team_id,
                        person.person_id,
                        sha256,
                    )
        return RegisterResult(
            outcome="not_allowed",
            message=f"The person is {person.describe()} in this team.",
        )

    async def _reconcile_pending_during_register_locked(
        self,
        sha256: str,
        pending: PendingBlobDelete,
        meta: Optional[BlobMetadata],
    ) -> Optional[RegisterResult]:
        """Reconcile an existing pending delete before evaluating ``meta`` for registration (C01).

        Returns a ``RegisterResult`` if registration should return immediately (``present=False``),
        or ``None`` if live metadata may proceed to normal validation and owner registration.
        """

        if pending.target_nuid is not None:
            if meta is None:
                await purge_blob_chunks(self._js, pending.target_nuid)
                await asyncio.to_thread(
                    self._repository.complete_pending_delete,
                    self._team_id,
                    sha256,
                    expected_nuid=pending.target_nuid,
                )
                return RegisterResult(outcome="ok", present=False)
            if meta.deleted:
                await purge_blob_chunks(self._js, pending.target_nuid)
                if meta.nuid != pending.target_nuid:
                    await purge_blob_chunks(self._js, meta.nuid)
                await purge_blob_metadata_keep_last(self._js, self._team_id, sha256)
                await asyncio.to_thread(
                    self._repository.complete_pending_delete,
                    self._team_id,
                    sha256,
                    expected_nuid=pending.target_nuid,
                )
                return RegisterResult(outcome="ok", present=False)
            if meta.nuid == pending.target_nuid:
                await delete_blob(
                    self._js,
                    self._team_id,
                    sha256,
                    expected_seq=meta.seq,
                    expected_nuid=meta.nuid,
                )
                await asyncio.to_thread(
                    self._repository.complete_pending_delete,
                    self._team_id,
                    sha256,
                    expected_nuid=pending.target_nuid,
                )
                return RegisterResult(outcome="ok", present=False)
            # Newer live metadata exists under a fresh nuid: purge the departed/deleted old nuid's chunks
            # without touching the new live metadata, then let normal validation register the new owner.
            await purge_blob_chunks(self._js, pending.target_nuid)
            return None

        # target_nuid is None (delete was queued but had not yet started against NATS).
        if meta is None:
            await asyncio.to_thread(self._repository.complete_pending_delete, self._team_id, sha256)
            return RegisterResult(outcome="ok", present=False)
        if meta.deleted:
            await purge_blob_chunks(self._js, meta.nuid)
            await purge_blob_metadata_keep_last(self._js, self._team_id, sha256)
            await asyncio.to_thread(self._repository.complete_pending_delete, self._team_id, sha256)
            return RegisterResult(outcome="ok", present=False)
        if pending.reason == "person_departed" and meta.person_ref is not None:
            if await self._is_person_departed(meta.person_ref):
                await asyncio.to_thread(
                    self._repository.set_pending_delete_nuid, self._team_id, sha256, meta.nuid
                )
                await delete_blob(
                    self._js,
                    self._team_id,
                    sha256,
                    expected_seq=meta.seq,
                    expected_nuid=meta.nuid,
                )
                await asyncio.to_thread(
                    self._repository.complete_pending_delete,
                    self._team_id,
                    sha256,
                    expected_nuid=meta.nuid,
                )
                return RegisterResult(outcome="ok", present=False)
        return None

    async def _execute_pending_delete_locked(self, sha256: str) -> bool:
        """Execute one pending delete under ``_lock``, protecting newer live nuids (C01)."""

        pending = await asyncio.to_thread(self._repository.get_pending_delete, self._team_id, sha256)
        if pending is None:
            return False

        # Re-check ownership under _lock: a new owner may have registered while waiting for _lock.
        if await asyncio.to_thread(self._repository.has_owners, self._team_id, sha256):
            if pending.target_nuid is not None:
                meta = await self._safe_stat(sha256)
                if meta is None or meta.deleted or meta.nuid != pending.target_nuid:
                    await purge_blob_chunks(self._js, pending.target_nuid)
            await asyncio.to_thread(
                self._repository.complete_pending_delete,
                self._team_id,
                sha256,
                expected_nuid=pending.target_nuid,
            )
            return False

        meta = await self._safe_stat(sha256)
        if pending.target_nuid is not None:
            return await self._finish_known_nuid_delete_locked(sha256, pending.target_nuid, meta)

        if meta is None:
            await asyncio.to_thread(self._repository.complete_pending_delete, self._team_id, sha256)
            return False

        # Record the exact nuid durably BEFORE publishing the deleted marker so a crash after the
        # marker (or after a concurrent overwrite) still purges this exact nuid without touching newer ones.
        target_nuid = meta.nuid
        await asyncio.to_thread(self._repository.set_pending_delete_nuid, self._team_id, sha256, target_nuid)
        return await self._delete_target_nuid_locked(sha256, target_nuid, meta)

    async def _finish_known_nuid_delete_locked(
        self,
        sha256: str,
        target_nuid: str,
        meta: Optional[BlobMetadata],
    ) -> bool:
        if meta is None or meta.nuid != target_nuid:
            # Either already absent, or a new upload replaced metadata with a fresh nuid:
            # purge only the old target_nuid's chunks and never touch newer live metadata.
            await purge_blob_chunks(self._js, target_nuid)
            await asyncio.to_thread(
                self._repository.complete_pending_delete,
                self._team_id,
                sha256,
                expected_nuid=target_nuid,
            )
            return True
        return await self._delete_target_nuid_locked(sha256, target_nuid, meta)

    async def _delete_target_nuid_locked(
        self,
        sha256: str,
        target_nuid: str,
        meta: BlobMetadata,
    ) -> bool:
        try:
            result = await delete_blob(
                self._js,
                self._team_id,
                sha256,
                expected_seq=meta.seq,
                expected_nuid=target_nuid,
            )
        except BlobConcurrentWrite:
            # Another writer moved the metadata subject after we read meta.seq. Every new upload uses
            # a fresh nuid, so re-read and purge target_nuid's chunks if it is no longer live.
            latest = await self._safe_stat(sha256)
            if latest is None or latest.deleted or latest.nuid != target_nuid:
                await purge_blob_chunks(self._js, target_nuid)
                if latest is not None and latest.deleted and latest.nuid == target_nuid:
                    await purge_blob_metadata_keep_last(self._js, self._team_id, sha256)
                await asyncio.to_thread(
                    self._repository.complete_pending_delete,
                    self._team_id,
                    sha256,
                    expected_nuid=target_nuid,
                )
                return True
            raise

        await asyncio.to_thread(
            self._repository.complete_pending_delete,
            self._team_id,
            sha256,
            expected_nuid=target_nuid,
        )
        return result.deleted

    async def _cleanup_unindexed_metadata(
        self,
        entries: tuple[BlobMetadataEntry, ...],
        *,
        now: datetime,
    ) -> int:
        """Delete unindexed live metadata for this team when old (> grace+skew) or owned by a departed person.

        Guarded per C01: re-checks ``not has_owners`` under ``_lock`` and passes ``expected_seq``
        and ``expected_nuid`` CAS to ``delete_blob`` so a newly registered blob is never deleted.
        """

        cutoff = now - timedelta(seconds=self._orphan_grace_seconds + self._clock_skew_seconds)
        deleted = 0
        for entry in entries:
            if (
                entry.team != self._team_id
                or entry.sha256 is None
                or entry.metadata is None
                or entry.metadata.deleted
            ):
                continue
            departed = (
                entry.metadata.person_ref is not None
                and await self._is_person_departed(entry.metadata.person_ref)
            )
            too_old = entry.timestamp is not None and _utc(entry.timestamp) < cutoff
            if not departed and not too_old:
                continue

            async with self._lock:
                if await asyncio.to_thread(self._repository.has_owners, self._team_id, entry.sha256):
                    continue
                try:
                    res = await delete_blob(
                        self._js,
                        self._team_id,
                        entry.sha256,
                        expected_seq=entry.seq,
                        expected_nuid=entry.metadata.nuid,
                    )
                    if res.deleted:
                        deleted += 1
                except BlobConcurrentWrite:
                    continue
                except Exception:
                    self._logger.exception(
                        "Could not delete unindexed live blob during maintenance. "
                        "event=blob_unindexed_cleanup_failed team=%s sha256=%s seq=%d nuid=%s",
                        self._team_id,
                        entry.sha256,
                        entry.seq,
                        entry.metadata.nuid,
                    )
        if deleted:
            self._logger.info(
                "Cleaned up unindexed live blob metadata. event=blob_unindexed_metadata_cleaned "
                "team=%s deleted=%d",
                self._team_id,
                deleted,
            )
        return deleted


class BlobOperations:
    """Protocol 1 ``svc.team`` operation family for ``blobs.register`` and ``blobs.delete_person``."""

    def __init__(
        self,
        service: BlobService,
        people: PeopleDirectory,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._service = service
        self._people = people
        self._logger = logger or logging.getLogger(__name__)

    @property
    def service(self) -> BlobService:
        return self._service

    def handlers(self) -> dict[str, Callable[[VerifiedFrame], Any]]:
        return {
            "blobs.register": self.handle_register,
            "blobs.delete_person": self.handle_delete_person,
        }

    def family(self) -> OperationFamily:
        return OperationFamily(
            self.handlers(),
            side_by_side=SIDE_BY_SIDE_KINDS,
            upkeep=self._maintain,
            close=self._service.close,
            prepare=self._service.prepare,
        )

    async def _maintain(self) -> None:
        await self._service.maintain()

    async def handle_register(self, verified: VerifiedFrame) -> dict[str, Any]:
        body = verified.raw["body"]
        raw_owner = body["owner"]
        owner_type = raw_owner["type"]
        person_ref = body.get("person")
        try:
            if owner_type == "request":
                owner = BlobOwner(request_id=str(raw_owner["request_id"]), person_ref=person_ref)
            elif owner_type == "agent":
                owner = BlobOwner(agent_id=str(raw_owner["agent_id"]), person_ref=person_ref)
            else:
                return {"outcome": "not_allowed", "message": f"Unsupported blob owner type: {owner_type!r}."}
        except ValueError as exc:
            return {"outcome": "not_allowed", "message": _short_message(str(exc))}

        try:
            result = await self._service.register(
                sha256=str(body["sha256"]),
                size=int(body["size"]),
                owner=owner,
            )
        except Exception as exc:
            self._logger.exception(
                "Blob registration failed unexpectedly. event=blob_register_failed team=%s sha256=%s",
                self._service.team_id,
                body.get("sha256"),
            )
            return {"outcome": "runtime_error", "message": _short_message(f"Blob registration failed: {exc}")}

        if result.outcome == "ok":
            reply: dict[str, Any] = {"outcome": "ok", "present": result.present}
            if result.present and result.expires_at is not None:
                reply["expires_at"] = format_timestamp(result.expires_at)
            return reply
        return {"outcome": result.outcome, "message": _short_message(result.message or "Blob registration refused.")}

    async def handle_delete_person(self, verified: VerifiedFrame) -> dict[str, Any]:
        certificate = verified.certificate
        if certificate is None:
            return {"outcome": "not_allowed", "message": "Sender certificate is missing."}

        roles = set(certificate.roles)
        if GATEWAY_ROLE in roles:
            check = await asyncio.to_thread(command_person, verified, self._people, self._logger)
            if check.person is None:
                return {"outcome": "not_allowed", "message": _short_message(check.reason)}
            if not is_administrator(check.person.roles):
                self._logger.warning(
                    "Refused blobs.delete_person from a non-administrator. "
                    "event=blob_delete_person_admin_required team=%s person_id=%s",
                    self._service.team_id,
                    check.person.person_id,
                )
                return {
                    "outcome": "not_allowed",
                    "message": "Only an administrator may delete a departed person's blobs.",
                }
        elif LEAD_HOST_ROLE not in roles:
            return {
                "outcome": "not_allowed",
                "message": "Only the gateway or the team's lead host may delete a departed person's blobs.",
            }

        body = verified.raw["body"]
        try:
            result = await self._service.delete_person(str(body["person"]))
        except Exception as exc:
            self._logger.exception(
                "Departed-person blob deletion failed unexpectedly. event=blob_delete_person_failed team=%s",
                self._service.team_id,
            )
            return {"outcome": "runtime_error", "message": _short_message(f"Blob deletion failed: {exc}")}

        if result.outcome == "ok":
            return {
                "outcome": "ok",
                "owners_removed": result.owners_removed,
                "blobs_deleted": result.blobs_deleted,
                "blobs_kept": result.blobs_kept,
            }
        return {"outcome": result.outcome, "message": _short_message(result.message or "Blob deletion refused.")}


__all__ = [
    "BLOB_KINDS",
    "LEAD_HOST_ROLE",
    "MAX_PUT_REGISTER_ATTEMPTS",
    "SIDE_BY_SIDE_KINDS",
    "BlobMaintenanceReport",
    "BlobOperations",
    "BlobService",
    "PersonDeleteResult",
    "RegisterResult",
]

"""Verified protocol 1 client for the ``UA1_BLOBS`` object store (``OBJ_UA1_BLOBS``).

``contracts/messages/v1/README.md`` ("Blobs"): ``OBJ_UA1_BLOBS`` disables rollup
headers (``allow_rollup_hdrs: false``) so a single writer cannot erase the bucket with
``Nats-Rollup: all``, and readers hold no ``$JS.API.STREAM.NAMES`` or consumer-create
grants on it (PR #151 S2). This module implements ADR-20 compatible put, verified get,
delete, and orphan-chunk sweeping directly against ``OBJ_UA1_BLOBS``:

- Objects are named ``{team}~{sha256}`` (unpadded base64url SHA-256); metadata subjects
  are ``$O.UA1_BLOBS.M.<padded base64url(name)>`` and chunk subjects are
  ``$O.UA1_BLOBS.C.<nuid>``.
- ``put_blob`` checks existing metadata and chunk presence (``STREAM.INFO``), cuts bytes
  into ``chunk_bytes`` (131,072) chunks under a fresh ``nuid``, aborts without publishing
  metadata if ``orphan_chunk_grace_seconds / 2`` elapses, and publishes ADR-20 metadata
  with ``Nats-Expected-Last-Subject-Sequence`` compare-and-set (PR #151 FA-7) and no
  ``Nats-Rollup`` header.
- ``get_blob`` reads the last metadata message and reads chunks directly through
  ``$JS.API.STREAM.MSG.GET.OBJ_UA1_BLOBS`` (``next_by_subj``), enforcing size caps before
  and during download and checking exact chunk count, absence of trailing chunks, total
  byte length, and SHA-256 digest.
- ``delete_blob`` writes a ``deleted: true`` marker keeping the owner headers with CAS
  and no ``Nats-Rollup`` header, purges the object's chunks, and purges earlier metadata
  keeping the latest marker.
- ``sweep_orphan_chunks`` checks chunk ages before listing metadata and purges old
  chunks not referenced by live (non-deleted) metadata, including chunks referenced only
  by ``deleted: true`` markers (PR #151 FA-6).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Final, Literal, Mapping, Optional, TypeGuard

from nats.js.api import Header
from nats.js.errors import APIError, NotFoundError
from nats.nuid import NUID
from pydantic import TypeAdapter, ValidationError

from custom_messages.wire.encoding import b64url, b64url_decode
from custom_messages.wire.ids import AGENT_ID, TEAM_ID
from custom_messages.wire.models import PersonRef, RecordId, Sha256Digest
from custom_transport.protocol1.contract_files import transport as contract_transport

_LOGGER = logging.getLogger(__name__)

BUCKET_NAME: Final = "UA1_BLOBS"
STREAM_NAME: Final = f"OBJ_{BUCKET_NAME}"
CHUNK_SUBJECT_PREFIX: Final = f"$O.{BUCKET_NAME}.C."
META_SUBJECT_PREFIX: Final = f"$O.{BUCKET_NAME}.M."
DIGEST_PREFIX: Final = "SHA-256="

HEADER_SHA256: Final = "UA-Blob-SHA256"
HEADER_TEAM: Final = "UA-Blob-Team"
HEADER_REQUEST: Final = "UA-Blob-Request"
HEADER_AGENT: Final = "UA-Blob-Agent"
HEADER_PERSON: Final = "UA-Blob-Person"

JS_ERR_NO_MESSAGE_FOUND: Final = 10037
JS_ERR_STREAM_NOT_FOUND: Final = 10059
JS_ERR_WRONG_LAST_SEQUENCE: Final = 10071
JS_ERR_MAX_BYTES_EXCEEDED: Final = 10077

MAX_CAS_ATTEMPTS: Final = 3
PAGE_ATTEMPTS: Final = 3

_NUID_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256_ADAPTER: Final = TypeAdapter(Sha256Digest)
_RECORD_ID_ADAPTER: Final = TypeAdapter(RecordId)
_PERSON_REF_ADAPTER: Final = TypeAdapter(PersonRef)


def _blob_contract() -> Mapping[str, Any]:
    raw = contract_transport()["object_store"][BUCKET_NAME]
    return dict(raw)


def default_chunk_bytes() -> int:
    """Contract chunk size for ``UA1_BLOBS`` (131,072 bytes)."""

    return int(_blob_contract()["chunk_bytes"])


def default_orphan_grace_seconds() -> int:
    """Contract grace window before unnamed chunks may be swept (86,400 seconds)."""

    return int(_blob_contract()["orphan_chunk_grace_seconds"])


def default_clock_skew_seconds() -> int:
    """Contract clock tolerance added to orphan sweep age checks (8 seconds)."""

    return int(contract_transport()["clock_skew_seconds"])


class BlobError(Exception):
    """Base error for protocol 1 ``UA1_BLOBS`` operations."""


class BlobNotFound(BlobError):
    """The requested blob object does not exist in ``OBJ_UA1_BLOBS``."""


class BlobDeleted(BlobNotFound):
    """The requested blob object has a ``deleted: true`` metadata marker."""


class BlobCorrupted(BlobError):
    """Stored blob metadata or chunk bytes failed integrity verification."""


class BlobTooLarge(BlobError):
    """The blob exceeds the caller's maximum byte limit."""


class BlobWriteTimedOut(BlobError):
    """Writing chunks reached half the orphan-chunk grace window; metadata was not published."""


class BlobStoreFull(BlobError):
    """``OBJ_UA1_BLOBS`` refused a publish because its byte limit was reached (JetStream 10077)."""


class BlobConcurrentWrite(BlobError):
    """A metadata compare-and-set publish failed because another write moved the subject sequence."""


class BlobSubjectListShifted(BlobError):
    """Paginated ``STREAM.INFO`` subject listing shifted across pages on every attempt."""


def validate_team_id(team: str) -> str:
    """Validate a protocol 1 team id for blob naming."""

    if not isinstance(team, str) or not TEAM_ID.fits(team):
        raise ValueError(f"invalid team id for blob name: {team!r}")
    return team


def validate_sha256(sha256: str) -> str:
    """Validate an unpadded canonical base64url SHA-256 digest."""

    if not isinstance(sha256, str):
        raise ValueError(f"invalid blob sha256: {sha256!r}")
    try:
        _SHA256_ADAPTER.validate_python(sha256)
        raw = b64url_decode(sha256)
    except (ValidationError, ValueError) as exc:
        raise ValueError(f"invalid blob sha256: {sha256!r}") from exc
    if len(raw) != 32:
        raise ValueError(f"invalid blob sha256 length: {len(raw)}")
    return sha256


def validate_nuid(nuid: str) -> str:
    """Validate that a blob chunk ``nuid`` is a single safe NATS subject token."""

    if not isinstance(nuid, str) or not _NUID_PATTERN.fullmatch(nuid):
        raise ValueError(f"invalid blob nuid: {nuid!r}")
    return nuid


def sha256_unpadded(data: bytes) -> str:
    """Unpadded base64url SHA-256 digest of ``data``."""

    return b64url(hashlib.sha256(data).digest())


def sha256_padded_digest(unpadded_sha256: str) -> str:
    """ADR-20 ``digest`` field value (``SHA-256=<padded base64url>``) for a validated unpadded digest."""

    validated = validate_sha256(unpadded_sha256)
    padded = validated + "=" * (-len(validated) % 4)
    return f"{DIGEST_PREFIX}{padded}"


def blob_object_name(team: str, sha256: str) -> str:
    """Canonical ``UA1_BLOBS`` object name: ``{team}~{sha256}``."""

    return f"{validate_team_id(team)}~{validate_sha256(sha256)}"


def blob_metadata_subject(name: str) -> str:
    """ADR-20 metadata subject: ``$O.UA1_BLOBS.M.<padded base64url(name)>``."""

    encoded = base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii")
    return f"{META_SUBJECT_PREFIX}{encoded}"


def blob_chunk_subject(nuid: str) -> str:
    """ADR-20 chunk subject: ``$O.UA1_BLOBS.C.<nuid>``."""

    return f"{CHUNK_SUBJECT_PREFIX}{validate_nuid(nuid)}"


def decode_metadata_subject(subject: str) -> Optional[str]:
    """Decode an ADR-20 metadata subject back to its object name, or ``None`` if malformed."""

    if not subject.startswith(META_SUBJECT_PREFIX):
        return None
    token = subject[len(META_SUBJECT_PREFIX):]
    if not token or "." in token or len(token) % 4 != 0:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii"))
        name = decoded.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if blob_metadata_subject(name) != subject:
        return None
    return name


def _validate_record_id(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}: {value!r}")
    try:
        _RECORD_ID_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    return value


def _validate_agent_id(value: str) -> str:
    if not isinstance(value, str) or not AGENT_ID.fits(value):
        raise ValueError(f"invalid blob agent id: {value!r}")
    return value


def _validate_person_ref(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid blob person ref: {value!r}")
    try:
        _PERSON_REF_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"invalid blob person ref: {value!r}") from exc
    return value


@dataclass(frozen=True)
class BlobOwner:
    """Owner headers recorded in ADR-20 metadata when writing a blob.

    Exactly one of ``request_id`` (a person's request) or ``agent_id`` (system work)
    must be set. ``person_ref`` is optional for ``request_id`` work and forbidden for
    ``agent_id`` system work.
    """

    request_id: Optional[str] = None
    agent_id: Optional[str] = None
    person_ref: Optional[str] = None

    def __post_init__(self) -> None:
        has_request = self.request_id is not None
        has_agent = self.agent_id is not None
        if has_request == has_agent:
            raise ValueError("BlobOwner requires exactly one of request_id or agent_id")
        if self.request_id is not None:
            _validate_record_id(self.request_id, "blob request id")
        if self.agent_id is not None:
            _validate_agent_id(self.agent_id)
            if self.person_ref is not None:
                raise ValueError("BlobOwner with agent_id (system work) cannot carry person_ref")
        if self.person_ref is not None:
            _validate_person_ref(self.person_ref)

    @property
    def owner_kind(self) -> Literal["request", "agent"]:
        return "request" if self.request_id is not None else "agent"

    @property
    def owner_id(self) -> str:
        return self.request_id if self.request_id is not None else (self.agent_id or "")

    def to_headers(self, team: str, sha256: str) -> dict[str, list[str]]:
        """Build the ADR-20 ``headers`` mapping (each header holds a one-element list)."""

        headers: dict[str, list[str]] = {
            HEADER_SHA256: [validate_sha256(sha256)],
            HEADER_TEAM: [validate_team_id(team)],
        }
        if self.request_id is not None:
            headers[HEADER_REQUEST] = [self.request_id]
        else:
            headers[HEADER_AGENT] = [self.agent_id or ""]
        if self.person_ref is not None:
            headers[HEADER_PERSON] = [self.person_ref]
        return headers


@dataclass(frozen=True)
class BlobMetadata:
    """Validated ADR-20 object metadata read from ``OBJ_UA1_BLOBS``."""

    name: str
    bucket: str
    nuid: str
    size: int
    chunks: int
    digest: str
    headers: dict[str, list[str]]
    max_chunk_size: int
    deleted: bool = False
    seq: int = 0

    @property
    def team(self) -> str:
        return self.headers[HEADER_TEAM][0]

    @property
    def sha256(self) -> str:
        return self.headers[HEADER_SHA256][0]

    @property
    def person_ref(self) -> Optional[str]:
        values = self.headers.get(HEADER_PERSON)
        return values[0] if values else None

    @property
    def owner_request_id(self) -> Optional[str]:
        values = self.headers.get(HEADER_REQUEST)
        return values[0] if values else None

    @property
    def owner_agent_id(self) -> Optional[str]:
        values = self.headers.get(HEADER_AGENT)
        return values[0] if values else None

    def as_live_payload(self) -> dict[str, Any]:
        """Serialize as the exact ADR-20 JSON payload for a live metadata message."""

        return {
            "name": self.name,
            "bucket": self.bucket,
            "nuid": self.nuid,
            "size": self.size,
            "chunks": self.chunks,
            "digest": self.digest,
            "headers": {k: list(v) for k, v in self.headers.items()},
            "options": {"max_chunk_size": self.max_chunk_size},
        }

    def as_deleted_payload(self) -> dict[str, Any]:
        """Serialize as the ADR-20 ``deleted: true`` marker keeping ``nuid`` and owner headers."""

        return {
            "name": self.name,
            "bucket": self.bucket,
            "nuid": self.nuid,
            "size": 0,
            "chunks": 0,
            "digest": "",
            "deleted": True,
            "headers": {k: list(v) for k, v in self.headers.items()},
            "options": {"max_chunk_size": self.max_chunk_size},
        }


@dataclass(frozen=True)
class BlobPutResult:
    """Outcome of ``put_blob``."""

    sha256: str
    size: int
    chunks: int
    nuid: str
    seq: int
    reused: bool
    metadata: BlobMetadata

    def to_blob_ref(self, *, media_type: str, name: Optional[str] = None) -> dict[str, Any]:
        """Build a protocol 1 ``blob_ref`` dictionary pointing to this blob."""

        ref: dict[str, Any] = {
            "sha256": self.sha256,
            "size": self.size,
            "media_type": media_type,
        }
        if name is not None:
            ref["name"] = name
        return ref


@dataclass(frozen=True)
class BlobDeleteResult:
    """Outcome of ``delete_blob``."""

    deleted: bool
    nuid: Optional[str]
    marker_seq: Optional[int]
    already_deleted: bool


@dataclass(frozen=True)
class BlobMetadataEntry:
    """One metadata subject inspected during ``sweep_orphan_chunks``."""

    subject: str
    seq: int
    timestamp: Optional[datetime]
    team: Optional[str]
    sha256: Optional[str]
    metadata: Optional[BlobMetadata]


@dataclass(frozen=True)
class BlobSweepResult:
    """Outcome of ``sweep_orphan_chunks``."""

    checked_chunk_subjects: int
    old_chunk_subjects: int
    purged_chunk_subjects: tuple[str, ...]
    kept_live_nuids: int
    metadata_entries: tuple[BlobMetadataEntry, ...]


def _is_non_negative_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _extract_single_header(headers: Mapping[str, Any], name: str, *, required: bool) -> Optional[str]:
    if name not in headers:
        if required:
            raise BlobCorrupted(f"Blob metadata is missing required header {name!r}.")
        return None
    raw = headers[name]
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], str):
        raise BlobCorrupted(f"Blob metadata header {name!r} must be a single-element string list.")
    return raw[0]


def _parse_metadata_headers(
    raw_headers: Any,
    *,
    expected_team: str,
    expected_sha256: str,
) -> dict[str, list[str]]:
    if not isinstance(raw_headers, Mapping):
        raise BlobCorrupted("Blob metadata headers must be a JSON object.")

    header_sha = _extract_single_header(raw_headers, HEADER_SHA256, required=True)
    header_team = _extract_single_header(raw_headers, HEADER_TEAM, required=True)
    if header_sha != expected_sha256 or header_team != expected_team:
        raise BlobCorrupted(
            f"Blob metadata headers ({header_team!r}, {header_sha!r}) do not match "
            f"expected ({expected_team!r}, {expected_sha256!r})."
        )

    request_id = _extract_single_header(raw_headers, HEADER_REQUEST, required=False)
    agent_id = _extract_single_header(raw_headers, HEADER_AGENT, required=False)
    person_ref = _extract_single_header(raw_headers, HEADER_PERSON, required=False)

    try:
        owner = BlobOwner(request_id=request_id, agent_id=agent_id, person_ref=person_ref)
    except ValueError as exc:
        raise BlobCorrupted(f"Blob metadata owner headers are invalid: {exc}") from exc

    parsed: dict[str, list[str]] = {}
    for key, val in raw_headers.items():
        if not isinstance(key, str) or not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            raise BlobCorrupted(f"Blob metadata header {key!r} has invalid shape.")
        parsed[key] = list(val)
    # Ensure canonical validated required keys are present.
    parsed.update(owner.to_headers(expected_team, expected_sha256))
    return parsed


def _parse_max_chunk_size(raw_options: Any) -> int:
    if raw_options is None:
        return default_chunk_bytes()
    if not isinstance(raw_options, Mapping):
        raise BlobCorrupted("Blob metadata options must be a JSON object.")
    raw_size = raw_options.get("max_chunk_size")
    if raw_size is None:
        return default_chunk_bytes()
    if not _is_non_negative_int(raw_size) or raw_size == 0:
        raise BlobCorrupted(f"Blob metadata max_chunk_size is invalid: {raw_size!r}")
    return int(raw_size)


def _validate_metadata_state(
    doc: Mapping[str, Any],
    *,
    expected_name: str,
    sha256: str,
) -> tuple[bool, int, int, str]:
    raw_deleted = doc.get("deleted", False)
    if not isinstance(raw_deleted, bool):
        raise BlobCorrupted(f"Blob metadata deleted flag for {expected_name} must be a boolean.")
    size = doc.get("size")
    chunks = doc.get("chunks")
    if not _is_non_negative_int(size) or not _is_non_negative_int(chunks):
        raise BlobCorrupted(f"Blob metadata size/chunks for {expected_name} must be non-negative integers.")
    digest = doc.get("digest")
    if not isinstance(digest, str):
        raise BlobCorrupted(f"Blob metadata digest for {expected_name} must be a string.")
    if raw_deleted:
        if size != 0 or chunks != 0 or digest != "":
            raise BlobCorrupted(
                f"Deleted blob metadata for {expected_name} must have size=0, chunks=0, and empty digest."
            )
        return True, 0, 0, ""
    expected_digest = sha256_padded_digest(sha256)
    if digest != expected_digest:
        raise BlobCorrupted(f"Blob metadata digest {digest!r} does not match expected {expected_digest!r}.")
    if (size == 0) != (chunks == 0):
        raise BlobCorrupted(f"Blob {expected_name} size ({size}) and chunks ({chunks}) are inconsistent.")
    return False, size, chunks, digest


def parse_blob_metadata(
    raw_bytes: bytes,
    *,
    expected_team: str,
    expected_sha256: str,
    seq: int = 0,
) -> BlobMetadata:
    """Parse and strictly validate ADR-20 metadata bytes for ``{expected_team}~{expected_sha256}``."""

    team = validate_team_id(expected_team)
    sha256 = validate_sha256(expected_sha256)
    expected_name = blob_object_name(team, sha256)

    try:
        doc = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlobCorrupted(f"Blob metadata for {expected_name} is not valid UTF-8 JSON.") from exc
    if not isinstance(doc, Mapping):
        raise BlobCorrupted(f"Blob metadata for {expected_name} must be a JSON object.")

    if doc.get("bucket") != BUCKET_NAME:
        raise BlobCorrupted(f"Blob metadata bucket {doc.get('bucket')!r} does not match {BUCKET_NAME!r}.")
    if doc.get("name") != expected_name:
        raise BlobCorrupted(f"Blob metadata name {doc.get('name')!r} does not match {expected_name!r}.")

    try:
        nuid = validate_nuid(doc.get("nuid"))  # type: ignore[arg-type]
    except ValueError as exc:
        raise BlobCorrupted(f"Blob metadata for {expected_name} has invalid nuid.") from exc

    deleted, size, chunks, digest = _validate_metadata_state(doc, expected_name=expected_name, sha256=sha256)
    headers = _parse_metadata_headers(doc.get("headers"), expected_team=team, expected_sha256=sha256)
    max_chunk_size = _parse_max_chunk_size(doc.get("options"))
    return BlobMetadata(
        name=expected_name,
        bucket=BUCKET_NAME,
        nuid=nuid,
        size=size,
        chunks=chunks,
        digest=digest,
        headers=headers,
        max_chunk_size=max_chunk_size,
        deleted=deleted,
        seq=seq,
    )


def _is_no_message_error(exc: NotFoundError) -> bool:
    return exc.err_code in (None, JS_ERR_NO_MESSAGE_FOUND)


def _translate_not_found(exc: NotFoundError, context: str) -> None:
    if exc.err_code == JS_ERR_STREAM_NOT_FOUND:
        raise BlobError(f"Blob store stream {STREAM_NAME} does not exist ({context}).") from exc


async def _read_raw_last_msg(js: Any, subject: str) -> Optional[Any]:
    """Read the last message on ``subject`` in ``OBJ_UA1_BLOBS`` via ``STREAM.MSG.GET``."""

    try:
        return await js.get_last_msg(STREAM_NAME, subject)
    except NotFoundError as exc:
        _translate_not_found(exc, subject)
        if _is_no_message_error(exc):
            return None
        raise


async def stat_blob(js: Any, team: str, sha256: str) -> Optional[BlobMetadata]:
    """Read and validate the latest metadata message for ``{team}~{sha256}``, or ``None`` if absent."""

    name = blob_object_name(team, sha256)
    raw = await _read_raw_last_msg(js, blob_metadata_subject(name))
    if raw is None:
        return None
    return parse_blob_metadata(
        raw.data or b"",
        expected_team=team,
        expected_sha256=sha256,
        seq=int(raw.seq or 0),
    )


async def _subjects_page(js: Any, subject_filter: str, offset: int) -> tuple[dict[str, int], int]:
    """Read one paginated ``STREAM.INFO`` subject page from ``OBJ_UA1_BLOBS``."""

    request = json.dumps({"subjects_filter": subject_filter, "offset": offset}).encode("utf-8")
    try:
        reply = await js._api_request(f"{js._prefix}.STREAM.INFO.{STREAM_NAME}", request, timeout=js._timeout)
    except NotFoundError as exc:
        raise BlobError(f"Blob store stream {STREAM_NAME} does not exist.") from exc
    state = reply.get("state") or {}
    raw_subjects = state.get("subjects") or {}
    subjects = {str(subj): int(count) for subj, count in raw_subjects.items()}
    return subjects, int(reply.get("total") or 0)


async def _stream_subjects_once(js: Any, subject_filter: str) -> Optional[dict[str, int]]:
    """Read all matching subjects page by page; returns ``None`` if two pages failed to join."""

    found: dict[str, int] = {}
    offset = 0
    last: Optional[str] = None
    while True:
        page, total = await _subjects_page(js, subject_filter, offset)
        if last is not None and last not in page:
            return None
        found.update(page)
        if not page or offset + len(page) >= total:
            return found
        last = max(page)
        offset += max(len(page) - 1, 1)


async def list_stream_subjects(js: Any, subject_filter: str) -> dict[str, int]:
    """List every subject and message count in ``OBJ_UA1_BLOBS`` matching ``subject_filter``."""

    for attempt in range(1, PAGE_ATTEMPTS + 1):
        subjects = await _stream_subjects_once(js, subject_filter)
        if subjects is not None:
            return subjects
        _LOGGER.info(
            "Blob stream subject list shifted between pages; retrying. "
            "event=blob_subject_list_shifted filter=%s attempt=%d attempts=%d",
            subject_filter,
            attempt,
            PAGE_ATTEMPTS,
        )
    raise BlobSubjectListShifted(
        f"Listing {STREAM_NAME} subjects matching {subject_filter!r} shifted on all {PAGE_ATTEMPTS} attempts."
    )


async def check_chunks_present(js: Any, metadata: BlobMetadata) -> bool:
    """Confirm via O(1) ``STREAM.INFO`` that ``OBJ_UA1_BLOBS`` holds exactly ``metadata.chunks`` chunks."""

    if metadata.deleted:
        return False
    if metadata.chunks == 0:
        return metadata.size == 0
    chunk_subject = blob_chunk_subject(metadata.nuid)
    subjects, _ = await _subjects_page(js, chunk_subject, 0)
    return int(subjects.get(chunk_subject, 0)) == metadata.chunks


async def _publish_checked(
    js: Any,
    subject: str,
    payload: bytes,
    *,
    headers: Optional[dict[str, str]] = None,
) -> Any:
    """Publish to ``OBJ_UA1_BLOBS`` without rollup headers, translating JetStream 10077 to ``BlobStoreFull``."""

    try:
        return await js.publish(subject, payload, headers=headers)
    except APIError as exc:
        if exc.err_code == JS_ERR_MAX_BYTES_EXCEEDED:
            raise BlobStoreFull(
                f"Blob store {STREAM_NAME} is full and refused publish to {subject}."
            ) from exc
        raise


async def _inspect_existing_for_put(
    js: Any,
    team: str,
    sha256: str,
    expected_size: int,
) -> tuple[int, Optional[BlobMetadata]]:
    """Return ``(expected_last_subject_seq, live_intact_metadata_or_none)`` for ``put_blob``."""

    name = blob_object_name(team, sha256)
    raw = await _read_raw_last_msg(js, blob_metadata_subject(name))
    if raw is None:
        return 0, None

    seq = int(raw.seq or 0)
    try:
        meta = parse_blob_metadata(raw.data or b"", expected_team=team, expected_sha256=sha256, seq=seq)
    except BlobCorrupted as exc:
        _LOGGER.warning(
            "Ignoring corrupt blob metadata on write; replacing via CAS. "
            "event=blob_corrupt_metadata_ignored name=%s seq=%d error=%s",
            name,
            seq,
            exc,
        )
        return seq, None

    if meta.deleted:
        return seq, None
    if meta.size != expected_size or not await check_chunks_present(js, meta):
        _LOGGER.warning(
            "Existing live blob metadata has mismatched size or missing chunks; replacing via CAS. "
            "event=blob_incomplete_chunks_overwritten name=%s seq=%d nuid=%s expected_chunks=%d",
            name,
            seq,
            meta.nuid,
            meta.chunks,
        )
        return seq, None
    return seq, meta


def _check_half_grace(clock: Callable[[], float], started: float, half_grace_seconds: float, name: str) -> None:
    elapsed = clock() - started
    if elapsed >= half_grace_seconds:
        raise BlobWriteTimedOut(
            f"Writing blob {name} exceeded half the orphan-chunk grace window "
            f"({elapsed:.1f}s >= {half_grace_seconds:.1f}s); aborted without publishing metadata."
        )


def _put_result(metadata: BlobMetadata, *, reused: bool) -> BlobPutResult:
    return BlobPutResult(
        sha256=metadata.sha256,
        size=metadata.size,
        chunks=metadata.chunks,
        nuid=metadata.nuid,
        seq=metadata.seq,
        reused=reused,
        metadata=metadata,
    )


def _validate_put_args(
    team: str,
    data: bytes,
    owner: BlobOwner,
    chunk_bytes: Optional[int],
    half_grace_seconds: Optional[float],
) -> tuple[str, bytes, int, float]:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("blob data must be bytes")
    if not isinstance(owner, BlobOwner):
        raise TypeError("owner must be a BlobOwner instance")
    validated_team = validate_team_id(team)
    size_per_chunk = default_chunk_bytes() if chunk_bytes is None else int(chunk_bytes)
    if size_per_chunk <= 0:
        raise ValueError(f"chunk_bytes must be positive, got {size_per_chunk}")
    grace_limit = (
        default_orphan_grace_seconds() / 2.0 if half_grace_seconds is None else float(half_grace_seconds)
    )
    if grace_limit <= 0:
        raise ValueError(f"half_grace_seconds must be positive, got {grace_limit}")
    return validated_team, bytes(data), size_per_chunk, grace_limit


async def _publish_metadata_cas(
    js: Any,
    *,
    metadata: BlobMetadata,
    meta_subject: str,
    expected_seq: int,
    clock: Callable[[], float],
    started: float,
    grace_limit: float,
) -> BlobPutResult:
    meta_bytes = json.dumps(metadata.as_live_payload(), separators=(",", ":")).encode("utf-8")
    current_expected = expected_seq

    for _attempt in range(1, MAX_CAS_ATTEMPTS + 1):
        _check_half_grace(clock, started, grace_limit, metadata.name)
        try:
            ack = await _publish_checked(
                js,
                meta_subject,
                meta_bytes,
                headers={Header.EXPECTED_LAST_SUBJECT_SEQUENCE: str(current_expected)},
            )
            seq = int(getattr(ack, "seq", 0) or 0)
            stored = BlobMetadata(
                name=metadata.name,
                bucket=metadata.bucket,
                nuid=metadata.nuid,
                size=metadata.size,
                chunks=metadata.chunks,
                digest=metadata.digest,
                headers=metadata.headers,
                max_chunk_size=metadata.max_chunk_size,
                deleted=False,
                seq=seq,
            )
            return _put_result(stored, reused=False)
        except APIError as exc:
            if exc.err_code != JS_ERR_WRONG_LAST_SEQUENCE:
                raise
            current_expected, existing = await _inspect_existing_for_put(
                js, metadata.team, metadata.sha256, metadata.size
            )
            if existing is not None:
                return _put_result(existing, reused=True)

    raise BlobConcurrentWrite(
        f"Metadata CAS for {metadata.name} conflicted on all {MAX_CAS_ATTEMPTS} attempts."
    )


async def put_blob(
    js: Any,
    team: str,
    data: bytes,
    owner: BlobOwner,
    *,
    chunk_bytes: Optional[int] = None,
    clock: Callable[[], float] = time.monotonic,
    half_grace_seconds: Optional[float] = None,
    nuid_factory: Optional[Callable[[], str]] = None,
) -> BlobPutResult:
    """Store ``data`` in ``OBJ_UA1_BLOBS`` under ``{team}~{sha256}``, reusing intact live copies.

    Never sends ``Nats-Rollup``, never calls ``$JS.API.STREAM.NAMES``, and never purges
    chunks on abort. Uses ``Nats-Expected-Last-Subject-Sequence`` CAS on metadata writes
    (PR #151 FA-7) and reuses already-uploaded chunks on CAS retries while still within
    half the orphan-chunk grace window.
    """

    validated_team, payload_bytes, size_per_chunk, grace_limit = _validate_put_args(
        team, data, owner, chunk_bytes, half_grace_seconds
    )
    sha256 = sha256_unpadded(payload_bytes)
    name = blob_object_name(validated_team, sha256)
    meta_subject = blob_metadata_subject(name)
    started = clock()

    expected_seq, existing = await _inspect_existing_for_put(js, validated_team, sha256, len(payload_bytes))
    if existing is not None:
        return _put_result(existing, reused=True)

    nuid = validate_nuid(nuid_factory() if nuid_factory is not None else NUID().next().decode("ascii"))
    chunk_subject = blob_chunk_subject(nuid)
    slices = [payload_bytes[i:i + size_per_chunk] for i in range(0, len(payload_bytes), size_per_chunk)]
    for piece in slices:
        _check_half_grace(clock, started, grace_limit, name)
        await _publish_checked(js, chunk_subject, piece)

    metadata = BlobMetadata(
        name=name,
        bucket=BUCKET_NAME,
        nuid=nuid,
        size=len(payload_bytes),
        chunks=len(slices),
        digest=sha256_padded_digest(sha256),
        headers=owner.to_headers(validated_team, sha256),
        max_chunk_size=size_per_chunk,
        deleted=False,
        seq=0,
    )
    return await _publish_metadata_cas(
        js,
        metadata=metadata,
        meta_subject=meta_subject,
        expected_seq=expected_seq,
        clock=clock,
        started=started,
        grace_limit=grace_limit,
    )


async def _read_next_chunk(js: Any, chunk_subject: str, next_seq: int) -> Optional[Any]:
    """Read the next chunk message at or after ``next_seq`` via ``STREAM.MSG.GET`` (S2)."""

    try:
        return await js.get_msg(STREAM_NAME, seq=next_seq, subject=chunk_subject, next=True)
    except NotFoundError as exc:
        _translate_not_found(exc, chunk_subject)
        if _is_no_message_error(exc):
            return None
        raise


async def _read_verified_chunks(
    js: Any,
    metadata: BlobMetadata,
    *,
    max_bytes: Optional[int],
) -> tuple[bytes, str]:
    """Read all chunks for ``metadata`` via ``STREAM.MSG.GET`` and return ``(bytes, unpadded_sha256)``."""

    chunk_subject = blob_chunk_subject(metadata.nuid)
    next_seq = 1
    buf = bytearray()
    hasher = hashlib.sha256()

    for index in range(metadata.chunks):
        raw = await _read_next_chunk(js, chunk_subject, next_seq)
        if raw is None:
            raise BlobCorrupted(f"Blob {metadata.name} is missing chunk {index + 1} of {metadata.chunks}.")
        if raw.subject != chunk_subject or raw.seq is None or raw.seq < next_seq:
            raise BlobCorrupted(
                f"Blob {metadata.name} returned unexpected chunk subject/sequence ({raw.subject!r}, {raw.seq!r})."
            )
        piece = raw.data or b""
        new_len = len(buf) + len(piece)
        if max_bytes is not None and new_len > max_bytes:
            raise BlobTooLarge(f"Blob {metadata.name} exceeded limit of {max_bytes} bytes while reading chunks.")
        if new_len > metadata.size:
            raise BlobCorrupted(
                f"Blob {metadata.name} chunk bytes ({new_len}) exceeded declared size {metadata.size}."
            )
        buf.extend(piece)
        hasher.update(piece)
        next_seq = int(raw.seq) + 1

    if await _read_next_chunk(js, chunk_subject, next_seq) is not None:
        raise BlobCorrupted(f"Blob {metadata.name} has more than the declared {metadata.chunks} chunks.")
    return bytes(buf), b64url(hasher.digest())


async def get_blob(
    js: Any,
    team: str,
    sha256: str,
    *,
    expected_size: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> bytes:
    """Read and fully verify a blob from ``OBJ_UA1_BLOBS`` using direct ``STREAM.MSG.GET`` reads (S2).

    Enforces ``max_bytes`` and ``expected_size`` before downloading chunks and during
    chunk reads, verifies exact chunk count and absence of trailing chunks, and checks
    the SHA-256 digest of the assembled bytes against ``sha256``.
    """

    validated_team = validate_team_id(team)
    validated_sha = validate_sha256(sha256)
    if expected_size is not None and not _is_non_negative_int(expected_size):
        raise ValueError(f"expected_size must be a non-negative integer, got {expected_size!r}")
    if max_bytes is not None and not _is_non_negative_int(max_bytes):
        raise ValueError(f"max_bytes must be a non-negative integer, got {max_bytes!r}")

    metadata = await stat_blob(js, validated_team, validated_sha)
    if metadata is None:
        raise BlobNotFound(f"Blob {validated_team}~{validated_sha} was not found.")
    if metadata.deleted:
        raise BlobDeleted(f"Blob {metadata.name} has been deleted.")
    if max_bytes is not None and metadata.size > max_bytes:
        raise BlobTooLarge(
            f"Blob {metadata.name} is {metadata.size} bytes, exceeding limit of {max_bytes} bytes."
        )
    if expected_size is not None and metadata.size != expected_size:
        raise BlobCorrupted(f"Blob {metadata.name} declares size {metadata.size}, expected {expected_size}.")

    content, actual_sha = await _read_verified_chunks(js, metadata, max_bytes=max_bytes)
    if len(content) != metadata.size:
        raise BlobCorrupted(f"Blob {metadata.name} assembled {len(content)} bytes, expected {metadata.size}.")
    if actual_sha != validated_sha:
        raise BlobCorrupted(
            f"Blob {metadata.name} SHA-256 mismatch: computed {actual_sha}, expected {validated_sha}."
        )
    return content


async def purge_blob_chunks(js: Any, nuid: str) -> bool:
    """Purge ``$O.UA1_BLOBS.C.<nuid>`` directly without touching any metadata subject."""

    return bool(await js.purge_stream(STREAM_NAME, subject=blob_chunk_subject(nuid)))


async def delete_blob(
    js: Any,
    team: str,
    sha256: str,
    *,
    expected_seq: Optional[int] = None,
    expected_nuid: Optional[str] = None,
    on_marker_written: Optional[Callable[[str], Awaitable[None]]] = None,
) -> BlobDeleteResult:
    """Delete a blob: write ``deleted: true`` marker with CAS, purge chunks, purge old metadata keeping last.

    ``expected_seq`` and ``expected_nuid`` guard against deleting newly written metadata
    when retrying an interrupted delete or cleaning up unindexed metadata.
    ``on_marker_written``, if supplied, is awaited with the deleted ``nuid`` right after
    the deleted marker is published and before chunks are purged so callers can record
    the ``nuid`` durably for crash recovery.
    """

    validated_team = validate_team_id(team)
    validated_sha = validate_sha256(sha256)
    name = blob_object_name(validated_team, validated_sha)
    meta_subject = blob_metadata_subject(name)

    metadata = await stat_blob(js, validated_team, validated_sha)
    if metadata is None:
        return BlobDeleteResult(deleted=False, nuid=None, marker_seq=None, already_deleted=False)

    if expected_seq is not None and metadata.seq != expected_seq:
        raise BlobConcurrentWrite(
            f"Blob {name} metadata moved from seq {expected_seq} to {metadata.seq} before delete."
        )
    if expected_nuid is not None and metadata.nuid != expected_nuid:
        raise BlobConcurrentWrite(
            f"Blob {name} metadata nuid changed from {expected_nuid!r} to {metadata.nuid!r} before delete."
        )

    already_deleted = metadata.deleted
    marker_seq = metadata.seq
    if not already_deleted:
        marker_bytes = json.dumps(metadata.as_deleted_payload(), separators=(",", ":")).encode("utf-8")
        try:
            ack = await _publish_checked(
                js,
                meta_subject,
                marker_bytes,
                headers={Header.EXPECTED_LAST_SUBJECT_SEQUENCE: str(metadata.seq)},
            )
        except APIError as exc:
            if exc.err_code == JS_ERR_WRONG_LAST_SEQUENCE:
                raise BlobConcurrentWrite(
                    f"Blob {name} metadata changed concurrently while writing deleted marker."
                ) from exc
            raise
        marker_seq = int(getattr(ack, "seq", 0) or 0)
        if on_marker_written is not None:
            await on_marker_written(metadata.nuid)

    await purge_blob_chunks(js, metadata.nuid)
    await js.purge_stream(STREAM_NAME, subject=meta_subject, keep=1)
    return BlobDeleteResult(
        deleted=True,
        nuid=metadata.nuid,
        marker_seq=marker_seq,
        already_deleted=already_deleted,
    )


def _normalize_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_metadata_entry(subject: str, raw: Any) -> BlobMetadataEntry:
    seq = int(raw.seq or 0)
    raw_time = _normalize_utc(raw.time) if isinstance(raw.time, datetime) else None
    decoded_name = decode_metadata_subject(subject)
    if decoded_name is None or "~" not in decoded_name:
        return BlobMetadataEntry(subject=subject, seq=seq, timestamp=raw_time, team=None, sha256=None, metadata=None)
    team, sha256 = decoded_name.split("~", 1)
    try:
        meta = parse_blob_metadata(raw.data or b"", expected_team=team, expected_sha256=sha256, seq=seq)
    except (ValueError, BlobCorrupted):
        return BlobMetadataEntry(
            subject=subject, seq=seq, timestamp=raw_time, team=team, sha256=sha256, metadata=None
        )
    return BlobMetadataEntry(
        subject=subject, seq=seq, timestamp=raw_time, team=team, sha256=sha256, metadata=meta
    )


async def _find_old_chunk_subjects(js: Any, cutoff: datetime) -> tuple[int, list[tuple[str, str]]]:
    chunk_subjects = await list_stream_subjects(js, f"{CHUNK_SUBJECT_PREFIX}>")
    old_candidates: list[tuple[str, str]] = []
    for chunk_subject in sorted(chunk_subjects):
        raw = await _read_raw_last_msg(js, chunk_subject)
        if raw is None:
            continue
        if not isinstance(raw.time, datetime):
            _LOGGER.warning(
                "Skipping blob chunk subject without timestamp during orphan sweep. "
                "event=blob_sweep_missing_time subject=%s",
                chunk_subject,
            )
            continue
        if _normalize_utc(raw.time) < cutoff:
            old_candidates.append((chunk_subject, chunk_subject[len(CHUNK_SUBJECT_PREFIX):]))
    return len(chunk_subjects), old_candidates


async def _scan_metadata_subjects(js: Any) -> tuple[set[str], list[BlobMetadataEntry]]:
    meta_subjects = await list_stream_subjects(js, f"{META_SUBJECT_PREFIX}>")
    live_nuids: set[str] = set()
    entries: list[BlobMetadataEntry] = []
    for meta_subject in sorted(meta_subjects):
        raw = await _read_raw_last_msg(js, meta_subject)
        if raw is None:
            continue
        entry = _parse_metadata_entry(meta_subject, raw)
        entries.append(entry)
        if entry.metadata is not None and not entry.metadata.deleted:
            live_nuids.add(entry.metadata.nuid)
    return live_nuids, entries


async def sweep_orphan_chunks(
    js: Any,
    *,
    now: Optional[datetime] = None,
    grace_seconds: Optional[int] = None,
    clock_skew_seconds: Optional[int] = None,
) -> BlobSweepResult:
    """Purge chunk subjects older than ``grace + clock_skew`` that no live metadata names (FA-6).

    Chunk ages are checked BEFORE metadata subjects are read, matching the contract
    invariant: any write in progress has a chunk newer than the cutoff, and any finished
    write published its metadata within half of ``grace_seconds``. Chunks named only by
    ``deleted: true`` metadata markers are NOT counted as live and are swept.
    """

    now_utc = _normalize_utc(now if now is not None else datetime.now(timezone.utc))
    grace = default_orphan_grace_seconds() if grace_seconds is None else int(grace_seconds)
    skew = default_clock_skew_seconds() if clock_skew_seconds is None else int(clock_skew_seconds)
    cutoff = now_utc - timedelta(seconds=grace + skew)

    checked_count, old_candidates = await _find_old_chunk_subjects(js, cutoff)
    # Read metadata strictly AFTER checking chunk ages.
    live_nuids, entries = await _scan_metadata_subjects(js)

    purged: list[str] = []
    for chunk_subject, nuid in old_candidates:
        if nuid in live_nuids:
            continue
        await js.purge_stream(STREAM_NAME, subject=chunk_subject)
        purged.append(chunk_subject)

    if purged:
        _LOGGER.info(
            "Swept orphaned blob chunk subjects from %s. "
            "event=blob_orphan_chunks_swept checked=%d old=%d purged=%d live_nuids=%d",
            STREAM_NAME,
            checked_count,
            len(old_candidates),
            len(purged),
            len(live_nuids),
        )
    return BlobSweepResult(
        checked_chunk_subjects=checked_count,
        old_chunk_subjects=len(old_candidates),
        purged_chunk_subjects=tuple(purged),
        kept_live_nuids=len(live_nuids),
        metadata_entries=tuple(entries),
    )


__all__ = [
    "BUCKET_NAME",
    "CHUNK_SUBJECT_PREFIX",
    "DIGEST_PREFIX",
    "HEADER_AGENT",
    "HEADER_PERSON",
    "HEADER_REQUEST",
    "HEADER_SHA256",
    "HEADER_TEAM",
    "JS_ERR_MAX_BYTES_EXCEEDED",
    "JS_ERR_NO_MESSAGE_FOUND",
    "JS_ERR_STREAM_NOT_FOUND",
    "JS_ERR_WRONG_LAST_SEQUENCE",
    "MAX_CAS_ATTEMPTS",
    "META_SUBJECT_PREFIX",
    "PAGE_ATTEMPTS",
    "STREAM_NAME",
    "BlobConcurrentWrite",
    "BlobCorrupted",
    "BlobDeleteResult",
    "BlobDeleted",
    "BlobError",
    "BlobMetadata",
    "BlobMetadataEntry",
    "BlobNotFound",
    "BlobOwner",
    "BlobPutResult",
    "BlobStoreFull",
    "BlobSubjectListShifted",
    "BlobSweepResult",
    "BlobTooLarge",
    "BlobWriteTimedOut",
    "blob_chunk_subject",
    "blob_metadata_subject",
    "blob_object_name",
    "check_chunks_present",
    "decode_metadata_subject",
    "default_chunk_bytes",
    "default_clock_skew_seconds",
    "default_orphan_grace_seconds",
    "delete_blob",
    "get_blob",
    "list_stream_subjects",
    "parse_blob_metadata",
    "purge_blob_chunks",
    "put_blob",
    "sha256_padded_digest",
    "sha256_unpadded",
    "stat_blob",
    "sweep_orphan_chunks",
    "validate_nuid",
    "validate_sha256",
    "validate_team_id",
]

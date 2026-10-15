"""Files agents attach to messages for people (``notify_user``).

An agent names a file in its task's folders. The agent tool checks it early;
the gateway checks it again for the person the message is for and then copies
it once into that person's scratch folder,
``users/<person>/scratch/sent/<id>/<name>`` (``agents/scratch/sent/…`` for
work for no person), and records the copy's SHA-256
(``stage_user_notification_attachments``). The file is found by its real
location, must be in the person's folder, the shared ``team/`` and
``agents/`` folders, a registered data folder (decision D16) or something
another person shared with this person (decision D15), and is opened without
following links
(``access_control.safe_open``), so a link swapped in after the check cannot
send a control file to a person.

The copy lives where the person's files live, so trusted maintenance deletes
it with the rest of ``scratch/`` (``custom_agents.scratch_retention``). Agents'
code can write that folder too, so the copy is not trusted as it stands: every
reader (chat channels, the adapter host, the web transcript download) reads it
through ``StagedAttachmentReader``, which opens it below its folder without
following links, requires a one-link regular file of bounded size, and checks
the recorded SHA-256. A link or changed bytes swapped in after staging are
refused, never sent. Readers that need a path (the Photon sidecar) get a
private 0600 copy of the checked bytes that is removed after use.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from access_control import safe_open
from access_control.safe_open import UnsafePath
from access_control.sensitive_paths import sensitive_reason
from custom_messages.contracts.delivery_v1 import UserNotificationAttachment
from id_generation import new_id
from runtime_settings.instance_layout import (
    SCRATCH_SENT_DIRNAME,
    InstanceLayout,
    WorkspaceScope,
    scratch_folder_parts,
    workspace_scope,
)

logger = logging.getLogger(__name__)


ATTACHMENT_KINDS = {"auto", "image", "document", "audio", "video"}
# The largest attachment the gateway copies for delivery (the adapter host's own limit).
MAX_OUTGOING_ATTACHMENT_BYTES = 256 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024

# Why a staged copy was not read (StagedAttachmentUnavailable.reason).
STAGED_NO_DIGEST = "no_digest"
STAGED_OUTSIDE_FOLDERS = "outside_staging_folders"
STAGED_MISSING = "missing"
STAGED_CHANGED = "changed"
STAGED_TOO_LARGE = "too_large"
STAGED_UNSAFE = "unsafe"


@dataclass(frozen=True)
class _Source:
    """A folder attachments may come from; ``only`` limits it to one file in it (a shared file)."""

    root: Path
    only: str | None = None


def _extra_sources(config: Any, person_id: str | None) -> list[_Source]:
    """Registered data folders, and what other people shared with ``person_id``.

    The agent-side decision already allowed the file (``custom_tools.file_access``
    asks the access service); this repeats only the location part of that
    rule, from the access store, so a path elsewhere is still refused.
    """

    from access_control.data_folders import DataFolderRegistry, protected_locations
    from access_control.folders import FOLDER_ROOT_USERS
    from custom_messages.contracts.access_v2 import GrantKind, GrantStatus, SubjectKind, SubjectRefV1, TargetKind
    from storage.access_repository import AccessRepository

    storage = getattr(config, "manager_storage_settings", None)
    team_id = str(getattr(getattr(config, "manager", None), "team_id", "") or "")
    workspaces = getattr(config, "workspace_path", None)
    if storage is None or not team_id or workspaces is None:
        return []
    store = AccessRepository(storage)
    root = getattr(config, "instance_root_path", None)
    protected = protected_locations(root, broker_credentials=getattr(config, "broker_credentials_path", None))
    sources = [_Source(folder.path) for folder in DataFolderRegistry(store, team_id).usable(protected)]
    if not person_id:
        return sources
    now = _utc_now()
    for grant in store.for_subjects(team_id, [SubjectRefV1(kind=SubjectKind.USER, id=person_id)]):
        shared = (
            grant.kind is GrantKind.ACCESS
            and grant.status is GrantStatus.ACTIVE
            and grant.target.kind in {TargetKind.FOLDER, TargetKind.FILE}
            and grant.target.id.split("/", 1)[0] == FOLDER_ROOT_USERS
            and (grant.expires_at_utc is None or grant.expires_at_utc > now)
        )
        if not shared:
            continue
        path = Path(os.path.realpath(Path(workspaces).joinpath(*grant.target.id.split("/"))))
        sources.append(_Source(path.parent, path.name) if grant.target.kind is TargetKind.FILE else _Source(path))
    return sources


def _utc_now() -> Any:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _attachment_scope(config: Any, person_id: str | None) -> WorkspaceScope | None:
    """Folders an attachment may come from: the person's folder, team/, agents/.

    Everything else under the instance root (team.env, instance configuration,
    control files, databases, broker state) is never an attachment source, and
    neither is another person's folder.
    """

    try:
        workspaces = getattr(config, "workspace_path")
    except Exception:
        return None
    if workspaces is None:
        return None
    return workspace_scope(Path(workspaces), person_id)


def _is_sensitive_path(path: Path) -> bool:
    return sensitive_reason(path) is not None


def _coerce_attachment_items(raw_attachments: Any) -> tuple[list[Any], str | None]:
    if raw_attachments in (None, "", []):
        return [], None
    if isinstance(raw_attachments, str):
        rendered = raw_attachments.strip()
        if not rendered:
            return [], None
        if rendered.startswith("[") or rendered.startswith("{"):
            try:
                decoded = json.loads(rendered)
            except json.JSONDecodeError as exc:
                return [], f"attachments must be a list, path string, or JSON object/list: {exc}"
            raw_attachments = decoded
        else:
            return [rendered], None
    if isinstance(raw_attachments, UserNotificationAttachment):
        return [raw_attachments], None
    if isinstance(raw_attachments, Mapping):
        return [dict(raw_attachments)], None
    if isinstance(raw_attachments, (list, tuple)):
        return list(raw_attachments), None
    return [], "attachments must be a list of attachment objects or path strings."


def _located(
    resolved: Path, scope: WorkspaceScope, extra: Iterable[_Source] = ()
) -> tuple[Path, tuple[str, ...]] | None:
    """The allowed folder holding the real path ``resolved``, and the path below it."""

    for root in scope.roots:
        real_root = Path(os.path.realpath(root))
        if resolved == real_root or real_root in resolved.parents:
            return real_root, resolved.relative_to(real_root).parts
    for source in extra:
        real_root = Path(os.path.realpath(source.root))
        if real_root not in resolved.parents:
            continue
        parts = resolved.relative_to(real_root).parts
        if source.only is None or parts == (source.only,):
            return real_root, parts
    return None


OUTSIDE_SOURCES = (
    "is outside this person's folder, the shared team/ and agents/ folders, the data folders, "
    "and what others shared with this person"
)


def _checked_path(
    resolved: Path, *, scope: WorkspaceScope, extra: Iterable[_Source] = ()
) -> tuple[str | None, int | None]:
    """Why a real path cannot be attached (None when it can), and its size."""

    located = _located(resolved, scope, extra)
    if located is None:
        return OUTSIDE_SOURCES, None
    if _is_sensitive_path(resolved):
        return "is sensitive and cannot be attached", None
    try:
        entry = safe_open.entry_status(*located)
    except UnsafePath as exc:
        return f"cannot be opened safely ({exc})", None
    if entry is None:
        return "does not exist", None
    if not stat.S_ISREG(entry.st_mode):
        return "is not a file", None
    if entry.st_nlink != 1:
        return "has more than one hard link", None
    return None, entry.st_size


def _normalize_path(raw_path: Any, *, home: Path) -> Path:
    candidate = Path(str(raw_path or "").strip()).expanduser()
    if not candidate.is_absolute():
        candidate = home / candidate
    return candidate.resolve()


def _infer_attachment_kind(*, requested: str | None, content_type: str, path: Path) -> str:
    normalized = str(requested or "auto").strip().lower() or "auto"
    if normalized not in ATTACHMENT_KINDS:
        raise ValueError(f"kind must be one of {sorted(ATTACHMENT_KINDS)}")
    if normalized != "auto":
        return normalized
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("audio/"):
        return "audio"
    if content_type.startswith("video/"):
        return "video"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"
    return "document"


def validate_user_notification_attachments(
    raw_attachments: Any,
    *,
    config: Any,
    person_id: str | None,
) -> tuple[list[UserNotificationAttachment], list[str]]:
    """Validate attachments for a message to ``person_id`` (None: no person)."""
    items, error = _coerce_attachment_items(raw_attachments)
    if error:
        return [], [error]
    if not items:
        return [], []

    scope = _attachment_scope(config, person_id)
    if scope is None:
        return [], ["Cannot validate attachments because the workspaces path is unavailable."]
    extra = _extra_sources(config, person_id)
    validated: list[UserNotificationAttachment] = []
    errors: list[str] = []

    for index, raw in enumerate(items):
        payload = raw.model_dump() if isinstance(raw, UserNotificationAttachment) else raw
        if isinstance(payload, str):
            payload = {"path": payload}
        if not isinstance(payload, Mapping):
            errors.append(f"attachments[{index}] must be an object or path string.")
            continue
        raw_path = str(payload.get("path") or "").strip()
        if not raw_path:
            errors.append(f"attachments[{index}].path is required.")
            continue
        try:
            resolved = _normalize_path(raw_path, home=scope.home)
        except Exception as exc:
            errors.append(f"attachments[{index}].path could not be resolved: {exc}")
            continue
        rejection, size_bytes = _checked_path(resolved, scope=scope, extra=extra)
        if rejection:
            errors.append(f"attachments[{index}].path {rejection}: {resolved}")
            continue
        content_type = str(payload.get("content_type") or "").strip()
        if not content_type:
            content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        try:
            kind = _infer_attachment_kind(
                requested=str(payload.get("kind") or "auto"),
                content_type=content_type,
                path=resolved,
            )
        except ValueError as exc:
            errors.append(f"attachments[{index}].{exc}")
            continue
        filename = Path(str(payload.get("filename") or resolved.name)).name
        caption = str(payload.get("caption") or "").strip() or None
        validated.append(
            UserNotificationAttachment(
                path=str(resolved),
                filename=filename,
                content_type=content_type,
                caption=caption,
                kind=kind,  # type: ignore[arg-type]
                size_bytes=size_bytes,
            )
        )
    return validated, errors


def stage_user_notification_attachments(
    attachments: tuple[UserNotificationAttachment, ...] | list[UserNotificationAttachment],
    *,
    config: Any,
    person_id: str | None,
) -> tuple[list[UserNotificationAttachment], list[str]]:
    """Copy checked attachments into the person's ``scratch/sent/``; channels send the copies.

    Each file is copied from its folder without following links, into a new
    folder created without following links, so the copy is the file that was
    checked; its SHA-256 is recorded on the returned attachment so readers can
    refuse it if it changes. Returns the attachments pointing at their copies,
    and a message for each one that could not be copied.
    """

    scope = _attachment_scope(config, person_id)
    if scope is None:
        return [], ["Cannot copy attachments because the workspaces path is unavailable."]
    extra = _extra_sources(config, person_id)
    sent_parts = (*scratch_folder_parts(person_id), SCRATCH_SENT_DIRNAME)
    staged: list[UserNotificationAttachment] = []
    errors: list[str] = []
    for attachment in attachments:
        resolved = Path(attachment.path)
        located = _located(resolved, scope, extra)
        if located is None:
            errors.append(f"{resolved} {OUTSIDE_SOURCES}.")
            continue
        name = Path(attachment.filename or resolved.name).name
        folder_parts = (*sent_parts, new_id("outgoing"))
        destination_parts = (*folder_parts, name if name not in {"", ".", ".."} else "attachment")
        try:
            copied, digest = _copy_with_digest(located, scope.workspaces_dir, destination_parts)
        except UnsafePath as exc:
            _remove_partial_copy(scope.workspaces_dir, folder_parts, destination_parts[-1])
            logger.warning(
                "An attachment was not copied for delivery. event=outgoing_attachment_refused "
                "person_id=%s path=%s reason=%s",
                person_id,
                resolved,
                exc.reason,
            )
            errors.append(f"{resolved} cannot be attached: {exc}")
            continue
        destination = scope.workspaces_dir.joinpath(*destination_parts)
        staged.append(
            attachment.model_copy(update={"path": str(destination), "size_bytes": copied, "sha256": digest})
        )
    return staged, errors


def _copy_with_digest(
    source: tuple[Path, tuple[str, ...]],
    workspaces: Path,
    destination_parts: tuple[str, ...],
) -> tuple[int, str]:
    """Copy a checked file to a new file below ``workspaces``; returns its size and SHA-256.

    The destination's folders are created and opened one component at a time
    without following links, and the file is created with ``O_EXCL``, so a
    link planted in the person's folder can neither redirect the copy nor
    stand in for it.
    """

    too_large = UnsafePath(
        f"The file is larger than {MAX_OUTGOING_ATTACHMENT_BYTES} bytes.", reason=safe_open.REASON_TOO_LARGE
    )
    digest = hashlib.sha256()
    copied = 0
    with safe_open.opened_file(*source) as (descriptor, info):
        if info.st_size > MAX_OUTGOING_ATTACHMENT_BYTES:
            raise too_large
        with (
            os.fdopen(os.dup(descriptor), "rb") as reader,
            safe_open.writable_file(workspaces, destination_parts, create_directories=True, exclusive=True) as writer,
        ):
            while chunk := reader.read(_COPY_CHUNK_BYTES):
                copied += len(chunk)
                if copied > MAX_OUTGOING_ATTACHMENT_BYTES:
                    raise too_large  # it grew while it was copied
                digest.update(chunk)
                writer.write(chunk)
    return copied, digest.hexdigest()


def _remove_partial_copy(workspaces: Path, folder_parts: tuple[str, ...], name: str) -> None:
    """Remove what a failed copy left behind, without following links."""

    try:
        folder = safe_open.open_directory(workspaces, folder_parts)
    except UnsafePath:
        return  # the folder was never created, or is not ours to remove
    try:
        try:
            os.unlink(name, dir_fd=folder)
        except FileNotFoundError:
            pass
    finally:
        os.close(folder)
    try:
        parent = safe_open.open_directory(workspaces, folder_parts[:-1])
    except UnsafePath:
        return
    try:
        os.rmdir(folder_parts[-1], dir_fd=parent)
    except OSError as exc:
        logger.warning(
            "A failed attachment copy left its folder behind. event=outgoing_attachment_cleanup_failed "
            "folder=%s error=%s",
            workspaces.joinpath(*folder_parts),
            exc,
        )
    finally:
        os.close(parent)


class StagedAttachmentUnavailable(Exception):
    """A staged copy cannot be read as staged; ``reason`` says why (``STAGED_*``)."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


_SAFE_OPEN_REFUSALS = {
    safe_open.REASON_NOT_FOUND: STAGED_MISSING,
    safe_open.REASON_TOO_LARGE: STAGED_TOO_LARGE,
}


@dataclass(frozen=True)
class StagedAttachmentReader:
    """The one way to read a file the gateway staged for a person.

    ``roots`` are the folders staged files live below: in the gateway, the
    workspaces folder (``notify_user`` copies) and ``state/uploads`` (chat
    uploads shown in the web transcript); in an external adapter host, its
    private download folder. A path outside them is refused. The file is
    opened without following links, must be a one-link regular file no larger
    than ``max_bytes``, and must still have the recorded SHA-256.
    """

    roots: tuple[Path, ...]
    private_copies_dir: Path
    max_bytes: int = MAX_OUTGOING_ATTACHMENT_BYTES

    def read(self, attachment: UserNotificationAttachment) -> bytes:
        """The checked bytes of a staged notification attachment."""

        return self.read_bytes(attachment.path, attachment.sha256)

    def read_bytes(self, path: str | Path, sha256: str | None) -> bytes:
        """The bytes at ``path`` if they still have the SHA-256 recorded at staging."""

        try:
            return self._verified_bytes(Path(path), sha256)
        except StagedAttachmentUnavailable as exc:
            logger.warning(
                "A staged attachment was refused and is not sent. event=staged_attachment_refused "
                "reason=%s path=%s detail=%s",
                exc.reason,
                path,
                exc,
            )
            raise

    def readable(
        self, attachments: Iterable[UserNotificationAttachment]
    ) -> Iterator[tuple[UserNotificationAttachment, bytes]]:
        """Each attachment with its checked bytes; refused ones are skipped (and logged)."""

        for attachment in attachments:
            try:
                yield attachment, self.read(attachment)
            except StagedAttachmentUnavailable:
                continue

    @contextmanager
    def private_copy(self, attachment: UserNotificationAttachment) -> Iterator[Path]:
        """A 0600 copy of the checked bytes in the instance's state folder, removed afterwards.

        For readers that can only take a path, such as the Photon sidecar;
        the copy is outside every folder agents can reach.
        """

        data = self.read(attachment)
        self.private_copies_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        suffix = Path(attachment.filename or attachment.path).suffix[:16]
        descriptor, raw_path = tempfile.mkstemp(prefix="attachment-", suffix=suffix, dir=self.private_copies_dir)
        copy = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as writer:
                writer.write(data)
            yield copy
        finally:
            copy.unlink(missing_ok=True)

    def _verified_bytes(self, path: Path, sha256: str | None) -> bytes:
        expected = str(sha256 or "").strip().lower()
        if not expected:
            raise StagedAttachmentUnavailable("No SHA-256 was recorded when it was staged.", reason=STAGED_NO_DIGEST)
        located = self._located(path)
        if located is None:
            raise StagedAttachmentUnavailable(
                "It is not in a folder staged files are kept in.", reason=STAGED_OUTSIDE_FOLDERS
            )
        try:
            data = safe_open.read_bytes(*located, max_bytes=self.max_bytes)
        except UnsafePath as exc:
            raise StagedAttachmentUnavailable(
                str(exc), reason=_SAFE_OPEN_REFUSALS.get(exc.reason, STAGED_UNSAFE)
            ) from exc
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
            raise StagedAttachmentUnavailable("It changed after it was staged.", reason=STAGED_CHANGED)
        return data

    def _located(self, path: Path) -> tuple[Path, tuple[str, ...]] | None:
        # Lexical on purpose: the staged path was built by trusted code, and
        # resolving it would follow links. safe_open refuses ".." components.
        if not path.is_absolute():
            return None
        for root in self.roots:
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                continue
            if parts:
                return root, parts
        return None


def gateway_staged_attachment_reader(config: Any) -> StagedAttachmentReader:
    """The gateway's reader: staged copies below ``workspaces/``, chat uploads below ``state/uploads``.

    Roots are real paths, matching how staging and uploads build their paths.
    """

    layout = InstanceLayout.at(config.instance_root_path)
    return StagedAttachmentReader(
        roots=(Path(os.path.realpath(layout.workspaces_dir)), Path(os.path.realpath(layout.uploads_dir))),
        private_copies_dir=layout.attachment_copies_dir,
    )

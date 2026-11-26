"""Agent-driven capture of a secret from a task file into a person's temporary credential store.

Implements broker action ``credential.capture_file`` on ``procedure:credentials``
(level ``write``). The secret value is read once directly from a regular file
inside the task person's folders without following symbolic links, stored in
the person's Sealbox tenant with a required expiry (5 minutes to 90 days) via
``credential_catalog.collect_credential``, deleted with an identity check after
the catalog commit succeeds, and reported to the person with one Inbox notice
(unless ``security_layer.credential_catalog.notify_on_agent_capture`` is off).
No secret bytes ever appear in parameters, results, notices, or logs.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping

from pydantic import ValidationError

from access_control import safe_open
from access_control.folders import FolderIdError
from access_control.safe_open import UnsafePath
from custom_messages.contracts.access_v2 import (
    CREDENTIAL_KEY_PATTERN,
    AccessLevel,
    AccessNoticeKind,
    AccessNoticeV1,
)
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    collect_credential,
    load_catalog,
)
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.credential_expiry import (
    CredentialExpiryError,
    capture_expiry_window,
    format_expiry_label,
    format_utc_timestamp,
    validate_capture_expiry,
)
from secure_tool_broker.defaults import (
    credential_max_file_bytes,
    notify_on_agent_capture_enabled,
)
from secure_tool_broker.task_files import LocatedTaskFile, TaskFileRejected, locate_task_file
from secure_tools.providers import ToolContext, ToolResult

if TYPE_CHECKING:
    from secure_tool_broker.access_gate import BrokerAccessGate

LOGGER = logging.getLogger(__name__)

DEFAULT_CAPTURE_FIELD_NAME = "secret"
CAPTURE_APPROVAL_SOURCE = "agent_file_capture"
CAPTURE_DISPLAY_NAME_MAX_CHARS = 120
CAPTURE_PURPOSE_MAX_CHARS = 400

CAPTURE_PERSON_REQUIRED = "capture_person_required"
CAPTURE_SECRET_PARAM_REJECTED = "capture_secret_param_rejected"
CAPTURE_TEAM_SCOPE_REFUSED = "capture_team_scope_refused"
CAPTURE_UNEXPECTED_PARAMS = "capture_unexpected_params"
CAPTURE_INVALID_PARAMS = "capture_invalid_params"
CAPTURE_PATH_OUTSIDE_SCOPE = "capture_path_outside_scope"
CAPTURE_ACCESS_GATE_REQUIRED = "capture_access_gate_required"
CAPTURE_FOLDER_ACCESS_DENIED = "capture_folder_access_denied"
CAPTURE_FOLDER_CHECK_UNAVAILABLE = "capture_folder_check_unavailable"
CAPTURE_SYMLINK_REFUSED = "capture_symlink_refused"
CAPTURE_NOT_REGULAR_FILE = "capture_not_regular_file"
CAPTURE_FILE_NOT_FOUND = "capture_file_not_found"
CAPTURE_FILE_TOO_LARGE = "capture_file_too_large"
CAPTURE_FILE_EMPTY = "capture_file_empty"
CAPTURE_FILE_INVALID_ENCODING = "capture_file_invalid_encoding"
CAPTURE_FILE_UNREADABLE = "capture_file_unreadable"
CAPTURE_TEAM_KEY_REFUSED = "capture_team_key_refused"
CAPTURE_EXTERNAL_SOURCE_KEY_REFUSED = "capture_external_source_key_refused"
CAPTURE_PERSON_CREDENTIAL_REFUSED = "capture_person_credential_refused"
CAPTURE_FIELD_NAME_MISMATCH = "capture_field_name_mismatch"

NOTICE_STATUS_SENT = "sent"
NOTICE_STATUS_OFF = "off"
NOTICE_STATUS_FAILED = "failed"

_ALLOWED_PARAMS = frozenset(
    {"path", "credential_key", "display_name", "purpose", "field_name", "expires_at"}
)
_SECRET_PARAM_KEYS = frozenset(
    {
        "value",
        "secret",
        "password",
        "token",
        "content",
        "contents",
        "content_base64",
        "submitted_fields",
        "fields_values",
        "fields",
        "data",
        "body",
        "text",
        "bytes",
        "raw",
    }
)
_TEAM_SCOPE_PARAM_KEYS = frozenset(
    {"scope", "scope_kind", "owner_scope", "owner_scope_kind", "team_scope", "owner_kind"}
)
_FILE_LEFT_IN_PLACE_NOTE = "The source file was left in place."


@dataclass(frozen=True)
class _ValidatedCaptureParams:
    path: str
    credential_key: str
    display_name: str
    purpose: str
    field_name: str
    expires_at: datetime
    expires_at_iso: str


def _refuse(error_code: str, message: str, **extra: Any) -> ToolResult:
    text = message.strip()
    if _FILE_LEFT_IN_PLACE_NOTE not in text:
        text = f"{text} {_FILE_LEFT_IN_PLACE_NOTE}"
    content: dict[str, Any] = {
        "error_code": error_code,
        "source_file_deleted": False,
    }
    content.update({key: value for key, value in extra.items() if value is not None})
    return ToolResult(status="error", error=text, content=content)


def _validate_single_line_text(
    raw: Any,
    *,
    param_name: str,
    max_chars: int,
) -> str | ToolResult:
    if not isinstance(raw, str):
        return _refuse(CAPTURE_INVALID_PARAMS, f"Parameter '{param_name}' must be a non-empty string.")
    text = raw.strip()
    if not text:
        return _refuse(CAPTURE_INVALID_PARAMS, f"Parameter '{param_name}' is required.")
    if len(text) > max_chars:
        return _refuse(
            CAPTURE_INVALID_PARAMS,
            f"Parameter '{param_name}' must be at most {max_chars} characters.",
        )
    if any(ch in {"\r", "\n", "\x00"} or not ch.isprintable() for ch in text):
        return _refuse(
            CAPTURE_INVALID_PARAMS,
            f"Parameter '{param_name}' must be single-line printable text.",
        )
    return text


def _check_param_keys(params: Mapping[str, Any]) -> ToolResult | None:
    lowered_keys = {str(key).strip().lower(): str(key) for key in params}
    secret_keys = sorted(orig for low, orig in lowered_keys.items() if low in _SECRET_PARAM_KEYS)
    if secret_keys:
        return _refuse(
            CAPTURE_SECRET_PARAM_REJECTED,
            "credential.capture_file reads secrets only from a local file; "
            f"secret-bearing parameters are refused ({', '.join(secret_keys)}).",
        )
    scope_keys = sorted(orig for low, orig in lowered_keys.items() if low in _TEAM_SCOPE_PARAM_KEYS)
    if scope_keys:
        return _refuse(
            CAPTURE_TEAM_SCOPE_REFUSED,
            "credential.capture_file stores credentials only in the task person's own scope; "
            f"scope parameters are refused ({', '.join(scope_keys)}).",
        )
    unexpected = sorted(str(key) for key in params if str(key) not in _ALLOWED_PARAMS)
    if unexpected:
        return _refuse(
            CAPTURE_UNEXPECTED_PARAMS,
            f"Unexpected parameter(s) for credential.capture_file: {', '.join(unexpected)}.",
        )
    return None


def _validate_identifier(
    raw: Any,
    *,
    param_name: str,
    default: str | None = None,
    credential_key: str | None = None,
) -> str | ToolResult:
    if raw is None and default is not None:
        return default
    if not isinstance(raw, str) or not CREDENTIAL_KEY_PATTERN.fullmatch(raw.strip()):
        return _refuse(
            CAPTURE_INVALID_PARAMS,
            f"Parameter '{param_name}' must start with a letter or number and contain only "
            "letters, numbers, '.', '_', or '-' (at most 120 characters).",
            credential_key=credential_key,
        )
    return raw.strip()


def _validate_params(
    params: Mapping[str, Any],
    *,
    now: datetime,
) -> _ValidatedCaptureParams | ToolResult:
    key_refusal = _check_param_keys(params)
    if key_refusal is not None:
        return key_refusal

    raw_path = params.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return _refuse(CAPTURE_INVALID_PARAMS, "Parameter 'path' must be a non-empty file path string.")
    path = raw_path.strip()

    credential_key = _validate_identifier(params.get("credential_key"), param_name="credential_key")
    if isinstance(credential_key, ToolResult):
        return credential_key

    field_name = _validate_identifier(
        params.get("field_name"),
        param_name="field_name",
        default=DEFAULT_CAPTURE_FIELD_NAME,
        credential_key=credential_key,
    )
    if isinstance(field_name, ToolResult):
        return field_name

    display_name = _validate_single_line_text(
        params.get("display_name"),
        param_name="display_name",
        max_chars=CAPTURE_DISPLAY_NAME_MAX_CHARS,
    )
    if isinstance(display_name, ToolResult):
        return display_name

    purpose = _validate_single_line_text(
        params.get("purpose"),
        param_name="purpose",
        max_chars=CAPTURE_PURPOSE_MAX_CHARS,
    )
    if isinstance(purpose, ToolResult):
        return purpose

    broker_now, earliest, latest = capture_expiry_window(now=now)
    try:
        validated_expiry = validate_capture_expiry(params.get("expires_at"), now=now)
    except CredentialExpiryError as exc:
        return _refuse(
            exc.code,
            exc.message,
            credential_key=credential_key,
            broker_now_utc=broker_now,
            earliest_expires_at=earliest,
            latest_expires_at=latest,
        )

    return _ValidatedCaptureParams(
        path=path,
        credential_key=credential_key,
        display_name=display_name,
        purpose=purpose,
        field_name=field_name,
        expires_at=validated_expiry,
        expires_at_iso=format_utc_timestamp(validated_expiry),
    )


def _build_capture_notice(
    *,
    team_id: str,
    person_id: str,
    agent_type: str,
    validated: _ValidatedCaptureParams,
    now: datetime,
) -> AccessNoticeV1:
    """Build the person's Inbox notice (never contains secret values or file content)."""

    notice_id = f"credential_captured_{secrets.token_hex(16)}"
    title = f"{agent_type} saved a temporary credential: {validated.display_name}"
    summary = (
        f"{agent_type} saved a temporary credential ({validated.display_name}, key {validated.credential_key}) "
        f"for your account from a task file. It is set to be deleted automatically on "
        f"{format_expiry_label(validated.expires_at)} ({validated.expires_at_iso})."
    )
    return AccessNoticeV1(
        notice_id=notice_id,
        team_id=team_id,
        kind=AccessNoticeKind.CREDENTIAL_CAPTURED,
        recipient_person_id=person_id,
        recipient_roles=(),
        title=title,
        summary=summary,
        created_at_utc=now,
    )


def _check_folder_access(
    access_gate: BrokerAccessGate,
    context: ToolContext,
    located: LocatedTaskFile,
    *,
    credential_key: str,
) -> ToolResult | None:
    if access_gate.access is None:
        return None
    try:
        decision = access_gate.decide_folder_for_context(
            context,
            located.folder_id,
            is_file=True,
            level=AccessLevel.WRITE,
        )
    except FolderIdError as exc:
        return _refuse(
            CAPTURE_FOLDER_ACCESS_DENIED,
            f"Folder access check refused this file path ({exc}).",
            credential_key=credential_key,
            folder_id=located.folder_id,
        )
    except RuntimeError as exc:
        LOGGER.warning(
            "Folder access check could not run for credential.capture_file. "
            "event=credential_capture_folder_check_failed credential_key=%s person_id=%s folder_id=%s error=%s",
            credential_key,
            context.canonical_user_id,
            located.folder_id,
            type(exc).__name__,
        )
        return _refuse(
            CAPTURE_FOLDER_CHECK_UNAVAILABLE,
            f"The folder access check for {located.folder_id} could not run: {exc}.",
            credential_key=credential_key,
            folder_id=located.folder_id,
        )
    if decision is not None and not decision.allowed:
        reason = f"{decision.reason} {decision.next_step}".strip()
        return _refuse(
            CAPTURE_FOLDER_ACCESS_DENIED,
            reason or "Folder write access is denied for this file.",
            credential_key=credential_key,
            folder_id=decision.folder_id or located.folder_id,
            next_step=decision.next_step or None,
        )
    return None


def _unsafe_path_code(reason: str) -> str:
    if reason == safe_open.REASON_LINK:
        return CAPTURE_SYMLINK_REFUSED
    if reason == safe_open.REASON_NOT_FOUND:
        return CAPTURE_FILE_NOT_FOUND
    if reason in {
        safe_open.REASON_NOT_REGULAR_FILE,
        safe_open.REASON_NOT_DIRECTORY,
        safe_open.REASON_SEVERAL_LINKS,
    }:
        return CAPTURE_NOT_REGULAR_FILE
    if reason == safe_open.REASON_TOO_LARGE:
        return CAPTURE_FILE_TOO_LARGE
    return CAPTURE_FILE_UNREADABLE


def _read_secret_file(
    located: LocatedTaskFile,
    *,
    max_bytes: int,
    credential_key: str,
) -> tuple[str, tuple[int, int, int, int]] | ToolResult:
    """Open once without following links, capture file identity, read once, and decode UTF-8."""

    try:
        with safe_open.opened_file(located.root, located.parts) as (descriptor, info):
            identity = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
            if info.st_size > max_bytes:
                return _refuse(
                    CAPTURE_FILE_TOO_LARGE,
                    f"The source file exceeds the maximum credential file size of {max_bytes} bytes.",
                    credential_key=credential_key,
                    max_file_bytes=max_bytes,
                )
            with os.fdopen(os.dup(descriptor), "rb") as reader:
                raw_bytes = reader.read(max_bytes + 1)
    except UnsafePath as exc:
        return _refuse(
            _unsafe_path_code(exc.reason),
            str(exc),
            credential_key=credential_key,
        )
    except OSError as exc:
        return _refuse(
            CAPTURE_FILE_UNREADABLE,
            f"The source file could not be read: {exc.strerror or type(exc).__name__}.",
            credential_key=credential_key,
        )

    if len(raw_bytes) > max_bytes:
        return _refuse(
            CAPTURE_FILE_TOO_LARGE,
            f"The source file exceeds the maximum credential file size of {max_bytes} bytes.",
            credential_key=credential_key,
            max_file_bytes=max_bytes,
        )
    if not raw_bytes:
        return _refuse(
            CAPTURE_FILE_EMPTY,
            "The source file is empty and cannot be stored as a credential.",
            credential_key=credential_key,
        )
    if b"\x00" in raw_bytes:
        return _refuse(
            CAPTURE_FILE_INVALID_ENCODING,
            "The source file contains NUL bytes and cannot be stored as a text credential.",
            credential_key=credential_key,
        )
    try:
        # Fixed error message: never format UnicodeDecodeError, which embeds raw failing bytes.
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _refuse(
            CAPTURE_FILE_INVALID_ENCODING,
            "The source file is not valid UTF-8 text and cannot be stored as a credential.",
            credential_key=credential_key,
        )
    secret_value = decoded.strip()
    if not secret_value:
        return _refuse(
            CAPTURE_FILE_EMPTY,
            "The source file contains only whitespace and cannot be stored as a credential.",
            credential_key=credential_key,
        )
    return secret_value, identity


def is_agent_captured_for_person(credential: CatalogCredential, person_id: str) -> bool:
    """Return True only when ``credential`` was created by ``credential.capture_file`` for ``person_id``."""

    approval = credential.approval
    if not isinstance(approval, Mapping):
        return False
    return (
        str(approval.get("status") or "").strip().lower() == "captured"
        and str(approval.get("source") or "").strip().lower() == CAPTURE_APPROVAL_SOURCE
        and str(approval.get("captured_for_person_id") or "").strip() == person_id
    )


def _has_non_sealbox_source(credential: CatalogCredential) -> bool:
    for field in credential.fields.values():
        for ref in field.resolution_order:
            if str(ref.source or "").strip().lower() != "sealbox":
                return True
    return False


def _make_overwrite_guard(
    *,
    credential_key: str,
    field_name: str,
    person_id: str,
    team_id: str | None,
) -> Callable[[CatalogCredential | None], ToolResult | None]:
    def _guard(existing: CatalogCredential | None) -> ToolResult | None:
        if team_id:
            team_catalog = load_catalog(scope=CredentialOwnerScope.team(team_id))
            if credential_key in team_catalog.credentials:
                return _refuse(
                    CAPTURE_TEAM_KEY_REFUSED,
                    f"Credential key '{credential_key}' belongs to the team's credential store and "
                    "cannot be created or replaced by credential.capture_file.",
                    credential_key=credential_key,
                )
        if existing is None:
            return None
        if _has_non_sealbox_source(existing):
            return _refuse(
                CAPTURE_EXTERNAL_SOURCE_KEY_REFUSED,
                f"Credential '{credential_key}' uses an external (non-Sealbox) source and cannot be "
                "replaced by credential.capture_file.",
                credential_key=credential_key,
            )
        if not is_agent_captured_for_person(existing, person_id):
            return _refuse(
                CAPTURE_PERSON_CREDENTIAL_REFUSED,
                f"Credential '{credential_key}' was not created by agent file capture for this person "
                "and cannot be replaced by an agent.",
                credential_key=credential_key,
            )
        existing_secret_fields = {
            name for name, field in existing.fields.items() if field.is_secret()
        }
        if existing_secret_fields != {field_name}:
            return _refuse(
                CAPTURE_FIELD_NAME_MISMATCH,
                f"Captured credential '{credential_key}' already uses field_name "
                f"{sorted(existing_secret_fields)!r}, not {field_name!r}; use the same field_name or a "
                "different credential_key.",
                credential_key=credential_key,
                existing_field_names=sorted(existing_secret_fields),
                requested_field_name=field_name,
            )
        return None

    return _guard


def _delete_source_file(
    located: LocatedTaskFile,
    *,
    expected_identity: tuple[int, int, int, int],
    credential_key: str,
    person_id: str,
) -> tuple[bool, str | None]:
    try:
        removed = safe_open.remove_file(
            located.root,
            located.parts,
            expected_identity=expected_identity,
        )
    except UnsafePath as exc:
        LOGGER.warning(
            "Captured credential was stored, but its source file could not be removed safely. "
            "event=credential_capture_delete_failed credential_key=%s person_id=%s reason=%s",
            credential_key,
            person_id,
            exc.reason,
        )
        return False, exc.reason
    except OSError as exc:
        LOGGER.warning(
            "Captured credential was stored, but its source file could not be removed. "
            "event=credential_capture_delete_failed credential_key=%s person_id=%s error=%s",
            credential_key,
            person_id,
            type(exc).__name__,
        )
        return False, "delete_failed"
    if not removed:
        LOGGER.warning(
            "Captured credential was stored, but its source file changed before deletion and was left in place. "
            "event=credential_capture_delete_skipped_changed credential_key=%s person_id=%s",
            credential_key,
            person_id,
        )
        return False, "source_file_changed"
    return True, None


def _deliver_capture_notice(
    access_gate: BrokerAccessGate,
    notice: AccessNoticeV1 | None,
    *,
    credential_key: str,
    person_id: str,
) -> tuple[bool, str]:
    if notice is None:
        return False, NOTICE_STATUS_OFF
    if access_gate.links is None:
        LOGGER.warning(
            "Captured credential was stored, but the Inbox notice could not be sent because the "
            "broker access store is unavailable. "
            "event=credential_capture_notice_unavailable credential_key=%s person_id=%s notice_id=%s",
            credential_key,
            person_id,
            notice.notice_id,
        )
        return False, NOTICE_STATUS_FAILED
    try:
        delivered = bool(access_gate.links.create_notice_if_absent(notice))
    except Exception as exc:  # noqa: BLE001 - never undo a committed capture on notice failure
        LOGGER.warning(
            "Captured credential was stored, but sending the Inbox notice failed. "
            "event=credential_capture_notice_failed credential_key=%s person_id=%s notice_id=%s error=%s",
            credential_key,
            person_id,
            notice.notice_id,
            type(exc).__name__,
        )
        return False, NOTICE_STATUS_FAILED
    if not delivered:
        LOGGER.warning(
            "Captured credential was stored, but the Inbox notice was not recorded. "
            "event=credential_capture_notice_not_recorded credential_key=%s person_id=%s notice_id=%s",
            credential_key,
            person_id,
            notice.notice_id,
        )
        return False, NOTICE_STATUS_FAILED
    return True, NOTICE_STATUS_SENT


def _plan_capture_notice(
    access_gate: BrokerAccessGate,
    *,
    person_id: str,
    agent_type: str,
    validated: _ValidatedCaptureParams,
    now: datetime,
) -> AccessNoticeV1 | ToolResult | None:
    """Build and validate the Inbox notice pre-write so metadata errors never follow a commit."""

    if not notify_on_agent_capture_enabled():
        return None
    try:
        return _build_capture_notice(
            team_id=access_gate.team_id,
            person_id=person_id,
            agent_type=agent_type,
            validated=validated,
            now=now,
        )
    except ValidationError as exc:
        return _refuse(
            CAPTURE_INVALID_PARAMS,
            f"Could not construct the capture notice from the provided metadata: {exc.errors()[0]['msg']}.",
            credential_key=validated.credential_key,
        )


def _store_captured_secret(
    validated: _ValidatedCaptureParams,
    secret_value: str,
    *,
    person_id: str,
    team_id: str | None,
    agent_type: str,
    now: datetime,
) -> ToolResult:
    request_payload = {
        "credential_key": validated.credential_key,
        "display_name": validated.display_name,
        "purpose": validated.purpose,
        "store_target": "sealbox",
        "fields": [
            {
                "name": validated.field_name,
                "kind": "credential_file",
                "visibility": "secret",
                "value_source": CAPTURE_APPROVAL_SOURCE,
                "required": True,
            }
        ],
    }
    approval_record = {
        "status": "captured",
        "source": CAPTURE_APPROVAL_SOURCE,
        "captured_by_agent_type": agent_type,
        "captured_for_person_id": person_id,
        "captured_at": format_utc_timestamp(now),
    }
    result = collect_credential(
        request_payload,
        {validated.field_name: secret_value},
        scope=CredentialOwnerScope.user(person_id),
        expires_at=validated.expires_at,
        allow_expiry_change=True,
        overwrite_guard=_make_overwrite_guard(
            credential_key=validated.credential_key,
            field_name=validated.field_name,
            person_id=person_id,
            team_id=team_id,
        ),
        approval=approval_record,
        now=now,
    )
    if result.status == "success":
        return result
    content = dict(result.content or {})
    content["source_file_deleted"] = False
    error_text = str(result.error or "Failed to store captured credential.").strip()
    if _FILE_LEFT_IN_PLACE_NOTE not in error_text:
        error_text = f"{error_text} {_FILE_LEFT_IN_PLACE_NOTE}"
    return ToolResult(status="error", error=error_text, content=content)


def capture_file_credential(
    params: Mapping[str, Any],
    *,
    context: ToolContext,
    access_gate: BrokerAccessGate | None = None,
    instance_root: str | None = None,
    now: datetime | None = None,
) -> ToolResult:
    """Store a task file's secret bytes as a temporary Sealbox credential for the verified person."""

    person_id = str(context.canonical_user_id or "").strip()
    principal_type = str(context.principal_type or "").strip().lower()
    if not person_id or principal_type != "user":
        return _refuse(
            CAPTURE_PERSON_REQUIRED,
            "credential.capture_file requires a verified person context and cannot run for system work.",
        )

    current_now = now if now is not None else datetime.now(timezone.utc)
    validated = _validate_params(params, now=current_now)
    if isinstance(validated, ToolResult):
        return validated

    agent_type = str(context.named_agent_type or context.agent_id or "agent").strip() or "agent"
    if access_gate is None:
        LOGGER.warning(
            "Refusing credential.capture_file because no BrokerAccessGate was provided. "
            "event=credential_capture_access_gate_missing credential_key=%s person_id=%s agent_type=%s",
            validated.credential_key,
            person_id,
            agent_type,
        )
        return _refuse(
            CAPTURE_ACCESS_GATE_REQUIRED,
            "credential.capture_file requires the broker access gate to enforce folder access.",
            credential_key=validated.credential_key,
        )

    team_id = str(access_gate.team_id or context.team_id or "").strip() or None

    planned_notice = _plan_capture_notice(
        access_gate,
        person_id=person_id,
        agent_type=agent_type,
        validated=validated,
        now=current_now,
    )
    if isinstance(planned_notice, ToolResult):
        return planned_notice

    resolved_root = instance_root if instance_root is not None else os.getenv("TEAM_INSTANCE_ROOT")
    try:
        located = locate_task_file(
            validated.path,
            instance_root=resolved_root,
            person_id=person_id,
        )
    except TaskFileRejected as exc:
        return _refuse(
            CAPTURE_PATH_OUTSIDE_SCOPE,
            str(exc),
            credential_key=validated.credential_key,
        )

    folder_refusal = _check_folder_access(
        access_gate,
        context,
        located,
        credential_key=validated.credential_key,
    )
    if folder_refusal is not None:
        return folder_refusal

    loaded = _read_secret_file(
        located,
        max_bytes=credential_max_file_bytes(),
        credential_key=validated.credential_key,
    )
    if isinstance(loaded, ToolResult):
        return loaded
    secret_value, file_identity = loaded

    store_result = _store_captured_secret(
        validated,
        secret_value,
        person_id=person_id,
        team_id=team_id,
        agent_type=agent_type,
        now=current_now,
    )
    if store_result.status != "success":
        return store_result

    deleted, delete_reason = _delete_source_file(
        located,
        expected_identity=file_identity,
        credential_key=validated.credential_key,
        person_id=person_id,
    )
    notice_sent, notice_status = _deliver_capture_notice(
        access_gate,
        planned_notice,
        credential_key=validated.credential_key,
        person_id=person_id,
    )

    LOGGER.info(
        "Stored captured file credential. event=credential_file_captured "
        "credential_key=%s field_name=%s person_id=%s agent_type=%s expires_at=%s "
        "source_file_deleted=%s notice_sent=%s notice_status=%s",
        validated.credential_key,
        validated.field_name,
        person_id,
        agent_type,
        validated.expires_at_iso,
        deleted,
        notice_sent,
        notice_status,
    )

    result_content: dict[str, Any] = {
        "status": "credential_captured",
        "credential_key": validated.credential_key,
        "field_name": validated.field_name,
        "display_name": validated.display_name,
        "expires_at": validated.expires_at_iso,
        "write_source": store_result.content.get("write_source", "sealbox"),
        "stored_secret_fields": store_result.content.get("stored_secret_fields", [validated.field_name]),
        "metadata": store_result.content.get("metadata"),
        "source_file_deleted": deleted,
        "notice_sent": notice_sent,
        "notice_status": notice_status,
    }
    if not deleted and delete_reason:
        result_content["source_file_delete_reason"] = delete_reason
    return ToolResult(status="success", content=result_content)


__all__ = [
    "CAPTURE_ACCESS_GATE_REQUIRED",
    "CAPTURE_APPROVAL_SOURCE",
    "CAPTURE_DISPLAY_NAME_MAX_CHARS",
    "CAPTURE_EXTERNAL_SOURCE_KEY_REFUSED",
    "CAPTURE_FIELD_NAME_MISMATCH",
    "CAPTURE_FILE_EMPTY",
    "CAPTURE_FILE_INVALID_ENCODING",
    "CAPTURE_FILE_NOT_FOUND",
    "CAPTURE_FILE_TOO_LARGE",
    "CAPTURE_FILE_UNREADABLE",
    "CAPTURE_FOLDER_ACCESS_DENIED",
    "CAPTURE_FOLDER_CHECK_UNAVAILABLE",
    "CAPTURE_INVALID_PARAMS",
    "CAPTURE_NOT_REGULAR_FILE",
    "CAPTURE_PATH_OUTSIDE_SCOPE",
    "CAPTURE_PERSON_CREDENTIAL_REFUSED",
    "CAPTURE_PERSON_REQUIRED",
    "CAPTURE_PURPOSE_MAX_CHARS",
    "CAPTURE_SECRET_PARAM_REJECTED",
    "CAPTURE_SYMLINK_REFUSED",
    "CAPTURE_TEAM_KEY_REFUSED",
    "CAPTURE_TEAM_SCOPE_REFUSED",
    "CAPTURE_UNEXPECTED_PARAMS",
    "DEFAULT_CAPTURE_FIELD_NAME",
    "NOTICE_STATUS_FAILED",
    "NOTICE_STATUS_OFF",
    "NOTICE_STATUS_SENT",
    "capture_file_credential",
    "is_agent_captured_for_person",
]

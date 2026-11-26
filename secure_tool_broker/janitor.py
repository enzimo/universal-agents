"""The broker's single periodic background janitor.

Runs once about 10 minutes after broker start and then every 6 hours, executing
three isolated maintenance jobs:

1. **Expired temporary credentials and link revocation** across all owner
   scopes: under the catalog key lock, ``credential_catalog.delete_credential``
   runs the ``before_delete`` hook (``revoke_links_for_expired_credential``,
   also called by team transfer import when a transferred temporary credential
   expired before import and by the hand-deletion flow for expired credentials)
   before removing anything. For each active account or gate link referencing
   the credential, it revokes the link first with the team's system role actor
   (never a fake person) and, only when that revocation actually changed the
   link, creates one deterministic Inbox notice (keyed by team, link, credential
   key, and ``expires_at``, naming the tool, whether a usable shared team
   account still applies or no linked account remains, and the exact ``/link``
   command; team and gate links notify team administrators via
   ``recipient_roles=(PERSON_ROLE_ADMIN,)``). No notice is sent when no active
   link used the credential or when the person already relinked it. Only after
   all link revocations and notices succeed does ``delete_credential`` remove
   all Sealbox versions and compare-and-delete the catalog row (reloading and
   retrying if the row was rewritten while still expired; a concurrently renewed
   or replaced credential is never deleted, and any failure leaves the catalog
   row for the next run to finish with one notice).
2. **Per-tenant Sealbox expired-secret cleanup**: runs
   ``sealbox-cli secret cleanup-expired --output json`` with a 90-second
   timeout (counting timeouts separately as unknown outcomes) for each recorded
   tenant mapping using an isolated tenant environment built exclusively by
   ``sealbox_tenants.tenant_environment_for_mapping`` (never inheriting the
   broker's root token, ``SEALBOX_TOKEN``, or ``HOME`` config).
3. **Periodic tool setup re-check**: runs ``tool_setup_recheck.recheck_due``
   unchanged (``security_layer.tool_setup_recheck_days == 0`` still turns it off).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Mapping

from access_control.entities import AGENT_ROLE_SYSTEM, role_id
from access_control.people import PERSON_ROLE_ADMIN
from access_control.tool_setup_states import (
    HOW_TEAM_LINK,
    SOURCE_PERIODIC_CHECK,
    ToolSetupStates,
    safely,
)
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccessNoticeV1,
    AccountLinkStatus,
    AccountLinkV1,
    LinkOwnerKind,
    LinkPurpose,
    SubjectKind,
    SubjectRefV1,
)
from secure_tool_broker.credential_catalog import CatalogCredential, delete_credential
from secure_tool_broker.credential_catalog_store import (
    CredentialCatalogStore,
    CredentialOwnerScope,
    SealboxTenantMapping,
    resolve_catalog_db_path,
)
from secure_tool_broker.credential_expiry import is_expired, parse_utc_timestamp
from secure_tool_broker.redaction import redact_text
from secure_tool_broker.sealbox_cli import (
    SEALBOX_TIMEOUT_ERROR_CODE,
    first_json_value,
    run_sealbox,
)
from secure_tool_broker.sealbox_tenants import tenant_environment_for_mapping
from secure_tool_broker.tool_setup_recheck import RecheckReport, recheck_due
from secure_tool_broker.tool_setup_tracking import note_link_missing
from secure_tools.chat_commands import link_command
from secure_tools.providers import ToolResult

LOGGER = logging.getLogger(__name__)

# Both timings are module constants (no new configuration settings).
INITIAL_DELAY_SECONDS: Final[float] = 600.0
JANITOR_INTERVAL_SECONDS: Final[float] = 6 * 3600.0
# `sealbox-cli secret cleanup-expired` uses a 10s connect timeout + 60s request
# timeout (~70s total); allow headroom so the broker does not kill a live call early.
SEALBOX_CLEANUP_TIMEOUT_SECONDS: Final[int] = 90

_NOTICE_ID_PREFIX: Final = "credential_expired_"
_UNSUPPORTED_SUBCOMMAND_MARKERS: Final = (
    "unrecognized subcommand",
    "unknown subcommand",
)


@dataclass(frozen=True)
class ExpiredCredentialsReport:
    scanned: int = 0
    expired: int = 0
    deleted: int = 0
    skipped_changed: int = 0
    failed: int = 0
    links_revoked: int = 0
    notices_sent: int = 0
    link_failures: int = 0


@dataclass(frozen=True)
class SealboxCleanupReport:
    tenants_scanned: int = 0
    tenants_cleaned: int = 0
    tenants_skipped: int = 0
    tenants_failed: int = 0
    tenants_timed_out: int = 0
    secrets_deleted: int = 0
    unsupported_cli: bool = False


@dataclass(frozen=True)
class JanitorReport:
    credentials: ExpiredCredentialsReport = field(default_factory=ExpiredCredentialsReport)
    sealbox_cleanup: SealboxCleanupReport = field(default_factory=SealboxCleanupReport)
    tool_setup_recheck: RecheckReport = field(default_factory=RecheckReport)
    job_errors: tuple[str, ...] = ()


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now.astimezone(timezone.utc)


def _system_actor(team_id: str) -> SubjectRefV1:
    """Broker/system role subject for automated link revocation (never a fake person)."""

    return SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(team_id, AGENT_ROLE_SYSTEM))


def _tool_display_name(gate: Any, tool_id: str) -> str:
    tool = gate.catalog.tools.get(tool_id) if getattr(gate, "catalog", None) is not None else None
    return tool.display_name if tool is not None else tool_id


def _link_matches_scope(link: AccountLinkV1, scope: CredentialOwnerScope, credential_key: str) -> bool:
    if link.credential_key != credential_key:
        return False
    if scope.scope_kind == "user":
        return link.owner_kind is LinkOwnerKind.PERSON and link.owner_id == scope.owner_id
    if scope.scope_kind == "team":
        return link.owner_kind is LinkOwnerKind.TEAM and link.owner_id == scope.owner_id
    return False


def _expired_link_notice_id(
    team_id: str,
    link: AccountLinkV1,
    *,
    expires_at: str | None = None,
) -> str:
    parts = [team_id, link.link_id, link.credential_key]
    if expires_at:
        parts.append(str(expires_at))
    raw = "|".join(parts)
    return _NOTICE_ID_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


def _build_revoked_link_notice(
    gate: Any,
    link: AccountLinkV1,
    *,
    fallback_team_link: AccountLinkV1 | None,
    now: datetime,
    expires_at: str | None = None,
) -> AccessNoticeV1:
    tool_name = _tool_display_name(gate, link.tool_id)
    command = link_command(link.tool_id, link.owner_kind, purpose=link.purpose)
    if link.owner_kind is LinkOwnerKind.TEAM:
        if link.purpose is LinkPurpose.GATE:
            title = f"{tool_name} gate credential expired"
            summary = (
                f"The team's temporary gate credential for {tool_name} ({link.credential_key}) expired and was "
                f"deleted, so its gate link was removed. {tool_name} cannot run until a team administrator or "
                f"owner links a new gate credential with {command}."
            )
        else:
            title = f"Team credential for {tool_name} expired"
            summary = (
                f"The team's temporary credential for {tool_name} ({link.credential_key}) expired and was "
                f"deleted, so the team account link was removed. A team administrator or owner can link a new "
                f"team account with {command}."
            )
        return AccessNoticeV1(
            notice_id=_expired_link_notice_id(gate.team_id, link, expires_at=expires_at),
            team_id=gate.team_id,
            kind=AccessNoticeKind.CREDENTIAL_EXPIRED,
            recipient_person_id=None,
            recipient_roles=(PERSON_ROLE_ADMIN,),
            title=title,
            summary=summary,
            created_at_utc=now,
        )

    title = f"Temporary credential for {tool_name} expired"
    if fallback_team_link is not None:
        summary = (
            f"Your temporary credential for {tool_name} ({link.credential_key}) expired and was deleted, so "
            f"its account link was removed. Your work now uses the team's shared account for {tool_name}. "
            f"To link your own account again, run {command}."
        )
    else:
        summary = (
            f"Your temporary credential for {tool_name} ({link.credential_key}) expired and was deleted, so "
            f"its account link was removed. {tool_name} now has no linked account for your work. "
            f"Run {command} to link a new credential."
        )
    return AccessNoticeV1(
        notice_id=_expired_link_notice_id(gate.team_id, link, expires_at=expires_at),
        team_id=gate.team_id,
        kind=AccessNoticeKind.CREDENTIAL_EXPIRED,
        recipient_person_id=link.owner_id,
        recipient_roles=(),
        title=title,
        summary=summary,
        created_at_utc=now,
    )


def _needs_retry_notice_for_system_revoked_link(
    link: AccountLinkV1,
    *,
    actor: SubjectRefV1,
    expires_at: str | None,
) -> bool:
    """Return True when ``link`` was already revoked by the broker system actor for this expiry."""

    if link.status is not AccountLinkStatus.REVOKED or link.revoked_by != actor:
        return False
    if expires_at is None:
        return True
    if link.revoked_at_utc is None:
        return False
    try:
        return link.revoked_at_utc >= parse_utc_timestamp(expires_at)
    except Exception:
        return False


def _update_revoked_personal_link_setup_state(
    gate: Any,
    link: AccountLinkV1,
    *,
    fallback_team_link: AccountLinkV1 | None,
) -> None:
    if link.owner_kind is not LinkOwnerKind.PERSON or link.purpose is not LinkPurpose.ACCOUNT:
        return
    # Do not overwrite setup state if the person already created a replacement link.
    replacement = gate.links.active_link(
        gate.team_id, link.tool_id, LinkOwnerKind.PERSON, link.owner_id, purpose=LinkPurpose.ACCOUNT
    )
    if replacement is not None:
        return
    if fallback_team_link is not None:
        tool_id = link.tool_id
        person_id = link.owner_id

        def _record_team_fallback() -> None:
            ToolSetupStates(gate.links).note_ready(
                gate.team_id,
                person_id,
                tool_id,
                how=HOW_TEAM_LINK,
                source=SOURCE_PERIODIC_CHECK,
            )

        safely(
            _record_team_fallback,
            what="janitor_link_fallback_state",
            tool_id=tool_id,
            person_id=person_id,
        )
    else:
        note_link_missing(gate.links, gate.team_id, link.owner_id, link.tool_id)


def revoke_links_for_expired_credential(
    gate: Any,
    scope: CredentialOwnerScope,
    credential_key: str,
    *,
    expires_at: str | None = None,
    now: datetime | None = None,
) -> tuple[int, int, int]:
    """Revoke active links referencing ``credential_key`` and notify each revoked link's owner.

    Shared by the broker janitor (before deleting the Sealbox records and catalog
    row, so any failure leaves the catalog row as a retry marker), by the hand-deletion
    flow for expired credentials, and by team transfer import (when an imported
    temporary credential expired before import).

    Order per link: revoke the active link first with the system role actor, and
    only when this revocation actually changed the link (so a person who relinked
    concurrently never gets a false expiry notice) create the deterministic Inbox
    notice (including ``expires_at`` in its id) and update tool setup state. If
    notice creation fails after revocation, the next retry sees the system-revoked
    link and delivers the missing notice once without duplicating already-sent notices.

    Returns ``(links_revoked, notices_sent, link_failures)``.
    """

    if gate is None or getattr(gate, "links", None) is None:
        return 0, 0, 0
    if scope.scope_kind not in {"user", "team"}:
        return 0, 0, 0
    moment = _utc_now(now)
    try:
        all_links = gate.links.list_links(gate.team_id, purpose=None, include_revoked=True)
    except Exception as exc:  # noqa: BLE001 - caller keeps catalog row for retry
        LOGGER.warning(
            "Broker janitor failed to list active links for an expired credential. "
            "event=broker_janitor_link_revoke_failed scope_kind=%s owner_id=%s "
            "credential_key=%s error=%s",
            scope.scope_kind,
            scope.owner_id,
            credential_key,
            type(exc).__name__,
        )
        return 0, 0, 1
    actor = _system_actor(gate.team_id)
    matching_active = [
        link
        for link in all_links
        if link.status is AccountLinkStatus.ACTIVE and _link_matches_scope(link, scope, credential_key)
    ]
    matching_retry_notice = [
        link
        for link in all_links
        if _link_matches_scope(link, scope, credential_key)
        and _needs_retry_notice_for_system_revoked_link(link, actor=actor, expires_at=expires_at)
    ]
    if not matching_active and not matching_retry_notice:
        return 0, 0, 0

    revoked_count = 0
    notices_count = 0
    failures = 0
    for link in matching_active:
        try:
            revoked = gate.links.revoke_link(link.link_id, actor=actor, at=moment)
            changed = (
                revoked.status is AccountLinkStatus.REVOKED
                and revoked.revoked_by == actor
                and revoked.revoked_at_utc == moment
            )
            if not changed:
                # The person relinked or another actor revoked this link before we could;
                # do not send a misleading expiry notice or overwrite setup state.
                continue
            revoked_count += 1
            fallback_team_link: AccountLinkV1 | None = None
            if link.owner_kind is LinkOwnerKind.PERSON and link.purpose is LinkPurpose.ACCOUNT:
                fallback_team_link, _ = gate.resolve_account_link(link.tool_id, None, now=moment)
            notice = _build_revoked_link_notice(
                gate,
                link,
                fallback_team_link=fallback_team_link,
                now=moment,
                expires_at=expires_at,
            )
            if gate.links.create_notice_if_absent(notice):
                notices_count += 1
            _update_revoked_personal_link_setup_state(gate, link, fallback_team_link=fallback_team_link)
        except Exception as exc:  # noqa: BLE001 - isolate each link failure
            failures += 1
            LOGGER.warning(
                "Broker janitor failed to revoke an expired credential's link or send its notice. "
                "event=broker_janitor_link_revoke_failed link_id=%s tool_id=%s owner_kind=%s "
                "owner_id=%s credential_key=%s error=%s",
                link.link_id,
                link.tool_id,
                link.owner_kind.value,
                link.owner_id,
                link.credential_key,
                type(exc).__name__,
            )
    for link in matching_retry_notice:
        try:
            fallback_team_link = None
            if link.owner_kind is LinkOwnerKind.PERSON and link.purpose is LinkPurpose.ACCOUNT:
                fallback_team_link, _ = gate.resolve_account_link(link.tool_id, None, now=moment)
            notice = _build_revoked_link_notice(
                gate,
                link,
                fallback_team_link=fallback_team_link,
                now=moment,
                expires_at=expires_at,
            )
            if gate.links.create_notice_if_absent(notice):
                notices_count += 1
                _update_revoked_personal_link_setup_state(gate, link, fallback_team_link=fallback_team_link)
        except Exception as exc:  # noqa: BLE001 - isolate each link failure
            failures += 1
            LOGGER.warning(
                "Broker janitor failed to deliver a pending notice for a revoked expired credential link. "
                "event=broker_janitor_link_revoke_failed link_id=%s tool_id=%s owner_kind=%s "
                "owner_id=%s credential_key=%s error=%s",
                link.link_id,
                link.tool_id,
                link.owner_kind.value,
                link.owner_id,
                link.credential_key,
                type(exc).__name__,
            )
    return revoked_count, notices_count, failures


def revoke_expired_credential_links_before_delete(
    gate: Any,
    scope: CredentialOwnerScope,
    credential_key: str,
    credential: CatalogCredential,
    *,
    now: datetime | None = None,
) -> ToolResult | None:
    """Hook for hand-deleting an already-expired credential so its links are revoked with one notice."""

    if credential.expires_at is None:
        return None
    moment = _utc_now(now)
    try:
        if not is_expired(credential.expires_at, now=moment):
            return None
    except Exception:
        return None
    _revoked, _noticed, failures = revoke_links_for_expired_credential(
        gate,
        scope,
        credential_key,
        expires_at=credential.expires_at,
        now=moment,
    )
    if failures > 0:
        return ToolResult(
            status="error",
            error="Failed to revoke links or send notices for expired credential; keeping row for retry.",
            content={
                "error_code": "janitor_link_revocation_failed",
                "credential_key": credential_key,
            },
        )
    return None


_MAX_CONCURRENT_DELETE_RETRIES: Final[int] = 3


def clean_expired_credentials(
    gate: Any,
    *,
    now: datetime | None = None,
) -> ExpiredCredentialsReport:
    """Revoke links/notify, delete Sealbox records, and compare-and-delete expired catalog rows."""

    db_path = resolve_catalog_db_path()
    if not db_path.is_file():
        return ExpiredCredentialsReport()
    moment = _utc_now(now)
    store = CredentialCatalogStore(db_path)
    rows, unreadable_rows = store.scan_all_credentials()
    expired = 0
    deleted = 0
    skipped_changed = 0
    failed = 0
    links_revoked = 0
    notices_sent = 0
    link_failures = 0

    for bad in unreadable_rows:
        failed += 1
        LOGGER.warning(
            "Broker janitor skipped an unreadable credential catalog row. "
            "event=broker_janitor_credential_row_unreadable scope_kind=%s owner_id=%s "
            "credential_key=%s error=%s",
            bad.scope_kind,
            bad.owner_id,
            bad.credential_key,
            bad.error,
        )

    for row in rows:
        raw_expires = row.credential.get("expires_at")
        if raw_expires is None or (isinstance(raw_expires, str) and not raw_expires.strip()):
            continue
        try:
            if not is_expired(raw_expires, now=moment):
                continue
        except Exception as exc:  # noqa: BLE001 - malformed row must not stop other rows
            failed += 1
            LOGGER.warning(
                "Broker janitor encountered an unreadable expiry timestamp. "
                "event=broker_janitor_credential_delete_failed scope_kind=%s owner_id=%s "
                "credential_key=%s error=%s",
                row.scope.scope_kind,
                row.scope.owner_id,
                row.credential_key,
                type(exc).__name__,
            )
            continue

        expired += 1
        current_scope = row.scope
        current_key = row.credential_key
        target_updated_at = row.updated_at
        target_expires_at = str(raw_expires)

        for attempt in range(_MAX_CONCURRENT_DELETE_RETRIES):
            attempt_expires_at = target_expires_at

            def _revoke_before_delete(
                _scope: CredentialOwnerScope = current_scope,
                _key: str = current_key,
                _expires_at: str = attempt_expires_at,
            ) -> ToolResult | None:
                nonlocal links_revoked, notices_sent, link_failures
                revoked, noticed, failures = revoke_links_for_expired_credential(
                    gate,
                    _scope,
                    _key,
                    expires_at=_expires_at,
                    now=moment,
                )
                links_revoked += revoked
                notices_sent += noticed
                link_failures += failures
                if failures > 0:
                    return ToolResult(
                        status="error",
                        error="Failed to revoke links or send notices for expired credential; keeping row for retry.",
                        content={
                            "error_code": "janitor_link_revocation_failed",
                            "credential_key": _key,
                        },
                    )
                return None

            try:
                result = delete_credential(
                    {"credential_key": current_key},
                    scope=current_scope,
                    expected_updated_at=target_updated_at,
                    expected_expires_at=target_expires_at,
                    before_delete=_revoke_before_delete,
                )
            except Exception as exc:  # noqa: BLE001 - isolate each credential failure
                failed += 1
                LOGGER.warning(
                    "Broker janitor failed while deleting an expired credential. "
                    "event=broker_janitor_credential_delete_failed scope_kind=%s owner_id=%s "
                    "credential_key=%s error=%s",
                    current_scope.scope_kind,
                    current_scope.owner_id,
                    current_key,
                    type(exc).__name__,
                )
                break

            if result.status != "success":
                failed += 1
                LOGGER.warning(
                    "Broker janitor could not delete an expired credential. "
                    "event=broker_janitor_credential_delete_failed scope_kind=%s owner_id=%s "
                    "credential_key=%s error_code=%s error=%s",
                    current_scope.scope_kind,
                    current_scope.owner_id,
                    current_key,
                    (result.content or {}).get("error_code"),
                    result.error,
                )
                break

            if (result.content or {}).get("deleted"):
                deleted += 1
                break

            status = str((result.content or {}).get("status") or "")
            if status == "credential_changed" and attempt + 1 < _MAX_CONCURRENT_DELETE_RETRIES:
                reloaded = store.load_credential(current_scope, current_key)
                reloaded_expires = reloaded.credential.get("expires_at") if reloaded is not None else None
                if reloaded is not None and reloaded_expires is not None and not (
                    isinstance(reloaded_expires, str) and not reloaded_expires.strip()
                ):
                    try:
                        still_expired = is_expired(reloaded_expires, now=moment)
                    except Exception as exc:  # noqa: BLE001 - malformed row must not stop other rows
                        failed += 1
                        LOGGER.warning(
                            "Broker janitor encountered an unreadable expiry timestamp on reload. "
                            "event=broker_janitor_credential_delete_failed scope_kind=%s owner_id=%s "
                            "credential_key=%s error=%s",
                            current_scope.scope_kind,
                            current_scope.owner_id,
                            current_key,
                            type(exc).__name__,
                        )
                        break
                    if still_expired:
                        target_updated_at = reloaded.updated_at
                        target_expires_at = str(reloaded_expires)
                        continue

            skipped_changed += 1
            LOGGER.info(
                "Skipped expired-credential deletion because the entry changed concurrently. "
                "event=broker_janitor_credential_skipped_changed scope_kind=%s owner_id=%s credential_key=%s",
                current_scope.scope_kind,
                current_scope.owner_id,
                current_key,
            )
            break

    report = ExpiredCredentialsReport(
        scanned=len(rows) + len(unreadable_rows),
        expired=expired,
        deleted=deleted,
        skipped_changed=skipped_changed,
        failed=failed,
        links_revoked=links_revoked,
        notices_sent=notices_sent,
        link_failures=link_failures,
    )
    LOGGER.info(
        "Broker janitor finished expired credential sweep. "
        "event=broker_janitor_credentials_finished scanned=%s expired=%s deleted=%s "
        "skipped_changed=%s failed=%s links_revoked=%s notices_sent=%s link_failures=%s",
        report.scanned,
        report.expired,
        report.deleted,
        report.skipped_changed,
        report.failed,
        report.links_revoked,
        report.notices_sent,
        report.link_failures,
    )
    return report


def _is_unsupported_cleanup_subcommand(returncode: int, stderr: str) -> bool:
    if returncode != 2:
        return False
    lowered = (stderr or "").lower()
    return any(marker in lowered for marker in _UNSUPPORTED_SUBCOMMAND_MARKERS)


def _extract_deleted_count(parsed: Mapping[str, Any] | None) -> int | None:
    if not isinstance(parsed, Mapping):
        return None
    value = parsed.get("deleted_count")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _is_sealbox_timeout(result: ToolResult) -> bool:
    content = result.content or {}
    if isinstance(content, Mapping) and content.get("error_code") == SEALBOX_TIMEOUT_ERROR_CODE:
        return True
    return "timed out" in (result.error or "").lower()


def _validated_tenant_env(mapping: SealboxTenantMapping) -> dict[str, str] | None:
    """Resolve and verify a strictly tenant-scoped environment for ``mapping``."""

    tenant_env = tenant_environment_for_mapping(mapping)
    if isinstance(tenant_env, ToolResult):
        LOGGER.warning(
            "Skipping Sealbox expired-secret cleanup for tenant because its environment could not be built. "
            "event=broker_janitor_sealbox_tenant_skipped tenant_id=%s material_id=%s "
            "scope_kind=%s owner_id=%s error_code=%s",
            mapping.tenant_id,
            mapping.material_id,
            mapping.scope_kind,
            mapping.owner_id,
            (tenant_env.content or {}).get("error_code"),
        )
        return None
    token_file = str(tenant_env.get("SEALBOX_TOKEN_FILE") or "").strip()
    root_token_raw = str(os.getenv("SEALBOX_ROOT_TOKEN_FILE") or "").strip()
    matches_root_token = False
    if token_file and root_token_raw:
        try:
            matches_root_token = Path(token_file).expanduser().resolve() == Path(root_token_raw).expanduser().resolve()
        except OSError:
            matches_root_token = False
    token_readable = False
    if token_file and Path(token_file).is_file():
        try:
            token_readable = bool(Path(token_file).read_text(encoding="utf-8").strip())
        except OSError:
            token_readable = False
    if (
        not token_file
        or not token_readable
        or matches_root_token
        or "SEALBOX_TOKEN" in tenant_env
        or "SEALBOX_ROOT_TOKEN_FILE" in tenant_env
    ):
        LOGGER.warning(
            "Refusing to run Sealbox expired-secret cleanup without an isolated tenant token file. "
            "event=broker_janitor_sealbox_tenant_skipped tenant_id=%s material_id=%s "
            "scope_kind=%s owner_id=%s error_code=sealbox_tenant_token_missing",
            mapping.tenant_id,
            mapping.material_id,
            mapping.scope_kind,
            mapping.owner_id,
        )
        return None
    return tenant_env


def cleanup_expired_sealbox_tenants() -> SealboxCleanupReport:
    """Run ``sealbox-cli secret cleanup-expired --output json`` once per mapped tenant."""

    db_path = resolve_catalog_db_path()
    if not db_path.is_file():
        return SealboxCleanupReport()
    mappings = CredentialCatalogStore(db_path).list_sealbox_tenant_mappings()
    tenants_cleaned = 0
    tenants_skipped = 0
    tenants_failed = 0
    tenants_timed_out = 0
    secrets_deleted = 0
    unsupported_cli = False

    for mapping in mappings:
        tenant_env = _validated_tenant_env(mapping)
        if tenant_env is None:
            tenants_skipped += 1
            continue
        completed = run_sealbox(
            ["secret", "cleanup-expired", "--output", "json"],
            env=tenant_env,
            timeout_seconds=SEALBOX_CLEANUP_TIMEOUT_SECONDS,
        )
        if isinstance(completed, ToolResult):
            if _is_sealbox_timeout(completed):
                tenants_timed_out += 1
                LOGGER.warning(
                    "Sealbox expired-secret cleanup timed out for tenant; the outcome on the "
                    "Sealbox server is unknown and the next janitor run will repeat it. "
                    "event=broker_janitor_sealbox_tenant_timeout outcome=unknown tenant_id=%s "
                    "material_id=%s scope_kind=%s owner_id=%s timeout_seconds=%s",
                    mapping.tenant_id,
                    mapping.material_id,
                    mapping.scope_kind,
                    mapping.owner_id,
                    SEALBOX_CLEANUP_TIMEOUT_SECONDS,
                )
            else:
                tenants_failed += 1
                LOGGER.warning(
                    "Sealbox expired-secret cleanup failed to run for tenant. "
                    "event=broker_janitor_sealbox_tenant_failed tenant_id=%s material_id=%s "
                    "scope_kind=%s owner_id=%s error=%s",
                    mapping.tenant_id,
                    mapping.material_id,
                    mapping.scope_kind,
                    mapping.owner_id,
                    completed.error,
                )
            continue
        if completed.returncode != 0:
            if _is_unsupported_cleanup_subcommand(completed.returncode, completed.stderr or ""):
                unsupported_cli = True
                LOGGER.warning(
                    "Installed sealbox-cli does not support `sealbox-cli secret cleanup-expired`; "
                    "skipping per-tenant Sealbox expired-secret cleanup for this janitor run. "
                    "event=broker_janitor_sealbox_cleanup_unsupported tenant_id=%s material_id=%s",
                    mapping.tenant_id,
                    mapping.material_id,
                )
                break
            tenants_failed += 1
            LOGGER.warning(
                "Sealbox expired-secret cleanup exited with an error for tenant. "
                "event=broker_janitor_sealbox_tenant_failed tenant_id=%s material_id=%s "
                "scope_kind=%s owner_id=%s exit_code=%s stderr=%s",
                mapping.tenant_id,
                mapping.material_id,
                mapping.scope_kind,
                mapping.owner_id,
                completed.returncode,
                redact_text(completed.stderr or "").strip(),
            )
            continue

        parsed = first_json_value(completed.stdout or "", lambda value: isinstance(value, Mapping))
        extracted_count = _extract_deleted_count(parsed)
        if extracted_count is None:
            LOGGER.warning(
                "Sealbox expired-secret cleanup succeeded without a deleted_count field in JSON output. "
                "event=broker_janitor_sealbox_cleanup_count_missing tenant_id=%s material_id=%s "
                "scope_kind=%s owner_id=%s",
                mapping.tenant_id,
                mapping.material_id,
                mapping.scope_kind,
                mapping.owner_id,
            )
            deleted_count = 0
        else:
            deleted_count = extracted_count
        tenants_cleaned += 1
        secrets_deleted += deleted_count
        LOGGER.info(
            "Cleaned expired Sealbox records for tenant. "
            "event=broker_janitor_sealbox_tenant_cleaned tenant_id=%s material_id=%s "
            "scope_kind=%s owner_id=%s deleted=%s",
            mapping.tenant_id,
            mapping.material_id,
            mapping.scope_kind,
            mapping.owner_id,
            deleted_count,
        )

    report = SealboxCleanupReport(
        tenants_scanned=len(mappings),
        tenants_cleaned=tenants_cleaned,
        tenants_skipped=tenants_skipped,
        tenants_failed=tenants_failed,
        tenants_timed_out=tenants_timed_out,
        secrets_deleted=secrets_deleted,
        unsupported_cli=unsupported_cli,
    )
    LOGGER.info(
        "Broker janitor finished per-tenant Sealbox cleanup. "
        "event=broker_janitor_sealbox_cleanup_finished tenants_scanned=%s tenants_cleaned=%s "
        "tenants_skipped=%s tenants_failed=%s tenants_timed_out=%s secrets_deleted=%s unsupported_cli=%s",
        report.tenants_scanned,
        report.tenants_cleaned,
        report.tenants_skipped,
        report.tenants_failed,
        report.tenants_timed_out,
        report.secrets_deleted,
        report.unsupported_cli,
    )
    return report


def run_janitor_once(
    gate: Any,
    *,
    tool_setup_recheck_days: int,
    now: datetime | None = None,
) -> JanitorReport:
    """Run every broker janitor job once with per-job error isolation."""

    moment = _utc_now(now)
    job_errors: list[str] = []

    credentials_report = ExpiredCredentialsReport()
    try:
        credentials_report = clean_expired_credentials(gate, now=moment)
    except Exception as exc:  # noqa: BLE001 - isolate each janitor job
        job_errors.append("expired_credentials")
        LOGGER.warning(
            "Broker janitor job failed; continuing with remaining jobs. "
            "event=broker_janitor_job_failed job=expired_credentials error=%s",
            type(exc).__name__,
        )

    sealbox_report = SealboxCleanupReport()
    try:
        sealbox_report = cleanup_expired_sealbox_tenants()
    except Exception as exc:  # noqa: BLE001 - isolate each janitor job
        job_errors.append("sealbox_cleanup")
        LOGGER.warning(
            "Broker janitor job failed; continuing with remaining jobs. "
            "event=broker_janitor_job_failed job=sealbox_cleanup error=%s",
            type(exc).__name__,
        )

    recheck_report = RecheckReport()
    try:
        recheck_report = recheck_due(gate, every_days=tool_setup_recheck_days, now=moment)
    except Exception as exc:  # noqa: BLE001 - isolate each janitor job
        job_errors.append("tool_setup_recheck")
        LOGGER.warning(
            "Broker janitor job failed; continuing with remaining jobs. "
            "event=broker_janitor_job_failed job=tool_setup_recheck error=%s",
            type(exc).__name__,
        )

    report = JanitorReport(
        credentials=credentials_report,
        sealbox_cleanup=sealbox_report,
        tool_setup_recheck=recheck_report,
        job_errors=tuple(job_errors),
    )
    LOGGER.info(
        "Broker janitor run finished. event=broker_janitor_run_finished "
        "credentials_deleted=%s links_revoked=%s notices_sent=%s "
        "sealbox_tenants_cleaned=%s sealbox_secrets_deleted=%s "
        "recheck_checked=%s recheck_notices=%s job_errors=%s",
        report.credentials.deleted,
        report.credentials.links_revoked,
        report.credentials.notices_sent,
        report.sealbox_cleanup.tenants_cleaned,
        report.sealbox_cleanup.secrets_deleted,
        report.tool_setup_recheck.checked,
        report.tool_setup_recheck.notices,
        ",".join(report.job_errors) or "none",
    )
    return report


async def run_broker_janitor(
    gate: Any,
    *,
    tool_setup_recheck_days: int,
    initial_delay_seconds: float = INITIAL_DELAY_SECONDS,
    interval_seconds: float = JANITOR_INTERVAL_SECONDS,
) -> None:
    """The broker's single background maintenance loop: run after initial delay, then every 6 hours."""

    if tool_setup_recheck_days <= 0:
        LOGGER.info(
            "Periodic tool setup checks are off (security_layer.tool_setup_recheck_days is 0). "
            "event=tool_setup_recheck_disabled"
        )
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(run_janitor_once, gate, tool_setup_recheck_days=tool_setup_recheck_days)
        except Exception as exc:  # noqa: BLE001 - never stop the broker on a janitor failure
            LOGGER.warning(
                "Broker janitor run failed unexpectedly; continuing on schedule. "
                "event=broker_janitor_run_failed error=%s",
                type(exc).__name__,
            )
        await asyncio.sleep(interval_seconds)


__all__ = [
    "ExpiredCredentialsReport",
    "INITIAL_DELAY_SECONDS",
    "JANITOR_INTERVAL_SECONDS",
    "JanitorReport",
    "SEALBOX_CLEANUP_TIMEOUT_SECONDS",
    "SealboxCleanupReport",
    "clean_expired_credentials",
    "cleanup_expired_sealbox_tenants",
    "revoke_expired_credential_links_before_delete",
    "revoke_links_for_expired_credential",
    "run_broker_janitor",
    "run_janitor_once",
]

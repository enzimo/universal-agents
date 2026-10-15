"""Credentials an onboarding import leaves for the new instance's first start.

Why a hand-over file: only the running broker stores credentials (its default
store, Sealbox, decrypts only while it runs), and onboarding runs before the
broker exists. So onboarding's Import (``assistant_gateway.onboarding_web.team_import``)
writes the export's still-encrypted credential section, the encryption
subkey that opens it and the export's signed manifest to
``InstanceLayout.team_import_credentials_path`` (``state/``, 0600, never
reachable by agents), together with the credentials the person selected. The
passphrase itself is never written; the subkey opens only this export's
credentials. The precedent is ``state/onboarding_account_keys.json``.

At the first start the supervisor (``docker/launch_headless.py``) runs
``assistant_cli team finish-import`` once as a short child process with the
environment the broker starts from, before agents or the gateway initialize,
so it can reach the broker's transfer routes as the ``operator`` caller
(``team_transfer.broker_credentials``). It waits for the broker to answer,
stores the selected credentials exactly as ``team import-credentials`` does
(people get their Inbox notices, the import receipt records them), writes a
report next to the import's other reports and deletes the file.

Failure handling:

- The broker does not answer within the retry window: the file stays and the
  next start tries again; the error is logged with ``event=`` keys.
- The broker refuses the request as a whole: the file stays for the next
  start and the refusal is logged.
- Retryable record failures are tried again within the same window. If they
  persist, the file stays for the next start. Completed records are skipped
  through the import receipt, and the owner gets the failure report.
- Terminal refusals are reported to the exporting owner. The file is deleted
  only once no retryable credential failures remain.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from pydantic import Field, ValidationError

from team_transfer.broker_credentials import (
    SECTION_NAME,
    BrokerUnavailable,
    CredentialBundle,
    CredentialImportReport,
    import_credential_bundle,
)
from team_transfer.errors import TransferError
from team_transfer.format import TransferManifestV1, TransferModel
from team_transfer.private_files import write_private_json
from team_transfer.sections import encrypted_member

logger = logging.getLogger(__name__)

# Waits before each try while the broker starts next to this step; about three minutes in all.
DEFAULT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.0, 5.0, 10.0, 20.0, 40.0, 60.0, 60.0)


class CredentialHandoverV1(TransferModel):
    schema_version: Literal[1] = 1
    manifest: TransferManifestV1
    encryption_key_b64: str = Field(min_length=1, repr=False)
    ciphertext_b64: str = Field(min_length=1, repr=False)
    selected_keys: tuple[str, ...] = Field(min_length=1)


class _CredentialRetryExhausted(TransferError):
    """A partial report whose retryable values still need their handover."""

    def __init__(self, report: CredentialImportReport) -> None:
        super().__init__(
            "Some credentials could not be stored before the retry window ended. "
            "The handover was kept, and the next start tries again."
        )
        self.report = report


def write_credential_handover(reader: Any, layout: Any, selected_keys: Sequence[str]) -> Path | None:
    """Leave the selected credentials for the first start; None when none were selected."""

    keys = sorted(set(selected_keys))
    if not keys:
        return None
    bundle = CredentialBundle.from_reader(reader)
    handover = CredentialHandoverV1(
        manifest=bundle.manifest,
        encryption_key_b64=base64.b64encode(bundle.encryption_key).decode("ascii"),
        ciphertext_b64=base64.b64encode(bundle.ciphertext).decode("ascii"),
        selected_keys=tuple(keys),
    )
    path: Path = layout.team_import_credentials_path
    write_private_json(path, handover.model_dump(mode="json"))
    logger.info(
        "Credentials left for the first start. event=team_import_credentials_handed_over team_id=%s count=%s",
        bundle.manifest.team_id,
        len(keys),
    )
    return path


def read_credential_handover(path: Path) -> tuple[CredentialBundle, list[str]]:
    """The bundle and selected keys, checked against the export's own member digest."""

    try:
        handover = CredentialHandoverV1.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise TransferError(
            f"The credentials left by onboarding at {path} cannot be read ({type(exc).__name__})."
        ) from exc
    ciphertext = base64.b64decode(handover.ciphertext_b64, validate=True)
    member = next(
        (m for m in handover.manifest.members if m.path == encrypted_member(SECTION_NAME)),
        None,
    )
    if member is None or member.sha256 != hashlib.sha256(ciphertext).hexdigest():
        raise TransferError(
            f"The credentials left by onboarding at {path} do not match their export."
        )
    bundle = CredentialBundle(
        manifest=handover.manifest,
        encryption_key=base64.b64decode(handover.encryption_key_b64, validate=True),
        ciphertext=ciphertext,
    )
    return bundle, list(handover.selected_keys)


def import_handed_over_credentials(
    config_path: str,
    *,
    retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> CredentialImportReport | None:
    """Store the handed-over credentials through the broker; None when there are none.

    Raises ``TransferError`` when the broker never answered, refused the
    request, or could not store retryable records within the retry window;
    the file then stays for the next start.
    """

    from assistant_gateway.config import GatewayInstanceConfig

    config = GatewayInstanceConfig.load_for_process(config_path)
    path = config.layout.team_import_credentials_path
    if not path.is_file():
        return None
    bundle, selected = read_credential_handover(path)
    pending = selected

    def store_pending() -> CredentialImportReport:
        nonlocal pending
        report = import_credential_bundle(bundle, str(config_path), selected_keys=pending)
        pending = [
            outcome["key"]
            for outcome in report.outcomes
            if not outcome.get("imported") and outcome.get("retryable")
        ]
        return report

    try:
        report = _with_retries(store_pending, retry_delays, sleep)
    except _CredentialRetryExhausted as exc:
        _record_outcomes(config, bundle, exc.report)
        raise
    _record_outcomes(config, bundle, report)
    refused = [outcome for outcome in report.outcomes if not outcome.get("imported")]
    path.unlink()
    logger.info(
        "Handed-over credentials imported. event=team_import_credentials_imported team_id=%s stored=%s refused=%s",
        bundle.manifest.team_id,
        len(report.outcomes) - len(refused),
        len(refused),
    )
    return report


def _record_outcomes(config: Any, bundle: CredentialBundle, report: CredentialImportReport) -> None:
    refused = [outcome for outcome in report.outcomes if not outcome.get("imported")]
    _write_report(config.layout, bundle, report)
    if refused:
        logger.warning(
            "Some handed-over credentials were not stored. event=team_import_credentials_refused "
            "team_id=%s refused=%s",
            bundle.manifest.team_id,
            ",".join(str(outcome.get("key")) for outcome in refused),
        )
        _tell_exporter(config, bundle, refused)


def _with_retries(
    call: Callable[[], CredentialImportReport],
    retry_delays: Sequence[float],
    sleep: Callable[[float], None],
) -> CredentialImportReport:
    last: BrokerUnavailable | None = None
    outcomes: dict[str, dict[str, Any]] = {}
    notified: set[str] = set()
    for attempt, delay in enumerate(retry_delays, start=1):
        if delay:
            sleep(delay)
        try:
            report = call()
        except BrokerUnavailable as exc:
            last = exc
            logger.info(
                "The broker is not answering yet. event=team_import_credentials_waiting attempt=%s",
                attempt,
            )
            continue
        outcomes.update((outcome["key"], outcome) for outcome in report.outcomes)
        notified.update(report.notified)
        retryable = [
            outcome["key"]
            for outcome in report.outcomes
            if not outcome.get("imported") and outcome.get("retryable")
        ]
        if not retryable:
            return CredentialImportReport(list(outcomes.values()), sorted(notified))
        logger.warning(
            "Credential storage will be retried. event=team_import_credentials_retrying attempt=%s keys=%s",
            attempt,
            ",".join(retryable),
        )
    if outcomes:
        raise _CredentialRetryExhausted(
            CredentialImportReport(list(outcomes.values()), sorted(notified))
        )
    raise TransferError(
        "The broker did not answer, so the credentials onboarding left were not stored; "
        f"the next start tries again. {last}"
    )


def _write_report(layout: Any, bundle: CredentialBundle, report: CredentialImportReport) -> None:
    manifest = bundle.manifest
    write_private_json(
        layout.team_import_dir
        / f"credentials-{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}.json",
        {
            "source": {
                "team_id": manifest.team_id,
                "exported_at_utc": manifest.exported_at_utc,
                "exported_by_owner": manifest.exported_by.owner_person_id,
            },
            "outcomes": [
                {
                    key: outcome[key]
                    for key in ("key", "imported", "reason", "owner", "retryable")
                    if key in outcome
                }
                for outcome in report.outcomes
            ],
            "notified": report.notified,
        },
    )


def _tell_exporter(config: Any, bundle: CredentialBundle, refused: list[dict[str, Any]]) -> None:
    """The owner who exported the team learns which credentials did not move, and why."""

    from custom_messages.contracts.access_v2 import AccessNoticeKind, AccessNoticeV1
    from storage.access_repository import AccessRepository

    manifest = bundle.manifest
    owner = manifest.exported_by.owner_person_id
    digest = hashlib.sha256(
        f"{manifest.team_id}\0{manifest.exported_at_utc}\0refused".encode()
    ).hexdigest()[:24]
    lines = [f"- {outcome.get('key')}: {outcome.get('reason') or 'not stored'}" for outcome in refused]
    summary = (
        "The team moved to this instance, but these sign-ins and keys were not stored:\n"
        + "\n".join(lines)
    )
    try:
        AccessRepository(config.manager_storage_settings).create_notice_if_absent(
            AccessNoticeV1(
                notice_id=f"notice_credentials_refused_{digest}",
                team_id=manifest.team_id,
                kind=AccessNoticeKind.CREDENTIALS_IMPORTED,
                recipient_person_id=owner,
                title="Some sign-ins and keys did not move",
                summary=summary[:4000],
                created_at_utc=datetime.now(timezone.utc),
            )
        )
    except Exception as exc:
        logger.error(
            "Could not tell the exporting owner about credentials that were not stored. "
            "event=team_import_credentials_notice_failed person_id=%s error_type=%s",
            owner,
            type(exc).__name__,
        )


__all__ = [
    "CredentialHandoverV1",
    "DEFAULT_RETRY_DELAYS_SECONDS",
    "import_handed_over_credentials",
    "read_credential_handover",
    "write_credential_handover",
]

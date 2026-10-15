"""The ``broker_credentials`` section: the broker's stored credentials, encrypted by the broker.

Only the broker handles secret values (``AGENTS.md``). So this section never
sees them: at export it calls the running broker's transfer route with the
encryption subkey derived from the transfer passphrase and stores the
ciphertext the broker returns; ``team import-credentials`` sends that
ciphertext and the same subkey to the new instance's broker, which decrypts
and stores each record (``secure_tool_broker.credential_transfer``).

The command reaches the broker as the ``operator`` caller, derived from the
broker root secret the operator's host already holds
(``access_control.caller_tokens``); the broker lets that caller reach only the
transfer routes. The credentials are imported after the new instance's first
start because the broker's default store (Sealbox) decrypts only while it runs.

After the import, each person whose sign-ins or keys moved gets an Inbox notice
naming the owner who exported the team (owner's decision 2026-10-05). The
notice id is derived from the export, so running the import again does not
repeat it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Mapping

from pydantic import BaseModel

from team_transfer.errors import TransferError
from team_transfer.sections import (
    ExportSource,
    ImportContext,
    PlannedRecord,
    SectionExport,
    SectionFiles,
    SectionRecords,
    encrypted_member,
)

logger = logging.getLogger(__name__)

SECTION_NAME = "broker_credentials"
_TIMEOUT_SECONDS = 300.0
_ROOT_TOKEN = "SECURE_TOOL_BROKER_TOKEN"


def _record_model() -> type[BaseModel]:
    from secure_tool_broker.credential_transfer import BrokerCredentialsDocumentV1

    return BrokerCredentialsDocumentV1


class BrokerUnavailable(TransferError):
    """The broker did not answer (not started yet, or stopped); a later try may succeed."""


@dataclass(frozen=True)
class BrokerEndpoint:
    """Where the instance's broker listens, and the operator's token for it."""

    base_url: str
    token: str


def broker_endpoint(config: Any, layout: Any) -> BrokerEndpoint:
    """The broker address and an operator token, from the instance's own settings and secrets."""

    from access_control.caller_tokens import CALLER_OPERATOR, derive_caller_token
    from env_secrets import get_env_with_file_fallback
    from internal_credentials import parse_env_file
    from secure_tool_broker.defaults import resolve_secure_tool_broker_internal_base_url

    environment: Mapping[str, str]
    if config.launch.secrets_from_environment:
        environment = os.environ
    else:
        env_path = config.launch.env_file_path or layout.team_env_path
        environment = parse_env_file(env_path) if os.path.isfile(env_path) else {}
    root = get_env_with_file_fallback(_ROOT_TOKEN, env=environment)
    if not root:
        raise TransferError(
            f"{_ROOT_TOKEN} is not set for this instance, so the broker cannot be reached."
        )
    base_url = os.environ.get(
        "SECURE_TOOL_BROKER_INTERNAL_BASE_URL"
    ) or resolve_secure_tool_broker_internal_base_url(
        config.security_layer.broker_internal_base_url
    )
    return BrokerEndpoint(
        base_url=str(base_url).rstrip("/"),
        token=derive_caller_token(root, CALLER_OPERATOR),
    )


def call_broker(
    endpoint: BrokerEndpoint, route: str, payload: dict[str, Any]
) -> dict[str, Any]:
    import httpx

    try:
        response = httpx.post(
            endpoint.base_url + route,
            json=payload,
            headers={"Authorization": f"Bearer {endpoint.token}"},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise BrokerUnavailable(
            f"The secure tool broker at {endpoint.base_url} did not answer ({type(exc).__name__}). Start the instance, "
            "or export without credentials (--skip-credentials)."
        ) from exc
    if response.status_code != 200:
        raise TransferError(
            f"The broker refused the request ({response.status_code}): {response.text[:500]}"
        )
    result: dict[str, Any] = response.json()
    return result


class BrokerCredentialsSection:
    name: ClassVar[str] = SECTION_NAME
    schema_version: ClassVar[int] = 2
    encrypted: ClassVar[bool] = True
    after_start: ClassVar[bool] = True
    record_model: ClassVar[type[BaseModel]] = _record_model()

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        from secure_tool_broker.defaults import CREDENTIAL_TRANSFER_EXPORT_ROUTE

        reply = call_broker(
            broker_endpoint(source.config, source.layout),
            CREDENTIAL_TRANSFER_EXPORT_ROUTE,
            {
                "team_id": source.team_id,
                "person_ids": _people(source),
                "encryption_key_b64": base64.b64encode(
                    files.keys.encryption_key
                ).decode("ascii"),
            },
        )
        if reply.get("schema_version") != self.schema_version:
            raise TransferError(
                f"The broker wrote credentials version {reply.get('schema_version')}, not {self.schema_version}."
            )
        files.add(
            "index.json", json.dumps(reply["index"], sort_keys=True).encode("utf-8")
        )
        return SectionExport(
            ciphertext=base64.b64decode(reply["ciphertext_b64"], validate=True),
            record_count=int(reply["record_count"]),
        )

    # Imported by ``import_credentials`` after the first start, never by ``team import``.
    def plan(
        self, records: SectionRecords, context: ImportContext
    ) -> list[PlannedRecord]:
        return []

    def apply(
        self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext
    ) -> None:
        raise TransferError(
            "The broker's credentials are imported with `team import-credentials` after the first start."
        )


def _people(source: ExportSource) -> list[str]:
    """Everyone in the team who was not removed; removed people's credentials stay behind."""

    from access_control.people import MEMBERSHIP_STATUS_REMOVED
    from storage.gateway_state_repository import create_gateway_state_repository

    repository = create_gateway_state_repository(source.config.state_storage_settings)
    return sorted(
        member.canonical_user_id
        for member in repository.list_team_people(source.team_id)
        if member.membership_status != MEMBERSHIP_STATUS_REMOVED
    )


# -- team import-credentials ------------------------------------------------------------


@dataclass(frozen=True)
class CredentialImportReport:
    outcomes: list[dict[str, Any]]
    notified: list[str]


@dataclass(frozen=True)
class CredentialBundle:
    """What the broker needs to store a transfer file's credentials.

    The section's ciphertext, the encryption subkey that opens it (never the
    passphrase) and the signed manifest naming the export. It comes from an
    open transfer file (``from_reader``) or from the file onboarding leaves
    for the first start (``team_transfer.credential_handover``), so the
    archive itself need not stay on disk until the broker runs.
    """

    manifest: Any
    encryption_key: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)

    @classmethod
    def from_reader(cls, reader: Any) -> "CredentialBundle":
        if reader.manifest.section(SECTION_NAME) is None:
            raise TransferError(
                "This transfer file has no stored credentials (it was exported with --skip-credentials)."
            )
        return cls(
            manifest=reader.manifest,
            encryption_key=reader.keys.encryption_key,
            ciphertext=reader.read_member(encrypted_member(SECTION_NAME)),
        )


def import_credentials(
    reader: Any,
    config_path: str,
    *,
    selected_keys: list[str] | None = None,
    dry_run: bool = False,
) -> CredentialImportReport:
    """Send the file's encrypted credentials to the new instance's broker, then tell each person."""

    return import_credential_bundle(
        CredentialBundle.from_reader(reader),
        config_path,
        selected_keys=selected_keys,
        dry_run=dry_run,
    )


def import_credential_bundle(
    bundle: CredentialBundle,
    config_path: str,
    *,
    selected_keys: list[str] | None = None,
    dry_run: bool = False,
) -> CredentialImportReport:
    from assistant_gateway.config import GatewayInstanceConfig

    manifest = bundle.manifest
    config = GatewayInstanceConfig.load_for_process(config_path)
    if config.gateway.team_id != manifest.team_id:
        raise TransferError(
            f"This instance is team {config.gateway.team_id}; the file is team {manifest.team_id}."
        )
    from team_transfer.settings import _require_storage_connection, storage_environment

    # previous_records reads only the manifest, which the bundle carries.
    context = ImportContext(bundle, config.layout.root, config.layout, {})
    context.scratch["settings.postgres_dsn"] = _require_storage_connection(
        config, config.layout
    )
    with storage_environment(context):
        if dry_run:
            from storage.read_only import read_only_repositories

            with read_only_repositories():
                return _import_with_config(
                    bundle, config, selected_keys=selected_keys, dry_run=True
                )
        from team_transfer.batches import import_lock, previous_records
        from team_transfer.private_files import write_private_json

        with import_lock(config.layout):
            previous = previous_records(context)
            if selected_keys is None:
                raise TransferError(
                    "Select individual credentials in team review before importing."
                )
            pending = sorted(set(selected_keys) - previous.get(SECTION_NAME, set()))
            if not pending:
                return CredentialImportReport(outcomes=[], notified=[])
            report = _import_with_config(
                bundle, config, selected_keys=pending, dry_run=False
            )
            value = json.loads(
                config.layout.team_import_receipt_path.read_text(encoding="utf-8")
            )
            value["records"][SECTION_NAME] = sorted(
                previous.get(SECTION_NAME, set())
                | {r["key"] for r in report.outcomes if r.get("imported")}
            )
            write_private_json(config.layout.team_import_receipt_path, value)
            return report


def _import_with_config(bundle, config, *, selected_keys, dry_run):
    from secure_tool_broker.defaults import CREDENTIAL_TRANSFER_IMPORT_ROUTE

    manifest = bundle.manifest
    reply = call_broker(
        broker_endpoint(config, config.layout),
        CREDENTIAL_TRANSFER_IMPORT_ROUTE,
        {
            "team_id": manifest.team_id,
            "encryption_key_b64": base64.b64encode(bundle.encryption_key).decode(
                "ascii"
            ),
            "ciphertext_b64": base64.b64encode(bundle.ciphertext).decode("ascii"),
            "selected_keys": selected_keys,
            "person_ids": _people(
                ExportSource(
                    config, config.layout, config.gateway.team_id, config.instance_name
                )
            ),
            "dry_run": dry_run,
        },
    )
    outcomes = list(reply.get("outcomes", []))
    people = sorted(
        {
            o["owner"]["owner_id"]
            for o in outcomes
            if o.get("imported") and o["owner"]["kind"] == "person"
        }
    )
    notified = []
    for person in people:
        try:
            if _notify(config, manifest, person):
                notified.append(person)
        except Exception as exc:
            logger.error(
                "Credentials stored but Inbox notice failed. event=team_transfer_notice_failed person_id=%s error_type=%s",
                person,
                type(exc).__name__,
            )
            for outcome in outcomes:
                if outcome.get("imported") and outcome["owner"] == {
                    "kind": "person",
                    "owner_id": person,
                }:
                    outcome["reason"] = (
                        "stored successfully; the Inbox notice failed, see the operator log"
                    )
    return CredentialImportReport(outcomes=outcomes, notified=notified)


def _notify(config: Any, manifest: Any, person_id: str) -> bool:
    from custom_messages.contracts.access_v2 import AccessNoticeKind, AccessNoticeV1
    from storage.access_repository import AccessRepository

    digest = hashlib.sha256(
        f"{manifest.team_id}\0{manifest.exported_at_utc}\0{person_id}".encode()
    ).hexdigest()[:24]
    notice = AccessNoticeV1(
        notice_id=f"notice_credentials_imported_{digest}",
        team_id=manifest.team_id,
        kind=AccessNoticeKind.CREDENTIALS_IMPORTED,
        recipient_person_id=person_id,
        title="Your sign-ins and keys moved to this instance",
        summary=(
            f"The team moved to this instance. Your stored sign-ins, model keys and saved credentials came with it, "
            f"exported on {manifest.exported_at_utc[:10]} by {manifest.exported_by.owner_person_id}. "
            "If a tool asks you to sign in again, use /link."
        ),
        created_at_utc=datetime.now(timezone.utc),
    )
    return bool(
        AccessRepository(config.manager_storage_settings).create_notice_if_absent(
            notice
        )
    )


__all__ = [
    "BrokerCredentialsSection",
    "BrokerEndpoint",
    "BrokerUnavailable",
    "CredentialBundle",
    "CredentialImportReport",
    "broker_endpoint",
    "call_broker",
    "import_credential_bundle",
    "import_credentials",
]

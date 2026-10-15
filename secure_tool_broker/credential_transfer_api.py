"""Requests and replies of the broker's team transfer routes (operator caller only).

The operator's command sends the transfer's encryption subkey (derived from
the passphrase, ``team_transfer.crypto``), never the passphrase itself. The
broker encrypts the records as a ``SectionDocumentV1`` of the
``broker_credentials`` section, so the transfer file stays uniform, and
decrypts them again on import. Replies carry ciphertext, counts and record
names only.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field

from secure_tool_broker.credential_transfer import CredentialOutcomeV1, credential_index, export_credentials, import_credentials
from secure_tool_broker.defaults import CREDENTIAL_TRANSFER_EXPORT_ROUTE, CREDENTIAL_TRANSFER_IMPORT_ROUTE

BROKER_CREDENTIALS_SECTION = "broker_credentials"
# When this changes, register the conversion from the previous release's
# version (team_transfer.sections.SECTION_UPGRADES) in this module too: the
# broker reads the records here (decrypt_records) and does not import
# team_transfer.broker_credentials.
BROKER_CREDENTIALS_SCHEMA_VERSION = 2


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CredentialExportRequestV1(_Model):
    team_id: str = Field(min_length=1, max_length=120)
    person_ids: tuple[str, ...] = Field(default=(), max_length=10_000)
    encryption_key_b64: str = Field(min_length=1, max_length=200, repr=False)


class CredentialExportResultV1(_Model):
    ciphertext_b64: str = Field(repr=False)
    record_count: int = Field(ge=0)
    schema_version: int = BROKER_CREDENTIALS_SCHEMA_VERSION
    index: list[dict[str, Any]] = Field(default_factory=list)


class CredentialImportRequestV1(_Model):
    team_id: str = Field(min_length=1, max_length=120)
    encryption_key_b64: str = Field(min_length=1, max_length=200, repr=False)
    ciphertext_b64: str = Field(repr=False)
    selected_keys: tuple[str, ...] | None = None
    person_ids: tuple[str, ...] | None = None
    dry_run: bool = False


class CredentialImportResultV1(_Model):
    outcomes: tuple[CredentialOutcomeV1, ...] = ()


def _key(encoded: str) -> bytes:
    return base64.b64decode(encoded, validate=True)


def encrypt_records(request: CredentialExportRequestV1, records: list[dict[str, Any]]) -> CredentialExportResultV1:
    from team_transfer.crypto import canonical_json, encrypt_with_key
    from team_transfer.format import SectionDocumentV1

    document = SectionDocumentV1(
        section=BROKER_CREDENTIALS_SECTION, schema_version=BROKER_CREDENTIALS_SCHEMA_VERSION, records=records
    )
    ciphertext = encrypt_with_key(canonical_json(document.model_dump(mode="json")), _key(request.encryption_key_b64))
    from team_transfer.container import MAX_MEMBER_BYTES

    if len(ciphertext) > MAX_MEMBER_BYTES:
        raise ValueError("credential transfer exceeds the archive member limit")
    return CredentialExportResultV1(ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
                                    record_count=len(records), index=credential_index(records))


def decrypt_records(request: CredentialImportRequestV1) -> list[dict[str, Any]]:
    from team_transfer.crypto import decrypt_with_key
    from team_transfer.format import SectionDocumentV1
    from team_transfer.sections import upgrade_records

    plaintext = decrypt_with_key(base64.b64decode(request.ciphertext_b64, validate=True), _key(request.encryption_key_b64))
    document = SectionDocumentV1.model_validate_json(plaintext)
    if document.section != BROKER_CREDENTIALS_SECTION:
        raise ValueError(
            f"This broker reads {BROKER_CREDENTIALS_SECTION} version {BROKER_CREDENTIALS_SCHEMA_VERSION}, "
            f"not {document.section} version {document.schema_version}."
        )
    return upgrade_records(document, BROKER_CREDENTIALS_SCHEMA_VERSION)


async def _payload(request: web.Request) -> Any:
    from team_transfer.container import MAX_MEMBER_BYTES

    try:
        # Only these operator routes accept a transfer-sized request. Ordinary
        # broker calls keep aiohttp's normal limit. Base64 adds one third.
        return await request.clone(client_max_size=(MAX_MEMBER_BYTES * 4 // 3) + 1024**2).json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text="The request body is not JSON.") from exc


def add_transfer_routes(app: web.Application, *, team_id: str) -> None:
    """The two operator-only routes; the broker's caller check admits only the operator to them.

    Kept outside ``SecureToolBrokerService`` so the transfer adds no weight to
    that class (its size is held by an architecture test).
    """

    async def export_handler(request: web.Request) -> web.Response:
        try:
            export = CredentialExportRequestV1.model_validate(await _payload(request))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=f"Invalid credential export request ({type(exc).__name__}).") from exc
        if export.team_id != team_id:
            raise web.HTTPForbidden(text="The credential export request names another team.")
        records = await asyncio.to_thread(export_credentials, export.team_id, list(export.person_ids))
        return web.json_response(encrypt_records(export, records).model_dump(mode="json"))

    async def import_handler(request: web.Request) -> web.Response:
        try:
            payload = CredentialImportRequestV1.model_validate(await _payload(request))
            records = decrypt_records(payload)
            index = credential_index(records)
            selected = set(payload.selected_keys) if payload.selected_keys is not None else {entry["key"] for entry in index}
            if selected - {entry["key"] for entry in index}:
                raise ValueError("selected credentials are absent from this export")
            records = [raw for raw, entry in zip(records, index) if entry["key"] in selected]
            if any(raw["owner"]["kind"] == "person" and payload.person_ids is not None
                   and raw["owner"]["owner_id"] not in payload.person_ids for raw in records):
                raise ValueError("a selected credential belongs to a person absent from the new team")
        except Exception as exc:  # malformed request, wrong key or wrong version; no value is in the message
            raise web.HTTPBadRequest(text=f"Invalid credential import request ({type(exc).__name__}).") from exc
        if payload.team_id != team_id:
            raise web.HTTPForbidden(text="The credential import request names another team.")
        if payload.dry_run:
            outcomes = [CredentialOutcomeV1(key=entry["key"], owner=entry["owner"], imported=False,
                                           reason="review only; nothing stored")
                        for entry in credential_index(records)]
        else:
            outcomes = await asyncio.to_thread(import_credentials, records, team_id=payload.team_id)
        return web.json_response(CredentialImportResultV1(outcomes=tuple(outcomes)).model_dump(mode="json"))

    app.router.add_post(CREDENTIAL_TRANSFER_EXPORT_ROUTE, export_handler)
    app.router.add_post(CREDENTIAL_TRANSFER_IMPORT_ROUTE, import_handler)


__all__ = [
    "BROKER_CREDENTIALS_SCHEMA_VERSION",
    "add_transfer_routes",
    "BROKER_CREDENTIALS_SECTION",
    "CredentialExportRequestV1",
    "CredentialExportResultV1",
    "CredentialImportRequestV1",
    "CredentialImportResultV1",
    "decrypt_records",
    "encrypt_records",
]

"""Carry the broker's stored credentials to a new instance (team transfer).

The team transfer (``team_transfer``) moves a team's configuration to a fresh
instance of a newer version. The owner decided on 2026-10-05 that credentials
move with it, the team's and each person's. Only the broker handles secret
values, so this module runs inside the broker: the operator's command calls
the broker's transfer routes (operator caller only, ``access_control.caller_tokens``)
with the encryption key derived from the transfer passphrase, and the broker
returns or accepts ciphertext. The command never holds a value.

What travels, for the team and for each person the export names:

- stored sign-ins and app settings (``credential_state_store.STATE_KEY_PATHS``)
  under stable names, so a later rename of the internal keys does not change
  the transfer format;
- model provider keys by endpoint address (``model_keys.ModelKeyStore``). They
  are stored again without a new live test: they were tested when first saved
  (owner's decision 2026-10-05, the same exception as onboarding);
- credential catalog entries. Secret fields stored inside this instance
  (Sealbox) carry their values and are stored again through the catalog's
  Sealbox writer; the complete catalog row keeps its original metadata and
  saves the new references in one commit. Fields kept in an external
  password manager carry only their references, which stay valid because the
  manager sessions travel too;
- password-manager session files in the owner's private folder (Bitwarden,
  1Password, Vault, KeePass);
- the team's approved credential form variants
  (``credential_template_overrides.yaml``, ``credential_templates``), which the
  broker owns and reads from the path its own settings give.

Every outcome names records only; values never appear in results or logs.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Callable, ContextManager, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

TEAM_OWNER = "team"
PERSON_OWNER = "person"
# Sources whose values live inside this instance; others are external stores.
_INTERNAL_SOURCES = frozenset({"sealbox"})
# Password-manager sessions in an owner's private folder (credential_state.credential_owner_environment).
OWNER_FILE_FOLDERS = ("bitwarden", "onepassword", "vault", "keepass")
# Stable transfer names of the stored sign-ins and app settings. Model keys are
# carried as typed records instead (ModelKeyStore).
STATE_TRANSFER_NAMES: dict[str, str] = {
    "xurl.sessions_json": "x.cli_sessions",
    "x.sign_in_json": "x.sign_in",
    "x.oauth_client_json": "x.team_app",
    "github.token": "github.sign_in",
    "gog.token_json": "google.sign_in",
    "gog.oauth_client_credentials_json": "google.oauth_client",
}
_MODEL_KEYS_STATE = "model.endpoint_keys_json"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CredentialImportRefused(RuntimeError):
    """A destination conflict that must not be retried as a store outage."""


class CredentialOwnerV1(_Model):
    kind: Literal["team", "person"]
    owner_id: str = Field(min_length=1, max_length=240)

    @property
    def label(self) -> str:
        return TEAM_OWNER if self.kind == TEAM_OWNER else f"person:{self.owner_id}"


class StoredValueV1(_Model):
    kind: Literal["stored_value"] = "stored_value"
    owner: CredentialOwnerV1
    name: str = Field(min_length=1, max_length=120)
    value: str = Field(repr=False)


class ModelKeyV1(_Model):
    kind: Literal["model_key"] = "model_key"
    owner: CredentialOwnerV1
    entry: dict[str, Any] = Field(repr=False)


class CatalogEntryV1(_Model):
    kind: Literal["catalog_entry"] = "catalog_entry"
    owner: CredentialOwnerV1
    credential_key: str = Field(min_length=1, max_length=200)
    credential: dict[str, Any]
    secret_values: dict[str, str] = Field(default_factory=dict, repr=False)


class OwnerFileV1(_Model):
    kind: Literal["owner_file"] = "owner_file"
    owner: CredentialOwnerV1
    path: str = Field(min_length=1, max_length=500)
    content_b64: str = Field(repr=False)


class TemplateOverridesV1(_Model):
    kind: Literal["template_overrides"] = "template_overrides"
    owner: CredentialOwnerV1
    content: str


_RECORD_MODELS: dict[str, type[_Model]] = {
    "template_overrides": TemplateOverridesV1,
    "stored_value": StoredValueV1,
    "model_key": ModelKeyV1,
    "catalog_entry": CatalogEntryV1,
    "owner_file": OwnerFileV1,
}


class BrokerCredentialsDocumentV1(_Model):
    """The plaintext shape of the ``broker_credentials`` transfer section (for its tracked schema)."""

    records: list[StoredValueV1 | ModelKeyV1 | CatalogEntryV1 | OwnerFileV1 | TemplateOverridesV1] = Field(
        default_factory=list
    )


class CredentialOutcomeV1(_Model):
    """What happened to one record; ``key`` names it, never its value."""

    key: str
    owner: CredentialOwnerV1
    imported: bool
    reason: str = ""
    retryable: bool = False


def owners(team_id: str, person_ids: list[str]) -> list[CredentialOwnerV1]:
    return [CredentialOwnerV1(kind=TEAM_OWNER, owner_id=team_id)] + [
        CredentialOwnerV1(kind=PERSON_OWNER, owner_id=person) for person in sorted(set(person_ids))
    ]


def _owner_context(owner: CredentialOwnerV1) -> ContextManager[None]:
    from secure_tool_broker.credential_state import credential_owner_context

    return credential_owner_context(owner.owner_id, scope_kind="team" if owner.kind == TEAM_OWNER else "user")


def _catalog_scope(owner: CredentialOwnerV1) -> Any:
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope

    return CredentialOwnerScope.team(owner.owner_id) if owner.kind == TEAM_OWNER else CredentialOwnerScope.user(owner.owner_id)


def _model_key_store(owner: CredentialOwnerV1) -> Any:
    from secure_tool_broker.model_keys import PERSON_SCOPE, TEAM_SCOPE, ModelKeyStore

    return ModelKeyStore(owner.owner_id, scope=TEAM_SCOPE if owner.kind == TEAM_OWNER else PERSON_SCOPE)


def _owner_folder(owner: CredentialOwnerV1) -> Path | None:
    from secure_tool_broker.credential_state import (
        TEAMS_DIRNAME,
        USERS_DIRNAME,
        _owner_material_id,
        broker_credentials_root,
    )

    root = broker_credentials_root()
    if root is None:
        return None
    return root / (TEAMS_DIRNAME if owner.kind == TEAM_OWNER else USERS_DIRNAME) / _owner_material_id(owner.owner_id)


# -- export ----------------------------------------------------------------------------


def export_credentials(team_id: str, person_ids: list[str]) -> list[dict[str, Any]]:
    """Every stored credential of the team and of the named people, as transfer records."""

    records: list[dict[str, Any]] = []
    for owner in owners(team_id, person_ids):
        records.extend(record.model_dump(mode="json") for record in _export_owner(owner))
    records.extend(record.model_dump(mode="json") for record in _export_template_overrides(team_id))
    logger.warning(
        "Stored credentials exported for a team transfer. event=credentials_transfer_exported team_id=%s "
        "owners=%s records=%s",
        team_id,
        len(person_ids) + 1,
        len(records),
    )
    return records


def _export_owner(owner: CredentialOwnerV1) -> list[_Model]:
    records: list[_Model] = []
    with _owner_context(owner):
        records.extend(_export_stored_values(owner))
    records.extend(ModelKeyV1(owner=owner, entry=entry.model_dump(mode="json")) for entry in _model_key_store(owner).entries())
    records.extend(_export_catalog(owner))
    records.extend(_export_owner_files(owner))
    return records


def _export_stored_values(owner: CredentialOwnerV1) -> list[StoredValueV1]:
    from secure_tool_broker.credential_state_store import read_credential_state

    values = []
    for key, name in STATE_TRANSFER_NAMES.items():
        value = read_credential_state(key)
        if isinstance(value, str) and value:
            values.append(StoredValueV1(owner=owner, name=name, value=value))
        elif value is not None and not isinstance(value, str):
            raise RuntimeError(f"Could not read {name} for {owner.label}: {getattr(value, 'error', value)}")
    return values


def _export_catalog(owner: CredentialOwnerV1) -> list[CatalogEntryV1]:
    from secure_tool_broker.credential_catalog import load_catalog

    scope = _catalog_scope(owner)
    catalog = load_catalog(scope=scope)
    entries = []
    for key, credential in sorted(catalog.credentials.items()):
        secret_values: dict[str, str] = {}
        for field_name, spec in credential.fields.items():
            if spec.is_secret() and _internal(spec):
                secret_values[field_name] = _stored_secret_value(owner, key, field_name, catalog=catalog, scope=scope)
        entries.append(
            CatalogEntryV1(
                owner=owner, credential_key=key, credential=credential.model_dump(mode="json"), secret_values=secret_values
            )
        )
    return entries


def _stored_secret_value(owner: CredentialOwnerV1, key: str, field_name: str, *, catalog: Any, scope: Any) -> str:
    """One secret field's value as this instance's store holds it.

    ``resolve_secret_field`` returns a ``ResolvedCredentialField`` (or a
    ``ToolResult`` when no source answers) and names the source that answered.
    Only a value from this instance's store is carried: the import stores it
    again and points the field at the new copy, so a value that came from an
    environment variable or a password manager would silently become a stored
    secret. Credentials save each stored field with that one source, so any
    other answer is an anomaly and stops the export with the reason.
    """

    from secure_tool_broker.credential_catalog import ResolvedCredentialField, resolve_secret_field

    resolved = resolve_secret_field(key, field_name, catalog=catalog, scope=scope)
    if isinstance(resolved, ResolvedCredentialField) and resolved.source in _INTERNAL_SOURCES:
        return resolved.value
    reason = (
        f"its value came from {resolved.source}, not this instance's store"
        if isinstance(resolved, ResolvedCredentialField)
        else getattr(resolved, "error", None) or "no source returned a value"
    )
    raise RuntimeError(f"Could not read the credential {key} field {field_name} for {owner.label}: {reason}.")


def _export_template_overrides(team_id: str) -> list[TemplateOverridesV1]:
    from secure_tool_broker.credential_templates import resolve_template_overrides_path

    path = resolve_template_overrides_path()
    if not path.is_file():
        return []
    owner = CredentialOwnerV1(kind=TEAM_OWNER, owner_id=team_id)
    return [TemplateOverridesV1(owner=owner, content=path.read_text(encoding="utf-8"))]


def _internal(spec: Any) -> bool:
    return any(ref.source in _INTERNAL_SOURCES for ref in spec.resolution_order)


def _export_owner_files(owner: CredentialOwnerV1) -> list[OwnerFileV1]:
    from secure_tool_broker.private_files import TEMPORARY_FILE_PREFIX

    folder = _owner_folder(owner)
    if folder is None or not folder.is_dir():
        return []
    files = []
    for name in OWNER_FILE_FOLDERS:
        base = folder / name
        for path in sorted(base.rglob("*")) if base.is_dir() else []:
            if path.is_symlink():
                raise RuntimeError(f"A link stands in {owner.label}'s credentials folder; it is not followed.")
            # A concurrent save has not published these bytes as a credential yet.
            if path.name.startswith(TEMPORARY_FILE_PREFIX):
                continue
            if path.is_file():
                files.append(
                    OwnerFileV1(
                        owner=owner,
                        path=path.relative_to(folder).as_posix(),
                        content_b64=base64.b64encode(path.read_bytes()).decode("ascii"),
                    )
                )
    return files


# -- import ----------------------------------------------------------------------------


def import_credentials(records: list[dict[str, Any]], *, team_id: str) -> list[CredentialOutcomeV1]:
    """Store every record for its owner; one outcome per record, values never included."""

    outcomes = []
    for raw in records:
        record = _RECORD_MODELS[str(raw.get("kind"))].model_validate(raw)
        outcomes.append(_import_record(record, team_id))
    logger.warning(
        "Stored credentials imported from a team transfer. event=credentials_transfer_imported team_id=%s "
        "imported=%s refused=%s",
        team_id,
        sum(1 for outcome in outcomes if outcome.imported),
        sum(1 for outcome in outcomes if not outcome.imported),
    )
    return outcomes


def _import_record(record: Any, team_id: str) -> CredentialOutcomeV1:
    owner: CredentialOwnerV1 = record.owner
    key = f"{owner.label}/{record.kind}/{_record_name(record)}"
    if owner.kind == TEAM_OWNER and owner.owner_id != team_id:
        return CredentialOutcomeV1(key=key, owner=owner, imported=False, reason="it belongs to another team")
    try:
        if _already_stored(record):
            return CredentialOutcomeV1(key=key, owner=owner, imported=False,
                                       reason="already stored in this instance; kept the current credential")
        _IMPORTERS[record.kind](record)
    except Exception as exc:  # each store reports in its own way; no value is in these messages
        logger.error(
            "A transferred credential could not be stored. event=credentials_transfer_record_failed key=%s error=%s",
            key,
            type(exc).__name__,
        )
        # Invalid records cannot improve on retry. Store and service failures
        # may recover, so the first-start importer must retain their values.
        return CredentialOutcomeV1(
            key=key,
            owner=owner,
            imported=False,
            reason=f"could not be stored ({type(exc).__name__})",
            retryable=not isinstance(exc, (ValueError, KeyError, CredentialImportRefused)),
        )
    return CredentialOutcomeV1(key=key, owner=owner, imported=True)


def _already_stored(record: Any) -> bool:
    """Later batches must not replace a sign-in created after the first start."""
    if isinstance(record, StoredValueV1):
        from secure_tool_broker.credential_state_store import read_credential_state

        internal = {name: key for key, name in STATE_TRANSFER_NAMES.items()}
        with _owner_context(record.owner):
            value = read_credential_state(internal[record.name])
        if value is not None and not isinstance(value, str):
            raise RuntimeError("could not inspect the destination credential")
        return value is not None
    if isinstance(record, ModelKeyV1):
        return any(entry.address == record.entry.get("address") for entry in _model_key_store(record.owner).entries())
    if isinstance(record, CatalogEntryV1):
        from secure_tool_broker.credential_catalog import load_catalog

        return record.credential_key in load_catalog(scope=_catalog_scope(record.owner)).credentials
    if isinstance(record, OwnerFileV1):
        folder = _owner_folder(record.owner)
        if folder is None:
            return False
        parts = tuple(record.path.split("/"))
        if parts[0] not in OWNER_FILE_FOLDERS or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("unexpected path")
        return folder.joinpath(*parts).exists() or folder.joinpath(*parts).is_symlink()
    from secure_tool_broker.credential_templates import resolve_template_overrides_path

    return resolve_template_overrides_path().exists()


def _record_name(record: Any) -> str:
    if isinstance(record, StoredValueV1):
        return record.name
    if isinstance(record, ModelKeyV1):
        return str(record.entry.get("address", ""))
    if isinstance(record, CatalogEntryV1):
        return record.credential_key
    if isinstance(record, TemplateOverridesV1):
        return "credential_template_overrides"
    return str(record.path)


def credential_index(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Public selection metadata. Secret fields never leave the broker."""
    index = []
    for raw in records:
        record = _RECORD_MODELS[str(raw.get("kind"))].model_validate(raw)
        index.append({
            "key": f"{record.owner.label}/{record.kind}/{_record_name(record)}",
            "owner": record.owner.model_dump(mode="json"),
        })
    if len({entry["key"] for entry in index}) != len(index):
        raise ValueError("duplicate credential transfer keys")
    return index


def _import_stored_value(record: StoredValueV1) -> None:
    from secure_tool_broker.credential_state_store import write_credential_state

    internal = {name: key for key, name in STATE_TRANSFER_NAMES.items()}
    if record.name not in internal:
        raise KeyError(record.name)
    with _owner_context(record.owner):
        try:
            result = write_credential_state(internal[record.name], record.value, exclusive=True)
        except FileExistsError as exc:
            raise CredentialImportRefused("A destination credential appeared during import; kept it.") from exc
    if result.status != "success":
        raise RuntimeError(result.error or "write failed")


def _import_model_key(record: ModelKeyV1) -> None:
    from secure_tool_broker.model_keys import StoredModelKey

    _model_key_store(record.owner).save(StoredModelKey.model_validate(record.entry))


def _import_catalog_entry(record: CatalogEntryV1) -> None:
    from secure_tool_broker.credential_catalog import (
        CatalogCredential,
        _SECRET_CACHE,
        _normalize_field_specs,
        _save_catalog_credential,
        write_credential_to_source,
    )
    from secure_tools.providers import ToolResult

    original = CatalogCredential.model_validate(record.credential)
    scope = _catalog_scope(record.owner)
    if not record.secret_values:
        # Only external references and public fields: store the entry as it was.
        _save_catalog_credential(record.credential_key, original, scope=scope)
        return
    values = {name: value.strip() for name, value in record.secret_values.items()}
    if any(not value for value in values.values()):
        raise ValueError("An imported secret field has no value.")
    request = _collection_request(record, original)
    _require_unreferenced_import_items(record.owner, request, scope=scope)
    try:
        written = write_credential_to_source(
            write_source=request["write_source"],
            credential_key=record.credential_key,
            request_payload=request,
            values=values,
            field_specs=_normalize_field_specs(request["fields"]),
            scope=scope,
        )
        if isinstance(written, ToolResult):
            raise RuntimeError(written.error or "write failed")
        # Publish the complete record once. A temporary secret-only catalog row
        # would make a failed metadata restore look like a completed import.
        fields = dict(original.fields)
        for name in values:
            fields[name] = original.fields[name].model_copy(
                update={"resolution_order": [written.source_ref_by_field[name]]}
            )
        _save_catalog_credential(record.credential_key, original.model_copy(update={"fields": fields}), scope=scope)
    except Exception as exc:
        logger.warning(
            "A catalog credential import failed; retry the same backup to reuse its stored secret items. "
            "event=credentials_transfer_catalog_import_failed key=%s owner=%s item_id=%s error=%s",
            record.credential_key, record.owner.label, request["item_id"], type(exc).__name__,
        )
        raise
    _SECRET_CACHE.clear()


def _require_unreferenced_import_items(owner: CredentialOwnerV1, request: dict[str, Any], *, scope: Any) -> None:
    """Do not overwrite an item a destination credential already names, including a registered alias."""
    from secure_tool_broker.credential_catalog import load_catalog

    targets = {f"{request['item_id']}:{field['field_ref']}" for field in request["fields"]}
    for key, credential in load_catalog(scope=scope).credentials.items():
        for field in credential.fields.values():
            if any(_sealbox_secret_item(ref) in targets for ref in field.resolution_order):
                raise CredentialImportRefused(
                    f"An imported secret item is already used by credential {key} for {owner.label}."
                )


def _sealbox_secret_item(ref: Any) -> str | None:
    """Match the Sealbox source adapter's secret item addressing, without resolving a value."""
    if ref.source not in _INTERNAL_SOURCES or ref.field_ref.strip() in {"", "password"}:
        return None
    key = (ref.item_id or ref.item_name or "").strip()
    return key if ref.field_ref.strip() == "value" else f"{key}:{ref.field_ref.strip()}"


def _collection_request(record: CatalogEntryV1, original: Any) -> dict[str, Any]:
    """Store imported values under references bound to the exact source record.

    Catalog names allow punctuation that Sealbox's key normalization strips.
    Fingerprinting the source metadata keeps distinct catalog names separate
    and lets a failed import or restart reuse its secret items on retry. Secret
    values are excluded from the fingerprint. Indexed field references keep
    distinct names within this credential from sharing a stored value. The
    catalog's identity check still skips completed imports before this runs.
    """

    from team_transfer.crypto import canonical_json

    identity = {
        "owner": record.owner.model_dump(mode="json"),
        "credential_key": record.credential_key,
        "credential": original.model_dump(mode="json"),
        "stored_fields": sorted(record.secret_values),
    }

    return {
        "credential_key": record.credential_key,
        "display_name": original.display_name,
        "purpose": original.purpose,
        "write_source": "sealbox",
        "item_id": f"team-transfer:{hashlib.sha256(canonical_json(identity)).hexdigest()}",
        "fields": [
            {
                "name": name,
                "kind": original.fields[name].kind,
                "secret": True,
                "required": True,
                "field_ref": f"field-{index}",
            }
            for index, name in enumerate(sorted(record.secret_values))
        ],
    }


def _import_owner_file(record: OwnerFileV1) -> None:
    from access_control import safe_open
    from secure_tool_broker.credential_state import broker_credentials_root

    folder = _owner_folder(record.owner)
    root = broker_credentials_root()
    if folder is None or root is None:
        raise RuntimeError("the broker has no credentials folder")
    parts = tuple(record.path.split("/"))
    if parts[0] not in OWNER_FILE_FOLDERS or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("unexpected path")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep owner folders below the trusted root, so links in them are refused.
    owner_directory = safe_open.open_directory(root, folder.relative_to(root).parts, create=True)
    try:
        # A person's credential folder is private even with a permissive umask.
        os.fchmod(owner_directory, 0o700)
    finally:
        os.close(owner_directory)
    destination = folder.relative_to(root).parts + parts
    _publish_import_file(root, destination, base64.b64decode(record.content_b64))


def _import_template_overrides(record: TemplateOverridesV1) -> None:
    from secure_tool_broker.credential_templates import resolve_template_overrides_path

    path = resolve_template_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _publish_import_file(path.parent, (path.name,), record.content.encode("utf-8"))


def _publish_import_file(root: Path, parts: tuple[str, ...], content: bytes) -> None:
    from secure_tool_broker.private_files import write_private_file

    try:
        write_private_file(root, parts, content, exclusive=True)
    except FileExistsError as exc:
        raise CredentialImportRefused("A destination credential appeared during import; kept it.") from exc


_IMPORTERS: dict[str, Callable[[Any], None]] = {
    "template_overrides": _import_template_overrides,
    "stored_value": _import_stored_value,
    "model_key": _import_model_key,
    "catalog_entry": _import_catalog_entry,
    "owner_file": _import_owner_file,
}


def owners_with_imports(outcomes: list[CredentialOutcomeV1]) -> list[str]:
    """People who had something stored for them (they get an Inbox notice)."""

    return sorted({outcome.owner.owner_id for outcome in outcomes if outcome.imported and outcome.owner.kind == PERSON_OWNER})


__all__ = [
    "BrokerCredentialsDocumentV1",
    "CatalogEntryV1",
    "CredentialOutcomeV1",
    "CredentialOwnerV1",
    "ModelKeyV1",
    "OwnerFileV1",
    "STATE_TRANSFER_NAMES",
    "StoredValueV1",
    "TemplateOverridesV1",
    "export_credentials",
    "import_credentials",
    "owners",
    "owners_with_imports",
]

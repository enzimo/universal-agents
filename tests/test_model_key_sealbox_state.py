"""Fresh-owner reads must let the authorized write provision the tenant."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from secure_tool_broker.credential_catalog_store import CredentialCatalogStore, CredentialOwnerScope
from secure_tool_broker.credential_sources import sealbox
from secure_tool_broker.credential_state import credential_owner_context
from secure_tool_broker.credential_state_store import read_credential_state, write_credential_state
from secure_tool_broker.model_keys import MODEL_KEYS_STATE_KEY, ModelKeyStore, ModelKeyStoreError, StoredModelKey
from model_providers.endpoints import EndpointKind
from secure_tools.providers import ToolResult


@pytest.fixture
def sealbox_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "credential_source")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE", "sealbox")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES", "false")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "catalog.db"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR", str(tmp_path / "tenants"))
    return tmp_path


@pytest.mark.parametrize("scope", ["team", "user"])
def test_fresh_owner_read_is_absent_and_first_save_reaches_write(sealbox_state, monkeypatch, scope):
    owner = "fresh-owner"
    store = ModelKeyStore(owner, scope=scope)
    write = Mock(return_value=ToolResult(status="success"))
    monkeypatch.setattr("secure_tool_broker.model_keys.write_credential_state", write)

    with credential_owner_context(owner, scope_kind=scope):
        assert read_credential_state(MODEL_KEYS_STATE_KEY) is None
    assert store.entries() == ()
    catalog = CredentialCatalogStore(sealbox_state / "catalog.db")
    assert catalog.get_sealbox_tenant_mapping(CredentialOwnerScope(scope_kind=scope, owner_id=owner)) is None
    assert not (sealbox_state / "tenants").exists()

    entry = StoredModelKey(
        address="https://model.example", kind=EndpointKind.OPENAI_COMPATIBLE,
        key="fresh-save-canary", saved_at=datetime.now(timezone.utc), saved_by_person_id="owner",
    )
    store.save(entry)
    write.assert_called_once()
    assert store.get(entry.address) == entry


@pytest.mark.parametrize("error_code", [
    "sealbox_tenant_material_missing", "sealbox_tenant_provisioning_incomplete",
    "auth_required", "unexpected_storage_error",
])
def test_sealbox_read_failures_block_model_key_save(sealbox_state, monkeypatch, error_code):
    failure = ToolResult(status="error", error="Credential storage failed", content={"error_code": error_code})
    monkeypatch.setattr(sealbox.SealboxCredentialSource, "get_secret", lambda _self, _request: failure)
    write = Mock()
    monkeypatch.setattr("secure_tool_broker.model_keys.write_credential_state", write)
    store = ModelKeyStore("existing-owner")
    with credential_owner_context("existing-owner", scope_kind="team"):
        assert read_credential_state(MODEL_KEYS_STATE_KEY) is failure
    with pytest.raises(ModelKeyStoreError):
        store.entries()
    entry = StoredModelKey(
        address="https://model.example", kind=EndpointKind.OPENAI_COMPATIBLE,
        key="blocked-save-canary", saved_at=datetime.now(timezone.utc), saved_by_person_id="owner",
    )
    with pytest.raises(ModelKeyStoreError):
        store.save(entry)
    write.assert_not_called()


def test_sealbox_errors_cannot_be_hidden_by_file_fallback(sealbox_state, monkeypatch):
    failure = ToolResult(status="error", error="Tenant material is damaged", content={"error_code": "sealbox_tenant_material_missing"})
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(sealbox_state / "credentials"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES", "true")
    monkeypatch.setattr(sealbox.SealboxCredentialSource, "get_secret", lambda _self, _request: failure)
    monkeypatch.setattr("secure_tool_broker.credential_state_store._write_sealbox_state", lambda *_args: failure)
    with credential_owner_context("damaged-owner"):
        files = {"SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE": "files"}
        assert write_credential_state(MODEL_KEYS_STATE_KEY, "old-file-record", env=files).status == "success"
        assert read_credential_state(MODEL_KEYS_STATE_KEY) is failure
        assert write_credential_state(MODEL_KEYS_STATE_KEY, "replacement-record") is failure
        assert read_credential_state(MODEL_KEYS_STATE_KEY, env=files) == "old-file-record"

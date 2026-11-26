"""The broker carries its stored credentials through a team transfer, and only for the operator.

The routes run in a real broker app (aiohttp test client) with the file-based
credential backend. API keys the credential catalog keeps in Sealbox use an
in-memory stand-in for the ``sealbox-cli`` program only (``_FakeSealboxCli``);
the catalog, the Sealbox credential source and the transfer code run as in the
broker.
"""

from __future__ import annotations

import base64
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from access_control.caller_tokens import CALLER_GATEWAY, CALLER_MANAGER, CALLER_OPERATOR, agent_caller_label
from secure_tool_broker.defaults import CREDENTIAL_TRANSFER_EXPORT_ROUTE, CREDENTIAL_TRANSFER_IMPORT_ROUTE
from secure_tool_broker.service import SecureToolBrokerService
from team_transfer.crypto import new_key_derivation, derive_keys
from tests.broker_access_support import BROKER_ROOT, TEST_TEAM, caller_token, open_gate

KEY = derive_keys("correct horse battery staple", new_key_derivation(opslimit=1, memlimit=8192)).encryption_key
KEY_B64 = base64.b64encode(KEY).decode("ascii")
GITHUB_TOKEN = "gho_person_github_token_value"
X_APP = '{"client_id": "team-app", "client_secret": "team-app-secret-value"}'
MODEL_KEY = "sk-team-model-key-value"
BW_SESSION = "bitwarden-session-value"
STORED_API_KEY = "team-api-key-kept-in-sealbox"
ENV_API_KEY = "api-key-from-the-environment"


class _FakeSealboxCli:
    """``sealbox-cli secret set`` and ``secret get``, kept in memory (one per instance's store)."""

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}
        self.set_calls: list[list[str]] = []

    def __call__(self, args, *, input_text=None, env=None, timeout_seconds=30):
        action, key = list(args[:2]), args[2]
        if action == ["secret", "set"]:
            self.set_calls.append(list(args))
            self.secrets[key] = input_text or ""
            return subprocess.CompletedProcess(args, 0, stdout='{"version": 1}\n', stderr="")
        if action == ["secret", "get"] and key in self.secrets:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"value": self.secrets[key]}), stderr="")
        if action == ["secret", "get"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=f"[SealboxError] secret not found: {key}")
        raise AssertionError(f"unexpected sealbox-cli call: {args}")


def _use_sealbox(fake: _FakeSealboxCli, monkeypatch: pytest.MonkeyPatch) -> None:
    from secure_tool_broker import credential_catalog
    from secure_tool_broker.credential_sources import sealbox as sealbox_source

    for module in (credential_catalog, sealbox_source):
        monkeypatch.setattr(module, "run_sealbox", fake)
        monkeypatch.setattr(module, "sealbox_tenant_environment", lambda **_kwargs: {})
    # Resolved secrets are cached per source and owner; a new store starts empty.
    credential_catalog._SECRET_CACHE.clear()


def _store_team_api_key(
    credential_key: str = "search_api", value: str = STORED_API_KEY, *, item_id: str | None = None,
    team_id: str = TEST_TEAM,
) -> None:
    """Save an API key the way the broker's form does: the value goes into Sealbox."""

    from secure_tool_broker.credential_catalog import collect_credential
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope

    request = {
        "credential_key": credential_key,
        "display_name": "Search API",
        "store_target": "sealbox",
        "fields": [{"name": "api_key", "kind": "api_key", "visibility": "secret", "value_source": "user_input"}],
    }
    if item_id is not None:
        request["item_id"] = item_id
    result = collect_credential(request, {"api_key": value}, scope=CredentialOwnerScope.team(team_id))
    assert result.status == "success", result.error


def _use_store(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the broker's credential stores at ``root`` (file backend, own catalog database)."""

    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(root / "credential_catalog.db"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(root / "broker_credentials"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH", str(root / "credential_template_overrides.yaml"))


def _seed_old_store() -> None:
    from model_providers.endpoints import EndpointKind
    from secure_tool_broker.credential_catalog import CatalogCredential, CatalogCredentialField, CatalogSourceRef, _save_catalog_credential
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_state import credential_owner_context
    from secure_tool_broker.credential_state_store import write_credential_state
    from secure_tool_broker.model_keys import ModelKeyStore, PERSON_SCOPE, StoredModelKey, TEAM_SCOPE  # noqa: F401
    from secure_tool_broker.credential_transfer import CredentialOwnerV1, _owner_folder

    assert write_credential_state("github.token", GITHUB_TOKEN, owner_id="usr_ana").status == "success"
    with credential_owner_context(TEST_TEAM, scope_kind="team"):
        assert write_credential_state("x.oauth_client_json", X_APP).status == "success"
    ModelKeyStore(TEST_TEAM, scope=TEAM_SCOPE).save(
        StoredModelKey(
            address="https://api.openai.com/v1",
            kind=EndpointKind.OPENAI,
            key=MODEL_KEY,
            saved_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            saved_by_person_id="usr_ana",
        )
    )
    # A credential kept in an external password manager travels as references only.
    _save_catalog_credential(
        "vault_api",
        CatalogCredential(
            display_name="Vault API",
            purpose="Reads the team's vault.",
            fields={
                "token": CatalogCredentialField(
                    kind="api_key",
                    resolution_order=[CatalogSourceRef(source="bitwarden", item_id="bw-item-1", field_ref="password")],
                )
            },
        ),
        scope=CredentialOwnerScope.user("usr_ana"),
    )
    folder = _owner_folder(CredentialOwnerV1(kind="person", owner_id="usr_ana"))
    assert folder is not None
    (folder / "bitwarden").mkdir(parents=True, exist_ok=True)
    (folder / "bitwarden" / "session").write_text(BW_SESSION)
    from secure_tool_broker.credential_templates import resolve_template_overrides_path

    resolve_template_overrides_path().write_text("schema_version: 1\ntemplates: {}\n")


def test_later_credential_import_keeps_a_new_sign_in(tmp_path, monkeypatch):
    from secure_tool_broker.credential_state_store import read_credential_state, write_credential_state
    from secure_tool_broker.credential_transfer import import_credentials

    _use_store(tmp_path, monkeypatch)
    assert write_credential_state("github.token", "new-sign-in", owner_id="usr_ana").status == "success"
    result = import_credentials([{"kind": "stored_value", "owner": {"kind": "person", "owner_id": "usr_ana"},
                                  "name": "github.sign_in", "value": "old-sign-in"}], team_id=TEST_TEAM)
    assert not result[0].imported and "kept the current credential" in result[0].reason
    assert read_credential_state("github.token", owner_id="usr_ana") == "new-sign-in"


@pytest.mark.asyncio
async def test_only_the_operator_reaches_the_transfer_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_store(tmp_path, monkeypatch)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    body = {"team_id": TEST_TEAM, "person_ids": [], "encryption_key_b64": KEY_B64}
    async with TestClient(TestServer(service.build_app())) as client:
        for label in (CALLER_GATEWAY, CALLER_MANAGER, agent_caller_label("TeamArchitect")):
            headers = {"Authorization": f"Bearer {caller_token(label)}"}
            for route in (CREDENTIAL_TRANSFER_EXPORT_ROUTE, CREDENTIAL_TRANSFER_IMPORT_ROUTE):
                response = await client.post(route, json=body, headers=headers)
                assert response.status == 403, (label, route)
        operator = {"Authorization": f"Bearer {caller_token(CALLER_OPERATOR)}"}
        assert (await client.post(CREDENTIAL_TRANSFER_EXPORT_ROUTE, json=body, headers=operator)).status == 200
        # The operator reaches nothing else.
        assert (await client.get("/v1/tools", headers=operator)).status == 403
        assert (await client.post("/v1/people/credentials/delete", json={}, headers=operator)).status == 403
        other_team = {**body, "team_id": "another-team"}
        assert (await client.post(CREDENTIAL_TRANSFER_EXPORT_ROUTE, json=other_team, headers=operator)).status == 403


@pytest.mark.asyncio
async def test_credentials_round_trip_through_the_broker_without_values_leaving_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    from secure_tool_broker.credential_catalog import load_catalog
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_state import credential_owner_context
    from secure_tool_broker.credential_state_store import read_credential_state
    from secure_tool_broker.credential_transfer import CredentialOwnerV1, _owner_folder
    from secure_tool_broker.model_keys import TEAM_SCOPE, ModelKeyStore

    operator = {"Authorization": f"Bearer {caller_token(CALLER_OPERATOR)}"}
    _use_store(tmp_path / "old", monkeypatch)
    _seed_old_store()
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        exported = await client.post(
            CREDENTIAL_TRANSFER_EXPORT_ROUTE,
            json={"team_id": TEST_TEAM, "person_ids": ["usr_ana"], "encryption_key_b64": KEY_B64},
            headers=operator,
        )
        reply = await exported.json()
    text = json.dumps(reply)
    for secret in (GITHUB_TOKEN, "team-app-secret-value", MODEL_KEY, BW_SESSION):
        assert secret not in text, "only ciphertext leaves the broker"
    assert reply["record_count"] == 6

    _use_store(tmp_path / "new", monkeypatch)
    async with TestClient(TestServer(service.build_app())) as client:
        imported = await client.post(
            CREDENTIAL_TRANSFER_IMPORT_ROUTE,
            json={"team_id": TEST_TEAM, "encryption_key_b64": KEY_B64, "ciphertext_b64": reply["ciphertext_b64"]},
            headers=operator,
        )
        result = await imported.json()
        wrong_key = await client.post(
            CREDENTIAL_TRANSFER_IMPORT_ROUTE,
            json={
                "team_id": TEST_TEAM,
                "encryption_key_b64": base64.b64encode(b"x" * 32).decode(),
                "ciphertext_b64": reply["ciphertext_b64"],
            },
            headers=operator,
        )
    assert wrong_key.status == 400
    assert all(outcome["imported"] for outcome in result["outcomes"]), result
    assert {outcome["key"] for outcome in result["outcomes"]} == {
        "team/stored_value/x.team_app",
        "team/model_key/https://api.openai.com/v1",
        "person:usr_ana/stored_value/github.sign_in",
        "person:usr_ana/catalog_entry/vault_api",
        "person:usr_ana/owner_file/bitwarden/session",
        "team/template_overrides/credential_template_overrides",
    }
    assert GITHUB_TOKEN not in json.dumps(result) and GITHUB_TOKEN not in caplog.text

    assert read_credential_state("github.token", owner_id="usr_ana") == GITHUB_TOKEN
    with credential_owner_context(TEST_TEAM, scope_kind="team"):
        assert read_credential_state("x.oauth_client_json") == X_APP
    [entry] = ModelKeyStore(TEST_TEAM, scope=TEAM_SCOPE).entries()
    assert (entry.address, entry.key, entry.saved_by_person_id) == ("https://api.openai.com/v1", MODEL_KEY, "usr_ana")
    vault = load_catalog(scope=CredentialOwnerScope.user("usr_ana")).credentials["vault_api"]
    assert vault.fields["token"].resolution_order[0].item_id == "bw-item-1"
    folder = _owner_folder(CredentialOwnerV1(kind="person", owner_id="usr_ana"))
    assert folder is not None and (folder / "bitwarden" / "session").read_text() == BW_SESSION
    assert oct((folder / "bitwarden" / "session").stat().st_mode & 0o777) == "0o600"
    assert (tmp_path / "new" / "credential_template_overrides.yaml").read_text() == "schema_version: 1\ntemplates: {}\n"


@pytest.mark.asyncio
async def test_large_credential_export_can_be_imported_with_individual_selection(tmp_path, monkeypatch):
    from secure_tool_broker.credential_transfer import CredentialOwnerV1, _owner_folder
    from secure_tool_broker.credential_state_store import read_credential_state

    operator = {"Authorization": f"Bearer {caller_token(CALLER_OPERATOR)}"}
    _use_store(tmp_path / "old", monkeypatch)
    _seed_old_store()
    old_folder = _owner_folder(CredentialOwnerV1(kind="person", owner_id="usr_ana"))
    large = old_folder / "bitwarden" / "large_cache"
    large.write_bytes(b"x" * 700_000)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        exported = await client.post(CREDENTIAL_TRANSFER_EXPORT_ROUTE, headers=operator, json={
            "team_id": TEST_TEAM, "person_ids": ["usr_ana"], "encryption_key_b64": KEY_B64,
        })
        assert exported.status == 200
        reply = await exported.json()
    assert len(reply["ciphertext_b64"]) > 1024**2
    index = json.dumps(reply["index"])
    assert GITHUB_TOKEN not in index and MODEL_KEY not in index
    assert "person:usr_ana/owner_file/bitwarden/large_cache" in index
    _use_store(tmp_path / "new", monkeypatch)
    async with TestClient(TestServer(service.build_app())) as client:
        selected = "person:usr_ana/owner_file/bitwarden/large_cache"
        response = await client.post(CREDENTIAL_TRANSFER_IMPORT_ROUTE, headers=operator, json={
            "team_id": TEST_TEAM, "encryption_key_b64": KEY_B64, "ciphertext_b64": reply["ciphertext_b64"],
            "selected_keys": [selected], "person_ids": ["usr_ana"],
        })
        assert response.status == 200, await response.text()
        outcomes = (await response.json())["outcomes"]
        assert [r["key"] for r in outcomes] == [selected] and outcomes[0]["imported"]
    folder = _owner_folder(CredentialOwnerV1(kind="person", owner_id="usr_ana"))
    assert (folder / "bitwarden" / "large_cache").read_bytes() == b"x" * 700_000
    assert read_credential_state("github.token", owner_id="usr_ana") is None


@pytest.mark.asyncio
async def test_the_export_teams_records_are_stored_under_the_brokers_team_when_the_import_gave_it_a_new_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner's decision 103: a 0.9 team whose id does not fit is imported under a new one."""

    from secure_tool_broker.credential_catalog import ResolvedCredentialField, resolve_secret_field
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_state import credential_owner_context
    from secure_tool_broker.credential_state_store import read_credential_state
    from secure_tool_broker.credential_templates import resolve_template_overrides_path
    from secure_tool_broker.credential_transfer import CredentialOwnerV1, _owner_folder, export_credentials
    from secure_tool_broker.model_keys import TEAM_SCOPE, ModelKeyStore
    from team_transfer.crypto import canonical_json, encrypt_with_key
    from team_transfer.format import SectionDocumentV1

    exported_team = "team_research_assistant_pro"
    team = {"kind": "team", "owner_id": exported_team}

    def team_folder(team_id: str) -> Path:
        folder = _owner_folder(CredentialOwnerV1(kind="team", owner_id=team_id))
        assert folder is not None
        return folder

    # The kinds the broker stores with the most steps, exported by a broker of the export's team: an API key
    # kept in Sealbox (catalog entry, its secret under an item id derived from the owner), a file in the
    # team's credentials folder (a folder named from the owner) and the template overrides.
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "transfer-test")
    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    _store_team_api_key(team_id=exported_team)
    (team_folder(exported_team) / "bitwarden").mkdir(parents=True)
    (team_folder(exported_team) / "bitwarden" / "session").write_text(BW_SESSION)
    resolve_template_overrides_path().write_text("schema_version: 1\ntemplates: {}\n")
    stored_kinds = [r for r in export_credentials(exported_team, [])
                    if r["kind"] in {"catalog_entry", "owner_file", "template_overrides"}]
    assert sorted(r["kind"] for r in stored_kinds) == ["catalog_entry", "owner_file", "template_overrides"]
    records = [
        {"kind": "stored_value", "owner": team, "name": "x.team_app", "value": X_APP},
        {"kind": "model_key", "owner": team, "entry": {
            "address": "https://api.openai.com/v1", "kind": "openai", "key": MODEL_KEY,
            "saved_at": "2026-09-01T00:00:00Z", "saved_by_person_id": "usr_ana"}},
        *stored_kinds,
        {"kind": "stored_value", "owner": {"kind": "team", "owner_id": "team_other"}, "name": "x.sign_in", "value": "other"},
        {"kind": "stored_value", "owner": {"kind": "person", "owner_id": "usr_ana"}, "name": "github.sign_in",
         "value": GITHUB_TOKEN},
    ]
    team_keys = ("team/stored_value/x.team_app", "team/model_key/https://api.openai.com/v1",
                 "team/catalog_entry/search_api", "team/owner_file/bitwarden/session",
                 "team/template_overrides/credential_template_overrides")
    document = SectionDocumentV1(section="broker_credentials", schema_version=2, records=records)
    ciphertext = base64.b64encode(encrypt_with_key(canonical_json(document.model_dump(mode="json")), KEY)).decode()
    body = {"team_id": TEST_TEAM, "encryption_key_b64": KEY_B64, "ciphertext_b64": ciphertext}
    operator = {"Authorization": f"Bearer {caller_token(CALLER_OPERATOR)}"}
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())

    async def imported(store: Path, **fields: str) -> dict[str, dict]:
        _use_store(store, monkeypatch)
        _use_sealbox(_FakeSealboxCli(), monkeypatch)
        async with TestClient(TestServer(service.build_app())) as client:
            response = await client.post(CREDENTIAL_TRANSFER_IMPORT_ROUTE, json={**body, **fields}, headers=operator)
            assert response.status == 200, await response.text()
            return {outcome["key"]: outcome for outcome in (await response.json())["outcomes"]}

    # Without the export's id (an import that kept it), its team's records belong to another team, as before.
    kept = await imported(tmp_path / "kept")
    assert {kept[key]["reason"] for key in team_keys} == {"it belongs to another team"}
    assert kept["person:usr_ana/stored_value/github.sign_in"]["imported"]

    renamed = await imported(tmp_path / "renamed", source_team_id=exported_team)
    for key in team_keys:
        assert renamed[key]["imported"] and renamed[key]["owner"] == {"kind": "team", "owner_id": TEST_TEAM}, key
    other = renamed["team/stored_value/x.sign_in"]
    assert (other["imported"], other["reason"], other["owner"]["owner_id"]) == (
        False, "it belongs to another team", "team_other"), "only the export's own team follows the new id"
    assert renamed["person:usr_ana/stored_value/github.sign_in"]["imported"]
    # Checked first: reading a team's model keys below creates its (empty) folder.
    assert not team_folder(exported_team).exists(), "nothing is filed under the export's team"
    assert (team_folder(TEST_TEAM) / "bitwarden" / "session").read_text() == BW_SESSION
    with credential_owner_context(TEST_TEAM, scope_kind="team"):
        assert read_credential_state("x.oauth_client_json") == X_APP
    [entry] = ModelKeyStore(TEST_TEAM, scope=TEAM_SCOPE).entries()
    assert entry.key == MODEL_KEY and not ModelKeyStore(exported_team, scope=TEAM_SCOPE).entries()
    resolved = resolve_secret_field("search_api", "api_key", scope=CredentialOwnerScope.team(TEST_TEAM))
    assert isinstance(resolved, ResolvedCredentialField) and resolved.value == STORED_API_KEY
    assert resolve_template_overrides_path().read_text() == "schema_version: 1\ntemplates: {}\n"


def test_previous_credential_schema_is_converted_inside_the_broker(monkeypatch):
    from secure_tool_broker import credential_transfer_api as api
    from team_transfer.crypto import canonical_json, encrypt_with_key
    from team_transfer.format import SectionDocumentV1
    from team_transfer.sections import SECTION_UPGRADES

    document = SectionDocumentV1(section="broker_credentials", schema_version=1, records=[{
        "kind": "stored_value", "owner": {"kind": "team", "owner_id": TEST_TEAM},
        "name": "old_name", "value": "private-value",
    }])
    called = []

    def upgrade(records):
        called.append(True)
        return [{**record, "name": "github.sign_in"} for record in records]

    monkeypatch.setitem(SECTION_UPGRADES, ("broker_credentials", 1, api.BROKER_CREDENTIALS_SCHEMA_VERSION), upgrade)
    request = api.CredentialImportRequestV1(team_id=TEST_TEAM, encryption_key_b64=KEY_B64,
        ciphertext_b64=base64.b64encode(encrypt_with_key(canonical_json(document.model_dump(mode="json")), KEY)).decode())
    converted = api.decrypt_records(request)
    assert called == [True] and converted[0]["name"] == "github.sign_in"


@pytest.mark.asyncio
async def test_api_keys_kept_in_sealbox_move_with_the_transfer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    from secure_tool_broker.credential_catalog import ResolvedCredentialField, resolve_secret_field
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope

    operator = {"Authorization": f"Bearer {caller_token(CALLER_OPERATOR)}"}
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "transfer-test")
    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    _store_team_api_key()
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        exported = await client.post(
            CREDENTIAL_TRANSFER_EXPORT_ROUTE,
            json={"team_id": TEST_TEAM, "person_ids": [], "encryption_key_b64": KEY_B64},
            headers=operator,
        )
        assert exported.status == 200, await exported.text()
        reply = await exported.json()
    assert STORED_API_KEY not in json.dumps(reply), "only ciphertext leaves the broker"

    _use_store(tmp_path / "new", monkeypatch)
    new_sealbox = _FakeSealboxCli()
    _use_sealbox(new_sealbox, monkeypatch)
    async with TestClient(TestServer(service.build_app())) as client:
        imported = await client.post(
            CREDENTIAL_TRANSFER_IMPORT_ROUTE,
            json={"team_id": TEST_TEAM, "encryption_key_b64": KEY_B64, "ciphertext_b64": reply["ciphertext_b64"]},
            headers=operator,
        )
        outcomes = {outcome["key"]: outcome for outcome in (await imported.json())["outcomes"]}
    assert outcomes["team/catalog_entry/search_api"]["imported"], outcomes
    assert list(new_sealbox.secrets.values()) == [STORED_API_KEY]
    resolved = resolve_secret_field("search_api", "api_key", scope=CredentialOwnerScope.team(TEST_TEAM))
    assert isinstance(resolved, ResolvedCredentialField)
    assert (resolved.value, resolved.source) == (STORED_API_KEY, "sealbox")
    assert STORED_API_KEY not in caplog.text


@pytest.mark.parametrize(
    ("first_source", "reason"),
    [
        ("env", "its value came from env, not this instance's store"),
        (None, "Credential field could not be resolved: mixed_api.api_key"),
    ],
    ids=["another source answers first", "the store has no value"],
)
def test_export_refuses_a_stored_field_it_cannot_read_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_source: str | None, reason: str
) -> None:
    """A stored field answered by another source, or by nothing, stops the export with the reason."""

    from secure_tool_broker.credential_catalog import (
        CatalogCredential,
        CatalogCredentialField,
        CatalogSourceRef,
        _save_catalog_credential,
    )
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_transfer import export_credentials

    _use_store(tmp_path, monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    monkeypatch.setenv("TRANSFER_TEST_API_KEY", ENV_API_KEY)
    stored = CatalogSourceRef(source="sealbox", item_id="missing-item", item_name="missing-item", field_ref="value")
    order = [CatalogSourceRef(source="env", env_var="TRANSFER_TEST_API_KEY"), stored] if first_source else [stored]
    _save_catalog_credential(
        "mixed_api",
        CatalogCredential(
            display_name="Mixed API",
            purpose="A stored field whose value the store cannot give.",
            fields={"api_key": CatalogCredentialField(kind="api_key", resolution_order=order)},
        ),
        scope=CredentialOwnerScope.team(TEST_TEAM),
    )
    with pytest.raises(RuntimeError, match="Could not read the credential mixed_api field api_key") as raised:
        export_credentials(TEST_TEAM, [])
    assert reason in str(raised.value)
    assert ENV_API_KEY not in str(raised.value)


def test_distinct_catalog_names_keep_distinct_imported_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from secure_tool_broker.credential_catalog import resolve_secret_field
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_transfer import export_credentials, import_credentials

    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    expected = {".search_api": STORED_API_KEY, "search_api": "another-stored-api-key"}
    for index, (key, value) in enumerate(expected.items()):
        _store_team_api_key(key, value, item_id=f"separate-source-item-{index}")
    records = export_credentials(TEST_TEAM, [])

    _use_store(tmp_path / "new", monkeypatch)
    destination = _FakeSealboxCli()
    _use_sealbox(destination, monkeypatch)
    outcomes = import_credentials(records, team_id=TEST_TEAM)
    assert all(outcome.imported for outcome in outcomes)
    for key, value in expected.items():
        assert resolve_secret_field(key, "api_key", scope=CredentialOwnerScope.team(TEST_TEAM)).value == value
    assert len(destination.secrets) == len(expected)


@pytest.mark.parametrize(("source_key", "imported"), [(".search_api", True), ("search_api", False)])
def test_import_keeps_the_secret_of_an_existing_catalog_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_key: str, imported: bool
) -> None:
    from secure_tool_broker.credential_catalog import resolve_secret_field
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_transfer import export_credentials, import_credentials

    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    _store_team_api_key(source_key)
    records = export_credentials(TEST_TEAM, [])

    _use_store(tmp_path / "new", monkeypatch)
    destination = _FakeSealboxCli()
    _use_sealbox(destination, monkeypatch)
    existing_value = "existing-destination-api-key"
    _store_team_api_key(value=existing_value)
    [outcome] = import_credentials(records, team_id=TEST_TEAM)
    assert outcome.imported is imported
    scope = CredentialOwnerScope.team(TEST_TEAM)
    assert resolve_secret_field("search_api", "api_key", scope=scope).value == existing_value
    if imported:
        assert resolve_secret_field(source_key, "api_key", scope=scope).value == STORED_API_KEY
    before_repeat = dict(destination.secrets)
    [repeated] = import_credentials(records, team_id=TEST_TEAM)
    assert not repeated.imported and "kept the current credential" in repeated.reason
    assert destination.secrets == before_repeat


@pytest.mark.parametrize("kind", ["api_key", "password"])
def test_distinct_field_names_keep_distinct_imported_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from secure_tool_broker.credential_catalog import collect_credential, resolve_secret_field
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_transfer import export_credentials, import_credentials

    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    scope = CredentialOwnerScope.team(TEST_TEAM)
    expected = {f".{kind}": STORED_API_KEY, kind: "another-stored-field-value"}
    saved = collect_credential(
        {
            "credential_key": "multiple_secrets",
            "display_name": "Multiple secrets",
            "fields": [
                {"name": name, "kind": kind, "field_ref": f"separate-source-field-{index}"}
                for index, name in enumerate(expected)
            ],
        },
        expected,
        scope=scope,
    )
    assert saved.status == "success", saved.error
    records = export_credentials(TEST_TEAM, [])

    _use_store(tmp_path / "new", monkeypatch)
    destination = _FakeSealboxCli()
    _use_sealbox(destination, monkeypatch)
    [outcome] = import_credentials(records, team_id=TEST_TEAM)
    assert outcome.imported
    for name, value in expected.items():
        assert resolve_secret_field("multiple_secrets", name, scope=scope).value == value
    assert len(destination.secrets) == len(expected)


def test_catalog_save_failure_retries_without_losing_metadata_or_leaving_duplicate_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    from secure_tool_broker.credential_catalog import collect_credential, load_catalog, resolve_secret_field
    from secure_tool_broker.credential_catalog_store import CredentialCatalogStore, CredentialOwnerScope
    from secure_tool_broker.credential_transfer import export_credentials, import_credentials

    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    scope = CredentialOwnerScope.team(TEST_TEAM)
    saved = collect_credential(
        {
            "credential_key": "search_api",
            "display_name": "Search account",
            "fields": [{"name": "api_key", "kind": "api_key"}, {"name": "username", "kind": "username"}],
        },
        {"api_key": STORED_API_KEY, "username": "account-owner"},
        scope=scope,
    )
    assert saved.status == "success", saved.error
    original = load_catalog(scope=scope).credentials["search_api"]
    records = export_credentials(TEST_TEAM, [])

    _use_store(tmp_path / "new", monkeypatch)
    destination = _FakeSealboxCli()
    _use_sealbox(destination, monkeypatch)
    original_put = CredentialCatalogStore.put_credential
    attempted_rows = []

    def fail_first_put(self, owner_scope, key, credential):
        attempted_rows.append(credential)
        if len(attempted_rows) == 1:
            raise RuntimeError("catalog storage unavailable")
        return original_put(self, owner_scope, key, credential)

    monkeypatch.setattr(CredentialCatalogStore, "put_credential", fail_first_put)
    [failed] = import_credentials(records, team_id=TEST_TEAM)
    assert not failed.imported
    assert "search_api" not in load_catalog(scope=scope).credentials
    assert "event=credentials_transfer_catalog_import_failed" in caplog.text
    first_items = set(destination.secrets)
    assert len(first_items) == 1

    [retried] = import_credentials(records, team_id=TEST_TEAM)
    assert retried.imported, retried.reason
    assert set(destination.secrets) == first_items
    assert len(attempted_rows) == 2, "each attempt publishes the complete catalog row once"
    assert all(set(row["fields"]) == set(original.fields) for row in attempted_rows)
    restored = load_catalog(scope=scope).credentials["search_api"]
    assert restored.fields["username"] == original.fields["username"]
    assert restored.fields["api_key"].value_source == original.fields["api_key"].value_source
    assert (restored.created_at, restored.approval) == (original.created_at, original.approval)
    assert resolve_secret_field("search_api", "api_key", scope=scope).value == STORED_API_KEY
    assert STORED_API_KEY not in caplog.text


def test_partial_secret_write_retries_the_same_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    from secure_tool_broker import credential_catalog
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_transfer import export_credentials, import_credentials

    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    scope = CredentialOwnerScope.team(TEST_TEAM)
    expected = {"api_key": STORED_API_KEY, "other_key": "second-transferred-secret"}
    saved = credential_catalog.collect_credential(
        {"credential_key": "multiple_secrets", "display_name": "Multiple secrets",
         "fields": [{"name": name, "kind": "api_key"} for name in expected]},
        expected, scope=scope,
    )
    assert saved.status == "success", saved.error
    records = export_credentials(TEST_TEAM, [])
    _use_store(tmp_path / "new", monkeypatch)
    destination = _FakeSealboxCli()
    _use_sealbox(destination, monkeypatch)
    writes = 0

    def fail_second_write(args, **kwargs):
        nonlocal writes
        if list(args[:2]) == ["secret", "set"]:
            writes += 1
            if writes == 2:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="secret storage unavailable")
        return destination(args, **kwargs)

    monkeypatch.setattr(credential_catalog, "run_sealbox", fail_second_write)
    [failed] = import_credentials(records, team_id=TEST_TEAM)
    assert not failed.imported
    assert "multiple_secrets" not in credential_catalog.load_catalog(scope=scope).credentials
    first_items = set(destination.secrets)
    assert len(first_items) == 1
    [retried] = import_credentials(records, team_id=TEST_TEAM)
    assert retried.imported
    assert first_items < set(destination.secrets)
    assert len(destination.secrets) == len(expected)
    for name, value in expected.items():
        assert credential_catalog.resolve_secret_field("multiple_secrets", name, scope=scope).value == value
        assert value not in caplog.text


@pytest.mark.parametrize("composed_reference", [False, True])
def test_retry_refuses_an_item_adopted_by_a_destination_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, composed_reference: bool
) -> None:
    from secure_tool_broker import credential_catalog
    from secure_tool_broker.credential_catalog_store import CredentialCatalogStore, CredentialOwnerScope
    from secure_tool_broker.credential_transfer import export_credentials, import_credentials

    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    _store_team_api_key()
    records = export_credentials(TEST_TEAM, [])
    _use_store(tmp_path / "new", monkeypatch)
    destination = _FakeSealboxCli()
    _use_sealbox(destination, monkeypatch)
    original_put = CredentialCatalogStore.put_credential
    failed_once = False

    def fail_once(self, *args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("catalog storage unavailable")
        return original_put(self, *args, **kwargs)

    monkeypatch.setattr(CredentialCatalogStore, "put_credential", fail_once)
    [failed] = import_credentials(records, team_id=TEST_TEAM)
    assert not failed.imported
    [item_key] = destination.secrets
    item_id, field_ref = item_key.rsplit(":", 1)
    scope = CredentialOwnerScope.team(TEST_TEAM)
    existing_value = "secret-owned-by-the-destination-alias"
    saved = credential_catalog.collect_credential(
        {"credential_key": "destination_alias", "display_name": "Existing alias", "item_id": item_id,
         "fields": [{"name": "api_key", "kind": "api_key", "field_ref": field_ref}]},
        {"api_key": existing_value}, scope=scope,
    )
    assert saved.status == "success", saved.error
    if composed_reference:
        registered = credential_catalog.register_existing_source(
            {"credential_key": "destination_alias", "field_name": "api_key", "kind": "api_key",
             "source": "sealbox", "item_id": item_id, "field_ref": field_ref},
            scope=scope,
        )
        assert registered.status == "success", registered.error
    before_retry = dict(destination.secrets)
    [retried] = import_credentials(records, team_id=TEST_TEAM)
    assert not retried.imported
    assert not retried.retryable
    assert destination.secrets == before_retry
    assert "search_api" not in credential_catalog.load_catalog(scope=scope).credentials
    assert credential_catalog.resolve_secret_field("destination_alias", "api_key", scope=scope).value == existing_value


def test_temporary_credential_transfer_carries_expiry_reapplies_ttl_and_upgrades_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5: temporary credentials carry expires_at, re-apply remaining Sealbox TTL on import, and v2 imports still pass."""

    from datetime import timedelta
    from secure_tool_broker import credential_transfer_api as api
    from secure_tool_broker.credential_catalog import collect_credential, load_catalog, resolve_secret_field
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_transfer import export_credentials, import_credentials
    from team_transfer.crypto import canonical_json, encrypt_with_key
    from team_transfer.format import SectionDocumentV1

    now = datetime.now(timezone.utc).replace(microsecond=0)
    future_expiry = (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    scope = CredentialOwnerScope.user("usr_ana")

    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    saved = collect_credential(
        {
            "credential_key": "temp_api",
            "display_name": "Temporary API",
            "store_target": "sealbox",
            "fields": [{"name": "api_key", "kind": "api_key", "visibility": "secret", "value_source": "user_input"}],
        },
        {"api_key": "temp-secret-token"},
        scope=scope,
        expires_at=future_expiry,
        allow_expiry_change=True,
        now=now,
    )
    assert saved.status == "success", saved.error

    records = export_credentials(TEST_TEAM, ["usr_ana"])
    assert len(records) == 1
    assert records[0]["credential"]["expires_at"] == future_expiry

    # Import 30 minutes later: Sealbox write must receive remaining TTL (~5400s).
    import_time = now + timedelta(minutes=30)
    _use_store(tmp_path / "new", monkeypatch)
    destination = _FakeSealboxCli()
    _use_sealbox(destination, monkeypatch)
    [outcome] = import_credentials(records, team_id=TEST_TEAM, now=import_time)
    assert outcome.imported, outcome.reason
    assert len(destination.set_calls) == 1
    ttl_flag_index = destination.set_calls[0].index("--ttl")
    ttl_value = int(destination.set_calls[0][ttl_flag_index + 1])
    assert 5395 <= ttl_value <= 5405
    imported_cred = load_catalog(scope=scope).credentials["temp_api"]
    assert imported_cred.expires_at == future_expiry
    assert resolve_secret_field("temp_api", "api_key", scope=scope, now=import_time).value == "temp-secret-token"

    # Previous schema version 2 document converts cleanly to version 3 inside decrypt_records.
    v2_doc = SectionDocumentV1(
        section="broker_credentials",
        schema_version=2,
        records=[
            {
                "kind": "catalog_entry",
                "owner": {"kind": "person", "owner_id": "usr_ana"},
                "credential_key": "legacy_v2_cred",
                "credential": {
                    "display_name": "Legacy V2",
                    "purpose": "Pre-expiry credential",
                    "fields": {},
                },
                "secret_values": {},
            }
        ],
    )
    v2_ciphertext = base64.b64encode(
        encrypt_with_key(canonical_json(v2_doc.model_dump(mode="json")), KEY)
    ).decode("ascii")
    upgraded = api.decrypt_records(
        api.CredentialImportRequestV1(
            team_id=TEST_TEAM,
            encryption_key_b64=KEY_B64,
            ciphertext_b64=v2_ciphertext,
        )
    )
    assert len(upgraded) == 1
    assert upgraded[0]["credential_key"] == "legacy_v2_cred"


def test_transfer_import_skips_expired_credential_revokes_destination_link_and_notifies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5 + C20 lead ruling: expired imported credential is skipped, destination link revoked with system actor, 1 notice, idempotent rerun."""

    from dataclasses import replace
    from datetime import timedelta
    from types import SimpleNamespace
    from access_catalog.catalog import CatalogSource
    from access_control.entities import AGENT_ROLE_SYSTEM, role_id
    from access_control.identity import ContextVerifier
    from custom_messages.contracts.access_v2 import (
        AccessNoticeKind,
        AccountLinkStatus,
        AccountLinkV1,
        AccountMode,
        CredentialInjectionV1,
        InjectionPlacement,
        LinkOwnerKind,
        LinkPurpose,
        SubjectKind,
        SubjectRefV1,
        ToolDefinitionV1,
        ToolKind,
    )
    from secure_tool_broker.access_gate import BrokerAccessGate
    from secure_tool_broker.credential_catalog import collect_credential, load_catalog
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_transfer import (
        EXPIRED_BEFORE_IMPORT_REASON,
        export_credentials,
        import_credentials,
    )
    from storage.access_repository import AccessRepository
    from storage.config import resolve_storage_settings
    from tests.broker_access_support import context_verify_keys

    now = datetime.now(timezone.utc).replace(microsecond=0)
    short_expiry = (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    scope = CredentialOwnerScope.user("usr_ana")

    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    saved = collect_credential(
        {
            "credential_key": "expiring_in_transit",
            "display_name": "Expiring API",
            "store_target": "sealbox",
            "fields": [{"name": "api_key", "kind": "api_key", "visibility": "secret", "value_source": "user_input"}],
        },
        {"api_key": "soon-expired-secret"},
        scope=scope,
        expires_at=short_expiry,
        allow_expiry_change=True,
        now=now,
    )
    assert saved.status == "success", saved.error
    records = export_credentials(TEST_TEAM, ["usr_ana"])
    assert len(records) == 1

    # Destination already imported the access section (active link exists for usr_ana -> expiring_in_transit).
    _use_store(tmp_path / "new", monkeypatch)
    destination = _FakeSealboxCli()
    _use_sealbox(destination, monkeypatch)
    access_store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "new" / "access.db")}})
    )
    demo_tool = ToolDefinitionV1(
        tool_id="http:transfer_demo",
        kind=ToolKind.HTTP,
        display_name="Transfer Demo",
        account=AccountMode.LINKED_CREDENTIAL,
        hosts=("transfer.demo.example",),
        credential_injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.HEADER,
                name="Authorization",
                field="api_key",
                template="Bearer {value}",
            ),
        ),
    )
    base_catalog = CatalogSource(None).current()
    custom_catalog = replace(base_catalog, tools={**base_catalog.tools, demo_tool.tool_id: demo_tool})
    gate = BrokerAccessGate(
        catalogs=SimpleNamespace(current=lambda: custom_catalog),  # type: ignore[arg-type]
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEST_TEAM,
        access=None,
        links=access_store,
    )
    access_store.create_link(
        AccountLinkV1(
            link_id="link-transfer-expired",
            team_id=TEST_TEAM,
            tool_id="http:transfer_demo",
            owner_kind=LinkOwnerKind.PERSON,
            owner_id="usr_ana",
            purpose=LinkPurpose.ACCOUNT,
            credential_key="expiring_in_transit",
            injections=demo_tool.credential_injections,
            created_by=SubjectRefV1(kind=SubjectKind.USER, id="usr_ana"),
            created_at_utc=now - timedelta(hours=1),
        )
    )

    after_expiry = now + timedelta(minutes=20)
    [outcome] = import_credentials(records, team_id=TEST_TEAM, access_gate=gate, now=after_expiry)
    assert outcome.imported is False
    assert outcome.reason == EXPIRED_BEFORE_IMPORT_REASON
    assert outcome.retryable is False
    assert destination.secrets == {}
    assert "expiring_in_transit" not in load_catalog(scope=scope).credentials

    # Destination link revoked by system role actor and exactly one notice sent.
    expected_actor = SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(TEST_TEAM, AGENT_ROLE_SYSTEM))
    revoked = access_store.get_link("link-transfer-expired")
    assert revoked is not None
    assert revoked.status is AccountLinkStatus.REVOKED
    assert revoked.revoked_by == expected_actor

    notices = [n for n, _read in access_store.notices_for(TEST_TEAM, person_id="usr_ana", roles=())]
    assert len(notices) == 1
    assert notices[0].kind is AccessNoticeKind.CREDENTIAL_EXPIRED
    assert "/link http:transfer_demo" in notices[0].summary

    # Re-running the same import adds no second revocation and no second notice.
    [rerun] = import_credentials(records, team_id=TEST_TEAM, access_gate=gate, now=after_expiry)
    assert rerun.imported is False
    assert rerun.reason == EXPIRED_BEFORE_IMPORT_REASON
    assert len(access_store.notices_for(TEST_TEAM, person_id="usr_ana", roles=())) == 1


def test_transfer_import_expired_credential_is_retryable_when_link_revocation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C26: a failed link revocation on an expired import marks the outcome retryable; retrying finishes revocation with one notice."""

    from dataclasses import replace
    from datetime import timedelta
    from types import SimpleNamespace
    from typing import Any
    from access_catalog.catalog import CatalogSource
    from access_control.entities import AGENT_ROLE_SYSTEM, role_id
    from access_control.identity import ContextVerifier
    from custom_messages.contracts.access_v2 import (
        AccessNoticeKind,
        AccountLinkStatus,
        AccountLinkV1,
        AccountMode,
        CredentialInjectionV1,
        InjectionPlacement,
        LinkOwnerKind,
        LinkPurpose,
        SubjectKind,
        SubjectRefV1,
        ToolDefinitionV1,
        ToolKind,
    )
    from secure_tool_broker.access_gate import BrokerAccessGate
    from secure_tool_broker.credential_catalog import collect_credential
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.credential_transfer import (
        EXPIRED_BEFORE_IMPORT_LINK_RETRY_REASON,
        EXPIRED_BEFORE_IMPORT_REASON,
        export_credentials,
        import_credentials,
    )
    from storage.access_repository import AccessRepository
    from storage.config import resolve_storage_settings
    from tests.broker_access_support import context_verify_keys

    now = datetime.now(timezone.utc).replace(microsecond=0)
    short_expiry = (now + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    scope = CredentialOwnerScope.user("usr_ana")

    _use_store(tmp_path / "old", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    saved = collect_credential(
        {
            "credential_key": "expiring_retry_link",
            "display_name": "Expiring Retry API",
            "store_target": "sealbox",
            "fields": [{"name": "api_key", "kind": "api_key", "visibility": "secret", "value_source": "user_input"}],
        },
        {"api_key": "soon-expired-secret"},
        scope=scope,
        expires_at=short_expiry,
        allow_expiry_change=True,
        now=now,
    )
    assert saved.status == "success", saved.error
    records = export_credentials(TEST_TEAM, ["usr_ana"])

    _use_store(tmp_path / "new", monkeypatch)
    _use_sealbox(_FakeSealboxCli(), monkeypatch)
    access_store = AccessRepository(
        resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "new" / "access.db")}})
    )
    demo_tool = ToolDefinitionV1(
        tool_id="http:transfer_retry_demo",
        kind=ToolKind.HTTP,
        display_name="Transfer Retry Demo",
        account=AccountMode.LINKED_CREDENTIAL,
        hosts=("transfer-retry.demo.example",),
        credential_injections=(
            CredentialInjectionV1(
                placement=InjectionPlacement.HEADER,
                name="Authorization",
                field="api_key",
                template="Bearer {value}",
            ),
        ),
    )
    base_catalog = CatalogSource(None).current()
    custom_catalog = replace(base_catalog, tools={**base_catalog.tools, demo_tool.tool_id: demo_tool})
    gate = BrokerAccessGate(
        catalogs=SimpleNamespace(current=lambda: custom_catalog),  # type: ignore[arg-type]
        verifier=ContextVerifier.from_value(context_verify_keys()),
        team_id=TEST_TEAM,
        access=None,
        links=access_store,
    )
    access_store.create_link(
        AccountLinkV1(
            link_id="link-transfer-retry",
            team_id=TEST_TEAM,
            tool_id="http:transfer_retry_demo",
            owner_kind=LinkOwnerKind.PERSON,
            owner_id="usr_ana",
            purpose=LinkPurpose.ACCOUNT,
            credential_key="expiring_retry_link",
            injections=demo_tool.credential_injections,
            created_by=SubjectRefV1(kind=SubjectKind.USER, id="usr_ana"),
            created_at_utc=now - timedelta(hours=1),
        )
    )

    real_revoke = access_store.revoke_link
    attempts = {"count": 0}

    def flaky_revoke(*args: Any, **kwargs: Any) -> Any:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient access store outage")
        return real_revoke(*args, **kwargs)

    monkeypatch.setattr(access_store, "revoke_link", flaky_revoke)

    after_expiry = now + timedelta(minutes=20)
    [first] = import_credentials(records, team_id=TEST_TEAM, access_gate=gate, now=after_expiry)
    assert first.imported is False
    assert first.retryable is True
    assert first.reason == EXPIRED_BEFORE_IMPORT_LINK_RETRY_REASON
    still_active = access_store.get_link("link-transfer-retry")
    assert still_active is not None
    assert still_active.status is AccountLinkStatus.ACTIVE

    [second] = import_credentials(records, team_id=TEST_TEAM, access_gate=gate, now=after_expiry)
    assert second.imported is False
    assert second.retryable is False
    assert second.reason == EXPIRED_BEFORE_IMPORT_REASON

    expected_actor = SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(TEST_TEAM, AGENT_ROLE_SYSTEM))
    revoked = access_store.get_link("link-transfer-retry")
    assert revoked is not None
    assert revoked.status is AccountLinkStatus.REVOKED
    assert revoked.revoked_by == expected_actor

    notices = [n for n, _read in access_store.notices_for(TEST_TEAM, person_id="usr_ana", roles=())]
    assert len(notices) == 1
    assert notices[0].kind is AccessNoticeKind.CREDENTIAL_EXPIRED
    assert "/link http:transfer_retry_demo" in notices[0].summary

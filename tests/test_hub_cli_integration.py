"""CLI boundaries exercised through catalog resolution and real child processes.

X and Hugging Face actions carry no account selector: the broker's access gate
chooses the account link (the person's own first, then the team's) and the
executor reads the linked credential from that owner's catalog.
"""

import base64
import json
import sys
from types import SimpleNamespace

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from custom_messages.contracts.access_v2 import AccountLinkV1, LinkOwnerKind
from secure_tool_broker import credential_catalog as catalog
from secure_tool_broker import hub_cli_executor as executor
from secure_tool_broker import xurl_executor
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.credential_state import (
    active_credential_owner_scope_kind,
    broker_credential_file,
    credential_owner_context,
)
from secure_tool_broker.credential_state_store import (
    CredentialStateBackendConfig,
    credential_state_item_path,
)
from secure_tool_broker.access_gate import REASON_ACCOUNT_LINK_MISSING
from secure_tool_broker.executors import execute_validated_action
from secure_tool_broker.service import SecureToolBrokerService
from secure_tools.basic_providers import register_builtin_providers
from secure_tools.hub_providers import MAX_FILE_BYTES, hub_cli_providers
from secure_tools.providers import ToolContext, ToolResult
from secure_tools.registry import get_provider
from tests.broker_access_support import (
    BROKER_ROOT,
    TEST_TEAM,
    access_store,
    account_link,
    agent_context,
    caller_headers,
    linked_gate,
    store_account_link,
)

TOOL_IDS = {"xurl": "cli:x", "huggingface": "cli:huggingface"}

OAUTH = json.dumps(
    {
        "client_id": "client-canary",
        "client_secret": "secret-canary",
        "username": "example",
        "access_token": "access-canary",
        "refresh_token": "refresh-canary",
        "expiration_time": 4000000000,
    }
)


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "catalog.db")
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv("HF_TOKEN", "ambient-hf-canary")
    monkeypatch.setenv("CLIENT_SECRET", "ambient-x-canary")
    calls = []

    class Source:
        def get_secret(self, request):
            calls.append(request)
            return OAUTH if request.ref.item_id == "x-token" else "hf-token-canary"

    monkeypatch.setattr(catalog, "get_credential_source", lambda name: Source())
    for user in ["alice", "bob"]:
        credentials = {}
        for key, field, item in [
            ("x", "oauth_json", "x-token"),
            ("hf", "api_token", "hf-token"),
        ]:
            credentials[key] = catalog.CatalogCredential(
                display_name=key,
                purpose="Test",
                fields={
                    field: catalog.CatalogCredentialField(
                        resolution_order=[
                            catalog.CatalogSourceRef(
                                source="sealbox", item_id=item, field_ref="value"
                            )
                        ],
                    )
                },
            )
        catalog.save_catalog(
            catalog.CredentialCatalog(credentials=credentials),
            scope=CredentialOwnerScope.user(user),
        )
    binary = tmp_path / "cli"
    binary.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
import yaml
assert "CLIENT_SECRET" not in os.environ
home = Path(os.environ["HOME"])
if "token" in sys.argv:
    assert "HF_TOKEN" not in os.environ
    path = home / ".xurl/auth.yml"
    data = yaml.safe_load(path.read_text())
    token = data["apps"]["ua"]["oauth2_tokens"]["example"]["oauth2"]
    token["access_token"] += "-rotated"
    path.write_text(yaml.safe_dump(data))
    print(token["access_token"])
elif "/2/tweets" in sys.argv:
    assert "HF_TOKEN" not in os.environ
    print(json.dumps({"data": {"id": "12345"}}))
elif "/2/users/me" in sys.argv:
    print(json.dumps({"data": {"id": "54321", "username": "example"}}))
else:
    assert os.environ["HF_TOKEN"] == "hf-token-canary"
    print(json.dumps({"user": "example", "token": "hf-token-canary"}))
"""
    )
    binary.chmod(0o755)
    monkeypatch.setattr(executor.shutil, "which", lambda name: str(binary))
    register_builtin_providers()
    return tmp_path, calls


def person_link(provider: str, credential_key: str, user: str = "alice") -> AccountLinkV1:
    return account_link(TOOL_IDS[provider], credential_key, owner_id=user)


def invoke(provider, action, user="alice", credential_key=None, link=None, **params):
    """Run an action with the account link the broker's gate would have chosen.

    ``credential_key`` is shorthand for the user's own link to that credential.
    """

    if link is None and credential_key is not None:
        link = person_link(provider, credential_key, user)
    validated = get_provider(provider).validate(action, params)
    return execute_validated_action(
        validated,
        ToolContext(canonical_user_id=user, principal_type="user", account_link=link),
    )


def test_sealbox_catalog_hydration_is_owner_scoped_and_does_not_expose_tokens(
    broker, caplog
):
    root, calls = broker
    result = invoke("huggingface", "auth.status", credential_key="hf")
    assert result.status == "success", result
    assert result.content == {"authenticated": True, "username": "example"}
    assert calls[-1].owner_scope_kind == "user"
    assert calls[-1].owner_id == "alice"
    assert "canary" not in repr(result) + caplog.text
    assert not list((root / "private").rglob("invocation-*"))


def test_x_refresh_survives_new_invocations_and_isolated_users(broker, caplog):
    first = invoke("xurl", "post.create", credential_key="x", text="hello")
    assert first.status == "success", first
    assert first.content["post_id"] == "12345"
    assert invoke("xurl", "auth.status", credential_key="x").status == "success"
    assert (
        invoke("xurl", "auth.status", user="bob", credential_key="x").status
        == "success"
    )
    for user, suffix in [("alice", "-rotated-rotated"), ("bob", "-rotated")]:
        with credential_owner_context(user):
            path = broker_credential_file("xurl", "sessions.json")
            state = json.loads(path.read_text())
            token = state["x"]["store"]["apps"]["ua"]["oauth2_tokens"]["example"][
                "oauth2"
            ]["access_token"]
            assert token == "access-canary" + suffix
            assert path.stat().st_mode & 0o777 == 0o600
    assert "canary" not in caplog.text + repr(first)


def test_x_never_publishes_when_refresh_cannot_be_persisted(broker, monkeypatch):
    calls = []
    original = xurl_executor.run_cli

    def run(command, *args, **kwargs):
        calls.append(command)
        return original(command, *args, **kwargs)

    monkeypatch.setattr(xurl_executor, "run_cli", run)
    monkeypatch.setattr(
        xurl_executor, "write_credential_state", lambda *args: ToolResult("error")
    )
    result = invoke("xurl", "post.create", credential_key="x", text="hello")
    assert result.content["error_code"] == "oauth_state_persistence_failed"
    assert len(calls) == 1 and "token" in calls[0]


def test_confirmed_post_receipt_survives_later_state_write_failure(broker, monkeypatch):
    from pathlib import Path

    original_run = xurl_executor.run_cli
    original_write = xurl_executor.write_credential_state
    writes = []

    def run(command, env, **kwargs):
        output = original_run(command, env, **kwargs)
        if "/2/tweets" in command:
            path = Path(env["HOME"]) / ".xurl/auth.yml"
            data = yaml.safe_load(path.read_text())
            data["apps"]["ua"]["oauth2_tokens"]["example"]["oauth2"]["access_token"] = "changed-during-request"
            path.write_text(yaml.safe_dump(data))
        return output

    def persist(*args):
        writes.append(args)
        return original_write(*args) if len(writes) == 1 else ToolResult("error")

    monkeypatch.setattr(xurl_executor, "run_cli", run)
    monkeypatch.setattr(xurl_executor, "write_credential_state", persist)
    result = invoke("xurl", "post.create", credential_key="x", text="hello")
    assert result.status == "success"
    assert result.content["post_id"] == "12345"
    assert "warning" in result.content


def test_unavailable_credentials_do_not_use_persisted_x_tokens(broker, monkeypatch):
    assert invoke("xurl", "auth.status", credential_key="x").status == "success"
    monkeypatch.setattr(
        executor, "resolve_secret_field", lambda *args, **kwargs: ToolResult("error")
    )
    result = invoke("xurl", "post.create", credential_key="x", text="hello")
    assert result.content["error_code"] == "credential_unavailable"


def test_wrong_credential_reports_missing_field_before_x_execution(broker, monkeypatch):
    from unittest.mock import Mock

    execute = Mock()
    monkeypatch.setattr(xurl_executor, "execute_xurl", execute)
    result = invoke("xurl", "auth.status", credential_key="hf")
    assert result.content["error_code"] == "credential_field_missing"
    assert result.content["required_field"] == "oauth_json"
    assert result.content["provider_operation_attempted"] is False
    assert result.content["action_owner"] == "human"
    execute.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "code", "owner"),
    [
        ("missing", "credential_not_found", "human"),
        ("unconfigured", "credential_sources_unconfigured", "team_lead"),
        ("error", "credential_source_failed", "team_lead"),
        ("exception", "credential_source_failed", "team_lead"),
        ("empty", "credential_value_unavailable", "team_lead"),
        ("public", "credential_field_not_secret", "human"),
    ],
)
def test_credential_resolution_diagnostics_are_safe(
    broker, monkeypatch, caplog, failure, code, owner,
):
    from unittest.mock import Mock

    catalog._SECRET_CACHE.clear()
    scope = CredentialOwnerScope.user("alice")
    data = catalog.load_catalog(scope=scope)
    if failure == "missing":
        del data.credentials["x"]
    elif failure == "unconfigured":
        data.credentials["x"].fields["oauth_json"].resolution_order = []
    elif failure == "public":
        data.credentials["x"].fields["oauth_json"] = catalog.CatalogCredentialField(
            visibility="public", value="private-material-canary",
        )
    catalog.save_catalog(data, scope=scope)

    class BrokenSource:
        def get_secret(self, request):
            if failure == "exception":
                raise RuntimeError("private-material-canary")
            if failure == "empty":
                return ""
            return ToolResult(
                "error", {"error_code": "private-material-canary"},
                "private-material-canary",
            )

    monkeypatch.setattr(catalog, "get_credential_source", lambda name: BrokenSource())
    execute = Mock()
    monkeypatch.setattr(xurl_executor, "execute_xurl", execute)
    result = invoke("xurl", "post.create", credential_key="x", text="hello")
    assert result.content["error_code"] == code
    assert result.content["failure_stage"] == "credential_resolution"
    assert result.content["provider_operation_attempted"] is False
    assert result.content["retry_safe"] is False
    assert result.content["action_owner"] == owner
    assert result.content["next_action"]
    assert "private-material-canary" not in repr(result) + caplog.text
    assert "credential_key" not in result.content
    execute.assert_not_called()


def test_unknown_resolution_code_is_not_forwarded(broker, monkeypatch, caplog):
    monkeypatch.setattr(
        executor, "resolve_secret_field",
        lambda *args, **kwargs: ToolResult(
            "error", {"error_code": "secret-code-canary"}, "secret-error-canary",
        ),
    )
    result = invoke("huggingface", "auth.status", credential_key="hf")
    assert result.content["error_code"] == "credential_unavailable"
    assert result.content["required_field"] == "api_token"
    assert "secret-code-canary" not in repr(result) + caplog.text
    assert "secret-error-canary" not in repr(result) + caplog.text


def test_source_exception_keeps_configured_failover_observable(broker, monkeypatch, caplog):
    catalog._SECRET_CACHE.clear()
    scope = CredentialOwnerScope.user("alice")
    data = catalog.load_catalog(scope=scope)
    data.credentials["x"].fields["oauth_json"].resolution_order.insert(
        0, catalog.CatalogSourceRef(source="bitwarden", item_id="unavailable"),
    )
    catalog.save_catalog(data, scope=scope)

    class Source:
        def get_secret(self, request):
            if request.ref.item_id == "unavailable":
                raise RuntimeError("fallback-secret-canary")
            return OAUTH

    monkeypatch.setattr(catalog, "get_credential_source", lambda name: Source())
    result = invoke("xurl", "auth.status", credential_key="x")
    assert result.status == "success"
    assert "event=credential_source_resolution_failed" in caplog.text
    assert "fallback-secret-canary" not in repr(result) + caplog.text


def test_timeout_does_not_retry_post(broker, monkeypatch):
    calls = []
    original = xurl_executor.run_cli

    def run(command, *args, **kwargs):
        if "/2/tweets" in command:
            calls.append(command)
            raise executor.CliFailure("outcome_unknown", write=True)
        return original(command, *args, **kwargs)

    monkeypatch.setattr(xurl_executor, "run_cli", run)
    result = invoke("xurl", "post.create", credential_key="x", text="hello")
    assert result.content == {"error_code": "outcome_unknown", "retry_safe": False}
    assert len(calls) == 1


@pytest.mark.parametrize(
    "params",
    [
        # Requests cannot pick an account; the gate's account link does.
        {"credential_key": "x"},
        {"credential_binding_id": "credential_binding_secret-canary"},
        {"token": "secret-canary"},
        {"owner_id": "bob"},
    ],
)
def test_invalid_or_secret_parameters_are_rejected_without_echo(params):
    with pytest.raises(ValueError) as error:
        hub_cli_providers()[0].validate("auth.status", params)
    assert "secret-canary" not in str(error.value)


@pytest.mark.parametrize(
    "filename",
    [
        "../token",
        "/etc/passwd",
        "x/../token",
        "-token",
        "a\\b",
        "a//b",
        "*.json",
        ".cache/huggingface/token",
    ],
)
def test_broker_paths_cannot_select_local_files(filename):
    with pytest.raises(ValueError):
        hub_cli_providers()[1].validate(
            "file.download",
            {
                "repo_id": "org/repo",
                "revision": "a" * 40,
                "filename": filename,
            },
        )


def test_upload_rejects_unbounded_data():
    with pytest.raises(ValueError):
        hub_cli_providers()[1].validate(
            "file.upload",
            {
                "repo_id": "org/repo",
                "revision": "a" * 40,
                "filename": "README.md",
                "content_base64": base64.b64encode(
                    b"a" * (MAX_FILE_BYTES + 1)
                ).decode(),
            },
        )


def test_hf_upload_uses_atomic_parent_commit_and_never_creates_repo(
    broker, monkeypatch
):
    import huggingface_hub

    calls = []

    class Api:
        def __init__(self, **kwargs):
            assert kwargs == {
                "endpoint": "https://huggingface.co",
                "token": "hf-token-canary",
            }

        def repo_info(self, *args, **kwargs):
            return SimpleNamespace(sha="a" * 40)

        def upload_file(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                oid="b" * 40,
                commit_url="https://huggingface.co/org/repo/commit/" + "b" * 40,
            )

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    params = {
        "repo_id": "org/repo",
        "revision": "a" * 40,
        "filename": "README.md",
        "content_base64": base64.b64encode(b"hello").decode(),
    }
    result = invoke("huggingface", "file.upload", credential_key="hf", **params)
    assert result.status == "success", result
    assert calls[0]["parent_commit"] == "a" * 40
    assert calls[0]["path_or_fileobj"] == b"hello"
    result = invoke(
        "huggingface", "file.upload", credential_key="hf", **{**params, "revision": "c" * 40}
    )
    assert result.content["error_code"] == "upload_revision_conflict"
    assert len(calls) == 1


def test_user_and_team_state_namespaces_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path))
    with credential_owner_context("same-id"):
        user_path = broker_credential_file("xurl", "sessions.json")
        user_key = credential_state_item_path(
            CredentialStateBackendConfig(), "xurl.sessions_json"
        )
        with credential_owner_context("same-id", scope_kind="team"):
            assert broker_credential_file("xurl", "sessions.json") != user_path
            assert (
                credential_state_item_path(
                    CredentialStateBackendConfig(), "xurl.sessions_json"
                )
                != user_key
            )
            assert active_credential_owner_scope_kind() == "team"
        assert active_credential_owner_scope_kind() == "user"


def test_export_is_private_and_never_overwrites(tmp_path):
    from scripts.export_xurl_credential import export_credential

    source, target = tmp_path / "auth.yml", tmp_path / "export.json"
    source.write_text(
        yaml.safe_dump(
            xurl_executor._initial_store(
                xurl_executor.OAuthCredentials.model_validate_json(OAUTH)
            )
        )
    )
    export_credential(source, "ua", "example", target)
    assert json.loads(target.read_text())["access_token"] == "access-canary"
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        export_credential(source, "ua", "example", target)


def test_sealbox_cold_start_and_refresh_state_preserve_team_scope(monkeypatch):
    import subprocess
    from secure_tool_broker import credential_state_store as state
    from secure_tool_broker.credential_sources import sealbox

    saved = {}
    scopes = []

    def tenant(**kwargs):
        scopes.append((kwargs["scope_kind"], kwargs["owner_id"]))
        return {}

    def command(args, **kwargs):
        key = args[2]
        if args[1] == "set":
            saved[key] = kwargs["input_text"]
            return subprocess.CompletedProcess(args, 0, json.dumps({"key": key}), "")
        if key in saved:
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"value": saved[key]}), ""
            )
        return subprocess.CompletedProcess(
            args, 1, "", 'Error: {"error":"[SealboxError] Secret not found: state"}'
        )

    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "credential_source"
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE", "sealbox")
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES", "false"
    )
    monkeypatch.setattr(sealbox, "sealbox_tenant_environment", tenant)
    monkeypatch.setattr(state, "sealbox_tenant_environment", tenant)
    monkeypatch.setattr(sealbox, "run_sealbox", command)
    monkeypatch.setattr(state, "run_sealbox", command)
    with credential_owner_context("team-example", scope_kind="team"):
        assert state.read_credential_state("xurl.sessions_json") is None
        assert (
            state.write_credential_state(
                "xurl.sessions_json", '{"refreshed":true}'
            ).status
            == "success"
        )
        assert state.read_credential_state("xurl.sessions_json") == '{"refreshed":true}'
    assert scopes and all(scope == ("team", "team-example") for scope in scopes)


def test_x_account_busy_does_not_refresh_or_publish(broker):
    import fcntl

    with credential_owner_context("alice"):
        lock_path = broker_credential_file("hub-cli", "xurl.lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = invoke("xurl", "post.create", credential_key="x", text="hello")
    assert result.content["error_code"] == "account_busy"


def save_team_credentials(team_id: str = TEST_TEAM) -> None:
    """The team's own X and Hugging Face credentials (same test source)."""

    catalog.save_catalog(
        catalog.CredentialCatalog(
            credentials={
                key: catalog.CatalogCredential(
                    display_name=f"team {key}",
                    purpose="Test",
                    fields={
                        field: catalog.CatalogCredentialField(
                            resolution_order=[
                                catalog.CatalogSourceRef(
                                    source="sealbox", item_id=item, field_ref="value"
                                )
                            ],
                        )
                    },
                )
                for key, field, item in [
                    ("team_x", "oauth_json", "x-token"),
                    ("team_hf", "api_token", "hf-token"),
                ]
            }
        ),
        scope=CredentialOwnerScope.team(team_id),
    )


def test_team_link_reads_the_teams_credential_and_keeps_team_state(broker, monkeypatch):
    _root, calls = broker
    # Every read reaches the source, so ``calls`` shows whose credential was used.
    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "0")
    save_team_credentials()
    link = account_link(
        "cli:x", "team_x", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM
    )

    result = invoke("xurl", "post.create", link=link, text="hello")

    assert result.status == "success", result
    assert (calls[-1].owner_scope_kind, calls[-1].owner_id) == ("team", TEST_TEAM)
    with credential_owner_context(TEST_TEAM, scope_kind="team"):
        state = json.loads(broker_credential_file("xurl", "sessions.json").read_text())
    assert "team_x" in state
    with credential_owner_context("alice"):
        assert not broker_credential_file("xurl", "sessions.json").exists()


@pytest.mark.parametrize(
    ("provider", "action", "params", "code"),
    [
        # Hugging Face uses only account links.
        ("huggingface", "auth.status", {}, REASON_ACCOUNT_LINK_MISSING),
        # X: no sign-in for the person (x_sign_in) and no team link.
        ("xurl", "post.create", {"text": "hello"}, "x_account_missing"),
    ],
)
def test_missing_account_is_refused_before_any_secret_resolution(broker, monkeypatch, provider, action, params, code):
    from unittest.mock import Mock

    execute = Mock()
    monkeypatch.setattr(xurl_executor, "execute_xurl", execute)
    before = len(broker[1])

    result = invoke(provider, action, **params)

    assert result.status == "error"
    assert result.content["error_code"] == code
    assert len(broker[1]) == before, "no credential was read"
    execute.assert_not_called()


@pytest.mark.asyncio
async def test_broker_app_uses_the_persons_link_then_the_teams_and_refuses_without_one(
    broker, tmp_path, monkeypatch
):
    _root, calls = broker
    # Every read reaches the source, so ``calls`` shows whose credential was used.
    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "0")
    save_team_credentials()
    links = access_store(tmp_path / "access.db")
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=linked_gate(links))

    def status_request(person: str) -> dict:
        return {
            "provider": "huggingface",
            "action": "auth.status",
            "params": {},
            "reason": "Check the linked Hugging Face account.",
            "context": agent_context(person),
        }

    async with TestClient(TestServer(service.build_app())) as client:

        async def check(person: str):
            response = await client.post(
                "/v1/tools/huggingface",
                headers=caller_headers(),
                json=status_request(person),
            )
            return response.status, json.loads(await response.text())

        missing_status, missing = await check("alice")
        assert missing_status == 403
        assert missing["reason_code"] == REASON_ACCOUNT_LINK_MISSING
        assert missing["tool_id"] == "cli:huggingface"
        assert "account link" in missing["next_step"]
        assert not calls, "a refused request reads no credential"

        store_account_link(
            links,
            "cli:huggingface",
            "team_hf",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id=TEST_TEAM,
        )
        team_status, team = await check("alice")
        assert (team_status, team["status"]) == (200, "success"), team
        assert (calls[-1].owner_scope_kind, calls[-1].owner_id) == ("team", TEST_TEAM)

        store_account_link(links, "cli:huggingface", "hf", owner_id="alice")
        own_status, own = await check("alice")
        assert (own_status, own["status"]) == (200, "success"), own
        assert (calls[-1].owner_scope_kind, calls[-1].owner_id) == ("user", "alice")

        # Bob has no link of his own, so the team's account is used for him.
        bob_status, _bob = await check("bob")
        assert bob_status == 200
        assert (calls[-1].owner_scope_kind, calls[-1].owner_id) == ("team", TEST_TEAM)

        # A request cannot choose an account.
        chosen = await client.post(
            "/v1/tools/huggingface",
            headers=caller_headers(),
            json={**status_request("bob"), "params": {"credential_key": "hf"}},
        )
        assert chosen.status == 400


def test_private_download_checks_size_revision_and_returns_only_requested_bytes(
    broker, monkeypatch
):
    import huggingface_hub
    from pathlib import Path

    metadata = SimpleNamespace(size=5, commit_hash="a" * 40)
    monkeypatch.setattr(
        huggingface_hub, "get_hf_file_metadata", lambda *args, **kwargs: metadata
    )
    calls = []

    def download(command, env, **kwargs):
        calls.append(command)
        assert "hf-token-canary" not in " ".join(command)
        assert env["HF_TOKEN"] == "hf-token-canary"
        path = Path(command[command.index("--local-dir") + 1]) / "config.json"
        path.parent.mkdir()
        path.write_bytes(b"hello")
        return str(path)

    monkeypatch.setattr(executor, "run_cli", download)
    params = {
        "repo_id": "org/repo",
        "revision": "a" * 40,
        "filename": "config.json",
    }
    result = invoke("huggingface", "file.download", credential_key="hf", **params)
    assert result.status == "success", result
    assert base64.b64decode(result.content["content_base64"]) == b"hello"
    metadata.size = MAX_FILE_BYTES + 1
    assert (
        invoke("huggingface", "file.download", credential_key="hf", **params).content[
            "error_code"
        ]
        == "download_size_or_revision_rejected"
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_model_lookup_reads_gated_metadata_with_the_teams_link(broker, tmp_path, monkeypatch):
    """The team lead's model capability lookup (model.info) uses the linked
    account through the broker, so no HF_TOKEN is needed in any process."""

    import huggingface_hub

    _root, calls = broker
    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "0")
    save_team_credentials()
    links = access_store(tmp_path / "access.db")
    store_account_link(links, "cli:huggingface", "team_hf", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
    seen = {}

    class FakeApi:
        def __init__(self, *, endpoint, token):
            seen["token"] = token

        def model_info(self, repo_id):
            seen["repo_id"] = repo_id
            return SimpleNamespace(pipeline_tag="image-text-to-text", tags=["vision"], gated="auto", private=False)

    monkeypatch.setattr(huggingface_hub, "HfApi", FakeApi)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=linked_gate(links))
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/v1/tools/huggingface",
            headers=caller_headers(),
            json={
                "provider": "huggingface",
                "action": "model.info",
                "params": {"repo_id": "org/gated-model"},
                "reason": "Look up model metadata for capability settings",
                "context": agent_context("alice"),
            },
        )
        body = json.loads(await response.text())

    assert response.status == 200, body
    assert body["status"] == "success", body
    assert body["content"]["pipeline_tag"] == "image-text-to-text"
    assert body["content"]["gated"] is True
    assert seen == {"token": "hf-token-canary", "repo_id": "org/gated-model"}
    assert (calls[-1].owner_scope_kind, calls[-1].owner_id) == ("team", TEST_TEAM)
    assert "canary" not in json.dumps(body)


def test_each_person_can_link_their_own_hugging_face_account(tmp_path):
    """/link cli:huggingface: anyone for themselves, the team's account only for
    administrators; the person's own link wins over the team's. (X uses each
    person's own sign-in instead, with the team's link as the fallback:
    tests/test_x_sign_in.py.)"""

    from access_catalog.catalog import CatalogSource
    from custom_messages.contracts.access_v2 import AccountLinkRequestV1
    from access_control.account_sharing import TeamAccountSharing
    from secure_tool_broker.account_links import AccountLinkRefused, linkable_tool

    catalog_now = CatalogSource(None).current()
    links = access_store(tmp_path / "access.db")
    gate = linked_gate(links)
    for tool_id in ("cli:huggingface",):

        def request(owner: LinkOwnerKind, roles: tuple[str, ...], tool_id: str = tool_id) -> AccountLinkRequestV1:
            return AccountLinkRequestV1(
                request_id=f"req-{tool_id}-{owner.value}-{roles[0]}",
                team_id=TEST_TEAM,
                tool_id=tool_id,
                owner_kind=owner,
                person_id="alice",
                person_roles=roles,
                reason="test",
            )

        sharing = TeamAccountSharing(links, TEST_TEAM)
        assert linkable_tool(catalog_now, request(LinkOwnerKind.PERSON, ("member",)), sharing).tool_id == tool_id
        with pytest.raises(AccountLinkRefused):
            linkable_tool(catalog_now, request(LinkOwnerKind.TEAM, ("member",)), sharing)
        assert linkable_tool(catalog_now, request(LinkOwnerKind.TEAM, ("admin",)), sharing).tool_id == tool_id

        team = store_account_link(links, tool_id, "team-key", owner_kind=LinkOwnerKind.TEAM, owner_id=TEST_TEAM)
        assert gate.account_link_for(tool_id, "alice") == team
        own = store_account_link(links, tool_id, "alice-key", owner_id="alice")
        assert gate.account_link_for(tool_id, "alice") == own
        assert gate.account_link_for(tool_id, "bob") == team

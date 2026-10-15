from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from secure_tool_broker import credential_catalog
from secure_tool_broker.credential_catalog_store import (
    CredentialOwnerScope,
    CredentialCatalogStore,
)
from secure_tool_broker.credential_state import credential_owner_context
from secure_tool_broker.credential_state_store import (
    read_credential_state,
    write_credential_state,
)
from secure_tool_broker.departed_person_credentials import delete_person_credentials
from secure_tool_broker.sealbox_runtime import (
    SealboxRuntimeConfig,
    ensure_managed_sealbox_material,
)
from secure_tool_broker.sealbox_tenants import sealbox_tenant_environment
from custom_messages.contracts.access_v2 import (
    CredentialInjectionV1,
    CredentialsFolderOutcome,
    InjectionPlacement,
    PersonCredentialDeletionV1,
    SignInService,
)
from secure_tool_broker.service import SecureToolBrokerService
from secure_tools.providers import ToolContext
from tests.broker_access_support import (
    BROKER_ROOT,
    account_link,
    gateway_headers,
    open_gate,
)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
]


def _find_binary(env_name: str, names: tuple[str, ...]) -> Path | None:
    configured = os.getenv(env_name)
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None
    for candidate in (
        *(Path("/tmp/sealbox/target/debug") / name for name in names),
        *(Path("/tmp/sealbox/target/release") / name for name in names),
        *(Path.home() / "projects/sealbox/target/debug" / name for name in names),
        *(Path.home() / "projects/sealbox/target/release" / name for name in names),
    ):
        if candidate.exists():
            return candidate
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_for_tcp(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.time() + 8
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"sealbox-server exited early with code {process.returncode}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("sealbox-server did not become reachable")


def _run_cli(
    cli: Path, args: list[str], *, env: dict[str, str], input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(cli), *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"sealbox-cli failed: {args}\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def _run_cli_raw(
    cli: Path,
    args: list[str],
    *,
    env: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(cli), *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )


@pytest.fixture
def live_sealbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cli = _find_binary("SEALBOX_CLI_PATH", ("sealbox-cli", "sealbox"))
    server = _find_binary("SEALBOX_SERVER_PATH", ("sealbox-server",))
    if cli is None or server is None:
        pytest.skip(
            "sealbox-cli and sealbox-server are required for live Sealbox tests"
        )

    token_file = tmp_path / "root_token"
    store_path = tmp_path / "sealbox.db"
    port = _free_port()
    runtime = SealboxRuntimeConfig(
        enabled=True,
        manage_service=True,
        auto_initialize=True,
        bind_host="127.0.0.1",
        bind_port=port,
        service_url=None,
        store_path=store_path,
        root_token_file=token_file,
        tenant_material_dir=tmp_path / "tenants",
        api_version="v2",
        ready_timeout_seconds=8,
    )
    ensure_managed_sealbox_material(runtime)

    server_env = os.environ.copy()
    server_env.update(
        {
            "STORE_PATH": str(store_path),
            "AUTH_TOKEN_FILE": str(token_file),
            "LISTEN_ADDR": f"127.0.0.1:{port}",
        }
    )
    process = subprocess.Popen(
        [str(server)],
        env=server_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_tcp(port, process)
        root_env = os.environ.copy()
        root_env.update(
            {
                "SEALBOX_CLI_PATH": str(cli),
                "SEALBOX_URL": runtime.url,
                "SEALBOX_TOKEN_FILE": str(token_file),
                "SEALBOX_ROOT_TOKEN_FILE": str(token_file),
                "SEALBOX_API_VERSION": "v2",
                "SEALBOX_OUTPUT_FORMAT": "json",
                "SECURE_TOOL_BROKER_CREDENTIALS_DIR": str(
                    tmp_path / "broker_credentials"
                ),
                "SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH": str(
                    tmp_path / "credential_catalog.db"
                ),
                "SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR": str(
                    runtime.tenant_material_dir
                ),
            }
        )
        for key, value in root_env.items():
            monkeypatch.setenv(key, value)

        def tenant_env(owner_id: str, *, provision: bool = True) -> dict[str, str]:
            result = sealbox_tenant_environment(
                scope_kind="user",
                owner_id=owner_id,
                provision=provision,
            )
            assert isinstance(result, dict), result
            return result

        def restart() -> None:
            nonlocal process
            process.terminate()
            process.wait(timeout=3)
            process = subprocess.Popen(
                [str(server)],
                env=server_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            _wait_for_tcp(port, process)

        yield {
            "cli": cli,
            "root_env": root_env,
            "tenant_env": tenant_env,
            "restart": restart,
            "process": process,
            "port": port,
            "server": server,
            "store_path": store_path,
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


async def _assert_unknown_form_result_after_broker_restart(tmp_path: Path, session_id: str) -> None:
    """A new broker loses the form session while its saved Sealbox credential remains."""

    from assistant_gateway.config import GatewayInstanceConfig
    from assistant_gateway.gateway import UserGateway
    from assistant_gateway.http_secure_broker import HttpGatewaySecureBroker
    from assistant_gateway.store import GatewayStateStore
    from custom_messages.contracts.credentials_v1 import SecureCredentialCollectionRequest

    config = GatewayInstanceConfig.create(
        instance_name="credential-restart-gateway",
        instance_root=str(tmp_path / "gateway_instance"),
        team_id="credential-restart-team",
        timezone="UTC",
        manager_agent_id="restart-manager",
    )
    config.user.canonical_user_id = "live-user-a"
    config.save()
    events: list = []
    gateway = UserGateway(
        config, output_sink=None, event_sink=events.append,
        state_store=GatewayStateStore(str(tmp_path / "gateway_state.db")),
    )
    gateway.state_store.create_session(
        session_key="credential-session", conversation_id="credential-conversation",
        manager_agent_id=config.manager.manager_agent_id,
        owner_canonical_user_id="live-user-a",
    )
    gateway._manager_client.publish_durable = AsyncMock()
    request = SecureCredentialCollectionRequest(
        service_name="Example", credential_key="example_signup", purpose="Store the signup details.",
        parent_task_id="credential-task",
    )
    restarted = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(restarted.build_app())) as client:
        base = str(client.make_url("/")).rstrip("/")
        gateway._credential_workflow._broker = HttpGatewaySecureBroker(
            internal_base_url=base, public_base_url=base,
            token=gateway_headers()["Authorization"].removeprefix("Bearer "),
        )
        await gateway._credential_workflow.poll_session(
            status_url=f"{base}/v1/credential-collection/sessions/{session_id}",
            session_id=session_id, request_content=request, correlation_id="credential-correlation",
            conversation_id="credential-conversation", session_key="credential-session",
        )
    [closing] = [event for event in events if event.message_type == "secure_credential_result"]
    assert closing.correlation_id == request.credential_request_id
    assert closing.text == (
        "The secure form for `example_signup` could not be checked. "
        "The gateway cannot confirm whether anything was stored or changed."
    )
    result = gateway._manager_client.publish_durable.await_args.args[0].msg_content
    assert result.status == "credential_unavailable"
    assert result.error == "Credential collection result could not be confirmed."


@pytest.mark.asyncio
async def test_live_sealbox_credential_collection_form_and_catalog_resolution(
    live_sealbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE", "sealbox")
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "live-sealbox")
    context = ToolContext(canonical_user_id="live-user-a", principal_type="user")
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())

    async with TestClient(TestServer(service.build_app())) as client:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": {
                    "canonical_user_id": "live-user-a",
                    "principal_type": "user",
                },
                "request": {
                    "credential_key": "example_signup",
                    "service_name": "Example",
                    "store_target": "sealbox",
                    "fields": [
                        {
                            "name": "username",
                            "kind": "username",
                            "visibility": "metadata_visible",
                            "value_source": "agent_supplied_metadata",
                            "value": "user@example.com",
                        },
                        {
                            "name": "service_password",
                            "kind": "password",
                            "visibility": "secret",
                            "value_source": "store_generated_secret",
                            "generation_policy": "store_native",
                        },
                    ],
                },
            },
        )
        create_payload = await create.json()
        form_path = create_payload["form_url"].split(
            str(client.make_url("/").origin()), 1
        )[-1]
        form = await client.get(form_path)
        form_text = await form.text()
        submit = await client.post(
            form_path, data={"field__username": "user@example.com"}
        )
        status = await client.get(
            f"/v1/credential-collection/sessions/{create_payload['session_id']}",
            headers=gateway_headers(),
        )
        status_payload = await status.json()

    assert create.status == 200
    assert form.status == 200
    assert "will be generated inside the credential store" in form_text
    assert "field__service_password" not in form_text
    assert submit.status == 200
    assert status_payload["status"] == "credential_stored"
    assert "generated_password" not in json.dumps(
        status_payload["result"], sort_keys=True
    )

    resolved = credential_catalog.resolve_secret_field(
        "example_signup", "service_password", context=context
    )
    assert not hasattr(resolved, "status")
    assert resolved.source == "sealbox"
    assert len(resolved.value) == 32
    assert resolved.value not in json.dumps(status_payload["result"], sort_keys=True)
    assert (
        resolved.value.encode("utf-8")
        not in (tmp_path / "credential_catalog.db").read_bytes()
    )

    await _assert_unknown_form_result_after_broker_restart(tmp_path, create_payload["session_id"])
    # Losing the broker session must not be mistaken for losing the saved secret.
    after_restart = credential_catalog.resolve_secret_field(
        "example_signup", "service_password", context=context
    )
    assert after_restart.value == resolved.value

    _run_cli(
        live_sealbox["cli"],
        [
            "credential",
            "set",
            "team-nats-agents:live-sealbox:example_signup",
            "--username",
            "user@example.com",
            "--output",
            "json",
        ],
        env=live_sealbox["tenant_env"]("live-user-a"),
        input_text="rotated-password",
    )
    delete_result = credential_catalog.delete_credential(
        {"credential_key": "example_signup"},
        context=context,
    )
    assert delete_result.status == "success"
    assert (
        "example_signup"
        not in credential_catalog.load_catalog(
            scope=CredentialOwnerScope.user("live-user-a")
        ).credentials
    )
    listed_after_delete = _run_cli(
        live_sealbox["cli"],
        [
            "credential",
            "list",
            "--key",
            "team-nats-agents:live-sealbox:example_signup",
            "--output",
            "json",
        ],
        env=live_sealbox["tenant_env"]("live-user-a"),
    )
    assert (
        "team-nats-agents:live-sealbox:example_signup" not in listed_after_delete.stdout
    )


def test_live_sealbox_secret_delete_without_version_removes_all_versions(
    live_sealbox,
) -> None:
    key = "live-all-version-delete"
    env = live_sealbox["tenant_env"]("live-delete-user")

    _run_cli(
        live_sealbox["cli"], ["secret", "set", key], env=env, input_text="value-v1"
    )
    _run_cli(
        live_sealbox["cli"], ["secret", "set", key], env=env, input_text="value-v2"
    )
    _run_cli(live_sealbox["cli"], ["secret", "get", key, "--version", "1"], env=env)
    _run_cli(live_sealbox["cli"], ["secret", "get", key, "--version", "2"], env=env)

    delete = _run_cli(live_sealbox["cli"], ["secret", "delete", key], env=env)

    assert "deleted successfully" in delete.stdout
    for args in (
        ["secret", "get", key],
        ["secret", "get", key, "--version", "1"],
        ["secret", "get", key, "--version", "2"],
    ):
        missing = _run_cli_raw(live_sealbox["cli"], args, env=env)
        assert missing.returncode != 0


def test_live_sealbox_credential_state_backend(
    live_sealbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "credential_source"
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE", "sealbox")
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE",
        "agent-credential-store",
    )

    with credential_owner_context("live-state-alice"):
        alice_result = write_credential_state("github.token", "ghp_alice_secret")
    with credential_owner_context("live-state-bob"):
        bob_result = write_credential_state("github.token", "ghp_bob_secret")
    with credential_owner_context("live-state-alice"):
        alice_resolved = read_credential_state("github.token")
    with credential_owner_context("live-state-bob"):
        bob_resolved = read_credential_state("github.token")

    assert alice_result.status == "success"
    assert bob_result.status == "success"
    assert (
        alice_result.content["credential_state_item_key"]
        == "agent-credential-store:github:token"
    )
    assert (
        bob_result.content["credential_state_item_key"]
        == "agent-credential-store:github:token"
    )
    assert alice_resolved == "ghp_alice_secret"
    assert bob_resolved == "ghp_bob_secret"


def _model_key_state(monkeypatch):
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "credential_source")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE", "sealbox")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES", "false")
    from secure_tool_broker.model_key_page import person_model_key_store, team_model_key_store

    team_model_key_store.cache_clear()
    person_model_key_store.cache_clear()


def _model_key_entry(value):
    from datetime import datetime, timezone
    from model_providers.endpoints import EndpointKind
    from secure_tool_broker.model_keys import StoredModelKey

    return StoredModelKey(
        address="https://model.example", kind=EndpointKind.OPENAI_COMPATIBLE,
        key=value, saved_at=datetime.now(timezone.utc), saved_by_person_id="owner-1",
        tested_model_id="test-model",
    )


@pytest.mark.parametrize("scope", ["team", "user"])
def test_live_first_model_key_save_provisions_only_on_write(live_sealbox, monkeypatch, scope, caplog):
    from secure_tool_broker.model_keys import ModelKeyStore

    _model_key_state(monkeypatch)
    caplog.set_level(logging.INFO)
    catalog = CredentialCatalogStore(live_sealbox["root_env"]["SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH"])
    owner = CredentialOwnerScope(scope_kind=scope, owner_id="fresh-model-owner")
    store = ModelKeyStore(owner.owner_id, scope=scope)
    assert store.entries() == ()
    assert catalog.get_sealbox_tenant_mapping(owner) is None

    entry = _model_key_entry("first-model-save-canary")
    store.save(entry)
    assert catalog.get_sealbox_tenant_mapping(owner) is not None
    assert ModelKeyStore(owner.owner_id, scope=scope).get(entry.address) == entry
    assert entry.key not in caplog.text


@pytest.mark.asyncio
async def test_live_onboarding_model_key_handover_signed_lookup_and_restart(live_sealbox, tmp_path, monkeypatch, caplog):
    import asyncio
    from access_control.person_settings import PersonSettings
    from assistant_gateway.http_secure_broker import HttpGatewaySecureBroker
    from assistant_gateway.onboarding_account_keys import OnboardingAccountKeyHandover, read_handover, write_handover
    from custom_agents.model_keys import HttpModelKeyBroker
    from custom_messages.contracts.access_v2 import OnboardingModelKeyV1
    from runtime_settings.instance_layout import InstanceLayout
    from secure_tool_broker.model_key_page import person_model_key_store
    from tests.broker_access_support import TEST_TEAM, agent_context, caller_token
    from tests.broker_web_support import build_gate, start_broker

    _model_key_state(monkeypatch)
    caplog.set_level(logging.INFO)
    root = tmp_path / "instance"
    InstanceLayout.at(root).ensure_directories()
    entry = _model_key_entry("onboarding-sealbox-model-canary")
    path = write_handover(
        root, team_id=TEST_TEAM, person_id="owner-1", keys={},
        model_keys=[OnboardingModelKeyV1(address=entry.address, kind=entry.kind, model_id=entry.tested_model_id, value=entry.key)],
    )
    assert path is not None and path.exists()
    running, stop = await asyncio.to_thread(start_broker, build_gate(tmp_path), monkeypatch)
    try:
        # Before onboarding writes anything, a signed fresh-owner lookup is missing,
        # never HTTP 503, and must not provision either owner's tenant.
        fresh_reply = HttpModelKeyBroker().lookup(entry.address, agent_context("bob"))
        assert fresh_reply.state == "missing"
        notices = []
        handover = OnboardingAccountKeyHandover(
            instance_root=root, team_id=TEST_TEAM,
            broker=HttpGatewaySecureBroker(internal_base_url=running.base_url, public_base_url=running.base_url, token=caller_token("gateway")),
            roles_of=lambda person: ("owner",) if person == "owner-1" else None,
            send_notice=notices.append, retry_delays=(0.0,),
            # The gateway and the broker open the same access store (sharing choices).
            store=lambda: running.gate.links, catalog=lambda: running.gate.catalog,
        )
        await handover.run(asyncio.Event())
        assert read_handover(root) is None and not path.exists()
        assert notices
        client = HttpModelKeyBroker()
        reply = client.lookup(entry.address, agent_context("bob"))
        assert reply.key == entry.key and reply.owner == "team"
        catalog = CredentialCatalogStore(live_sealbox["root_env"]["SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH"])
        assert catalog.get_sealbox_tenant_mapping(CredentialOwnerScope.user("bob")) is None

        personal = _model_key_entry("alice-personal-model-canary")
        person_model_key_store("alice").save(personal)
        assert client.lookup(entry.address, agent_context("alice")).key == personal.key
        assert client.lookup(entry.address, agent_context("bob")).key == entry.key
        forged = agent_context("bob")
        forged["canonical_user_id"] = "alice"
        with pytest.raises(RuntimeError, match="HTTP 403"):
            client.lookup(entry.address, forged)
        PersonSettings(running.gate.links, TEST_TEAM).set_team_keys("bob", False, by="owner-1")
        refused = client.lookup(entry.address, agent_context("bob"))
        assert not refused.team_keys_allowed and not refused.key
        PersonSettings(running.gate.links, TEST_TEAM).set_team_keys("bob", True, by="owner-1")
    finally:
        stop()

    live_sealbox["restart"]()
    _model_key_state(monkeypatch)
    running, stop = await asyncio.to_thread(start_broker, build_gate(tmp_path), monkeypatch)
    try:
        client = HttpModelKeyBroker()
        assert client.lookup(entry.address, agent_context("bob")).key == entry.key
        assert client.lookup(entry.address, agent_context("alice")).key == personal.key
        assert not path.exists()
    finally:
        stop()
    assert entry.key not in caplog.text and personal.key not in caplog.text


@pytest.mark.parametrize(("filename", "damage"), [
    ("token", "missing"), ("public.pem", "missing"), ("private.pem", "missing"),
    ("token", "corrupt"), ("public.pem", "corrupt"), ("private.pem", "corrupt"),
    ("public.pem", "mismatched"), ("private.pem", "mismatched"),
])
def test_live_damaged_tenant_blocks_model_key_reads_and_writes(live_sealbox, monkeypatch, filename, damage, caplog):
    from secure_tool_broker.model_keys import ModelKeyStore, ModelKeyStoreError

    _model_key_state(monkeypatch)
    caplog.set_level(logging.INFO)
    entry = _model_key_entry("damaged-tenant-model-canary")
    store = ModelKeyStore("damaged-owner", scope="user")
    store.save(entry)
    catalog = CredentialCatalogStore(live_sealbox["root_env"]["SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH"])
    owner = CredentialOwnerScope.user("damaged-owner")
    mapping = catalog.get_sealbox_tenant_mapping(owner)
    directory = Path(live_sealbox["root_env"]["SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR"]) / mapping.material_id
    path = directory / filename
    if damage == "missing":
        path.unlink()
    elif damage == "mismatched":
        from secure_tool_broker.sealbox_runtime import ensure_sealbox_key_pair_files

        other = directory.parent / "other-pair"
        ensure_sealbox_key_pair_files(public_key_file=other / "public.pem", private_key_file=other / "private.pem")
        path.write_bytes((other / filename).read_bytes())
    else:
        path.write_text("invalid-tenant-material", encoding="utf-8")
    material = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()}
    fresh = ModelKeyStore(owner.owner_id, scope="user")
    with pytest.raises(ModelKeyStoreError):
        fresh.get(entry.address)
    with pytest.raises(ModelKeyStoreError):
        fresh.save(entry.model_copy(update={"key": "replacement-must-not-be-saved"}))
    with credential_owner_context(owner.owner_id):
        assert write_credential_state("model.endpoint_keys_json", "{}").status == "error"
    assert catalog.get_sealbox_tenant_mapping(owner) == mapping
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir() if p.is_file()} == material
    assert entry.key not in caplog.text and "replacement-must-not-be-saved" not in caplog.text


def test_live_removal_deletes_the_persons_sealbox_sign_ins_only(
    live_sealbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing a person deletes their sign-ins from their Sealbox tenant; others' stay."""

    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "credential_source"
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE", "sealbox")
    for person in ("live-departing", "live-staying"):
        for key in ("github.token", "gog.token_json", "x.sign_in_json"):
            stored = write_credential_state(key, f"{person}-{key}", owner_id=person)
            assert stored.status == "success", stored
    with credential_owner_context("live-team", scope_kind="team"):
        assert write_credential_state("x.oauth_client_json", "team-x-app").status == "success"
    request = PersonCredentialDeletionV1(team_id="live-team", person_id="live-departing")

    removed = delete_person_credentials(request)
    repeated = delete_person_credentials(request)
    never_signed_in = delete_person_credentials(
        request.model_copy(update={"person_id": "live-never-signed-in"})
    )

    assert removed.sign_ins_deleted == (SignInService.GITHUB, SignInService.GOOGLE, SignInService.X)
    # Sealbox holds the values, so this person never had a credentials folder.
    assert removed.sign_ins_failed == () and removed.folder is CredentialsFolderOutcome.ABSENT
    assert repeated.sign_ins_deleted == () and repeated.sign_ins_failed == ()
    assert never_signed_in.sign_ins_deleted == () and never_signed_in.sign_ins_failed == ()
    for key in ("github.token", "gog.token_json", "x.sign_in_json"):
        assert read_credential_state(key, owner_id="live-departing") is None
        assert read_credential_state(key, owner_id="live-staying") == f"live-staying-{key}"
    with credential_owner_context("live-team", scope_kind="team"):
        assert read_credential_state("x.oauth_client_json") == "team-x-app"


@pytest.mark.asyncio
async def test_live_two_users_same_key_api_hydration_restart_and_revocation(
    live_sealbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE", "sealbox")
    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "live-multi-user")
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())

    request_template = {
        "credential_key": "shared_logical_key",
        "service_name": "Live Echo",
        "store_target": "sealbox",
        "fields": [
            {
                "name": "api_key",
                "kind": "api_key",
                "visibility": "secret",
                "value_source": "user_input",
            }
        ],
    }

    async def collect_for_user(client: TestClient, owner_id: str, value: str) -> None:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": {
                    "canonical_user_id": owner_id,
                    "principal_type": "user",
                },
                "request": request_template,
            },
        )
        assert create.status == 200, await create.text()
        payload = await create.json()
        form_path = payload["form_url"].split(str(client.make_url("/").origin()), 1)[-1]
        submit = await client.post(
            form_path,
            data={
                "field__api_key": value,
                "canonical_user_id": "attacker-selected-owner",
                "owner_id": "attacker-selected-owner",
            },
        )
        assert submit.status == 200, await submit.text()
        status = await client.get(
            f"/v1/credential-collection/sessions/{payload['session_id']}",
            headers=gateway_headers(),
        )
        assert (await status.json())["status"] == "credential_stored"

    async with TestClient(TestServer(service.build_app())) as client:
        await collect_for_user(client, "user-alice", "alice-live-secret")
        await collect_for_user(client, "user-bob", "bob-live-secret")

    # Each person's own account link says where their credential goes; the
    # broker's gate chooses it, so a request cannot pick another person's.
    live_key = (
        CredentialInjectionV1(
            placement=InjectionPlacement.HEADER, name="X-Live-Key", field="api_key"
        ),
    )

    def person_context(owner_id: str) -> ToolContext:
        return ToolContext(
            canonical_user_id=owner_id,
            principal_type="user",
            account_link=account_link(
                "http:live-echo",
                "shared_logical_key",
                owner_id=owner_id,
                injections=live_key,
            ),
        )

    alice_context = person_context("user-alice")
    bob_context = person_context("user-bob")
    missing_context = ToolContext(
        canonical_user_id="user-charlie", principal_type="user"
    )
    alice = credential_catalog.resolve_secret_field(
        "shared_logical_key", "api_key", context=alice_context
    )
    bob = credential_catalog.resolve_secret_field(
        "shared_logical_key", "api_key", context=bob_context
    )
    missing = credential_catalog.resolve_secret_field(
        "shared_logical_key", "api_key", context=missing_context
    )
    assert alice.value == "alice-live-secret"
    assert bob.value == "bob-live-secret"
    assert getattr(missing, "status", None) == "error"

    received: list[str] = []

    class EchoHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            received.append(self.headers.get("X-Live-Key", ""))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    echo = ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    thread = threading.Thread(target=echo.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{echo.server_address[1]}/check"
        for context, attempted_owner in (
            (alice_context, "user-bob"),
            (bob_context, "user-alice"),
        ):
            result = credential_catalog.api_request(
                {"owner_id": attempted_owner, "method": "GET", "url": url},
                context=context,
            )
            assert result.status == "success", result
        placeholder = credential_catalog.api_request(
            {"method": "GET", "url": url, "headers": {"X-Live-Key": "${LIVE_API_KEY}"}},
            context=alice_context,
        )
        assert placeholder.content["error_code"] == "api_request_placeholders_unsupported"
    finally:
        echo.shutdown()
        echo.server_close()
        thread.join(timeout=3)
    assert received == ["alice-live-secret", "bob-live-secret"]

    store = CredentialCatalogStore(tmp_path / "credential_catalog.db")
    alice_mapping = store.get_sealbox_tenant_mapping(
        CredentialOwnerScope.user("user-alice")
    )
    bob_mapping = store.get_sealbox_tenant_mapping(
        CredentialOwnerScope.user("user-bob")
    )
    assert alice_mapping is not None
    assert bob_mapping is not None
    assert alice_mapping.tenant_id != bob_mapping.tenant_id
    assert alice_mapping.material_id != bob_mapping.material_id

    live_sealbox["restart"]()
    bob_after_restart = credential_catalog.resolve_secret_field(
        "shared_logical_key", "api_key", context=bob_context
    )
    assert bob_after_restart.value == "bob-live-secret"

    deleted = credential_catalog.delete_credential(
        {"credential_key": "shared_logical_key"},
        context=alice_context,
    )
    assert deleted.status == "success"
    bob_after_alice_delete = credential_catalog.resolve_secret_field(
        "shared_logical_key", "api_key", context=bob_context
    )
    assert bob_after_alice_delete.value == "bob-live-secret"

    listed = _run_cli(
        live_sealbox["cli"],
        ["tenant", "token", "list", bob_mapping.tenant_id, "--output", "json"],
        env=live_sealbox["root_env"],
    )
    token_payload = json.loads(listed.stdout)
    token_id = token_payload["tokens"][0]["id"]
    _run_cli(
        live_sealbox["cli"],
        [
            "tenant",
            "token",
            "revoke",
            bob_mapping.tenant_id,
            token_id,
            "--output",
            "json",
        ],
        env=live_sealbox["root_env"],
    )
    revoked = credential_catalog.resolve_secret_field(
        "shared_logical_key", "api_key", context=bob_context
    )
    assert getattr(revoked, "status", None) == "error"


@pytest.mark.asyncio
async def test_live_team_tenants_are_isolated_restartable_and_revocable(
    live_sealbox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE", "sealbox")
    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "live-team-scope")
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())

    async def collect_for_team(
        client: TestClient,
        *,
        user_id: str,
        team_id: str,
        value: str,
    ) -> None:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": {
                    "canonical_user_id": user_id,
                    "team_id": team_id,
                    "principal_type": "user",
                },
                "request": {
                    "credential_key": "shared_team_key",
                    "service_name": "Live Team MCP",
                    "store_target": "sealbox",
                    "owner_scope_kind": "team",
                    "fields": [
                        {
                            "name": "access_token",
                            "kind": "token",
                            "visibility": "secret",
                            "value_source": "user_input",
                        }
                    ],
                },
            },
        )
        assert create.status == 200, await create.text()
        payload = await create.json()
        form_path = payload["form_url"].split(str(client.make_url("/").origin()), 1)[-1]
        submitted = await client.post(
            form_path,
            data={
                "field__access_token": value,
                "team_id": "attacker-selected-team",
                "owner_id": "attacker-selected-team",
            },
        )
        assert submitted.status == 200, await submitted.text()

    async with TestClient(TestServer(service.build_app())) as client:
        await collect_for_team(
            client,
            user_id="team-a-admin",
            team_id="team-a",
            value="team-a-live-secret",
        )
        await collect_for_team(
            client,
            user_id="team-b-admin",
            team_id="team-b",
            value="team-b-live-secret",
        )

    team_a_scope = CredentialOwnerScope.team("team-a")
    team_b_scope = CredentialOwnerScope.team("team-b")
    team_a = credential_catalog.resolve_secret_field(
        "shared_team_key", "access_token", scope=team_a_scope
    )
    team_b = credential_catalog.resolve_secret_field(
        "shared_team_key", "access_token", scope=team_b_scope
    )
    user_view = credential_catalog.resolve_secret_field(
        "shared_team_key",
        "access_token",
        scope=CredentialOwnerScope.user("team-a-admin"),
    )
    assert team_a.value == "team-a-live-secret"
    assert team_b.value == "team-b-live-secret"
    assert getattr(user_view, "status", None) == "error"

    store = CredentialCatalogStore(tmp_path / "credential_catalog.db")
    team_a_mapping = store.get_sealbox_tenant_mapping(team_a_scope)
    team_b_mapping = store.get_sealbox_tenant_mapping(team_b_scope)
    assert team_a_mapping is not None
    assert team_b_mapping is not None
    assert team_a_mapping.tenant_id != team_b_mapping.tenant_id
    assert team_a_mapping.material_id != team_b_mapping.material_id

    live_sealbox["restart"]()
    after_restart = credential_catalog.resolve_secret_field(
        "shared_team_key", "access_token", scope=team_b_scope
    )
    assert after_restart.value == "team-b-live-secret"

    deleted = credential_catalog.delete_credential(
        {"credential_key": "shared_team_key"},
        scope=team_a_scope,
    )
    assert deleted.status == "success"
    team_b_after_delete = credential_catalog.resolve_secret_field(
        "shared_team_key", "access_token", scope=team_b_scope
    )
    assert team_b_after_delete.value == "team-b-live-secret"

    listed = _run_cli(
        live_sealbox["cli"],
        ["tenant", "token", "list", team_b_mapping.tenant_id, "--output", "json"],
        env=live_sealbox["root_env"],
    )
    token_id = json.loads(listed.stdout)["tokens"][0]["id"]
    _run_cli(
        live_sealbox["cli"],
        [
            "tenant",
            "token",
            "revoke",
            team_b_mapping.tenant_id,
            token_id,
            "--output",
            "json",
        ],
        env=live_sealbox["root_env"],
    )
    revoked = credential_catalog.resolve_secret_field(
        "shared_team_key", "access_token", scope=team_b_scope
    )
    assert getattr(revoked, "status", None) == "error"


def test_live_team_transfer_carries_an_api_key_kept_in_sealbox(
    live_sealbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The transfer reads a catalog API key from real Sealbox and stores it again for the new instance."""

    from secure_tool_broker.credential_transfer import export_credentials, import_credentials

    team_id = "team-transfer-live"
    secret_value = "live-team-api-key-kept-in-sealbox"
    scope = CredentialOwnerScope.team(team_id)
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "transfer-live")
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH", str(tmp_path / "template_overrides.yaml")
    )
    saved = credential_catalog.collect_credential(
        {
            "credential_key": "search_api",
            "display_name": "Search API",
            "store_target": "sealbox",
            "fields": [{"name": "api_key", "kind": "api_key", "visibility": "secret", "value_source": "user_input"}],
        },
        {"api_key": secret_value},
        scope=scope,
    )
    assert saved.status == "success", saved.error

    records = export_credentials(team_id, [])
    [entry] = [record for record in records if record["kind"] == "catalog_entry"]
    assert entry["secret_values"] == {"api_key": secret_value}

    # The new instance has its own catalog; the value is stored again through it.
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "new_credential_catalog.db"))
    credential_catalog._SECRET_CACHE.clear()
    [outcome] = import_credentials([entry], team_id=team_id)
    assert outcome.imported, outcome.reason
    resolved = credential_catalog.resolve_secret_field("search_api", "api_key", scope=scope)
    assert isinstance(resolved, credential_catalog.ResolvedCredentialField), resolved
    assert (resolved.value, resolved.source) == (secret_value, "sealbox")
    assert secret_value not in caplog.text


def test_live_team_transfer_keeps_colliding_names_and_existing_secrets_separate(
    live_sealbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """New catalog references must not overwrite another field or an existing destination secret."""

    from secure_tool_broker.credential_transfer import export_credentials, import_credentials

    team_id = "team-transfer-collisions-live"
    scope = CredentialOwnerScope.team(team_id)
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "transfer-collisions-live")
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH", str(tmp_path / "template_overrides.yaml")
    )
    expected = {".api_key": "live-imported-first-secret", "api_key": "live-imported-second-secret"}
    saved = credential_catalog.collect_credential(
        {
            "credential_key": ".search_api",
            "display_name": "Imported API",
            "fields": [
                {"name": name, "kind": "api_key", "field_ref": f"source-field-{index}"}
                for index, name in enumerate(expected)
            ],
        },
        expected,
        scope=scope,
    )
    assert saved.status == "success", saved.error
    records = export_credentials(team_id, [])

    # The same real server gives the new catalog its own tenant and secret storage.
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "destination_catalog.db"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR", str(tmp_path / "destination_tenants"))
    credential_catalog._SECRET_CACHE.clear()
    existing_value = "live-existing-destination-secret"
    saved = credential_catalog.collect_credential(
        {
            "credential_key": "search_api",
            "display_name": "Existing API",
            "fields": [{"name": "api_key", "kind": "api_key"}],
        },
        {"api_key": existing_value},
        scope=scope,
    )
    assert saved.status == "success", saved.error

    [outcome] = import_credentials(records, team_id=team_id)
    assert outcome.imported, outcome.reason
    for name, value in expected.items():
        resolved = credential_catalog.resolve_secret_field(".search_api", name, scope=scope)
        assert isinstance(resolved, credential_catalog.ResolvedCredentialField)
        assert (resolved.value, resolved.source) == (value, "sealbox")
    existing = credential_catalog.resolve_secret_field("search_api", "api_key", scope=scope)
    assert isinstance(existing, credential_catalog.ResolvedCredentialField)
    assert existing.value == existing_value
    [repeated] = import_credentials(records, team_id=team_id)
    assert not repeated.imported and "kept the current credential" in repeated.reason
    for value in [*expected.values(), existing_value]:
        assert value not in caplog.text


def test_live_team_transfer_reuses_secrets_after_catalog_storage_failure(
    live_sealbox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A refused catalog save can retry against the same real Sealbox item and publish a complete entry."""
    from secure_tool_broker.credential_transfer import export_credentials, import_credentials

    team_id = "team-transfer-retry-live"
    scope = CredentialOwnerScope.team(team_id)
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "transfer-retry-live")
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH", str(tmp_path / "template_overrides.yaml")
    )
    secret_value = "live-retried-catalog-secret"
    saved = credential_catalog.collect_credential(
        {"credential_key": "search_api", "display_name": "Search account",
         "fields": [{"name": "api_key", "kind": "api_key"}, {"name": "username", "kind": "username"}]},
        {"api_key": secret_value, "username": "account-owner"}, scope=scope,
    )
    assert saved.status == "success", saved.error
    records = export_credentials(team_id, [])
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "retry_catalog.db"))
    monkeypatch.setenv("SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR", str(tmp_path / "retry_tenants"))
    credential_catalog._SECRET_CACHE.clear()
    original_put = CredentialCatalogStore.put_credential
    original_run = credential_catalog.run_sealbox
    attempted_rows = []
    written_items = []

    def fail_first_put(self, owner_scope, key, credential):
        attempted_rows.append(credential)
        if len(attempted_rows) == 1:
            raise RuntimeError("catalog storage unavailable")
        return original_put(self, owner_scope, key, credential)

    def record_secret_writes(args, **kwargs):
        if list(args[:2]) == ["secret", "set"]:
            written_items.append(args[2])
        return original_run(args, **kwargs)

    monkeypatch.setattr(CredentialCatalogStore, "put_credential", fail_first_put)
    monkeypatch.setattr(credential_catalog, "run_sealbox", record_secret_writes)
    [failed] = import_credentials(records, team_id=team_id)
    assert not failed.imported
    assert "search_api" not in credential_catalog.load_catalog(scope=scope).credentials
    [retried] = import_credentials(records, team_id=team_id)
    assert retried.imported, retried.reason
    assert len(written_items) == 2 and written_items[0] == written_items[1]
    assert len(attempted_rows) == 2
    assert all(set(row["fields"]) == {"api_key", "username"} for row in attempted_rows)
    restored = credential_catalog.load_catalog(scope=scope).credentials["search_api"]
    assert restored.fields["username"].value == "account-owner"
    resolved = credential_catalog.resolve_secret_field("search_api", "api_key", scope=scope)
    assert isinstance(resolved, credential_catalog.ResolvedCredentialField)
    assert (resolved.value, resolved.source) == (secret_value, "sealbox")
    assert resolved.source_ref["item_id"] == written_items[0]
    assert "event=credentials_transfer_catalog_import_failed" in caplog.text
    assert secret_value not in caplog.text

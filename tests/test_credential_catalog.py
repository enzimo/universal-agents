import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest

from runtime_settings.openshell_naming import sandbox_name as openshell_sandbox_name
from aiohttp.test_utils import TestClient, TestServer

from custom_agents.message_routing import is_response_content
from custom_agents.structured_llm_messages import StructuredLLMResponse
from custom_messages.contracts.access_v2 import (
    AccountLinkV1,
    CredentialInjectionV1,
    InjectionPlacement,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
)
from custom_messages.contracts.credentials_v1 import SecureCredentialCollectionRequest, SecureCredentialCollectionResult, SecureCredentialFieldSpec
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from secure_tool_broker import credential_catalog
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    CatalogCredentialField,
    CatalogSourceRef,
    CredentialCatalog,
    CredentialWriteResult,
)
from secure_tool_broker.credential_catalog_store import (
    CredentialCatalogStore,
    CredentialOwnerScope,
    resolve_catalog_db_path,
)
from secure_tool_broker.service import (
    CredentialCollectionSession,
    SecureToolBrokerService,
    _render_manual_signup_action,
    _run_claimed_form_thread,
)
from tests.broker_access_support import BROKER_ROOT, gateway_headers, open_gate
from secure_tools.providers import ToolContext, ToolResult, ValidatedAction


TEST_CANONICAL_USER_ID = "credential-catalog-test-user"
TEST_TOOL_CONTEXT = ToolContext(
    agent_id="agent-1",
    canonical_user_id=TEST_CANONICAL_USER_ID,
    principal_type="user",
)
TEST_TRUSTED_CONTEXT = {
    "agent_id": "agent-1",
    "canonical_user_id": TEST_CANONICAL_USER_ID,
    "principal_type": "user",
}
TEST_OWNER_SCOPE = CredentialOwnerScope.user(TEST_CANONICAL_USER_ID)
TEST_TEAM_ID = "team-1"
TEST_TEAM_SCOPE = CredentialOwnerScope.team(TEST_TEAM_ID)
LINKED_TOOL_ID = "http:example"
LINK_CREATED_AT = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
OPENSHELL = "/usr/local/bin/openshell"


@pytest.fixture(autouse=True)
def _isolate_catalog_and_stub_tenant_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH",
        str(tmp_path / "credential_catalog.db"),
    )
    monkeypatch.setattr(
        credential_catalog,
        "sealbox_tenant_environment",
        lambda **_kwargs: {},
    )


def test_generated_password_never_appears_in_agent_visible_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "GENERATED-PASSWORD-SHOULD-NOT-LEAK"
    monkeypatch.setattr(
        credential_catalog, "generate_password", lambda **_kwargs: secret
    )

    captured_values: dict[str, str] = {}

    def fake_write(**kwargs):
        captured_values.update(kwargs["values"])
        return CredentialWriteResult(
            source="bitwarden",
            item_id="item-1",
            item_name="team :: example",
            source_ref_by_field={
                "service_password": CatalogSourceRef(
                    source="bitwarden",
                    item_id="item-1",
                    item_name="team :: example",
                    field_ref="password",
                )
            },
        )

    monkeypatch.setattr(credential_catalog, "write_credential_to_source", fake_write)

    result = credential_catalog.collect_credential(
        {
            "credential_key": "example_service_login",
            "display_name": "Example Service Login",
            "purpose": "Account created for approved agent workflow.",
            "store_target": "bitwarden",
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
                    "generation_policy": "store_native_with_bitwarden_fallback",
                },
            ],
        },
        {}, scope=TEST_OWNER_SCOPE,
    )

    assert result.status == "success"
    assert captured_values["service_password"] == secret
    rendered_result = json.dumps(result.content, sort_keys=True)
    assert secret not in rendered_result
    assert secret.encode() not in (tmp_path / "credential_catalog.db").read_bytes()
    assert result.content["metadata"]["fields"]["service_password"]["secret"] is True
    assert "value" not in result.content["metadata"]["fields"]["service_password"]


def test_sealbox_generated_password_is_stored_by_cli_without_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "test-instance")
    calls: list[dict[str, object]] = []

    def fake_run_sealbox(args, *, input_text=None, env=None, timeout_seconds=30):
        calls.append({"args": list(args), "input_text": input_text})
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout='{"version":1}\n', stderr=""
        )

    monkeypatch.setattr(credential_catalog, "run_sealbox", fake_run_sealbox)

    result = credential_catalog.collect_credential(
        {
            "credential_key": "example_service_login",
            "display_name": "Example Service Login",
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
        {}, scope=TEST_OWNER_SCOPE,
    )

    assert result.status == "success"
    assert result.content["write_source"] == "sealbox"
    assert (
        result.content["item_id"]
        == "team-nats-agents:test-instance:example_service_login"
    )
    assert calls == [
        {
            "args": [
                "credential",
                "set",
                "team-nats-agents:test-instance:example_service_login",
                "--username",
                "user@example.com",
                "--output",
                "json",
                "--generate-password",
                "--length",
                "32",
            ],
            "input_text": None,
        }
    ]
    rendered = json.dumps(result.content, sort_keys=True)
    assert "generated_password" not in rendered
    stored = credential_catalog.load_catalog(scope=TEST_OWNER_SCOPE)
    password = stored.credentials["example_service_login"].fields["service_password"]
    assert password.resolution_order[0].source == "sealbox"
    assert "generated_password" not in json.dumps(
        stored.model_dump(mode="json"), sort_keys=True
    )


def test_bitwarden_login_payload_maps_username_and_password_to_login_fields() -> None:
    payload = credential_catalog._bitwarden_login_payload(
        item_name="team-nats-agents :: default :: home_assistant_login",
        request_payload={
            "credential_key": "home_assistant_login",
            "purpose": "Login credential for approved Home Assistant workflows.",
            "service_url": "http://homeassistant.local:8123",
        },
        values={
            "username": "ha-user",
            "service_password": "USER-SUBMITTED-PASSWORD",
        },
        field_specs={
            "username": {
                "name": "username",
                "kind": "username",
                "visibility": "metadata_visible",
            },
            "service_password": {
                "name": "service_password",
                "kind": "password",
                "visibility": "secret",
            },
        },
        existing_item=None,
    )

    assert payload["type"] == 1
    assert payload["login"]["username"] == "ha-user"
    assert payload["login"]["password"] == "USER-SUBMITTED-PASSWORD"
    assert payload["login"]["uris"] == [{"uri": "http://homeassistant.local:8123"}]
    assert all(field["name"] != "service_password" for field in payload["fields"])


def test_collect_credential_rejects_empty_field_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    result = credential_catalog.collect_credential(
        {
            "credential_key": "blank_login",
            "display_name": "Blank Login",
            "purpose": "Should not create an empty password-manager item.",
            "fields": [],
        },
        {}, scope=TEST_OWNER_SCOPE,
    )

    assert result.status == "error"
    assert result.content["error_code"] == "credential_fields_required"
    assert "requires at least one credential field spec" in result.error


def test_secret_field_value_in_request_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    result = credential_catalog.collect_credential(
        {
            "credential_key": "bad_request",
            "display_name": "Bad Request",
            "purpose": "Should reject secret values in metadata.",
            "fields": [
                {
                    "name": "password",
                    "kind": "password",
                    "visibility": "secret",
                    "value_source": "agent_supplied_metadata",
                    "value": "DO-NOT-ACCEPT",
                }
            ],
        },
        {}, scope=TEST_OWNER_SCOPE,
    )

    assert result.status == "error"
    assert result.content["error_code"] == "secret_value_in_request_rejected"


def test_collect_missing_required_field_explains_gateway_form_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    result = credential_catalog.collect_credential(
        {
            "credential_key": "home_assistant_login",
            "display_name": "Home Assistant Login",
            "purpose": "Login for approved Home Assistant automation.",
            "fields": [
                {
                    "name": "username",
                    "kind": "username",
                    "visibility": "metadata_visible",
                    "value_source": "user_input",
                },
                {
                    "name": "service_password",
                    "kind": "password",
                    "visibility": "secret",
                    "value_source": "user_input",
                },
            ],
        },
        {}, scope=TEST_OWNER_SCOPE,
    )

    assert result.status == "error"
    assert result.content["error_code"] == "missing_required_credential_field"
    assert (
        result.content["form_creation_action"] == "secure_credential_collection_request"
    )
    assert "does not create a user-facing form" in result.error
    assert "action_type=secure_credential_collection_request" in result.error


def test_signup_prepare_reports_it_does_not_create_form() -> None:
    result = credential_catalog.signup_prepare(
        {
            "service_name": "Home Assistant",
            "signup_url": "https://homeassistant.local",
            "credential_key": "home_assistant_login",
            "username": "user@example.com",
        }
    )

    assert result.status == "success"
    assert result.content["status"] == "signup_prepared"
    assert result.content["creates_form"] is False
    assert (
        "action_type=secure_credential_collection_request"
        in result.content["next_step"]
    )


def test_generated_secret_form_field_renders_as_store_generated_note() -> None:
    from secure_tool_broker.service import CredentialCollectionSession

    rendered = SecureToolBrokerService._render_credential_collection_form(
        CredentialCollectionSession(
            request_payload={
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
                        "description": "Use the **account email** from `example.com`.\n\n<script>alert(1)</script>",
                    },
                    {
                        "name": "service_password",
                        "kind": "password",
                        "visibility": "secret",
                        "value_source": "store_generated_secret",
                        "generation_policy": "store_native",
                        "label": "Password",
                    },
                ],
            },
            tool_context=TEST_TOOL_CONTEXT,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=45),
        )
    )

    assert "will be generated inside the credential store" in rendered
    assert "field__service_password" not in rendered
    assert "field__username" in rendered
    assert "UAWebChatMarkdown" in rendered
    assert "data-markdown=" in rendered
    assert "Use the **account email** from `example.com`." in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<script>alert(1)</script>" not in rendered
    assert 'class="brand-mark"' in rendered
    assert "data:image/png;base64," in rendered
    assert "Secure tool broker authentication" in rendered
    assert "--broker-chip-bg: rgba(220, 252, 231, 0.76)" in rendered
    assert "Session expires in" in rendered
    assert "data-session-countdown" in rendered
    assert "formatRemaining" in rendered
    assert "45:00" in rendered or "44:59" in rendered
    assert "Session expires at" not in rendered


def test_credential_deletion_form_requires_checkbox_and_red_delete_button() -> None:
    from secure_tool_broker.service import CredentialCollectionSession

    rendered = SecureToolBrokerService._render_credential_collection_form(
        CredentialCollectionSession(
            request_payload={
                "credential_key": "test_credential",
                "service_name": "Test Credential",
                "flow": "credential_deletion",
                "source": "sealbox",
                "source_item_id": "team-nats-agents:default:test_credential",
                "fields": [],
            },
            tool_context=TEST_TOOL_CONTEXT,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
    )

    assert "Secure Credential Deletion" in rendered
    assert "Credential source: <code>sealbox</code>" in rendered
    assert "team-nats-agents:default:test_credential" in rendered
    assert "Delete target type: <code>credential</code>" in rendered
    assert "This will delete the credential." in rendered
    assert "I understand. Proceed." in rendered
    assert 'name="confirm_delete"' in rendered
    assert (
        'style="border-color:#b91c1c;background:#dc2626;color:#ffffff;">Delete</button>'
        in rendered
    )


def test_delete_catalog_credential_removes_sealbox_record_and_catalog_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = CredentialCatalog(
        credentials={
            "test_credential": CatalogCredential(
                display_name="Test Credential",
                purpose="Deletion regression test.",
                fields={
                    "username": CatalogCredentialField(
                        kind="username",
                        visibility="metadata_visible",
                        value_source="user_or_agent_supplied_metadata",
                        value="test-user",
                    ),
                    "password": CatalogCredentialField(
                        kind="password",
                        visibility="secret",
                        value_source="user_input",
                        resolution_order=[
                            CatalogSourceRef(
                                source="sealbox",
                                item_id="team-nats-agents:default:test_credential",
                                item_name="team-nats-agents:default:test_credential",
                                field_ref="password",
                            )
                        ],
                    ),
                },
            )
        }
    )
    credential_catalog.save_catalog(catalog, scope=TEST_OWNER_SCOPE)
    commands: list[list[str]] = []

    def fake_run_sealbox(args, *, input_text=None, env=None, timeout_seconds=30):
        commands.append(list(args))
        assert input_text is None
        if args == ["credential", "delete", "team-nats-agents:default:test_credential"]:
            return subprocess.CompletedProcess(
                ["sealbox-cli", *args], 0, stdout="", stderr=""
            )
        raise AssertionError(f"unexpected sealbox args: {args}")

    monkeypatch.setattr(
        "secure_tool_broker.credential_catalog.run_sealbox", fake_run_sealbox
    )

    result = credential_catalog.delete_credential({"credential_key": "test_credential"}, scope=TEST_OWNER_SCOPE)

    assert result.status == "success"
    assert result.content["status"] == "credential_deleted"
    assert result.content["removed_catalog_entry"] is True
    assert result.content["deleted_source_refs"] == [
        {
            "source": "sealbox",
            "item_key": "team-nats-agents:default:test_credential",
            "item_kind": "credential",
        }
    ]
    assert commands == [
        ["credential", "delete", "team-nats-agents:default:test_credential"]
    ]
    assert "test_credential" not in credential_catalog.load_catalog(scope=TEST_OWNER_SCOPE).credentials


def test_delete_catalog_credential_falls_back_when_credential_delete_is_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_key = "team-nats-agents:default:test_credential"
    catalog = CredentialCatalog(
        credentials={
            "test_credential": CatalogCredential(
                display_name="Test Credential",
                purpose="Deletion target regression test.",
                fields={
                    "password": CatalogCredentialField(
                        kind="password",
                        visibility="secret",
                        value_source="existing_source",
                        resolution_order=[
                            CatalogSourceRef(
                                source="sealbox",
                                item_id=item_key,
                                item_name=item_key,
                                field_ref="password",
                            )
                        ],
                    )
                },
            )
        }
    )
    credential_catalog.save_catalog(catalog, scope=TEST_OWNER_SCOPE)
    commands: list[list[str]] = []

    def fake_run_sealbox(args, *, input_text=None, env=None, timeout_seconds=30):
        commands.append(list(args))
        assert input_text is None
        if args == ["credential", "delete", item_key]:
            return subprocess.CompletedProcess(
                ["sealbox-cli", *args],
                2,
                stdout="",
                stderr=(
                    "error: unrecognized subcommand 'delete'\n\n"
                    "Usage: sealbox-cli credential [OPTIONS] <COMMAND>"
                ),
            )
        if args == ["secret", "delete", item_key]:
            return subprocess.CompletedProcess(
                ["sealbox-cli", *args], 0, stdout="", stderr=""
            )
        raise AssertionError(f"unexpected sealbox args: {args}")

    monkeypatch.setattr(
        "secure_tool_broker.credential_catalog.run_sealbox", fake_run_sealbox
    )

    result = credential_catalog.delete_credential({"credential_key": "test_credential"}, scope=TEST_OWNER_SCOPE)

    assert result.status == "success"
    assert result.content["status"] == "credential_deleted"
    assert result.content["deleted_source_refs"] == [
        {
            "source": "sealbox",
            "item_key": item_key,
            "item_kind": "credential",
            "delete_command_fallback": "secret.delete_after_credential_delete_unsupported",
        }
    ]
    assert commands == [
        ["credential", "delete", item_key],
        ["secret", "delete", item_key],
    ]
    assert "test_credential" not in credential_catalog.load_catalog(scope=TEST_OWNER_SCOPE).credentials


def test_delete_catalog_credential_fallback_requires_current_sealbox_cli_when_secret_delete_needs_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_key = "team-nats-agents:default:test_credential"
    catalog = CredentialCatalog(
        credentials={
            "test_credential": CatalogCredential(
                display_name="Test Credential",
                purpose="Deletion target regression test.",
                fields={
                    "password": CatalogCredentialField(
                        kind="password",
                        visibility="secret",
                        value_source="existing_source",
                        resolution_order=[
                            CatalogSourceRef(
                                source="sealbox",
                                item_id=item_key,
                                item_name=item_key,
                                field_ref="password",
                            )
                        ],
                    )
                },
            )
        }
    )
    credential_catalog.save_catalog(catalog, scope=TEST_OWNER_SCOPE)
    commands: list[list[str]] = []

    def fake_run_sealbox(args, *, input_text=None, env=None, timeout_seconds=30):
        commands.append(list(args))
        assert input_text is None
        if args == ["credential", "delete", item_key]:
            return subprocess.CompletedProcess(
                ["sealbox-cli", *args],
                2,
                stdout="",
                stderr=(
                    "error: unrecognized subcommand 'delete'\n\n"
                    "Usage: sealbox-cli credential [OPTIONS] <COMMAND>"
                ),
            )
        if args == ["secret", "delete", item_key]:
            return subprocess.CompletedProcess(
                ["sealbox-cli", *args],
                2,
                stdout="",
                stderr=(
                    "error: the following required arguments were not provided:\n"
                    "  --version <VERSION>\n\n"
                    "Usage: sealbox-cli secret delete --version <VERSION> <KEY>"
                ),
            )
        raise AssertionError(f"unexpected sealbox args: {args}")

    monkeypatch.setattr(
        "secure_tool_broker.credential_catalog.run_sealbox", fake_run_sealbox
    )

    result = credential_catalog.delete_credential({"credential_key": "test_credential"}, scope=TEST_OWNER_SCOPE)

    assert result.status == "error"
    assert result.content["error_code"] == "sealbox_cli_update_required"
    assert "Current Sealbox supports `secret delete <key>`" in (result.error or "")
    assert commands == [
        ["credential", "delete", item_key],
        ["secret", "delete", item_key],
    ]
    assert "test_credential" in credential_catalog.load_catalog(scope=TEST_OWNER_SCOPE).credentials


def test_delete_catalog_credential_prefers_explicit_sealbox_credential_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_item = "team-nats-agents:default:test_credential"
    custom_item = f"{root_item}:api_token"
    catalog = CredentialCatalog(
        credentials={
            "test_credential": CatalogCredential(
                display_name="Test Credential",
                purpose="Deletion target regression test.",
                fields={
                    "password": CatalogCredentialField(
                        kind="password",
                        visibility="secret",
                        value_source="existing_source",
                        resolution_order=[
                            CatalogSourceRef(
                                source="sealbox",
                                item_id=root_item,
                                item_name=root_item,
                                field_ref="value",
                            )
                        ],
                    ),
                    "api_token": CatalogCredentialField(
                        kind="token",
                        visibility="secret",
                        value_source="existing_source",
                        resolution_order=[
                            CatalogSourceRef(
                                source="sealbox",
                                item_id=custom_item,
                                item_name=custom_item,
                                field_ref="value",
                            )
                        ],
                    ),
                },
            )
        }
    )
    credential_catalog.save_catalog(catalog, scope=TEST_OWNER_SCOPE)
    commands: list[list[str]] = []

    def fake_run_sealbox(args, *, input_text=None, env=None, timeout_seconds=30):
        commands.append(list(args))
        assert input_text is None
        if args in (
            ["credential", "delete", root_item],
            ["secret", "delete", custom_item],
        ):
            return subprocess.CompletedProcess(
                ["sealbox-cli", *args], 0, stdout="", stderr=""
            )
        raise AssertionError(f"unexpected sealbox args: {args}")

    monkeypatch.setattr(
        "secure_tool_broker.credential_catalog.run_sealbox", fake_run_sealbox
    )

    result = credential_catalog.delete_credential(
        {
            "credential_key": "test_credential",
            "flow": "credential_deletion",
            "source": "sealbox",
            "source_item_id": root_item,
        }, scope=TEST_OWNER_SCOPE
    )

    assert result.status == "success"
    assert result.content["deleted_source_refs"] == [
        {
            "source": "sealbox",
            "item_key": root_item,
            "item_kind": "credential",
        },
        {
            "source": "sealbox",
            "item_key": custom_item,
            "item_kind": "secret",
        },
    ]
    assert commands == [
        ["credential", "delete", root_item],
        ["secret", "delete", custom_item],
    ]
    assert "test_credential" not in credential_catalog.load_catalog(scope=TEST_OWNER_SCOPE).credentials


def test_delete_catalog_credential_removes_catalog_when_sealbox_item_already_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_key = "team-nats-agents:default:test_credential"
    catalog = CredentialCatalog(
        credentials={
            "test_credential": CatalogCredential(
                display_name="Test Credential",
                purpose="Deletion target regression test.",
                fields={
                    "password": CatalogCredentialField(
                        kind="password",
                        visibility="secret",
                        value_source="existing_source",
                        resolution_order=[
                            CatalogSourceRef(
                                source="sealbox",
                                item_id=item_key,
                                item_name=item_key,
                                field_ref="password",
                            )
                        ],
                    )
                },
            )
        }
    )
    credential_catalog.save_catalog(catalog, scope=TEST_OWNER_SCOPE)

    def fake_run_sealbox(args, *, input_text=None, env=None, timeout_seconds=30):
        assert input_text is None
        assert args == ["credential", "delete", item_key]
        return subprocess.CompletedProcess(
            ["sealbox-cli", *args],
            1,
            stdout="",
            stderr=f"[SealboxError] Secret not found: {item_key}",
        )

    monkeypatch.setattr(
        "secure_tool_broker.credential_catalog.run_sealbox", fake_run_sealbox
    )

    result = credential_catalog.delete_credential({"credential_key": "test_credential"}, scope=TEST_OWNER_SCOPE)

    assert result.status == "success"
    assert result.content["status"] == "credential_deleted"
    assert result.content["deleted_source_refs"] == [
        {
            "source": "sealbox",
            "item_key": item_key,
            "item_kind": "credential",
            "already_absent": True,
        }
    ]
    assert "test_credential" not in credential_catalog.load_catalog(scope=TEST_OWNER_SCOPE).credentials


@pytest.mark.asyncio
async def test_credential_collection_session_defaults_to_fifteen_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "SECURE_TOOL_BROKER_COLLECTION_SESSION_TTL_SECONDS", raising=False
    )

    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    before = datetime.now(timezone.utc)
    async with TestClient(TestServer(service.build_app())) as client:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": TEST_TRUSTED_CONTEXT,
                "request": {
                    "credential_key": "example_login",
                    "service_name": "Example",
                    "purpose": "Collect a test credential.",
                },
            },
        )
        payload = await create.json()
    after = datetime.now(timezone.utc)

    expires_at = datetime.fromisoformat(payload["expires_at"])
    assert create.status == 200
    assert before + timedelta(minutes=15) <= expires_at <= after + timedelta(minutes=15)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "expected_status"),
    [
        (None, "credential_stored"),
        ("manual_signup", "manual_signup_collected"),
    ],
)
async def test_credential_submission_accepted_before_expiry_cannot_be_expired(
    flow: str | None,
    expected_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_started = Event()
    release_operation = Event()

    def fake_collect(request_payload, submitted_fields, *, context, scope):
        assert request_payload["credential_key"] == "smith_bank_login"
        assert submitted_fields == {
            "username": "smith-user",
            "password": "USER-SUBMITTED-SECRET",
        }
        operation_started.set()
        assert release_operation.wait(timeout=5)
        return ToolResult(
            status="success",
            content={
                "status": "credential_stored",
                "credential_key": request_payload["credential_key"],
            },
        )

    monkeypatch.setattr("secure_tool_broker.service.collect_credential", fake_collect)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    request_payload = {
        "credential_key": "smith_bank_login",
        "service_name": "Smith Bank",
        "purpose": "Store a test login.",
        "fields": [],
    }
    if flow is not None:
        request_payload["flow"] = flow

    async with TestClient(TestServer(service.build_app())) as client:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={"context": TEST_TRUSTED_CONTEXT, "request": request_payload},
        )
        create_payload = await create.json()
        session_id = create_payload["session_id"]
        form_path = create_payload["form_url"].split(
            str(client.make_url("/").origin()), 1
        )[-1]
        submit_task = asyncio.create_task(
            client.post(
                form_path,
                data={
                    "field__username": "smith-user",
                    "field__password": "USER-SUBMITTED-SECRET",
                },
            )
        )
        try:
            assert await asyncio.to_thread(operation_started.wait, 2)
            service._credential_collection_sessions[session_id].expires_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )
            during_submit = await client.get(
                f"/v1/credential-collection/sessions/{session_id}",
                headers=gateway_headers(),
            )
            during_payload = await during_submit.json()
        finally:
            release_operation.set()
        submitted = await submit_task
        completed = await client.get(
            f"/v1/credential-collection/sessions/{session_id}",
            headers=gateway_headers(),
        )
        completed_payload = await completed.json()

    assert create.status == 200
    assert during_submit.status == 200
    assert during_payload["status"] == "pending"
    assert during_payload["result"] is None
    assert submitted.status == 200
    assert completed_payload["status"] == expected_status
    assert completed_payload["used"] is True


@pytest.mark.asyncio
async def test_expired_credential_session_rejects_late_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collected = False

    def fake_collect(*args, **kwargs):
        nonlocal collected
        collected = True
        return ToolResult(status="success", content={"status": "credential_stored"})

    monkeypatch.setattr("secure_tool_broker.service.collect_credential", fake_collect)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": TEST_TRUSTED_CONTEXT,
                "request": {
                    "credential_key": "expired_login",
                    "service_name": "Expired Bank",
                    "fields": [],
                },
            },
        )
        create_payload = await create.json()
        session_id = create_payload["session_id"]
        service._credential_collection_sessions[session_id].expires_at = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)
        form_path = create_payload["form_url"].split(
            str(client.make_url("/").origin()), 1
        )[-1]
        submitted = await client.post(
            form_path,
            data={
                "field__username": "late-user",
                "field__password": "USER-SUBMITTED-SECRET",
            },
        )
        status = await client.get(
            f"/v1/credential-collection/sessions/{session_id}",
            headers=gateway_headers(),
        )
        status_payload = await status.json()

    assert submitted.status == 410
    assert collected is False
    assert status_payload["status"] == "credential_unavailable"
    assert status_payload["result"]["content"]["error_code"] == (
        "credential_collection_session_expired"
    )


@pytest.mark.asyncio
async def test_credential_deletion_session_requires_confirmation_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[dict[str, object]] = []

    def fake_delete(request_payload, *, context, scope):
        assert context.canonical_user_id == TEST_CANONICAL_USER_ID
        assert context.principal_type == "user"
        assert scope == CredentialOwnerScope.user(TEST_CANONICAL_USER_ID)
        deleted.append(dict(request_payload))
        return ToolResult(
            status="success",
            content={
                "status": "credential_deleted",
                "credential_key": request_payload["credential_key"],
                "removed_catalog_entry": True,
                "deleted_source_refs": [
                    {
                        "source": "sealbox",
                        "item_key": request_payload["source_item_id"],
                        "item_kind": "credential",
                    }
                ],
                "message": "Credential test_credential was deleted.",
            },
        )

    monkeypatch.setattr("secure_tool_broker.service.delete_credential", fake_delete)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": TEST_TRUSTED_CONTEXT,
                "request": {
                    "credential_key": "test_credential",
                    "service_name": "Test Credential",
                    "flow": "credential_deletion",
                    "source": "sealbox",
                    "source_item_id": "team-nats-agents:default:test_credential",
                    "purpose": "User requested deletion.",
                },
            },
        )
        create_payload = await create.json()
        form_path = create_payload["form_url"].split(
            str(client.make_url("/").origin()), 1
        )[-1]
        unconfirmed = await client.post(form_path, data={})
        confirmed = await client.post(form_path, data={"confirm_delete": "yes"})
        confirmed_text = await confirmed.text()
        status = await client.get(
            f"/v1/credential-collection/sessions/{create_payload['session_id']}",
            headers=gateway_headers(),
        )
        status_payload = await status.json()

    assert create.status == 200
    assert (
        create_payload["message"]
        == "Open the URL and confirm credential deletion directly with the secure broker."
    )
    assert unconfirmed.status == 400
    assert deleted == [
        {
            "credential_key": "test_credential",
            "service_name": "Test Credential",
            "flow": "credential_deletion",
            "source": "sealbox",
            "source_item_id": "team-nats-agents:default:test_credential",
            "purpose": "User requested deletion.",
            "owner_scope_kind": "user",
            "display_name": "Test Credential",
            "fields": [],
        }
    ]
    assert confirmed.status == 200
    assert "Credential deletion complete" in confirmed_text
    assert status_payload["status"] == "credential_deleted"
    assert status_payload["result"]["content"]["deleted_source_refs"][0][
        "item_key"
    ] == ("team-nats-agents:default:test_credential")


@pytest.mark.asyncio
async def test_credential_deletion_accepted_before_expiry_cannot_be_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_started = Event()
    release_operation = Event()

    def fake_delete(request_payload, *, context, scope):
        operation_started.set()
        assert release_operation.wait(timeout=5)
        return ToolResult(
            status="success",
            content={
                "status": "credential_deleted",
                "credential_key": request_payload["credential_key"],
            },
        )

    monkeypatch.setattr("secure_tool_broker.service.delete_credential", fake_delete)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": TEST_TRUSTED_CONTEXT,
                "request": {
                    "credential_key": "smith_bank_login",
                    "service_name": "Smith Bank",
                    "flow": "credential_deletion",
                    "purpose": "Delete a test login.",
                },
            },
        )
        create_payload = await create.json()
        session_id = create_payload["session_id"]
        form_path = create_payload["form_url"].split(
            str(client.make_url("/").origin()), 1
        )[-1]
        submit_task = asyncio.create_task(
            client.post(form_path, data={"confirm_delete": "yes"})
        )
        try:
            assert await asyncio.to_thread(operation_started.wait, 2)
            service._credential_collection_sessions[session_id].expires_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )
            during_submit = await client.get(
                f"/v1/credential-collection/sessions/{session_id}",
                headers=gateway_headers(),
            )
            during_payload = await during_submit.json()
        finally:
            release_operation.set()
        submitted = await submit_task
        completed = await client.get(
            f"/v1/credential-collection/sessions/{session_id}",
            headers=gateway_headers(),
        )
        completed_payload = await completed.json()

    assert during_payload["status"] == "pending"
    assert during_payload["result"] is None
    assert submitted.status == 200
    assert completed_payload["status"] == "credential_deleted"
    assert completed_payload["used"] is True


def test_resolve_secret_field_source_failover_env_file_then_credential_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_secret = tmp_path / "password.txt"
    file_secret.write_text("from-file\n", encoding="utf-8")
    catalog = CredentialCatalog(
        credentials={
            "example": CatalogCredential(
                display_name="Example",
                purpose="Testing source failover.",
                fields={
                    "password": CatalogCredentialField(
                        kind="password",
                        visibility="secret",
                        resolution_order=[
                            CatalogSourceRef(source="env", env_var="EXAMPLE_PASSWORD"),
                            CatalogSourceRef(
                                source="bitwarden",
                                item_name="missing",
                                field_ref="password",
                            ),
                            CatalogSourceRef(
                                source="vault", item_id="missing", field_ref="password"
                            ),
                            CatalogSourceRef(
                                source="onepassword",
                                item_name="fallback",
                                field_ref="password",
                            ),
                            CatalogSourceRef(
                                source="keepass",
                                item_name="unused",
                                field_ref="password",
                            ),
                        ],
                    )
                },
            )
        }
    )

    resolved = credential_catalog.resolve_secret_field(
        "example",
        "password",
        catalog=catalog,
        env={"EXAMPLE_PASSWORD_FILE": str(file_secret)}, scope=TEST_OWNER_SCOPE,
    )
    assert not isinstance(resolved, ToolResult)
    assert resolved.value == "from-file"
    assert resolved.source == "env"

    calls: list[str] = []

    class FakeSource:
        def __init__(self, provider: str) -> None:
            self.provider = provider

        def get_secret(self, request):
            calls.append(self.provider)
            if self.provider in {"bitwarden", "vault"}:
                return ToolResult(status="error", error="not found")
            return "from-onepassword"

    monkeypatch.setattr(
        credential_catalog,
        "get_credential_source",
        lambda provider: FakeSource(provider),
    )
    resolved = credential_catalog.resolve_secret_field(
        "example", "password", catalog=catalog, env={}, scope=TEST_OWNER_SCOPE
    )

    assert not isinstance(resolved, ToolResult)
    assert resolved.value == "from-onepassword"
    assert resolved.source == "onepassword"
    assert calls == ["bitwarden", "vault", "onepassword"]

    credential_catalog._SECRET_CACHE.clear()
    calls.clear()

    class KeepassFallbackSource:
        def __init__(self, provider: str) -> None:
            self.provider = provider

        def get_secret(self, request):
            calls.append(self.provider)
            if self.provider != "keepass":
                return ToolResult(status="error", error="not found")
            return "from-keepass"

    monkeypatch.setattr(
        credential_catalog,
        "get_credential_source",
        lambda provider: KeepassFallbackSource(provider),
    )
    resolved = credential_catalog.resolve_secret_field(
        "example", "password", catalog=catalog, env={}, scope=TEST_OWNER_SCOPE
    )

    assert not isinstance(resolved, ToolResult)
    assert resolved.value == "from-keepass"
    assert resolved.source == "keepass"
    assert calls == ["bitwarden", "vault", "onepassword", "keepass"]


def test_resolve_secret_field_prefers_team_env_over_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_secret = tmp_path / "team-secret.txt"
    file_secret.write_text("from-team-file\n", encoding="utf-8")
    team_env = tmp_path / "team.env"
    team_env.write_text(f"EXAMPLE_PASSWORD_FILE={file_secret}\n", encoding="utf-8")
    monkeypatch.setenv("SECURE_TOOL_BROKER_TEAM_ENV_PATH", str(team_env))
    monkeypatch.setenv("EXAMPLE_PASSWORD", "from-process-env")
    catalog = CredentialCatalog(
        credentials={
            "example": CatalogCredential(
                display_name="Example",
                purpose="Testing team env priority.",
                fields={
                    "password": CatalogCredentialField(
                        kind="password",
                        visibility="secret",
                        resolution_order=[
                            CatalogSourceRef(source="env", env_var="EXAMPLE_PASSWORD"),
                        ],
                    )
                },
            )
        }
    )

    resolved = credential_catalog.resolve_secret_field(
        "example", "password", catalog=catalog, scope=TEST_OWNER_SCOPE
    )

    assert not isinstance(resolved, ToolResult)
    assert resolved.value == "from-team-file"
    assert resolved.source == "env"
    assert (
        team_env.read_text(encoding="utf-8") == f"EXAMPLE_PASSWORD_FILE={file_secret}\n"
    )


def test_resolved_secret_cache_uses_sliding_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_catalog._SECRET_CACHE.clear()
    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "10")
    now = [100.0]
    monkeypatch.setattr(credential_catalog.time, "monotonic", lambda: now[0])

    calls = 0

    class FakeSource:
        def get_secret(self, request):
            nonlocal calls
            calls += 1
            return "CACHED-SECRET"

    monkeypatch.setattr(
        credential_catalog, "get_credential_source", lambda _provider: FakeSource()
    )
    catalog = CredentialCatalog(
        credentials={
            "example": CatalogCredential(
                display_name="Example",
                purpose="Testing secret cache.",
                fields={
                    "api_token": CatalogCredentialField(
                        kind="token",
                        visibility="secret",
                        resolution_order=[
                            CatalogSourceRef(
                                source="bitwarden",
                                item_name="item",
                                field_ref="api_token",
                            )
                        ],
                    )
                },
            )
        }
    )

    first = credential_catalog.resolve_secret_field(
        "example", "api_token", catalog=catalog, env={}, scope=TEST_OWNER_SCOPE
    )
    assert not isinstance(first, ToolResult)
    assert calls == 1

    now[0] = 105.0
    second = credential_catalog.resolve_secret_field(
        "example", "api_token", catalog=catalog, env={}, scope=TEST_OWNER_SCOPE
    )
    assert not isinstance(second, ToolResult)
    assert calls == 1

    now[0] = 112.0
    third = credential_catalog.resolve_secret_field(
        "example", "api_token", catalog=catalog, env={}, scope=TEST_OWNER_SCOPE
    )
    assert not isinstance(third, ToolResult)
    assert calls == 1

    now[0] = 123.0
    fourth = credential_catalog.resolve_secret_field(
        "example", "api_token", catalog=catalog, env={}, scope=TEST_OWNER_SCOPE
    )
    assert not isinstance(fourth, ToolResult)
    assert calls == 2


def _link(
    *injections: CredentialInjectionV1,
    owner_kind: LinkOwnerKind = LinkOwnerKind.PERSON,
    tool_id: str = LINKED_TOOL_ID,
    credential_key: str = "example_api",
) -> AccountLinkV1:
    """An account link as the broker's gate hands it to an executor."""

    return AccountLinkV1(
        link_id=f"account_link_{owner_kind.value}_{credential_key}",
        team_id=TEST_TEAM_ID,
        tool_id=tool_id,
        owner_kind=owner_kind,
        owner_id=TEST_CANONICAL_USER_ID if owner_kind is LinkOwnerKind.PERSON else TEST_TEAM_ID,
        credential_key=credential_key,
        injections=injections,
        created_by=SubjectRefV1(kind=SubjectKind.USER, id=TEST_CANONICAL_USER_ID),
        created_at_utc=LINK_CREATED_AT,
    )


def _linked_context(
    link: AccountLinkV1 | None, person_id: str = TEST_CANONICAL_USER_ID
) -> ToolContext:
    return ToolContext(
        agent_id="agent-1",
        canonical_user_id=person_id,
        principal_type="user",
        team_id=TEST_TEAM_ID,
        account_link=link,
    )


def _header(name: str, field: str = "api_token", template: str = "{value}") -> CredentialInjectionV1:
    return CredentialInjectionV1(
        placement=InjectionPlacement.HEADER, name=name, field=field, template=template
    )


def _query(name: str, field: str = "api_token") -> CredentialInjectionV1:
    return CredentialInjectionV1(placement=InjectionPlacement.QUERY, name=name, field=field)


def _env(name: str, field: str = "api_token") -> CredentialInjectionV1:
    return CredentialInjectionV1(placement=InjectionPlacement.ENV, name=name, field=field)


def _save_linked_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    scope: CredentialOwnerScope = TEST_OWNER_SCOPE,
    token_env: str = "EXAMPLE_API_TOKEN",
    token: str = "LINKED-TOKEN-SECRET",
    credential_key: str = "example_api",
) -> None:
    """Save a credential with a secret token (read from an env source) and a
    visible username, in the file-backed catalog of ``scope``."""

    monkeypatch.setenv(token_env, token)
    credential_catalog.save_catalog(
        CredentialCatalog(
            credentials={
                credential_key: CatalogCredential(
                    display_name="Example API",
                    purpose="Testing linked accounts.",
                    fields={
                        "api_token": CatalogCredentialField(
                            kind="token",
                            visibility="secret",
                            resolution_order=[CatalogSourceRef(source="env", env_var=token_env)],
                        ),
                        "username": CatalogCredentialField(
                            kind="username",
                            visibility="metadata_visible",
                            value="user@example.com",
                        ),
                    },
                )
            }
        ),
        scope=scope,
    )


class _JsonHeaders(dict):
    def get_content_charset(self):
        return "utf-8"


class _JsonResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = self.code = status
        self.headers = _JsonHeaders({"Content-Type": "application/json"})
        self._body = body

    def read(self, _limit):
        return self._body

    def getcode(self):
        return self.status

    def close(self):
        return None


def _capture_requests(monkeypatch: pytest.MonkeyPatch, body: bytes = b'{"ok": true}') -> list:
    sent: list = []

    def fake_send(request, *, timeout):
        sent.append(request)
        return _JsonResponse(body)

    monkeypatch.setattr(credential_catalog, "_send_api_request", fake_send)
    return sent


def _refuse_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        credential_catalog,
        "_send_api_request",
        lambda *_args, **_kwargs: pytest.fail("a refused request must not reach the network"),
    )


def test_api_request_adds_the_linked_header_and_redacts_the_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_linked_credential(tmp_path, monkeypatch, token="HOME-TOKEN-SECRET")
    link = _link(_header("Authorization", template="Bearer {value}"))
    sent = _capture_requests(
        monkeypatch,
        b'{"message": "API running", "echo": "HOME-TOKEN-SECRET", "seen": "Bearer HOME-TOKEN-SECRET"}',
    )

    result = credential_catalog.api_request(
        {
            "method": "GET",
            "url": "https://api.example.test/api/",
            "headers": {"Accept": "application/json"},
        },
        context=_linked_context(link),
    )

    assert result.status == "success"
    headers = {key.lower(): value for key, value in sent[0].header_items()}
    assert headers["authorization"] == "Bearer HOME-TOKEN-SECRET"
    assert headers["accept"] == "application/json"
    assert result.content["account_link_id"] == link.link_id
    assert result.content["json"] == {
        "message": "API running",
        "echo": "[REDACTED]",
        "seen": "[REDACTED]",
    }
    assert "HOME-TOKEN-SECRET" not in json.dumps(result.content, sort_keys=True)


def test_api_request_adds_the_linked_query_value_and_redacts_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_linked_credential(tmp_path, monkeypatch, token="QUERY-KEY-SECRET")
    link = _link(_query("api_key"))
    sent = _capture_requests(
        monkeypatch, b'{"url": "https://api.example.test/items?page=2&api_key=QUERY-KEY-SECRET"}'
    )

    result = credential_catalog.api_request(
        {"url": "https://api.example.test/items", "params": {"page": "2"}},
        context=_linked_context(link),
    )

    assert result.status == "success"
    assert sent[0].full_url == "https://api.example.test/items?page=2&api_key=QUERY-KEY-SECRET"
    assert result.content["json"] == {"url": "https://api.example.test/items?page=2&api_key=[REDACTED]"}
    assert "QUERY-KEY-SECRET" not in json.dumps(result.content, sort_keys=True)


def test_api_request_refuses_a_header_the_linked_account_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_linked_credential(tmp_path, monkeypatch)
    _refuse_network(monkeypatch)

    result = credential_catalog.api_request(
        {
            "url": "https://api.example.test/items",
            "headers": {"authorization": "Bearer agent-chosen-token"},
        },
        context=_linked_context(_link(_header("Authorization", template="Bearer {value}"))),
    )

    assert result.status == "error"
    assert result.content["error_code"] == "api_request_header_reserved"
    assert result.content["headers"] == ["authorization"]


@pytest.mark.parametrize(
    "request_params",
    [
        {"headers": {"Authorization": "Bearer ${HOME_ASSISTANT_TOKEN}"}},
        {"method": "POST", "json_body": {"apikey": "${PORKBUN_API_KEY}"}},
        {"params": {"key": "{{ API_KEY }}"}},
        {"url": "https://api.example.test/items?key=${API_KEY}"},
    ],
    ids=["header", "json_body", "query", "url"],
)
def test_api_request_refuses_credential_placeholders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_params: dict,
) -> None:
    _save_linked_credential(tmp_path, monkeypatch)
    _refuse_network(monkeypatch)

    result = credential_catalog.api_request(
        {"url": "https://api.example.test/items", **request_params},
        context=_linked_context(_link(_header("X-API-Key"))),
    )

    assert result.status == "error"
    assert result.content["error_code"] == "api_request_placeholders_unsupported"


@pytest.mark.parametrize("owner_kind", [LinkOwnerKind.PERSON, LinkOwnerKind.TEAM])
def test_api_request_uses_the_catalog_of_the_links_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_kind: LinkOwnerKind,
) -> None:
    # The person and the team each saved a credential under the same key; the
    # link's owner, not the person the task works for, decides which is sent.
    _save_linked_credential(
        tmp_path, monkeypatch, scope=TEST_OWNER_SCOPE, token_env="PERSON_API_TOKEN", token="PERSON-SECRET"
    )
    _save_linked_credential(
        tmp_path, monkeypatch, scope=TEST_TEAM_SCOPE, token_env="TEAM_API_TOKEN", token="TEAM-SECRET"
    )
    sent = _capture_requests(monkeypatch)

    result = credential_catalog.api_request(
        {"url": "https://api.example.test/items"},
        context=_linked_context(_link(_header("X-API-Key"), owner_kind=owner_kind)),
    )

    assert result.status == "success"
    expected = "PERSON-SECRET" if owner_kind is LinkOwnerKind.PERSON else "TEAM-SECRET"
    assert dict(sent[0].header_items())["X-api-key"] == expected


def _fake_openshell(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, str]]]:
    """Record OpenShell CLI calls (command and environment) instead of running them."""

    calls: list[tuple[list[str], dict[str, str]]] = []

    class Completed:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((list(command), dict(kwargs.get("env") or {})))
        return Completed()

    monkeypatch.setattr(credential_catalog, "_LEASES", {})
    monkeypatch.setattr(credential_catalog.shutil, "which", lambda _name: OPENSHELL)
    monkeypatch.setattr(credential_catalog.subprocess, "run", fake_run)
    return calls


def test_bind_for_openshell_gives_the_sandbox_only_the_linked_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_linked_credential(tmp_path, monkeypatch, token="OPEN-SHELL-SECRET")
    link = _link(
        _env("EXAMPLE_TOKEN"),
        _env("EXAMPLE_USERNAME", field="username"),
        _header("Authorization", template="Bearer {value}"),
    )
    calls = _fake_openshell(monkeypatch)

    result = credential_catalog.execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.bind_for_openshell",
            params={"tool_id": LINKED_TOOL_ID},
        ),
        _linked_context(link),
    )
    # The person's own sandbox, named by the broker (runtime_settings.openshell_naming).
    persons_sandbox = openshell_sandbox_name(None, TEST_CANONICAL_USER_ID)

    assert result.status == "success", result.error
    content = result.content
    assert "OPEN-SHELL-SECRET" not in json.dumps(content, sort_keys=True)
    assert content["status"] == "openshell_binding_ready"
    assert content["tool_id"] == LINKED_TOOL_ID
    assert content["account_link_id"] == link.link_id
    assert content["provider_type"] == "generic"
    assert content["sandbox_name"] == persons_sandbox
    assert content["env"] == ["EXAMPLE_TOKEN", "EXAMPLE_USERNAME"]
    assert content["provider_name"].startswith("http-example-")
    create_command, create_env = calls[0]
    assert create_command[:5] == [OPENSHELL, "provider", "create", "--name", content["provider_name"]]
    assert create_command[create_command.index("--type") + 1] == "generic"
    assert [
        create_command[index + 1] for index, arg in enumerate(create_command) if arg == "--credential"
    ] == ["EXAMPLE_TOKEN", "EXAMPLE_USERNAME"]
    assert "OPEN-SHELL-SECRET" not in " ".join(create_command)
    assert create_env["EXAMPLE_TOKEN"] == "OPEN-SHELL-SECRET"
    assert create_env["EXAMPLE_USERNAME"] == "user@example.com"
    # The header injection is for web requests; it never enters the sandbox.
    assert "Bearer OPEN-SHELL-SECRET" not in create_env.values()
    assert calls[1][0] == [OPENSHELL, "sandbox", "provider", "attach", persons_sandbox, content["provider_name"]]
    lease = credential_catalog._LEASES[content["lease_id"]]
    assert lease["tool_id"] == LINKED_TOOL_ID
    assert lease["link_id"] == link.link_id
    assert lease["person_id"] == TEST_CANONICAL_USER_ID


def test_bind_for_openshell_uses_a_requested_provider_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_linked_credential(tmp_path, monkeypatch)
    calls = _fake_openshell(monkeypatch)
    monkeypatch.setenv("OPENSHELL_SANDBOX_NAME", "default-sandbox")

    result = credential_catalog.bind_for_openshell(
        {"tool_id": LINKED_TOOL_ID, "provider_type": "custom_rest"},
        context=_linked_context(_link(_env("EXAMPLE_TOKEN"))),
    )

    assert result.status == "success", result.error
    assert result.content["provider_type"] == "custom_rest"
    assert result.content["sandbox_name"] == openshell_sandbox_name("default-sandbox", TEST_CANONICAL_USER_ID)
    assert calls[0][0][calls[0][0].index("--type") + 1] == "custom_rest"


_ENV_LINK = _link(_env("EXAMPLE_TOKEN"))


@pytest.mark.parametrize(
    ("params", "link", "error_code"),
    [
        ({}, _ENV_LINK, "openshell_tool_id_required"),
        (
            {"tool_id": LINKED_TOOL_ID, "provider_type": "Custom REST"},
            _ENV_LINK,
            "openshell_provider_type_invalid",
        ),
        # Another person's sandbox: the broker names the sandbox from the verified person.
        ({"tool_id": LINKED_TOOL_ID, "sandbox_name": "ua-p-someone-0123456789"}, _ENV_LINK, "openshell_sandbox_not_the_persons"),
        ({"tool_id": LINKED_TOOL_ID}, None, "account_link_missing"),
        ({"tool_id": "http:other"}, _ENV_LINK, "account_link_tool_mismatch"),
        (
            {"tool_id": LINKED_TOOL_ID},
            _link(_header("X-API-Key")),
            "account_link_has_no_environment",
        ),
    ],
    ids=["no_tool_id", "bad_provider_type", "another_persons_sandbox", "no_link", "other_tool", "no_env"],
)
def test_bind_for_openshell_refuses_before_touching_credentials_or_openshell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    params: dict,
    link: AccountLinkV1 | None,
    error_code: str,
) -> None:
    _save_linked_credential(tmp_path, monkeypatch)
    calls = _fake_openshell(monkeypatch)
    monkeypatch.delenv("OPENSHELL_SANDBOX_NAME", raising=False)
    monkeypatch.setattr(
        credential_catalog,
        "resolve_secret_field",
        lambda *_args, **_kwargs: pytest.fail("a refused binding must not read the credential"),
    )

    result = credential_catalog.bind_for_openshell(params, context=_linked_context(link))

    assert result.status == "error"
    assert result.content["error_code"] == error_code
    assert calls == []
    assert credential_catalog._LEASES == {}
    if error_code == "account_link_has_no_environment":
        assert result.content["next_step"] == (
            "Link the account again and name the environment variables code should see."
        )


def test_release_openshell_binding_only_for_the_person_it_was_made_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_linked_credential(tmp_path, monkeypatch)
    calls = _fake_openshell(monkeypatch)
    bound = credential_catalog.bind_for_openshell(
        {"tool_id": LINKED_TOOL_ID},
        context=_linked_context(_link(_env("EXAMPLE_TOKEN"))),
    )
    lease_id = bound.content["lease_id"]
    provider_name = bound.content["provider_name"]

    other = credential_catalog.release_openshell_binding(
        {"lease_id": lease_id}, context=_linked_context(None, person_id="someone-else")
    )
    released = credential_catalog.execute_credential_catalog_action(
        ValidatedAction(
            provider="credential_catalog",
            action="credential.release_openshell_binding",
            params={"lease_id": lease_id},
        ),
        _linked_context(None),
    )

    assert other.status == "error"
    assert other.content["error_code"] == "lease_not_found"
    assert released.status == "success", released.error
    assert released.content["status"] == "openshell_binding_released"
    assert lease_id not in credential_catalog._LEASES
    assert [command for command, _env in calls[-2:]] == [
        [OPENSHELL, "sandbox", "provider", "detach", bound.content["sandbox_name"], provider_name],
        [OPENSHELL, "provider", "delete", provider_name],
    ]


def test_a_credential_saved_with_openshell_bindings_still_loads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Credentials saved before account links carry a "bindings" key; loading
    # ignores it and the next save drops it.
    store = CredentialCatalogStore(resolve_catalog_db_path())
    store.put_credential(
        TEST_OWNER_SCOPE,
        "legacy_api",
        {
            "display_name": "Legacy API",
            "purpose": "Saved before account links.",
            "fields": {
                "api_token": {
                    "kind": "token",
                    "visibility": "secret",
                    "resolution_order": [{"source": "env", "env_var": "LEGACY_API_TOKEN"}],
                }
            },
            "bindings": {
                "openshell_legacy": {
                    "consumer": "openshell",
                    "lifecycle": "per_use",
                    "provider_type": "custom_rest",
                    "env": {"LEGACY_TOKEN": "api_token"},
                    "allowed_hosts": ["api.example.test"],
                }
            },
        },
    )

    catalog = credential_catalog.load_catalog(scope=TEST_OWNER_SCOPE)
    metadata = credential_catalog.get_credential_metadata("legacy_api", context=TEST_TOOL_CONTEXT)
    credential_catalog._save_catalog_credential(
        "legacy_api", catalog.credentials["legacy_api"], scope=TEST_OWNER_SCOPE
    )

    assert catalog.credentials["legacy_api"].fields["api_token"].is_secret()
    assert metadata.status == "success"
    assert metadata.content["credential"]["fields"]["api_token"]["secret"] is True
    assert "bindings" not in metadata.content["credential"]
    assert "bindings" not in store.load_scope(TEST_OWNER_SCOPE)["legacy_api"]


def test_secure_requests_noncredentialed_proxy_skips_catalog_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeHeaders(dict):
        def get_content_charset(self):
            return "utf-8"

    class FakeResponse:
        status = 200
        code = 200
        headers = FakeHeaders({"Content-Type": "application/json"})

        def read(self, _limit):
            return b'{"ok": true}'

        def getcode(self):
            return self.status

        def close(self):
            return None

    def fake_urlopen(request, *, timeout):
        assert request.full_url == "https://api.example.com/status?a=1&b=2"
        assert request.get_method() == "GET"
        assert timeout == 5
        return FakeResponse()

    monkeypatch.setattr(credential_catalog, "_send_api_request", fake_urlopen)

    result = credential_catalog.execute_secure_requests_action(
        ValidatedAction(
            provider="secure_requests",
            action="get",
            params={
                "url": "https://api.example.com/status",
                "params": {"a": "1", "b": "2"},
                "timeout": 5,
            },
        ),
        TEST_TOOL_CONTEXT,
    )

    assert result.status == "success"
    assert result.content["json"] == {"ok": True}
    assert "credential_key" not in result.content


def test_fixed_method_actions_refuse_another_method(tmp_path, monkeypatch):
    monkeypatch.setattr(
        credential_catalog,
        "_send_api_request",
        lambda *_args, **_kwargs: pytest.fail("a mismatched method must not be sent"),
    )

    result = credential_catalog.execute_secure_requests_action(
        ValidatedAction(
            provider="secure_requests",
            action="get",
            params={"url": "https://api.example.com/items/1", "method": "DELETE"},
        ),
        TEST_TOOL_CONTEXT,
    )

    assert result.status == "error"
    assert result.content["error_code"] == "api_request_method_mismatch"


def test_redirects_are_returned_not_followed_with_credentials():
    import http.server
    import threading

    received: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            received.append((self.path, self.headers.get("Authorization")))
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"reached")

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = credential_catalog.urllib_request.Request(
            f"http://127.0.0.1:{server.server_port}/start",
            headers={"Authorization": "Bearer secret"},
        )
        try:
            response = credential_catalog._send_api_request(request, timeout=5)
            status = response.status
        except credential_catalog.urllib_error.HTTPError as exc:
            status = exc.code
    finally:
        server.shutdown()

    assert status == 302
    assert received == [("/start", "Bearer secret")]


def test_signup_boundary_detection_returns_manual_required() -> None:
    result = credential_catalog.signup_execute_approved(
        {
            "credential_key": "example",
            "service_name": "Example",
            "observed_page_text": "Complete this reCAPTCHA challenge before continuing.",
        }
    )

    assert result.status == "success"
    assert result.content["status"] == "manual_signup_required"
    assert result.content["boundary_reason"] == "captcha"


@pytest.mark.asyncio
async def test_empty_collection_fields_fall_back_to_username_password_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_collect(request_payload, submitted_fields, *, context, scope):
        assert context.canonical_user_id == TEST_CANONICAL_USER_ID
        assert context.principal_type == "user"
        assert scope == CredentialOwnerScope.user(TEST_CANONICAL_USER_ID)
        captured["request"] = request_payload
        captured["submitted"] = submitted_fields
        return ToolResult(
            status="success",
            content={
                "status": "credential_stored",
                "credential_key": request_payload["credential_key"],
                "metadata": {
                    "credential_key": request_payload["credential_key"],
                    "fields": {
                        "username": {
                            "kind": "username",
                            "visibility": "metadata_visible",
                            "value": "ha-user",
                        },
                        "password": {
                            "kind": "password",
                            "visibility": "secret",
                            "secret": True,
                        },
                    },
                },
            },
        )

    monkeypatch.setattr("secure_tool_broker.service.collect_credential", fake_collect)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": TEST_TRUSTED_CONTEXT,
                "request": {
                    "credential_key": "home_assistant_login",
                    "service_name": "Home Assistant",
                    "purpose": "Login credential for approved Home Assistant workflows.",
                    "fields": [],
                },
            },
        )
        create_payload = await create.json()
        form_path = create_payload["form_url"].split(
            str(client.make_url("/").origin()), 1
        )[-1]
        submit = await client.post(
            form_path,
            data={
                "field__username": "ha-user",
                "field__password": "USER-SUBMITTED-SECRET",
            },
        )
        submit_text = await submit.text()

    assert create.status == 200
    assert submit.status == 200
    assert "Credential submission complete" in submit_text
    assert "Close Page" in submit_text
    assert "window.close()" in submit_text
    assert [field["name"] for field in captured["request"]["fields"]] == [
        "username",
        "password",
    ]
    assert captured["submitted"] == {
        "username": "ha-user",
        "password": "USER-SUBMITTED-SECRET",
    }


@pytest.mark.asyncio
async def test_manual_fallback_form_stores_user_submitted_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_collect(request_payload, submitted_fields, *, context, scope):
        assert context.canonical_user_id == TEST_CANONICAL_USER_ID
        assert context.principal_type == "user"
        assert scope == CredentialOwnerScope.user(TEST_CANONICAL_USER_ID)
        captured["request"] = request_payload
        captured["submitted"] = submitted_fields
        return ToolResult(
            status="success",
            content={
                "status": "credential_stored",
                "credential_key": request_payload["credential_key"],
                "metadata": {
                    "credential_key": request_payload["credential_key"],
                    "fields": {
                        "username": {
                            "kind": "username",
                            "visibility": "metadata_visible",
                            "value": "user@example.com",
                        },
                        "password": {
                            "kind": "password",
                            "visibility": "secret",
                            "secret": True,
                        },
                    },
                },
            },
        )

    monkeypatch.setattr("secure_tool_broker.service.collect_credential", fake_collect)
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": TEST_TRUSTED_CONTEXT,
                "request": {
                    "credential_key": "example_signup",
                    "service_name": "Example",
                    "flow": "manual_signup",
                    "signup_url": "https://example.com/signup?source=ua&mode=manual",
                    "username": "user@example.com",
                    "password_field_name": "password",
                    "purpose": "Manual fallback signup test.",
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
            form_path,
            data={
                "field__username": "user@example.com",
                "field__password": "USER-SUBMITTED-SECRET",
            },
        )
        submit_text = await submit.text()
        status = await client.get(
            f"/v1/credential-collection/sessions/{create_payload['session_id']}",
            headers=gateway_headers(),
        )
        status_payload = await status.json()

    assert create.status == 200
    assert create.headers["Cache-Control"] == "no-store"
    assert form.status == 200
    assert form.headers["Cache-Control"] == "no-store"
    assert 'href="https://example.com/signup?source=ua&amp;mode=manual"' in form_text
    assert 'target="_blank" rel="noopener noreferrer"' in form_text
    assert "Open Example signup on example.com" in form_text
    assert submit.status == 200
    assert submit.headers["Cache-Control"] == "no-store"
    assert "Close Page" in submit_text
    assert "window.close()" in submit_text
    assert captured["submitted"] == {
        "username": "user@example.com",
        "password": "USER-SUBMITTED-SECRET",
    }
    assert status_payload["status"] == "manual_signup_collected"
    assert status.headers["Cache-Control"] == "no-store"
    assert "USER-SUBMITTED-SECRET" not in json.dumps(status_payload, sort_keys=True)


@pytest.mark.parametrize(
    "signup_url",
    [
        "javascript:alert(1)",
        "https://user:password@example.com/signup",
        "https://example.com/signup\nmalformed",
        "//example.com/signup",
    ],
)
def test_manual_signup_form_rejects_unsafe_signup_urls(signup_url: str) -> None:
    assert (
        _render_manual_signup_action(
            {"flow": "manual_signup", "signup_url": signup_url},
            service_name="Example",
        )
        == ""
    )


@pytest.mark.asyncio
async def test_cancelled_form_request_keeps_claim_until_worker_exits() -> None:
    operation_started = Event()
    release_operation = Event()
    session = CredentialCollectionSession(
        request_payload={},
        tool_context=TEST_TOOL_CONTEXT,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        submission_in_progress=True,
    )

    def blocked_operation() -> None:
        operation_started.set()
        assert release_operation.wait(timeout=5)

    submission = asyncio.create_task(
        _run_claimed_form_thread(session, blocked_operation)
    )
    assert await asyncio.to_thread(operation_started.wait, 2)
    submission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submission

    assert session.submission_in_progress is True
    release_operation.set()
    for _ in range(100):
        if not session.submission_in_progress:
            break
        await asyncio.sleep(0.01)
    assert session.submission_in_progress is False


def test_secure_credential_messages_round_trip_and_route_as_responses() -> None:
    request = SecureCredentialCollectionRequest(
        parent_task_id="task-1",
        service_name="Example",
        signup_url="https://example.com/signup",
        credential_key="example_login",
        purpose="Approved workflow credential.",
        fields=[
            SecureCredentialFieldSpec(
                name="username",
                kind="username",
                visibility="metadata_visible",
                value_source="agent_supplied_metadata",
                value="user@example.com",
            )
        ],
    )
    envelope = MessageEnvelope(
        msg_content=request,
        msg_meta=MessageMeta(msg_correlation_id="corr-1"),
        msg_header=MessageHeader(
            from_agent_id="agent-1", response_routing_key="gateway"
        ),
    )

    restored = MessageEnvelope.model_validate_json(envelope.model_dump_json())
    assert isinstance(restored.msg_content, SecureCredentialCollectionRequest)
    assert restored.msg_content.fields[0].value == "user@example.com"
    structured = StructuredLLMResponse(
        selected_message_type="SecureCredentialCollectionRequest",
        secure_credential_collection_request=request,
    )
    assert structured.to_comms_message().msg_content.credential_key == "example_login"

    result = SecureCredentialCollectionResult(
        credential_request_id=request.credential_request_id,
        status="credential_stored",
        credential_key="example_login",
        service_name="Example",
        parent_task_id="task-1",
        message="Stored.",
    )
    assert is_response_content(result)


@pytest.mark.live
@pytest.mark.external
@pytest.mark.skipif(
    not os.getenv("LIVE_BITWARDEN_OPENSHELL_TEST"),
    reason="Optional live Bitwarden/OpenShell credential catalog test is opt-in.",
)
def test_live_bitwarden_openshell_binding_opt_in() -> None:
    completed = subprocess.run(
        ["bw", "status"], capture_output=True, text=True, timeout=15, check=False
    )
    assert completed.returncode == 0
    assert "unlocked" in completed.stdout.lower()

import io
import json
from pathlib import Path

import pytest
import yaml
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from secure_tool_broker import credential_catalog
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.credential_templates import (
    load_credential_templates,
    list_credential_templates,
    render_credential_template_request,
    save_approved_template_variant,
)
from secure_tool_broker.service import SecureToolBrokerService
from tests.broker_access_support import BROKER_ROOT, gateway_headers, open_gate
from secure_tools.providers import ToolContext, ToolResult


TEST_CANONICAL_USER_ID = "credential-template-test-user"
TEST_TRUSTED_CONTEXT = {
    "agent_id": "agent-1",
    "canonical_user_id": TEST_CANONICAL_USER_ID,
    "principal_type": "user",
}


def test_builtin_templates_include_twitter_and_render_existing_request() -> None:
    templates = load_credential_templates()

    assert "x_twitter_login" in templates
    assert "x_twitter_api_bearer_token" in templates
    assert [item["template_id"] for item in list_credential_templates({"query": "x"}).content["templates"]] == [
        "x_twitter_api_bearer_token",
        "x_twitter_login",
    ]
    assert [item["template_id"] for item in list_credential_templates({"query": "update credential"}).content["templates"]] == [
        "generic_username_password",
    ]
    assert [item["template_id"] for item in list_credential_templates({"query": "home assistant"}).content["templates"]] == [
        "home_assistant_api_token",
        "home_assistant_login",
    ]
    assert [item["template_id"] for item in list_credential_templates({"query": "home assistant token"}).content["templates"]] == [
        "home_assistant_api_token",
    ]

    result = render_credential_template_request(
        {
            "template_id": "x_twitter_api_bearer_token",
            "service_name": "X/Twitter",
            "save_template_variant_on_approval": True,
        }
    )

    assert result.status == "success"
    request = result.content["request"]
    assert request["credential_key"] == "x_twitter_api_token"
    assert request["template_reference"]["template_id"] == "x_twitter_api_bearer_token"
    assert request["template_variant"]["base_template_id"] == "x_twitter_api_bearer_token"
    # Templates describe the credential only; account links say where it goes.
    assert "bindings" not in request
    assert "SECRET" not in json.dumps(request)


def test_home_assistant_template_renders_login_request() -> None:
    result = render_credential_template_request(
        {
            "template_id": "home_assistant_login",
            "service_name": "Home Assistant",
            "domain": "https://home.example.test",
            "desired_username": "test_guest",
        }
    )

    assert result.status == "success"
    request = result.content["request"]
    assert request["credential_key"] == "home_assistant_login"
    assert request["fields"][0]["value"] == "test_guest"
    assert [field["name"] for field in request["fields"]] == ["username", "service_password"]
    assert "bindings" not in request
    assert "SECRET" not in json.dumps(request)


def test_home_assistant_api_token_template_renders_token_request() -> None:
    result = render_credential_template_request(
        {
            "template_id": "home_assistant_api_token",
            "service_name": "Home Assistant",
            "domain": "https://home.example.test",
        }
    )

    assert result.status == "success"
    request = result.content["request"]
    assert request["credential_key"] == "home_assistant_token"
    assert [field["name"] for field in request["fields"]] == ["api_token"]
    assert "bindings" not in request
    assert "SECRET" not in json.dumps(request)


def test_github_pat_template_renders_api_token_request() -> None:
    templates = load_credential_templates()

    assert "github_pat" in templates
    assert [item["template_id"] for item in list_credential_templates({"query": "github"}).content["templates"]] == [
        "github_pat",
    ]

    result = render_credential_template_request(
        {
            "template_id": "github_pat",
            "service_name": "GitHub",
        }
    )

    assert result.status == "success"
    request = result.content["request"]
    assert request["service_name"] == "GitHub"
    assert request["credential_key"] == "github_pat"
    assert request["purpose"] == "GitHub API and CLI access for approved workflows."
    assert [field["name"] for field in request["fields"]] == ["api_token"]
    assert request["fields"][0]["label"] == "GitHub personal access token"
    assert "bindings" not in request
    assert "SECRET" not in json.dumps(request)


def test_template_render_request_requires_template_id_with_generic_guidance() -> None:
    result = render_credential_template_request({"service_name": "Home Assistant"})

    assert result.status == "error"
    assert result.content["error_code"] == "template_id_required"
    assert result.content["form_creation_action"] == "secure_credential_collection_request"
    assert "template.render_request requires template_id" in result.error
    assert "generic_username_password" in result.content["suggested_template_ids"]


def test_example_template_overrides_file_loads() -> None:
    example_path = (
        Path(__file__).resolve().parents[1]
        / "ua_definitions"
        / "credential_templates"
        / "credential_template_overrides.example.yaml"
    )

    templates = load_credential_templates(overrides_path=example_path)

    loaded = templates["approved_x_twitter_posting_login_example"]
    assert loaded.source == "approved_variant"
    assert loaded.template.base_template_id == "x_twitter_login"
    assert [field.name for field in loaded.template.fields] == ["username", "service_password"]
    assert "bindings" not in loaded.template.model_dump(mode="json")
    assert "SECRET" not in example_path.read_text(encoding="utf-8")


def test_template_rejects_secret_values_in_approved_variants(tmp_path: Path) -> None:
    overrides_path = tmp_path / "credential_template_overrides.yaml"
    overrides_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "templates": {
                    "bad_secret_template": {
                        "template_id": "bad_secret_template",
                        "display_name": "Bad",
                        "credential_kind": "api_token",
                        "fields": [
                            {
                                "name": "api_token",
                                "kind": "token",
                                "visibility": "secret",
                                "value": "SHOULD-NOT-BE-HERE",
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Secret field"):
        load_credential_templates(overrides_path=overrides_path)


def test_agent_augmentation_can_adjust_metadata_but_not_secret_values() -> None:
    ok = render_credential_template_request(
        {
            "template_id": "generic_api_token",
            "service_name": "Example API",
            "augmentations": {
                "purpose": "Example-specific token.",
                "fields": {
                    "api_token": {
                        "label": "Example API token",
                        "description": "Paste the generated token.",
                    }
                },
            },
        }
    )

    assert ok.status == "success"
    assert ok.content["request"]["fields"][0]["label"] == "Example API token"
    assert ok.content["request"]["purpose"] == "Example-specific token."

    rejected = render_credential_template_request(
        {
            "template_id": "generic_api_token",
            "service_name": "Example API",
            "augmentations": {
                "fields": {
                    "api_token": {
                        "value": "SECRET-VALUE",
                    }
                }
            },
        }
    )

    assert rejected.status == "error"
    assert "secret value" in rejected.error.lower()


def test_approved_template_variant_saved_only_after_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overrides_path = tmp_path / "credential_template_overrides.yaml"
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH", str(overrides_path))
    rendered = render_credential_template_request(
        {
            "template_id": "generic_api_token",
            "service_name": "Example API",
            "augmentations": {"purpose": "Example-specific approved token."},
            "save_template_variant_on_approval": True,
        }
    )
    variant = rendered.content["template_variant"]

    unapproved = save_approved_template_variant({"template_variant": variant, "user_approved": False})
    assert unapproved.status == "error"
    assert not overrides_path.exists()

    approved = save_approved_template_variant({"template_variant": variant, "user_approved": True})
    assert approved.status == "success"
    saved = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    assert approved.content["template_id"] in saved["templates"]
    assert "SECRET" not in overrides_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_broker_session_saves_approved_template_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overrides_path = tmp_path / "credential_template_overrides.yaml"
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH", str(overrides_path))
    rendered = render_credential_template_request(
        {
            "template_id": "generic_api_token",
            "service_name": "Example API",
            "augmentations": {"purpose": "Example-specific approved token."},
            "save_template_variant_on_approval": True,
            "user_approved": True,
        }
    )

    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    async with TestClient(TestServer(service.build_app())) as client:
        create = await client.post(
            "/v1/credential-collection/sessions",
            headers=gateway_headers(),
            json={
                "context": TEST_TRUSTED_CONTEXT,
                "request": rendered.content["request"],
            },
        )

    assert create.status == 200
    saved = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    assert rendered.content["template_variant"]["template_id"] in saved["templates"]


OWNER = CredentialOwnerScope.user("person-1")


def test_register_existing_env_source_stores_only_source_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "credential_catalog.db"))
    secret_file = tmp_path / "twitter-token.txt"
    secret_file.write_text("TWITTER-SECRET\n", encoding="utf-8")
    monkeypatch.setenv("TWITTER_BEARER_TOKEN_FILE", str(secret_file))

    result = credential_catalog.register_existing_source(
        {
            "credential_key": "x_twitter_api_token",
            "display_name": "X/Twitter API Token",
            "purpose": "Bootstrap env token registration.",
            "fields": [
                {
                    "name": "api_token",
                    "kind": "token",
                    "visibility": "secret",
                    "source_ref": {
                        "source": "env",
                        "env_var": "TWITTER_BEARER_TOKEN",
                        "file_fallback": True,
                    },
                }
            ],
        },
        scope=OWNER,
    )

    assert result.status == "success"
    assert result.content["status"] == "credential_stored"
    assert "bindings" not in result.content["metadata"]
    catalog = credential_catalog.load_catalog(scope=OWNER)
    source_ref = catalog.credentials["x_twitter_api_token"].fields["api_token"].resolution_order[0]
    assert source_ref.env_var == "TWITTER_BEARER_TOKEN"
    assert "TWITTER-SECRET" not in json.dumps(catalog.model_dump(mode="json"), sort_keys=True)


def test_register_existing_env_source_missing_returns_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "credential_catalog.db"))

    result = credential_catalog.register_existing_source(
        {
            "credential_key": "missing_token",
            "fields": [
                {
                    "name": "api_token",
                    "kind": "token",
                    "visibility": "secret",
                    "source_ref": {"source": "env", "env_var": "MISSING_TOKEN", "file_fallback": True},
                }
            ],
        },
        scope=OWNER,
    )

    assert result.status == "error"
    assert result.content["status"] == "credential_unavailable"


@pytest.mark.asyncio
async def test_file_upload_credential_form_stores_content_without_agent_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_collect(
        request_payload,
        submitted_fields,
        *,
        context: ToolContext,
        scope,
    ):
        assert context.canonical_user_id == TEST_CANONICAL_USER_ID
        assert context.principal_type == "user"
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
                        "service_account_json": {
                            "kind": "credential_file",
                            "visibility": "secret",
                            "secret": True,
                        }
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
                    "credential_key": "example_service_account",
                    "service_name": "Example",
                    "purpose": "File upload test.",
                    "fields": [
                        {
                            "name": "service_account_json",
                            "kind": "credential_file",
                            "visibility": "secret",
                            "value_source": "user_input",
                            "form": {"input_control": "file", "accept": ["application/json"]},
                        }
                    ],
                }
            },
        )
        create_payload = await create.json()
        form_path = create_payload["form_url"].split(str(client.make_url("/").origin()), 1)[-1]
        form = FormData()
        form.add_field(
            "field__service_account_json",
            io.BytesIO(b'{"private_key":"SECRET-FILE-CONTENT"}'),
            filename="service-account.json",
            content_type="application/json",
        )
        submit = await client.post(form_path, data=form)
        status = await client.get(
            f"/v1/credential-collection/sessions/{create_payload['session_id']}",
            headers=gateway_headers(),
        )
        status_payload = await status.json()

    assert create.status == 200
    assert submit.status == 200
    assert captured["submitted"] == {"service_account_json": '{"private_key":"SECRET-FILE-CONTENT"}'}
    assert "SECRET-FILE-CONTENT" not in json.dumps(status_payload, sort_keys=True)

"""``/auth model`` in the gateway, and the shared endpoint and redaction rules.

Covered: the command opens the broker page with the configured endpoints
and the person's roles (the broker decides); ``/auth status`` lists what
the broker knows about model keys, including environment overrides and
rejections, without a key; the configured endpoints come from
``llm.litellm_api_base``, ``LLM_OVERRIDES.yaml`` and ``embedding.api_base``;
endpoint addresses are normalized and refused when unsafe; model provider
keys are redacted from broker logs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant_gateway.application.commands.authentication import (
    AuthenticationCommandHandler,
    AuthenticationCommandRequest,
)
from assistant_gateway.application.commands.context import GatewayActorContext
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.model_key_endpoints import configured_model_endpoints
from custom_messages.contracts.model_keys_v1 import ConnectionPurposeV1, ModelKeyEndpointUseV1
from model_providers.endpoints import EndpointKind, InvalidEndpointAddress, display_host, endpoint_address
from secure_tool_broker.redaction import redact_text

ACTOR = GatewayActorContext(channel_name="website", user_id="u", chat_id="c", canonical_user_id="usr_alice")


class _Identity:
    def __init__(self, roles: tuple[str, ...]) -> None:
        self.roles = roles

    def canonical_user_id_for_context(self, **_context: str | None) -> str:
        return "usr_alice"

    def team_roles_for_person(self, _person_id: str) -> tuple[str, ...]:
        return self.roles


class _Broker:
    auth_unavailable_message = None

    def __init__(self, status: dict | None = None) -> None:
        self.requests: list[dict] = []
        self.status = status or {"endpoints": []}

    def user_form_url(self, internal_url: str) -> str:
        return internal_url

    async def create_auth_session(self, **request: object) -> dict[str, object]:
        self.requests.append(request)
        return {"method": "team", "login_url": "https://broker.internal/auth/model/v1.signed", "expires_at": ""}

    async def get_auth_status(self, **_request: object) -> dict[str, object]:
        return {"providers": {}}

    async def get_model_key_status(self) -> dict:
        return self.status


USE = ModelKeyEndpointUseV1(
    base_url="https://bifrost.example.com/v1", kind=EndpointKind.OPENAI_COMPATIBLE, model_id="openai/gpt-6-luna"
)


@pytest.mark.asyncio
async def test_auth_model_opens_the_broker_page_with_the_configured_endpoints_and_roles() -> None:
    broker = _Broker()
    handler = AuthenticationCommandHandler(
        identity=_Identity(("admin",)), secure_broker=broker, team_id="team_ua", model_endpoints=lambda: (USE,)  # type: ignore[arg-type]
    )

    result = await handler.execute(AuthenticationCommandRequest(command="/auth model", actor=ACTOR))

    request = broker.requests[0]
    assert request["provider"] == "model" and request["method"] == "team"
    assert request["context"]["person_roles"] == ["admin"]
    assert [json.loads(item) for item in request["options"]["endpoints"]] == [USE.model_dump(mode="json")]
    assert "test and save the team's model provider keys" in result.description  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_auth_status_lists_model_keys_overrides_and_rejections_without_a_key() -> None:
    status = {
        "endpoints": [
            {
                "address": "https://bifrost.example.com",
                "kind": "openai_compatible",
                "state": "stored",
                "sources": [
                    {"caller": "manager", "source": "environment", "env_var": "LITELLM_API_KEY", "reported_at": "2026-10-01T10:00:00Z"}
                ],
                "last_rejection": {
                    "caller": "manager", "source": "environment", "env_var": "LITELLM_API_KEY",
                    "status_code": 401, "rejected_at": "2026-10-01T10:05:00Z",
                },
            }
        ]
    }
    handler = AuthenticationCommandHandler(identity=_Identity(("member",)), secure_broker=_Broker(status), team_id="team_ua")  # type: ignore[arg-type]

    result = await handler.execute(AuthenticationCommandRequest(command="/auth status", actor=ACTOR))

    assert "bifrost.example.com: key stored; overridden by LITELLM_API_KEY in the environment" in result
    assert "the provider rejected LITELLM_API_KEY at 2026-10-01 10:05 UTC" in result
    assert "/auth model" in result


def test_configured_endpoints_come_from_the_model_endpoint_overrides_and_embeddings(tmp_path: Path) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="model-keys", instance_root=str(tmp_path / "instance"), team_id="team_ua", timezone="UTC"
    )
    config.llm.provider, config.llm.model_id = "openai", "gpt-5"
    config.llm.litellm_api_base = "https://bifrost.example.com/v1"
    config.embedding.provider, config.embedding.model_id = "openai_compatible", "embed-1"
    config.embedding.api_base = "https://emb.example.com/v1"
    config.layout.llm_overrides_path.parent.mkdir(parents=True, exist_ok=True)
    config.layout.llm_overrides_path.write_text(
        "agent_types:\n  TeamArchitect:\n    provider: litellm\n    model_id: openai/gpt-6-luna\n    litellm_api_mode: responses\n",
        encoding="utf-8",
    )

    uses = configured_model_endpoints(config)

    assert [(use.address, use.purpose, use.model_id, use.api_mode) for use in uses] == [
        ("https://api.openai.com", ConnectionPurposeV1.CHAT, "gpt-5", "chat_completions"),
        ("https://bifrost.example.com", ConnectionPurposeV1.CHAT, "openai/gpt-6-luna", "responses"),
        ("https://emb.example.com", ConnectionPurposeV1.EMBEDDING, "embed-1", "chat_completions"),
    ]
    assert uses[0].kind is EndpointKind.OPENAI and uses[0].label == "OpenAI"


@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("HTTPS://Bifrost.Example.com:443/v1/", "https://bifrost.example.com"),
        ("http://litellm:4000/v1", "http://litellm:4000"),
        ("https://[::1]:8443", "https://[::1]:8443"),
    ],
)
def test_endpoint_addresses_keep_only_scheme_host_and_port(url: str, address: str) -> None:
    assert endpoint_address(url) == address


@pytest.mark.parametrize(
    "url", ["", "bifrost.example.com", "ftp://x", "https://u:p@x.example.com", "https://x.example.com/v1?key=1", "https://x/#a"]
)
def test_unsafe_or_incomplete_addresses_are_refused(url: str) -> None:
    with pytest.raises(InvalidEndpointAddress):
        endpoint_address(url)


def test_display_host_drops_the_scheme() -> None:
    assert display_host("https://bifrost.example.com:8443") == "bifrost.example.com:8443"


@pytest.mark.parametrize(
    "key",
    ["sk-proj-abcdefghijklmnop1234", "sk-ant-api03-abcdefghijklmnop", "sk-or-v1-abcdefghijklmnopqrst", "xai-abcdefghijklmnopqrst", "AIzaSyAbcdefghijklmnopqrstuvwxyz0123456"],
)
def test_model_provider_keys_are_redacted_from_broker_logs(key: str) -> None:
    assert key not in redact_text(f"provider answered for {key} with 401")


def test_ordinary_words_are_not_redacted() -> None:
    text = "task-abcdefghijklmnopqrstuvwxyz and risk-assessment-for-everything"
    assert redact_text(text) == text

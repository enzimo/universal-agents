from __future__ import annotations

import asyncio
import io
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import sentinel
from urllib.request import urlopen

from aiohttp.test_utils import TestServer
import pytest
import yaml

from assistant_cli import (
    CliMessageTracker,
    GatewayOnboardingSeed,
    InteractiveCliConsole,
    _collect_onboarding_answers,
    _handle_local_session_command as _handle_application_session_command,
    _interactive_app_list,
    _load_cli_env_file,
    _load_gateway_instance_config,
    _merge_photon_onboarding_env,
    _onboard,
    _parse_reply_command,
    _resolve_instance_config_path,
    _run_gateway_services,
    _run_secure_tool_broker_service,
    _resolve_onboarding_seed,
    build_parser,
)
from assistant_gateway.onboarding_engine import (
    OnboardingTemplate,
    load_base_onboarding_template,
)
from runtime_settings.agent_types import AgentTypeRuntimeSettings
from runtime_settings.governance import REMOVED_TOOL_POLICY_KEYS
from runtime_settings.legacy_layout import present_removed_location_settings
from runtime_settings.telemetry import ActivityCollectorSettings
from assistant_gateway.config import DeliverySettings, GatewayInstanceConfig
from runtime_settings.voice import VoiceSettings
from assistant_gateway.application.interactive_commands import (
    GatewayCommandRequest,
    GatewayCommandService,
    GatewayCommandSettings,
)
from assistant_gateway.application.account_links import GatewayAccountLinks
from assistant_gateway.application.broker_sign_in import GatewayBrokerSignIns
from assistant_gateway.application.commands import (
    AccessCommandHandler,
    AccountSharingCommandHandler,
    AccountLinkCommandHandler,
    AuthenticationCommandHandler,
    IdentityCommandHandler,
    MemoryTopicCommandHandler,
)
from assistant_gateway.application.manager_control_command_service import (
    GatewayManagerControlCommandService,
)
from assistant_gateway.application.message_submission_command_service import (
    GatewayMessageSubmissionCommandService,
)
from assistant_gateway.command_composition import build_gateway_command_application
from assistant_gateway.http_secure_broker import HttpGatewaySecureBroker
from assistant_gateway.application.session_command_service import (
    GatewaySessionCommandService,
)
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.runtime_ports import SystemGatewayClock
from secure_tool_broker.auth_pages import BROKER_APPEARANCE_ENV
from secure_tool_broker.mcp_config import BrokerMCPConfig
from secure_tool_broker.service import SecureToolBrokerService
from access_control.caller_tokens import CALLER_GATEWAY
from secure_tool_broker.access_gate import BrokerAccessGate
from tests.broker_access_support import BROKER_ROOT, caller_token, open_gate
from tests.application_test_doubles import RecordingLinkResults, application_from


class FakeGateway:
    def __init__(self, state_store: GatewayStateStore):
        self.state_store = state_store
        self.session_repository = state_store.session_repository
        self.request_repository = state_store.request_repository
        self.started_sessions: list[str] = []
        self.sent_user_messages: list[dict] = []
        self.config = SimpleNamespace(
            internal_website_base_url="http://127.0.0.1:18500",
            network=SimpleNamespace(public_origin="http://127.0.0.1"),
            interactive_apps=SimpleNamespace(
                internal_website=SimpleNamespace(enabled=True, public_port=18500),
            ),
        )

    @property
    def command_application(self) -> GatewayCommandService:
        if isinstance(self.config, GatewayInstanceConfig):
            return build_gateway_command_application(
                config=self.config,
                operations=self,
                session_repository=self.session_repository,
                request_repository=self.request_repository,
                private_authorizer=_AllowPrivateAccess(),
                operational_controls=self,
                link_results=RecordingLinkResults(),
            )
        secure_broker = HttpGatewaySecureBroker(
            internal_base_url=None,
            public_base_url=None,
            token=None,
        )
        return GatewayCommandService(
            settings=GatewayCommandSettings(
                docs_base_url=(
                    os.getenv("INTERNAL_WEBSITE_BASE_URL", "").strip()
                    or self.config.internal_website_base_url
                ).rstrip("/"),
            ),
            authentication_commands=AuthenticationCommandHandler(
                identity=self,
                secure_broker=secure_broker,
                team_id="",
            ),
            account_link_commands=AccountLinkCommandHandler(
                identity=self,
                account_links=GatewayAccountLinks(
                    broker=secure_broker,
                    team_id="",
                    clock=SystemGatewayClock(),
                ),
                sign_ins=GatewayBrokerSignIns(secure_broker=secure_broker, team_id=""),
                link_results=RecordingLinkResults(),
            ),
            account_sharing_commands=AccountSharingCommandHandler(identity=self, sharing=_NoAccountSharing()),
            access_commands=AccessCommandHandler(identity=self, access=_AccessControlOff()),
            identity_commands=IdentityCommandHandler(self),
            memory_topic_commands=MemoryTopicCommandHandler(self),
            message_submission_commands=GatewayMessageSubmissionCommandService(self),
            manager_control_commands=GatewayManagerControlCommandService(self),
            session_commands=GatewaySessionCommandService(
                sessions=self.session_repository,
                requests=self.request_repository,
                actors=self,
                lifecycle=self,
                authorizer=_AllowPrivateAccess(),
                team_id="",
            ),
        )

    def canonical_user_id_for_context(self, **_context) -> str | None:
        user_config = getattr(self.config, "user", None)
        return str(getattr(user_config, "canonical_user_id", "") or "") or None

    def gateway_users_enabled(self) -> bool:
        users = getattr(getattr(self.config, "gateway", None), "users", None)
        return bool(getattr(users, "enabled", False))

    def resolve_session(self, *, session_key=None, **context):
        if session_key:
            session = self.session_repository.get_session(session_key)
            return session or SimpleNamespace(session_key=session_key)
        return self.start_new_session(**context)

    def requester_timezone_for_context(self, **_context) -> str:
        return "UTC"

    def start_new_session(
        self,
        *,
        channel_name=None,
        user_id=None,
        chat_id=None,
        thread_id=None,
        canonical_user_id=None,
    ):
        session = self.state_store.create_session(
            session_key="session_new",
            conversation_id="conversation_id_new",
            manager_agent_id="manager_1",
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            owner_canonical_user_id=canonical_user_id,
        )
        self.started_sessions.append(session.session_key)
        return session

    async def send_user_message(self, text: str, **kwargs):
        self.sent_user_messages.append({"text": text, **kwargs})
        return SimpleNamespace(
            text="Tour started.",
            conversation_id=kwargs.get("conversation_id") or "conversation_id_tour",
            correlation_id="corr_tour",
            task_id="task_tour",
            attachments=(),
        )

    def switch_session_conversation(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: str | None,
    ):
        self.session_repository.upsert_session(
            session_key=session_key,
            conversation_id=conversation_id,
            last_task_id=last_task_id,
            manager_agent_id=None,
        )
        return self.session_repository.get_session(session_key)


class _AllowPrivateAccess:
    def can_access(self, **_request: object) -> bool:
        return True


class _AccessControlOff:
    """The /access port with authorization off: only the help answers."""

    def authorization_enabled(self) -> bool:
        return False


class _NoAccountSharing:
    """The /accounts port; these session tests never run /accounts."""

    def overview(self, *, person_id: str, roles: tuple[str, ...]) -> str:
        raise AssertionError("/accounts is not part of these tests")

    def change(self, *, subject: str, shared: bool, person_id: str, roles: tuple[str, ...]) -> str:
        raise AssertionError("/accounts is not part of these tests")

    def setup(self, *, person_id: str, roles: tuple[str, ...]) -> str:
        raise AssertionError("/setup is not part of these tests")


def _assert_owned_broker_payload(
    payload: dict, gateway: FakeGateway, **expected: object
) -> None:
    assert payload.get("context") == {
        "canonical_user_id": gateway.config.user.canonical_user_id,
        "team_id": gateway.config.manager.team_id,
        "principal_type": "user",
    }
    assert {
        key: value for key, value in payload.items() if key != "context"
    } == expected


async def handle_interactive_session_command(*, gateway, **request):
    result = await gateway.command_application.execute(GatewayCommandRequest(**request))
    return result.handled, result.session_key, result.text


async def _handle_local_session_command(*, gateway=None, application=None, **request):
    """Keep legacy-shaped CLI fixtures behind the typed application boundary."""

    resolved_application = application
    if resolved_application is None:
        resolved_application = application_from(
            gateway,
            command_application=gateway.command_application,
        )
    return await _handle_application_session_command(
        application=resolved_application,
        **request,
    )


class _FakeTtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def _clear_onboarding_runtime_env(monkeypatch):
    for key in (
        "POSTGRES_DSN",
        "POSTGRES_DSN_FILE",
        "NATS_URL",
        "NATS_SERVERS",
        "NATS_USER",
        "NATS_USER_FILE",
        "NATS_PASSWORD",
        "NATS_PASSWORD_FILE",
        "NATS_CREDENTIALS_FILE",
        "LIVE_TEST_POSTGRES_DSN",
        "TASK_HISTORY_DB_PATH",
        "PYTHON_ENVS_DIR",
        "SQLITE_DB_PATH",
        "STORAGE_BACKEND",
        "SECURITY_LAYER_MODE",
        "SECURE_CLI_ENABLED",
        "LLM_STREAM",
        "LLM_REQUEST_TIMEOUT_SECONDS",
        "NETWORK_PUBLIC_ORIGIN",
        "SECURE_TOOL_BROKER_INTERNAL_BASE_URL",
        "SECURE_TOOL_BROKER_PUBLIC_BASE_URL",
        "SECURE_TOOL_BROKER_PUBLIC_PORT",
        "SECURE_TOOL_BROKER_HOST",
        "SECURE_TOOL_BROKER_PORT",
        "SECURE_TOOL_BROKER_CREDENTIALS_DIR",
        "SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH",
        "USER_DOCS_BIND_HOST",
        "USER_DOCS_BIND_PORT",
        "USER_DOCS_BASE_URL",
        "USER_DOCS_ROOT",
        "USER_DOCS_SKILLS_DIR",
        "WEBSITE_APP_ENABLED",
        "WEBSITE_APP_BIND_HOST",
        "WEBSITE_APP_BIND_PORT",
        "WEBSITE_APP_BASE_URL",
        "WEBSITE_APP_SITES_DIR",
        "INTERNAL_WEBSITE_ENABLED",
        "INTERNAL_WEBSITE_BIND_HOST",
        "INTERNAL_WEBSITE_BIND_PORT",
        "INTERNAL_WEBSITE_PUBLIC_PORT",
        "INTERNAL_WEBSITE_DOCS_ROOT",
        "INTERNAL_WEBSITE_SKILLS_DIR",
        "INTERNAL_WEBSITE_SITES_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.asyncio
async def test_handle_local_session_command_lists_previews_searches_and_switches(
    tmp_path,
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    for idx in range(1, 10):
        session_key = f"session_{idx}"
        conversation_id = f"conv_{idx}"
        store.create_session(
            session_key=session_key,
            session_alias="general" if idx == 1 else None,
            conversation_id=conversation_id,
            manager_agent_id="manager_1",
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_1",
        )
        store.record_request(
            correlation_id=f"corr_{idx}",
            session_key=session_key,
            request_kind="interactive",
            request_text=f"Request text {idx}",
            conversation_id=conversation_id,
            followup_to_task_id=None,
        )
    gateway = FakeGateway(store)

    handled, current, message = await _handle_local_session_command(
        raw_text="/list_sessions",
        gateway=gateway,
        current_session_key="session_9",
    )
    assert handled is True
    assert current == "session_9"
    assert "Sessions 1-7 of 9:" in message
    assert "1. session_9 *" in message
    assert "7. session_3" in message
    assert "preview=Request text 9" in message
    assert "Use /list_sessions 8-9 for the next page." in message

    handled, current, message = await _handle_local_session_command(
        raw_text="/list_sessions 4-8",
        gateway=gateway,
        current_session_key="session_9",
    )
    assert handled is True
    assert current == "session_9"
    assert "Sessions 4-8 of 9:" in message
    assert "4. session_6" in message
    assert "8. session_2" in message

    handled, current, message = await _handle_local_session_command(
        raw_text="/preview_session general",
        gateway=gateway,
        current_session_key="session_9",
    )
    assert handled is True
    assert current == "session_9"
    assert "Session: session_1" in message
    assert "Alias: general" in message
    assert "Request text 1" in message

    handled, current, message = await _handle_local_session_command(
        raw_text="/search_session general",
        gateway=gateway,
        current_session_key="session_9",
    )
    assert handled is True
    assert current == "session_9"
    assert "session_1" in message
    assert "general" in message

    handled, current, message = await _handle_local_session_command(
        raw_text="/switch_session general",
        gateway=gateway,
        current_session_key="session_9",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
    )
    assert handled is True
    assert current == "session_1"
    assert message == "Switched to session: session_1\nActive conversation: conv_1"
    active = store.get_active_session_for_channel(
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
    )
    assert active is not None
    assert active.session_key == "session_1"

    store.register_conversation_session(
        session_key="session_1",
        conversation_id="conv_extra",
        last_task_id="task_extra",
    )
    store.record_request(
        correlation_id="corr_extra",
        session_key="session_1",
        request_kind="interactive",
        request_text="Extra conversation request",
        conversation_id="conv_extra",
        followup_to_task_id="task_extra",
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/list_conversations",
        gateway=gateway,
        current_session_key="session_1",
    )
    assert handled is True
    assert current == "session_1"
    assert "Conversations for session_1:" in message
    assert "conv_1 *" in message
    assert "conv_extra" in message

    handled, current, message = await _handle_local_session_command(
        raw_text="/switch_conversation conv_extra",
        gateway=gateway,
        current_session_key="session_1",
    )
    assert handled is True
    assert current == "session_1"
    assert message == "Active conversation: conv_extra"
    assert store.get_session("session_1").conversation_id == "conv_extra"
    assert store.get_session("session_1").last_task_id == "task_extra"

    handled, current, message = await _handle_local_session_command(
        raw_text="/rename_session dev",
        gateway=gateway,
        current_session_key="session_1",
    )
    assert handled is True
    assert current == "session_1"
    assert message == "Session session_1 aliased as: dev"
    assert store.get_session_by_alias("dev") is not None

    handled, current, message = await _handle_local_session_command(
        raw_text="/rename_session session_2 ops",
        gateway=gateway,
        current_session_key="session_1",
    )
    assert handled is True
    assert current == "session_1"
    assert message == "Session session_2 aliased as: ops"
    assert store.get_session_by_alias("ops") is not None

    handled, current, message = await _handle_local_session_command(
        raw_text="/rename_session session_3 dev",
        gateway=gateway,
        current_session_key="session_1",
    )
    assert handled is True
    assert current == "session_1"
    assert message == "Alias already in use: dev"


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_starts_github_flow(
    tmp_path, monkeypatch
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        network={"public_origin": "http://public.example.test"},
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 18081,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        assert base_url == "http://127.0.0.1:8766"
        assert token == "test-token"
        assert method == "POST"
        assert path == "/v1/auth/github/sessions"
        _assert_owned_broker_payload(payload, gateway, method="web")
        return {"login_url": "http://127.0.0.1:8766/auth/github/v1.mocked_session"}

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/link cli:github",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "GitHub sign-in started." in message
    assert "http://public.example.test:18081/auth/github/v1.mocked_session" in message
    assert "Broker URL note" not in message
    assert "http://127.0.0.1:8766" not in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_uses_configured_public_broker_port_for_github_web_all(
    tmp_path,
    monkeypatch,
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        network={"public_origin": "http://192.0.2.10"},
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 18600,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.setenv("SECURE_TOOL_BROKER_PUBLIC_PORT", "8766")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        assert base_url == "http://127.0.0.1:8766"
        assert token == "test-token"
        assert method == "POST"
        assert path == "/v1/auth/github/sessions"
        _assert_owned_broker_payload(payload, gateway, method="web-all")
        return {
            "method": "web-all",
            "flow_kind": "external_browser",
            "login_url": "http://127.0.0.1:8766/auth/github/v1.mocked_session",
        }

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/link cli:github web-all",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "http://192.0.2.10:18600/auth/github/v1.mocked_session" in message
    assert "http://192.0.2.10:8766/auth/github/v1.mocked_session" not in message
    assert "complete the high-privilege GitHub browser sign-in and MFA" in message
    assert "Broker URL note" not in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_starts_github_web_flow(
    tmp_path, monkeypatch
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 8766,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        assert base_url == "http://127.0.0.1:8766"
        assert token == "test-token"
        assert method == "POST"
        assert path == "/v1/auth/github/sessions"
        _assert_owned_broker_payload(payload, gateway, method="web")
        return {
            "method": "web",
            "flow_kind": "external_browser",
            "login_url": "http://127.0.0.1:8766/auth/github/v1.mocked_session",
        }

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/link cli:github web",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "GitHub sign-in started." in message
    assert "complete the GitHub browser sign-in and MFA" in message
    assert "http://127.0.0.1:8766/auth/github/v1.mocked_session" in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_starts_github_web_all_flow(
    tmp_path, monkeypatch
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 8766,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        assert base_url == "http://127.0.0.1:8766"
        assert token == "test-token"
        assert method == "POST"
        assert path == "/v1/auth/github/sessions"
        _assert_owned_broker_payload(payload, gateway, method="web-all")
        return {
            "method": "web-all",
            "flow_kind": "external_browser",
            "login_url": "http://127.0.0.1:8766/auth/github/v1.mocked_session",
        }

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/link cli:github web-all",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "GitHub sign-in started." in message
    assert "complete the high-privilege GitHub browser sign-in and MFA" in message
    assert "http://127.0.0.1:8766/auth/github/v1.mocked_session" in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_rejects_legacy_broker_auth_url(
    tmp_path, monkeypatch
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 8766,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        return {
            "method": "web",
            "flow_kind": "external_browser",
            "login_url": "http://127.0.0.1:8766/auth/github/raw_legacy_session",
        }

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/link cli:github web",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "older in-memory sign-in address" in message
    assert "Restart the secure tool broker and gateway" in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_uses_live_broker_signed_github_web_url(
    tmp_path,
    monkeypatch,
) -> None:
    starts = []

    def fake_start(self, session, payload, logger):
        starts.append(dict(payload))
        session.state.update(
            {
                "status": "pending",
                "external_url": "https://github.com/login/device",
                "user_code": "LIVE-1234",
                "message": "Waiting for GitHub authorization.",
            }
        )

    monkeypatch.setattr(
        "secure_tool_broker.auth_flows.GitHubWebAuthFlow.start", fake_start
    )
    # The gateway process holds its own caller token, derived from the root.
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", caller_token(CALLER_GATEWAY))
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=open_gate())
    server = TestServer(service.build_app(), host="127.0.0.1")
    await server.start_server()
    try:
        broker_internal_base_url = str(server.make_url("")).rstrip("/")
        store = GatewayStateStore(str(tmp_path / "gateway.db"))
        gateway = FakeGateway(store)
        gateway.config = GatewayInstanceConfig(
            instance_name="test_instance",
            instance_root=str(tmp_path / "instance"),
            network={"public_origin": broker_internal_base_url},
            security_layer={
                "mode": "broker",
                "broker_internal_base_url": broker_internal_base_url,
                "broker_public_port": None,
            },
        )
        # The gateway and the broker serve the same team (one instance config).
        service._access_gate.team_id = gateway.config.manager.team_id

        handled, current, message = await _handle_local_session_command(
            raw_text="/link cli:github web",
            gateway=gateway,
            current_session_key="session_1",
        )

        assert handled is True
        assert current == "session_1"
        match = re.search(r"https?://\S+/auth/github/\S+", message)
        assert match is not None
        login_url = match.group(0)
        assert "/auth/github/v1." in login_url
        service._auth_sessions.clear()

        def fetch_auth_page() -> str:
            with urlopen(login_url, timeout=10) as response:
                assert response.status == 200
                return response.read().decode("utf-8", errors="replace")

        page_text = await asyncio.to_thread(fetch_auth_page)
    finally:
        await server.close()

    assert "GitHub Browser Auth" in page_text
    assert "LIVE-1234" in page_text
    assert starts == [{"method": "web"}]


@pytest.mark.asyncio
async def test_run_secure_tool_broker_service_uses_config_bind_settings(
    tmp_path, monkeypatch
):
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    )
    config.security_layer.mode = "broker"
    config.security_layer.broker_bind_host = "0.0.0.0"
    config.security_layer.broker_bind_port = 8767
    config.agent_types = {
        agent_type: AgentTypeRuntimeSettings.model_validate(
            {
                "llm": {
                    "provider": "openai-codex",
                    "model_id": "gpt-5.6-luna",
                }
            }
        )
        for agent_type in (
            "CodeWritingAgent",
            "ResearchComparisonAgent",
            "WebResearchAgent",
        )
    }
    config_path = config.save()
    observed: dict[str, object] = {}
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OP_CONFIG_DIR", raising=False)
    monkeypatch.delenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH", raising=False
    )

    async def fake_run_secure_tool_broker(*, host, port, token, **authorization):
        observed.update(
            {
                "host": host,
                "port": port,
                "token": token,
                "authorization_keys": tuple(sorted(authorization)),
                "mcp_config": authorization["mcp_config"],
                "access_gate": authorization["access_gate"],
            }
        )

    monkeypatch.setattr(
        "assistant_cli.run_secure_tool_broker", fake_run_secure_tool_broker
    )

    await _run_secure_tool_broker_service(str(config_path))

    mcp_config = observed.pop("mcp_config")
    assert isinstance(mcp_config, BrokerMCPConfig)
    assert {
        name: settings.agent.external_mcp_servers
        for name, settings in mcp_config.agent_types.items()
    } == {
        "CodeWritingAgent": None,
        "ResearchComparisonAgent": None,
        "WebResearchAgent": None,
    }
    # Authorization is on by default: the gate decides every agent tool use
    # (including MCP calls) with grants. The broker gets no other
    # authorization: MCP discovery follows approved attachments and the
    # hosted-server routes admit only the manager and the gateway.
    assert config.authorization.enabled is True
    access_gate = observed.pop("access_gate")
    assert isinstance(access_gate, BrokerAccessGate)
    assert access_gate.team_id == "team_test"
    assert access_gate.access is not None
    assert access_gate.links is not None, "account links are read in both modes"
    assert observed == {
        "host": "0.0.0.0",
        "port": 8767,
        "token": "test-token",
        "authorization_keys": ("access_gate", "mcp_config", "tool_setup_recheck_days"),
    }


@pytest.mark.asyncio
async def test_run_secure_tool_broker_service_sets_broker_credential_state_env(
    tmp_path, monkeypatch
):
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    )
    config.security_layer.mode = "broker"
    config.security_layer.broker_credentials_dir = "broker_credentials"
    config_path = config.save()
    observed: dict[str, object] = {}
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OP_CONFIG_DIR", raising=False)
    monkeypatch.delenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH", raising=False
    )

    async def fake_run_secure_tool_broker(*, host, port, token, **_authorization):
        observed["credentials_dir"] = os.environ.get(
            "SECURE_TOOL_BROKER_CREDENTIALS_DIR"
        )
        observed["gh_config_dir"] = os.environ.get("GH_CONFIG_DIR")
        observed["op_config_dir"] = os.environ.get("OP_CONFIG_DIR")
        observed["credential_state_backend_mode"] = os.environ.get(
            "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE"
        )
        observed["credential_state_backend_namespace"] = os.environ.get(
            "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE"
        )
        observed["template_overrides_path"] = os.environ.get(
            "SECURE_TOOL_BROKER_CREDENTIAL_TEMPLATE_OVERRIDES_PATH"
        )
        observed["secret_cache_ttl"] = os.environ.get(
            "SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS"
        )
        observed["token"] = token

    monkeypatch.setattr(
        "assistant_cli.run_secure_tool_broker", fake_run_secure_tool_broker
    )

    await _run_secure_tool_broker_service(str(config_path))

    expected_dir = str((config.instance_root_path / "broker_credentials").resolve())
    assert observed["credentials_dir"] == expected_dir
    assert observed["gh_config_dir"] == str((Path(expected_dir) / "gh").resolve())
    assert observed["op_config_dir"] == str(
        (Path(expected_dir) / "onepassword" / "config").resolve()
    )
    assert observed["credential_state_backend_mode"] == "credential_source"
    assert observed["credential_state_backend_namespace"] == "team-cred-store"
    assert observed["template_overrides_path"] == str(
        (config.instance_root_path / "credential_template_overrides.yaml").resolve()
    )
    assert observed["secret_cache_ttl"] == "300"
    assert observed["token"] == "test-token"


@pytest.mark.asyncio
async def test_run_secure_tool_broker_service_sets_broker_appearance_env(
    tmp_path, monkeypatch
):
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    )
    config.security_layer.mode = "broker"
    config.ui.onboarding.appearance.light_colors = [
        "#0011aa",
        "#ffee00",
        "#00aa44",
        "#dd2200",
    ]
    config.ui.onboarding.appearance.light_diffusion = 12
    config.ui.onboarding.appearance.polygon_opacity_low = 16
    config.ui.onboarding.appearance.polygon_opacity_high = 64
    config.ui.onboarding.appearance.polygon_line_color = "#123456"
    config.ui.onboarding.appearance.polygon_line_opacity = 31
    expected_appearance = config.ui.onboarding.appearance.model_dump(mode="json")
    config_path = config.save()
    observed: dict[str, object] = {}
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_run_secure_tool_broker(*, host, port, token, **_authorization):
        observed["appearance"] = os.environ.get(BROKER_APPEARANCE_ENV)
        observed["token"] = token

    monkeypatch.setattr(
        "assistant_cli.run_secure_tool_broker", fake_run_secure_tool_broker
    )

    await _run_secure_tool_broker_service(str(config_path))

    assert json.loads(str(observed["appearance"])) == expected_appearance
    assert observed["token"] == "test-token"
    assert BROKER_APPEARANCE_ENV not in os.environ


@pytest.mark.asyncio
async def test_run_secure_tool_broker_service_loads_gog_keyring_password_file_from_env(
    tmp_path, monkeypatch
):
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    )
    config.security_layer.mode = "broker"
    config_path = config.save()
    secret_file = tmp_path / "gog-keyring-password"
    secret_file.write_text("from-secret-file\n", encoding="utf-8")
    env_file = tmp_path / "team.env"
    env_file.write_text(f"GOG_KEYRING_PASSWORD_FILE={secret_file}\n", encoding="utf-8")
    observed: dict[str, object] = {}
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.delenv("GOG_KEYRING_PASSWORD", raising=False)
    monkeypatch.delenv("GOG_KEYRING_PASSWORD_FILE", raising=False)

    async def fake_run_secure_tool_broker(*, host, port, token, **_authorization):
        observed["gog_keyring_password"] = os.environ.get("GOG_KEYRING_PASSWORD")
        observed["gog_keyring_password_file"] = os.environ.get(
            "GOG_KEYRING_PASSWORD_FILE"
        )
        observed["token"] = token

    monkeypatch.setattr(
        "assistant_cli.run_secure_tool_broker", fake_run_secure_tool_broker
    )

    await _run_secure_tool_broker_service(str(config_path), env_path=str(env_file))

    assert observed["gog_keyring_password"] == "from-secret-file"
    assert observed["gog_keyring_password_file"] == str(secret_file)
    assert observed["token"] == "test-token"
    assert "GOG_KEYRING_PASSWORD" not in os.environ
    assert "GOG_KEYRING_PASSWORD_FILE" not in os.environ


@pytest.mark.asyncio
async def test_run_secure_tool_broker_service_sets_bitwarden_server_url_from_config(
    tmp_path, monkeypatch
):
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    )
    config.security_layer.mode = "broker"
    config.security_layer.bitwarden_server_url = "https://vault.example.test"
    config_path = config.save()
    observed: dict[str, object] = {}
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.delenv("BITWARDEN_SERVER_URL", raising=False)

    async def fake_run_secure_tool_broker(*, host, port, token, **_authorization):
        observed["bitwarden_server_url"] = os.environ.get("BITWARDEN_SERVER_URL")
        observed["token"] = token

    monkeypatch.setattr(
        "assistant_cli.run_secure_tool_broker", fake_run_secure_tool_broker
    )

    await _run_secure_tool_broker_service(str(config_path))

    assert observed["bitwarden_server_url"] == "https://vault.example.test"
    assert observed["token"] == "test-token"


@pytest.mark.asyncio
async def test_run_secure_tool_broker_service_uses_internal_base_url_bind_fallback(
    tmp_path, monkeypatch
):
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    )
    config.security_layer.mode = "broker"
    config.security_layer.broker_internal_base_url = "http://192.0.2.10:9876"
    config_path = config.save()
    raw_saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    observed: dict[str, object] = {}
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_run_secure_tool_broker(*, host, port, token, **_authorization):
        observed.update({"host": host, "port": port, "token": token})

    monkeypatch.setattr(
        "assistant_cli.run_secure_tool_broker", fake_run_secure_tool_broker
    )

    await _run_secure_tool_broker_service(str(config_path))

    assert "broker_bind_host" not in raw_saved["security_layer"]
    assert "broker_bind_port" not in raw_saved["security_layer"]
    assert observed == {"host": "192.0.2.10", "port": 9876, "token": "test-token"}


@pytest.mark.asyncio
async def test_run_secure_tool_broker_service_env_overrides_config_bind_settings(
    tmp_path, monkeypatch
):
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    )
    config.security_layer.mode = "broker"
    config.security_layer.broker_bind_host = "0.0.0.0"
    config.security_layer.broker_bind_port = 8767
    config_path = config.save()
    observed: dict[str, object] = {}
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.setenv("SECURE_TOOL_BROKER_HOST", "127.0.0.2")
    monkeypatch.setenv("SECURE_TOOL_BROKER_PORT", "8768")

    async def fake_run_secure_tool_broker(*, host, port, token, **_authorization):
        observed.update({"host": host, "port": port, "token": token})

    monkeypatch.setattr(
        "assistant_cli.run_secure_tool_broker", fake_run_secure_tool_broker
    )

    await _run_secure_tool_broker_service(str(config_path))

    assert observed == {"host": "127.0.0.2", "port": 8768, "token": "test-token"}


@pytest.mark.asyncio
async def test_run_secure_tool_broker_service_cli_overrides_env_and_config_bind_settings(
    tmp_path, monkeypatch
):
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="UTC",
    )
    config.security_layer.mode = "broker"
    config.security_layer.broker_bind_host = "0.0.0.0"
    config.security_layer.broker_bind_port = 8767
    config_path = config.save()
    observed: dict[str, object] = {}
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.setenv("SECURE_TOOL_BROKER_HOST", "127.0.0.2")
    monkeypatch.setenv("SECURE_TOOL_BROKER_PORT", "8768")

    async def fake_run_secure_tool_broker(*, host, port, token, **_authorization):
        observed.update({"host": host, "port": port, "token": token})

    monkeypatch.setattr(
        "assistant_cli.run_secure_tool_broker", fake_run_secure_tool_broker
    )

    await _run_secure_tool_broker_service(str(config_path), host="127.0.0.3", port=8769)

    assert observed == {"host": "127.0.0.3", "port": 8769, "token": "test-token"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "provider", "sign_in_method", "prompt"),
    [
        ("/link cli:google", "google", "web", "sign in with your Google account"),
        ("/link cli:x", "x", "web", "sign in with your X account"),
        ("/auth bitwarden", "bitwarden", None, "complete broker-side API key login and unlock"),
    ],
)
async def test_handle_local_session_auth_command_starts_non_github_flows(
    tmp_path,
    monkeypatch,
    command,
    provider,
    sign_in_method,
    prompt,
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 8766,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        assert base_url == "http://127.0.0.1:8766"
        assert token == "test-token"
        assert method == "POST"
        assert path == f"/v1/auth/{provider}/sessions"
        _assert_owned_broker_payload(
            payload, gateway, **({"method": sign_in_method} if sign_in_method else {})
        )
        return {"login_url": f"http://127.0.0.1:8766/auth/{provider}/v1.mocked_session"}

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text=command,
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    display_name = {"google": "Google", "x": "X", "bitwarden": "Bitwarden"}[provider]
    assert f"{display_name} sign-in started." in message
    assert prompt in message
    assert f"http://127.0.0.1:8766/auth/{provider}/v1.mocked_session" in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_sets_bitwarden_server_url(
    tmp_path, monkeypatch
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 8766,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        assert base_url == "http://127.0.0.1:8766"
        assert token == "test-token"
        assert method == "POST"
        assert path == "/v1/auth/bitwarden/server"
        _assert_owned_broker_payload(
            payload,
            gateway,
            server_url="https://vault.example.test",
        )
        return {
            "status": "success",
            "content": {"server_url": "https://vault.example.test"},
        }

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/auth bitwarden server https://vault.example.test",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "Bitwarden CLI server URL configured: https://vault.example.test" in message
    assert "Run /auth bitwarden again" in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_defaults_to_google_web_flow(
    tmp_path, monkeypatch
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 8766,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        assert base_url == "http://127.0.0.1:8766"
        assert token == "test-token"
        assert method == "POST"
        assert path == "/v1/auth/google/sessions"
        # /link cli:google names its default sign-in (the browser flow) explicitly.
        _assert_owned_broker_payload(payload, gateway, method="web")
        return {
            "method": "web",
            "flow_kind": "browser_redirect_form",
            "login_url": "http://127.0.0.1:8766/auth/google/v1.mocked_session",
        }

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/link cli:google",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "Google sign-in started." in message
    assert "sign in with your Google account" in message
    assert "http://127.0.0.1:8766/auth/google/v1.mocked_session" in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_accepts_google_json_token_method(
    tmp_path, monkeypatch
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 8766,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        assert method == "POST"
        assert path == "/v1/auth/google/sessions"
        _assert_owned_broker_payload(payload, gateway, method="json_token")
        return {
            "method": "json_token",
            "flow_kind": "form",
            "login_url": "http://127.0.0.1:8766/auth/google/v1.mocked_session",
        }

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/link cli:google json_token",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "paste a gog token export JSON" in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_google_credentials_is_removed(
    tmp_path, monkeypatch
):
    """A person's own Google sign-in app is pasted on the /link cli:google page now."""

    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 8766,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(**_kwargs):
        raise AssertionError("no sign-in page opens for a removed command")

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/auth google credentials",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "/link <tool_id>" in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_status_lists_providers(tmp_path, monkeypatch):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 8766,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        if path == "/v1/auth/model-keys/status":
            assert method == "GET"
            return {"endpoints": []}
        assert method == "POST"
        assert path == "/v1/auth/status"
        _assert_owned_broker_payload(payload, gateway)
        return {
            "providers": {
                "github": {
                    "status": "auth_required",
                    "authenticated": False,
                    "message": "GitHub auth required.",
                },
                "google": {
                    "status": "configured",
                    "authenticated": True,
                    "message": "Google configured.",
                },
                "bitwarden": {
                    "status": "auth_required",
                    "authenticated": False,
                    "message": "Bitwarden auth required.",
                },
            }
        }

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/auth",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "Secure broker auth status:" in message
    assert "github: auth_required" in message
    assert "google: authenticated" in message
    assert "Model provider keys: none stored. Administrators save them with /auth model." in message


@pytest.mark.asyncio
async def test_handle_local_session_auth_command_reports_env_backed_provider_status(
    tmp_path, monkeypatch
):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)
    gateway.config = GatewayInstanceConfig(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        security_layer={
            "mode": "broker",
            "broker_internal_base_url": "http://127.0.0.1:8766",
            "broker_public_port": 8766,
        },
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        assert method == "POST"
        assert path == "/v1/auth/status/onepassword"
        _assert_owned_broker_payload(payload, gateway)
        return {
            "provider": "onepassword",
            "status": "auth_required",
            "authenticated": False,
            "message": "1Password auth required.",
        }

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    handled, current, message = await _handle_local_session_command(
        raw_text="/auth onepassword",
        gateway=gateway,
        current_session_key="session_1",
    )

    assert handled is True
    assert current == "session_1"
    assert "onepassword: auth_required" in message
    assert "has no sign-in page" in message


@pytest.mark.asyncio
async def test_shared_tour_command_forwards_explicit_canonical_user_id(tmp_path):
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = FakeGateway(store)

    handled, current, message = await handle_interactive_session_command(
        raw_text="/tour",
        gateway=gateway,
        current_session_key="website:web-chat:browser_1",
        channel_name="website",
        user_id="browser_1",
        chat_id="web-chat",
        canonical_user_id="user_alpha",
    )

    assert handled is True
    assert current == "website:web-chat:browser_1"
    assert message == "Tour started."
    assert gateway.sent_user_messages[0]["canonical_user_id"] == "user_alpha"


def test_cli_reply_command_and_tracker():
    tracker = CliMessageTracker()
    ref = tracker.add(
        text="hello",
        session_key="session_1",
        conversation_id="conv_1",
    )
    assert tracker.get(ref.ref_id).conversation_id == "conv_1"

    ref_id, reply_text = _parse_reply_command("/reply msg_1 continue this thread")
    assert ref_id == "msg_1"
    assert reply_text == "continue this thread"

    missing_ref_id, missing_reply_text = _parse_reply_command("/reply msg_1")
    assert missing_ref_id is None
    assert missing_reply_text is None


def test_interactive_console_redraws_prompt_after_async_output(monkeypatch):
    stream = _FakeTtyStream()
    console = InteractiveCliConsole(stream=stream)

    class _FakeReadline:
        @staticmethod
        def get_line_buffer() -> str:
            return ""

        @staticmethod
        def redisplay() -> None:
            return None

    monkeypatch.setattr("assistant_cli.readline", _FakeReadline)
    console._input_active = True
    console._active_prompt = "> "

    console.print_text("[19:27] [msg_2] (conv_1) Reminder fired")

    assert stream.getvalue() == "\r\033[2K[19:27] [msg_2] (conv_1) Reminder fired\n> "


@pytest.mark.asyncio
async def test_run_gateway_services_treats_cancelled_idle_wait_as_clean_shutdown(
    monkeypatch,
):
    events: list[str] = []
    event_sink_handler = object()
    gateway_instance = None
    config_fixture = SimpleNamespace(
        assistant=SimpleNamespace(manager_transition_notices_enabled=False),
        manager=SimpleNamespace(manager_agent_id="manager_1"),
        interactive_apps=SimpleNamespace(
            adapter_host=SimpleNamespace(enabled=False)
        ),
    )

    class FakeGateway:
        def __init__(self, config, output_sink):
            nonlocal gateway_instance
            assert config is config_fixture
            assert output_sink is not None
            self.event_sink = None
            self.transition_notices_enabled = False
            self.application = sentinel.application
            self.manager_transitions = self
            self.proactive_delivery = self
            gateway_instance = self

        async def connect(self):
            assert self.event_sink is None
            assert self.transition_notices_enabled is False
            events.append("gateway.connect")

        async def close(self):
            events.append("gateway.close")

        def bind_event_sink(self, event_sink):
            self.event_sink = event_sink

        def set_hooks(self, **_hooks):
            pass

        def enable_notices(self):
            self.transition_notices_enabled = True
            events.append("gateway.enable_notices")

        async def emit_notice(self, text):
            assert self.event_sink is event_sink_handler
            events.append(f"gateway.notice:{text}")

    class FakeAppManager:
        def __init__(
            self,
            loaded_config,
            application,
            output_sink,
            include_apps,
        ):
            assert loaded_config is config_fixture
            assert output_sink is not None
            assert include_apps is None
            assert application is sentinel.application
            self.registry = sentinel.registry
            self.gateway = gateway_instance
            self.handle_gateway_event = event_sink_handler
            self.has_enabled_apps = True
            self.has_running_apps = True
            self.enabled_app_names = ["telegram"]
            self.running_app_names = ["telegram"]

        async def start(self):
            assert self.gateway.event_sink is None
            assert self.gateway.transition_notices_enabled is False
            events.append("app.start")

        async def handle_manager_unavailable(self):
            pass

        async def handle_manager_available(self, _presence):
            pass

        async def stop(self):
            assert callable(self.gateway.event_sink)
            assert self.gateway.transition_notices_enabled is False
            events.append("app.stop")

    class _CancelledEvent:
        async def wait(self):
            raise asyncio.CancelledError()

    monkeypatch.setattr(
        "assistant_cli._load_gateway_instance_config",
        lambda *args, **kwargs: config_fixture,
    )
    monkeypatch.setattr("assistant_cli.UserGateway", FakeGateway)
    monkeypatch.setattr("assistant_cli.InteractiveAppManager", FakeAppManager)
    monkeypatch.setattr("assistant_cli.asyncio.Event", lambda: _CancelledEvent())

    await _run_gateway_services(
        "instance_config.yaml", env_path=None, include_apps=None, disable_apps=False
    )

    assert gateway_instance is not None
    assert callable(gateway_instance.event_sink)
    assert gateway_instance.transition_notices_enabled is False
    assert events == ["gateway.connect", "app.start", "app.stop", "gateway.close"]


@pytest.mark.asyncio
async def test_run_gateway_services_emits_headless_startup_notice_via_gateway(
    monkeypatch,
):
    events: list[str] = []
    event_sink_handler = object()
    gateway_instance = None
    config_fixture = SimpleNamespace(
        assistant=SimpleNamespace(manager_transition_notices_enabled=False),
        manager=SimpleNamespace(manager_agent_id="manager_1"),
        interactive_apps=SimpleNamespace(
            adapter_host=SimpleNamespace(enabled=False)
        ),
    )

    class FakeGateway:
        def __init__(self, config, output_sink):
            nonlocal gateway_instance
            assert config is config_fixture
            assert output_sink is not None
            self.event_sink = None
            self.transition_notices_enabled = False
            self.application = sentinel.application
            self.manager_transitions = self
            self.proactive_delivery = self
            gateway_instance = self

        async def connect(self):
            events.append("gateway.connect")

        async def close(self):
            events.append("gateway.close")

        def bind_event_sink(self, event_sink):
            self.event_sink = event_sink

        def set_hooks(self, **_hooks):
            pass

        def enable_notices(self):
            self.transition_notices_enabled = True
            events.append("gateway.enable_notices")

        async def emit_notice(self, text):
            assert callable(self.event_sink)
            events.append(f"gateway.notice:{text}")

    class FakeAppManager:
        def __init__(
            self,
            loaded_config,
            application,
            output_sink,
            include_apps,
        ):
            assert loaded_config is config_fixture
            assert output_sink is not None
            assert include_apps is None
            assert application is sentinel.application
            self.registry = sentinel.registry
            self.gateway = gateway_instance
            self.handle_gateway_event = event_sink_handler
            self.has_enabled_apps = True
            self.has_running_apps = True
            self.enabled_app_names = ["telegram"]
            self.running_app_names = ["telegram"]

        async def start(self):
            events.append("app.start")

        async def handle_manager_unavailable(self):
            pass

        async def handle_manager_available(self, _presence):
            pass

        async def stop(self):
            assert callable(self.gateway.event_sink)
            assert self.gateway.transition_notices_enabled is False
            events.append("app.stop")

    class _CancelledEvent:
        async def wait(self):
            raise asyncio.CancelledError()

    monkeypatch.setattr(
        "assistant_cli._load_gateway_instance_config",
        lambda *args, **kwargs: config_fixture,
    )
    monkeypatch.setattr("assistant_cli.UserGateway", FakeGateway)
    monkeypatch.setattr("assistant_cli.InteractiveAppManager", FakeAppManager)
    monkeypatch.setattr("assistant_cli.asyncio.Event", lambda: _CancelledEvent())
    monkeypatch.setenv(
        "TEAM_HEADLESS_STARTUP_NOTICE",
        '["Headless startup migrated instance_config.yaml.", "Second startup notice."]',
    )

    await _run_gateway_services(
        "instance_config.yaml", env_path=None, include_apps=None, disable_apps=False
    )

    assert events == [
        "gateway.connect",
        "app.start",
        "gateway.notice:Headless startup migrated instance_config.yaml.",
        "gateway.notice:Second startup notice.",
        "app.stop",
        "gateway.close",
    ]


@pytest.mark.asyncio
async def test_run_gateway_services_keeps_gateway_running_when_apps_do_not_start(
    monkeypatch, capsys
):
    events: list[str] = []
    gateway_instance = None
    config_fixture = SimpleNamespace(
        assistant=SimpleNamespace(manager_transition_notices_enabled=False),
        manager=SimpleNamespace(manager_agent_id="manager_1"),
        interactive_apps=SimpleNamespace(
            adapter_host=SimpleNamespace(enabled=False)
        ),
    )

    class FakeGateway:
        def __init__(self, config, output_sink):
            nonlocal gateway_instance
            assert config is config_fixture
            assert output_sink is not None
            self.event_sink = None
            self.transition_notices_enabled = False
            self.application = sentinel.application
            self.manager_transitions = self
            self.proactive_delivery = self
            gateway_instance = self

        async def connect(self):
            events.append("gateway.connect")

        async def close(self):
            events.append("gateway.close")

        def enable_notices(self):
            self.transition_notices_enabled = True
            events.append("gateway.enable_notices")

    class FakeAppManager:
        def __init__(
            self,
            loaded_config,
            application,
            output_sink,
            include_apps,
        ):
            assert loaded_config is config_fixture
            assert output_sink is not None
            assert include_apps is None
            assert application is sentinel.application
            self.registry = sentinel.registry
            self.gateway = gateway_instance
            self.has_enabled_apps = True
            self.has_running_apps = False
            self.enabled_app_names = ["discord"]
            self.running_app_names = []
            self.handle_gateway_event = object()

        async def start(self):
            assert self.gateway.event_sink is None
            events.append("app.start")

        async def stop(self):
            assert self.gateway.event_sink is None
            events.append("app.stop")

    class _CancelledEvent:
        async def wait(self):
            raise asyncio.CancelledError()

    monkeypatch.setattr(
        "assistant_cli._load_gateway_instance_config",
        lambda *args, **kwargs: config_fixture,
    )
    monkeypatch.setattr("assistant_cli.UserGateway", FakeGateway)
    monkeypatch.setattr("assistant_cli.InteractiveAppManager", FakeAppManager)
    monkeypatch.setattr("assistant_cli.asyncio.Event", lambda: _CancelledEvent())

    await _run_gateway_services(
        "instance_config.yaml", env_path=None, include_apps=None, disable_apps=False
    )

    assert gateway_instance is not None
    assert gateway_instance.event_sink is None
    assert gateway_instance.transition_notices_enabled is False
    assert events == ["gateway.connect", "app.start", "app.stop", "gateway.close"]
    assert (
        "no configured interactive apps started successfully" in capsys.readouterr().out
    )


def test_cli_parser_accepts_schedule_pause_resume_and_heartbeat_commands():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["run-user-docs", "--config", "config.yaml"])

    args = parser.parse_args(
        [
            "cron-pause",
            "--config",
            "/tmp/config.yaml",
            "--job-id",
            "job_1",
            "--minutes",
            "30",
        ]
    )
    assert args.command == "cron-pause"
    assert args.job_id == "job_1"
    assert args.minutes == 30

    args = parser.parse_args(
        ["cron-resume", "--config", "/tmp/config.yaml", "--job-id", "job_1"]
    )
    assert args.command == "cron-resume"

    args = parser.parse_args(
        ["cron-disable", "--config", "/tmp/config.yaml", "--job-id", "job_1"]
    )
    assert args.command == "cron-disable"

    args = parser.parse_args(
        [
            "objective-list",
            "--config",
            "/tmp/config.yaml",
            "--owner-agent-id",
            "manager_id",
            "--include-closed",
        ]
    )
    assert args.command == "objective-list"
    assert args.owner_agent_id == "manager_id"
    assert args.include_closed is True

    args = parser.parse_args(
        ["objective-show", "--config", "/tmp/config.yaml", "--objective-id", "objective_id_1"]
    )
    assert args.command == "objective-show"
    assert args.objective_id == "objective_id_1"

    # Objective changes go through the running manager (Runtime controls or
    # the team lead), so the host commands that wrote the record directly
    # from another process are gone.
    for removed in ("objective-pause", "objective-resume", "objective-abandon"):
        with pytest.raises(SystemExit):
            parser.parse_args(
                [removed, "--config", "/tmp/config.yaml", "--objective-id", "objective_id_1"]
            )

    args = parser.parse_args(
        ["heartbeat-pause", "--config", "/tmp/config.yaml", "--minutes", "15"]
    )
    assert args.command == "heartbeat-pause"

    args = parser.parse_args(["heartbeat-resume", "--config", "/tmp/config.yaml"])
    assert args.command == "heartbeat-resume"

    args = parser.parse_args(["heartbeat-disable", "--config", "/tmp/config.yaml"])
    assert args.command == "heartbeat-disable"

    args = parser.parse_args(["onboard", "--instance-name", "bot"])
    assert args.command == "onboard"
    assert args.base_env_file is None
    assert args.request_timeout_seconds is None
    assert args.generate_internal_credentials is None

    args = parser.parse_args(
        ["onboard", "--instance-name", "bot", "--request-timeout-seconds", "7200"]
    )
    assert args.request_timeout_seconds == 7200

    args = parser.parse_args(
        ["onboard", "--instance-name", "bot", "--no-generate-internal-credentials"]
    )
    assert args.generate_internal_credentials is False

    args = parser.parse_args(
        ["onboard", "--instance-name", "bot", "--generate-internal-credentials"]
    )
    assert args.generate_internal_credentials is True

    args = parser.parse_args(
        ["bridge-chat", "--config", "/tmp/config.yaml", "--env", "/tmp/debug.env"]
    )
    assert args.command == "bridge-chat"
    assert args.env == "/tmp/debug.env"

    args = parser.parse_args(
        ["run-gateway", "--config", "/tmp/config.yaml", "--no-apps"]
    )
    assert args.command == "run-gateway"
    assert args.no_apps is True

    args = parser.parse_args(["run-gateway", "--no-apps"])
    assert args.command == "run-gateway"
    assert args.config is None
    assert args.no_apps is True

    args = parser.parse_args(
        ["run-app", "--config", "/tmp/config.yaml", "--app", "telegram"]
    )
    assert args.command == "run-app"
    assert args.app == "telegram"

    args = parser.parse_args(
        ["run-app", "--config", "/tmp/config.yaml", "--app", "discord"]
    )
    assert args.command == "run-app"
    assert args.app == "discord"

    args = parser.parse_args(
        ["run-app", "--config", "/tmp/config.yaml", "--app", "whatsapp"]
    )
    assert args.command == "run-app"
    assert args.app == "whatsapp"

    args = parser.parse_args(
        ["run-app", "--config", "/tmp/config.yaml", "--app", "signal"]
    )
    assert args.command == "run-app"
    assert args.app == "signal"

    args = parser.parse_args(
        ["run-app", "--config", "/tmp/config.yaml", "--app", "photon"]
    )
    assert args.command == "run-app"
    assert args.app == "photon"

    args = parser.parse_args(
        ["run-app", "--config", "/tmp/config.yaml", "--app", "website"]
    )
    assert args.command == "run-app"
    assert args.app == "website"

    args = parser.parse_args(
        ["run-app", "--config", "/tmp/config.yaml", "--app", "example.matrix"]
    )
    assert args.app == "example.matrix"

    args = parser.parse_args(
        ["interactive-app-list", "--config", "/tmp/config.yaml"]
    )
    assert args.command == "interactive-app-list"

    args = parser.parse_args(["onbard", "--instance-name", "bot", "--non-interactive"])
    assert args.command == "onbard"
    assert args.instance_name == "bot"

    args = parser.parse_args(
        ["migrate-config", "--config", "/tmp/instance_config.yaml", "--yes"]
    )
    assert args.command == "migrate-config"
    assert args.config == "/tmp/instance_config.yaml"
    assert args.yes is True


def test_interactive_app_list_uses_registry_catalog(tmp_path, capsys) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="catalog-test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_catalog",
        timezone="UTC",
    )
    config_path = config.save(tmp_path / "instance_config.yaml")

    _interactive_app_list(str(config_path))

    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["plugin_id"] == "builtin.internal_website"
    assert payload[0]["configuration_schema"]["type"] == "object"
    telegram = next(
        item for item in payload if item["plugin_id"] == "builtin.telegram"
    )
    assert telegram["secret_reference_names"] == ["TELEGRAM_BOT_TOKEN"]


def test_resolve_instance_config_path_requires_config_or_instance_root(monkeypatch):
    monkeypatch.delenv("TEAM_INSTANCE_ROOT", raising=False)

    with pytest.raises(SystemExit) as exc:
        _resolve_instance_config_path(None)

    assert "set TEAM_INSTANCE_CONFIG, or set TEAM_INSTANCE_ROOT" in str(exc.value)


def test_collect_onboarding_answers_non_interactive_uses_explicit_and_default_values():
    parser = build_parser()
    args = parser.parse_args(
        [
            "onboard",
            "--instance-name",
            "mybot",
            "--assistant-name",
            "Ava",
            "--assistant-purpose",
            "Help with engineering operations.",
            "--user-role",
            "Platform engineer",
            "--user-context",
            "I run a small software team and prefer high-signal summaries.",
            "--preferred-channel",
            "discord",
            "--preferred-output-chat-id",
            "123456789012345678",
            "--preferred-channel-thread-id",
            "223344556677889900",
            "--enable-discord",
            "--discord-allow-user-id",
            "999",
            "--discord-allow-channel-id",
            "123456789012345678",
            "--discord-group-policy",
            "open",
            "--discord-command-guild-id",
            "777777777777777777",
            "--enable-telegram",
            "--telegram-allow-user-id",
            "111",
            "--telegram-allow-chat-id",
            "-222",
            "--telegram-group-policy",
            "open",
            "--telegram-manager-offline-policy",
            "notify",
            "--enable-signal",
            "--signal-base-url",
            "http://signal.example.test:8080",
            "--signal-account",
            "+15550001111",
            "--signal-allow-sender-id",
            "+15551234567",
            "--signal-allow-group-id",
            "group.signal",
            "--signal-group-policy",
            "open",
            "--signal-mention-marker",
            "@Ava",
            "--signal-poll-timeout-seconds",
            "3.5",
            "--signal-max-messages-per-poll",
            "25",
            "--signal-drop-pending-messages-on-startup",
            "--enable-photon",
            "--photon-project-id",
            "photon-project",
            "--photon-project-secret",
            "photon-secret",
            "--photon-allow-sender-id",
            "imessage:+15551234567",
            "--photon-allow-group-id",
            "photon-group",
            "--photon-group-policy",
            "open",
            "--photon-mention-marker",
            "@Ava",
            "--enable-whatsapp",
            "--whatsapp-allow-sender-id",
            "+15551234567",
            "--whatsapp-webhook-host",
            "127.0.0.1",
            "--whatsapp-webhook-port",
            "18080",
            "--whatsapp-webhook-path",
            "/hooks/whatsapp",
            "--whatsapp-public-base-url",
            "https://bots.example.com",
            "--heartbeat-active-hours",
            "--heartbeat-active-start-hour",
            "9",
            "--heartbeat-active-end-hour",
            "17",
            "--model-provider",
            "openai",
            "--model-id",
            "gpt-5",
            "--reasoning-effort",
            "high",
            "--reasoning-summary",
            "detailed",
            "--litellm-api-mode",
            "responses",
            "--public-origin",
            "https://public.example.test",
            "--secure-tool-broker-public-port",
            "9443",
            "--secure-tool-broker-bind-host",
            "127.0.0.1",
            "--secure-tool-broker-bind-port",
            "8766",
            "--secure-tool-broker-credentials-dir",
            "broker_credentials",
            "--internal-website-bind-host",
            "127.0.0.1",
            "--internal-website-bind-port",
            "18501",
            "--internal-website-public-port",
            "18443",
            "--internal-website-docs-root",
            "custom-docs",
            "--internal-website-skills-dir",
            "custom-doc-skills",
            "--non-interactive",
        ]
    )

    template = OnboardingTemplate(
        team_id="team_default",
        manager_storage_backend="sqlite",
        manager_storage_sqlite_path="data/task_history.db",
        manager_storage_postgres_dsn=None,
        timezone="America/Chicago",
        model_provider="",
        model_id="",
        reasoning_effort="",
    )
    answers = _collect_onboarding_answers(args, template=template)

    assert answers["instance_name"] == "mybot"
    # Only the instance root is asked for; every folder is derived from it.
    assert "workspace_dir" not in answers
    assert answers["team_id"] == "team_default"
    assert answers["assistant_name"] == "Ava"
    assert answers["assistant_purpose"] == "Help with engineering operations."
    assert answers["user_role"] == "Platform engineer"
    assert (
        answers["user_context"]
        == "I run a small software team and prefer high-signal summaries."
    )
    assert answers["default_delivery_channel"] == "discord"
    assert answers["default_delivery_chat_id"] == "123456789012345678"
    assert answers["default_delivery_thread_id"] == "223344556677889900"
    assert answers["request_timeout_seconds"] == 3600
    assert answers["heartbeat_active_hours_enabled"] is True
    assert answers["heartbeat_active_start"] == 9
    assert answers["heartbeat_active_end"] == 17
    assert answers["model_provider"] == "openai"
    assert answers["model_id"] == "gpt-5"
    assert answers["reasoning_effort"] == "high"
    assert answers["reasoning_summary"] == "detailed"
    assert answers["litellm_api_mode"] == "responses"
    assert not set(REMOVED_TOOL_POLICY_KEYS) & answers.keys()
    assert answers["public_origin"] == "https://public.example.test"
    assert answers["secure_tool_broker_public_port"] == 9443
    assert answers["secure_tool_broker_bind_host"] == "127.0.0.1"
    assert answers["secure_tool_broker_bind_port"] == 8766
    assert answers["secure_tool_broker_credentials_dir"] == "broker_credentials"
    assert answers["internal_website_bind_host"] == "127.0.0.1"
    assert answers["internal_website_bind_port"] == 18501
    assert answers["internal_website_public_port"] == 18443
    assert answers["internal_website_docs_root"] == "custom-docs"
    assert answers["internal_website_skills_dir"] == "custom-doc-skills"
    assert "internal_website_sites_dir" not in answers
    assert answers["discord_enabled"] is True
    assert answers["discord_allowed_user_ids"] == ["999"]
    assert answers["discord_allowed_channel_ids"] == ["123456789012345678"]
    assert answers["discord_group_policy"] == "open"
    assert answers["discord_thread_scoped_sessions"] is True
    assert answers["discord_command_guild_ids"] == ["777777777777777777"]
    assert answers["telegram_enabled"] is True
    assert answers["telegram_allowed_user_ids"] == ["111"]
    assert answers["telegram_allowed_chat_ids"] == ["-222"]
    assert answers["telegram_group_policy"] == "open"
    assert answers["telegram_manager_offline_policy"] == "notify"
    assert answers["telegram_topic_scoped_sessions"] is True
    assert answers["signal_enabled"] is True
    assert answers["signal_base_url"] == "http://signal.example.test:8080"
    assert answers["signal_account"] == "+15550001111"
    assert answers["signal_allowed_sender_ids"] == ["+15551234567"]
    assert answers["signal_allowed_group_ids"] == ["group.signal"]
    assert answers["signal_group_policy"] == "open"
    assert answers["signal_mention_markers"] == ["@Ava"]
    assert answers["signal_poll_timeout_seconds"] == 3.5
    assert answers["signal_max_messages_per_poll"] == 25
    assert answers["signal_drop_pending_messages_on_startup"] is True
    assert answers["photon_enabled"] is True
    assert answers["photon_project_id"] == "photon-project"
    assert answers["photon_project_secret"] == "photon-secret"
    assert answers["photon_allowed_sender_ids"] == ["imessage:+15551234567"]
    assert answers["photon_allowed_group_ids"] == ["photon-group"]
    assert answers["photon_group_policy"] == "open"
    assert answers["photon_mention_markers"] == ["@Ava"]
    assert answers["whatsapp_enabled"] is True
    assert answers["whatsapp_allowed_sender_ids"] == ["+15551234567"]
    assert answers["whatsapp_webhook_host"] == "127.0.0.1"
    assert answers["whatsapp_webhook_port"] == 18080
    assert answers["whatsapp_webhook_path"] == "/hooks/whatsapp"
    assert answers["whatsapp_public_base_url"] == "https://bots.example.com"
    assert answers["whatsapp_validate_twilio_signature"] is True


def test_collect_onboarding_answers_non_interactive_uses_template_defaults(
    monkeypatch, tmp_path
):
    expected_instance_root = str((tmp_path / "instances" / "mybot").resolve())
    parser = build_parser()
    args = parser.parse_args(
        [
            "onboard",
            "--instance-name",
            "mybot",
            "--non-interactive",
        ]
    )

    template = OnboardingTemplate(
        team_id="team_from_template",
        manager_storage_backend="sqlite",
        manager_storage_sqlite_path="data/task_history.db",
        manager_storage_postgres_dsn=None,
        timezone="America/Phoenix",
        model_provider="openai",
        model_id="gpt-5.4",
        reasoning_effort="medium",
        delivery=DeliverySettings.model_validate(
            {
                "default_target": {
                    "channel_name": "telegram",
                    "chat_id": "-100999",
                    "thread_id": "44",
                },
                "quiet_hours": {
                    "enabled": False,
                    "start_hour_local": 21,
                    "end_hour_local": 7,
                },
                "digest_delivery_hour_local": 9,
            }
        ),
    )
    monkeypatch.chdir(tmp_path)
    answers = _collect_onboarding_answers(args, template=template)

    assert answers["instance_root"] == expected_instance_root
    assert "workspace_dir" not in answers
    assert answers["team_id"] == "team_from_template"
    assert answers["timezone"] == "America/Phoenix"
    assert answers["model_provider"] == "openai"
    assert answers["model_id"] == "gpt-5.4"
    assert answers["reasoning_effort"] == "medium"
    assert answers["default_delivery_channel"] == "telegram"
    assert answers["default_delivery_chat_id"] == "-100999"
    assert answers["default_delivery_thread_id"] == "44"
    assert answers["request_timeout_seconds"] == 3600
    assert answers["quiet_enabled"] is False
    assert answers["quiet_start"] == 21
    assert answers["quiet_end"] == 7
    assert answers["digest_hour"] == 9
    assert answers["public_origin"] == "http://127.0.0.1"
    assert answers["secure_tool_broker_public_port"] == 18600
    assert answers["secure_tool_broker_bind_host"] == "0.0.0.0"
    assert answers["secure_tool_broker_bind_port"] == 18600
    assert answers["secure_tool_broker_credentials_dir"] == "broker_credentials"
    assert answers["internal_website_bind_host"] == "0.0.0.0"
    assert answers["internal_website_bind_port"] == 18090
    assert answers["internal_website_public_port"] == 18090
    assert answers["internal_website_docs_root"] is None
    assert answers["internal_website_skills_dir"] is None
    assert "internal_website_sites_dir" not in answers
    assert not set(REMOVED_TOOL_POLICY_KEYS) & answers.keys()
    assert answers["discord_enabled"] is False
    assert answers["discord_group_policy"] == "mention"
    assert answers["discord_command_guild_ids"] == []
    assert answers["telegram_enabled"] is False
    assert answers["telegram_manager_offline_policy"] == "pause_telegram_polling"
    assert answers["signal_enabled"] is False
    assert answers["signal_base_url"] == "http://127.0.0.1:8080"
    assert answers["signal_account"] == ""
    assert answers["signal_allowed_sender_ids"] == []
    assert answers["signal_allowed_group_ids"] == []
    assert answers["signal_group_policy"] == "mention"
    assert answers["signal_mention_markers"] == []
    assert answers["signal_poll_timeout_seconds"] == 5.0
    assert answers["signal_max_messages_per_poll"] == 10
    assert answers["signal_drop_pending_messages_on_startup"] is False
    assert answers["photon_enabled"] is False
    assert answers["photon_project_id"] == ""
    assert answers["photon_project_secret"] == ""
    assert answers["photon_allowed_sender_ids"] == []
    assert answers["photon_allowed_group_ids"] == []
    assert answers["photon_group_policy"] == "mention"
    assert answers["photon_mention_markers"] == []
    assert answers["whatsapp_enabled"] is False
    assert answers["whatsapp_webhook_host"] == "0.0.0.0"
    assert answers["whatsapp_webhook_port"] == 8080
    assert answers["whatsapp_webhook_path"] == "/webhooks/whatsapp"
    assert answers["whatsapp_validate_twilio_signature"] is True


def test_resolve_onboarding_seed_interactive_prompts_for_base_config_and_env_sources(
    monkeypatch,
    tmp_path,
):
    original_path = tmp_path / "original_config.yaml"
    original_path.write_text(
        "schema_version: 1\n"
        "agent:\n"
        "  default_team_id: team_original\n"
        "llm:\n"
        "  provider: openai\n"
        "  model_id: gpt-4o\n",
        encoding="utf-8",
    )
    replacement_path = tmp_path / "replacement_config.yaml"
    replacement_path.write_text(
        "schema_version: 1\n"
        "agent:\n"
        "  default_team_id: team_replacement\n"
        "  default_request_timezone: America/Phoenix\n"
        "llm:\n"
        "  provider: anthropic\n"
        "  model_id: claude-sonnet-4\n"
        "  reasoning_effort: high\n",
        encoding="utf-8",
    )
    source_env_path = tmp_path / "source.env"
    source_env_path.write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(["onboard", "--base-config", str(original_path)])
    prompts: list[str] = []
    responses = iter([str(replacement_path), str(source_env_path)])

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", fake_input)
    seed = _resolve_onboarding_seed(args)

    assert prompts == [
        f"Existing config.yaml to copy from [{original_path}] (press Enter to keep, '-' to skip): ",
        "Existing .env file to copy for runtime launch (optional, press Enter to skip): ",
    ]
    assert seed.template.team_id == "team_replacement"
    assert seed.template.timezone == "America/Phoenix"
    assert seed.template.model_provider == "anthropic"
    assert seed.template.model_id == "claude-sonnet-4"
    assert seed.template.reasoning_effort == "high"
    assert seed.source_env_file_path == str(source_env_path.resolve())


def test_load_base_onboarding_template_uses_storage_backend_defaults(
    monkeypatch, tmp_path
):
    _clear_onboarding_runtime_env(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "schema_version: 1\n"
        "agent:\n"
        "  default_team_id: team_test\n"
        "storage:\n"
        "  backend: postgres\n"
        "  postgres:\n"
        "    dsn: postgresql://team:secret@db/team_agents\n",
        encoding="utf-8",
    )

    template = load_base_onboarding_template(str(config_path))

    assert template.team_id == "team_test"
    assert template.manager_storage_backend == "postgres"
    assert (
        template.manager_storage_postgres_dsn
        == "postgresql://team:secret@db/team_agents"
    )
    assert template.manager_storage_sqlite_path == "dbs/task_history.db"


def test_load_base_onboarding_template_copies_delivery_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "schema_version: 1\n"
        "agent:\n"
        "  default_team_id: team_test\n"
        "delivery:\n"
        "  quiet_hours:\n"
        "    enabled: false\n"
        "    start_hour_local: 21\n"
        "    end_hour_local: 7\n"
        "  digest_delivery_hour_local: 9\n"
        "  summarize_deferred: false\n"
        "  notification_routing:\n"
        "    mode: default_target\n"
        "  urgent_keywords: [urgent, now]\n"
        "  default_target:\n"
        "    channel_name: telegram\n"
        "    chat_id: -100123\n"
        "    thread_id: 42\n",
        encoding="utf-8",
    )

    template = load_base_onboarding_template(str(config_path))

    assert template.delivery.default_target.channel_name == "telegram"
    assert template.delivery.default_target.chat_id == "-100123"
    assert template.delivery.default_target.thread_id == "42"
    assert template.delivery.quiet_hours.enabled is False
    assert template.delivery.quiet_hours.start_hour_local == 21
    assert template.delivery.quiet_hours.end_hour_local == 7
    assert template.delivery.digest_delivery_hour_local == 9
    assert template.delivery.summarize_deferred is False
    assert template.delivery.notification_routing.mode == "default_target"
    assert template.delivery.urgent_keywords == ["urgent", "now"]


def test_load_base_onboarding_template_copies_activity_collector_non_secret_defaults(
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "schema_version: 1\n"
        "agent:\n"
        "  default_team_id: team_test\n"
        "activity_collector:\n"
        "  base_url: http://127.0.0.1:8765\n"
        "  connect_timeout_seconds: 3.5\n"
        "  request_timeout_seconds: 9.0\n"
        "  batch_max_events: 40\n"
        "  flush_interval_seconds: 2.5\n"
        "  max_buffered_events: 600\n"
        "  drop_when_unavailable: false\n"
        "  bind_host: 0.0.0.0\n"
        "  bind_port: 9876\n"
        "  user_url_path: activity\n"
        "  admin_url_path: ops\n"
        "  ingest_token_env_var: CUSTOM_ACTIVITY_INGEST_TOKEN\n"
        "  user_token: should-not-copy\n"
        "  admin_token: should-not-copy\n"
        "  ingest_token: should-not-copy\n"
        "  storage:\n"
        "    backend: sqlite\n"
        "    sqlite:\n"
        "      path: collector.db\n",
        encoding="utf-8",
    )

    template = load_base_onboarding_template(str(config_path))

    collector = template.activity_collector
    assert collector.base_url == "http://127.0.0.1:8765"
    assert collector.connect_timeout_seconds == 3.5
    assert collector.request_timeout_seconds == 9.0
    assert collector.batch_max_events == 40
    assert collector.flush_interval_seconds == 2.5
    assert collector.max_buffered_events == 600
    assert collector.drop_when_unavailable is False
    assert collector.bind_host == "0.0.0.0"
    assert collector.bind_port == 9876
    assert collector.user_url_path == "/activity"
    assert collector.admin_url_path == "/ops"
    assert collector.ingest_token_env_var == "CUSTOM_ACTIVITY_INGEST_TOKEN"
    assert collector.storage.sqlite.path == "collector.db"
    assert not hasattr(collector, "user_token")
    assert not hasattr(collector, "admin_token")
    assert not hasattr(collector, "ingest_token")


def test_load_base_onboarding_template_copies_voice_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "schema_version: 1\n"
        "agent:\n"
        "  default_team_id: team_test\n"
        "voice:\n"
        "  enabled: true\n"
        "  input_enabled: true\n"
        "  output_enabled: false\n"
        "  provider: grok\n"
        "  api_base: https://voice.example.test/v1\n"
        "  stt_language: en\n"
        "  stt_format_text: false\n"
        "  tts_voice: ara\n"
        "  tts_language: en\n"
        "  tts_output_codec: wav\n"
        "  tts_sample_rate: 44100\n"
        "  tts_bit_rate: 192000\n"
        "  connect_timeout_seconds: 4.0\n"
        "  request_timeout_seconds: 45.0\n",
        encoding="utf-8",
    )

    template = load_base_onboarding_template(str(config_path))

    assert template.voice.enabled is True
    assert template.voice.input_enabled is True
    assert template.voice.output_enabled is False
    assert template.voice.provider == "grok"
    assert template.voice.api_base == "https://voice.example.test/v1"
    assert template.voice.stt_language == "en"
    assert template.voice.stt_format_text is False
    assert template.voice.tts_voice == "ara"
    assert template.voice.tts_language == "en"
    assert template.voice.tts_output_codec == "wav"
    assert template.voice.tts_sample_rate == 44100
    assert template.voice.tts_bit_rate == 192000
    assert template.voice.connect_timeout_seconds == 4.0
    assert template.voice.request_timeout_seconds == 45.0


def test_collect_onboarding_answers_interactive_shows_template_defaults(
    monkeypatch, tmp_path
):
    parser = build_parser()
    args = parser.parse_args(["onboard", "--instance-name", "mybot"])
    template = OnboardingTemplate(
        team_id="team_existing",
        manager_storage_backend="sqlite",
        manager_storage_sqlite_path="data/task_history.db",
        manager_storage_postgres_dsn=None,
        timezone="America/New_York",
        model_provider="anthropic",
        model_id="claude-sonnet-4",
        reasoning_effort="medium",
    )
    monkeypatch.chdir(tmp_path)
    prompts: list[str] = []
    responses = iter([""] * 80)
    expected_instance_root = str((tmp_path / "instances" / "mybot").resolve())

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", fake_input)
    answers = _collect_onboarding_answers(args, template=template)

    assert "Instance name [mybot]: " in prompts
    assert "User timezone [America/New_York]: " in prompts
    assert f"Instance root directory [{expected_instance_root}]: " in prompts
    # Folders are derived from the instance root, so onboarding no longer asks
    # for a workspace or generated-sites directory.
    assert not any(prompt.startswith("Workspace directory") for prompt in prompts)
    assert not any("generated sites directory" in prompt for prompt in prompts)
    assert "Team id [team_existing]: " in prompts
    assert "Default delivery chat id: " in prompts
    assert "Default delivery thread/topic id: " in prompts
    assert "Gateway request timeout (seconds) [3600]: " in prompts
    assert "Preferred model provider [anthropic]: " in prompts
    assert "Preferred model id [claude-sonnet-4]: " in prompts
    assert "Preferred reasoning effort [medium]: " in prompts
    # Tools and folders are given with grants, so onboarding no longer asks
    # about a workspace restriction, web search or shell commands.
    assert not any(
        prompt.startswith(("Restrict tools", "Allow web search", "Allow shell"))
        for prompt in prompts
    )
    assert answers["instance_name"] == "mybot"
    assert answers["instance_root"] == expected_instance_root
    assert "workspace_dir" not in answers
    assert answers["timezone"] == "America/New_York"
    assert answers["team_id"] == "team_existing"
    assert answers["default_delivery_chat_id"] == ""
    assert answers["default_delivery_thread_id"] == ""
    assert answers["request_timeout_seconds"] == 3600
    assert answers["model_provider"] == "anthropic"
    assert answers["model_id"] == "claude-sonnet-4"
    assert answers["reasoning_effort"] == "medium"
    assert not set(REMOVED_TOOL_POLICY_KEYS) & answers.keys()


def test_collect_onboarding_answers_interactive_prompts_for_signal_settings(
    monkeypatch, tmp_path
):
    parser = build_parser()
    args = parser.parse_args(["onboard", "--instance-name", "mybot", "--enable-signal"])
    template = OnboardingTemplate(
        team_id="team_existing",
        manager_storage_backend="sqlite",
        manager_storage_sqlite_path="data/task_history.db",
        manager_storage_postgres_dsn=None,
        timezone="America/New_York",
        model_provider="anthropic",
        model_id="claude-sonnet-4",
        reasoning_effort="medium",
    )
    monkeypatch.chdir(tmp_path)
    prompts: list[str] = []
    responses = iter([""] * 80)

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", fake_input)
    answers = _collect_onboarding_answers(args, template=template)

    assert "Signal REST API base URL [http://127.0.0.1:8080]: " in prompts
    assert "Signal account/number: " in prompts
    assert (
        "Allowed Signal direct sender ids or phone numbers (comma-separated, blank for any): "
        in prompts
    )
    assert (
        "Allowed Signal group ids (comma-separated, blank to disable groups): "
        in prompts
    )
    assert "Signal group policy (open or mention) [mention]: " in prompts
    assert "Signal group mention markers (comma-separated) [@Lisa]: " in prompts
    assert "Signal receive long-poll timeout seconds [5]: " in prompts
    assert "Signal max messages per poll [10]: " in prompts
    assert "Drop pending Signal messages on startup [y/N]: " in prompts
    assert answers["signal_enabled"] is True


def test_collect_onboarding_answers_interactive_prompts_for_photon_settings(
    monkeypatch, tmp_path
):
    parser = build_parser()
    args = parser.parse_args(["onboard", "--instance-name", "mybot", "--enable-photon"])
    template = OnboardingTemplate(
        team_id="team_existing",
        manager_storage_backend="sqlite",
        manager_storage_sqlite_path="data/task_history.db",
        manager_storage_postgres_dsn=None,
        timezone="America/New_York",
        model_provider="anthropic",
        model_id="claude-sonnet-4",
        reasoning_effort="medium",
    )
    monkeypatch.chdir(tmp_path)
    prompts: list[str] = []
    responses = iter([""] * 80)

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", fake_input)
    answers = _collect_onboarding_answers(args, template=template)

    assert "Photon PHOTON_PROJECT_ID: " in prompts
    assert "Photon PHOTON_PROJECT_SECRET: " in prompts
    assert (
        "Allowed Photon direct sender ids (comma-separated, blank for any): " in prompts
    )
    assert (
        "Allowed Photon group ids (comma-separated, blank to disable groups): "
        in prompts
    )
    assert "Photon group policy (open or mention) [mention]: " in prompts
    assert "Photon group mention markers (comma-separated) [@Lisa]: " in prompts
    assert answers["photon_enabled"] is True


def test_merge_photon_onboarding_env_writes_required_spectrum_values(tmp_path):
    env_path = tmp_path / "team.env"

    _merge_photon_onboarding_env(
        env_path,
        {
            "photon_project_id": "project-id",
            "photon_project_secret": "project-secret",
        },
    )

    env_text = env_path.read_text(encoding="utf-8")
    assert "PHOTON_PROJECT_ID=project-id" in env_text
    assert "PHOTON_PROJECT_SECRET=project-secret" in env_text


def test_collect_onboarding_answers_interactive_prompts_for_whatsapp_settings(
    monkeypatch, tmp_path
):
    parser = build_parser()
    args = parser.parse_args(
        ["onboard", "--instance-name", "mybot", "--enable-whatsapp"]
    )
    template = OnboardingTemplate(
        team_id="team_existing",
        manager_storage_backend="sqlite",
        manager_storage_sqlite_path="data/task_history.db",
        manager_storage_postgres_dsn=None,
        timezone="America/New_York",
        model_provider="anthropic",
        model_id="claude-sonnet-4",
        reasoning_effort="medium",
    )
    monkeypatch.chdir(tmp_path)
    prompts: list[str] = []
    responses = iter([""] * 60)

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", fake_input)
    answers = _collect_onboarding_answers(args, template=template)

    assert (
        "Allowed WhatsApp sender ids or phone numbers (comma-separated, blank for any): "
        in prompts
    )
    assert "WhatsApp webhook host/interface [0.0.0.0]: " in prompts
    assert "WhatsApp webhook port [8080]: " in prompts
    assert "WhatsApp webhook path [/webhooks/whatsapp]: " in prompts
    assert "WhatsApp public webhook base URL (blank to skip): " in prompts
    assert "Validate Twilio webhook signatures [Y/n]: " in prompts
    assert answers["whatsapp_enabled"] is True


@pytest.mark.parametrize(
    "removed_flag",
    [
        "--restrict-to-workspace",
        "--no-restrict-to-workspace",
        "--disable-web-search",
        "--disable-shell",
    ],
)
def test_onboard_refuses_removed_tool_switch_flags(removed_flag, capsys):
    # The tool_policy switches were removed: an administrator registers folders
    # outside the workspace in the access store (/access folders add) and every
    # tool is given with grants, so these flags no longer exist.
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["onboard", "--instance-name", "mybot", removed_flag])

    assert removed_flag in capsys.readouterr().err


def test_load_cli_env_file_allows_onboarding_template_to_use_nats_env_overrides(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "schema_version: 1\ntransport: {}\nagent: {}\nllm: {}\nworkspace: {}\n",
        encoding="utf-8",
    )
    user_secret_path = tmp_path / "nats_user.txt"
    password_secret_path = tmp_path / "nats_password.txt"
    user_secret_path.write_text("nova\n", encoding="utf-8")
    password_secret_path.write_text("secret\n", encoding="utf-8")
    env_path = tmp_path / "debug.env"
    env_path.write_text(
        "NATS_SERVERS=nats://nats-a.example.com:4222,nats://nats-b.example.com:4222\n"
        f"NATS_USER_FILE={user_secret_path}\n"
        f"NATS_PASSWORD_FILE={password_secret_path}\n",
        encoding="utf-8",
    )

    nats_env_keys = [
        "NATS_URL",
        "NATS_SERVERS",
        "NATS_USER",
        "NATS_USER_FILE",
        "NATS_PASSWORD",
        "NATS_PASSWORD_FILE",
    ]
    original_env = {key: os.environ.get(key) for key in nats_env_keys}
    for key in nats_env_keys:
        monkeypatch.delenv(key, raising=False)

    try:
        _load_cli_env_file(str(env_path))
        config = GatewayInstanceConfig.create(
            instance_name="test",
            instance_root=str(tmp_path / "instance"),
            team_id="team_test",
            timezone="America/Chicago",
        )
        loaded = _load_gateway_instance_config(
            str(config.save()), env_path=str(env_path)
        )

        assert loaded.transport.nats_servers == [
            "nats://nats-a.example.com:4222",
            "nats://nats-b.example.com:4222",
        ]
        assert loaded.transport.nats_user == "nova"
        assert loaded.transport.nats_password == "secret"
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_load_base_onboarding_template_copies_llm_runtime_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "schema_version: 1\n"
        "transport: {}\n"
        "agent: {}\n"
        "workspace: {}\n"
        "llm:\n"
        "  provider: litellm\n"
        "  model_id: gemini-3.1-flash-lite-preview\n"
        "  temperature: 0.0\n"
        "  context_window_tokens: 200000\n"
        "  max_output_tokens: 4096\n"
        "  supports_image_input: true\n"
        "  supports_audio_input: false\n"
        "  model_capabilities:\n"
        "    litellm:\n"
        "      gemini-3.1-flash-lite-preview:\n"
        "        supports_image_input: true\n"
        "        supports_audio_input: false\n"
        "  litellm_api_base: https://litellm.example.test\n"
        "  compaction_model_provider: litellm\n"
        "  compaction_model_id: compact-model\n"
        "  failover:\n"
        "    enabled: true\n"
        "    switch_for:\n"
        "      min_turns: 5\n"
        "    fallbacks:\n"
        "      - provider: openai\n"
        "        model_id: fallback-model\n"
        "embedding:\n"
        "  provider: openai_compatible\n"
        "  model_id: pplx-embed-v1-0.6b\n"
        "  api_base: https://litellm.example.test/v1\n"
        "  timeout_seconds: 10\n",
        encoding="utf-8",
    )

    template = load_base_onboarding_template(str(config_path))

    assert template.model_provider == "litellm"
    assert template.model_id == "gemini-3.1-flash-lite-preview"
    assert template.llm_temperature == 0.0
    assert template.llm_context_window_tokens == 200000
    assert template.llm_max_output_tokens == 4096
    assert template.supports_image_input is True
    assert template.supports_audio_input is False
    assert template.model_capabilities == {
        "litellm": {
            "gemini-3.1-flash-lite-preview": {
                "supports_image_input": True,
                "supports_audio_input": False,
            }
        }
    }
    assert template.litellm_api_base == "https://litellm.example.test"
    assert template.compaction_model_provider == "litellm"
    assert template.compaction_model_id == "compact-model"
    assert template.llm_failover["enabled"] is True
    assert template.llm_failover["switch_for"]["min_turns"] == 5
    assert template.llm_failover["fallbacks"] == [
        {"provider": "openai", "model_id": "fallback-model"}
    ]
    assert template.embedding_provider == "openai_compatible"
    assert template.embedding_model_id == "pplx-embed-v1-0.6b"
    assert template.embedding_api_base == "https://litellm.example.test/v1"
    assert template.embedding_timeout_seconds == 10


def test_load_base_onboarding_template_copies_agent_runtime_defaults_and_overrides(
    tmp_path,
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "schema_version: 1\n"
        "agent:\n"
        "  memory_graph_enabled: true\n"
        "  max_queued_top_level_tasks: 22\n"
        "  task_queue_warning_threshold: 9\n"
        "  subtask_timeout_seconds: 2400\n"
        "  subtask_watchdog_interval_seconds: 20\n"
        "  subtask_status_probe_timeout_seconds: 2.5\n"
        "  restart_executing_task_grace_seconds: 300\n"
        "  same_role_runtime_takeover_enabled: false\n"
        "  max_tool_calls_per_task: 30\n"
        "  max_tool_result_tokens_per_call: 3000\n"
        "  runtime_budget_policy:\n"
        "    mode: user_guided\n"
        "    max_user_guided_pauses_per_task: 2\n"
        "  inner_loop_context:\n"
        "    max_total_tokens: 12000\n"
        "  context_engine:\n"
        "    reserved_output_tokens: 2048\n"
        "  conversation_compactor:\n"
        "    type: noop\n"
        "  conversation_summary_generator_type: llm_summarizing\n"
        "  deferred_message_warning_threshold: 13\n"
        "  tool_blocklist:\n"
        "    - shell\n"
        "  prompt_skill_description_exclusions:\n"
        "    - slides\n"
        "  prompt_skill_description_limit: 35\n"
        "  structured_output_failure_threshold: 5\n"
        "agent_types:\n"
        "  WebResearchAgent:\n"
        "    agent:\n"
        "      max_tool_calls_per_task: 18\n"
        "      max_tool_result_tokens_per_call: 7500\n"
        "      prompt_skill_description_limit: 11\n",
        encoding="utf-8",
    )

    template = load_base_onboarding_template(str(config_path))

    assert template.agent.memory_graph_enabled is True
    assert template.agent.max_queued_top_level_tasks == 22
    assert template.agent.task_queue_warning_threshold == 9
    assert template.agent.subtask_timeout_seconds == 2400
    assert template.agent.subtask_watchdog_interval_seconds == 20
    assert template.agent.subtask_status_probe_timeout_seconds == 2.5
    assert template.agent.restart_executing_task_grace_seconds == 300
    assert template.agent.same_role_runtime_takeover_enabled is False
    assert template.agent.max_tool_calls_per_task == 30
    assert template.agent.max_tool_result_tokens_per_call == 3000
    assert template.agent.runtime_budget_policy["mode"] == "user_guided"
    assert template.agent.runtime_budget_policy["max_user_guided_pauses_per_task"] == 2
    assert template.agent.inner_loop_context["max_total_tokens"] == 12000
    assert template.agent.context_engine["reserved_output_tokens"] == 2048
    assert template.agent.conversation_compactor["type"] == "noop"
    assert template.agent.conversation_summary_generator_type == "llm_summarizing"
    assert template.agent.deferred_message_warning_threshold == 13
    assert template.agent.tool_blocklist == ["shell"]
    assert template.agent.prompt_skill_description_exclusions == ["slides"]
    assert template.agent.prompt_skill_description_limit == 35
    assert template.agent.structured_output_failure_threshold == 5
    assert template.agent_types["WebResearchAgent"].agent.max_tool_calls_per_task == 18
    assert (
        template.agent_types["WebResearchAgent"].agent.max_tool_result_tokens_per_call
        == 7500
    )
    assert (
        template.agent_types["WebResearchAgent"].agent.prompt_skill_description_limit
        == 11
    )


def test_onboard_copies_embedding_and_voice_defaults_into_instance_config(
    monkeypatch, tmp_path
):
    parser = build_parser()
    args = parser.parse_args(
        ["onboard", "--instance-name", "mybot", "--non-interactive"]
    )
    template = OnboardingTemplate(
        team_id="team_default",
        manager_storage_backend="sqlite",
        manager_storage_sqlite_path="data/task_history.db",
        manager_storage_postgres_dsn=None,
        timezone="America/Chicago",
        model_provider="litellm",
        model_id="gemini-3.1-flash-lite-preview",
        reasoning_effort="",
        embedding_provider="openai_compatible",
        embedding_model_id="pplx-embed-v1-0.6b",
        embedding_api_base="https://litellm.example.test/v1",
        embedding_dimensions=None,
        embedding_timeout_seconds=10.0,
        embedding_organization=None,
        activity_collector=ActivityCollectorSettings(
            base_url="http://127.0.0.1:8765",
            bind_host="0.0.0.0",
            bind_port=9876,
        ),
        voice=VoiceSettings(
            enabled=True,
            input_enabled=True,
            output_enabled=False,
            provider="grok",
            api_base="https://voice.example.test/v1",
            stt_language="en",
            stt_format_text=False,
            tts_voice="ara",
            tts_language="en",
            tts_output_codec="wav",
            tts_sample_rate=44100,
            tts_bit_rate=192000,
            connect_timeout_seconds=4.0,
            request_timeout_seconds=45.0,
        ),
    )
    template.agent.memory_graph_enabled = True
    template.agent.subtask_timeout_seconds = 2400
    template.agent.max_tool_calls_per_task = 30
    template.agent.max_tool_result_tokens_per_call = 3000
    template.agent_types["WebResearchAgent"] = AgentTypeRuntimeSettings.model_validate(
        {
            "agent": {
                "max_tool_calls_per_task": 18,
                "max_tool_result_tokens_per_call": 7500,
            }
        }
    )
    monkeypatch.chdir(tmp_path)

    captured: dict[str, GatewayInstanceConfig] = {}

    def _fake_bootstrap_instance(
        config: GatewayInstanceConfig,
        source_env_file_path=None,
        assistant_purpose=None,
        assistant_response_style=None,
        assistant_autonomy=None,
        expected_config_revision=None,
    ):
        captured["config"] = config
        captured["assistant_purpose"] = assistant_purpose
        captured["assistant_response_style"] = assistant_response_style
        captured["assistant_autonomy"] = assistant_autonomy
        return []

    monkeypatch.setattr(
        "assistant_cli._resolve_onboarding_seed",
        lambda _args: GatewayOnboardingSeed(
            template=template, source_env_file_path=None
        ),
    )
    monkeypatch.setattr("assistant_cli.bootstrap_instance", _fake_bootstrap_instance)

    _onboard(args)

    generated = captured["config"]
    assert generated.embedding.provider == "openai_compatible"
    assert generated.embedding.model_id == "pplx-embed-v1-0.6b"
    assert generated.embedding.api_base == "https://litellm.example.test/v1"
    assert generated.embedding.dimensions is None
    assert generated.embedding.timeout_seconds == 10.0
    assert generated.activity_collector.base_url == "http://127.0.0.1:8765"
    assert generated.activity_collector.bind_host == "0.0.0.0"
    assert generated.activity_collector.bind_port == 9876
    assert generated.voice.enabled is True
    assert generated.voice.input_enabled is True
    assert generated.voice.output_enabled is False
    assert generated.voice.provider == "grok"
    assert generated.voice.api_base == "https://voice.example.test/v1"
    assert generated.voice.stt_format_text is False
    assert generated.voice.tts_voice == "ara"
    assert generated.voice.tts_output_codec == "wav"
    assert generated.voice.tts_sample_rate == 44100
    assert generated.voice.tts_bit_rate == 192000
    assert generated.agent.memory_graph_enabled is True
    assert generated.agent.subtask_timeout_seconds == 2400
    assert generated.agent.max_tool_calls_per_task == 30
    assert generated.agent.max_tool_result_tokens_per_call == 3000
    assert generated.agent_types["WebResearchAgent"].agent.max_tool_calls_per_task == 18
    assert (
        generated.agent_types["WebResearchAgent"].agent.max_tool_result_tokens_per_call
        == 7500
    )
    assert captured["assistant_purpose"] is None


def test_onboard_generated_instance_config_omits_transport_credentials_and_postgres_dsn(
    monkeypatch, tmp_path
):
    parser = build_parser()
    args = parser.parse_args(
        [
            "onboard",
            "--instance-name",
            "mybot",
            "--instance-root",
            str(tmp_path / "instance"),
            "--non-interactive",
        ]
    )
    template = OnboardingTemplate(
        team_id="team_default",
        manager_storage_backend="postgres",
        manager_storage_sqlite_path=None,
        manager_storage_postgres_dsn="postgresql://team:topsecret@db.internal:5432/team_runtime",
        timezone="America/Chicago",
        model_provider="litellm",
        model_id="gemini-3.1-flash-lite-preview",
        reasoning_effort="",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "assistant_cli._resolve_onboarding_seed",
        lambda _args: GatewayOnboardingSeed(
            template=template, source_env_file_path=None
        ),
    )

    _onboard(args)

    config_path = tmp_path / "instance" / "instance_config.yaml"
    saved_text = config_path.read_text(encoding="utf-8")
    assert "nats_password:" not in saved_text
    assert "user_token:" not in saved_text
    assert "admin_token:" not in saved_text
    assert "ingest_token:" not in saved_text
    assert "postgresql://team:topsecret@db.internal:5432/team_runtime" not in saved_text
    assert "dsn:" not in saved_text
    env_text = (tmp_path / "instance" / "team.env").read_text(encoding="utf-8")
    assert "NATS_USER=team_mybot" in env_text
    assert "NATS_PASSWORD=nats_" in env_text
    assert (
        "POSTGRES_DSN=postgresql://team:topsecret@db.internal:5432/team_runtime"
        in env_text
    )


def test_onboard_generates_broker_owned_gog_keyring_password(monkeypatch, tmp_path):
    _clear_onboarding_runtime_env(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        [
            "onboard",
            "--instance-name",
            "mybot",
            "--instance-root",
            str(tmp_path / "instance"),
            "--security-layer-mode",
            "openshell",
            "--non-interactive",
        ]
    )
    monkeypatch.chdir(tmp_path)

    _onboard(args)

    env_text = (tmp_path / "instance" / "team.env").read_text(encoding="utf-8")
    assert "SECURE_TOOL_BROKER_TOKEN=stb_" in env_text
    assert "GOG_KEYRING_PASSWORD=gog_" in env_text
    assert "SEALBOX_TOKEN=" not in env_text
    assert "SEALBOX_PRIVATE_KEY" not in env_text
    sealbox_dir = tmp_path / "instance" / "broker_credentials" / "sealbox"
    root_token_path = sealbox_dir / "root_token"
    assert root_token_path.read_text(encoding="utf-8").strip()
    assert oct(root_token_path.stat().st_mode & 0o777) == "0o600"
    assert not (sealbox_dir / "tenants").exists()


def test_onboard_rotates_only_managed_sealbox_root_token_before_user_provisioning(
    monkeypatch, tmp_path
):
    _clear_onboarding_runtime_env(monkeypatch)
    parser = build_parser()
    argv = [
        "onboard",
        "--instance-name",
        "mybot",
        "--instance-root",
        str(tmp_path / "instance"),
        "--security-layer-mode",
        "broker",
        "--non-interactive",
    ]
    monkeypatch.chdir(tmp_path)

    _onboard(parser.parse_args(argv))
    sealbox_dir = tmp_path / "instance" / "broker_credentials" / "sealbox"
    root_token_path = sealbox_dir / "root_token"
    first_token = root_token_path.read_text(encoding="utf-8")

    _onboard(parser.parse_args([*argv, "--rotate-internal-credentials"]))

    assert root_token_path.read_text(encoding="utf-8") != first_token
    assert not (sealbox_dir / "tenants").exists()


def test_instance_config_refuses_removed_single_tenant_sealbox_key_files(tmp_path):
    config = GatewayInstanceConfig.create(
        instance_name="mybot",
        instance_root=str(tmp_path / "instance"),
        team_id="team_303",
        timezone="America/Chicago",
    )
    payload = config.to_yaml_dict()
    payload.setdefault("security_layer", {})["sealbox"] = {
        "root_token_file": "mounted/sealbox-token",
        "legacy_public_key_file": "mounted/sealbox-public.pem",
        "legacy_private_key_file": "mounted/sealbox-private.pem",
    }
    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    config.config_path.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ValueError) as refused:
        GatewayInstanceConfig.load(config.config_path)

    message = str(refused.value)
    assert "security_layer.sealbox.legacy_private_key_file" in message
    assert "security_layer.sealbox.legacy_public_key_file" in message
    assert "has been removed" in message


def test_onboard_does_not_generate_nats_credentials_for_external_bootstrap_env(
    monkeypatch, tmp_path
):
    _clear_onboarding_runtime_env(monkeypatch)
    parser = build_parser()
    base_env = tmp_path / "bootstrap.env"
    base_env.write_text(
        "NATS_SERVERS=nats://nats.example.test:4222\n", encoding="utf-8"
    )
    args = parser.parse_args(
        [
            "onboard",
            "--instance-name",
            "mybot",
            "--instance-root",
            str(tmp_path / "instance"),
            "--base-env-file",
            str(base_env),
            "--non-interactive",
        ]
    )
    monkeypatch.chdir(tmp_path)

    _onboard(args)

    env_text = (tmp_path / "instance" / "team.env").read_text(encoding="utf-8")
    assert "NATS_SERVERS=nats://nats.example.test:4222" in env_text
    assert "NATS_USER=" not in env_text
    assert "NATS_PASSWORD=" not in env_text


def test_onboard_preserves_existing_internal_credentials_by_default(
    monkeypatch, tmp_path
):
    parser = build_parser()
    argv = [
        "onboard",
        "--instance-name",
        "mybot",
        "--instance-root",
        str(tmp_path / "instance"),
        "--non-interactive",
    ]
    template = OnboardingTemplate(
        team_id="team_default",
        manager_storage_backend="postgres",
        manager_storage_sqlite_path=None,
        manager_storage_postgres_dsn="postgresql://team:topsecret@db.internal:5432/team_runtime",
        timezone="America/Chicago",
        model_provider="litellm",
        model_id="gemini-3.1-flash-lite-preview",
        reasoning_effort="",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "assistant_cli._resolve_onboarding_seed",
        lambda _args: GatewayOnboardingSeed(
            template=template, source_env_file_path=None
        ),
    )

    _onboard(parser.parse_args(argv))
    env_path = tmp_path / "instance" / "team.env"
    first_env = env_path.read_text(encoding="utf-8")

    _onboard(parser.parse_args(argv))
    second_env = env_path.read_text(encoding="utf-8")

    assert second_env == first_env


def test_onboard_rerun_reconciles_existing_config_and_writes_report(
    monkeypatch, tmp_path, capsys
):
    parser = build_parser()
    instance_root = tmp_path / "instance"
    argv = [
        "onboard",
        "--instance-name",
        "mybot",
        "--instance-root",
        str(instance_root),
        "--non-interactive",
    ]
    template = OnboardingTemplate(
        team_id="team_default",
        manager_storage_backend="sqlite",
        manager_storage_sqlite_path="data/task_history.db",
        manager_storage_postgres_dsn=None,
        timezone="America/Chicago",
        model_provider="litellm",
        model_id="gemini-3.1-flash-lite-preview",
        reasoning_effort="",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "assistant_cli._resolve_onboarding_seed",
        lambda _args: GatewayOnboardingSeed(
            template=template, source_env_file_path=None
        ),
    )

    _onboard(parser.parse_args(argv))
    config_path = instance_root / "instance_config.yaml"
    config = GatewayInstanceConfig.load(config_path)
    config.gateway.request_timeout_seconds = 987.0
    config.delivery.default_target.chat_id = "operator-chat"
    config.launch.env_file_path = str((instance_root / "custom.env").resolve())
    config.save()
    raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw_config["stale_top_level"] = {"old_setting": True}
    raw_config["assistant"]["obsolete_setting"] = "remove-me"
    config_path.write_text(
        yaml.safe_dump(raw_config, sort_keys=False), encoding="utf-8"
    )

    _onboard(parser.parse_args(argv))

    updated = GatewayInstanceConfig.load(config_path)
    assert updated.gateway.request_timeout_seconds == 987.0
    assert updated.delivery.default_target.chat_id == "operator-chat"
    assert updated.launch.env_file_path == str((instance_root / "custom.env").resolve())
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "stale_top_level" not in saved
    assert "obsolete_setting" not in saved["assistant"]
    report_paths = sorted(
        (instance_root / "onboarding_reports").glob("onboarding_report_*.md")
    )
    assert report_paths
    report_text = report_paths[-1].read_text(encoding="utf-8")
    assert "Mode: `updated`" in report_text
    assert "`stale_top_level.old_setting`" in report_text
    assert "`assistant.obsolete_setting`" in report_text
    output = capsys.readouterr().out
    assert "Onboarding reconciliation report:" in output
    assert "Mode: updated" in output
    assert f"Detailed report: {report_paths[-1]}" in output


@pytest.mark.parametrize("environment_secrets", [False, True])
def test_onboard_rerun_refuses_a_configuration_changed_after_reconciliation_read(
    monkeypatch, tmp_path, environment_secrets
):
    import assistant_cli
    from config_file_updates import ConfigFileConflict

    _clear_onboarding_runtime_env(monkeypatch)
    parser = build_parser()
    instance_root = tmp_path / "instance"
    argv = ["onboard", "--instance-name", "mybot", "--instance-root", str(instance_root), "--non-interactive"]
    if environment_secrets:
        argv.append("--secrets-from-environment")
    template = OnboardingTemplate(
        team_id="team_default", manager_storage_backend="sqlite",
        manager_storage_sqlite_path="data/task_history.db", manager_storage_postgres_dsn=None,
        timezone="UTC", model_provider="litellm", model_id="gemini-3.1-flash-lite-preview", reasoning_effort="",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        assistant_cli, "_resolve_onboarding_seed",
        lambda _args: GatewayOnboardingSeed(template=template, source_env_file_path=None),
    )
    _onboard(parser.parse_args(argv))
    config_path = instance_root / "instance_config.yaml"
    reconcile = assistant_cli._reconcile_existing_onboarding_config

    def reconcile_after_another_writer_saves(existing, proposed, args):
        other = GatewayInstanceConfig.load(config_path)
        other.gateway.request_timeout_seconds = 1234.0
        other.save()
        return reconcile(existing, proposed, args)

    monkeypatch.setattr(assistant_cli, "_reconcile_existing_onboarding_config", reconcile_after_another_writer_saves)
    with pytest.raises(ConfigFileConflict, match="settings file changed"):
        _onboard(parser.parse_args(argv))
    assert GatewayInstanceConfig.load(config_path).gateway.request_timeout_seconds == 1234.0


def test_onboard_applies_the_same_base_config_sections_as_the_web_onboarding(
    monkeypatch, tmp_path
):
    # Both onboarding front ends copy a base config through
    # onboarding_engine.apply_onboarding_template, so the command line also
    # carries gateway user settings, execution_control, llm.xai_oauth and
    # agent.agent_definition_paths.
    _clear_onboarding_runtime_env(monkeypatch)
    parser = build_parser()
    extra_agents = tmp_path / "extra_agents"
    extra_agents.mkdir()
    base_config = tmp_path / "config.yaml"
    base_config.write_text(
        "schema_version: 1\n"
        "gateway:\n"
        "  users:\n"
        "    allow_newuser_registration: false\n"
        "    link_code_ttl_seconds: 600\n"
        "execution_control:\n"
        "  runner:\n"
        "    max_concurrent_jobs: 7\n"
        "llm:\n"
        "  xai_oauth:\n"
        "    refresh_skew_seconds: 42\n"
        "agent:\n"
        "  agent_definition_paths:\n"
        f"    - {extra_agents}\n",
        encoding="utf-8",
    )
    instance_root = tmp_path / "instance"
    argv = [
        "onboard",
        "--instance-name",
        "mybot",
        "--instance-root",
        str(instance_root),
        "--base-config",
        str(base_config),
        "--non-interactive",
    ]
    monkeypatch.chdir(tmp_path)

    _onboard(parser.parse_args(argv))

    config_path = instance_root / "instance_config.yaml"
    created = GatewayInstanceConfig.load(config_path)
    assert created.gateway.users.allow_newuser_registration is False
    assert created.gateway.users.link_code_ttl_seconds == 600
    assert created.execution_control.runner.max_concurrent_jobs == 7
    assert created.llm.xai_oauth.refresh_skew_seconds == 42
    assert created.agent.agent_definition_paths == [str(extra_agents)]

    # A re-run with --base-config reapplies execution_control from the base
    # config. Before the template was shared it was reset to the default.
    created.execution_control.runner.max_concurrent_jobs = 3
    created.save()
    _onboard(parser.parse_args(argv))

    rerun = GatewayInstanceConfig.load(config_path)
    assert rerun.execution_control.runner.max_concurrent_jobs == 7
    assert rerun.llm.xai_oauth.refresh_skew_seconds == 42


def test_onboard_can_skip_internal_credential_generation(monkeypatch, tmp_path):
    parser = build_parser()
    base_env = tmp_path / "source.env"
    base_env.write_text(
        "NATS_SERVERS=nats://broker.external:4222\nNATS_PASSWORD=operator-owned\n",
        encoding="utf-8",
    )
    args = parser.parse_args(
        [
            "onboard",
            "--instance-name",
            "mybot",
            "--instance-root",
            str(tmp_path / "instance"),
            "--base-env-file",
            str(base_env),
            "--no-generate-internal-credentials",
            "--non-interactive",
        ]
    )
    template = OnboardingTemplate(
        team_id="team_default",
        manager_storage_backend="postgres",
        manager_storage_sqlite_path=None,
        manager_storage_postgres_dsn="postgresql://team:topsecret@db.internal:5432/team_runtime",
        timezone="America/Chicago",
        model_provider="litellm",
        model_id="gemini-3.1-flash-lite-preview",
        reasoning_effort="",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "assistant_cli._resolve_onboarding_seed",
        lambda _args: GatewayOnboardingSeed(
            template=template, source_env_file_path=str(base_env.resolve())
        ),
    )

    _onboard(args)

    env_text = (tmp_path / "instance" / "team.env").read_text(encoding="utf-8")
    assert (
        env_text
        == "NATS_SERVERS=nats://broker.external:4222\nNATS_PASSWORD=operator-owned\n"
    )
    assert "POSTGRES_DSN=" not in env_text
    assert "ACTIVITY_COLLECTOR_USER_TOKEN=" not in env_text
    assert "ACTIVITY_COLLECTOR_INGEST_TOKEN=" not in env_text


def test_onboard_base_config_maps_gateway_and_agent_runtime_settings(
    monkeypatch, tmp_path
):
    _clear_onboarding_runtime_env(monkeypatch)
    parser = build_parser()
    base_config = tmp_path / "config.yaml"
    base_config.write_text(
        "schema_version: 1\n"
        "transport:\n"
        "  nats_servers:\n"
        "    - nats://broker.example.com:4222\n"
        "llm:\n"
        "  provider: litellm\n"
        "  model_id: gemini-3.1-flash-lite-preview\n"
        "  reasoning_effort: high\n"
        "  context_window_tokens: 200000\n"
        "  max_output_tokens: 4096\n"
        "  stream: false\n"
        "  supports_image_input: true\n"
        "  supports_audio_input: false\n"
        "  model_capabilities:\n"
        "    litellm:\n"
        "      gemini-3.1-flash-lite-preview:\n"
        "        supports_image_input: true\n"
        "        supports_audio_input: false\n"
        "  reasoning_trace_retention_messages: 6\n"
        "  compaction_model_provider: litellm\n"
        "  compaction_model_id: compact-model\n"
        "  litellm_api_base: http://localhost:4000\n"
        "  failover:\n"
        "    enabled: true\n"
        "    switch_for:\n"
        "      min_turns: 4\n"
        "    fallbacks:\n"
        "      - provider: openai\n"
        "        model_id: fallback-model\n"
        "embedding:\n"
        "  provider: openai_compatible\n"
        "  model_id: pplx-embed-v1-0.6b\n"
        "  api_base: http://localhost:4000/v1\n"
        "  dimensions: 64\n"
        "  timeout_seconds: 12\n"
        "  organization: org_123\n"
        "  extra_headers:\n"
        "    X-Test: value\n"
        "agent:\n"
        "  default_team_id: team_base\n"
        "  default_request_timezone: America/New_York\n"
        "  memory_graph_enabled: true\n"
        "  max_queued_top_level_tasks: 22\n"
        "  task_queue_warning_threshold: 9\n"
        "  subtask_timeout_seconds: 2400\n"
        "  subtask_watchdog_interval_seconds: 20\n"
        "  subtask_status_probe_timeout_seconds: 2.5\n"
        "  restart_executing_task_grace_seconds: 300\n"
        "  same_role_runtime_takeover_enabled: false\n"
        "  max_tool_calls_per_task: 30\n"
        "  max_tool_result_tokens_per_call: 3000\n"
        "  runtime_budget_policy:\n"
        "    mode: user_guided\n"
        "    max_user_guided_pauses_per_task: 2\n"
        "  inner_loop_context:\n"
        "    max_total_tokens: 12000\n"
        "  context_engine:\n"
        "    reserved_output_tokens: 2048\n"
        "  conversation_compactor:\n"
        "    type: noop\n"
        "  conversation_summary_generator_type: llm_summarizing\n"
        "  deferred_message_warning_threshold: 13\n"
        "  structured_output_failure_threshold: 5\n"
        "delivery:\n"
        "  quiet_hours:\n"
        "    enabled: false\n"
        "    start_hour_local: 21\n"
        "    end_hour_local: 7\n"
        "    allow_urgent_delivery: false\n"
        "  digest_delivery_hour_local: 9\n"
        "  summarize_deferred: false\n"
        "  notification_routing:\n"
        "    mode: default_target\n"
        "  urgent_keywords: [urgent, now]\n"
        "  default_target:\n"
        "    channel_name: telegram\n"
        "    chat_id: -100123\n"
        "    thread_id: 42\n"
        "network:\n"
        "  public_origin: http://public.example.test\n"
        "security_layer:\n"
        "  mode: openshell\n"
        "  broker_internal_base_url: http://127.0.0.1:8777\n"
        "  broker_public_port: 8777\n"
        "  broker_bind_host: 0.0.0.0\n"
        "  broker_bind_port: 8777\n"
        "  broker_credentials_dir: broker_credentials\n"
        "  sealbox:\n"
        "    enabled: true\n"
        "    manage_service: true\n"
        "    auto_initialize: true\n"
        "    root_token_file: managed/sealbox-token\n"
        "  openshell:\n"
        "    sandbox_name: team-test\n"
        "mcp_profiles:\n"
        "  skills_mcp:\n"
        "    transport: streamable_http\n"
        "    server_url: http://127.0.0.1:8000/mcp\n"
        "interactive_apps:\n"
        "  plugins:\n"
        "    builtin.internal_website:\n"
        "      settings:\n"
        "        docs_root: custom-user-docs\n"
        "        skills_dir: custom-doc-skills\n"
        "storage:\n"
        "  backend: postgres\n"
        "  postgres:\n"
        "    dsn: postgresql://team:secret@db/team_agents\n"
        "agent_types:\n"
        "  WebResearchAgent:\n"
        "    agent:\n"
        "      max_tool_calls_per_task: 18\n"
        "      max_tool_result_tokens_per_call: 7500\n",
        encoding="utf-8",
    )
    args = parser.parse_args(
        [
            "onboard",
            "--instance-name",
            "mybot",
            "--base-config",
            str(base_config),
            "--non-interactive",
        ]
    )
    monkeypatch.chdir(tmp_path)

    _onboard(args)

    config = GatewayInstanceConfig.load(
        tmp_path / "instances" / "mybot" / "instance_config.yaml"
    )
    raw_saved = yaml.safe_load(
        (tmp_path / "instances" / "mybot" / "instance_config.yaml").read_text(
            encoding="utf-8"
        )
    )
    saved = config.to_yaml_dict()
    assert config.gateway.team_id == "team_base"
    assert config.state.storage.backend == "postgres"
    assert config.state.storage.postgres.dsn is None
    assert config.gateway.storage.backend == "postgres"
    assert config.gateway.storage.postgres.dsn is None
    assert config.llm.provider == "litellm"
    assert config.llm.model_id == "gemini-3.1-flash-lite-preview"
    assert config.llm.reasoning_effort == "high"
    assert config.llm.context_window_tokens == 200000
    assert config.llm.max_output_tokens == 4096
    assert config.llm.stream is False
    assert config.llm.supports_image_input is True
    assert config.llm.supports_audio_input is False
    assert config.llm.model_capabilities == {
        "litellm": {
            "gemini-3.1-flash-lite-preview": {
                "supports_image_input": True,
                "supports_audio_input": False,
            }
        }
    }
    assert config.llm.reasoning_trace_retention_messages == 6
    assert config.llm.compaction_model_provider == "litellm"
    assert config.llm.compaction_model_id == "compact-model"
    assert config.llm.litellm_api_base == "http://localhost:4000"
    assert config.llm.failover["enabled"] is True
    assert config.llm.failover["switch_for"]["min_turns"] == 4
    assert config.llm.failover["switch_for"]["min_seconds"] == 900
    assert config.llm.failover["fallbacks"] == [
        {"provider": "openai", "model_id": "fallback-model"}
    ]
    assert config.embedding.provider == "openai_compatible"
    assert config.embedding.model_id == "pplx-embed-v1-0.6b"
    assert config.embedding.api_base == "http://localhost:4000/v1"
    assert config.embedding.dimensions == 64
    assert config.embedding.timeout_seconds == 12
    assert config.embedding.organization == "org_123"
    assert config.embedding.extra_headers == {"X-Test": "value"}
    assert config.security_layer.mode == "openshell"
    assert config.security_layer.broker_internal_base_url == "http://127.0.0.1:8777"
    assert config.network.public_origin == "http://public.example.test"
    assert config.security_layer.broker_public_port == 8777
    assert (
        config.secure_tool_broker_public_base_url == "http://public.example.test:8777"
    )
    assert config.security_layer.broker_bind_host == "0.0.0.0"
    assert config.security_layer.broker_bind_port == 8777
    assert config.security_layer.broker_credentials_dir == "broker_credentials"
    assert config.security_layer.sealbox.root_token_file == "managed/sealbox-token"
    assert config.security_layer.sealbox.tenant_material_dir == "sealbox/tenants"
    assert config.security_layer.openshell.sandbox_name == "team-test"
    assert raw_saved["security_layer"]["broker_bind_host"] == "0.0.0.0"
    assert raw_saved["security_layer"]["broker_bind_port"] == 8777
    assert raw_saved["security_layer"]["broker_credentials_dir"] == "broker_credentials"
    assert raw_saved["security_layer"]["broker_public_port"] == 8777
    assert "broker_public_base_url" not in raw_saved["security_layer"]
    assert raw_saved["network"]["public_origin"] == "http://public.example.test"
    assert (
        raw_saved["security_layer"]["sealbox"]["root_token_file"]
        == "managed/sealbox-token"
    )
    assert (
        raw_saved["security_layer"]["sealbox"]["tenant_material_dir"]
        == "sealbox/tenants"
    )
    assert "token_file" not in raw_saved["security_layer"]["sealbox"]
    assert "public_key_file" not in raw_saved["security_layer"]["sealbox"]
    assert "private_key_file" not in raw_saved["security_layer"]["sealbox"]
    sealbox_dir = tmp_path / "instances" / "mybot" / "broker_credentials" / "managed"
    assert (sealbox_dir / "sealbox-token").is_file()
    assert not (sealbox_dir / "sealbox-public.pem").exists()
    assert not (sealbox_dir / "sealbox-private.pem").exists()
    # Only the instance root is saved; every folder is derived from it.
    assert "workspace_dir" not in raw_saved
    assert "workspace_dir" not in raw_saved["agent"]
    assert raw_saved["instance_root"] == str(
        (tmp_path / "instances" / "mybot").resolve()
    )
    assert "task_history_db_path" not in raw_saved["workspace"]
    assert present_removed_location_settings(raw_saved) == []
    assert "user_docs" not in raw_saved
    assert "website" not in raw_saved["interactive_apps"]
    assert config.interactive_apps.internal_website.bind_host == "0.0.0.0"
    assert config.interactive_apps.internal_website.bind_port == 18090
    assert config.interactive_apps.internal_website.public_port == 18090
    assert config.interactive_apps.internal_website.docs_root == "custom-user-docs"
    assert config.interactive_apps.internal_website.skills_dir == "custom-doc-skills"
    assert (
        raw_saved["interactive_apps"]["plugins"]["builtin.internal_website"][
            "settings"
        ]["docs_root"]
        == "custom-user-docs"
    )
    assert (
        raw_saved["interactive_apps"]["plugins"]["builtin.internal_website"][
            "settings"
        ]["skills_dir"]
        == "custom-doc-skills"
    )
    assert config.delivery.default_target.channel_name == "telegram"
    assert config.delivery.default_target.chat_id == "-100123"
    assert config.delivery.default_target.thread_id == "42"
    assert config.delivery.quiet_hours.enabled is False
    assert config.delivery.quiet_hours.start_hour_local == 21
    assert config.delivery.quiet_hours.end_hour_local == 7
    assert config.delivery.quiet_hours.allow_urgent_delivery is False
    assert config.delivery.digest_delivery_hour_local == 9
    assert config.delivery.summarize_deferred is False
    assert config.delivery.notification_routing.mode == "default_target"
    assert config.delivery.urgent_keywords == ["urgent", "now"]
    assert config.mcp_profiles["skills_mcp"]["transport"] == "streamable_http"
    assert raw_saved["agent"]["runtime_budget_policy"]["mode"] == "user_guided"
    assert "agent_default" not in raw_saved
    assert config.agent.memory_graph_enabled is True
    assert config.agent.max_queued_top_level_tasks == 22
    assert config.agent.task_queue_warning_threshold == 9
    assert config.agent.subtask_timeout_seconds == 2400
    assert config.agent.subtask_watchdog_interval_seconds == 20
    assert config.agent.subtask_status_probe_timeout_seconds == 2.5
    assert config.agent.restart_executing_task_grace_seconds == 300
    assert config.agent.same_role_runtime_takeover_enabled is False
    assert config.agent.max_tool_calls_per_task == 30
    assert config.agent.max_tool_result_tokens_per_call == 3000
    assert config.agent.runtime_budget_policy["mode"] == "user_guided"
    assert config.agent.runtime_budget_policy["max_user_guided_pauses_per_task"] == 2
    assert config.agent.inner_loop_context["max_total_tokens"] == 12000
    assert config.agent.context_engine["reserved_output_tokens"] == 2048
    assert config.agent.conversation_compactor["type"] == "noop"
    assert config.agent.conversation_summary_generator_type == "llm_summarizing"
    assert config.agent.deferred_message_warning_threshold == 13
    assert config.agent.structured_output_failure_threshold == 5
    assert config.agent_types["WebResearchAgent"].agent.max_tool_calls_per_task == 18
    assert (
        config.agent_types["WebResearchAgent"].agent.max_tool_result_tokens_per_call
        == 7500
    )
    assert "gateway" in saved
    assert "manager" not in saved
    assert (
        "postgresql://team:secret@db/team_agents"
        not in config.config_path.read_text(encoding="utf-8")
    )


def test_onboard_base_config_refuses_custom_removed_locations(monkeypatch, tmp_path):
    """Custom control-file locations must be moved by hand before onboarding."""

    _clear_onboarding_runtime_env(monkeypatch)
    base_config = tmp_path / "config.yaml"
    base_config.write_text(
        "schema_version: 1\n"
        "workspace:\n"
        "  skills_dir: custom-skills\n"
        "  llm_override_path: CUSTOM_OVERRIDES.yaml\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(
        [
            "onboard",
            "--instance-name",
            "mybot",
            "--base-config",
            str(base_config),
            "--non-interactive",
        ]
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="no longer configurable") as raised:
        _onboard(args)

    assert "workspace.skills_dir=custom-skills" in str(raised.value)
    assert "workspace.llm_override_path=CUSTOM_OVERRIDES.yaml" in str(raised.value)
    assert not (tmp_path / "instances" / "mybot" / "instance_config.yaml").exists()


def test_load_gateway_instance_config_applies_env_nats_overrides(monkeypatch, tmp_path):
    config = GatewayInstanceConfig.create(
        instance_name="mybot",
        instance_root=str(tmp_path / "instance"),
        team_id="team_303",
        timezone="America/Chicago",
    )
    config.save()
    env_path = tmp_path / "debug.env"
    env_path.write_text(
        "NATS_SERVERS=nats://broker-a.internal:4222,nats://broker-b.internal:4222\n"
        "NATS_USER=test-user\n"
        "NATS_PASSWORD=topsecret\n",
        encoding="utf-8",
    )

    nats_env_keys = [
        "NATS_URL",
        "NATS_SERVERS",
        "NATS_USER",
        "NATS_USER_FILE",
        "NATS_PASSWORD",
    ]
    original_env = {key: os.environ.get(key) for key in nats_env_keys}
    for key in nats_env_keys:
        monkeypatch.delenv(key, raising=False)

    try:
        loaded = _load_gateway_instance_config(
            str(config.config_path), env_path=str(env_path)
        )

        assert loaded.transport.nats_servers == [
            "nats://broker-a.internal:4222",
            "nats://broker-b.internal:4222",
        ]
        assert loaded.transport.nats_user == "test-user"
        assert loaded.transport.nats_password == "topsecret"
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

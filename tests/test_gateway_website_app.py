from __future__ import annotations

from tests.browser_assets import module_source

import asyncio
import inspect
import json
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.config import (
    GatewayInstanceConfig,
    resolve_activity_collector_runtime_base_url,
)
from assistant_gateway.command_composition import build_gateway_command_application
from assistant_gateway.application.website_application import WebMemoryConflictError
from assistant_gateway.application.gateway_application import GatewayApplicationResponse
from assistant_gateway.application.transient_presentation import GatewayTransientAction
from assistant_gateway.application.account_links import AccountLinkForm
from tests.application_test_doubles import (
    RecordingLinkResults,
    website_application_from as WebsiteGatewayApplicationFacade,
)
from assistant_gateway.gateway import GatewayDeliveryEvent, UserGateway
from assistant_gateway.interactive_apps.website import (
    InternalWebsiteInteractiveApp as _InternalWebsiteInteractiveApp,
)
from assistant_gateway.attachments import (
    gateway_staged_attachment_reader,
    stage_user_notification_attachments,
    validate_user_notification_attachments,
)
from assistant_gateway.interactive_apps.website_support import (
    resolve_internal_website_published_sites,
)
from assistant_gateway.interactive_apps.host_ports import (
    InternalWebsiteHostEnvironment,
    InteractiveAppRuntimeContext,
    LocalInteractiveAppAttachmentStore,
)
from assistant_gateway.inbound_attachments import (
    InboundAttachmentStoragePolicy,
    resolve_inbound_attachment_cache_dir,
)
from assistant_gateway.application.commands import tour_start_request_text
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.runtime_ports import (
    OwnershipPrivateResourceAuthorizer,
    SystemGatewayIdentitySecrets,
)
from custom_agents.skill_repository import build_skill_tree_manifest
from runtime_settings.appearance import DarkMetalAppearanceSettings, OnboardingAppearanceSettings
from user_docs.published_sites import (
    SITE_INDEX_FILENAME,
    PublishedSiteRecordV1,
    site_folder_parts,
)


class InternalWebsiteInteractiveApp(_InternalWebsiteInteractiveApp):
    """Test composition helper that keeps legacy gateway fakes behind the facade."""

    def __init__(self, config, *, gateway, bridge=None, logger=None) -> None:
        if not config.config_path.exists():
            config.save(config.config_path)
        gateway.command_application = build_gateway_command_application(
            config=config,
            operations=gateway,
            session_repository=gateway.session_repository,
            request_repository=gateway.request_repository,
            private_authorizer=gateway._private_authorizer,
            operational_controls=getattr(gateway, "_operational_controls", gateway),
            link_results=RecordingLinkResults(),
        )
        application = WebsiteGatewayApplicationFacade(
            gateway,
            config=config,
            command_application=gateway.command_application,
        )
        attachment_settings = config.interactive_apps.internal_website.attachments
        environment = _website_environment(config)
        runtime = InteractiveAppRuntimeContext(
            request_timeout_seconds=config.manager.request_timeout_seconds,
            voice=config.voice,
            authorization_presentation_mode=config.authorization.presentation.mode,
            voice_client=None,
            attachments=LocalInteractiveAppAttachmentStore(
                InboundAttachmentStoragePolicy(
                    cache_dir=resolve_inbound_attachment_cache_dir(config),
                    max_file_bytes=attachment_settings.max_file_bytes,
                    allowed_kinds=frozenset(attachment_settings.allowed_kinds),
                ),
                application,
            ),
            outgoing_attachments=gateway_staged_attachment_reader(config),
            website=environment,
        )
        super().__init__(
            config.interactive_apps.internal_website,
            application,
            bridge,
            runtime,
            logger=logger,
        )


def _website_environment(
    config: GatewayInstanceConfig,
) -> InternalWebsiteHostEnvironment:
    return InternalWebsiteHostEnvironment(
        instance_root_path=config.instance_root_path,
        workspace_path=config.workspace_path,
        config_path=config.config_path.resolve(),
        base_url=config.internal_website_base_url,
        public_origin=config.network.public_origin,
        assistant_display_name=config.assistant.display_name,
        appearance=config.ui.onboarding.appearance.model_dump(mode="json"),
        activity_collector_base_url=(
            resolve_activity_collector_runtime_base_url(config) or ""
        ),
        activity_collector_admin_token=config.activity_collector.resolve_admin_token(),
        activity_collector_request_timeout_seconds=(
            config.activity_collector.request_timeout_seconds
        ),
        agent_debug=dict(config.agent.debug or {}),
    )


class _FakeGateway:
    def __init__(self) -> None:
        # A private database per fake, removed with it: the store treats
        # ":memory:" as a file name in the working directory.
        self._state_dir = tempfile.TemporaryDirectory(prefix="ua-fake-gateway-")
        self.state_store = GatewayStateStore(str(Path(self._state_dir.name) / "state.db"))
        self.session_repository = self.state_store.session_repository
        self.request_repository = self.state_store.request_repository
        self._private_authorizer = OwnershipPrivateResourceAuthorizer()
        self.identity_service = self
        self.sent_user_messages: list[dict] = []
        self.replies_to_pending: list[dict] = []
        self.context_debug_controls: list[dict] = []
        self.stop_requests: list[dict] = []
        self.objective_control_calls: list[dict] = []
        self.schedule_control_calls: list[dict] = []
        self.mcp_control_calls: list[dict] = []
        self.memory_calls: list[tuple[str, dict]] = []
        # The id of the question waiting in "pending_session", if any.
        self.waiting_question: str | None = None
        self.event_sink = None

    async def emit_delivery_event(self, event) -> bool:
        if not callable(self.event_sink):
            return False
        result = self.event_sink(event)
        if inspect.isawaitable(result):
            await result
        return True

    @staticmethod
    def is_task_progress_status_request(text, attachments=None) -> bool:
        return str(text or "").strip() == "?" and not attachments

    def start_new_session(
        self,
        *,
        channel_name=None,
        user_id=None,
        chat_id=None,
        thread_id=None,
        canonical_user_id=None,
    ):
        return SimpleNamespace(
            session_key=f"{channel_name}:{chat_id}:{user_id}:new",
            conversation_id="conv_new",
            parallel_thread=False,
        )

    async def waiting_question_id_for_session(self, session_key: str):
        return self.waiting_question if session_key == "pending_session" else None

    async def reply_to_pending_query(
        self, *, session_key: str, task_query_id: str, response_text: str, replied_message=None
    ):
        self.replies_to_pending.append(
            {
                "session_key": session_key,
                "task_query_id": task_query_id,
                "response_text": response_text,
            }
        )
        return SimpleNamespace(
            text=f"Pending reply: {response_text}",
            conversation_id="conv_pending",
            correlation_id="corr_pending",
            task_id="task_pending",
            attachments=(),
        )

    async def send_user_message(self, text: str, **kwargs):
        self.sent_user_messages.append({"text": text, **kwargs})
        return SimpleNamespace(
            text=f"Reply: {text}",
            conversation_id=kwargs.get("conversation_id") or "conv_reply",
            correlation_id="corr_reply",
            task_id="task_reply",
            attachments=(),
        )

    async def send_context_debug_dump_control(
        self, *, enabled: bool, mode: str, **kwargs
    ):
        self.context_debug_controls.append({"enabled": enabled, "mode": mode, **kwargs})
        return SimpleNamespace(
            text=f"Context debug dump settings applied: enabled={str(enabled).lower()} mode={mode}.",
            conversation_id=kwargs.get("conversation_id") or "conv_debug",
            correlation_id="corr_debug",
            task_id=None,
            message_type="system_status",
            attachments=(),
        )

    async def send_stop_request(self, **kwargs):
        self.stop_requests.append(kwargs)
        return SimpleNamespace(text="Stopped active task.")

    async def stop(self, request):
        route = request.route
        return await self.send_stop_request(
            session_key=request.session_key,
            channel_name=route.channel_name,
            user_id=route.user_id,
            chat_id=route.chat_id,
            thread_id=route.thread_id,
            canonical_user_id=route.canonical_user_id,
        )

    @staticmethod
    def canonical_user_id_for_context(**_context):
        return None

    @staticmethod
    def gateway_users_enabled() -> bool:
        return False

    @staticmethod
    def requester_timezone_for_context(**_context) -> str:
        return "UTC"

    def switch_session_conversation(
        self,
        *,
        session_key: str,
        conversation_id: str,
        last_task_id: str | None,
    ):
        existing = self.session_repository.get_session(session_key)
        if existing is None:
            return None
        self.session_repository.upsert_session(
            session_key=session_key,
            conversation_id=conversation_id,
            last_task_id=last_task_id,
            manager_agent_id=existing.manager_agent_id,
        )
        return self.session_repository.get_session(session_key)

    @staticmethod
    def resolve_session(*, session_key=None, **_context):
        return SimpleNamespace(
            session_key=session_key or "website:web-chat:resolved",
            conversation_id="conv_resolved",
        )

    def resolve_web_user_token(self, raw_token: str):
        return None

    def resolve_web_token(self, raw_token: str):
        return self.resolve_web_user_token(raw_token)

    def get_web_chat_appearance(self, canonical_user_id: str):
        record = self.state_store.identity_repository.get_gateway_user_appearance(
            canonical_user_id
        )
        return record.appearance if record is not None else None

    def save_web_chat_appearance(self, *, canonical_user_id: str, appearance: dict):
        record = self.state_store.identity_repository.upsert_gateway_user_appearance(
            canonical_user_id=canonical_user_id,
            appearance=appearance,
        )
        return record.appearance

    def reset_web_chat_appearance(self, canonical_user_id: str) -> bool:
        return self.state_store.identity_repository.delete_gateway_user_appearance(
            canonical_user_id
        )

    @staticmethod
    def consume_web_invite(_raw_token: str):
        return None

    @staticmethod
    def is_web_chat_user_active(_canonical_user_id: str) -> bool:
        return True

    def list_web_chat_team_members(self, *, presence_only=False):
        return [
            {
                "agent_id": "agent_architect",
                "name": "Nova",
                "named_agent_type": "TeamArchitect",
                "is_team_lead": True,
                "status": {
                    "key": "available",
                    "label": "Available",
                    "tone": "success",
                },
                "available_skills": ["prototype", "runtime-operations"],
                "last_seen_at": "2026-08-11T12:00:00+00:00",
            }
        ]

    async def list_web_chat_memories(self, **request):
        self.memory_calls.append(("list", request))
        scope = request["scope"]
        return {
            "scope": scope,
            "memories": [
                {
                    "scope": scope,
                    "memory_id": 7,
                    "fact_text": "Prefer concise release notes.",
                    "tags": ["writing", "release"],
                    "valid_from_utc": "2026-08-01T12:00:00Z",
                    "invalid_after_utc": None,
                    "created_at_utc": "2026-08-01T12:00:00Z",
                    "updated_at_utc": "2026-08-24T12:00:00Z",
                    "record_digest": "a" * 64,
                    "available_actions": (
                        ["edit", "delete"] if scope == "personal" else []
                    ),
                }
            ],
            "offset": request["offset"],
            "limit": request["limit"],
            "total": 1,
            "has_more": False,
        }

    async def mutate_web_chat_memory(self, **request):
        self.memory_calls.append(("mutate", request))
        return {
            "scope": request["scope"],
            "memory_id": request["memory_id"],
            "action": request["action"],
            "memory": (
                {
                    "scope": request["scope"],
                    "memory_id": request["memory_id"],
                    "fact_text": request["fact_text"],
                    "tags": request["tags"],
                    "valid_from_utc": "2026-08-01T12:00:00Z",
                    "invalid_after_utc": request["invalid_after_utc"],
                    "created_at_utc": "2026-08-01T12:00:00Z",
                    "updated_at_utc": "2026-08-25T12:00:00Z",
                    "record_digest": "b" * 64,
                    "available_actions": ["edit", "delete"],
                }
                if request["action"] == "edit"
                else None
            ),
            "event_id": "memory-event-1",
            "deleted": request["action"] == "delete",
        }

    def list_web_chat_mcp_connections(self, **_context):
        return []

    def list_web_chat_hosted_mcp_servers(self, **_context):
        return []

    async def list_web_chat_objectives(self, **context):
        self.objective_control_calls.append(context)
        return []

    async def list_web_chat_schedules(self, **_context):
        return []

    async def control_web_chat_objective(self, **request):
        self.objective_control_calls.append(request)
        return {
            "objective_id": request["objective_id"],
            "action": request["action"],
            "status": "paused",
            "success": True,
            "message": "objective_paused",
            "cancellation_receipts": [],
        }

    async def control_web_chat_schedule(self, **request):
        self.schedule_control_calls.append(request)
        return {
            "schedule_id": request["schedule_id"],
            "action": request["action"],
            "success": True,
        }

    async def control_web_chat_mcp_lifecycle(self, **request):
        self.mcp_control_calls.append(request)
        return {
            "resource_kind": request["resource_kind"],
            "resource_id": request["resource_id"],
            "action": request["action"],
            "status": "disabled",
            "request_digest": request["expected_digest"],
            "result_digest": "f" * 64,
            "recoverable": True,
        }


class _StatefulFakeGateway(_FakeGateway):
    def __init__(self, state_store: GatewayStateStore) -> None:
        super().__init__()
        self.state_store = state_store
        self.session_repository = state_store.session_repository
        self.request_repository = state_store.request_repository
        self.delivery_repository = state_store.delivery_repository
        self._private_authorizer = OwnershipPrivateResourceAuthorizer()

    def can_access_private_resource(self, **kwargs):
        return self._private_authorizer.can_access(**kwargs)

    def resolve_session(
        self,
        *,
        session_key=None,
        channel_name=None,
        user_id=None,
        chat_id=None,
        thread_id=None,
        canonical_user_id=None,
    ):
        existing = self.state_store.resolve_session_reference(session_key or "")
        if existing is not None:
            return existing
        return self.state_store.create_session(
            session_key=session_key,
            conversation_id="conv_created",
            manager_agent_id="manager_1",
            channel_name=channel_name,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            owner_canonical_user_id=canonical_user_id,
        )


class _UserTokenFakeGateway(_FakeGateway):
    def resolve_web_user_token(self, raw_token: str):
        if raw_token != "ua_user_token":
            return None
        return SimpleNamespace(
            user=SimpleNamespace(
                canonical_user_id="user_alpha",
                display_name="Alice",
            )
        )


class _UserTokenStatefulFakeGateway(_StatefulFakeGateway):
    def resolve_web_user_token(self, raw_token: str):
        if raw_token != "ua_user_token":
            return None
        return SimpleNamespace(
            user=SimpleNamespace(
                canonical_user_id="user_alpha",
                display_name="Alice",
            )
        )


class _MCPFakeGateway(_UserTokenStatefulFakeGateway):
    """Records the MCP decisions the website forwards (the real controls are in test_mcp_connections)."""

    def __init__(self, state_store: GatewayStateStore) -> None:
        super().__init__(state_store)
        self.permission_calls: list[tuple[str, dict]] = []

    def decide_web_chat_mcp_connection(self, **kwargs):
        self.permission_calls.append(("decide_mcp_connection", kwargs))
        return {
            "status": "active",
            "connection_id": kwargs["connection_id"],
            "request_digest": kwargs["expected_record_digest"],
            "record_digest": "c" * 64,
            "response_digest": "d" * 64,
            "decision": kwargs["decision"],
        }


def _config(tmp_path: Path) -> GatewayInstanceConfig:
    config = GatewayInstanceConfig.create(
        instance_name="websitetest",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Phoenix",
    )
    config.network.public_origin = "http://127.0.0.1"
    config.interactive_apps.internal_website.enabled = True
    config.interactive_apps.internal_website.bind_host = "127.0.0.1"
    config.interactive_apps.internal_website.bind_port = 18500
    config.interactive_apps.internal_website.public_port = 18500
    docs_root = config.instance_root_path / "user_docs" / "site"
    docs_root.mkdir(parents=True, exist_ok=True)
    (docs_root / "index.html").write_text(
        "<!doctype html><title>Universal Agents Docs</title>", encoding="utf-8"
    )
    config.interactive_apps.internal_website.docs_root = str(docs_root)
    return config


def _last_transcript_content(payload: dict) -> str:
    return str(payload["messages"][-1]["content"])


def _transient_content(payload: dict) -> str:
    return str(payload["transient"]["content"])


def _assert_sensitive_transient_timeline_item(payload: dict) -> None:
    transient = payload["transient"]
    anchor = payload["messages"][-1]
    assert transient["kind"] == "sensitive_command_result"
    assert transient["item_id"] == (f"sensitive_command_result:{anchor['message_id']}")
    assert transient["session_sequence"] == anchor["session_sequence"]
    assert transient["created_at_utc"] == anchor["created_at_utc"]


_CONFIG_USER = object()


def _write_site(
    config: GatewayInstanceConfig,
    site_id: str = "demo",
    *,
    owner: object = _CONFIG_USER,
    title: str = "Demo",
) -> Path:
    """Publish a page the way publish_single_page_site does: file in the owner's folder, then record."""

    store = resolve_internal_website_published_sites(_website_environment(config))
    person = config.user.canonical_user_id if owner is _CONFIG_USER else owner
    parts = site_folder_parts(person, site_id)
    digests = store.write_files(
        parts, {SITE_INDEX_FILENAME: f"<!doctype html><title>{title}</title>".encode()}
    )
    now = datetime.now(timezone.utc).isoformat()
    store.save(
        PublishedSiteRecordV1(
            site_id=site_id,
            folder="/".join(parts),
            owner_person_id=person,
            title=title,
            site_kind="article",
            created_at=now,
            updated_at=now,
            files=digests,
        )
    )
    return store.workspaces_dir.joinpath(*parts)


def _enable_web_chat(config: GatewayInstanceConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")


async def _signed_in(client: TestClient, token: str = "uaweb_test_token") -> dict[str, str]:
    """Cookie header of a Web Chat session (the bootstrap token signs in as config.user)."""

    response = await client.post("/api/web-chat/auth", json={"token": token})
    assert response.status == 200
    return {"Cookie": f"ua_web_chat_session={response.cookies['ua_web_chat_session'].value}"}


@pytest.mark.asyncio
async def test_website_app_includes_attested_managed_skills_in_docs(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    managed_root = (
        config.instance_root_path / "runtime" / "skill-installation" / "installed"
    )
    attestations_root = (
        config.instance_root_path / "runtime" / "skill-installation" / "attestations"
    )
    managed_skill = managed_root / "reviewed-demo"
    managed_skill.mkdir(parents=True)
    attestations_root.mkdir(parents=True)
    (managed_skill / "SKILL.md").write_text(
        "---\nname: reviewed-demo\ndescription: Reviewed install.\n---\n\n# Reviewed\n",
        encoding="utf-8",
    )
    manifest = build_skill_tree_manifest(managed_skill)
    (attestations_root / "reviewed-demo.json").write_text(
        json.dumps(
            {
                "skill_name": "reviewed-demo",
                "installed_path": str(managed_skill.resolve()),
                "tree_sha256": manifest.tree_sha256,
            }
        ),
        encoding="utf-8",
    )
    app = InternalWebsiteInteractiveApp(config, gateway=_FakeGateway())

    async with TestClient(TestServer(app.build_app())) as client:
        index_response = await client.get("/docs/skills/")
        detail_response = await client.get("/docs/skills/reviewed-demo")
        index_text = await index_response.text()
        detail_text = await detail_response.text()

    assert index_response.status == 200
    assert "Managed Installed Skills" in index_text
    assert "reviewed-demo" in index_text
    assert detail_response.status == 200
    assert "Managed installed skill" in detail_text


@pytest.mark.asyncio
async def test_website_adapter_rejects_delivery_without_durable_identity(
    tmp_path: Path,
) -> None:
    app = InternalWebsiteInteractiveApp(
        _config(tmp_path), gateway=_FakeGateway(), bridge=SimpleNamespace()
    )
    event = GatewayDeliveryEvent(
        text="Late response",
        session_key="website:web-chat:browser_1",
        channel_name="website",
        user_id="browser_1",
        chat_id="web-chat",
        thread_id=None,
        conversation_id="conv_reply",
        scheduled_job_id=None,
        correlation_id="corr_reply",
        original_message_id="browser_msg_1",
    )

    with pytest.raises(RuntimeError, match="before transcript persistence"):
        await app.deliver_event(event)


@pytest.mark.asyncio
async def test_web_chat_transcript_and_reply_survive_restart_and_second_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    owner = config.user.canonical_user_id
    store.create_session(
        session_key="website:web-chat:durable",
        conversation_id="conv_durable",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        owner_canonical_user_id=owner,
    )
    gateway = _StatefulFakeGateway(store)
    first_app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(first_app.build_app())) as first_browser:
        auth_response = await first_browser.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        cookie = auth_response.cookies["ua_web_chat_session"].value
        request_payload = {
            "message": "Persist this on device A",
            "session_key": "website:web-chat:durable",
            "conversation_id": "conv_durable",
            "client_request_id": "device_a_request_1",
        }
        sent_response = await first_browser.post(
            "/api/web-chat/chat",
            headers={"Cookie": f"ua_web_chat_session={cookie}"},
            json=request_payload,
        )
        sent_payload = await sent_response.json()
        retry_response = await first_browser.post(
            "/api/web-chat/chat",
            headers={"Cookie": f"ua_web_chat_session={cookie}"},
            json=request_payload,
        )
        retry_payload = await retry_response.json()

    restarted_store = GatewayStateStore(str(tmp_path / "gateway.db"))
    restarted_gateway = _StatefulFakeGateway(restarted_store)
    replacement_app = InternalWebsiteInteractiveApp(
        config, gateway=restarted_gateway, bridge=SimpleNamespace()
    )
    async with TestClient(TestServer(replacement_app.build_app())) as second_browser:
        auth_response = await second_browser.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        cookie = auth_response.cookies["ua_web_chat_session"].value
        sessions_response = await second_browser.get(
            "/api/web-chat/sessions",
            headers={"Cookie": f"ua_web_chat_session={cookie}"},
        )
        sessions_payload = await sessions_response.json()
        history_response = await second_browser.get(
            "/api/web-chat/sessions/website:web-chat:durable/messages?limit=50",
            headers={"Cookie": f"ua_web_chat_session={cookie}"},
        )
        history_payload = await history_response.json()
        events_response = await second_browser.get(
            "/api/web-chat/events",
            headers={"Cookie": f"ua_web_chat_session={cookie}"},
        )
        events_payload = await events_response.json()
        continued_response = await second_browser.post(
            "/api/web-chat/chat",
            headers={"Cookie": f"ua_web_chat_session={cookie}"},
            json={
                "message": "Continue the saved discussion",
                "session_key": sessions_payload["sessions"][0]["session_key"],
                "conversation_id": sessions_payload["sessions"][0]["conversation_id"],
                "client_request_id": "device_b_request_1",
            },
        )
        continued_payload = await continued_response.json()

    assert sent_response.status == 200
    assert retry_response.status == 200
    assert sessions_response.status == 200
    assert history_response.status == 200
    assert events_response.status == 200
    assert [message["content"] for message in sent_payload["messages"]] == [
        "Persist this on device A",
        "Reply: Persist this on device A",
    ]
    assert sent_payload["messages"][0]["client_request_id"] == "device_a_request_1"
    assert sent_payload["messages"][1]["client_request_id"] is None
    assert [message["message_id"] for message in history_payload["messages"]] == [
        message["message_id"] for message in sent_payload["messages"]
    ]
    assert history_payload["messages"][0]["client_request_id"] == ("device_a_request_1")
    assert events_payload["messages"][0]["client_request_id"] == ("device_a_request_1")
    assert [message["message_id"] for message in retry_payload["messages"]] == [
        message["message_id"] for message in sent_payload["messages"]
    ]
    assert len(gateway.sent_user_messages) == 1
    assert continued_response.status == 200
    assert continued_payload["session_key"] == "website:web-chat:durable"
    assert continued_payload["conversation_id"] == "conv_durable"
    assert len(restarted_gateway.sent_user_messages) == 1
    assert restarted_gateway.sent_user_messages[0]["session_key"] == "website:web-chat:durable"
    assert restarted_gateway.sent_user_messages[0]["conversation_id"] == "conv_durable"
    assert len(sessions_payload["sessions"]) == 1
    assert sessions_payload["event_cursor"]
    assert history_payload["event_cursor"]
    assert not sessions_payload["event_cursor"].isdigit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_type", "action_kind", "action_label", "safe_transcript"),
    [
        (
            "secure_credential_collection",
            "secure_credential_collection",
            "Open secure credential form",
            "Secure credential entry is ready in the trusted credential workflow.",
        ),
        (
            "tool_setup",
            "tool_setup",
            "Sign in to GitHub",
            "A setup page is ready in the trusted credential workflow.",
        ),
    ],
)
async def test_web_chat_secure_form_url_is_only_in_initial_no_store_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    message_type: str,
    action_kind: str,
    action_label: str,
    safe_transcript: str,
) -> None:
    caplog.set_level("INFO")
    form_url = "https://broker.example.test/credential-form/bearer-session-secret"
    expires_at = "2026-08-29T04:06:47+00:00"

    class CredentialGateway(_StatefulFakeGateway):
        async def send_user_message(self, text: str, **kwargs):
            self.sent_user_messages.append({"text": text, **kwargs})
            return GatewayApplicationResponse(
                text=f"Open the secure broker form: {form_url}",
                conversation_id=kwargs.get("conversation_id") or "conv_secure_form",
                correlation_id="corr_secure_form",
                task_id="task_secure_form",
                message_type=message_type,
                transcript_text=safe_transcript,
                transient_action=GatewayTransientAction(
                    kind=action_kind,
                    label=action_label,
                    description="Use the secure form below to continue.",
                    url=form_url,
                    expires_at_utc=expires_at,
                ),
            )

    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    owner = config.user.canonical_user_id
    store.create_session(
        session_key="website:web-chat:secure-form",
        conversation_id="conv_secure_form",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        owner_canonical_user_id=owner,
    )
    gateway = CredentialGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )
    request_payload = {
        "message": "Store this credential",
        "session_key": "website:web-chat:secure-form",
        "conversation_id": "conv_secure_form",
        "client_request_id": "secure_form_request_1",
    }

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        cookie = auth_response.cookies["ua_web_chat_session"].value
        headers = {"Cookie": f"ua_web_chat_session={cookie}"}
        initial_response = await client.post(
            "/api/web-chat/chat", headers=headers, json=request_payload
        )
        initial_payload = await initial_response.json()
        retry_response = await client.post(
            "/api/web-chat/chat", headers=headers, json=request_payload
        )
        retry_payload = await retry_response.json()
        history_response = await client.get(
            "/api/web-chat/sessions/website:web-chat:secure-form/messages?limit=50",
            headers=headers,
        )
        history_payload = await history_response.json()
        events_response = await client.get("/api/web-chat/events", headers=headers)
        events_payload = await events_response.json()

    assert initial_response.status == 200
    assert initial_response.headers["Cache-Control"] == "no-store"
    ready_message = initial_payload["messages"][-1]
    assert initial_payload["transient"] == {
        "kind": action_kind,
        "item_id": f"{action_kind}:corr_secure_form",
        "correlation_id": "corr_secure_form",
        "session_sequence": ready_message["session_sequence"],
        "created_at_utc": ready_message["created_at_utc"],
        "content": "Use the secure form below to continue.",
        "action": {
            "label": action_label,
            "url": form_url,
            "expires_at_utc": expires_at,
        },
    }
    assert ready_message["content"] == safe_transcript
    assert "transient" not in retry_payload
    assert len(gateway.sent_user_messages) == 1
    assert form_url not in json.dumps(initial_payload["messages"], sort_keys=True)
    assert form_url not in initial_payload["transient"]["content"]
    assert form_url not in json.dumps(history_payload, sort_keys=True)
    assert form_url not in json.dumps(events_payload, sort_keys=True)
    assert form_url.encode() not in (tmp_path / "gateway.db").read_bytes()
    assert "event=web_chat_transient_action_included" in caplog.text
    assert form_url not in caplog.text


def test_web_chat_omits_memory_only_action_without_durable_timeline_anchor(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("ERROR")
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    app = InternalWebsiteInteractiveApp(
        config,
        gateway=_StatefulFakeGateway(store),
        bridge=SimpleNamespace(),
    )
    form_url = "https://broker.example.test/credential-form/bearer-session-secret"
    response = GatewayApplicationResponse(
        text="Open the secure form.",
        conversation_id="conv_missing_anchor",
        correlation_id="corr_missing_anchor",
        task_id="task_missing_anchor",
        message_type="secure_credential_collection",
        transcript_text="Secure credential entry is ready.",
        transient_action=GatewayTransientAction(
            kind="secure_credential_collection",
            label="Open secure credential form",
            description="Use the secure form below to continue.",
            url=form_url,
            expires_at_utc="2026-08-29T04:06:47+00:00",
        ),
    )

    payload = app.conversation._transient_action_payload(response, messages=())

    assert payload is None
    assert "event=web_chat_transient_action_position_missing" in caplog.text
    assert "correlation_id=corr_missing_anchor" in caplog.text
    assert form_url not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "provider", "handler"),
    [
        ("/link cli:github", "github", "_account_link_commands"),
        ("/link cli:google", "google", "_account_link_commands"),
        ("/link cli:x", "x", "_account_link_commands"),
        ("/auth bitwarden", "bitwarden", "_authentication_commands"),
    ],
)
async def test_web_chat_broker_auth_uses_memory_only_open_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    provider: str,
    handler: str,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(config, gateway=gateway)
    login_url = f"https://broker.example.test/auth/{provider}/v1.signed-test"
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    calls = []

    async def create_auth_session(**kwargs):
        calls.append(kwargs)
        return {"login_url": login_url, "expires_at": expires_at}

    monkeypatch.setattr(
        getattr(gateway.command_application, handler)._sign_ins,
        "_broker",
        SimpleNamespace(
            auth_unavailable_message=None,
            create_auth_session=create_auth_session,
            user_form_url=lambda url: url,
        ),
    )
    async with TestClient(TestServer(app.build_app())) as client:
        auth = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        headers = {
            "Cookie": f"ua_web_chat_session={auth.cookies['ua_web_chat_session'].value}"
        }
        session = await client.post("/api/web-chat/sessions", headers=headers)
        session_key = (await session.json())["session"]["session_key"]
        request = {
            "message": command,
            "session_key": session_key,
            "client_request_id": "broker_auth_test",
        }
        response = await client.post(
            "/api/web-chat/chat", headers=headers, json=request
        )
        payload = await response.json()
        retry = await client.post("/api/web-chat/chat", headers=headers, json=request)
        replay = await retry.json()
        events = await client.get("/api/web-chat/events", headers=headers)
        history = await events.json()

    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert len(calls) == 1
    transient = payload["transient"]
    assert transient["kind"] == "broker_authentication"
    assert transient["session_sequence"] == payload["messages"][-1]["session_sequence"]
    assert transient["action"] == {
        "label": "Open",
        "url": login_url,
        "expires_at_utc": expires_at,
    }
    assert login_url not in transient["content"]
    assert login_url not in json.dumps(payload["messages"])
    assert login_url not in json.dumps(history)
    assert login_url not in json.dumps(replay)
    assert "transient" not in replay


@pytest.mark.asyncio
async def test_web_chat_link_command_uses_memory_only_open_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(config, gateway=gateway)
    form_url = "https://broker.example.test/account-link/bearer-link-secret"
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    opened = []

    async def open_form(**request):
        opened.append(request)
        return AccountLinkForm(
            session_id="bearer-link-secret",
            form_url=form_url,
            status_url="https://broker.internal/v1/account-link/sessions/x",
            expires_at_utc=expires_at,
            tool_display_name="Portainer",
        )

    handler = gateway.command_application._account_link_commands
    monkeypatch.setattr(handler, "_account_links", SimpleNamespace(open_form=open_form))
    monkeypatch.setattr(
        handler,
        "_identity",
        SimpleNamespace(team_roles_for_person=lambda _person: ("member",)),
    )
    async with TestClient(TestServer(app.build_app())) as client:
        auth = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        headers = {
            "Cookie": f"ua_web_chat_session={auth.cookies['ua_web_chat_session'].value}"
        }
        session = await client.post("/api/web-chat/sessions", headers=headers)
        session_key = (await session.json())["session"]["session_key"]
        request = {
            "message": "/link http:portainer",
            "session_key": session_key,
            "client_request_id": "account_link_command_test",
        }
        response = await client.post(
            "/api/web-chat/chat", headers=headers, json=request
        )
        payload = await response.json()
        retry = await client.post("/api/web-chat/chat", headers=headers, json=request)
        replay = await retry.json()
        events = await client.get("/api/web-chat/events", headers=headers)
        history = await events.json()

    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert len(opened) == 1
    assert opened[0]["tool_id"] == "http:portainer"
    assert opened[0]["owner"] == "person"
    assert opened[0]["person_roles"] == ("member",)
    transient = payload["transient"]
    assert transient["kind"] == "account_link"
    assert transient["session_sequence"] == payload["messages"][-1]["session_sequence"]
    assert transient["action"] == {
        "label": "Open account link form",
        "url": form_url,
        "expires_at_utc": expires_at,
    }
    assert transient["content"].startswith(
        "Link an account to http:portainer (your own account)."
    )
    # History keeps a plain line in place of the card, never the form link.
    assert payload["messages"][-1]["content"] == (
        "Account link form opened for http:portainer (your own account)."
    )
    # The gateway watches the form once and posts the result in this chat,
    # tagged with this request's id so the card goes away.
    [watch] = handler._link_results.account_links
    assert watch["tool_id"] == "http:portainer"
    assert watch["restart_command"] == "/link http:portainer"
    assert watch["target"].session_key == session_key
    assert watch["target"].correlation_id == "account_link_command_test"
    assert transient["correlation_id"] == "account_link_command_test"
    assert form_url not in transient["content"]
    assert form_url not in json.dumps(payload["messages"])
    assert form_url not in json.dumps(history)
    assert form_url not in json.dumps(replay)
    assert "transient" not in replay


@pytest.mark.asyncio
async def test_web_chat_simultaneous_command_retry_executes_command_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )
    original_execute = app.application.execute_interactive_command
    execution_started = asyncio.Event()
    release_execution = asyncio.Event()
    execution_count = 0

    async def delayed_execute(**kwargs):
        nonlocal execution_count
        execution_count += 1
        execution_started.set()
        await release_execution.wait()
        return await original_execute(**kwargs)

    monkeypatch.setattr(
        app.application,
        "execute_interactive_command",
        delayed_execute,
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        cookie = auth_response.cookies["ua_web_chat_session"].value
        headers = {"Cookie": f"ua_web_chat_session={cookie}"}
        session_response = await client.post(
            "/api/web-chat/sessions",
            headers=headers,
        )
        session_payload = await session_response.json()
        request_payload = {
            "message": "/help",
            "session_key": session_payload["session"]["session_key"],
            "client_request_id": "simultaneous_command_1",
        }
        first_request = asyncio.create_task(
            client.post("/api/web-chat/chat", headers=headers, json=request_payload)
        )
        await asyncio.wait_for(execution_started.wait(), timeout=5)
        second_response = await client.post(
            "/api/web-chat/chat", headers=headers, json=request_payload
        )
        second_payload = await second_response.json()
        release_execution.set()
        first_response = await first_request
        first_payload = await first_response.json()

    assert execution_count == 1
    assert first_response.status == 200
    assert second_response.status == 200
    assert second_payload["status"] == "processing"
    assert len(second_payload["messages"]) == 1
    assert [message["message_id"] for message in first_payload["messages"][:1]] == [
        message["message_id"] for message in second_payload["messages"]
    ]


@pytest.mark.asyncio
async def test_website_app_user_bound_durable_events_filter_by_owner(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _StatefulFakeGateway(store)
    gateway.identity_service.resolve_web_token = lambda raw_token: (
        SimpleNamespace(
            user=SimpleNamespace(canonical_user_id="user_alpha", display_name="Alice")
        )
        if raw_token == "ua_user_token"
        else None
    )
    store.create_session(
        session_key="website:web-chat:owned",
        conversation_id="conv_owned",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="user_alpha",
        chat_id="web-chat",
        owner_canonical_user_id="user_alpha",
    )
    store.create_session(
        session_key="website:web-chat:other",
        conversation_id="conv_other",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="user_beta",
        chat_id="web-chat",
        owner_canonical_user_id="user_beta",
    )
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )
    await app.application.operations.emit_delivery_event(
        GatewayDeliveryEvent(
            text="Owned scheduled reply",
            session_key="website:web-chat:owned",
            channel_name="website",
            user_id="user_alpha",
            chat_id="web-chat",
            thread_id=None,
            conversation_id="conv_owned",
            scheduled_job_id="sched_owned",
            correlation_id="corr_owned",
            original_message_id=None,
        )
    )
    await app.application.operations.emit_delivery_event(
        GatewayDeliveryEvent(
            text="Other user's scheduled reply",
            session_key="website:web-chat:other",
            channel_name="website",
            user_id="user_beta",
            chat_id="web-chat",
            thread_id=None,
            conversation_id="conv_other",
            scheduled_job_id="sched_other",
            correlation_id="corr_other",
            original_message_id=None,
        )
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "ua_user_token"}
        )
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        events_response = await client.get(
            "/api/web-chat/events",
            headers={"Cookie": f"ua_web_chat_session={session_cookie}"},
        )
        payload = await events_response.json()

    assert events_response.status == 200
    assert [event["content"] for event in payload["messages"]] == [
        "Owned scheduled reply"
    ]
    assert payload["next_cursor"]
    assert not payload["next_cursor"].isdigit()
    # Every live page carries the team's status for the activity mesh,
    # Settings → Team and the Tasks view (one read for all three).
    [member] = payload["team_activity"]["members"]
    assert (member["agent_id"], member["status"]["key"]) == ("agent_architect", "available")


@pytest.mark.asyncio
async def test_workspace_browser_shows_members_their_own_folder_and_renders_safe_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every signed-in team member may browse: members see only their own folder,
    administrators also the shared area. Roles come from the team, not the browser."""

    from runtime_settings.instance_layout import person_folder_name

    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    workspace = config.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    # The bootstrap token signs in as config.user, an owner here; ua_user_token as user_alpha, a member.
    admin_id = config.user.canonical_user_id
    member_folder = workspace / "users" / person_folder_name("user_alpha")
    member_folder.mkdir(parents=True)
    (member_folder / "notes.md").write_text("# Alice's notes\n", encoding="utf-8")
    (workspace / "users" / person_folder_name(admin_id)).mkdir(parents=True)
    (workspace / "README.md").write_text("# Rendered Markdown\n", encoding="utf-8")
    json_content = '{"name":"<script>alert(1)</script>","enabled":true}\n'
    (workspace / "settings.json").write_text(json_content, encoding="utf-8")
    html_content = (
        "<!doctype html><style>h1{color:green}</style>"
        "<h1>Rendered HTML</h1><script>window.parent.document.body.remove()</script>"
    )
    (workspace / "page.html").write_text(html_content, encoding="utf-8")
    (workspace / "archive.bin").write_bytes(b"\x00\x01")
    (workspace / "debug.env").write_text("TOKEN=secret\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "outside-link.txt").symlink_to(outside)
    (workspace / "outside-hard-link.txt").hardlink_to(outside)
    gateway = _UserTokenFakeGateway()
    team_roles = {admin_id: ("owner",), "user_alpha": ("member",)}
    gateway.access_inbox = SimpleNamespace(
        person=lambda person_id: (
            SimpleNamespace(roles=team_roles[person_id]) if person_id in team_roles else None
        )
    )
    app = InternalWebsiteInteractiveApp(
        config,
        gateway=gateway,
        bridge=SimpleNamespace(),
    )

    async with TestClient(TestServer(app.build_app())) as client:
        unauthorized = await client.get("/api/web-chat/workspace")
        bootstrap_auth = await client.post(
            "/api/web-chat/auth",
            json={"token": "uaweb_test_token"},
        )
        bootstrap_cookie = bootstrap_auth.cookies["ua_web_chat_session"].value
        bootstrap_headers = {
            "Cookie": f"ua_web_chat_session={bootstrap_cookie}",
        }
        listing = await client.get(
            "/api/web-chat/workspace",
            headers=bootstrap_headers,
        )
        listing_payload = await listing.json()
        markdown = await client.get(
            "/api/web-chat/workspace/file?path=README.md",
            headers=bootstrap_headers,
        )
        markdown_payload = await markdown.json()
        json_preview = await client.get(
            "/api/web-chat/workspace/file?path=settings.json",
            headers=bootstrap_headers,
        )
        json_payload = await json_preview.json()
        html_metadata = await client.get(
            "/api/web-chat/workspace/file?path=page.html",
            headers=bootstrap_headers,
        )
        html_metadata_payload = await html_metadata.json()
        html_response = await client.get(
            "/api/web-chat/workspace/content?path=page.html",
            headers=bootstrap_headers,
        )
        html_response_text = await html_response.text()
        traversal = await client.get(
            "/api/web-chat/workspace/file?path=..%2Foutside.txt",
            headers=bootstrap_headers,
        )
        secret = await client.get(
            "/api/web-chat/workspace/file?path=debug.env",
            headers=bootstrap_headers,
        )
        symlink = await client.get(
            "/api/web-chat/workspace/file?path=outside-link.txt",
            headers=bootstrap_headers,
        )
        hard_link = await client.get(
            "/api/web-chat/workspace/file?path=outside-hard-link.txt",
            headers=bootstrap_headers,
        )
        unsupported_content = await client.get(
            "/api/web-chat/workspace/content?path=archive.bin",
            headers=bootstrap_headers,
        )
        admin_users = await client.get(
            "/api/web-chat/workspace?path=users",
            headers=bootstrap_headers,
        )
        admin_users_payload = await admin_users.json()
        admin_other_folder = await client.get(
            f"/api/web-chat/workspace?path=users%2F{person_folder_name('user_alpha')}",
            headers=bootstrap_headers,
        )
        user_auth = await client.post(
            "/api/web-chat/auth",
            json={"token": "ua_user_token"},
        )
        user_cookie = user_auth.cookies["ua_web_chat_session"].value
        member_headers = {"Cookie": f"ua_web_chat_session={user_cookie}"}
        member_root = await client.get("/api/web-chat/workspace", headers=member_headers)
        member_root_payload = await member_root.json()
        member_users = await client.get(
            "/api/web-chat/workspace?path=users", headers=member_headers
        )
        member_users_payload = await member_users.json()
        member_own_file = await client.get(
            f"/api/web-chat/workspace/file?path=users%2F{person_folder_name('user_alpha')}%2Fnotes.md",
            headers=member_headers,
        )
        member_own_file_payload = await member_own_file.json()
        member_shared_file = await client.get(
            "/api/web-chat/workspace/file?path=README.md",
            headers=member_headers,
        )
        member_other_folder = await client.get(
            f"/api/web-chat/workspace?path=users%2F{person_folder_name(admin_id)}",
            headers=member_headers,
        )
        team_roles.pop("user_alpha")
        former_member = await client.get(
            "/api/web-chat/workspace", headers=member_headers
        )

    assert unauthorized.status == 401
    assert listing.status == 200
    assert {entry["name"] for entry in listing_payload["entries"]} == {
        "README.md",
        "archive.bin",
        "page.html",
        "settings.json",
        "users",
    }
    # Administrators see only their own folder under users/; other people's stay private.
    assert [entry["name"] for entry in admin_users_payload["entries"]] == [
        person_folder_name(admin_id)
    ]
    assert admin_other_folder.status == 403
    # Members see only users/ at the top and only their own folder inside it.
    assert member_root.status == 200
    assert [entry["name"] for entry in member_root_payload["entries"]] == ["users"]
    assert [entry["name"] for entry in member_users_payload["entries"]] == [
        person_folder_name("user_alpha")
    ]
    assert member_own_file.status == 200
    assert member_own_file_payload["content"] == "# Alice's notes\n"
    assert member_shared_file.status == 403
    assert member_other_folder.status == 403
    assert former_member.status == 403
    assert markdown.status == 200
    assert markdown_payload["preview_kind"] == "markdown"
    assert markdown_payload["content"] == "# Rendered Markdown\n"
    assert json_preview.status == 200
    assert json_payload["preview_kind"] == "text"
    assert json_payload["content"] == json_content
    assert json_payload["syntax_language"] == "JSON"
    assert json_payload["formatted_content"].startswith('{\n  "name":')
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in json_payload["highlighted_html"]
    assert "<script>" not in json_payload["highlighted_html"]
    assert html_metadata.status == 200
    assert html_metadata_payload["preview_kind"] == "html"
    assert "content" not in html_metadata_payload
    assert html_response.status == 200
    assert html_response.content_type == "text/html"
    assert html_response_text == html_content
    assert "sandbox" in html_response.headers["Content-Security-Policy"]
    assert "script-src 'none'" in html_response.headers["Content-Security-Policy"]
    assert html_response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert traversal.status == 403
    assert secret.status == 403
    assert symlink.status == 403
    assert hard_link.status == 403
    assert unsupported_content.status == 415


@pytest.mark.asyncio
async def test_web_chat_team_api_requires_auth_and_returns_redacted_presence(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _UserTokenStatefulFakeGateway(store)
    store.create_session(
        session_key="website:web-chat:team",
        conversation_id="conv_team",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="user_alpha",
        chat_id="web-chat",
        owner_canonical_user_id="user_alpha",
    )
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        unauthorized = await client.get("/api/web-chat/team")
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "ua_user_token"}
        )
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        response = await client.get(
            "/api/web-chat/team?session_key=website%3Aweb-chat%3Ateam",
            headers={"Cookie": f"ua_web_chat_session={session_cookie}"},
        )
        payload = await response.json()

    assert unauthorized.status == 401
    assert response.status == 200
    assert payload == {
        "status": "ok",
        "authorization_presentation_mode": "friendly",
        "members": [
            {
                "agent_id": "agent_architect",
                "name": "Nova",
                "named_agent_type": "TeamArchitect",
                "is_team_lead": True,
                "status": {
                    "key": "available",
                    "label": "Available",
                    "tone": "success",
                },
                "available_skills": ["prototype", "runtime-operations"],
                "last_seen_at": "2026-08-11T12:00:00+00:00",
            }
        ],
        "mcp_connections": [],
        "hosted_mcp_servers": [],
    }


@pytest.mark.asyncio
async def test_web_chat_memory_api_is_scoped_csrf_protected_and_digest_bound(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _UserTokenStatefulFakeGateway(store)
    session_key = "website:web-chat:memories"
    store.create_session(
        session_key=session_key,
        conversation_id="conv_memories",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="user_alpha",
        chat_id="web-chat",
        owner_canonical_user_id="user_alpha",
    )
    app = InternalWebsiteInteractiveApp(
        config,
        gateway=gateway,
        bridge=SimpleNamespace(),
    )

    async with TestClient(TestServer(app.build_app())) as client:
        unauthorized = await client.get(
            f"/api/web-chat/memories?session_key={session_key}&scope=personal"
        )
        auth_response = await client.post(
            "/api/web-chat/auth",
            json={"token": "ua_user_token"},
        )
        auth_payload = await auth_response.json()
        list_response = await client.get(
            f"/api/web-chat/memories?session_key={session_key}&scope=personal&limit=999&offset=20"
        )
        list_payload = await list_response.json()
        edit_body = {
            "session_key": session_key,
            "record_digest": "a" * 64,
            "fact_text": "Prefer focused release notes.",
            "tags": ["writing", "release"],
            "invalid_after_utc": "2026-12-31T23:59:00Z",
        }
        missing_csrf = await client.patch(
            "/api/web-chat/memories/personal/7",
            json=edit_body,
        )
        headers = {
            "Origin": str(client.make_url("/")).rstrip("/"),
            "Sec-Fetch-Site": "same-origin",
            "X-UA-Permission-CSRF": auth_payload["permission_csrf_token"],
        }
        malformed_tags = await client.patch(
            "/api/web-chat/memories/personal/7",
            headers=headers,
            json={**edit_body, "tags": "writing"},
        )
        edit_response = await client.patch(
            "/api/web-chat/memories/personal/7",
            headers=headers,
            json=edit_body,
        )
        edit_payload = await edit_response.json()

        async def reject_stale_edit(**_request):
            raise WebMemoryConflictError(
                "usermem #7 changed after it was loaded. Refresh and try again."
            )

        gateway.mutate_web_chat_memory = reject_stale_edit
        stale_response = await client.patch(
            "/api/web-chat/memories/personal/7",
            headers=headers,
            json=edit_body,
        )

    assert unauthorized.status == 401
    assert auth_response.status == 200
    assert list_response.status == 200
    assert list_payload["scope"] == "personal"
    assert list_payload["limit"] == 20
    assert list_payload["offset"] == 20
    assert list_payload["memories"][0]["fact_text"] == ("Prefer concise release notes.")
    assert list_payload["memories"][0]["available_actions"] == ["edit", "delete"]
    assert list_payload["memories"][0]["record_digest"] == "a" * 64
    assert missing_csrf.status == 403
    assert malformed_tags.status == 400
    assert edit_response.status == 200
    assert edit_payload["memory"]["fact_text"] == "Prefer focused release notes."
    assert stale_response.status == 409
    assert gateway.memory_calls[0][0] == "list"
    assert gateway.memory_calls[0][1]["canonical_user_id"] == "user_alpha"
    assert gateway.memory_calls[1] == (
        "mutate",
        {
            "session_key": session_key,
            "channel_name": "website",
            "user_id": "user_alpha",
            "chat_id": "web-chat",
            "canonical_user_id": "user_alpha",
            "scope": "personal",
            "memory_id": 7,
            "action": "edit",
            "expected_record_digest": "a" * 64,
            "fact_text": "Prefer focused release notes.",
            "tags": ("writing", "release"),
            "invalid_after_utc": datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc),
        },
    )


@pytest.mark.asyncio
async def test_web_chat_mcp_decision_is_csrf_protected_and_digest_bound(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _MCPFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )
    session_key = "website:web-chat:mcp-decision"

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "ua_user_token"}
        )
        auth_payload = await auth_response.json()
        cookie = auth_response.cookies["ua_web_chat_session"].value
        origin = str(client.make_url("/")).rstrip("/")
        headers = {
            "Cookie": f"ua_web_chat_session={cookie}",
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
            "X-UA-Permission-CSRF": auth_payload["permission_csrf_token"],
        }
        await client.post(
            "/api/web-chat/session-context",
            headers=headers,
            json={"session_key": session_key},
        )
        denied = await client.post(
            "/api/web-chat/mcp-connections/mcp_conn_exact/decision",
            headers={"Cookie": f"ua_web_chat_session={cookie}"},
            json={
                "session_key": session_key,
                "decision": "approve",
                "expected_record_digest": "b" * 64,
            },
        )
        response = await client.post(
            "/api/web-chat/mcp-connections/mcp_conn_exact/decision",
            headers=headers,
            json={
                "session_key": session_key,
                "decision": "approve",
                "expected_record_digest": "b" * 64,
            },
        )
        payload = await response.json()

    assert denied.status == 403
    assert response.status == 200
    assert payload["request_digest"] == "b" * 64
    assert payload["response_digest"] == "d" * 64
    assert gateway.permission_calls == [
        (
            "decide_mcp_connection",
            {
                "connection_id": "mcp_conn_exact",
                "expected_record_digest": "b" * 64,
                "decision": "approve",
                "reason": "",
                "session_key": session_key,
                "channel_name": "website",
                "user_id": "user_alpha",
                "chat_id": "web-chat",
                "canonical_user_id": "user_alpha",
            },
        )
    ]


@pytest.mark.asyncio
async def test_web_chat_shell_protects_permission_ui_from_framing_and_caching(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    app = InternalWebsiteInteractiveApp(
        config, gateway=_FakeGateway(), bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        response = await client.get("/webchat")
        markup = await response.text()

    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert 'id="chat-settings-overlay"' in markup
    assert '<h2 id="settings-dialog-title">Settings</h2>' in markup
    assert 'id="settings-inbox-tab"' in markup
    assert 'id="settings-inbox-count"' in markup
    assert 'id="settings-inbox-panel"' in markup
    assert 'id="settings-access-tab"' in markup
    assert 'id="settings-access-panel"' in markup
    assert 'id="settings-access-map"' in markup
    assert 'id="settings-memories-tab"' in markup
    assert 'id="settings-memories-panel"' in markup
    assert 'id="settings-appearance-tab"' in markup
    assert 'id="settings-appearance-panel"' in markup
    assert "Choose the active theme and tune its background." in markup
    assert "Reset to Defaults" in markup
    assert "Changes apply to this browser" not in markup
    assert 'id="memory-personal-tab"' in markup
    assert 'id="memory-team-tab"' in markup
    # Settings is an ES module imported by chat.js; the old Permissions tab
    # and its classic script are gone.
    assert "permission_controls.js" not in markup
    assert 'id="settings-permissions-tab"' not in markup
    assert 'id="settings-procedures"' in markup
    assert re.search(r'src="/chat\.js\?v=[0-9a-f]{16}"', markup)
    assert re.search(r'src="/appearance\.js\?v=[0-9a-f]{16}"', markup)
    assert re.search(r'src="/appearance_customizer\.js\?v=[0-9a-f]{16}"', markup)
    assert re.search(r'src="/metallic_mesh\.js\?v=[0-9a-f]{16}"', markup)
    assert re.search(r'src="/chat_delivery\.js\?v=[0-9a-f]{16}"', markup)


class _RetentionChanges:
    """Who may change file retention: the person check's answer, switchable by the test."""

    def __init__(self) -> None:
        self.allowed: set[str] = set()

    def may_change(self, person_id, kind) -> bool:
        return person_id in self.allowed


@pytest.mark.asyncio
async def test_file_retention_is_shown_to_members_and_saved_only_by_those_who_may_change_it(
    tmp_path: Path,
) -> None:
    from assistant_gateway.file_retention import GatewayFileRetention
    from assistant_gateway.upload_retention import UploadRetention

    config = _config(tmp_path)
    config.gateway.users.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.identity_repository.create_gateway_user(
        canonical_user_id="user_alpha", display_name="Alice", timezone="UTC"
    )
    gateway = _UserTokenStatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    changes = _RetentionChanges()
    uploads = UploadRetention(records=store, uploads_dir=config.layout.uploads_dir, retention_days=90)
    gateway.file_retention = GatewayFileRetention(
        config_path=config.config_path, person_changes=changes, uploads=uploads
    )
    saved_settings = config.layout.instance_config_path

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post("/api/web-chat/auth", json={"token": "ua_user_token"})
        auth_payload = await auth_response.json()
        read_headers = {"Cookie": f"ua_web_chat_session={auth_response.cookies['ua_web_chat_session'].value}"}
        write_headers = {
            **read_headers,
            "Origin": str(client.make_url("/")).rstrip("/"),
            "Sec-Fetch-Site": "same-origin",
            "X-UA-Permission-CSRF": auth_payload["permission_csrf_token"],
        }
        member_view = await (await client.get("/api/web-chat/file-retention", headers=read_headers)).json()
        before = saved_settings.read_bytes()
        refused = await client.put(
            "/api/web-chat/file-retention", headers=write_headers, json={"upload_days": 30, "scratch_days": 7, "output_days": 60}
        )
        refused_payload = await refused.json()
        after_refusal = saved_settings.read_bytes()

        changes.allowed.add("user_alpha")
        missing_csrf = await client.put(
            "/api/web-chat/file-retention", headers=read_headers, json={"upload_days": 30, "scratch_days": 7, "output_days": 60}
        )
        invalid = await client.put(
            "/api/web-chat/file-retention", headers=write_headers, json={"upload_days": 30, "scratch_days": 7, "output_days": -1}
        )
        saved = await client.put(
            "/api/web-chat/file-retention", headers=write_headers, json={"upload_days": 30, "scratch_days": 7, "output_days": 60}
        )
        saved_payload = await saved.json()
        admin_view = await (await client.get("/api/web-chat/file-retention", headers=read_headers)).json()

    assert member_view == {
        "status": "ok",
        "upload_days": 90,
        "scratch_days": 21,
        "output_days": 90,
        "max_days": 3650,
        "can_change": False,
    }
    assert refused.status == 403
    assert refused_payload["error"] == "Only team administrators can change how long files are kept."
    assert after_refusal == before
    assert missing_csrf.status == 403
    assert invalid.status == 400
    assert saved.status == 200
    assert (saved_payload["upload_days"], saved_payload["scratch_days"], saved_payload["output_days"]) == (30, 7, 60)
    assert admin_view["can_change"] is True
    assert (admin_view["upload_days"], admin_view["scratch_days"], admin_view["output_days"]) == (30, 7, 60)
    reloaded = type(config).load_saved(config.config_path)
    assert reloaded.interactive_apps.internal_website.attachments.retention_days == 30
    assert reloaded.tool_policy.scratch_retention_days == 7
    assert reloaded.tool_policy.output_retention_days == 60
    assert uploads.retention_days == 30


@pytest.mark.asyncio
async def test_web_chat_appearance_is_validated_and_persisted_for_signed_in_user(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.gateway.users.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.identity_repository.create_gateway_user(
        canonical_user_id="user_alpha",
        display_name="Alice",
        timezone="UTC",
    )
    gateway = _UserTokenStatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config,
        gateway=gateway,
        bridge=SimpleNamespace(),
    )

    async with TestClient(TestServer(app.build_app())) as client:
        unauthorized = await client.get("/api/web-chat/appearance")
        auth_response = await client.post(
            "/api/web-chat/auth",
            json={"token": "ua_user_token"},
        )
        auth_payload = await auth_response.json()
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        read_headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        write_headers = {
            **read_headers,
            "Origin": str(client.make_url("/")).rstrip("/"),
            "Sec-Fetch-Site": "same-origin",
            "X-UA-Permission-CSRF": auth_payload["permission_csrf_token"],
        }
        empty_response = await client.get(
            "/api/web-chat/appearance",
            headers=read_headers,
        )
        empty_payload = await empty_response.json()
        missing_csrf = await client.put(
            "/api/web-chat/appearance",
            headers=read_headers,
            json={"appearance": {"color_mode": "light"}},
        )
        invalid_response = await client.put(
            "/api/web-chat/appearance",
            headers=write_headers,
            json={"appearance": {"polygon_seed": 0}},
        )
        save_response = await client.put(
            "/api/web-chat/appearance",
            headers=write_headers,
            json={
                "appearance": {
                    "color_mode": "light",
                    "polygon_seed": 8675309,
                    "light_diffusion": 72,
                }
            },
        )
        saved_payload = await save_response.json()
        loaded_response = await client.get(
            "/api/web-chat/appearance",
            headers=read_headers,
        )
        loaded_payload = await loaded_response.json()
        reset_response = await client.delete(
            "/api/web-chat/appearance",
            headers=write_headers,
        )
        reset_payload = await reset_response.json()
        empty_after_reset = await client.get(
            "/api/web-chat/appearance",
            headers=read_headers,
        )
        empty_after_reset_payload = await empty_after_reset.json()

    assert unauthorized.status == 401
    assert empty_response.status == 200
    assert empty_payload["appearance"] is None
    assert missing_csrf.status == 403
    assert invalid_response.status == 400
    assert save_response.status == 200
    assert saved_payload["appearance"]["color_mode"] == "light"
    assert saved_payload["appearance"]["polygon_seed"] == 8675309
    assert saved_payload["appearance"]["light_diffusion"] == 72
    assert loaded_response.status == 200
    assert loaded_payload == saved_payload
    assert loaded_response.headers["Cache-Control"] == "no-store"
    assert reset_response.status == 200
    assert reset_payload["appearance"] is None
    assert empty_after_reset.status == 200
    assert empty_after_reset_payload["appearance"] is None


@pytest.mark.asyncio
async def test_saved_appearance_drops_retired_dark_tuning_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A preference saved with dark settings that no longer exist still loads.

    Only its dark part is replaced by the defaults (the old values were tuned
    for a lighting that is gone); the person's mode and light-theme choices
    stay, and the cleaned preference is saved so this happens once.
    """

    config = _config(tmp_path)
    config.gateway.users.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.identity_repository.create_gateway_user(
        canonical_user_id="user_alpha",
        display_name="Alice",
        timezone="UTC",
    )
    saved = OnboardingAppearanceSettings(color_mode="light", light_diffusion=33).model_dump(mode="json")
    saved["dark"] = {
        "surface_color": "#030202",
        "facet_depth": 22,
        "facet_size": 220,
        "metallic_seams": 30,
        "surface_grain": 50,
        "shimmer_count": 9,
        "shimmer_speed": 250,
        "shimmer_width": 25,
        "mesh_morphing": False,
        "mesh_morph_speed": 100,
        "seed": 9187,
        "accents": {
            "gold": {"color": "#618500", "strength": 89},
            "silver": {"color": "#1a1414", "strength": 65},
            "bronze": {"color": "#ae6832", "strength": 30},
        },
    }
    store.identity_repository.upsert_gateway_user_appearance(
        canonical_user_id="user_alpha",
        appearance=saved,
    )
    gateway = _UserTokenStatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    caplog.set_level("WARNING")

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post("/api/web-chat/auth", json={"token": "ua_user_token"})
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        first = await client.get("/api/web-chat/appearance", headers=headers)
        first_payload = await first.json()
        warnings_after_first = caplog.text.count("event=web_chat_appearance_retired_dark_dropped")
        second = await client.get("/api/web-chat/appearance", headers=headers)
        second_payload = await second.json()

    assert first.status == 200
    assert first_payload["appearance"]["color_mode"] == "light"
    assert first_payload["appearance"]["light_diffusion"] == 33
    assert first_payload["appearance"]["dark"] == DarkMetalAppearanceSettings().model_dump(mode="json")
    assert warnings_after_first == 1
    assert "canonical_user_id=user_alpha" in caplog.text
    assert "shimmer_count" in caplog.text
    assert second.status == 200
    assert second_payload == first_payload
    assert caplog.text.count("event=web_chat_appearance_retired_dark_dropped") == 1
    stored = store.identity_repository.get_gateway_user_appearance("user_alpha")
    assert stored is not None
    assert "shimmer_count" not in stored.appearance["dark"]


@pytest.mark.asyncio
async def test_operator_ui_assets_cannot_mix_cached_release_versions(
    tmp_path: Path,
) -> None:
    from assistant_gateway.interactive_apps.website_support import ONBOARDING_ASSET_ROOT

    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    app = InternalWebsiteInteractiveApp(
        config, gateway=_FakeGateway(), bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        web_chat_shell = await client.get("/webchat")
        config_editor_shell = await client.get("/config-editor")
        responses = [
            await client.get("/styles.css?v=ignored-by-route"),
            await client.get("/chat.js?v=ignored-by-route"),
            await client.get("/chat_delivery.js?v=ignored-by-route"),
            await client.get("/action_feedback.js?v=ignored-by-route"),
            await client.get("/appearance.js?v=ignored-by-route"),
            await client.get("/appearance_customizer.js?v=ignored-by-route"),
            await client.get("/metallic_mesh.js?v=ignored-by-route"),
            await client.get("/agent-mark.png?v=ignored-by-route"),
            config_editor_shell,
        ]
        imports = set(re.findall(r'from "\./([\w.]+)"', module_source(ONBOARDING_ASSET_ROOT, "chat.js")))
        for module in imports:
            response = await client.get("/" + module)
            assert response.status == 200, f"Chat imports an unserved module: {module}"
            responses.append(response)
        web_chat_markup = await web_chat_shell.text()
        config_editor_markup = await config_editor_shell.text()

    assert all(response.status == 200 for response in responses)
    assert all(
        response.headers["Cache-Control"] == "no-store" for response in responses
    )
    chat_revision = re.search(r"/styles\.css\?v=([0-9a-f]{16})", web_chat_markup)
    editor_revision = re.search(r"/styles\.css\?v=([0-9a-f]{16})", config_editor_markup)
    assert chat_revision is not None
    assert editor_revision is not None
    assert chat_revision.group(1) == editor_revision.group(1)
    assert f"/chat.js?v={chat_revision.group(1)}" in web_chat_markup
    assert f"/chat_delivery.js?v={chat_revision.group(1)}" in web_chat_markup
    assert f"/action_feedback.js?v={chat_revision.group(1)}" in web_chat_markup
    assert f"/action_feedback.js?v={chat_revision.group(1)}" in config_editor_markup
    assert f"/config-editor.js?v={chat_revision.group(1)}" in config_editor_markup


@pytest.mark.asyncio
async def test_website_app_serves_sites_health_and_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    _write_site(config, title="Motor Repair Guide")
    _write_site(config, "someone-else", owner="usr_other", title="Other Person Page")
    app = InternalWebsiteInteractiveApp(
        config, gateway=_FakeGateway(), bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        health_response = await client.get("/health")
        root_redirect = await client.get("/", allow_redirects=False)
        docs_response = await client.get("/docs/")
        setup_response = await client.get("/docker-compose-setup.html")
        anonymous_index = await (await client.get("/docs/sites/")).text()
        headers = await _signed_in(client)
        index_response = await client.get("/docs/sites/", headers=headers)
        site_response = await client.get("/docs/sites/demo/")
        other_site_response = await client.get("/docs/sites/someone-else/")
        missing_response = await client.get("/docs/sites/bad!id/")

        health = await health_response.json()
        docs_text = await docs_response.text()
        setup_text = await setup_response.text()
        index_text = await index_response.text()
        site_text = await site_response.text()

    assert health_response.status == 200
    assert health["base_url"] == "http://127.0.0.1:18500"
    assert health["generated_site_chat_enabled"] is True
    assert root_redirect.status == 301
    assert root_redirect.headers["Location"] == "/docs/"
    assert docs_response.status == 200
    assert "Universal Agents" in docs_text
    assert setup_response.status == 200
    assert setup_text == (Path(__file__).resolve().parents[1] / "docker-compose-setup.html").read_text(encoding="utf-8")
    # The index lists only the signed-in person's pages; without a session it
    # asks the person to sign in instead of listing anything.
    assert "Sign in to" in anonymous_index
    assert "Motor Repair Guide" not in anonymous_index
    assert "Motor Repair Guide" in index_text
    assert "Other Person Page" not in index_text
    # A page link opens without signing in (the id is the secret part).
    assert site_response.status == 200
    assert "Motor Repair Guide" in site_text
    assert other_site_response.status == 200
    assert missing_response.status == 404


@pytest.mark.asyncio
async def test_website_app_serves_docker_compose_setup_when_web_chat_disabled(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = False
    app = InternalWebsiteInteractiveApp(
        config, gateway=_FakeGateway(), bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        setup_response = await client.get("/docker-compose-setup.html")
        web_chat_response = await client.get("/webchat")
        setup_text = await setup_response.text()

    assert setup_response.status == 200
    assert setup_text == (Path(__file__).resolve().parents[1] / "docker-compose-setup.html").read_text(encoding="utf-8")
    assert web_chat_response.status == 404


@pytest.mark.asyncio
async def test_website_app_routes_chat_to_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    _write_site(config, site_id="motor-guide")
    # A per-test database: the in-memory fake store is a shared file.
    gateway = _StatefulFakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        response = await client.post(
            "/api/sites/motor-guide/chat",
            headers=await _signed_in(client),
            json={
                "message": "What bearing should I buy?",
                "client_id": "browser_1",
                "conversation_id": "conv_existing",
                "message_id": "msg_browser_1",
            },
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["reply"] == "Reply: What bearing should I buy?"
    assert payload["conversation_id"] == "conv_existing"
    assert gateway.sent_user_messages == [
        {
            "text": "What bearing should I buy?",
            "session_key": "website:motor-guide:browser_1",
            "conversation_id": "conv_existing",
            "channel_name": "website",
            "user_id": "browser_1",
            "chat_id": "motor-guide",
            "thread_id": "motor-guide",
            "original_message_id": "msg_browser_1",
            "query_handler": None,
            # Sent as the signed-in owner of the page, never as the default person.
            "canonical_user_id": config.user.canonical_user_id,
            "attachments": None,
            # Page chats have no reply feature.
            "replied_message": None,
        }
    ]


@pytest.mark.asyncio
async def test_generated_site_does_not_treat_legacy_newuser_as_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    _write_site(config, site_id="motor-guide")
    # A per-test database: the in-memory fake store is a shared file.
    gateway = _StatefulFakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        response = await client.post(
            "/api/sites/motor-guide/chat",
            headers=await _signed_in(client),
            json={"message": "/newuser list", "client_id": "browser_1"},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["reply"] == "Reply: /newuser list"
    assert gateway.sent_user_messages[0]["text"] == "/newuser list"


@pytest.mark.asyncio
async def test_website_app_reuses_mapped_conversation_session_without_rebinding_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    _write_site(config, site_id="motor-guide")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="session_origin",
        conversation_id="conv_origin",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    store.upsert_session(
        session_key="session_origin",
        conversation_id="conv_origin",
        last_task_id="task_origin",
        manager_agent_id="manager_1",
    )
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        response = await client.post(
            "/api/sites/motor-guide/chat",
            headers=await _signed_in(client),
            json={
                "message": "Continue this guide.",
                "client_id": "browser_1",
                "conversation_id": "conv_origin",
            },
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["session_key"] == "session_origin"
    sent = gateway.sent_user_messages[0]
    assert sent["session_key"] == "session_origin"
    assert sent["conversation_id"] == "conv_origin"
    assert sent["channel_name"] is None
    assert sent["chat_id"] is None
    assert store.get_session("session_origin").channel_name == "telegram"
    assert store.get_session("session_origin").chat_id == "chat_1"


@pytest.mark.asyncio
async def test_website_app_mirrors_web_chat_to_original_channel_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    _write_site(config, site_id="motor-guide")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="session_origin",
        conversation_id="conv_origin",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
        thread_id="topic_1",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    store.upsert_session(
        session_key="session_origin",
        conversation_id="conv_origin",
        last_task_id="task_origin",
        manager_agent_id="manager_1",
    )
    gateway = _StatefulFakeGateway(store)
    mirrored_events = []
    gateway.event_sink = lambda event: mirrored_events.append(event)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        response = await client.post(
            "/api/sites/motor-guide/chat",
            headers=await _signed_in(client),
            json={
                "message": "Continue this guide.",
                "client_id": "browser_1",
                "conversation_id": "conv_origin",
                "mirror_to_origin": True,
            },
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["mirrored"] is True
    assert len(mirrored_events) == 1
    event = mirrored_events[0]
    assert event.session_key == "session_origin"
    assert event.channel_name == "telegram"
    assert event.chat_id == "chat_1"
    assert event.thread_id == "topic_1"
    assert event.conversation_id == "conv_origin"
    assert "Web follow-up on motor-guide" in event.text
    assert "User: Continue this guide." in event.text
    assert "Assistant: Reply: Continue this guide." in event.text


@pytest.mark.asyncio
async def test_website_app_mirrors_generated_site_chat_back_to_web_chat_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    _write_site(config, site_id="motor-guide")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="website:web-chat:browser_1",
        conversation_id="conv_origin",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="browser_1",
        chat_id="web-chat",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    store.upsert_session(
        session_key="website:web-chat:browser_1",
        conversation_id="conv_origin",
        last_task_id="task_origin",
        manager_agent_id="manager_1",
    )
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        headers = await _signed_in(client)
        site_response = await client.post(
            "/api/sites/motor-guide/chat",
            headers=headers,
            json={
                "message": "Continue this guide from the how-to page.",
                "client_id": "browser_1",
                "session_key": "website:web-chat:browser_1",
                "conversation_id": "conv_origin",
                "mirror_to_origin": True,
            },
        )
        site_payload = await site_response.json()
        events_response = await client.get(
            "/api/web-chat/events",
            headers=headers,
        )
        events_payload = await events_response.json()

    assert site_response.status == 200
    assert site_payload["mirrored"] is True
    assert events_response.status == 200
    assert len(events_payload["messages"]) == 1
    event = events_payload["messages"][0]
    assert event["session_key"] == "website:web-chat:browser_1"
    assert event["conversation_id"] == "conv_origin"
    assert "Web follow-up on motor-guide" in event["content"]
    assert "User: Continue this guide from the how-to page." in event["content"]
    assert (
        "Assistant: Reply: Continue this guide from the how-to page."
        in event["content"]
    )


@pytest.mark.asyncio
async def test_website_app_attaches_new_conversation_to_embedded_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    _write_site(config, site_id="focused-guide")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="session_origin",
        conversation_id="conv_old",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    store.upsert_session(
        session_key="session_origin",
        conversation_id="conv_old",
        last_task_id="task_old",
        manager_agent_id="manager_1",
    )
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        response = await client.post(
            "/api/sites/focused-guide/chat",
            headers=await _signed_in(client),
            json={
                "message": "Start from the focused site context.",
                "client_id": "browser_1",
                "session_key": "session_origin",
                "conversation_id": "conv_focused",
            },
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["session_key"] == "session_origin"
    mapping = store.get_session_conversation("conv_focused")
    assert mapping is not None
    assert mapping.session_key == "session_origin"
    assert mapping.last_task_id is None
    assert store.get_session_conversation("conv_old").last_task_id == "task_old"
    sent = gateway.sent_user_messages[0]
    assert sent["session_key"] == "session_origin"
    assert sent["conversation_id"] == "conv_focused"
    assert sent["channel_name"] is None


@pytest.mark.asyncio
async def test_website_app_handles_new_and_pending_chat_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    _write_site(config)
    # A per-test database: the in-memory fake store is a shared file.
    gateway = _StatefulFakeGateway(GatewayStateStore(str(tmp_path / "gateway.db")))
    gateway.waiting_question = "task_query_pending"
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        headers = await _signed_in(client)
        new_response = await client.post(
            "/api/sites/demo/chat",
            headers=headers,
            json={"message": "/new", "client_id": "browser_2"},
        )
        pending_response = await client.post(
            "/api/sites/demo/chat",
            headers=headers,
            json={
                "message": "Use the 12 mm shaft.",
                "client_id": "browser_2",
                "session_key": "pending_session",
            },
        )
        new_payload = await new_response.json()
        pending_payload = await pending_response.json()

    assert new_response.status == 200
    assert new_payload["session_key"].startswith("session_")
    assert pending_response.status == 200
    assert pending_payload["reply"] == "Pending reply: Use the 12 mm shaft."
    assert gateway.replies_to_pending == [
        {
            "session_key": "pending_session",
            "task_query_id": "task_query_pending",
            "response_text": "Use the 12 mm shaft.",
        }
    ]


@pytest.mark.asyncio
async def test_generated_site_chat_runs_only_for_the_signed_in_page_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page link opens the page for anyone, but its chat acts only as its owner.

    Before, the page embedded its owner's session key and needed no sign-in,
    so anyone with the link could talk to the agents in the owner's thread.
    """

    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    _write_site(config, site_id="owner-guide")
    _write_site(config, site_id="maintenance-page", owner=None)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="session_owner",
        conversation_id="conv_owner",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    store.create_session(
        session_key="session_other_person",
        conversation_id="conv_other",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_2",
        chat_id="chat_2",
        owner_canonical_user_id="user_alpha",
    )
    gateway = _UserTokenStatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )
    message = {
        "message": "Show me my calendar.",
        "client_id": "browser_1",
        "session_key": "session_owner",
        "conversation_id": "conv_owner",
    }

    async with TestClient(TestServer(app.build_app())) as client:
        anonymous = await client.post("/api/sites/owner-guide/chat", json=message)
        anonymous_payload = await anonymous.json()
        other_person = await client.post(
            "/api/sites/owner-guide/chat",
            headers=await _signed_in(client, "ua_user_token"),
            json=message,
        )
        other_person_payload = await other_person.json()
        owner_headers = await _signed_in(client)
        maintenance_page = await client.post(
            "/api/sites/maintenance-page/chat", headers=owner_headers, json=message
        )
        foreign_thread = await client.post(
            "/api/sites/owner-guide/chat",
            headers=owner_headers,
            json={**message, "session_key": "session_other_person", "conversation_id": None},
        )
        foreign_conversation = await client.post(
            "/api/sites/owner-guide/chat",
            headers=owner_headers,
            json={**message, "session_key": "", "conversation_id": "conv_other"},
        )
        owner = await client.post(
            "/api/sites/owner-guide/chat", headers=owner_headers, json=message
        )

    assert anonymous.status == 401
    assert anonymous_payload["status"] == "sign_in_required"
    assert anonymous_payload["sign_in_url"] == "/webchat"
    assert other_person.status == 403
    assert other_person_payload["status"] == "not_page_owner"
    assert maintenance_page.status == 403
    assert foreign_thread.status == 404
    assert foreign_conversation.status == 404
    assert owner.status == 200
    # Only the owner's own message reached the gateway, sent as the owner.
    assert len(gateway.sent_user_messages) == 1
    sent = gateway.sent_user_messages[0]
    assert sent["session_key"] == "session_owner"
    assert sent["canonical_user_id"] == config.user.canonical_user_id


@pytest.mark.asyncio
async def test_generated_site_is_served_only_while_it_matches_its_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(tmp_path)
    site_dir = _write_site(config, site_id="guide", title="Original")
    assert site_dir == config.layout.person_dir(config.user.canonical_user_id) / "sites" / "guide"
    app = InternalWebsiteInteractiveApp(
        config, gateway=_FakeGateway(), bridge=SimpleNamespace()
    )
    page = site_dir / SITE_INDEX_FILENAME

    async with TestClient(TestServer(app.build_app())) as client:
        original = await client.get("/docs/sites/guide/")
        original_text = await original.text()
        page.write_text("<!doctype html><script>steal()</script>", encoding="utf-8")
        with caplog.at_level("WARNING"):
            changed = await client.get("/docs/sites/guide/")
        page.unlink()
        page.symlink_to(config.layout.soul_path)
        linked = await client.get("/docs/sites/guide/")
        # A folder with no record is never served, whatever it holds.
        unrecorded = site_dir.parent / "unrecorded"
        unrecorded.mkdir()
        (unrecorded / SITE_INDEX_FILENAME).write_text("<p>planted</p>", encoding="utf-8")
        planted = await client.get("/docs/sites/unrecorded/")

    assert original.status == 200
    assert "Original" in original_text
    assert changed.status == 404
    assert "event=generated_site_changed site_id=guide" in caplog.text
    assert linked.status == 404
    assert planted.status == 404


@pytest.mark.asyncio
async def test_internal_website_serves_web_chat_shell_auth_without_runtime_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.assistant.display_name = "Ava"
    config.ui.onboarding.appearance.polygon_line_color = "#123456"
    config.agent.debug["context_dump"]["enabled"] = True
    config.agent.debug["context_dump"]["mode"] = "on_budget_warning"
    config.agent.debug["context_dump"]["web_chat_controls_enabled"] = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    config_path = config.save()
    env_path = config.instance_root_path / "team.env"
    env_path.write_text("WEB_CHAT_AUTH_TOKEN=uaweb_test_token\n", encoding="utf-8")
    config.launch.env_file_path = str(env_path)
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    app = InternalWebsiteInteractiveApp(
        config, gateway=_FakeGateway(), bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        shell_response = await client.get("/webchat")
        old_token_shell_response = await client.get("/uaweb_test_token")
        settings_response = await client.get("/api/web-chat/settings")
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        bad_auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "wrong-token"}
        )
        config_download = await client.get("/download/instance_config.yaml")
        env_download = await client.get("/download/team.env")

        shell_text = await shell_response.text()
        settings_payload = await settings_response.json()
        auth_payload = await auth_response.json()

    assert shell_response.status == 200
    assert "Universal Agents Web Chat" in shell_text
    assert old_token_shell_response.status == 404
    assert settings_payload["assistant_name"] == "Ava"
    assert (
        settings_payload["version"]
        == (Path(__file__).resolve().parents[1] / "VERSION")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert settings_payload["appearance"]["polygon_line_color"] == "#123456"
    assert 'id="chat-version"' in shell_text
    command_guides = settings_payload["commands"]
    assert command_guides[0]["syntax"] == "/help"
    command_syntax = "\n".join(command["syntax"] for command in command_guides)
    assert "/auth" in command_syntax
    assert "/tour" in command_syntax
    assert "/user" in command_syntax
    assert "/stop" in command_syntax
    assert "/list_sessions" not in command_syntax
    assert "/edit config" not in command_syntax
    assert "/reply" not in command_syntax
    assert all(
        command["documentation_url"].startswith("/docs/") for command in command_guides
    )
    stop_guide = next(
        command for command in command_guides if command["syntax"].startswith("/stop")
    )
    assert stop_guide == {
        "syntax": "/stop [ task | conversation | session ... ]",
        "description": "list active work and choose what to cancel",
        "documentation_url": "/docs/features/#stopping-active-work",
        "documentation_label": "see options",
    }
    assert settings_payload["debug"]["context_dump"] == {
        "enabled": True,
        "mode": "on_budget_warning",
        "controls_visible": True,
    }
    assert auth_response.status == 200
    assert auth_payload["assistant_name"] == "Ava"
    assert "ua_web_chat_session" in auth_response.cookies
    assert bad_auth_response.status == 401
    assert config_path.name == "instance_config.yaml"
    assert config_download.status == 404
    assert env_download.status == 404


@pytest.mark.asyncio
async def test_internal_website_stop_passes_authenticated_canonical_user_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    gateway = _FakeGateway()
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        auth_payload = await auth_response.json()
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        session_response = await client.post(
            "/api/web-chat/sessions",
            headers=headers,
        )
        session_payload = await session_response.json()
        response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": "/stop",
                "session_key": session_payload["session"]["session_key"],
                "client_request_id": "stop_request_1",
            },
        )

    assert response.status == 200
    assert (
        gateway.stop_requests[0]["canonical_user_id"]
        == auth_payload["canonical_user_id"]
    )


@pytest.mark.asyncio
async def test_authenticated_web_chat_commands_preserve_identity_with_real_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.manager.manager_agent_id = None
    config.gateway.users.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    config.security_layer.mode = "broker"
    config.security_layer.broker_internal_base_url = "http://127.0.0.1:8766"
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-broker-token")

    broker_requests: list[dict] = []

    async def fake_call_broker_json(*, base_url, token, method, path, payload=None):
        broker_requests.append(
            {
                "base_url": base_url,
                "token": token,
                "method": method,
                "path": path,
                "payload": payload,
            }
        )
        return {"providers": {}}

    monkeypatch.setattr(
        "assistant_gateway.http_secure_broker._call_broker_json",
        fake_call_broker_json,
    )

    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = UserGateway(config, output_sink=None, state_store=store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        auth_payload = await auth_response.json()
        canonical_user_id = auth_payload["canonical_user_id"]
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        session_response = await client.post(
            "/api/web-chat/sessions",
            headers=headers,
        )
        session_payload = await session_response.json()
        command_session_key = session_payload["session"]["session_key"]

        status_response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": "/user status",
                "session_key": command_session_key,
                "client_request_id": "command_status_1",
            },
        )
        status_payload = await status_response.json()
        newuser_help_response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": "/user help",
                "session_key": command_session_key,
                "client_request_id": "command_user_help_1",
            },
        )
        newuser_help_payload = await newuser_help_response.json()
        invite_response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": ('/user invite --new "Alice" --label "Browser Onboarding"'),
                "session_key": command_session_key,
                "client_request_id": "command_invite_1",
            },
        )
        invite_payload = await invite_response.json()
        invite_retry_response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": ('/user invite --new "Alice" --label "Browser Onboarding"'),
                "session_key": command_session_key,
                "client_request_id": "command_invite_1",
            },
        )
        invite_retry_payload = await invite_retry_response.json()
        direct_binding_response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": '/user 123456789 "Wrong surface"',
                "session_key": command_session_key,
                "client_request_id": "command_direct_binding_1",
            },
        )
        direct_binding_payload = await direct_binding_response.json()
        link_response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": "/user link AbCdEf1234",
                "session_key": command_session_key,
                "client_request_id": "command_link_1",
            },
        )
        link_payload = await link_response.json()
        alice = next(
            user
            for user in store.list_gateway_users(limit=100)
            if user.display_name == "Alice"
        )
        issued_alice_token = (
            gateway.application.identity.issue_web_user_enrollment_token(
                canonical_user_id=alice.canonical_user_id,
                label="Alice browser",
                pending_expires_at_utc=(
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            )
        )
        assert issued_alice_token is not None
        activated_alice_token = (
            gateway.application.identity.activate_web_user_enrollment_token(
                canonical_user_id=alice.canonical_user_id,
                token_id=issued_alice_token.token_id,
            )
        )
        assert activated_alice_token is not None
        alice_token = issued_alice_token.raw_token
        tour_response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": "/tour",
                "session_key": command_session_key,
                "client_request_id": "command_tour_1",
            },
        )
        tour_payload = await tour_response.json()
        new_response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": "/new",
                "session_key": command_session_key,
                "client_request_id": "command_new_1",
            },
        )
        new_payload = await new_response.json()
        auth_status_response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": "/auth status",
                "session_key": new_payload["session_key"],
                "client_request_id": "command_auth_status_1",
            },
        )
        auth_status_payload = await auth_status_response.json()
        stop_response = await client.post(
            "/api/web-chat/chat",
            headers=headers,
            json={
                "message": "/stop",
                "session_key": new_payload["session_key"],
                "client_request_id": "command_stop_1",
            },
        )
        stop_payload = await stop_response.json()
        alice_auth_response = await client.post(
            "/api/web-chat/auth", json={"token": alice_token}
        )
        alice_cookie = alice_auth_response.cookies["ua_web_chat_session"].value
        alice_headers = {"Cookie": f"ua_web_chat_session={alice_cookie}"}
        alice_session_response = await client.post(
            "/api/web-chat/sessions",
            headers=alice_headers,
        )
        alice_session_payload = await alice_session_response.json()
        alice_invite_response = await client.post(
            "/api/web-chat/chat",
            headers=alice_headers,
            json={
                "message": '/user invite --new "Bob"',
                "session_key": alice_session_payload["session"]["session_key"],
                "client_request_id": "alice_command_invite_1",
            },
        )
        alice_invite_payload = await alice_invite_response.json()

    assert auth_response.status == 200
    assert auth_payload["user_bound"] is True
    assert canonical_user_id == config.user.canonical_user_id

    assert status_response.status == 200
    assert f"Canonical user: {canonical_user_id}" in _last_transcript_content(
        status_payload
    )
    assert f"Display name: {config.user.display_name}" in _last_transcript_content(
        status_payload
    )
    assert "authenticated Web Chat principal" in _last_transcript_content(
        status_payload
    )
    assert newuser_help_response.status == 200
    assert (
        "Direct external-id registration and /user link must be run"
        in _last_transcript_content(newuser_help_payload)
    )

    assert invite_response.status == 200
    assert invite_response.headers["Cache-Control"] == "no-store"
    _assert_sensitive_transient_timeline_item(invite_payload)
    assert "Registered web-only user." in _transient_content(invite_payload)
    assert "(Alice)" in _transient_content(invite_payload)
    assert "Invite code: uainvite_" in _transient_content(invite_payload)
    assert "uainvite_" not in _last_transcript_content(invite_payload)
    assert invite_retry_response.status == 200
    assert "transient" not in invite_retry_payload
    assert [message["message_id"] for message in invite_retry_payload["messages"]] == [
        message["message_id"] for message in invite_payload["messages"]
    ]
    assert (
        sum(
            user.display_name == "Alice" for user in store.list_gateway_users(limit=100)
        )
        == 1
    )
    alice_memberships = store.list_team_memberships(
        alice.canonical_user_id,
        status="active",
    )
    assert any(
        membership.team_id == config.gateway.team_id and membership.role == "member"
        for membership in alice_memberships
    )

    assert direct_binding_response.status == 200
    assert "not available in Web Chat" in _last_transcript_content(
        direct_binding_payload
    )
    assert link_response.status == 200
    assert "not available in Web Chat" in _last_transcript_content(link_payload)
    link_transcript = store.transcript_repository.list_transcript_messages(
        owner_canonical_user_id=canonical_user_id,
        session_key=command_session_key,
        before_session_sequence=None,
        limit=100,
    )
    assert all("AbCdEf1234" not in message.content for message in link_transcript)
    assert any(
        message.content == "/user link [redacted]" for message in link_transcript
    )
    assert (
        store.get_active_channel_binding(
            channel_name="website",
            external_user_id=canonical_user_id,
        )
        is None
    )

    assert alice_auth_response.status == 200
    assert alice_invite_response.status == 200
    _assert_sensitive_transient_timeline_item(alice_invite_payload)
    assert (
        _transient_content(alice_invite_payload)
        == "Only an admin can create Web Chat invitations."
    )
    assert _last_transcript_content(alice_invite_payload) == (
        "The requested Web Chat access operation was processed. One-time material "
        "is not stored in transcript history."
    )

    assert tour_response.status == 200
    assert "not linked to a Universal Agents user" not in _last_transcript_content(
        tour_payload
    )
    tour_session = store.get_session(command_session_key)
    assert tour_session is not None
    assert tour_session.owner_canonical_user_id == canonical_user_id

    assert new_response.status == 200
    new_session = store.get_session(new_payload["session_key"])
    assert new_session is not None
    assert new_session.owner_canonical_user_id == canonical_user_id

    assert auth_status_response.status == 200
    _assert_sensitive_transient_timeline_item(auth_status_payload)
    assert _transient_content(auth_status_payload).startswith(
        "Secure broker auth status:"
    )
    assert "Secure broker auth status:" not in _last_transcript_content(
        auth_status_payload
    )
    assert broker_requests == [
        {
            "base_url": "http://127.0.0.1:8766",
            "token": "test-broker-token",
            "method": "POST",
            "path": "/v1/auth/status",
            "payload": {
                "context": {
                    "canonical_user_id": canonical_user_id,
                    "team_id": config.manager.team_id,
                    "principal_type": "user",
                }
            },
        },
        {
            "base_url": "http://127.0.0.1:8766",
            "token": "test-broker-token",
            "method": "GET",
            "path": "/v1/auth/model-keys/status",
            "payload": None,
        },
    ]

    assert stop_response.status == 200
    assert "not linked to a Universal Agents user" not in _last_transcript_content(
        stop_payload
    )


@pytest.mark.asyncio
async def test_internal_website_web_chat_hides_debug_commands_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    gateway = _FakeGateway()
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        settings_response = await client.get("/api/web-chat/settings")
        settings_payload = await settings_response.json()
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        session_headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        session_response = await client.post(
            "/api/web-chat/sessions",
            headers=session_headers,
        )
        session_payload = await session_response.json()
        session_key = session_payload["session"]["session_key"]
        help_response = await client.post(
            "/api/web-chat/chat",
            headers=session_headers,
            json={
                "message": "/help",
                "session_key": session_key,
                "client_request_id": "help_request_1",
            },
        )
        tour_response = await client.post(
            "/api/web-chat/chat",
            headers=session_headers,
            json={
                "message": "/tour",
                "session_key": session_key,
                "client_request_id": "tour_request_1",
            },
        )
        blocked_response = await client.post(
            "/api/web-chat/chat",
            headers=session_headers,
            json={
                "message": "/list_sessions",
                "session_key": session_key,
                "client_request_id": "blocked_request_1",
            },
        )
        reply_response = await client.post(
            "/api/web-chat/chat",
            headers=session_headers,
            json={
                "message": "/reply msg_1 continue",
                "session_key": session_key,
                "client_request_id": "reply_request_1",
            },
        )
        help_payload = await help_response.json()
        tour_payload = await tour_response.json()
        blocked_payload = await blocked_response.json()
        reply_payload = await reply_response.json()

    commands_text = "\n".join(
        command["syntax"] for command in settings_payload["commands"]
    )
    assert settings_response.status == 200
    assert "/list_sessions" not in commands_text
    assert "/edit config" not in commands_text
    assert "/switch_topic" not in commands_text
    assert "/reply" not in commands_text
    assert "/tour" in commands_text
    assert "/user" in commands_text
    assert "/newuser" not in commands_text
    assert help_response.status == 200
    assert "/tour" in _last_transcript_content(help_payload)
    assert "/user" in _last_transcript_content(help_payload)
    assert "/newuser" not in _last_transcript_content(help_payload)
    assert "/list_sessions" not in _last_transcript_content(help_payload)
    assert "/edit config" not in _last_transcript_content(help_payload)
    assert tour_response.status == 200
    assert (
        _last_transcript_content(tour_payload) == f"Reply: {tour_start_request_text()}"
    )
    assert gateway.sent_user_messages[0]["text"] == tour_start_request_text()
    assert blocked_response.status == 200
    assert (
        _last_transcript_content(blocked_payload)
        == "This command is not available in Web Chat. Use /help for supported commands."
    )
    assert reply_response.status == 200
    assert (
        _last_transcript_content(reply_payload)
        == "The /reply command is available only in the local CLI."
    )


@pytest.mark.asyncio
async def test_internal_website_user_token_auth_binds_web_chat_to_canonical_user(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.save()
    gateway = _UserTokenFakeGateway()
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "ua_user_token"}
        )
        auth_payload = await auth_response.json()
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        session_headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        session_response = await client.post(
            "/api/web-chat/sessions",
            headers=session_headers,
        )
        session_payload = await session_response.json()
        chat_response = await client.post(
            "/api/web-chat/chat",
            headers=session_headers,
            json={
                "message": "Hello from a user token",
                "session_key": session_payload["session"]["session_key"],
                "client_request_id": "msg_user_token_1",
            },
        )
        config_response = await client.get(
            "/api/config-editor/config", headers=session_headers
        )
        chat_payload = await chat_response.json()

    assert auth_response.status == 200
    assert auth_payload["user_bound"] is True
    assert auth_payload["canonical_user_id"] == "user_alpha"
    assert auth_payload["display_name"] == "Alice"
    assert chat_response.status == 200
    assert _last_transcript_content(chat_payload) == "Reply: Hello from a user token"
    assert gateway.sent_user_messages[0]["user_id"] == "user_alpha"
    assert gateway.sent_user_messages[0]["canonical_user_id"] == "user_alpha"
    assert config_response.status == 403


@pytest.mark.asyncio
async def test_internal_website_invite_enrolls_permanent_token_and_disabled_user_session_is_invalidated(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.gateway.users.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = UserGateway(config, output_sink=None, state_store=store)
    user = store.create_gateway_user(
        canonical_user_id="user_invited",
        display_name="Invited User",
        timezone="America/Chicago",
    )
    store.create_team_membership(
        canonical_user_id=user.canonical_user_id,
        team_id=config.gateway.team_id,
        role="member",
    )
    raw_invite = "uainvite_test_single_use"
    store.create_web_user_invite(
        invite_hash=SystemGatewayIdentitySecrets().hash_secret(raw_invite),
        canonical_user_id=user.canonical_user_id,
        label="Initial browser",
        expires_at_utc=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        created_by_canonical_user_id=config.user.canonical_user_id,
    )
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": raw_invite}
        )
        auth_payload = await auth_response.json()
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        enrollment_session = await client.get("/api/web-chat/session", headers=headers)
        enrollment_session_payload = await enrollment_session.json()
        sessions_before_enrollment = await client.get(
            "/api/web-chat/sessions", headers=headers
        )
        first_token_response = await client.post(
            "/api/web-chat/enrollment-token", headers=headers
        )
        first_token_payload = await first_token_response.json()
        session_after_generation = await client.get(
            "/api/web-chat/session", headers=headers
        )
        session_after_generation_payload = await session_after_generation.json()
        sessions_after_generation = await client.get(
            "/api/web-chat/sessions", headers=headers
        )
        replacement_token_response = await client.post(
            "/api/web-chat/enrollment-token", headers=headers
        )
        replacement_token_payload = await replacement_token_response.json()
        permanent_token = replacement_token_payload["token"]
        completion_response = await client.post(
            "/api/web-chat/enrollment-complete", headers=headers
        )
        completion_payload = await completion_response.json()
        sessions_after_enrollment = await client.get(
            "/api/web-chat/sessions", headers=headers
        )
        second_auth_response = await client.post(
            "/api/web-chat/auth", json={"token": raw_invite}
        )
        logout_response = await client.post("/api/web-chat/logout", headers=headers)
        permanent_auth_response = await client.post(
            "/api/web-chat/auth", json={"token": permanent_token}
        )
        permanent_auth_payload = await permanent_auth_response.json()
        permanent_cookie = permanent_auth_response.cookies["ua_web_chat_session"].value
        permanent_headers = {"Cookie": f"ua_web_chat_session={permanent_cookie}"}
        permanent_sessions = await client.get(
            "/api/web-chat/sessions", headers=permanent_headers
        )
        resolved_permanent_before_disable = (
            gateway.application.identity.authenticate_web_chat_token(permanent_token)
        )

    restarted_store = GatewayStateStore(str(tmp_path / "gateway.db"))
    restarted_gateway = UserGateway(
        config, output_sink=None, state_store=restarted_store
    )
    restarted_app = InternalWebsiteInteractiveApp(
        config, gateway=restarted_gateway, bridge=SimpleNamespace()
    )
    async with TestClient(TestServer(restarted_app.build_app())) as restarted_client:
        restarted_auth_response = await restarted_client.post(
            "/api/web-chat/auth",
            json={"token": permanent_token},
        )
        restarted_auth_payload = await restarted_auth_response.json()
        restarted_cookie = restarted_auth_response.cookies["ua_web_chat_session"].value
        restarted_headers = {"Cookie": f"ua_web_chat_session={restarted_cookie}"}
        restarted_sessions = await restarted_client.get(
            "/api/web-chat/sessions",
            headers=restarted_headers,
        )
        restarted_store.set_gateway_user_status(user.canonical_user_id, "disabled")
        sessions_after_disable = await restarted_client.get(
            "/api/web-chat/sessions",
            headers=restarted_headers,
        )

    assert auth_response.status == 200
    assert auth_payload["canonical_user_id"] == user.canonical_user_id
    assert auth_payload["credential_kind"] == "invite"
    assert auth_payload["is_bootstrap_user"] is False
    assert auth_payload["permanent_token_required"] is True
    assert auth_payload["permanent_token_status"] == "required"
    assert auth_payload["enrollment_label"] == "Initial browser"
    assert enrollment_session.status == 200
    assert enrollment_session_payload["permanent_token_status"] == "required"
    assert sessions_before_enrollment.status == 403
    assert first_token_response.status == 200
    assert first_token_payload["token"].startswith("uauser_")
    assert (
        gateway.application.identity.authenticate_web_chat_token(
            first_token_payload["token"]
        )
        is None
    )
    assert session_after_generation.status == 200
    assert (
        session_after_generation_payload["permanent_token_status"]
        == "pending_confirmation"
    )
    assert sessions_after_generation.status == 403
    assert replacement_token_response.status == 200
    assert permanent_token.startswith("uauser_")
    assert permanent_token != first_token_payload["token"]
    assert completion_response.status == 200
    assert completion_payload["permanent_token_required"] is False
    assert completion_payload["permanent_token_status"] == "complete"
    assert sessions_after_enrollment.status == 200
    assert (
        gateway.application.identity.authenticate_web_chat_token(
            first_token_payload["token"]
        )
        is None
    )
    assert resolved_permanent_before_disable is not None
    assert permanent_token.encode() not in (tmp_path / "gateway.db").read_bytes()
    assert second_auth_response.status == 401
    assert logout_response.status == 200
    assert permanent_auth_response.status == 200
    assert permanent_auth_payload["credential_kind"] == "user_token"
    assert permanent_auth_payload["permanent_token_required"] is False
    assert permanent_sessions.status == 200
    assert restarted_auth_response.status == 200
    assert restarted_auth_payload["canonical_user_id"] == user.canonical_user_id
    assert restarted_auth_payload["credential_kind"] == "user_token"
    assert restarted_sessions.status == 200
    assert sessions_after_disable.status == 401


@pytest.mark.asyncio
async def test_internal_website_web_chat_logout_and_https_cookie_security(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.network.public_origin = "https://agents.example.test"
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    app = InternalWebsiteInteractiveApp(
        config, gateway=_FakeGateway(), bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        sessions_before_logout = await client.get(
            "/api/web-chat/sessions", headers=headers
        )
        logout_response = await client.post("/api/web-chat/logout", headers=headers)
        sessions_after_logout = await client.get(
            "/api/web-chat/sessions", headers=headers
        )

    auth_set_cookie = auth_response.headers.getall("Set-Cookie")
    logout_set_cookie = logout_response.headers.getall("Set-Cookie")
    assert any(
        "Secure" in header and "HttpOnly" in header and "SameSite=Strict" in header
        for header in auth_set_cookie
    )
    assert sessions_before_logout.status == 200
    assert logout_response.status == 200
    assert logout_response.headers["Cache-Control"] == "no-store"
    assert any("Max-Age=0" in header for header in logout_set_cookie)
    assert sessions_after_logout.status == 401


def test_web_chat_browser_state_is_principal_scoped_and_credentials_are_not_persisted() -> (
    None
):
    asset_root = (
        Path(__file__).resolve().parents[1]
        / "assistant_gateway"
        / "onboarding_web"
        / "assets"
    )
    script = module_source(asset_root, "chat.js")
    markup = (asset_root / "chat.html").read_text(encoding="utf-8")
    styles = (asset_root / "styles.css").read_text(encoding="utf-8")
    onboarding_script = module_source(asset_root, "app.js")
    appearance_script = (asset_root / "appearance.js").read_text(encoding="utf-8")
    config_editor_script = (asset_root / "config-editor.js").read_text(encoding="utf-8")
    credential_script = (asset_root / "credential_controls.js").read_text(
        encoding="utf-8"
    )
    action_feedback_script = (asset_root / "action_feedback.js").read_text(
        encoding="utf-8"
    )
    # The Settings dialog and the modules it imports (Team, Memories, Inbox, Access).
    settings_script = module_source(asset_root, "settings_controls.mjs")
    delivery_script = (asset_root / "chat_delivery.js").read_text(encoding="utf-8")
    workspace_script = (asset_root / "workspace_browser.js").read_text(encoding="utf-8")

    assert 'storageKey = "ua.webchat.ui." + keySuffix;' in script
    assert '"ua.webchat.client_id." + keySuffix' in script
    assert 'window.localStorage.removeItem("ua.webchat.threads");' in script
    assert (
        'window.localStorage.removeItem("ua.webchat.threads." + keySuffix);' in script
    )
    assert "messages: []," in script
    assert (
        "window.localStorage.setItem(sharedState.storageKey, JSON.stringify(uiThreads));"
        in script
    )
    assert 'requestClient.fetch("/api/web-chat/sessions"' in script
    assert '"/messages" + query' in script
    assert "mergeTranscriptMessages(thread, payload.messages || []);" in script
    assert "message.message_id" in script
    assert 'requestClient.fetch("/api/web-chat/appearance"' in script
    assert (
        'window.fetch("/api/web-chat/sessions", {\n          method: "DELETE"'
        not in script
    )
    assert 'window.fetch("/api/web-chat/appearance"' in appearance_script
    assert "localStorage" not in appearance_script
    assert "return loadUserAppearance().then(loadConfig);" in config_editor_script
    assert 'commandName === "/clear"' not in script
    assert "transientItems" in script
    save_threads_source = script.split("function saveThreads()", 1)[1].split(
        "function createThread", 1
    )[0]
    assert "transientItems" not in save_threads_source
    assert "formatSessionCountdown" in credential_script
    assert "message-transient-countdown" in script
    assert "message-transient-open" in script
    countdown_source = script.split(
        "function replaceTransientActionWithExpiredState(countdown)", 1
    )[1].split("function updateTransientCountdown()", 1)[0]
    assert "openAction.remove();" in countdown_source
    assert "countdown.remove();" in countdown_source
    assert 'expiredStatus.setAttribute("role", "status");' in countdown_source
    assert "This secure form link has expired." in script
    assert "A submitted request may still be finishing." in script
    assert 'actions.querySelector(".message-transient-dismiss")' in countdown_source
    assert ".message-transient-expired {" in styles
    assert '.message-transient-open[aria-disabled="true"]' not in styles
    render_messages_source = script.split("function renderMessages(options)", 1)[1].split(
        "function appendSystemError", 1
    )[0]
    timeline_index = render_messages_source.index(
        "window.UAChatDelivery.buildTimeline("
    )
    durable_message_index = render_messages_source.index(
        "messageList.appendChild(createMessageBubble(item.value, thread));"
    )
    transient_notice_index = render_messages_source.index(
        "messageList.appendChild(createTransientItemCard(thread, item.value));"
    )
    scroll_index = render_messages_source.index(
        "shellState.transcriptView.restore(viewport, {"
    )
    assert timeline_index < transient_notice_index < scroll_index
    assert timeline_index < durable_message_index < scroll_index
    transient_item_source = script.split(
        "function createTransientItemCard(thread, item)", 1
    )[1].split("function renderMessages(options)", 1)[0]
    assert "transientText.textContent = item.content;" in transient_item_source
    assert "transientText.textContent = item.url;" not in transient_item_source
    assert 'transientCopy.textContent = "Copy";' in transient_item_source
    assert "if (item.url)" in transient_item_source
    assert 'transientOpen.target = "_blank";' in transient_item_source
    assert 'transientOpen.rel = "noopener noreferrer";' in transient_item_source
    assert "transientOpen.textContent = item.label;" in transient_item_source
    assert "message-transient-dismiss" in transient_item_source
    assert "reconcileTransientItems(" in script
    # Only access Inbox (#access_request=) and Access (#agent=) links remain;
    # the old interaction and permission handoff links are gone. The page reads
    # them once (chat_links.mjs) and puts plain chat back in the address.
    assert "readChatPageLink(window.location)" in script
    assert "window.history.replaceState(null, document.title, pageLink.cleanUrl)" in script
    assert "handoffParams" not in script
    assert "pendingHumanInteractionId" not in script
    assert "pendingPermissionHandoffToken" not in script
    assert 'threadList.innerHTML = "";' in script
    assert 'messageList.innerHTML = "";' in script
    assert 'commandLines.join(" ")' not in script
    assert "function commandDefinition(line)" in script
    assert 'typeof line !== "object"' in script
    assert "function commandDocumentationLink(" in script
    assert 'link.target = "_blank";' in script
    assert 'link.rel = "noreferrer";' in script
    assert '"command-guide-name"' in script
    assert '"command-guide-doc-link"' in script
    assert '"command-guide-token " + tokenClass' in script
    assert '.replace(/\\|/g, " | ")' in script
    assert "messageList.appendChild(createWelcomeGuidance());" in script
    assert 'uiElement("aside", "message-guidance")' in script
    assert ".message-guidance {" in styles
    assert ".command-guide-token.is-option {" in styles
    assert ".command-guide-doc-link {" in styles
    assert 'timestampElement(message.createdAt, "message-timestamp")' in script
    assert ".message-timestamp {" in styles
    assert 'messageInput.value = "";' in script
    assert "clearPendingAttachments();" in script
    assert "function unlockObjectives()" in script
    assert 'requestClient.fetch("/api/web-chat/session"' in script
    assert 'requestClient.fetch("/api/web-chat/enrollment-token"' in script
    assert 'requestClient.fetch("/api/web-chat/enrollment-complete"' in script
    assert 'id="chat-enrollment-overlay"' in markup
    assert 'id="chat-enrollment-token"' in markup
    assert 'class="chat-sidebar-header"' in markup
    assert '<button id="settings-button" class="settings-button"' in markup
    assert 'aria-label="Open Settings"' in markup
    assert (
        '<svg viewBox="0 0 24 24" width="21" height="21" fill="none" '
        'stroke="currentColor"' in markup
    )
    assert 'class="chat-thread-history"' in markup
    assert markup.index('id="chat-thread-history-label"') < markup.index(
        'id="thread-list"'
    )
    assert 'id="permission-center-button"' not in markup
    assert 'id="settings-inbox-tab"' in markup
    assert 'id="settings-inbox-panel"' in markup
    assert 'id="settings-team-tab"' in markup
    assert 'id="settings-appearance-tab"' in markup
    assert 'id="settings-appearance-panel"' in markup
    assert 'id="settings-permissions-tab"' not in markup
    assert 'id="settings-permissions-panel"' not in markup
    assert 'href="/webchat?view=objectives"' in markup
    assert 'href="/webchat?view=files"' in markup
    assert 'target="_blank" rel="noopener noreferrer"' in markup
    assert 'id="objectives-app"' in markup
    assert 'id="workspace-app"' in markup
    assert 'id="workspace-tree"' in markup
    assert 'id="workspace-viewer"' in markup
    assert 'id="workspace-viewer-tools"' in markup
    assert ".chat-sidebar {\n  display: flex;\n  flex-direction: column;" in styles
    assert ".chat-thread-history {" in styles
    assert "flex: 1 1 0;" in styles
    assert ".settings-button svg {" in styles
    assert "Signed in as" not in markup
    assert 'class="chat-account-meta"' in markup
    assert markup.index('id="chat-account-name"') < markup.index('id="chat-version"')
    # Log out sits at the bottom of Settings' left column, not in the sidebar.
    assert 'id="chat-logout-button"' not in markup
    assert markup.index('id="settings-status-link"') < markup.index('id="settings-logout-button"')
    assert markup.index('id="settings-logout-button"') < markup.index('class="settings-panels"')
    assert "grid-template-columns: minmax(0, 1fr) auto;" in styles
    assert ".chat-account-controls {\n  display: grid;\n  flex: 0 0 auto;" in styles
    assert "/operational_ids.js" in markup
    assert "/credential_controls.js" in markup
    assert "/action_feedback.js" in markup
    assert "/permission_controls.js" not in markup
    assert "/appearance.js" in markup
    assert "/metallic_mesh.js" in markup
    assert "/chat_delivery.js" in markup
    assert "/workspace_browser.js" in markup
    assert 'file.preview_kind === "markdown"' in workspace_script
    assert "markdownRenderer.render" in workspace_script
    assert "splitFileName" in workspace_script
    assert '"Wrap lines"' in workspace_script
    assert '"Show raw"' in workspace_script
    assert "code.innerHTML = file.highlighted_html;" in workspace_script
    assert "code.textContent = displayContent;" in workspace_script
    assert 'file.preview_kind === "html"' in workspace_script
    assert 'htmlFrame.setAttribute("sandbox", "")' in workspace_script
    assert "htmlFrame.src = contentUrl(file.path);" in workspace_script
    assert 'primaryButton.setAttribute("aria-busy", "true")' in action_feedback_script
    assert 'primaryButton.classList.add("is-action-pending")' in action_feedback_script
    assert ".button.is-action-pending::before" in styles
    assert ".workspace-layout {" in styles
    assert ".workspace-tree-name-extension {" in styles
    assert ".workspace-text-preview.is-nowrap {" in styles
    assert '.workspace-syntax [class^="ua-syntax-k"] {' in styles
    assert "window.UACredentialControls" in credential_script
    assert "credentialControls.toggleReveal" in onboarding_script
    assert "credentialControls.copyTextToClipboard" in onboarding_script
    assert "credentialControls.secureRandomHexToken" in onboarding_script
    assert "Math.random()" not in credential_script
    assert 'window.localStorage.setItem("token"' not in script
    assert 'window.localStorage.setItem("ua.webchat.token"' not in script
    assert "window.sessionStorage" not in script
    assert "window.UASettingsControls" not in script
    assert "sharedState.settingsControls = createSettingsControls({" in script
    assert "export function createSettingsControls" in settings_script
    assert 'TEAM_API = "/api/web-chat/team"' in settings_script
    assert "?session_key=${encodeURIComponent(getSessionKey())}" in settings_script
    # Team reloads use the shared Refresh-button spinner and never show a
    # transient "Loading/Refreshing live team status" line (ui/DESIGN.md).
    assert "client.beginRefresh(refreshButton)" in settings_script
    assert "window.UAActionFeedback.beginRefresh(button)" not in settings_script
    assert "browser.UAActionFeedback.beginRefresh(button)" in settings_script
    assert "live team status" not in settings_script
    assert "stableTeamSignature" in settings_script
    assert "updateLastSeen(members)" in settings_script
    # A failed refresh keeps the last tables instead of clearing them first.
    assert "content.replaceChildren();\n        setStatus" not in settings_script
    assert "connection.can_decide === true" in settings_script
    assert '"button primary mcp-connection-action", "Approve"' in settings_script
    assert '"button ghost mcp-connection-action", "Reject"' in settings_script
    assert 'presentationMode: "friendly"' in settings_script
    assert "presentationMode !== TECHNICAL_MODE" in settings_script
    assert 'was ${approved ? "approved" : "rejected"}.' in settings_script
    # MCP decisions and procedure changes confirm the session first.
    assert 'SESSION_CONTEXT_API = "/api/web-chat/session-context"' in settings_script
    assert "/api/web-chat/permission-context" not in script
    assert 'PROCEDURE_PROPOSALS_API = "/api/web-chat/procedures/proposals"' in settings_script
    assert 'INSTALLED_PROCEDURES_API = "/api/web-chat/procedures/installed"' in settings_script
    assert '"/api/web-chat/objectives"' in script
    assert '"X-UA-Permission-CSRF": sharedState.runtimeControlsCsrfToken' in script
    assert "activity_collector_admin_token" not in script
    assert "expandedSkills" in settings_script
    assert "details.open = Boolean(state.expandedSkills[agentId]);" in settings_script
    assert "else delete state.expandedSkills[agentId];" in settings_script
    assert 'messageId: String(message.messageId || message.message_id || "")' in script
    assert (
        "clientRequestId: String(message.clientRequestId || "
        'message.client_request_id || "")' in script
    )
    assert "window.UAChatDelivery.reconcileMessages(" in script
    assert "anchorSessionSequence: Number(message.anchorSessionSequence || 0)" in script
    assert "window.UAChatDelivery.latestDurableSessionSequence(" in script
    assert (
        "latestDurableSessionSequence: latestDurableSessionSequence" in delivery_script
    )
    assert "clearErrorNotice: clearErrorNotice" in delivery_script
    assert "createErrorNotice: createErrorNotice" in delivery_script
    assert 'RENAME_THREAD: "rename_thread"' in script
    assert (
        "clearSystemError(thread, shellState.SYSTEM_ERROR_SOURCE.RENAME_THREAD);"
        in script
    )
    # Permission decision receipts were removed with the old Permissions tab.
    assert "PERMISSION_RECEIPT" not in script
    # Per-request Retry cleanup is exercised by chat_send_retry.test.cjs,
    # including later acknowledgements and live acceptance before HTTP failure.
    assert "deliveryTracker.watch(clientRequestId);" in script
    assert 'deliveryStatus.textContent = "not delivered";' in script
    assert "liveEvents.start();" in script
    assert "DELIVERY_TIMEOUT_MS = 15000" in delivery_script
    assert ".message-delivery-status.is-error {" in styles
    assert "message.deliveryId" not in script
    assert "message.eventId" not in script
    assert 'sharedState.asyncEventCursor = "";' in script
    assert "asyncEventStreamId" not in script
    assert "asyncEventLastId" not in script
    transcript_event_handler = script.split(
        "function applyTranscriptEvent(event) {", maxsplit=1
    )[1].split("function revealPassword", maxsplit=1)[0]
    assert "mergeTranscriptMessages(thread, [event]);" in transcript_event_handler
    assert "if (attention.receive(event, thread)) {" in transcript_event_handler
    assert (
        "setThreadUnread(thread, !isReadingThread(thread), messageLevelFromPayload(event));"
        in transcript_event_handler
    )
    assert "setThreadPending" not in transcript_event_handler
    assert "finishTerminalReplyWait" not in transcript_event_handler
    assert (
        "setThreadUnread(thread, !isReadingThread(thread), "
        "messageLevelFromPayload(lastMessage));" in script
    )
    assert (
        'pendingIndicator.setAttribute("aria-label", "New reply in this thread");'
        in script
    )
    assert ".thread-row.is-pending .thread-active-indicator {" in styles
    unfocused_pending_indicator_style = styles.split(
        ".thread-row.is-pending:not(.is-active) .thread-active-indicator::before {",
        maxsplit=1,
    )[1].split("}", maxsplit=1)[0]
    assert "0 0 14px rgba(2, 132, 199, 0.52);" in unfocused_pending_indicator_style
    unfocused_unread_row_style = styles.split(
        ".thread-row.has-unread:not(.is-active) {", maxsplit=1
    )[1].split("}", maxsplit=1)[0]
    assert "border-color: rgba(32, 240, 90, 0.64);" in unfocused_unread_row_style
    assert "0 0 16px rgba(32, 240, 90, 0.30);" in unfocused_unread_row_style
    unfocused_attention_row_style = styles.split(
        ".thread-row.has-attention:not(.is-active) {", maxsplit=1
    )[1].split("}", maxsplit=1)[0]
    assert "border-color: rgba(255, 214, 10, 0.70);" in unfocused_attention_row_style
    pending_pulse_style = styles.split("@keyframes thread-reply-pulse {", maxsplit=1)[
        1
    ].split(".thread-title-tooltip", maxsplit=1)[0]
    assert "opacity: 0.78;" in pending_pulse_style
    assert "opacity: 0.42;" not in pending_pulse_style
    unread_indicator_style = styles.split(
        ".thread-row.has-unread:not(.is-pending) .thread-active-indicator::before {",
        maxsplit=1,
    )[1].split("}", maxsplit=1)[0]
    assert "background: var(--green-bright);" in unread_indicator_style
    assert "animation: none;" in unread_indicator_style
    # Gateway errors become one plain sentence: message, recovery, and in
    # technical mode the stable code and failure details (ui/DESIGN.md).
    assert "[error.message, error.recovery].filter(Boolean)" in settings_script
    assert "technical.status_code" in settings_script
    assert "export function failureMessage(error, fallbackMessage)" in settings_script
    assert "The request did not reach the gateway." in settings_script
    assert "close();" in settings_script
    # The old Permissions tab, its challenge cards, receipts and handoffs are gone.
    assert ">Approved Access<" not in markup
    assert ">Needs Approval<" not in markup
    assert ">Custom Permissions<" not in markup
    assert ">Permissions<" not in markup
    assert "choice_number" not in script
    assert "challenge_digest" not in script
    assert 'receipt_type: "permission_decision"' not in script
    assert '"/receipts"' not in script
    assert "openHandoff" not in script
    assert "mountInlineReview" not in script
    for removed_route in (
        "/api/web-chat/human-interactions",
        "/api/web-chat/permissions",
        "/api/web-chat/permission-handoffs",
        "/api/web-chat/permission-grants",
        "/api/web-chat/permission-polic",
        "/api/web-chat/permission-capability-proposals",
        "/api/web-chat/installed-permissions",
        "/api/web-chat/authorization-administration",
        "/api/web-chat/emergency-deny",
    ):
        assert removed_route not in script, removed_route
    for removed_message_type in (
        '"permission_approval"',
        '"human_interaction_request"',
        '"command_permissions"',
    ):
        assert removed_message_type not in script, removed_message_type
    assert ".message-bubble.is-system {" in styles
    assert "border-left: 4px solid rgba(183, 121, 31, 0.72);" in styles
    assert ".message-bubble.is-system .message-role {" in styles
    assert ".innerHTML" not in settings_script
    assert "localStorage" not in settings_script
    assert "sessionStorage" not in settings_script
    assert "window.history.replaceState(" in script


def test_workspace_tree_icons_keep_borders_inside_clip_box() -> None:
    styles = (
        Path(__file__).resolve().parents[1]
        / "assistant_gateway"
        / "onboarding_web"
        / "assets"
        / "styles.css"
    ).read_text(encoding="utf-8")

    icon_container_style = styles.split(".workspace-tree-kind {", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]
    icon_shape_style = styles.split(
        ".workspace-tree-kind::before,\n.workspace-tree-kind::after {", maxsplit=1
    )[1].split("}", maxsplit=1)[0]

    assert "overflow: hidden;" in icon_container_style
    assert "box-sizing: border-box;" in icon_shape_style


def test_runtime_control_cards_contain_long_values_and_align_schedule_actions() -> None:
    asset_root = (
        Path(__file__).resolve().parents[1]
        / "assistant_gateway"
        / "onboarding_web"
        / "assets"
    )
    script = module_source(asset_root, "chat.js")
    styles = (asset_root / "styles.css").read_text(encoding="utf-8")

    assert '"runtime-control-actions runtime-schedule-controls"' in script
    assert 'uiElement("div", "runtime-schedule-actions")' in script
    assert "controls.appendChild(pauseInput);" in script
    assert "actionButtons.appendChild(button);" in script
    card_style = styles.split(".objective-card {", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]
    schedule_controls_style = styles.split(".runtime-schedule-controls {", maxsplit=1)[
        1
    ].split("}", maxsplit=1)[0]
    schedule_actions_style = styles.split(".runtime-schedule-actions {", maxsplit=1)[
        1
    ].split("}", maxsplit=1)[0]
    title_style = styles.split(".objective-card-title {", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]
    resource_value_style = styles.split(".runtime-resource-details > * {", maxsplit=1)[
        1
    ].split("}", maxsplit=1)[0]

    assert "min-width: 0;" in card_style
    assert "overflow-wrap: anywhere;" in card_style
    assert "grid-template-columns: minmax(0, 1fr);" in schedule_controls_style
    assert "flex-wrap: nowrap;" in schedule_actions_style
    assert "overflow-wrap: anywhere;" in title_style
    assert "overflow-wrap: anywhere;" in resource_value_style


def test_settings_dialog_dimensions_and_installed_procedures_layout() -> None:
    asset_root = (
        Path(__file__).resolve().parents[1]
        / "assistant_gateway"
        / "onboarding_web"
        / "assets"
    )
    team_script = (asset_root / "team_controls.mjs").read_text(encoding="utf-8")
    markup = (asset_root / "chat.html").read_text(encoding="utf-8")
    styles = (asset_root / "styles.css").read_text(encoding="utf-8")

    settings_dialog_style = styles.split(".settings-dialog {", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]
    procedures_style = styles.split(".procedures-content {", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]

    assert "width: min(1456px, 100%);" in settings_dialog_style
    assert (
        "height: min(1330px, calc(100vh - clamp(36px, 10vh, 108px)));"
        in settings_dialog_style
    )
    # Installed procedures sit in the Team panel below Hosted MCP Servers, as
    # their own section outside the automatically refreshed team tables.
    assert markup.index('id="settings-team-content"') < markup.index(
        'id="settings-procedures"'
    ) < markup.index('id="settings-tasks-panel"')
    assert 'PROCEDURES_TITLE = "Installed procedures"' in team_script
    assert "display: grid;" in procedures_style
    # Installed rows are read from each entry's `definition`, in the compact
    # table that turns into labeled cards on narrow screens.
    assert '"permission-grants-table-wrap", "permission-grants-table procedures-table"' in team_script
    assert "entry.definition" in team_script
    assert "definition.display_name" in team_script
    assert "entry && entry.handler_registration_valid === true" in team_script
    assert ".permission-grants-table-wrap {" in styles
    assert '.permission-grants-table td::before' in styles
    # Add appears only when the gateway says the registered code is installable.
    assert "proposal.administrator_installable === true" in team_script


@pytest.mark.asyncio
async def test_internal_website_web_chat_multipart_upload_sends_task_attachments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="website:web-chat:thread_upload",
        conversation_id="conversation_upload",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=config.user.canonical_user_id,
        chat_id="web-chat",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    form = FormData()
    form.add_field("message", "Please inspect this file.")
    form.add_field("session_key", "website:web-chat:thread_upload")
    form.add_field("conversation_id", "conversation_upload")
    form.add_field("client_request_id", "attachment_request_1")
    form.add_field(
        "attachments[]",
        BytesIO(b"hello attachment"),
        filename="notes.txt",
        content_type="text/plain",
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        response = await client.post("/api/web-chat/chat", data=form, headers=headers)
        payload = await response.json()

        sent_attachment = gateway.sent_user_messages[0]["attachments"][0]
        attachment_response = await client.get(
            f"/api/web-chat/attachments/{sent_attachment.attachment_id}",
            headers=headers,
        )
        client.session.cookie_jar.clear()
        unauthenticated_response = await client.get(
            f"/api/web-chat/attachments/{sent_attachment.attachment_id}"
        )
        attachment_bytes = await attachment_response.read()

    assert response.status == 200
    assert _last_transcript_content(payload) == "Reply: Please inspect this file."
    assert payload["messages"][-1]["attachments"] == []
    sent = gateway.sent_user_messages[0]
    assert sent["text"] == "Please inspect this file."
    assert len(sent["attachments"]) == 1
    assert sent_attachment.filename == "notes.txt"
    assert sent_attachment.kind == "document"
    assert sent_attachment.local_path()
    # Chat uploads are kept in the instance's state/uploads folder, outside
    # every folder agents can open directly.
    assert config.layout.uploads_dir in Path(sent_attachment.local_path()).parents
    assert Path(sent_attachment.local_path()).read_bytes() == b"hello attachment"
    assert store.get_attachment(sent_attachment.attachment_id) is not None
    assert attachment_response.status == 200
    assert attachment_bytes == b"hello attachment"
    assert unauthenticated_response.status == 401


@pytest.mark.asyncio
async def test_internal_website_web_chat_attachment_only_message_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="website:web-chat:attachment_only",
        conversation_id="conversation_attachment_only",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=config.user.canonical_user_id,
        chat_id="web-chat",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    form = FormData()
    form.add_field("session_key", "website:web-chat:attachment_only")
    form.add_field("conversation_id", "conversation_attachment_only")
    form.add_field("client_request_id", "attachment_only_request_1")
    form.add_field(
        "attachments[]",
        BytesIO(b"image-bytes"),
        filename="paste.png",
        content_type="image/png",
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        response = await client.post(
            "/api/web-chat/chat",
            data=form,
            headers={"Cookie": f"ua_web_chat_session={session_cookie}"},
        )

    assert response.status == 200
    sent = gateway.sent_user_messages[0]
    assert sent["text"] == "User sent attachment(s)."
    assert len(sent["attachments"]) == 1
    assert sent["attachments"][0].kind == "image"


_NOTES_FILE = ("notes.txt", b"upload bytes", "text/plain")


def _upload_form(
    *,
    session_key: str,
    client_request_id: str,
    message: str = "Please read this.",
    conversation_id: str | None = None,
    files: tuple[tuple[str, bytes, str], ...] = (_NOTES_FILE,),
) -> FormData:
    form = FormData()
    form.add_field("message", message)
    form.add_field("session_key", session_key)
    if conversation_id is not None:
        form.add_field("conversation_id", conversation_id)
    form.add_field("client_request_id", client_request_id)
    for filename, data, content_type in files:
        form.add_field(
            "attachments[]", BytesIO(data), filename=filename, content_type=content_type
        )
    return form


def _saved_uploads(config: GatewayInstanceConfig, database: Path) -> tuple[int, int]:
    """Upload files on disk and upload records in the gateway database."""

    uploads_dir = config.layout.uploads_dir
    files = (
        sum(1 for path in uploads_dir.rglob("*") if path.is_file())
        if uploads_dir.exists()
        else 0
    )
    with sqlite3.connect(database) as conn:
        (records,) = conn.execute("SELECT COUNT(*) FROM gateway_attachments").fetchone()
    return files, records


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_key",
    ["website:web-chat:beta_thread", "website:web-chat:no_such_thread"],
    ids=["another-persons-conversation", "unknown-conversation"],
)
async def test_web_chat_upload_to_a_conversation_the_sender_does_not_own_saves_nothing(
    tmp_path: Path,
    session_key: str,
) -> None:
    """Files are saved only once the conversation is known to be the sender's.

    Before, the files were saved and recorded under the named conversation, as
    its owner's, and only then refused, so they stayed in another person's folder.
    """

    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    database = tmp_path / "gateway.db"
    store = GatewayStateStore(str(database))
    gateway = _StatefulFakeGateway(store)
    gateway.identity_service.resolve_web_token = lambda raw_token: (
        SimpleNamespace(
            user=SimpleNamespace(canonical_user_id="user_alpha", display_name="Alice")
        )
        if raw_token == "ua_user_token"
        else None
    )
    store.create_session(
        session_key="website:web-chat:beta_thread",
        conversation_id="conv_beta",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="user_beta",
        chat_id="web-chat",
        owner_canonical_user_id="user_beta",
    )
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        response = await client.post(
            "/api/web-chat/chat",
            headers=await _signed_in(client, "ua_user_token"),
            data=_upload_form(session_key=session_key, client_request_id="upload_1"),
        )

    assert response.status == 404
    assert gateway.sent_user_messages == []
    assert _saved_uploads(config, database) == (0, 0)


@pytest.mark.asyncio
async def test_web_chat_resent_upload_gets_the_first_answer_without_saving_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser retry of a send with files returns the first send's messages and saves nothing new."""

    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    database = tmp_path / "gateway.db"
    store = GatewayStateStore(str(database))
    store.create_session(
        session_key="website:web-chat:retry_thread",
        conversation_id="conv_retry",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=config.user.canonical_user_id,
        chat_id="web-chat",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        headers = await _signed_in(client)
        responses = [
            await client.post(
                "/api/web-chat/chat",
                headers=headers,
                data=_upload_form(
                    session_key="website:web-chat:retry_thread",
                    client_request_id="upload_retry_1",
                ),
            )
            for _ in range(2)
        ]
        first, resent = [await response.json() for response in responses]

    assert [response.status for response in responses] == [200, 200]
    assert [message["message_id"] for message in resent["messages"]] == [
        message["message_id"] for message in first["messages"]
    ]
    assert len(gateway.sent_user_messages) == 1
    assert _saved_uploads(config, database) == (1, 1)


@pytest.mark.asyncio
async def test_web_chat_separate_upload_and_retry_leave_the_original_question_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser's create-then-send flow uses the new conversation even while an older one waits."""

    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    database = tmp_path / "gateway.db"
    store = GatewayStateStore(str(database))
    owner = config.user.canonical_user_id
    store.create_session(
        session_key="pending_session",
        conversation_id="conv_pending",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        owner_canonical_user_id=owner,
    )
    gateway = _StatefulFakeGateway(store)
    gateway.waiting_question = "task_query_pending"
    app = InternalWebsiteInteractiveApp(config, gateway=gateway, bridge=SimpleNamespace())
    async with TestClient(TestServer(app.build_app())) as client:
        headers = await _signed_in(client)
        created = await client.post("/api/web-chat/sessions", headers=headers)
        assert created.status == 201
        session = (await created.json())["session"]
        session_key = session["session_key"]
        assert session_key != "pending_session"
        exchanges = []
        for _ in range(2):
            response = await client.post(
                "/api/web-chat/chat",
                headers=headers,
                data=_upload_form(
                    session_key=session_key,
                    conversation_id=session["conversation_id"],
                    client_request_id="separate_upload_1",
                ),
            )
            assert response.status == 200
            exchanges.append(await response.json())
        earlier = await client.get("/api/web-chat/sessions/pending_session/messages")
        assert (await earlier.json())["messages"] == []

    assert len(gateway.sent_user_messages) == 1
    assert gateway.sent_user_messages[0]["session_key"] == session_key
    assert gateway.replies_to_pending == []
    assert gateway.waiting_question == "task_query_pending"
    assert exchanges[0]["messages"] == exchanges[1]["messages"]
    assert {message["session_key"] for message in exchanges[0]["messages"]} == {session_key}
    assert _saved_uploads(config, database) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("form_fields", "waiting_for_answer", "status"),
    [
        pytest.param({"message": "/help"}, False, 400, id="slash-command"),
        pytest.param(
            {"conversation_id": "conv_other"}, False, 409, id="other-conversation-id"
        ),
        pytest.param(
            {"client_request_id": "not a request id"}, False, 400, id="bad-request-id"
        ),
        pytest.param({}, True, 400, id="assistant-waits-for-an-answer"),
        # The first file passes its checks, the second does not.
        pytest.param(
            {"files": (_NOTES_FILE, ("run.sh", b"echo hello", "text/x-shellscript"))},
            False,
            400,
            id="a-later-file-is-refused",
        ),
    ],
)
async def test_web_chat_refused_upload_saves_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    form_fields: dict[str, Any],
    waiting_for_answer: bool,
    status: int,
) -> None:
    """Every refusal of a send with files comes before the files are saved."""

    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    database = tmp_path / "gateway.db"
    store = GatewayStateStore(str(database))
    # The fake gateway has a question waiting only in "pending_session".
    store.create_session(
        session_key="pending_session",
        conversation_id="conv_pending",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=config.user.canonical_user_id,
        chat_id="web-chat",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway = _StatefulFakeGateway(store)
    if waiting_for_answer:
        gateway.waiting_question = "task_query_pending"
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )
    fields = {
        "session_key": "pending_session",
        "client_request_id": "upload_refused_1",
        **form_fields,
    }

    async with TestClient(TestServer(app.build_app())) as client:
        response = await client.post(
            "/api/web-chat/chat",
            headers=await _signed_in(client),
            data=_upload_form(**fields),
        )

    assert response.status == status
    assert gateway.sent_user_messages == []
    assert gateway.replies_to_pending == []
    assert _saved_uploads(config, database) == (0, 0)


@pytest.mark.asyncio
async def test_internal_website_web_chat_async_event_includes_and_serves_assistant_attachments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    image_path = config.layout.person_dir(config.user.canonical_user_id) / "reply.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"assistant-image")
    checked, _errors = validate_user_notification_attachments(
        [{"path": str(image_path), "content_type": "image/png", "kind": "image"}],
        config=config,
        person_id=config.user.canonical_user_id,
    )
    staged, _errors = stage_user_notification_attachments(
        checked, config=config, person_id=config.user.canonical_user_id
    )
    staged_copy = Path(staged[0].path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="website:web-chat:browser_1",
        conversation_id="conv_reply",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=config.user.canonical_user_id,
        chat_id="web-chat",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    app = InternalWebsiteInteractiveApp(
        config, gateway=_StatefulFakeGateway(store), bridge=SimpleNamespace()
    )
    await app.application.operations.emit_delivery_event(
        GatewayDeliveryEvent(
            text="Here is the image.",
            session_key="website:web-chat:browser_1",
            channel_name="website",
            user_id="browser_1",
            chat_id="web-chat",
            thread_id=None,
            conversation_id="conv_reply",
            scheduled_job_id=None,
            correlation_id="corr_reply",
            original_message_id="browser_msg_1",
            message_type="task_query",
            message_level="attention",
            attachments=tuple(staged),
        )
    )

    async with TestClient(TestServer(app.build_app())) as client:
        headers = await _signed_in(client)
        events_response = await client.get(
            "/api/web-chat/events",
            headers=headers,
        )
        payload = await events_response.json()
        attachment_url = payload["messages"][0]["attachments"][0]["url"]
        attachment_response = await client.get(attachment_url, headers=headers)
        attachment_bytes = await attachment_response.read()
        # The copy is in the person's scratch folder, which agents' code can
        # write: changed bytes or a link swapped in are "gone", never served.
        staged_copy.write_bytes(b"changed-by-agent-code")
        changed_response = await client.get(attachment_url, headers=headers)
        staged_copy.unlink()
        staged_copy.symlink_to(config.layout.soul_path)
        linked_response = await client.get(attachment_url, headers=headers)
        staged_copy.unlink()
        removed_response = await client.get(attachment_url, headers=headers)

    assert events_response.status == 200
    assert payload["messages"][0]["message_type"] == "task_query"
    assert payload["messages"][0]["message_level"] == "attention"
    attachment = payload["messages"][0]["attachments"][0]
    assert attachment["filename"] == "reply.png"
    assert attachment["kind"] == "image"
    assert attachment_response.status == 200
    assert attachment_bytes == b"assistant-image"
    assert changed_response.status == 410
    assert linked_response.status == 410
    assert removed_response.status == 410


@pytest.mark.asyncio
async def test_internal_website_config_editor_auth_save_restore_download_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.user.display_name = "Original User"
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    config_path = config.save()
    original_content = config_path.read_text(encoding="utf-8")
    for index in range(6):
        config_path.with_name(
            f"instance_config.yaml.bak.preexisting.{index}"
        ).write_text(original_content, encoding="utf-8")
    updated_content = original_content.replace(
        "display_name: Original User", "display_name: Edited User"
    )
    assert updated_content != original_content
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    app = InternalWebsiteInteractiveApp(
        config, gateway=_UserTokenFakeGateway(), bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        shell_response = await client.get("/config-editor")
        motion_response = await client.get("/mesh_motion.js")
        ui_motion_response = await client.get("/ui_motion.js")
        ui_motion_text = await ui_motion_response.text()
        activity_mesh_response = await client.get("/activity_mesh.mjs")
        activity_mesh_text = await activity_mesh_response.text()
        intro_word_response = await client.get("/intro_word.js")
        intro_word_text = await intro_word_response.text()
        glass_response = await client.get("/glass_mesh.js")
        glass_text = await glass_response.text()
        panel_motion_response = await client.get("/panel_motion.js")
        panel_motion_text = await panel_motion_response.text()
        operational_ids_response = await client.get("/operational_ids.js")
        credential_controls_response = await client.get("/credential_controls.js")
        action_feedback_response = await client.get("/action_feedback.js")
        script_response = await client.get("/config-editor.js")
        unauth_config_response = await client.get("/api/config-editor/config")
        user_auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "ua_user_token"}
        )
        user_session_cookie = user_auth_response.cookies["ua_web_chat_session"].value
        user_session_headers = {"Cookie": f"ua_web_chat_session={user_session_cookie}"}
        user_config_response = await client.get(
            "/api/config-editor/config", headers=user_session_headers
        )
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        session_headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        config_response = await client.get(
            "/api/config-editor/config", headers=session_headers
        )
        config_payload = await config_response.json()
        invalid_save_response = await client.put(
            "/api/config-editor/config",
            headers=session_headers,
            json={"content": "instance_name: ["},
        )
        save_response = await client.put(
            "/api/config-editor/config",
            headers=session_headers,
            json={"content": updated_content, "revision": config_payload["revision"]},
        )
        download_response = await client.get(
            "/api/config-editor/download", headers=session_headers
        )
        save_payload = await save_response.json()
        backup_name = save_payload["backup"]["name"]
        stale_save = await client.put(
            "/api/config-editor/config", headers=session_headers,
            json={"content": original_content, "revision": config_payload["revision"]},
        )
        missing_revision = await client.put(
            "/api/config-editor/config", headers=session_headers, json={"content": original_content},
        )
        stale_restore = await client.post(
            f"/api/config-editor/backups/{backup_name}/restore", headers=session_headers,
            json={"revision": config_payload["revision"]},
        )
        assert stale_save.status == missing_revision.status == stale_restore.status == 409
        assert config_path.read_text(encoding="utf-8") == updated_content
        restore_response = await client.post(
            f"/api/config-editor/backups/{backup_name}/restore",
            headers=session_headers,
            json={"revision": save_payload["revision"]},
        )
        restart_response = await client.post(
            "/api/config-editor/restart", headers=session_headers
        )
        shell_text = await shell_response.text()
        config_payload = await config_response.json()
        invalid_save_text = await invalid_save_response.text()
        download_text = await download_response.text()
        restore_payload = await restore_response.json()
        restart_payload = await restart_response.json()

    assert shell_response.status == 200
    assert "/docs/assets/static-mesh.js" in shell_text
    assert "config-editor-highlight" in shell_text
    assert 'href="/webchat"' in shell_text
    assert motion_response.status == 200
    assert ui_motion_response.status == 200
    assert "window.UAMotion" in ui_motion_text
    assert activity_mesh_response.status == 200
    assert "export function createActivityMesh" in activity_mesh_text
    # The sign-in page plays the onboarding intro for someone new to the browser.
    assert intro_word_response.status == 200
    assert "window.UAIntroWord" in intro_word_text
    # The shared glass and the popups' opening and closing glass.
    assert glass_response.status == 200
    assert "window.UAGlass" in glass_text
    assert panel_motion_response.status == 200
    assert "window.UAPanelMotion" in panel_motion_text
    assert operational_ids_response.status == 200
    assert credential_controls_response.status == 200
    assert action_feedback_response.status == 200
    assert script_response.status == 200
    assert unauth_config_response.status == 401
    assert user_auth_response.status == 200
    assert user_config_response.status == 403
    assert auth_response.status == 200
    assert config_response.status == 200
    assert config_payload["content"] == original_content
    assert invalid_save_response.status == 400
    assert "Invalid YAML" in invalid_save_text
    assert save_response.status == 200
    assert Path(save_payload["backup"]["path"]).is_file()
    assert download_response.status == 200
    assert "Edited User" in download_text
    assert restore_response.status == 200
    assert restore_payload["restored_from"] == backup_name
    assert len(save_payload["backups"]) == 5
    assert len(restore_payload["backups"]) == 5
    assert len(list(config_path.parent.glob("instance_config.yaml.bak.*"))) == 5
    assert GatewayInstanceConfig.load(config_path).user.display_name == "Original User"
    assert restart_response.status == 200
    assert restart_payload["scope"] == "all"
    restart_requests = list(
        (config.instance_root_path / "runtime" / "restart_requests").glob(
            "restart_*.json"
        )
    )
    assert len(restart_requests) == 1
    restart_request = json.loads(restart_requests[0].read_text(encoding="utf-8"))
    assert restart_request["schema_version"] == 4
    assert restart_request["requested_by"] == "gateway_config_editor"
    assert restart_request["authority"] == {
        "mode": "authenticated_gateway_operator",
        "source": "bootstrap_config_editor_session",
        "canonical_user_id": config.user.canonical_user_id,
    }


@pytest.mark.asyncio
async def test_internal_website_config_editor_targets_the_saved_instance_config_when_started_elsewhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.user.display_name = "Canonical User"
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = (
        "WEB_CHAT_AUTH_TOKEN"
    )
    canonical_path = config.save()
    canonical_content = canonical_path.read_text(encoding="utf-8")
    updated_content = canonical_content.replace(
        "display_name: Canonical User", "display_name: Edited User"
    )
    assert updated_content != canonical_content

    # A process started from another file (TEAM_INSTANCE_CONFIG) still edits the saved one.
    runtime_path = tmp_path / "started_elsewhere" / "instance_config.yaml"
    runtime_config = GatewayInstanceConfig.load(canonical_path)
    runtime_config.user.display_name = "Runtime User"
    runtime_config.save(runtime_path)
    runtime_content = runtime_path.read_text(encoding="utf-8")
    launched_config = GatewayInstanceConfig.load(runtime_path)
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    app = InternalWebsiteInteractiveApp(
        launched_config, gateway=_FakeGateway(), bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        session_headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        config_response = await client.get(
            "/api/config-editor/config", headers=session_headers
        )
        config_payload = await config_response.json()
        save_response = await client.put(
            "/api/config-editor/config",
            headers=session_headers,
            json={"content": updated_content, "revision": config_payload["revision"]},
        )
        download_response = await client.get(
            "/api/config-editor/download", headers=session_headers
        )

        config_payload = await config_response.json()
        save_payload = await save_response.json()
        download_text = await download_response.text()

    assert auth_response.status == 200
    assert config_response.status == 200
    assert config_payload["path"] == str(canonical_path)
    assert config_payload["content"] == canonical_content
    assert save_response.status == 200
    assert save_payload["path"] == str(canonical_path)
    assert Path(save_payload["backup"]["path"]).parent == canonical_path.parent
    assert save_payload["backup"]["name"].startswith("instance_config.yaml.bak.")
    assert GatewayInstanceConfig.load(canonical_path).user.display_name == "Edited User"
    assert runtime_path.read_text(encoding="utf-8") == runtime_content
    assert GatewayInstanceConfig.load(runtime_path).user.display_name == "Runtime User"
    assert download_response.status == 200
    assert "Edited User" in download_text


@pytest.mark.asyncio
async def test_internal_website_proxies_live_agent_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _enable_web_chat(config, monkeypatch)
    monkeypatch.setenv("ACTIVITY_COLLECTOR_ADMIN_TOKEN", "admin-token")
    observed_authorization = []

    async def handle_agents(request: web.Request) -> web.Response:
        observed_authorization.append(request.headers.get("Authorization"))
        return web.json_response(
            {
                "agents": [
                    {
                        "agent_id": "manager-1",
                        "name": "Ava",
                        "named_agent_type": "TeamArchitect",
                        "is_team_lead": True,
                        "is_executing": False,
                        "has_execution_capacity": True,
                        "is_paused": False,
                        "is_exiting": False,
                        "top_level_queue_depth": 0,
                        "waiting_top_level_task_count": 0,
                        "deferred_message_count": 0,
                        "diagnostic": {"activity_source": "collector_events"},
                        "last_seen_at": "2026-06-05T12:00:00+00:00",
                        "recent_activity": [
                            {
                                "created_at": "2026-06-05T12:00:00+00:00",
                                "summary": "Manager heartbeat received.",
                                "activity_type": "status",
                                "stage": "completed",
                            }
                        ],
                    }
                ]
            }
        )

    collector_app = web.Application()
    collector_app.router.add_get("/api/admin/agents", handle_agents)
    async with TestClient(TestServer(collector_app)) as collector:
        config.activity_collector.base_url = str(collector.make_url("/")).rstrip("/")
        website = InternalWebsiteInteractiveApp(
            config, gateway=_FakeGateway(), bridge=SimpleNamespace()
        )
        async with TestClient(TestServer(website.build_app())) as client:
            response = await client.get("/api/agents/status", headers=await _signed_in(client))
            payload = await response.json()

    assert response.status == 200
    assert observed_authorization == ["Bearer admin-token"]
    assert payload["status"] == "ok"
    assert payload["source"] == "activity_collector"
    assert payload["agents"][0]["agent_id"] == "manager-1"
    assert (
        payload["agents"][0]["recent_activity"][0]["summary"]
        == "Manager heartbeat received."
    )


@pytest.mark.asyncio
async def test_web_chat_objectives_require_user_session_and_use_manager_authority(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _UserTokenStatefulFakeGateway(store)
    session_key = "website:web-chat:runtime-controls"
    store.create_session(
        session_key=session_key,
        conversation_id="conv_runtime_controls",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="user_alpha",
        chat_id="web-chat",
        owner_canonical_user_id="user_alpha",
    )
    observed_contexts: list[dict] = []

    async def list_objectives(**context):
        observed_contexts.append(context)
        return [
            {
                "objective_id": "objective-release",
                "record_digest": "a" * 64,
                "title": "Ship the release",
                "desired_outcome": "Publish a verified release.",
                "status": "active",
                "horizon": "finite",
                "priority": "normal",
                "current_focus": "Integration verification",
                "updated_at": "2026-08-24T12:00:00Z",
                "available_actions": ["pause", "stop", "abandon", "delete"],
            }
        ]

    gateway.list_web_chat_objectives = list_objectives
    website = InternalWebsiteInteractiveApp(
        config,
        gateway=gateway,
        bridge=SimpleNamespace(),
    )
    async with TestClient(TestServer(website.build_app())) as client:
        unauthorized = await client.get("/api/web-chat/objectives")
        auth_response = await client.post(
            "/api/web-chat/auth",
            json={"token": "ua_user_token"},
        )
        response = await client.get(
            "/api/web-chat/objectives?session_key=" + session_key
        )
        payload = await response.json()

    assert unauthorized.status == 401
    assert auth_response.status == 200
    assert response.status == 200
    assert observed_contexts[0]["canonical_user_id"] == "user_alpha"
    assert observed_contexts[0]["session_key"] == session_key
    assert payload == {
        "status": "ok",
        "schema_version": 1,
        "objectives": [
            {
                "schema_version": 1,
                "objective_id": "objective-release",
                "record_digest": "a" * 64,
                "title": "Ship the release",
                "desired_outcome": "Publish a verified release.",
                "status": "active",
                "horizon": "finite",
                "priority": "normal",
                "progress_summary": None,
                "current_focus": "Integration verification",
                "current_plan_id": None,
                "plan_summary": None,
                "subgoals": [],
                "success_criteria": [],
                "schedule_ids": [],
                "heartbeat_item_ids": [],
                "work_policy_status": None,
                "updated_at": "2026-08-24T12:00:00Z",
                "deletion_requested_at": None,
                "deleted_at": None,
                "deletion_summary": None,
                "available_actions": ["pause", "stop", "abandon", "delete"],
            }
        ],
    }


@pytest.mark.asyncio
async def test_runtime_controls_require_csrf_and_forward_exact_resource_versions(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    gateway = _UserTokenStatefulFakeGateway(store)
    session_key = "website:web-chat:runtime-mutations"
    store.create_session(
        session_key=session_key,
        conversation_id="conv_runtime_mutations",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="user_alpha",
        chat_id="web-chat",
        owner_canonical_user_id="user_alpha",
    )
    website = InternalWebsiteInteractiveApp(
        config,
        gateway=gateway,
        bridge=SimpleNamespace(),
    )
    async with TestClient(TestServer(website.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth",
            json={"token": "ua_user_token"},
        )
        auth_payload = await auth_response.json()
        common_payload = {"session_key": session_key}
        rejected = await client.post(
            "/api/web-chat/objectives/objective_exact/control",
            json={
                **common_payload,
                "action": "pause",
                "expected_record_digest": "a" * 64,
            },
        )
        trusted_headers = {
            "Origin": str(client.make_url("/")).rstrip("/"),
            "Sec-Fetch-Site": "same-origin",
            "X-UA-Permission-CSRF": auth_payload["permission_csrf_token"],
        }
        objective_response = await client.post(
            "/api/web-chat/objectives/objective_exact/control",
            headers=trusted_headers,
            json={
                **common_payload,
                "action": "pause",
                "expected_record_digest": "a" * 64,
            },
        )
        schedule_response = await client.post(
            "/api/web-chat/schedules/schedule_exact/control",
            headers=trusted_headers,
            json={
                **common_payload,
                "action": "disable",
                "expected_record_digest": "b" * 64,
            },
        )
        mcp_response = await client.post(
            "/api/web-chat/mcp/connection/connection_exact/control",
            headers=trusted_headers,
            json={
                **common_payload,
                "action": "disable",
                "expected_digest": "c" * 64,
                "expected_lifecycle": "active",
            },
        )

    assert rejected.status == 403
    assert objective_response.status == 200
    assert schedule_response.status == 200
    assert mcp_response.status == 200
    assert gateway.objective_control_calls[-1]["canonical_user_id"] == "user_alpha"
    assert gateway.objective_control_calls[-1]["objective_id"] == "objective_exact"
    assert gateway.objective_control_calls[-1]["expected_record_digest"] == "a" * 64
    assert gateway.schedule_control_calls[-1]["schedule_id"] == "schedule_exact"
    assert gateway.schedule_control_calls[-1]["expected_record_digest"] == "b" * 64
    assert gateway.mcp_control_calls[-1]["resource_id"] == "connection_exact"
    assert gateway.mcp_control_calls[-1]["expected_digest"] == "c" * 64


@pytest.mark.asyncio
async def test_internal_website_web_chat_routes_messages_sessions_and_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.agent.debug["context_dump"]["web_chat_controls_enabled"] = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="website:web-chat:browser_1",
        conversation_id="conv_web",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="browser_1",
        chat_id="web-chat",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    store.record_request(
        correlation_id="corr_web",
        session_key="website:web-chat:browser_1",
        request_kind="interactive",
        request_text="Hello there",
        conversation_id="conv_web",
        followup_to_task_id=None,
    )
    dump_dir = (
        config.layout.diagnostics_dir
        / "context_dumps"
        / "task_web"
        / "20260609T212513.000000Z_post_corr_web"
    )
    dump_dir.mkdir(parents=True)
    (dump_dir / "post_metadata.json").write_text(
        '{"created_at":"2026-06-09T21:25:13+00:00","phase":"post"}',
        encoding="utf-8",
    )
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        raw_token_response = await client.get(
            "/api/web-chat/sessions",
            headers={"X-Web-Chat-Token": "uaweb_test_token"},
        )
        unauth_debug_response = await client.patch(
            "/api/web-chat/debug/context-dump",
            json={"enabled": True, "mode": "always"},
        )
        unauth_debug_status_response = await client.get(
            "/api/web-chat/debug/context-dump"
        )
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        auth_payload = await auth_response.json()
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        session_headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        sessions_response = await client.get(
            "/api/web-chat/sessions",
            headers=session_headers,
        )
        debug_status_response = await client.get(
            "/api/web-chat/debug/context-dump",
            headers=session_headers,
        )
        debug_response = await client.patch(
            "/api/web-chat/debug/context-dump",
            headers=session_headers,
            json={
                "enabled": True,
                "mode": "always",
                "client_id": "browser_1",
                "session_key": "website:web-chat:browser_1",
                "conversation_id": "conv_web",
            },
        )
        message_response = await client.post(
            "/api/web-chat/chat",
            headers=session_headers,
            json={
                "message": "What is next?",
                "session_key": "website:web-chat:browser_1",
                "conversation_id": "conv_web",
                "client_request_id": "message_request_1",
            },
        )
        help_response = await client.post(
            "/api/web-chat/chat",
            headers=session_headers,
            json={
                "message": "/help",
                "session_key": "website:web-chat:browser_1",
                "client_request_id": "help_request_1",
            },
        )
        rename_response = await client.patch(
            "/api/web-chat/sessions/website:web-chat:browser_1",
            headers=session_headers,
            json={"session_alias": "Motor Research"},
        )
        archive_response = await client.post(
            "/api/web-chat/sessions/website:web-chat:browser_1/archive",
            headers=session_headers,
        )
        sessions_payload = await sessions_response.json()
        debug_status_payload = await debug_status_response.json()
        debug_payload = await debug_response.json()
        message_payload = await message_response.json()
        help_payload = await help_response.json()
        rename_payload = await rename_response.json()

    assert raw_token_response.status == 401
    assert auth_response.status == 200
    assert auth_payload["user_bound"] is True
    assert auth_payload["canonical_user_id"] == config.user.canonical_user_id
    assert sessions_response.status == 200
    # Named by its first ordinary transcript message; this one has none yet.
    assert sessions_payload["sessions"][0]["session_key"] == "website:web-chat:browser_1"
    assert sessions_payload["sessions"][0]["title"] is None
    assert "latest_request_text" not in sessions_payload["sessions"][0]
    assert unauth_debug_response.status == 401
    assert unauth_debug_status_response.status == 401
    assert debug_status_response.status == 200
    assert debug_status_payload["context_dump"]["last_dump"] == {
        "available": True,
        "dumped_at": "2026-06-09T21:25:13+00:00",
        "dump_id": "task_web",
        "task_id": "task_web",
        "correlation_id": "corr_web",
        "relative_path": "state/diagnostics/context_dumps/task_web/20260609T212513.000000Z_post_corr_web",
        "output_dir": "state/diagnostics/context_dumps",
    }
    assert debug_response.status == 200
    assert debug_payload["context_dump"]["enabled"] is True
    assert debug_payload["context_dump"]["mode"] == "always"
    assert debug_payload["context_dump"]["last_dump"]["available"] is True
    assert debug_payload["live_applied"] is True
    assert (
        GatewayInstanceConfig.load(config.config_path).agent.debug["context_dump"][
            "enabled"
        ]
        is True
    )
    assert gateway.context_debug_controls[-1]["enabled"] is True
    assert gateway.context_debug_controls[-1]["mode"] == "always"
    assert (
        gateway.context_debug_controls[-1]["session_key"]
        == "website:web-chat:browser_1"
    )
    assert (
        gateway.context_debug_controls[-1]["user_id"] == config.user.canonical_user_id
    )
    assert (
        gateway.context_debug_controls[-1]["canonical_user_id"]
        == config.user.canonical_user_id
    )
    assert message_response.status == 200
    assert _last_transcript_content(message_payload) == "Reply: What is next?"
    assert gateway.sent_user_messages[-1]["chat_id"] == "web-chat"
    assert help_response.status == 200
    assert help_payload["messages"][-1]["role"] == "system"
    assert "/auth" in _last_transcript_content(help_payload)
    assert "/list_sessions" not in _last_transcript_content(help_payload)
    assert rename_response.status == 200
    assert rename_payload["session_alias"] == "Motor Research"
    assert (
        store.get_session("website:web-chat:browser_1").session_alias
        == "Motor Research"
    )
    assert archive_response.status == 200
    assert store.get_session("website:web-chat:browser_1").status == "archived"


@pytest.mark.asyncio
async def test_internal_website_web_chat_user_sessions_are_owner_scoped(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="website:web-chat:alpha",
        conversation_id="conv_alpha",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="user_alpha",
        chat_id="web-chat",
        owner_canonical_user_id="user_alpha",
    )
    store.record_request(
        correlation_id="corr_alpha",
        session_key="website:web-chat:alpha",
        request_kind="interactive",
        request_text="Alpha request",
        conversation_id="conv_alpha",
        followup_to_task_id=None,
    )
    store.create_session(
        session_key="website:web-chat:beta",
        conversation_id="conv_beta",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="user_beta",
        chat_id="web-chat",
        owner_canonical_user_id="user_beta",
    )
    store.record_request(
        correlation_id="corr_beta",
        session_key="website:web-chat:beta",
        request_kind="interactive",
        request_text="Beta request",
        conversation_id="conv_beta",
        followup_to_task_id=None,
    )
    store.create_session(
        session_key="telegram:alpha",
        conversation_id="conv_telegram_alpha",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_alpha",
        chat_id="chat_alpha",
        owner_canonical_user_id="user_alpha",
    )
    gateway = _UserTokenStatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "ua_user_token"}
        )
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        session_headers = {"Cookie": f"ua_web_chat_session={session_cookie}"}
        sessions_response = await client.get(
            "/api/web-chat/sessions", headers=session_headers
        )
        rename_other_response = await client.patch(
            "/api/web-chat/sessions/website:web-chat:beta",
            headers=session_headers,
            json={"session_alias": "Should Not Work"},
        )
        archive_other_response = await client.post(
            "/api/web-chat/sessions/website:web-chat:beta/archive",
            headers=session_headers,
        )
        archive_other_channel_response = await client.post(
            "/api/web-chat/sessions/telegram:alpha/archive",
            headers=session_headers,
        )
        post_other_response = await client.post(
            "/api/web-chat/chat",
            headers=session_headers,
            json={
                "message": "Can I use beta?",
                "session_key": "website:web-chat:beta",
                "client_request_id": "cross_owner_request_1",
            },
        )
        sessions_payload = await sessions_response.json()

    assert auth_response.status == 200
    assert sessions_response.status == 200
    assert [session["session_key"] for session in sessions_payload["sessions"]] == [
        "website:web-chat:alpha"
    ]
    assert rename_other_response.status == 404
    assert archive_other_response.status == 404
    assert archive_other_channel_response.status == 404
    assert post_other_response.status == 404
    assert store.get_session("website:web-chat:beta").status == "active"
    assert store.get_session("website:web-chat:beta").session_alias is None
    assert store.get_session("telegram:alpha").status == "active"


@pytest.mark.asyncio
async def test_internal_website_web_chat_rename_rejects_alias_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    store.create_session(
        session_key="website:web-chat:browser_1",
        conversation_id="conv_web_1",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="browser_1",
        chat_id="web-chat",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    store.create_session(
        session_key="website:web-chat:browser_2",
        session_alias="Existing Name",
        conversation_id="conv_web_2",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="browser_2",
        chat_id="web-chat",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway = _StatefulFakeGateway(store)
    app = InternalWebsiteInteractiveApp(
        config, gateway=gateway, bridge=SimpleNamespace()
    )

    async with TestClient(TestServer(app.build_app())) as client:
        auth_response = await client.post(
            "/api/web-chat/auth", json={"token": "uaweb_test_token"}
        )
        session_cookie = auth_response.cookies["ua_web_chat_session"].value
        conflict_response = await client.patch(
            "/api/web-chat/sessions/website:web-chat:browser_1",
            headers={"Cookie": f"ua_web_chat_session={session_cookie}"},
            json={"session_alias": "existing name"},
        )

    assert conflict_response.status == 409
    assert store.get_session("website:web-chat:browser_1").session_alias is None


@pytest.mark.asyncio
async def test_web_chat_procedure_routes_list_add_and_reject_as_the_signed_in_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed-procedure reviews through HTTP, the real gateway composition and the real service."""

    from assistant_gateway.gateway import UserGateway
    from assistant_gateway.interactive_apps.manager import InteractiveAppManager
    from custom_agents import access_runtime
    from tests.change_approval_support import use_signed_contexts
    from tests.test_installed_permissions import _propose, _setup

    use_signed_contexts(monkeypatch)
    config, service, specialist, handler = _setup(tmp_path)
    config.authorization.enabled = False  # any active member may review (trusted single-person mode)
    config.gateway.users.enabled = True
    config.interactive_apps.internal_website.web_chat_enabled = True
    config.interactive_apps.internal_website.web_chat_token_env_var = "WEB_CHAT_AUTH_TOKEN"
    config.save(config.config_path)
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_procedure_token")
    added, declined = _propose(service, specialist), _propose(service, specialist)
    gateway = UserGateway(config)
    # The shipped handler registry is empty; this service carries the test handler.
    gateway.procedures._service = lambda: service
    manager = InteractiveAppManager(config, gateway.application, include_apps=["website"])
    website = _InternalWebsiteInteractiveApp(
        config.interactive_apps.internal_website, gateway.application, runtime=manager.runtime
    )
    try:
        async with TestClient(TestServer(website.build_app())) as client:
            proposals_url = "/api/web-chat/procedures/proposals"
            assert (await client.get(proposals_url, params={"session_key": "any"})).status == 401
            auth = await client.post("/api/web-chat/auth", json={"token": "uaweb_procedure_token"})
            csrf = (await auth.json())["permission_csrf_token"]
            session_key = (await (await client.post("/api/web-chat/sessions")).json())["session"]["session_key"]
            origin = str(client.make_url("/")).rstrip("/")
            headers = {"X-UA-Permission-CSRF": csrf, "Origin": origin, "Sec-Fetch-Site": "same-origin"}
            context = await client.post("/api/web-chat/session-context", headers=headers, json={"session_key": session_key})
            assert context.status == 200, await context.text()

            listed = await client.get(proposals_url, params={"session_key": session_key})
            assert listed.status == 200, await listed.text()
            payload = await listed.json()
            assert payload["total"] == 2 and {item["administrator_installable"] for item in payload["proposals"]} == {True}
            assert (await client.get(proposals_url, params={"session_key": session_key, "offset": "-1"})).status == 400

            install_url = f"{proposals_url}/{added['proposal_id']}/install"
            body = {"session_key": session_key, "explanation": "Checked the handler."}
            assert (await client.post(install_url, json=body)).status == 403, "the write checks apply"
            assert (
                await client.post(install_url, headers={**headers, "Origin": "https://evil.example"}, json=body)
            ).status == 403
            installed = await client.post(install_url, headers=headers, json=body)
            assert installed.status == 200, await installed.text()
            result = await installed.json()
            assert result["status"] == "installed"
            assert result["receipt"]["installed_by_principal_id"] == config.user.canonical_user_id
            rejected = await client.post(
                f"{proposals_url}/{declined['proposal_id']}/reject", headers=headers, json={"session_key": session_key}
            )
            assert rejected.status == 200, await rejected.text()
            assert (await rejected.json())["proposal"]["status"] == "rejected"
            again = await client.post(install_url, headers=headers, json=body)
            assert again.status in {400, 409}, "adding twice is refused with a plain error"
            assert set((await again.json())["error"]) >= {"code", "title", "message", "recovery"}

            definitions = await (
                await client.get("/api/web-chat/procedures/installed", params={"session_key": session_key})
            ).json()
            [view] = definitions["definitions"]
            assert view["definition"]["definition_id"] == result["definition"]["definition_id"]
            assert view["handler_registration_valid"] is True
            assert handler.calls == [], "adding a procedure runs nothing"
    finally:
        await gateway.close()
        access_runtime._runtime_access.cache_clear()

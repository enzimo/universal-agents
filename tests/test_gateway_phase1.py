from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

import assistant_gateway.application.broker_session_watch as broker_session_watch_module
import assistant_gateway.application.unsolicited_delivery as unsolicited_delivery_module
import assistant_gateway.gateway as gateway_module
import assistant_gateway.http_secure_broker as secure_broker_module
from assistant_gateway.application.automated_submission import (
    GatewayAutomationRequest,
    GatewayAutonomousTaskRequest,
)
from assistant_gateway.application.clarification import (
    selected_choice_ids_from_text,
)
from assistant_gateway.application.delivery_presentation import (
    GatewayDeliveryPresentation,
)
from assistant_gateway.application.interactive_commands import GatewayCommandRequest
from assistant_gateway.application.operational_control import (
    GatewayContextDebugControlRequest,
    GatewayStopControlRequest,
)
from assistant_gateway.application.request_context import GatewayRequestRoute
from assistant_gateway.application.ports import GatewaySecureBrokerRefusal
from assistant_gateway.bootstrap import bootstrap_instance, seed_default_gateway_user
from assistant_gateway.channel_bridge import ChannelBridge
from assistant_gateway.config import (
    INSTANCE_CONFIG_SCHEMA_VERSION,
    DeliverySettings,
    GatewayInstanceConfig,
)
from assistant_gateway.delivery import (
    evaluate_delivery,
    is_within_quiet_hours,
    summarize_delivery_batch,
)
from assistant_gateway.gateway import UserGateway
from assistant_gateway.nats_manager_runtime import NatsGatewayManagerRuntime
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.translation import ChannelInboundMessage
from custom_agents.conversation_store import ConversationStore
from custom_agents.heartbeat_store import HeartbeatStore, extract_active_heartbeat_tasks
from custom_agents.manager_bootstrap_runtime import migrate_legacy_heartbeat_markdown
from custom_agents.manager_control_runtime import (
    handle_manager_memory_command,
    handle_manager_topic_command,
)
from custom_agents.provider_error_runtime import PROVIDER_TIMEOUT_USER_MESSAGE
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_agents.team_composition import TEAM_COMPOSITION_SCHEMA_VERSION
from custom_messages.contracts.control_v1 import (
    AgentStatus,
    ControlInstruction,
    ManagerMemoryCommand,
    ManagerRuntimeWorkCommand,
    PinnedMemoryCommandPayload,
    SystemStatusMessage,
)
from custom_messages.contracts.credentials_v1 import (
    SecureCredentialCollectionRequest,
    SecureCredentialCollectionResult,
    ToolSetupResultV1,
)
from custom_messages.contracts.delivery_v1 import (
    DeliveryReceipt,
    UserNotification,
    UserNotificationAttachment,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import (
    GatewayContext,
    MessageHeader,
    MessageMeta,
)
from custom_messages.contracts.schedule_v1 import AutomationMessage, ScheduledMessage
from custom_messages.contracts.task_v1 import (
    Task,
    TaskAnswer,
    TaskProgressStatus,
    TaskQuery,
    TaskQueryChoiceOption,
    TaskStatus,
)
from custom_messages.enums import (
    ControlType,
    MessageMetaType,
    ProgressState,
    RecoverablePauseTrigger,
    RequesterType,
    RoutingType,
    TaskQueryReason,
)
from custom_messages.system_schedules import (
    person_system_schedule_id,
)
from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.transport import TransportSettings
from storage.conversation_repository import SqliteConversationRepository
from storage.pinned_memory_repository import create_pinned_memory_repository
from storage.knowledge_repository import create_knowledge_repository
from custom_agents.knowledge_command_runtime import handle_manager_knowledge_command


async def handle_interactive_session_command(*, gateway, **request):
    result = await gateway.command_application.execute(GatewayCommandRequest(**request))
    return result.handled, result.session_key, result.text


class _AsyncNullContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _CaptureManagerComms:
    def __init__(self) -> None:
        self.messages: list[MessageEnvelope] = []

    async def send_message(self, *, message: MessageEnvelope, routing_key: str) -> None:
        self.messages.append(message)


class _CaptureGatewayManagerTransport:
    def __init__(self, on_durable=None, on_operational=None) -> None:
        self.durable: list[tuple[MessageEnvelope, str]] = []
        self.operational: list[tuple[MessageEnvelope, str]] = []
        self._on_durable = on_durable
        self._on_operational = on_operational

    async def publish_durable(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        self.durable.append((envelope, routing_key))
        if self._on_durable is not None:
            await self._on_durable(envelope, routing_key)

    async def publish_operational(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        self.operational.append((envelope, routing_key))
        if self._on_operational is not None:
            await self._on_operational(envelope, routing_key)


class _FixedGatewayIds:
    def __init__(self, correlation_id: str = "corr_id_deadbeef") -> None:
        self.correlation_id = correlation_id

    def new_correlation_id(self) -> str:
        return self.correlation_id

    def new_local_correlation_id(self) -> str:
        return "local_corr_deadbeef"

    def new_scheduled_job_id(self) -> str:
        return "cron_deadbeef"

    def new_session_key(self) -> str:
        return "session_deadbeef00"

    def new_conversation_id(self) -> str:
        return "conversation_id_deadbeef0000"


class _FixedGatewayClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now_utc(self) -> datetime:
        return self.value


@pytest.fixture
def gateway_signed_commands(monkeypatch: pytest.MonkeyPatch):
    """The manager reads a command's person only from its gateway-signed context."""

    from tests.broker_access_support import context_verify_keys, forget_task_context_keys

    monkeypatch.setenv("ACCESS_CONTEXT_VERIFY_KEYS", context_verify_keys())
    forget_task_context_keys()
    yield
    forget_task_context_keys()


def _gateway_signer():
    from tests.broker_access_support import context_signer

    return context_signer()


def _attach_in_process_manager_control(
    gateway: UserGateway,
    config: GatewayInstanceConfig,
) -> ConversationStore:
    comms = _CaptureManagerComms()
    conversation_store = ConversationStore(
        db_path=config.manager_storage_settings.sqlite_path or "",
        storage_settings=config.manager_storage_settings, artifacts_dir=Path(config.manager_storage_settings.sqlite_path or "").parent / "conversation_artifacts",
    )
    manager = SimpleNamespace(
        task_manager=SimpleNamespace(operations_runtime=TaskOperationRuntime()),
        id=config.manager.manager_agent_id or "manager_1234",
        name="Manager",
        is_team_lead=True,
        team_id=config.manager.team_id,
        memory_config=dict(config.agent.memory or {}),
        _logger=logging.getLogger("test.in_process_manager"),
        _pinned_memory_repository=create_pinned_memory_repository(
            config.manager_storage_settings
        ),
        _conversation_store=conversation_store,
        knowledge_config={},
        _knowledge_repository=create_knowledge_repository(config.manager_storage_settings),
        team_comms=comms,
    )

    async def request_manager(request):
        command = request.command
        session = request.session
        comms.messages.clear()
        # As the gateway sends it: the signed-in person, gateway-signed.
        meta = MessageMeta(
            msg_correlation_id="corr_manager_control",
            conversation_id=session.conversation_id,
            request_timezone=config.user.timezone,
            gateway_context=GatewayContext(
                canonical_user_id=request.person.canonical_user_id,
                session_key=session.session_key,
                task_context=_gateway_signer().issue(
                    team_id=request.person.team_id,
                    person_id=request.person.canonical_user_id,
                    person_roles=request.person.roles,
                ),
            ),
        )
        header = MessageHeader(from_agent_id="gateway", response_routing_key="gateway")
        if command.message_class == "ManagerMemoryCommand":
            await handle_manager_memory_command(
                manager,
                command=command,
                msg_header=header,
                msg_meta=meta,
            )
        elif command.message_class == "ManagerKnowledgeCommand":
            await handle_manager_knowledge_command(
                manager,
                command=command,
                msg_header=header,
                msg_meta=meta,
            )
        else:
            await handle_manager_topic_command(
                manager,
                command=command,
                msg_header=header,
                msg_meta=meta,
            )
        return comms.messages[-1].msg_content

    gateway._manager_commands.request = request_manager  # type: ignore[method-assign]
    return conversation_store


class _FakeGatewayManagerRuntime:
    def __init__(
        self,
        *,
        has_live_consumer: bool | None = True,
    ) -> None:
        self.connected = True
        self.has_live_consumer = has_live_consumer
        self.probes: list[str] = []
        self.put_calls: list[dict[str, object]] = []
        self.durable: list[tuple[MessageEnvelope, str]] = []
        self.operational: list[tuple[MessageEnvelope, str]] = []

    async def connect(self, **_kwargs) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def publish_durable(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        self.durable.append((envelope, routing_key))

    async def publish_operational(
        self,
        envelope: MessageEnvelope,
        *,
        routing_key: str,
    ) -> None:
        self.operational.append((envelope, routing_key))

    def stopped_receivers(self) -> tuple:
        return ()

    async def jetstream_usage(self) -> None:
        return None

    async def manager_has_live_consumer(
        self,
        manager_id: str,
    ) -> bool | None:
        self.probes.append(manager_id)
        return self.has_live_consumer

    async def put_object(self, **kwargs):
        self.put_calls.append(dict(kwargs))
        return SimpleNamespace(name=kwargs.get("object_name"))


class _FakeIncomingMessage:
    def __init__(self, envelope: MessageEnvelope):
        self.body = envelope.model_dump_json().encode("utf-8")

    def process(self):
        return _AsyncNullContext()


class _FakeQueueIterator:
    def __init__(
        self, messages: list[_FakeIncomingMessage], *, block_forever: bool = False
    ):
        self._messages = list(messages)
        self._block_forever = block_forever

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        if self._block_forever:
            await asyncio.Future()
        raise StopAsyncIteration


class _FakeQueue:
    def __init__(
        self,
        name: str,
        messages: list[_FakeIncomingMessage],
        *,
        block_forever: bool = False,
    ):
        self.name = name
        self._messages = messages
        self._block_forever = block_forever
        self.consume_handler = None
        self.bound_exchange = None
        self.cancelled_consumer_tags: list[str] = []

    def iterator(self):
        return _FakeQueueIterator(self._messages, block_forever=self._block_forever)

    async def consume(self, handler):
        self.consume_handler = handler
        return "consumer_tag_1"

    async def bind(self, exchange):
        self.bound_exchange = exchange

    async def cancel(self, consumer_tag: str):
        self.cancelled_consumer_tags.append(consumer_tag)


class _FakeExchange:
    def __init__(self):
        self.published_messages: list[tuple[object, str]] = []

    async def publish(self, message, routing_key: str):
        self.published_messages.append((message, routing_key))


class _FakeResponseQueue:
    def __init__(self):
        self.consume_handler = None
        self.cancelled_consumer_tags: list[str] = []

    async def consume(self, handler):
        self.consume_handler = handler
        return "consumer_tag_1"

    async def cancel(self, consumer_tag: str):
        self.cancelled_consumer_tags.append(consumer_tag)


class _FakeConnectChannel:
    def __init__(self):
        self.response_queue = _FakeResponseQueue()
        self.broadcast_queue = _FakeQueue("gateway_broadcast_queue", [])
        self.prefetch_count = None
        self.closed = False
        self.exchange_declarations: list[dict[str, object]] = []

    async def set_qos(self, prefetch_count: int):
        self.prefetch_count = prefetch_count

    async def declare_queue(
        self,
        name: str | None = None,
        durable: bool = True,
        exclusive: bool = False,
        auto_delete: bool = False,
    ):
        if exclusive:
            return self.broadcast_queue
        return self.response_queue

    async def declare_exchange(self, *args, **kwargs):
        self.exchange_declarations.append(dict(kwargs))
        return _FakeExchange()

    async def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, channel: _FakeConnectChannel):
        self._channel = channel
        self.closed = False

    async def channel(self):
        return self._channel

    async def close(self):
        self.closed = True


class _FakeChannel:
    def __init__(self, queue: _FakeQueue, exchange: _FakeExchange):
        self.queue = queue
        self.exchange = exchange

    async def declare_queue(self, *args, **kwargs):
        return self.queue

    async def declare_exchange(self, *args, **kwargs):
        return self.exchange


class _FakePassiveQueue:
    def __init__(self, consumer_count: int | None):
        self.declaration_result = SimpleNamespace(consumer_count=consumer_count)


class _FakeLivenessChannel:
    def __init__(
        self,
        *,
        queue: _FakePassiveQueue | None = None,
        queue_error: Exception | None = None,
    ):
        self.queue = queue
        self.queue_error = queue_error

    async def declare_queue(self, *args, **kwargs):
        if self.queue_error is not None:
            raise self.queue_error
        return self.queue


def _fake_nats_adapter() -> SimpleNamespace:
    return SimpleNamespace(
        connect=AsyncMock(),
        subscribe_route=AsyncMock(),
        subscribe_subject=AsyncMock(),
        publish=AsyncMock(),
        close=AsyncMock(),
    )


def _build_test_config(tmp_path: Path) -> GatewayInstanceConfig:
    """A gateway's configuration, saved as a launched instance's always is."""

    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1234",
        heartbeat_interval_seconds=2,
    )
    config.save()
    return config


async def _link_bootstrap_admin_channel(
    gateway: UserGateway,
    config: GatewayInstanceConfig,
    *,
    channel_name: str,
    user_id: str,
    chat_id: str,
    thread_id: str | None = None,
) -> None:
    _, _, code_message = await handle_interactive_session_command(
        raw_text="/user code",
        gateway=gateway,
        current_session_key=None,
        channel_name="cli",
        user_id=config.user.canonical_user_id,
        chat_id=f"cli:{config.instance_name}",
    )
    assert code_message is not None
    code = str(code_message).split(": ", 1)[1].splitlines()[0]
    _, _, linked = await handle_interactive_session_command(
        raw_text=f"/user link {code}",
        gateway=gateway,
        current_session_key=None,
        channel_name=channel_name,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    assert linked is not None
    assert "Linked" in linked


def _contains_none(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_contains_none(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_none(child) for child in value)
    return False


def _unavailable_notice(config: GatewayInstanceConfig) -> str:
    return config.assistant.unavailable_message_template.format(
        assistant_name=config.assistant.display_name,
        agent_name=config.assistant.display_name,
    ).strip()


def _available_notice(config: GatewayInstanceConfig) -> str:
    return config.assistant.available_message_template.format(
        assistant_name=config.assistant.display_name,
        agent_name=config.assistant.display_name,
    ).strip()


def _telegram_transition_unavailable_notice(config: GatewayInstanceConfig) -> str:
    return (
        f"{_unavailable_notice(config)} Telegram polling will pause now, and Telegram only keeps missed "
        "bot messages for up to 24 hours while the manager is offline."
    )


def _telegram_transition_available_notice(config: GatewayInstanceConfig) -> str:
    return (
        f"{_available_notice(config)} Telegram polling will resume now and the gateway will process missed "
        "Telegram messages that are still available from the last 24 hours."
    )


@pytest.mark.asyncio
async def test_gateway_mirrors_cached_attachment_to_nats_object_store_with_fake_adapter(
    tmp_path: Path,
) -> None:
    config = _build_test_config(tmp_path)
    config.interactive_apps.internal_website.attachments.nats_object_bucket = (
        "test_attachments"
    )
    config.interactive_apps.internal_website.attachments.nats_object_ttl_seconds = 120
    config.interactive_apps.internal_website.attachments.nats_object_max_bytes = 4096
    store = GatewayStateStore(str(tmp_path / "state.db"))
    file_path = config.layout.uploads_dir / "website_web-chat_browser_1" / "att_1.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("hello remote worker", encoding="utf-8")
    store.upsert_attachment(
        attachment_id="att_1",
        session_key="website:web-chat:browser_1",
        conversation_id="conv_1",
        correlation_id="corr_1",
        filename="att_1.txt",
        content_type="text/plain",
        kind="document",
        size_bytes=file_path.stat().st_size,
        sha256="abc123",
        local_path=str(file_path),
    )
    manager_runtime = _FakeGatewayManagerRuntime()
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=store,
        manager_runtime=manager_runtime,
    )

    result = await gateway.attachment_replication.mirror("att_1")

    assert result.attachment_id == "att_1"
    assert result.bucket == "test_attachments"
    assert result.object_name == "att_1/att_1.txt"
    assert result.size_bytes == file_path.stat().st_size
    assert result.sha256 == "abc123"
    assert result.content_type == "text/plain"
    assert result.updated is True
    assert manager_runtime.put_calls == [
        {
            "bucket": "test_attachments",
            "object_name": "att_1/att_1.txt",
            "path": str(file_path.resolve()),
            "ttl_seconds": 120.0,
            "max_bytes": 4096,
            "headers": {
                "content-type": "text/plain",
                "filename": "att_1.txt",
                "sha256": "abc123",
                "attachment-id": "att_1",
            },
        }
    ]
    updated = store.get_attachment("att_1")
    assert updated is not None
    assert updated.nats_bucket == "test_attachments"
    assert updated.nats_object_name == "att_1/att_1.txt"


@pytest.mark.asyncio
async def test_gateway_reply_routing_trace_logs_pending_queue_match(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _build_test_config(tmp_path)
    config.agent.debug = {"reply_routing_trace": {"enabled": True}}
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    pending_queue: asyncio.Queue[MessageEnvelope] = asyncio.Queue()
    gateway._manager_client.register_response_channel(
        "corr_trace",
        queue=pending_queue,
    )
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_trace",
            task_type=MessageMetaType.RESPONSE,
            answer="Done.",
        ),
        msg_meta=MessageMeta(
            msg_correlation_id="corr_trace",
            conversation_id="conv_trace",
            gateway_context=GatewayContext(
                session_key="web_chat:default:browser_1",
                channel_name="web_chat",
                chat_id="default",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path="gateway_response",
            response_routing_key=None,
        ),
    )

    with caplog.at_level(logging.INFO, logger="assistant_gateway.gateway"):
        await gateway._manager_client.handle_response(envelope)

    assert await pending_queue.get() == envelope
    assert "event=gateway_response_matched_pending_queue" in caplog.text
    assert "corr_trace" in caplog.text
    assert "matched_pending=True" in caplog.text


@pytest.mark.asyncio
async def test_gateway_drops_unmatched_agent_status_response_without_user_delivery(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _build_test_config(tmp_path)
    config.agent.debug = {"reply_routing_trace": {"enabled": True}}
    outputs: list[str] = []
    gateway = UserGateway(
        config,
        output_sink=outputs.append,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="agent_id_web_research",
            team_id=config.manager.team_id,
            named_agent_type="WebResearchAgent",
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            name="researcher",
            agent_role="research",
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(msg_correlation_id="corr_discovery_late"),
        msg_header=MessageHeader(
            from_agent_id="agent_id_web_research",
            to_routing_path=config.manager.response_queue_name,
            response_routing_key=None,
            agent_signature="",
        ),
    )

    with caplog.at_level(logging.INFO, logger="assistant_gateway.gateway"):
        await gateway._manager_client.handle_response(envelope)

    assert outputs == []
    assert "gateway_operational_agent_status_unmatched_pending_dropped" in caplog.text
    assert "Gateway reply missing gateway_context" not in caplog.text
    assert "Gateway reply routing fell back to default session" not in caplog.text


def _manager_status_envelope(
    config: GatewayInstanceConfig,
    *,
    manager_id: str = "manager_live_4321",
    is_exiting: bool = False,
) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=AgentStatus(
            self_id=manager_id,
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=is_exiting,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id=manager_id,
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )


def _set_pending_query_created_at(
    db_path: Path, *, task_query_id: str, created_at: str
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE gateway_questions SET created_at = ? WHERE task_query_id = ?",
            (created_at, task_query_id),
        )
        conn.commit()


def _response_envelope(
    *, correlation_id: str, conversation_id: str, task_id: str, answer: str
) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(
            task_id=task_id,
            task_type=MessageMetaType.RESPONSE,
            answer=TaskAnswer(answer=answer),
        ),
        msg_meta=MessageMeta(
            msg_correlation_id=correlation_id,
            conversation_id=conversation_id,
        ),
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path="user_queue",
            agent_signature="",
        ),
    )


def _timer_response_envelope(
    *,
    correlation_id: str,
    conversation_id: str,
    task_id: str,
    session_key: str,
    scheduled_job_id: str,
    answer: str,
) -> MessageEnvelope:
    return MessageEnvelope(
        msg_content=Task(
            task_id=task_id,
            task_type=MessageMetaType.RESPONSE,
            answer=TaskAnswer(answer=answer),
        ),
        msg_meta=MessageMeta(
            msg_correlation_id=correlation_id,
            conversation_id=conversation_id,
            requester_type=RequesterType.TIMER_REQUESTER,
            gateway_context=GatewayContext(
                session_key=session_key,
                channel_name="local",
                user_id="test-user",
                chat_id="chat_101",
                delivery_mode="cron",
                scheduled_job_id=scheduled_job_id,
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path="user_queue",
            agent_signature="",
        ),
    )


def test_onboarding_bootstrap_creates_expected_files(tmp_path: Path):
    config = _build_test_config(tmp_path)
    created = bootstrap_instance(config)
    created_paths = {path.name for path in created}

    assert "instance_config.yaml" in created_paths
    assert "SOUL.md" in created_paths
    assert "USER.md" not in created_paths
    assert "TOOLS.md" not in created_paths
    assert "LLM_OVERRIDES.yaml" in created_paths
    assert "TEAM_TEMPLATES.yaml" in created_paths
    assert "TEAM_AGENTS.yaml" in created_paths
    assert "HEARTBEAT.md" not in created_paths
    assert "DELIVERY.md" not in created_paths
    layout = config.layout
    # Control files are written to control/, never into the agents' workspaces.
    assert {path.parent for path in created if path.name != "instance_config.yaml"} == {
        layout.control_dir
    }
    for area in (layout.control_dir, layout.state_dir, layout.dbs_dir, layout.users_dir, layout.team_dir, layout.agents_dir):
        assert area.is_dir()
    assert not (layout.control_dir / "DELIVERY.md").exists()
    assert config.config_path.exists()
    assert config.user.canonical_user_id == config.user.display_name
    saved_text = config.config_path.read_text(encoding="utf-8")
    assert f"schema_version: {INSTANCE_CONFIG_SCHEMA_VERSION}" in saved_text
    assert f"canonical_user_id: {config.user.display_name}" in saved_text
    assert "state:" in saved_text
    assert "storage:" in saved_text
    saved_yaml = yaml.safe_load(saved_text)
    # Catalog nulls disable inherited model options; incidental defaults stay omitted.
    assert saved_yaml["llm"].pop("model_capabilities") == config.llm.model_capabilities
    assert not _contains_none(saved_yaml)
    assert "agent_types" not in saved_yaml
    assert "work_context:" not in saved_text
    assert "preferences_notes:" not in saved_text

    assert not (layout.control_dir / "USER.md").exists()
    assert not (layout.control_dir / "TOOLS.md").exists()
    assert not (layout.control_dir / "HEARTBEAT.md").exists()
    assert not any(config.workspace_path.rglob("*.md"))

    overrides_text = layout.llm_overrides_path.read_text(
        encoding="utf-8"
    )
    assert "ToolUsingAgent" not in overrides_text
    assert "PersonalAdminAgent" in overrides_text
    assert "ResearchComparisonAgent" in overrides_text
    assert "InternalImprovementAgent" in overrides_text
    assert "IntegrationAgent" not in overrides_text
    assert "CodebaseAnalysisAgent" not in overrides_text
    assert "TestVerificationAgent" not in overrides_text
    overrides = yaml.safe_load(overrides_text)
    assert overrides["default"]["reasoning_summary"] is None
    assert overrides["default"]["litellm_api_mode"] == "chat_completions"
    assert overrides["agent_types"]["TeamArchitect"]["reasoning_summary"] is None
    assert (
        overrides["agent_types"]["TeamArchitect"]["litellm_api_mode"]
        == "chat_completions"
    )

    team_templates_text = layout.team_templates_path.read_text(
        encoding="utf-8"
    )
    assert "personal_operations_team" in team_templates_text
    assert "internal_tooling_team" in team_templates_text
    assert "PersonalAdminAgent" in team_templates_text
    assert "InternalImprovementAgent" in team_templates_text
    assert "IntegrationAgent" not in team_templates_text
    assert "CodebaseAnalysisAgent" not in team_templates_text
    assert "TestVerificationAgent" not in team_templates_text

    team_agents_text = layout.team_composition_path.read_text(
        encoding="utf-8"
    )
    team_agents = yaml.safe_load(team_agents_text)
    assert team_agents["schema_version"] == TEAM_COMPOSITION_SCHEMA_VERSION
    assert "approved_attachments" not in team_agents
    assert (
        "generated_agent_bundles_dir: agent_bundles/team_lead_generated"
        in team_agents_text
    )


def test_onboarding_seed_default_gateway_user_creates_registry_rows(tmp_path: Path):
    config = _build_test_config(tmp_path)
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "chat_123"
    config.delivery.default_target.thread_id = "thread_456"
    bootstrap_instance(config)

    store = GatewayStateStore(
        str(tmp_path / "state.db"), storage_settings=config.state_storage_settings
    )
    assert seed_default_gateway_user(config, state_store=store) is True

    user = store.get_gateway_user(config.user.canonical_user_id)
    assert user is not None
    assert user.display_name == config.user.display_name
    memberships = store.list_team_memberships(
        config.user.canonical_user_id, status="active"
    )
    assert [(membership.team_id, membership.role) for membership in memberships] == [
        (config.gateway.team_id, "owner")
    ]
    binding = store.get_active_channel_binding(
        channel_name="cli",
        external_user_id=config.user.canonical_user_id,
        chat_scope=f"cli:{config.instance_name}",
    )
    assert binding is not None
    assert binding.canonical_user_id == config.user.canonical_user_id
    user = store.get_gateway_user(config.user.canonical_user_id)
    assert user is not None
    assert user.default_channel_name == "telegram"
    assert user.default_chat_id == "chat_123"
    assert user.default_thread_id == "thread_456"


def test_seed_keeps_an_existing_membership_role(tmp_path: Path):
    """Instances set up before owners existed keep their administrator, and a
    role changed with /user role is not reset at the next start."""

    config = _build_test_config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "state.db"))
    assert seed_default_gateway_user(config, state_store=store) is True
    store.create_team_membership(
        canonical_user_id=config.user.canonical_user_id,
        team_id=config.gateway.team_id,
        role="admin",
    )

    assert seed_default_gateway_user(config, state_store=store) is True

    memberships = store.list_team_memberships(config.user.canonical_user_id)
    assert [(membership.team_id, membership.role) for membership in memberships] == [
        (config.gateway.team_id, "admin")
    ]


def test_seed_default_gateway_user_binds_first_legacy_allowed_channel_ids(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.interactive_apps.telegram.allowed_user_ids = ["tg_admin", "tg_other"]
    config.interactive_apps.discord.allowed_user_ids = ["dc_admin", "dc_other"]
    config.interactive_apps.whatsapp.allowed_sender_ids = [
        "+15550000001",
        "+15550000009",
    ]
    config.interactive_apps.signal.allowed_sender_ids = ["+15550000002"]
    config.interactive_apps.photon.allowed_sender_ids = ["+15550000003"]
    store = GatewayStateStore(str(tmp_path / "state.db"))

    assert seed_default_gateway_user(config, state_store=store) is True

    expected_bindings = {
        ("telegram", "tg_admin"),
        ("discord", "dc_admin"),
        ("whatsapp", "+15550000001"),
        ("signal", "+15550000002"),
        ("photon", "+15550000003"),
    }
    for channel_name, external_user_id in expected_bindings:
        binding = store.get_active_channel_binding(
            channel_name=channel_name,
            external_user_id=external_user_id,
            chat_scope="",
        )
        assert binding is not None
        assert binding.canonical_user_id == config.user.canonical_user_id
        assert binding.metadata["seeded_from"] == "legacy_allowlist_first_entry"

    assert (
        store.get_active_channel_binding(
            channel_name="telegram",
            external_user_id="tg_other",
            chat_scope="",
        )
        is None
    )


def test_gateway_startup_migrates_only_gateway_owned_legacy_rows(tmp_path: Path):
    config = _build_test_config(tmp_path)
    state_store = GatewayStateStore(str(tmp_path / "state.db"))
    state_store.create_session(
        session_key="legacy_session",
        conversation_id="legacy_conv",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="website",
        user_id="legacy_browser",
        chat_id="web-chat",
    )
    state_store.register_conversation_session(
        session_key="legacy_session",
        conversation_id="legacy_conv",
    )
    conversation_repo = SqliteConversationRepository(
        config.manager_storage_settings.sqlite_path or "", artifacts_dir=Path(config.manager_storage_settings.sqlite_path or "").parent / "conversation_artifacts"
    )
    conversation_repo.ensure_thread(
        conversation_id="legacy_conv",
        team_id=config.gateway.team_id,
        owner_agent_id=config.manager.manager_agent_id,
        owner_agent_name="Manager",
        requester_name="Legacy User",
        requester_id="legacy_browser",
        root_task_id="legacy_task",
    )

    UserGateway(config, output_sink=None, state_store=state_store)

    migrated_session = state_store.get_session("legacy_session")
    assert migrated_session is not None
    assert migrated_session.owner_canonical_user_id == config.user.canonical_user_id
    migrated_mapping = state_store.get_session_conversation("legacy_conv")
    assert migrated_mapping is not None
    assert migrated_mapping.owner_canonical_user_id == config.user.canonical_user_id
    migrated_thread = conversation_repo.get_thread("legacy_conv")
    assert migrated_thread is not None
    assert migrated_thread["owner_canonical_user_id"] is None


def test_gateway_refreshes_delivery_preferences_from_instance_config_file(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    updated = GatewayInstanceConfig.load(config.config_path)
    updated.delivery.quiet_hours.enabled = True
    updated.delivery.quiet_hours.start_hour_local = 20
    updated.delivery.quiet_hours.end_hour_local = 6
    updated.delivery.default_target.channel_name = "telegram"
    updated.delivery.default_target.chat_id = "-100456"
    updated.save()
    gateway._delivery_preferences._config_file_mtime_ns = -1

    gateway._delivery_preferences.refresh()
    policy = gateway._delivery_preferences.current_delivery_policy()

    assert gateway.config.delivery.quiet_hours.enabled is True
    assert gateway.config.delivery.quiet_hours.start_hour_local == 20
    assert gateway.config.delivery.default_target.channel_name == "telegram"
    assert gateway.config.delivery.default_target.chat_id == "-100456"
    assert policy.quiet_hours.enabled is True
    assert policy.quiet_hours.start_hour_local == 20
    assert policy.quiet_hours.end_hour_local == 6
    assert not (config.workspace_path / "DELIVERY.md").exists()


def test_onboarding_bootstrap_copies_launch_env_file_outside_workspace(tmp_path: Path):
    config = _build_test_config(tmp_path)
    source_env_path = tmp_path / "source.env"
    source_env_path.write_text("OPENAI_API_KEY=test-key\n", encoding="utf-8")
    config.launch.env_file_path = str(
        (config.instance_root_path / "team.env").resolve()
    )

    created = bootstrap_instance(config, source_env_file_path=str(source_env_path))

    copied_env_path = config.instance_root_path / "team.env"
    assert copied_env_path in created
    assert copied_env_path.exists()
    assert copied_env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=test-key\n"
    assert str(copied_env_path) == config.launch.env_file_path
    assert not str(copied_env_path).startswith(str(config.workspace_path))

    loaded = GatewayInstanceConfig.load(config.config_path)
    assert loaded.launch.env_file_path == str(copied_env_path)


def test_onboarding_bootstrap_merges_launch_env_file_with_source_overrides(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    source_env_path = tmp_path / "source.env"
    source_env_path.write_text(
        "OPENAI_API_KEY=source-key\nEXA_API_KEY=source-exa\nCRAWL4AI_API_TOKEN=source-crawl\nEMPTY_VALUE=filled\n",
        encoding="utf-8",
    )
    config.launch.env_file_path = str(
        (config.instance_root_path / "team.env").resolve()
    )
    copied_env_path = config.instance_root_path / "team.env"
    copied_env_path.parent.mkdir(parents=True, exist_ok=True)
    copied_env_path.write_text(
        "OPENAI_API_KEY=existing-key\nEMPTY_VALUE=\n", encoding="utf-8"
    )

    created = bootstrap_instance(config, source_env_file_path=str(source_env_path))

    env_text = copied_env_path.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=source-key" in env_text
    assert "OPENAI_API_KEY=existing-key" not in env_text
    assert "EMPTY_VALUE=filled" in env_text
    # Keys kept in the credential store never reach team.env (startup refuses
    # them there); the Exa key is handed over for the team's linked account.
    assert "EXA_API_KEY" not in env_text and "CRAWL4AI_API_TOKEN" not in env_text
    from assistant_gateway.onboarding_account_keys import read_handover

    handover = read_handover(config.instance_root)
    assert handover is not None and handover.keys == {"http:exa": "source-exa"}
    assert config.layout.onboarding_account_keys_path in created


def test_onboarding_bootstrap_copies_a_new_team_env_without_credential_store_keys(tmp_path: Path) -> None:
    config = _build_test_config(tmp_path)
    source_env_path = tmp_path / "source.env"
    source_env_path.write_text(
        "# imported\nOPENAI_API_KEY=source-key\nBRAVE_SEARCH_API_KEY=source-brave\nEXA_API_KEY_FILE=/run/secrets/exa\n",
        encoding="utf-8",
    )
    config.launch.env_file_path = str((config.instance_root_path / "team.env").resolve())

    bootstrap_instance(config, source_env_file_path=str(source_env_path))

    env_text = (config.instance_root_path / "team.env").read_text(encoding="utf-8")
    assert env_text == "# imported\nOPENAI_API_KEY=source-key\n"
    from assistant_gateway.onboarding_account_keys import read_handover

    handover = read_handover(config.instance_root)
    assert handover is not None and handover.keys == {"http:brave-search": "source-brave"}


def test_onboarding_bootstrap_writes_instance_root_hint_when_container_data_root_is_known(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _build_test_config(tmp_path)
    data_root = tmp_path / "mounted-data"
    hint_path = data_root / ".team-instance-root"
    monkeypatch.setenv("TEAM_DATA_ROOT", str(data_root))

    created = bootstrap_instance(config)

    assert hint_path in created
    assert hint_path.read_text(encoding="utf-8").strip() == str(
        config.instance_root_path
    )


def test_gateway_instance_config_save_strips_secret_like_env_owned_fields(
    tmp_path: Path,
):
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_storage_backend="postgres",
        manager_storage_postgres_dsn="postgresql://team:topsecret@db.internal:5432/team_runtime",
    )
    config.transport = TransportSettings(
        nats_servers=["nats://nats.example.com:4222"],
        nats_user="team_user",
        nats_password="super-secret",
    )

    config.save()

    saved_text = config.config_path.read_text(encoding="utf-8")
    assert "nats://nats.example.com:4222" in saved_text
    assert "nats_user: team_user" in saved_text
    assert "nats_password:" not in saved_text
    assert "user_token:" not in saved_text
    assert "admin_token:" not in saved_text
    assert "ingest_token:" not in saved_text
    assert "postgresql://team:topsecret@db.internal:5432/team_runtime" not in saved_text
    assert "dsn:" not in saved_text

    loaded = GatewayInstanceConfig.load(config.config_path)
    assert loaded.transport.nats_user == "team_user"
    assert loaded.transport.nats_password is None
    assert loaded.gateway.storage.backend == "postgres"
    assert loaded.gateway.storage.postgres.dsn is None
    assert loaded.state.storage.backend == "postgres"
    assert loaded.state.storage.postgres.dsn is None


def test_gateway_instance_config_load_save_preserves_explicit_config_path(
    tmp_path: Path,
):
    instance_root = tmp_path / "instance"
    runtime_path = tmp_path / "elsewhere" / "instance_config.yaml"
    original_path = instance_root / "instance_config.yaml"
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(instance_root),
        team_id="team_test",
        timezone="America/Chicago",
    )
    config.voice.enabled = True
    config.save(runtime_path)
    original_path.parent.mkdir(parents=True, exist_ok=True)
    original_path.write_text(
        "sentinel: original\nvoice:\n  enabled: true\n", encoding="utf-8"
    )

    loaded = GatewayInstanceConfig.load(runtime_path)
    loaded.manager.manager_agent_id = "manager_live"
    loaded.voice.enabled = False
    loaded.save()

    runtime_text = runtime_path.read_text(encoding="utf-8")
    original_text = original_path.read_text(encoding="utf-8")
    assert loaded.config_path == runtime_path.resolve()
    assert "manager_live" in runtime_text
    assert "enabled: false" in runtime_text
    assert original_text == "sentinel: original\nvoice:\n  enabled: true\n"


def test_gateway_instance_config_resolves_relative_schedule_db_under_instance_root(
    tmp_path: Path,
):
    instance_root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1234",
        manager_storage_sqlite_path="data/task_history.db",
    )

    assert config.state.storage.backend == "sqlite"
    assert config.state.storage.sqlite.path == str(
        (instance_root / "data/task_history.db").resolve()
    )
    # Databases stay outside workspaces/, where agents cannot open them.
    assert config.layout.is_instance_private(Path(config.state.storage.sqlite.path))


def test_gateway_state_store_persists_session_and_deliveries(tmp_path: Path):
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    store.create_session(
        session_key="default",
        conversation_id="conv_1",
        manager_agent_id="manager_1234",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
        thread_id="thread_1",
    )
    store.upsert_session(
        session_key="default",
        conversation_id="conv_1",
        last_task_id="task_1",
        manager_agent_id="manager_1234",
    )
    session = store.get_session("default")
    assert session is not None
    assert session.conversation_id == "conv_1"
    assert session.last_task_id == "task_1"
    assert session.chat_id == "chat_1"
    assert session.thread_id == "thread_1"

    delivery_id = store.add_deferred_delivery(
        session_key="default",
        origin_kind="cron",
        payload={"hello": "world"},
        rendered_text="Deferred result",
        available_after="2000-01-01T00:00:00+00:00",
    )
    due = store.get_due_deliveries()
    assert len(due) == 1
    assert due[0].delivery_id == delivery_id


def test_gateway_state_store_expires_pending_queries(tmp_path: Path):
    db_path = tmp_path / "gateway_state.db"
    store = GatewayStateStore(str(db_path))
    store.add_pending_query(
        session_key="session_1",
        correlation_id="corr_1",
        task_query_id="task_query_1",
        parent_task_id="task_1",
        conversation_id="conv_1",
        query_text="Continue?",
    )
    _set_pending_query_created_at(
        db_path,
        task_query_id="task_query_1",
        created_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
    )

    expired = store.expire_pending_queries(
        cutoff_iso=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    )

    assert expired == 1
    assert store.list_open_pending_queries("session_1") == []


def test_gateway_startup_expires_old_pending_queries(tmp_path: Path):
    db_path = tmp_path / "gateway_state.db"
    store = GatewayStateStore(str(db_path))
    store.add_pending_query(
        session_key="session_1",
        correlation_id="corr_1",
        task_query_id="task_query_1",
        parent_task_id="task_1",
        conversation_id="conv_1",
        query_text="Continue?",
    )
    _set_pending_query_created_at(
        db_path,
        task_query_id="task_query_1",
        created_at=(datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
    )
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1234",
        request_timeout_seconds=60,
        manager_storage_sqlite_path=str(tmp_path / "manager.db"),
    )
    gateway = UserGateway(config, output_sink=None, state_store=store)

    gateway._clarification_core.expire_stale_on_startup()

    assert store.list_open_pending_queries("session_1") == []


@pytest.mark.asyncio
async def test_gateway_clears_pending_query_when_manager_reports_parent_missing(
    tmp_path: Path,
):
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    store.add_pending_query(
        session_key="session_1",
        correlation_id="corr_1",
        task_query_id="task_query_1",
        parent_task_id="task_missing",
        conversation_id="conv_1",
        query_text="Continue?",
    )
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1234",
        manager_storage_sqlite_path=str(tmp_path / "manager.db"),
    )
    gateway = UserGateway(config, output_sink=None, state_store=store)
    gateway._manager_client.set_runtime(
        _FakeGatewayManagerRuntime(),
        use_for_outbound=False,
    )

    async def fake_publish(envelope: MessageEnvelope) -> None:
        request = envelope.msg_content
        assert isinstance(request, TaskStatus)
        response = request.model_copy(
            update={
                "status_type": MessageMetaType.RESPONSE,
                "is_known_task": False,
                "responder_agent_id": "manager_1234",
            }
        )
        await gateway._manager_client.get_control_channel(
            envelope.msg_meta.msg_correlation_id
        ).put(
            MessageEnvelope(
                msg_content=response,
                msg_header=MessageHeader(from_agent_id="manager_1234"),
                msg_meta=envelope.msg_meta,
            )
        )

    gateway._manager_client.publish_operational = fake_publish  # type: ignore[method-assign]

    assert await gateway._clarification_core.resolve_waiting_question("session_1") is None
    assert store.list_open_pending_queries("session_1") == []


@pytest.mark.asyncio
async def test_gateway_preserves_pending_query_when_manager_reports_parent_active(
    tmp_path: Path,
):
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    store.add_pending_query(
        session_key="session_1",
        correlation_id="corr_1",
        task_query_id="task_query_1",
        parent_task_id="task_1",
        conversation_id="conv_1",
        query_text="Continue?",
    )
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1234",
        manager_storage_sqlite_path=str(tmp_path / "manager.db"),
    )
    gateway = UserGateway(config, output_sink=None, state_store=store)
    gateway._manager_client.set_runtime(
        _FakeGatewayManagerRuntime(),
        use_for_outbound=False,
    )

    async def fake_publish(envelope: MessageEnvelope) -> None:
        request = envelope.msg_content
        assert isinstance(request, TaskStatus)
        response = request.model_copy(
            update={
                "status_type": MessageMetaType.RESPONSE,
                "is_known_task": True,
                "responder_agent_id": "manager_1234",
            }
        )
        await gateway._manager_client.get_control_channel(
            envelope.msg_meta.msg_correlation_id
        ).put(
            MessageEnvelope(
                msg_content=response,
                msg_header=MessageHeader(from_agent_id="manager_1234"),
                msg_meta=envelope.msg_meta,
            )
        )

    gateway._manager_client.publish_operational = fake_publish  # type: ignore[method-assign]

    pending_query = await gateway._clarification_core.resolve_waiting_question("session_1")

    assert pending_query is not None
    assert pending_query.task_query_id == "task_query_1"


@pytest.mark.asyncio
async def test_question_mark_uses_operational_progress_path_without_recording_task(
    tmp_path: Path,
):
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1234",
    )
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    gateway = UserGateway(config, output_sink=None, state_store=store)
    gateway._manager_client.set_runtime(
        _FakeGatewayManagerRuntime(),
        use_for_outbound=False,
    )

    async def fake_publish(envelope: MessageEnvelope) -> None:
        request = envelope.msg_content
        assert isinstance(request, TaskProgressStatus)
        response = request.model_copy(
            update={
                "status_type": MessageMetaType.RESPONSE,
                "status": "active",
                "root_task_id": "task_active",
                "root_state": ProgressState.WAITING,
                "plan_phase": "Collect vehicle specifications",
                "last_activity": "ResearchComparisonAgent is comparing trims.",
                "responder_agent_id": "manager_1234",
            }
        )
        await gateway._manager_client.get_control_channel(
            envelope.msg_meta.msg_correlation_id
        ).put(
            MessageEnvelope(
                msg_content=response,
                msg_header=MessageHeader(from_agent_id="manager_1234"),
                msg_meta=envelope.msg_meta,
            )
        )

    gateway._manager_client.publish_operational = fake_publish  # type: ignore[method-assign]

    response = await gateway.application.send_user_message("?")

    assert response.message_type == "task_progress_status"
    assert response.task_id == "task_active"
    assert "Collect vehicle specifications" in response.text
    assert store.list_requests_for_session(config.user.default_session_key) == []


def test_delivery_presentation_preserves_credential_form_ready_task_reply() -> None:
    answer = (
        "The credential collection form is ready. The system is now waiting for you to enter "
        "your Home Assistant username and password. You'll see a secure form appear in this chat."
    )
    envelope = _response_envelope(
        correlation_id="corr_1",
        conversation_id="conv_1",
        task_id="task_1",
        answer=answer,
    )

    assert GatewayDeliveryPresentation.render_envelope(envelope) == answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "expected_label", "expected_description"),
    [
        (
            "credential_collection",
            "Open secure credential form",
            "submit the requested fields",
        ),
        (
            "credential_deletion",
            "Open deletion confirmation",
            "review and confirm deletion",
        ),
    ],
)
@pytest.mark.parametrize(
    ("public_port", "expected_public_base_url"),
    [
        (18600, "http://192.168.1.50:18600"),
        (None, "http://192.168.1.50"),
    ],
)
async def test_gateway_credential_form_uses_configured_public_broker_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flow: str,
    expected_label: str,
    expected_description: str,
    public_port: int | None,
    expected_public_base_url: str,
) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1234",
    )
    config.network.public_origin = "http://192.168.1.50"
    config.security_layer.mode = "broker"
    config.security_layer.broker_internal_base_url = "http://127.0.0.1:8766"
    config.security_layer.broker_public_port = public_port
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.setenv("SECURE_TOOL_BROKER_PUBLIC_PORT", "8766")
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    observed_post: dict[str, object] = {}
    poll_kwargs: dict[str, object] = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return {
                "session_id": "session_123",
                "form_url": "http://127.0.0.1:8766/credential-collection/session_123",
                "status_url": "http://127.0.0.1:8766/v1/credential-collection/sessions/session_123",
                "expires_at": "2099-08-29T04:06:47+00:00",
            }

    class FakeClientSession:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def request(self, method, url, *, headers=None, json=None):
            observed_post.update(
                {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "json": json,
                    "timeout": self.kwargs.get("timeout"),
                }
            )
            return FakeResponse()

    async def fake_poll_secure_credential_collection_session(**kwargs):
        poll_kwargs.update(kwargs)

    monkeypatch.setattr(
        secure_broker_module.aiohttp,
        "ClientSession",
        FakeClientSession,
    )
    monkeypatch.setattr(
        gateway._credential_workflow,
        "poll_session",
        fake_poll_secure_credential_collection_session,
    )
    gateway.state_store.create_session(
        session_key="session_1",
        conversation_id="conv_1",
        manager_agent_id=config.manager.manager_agent_id,
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway.state_store.record_request(
        correlation_id="corr_1",
        session_key="session_1",
        request_kind="interactive",
        request_text="store a test credential",
        conversation_id="conv_1",
        followup_to_task_id=None,
        status="pending",
    )
    request_content = SecureCredentialCollectionRequest(
        service_name="Sealbox",
        credential_key="test_credential",
        purpose="Store or delete a test credential.",
        flow=flow,
        source="sealbox",
        source_item_id="test_credential",
        parent_task_id="task_1",
    )

    response = await gateway._credential_workflow.start_collection(
        request_content=request_content,
        request_envelope=_response_envelope(
            correlation_id="corr_1",
            conversation_id="conv_1",
            task_id="task_1",
            answer="",
        ),
        correlation_id="corr_1",
        conversation_id="conv_1",
        session_key="session_1",
    )
    await asyncio.sleep(0)

    assert (
        observed_post["url"]
        == "http://127.0.0.1:8766/v1/credential-collection/sessions"
    )
    expected_form_url = f"{expected_public_base_url}/credential-collection/session_123"
    assert expected_form_url in response.text
    assert response.transient_action is not None
    assert response.transient_action.label == expected_label
    assert response.transient_action.url == expected_form_url
    # The card knows its form, so the form's closing line closes exactly this card.
    assert response.transient_action.request_id == request_content.credential_request_id
    assert "Use the secure form below" in response.transient_action.description
    assert expected_description in response.transient_action.description
    assert response.transient_action.url not in response.transient_action.description
    assert response.transient_action.expires_at_utc == "2099-08-29T04:06:47+00:00"
    assert response.transcript_text == (
        "Secure credential entry is ready in the trusted credential workflow."
    )
    assert (
        "http://192.168.1.50:8766/credential-collection/session_123"
        not in response.text
    )
    assert (
        "http://127.0.0.1:8766/credential-collection/session_123" not in response.text
    )
    assert (
        poll_kwargs["status_url"]
        == "http://127.0.0.1:8766/v1/credential-collection/sessions/session_123"
    )
    request_record = gateway.state_store.get_request("corr_1")
    assert request_record is not None
    assert request_record.status == gateway_module.REQUEST_STATUS_WAITING_FOR_CREDENTIAL
    session = gateway.state_store.get_session("session_1")
    assert session is not None
    assert session.last_task_id == "task_1"
    conversation = gateway.state_store.get_session_conversation("conv_1")
    assert conversation is not None
    assert conversation.last_task_id == "task_1"


@pytest.mark.asyncio
async def test_gateway_rejects_malformed_credential_session_before_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _build_test_config(tmp_path)
    config.security_layer.mode = "broker"
    config.security_layer.broker_internal_base_url = "http://127.0.0.1:8766"
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    state_store = GatewayStateStore(str(tmp_path / "state.db"))
    gateway = UserGateway(config, output_sink=None, state_store=state_store)
    state_store.create_session(
        session_key="session_invalid_collection",
        conversation_id="conversation_invalid_collection",
        manager_agent_id=config.manager.manager_agent_id,
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    state_store.record_request(
        correlation_id="correlation_invalid_collection",
        session_key="session_invalid_collection",
        request_kind="interactive",
        request_text="store a credential",
        conversation_id="conversation_invalid_collection",
        followup_to_task_id=None,
        status="pending",
    )
    request_content = SecureCredentialCollectionRequest(
        service_name="Example",
        credential_key="example_login",
        purpose="Store a test login.",
        parent_task_id="task_invalid_collection",
    )
    monkeypatch.setattr(
        gateway._secure_broker,
        "create_collection_session",
        AsyncMock(
            return_value={
                "session_id": "broker_session_invalid",
                "form_url": (
                    "http://127.0.0.1:8766/credential-collection/broker_session_invalid"
                ),
                "status_url": (
                    "http://127.0.0.1:8766/v1/credential-collection/sessions/"
                    "broker_session_invalid"
                ),
            }
        ),
    )
    publish_result = AsyncMock()
    monkeypatch.setattr(gateway._credential_workflow, "publish_result", publish_result)

    response = await gateway._credential_workflow.start_collection(
        request_content=request_content,
        request_envelope=_response_envelope(
            correlation_id="correlation_invalid_collection",
            conversation_id="conversation_invalid_collection",
            task_id="task_invalid_collection",
            answer="",
        ),
        correlation_id="correlation_invalid_collection",
        conversation_id="conversation_invalid_collection",
        session_key="session_invalid_collection",
    )

    assert response.text == "Secure credential collection session could not be created."
    assert response.transcript_text == response.text
    assert response.transient_action is None
    assert gateway._credential_workflow.pending_poll_count == 0
    assert state_store.credential_collection_repository.list_credential_collections() == []
    request_record = state_store.get_request("correlation_invalid_collection")
    assert request_record is not None
    assert request_record.status == "pending"
    publish_result.assert_awaited_once()
    failure_payload = publish_result.await_args.kwargs["session_payload"]
    assert failure_payload["status"] == "credential_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "form_url",
    [
        "http://:8766/credential-collection/broker_session_invalid",
        "http://broker.example.test:invalid/credential-collection/session",
        "https://broker.example.test/credential-collection/session\nignored",
    ],
)
async def test_gateway_rejects_unusable_public_credential_form_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    form_url: str,
) -> None:
    config = _build_test_config(tmp_path)
    config.security_layer.mode = "broker"
    config.security_layer.broker_internal_base_url = "http://127.0.0.1:8766"
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    state_store = GatewayStateStore(str(tmp_path / "state.db"))
    gateway = UserGateway(config, output_sink=None, state_store=state_store)
    state_store.create_session(
        session_key="session_invalid_public_form",
        conversation_id="conversation_invalid_public_form",
        manager_agent_id=config.manager.manager_agent_id,
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    state_store.record_request(
        correlation_id="correlation_invalid_public_form",
        session_key="session_invalid_public_form",
        request_kind="interactive",
        request_text="store a credential",
        conversation_id="conversation_invalid_public_form",
        followup_to_task_id=None,
        status="pending",
    )
    request_content = SecureCredentialCollectionRequest(
        service_name="Example",
        credential_key="example_login",
        purpose="Store a test login.",
        parent_task_id="task_invalid_public_form",
    )
    monkeypatch.setattr(
        gateway._secure_broker,
        "create_collection_session",
        AsyncMock(
            return_value={
                "session_id": "broker_session_invalid_public_form",
                "form_url": form_url,
                "status_url": (
                    "http://127.0.0.1:8766/v1/credential-collection/sessions/"
                    "broker_session_invalid_public_form"
                ),
                "expires_at": "2099-08-29T04:06:47+00:00",
            }
        ),
    )
    monkeypatch.setattr(gateway._secure_broker, "user_form_url", lambda value: value)
    publish_result = AsyncMock()
    monkeypatch.setattr(gateway._credential_workflow, "publish_result", publish_result)

    response = await gateway._credential_workflow.start_collection(
        request_content=request_content,
        request_envelope=_response_envelope(
            correlation_id="correlation_invalid_public_form",
            conversation_id="conversation_invalid_public_form",
            task_id="task_invalid_public_form",
            answer="",
        ),
        correlation_id="correlation_invalid_public_form",
        conversation_id="conversation_invalid_public_form",
        session_key="session_invalid_public_form",
    )

    assert response.transient_action is None
    assert gateway._credential_workflow.pending_poll_count == 0
    assert (
        state_store.get_request("correlation_invalid_public_form").status == "pending"
    )
    publish_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_credential_deletion_poll_treats_deleted_as_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1234",
    )
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    sleep_calls = 0
    published: dict[str, object] = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return {
                "session_id": "session_123",
                "status": "credential_deleted",
                "result": {
                    "status": "success",
                    "content": {
                        "status": "credential_deleted",
                        "credential_key": "test_credential",
                        "message": "Credential test_credential was deleted.",
                    },
                },
            }

    class FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url, *, headers=None, timeout=None):
            return FakeResponse()

    async def fake_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise AssertionError("gateway poller did not stop after credential_deleted")

    async def fake_publish_secure_credential_collection_result(**kwargs):
        published.update(kwargs)

    monkeypatch.setattr(
        gateway._secure_broker,
        "get_collection_status",
        AsyncMock(return_value=await FakeResponse().json()),
    )
    monkeypatch.setattr(
        broker_session_watch_module.asyncio,
        "sleep",
        fake_sleep,
    )
    monkeypatch.setattr(
        gateway._credential_workflow,
        "publish_result",
        fake_publish_secure_credential_collection_result,
    )
    request_content = SecureCredentialCollectionRequest(
        service_name="Sealbox",
        credential_key="test_credential",
        purpose="Delete stored test credential.",
        flow="credential_deletion",
        source="sealbox",
        source_item_id="team-nats-agents:default:test_credential",
        parent_task_id="task_1",
    )

    await gateway._credential_workflow.poll_session(
        status_url="http://127.0.0.1:18600/v1/credential-collection/sessions/session_123",
        session_id="session_123",
        request_content=request_content,
        correlation_id="corr_1",
        conversation_id="conv_1",
        session_key="session_1",
    )

    assert sleep_calls == 1
    assert published["session_payload"]["status"] == "credential_deleted"


@pytest.mark.asyncio
async def test_gateway_credential_collection_poll_treats_stored_as_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1234",
    )
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    sleep_calls = 0
    published: dict[str, object] = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def json(self):
            return {
                "session_id": "session_123",
                "status": "credential_stored",
                "result": {
                    "status": "success",
                    "content": {
                        "status": "credential_stored",
                        "credential_key": "some_test_token",
                        "message": "Credential `some_test_token` was stored securely.",
                    },
                },
            }

    class FakeClientSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def get(self, url, *, headers=None, timeout=None):
            return FakeResponse()

    async def fake_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise AssertionError("gateway poller did not stop after credential_stored")

    async def fake_publish_secure_credential_collection_result(**kwargs):
        published.update(kwargs)

    monkeypatch.setattr(
        gateway._secure_broker,
        "get_collection_status",
        AsyncMock(return_value=await FakeResponse().json()),
    )
    monkeypatch.setattr(
        broker_session_watch_module.asyncio,
        "sleep",
        fake_sleep,
    )
    monkeypatch.setattr(
        gateway._credential_workflow,
        "publish_result",
        fake_publish_secure_credential_collection_result,
    )
    request_content = SecureCredentialCollectionRequest(
        service_name="GitHub",
        credential_key="some_test_token",
        purpose="Store a test API token.",
        parent_task_id="task_1",
    )

    await gateway._credential_workflow.poll_session(
        status_url="http://127.0.0.1:18600/v1/credential-collection/sessions/session_123",
        session_id="session_123",
        request_content=request_content,
        correlation_id="corr_1",
        conversation_id="conv_1",
        session_key="session_1",
    )

    assert sleep_calls == 1
    assert published["session_payload"]["status"] == "credential_stored"


async def _watch_credential_form(
    gateway: UserGateway,
    monkeypatch: pytest.MonkeyPatch,
    request_content: SecureCredentialCollectionRequest,
    status: str,
) -> None:
    """The gateway's watcher for an opened form, with the broker reporting ``status`` at once."""

    async def no_wait(_seconds) -> None:
        return None

    monkeypatch.setattr(
        gateway._secure_broker,
        "get_collection_status",
        AsyncMock(
            return_value={
                "session_id": "session_123",
                "status": status,
                "result": {"status": "success", "content": {"status": status, "message": "secret-free broker words"}},
            }
        ),
    )
    monkeypatch.setattr(broker_session_watch_module.asyncio, "sleep", no_wait)
    await gateway._credential_workflow.poll_session(
        status_url="http://127.0.0.1:18600/v1/credential-collection/sessions/session_123",
        session_id="session_123",
        request_content=request_content,
        correlation_id="corr_1",
        conversation_id="conv_1",
        session_key="session_1",
    )


def _credential_form_gateway(tmp_path: Path):
    """An opened form's gateway with real SQLite state and captured chat events."""

    config = _build_test_config(tmp_path)
    events: list = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=events.append,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.state_store.create_session(
        session_key="session_1",
        conversation_id="conv_1",
        manager_agent_id=config.manager.manager_agent_id,
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    request = SecureCredentialCollectionRequest(
        service_name="Bifrost", credential_key="bifrost_virtual_key", purpose="Store it.", parent_task_id="task_1"
    )
    return gateway, events, request


@pytest.mark.asyncio
async def test_credential_form_closes_when_manager_delivery_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway, events, request = _credential_form_gateway(tmp_path)

    gateway._manager_client.publish_durable = AsyncMock(side_effect=[RuntimeError("Gateway is not connected"), None])
    await _watch_credential_form(gateway, monkeypatch, request, "credential_stored")
    assert gateway._manager_client.publish_durable.await_count == 2

    [closing] = [event for event in events if event.message_type == "secure_credential_result"]
    assert closing.correlation_id == request.credential_request_id
    assert closing.text == "The secure form for `bifrost_virtual_key` is done: the credential was stored."


@pytest.mark.asyncio
async def test_credential_form_closes_before_stalled_manager_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway, events, request = _credential_form_gateway(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stalled_publish(*args, **kwargs):
        entered.set()
        await release.wait()

    gateway._manager_client.publish_durable = stalled_publish
    watch = asyncio.create_task(_watch_credential_form(gateway, monkeypatch, request, "credential_stored"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        [closing] = [event for event in events if event.message_type == "secure_credential_result"]
        assert closing.correlation_id == request.credential_request_id
        assert not watch.done()
    finally:
        release.set()
        await watch
    assert len([event for event in events if event.message_type == "secure_credential_result"]) == 1


@pytest.mark.asyncio
async def test_credential_result_reaches_manager_while_chat_delivery_stalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, _events, request = _credential_form_gateway(tmp_path)
    closing_started = asyncio.Event()
    published = asyncio.Event()
    release = asyncio.Event()

    async def stalled_emit(_event):
        closing_started.set()
        await release.wait()

    async def publish(*args, **kwargs):
        published.set()

    monkeypatch.setattr(gateway._credential_workflow._event_output, "emit", stalled_emit)
    gateway._manager_client.publish_durable = publish
    watch = asyncio.create_task(_watch_credential_form(gateway, monkeypatch, request, "credential_stored"))
    try:
        await asyncio.wait_for(published.wait(), timeout=5)
        assert closing_started.is_set()
        assert not watch.done()
    finally:
        release.set()
        await watch


@pytest.mark.asyncio
async def test_credential_watcher_shutdown_cancels_both_deliveries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, _events, request = _credential_form_gateway(tmp_path)
    chat_started = asyncio.Event()
    manager_started = asyncio.Event()
    chat_stopped = asyncio.Event()
    manager_stopped = asyncio.Event()

    async def stalled_emit(_event):
        chat_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            chat_stopped.set()

    async def stalled_publish(*args, **kwargs):
        manager_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            manager_stopped.set()

    monkeypatch.setattr(gateway._credential_workflow._event_output, "emit", stalled_emit)
    gateway._manager_client.publish_durable = stalled_publish
    gateway._credential_workflow._watches.start(
        _watch_credential_form(gateway, monkeypatch, request, "credential_stored"), name="credential-shutdown-test"
    )
    try:
        await asyncio.wait_for(asyncio.gather(chat_started.wait(), manager_started.wait()), timeout=5)
    finally:
        await gateway._credential_workflow.close()
    assert chat_stopped.is_set()
    assert manager_stopped.is_set()
    assert gateway._credential_workflow.pending_poll_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("http_status", [404, 410])
async def test_missing_credential_form_has_an_unknown_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, http_status: int
) -> None:
    gateway, events, request = _credential_form_gateway(tmp_path)
    gateway._manager_client.publish_durable = AsyncMock()
    monkeypatch.setattr(
        gateway._secure_broker,
        "get_collection_status",
        AsyncMock(side_effect=GatewaySecureBrokerRefusal("Form session is no longer available.", status=http_status)),
    )

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(broker_session_watch_module.asyncio, "sleep", no_wait)
    await gateway._credential_workflow.poll_session(
        status_url="http://127.0.0.1:18600/v1/credential-collection/sessions/session_123",
        session_id="session_123", request_content=request, correlation_id="corr_1",
        conversation_id="conv_1", session_key="session_1",
    )

    [closing] = [event for event in events if event.message_type == "secure_credential_result"]
    assert closing.text == (
        "The secure form for `bifrost_virtual_key` could not be checked. "
        "The gateway cannot confirm whether anything was stored or changed."
    )
    result = gateway._manager_client.publish_durable.await_args.args[0].msg_content
    assert result.status == "credential_unavailable"
    assert result.error == "Credential collection result could not be confirmed."
    assert result.message == "The gateway cannot confirm whether anything was stored or changed."


@pytest.mark.asyncio
@pytest.mark.parametrize("read_fails", [False, True])
async def test_credential_form_deadline_does_not_claim_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_fails: bool
) -> None:
    gateway, events, request = _credential_form_gateway(tmp_path)
    gateway._manager_client.publish_durable = AsyncMock()
    clock = _FixedGatewayClock(datetime.now(timezone.utc))
    monkeypatch.setattr(gateway._credential_workflow, "_clock", clock)
    monkeypatch.setattr(gateway._secure_broker, "_collection_ttl_seconds", 60)
    read_status = AsyncMock(side_effect=RuntimeError("Broker is unreachable")) if read_fails else AsyncMock(
        return_value={"status": "pending", "submission_in_progress": True}
    )
    monkeypatch.setattr(gateway._secure_broker, "get_collection_status", read_status)

    async def advance_clock(_seconds):
        clock.value += timedelta(seconds=60)

    monkeypatch.setattr(broker_session_watch_module.asyncio, "sleep", advance_clock)
    await gateway._credential_workflow.poll_session(
        status_url="http://127.0.0.1:18600/v1/credential-collection/sessions/session_123",
        session_id="session_123", request_content=request, correlation_id="corr_1",
        conversation_id="conv_1", session_key="session_1",
    )

    [closing] = [event for event in events if event.message_type == "secure_credential_result"]
    assert "could not be checked" in closing.text
    assert "cannot confirm whether anything was stored or changed" in closing.text
    assert "without storing" not in closing.text


@pytest.mark.asyncio
async def test_a_credential_form_that_never_opened_posts_no_closing_line(tmp_path: Path) -> None:
    """The broker refused to open the form: the task hears, but there was no card to close."""

    config = _build_test_config(tmp_path)
    events: list = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=events.append,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.state_store.create_session(
        session_key="session_1",
        conversation_id="conv_1",
        manager_agent_id=config.manager.manager_agent_id,
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway._manager_client.publish_durable = AsyncMock()

    await gateway._credential_workflow.fail_unmatched_collection_request(
        request_content=SecureCredentialCollectionRequest(
            service_name="Bifrost", credential_key="bifrost_virtual_key", purpose="Store it.", parent_task_id="task_1"
        ),
        request_envelope=_response_envelope(correlation_id="corr_1", conversation_id="conv_1", task_id="task_1", answer=""),
        correlation_id="corr_1",
        conversation_id="conv_1",
        session_key="session_1",
    )

    gateway._manager_client.publish_durable.assert_awaited_once()
    assert [event for event in events if event.message_type == "secure_credential_result"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow", "status", "text"),
    [
        ("credential_collection", "credential_stored", "The secure form for `bifrost_virtual_key` is done: the credential was stored."),
        ("credential_deletion", "credential_deleted", "The secure form for `bifrost_virtual_key` is done: the credential was deleted."),
        ("manual_signup", "manual_signup_collected", "The secure form for `bifrost_virtual_key` is done: the sign-up details were stored."),
        ("approved_signup", "signup_completed", "The secure form for `bifrost_virtual_key` is done: the sign-up finished and the credential was stored."),
        (
            "manual_signup", "signup_failed",
            "The secure form for `bifrost_virtual_key` could not finish. "
            "The gateway cannot confirm whether anything was stored or changed.",
        ),
        (
            "credential_collection",
            "credential_unavailable",
            "The secure form for `bifrost_virtual_key` could not finish. "
            "The gateway cannot confirm whether anything was stored or changed.",
        ),
    ],
)
async def test_a_finished_credential_form_posts_one_line_that_closes_its_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flow: str, status: str, text: str
) -> None:
    """The chat learns the form ended when the form ends, not when the agent answers.

    Before, the gateway told only the agent; the card's countdown kept running
    until the agent's final answer, however long the agent worked on.
    """

    config = _build_test_config(tmp_path)
    events: list = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=events.append,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.state_store.create_session(
        session_key="session_1",
        conversation_id="conv_1",
        manager_agent_id=config.manager.manager_agent_id,
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway._manager_client.publish_durable = AsyncMock()
    request_content = SecureCredentialCollectionRequest(
        service_name="Bifrost",
        credential_key="bifrost_virtual_key",
        purpose="Store the Bifrost virtual key.",
        flow=flow,
        parent_task_id="task_1",
    )

    await _watch_credential_form(gateway, monkeypatch, request_content, status)

    gateway._manager_client.publish_durable.assert_awaited_once()
    [closing] = [event for event in events if event.message_type == "secure_credential_result"]
    assert closing.correlation_id == request_content.credential_request_id
    assert (closing.session_key, closing.conversation_id, closing.text) == ("session_1", "conv_1", text)


@pytest.mark.asyncio
async def test_a_credential_form_line_that_cannot_be_posted_is_logged_and_the_agent_still_hears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = _build_test_config(tmp_path)

    def broken_sink(_event) -> None:
        raise RuntimeError("chat output is down")

    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=broken_sink,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.state_store.create_session(
        session_key="session_1",
        conversation_id="conv_1",
        manager_agent_id=config.manager.manager_agent_id,
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway._manager_client.publish_durable = AsyncMock()

    request_content = SecureCredentialCollectionRequest(
        service_name="Bifrost", credential_key="bifrost_virtual_key", purpose="Store it.", parent_task_id="task_1"
    )
    with caplog.at_level(logging.ERROR):
        await _watch_credential_form(gateway, monkeypatch, request_content, "credential_stored")

    gateway._manager_client.publish_durable.assert_awaited_once()
    assert "event=gateway_credential_form_closing_failed" in caplog.text


@pytest.mark.asyncio
async def test_gateway_secure_credential_result_preserves_request_flow_metadata(
    tmp_path: Path,
) -> None:
    config = _build_test_config(tmp_path)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.state_store.create_session(
        session_key="session_1",
        conversation_id="conv_1",
        manager_agent_id=config.manager.manager_agent_id,
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway._manager_client.publish_durable = AsyncMock()
    request_content = SecureCredentialCollectionRequest(
        service_name="Sealbox",
        credential_key="test_credential",
        purpose="Delete stored test credential.",
        flow="credential_deletion",
        source="sealbox",
        source_item_id="team-nats-agents:default:test_credential",
        parent_task_id="task_1",
    )

    await gateway._credential_workflow.publish_result(
        request_content=request_content,
        session_payload={
            "session_id": "session_123",
            "status": "credential_unavailable",
            "result": {
                "status": "error",
                "error": "sealbox-cli credential delete exited with code 2",
                "content": {"status": "credential_unavailable"},
            },
        },
        correlation_id="corr_1",
        conversation_id="conv_1",
        session_key="session_1",
    )

    gateway._manager_client.publish_durable.assert_awaited_once()
    published = gateway._manager_client.publish_durable.await_args.args[0]
    assert isinstance(published.msg_content, SecureCredentialCollectionResult)
    assert published.msg_content.status == "credential_unavailable"
    assert published.msg_content.service_name == "Sealbox"
    assert published.msg_content.display_name is None
    assert published.msg_content.message == ""
    assert published.msg_content.metadata["flow"] == "credential_deletion"
    assert published.msg_content.metadata["source"] == "sealbox"
    assert (
        published.msg_content.metadata["source_item_id"]
        == "team-nats-agents:default:test_credential"
    )


@pytest.mark.asyncio
async def test_gateway_credential_result_preserves_friendly_service_context(
    tmp_path: Path,
) -> None:
    config = _build_test_config(tmp_path)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.state_store.create_session(
        session_key="session_ally_bank",
        conversation_id="conv_ally_bank",
        manager_agent_id=config.manager.manager_agent_id,
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway._manager_client.publish_durable = AsyncMock()
    request_content = SecureCredentialCollectionRequest(
        service_name="Ally Bank",
        display_name="Ally Bank login",
        credential_key="ally_bank_login",
        purpose="Store the user's Ally Bank login.",
        parent_task_id="task_ally_bank",
    )

    await gateway._credential_workflow.publish_result(
        request_content=request_content,
        session_payload={
            "session_id": "session_ally_bank_broker",
            "status": "credential_stored",
            "result": {
                "status": "success",
                "content": {
                    "status": "credential_stored",
                    "credential_key": "ally_bank_login",
                },
            },
        },
        correlation_id="corr_ally_bank",
        conversation_id="conv_ally_bank",
        session_key="session_ally_bank",
    )

    published = gateway._manager_client.publish_durable.await_args.args[0]
    assert isinstance(published.msg_content, SecureCredentialCollectionResult)
    assert published.msg_content.service_name == "Ally Bank"
    assert published.msg_content.display_name == "Ally Bank login"
    assert published.msg_content.message == ""


def test_delivery_policy_quiet_hours_and_digest():
    settings = DeliverySettings()
    now = datetime(2026, 3, 20, 7, 0, tzinfo=timezone.utc)

    assert is_within_quiet_hours(
        timezone_name="America/Chicago",
        delivery_settings=settings,
        now_utc=now,
    )

    decision = evaluate_delivery(
        origin_kind="cron",
        rendered_text="Daily report ready",
        timezone_name="America/Chicago",
        delivery_settings=settings,
        now_utc=now,
    )
    assert decision.action == "defer_to_digest"
    assert decision.available_after is not None

    urgent = evaluate_delivery(
        origin_kind="cron",
        rendered_text="Critical failure in pipeline",
        timezone_name="America/Chicago",
        delivery_settings=settings,
        now_utc=now,
    )
    assert urgent.action == "deliver_now"
    assert summarize_delivery_batch(["a", "b"]).startswith("Deferred summary")


def test_heartbeat_source_helpers_parse_markdown(tmp_path: Path):
    heartbeat_path = tmp_path / "HEARTBEAT.md"
    heartbeat_path.write_text(
        "# Heartbeat Tasks\n\n## Active Tasks\n\n- Check inbox\n- Review reminders\n\n## Completed\n",
        encoding="utf-8",
    )

    tasks = extract_active_heartbeat_tasks(heartbeat_path.read_text(encoding="utf-8"))
    assert tasks == ["Check inbox", "Review reminders"]


@pytest.mark.asyncio
async def test_gateway_installs_schedule_after_manager_owned_heartbeat_seed(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.state.storage.sqlite.path = str(tmp_path / "task_history.db")
    config.heartbeat.source_id = "hb_source_test"
    config.heartbeat.schedule_id = "hb_sched_test"
    bootstrap_instance(config)
    heartbeat_path = config.workspace_path / "HEARTBEAT.md"
    heartbeat_path.write_text(
        "# Heartbeat Tasks\n\n## Active Tasks\n\n- Reply with the exact phrase heartbeat seeded ok.\n\n## Completed\n",
        encoding="utf-8",
    )
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope, routing_key: str) -> None:
        published.append(envelope)

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]

    heartbeat_store = HeartbeatStore(storage_settings=config.manager_storage_settings)
    heartbeat_store.ensure_source(source_id="hb_source_test", title="Heartbeat")
    migrate_legacy_heartbeat_markdown(
        workspace_dir=config.workspace_path,
        instance_root=config.instance_root_path,
        heartbeat_store=heartbeat_store,
        source_id="hb_source_test",
    )
    await gateway._maintenance_schedules.ensure_heartbeat()

    source = heartbeat_store.get_source("hb_source_test")
    assert source is not None
    items = heartbeat_store.list_items("hb_source_test")
    assert len(items) == 1
    assert "heartbeat seeded ok" in items[0].instruction
    assert items[0].conversation_id.startswith("conversation_id_")
    assert items[0].source_key is not None
    assert not heartbeat_path.exists()
    assert list(
        (config.instance_root_path / "migrations" / "workspace_legacy_markdown").glob(
            "HEARTBEAT.*.md"
        )
    )
    assert [message.msg_content.install_mode for message in published] == ["ensure", "refresh"]
    assert isinstance(published[0].msg_content, ScheduledMessage)
    assert published[0].msg_content.schedule_id == "hb_sched_test"
    payload_meta = published[0].msg_content.payload_meta
    assert payload_meta is not None
    assert payload_meta.gateway_context is not None
    assert payload_meta.gateway_context.heartbeat_source_id == "hb_source_test"
    scheduled_target = gateway.state_store.get_scheduled_target("hb_sched_test")
    assert scheduled_target is not None
    assert scheduled_target.session_key == config.heartbeat.session_key
    assert scheduled_target.owner_canonical_user_id == config.user.canonical_user_id


def test_manager_migrates_legacy_heartbeat_even_when_launcher_is_disabled(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.state.storage.sqlite.path = str(tmp_path / "task_history.db")
    config.heartbeat.enabled = False
    config.heartbeat.source_id = "hb_source_disabled"
    bootstrap_instance(config)
    heartbeat_path = config.workspace_path / "HEARTBEAT.md"
    heartbeat_path.write_text(
        "# Heartbeat Tasks\n\n## Active Tasks\n\n- Preserve this disabled heartbeat.\n",
        encoding="utf-8",
    )

    heartbeat_store = HeartbeatStore(storage_settings=config.manager_storage_settings)
    heartbeat_store.ensure_source(
        source_id="hb_source_disabled",
        title="Heartbeat",
        enabled=False,
    )
    migrate_legacy_heartbeat_markdown(
        workspace_dir=config.workspace_path,
        instance_root=config.instance_root_path,
        heartbeat_store=heartbeat_store,
        source_id="hb_source_disabled",
    )
    items = heartbeat_store.list_items("hb_source_disabled")
    assert [item.instruction for item in items] == ["Preserve this disabled heartbeat."]
    assert not heartbeat_path.exists()
    assert list(
        (config.instance_root_path / "migrations" / "workspace_legacy_markdown").glob(
            "HEARTBEAT.*.md"
        )
    )


@pytest.mark.asyncio
async def test_gateway_heartbeat_reuses_legacy_non_cli_delivery_session_after_owner_migration(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.state.storage.sqlite.path = str(tmp_path / "task_history.db")
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "chat_123"
    config.heartbeat.source_id = "hb_source_test"
    config.heartbeat.schedule_id = "hb_sched_test"
    bootstrap_instance(config)

    state_store = GatewayStateStore(str(tmp_path / "state.db"))
    state_store.create_session(
        session_key=config.heartbeat.session_key,
        conversation_id="legacy_heartbeat_conversation",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="telegram",
        user_id=config.user.canonical_user_id,
        chat_id="chat_123",
    )
    gateway = UserGateway(config, output_sink=None, state_store=state_store)
    migrated_session = state_store.get_session(config.heartbeat.session_key)
    assert migrated_session is not None
    assert migrated_session.owner_canonical_user_id == config.user.canonical_user_id

    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope, routing_key: str) -> None:
        published.append(envelope)

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]

    await gateway._maintenance_schedules.ensure_heartbeat()

    assert [message.msg_content.install_mode for message in published] == ["ensure", "refresh"]
    schedule = published[0].msg_content
    assert isinstance(schedule, ScheduledMessage)
    assert schedule.payload_meta is not None
    assert schedule.payload_meta.gateway_context is not None
    assert (
        schedule.payload_meta.gateway_context.canonical_user_id
        == config.user.canonical_user_id
    )
    assert published[0].msg_meta.gateway_context is not None
    assert (
        published[0].msg_meta.gateway_context.canonical_user_id
        == config.user.canonical_user_id
    )
    scheduled_target = gateway.state_store.get_scheduled_target("hb_sched_test")
    assert scheduled_target is not None
    assert scheduled_target.session_key == config.heartbeat.session_key
    assert scheduled_target.conversation_id == "legacy_heartbeat_conversation"
    assert scheduled_target.owner_canonical_user_id == config.user.canonical_user_id


@pytest.mark.asyncio
async def test_gateway_installs_local_memreview_schedules(tmp_path: Path):
    config = _build_test_config(tmp_path)
    config.state.storage.sqlite.path = str(tmp_path / "task_history.db")
    config.agent.memory["review"]["coremem_audit_interval_seconds"] = 123
    config.agent.memory["review"]["usermem_audit_interval_seconds"] = 456
    config.agent.memory["review"]["stale_conversation_review_interval_seconds"] = 789
    # Only memory review's schedules here (knowledge curation's: tests/test_knowledge_curation_schedules.py).
    config.agent.knowledge.curation.enabled = False
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope, routing_key: str) -> None:
        published.append(envelope)

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]

    await gateway._maintenance_schedules.ensure_specialist_schedules()

    # Each schedule is installed once (ensure keeps an operator's pause and
    # cadence) and then refreshed (only its person, roles and session change):
    # the team's coremem audit, then the first user's own usermem and
    # stale-conversation pair (the only active person).
    person = config.user.canonical_user_id
    assert [item.msg_content.install_mode for item in published] == ["ensure", "refresh"] * 3
    schedules = {item.msg_content.schedule_id: item.msg_content for item in published}
    usermem_id = person_system_schedule_id("mem_rev_user", "test_instance", person)
    stale_id = person_system_schedule_id("mem_rev_stale", "test_instance", person)
    assert set(schedules) == {"mem_rev_core_test_instance", usermem_id, stale_id}
    assert (
        schedules["mem_rev_core_test_instance"].schedule_name
        == "memory_review:coremem:test_instance"
    )
    assert schedules["mem_rev_core_test_instance"].interval_ms == 123000
    assert schedules[usermem_id].schedule_name == f"memory_review:usermem:test_instance:{person}"
    assert schedules[usermem_id].interval_ms == 456000
    assert (
        schedules[stale_id].schedule_name
        == f"memory_review:stale_conversations:test_instance:{person}"
    )
    assert schedules[stale_id].interval_ms == 789000
    for schedule_id in schedules:
        assert schedules[schedule_id].claim_named_agent_type == "MemReviewAgent"
        assert schedules[schedule_id].payload_meta.to_named_agent_type == "MemReviewAgent"
        assert schedules[schedule_id].payload_meta.gateway_context.canonical_user_id == person
        target = gateway.state_store.get_scheduled_target(schedule_id)
        assert target is not None
        assert target.conversation_id
    # The person's pair uses their own maintenance session, not the team's proactive one.
    assert gateway.state_store.get_scheduled_target(usermem_id).session_key == f"person_maintenance:{person}"
    assert gateway.state_store.get_scheduled_target("mem_rev_core_test_instance").session_key == config.heartbeat.session_key


@pytest.mark.asyncio
async def test_gateway_add_cron_job_preallocates_dedicated_schedule_conversation(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._manager_client.publish_durable = AsyncMock()
    gateway.state_store.create_session(
        session_key="session_cron",
        conversation_id="conversation_source",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="local",
        user_id="user_1",
        chat_id="chat_1",
        owner_canonical_user_id=config.user.canonical_user_id,
    )

    schedule_id = await gateway.application.add_cron_job(
        session_key="session_cron",
        name="match updates",
        instruction="Report important match events.",
        interval_seconds=60,
    )

    gateway._manager_client.publish_durable.assert_awaited_once()
    envelope = gateway._manager_client.publish_durable.await_args.args[0]
    assert isinstance(envelope.msg_content, ScheduledMessage)
    schedule = envelope.msg_content
    assert schedule.schedule_id == schedule_id
    assert envelope.msg_meta.conversation_id == "conversation_source"
    assert schedule.payload_meta is not None
    assert schedule.payload_meta.conversation_id
    assert schedule.payload_meta.conversation_id != "conversation_source"
    request = gateway.state_store.get_request(schedule.payload_meta.msg_correlation_id)
    assert request is not None
    assert request.conversation_id == schedule.payload_meta.conversation_id
    target = gateway.state_store.get_scheduled_target(schedule_id)
    assert target is not None
    assert target.session_key == "session_cron"
    assert target.conversation_id == schedule.payload_meta.conversation_id


@pytest.mark.asyncio
async def test_gateway_send_user_message_threads_followups(tmp_path: Path):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope, routing_key: str) -> None:
        published.append(envelope)
        correlation_id = envelope.msg_meta.msg_correlation_id
        response = _response_envelope(
            correlation_id=correlation_id,
            conversation_id="conv_phase1",
            task_id=envelope.msg_content.task_id,
            answer=f"answer_{len(published)}",
        )
        await gateway._manager_client.handle_response(response)

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]

    first = await gateway.application.send_user_message("hello")
    second = await gateway.application.send_user_message("follow up")

    assert first.conversation_id == "conv_phase1"
    assert second.conversation_id == "conv_phase1"
    assert published[0].msg_content.problem_statement == "hello"
    assert published[1].msg_content.problem_statement == "follow up"
    assert published[0].msg_meta.conversation_id is not None
    assert published[1].msg_meta.conversation_id == "conv_phase1"
    assert published[1].msg_meta.followup_to_task_id == published[0].msg_content.task_id
    assert published[0].msg_meta.gateway_context is not None
    assert (
        published[0].msg_meta.gateway_context.canonical_user_id
        == config.user.canonical_user_id
    )


@pytest.mark.asyncio
async def test_gateway_app_user_does_not_get_configured_canonical_user_without_binding(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope, routing_key: str) -> None:
        published.append(envelope)
        correlation_id = envelope.msg_meta.msg_correlation_id
        await gateway._manager_client.handle_response(
            _response_envelope(
                correlation_id=correlation_id,
                conversation_id="conv_app",
                task_id=envelope.msg_content.task_id,
                answer="ok",
            )
        )

    gateway._publish_envelope = fake_publish  # type: ignore[method-assign]

    response = await gateway.application.send_user_message(
        "hello from telegram",
        channel_name="telegram",
        user_id="tg_123",
        chat_id="chat_123",
    )

    assert published == []
    assert "not linked" in response.text


@pytest.mark.asyncio
async def test_newuser_command_binds_channel_and_gateway_uses_canonical_user(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_123",
    )

    handled, _, message = await handle_interactive_session_command(
        raw_text='/user tg_123 "Alice Example"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_123",
    )
    binding = gateway.state_store.get_active_channel_binding(
        channel_name="telegram",
        external_user_id="tg_123",
    )
    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope, routing_key: str) -> None:
        published.append(envelope)
        correlation_id = envelope.msg_meta.msg_correlation_id
        await gateway._manager_client.handle_response(
            _response_envelope(
                correlation_id=correlation_id,
                conversation_id="conv_app",
                task_id=envelope.msg_content.task_id,
                answer="ok",
            )
        )

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]
    await gateway.application.send_user_message(
        "hello from registered telegram",
        channel_name="telegram",
        user_id="tg_123",
        chat_id="chat_123",
    )

    assert handled is True
    assert message is not None
    assert "Registered user" in message
    assert binding is not None
    assert binding.canonical_user_id.startswith("usr_")
    assert published[0].msg_meta.gateway_context is not None
    assert (
        published[0].msg_meta.gateway_context.canonical_user_id
        == binding.canonical_user_id
    )


@pytest.mark.asyncio
async def test_web_only_invited_user_is_named_from_canonical_profile(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.user.display_name = "Riley"
    config.save()
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    handled, _, invite_message = await handle_interactive_session_command(
        raw_text=('/user invite --new "Alice" --label "Browser Onboarding"'),
        gateway=gateway,
        current_session_key=None,
        channel_name="website",
        user_id=config.user.canonical_user_id,
        chat_id="web-chat",
        canonical_user_id=config.user.canonical_user_id,
        enable_channel_binding_commands=False,
    )
    alice = next(
        user
        for user in gateway.state_store.list_gateway_users(limit=100)
        if user.display_name == "Alice"
    )
    published: list[MessageEnvelope] = []

    async def fake_publish(
        envelope: MessageEnvelope,
        routing_key: str,
    ) -> None:
        published.append(envelope)
        correlation_id = envelope.msg_meta.msg_correlation_id
        await gateway._manager_client.handle_response(
            _response_envelope(
                correlation_id=correlation_id,
                conversation_id="conv_alice_web",
                task_id=envelope.msg_content.task_id,
                answer="Hello Alice",
            )
        )

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]
    await gateway.application.send_user_message(
        "Hello",
        session_key="website:web-chat:alice",
        channel_name="website",
        user_id=alice.canonical_user_id,
        chat_id="web-chat",
        canonical_user_id=alice.canonical_user_id,
    )

    assert handled is True
    assert invite_message is not None
    assert "Registered web-only user." in invite_message
    assert len(published) == 1
    assert published[0].msg_meta.from_agent_name == "Alice"
    assert published[0].msg_meta.from_agent_name != config.user.display_name
    assert published[0].msg_meta.gateway_context is not None
    assert (
        published[0].msg_meta.gateway_context.canonical_user_id
        == alice.canonical_user_id
    )
    alice_session = gateway.state_store.get_session("website:web-chat:alice")
    assert alice_session is not None
    assert alice_session.owner_canonical_user_id == alice.canonical_user_id


@pytest.mark.asyncio
async def test_newuser_rejects_mismatched_signed_and_channel_identities(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    alice = gateway.state_store.create_gateway_user(
        display_name="Alice",
        timezone="UTC",
    )
    gateway.state_store.create_team_membership(
        canonical_user_id=alice.canonical_user_id,
        team_id=config.gateway.team_id,
        role="member",
    )

    _, _, message = await handle_interactive_session_command(
        raw_text='/user invite --new "Bob"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
        canonical_user_id=alice.canonical_user_id,
    )

    assert message is not None
    assert "does not match the channel identity" in message
    assert all(
        user.display_name != "Bob"
        for user in gateway.state_store.list_gateway_users(limit=100)
    )


@pytest.mark.asyncio
async def test_task_query_reply_uses_session_owner_display_name(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.user.display_name = "Riley"
    config.save()
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    alice = gateway.state_store.create_gateway_user(
        display_name="Alice",
        timezone="America/Chicago",
    )
    gateway.state_store.create_team_membership(
        canonical_user_id=alice.canonical_user_id,
        team_id=config.gateway.team_id,
        role="member",
    )
    session = gateway.state_store.create_session(
        session_key="website:web-chat:alice-query",
        conversation_id="conv_alice_query",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="website",
        user_id=alice.canonical_user_id,
        chat_id="web-chat",
        owner_canonical_user_id=alice.canonical_user_id,
    )
    published: list[MessageEnvelope] = []

    async def fake_publish(
        envelope: MessageEnvelope,
        routing_key: str,
    ) -> None:
        published.append(envelope)

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]
    await gateway._clarification_core.reply_to_task_query(
        TaskQuery(
            task_query_id="query_alice",
            query_type=MessageMetaType.REQUEST,
            message="Which option?",
            parent_task_id="task_alice",
        ),
        correlation_id="corr_alice_query",
        response_text="Option B",
        session_key=session.session_key,
        conversation_id=session.conversation_id,
    )

    assert len(published) == 1
    assert published[0].msg_meta.from_agent_name == "Alice"
    assert published[0].msg_meta.request_timezone == "America/Chicago"
    assert published[0].msg_meta.gateway_context is not None
    assert (
        published[0].msg_meta.gateway_context.canonical_user_id
        == alice.canonical_user_id
    )


@pytest.mark.asyncio
async def test_newuser_create_requires_admin_role(tmp_path: Path):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    _, _, created_member = await handle_interactive_session_command(
        raw_text='/user tg_member "Member"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    _, _, blocked = await handle_interactive_session_command(
        raw_text='/user tg_other "Other"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_member",
        chat_id="chat_admin",
    )
    other_binding = gateway.state_store.get_active_channel_binding(
        channel_name="telegram",
        external_user_id="tg_other",
    )

    assert created_member is not None and "Registered user" in created_member
    assert blocked == "Only an admin can register new users."
    assert other_binding is None


@pytest.mark.asyncio
async def test_newuser_help_and_status_explain_channel_identity(tmp_path: Path):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    _, _, help_text = await handle_interactive_session_command(
        raw_text="/user help",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="123456789",
        chat_id="chat_admin",
    )
    _, _, status_text = await handle_interactive_session_command(
        raw_text="/user status",
        gateway=gateway,
        current_session_key=None,
        channel_name="signal",
        user_id="sender-uuid",
        chat_id="+15551234567",
    )
    _, _, root_help_text = await handle_interactive_session_command(
        raw_text="/help",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="123456789",
        chat_id="chat_admin",
    )

    assert help_text is not None
    assert "Telegram uses numeric from_user.id" in help_text
    assert "Discord uses numeric user id" in help_text
    assert "WhatsApp uses the normalized E.164 sender id" in help_text
    assert "Signal uses sourceUuid" in help_text
    assert "Photon/iMessage uses sender.id" in help_text
    assert '/user token "Alice laptop"' in help_text
    assert "quoted value is only a label" in help_text
    assert '/user invite --new "Alice"' in help_text
    assert "generates, copies, and confirms a reusable uauser_... token" in help_text
    assert "one-time, short-lived code" in help_text
    assert "not a Web Chat credential" in help_text
    assert "/user list" in help_text
    assert "disable <usr_...>" in help_text
    assert root_help_text is not None
    assert "User onboarding and administration:" in root_help_text
    assert "/user list" in root_help_text

    legacy_handled, _, legacy_message = await handle_interactive_session_command(
        raw_text="/newuser list",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    assert legacy_handled is False
    assert legacy_message is None
    assert "Telegram uses numeric from_user.id" in root_help_text
    assert "Photon/iMessage uses sender.id" in root_help_text
    assert status_text is not None
    assert "Channel: signal" in status_text
    assert "External user id: sender-uuid" in status_text
    assert "No canonical user is registered" in status_text


@pytest.mark.asyncio
async def test_admin_can_invite_existing_and_web_only_users_but_member_cannot(
    tmp_path: Path,
) -> None:
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    await handle_interactive_session_command(
        raw_text='/user tg_member "Member"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    member_binding = gateway.state_store.get_active_channel_binding(
        channel_name="telegram",
        external_user_id="tg_member",
    )
    assert member_binding is not None

    _, _, existing_message = await handle_interactive_session_command(
        raw_text=f'/user invite {member_binding.canonical_user_id} --label "Member laptop"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    _, _, web_only_message = await handle_interactive_session_command(
        raw_text='/user invite --new "Browser Person" --label "Initial browser"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    _, _, denied_message = await handle_interactive_session_command(
        raw_text=f"/user invite {member_binding.canonical_user_id}",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_member",
        chat_id="chat_admin",
    )
    _, _, denied_probe = await handle_interactive_session_command(
        raw_text="/user invite usr_does_not_exist",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_member",
        chat_id="chat_admin",
    )

    existing_code = next(
        line.removeprefix("Invite code: ")
        for line in str(existing_message).splitlines()
        if line.startswith("Invite code: ")
    )
    web_only_code = next(
        line.removeprefix("Invite code: ")
        for line in str(web_only_message).splitlines()
        if line.startswith("Invite code: ")
    )
    persisted_gateway_state = (tmp_path / "state.db").read_bytes()
    existing_user = gateway._identity_service.consume_web_invite(existing_code)
    web_only_user = gateway._identity_service.consume_web_invite(web_only_code)

    assert str(existing_message).startswith("Web Chat invitation")
    assert "/webchat" in str(existing_message)
    assert existing_user is not None
    assert existing_user.user.canonical_user_id == member_binding.canonical_user_id
    assert existing_code.encode() not in persisted_gateway_state
    assert web_only_code.encode() not in persisted_gateway_state
    assert gateway._identity_service.consume_web_invite(existing_code) is None
    assert str(web_only_message).startswith("Registered web-only user.")
    assert web_only_user is not None
    assert web_only_user.user.display_name == "Browser Person"
    assert web_only_user.user.canonical_user_id.startswith("usr_")
    assert web_only_user.web_invite_label == "Initial browser"
    assert (
        gateway.state_store.list_channel_bindings_for_user(
            web_only_user.user.canonical_user_id
        )
        == []
    )
    pending_token = gateway._identity_service.issue_enrollment_token(
        canonical_user_id=web_only_user.user.canonical_user_id,
        label=web_only_user.web_invite_label,
        pending_expires_at_utc=(
            datetime.now(timezone.utc) + timedelta(hours=12)
        ).isoformat(),
    )
    assert pending_token is not None
    assert pending_token.raw_token.startswith("uauser_")
    assert pending_token.record.status == "pending"
    assert gateway._identity_service.resolve_web_token(pending_token.raw_token) is None
    activated_token = gateway._identity_service.activate_enrollment_token(
        canonical_user_id=web_only_user.user.canonical_user_id,
        token_id=pending_token.record.token_id,
    )
    assert activated_token is not None
    assert activated_token.status == "active"
    assert activated_token.expires_at_utc is None
    assert (
        gateway._identity_service.resolve_web_token(pending_token.raw_token) is not None
    )
    assert pending_token.raw_token.encode() not in (tmp_path / "state.db").read_bytes()
    assert denied_message == "Only an admin can create Web Chat invitations."
    assert denied_probe == "Only an admin can create Web Chat invitations."


@pytest.mark.asyncio
async def test_admin_can_list_registered_users_and_canonical_ids(tmp_path: Path):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    await handle_interactive_session_command(
        raw_text='/user tg_member "Member"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    binding = gateway.state_store.get_active_channel_binding(
        channel_name="telegram",
        external_user_id="tg_member",
    )
    assert binding is not None
    _, _, active_member_denied = await handle_interactive_session_command(
        raw_text="/user list",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_member",
        chat_id="chat_admin",
    )
    await handle_interactive_session_command(
        raw_text=f"/user disable {binding.canonical_user_id}",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )

    _, _, listed = await handle_interactive_session_command(
        raw_text="/user list",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    _, _, disabled_list = await handle_interactive_session_command(
        raw_text="/user list disabled",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    _, _, disabled_member_denied = await handle_interactive_session_command(
        raw_text="/user list",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_member",
        chat_id="chat_admin",
    )

    assert listed is not None
    assert "Registered users: 2" in listed
    assert f"canonical_user_id: {config.user.canonical_user_id}" in listed
    assert f"canonical_user_id: {binding.canonical_user_id}" in listed
    assert "channel_bindings: telegram:tg_member" in listed
    assert "status: disabled" in listed
    assert disabled_list is not None
    assert "Registered users (disabled): 1" in disabled_list
    assert f"canonical_user_id: {binding.canonical_user_id}" in disabled_list
    assert config.user.canonical_user_id not in disabled_list
    assert active_member_denied == "Only an admin can list users."
    assert disabled_member_denied == "Only an admin can list users."


@pytest.mark.asyncio
async def test_admin_can_disable_and_enable_user_without_deleting_history(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    await handle_interactive_session_command(
        raw_text='/user tg_member "Member"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    binding = gateway.state_store.get_active_channel_binding(
        channel_name="telegram",
        external_user_id="tg_member",
    )
    assert binding is not None
    gateway.state_store.create_session(
        session_key="member_session",
        conversation_id="member_conversation",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="telegram",
        user_id="tg_member",
        chat_id="chat_admin",
        owner_canonical_user_id=binding.canonical_user_id,
    )
    _, _, token_message = await handle_interactive_session_command(
        raw_text="/user token phone",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_member",
        chat_id="chat_admin",
    )
    raw_token = str(token_message).splitlines()[1]

    _, _, disabled = await handle_interactive_session_command(
        raw_text="/user disable tg_member",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    blocked_response = await gateway.application.send_user_message(
        "hello while disabled",
        channel_name="telegram",
        user_id="tg_member",
        chat_id="chat_admin",
    )
    disabled_user = gateway.state_store.get_gateway_user(binding.canonical_user_id)
    preserved_session = gateway.state_store.get_session("member_session")
    resolved_token_while_disabled = gateway._identity_service.resolve_web_token(
        raw_token
    )
    self_disable = await gateway._command_operations.set_gateway_user_enabled_status(
        target=config.user.canonical_user_id,
        enabled=False,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )

    _, _, enabled = await handle_interactive_session_command(
        raw_text=f"/user enable {binding.canonical_user_id}",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_admin",
    )
    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope, routing_key: str) -> None:
        published.append(envelope)
        correlation_id = envelope.msg_meta.msg_correlation_id
        await gateway._manager_client.handle_response(
            _response_envelope(
                correlation_id=correlation_id,
                conversation_id="member_conversation",
                task_id=envelope.msg_content.task_id,
                answer="ok",
            )
        )

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]
    enabled_response = await gateway.application.send_user_message(
        "hello after enable",
        channel_name="telegram",
        user_id="tg_member",
        chat_id="chat_admin",
    )

    assert disabled is not None and "Disabled Member (" in disabled
    assert disabled_user is not None and disabled_user.status == "disabled"
    assert resolved_token_while_disabled is None
    assert "disabled" in blocked_response.text
    assert preserved_session is not None
    assert preserved_session.owner_canonical_user_id == binding.canonical_user_id
    assert "Refusing to disable the current admin user" in self_disable
    assert enabled is not None and "Enabled Member (" in enabled
    assert gateway._identity_service.resolve_web_token(raw_token) is not None
    assert enabled_response.text == "ok"
    assert published


@pytest.mark.asyncio
async def test_session_debug_commands_are_scoped_to_current_canonical_user(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_shared",
    )
    await handle_interactive_session_command(
        raw_text='/user tg_user_a "User A"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_shared",
    )
    await handle_interactive_session_command(
        raw_text='/user tg_user_b "User B"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_shared",
    )
    binding_a = gateway.state_store.get_active_channel_binding(
        channel_name="telegram",
        external_user_id="tg_user_a",
    )
    binding_b = gateway.state_store.get_active_channel_binding(
        channel_name="telegram",
        external_user_id="tg_user_b",
    )
    assert binding_a is not None
    assert binding_b is not None
    gateway.state_store.create_session(
        session_key="session_a",
        session_alias="alpha",
        conversation_id="conv_a",
        manager_agent_id="manager_1234",
        channel_name="telegram",
        user_id="tg_user_a",
        chat_id="chat_shared",
        owner_canonical_user_id=binding_a.canonical_user_id,
    )
    gateway.state_store.record_request(
        correlation_id="corr_a",
        session_key="session_a",
        request_kind="interactive",
        request_text="private alpha request",
        conversation_id="conv_a",
        followup_to_task_id=None,
    )
    gateway.state_store.create_session(
        session_key="session_b",
        session_alias="beta",
        conversation_id="conv_b",
        manager_agent_id="manager_1234",
        channel_name="telegram",
        user_id="tg_user_b",
        chat_id="chat_shared",
        owner_canonical_user_id=binding_b.canonical_user_id,
    )
    gateway.state_store.record_request(
        correlation_id="corr_b",
        session_key="session_b",
        request_kind="interactive",
        request_text="private beta request",
        conversation_id="conv_b",
        followup_to_task_id=None,
    )

    _, _, listed = await handle_interactive_session_command(
        raw_text="/list_sessions",
        gateway=gateway,
        current_session_key="session_b",
        channel_name="telegram",
        user_id="tg_user_b",
        chat_id="chat_shared",
    )
    _, _, preview_a = await handle_interactive_session_command(
        raw_text="/preview_session alpha",
        gateway=gateway,
        current_session_key="session_b",
        channel_name="telegram",
        user_id="tg_user_b",
        chat_id="chat_shared",
    )
    _, _, search_a = await handle_interactive_session_command(
        raw_text="/search_session alpha",
        gateway=gateway,
        current_session_key="session_b",
        channel_name="telegram",
        user_id="tg_user_b",
        chat_id="chat_shared",
    )
    _, switched_key, switch_a = await handle_interactive_session_command(
        raw_text="/switch_session alpha",
        gateway=gateway,
        current_session_key="session_b",
        channel_name="telegram",
        user_id="tg_user_b",
        chat_id="chat_shared",
    )

    assert listed is not None
    assert "session_b" in listed
    assert "session_a" not in listed
    assert preview_a == "Session not found: alpha"
    assert search_a == "No sessions matched: alpha"
    assert switch_a == "Session not found: alpha"
    assert switched_key == "session_b"


def test_gateway_resolve_session_rejects_cross_owner_session_key(tmp_path: Path):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    admin = gateway.state_store.create_gateway_user(
        display_name="Admin", timezone="UTC"
    )
    member = gateway.state_store.create_gateway_user(
        display_name="Member", timezone="UTC"
    )
    gateway.state_store.create_channel_binding(
        canonical_user_id=admin.canonical_user_id,
        channel_name="telegram",
        external_user_id="tg_admin",
        chat_scope="telegram:chat_shared",
    )
    gateway.state_store.create_channel_binding(
        canonical_user_id=member.canonical_user_id,
        channel_name="telegram",
        external_user_id="tg_member",
        chat_scope="telegram:chat_shared",
    )
    gateway.state_store.create_session(
        session_key="session_admin",
        conversation_id="conv_admin",
        manager_agent_id="manager_1234",
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_shared",
        owner_canonical_user_id=admin.canonical_user_id,
    )

    with pytest.raises(PermissionError):
        gateway.application.resolve_session(
            session_key="session_admin",
            channel_name="telegram",
            user_id="tg_member",
            chat_id="chat_shared",
        )

    session = gateway.state_store.get_session("session_admin")
    assert session is not None
    assert session.owner_canonical_user_id == admin.canonical_user_id
    assert session.user_id == "tg_admin"


@pytest.mark.asyncio
async def test_newuser_requires_replace_to_rebind_existing_channel_identity(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="discord",
        user_id="discord_admin",
        chat_id="discord_chat",
    )

    await handle_interactive_session_command(
        raw_text='/user discord_123 "Alice Example"',
        gateway=gateway,
        current_session_key=None,
        channel_name="discord",
        user_id="discord_admin",
        chat_id="discord_chat",
    )
    first_binding = gateway.state_store.get_active_channel_binding(
        channel_name="discord",
        external_user_id="discord_123",
    )
    _, _, blocked = await handle_interactive_session_command(
        raw_text='/user discord_123 "Second User"',
        gateway=gateway,
        current_session_key=None,
        channel_name="discord",
        user_id="discord_admin",
        chat_id="discord_chat",
    )
    _, _, replaced = await handle_interactive_session_command(
        raw_text='/user --replace discord_123 "Second User"',
        gateway=gateway,
        current_session_key=None,
        channel_name="discord",
        user_id="discord_admin",
        chat_id="discord_chat",
    )
    second_binding = gateway.state_store.get_active_channel_binding(
        channel_name="discord",
        external_user_id="discord_123",
    )

    assert first_binding is not None
    assert blocked is not None
    assert "already bound" in blocked
    assert replaced is not None
    assert "Registered user" in replaced
    assert second_binding is not None
    assert second_binding.canonical_user_id != first_binding.canonical_user_id


@pytest.mark.asyncio
async def test_newuser_link_token_and_default_channel_commands(tmp_path: Path):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_a",
        thread_id="thread_a",
    )

    _, _, registered_a = await handle_interactive_session_command(
        raw_text='/user tg_link_a "User A"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_admin",
        chat_id="chat_a",
        thread_id="thread_a",
    )
    binding_a = gateway.state_store.get_active_channel_binding(
        channel_name="telegram",
        external_user_id="tg_link_a",
    )
    _, _, code_message = await handle_interactive_session_command(
        raw_text="/user code",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_link_a",
        chat_id="chat_a",
        thread_id="thread_a",
    )
    code = str(code_message).split(": ", 1)[1].splitlines()[0]
    _, _, linked = await handle_interactive_session_command(
        raw_text=f"/user link {code}",
        gateway=gateway,
        current_session_key=None,
        channel_name="discord",
        user_id="discord_link_a",
        chat_id="discord_chat_a",
    )
    discord_binding = gateway.state_store.get_active_channel_binding(
        channel_name="discord",
        external_user_id="discord_link_a",
    )
    _, _, default_set = await handle_interactive_session_command(
        raw_text="/user default-channel current",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_link_a",
        chat_id="chat_a",
        thread_id="thread_a",
    )
    _, _, token_message = await handle_interactive_session_command(
        raw_text="/user token laptop",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_link_a",
        chat_id="chat_a",
    )
    raw_token = str(token_message).splitlines()[1]
    resolved_from_token = gateway._identity_service.resolve_web_token(raw_token)
    token_row = gateway.state_store._repository.resolve_web_user_token(  # noqa: SLF001 - verifies stored hash behavior.
        token_hash=gateway._identity_service.hash_secret(raw_token)  # noqa: SLF001
    )
    user_after_default = (
        gateway.state_store.get_gateway_user(binding_a.canonical_user_id)
        if binding_a
        else None
    )
    _, _, default_cleared = await handle_interactive_session_command(
        raw_text="/user default-channel clear",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_link_a",
        chat_id="chat_a",
    )

    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="whatsapp",
        user_id="whatsapp_admin",
        chat_id="whatsapp_chat_b",
    )
    await handle_interactive_session_command(
        raw_text='/user whatsapp_link_b "User B"',
        gateway=gateway,
        current_session_key=None,
        channel_name="whatsapp",
        user_id="whatsapp_admin",
        chat_id="whatsapp_chat_b",
    )
    _, _, replace_code_message = await handle_interactive_session_command(
        raw_text="/user code",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_link_a",
        chat_id="chat_a",
    )
    replace_code = str(replace_code_message).split(": ", 1)[1].splitlines()[0]
    _, _, replace_blocked = await handle_interactive_session_command(
        raw_text=f"/user link {replace_code}",
        gateway=gateway,
        current_session_key=None,
        channel_name="whatsapp",
        user_id="whatsapp_link_b",
        chat_id="whatsapp_chat_b",
    )
    _, _, replace_linked = await handle_interactive_session_command(
        raw_text=f"/user link {replace_code} --replace",
        gateway=gateway,
        current_session_key=None,
        channel_name="whatsapp",
        user_id="whatsapp_link_b",
        chat_id="whatsapp_chat_b",
    )
    whatsapp_binding = gateway.state_store.get_active_channel_binding(
        channel_name="whatsapp",
        external_user_id="whatsapp_link_b",
    )

    assert registered_a is not None
    assert binding_a is not None
    assert linked is not None and "Linked discord identity" in linked
    assert discord_binding is not None
    assert discord_binding.canonical_user_id == binding_a.canonical_user_id
    assert default_set is not None and "Default notification target set" in default_set
    assert user_after_default is not None
    assert user_after_default.default_channel_name == "telegram"
    assert user_after_default.default_chat_id == "chat_a"
    assert user_after_default.default_thread_id == "thread_a"
    assert resolved_from_token is not None
    assert resolved_from_token.user.canonical_user_id == binding_a.canonical_user_id
    assert token_row is not None
    assert token_row.token_hash != raw_token
    assert raw_token not in token_row.token_hash
    assert (
        default_cleared is not None
        and "Cleared default notification target" in default_cleared
    )
    assert replace_blocked is not None and "already bound" in replace_blocked
    assert replace_linked is not None and "Linked whatsapp identity" in replace_linked
    assert whatsapp_binding is not None
    assert whatsapp_binding.canonical_user_id == binding_a.canonical_user_id


@pytest.mark.asyncio
async def test_coremem_command_manages_team_scoped_memory(tmp_path: Path, gateway_signed_commands: None):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    _attach_in_process_manager_control(gateway, config)
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_coremem_admin",
        chat_id="chat_coremem_1",
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="discord",
        user_id="discord_coremem_admin",
        chat_id="chat_coremem_2",
    )

    handled, _, blocked = await handle_interactive_session_command(
        raw_text='/coremem add "Team always runs smoke tests before deploy"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_coremem_1",
        chat_id="chat_coremem_1",
    )
    await handle_interactive_session_command(
        raw_text='/user tg_coremem_1 "Coremem User A"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_coremem_admin",
        chat_id="chat_coremem_1",
    )
    await handle_interactive_session_command(
        raw_text='/user discord_coremem_2 "Coremem User B"',
        gateway=gateway,
        current_session_key=None,
        channel_name="discord",
        user_id="discord_coremem_admin",
        chat_id="chat_coremem_2",
    )
    _, _, member_create = await handle_interactive_session_command(
        raw_text='/coremem add "Team always runs smoke tests before deploy" tags=deploy,ops',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_coremem_1",
        chat_id="chat_coremem_1",
    )
    _, _, created = await handle_interactive_session_command(
        raw_text='/coremem add "Team always runs smoke tests before deploy" tags=deploy,ops',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_coremem_admin",
        chat_id="chat_coremem_1",
    )
    _, _, listed = await handle_interactive_session_command(
        raw_text="/coremem list range=1-5",
        gateway=gateway,
        current_session_key=None,
        channel_name="discord",
        user_id="discord_coremem_2",
        chat_id="chat_coremem_2",
    )
    _, _, member_delete = await handle_interactive_session_command(
        raw_text="/coremem 1 delete",
        gateway=gateway,
        current_session_key=None,
        channel_name="discord",
        user_id="discord_coremem_2",
        chat_id="chat_coremem_2",
    )
    _, _, deleted = await handle_interactive_session_command(
        raw_text="/coremem 1 delete",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_coremem_admin",
        chat_id="chat_coremem_1",
    )

    assert handled is True
    assert blocked is not None
    assert "Use /user first" in blocked
    assert member_create is not None
    assert "team_admin_required" in member_create
    assert created is not None
    assert "Core memory saved: coremem #1" in created
    assert "event pin_mem_evt_" in created
    assert listed is not None
    assert "Team always runs smoke tests before deploy" in listed
    assert member_delete is not None
    assert "team_admin_required" in member_delete
    assert deleted is not None
    assert "Core memory deleted: coremem #1" in deleted


@pytest.mark.asyncio
async def test_settings_memory_operation_reaches_the_manager_as_the_signed_person(
    tmp_path: Path, gateway_signed_commands: None
):
    """The Settings memory path runs end to end: the gateway checks its person,
    signs it onto the command and computes the actions it may offer."""

    from custom_messages.contracts.control_v1 import PinnedMemoryCommandPayload

    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    _attach_in_process_manager_control(gateway, config)
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_settings_admin",
        chat_id="chat_settings_1",
    )
    operations = gateway._command_operations

    result = await operations.execute_pinned_memory_operation(
        command=PinnedMemoryCommandPayload(
            bank="usermem", action="create", fact_text="Prefers morning summaries."
        ),
        session_key=operations._settings.default_session_key,
        channel_name="telegram",
        user_id="tg_settings_admin",
        chat_id="chat_settings_1",
        thread_id=None,
        canonical_user_id=config.user.canonical_user_id,
    )

    assert result.result.fact is not None
    assert result.result.fact.fact_text == "Prefers morning summaries."
    assert result.can_create is True
    assert result.available_actions


@pytest.mark.asyncio
async def test_knowledge_page_requests_reach_the_manager_as_the_signed_person(
    tmp_path: Path, gateway_signed_commands: None
):
    """The Knowledge page runs end to end through the real gateway: the route's
    person resolves a session, the manager runs as that gateway-signed person,
    and the gateway names the editor before answering the browser."""

    from assistant_gateway.application.knowledge_pages import GatewayKnowledgeRefused
    from assistant_gateway.application.route_context import GatewayRouteContextV1
    from custom_messages.contracts.control_v1 import KnowledgeCommandAction, KnowledgeCommandPayload

    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    _attach_in_process_manager_control(gateway, config)
    person = config.user.canonical_user_id
    context = GatewayRouteContextV1(
        session_key="website:web-chat:knowledge", channel_name="website", user_id=person,
        chat_id="web-chat", canonical_user_id=person,
    )
    knowledge = gateway.application.knowledge

    created = await knowledge.run(
        context,
        KnowledgeCommandPayload(action=KnowledgeCommandAction.WRITE, space="personal", title="Travel", body="Window seats."),
    )
    listed = await knowledge.run(context, KnowledgeCommandPayload(action=KnowledgeCommandAction.LIST, space="personal"))
    with pytest.raises(GatewayKnowledgeRefused) as conflict:
        await knowledge.run(
            context,
            KnowledgeCommandPayload(
                action=KnowledgeCommandAction.WRITE, space="personal", page="travel", base_revision=5, body="Aisle."
            ),
        )

    assert knowledge.enabled is True
    assert created.page.body == "Window seats." and created.page.last_editor.label == "You"
    assert person not in created.model_dump_json()
    assert listed.total == 1
    assert (conflict.value.code, conflict.value.current_revision) == ("conflict", 1)


@pytest.mark.asyncio
async def test_usermem_command_requires_registration_and_isolates_by_canonical_user(
    tmp_path: Path, gateway_signed_commands: None,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    _attach_in_process_manager_control(gateway, config)
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_usermem_admin",
        chat_id="chat_a",
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="discord",
        user_id="discord_usermem_admin",
        chat_id="chat_b",
    )

    _, _, blocked = await handle_interactive_session_command(
        raw_text='/usermem add "I prefer short answers"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_user_a",
        chat_id="chat_a",
    )
    await handle_interactive_session_command(
        raw_text='/user tg_user_a "User A"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_usermem_admin",
        chat_id="chat_a",
    )
    _, _, saved = await handle_interactive_session_command(
        raw_text='/usermem add "I prefer short answers"',
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_user_a",
        chat_id="chat_a",
    )
    _, _, listed_a = await handle_interactive_session_command(
        raw_text="/usermem list",
        gateway=gateway,
        current_session_key=None,
        channel_name="telegram",
        user_id="tg_user_a",
        chat_id="chat_a",
    )
    await handle_interactive_session_command(
        raw_text='/user discord_user_b "User B"',
        gateway=gateway,
        current_session_key=None,
        channel_name="discord",
        user_id="discord_usermem_admin",
        chat_id="chat_b",
    )
    _, _, listed_b = await handle_interactive_session_command(
        raw_text="/usermem list",
        gateway=gateway,
        current_session_key=None,
        channel_name="discord",
        user_id="discord_user_b",
        chat_id="chat_b",
    )

    assert blocked is not None
    assert "Use /user first" in blocked
    assert saved is not None
    assert "User memory saved: usermem #1" in saved
    assert listed_a is not None
    assert "I prefer short answers" in listed_a
    assert listed_b is not None
    assert "No active facts" in listed_b
    assert "I prefer short answers" not in listed_b


@pytest.mark.asyncio
async def test_topic_commands_tag_list_and_switch_threads(tmp_path: Path, gateway_signed_commands: None):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    await _link_bootstrap_admin_channel(
        gateway,
        config,
        channel_name="telegram",
        user_id="tg_topics",
        chat_id="chat_topics",
    )
    session = gateway.application.start_new_session(
        channel_name="telegram",
        user_id="tg_topics",
        chat_id="chat_topics",
    )
    store = _attach_in_process_manager_control(gateway, config)
    store.ensure_thread(
        conversation_id=session.conversation_id,
        team_id=config.manager.team_id,
        owner_agent_id="manager_1234",
        owner_agent_name=config.manager.named_agent_type,
        requester_name="Topic User",
        requester_id="tg_topics",
        root_task_id="task_topic_current",
        subject_preview="Current car repair thread",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    target_conversation_id = "conversation_topic_target"
    store.ensure_thread(
        conversation_id=target_conversation_id,
        team_id=config.manager.team_id,
        owner_agent_id="manager_1234",
        owner_agent_name=config.manager.named_agent_type,
        requester_name="Topic User",
        requester_id="tg_topics",
        root_task_id="task_topic_target",
        subject_preview="Prior car repair thread",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    store.tag_thread(
        conversation_id=target_conversation_id,
        tag="car repair",
        team_id=config.manager.team_id,
        canonical_user_id=config.user.canonical_user_id,
    )

    _, active_session_key, tagged = await handle_interactive_session_command(
        raw_text="/tag_thread diagnostics",
        gateway=gateway,
        current_session_key=session.session_key,
        channel_name="telegram",
        user_id="tg_topics",
        chat_id="chat_topics",
    )
    _, _, topics = await handle_interactive_session_command(
        raw_text="/topics",
        gateway=gateway,
        current_session_key=active_session_key,
        channel_name="telegram",
        user_id="tg_topics",
        chat_id="chat_topics",
    )
    _, switched_session_key, switched = await handle_interactive_session_command(
        raw_text="/switch_topic car repair",
        gateway=gateway,
        current_session_key=active_session_key,
        channel_name="telegram",
        user_id="tg_topics",
        chat_id="chat_topics",
    )
    switched_session = gateway.state_store.get_session(switched_session_key)
    for conversation_id, preview in (
        ("conversation_topic_ambiguous_a", "Ambiguous topic A"),
        ("conversation_topic_ambiguous_b", "Ambiguous topic B"),
    ):
        store.ensure_thread(
            conversation_id=conversation_id,
            team_id=config.manager.team_id,
            owner_agent_id="manager_1234",
            owner_agent_name=config.manager.named_agent_type,
            requester_name="Topic User",
            requester_id="tg_topics",
            root_task_id=f"task_{conversation_id}",
            subject_preview=preview,
            owner_canonical_user_id=config.user.canonical_user_id,
        )
        store.tag_thread(
            conversation_id=conversation_id,
            tag="ambiguous topic",
            team_id=config.manager.team_id,
            canonical_user_id=config.user.canonical_user_id,
        )
    _, ambiguous_session_key, ambiguous = await handle_interactive_session_command(
        raw_text="/switch_topic ambiguous topic",
        gateway=gateway,
        current_session_key=switched_session_key,
        channel_name="telegram",
        user_id="tg_topics",
        chat_id="chat_topics",
    )
    ambiguous_session = gateway.state_store.get_session(ambiguous_session_key)

    assert tagged is not None
    assert "Tagged thread" in tagged
    assert topics is not None
    assert "car-repair" in topics
    assert "diagnostics" in topics
    assert switched is not None
    assert f"conversation {target_conversation_id}" in switched
    assert switched_session is not None
    assert switched_session.conversation_id == target_conversation_id
    assert ambiguous is not None
    assert "Multiple threads matched topic ambiguous-topic" in ambiguous
    assert "conversation_topic_ambiguous_a" in ambiguous
    assert "conversation_topic_ambiguous_b" in ambiguous
    assert "updated=" in ambiguous
    assert " CDT" in ambiguous or " CST" in ambiguous
    assert "+00:00" not in ambiguous
    assert ambiguous_session is not None
    assert ambiguous_session.conversation_id == target_conversation_id


@pytest.mark.asyncio
async def test_gateway_send_user_message_can_target_specific_conversation(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    session = gateway.application.start_new_session()
    gateway.state_store.register_conversation_session(
        session_key=session.session_key,
        conversation_id="conv_extra",
        last_task_id="task_prev_extra",
    )

    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope, routing_key: str) -> None:
        published.append(envelope)
        correlation_id = envelope.msg_meta.msg_correlation_id
        response = _response_envelope(
            correlation_id=correlation_id,
            conversation_id="conv_extra",
            task_id=envelope.msg_content.task_id,
            answer="answer_extra",
        )
        await gateway._manager_client.handle_response(response)

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]

    response = await gateway.application.send_user_message(
        "follow this extra conversation",
        session_key=session.session_key,
        conversation_id="conv_extra",
    )

    assert response.conversation_id == "conv_extra"
    assert published[0].msg_meta.conversation_id == "conv_extra"
    assert published[0].msg_meta.followup_to_task_id == "task_prev_extra"
    assert (
        gateway.state_store.get_session(session.session_key).conversation_id
        == "conv_extra"
    )
    assert (
        gateway.state_store.get_session_conversation("conv_extra").last_task_id
        == published[0].msg_content.task_id
    )


@pytest.mark.asyncio
async def test_gateway_send_user_message_marks_request_timed_out(tmp_path: Path):
    config = _build_test_config(tmp_path)
    config.manager.request_timeout_seconds = 0.01
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    async def fake_publish(envelope: MessageEnvelope, routing_key: str) -> None:
        return None

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]

    with pytest.raises(
        TimeoutError,
        match="Timed out after 0.01s waiting for a terminal manager response.",
    ):
        await gateway.application.send_user_message("this will time out")

    request = gateway.state_store.list_requests_for_session("default", limit=1)[0]
    assert request.status == "timed_out"


@pytest.mark.asyncio
async def test_gateway_send_stop_request_lists_live_work_without_history_targeting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    session = gateway.application.start_new_session()
    gateway.state_store.record_request(
        correlation_id="corr_pending_stop",
        session_key=session.session_key,
        request_kind="interactive",
        request_text="work in progress",
        conversation_id=session.conversation_id,
        followup_to_task_id="task_prev_completed",
        status="timed_out",
    )

    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope, *, routing_key: str) -> None:
        published.append(envelope)
        correlation_id = envelope.msg_meta.msg_correlation_id
        command = envelope.msg_content
        assert isinstance(command, ManagerRuntimeWorkCommand)
        await gateway._manager_client.get_control_channel(correlation_id).put(
            MessageEnvelope(
                msg_content=command,
                msg_meta=MessageMeta(
                    msg_correlation_id=correlation_id,
                    conversation_id=session.conversation_id,
                ),
                msg_header=MessageHeader(
                    from_agent_id="manager_1234",
                    to_routing_path="user_queue",
                    agent_signature="",
                ),
            )
        )
        assert routing_key == "manager_1234"

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]

    from tests.loop_storage_support import record_loop_opens

    with record_loop_opens(monkeypatch) as loop_opens:
        response = await gateway._operational_controls.stop(
            GatewayStopControlRequest(session_key=session.session_key)
        )

    # Web Chat's Tasks panel asks this every 10 seconds; its database work runs off the loop.
    assert loop_opens.where == [], loop_opens.report()
    assert response.message_type == "runtime_work_control"
    assert response.text == "No active work is currently owned by this user."
    assert len(published) == 1
    stop_envelope = published[0]
    assert isinstance(stop_envelope.msg_content, ManagerRuntimeWorkCommand)
    assert stop_envelope.msg_content.action == "inventory"
    assert stop_envelope.msg_content.scope == "user"
    assert stop_envelope.msg_content.target_task_id is None
    assert stop_envelope.msg_header.to_routing_path == "manager_1234"
    assert stop_envelope.msg_meta.is_response_requested is True
    assert stop_envelope.msg_meta.followup_to_task_id is None


@pytest.mark.asyncio
async def test_gateway_send_stop_request_accepts_authenticated_session_owner_without_channel_binding(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    owner = gateway.state_store.create_gateway_user(
        display_name="Owner", timezone="UTC"
    )
    gateway.state_store.create_team_membership(
        canonical_user_id=owner.canonical_user_id,
        team_id=config.gateway.team_id,
        role="member",
    )
    session = gateway.state_store.create_session(
        session_key="website:web-chat:owner-thread",
        conversation_id="conv_owner",
        manager_agent_id="manager_1234",
        channel_name="website",
        user_id=owner.canonical_user_id,
        chat_id="web-chat",
        owner_canonical_user_id=owner.canonical_user_id,
    )
    gateway.state_store.record_request(
        correlation_id="corr_owner_stop",
        session_key=session.session_key,
        request_kind="interactive",
        request_text="work in progress",
        conversation_id=session.conversation_id,
        followup_to_task_id="task_owner_live",
        status="submitted",
    )

    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope, *, routing_key: str) -> None:
        published.append(envelope)
        correlation_id = envelope.msg_meta.msg_correlation_id
        command = envelope.msg_content
        assert isinstance(command, ManagerRuntimeWorkCommand)
        await gateway._manager_client.get_control_channel(correlation_id).put(
            MessageEnvelope(
                msg_content=command,
                msg_meta=MessageMeta(
                    msg_correlation_id=correlation_id,
                    conversation_id=session.conversation_id,
                ),
                msg_header=MessageHeader(
                    from_agent_id="manager_1234",
                    to_routing_path="user_queue",
                    agent_signature="",
                ),
            )
        )
        assert routing_key == "manager_1234"

    gateway._manager_client.publish_durable = fake_publish  # type: ignore[method-assign]

    response = await gateway._operational_controls.stop(
        GatewayStopControlRequest(
            session_key=session.session_key,
            route=GatewayRequestRoute(
                channel_name="website",
                user_id=owner.canonical_user_id,
                chat_id="web-chat",
                canonical_user_id=owner.canonical_user_id,
            ),
        )
    )

    assert response.text == "No active work is currently owned by this user."
    assert len(published) == 1
    assert published[0].msg_meta.from_agent_name.endswith("_gateway")
    assert published[0].msg_meta.gateway_context is not None
    assert (
        published[0].msg_meta.gateway_context.canonical_user_id
        == owner.canonical_user_id
    )

    other = gateway.state_store.create_gateway_user(
        display_name="Other", timezone="UTC"
    )
    with pytest.raises(PermissionError, match="Session not found"):
        await gateway._operational_controls.stop(
            GatewayStopControlRequest(
                session_key=session.session_key,
                route=GatewayRequestRoute(
                    channel_name="website",
                    user_id=other.canonical_user_id,
                    chat_id="web-chat",
                    canonical_user_id=other.canonical_user_id,
                ),
            )
        )


@pytest.mark.asyncio
async def test_gateway_send_context_debug_dump_control_targets_manager(tmp_path: Path):
    config = _build_test_config(tmp_path)
    bootstrap_instance(config)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    session = gateway.application.start_new_session()
    published: list[MessageEnvelope] = []

    async def fake_publish(envelope: MessageEnvelope) -> None:
        published.append(envelope)
        correlation_id = envelope.msg_meta.msg_correlation_id
        await gateway._manager_client.get_response_channel(correlation_id).put(
            MessageEnvelope(
                msg_content=SystemStatusMessage(
                    system_status="Context debug dump settings applied: enabled=true mode=always.",
                ),
                msg_meta=MessageMeta(
                    msg_correlation_id=correlation_id,
                    conversation_id=session.conversation_id,
                ),
                msg_header=MessageHeader(
                    from_agent_id="manager_1234",
                    to_routing_path="user_queue",
                    agent_signature="",
                ),
            )
        )

    gateway._manager_client.publish_operational = fake_publish  # type: ignore[method-assign]

    response = await gateway._operational_controls.set_context_debug_dump(
        GatewayContextDebugControlRequest(
            enabled=True,
            mode="always",
            session_key=session.session_key,
        )
    )

    assert response.message_type == "system_status"
    assert (
        response.text
        == "Context debug dump settings applied: enabled=true mode=always."
    )
    assert len(published) == 1
    envelope = published[0]
    assert isinstance(envelope.msg_content, ControlInstruction)
    assert envelope.msg_content.instruction.instruction == "debug_context_dump"
    assert envelope.msg_content.runtime_settings == {"enabled": True, "mode": "always"}
    assert envelope.msg_content.cascade_to_subtasks is False
    assert envelope.msg_header.to_routing_path == RoutingType.ALL_AGENTS_ROUTE
    assert envelope.msg_meta.is_response_requested is True
    assert envelope.msg_meta.to_named_agent_type == config.manager.named_agent_type


def test_delivery_presentation_prefers_cancelled_answer_over_failed_errors() -> None:
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_id_cancelled",
            task_type=MessageMetaType.RESPONSE,
            task_state=[
                (ProgressState.CANCELLED, datetime.now(timezone.utc).isoformat())
            ],
            errors=["Stop requested by user."],
            answer="Conversation cancelled: Stop requested by user.",
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path="user_queue",
            agent_signature="",
        ),
    )

    rendered = GatewayDeliveryPresentation.render_envelope(envelope)

    assert rendered == "Conversation cancelled: Stop requested by user."


def test_delivery_presentation_prefers_friendly_answer_over_diagnostic_errors() -> None:
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_id_friendly_failure",
            task_type=MessageMetaType.RESPONSE,
            errors=["sealbox-cli credential delete exited with code 2"],
            answer=(
                "I couldn't delete your saved credentials for Ally Bank. "
                "Nothing was changed."
            ),
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path="user_queue",
            agent_signature="",
        ),
    )

    rendered = GatewayDeliveryPresentation.render_envelope(envelope)

    assert rendered == (
        "I couldn't delete your saved credentials for Ally Bank. Nothing was changed."
    )
    assert (
        GatewayDeliveryPresentation.message_level(envelope.msg_content) == "attention"
    )


@pytest.mark.parametrize(
    ("status", "flow", "expected"),
    [
        (
            "credential_stored",
            "credential_collection",
            "Your credentials for Ally Bank were stored successfully.",
        ),
        (
            "credential_deleted",
            "credential_deletion",
            "Your saved credentials for Ally Bank were deleted successfully.",
        ),
        (
            "signup_completed",
            "signup",
            (
                "Your Ally Bank account was created and its credentials were stored "
                "successfully."
            ),
        ),
        (
            "manual_signup_required",
            "signup",
            (
                "More information is needed to finish creating your Ally Bank account. "
                "Use the secure form to continue."
            ),
        ),
        (
            "credential_unavailable",
            "credential_collection",
            "I couldn't store your credentials for Ally Bank. Please try again.",
        ),
    ],
)
def test_delivery_presentation_humanizes_raw_credential_results(
    status: str,
    flow: str,
    expected: str,
) -> None:
    result = SecureCredentialCollectionResult(
        credential_request_id="cred_req_delivery_fallback",
        status=status,
        credential_key="ally_bank_login",
        service_name="Ally Bank",
        display_name="Ally Bank login",
        parent_task_id="task_delivery_fallback",
        metadata={"flow": flow},
        message="",
    )
    envelope = MessageEnvelope(
        msg_content=result,
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(from_agent_id="manager", agent_signature=""),
    )

    rendered = GatewayDeliveryPresentation.render_envelope(envelope)

    assert rendered == expected
    assert status not in rendered
    assert "ally_bank_login" not in rendered


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (
            "completed",
            "The GitHub sign-in is done (account exampleuser).",
            "The GitHub sign-in is done (account exampleuser). Setting up a tool gives nobody access by itself.",
        ),
        ("completed", "", "cli:github is set up. Setting up a tool gives nobody access by itself."),
        (
            "declined",
            "The form for the account link for Portainer was closed without linking anything.",
            "The form for the account link for Portainer was closed without linking anything.",
        ),
        ("unavailable", "", "cli:github was not set up. Nothing was changed."),
    ],
)
def test_delivery_presentation_humanizes_raw_tool_setup_result(status: str, message: str, expected: str) -> None:
    result = ToolSetupResultV1(
        setup_request_id="tool_setup_req_delivery_fallback",
        status=status,
        parent_task_id="task_setup_delivery_fallback",
        tool_id="cli:github",
        kind="sign_in",
        owner="person",
        message=message,
        reason_code="tool_setup_sign_in_failed" if status == "unavailable" else None,
    )
    envelope = MessageEnvelope(
        msg_content=result,
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(from_agent_id="manager", agent_signature=""),
    )

    rendered = GatewayDeliveryPresentation.render_envelope(envelope)

    assert rendered == expected
    assert "tool_setup_" not in rendered
    assert GatewayDeliveryPresentation.message_type(result) == "tool_setup"


def test_delivery_presentation_rejects_manager_control_envelopes() -> None:
    envelope = MessageEnvelope(
        msg_content=ManagerMemoryCommand(
            command_type=MessageMetaType.RESPONSE,
            command=PinnedMemoryCommandPayload(
                bank="usermem",
                action="list",
            ),
            success=True,
        ),
        msg_header=MessageHeader(from_agent_id="manager_1234"),
        msg_meta=MessageMeta(msg_correlation_id="corr_memory_response"),
    )

    assert not GatewayDeliveryPresentation.supports_user_delivery(envelope.msg_content)
    with pytest.raises(
        ValueError,
        match="Unsupported gateway delivery content type: ManagerMemoryCommand",
    ):
        GatewayDeliveryPresentation.render_envelope(envelope)


def test_delivery_presentation_marks_error_task_attention() -> None:
    task = Task(
        task_id="task_attention",
        task_type=MessageMetaType.RESPONSE,
        errors=["Tool call failed."],
    )

    assert GatewayDeliveryPresentation.message_level(task) == "attention"


def test_delivery_presentation_renders_clean_provider_timeout_error() -> None:
    task = Task(
        task_id="task_provider_timeout",
        task_type=MessageMetaType.RESPONSE,
        errors=[PROVIDER_TIMEOUT_USER_MESSAGE],
    )
    envelope = MessageEnvelope(
        msg_content=task,
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(from_agent_id="manager", agent_signature=""),
    )

    assert GatewayDeliveryPresentation.render_envelope(envelope) == (
        f"Task failed:\n- {PROVIDER_TIMEOUT_USER_MESSAGE}"
    )
    assert GatewayDeliveryPresentation.message_level(task) == "attention"


def test_failover_notification_is_presented_as_system_notification() -> None:
    notification = UserNotification(
        text="Model failover: the system switched models and is retrying the request.",
        reason="model_provider_failover",
    )
    envelope = MessageEnvelope(
        msg_content=notification,
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(from_agent_id="manager", agent_signature=""),
    )

    assert (
        GatewayDeliveryPresentation.message_type(notification) == "system_notification"
    )
    assert GatewayDeliveryPresentation.render_envelope(envelope) == notification.text


def test_regular_user_notification_keeps_user_notification_type() -> None:
    notification = UserNotification(
        text="Background work finished.", reason="work_done"
    )

    assert GatewayDeliveryPresentation.message_type(notification) == "user_notification"


def test_delivery_presentation_marks_recoverable_pause_query_attention() -> None:
    query = TaskQuery(
        query_type=MessageMetaType.REQUEST,
        message="The thread hit its tool budget. How should I proceed?",
        query_reason=TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE,
        recoverable_pause_trigger=RecoverablePauseTrigger.TOOL_CALL_BUDGET_EXCEEDED,
    )

    assert GatewayDeliveryPresentation.message_level(query) == "attention"


def test_delivery_presentation_renders_task_query_choices() -> None:
    query = TaskQuery(
        query_type=MessageMetaType.REQUEST,
        message="Which capability values should I apply?",
        choice_mode="multiple",
        choices=[
            TaskQueryChoiceOption(
                option_id="image_true", label="Set image input true", recommended=True
            ),
            TaskQueryChoiceOption(
                option_id="audio_false", label="Set audio input false"
            ),
        ],
    )
    envelope = MessageEnvelope(
        msg_content=query,
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path="user_queue",
            agent_signature="",
        ),
    )

    rendered = GatewayDeliveryPresentation.render_envelope(envelope)

    assert "Choose one or more options:" in rendered
    assert "1. Set image input true (recommended) [image_true]" in rendered
    assert "Reply with option ids or numbers separated by commas." in rendered


def test_gateway_selected_choice_parser_accepts_ids_numbers_and_labels() -> None:
    query = TaskQuery(
        query_type=MessageMetaType.REQUEST,
        message="Choose values.",
        choice_mode="multiple",
        choices=[
            TaskQueryChoiceOption(option_id="image_true", label="Set image input true"),
            TaskQueryChoiceOption(
                option_id="audio_false", label="Set audio input false"
            ),
        ],
    )

    assert selected_choice_ids_from_text(query, "1., audio_false") == [
        "image_true",
        "audio_false",
    ]
    assert selected_choice_ids_from_text(query, "Set image input true") == [
        "image_true"
    ]

    single_query = query.model_copy(update={"choice_mode": "single"})
    assert selected_choice_ids_from_text(single_query, "1,2") == ["image_true"]


@pytest.mark.asyncio
async def test_gateway_pending_task_query_preserves_structured_payload(
    tmp_path: Path,
) -> None:
    config = _build_test_config(tmp_path)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    correlation_id = "corr_choices"
    session_key = "session_choices"
    gateway._manager_client.register_response_channel(correlation_id)
    query = TaskQuery(
        query_type=MessageMetaType.REQUEST,
        message="Approve capability updates?",
        choice_mode="single",
        choices=[
            TaskQueryChoiceOption(
                option_id="approve", label="Approve", recommended=True
            )
        ],
        allow_free_text=False,
    )
    envelope = MessageEnvelope(
        msg_content=query,
        msg_meta=MessageMeta(
            msg_correlation_id=correlation_id, conversation_id="conversation_choices"
        ),
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path="user_queue",
            agent_signature="",
        ),
    )
    await gateway._manager_client.get_response_channel(correlation_id).put(envelope)

    response = await gateway._response_coordinator.await_terminal_response(
        correlation_id=correlation_id,
        session_key=session_key,
        query_handler=None,
    )
    pending = gateway.state_store.get_pending_query(query.task_query_id)

    assert response.message_type == "task_query"
    assert "1. Approve (recommended) [approve]" in response.text
    assert pending is not None
    assert pending.query_text == response.text
    assert pending.query_payload_json is not None
    restored = TaskQuery.model_validate_json(pending.query_payload_json)
    assert restored.choices[0].option_id == "approve"


@pytest.mark.asyncio
@pytest.mark.parametrize("request_kind", ["interactive", "interactive_query_response"])
async def test_late_pause_query_retains_reply_target_after_gateway_restart(
    tmp_path: Path,
    request_kind: str,
) -> None:
    config = _build_test_config(tmp_path)
    config.delivery.quiet_hours.enabled = True
    config.delivery.quiet_hours.start_hour_local = 0
    config.delivery.quiet_hours.end_hour_local = 0
    config.delivery.quiet_hours.allow_urgent_delivery = False
    store = GatewayStateStore(str(tmp_path / "state.db"))
    delivered = []

    def receive(event):
        # A user can answer as soon as the channel displays the prompt.
        assert [
            question.was_shown
            for question in store.list_open_pending_queries(event.session_key)
        ] == [True]
        delivered.append(event)

    gateway = UserGateway(
        config, output_sink=None, state_store=store, event_sink=receive
    )
    session = gateway.application.start_new_session()
    correlation_id = "corr_late_pause"
    store.record_request(
        correlation_id=correlation_id,
        session_key=session.session_key,
        request_kind=request_kind,
        request_text="Review memory",
        conversation_id=session.conversation_id,
        followup_to_task_id=None,
        status="timed_out",
    )
    query = TaskQuery(
        query_type=MessageMetaType.REQUEST,
        parent_task_id="paused_task",
        message="This task has been paused. Reply 1 to continue, 2 for partial results, 3 to stop.",
        query_reason=TaskQueryReason.RECOVERABLE_RUNTIME_PAUSE,
        recoverable_pause_trigger=RecoverablePauseTrigger.TOOL_CALL_BUDGET_EXCEEDED,
    )
    meta = MessageMeta(
        msg_correlation_id=correlation_id,
        conversation_id=session.conversation_id,
        requester_type=RequesterType.USER_REQUESTER,
        gateway_context=GatewayContext(
            session_key=session.session_key,
            canonical_user_id=config.user.canonical_user_id,
            delivery_mode="interactive",
        ),
    )
    header = MessageHeader(from_agent_id=config.manager.manager_agent_id)
    # No response waiter remains, as after a timeout or gateway restart.
    await gateway._manager_client.handle_response(
        MessageEnvelope(
            msg_content=query,
            msg_meta=meta,
            msg_header=header,
        )
    )
    assert len(delivered) == 1
    pending = store.get_pending_query(query.task_query_id)
    assert pending is not None
    assert pending.session_key == session.session_key

    assert pending.query_payload_json == query.model_dump_json()

    async def finish(envelope, routing_key):
        assert isinstance(envelope.msg_content, TaskQuery)
        assert envelope.msg_content.query_type == MessageMetaType.RESPONSE
        assert envelope.msg_content.task_query_id == query.task_query_id
        assert envelope.msg_content.parent_task_id == "paused_task"
        assert envelope.msg_content.message == "1"
        assert envelope.msg_meta.msg_correlation_id == correlation_id
        assert envelope.msg_meta.conversation_id == session.conversation_id
        await restarted._manager_client.handle_response(
            MessageEnvelope(
                msg_content=Task(
                    task_id="paused_task",
                    task_type=MessageMetaType.RESPONSE,
                    answer=TaskAnswer(answer="Review finished"),
                ),
                msg_meta=meta,
                msg_header=header,
            )
        )

    transport = _CaptureGatewayManagerTransport(on_durable=finish)
    restarted = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=transport,
    )
    responses = await ChannelBridge(restarted.application).handle_inbound(
        ChannelInboundMessage(
            channel_name="telegram",
            user_id="test-user",
            chat_id="test-chat",
            session_key=session.session_key,
            text="1",
        )
    )
    assert responses[0].text == "Review finished"
    assert len(transport.durable) == 1
    assert restarted.state_store.list_open_pending_queries(session.session_key) == []
    assert restarted.state_store.get_pending_query(query.task_query_id).status == "answered"


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_fails", [True, False])
async def test_query_reply_consumed_only_after_publication(
    tmp_path: Path,
    publish_fails: bool,
    monkeypatch,
) -> None:
    config = _build_test_config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "state.db"))

    async def publish(envelope, routing_key):
        if publish_fails:
            raise RuntimeError("Publication failed")

    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=store,
        manager_transport=_CaptureGatewayManagerTransport(on_durable=publish),
    )
    session = gateway.application.start_new_session()
    store.add_pending_query(
        session_key=session.session_key,
        correlation_id="corr_answer",
        task_query_id="question",
        parent_task_id="paused_task",
        conversation_id=session.conversation_id,
        query_text="Continue?",
    )
    store.mark_pending_query_shown("question")
    monkeypatch.setattr(
        gateway._response_coordinator,
        "await_terminal_response",
        AsyncMock(side_effect=TimeoutError("Response wait expired")),
    )
    with pytest.raises(RuntimeError if publish_fails else TimeoutError):
        await gateway.application.reply_to_pending_query(
            session_key=session.session_key,
            task_query_id="question",
            response_text="1",
        )
    # An answer that could not be sent leaves the question open for another.
    assert store.get_pending_query("question").status == (
        "open" if publish_fails else "answered"
    )


@pytest.mark.asyncio
async def test_answering_query_preserves_next_question(tmp_path: Path) -> None:
    config = _build_test_config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "state.db"))
    query = TaskQuery(
        query_type=MessageMetaType.REQUEST,
        parent_task_id="paused_task",
        message="Continue again?",
    )

    async def ask_again(envelope, routing_key):
        await gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=query,
                msg_meta=MessageMeta(
                    msg_correlation_id="corr_repeated_pause",
                    conversation_id=session.conversation_id,
                ),
                msg_header=MessageHeader(from_agent_id=config.manager.manager_agent_id),
            )
        )

    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=store,
        manager_transport=_CaptureGatewayManagerTransport(on_durable=ask_again),
    )
    session = gateway.application.start_new_session()
    store.add_pending_query(
        session_key=session.session_key,
        correlation_id="corr_repeated_pause",
        task_query_id="previous_question",
        parent_task_id="paused_task",
        conversation_id=session.conversation_id,
        query_text="Continue?",
    )
    store.mark_pending_query_shown("previous_question")
    response = await gateway.application.reply_to_pending_query(
        session_key=session.session_key,
        task_query_id="previous_question",
        response_text="1",
    )
    assert response.message_type == "task_query"
    assert response.text == "Continue again?"
    [pending] = store.list_open_pending_queries(session.session_key)
    assert pending.task_query_id == query.task_query_id
    assert pending.was_shown


def test_gateway_new_session_tracks_channel_and_active_binding(tmp_path: Path):
    config = _build_test_config(tmp_path)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    session = gateway.application.start_new_session(
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
    )

    assert session.session_key.startswith("session_")
    assert session.conversation_id.startswith("conversation_id_")
    active = gateway.state_store.get_active_session_for_channel(
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
    )
    assert active is not None
    assert active.session_key == session.session_key


def test_gateway_conversation_to_session_mapping_is_gateway_owned(tmp_path: Path):
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    session = store.create_session(
        session_key="session_alpha",
        conversation_id="conv_alpha",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_alpha",
    )

    store.register_conversation_session(
        session_key=session.session_key,
        conversation_id="conv_alpha_followup",
    )

    original = store.get_session_by_conversation_id("conv_alpha")
    followup = store.get_session_by_conversation_id("conv_alpha_followup")

    assert original is not None
    assert followup is not None
    assert original.session_key == "session_alpha"
    assert followup.session_key == "session_alpha"
    assert followup.channel_name == "telegram"
    assert followup.chat_id == "chat_alpha"


def test_gateway_state_store_lists_and_searches_sessions(tmp_path: Path):
    store = GatewayStateStore(str(tmp_path / "gateway_state.db"))
    store.create_session(
        session_key="session_alpha",
        session_alias="general",
        conversation_id="conv_alpha",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_alpha",
    )
    store.record_request(
        correlation_id="corr_alpha",
        session_key="session_alpha",
        request_kind="interactive",
        request_text="Plan the alpha migration",
        conversation_id="conv_alpha",
        followup_to_task_id=None,
    )
    store.create_session(
        session_key="session_beta",
        conversation_id="conv_beta",
        manager_agent_id="manager_1",
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_beta",
    )

    sessions = store.list_sessions()
    assert [session.session_key for session in sessions][:2] == [
        "session_beta",
        "session_alpha",
    ]

    matches = store.search_sessions("alpha")
    assert matches
    assert matches[0].session_key == "session_alpha"
    assert matches[0].matched_request_text == "Plan the alpha migration"
    assert store.get_session_by_alias("general") is not None
    assert store.resolve_session_reference("general") is not None
    assert store.resolve_session_reference("general").session_key == "session_alpha"

    session_list = store.list_sessions_with_preview(limit=2, offset=0)
    assert len(session_list) == 2
    assert session_list[1].latest_request_text == "Plan the alpha migration"
    assert session_list[1].session_alias == "general"
    assert store.count_sessions() == 2

    requests = store.list_requests_for_session("session_alpha")
    assert len(requests) == 1
    assert requests[0].request_text == "Plan the alpha migration"


def test_delivery_projection_logs_fallback_routing(tmp_path: Path, caplog):
    config = _build_test_config(tmp_path)
    store = GatewayStateStore(str(tmp_path / "state.db"))
    gateway = UserGateway(config, output_sink=None, state_store=store)
    session = gateway.application.start_new_session(
        channel_name="telegram",
        user_id="user_1",
        chat_id="chat_1",
    )
    store.upsert_scheduled_target(
        scheduled_job_id="cron_123",
        session_key=session.session_key,
        conversation_id=session.conversation_id,
    )
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_1",
            task_type=MessageMetaType.RESPONSE,
            answer=TaskAnswer(answer="scheduled result"),
        ),
        msg_meta=MessageMeta(
            msg_correlation_id="corr_123",
            conversation_id=session.conversation_id,
        ),
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path="user_queue",
            agent_signature="",
        ),
    )

    with caplog.at_level("WARNING"):
        event = gateway._delivery_projection.project(
            envelope=envelope,
            rendered_text="scheduled result",
            request_record=None,
        )

    assert event.session_key == session.session_key
    assert event.chat_id == "chat_1"
    assert event.scheduled_job_id == "cron_123"
    assert "missing gateway_context" in caplog.text
    assert "conversation_id -> session mapping" in caplog.text
    assert "conversation_id -> scheduled target mapping" in caplog.text


@pytest.mark.asyncio
async def test_gateway_release_due_deliveries_outputs_digest(tmp_path: Path):
    config = _build_test_config(tmp_path)
    outputs: list[str] = []
    gateway = UserGateway(
        config,
        output_sink=lambda text: outputs.append(text),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    gateway.state_store.add_deferred_delivery(
        session_key="cron",
        origin_kind="cron",
        payload={"kind": "cron"},
        rendered_text="First deferred item",
        available_after="2000-01-01T00:00:00+00:00",
    )
    gateway.state_store.add_deferred_delivery(
        session_key="cron",
        origin_kind="cron",
        payload={"kind": "cron"},
        rendered_text="Second deferred item",
        available_after="2000-01-01T00:00:00+00:00",
    )

    released = await gateway.application.release_due_deliveries()

    assert len(released) == 1
    assert "First deferred item" in released[0]
    assert outputs
    assert not gateway.state_store.list_pending_deliveries()


@pytest.mark.asyncio
async def test_gateway_delivers_urgent_user_notification_through_gateway_context(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    # Attachments must sit in the folder of the person the task works for
    # (gateway_context.canonical_user_id below) or the shared team/agents folders.
    image_path = config.layout.person_dir("test_user") / "screenshots" / "site.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"png-data")
    config.delivery.quiet_hours.enabled = True
    config.delivery.quiet_hours.start_hour_local = 0
    config.delivery.quiet_hours.end_hour_local = 0
    config.delivery.quiet_hours.allow_urgent_delivery = True
    config.delivery.urgent_keywords = []
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    manager_transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=manager_transport,
    )
    envelope = MessageEnvelope(
        msg_content=UserNotification(
            text="Finished the GitHub task and posted the result.",
            urgency="urgent",
            attachments=[
                UserNotificationAttachment(
                    path=str(image_path),
                    filename="site.png",
                    content_type="image/png",
                    caption="Screenshot",
                    kind="image",
                    size_bytes=image_path.stat().st_size,
                )
            ],
            reason="github_task_done",
            source_task_id="task_1",
            source_agent_id="agent_1",
            source_agent_name="TeamArchitect",
        ),
        msg_meta=MessageMeta(
            from_agent_name="TeamArchitect",
            msg_correlation_id="notify_corr_1",
            conversation_id="conversation_1",
            requester_type=RequesterType.AGENT_REQUESTER,
            gateway_context=GatewayContext(
                gateway_instance_name=config.instance_name,
                session_key="preferred_session",
                channel_name="telegram",
                user_id="test_user",
                canonical_user_id="test_user",
                chat_id="-100123",
                thread_id="42",
                delivery_mode="agent_notification",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id="agent_1",
            to_routing_path=config.manager.response_queue_name,
            agent_signature="",
        ),
    )

    await gateway.inbound_messages.handle(envelope)

    assert len(delivered_events) == 1
    assert delivered_events[0].text == "Finished the GitHub task and posted the result."
    assert delivered_events[0].channel_name == "telegram"
    assert delivered_events[0].chat_id == "-100123"
    assert delivered_events[0].thread_id == "42"
    assert len(delivered_events[0].attachments) == 1
    # Channels send the gateway's copy in the person's scratch/sent/ folder,
    # read back only while it still has the SHA-256 recorded at staging.
    delivered = Path(delivered_events[0].attachments[0].path)
    layout = InstanceLayout.at(config.instance_root_path)
    assert delivered.parent.parent == layout.person_scratch_dir("test_user") / "sent"
    assert delivered.name == "site.png"
    assert delivered.read_bytes() == image_path.read_bytes()
    assert delivered_events[0].attachments[0].sha256 == hashlib.sha256(b"png-data").hexdigest()
    assert not gateway.state_store.list_pending_deliveries()
    assert len(manager_transport.durable) == 1
    published, routing_key = manager_transport.durable[0]
    assert routing_key == config.gateway.team_id
    assert isinstance(published.msg_content, DeliveryReceipt)
    assert published.msg_content.task_id == "task_1"
    assert published.msg_content.conversation_id == "conversation_1"
    assert published.msg_content.correlation_id == "notify_corr_1"
    assert published.msg_content.delivery_via == "immediate"


@pytest.mark.asyncio
async def test_gateway_delivers_current_thread_user_notification_during_quiet_hours(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.delivery.quiet_hours.enabled = True
    config.delivery.quiet_hours.start_hour_local = 0
    config.delivery.quiet_hours.end_hour_local = 0
    config.delivery.quiet_hours.allow_urgent_delivery = False
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    manager_transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=manager_transport,
    )
    envelope = MessageEnvelope(
        msg_content=UserNotification(
            text="Created usermem #3.",
            urgency="normal",
            reason="memory_created",
            source_task_id="task_1",
            source_agent_id="agent_1",
            source_agent_name="MemReviewAgent",
        ),
        msg_meta=MessageMeta(
            from_agent_name="MemReviewAgent",
            msg_correlation_id="notify_corr_current_thread",
            conversation_id="conversation_1",
            requester_type=RequesterType.AGENT_REQUESTER,
            gateway_context=GatewayContext(
                gateway_instance_name=config.instance_name,
                session_key="source_session",
                channel_name="telegram",
                user_id="telegram_user_1",
                canonical_user_id="canonical_test_user",
                chat_id="-100999",
                thread_id="99",
                delivery_mode="interactive",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id="agent_1",
            to_routing_path=config.manager.response_queue_name,
            agent_signature="",
        ),
    )

    await gateway.inbound_messages.handle(envelope)

    assert len(delivered_events) == 1
    assert delivered_events[0].text == "Created usermem #3."
    assert delivered_events[0].channel_name == "telegram"
    assert delivered_events[0].chat_id == "-100999"
    assert delivered_events[0].thread_id == "99"
    assert not gateway.state_store.list_pending_deliveries()
    assert len(manager_transport.durable) == 1
    published, routing_key = manager_transport.durable[0]
    assert routing_key == config.gateway.team_id
    assert isinstance(published.msg_content, DeliveryReceipt)
    assert published.msg_content.task_id == "task_1"
    assert published.msg_content.conversation_id == "conversation_1"
    assert published.msg_content.correlation_id == "notify_corr_current_thread"
    assert published.msg_content.delivery_via == "immediate"


@pytest.mark.asyncio
async def test_gateway_delivers_interactive_terminal_task_after_pending_request_closes(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.INFO)
    config = _build_test_config(tmp_path)
    config.delivery.quiet_hours.enabled = True
    config.delivery.quiet_hours.start_hour_local = 0
    config.delivery.quiet_hours.end_hour_local = 0
    config.delivery.quiet_hours.allow_urgent_delivery = False
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    manager_transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=manager_transport,
    )
    gateway.state_store.create_session(
        session_key="source_session",
        conversation_id="credential_result_conversation",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="website",
        user_id="web_user_1",
        chat_id="web-chat",
        owner_canonical_user_id="canonical_test_user",
    )
    gateway.state_store.record_request(
        correlation_id="credential_result_corr_after_http_response",
        session_key="source_session",
        request_kind="secure_credential_collection_result",
        request_text="Credential collection completed.",
        conversation_id="credential_result_conversation",
        followup_to_task_id="task_credential_collection",
        status="waiting_for_credential_result",
    )
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_credential_collection",
            task_type=MessageMetaType.RESPONSE,
            answer=TaskAnswer(
                answer="Your credentials for Chase Bank were stored successfully."
            ),
        ),
        msg_meta=MessageMeta(
            from_agent_name="TeamArchitect",
            msg_correlation_id="credential_result_corr_after_http_response",
            conversation_id="credential_result_conversation",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                gateway_instance_name=config.instance_name,
                session_key="source_session",
                channel_name="website",
                user_id="web_user_1",
                canonical_user_id="canonical_test_user",
                chat_id="web-chat",
                delivery_mode="interactive",
                original_message_id="web_request_credential_collection",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path=config.manager.response_queue_name,
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_response(envelope)

    assert len(delivered_events) == 1
    assert (
        delivered_events[0].text
        == "Your credentials for Chase Bank were stored successfully."
    )
    assert delivered_events[0].session_key == "source_session"
    assert delivered_events[0].channel_name == "website"
    assert delivered_events[0].chat_id == "web-chat"
    assert not gateway.state_store.list_pending_deliveries()
    history = gateway._transcript_service.history_page(
        owner_canonical_user_id="canonical_test_user",
        session_key="source_session",
        before_cursor=None,
    )
    assert len(history.messages) == 1
    assert (
        history.messages[0].content
        == "Your credentials for Chase Bank were stored successfully."
    )
    assert (
        history.messages[0].correlation_id
        == "credential_result_corr_after_http_response"
    )
    assert history.messages[0].message_type == "task"
    request_record = gateway.state_store.get_request(
        "credential_result_corr_after_http_response"
    )
    assert request_record is not None
    assert request_record.status == "delivered"
    assert manager_transport.durable == []
    assert "event=gateway_interactive_continuation_delivery_restored" in caplog.text


@pytest.mark.asyncio
async def test_late_credential_result_preserves_newer_conversation_lineage(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    config = _build_test_config(tmp_path)
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda _event: None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=_CaptureGatewayManagerTransport(),
    )
    gateway.state_store.create_session(
        session_key="source_session",
        conversation_id="credential_result_conversation",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="website",
        user_id="web_user_1",
        chat_id="web-chat",
        owner_canonical_user_id="canonical_test_user",
    )
    gateway.state_store.register_conversation_session(
        session_key="source_session",
        conversation_id="credential_result_conversation",
        last_task_id="task_newer_conversation_turn",
        owner_canonical_user_id="canonical_test_user",
    )
    gateway.state_store.record_request(
        correlation_id="late_credential_result_corr",
        session_key="source_session",
        request_kind="secure_credential_collection_result",
        request_text="Credential collection completed.",
        conversation_id="credential_result_conversation",
        followup_to_task_id="task_archived_credential_collection",
        status="waiting_for_credential_result",
    )
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_archived_credential_collection",
            task_type=MessageMetaType.RESPONSE,
            answer="The secure form expired. Please start the request again.",
            errors=["The originating task was no longer active."],
        ),
        msg_meta=MessageMeta(
            from_agent_name="TeamArchitect",
            msg_correlation_id="late_credential_result_corr",
            conversation_id="credential_result_conversation",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                gateway_instance_name=config.instance_name,
                session_key="source_session",
                channel_name="website",
                user_id="web_user_1",
                canonical_user_id="canonical_test_user",
                chat_id="web-chat",
                delivery_mode="interactive",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path=config.manager.response_queue_name,
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_response(envelope)

    conversation = gateway.state_store.get_session_conversation(
        "credential_result_conversation"
    )
    assert conversation is not None
    assert conversation.last_task_id == "task_newer_conversation_turn"
    request = gateway.state_store.get_request("late_credential_result_corr")
    assert request is not None
    assert request.status == "delivered"
    assert "event=gateway_late_credential_result_lineage_preserved" in caplog.text


@pytest.mark.asyncio
async def test_gateway_does_not_restore_interactive_delivery_for_agent_task(
    tmp_path: Path,
) -> None:
    config = _build_test_config(tmp_path)
    config.delivery.quiet_hours.enabled = True
    config.delivery.quiet_hours.start_hour_local = 0
    config.delivery.quiet_hours.end_hour_local = 0
    config.delivery.quiet_hours.allow_urgent_delivery = False
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=_CaptureGatewayManagerTransport(),
    )
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_agent_owned",
            task_type=MessageMetaType.RESPONSE,
            answer=TaskAnswer(answer="Autonomous task finished."),
        ),
        msg_meta=MessageMeta(
            from_agent_name="Specialist",
            msg_correlation_id="agent_owned_corr",
            conversation_id="agent_owned_conversation",
            requester_type=RequesterType.AGENT_REQUESTER,
            gateway_context=GatewayContext(
                gateway_instance_name=config.instance_name,
                session_key="agent_owned_session",
                channel_name="telegram",
                user_id="telegram_user_1",
                canonical_user_id="canonical_test_user",
                chat_id="-100999",
                delivery_mode="interactive",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id="agent_1",
            to_routing_path=config.manager.response_queue_name,
            agent_signature="",
        ),
    )

    await gateway.inbound_messages.handle(envelope)

    assert delivered_events == []
    pending = gateway.state_store.list_pending_deliveries()
    assert len(pending) == 1
    assert pending[0].origin_kind == "autonomous"


@pytest.mark.asyncio
async def test_gateway_drops_unsupported_unsolicited_manager_control(
    tmp_path: Path,
    caplog,
) -> None:
    config = _build_test_config(tmp_path)
    outputs: list[str] = []
    gateway = UserGateway(
        config,
        output_sink=outputs.append,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=_CaptureGatewayManagerTransport(),
    )
    session = gateway.application.start_new_session()
    envelope = MessageEnvelope(
        msg_content=ManagerMemoryCommand(
            command_id="manager_memory_late",
            command_type=MessageMetaType.RESPONSE,
            command=PinnedMemoryCommandPayload(
                bank="usermem",
                action="list",
            ),
            success=True,
            rendered_result="No memories found.",
        ),
        msg_meta=MessageMeta(
            msg_correlation_id="corr_memory_late",
            conversation_id=session.conversation_id,
            gateway_context=GatewayContext(
                canonical_user_id=config.user.canonical_user_id,
                session_key=session.session_key,
                channel_name="local",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id=config.manager.manager_agent_id,
            to_routing_path=config.manager.response_queue_name,
            agent_signature="",
        ),
    )

    with caplog.at_level(logging.WARNING):
        await gateway.inbound_messages.handle(envelope)

    assert outputs == []
    assert "event=gateway_unsupported_unsolicited_delivery_dropped" in caplog.text


@pytest.mark.asyncio
async def test_gateway_fails_unmatched_credential_request_closed_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _build_test_config(tmp_path)
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=_CaptureGatewayManagerTransport(),
    )
    session = gateway.application.start_new_session()
    correlation_id = "correlation_unmatched_credential"
    gateway.state_store.record_request(
        correlation_id=correlation_id,
        session_key=session.session_key,
        request_kind="interactive",
        request_text="Store an Example login",
        conversation_id=session.conversation_id,
        followup_to_task_id=None,
        status="submitted",
    )
    publish_result = AsyncMock()
    monkeypatch.setattr(gateway._credential_workflow, "publish_result", publish_result)
    request_content = SecureCredentialCollectionRequest(
        service_name="Example",
        credential_key="example_login",
        purpose="Store a test login.",
        parent_task_id="task_unmatched_credential",
    )
    envelope = MessageEnvelope(
        msg_content=request_content,
        msg_meta=MessageMeta(
            from_agent_name="TeamArchitect",
            msg_correlation_id=correlation_id,
            conversation_id=session.conversation_id,
            requester_type=RequesterType.AGENT_REQUESTER,
        ),
        msg_header=MessageHeader(
            from_agent_id=config.manager.manager_agent_id,
            to_routing_path=config.manager.response_queue_name,
            agent_signature="",
        ),
    )

    await gateway.inbound_messages.handle(envelope)

    publish_result.assert_awaited_once()
    failure = publish_result.await_args.kwargs["session_payload"]
    assert failure["status"] == "credential_unavailable"
    assert "interactive response expired" in failure["result"]["error"]
    assert delivered_events == []


@pytest.mark.asyncio
async def test_autonomous_force_immediate_intent_is_persisted(tmp_path: Path) -> None:
    config = _build_test_config(tmp_path)
    manager_transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=manager_transport,
        id_generator=_FixedGatewayIds("corr_id_force_immediate"),
    )
    session = gateway.application.start_new_session()

    correlation_id = await gateway.automated_submission.submit_autonomous_task(
        GatewayAutonomousTaskRequest(
            instruction="Send this update now.",
            session_key=session.session_key,
            origin_kind="heartbeat",
            deliver_quiet_hours=False,
        )
    )

    request = gateway.state_store.get_request(correlation_id)
    assert request is not None
    assert request.request_kind == "interactive"
    published, _routing_key = manager_transport.durable[0]
    assert published.msg_meta.gateway_context is not None
    assert published.msg_meta.gateway_context.delivery_mode == "interactive"


@pytest.mark.asyncio
async def test_gateway_unsolicited_timer_delivery_emits_receipt_immediately(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    outputs: list[str] = []
    manager_transport = _CaptureGatewayManagerTransport()
    delivered_at = datetime(2026, 1, 2, 18, 0, tzinfo=timezone.utc)
    gateway = UserGateway(
        config,
        output_sink=lambda text: outputs.append(text),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=manager_transport,
        clock=_FixedGatewayClock(delivered_at),
    )
    gateway.state_store.create_session(
        session_key="session_sched1234",
        conversation_id="conversation_id_sched1234",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="local",
        user_id="test-user",
        chat_id="chat_101",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    gateway.state_store.record_request(
        correlation_id="schedule_corr_sched1234",
        session_key="session_sched1234",
        request_kind="cron",
        request_text="Time to grab your tea",
        conversation_id="conversation_id_sched1234",
        followup_to_task_id="task_id_schedule_request",
        status="pending",
    )
    monkeypatch.setattr(
        unsolicited_delivery_module,
        "evaluate_delivery",
        lambda **_: SimpleNamespace(action="deliver_now", available_after=None),
    )

    envelope = _timer_response_envelope(
        correlation_id="schedule_corr_sched1234",
        conversation_id="conversation_id_sched1234",
        task_id="task_id_reminder_fire",
        session_key="session_sched1234",
        scheduled_job_id="sched_test1234",
        answer="Reminder: Time to grab your tea!",
    )

    await gateway.inbound_messages.handle(envelope)

    assert outputs == ["Reminder: Time to grab your tea!"]
    request = gateway.state_store.get_request("schedule_corr_sched1234")
    assert request is not None
    assert request.status == "delivered"
    assert len(manager_transport.durable) == 1
    published, routing_key = manager_transport.durable[0]
    assert routing_key == config.gateway.team_id
    assert isinstance(published.msg_content, DeliveryReceipt)
    assert published.msg_content.task_id == "task_id_reminder_fire"
    assert published.msg_content.delivery_via == "immediate"
    assert published.msg_content.scheduled_job_id == "sched_test1234"
    assert published.msg_content.delivered_at == delivered_at.isoformat()


@pytest.mark.asyncio
async def test_gateway_deferred_timer_delivery_emits_receipt_on_digest_release(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    outputs: list[str] = []
    manager_transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config,
        output_sink=lambda text: outputs.append(text),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=manager_transport,
    )
    gateway.state_store.create_session(
        session_key="session_sched1234",
        conversation_id="conversation_id_sched1234",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="local",
        user_id="test-user",
        chat_id="chat_101",
    )
    gateway.state_store.record_request(
        correlation_id="schedule_corr_sched1234",
        session_key="session_sched1234",
        request_kind="cron",
        request_text="Time to grab your tea",
        conversation_id="conversation_id_sched1234",
        followup_to_task_id="task_id_schedule_request",
        status="pending",
    )
    monkeypatch.setattr(
        unsolicited_delivery_module,
        "evaluate_delivery",
        lambda **_: SimpleNamespace(
            action="defer", available_after="2000-01-01T00:00:00+00:00"
        ),
    )

    envelope = _timer_response_envelope(
        correlation_id="schedule_corr_sched1234",
        conversation_id="conversation_id_sched1234",
        task_id="task_id_reminder_fire",
        session_key="session_sched1234",
        scheduled_job_id="sched_test1234",
        answer="Reminder: Time to grab your tea!",
    )

    await gateway.inbound_messages.handle(envelope)

    request = gateway.state_store.get_request("schedule_corr_sched1234")
    assert request is not None
    assert request.status == "deferred"
    assert manager_transport.durable == []

    released = await gateway.application.release_due_deliveries()

    assert len(released) == 1
    assert "Reminder: Time to grab your tea!" in released[0]
    request = gateway.state_store.get_request("schedule_corr_sched1234")
    assert request is not None
    assert request.status == "delivered"
    assert len(manager_transport.durable) == 1
    published, routing_key = manager_transport.durable[0]
    assert routing_key == config.gateway.team_id
    assert isinstance(published.msg_content, DeliveryReceipt)
    assert published.msg_content.task_id == "task_id_reminder_fire"
    assert published.msg_content.delivery_via == "digest"


@pytest.mark.asyncio
async def test_gateway_submit_automation_publishes_full_inline_automation_message(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    manager_transport = _CaptureGatewayManagerTransport()
    now_utc = datetime(2026, 1, 2, 18, 0, tzinfo=timezone.utc)
    gateway = UserGateway(
        config,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=manager_transport,
        clock=_FixedGatewayClock(now_utc),
        id_generator=_FixedGatewayIds("corr_id_automation"),
    )
    gateway.state_store.create_session(
        session_key="session_auto1234",
        conversation_id="conversation_auto1234",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="local",
        user_id="test-user",
        chat_id="chat_101",
        owner_canonical_user_id=config.user.canonical_user_id,
    )
    original_envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_original_1234",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Original user request",
        ),
        msg_meta=MessageMeta(
            conversation_id="conversation_auto1234",
            request_timezone="America/Chicago",
            requester_type=RequesterType.USER_REQUESTER,
            gateway_context=GatewayContext(
                session_key="session_auto1234",
                channel_name="local",
                user_id="test-user",
                canonical_user_id=config.user.canonical_user_id,
                chat_id="chat_101",
                original_message_id="orig_msg_1234",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id="gateway_queue",
            to_routing_path="manager_1234",
            response_routing_key="gateway_queue",
            agent_signature="",
        ),
    )

    correlation_id = await gateway.automated_submission.submit_automation(
        GatewayAutomationRequest(
            automation_id="automation_1234",
            hook_name="after_user_reply",
            original_envelope=original_envelope,
            rendered_original_user_text="Original user request",
            allowed_decisions=("reply_now", "defer"),
            decision_instructions="Only reply if the request is actionable.",
        )
    )

    assert correlation_id == "corr_id_automation"
    request = gateway.state_store.get_request(correlation_id)
    assert request is not None
    assert request.request_kind == "automation"
    assert request.followup_to_task_id == "task_original_1234"
    assert len(manager_transport.durable) == 1
    published, routing_key = manager_transport.durable[0]
    assert routing_key == config.manager.manager_agent_id
    assert isinstance(published.msg_content, AutomationMessage)
    assert published.msg_content.automation_id == "automation_1234"
    assert published.msg_content.hook_name == "after_user_reply"
    assert published.msg_content.rendered_original_user_text == "Original user request"
    assert published.msg_content.requester_local_time == "2026-01-02T12:00:00-06:00"
    assert (
        published.msg_content.original_envelope_snapshot.msg_content["task_id"]
        == "task_original_1234"
    )
    assert published.msg_meta is not None
    assert published.msg_meta.requester_type == RequesterType.TIMER_REQUESTER
    assert published.msg_meta.followup_to_task_id == "task_original_1234"
    assert published.msg_meta.gateway_context is not None
    assert published.msg_meta.gateway_context.delivery_mode == "automation"


@pytest.mark.asyncio
async def test_gateway_automation_reply_still_emits_delivery_receipt(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    outputs: list[str] = []
    manager_transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config,
        output_sink=lambda text: outputs.append(text),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_transport=manager_transport,
    )
    gateway.state_store.create_session(
        session_key="session_auto1234",
        conversation_id="conversation_auto1234",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="local",
        user_id="test-user",
        chat_id="chat_101",
    )
    gateway.state_store.record_request(
        correlation_id="automation_corr_1234",
        session_key="session_auto1234",
        request_kind="automation",
        request_text="Original user request",
        conversation_id="conversation_auto1234",
        followup_to_task_id="task_original_1234",
        status="pending",
    )
    monkeypatch.setattr(
        unsolicited_delivery_module,
        "evaluate_delivery",
        lambda **_: SimpleNamespace(action="deliver_now", available_after=None),
    )
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_automation_reply_1234",
            task_type=MessageMetaType.RESPONSE,
            answer=TaskAnswer(answer="Automation follow-up reply"),
        ),
        msg_meta=MessageMeta(
            msg_correlation_id="automation_corr_1234",
            conversation_id="conversation_auto1234",
            requester_type=RequesterType.TIMER_REQUESTER,
            gateway_context=GatewayContext(
                session_key="session_auto1234",
                channel_name="local",
                user_id="test-user",
                chat_id="chat_101",
                delivery_mode="automation",
                scheduled_job_id="automation_1234",
            ),
        ),
        msg_header=MessageHeader(
            from_agent_id="manager_1234",
            to_routing_path="user_queue",
            agent_signature="",
        ),
    )

    await gateway.inbound_messages.handle(envelope)

    assert outputs == ["Automation follow-up reply"]
    request = gateway.state_store.get_request("automation_corr_1234")
    assert request is not None
    assert request.status == "delivered"
    assert len(manager_transport.durable) == 1
    published, routing_key = manager_transport.durable[0]
    assert routing_key == config.gateway.team_id
    assert isinstance(published.msg_content, DeliveryReceipt)
    assert published.msg_content.task_id == "task_automation_reply_1234"
    assert published.msg_content.delivery_via == "immediate"


@pytest.mark.asyncio
async def test_gateway_discover_manager_id_reads_matching_status_from_iterator(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.manager.named_agent_type = "TeamArchitect"
    expected_correlation_id = "corr_id_deadbeef"

    gateway: UserGateway

    async def fake_publish(envelope, routing_key):
        assert envelope.msg_meta.msg_correlation_id == expected_correlation_id
        assert routing_key == RoutingType.ALL_AGENTS_ROUTE.value
        response = MessageEnvelope(
            msg_content=AgentStatus(
                self_id="manager_live_123",
                team_id=config.manager.team_id,
                status_type=MessageMetaType.RESPONSE,
                info_type=ControlType.STATUS_REQUEST_BASIC,
                named_agent_type="TeamArchitect",
                is_paused=False,
                is_executing=False,
                has_execution_capacity=True,
                is_busy=False,
                name="manager",
                agent_role="manager",
            ),
            msg_meta=MessageMeta(msg_correlation_id=expected_correlation_id),
            msg_header=MessageHeader(
                from_agent_id="manager_live_123",
                to_routing_path=config.manager.response_queue_name,
                agent_signature="",
            ),
        )
        await gateway._manager_client.get_response_channel(expected_correlation_id).put(
            response
        )

    manager_transport = _CaptureGatewayManagerTransport(on_operational=fake_publish)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        id_generator=_FixedGatewayIds(expected_correlation_id),
        manager_transport=manager_transport,
    )

    manager_id = await gateway._manager_client.discover_manager_id(timeout_seconds=0.1)

    assert manager_id == "manager_live_123"
    assert len(manager_transport.operational) == 1


def _manager_status(config, manager_id: str = "manager_live_123") -> AgentStatus:
    return AgentStatus(
        self_id=manager_id,
        team_id=config.manager.team_id,
        status_type=MessageMetaType.RESPONSE,
        info_type=ControlType.STATUS_REQUEST_BASIC,
        named_agent_type="TeamArchitect",
        is_paused=False,
        is_executing=False,
        has_execution_capacity=True,
        is_busy=False,
        name="manager",
        agent_role="manager",
    )


@pytest.mark.asyncio
async def test_gateway_discovery_asks_again_until_a_starting_manager_answers(tmp_path: Path, monkeypatch):
    """At startup the manager misses the first request; discovery repeats it instead of waiting out the timeout."""

    from assistant_gateway.application import manager_client

    monkeypatch.setattr(manager_client, "DISCOVERY_RESEND_SECONDS", 0.02)
    config = _build_test_config(tmp_path)
    config.manager.named_agent_type = "TeamArchitect"
    gateway: UserGateway

    async def answer_from_the_third_request(envelope, routing_key):
        if len(manager_transport.operational) < 3:
            return  # the manager is still starting
        response = MessageEnvelope(
            msg_content=_manager_status(config),
            msg_meta=MessageMeta(msg_correlation_id=envelope.msg_meta.msg_correlation_id),
            msg_header=MessageHeader(
                from_agent_id="manager_live_123", to_routing_path=config.manager.response_queue_name, agent_signature=""
            ),
        )
        await gateway._manager_client.get_response_channel(envelope.msg_meta.msg_correlation_id).put(response)

    manager_transport = _CaptureGatewayManagerTransport(on_operational=answer_from_the_third_request)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        id_generator=_FixedGatewayIds(),
        manager_transport=manager_transport,
    )

    started = asyncio.get_running_loop().time()
    manager_id = await gateway._manager_client.discover_manager_id(timeout_seconds=5)

    assert manager_id == "manager_live_123"
    assert len(manager_transport.operational) == 3
    assert asyncio.get_running_loop().time() - started < 1, "found when it answered, not at the timeout"


@pytest.mark.asyncio
async def test_gateway_discovery_ends_when_the_manager_announced_itself(tmp_path: Path, monkeypatch):
    from assistant_gateway.application import manager_client

    monkeypatch.setattr(manager_client, "DISCOVERY_RESEND_SECONDS", 0.02)
    config = _build_test_config(tmp_path)
    config.manager.named_agent_type = "TeamArchitect"
    gateway: UserGateway

    async def announce_instead_of_answering(envelope, routing_key):
        await gateway._manager_client.apply_manager_status(_manager_status(config, "manager_announced"), source="presence")

    manager_transport = _CaptureGatewayManagerTransport(on_operational=announce_instead_of_answering)
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        id_generator=_FixedGatewayIds(),
        manager_transport=manager_transport,
    )

    assert await gateway._manager_client.discover_manager_id(timeout_seconds=5) == "manager_announced"
    assert len(manager_transport.operational) == 1


@pytest.mark.asyncio
async def test_gateway_discover_manager_id_times_out_cleanly(tmp_path: Path):
    config = _build_test_config(tmp_path)
    config.manager.named_agent_type = "TeamArchitect"
    manager_transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        id_generator=_FixedGatewayIds(),
        manager_transport=manager_transport,
    )

    with pytest.raises(
        TimeoutError, match="Timed out waiting for TeamArchitect discovery."
    ):
        await gateway._manager_client.discover_manager_id(timeout_seconds=0.01)
    assert len(manager_transport.operational) == 1


@pytest.mark.asyncio
async def test_gateway_connect_refreshes_stale_manager_id_before_accepting_requests(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "agent_id_stale1234"
    save_mock = Mock()

    def fake_save(self):
        save_mock()

    monkeypatch.setattr(type(config), "save", fake_save)

    fake_adapter = _fake_nats_adapter()
    manager_runtime = NatsGatewayManagerRuntime.from_gateway_config(
        config,
        adapter=fake_adapter,
    )
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_runtime=manager_runtime,
    )
    gateway._manager_client.discover_manager_id = AsyncMock(
        return_value="agent_id_live5678"
    )

    await gateway.connect(start_services=False)

    assert gateway.config.manager.manager_agent_id == "agent_id_live5678"
    gateway._manager_client.discover_manager_id.assert_awaited_once()
    save_mock.assert_called_once()
    fake_adapter.connect.assert_awaited_once()
    fake_adapter.subscribe_route.assert_awaited_once()
    assert fake_adapter.subscribe_subject.await_count == 2


@pytest.mark.asyncio
async def test_gateway_connect_skips_config_save_when_manager_id_is_already_current(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "agent_id_live5678"
    save_mock = Mock()

    def fake_save(self):
        save_mock()

    monkeypatch.setattr(type(config), "save", fake_save)

    fake_adapter = _fake_nats_adapter()
    manager_runtime = NatsGatewayManagerRuntime.from_gateway_config(
        config,
        adapter=fake_adapter,
    )
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_runtime=manager_runtime,
    )
    gateway._manager_client.discover_manager_id = AsyncMock(
        return_value="agent_id_live5678"
    )

    await gateway.connect(start_services=False)

    assert gateway.config.manager.manager_agent_id == "agent_id_live5678"
    gateway._manager_client.discover_manager_id.assert_awaited_once()
    save_mock.assert_not_called()
    fake_adapter.connect.assert_awaited_once()
    fake_adapter.subscribe_route.assert_awaited_once()
    assert fake_adapter.subscribe_subject.await_count == 2


@pytest.mark.asyncio
async def test_gateway_connect_tolerates_manager_discovery_timeout_without_cached_route(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    fake_adapter = _fake_nats_adapter()
    manager_runtime = NatsGatewayManagerRuntime.from_gateway_config(
        config,
        adapter=fake_adapter,
    )
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_runtime=manager_runtime,
    )
    gateway._manager_client.discover_manager_id = AsyncMock(
        side_effect=TimeoutError("Timed out waiting for TeamArchitect discovery.")
    )

    await gateway.connect(start_services=False)

    assert gateway.config.manager.manager_agent_id is None
    gateway._manager_client.discover_manager_id.assert_awaited_once()
    save_mock.assert_not_called()
    fake_adapter.subscribe_route.assert_awaited_once()
    assert fake_adapter.subscribe_subject.await_count == 2


@pytest.mark.asyncio
async def test_gateway_connect_tolerates_manager_discovery_timeout_and_clears_stale_route(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "agent_id_stale1234"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    fake_adapter = _fake_nats_adapter()
    manager_runtime = NatsGatewayManagerRuntime.from_gateway_config(
        config,
        adapter=fake_adapter,
    )
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_runtime=manager_runtime,
    )
    gateway._manager_client.discover_manager_id = AsyncMock(
        side_effect=TimeoutError("Timed out waiting for TeamArchitect discovery.")
    )

    await gateway.connect(start_services=False)

    assert gateway.config.manager.manager_agent_id is None
    gateway._manager_client.discover_manager_id.assert_awaited_once()
    save_mock.assert_called_once()
    fake_adapter.subscribe_route.assert_awaited_once()
    assert fake_adapter.subscribe_subject.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization_enabled", [True, False])
async def test_gateway_connect_does_not_subscribe_to_the_old_authorization_stream(
    tmp_path: Path, authorization_enabled: bool
) -> None:
    config = _build_test_config(tmp_path)
    config.authorization.enabled = authorization_enabled
    fake_adapter = _fake_nats_adapter()
    manager_runtime = NatsGatewayManagerRuntime.from_gateway_config(
        config,
        adapter=fake_adapter,
    )
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_runtime=manager_runtime,
    )
    gateway._manager_client.discover_manager_id = AsyncMock(return_value=None)

    await gateway.connect(start_services=False)

    assert fake_adapter.subscribe_subject.await_count == 2
    subjects = {
        call.kwargs["subject"]
        for call in fake_adapter.subscribe_subject.await_args_list
    }
    assert not any(".authorization." in subject for subject in subjects)
    await gateway.close()


@pytest.mark.asyncio
async def test_gateway_refresh_manager_id_keeps_live_route_learned_from_broadcast_during_discovery_timeout(
    tmp_path: Path,
    monkeypatch,
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "agent_id_stale1234"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    async def fake_discover_manager_id() -> str:
        await gateway._manager_client.apply_manager_status(
            AgentStatus(
                self_id="agent_id_live5678",
                team_id=config.manager.team_id,
                named_agent_type=config.manager.named_agent_type,
                is_paused=False,
                is_executing=False,
                has_execution_capacity=True,
                is_busy=False,
                is_exiting=False,
                name="manager",
                agent_role="manager",
                last_seen_at=datetime.now(timezone.utc).isoformat(),
                status_type=MessageMetaType.RESPONSE,
                info_type=ControlType.STATUS_REQUEST_BASIC,
            ),
            source="broadcast",
        )
        raise TimeoutError("Timed out waiting for TeamArchitect discovery.")

    gateway._manager_client.discover_manager_id = fake_discover_manager_id

    await gateway._manager_client.refresh_manager_id()

    assert gateway.config.manager.manager_agent_id == "agent_id_live5678"
    assert save_mock.call_count == 1


@pytest.mark.asyncio
async def test_gateway_refresh_manager_id_keeps_cached_route_when_same_manager_rebroadcasts_during_discovery_timeout(
    tmp_path: Path,
    monkeypatch,
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "agent_id_live5678"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    async def fake_discover_manager_id() -> str:
        await gateway._manager_client.apply_manager_status(
            AgentStatus(
                self_id="agent_id_live5678",
                team_id=config.manager.team_id,
                named_agent_type=config.manager.named_agent_type,
                is_paused=False,
                is_executing=False,
                has_execution_capacity=True,
                is_busy=False,
                is_exiting=False,
                name="manager",
                agent_role="manager",
                last_seen_at=datetime.now(timezone.utc).isoformat(),
                status_type=MessageMetaType.RESPONSE,
                info_type=ControlType.STATUS_REQUEST_BASIC,
            ),
            source="broadcast",
        )
        raise TimeoutError("Timed out waiting for TeamArchitect discovery.")

    gateway._manager_client.discover_manager_id = fake_discover_manager_id

    await gateway._manager_client.refresh_manager_id()

    assert gateway.config.manager.manager_agent_id == "agent_id_live5678"
    save_mock.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_manager_status_requires_configured_team(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    for team_id in (None, "NO_TEAM", "other_team"):
        await gateway._manager_client.apply_manager_status(
            AgentStatus(
                self_id=f"agent_id_{team_id or 'missing'}",
                team_id=team_id,
                named_agent_type=config.manager.named_agent_type,
                is_paused=False,
                is_executing=False,
                has_execution_capacity=True,
                is_busy=False,
                is_exiting=False,
                name="manager",
                agent_role="manager",
                last_seen_at=datetime.now(timezone.utc).isoformat(),
                status_type=MessageMetaType.RESPONSE,
                info_type=ControlType.STATUS_REQUEST_BASIC,
            ),
            source="broadcast",
        )

    assert gateway.config.manager.manager_agent_id is None
    save_mock.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_connect_starts_services_without_manager_route(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None

    fake_adapter = _fake_nats_adapter()
    manager_runtime = NatsGatewayManagerRuntime.from_gateway_config(
        config,
        adapter=fake_adapter,
    )
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
        manager_runtime=manager_runtime,
    )
    gateway._manager_client.discover_manager_id = AsyncMock(
        side_effect=TimeoutError("Timed out waiting for TeamArchitect discovery.")
    )

    await gateway.connect(start_services=True)
    try:
        assert gateway.config.manager.manager_agent_id is None
        task_names = {task.get_name() for task in gateway._background_tasks}
        assert "gateway_delivery_loop" in task_names
        assert "gateway_manager_liveness_loop" in task_names
        if gateway._activity_notices.enabled:
            assert "gateway_activity_plan_notice_loop" in task_names
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_gateway_broadcast_presence_updates_manager_route(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="manager_live_4321",
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=False,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_live_4321",
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_presence(envelope)

    assert gateway.config.manager.manager_agent_id == "manager_live_4321"
    save_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_broadcast_presence_installs_heartbeat_schedule_after_manager_late_start(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "-100123"
    config.delivery.default_target.thread_id = "42"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._started = True
    gateway.manager_transitions.start()
    gateway.manager_transitions.enable_notices()
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]
    envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="manager_live_4321",
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=False,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_live_4321",
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_presence(envelope)

    assert gateway.config.manager.manager_agent_id == "manager_live_4321"
    gateway._maintenance_schedules.ensure_all.assert_awaited_once()
    assert len(delivered_events) == 1
    assert delivered_events[0].text == _telegram_transition_available_notice(config)
    assert delivered_events[0].channel_name == "telegram"
    assert delivered_events[0].chat_id == "-100123"
    assert delivered_events[0].thread_id == "42"
    save_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_broadcast_shutdown_nulls_manager_route_and_notifies_pending_waiters(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "manager_live_4321"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    state_store = GatewayStateStore(str(tmp_path / "state.db"))
    gateway = UserGateway(config, output_sink=None, state_store=state_store)
    pending_queue: asyncio.Queue[MessageEnvelope] = asyncio.Queue()
    gateway._manager_client.register_response_channel(
        "corr_id_waiting",
        queue=pending_queue,
    )
    state_store.record_request(
        correlation_id="corr_id_waiting",
        session_key="session_1",
        request_kind="interactive",
        request_text="Hello?",
        conversation_id="conv_1",
        followup_to_task_id=None,
    )
    envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="manager_live_4321",
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=True,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_live_4321",
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_presence(envelope)

    assert gateway.config.manager.manager_agent_id is None
    save_mock.assert_called_once()
    queued = await pending_queue.get()
    assert isinstance(queued.msg_content, SystemStatusMessage)
    assert queued.msg_content.system_status == _unavailable_notice(config)
    request = state_store.get_request("corr_id_waiting")
    assert request is not None
    assert request.status == "manager_unavailable"


@pytest.mark.asyncio
async def test_send_user_message_returns_standard_notification_when_manager_route_is_null(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    state_store = GatewayStateStore(str(tmp_path / "state.db"))
    gateway = UserGateway(config, output_sink=None, state_store=state_store)

    response = await gateway.application.send_user_message("Are you there?")

    assert response.message_type == "system_status"
    assert response.text == _unavailable_notice(config)
    request = state_store.get_request(response.correlation_id)
    assert request is not None
    assert request.status == "manager_unavailable"


@pytest.mark.asyncio
async def test_gateway_liveness_probe_marks_manager_unavailable_when_consumer_disappears(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "manager_live_4321"
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "-100123"
    config.delivery.default_target.thread_id = "42"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    state_store = GatewayStateStore(str(tmp_path / "state.db"))
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=state_store,
    )
    gateway.manager_transitions.enable_notices()
    manager_runtime = _FakeGatewayManagerRuntime(has_live_consumer=False)
    gateway._manager_client.set_runtime(
        manager_runtime,
        use_for_outbound=False,
    )
    pending_queue: asyncio.Queue[MessageEnvelope] = asyncio.Queue()
    gateway._manager_client.register_response_channel(
        "corr_id_waiting",
        queue=pending_queue,
    )
    state_store.record_request(
        correlation_id="corr_id_waiting",
        session_key="session_1",
        request_kind="interactive",
        request_text="Hello?",
        conversation_id="conv_1",
        followup_to_task_id=None,
    )

    await gateway._manager_client.check_manager_liveness_once()

    assert gateway.config.manager.manager_agent_id is None
    save_mock.assert_called_once()
    queued = await pending_queue.get()
    assert isinstance(queued.msg_content, SystemStatusMessage)
    assert queued.msg_content.system_status == _unavailable_notice(config)
    assert len(delivered_events) == 1
    assert delivered_events[0].text == _telegram_transition_unavailable_notice(config)
    assert delivered_events[0].channel_name == "telegram"
    assert delivered_events[0].chat_id == "-100123"
    assert delivered_events[0].thread_id == "42"
    assert manager_runtime.probes == ["manager_live_4321"]
    request = state_store.get_request("corr_id_waiting")
    assert request is not None
    assert request.status == "manager_unavailable"


@pytest.mark.asyncio
async def test_gateway_unavailable_transition_runs_hook_after_notice(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "manager_live_4321"
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "-100123"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    events: list[str] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: events.append(f"notice:{event.text}"),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.manager_transitions.enable_notices()
    gateway.manager_transitions.set_hooks(
        on_manager_unavailable=lambda: events.append("hook:pause"),
    )

    await gateway._manager_client.mark_manager_unavailable(reason="test transition")

    assert events == [
        f"notice:{_telegram_transition_unavailable_notice(config)}",
        "hook:pause",
    ]
    save_mock.assert_called_once()


@pytest.mark.parametrize(
    ("channel_name", "chat_id", "thread_id", "unavailable_notice", "available_notice"),
    [
        ("cli", "", None, _unavailable_notice, _available_notice),
        ("discord", "12345", "67890", _unavailable_notice, _available_notice),
        (
            "telegram",
            "-100123",
            "42",
            _telegram_transition_unavailable_notice,
            _telegram_transition_available_notice,
        ),
        ("whatsapp", "+15551234567", None, _unavailable_notice, _available_notice),
    ],
)
@pytest.mark.asyncio
async def test_gateway_manager_transition_notices_use_preferred_delivery_target(
    tmp_path: Path,
    monkeypatch,
    channel_name: str,
    chat_id: str,
    thread_id: str | None,
    unavailable_notice,
    available_notice,
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "manager_live_4321"
    config.delivery.default_target.channel_name = channel_name
    config.delivery.default_target.chat_id = chat_id
    config.delivery.default_target.thread_id = thread_id or ""
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.manager_transitions.enable_notices()

    await gateway._manager_client.mark_manager_unavailable(reason="test transition")

    expected_chat_id = (
        chat_id if channel_name != "cli" else f"cli:{config.instance_name}"
    )
    expected_thread_id = thread_id if channel_name != "cli" else None
    assert gateway.config.manager.manager_agent_id is None
    assert len(delivered_events) == 1
    assert delivered_events[0].text == unavailable_notice(config)
    assert delivered_events[0].channel_name == channel_name
    assert delivered_events[0].chat_id == expected_chat_id
    assert delivered_events[0].thread_id == expected_thread_id

    delivered_events.clear()
    gateway._started = True
    gateway.manager_transitions.start()
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]

    await gateway._manager_client.handle_presence(_manager_status_envelope(config))

    assert gateway.config.manager.manager_agent_id == "manager_live_4321"
    assert len(delivered_events) == 1
    assert delivered_events[0].text == available_notice(config)
    assert delivered_events[0].channel_name == channel_name
    assert delivered_events[0].chat_id == expected_chat_id
    assert delivered_events[0].thread_id == expected_thread_id


@pytest.mark.asyncio
async def test_gateway_unavailable_transition_uses_active_telegram_session_when_preferred_chat_missing(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "manager_live_4321"
    config.interactive_apps.telegram.enabled = True
    config.interactive_apps.telegram.manager_offline_policy = "pause_telegram_polling"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.manager_transitions.enable_notices()
    telegram_session = gateway.application.start_new_session(
        channel_name="telegram",
        user_id="user_1",
        chat_id="-100123",
        thread_id="42",
    )

    await gateway._manager_client.mark_manager_unavailable(reason="test transition")

    assert gateway.config.manager.manager_agent_id is None
    assert len(delivered_events) == 1
    assert delivered_events[0].text == _telegram_transition_unavailable_notice(config)
    assert delivered_events[0].channel_name == "telegram"
    assert delivered_events[0].chat_id == "-100123"
    assert delivered_events[0].thread_id == "42"
    assert delivered_events[0].session_key == telegram_session.session_key
    save_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_available_transition_runs_hook_after_notice(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "-100123"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    events: list[str] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: events.append(f"notice:{event.text}"),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._started = True
    gateway.manager_transitions.start()
    gateway.manager_transitions.enable_notices()
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]
    gateway.manager_transitions.set_hooks(
        on_manager_available=lambda: events.append("hook:resume"),
    )
    envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="manager_live_4321",
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=False,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_live_4321",
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_presence(envelope)

    assert events == [
        f"notice:{_telegram_transition_available_notice(config)}",
        "hook:resume",
    ]
    save_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_available_transition_uses_active_telegram_session_when_preferred_chat_missing(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    config.interactive_apps.telegram.enabled = True
    config.interactive_apps.telegram.manager_offline_policy = "pause_telegram_polling"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._started = True
    gateway.manager_transitions.start()
    gateway.manager_transitions.enable_notices()
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]
    telegram_session = gateway.application.start_new_session(
        channel_name="telegram",
        user_id="user_1",
        chat_id="-100123",
        thread_id="42",
    )
    envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="manager_live_4321",
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=False,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_live_4321",
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_presence(envelope)

    assert gateway.config.manager.manager_agent_id == "manager_live_4321"
    assert len(delivered_events) == 1
    assert delivered_events[0].text == _telegram_transition_available_notice(config)
    assert delivered_events[0].channel_name == "telegram"
    assert delivered_events[0].chat_id == "-100123"
    assert delivered_events[0].thread_id == "42"
    assert delivered_events[0].session_key == telegram_session.session_key
    save_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_unavailable_transition_uses_active_discord_session_when_preferred_chat_missing(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "manager_live_4321"
    config.interactive_apps.discord.enabled = True
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.manager_transitions.enable_notices()
    discord_session = gateway.application.start_new_session(
        channel_name="discord",
        user_id="user_1",
        chat_id="12345",
        thread_id="67890",
    )

    await gateway._manager_client.mark_manager_unavailable(reason="test transition")

    assert gateway.config.manager.manager_agent_id is None
    assert len(delivered_events) == 1
    assert delivered_events[0].text == _unavailable_notice(config)
    assert delivered_events[0].channel_name == "discord"
    assert delivered_events[0].chat_id == "12345"
    assert delivered_events[0].thread_id == "67890"
    assert delivered_events[0].session_key == discord_session.session_key
    save_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_available_transition_uses_active_discord_session_when_preferred_chat_missing(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    config.interactive_apps.discord.enabled = True
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._started = True
    gateway.manager_transitions.start()
    gateway.manager_transitions.enable_notices()
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]
    discord_session = gateway.application.start_new_session(
        channel_name="discord",
        user_id="user_1",
        chat_id="12345",
        thread_id="67890",
    )
    envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="manager_live_4321",
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=False,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_live_4321",
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_presence(envelope)

    assert gateway.config.manager.manager_agent_id == "manager_live_4321"
    assert len(delivered_events) == 1
    assert delivered_events[0].text == _available_notice(config)
    assert delivered_events[0].channel_name == "discord"
    assert delivered_events[0].chat_id == "12345"
    assert delivered_events[0].thread_id == "67890"
    assert delivered_events[0].session_key == discord_session.session_key
    save_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_unavailable_transition_uses_active_whatsapp_session_when_preferred_chat_missing(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "manager_live_4321"
    config.interactive_apps.whatsapp.enabled = True
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway.manager_transitions.enable_notices()
    whatsapp_session = gateway.application.start_new_session(
        channel_name="whatsapp",
        user_id="+15551234567",
        chat_id="+15551234567",
        thread_id=None,
    )

    await gateway._manager_client.mark_manager_unavailable(reason="test transition")

    assert gateway.config.manager.manager_agent_id is None
    assert len(delivered_events) == 1
    assert delivered_events[0].text == _unavailable_notice(config)
    assert delivered_events[0].channel_name == "whatsapp"
    assert delivered_events[0].chat_id == "+15551234567"
    assert delivered_events[0].thread_id is None
    assert delivered_events[0].session_key == whatsapp_session.session_key
    save_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_available_transition_uses_active_whatsapp_session_when_preferred_chat_missing(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    config.interactive_apps.whatsapp.enabled = True
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._started = True
    gateway.manager_transitions.start()
    gateway.manager_transitions.enable_notices()
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]
    whatsapp_session = gateway.application.start_new_session(
        channel_name="whatsapp",
        user_id="+15551234567",
        chat_id="+15551234567",
        thread_id=None,
    )
    envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="manager_live_4321",
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=False,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_live_4321",
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_presence(envelope)

    assert gateway.config.manager.manager_agent_id == "manager_live_4321"
    assert len(delivered_events) == 1
    assert delivered_events[0].text == _available_notice(config)
    assert delivered_events[0].channel_name == "whatsapp"
    assert delivered_events[0].chat_id == "+15551234567"
    assert delivered_events[0].thread_id is None
    assert delivered_events[0].session_key == whatsapp_session.session_key
    save_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_unavailable_notice_renders_assistant_name_from_template(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.assistant.display_name = "Nova"
    config.assistant.unavailable_message_template = "{assistant_name} is not currently available. Please try again after it reconnects."
    config.manager.manager_agent_id = None
    state_store = GatewayStateStore(str(tmp_path / "state.db"))
    gateway = UserGateway(config, output_sink=None, state_store=state_store)

    response = await gateway.application.send_user_message("Are you there?")

    assert response.message_type == "system_status"
    assert (
        response.text
        == "Nova is not currently available. Please try again after it reconnects."
    )


@pytest.mark.asyncio
async def test_gateway_available_notice_renders_assistant_name_from_template(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.assistant.display_name = "Nova"
    config.assistant.available_message_template = "{assistant_name} is now available."
    config.manager.manager_agent_id = None
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._started = True
    gateway.manager_transitions.start()
    gateway.manager_transitions.enable_notices()
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]
    envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="manager_live_4321",
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=False,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_live_4321",
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_presence(envelope)

    assert len(delivered_events) == 1
    assert delivered_events[0].text == "Nova is now available."


@pytest.mark.asyncio
async def test_gateway_emit_proactive_notice_uses_preferred_delivery_target(
    tmp_path: Path,
):
    config = _build_test_config(tmp_path)
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "-100123"
    config.delivery.default_target.thread_id = "42"
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    await gateway.proactive_delivery.emit_notice(
        "Headless startup migrated instance_config.yaml."
    )

    assert len(delivered_events) == 1
    assert delivered_events[0].text == "Headless startup migrated instance_config.yaml."
    assert delivered_events[0].channel_name == "telegram"
    assert delivered_events[0].chat_id == "-100123"
    assert delivered_events[0].thread_id == "42"


@pytest.mark.asyncio
async def test_gateway_delivers_plan_progress_activity_notice_once(tmp_path: Path):
    config = _build_test_config(tmp_path)
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "-100123"
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )

    delivered = await gateway._activity_notices.deliver(
        [
            {
                "activity_event_id": "evt-plan-1",
                "activity_type": "plan_updated",
                "stage": "planning",
                "summary": "Plan milestone completed: inspect runtime path",
                "detail": "Internal details are not required for the proactive notice.",
                "user_visible": True,
                "details": {"notice_type": "plan_progress"},
            },
            {
                "activity_event_id": "evt-internal",
                "activity_type": "plan_updated",
                "stage": "planning",
                "summary": "Internal plan action",
                "user_visible": False,
                "details": {"notice_type": "plan_progress"},
            },
        ]
    )
    duplicate_delivered = await gateway._activity_notices.deliver(
        [
            {
                "activity_event_id": "evt-plan-1",
                "activity_type": "plan_updated",
                "stage": "planning",
                "summary": "Plan milestone completed: inspect runtime path",
                "user_visible": True,
                "details": {"notice_type": "plan_progress"},
            }
        ]
    )

    assert delivered == 1
    assert duplicate_delivered == 0
    assert len(delivered_events) == 1
    assert delivered_events[0].text == "Plan milestone completed: inspect runtime path"
    assert delivered_events[0].channel_name == "telegram"
    assert delivered_events[0].chat_id == "-100123"


@pytest.mark.asyncio
async def test_gateway_startup_broadcast_replay_updates_route_without_emitting_notices(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    config.delivery.default_target.channel_name = "telegram"
    config.delivery.default_target.chat_id = "-100123"
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._started = True
    gateway.manager_transitions.start()
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]

    live_envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="manager_live_4321",
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=False,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_live_4321",
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )
    exit_envelope = MessageEnvelope(
        msg_content=AgentStatus(
            self_id="manager_live_4321",
            team_id=config.manager.team_id,
            named_agent_type=config.manager.named_agent_type,
            is_paused=False,
            is_executing=False,
            has_execution_capacity=True,
            is_busy=False,
            is_exiting=True,
            name="manager",
            agent_role="manager",
            last_seen_at=datetime.now(timezone.utc).isoformat(),
            status_type=MessageMetaType.RESPONSE,
            info_type=ControlType.STATUS_REQUEST_BASIC,
        ),
        msg_meta=MessageMeta(),
        msg_header=MessageHeader(
            from_agent_id="manager_live_4321",
            to_routing_path="++<all>++",
            agent_signature="",
        ),
    )

    await gateway._manager_client.handle_presence(live_envelope)
    await gateway._manager_client.handle_presence(exit_envelope)

    assert gateway.config.manager.manager_agent_id is None
    assert delivered_events == []
    gateway._maintenance_schedules.ensure_all.assert_awaited_once()
    assert save_mock.call_count == 2


@pytest.mark.asyncio
async def test_gateway_liveness_probe_keeps_live_manager_route_when_consumer_exists(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "manager_live_4321"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    state_store = GatewayStateStore(str(tmp_path / "state.db"))
    gateway = UserGateway(config, output_sink=None, state_store=state_store)
    manager_runtime = _FakeGatewayManagerRuntime(has_live_consumer=True)
    gateway._manager_client.set_runtime(
        manager_runtime,
        use_for_outbound=False,
    )

    await gateway._manager_client.check_manager_liveness_once()

    assert gateway.config.manager.manager_agent_id == "manager_live_4321"
    save_mock.assert_not_called()
    assert manager_runtime.probes == ["manager_live_4321"]


@pytest.mark.asyncio
async def test_gateway_liveness_probe_keeps_manager_route_when_presence_is_recent(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "manager_live_4321"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._manager_client.set_runtime(
        _FakeGatewayManagerRuntime(has_live_consumer=None),
        use_for_outbound=False,
    )
    gateway._manager_client.manager_status_updated_at = datetime.now(
        timezone.utc
    ) - timedelta(seconds=29)

    await gateway._manager_client.check_manager_liveness_once()

    assert gateway.config.manager.manager_agent_id == "manager_live_4321"
    save_mock.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_liveness_probe_marks_manager_unavailable_when_presence_is_stale(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = "manager_live_4321"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._manager_client.set_runtime(
        _FakeGatewayManagerRuntime(has_live_consumer=None),
        use_for_outbound=False,
    )
    gateway._manager_client.manager_status_updated_at = datetime.now(
        timezone.utc
    ) - timedelta(seconds=31)

    await gateway._manager_client.check_manager_liveness_once()

    assert gateway.config.manager.manager_agent_id is None
    save_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_liveness_probe_rediscovers_manager_and_sends_recovery_notice(
    tmp_path: Path, monkeypatch
):
    config = _build_test_config(tmp_path)
    config.manager.manager_agent_id = None
    config.interactive_apps.telegram.enabled = True
    config.interactive_apps.telegram.manager_offline_policy = "pause_telegram_polling"
    save_mock = Mock()
    monkeypatch.setattr(type(config), "save", lambda self: save_mock())

    events: list[str] = []
    delivered_events: list[gateway_module.GatewayDeliveryEvent] = []
    gateway = UserGateway(
        config,
        output_sink=None,
        event_sink=lambda event: delivered_events.append(event),
        state_store=GatewayStateStore(str(tmp_path / "state.db")),
    )
    gateway._started = True
    gateway.manager_transitions.start()
    gateway.manager_transitions.enable_notices()
    gateway._manager_client.set_runtime(
        _FakeGatewayManagerRuntime(has_live_consumer=None),
        use_for_outbound=False,
    )
    gateway._maintenance_schedules.ensure_all = AsyncMock()  # type: ignore[method-assign]
    gateway._manager_client.discover_manager_id = AsyncMock(
        return_value="manager_live_4321"
    )
    gateway.manager_transitions.set_hooks(
        on_manager_available=lambda: events.append("hook:resume"),
    )
    telegram_session = gateway.application.start_new_session(
        channel_name="telegram",
        user_id="user_1",
        chat_id="-100123",
        thread_id="42",
    )

    await gateway._manager_client.check_manager_liveness_once()

    assert gateway.config.manager.manager_agent_id == "manager_live_4321"
    gateway._manager_client.discover_manager_id.assert_awaited_once_with(
        timeout_seconds=gateway_module.MANAGER_RECOVERY_DISCOVERY_TIMEOUT_SECONDS
    )
    gateway._maintenance_schedules.ensure_all.assert_awaited_once()
    save_mock.assert_called_once()
    assert len(delivered_events) == 1
    assert delivered_events[0].text == _telegram_transition_available_notice(config)
    assert delivered_events[0].channel_name == "telegram"
    assert delivered_events[0].chat_id == "-100123"
    assert delivered_events[0].thread_id == "42"
    assert delivered_events[0].session_key == telegram_session.session_key
    assert events == ["hook:resume"]


@pytest.mark.asyncio
async def test_web_chat_keeps_the_credential_form_closing_line_under_the_forms_own_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _build_test_config(tmp_path)
    gateway = UserGateway(config, output_sink=None, state_store=GatewayStateStore(str(tmp_path / "state.db")))
    owner = config.user.canonical_user_id
    gateway.state_store.create_session(
        session_key="session_1",
        conversation_id="conv_1",
        manager_agent_id=config.manager.manager_agent_id,
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        owner_canonical_user_id=owner,
    )
    gateway._manager_client.publish_durable = AsyncMock()
    request_content = SecureCredentialCollectionRequest(
        service_name="Bifrost", credential_key="bifrost_virtual_key", purpose="Store it.", parent_task_id="task_1"
    )

    await _watch_credential_form(gateway, monkeypatch, request_content, "credential_stored")

    page = gateway._transcript_service.history_page(
        owner_canonical_user_id=owner, session_key="session_1", before_cursor=None
    )
    [line] = [message for message in page.messages if message.message_type == "secure_credential_result"]
    assert (line.correlation_id, line.role) == (request_content.credential_request_id, "system")
    assert line.content == "The secure form for `bifrost_virtual_key` is done: the credential was stored."

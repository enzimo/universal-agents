# Direct execution bootstraps the repository path before project imports.
# ruff: noqa: E402

import argparse
import asyncio
import contextlib
import json
import logging
import shutil
import socket
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import uuid
import yaml
from aiohttp import ClientSession, ClientTimeout, CookieJar

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from assistant_cli import (
    CliMessageTracker,
    INTERACTIVE_SESSION_HELP,
    _handle_local_session_command,
    _parse_reply_command,
)
from assistant_gateway.bootstrap import bootstrap_instance
from assistant_gateway.channel_bridge import ChannelBridge
from assistant_gateway.config import GatewayInstanceConfig
from runtime_settings.interactive_apps import InteractiveAppPluginConfiguration
from tests.application_test_doubles import (
    application_from as GatewayApplicationFacade,
)
from assistant_gateway.gateway import GatewayDeliveryEvent, UserGateway
from assistant_gateway.inbound_attachments import store_inbound_attachment_bytes
from assistant_gateway.interactive_apps.base import BaseInteractiveApp
from assistant_gateway.interactive_apps.contracts import (
    INTERACTIVE_APP_PLUGIN_API_VERSION,
    InteractiveAppCapability,
    InteractiveAppDependencyReport,
    InteractiveAppDescriptor,
    InteractiveAppLifecycleState,
    InteractiveAppOnboardingMetadata,
    InteractiveAppPackageProvenance,
    InteractiveAppPluginOrigin,
)
from assistant_gateway.interactive_apps.manager import InteractiveAppManager
from assistant_gateway.interactive_apps.registry import InteractiveAppRegistry
from assistant_gateway.translation import ChannelInboundMessage
from custom_agents.conversation_store import ConversationStore
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.control_v1 import ControlInstruction, Instruction
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import ControlType, RoutingType
from config_loader import load_config
from live_db_fixtures import provision_db, suite_default_db_path
from live_storage_utils import (
    apply_storage_env,
    resolve_live_storage_settings,
    reset_postgres_storage,
    storage_identifier,
)
from storage.config import StorageSettings
from storage.postgres_connection import connect_postgres
import live_nats_client
from id_generation import new_id, new_id_suffix


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

TEST_RESULTS_ROOT = PROJECT_ROOT / "test_results" / "gateway_live"
LIVE_BRIDGE_TELEGRAM_USER_ID = "user_1"
MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS = 150
AUTOMATION_QUIET_DIGEST_EXPECTED_TEXT = (
    "quiet hours digest follow-up scheduled for later"
)
AUTOMATION_URGENT_OVERRIDE_EXPECTED_TEXT = (
    "urgent: critical outage detected, investigate immediately"
)
AUTOMATION_DIGEST_RELEASE_EXPECTED_TEXT = (
    "quiet hours digest release ready for tomorrow"
)


class _LiveRegistryProbeApp(BaseInteractiveApp):
    """Dependency-free live adapter used to exercise the plugin host boundary."""

    channel_name = "telegram"

    def __init__(self, context) -> None:
        self.application = context.application
        self.bridge = context.bridge
        self.inbound_outputs: list[str] = []
        self.delivered_events: list[str] = []
        self.running = False

    @property
    def enabled(self) -> bool:
        return True

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def deliver_event(self, event: GatewayDeliveryEvent) -> None:
        if not self.running:
            raise RuntimeError("Live registry probe is not running.")
        self.delivered_events.append(event.text_with_attachment_paths())

    async def emit_text_activity_update(self, _context, _text: str) -> None:
        return None

    async def handle_inbound(self, inbound: ChannelInboundMessage) -> list[str]:
        if not self.running:
            raise RuntimeError("Live registry probe is not running.")
        messages = await self.bridge.handle_inbound(inbound)
        rendered = [message.text for message in messages]
        self.inbound_outputs.extend(rendered)
        return rendered


def _configure_live_bridge_user(config: GatewayInstanceConfig) -> None:
    """Seed the bridge identity through the normal legacy allowlist migration."""

    config.interactive_apps.telegram.allowed_user_ids = [LIVE_BRIDGE_TELEGRAM_USER_ID]


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _prepare_result_dir(test_number: int) -> Path:
    path = TEST_RESULTS_ROOT / f"test_{test_number}"
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_team_id_from_config() -> str:
    cfg = load_config()
    return cfg.get("agent.default_team_id", "team_303")


def _repo_python_executable() -> str:
    repo_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    if repo_python.exists():
        return str(repo_python)
    return sys.executable


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _scenario_manager_db_path(test_number: int) -> str:
    return str(suite_default_db_path("gateway", test_number))


def _resolve_manager_storage_settings(test_number: int) -> StorageSettings:
    return resolve_live_storage_settings(
        sqlite_fallback_path=_scenario_manager_db_path(test_number),
        workspace_dir=PROJECT_ROOT,
    )


def _resolve_gateway_state_storage_settings(
    result_dir: Path, test_number: int
) -> StorageSettings:
    return resolve_live_storage_settings(
        sqlite_fallback_path=result_dir / f"gateway_state_{test_number}.db",
        workspace_dir=PROJECT_ROOT,
    )


def _apply_manager_storage(
    config: GatewayInstanceConfig, test_number: int
) -> StorageSettings:
    settings = _resolve_manager_storage_settings(test_number)
    if settings.backend == "postgres":
        config.state.storage.backend = "postgres"
        config.state.storage.postgres.dsn = settings.postgres_dsn or ""
        config.state.storage.sqlite.path = ""
    else:
        config.state.storage.backend = "sqlite"
        config.state.storage.sqlite.path = settings.sqlite_path or ""
        config.state.storage.postgres.dsn = ""
    return settings


def _apply_state_storage(
    config: GatewayInstanceConfig, result_dir: Path, test_number: int
) -> StorageSettings:
    settings = _resolve_gateway_state_storage_settings(result_dir, test_number)
    if settings.backend == "postgres":
        config.gateway.storage.backend = "postgres"
        config.gateway.storage.postgres.dsn = settings.postgres_dsn or ""
        config.gateway.storage.sqlite.path = ""
    else:
        config.gateway.storage.backend = "sqlite"
        config.gateway.storage.sqlite.path = settings.sqlite_path or ""
        config.gateway.storage.postgres.dsn = ""
    return settings


async def _wait_until(
    predicate, timeout_seconds: float = 60.0, interval_seconds: float = 0.5
):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        result = predicate()
        if result:
            return result
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Timed out waiting for condition.")
        await asyncio.sleep(interval_seconds)


def _serialize_pending_deliveries(records: list[object]) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for item in records:
        serialized.append(
            {
                "delivery_id": getattr(item, "delivery_id", None),
                "session_key": getattr(item, "session_key", None),
                "origin_kind": getattr(item, "origin_kind", None),
                "rendered_text": getattr(item, "rendered_text", None),
                "available_after": getattr(item, "available_after", None),
                "correlation_id": getattr(item, "correlation_id", None),
                "status": getattr(item, "status", None),
            }
        )
    return serialized


def _force_all_pending_deliveries_due(state_storage: StorageSettings) -> str:
    due_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    if state_storage.backend == "postgres":
        with connect_postgres(state_storage.postgres_dsn or "") as conn:
            conn.execute(
                "UPDATE gateway_deliveries SET available_after = %s WHERE status = 'pending'",
                (due_at,),
            )
            conn.commit()
        return due_at

    conn = sqlite3.connect(state_storage.sqlite_path or "")
    try:
        conn.execute(
            "UPDATE gateway_deliveries SET available_after = ? WHERE status = 'pending'",
            (due_at,),
        )
        conn.commit()
    finally:
        conn.close()
    return due_at


async def _wait_for_request_status(
    gateway: UserGateway,
    *,
    correlation_id: str,
    expected_status: str,
    timeout_seconds: float = 120.0,
):
    def _status_matches():
        request = gateway.state_store.get_request(correlation_id)
        if request is None:
            return None
        if request.status != expected_status:
            return None
        return request

    return await _wait_until(_status_matches, timeout_seconds=timeout_seconds)


async def _send_automation_seed_message(gateway: UserGateway, *, expected_phrase: str):
    response = await gateway.send_user_message(f"Reply with exactly {expected_phrase}")
    expected = expected_phrase.lower().strip().rstrip(".")
    actual = response.text.lower().strip().rstrip(".")
    if expected not in actual:
        raise AssertionError(f"Unexpected automation seed response: {response.text}")
    return response


def _write_managed_manager_config(
    result_dir: Path,
    *,
    memory_graph_enabled: bool,
    embedding_provider: str = "local",
    base_config_path: str | Path | None = None,
) -> Path:
    if base_config_path is not None:
        source_config_path = Path(base_config_path).expanduser().resolve()
    else:
        repo_config_path = PROJECT_ROOT / "config.yaml"
        source_config_path = (
            repo_config_path
            if repo_config_path.exists()
            else PROJECT_ROOT / "config.example.yaml"
        )
    if not source_config_path.is_file():
        raise FileNotFoundError(
            f"Managed gateway scenario config was not found: {source_config_path}"
        )
    config = load_config(str(source_config_path)).to_dict()
    agent_cfg = dict(config.get("agent", {}) or {})
    agent_cfg["memory_graph_enabled"] = memory_graph_enabled
    manager_workspace = (result_dir / "manager_workspace").resolve()
    (manager_workspace / "skills").mkdir(parents=True, exist_ok=True)
    agent_cfg["workspace_dir"] = str(manager_workspace)
    config["agent"] = agent_cfg

    # These scenarios exercise manager/gateway behavior without an Activity
    # Collector process. Disable its emitter instead of accepting predictable
    # connection warnings as test noise.
    activity_collector_cfg = dict(config.get("activity_collector", {}) or {})
    activity_collector_cfg["base_url"] = None
    config["activity_collector"] = activity_collector_cfg

    embedding_cfg = dict(config.get("embedding", {}) or {})
    embedding_cfg["provider"] = embedding_provider
    if embedding_provider == "local":
        embedding_cfg.pop("model_id", None)
        embedding_cfg.pop("api_base", None)
        embedding_cfg.pop("api_key", None)
        embedding_cfg.pop("organization", None)
    config["embedding"] = embedding_cfg

    config_path = result_dir / "managed_manager.config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path


class ManagedManagerProcess:
    def __init__(
        self,
        *,
        team_id: str,
        agent_id: str,
        result_dir: Path,
        env_path: str | None,
        storage_settings: StorageSettings,
        agent_result_transformer: str | None = None,
        extra_env: dict[str, str] | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.team_id = team_id
        self.agent_id = agent_id
        self.result_dir = result_dir
        self.env_path = env_path
        self.storage_settings = storage_settings
        self.agent_result_transformer = agent_result_transformer
        self.extra_env = dict(extra_env or {})
        self.config_path = config_path
        safe_name = f"TeamArchitect_{agent_id}".replace(os.sep, "_")
        self.stdout_log_path = result_dir / f"{safe_name}_stdout.log"
        self.stderr_log_path = result_dir / f"{safe_name}_stderr.log"
        self.process: asyncio.subprocess.Process | None = None
        self._stdout_handle = None
        self._stderr_handle = None

    async def start(self) -> None:
        if self.process is not None:
            raise RuntimeError(f"Managed manager {self.agent_id} already started.")
        cmd = [
            _repo_python_executable(),
            "strands_main.py",
            "--agent-type",
            "TeamArchitect",
            "--team-id",
            self.team_id,
            "--agent-id",
            self.agent_id,
            "--is-lead",
        ]
        resolved_config_path = self.config_path or (PROJECT_ROOT / "config.yaml")
        if resolved_config_path.exists():
            cmd.extend(["--config", str(resolved_config_path)])
        if (
            self.storage_settings.backend == "sqlite"
            and self.storage_settings.sqlite_path
        ):
            cmd.extend(["--task-history-db-path", self.storage_settings.sqlite_path])
        if self.env_path:
            cmd.extend(["--env", self.env_path])
        if self.agent_result_transformer:
            cmd.extend(["--agent-result-transformer", self.agent_result_transformer])

        env = apply_storage_env(os.environ.copy(), self.storage_settings)
        env.update(self.extra_env)
        env.setdefault("PYTHONUNBUFFERED", "1")
        self._stdout_handle = self.stdout_log_path.open("w", encoding="utf-8")
        self._stderr_handle = self.stderr_log_path.open("w", encoding="utf-8")
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=self._stdout_handle,
            stderr=self._stderr_handle,
        )

    async def stop(self, timeout_seconds: float = 20.0) -> int | None:
        if self.process is None:
            self._close_handles()
            return None
        if self.process.returncode is not None:
            self._close_handles()
            return self.process.returncode
        self.process.terminate()
        try:
            returncode = await asyncio.wait_for(
                self.process.wait(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            self.process.kill()
            returncode = await self.process.wait()
        self._close_handles()
        return returncode

    async def kill(self, timeout_seconds: float = 20.0) -> int | None:
        if self.process is None:
            self._close_handles()
            return None
        if self.process.returncode is not None:
            self._close_handles()
            return self.process.returncode
        self.process.kill()
        returncode = await asyncio.wait_for(
            self.process.wait(), timeout=timeout_seconds
        )
        self._close_handles()
        return returncode

    async def wait(self, timeout_seconds: float = 20.0) -> int:
        if self.process is None:
            raise RuntimeError("Managed manager process not started.")
        return await asyncio.wait_for(self.process.wait(), timeout=timeout_seconds)

    def snapshot(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "team_id": self.team_id,
            "env_path": self.env_path,
            "agent_result_transformer": self.agent_result_transformer,
            "storage_backend": self.storage_settings.backend,
            "storage_locator": storage_identifier(self.storage_settings),
            "extra_env": self.extra_env,
            "config_path": str(self.config_path)
            if self.config_path is not None
            else None,
            "stdout_log_path": str(self.stdout_log_path),
            "stderr_log_path": str(self.stderr_log_path),
            "returncode": None if self.process is None else self.process.returncode,
        }

    def _close_handles(self) -> None:
        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None and not handle.closed:
                handle.flush()
                handle.close()


async def _send_exit_instruction(
    *,
    team_id: str,
    target_agent_id: str,
) -> None:
    envelope = MessageEnvelope(
        msg_content=ControlInstruction(
            type=ControlType.CONTROL_MSG,
            instruction=Instruction(instruction="exit"),
        ),
        msg_meta=MessageMeta(from_agent_name="live_gateway_scenarios"),
        msg_header=MessageHeader(
            from_agent_id="live_gateway_scenarios",
            to_routing_path=target_agent_id,
            response_routing_key=RoutingType.NULL_ROUTE.value,
            agent_signature="",
        ),
    )
    async with live_nats_client.LiveNatsClient(team_id) as client:
        await client.publish(target_agent_id, envelope, durable=True)


async def _wait_for_manager_route(
    gateway: UserGateway,
    manager: ManagedManagerProcess,
    timeout_seconds: float = 60.0,
) -> None:
    def route_is_ready() -> bool:
        process = manager.process
        if process is not None and process.returncode is not None:
            raise RuntimeError(
                f"Managed manager {manager.agent_id} exited with code "
                f"{process.returncode} before its route became available; "
                f"stdout={manager.stdout_log_path} stderr={manager.stderr_log_path}"
            )
        return gateway.config.manager.manager_agent_id == manager.agent_id

    await _wait_until(
        route_is_ready,
        timeout_seconds=timeout_seconds,
        interval_seconds=0.5,
    )


async def _wait_for_manager_unavailable(
    gateway: UserGateway, timeout_seconds: float = 60.0
) -> None:
    await _wait_until(
        lambda: gateway.config.manager.manager_agent_id is None,
        timeout_seconds=timeout_seconds,
        interval_seconds=0.5,
    )


def _prepare_managed_manager_storage(test_number: int) -> StorageSettings:
    settings = _resolve_manager_storage_settings(test_number)
    if settings.backend == "postgres":
        reset_postgres_storage(settings)
    else:
        provision_db(
            suite="gateway",
            test_id=test_number,
            db_path=Path(settings.sqlite_path or ""),
        )
    return settings


async def _start_managed_manager(
    *,
    team_id: str,
    result_dir: Path,
    env_path: str | None,
    storage_settings: StorageSettings,
    agent_id: str | None = None,
    agent_result_transformer: str | None = None,
    extra_env: dict[str, str] | None = None,
    config_path: Path | None = None,
) -> ManagedManagerProcess:
    manager = ManagedManagerProcess(
        team_id=team_id,
        agent_id=agent_id or f"gateway-manager-{new_id_suffix()}",
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=storage_settings,
        agent_result_transformer=agent_result_transformer,
        extra_env=extra_env,
        config_path=config_path,
    )
    await manager.start()
    return manager


async def run_test_32(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_chat",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
    )
    _apply_manager_storage(config, 32)
    _apply_state_storage(config, result_dir, 32)
    bootstrap_instance(config)

    outputs: list[str] = []
    async with UserGateway(
        config,
        output_sink=lambda text: outputs.append(text),
    ) as gateway:
        response = await gateway.send_user_message("Answer briefly: what is 2 + 2?")
        _write_json(
            result_dir / "result.json",
            {
                "response": response.text,
                "conversation_id": response.conversation_id,
                "task_id": response.task_id,
                "outputs": outputs,
            },
        )
        if "4" not in response.text:
            raise AssertionError(
                f"Expected response to contain 4, got: {response.text}"
            )


async def run_test_33(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_defer",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=True,
        quiet_start_hour_local=0,
        quiet_end_hour_local=0,
    )
    _apply_manager_storage(config, 33)
    _apply_state_storage(config, result_dir, 33)
    bootstrap_instance(config)

    outputs: list[str] = []
    async with UserGateway(
        config,
        output_sink=lambda text: outputs.append(text),
    ) as gateway:
        await gateway.submit_autonomous_task(
            instruction="Reply with the exact phrase autonomous ping complete.",
            session_key="cron",
            origin_kind="cron",
            deliver_quiet_hours=True,
        )
        pending = await _wait_until(
            lambda: gateway.state_store.list_pending_deliveries(),
            timeout_seconds=120.0,
        )
        _write_json(
            result_dir / "result.json",
            {
                "pending_count": len(pending),
                "pending_rendered_texts": [item.rendered_text for item in pending],
                "outputs": outputs,
            },
        )
        if not any(
            "autonomous ping complete" in item.rendered_text.lower() for item in pending
        ):
            raise AssertionError("Expected deferred autonomous reply to be stored.")


async def run_test_34(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_cron",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    _apply_manager_storage(config, 34)
    _apply_state_storage(config, result_dir, 34)
    bootstrap_instance(config)

    outputs: list[str] = []
    async with UserGateway(
        config,
        output_sink=lambda text: outputs.append(text),
    ) as gateway:
        job_id = await gateway.add_cron_job(
            session_key="cron",
            name="live cron",
            instruction="Reply with the exact phrase cron event complete.",
            interval_seconds=2,
        )
        await _wait_until(
            lambda: next(
                (text for text in outputs if "cron event complete" in text.lower()),
                None,
            ),
            timeout_seconds=120.0,
        )
        _write_json(
            result_dir / "result.json",
            {
                "job_id": job_id,
                "outputs": outputs,
            },
        )


async def run_test_35(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_bridge_chat",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    _configure_live_bridge_user(config)
    _apply_manager_storage(config, 35)
    _apply_state_storage(config, result_dir, 35)
    bootstrap_instance(config)

    async with UserGateway(config, output_sink=None) as gateway:
        bridge = ChannelBridge(
            GatewayApplicationFacade(
                gateway,
                command_application=gateway.command_application,
            )
        )
        messages = await bridge.handle_inbound(
            ChannelInboundMessage(
                channel_name="telegram",
                user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                chat_id="chat_1",
                text="Answer briefly with the exact phrase bridge chat ok.",
                session_key="telegram:chat_1",
                message_id="msg_bridge_1",
            )
        )
        _write_json(
            result_dir / "result.json",
            {
                "messages": [message.text for message in messages],
            },
        )
        if not messages or "bridge chat ok" not in messages[0].text.lower():
            raise AssertionError(
                "Expected bridge chat response to contain 'bridge chat ok'."
            )


async def run_test_36(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_bridge_cron",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    _configure_live_bridge_user(config)
    _apply_manager_storage(config, 36)
    _apply_state_storage(config, result_dir, 36)
    bootstrap_instance(config)

    outputs: list[str] = []
    async with UserGateway(
        config,
        output_sink=lambda text: outputs.append(text),
    ) as gateway:
        bridge = ChannelBridge(
            GatewayApplicationFacade(
                gateway,
                command_application=gateway.command_application,
            )
        )
        messages = await bridge.handle_inbound(
            ChannelInboundMessage(
                channel_name="telegram",
                user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                chat_id="chat_1",
                text="remind me every 2 seconds to reply with the exact phrase bridge cron ok.",
                session_key="telegram:chat_1",
                message_id="msg_bridge_2",
            )
        )
        await _wait_until(
            lambda: next(
                (text for text in outputs if "bridge cron ok" in text.lower()), None
            ),
            timeout_seconds=120.0,
        )
        _write_json(
            result_dir / "result.json",
            {
                "messages": [message.text for message in messages],
                "outputs": outputs,
            },
        )
        if not messages or "Scheduled reminder created" not in messages[0].text:
            raise AssertionError("Expected bridge cron creation acknowledgement.")


async def run_test_37(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_bridge_new_session",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    _configure_live_bridge_user(config)
    _apply_manager_storage(config, 37)
    _apply_state_storage(config, result_dir, 37)
    bootstrap_instance(config)

    async with UserGateway(config, output_sink=None) as gateway:
        bridge = ChannelBridge(
            GatewayApplicationFacade(
                gateway,
                command_application=gateway.command_application,
            )
        )

        first_new = await bridge.handle_inbound(
            ChannelInboundMessage(
                channel_name="telegram",
                user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                chat_id="chat_new",
                text="/new",
                session_key=None,
                message_id="msg_new_1",
            )
        )
        session_after_first_new = gateway.state_store.get_active_session_for_channel(
            channel_name="telegram",
            user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
            chat_id="chat_new",
        )
        if session_after_first_new is None:
            raise AssertionError("Expected /new to create an active session.")

        first_response = await bridge.handle_inbound(
            ChannelInboundMessage(
                channel_name="telegram",
                user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                chat_id="chat_new",
                text="Reply with the exact phrase first session ok.",
                session_key=None,
                message_id="msg_new_2",
            )
        )

        second_new = await bridge.handle_inbound(
            ChannelInboundMessage(
                channel_name="telegram",
                user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                chat_id="chat_new",
                text="/new",
                session_key=None,
                message_id="msg_new_3",
            )
        )
        session_after_second_new = gateway.state_store.get_active_session_for_channel(
            channel_name="telegram",
            user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
            chat_id="chat_new",
        )
        if session_after_second_new is None:
            raise AssertionError("Expected second /new to keep an active session.")
        if session_after_first_new.session_key == session_after_second_new.session_key:
            raise AssertionError(
                "Expected second /new to rotate to a fresh session_key."
            )
        if (
            session_after_first_new.conversation_id
            == session_after_second_new.conversation_id
        ):
            raise AssertionError(
                "Expected second /new to rotate to a fresh conversation_id."
            )

        second_response = await bridge.handle_inbound(
            ChannelInboundMessage(
                channel_name="telegram",
                user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                chat_id="chat_new",
                text="Reply with the exact phrase second session ok.",
                session_key=None,
                message_id="msg_new_4",
            )
        )

        _write_json(
            result_dir / "result.json",
            {
                "first_new": [message.text for message in first_new],
                "second_new": [message.text for message in second_new],
                "first_session_key": session_after_first_new.session_key,
                "first_conversation_id": session_after_first_new.conversation_id,
                "second_session_key": session_after_second_new.session_key,
                "second_conversation_id": session_after_second_new.conversation_id,
                "first_response": [message.text for message in first_response],
                "second_response": [message.text for message in second_response],
            },
        )

        if "Started new session:" not in first_new[0].text:
            raise AssertionError("Expected first /new acknowledgement.")
        if "Started new session:" not in second_new[0].text:
            raise AssertionError("Expected second /new acknowledgement.")
        if "first session ok" not in first_response[0].text.lower():
            raise AssertionError("Expected first session reply.")
        if "second session ok" not in second_response[0].text.lower():
            raise AssertionError("Expected second session reply.")


async def run_test_38(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_bridge_routing",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    _apply_manager_storage(config, 38)
    _apply_state_storage(config, result_dir, 38)
    bootstrap_instance(config)

    events: list[dict] = []

    def capture_event(event) -> None:
        events.append(
            {
                "text": event.text,
                "session_key": event.session_key,
                "channel_name": event.channel_name,
                "user_id": event.user_id,
                "chat_id": event.chat_id,
                "conversation_id": event.conversation_id,
                "scheduled_job_id": event.scheduled_job_id,
                "correlation_id": event.correlation_id,
            }
        )

    async with UserGateway(
        config,
        output_sink=None,
        event_sink=capture_event,
    ) as gateway:
        bridge = ChannelBridge(
            GatewayApplicationFacade(
                gateway,
                command_application=gateway.command_application,
            )
        )

        await bridge.handle_inbound(
            ChannelInboundMessage(
                channel_name="telegram",
                user_id="user_1",
                chat_id="chat_alpha",
                text="remind me every 2 seconds to reply with the exact phrase alpha route ok.",
                session_key=None,
                message_id="msg_route_1",
            )
        )
        await bridge.handle_inbound(
            ChannelInboundMessage(
                channel_name="telegram",
                user_id="user_1",
                chat_id="chat_beta",
                text="remind me every 2 seconds to reply with the exact phrase beta route ok.",
                session_key=None,
                message_id="msg_route_2",
            )
        )

        alpha_event = await _wait_until(
            lambda: next(
                (
                    event
                    for event in events
                    if "alpha route ok" in event["text"].lower()
                ),
                None,
            ),
            timeout_seconds=120.0,
        )
        beta_event = await _wait_until(
            lambda: next(
                (event for event in events if "beta route ok" in event["text"].lower()),
                None,
            ),
            timeout_seconds=120.0,
        )

        _write_json(
            result_dir / "result.json",
            {
                "events": events,
                "alpha_event": alpha_event,
                "beta_event": beta_event,
            },
        )

        if alpha_event["chat_id"] != "chat_alpha":
            raise AssertionError(
                f"Expected alpha scheduled reply on chat_alpha, got {alpha_event['chat_id']}"
            )
        if beta_event["chat_id"] != "chat_beta":
            raise AssertionError(
                f"Expected beta scheduled reply on chat_beta, got {beta_event['chat_id']}"
            )
        if alpha_event["session_key"] == beta_event["session_key"]:
            raise AssertionError(
                "Expected scheduled replies for different chats to use different session keys."
            )
        if not alpha_event["scheduled_job_id"] or not beta_event["scheduled_job_id"]:
            raise AssertionError(
                "Expected scheduled replies to carry scheduled_job_id."
            )


async def run_test_39(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_cli_sessions",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    _apply_manager_storage(config, 39)
    _apply_state_storage(config, result_dir, 39)
    bootstrap_instance(config)

    async with UserGateway(config, output_sink=None) as gateway:
        current_session_key = gateway.resolve_session(
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_cli",
        ).session_key

        handled, same_session_key, help_text = await _handle_local_session_command(
            raw_text="/help",
            gateway=gateway,
            current_session_key=current_session_key,
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_cli",
        )
        if (
            not handled
            or INTERACTIVE_SESSION_HELP not in help_text
            or "Documentation:" not in help_text
        ):
            raise AssertionError("Expected /help to return the interactive help text.")
        current_session_key = same_session_key or current_session_key

        first_response = await gateway.send_user_message(
            "Reply with the exact phrase session one ok.",
            session_key=current_session_key,
        )

        (
            handled,
            current_session_key,
            rename_message,
        ) = await _handle_local_session_command(
            raw_text="/rename_session general",
            gateway=gateway,
            current_session_key=current_session_key,
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_cli",
        )
        if (
            not handled
            or rename_message != f"Session {current_session_key} aliased as: general"
        ):
            raise AssertionError(
                "Expected /rename_session to alias the current session."
            )

        extra_sessions: list[str] = []
        for _ in range(8):
            handled, current_session_key, _ = await _handle_local_session_command(
                raw_text="/new",
                gateway=gateway,
                current_session_key=current_session_key,
                channel_name="telegram",
                user_id="user_1",
                chat_id="chat_cli",
            )
            if not handled or current_session_key is None:
                raise AssertionError("Expected /new to create an additional session.")
            extra_sessions.append(current_session_key)

        handled, _, list_default = await _handle_local_session_command(
            raw_text="/list_sessions",
            gateway=gateway,
            current_session_key=current_session_key,
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_cli",
        )
        if not handled or "Sessions 1-7 of 9:" not in list_default:
            raise AssertionError(
                "Expected default /list_sessions view with total count and alias preview."
            )

        handled, _, list_range = await _handle_local_session_command(
            raw_text="/list_sessions 8-9",
            gateway=gateway,
            current_session_key=current_session_key,
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_cli",
        )
        if (
            not handled
            or "Sessions 8-9 of 9:" not in list_range
            or "(general)" not in list_range
        ):
            raise AssertionError("Expected ranged /list_sessions output.")

        handled, _, preview_text = await _handle_local_session_command(
            raw_text="/preview_session general",
            gateway=gateway,
            current_session_key=current_session_key,
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_cli",
        )
        if (
            not handled
            or "Alias: general" not in preview_text
            or "session one ok" not in preview_text.lower()
        ):
            raise AssertionError("Expected alias-aware preview output.")

        handled, _, search_text = await _handle_local_session_command(
            raw_text="/search_session general",
            gateway=gateway,
            current_session_key=current_session_key,
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_cli",
        )
        if not handled or "general" not in search_text:
            raise AssertionError("Expected alias-aware session search output.")

        (
            handled,
            current_session_key,
            switch_message,
        ) = await _handle_local_session_command(
            raw_text="/switch_session general",
            gateway=gateway,
            current_session_key=current_session_key,
            channel_name="telegram",
            user_id="user_1",
            chat_id="chat_cli",
        )
        expected_switch_message = (
            f"Switched to session: {same_session_key}\n"
            f"Active conversation: {first_response.conversation_id}"
        )
        if not handled or switch_message != expected_switch_message:
            raise AssertionError(
                "Expected alias-based switch back to the original session."
            )

        resumed_response = await gateway.send_user_message(
            "Reply with the exact phrase session resumed ok.",
            session_key=current_session_key,
        )
        if resumed_response.conversation_id != first_response.conversation_id:
            raise AssertionError(
                "Expected switched session to resume the original conversation_id."
            )

        _write_json(
            result_dir / "result.json",
            {
                "help_text": help_text,
                "renamed_session_key": same_session_key,
                "rename_message": rename_message,
                "list_default": list_default,
                "list_range": list_range,
                "preview_text": preview_text,
                "search_text": search_text,
                "switch_message": switch_message,
                "first_conversation_id": first_response.conversation_id,
                "resumed_conversation_id": resumed_response.conversation_id,
                "extra_sessions": extra_sessions,
            },
        )


async def run_test_40(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_heartbeat_tools",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=True,
        heartbeat_interval_seconds=3600,
        quiet_hours_enabled=False,
    )
    manager_storage_settings = _apply_manager_storage(config, 40)
    _apply_state_storage(config, result_dir, 40)
    bootstrap_instance(config)
    heartbeat_store = HeartbeatStore(
        db_path="ignored.db", storage_settings=manager_storage_settings
    )
    heartbeat_store.ensure_source(
        source_id=config.heartbeat.source_id, title="Live Heartbeat"
    )
    heartbeat_store.add_item(
        source_id=config.heartbeat.source_id,
        title="Seeded Live",
        instruction="Reply with the exact phrase heartbeat seeded live.",
        conversation_id="conversation_live_heartbeat_seeded",
    )

    async with UserGateway(config, output_sink=None) as gateway:
        initial = await gateway.send_user_message(
            "Use your list_heartbeat_items tool for the current heartbeat source and reply with only the instruction text of the first item."
        )
        create = await gateway.send_user_message(
            "Use your add_heartbeat_item tool to create a heartbeat item titled Live Added with the exact instruction "
            "'Reply with the exact phrase heartbeat item added live.' Reply with only the created heartbeat_item_id."
        )
        heartbeat_item_id = create.text.strip()
        disable = await gateway.send_user_message(
            f"Use your disable_heartbeat_item tool for heartbeat_item_id {heartbeat_item_id}. Reply with exactly disabled."
        )
        source = heartbeat_store.get_source(config.heartbeat.source_id)
        items = heartbeat_store.list_items(config.heartbeat.source_id)
        added_item = heartbeat_store.get_item(heartbeat_item_id)
        _write_json(
            result_dir / "result.json",
            {
                "initial_response": initial.text,
                "create_response": create.text,
                "disable_response": disable.text,
                "heartbeat_item_id": heartbeat_item_id,
                "source": source.__dict__ if source else None,
                "items": [item.__dict__ for item in items],
            },
        )
        if "heartbeat seeded live" not in initial.text.lower():
            raise AssertionError(
                f"Expected seeded heartbeat item in response, got: {initial.text}"
            )
        if not heartbeat_item_id.startswith("heartbeat_item_"):
            raise AssertionError(
                f"Expected heartbeat item id, got: {heartbeat_item_id}"
            )
        if "disabled" not in disable.text.lower():
            raise AssertionError(
                f"Expected disable acknowledgement, got: {disable.text}"
            )
        if source is None:
            raise AssertionError("Expected canonical heartbeat source to exist.")
        if (
            added_item is None
            or added_item.instruction
            != "Reply with the exact phrase heartbeat item added live."
        ):
            raise AssertionError(
                "Expected added heartbeat item to exist with the requested instruction."
            )
        if added_item.enabled:
            raise AssertionError("Expected added heartbeat item to be disabled.")


async def run_test_41(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_heartbeat_schedule",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=True,
        heartbeat_interval_seconds=15,
        quiet_hours_enabled=False,
    )
    manager_storage_settings = _apply_manager_storage(config, 41)
    _apply_state_storage(config, result_dir, 41)
    bootstrap_instance(config)
    heartbeat_store = HeartbeatStore(
        db_path="ignored.db", storage_settings=manager_storage_settings
    )
    heartbeat_store.ensure_source(
        source_id=config.heartbeat.source_id, title="Live Heartbeat"
    )
    heartbeat_store.add_item(
        source_id=config.heartbeat.source_id,
        title="Stocks Live",
        instruction="Reply with the exact phrase heartbeat stocks live.",
        conversation_id="conversation_live_heartbeat_stocks",
    )
    heartbeat_store.add_item(
        source_id=config.heartbeat.source_id,
        title="News Live",
        instruction="Reply with the exact phrase heartbeat news live.",
        conversation_id="conversation_live_heartbeat_news",
    )

    outputs: list[str] = []
    events: list[GatewayDeliveryEvent] = []
    async with UserGateway(
        config,
        output_sink=lambda text: outputs.append(text),
        event_sink=lambda event: events.append(event),
    ):
        await _wait_until(
            lambda: (
                next(
                    (
                        event
                        for event in events
                        if "heartbeat stocks live" in event.text.lower()
                    ),
                    None,
                )
                and next(
                    (
                        event
                        for event in events
                        if "heartbeat news live" in event.text.lower()
                    ),
                    None,
                )
            ),
            timeout_seconds=120.0,
        )
        source = heartbeat_store.get_source(config.heartbeat.source_id)
        items = heartbeat_store.list_items(config.heartbeat.source_id)
        _write_json(
            result_dir / "result.json",
            {
                "outputs": outputs,
                "events": [event.__dict__ for event in events],
                "source": source.__dict__ if source else None,
                "items": [item.__dict__ for item in items],
                "schedule_id": config.heartbeat.schedule_id,
                "source_id": config.heartbeat.source_id,
            },
        )
        if source is None:
            raise AssertionError("Expected canonical heartbeat source to exist.")
        if len(items) != 2:
            raise AssertionError(f"Expected two heartbeat items, got {len(items)}")
        stocks_event = next(
            (
                event
                for event in events
                if "heartbeat stocks live" in event.text.lower()
            ),
            None,
        )
        news_event = next(
            (event for event in events if "heartbeat news live" in event.text.lower()),
            None,
        )
        if stocks_event is None or news_event is None:
            raise AssertionError("Expected both heartbeat item replies to be emitted.")
        if stocks_event.conversation_id == news_event.conversation_id:
            raise AssertionError(
                "Expected heartbeat items to use distinct conversation ids."
            )
        item_conversation_ids = {item.conversation_id for item in items}
        if (
            stocks_event.conversation_id not in item_conversation_ids
            or news_event.conversation_id not in item_conversation_ids
        ):
            raise AssertionError(
                "Expected emitted heartbeat conversations to match canonical heartbeat item conversations."
            )


async def run_test_42(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_manager_schedule_tools",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    manager_storage_settings = _apply_manager_storage(config, 42)
    _apply_state_storage(config, result_dir, 42)
    bootstrap_instance(config)

    outputs: list[str] = []
    async with UserGateway(
        config,
        output_sink=lambda text: outputs.append(text),
    ) as gateway:
        create = await gateway.send_user_message(
            "Create a recurring reminder named live manager reminder that every 20 seconds replies with the exact phrase manager schedule tool ok. "
            "Use your schedule-management tools. After creating it, reply with the schedule id only."
        )
        schedule_id = create.text.strip()
        schedule_store = ScheduleStore(
            db_path="ignored.db",
            logger=logger,
            storage_settings=manager_storage_settings,
        )
        await _wait_until(
            lambda: schedule_store.get_scheduled_message(schedule_id),
            timeout_seconds=60.0,
        )
        await _wait_until(
            lambda: next(
                (
                    text
                    for text in outputs
                    if "manager schedule tool ok" in text.lower()
                ),
                None,
            ),
            timeout_seconds=180.0,
        )
        scheduled_message = schedule_store.get_scheduled_message(schedule_id)
        _write_json(
            result_dir / "result.json",
            {
                "schedule_id": schedule_id,
                "create_response": create.text,
                "scheduled_message": scheduled_message.model_dump(mode="json")
                if scheduled_message
                else None,
                "outputs": outputs,
            },
        )
        if scheduled_message is None:
            raise AssertionError(
                f"Expected manager-created schedule {schedule_id} to exist."
            )
        if scheduled_message.schedule_name != "live manager reminder":
            raise AssertionError(
                f"Expected schedule name to be preserved, got {scheduled_message.schedule_name}"
            )


async def run_test_43(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_manager_pause_tools",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    manager_storage_settings = _apply_manager_storage(config, 43)
    _apply_state_storage(config, result_dir, 43)
    bootstrap_instance(config)

    async with UserGateway(config, output_sink=None) as gateway:
        session = gateway.start_new_session()
        schedule_id = await gateway.add_cron_job(
            session_key=session.session_key,
            name="pause target",
            instruction="pause target ok",
            interval_seconds=300,
        )
        schedule_store = ScheduleStore(
            db_path="ignored.db",
            logger=logger,
            storage_settings=manager_storage_settings,
        )
        await _wait_until(
            lambda: schedule_store.get_scheduled_message(schedule_id),
            timeout_seconds=60.0,
        )

        pause = await gateway.send_user_message(
            f"Pause the scheduled job with id {schedule_id} for 1 minute using your schedule-management tools. "
            "After pausing it, reply with exactly paused."
        )
        paused_message = schedule_store.get_scheduled_message(schedule_id)
        if paused_message is None or paused_message.paused_until is None:
            raise AssertionError(
                "Expected paused_until to be set after manager pause request."
            )

        resume = await gateway.send_user_message(
            f"Resume the scheduled job with id {schedule_id} using your schedule-management tools. "
            "After resuming it, reply with exactly resumed."
        )
        resumed_message = schedule_store.get_scheduled_message(schedule_id)
        _write_json(
            result_dir / "result.json",
            {
                "schedule_id": schedule_id,
                "create_mode": "gateway_seeded",
                "pause_response": pause.text,
                "resume_response": resume.text,
                "paused_until_after_pause": paused_message.paused_until
                if paused_message
                else None,
                "paused_until_after_resume": resumed_message.paused_until
                if resumed_message
                else None,
                "enabled_after_resume": resumed_message.enabled
                if resumed_message
                else None,
            },
        )
        if "paused" not in pause.text.lower():
            raise AssertionError(f"Expected pause acknowledgement, got: {pause.text}")
        if "resumed" not in resume.text.lower():
            raise AssertionError(f"Expected resume acknowledgement, got: {resume.text}")


async def run_test_44(result_dir: Path, team_id: str) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="live_cli_conversation_switching",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
    )
    _apply_manager_storage(config, 44)
    _apply_state_storage(config, result_dir, 44)
    bootstrap_instance(config)

    tracker = CliMessageTracker()
    async with UserGateway(config, output_sink=None) as gateway:
        session = gateway.resolve_session()
        alpha = await gateway.send_user_message(
            "Reply with exactly cli alpha live.",
            session_key=session.session_key,
        )
        alpha_ref = tracker.add(
            text=alpha.text,
            session_key=session.session_key,
            conversation_id=alpha.conversation_id,
        )

        beta_conversation_id = "conversation_id_cli_live_beta"
        gateway.state_store.register_conversation_session(
            session_key=session.session_key,
            conversation_id=beta_conversation_id,
            last_task_id=None,
        )
        beta = await gateway.send_user_message(
            "Reply with exactly cli beta live.",
            session_key=session.session_key,
            conversation_id=beta_conversation_id,
        )
        beta_ref = tracker.add(
            text=beta.text,
            session_key=session.session_key,
            conversation_id=beta.conversation_id,
        )

        (
            handled,
            current_session_key,
            list_message,
        ) = await _handle_local_session_command(
            raw_text="/list_conversations",
            gateway=gateway,
            current_session_key=session.session_key,
        )
        if not handled or current_session_key != session.session_key:
            raise AssertionError(
                "Expected /list_conversations to be handled for the active session."
            )
        if (
            alpha.conversation_id not in list_message
            or beta.conversation_id not in list_message
        ):
            raise AssertionError(
                f"Expected both conversations in list output, got: {list_message}"
            )

        (
            handled,
            current_session_key,
            switch_message,
        ) = await _handle_local_session_command(
            raw_text=f"/switch_conversation {beta.conversation_id}",
            gateway=gateway,
            current_session_key=session.session_key,
        )
        if not handled or "Active conversation" not in (switch_message or ""):
            raise AssertionError(
                f"Expected successful conversation switch, got: {switch_message}"
            )

        active_followup = await gateway.send_user_message(
            "Reply with exactly cli beta followup live.",
            session_key=current_session_key,
        )
        if active_followup.conversation_id != beta.conversation_id:
            raise AssertionError(
                "Expected normal freeform input to follow the active conversation."
            )

        ref_id, reply_text = _parse_reply_command(
            "/reply msg_1 Reply with exactly cli alpha followup live."
        )
        if ref_id != alpha_ref.ref_id or reply_text is None:
            raise AssertionError(
                "Expected /reply parser to extract the message reference and reply text."
            )

        ref = tracker.get(ref_id)
        if ref is None or ref.conversation_id is None:
            raise AssertionError(
                "Expected stored CLI message reference to resolve to a conversation."
            )

        targeted_followup = await gateway.send_user_message(
            reply_text,
            session_key=current_session_key,
            conversation_id=ref.conversation_id,
        )
        if targeted_followup.conversation_id != alpha.conversation_id:
            raise AssertionError(
                "Expected /reply style targeting to use the referenced conversation."
            )

        _write_json(
            result_dir / "result.json",
            {
                "session_key": session.session_key,
                "alpha_ref": alpha_ref.ref_id,
                "alpha_conversation_id": alpha.conversation_id,
                "beta_ref": beta_ref.ref_id,
                "beta_conversation_id": beta.conversation_id,
                "list_message": list_message,
                "switch_message": switch_message,
                "active_followup": {
                    "text": active_followup.text,
                    "conversation_id": active_followup.conversation_id,
                },
                "targeted_followup": {
                    "text": targeted_followup.text,
                    "conversation_id": targeted_followup.conversation_id,
                },
            },
        )


async def run_test_45(
    result_dir: Path,
    team_id: str,
    *,
    env_path: str | None,
    config_path: str | None,
) -> None:
    manager_storage = _prepare_managed_manager_storage(45)
    manager_config_path = _write_managed_manager_config(
        result_dir,
        memory_graph_enabled=False,
        base_config_path=config_path,
    )
    manager = await _start_managed_manager(
        team_id=team_id,
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=manager_storage,
        extra_env={"MEMORY_GRAPH_ENABLED": "false"},
        config_path=manager_config_path,
    )
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_graceful_manager_restart",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
        request_timeout_seconds=MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS,
    )
    _apply_manager_storage(config, 45)
    _apply_state_storage(config, result_dir, 45)
    bootstrap_instance(config)

    replacement: ManagedManagerProcess | None = None
    try:
        async with UserGateway(config, output_sink=None) as gateway:
            await _wait_for_manager_route(gateway, manager)
            first = await gateway.send_user_message(
                "Reply with exactly gateway graceful precheck ok."
            )
            if "gateway graceful precheck ok" not in first.text.lower():
                raise AssertionError(
                    f"Unexpected initial gateway response: {first.text}"
                )

            await _send_exit_instruction(
                team_id=team_id, target_agent_id=manager.agent_id
            )
            graceful_exit_code = await manager.wait(timeout_seconds=30)
            await _wait_for_manager_unavailable(gateway, timeout_seconds=30)

            unavailable = await gateway.send_user_message(
                "Reply with exactly gateway graceful unavailable."
            )
            if "not currently available" not in unavailable.text.lower():
                raise AssertionError(
                    f"Expected manager-unavailable notice, got: {unavailable.text}"
                )

            replacement = await _start_managed_manager(
                team_id=team_id,
                result_dir=result_dir,
                env_path=env_path,
                storage_settings=manager_storage,
                agent_id=f"gateway-manager-replacement-{new_id_suffix()}",
                config_path=manager_config_path,
            )
            await _wait_for_manager_route(gateway, replacement)
            recovered = await gateway.send_user_message(
                "Reply with exactly gateway graceful recovery ok."
            )
            if "gateway graceful recovery ok" not in recovered.text.lower():
                raise AssertionError(
                    f"Unexpected post-restart gateway response: {recovered.text}"
                )

            _write_json(
                result_dir / "result.json",
                {
                    "initial_manager": manager.snapshot(),
                    "graceful_exit_code": graceful_exit_code,
                    "replacement_manager": replacement.snapshot(),
                    "initial_response": first.text,
                    "unavailable_response": unavailable.text,
                    "recovered_response": recovered.text,
                },
            )
    finally:
        cleanup: list[dict] = []
        for proc in [replacement, manager]:
            if proc is None:
                continue
            with contextlib.suppress(Exception):
                returncode = await proc.stop()
                cleanup.append({**proc.snapshot(), "returncode": returncode})
        if cleanup:
            _write_json(result_dir / "cleanup.json", {"processes": cleanup})


async def run_test_46(
    result_dir: Path,
    team_id: str,
    *,
    env_path: str | None,
    config_path: str | None,
) -> None:
    manager_storage = _prepare_managed_manager_storage(46)
    manager_config_path = _write_managed_manager_config(
        result_dir,
        memory_graph_enabled=False,
        base_config_path=config_path,
    )
    manager = await _start_managed_manager(
        team_id=team_id,
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=manager_storage,
        extra_env={"MEMORY_GRAPH_ENABLED": "false"},
        config_path=manager_config_path,
    )
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_crash_manager_restart",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
        request_timeout_seconds=MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS,
    )
    _apply_manager_storage(config, 46)
    _apply_state_storage(config, result_dir, 46)
    bootstrap_instance(config)

    replacement: ManagedManagerProcess | None = None
    try:
        async with UserGateway(config, output_sink=None) as gateway:
            await _wait_for_manager_route(gateway, manager)
            first = await gateway.send_user_message(
                "Reply with exactly gateway crash precheck ok."
            )
            if "gateway crash precheck ok" not in first.text.lower():
                raise AssertionError(
                    f"Unexpected initial gateway response: {first.text}"
                )

            crash_exit_code = await manager.kill(timeout_seconds=10)
            await _wait_for_manager_unavailable(gateway, timeout_seconds=45)

            unavailable = await gateway.send_user_message(
                "Reply with exactly gateway crash unavailable."
            )
            if "not currently available" not in unavailable.text.lower():
                raise AssertionError(
                    f"Expected manager-unavailable notice after crash, got: {unavailable.text}"
                )

            replacement = await _start_managed_manager(
                team_id=team_id,
                result_dir=result_dir,
                env_path=env_path,
                storage_settings=manager_storage,
                agent_id=f"gateway-manager-replacement-{new_id_suffix()}",
                config_path=manager_config_path,
            )
            await _wait_for_manager_route(gateway, replacement)
            recovered = await gateway.send_user_message(
                "Reply with exactly gateway crash recovery ok."
            )
            if "gateway crash recovery ok" not in recovered.text.lower():
                raise AssertionError(
                    f"Unexpected crash-recovery gateway response: {recovered.text}"
                )

            _write_json(
                result_dir / "result.json",
                {
                    "initial_manager": manager.snapshot(),
                    "crash_exit_code": crash_exit_code,
                    "replacement_manager": replacement.snapshot(),
                    "initial_response": first.text,
                    "unavailable_response": unavailable.text,
                    "recovered_response": recovered.text,
                },
            )
    finally:
        cleanup: list[dict] = []
        for proc in [replacement, manager]:
            if proc is None:
                continue
            with contextlib.suppress(Exception):
                returncode = await proc.stop()
                cleanup.append({**proc.snapshot(), "returncode": returncode})
        if cleanup:
            _write_json(result_dir / "cleanup.json", {"processes": cleanup})


async def run_test_47(
    result_dir: Path,
    team_id: str,
    *,
    env_path: str | None,
    config_path: str | None,
) -> None:
    manager_storage = _prepare_managed_manager_storage(47)
    manager_config_path = _write_managed_manager_config(
        result_dir,
        memory_graph_enabled=False,
        base_config_path=config_path,
    )
    manager = await _start_managed_manager(
        team_id=team_id,
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=manager_storage,
        agent_result_transformer="tests.live_result_transformers:crash_first_task_before_reply",
        config_path=manager_config_path,
    )
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_inflight_manager_crash",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
        request_timeout_seconds=MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS,
    )
    _apply_manager_storage(config, 47)
    _apply_state_storage(config, result_dir, 47)
    bootstrap_instance(config)

    replacement: ManagedManagerProcess | None = None
    try:
        async with UserGateway(config, output_sink=None) as gateway:
            await _wait_for_manager_route(gateway, manager)
            interrupted = await gateway.send_user_message(
                "Reply with exactly gateway inflight crash."
            )
            crash_exit_code = await manager.wait(timeout_seconds=20)
            if "not currently available" not in interrupted.text.lower():
                raise AssertionError(
                    f"Expected in-flight manager-unavailable notice, got: {interrupted.text}"
                )
            await _wait_for_manager_unavailable(gateway, timeout_seconds=45)

            replacement = await _start_managed_manager(
                team_id=team_id,
                result_dir=result_dir,
                env_path=env_path,
                storage_settings=manager_storage,
                agent_id=f"gateway-manager-replacement-{new_id_suffix()}",
                config_path=manager_config_path,
            )
            await _wait_for_manager_route(gateway, replacement)
            recovered = await gateway.send_user_message(
                "Reply with exactly gateway inflight recovery ok."
            )
            if "gateway inflight recovery ok" not in recovered.text.lower():
                raise AssertionError(
                    f"Unexpected in-flight recovery gateway response: {recovered.text}"
                )

            _write_json(
                result_dir / "result.json",
                {
                    "crashing_manager": manager.snapshot(),
                    "crash_exit_code": crash_exit_code,
                    "replacement_manager": replacement.snapshot(),
                    "interrupted_response": interrupted.text,
                    "recovered_response": recovered.text,
                },
            )
    finally:
        cleanup: list[dict] = []
        for proc in [replacement, manager]:
            if proc is None:
                continue
            with contextlib.suppress(Exception):
                returncode = await proc.stop()
                cleanup.append({**proc.snapshot(), "returncode": returncode})
        if cleanup:
            _write_json(result_dir / "cleanup.json", {"processes": cleanup})


async def run_test_48(
    result_dir: Path,
    team_id: str,
    *,
    env_path: str | None,
    config_path: str | None,
) -> None:
    manager_storage = _prepare_managed_manager_storage(48)
    manager_config_path = _write_managed_manager_config(
        result_dir,
        memory_graph_enabled=False,
        base_config_path=config_path,
    )
    manager = await _start_managed_manager(
        team_id=team_id,
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=manager_storage,
        extra_env={"MEMORY_GRAPH_ENABLED": "false"},
        config_path=manager_config_path,
    )
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_stop_command",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
        request_timeout_seconds=MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS,
    )
    _apply_manager_storage(config, 48)
    _apply_state_storage(config, result_dir, 48)
    bootstrap_instance(config)

    outputs: list[str] = []
    request_session_key = "live-stop-session"
    target_file = result_dir / "stop_should_not_create.txt"

    try:
        async with UserGateway(
            config,
            output_sink=lambda text: outputs.append(text),
        ) as gateway:
            await _wait_for_manager_route(gateway, manager)

            request_task = asyncio.create_task(
                gateway.send_user_message(
                    (
                        "Instantiate a CodeWritingAgent and delegate this as a subtask. "
                        f"Have the specialist use shell to `sleep 8` and then write the exact text "
                        f"`stop should not complete` to `{target_file}`. "
                        "Return a final Task response only after the delegated subtask completes."
                    ),
                    session_key=request_session_key,
                )
            )

            await _wait_until(lambda: not request_task.done(), timeout_seconds=10.0)
            await asyncio.sleep(2.0)

            handled, same_session_key, stop_text = await _handle_local_session_command(
                raw_text="/stop",
                gateway=gateway,
                current_session_key=request_session_key,
            )
            if not handled:
                raise AssertionError(
                    "Expected /stop to be handled by the interactive session command surface."
                )
            if same_session_key != request_session_key:
                raise AssertionError(
                    "Expected /stop to keep the current session active."
                )
            if stop_text is None or "Stopped task" not in stop_text:
                raise AssertionError(
                    f"Expected /stop to report a stopped task, got: {stop_text!r}"
                )

            cancelled = await asyncio.wait_for(request_task, timeout=60.0)
            cancelled_text = cancelled.text.lower()
            if "cancelled" not in cancelled_text and "canceled" not in cancelled_text:
                raise AssertionError(
                    f"Expected cancelled task response after /stop, got: {cancelled.text}"
                )

            await asyncio.sleep(10.0)
            if target_file.exists():
                raise AssertionError(
                    f"Stop scenario should not have created delegated output file: {target_file}"
                )

            manager_log_text = ""
            for path in [manager.stdout_log_path, manager.stderr_log_path]:
                if path.exists():
                    manager_log_text += path.read_text(encoding="utf-8")
            if "Agent received 'stop' control instruction" not in manager_log_text:
                raise AssertionError(
                    "Manager logs did not record receipt of the stop control instruction."
                )

            _write_json(
                result_dir / "result.json",
                {
                    "manager": manager.snapshot(),
                    "session_key": request_session_key,
                    "stop_text": stop_text,
                    "cancelled_response": cancelled.text,
                    "target_file": str(target_file),
                    "target_file_exists": target_file.exists(),
                    "outputs": outputs,
                },
            )
    finally:
        cleanup: list[dict] = []
        with contextlib.suppress(Exception):
            returncode = await manager.stop()
            cleanup.append({**manager.snapshot(), "returncode": returncode})
        if cleanup:
            _write_json(result_dir / "cleanup.json", {"processes": cleanup})


async def run_test_49(
    result_dir: Path,
    team_id: str,
    *,
    env_path: str | None,
    config_path: str | None,
) -> None:
    manager_storage = _prepare_managed_manager_storage(49)
    manager_config_path = _write_managed_manager_config(
        result_dir,
        memory_graph_enabled=False,
        embedding_provider="local",
        base_config_path=config_path,
    )
    manager = await _start_managed_manager(
        team_id=team_id,
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=manager_storage,
        extra_env={"MEMORY_GRAPH_ENABLED": "false"},
        config_path=manager_config_path,
    )
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_automation_quiet_digest",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=True,
        quiet_start_hour_local=0,
        quiet_end_hour_local=0,
        request_timeout_seconds=MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS,
    )
    _apply_manager_storage(config, 49)
    _apply_state_storage(config, result_dir, 49)
    bootstrap_instance(config)

    outputs: list[str] = []
    try:
        async with UserGateway(
            config,
            output_sink=lambda text: outputs.append(text),
        ) as gateway:
            await _wait_for_manager_route(gateway, manager)
            seed = await _send_automation_seed_message(
                gateway,
                expected_phrase="automation quiet digest seed ready.",
            )
            baseline_output_count = len(outputs)

            correlation_id = await gateway.submit_automation(
                automation_id=new_id("auto_quiet_dig"),
                hook_name="quiet_hours_digest_followup",
                original_envelope=seed.raw_envelope,
                rendered_original_user_text="Low-priority follow-up reminder for tomorrow's housekeeping review.",
                allowed_decisions=["defer_to_digest", "send_now"],
                decision_instructions=(
                    "Quiet hours are active and this follow-up is low priority. "
                    "Do not interrupt the user now. Choose the later digest path and "
                    "return exactly this text with no markdown: "
                    f"{AUTOMATION_QUIET_DIGEST_EXPECTED_TEXT}"
                ),
            )
            request = await _wait_for_request_status(
                gateway,
                correlation_id=correlation_id,
                expected_status="deferred",
            )
            pending = await _wait_until(
                lambda: gateway.state_store.list_pending_deliveries(),
                timeout_seconds=120.0,
            )
            pending_texts = [item.rendered_text for item in pending]
            if not any(
                AUTOMATION_QUIET_DIGEST_EXPECTED_TEXT in text.lower()
                for text in pending_texts
            ):
                raise AssertionError(
                    f"Expected digest-oriented deferred text, got: {pending_texts!r}"
                )
            if len(outputs) != baseline_output_count:
                raise AssertionError(
                    f"Expected no immediate automation delivery during quiet hours, got outputs: {outputs!r}"
                )

            _write_json(
                result_dir / "result.json",
                {
                    "manager": manager.snapshot(),
                    "seed_response": {
                        "text": seed.text,
                        "conversation_id": seed.conversation_id,
                        "task_id": seed.task_id,
                    },
                    "automation": {
                        "correlation_id": correlation_id,
                        "request_status": request.status,
                        "decision_instructions": (
                            "Quiet hours are active and this follow-up is low priority. "
                            "Do not interrupt the user now. Choose the later digest path."
                        ),
                    },
                    "pending_deliveries": _serialize_pending_deliveries(pending),
                    "outputs": outputs,
                },
            )
    finally:
        cleanup: list[dict] = []
        with contextlib.suppress(Exception):
            returncode = await manager.stop()
            cleanup.append({**manager.snapshot(), "returncode": returncode})
        if cleanup:
            _write_json(result_dir / "cleanup.json", {"processes": cleanup})


async def run_test_50(
    result_dir: Path,
    team_id: str,
    *,
    env_path: str | None,
    config_path: str | None,
) -> None:
    manager_storage = _prepare_managed_manager_storage(50)
    manager_config_path = _write_managed_manager_config(
        result_dir,
        memory_graph_enabled=False,
        embedding_provider="local",
        base_config_path=config_path,
    )
    manager = await _start_managed_manager(
        team_id=team_id,
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=manager_storage,
        extra_env={"MEMORY_GRAPH_ENABLED": "false"},
        config_path=manager_config_path,
    )
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_automation_quiet_urgent",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=True,
        quiet_start_hour_local=0,
        quiet_end_hour_local=0,
        request_timeout_seconds=MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS,
    )
    _apply_manager_storage(config, 50)
    _apply_state_storage(config, result_dir, 50)
    bootstrap_instance(config)

    outputs: list[str] = []
    try:
        async with UserGateway(
            config,
            output_sink=lambda text: outputs.append(text),
        ) as gateway:
            await _wait_for_manager_route(gateway, manager)
            seed = await _send_automation_seed_message(
                gateway,
                expected_phrase="automation urgent override seed ready.",
            )
            baseline_output_count = len(outputs)

            correlation_id = await gateway.submit_automation(
                automation_id=new_id("auto_quiet_urg"),
                hook_name="quiet_hours_urgent_override",
                original_envelope=seed.raw_envelope,
                rendered_original_user_text="Critical production outage follow-up that should wake the user.",
                allowed_decisions=["defer_to_digest", "send_now"],
                decision_instructions=(
                    "This is a real critical outage. Override quiet hours and send immediately. "
                    "Return exactly this text with no markdown: "
                    f"{AUTOMATION_URGENT_OVERRIDE_EXPECTED_TEXT}"
                ),
            )
            request = await _wait_for_request_status(
                gateway,
                correlation_id=correlation_id,
                expected_status="delivered",
            )
            delivered_text = await _wait_until(
                lambda: next(
                    (
                        text
                        for text in outputs[baseline_output_count:]
                        if AUTOMATION_URGENT_OVERRIDE_EXPECTED_TEXT in text.lower()
                    ),
                    None,
                ),
                timeout_seconds=120.0,
            )
            pending = gateway.state_store.list_pending_deliveries()
            if pending:
                raise AssertionError(
                    f"Expected urgent override to bypass deferral, found pending deliveries: {pending!r}"
                )

            _write_json(
                result_dir / "result.json",
                {
                    "manager": manager.snapshot(),
                    "seed_response": {
                        "text": seed.text,
                        "conversation_id": seed.conversation_id,
                        "task_id": seed.task_id,
                    },
                    "automation": {
                        "correlation_id": correlation_id,
                        "request_status": request.status,
                        "decision_instructions": "Critical outage should bypass quiet hours with urgent wording.",
                    },
                    "delivered_text": delivered_text,
                    "pending_deliveries": _serialize_pending_deliveries(pending),
                    "outputs": outputs,
                },
            )
    finally:
        cleanup: list[dict] = []
        with contextlib.suppress(Exception):
            returncode = await manager.stop()
            cleanup.append({**manager.snapshot(), "returncode": returncode})
        if cleanup:
            _write_json(result_dir / "cleanup.json", {"processes": cleanup})


async def run_test_51(
    result_dir: Path,
    team_id: str,
    *,
    env_path: str | None,
    config_path: str | None,
) -> None:
    manager_storage = _prepare_managed_manager_storage(51)
    manager_config_path = _write_managed_manager_config(
        result_dir,
        memory_graph_enabled=False,
        embedding_provider="local",
        base_config_path=config_path,
    )
    manager = await _start_managed_manager(
        team_id=team_id,
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=manager_storage,
        extra_env={"MEMORY_GRAPH_ENABLED": "false"},
        config_path=manager_config_path,
    )
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_automation_digest_release",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=True,
        quiet_start_hour_local=0,
        quiet_end_hour_local=0,
        request_timeout_seconds=MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS,
    )
    _apply_manager_storage(config, 51)
    state_storage = _apply_state_storage(config, result_dir, 51)
    bootstrap_instance(config)

    outputs: list[str] = []
    try:
        async with UserGateway(
            config,
            output_sink=lambda text: outputs.append(text),
        ) as gateway:
            await _wait_for_manager_route(gateway, manager)
            seed = await _send_automation_seed_message(
                gateway,
                expected_phrase="automation digest release seed ready.",
            )
            baseline_output_count = len(outputs)

            correlation_id = await gateway.submit_automation(
                automation_id=new_id("auto_digest_rel"),
                hook_name="quiet_hours_digest_release",
                original_envelope=seed.raw_envelope,
                rendered_original_user_text="Non-urgent after-hours note that should be bundled into the digest.",
                allowed_decisions=["defer_to_digest", "send_now"],
                decision_instructions=(
                    "Quiet hours are active and this is not urgent. "
                    "Return exactly this text with no markdown: "
                    f"{AUTOMATION_DIGEST_RELEASE_EXPECTED_TEXT}"
                ),
            )
            await _wait_for_request_status(
                gateway,
                correlation_id=correlation_id,
                expected_status="deferred",
            )
            pending = await _wait_until(
                lambda: gateway.state_store.list_pending_deliveries(),
                timeout_seconds=120.0,
            )
            deferred_item = next(
                (
                    item
                    for item in pending
                    if item.correlation_id == correlation_id
                    and AUTOMATION_DIGEST_RELEASE_EXPECTED_TEXT
                    in item.rendered_text.lower()
                ),
                None,
            )
            if deferred_item is None:
                raise AssertionError(
                    f"Expected deferred automation payload for {correlation_id}, got: {pending!r}"
                )

            envelope = MessageEnvelope.model_validate_json(deferred_item.payload_json)
            task_id = getattr(envelope.msg_content, "task_id", None)
            if not task_id:
                raise AssertionError(
                    "Expected deferred automation payload to preserve a task_id for delivery receipt checks."
                )

            conversation_store = ConversationStore(
                db_path=manager_storage.sqlite_path or "",
                storage_settings=manager_storage, artifacts_dir=Path(manager_storage.sqlite_path or "").parent / "conversation_artifacts",
            )
            turn_before = await _wait_until(
                lambda: conversation_store.get_turn_by_task_id(task_id),
                timeout_seconds=120.0,
            )
            if turn_before.get("delivery_state") != "pending_gateway":
                raise AssertionError(
                    f"Expected pending_gateway before digest release, got: {turn_before}"
                )

            forced_due_at = _force_all_pending_deliveries_due(state_storage)
            released_batches = await gateway.release_due_deliveries()
            if not released_batches:
                raise AssertionError(
                    "Expected forced-due deferred delivery to release as a digest batch."
                )

            await _wait_for_request_status(
                gateway,
                correlation_id=correlation_id,
                expected_status="delivered",
            )
            turn_after = await _wait_until(
                lambda: conversation_store.get_turn_by_task_id(task_id),
                timeout_seconds=120.0,
            )
            if (
                turn_after.get("delivery_state") != "delivered"
                or turn_after.get("delivery_via") != "digest"
            ):
                raise AssertionError(
                    f"Expected digest delivery receipt to mark the turn delivered, got: {turn_after}"
                )
            if gateway.state_store.list_pending_deliveries():
                raise AssertionError(
                    "Expected no pending deliveries after forced digest release."
                )
            digest_output = await _wait_until(
                lambda: next(
                    (
                        text
                        for text in outputs[baseline_output_count:]
                        if "[Deferred Delivery:" in text
                        and AUTOMATION_DIGEST_RELEASE_EXPECTED_TEXT in text.lower()
                    ),
                    None,
                ),
                timeout_seconds=120.0,
            )

            _write_json(
                result_dir / "result.json",
                {
                    "manager": manager.snapshot(),
                    "seed_response": {
                        "text": seed.text,
                        "conversation_id": seed.conversation_id,
                        "task_id": seed.task_id,
                    },
                    "automation": {
                        "correlation_id": correlation_id,
                        "forced_due_at": forced_due_at,
                        "released_batches": released_batches,
                        "deferred_task_id": task_id,
                    },
                    "turn_before_release": turn_before,
                    "turn_after_release": turn_after,
                    "digest_output": digest_output,
                    "outputs": outputs,
                },
            )
    finally:
        cleanup: list[dict] = []
        with contextlib.suppress(Exception):
            returncode = await manager.stop()
            cleanup.append({**manager.snapshot(), "returncode": returncode})
        if cleanup:
            _write_json(result_dir / "cleanup.json", {"processes": cleanup})


async def run_test_52(
    result_dir: Path,
    team_id: str,
    *,
    env_path: str | None,
    config_path: str | None,
) -> None:
    manager_storage = _prepare_managed_manager_storage(52)
    manager_config_path = _write_managed_manager_config(
        result_dir,
        memory_graph_enabled=False,
        embedding_provider="local",
        base_config_path=config_path,
    )
    manager = await _start_managed_manager(
        team_id=team_id,
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=manager_storage,
        extra_env={"MEMORY_GRAPH_ENABLED": "false"},
        config_path=manager_config_path,
    )
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_adapter_application_boundary",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
        request_timeout_seconds=MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS,
    )
    _configure_live_bridge_user(config)
    _apply_manager_storage(config, 52)
    _apply_state_storage(config, result_dir, 52)
    bootstrap_instance(config)

    try:
        async with UserGateway(config, output_sink=None) as gateway:
            await _wait_for_manager_route(gateway, manager)
            application = GatewayApplicationFacade(
                gateway,
                command_application=gateway.command_application,
            )
            if not application.is_registered_user(
                channel_name="telegram",
                external_user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
            ):
                raise AssertionError(
                    "Expected seeded Telegram identity to resolve through GatewayApplication."
                )

            command = await application.execute_interactive_command(
                raw_text="/new",
                channel_name="telegram",
                user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                chat_id="chat_adapter_boundary",
                help_text=INTERACTIVE_SESSION_HELP,
                enable_reply_command=False,
            )
            if not command.handled or not command.session_key:
                raise AssertionError(
                    f"Expected /new to execute through GatewayApplication, got: {command!r}"
                )
            active_session = application.get_active_session(
                channel_name="telegram",
                user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                chat_id="chat_adapter_boundary",
            )
            if (
                active_session is None
                or active_session.session_key != command.session_key
            ):
                raise AssertionError(
                    "Expected command-created session to be active for the adapter route."
                )

            stored = store_inbound_attachment_bytes(
                data=b"gateway adapter boundary live attachment",
                filename="boundary.txt",
                content_type="text/plain",
                config=config,
                session_key=active_session.session_key,
                conversation_id=active_session.conversation_id,
                origin="telegram",
                caption="adapter boundary live validation",
            )
            application.register_inbound_attachment(
                stored.attachment,
                session_key=active_session.session_key,
                conversation_id=active_session.conversation_id,
                expires_at=stored.expires_at,
            )
            persisted_attachment = gateway.state_store.get_attachment(
                stored.attachment.attachment_id
            )
            if persisted_attachment is None:
                raise AssertionError(
                    "Expected GatewayApplication to register inbound attachment metadata."
                )

            bridge = ChannelBridge(application)
            messages = await bridge.handle_inbound(
                ChannelInboundMessage(
                    channel_name="telegram",
                    user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                    chat_id="chat_adapter_boundary",
                    text=(
                        "Reply briefly with the exact phrase "
                        "adapter application boundary ok."
                    ),
                    session_key=active_session.session_key,
                    message_id="msg_adapter_boundary_1",
                )
            )
            if (
                not messages
                or "adapter application boundary ok" not in messages[0].text.lower()
            ):
                raise AssertionError(
                    f"Unexpected adapter application response: {messages!r}"
                )

            _write_json(
                result_dir / "result.json",
                {
                    "manager": manager.snapshot(),
                    "registered_user": True,
                    "command": {
                        "handled": command.handled,
                        "session_key": command.session_key,
                        "text": command.text,
                    },
                    "active_session": {
                        "session_key": active_session.session_key,
                        "conversation_id": active_session.conversation_id,
                    },
                    "attachment": {
                        "attachment_id": stored.attachment.attachment_id,
                        "filename": stored.attachment.filename,
                        "kind": stored.attachment.kind,
                        "persisted": True,
                    },
                    "messages": [message.text for message in messages],
                },
            )
    finally:
        cleanup: list[dict] = []
        with contextlib.suppress(Exception):
            returncode = await manager.stop()
            cleanup.append({**manager.snapshot(), "returncode": returncode})
        if cleanup:
            _write_json(result_dir / "cleanup.json", {"processes": cleanup})


async def run_test_53(
    result_dir: Path,
    team_id: str,
    *,
    env_path: str | None,
    config_path: str | None,
) -> None:
    manager_storage = _prepare_managed_manager_storage(53)
    manager_config_path = _write_managed_manager_config(
        result_dir,
        memory_graph_enabled=False,
        embedding_provider="local",
        base_config_path=config_path,
    )
    manager = await _start_managed_manager(
        team_id=team_id,
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=manager_storage,
        extra_env={"MEMORY_GRAPH_ENABLED": "false"},
        config_path=manager_config_path,
    )
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_adapter_plugin_registry",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
        request_timeout_seconds=MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS,
    )
    config.interactive_apps.internal_website.enabled = False
    config.interactive_apps.telegram.enabled = True
    config.interactive_apps.plugins["live.registry_probe"] = (
        InteractiveAppPluginConfiguration(
            distribution="live-registry-probe",
            settings=config.interactive_apps.telegram.model_copy(deep=True),
        )
    )
    _configure_live_bridge_user(config)
    _apply_manager_storage(config, 53)
    _apply_state_storage(config, result_dir, 53)
    bootstrap_instance(config)

    probe_apps: list[_LiveRegistryProbeApp] = []

    def build_probe(context):
        app = _LiveRegistryProbeApp(context)
        probe_apps.append(app)
        return app

    descriptor = InteractiveAppDescriptor(
        plugin_id="live.registry_probe",
        channel_name="telegram",
        display_name="Live Registry Probe",
        plugin_api_version=INTERACTIVE_APP_PLUGIN_API_VERSION,
        implementation_version="live-test",
        capabilities=frozenset(
            {
                InteractiveAppCapability.INBOUND_TEXT,
                InteractiveAppCapability.OUTBOUND_TEXT,
                InteractiveAppCapability.PROACTIVE_DELIVERY,
            }
        ),
        configuration_model=type(config.interactive_apps.telegram),
        dependency_probe=lambda _configuration: InteractiveAppDependencyReport(
            available=True,
            checked=("live registry probe",),
            detail="The dependency-free live registry probe is available.",
        ),
        factory=build_probe,
        onboarding=InteractiveAppOnboardingMetadata(
            summary="Live registry probe.",
            setup_order=1,
        ),
        documentation_reference="tests/live_gateway_scenarios.py",
    )
    registry = InteractiveAppRegistry(
        allowed_optional_plugins={"live.registry_probe": "live-registry-probe"}
    )
    registry.register_optional(
        descriptor,
        provenance=InteractiveAppPackageProvenance(
            origin=InteractiveAppPluginOrigin.PYTHON_ENTRY_POINT,
            distribution_name="live-registry-probe",
            distribution_version="live-test",
            entry_point_group="universal_agents.chat_adapters.v1",
            entry_point_name="live.registry_probe",
            entry_point_value="tests.live_gateway_scenarios:build_probe",
        ),
    )

    app_manager = None
    stopped_health = None
    try:
        async with UserGateway(config, output_sink=None) as gateway:
            await _wait_for_manager_route(gateway, manager)
            app_manager = InteractiveAppManager(
                config,
                gateway.application,
                registry=registry,
            )
            await app_manager.start()
            gateway.bind_event_sink(app_manager.handle_gateway_event)
            if app_manager.running_app_names != ["telegram"]:
                raise AssertionError(
                    f"Expected registry probe to be running, got: {app_manager.running_app_names}"
                )
            if len(probe_apps) != 1:
                raise AssertionError(
                    f"Expected one registry-created adapter, got: {len(probe_apps)}"
                )
            app = probe_apps[0]

            new_session_messages = await app.handle_inbound(
                ChannelInboundMessage(
                    channel_name="telegram",
                    user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                    chat_id="chat_registry_probe",
                    text="/new",
                    message_id="msg_registry_probe_new",
                )
            )
            if (
                not new_session_messages
                or "started new session" not in new_session_messages[0].lower()
            ):
                raise AssertionError(
                    f"Unexpected registry probe new-session response: {new_session_messages!r}"
                )
            active_session = app.application.get_active_session(
                channel_name="telegram",
                user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                chat_id="chat_registry_probe",
            )
            if active_session is None:
                raise AssertionError(
                    "Expected the registry-created adapter to establish an active session."
                )

            response_messages = await app.handle_inbound(
                ChannelInboundMessage(
                    channel_name="telegram",
                    user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                    chat_id="chat_registry_probe",
                    text="Reply briefly with the exact phrase adapter registry live ok.",
                    message_id="msg_registry_probe_request",
                )
            )
            if (
                not response_messages
                or "adapter registry live ok" not in response_messages[0].lower()
            ):
                raise AssertionError(
                    f"Unexpected registry probe manager response: {response_messages!r}"
                )

            await app_manager.handle_gateway_event(
                GatewayDeliveryEvent(
                    text="registry proactive delivery ok",
                    session_key=active_session.session_key,
                    channel_name="telegram",
                    user_id=LIVE_BRIDGE_TELEGRAM_USER_ID,
                    chat_id="chat_registry_probe",
                    thread_id=None,
                    conversation_id=active_session.conversation_id,
                    scheduled_job_id=None,
                    correlation_id="registry_probe_proactive",
                    original_message_id=None,
                )
            )
            if app.delivered_events != ["registry proactive delivery ok"]:
                raise AssertionError(
                    f"Unexpected registry probe proactive deliveries: {app.delivered_events!r}"
                )

            running_health = app_manager.plugin_health_for_channel("telegram")
            if (
                running_health is None
                or running_health.state != InteractiveAppLifecycleState.RUNNING
            ):
                raise AssertionError(
                    f"Expected running plugin health, got: {running_health!r}"
                )

            gateway.bind_event_sink(None)
            await app_manager.stop()
            stopped_health = app_manager.plugin_health_for_channel("telegram")
            if (
                stopped_health is None
                or stopped_health.state != InteractiveAppLifecycleState.STOPPED
            ):
                raise AssertionError(
                    f"Expected stopped plugin health, got: {stopped_health!r}"
                )

            _write_json(
                result_dir / "result.json",
                {
                    "manager": manager.snapshot(),
                    "plugin": {
                        "plugin_id": descriptor.plugin_id,
                        "channel_name": descriptor.channel_name,
                        "capabilities": sorted(descriptor.capabilities),
                        "running_state": str(running_health.state),
                        "stopped_state": str(stopped_health.state),
                        "dependency_checked": list(
                            running_health.dependency_report.checked
                        ),
                    },
                    "new_session_messages": new_session_messages,
                    "response_messages": response_messages,
                    "delivered_events": app.delivered_events,
                    "adapter_has_gateway_attribute": hasattr(app, "gateway"),
                },
            )
    finally:
        if app_manager is not None and app_manager.has_running_apps:
            with contextlib.suppress(Exception):
                await app_manager.stop()
        cleanup: list[dict] = []
        with contextlib.suppress(Exception):
            returncode = await manager.stop()
            cleanup.append({**manager.snapshot(), "returncode": returncode})
        if cleanup:
            _write_json(result_dir / "cleanup.json", {"processes": cleanup})


async def run_test_54(
    result_dir: Path,
    team_id: str,
    *,
    env_path: str | None,
    config_path: str | None,
) -> None:
    manager_storage = _prepare_managed_manager_storage(54)
    manager_config_path = _write_managed_manager_config(
        result_dir,
        memory_graph_enabled=False,
        embedding_provider="local",
        base_config_path=config_path,
    )
    manager = await _start_managed_manager(
        team_id=team_id,
        result_dir=result_dir,
        env_path=env_path,
        storage_settings=manager_storage,
        extra_env={"MEMORY_GRAPH_ENABLED": "false"},
        config_path=manager_config_path,
    )
    bind_port = _available_loopback_port()
    auth_env_var = "UA_LIVE_GATEWAY_WEBSITE_TOKEN"
    auth_token = f"live-{uuid.uuid4().hex}"
    os.environ[auth_env_var] = auth_token
    config = GatewayInstanceConfig.create(
        instance_name="live_gateway_website_plugin",
        instance_root=str(result_dir / "instance"),
        team_id=team_id,
        timezone="America/Chicago",
        heartbeat_enabled=False,
        quiet_hours_enabled=False,
        request_timeout_seconds=MANAGED_GATEWAY_RESPONSE_TIMEOUT_SECONDS,
    )
    website_settings = config.interactive_apps.internal_website
    website_settings.enabled = True
    website_settings.web_chat_enabled = True
    website_settings.web_chat_token_env_var = auth_env_var
    website_settings.bind_host = "127.0.0.1"
    website_settings.bind_port = bind_port
    website_settings.public_port = bind_port
    config.network.public_origin = "http://127.0.0.1"
    _apply_manager_storage(config, 54)
    _apply_state_storage(config, result_dir, 54)
    bootstrap_instance(config)

    app_manager = None
    try:
        async with UserGateway(config, output_sink=None) as gateway:
            await _wait_for_manager_route(gateway, manager)
            app_manager = InteractiveAppManager(
                config,
                gateway.application,
                include_apps=["website"],
            )
            await app_manager.start()
            gateway.bind_event_sink(app_manager.handle_gateway_event)

            website = app_manager.apps["website"]
            running_health = app_manager.plugin_health_for_channel("website")
            if (
                running_health is None
                or running_health.state != InteractiveAppLifecycleState.RUNNING
                or running_health.plugin_id != "builtin.internal_website"
            ):
                raise AssertionError(
                    f"Expected running built-in Website plugin health, got: {running_health!r}"
                )
            if hasattr(website, "gateway"):
                raise AssertionError(
                    "Registry-created Website adapter retained a concrete gateway attribute."
                )

            base_url = f"http://127.0.0.1:{bind_port}"
            async with ClientSession(
                cookie_jar=CookieJar(unsafe=True),
                timeout=ClientTimeout(total=45),
            ) as client:
                auth_response = await client.post(
                    f"{base_url}/api/web-chat/auth",
                    json={"token": auth_token},
                )
                auth_payload = await auth_response.json()
                if auth_response.status != 200 or not auth_payload.get("user_bound"):
                    raise AssertionError(
                        f"Unexpected Website authentication response: "
                        f"status={auth_response.status} payload={auth_payload!r}"
                    )

                chat_response = await client.post(
                    f"{base_url}/api/web-chat/chat",
                    json={
                        "message": (
                            "Reply briefly with the exact phrase "
                            "website application boundary live ok."
                        ),
                        "client_id": "browser_live_website",
                        "session_key": "website:web-chat:browser_live_website",
                        "message_id": "msg_live_website",
                    },
                )
                chat_payload = await chat_response.json()
                if (
                    chat_response.status != 200
                    or "website application boundary live ok"
                    not in str(chat_payload.get("reply") or "").lower()
                ):
                    raise AssertionError(
                        f"Unexpected Website manager response: "
                        f"status={chat_response.status} payload={chat_payload!r}"
                    )

                sessions_response = await client.get(
                    f"{base_url}/api/web-chat/sessions"
                )
                sessions_payload = await sessions_response.json()
                if sessions_response.status != 200 or not sessions_payload.get(
                    "sessions"
                ):
                    raise AssertionError(
                        f"Expected the Website application session through HTTP, got: "
                        f"status={sessions_response.status} payload={sessions_payload!r}"
                    )

            gateway.bind_event_sink(None)
            await app_manager.stop()
            stopped_health = app_manager.plugin_health_for_channel("website")
            if (
                stopped_health is None
                or stopped_health.state != InteractiveAppLifecycleState.STOPPED
            ):
                raise AssertionError(
                    f"Expected stopped Website plugin health, got: {stopped_health!r}"
                )

            _write_json(
                result_dir / "result.json",
                {
                    "manager": manager.snapshot(),
                    "plugin": {
                        "plugin_id": running_health.plugin_id,
                        "channel_name": running_health.channel_name,
                        "capabilities": sorted(running_health.capabilities),
                        "running_state": str(running_health.state),
                        "stopped_state": str(stopped_health.state),
                    },
                    "auth": {
                        "status": auth_payload.get("status"),
                        "user_bound": auth_payload.get("user_bound"),
                        "canonical_user_id": auth_payload.get("canonical_user_id"),
                    },
                    "chat": {
                        "status": chat_payload.get("status"),
                        "reply": chat_payload.get("reply"),
                        "session_key": chat_payload.get("session_key"),
                        "conversation_id": chat_payload.get("conversation_id"),
                        "correlation_id": chat_payload.get("correlation_id"),
                    },
                    "session_count": len(sessions_payload["sessions"]),
                    "adapter_has_gateway_attribute": hasattr(website, "gateway"),
                },
            )
    finally:
        os.environ.pop(auth_env_var, None)
        if app_manager is not None and app_manager.has_running_apps:
            with contextlib.suppress(Exception):
                await app_manager.stop()
        cleanup: list[dict] = []
        with contextlib.suppress(Exception):
            returncode = await manager.stop()
            cleanup.append({**manager.snapshot(), "returncode": returncode})
        if cleanup:
            _write_json(result_dir / "cleanup.json", {"processes": cleanup})


async def main() -> None:
    parser = argparse.ArgumentParser(description="Live gateway scenarios for Phase 1")
    parser.add_argument(
        "--test",
        type=int,
        required=True,
        choices=[
            32,
            33,
            34,
            35,
            36,
            37,
            38,
            39,
            40,
            41,
            42,
            43,
            44,
            45,
            46,
            47,
            48,
            49,
            50,
            51,
            52,
            53,
            54,
        ],
        help="Scenario number",
    )
    parser.add_argument("--team-id", required=True, help="Active team id")
    parser.add_argument("--env", default=None, help="Optional env file")
    parser.add_argument(
        "--config",
        default=None,
        help="Optional base config for managed-manager scenarios.",
    )
    args = parser.parse_args()

    if args.env and load_dotenv:
        load_dotenv(args.env)
        logger.info("Loaded environment variables from %s", args.env)

    result_dir = _prepare_result_dir(args.test)
    metadata = {
        "test": args.test,
        "team_id": args.team_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(result_dir / "run_metadata.json", metadata)

    if args.test == 32:
        await run_test_32(result_dir, args.team_id)
    elif args.test == 33:
        await run_test_33(result_dir, args.team_id)
    elif args.test == 34:
        await run_test_34(result_dir, args.team_id)
    elif args.test == 35:
        await run_test_35(result_dir, args.team_id)
    elif args.test == 36:
        await run_test_36(result_dir, args.team_id)
    elif args.test == 37:
        await run_test_37(result_dir, args.team_id)
    elif args.test == 38:
        await run_test_38(result_dir, args.team_id)
    elif args.test == 39:
        await run_test_39(result_dir, args.team_id)
    elif args.test == 40:
        await run_test_40(result_dir, args.team_id)
    elif args.test == 41:
        await run_test_41(result_dir, args.team_id)
    elif args.test == 42:
        await run_test_42(result_dir, args.team_id)
    elif args.test == 43:
        await run_test_43(result_dir, args.team_id)
    elif args.test == 44:
        await run_test_44(result_dir, args.team_id)
    elif args.test == 45:
        await run_test_45(
            result_dir,
            args.team_id,
            env_path=args.env,
            config_path=args.config,
        )
    elif args.test == 46:
        await run_test_46(
            result_dir,
            args.team_id,
            env_path=args.env,
            config_path=args.config,
        )
    elif args.test == 47:
        await run_test_47(
            result_dir,
            args.team_id,
            env_path=args.env,
            config_path=args.config,
        )
    elif args.test == 48:
        await run_test_48(
            result_dir,
            args.team_id,
            env_path=args.env,
            config_path=args.config,
        )
    elif args.test == 49:
        await run_test_49(
            result_dir,
            args.team_id,
            env_path=args.env,
            config_path=args.config,
        )
    elif args.test == 50:
        await run_test_50(
            result_dir,
            args.team_id,
            env_path=args.env,
            config_path=args.config,
        )
    elif args.test == 51:
        await run_test_51(
            result_dir,
            args.team_id,
            env_path=args.env,
            config_path=args.config,
        )
    elif args.test == 52:
        await run_test_52(
            result_dir,
            args.team_id,
            env_path=args.env,
            config_path=args.config,
        )
    elif args.test == 53:
        await run_test_53(
            result_dir,
            args.team_id,
            env_path=args.env,
            config_path=args.config,
        )
    elif args.test == 54:
        await run_test_54(
            result_dir,
            args.team_id,
            env_path=args.env,
            config_path=args.config,
        )
    else:
        raise SystemExit(f"Unsupported test: {args.test}")


if __name__ == "__main__":
    asyncio.run(main())

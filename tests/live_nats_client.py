import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Add parent directory to path to import strands components
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_messages.contracts.control_v1 import AgentStatus
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskAnswer
from custom_messages.enums import ControlType, MessageMetaType, RoutingType
from custom_transport.base import TransportPublishOptions
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.subjects import NatsSubjectMapper
from id_generation import new_id, new_id_suffix
from runtime_settings.transport import TransportSettings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_RESULTS_ROOT = PROJECT_ROOT / "test_results"
TEST_DATA_ROOT = TEST_RESULTS_ROOT / "test_data"
LIVE_TEST_SKILL_ARTIFACT_DIRS = {
    "5": [
        PROJECT_ROOT / ".skills" / "weather-checker",
    ],
}


def _load_live_nats_env() -> None:
    debug_env = Path(os.getenv("TEAM_NATS_LIVE_ENV", str(PROJECT_ROOT / "debug.env")))
    if debug_env.is_file():
        load_dotenv(debug_env, override=False)


def resolve_live_transport_settings() -> TransportSettings:
    """Resolve one transport for all participants using production precedence."""
    from assistant_gateway.runtime_config import load_runtime_config_file
    from config_loader import load_config

    _load_live_nats_env()
    explicit_path = os.getenv("TEAM_NATS_LIVE_CONFIG") or os.getenv("UA_LIVE_CONFIG")
    config_path = Path(explicit_path or PROJECT_ROOT / "config.yaml").expanduser()
    if explicit_path or config_path.is_file():
        config = load_runtime_config_file(config_path)
    else:
        config = load_config(None, search_default_paths=False)
    settings = TransportSettings.model_validate(config.get("transport", {}))
    if not settings.nats_servers:
        settings.nats_servers = [settings.nats_url]
    return settings


def nats_servers_from_env() -> list[str]:
    return resolve_live_transport_settings().nats_servers


def nats_transport_config_from_env(
    *,
    transport: TransportSettings | None = None,
    servers: list[str] | None = None,
    **overrides: object,
) -> NatsTransportConfig:
    """Build the live-test transport with the production NATS auth contract."""

    settings = transport if transport is not None else resolve_live_transport_settings()
    values = settings.model_dump(exclude={"type", "nats_url"})
    for source, target in (
        ("nats_servers", "servers"),
        ("nats_name", "name"),
        ("nats_user", "user"),
        ("nats_password", "password"),
    ):
        values[target] = values.pop(source)
    values["servers"] = servers or settings.nats_servers or [settings.nats_url]
    values.update(overrides)
    return NatsTransportConfig(**values)


def response_route_for_team(team_id: str) -> str:
    return f"{team_id}_gateway_live_user_{new_id_suffix()}_response"


class LiveNatsQueue:
    def __init__(self, name: str) -> None:
        self.name = name
        self._messages: asyncio.Queue[MessageEnvelope] = asyncio.Queue()

    async def push(self, envelope: MessageEnvelope) -> None:
        await self._messages.put(envelope)

    async def get(self, *, timeout: float) -> MessageEnvelope:
        return await asyncio.wait_for(self._messages.get(), timeout=timeout)

    async def purge(self) -> None:
        while not self._messages.empty():
            self._messages.get_nowait()


class LiveNatsClient:
    def __init__(
        self,
        team_id: str,
        *,
        transport: TransportSettings | None = None,
        servers: list[str] | None = None,
    ) -> None:
        self.team_id = team_id
        config = nats_transport_config_from_env(
            transport=transport,
            servers=servers,
            name=f"live-nats-client-{new_id_suffix()}",
        )
        self.mapper = NatsSubjectMapper(
            team_id=team_id,
            subject_prefix=config.subject_prefix,
            user_queue_name=f"{team_id}_user_interact_queue",
        )
        self.adapter = NatsTransportAdapter(
            config=config,
            mapper=self.mapper,
            logger=logger,
        )

    async def __aenter__(self) -> "LiveNatsClient":
        await self.adapter.connect(reconcile_topology=True)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.adapter.close()

    async def subscribe_route(self, route: str) -> LiveNatsQueue:
        queue = LiveNatsQueue(route)

        async def handler(delivery) -> None:
            await queue.push(delivery.envelope)
            await delivery.ack()

        await self.adapter.subscribe_route(
            name=f"live-client:{route}",
            routing_key=route,
            handler=handler,
            durable_name=self.mapper.durable_name(new_id("live_client")),
        )
        return queue

    async def publish(
        self,
        routing_key: str,
        envelope: MessageEnvelope,
        *,
        operational: bool = False,
        durable: bool = True,
    ) -> None:
        await self.adapter.publish(
            envelope=envelope,
            routing_key=routing_key,
            options=TransportPublishOptions(operational=operational, durable=durable),
        )


def _ensure_response_metadata(envelope: MessageEnvelope, default_from_agent_name: str = "user") -> MessageEnvelope:
    """
    Normalize live-test requests so response semantics are explicit.

    The live tests always expect a reply on `response_routing_key`. Mark those
    requests accordingly so the serialized payload shown to the runtime and the
    LLM is internally consistent.
    """
    if envelope.msg_meta is None:
        envelope.msg_meta = MessageMeta(from_agent_name=default_from_agent_name)

    if envelope.msg_header and envelope.msg_header.response_routing_key:
        envelope.msg_meta.is_response_requested = True

    if not envelope.msg_meta.from_agent_name:
        envelope.msg_meta.from_agent_name = default_from_agent_name

    return envelope


def extract_test_number(template_path: str) -> str:
    """Extract the leading numeric test identifier from a template filename."""
    template_name = Path(template_path).name
    match = re.match(r"(?P<test_number>\d+)", template_name)
    if not match:
        raise ValueError(f"Template filename must start with a numeric test id: {template_name}")
    return match.group("test_number")


def prepare_test_result_dir(template_path: str) -> Path:
    """Reset and recreate the per-test result directory under `test_results/`."""
    test_number = extract_test_number(template_path)
    result_dir = TEST_RESULTS_ROOT / f"test_{test_number}"
    if result_dir.exists():
        shutil.rmtree(result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    return result_dir


def cleanup_live_test_artifacts(template_path: str) -> list[str]:
    """
    Remove residual artifacts that would invalidate a fresh live-test run.

    This is primarily used for tests that intentionally create reusable runtime
    assets, such as filesystem-backed skills under `.skills/`.
    """
    test_number = extract_test_number(template_path)
    removed_paths: list[str] = []
    for path in LIVE_TEST_SKILL_ARTIFACT_DIRS.get(test_number, []):
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed_paths.append(str(path))
    return removed_paths


def get_live_test_task_history_db_path() -> Path:
    """Return the shared SQLite task-history DB path used across live tests."""
    TEST_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    return TEST_DATA_ROOT / "task_history.db"


def write_json_file(path: Path, payload: dict | list) -> None:
    """Write structured JSON to disk with stable formatting."""
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def append_jsonl_file(path: Path, payload: dict) -> None:
    """Append one JSON object per line for lightweight event tracing."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")


def persist_final_answer(
    result_dir: Path,
    correlation_id: str,
    task_text: str,
    answer_text: str,
    response_envelope: MessageEnvelope,
) -> None:
    """Persist the final answer for this test run inside its result directory."""
    answer_payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": correlation_id,
        "task": task_text,
        "answer": answer_text,
    }
    write_json_file(result_dir / "final_answer.json", answer_payload)
    (result_dir / "final_answer.txt").write_text(answer_text, encoding="utf-8")
    write_json_file(result_dir / "final_response_envelope.json", response_envelope.model_dump(mode="json"))
    logger.info(f"Stored final answer artifacts in {result_dir}")

async def discover_manager_id(client: LiveNatsClient, team_id: str, user_queue: LiveNatsQueue) -> str:
    """Discovers the TeamArchitect's ID by sending an operational status request."""
    logger.info(f"Discovering TeamArchitect ID for team '{team_id}'...")

    status_msg = AgentStatus(
        self_id="user_client",
        status_type=MessageMetaType.REQUEST,
        info_type=ControlType.STATUS_REQUEST_BASIC,
        named_agent_type="user_client",
        is_paused=False,
        is_executing=False,
        has_execution_capacity=True,
        is_busy=False,
        name="user_client",
        agent_role="user_client"
    )

    envelope = _ensure_response_metadata(MessageEnvelope(
        msg_content=status_msg,
        msg_meta=MessageMeta(from_agent_name="user", to_named_agent_type=None),
        msg_header=MessageHeader(
            from_agent_id="user_client",
            to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
            response_routing_key=user_queue.name,
            agent_signature=""
        )
    ))

    try:
        await client.publish(RoutingType.ALL_AGENTS_ROUTE, envelope, operational=True, durable=False)
        logger.info("Published operational presence request over NATS")
    except Exception as e:
        logger.error(f"Failed to publish status request (is the team running?): {e}")
        sys.exit(1)

    logger.info("Waiting for answers from team to find TeamArchitect...")
    manager_id = None

    try:
        deadline = asyncio.get_running_loop().time() + 20.0
        while asyncio.get_running_loop().time() < deadline:
            env = await user_queue.get(timeout=max(0.1, deadline - asyncio.get_running_loop().time()))
            if isinstance(env.msg_content, AgentStatus) and env.msg_content.named_agent_type == "TeamArchitect":
                manager_id = env.msg_content.self_id
                logger.info(f"Found TeamArchitect with ID: {manager_id}")
                break
    except asyncio.TimeoutError:
        logger.error("Timed out waiting for TeamArchitect to respond.")
        sys.exit(1)

    return manager_id

async def main():
    parser = argparse.ArgumentParser(description="Live NATS Test Client")
    parser.add_argument("--template", required=True, help="Path to JSON template file")
    parser.add_argument("--team-id", required=True, help="Active Team ID")
    parser.add_argument("--agent-id", help="Target agent ID (will auto-discover TeamArchitect if not provided)")
    parser.add_argument("--timeout", type=int, default=120, help="Timeout in seconds to wait for final response")
    parser.add_argument("--env", type=str, default=None, help="Path to a .env file to load")

    args = parser.parse_args()
    cleaned_paths = cleanup_live_test_artifacts(args.template)
    result_dir = prepare_test_result_dir(args.template)
    task_history_db_path = get_live_test_task_history_db_path()

    if args.env and load_dotenv:
        load_dotenv(args.env)
        logger.info(f"Loaded environment variables from {args.env}")

    logger.info("Connecting to NATS server(s): %s", nats_servers_from_env())

    async with LiveNatsClient(args.team_id) as client:
        user_queue_name = response_route_for_team(args.team_id)
        user_queue = await client.subscribe_route(user_queue_name)
        logger.info(f"Purged and listening on queue: {user_queue_name}")

        # Auto-discover target agent ID if not provided
        target_agent_id = args.agent_id
        if not target_agent_id:
            target_agent_id = await discover_manager_id(client, args.team_id, user_queue)

        # Read matching JSON template
        try:
            with open(args.template, 'r') as f:
                template_str = f.read()
        except Exception as e:
            logger.error(f"Failed to read template file: {e}")
            sys.exit(1)

        # Variables for injection
        correlation_id = new_id("corr_id")
        initial_msg_id = new_id("msg_id")
        event_log_path = result_dir / "message_events.jsonl"

        # Substitute placeholders
        message_str = template_str.replace("{{MESSAGE_ID}}", initial_msg_id)
        message_str = message_str.replace("{{TEAM_ID}}", args.team_id)
        message_str = message_str.replace("{{USER_QUEUE}}", user_queue_name)
        message_str = message_str.replace("{{CORRELATION_ID}}", correlation_id)
        message_str = message_str.replace("{{TARGET_AGENT_ID}}", target_agent_id)
        message_str = message_str.replace("{{TIMESTAMP}}", datetime.now(timezone.utc).isoformat())
        message_str = message_str.replace("{{TEST_OUTPUT_DIR}}", str(result_dir))

        try:
            # Parse just to ensure valid JSON and extract task info if available
            env_test = _ensure_response_metadata(
                MessageEnvelope.model_validate_json(message_str)
            )
            task_text = ""
            if isinstance(env_test.msg_content, Task) and env_test.msg_content.problem_statement:
                task_text = env_test.msg_content.problem_statement
            elif isinstance(env_test.msg_content, AgentStatus):
                task_text = "STATUS_REQUEST_FULL"

        except Exception as e:
            logger.error(f"Template parsing error after substitution: {e}")
            sys.exit(1)

        # Publish message
        message_str = env_test.model_dump_json()
        write_json_file(result_dir / "request_envelope.json", env_test.model_dump(mode="json"))
        write_json_file(
            result_dir / "run_metadata.json",
            {
                "template": str(Path(args.template).resolve()),
                "test_number": extract_test_number(args.template),
                "result_dir": str(result_dir),
                "task_history_db_path": str(task_history_db_path),
                "team_id": args.team_id,
                "target_agent_id": target_agent_id,
                "correlation_id": correlation_id,
                "message_id": initial_msg_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "pre_run_cleanup_paths": cleaned_paths,
            },
        )

        await client.publish(target_agent_id, env_test, durable=True)
        logger.info(f"Published message {initial_msg_id} to {target_agent_id} (Correlation: {correlation_id})")

        # Wait for response
        logger.info(f"Waiting up to {args.timeout}s for responses...")
        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= args.timeout:
                logger.error("Timed out waiting for final task response.")
                break

            try:
                remaining = args.timeout - elapsed
                resp_env = await user_queue.get(timeout=remaining)
                if resp_env:
                        try:
                            append_jsonl_file(
                                event_log_path,
                                {
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "event": "received_message",
                                    "envelope": resp_env.model_dump(mode="json"),
                                },
                            )

                            # Discard messages that don't match our current request
                            if resp_env.msg_meta.msg_correlation_id != correlation_id:
                                logger.info(f"Ignored message with unmatched correlation ID: {resp_env.msg_meta.msg_correlation_id}")
                                continue

                            logger.info(f"Received message type: {type(resp_env.msg_content).__name__}")

                            if isinstance(resp_env.msg_content, AgentStatus):
                                logger.info(
                                    "Agent Status received: "
                                    f"Executing={resp_env.msg_content.is_executing}, "
                                    f"HasCapacity={resp_env.msg_content.has_execution_capacity}, "
                                    f"Paused={resp_env.msg_content.is_paused}"
                                )
                                if "status_request" in args.template:
                                    answer_text = json.dumps(resp_env.msg_content.model_dump(), default=str)
                                    persist_final_answer(
                                        result_dir=result_dir,
                                        correlation_id=correlation_id,
                                        task_text=task_text,
                                        answer_text=answer_text,
                                        response_envelope=resp_env,
                                    )
                                    break
                            elif isinstance(resp_env.msg_content, Task):
                                if resp_env.msg_content.task_type == MessageMetaType.RESPONSE:
                                    logger.info("Task response received!")
                                    answer_obj = resp_env.msg_content.answer

                                    answer_text = ""
                                    if isinstance(answer_obj, TaskAnswer):
                                        logger.info(f"Answer: {answer_obj.answer}")
                                        if answer_obj.tools_used:
                                            logger.info(f"Tools Used: {answer_obj.tools_used}")
                                        answer_text = answer_obj.answer
                                    else:
                                        logger.info(f"Answer: {answer_obj}")
                                        answer_text = str(answer_obj)

                                    persist_final_answer(
                                        result_dir=result_dir,
                                        correlation_id=correlation_id,
                                        task_text=task_text,
                                        answer_text=answer_text,
                                        response_envelope=resp_env,
                                    )
                                    break
                                else:
                                    logger.info(f"Received intermediate Task request. "
                                                f"Problem statement: {resp_env.msg_content.problem_statement}")
                        except Exception as e:
                            logger.warning(f"Error processing response envelope: {e}")
            except asyncio.TimeoutError:
                logger.error("Timed out waiting for final task response.")
                break

        await user_queue.purge()
        logger.info(f"Purged queue: {user_queue_name} to ensure test atomicity.")

if __name__ == "__main__":
    asyncio.run(main())

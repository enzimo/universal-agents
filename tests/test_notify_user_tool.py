from custom_agents.task_operation_runtime import TaskOperationRuntime
import os
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.runtime_config import build_runtime_config_from_instance_config
from config_loader import get_config, set_config
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.delivery_v1 import UserNotification
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage, ScheduleTaskMemory
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.runtime_context import _agent_ctx, get_agent_ref, set_agent_ref
from custom_tools.providers.communication import notify_user
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts
from tests.pending_work_support import call_tool_on_agent_loop


@pytest.fixture(autouse=True)
def restore_global_config():
    previous = get_config()
    yield
    set_config(previous)


def _write_config(
    tmp_path: Path,
    *,
    channel_name: str = "telegram",
    chat_id: str = "-100123",
    notification_routing_mode: str = "source_thread_or_default",
) -> Path:
    instance_root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="test-instance",
        instance_root=str(instance_root),
        team_id="test_team",
        timezone="America/Chicago",
    )
    config.delivery.default_target.channel_name = channel_name
    config.delivery.default_target.chat_id = chat_id
    config.delivery.default_target.thread_id = "42"
    config.delivery.notification_routing.mode = notification_routing_mode
    config.user.canonical_user_id = "test_user"
    config.user.default_session_key = "preferred_session"
    config.manager.manager_agent_id = "manager_agent_1"
    return config.save(instance_root / "instance_config.yaml")


def _fake_agent(
    config_path: Path,
    *,
    team_comms: SimpleNamespace | None = None,
    active_gateway_context: GatewayContext | None = None,
    named_agent_type: str = "TeamArchitect",
    memory_config: dict | None = None,
) -> SimpleNamespace:
    active_meta = MessageMeta(
        conversation_id="conversation_1",
        msg_correlation_id="source_corr_1",
        request_timezone="America/Chicago",
        gateway_context=active_gateway_context
        or GatewayContext(session_key="source_session"),
    )
    task_manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        current_task_id="task_1",
        get_task_info=lambda task_id: (
            SimpleNamespace(meta=active_meta) if task_id == "task_1" else None
        ),
        append_history_entry=MagicMock(),
    )
    # The person a task works for comes only from its signed context, checked
    # with the task storage's access settings (authorization off here).
    task_manager.storage_settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(config_path.parent / "task_history.db")}}
    )
    return SimpleNamespace(
        id="agent_1",
        name=named_agent_type,
        named_agent_type=named_agent_type,
        team_id="test_team",
        authorization_config={"enabled": False},
        memory_config=memory_config if memory_config is not None else {},
        instance_config_path=str(config_path),
        # Agents work in their instance's workspaces/ (runtime_settings.instance_layout).
        instance_root=str(config_path.parent),
        workspace_dir=str(config_path.parent / "workspaces"),
        task_manager=task_manager,
        team_comms=team_comms or SimpleNamespace(send_message=AsyncMock()),
    )


def _fake_agent_without_instance_config(
    *, instance_root: Path | None = None, team_comms: SimpleNamespace | None = None
) -> SimpleNamespace:
    agent = _fake_agent(Path("/missing/instance_config.yaml"), team_comms=team_comms)
    agent.instance_config_path = None
    if instance_root is not None:
        agent.instance_root = str(instance_root)
        agent.workspace_dir = str(Path(instance_root) / "workspaces")
    return agent



def _notify(*args, **kwargs):
    """Call notify_user as Strands does: in a worker thread while the agent loop runs."""
    return call_tool_on_agent_loop(get_agent_ref(), notify_user._tool_func, *args, **kwargs)


_OUTSIDE_TASK_FOLDERS = "outside this person's folder, the shared team/ and agents/ folders, the data folders, and what others shared"


def test_notify_user_publishes_user_notification_to_gateway_preferred_target(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    # The active task works for no person, so it may attach files from the
    # shared team/ and agents/ folders.
    screenshot = (
        GatewayInstanceConfig.load(config_path).layout.team_dir / "screenshots" / "tesla.png"
    )
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png-data")
    team_comms = SimpleNamespace(send_message=AsyncMock())
    agent = _fake_agent(config_path, team_comms=team_comms)
    set_agent_ref(agent)
    try:
        result = _notify(
            "Done with the GitHub task.",
            urgency="urgent",
            reason="github_task_done",
            attachments=[
                {
                    "path": str(screenshot),
                    "filename": "tesla.png",
                    "content_type": "image/png",
                    "caption": "Tesla homepage screenshot",
                }
            ],
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "success"
    assert result["channel_name"] == "telegram"
    assert result["chat_id"] == "-100123"
    assert result["thread_id"] == "42"
    assert result["attachment_count"] == 1
    assert result["attachments"][0]["path"] == str(screenshot)
    assert result["task_history_recorded"] is True
    assert result["task_history_warning"] is None
    assert result["user_visible_message_already_sent"] == "Done with the GitHub task."
    assert "do not repeat that message verbatim" in result["final_response_instruction"]

    team_comms.send_message.assert_awaited_once()
    sent = team_comms.send_message.await_args.kwargs
    envelope = sent["message"]
    assert sent["routing_key"] == "test_team_test-instance_user_gateway"
    assert isinstance(envelope.msg_content, UserNotification)
    assert envelope.msg_content.text == "Done with the GitHub task."
    assert envelope.msg_content.urgency == "urgent"
    assert envelope.msg_content.reason == "github_task_done"
    assert len(envelope.msg_content.attachments) == 1
    assert envelope.msg_content.attachments[0].path == str(screenshot)
    assert envelope.msg_content.attachments[0].kind == "image"
    assert envelope.msg_content.source_task_id == "task_1"
    assert envelope.msg_meta.conversation_id == "conversation_1"
    assert envelope.msg_meta.followup_to_task_id == "task_1"
    assert envelope.msg_meta.gateway_context.channel_name == "telegram"
    assert envelope.msg_meta.gateway_context.chat_id == "-100123"
    assert envelope.msg_meta.gateway_context.thread_id == "42"
    assert envelope.msg_meta.gateway_context.session_key == "preferred_session"
    agent.task_manager.append_history_entry.assert_called_once()
    history_call = agent.task_manager.append_history_entry.call_args
    assert history_call.args[0] == "task_1"
    assert (
        history_call.args[1]
        == "User-visible notification sent through notify_user: Done with the GitHub task."
    )
    assert history_call.kwargs["details"]["event"] == "notify_user_sent"
    assert (
        history_call.kwargs["details"]["message_preview"]
        == "Done with the GitHub task."
    )


def test_notify_user_records_scheduled_notification_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Notes are recorded only on a schedule of the person the task verifiably
    # works for, as record_scheduled_run does.
    use_signed_contexts(monkeypatch)
    config_path = _write_config(tmp_path)
    schedule_store = ScheduleStore(db_path=str(tmp_path / "schedule.db"))
    schedule_store.add(
        ScheduledMessage(
            schedule_id="sched_notify_memory",
            schedule_name="notify memory",
            payload=Task(
                task_type=MessageMetaType.REQUEST,
                problem_statement="Check the match.",
            ),
            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id="usr_notify_owner")),
            interval_ms=60_000,
            repeat_count=None,
            schedule_memory=ScheduleTaskMemory(schedule_kind="live_sports_monitor"),
        )
    )
    active_context = GatewayContext(
        canonical_user_id="usr_notify_owner",
        task_context=context_signer().issue(
            team_id="test_team", person_id="usr_notify_owner", person_roles=("member",)
        ),
        session_key="source_session",
        channel_name="telegram",
        chat_id="-100999",
        thread_id="99",
        delivery_mode="cron",
        scheduled_job_id="sched_notify_memory",
    )
    team_comms = SimpleNamespace(send_message=AsyncMock())
    agent = _fake_agent(
        config_path, team_comms=team_comms, active_gateway_context=active_context
    )
    agent._schedule_store = schedule_store
    set_agent_ref(agent)
    try:
        result = _notify(
            "Goal update.",
            reason="scheduled_match_goal",
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "success"
    assert result["schedule_memory_notification"]["recorded"] is True
    stored = schedule_store.get_scheduled_message("sched_notify_memory")
    assert stored is not None
    assert stored.schedule_memory is not None
    assert stored.schedule_memory.ledger.total_notifications == 1
    assert (
        stored.schedule_memory.ledger.recent_notifications[-1]["summary"]
        == "Goal update."
    )


def test_notify_user_can_target_current_thread(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    active_context = GatewayContext(
        gateway_instance_name="test-instance",
        session_key="source_session",
        channel_name="telegram",
        user_id="telegram_user_1",
        canonical_user_id="canonical_test_user",
        chat_id="-100999",
        thread_id="99",
        delivery_mode="interactive",
        original_message_id="msg_1",
    )
    team_comms = SimpleNamespace(send_message=AsyncMock())
    agent = _fake_agent(
        config_path, team_comms=team_comms, active_gateway_context=active_context
    )
    set_agent_ref(agent)
    try:
        result = _notify(
            "Current thread progress update.",
            reason="progress_update",
            target="current_thread",
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "success"
    assert result["channel_name"] == "telegram"
    assert result["chat_id"] == "-100999"
    assert result["thread_id"] == "99"

    team_comms.send_message.assert_awaited_once()
    envelope = team_comms.send_message.await_args.kwargs["message"]
    assert envelope.msg_meta.gateway_context.session_key == "source_session"
    assert envelope.msg_meta.gateway_context.delivery_mode == "interactive"
    assert envelope.msg_meta.gateway_context.canonical_user_id == "canonical_test_user"
    assert envelope.msg_meta.gateway_context.original_message_id == "msg_1"


def test_notify_user_auto_targets_active_web_chat_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_signed_contexts(monkeypatch)
    config_path = _write_config(tmp_path)
    # The active task works for canonical_test_user, so their own folder is allowed.
    screenshot = (
        GatewayInstanceConfig.load(config_path).layout.person_dir("canonical_test_user")
        / "screenshots"
        / "reuters.png"
    )
    screenshot.parent.mkdir(parents=True)
    screenshot.write_bytes(b"png-data")
    active_context = GatewayContext(
        gateway_instance_name="test-instance",
        session_key="website:web-chat:browser_1",
        channel_name="website",
        user_id="browser_1",
        canonical_user_id="canonical_test_user",
        chat_id="web-chat",
        delivery_mode="interactive",
        original_message_id="browser_msg_1",
        task_context=context_signer().issue(
            team_id="test_team", person_id="canonical_test_user", person_roles=("member",)
        ),
    )
    team_comms = SimpleNamespace(send_message=AsyncMock())
    agent = _fake_agent(
        config_path, team_comms=team_comms, active_gateway_context=active_context
    )
    set_agent_ref(agent)
    try:
        result = _notify(
            "Screenshot attached.",
            attachments=[
                {
                    "path": str(screenshot),
                    "filename": "reuters.png",
                    "content_type": "image/png",
                    "kind": "image",
                }
            ],
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "success"
    assert result["channel_name"] == "website"
    assert result["chat_id"] == "web-chat"
    assert result["attachment_count"] == 1

    team_comms.send_message.assert_awaited_once()
    envelope = team_comms.send_message.await_args.kwargs["message"]
    assert envelope.msg_meta.gateway_context.session_key == "website:web-chat:browser_1"
    assert envelope.msg_meta.gateway_context.delivery_mode == "interactive"
    assert envelope.msg_meta.gateway_context.channel_name == "website"
    assert envelope.msg_meta.gateway_context.chat_id == "web-chat"
    assert envelope.msg_content.attachments[0].path == str(screenshot)


def test_notify_user_explicit_default_uses_preferred_target_during_interactive_task(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    active_context = GatewayContext(
        gateway_instance_name="test-instance",
        session_key="website:web-chat:browser_1",
        channel_name="website",
        user_id="browser_1",
        canonical_user_id="canonical_test_user",
        chat_id="web-chat",
        delivery_mode="interactive",
    )
    team_comms = SimpleNamespace(send_message=AsyncMock())
    agent = _fake_agent(
        config_path, team_comms=team_comms, active_gateway_context=active_context
    )
    set_agent_ref(agent)
    try:
        result = _notify(
            "Send to the preferred target.", target="default"
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "success"
    assert result["channel_name"] == "telegram"
    assert result["chat_id"] == "-100123"

    team_comms.send_message.assert_awaited_once()
    envelope = team_comms.send_message.await_args.kwargs["message"]
    assert envelope.msg_meta.gateway_context.session_key == "preferred_session"
    assert envelope.msg_meta.gateway_context.delivery_mode == "agent_notification"


def test_notify_user_memory_class_forces_memory_notification_target(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    active_context = GatewayContext(
        gateway_instance_name="test-instance",
        session_key="discord:discord_chat_1:thread_1",
        channel_name="discord",
        user_id="discord_user_1",
        canonical_user_id="canonical_test_user",
        chat_id="discord_chat_1",
        thread_id="thread_1",
        delivery_mode="interactive",
    )
    team_comms = SimpleNamespace(send_message=AsyncMock())
    agent = _fake_agent(
        config_path,
        team_comms=team_comms,
        active_gateway_context=active_context,
        named_agent_type="MemReviewAgent",
        memory_config={"notifications": {"delivery_target": "default"}},
    )
    set_agent_ref(agent)
    try:
        result = _notify(
            "Saved a memory.",
            reason="",
            target="current_thread",
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "success"
    assert result["notification_class"] == "memory"
    assert result["requested_target"] == "current_thread"
    assert result["effective_target"] == "default"
    assert result["channel_name"] == "telegram"
    assert result["chat_id"] == "-100123"

    team_comms.send_message.assert_awaited_once()
    envelope = team_comms.send_message.await_args.kwargs["message"]
    assert envelope.msg_meta.gateway_context.delivery_mode == "agent_notification"
    assert envelope.msg_meta.gateway_context.channel_name == "telegram"
    assert envelope.msg_meta.gateway_context.chat_id == "-100123"
    history_details = agent.task_manager.append_history_entry.call_args.kwargs[
        "details"
    ]
    assert history_details["notification_class"] == "memory"
    assert history_details["requested_target"] == "current_thread"
    assert history_details["target"] == "default"


def test_notify_user_auto_can_be_forced_to_preferred_target_during_interactive_task(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path, notification_routing_mode="default_target")
    active_context = GatewayContext(
        gateway_instance_name="test-instance",
        session_key="website:web-chat:browser_1",
        channel_name="website",
        user_id="browser_1",
        canonical_user_id="canonical_test_user",
        chat_id="web-chat",
        delivery_mode="interactive",
    )
    team_comms = SimpleNamespace(send_message=AsyncMock())
    agent = _fake_agent(
        config_path, team_comms=team_comms, active_gateway_context=active_context
    )
    set_agent_ref(agent)
    try:
        result = _notify(
            "Send auto notification to the preferred target."
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "success"
    assert result["channel_name"] == "telegram"
    assert result["chat_id"] == "-100123"

    team_comms.send_message.assert_awaited_once()
    envelope = team_comms.send_message.await_args.kwargs["message"]
    assert envelope.msg_meta.gateway_context.session_key == "preferred_session"
    assert envelope.msg_meta.gateway_context.delivery_mode == "agent_notification"


def test_notify_user_current_thread_requires_gateway_context(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    agent = _fake_agent(config_path, active_gateway_context=None)
    agent.task_manager = SimpleNamespace(
        current_task_id="task_1",
        get_task_info=lambda task_id: SimpleNamespace(
            meta=MessageMeta(conversation_id="conversation_1")
        ),
    )
    set_agent_ref(agent)
    try:
        result = _notify("Created memory.", target="current_thread")
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "error"
    assert "requires an active task with gateway_context" in result["error"]


def test_notify_user_resolves_instance_config_from_runtime_config(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    instance_config = GatewayInstanceConfig.load(config_path)
    set_config(build_runtime_config_from_instance_config(instance_config))
    team_comms = SimpleNamespace(send_message=AsyncMock())
    set_agent_ref(_fake_agent_without_instance_config(team_comms=team_comms))
    try:
        result = _notify("Done through runtime config.")
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "success"
    assert result["channel_name"] == "telegram"
    team_comms.send_message.assert_awaited_once()


def test_notify_user_resolves_instance_config_from_agent_instance_root(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    config = GatewayInstanceConfig.load(config_path)
    team_comms = SimpleNamespace(send_message=AsyncMock())
    set_agent_ref(
        _fake_agent_without_instance_config(
            instance_root=config.instance_root_path, team_comms=team_comms
        )
    )
    try:
        result = _notify("Done through instance root discovery.")
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "success"
    assert result["channel_name"] == "telegram"
    team_comms.send_message.assert_awaited_once()


def test_notify_user_logs_runtime_config_failure_before_instance_root_fallback(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    config_path = _write_config(tmp_path)
    config = GatewayInstanceConfig.load(config_path)
    team_comms = SimpleNamespace(send_message=AsyncMock())

    def fail_runtime_config():
        raise RuntimeError("runtime config unavailable")

    monkeypatch.setattr("config_loader.get_config", fail_runtime_config)
    set_agent_ref(
        _fake_agent_without_instance_config(
            instance_root=config.instance_root_path, team_comms=team_comms
        )
    )
    try:
        with caplog.at_level(
            logging.WARNING,
            logger="custom_tools.providers.communication",
        ):
            result = _notify("Done through logged fallback.")
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "success"
    assert "event=notify_user_runtime_config_lookup_failed" in caplog.text
    team_comms.send_message.assert_awaited_once()


def test_notify_user_rejects_app_target_without_chat_id(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, channel_name="telegram", chat_id="")
    set_agent_ref(_fake_agent(config_path))
    try:
        result = _notify("Cannot route this.")
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "error"
    assert "delivery.default_target.chat_id" in result["error"]


def test_notify_user_rejects_attachments_from_instance_root_and_database(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    config = GatewayInstanceConfig.load(config_path)
    team_env = config.instance_root_path / "team.env"
    team_env.write_text("SECURE_TOOL_BROKER_TOKEN=secret\n", encoding="utf-8")
    database_dir = Path(config.resolve_manager_storage_settings().sqlite_path).parent
    database_dir.mkdir(parents=True, exist_ok=True)
    database_copy = database_dir / "export.txt"
    database_copy.write_text("rows", encoding="utf-8")
    set_agent_ref(_fake_agent(config_path))
    try:
        result = _notify(
            "Files attached.",
            attachments=[{"path": str(team_env)}, {"path": str(database_copy)}],
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "error"
    assert _OUTSIDE_TASK_FOLDERS in result["attachment_errors"][0]
    assert str(team_env) in result["attachment_errors"][0]
    # The database folder (dbs/) sits outside workspaces/ and is refused too.
    assert _OUTSIDE_TASK_FOLDERS in result["attachment_errors"][1]
    assert str(database_copy) in result["attachment_errors"][1]


def test_notify_user_rejects_attachment_outside_instance_roots(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png-data")
    set_agent_ref(_fake_agent(config_path))
    try:
        result = _notify(
            "Screenshot attached.",
            attachments=[{"path": str(outside), "content_type": "image/png"}],
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "error"
    assert _OUTSIDE_TASK_FOLDERS in result["attachment_errors"][0]


def test_notify_user_rejects_attachments_from_other_people_and_control_files(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    layout = GatewayInstanceConfig.load(config_path).layout
    other_person_file = layout.person_dir("someone_else") / "private.png"
    other_person_file.parent.mkdir(parents=True)
    other_person_file.write_bytes(b"png-data")
    control_file = layout.soul_path
    control_file.parent.mkdir(parents=True, exist_ok=True)
    control_file.write_text("# Soul\n", encoding="utf-8")
    active_context = GatewayContext(
        gateway_instance_name="test-instance",
        session_key="website:web-chat:browser_1",
        channel_name="website",
        user_id="browser_1",
        canonical_user_id="canonical_test_user",
        chat_id="web-chat",
        delivery_mode="interactive",
    )
    team_comms = SimpleNamespace(send_message=AsyncMock())
    set_agent_ref(
        _fake_agent(config_path, team_comms=team_comms, active_gateway_context=active_context)
    )
    try:
        result = _notify(
            "Files attached.",
            attachments=[{"path": str(other_person_file)}, {"path": str(control_file)}],
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "error"
    assert _OUTSIDE_TASK_FOLDERS in result["attachment_errors"][0]
    assert _OUTSIDE_TASK_FOLDERS in result["attachment_errors"][1]
    team_comms.send_message.assert_not_awaited()


def test_notify_user_tool_is_available_to_active_agents() -> None:
    from custom_agents.strands_team import AGENT_TYPE_MAP

    missing = [
        agent_type
        for agent_type, config in AGENT_TYPE_MAP.items()
        if notify_user not in config.get("tools", [])
    ]

    assert missing == []


def _routing_config() -> SimpleNamespace:
    return SimpleNamespace(
        instance_name="test",
        user=SimpleNamespace(canonical_user_id="usr_first", default_session_key="session_first"),
        delivery=SimpleNamespace(
            default_target=SimpleNamespace(channel_name="telegram", chat_id="chat_first", thread_id=None),
            notification_routing=SimpleNamespace(mode="source_thread_or_default"),
        ),
        manager=SimpleNamespace(manager_agent_id="manager_1"),
        heartbeat=SimpleNamespace(source_id="hb"),
    )


def _meta_for(person_id: str, **route: str) -> SimpleNamespace:
    return SimpleNamespace(gateway_context=SimpleNamespace(canonical_user_id=person_id, delivery_mode="memory_review", **route))


def test_a_default_notice_for_another_person_goes_to_their_route_never_the_first_users_chat() -> None:
    """The configured default chat belongs to the configured user; other people's notices use their own route."""

    config = _routing_config()
    from custom_tools.providers.communication import _notify_user_gateway_context

    first = _notify_user_gateway_context(config, target="default", source_meta=None, person_id="usr_first")
    assert (first.chat_id, first.canonical_user_id) == ("chat_first", "usr_first")

    ben = _notify_user_gateway_context(
        config, target="default", source_meta=_meta_for("usr_ben", scheduled_job_id="mem_rev_user_test_usr_ben"),
        person_id="usr_ben",
    )
    assert ben.canonical_user_id == "usr_ben" and ben.scheduled_job_id == "mem_rev_user_test_usr_ben"
    assert ben.chat_id != "chat_first"

    with pytest.raises(ValueError, match="No chat is known"):
        _notify_user_gateway_context(config, target="default", source_meta=None, person_id="usr_ben")
    with pytest.raises(ValueError, match="No chat is known"):  # a route that belongs to someone else
        _notify_user_gateway_context(
            config, target="default", source_meta=_meta_for("usr_first", session_key="session_first"), person_id="usr_ben"
        )


def test_notify_user_says_plainly_why_a_send_did_not_finish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare TimeoutError printed as an empty string: "Failed to publish user notification: "."""
    import asyncio

    from custom_tools.providers import _runtime, communication

    async def stuck_send(**_kwargs):
        await asyncio.Event().wait()

    team_comms = SimpleNamespace(send_message=AsyncMock(side_effect=stuck_send))
    agent = _fake_agent(_write_config(tmp_path), team_comms=team_comms)
    monkeypatch.setattr(
        communication,
        "_run_agent_coroutine",
        lambda target, coroutine: _runtime._run_agent_coroutine(target, coroutine, timeout=0.05),
    )
    set_agent_ref(agent)
    try:
        result = _notify("Done with the GitHub task.")
    finally:
        _agent_ctx.set(None)
    assert result["status"] == "error"
    assert result["error"] == (
        "Failed to publish user notification: the send did not finish within 0.05 s and was cancelled"
    )

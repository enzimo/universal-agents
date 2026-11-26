"""Checkpoint 4 tests: gateway protocol 1 root-task signing (A1) plus C14 and C16 fixes."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from access_control.authority_verifier import (
    AuthorityTokenVerifier,
    IdentityRejected,
    REFUSAL_NO_TRUST_STORE,
    REFUSAL_UNKNOWN_ISSUER,
    get_process_verifier,
    set_process_verifier,
)
from access_control.ownership import (
    answer_context_id,
    request_context_id,
    sent_files_access,
    steering_context_id,
)
from access_control.task_identity import resolve_task_identity
from assistant_gateway.application.automated_submission import (
    GatewayAutomationRequest,
    GatewayAutonomousTaskRequest,
)
from assistant_gateway.application.interactive_submission import (
    GatewayInteractiveSubmissionRequest,
)
from assistant_gateway.application.manager_commands import (
    GatewayManagerCommandRequest,
)
from assistant_gateway.application.request_context import (
    GatewayCommandPerson,
    GatewayRequestRoute,
)
from assistant_gateway.application.task_progress import (
    GatewayTaskProgressRequest,
)
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.message_host import (
    GatewayHostUnavailable,
    GatewayMessageHost,
)
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.access_v2 import PersonStanding
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.control_v1 import (
    ManagerRuntimeWorkCommand,
    PersonStandingCommandV1,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import RuntimeSnapshotV3, TaskSnapshotRecord
from custom_messages.contracts.task_v1 import (
    Task,
    TaskProgressStatus,
    TaskQuery,
    TaskStatus,
    TaskSteering,
)
from custom_messages.enums import MessageMetaType, ProgressState, TaskSteeringType
from custom_messages.wire.authority_bindings import (
    AUTHORITY_TOKEN_MAX_LIFETIME_SECONDS,
    AuthorityTokenRecord,
)
from custom_messages.wire.models import AuthorityToken
from custom_transport.protocol1.authority_tokens import root_task_token
from custom_transport.protocol1.trust_reader import TrustReader, trust_reader_transport_config
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from storage.runtime_state_repository import create_runtime_state_repository
from storage.sqlite_connection import connect_sqlite
from tests import message_contract_support as fixtures
from tests import test_authority_tokens_and_verifier as tv
from tests.test_gateway_phase1 import _build_test_config, _CaptureGatewayManagerTransport


def _joined_gateway_host(
    tmp_path: Path,
    *,
    team: str = tv.TEAM,
    clock: Any = None,
) -> GatewayMessageHost:
    """A ``GatewayMessageHost`` already joined with the fixture gateway host certificate."""

    layout = InstanceLayout.at(tmp_path / "gateway-host")
    host_wrapper = GatewayMessageHost(layout=layout, team=team)
    gw_identity = tv._identity_for("gateway-1", "test-gateway-key-1")
    host_wrapper.host = SimpleNamespace(
        identity=gw_identity,
        clock=clock or (lambda: fixtures.NOW),
    )
    host_wrapper._joined = True
    host_wrapper._ready.set()
    return host_wrapper


def _build_real_gateway(tmp_path: Path) -> tuple[UserGateway, _CaptureGatewayManagerTransport, GatewayMessageHost, AuthorityTokenVerifier, str, str]:
    """Compose a real ``UserGateway`` wired with a joined protocol 1 ``GatewayMessageHost``."""

    config = _build_test_config(tmp_path)
    config.gateway.team_id = tv.TEAM
    config.manager.team_id = tv.TEAM
    config.delivery.quiet_hours.enabled = False
    gw_host = _joined_gateway_host(tmp_path, team=tv.TEAM)
    transport = _CaptureGatewayManagerTransport()
    gateway = UserGateway(
        config,
        output_sink=None,
        state_store=GatewayStateStore(str(tmp_path / "gateway.db")),
        manager_transport=transport,
        token_signer=gw_host,
    )
    gateway._manager_client._manager_id = "agent_lead_1"
    person_id = config.user.canonical_user_id
    access_repo = AccessRepository(config.manager_storage_settings)
    person_row = access_repo.team_person(tv.TEAM, person_id)
    assert person_row is not None
    verifier = AuthorityTokenVerifier(
        fixtures.trust_store(),
        people=access_repo,
        clock=lambda: fixtures.NOW,
    )
    return gateway, transport, gw_host, verifier, person_id, person_row.person_ref


# -- A1: GatewayMessageHost root-task signing & non-gateway refusal --------------------------------


def test_gateway_message_host_signs_contract_valid_root_task_token_a1(tmp_path: Path) -> None:
    gw_host = _joined_gateway_host(tmp_path)
    token = gw_host.sign_root_task_token(
        person_ref=tv.ALICE_REF,
        root_task_id="task_abc",
        request_id="message_context_" + ("a" * 64),
    )

    assert isinstance(token, AuthorityTokenRecord)
    wire = AuthorityToken.model_validate(token.model_dump(mode="json"))
    assert wire.bound_to.type == "root_task"
    assert wire.bound_to.id == "task_abc"
    assert wire.bound_to.request_id == "message_context_" + ("a" * 64)
    assert wire.person == tv.ALICE_REF
    assert "schedule_id" not in token.model_dump(mode="json")

    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=tv._default_people(), clock=lambda: fixtures.NOW)
    verified = verifier.verify_root_task(token, team_id=tv.TEAM, now=fixtures.NOW, require_issuer="gateway")
    assert verified.person_id == "usr_alice"
    assert verified.root_task_id == "task_abc"
    assert verified.request_id == "message_context_" + ("a" * 64)
    assert (verified.expires_at - verified.issued_at).total_seconds() <= AUTHORITY_TOKEN_MAX_LIFETIME_SECONDS

    # Non-opaque person id is refused without echoing it.
    with pytest.raises(ValueError, match="opaque reference") as exc:
        gw_host.sign_root_task_token(person_ref="alice@example.com", root_task_id="t1", request_id="r1")
    assert "alice@example.com" not in str(exc.value)

    # Team outside certificate is refused.
    with pytest.raises(ValueError, match="does not serve team"):
        gw_host.sign_root_task_token(person_ref=tv.ALICE_REF, root_task_id="t1", request_id="r1", team="team_other")

    # Unjoined host refuses cleanly.
    unjoined = GatewayMessageHost(layout=InstanceLayout.at(tmp_path / "unjoined"), team=tv.TEAM)
    with pytest.raises(GatewayHostUnavailable):
        unjoined.sign_root_task_token(person_ref=tv.ALICE_REF, root_task_id="t1", request_id="r1")


def test_non_gateway_agent_host_cannot_sign_root_task_token_a1() -> None:
    """Decision 58 / A1: a lead_host or worker_host certificate is refused by root_task_token."""

    worker_identity = tv._identity_for("worker-host-1", "test-worker-key-1", epoch=3)

    with pytest.raises(ValueError, match="Only a gateway or scheduler"):
        root_task_token(
            worker_identity,
            team=tv.TEAM,
            root_task_id="task_forged",
            request_id="task_context_forged",
            person_ref=tv.ALICE_REF,
            issued=fixtures.NOW,
        )


# -- A1: Every gateway admission path produces a contract-valid root-task token --------------------


@pytest.mark.asyncio
async def test_all_gateway_admission_paths_sign_protocol1_root_task_tokens_a1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway, transport, _gw_host, verifier, person_id, person_ref = _build_real_gateway(tmp_path)
    route = GatewayRequestRoute(
        channel_name="cli",
        user_id=person_id,
        chat_id="chat-1",
        canonical_user_id=person_id,
    )
    session = gateway._session_service.resolve_session(
        requested_session_key="cli:owner",
        default_session_key="cli:owner",
        manager_agent_id="agent_lead_1",
        owner_canonical_user_id=person_id,
        default_owner_canonical_user_id=person_id,
        enforce_owner_scope=False,
    )
    attachment = UserTaskAttachment(
        attachment_id="att_1",
        kind="document",
        filename="report.txt",
        local_path="/uploads/report.txt",
        size_bytes=12,
        sha256="a" * 64,
    )

    def _assert_valid_gateway_token(
        envelope: MessageEnvelope,
        *,
        expected_root_task_id: str,
        expected_request_id: str | None = None,
        request_id_prefix: str | None = None,
    ) -> Any:
        assert envelope.msg_meta is not None and envelope.msg_meta.gateway_context is not None
        token = envelope.msg_meta.gateway_context.task_context
        assert isinstance(token, AuthorityTokenRecord)
        dumped = json.loads(envelope.model_dump_json())
        raw_token = dumped["msg_meta"]["gateway_context"]["task_context"]
        wire = AuthorityToken.model_validate(raw_token)
        assert wire.person == person_ref
        assert person_id not in json.dumps(raw_token)
        assert "schedule_id" not in raw_token
        assert wire.bound_to.type == "root_task"
        assert wire.bound_to.id == expected_root_task_id
        if expected_request_id is not None:
            assert wire.bound_to.request_id == expected_request_id
        if request_id_prefix is not None:
            assert wire.bound_to.request_id.startswith(request_id_prefix)
        resolved = resolve_task_identity(
            verifier=verifier,
            team_id=tv.TEAM,
            task_context=token,
            schedule_run=None,
            schedule_runs=None,
            valid_at=fixtures.NOW,
            reached_at=fixtures.NOW,
        )
        assert resolved.person_id == person_id
        assert resolved.context_id == wire.bound_to.request_id
        assert resolved.root_task_id == expected_root_task_id
        # Carried unchanged through JSON serialization (delegations and replies).
        roundtripped = GatewayContext.model_validate_json(envelope.msg_meta.gateway_context.model_dump_json())
        assert roundtripped.task_context == token
        return resolved

    # 1. Interactive chat submission (with attachment -> message_context_<sha256> and sent_files_access).
    monkeypatch.setattr(
        gateway._response_coordinator,
        "await_terminal_response",
        AsyncMock(return_value=SimpleNamespace(text="ok")),
    )
    await gateway._interactive_submission_service.submit(
        GatewayInteractiveSubmissionRequest(
            text="Check this file",
            session_key=session.session_key,
            route=route,
            attachments=(attachment,),
        )
    )
    chat_env = transport.durable[-1][0]
    assert isinstance(chat_env.msg_content, Task)
    chat_sender = _assert_valid_gateway_token(
        chat_env,
        expected_root_task_id=chat_env.msg_content.task_id,
        expected_request_id=request_context_id(chat_env.msg_content),
    )
    assert sent_files_access(
        sender=chat_sender,
        task_id=chat_env.msg_content.task_id,
        conversation_id=chat_env.msg_meta.conversation_id,
        person_id=person_id,
        message_context_id=request_context_id(chat_env.msg_content),
        message_conversation_id=chat_env.msg_meta.conversation_id,
    ).allowed

    # 2. Thread guidance (steering with attachment -> message_context_<sha256> and sent_files_access).
    steering = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        message="Use this updated file",
        target_task_id=chat_env.msg_content.task_id,
        target_correlation_id="corr_parent",
        attachments=[attachment],
    )
    held = SimpleNamespace(
        correlation_id="corr_steer",
        target_correlation_id="corr_parent",
        session_key=session.session_key,
        conversation_id=session.conversation_id,
        steering_json=steering.model_dump_json(),
    )
    target_req = SimpleNamespace(
        correlation_id="corr_parent",
        session_key=session.session_key,
        request_kind="interactive",
        request_text="Check this file",
        conversation_id=session.conversation_id,
        active_task_id=chat_env.msg_content.task_id,
        run_state="running",
    )
    guidance_env = gateway._thread_guidance._envelope(held, target_req, "agent_lead_1")  # type: ignore[arg-type]
    assert guidance_env is not None and isinstance(guidance_env.msg_content, TaskSteering)
    guidance_sender = _assert_valid_gateway_token(
        guidance_env,
        expected_root_task_id=chat_env.msg_content.task_id,
        expected_request_id=steering_context_id(guidance_env.msg_content),
    )
    assert sent_files_access(
        sender=guidance_sender,
        task_id=chat_env.msg_content.task_id,
        conversation_id=session.conversation_id,
        person_id=person_id,
        message_context_id=steering_context_id(guidance_env.msg_content),
        message_conversation_id=guidance_env.msg_meta.conversation_id,
    ).allowed

    # 3. Clarification answer (with attachment -> message_context_<sha256> and sent_files_access).
    question = TaskQuery(
        task_query_id="q_1",
        query_type=MessageMetaType.REQUEST,
        message="Which file?",
        parent_task_id=chat_env.msg_content.task_id,
    )
    await gateway._clarification_core.reply_to_task_query(
        question,
        correlation_id="corr_q",
        response_text="This one",
        session_key=session.session_key,
        conversation_id=session.conversation_id,
        attachments=(attachment,),
    )
    answer_env = transport.durable[-1][0]
    assert isinstance(answer_env.msg_content, TaskQuery)
    answer_sender = _assert_valid_gateway_token(
        answer_env,
        expected_root_task_id=chat_env.msg_content.task_id,
        expected_request_id=answer_context_id(answer_env.msg_content),
    )
    assert sent_files_access(
        sender=answer_sender,
        task_id=chat_env.msg_content.task_id,
        conversation_id=session.conversation_id,
        person_id=person_id,
        message_context_id=answer_context_id(answer_env.msg_content),
        message_conversation_id=answer_env.msg_meta.conversation_id,
    ).allowed

    # 4. Automated submission (autonomous task + automation hook).
    await gateway.automated_submission.submit_autonomous_task(
        GatewayAutonomousTaskRequest(
            instruction="Run daily check",
            session_key=session.session_key,
            origin_kind="proactive",
            route=route,
        )
    )
    auto_env = transport.durable[-1][0]
    assert isinstance(auto_env.msg_content, Task)
    _assert_valid_gateway_token(
        auto_env,
        expected_root_task_id=auto_env.msg_content.task_id,
        request_id_prefix="task_context_",
    )

    await gateway.automated_submission.submit_automation(
        GatewayAutomationRequest(
            automation_id="auto_123",
            hook_name="quiet_hours",
            original_envelope=chat_env,
            rendered_original_user_text="Check this file",
            allowed_decisions=("deliver_now",),
            decision_instructions="Decide",
            session_key=session.session_key,
            route=route,
        )
    )
    automation_env = transport.durable[-1][0]
    _assert_valid_gateway_token(
        automation_env,
        expected_root_task_id="auto_123",
        request_id_prefix="task_context_",
    )

    # 5. Manager control commands (PersonStandingCommandV1 + ManagerRuntimeWorkCommand).
    async def _answer_control(env: MessageEnvelope, **_kwargs: Any) -> None:
        corr = env.msg_meta.msg_correlation_id
        reply = MessageEnvelope(
            msg_content=env.msg_content,
            msg_header=MessageHeader(from_agent_id="agent_lead_1"),
            msg_meta=MessageMeta(msg_correlation_id=corr),
        )
        await gateway._manager_client.handle_response(reply)

    orig_pub_durable = transport.publish_durable

    async def _pub_and_reply(env: MessageEnvelope, *, routing_key: str) -> None:
        await orig_pub_durable(env, routing_key=routing_key)
        if isinstance(env.msg_content, (PersonStandingCommandV1, ManagerRuntimeWorkCommand)):
            await _answer_control(env)

    monkeypatch.setattr(transport, "publish_durable", _pub_and_reply)

    standing_cmd = PersonStandingCommandV1(
        command_id="standing_cmd_1",
        team_id=tv.TEAM,
        person_id="usr_bob",
        standing=PersonStanding.DISABLED,
    )
    await gateway._manager_commands.request_person_standing(standing_cmd, actor_person_id=person_id)
    _assert_valid_gateway_token(
        transport.durable[-1][0],
        expected_root_task_id="standing_cmd_1",
        request_id_prefix="task_context_",
    )

    work_cmd = ManagerRuntimeWorkCommand(
        command_id="work_cmd_1",
        action="inventory",
        scope="conversation",
        conversation_id=session.conversation_id,
    )
    await gateway._manager_commands.request(
        GatewayManagerCommandRequest(
            command=work_cmd,
            session=session,
            person=GatewayCommandPerson(canonical_user_id=person_id, team_id=tv.TEAM, roles=("owner",)),
            route=route,
        )
    )
    _assert_valid_gateway_token(
        transport.durable[-1][0],
        expected_root_task_id="work_cmd_1",
        request_id_prefix="task_context_",
    )

    # 6. Operational probes: parent task probe (TaskStatus) and task progress status (TaskProgressStatus).
    orig_pub_op = transport.publish_operational

    async def _pub_op_and_reply(env: MessageEnvelope, *, routing_key: str) -> None:
        await orig_pub_op(env, routing_key=routing_key)
        corr = env.msg_meta.msg_correlation_id
        if isinstance(env.msg_content, TaskStatus):
            reply_content: Any = TaskStatus(
                status_type=MessageMetaType.RESPONSE,
                task_status_id=env.msg_content.task_status_id,
                task_id=env.msg_content.task_id,
                is_known_task=True,
            )
        elif isinstance(env.msg_content, TaskProgressStatus):
            reply_content = TaskProgressStatus(
                status_type=MessageMetaType.RESPONSE,
                progress_status_id=env.msg_content.progress_status_id,
                canonical_user_id=env.msg_content.canonical_user_id,
                session_key=env.msg_content.session_key,
                conversation_id=env.msg_content.conversation_id,
                status="idle",
            )
        else:
            return
        await gateway._manager_client.handle_response(
            MessageEnvelope(
                msg_content=reply_content,
                msg_header=MessageHeader(from_agent_id="agent_lead_1"),
                msg_meta=MessageMeta(msg_correlation_id=corr),
            )
        )

    monkeypatch.setattr(transport, "publish_operational", _pub_op_and_reply)

    monkeypatch.setattr(gateway._manager_client._runtime, "_connected", True)
    probe_pending = SimpleNamespace(
        task_query_id="q_probe",
        session_key=session.session_key,
        conversation_id=session.conversation_id,
        parent_task_id=chat_env.msg_content.task_id,
    )
    assert await gateway._clarification_core._parent_is_active(probe_pending)  # type: ignore[arg-type]
    probe_env = transport.operational[-1][0]
    assert isinstance(probe_env.msg_content, TaskStatus)
    _assert_valid_gateway_token(
        probe_env,
        expected_root_task_id=probe_env.msg_content.task_status_id,
        request_id_prefix="task_context_",
    )

    await gateway._task_progress_service.request(
        GatewayTaskProgressRequest(
            session_key=session.session_key,
            conversation_id=session.conversation_id,
            route=route,
        )
    )
    prog_env = transport.operational[-1][0]
    assert isinstance(prog_env.msg_content, TaskProgressStatus)
    _assert_valid_gateway_token(
        prog_env,
        expected_root_task_id=prog_env.msg_content.progress_status_id,
        request_id_prefix="task_context_",
    )


@pytest.mark.asyncio
async def test_ensure_signer_ready_waits_for_joining_gateway_host_a1(tmp_path: Path) -> None:
    """Requests arriving while the gateway host is finishing its background join wait briefly and get a token."""

    gateway, _transport, gw_host, verifier, person_id, _person_ref = _build_real_gateway(tmp_path)
    gw_host._joined = False
    gw_host._ready = asyncio.Event()

    async def _finish_join() -> None:
        await asyncio.sleep(0.02)
        gw_host._joined = True
        gw_host._ready.set()

    joiner = asyncio.create_task(_finish_join())
    try:
        await gateway._request_context_service.ensure_signer_ready(timeout=2.0)
    finally:
        await joiner

    ctx = gateway._request_context_service.build_gateway_context(
        session_key="cli:owner",
        delivery_mode="interactive",
        route=GatewayRequestRoute(canonical_user_id=person_id),
        manager_agent_id="lead",
        root_task_id="task_after_join",
    )
    assert ctx.task_context is not None
    resolved = resolve_task_identity(
        verifier=verifier,
        team_id=tv.TEAM,
        task_context=ctx.task_context,
        schedule_run=None,
        schedule_runs=None,
        valid_at=fixtures.NOW,
        reached_at=fixtures.NOW,
    )
    assert resolved.person_id == person_id and resolved.root_task_id == "task_after_join"


# -- C14: Change ledger historical verification & explicit verifier injection ----------------------


def test_operator_archives_deferred_request_without_process_verifier_c14(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C14(a): strands_main._fail_latest_deferred_task works with get_process_verifier() == None."""

    import strands_main

    gateway, _transport, gw_host, verifier, person_id, person_ref = _build_real_gateway(tmp_path)
    storage = gateway.config.manager_storage_settings
    db_path = str(storage.sqlite_path)
    token = gw_host.sign_root_task_token(
        person_ref=person_ref,
        root_task_id="task_deferred_1",
        request_id="task_context_deferred_1",
    )
    envelope = MessageEnvelope(
        msg_content=Task(
            task_id="task_deferred_1",
            task_type=MessageMetaType.REQUEST,
            problem_statement="Stuck work",
        ),
        msg_header=MessageHeader(from_agent_id="gateway"),
        msg_meta=MessageMeta(
            gateway_context=GatewayContext(canonical_user_id=person_id, task_context=token),
        ),
    )
    repo = create_runtime_state_repository(storage)
    repo.add_deferred_message(
        owner_agent_id="lead",
        owner_agent_name="Lead",
        team_id=tv.TEAM,
        owner_named_agent_type="TeamArchitect",
        message_id="msg_1",
        envelope_json=envelope.model_dump_json(),
        first=True,
        expect_existing=False,
    )

    set_process_verifier(None)
    assert get_process_verifier() is None
    monkeypatch.setattr(strands_main, "_operator_authority_verifier", lambda _ctx: verifier)

    context = strands_main.OperatorContext(
        storage_settings=storage,
        db_path=db_path,
        team_id=tv.TEAM,
        named_agent_type="TeamArchitect",
        is_team_lead=True,
        owner_agent_name="Lead",
    )
    outcome = strands_main._fail_latest_deferred_task(
        context=context,
        owner_agent_id="lead",
        reason="Operator cleared stuck deferred task.",
    )
    assert outcome["task_id"] == "task_deferred_1"
    assert get_process_verifier() is None

    with connect_sqlite(db_path) as conn:
        rows = conn.execute("SELECT request_id, state, person_id FROM change_requests").fetchall()
    assert [(str(r[0]), str(r[1]), str(r[2])) for r in rows] == [
        ("task_context_deferred_1", "ended", person_id)
    ]


def test_change_ledger_saves_and_cancels_after_gateway_revocation_and_stale_revocations_c14(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """C14(b): revoked gateway cert and stale revocations (+30d) never abort saves or stop commands."""

    gateway, _transport, gw_host, _verifier, person_id, person_ref = _build_real_gateway(tmp_path)
    access_repo = AccessRepository(gateway.config.manager_storage_settings)
    db_path = str(tmp_path / "tasks_c14.db")
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": db_path}})

    # Case 1: gateway certificate revoked after admission.
    revoked_store = fixtures.trust_store(add=("extra/gateway-revoked.json",))
    revoked_verifier = AuthorityTokenVerifier(revoked_store, people=access_repo, clock=lambda: fixtures.NOW)
    repo = create_runtime_state_repository(storage, authority_verifier=revoked_verifier)

    token = gw_host.sign_root_task_token(
        person_ref=person_ref,
        root_task_id="task_revoked_gw",
        request_id="task_context_revoked_gw",
    )
    snap_task = TaskSnapshotRecord(
        task_id="task_revoked_gw",
        state=ProgressState.ACCEPTED,
        task=Task(task_id="task_revoked_gw", task_type=MessageMetaType.REQUEST, problem_statement="Work"),
        header=MessageHeader(message_id="m1", timestamp=fixtures.NOW.isoformat()),
        meta=MessageMeta(gateway_context=GatewayContext(source="test", task_context=token)),
        owner_person_id=person_id,
    )
    snapshot = RuntimeSnapshotV3(schema_version=3, tasks=[snap_task], task_queue=["task_revoked_gw"])

    set_process_verifier(None)
    with caplog.at_level(logging.WARNING, logger="access_control.authority_verifier"):
        repo.save_task_runtime(
            owner_agent_id="lead",
            owner_agent_name="Lead",
            team_id=tv.TEAM,
            owner_named_agent_type="TeamArchitect",
            runtime_state_json=snapshot.model_dump_json(),
            expect_existing=False,
        )
        repo.record_task_cancellations(
            team_id=tv.TEAM,
            root_task_id="task_revoked_gw",
            task_ids=["task_revoked_gw"],
            canonical_user_id=person_id,
            command_id="stop_revoked_1",
            reason="Stop tasks from revoked gateway.",
            cancelled_at=fixtures.NOW.isoformat(),
        )
    assert "event=change_ledger_historical_issuer_revoked" in caplog.text

    # Case 2: revocations record stale by +30 days.
    future_now = fixtures.NOW + timedelta(days=30)
    stale_verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=access_repo, clock=lambda: future_now)
    repo_stale = create_runtime_state_repository(storage, authority_verifier=stale_verifier)
    token2 = gw_host.sign_root_task_token(
        person_ref=person_ref,
        root_task_id="task_stale_rev",
        request_id="task_context_stale_rev",
    )
    snap_task2 = TaskSnapshotRecord(
        task_id="task_stale_rev",
        state=ProgressState.ACCEPTED,
        task=Task(task_id="task_stale_rev", task_type=MessageMetaType.REQUEST, problem_statement="Work 2"),
        header=MessageHeader(message_id="m2", timestamp=fixtures.NOW.isoformat()),
        meta=MessageMeta(gateway_context=GatewayContext(source="test", task_context=token2)),
        owner_person_id=person_id,
    )
    snapshot2 = RuntimeSnapshotV3(schema_version=3, tasks=[snap_task2], task_queue=["task_stale_rev"])

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="access_control.authority_verifier"):
        repo_stale.save_task_runtime(
            owner_agent_id="lead_2",
            owner_agent_name="Lead",
            team_id=tv.TEAM,
            owner_named_agent_type="TeamArchitect",
            runtime_state_json=snapshot2.model_dump_json(),
            expect_existing=False,
        )
        repo_stale.record_task_cancellations(
            team_id=tv.TEAM,
            root_task_id="task_stale_rev",
            task_ids=["task_stale_rev"],
            canonical_user_id=person_id,
            command_id="stop_stale_1",
            reason="Stop during NATS outage.",
            cancelled_at=future_now.isoformat(),
        )
    assert "event=change_ledger_historical_revocations_stale" in caplog.text

    with connect_sqlite(db_path) as conn:
        rows = {
            str(r[0]): str(r[1])
            for r in conn.execute("SELECT request_id, state FROM change_requests").fetchall()
        }
    assert rows == {
        "task_context_revoked_gw": "stopped",
        "task_context_stale_rev": "stopped",
    }

    # Forged signature still aborts a new save even with historical attribution.
    forged = token.model_copy(update={"signature": "A" * 86})
    bad_task = snap_task.model_copy(
        update={
            "task_id": "task_forged",
            "task": snap_task.task.model_copy(update={"task_id": "task_forged"}),
            "meta": MessageMeta(gateway_context=GatewayContext(source="test", task_context=forged)),
        }
    )
    with pytest.raises(IdentityRejected):
        repo.save_task_runtime(
            owner_agent_id="lead_bad",
            owner_agent_name="Lead",
            team_id=tv.TEAM,
            owner_named_agent_type="TeamArchitect",
            runtime_state_json=RuntimeSnapshotV3(schema_version=3, tasks=[bad_task], task_queue=["task_forged"]).model_dump_json(),
            expect_existing=False,
        )


# -- C16: TrustReader transport identity and offline startup recovery ------------------------------


def test_trust_reader_transport_config_identifies_the_reader_not_team_services_c16(tmp_path: Path) -> None:
    config = GatewayInstanceConfig.create(
        instance_name="acme",
        instance_root=str(tmp_path / "inst"),
        team_id="team_acme",
        timezone="UTC",
        display_name="Owner",
    )
    broker_cfg = trust_reader_transport_config(config, reader_id="secure-tool-broker")
    op_cfg = trust_reader_transport_config(config, reader_id="strands-operator")

    assert broker_cfg.name == "acme-secure-tool-broker"
    assert broker_cfg.reply_inbox_runtime == "secure-tool-broker"
    assert broker_cfg.jetstream_enabled is False

    assert op_cfg.name == "acme-strands-operator"
    assert op_cfg.reply_inbox_runtime == "strands-operator"
    assert op_cfg.jetstream_enabled is False


@pytest.mark.asyncio
async def test_trust_reader_starts_without_nats_fails_closed_and_connects_in_background_c16(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C16: broker opens TrustReader when NATS is down, fails closed, and syncs once NATS answers."""

    from custom_transport.protocol1.bootstrap import write_bundle
    import custom_transport.protocol1.trust_reader as tr_mod

    layout = InstanceLayout.at(tmp_path / "inst")
    layout.ensure_directories()
    write_bundle(layout.trust_bootstrap_path, tv._bootstrap_bundle_for_example())

    attempts = 0
    connected_event = asyncio.Event()
    gw_cert_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    rev_raw = (fixtures.TRUST / "revocations" / "install_example.json").read_bytes()
    entries = {
        f"host.{tv.INSTALLATION}.gateway-1.1": gw_cert_raw,
        f"revocations.{tv.INSTALLATION}": rev_raw,
    }

    monkeypatch.setattr(
        tr_mod.StateRecords,
        "on_connection",
        classmethod(lambda cls, _nc, _id, trust: tv._TestStateRecords(entries, trust)),
    )

    async def _connect_later() -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("NATS still down")
        connected_event.set()
        return object()

    reader = TrustReader.open(
        layout,
        "secure-tool-broker",
        nc=None,
        connect=_connect_later,
        clock=lambda: fixtures.NOW,
    )
    assert reader.connected is False

    token = root_task_token(
        tv._identity_for("gateway-1", "test-gateway-key-1"),
        team=tv.TEAM,
        root_task_id="t1",
        request_id="task_context_1",
        person_ref=tv.ALICE_REF,
        issued=fixtures.NOW,
    )
    verifier = AuthorityTokenVerifier(reader, people=tv._default_people(), clock=lambda: fixtures.NOW)
    with pytest.raises(IdentityRejected) as refused:
        await verifier.verify_root_task_async(token, team_id=tv.TEAM, now=fixtures.NOW)
    assert refused.value.code in {REFUSAL_UNKNOWN_ISSUER, REFUSAL_NO_TRUST_STORE}

    old_retry = tr_mod._WATCH_RETRY_SECONDS
    tr_mod._WATCH_RETRY_SECONDS = 0.02
    stop = asyncio.Event()
    task = asyncio.create_task(reader.watch_and_refresh(stop))
    try:
        await asyncio.wait_for(connected_event.wait(), timeout=2.0)
        for _ in range(50):
            if reader.connected and reader.store.revocations_current(tv.INSTALLATION, fixtures.NOW):
                break
            await asyncio.sleep(0.01)
        verified = verifier.verify_root_task(token, team_id=tv.TEAM, now=fixtures.NOW, require_issuer="gateway")
        assert verified.person_id == "usr_alice"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        tr_mod._WATCH_RETRY_SECONDS = old_retry


# -- C17: Admitted non-terminal root task resave after restart -------------------------------------


@pytest.mark.parametrize("case", ["cert_not_held", "no_verifier_yet"])
def test_admitted_non_terminal_resave_succeeds_without_issuer_cert_or_verifier_c17(
    tmp_path: Path, case: str
) -> None:
    """C17: re-saving an already-admitted running task after restart never aborts on missing trust."""

    gateway, _transport, gw_host, _verifier, person_id, person_ref = _build_real_gateway(tmp_path)
    access_repo = AccessRepository(gateway.config.manager_storage_settings)
    db_path = tmp_path / "admitted_resave.db"
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(db_path)}})
    full_verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=access_repo, clock=lambda: fixtures.NOW)
    token = gw_host.sign_root_task_token(
        person_ref=person_ref,
        root_task_id="task_admitted_run",
        request_id="task_context_admitted_run",
    )

    def _snap(state: ProgressState, text: str, tok: Any) -> str:
        rec = TaskSnapshotRecord(
            task_id="task_admitted_run",
            state=state,
            task=Task(task_id="task_admitted_run", task_type=MessageMetaType.REQUEST, problem_statement=text),
            header=MessageHeader(message_id="m1", timestamp=fixtures.NOW.isoformat()),
            meta=MessageMeta(gateway_context=GatewayContext(source="test", task_context=tok)),
            owner_person_id=person_id,
        )
        return RuntimeSnapshotV3(schema_version=3, tasks=[rec], task_queue=["task_admitted_run"]).model_dump_json()

    set_process_verifier(None)
    repo = create_runtime_state_repository(storage, authority_verifier=full_verifier)
    kw = dict(owner_agent_id="lead", owner_agent_name="Lead", team_id=tv.TEAM, owner_named_agent_type="TeamArchitect")
    repo.save_task_runtime(
        runtime_state_json=_snap(ProgressState.ACCEPTED, "v1", token),
        expect_existing=False,
        **kw,
    )

    # Simulate a restarted process whose trust store does not hold gateway-1 yet, or has no verifier bound yet.
    restarted_verifier = (
        AuthorityTokenVerifier(
            fixtures.trust_store(remove=("certificates/gateway-1.json",)),
            people=access_repo,
            clock=lambda: fixtures.NOW,
        )
        if case == "cert_not_held"
        else None
    )
    repo2 = create_runtime_state_repository(storage, authority_verifier=restarted_verifier)
    repo2.save_task_runtime(
        runtime_state_json=_snap(ProgressState.IN_PROGRESS, "v2", token),
        expect_existing=True,
        **kw,
    )

    with connect_sqlite(db_path) as conn:
        rows = {
            str(r[0]): str(r[1])
            for r in conn.execute("SELECT request_id, state FROM change_requests").fetchall()
        }
    assert rows == {"task_context_admitted_run": "open"}

    # Security invariant: a task originally saved WITHOUT a token cannot sneak in a forged token on resave.
    unverified_rec = TaskSnapshotRecord(
        task_id="task_unsigned",
        state=ProgressState.ACCEPTED,
        task=Task(task_id="task_unsigned", task_type=MessageMetaType.REQUEST, problem_statement="v1"),
        header=MessageHeader(message_id="m2", timestamp=fixtures.NOW.isoformat()),
        meta=MessageMeta(gateway_context=GatewayContext(source="test", task_context=None)),
        owner_person_id=person_id,
    )
    repo.save_task_runtime(
        owner_agent_id="lead_unsigned",
        owner_agent_name="Lead",
        team_id=tv.TEAM,
        owner_named_agent_type="TeamArchitect",
        runtime_state_json=RuntimeSnapshotV3(
            schema_version=3, tasks=[unverified_rec], task_queue=["task_unsigned"]
        ).model_dump_json(),
        expect_existing=False,
    )
    forged = token.model_copy(update={"signature": "A" * 86})
    sneaky_rec = unverified_rec.model_copy(
        update={
            "state": ProgressState.IN_PROGRESS,
            "meta": MessageMeta(gateway_context=GatewayContext(source="test", task_context=forged)),
        }
    )
    with pytest.raises(IdentityRejected):
        repo2.save_task_runtime(
            owner_agent_id="lead_unsigned",
            owner_agent_name="Lead",
            team_id=tv.TEAM,
            owner_named_agent_type="TeamArchitect",
            runtime_state_json=RuntimeSnapshotV3(
                schema_version=3, tasks=[sneaky_rec], task_queue=["task_unsigned"]
            ).model_dump_json(),
            expect_existing=True,
        )


# -- C18: Non-blocking broker startup against closed NATS port and post-close reconnect ------------


@pytest.mark.asyncio
async def test_broker_startup_does_not_block_on_unreachable_nats_and_recovers_after_close_c18(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C18: broker startup does not block 120s when NATS is down, and TrustReader reconnects after close."""

    import socket
    import time

    import assistant_cli
    import custom_transport.protocol1.trust_reader as tr_mod
    from custom_transport.nats_adapter import NatsTransportAdapter
    from custom_transport.protocol1.bootstrap import write_bundle
    from custom_transport.subjects import NatsSubjectMapper

    # Pick an unbound local TCP port that immediately refuses connections.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()

    inst_root = tmp_path / "inst"
    config = GatewayInstanceConfig.create(
        instance_name="acme-c18",
        instance_root=str(inst_root),
        team_id="team_c18",
        timezone="UTC",
        display_name="Owner",
    )
    config.transport.nats_servers = [f"nats://127.0.0.1:{closed_port}"]
    config.layout.ensure_directories()
    config_path = config.save(inst_root / "instance_config.yaml")
    write_bundle(config.layout.trust_bootstrap_path, tv._bootstrap_bundle_for_example())

    # 1. Real NatsTransportAdapter.connect with trust_reader_transport_config fails fast (< 5s, not 120s).
    adapter = NatsTransportAdapter(
        config=trust_reader_transport_config(config, reader_id="secure-tool-broker"),
        mapper=NatsSubjectMapper(team_id="team_c18"),
    )
    t0 = time.monotonic()
    with pytest.raises(Exception):
        await adapter.connect(reconcile_topology=False)
    assert time.monotonic() - t0 < 5.0

    # 2. Broker service startup builds its gate and hands off to run_secure_tool_broker immediately (< 3s).
    observed: dict[str, Any] = {}

    async def _fake_run_broker(**kwargs: Any) -> None:
        observed.update(kwargs)

    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "test-token")
    monkeypatch.setattr(assistant_cli, "run_secure_tool_broker", _fake_run_broker)
    t1 = time.monotonic()
    await assistant_cli._run_secure_tool_broker_service(
        str(config_path),
        host="127.0.0.1",
        port=18767,
    )
    assert time.monotonic() - t1 < 3.0
    assert observed["access_gate"] is not None
    assert observed["trust_reader"] is not None
    assert observed["trust_reader"].connected is False

    # 3. Once connected, if the NATS connection permanently closes, watch_and_refresh resets records and reconnects.
    gw_cert_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    rev_raw = (fixtures.TRUST / "revocations" / "install_example.json").read_bytes()
    entries: dict[str, bytes] = {f"revocations.{tv.INSTALLATION}": rev_raw}

    class _FakeConn:
        def __init__(self) -> None:
            self.is_closed = False

    class _ClosingRecords(tv._TestStateRecords):
        def __init__(self, nc: _FakeConn, data: dict[str, bytes], trust: Any) -> None:
            super().__init__(data, trust)
            self._nc = nc

        async def watch(self, record: str, pattern: str):  # type: ignore[override]
            while not self._nc.is_closed:
                await asyncio.sleep(0.01)
            raise RuntimeError("nats: connection closed")
            yield  # pragma: no cover

    conns: list[_FakeConn] = []
    reconnected = asyncio.Event()

    async def _connect_twice() -> Any:
        conn = _FakeConn()
        conns.append(conn)
        if len(conns) >= 2:
            entries[f"host.{tv.INSTALLATION}.gateway-1.1"] = gw_cert_raw
            reconnected.set()
        return conn

    monkeypatch.setattr(
        tr_mod.StateRecords,
        "on_connection",
        classmethod(lambda cls, nc, _id, trust: _ClosingRecords(nc, entries, trust)),
    )

    reader = TrustReader.open(
        config.layout,
        "secure-tool-broker-reconnect",
        nc=None,
        connect=_connect_twice,
        clock=lambda: fixtures.NOW,
    )
    old_retry = tr_mod._WATCH_RETRY_SECONDS
    old_upkeep = tr_mod._UPKEEP_CHECK_INTERVAL
    tr_mod._WATCH_RETRY_SECONDS = 0.02
    tr_mod._UPKEEP_CHECK_INTERVAL = timedelta(milliseconds=20)
    stop = asyncio.Event()
    watch_task = asyncio.create_task(reader.watch_and_refresh(stop))
    try:
        for _ in range(50):
            if len(conns) == 1 and reader.connected:
                break
            await asyncio.sleep(0.01)
        assert len(conns) == 1 and reader.connected
        # Simulate permanent NATS connection close after a long outage.
        conns[0].is_closed = True
        await asyncio.wait_for(reconnected.wait(), timeout=2.0)
        token = root_task_token(
            tv._identity_for("gateway-1", "test-gateway-key-1"),
            team=tv.TEAM,
            root_task_id="t_reconn",
            request_id="task_context_reconn",
            person_ref=tv.ALICE_REF,
            issued=fixtures.NOW,
        )
        verifier = AuthorityTokenVerifier(reader, people=tv._default_people(), clock=lambda: fixtures.NOW)
        for _ in range(50):
            try:
                verified = verifier.verify_root_task(token, team_id=tv.TEAM, now=fixtures.NOW, require_issuer="gateway")
                assert verified.person_id == "usr_alice"
                break
            except IdentityRejected:
                await asyncio.sleep(0.01)
        else:
            raise AssertionError("TrustReader did not re-sync gateway-1 after reconnecting")
    finally:
        stop.set()
        await asyncio.wait_for(watch_task, timeout=2.0)
        tr_mod._WATCH_RETRY_SECONDS = old_retry
        tr_mod._UPKEEP_CHECK_INTERVAL = old_upkeep


# -- C19: Never publish unsigned person requests; maintenance installs wait for host join ----------


@pytest.mark.asyncio
async def test_gateway_refuses_unsigned_person_request_and_maintenance_waits_for_join_c19(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """C19: an unjoined host refuses to publish a person's request and returns a plain notice; maintenance waits."""

    from assistant_gateway.application.request_context import SIGNER_UNAVAILABLE_USER_MESSAGE
    from assistant_gateway.application.thread_guidance import GuidanceDelivery

    # 1. Host that never joins: interactive request is NOT published and person gets a plain notice.
    (tmp_path / "unjoined").mkdir(parents=True, exist_ok=True)
    gateway, transport, _gw_host, _verifier, person_id, _person_ref = _build_real_gateway(tmp_path / "unjoined")

    class _NeverReadySigner:
        async def ready(self, timeout: float = 5.0) -> None:
            raise TimeoutError("host never joined")

        def sign_root_task_token(self, **_kwargs: Any) -> AuthorityTokenRecord:
            raise GatewayHostUnavailable("gateway host is not joined yet")

    gateway._request_context_service._token_signer = _NeverReadySigner()
    route = GatewayRequestRoute(
        channel_name="telegram",
        user_id="tg_alice",
        chat_id="chat_alice",
        canonical_user_id=person_id,
    )
    session = gateway._session_service.resolve_session(
        requested_session_key="telegram:chat_alice",
        default_session_key="telegram:chat_alice",
        manager_agent_id="agent_lead_1",
        owner_canonical_user_id=person_id,
        default_owner_canonical_user_id=person_id,
        enforce_owner_scope=False,
    )

    with caplog.at_level(logging.WARNING, logger="assistant_gateway.application.request_context"):
        response = await gateway._interactive_submission_service.submit(
            GatewayInteractiveSubmissionRequest(
                text="Please run my report.",
                session_key=session.session_key,
                route=route,
            )
        )

    assert transport.durable == [], "unsigned person request must never be published"
    assert SIGNER_UNAVAILABLE_USER_MESSAGE in response.text
    assert "event=gateway_authority_token_unavailable" in caplog.text
    assert f"person_id={person_id}" in caplog.text

    # Held guidance also stays held (never published unsigned) when host is unavailable.
    steering = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        message="Wait, use Q2 instead.",
        target_task_id="task_running",
        target_correlation_id="corr_running",
    )
    gateway.state_store.record_request(
        correlation_id="corr_running",
        session_key=session.session_key,
        request_kind="interactive",
        request_text="Original",
        conversation_id=session.conversation_id,
        followup_to_task_id=None,
        active_task_id="task_running",
        run_state="running",
    )
    held = SimpleNamespace(
        correlation_id="corr_steer_c19",
        target_correlation_id="corr_running",
        session_key=session.session_key,
        conversation_id=session.conversation_id,
        steering_json=steering.model_dump_json(),
    )
    attempt = await gateway._thread_guidance._send_attempt(held)  # type: ignore[arg-type]
    assert attempt.outcome is GuidanceDelivery.UNCONFIRMED
    assert transport.operational == []

    # 2. Maintenance installs wait for the host join and publish signed envelopes.
    (tmp_path / "delayed").mkdir(parents=True, exist_ok=True)
    gw2, transport2, gw_host2, verifier2, person_id2, _person_ref2 = _build_real_gateway(tmp_path / "delayed")
    gw2.config.heartbeat.enabled = True
    gw2._maintenance_schedules._settings = gw2._maintenance_schedules._settings.__class__(
        instance_name=gw2._maintenance_schedules._settings.instance_name,
        response_route=gw2._maintenance_schedules._settings.response_route,
        user_display_name=gw2._maintenance_schedules._settings.user_display_name,
        request_timezone=gw2._maintenance_schedules._settings.request_timezone,
        default_owner_canonical_user_id=gw2._maintenance_schedules._settings.default_owner_canonical_user_id,
        heartbeat=gw2._maintenance_schedules._settings.heartbeat.__class__(
            enabled=True,
            schedule_id="heartbeat_launcher_test",
            interval_seconds=60,
            source_id="hb_src",
        ),
        specialist_schedules=(),
        internal_improvement=gw2._maintenance_schedules._settings.internal_improvement,
    )
    joined_gate = asyncio.Event()

    class _DelayedJoinSigner:
        async def ready(self, timeout: float = 5.0) -> None:
            await asyncio.wait_for(joined_gate.wait(), timeout=timeout)

        def sign_root_task_token(self, **kwargs: Any) -> AuthorityTokenRecord:
            if not joined_gate.is_set():
                raise GatewayHostUnavailable("not joined yet")
            return gw_host2.sign_root_task_token(**kwargs)

    gw2._request_context_service._token_signer = _DelayedJoinSigner()

    async def _finish_join() -> None:
        await asyncio.sleep(0.05)
        joined_gate.set()

    join_task = asyncio.create_task(_finish_join())
    try:
        await gw2._maintenance_schedules.ensure_all()
    finally:
        await join_task

    assert len(transport2.durable) >= 1
    for env, _route in transport2.durable:
        tok = env.msg_meta.gateway_context.task_context
        assert isinstance(tok, AuthorityTokenRecord)
        verified = verifier2.verify_root_task(tok, team_id=tv.TEAM, now=fixtures.NOW, require_issuer="gateway")
        assert verified.person_id == person_id2

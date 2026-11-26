"""Checkpoint 3 tests: contract model cutover, resolver cutover, Decision 52, C04/C05/C07/C12."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from access_catalog.catalog import CatalogSource
from access_control.authority_verifier import (
    AuthorityTokenVerifier,
    IdentityRejected,
    REFUSAL_EXPIRED,
    REFUSAL_INVALID_PERMIT,
    REFUSAL_PERMIT_NOT_ALLOWED,
    REFUSAL_PERSON_INACTIVE,
    use_process_verifier,
)
from access_control.caller_tokens import CALLER_MANAGER, BrokerCaller
from access_control.ownership import request_context_id, sent_files_access
from access_control.task_identity import (
    ScheduleFacts,
    ScheduleRunLookup,
    job_schedule_id,
    reported_schedule_run,
    reported_task_person,
    resolve_task_identity,
    resolve_task_identity_async,
    verified_token_to_task_context,
)
from custom_agents import access_runtime, change_writers, manager_control_runtime, working_requests
from custom_agents.agent_host import AgentProcessHost
from custom_agents.agent_requests import configure_process_host
from custom_agents.task_message_runtime import _handle_task_request
from custom_messages.contracts.access_v2 import (
    PersonStanding,
    ScheduleRunIdentityV1,
    TeamPersonV1,
)
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.contracts.common_v1 import PrincipalDescriptorV1, PrincipalType
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.mcp_v1 import MCPInvocationRequestV1
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.runtime_snapshot_v3 import TaskSnapshotRecord
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState
from custom_messages.wire.authority_bindings import AuthorityTokenRecord
from custom_tools.runtime_context import task_work
from custom_transport.nats_adapter import NatsTransportAdapter
from custom_transport.protocol1.authority_tokens import root_task_token, schedule_permit_token
from custom_transport.protocol1.dead_letters import raw_copy_names_person
from custom_transport.protocol1.joining import HostProfile
from custom_transport.protocol1.trust_reader import TrustReader
from custom_transport.protocol1.trust_state import PersistentTrustStore
from custom_transport.subjects import NatsSubjectMapper
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker.access_gate import BrokerAccessGate
from secure_tool_broker.service import SecureToolBrokerService
from storage.change_ledger import ensure_ledger_tables, record_task_request_state, require_same_request_identity
from storage.config import resolve_storage_settings
from storage.sql_dialect import SQLITE
from tests import message_contract_support as fixtures
from tests import test_authority_tokens_and_verifier as tv
from tests.broker_access_support import BROKER_ROOT, caller_headers


class _FakeSchedules:
    def __init__(self, *rows: ScheduleFacts) -> None:
        self._rows = {row.schedule_id: row for row in rows}

    def schedule_facts(self, schedule_id: str) -> ScheduleFacts | None:
        return self._rows.get(schedule_id)


def _gw_token(
    *,
    person_ref: str = tv.ALICE_REF,
    root_task_id: str = "task_1",
    request_id: str = "task_context_1",
    issued: datetime | None = None,
    expires_at: datetime | None = None,
    team: str = tv.TEAM,
) -> dict[str, Any]:
    return root_task_token(
        tv._identity_for("gateway-1", "test-gateway-key-1"),
        team=team,
        root_task_id=root_task_id,
        request_id=request_id,
        person_ref=person_ref,
        issued=issued or fixtures.NOW,
        expires_at=expires_at,
    )


def _sched_token(
    *,
    schedule_id: str = "sched_1",
    person_ref: str | None = tv.ALICE_REF,
    root_task_id: str = "task_sched_1",
    request_id: str = "task_context_sched_1",
    issued: datetime | None = None,
) -> dict[str, Any]:
    return root_task_token(
        tv._identity_for("scheduler-1", "test-scheduler-key-1"),
        team=tv.TEAM,
        root_task_id=root_task_id,
        request_id=request_id,
        person_ref=person_ref,
        schedule_id=schedule_id,
        issued=issued or fixtures.NOW,
    )


def _sched_permit(
    *,
    schedule_id: str = "sched_1",
    person_ref: str | None = tv.ALICE_REF,
    owner_permit: bool = False,
    issued: datetime | None = None,
) -> dict[str, Any]:
    return schedule_permit_token(
        tv._identity_for("scheduler-1", "test-scheduler-key-1"),
        team=tv.TEAM,
        schedule_id=schedule_id,
        person_ref=person_ref,
        owner_permit=owner_permit,
        issued=issued or fixtures.NOW,
    )


def test_contract_models_accept_authority_tokens_and_reject_legacy_signed_contexts() -> None:
    token = _gw_token()
    permit = _sched_permit()
    run = ScheduleRunIdentityV1(
        permit=AuthorityTokenRecord.model_validate(permit),
        context_id="schedule_run_abc123",
        conversation_id="conv_1",
        started_at_utc=fixtures.NOW,
    )

    gc = GatewayContext(task_context=token)
    assert isinstance(gc.task_context, AuthorityTokenRecord)
    assert gc.model_dump(mode="json")["task_context"] == token

    gc_run = GatewayContext(schedule_run=run)
    assert gc_run.schedule_run is not None
    assert gc_run.schedule_run.permit.model_dump(mode="json") == permit

    mcp_req = MCPInvocationRequestV1(
        record_digest="a" * 64,
        operation_name="list",
        tool_use_id="tu_1",
        principal=PrincipalDescriptorV1(
            principal_id="agent_1",
            principal_type=PrincipalType.AGENT,
            named_agent_type="ResearchAgent",
            team_id=tv.TEAM,
            on_behalf_of_user_id="usr_alice",
        ),
        task_context=token,
    )
    assert isinstance(mcp_req.task_context, AuthorityTokenRecord)

    legacy_signed = {
        "context": {"context_id": "ctx_old", "issuer": "gateway", "team_id": tv.TEAM},
        "key_id": "gw-1",
        "signature": "sig",
    }
    with pytest.raises(ValidationError):
        GatewayContext.model_validate({"task_context": legacy_signed})
    with pytest.raises(ValidationError):
        ScheduleRunIdentityV1.model_validate(
            {
                "permit": {"permit": {"schedule_id": "s1"}, "key_id": "s-1", "signature": "sig"},
                "context_id": "schedule_run_1",
                "started_at_utc": fixtures.NOW.isoformat(),
            }
        )
    # Non-schedule_run_ context_id rejected on ScheduleRunIdentityV1.
    with pytest.raises(ValidationError):
        ScheduleRunIdentityV1.model_validate(
            {
                "permit": permit,
                "context_id": "message_context_hijack",
                "started_at_utc": fixtures.NOW.isoformat(),
            }
        )


def test_resolve_task_identity_all_cases_and_decision_52() -> None:
    people = tv._default_people()
    schedules = _FakeSchedules(
        ScheduleFacts("sched_1", True, "usr_alice", False, None, runner_type="ResearchAgent"),
        ScheduleFacts("sched_sys", True, None, True, "internal_improvement", runner_type="InternalImprovementAgent"),
        ScheduleFacts("sched_off", False, "usr_alice", False, None, runner_type="ResearchAgent"),
    )
    lookup = ScheduleRunLookup(schedules, people)
    trust = fixtures.trust_store(now=fixtures.NOW)
    verifier = AuthorityTokenVerifier(trust, people=people, clock=lambda: fixtures.NOW)

    # 1. Gateway person task.
    ctx = resolve_task_identity(
        verifier=verifier,
        team_id=tv.TEAM,
        task_context=_gw_token(root_task_id="task_p", request_id="message_context_p"),
        schedule_run=None,
        schedule_runs=lookup,
        now=fixtures.NOW,
    )
    assert ctx.issuer == "gateway"
    assert "gateway" in ctx.issuer_roles
    assert ctx.person_id == "usr_alice"
    assert ctx.person_roles == ("member",)
    assert ctx.root_task_id == "task_p"
    assert ctx.context_id == "message_context_p"
    assert job_schedule_id(ctx, lookup, lead_agent_type="TeamArchitect") is None

    # 2. Scheduled run root-task token (scheduler-signed) -> job_schedule_id returns sched_1.
    sched_ctx = resolve_task_identity(
        verifier=verifier,
        team_id=tv.TEAM,
        task_context=_sched_token(schedule_id="sched_1"),
        schedule_run=None,
        schedule_runs=lookup,
        now=fixtures.NOW,
    )
    assert sched_ctx.issuer == "scheduler"
    assert "scheduler" in sched_ctx.issuer_roles
    assert sched_ctx.schedule_id == "sched_1"
    assert job_schedule_id(sched_ctx, lookup, lead_agent_type="TeamArchitect") == "sched_1"

    # 3. Permit-backed scheduled run identity -> live schedule & person checks pass.
    permit_run = ScheduleRunIdentityV1(
        permit=AuthorityTokenRecord.model_validate(_sched_permit(schedule_id="sched_1", person_ref=tv.ALICE_REF)),
        context_id="schedule_run_run1",
        conversation_id="conv_run1",
        started_at_utc=fixtures.NOW,
    )
    permit_ctx = resolve_task_identity(
        verifier=verifier,
        team_id=tv.TEAM,
        task_context=None,
        schedule_run=permit_run,
        schedule_runs=lookup,
        now=fixtures.NOW,
    )
    assert permit_ctx.issuer == "scheduler"
    assert permit_ctx.person_id == "usr_alice"
    assert permit_ctx.schedule_id == "sched_1"
    assert permit_ctx.context_id == "schedule_run_run1"
    assert permit_ctx.conversation_id == "conv_run1"

    # 4. System work (no person, scheduler-signed).
    sys_ctx = resolve_task_identity(
        verifier=verifier,
        team_id=tv.TEAM,
        task_context=_sched_token(schedule_id="sched_sys", person_ref=None),
        schedule_run=None,
        schedule_runs=lookup,
        now=fixtures.NOW,
    )
    assert sys_ctx.person_id is None and sys_ctx.schedule_id == "sched_sys"

    # 5. Bare permit presented as task_context is refused (C01).
    with pytest.raises(IdentityRejected) as exc_bare:
        resolve_task_identity(
            verifier=verifier,
            team_id=tv.TEAM,
            task_context=_sched_permit(schedule_id="sched_1"),
            schedule_run=None,
            schedule_runs=lookup,
            now=fixtures.NOW,
        )
    assert exc_bare.value.code == REFUSAL_PERMIT_NOT_ALLOWED

    # 6. Disabled schedule refuses permit run.
    off_run = ScheduleRunIdentityV1(
        permit=AuthorityTokenRecord.model_validate(_sched_permit(schedule_id="sched_off", person_ref=tv.ALICE_REF)),
        context_id="schedule_run_off",
        started_at_utc=fixtures.NOW,
    )
    with pytest.raises(IdentityRejected, match="turned off") as exc_off:
        resolve_task_identity(
            verifier=verifier, team_id=tv.TEAM, task_context=None, schedule_run=off_run, schedule_runs=lookup, now=fixtures.NOW
        )
    assert exc_off.value.code == REFUSAL_INVALID_PERMIT

    # 7. Role revocation while token lasts takes effect immediately; disabled owner is refused.
    people.put(
        TeamPersonV1(
            team_id=tv.TEAM,
            person_id="usr_alice",
            person_ref=tv.ALICE_REF,
            standing=PersonStanding.ACTIVE,
            roles=(),
            changed_at_utc=fixtures.NOW,
        )
    )
    ctx_no_roles = resolve_task_identity(
        verifier=verifier, team_id=tv.TEAM, task_context=_gw_token(), schedule_run=None, schedule_runs=lookup, now=fixtures.NOW
    )
    assert ctx_no_roles.person_roles == ()

    people.put(
        TeamPersonV1(
            team_id=tv.TEAM,
            person_id="usr_alice",
            person_ref=tv.ALICE_REF,
            standing=PersonStanding.DISABLED,
            roles=(),
            changed_at_utc=fixtures.NOW,
        )
    )
    with pytest.raises(IdentityRejected) as exc_disabled:
        resolve_task_identity(
            verifier=verifier, team_id=tv.TEAM, task_context=_gw_token(), schedule_run=None, schedule_runs=lookup, now=fixtures.NOW
        )
    assert exc_disabled.value.code == REFUSAL_PERSON_INACTIVE

    # Historical attribution still identifies the disabled person & expired token.
    assert reported_task_person(verifier=verifier, team_id=tv.TEAM, task_context=_gw_token(), schedule_run=None) == "usr_alice"
    rep_run = reported_schedule_run(verifier=verifier, team_id=tv.TEAM, task_context=None, schedule_run=permit_run)
    assert rep_run is not None and rep_run.schedule_id == "sched_1" and rep_run.person_id == "usr_alice"

    # Re-enable Alice for Decision 52 test.
    people.put(
        TeamPersonV1(
            team_id=tv.TEAM,
            person_id="usr_alice",
            person_ref=tv.ALICE_REF,
            standing=PersonStanding.ACTIVE,
            roles=("member",),
            changed_at_utc=fixtures.NOW,
        )
    )
    short_token = _gw_token(issued=fixtures.NOW, expires_at=fixtures.NOW + timedelta(hours=1))
    after_expiry = fixtures.NOW + timedelta(hours=3)
    trust.add(fixtures.current_revocations(after_expiry, sequence=300), now=after_expiry)
    with pytest.raises(IdentityRejected) as exc_exp:
        resolve_task_identity(
            verifier=verifier, team_id=tv.TEAM, task_context=short_token, schedule_run=None, schedule_runs=lookup, now=after_expiry
        )
    assert exc_exp.value.code == REFUSAL_EXPIRED

    queued_ok = resolve_task_identity(
        verifier=verifier,
        team_id=tv.TEAM,
        task_context=short_token,
        schedule_run=None,
        schedule_runs=lookup,
        now=after_expiry,
        valid_at=fixtures.NOW + timedelta(minutes=30),
        reached_at=fixtures.NOW + timedelta(minutes=30),
    )
    assert queued_ok.person_id == "usr_alice"


def test_sent_files_access_enforces_gateway_role_and_conversation_consistency_c05() -> None:
    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=tv._default_people(), clock=lambda: fixtures.NOW)
    task = Task(
        task_id="task_files_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="inspect",
        attachments=[
            UserTaskAttachment(
                attachment_id="att_1",
                filename="report.txt",
                media_type="text/plain",
                size_bytes=12,
                sha256="a" * 64,
                local_path_value="/uploads/report.txt",
            )
        ],
    )
    ctx_id = request_context_id(task)

    gw_verified = verifier.verify_root_task(
        _gw_token(person_ref=tv.ALICE_REF, root_task_id=task.task_id, request_id=ctx_id),
        team_id=tv.TEAM,
        now=fixtures.NOW,
    )
    gw_sender = verified_token_to_task_context(gw_verified)
    assert gw_sender.conversation_id is None  # Protocol 1 AuthorityToken carries no conversation_id (C05)

    # Matching message_conversation_id succeeds.
    ok = sent_files_access(
        sender=gw_sender,
        task_id=task.task_id,
        conversation_id="conv_A",
        person_id="usr_alice",
        message_context_id=ctx_id,
        message_conversation_id="conv_A",
    )
    assert ok.allowed

    # Missing or mismatched message_conversation_id is refused as another_conversation (C05).
    missing_conv = sent_files_access(
        sender=gw_sender,
        task_id=task.task_id,
        conversation_id="conv_A",
        person_id="usr_alice",
        message_context_id=ctx_id,
        message_conversation_id=None,
    )
    assert not missing_conv.allowed and missing_conv.reason == "another_conversation"

    wrong_conv = sent_files_access(
        sender=gw_sender,
        task_id=task.task_id,
        conversation_id="conv_A",
        person_id="usr_alice",
        message_context_id=ctx_id,
        message_conversation_id="conv_B",
    )
    assert not wrong_conv.allowed and wrong_conv.reason == "another_conversation"

    # Scheduler-signed token (even for an admin!) is refused as sender_not_gateway (C04).
    sched_verified = verifier.verify_root_task(
        _sched_token(person_ref=tv.ADMIN_REF, root_task_id=task.task_id, request_id=ctx_id, schedule_id="sched_1"),
        team_id=tv.TEAM,
        now=fixtures.NOW,
    )
    sched_sender = verified_token_to_task_context(sched_verified)
    refused = sent_files_access(
        sender=sched_sender,
        task_id=task.task_id,
        conversation_id="conv_A",
        person_id="usr_admin",
        message_context_id=ctx_id,
        message_conversation_id="conv_A",
    )
    assert not refused.allowed and refused.reason == "sender_not_gateway"


def test_gateway_only_call_sites_refuse_scheduler_tokens_even_for_admins_c04(monkeypatch: pytest.MonkeyPatch) -> None:
    """C04: valid scheduler-signed admin tokens are refused at every gateway-only call site."""

    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=tv._default_people(), clock=lambda: fixtures.NOW)
    sched_admin_token = _sched_token(
        person_ref=tv.ADMIN_REF,
        root_task_id="task_admin_sched",
        request_id="task_context_admin_sched",
        schedule_id="sched_admin",
    )
    gw_admin_token = _gw_token(
        person_ref=tv.ADMIN_REF,
        root_task_id="task_admin_gw",
        request_id="message_context_admin_gw",
    )

    with use_process_verifier(verifier):
        # 1. manager_control_runtime.verified_command_person
        dummy_logger = SimpleNamespace(warning=lambda *a, **k: None)
        agent = SimpleNamespace(id="lead_1", team_id=tv.TEAM, _logger=dummy_logger)
        meta_sched = MessageMeta(gateway_context=GatewayContext(task_context=sched_admin_token))
        person, refusal = manager_control_runtime.verified_command_person(agent, meta_sched)
        assert person is None and refusal == "sender_not_gateway"

        meta_gw = MessageMeta(gateway_context=GatewayContext(task_context=gw_admin_token))
        person_ok, refusal_ok = manager_control_runtime.verified_command_person(agent, meta_gw)
        assert refusal_ok is None and person_ok is not None and person_ok.canonical_user_id == "usr_admin"

        # 2. working_requests.turn_request_id
        tasks = SimpleNamespace(get_task_info=lambda tid: SimpleNamespace(meta=meta_sched if tid == "t_s" else meta_gw))
        wr_agent = SimpleNamespace(id="lead_1", task_manager=tasks, _running_turns={})
        monkeypatch.setattr(
            "custom_agents.access_runtime.runtime_access",
            lambda _a: SimpleNamespace(verifier=verifier, team_id=tv.TEAM, schedule_runs=None),
        )
        assert working_requests.turn_request_id(wr_agent, "t_s") is None
        assert working_requests.turn_request_id(wr_agent, "t_g") == "task_admin_gw"

        # 3. change_writers.verified_work
        sched_ctx = verified_token_to_task_context(
            verifier.verify_root_task(sched_admin_token, team_id=tv.TEAM, now=fixtures.NOW)
        )
        gw_ctx = verified_token_to_task_context(
            verifier.verify_root_task(gw_admin_token, team_id=tv.TEAM, now=fixtures.NOW)
        )
        monkeypatch.setattr("custom_agents.access_runtime.verified_task_context", lambda _a: sched_ctx)
        work_sched = change_writers.verified_work(agent)
        assert work_sched.person_id == "usr_admin" and work_sched.request_id == ""

        monkeypatch.setattr("custom_agents.access_runtime.verified_task_context", lambda _a: gw_ctx)
        work_gw = change_writers.verified_work(agent)
        assert work_gw.person_id == "usr_admin" and work_gw.request_id == "message_context_admin_gw"


def test_change_ledger_rechecks_authority_token_and_protects_request_identity() -> None:
    trust = fixtures.trust_store(now=fixtures.NOW)
    after_expiry = fixtures.NOW + timedelta(hours=3)
    trust.add(fixtures.current_revocations(after_expiry, sequence=310), now=after_expiry)
    verifier = AuthorityTokenVerifier(trust, people=tv._default_people(), clock=lambda: after_expiry)

    conn = sqlite3.connect(":memory:")
    ensure_ledger_tables(conn, SQLITE)

    gw_token = _gw_token(
        person_ref=tv.ALICE_REF,
        root_task_id="task_led_1",
        request_id="message_context_led_1",
        issued=fixtures.NOW,
        expires_at=fixtures.NOW + timedelta(hours=1),
    )
    sched_token = _sched_token(person_ref=tv.ALICE_REF, schedule_id="sched_1")
    task_open = TaskSnapshotRecord(
        task_id="task_led_1",
        state=ProgressState.IN_PROGRESS,
        owner_person_id="usr_alice",
        task=Task(task_id="task_led_1", task_type=MessageMetaType.REQUEST, problem_statement="run"),
        header=MessageHeader(from_agent_id="gw"),
        meta=MessageMeta(gateway_context=GatewayContext(task_context=gw_token)),
    )
    # Expired gateway token still opens/closes historical request state when signature + revocations check out.
    record_task_request_state(conn, SQLITE, team_id=tv.TEAM, task=task_open, now=after_expiry, verifier=verifier)
    row = conn.execute("SELECT state FROM change_requests WHERE request_id=?", ("message_context_led_1",)).fetchone()
    assert row is not None and row[0] == "open"

    # Scheduler token is ignored by the request ledger (does not open a person request).
    sched_snap = task_open.model_copy(
        update={
            "task_id": "task_sched_1",
            "meta": MessageMeta(gateway_context=GatewayContext(task_context=sched_token)),
        }
    )
    record_task_request_state(conn, SQLITE, team_id=tv.TEAM, task=sched_snap, now=after_expiry, verifier=verifier)
    assert conn.execute("SELECT COUNT(*) FROM change_requests").fetchone()[0] == 1

    # Stripping or swapping the admitted gateway token is refused by require_same_request_identity.
    stripped = task_open.model_copy(update={"meta": MessageMeta(gateway_context=GatewayContext())})
    with pytest.raises(IdentityRejected):
        require_same_request_identity(task_open, stripped)

    # Terminal save closes the request.
    task_done = task_open.model_copy(update={"state": ProgressState.COMPLETED})
    require_same_request_identity(task_open, task_done)
    record_task_request_state(conn, SQLITE, team_id=tv.TEAM, task=task_done, now=after_expiry, verifier=verifier)
    row2 = conn.execute("SELECT state FROM change_requests WHERE request_id=?", ("message_context_led_1",)).fetchone()
    assert row2 is not None and row2[0] == "ended"


def test_nats_adapter_populates_stored_at_and_sets_aside_legacy_context_immediately_c07() -> None:
    adapter = NatsTransportAdapter(config=SimpleNamespace(team_id=tv.TEAM), mapper=NatsSubjectMapper(team_id=tv.TEAM))

    # 1. Valid envelope with protocol 1 token populates InboundDelivery.stored_at from msg.metadata.timestamp.
    env = MessageEnvelope(
        msg_header=MessageHeader(from_agent_id="gw"),
        msg_content=Task(task_id="t_stored", task_type=MessageMetaType.REQUEST, problem_statement="hi"),
        msg_meta=MessageMeta(gateway_context=GatewayContext(task_context=_gw_token())),
    )
    stored_ts = datetime(2026, 11, 26, 4, 0, 0, tzinfo=timezone.utc)
    fake_msg = SimpleNamespace(
        data=env.model_dump_json().encode("utf-8"),
        subject="ua.agents.test",
        metadata=SimpleNamespace(timestamp=stored_ts, num_delivered=1, sequence=SimpleNamespace(stream=42)),
        headers={},
    )
    delivery = adapter._delivery_from_msg(fake_msg, logical_route="route_1", max_deliver=5, ack_wait_seconds=30.0)
    assert delivery.stored_at == stored_ts

    # 2. Queued legacy SignedTaskContextV1 envelope is set aside ONCE and termed on first delivery (C07).
    legacy_payload = json.dumps(
        {
            "msg_header": {"message_class": "MessageHeader", "message_id": "msg_legacy_1"},
            "msg_content": {"message_class": "Task", "task_id": "task_leg", "task_type": "request", "problem_statement": "old"},
            "msg_meta": {
                "gateway_context": {
                    "task_context": {
                        "context": {"context_id": "ctx_old", "issuer": "gateway", "team_id": tv.TEAM},
                        "key_id": "gw-old",
                        "signature": "oldsig",
                    }
                }
            },
        }
    ).encode("utf-8")
    set_aside_calls: list[dict[str, Any]] = []

    async def _fake_set_aside(msg: Any, *, reason: str, error: str, subscription_name: str) -> bool:
        set_aside_calls.append({"reason": reason, "error": error, "subscription": subscription_name})
        return True

    adapter._set_aside = _fake_set_aside  # type: ignore[method-assign]
    term_mock = AsyncMock()
    nak_mock = AsyncMock()
    legacy_msg = SimpleNamespace(
        data=legacy_payload,
        subject="ua.agents.test",
        metadata=SimpleNamespace(timestamp=stored_ts, num_delivered=1, sequence=SimpleNamespace(stream=99)),
        headers={},
        term=term_mock,
        nak=nak_mock,
    )
    decoded = asyncio.run(
        adapter._decode_or_settle(
            legacy_msg,
            subscription_name="sub_lead",
            logical_route="route_lead",
            max_deliver=5,
            ack_wait_seconds=30.0,
            jetstream=True,
        )
    )
    assert decoded is None
    assert len(set_aside_calls) == 1
    assert "legacy_signed_context:" in set_aside_calls[0]["error"]
    term_mock.assert_awaited_once()
    nak_mock.assert_not_awaited()


def test_dead_letter_person_matching_finds_authority_token_and_permit_person_refs() -> None:
    env_token = MessageEnvelope(
        msg_header=MessageHeader(from_agent_id="gw"),
        msg_content=Task(task_id="t_dlq_1", task_type=MessageMetaType.REQUEST, problem_statement="hi"),
        msg_meta=MessageMeta(gateway_context=GatewayContext(task_context=_gw_token(person_ref=tv.ALICE_REF))),
    ).model_dump_json().encode("utf-8")
    assert raw_copy_names_person(env_token, person_ref=tv.ALICE_REF) is True
    assert raw_copy_names_person(env_token, person_ref=tv.BOB_REF) is False

    env_permit = MessageEnvelope(
        msg_header=MessageHeader(from_agent_id="sched"),
        msg_content=Task(task_id="t_dlq_2", task_type=MessageMetaType.REQUEST, problem_statement="job"),
        msg_meta=MessageMeta(
            gateway_context=GatewayContext(
                schedule_run=ScheduleRunIdentityV1(
                    permit=AuthorityTokenRecord.model_validate(_sched_permit(person_ref=tv.BOB_REF)),
                    context_id="schedule_run_dlq2",
                    started_at_utc=fixtures.NOW,
                )
            )
        ),
    ).model_dump_json().encode("utf-8")
    assert raw_copy_names_person(env_permit, person_ref=tv.BOB_REF) is True
    assert raw_copy_names_person(env_permit, person_ref=tv.ALICE_REF) is False


def test_strands_main_verified_deferred_task_owner_verifies_token_and_fails_closed(tmp_path: Path) -> None:
    import strands_main

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "op.db")}})
    ctx = strands_main.OperatorContext(
        storage_settings=storage,
        db_path=str(tmp_path / "op.db"),
        team_id=tv.TEAM,
        named_agent_type="TeamArchitect",
        is_team_lead=True,
        owner_agent_name="TeamArchitect",
        instance_root=tmp_path,
    )
    meta = MessageMeta(gateway_context=GatewayContext(canonical_user_id="usr_alice", task_context=_gw_token()))

    # Without bootstrap bundle or verifier, fails closed (returns None, never trusts canonical_user_id).
    assert strands_main._verified_deferred_task_owner(ctx, "task_op_1", meta) is None

    # With active protocol 1 verifier, resolves verified person from token.
    verifier = AuthorityTokenVerifier(fixtures.trust_store(), people=tv._default_people(), clock=lambda: fixtures.NOW)
    with use_process_verifier(verifier):
        assert strands_main._verified_deferred_task_owner(ctx, "task_op_1", meta) == "usr_alice"


# -- C12: bootstrap-only on-demand fetch per verifying process ------------------------------------


def _setup_bootstrap_agent_host(
    tmp_path: Path, *, runtime: str, roles: tuple[str, ...], agent_type: str
) -> tuple[AgentProcessHost, Any, str]:
    gw_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    gw_key = json.loads(gw_raw)["key"]
    entries = {gw_key: gw_raw}
    host, _live, records = tv._build_bootstrap_live_host(tmp_path, entries, clock_fn=lambda: fixtures.NOW)
    profile = HostProfile(runtime, (tv.TEAM,), roles, agent_types=(agent_type,), session_limit=4)
    proc = AgentProcessHost(InstanceLayout.at(tmp_path), profile, tv.TEAM)
    proc.host = host
    return proc, records, gw_key


def _seed_people_in_storage(tmp_path: Path) -> Any:
    from storage.access_repository import AccessRepository

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
    repo = AccessRepository(storage)
    repo.record_person(
        TeamPersonV1(
            team_id=tv.TEAM,
            person_id="usr_alice",
            person_ref=tv.ALICE_REF,
            standing=PersonStanding.ACTIVE,
            roles=("member",),
            changed_at_utc=fixtures.NOW,
        )
    )
    return storage, repo


def _minimal_agent_for_admission(tmp_path: Path, storage: Any, *, is_team_lead: bool, agent_type: str) -> Any:
    admitted: dict[str, Any] = {}

    class _Ops:
        async def run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

    class _TaskManager:
        storage_settings = storage
        operations_runtime = _Ops()

        def get_task_info(self, task_id: str) -> Any:
            return admitted.get(task_id)

        def is_buffer_full(self) -> bool:
            return False

        def add_task(self, task: Task, header: MessageHeader, meta: MessageMeta, **kwargs: Any) -> bool:
            admitted[task.task_id] = SimpleNamespace(
                task=task, header=header, meta=meta, owner_person_id=kwargs.get("owner_person_id")
            )
            return True

    logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
    return SimpleNamespace(
        id=f"agent_{agent_type.lower()}",
        name=agent_type,
        team_id=tv.TEAM,
        named_agent_type=agent_type,
        is_team_lead=is_team_lead,
        access_role="",
        instance_root=str(tmp_path),
        authorization_config={"enabled": False},
        task_manager=_TaskManager(),
        _conversation_store=None,
        _logger=logger,
        _ensure_request_timezone=lambda meta, **_: meta,
        _admitted=admitted,
    )


def test_bootstrap_only_lead_host_admission_fetches_gateway_cert_on_demand_c12(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C12: lead host starting from bootstrap-only trust fetches gateway-1 on first task admission."""

    storage, _repo = _seed_people_in_storage(tmp_path)
    proc, records, gw_key = _setup_bootstrap_agent_host(
        tmp_path / "lead_host", runtime="lead", roles=("lead_host",), agent_type="TeamArchitect"
    )
    agent = _minimal_agent_for_admission(tmp_path, storage, is_team_lead=True, agent_type="TeamArchitect")

    # Stub non-identity admission side-effects so _handle_task_request exercises real async identity admission.
    monkeypatch.setattr("custom_agents.task_message_runtime._custody_for_new_task", AsyncMock(return_value=SimpleNamespace(admit=True, takeover_from=None, reason="free")))
    async def _admit_immediate(_s: Any, _c: Any, admit: Any, *_a: Any) -> Any:
        return await admit(None)

    monkeypatch.setattr("custom_agents.task_message_runtime._admit_with_custody", _admit_immediate)
    monkeypatch.setattr("custom_agents.task_message_runtime.apply_redispatch_notice", AsyncMock(return_value=True))
    monkeypatch.setattr("custom_agents.task_message_runtime.send_task_claim", AsyncMock())
    monkeypatch.setattr("custom_agents.task_message_runtime.check_queue_pressure", AsyncMock())
    monkeypatch.setattr("custom_agents.task_message_runtime.schedule_pending_work", lambda _a: None)

    token = _gw_token(root_task_id="task_lead_boot", request_id="message_context_lead_boot")
    task = Task(task_id="task_lead_boot", task_type=MessageMetaType.REQUEST, problem_statement="hello")
    meta = MessageMeta(gateway_context=GatewayContext(canonical_user_id="usr_alice", task_context=token))

    # Align verifier clock with fixture timestamp.
    async def _run() -> None:
        proc.loop = asyncio.get_running_loop()
        configure_process_host(proc)
        try:
            access_runtime.runtime_access(agent).verifier._clock = lambda: fixtures.NOW
            await _handle_task_request(
                agent,
                task,
                MessageHeader(from_agent_id="gw"),
                meta,
                message_stored_at=fixtures.NOW,
            )
        finally:
            configure_process_host(None)

    asyncio.run(_run())
    assert records.reads == [gw_key]
    assert agent._admitted["task_lead_boot"].owner_person_id == "usr_alice"


def test_bootstrap_only_worker_delegated_admission_warms_trust_for_sync_tool_hook_c12(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C12: worker host fetches gateway-1 at delegated-task admission so subsequent sync tool hooks succeed."""

    storage, _repo = _seed_people_in_storage(tmp_path)
    proc, records, gw_key = _setup_bootstrap_agent_host(
        tmp_path / "worker_host", runtime="worker", roles=("worker_host",), agent_type="ResearchAgent"
    )
    worker = _minimal_agent_for_admission(tmp_path, storage, is_team_lead=False, agent_type="ResearchAgent")

    monkeypatch.setattr("custom_agents.task_message_runtime._custody_for_new_task", AsyncMock(return_value=SimpleNamespace(admit=True, takeover_from=None, reason="free")))
    async def _admit_immediate(_s: Any, _c: Any, admit: Any, *_a: Any) -> Any:
        return await admit(None)

    monkeypatch.setattr("custom_agents.task_message_runtime._admit_with_custody", _admit_immediate)
    monkeypatch.setattr("custom_agents.task_message_runtime.apply_redispatch_notice", AsyncMock(return_value=True))
    monkeypatch.setattr("custom_agents.task_message_runtime.send_task_claim", AsyncMock())
    monkeypatch.setattr("custom_agents.task_message_runtime.check_queue_pressure", AsyncMock())
    monkeypatch.setattr("custom_agents.task_message_runtime.schedule_pending_work", lambda _a: None)

    token = _gw_token(root_task_id="task_root_1", request_id="message_context_root_1")
    subtask = Task(
        task_id="task_worker_sub_1",
        parent_task_id="task_root_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="delegated work",
    )
    meta = MessageMeta(gateway_context=GatewayContext(canonical_user_id="usr_alice", task_context=token))

    async def _run() -> str | None:
        proc.loop = asyncio.get_running_loop()
        configure_process_host(proc)
        try:
            access_runtime.runtime_access(worker).verifier._clock = lambda: fixtures.NOW
            await _handle_task_request(
                worker,
                subtask,
                MessageHeader(from_agent_id="lead_1"),
                meta,
                message_stored_at=fixtures.NOW,
            )
            # Now simulate the synchronous tool-call hook reading verified_person_id on the worker.
            with task_work("task_worker_sub_1"):
                return access_runtime.verified_person_id(worker)
        finally:
            configure_process_host(None)

    person_from_sync_hook = asyncio.run(_run())
    assert records.reads == [gw_key]
    assert worker._admitted["task_worker_sub_1"].owner_person_id == "usr_alice"
    assert person_from_sync_hook == "usr_alice"


@pytest.mark.asyncio
async def test_bootstrap_only_broker_execute_handler_fetches_gateway_cert_on_demand_c12(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C12: broker /v1/tools/{provider} handler awaits async issuer fetch before checking the gate."""

    gw_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    gw_key = json.loads(gw_raw)["key"]
    layout = InstanceLayout.at(tmp_path / "broker")
    persistent = PersistentTrustStore.open(
        tv._bootstrap_bundle_for_example(include_revocations=True),
        layout.reader_trust_dir("secure-tool-broker"),
        fixtures.NOW,
    )
    records = tv._TestStateRecords({gw_key: gw_raw}, persistent)
    reader = TrustReader(persistent, records, reader_id="secure-tool-broker", clock=lambda: fixtures.NOW)

    gate = BrokerAccessGate(
        catalogs=CatalogSource(None),
        verifier=AuthorityTokenVerifier(reader, people=tv._default_people(), clock=lambda: fixtures.NOW),
        team_id=tv.TEAM,
        access=None,
    )
    service = SecureToolBrokerService(token=BROKER_ROOT, access_gate=gate)

    import contextlib

    class _Completed:
        returncode = 0
        stdout = "[]"
        stderr = ""

    monkeypatch.setattr(
        "secure_tool_broker.executors.signed_in_google", lambda *_args, **_kwargs: contextlib.nullcontext({})
    )
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(tmp_path / "broker_credentials"))
    monkeypatch.setattr("secure_tool_broker.executors.shutil.which", lambda _name: "/usr/local/bin/gog")
    monkeypatch.setattr("secure_tool_broker.executors.subprocess.run", lambda *_a, **_k: _Completed())

    token = _gw_token(root_task_id="task_broker_exec", request_id="message_context_broker_exec")
    payload = {
        "provider": "google",
        "action": "gog.exec",
        "params": {"argv": ["gmail", "search", "newer_than:1d"]},
        "reason": "check mail",
        "context": {
            "canonical_user_id": "usr_alice",
            "principal_type": "user",
            "team_id": tv.TEAM,
            "agent_id": "agent_1",
            "named_agent_type": "GeneralAssistant",
            "task_id": "task_broker_exec",
            "task_context": token,
        },
    }
    async with TestClient(TestServer(service.build_app())) as client:
        resp = await client.post("/v1/tools/google", headers=caller_headers(CALLER_MANAGER), json=payload)
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["status"] == "success"
    assert records.reads == [gw_key]


@pytest.mark.asyncio
async def test_bootstrap_only_broker_mcp_invoke_handler_fetches_gateway_cert_on_demand_c12(tmp_path: Path) -> None:
    """C12: BrokerMCPRuntime._require_access fetches missing gateway cert once on demand before checking MCP access."""

    from secure_tool_broker.mcp_runtime import BrokerMCPRuntime

    gw_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    gw_key = json.loads(gw_raw)["key"]
    layout = InstanceLayout.at(tmp_path / "broker_mcp")
    persistent = PersistentTrustStore.open(
        tv._bootstrap_bundle_for_example(include_revocations=True),
        layout.reader_trust_dir("secure-tool-broker"),
        fixtures.NOW,
    )
    records = tv._TestStateRecords({gw_key: gw_raw}, persistent)
    reader = TrustReader(persistent, records, reader_id="secure-tool-broker", clock=lambda: fixtures.NOW)

    gate = BrokerAccessGate(
        catalogs=CatalogSource(None),
        verifier=AuthorityTokenVerifier(reader, people=tv._default_people(), clock=lambda: fixtures.NOW),
        team_id=tv.TEAM,
        access=None,
    )
    runtime = BrokerMCPRuntime.__new__(BrokerMCPRuntime)
    runtime._access_gate = gate
    runtime._clients = {}

    token = _gw_token(root_task_id="task_broker_mcp", request_id="message_context_broker_mcp")
    mcp_payload = MCPInvocationRequestV1(
        record_digest="b" * 64,
        operation_name="search_docs",
        tool_use_id="tu_mcp_1",
        task_id="task_broker_mcp",
        principal=PrincipalDescriptorV1(
            principal_id="agent_1",
            principal_type=PrincipalType.AGENT,
            named_agent_type="ResearchAgent",
            team_id=tv.TEAM,
            on_behalf_of_user_id="usr_alice",
        ),
        task_context=token,
    )
    record = SimpleNamespace(
        connection_id="conn_1",
        descriptor=SimpleNamespace(connection_name="docs"),
    )
    link = await runtime._require_access(BrokerCaller(CALLER_MANAGER), record, "search_docs", mcp_payload)
    assert link is None
    assert records.reads == [gw_key]


@pytest.mark.asyncio
async def test_resolve_task_identity_async_rejects_both_without_fetching(tmp_path: Path) -> None:
    gw_raw = (fixtures.TRUST / "certificates" / "gateway-1.json").read_bytes()
    gw_key = json.loads(gw_raw)["key"]
    _host, live, records = tv._build_bootstrap_live_host(tmp_path / "h", {gw_key: gw_raw}, clock_fn=lambda: fixtures.NOW)
    verifier = AuthorityTokenVerifier(live, people=tv._default_people(), clock=lambda: fixtures.NOW)
    run = ScheduleRunIdentityV1(
        permit=AuthorityTokenRecord.model_validate(_sched_permit()),
        context_id="schedule_run_both",
        started_at_utc=fixtures.NOW,
    )
    with pytest.raises(IdentityRejected, match="both"):
        await resolve_task_identity_async(
            verifier=verifier,
            team_id=tv.TEAM,
            task_context=_gw_token(),
            schedule_run=run,
            schedule_runs=None,
            now=fixtures.NOW,
        )
    assert records.reads == []

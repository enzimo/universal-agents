"""Live protocol 1 authority-token tests on nats-server 2.15+ under real generated operator-mode permissions (A8).

Runs real ``InstallationAuthority``, real ``team-services`` (``serve_team`` with ``ScheduleOperations``
and ``SchedulerOperations`` holding roles ``team_services`` and ``scheduler``), real ``GatewayMessageHost``,
real ``MessageHost`` worker/lead hosts with real agent sessions, and real Cedar access decisions
(``custom_agents.access_runtime.decide_call`` + ``target/debug/ua-cedar-authorizer``) against an
isolated operator-mode NATS broker whose per-role user JWTs come directly from
``contracts/messages/v1/generated/permissions/*.json``:

1. A person's task's tool call is decided for that person from the gateway-signed root-task token
   (allowed for a person holding the grant, denied for another person without it), while forged,
   other-team, expired, and live-revoked-issuer tokens are refused.
2. A scheduled run gets its token from team services via ``run_context.sign`` on ``svc.scheduler``
   and its tool calls are decided for the schedule's owner; a worker session that does not hold
   the schedule claim is refused ``claim_not_held``; a system-work schedule gets a scheduler token
   naming ``schedule_id`` and no person; the gateway cannot publish to ``svc.scheduler`` under
   operator-mode NATS permissions.
3. ``schedules.install`` over ``svc.team`` from the gateway (command token) and from an agent's
   task (gateway-signed root-task token) installs vouched schedules with scheduler-signed permits
   and atomic idempotent replies.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nats
import pytest

from access_catalog.catalog import load_catalog
from access_control.authority_verifier import (
    AuthorityTokenVerifier,
    IdentityRejected,
    REFUSAL_BAD_SIGNATURE,
    REFUSAL_EXPIRED,
    REFUSAL_REVOKED_ISSUER,
    REFUSAL_WRONG_TEAM,
    use_process_verifier,
)
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService
from access_control.entities import PersonPrincipal
from access_control.people import PERSON_ROLE_MEMBER, PERSON_ROLE_OWNER
from access_control.people_directory import PeopleDirectory
from access_control.starter_profiles import apply_starter_profile
from assistant_gateway.message_host import GATEWAY_RUNTIME, GatewayMessageHost, gateway_profile
from assistant_gateway.team_services_client import MessageHostTeamServices
from custom_agents.access_runtime import _runtime_access, decide_call
from custom_agents.agent_host import LEAD_HOST_ROLES
from custom_agents.heartbeat_store import HeartbeatStore
from custom_agents.schedule_store import ScheduleStore
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    GrantLifetime,
    GrantV1,
    PersonStanding,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
    TeamPersonV1,
)
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage, ScheduleTaskMemory
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ScheduleManagementClass, SystemScheduleKind
from custom_messages.wire.authority_bindings import AuthorityTokenRecord
from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT
from custom_messages.wire.permissions import HostPlacement, host_permissions, roles_permissions
from custom_transport.protocol1.authority_tokens import root_task_token
from custom_transport.protocol1.delivery_refs import DeliveryRefs
from custom_transport.protocol1.host_keys import load_authority_keys, load_host_keys
from custom_transport.protocol1.host_runtime import MessageHost, TEAM_SERVICES_RUNTIME
from custom_transport.protocol1.joining import HostProfile
from custom_transport.protocol1.live import utc_now
from docker import message_hosts
from installation_authority.service import AUTHORITY_RUNTIME, InstallationAuthority, revocations_key
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings
from team_services.process import TEAM_SERVICES_ROLES, serve_team, team_services_profile
from team_services.scheduler import SchedulerOperations
from team_services.schedules import ScheduleOperations
from tests.nats_operator_support import OperatorAccount
from tests.own_nats_server import own_nats_server
from tests.queue_limits_support import TEST_QUEUE_LIMITS
from tests.task_work_support import bind_task
from tests.test_live_installation_authority import put_record, revocation_entry
from tests.test_live_message_contract import (
    COUNT,
    TRANSPORT,
    Client,
    nats_server,
    role_document,
)
from tests.test_live_team_services import DEFINITION, until_team_services_serve
from custom_transport.protocol1.host_partitions import draw_agent_id
from custom_messages.wire.partitions import OWNED

ROOT = Path(__file__).resolve().parents[1]
CEDAR_BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.nats,
    pytest.mark.skipif(not CEDAR_BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)"),
]

TEAM = "team_auth_live"
OTHER_TEAM = "team_auth_other"
ADA_ID = "usr_ada_live"
ADA_REF = "p-AdaLive000000000000001"
BO_ID = "usr_bo_live"
BO_REF = "p-BoLive0000000000000001"
OTHER_GW_RUNTIME = "gateway-other"
WORKER_RUNTIME = "worker-host-1"
LEAD_RUNTIME = "lead-host-1"


def _worker_profile(team: str = TEAM) -> HostProfile:
    return HostProfile(WORKER_RUNTIME, (team,), ("worker_host",), ("researcher",), 4)


def _lead_profile(team: str = TEAM) -> HostProfile:
    return HostProfile(LEAD_RUNTIME, (team,), LEAD_HOST_ROLES, ("TeamArchitect", "researcher"), 4)


async def _open_owned_session(
    started: MessageHost,
    prefix: str,
    agent_type: str = "researcher",
    *,
    exclude: set[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    assert started.records is not None and started.live is not None
    runtime = started.identity.certificate.runtime
    count = started.live.trust.store.partition_count
    audience = {"installation": started.identity.installation, "target": "team_services", "id": TEAM, "team": TEAM}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15.0
    while True:
        owned: list[int] = []
        for key in await started.records.keys("partition_owner", f"{TEAM}.partition.", team=TEAM):
            stored = await started.records.read_report("partition_owner", key, now=started.clock())
            if stored is not None and stored.record.body.state == OWNED and stored.record.body.host == runtime:
                owned.append(int(stored.record.body.partition))
        if owned:
            agent_id = draw_agent_id(TEAM, sorted(owned), prefix=prefix, partition_count=count, exclude_ids=exclude or ())
            reply = await started.live.request(
                "sessions.open",
                {"request_id": f"open-{agent_id}", "agent": agent_id, "agent_type": agent_type, "definition_sha256": DEFINITION},
                audience,
                team=TEAM,
                timeout=10,
            )
            body = reply.raw["body"]
            if body.get("outcome") != "partition_not_owned":
                return agent_id, body
        if loop.time() >= deadline:
            raise AssertionError(f"host {runtime} never owned a partition of {TEAM}")
        await asyncio.sleep(0.05)


def _other_gateway_profile() -> HostProfile:
    return HostProfile(OTHER_GW_RUNTIME, (OTHER_TEAM,), ("gateway",))


async def _connect_user(
    stack: AsyncExitStack,
    url: str,
    account: OperatorAccount,
    name: str,
    perms: dict[str, Any] | None,
    *,
    runtime: str,
) -> Client:
    user = account.user(name, perms)
    errors: asyncio.Queue[Exception] = asyncio.Queue()
    nc = await nats.connect(
        url,
        inbox_prefix=f"_INBOX.{runtime}".encode(),
        error_cb=errors.put,
        allow_reconnect=False,
        **user.login(),
    )
    stack.push_async_callback(nc.close)
    return Client(nc, errors)


class _LiveCluster:
    """Operator-mode NATS broker + real InstallationAuthority + real team-services + real hosts."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.layout = InstanceLayout.at(tmp_path / "instance")
        self.storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
        self.access = AccessRepository(self.storage)
        now = datetime.now(timezone.utc)
        for person_id, person_ref, roles in (
            (ADA_ID, ADA_REF, (PERSON_ROLE_OWNER,)),
            (BO_ID, BO_REF, (PERSON_ROLE_MEMBER,)),
        ):
            self.access.record_person(
                TeamPersonV1(
                    team_id=TEAM,
                    person_id=person_id,
                    person_ref=person_ref,
                    standing=PersonStanding.ACTIVE,
                    roles=roles,
                    changed_at_utc=now,
                )
            )
        self.people = PeopleDirectory(self.access, TEAM)
        self.schedule_store = ScheduleStore(str(tmp_path / "manager.db"))
        self.heartbeat_store = HeartbeatStore(str(tmp_path / "manager.db"))

        # Prepare real installation authority keys and expected hosts.
        profiles = (
            team_services_profile(TEAM),
            gateway_profile(TEAM),
            _other_gateway_profile(),
            _lead_profile(TEAM),
            _worker_profile(TEAM),
        )
        expected = {p.runtime: message_hosts.expected_host(self.layout, p) for p in profiles}
        message_hosts.prepare_message_host_trust(self.layout, expected, partitions=DEFAULT_PARTITION_COUNT)
        self.auth_keys = load_authority_keys(self.layout)
        self.auth_host_keys = load_host_keys(self.layout, AUTHORITY_RUNTIME)
        self.installation = self.auth_keys.installation

        self.account = OperatorAccount(TRANSPORT["partitions"]["account_mappings"], COUNT)
        self.stop = asyncio.Event()

    def _perms(self, roles: tuple[str, ...], placement: HostPlacement) -> dict[str, Any]:
        if len(roles) == 1:
            return host_permissions(role_document(roles[0]), placement)
        return roles_permissions([role_document(r) for r in roles], placement, partition_count=COUNT)

    async def start(self, stack: AsyncExitStack, url: str) -> dict[str, Any]:
        all_parts = {TEAM: tuple(range(COUNT))}
        auth_client = await _connect_user(
            stack,
            url,
            self.account,
            AUTHORITY_RUNTIME,
            self._perms(
                ("installation_authority",),
                HostPlacement(
                    AUTHORITY_RUNTIME,
                    (TEAM, OTHER_TEAM),
                    installation=self.installation,
                ),
            ),
            runtime=AUTHORITY_RUNTIME,
        )
        ts_client = await _connect_user(
            stack,
            url,
            self.account,
            TEAM_SERVICES_RUNTIME,
            self._perms(
                TEAM_SERVICES_ROLES,
                HostPlacement(
                    TEAM_SERVICES_RUNTIME,
                    (TEAM,),
                    partitions=all_parts,
                    installation=self.installation,
                ),
            ),
            runtime=TEAM_SERVICES_RUNTIME,
        )
        gw_client = await _connect_user(
            stack,
            url,
            self.account,
            GATEWAY_RUNTIME,
            self._perms(
                ("gateway",),
                HostPlacement(
                    GATEWAY_RUNTIME,
                    (TEAM,),
                    edges={TEAM: ("web",)},
                    installation=self.installation,
                ),
            ),
            runtime=GATEWAY_RUNTIME,
        )
        other_gw_client = await _connect_user(
            stack,
            url,
            self.account,
            OTHER_GW_RUNTIME,
            self._perms(
                ("gateway",),
                HostPlacement(
                    OTHER_GW_RUNTIME,
                    (OTHER_TEAM,),
                    edges={OTHER_TEAM: ("web",)},
                    installation=self.installation,
                ),
            ),
            runtime=OTHER_GW_RUNTIME,
        )
        lead_client = await _connect_user(
            stack,
            url,
            self.account,
            LEAD_RUNTIME,
            self._perms(
                LEAD_HOST_ROLES,
                HostPlacement(
                    LEAD_RUNTIME,
                    (TEAM,),
                    partitions=all_parts,
                    pools={TEAM: ("researcher",)},
                    installation=self.installation,
                ),
            ),
            runtime=LEAD_RUNTIME,
        )
        worker_client = await _connect_user(
            stack,
            url,
            self.account,
            WORKER_RUNTIME,
            self._perms(
                ("worker_host",),
                HostPlacement(
                    WORKER_RUNTIME,
                    (TEAM,),
                    partitions=all_parts,
                    pools={TEAM: ("researcher",)},
                    installation=self.installation,
                ),
            ),
            runtime=WORKER_RUNTIME,
        )

        # 1. Start real InstallationAuthority under generated installation_authority JWT.
        authority = InstallationAuthority(
            self.layout,
            self.auth_keys,
            self.auth_host_keys,
            partitions=DEFAULT_PARTITION_COUNT,
            queues=TEST_QUEUE_LIMITS,
        )
        await authority.start(auth_client.nc, teams=[TEAM, OTHER_TEAM])

        # 2. Start real team-services (ScheduleOperations + SchedulerOperations) under
        #    generated team_services + scheduler JWTs.
        ts_host = MessageHost(self.layout, team_services_profile(TEAM), ts_client.nc)
        ts_verifier = AuthorityTokenVerifier(ts_host, people=self.access, installation=self.installation)
        scheduler_ops = SchedulerOperations(
            self.schedule_store,
            self.people,
            TEAM,
            identity_fn=lambda: ts_host.live.identity if ts_host.live is not None else None,
            verifier_fn=lambda: ts_verifier,
            heartbeat_store_fn=lambda: self.heartbeat_store,
            clock=ts_host.clock,
            logger=logging.getLogger("test.live_authority.scheduler"),
        )
        refs = DeliveryRefs.load(self.layout, TEAM)
        schedule_ops = ScheduleOperations(
            self.schedule_store,
            self.people,
            refs,
            lambda person: person.is_admin,
            ts_host.clock,
            logging.getLogger("test.live_authority.schedules"),
            on_schedule_installed=scheduler_ops.issue_permit_for_schedule,
        )
        serving = asyncio.create_task(serve_team(ts_host, TEAM, self.stop, [schedule_ops.family(), scheduler_ops.family()]))
        stack.push_async_callback(lambda: serving)
        stack.callback(self.stop.set)
        await until_team_services_serve(ts_host, serving)

        # 3. Start real GatewayMessageHost (TEAM), real other-team GatewayMessageHost (OTHER_TEAM),
        #    and real lead + worker MessageHosts under their generated operator-mode JWTs.
        gateway = GatewayMessageHost(self.layout, TEAM)
        gateway.start(gw_client.nc)
        stack.push_async_callback(gateway.close)
        await gateway.ready(timeout=20)

        other_gateway = MessageHost(self.layout, _other_gateway_profile(), other_gw_client.nc)
        await other_gateway.start()

        lead_host = MessageHost(self.layout, _lead_profile(TEAM), lead_client.nc)
        await lead_host.start()

        worker_host = MessageHost(self.layout, _worker_profile(TEAM), worker_client.nc)
        await worker_host.start()

        return {
            "authority": authority,
            "auth_client": auth_client,
            "ts_host": ts_host,
            "ts_client": ts_client,
            "gateway": gateway,
            "gw_client": gw_client,
            "other_gateway": other_gateway,
            "lead_host": lead_host,
            "lead_client": lead_client,
            "worker_host": worker_host,
            "worker_client": worker_client,
            "scheduler_ops": scheduler_ops,
        }


def _agent_for_token(
    storage: Any,
    token: Any,
    *,
    agent_type: str = "WebResearchAgent",
    lead: bool = False,
    role: str = "worker",
    task_id: str = "task-live-1",
) -> SimpleNamespace:
    token_record = AuthorityTokenRecord.model_validate(token) if isinstance(token, dict) else token
    meta = SimpleNamespace(gateway_context=SimpleNamespace(task_context=token_record, schedule_run=None), conversation_id="conv-live")
    task_manager = SimpleNamespace(
        storage_settings=storage,
        current_task_id=task_id,
        get_task_info=lambda _tid: SimpleNamespace(meta=meta),
        terminal_callbacks=[],
        release_callbacks=[],
    )
    task_manager.add_task_terminal_callback = task_manager.terminal_callbacks.append
    task_manager.add_task_release_callback = task_manager.release_callbacks.append
    bind_task(task_id)
    return SimpleNamespace(
        id=f"agent-{agent_type}-live",
        named_agent_type=agent_type,
        team_id=TEAM,
        is_team_lead=lead,
        access_role=role,
        authorization_config={"enabled": True, "cedar_binary_path": str(CEDAR_BINARY)},
        task_manager=task_manager,
        instance_root=None,
    )


def _start_operator_broker(account: OperatorAccount, tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    settings = account.server_settings(tmp_path)
    config = tmp_path / "nats.json"
    config.write_text(json.dumps(settings), encoding="utf-8")
    config.chmod(0o600)
    return own_nats_server(nats_server(), tmp_path, "-c", str(config))


@pytest.mark.asyncio
async def test_live_person_task_decision_and_token_refusals_under_operator_mode(tmp_path: Path) -> None:
    """A8: gateway-signed root-task token decides tool calls per person under real Cedar and
    operator-mode NATS; forged, other-team, expired, and live-revoked-issuer tokens are refused."""

    _runtime_access.cache_clear()
    cluster = _LiveCluster(tmp_path)
    access_service = AccessService(
        store=cluster.access,
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(CEDAR_BINARY)),
        team_id=TEAM,
    )
    # Only Ada gets the "trusted" starter profile (granting fn:publishing write via lead hand-down).
    apply_starter_profile(access_service, PersonPrincipal(ADA_ID, TEAM, (PERSON_ROLE_OWNER,)), "trusted", lead_agent_type="TeamArchitect")

    with _start_operator_broker(cluster.account, tmp_path / "nats") as url:
        async with AsyncExitStack() as stack:
            ctx = await cluster.start(stack, url)
            authority: InstallationAuthority = ctx["authority"]
            gateway: GatewayMessageHost = ctx["gateway"]
            other_gateway: MessageHost = ctx["other_gateway"]
            worker_host: MessageHost = ctx["worker_host"]

            verifier = AuthorityTokenVerifier(
                worker_host,
                people=cluster.access,
                installation=cluster.installation,
            )
            with use_process_verifier(verifier):
                try:
                    now = utc_now()
                    # 1. Valid gateway-signed root-task token for Ada vs Bo.
                    ada_token = gateway.sign_root_task_token(
                        root_task_id="task-ada-1",
                        request_id="task_context_ada_live_1",
                        person_ref=ADA_REF,
                    )
                    bo_token = gateway.sign_root_task_token(
                        root_task_id="task-bo-1",
                        request_id="task_context_bo_live_1",
                        person_ref=BO_REF,
                    )
                    # Worker fetches gateway's certificate from live UA1_CERTS over operator-mode NATS.
                    assert await verifier.ensure_issuer_known(ada_token, now=now) is True

                    ada_decision = decide_call(_agent_for_token(cluster.storage, ada_token, task_id="task-ada-1"), "publish_single_page_site", {})
                    assert ada_decision is not None and ada_decision.allowed is True

                    bo_decision = decide_call(_agent_for_token(cluster.storage, bo_token, task_id="task-bo-1"), "publish_single_page_site", {})
                    assert bo_decision is not None and bo_decision.allowed is False
                    assert bo_decision.tool_id == "fn:publishing"

                    # 2. Forged token (tampered signature) is refused.
                    forged = ada_token.model_dump(mode="json")
                    sig = str(forged["signature"])
                    forged["signature"] = ("A" if sig[0] != "A" else "B") + sig[1:]
                    with pytest.raises(IdentityRejected) as forged_exc:
                        verifier.verify_root_task(forged, team_id=TEAM, now=now)
                    assert forged_exc.value.code == REFUSAL_BAD_SIGNATURE
                    forged_decision = decide_call(_agent_for_token(cluster.storage, forged, task_id="task-forged"), "send_message", {})
                    assert forged_decision is not None and forged_decision.allowed is False
                    assert "Nobody verifiably asked" in forged_decision.reason

                    # 3. Other-team token (genuinely signed by OTHER_TEAM's gateway host certificate) is refused.
                    assert other_gateway.live is not None
                    other_token = root_task_token(
                        other_gateway.live.identity,
                        team=OTHER_TEAM,
                        root_task_id="task-other-1",
                        request_id="task_context_other_1",
                        person_ref=ADA_REF,
                        issued=now,
                    )
                    assert await verifier.ensure_issuer_known(other_token, now=now) is True
                    with pytest.raises(IdentityRejected) as team_exc:
                        verifier.verify_root_task(other_token, team_id=TEAM, now=now)
                    assert team_exc.value.code == REFUSAL_WRONG_TEAM
                    other_decision = decide_call(_agent_for_token(cluster.storage, other_token, task_id="task-other"), "send_message", {})
                    assert other_decision is not None and other_decision.allowed is False

                    # 4. Expired token is refused (issued while cert is valid, verified past expires_at + clock skew).
                    assert gateway.host is not None and gateway.host.live is not None
                    expired = root_task_token(
                        gateway.host.live.identity,
                        team=TEAM,
                        root_task_id="task-exp-1",
                        request_id="task_context_exp_1",
                        person_ref=ADA_REF,
                        issued=now,
                        expires_at=now + timedelta(seconds=1),
                    )
                    past_expiry = now + timedelta(minutes=2)
                    with pytest.raises(IdentityRejected) as exp_exc:
                        verifier.verify_root_task(expired, team_id=TEAM, now=past_expiry)
                    assert exp_exc.value.code == REFUSAL_EXPIRED
                    exp_verifier = AuthorityTokenVerifier(
                        worker_host,
                        people=cluster.access,
                        installation=cluster.installation,
                        clock=lambda: past_expiry,
                    )
                    with use_process_verifier(exp_verifier):
                        exp_decision = decide_call(_agent_for_token(cluster.storage, expired, task_id="task-exp"), "send_message", {})
                    assert exp_decision is not None and exp_decision.allowed is False

                    # 5. Revoked-issuer token: revoke gateway's certificate in live UA1_CERTS and refresh worker trust.
                    gw_cert = gateway.host.live.identity.certificate
                    revoked_doc = authority.revocations_record(
                        sequence=authority._revocations["sequence"] + 1,
                        revocation_epoch=int(authority._revocations["body"]["revocation_epoch"]) + 1,
                        entries=[revocation_entry(gw_cert)],
                        issued=utc_now(),
                    )
                    await put_record(ctx["auth_client"].nc, revocations_key(cluster.installation), revoked_doc)
                    await worker_host.refresh_trust()

                    with pytest.raises(IdentityRejected) as rev_exc:
                        verifier.verify_root_task(ada_token, team_id=TEAM, now=utc_now())
                    assert rev_exc.value.code == REFUSAL_REVOKED_ISSUER
                    rev_decision = decide_call(_agent_for_token(cluster.storage, ada_token, task_id="task-revoked"), "publish_single_page_site", {})
                    assert rev_decision is not None and rev_decision.allowed is False

                    # No operator-mode NATS permission violations on any real host connection.
                    for key in ("auth_client", "ts_client", "gw_client", "lead_client", "worker_client"):
                        assert ctx[key].errors.empty(), f"unexpected NATS error on {key}"
                finally:
                    _runtime_access.cache_clear()


@pytest.mark.asyncio
async def test_live_scheduled_run_sign_claim_not_held_and_owner_tool_decision(tmp_path: Path) -> None:
    """A8: run_context.sign on svc.scheduler under operator-mode NATS refuses a non-holder
    worker with claim_not_held, signs a scheduler token for the claim holder, decides tool calls
    for the schedule's owner, signs system-work runs without a person, and forbids the gateway
    from publishing to svc.scheduler."""

    _runtime_access.cache_clear()
    cluster = _LiveCluster(tmp_path)
    # Give schedule sched-ada-live a direct schedule grant for fn:publishing write on Ada's work.
    now = datetime.now(timezone.utc)
    cluster.access.create(
        GrantV1(
            grant_id="grant-sched-live-1",
            team_id=TEAM,
            subject=SubjectRefV1(kind=SubjectKind.SCHEDULE, id=f"{TEAM}/sched-ada-live"),
            for_person_id=ADA_ID,
            target=TargetRefV1(kind=TargetKind.TOOL, id="fn:publishing"),
            level=AccessLevel.WRITE,
            lifetime=GrantLifetime.UNTIL_REVOKED,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=ADA_ID),
            reason="live test schedule grant",
            created_at_utc=now,
        )
    )
    # Seed one vouched user schedule for Ada and one system-work schedule (both due immediately).
    due_past = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    cluster.schedule_store.add(
        ScheduledMessage(
            schedule_id="sched-ada-live",
            schedule_name="Ada's live scheduled job",
            management_class=ScheduleManagementClass.USER,
            claim_team_id=TEAM,
            claim_named_agent_type="researcher",
            interval_ms=60_000,
            next_fire_at=due_past,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Publish Ada's update."),
            payload_meta=MessageMeta(gateway_context=GatewayContext(canonical_user_id=ADA_ID, session_key="web:ada")),
            schedule_memory=ScheduleTaskMemory(),
        ),
        owner_vouched=True,
    )
    cluster.schedule_store.add(
        ScheduledMessage(
            schedule_id="sched-sys-live",
            schedule_name="System cleanup",
            management_class=ScheduleManagementClass.SYSTEM,
            system_schedule_kind=SystemScheduleKind.INTERNAL_IMPROVEMENT,
            claim_team_id=TEAM,
            claim_named_agent_type="researcher",
            interval_ms=60_000,
            next_fire_at=due_past,
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Run cleanup."),
            payload_meta=MessageMeta(),
            schedule_memory=ScheduleTaskMemory(),
        ),
    )

    with _start_operator_broker(cluster.account, tmp_path / "nats") as url:
        async with AsyncExitStack() as stack:
            ctx = await cluster.start(stack, url)
            worker_host: MessageHost = ctx["worker_host"]
            gw_client: Client = ctx["gw_client"]

            # Open two real worker agent sessions on worker_host.
            holder_agent, opened_a = await _open_owned_session(worker_host, "researcher")
            assert opened_a["outcome"] == "ok"
            other_agent, opened_b = await _open_owned_session(worker_host, "researcher", exclude={holder_agent})
            assert opened_b["outcome"] == "ok"

            assert worker_host.live is not None
            for agent_id, opened in ((holder_agent, opened_a), (other_agent, opened_b)):
                sess = opened["session"]
                worker_host.live.trust.add(json.dumps(sess).encode("utf-8"), now=worker_host.clock())
                worker_host.live.identity = worker_host.live.identity.with_session(agent_id, sess["body"]["session"])

            # Gateway is forbidden by operator-mode NATS permissions from publishing to svc.scheduler.
            await gw_client.publish_refused(f"ua.v1.{TEAM}.{GATEWAY_RUNTIME}.svc.scheduler")

            # Claim sched-ada-live as holder_agent.
            claimed = cluster.schedule_store.claim_next_due(
                claimer_agent_id=holder_agent,
                claimer_named_agent_type="researcher",
                claimer_team_id=TEAM,
                claim_ttl_seconds=120,
                exclude_schedule_ids=("sched-sys-live",),
            )
            assert claimed is not None and claimed["schedule_id"] == "sched-ada-live"
            claim_token = str(claimed["claim_token"])
            job_id = "sched-ada-live"
            scheduler_audience = {
                "installation": cluster.installation,
                "team": TEAM,
                "target": "team_services",
                "id": TEAM,
            }

            # 1. Worker without the claim (other_agent) is refused claim_not_held.
            refused = await worker_host.live.request(
                "run_context.sign",
                {
                    "request_id": "rcs-not-holder",
                    "schedule_id": "sched-ada-live",
                    "claim_token": claim_token,
                    "scheduled_job_id": job_id,
                    "conversation_id": "conv-sched-1",
                },
                scheduler_audience,
                team=TEAM,
                agent=other_agent,
                timeout=10,
            )
            assert refused.raw["body"]["outcome"] == "claim_not_held"

            # 2. Worker holding the claim (holder_agent) receives a scheduler-signed token for Ada.
            signed = await worker_host.live.request(
                "run_context.sign",
                {
                    "request_id": "rcs-holder-ok",
                    "schedule_id": "sched-ada-live",
                    "claim_token": claim_token,
                    "scheduled_job_id": job_id,
                    "conversation_id": "conv-sched-1",
                },
                scheduler_audience,
                team=TEAM,
                agent=holder_agent,
                timeout=10,
            )
            body = signed.raw["body"]
            assert body["outcome"] == "ok"
            run_token = body["authority"]
            assert run_token["person"] == ADA_REF
            assert run_token["schedule_id"] == "sched-ada-live"

            # Decide tool calls on that scheduled run under real Cedar using worker_host's live trust.
            verifier = AuthorityTokenVerifier(
                worker_host,
                people=cluster.access,
                installation=cluster.installation,
            )
            with use_process_verifier(verifier):
                try:
                    await verifier.ensure_issuer_known(run_token, now=utc_now())
                    sched_agent = _agent_for_token(cluster.storage, run_token, task_id="task-sched-ada")
                    # Allowed by sched-ada-live's grant for Ada!
                    allowed = decide_call(sched_agent, "publish_single_page_site", {})
                    assert allowed is not None and allowed.allowed is True
                    # Denied for an ungranted tool (e.g. instantiate_agent is team-lead only).
                    denied = decide_call(sched_agent, "instantiate_agent", {})
                    assert denied is not None and denied.allowed is False

                    # 3. System-work schedule gets a scheduler token with schedule_id and no person.
                    claimed_sys = cluster.schedule_store.claim_next_due(
                        claimer_agent_id=holder_agent,
                        claimer_named_agent_type="researcher",
                        claimer_team_id=TEAM,
                        claim_ttl_seconds=120,
                        exclude_schedule_ids=("sched-ada-live",),
                    )
                    assert claimed_sys is not None and claimed_sys["schedule_id"] == "sched-sys-live"
                    sys_reply = await worker_host.live.request(
                        "run_context.sign",
                        {
                            "request_id": "rcs-sys-ok",
                            "schedule_id": "sched-sys-live",
                            "claim_token": str(claimed_sys["claim_token"]),
                            "scheduled_job_id": "sched-sys-live",
                            "conversation_id": "conv-sys-1",
                        },
                        scheduler_audience,
                        team=TEAM,
                        agent=holder_agent,
                        timeout=10,
                    )
                    assert sys_reply.raw["body"]["outcome"] == "ok"
                    assert sys_reply.raw["body"]["system_work"] is True
                    sys_token = sys_reply.raw["body"]["authority"]
                    assert "person" not in sys_token or sys_token["person"] is None
                    assert sys_token["schedule_id"] == "sched-sys-live"
                    verified_sys = verifier.verify_root_task(sys_token, team_id=TEAM, now=utc_now())
                    assert verified_sys.person_ref is None and verified_sys.schedule_id == "sched-sys-live"

                    for key in ("auth_client", "ts_client", "lead_client", "worker_client"):
                        assert ctx[key].errors.empty(), f"unexpected NATS error on {key}"
                finally:
                    _runtime_access.cache_clear()


@pytest.mark.asyncio
async def test_live_schedules_install_from_gateway_and_agent_task_under_operator_mode(tmp_path: Path) -> None:
    """A8: schedules.install over svc.team succeeds from both the gateway (command token)
    and an agent task (gateway-signed root-task token), vouches the owner, issues a
    scheduler-signed permit, and replays the stored idempotent reply."""

    cluster = _LiveCluster(tmp_path)
    with _start_operator_broker(cluster.account, tmp_path / "nats") as url:
        async with AsyncExitStack() as stack:
            ctx = await cluster.start(stack, url)
            gateway: GatewayMessageHost = ctx["gateway"]
            lead_host: MessageHost = ctx["lead_host"]

            # 1. Gateway installs a user schedule via command token.
            client = MessageHostTeamServices(gateway, timeout_seconds=10)
            gw_install_body = {
                "request_id": "req-gw-install-1",
                "schedule_id": "sched-from-gateway",
                "mode": "create",
                "name": "Gateway created schedule",
                "management_class": "user",
                "timing": {"kind": "interval", "interval_ms": 300_000},
                "repeat_count": 0,
                "enabled": True,
                "action": {
                    "type": "task",
                    "instruction": {"type": "inline", "text": "Summarize daily updates."},
                },
            }
            gw_reply = await client.command(
                "schedules.install",
                gw_install_body,
                person_ref=ADA_REF,
                request_id="req-gw-install-1",
            )
            assert gw_reply["outcome"] == "ok"
            assert gw_reply["state"] == "installed"
            assert gw_reply["schedule_id"] == "sched-from-gateway"
            assert cluster.schedule_store.is_owner_vouched("sched-from-gateway") is True
            gw_permit_raw = cluster.schedule_store.run_permit("sched-from-gateway")
            assert gw_permit_raw is not None
            gw_permit = AuthorityTokenRecord.model_validate(gw_permit_raw)
            assert gw_permit.person == ADA_REF and gw_permit.schedule_id == "sched-from-gateway"

            # Idempotent replay returns the exact stored reply.
            gw_replay = await client.command(
                "schedules.install",
                gw_install_body,
                person_ref=ADA_REF,
                request_id="req-gw-install-1",
            )
            assert gw_replay == gw_reply

            # 2. Lead agent installs a user schedule via gateway-signed root-task token over svc.team.
            lead_agent, opened_lead = await _open_owned_session(lead_host, "lead", agent_type="TeamArchitect")
            assert opened_lead["outcome"] == "ok"
            assert lead_host.live is not None
            lead_sess = opened_lead["session"]
            lead_host.live.trust.add(json.dumps(lead_sess).encode("utf-8"), now=lead_host.clock())
            lead_host.live.identity = lead_host.live.identity.with_session(lead_agent, lead_sess["body"]["session"])

            task_token = gateway.sign_root_task_token(
                root_task_id="task-agent-sched-1",
                request_id="task_context_agent_sched_1",
                person_ref=ADA_REF,
            ).model_dump(mode="json")
            team_audience = {
                "installation": cluster.installation,
                "team": TEAM,
                "target": "team_services",
                "id": TEAM,
            }
            agent_install_body = {
                "request_id": "req-agent-install-1",
                "schedule_id": "sched-from-agent",
                "mode": "create",
                "name": "Agent created schedule",
                "management_class": "user",
                "timing": {"kind": "interval", "interval_ms": 600_000},
                "repeat_count": 0,
                "enabled": True,
                "action": {
                    "type": "task",
                    "instruction": {"type": "inline", "text": "Check nightly status."},
                },
            }
            agent_reply = await lead_host.live.request(
                "schedules.install",
                agent_install_body,
                team_audience,
                team=TEAM,
                agent=lead_agent,
                authority=task_token,
                timeout=10,
            )
            assert agent_reply.raw["body"]["outcome"] == "ok"
            assert agent_reply.raw["body"]["state"] == "installed"
            assert agent_reply.raw["body"]["schedule_id"] == "sched-from-agent"
            assert cluster.schedule_store.is_owner_vouched("sched-from-agent") is True
            agent_permit_raw = cluster.schedule_store.run_permit("sched-from-agent")
            assert agent_permit_raw is not None
            agent_permit = AuthorityTokenRecord.model_validate(agent_permit_raw)
            assert agent_permit.person == ADA_REF and agent_permit.schedule_id == "sched-from-agent"

            # Idempotent replay from the agent returns the exact stored reply.
            agent_replay = await lead_host.live.request(
                "schedules.install",
                agent_install_body,
                team_audience,
                team=TEAM,
                agent=lead_agent,
                authority=task_token,
                timeout=10,
            )
            assert agent_replay.raw["body"] == agent_reply.raw["body"]

            for key in ("auth_client", "ts_client", "gw_client", "lead_client", "worker_client"):
                assert ctx[key].errors.empty(), f"unexpected NATS error on {key}"

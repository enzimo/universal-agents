"""Real NATS regressions for session openings, restart cleanup and startup drain."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from assistant_gateway.message_host import GATEWAY_RUNTIME, GatewayMessageHost, gateway_profile
from custom_agents.agent_host import AgentProcessHost, startup_agent_host_profile
from custom_agents.agent_requests import configure_process_host
from custom_agents.relayed_task_controls import RELAYED_HANDLERS
from custom_agents.task_manager import TaskManager
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task, TaskQuery
from custom_messages.enums import MessageMetaType, ProgressState
from custom_messages.wire.sizes import INLINE_TEXT_MAX_CHARS
from custom_messages.wire.trust import kept_for
from custom_transport.protocol1.host_runtime import MessageHost
from custom_transport.protocol1.live import RequestRefused
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import AccessRepository
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_services_profile
from team_services.relays import Relays
from team_services.sessions import SessionService
from tests.test_live_agent_hosts import DEFINITION, PERSON, _agent, _has_session, _until
from tests.test_live_team_services import TEAM, audience, broker, connect, installation, open_session  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


@asynccontextmanager
async def _relayed_runtime(url, tmp_path, handlers):
    layout = InstanceLayout.at(tmp_path / "instance")
    profile = startup_agent_host_profile(TEAM, "agent-recovery", "Researcher")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, url, layout, team_services_profile(TEAM), gateway_profile(TEAM), profile)
        services = MessageHost(layout, team_services_profile(TEAM), await connect(stack, url, TEAM_SERVICES_RUNTIME))
        relays = Relays(services, TEAM, "TeamArchitect", logging.getLogger(__name__))
        serving = asyncio.create_task(serve_team(services, TEAM, stop, [relays.family()]))
        gateway = GatewayMessageHost(layout, TEAM)
        gateway.start(await connect(stack, url, GATEWAY_RUNTIME))
        process = AgentProcessHost(layout, profile, TEAM, handlers=handlers)
        try:
            await gateway.ready(timeout=20)
            yield stack, services, process, gateway
        finally:
            await process.stop()
            await gateway.close()
            stop.set()
            await asyncio.wait_for(serving, 10)


@pytest.mark.asyncio
async def test_cleanup_retry_keeps_an_opening_whose_reply_is_delayed(broker, tmp_path, monkeypatch):  # noqa: F811
    async def tasks_list(_agent, _frame):
        return {"outcome": "ok", "tasks": [], "total": 0, "queued_count": 0, "current_count": 0, "queue_digest": "q"}

    async with _relayed_runtime(broker, tmp_path, {"tasks.list": tasks_list}) as (stack, services, process, gateway):
        saved, cleaned, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        request, cleanup = process._request, process._close_earlier_sessions
        cleanup_calls = 0

        async def cleanup_with_first_listing_failure():
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == 1:
                async def fail_keys(*_args):
                    raise OSError("transient listing failure")
                with monkeypatch.context() as opening:
                    opening.setattr(process.host.records, "keys", fail_keys)
                    await cleanup()
            else:
                await saved.wait()
                await cleanup()
                cleaned.set()

        async def delay_open_reply(kind, body):
            reply = await request(kind, body)
            if kind == "sessions.open" and reply["outcome"] == "ok":
                saved.set()
                await release.wait()
            return reply

        monkeypatch.setattr(process, "_close_earlier_sessions", cleanup_with_first_listing_failure)
        monkeypatch.setattr(process, "_request", delay_open_reply)
        process.attach(_agent("worker_race"), agent_type="Researcher", definition=DEFINITION,
                       nc=await connect(stack, broker, process.profile.runtime))
        try:
            await asyncio.wait_for(cleaned.wait(), 20)
            placement = await services.records.read_report("placement", f"{TEAM}.worker_race", now=services.clock())
            assert placement is not None
            assert process.agents["worker_race"].session is None
            release.set()
            await _until(lambda: _has_session(process, "worker_race"))
            reply = await gateway.command("tasks.list", {"request_id": "after-cleanup", "agent": "worker_race",
                                                         "offset": 0, "limit": 20}, person_ref=PERSON)
            assert reply.raw["body"]["outcome"] == "ok"
            assert not process._pending_closes and not process._cleanup_needed
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("characters", [INLINE_TEXT_MAX_CHARS, INLINE_TEXT_MAX_CHARS + 1])
async def test_saved_long_waiting_questions_reach_the_gateway(broker, tmp_path, caplog, characters):  # noqa: F811
    manager = TaskManager(db_path=str(tmp_path / "tasks.db"))
    manager.set_owner_identity("worker_one", "Worker", TEAM, "Researcher")
    person_ref = "p-OwnerOwnerOwnerOwnerOw"
    AccessRepository(manager.storage_settings).record_person(TeamPersonV1(
        team_id=TEAM, person_id="owner", person_ref=person_ref, roles=("member",),
        standing=PersonStanding.ACTIVE, changed_at_utc=datetime.now(timezone.utc)))
    manager.add_task(Task(task_id="waiting-root", problem_statement="Waiting for an answer",
                          task_type=MessageMetaType.REQUEST), MessageHeader(), MessageMeta(), owner_person_id="owner")
    manager.update_main_task_state("waiting-root", ProgressState.WAITING, datetime.now(timezone.utc).isoformat())
    original = "😀" * characters
    manager.add_task_query("waiting-root", TaskQuery(parent_task_id="waiting-root",
                           query_type=MessageMetaType.REQUEST, message=original))
    agent = SimpleNamespace(id="worker_one", named_agent_type="Researcher", team_id=TEAM, is_team_lead=False,
                            task_manager=manager, _runtime_loop=asyncio.get_running_loop(), _worker_ready=True)
    async with _relayed_runtime(broker, tmp_path, dict(RELAYED_HANDLERS)) as (stack, _services, process, gateway):
        process.attach(agent, agent_type="Researcher", definition=DEFINITION,
                       nc=await connect(stack, broker, process.profile.runtime))
        await _until(lambda: _has_session(process, agent.id))
        with caplog.at_level(logging.WARNING):
            reply = await gateway.command("tasks.list", {"request_id": "waiting-message", "agent": agent.id,
                                                         "offset": 0, "limit": 20}, person_ref=person_ref)
        body = reply.raw["body"]
        assert body["outcome"] == "ok" and body["queue_digest"]
        preview = body["tasks"][0]["blocker"]["text"]
        assert len(preview) <= INLINE_TEXT_MAX_CHARS
        if characters > INLINE_TEXT_MAX_CHARS:
            assert "Preview shortened" in preview
            assert "event=task_blocker_preview_shortened" in caplog.text
        else:
            assert preview == original
            assert "event=task_blocker_preview_shortened" not in caplog.text
        assert "event=protocol1_reply_failed" not in caplog.text
        from assistant_gateway.application.route_context import GatewayRouteContextV1
        from assistant_gateway.application.website_application import WebTaskListRequestV1
        from assistant_gateway.application.website_tasks import list_tasks

        async def request(kind, body, *, person_id):
            assert person_id == "owner"
            answer = await gateway.command(kind, {**body, "request_id": "browser-waiting-message"}, person_ref=person_ref)
            return answer.raw["body"]

        browser = await list_tasks(SimpleNamespace(request=request), WebTaskListRequestV1(
            context=GatewayRouteContextV1(session_key="session", canonical_user_id="owner", channel_name="website",
                                           user_id="owner", chat_id="web-chat"), agent_id=agent.id))
        assert browser.tasks[0].blocker == preview


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["interrupted_open", "forgotten_session", "handed_over", "interrupted_handover"])
async def test_abandoned_placements_release_capacity_but_handovers_stay(broker, tmp_path, monkeypatch, trigger):  # noqa: F811
    layout = InstanceLayout.at(tmp_path / "instance")
    profile = startup_agent_host_profile(TEAM, "agent-one-slot", "researcher")
    stop = asyncio.Event()
    async with AsyncExitStack() as stack:
        await installation(stack, broker, layout, team_services_profile(TEAM), profile)
        services = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(serve_team(services, TEAM, stop))
        earlier = MessageHost(layout, profile, await connect(stack, broker, profile.runtime))
        await earlier.start()
        try:
            if trigger == "interrupted_open":
                async def fail_session_write(*_args):
                    raise RuntimeError("crash after placement commit")
                with monkeypatch.context() as broken:
                    broken.setattr(SessionService, "_store_session", fail_session_write)
                    with pytest.raises(RequestRefused):
                        await open_session(earlier, "old_agent")
            else:
                opened = await open_session(earlier, "old_agent")
                if trigger in {"handed_over", "interrupted_handover"}:
                    body = {"request_id": "handover", "session": opened["session"]["body"]["session"],
                            "reason": "handed_over"}
                    if trigger == "interrupted_handover":
                        async def fail_end_write(*_args):
                            raise RuntimeError("crash after handover marker")
                        with monkeypatch.context() as broken:
                            broken.setattr(SessionService, "_store_session", fail_end_write)
                            with pytest.raises(RequestRefused):
                                await earlier.live.request("sessions.close", body, audience(earlier), team=TEAM)
                    else:
                        await earlier.live.request("sessions.close", body, audience(earlier), team=TEAM)
                    await services._renew()
                    upkeep = SessionService(services.records, services.live.trust, earlier.identity.installation,
                                            services.clock)
                    await upkeep.maintain(TEAM)
                future = datetime.now(timezone.utc) + kept_for("agent_session") + timedelta(seconds=1)
                upkeep = SessionService(services.records, services.live.trust, earlier.identity.installation,
                                        lambda: future)
                await upkeep._delete_forgotten_sessions(TEAM)
            assert not await services.records.keys("agent_session", f"session.{earlier.identity.installation}.{TEAM}.")
            replacement = await open_session(earlier, "new_agent")
            remaining = await services.records.read_report("placement", f"{TEAM}.old_agent", now=services.clock())
            if trigger in {"handed_over", "interrupted_handover"}:
                assert replacement["outcome"] == "limit_reached"
                assert remaining is not None and remaining.record.body.handed_over is True
            else:
                assert replacement["outcome"] == "ok"
                assert remaining is None
        finally:
            stop.set()
            await asyncio.wait_for(serving, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_owner", ["this_agent", "gone_agent"])
async def test_startup_drain_keeps_own_recovered_work_and_adopts_no_other_work(broker, tmp_path, saved_owner):  # noqa: F811
    from custom_agents.strands_agent import UniversalAgent

    db = str(tmp_path / "tasks.db")
    old = TaskManager(db_path=db)
    old.set_owner_identity(saved_owner, "Saved owner", TEAM, "Researcher")
    old.add_task(Task(task_id="saved_task", task_type=MessageMetaType.REQUEST, problem_statement="Saved request"),
                 MessageHeader(), MessageMeta(), owner_person_id=None)
    old.update_main_task_state("saved_task", ProgressState.WAITING, datetime.now(timezone.utc).isoformat())
    old._persist_runtime_state()
    layout = InstanceLayout.at(tmp_path / "instance")
    profile = startup_agent_host_profile(TEAM, "agent-starting", "Researcher")
    process = AgentProcessHost(layout, profile, TEAM, drain_poll_seconds=0.02)
    stop = asyncio.Event()
    configure_process_host(process)
    with patch("custom_agents.strands_agent.Agent", return_value=MagicMock()), patch(
        "custom_agents.strands_agent.UniversalAgent._configure_model", return_value=MagicMock()
    ), patch("custom_agents.strands_agent.schedule_pending_work"):
        agent = UniversalAgent(id="this_agent", name="Starting agent", team_id=TEAM,
                               named_agent_type="Researcher", nats_servers=[broker],
                               task_manager=TaskManager(db_path=db), workspace_dir=str(tmp_path),
                               instance_root=str(tmp_path), memory_graph_enabled=False,
                               activity_collector_base_url=None, tools=[])
        adapter = agent._ensure_nats_transport_adapter()
        await adapter.connect(reconcile_topology=True)
        subscribe = adapter.subscribe_route
        startup_paused, release = asyncio.Event(), asyncio.Event()

        async def delayed_direct_route(**kwargs):
            if kwargs["name"] == f"{agent.id}:direct":
                startup_paused.set()
                await release.wait()
            return await subscribe(**kwargs)

        adapter.subscribe_route = delayed_direct_route
        async with AsyncExitStack() as stack:
            await installation(stack, broker, layout, team_services_profile(TEAM), profile)
            services = MessageHost(layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
            serving = asyncio.create_task(serve_team(services, TEAM, stop))
            running = asyncio.create_task(agent.connect())
            try:
                await asyncio.wait_for(startup_paused.wait(), 20)
                await _until(lambda: _has_session(process, agent.id), timeout=20)
                process.host.learned_other_contract("2" * 64, "startup-regression")
                await _until(lambda: getattr(agent, "_contract_drain", None) is not None)
                await asyncio.sleep(process.drain_poll_seconds * 3)
                assert agent.id in process.agents and not process._draining.done()
                release.set()
                await _until(lambda: agent._worker_ready, timeout=20)
                if saved_owner == agent.id:
                    assert agent.task_manager.get_task_info("saved_task") is not None
                    assert _has_session(process, agent.id)
                    agent.task_manager.update_main_task_state("saved_task", ProgressState.COMPLETED,
                                                              datetime.now(timezone.utc).isoformat())
                else:
                    assert agent.task_manager.get_task_info("saved_task") is None
                    snapshots = agent.task_manager.runtime_state_store.peek_task_runtime_by_role(TEAM, "Researcher")
                    assert any(row.owner_agent_id == saved_owner for row in snapshots)
                await _until(lambda: process._draining.done(), timeout=20)
                assert process.host.state == "drained" and agent.id not in process.agents
            finally:
                release.set()
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                await process.stop()
                configure_process_host(None)
                stop.set()
                await asyncio.wait_for(serving, 10)


@pytest.mark.asyncio
async def test_orphan_cleanup_requires_all_session_reads_before_deleting(broker, tmp_path, monkeypatch):  # noqa: F811
    async with _relayed_runtime(broker, tmp_path, {}) as (stack, services, process, _gateway):
        earlier = MessageHost(process.layout, process.profile, await connect(stack, broker, process.profile.runtime))
        await earlier.start()
        await open_session(earlier, "active_agent", "Researcher")
        current = await services.records.read_report("placement", f"{TEAM}.active_agent", now=services.clock())
        key = f"{TEAM}.abandoned_agent"
        orphan = services.records.signed("placement", key, {**current.record.document.body, "agent": "abandoned_agent"},
                                         sequence=1, issued_at=services.clock())
        await services.records.put("placement", key, orphan)

        async def fail_read(*_args):
            raise OSError("session record unavailable")

        with monkeypatch.context() as failed:
            failed.setattr(SessionService, "_session", fail_read)
            with pytest.raises(RequestRefused):
                await open_session(earlier, "replacement", "Researcher")
        for agent_id in ("active_agent", "abandoned_agent"):
            assert await services.records.read_report("placement", f"{TEAM}.{agent_id}", now=services.clock()) is not None
        # A fresh request can now reap only the orphan. The active agent still uses the one slot.
        result = await earlier.live.request("sessions.open", {"request_id": "replacement-after-read",
            "agent": "replacement", "agent_type": "Researcher", "definition_sha256": DEFINITION},
            audience(earlier), team=TEAM)
        assert result.raw["body"]["outcome"] == "limit_reached"
        assert await services.records.read_report("placement", key, now=services.clock()) is None
        assert await services.records.read_report("placement", f"{TEAM}.active_agent", now=services.clock()) is not None

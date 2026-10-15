"""Knowledge review and restoration through the real gateway and NATS manager route.

No model is needed. The signed gateway request, wire models, durable pull consumer,
reply matching and manager's SQLite store all run as they do in the application.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from live_nats_client import LiveNatsClient, resolve_live_transport_settings

from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from custom_agents.knowledge_command_runtime import handle_manager_knowledge_command
from custom_messages.contracts.control_v1 import AgentStatus, KnowledgeCommandAction, KnowledgeCommandPayload, ManagerKnowledgeCommand
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import ControlType, MessageMetaType, RoutingType
from custom_transport.base import SubscriptionDelivery
from id_generation import new_id
from knowledge_base.service import KnowledgeService
from runtime_settings.knowledge import KnowledgeSettings
from storage.knowledge_repository import create_knowledge_repository
from tests.broker_access_support import forget_task_context_keys, use_gateway_key
from tests.knowledge_support import agent as worker_actor

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


@pytest.mark.asyncio
async def test_gateway_reviews_notes_and_restores_archives_over_nats(tmp_path, monkeypatch):
    use_gateway_key(monkeypatch)
    try:
        await _knowledge_over_nats(tmp_path)
    finally:
        forget_task_context_keys()


async def _knowledge_over_nats(tmp_path) -> None:
    team_id, manager_id = new_id("live_knowledge"), new_id("manager")
    manager_db, gateway_db = tmp_path / "manager.db", tmp_path / "gateway.db"
    config = GatewayInstanceConfig.create(
        instance_name="live_knowledge", instance_root=str(tmp_path / "instance"),
        team_id=team_id, timezone="UTC", display_name="Knowledge reviewer",
        manager_agent_id=manager_id, manager_storage_sqlite_path=str(manager_db),
        state_storage_sqlite_path=str(gateway_db), named_agent_type="TeamArchitect",
    )
    config.transport = resolve_live_transport_settings()
    gateway = UserGateway(config, output_sink=None, state_store=GatewayStateStore(str(gateway_db)))
    session = gateway.application.resolve_session(session_key=config.user.default_session_key)
    context = GatewayRouteContextV1(session_key=session.session_key, canonical_user_id=config.user.canonical_user_id)
    repository = create_knowledge_repository(config.manager_storage_settings)
    service = KnowledgeService(repository, settings=KnowledgeSettings())
    worker = replace(worker_actor("review_worker", person=config.user.canonical_user_id), team_id=team_id)

    async with LiveNatsClient(team_id, transport=config.transport) as manager_client:
        class ManagerComms:
            async def send_message(self, *, message, routing_key) -> None:
                await manager_client.publish(routing_key, message, durable=True)

        manager = SimpleNamespace(
            id=manager_id, name="Knowledge manager", named_agent_type="TeamArchitect",
            team_id=team_id, is_team_lead=True, knowledge_config={},
            _knowledge_repository=repository, _logger=logging.getLogger(__name__),
            team_comms=ManagerComms(),
        )

        async def manager_command(delivery) -> None:
            envelope = delivery.envelope
            if isinstance(envelope.msg_content, ManagerKnowledgeCommand):
                await handle_manager_knowledge_command(
                    manager, command=envelope.msg_content,
                    msg_header=envelope.msg_header, msg_meta=envelope.msg_meta,
                )
            await delivery.ack()

        async def manager_status(delivery) -> None:
            envelope = delivery.envelope
            if isinstance(envelope.msg_content, AgentStatus) and envelope.msg_content.status_type == MessageMetaType.REQUEST:
                response = MessageEnvelope(
                    msg_content=AgentStatus(
                        self_id=manager_id, team_id=team_id, named_agent_type="TeamArchitect",
                        name=manager.name, agent_role="manager", is_paused=False,
                        is_executing=False, has_execution_capacity=True, is_busy=False,
                        status_type=MessageMetaType.RESPONSE, info_type=ControlType.STATUS_REQUEST_BASIC,
                    ),
                    msg_header=MessageHeader(from_agent_id=manager_id, to_routing_path=envelope.msg_header.response_routing_key),
                    msg_meta=MessageMeta(msg_correlation_id=envelope.msg_meta.msg_correlation_id),
                )
                await manager_client.publish(RoutingType.ALL_AGENTS_ROUTE.value, response, operational=True, durable=False)
            await delivery.ack()

        await manager_client.adapter.subscribe_subject(
            name="live-knowledge-manager-status", subject=manager_client.mapper.operational_subject(),
            handler=manager_status, delivery=SubscriptionDelivery.CORE,
            logical_route=RoutingType.ALL_AGENTS_ROUTE.value,
        )
        await manager_client.adapter.subscribe_subject(
            name="live-knowledge-manager-control",
            subject=manager_client.mapper.agent_control_subject(manager_id),
            handler=manager_command,
            durable_name=manager_client.mapper.durable_name(f"manager_{manager_id}_control"),
            logical_route=manager_id,
        )

        async def request(action, **fields):
            return await gateway.application.knowledge.run(context, KnowledgeCommandPayload(action=action, **fields))

        try:
            await gateway.connect(start_services=False)
            created = await request(KnowledgeCommandAction.WRITE, title="Runbook", body="Check the workers.", summary="Worker maintenance.")
            assert created.page.revision == 1 and created.page.last_editor.person_id == ""
            kept = await request(KnowledgeCommandAction.WRITE, page="runbook", base_revision=1, body="Check all workers.")
            assert kept.page.summary == "Worker maintenance."
            cleared = await request(KnowledgeCommandAction.WRITE, page="runbook", base_revision=2, body="Check all workers.", summary="")
            assert cleared.page.summary == ""

            proposed = service.add_note(worker, "team/runbook", "Email alice@example.com.").contribution
            waiting = await request(KnowledgeCommandAction.COUNT_CONTRIBUTIONS)
            assert waiting.total == 1 and waiting.contributions == []
            published = await request(
                KnowledgeCommandAction.PUBLISH_CONTRIBUTION, contribution_id=proposed.contribution_id,
                edited=True, body="Contact the on-call lead.", base_revision=3,
            )
            assert [note.body for note in published.page.open_notes] == ["Contact the on-call lead."]
            assert "alice@example.com" not in published.model_dump_json()
            decided = await request(KnowledgeCommandAction.COUNT_CONTRIBUTIONS)
            assert decided.total == 0

            archived = await request(KnowledgeCommandAction.ARCHIVE, page="runbook", base_revision=3)
            assert archived.page.status == "archived"
            current = await request(KnowledgeCommandAction.LIST)
            found = await request(KnowledgeCommandAction.LIST, status="archived", query="workers")
            assert current.total == 0 and found.total == 1
            restored = await request(
                KnowledgeCommandAction.WRITE, page=found.pages[0].page, base_revision=found.pages[0].revision,
                body=archived.page.body,
            )
            assert restored.page.status == "active" and restored.page.body == "Check all workers."

            for number in range(101):
                service.add_note(replace(worker, actor_id=f"worker_{number}"), "team/runbook", f"Check step {number}.")
            final = await request(KnowledgeCommandAction.LIST_CONTRIBUTIONS, offset=100)
            assert final.total == 101 and len(final.contributions) == 1
            counted = await request(KnowledgeCommandAction.COUNT_CONTRIBUTIONS)
            assert counted.total == 101 and counted.contributions == []
        finally:
            await gateway.close()

    # Knowledge remains manager-owned; a passing wire round trip must not mask a
    # gateway accidentally opening a second knowledge store.
    with sqlite3.connect(gateway_db) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "knowledge_pages" not in tables

"""Knowledge review and restoration through the real gateway, NATS and the team services.

No model is needed. The installation authority, the team services (with their memory and
knowledge handlers on the test's SQLite database) and the gateway's own protocol 1 host
run on nats-server 2.15.1: the gateway signs each person's command, the team services
verify it and act for the person its token names, and the gateway checks and renders the
reply, as in the application.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.nats_manager_runtime import NatsGatewayManagerRuntime
from assistant_gateway.message_host import gateway_profile
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from custom_messages.contracts.control_v1 import KnowledgeCommandAction, KnowledgeCommandPayload
from custom_transport.protocol1.host_runtime import MessageHost
from knowledge_base.service import DraftInput, KnowledgeService
from runtime_settings.knowledge import KnowledgeSettings, KnowledgeWriteLimits
from storage.access_repository import AccessRepository
from storage.knowledge_repository import create_knowledge_repository
from team_services.process import TEAM_SERVICES_RUNTIME, serve_team, team_operations, team_services_profile
from tests.knowledge_support import agent as worker_actor
from tests.test_live_team_services import TEAM, broker, connect, installation  # noqa: F401 - broker is a fixture

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.nats]


@pytest.mark.asyncio
async def test_gateway_reviews_notes_and_restores_archives_over_nats(
    tmp_path, broker: str, monkeypatch,  # noqa: F811 - the shared live NATS fixture
) -> None:
    from assistant_gateway import team_view

    # No team lead runs here: the gateway's start need not wait the usual 15 seconds for one.
    monkeypatch.setattr(team_view, "LEAD_START_SECONDS", 1.0)
    manager_db, gateway_db = tmp_path / "manager.db", tmp_path / "gateway.db"
    config = GatewayInstanceConfig.create(
        instance_name="live_knowledge", instance_root=str(tmp_path / "instance"),
        team_id=TEAM, timezone="UTC", display_name="Knowledge reviewer",
        manager_agent_id="manager_live", manager_storage_sqlite_path=str(manager_db),
        state_storage_sqlite_path=str(gateway_db), named_agent_type="TeamArchitect",
    )
    config.transport.nats_url = broker
    config.transport.nats_servers = [broker]
    config.agent.knowledge.limits.max_body_chars = 500_000
    config.agent.knowledge.limits.max_note_chars = 50_000
    person = config.user.canonical_user_id
    AccessRepository(config.manager_storage_settings).record_person(TeamPersonV1(
        team_id=TEAM, person_id=person, person_ref="p-OwnerOwnerOwnerOwnerOw", standing=PersonStanding.ACTIVE,
        roles=("owner",), changed_at_utc=datetime.now(timezone.utc)))
    repository = create_knowledge_repository(config.manager_storage_settings)
    service = KnowledgeService(
        repository, settings=KnowledgeSettings(limits=KnowledgeWriteLimits(max_body_chars=500_000, max_note_chars=50_000))
    )
    worker = replace(worker_actor("review_worker", person=person), team_id=TEAM)
    stop = asyncio.Event()

    async with AsyncExitStack() as stack:
        await installation(stack, broker, config.layout, team_services_profile(TEAM), gateway_profile(TEAM))
        host = MessageHost(config.layout, team_services_profile(TEAM), await connect(stack, broker, TEAM_SERVICES_RUNTIME))
        serving = asyncio.create_task(
            serve_team(host, TEAM, stop, team_operations(config, host.clock, js=host.nc.jetstream()))
        )
        # The gateway service's runtime: the one process that runs the gateway's protocol 1 host.
        gateway = UserGateway(config, output_sink=None, state_store=GatewayStateStore(str(gateway_db)),
                              manager_runtime=NatsGatewayManagerRuntime.for_gateway_service(config))
        session = gateway.application.resolve_session(session_key=config.user.default_session_key)
        context = GatewayRouteContextV1(session_key=session.session_key, canonical_user_id=person)

        async def request(action, **fields):
            return await gateway.application.knowledge.run(context, KnowledgeCommandPayload(action=action, **fields))

        try:
            await gateway.connect(start_services=False)
            created = await request(KnowledgeCommandAction.WRITE, title="Runbook", body="Check the workers.", summary="Worker maintenance.")
            assert created.page.revision == 1 and created.page.last_editor.label == "You"
            # The browser's editor carries a label only, never a person id or reference.
            assert set(created.page.last_editor.model_dump()) == {"kind", "agent_type", "label"}
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

            # Decision 77 / A5: Knowledge pages up to 500,000 characters and notes up to 50,000 characters
            # travel transparently via UA1_BLOBS end-to-end across Web Chat gateway, NATS, and team services.
            body_500k = ("Handbook section — ✓ line.\n" * 25_000)[:499_999] + "."
            assert len(body_500k) == 500_000
            saved_500k = await request(KnowledgeCommandAction.WRITE, title="Handbook", body=body_500k)
            assert saved_500k.page.revision == 1
            assert saved_500k.page.body == body_500k

            read_500k = await request(KnowledgeCommandAction.READ, page="handbook")
            assert read_500k.page.body == body_500k

            note_50k = ("Detailed operational note — ✓.\n" * 2_000)[:49_999] + "."
            assert len(note_50k) == 50_000
            noted_50k = await request(KnowledgeCommandAction.ADD_NOTE, page="handbook", note=note_50k)
            assert [note.body for note in noted_50k.page.open_notes] == [note_50k]
            assert noted_50k.page.body == body_500k

            # Edit to short text, then read and revert back to the 500,000-character revision 1.
            shortened = await request(KnowledgeCommandAction.WRITE, page="handbook", base_revision=1, body="Short now.")
            assert shortened.page.revision == 2 and shortened.page.body == "Short now."
            rev1 = await request(KnowledgeCommandAction.READ, page="handbook", revision=1)
            assert rev1.page.body == body_500k
            reverted = await request(KnowledgeCommandAction.REVERT, page="handbook", revision=1, base_revision=2)
            assert reverted.page.revision == 3 and reverted.page.body == body_500k

            # Long worker proposal (> 32,768 chars) is read and published cleanly through UA1_BLOBS.
            long_proposal_body = ("Proposed runbook update line.\n" * 2_000)[:44_999] + "."
            proposed_long = service.write(
                worker, "team/handbook", DraftInput(title="Handbook", body=long_proposal_body), base_revision=3
            ).contribution
            assert proposed_long is not None
            shown_proposal = await request(
                KnowledgeCommandAction.READ_CONTRIBUTION, contribution_id=proposed_long.contribution_id
            )
            assert shown_proposal.contribution.body == long_proposal_body
            published_long = await request(
                KnowledgeCommandAction.PUBLISH_CONTRIBUTION,
                contribution_id=proposed_long.contribution_id,
                edited=False,
                base_revision=3,
            )
            assert published_long.page.body == long_proposal_body
        finally:
            await gateway.close()
            stop.set()
            await serving

    # Knowledge stays in the team's database; a passing wire round trip must not mask a
    # gateway accidentally opening a second knowledge store.
    with sqlite3.connect(gateway_db) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "knowledge_pages" not in tables

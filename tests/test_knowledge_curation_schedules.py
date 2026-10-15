"""KnowledgeCuratorAgent's schedules: installed by the gateway, prepared (or skipped) on the curator, signed right."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from access_control.identity import ContextVerifier
from assistant_gateway.gateway import _knowledge_curation_schedules
from custom_agents.knowledge_curation_runtime import prepare_knowledge_curation_schedule
from custom_agents.scheduled_runtime import _runs_for_nobody, _sign_for_nobody
from custom_messages.contracts.routing_v1 import MessageMeta
from custom_messages.enums import ScheduleManagementClass, SystemScheduleKind
from custom_messages.system_schedules import is_person_system_schedule, person_system_schedule_id
from id_generation import KNOWLEDGE_CURATION_PERSON_ID_PREFIX, KNOWLEDGE_CURATION_TEAM_ID_PREFIX, operational_id
from knowledge_base.records import KnowledgeActor
from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import ActorKind
from runtime_settings.knowledge import KnowledgeCurationSettings, KnowledgeSettings
from storage.knowledge_repository import create_knowledge_repository
from storage.config import resolve_storage_settings
from tests import test_person_memory_review_schedules as _gateway_schedules
from tests.test_person_memory_review_schedules import ACTIVE, INSTANCE, OLIVE, TEAM, _deliver, _gateway, _stored

# The gateway schedule tests' fixture: a gateway state store with people and a manager's schedule store.
world = _gateway_schedules.world


def _config(**curation: object) -> SimpleNamespace:
    knowledge = KnowledgeSettings(curation=KnowledgeCurationSettings(**curation))
    return SimpleNamespace(agent=SimpleNamespace(knowledge=knowledge), instance_name=INSTANCE)


def _person_id(person: str) -> str:
    return person_system_schedule_id(KNOWLEDGE_CURATION_PERSON_ID_PREFIX, INSTANCE, person)


TEAM_ID = operational_id(KNOWLEDGE_CURATION_TEAM_ID_PREFIX, INSTANCE)


def test_the_gateway_installs_a_run_per_person_and_one_team_run(world) -> None:
    asyncio.run(_gateway(world, more_schedules=(_knowledge_curation_schedules(_config()),)).ensure_all())
    _deliver(world)
    stored = _stored(world)

    verifier = ContextVerifier.from_env()
    for person in ACTIVE:
        schedule = stored[_person_id(person)]
        assert schedule.system_schedule_kind == SystemScheduleKind.KNOWLEDGE_CURATION_PERSON
        assert schedule.claim_named_agent_type == "KnowledgeCuratorAgent"
        assert verifier.verify(schedule.payload_meta.gateway_context.task_context, team_id=TEAM).person_id == person
        assert is_person_system_schedule(schedule)
    team = stored[TEAM_ID]
    assert (team.management_class, team.system_schedule_kind) == (
        ScheduleManagementClass.SYSTEM,
        SystemScheduleKind.KNOWLEDGE_CURATION_TEAM,
    )
    assert team.payload_meta.gateway_context.canonical_user_id == OLIVE and not is_person_system_schedule(team)
    assert team.interval_ms == 1440 * 60 * 1000


def test_curation_off_installs_nothing(world) -> None:
    asyncio.run(_gateway(world, more_schedules=(_knowledge_curation_schedules(_config(enabled=False)),)).ensure_all())
    _deliver(world)
    assert not any(schedule_id.startswith("kb_cur_") for schedule_id in _stored(world))


def _curator(tmp_path):
    settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "knowledge.db")}})
    return SimpleNamespace(_knowledge_repository=create_knowledge_repository(settings), team_id=TEAM, id="curator_1")


def test_runs_with_nothing_to_review_are_skipped_and_others_get_counts(world) -> None:
    asyncio.run(_gateway(world, more_schedules=(_knowledge_curation_schedules(_config()),)).ensure_all())
    _deliver(world)
    stored = _stored(world)
    curator = _curator(world.tmp_path)
    person = ACTIVE[0]
    person_run, team_run = stored[_person_id(person)], stored[TEAM_ID]

    assert prepare_knowledge_curation_schedule(curator, _person_id(person), person_run) is None
    assert prepare_knowledge_curation_schedule(curator, TEAM_ID, team_run) is None

    service = KnowledgeService(curator._knowledge_repository, settings=KnowledgeSettings())
    system = KnowledgeActor(ActorKind.AGENT, "sys", TEAM)
    page = service.write(system, "team/ci", DraftInput(title="CI", body="CI runs on push. See [[missing]].")).page
    for_person = KnowledgeActor(ActorKind.AGENT, "w", TEAM, work_person_id=person, work_id="ctx")
    service.write(for_person, "team/ci", DraftInput(body="CI runs nightly too, says my private notebook."), base_revision=page.revision)

    prepared = prepare_knowledge_curation_schedule(curator, _person_id(person), person_run)
    assert prepared is not None and "Pending team contributions from their work: 1" in prepared.payload.problem_statement
    assert "notebook" not in prepared.payload.problem_statement
    team_prepared = prepare_knowledge_curation_schedule(curator, TEAM_ID, team_run)
    assert team_prepared is not None and "broken links: 1" in team_prepared.payload.problem_statement
    # Another person's run still has nothing to review.
    assert prepare_knowledge_curation_schedule(curator, _person_id(ACTIVE[1]), stored[_person_id(ACTIVE[1])]) is None


def test_the_team_run_is_signed_for_no_person_and_a_person_run_is_not_changed(world) -> None:
    asyncio.run(_gateway(world, more_schedules=(_knowledge_curation_schedules(_config()),)).ensure_all())
    _deliver(world)
    stored = _stored(world)
    assert _runs_for_nobody(stored[TEAM_ID]) and not _runs_for_nobody(stored[_person_id(ACTIVE[0])])

    meta = MessageMeta.model_validate(stored[TEAM_ID].payload_meta.model_dump())
    agent = SimpleNamespace(team_id=TEAM, id="curator_1", _logger=logging.getLogger("test"))
    assert _sign_for_nobody(agent, meta, TEAM_ID)
    context = ContextVerifier.from_env().verify(meta.gateway_context.task_context, team_id=TEAM)
    assert context.person_id is None and meta.gateway_context.canonical_user_id is None


def test_fresh_duplicates_trigger_a_run_and_cleanup_restores_quiet_skipping(world) -> None:
    asyncio.run(_gateway(world, more_schedules=(_knowledge_curation_schedules(_config()),)).ensure_all())
    _deliver(world)
    team_run = _stored(world)[TEAM_ID]
    curator = _curator(world.tmp_path)
    service = KnowledgeService(curator._knowledge_repository, settings=KnowledgeSettings())
    system = KnowledgeActor(ActorKind.AGENT, "sys", TEAM)
    for page in ("team/release-process", "team/releasing"):
        service.write(system, page, DraftInput(title="Release process", body="Run checks and publish the release tag."))

    prepared = prepare_knowledge_curation_schedule(curator, TEAM_ID, team_run)
    assert prepared is not None and "duplicate pages: 1" in prepared.payload.problem_statement
    assert "publish the release tag" not in prepared.payload.problem_statement
    service.archive(system, "team/releasing", base_revision=1, reason="See release-process")
    assert prepare_knowledge_curation_schedule(curator, TEAM_ID, team_run) is None


def test_administrators_hear_once_about_frozen_pages_and_many_refusals(world) -> None:
    from custom_agents.knowledge_curation_runtime import NOTICE_REFUSED_WRITES
    from custom_messages.contracts.access_v2 import AccessNoticeKind
    from knowledge_base.records import KnowledgeError
    from storage.access_repository import AccessRepository

    asyncio.run(_gateway(world, more_schedules=(_knowledge_curation_schedules(_config()),)).ensure_all())
    _deliver(world)
    team_run = _stored(world)[TEAM_ID]
    curator = _curator(world.tmp_path)
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(world.tmp_path / "knowledge.db")}})
    curator.task_manager = SimpleNamespace(storage_settings=storage)
    inbox = AccessRepository(storage)

    def admin_notices():
        return [notice for notice, _read in inbox.notices_for(TEAM, person_id="usr_admin", roles=("admin",))]

    assert prepare_knowledge_curation_schedule(curator, TEAM_ID, team_run) is None
    assert admin_notices() == [], "nothing found, nothing sent"

    service = KnowledgeService(curator._knowledge_repository, settings=KnowledgeSettings())
    system = KnowledgeActor(ActorKind.AGENT, "sys", TEAM, agent_type="KnowledgeCuratorAgent")
    service.write(system, "team/ci", DraftInput(title="CI", body="Internal build steps for the runners."))
    service.freeze(system, "team/ci", minutes=60)
    for_alice = KnowledgeActor(ActorKind.AGENT, "w", TEAM, work_person_id=ACTIVE[0], work_id="ctx")
    service.write(for_alice, "personal/tea", DraftInput(title="Private tea list", body="Green tea."))
    service.freeze(for_alice, "personal/tea", minutes=60)
    noisy = KnowledgeActor(ActorKind.AGENT, "r", TEAM, agent_type="Researcher")
    for _ in range(NOTICE_REFUSED_WRITES):
        try:
            service.write(noisy, "team/keys", DraftInput(title="Keys", body="password: hunter22hunter"))
        except KnowledgeError:
            pass
    quiet = KnowledgeActor(ActorKind.AGENT, "q", TEAM, agent_type="Coder")
    try:
        service.write(quiet, "team/keys", DraftInput(title="Keys", body="password: hunter22hunter"))
    except KnowledgeError:
        pass

    assert prepare_knowledge_curation_schedule(curator, TEAM_ID, team_run) is not None
    [notice] = admin_notices()
    assert notice.kind is AccessNoticeKind.KNOWLEDGE_NEEDS_ATTENTION and notice.recipient_roles == ("admin",)
    assert "CI (team/ci), frozen until" in notice.summary
    assert f"Researcher: {NOTICE_REFUSED_WRITES} refused (sensitive content {NOTICE_REFUSED_WRITES})" in notice.summary
    for private in ("Internal build steps", "hunter22", "Private tea list", "Coder"):
        assert private not in notice.summary, private
    assert inbox.notices_for(TEAM, person_id="usr_member", roles=("member",)) == [], "administrators only"

    # The next run the same day finds the same and sends nothing new.
    prepare_knowledge_curation_schedule(curator, TEAM_ID, team_run)
    assert len(admin_notices()) == 1


@pytest.mark.parametrize("lifted", [False, True], ids=["expired", "lifted"])
def test_ended_freezes_still_reach_administrators(world, lifted: bool) -> None:
    from storage.access_repository import AccessRepository

    asyncio.run(_gateway(world, more_schedules=(_knowledge_curation_schedules(_config()),)).ensure_all())
    _deliver(world)
    team_run = _stored(world)[TEAM_ID]
    curator = _curator(world.tmp_path)
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(world.tmp_path / "knowledge.db")}})
    curator.task_manager = SimpleNamespace(storage_settings=storage)
    inbox = AccessRepository(storage)
    now = datetime.now(timezone.utc)
    # The daily preparation arrives well after a typical one-hour freeze.
    clock = SimpleNamespace(now=now - timedelta(hours=2))
    service = KnowledgeService(curator._knowledge_repository, settings=KnowledgeSettings(), clock=lambda: clock.now)
    system = KnowledgeActor(ActorKind.AGENT, "sys", TEAM, agent_type="KnowledgeCuratorAgent")
    service.write(system, "team/ci", DraftInput(title="CI", body="Check changes before release."))
    frozen = service.freeze(system, "team/ci", minutes=60)
    if lifted:
        clock.now += timedelta(minutes=10)
        service.unfreeze(system, "team/ci")

    for_alice = KnowledgeActor(ActorKind.AGENT, "w", TEAM, work_person_id=ACTIVE[0], work_id="ctx")
    service.write(for_alice, "personal/tea", DraftInput(title="Private tea list", body="Green tea."))
    service.freeze(for_alice, "personal/tea", minutes=60)
    clock.now = now - timedelta(hours=26)
    service.write(system, "team/older-freeze", DraftInput(title="Older freeze", body="Check the earlier release."))
    service.freeze(system, "team/older-freeze", minutes=60)

    activity = curator._knowledge_repository.activity(team_id=TEAM, since=now - timedelta(hours=24), now=now, limit=20)
    [event] = activity.recently_frozen
    assert event.reference == "team/ci"
    assert event.frozen_until_utc == (None if lifted else frozen.frozen_until_utc)
    assert activity.frozen_pages == [], "historical events do not become active freezes"

    assert prepare_knowledge_curation_schedule(curator, TEAM_ID, team_run) is None, "no model work remains"
    [notice] = [notice for notice, _read in inbox.notices_for(TEAM, person_id="usr_admin", roles=("admin",))]
    expected = "freeze was lifted" if lifted else f"freeze expired at {frozen.frozen_until_utc}"
    assert f"CI (team/ci), {expected}" in notice.summary
    for absent in ("Private tea list", "Older freeze", "Check changes", "frozen until", "agents add notes"):
        assert absent not in notice.summary, absent
    assert inbox.notices_for(TEAM, person_id="usr_member", roles=("member",)) == []

    prepare_knowledge_curation_schedule(curator, TEAM_ID, team_run)
    assert len(inbox.notices_for(TEAM, person_id="usr_admin", roles=("admin",))) == 1

"""A 0.9 team whose id does not fit moves to this version through onboarding's Import under a new id (decision 103).

Opening such an export prepares no review: the page explains why the id does not fit and asks
for a new one, starting from the suggestion. ``POST /api/team-import/team-id`` checks the typed id
like a typed onboarding id (a refused one is shown by the field and nothing is written) and
prepares the review under it. The review folder is fixed per export, so opening the export again
offers the id chosen before: the same id keeps the selections, another id prepares the review again
(``team_transfer.review.prepare_review(restart_for_another_id=True)``). After the import, the first
start stores the handed-over credentials for the new team, and no file or stored value of the new
instance names the old id.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

import tests.test_onboarding_team_import as onboarding_tests
import tests.test_team_transfer_schedules as schedules_tests
from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.schedule_store import ScheduleStore
from internal_credentials import parse_env_file
from runtime_settings.instance_layout import InstanceLayout
from storage.access_repository import AccessRepository
from storage.pinned_memory_repository import create_pinned_memory_repository
from team_transfer.credential_handover import import_handed_over_credentials
from team_transfer.team_ids import team_id_explanation
from tests.test_onboarding_team_import import GITHUB_TOKEN, PASSPHRASE, _confirm_schedules, _onboarding
from tests.test_team_transfer_broker_credentials import _use_store
from tests.test_team_transfer_team_id_renames import _files_naming, _stored_naming

OLD = "team_research_assistant_pro"  # 27 characters, as 0.9's onboarding built it
NEW = "team_research_assistant"  # the suggestion
OTHER = "research_team"
OPEN = {"file_name": "team.uatransfer", "passphrase": PASSPHRASE}


def _copied(export: Path, tmp_path: Path) -> Path:
    root = tmp_path / "new"
    (root / "import").mkdir(parents=True)
    shutil.copy(export, root / "import" / "team.uatransfer")
    return root


def _written(root: Path) -> list[str]:
    """What the new root holds besides the copied export."""

    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if not path.is_relative_to(root / "import"))


async def _open(client: TestClient) -> tuple[dict[str, str], dict[str, Any]]:
    opened = await client.post("/api/team-import/open", json=OPEN)
    body = await opened.json()
    assert opened.status == 200, body
    return {"X-Transfer-Token": body["token"]}, body["team_id_question"]


async def _choose(client: TestClient, headers: dict[str, str], team_id: str) -> tuple[int, dict[str, Any]]:
    response = await client.post("/api/team-import/team-id", json={"team_id": team_id}, headers=headers)
    return response.status, await response.json()


async def _review(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    return await (await client.get("/api/team-import/review", headers=headers)).json()


@pytest.mark.asyncio
async def test_a_long_team_id_moves_through_onboardings_import_under_a_new_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    # The onboarding import test's old instance (people, a schedule, team memory, Ana's sign-in), built for OLD.
    monkeypatch.setattr(onboarding_tests, "TEAM", OLD)
    monkeypatch.setattr(schedules_tests, "TEAM", OLD)
    root = _copied(await onboarding_tests._export(tmp_path, monkeypatch), tmp_path)
    service = _onboarding(root)

    async with TestClient(TestServer(service.build_app())) as client:
        # Opening explains why the id does not fit and suggests one; no review is prepared yet.
        headers, question = await _open(client)
        assert question == {"exported_team_id": OLD, "explanation": team_id_explanation(OLD), "suggestion": NEW,
                            "chosen_before": None}
        assert "is 27 characters long" in question["explanation"]
        waiting = await client.get("/api/team-import/review", headers=headers)
        assert waiting.status == 409 and "Choose the team's new id first" in (await waiting.json())["error"]
        assert (await _choose(client, {"X-Transfer-Token": "not the token"}, NEW))[0] == 403

        # An id the rule refuses is answered by the field, and nothing is written.
        for typed, reason in (("", "The new team id is empty"), ("team research", "'team research'"),
                              ("t" * 25, "is 25 characters long")):
            status, body = await _choose(client, headers, typed)
            assert (status, body["field"]) == (400, "team_id") and reason in body["error"], body
        assert _written(root) == []

        # The first choice: another id, and one fact left out.
        assert await _choose(client, headers, f" {OTHER} ") == (
            200, {"page": "/team-import", "team_id": OTHER, "restarted_review": False})
        review = await _review(client, headers)
        assert (review["team_id"], review["imported_team_id"]) == (OLD, OTHER)
        outdated = next(r for r in review["records"] if r["title"] == "An outdated fact")
        await client.post("/api/team-import/record", headers=headers,
                          json={"section": outdated["section"], "key": outdated["key"], "selected": False})

        # Opened again: the page offers that id, and keeping it keeps the selection.
        headers, question = await _open(client)
        assert (question["suggestion"], question["chosen_before"]) == (OTHER, OTHER)
        assert (await _choose(client, headers, OTHER))[1]["restarted_review"] is False
        assert not next(r for r in (await _review(client, headers))["records"] if r["key"] == outdated["key"])["selected"]
        # A refused id leaves that review as it was.
        assert (await _choose(client, headers, "team research"))[0] == 400
        assert (await _review(client, headers))["imported_team_id"] == OTHER

        # Opened again and given the suggestion: the review is prepared again, everything selected.
        headers, _question = await _open(client)
        assert (await _choose(client, headers, NEW))[1] == {
            "page": "/team-import", "team_id": NEW, "restarted_review": True}
        review = await _review(client, headers)
        assert (review["team_id"], review["imported_team_id"]) == (OLD, NEW)
        assert all(record["selected"] for record in review["records"])

        await _confirm_schedules(client, headers, review)
        validation = await (await client.post("/api/team-import/validate", json={}, headers=headers)).json()
        assert validation["valid"], validation
        imported = await client.post("/api/team-import/import", json={"digest": validation["digest"]}, headers=headers)
        result = await imported.json()
        assert imported.status == 200, result
        await asyncio.sleep(0.6)

    assert result["restarting"] is True and service.completion_event.is_set()
    assert {key: service.last_completion["team_import"][key] for key in ("team_id", "exported_team_id")} == {
        "team_id": NEW, "exported_team_id": OLD}
    logged = caplog.text
    assert f"event=onboarding_team_import_team_id_chosen team_id={NEW} exported_team_id={OLD!r} restarted_review=True" \
        in logged
    assert f"event=onboarding_team_imported team_id={NEW} exported_team_id={OLD!r} " in logged
    config = GatewayInstanceConfig.load_saved(root / "instance_config.yaml")
    assert config.gateway.team_id == NEW
    facts = create_pinned_memory_repository(config.manager_storage_settings).list_coremem(team_id=NEW)
    assert sorted(fact.fact_text for fact in facts) == ["An outdated fact", "Keep the weekly report short"]
    assert ScheduleStore(storage_settings=config.manager_storage_settings).get_row("sched_morning") is not None
    layout = InstanceLayout.at(root)
    assert not (root / "import" / "team.uatransfer").exists() and not any(layout.team_import_review_dir.iterdir())

    # The first start: the new instance's broker is team NEW and stores Ana's sign-in.
    monkeypatch.setattr(onboarding_tests, "TEAM", NEW)
    _use_store(tmp_path / "new_store", monkeypatch)
    broker = await onboarding_tests._serving_broker(parse_env_file(root / "team.env")["SECURE_TOOL_BROKER_TOKEN"],
                                                    monkeypatch)
    try:
        report = await asyncio.to_thread(import_handed_over_credentials, str(config.config_path), retry_delays=(0.0,))
    finally:
        await broker.close()
    from secure_tool_broker.credential_state_store import read_credential_state

    assert report is not None and all(outcome["imported"] for outcome in report.outcomes)
    assert read_credential_state("github.token", owner_id="usr_ana") == GITHUB_TOKEN
    assert not layout.team_import_credentials_path.exists()
    notices = AccessRepository(config.manager_storage_settings)
    assert [notice.title for notice, _read in notices.notices_for(NEW, person_id="usr_ana", roles=())] == [
        "Your sign-ins and keys moved to this instance"]
    assert notices.notices_for(OLD, person_id="usr_ana", roles=()) == []

    # Only the import's own reports (where the team came from) name the old id.
    for searched in (root, tmp_path / "new_store"):
        assert _stored_naming(searched, (OLD,)) == []
        assert _files_naming(searched, (OLD,), leaving_out=layout.team_import_dir) == []


@pytest.mark.asyncio
async def test_an_id_another_team_in_the_knowledge_already_has_is_refused_by_the_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tests.test_team_transfer_knowledge as knowledge_tests
    from team_transfer.crypto import new_key_derivation
    from team_transfer.service import export_source, export_team
    from tests.test_team_transfer_team_id_renames import _knowledge_shared_with

    monkeypatch.setattr(knowledge_tests, "TEAM", OLD)
    out = tmp_path / "team.uatransfer"
    export_team(export_source(_knowledge_shared_with(tmp_path, OTHER)), out, PASSPHRASE, owner_person_id="usr_ana",
                sections=knowledge_tests.SECTIONS, key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    root = _copied(out, tmp_path)

    async with TestClient(TestServer(_onboarding(root).build_app())) as client:
        headers, question = await _open(client)
        assert question["suggestion"] == NEW
        status, body = await _choose(client, headers, OTHER)
        assert (status, body["field"]) == (400, "team_id") and "Choose another new team id" in body["error"], body
        assert not InstanceLayout.at(root).team_import_review_dir.exists() or not any(
            InstanceLayout.at(root).team_import_review_dir.iterdir()), "no review is prepared"

        # After a review under a free id, that id is refused the same way and the review stays.
        assert (await _choose(client, headers, NEW))[0] == 200
        status, body = await _choose(client, headers, OTHER)
        assert (status, body["field"]) == (400, "team_id"), body
        assert (await _review(client, headers))["imported_team_id"] == NEW
        headers, question = await _open(client)
        assert question["chosen_before"] == NEW


@pytest.mark.asyncio
async def test_the_id_step_and_an_import_never_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Preparing the review under another id may replace its folder, so neither starts while the other runs."""

    import threading

    import assistant_gateway.onboarding_web.team_import as team_import
    from team_transfer.errors import TransferError
    from team_transfer.review_session import ReviewSession
    from tests.test_team_import_protocol1_ids import FITS, TOO_LONG, _export

    root = _copied(_export(tmp_path, TOO_LONG), tmp_path)
    preparing, release = threading.Event(), threading.Event()
    prepare = team_import.prepare_review

    def slow_prepare(*args: Any, **kwargs: Any) -> Any:
        preparing.set()
        release.wait(10)
        return prepare(*args, **kwargs)

    async def slow_apply(self: ReviewSession, payload: Any) -> Any:
        await asyncio.Event().wait()
        raise TransferError("never reached")

    async with TestClient(TestServer(_onboarding(root).build_app())) as client:
        headers, _question = await _open(client)
        assert (await _choose(client, headers, FITS))[0] == 200

        # While the review is prepared again, the import is refused.
        monkeypatch.setattr(team_import, "prepare_review", slow_prepare)
        choosing = asyncio.create_task(_choose(client, headers, "team_b"))
        await asyncio.to_thread(preparing.wait, 10)
        refused = await client.post("/api/team-import/import", json={"digest": "d"}, headers=headers)
        assert refused.status == 409 and "being prepared" in (await refused.json())["error"]
        release.set()
        assert (await choosing)[0] == 200

        # While an import runs, the id step is refused.
        monkeypatch.setattr(ReviewSession, "apply", slow_apply)
        importing = asyncio.create_task(client.post("/api/team-import/import", json={"digest": "d"}, headers=headers))
        await asyncio.sleep(0.05)
        status, body = await _choose(client, headers, FITS)
        importing.cancel()
    assert status == 409 and "import is running" in body["error"]

"""A 0.9 team whose id does not fit moves to this version through onboarding's Import under a new id (decision 103).

Opening such an export prepares no review: the page explains why the id does not fit and asks
for a new one, starting from the suggestion. ``POST /api/team-import/team-id`` checks the typed id
like a typed onboarding id (a refused one is shown by the field and nothing is written) and
prepares the review under it, once per opening. The review folder is fixed per export, so opening
the export again offers the id chosen before: the same id keeps the selections, another id prepares
the review again (``team_transfer.review.prepare_review(restart_for_another_id=True)``). After the import, the first
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
from team_transfer.team_id_choice import team_id_explanation
from tests.test_onboarding_team_import import GITHUB_TOKEN, PASSPHRASE, _confirm_schedules, _onboarding
from tests.test_team_transfer_broker_credentials import _use_store
from tests.test_team_transfer_team_id_renames import _files_naming, _names, _stored_naming

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


def _text_of(review: dict[str, Any], key: str) -> str:
    return next(record["text"] for record in review["records"] if record["key"] == key)


async def _import_all_this_version_takes(client: TestClient, headers: dict[str, str], review: dict[str, Any]) -> None:
    """The every-section team's records but what this version refuses for any team, schedules confirmed, imported."""

    import tests.test_team_import_new_team_id_cli as cli_tests

    for record in review["records"]:
        selected = (record["section"], record["key"]) not in cli_tests.REFUSED_FOR_ANY_TEAM
        body: dict[str, Any] = {"section": record["section"], "key": record["key"], "selected": selected}
        if selected and record["section"] == "schedules" and record["text"] is not None:
            body.update(text=record["text"], confirmed=True)
        saved = await client.post("/api/team-import/record", json=body, headers=headers)
        assert saved.status == 200, (body, await saved.json())
    validation = await (await client.post("/api/team-import/validate", json={}, headers=headers)).json()
    assert validation["valid"], validation
    imported = await client.post("/api/team-import/import", json={"digest": validation["digest"]}, headers=headers)
    assert imported.status == 200, await imported.json()
    await asyncio.sleep(0.6)


async def _until(condition: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.01)


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
        # Once this opening has its review, another id needs the export opened again.
        status, body = await _choose(client, headers, NEW)
        assert status == 409 and "open the export again" in body["error"], body
        assert (await _review(client, headers))["imported_team_id"] == OTHER
        # Opened again, an id the rule refuses leaves the saved review as it was.
        headers, _question = await _open(client)
        assert (await _choose(client, headers, "team research"))[0] == 400
        headers, question = await _open(client)
        assert question["chosen_before"] == OTHER

        # Given the suggestion instead: the review is prepared again, everything selected.
        assert (await _choose(client, headers, NEW))[1] == {
            "page": "/team-import", "team_id": NEW, "restarted_review": True}
        # Continue pressed again after a lost reply: the same id, read as the id step reads it, gets the
        # same answer and prepares nothing again; another id is still refused above.
        assert await _choose(client, headers, f" {NEW}") == (
            200, {"page": "/team-import", "team_id": NEW, "restarted_review": False})
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

        # After a review under a free id, opening again and choosing that id is refused the same
        # way, and the saved review stays.
        assert (await _choose(client, headers, NEW))[0] == 200
        headers, question = await _open(client)
        assert question["chosen_before"] == NEW
        status, body = await _choose(client, headers, OTHER)
        assert (status, body["field"]) == (400, "team_id") and "Choose another new team id" in body["error"], body
        headers, question = await _open(client)
        assert question["chosen_before"] == NEW


@pytest.mark.asyncio
async def test_each_opening_chooses_the_id_once_and_an_older_opening_cannot_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Preparing the review under another id replaces its folder, so nothing on this opening may use it then.

    The review routes refuse until the opening has its review, and the id step refuses once it
    has one. Two id steps sent at once: the second waits for the first and is refused. An
    import whose request was accepted for an export that was opened again before its body
    arrived is refused, so the newer opening's id step never runs beside an import.
    """

    import threading

    import assistant_gateway.onboarding_web.team_import as team_import
    from tests.test_team_import_protocol1_ids import FITS, TOO_LONG, _export

    root = _copied(_export(tmp_path, TOO_LONG), tmp_path)
    preparing, release = threading.Event(), threading.Event()
    prepare = team_import.prepare_review

    def slow_prepare(*args: Any, **kwargs: Any) -> Any:
        preparing.set()
        release.wait(10)
        return prepare(*args, **kwargs)

    # Set once an import request is past the token check, before it reads its body.
    import_started = asyncio.Event()
    handle_import = team_import.TeamImport._import

    async def noting_import(self: Any, request: Any, opened: Any) -> Any:
        import_started.set()
        return await handle_import(self, request, opened)

    monkeypatch.setattr(team_import.TeamImport, "_import", noting_import)

    async with TestClient(TestServer(_onboarding(root).build_app())) as client:
        headers, _question = await _open(client)
        # A body the model refuses (a pasted id over 255 characters, or no id) is answered by the field.
        for body in ({"team_id": "t" * 300}, {}):
            response = await client.post("/api/team-import/team-id", json=body, headers=headers)
            assert (response.status, (await response.json())["field"]) == (400, "team_id")
        assert "letters, digits" in (await response.json())["error"]

        monkeypatch.setattr(team_import, "prepare_review", slow_prepare)
        first = asyncio.create_task(_choose(client, headers, FITS))
        await asyncio.to_thread(preparing.wait, 10)
        second = asyncio.create_task(_choose(client, headers, "team_b"))
        await asyncio.sleep(0.05)  # the second call is waiting for the first
        release.set()
        assert (await first)[0] == 200
        status, body = await second
        assert status == 409 and "already chosen" in body["error"], body
        assert (await _review(client, headers))["imported_team_id"] == FITS

        # An import whose body arrives after the export was opened again is refused.
        body_sent = asyncio.Event()

        async def slow_body() -> Any:
            yield b'{"digest": '
            await body_sent.wait()
            yield b'"d"}'

        importing = asyncio.create_task(client.post(
            "/api/team-import/import", data=slow_body(), headers={**headers, "Content-Type": "application/json"}))
        await asyncio.wait_for(import_started.wait(), 10)
        newer, _question = await _open(client)
        body_sent.set()
        refused = await importing
        assert refused.status == 403 and "Open the export again" in (await refused.json())["error"]

        # A review that cannot be prepared is answered on the page and logged with the file and the error type.
        def failing_prepare(*args: Any, **kwargs: Any) -> Any:
            raise OSError("disk full")

        monkeypatch.setattr(team_import, "prepare_review", failing_prepare)
        status, body = await _choose(client, newer, FITS)
    assert (status, body["error"]) == (400, "disk full") and "field" not in body, body
    assert ("event=onboarding_team_import_team_id_failed file_name=team.uatransfer "
            f"exported_team_id={TOO_LONG!r} error_type=OSError") in caplog.text


@pytest.mark.asyncio
async def test_every_section_moves_through_the_page_under_a_new_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command-line test's every-section team, sent through the page's routes instead.

    Both paths read the records under the chosen ids at the same point, but the page has its own
    opening, review session, validation and hand-over; this checks that they pass the chosen id on
    to every section. Credentials are left out here: the first test covers their hand-over.
    """

    import tests.test_team_import_new_team_id_cli as cli_tests
    from instance_upgrades.protocol1_ids import check_ids_before_upgrade, load_saved_for_check

    out, _secret, derived, _registry = cli_tests._exported_every_section(tmp_path, monkeypatch)
    needles = (OLD, *derived.values())
    root = _copied(out, tmp_path)

    async with TestClient(TestServer(_onboarding(root).build_app())) as client:
        headers, question = await _open(client)
        assert question["suggestion"] == NEW
        assert (await _choose(client, headers, NEW))[0] == 200
        review = await _review(client, headers)
        assert (review["team_id"], review["imported_team_id"]) == (OLD, NEW)
        assert sorted({record["section"] for record in review["records"]}) == [
            "access", "control", "knowledge", "mcp", "objectives", "people", "pinned_memory", "procedures",
            "schedules", "settings"]
        # No record key or title on the page names the old id or one derived from it.
        shown = " ".join(f"{record['key']} {record.get('title') or ''}" for record in review["records"])
        assert [needle for needle in needles if _names(shown, needle)] == []

        # Everything but what this version refuses for any team, as on the command line.
        await _import_all_this_version_takes(client, headers, review)

    config = GatewayInstanceConfig.load_saved(root / "instance_config.yaml")
    assert config.gateway.team_id == NEW
    assert {grant.grant_id for grant in AccessRepository(config.manager_storage_settings).list_team(NEW)} == {
        "g_person", "g_lead", "g_team", "g_role"}
    checked = load_saved_for_check(root / "instance_config.yaml")
    assert check_ids_before_upgrade(checked, lambda: checked.manager_storage_settings) is None
    # No file (the import reports aside) or stored value names the old id or a derived one; the same search finds
    # the new id.
    assert cli_tests._files_naming(root, needles) == []
    assert _stored_naming(root, needles) == []
    assert _stored_naming(root, (NEW,))


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["body still arriving", "waiting behind a validation"])
async def test_a_save_accepted_on_an_earlier_opening_never_reaches_the_newer_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, timing: str
) -> None:
    """A settings save of an earlier opening cannot put a value built from its id into the newer review.

    Opening the export again closes the earlier opening's review session under its lock, then its
    reader, before the newer opening gets its token. The save is for ``gateway.response_queue_name``,
    whose text the earlier opening built from its id; it was accepted (past the token check) before the
    export was opened again:

    - its body still arriving: it is refused, and the newer review keeps the value built from its id;
    - complete, waiting for the session's lock behind that opening's validation: the newer opening waits
      too, the validation still reads an open export, and the save finishes first, in the folder the
      newer opening's id step then prepares again.

    Either way the import writes both settings built from the team id with the newer id, not the earlier.
    """

    import json
    import threading

    import assistant_gateway.onboarding_web.team_import as team_import
    import team_transfer.review_session as review_session
    import tests.test_team_import_new_team_id_cli as cli_tests

    out, _secret, _derived, _registry = cli_tests._exported_every_section(tmp_path, monkeypatch)
    root = _copied(out, tmp_path)
    service = _onboarding(root)
    queue_key = "gateway.response_queue_name"

    # Set once the save is past the token check, before it reads its body.
    save_started = asyncio.Event()
    handle_record = team_import.TeamImport._record

    async def noting_record(self: Any, request: Any, opened: Any) -> Any:
        save_started.set()
        return await handle_record(self, request, opened)

    monkeypatch.setattr(team_import.TeamImport, "_record", noting_record)

    # The earlier opening's validation, held after it took the lock and before it reads the export.
    planning, release = threading.Event(), threading.Event()
    plan_import = review_session.plan_import

    def held_plan(*args: Any, **kwargs: Any) -> Any:
        if not planning.is_set():
            planning.set()
            release.wait(10)
        return plan_import(*args, **kwargs)

    async with TestClient(TestServer(service.build_app())) as client:
        earlier, _question = await _open(client)
        assert (await _choose(client, earlier, OTHER))[0] == 200
        earlier_review = await _review(client, earlier)
        earlier_text = _text_of(earlier_review, queue_key)
        assert f"{OTHER}_alpha_custom" in earlier_text
        payload = json.dumps({"section": "settings", "key": queue_key, "selected": True, "text": earlier_text}).encode()
        body_sent = asyncio.Event()
        validating = None
        if timing == "body still arriving":

            async def arriving() -> Any:
                yield payload[:5]
                await body_sent.wait()
                yield payload[5:]

            data: Any = arriving()
        else:
            await _confirm_schedules(client, earlier, earlier_review)  # so the validation reaches the planner
            monkeypatch.setattr(review_session, "plan_import", held_plan)
            validating = asyncio.create_task(client.post("/api/team-import/validate", json={}, headers=earlier))
            assert await asyncio.to_thread(planning.wait, 10), await (await validating).json()
            data = payload
        late = asyncio.create_task(client.post(
            "/api/team-import/record", data=data, headers={**earlier, "Content-Type": "application/json"}))
        await asyncio.wait_for(save_started.wait(), 10)
        await asyncio.sleep(0.05)  # the save waits for its body, or for the lock

        opening_again = asyncio.create_task(_open(client))
        if validating is not None:
            # The earlier opening is detached at once; the newer one waits for its validation to finish.
            await _until(lambda: service.team_import._open is None)
            await asyncio.sleep(0.05)
            assert not opening_again.done()
            release.set()
            validated = await validating
            assert validated.status == 200 and (await validated.json())["records"], "it read an open export"
        newer, _question = await opening_again
        assert await _choose(client, newer, NEW) == (
            200, {"page": "/team-import", "team_id": NEW, "restarted_review": True})
        newer_text = _text_of(await _review(client, newer), queue_key)
        assert f"{NEW}_alpha_custom" in newer_text

        body_sent.set()
        answered = await late
        if validating is None:
            assert (answered.status, await answered.json()) == (400, {
                "error": "This review was closed because the export was opened again. Continue on the newest page."})
            assert "event=team_transfer_review_step_after_close step=update" in caplog.text
        else:
            assert (answered.status, await answered.json()) == (200, {"saved": True}), "it finished first"
        review = await _review(client, newer)
        assert _text_of(review, queue_key) == newer_text
        await _import_all_this_version_takes(client, newer, review)

    config = GatewayInstanceConfig.load_saved(root / "instance_config.yaml")
    assert (config.gateway.team_id, config.transport.subject_prefix, config.gateway.response_queue_name) == (
        NEW, NEW, f"{NEW}_alpha_custom")

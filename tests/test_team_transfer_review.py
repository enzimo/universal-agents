"""Reviewed, edited, selective imports and later batches through real stores and HTTP routes."""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.config import GatewayInstanceConfig
from custom_agents.schedule_store import ScheduleStore
from storage.gateway_state_repository import create_gateway_state_repository
from storage.pinned_memory_repository import create_pinned_memory_repository
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.people import PeopleSection
from team_transfer.pinned_memory import PinnedMemorySection
from team_transfer.review import ReviewFiles, prepare_review, text_digest
from team_transfer.review_web import build_review_app
from team_transfer.schedules import SchedulesSection
from team_transfer.sections import RecordOutcome
from team_transfer.service import (
    TransferError,
    default_sections,
    export_source,
    export_team,
    import_team,
)
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance
from tests.test_team_transfer_schedules import _schedule

PASSPHRASE = "correct horse battery staple"
SECTIONS = (
    SettingsSection(),
    PeopleSection(),
    SchedulesSection(),
    PinnedMemorySection(),
)


@pytest.fixture
def backup(tmp_path):
    path, config = new_instance(tmp_path, "alpha", "team_a")
    config.heartbeat.enabled = False
    config.llm.model_id = "source-model"
    config.save(path)
    people = create_gateway_state_repository(config.state_storage_settings)
    for person, role in (("usr_ana", "owner"), ("usr_ben", "member")):
        people.create_gateway_user(
            canonical_user_id=person, display_name=person, timezone="UTC"
        )
        people.create_team_membership(
            canonical_user_id=person, team_id="team_a", role=role
        )
    store = ScheduleStore(storage_settings=config.manager_storage_settings)
    people.upsert_session(
        session_key="sess_ana",
        owner_canonical_user_id="usr_ana",
        status="active",
        channel_name="telegram",
        user_id="tg-42",
        chat_id="42",
        conversation_id=None,
        last_task_id=None,
        manager_agent_id=None,
    )
    for key in ("sched_first", "sched_second"):
        if key == "sched_first":
            store.add(
                _schedule(key, "usr_ana", repeat_count=5), remaining_count_override=2
            )
        else:
            store.add(_schedule(key, "usr_ana"))
    memory = create_pinned_memory_repository(config.manager_storage_settings)
    memory.create_coremem(team_id="team_a", fact_text="First fact", capacity=10)
    memory.create_coremem(team_id="team_a", fact_text="Second fact", capacity=10)
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(path),
        out,
        PASSPHRASE,
        owner_person_id="usr_ana",
        sections=SECTIONS,
        key_derivation=new_key_derivation(opslimit=1, memlimit=8192),
    )
    with TransferReader.open(out, PASSPHRASE) as reader:
        yield reader, tmp_path / "new", tmp_path / "review"


def choose(files, predicate, *, confirm=False):
    files.save(
        files.review.model_copy(
            update={
                "records": [
                    r.model_copy(
                        update={
                            "selected": predicate(r),
                            "confirmed_digest": text_digest(files.text(r))
                            if confirm and r.section == "schedules" and r.file
                            else None,
                        }
                    )
                    for r in files.review.records
                ]
            }
        )
    )


def write_batch(files):
    choices, overrides = files.choices(require_confirmation=True)
    return import_team(
        files.reader,
        files.review.target_root,
        dry_run=False,
        sections=SECTIONS,
        choices=choices,
        overrides=overrides,
        allow_existing=True,
        require_valid=True,
    )


def test_two_edited_batches_preserve_ids_and_do_not_repeat_memories(backup):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    first_memory = next(r for r in files.review.records if r.section == "pinned_memory")
    schedule = next(r for r in files.review.records if r.key == "schedule/sched_first")
    edited = (
        files.text(schedule)
        .replace("Run sched_first", "Send the edited reminder")
        .replace("interval_ms: 3600000", "interval_ms: 7200000")
    )
    (directory / schedule.file).write_text(edited)
    (directory / first_memory.file).write_text("Edited fact")
    choose(
        files,
        lambda r: (
            r.section in {"settings", "people"}
            or r.key in {schedule.key, first_memory.key, "session/sess_ana"}
        ),
        confirm=True,
    )
    write_batch(files)
    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    store = ScheduleStore(storage_settings=config.manager_storage_settings)
    assert (
        store.get_scheduled_message("sched_first").payload.problem_statement
        == "Send the edited reminder"
    )
    assert store.get_scheduled_message("sched_first").interval_ms == 7200000
    assert store.get_scheduled_message("sched_second") is None
    memory = create_pinned_memory_repository(config.manager_storage_settings)
    assert [row.fact_text for row in memory.list_unexpired_facts()] == ["Edited fact"]

    choose(
        files,
        lambda r: r.section != "schedules" or r.key == "schedule/sched_second",
        confirm=True,
    )
    write_batch(files)
    assert store.get_scheduled_message("sched_second") is not None
    assert sorted(row.fact_text for row in memory.list_unexpired_facts()) == [
        "Edited fact",
        "Second fact",
    ]
    write_batch(files)
    assert len(memory.list_unexpired_facts()) == 2
    receipt = json.loads(config.layout.team_import_receipt_path.read_text())
    assert receipt["source_mac"] == reader.manifest.mac


def test_editing_a_confirmed_schedule_requires_confirmation_again(backup):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    choose(
        files,
        lambda r: r.section != "schedules" or r.key == "schedule/sched_first",
        confirm=True,
    )
    schedule = next(r for r in files.review.records if r.key == "schedule/sched_first")
    (directory / schedule.file).write_text(
        files.text(schedule).replace("Run sched_first", "Edited later")
    )
    with pytest.raises(TransferError, match="Confirm schedule"):
        write_batch(files)
    assert not target.exists()


def test_schedule_owner_and_provenance_cannot_be_changed_in_markdown(backup):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    schedule = next(r for r in files.review.records if r.key == "schedule/sched_first")
    (directory / schedule.file).write_text(
        files.text(schedule).replace(
            "canonical_user_id: usr_ana", "canonical_user_id: usr_ben"
        )
    )
    with pytest.raises(TransferError, match="cannot change payload_meta"):
        files.edited_record(schedule)


def test_later_batch_is_refused_while_the_runtime_lock_is_held(backup):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    choose(files, lambda r: r.section in {"settings", "people"}, confirm=True)
    write_batch(files)
    choose(
        files,
        lambda r: r.key in {"schedule/sched_first", "session/sess_ana"},
        confirm=True,
    )
    from runtime_hosting.process_run import acquire_process_run
    import os

    lock = acquire_process_run(
        GatewayInstanceConfig.load(
            target / "instance_config.yaml"
        ).layout.process_run_lock_path
    )
    try:
        with pytest.raises(TransferError, match="Stop this instance"):
            write_batch(files)
    finally:
        os.close(lock.fd)
    assert not GatewayInstanceConfig.load(
        target / "instance_config.yaml"
    ).layout.team_import_marker_path.exists()


@pytest.mark.asyncio
async def test_browser_requires_private_token_and_validated_exact_review(backup):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    token = "private-test-token"
    async with TestClient(TestServer(build_review_app(files, token))) as client:
        assert (await client.get("/api/review")).status == 403
        headers = {"X-Transfer-Token": token}
        state = await client.get("/api/review", headers=headers)
        assert state.headers["Cache-Control"] == "no-store"
        public = await state.json()
        assert "Schedule review" in json.dumps(public)
        assert (
            next(r for r in public["records"] if r["key"] == "schedule/sched_first")[
                "remaining_count"
            ]
            == 2
        )
        for r in files.review.records:
            response = await client.post(
                "/api/record",
                headers=headers,
                json={
                    "section": r.section,
                    "key": r.key,
                    "selected": r.section in {"settings", "people"}
                    or r.key in {"schedule/sched_first", "session/sess_ana"},
                    "confirmed": r.key == "schedule/sched_first",
                    "text": files.text(r),
                },
            )
            assert response.status == 200, await response.text()
        validated = await client.post("/api/validate", headers=headers, json={})
        assert validated.status == 200, await validated.text()
        result = await validated.json()
        assert result["valid"]
        bad = await client.post(
            "/api/import", headers=headers, json={"digest": "wrong"}
        )
        assert bad.status == 400 and not target.exists()
        schedule = next(
            r for r in files.review.records if r.key == "schedule/sched_first"
        )
        original = files.text(schedule)
        (directory / schedule.file).write_text(
            original + "\nChanged in an external editor.\n"
        )
        changed = await client.post(
            "/api/import", headers=headers, json={"digest": result["digest"]}
        )
        assert changed.status == 400 and not target.exists()
        (directory / schedule.file).write_text(original)
        applied = await client.post(
            "/api/import", headers=headers, json={"digest": result["digest"]}
        )
        assert applied.status == 200, await applied.text()
        assert (target / "instance_config.yaml").is_file()


def test_review_paths_and_source_are_bound_to_the_backup(backup):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    raw = files.review.model_dump(mode="json")
    editable = next(r for r in raw["records"] if r["file"])
    editable["file"] = "../../outside"
    (directory / "review.json").write_text(json.dumps(raw))
    with pytest.raises(TransferError, match="file path does not match"):
        ReviewFiles.open(reader, directory)


@pytest.mark.parametrize("repeat_count, remaining", [(5, 2), (3, 3), (None, None)])
def test_schedule_repeat_edits_set_actual_remaining_runs(
    backup, repeat_count, remaining
):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    record = next(r for r in files.review.records if r.key == "schedule/sched_first")
    value = "null" if repeat_count is None else str(repeat_count)
    (directory / record.file).write_text(
        files.text(record).replace("repeat_count: 5", f"repeat_count: {value}")
    )
    choose(
        files,
        lambda r: (
            r.section in {"settings", "people"}
            or r.key in {record.key, "session/sess_ana"}
        ),
        confirm=True,
    )
    write_batch(files)
    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    store = ScheduleStore(storage_settings=config.manager_storage_settings)
    actual = next(
        row
        for row in store.build_telemetry_snapshot()["schedules"]
        if row["schedule_id"] == "sched_first"
    )
    assert (actual["repeat_count"], actual["remaining_count"]) == (
        repeat_count,
        remaining,
    )


@pytest.mark.parametrize("section", ["settings", "schedules"])
def test_editable_yaml_refuses_repeated_settings(backup, section):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    record = next(r for r in files.review.records if r.section == section and r.file)
    text = files.text(record)
    if section == "settings":
        text += "\nvalue: duplicate\n"
    else:
        text = text.replace("repeat_count: 5", "repeat_count: 5\nrepeat_count: 9")
    (directory / record.file).write_text(text)
    with pytest.raises(TransferError, match="repeats setting"):
        files.edited_record(record)


@pytest.mark.parametrize(
    "missing, schedule",
    [
        ("scheduled_target/sched_daily", "sched_daily"),
        ("worker_binding/sched_worker", "sched_worker"),
    ],
)
def test_partial_schedule_selection_requires_saved_delivery_and_worker_setup(
    tmp_path, missing, schedule
):
    from tests.test_team_transfer_schedules import _old_instance
    from team_transfer.review import archive_records
    from team_transfer.sections import RecordOutcome
    from team_transfer.service import plan_import

    path, _store = _old_instance(tmp_path)
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(path),
        out,
        PASSPHRASE,
        owner_person_id="usr_ana",
        sections=SECTIONS,
        key_derivation=new_key_derivation(opslimit=1, memlimit=8192),
    )
    with TransferReader.open(out, PASSPHRASE) as reader:
        choices = {
            name: set(records) for name, records in archive_records(reader).items()
        }
        choices["schedules"].remove(missing)
        plan, _ = plan_import(
            reader, tmp_path / "new", sections=SECTIONS, choices=choices
        )
        decision = next(r for r in plan.records if r.key == f"schedule/{schedule}")
        assert decision.outcome is RecordOutcome.REFUSED
        assert missing in decision.reason


def test_later_model_choice_updates_untouched_bootstrap_overrides(backup):
    from config_loader import load_persistent_llm_override

    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    choose(
        files, lambda r: r.section in {"settings", "people"} and r.key != "llm.model_id"
    )
    write_batch(files)
    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert config.llm.model_id != "source-model"
    choose(files, lambda r: r.key == "llm.model_id")
    write_batch(files)
    actual = load_persistent_llm_override(
        instance_root=str(target), agent_type="TeamArchitect"
    )
    assert actual["model_id"] == "source-model"


def test_memories_can_be_imported_in_reverse_order_without_id_collisions(backup):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    facts = [r for r in files.review.records if r.section == "pinned_memory"]
    choose(
        files, lambda r: r.section in {"settings", "people"} or r.key == facts[1].key
    )
    write_batch(files)
    choose(files, lambda r: r.section == "pinned_memory")
    write_batch(files)
    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    assert sorted(
        r.fact_text
        for r in create_pinned_memory_repository(
            config.manager_storage_settings
        ).list_unexpired_facts()
    ) == ["First fact", "Second fact"]


def test_schedule_requires_its_session_and_validation_changes_no_stored_state(backup):
    from team_transfer.service import plan_import
    from storage.read_only import read_only_repositories
    from storage.sqlite_connection import connect_sqlite
    import sqlite3

    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    choose(files, lambda r: r.section in {"settings", "people"})
    write_batch(files)
    config = GatewayInstanceConfig.load(target / "instance_config.yaml")
    db = config.manager_storage_settings.sqlite_path
    with read_only_repositories(), connect_sqlite(db) as conn:
        before = conn.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        before_version = conn.execute("PRAGMA schema_version").fetchone()[0]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE unintended (id TEXT)")
    choose(files, lambda r: r.key == "schedule/sched_first", confirm=True)
    choices, overrides = files.choices(require_confirmation=True)
    plan, _ = plan_import(
        reader,
        target,
        sections=SECTIONS,
        choices=choices,
        overrides=overrides,
        allow_existing=True,
    )
    assert any("chat session sess_ana" in r.reason for r in plan.records)
    with read_only_repositories(), connect_sqlite(db) as conn:
        assert (
            conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
            == before
        )
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == before_version
    assert (
        ScheduleStore(
            storage_settings=config.manager_storage_settings
        ).get_scheduled_message("sched_first")
        is None
    )


@pytest.mark.parametrize("edit_destination", [False, True])
def test_later_control_selection_reads_source_files_and_preserves_destination_edits(
    tmp_path, edit_destination
):
    from assistant_gateway.bootstrap import bootstrap_instance
    from team_transfer.control import ControlSection

    path, old = new_instance(tmp_path, "old", "team_a")
    bootstrap_instance(old)
    old.layout.soul_path.write_text("Source instructions")
    people = create_gateway_state_repository(old.state_storage_settings)
    people.create_gateway_user(
        canonical_user_id="usr_ana", display_name="Ana", timezone="UTC"
    )
    people.create_team_membership(
        canonical_user_id="usr_ana", team_id="team_a", role="owner"
    )
    sections = (SettingsSection(), ControlSection(), PeopleSection())
    out, target = tmp_path / "control.uatransfer", tmp_path / "new"
    export_team(
        export_source(path),
        out,
        PASSPHRASE,
        owner_person_id="usr_ana",
        sections=sections,
        key_derivation=new_key_derivation(opslimit=1, memlimit=8192),
    )
    with TransferReader.open(out, PASSPHRASE) as reader:
        files = prepare_review(reader, target, tmp_path / "review")
        choose(files, lambda r: r.section in {"settings", "people"})
        choices, overrides = files.choices(require_confirmation=True)
        import_team(
            reader,
            target,
            dry_run=False,
            sections=sections,
            choices=choices,
            overrides=overrides,
            allow_existing=True,
            require_valid=True,
        )
        new = GatewayInstanceConfig.load(target / "instance_config.yaml")
        if edit_destination:
            new.layout.soul_path.write_text("Destination instructions")
        choose(files, lambda r: r.key == "SOUL.md")
        choices, overrides = files.choices(require_confirmation=True)
        plan = import_team(
            reader,
            target,
            dry_run=False,
            sections=sections,
            choices=choices,
            overrides=overrides,
            allow_existing=True,
            require_valid=True,
        )
        assert new.layout.soul_path.read_text() == (
            "Destination instructions" if edit_destination else "Source instructions"
        )
        if edit_destination:
            assert any("edited" in r.reason for r in plan.skipped_records)


@pytest.mark.parametrize("invalid_edit", ["blank_memory", "impossible_cron"])
def test_invalid_later_edits_are_refused_without_blocking_the_instance(backup, invalid_edit):
    from team_transfer.service import plan_import

    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    choose(files, lambda row: row.section in {"settings", "people"})
    write_batch(files)
    config = GatewayInstanceConfig.load_for_process(target / "instance_config.yaml")
    receipt = config.layout.team_import_receipt_path.read_bytes()
    if invalid_edit == "blank_memory":
        record = next(row for row in files.review.records if row.section == "pinned_memory")
        edited = "   \n"
        selected = {record.key}
    else:
        record = next(row for row in files.review.records if row.key == "schedule/sched_first")
        edited = (files.text(record).replace("interval_ms: 3600000", "interval_ms: null")
                  .replace("cron_expression: null", "cron_expression: '0 0 31 2 *'")
                  .replace("cron_timezone: null", "cron_timezone: UTC"))
        selected = {record.key, "session/sess_ana"}
    original = files.text(record)
    (directory / record.file).write_text(edited)
    choose(files, lambda row: row.key in selected, confirm=True)
    choices, overrides = files.choices(require_confirmation=True)
    plan, _context = plan_import(reader, target, sections=SECTIONS, choices=choices,
                                 overrides=overrides, allow_existing=True)
    assert any(row.key == record.key for row in plan.by_outcome(RecordOutcome.REFUSED))
    with pytest.raises(TransferError, match="Selected records were refused"):
        write_batch(files)
    assert config.layout.team_import_receipt_path.read_bytes() == receipt
    assert not config.layout.team_import_marker_path.exists()
    GatewayInstanceConfig.load_for_process(target / "instance_config.yaml")
    (directory / record.file).write_text(original)
    choose(files, lambda row: row.key in selected, confirm=True)
    write_batch(files)
    if invalid_edit == "blank_memory":
        assert create_pinned_memory_repository(config.manager_storage_settings).list_unexpired_facts()[0].fact_text == original
    else:
        assert ScheduleStore(storage_settings=config.manager_storage_settings).get_scheduled_message("sched_first") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["settings", "schedules"])
async def test_browser_returns_an_editable_yaml_error_and_allows_correction(backup, section):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    key = "llm.model_id" if section == "settings" else "schedule/sched_first"
    record = next(row for row in files.review.records if row.key == key)
    original = files.text(record)
    invalid = "value: [unterminated\n" if section == "settings" else original.replace("cron_expression: null", "cron_expression: [unterminated")
    choose(files, lambda row: row.section in {"settings", "people"} or row.key in {key, "session/sess_ana"})
    headers = {"X-Transfer-Token": "test-token"}
    async with TestClient(TestServer(build_review_app(files, "test-token"))) as client:
        payload = dict(section=section, key=key, selected=True, text=invalid, confirmed=section == "schedules")
        assert (await client.post("/api/record", headers=headers, json=payload)).status == 200
        response = await client.post("/api/validate", headers=headers, json={})
        assert response.status == 400
        error = (await response.json())["error"]
        assert "Editable YAML is invalid at line" in error and "unterminated" not in error
        assert response.headers["Cache-Control"] == "no-store"
        assert not target.exists()
        payload["text"] = original
        assert (await client.post("/api/record", headers=headers, json=payload)).status == 200
        response = await client.post("/api/validate", headers=headers, json={})
        assert response.status == 200, await response.text()
        assert (await response.json())["valid"]


@pytest.mark.asyncio
async def test_browser_schedule_confirmation_names_submitted_text_despite_an_external_save(backup, monkeypatch):
    from access_control import safe_open

    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    record = next(row for row in files.review.records if row.key == "schedule/sched_first")
    reviewed = files.text(record)
    external = reviewed.replace("Run sched_first", "Externally changed instructions")
    choose(files, lambda row: row.section in {"settings", "people"} or row.key in {record.key, "session/sess_ana"})
    write_bytes = safe_open.write_bytes

    def external_save(root, parts, data):
        write_bytes(root, parts, data)
        if parts == tuple(record.file.split("/")):
            write_bytes(root, parts, external.encode("utf-8"))

    monkeypatch.setattr(safe_open, "write_bytes", external_save)
    headers = {"X-Transfer-Token": "test-token"}
    async with TestClient(TestServer(build_review_app(files, "test-token"))) as client:
        payload = dict(section="schedules", key=record.key, selected=True, text=reviewed, confirmed=True)
        assert (await client.post("/api/record", headers=headers, json=payload)).status == 200
        current = next(row for row in files.review.records if row.key == record.key)
        assert current.confirmed_digest == text_digest(reviewed)
        assert files.text(current) == external
        response = await client.post("/api/validate", headers=headers, json={})
        assert response.status == 400
        assert "confirm" in (await response.json())["error"].lower()
        assert not target.exists()
        monkeypatch.setattr(safe_open, "write_bytes", write_bytes)
        payload["text"] = external
        assert (await client.post("/api/record", headers=headers, json=payload)).status == 200
        response = await client.post("/api/validate", headers=headers, json={})
        assert response.status == 200, await response.text()
        assert (await response.json())["valid"]


@pytest.mark.asyncio
async def test_browser_schedule_confirmation_requires_the_exact_reviewed_text(backup):
    reader, target, directory = backup
    files = prepare_review(reader, target, directory)
    record = next(row for row in files.review.records if row.key == "schedule/sched_first")
    before = files.text(record)
    headers = {"X-Transfer-Token": "test-token"}
    async with TestClient(TestServer(build_review_app(files, "test-token"))) as client:
        response = await client.post("/api/record", headers=headers, json=dict(
            section="schedules", key=record.key, selected=True, confirmed=True))
        assert response.status == 400
        assert "exact schedule text" in (await response.json())["error"]
    assert files.text(record) == before
    assert record.confirmed_digest is None


@pytest.mark.parametrize(
    "section",
    [section.name for section in default_sections() if not section.after_start],
)
def test_every_reviewed_section_has_a_record_key(section):
    """The review keys every section a backup carries (``archive_records``).

    A section without a case in ``selection.record_key`` made every backup that
    carried it fail to open for review, as knowledge did. An empty record must
    fail on a missing field, not for lack of a contract.
    """
    from team_transfer.selection import record_key

    with pytest.raises(KeyError):
        record_key(section, {})

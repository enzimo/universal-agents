"""Review lesson writes commit a retry receipt with their actual effect, on both databases."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from uuid import uuid4

import pytest

from knowledge_base.records import CODE_WRITE_MISMATCH, KnowledgeError, KnowledgeWriteKey
from knowledge_base.service import DraftInput, KnowledgeService
from runtime_settings.knowledge import KnowledgeSettings
from storage.knowledge_repository import create_knowledge_repository
from storage.postgres_connection import connect_postgres
from tests.knowledge_support import BACKENDS, NOW, agent, knowledge_storage


@pytest.fixture(params=BACKENDS)
def storage(request, tmp_path):
    with knowledge_storage(request.param, tmp_path) as settings:
        yield settings


@pytest.fixture
def unrelated_schema(storage):
    """A different instance's tables must not affect departure cleanup."""

    if not storage.postgres_dsn:
        yield ""
        return
    schema = "unrelated_knowledge_" + uuid4().hex
    with connect_postgres(storage.postgres_dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE TABLE {schema}.objectives (objective_id TEXT, owner_canonical_user_id TEXT)")
        conn.execute(f"INSERT INTO {schema}.objectives VALUES ('other_objective', 'alice')")
        conn.execute(f"CREATE TABLE {schema}.knowledge_pages (elsewhere_only_column TEXT)")
        conn.commit()
    try:
        yield schema
    finally:
        with connect_postgres(storage.postgres_dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()


KEY = KnowledgeWriteKey(operation_id="aar:manager:review_1", payload_digest="lesson_digest")
DRAFT = DraftInput(title="Release lessons", body="Run CI before tagging.")


def _service(storage):
    return KnowledgeService(create_knowledge_repository(storage), settings=KnowledgeSettings(), clock=lambda: NOW)


@pytest.mark.parametrize("proposing", [False, True])
def test_a_receipt_survives_restart_without_another_write(storage, proposing):
    actor = agent("manager", person="alice" if proposing else "")
    service = _service(storage)
    first = service.write(actor, "team/lessons", DRAFT, write_key=KEY)
    replay = _service(storage).write(actor, "team/lessons", DRAFT, write_key=KEY)

    assert replay.receipt == first.receipt
    assert replay.pending is proposing
    assert len(service._repository.export_rows("knowledge_contributions")) == int(proposing)
    assert len(service._repository.export_rows("knowledge_pages")) == int(not proposing)
    assert len(service._repository.export_rows("knowledge_revisions")) == int(not proposing)
    with service._repository._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_write_events").fetchone()[0] == 1


def test_a_receipt_keeps_the_original_revision_and_checks_its_person_and_payload(storage):
    service = _service(storage)
    actor = agent("manager", person="alice")
    saved = service.write(actor, "personal/lessons", DRAFT, write_key=KEY)
    service.write(actor, "personal/lessons", DraftInput(body="Run the full CI suite."), base_revision=1)
    replay = service.write(actor, "personal/lessons", DRAFT, write_key=KEY)

    assert replay.receipt == saved.receipt and replay.receipt.revision == 1
    assert service.read(actor, "personal/lessons").page.revision == 2
    assert service._repository.get_write_receipt(replace(actor, work_person_id="bob"), KEY) is None
    assert service._repository.get_write_receipt(replace(actor, team_id="other_team"), KEY) is None
    with pytest.raises(KnowledgeError) as mismatch:
        service.write(actor, "personal/lessons", DRAFT, write_key=replace(KEY, payload_digest="another_lesson"))
    assert mismatch.value.code == CODE_WRITE_MISMATCH


def test_departure_deletes_only_the_departing_persons_review_receipts(storage, unrelated_schema):
    from storage.person_departure_repository import PersonDepartureRepository

    service = _service(storage)
    alice = agent("manager", person="alice")
    bob = agent("manager", person="bob")
    service.write(alice, "personal/lessons", DRAFT, write_key=KEY)
    service.write(bob, "personal/lessons", DRAFT, write_key=KEY)

    departures = PersonDepartureRepository(storage)
    deleted = departures.delete_person_records("alice")

    assert deleted["knowledge_write_receipts"] == 1
    assert service._repository.get_write_receipt(alice, KEY) is None
    assert service._repository.get_write_receipt(bob, KEY) is not None
    assert service.read(bob, "personal/lessons").page.body == DRAFT.body
    if unrelated_schema:
        with departures._connect() as conn:
            assert not departures._table_exists(conn, "objectives")
            assert "elsewhere_only_column" not in departures._columns(conn, "knowledge_pages")
            assert conn.execute(f"SELECT COUNT(*) FROM {unrelated_schema}.objectives").fetchone()[0] == 1


@pytest.mark.parametrize("proposing", [False, True])
def test_failure_while_storing_the_receipt_rolls_back_the_whole_write(storage, proposing, monkeypatch):
    service = _service(storage)
    actor = agent("manager", person="alice" if proposing else "")
    original = service._repository._store_write_receipt

    def fail_after_receipt(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Interrupted before commit")

    with monkeypatch.context() as patch:
        patch.setattr(service._repository, "_store_write_receipt", fail_after_receipt)
        with pytest.raises(RuntimeError, match="Interrupted before commit"):
            service.write(actor, "team/lessons", DRAFT, write_key=KEY)

    assert service._repository.get_write_receipt(actor, KEY) is None
    for table in ("knowledge_pages", "knowledge_revisions", "knowledge_contributions"):
        assert service._repository.export_rows(table) == []
    assert service.write(actor, "team/lessons", DRAFT, write_key=KEY).receipt is not None


@pytest.mark.parametrize("proposing", [False, True])
def test_concurrent_retries_commit_one_effect_and_one_receipt(storage, proposing, monkeypatch):
    service = _service(storage)
    actor = agent("manager", person="alice" if proposing else "")
    method = "add_contribution" if proposing else "apply_page_change"
    original = getattr(service._repository, method)
    prepared = Barrier(2)

    def write_together(*args, **kwargs):
        # Both requests have prepared their write before either acquires the
        # transaction lock. A service-level receipt read alone cannot guard this.
        prepared.wait(timeout=15)
        return original(*args, **kwargs)

    monkeypatch.setattr(service._repository, method, write_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.write, actor, "team/lessons", DRAFT, write_key=KEY) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]

    assert results[0].receipt == results[1].receipt
    assert len(service._repository.export_rows("knowledge_contributions")) == int(proposing)
    assert len(service._repository.export_rows("knowledge_pages")) == int(not proposing)
    assert len(service._repository.export_rows("knowledge_revisions")) == int(not proposing)


@pytest.mark.parametrize("editing", [False, True])
def test_a_retry_replays_a_receipt_committed_during_preflight(storage, editing, monkeypatch):
    first = _service(storage)
    retry = _service(storage)
    actor = agent("manager")
    revision = 0
    if editing:
        first.write(actor, "team/lessons", DraftInput(title="Release lessons", body="Tag releases."))
        revision = 1
    inspected = Event()
    committed = Event()
    original = retry._repository.get_write_receipt

    def inspect_before_commit(*args, **kwargs):
        receipt = original(*args, **kwargs)
        if not inspected.is_set():
            assert receipt is None
            inspected.set()
            assert committed.wait(timeout=15)
        return receipt

    monkeypatch.setattr(retry._repository, "get_write_receipt", inspect_before_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        replay = pool.submit(retry.write, actor, "team/lessons", DRAFT, base_revision=revision, write_key=KEY)
        assert inspected.wait(timeout=15)
        saved = first.write(actor, "team/lessons", DRAFT, base_revision=revision, write_key=KEY)
        committed.set()
        result = replay.result(timeout=20)

    assert result.receipt == saved.receipt
    assert len(first._repository.export_rows("knowledge_revisions")) == revision + 1

"""Several processes writing one knowledge store: no lost edits, limits exact across processes.

Each worker is a separate OS process with its own connection, as agent
processes are. These are the guarantees the store's write lock provides on
SQLite (``BEGIN IMMEDIATE``) and PostgreSQL (transaction advisory lock).
"""

from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest

from knowledge_base.records import CODE_CONFLICT, CODE_LIMIT, KnowledgeError
from knowledge_base.service import DraftInput, KnowledgeService
from knowledge_base.vocabulary import PageStatus
from runtime_settings.knowledge import KnowledgeSettings, KnowledgeWriteLimits
from storage.config import StorageSettings
from storage.knowledge_repository import create_knowledge_repository
from tests.knowledge_support import BACKENDS, agent, knowledge_storage, person

pytestmark = pytest.mark.integration

WRITERS = 8
EDITS_EACH = 5


def _service(settings: StorageSettings, **limits: int) -> KnowledgeService:
    return KnowledgeService(
        create_knowledge_repository(settings),
        settings=KnowledgeSettings(limits=KnowledgeWriteLimits(**limits)),
    )


def _edit_until_saved(settings: StorageSettings, writer: int) -> tuple[int, int]:
    """Read, edit, and on a conflict read again, until ``EDITS_EACH`` edits are saved."""

    svc = _service(settings, page_edits_per_agent=1000, writes_per_agent_per_hour=10000, edit_war_edits=1000)
    actor = agent(f"writer_{writer}")
    saved = conflicts = 0
    while saved < EDITS_EACH:
        current = svc.read(actor, "team/shared").page
        try:
            svc.write(actor, "team/shared", DraftInput(body=f"edit {writer}-{saved}"), base_revision=current.revision)
            saved += 1
        except KnowledgeError as error:
            assert error.code == CODE_CONFLICT, error.code
            conflicts += 1
    return saved, conflicts


def _create_pages(settings: StorageSettings, worker: int) -> int:
    svc = _service(settings, writes_per_agent_per_hour=12, new_pages_per_work=1000)
    actor = agent("one_agent")  # the same agent writing from several processes
    created = 0
    for index in range(10):
        try:
            svc.write(actor, f"team/page-{worker}-{index}", DraftInput(title="P", body=f"Page {worker} {index}."))
            created += 1
        except KnowledgeError as error:
            assert error.code == CODE_LIMIT, error.code
    return created


def _propose_archives(settings: StorageSettings, worker: int) -> int:
    svc = _service(settings, archives_per_agent_per_hour=3, writes_per_agent_per_hour=1000)
    actor = agent("one_agent", person="alice")
    proposed = 0
    for index in range(4):
        try:
            svc.archive(actor, f"team/archive-{worker}-{index}", base_revision=1, reason="Outdated")
            proposed += 1
        except KnowledgeError as error:
            assert error.code == CODE_LIMIT, error.code
            assert error.details["limit"] == "archives_per_agent_per_hour"
    return proposed


@pytest.fixture(params=BACKENDS)
def settings(request, tmp_path):
    with knowledge_storage(request.param, tmp_path) as storage_settings:
        yield storage_settings


def _pool(workers: int) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))


def test_concurrent_edits_lose_nothing(settings):
    svc = _service(settings)
    page = svc.write(agent("seed"), "team/shared", DraftInput(title="Shared", body="start")).page
    with _pool(WRITERS) as pool:
        results = list(pool.map(_edit_until_saved, [settings] * WRITERS, range(WRITERS)))
    assert sum(saved for saved, _ in results) == WRITERS * EDITS_EACH
    repository = create_knowledge_repository(settings)
    final = repository.get_page_by_id(page.page_id)
    assert final.revision == 1 + WRITERS * EDITS_EACH
    revisions = repository.list_revisions(page.page_id, limit=1000)
    assert sorted(r.revision for r in revisions) == list(range(1, final.revision + 1))
    bodies = {r.body for r in revisions}
    assert all(f"edit {w}-{n}" in bodies for w in range(WRITERS) for n in range(EDITS_EACH))


def test_write_limit_is_exact_across_processes(settings):
    with _pool(4) as pool:
        created = sum(pool.map(_create_pages, [settings] * 4, range(4)))
    assert created == 12


def test_pending_archive_limit_is_exact_across_processes(settings):
    svc = _service(settings)
    for worker in range(4):
        for index in range(4):
            svc.write(person("admin"), f"team/archive-{worker}-{index}", DraftInput(title="Old", body="Old instructions."))

    with _pool(4) as pool:
        proposed = sum(pool.map(_propose_archives, [settings] * 4, range(4)))

    assert proposed == 3
    actor = agent("one_agent", person="alice")
    assert len(svc.list_contributions(actor)) == 3
    assert all(
        svc.read(actor, f"team/archive-{worker}-{index}").page.status is PageStatus.ACTIVE
        for worker in range(4)
        for index in range(4)
    )

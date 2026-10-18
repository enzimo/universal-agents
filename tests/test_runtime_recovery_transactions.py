"""Recovery custody across actual transactions, contenders, and process death.

The last tests cover a previous owner that is still running after a takeover:
its saves are refused (``RuntimeCustodyLost``) instead of claiming the work again.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from tests.runtime_snapshot_fixtures import seed_deferred, snapshot_json

from storage.config import resolve_storage_settings
from storage.runtime_state_repository import (
    RuntimeCustodyLost,
    RuntimeRecoveryConflict,
    create_runtime_state_repository,
)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=[pytest.mark.live, pytest.mark.postgres])])
def recovery_store(request, tmp_path):
    if request.param == "postgres":
        dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip(
                "LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL recovery"
            )
        config = {"backend": "postgres", "postgres": {"dsn": dsn}}
    else:
        config = {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "state.db")}}
    repo = create_runtime_state_repository(resolve_storage_settings(config))
    owners = [f"recovery_{uuid4().hex}_{suffix}" for suffix in ("source", "one", "two")]
    yield repo, config, owners
    for owner in owners:
        repo.clear_task_runtime(owner)
        repo.remove_deferred_messages(owner_agent_id=owner, message_ids=SEEDED_MESSAGES, expect_existing=False)


SEEDED_MESSAGES = ("work", "work2", "m1")


def save(repo, kind, owner, *, expect_existing, message_id="work"):
    """One write of the given kind of saved work, as its owner makes it."""
    if kind == "task_runtime":
        repo.save_task_runtime(
            owner_agent_id=owner, owner_agent_name="Original", team_id="recovery_tests",
            owner_named_agent_type="Worker", runtime_state_json=snapshot_json(), expect_existing=expect_existing,
        )
    else:
        seed_deferred(repo, owner, message_id, team_id="recovery_tests", expect_existing=expect_existing)


def seed(repo, kind, owner):
    save(repo, kind, owner, expect_existing=False)
    return getattr(repo, f"load_{kind}_by_owner")(owner)


@pytest.mark.parametrize("kind", ["task_runtime", "deferred_messages"])
def test_only_one_replacement_can_commit_the_same_recovery(recovery_store, kind):
    repo, config, owners = recovery_store
    sources = seed(repo, kind, owners[0])
    barrier = Barrier(2)

    def recover(owner):
        contender = create_runtime_state_repository(resolve_storage_settings(config))
        barrier.wait()
        try:
            getattr(contender, f"commit_{kind}_recovery")(
                sources=sources, replacement=replace(sources[0], owner_agent_id=owner)
            )
        except RuntimeRecoveryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        committed = list(executor.map(recover, owners[1:]))
    assert sorted(committed) == [False, True]
    load = getattr(repo, f"load_{kind}_by_owner")
    assert load(owners[0]) == []
    assert sum(len(load(owner)) for owner in owners[1:]) == 1


@pytest.mark.parametrize("kind", ["task_runtime", "deferred_messages"])
def test_recovery_rejects_a_snapshot_changed_since_decode(recovery_store, kind):
    repo, _, owners = recovery_store
    original = seed(repo, kind, owners[0])
    # A current owner wrote another snapshot while the replacement decoded its copy.
    save(repo, kind, owners[0], expect_existing=True, message_id="work2")
    with pytest.raises(RuntimeRecoveryConflict):
        getattr(repo, f"commit_{kind}_recovery")(
            sources=original, replacement=replace(original[0], owner_agent_id=owners[1])
        )
    load = getattr(repo, f"load_{kind}_by_owner")
    assert len(load(owners[0])) == 1
    assert load(owners[1]) == []


@pytest.mark.parametrize("kind", ["task_runtime", "deferred_messages"])
@pytest.mark.parametrize("death_at", ["after_read", "after_delete", "after_commit"])
def test_process_death_leaves_one_recoverable_custodian(recovery_store, kind, death_at):
    repo, config, owners = recovery_store
    seed(repo, kind, owners[0])
    script = """
import json, os, sys
from dataclasses import replace
from storage.config import resolve_storage_settings
from storage.runtime_state_repository import create_runtime_state_repository
config, kind, source, destination, death_at = json.loads(sys.argv[1])
repo = create_runtime_state_repository(resolve_storage_settings(config))
rows = getattr(repo, f'load_{kind}_by_owner')(source)
if death_at == 'after_read':
    os._exit(19)
if death_at == 'after_delete':
    delete = repo._delete_rows_with_connection
    def die_after_delete(*args):
        delete(*args)
        os._exit(19)
    repo._delete_rows_with_connection = die_after_delete
getattr(repo, f'commit_{kind}_recovery')(
    sources=rows, replacement=replace(rows[0], owner_agent_id=destination)
)
os._exit(19)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            json.dumps([config, kind, *owners[:2], death_at]),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 19, result.stderr
    load = getattr(repo, f"load_{kind}_by_owner")
    expected_owner = owners[1] if death_at == "after_commit" else owners[0]
    assert len(load(expected_owner)) == 1
    assert sum(len(load(owner)) for owner in owners) == 1


# -- a previous owner that is still running cannot save taken-over work -------------


def save_again(repo, kind, owner, *, expect_existing):
    save(repo, kind, owner, expect_existing=expect_existing, message_id="work2")


@pytest.mark.parametrize("kind", ["task_runtime", "deferred_messages"])
def test_a_former_owner_cannot_recreate_custody_with_a_full_save(recovery_store, kind):
    """Before 2026-10-17 this save wrote the old owner's row again: two owners, one task."""
    repo, _, owners = recovery_store
    sources = seed(repo, kind, owners[0])
    getattr(repo, f"commit_{kind}_recovery")(
        sources=sources, replacement=replace(sources[0], owner_agent_id=owners[1])
    )
    with pytest.raises(RuntimeCustodyLost) as refused:
        save_again(repo, kind, owners[0], expect_existing=True)
    assert (refused.value.owner_agent_id, refused.value.store.value) == (owners[0], kind)
    load = getattr(repo, f"load_{kind}_by_owner")
    assert load(owners[0]) == []
    assert len(load(owners[1])) == 1


@pytest.mark.parametrize("kind", ["task_runtime", "deferred_messages"])
def test_a_first_save_still_creates_custody(recovery_store, kind):
    repo, _, owners = recovery_store
    save_again(repo, kind, owners[0], expect_existing=False)
    save_again(repo, kind, owners[0], expect_existing=True)
    assert len(getattr(repo, f"load_{kind}_by_owner")(owners[0])) == 1


@pytest.mark.parametrize("kind", ["task_runtime", "deferred_messages"])
def test_custody_check_and_takeover_serialize(recovery_store, kind):
    """The owner's save and a takeover race; exactly one wins and one agent holds the work."""
    repo, config, owners = recovery_store
    sources = seed(repo, kind, owners[0])
    barrier = Barrier(2)

    def owner_saves():
        contender = create_runtime_state_repository(resolve_storage_settings(config))
        barrier.wait()
        try:
            save_again(contender, kind, owners[0], expect_existing=True)
        except RuntimeCustodyLost:
            return "refused"
        return "saved"

    def replacement_takes_over():
        contender = create_runtime_state_repository(resolve_storage_settings(config))
        barrier.wait()
        try:
            getattr(contender, f"commit_{kind}_recovery")(
                sources=sources, replacement=replace(sources[0], owner_agent_id=owners[1])
            )
        except RuntimeRecoveryConflict:
            return "conflict"
        return "took_over"

    with ThreadPoolExecutor(max_workers=2) as executor:
        saved = executor.submit(owner_saves)
        taken = executor.submit(replacement_takes_over)
        outcome = (saved.result(timeout=30), taken.result(timeout=30))
    assert outcome in {("saved", "conflict"), ("refused", "took_over")}
    load = getattr(repo, f"load_{kind}_by_owner")
    assert sum(len(load(owner)) for owner in owners[:2]) == 1


def test_a_history_write_without_a_row_reports_custody_lost(recovery_store):
    repo, _, owners = recovery_store
    from custom_messages.contracts.runtime_snapshot_v3 import decode_runtime_snapshot

    [task] = decode_runtime_snapshot(snapshot_json()).tasks
    with pytest.raises(RuntimeCustodyLost):
        repo.save_task_runtime_task(
            owner_agent_id=owners[0], task=task, task_plan_links={},
            task_objective_links={}, plan_objective_links={},
        )


def test_a_deferred_hand_off_and_a_deferred_takeover_serialize(recovery_store):
    """The task save that takes a deferred message races a takeover of the deferred row.

    Exactly one wins: either the message moved into the old owner's task row and
    the takeover's transfer is refused, or the takeover holds the message and
    the old owner's save wrote nothing.
    """
    repo, config, owners = recovery_store
    envelope = {"msg_header": {"message_id": "m1"}, "msg_content": {"task_id": "m1"}}
    repo.add_deferred_message(
        owner_agent_id=owners[0], owner_agent_name="Original", team_id="recovery_tests",
        owner_named_agent_type="Worker", message_id="m1", envelope_json=json.dumps(envelope),
        first=False, expect_existing=False,
    )
    sources = repo.load_deferred_messages_by_owner(owners[0])
    barrier = Barrier(2)

    def hand_off():
        contender = create_runtime_state_repository(resolve_storage_settings(config))
        barrier.wait()
        try:
            contender.save_task_runtime(
                owner_agent_id=owners[0], owner_agent_name="Original", team_id="recovery_tests",
                owner_named_agent_type="Worker", runtime_state_json=snapshot_json("m1"),
                expect_existing=False, deferred_handoff="m1",
            )
        except RuntimeCustodyLost:
            return "refused"
        return "moved"

    def take_over():
        contender = create_runtime_state_repository(resolve_storage_settings(config))
        barrier.wait()
        try:
            contender.commit_deferred_messages_recovery(
                sources=sources, replacement=replace(sources[0], owner_agent_id=owners[1])
            )
        except RuntimeRecoveryConflict:
            return "conflict"
        return "took_over"

    with ThreadPoolExecutor(max_workers=2) as executor:
        moved = executor.submit(hand_off)
        taken = executor.submit(take_over)
        outcome = (moved.result(timeout=30), taken.result(timeout=30))
    assert outcome in {("moved", "conflict"), ("refused", "took_over")}
    in_task_row = len(repo.load_task_runtime_by_owner(owners[0])) == 1
    taken_rows = repo.load_deferred_messages_by_owner(owners[1])
    in_new_deferred = bool(taken_rows) and "m1" in taken_rows[0].messages_json
    assert in_task_row != in_new_deferred

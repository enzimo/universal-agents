"""Recovery custody across actual transactions, contenders, and process death."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from tests.runtime_snapshot_fixtures import snapshot_json

from storage.config import resolve_storage_settings
from storage.runtime_state_repository import (
    RuntimeRecoveryConflict,
    create_runtime_state_repository,
)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.live)])
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
        repo.clear_deferred_messages(owner)


def seed(repo, kind, owner):
    payload = "runtime_state_json" if kind == "task_runtime" else "messages_json"
    getattr(repo, f"save_{kind}")(
        owner_agent_id=owner,
        owner_agent_name="Original",
        team_id="recovery_tests",
        owner_named_agent_type="Worker",
        **{payload: snapshot_json() if kind == "task_runtime" else '[{"work":1}]'},
    )
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
    seed(repo, kind, owners[0])
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

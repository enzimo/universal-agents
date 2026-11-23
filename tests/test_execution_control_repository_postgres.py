"""Real PostgreSQL tests for PostgresExecutionControlRepository SSH tables, upgrades, fencing, and lock ordering.

Exercises ``begin_schema_setup`` advisory locking, 0.9 -> 0.10 in-place column
upgrades via ``ensure_column``, SSH target/session/interaction persistence,
``claim_interactive_session`` ``FOR UPDATE`` + fence CAS concurrency, and
deadlock resistance under canonical table lock ordering
(``execution_ssh_targets`` -> ``execution_interactive_sessions`` ->
``execution_ssh_interactions``).

Needs ``LIVE_TEST_POSTGRES_DSN``; each test runs in an isolated PostgreSQL
schema dropped in ``finally``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import threading
import time
from typing import Any
from uuid import uuid4

import pytest

from custom_messages.contracts.access_v2 import ChangeApprovalMode
from custom_messages.contracts.common_v1 import contract_sha256
from runtime_hosting.execution_contracts import (
    ArtifactKind,
    ArtifactReferenceV1,
    ArtifactStorageBackend,
    ExecutionActivationKind,
    InteractiveSessionStatus,
    SSHDelegationBoundsV1,
    SSHSecretRecipe,
    SSHTargetStatus,
)
from storage.execution_control_repository import (
    ImmutableRecordConflict,
    PostgresExecutionControlRepository,
    StaleExecutionFence,
)
from tests.execution_ssh_repository_support import (
    build_enrolled_ssh_target,
    build_pending_enrolled_session,
    build_ssh_activation,
    build_ssh_interaction_activation,
    create_live_enrolled_session,
    team_owner_scope,
)


pytestmark = [pytest.mark.live, pytest.mark.postgres, pytest.mark.integration]

TEAM = "team_exec_pg"
PAUSE_SECONDS = 0.35


@contextmanager
def isolated_postgres_dsn() -> Iterator[str]:
    from psycopg.conninfo import make_conninfo

    from storage.postgres_connection import connect_postgres

    dsn = os.environ.get("LIVE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("LIVE_TEST_POSTGRES_DSN is required for real PostgreSQL execution control repository tests")
    schema = "exec_ctrl_test_" + uuid4().hex
    with connect_postgres(dsn) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.commit()
    try:
        yield make_conninfo(dsn, options=f"-csearch_path={schema}")
    finally:
        with connect_postgres(dsn) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")
            conn.commit()


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    with isolated_postgres_dsn() as dsn:
        yield dsn


class _PausedAfterStatement:
    """Connection wrapper that signals ``reached`` and pauses once after a statement matching ``marker``."""

    def __init__(self, conn: Any, marker: str, reached: threading.Event, pause_seconds: float = PAUSE_SECONDS) -> None:
        self._conn = conn
        self._marker = marker
        self._reached = reached
        self._pause_seconds = pause_seconds

    def __enter__(self) -> "_PausedAfterStatement":
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: object) -> object:
        return self._conn.__exit__(*exc)

    def execute(self, sql: str, params: object = None) -> Any:
        cursor = self._conn.execute(sql, params)
        if self._marker in sql and not self._reached.is_set():
            self._reached.set()
            time.sleep(self._pause_seconds)
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _assert_no_deadlock_errors(errors: list[BaseException]) -> None:
    for exc in errors:
        name = exc.__class__.__name__
        assert name != "DeadlockDetected" and "deadlock" not in str(exc).lower(), f"PostgreSQL deadlock occurred: {exc!r}"


def test_begin_schema_setup_fresh_and_concurrent_initialization(
    pg_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Concurrent PostgresExecutionControlRepository initialization serializes via begin_schema_setup without DDL races."""

    from storage.postgres_connection import connect_postgres

    worker_count = 6
    barrier = threading.Barrier(worker_count)

    def init_repo(_: int) -> PostgresExecutionControlRepository:
        barrier.wait(10)
        return PostgresExecutionControlRepository(pg_dsn)

    with caplog.at_level(logging.INFO, logger="storage.execution_control_repository"):
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            repos = list(pool.map(init_repo, range(worker_count)))

    assert len(repos) == worker_count
    assert "storage_column_added" not in caplog.text
    with connect_postgres(pg_dsn) as conn:
        tables = {
            str(row["table_name"])
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            ).fetchall()
        }
    assert {
        "execution_ssh_targets",
        "execution_interactive_sessions",
        "execution_ssh_interactions",
    } <= tables


def test_upgrade_from_0_9_postgres_schema_adds_ssh_columns_and_preserves_digests(
    pg_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Opening 0.10 PostgresExecutionControlRepository on a 0.9 schema adds the 5 SSH columns via ensure_column."""

    from storage.postgres_connection import connect_postgres

    PostgresExecutionControlRepository(pg_dsn)
    owner = team_owner_scope(TEAM)
    target_09_payload = {
        "host": "legacy-pg.internal",
        "port": 22,
        "username": "deploy",
        "host_key_algorithm": "ssh-ed25519",
        "host_key_fingerprint": "SHA256:ABCDEFGHIJKLMNOPQRSTUVWX",
        "credential_binding_id": "legacy_disk_key",
        "remote_persistent_session_name": None,
        "allow_port_forwarding": False,
        "allow_agent_forwarding": False,
        "allow_x11_forwarding": False,
    }
    target_09_digest = contract_sha256(target_09_payload)
    connect_09 = build_ssh_activation(
        ExecutionActivationKind.SSH_CONNECT,
        resource_id="ssh_sess_pg_09",
        resource_digest=target_09_digest,
        owner_scope=owner,
        mode=ChangeApprovalMode.HELD_ACCESS,
        function="open_execution_ssh_session",
    )
    interact_09 = build_ssh_activation(
        ExecutionActivationKind.SSH_INTERACT,
        resource_id="ssh_sess_pg_09",
        resource_digest=target_09_digest,
        owner_scope=owner,
        mode=ChangeApprovalMode.PERSON_APPROVAL,
        function="change:execution_ssh_interaction",
    )
    session_09_json = json.dumps(
        {
            "schema_version": 2,
            "session_id": "ssh_sess_pg_09",
            "owner": owner.model_dump(mode="json"),
            "target": target_09_payload,
            "target_digest": target_09_digest,
            "connect_activation": connect_09.model_dump(mode="json"),
            "status": "pending",
            "fence": 0,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at_utc": None,
            "terminal_rows": 24,
            "terminal_columns": 80,
            "transcript_bytes": 0,
            "transcript_artifact": None,
            "created_by_task_id": "task_pg_09",
            "created_at_utc": "2026-10-01T00:00:00+00:00",
            "updated_at_utc": "2026-10-01T00:00:00+00:00",
        },
        sort_keys=True,
    )

    # Downgrade schema in-place to match 0.9 before reopening 0.10.
    with connect_postgres(pg_dsn) as conn:
        conn.execute("DROP TABLE execution_ssh_targets")
        conn.execute(
            "ALTER TABLE execution_ssh_interactions "
            "DROP COLUMN delegation_json, DROP COLUMN revoked_at_utc, DROP COLUMN revoked_by"
        )
        conn.execute(
            "ALTER TABLE execution_interactive_sessions "
            "DROP COLUMN revoked_at_utc, DROP COLUMN revoked_by"
        )
        conn.execute(
            """
            INSERT INTO execution_interactive_sessions (
                session_id, team_id, owner_scope_kind, owner_id, generation,
                observed_generation, desired_or_status, lease_owner, lease_token,
                lease_expires_at_utc, record_json
            ) VALUES (%s, %s, %s, %s, 0, 0, 'pending', NULL, NULL, NULL, %s)
            """,
            ("ssh_sess_pg_09", owner.team_id, owner.scope_kind.value, owner.owner_id, session_09_json),
        )
        conn.execute(
            """
            INSERT INTO execution_ssh_interactions (
                session_id, team_id, owner_scope_kind, owner_id, record_json
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            ("ssh_sess_pg_09", owner.team_id, owner.scope_kind.value, owner.owner_id, interact_09.model_dump_json()),
        )
        conn.commit()

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="storage.execution_control_repository"):
        upgraded = PostgresExecutionControlRepository(pg_dsn)

    assert caplog.text.count("storage_column_added") == 5
    loaded_session = upgraded.get_interactive_session(team_id=TEAM, session_id="ssh_sess_pg_09")
    assert loaded_session is not None
    assert loaded_session.target_digest == target_09_digest
    assert loaded_session.target.calculate_digest() == target_09_digest
    loaded_interaction = upgraded.get_ssh_interaction(team_id=TEAM, session_id="ssh_sess_pg_09")
    assert loaded_interaction is not None
    assert loaded_interaction.bounds is None
    assert loaded_interaction.revoked_at_utc is None
    assert loaded_interaction.activation.binds_session(loaded_session)
    assert upgraded.get_interactive_session_revocation(team_id=TEAM, session_id="ssh_sess_pg_09") is None
    assert upgraded.list_ssh_targets(team_id=TEAM) == ()

    # Subsequent reopen sees all columns already present and logs no upgrade events.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="storage.execution_control_repository"):
        PostgresExecutionControlRepository(pg_dsn)
    assert "storage_column_added" not in caplog.text


def test_ssh_target_interaction_bootstrap_lifecycle_and_latch(pg_dsn: str) -> None:
    """put_ssh_target idempotency/conflicts, password bootstrap exclusivity/takeover/latch, and delegation bounds."""

    from storage.postgres_connection import connect_postgres

    repo = PostgresExecutionControlRepository(pg_dsn)
    owner = team_owner_scope(TEAM)
    target = build_enrolled_ssh_target(owner, target_id="ssh_tgt_pg_life")

    assert repo.put_ssh_target(target) == target
    assert repo.put_ssh_target(target) == target
    with pytest.raises(ImmutableRecordConflict, match="different content"):
        repo.put_ssh_target(build_enrolled_ssh_target(owner, target_id=target.target_id, host="conflict.internal"))
    assert repo.get_ssh_target(team_id="other_team", target_id=target.target_id) is None

    other_target = build_enrolled_ssh_target(owner, target_id="ssh_tgt_pg_other", host="other.internal")
    repo.put_ssh_target(other_target)
    expired_target = build_enrolled_ssh_target(
        owner,
        target_id="ssh_tgt_pg_expired",
        expires_at_utc=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    repo.put_ssh_target(expired_target)
    assert {item.target_id for item in repo.list_ssh_targets(team_id=TEAM, include_inactive=False)} == {
        target.target_id,
        other_target.target_id,
    }

    # Unknown and unleased sessions cannot claim bootstrap.
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target.target_id, session_id="missing") is False
    unleased = build_pending_enrolled_session(target, session_id="ssh_sess_pg_unleased")
    repo.create_interactive_session(unleased)
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target.target_id, session_id=unleased.session_id) is False

    sess_a = create_live_enrolled_session(repo, target, session_id="ssh_sess_pg_a", broker_id="b-a", lease_token="tok-a")
    sess_b = create_live_enrolled_session(repo, target, session_id="ssh_sess_pg_b", broker_id="b-b", lease_token="tok-b")
    sess_wrong = create_live_enrolled_session(repo, other_target, session_id="ssh_sess_pg_wrong", broker_id="b-w", lease_token="tok-w")
    sess_exp = create_live_enrolled_session(repo, expired_target, session_id="ssh_sess_pg_exp", broker_id="b-e", lease_token="tok-e")

    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target.target_id, session_id=sess_wrong.session_id) is False
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=expired_target.target_id, session_id=sess_exp.session_id) is False

    # Exclusive claim, idempotent re-claim, release, and re-acquire.
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target.target_id, session_id=sess_a.session_id) is True
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target.target_id, session_id=sess_a.session_id) is True
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target.target_id, session_id=sess_b.session_id) is False
    assert repo.release_ssh_target_password_bootstrap_claim(team_id=TEAM, target_id=target.target_id, session_id=sess_b.session_id) is False
    assert repo.release_ssh_target_password_bootstrap_claim(team_id=TEAM, target_id=target.target_id, session_id=sess_a.session_id) is True
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target.target_id, session_id=sess_b.session_id) is True

    # Takeover after holder lease expiry.
    expired_iso = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    with connect_postgres(pg_dsn) as conn:
        expired_b = sess_b.model_copy(update={"lease_expires_at_utc": datetime.fromisoformat(expired_iso)})
        conn.execute(
            "UPDATE execution_interactive_sessions SET lease_expires_at_utc = %s, record_json = %s WHERE session_id = %s",
            (expired_iso, expired_b.model_dump_json(), sess_b.session_id),
        )
        conn.commit()
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target.target_id, session_id=sess_a.session_id) is True

    # Takeover after holder revocation.
    repo.revoke_interactive_session(team_id=TEAM, session_id=sess_a.session_id, revoked_by="admin_pg")
    sess_c = create_live_enrolled_session(repo, target, session_id="ssh_sess_pg_c", broker_id="b-c", lease_token="tok-c")
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target.target_id, session_id=sess_c.session_id) is True

    # Delegation bounds round-trip and permanent bootstrap latch.
    bounds = SSHDelegationBoundsV1(
        delegated_agent_type="HostBootstrapper",
        delegated_task_id="work_pg_1",
        allow_commands=True,
        allowed_secret_recipes=(SSHSecretRecipe.TAILSCALE_UP,),
    )
    stored_interaction = repo.put_ssh_interaction(build_ssh_interaction_activation(sess_c, bounds=bounds), bounds=bounds)
    assert stored_interaction.bounds == bounds
    assert stored_interaction.activation.binds_session(sess_c, bounds=bounds)

    latched = repo.mark_ssh_target_password_bootstrap_completed(team_id=TEAM, target_id=target.target_id)
    assert latched is not None
    assert latched.password_bootstrap_completed_at_utc is not None
    assert latched.enrollment_digest() == target.enrollment_digest()
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target.target_id, session_id=sess_c.session_id) is False


def test_revoke_ssh_target_and_session_fencing_and_coalesce(pg_dsn: str) -> None:
    """Cascading target revocation isolates other targets, preserves COALESCE metadata, and fences renewals."""

    repo = PostgresExecutionControlRepository(pg_dsn)
    owner = team_owner_scope(TEAM)
    target_a = build_enrolled_ssh_target(owner, target_id="ssh_tgt_pg_rev_a", host="rev-a.internal")
    target_b = build_enrolled_ssh_target(owner, target_id="ssh_tgt_pg_rev_b", host="rev-b.internal")
    repo.put_ssh_target(target_a)
    repo.put_ssh_target(target_b)

    sess_a1 = create_live_enrolled_session(repo, target_a, session_id="ssh_sess_pg_ra1", broker_id="b1", lease_token="t1")
    sess_a2 = create_live_enrolled_session(repo, target_a, session_id="ssh_sess_pg_ra2", broker_id="b2", lease_token="t2")
    sess_b1 = create_live_enrolled_session(repo, target_b, session_id="ssh_sess_pg_rb1", broker_id="b3", lease_token="t3")
    for sess in (sess_a1, sess_a2, sess_b1):
        repo.put_ssh_interaction(build_ssh_interaction_activation(sess))

    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target_a.target_id, session_id=sess_a1.session_id) is True

    # Stale release=True must roll back without clearing sess_a1's bootstrap claim.
    with pytest.raises(StaleExecutionFence):
        repo.update_claimed_interactive_session(
            sess_a1.model_copy(update={"status": InteractiveSessionStatus.CLOSED}),
            broker_id="b1",
            lease_token="wrong-token",
            fence=sess_a1.fence,
            release=True,
        )
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target_a.target_id, session_id=sess_a2.session_id) is False

    first_time = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    second_time = first_time + timedelta(minutes=10)
    rev_first = repo.revoke_ssh_target(
        team_id=TEAM, target_id=target_a.target_id, revoked_by="admin_first", revoked_at_utc=first_time
    )
    assert rev_first is not None and rev_first.status == SSHTargetStatus.REVOKED
    rev_second = repo.revoke_ssh_target(
        team_id=TEAM, target_id=target_a.target_id, revoked_by="admin_second", revoked_at_utc=second_time
    )
    assert rev_second is not None
    assert rev_second.revoked_by == "admin_first" and rev_second.revoked_at_utc == first_time

    for sess in (sess_a1, sess_a2):
        rev = repo.get_interactive_session_revocation(team_id=TEAM, session_id=sess.session_id)
        assert rev is not None and rev.revoked_by == "admin_first" and rev.revoked_at_utc == first_time
        inter = repo.get_ssh_interaction(team_id=TEAM, session_id=sess.session_id)
        assert inter is not None and inter.revoked_by == "admin_first" and inter.revoked_at_utc == first_time

    # Lease renewal on revoked session fails closed, while release=True persists final state and keeps revocation.
    with pytest.raises(StaleExecutionFence, match="revoked"):
        repo.update_claimed_interactive_session(
            sess_a1.model_copy(update={"transcript_bytes": 64}),
            broker_id="b1",
            lease_token="t1",
            fence=sess_a1.fence,
            release=False,
        )
    released = repo.update_claimed_interactive_session(
        sess_a1.model_copy(update={"status": InteractiveSessionStatus.CLOSED, "transcript_bytes": 64}),
        broker_id="b1",
        lease_token="t1",
        fence=sess_a1.fence,
        release=True,
    )
    assert released.lease_owner is None
    assert repo.get_interactive_session_revocation(team_id=TEAM, session_id=sess_a1.session_id) is not None

    # Unrelated target_b remains active and unrevoked.
    assert repo.get_interactive_session_revocation(team_id=TEAM, session_id=sess_b1.session_id) is None
    inter_b = repo.get_ssh_interaction(team_id=TEAM, session_id=sess_b1.session_id)
    assert inter_b is not None and inter_b.revoked_at_utc is None


def test_claim_interactive_session_for_update_fence_cas_concurrency(pg_dsn: str) -> None:
    """Concurrent claim_interactive_session calls serialize via FOR UPDATE + generation CAS so exactly one broker wins."""

    repo = PostgresExecutionControlRepository(pg_dsn)
    owner = team_owner_scope(TEAM)
    target = build_enrolled_ssh_target(owner, target_id="ssh_tgt_pg_claim_race")
    repo.put_ssh_target(target)
    pending = build_pending_enrolled_session(target, session_id="ssh_sess_pg_claim_race")
    repo.create_interactive_session(pending)

    contenders = 6
    barrier = threading.Barrier(contenders)

    def attempt(index: int) -> Any:
        worker_repo = PostgresExecutionControlRepository(pg_dsn)
        barrier.wait(10)
        try:
            return worker_repo.claim_interactive_session(
                team_id=TEAM,
                session_id=pending.session_id,
                broker_id=f"broker-{index}",
                lease_token=f"tok-{index}",
                lease_ttl_seconds=120,
            )
        except StaleExecutionFence as exc:
            return exc

    with ThreadPoolExecutor(max_workers=contenders) as pool:
        outcomes = list(pool.map(attempt, range(contenders)))

    winners = [item for item in outcomes if not isinstance(item, Exception)]
    fenced = [item for item in outcomes if isinstance(item, StaleExecutionFence)]
    assert len(winners) == 1 and len(fenced) == contenders - 1, outcomes
    assert winners[0].fence == 1

    # Stale fence update fails closed.
    with pytest.raises(StaleExecutionFence):
        repo.update_claimed_interactive_session(
            winners[0].model_copy(update={"status": InteractiveSessionStatus.OPEN}),
            broker_id=winners[0].lease_owner or "",
            lease_token=winners[0].lease_token or "",
            fence=0,
            release=False,
        )


def test_close_unleased_interactive_session_and_mark_interactive_sessions_lost(pg_dsn: str) -> None:
    """close_unleased_interactive_session and mark_interactive_sessions_lost enforce lease guards and clear bootstrap claims."""

    from storage.postgres_connection import connect_postgres

    repo = PostgresExecutionControlRepository(pg_dsn)
    owner = team_owner_scope(TEAM)
    target_1 = build_enrolled_ssh_target(owner, target_id="ssh_tgt_pg_lost_1", host="lost-1.internal")
    target_2 = build_enrolled_ssh_target(owner, target_id="ssh_tgt_pg_lost_2", host="lost-2.internal")
    repo.put_ssh_target(target_1)
    repo.put_ssh_target(target_2)

    # Unleased pending session closes cleanly and cannot be re-claimed.
    unleased = build_pending_enrolled_session(target_1, session_id="ssh_sess_pg_close_1")
    repo.create_interactive_session(unleased)
    closed = repo.close_unleased_interactive_session(team_id=TEAM, session_id=unleased.session_id)
    assert closed is not None and closed.status == InteractiveSessionStatus.CLOSED
    with pytest.raises(StaleExecutionFence):
        repo.claim_interactive_session(
            team_id=TEAM, session_id=unleased.session_id, broker_id="b-x", lease_token="t-x", lease_ttl_seconds=60
        )

    # Already-terminal session with transcript_artifact stays untouched on idempotent close.
    with_art = create_live_enrolled_session(repo, target_1, session_id="ssh_sess_pg_art", broker_id="b-art", lease_token="t-art")
    artifact = ArtifactReferenceV1(
        artifact_id="art_pg_transcript",
        kind=ArtifactKind.LOG,
        sha256="b" * 64,
        size_bytes=128,
        media_type="text/plain",
        storage_backend=ArtifactStorageBackend.FILESYSTEM,
        storage_key="logs/pg.log",
    )
    repo.update_claimed_interactive_session(
        with_art.model_copy(update={"status": InteractiveSessionStatus.CLOSED, "transcript_artifact": artifact}),
        broker_id="b-art",
        lease_token="t-art",
        fence=with_art.fence,
        release=True,
    )
    idempotent = repo.close_unleased_interactive_session(team_id=TEAM, session_id=with_art.session_id)
    assert idempotent is not None and idempotent.transcript_artifact == artifact

    # Actively leased session refuses close_unleased without clearing bootstrap claim; expired lease closes and clears.
    leased_exp = create_live_enrolled_session(repo, target_1, session_id="ssh_sess_pg_exp_close", broker_id="b-e", lease_token="t-e")
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target_1.target_id, session_id=leased_exp.session_id) is True
    with pytest.raises(StaleExecutionFence, match="leased"):
        repo.close_unleased_interactive_session(team_id=TEAM, session_id=leased_exp.session_id)

    expired_iso = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    with connect_postgres(pg_dsn) as conn:
        expired_copy = leased_exp.model_copy(update={"lease_expires_at_utc": datetime.fromisoformat(expired_iso)})
        conn.execute(
            "UPDATE execution_interactive_sessions SET lease_expires_at_utc = %s, record_json = %s WHERE session_id = %s",
            (expired_iso, expired_copy.model_dump_json(), leased_exp.session_id),
        )
        conn.commit()
    disc = repo.close_unleased_interactive_session(team_id=TEAM, session_id=leased_exp.session_id, disconnected=True)
    assert disc is not None and disc.status == InteractiveSessionStatus.DISCONNECTED

    # mark_interactive_sessions_lost recovers OPEN to LOST and releases leased PENDING while clearing bootstrap claims.
    open_sess = create_live_enrolled_session(repo, target_1, session_id="ssh_sess_pg_open_lost", broker_id="b-o", lease_token="t-o", open_session=True)
    pending_leased = create_live_enrolled_session(repo, target_2, session_id="ssh_sess_pg_pend_lost", broker_id="b-p", lease_token="t-p", open_session=False)
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target_1.target_id, session_id=open_sess.session_id) is True
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target_2.target_id, session_id=pending_leased.session_id) is True

    recovered = repo.mark_interactive_sessions_lost(team_id=TEAM, updated_at_utc=datetime.now(timezone.utc))
    assert [item.session_id for item in recovered] == [open_sess.session_id]
    reloaded_open = repo.get_interactive_session(team_id=TEAM, session_id=open_sess.session_id)
    assert reloaded_open is not None and reloaded_open.status == InteractiveSessionStatus.LOST and reloaded_open.lease_owner is None
    reloaded_pending = repo.get_interactive_session(team_id=TEAM, session_id=pending_leased.session_id)
    assert reloaded_pending is not None and reloaded_pending.status == InteractiveSessionStatus.PENDING and reloaded_pending.lease_owner is None

    # Both targets' bootstrap claims were cleared so fresh sessions can claim immediately.
    fresh_1 = create_live_enrolled_session(repo, target_1, session_id="ssh_sess_pg_fresh_1", broker_id="b-f1", lease_token="t-f1")
    fresh_2 = create_live_enrolled_session(repo, target_2, session_id="ssh_sess_pg_fresh_2", broker_id="b-f2", lease_token="t-f2")
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target_1.target_id, session_id=fresh_1.session_id) is True
    assert repo.claim_ssh_target_password_bootstrap(team_id=TEAM, target_id=target_2.target_id, session_id=fresh_2.session_id) is True


@pytest.mark.parametrize(
    "concurrent_op",
    ["revoke_interactive_session", "update_claimed_release", "mark_interactive_sessions_lost"],
)
def test_deterministic_paused_lock_overlap_has_zero_deadlocks(pg_dsn: str, concurrent_op: str) -> None:
    """C02: Pause revoke_ssh_target right after locking execution_ssh_targets while session mutations run concurrently."""

    from storage.postgres_connection import connect_postgres

    setup_repo = PostgresExecutionControlRepository(pg_dsn)
    owner = team_owner_scope(TEAM)
    target = build_enrolled_ssh_target(owner, target_id=f"ssh_tgt_pg_pause_{concurrent_op}")
    setup_repo.put_ssh_target(target)
    session = create_live_enrolled_session(
        setup_repo,
        target,
        session_id=f"ssh_sess_pg_pause_{concurrent_op}",
        broker_id="broker-pause",
        lease_token="tok-pause",
    )
    setup_repo.put_ssh_interaction(build_ssh_interaction_activation(session))
    assert setup_repo.claim_ssh_target_password_bootstrap(
        team_id=TEAM, target_id=target.target_id, session_id=session.session_id
    ) is True

    revoker = PostgresExecutionControlRepository(pg_dsn)
    racer = PostgresExecutionControlRepository(pg_dsn)
    locked_target = threading.Event()
    original_connect = revoker._connect
    setattr(
        revoker,
        "_connect",
        lambda: _PausedAfterStatement(original_connect(), "FROM execution_ssh_targets", locked_target),
    )

    errors: list[BaseException] = []

    def run_revoker() -> None:
        try:
            revoker.revoke_ssh_target(team_id=TEAM, target_id=target.target_id, revoked_by="admin_pause")
        except BaseException as exc:  # noqa: BLE001 - inspected explicitly below
            errors.append(exc)

    def run_racer() -> None:
        try:
            if concurrent_op == "revoke_interactive_session":
                racer.revoke_interactive_session(team_id=TEAM, session_id=session.session_id, revoked_by="admin_sess")
            elif concurrent_op == "update_claimed_release":
                racer.update_claimed_interactive_session(
                    session.model_copy(update={"status": InteractiveSessionStatus.CLOSED}),
                    broker_id="broker-pause",
                    lease_token="tok-pause",
                    fence=session.fence,
                    release=True,
                )
            else:
                racer.mark_interactive_sessions_lost(team_id=TEAM, updated_at_utc=datetime.now(timezone.utc))
        except BaseException as exc:  # noqa: BLE001 - inspected explicitly below
            errors.append(exc)

    thread = threading.Thread(target=run_revoker)
    thread.start()
    assert locked_target.wait(10), "revoke_ssh_target never reached its execution_ssh_targets FOR UPDATE lock"
    run_racer()
    thread.join(timeout=20)
    assert not thread.is_alive(), "revoke_ssh_target hung during lock overlap"

    _assert_no_deadlock_errors(errors)
    assert errors == [], f"Unexpected errors during paused lock overlap: {errors!r}"

    stored_target = setup_repo.get_ssh_target(team_id=TEAM, target_id=target.target_id)
    assert stored_target is not None and stored_target.status == SSHTargetStatus.REVOKED
    assert setup_repo.get_interactive_session_revocation(team_id=TEAM, session_id=session.session_id) is not None
    stored_inter = setup_repo.get_ssh_interaction(team_id=TEAM, session_id=session.session_id)
    assert stored_inter is not None and stored_inter.revoked_at_utc is not None
    with connect_postgres(pg_dsn) as conn:
        claim_row = conn.execute(
            "SELECT password_bootstrap_claimed_by_session_id FROM execution_ssh_targets WHERE target_id = %s",
            (target.target_id,),
        ).fetchone()
    assert claim_row is not None and claim_row["password_bootstrap_claimed_by_session_id"] is None


def test_multithreaded_target_revocation_vs_session_mutations_no_deadlocks(pg_dsn: str) -> None:
    """A3 / C02: Stress-test concurrent revoke_ssh_target vs session revoke/release/close/lost/bootstrap with zero deadlocks."""

    from storage.postgres_connection import connect_postgres

    setup_repo = PostgresExecutionControlRepository(pg_dsn)
    owner = team_owner_scope(TEAM)
    rounds = 15

    for round_idx in range(rounds):
        target = build_enrolled_ssh_target(owner, target_id=f"ssh_tgt_stress_{round_idx}")
        setup_repo.put_ssh_target(target)
        s1 = create_live_enrolled_session(setup_repo, target, session_id=f"ssh_sess_st_{round_idx}_1", broker_id="b1", lease_token="t1")
        s2 = create_live_enrolled_session(setup_repo, target, session_id=f"ssh_sess_st_{round_idx}_2", broker_id="b2", lease_token="t2")
        s3 = build_pending_enrolled_session(target, session_id=f"ssh_sess_st_{round_idx}_3")
        setup_repo.create_interactive_session(s3)
        setup_repo.put_ssh_interaction(build_ssh_interaction_activation(s1))
        setup_repo.put_ssh_interaction(build_ssh_interaction_activation(s2))
        assert setup_repo.claim_ssh_target_password_bootstrap(
            team_id=TEAM, target_id=target.target_id, session_id=s1.session_id
        ) is True

        def do_revoke_target(repo: PostgresExecutionControlRepository) -> Any:
            return repo.revoke_ssh_target(
                team_id=TEAM, target_id=target.target_id, revoked_by="admin_stress_target"
            )

        def do_revoke_session(repo: PostgresExecutionControlRepository) -> Any:
            return repo.revoke_interactive_session(
                team_id=TEAM, session_id=s1.session_id, revoked_by="admin_stress_session"
            )

        def do_release_session(repo: PostgresExecutionControlRepository) -> Any:
            return repo.update_claimed_interactive_session(
                s2.model_copy(update={"status": InteractiveSessionStatus.CLOSED}),
                broker_id="b2",
                lease_token="t2",
                fence=s2.fence,
                release=True,
            )

        def do_close_unleased(repo: PostgresExecutionControlRepository) -> Any:
            return repo.close_unleased_interactive_session(
                team_id=TEAM, session_id=s3.session_id
            )

        def do_mark_lost(repo: PostgresExecutionControlRepository) -> Any:
            return repo.mark_interactive_sessions_lost(
                team_id=TEAM, updated_at_utc=datetime.now(timezone.utc)
            )

        def do_claim_bootstrap(repo: PostgresExecutionControlRepository) -> Any:
            return repo.claim_ssh_target_password_bootstrap(
                team_id=TEAM, target_id=target.target_id, session_id=s2.session_id
            )

        workers: list[Callable[[PostgresExecutionControlRepository], Any]] = [
            do_revoke_target,
            do_revoke_session,
            do_release_session,
            do_close_unleased,
            do_mark_lost,
            do_claim_bootstrap,
        ]
        barrier = threading.Barrier(len(workers))
        unexpected: list[BaseException] = []

        def run_worker(fn: Callable[[PostgresExecutionControlRepository], Any]) -> None:
            worker_repo = PostgresExecutionControlRepository(pg_dsn)
            barrier.wait(10)
            try:
                fn(worker_repo)
            except StaleExecutionFence:
                # Expected when mark_interactive_sessions_lost clears s2's lease before release=True runs.
                return
            except BaseException as exc:  # noqa: BLE001 - asserted below
                unexpected.append(exc)

        with ThreadPoolExecutor(max_workers=len(workers)) as pool:
            list(pool.map(run_worker, workers))

        _assert_no_deadlock_errors(unexpected)
        assert unexpected == [], f"Round {round_idx} raised unexpected errors: {unexpected!r}"

        stored_target = setup_repo.get_ssh_target(team_id=TEAM, target_id=target.target_id)
        assert stored_target is not None and stored_target.status == SSHTargetStatus.REVOKED
        for sess_id in (s1.session_id, s2.session_id, s3.session_id):
            assert setup_repo.get_interactive_session_revocation(team_id=TEAM, session_id=sess_id) is not None
        for sess_id in (s1.session_id, s2.session_id):
            inter = setup_repo.get_ssh_interaction(team_id=TEAM, session_id=sess_id)
            assert inter is not None and inter.revoked_at_utc is not None
        with connect_postgres(pg_dsn) as conn:
            row = conn.execute(
                "SELECT password_bootstrap_claimed_by_session_id FROM execution_ssh_targets WHERE target_id = %s",
                (target.target_id,),
            ).fetchone()
        assert row is not None and row["password_bootstrap_claimed_by_session_id"] is None

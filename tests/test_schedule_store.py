"""
Unit tests for ScheduleStore — the SQLite-backed persistent scheduler.

These tests verify add/remove, ordering, update-after-fire logic,
enable/disable toggling, and DB persistence across reconnections.
"""

import os
import sqlite3
import sys

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone

from custom_agents.schedule_store import AdvanceOutcome, ScheduleStore, compute_next_fire_at
from custom_messages.contracts.routing_v1 import GatewayContext, MessageHeader, MessageMeta
from custom_messages.contracts.schedule_v1 import ScheduledMessage as ScheduledMessageModel, ScheduleTaskMemory
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, RequesterType, ScheduleManagementClass


def ScheduledMessage(**kwargs):
    """Build ordinary user-managed schedules for generic store tests."""

    kwargs.setdefault("management_class", ScheduleManagementClass.USER)
    return ScheduledMessageModel(**kwargs)


@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary DB path for each test."""
    return str(tmp_path / "test_schedules.db")


@pytest.fixture
def store(db_path):
    """Create a fresh ScheduleStore for each test."""
    return ScheduleStore(db_path=db_path)


class TestComputeNextFireAt:
    """Tests for the compute_next_fire_at utility function."""

    def test_neither_raises(self):
        with pytest.raises(ValueError, match="Either interval_ms or cron_expression"):
            compute_next_fire_at()


class TestScheduleStoreBasics:
    """Basic add/remove/query operations."""

    def test_add_surfaces_persistence_failure(self, store, monkeypatch):
        msg = ScheduledMessage(
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Test"),
            interval_ms=1000,
        )

        def fail_add(*_args, **_kwargs):
            raise sqlite3.OperationalError("simulated persistence failure")

        monkeypatch.setattr(store._repository, "write_locked", fail_add)
        with pytest.raises(sqlite3.OperationalError, match="simulated persistence failure"):
            store.add(msg)

    def test_remove_nonexistent(self, store):
        assert store.remove("nonexistent_id") is False

    def test_add_if_absent_preserves_existing_runtime_state(self, store):
        original = ScheduledMessage(
            schedule_id="sched_ensure_once",
            schedule_name="original",
            install_mode="ensure",
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Original"),
            interval_ms=60_000,
            repeat_count=None,
            next_fire_at="2026-07-16T12:00:00+00:00",
        )
        assert store.add_if_absent(original) is True
        paused_until = "2026-07-20T12:00:00+00:00"
        assert store.set_enabled(original.schedule_id, False)
        assert store.set_paused_until(original.schedule_id, paused_until)

        replacement = ScheduledMessage(
            schedule_id=original.schedule_id,
            schedule_name="replacement",
            install_mode="ensure",
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Replacement"),
            interval_ms=1_000,
            repeat_count=None,
            next_fire_at="2026-07-15T12:00:00+00:00",
        )
        assert store.add_if_absent(replacement) is False

        restored = store.get_scheduled_message(original.schedule_id)
        assert restored is not None
        assert restored.schedule_name == "original"
        assert restored.payload.problem_statement == "Original"
        assert restored.interval_ms == 60_000
        assert restored.next_fire_at == "2026-07-16T12:00:00+00:00"
        assert restored.enabled is False
        assert restored.paused_until == paused_until

    def test_roundtrip_preserves_payload_header_and_meta(self, store):
        msg = ScheduledMessage(
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Test"),
            payload_header=MessageHeader(
                from_agent_id="gateway_queue",
                to_routing_path="manager_id",
                response_routing_key="gateway_queue",
                agent_signature="",
            ),
            payload_meta=MessageMeta(
                from_agent_name="test_gateway",
                msg_correlation_id="corr_123",
                requester_type=RequesterType.TIMER_REQUESTER,
                gateway_context=GatewayContext(
                    gateway_instance_name="testbot",
                    session_key="session_1",
                    delivery_mode="cron",
                    scheduled_job_id="cron_123",
                    canonical_user_id="usr_owner",
                ),
            ),
            interval_ms=1000,
        )
        store.add(msg)

        restored = store.get_scheduled_message(msg.schedule_id)
        assert restored is not None
        assert restored.payload_header is not None
        assert restored.payload_header.response_routing_key == "gateway_queue"
        assert restored.payload_meta is not None
        assert restored.payload_meta.msg_correlation_id == "corr_123"
        assert restored.payload_meta.gateway_context is not None
        assert restored.payload_meta.gateway_context.session_key == "session_1"
        assert restored.payload_meta.gateway_context.scheduled_job_id == "cron_123"

    @pytest.mark.parametrize("embedded_owner", [None, "usr_wrong"])
    @pytest.mark.parametrize("row_owner", [None, "usr_authoritative"])
    def test_row_owner_is_authoritative_for_executable_payload_context(
        self,
        db_path,
        embedded_owner,
        row_owner,
        caplog,
    ):
        store = ScheduleStore(db_path=db_path)
        msg = ScheduledMessage(
            schedule_id=f"sched_owner_{embedded_owner or 'missing'}",
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Test"),
            payload_meta=MessageMeta(
                gateway_context=GatewayContext(
                    session_key="session_owner_repair",
                    canonical_user_id=embedded_owner,
                )
            ),
            interval_ms=1000,
        )
        store.add(msg)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE scheduled_messages SET owner_canonical_user_id = ? WHERE schedule_id = ?",
                (row_owner, msg.schedule_id),
            )
            conn.commit()

        with caplog.at_level("WARNING"):
            restored = store.get_scheduled_message(msg.schedule_id)

        assert restored is not None
        assert restored.payload_meta is not None
        assert restored.payload_meta.gateway_context is not None
        assert restored.payload_meta.gateway_context.canonical_user_id == row_owner
        assert restored.payload_meta.gateway_context.session_key == (
            "session_owner_repair" if row_owner is not None else None
        )
        assert "event=schedule_owner_context_reconciled" in caplog.text

    def test_roundtrip_overlays_row_owned_fire_state_before_rewrite(self, store):
        original_next_fire_at = "2026-06-20T12:00:00+00:00"
        msg = ScheduledMessage(
            schedule_id="sched_overlay_fire_state",
            payload=Task(task_type=MessageMetaType.REQUEST, problem_statement="Test"),
            interval_ms=60_000,
            repeat_count=None,
            next_fire_at=original_next_fire_at,
            schedule_memory=ScheduleTaskMemory(),
        )
        store.add(msg)
        assert store.update_after_fire(msg.schedule_id) == AdvanceOutcome.ADVANCED
        updated_next_fire_at = next(
            row["next_fire_at"]
            for row in store.get_all()
            if row["schedule_id"] == msg.schedule_id
        )
        assert updated_next_fire_at != original_next_fire_at

        restored = store.get_scheduled_message(msg.schedule_id)
        assert restored is not None
        assert restored.next_fire_at == updated_next_fire_at

        restored.schedule_memory.ledger.total_runs = 1
        store.add(restored, remaining_count_override=None)

        row = store.get_scheduled_message(msg.schedule_id)
        assert row is not None
        assert row.next_fire_at == updated_next_fire_at
        assert row.schedule_memory is not None
        assert row.schedule_memory.ledger.total_runs == 1


class TestScheduleStoreFireLogic:
    """Tests for update_after_fire — decrement, exhaust, and reschedule."""

    def test_multi_fire_decrements(self, store):
        msg = ScheduledMessage(
            payload=Task(task_type=MessageMetaType.REQUEST),
            interval_ms=1000,
            repeat_count=3,
        )
        store.add(msg)

        # Fire 1: 3 → 2
        assert store.update_after_fire(msg.schedule_id) == AdvanceOutcome.ADVANCED
        assert store.count_enabled() == 1  # still alive

        # Fire 2: 2 → 1
        assert store.update_after_fire(msg.schedule_id) == AdvanceOutcome.ADVANCED

        # Fire 3: 1 → 0 → removed
        assert store.update_after_fire(msg.schedule_id) == AdvanceOutcome.FINISHED
        assert store.count_enabled() == 0

    def test_claim_next_due_scopes_to_named_agent_type_and_releases(self, store):
        now = datetime.now(timezone.utc)
        msg = ScheduledMessage(
            payload=Task(task_type=MessageMetaType.REQUEST),
            interval_ms=1000,
            repeat_count=2,
        )
        msg.next_fire_at = (now - timedelta(seconds=1)).isoformat()
        store.add(
            msg,
            claim_team_id="test_team",
            claim_named_agent_type="TeamArchitect",
        )

        wrong_type = store.claim_next_due(
            claimer_agent_id="worker_1",
            claimer_named_agent_type="WorkerAgent",
            claimer_team_id="test_team",
        )
        assert wrong_type is None

        claimed = store.claim_next_due(
            claimer_agent_id="manager_1",
            claimer_named_agent_type="TeamArchitect",
            claimer_team_id="test_team",
        )
        assert claimed is not None
        assert claimed["schedule_id"] == msg.schedule_id
        assert claimed["claimed_by_agent_id"] == "manager_1"
        claim_token = claimed["claim_token"]

        duplicate = store.claim_next_due(
            claimer_agent_id="manager_2",
            claimer_named_agent_type="TeamArchitect",
            claimer_team_id="test_team",
        )
        assert duplicate is None

        assert store.release_claim(
            msg.schedule_id,
            claim_token=claim_token,
            claimer_agent_id="manager_1",
        ) is True

        reclaimed = store.claim_next_due(
            claimer_agent_id="manager_2",
            claimer_named_agent_type="TeamArchitect",
            claimer_team_id="test_team",
        )
        assert reclaimed is not None
        assert reclaimed["claimed_by_agent_id"] == "manager_2"

    def test_update_after_fire_with_claim_clears_claim_on_recurring_schedule(self, store):
        now = datetime.now(timezone.utc)
        msg = ScheduledMessage(
            payload=Task(task_type=MessageMetaType.REQUEST),
            interval_ms=1000,
            repeat_count=2,
        )
        msg.next_fire_at = (now - timedelta(seconds=1)).isoformat()
        store.add(
            msg,
            claim_team_id="test_team",
            claim_named_agent_type="TeamArchitect",
        )
        claimed = store.claim_next_due(
            claimer_agent_id="manager_1",
            claimer_named_agent_type="TeamArchitect",
            claimer_team_id="test_team",
        )
        assert claimed is not None

        assert store.update_after_fire(
            msg.schedule_id,
            claim_token=claimed["claim_token"],
            claimer_agent_id="manager_1",
            due_at=claimed["next_fire_at"],
        ) == AdvanceOutcome.ADVANCED
        rows = store.get_all()
        assert rows[0]["claimed_by_agent_id"] is None
        assert rows[0]["claim_token"] is None

    def test_roundtrip_preserves_claim_scope(self, store):
        msg = ScheduledMessage(
            payload=Task(task_type=MessageMetaType.REQUEST),
            interval_ms=1000,
        )
        store.add(
            msg,
            claim_team_id="test_team",
            claim_named_agent_type="TeamArchitect",
        )

        restored = store.get_scheduled_message(msg.schedule_id)
        assert restored is not None
        assert restored.claim_team_id == "test_team"
        assert restored.claim_named_agent_type == "TeamArchitect"

    def test_expired_schedule_pruned_before_claim(self, store):
        now = datetime.now(timezone.utc)
        msg = ScheduledMessage(
            payload=Task(task_type=MessageMetaType.REQUEST),
            interval_ms=1000,
            repeat_count=None,
            next_fire_at=(now - timedelta(seconds=10)).isoformat(),
            expires_at_utc=(now - timedelta(seconds=1)).isoformat(),
        )
        store.add(
            msg,
            claim_team_id="test_team",
            claim_named_agent_type="TeamArchitect",
        )
        assert store.get_scheduled_message(msg.schedule_id) is not None

        claimed = store.claim_next_due(
            claimer_agent_id="manager_1",
            claimer_named_agent_type="TeamArchitect",
            claimer_team_id="test_team",
        )

        assert claimed is None
        assert store.get_scheduled_message(msg.schedule_id) is None

    def test_update_after_fire_removes_when_next_fire_reaches_expiration(self, store):
        now = datetime.now(timezone.utc)
        msg = ScheduledMessage(
            payload=Task(task_type=MessageMetaType.REQUEST),
            interval_ms=60_000,
            repeat_count=None,
            next_fire_at=(now - timedelta(seconds=1)).isoformat(),
            expires_at_utc=(now + timedelta(seconds=30)).isoformat(),
        )
        store.add(msg)

        assert store.update_after_fire(msg.schedule_id) == AdvanceOutcome.FINISHED
        assert store.get_scheduled_message(msg.schedule_id) is None


class TestScheduleStoreEnableDisable:
    """Tests for enable/disable toggling."""

    def test_paused_schedule_not_due_until_pause_expires(self, store):
        now = datetime.now(timezone.utc)
        msg = ScheduledMessage(
            schedule_name="paused schedule",
            payload=Task(task_type=MessageMetaType.REQUEST),
            interval_ms=1000,
        )
        msg.next_fire_at = (now - timedelta(seconds=10)).isoformat()
        msg.paused_until = (now + timedelta(minutes=5)).isoformat()
        store.add(msg)

        assert store.get_next_due() is None
        assert store.get_next_wake_time() == msg.paused_until

        assert store.set_paused_until(msg.schedule_id, None) is True
        due = store.get_next_due()
        assert due is not None
        assert due["schedule_id"] == msg.schedule_id

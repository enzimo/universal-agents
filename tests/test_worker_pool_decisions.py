"""Worker pool controller decisions, pool sizes and the configuration upgrade (pure logic)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_agents.delegation_wake import counts_text
from custom_agents.worker_pool_controller import (
    PoolObservation,
    capacity_threshold,
    decide,
    pool_launch_owner,
)
from custom_agents.worker_pool_limits import PoolLimit, resolve_limit
from runtime_settings.worker_pools import WorkerPoolsConfig, move_worker_capacity_settings
from storage.worker_pool_repository import WorkerPoolLimitV1

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _limit(effective: int = 4) -> PoolLimit:
    return PoolLimit(scope="WebResearchAgent", effective=effective, default=4, ceiling=16)


def _observation(**overrides) -> PoolObservation:
    values = dict(
        agent_type="WebResearchAgent",
        waiting=0,
        asking=0,
        unacknowledged=0,
        oldest_wait_seconds=None,
        running=0,
        starting=0,
        unknown=0,
        draining=0,
        cooling_down=0,
        limit=_limit(),
        observed_at=NOW,
    )
    values.update(overrides)
    return PoolObservation(**values)


def _decide(observation: PoolObservation, *, team_active: int = 0, team_limit: int = 32, **settings):
    return decide(
        observation,
        team_active=team_active,
        team_limit_value=team_limit,
        settings=WorkerPoolsConfig(**settings),
    )


def test_no_waiting_tasks_starts_nothing():
    decision = _decide(_observation(running=2))
    assert decision.starts == 0
    assert not decision.at_capacity


def test_a_type_with_tasks_and_no_worker_starts_one_at_once():
    decision = _decide(_observation(waiting=5, oldest_wait_seconds=0.5))
    assert decision.starts == 1


def test_tasks_waiting_with_no_free_worker_scale_up_after_the_wait():
    early = _decide(_observation(waiting=6, running=1, oldest_wait_seconds=5))
    assert early.starts == 0
    late = _decide(_observation(waiting=6, running=1, oldest_wait_seconds=25))
    # Room for three more under a pool size of 4.
    assert late.starts == 3


def test_an_idle_worker_asking_means_no_new_start():
    decision = _decide(_observation(waiting=3, running=2, asking=1, oldest_wait_seconds=60))
    assert decision.starts == 0


def test_starting_workers_count_against_waiting_tasks():
    decision = _decide(_observation(waiting=2, running=1, starting=2, oldest_wait_seconds=60))
    assert decision.starts == 0


def test_starts_respect_the_team_limit_and_per_pass_maximum():
    decision = _decide(
        _observation(waiting=40, running=1, oldest_wait_seconds=60, limit=_limit(16)),
        team_active=30,
        team_limit=32,
    )
    assert decision.starts == 2
    decision = _decide(
        _observation(waiting=40, running=1, oldest_wait_seconds=60, limit=_limit(16)),
        max_starts_per_pass=3,
    )
    assert decision.starts == 3


def test_a_pool_at_its_size_reports_capacity_while_tasks_wait():
    decision = _decide(_observation(waiting=8, running=4, oldest_wait_seconds=60))
    assert decision.starts == 0
    assert decision.at_capacity
    assert decision.reason == "the pool is at its size"


def test_capacity_threshold_is_95_percent_rounded_up():
    assert capacity_threshold(4, 0.95) == 4
    assert capacity_threshold(32, 0.95) == 31
    assert capacity_threshold(0, 0.95) == 0


def test_a_provider_limit_blocks_new_workers():
    decision = _decide(_observation(waiting=8, running=2, cooling_down=1, oldest_wait_seconds=60))
    assert decision.starts == 0
    assert decision.provider_blocked


def test_turned_off_pools_start_nothing():
    decision = _decide(_observation(waiting=3, oldest_wait_seconds=60), enabled=False)
    assert decision.starts == 0


def test_a_lowered_size_reports_surplus_workers_not_yet_draining():
    decision = _decide(_observation(running=6, draining=1, limit=_limit(2)))
    assert decision.surplus == 3


def test_pool_starts_are_recorded_for_the_team_pool():
    assert pool_launch_owner("team_a") == "worker_pool:team_a"


def _record(**overrides) -> WorkerPoolLimitV1:
    values = dict(team_id="t", scope="WebResearchAgent", updated_at_utc=NOW)
    values.update(overrides)
    return WorkerPoolLimitV1(**values)


def test_pool_size_comes_from_temporary_then_lasting_then_default():
    assert resolve_limit(None, scope="X", default=4, ceiling=16, now=NOW).effective == 4
    lasting = _record(lasting_max_workers=6)
    assert resolve_limit(lasting, scope="X", default=4, ceiling=16, now=NOW).source == "lasting"
    temporary = _record(
        lasting_max_workers=6,
        temporary_max_workers=10,
        temporary_until_utc=NOW + timedelta(minutes=30),
    )
    limit = resolve_limit(temporary, scope="X", default=4, ceiling=16, now=NOW)
    assert (limit.effective, limit.source) == (10, "temporary")
    expired = resolve_limit(
        temporary, scope="X", default=4, ceiling=16, now=NOW + timedelta(hours=1)
    )
    assert (expired.effective, expired.source) == (6, "lasting")


def test_a_size_above_a_lowered_ceiling_is_cut_to_the_ceiling(caplog):
    limit = resolve_limit(_record(lasting_max_workers=12), scope="X", default=4, ceiling=8, now=NOW)
    assert limit.effective == 8
    assert "worker_pool_limit_above_ceiling" in caplog.text


def test_settings_refuse_a_default_above_the_ceiling():
    with pytest.raises(ValueError):
        WorkerPoolsConfig(default_max_workers_per_type=20, max_workers_per_type_ceiling=16)
    with pytest.raises(ValueError):
        WorkerPoolsConfig(max_workers_per_type_ceiling=40, max_total_workers_per_team=32)


def test_upgrade_moves_customized_capacity_and_drops_obsolete_settings():
    document = {
        "agent": {
            "schedule_workers": {
                "enabled": True,
                "max_auto_workers_per_type": 1,
                "max_auto_workers_per_team": 4,
                "max_launches_per_type_per_hour": 12,
                "max_total_workers_per_team": 48,
            },
            "delegation_assurance": {"enabled": True},
        },
        "agent_types": {"WebResearchAgent": {"agent": {"delegation_assurance": {}}}},
    }
    notes = move_worker_capacity_settings(document)
    assert document["agent"]["schedule_workers"] == {"enabled": True}
    assert document["agent"]["worker_pools"] == {"max_total_workers_per_team": 48}
    assert "delegation_assurance" not in document["agent"]
    assert document["agent_types"]["WebResearchAgent"]["agent"] == {}
    assert any("moved to agent.worker_pools.max_total_workers_per_team" in note for note in notes)
    # Validates as current settings.
    WorkerPoolsConfig.model_validate(document["agent"]["worker_pools"])


def test_upgrade_keeps_a_small_team_limit_valid():
    document = {"agent": {"schedule_workers": {"max_total_workers_per_team": 8}}}
    move_worker_capacity_settings(document)
    pools = WorkerPoolsConfig.model_validate(document["agent"]["worker_pools"])
    assert pools.max_workers_per_type_ceiling == 8
    assert pools.default_max_workers_per_type <= 8


def test_upgrade_of_a_current_document_changes_nothing():
    document = {"agent": {"worker_pools": {"default_max_workers_per_type": 6}}}
    assert move_worker_capacity_settings(document) == []
    assert document == {"agent": {"worker_pools": {"default_max_workers_per_type": 6}}}


def test_progress_counts_read_plainly():
    assert (
        counts_text({"queued": 11, "running": 4, "finished": 9, "failed": 0, "cancelled": 0})
        == "9 of 24 done, 4 running, 11 waiting for a free worker"
    )
    assert counts_text({"finished": 2, "failed": 1}) == "2 of 3 done, 1 failed"

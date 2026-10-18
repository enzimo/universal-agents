"""Recovery records and cleanup ownership on real SQLite and PostgreSQL stores."""

from datetime import timedelta

import pytest

from custom_messages.contracts.access_v2 import AccessRequestStatus, AccessRequestV1
from storage.access_repository import AccessRepository, DecisionClaim, DecisionClaimLost, DecisionInProgress, DecisionRecovery
from storage.config import resolve_storage_settings
from tests.test_access_store_postgres import NOW, OWNER, TEAM, _race, _request, postgres_storage


@pytest.fixture(params=[
    "sqlite",
    pytest.param("postgres", marks=[pytest.mark.postgres, pytest.mark.live, pytest.mark.integration]),
])
def repositories(request, tmp_path):
    if request.param == "postgres":
        with postgres_storage() as settings:
            yield AccessRepository(settings), AccessRepository(settings)
    else:
        settings = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "access.db")}})
        yield AccessRepository(settings), AccessRepository(settings)


def prepare(store):
    request = store.create_request(_request())
    claim = DecisionClaim(request.request_id, "decision_original")
    store.claim_decision(request.request_id, claim_id=claim.claim_id, at=NOW, until=NOW + timedelta(seconds=1))
    finished = request.model_copy(update={
        "status": AccessRequestStatus.GRANTED, "decided_by": OWNER, "decided_at_utc": NOW,
    })
    recovery = DecisionRecovery(claim, finished, {"tool_files": []}, NOW + timedelta(seconds=1))
    store.save_decision_recovery(recovery)
    return claim, recovery


def test_recovery_survives_reopening_and_blocks_takeover_until_reconciled(repositories):
    first, _second = repositories
    claim, recovery = prepare(first)
    reopened = AccessRepository(first.settings)
    later = NOW + timedelta(minutes=11)
    assert reopened.decision_recoveries(TEAM, now=later) == [recovery]
    with pytest.raises(DecisionInProgress):
        reopened.claim_decision(claim.request_id, claim_id="decision_new", at=later, until=later + timedelta(minutes=10))
    with reopened.decision_cleanup_transaction(claim, require_record=True, now=later) as state:
        assert state is not None and state[0].status is AccessRequestStatus.WAITING and not state[1]
    assert reopened.decision_recoveries(TEAM, now=later) == []
    reopened.claim_decision(claim.request_id, claim_id="decision_new", at=later, until=later + timedelta(minutes=10))
    # Another maintenance pass can still hold the old list; it must not touch
    # a file or a claim belonging to the new decision.
    with reopened.decision_cleanup_transaction(claim, require_record=True, now=later) as stale:
        assert stale is None
    reopened.renew_decision(DecisionClaim(claim.request_id, "decision_new"), until=later + timedelta(minutes=20))


def test_a_takeover_waits_for_cleanup_and_then_can_claim(repositories):
    first, second = repositories
    claim, _recovery = prepare(first)
    later = NOW + timedelta(minutes=11)

    def cleanup():
        with first.decision_cleanup_transaction(claim, require_record=True, now=later) as state:
            assert state is not None and not state[1]
        return True

    outcomes = _race(
        first, "SELECT claim_id, claimed_until FROM access_request_claims", cleanup,
        lambda: second.claim_decision(
            claim.request_id, claim_id="decision_new", at=later, until=later + timedelta(minutes=10)
        ),
    )
    assert outcomes[0] is True and isinstance(outcomes[1], AccessRequestV1), outcomes


def test_failed_reconciliation_keeps_its_recovery_record(repositories):
    first, second = repositories
    claim, recovery = prepare(first)
    with pytest.raises(RuntimeError, match="retry cleanup"):
        with first.decision_cleanup_transaction(claim, require_record=True, now=NOW + timedelta(seconds=2)):
            raise RuntimeError("retry cleanup")
    assert second.decision_recoveries(TEAM, now=NOW + timedelta(seconds=2)) == [recovery]


def test_renewal_keeps_a_stale_recovery_pass_from_cleaning_live_work(repositories):
    first, second = repositories
    claim, recovery = prepare(first)
    scanned_at = NOW + timedelta(seconds=2)
    assert second.decision_recoveries(TEAM, now=scanned_at) == [recovery]
    until = NOW + timedelta(hours=1)
    first.renew_decision(claim, until=until)
    # A later artifact reservation must not shorten the renewed deadline.
    first.save_decision_recovery(recovery)
    assert second.decision_recoveries(TEAM, now=scanned_at) == []
    with second.decision_cleanup_transaction(claim, require_record=True, now=scanned_at) as stale:
        assert stale is None
    first.renew_decision(claim, until=until + timedelta(hours=1))


def test_cleanup_reads_effects_added_after_the_maintenance_scan(repositories):
    from dataclasses import replace

    first, second = repositories
    claim, recovery = prepare(first)
    later = NOW + timedelta(seconds=2)
    [scanned] = second.decision_recoveries(TEAM, now=later)
    latest = replace(recovery, effects={"tool_files": ["reserved_after_scan"]})
    first.save_decision_recovery(latest)
    with second.decision_cleanup_transaction(claim, require_record=True, now=later) as state:
        assert state is not None and state[2] == latest
        assert state[2].effects != scanned.effects
    assert first.decision_recoveries(TEAM) == []


def test_external_publication_and_cleanup_exclude_each_other(repositories):
    import threading

    first, second = repositories
    claim, _recovery = prepare(first)
    published = threading.Event()

    def publish():
        with first.decision_effect_transaction(claim):
            published.set()
        return True

    def cleanup():
        with second.decision_cleanup_transaction(claim, require_record=True, now=NOW + timedelta(seconds=2)) as state:
            assert state is not None and published.is_set(), "cleanup waits until publication ends"
        return True

    assert _race(first, "SELECT claim_id FROM access_request_claims", publish, cleanup) == [True, True]
    # Once cleanup wins, the old claimant cannot publish another effect.
    with pytest.raises(DecisionClaimLost):
        with first.decision_effect_transaction(claim):
            pytest.fail("a released decision must not publish")

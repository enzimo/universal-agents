"""Agent command admission survives restarts and concurrent requests on both databases."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from storage.operation_outcomes import CommandOutcome, OutcomeKeyTaken, is_receipt
from storage.relayed_command_outcomes import RelayedCommandOutcomes
from tests.test_pinned_memory_stale_edits import BACKENDS, _storage


@pytest.fixture(params=BACKENDS)
def settings(request, tmp_path):
    with _storage(request.param, tmp_path) as storage:
        yield storage


def test_only_one_contender_admits_a_command_and_its_reply_survives_reopening(settings):
    outcome = CommandOutcome(team_id="team", person_id="alice", retry_key="command-1", kind="debug.dump",
                             body_digest="digest-1", now=datetime.now(timezone.utc), reply_for=lambda reply: reply)
    stores = [RelayedCommandOutcomes(settings), RelayedCommandOutcomes(settings)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda store: store.admit(outcome), stores))
    assert sum(reply is None for reply in replies) == 1
    assert all(is_receipt(reply) for reply in replies if reply is not None)
    final = {"outcome": "ok", "enabled": True, "mode": "always"}
    assert stores[0].complete(outcome, final) == final
    reopened = RelayedCommandOutcomes(settings)
    assert reopened.admit(outcome) == final
    assert reopened.admit(replace(outcome, body_digest="other-body")) == final
    with pytest.raises(OutcomeKeyTaken):
        reopened.admit(replace(outcome, person_id="bob"))


def test_admission_without_a_final_answer_stays_explicitly_unknown_after_restart(settings):
    outcome = CommandOutcome(team_id="team", person_id="alice", retry_key="command-2", kind="tasks.remove",
                             body_digest="digest-2", now=datetime.now(timezone.utc), reply_for=lambda reply: reply)
    assert RelayedCommandOutcomes(settings).admit(outcome) is None
    assert is_receipt(RelayedCommandOutcomes(settings).admit(outcome))

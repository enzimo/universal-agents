"""The storage canary exercises current tools without an external service."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_control.authority_verifier import (
    AuthorityTokenVerifier,
    IdentityRejected,
    set_process_verifier,
)
from custom_agents.access_runtime import runtime_access
from custom_agents.task_context_runtime import context_verifier
from custom_transport.protocol1.authority_tokens import root_task_token
from storage.config import resolve_storage_settings
from tests import live_objective_storage_validation as validation
from tests import message_contract_support as fixtures
from tests import test_authority_tokens_and_verifier as tv
from tests.broker_access_support import forget_task_context_keys, signed_task_token


def test_objective_storage_canary_completes_and_restores_identity_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("LIVE_TEST_POSTGRES_DSN", raising=False)
    monkeypatch.setattr(validation, "RESULT_DIR", tmp_path)
    caller_verifier = AuthorityTokenVerifier(
        fixtures.trust_store(),
        people=tv._default_people(),
        clock=lambda: fixtures.NOW,
    )
    caller_context = root_task_token(
        tv._identity_for("gateway-1", "test-gateway-key-1"),
        team=tv.TEAM,
        root_task_id="task_caller",
        request_id="task_context_caller",
        person_ref=tv.ALICE_REF,
        issued=fixtures.NOW,
    )
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}},
        base_dir=str(tmp_path),
    )
    agent = SimpleNamespace(
        team_id=tv.TEAM,
        authorization_config={"enabled": False},
        task_manager=SimpleNamespace(storage_settings=settings),
    )
    forget_task_context_keys()
    set_process_verifier(caller_verifier)
    try:
        # Prime the same runtime identity lookup the canary uses. Its own verifier
        # must work during validation, and the caller's must work afterward.
        runtime_access(agent).verifier.verify_root_task(caller_context, team_id=tv.TEAM, now=fixtures.NOW)
        context_verifier().verify_root_task(caller_context, team_id=tv.TEAM, now=fixtures.NOW)
        report = validation.run_validation()

        assert report["status"] == "passed"
        assert report["storage_backend"] == "sqlite"
        assert report["terminal_review_conclusion"] == "plan_work_remains"
        assert report["replacement_claimed_schedule"] is True
        assert report["owner_collision_archived_snapshots"] == 1
        canary_context = signed_task_token(
            "live-objective-user",
            team_id="team_live_objective",
            roles=("owner",),
        )
        for verifier in (runtime_access(agent).verifier, context_verifier()):
            assert verifier.verify_root_task(caller_context, team_id=tv.TEAM, now=fixtures.NOW).person_id == "usr_alice"
            with pytest.raises(IdentityRejected) as exc_info:
                verifier.verify_root_task(canary_context, team_id="team_live_objective", now=datetime.now(timezone.utc))
            assert exc_info.value.code == "wrong_installation"
    finally:
        forget_task_context_keys()

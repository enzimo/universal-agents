"""The storage canary exercises current tools without an external service."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from access_control.identity import (
    ENV_VERIFY_KEYS,
    ISSUER_GATEWAY,
    ContextSigner,
    IdentityRejected,
    generate_signing_key,
    verify_keys_value,
)
from custom_agents.access_runtime import runtime_access
from custom_agents.task_context_runtime import context_verifier
from storage.config import resolve_storage_settings
from tests import live_objective_storage_validation as validation
from tests.broker_access_support import context_signer, forget_task_context_keys


def test_objective_storage_canary_completes_and_restores_identity_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("LIVE_TEST_POSTGRES_DSN", raising=False)
    monkeypatch.setattr(validation, "RESULT_DIR", tmp_path)
    private_key, public_key = generate_signing_key(ISSUER_GATEWAY)
    caller_keys = verify_keys_value([public_key])
    monkeypatch.setenv(ENV_VERIFY_KEYS, caller_keys)
    caller_context = ContextSigner.from_value(ISSUER_GATEWAY, private_key).issue(
        team_id="team_live_objective", person_id="caller-person"
    )
    settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(tmp_path / "task_history.db")}},
        base_dir=str(tmp_path),
    )
    agent = SimpleNamespace(
        team_id="team_live_objective",
        authorization_config={"enabled": False},
        task_manager=SimpleNamespace(storage_settings=settings),
    )
    forget_task_context_keys()
    try:
        # Prime the same runtime identity lookup the canary uses. Its own key
        # must work during validation, and the caller's must work afterward.
        runtime_access(agent).verifier.verify(caller_context)
        context_verifier().verify(caller_context)
        report = validation.run_validation()

        assert report["status"] == "passed"
        assert report["storage_backend"] == "sqlite"
        assert report["terminal_review_conclusion"] == "plan_work_remains"
        assert report["replacement_claimed_schedule"] is True
        assert report["owner_collision_archived_snapshots"] == 1
        assert os.environ[ENV_VERIFY_KEYS] == caller_keys
        canary_context = context_signer().issue(
            team_id="team_live_objective", person_id="live-objective-user"
        )
        for verifier in (runtime_access(agent).verifier, context_verifier()):
            assert verifier.verify(caller_context).person_id == "caller-person"
            with pytest.raises(IdentityRejected, match="unknown key"):
                verifier.verify(canary_context)
    finally:
        forget_task_context_keys()

"""Test identity setup replaces keys cached by a previous runtime."""

import pytest

from access_control.identity import IdentityRejected
from custom_agents.task_context_runtime import context_verifier
from tests.broker_access_support import context_signer, forget_task_context_keys
from tests.change_approval_support import use_signed_contexts


def test_signed_context_setup_replaces_an_already_cached_verifier(monkeypatch):
    monkeypatch.delenv("ACCESS_CONTEXT_VERIFY_KEYS", raising=False)
    forget_task_context_keys()
    signed = context_signer().issue(team_id="team", person_id="owner")
    try:
        # A live agent can initialize this before a later fixture sets its keys.
        with pytest.raises(IdentityRejected):
            context_verifier().verify(signed, team_id="team")

        use_signed_contexts(monkeypatch)

        assert context_verifier().verify(signed, team_id="team").person_id == "owner"
    finally:
        forget_task_context_keys()

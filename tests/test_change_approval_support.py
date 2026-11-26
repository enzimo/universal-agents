"""Test identity setup replaces keys cached by a previous runtime."""

import pytest

from access_control.authority_verifier import IdentityRejected
from custom_agents.task_context_runtime import context_verifier
from tests.broker_access_support import forget_task_context_keys, signed_task_token
from tests.change_approval_support import use_signed_contexts


def test_signed_context_setup_replaces_an_already_cached_verifier(monkeypatch):
    forget_task_context_keys()
    signed = signed_task_token("owner", team_id="team", roles=("owner",))
    try:
        # A live agent can initialize this before a later fixture sets its trust store.
        with pytest.raises(IdentityRejected):
            context_verifier().verify_root_task(signed, team_id="team")

        use_signed_contexts(monkeypatch)

        assert context_verifier().verify_root_task(signed, team_id="team").person_id == "owner"
    finally:
        forget_task_context_keys()

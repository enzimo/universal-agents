"""A newly added person's first message passes the first-time setup in the real gateway.

``UserGateway`` wires ``assistant_gateway.personal_setup`` into the message
path: a person whose first-time setup is due and who has no model gets the
setup list as the reply, and nothing is sent to the team (no request is
recorded, no manager is needed). The setup and its list are tested in
``tests/test_person_setup.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from access_control.person_settings import PersonSettings
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.personal_setup import SETUP_DUE_EVENT, SETUP_SHOWN_EVENT
from assistant_gateway.store import GatewayStateStore


def _gateway(tmp_path: Path) -> tuple[UserGateway, GatewayInstanceConfig, GatewayStateStore]:
    config = GatewayInstanceConfig.create(
        instance_name="setup_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_setup",
        timezone="America/Chicago",
        manager_agent_id="manager_setup",
    )
    config.save()
    state = GatewayStateStore(str(tmp_path / "state.db"))
    return UserGateway(config, output_sink=None, state_store=state), config, state


@pytest.mark.asyncio
async def test_a_new_person_without_a_model_gets_the_setup_list_and_nothing_reaches_the_team(tmp_path: Path) -> None:
    gateway, config, state = _gateway(tmp_path)
    person = config.user.canonical_user_id
    # As GatewayPersonSetupStore does when an administrator adds someone with --own-keys.
    PersonSettings(gateway.access_inbox.access_store(), config.manager.team_id).set_team_keys(person, False, by="admin")
    gateway.access_inbox.personal_setup.mark_due(person)

    response = await gateway.application.send_user_message(
        "Summarize my week",
        channel_name="cli",
        user_id=person,
        chat_id=f"cli:{config.instance_name}",
        canonical_user_id=person,
    )

    assert "Welcome to the team." in response.text
    assert "send /auth model me" in response.text and "Your message was not sent to the team yet." in response.text
    assert state.gateway_user_event_types(canonical_user_id=person, event_types=(SETUP_DUE_EVENT, SETUP_SHOWN_EVENT)) == {
        SETUP_DUE_EVENT,
        SETUP_SHOWN_EVENT,
    }
    sessions = gateway.session_repository.list_sessions(owner_canonical_user_id=person)
    assert sessions, "the message reached the person's chat session"
    for session in sessions:
        assert gateway.request_repository.list_requests_for_session(session.session_key) == [], (
            "the message was never sent to the team"
        )

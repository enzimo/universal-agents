"""Keys typed during onboarding become the team's linked accounts at gateway start.

Onboarding writes the Exa and Brave keys to a private hand-over file (never to
team.env); at start the gateway sends them on the broker's gateway-only route
for the person who onboarded; the broker stores them in the team's credential
collection and links them, and the gateway deletes the file and tells
administrators. A key that could not be linked stays in the file.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from assistant_gateway.http_secure_broker import HttpGatewaySecureBroker
from assistant_gateway.onboarding_account_keys import (
    OnboardingAccountKeyHandover,
    keys_from_answers,
    read_handover,
    write_handover,
)
from custom_messages.contracts.access_v2 import AccessNoticeKind, AccessNoticeV1, LinkOwnerKind
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker.credential_catalog import load_catalog
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from access_catalog.catalog import CatalogSource
from access_control.account_sharing import TeamAccountSharing
from custom_messages.contracts.access_v2 import AccountSharing
from tests.broker_access_support import TEST_TEAM, access_store, caller_token
from tests.broker_web_support import build_gate, fake_sealbox, isolate_credentials, start_broker, work_for

EXA_KEY = "typed-exa-key-value"
BRAVE_KEY = "typed-brave-key-value"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    root = tmp_path / "instance"
    InstanceLayout.at(root).ensure_directories()
    return root


@pytest.fixture
def broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    isolate_credentials(tmp_path, monkeypatch)
    held = fake_sealbox(monkeypatch)
    running, stop = start_broker(build_gate(tmp_path), monkeypatch)
    running.sealbox = held  # type: ignore[attr-defined]
    yield running
    stop()


def _gateway_broker(running) -> HttpGatewaySecureBroker:
    from access_control.caller_tokens import CALLER_GATEWAY

    return HttpGatewaySecureBroker(
        internal_base_url=running.base_url, public_base_url=running.base_url, token=caller_token(CALLER_GATEWAY)
    )


def _handover(instance: Path, running, *, roles, notices: list, retry_delays=(0.0,)) -> OnboardingAccountKeyHandover:
    return OnboardingAccountKeyHandover(
        instance_root=instance,
        team_id=TEST_TEAM,
        broker=_gateway_broker(running),
        roles_of=lambda person_id: roles if person_id == "owner-1" else None,
        send_notice=notices.append,
        # The gateway and the broker open the same access store.
        store=lambda: running.gate.links,
        catalog=lambda: running.gate.catalog,
        now=lambda: NOW,
        retry_delays=retry_delays,
    )


def test_the_hand_over_file_is_private_and_merges_new_keys(instance: Path) -> None:
    write_handover(instance, team_id=TEST_TEAM, person_id="owner-1", keys={"http:exa": "first"})
    path = write_handover(
        instance, team_id=TEST_TEAM, person_id="owner-1", keys=keys_from_answers({"brave_search_api_key": " b "})
    )

    assert path == InstanceLayout.at(instance).onboarding_account_keys_path
    assert (path.stat().st_mode & 0o777) == 0o600
    assert InstanceLayout.at(instance).workspaces_dir not in path.parents, "agents cannot reach it"
    handover = read_handover(instance)
    assert handover is not None and handover.keys == {"http:exa": "first", "http:brave-search": "b"}
    assert "first" not in repr(handover), "values stay out of repr and logs"
    assert write_handover(instance, team_id=TEST_TEAM, person_id="owner-1", keys={}) is None


@pytest.mark.asyncio
async def test_gateway_start_links_the_keys_for_the_team_and_deletes_the_file(
    instance: Path, broker, monkeypatch, caplog
) -> None:
    write_handover(instance, team_id=TEST_TEAM, person_id="owner-1", keys={"http:exa": EXA_KEY, "http:brave-search": BRAVE_KEY})
    notices: list[AccessNoticeV1] = []
    caplog.set_level(logging.DEBUG)

    await _handover(instance, broker, roles=("owner",), notices=notices).run(asyncio.Event())

    assert not InstanceLayout.at(instance).onboarding_account_keys_path.exists()
    for tool_id in ("http:exa", "http:brave-search"):
        link = broker.gate.links.active_link(TEST_TEAM, tool_id, LinkOwnerKind.TEAM, TEST_TEAM)
        assert link is not None and link.created_by.id == "owner-1"
    assert set(load_catalog(scope=CredentialOwnerScope.team(TEST_TEAM)).credentials) == {
        "http-exa-team",
        "http-brave-search-team",
    }
    assert sorted(broker.sealbox.values()) == sorted([EXA_KEY, BRAVE_KEY]), "the values are only in the store"
    [notice] = notices
    assert notice.kind is AccessNoticeKind.ONBOARDING_KEYS_LINKED and notice.recipient_roles == ("admin",)
    assert notice.title == "Exa and Brave keys from onboarding were linked for the team"
    assert "event=onboarding_keys_linked" in caplog.text
    for secret in (EXA_KEY, BRAVE_KEY):
        assert secret not in caplog.text and secret not in notice.summary

    # The linked key is what a later Exa search carries.
    from custom_tools.exa_tools import exa_search

    work_for(monkeypatch, "user-1")
    broker.answer_with({"results": []})
    assert exa_search(query="after onboarding")["status"] == "success"
    assert next(v for k, v in broker.sent[-1].headers.items() if k.lower() == "x-api-key") == EXA_KEY


@pytest.mark.asyncio
async def test_keys_stay_in_the_file_when_the_person_is_not_an_administrator(instance: Path, broker, caplog) -> None:
    write_handover(instance, team_id=TEST_TEAM, person_id="owner-1", keys={"http:exa": EXA_KEY})
    notices: list = []

    await _handover(instance, broker, roles=("member",), notices=notices).run(asyncio.Event())

    handover = read_handover(instance)
    assert handover is not None and handover.keys == {"http:exa": EXA_KEY}, "kept for the next start"
    assert broker.gate.links.active_link(TEST_TEAM, "http:exa", LinkOwnerKind.TEAM, TEST_TEAM) is None
    assert notices == []
    assert "event=onboarding_key_not_linked tool_id=http:exa reason_code=account_link_team_admin_required" in caplog.text
    assert EXA_KEY not in caplog.text


@pytest.mark.asyncio
async def test_keys_stay_in_the_file_when_the_broker_does_not_answer(instance: Path, caplog) -> None:
    write_handover(instance, team_id=TEST_TEAM, person_id="owner-1", keys={"http:exa": EXA_KEY})
    unreachable = HttpGatewaySecureBroker(internal_base_url="http://127.0.0.1:9", public_base_url=None, token="token")
    handover = OnboardingAccountKeyHandover(
        instance_root=instance,
        team_id=TEST_TEAM,
        broker=unreachable,
        roles_of=lambda _person: ("owner",),
        send_notice=lambda _notice: None,
        store=lambda: access_store(instance.parent / "access.db"),
        catalog=CatalogSource(None).current,
        retry_delays=(0.0, 0.01),
    )

    await handover.run(asyncio.Event())

    assert read_handover(instance) is not None
    assert caplog.text.count("event=onboarding_keys_broker_retry") == 2
    assert "event=onboarding_keys_broker_unreachable attempts=2" in caplog.text


@pytest.mark.asyncio
async def test_an_inactive_first_user_keeps_the_keys_waiting(instance: Path, broker, caplog) -> None:
    write_handover(instance, team_id=TEST_TEAM, person_id="someone-else", keys={"http:exa": EXA_KEY})

    await _handover(instance, broker, roles=("owner",), notices=[]).run(asyncio.Event())

    assert read_handover(instance) is not None
    assert broker.sent == [] and "event=onboarding_keys_person_inactive" in caplog.text


def test_a_hand_over_file_that_is_not_valid_is_kept_and_reported(instance: Path, caplog) -> None:
    path = InstanceLayout.at(instance).onboarding_account_keys_path
    path.write_text(json.dumps({"team_id": TEST_TEAM, "keys": {}}), encoding="utf-8")

    assert read_handover(instance) is None
    assert path.exists() and "event=onboarding_keys_file_invalid" in caplog.text


@pytest.mark.asyncio
async def test_onboarding_model_keys_become_the_teams_keys_only_for_an_administrator(instance: Path, broker, caplog) -> None:
    from custom_messages.contracts.access_v2 import OnboardingModelKeyV1
    from model_providers.endpoints import EndpointKind
    from secure_tool_broker.model_key_page import team_model_key_store

    team_model_key_store.cache_clear()
    model_key = OnboardingModelKeyV1(
        address="https://openrouter.ai/api/v1", kind=EndpointKind.OPENAI_COMPATIBLE, model_id="deepseek/x", value="sk-onboarding-key-001"
    )
    write_handover(instance, team_id=TEST_TEAM, person_id="owner-1", keys={}, model_keys=[model_key])

    member_notices: list = []
    await _handover(instance, broker, roles=("member",), notices=member_notices).run(asyncio.Event())

    waiting = read_handover(instance)
    assert waiting is not None and set(waiting.model_keys) == {"https://openrouter.ai"}, "kept for the next start"
    assert team_model_key_store(TEST_TEAM).entries() == ()

    notices: list = []
    await _handover(instance, broker, roles=("owner",), notices=notices).run(asyncio.Event())

    stored = team_model_key_store(TEST_TEAM).get("https://openrouter.ai")
    assert stored is not None and stored.key == "sk-onboarding-key-001" and stored.tested_model_id == "deepseek/x"
    assert not InstanceLayout.at(instance).onboarding_account_keys_path.exists()
    [notice] = notices
    assert "openrouter.ai" in notice.title and "/auth model" in notice.summary
    assert "sk-onboarding-key-001" not in caplog.text


# -- the sharing choices made on the Secrets page (access_control.account_sharing) ---------


def _tool(tool_id: str):
    return CatalogSource(None).current().tools[tool_id]


@pytest.mark.asyncio
async def test_a_private_service_key_becomes_the_administrators_own_and_serves_only_their_work(
    instance: Path, broker, monkeypatch, caplog
) -> None:
    write_handover(
        instance,
        team_id=TEST_TEAM,
        person_id="owner-1",
        keys={"http:exa": EXA_KEY, "http:brave-search": BRAVE_KEY},
        sharing={"http:exa": AccountSharing.PRIVATE, "model_keys": AccountSharing.PRIVATE},
    )
    notices: list[AccessNoticeV1] = []
    caplog.set_level(logging.DEBUG)

    await _handover(instance, broker, roles=("owner",), notices=notices).run(asyncio.Event())

    sharing = TeamAccountSharing(broker.gate.links, TEST_TEAM)
    assert sharing.for_tool(_tool("http:exa")) is AccountSharing.PRIVATE
    assert sharing.model_keys() is AccountSharing.PRIVATE
    links = broker.gate.links
    own = links.active_link(TEST_TEAM, "http:exa", LinkOwnerKind.PERSON, "owner-1")
    assert own is not None and links.active_link(TEST_TEAM, "http:exa", LinkOwnerKind.TEAM, TEST_TEAM) is None
    assert links.active_link(TEST_TEAM, "http:brave-search", LinkOwnerKind.TEAM, TEST_TEAM) is not None
    assert "http-exa-personal" in load_catalog(scope=CredentialOwnerScope.user("owner-1")).credentials
    assert not InstanceLayout.at(instance).onboarding_account_keys_path.exists()
    team_notice, own_notice = notices
    assert team_notice.recipient_roles == ("admin",) and team_notice.title.startswith("Brave key from onboarding")
    assert own_notice.recipient_person_id == "owner-1"
    assert own_notice.title == "Exa key from onboarding was saved as your own"
    assert "event=onboarding_sharing_recorded" in caplog.text and "event=onboarding_keys_linked_own" in caplog.text
    assert EXA_KEY not in caplog.text and EXA_KEY not in own_notice.summary

    # The administrator's searches carry their key; someone else's have no Exa account.
    from custom_tools.exa_tools import exa_search

    work_for(monkeypatch, "owner-1")
    broker.answer_with({"results": []})
    assert exa_search(query="mine")["status"] == "success"
    assert next(v for k, v in broker.sent[-1].headers.items() if k.lower() == "x-api-key") == EXA_KEY
    sent = len(broker.sent)
    work_for(monkeypatch, "user-1")
    refused = exa_search(query="theirs")
    assert refused["status"] == "error" and "/link http:exa" in str(refused)
    assert len(broker.sent) == sent, "nothing reached Exa for the other person"


@pytest.mark.asyncio
async def test_choices_are_recorded_once_so_a_later_change_wins_over_a_restart(instance: Path, broker) -> None:
    write_handover(
        instance, team_id=TEST_TEAM, person_id="owner-1", keys={"http:exa": EXA_KEY},
        sharing={"http:exa": AccountSharing.PRIVATE},
    )
    unreachable = OnboardingAccountKeyHandover(
        instance_root=instance,
        team_id=TEST_TEAM,
        broker=HttpGatewaySecureBroker(internal_base_url="http://127.0.0.1:9", public_base_url=None, token="token"),
        roles_of=lambda _person: ("owner",),
        send_notice=lambda _notice: None,
        store=lambda: broker.gate.links,
        catalog=lambda: broker.gate.catalog,
        retry_delays=(0.0,),
    )

    # The broker is down: the choice is recorded anyway and leaves the file; the key waits.
    await unreachable.run(asyncio.Event())
    waiting = read_handover(instance)
    sharing = TeamAccountSharing(broker.gate.links, TEST_TEAM)
    assert waiting is not None and waiting.keys == {"http:exa": EXA_KEY} and waiting.sharing == {}
    assert sharing.for_tool(_tool("http:exa")) is AccountSharing.PRIVATE
    # An administrator shares Exa before the next start: the waiting key becomes the team's.
    sharing.choose_tool(_tool("http:exa"), AccountSharing.SHARED, by="owner-1")
    await _handover(instance, broker, roles=("owner",), notices=[]).run(asyncio.Event())

    assert sharing.for_tool(_tool("http:exa")) is AccountSharing.SHARED
    assert broker.gate.links.active_link(TEST_TEAM, "http:exa", LinkOwnerKind.TEAM, TEST_TEAM) is not None
    assert broker.gate.links.active_link(TEST_TEAM, "http:exa", LinkOwnerKind.PERSON, "owner-1") is None


@pytest.mark.asyncio
async def test_choices_without_keys_are_recorded_and_the_file_goes(instance: Path, broker) -> None:
    write_handover(instance, team_id=TEST_TEAM, person_id="owner-1", keys={}, sharing={"cli:x": AccountSharing.SHARED})

    await _handover(instance, broker, roles=("owner",), notices=[]).run(asyncio.Event())

    assert TeamAccountSharing(broker.gate.links, TEST_TEAM).for_tool(_tool("cli:x")) is AccountSharing.SHARED
    assert not InstanceLayout.at(instance).onboarding_account_keys_path.exists()
    assert broker.sent == []


@pytest.mark.asyncio
async def test_a_member_cannot_record_choices_so_they_wait(instance: Path, broker, caplog) -> None:
    write_handover(instance, team_id=TEST_TEAM, person_id="owner-1", keys={}, sharing={"http:exa": AccountSharing.PRIVATE})

    await _handover(instance, broker, roles=("member",), notices=[]).run(asyncio.Event())

    assert broker.gate.links.team_account_sharing(TEST_TEAM) == []
    waiting = read_handover(instance)
    assert waiting is not None and waiting.sharing == {"http:exa": AccountSharing.PRIVATE}
    assert "event=onboarding_sharing_refused" in caplog.text


@pytest.mark.asyncio
async def test_unknown_and_fixed_services_in_the_file_are_skipped(instance: Path, broker, caplog) -> None:
    write_handover(
        instance, team_id=TEST_TEAM, person_id="owner-1", keys={},
        sharing={"http:gone": AccountSharing.SHARED, "cli:github": AccountSharing.SHARED, "http:exa": AccountSharing.PRIVATE},
    )

    await _handover(instance, broker, roles=("owner",), notices=[]).run(asyncio.Event())

    assert [record.subject for record in broker.gate.links.team_account_sharing(TEST_TEAM)] == ["http:exa"]
    assert "event=onboarding_sharing_unknown_service subject=http:gone" in caplog.text
    assert "event=onboarding_sharing_fixed_service subject=cli:github" in caplog.text
    assert not InstanceLayout.at(instance).onboarding_account_keys_path.exists()


def test_the_hand_over_file_merges_sharing_choices(instance: Path) -> None:
    write_handover(instance, team_id=TEST_TEAM, person_id="owner-1", keys={}, sharing={"http:exa": AccountSharing.PRIVATE})
    write_handover(
        instance, team_id=TEST_TEAM, person_id="owner-1", keys={"http:exa": EXA_KEY},
        sharing={"model_keys": AccountSharing.SHARED},
    )

    handover = read_handover(instance)
    assert handover is not None and handover.keys == {"http:exa": EXA_KEY}
    assert handover.sharing == {"http:exa": AccountSharing.PRIVATE, "model_keys": AccountSharing.SHARED}

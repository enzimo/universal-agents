"""The gateway's access Inbox with real Cedar and SQLite.

Covers: a waiting request announced once in the task's conversation; a staged
change announced with its one-line summary and the chat card's data (the
request id, choices, and the stored decision read back by id); who sees
and who decides it; a decision that wakes the requesting agent, including
when the transport is down at the time; silent notices with read marks and a
one-click revoke; and refusals shown as plain text.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from access_catalog.catalog import load_catalog
from access_control.cedar import CedarAccessClient
from access_control.decisions import AccessService, HandDownRefused
from access_control.entities import AgentPrincipal, PersonPrincipal, agent_role_resolver, schedule_subject
from access_control.inbox import StoredAccessInbox
from access_control.requests import AccessRequests
from access_control.starter_profiles import apply_starter_profile
from assistant_gateway.access_inbox import GatewayAccessInbox, GatewayAccessInboxSettings
from assistant_gateway.access_setup import MembershipRoles
from assistant_gateway.application.access_inbox_ports import NoticeQuery
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeKind,
    AccessNoticeV1,
    AccessRequestResolvedV1,
    AccessRequestStatus,
    AccessReviewChoice,
    ChangeProposalV1,
    GrantLifetime,
    GrantStatus,
    TargetKind,
    TargetRefV1,
)
from storage.access_repository import AccessRepository
from storage.config import resolve_storage_settings

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target" / "debug" / "ua-cedar-authorizer"
TEAM = "team-1"
LEAD = "TeamArchitect"
OWNER = PersonPrincipal("owner-1", TEAM, ("owner",))
# work_id: the request its task works on (task-only access is bound to it).
WORKER = AgentPrincipal("agent-web", "WebResearchAgent", "worker", TEAM, work_id="task-1")
LEAD_AGENT = AgentPrincipal("agent-lead", LEAD, "team_lead", TEAM, work_id="task-1")
MODEL_SWITCH = "Switch TeamArchitect to deepseek/deepseek-v4.1-flash via openrouter (files off). Takes effect now."
SCHEDULES = TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules")
# A tool members do not hold (in no standard set).
X_POSTS = TargetRefV1(kind=TargetKind.TOOL, id="cli:x")

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="build ua-cedar-authorizer first (cargo build)")


class Identity:
    """Active users and their team roles, as the gateway identity store keeps them."""

    def __init__(self, roles: dict[str, str]) -> None:
        self.roles = roles

    def get_gateway_user(self, person_id: str) -> Any:
        return SimpleNamespace(status="active") if person_id in self.roles else None

    def list_team_memberships(self, person_id: str, status: str = "active") -> list[Any]:
        role = self.roles.get(person_id)
        return [SimpleNamespace(team_id=TEAM, role=role)] if role else []


@dataclass
class Manager:
    connected: bool = True
    published: list[tuple[str, Any]] = field(default_factory=list)

    async def publish_durable(self, envelope: Any, *, routing_key: str) -> None:
        self.published.append((routing_key, envelope))


def session(owner: str, conversation_id: str, key: str) -> Any:
    return SimpleNamespace(
        session_key=key,
        owner_canonical_user_id=owner,
        status="active",
        channel_name="web",
        user_id=owner,
        chat_id=None,
        thread_id=None,
        conversation_id=conversation_id,
    )


@dataclass
class Harness:
    inbox: GatewayAccessInbox
    agents: AccessRequests
    manager: Manager
    emitted: list[Any]


@pytest.fixture
def harness(tmp_path: Path):
    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    identity = Identity({"owner-1": "owner", "member-1": "member"})
    sessions = {"conv-1": session("owner-1", "conv-1", "s-owner"), "conv-2": session("member-1", "conv-2", "s-member")}
    manager = Manager()
    emitted: list[Any] = []

    async def emit(event: Any) -> None:
        emitted.append(event)

    inbox = GatewayAccessInbox(
        settings=GatewayAccessInboxSettings(
            team_id=TEAM,
            instance_name="test",
            response_route="gateway-route",
            lead_agent_type=LEAD,
            inbox_url="https://ua.example/webchat",
            instance_root=None,
            storage_settings=storage,
            cedar_binary=str(BINARY),
            authorization_enabled=True,
        ),
        memberships=MembershipRoles(identity),
        manager_client=manager,
        session_repository=SimpleNamespace(get_session_by_conversation_id=sessions.get),
        proactive_session=lambda: session("owner-1", "conv-default", "s-default"),
        emit_delivery_event=emit,
    )
    # The agents' side: its own service on the same store, as in an agent process.
    service = AccessService(
        store=AccessRepository(storage),
        catalog=load_catalog(None),
        cedar=CedarAccessClient(binary=str(BINARY)),
        team_id=TEAM,
        agent_role_of=agent_role_resolver(LEAD),
    )
    agents = AccessRequests(service=service, inbox=StoredAccessInbox(service.store), lead_agent_type=LEAD)
    yield Harness(inbox, agents, manager, emitted)
    inbox.close()
    service.cedar.close()


def ask(harness: Harness, person: PersonPrincipal = OWNER, conversation_id: str = "conv-1"):
    return harness.agents.submit(
        requester=WORKER,
        person=person,
        target=SCHEDULES,
        level=AccessLevel.WRITE,
        reason="Track the weekly report",
        task_id="task-1",
        conversation_id=conversation_id,
    ).request


def test_a_waiting_request_is_announced_once_where_its_person_can_decide(harness: Harness) -> None:
    request = ask(harness)

    asyncio.run(harness.inbox.run_once())
    asyncio.run(harness.inbox.run_once())

    [event] = harness.emitted
    assert event.session_key == "s-owner" and event.message_type == "access_review"
    assert event.review_url == f"https://ua.example/webchat?view=inbox#access_request={request.request_id}"
    assert "WebResearchAgent asks for read-and-change access to" in event.text
    assert "Track the weekly report" in event.text
    # Chat buttons show no hover or card, so the message says what each one does.
    assert "What each button does:" in event.text
    assert "- Allow for this task: WebResearchAgent can use Schedules (read-and-change) for this task only" in event.text
    assert "- Allow and keep: TeamArchitect keeps read-and-change access to Schedules for your later tasks" in event.text
    # The web card gets the same words, one variant per state of its boxes.
    [view] = asyncio.run(harness.inbox.inbox("owner-1"))["reviews"]
    assert view["choice_effects_default"] == "kept" and "TeamArchitect keeps it for you" in view["keep_help"]
    assert set(view["choice_effects"]["approve"]) == {"task", "kept"}
    assert view["choice_effects"]["deny"] == {"any": "Nothing is given, and WebResearchAgent is told you said no."}


@pytest.mark.parametrize("operation", ["end_grants_without_granted_outcome", "decision_recoveries"])
def test_a_failed_recovery_check_does_not_skip_announcements(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, operation: str
) -> None:
    request = ask(harness)

    def broken(*_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("database unavailable")

    store = harness.inbox.requests().service.store
    monkeypatch.setattr(type(store), operation, broken)
    with caplog.at_level(logging.ERROR):
        asyncio.run(harness.inbox.run_once())

    [event] = harness.emitted
    assert event.access_request_id == request.request_id
    event = "grants_without_outcome_check_failed" if operation == "end_grants_without_granted_outcome" else "access_decision_recovery_scan_failed"
    assert f"event={event}" in caplog.text


def test_a_members_request_goes_to_the_member_when_they_hold_the_access(harness: Harness) -> None:
    """Decision D13: what an agent may do for a person who holds the access is theirs to decide."""

    member = PersonPrincipal("member-1", TEAM, ("member",))
    request = ask(harness, person=member, conversation_id="conv-2")
    assert not request.person_lacks_access

    asyncio.run(harness.inbox.run_once())
    assert [event.session_key for event in harness.emitted] == ["s-member"], "announced where the member can decide"

    [member_view] = asyncio.run(harness.inbox.inbox("member-1"))["reviews"]
    assert member_view["can_decide"] and not member_view["person_lacks_access"]
    assert [c["choice"] for c in member_view["choices"]] == ["approve", "approve_read", "deny"]
    assert asyncio.run(harness.inbox.inbox("owner-1"))["reviews"] == [], "an administrator does not decide it"
    assert asyncio.run(harness.inbox.inbox("stranger"))["reviews"] == []

    with pytest.raises(HandDownRefused, match="Only the person this work is for"):
        asyncio.run(harness.inbox.decide("owner-1", request.request_id, "approve"))
    decided = asyncio.run(harness.inbox.decide("member-1", request.request_id, "approve"))
    assert decided["status"] == "granted"
    # Kept by the team lead for the member's work; the worker has it for its task.
    lead_grant_id, _task_grant_id = harness.inbox.requests().service.store.get_request(request.request_id).grant_ids
    lead_grant = harness.agents.service.store.get(lead_grant_id)
    assert lead_grant.for_person_id == "member-1" and lead_grant.subject.id == f"{TEAM}/{LEAD}"


def test_a_members_request_for_access_they_lack_goes_to_the_owner_and_the_member_sees_it_read_only(
    harness: Harness,
) -> None:
    member = PersonPrincipal("member-1", TEAM, ("member",))
    request = harness.agents.submit(
        requester=WORKER,
        person=member,
        target=X_POSTS,
        level=AccessLevel.WRITE,
        reason="Post the weekly report",
        task_id="task-1",
        conversation_id="conv-2",
    ).request
    assert request.person_lacks_access

    asyncio.run(harness.inbox.run_once())
    assert [event.session_key for event in harness.emitted] == ["s-default"], "the member cannot decide"

    [member_view] = asyncio.run(harness.inbox.inbox("member-1"))["reviews"]
    assert member_view["can_decide"] is False and member_view["choices"] == []
    [owner_view] = asyncio.run(harness.inbox.inbox("owner-1"))["reviews"]
    assert owner_view["can_decide"] and owner_view["person_lacks_access"]
    assert [c["choice"] for c in owner_view["choices"]] == ["approve", "approve_read", "deny"]
    assert asyncio.run(harness.inbox.inbox("stranger"))["reviews"] == []

    with pytest.raises(HandDownRefused):
        asyncio.run(harness.inbox.decide("member-1", request.request_id, "approve"))


def test_a_decision_wakes_the_agent_now_or_after_the_transport_returns(harness: Harness) -> None:
    first, second = ask(harness), ask(harness)

    decided = asyncio.run(harness.inbox.decide("owner-1", first.request_id, "approve_read", "Read is enough"))

    assert decided["status"] == "granted"
    [(route, envelope)] = harness.manager.published
    assert route == WORKER.agent_id and isinstance(envelope.msg_content, AccessRequestResolvedV1)
    assert envelope.msg_content.request_id == first.request_id

    harness.manager.connected = False
    asyncio.run(harness.inbox.decide("owner-1", second.request_id, "deny"))
    store = harness.inbox.requests().service.store
    assert [r.request_id for r in store.unsent_resolutions(TEAM)] == [second.request_id]
    harness.manager.connected = True
    asyncio.run(harness.inbox.run_once())
    assert harness.manager.published[-1][1].msg_content.request_id == second.request_id
    assert store.unsent_resolutions(TEAM) == []
    assert store.get_request(second.request_id).status is AccessRequestStatus.DENIED

    with pytest.raises(HandDownRefused, match="already"):
        asyncio.run(harness.inbox.decide("owner-1", first.request_id, "approve"))
    with pytest.raises(HandDownRefused, match="Choose approve"):
        asyncio.run(harness.inbox.decide("owner-1", "access_req_x", "maybe"))


def test_notices_are_listed_silently_marked_read_and_offer_a_revoke(harness: Harness) -> None:
    apply_starter_profile(harness.agents.service, OWNER, "trusted", lead_agent_type=LEAD)
    # Access added to one worker task is not announced.
    for_the_task = harness.agents.submit(
        requester=WORKER, person=OWNER, target=SCHEDULES, level=AccessLevel.READ, reason="Research", task_id="task-1"
    )
    assert for_the_task.request.status is AccessRequestStatus.GRANTED
    # Lasting access handed on (a scheduled job's, at setup) is.
    handed = harness.agents.give(
        LEAD_AGENT,
        recipient=schedule_subject(TEAM, "digest"),
        target=SCHEDULES,
        level=AccessLevel.READ,
        reason="The digest job needs it",
        person=OWNER,
    )
    assert handed.grant is not None and handed.grant.status is GrantStatus.ACTIVE

    asyncio.run(harness.inbox.run_once())
    assert harness.emitted == [] and harness.manager.published == [], "notices are silent; nobody waits"
    inbox = asyncio.run(harness.inbox.inbox("owner-1"))
    [notice] = inbox["notices"]
    assert inbox["unread_notices"] == 1 and notice["grant_id"] == handed.grant.grant_id
    assert asyncio.run(harness.inbox.inbox("member-1"))["notices"] == [], "the notice is the owner's"

    assert asyncio.run(harness.inbox.mark_notices_read("owner-1", (notice["notice_id"],))) == {
        "marked": 1, "unread_notices": 0,
    }
    assert asyncio.run(harness.inbox.inbox("owner-1"))["unread_notices"] == 0

    with pytest.raises(HandDownRefused):
        asyncio.run(harness.inbox.revoke_grant("member-1", handed.grant.grant_id))
    assert asyncio.run(harness.inbox.revoke_grant("owner-1", handed.grant.grant_id)) == [handed.grant.grant_id]
    assert harness.agents.service.store.get(handed.grant.grant_id).status is GrantStatus.REVOKED


def stored_notice(notice_id: str, minute: int, *, person: str | None = None, roles: tuple[str, ...] = ()) -> AccessNoticeV1:
    return AccessNoticeV1(
        notice_id=notice_id, team_id=TEAM, kind=AccessNoticeKind.WORK_STUCK, recipient_person_id=person,
        recipient_roles=roles, title=f"Notice {notice_id}", summary="Something to check.",
        created_at_utc=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minute),
    )


def test_notices_come_in_pages_unread_first_and_are_marked_read_in_bulk(harness: Harness) -> None:
    for minute in range(25):
        harness.inbox.send_notice(stored_notice(f"own-{minute:02d}", minute, person="owner-1"))
    harness.inbox.send_notice(stored_notice("admins", 30, roles=("admin",)))  # an owner is an administrator too
    harness.inbox.send_notice(stored_notice("members", 31, person="member-1"))

    first = asyncio.run(harness.inbox.inbox("owner-1", NoticeQuery(unread_only=True, offset=0, limit=20)))
    assert [n["notice_id"] for n in first["notices"][:2]] == ["admins", "own-24"], "newest first"
    assert (len(first["notices"]), first["notices_total"], first["notices_all_total"], first["unread_notices"]) == (
        20, 26, 26, 26,
    )
    second = asyncio.run(harness.inbox.inbox("owner-1", NoticeQuery(unread_only=True, offset=20, limit=20)))
    assert [n["notice_id"] for n in second["notices"]][-1] == "own-00" and second["notices_offset"] == 20

    # Someone else's notice is ignored, not marked.
    marked = asyncio.run(harness.inbox.mark_notices_read("owner-1", ("admins", "own-24", "members")))
    assert marked == {"marked": 2, "unread_notices": 24}
    unread = asyncio.run(harness.inbox.inbox("owner-1"))
    assert unread["notices_total"] == 24 and "admins" not in {n["notice_id"] for n in unread["notices"]}
    everything = asyncio.run(harness.inbox.inbox("owner-1", NoticeQuery(unread_only=False)))
    assert everything["notices_total"] == 26 and everything["notices"][0] == {**everything["notices"][0], "read": True}
    assert asyncio.run(harness.inbox.counts("owner-1")) == {"decidable_requests": 0, "unread_notices": 24}

    assert asyncio.run(harness.inbox.mark_notices_read("owner-1", None)) == {"marked": 24, "unread_notices": 0}
    assert asyncio.run(harness.inbox.counts("member-1"))["unread_notices"] == 1, "only the owner's notices were marked"
    assert asyncio.run(harness.inbox.counts("stranger")) == {"decidable_requests": 0, "unread_notices": 0}


def test_the_badge_counts_requests_the_person_can_decide(harness: Harness) -> None:
    harness.agents.submit(
        requester=WORKER, person=OWNER, target=X_POSTS, level=AccessLevel.WRITE, reason="Post the digest",
        task_id="task-1", conversation_id="conv-1",
    )
    assert asyncio.run(harness.inbox.counts("owner-1"))["decidable_requests"] == 1
    assert asyncio.run(harness.inbox.counts("member-1"))["decidable_requests"] == 0


def test_a_request_to_run_code_outside_the_sandbox_says_what_that_means(harness: Harness) -> None:
    request = harness.agents.submit(
        requester=WORKER,
        person=OWNER,
        target=TargetRefV1(kind=TargetKind.TOOL, id="code:unconfined"),
        level=AccessLevel.FULL,
        reason="Install a system package",
        task_id="task-1",
        conversation_id="conv-1",
    ).request

    asyncio.run(harness.inbox.run_once())
    [view] = asyncio.run(harness.inbox.inbox("owner-1"))["reviews"]

    assert view["request_id"] == request.request_id
    assert view["target_name"] == "Unconfined host execution"
    assert any("outside the sandbox" in line and "runtime's own rights" in line for line in view["effect"])
    [event] = harness.emitted
    assert "runtime's own rights" in event.text


def test_the_card_says_what_approving_does_to_the_holders_access_on_the_same_tool(harness: Harness) -> None:
    def lead_asks(level: AccessLevel, **kwargs):
        return harness.agents.submit(
            requester=LEAD_AGENT, person=OWNER, target=SCHEDULES, level=level, reason="Plan the week",
            task_id="lead-task", conversation_id="conv-1", **kwargs,
        ).request

    kept = lead_asks(AccessLevel.WRITE)
    harness.agents.decide(kept.request_id, OWNER, AccessReviewChoice.APPROVE)
    # read-and-change until removed is in place; more for one task is a step-up beside it.
    assert harness.inbox.entry_lines(harness.agents, lead_asks(AccessLevel.FULL, lifetime=GrantLifetime.TASK)) == [
        "TeamArchitect keeps its read-and-change access to Schedules until someone removes it; "
        "approving adds this beside it."
    ]
    # Full until removed would replace it.
    assert harness.inbox.entry_lines(harness.agents, lead_asks(AccessLevel.FULL)) == [
        "TeamArchitect now has read-and-change access to Schedules until someone removes it. Approving replaces it."
    ]
    # A worker's kept access is the team lead's to keep, so the card says what happens to that.
    worker_asks = harness.agents.submit(
        requester=WORKER, person=OWNER, target=SCHEDULES, level=AccessLevel.FULL, reason="Manage schedules",
        task_id="task-1", conversation_id="conv-1",
    ).request
    assert harness.inbox.entry_lines(harness.agents, worker_asks) == [
        "TeamArchitect now has read-and-change access to Schedules until someone removes it. Approving replaces it."
    ]


def stage_model_switch(harness: Harness):
    return harness.agents.submit_change(
        requester=LEAD_AGENT,
        person=OWNER,
        proposal=ChangeProposalV1(
            kind="runtime_settings",
            change_id="settings_change_1",
            plan_digest="a" * 64,
            title="Change runtime settings: current_agent.llm.provider, current_agent.llm.model_id",
            summary=MODEL_SWITCH,
            effect=(
                "LLM provider override for this or one named agent type; applies at once when it targets the agent making the "
                "change; other running agents of the target type apply it at their next turn.: current_agent.llm.provider for TeamArchitect "
                "changes from 'litellm' to 'openrouter'.",
            ),
        ),
        reason="The person asked for a faster model",
        task_id="task-lead",
        conversation_id="conv-1",
    ).request


def test_a_staged_change_is_announced_with_its_summary_and_the_request_id(harness: Harness) -> None:
    request = stage_model_switch(harness)

    asyncio.run(harness.inbox.run_once())

    [event] = harness.emitted
    assert event.message_type == "access_review" and event.access_request_id == request.request_id
    assert event.review_choices == ("approve_all", "deny")
    first, second = event.text.split("\n")
    assert first == f"TeamArchitect asks for approval: {MODEL_SWITCH}"
    assert second == f"The task waits for your decision. Details are in the Inbox: {event.review_url}"
    assert "LLM provider override" not in event.text, "registry descriptions stay in the Inbox details"
    [view] = asyncio.run(harness.inbox.inbox("owner-1"))["reviews"]
    assert view["summary"] == first
    assert any("LLM provider override" in line for line in view["effect"]), "the Inbox keeps the full detail"


def test_one_request_is_read_by_id_with_its_stored_decision_for_those_who_may_see_it(harness: Harness) -> None:
    request = stage_model_switch(harness)

    waiting = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert waiting["status"] == "waiting" and waiting["can_decide"] is True
    assert [choice["choice"] for choice in waiting["choices"]] == ["approve", "deny"]
    assert asyncio.run(harness.inbox.review("member-1", request.request_id)) is None, "not theirs to see"
    assert asyncio.run(harness.inbox.review("stranger", request.request_id)) is None
    assert asyncio.run(harness.inbox.review("owner-1", "access_req_missing")) is None

    asyncio.run(harness.inbox.decide("owner-1", request.request_id, "deny"))
    decided = asyncio.run(harness.inbox.review("owner-1", request.request_id))

    assert decided["status"] == "denied" and decided["decision"] == "deny"
    assert decided["decided_by_name"] and decided["decided_at_utc"]
    assert decided["can_decide"] is False and decided["choices"] == []
    assert decided["summary"] == waiting["summary"]


def test_a_member_sees_a_request_for_their_work_but_may_not_decide_it(harness: Harness) -> None:
    member = PersonPrincipal("member-1", TEAM, ("member",))
    request = harness.agents.submit(
        requester=WORKER,
        person=member,
        target=X_POSTS,
        level=AccessLevel.WRITE,
        reason="Post the weekly report",
        task_id="task-1",
        conversation_id="conv-2",
    ).request

    view = asyncio.run(harness.inbox.review("member-1", request.request_id))

    assert view["can_decide"] is False and view["choices"] == []
    assert view["summary"] == f"{view['title']} (until someone removes it)."


def test_a_decision_with_account_link_forms_wakes_the_agent_after_the_forms_end(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An approved proposal asks the approver to choose credentials; its task resumes once they did.

    The gateway watches the forms (tool_setup.GatewayToolSetups.after_proposal_links)
    and holds the decision message until they end; the maintenance loop does
    not send the held message meanwhile.
    """

    from assistant_gateway.application.account_links import AccountLinkForm
    from assistant_gateway.proposal_follow_ups import PendingFollowUpLink, form_key, parse_utc

    request = ask(harness)
    watched: dict[str, Any] = {}

    class Setups:
        def after_proposal_links(
            self, *, request_id: str, forms: list[Any], then: Any, on_form_ended: Any = None
        ) -> None:
            watched.update(request_id=request_id, forms=forms, then=then, on_form_ended=on_form_ended)

    form = AccountLinkForm(
        session_id="sess-portainer-1",
        form_url="https://broker.example/account-link/s1",
        status_url="https://broker.example/status/s1",
        expires_at_utc="2099-01-01T00:15:00+00:00",
        tool_display_name="Portainer",
    )

    async def open_links(person_id: str, finished: Any) -> tuple[list[PendingFollowUpLink], list[Any]]:
        entry = PendingFollowUpLink(
            key=form_key(form, "http:portainer", "team"),
            tool_id="http:portainer",
            tool_name="Portainer",
            label="Choose the credential for Portainer",
            url=form.form_url,
            expires_at_utc=parse_utc(form.expires_at_utc),
        )
        return [entry], [(form, "http:portainer", "team", "account")]

    harness.inbox.tool_setups = Setups()
    monkeypatch.setattr(harness.inbox, "_open_account_links", open_links)

    view = asyncio.run(harness.inbox.decide("owner-1", request.request_id, "approve"))
    asyncio.run(harness.inbox.run_once())

    assert view["follow_up_links"][0]["label"] == "Choose the credential for Portainer"
    assert watched["request_id"] == request.request_id and len(watched["forms"]) == 1
    resolved = [env for _route, env in harness.manager.published if isinstance(env.msg_content, AccessRequestResolvedV1)]
    assert resolved == [], "the agent is not told while the approver's form is open"

    asyncio.run(watched["then"]())

    [(route, envelope)] = [
        (route, env) for route, env in harness.manager.published if isinstance(env.msg_content, AccessRequestResolvedV1)
    ]
    assert route == WORKER.agent_id and envelope.msg_content.request_id == request.request_id


def test_proposal_follow_up_links_persist_across_reads_and_dismiss_as_each_form_ends(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up links survive reloads for the approver only and clear as each broker form ends."""

    from assistant_gateway.application.account_links import AccountLinkForm
    from assistant_gateway.application.tool_setup import PageEnding
    from assistant_gateway.proposal_follow_ups import PendingFollowUpLink, form_key, parse_utc

    member = PersonPrincipal("member-1", TEAM, ("member",))
    request = harness.agents.submit(
        requester=WORKER,
        person=member,
        target=X_POSTS,
        level=AccessLevel.WRITE,
        reason="Post the release update",
        task_id="task-1",
        conversation_id="conv-2",
    ).request
    watched: dict[str, Any] = {}

    class Setups:
        def after_proposal_links(
            self, *, request_id: str, forms: list[Any], then: Any, on_form_ended: Any = None
        ) -> None:
            watched.update(request_id=request_id, forms=forms, then=then, on_form_ended=on_form_ended)

    form_one = AccountLinkForm(
        session_id="sess-portainer",
        form_url="https://broker.example/account-link/s1",
        status_url="https://broker.example/status/s1",
        expires_at_utc="2099-01-01T00:15:00+00:00",
        tool_display_name="Portainer",
    )
    form_two = AccountLinkForm(
        session_id="sess-gitea",
        form_url="https://broker.example/account-link/s2",
        status_url="https://broker.example/status/s2",
        expires_at_utc="2099-01-01T00:15:00+00:00",
        tool_display_name="Gitea",
    )

    async def open_links(person_id: str, finished: Any) -> tuple[list[PendingFollowUpLink], list[Any]]:
        entries = [
            PendingFollowUpLink(
                key=form_key(form_one, "http:portainer", "team"),
                tool_id="http:portainer",
                tool_name="Portainer",
                label="Choose the credential for Portainer",
                url=form_one.form_url,
                expires_at_utc=parse_utc(form_one.expires_at_utc),
            ),
            PendingFollowUpLink(
                key=form_key(form_two, "http:gitea", "team"),
                tool_id="http:gitea",
                tool_name="Gitea",
                label="Choose the credential for Gitea",
                url=form_two.form_url,
                expires_at_utc=parse_utc(form_two.expires_at_utc),
            ),
        ]
        return entries, [(form_one, "http:portainer", "team", "account"), (form_two, "http:gitea", "team", "account")]

    harness.inbox.tool_setups = Setups()
    monkeypatch.setattr(harness.inbox, "_open_account_links", open_links)

    decided = asyncio.run(harness.inbox.decide("owner-1", request.request_id, "approve"))
    assert [link["label"] for link in decided["follow_up_links"]] == [
        "Choose the credential for Portainer",
        "Choose the credential for Gitea",
    ]
    assert decided["follow_up_links"][0]["expires_at_utc"] == "2099-01-01T00:15:00+00:00"
    assert decided["follow_up_outcomes"] == []

    # Reloading review_view keeps both links for the approver, and hides them from everyone else.
    approver_view = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert approver_view is not None
    assert len(approver_view["follow_up_links"]) == 2
    other_view = asyncio.run(harness.inbox.review("member-1", request.request_id))
    assert other_view is not None
    assert other_view["follow_up_links"] == []
    assert other_view["follow_up_outcomes"] == []

    # First form finishes: its link disappears immediately while the second remains active.
    watched["on_form_ended"](
        form_one,
        "http:portainer",
        "team",
        PageEnding("completed", "The account link for Portainer is done."),
    )
    after_first = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert after_first is not None
    assert [link["label"] for link in after_first["follow_up_links"]] == ["Choose the credential for Gitea"]
    assert after_first["follow_up_outcomes"] == ["The account link for Portainer is done."]

    # Second form finishes: all links are cleared and both plain outcomes remain visible to the approver.
    watched["on_form_ended"](
        form_two,
        "http:gitea",
        "team",
        PageEnding("declined", "The form for the account link for Gitea was closed without linking anything."),
    )
    after_second = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert after_second is not None
    assert after_second["follow_up_links"] == []
    assert after_second["follow_up_settling"] is False
    assert after_second["follow_up_settling_until_utc"] is None
    assert after_second["follow_up_outcomes"] == [
        "The account link for Portainer is done.",
        "The form for the account link for Gitea was closed without linking anything.",
    ]


def test_proposal_follow_up_review_reports_settling_during_watch_grace_until_late_completion(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A form submitted right before expiry stays settling during WATCH_GRACE until record_ended arrives (F3)."""

    from datetime import timedelta
    from assistant_gateway.application.account_links import AccountLinkForm
    from assistant_gateway.application.broker_session_watch import WATCH_GRACE
    from assistant_gateway.application.tool_setup import PageEnding
    from assistant_gateway.proposal_follow_ups import (
        SETTLING_BACKUP_BUFFER,
        PendingFollowUpLink,
        form_key,
        parse_utc,
    )

    now_box = [harness.agents.service.clock()]
    harness.inbox.requests().service.clock = lambda: now_box[0]
    expires = now_box[0] + timedelta(minutes=15)

    request = ask(harness)
    watched: dict[str, Any] = {}

    class Setups:
        def after_proposal_links(
            self, *, request_id: str, forms: list[Any], then: Any, on_form_ended: Any = None
        ) -> None:
            watched.update(request_id=request_id, forms=forms, then=then, on_form_ended=on_form_ended)

    form_one = AccountLinkForm(
        session_id="sess-grace",
        form_url="https://broker.example/account-link/grace",
        status_url="https://broker.example/status/grace",
        expires_at_utc=expires.isoformat(),
        tool_display_name="Portainer",
    )

    async def open_links(person_id: str, finished: Any) -> tuple[list[PendingFollowUpLink], list[Any]]:
        entry = PendingFollowUpLink(
            key=form_key(form_one, "http:portainer", "team"),
            tool_id="http:portainer",
            tool_name="Portainer",
            label="Choose the credential for Portainer",
            url=form_one.form_url,
            expires_at_utc=parse_utc(form_one.expires_at_utc),
        )
        return [entry], [(form_one, "http:portainer", "team", "account")]

    harness.inbox.tool_setups = Setups()
    monkeypatch.setattr(harness.inbox, "_open_account_links", open_links)

    decided = asyncio.run(harness.inbox.decide("owner-1", request.request_id, "approve"))
    assert len(decided["follow_up_links"]) == 1
    assert decided["follow_up_settling"] is False

    # Move clock 10s past expires_at_utc (inside WATCH_GRACE): link is hidden, settling=True, no premature expiry.
    now_box[0] = expires + timedelta(seconds=10)
    settling_view = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert settling_view is not None
    assert settling_view["follow_up_links"] == []
    assert settling_view["follow_up_outcomes"] == []
    assert settling_view["follow_up_settling"] is True
    assert settling_view["follow_up_settling_until_utc"] == (
        expires + WATCH_GRACE + SETTLING_BACKUP_BUFFER
    ).isoformat()

    # Watch confirms completion during grace: settling clears and completion outcome replaces it.
    now_box[0] = expires + timedelta(seconds=20)
    watched["on_form_ended"](
        form_one,
        "http:portainer",
        "team",
        PageEnding("completed", "The account link for Portainer is done."),
    )
    done_view = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert done_view is not None
    assert done_view["follow_up_links"] == []
    assert done_view["follow_up_settling"] is False
    assert done_view["follow_up_settling_until_utc"] is None
    assert done_view["follow_up_outcomes"] == ["The account link for Portainer is done."]


def test_proposal_with_no_waiting_task_sends_resolution_once_and_still_watches_forms(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When requester_agent_id is None, decide sends resolution once immediately and still watches forms (C07)."""

    from assistant_gateway.application.account_links import AccountLinkForm
    from assistant_gateway.application.tool_setup import PageEnding
    from assistant_gateway.proposal_follow_ups import PendingFollowUpLink, form_key, parse_utc
    from custom_messages.contracts.access_v2 import AccessRequestKind, AccessRequestV1, SubjectKind, SubjectRefV1

    request = AccessRequestV1(
        request_id="access_req_no_agent",
        team_id=TEAM,
        kind=AccessRequestKind.ACCESS,
        requester_agent_id=None,
        person_id="owner-1",
        requested_by_person_id="owner-1",
        subject=SubjectRefV1(kind=SubjectKind.USER, id="owner-1"),
        target=X_POSTS,
        level=AccessLevel.WRITE,
        reason="Approve account links without a waiting agent",
        created_at_utc=harness.agents.service.clock(),
    )
    harness.agents.service.store.create_request(request)

    watched: dict[str, Any] = {}
    resolutions_sent: list[str] = []
    real_send = harness.inbox.send_resolution

    async def counted_send(req: AccessRequestV1) -> bool:
        resolutions_sent.append(req.request_id)
        return await real_send(req)

    class Setups:
        def after_proposal_links(
            self, *, request_id: str, forms: list[Any], then: Any, on_form_ended: Any = None
        ) -> None:
            watched.update(request_id=request_id, forms=forms, then=then, on_form_ended=on_form_ended)

    form_one = AccountLinkForm(
        session_id="sess-no-agent-1",
        form_url="https://broker.example/account-link/na1",
        status_url="https://broker.example/status/na1",
        expires_at_utc="2099-01-01T00:15:00+00:00",
        tool_display_name="Portainer",
    )
    form_two = AccountLinkForm(
        session_id="sess-no-agent-2",
        form_url="https://broker.example/account-link/na2",
        status_url="https://broker.example/status/na2",
        expires_at_utc="2099-01-01T00:15:00+00:00",
        tool_display_name="Gitea",
    )

    async def open_links(person_id: str, finished: Any) -> tuple[list[PendingFollowUpLink], list[Any]]:
        entries = [
            PendingFollowUpLink(
                key=form_key(form_one, "http:portainer", "team"),
                tool_id="http:portainer",
                tool_name="Portainer",
                label="Choose the credential for Portainer",
                url=form_one.form_url,
                expires_at_utc=parse_utc(form_one.expires_at_utc),
            ),
            PendingFollowUpLink(
                key=form_key(form_two, "http:gitea", "team"),
                tool_id="http:gitea",
                tool_name="Gitea",
                label="Choose the credential for Gitea",
                url=form_two.form_url,
                expires_at_utc=parse_utc(form_two.expires_at_utc),
            ),
        ]
        return entries, [(form_one, "http:portainer", "team", "account"), (form_two, "http:gitea", "team", "account")]

    harness.inbox.tool_setups = Setups()
    monkeypatch.setattr(harness.inbox, "_open_account_links", open_links)
    monkeypatch.setattr(harness.inbox, "send_resolution", counted_send)

    decided = asyncio.run(harness.inbox.decide("owner-1", request.request_id, "approve"))
    assert len(decided["follow_up_links"]) == 2
    assert resolutions_sent == [request.request_id], "resolution sent once immediately when no task waits"
    assert watched["request_id"] == request.request_id and callable(watched["on_form_ended"])

    # Releasing after all forms end must not send a second resolution.
    asyncio.run(watched["then"]())
    assert resolutions_sent == [request.request_id]

    # Per-form endings still dismiss links on review().
    watched["on_form_ended"](
        form_one, "http:portainer", "team", PageEnding("completed", "The account link for Portainer is done.")
    )
    mid = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert mid is not None
    assert [link["label"] for link in mid["follow_up_links"]] == ["Choose the credential for Gitea"]
    assert mid["follow_up_outcomes"] == ["The account link for Portainer is done."]


def _as_saved_before_work_ids(tmp_path: Path, request_id: str, *, status: str) -> None:
    """Rewrite a stored request the way v0.6.0 saved one for one task: the agent's task id, no request id."""

    import json
    import sqlite3

    with sqlite3.connect(tmp_path / "manager.db") as conn:
        (payload,) = conn.execute("SELECT payload_json FROM access_requests WHERE request_id=?", (request_id,)).fetchone()
        saved = json.loads(payload)
        for added_later in ("work_id", "schedule_id", "hand_on"):
            saved.pop(added_later, None)
        saved.update(lifetime="task", status=status)
        conn.execute(
            "UPDATE access_requests SET payload_json=?, status=? WHERE request_id=?",
            (json.dumps(saved), status, request_id),
        )


def test_requests_saved_before_the_upgrade_still_show_and_cannot_be_approved(harness: Harness, tmp_path: Path) -> None:
    decided = ask(harness)
    waiting = ask(harness, conversation_id="conv-1")
    _as_saved_before_work_ids(tmp_path, decided.request_id, status="granted")
    _as_saved_before_work_ids(tmp_path, waiting.request_id, status="waiting")

    # Chat cards in older transcripts load these requests.
    shown = asyncio.run(harness.inbox.review("owner-1", decided.request_id))
    assert shown is not None and shown["rows"]
    [listed] = asyncio.run(harness.inbox.inbox("owner-1"))["reviews"]
    assert listed["request_id"] == waiting.request_id
    assert [choice["choice"] for choice in listed["choices"]] == ["deny"]
    assert listed["effect"] == ["This request was made before an upgrade, for a task that has ended, so it can only be "
                                "denied. The agent asks again if it still needs the access."]

    with pytest.raises(HandDownRefused, match="before an upgrade"):
        asyncio.run(harness.inbox.decide("owner-1", waiting.request_id, "approve"))
    denied = asyncio.run(harness.inbox.decide("owner-1", waiting.request_id, "deny"))
    assert denied["status"] == "denied"


def test_the_team_leads_startup_check_withdraws_waiting_requests_saved_before_the_upgrade(
    harness: Harness, tmp_path: Path
) -> None:
    old = ask(harness)
    _as_saved_before_work_ids(tmp_path, old.request_id, status="waiting")

    assert harness.agents.close_requests_saved_before_work_ids() == [old.request_id]

    assert harness.agents.service.store.get_request(old.request_id).status.value == "withdrawn"
    assert asyncio.run(harness.inbox.inbox("owner-1"))["reviews"] == []
    assert harness.agents.close_requests_saved_before_work_ids() == []


def test_a_decided_card_for_someone_linked_to_it_is_shown_without_asking_the_access_service(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat reload reads every card; only cards whose answer can change ask Cedar."""

    asked: list[str] = []
    real = GatewayAccessInbox._can_decide

    def counted(requests, person, request):
        asked.append(person.person_id)
        return real(requests, person, request)

    monkeypatch.setattr(GatewayAccessInbox, "_can_decide", staticmethod(counted))
    request = ask(harness)
    waiting = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert waiting is not None and waiting["can_decide"] and asked == ["owner-1"], "a waiting card asks"

    asyncio.run(harness.inbox.decide("owner-1", request.request_id, "deny"))
    asked.clear()
    decided = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert decided is not None and not decided["can_decide"]
    assert asked == [], "the person the work is for sees their decided card without a check"

    assert asyncio.run(harness.inbox.review("member-1", request.request_id)) is None
    assert asked == ["member-1"], "someone with no link to it is checked, and does not see it"


def test_an_approved_proposals_credential_forms_carry_their_card_view_across_decide_review_and_ending(
    harness: Harness,
) -> None:
    """Approved proposal forms carry frame_url + expires_at_utc for the approver only across decide/review/ending (C03)."""

    from assistant_gateway.application.account_links import AccountLinkForm
    from assistant_gateway.application.tool_setup import PageEnding

    member = PersonPrincipal("member-1", TEAM, ("member",))
    request = harness.agents.submit(
        requester=WORKER,
        person=member,
        target=X_POSTS,
        level=AccessLevel.WRITE,
        reason="Post the release update",
        task_id="task-1",
        conversation_id="conv-2",
    ).request
    opened: list[dict[str, Any]] = []
    watched: dict[str, Any] = {}
    forms_by_tool = {
        "http:portainer": AccountLinkForm(
            session_id="s1",
            form_url="http://127.0.0.1:18600/account-link/v1.s1.sig",
            status_url="http://127.0.0.1:18600/account-link/v1.s1.sig/status",
            expires_at_utc="2099-01-01T00:15:00+00:00",
            tool_display_name="Portainer",
        ),
        "http:gitea": AccountLinkForm(
            session_id="s2",
            form_url="http://127.0.0.1:18600/account-link/v1.s2.sig",
            status_url="http://127.0.0.1:18600/account-link/v1.s2.sig/status",
            expires_at_utc="2099-01-01T00:20:00+00:00",
            tool_display_name="Gitea",
        ),
    }

    class Setups:
        async def open_proposal_link(self, **fields: Any) -> AccountLinkForm:
            opened.append(fields)
            return forms_by_tool[fields["tool_id"]]

        def after_proposal_links(
            self, *, request_id: str, forms: list[Any], then: Any, on_form_ended: Any = None
        ) -> None:
            watched.update(request_id=request_id, forms=forms, then=then, on_form_ended=on_form_ended)

    harness.inbox.tool_setups = Setups()
    from custom_messages.contracts.access_v2 import ProposedAccountLinkV1

    harness.inbox.proposals.account_links[request.request_id] = [
        ProposedAccountLinkV1(tool_id="http:portainer", owner="team", purpose="Read the stacks"),
        ProposedAccountLinkV1(tool_id="http:gitea", owner="team", purpose="Read repos"),
    ]

    decided = asyncio.run(harness.inbox.decide("owner-1", request.request_id, "approve"))
    assert [fields["tool_id"] for fields in opened] == ["http:portainer", "http:gitea"]
    assert len(decided["follow_up_links"]) == 2
    assert decided["follow_up_links"][0]["url"] == "http://127.0.0.1:18600/account-link/v1.s1.sig"
    assert decided["follow_up_links"][0]["frame_url"] == "http://127.0.0.1:18600/account-link/v1.s1.sig?view=card"
    assert decided["follow_up_links"][0]["expires_at_utc"] == "2099-01-01T00:15:00+00:00"
    assert decided["follow_up_links"][0]["label"].startswith("Choose the credential for ")

    # Reloading through review() preserves frame_url and expires_at_utc for the approver only.
    reloaded = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert reloaded is not None
    assert [link["frame_url"] for link in reloaded["follow_up_links"]] == [
        "http://127.0.0.1:18600/account-link/v1.s1.sig?view=card",
        "http://127.0.0.1:18600/account-link/v1.s2.sig?view=card",
    ]
    assert [link["expires_at_utc"] for link in reloaded["follow_up_links"]] == [
        "2099-01-01T00:15:00+00:00",
        "2099-01-01T00:20:00+00:00",
    ]

    other_view = asyncio.run(harness.inbox.review("member-1", request.request_id))
    assert other_view is not None
    assert other_view["follow_up_links"] == []
    assert other_view["follow_up_outcomes"] == []

    # Ending the first form removes only its link; the second form keeps its frame_url on reload.
    watched["on_form_ended"](
        forms_by_tool["http:portainer"],
        "http:portainer",
        "team",
        PageEnding("completed", "The account link for Portainer is done."),
    )
    after_one = asyncio.run(harness.inbox.review("owner-1", request.request_id))
    assert after_one is not None
    assert len(after_one["follow_up_links"]) == 1
    assert after_one["follow_up_links"][0]["url"] == "http://127.0.0.1:18600/account-link/v1.s2.sig"
    assert after_one["follow_up_links"][0]["frame_url"] == "http://127.0.0.1:18600/account-link/v1.s2.sig?view=card"
    assert after_one["follow_up_links"][0]["expires_at_utc"] == "2099-01-01T00:20:00+00:00"
    assert after_one["follow_up_outcomes"] == ["The account link for Portainer is done."]


def test_an_approved_proposals_gate_link_opens_with_gate_purpose_and_distinct_request_id(harness: Harness) -> None:
    from assistant_gateway.application.account_links import AccountLinkForm
    from custom_messages.contracts.access_v2 import LinkPurpose, ProposedAccountLinkV1

    request = ask(harness)
    opened: list[dict[str, Any]] = []

    class Setups:
        async def open_proposal_link(self, **fields: Any) -> AccountLinkForm:
            opened.append(fields)
            return AccountLinkForm(
                session_id=f"s-{fields['purpose']}",
                form_url=f"http://127.0.0.1:18600/account-link/v1.{fields['purpose']}.sig",
                status_url=f"http://127.0.0.1:18600/account-link/v1.{fields['purpose']}.sig/status",
                expires_at_utc="2026-11-20T10:15:00Z",
                tool_display_name="Gitea",
            )

    harness.inbox.tool_setups = Setups()
    harness.inbox.proposals.account_links[request.request_id] = [
        ProposedAccountLinkV1(tool_id="http:gitea", owner="team", purpose="Use team account"),
        ProposedAccountLinkV1(
            tool_id="http:gitea",
            owner="team",
            purpose="Pass the Cloudflare gate",
            link_purpose=LinkPurpose.GATE,
        ),
    ]

    links, forms = asyncio.run(harness.inbox._open_account_links("owner-1", request))

    assert [f["request_id"] for f in opened] == [
        f"{request.request_id}:http:gitea:account",
        f"{request.request_id}:http:gitea:gate",
    ]
    assert [f["purpose"] for f in opened] == ["account", "gate"]
    assert links[0].label.startswith("Choose the credential for ")
    assert links[1].label.startswith("Choose the team gate credential for ")
    assert [entry[3] for entry in forms] == ["account", "gate"]


def test_tool_removal_proposal_renders_under_tool_removal_section_with_full_impact(tmp_path: Path) -> None:
    from access_catalog.instance_tools import write_instance_tool
    from access_control.proposals import ACCESS_PROPOSAL_KIND, proposal_summary, with_tool_removal_baselines
    from custom_messages.contracts.access_v2 import (
        AccessProposalV1,
        AccountLinkStatus,
        AccountLinkV1,
        CredentialInjectionV1,
        GrantKind,
        GrantV1,
        HttpRouteV1,
        InjectionPlacement,
        LinkOwnerKind,
        LinkPurpose,
        ProposedHttpToolV1,
        ProposedToolRemovalV1,
        SubjectKind,
        SubjectRefV1,
    )

    instance_root = tmp_path / "instance"
    write_instance_tool(
        instance_root,
        ProposedHttpToolV1(
            tool_id="http:gitea",
            display_name="Gitea",
            hosts=("gitea.example.com",),
            routes=(HttpRouteV1(name="delete", method="DELETE", path="/repos/**", level=AccessLevel.FULL),),
            uses_credential=False,
        ),
    )

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    identity = Identity({"owner-1": "owner", "member-1": "member"})
    sessions = {"conv-1": session("owner-1", "conv-1", "s-owner")}
    manager = Manager()
    emitted: list[Any] = []

    async def emit(event: Any) -> None:
        emitted.append(event)

    inbox = GatewayAccessInbox(
        settings=GatewayAccessInboxSettings(
            team_id=TEAM,
            instance_name="test",
            response_route="gateway-route",
            lead_agent_type=LEAD,
            inbox_url="https://ua.example/webchat",
            instance_root=instance_root,
            storage_settings=storage,
            cedar_binary=str(BINARY),
            authorization_enabled=True,
        ),
        memberships=MembershipRoles(identity),
        manager_client=manager,
        session_repository=SimpleNamespace(get_session_by_conversation_id=sessions.get),
        proactive_session=lambda: session("owner-1", "conv-default", "s-default"),
        emit_delivery_event=emit,
    )
    try:
        requests = inbox.requests()
        store = requests.service.store
        now = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
        owner_ref = SubjectRefV1(kind=SubjectKind.USER, id="owner-1")
        store.create(
            GrantV1(
                grant_id="g_tool",
                team_id=TEAM,
                kind=GrantKind.ACCESS,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{LEAD}"),
                target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                level=AccessLevel.WRITE,
                lifetime=GrantLifetime.UNTIL_REVOKED,
                status=GrantStatus.ACTIVE,
                granted_by=owner_ref,
                created_at_utc=now,
                for_person_id="owner-1",
            )
        )
        store.create(
            GrantV1(
                grant_id="b_op",
                team_id=TEAM,
                kind=GrantKind.BLOCK,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/Worker"),
                target=TargetRefV1(kind=TargetKind.OPERATION, id="http:gitea#route:delete"),
                level=AccessLevel.FULL,
                lifetime=GrantLifetime.UNTIL_REVOKED,
                status=GrantStatus.ACTIVE,
                granted_by=owner_ref,
                created_at_utc=now,
            )
        )
        store.create_link(
            AccountLinkV1(
                link_id="l_gate",
                team_id=TEAM,
                tool_id="http:gitea",
                owner_kind=LinkOwnerKind.TEAM,
                owner_id=TEAM,
                purpose=LinkPurpose.GATE,
                credential_key="cred_gate",
                injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate", field="secret"),),
                status=AccountLinkStatus.ACTIVE,
                created_by=owner_ref,
                created_at_utc=now,
            )
        )

        proposal = with_tool_removal_baselines(
            AccessProposalV1(
                title="Remove Gitea",
                reason="Gitea instance retired.",
                tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea"),),
            ),
            instance_root=instance_root,
            catalog=requests.service.catalog,
        )
        compiled = inbox.proposals.compiler(requests).compile(proposal, person=OWNER, task_id="task-1")
        store.save_plan(TEAM, "chg_rm_1", ACCESS_PROPOSAL_KIND, compiled.digest, proposal.model_dump(mode="json"), at=now)
        requests.submit_change(
            requester=LEAD_AGENT,
            person=OWNER,
            proposal=ChangeProposalV1(
                kind=ACCESS_PROPOSAL_KIND,
                change_id="chg_rm_1",
                plan_digest=compiled.digest,
                title=proposal.title,
                summary=proposal_summary(compiled.rows),
                effect=tuple(row.text[:1000] for row in compiled.rows[:40]),
                items=compiled.rows,
            ),
            reason=proposal.reason,
            task_id="task-1",
            conversation_id="conv-1",
        )

        [view] = asyncio.run(inbox.inbox("owner-1"))["reviews"]
        assert view["summary"] == f"{LEAD} asks for approval: Access proposal: remove 1 tool. Takes effect when approved."
        assert [row["section"] for row in view["rows"]] == ["tool_removal"] * 4
        assert [row["item_id"] for row in view["rows"]] == [
            "remove-0",
            "remove-0-grant-0",
            "remove-0-block-0",
            "remove-0-link-0",
        ]
        assert "Gitea (http:gitea)" in view["rows"][0]["text"]
        assert "g_tool" in view["rows"][1]["text"]
        assert "b_op" in view["rows"][2]["text"] and "route:delete" in view["rows"][2]["text"]
        assert "l_gate" in view["rows"][3]["text"] and "gate credential link" in view["rows"][3]["text"]

        # F8: approving through GatewayAccessInbox.decide deletes the file, cleans up DB rows, and sends resolution.
        request_id = view["request_id"]
        decided_view = asyncio.run(inbox.decide("owner-1", request_id, "approve"))
        assert decided_view["status"] == "granted"
        assert not (instance_root / "control" / "access" / "tools" / "proposed-http-gitea.yaml").exists()
        assert list((instance_root / "control" / "access" / "tools").glob(".removed-*.tmp")) == []
        assert store.get("g_tool").status is GrantStatus.REVOKED  # type: ignore[union-attr]
        assert store.get("b_op").status is GrantStatus.REVOKED  # type: ignore[union-attr]
        assert store.get_link("l_gate").status is AccountLinkStatus.REVOKED  # type: ignore[union-attr]
        assert any(
            getattr(envelope.msg_content, "request_id", None) == request_id for _key, envelope in manager.published
        )
    finally:
        inbox.close()


def test_tool_removal_inbox_redacts_other_peoples_records_for_non_admin_viewers(tmp_path: Path) -> None:
    """F3: non-admin member sees overview + own/team rows only; other people's grants, blocks, and links are omitted."""

    from access_catalog.instance_tools import write_instance_tool
    from access_control.proposals import ACCESS_PROPOSAL_KIND, proposal_summary, with_tool_removal_baselines
    from custom_messages.contracts.access_v2 import (
        AccessProposalV1,
        AccountLinkStatus,
        AccountLinkV1,
        GrantKind,
        GrantV1,
        LinkOwnerKind,
        LinkPurpose,
        ProposedHttpToolV1,
        ProposedToolRemovalV1,
        SubjectKind,
        SubjectRefV1,
    )

    instance_root = tmp_path / "instance"
    write_instance_tool(
        instance_root,
        ProposedHttpToolV1(
            tool_id="http:gitea",
            display_name="Gitea",
            hosts=("gitea.example.com",),
            uses_credential=True,
        ),
    )

    storage = resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
    identity = Identity({"owner-1": "owner", "member-1": "member", "member-2": "member"})
    sessions = {"conv-1": session("member-1", "conv-1", "s-member")}
    manager = Manager()

    async def emit(_event: Any) -> None:
        return None

    inbox = GatewayAccessInbox(
        settings=GatewayAccessInboxSettings(
            team_id=TEAM,
            instance_name="test",
            response_route="gateway-route",
            lead_agent_type=LEAD,
            inbox_url="https://ua.example/webchat",
            instance_root=instance_root,
            storage_settings=storage,
            cedar_binary=str(BINARY),
            authorization_enabled=True,
        ),
        memberships=MembershipRoles(identity),
        manager_client=manager,
        session_repository=SimpleNamespace(get_session_by_conversation_id=sessions.get),
        proactive_session=lambda: session("owner-1", "conv-default", "s-default"),
        emit_delivery_event=emit,
    )
    try:
        requests = inbox.requests()
        store = requests.service.store
        now = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
        owner_ref = SubjectRefV1(kind=SubjectKind.USER, id="owner-1")
        # Grants: member-1's own, team-wide (for_person_id=None), member-2's private agent grant, member-2's user grant.
        for grant in (
            GrantV1(
                grant_id="g_member1",
                team_id=TEAM,
                kind=GrantKind.ACCESS,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{LEAD}"),
                target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                level=AccessLevel.READ,
                lifetime=GrantLifetime.UNTIL_REVOKED,
                status=GrantStatus.ACTIVE,
                granted_by=owner_ref,
                created_at_utc=now,
                for_person_id="member-1",
            ),
            GrantV1(
                grant_id="g_teamwide",
                team_id=TEAM,
                kind=GrantKind.ACCESS,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{LEAD}"),
                target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                level=AccessLevel.READ,
                lifetime=GrantLifetime.UNTIL_REVOKED,
                status=GrantStatus.ACTIVE,
                granted_by=owner_ref,
                created_at_utc=now,
                for_person_id=None,
            ),
            GrantV1(
                grant_id="g_member2_secret",
                team_id=TEAM,
                kind=GrantKind.ACCESS,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/{LEAD}"),
                target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                level=AccessLevel.FULL,
                lifetime=GrantLifetime.UNTIL_REVOKED,
                status=GrantStatus.ACTIVE,
                granted_by=owner_ref,
                created_at_utc=now,
                for_person_id="member-2",
            ),
            GrantV1(
                grant_id="g_user_member2",
                team_id=TEAM,
                kind=GrantKind.ACCESS,
                subject=SubjectRefV1(kind=SubjectKind.USER, id="member-2"),
                target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                level=AccessLevel.WRITE,
                lifetime=GrantLifetime.UNTIL_REVOKED,
                status=GrantStatus.ACTIVE,
                granted_by=owner_ref,
                created_at_utc=now,
            ),
            GrantV1(
                grant_id="b_teamwide",
                team_id=TEAM,
                kind=GrantKind.BLOCK,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/Worker"),
                target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                level=AccessLevel.FULL,
                lifetime=GrantLifetime.UNTIL_REVOKED,
                status=GrantStatus.ACTIVE,
                granted_by=owner_ref,
                created_at_utc=now,
            ),
            GrantV1(
                grant_id="b_member2_private",
                team_id=TEAM,
                kind=GrantKind.BLOCK,
                subject=SubjectRefV1(kind=SubjectKind.AGENT_TYPE, id=f"{TEAM}/Worker"),
                target=TargetRefV1(kind=TargetKind.TOOL, id="http:gitea"),
                level=AccessLevel.FULL,
                lifetime=GrantLifetime.TASK,
                task_id="task-m2",
                status=GrantStatus.ACTIVE,
                granted_by=owner_ref,
                created_at_utc=now,
                for_person_id="member-2",
            ),
        ):
            store.create(grant)

        # Links: team account, member-1's personal link, member-2's personal link.
        for link in (
            AccountLinkV1(
                link_id="l_team",
                team_id=TEAM,
                tool_id="http:gitea",
                owner_kind=LinkOwnerKind.TEAM,
                owner_id=TEAM,
                purpose=LinkPurpose.ACCOUNT,
                credential_key="cred_team",
                status=AccountLinkStatus.ACTIVE,
                created_by=owner_ref,
                created_at_utc=now,
            ),
            AccountLinkV1(
                link_id="l_member1",
                team_id=TEAM,
                tool_id="http:gitea",
                owner_kind=LinkOwnerKind.PERSON,
                owner_id="member-1",
                purpose=LinkPurpose.ACCOUNT,
                credential_key="cred_m1",
                status=AccountLinkStatus.ACTIVE,
                created_by=SubjectRefV1(kind=SubjectKind.USER, id="member-1"),
                created_at_utc=now,
            ),
            AccountLinkV1(
                link_id="l_member2_secret",
                team_id=TEAM,
                tool_id="http:gitea",
                owner_kind=LinkOwnerKind.PERSON,
                owner_id="member-2",
                purpose=LinkPurpose.ACCOUNT,
                credential_key="cred_m2",
                status=AccountLinkStatus.ACTIVE,
                created_by=SubjectRefV1(kind=SubjectKind.USER, id="member-2"),
                created_at_utc=now,
            ),
        ):
            store.create_link(link)

        member1 = inbox.person("member-1")
        assert member1 is not None
        proposal = with_tool_removal_baselines(
            AccessProposalV1(
                title="Remove Gitea",
                reason="Retired.",
                tool_removals=(ProposedToolRemovalV1(tool_id="http:gitea"),),
            ),
            instance_root=instance_root,
            catalog=requests.service.catalog,
        )
        compiled = inbox.proposals.compiler(requests).compile(proposal, person=member1, task_id="task-m1")
        store.save_plan(TEAM, "chg_rm_priv", ACCESS_PROPOSAL_KIND, compiled.digest, proposal.model_dump(mode="json"), at=now)
        outcome = requests.submit_change(
            requester=LEAD_AGENT,
            person=member1,
            proposal=ChangeProposalV1(
                kind=ACCESS_PROPOSAL_KIND,
                change_id="chg_rm_priv",
                plan_digest=compiled.digest,
                title=proposal.title,
                summary=proposal_summary(compiled.rows),
                effect=tuple(row.text[:1000] for row in compiled.rows[:40]),
                items=compiled.rows,
            ),
            reason=proposal.reason,
            task_id="task-m1",
            conversation_id="conv-1",
        )
        assert outcome.request is not None
        request_id = outcome.request.request_id

        # Administrator sees every detail row and effect line.
        [admin_view] = asyncio.run(inbox.inbox("owner-1"))["reviews"]
        assert admin_view["can_decide"] is True
        admin_text = "\n".join(row["text"] for row in admin_view["rows"])
        admin_effect = "\n".join(admin_view["effect"])
        for secret_id in ("g_member2_secret", "g_user_member2", "b_member2_private", "l_member2_secret"):
            assert secret_id in admin_text and secret_id in admin_effect

        # Non-admin member sees overview + own/team rows only, in BOTH inbox() and review(), rows AND effect.
        [member_inbox_view] = asyncio.run(inbox.inbox("member-1"))["reviews"]
        member_direct_view = asyncio.run(inbox.review("member-1", request_id))
        assert member_direct_view is not None
        for view in (member_inbox_view, member_direct_view):
            assert view["can_decide"] is False
            rows_text = "\n".join(row["text"] for row in view["rows"])
            effect_text = "\n".join(view["effect"])
            assert "Remove web API tool Gitea (http:gitea)" in rows_text
            for visible_id in ("g_member1", "g_teamwide", "b_teamwide", "l_team", "l_member1"):
                assert visible_id in rows_text and visible_id in effect_text
            for hidden_id in ("g_member2_secret", "g_user_member2", "b_member2_private", "l_member2_secret", "member-2"):
                assert hidden_id not in rows_text
                assert hidden_id not in effect_text
    finally:
        inbox.close()

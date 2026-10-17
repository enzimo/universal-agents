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


def test_a_failed_recovery_of_unfinished_decisions_does_not_skip_announcements(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    request = ask(harness)

    def broken(*_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("database unavailable")

    store = harness.inbox.requests().service.store
    monkeypatch.setattr(type(store), "recover_unfinished_decisions", broken)
    with caplog.at_level(logging.ERROR):
        asyncio.run(harness.inbox.run_once())

    [event] = harness.emitted
    assert event.access_request_id == request.request_id
    assert "event=access_decision_recovery_failed" in caplog.text


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

    request = ask(harness)
    watched: dict[str, Any] = {}

    class Setups:
        def after_proposal_links(self, *, request_id: str, forms: list[Any], then: Any) -> None:
            watched.update(request_id=request_id, forms=forms, then=then)

    async def open_links(person_id: str, finished: Any) -> tuple[list[dict[str, str]], list[Any]]:
        link = {"label": "Choose the credential for Portainer", "url": "https://broker.example/account-link/s1"}
        return [link], [("form", "http:portainer", "team")]

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

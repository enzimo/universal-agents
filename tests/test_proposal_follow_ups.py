"""Unit tests for ProposalFollowUpTracker (proposal follow-up credential link lifecycle)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from assistant_gateway.application.account_links import AccountLinkForm
from assistant_gateway.proposal_follow_ups import (
    PendingFollowUpLink,
    ProposalFollowUpTracker,
    form_key,
    parse_utc,
)


def _ts(minutes: int = 0) -> datetime:
    return datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def _link(
    key: str,
    *,
    tool_id: str = "http:portainer",
    tool_name: str = "Portainer",
    url: str = "https://broker.example/account-link/s1",
    expires_at_utc: datetime | None = None,
) -> PendingFollowUpLink:
    return PendingFollowUpLink(
        key=key,
        tool_id=tool_id,
        tool_name=tool_name,
        label=f"Choose the credential for {tool_name}",
        url=url,
        expires_at_utc=expires_at_utc,
    )


def test_snapshot_is_scoped_strictly_to_the_approving_person() -> None:
    tracker = ProposalFollowUpTracker()
    expires = _ts(15)
    tracker.register(
        "req-1",
        approver_person_id="admin-1",
        links=[_link("session:s1", expires_at_utc=expires)],
        now=_ts(0),
    )

    links, outcomes = tracker.snapshot("req-1", viewer_id="admin-1", now=_ts(1))
    assert links == [
        {
            "label": "Choose the credential for Portainer",
            "url": "https://broker.example/account-link/s1",
            "expires_at_utc": expires.isoformat(),
        }
    ]
    assert outcomes == []

    # Another administrator, the requester, or an anonymous viewer never sees these links or outcomes.
    assert tracker.snapshot("req-1", viewer_id="admin-2", now=_ts(1)) == ([], [])
    assert tracker.snapshot("req-1", viewer_id="", now=_ts(1)) == ([], [])


def test_record_ended_dismisses_one_form_at_a_time_and_keeps_sibling_open() -> None:
    tracker = ProposalFollowUpTracker()
    tracker.register(
        "req-2",
        approver_person_id="admin-1",
        links=[
            _link("session:s1", tool_id="http:portainer", tool_name="Portainer", url="https://broker.example/s1"),
            _link("session:s2", tool_id="http:gitea", tool_name="Gitea", url="https://broker.example/s2"),
        ],
        now=_ts(0),
    )

    assert tracker.record_ended(
        "req-2",
        key="session:s1",
        message="The account link for Portainer is done.",
        now=_ts(2),
    )

    links, outcomes = tracker.snapshot("req-2", viewer_id="admin-1", now=_ts(2))
    assert [link["label"] for link in links] == ["Choose the credential for Gitea"]
    assert outcomes == ["The account link for Portainer is done."]

    assert tracker.record_ended(
        "req-2",
        key="session:s2",
        message="The form for the account link for Gitea was closed without linking anything.",
        now=_ts(3),
    )
    links, outcomes = tracker.snapshot("req-2", viewer_id="admin-1", now=_ts(3))
    assert links == []
    assert outcomes == [
        "The account link for Portainer is done.",
        "The form for the account link for Gitea was closed without linking anything.",
    ]


def test_expired_links_drop_on_snapshot_and_upgrade_cleanly_if_completed_in_grace() -> None:
    tracker = ProposalFollowUpTracker()
    tracker.register(
        "req-3",
        approver_person_id="admin-1",
        links=[
            _link("session:s1", tool_name="Portainer", expires_at_utc=_ts(10)),
            _link("session:s2", tool_name="Gitea", expires_at_utc=_ts(20)),
        ],
        now=_ts(0),
    )

    links, outcomes = tracker.snapshot("req-3", viewer_id="admin-1", now=_ts(10))
    assert [link["label"] for link in links] == ["Choose the credential for Gitea"]
    assert outcomes == ["The form for the account link for Portainer expired before it was used."]

    # Broker watch confirms completion during WATCH_GRACE: replaces expiry text without duplicating.
    tracker.record_ended(
        "req-3",
        key="session:s1",
        message="The account link for Portainer is done.",
        now=_ts(11),
    )
    links, outcomes = tracker.snapshot("req-3", viewer_id="admin-1", now=_ts(11))
    assert [link["label"] for link in links] == ["Choose the credential for Gitea"]
    assert outcomes == ["The account link for Portainer is done."]


def test_prune_drops_old_finished_entries_and_caps_total_entries() -> None:
    tracker = ProposalFollowUpTracker(finished_retention=timedelta(minutes=30), max_tracked_requests=2)
    tracker.register("req-old", approver_person_id="admin-1", links=[_link("session:s1")], now=_ts(0))
    tracker.record_ended("req-old", key="session:s1", message="Done.", now=_ts(1))

    # Still visible before retention elapses.
    assert tracker.snapshot("req-old", viewer_id="admin-1", now=_ts(29)) == ([], ["Done."])
    tracker.prune(_ts(31))
    assert tracker.snapshot("req-old", viewer_id="admin-1", now=_ts(31)) == ([], [])

    # Max capacity evicts finished entries before active ones.
    tracker.register("req-done", approver_person_id="admin-1", links=[_link("session:a")], now=_ts(40))
    tracker.record_ended("req-done", key="session:a", message="Done.", now=_ts(41))
    tracker.register("req-live-1", approver_person_id="admin-1", links=[_link("session:b")], now=_ts(42))
    tracker.register("req-live-2", approver_person_id="admin-1", links=[_link("session:c")], now=_ts(43))

    assert tracker.snapshot("req-done", viewer_id="admin-1", now=_ts(43)) == ([], [])
    assert len(tracker.snapshot("req-live-1", viewer_id="admin-1", now=_ts(43))[0]) == 1
    assert len(tracker.snapshot("req-live-2", viewer_id="admin-1", now=_ts(43))[0]) == 1


def test_prune_expires_elapsed_never_viewed_links_even_under_capacity() -> None:
    from assistant_gateway.application.tool_setup import account_link_form_expired

    tracker = ProposalFollowUpTracker(finished_retention=timedelta(hours=1), max_tracked_requests=10)
    tracker.register(
        "req-never-viewed",
        approver_person_id="admin-1",
        links=[_link("session:s1", tool_name="Portainer", expires_at_utc=_ts(15))],
        now=_ts(0),
    )

    # Just after expiry (before retention elapses): prune transitions the link to expired outcome.
    tracker.prune(_ts(20))
    assert tracker.snapshot("req-never-viewed", viewer_id="admin-1", now=_ts(20)) == (
        [],
        [account_link_form_expired("Portainer")],
    )

    # Hours after expiry (past retention): prune drops the entry completely.
    tracker.prune(_ts(300))
    assert tracker.snapshot("req-never-viewed", viewer_id="admin-1", now=_ts(300)) == ([], [])


def test_parse_utc_and_form_key() -> None:
    assert parse_utc("2026-11-21T12:00:00Z") == _ts(0)
    assert parse_utc("not-a-timestamp") is None
    assert parse_utc(None) is None

    form = AccountLinkForm(
        session_id="sess-42",
        form_url="https://broker.example/link/42",
        status_url="https://broker.example/status/42",
        expires_at_utc="2026-11-21T12:15:00+00:00",
        tool_display_name="Portainer",
    )
    assert form_key(form, "http:portainer", "team") == "session:sess-42"
    assert form_key("plain-standin", "http:portainer", "team") == "tool:http:portainer:team:"

"""Sample Web Chat answers for the onboarding aesthetic preview.

``scripts/run_onboarding_aesthetic_preview.py`` serves Web Chat without a
gateway or manager. This module builds its sample answers for the Settings
tabs and Runtime views (Schedules, Memories, Tasks, Inbox, Accounts and
Access) through the gateway's own response models and payload helpers, so a
change to a real response format fails here (and in
``tests/test_onboarding_aesthetic_preview.py``) instead of leaving the
preview page broken. The samples describe one small household team; nothing
is stored and controls change nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import cache
from types import SimpleNamespace
from typing import Any, Literal

from access_catalog.catalog import load_catalog
from access_control.account_sharing import TeamAccountSharing
from access_control.entities import AGENT_ROLE_TEAM_LEAD, AGENT_ROLE_WORKER, role_id
from access_control.requests import LEVEL_WORDS, AccessRequests
from access_control.standard_grants import standard_grants

from assistant_gateway.account_sharing import MODEL_KEYS_NAME
from assistant_gateway.application.account_overview import (
    AccountsPage,
    AccountStatus,
    ServiceAccounts,
)
from assistant_gateway.application.website_application import (
    WebMemoryListV1,
    WebMemoryV1,
    WebScheduleListV1,
    WebScheduleV1,
    WebScheduleWorkerAssignmentV1,
    WebTaskListV1,
    WebTaskV1,
)
from assistant_gateway.access_inbox import GatewayAccessInbox
from assistant_gateway.access_map import (
    ROLE_WORDS,
    TARGET_KIND_WORDS,
    YOU,
    GatewayAccessMap,
    _page,
)
from assistant_gateway.interactive_apps.website_handlers.accounts import page_payload
from custom_messages.contracts.access_v2 import (
    AccessNoticeKind,
    AccessNoticeV1,
    AccountSharing,
    SubjectKind,
    SubjectRefV1,
)
from secure_tools.chat_commands import (
    ACCOUNTS_COMMAND,
    ACCOUNTS_MODEL_SUBJECT,
    ACCOUNTS_PRIVATE,
    ACCOUNTS_SHARE,
    link_command,
)

PREVIEW_OBJECTIVE_ID = "objective-preview-release"
PREVIEW_SCHEDULE_ID = "schedule-preview-release"
PREVIEW_SCHEDULE_DIGEST = "b" * 64
PREVIEW_TEAM_LEAD_ID = "preview-manager"
PREVIEW_TEAM_ID = "preview-team"
# The signed-in preview person (run_onboarding_aesthetic_preview.py).
PREVIEW_PERSON_ID = "usr_dev_preview"
# One page of a list, as the real routes send it (website_handlers/pagination.py).
PREVIEW_PAGE_SIZE = 20

MemoryScope = Literal["personal", "team"]
TaskView = Literal["active", "finished"]


def _ok(model_payload: dict[str, Any]) -> dict[str, Any]:
    """The real routes answer ``{"status": "ok", **payload}``."""

    return {"status": "ok", **model_payload}


def schedules(now: datetime) -> dict[str, Any]:
    """A person's reminder, a research brief, the objective's work sessions and the nightly memory review."""

    def schedule(
        schedule_id: str,
        name: str,
        *,
        management_class: str,
        next_fire_in: timedelta,
        assignment: WebScheduleWorkerAssignmentV1,
        digest: str,
        kind: str | None = None,
        objective_id: str | None = None,
    ) -> WebScheduleV1:
        return WebScheduleV1(
            schedule_id=schedule_id,
            record_digest=digest,
            schedule_name=name,
            objective_id=objective_id,
            management_class=management_class,
            system_schedule_kind=kind,
            enabled=True,
            next_fire_at=now + next_fire_in,
            worker_assignment=assignment,
            available_actions=("pause", "disable", "remove"),
        )

    listing = WebScheduleListV1(
        schedules=(
            schedule(
                "schedule-preview-groceries",
                "Friday grocery list reminder",
                management_class="user",
                next_fire_in=timedelta(days=2, hours=3),
                assignment=WebScheduleWorkerAssignmentV1(
                    agent_type="PersonalAdminAgent", agent_id=None, automatic_creation="default_policy"
                ),
                digest="e" * 64,
            ),
            schedule(
                "schedule-preview-news",
                "Weekday morning news brief",
                management_class="user",
                next_fire_in=timedelta(hours=14),
                assignment=WebScheduleWorkerAssignmentV1(
                    agent_type="WebResearchAgent", agent_id="preview-researcher", automatic_creation="default_policy"
                ),
                digest="f" * 64,
            ),
            schedule(
                PREVIEW_SCHEDULE_ID,
                "Continue release verification",
                management_class="system",
                kind="objective_work_session",
                objective_id=PREVIEW_OBJECTIVE_ID,
                next_fire_in=timedelta(minutes=45),
                assignment=WebScheduleWorkerAssignmentV1(
                    agent_type="TeamArchitect", agent_id=PREVIEW_TEAM_LEAD_ID, automatic_creation="not_configured"
                ),
                digest=PREVIEW_SCHEDULE_DIGEST,
            ),
            schedule(
                "schedule-preview-memory-review",
                "Daily personal memory review",
                management_class="system",
                kind="memory_review_usermem",
                next_fire_in=timedelta(hours=9),
                assignment=WebScheduleWorkerAssignmentV1(
                    agent_type=None,
                    agent_id=None,
                    automatic_creation="enabled",
                    review_agent_type="MemReviewAgent",
                ),
                digest="9" * 64,
            ),
        )
    )
    return _ok(listing.model_dump(mode="json"))


# (fact, tags, days since it was learned)
_PERSONAL_MEMORIES = (
    ("Likes apples, especially Honeycrisp.", ("food", "preferences"), 0),
    ("Prefers short answers with the summary first.", ("communication",), 3),
    ("Grocery shopping happens on Saturday mornings.", ("household", "routine"), 9),
)
_TEAM_MEMORIES = (
    ("Release notes use plain language and list breaking changes first.", ("release", "writing"), 5),
    ("Research briefs cite every source with a link.", ("research",), 12),
)


def memories(scope: MemoryScope, now: datetime) -> dict[str, Any]:
    """The Memories tab's personal or team list; agents learn these from chat."""

    rows = _PERSONAL_MEMORIES if scope == "personal" else _TEAM_MEMORIES
    items = tuple(
        WebMemoryV1(
            scope=scope,
            memory_id=index,
            fact_text=fact,
            tags=tags,
            valid_from_utc=now - timedelta(days=age_days),
            invalid_after_utc=None,
            created_at_utc=now - timedelta(days=age_days),
            updated_at_utc=now - timedelta(days=age_days),
            record_digest=f"{index:064x}",
            available_actions=("edit", "delete"),
        )
        for index, (fact, tags, age_days) in enumerate(rows, start=1)
    )
    listing = WebMemoryListV1(
        scope=scope,
        memories=items,
        offset=0,
        limit=PREVIEW_PAGE_SIZE,
        total=len(items),
        has_more=False,
        can_create=True,
    )
    return _ok(listing.model_dump(mode="json"))


def tasks(agent_id: str, view: TaskView, now: datetime) -> dict[str, Any]:
    """The Tasks view for one team member: the team lead has one task under way and one finished."""

    rows: tuple[WebTaskV1, ...] = ()
    if agent_id == PREVIEW_TEAM_LEAD_ID and view == "active":
        rows = (
            WebTaskV1(
                root_task_id="task-preview-weekend",
                state="in_progress",
                problem_statement="Plan this weekend's meals and add what we need to the grocery list.",
                is_current=True,
                queue_position=None,
                latest_activity_at=(now - timedelta(minutes=2)).isoformat(),
                latest_activity="Asked Atlas to check which recipes use apples.",
                blocker=None,
                owner_agent_id=PREVIEW_TEAM_LEAD_ID,
                owner_agent_name="Ava",
                answer=None,
            ),
        )
    elif agent_id == PREVIEW_TEAM_LEAD_ID:
        rows = (
            WebTaskV1(
                root_task_id="task-preview-news",
                state="completed",
                problem_statement="Summarize this morning's local news.",
                is_current=False,
                queue_position=None,
                latest_activity_at=(now - timedelta(hours=5)).isoformat(),
                latest_activity="Sent the summary.",
                blocker=None,
                owner_agent_id=PREVIEW_TEAM_LEAD_ID,
                owner_agent_name="Ava",
                answer="Three stories today: the farmers market moves indoors, roadwork on Main Street, and a library book sale.",
            ),
        )
    listing = WebTaskListV1(
        tasks=rows,
        total=len(rows),
        queued_count=0,
        current_count=sum(1 for row in rows if row.is_current),
        inventory_digest=f"{len(rows):064x}",
    )
    return _ok(listing.model_dump(mode="json"))


def inbox_counts() -> dict[str, Any]:
    """Nothing waits for a decision; one notice is unread (the Settings badge)."""

    return {"status": "ok", "decidable_requests": 0, "unread_notices": 1}


def inbox(now: datetime) -> dict[str, Any]:
    """The access Inbox: no requests to decide, and one notice about a model change."""

    notice = GatewayAccessInbox.notice_view(
        AccessNoticeV1(
            notice_id="notice-preview-model",
            team_id=PREVIEW_TEAM_ID,
            kind=AccessNoticeKind.MODEL_CHANGED,
            recipient_person_id=PREVIEW_PERSON_ID,
            title="Ava switched Atlas to a faster model",
            summary="Web Research Agent now uses a faster model for quick lookups. You can switch it back in chat.",
            created_at_utc=now - timedelta(hours=1),
        ),
        read=False,
    )
    return {
        "status": "ok",
        "reviews": [],
        "notices": [notice],
        "notices_offset": 0,
        "notices_total": 1,
        "notices_all_total": 1,
        "unread_notices": 1,
    }


def accounts() -> dict[str, Any]:
    """Whose account agents use for each service, for an administrator."""

    page = AccountsPage(
        services=(
            ServiceAccounts(
                subject=ACCOUNTS_MODEL_SUBJECT, name=MODEL_KEYS_NAME, sharing=AccountSharing.SHARED, fixed=False
            ),
            ServiceAccounts(
                subject="cli:github",
                name="GitHub",
                sharing=AccountSharing.PRIVATE,
                fixed=True,
                own_account=AccountStatus.SIGN_IN,
                setup_command=link_command("cli:github"),
                signs_in=True,
            ),
            ServiceAccounts(
                subject="cli:google",
                name="Google Workspace",
                sharing=AccountSharing.PRIVATE,
                fixed=True,
                own_account=AccountStatus.LINKED,
                setup_command=link_command("cli:google"),
                signs_in=True,
            ),
        ),
        can_change=True,
        share_command=f"{ACCOUNTS_COMMAND} {ACCOUNTS_SHARE}",
        private_command=f"{ACCOUNTS_COMMAND} {ACCOUNTS_PRIVATE}",
    )
    return _ok(page_payload(page))


class _NoSharingChoices:
    """An access store where no administrator has chosen any service's sharing, so each keeps its default."""

    def account_sharing(self, team_id: str, subject: str) -> None:
        return None

    def team_account_sharing(self, team_id: str) -> list[Any]:
        return []

    def set_account_sharing(self, record: Any) -> Any:
        raise NotImplementedError("The preview stores nothing.")


@cache
def _map_read() -> SimpleNamespace:
    """What the gateway's wording helpers read from one Access map read (``access_map._MapRead``).

    ``AccessRequests.target_name`` reads ``service.catalog`` and
    ``GatewayAccessMap._tool_view`` also reads ``sharing``; with no account
    links it never calls ``link_view``. Calling the real helpers keeps the
    preview's names and tool cards worded exactly as the Access tab words them.
    """

    return SimpleNamespace(
        service=SimpleNamespace(catalog=load_catalog()),
        sharing=TeamAccountSharing(_NoSharingChoices(), PREVIEW_TEAM_ID),
    )


@cache
def _standard_rows(role: str) -> tuple[dict[str, str], ...]:
    """The standard access a role brings, worded as the Access tab words it (``assistant_gateway.access_map``)."""

    holder = SubjectRefV1(kind=SubjectKind.ROLE, id=role_id(PREVIEW_TEAM_ID, role))
    rows = []
    for grant in standard_grants(PREVIEW_TEAM_ID):
        if grant.subject != holder:
            continue
        target = grant.target
        rows.append(
            {
                "target": target.id,
                "target_kind": target.kind.value,
                "target_kind_words": TARGET_KIND_WORDS[target.kind],
                "target_name": AccessRequests.target_name(_map_read(), target),  # type: ignore[arg-type]
                "level": grant.level.value,
                "level_words": LEVEL_WORDS[grant.level],
            }
        )
    return tuple(rows)


def _tool_page(offset: int) -> dict[str, Any]:
    """The Access tab's By Tool view: one page of the tool catalog, no account linked yet."""

    read = _map_read()
    catalog = read.service.catalog
    tools = sorted(catalog.tools.values(), key=lambda tool: (tool.display_name.lower(), tool.tool_id))
    page, meta = _page(tools, offset)
    return {**meta, "items": [GatewayAccessMap._tool_view(read, tool, []) for tool in page]}  # type: ignore[arg-type]


def access_map(view: str, offset: int = 0) -> dict[str, Any]:
    """The Access tab: each team member's role and the standard access it brings, and the tool catalog.

    No access has been given or blocked yet; the files view lists no shares
    and no data folders.
    """

    if view == "files":
        return {
            "status": "ok",
            "view": "files",
            "enabled": True,
            "notice": "",
            "shared_by_you": [],
            "shared_with_you": [],
            "data_folders": [],
            "can_manage_folders": True,
        }
    if view == "tools":
        return {"status": "ok", "view": "tools", "enabled": True, "notice": "", **_tool_page(offset)}
    paging = {"total": 0, "offset": 0, "page_size": PREVIEW_PAGE_SIZE, "has_more": False}
    if view == "people":
        return {
            "status": "ok",
            "view": "people",
            "enabled": True,
            "notice": "",
            "sees_everyone": True,
            **paging,
            "total": 1,
            "items": [{"person_id": PREVIEW_PERSON_ID, "title": YOU, "is_you": True, "given": []}],
        }
    members = (
        ("TeamArchitect", AGENT_ROLE_TEAM_LEAD),
        ("WebResearchAgent", AGENT_ROLE_WORKER),
        ("CodeWritingAgent", AGENT_ROLE_WORKER),
    )
    items = [
        {
            "key": f"{SubjectKind.AGENT_TYPE.value}:{agent_type}",
            "subject_kind": SubjectKind.AGENT_TYPE.value,
            "subject": agent_type,
            "title": agent_type,
            "role_words": ROLE_WORDS[role],
            "standard": list(_standard_rows(role)),
            "grants": [],
            "blocks": [],
            "can_block": False,
        }
        for agent_type, role in members
    ]
    return {
        "status": "ok",
        "view": "agents",
        "enabled": True,
        "notice": "",
        **paging,
        "total": len(items),
        "items": items,
        "block_tools": [],
        "starter_profile": {"current": "", "choices": []},
        "focus_key": "",
        "focus_missing": False,
    }


__all__ = [
    "PREVIEW_OBJECTIVE_ID",
    "PREVIEW_SCHEDULE_DIGEST",
    "PREVIEW_SCHEDULE_ID",
    "access_map",
    "accounts",
    "inbox",
    "inbox_counts",
    "memories",
    "schedules",
    "tasks",
]

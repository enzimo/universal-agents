"""Records exchanged between the knowledge service, store, tools and gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from knowledge_base.vocabulary import (
    ActorKind,
    ChangeKind,
    ContributionKind,
    ContributionStatus,
    KnowledgeSpace,
    NoteStatus,
    PageStatus,
)


@dataclass(frozen=True)
class KnowledgeActor:
    """Who is writing, and for whom.

    ``work_person_id`` is the person the work serves, from the verified signed
    context (agents) or the signed-in session (people); empty for system work.
    ``work_id`` is the person's request (signed context id), so limits count
    all agents working on one request together.
    """

    kind: ActorKind
    actor_id: str
    team_id: str
    agent_type: str = ""
    display_name: str = ""
    work_person_id: str = ""
    task_id: str = ""
    work_id: str = ""

    @property
    def is_agent(self) -> bool:
        return self.kind is ActorKind.AGENT

    @property
    def is_person(self) -> bool:
        return self.kind is ActorKind.PERSON


@dataclass(frozen=True)
class PageKey:
    """A page's place: team space, or one person's personal space."""

    team_id: str
    space: KnowledgeSpace
    owner_person_id: str
    slug: str

    def reference(self) -> str:
        return f"{self.space.value}/{self.slug}"


@dataclass(frozen=True)
class PageDraft:
    title: str
    page_type: str
    summary: str
    tags: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class KnowledgePage:
    page_id: str
    team_id: str
    space: KnowledgeSpace
    owner_person_id: str
    slug: str
    title: str
    page_type: str
    summary: str
    tags: tuple[str, ...]
    body: str
    status: PageStatus
    revision: int
    frozen_until_utc: Optional[str]
    created_at_utc: str
    updated_at_utc: str
    updated_by_kind: str
    updated_by_agent_type: str

    @property
    def key(self) -> PageKey:
        return PageKey(self.team_id, self.space, self.owner_person_id, self.slug)

    def reference(self) -> str:
        return self.key.reference()


@dataclass(frozen=True)
class KnowledgeRevision:
    revision_id: str
    page_id: str
    revision: int
    title: str
    page_type: str
    summary: str
    tags: tuple[str, ...]
    body: str
    status: PageStatus
    change_kind: ChangeKind
    change_summary: str
    actor_kind: str
    actor_id: str
    actor_agent_type: str
    work_person_id: str
    contribution_id: str
    created_at_utc: str


@dataclass(frozen=True)
class KnowledgeNote:
    note_id: str
    page_id: str
    team_id: str
    space: KnowledgeSpace
    owner_person_id: str
    body: str
    status: NoteStatus
    actor_kind: str
    actor_id: str
    actor_agent_type: str
    work_person_id: str
    created_at_utc: str


@dataclass(frozen=True)
class KnowledgeContribution:
    """A team-space change proposed from one person's work, private to that person until published."""

    contribution_id: str
    team_id: str
    owner_person_id: str
    kind: ContributionKind
    page_id: str
    slug: str
    base_revision: int
    title: str
    page_type: str
    summary: str
    tags: tuple[str, ...]
    body: str
    change_summary: str
    review_kinds: tuple[str, ...]
    status: ContributionStatus
    actor_kind: str
    actor_id: str
    actor_agent_type: str
    task_id: str
    work_id: str
    created_at_utc: str
    decided_at_utc: Optional[str]
    decided_by_kind: str
    decided_by_id: str
    decision_reason: str
    published_revision: int


@dataclass(frozen=True)
class AgentActivity:
    """One agent type's knowledge writes in a period, by outcome and refusal reason."""

    agent_type: str
    applied: int
    pending: int
    refused: dict[str, int]


@dataclass(frozen=True)
class RecentFreeze:
    """A page frozen in the report's period and its current freeze state. Names only, never page text."""

    reference: str
    title: str
    frozen_until_utc: Optional[str]  # None when lifted; a past time when expired
    after_edit_war: bool  # set by the store after an edit war; otherwise by a curator or a person


@dataclass(frozen=True)
class ActivityReport:
    """What the curator reviews: who wrote how much, busy pages, frozen pages. Ids and counts only."""

    since_utc: str
    by_agent_type: list[AgentActivity]
    busiest_pages: list[tuple[str, int]]  # (page reference, applied edits)
    frozen_pages: list[tuple[str, str]]  # (page reference, frozen until)
    recently_frozen: list[RecentFreeze] = field(default_factory=list)


@dataclass(frozen=True)
class PageProblems:
    """Deterministic checks of the team space (llm-wiki style lint)."""

    broken_links: list[tuple[str, str]]  # (linking page, missing target)
    stale_pages: list[tuple[str, str]]  # (page, last updated)
    pages_with_open_notes: list[tuple[str, int]]
    duplicate_pages: list[tuple[str, str]]  # (canonical page, exact duplicate)


@dataclass(frozen=True)
class SearchHit:
    page: KnowledgePage
    snippet: str


@dataclass(frozen=True)
class PageChange:
    """One change to a page, applied by the store in a single transaction."""

    key: PageKey
    base_revision: int  # 0 creates the page
    draft: PageDraft
    status: PageStatus
    change_kind: ChangeKind
    change_summary: str
    actor: KnowledgeActor
    link_targets: tuple[tuple[KnowledgeSpace, str], ...]
    fingerprint: str
    contribution_id: str = ""


@dataclass(frozen=True)
class KnowledgeWriteKey:
    """Trusted operation identity and exact input digest for a write that may be retried."""

    operation_id: str
    payload_digest: str


@dataclass(frozen=True)
class KnowledgeWriteReceipt:
    """A write's original outcome, committed with its page or proposed change.

    Receipts contain no page text. They belong to the review's verified person,
    survive process restarts, and are not transferred because reviews are not
    transferred either.
    """

    reference: str
    page_id: str = ""
    revision: int = 0
    contribution_id: str = ""


@dataclass(frozen=True)
class PageMerge:
    """One merge, applied by the store in a single transaction (``KnowledgeService.merge``).

    ``target`` is the target's new revision; ``source`` archives the source
    with its current text. Both carry ``ChangeKind.MERGE`` and history entries
    written by trusted code. The store also keeps the source's name as a
    redirect to the target, moves the source's open notes to the target, and
    points pending contributions for the source at the target.
    """

    source: PageChange
    target: PageChange
    refuse_frozen: bool  # agents' merges respect freezes; people's do not


@dataclass(eq=False)
class KnowledgeError(Exception):
    """A refused knowledge operation, explained for the agent or person.

    ``code`` is stable for callers; ``message`` says what did not happen and
    what to do instead; ``details`` carries ids and numbers, never content
    that a rule refused.
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "error_code": self.code, "message": self.message, "error": self.message, **self.details}


# Stable refusal codes.
CODE_INVALID = "invalid_input"
CODE_NOT_FOUND = "not_found"
CODE_CONFLICT = "revision_conflict"
CODE_EXISTS = "page_exists"
CODE_SENSITIVE = "sensitive_content"
CODE_REVIEW_REQUIRED = "review_required"
CODE_LIMIT = "limit_reached"
CODE_FROZEN = "page_frozen"
CODE_REPEAT = "repeats_recent_revision"
CODE_SCOPE = "personal_space_unavailable"
CODE_DISABLED = "knowledge_disabled"
CODE_NOT_PENDING = "contribution_not_pending"
CODE_MERGED = "page_merged"
CODE_WRITE_MISMATCH = "saved_write_mismatch"

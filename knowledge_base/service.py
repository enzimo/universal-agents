"""The knowledge service: trusted rules between callers and the store.

Callers (agent tools now; the curator and the gateway later) say who is acting
(``KnowledgeActor``, built from verified identity only) and what they want.
The service decides:

1. **Whose space.** ``personal/<slug>`` always means the actor's own person's
   space; there is no way to name another person's space. Work for no person
   (system work) has no personal space.
2. **Where a team change goes.** An agent working for a person never changes
   the team space directly: its change becomes a pending contribution, private
   to that person, until the curator (working for that person) or the person
   publishes it. System work and people editing in the gateway change the
   team space directly.
3. **What is never stored** (``knowledge_base.sensitive_content``): blocked
   content is refused everywhere; contact details, birth dates and text
   copied from the person's own memories are flagged for review on team-bound
   changes. System work cannot publish flagged content; a person must confirm
   it; a contribution carries the flags to its reviewer.
4. **Limits.** Agent writes carry the configured limits into the store, which
   checks them inside its write transaction. People's edits and publishing
   decisions carry none (sizes still apply).
5. **Merged names.** A merged page's name leads to the page it was merged
   into (``merge``): reading, noting, editing and linking by the old name reach
   the target, and nobody creates a new page under it, so old links never
   silently point at a different topic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional

from access_control.ownership import (
    PRIVATE_READ,
    PRIVATE_UPDATE,
    RESOURCE_KNOWLEDGE_CONTRIBUTION,
    private_access,
)
from id_generation import new_id
from knowledge_base.records import (
    ActivityReport,
    PageProblems,
    CODE_CONFLICT,
    CODE_DISABLED,
    CODE_EXISTS,
    CODE_INVALID,
    CODE_MERGED,
    CODE_NOT_FOUND,
    CODE_NOT_PENDING,
    CODE_REVIEW_REQUIRED,
    CODE_SCOPE,
    CODE_SENSITIVE,
    KnowledgeActor,
    KnowledgeContribution,
    KnowledgeError,
    KnowledgeNote,
    KnowledgePage,
    KnowledgeRevision,
    KnowledgeWriteKey,
    KnowledgeWriteReceipt,
    PageChange,
    PageDraft,
    PageKey,
    PageMerge,
    SearchHit,
)
from knowledge_base.sensitive_content import SensitiveContentRules
from knowledge_base.text import link_targets, overlaps, query_terms, slugify, text_fingerprint, written_links
from storage.knowledge_repository import utc_text
from knowledge_base.vocabulary import (
    CONTRIBUTION_ID_PREFIX,
    DEFAULT_PAGE_TYPE,
    ChangeKind,
    ContributionKind,
    ContributionStatus,
    KnowledgeSpace,
    NoteStatus,
    PageStatus,
    PageType,
    WriteAction,
)
from runtime_settings.knowledge import KnowledgeSettings, KnowledgeWriteLimits

if TYPE_CHECKING:
    from storage.knowledge_repository import KnowledgeRepository

PERSONAL_OVERLAP_KIND = "personal_memory_overlap"
# The team history entry of a published contribution, written by trusted code
# only (publish_contribution docstring).
PUBLISHED_AFTER_REVIEW: dict[ChangeKind, str] = {
    ChangeKind.CREATE: "New page published after review",
    ChangeKind.EDIT: "Edit published after review",
    ChangeKind.ARCHIVE: "Archived after review",
}
# The longest freeze a curator or person may set (a week).
MAX_FREEZE_MINUTES = 7 * 24 * 60
_PERSONAL_PAGES_COMPARED = 100
_NOTES_SHOWN = 5
_BACKLINKS_SHOWN = 20
_CONTRIBUTIONS_SHOWN = 20
# One page of the Web Chat Knowledge list (``browse``).
_BROWSE_PAGE_LIMIT = 50
# Links resolved for one shown page (``page_links``).
_LINKS_RESOLVED = 100

# Text of the person's own memories and personal pages, for the overlap check.
PersonalTexts = Callable[[str], Iterable[str]]


@dataclass(frozen=True)
class PageView:
    page: KnowledgePage
    revision: Optional[KnowledgeRevision]
    backlinks: list[KnowledgePage]
    open_notes: list[KnowledgeNote]
    pending_contributions: list[KnowledgeContribution]
    # The name the reader used when it was a merged page's name leading here.
    redirected_from: str = ""


@dataclass(frozen=True)
class PageLink:
    """One ``[[link]]`` in a page's text: as written, the page it opens, and whether that page exists."""

    written: str
    space: str
    slug: str
    exists: bool


@dataclass(frozen=True)
class MergeResult:
    """A merge as stored: the archived source and the target's new revision."""

    source: KnowledgePage
    target: KnowledgePage
    review_kinds: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class WriteResult:
    """What a write did: changed a page, added a note, or proposed a team change."""

    page: Optional[KnowledgePage] = None
    note: Optional[KnowledgeNote] = None
    contribution: Optional[KnowledgeContribution] = None
    review_kinds: tuple[str, ...] = field(default=())
    receipt: Optional[KnowledgeWriteReceipt] = None

    @property
    def pending(self) -> bool:
        return (
            self.contribution is not None and self.contribution.status is ContributionStatus.PENDING
        ) or (self.receipt is not None and bool(self.receipt.contribution_id))


@dataclass(frozen=True)
class DraftInput:
    """A write request's fields.

    Empty title, body and page_type keep the current values on an edit.
    Omitted summary and tags keep their values; an empty summary or tags clears them.
    """

    title: str = ""
    body: str = ""
    summary: Optional[str] = None
    page_type: str = ""
    tags: Optional[list[str]] = None


class KnowledgeService:
    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        settings: KnowledgeSettings,
        personal_texts: Optional[PersonalTexts] = None,
        logger: Optional[logging.Logger] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._limits = settings.limits
        self._rules = SensitiveContentRules.from_patterns(
            (pattern.name, pattern.pattern) for pattern in settings.sensitive_content.blocked_patterns
        )
        self._personal_texts = personal_texts
        self._logger = logger or logging.getLogger(__name__)
        self._clock = clock

    # -- references and spaces ---------------------------------------------------------

    def page_key(self, actor: KnowledgeActor, reference: str) -> PageKey:
        """``team/<slug>`` or ``personal/<slug>``; personal always means the actor's own person."""

        self._require_enabled()
        space_text, separator, name = (reference or "").strip().partition("/")
        space = _space(space_text) if separator else None
        slug = slugify(name)
        if space is None or not slug:
            raise KnowledgeError(
                CODE_INVALID,
                f"'{reference}' is not a page reference. Use team/<page-name> or personal/<page-name>, "
                "for example team/github-release-process.",
            )
        if space is KnowledgeSpace.PERSONAL:
            if not actor.work_person_id:
                raise KnowledgeError(
                    CODE_SCOPE,
                    "Personal pages exist only in work for a person; this work is for nobody. Use team/ pages.",
                )
            return PageKey(actor.team_id, space, actor.work_person_id, slug)
        return PageKey(actor.team_id, space, "", slug)

    def _readable_spaces(self, actor: KnowledgeActor, space: str) -> tuple[KnowledgeSpace, ...]:
        requested = (space or "all").strip().lower()
        if requested in ("", "all"):
            return (KnowledgeSpace.TEAM, KnowledgeSpace.PERSONAL) if actor.work_person_id else (KnowledgeSpace.TEAM,)
        chosen = _space(requested)
        if chosen is None:
            raise KnowledgeError(CODE_INVALID, "space must be all, team or personal.")
        if chosen is KnowledgeSpace.PERSONAL and not actor.work_person_id:
            raise KnowledgeError(CODE_SCOPE, "Personal pages exist only in work for a person; this work is for nobody.")
        return (chosen,)

    # -- reads -------------------------------------------------------------------------

    def search(
        self,
        actor: KnowledgeActor,
        *,
        query: str,
        space: str = "all",
        page_type: str = "",
        tag: str = "",
        limit: int = 0,
        offset: int = 0,
    ) -> list[SearchHit]:
        self._require_enabled()
        maximum = self._settings.search.max_results
        return self._repository.search(
            team_id=actor.team_id,
            reader_person_id=actor.work_person_id,
            spaces=self._readable_spaces(actor, space),
            terms=query_terms(query),
            page_type=_page_type(page_type) if page_type else "",
            tag=slugify(tag) if tag else "",
            limit=min(max(int(limit or maximum), 1), maximum),
            offset=max(int(offset or 0), 0),
        )

    def browse(
        self,
        actor: KnowledgeActor,
        *,
        query: str,
        space: str,
        limit: int,
        offset: int = 0,
        status: PageStatus = PageStatus.ACTIVE,
    ) -> tuple[list[SearchHit], int]:
        """One page of a person's page list in one space, and how many pages match in all.

        For the Web Chat Knowledge page, which pages 20 at a time with a total
        (``ui/DESIGN.md``); ``search`` stays bounded by ``search.max_results``
        for agents. Spaces follow the same rules as ``search``. Active pages
        are shown by default; archived pages can be listed separately to restore them.
        """

        self._require_enabled()
        spaces = self._readable_spaces(actor, space)
        terms = query_terms(query)
        hits = self._repository.search(
            team_id=actor.team_id,
            reader_person_id=actor.work_person_id,
            spaces=spaces,
            terms=terms,
            limit=min(max(int(limit), 1), _BROWSE_PAGE_LIMIT),
            offset=max(int(offset), 0),
            status=status,
        )
        total = self._repository.count_pages(
            team_id=actor.team_id, reader_person_id=actor.work_person_id, spaces=spaces, terms=terms, status=status
        )
        return hits, total

    def page_links(self, page: KnowledgePage, body: str) -> list[PageLink]:
        """Where each ``[[link]]`` in ``body`` (shown on ``page``) leads, for the Web Chat Knowledge page.

        The same rules as the stored links (``link_targets``): a bare name stays
        in the page's space and a team page never links into a personal space.
        A merged page's name counts as existing, because opening it reaches the
        page it was merged into. Only pages the reader of ``page`` may read are
        named: a personal link stays in ``page``'s own person's space.
        """

        links: list[PageLink] = []
        for written, target in written_links(body)[:_LINKS_RESOLVED]:
            resolved = link_targets(page.space, f"[[{target}]]")
            if not resolved:
                continue
            space, slug = resolved[0]
            key = PageKey(page.team_id, space, page.owner_person_id if space is KnowledgeSpace.PERSONAL else "", slug)
            exists = (
                self._repository.get_redirect_target(key) is not None or self._repository.get_page(key) is not None
            )
            links.append(PageLink(written=written, space=space.value, slug=slug, exists=exists))
        return links

    def read(self, actor: KnowledgeActor, reference: str, *, revision: int = 0) -> PageView:
        page, redirected_from = self._resolved_page(actor, reference)
        snapshot = None
        if revision and revision != page.revision:
            snapshot = self._repository.get_revision(page.page_id, int(revision))
            if snapshot is None:
                raise KnowledgeError(CODE_NOT_FOUND, f"{page.reference()} has no revision {revision}.")
        pending: list[KnowledgeContribution] = []
        if page.space is KnowledgeSpace.TEAM and actor.work_person_id:
            pending = self._repository.list_contributions(
                team_id=actor.team_id,
                owner_person_id=actor.work_person_id,
                status=ContributionStatus.PENDING,
                page_id=page.page_id,
                limit=_CONTRIBUTIONS_SHOWN,
            )
        return PageView(
            page=page,
            revision=snapshot,
            backlinks=self._repository.backlinks(page, reader_person_id=actor.work_person_id, limit=_BACKLINKS_SHOWN),
            open_notes=self._repository.list_notes(page.page_id, status=NoteStatus.OPEN, limit=_NOTES_SHOWN),
            pending_contributions=pending,
            redirected_from=redirected_from,
        )

    def history(self, actor: KnowledgeActor, reference: str, *, limit: int = 10) -> tuple[KnowledgePage, list[KnowledgeRevision]]:
        page = self._existing_page(actor, reference)
        return page, self._repository.list_revisions(page.page_id, limit=min(max(int(limit or 10), 1), 50))

    def _existing_page(self, actor: KnowledgeActor, reference: str) -> KnowledgePage:
        return self._resolved_page(actor, reference)[0]

    def _resolved_page(self, actor: KnowledgeActor, reference: str) -> tuple[KnowledgePage, str]:
        """The named page, following a merged page's name to its target; and that old name, if followed."""

        key = self.page_key(actor, reference)
        target = self._repository.get_redirect_target(key)
        if target is not None:
            return target, key.reference()
        page = self._repository.get_page(key)
        if page is None:
            raise KnowledgeError(
                CODE_NOT_FOUND,
                f"There is no page named {key.reference()}. Search with knowledge_search to find the right name.",
            )
        return page, ""

    # -- writes ------------------------------------------------------------------------

    def write(
        self,
        actor: KnowledgeActor,
        reference: str,
        draft: DraftInput,
        *,
        base_revision: int = 0,
        change_summary: str = "",
        confirm_review: bool = False,
        write_key: Optional[KnowledgeWriteKey] = None,
    ) -> WriteResult:
        """Create (``base_revision`` 0) or edit a page, or propose the change when it needs review."""

        self._require_enabled()
        receipt = self._write_receipt(actor, write_key)
        if receipt is not None:
            return WriteResult(receipt=receipt)
        try:
            return self._write_draft(
                actor, reference, draft, base_revision=base_revision, change_summary=change_summary,
                confirm_review=confirm_review, write_key=write_key,
            )
        except KnowledgeError:
            # Another identical call may commit after the initial receipt read
            # but before preflight sees its new page/revision. Recover its
            # outcome before asking the caller to change the original fields.
            receipt = self._write_receipt(actor, write_key)
            if receipt is not None:
                return WriteResult(receipt=receipt)
            raise

    def _write_draft(
        self,
        actor: KnowledgeActor,
        reference: str,
        draft: DraftInput,
        *,
        base_revision: int,
        change_summary: str,
        confirm_review: bool,
        write_key: Optional[KnowledgeWriteKey],
    ) -> WriteResult:
        """Validate and save the draft; the write dispatcher handles committed retries."""

        metadata = (reference, *(str(tag) for tag in draft.tags or ()))
        # Check original names before normalization can remove a credential's
        # identifying punctuation, and before reference errors can echo it.
        self._require_not_blocked(
            actor, metadata, page_id="", action=WriteAction.CREATE if not base_revision else WriteAction.EDIT
        )
        key = self._write_key(self.page_key(actor, reference), base_revision)
        current = self._repository.get_page(key)
        if base_revision == 0 and current is not None:
            raise KnowledgeError(
                CODE_EXISTS,
                f"{key.reference()} already exists (revision {current.revision}). "
                "Read it, then pass base_revision to edit it.",
                {"page_id": current.page_id, "current_revision": current.revision},
            )
        if base_revision and current is None:
            raise KnowledgeError(CODE_NOT_FOUND, f"There is no page named {key.reference()} to edit.")
        if current is not None and current.revision != base_revision:
            raise KnowledgeError(
                CODE_CONFLICT,
                f"{key.reference()} changed since you read it: it is at revision {current.revision}, not "
                f"{base_revision}. Read it again, merge your change, and save with the new revision.",
                {"page_id": current.page_id, "current_revision": current.revision},
            )
        page_draft = self._draft(draft, current)
        change_kind = ChangeKind.CREATE if current is None else (
            ChangeKind.RESTORE if current.status is PageStatus.ARCHIVED else ChangeKind.EDIT
        )
        # The change summary goes into the page's history, so it is checked like the text.
        review = self._check_content(
            actor,
            key,
            (*_page_texts(key, page_draft), *metadata, change_summary or ""),
            page_id=current.page_id if current else "",
            action=WriteAction.CREATE if current is None else WriteAction.EDIT,
        )
        if self._needs_contribution(actor, key):
            kind = ContributionKind.CREATE if current is None else ContributionKind.EDIT
            return self._propose(
                actor, key, kind, page_draft, current, base_revision, change_summary, review, write_key=write_key
            )
        self._require_review_confirmed(actor, review, confirm_review, page_id=current.page_id if current else "")
        page = self._repository.apply_page_change(
            self._change(key, base_revision, page_draft, PageStatus.ACTIVE, change_kind, change_summary, actor),
            limits=self._limits_for(actor),
            now=self._clock(),
            write_key=write_key,
        )
        receipt = self._write_receipt(actor, write_key)
        return WriteResult(page=page, review_kinds=review, receipt=receipt)

    def _write_receipt(
        self, actor: KnowledgeActor, write_key: Optional[KnowledgeWriteKey]
    ) -> Optional[KnowledgeWriteReceipt]:
        """Only trusted review writes have a receipt; ordinary writes keep their existing path."""

        return self._repository.get_write_receipt(actor, write_key) if write_key is not None else None

    def _write_key(self, key: PageKey, base_revision: int) -> PageKey:
        """An edit by a merged page's name changes its target; a new page never takes that name."""

        target = self._repository.get_redirect_target(key)
        if target is None:
            return key
        if base_revision:
            return target.key
        raise KnowledgeError(
            CODE_MERGED,
            f"{key.reference()} was merged into {target.reference()}, and its name now leads there. "
            f"Edit {target.reference()} instead, or choose another name for a new page.",
            {"page_id": target.page_id, "redirects_to": target.reference()},
        )

    def add_note(self, actor: KnowledgeActor, reference: str, note: str, *, confirm_review: bool = False) -> WriteResult:
        page = self._existing_page(actor, reference)
        text = (note or "").strip()
        if not text:
            raise KnowledgeError(CODE_INVALID, "The note is empty.")
        if len(text) > self._limits.max_note_chars:
            raise KnowledgeError(
                CODE_INVALID,
                f"The note has {len(text)} characters; the limit is {self._limits.max_note_chars}. Shorten it.",
            )
        review = self._check_content(actor, page.key, (text,), page_id=page.page_id, action=WriteAction.NOTE)
        if self._needs_contribution(actor, page.key):
            draft = PageDraft(title=page.title, page_type=page.page_type, summary="", tags=(), body=text)
            return self._propose(actor, page.key, ContributionKind.NOTE, draft, page, page.revision, "", review)
        self._require_review_confirmed(actor, review, confirm_review, page_id=page.page_id)
        stored = self._repository.add_note(
            page, body=text, actor=actor, limits=self._limits_for(actor), now=self._clock()
        )
        if stored.page_id != page.page_id:
            # The store follows a merge that committed after our initial read.
            page = self._existing_page(actor, page.reference())
        return WriteResult(page=page, note=stored, review_kinds=review)

    def archive(self, actor: KnowledgeActor, reference: str, *, base_revision: int, reason: str) -> WriteResult:
        page = self._existing_page(actor, reference)
        if page.status is PageStatus.ARCHIVED:
            raise KnowledgeError(CODE_INVALID, f"{page.reference()} is already archived.")
        if page.revision != base_revision:
            raise KnowledgeError(
                CODE_CONFLICT,
                f"{page.reference()} is at revision {page.revision}, not {base_revision}. Read it again first.",
                {"page_id": page.page_id, "current_revision": page.revision},
            )
        summary = (reason or "").strip()[: self._limits.max_summary_chars]
        if not summary:
            raise KnowledgeError(CODE_INVALID, "Say why the page should be archived.")
        # The reason goes into the page's history, so it is checked like page text.
        review = self._check_content(actor, page.key, (summary,), page_id=page.page_id, action=WriteAction.ARCHIVE)
        draft = PageDraft(page.title, page.page_type, page.summary, page.tags, page.body)
        if self._needs_contribution(actor, page.key):
            return self._propose(actor, page.key, ContributionKind.ARCHIVE, draft, page, base_revision, summary, review)
        self._require_review_confirmed(actor, review, False, page_id=page.page_id)
        changed = self._repository.apply_page_change(
            self._change(page.key, base_revision, draft, PageStatus.ARCHIVED, ChangeKind.ARCHIVE, summary, actor),
            limits=self._limits_for(actor),
            now=self._clock(),
        )
        return WriteResult(page=changed)

    # -- contributions -----------------------------------------------------------------

    def list_contributions(
        self, actor: KnowledgeActor, *, status: Optional[ContributionStatus] = ContributionStatus.PENDING, limit: int = 20
    ) -> list[KnowledgeContribution]:
        """The actor's own person's contributions; nobody lists another person's."""

        self._require_enabled()
        if not actor.work_person_id:
            return []
        return self._repository.list_contributions(
            team_id=actor.team_id,
            owner_person_id=actor.work_person_id,
            status=status,
            limit=min(max(int(limit or 20), 1), 100),
        )

    def browse_contributions(
        self, actor: KnowledgeActor, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[KnowledgeContribution], int]:
        """One page of the actor's own pending contributions, with the full total.

        Pagination happens in the store so older proposals remain reachable
        when a person has more than one curator batch waiting for review.
        """

        self._require_enabled()
        if not actor.work_person_id:
            return [], 0
        contributions = self._repository.list_contributions(
            team_id=actor.team_id,
            owner_person_id=actor.work_person_id,
            status=ContributionStatus.PENDING,
            limit=min(max(int(limit), 1), _BROWSE_PAGE_LIMIT),
            offset=max(int(offset), 0),
        )
        return contributions, self.count_pending_contributions(actor)

    def count_pending_contributions(self, actor: KnowledgeActor) -> int:
        """How many of the actor's own person's contributions still wait (0 for work for nobody)."""

        self._require_enabled()
        if not actor.work_person_id:
            return 0
        return self._repository.count_contributions(
            team_id=actor.team_id, owner_person_id=actor.work_person_id, status=ContributionStatus.PENDING
        )

    def get_contribution(self, actor: KnowledgeActor, contribution_id: str, *, action: str = PRIVATE_READ) -> KnowledgeContribution:
        self._require_enabled()
        contribution = self._repository.get_contribution(contribution_id)
        if contribution is None or not private_access(
            person_id=actor.work_person_id or None,
            owner_id=contribution.owner_person_id,
            resource_type=RESOURCE_KNOWLEDGE_CONTRIBUTION,
            resource_id=contribution_id,
            action=action,
        ).allowed:
            # The same answer for "missing" and "someone else's", so ids reveal nothing.
            raise KnowledgeError(CODE_NOT_FOUND, f"There is no contribution {contribution_id} for this person.")
        return contribution

    def publish_contribution(
        self,
        actor: KnowledgeActor,
        contribution_id: str,
        *,
        draft: Optional[DraftInput] = None,
        base_revision: int = 0,
        note: Optional[str] = None,
        page: str = "",
    ) -> WriteResult:
        """Publish one pending contribution to the team space, optionally as an edited version.

        Only in work for the contribution's person (the curator's run for that
        person) or by that person. Blocked content is refused again; review
        flags are the reviewer's decision.

        Only reviewed page content reaches the team, and nothing written about
        the review: the team history gets a fixed entry from trusted code
        (``PUBLISHED_AFTER_REVIEW``), never the proposal's own change summary
        or the reviewer's description of it (live privacy tests 2026-10-10:
        reviewers' summaries said which kinds of private detail they removed).
        A proposed new page is published under the reviewer's ``page`` name,
        or else under a name made from its reviewed title, never under the
        proposed name unless the reviewer repeats it.

        An omitted note publishes the proposed text; an explicitly empty note
        is refused. An archive changes only status and keeps the current page's
        content, guarded by the reviewer's base revision. Edit fields are refused
        for an archive so publishing cannot replace newer text with its old snapshot.
        """

        contribution = self._pending(actor, contribution_id)
        if contribution.kind is ContributionKind.ARCHIVE and draft is not None:
            raise KnowledgeError(
                CODE_INVALID, "An archive changes only the page's status. Publish without edited page fields."
            )
        proposed = DraftInput(
            title=contribution.title,
            body=contribution.body,
            summary=contribution.summary,
            page_type=contribution.page_type,
            tags=list(contribution.tags),
        )
        chosen = _merge_inputs(proposed, draft)
        key = PageKey(contribution.team_id, KnowledgeSpace.TEAM, "", contribution.slug)
        raw_reference = key.reference()
        if page and contribution.kind is not ContributionKind.CREATE:
            raise KnowledgeError(CODE_INVALID, "Only a proposed new page can be published under another name.")
        if contribution.kind is ContributionKind.CREATE:
            # A title is always a name, even when it contains a slash. Only an
            # explicit page argument can name a space.
            raw_reference = _team_reference(page) if page else f"{KnowledgeSpace.TEAM.value}/{chosen.title}"
        metadata = (raw_reference, *(str(tag) for tag in chosen.tags or ()))
        self._require_not_blocked(actor, metadata, page_id=contribution.page_id)
        if contribution.kind is ContributionKind.CREATE:
            key = self.page_key(actor, raw_reference)
            if key.space is not KnowledgeSpace.TEAM:
                raise KnowledgeError(CODE_INVALID, "A contribution is published to a team page.")
        current = self._repository.get_page(key)
        if contribution.kind is ContributionKind.NOTE:
            if current is None:
                raise KnowledgeError(CODE_NOT_FOUND, f"{key.reference()} no longer exists; reject this note.")
            text = (contribution.body if note is None else note).strip()
            if not text:
                raise KnowledgeError(CODE_INVALID, "The note is empty.")
            if len(text) > self._limits.max_note_chars:
                raise KnowledgeError(
                    CODE_INVALID,
                    f"The note has {len(text)} characters; the limit is {self._limits.max_note_chars}. Shorten it.",
                )
            self._require_not_blocked(actor, (text,), page_id=current.page_id, action=WriteAction.NOTE)
            stored = self._repository.add_note(
                current, body=text, actor=actor, limits=None, now=self._clock(), contribution_id=contribution_id
            )
            if stored.page_id != current.page_id:
                current = self._existing_page(actor, current.reference())
            return WriteResult(page=current, note=stored)
        status = PageStatus.ARCHIVED if contribution.kind is ContributionKind.ARCHIVE else PageStatus.ACTIVE
        # A proposed new page whose name someone took meanwhile is published as an
        # edit when the reviewer names the revision they merged it into.
        if contribution.kind is ContributionKind.CREATE and not (current is not None and base_revision):
            revision = 0
            kind = ChangeKind.CREATE
        else:
            if current is None:
                raise KnowledgeError(CODE_NOT_FOUND, f"{key.reference()} no longer exists; reject this contribution.")
            revision = int(base_revision or contribution.base_revision)
            kind = ChangeKind.ARCHIVE if contribution.kind is ContributionKind.ARCHIVE else ChangeKind.EDIT
        if kind is ChangeKind.ARCHIVE:
            assert current is not None
            # The proposal's text is a review snapshot, never an archive edit.
            # A future revision must not become valid after we copy older text.
            # The store checks again inside its transaction for later writers.
            if current.revision != revision:
                raise KnowledgeError(
                    CODE_CONFLICT,
                    f"{key.reference()} is at revision {current.revision}, not {revision}. Read it again first.",
                    {"page_id": current.page_id, "current_revision": current.revision},
                )
            page_draft = PageDraft(current.title, current.page_type, current.summary, current.tags, current.body)
        else:
            page_draft = self._draft(chosen, current if kind is not ChangeKind.CREATE else None)
        summary = PUBLISHED_AFTER_REVIEW[kind]
        self._require_not_blocked(
            actor, _page_texts(key, page_draft), page_id=current.page_id if current else ""
        )
        published = self._repository.apply_page_change(
            self._change(key, revision, page_draft, status, kind, summary, actor, contribution_id=contribution_id),
            limits=None,
            now=self._clock(),
        )
        return WriteResult(page=published)

    def reject_contribution(self, actor: KnowledgeActor, contribution_id: str, *, reason: str) -> KnowledgeContribution:
        contribution = self._pending(actor, contribution_id)
        text = (reason or "").strip()
        if not text:
            raise KnowledgeError(CODE_INVALID, "Say why the contribution is rejected.")
        self._require_not_blocked(actor, (text,), page_id=contribution.page_id, action=WriteAction.DECIDE)
        return self._repository.decide_contribution(
            contribution_id, status=ContributionStatus.REJECTED, actor=actor, reason=text, now=self._clock()
        )

    def withdraw_contribution(self, actor: KnowledgeActor, contribution_id: str) -> KnowledgeContribution:
        self._pending(actor, contribution_id)
        return self._repository.decide_contribution(
            contribution_id, status=ContributionStatus.WITHDRAWN, actor=actor, reason="withdrawn", now=self._clock()
        )

    def _pending(self, actor: KnowledgeActor, contribution_id: str) -> KnowledgeContribution:
        contribution = self.get_contribution(actor, contribution_id, action=PRIVATE_UPDATE)
        if contribution.status is not ContributionStatus.PENDING:
            raise KnowledgeError(
                CODE_NOT_PENDING, f"Contribution {contribution_id} is already {contribution.status.value}."
            )
        return contribution

    # -- curation ----------------------------------------------------------------------
    #
    # Tools under fn:knowledge_curation (tool set knowledge_review: the curator
    # and people, never the team lead). Decisions carry no write limits: the
    # curator is the guard the limits report to.

    def revert(
        self, actor: KnowledgeActor, reference: str, *, to_revision: int, base_revision: int, reason: str
    ) -> WriteResult:
        """Make an earlier revision current again, as a new revision."""

        page = self._existing_page(actor, reference)
        if self._needs_contribution(actor, page.key):
            raise KnowledgeError(
                CODE_SCOPE,
                "In work for a person, propose team changes with knowledge_write. "
                "Direct team reverts belong to the curator's team maintenance run.",
            )
        snapshot = self._repository.get_revision(page.page_id, int(to_revision))
        if snapshot is None or snapshot.revision >= page.revision:
            raise KnowledgeError(CODE_INVALID, f"{page.reference()} has no earlier revision {to_revision}.")
        summary = " ".join((reason or "").split())[: self._limits.max_summary_chars]
        if not summary:
            raise KnowledgeError(CODE_INVALID, "Say why the page is reverted.")
        draft = PageDraft(snapshot.title, snapshot.page_type, snapshot.summary, snapshot.tags, snapshot.body)
        self._require_not_blocked(actor, (draft.title, draft.summary, draft.body, summary), page_id=page.page_id)
        # A review explanation may contain private facts. Team history records
        # only the restored revision; personal history may keep its own reason.
        history_summary = f"Reverted to revision {snapshot.revision}"
        if page.space is KnowledgeSpace.PERSONAL:
            history_summary += f": {summary}"
        changed = self._repository.apply_page_change(
            self._change(
                page.key, base_revision, draft, PageStatus.ACTIVE, ChangeKind.REVERT,
                history_summary, actor,
            ),
            limits=None,
            now=self._clock(),
        )
        return WriteResult(page=changed)

    def merge(
        self,
        actor: KnowledgeActor,
        source_reference: str,
        target_reference: str,
        draft: DraftInput,
        *,
        base_revision: int,
        source_revision: int,
        confirm_review: bool = False,
    ) -> MergeResult:
        """Merge the source page into the target: one page, and the source's name leads to it.

        ``draft.body`` is the merged text of both pages; empty title and
        summary keep the target's. Both pages are in the same space. Work for
        a person merges only that person's own pages: team pages are merged
        by the team run for nobody or by people, the same rule as ``revert``.
        Both base revisions are checked, so neither page's newer text is lost.

        In one transaction the target gets the merged text as a new revision,
        the source is archived with every revision kept, and the source's
        name becomes a redirect to the target (``knowledge_redirects``). Both
        history entries are fixed text from trusted code naming the other
        page, never the caller's words (the ``PUBLISHED_AFTER_REVIEW`` rule).
        Open notes on the source move to the target, and pending contributions
        for the source are pointed at the target. Agents cannot merge a frozen
        page; a merge is ordinary content, so a freeze applies to it.
        """

        self._require_enabled()
        self._require_not_blocked(actor, (source_reference, target_reference), page_id="", action=WriteAction.MERGE)
        source = self._existing_page(actor, source_reference)
        target = self._existing_page(actor, target_reference)
        self._require_mergeable(actor, source, target)
        for page, revision in ((target, base_revision), (source, source_revision)):
            if page.revision != int(revision):
                raise KnowledgeError(
                    CODE_CONFLICT,
                    f"{page.reference()} is at revision {page.revision}, not {revision}. Read both pages again "
                    "and merge their current text.",
                    {"page_id": page.page_id, "current_revision": page.revision},
                )
        if not (draft.body or "").strip():
            raise KnowledgeError(CODE_INVALID, "Send the merged text of both pages as body.")
        merged = self._draft(DraftInput(title=draft.title, body=draft.body, summary=draft.summary), target)
        review = self._check_content(
            actor, target.key, _page_texts(target.key, merged), page_id=target.page_id, action=WriteAction.MERGE
        )
        self._require_review_confirmed(actor, review, confirm_review, page_id=target.page_id)
        kept = PageDraft(source.title, source.page_type, source.summary, source.tags, source.body)
        source_entry = f"Merged into {merged.title} ({target.reference()})"
        target_entry = f"Merged {source.title} ({source.reference()}) into this page"
        stored_source, stored_target = self._repository.merge_pages(
            PageMerge(
                source=self._change(
                    source.key, source.revision, kept, PageStatus.ARCHIVED, ChangeKind.MERGE, source_entry, actor
                ),
                target=self._change(
                    target.key, target.revision, merged, PageStatus.ACTIVE, ChangeKind.MERGE, target_entry, actor
                ),
                refuse_frozen=actor.is_agent,
            ),
            now=self._clock(),
        )
        return MergeResult(source=stored_source, target=stored_target, review_kinds=review)

    def _require_mergeable(self, actor: KnowledgeActor, source: KnowledgePage, target: KnowledgePage) -> None:
        if source.page_id == target.page_id:
            raise KnowledgeError(CODE_INVALID, f"{source.reference()} and {target.reference()} are the same page.")
        if source.space is not target.space:
            raise KnowledgeError(
                CODE_INVALID,
                "Both pages must be in the same space: merge team pages into team pages and personal pages into "
                "personal pages.",
            )
        if self._needs_contribution(actor, target.key):
            raise KnowledgeError(
                CODE_SCOPE,
                "In work for a person, only that person's own pages can be merged. "
                "Team pages are merged by the curator's team maintenance run.",
            )
        for page in (source, target):
            if page.status is PageStatus.ARCHIVED:
                raise KnowledgeError(
                    CODE_INVALID,
                    f"{page.reference()} is archived; only active pages are merged. Restore it with an edit first "
                    "if it should stay.",
                    {"page_id": page.page_id},
                )

    def freeze(self, actor: KnowledgeActor, reference: str, *, minutes: int) -> KnowledgePage:
        """Stop agent edits to a page for ``minutes`` (1 to a week); notes still work. ``unfreeze`` lifts it."""

        page = self._existing_page(actor, reference)
        if int(minutes) < 1:
            raise KnowledgeError(CODE_INVALID, "A freeze lasts at least one minute. Use unfreeze to lift a freeze.")
        until = utc_text(self._clock() + timedelta(minutes=min(int(minutes), MAX_FREEZE_MINUTES)))
        return self._set_freeze(actor, page, until)

    def unfreeze(self, actor: KnowledgeActor, reference: str) -> KnowledgePage:
        """Let agents edit a frozen page again, for example once the pages they fought over are settled."""

        page = self._existing_page(actor, reference)
        if not page.frozen_until_utc or page.frozen_until_utc <= utc_text(self._clock()):
            raise KnowledgeError(CODE_INVALID, f"{page.reference()} is not frozen.", {"page_id": page.page_id})
        return self._set_freeze(actor, page, None)

    def _set_freeze(self, actor: KnowledgeActor, page: KnowledgePage, until: Optional[str]) -> KnowledgePage:
        self._repository.set_frozen_until(page.page_id, until, actor=actor, now=self._clock())
        self._logger.info(
            "Knowledge page freeze changed. event=%s page_id=%s frozen_until=%s actor_kind=%s actor_id=%s",
            "knowledge_page_freeze_set" if until else "knowledge_page_freeze_lifted",
            page.page_id,
            until,
            actor.kind.value,
            actor.actor_id,
        )
        refreshed = self._repository.get_page_by_id(page.page_id)
        assert refreshed is not None
        return refreshed

    def open_notes(self, actor: KnowledgeActor, *, space: str = "team", limit: int = 20) -> list[KnowledgeNote]:
        """Open notes in the team space, or in the actor's own person's space."""

        chosen = self._readable_spaces(actor, space)
        if len(chosen) != 1:
            raise KnowledgeError(CODE_INVALID, "space must be team or personal.")
        owner = actor.work_person_id if chosen[0] is KnowledgeSpace.PERSONAL else ""
        return self._repository.list_open_notes(
            team_id=actor.team_id, space=chosen[0], owner_person_id=owner, limit=min(max(int(limit or 20), 1), 100)
        )

    def page_references(self, page_ids: Iterable[str]) -> dict[str, str]:
        """Page names for page ids (notes carry only their page's id)."""

        references: dict[str, str] = {}
        for page_id in dict.fromkeys(page_ids):
            page = self._repository.get_page_by_id(page_id)
            references[page_id] = page.reference() if page is not None else ""
        return references

    def resolve_note(self, actor: KnowledgeActor, note_id: str, *, merged: bool) -> None:
        notes = {note.note_id: note for note in self.open_notes(actor, space="team", limit=100)}
        if actor.work_person_id:
            notes.update({note.note_id: note for note in self.open_notes(actor, space="personal", limit=100)})
        if note_id not in notes:
            raise KnowledgeError(CODE_NOT_FOUND, f"There is no open note {note_id} you can resolve.")
        status = NoteStatus.RESOLVED if merged else NoteStatus.DISMISSED
        self._repository.resolve_note(note_id, team_id=actor.team_id, status=status, actor=actor, now=self._clock())

    def activity(self, actor: KnowledgeActor, *, hours: int = 24) -> ActivityReport:
        self._require_enabled()
        now = self._clock()
        return self._repository.activity(
            team_id=actor.team_id, since=now - timedelta(hours=min(max(int(hours or 24), 1), 24 * 30)), now=now, limit=20
        )

    def problems(self, actor: KnowledgeActor, *, stale_days: int = 180) -> PageProblems:
        self._require_enabled()
        stale_before = self._clock() - timedelta(days=min(max(int(stale_days or 180), 1), 3650))
        return self._repository.page_problems(team_id=actor.team_id, stale_before=stale_before, limit=50)

    # -- rules -------------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self._settings.enabled:
            raise KnowledgeError(CODE_DISABLED, "The knowledge base is turned off (agent.knowledge.enabled).")

    @staticmethod
    def _needs_contribution(actor: KnowledgeActor, key: PageKey) -> bool:
        """An agent working for a person proposes team changes; it never makes them."""

        return key.space is KnowledgeSpace.TEAM and actor.is_agent and bool(actor.work_person_id)

    def _limits_for(self, actor: KnowledgeActor) -> Optional[KnowledgeWriteLimits]:
        return self._limits if actor.is_agent else None

    def _check_content(
        self,
        actor: KnowledgeActor,
        key: PageKey,
        texts: tuple[str, ...],
        *,
        page_id: str,
        action: WriteAction = WriteAction.EDIT,
    ) -> tuple[str, ...]:
        """Refuse blocked content; return review flags for team-bound text."""

        scan = self._require_not_blocked(actor, texts, page_id=page_id, action=action)
        if key.space is not KnowledgeSpace.TEAM:
            return ()
        kinds = set(scan.review_kinds())
        if actor.work_person_id and self._personal_overlap(actor, texts):
            kinds.add(PERSONAL_OVERLAP_KIND)
        return tuple(sorted(kinds))

    def _require_not_blocked(
        self, actor: KnowledgeActor, texts: tuple[str, ...], *, page_id: str, action: WriteAction = WriteAction.EDIT
    ) -> Any:
        scan = self._rules.scan(*texts)
        if scan.is_blocked:
            self._logger.warning(
                "Knowledge write refused for sensitive content. event=knowledge_sensitive_content_refused "
                "kinds=%s actor_kind=%s actor_id=%s agent_type=%s team_id=%s task_id=%s page_id=%s",
                sorted({finding.kind for finding in scan.blocked}),
                actor.kind.value,
                actor.actor_id,
                actor.agent_type,
                actor.team_id,
                actor.task_id,
                page_id,
            )
            self._repository.record_refusal(
                actor, action=action, page_id=page_id, reason=CODE_SENSITIVE, now=self._clock()
            )
            raise KnowledgeError(
                CODE_SENSITIVE,
                scan.blocked_message(),
                {"found": [{"kind": f.kind, "line": f.line, "column": f.column} for f in scan.blocked[:10]]},
            )
        return scan

    def _personal_overlap(self, actor: KnowledgeActor, texts: tuple[str, ...]) -> bool:
        candidate = "\n".join(texts)
        private: list[str] = list(self._personal_texts(actor.work_person_id)) if self._personal_texts else []
        private.extend(
            hit.page.body
            for hit in self._repository.search(
                team_id=actor.team_id,
                reader_person_id=actor.work_person_id,
                spaces=(KnowledgeSpace.PERSONAL,),
                terms=[],
                limit=_PERSONAL_PAGES_COMPARED,
            )
        )
        return any(overlaps(candidate, text) for text in private if text)

    def _require_review_confirmed(
        self, actor: KnowledgeActor, review: tuple[str, ...], confirmed: bool, *, page_id: str
    ) -> None:
        """Direct team changes with flagged content: a person confirms; system work cannot."""

        if not review or (actor.is_person and confirmed):
            return
        self._repository.record_refusal(
            actor, action=WriteAction.EDIT, page_id=page_id, reason=CODE_REVIEW_REQUIRED, now=self._clock()
        )
        if actor.is_person:
            message = (
                "This team page change contains " + ", ".join(_kind_label(k) for k in review)
                + ". Everyone on the team will see it. Confirm to save it anyway, or remove those details."
            )
        else:
            message = (
                "Work for nobody cannot put " + ", ".join(_kind_label(k) for k in review)
                + " on team pages. Remove those details and save again."
            )
        raise KnowledgeError(CODE_REVIEW_REQUIRED, message, {"review_kinds": list(review)})

    def _draft(self, draft: DraftInput, current: Optional[KnowledgePage]) -> PageDraft:
        limits = self._limits
        title = " ".join((draft.title or (current.title if current else "")).split())
        body = (draft.body or (current.body if current else "")).strip()
        raw_summary = draft.summary if draft.summary is not None else (current.summary if current else "")
        summary = " ".join(raw_summary.split())
        if not title:
            raise KnowledgeError(CODE_INVALID, "A new page needs a title.")
        if not body:
            raise KnowledgeError(CODE_INVALID, "A page needs text in its body.")
        for label, value, maximum in (
            ("title", title, limits.max_title_chars),
            ("summary", summary, limits.max_summary_chars),
            ("body", body, limits.max_body_chars),
        ):
            if len(value) > maximum:
                raise KnowledgeError(
                    CODE_INVALID,
                    f"The {label} has {len(value)} characters; the limit is {maximum}. "
                    + ("Split the page into smaller linked pages." if label == "body" else "Shorten it."),
                )
        raw_tags = draft.tags if draft.tags is not None else (list(current.tags) if current else [])
        tags = tuple(dict.fromkeys(tag for tag in (slugify(str(t)) for t in raw_tags) if tag))
        if len(tags) > limits.max_tags:
            raise KnowledgeError(CODE_INVALID, f"A page may have at most {limits.max_tags} tags.")
        page_type = _page_type(draft.page_type) if draft.page_type else (current.page_type if current else DEFAULT_PAGE_TYPE.value)
        return PageDraft(title=title, page_type=page_type, summary=summary, tags=tags, body=body)

    def _change(
        self,
        key: PageKey,
        base_revision: int,
        draft: PageDraft,
        status: PageStatus,
        change_kind: ChangeKind,
        change_summary: str,
        actor: KnowledgeActor,
        *,
        contribution_id: str = "",
    ) -> PageChange:
        return PageChange(
            key=key,
            base_revision=int(base_revision),
            draft=draft,
            status=status,
            change_kind=change_kind,
            change_summary=" ".join((change_summary or "").split())[: self._limits.max_summary_chars],
            actor=actor,
            link_targets=link_targets(key.space, draft.body),
            fingerprint=text_fingerprint(draft.title, draft.page_type, draft.summary, " ".join(draft.tags), draft.body, status.value),
            contribution_id=contribution_id,
        )

    def _propose(
        self,
        actor: KnowledgeActor,
        key: PageKey,
        kind: ContributionKind,
        draft: PageDraft,
        current: Optional[KnowledgePage],
        base_revision: int,
        change_summary: str,
        review: tuple[str, ...],
        *,
        write_key: Optional[KnowledgeWriteKey] = None,
    ) -> WriteResult:
        contribution = KnowledgeContribution(
            contribution_id=new_id(CONTRIBUTION_ID_PREFIX),
            team_id=key.team_id,
            owner_person_id=actor.work_person_id,
            kind=kind,
            page_id=current.page_id if current else "",
            slug=key.slug,
            base_revision=int(base_revision),
            title=draft.title,
            page_type=draft.page_type,
            summary=draft.summary,
            tags=draft.tags,
            body=draft.body,
            change_summary=" ".join((change_summary or "").split())[: self._limits.max_summary_chars],
            review_kinds=review,
            status=ContributionStatus.PENDING,
            actor_kind=actor.kind.value,
            actor_id=actor.actor_id,
            actor_agent_type=actor.agent_type,
            task_id=actor.task_id,
            work_id=actor.work_id,
            created_at_utc="",
            decided_at_utc=None,
            decided_by_kind="",
            decided_by_id="",
            decision_reason="",
            published_revision=0,
        )
        stored = self._repository.add_contribution(
            contribution, limits=self._limits_for(actor), now=self._clock(), write_key=write_key
        )
        receipt = self._write_receipt(actor, write_key)
        return WriteResult(page=current, contribution=stored, review_kinds=review, receipt=receipt)


def _space(value: str) -> Optional[KnowledgeSpace]:
    try:
        return KnowledgeSpace((value or "").strip().lower())
    except ValueError:
        return None


def _page_type(value: str) -> str:
    try:
        return PageType((value or "").strip().lower()).value
    except ValueError as exc:
        raise KnowledgeError(
            CODE_INVALID, f"page_type must be one of: {', '.join(t.value for t in PageType)}."
        ) from exc


def _page_texts(key: PageKey, draft: PageDraft) -> tuple[str, ...]:
    """Every stored page text field, including normalized names and tags.

    Both direct writes and publication scan these fields so adding metadata
    cannot provide a second way to store prohibited content.
    """

    return (key.slug, draft.title, draft.summary, draft.body, *draft.tags)


def _team_reference(name: str) -> str:
    """An explicit page reference, with ``team/`` optional for a bare name."""

    text = (name or "").strip()
    return text if text.startswith(f"{KnowledgeSpace.TEAM.value}/") or "/" in text else f"{KnowledgeSpace.TEAM.value}/{text}"


def _merge_inputs(proposed: DraftInput, override: Optional[DraftInput]) -> DraftInput:
    if override is None:
        return proposed
    return DraftInput(
        title=override.title or proposed.title,
        body=override.body or proposed.body,
        summary=override.summary if override.summary is not None else proposed.summary,
        page_type=override.page_type or proposed.page_type,
        tags=override.tags if override.tags is not None else proposed.tags,
    )


_KIND_LABELS = {
    "email_address": "email addresses",
    "phone_number": "phone numbers",
    "street_address": "street addresses",
    "birth_date": "birth dates",
    PERSONAL_OVERLAP_KIND: "text from the person's own memories or personal pages",
}


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind.replace("_", " "))


__all__ = [
    "DraftInput",
    "KnowledgeService",
    "MAX_FREEZE_MINUTES",
    "MergeResult",
    "PERSONAL_OVERLAP_KIND",
    "PageLink",
    "PageView",
    "WriteResult",
]

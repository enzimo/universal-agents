"""People's knowledge base commands at the team's address: the Web Chat Knowledge page.

``knowledge.list``, ``.read``, ``.history``, ``.write``, ``.add_note``, ``.archive``,
``.revert``, ``.list_contributions``, ``.count_contributions``, ``.read_contribution``,
``.publish_contribution`` and ``.withdraw_contribution``. Until phase 2 the gateway sent
these to the team lead's process as a durable ``ManagerKnowledgeCommand``; they now run
here, with no model, on the same database and the same service
(``knowledge_base.service``), so the rules are unchanged:

- **Who acts** comes from the command's authority token only (``team_services.people``).
  The service runs as that person (``ActorKind.PERSON``, ``work_person_id`` the same
  person), so ``personal/`` is always their own space.
- **What they may touch** is decided by ``access_control.knowledge_access``: every team
  member reads and edits team pages; personal pages and pending proposals are the
  person's own.
- **What the service decides.** A person's team edit publishes directly and is not rate
  limited; the never-stored rules apply to every write, and content held for review
  needs the person's explicit confirmation (``confirm_review``). Archive and revert
  reasons are fixed texts from the gateway's code, never typed in the browser.
- **Editors are named by reference.** A person who changed something is named by their
  opaque person reference; the gateway turns it into "You" or a display name, and a person
  the people records no longer hold (someone who left) has none and is shown as "A team
  member". An agent is
  named by its type, which the contract caps at 24 characters (plan decision 56); the
  runtime still allows longer types until phase 3, so a longer one is shown as "Agent"
  and logged once (``_agent_editor``).
- **A change is carried out once.** Its receipt is stored with the change, in its
  transaction: what it changed, by id, and the review kinds the person confirmed, never
  page text. The reply shows the page as readers see it once the change is committed (its
  links in, open notes and proposals), so it is built from the receipt after the commit,
  on the first try and on every retry alike (``team_services.outcomes``). When that
  fails, the change is already made, and the request is answered as unknown
  (``AnswerUnknown``, ``handler_failed``), never as refused.

Replies carry page text inline. The contract's inline text holds 32,768 characters
(``knowledge_base.records.INLINE_TEXT_MAX``); longer text needs the object store, which
phase 2 does not have yet. Agents' tools still read and write longer pages (up to
``agent.knowledge.limits.max_body_chars``, at most 500,000; 24,000 by default), so the
Knowledge page meets them, and no knowledge may be lost or cut because of the limit:

- A read that would show longer text (a page, an earlier revision, an open note, a
  proposal or the team page a proposal changes) is answered ``runtime_error`` with a
  plain message (``PageTooLong``) that says nothing was changed or cut and that agents
  can still use the whole text.
- A change is refused the same way, before anything is stored, when it would replace,
  hide or decide text the person could not open here, or when its reply could not show
  the result: saving, noting or archiving a longer page, reverting it or reverting to a
  longer revision, and publishing or withdrawing a proposal that is longer or changes a
  longer page (``_require_page_fits``, ``_require_proposal_fits``). The text stays as it
  was, every revision with it, and proposals keep waiting.
- The gateway refuses longer typed text before sending it
  (``assistant_gateway.application.knowledge_wire``), and the editor keeps the text.

A reply is also limited in bytes: one frame carries at most 262,144
(``custom_messages.wire.sizes``). Texts within the character limit can still make a
page's reply larger, with its open notes, links and backlinks, in non-Latin script or
where a team raised the note, title, summary or tag limits. So, by the same rules:

- a list (pages, proposals, a page's history) returns the rows that fit and says where
  to continue (``next_offset``); the gateway asks again from there;
- a read whose reply would not fit is refused plainly (``ReplyTooLarge``);
- a change measures the reply it would get inside its own transaction, on the page as
  the change leaves it, and is rolled back and refused plainly when it would not fit
  (``_Change.receipt``), so nothing is saved that the person is then not told about.

Administrators see how much stored knowledge is longer on the Status page
(``custom_agents.long_knowledge``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from access_control.knowledge_access import knowledge_command_access
from access_control.people_directory import PeopleDirectory
from custom_messages.contracts.control_v1 import KnowledgeCommandAction, KnowledgeCommandPayload
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame
from custom_messages.wire.kinds import KINDS
from custom_messages.wire.sizes import REPLY_BODY_MAX_BYTES, encoded_size
from knowledge_base.records import (
    CODE_DISABLED,
    CODE_INVALID,
    CODE_MERGED,
    CODE_NOT_FOUND,
    INLINE_TEXT_MAX,
    ConfirmedChange,
    KnowledgeActor,
    KnowledgeContribution,
    KnowledgeError,
    KnowledgeNote,
    KnowledgePage,
    KnowledgeRevision,
)
from knowledge_base.sensitive_content import REVIEW_KIND_LABELS, SensitiveContentRules
from knowledge_base.service import PERSONAL_OVERLAP_KIND, DraftInput, KnowledgeService, PageView, knowledge_service_for
from knowledge_base.vocabulary import ActorKind, ContributionKind, KnowledgeSpace, PageStatus
from runtime_settings.knowledge import KnowledgeSettings
from storage.operation_outcomes import RECEIPT_KEY, CommandOutcome
from team_services.outcomes import AnswerUnknown, CommandOnce
from team_services.people import PersonCheck, command_person
from team_services.service import OperationFamily

KNOWLEDGE_ACTIONS = (
    "list", "read", "history", "write", "add_note", "archive", "revert", "list_contributions",
    "count_contributions", "read_contribution", "publish_contribution", "withdraw_contribution",
)
KNOWLEDGE_KINDS = tuple(f"knowledge.{action}" for action in KNOWLEDGE_ACTIONS)
_CHANGES = frozenset({"write", "add_note", "archive", "revert", "publish_contribution", "withdraw_contribution"})

PERSONAL_OVERLAP_LABEL = "text copied from your own memories or personal pages"
KNOWLEDGE_OFF_TEXT = (
    "The knowledge base is turned off for this team (agent.knowledge.enabled). Ask an administrator to turn it on."
)
RUNTIME_ERROR_TEXT = (
    "The knowledge base could not finish this request. Nothing was changed. Try again; if it keeps failing, an "
    "administrator can find the reason in the team services' log."
)
NOT_YOURS_TEXT = "Only the person whose pages or proposals these are can open or change them."
# Text longer than Web Chat can carry (module docstring): what it is, its length, and who can still use it.
TOO_LONG_TEXT = "{subject} has {length:,} characters, more than the {limit:,} Web Chat can show. {still}"
PAGE_TEXT = "This page's text"
REVISION_TEXT = "The text of revision {revision} of this page"
NOTE_TEXT = "An open note on this page"
PROPOSAL_TEXT = "This proposal's text"
PROPOSED_PAGE_TEXT = "The text of the team page this proposal changes"
AGENTS_STILL = (
    "Nothing was changed and nothing was cut. Agents can still read and change all of it, for example to split "
    "the page into smaller linked pages."
)
CURATOR_STILL = (
    "Nothing was changed and nothing was cut. The knowledge curator, working for you, can still read all of it "
    "and publish or reject the proposal."
)
# A reply larger than one frame carries (module docstring): what it is, its size, and who can still use it.
TOO_LARGE_TEXT = "{subject} too large for Web Chat to show: {size:,} bytes, more than the {limit:,} one reply carries. {still}"
PAGE_LARGE = "This page, with its open notes, links and backlinks, is"
CHANGED_PAGE_LARGE = "After this change the page, with its open notes, links and backlinks, would be"
PROPOSAL_LARGE = "This proposal, with the team page it changes, is"
LIST_ROW_LARGE = "One entry of this list is"
# The contract's short messages.
_MESSAGE_MAX = 500
# The newest revisions a history shows (the store returns at most 50).
HISTORY_MAX = 50
# The contract's review flags per reply (``review_flags`` maxItems).
FLAGS_MAX = 100
MORE_FLAGS_TEXT = "Only the first {shown} of {total:,} flagged places are listed."
# The contract's agent type: one NATS subject token of at most 24 characters (common.schema.json, plan decision 56).
_CONTRACT_AGENT_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,23}")
# How an agent whose type the contract cannot carry is shown.
AGENT_FALLBACK_TYPE = "Agent"
# The open notes a page shows, newest first (``KnowledgeService.read``).
_NOTES_SHOWN = 5

# The service's refusal codes, as the contract's outcomes.
_OUTCOMES = {CODE_INVALID: "invalid_request", CODE_DISABLED: "feature_off", CODE_MERGED: "merged"}
# A list's rows, by action (``_fit_rows``).
_LIST_ROWS = {"list": "pages", "list_contributions": "contributions", "history": "revisions"}


class NotShownInWebChat(ValueError):
    """Knowledge Web Chat cannot carry: nothing was changed (module docstring)."""

    event = "team_knowledge_not_shown"

    def __init__(self, message: str, size: int, limit: int) -> None:
        super().__init__(message)
        self.size = size
        self.limit = limit


class PageTooLong(NotShownInWebChat):
    """Knowledge text longer than the contract's inline text."""

    event = "team_knowledge_too_long"

    def __init__(self, subject: str, length: int, still: str) -> None:
        super().__init__(TOO_LONG_TEXT.format(subject=subject, length=length, limit=INLINE_TEXT_MAX, still=still),
                         length, INLINE_TEXT_MAX)


class ReplyTooLarge(NotShownInWebChat):
    """A reply larger than one frame carries (``custom_messages.wire.sizes``)."""

    event = "team_knowledge_too_large"

    def __init__(self, subject: str, size: int, still: str = AGENTS_STILL) -> None:
        super().__init__(TOO_LARGE_TEXT.format(subject=subject, size=size, limit=REPLY_BODY_MAX_BYTES, still=still),
                         size, REPLY_BODY_MAX_BYTES)


@dataclass(frozen=True)
class _Request:
    service: KnowledgeService
    repository: Any
    actor: KnowledgeActor
    action: str
    body: dict[str, Any]
    editor: Callable[[str, str, str], dict[str, Any]]

    @property
    def reference(self) -> str:
        return f"{self.body['space']}/{self.body['page']}"

    def person_editor(self) -> dict[str, Any]:
        return self.editor(ActorKind.PERSON.value, self.actor.actor_id, "")


@dataclass
class _Change:
    """One change: what its reply shows besides the page it leaves, prepared before it, and its stored receipt.

    Before the change, the changer prepares the rest of the page the reply will show
    (``prepare``): the backlinks, open notes and pending proposals of the page the change
    acts on, or the backlinks a page it creates will have, each with its editors named.
    Inside the change's transaction, ``receipt`` measures the reply the change would get,
    on the page as the change leaves it and without reading anything, and refuses with
    ``ReplyTooLarge`` (which rolls the change back) when it would not fit; then it keeps
    and returns the receipt the store saves.
    """

    request: _Request
    person_editor: dict[str, Any]
    page: Optional[KnowledgePage] = None
    backlinks: list[dict[str, Any]] = field(default_factory=list)
    open_notes: list[dict[str, Any]] = field(default_factory=list)
    pending: list[dict[str, Any]] = field(default_factory=list)
    stored: Optional[dict[str, Any]] = None

    def prepare(self, view: Optional[PageView]) -> None:
        """The page the change acts on, as read before it (None: it could not be read; the change refuses)."""

        if view is None:
            return
        self.page = view.page
        self.backlinks = [_summary(linked) for linked in view.backlinks]
        self.open_notes = [_note(self.request, note, any_editor=True) for note in view.open_notes]
        self.pending = _pending(view.pending_contributions)

    def prepare_new(self, reference: str) -> None:
        """A page the change creates under ``reference``: the pages already linking to its name."""

        try:
            linked = self.request.service.backlinks_to_name(self.request.actor, reference)
        except KnowledgeError:
            return  # a name the change refuses, with its own reason
        self.backlinks = [_summary(page) for page in linked]

    def receipt(self, result: Any) -> dict[str, Any]:
        confirmed: tuple[str, ...] = ()
        if isinstance(result, ConfirmedChange):
            result, confirmed = result.result, tuple(result.review_kinds)
        self._require_fits(result, confirmed)
        if isinstance(result, KnowledgePage):
            receipt: dict[str, Any] = {"page_id": result.page_id}
        elif isinstance(result, KnowledgeNote):
            receipt = {"page_id": result.page_id, "note_id": result.note_id}
        elif isinstance(result, KnowledgeContribution):
            receipt = {"contribution_id": result.contribution_id}
        else:
            raise TypeError(f"A knowledge change returned {type(result).__name__}.")
        if confirmed:
            receipt["review_kinds"] = list(confirmed)
        self.stored = {RECEIPT_KEY: receipt}
        return self.stored

    def _require_fits(self, result: Any, confirmed: tuple[str, ...]) -> None:
        request = self.request
        if isinstance(result, KnowledgeContribution):
            reply: dict[str, Any] = {"outcome": "ok", "contribution": _contribution(request, result)}
        elif isinstance(result, KnowledgeNote):
            if self.page is None or result.page_id != self.page.page_id:
                return  # a page the change found by itself (a merge meanwhile): measured when the reply is built
            # The person added the note, and the page shows its newest open notes first.
            note = _note_shown(result, self.person_editor)
            reply = {"outcome": "ok", "page": self._detail(self.page, [note, *self.open_notes][:_NOTES_SHOWN]),
                     "note": note}
        else:
            reply = {"outcome": "ok", "page": self._detail(result, None)}
        if request.action in {"write", "add_note"}:
            reply["review_flags"] = _review_flags(confirmed)
        size = encoded_size(reply)
        if size > REPLY_BODY_MAX_BYTES:
            raise ReplyTooLarge(CHANGED_PAGE_LARGE, size)

    def _detail(self, page: KnowledgePage, notes: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
        same = self.page is not None and page.page_id == self.page.page_id
        return _detail(
            self.request, page,
            backlinks=self.backlinks,
            open_notes=notes if notes is not None else (self.open_notes if same else []),
            pending=self.pending if same else [],
            # The person made the change; for a note the page keeps its last editor, of about the same size.
            updated_by=self.person_editor,
            check_links=False,
        )


class KnowledgeOperations:
    """Answers people's Knowledge page commands for one team (module docstring)."""

    def __init__(self, repository: Any, people: PeopleDirectory, settings: KnowledgeSettings,
                 clock: Callable[[], datetime], logger: logging.Logger, *, personal_memory: Any = None) -> None:
        self._repository = repository
        self._people = people
        self._settings = settings
        self._clock = clock
        self._logger = logger
        self._service = (
            knowledge_service_for(repository, settings=settings, personal_memory=personal_memory, logger=logger)
            if repository is not None and settings.enabled else None
        )
        self._rules = SensitiveContentRules.from_patterns(
            (pattern.name, pattern.pattern) for pattern in settings.sensitive_content.blocked_patterns
        )
        # Agent types and people already logged as not shown by name (``_editor``): once each per process.
        self._long_agent_types: set[str] = set()
        self._unrecorded_people: set[str] = set()

    def family(self) -> OperationFamily:
        """Its handlers at the team's address and its upkeep; they run in order (module docstring)."""

        return OperationFamily({kind: self.handle for kind in KNOWLEDGE_KINDS}, upkeep=self.purge_outcomes)

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        return await asyncio.to_thread(self.answer, verified)

    async def purge_outcomes(self) -> None:
        """Delete stored outcomes past their retention, also when no new command stores one."""

        if self._repository is None:
            return
        purged = await asyncio.to_thread(self._repository.purge_command_outcomes, self._clock())
        if purged:
            self._logger.info("Deleted old stored outcomes. event=team_knowledge_outcomes_purged count=%s", purged)

    def answer(self, verified: VerifiedFrame) -> dict[str, Any]:
        """The reply to one Knowledge page command (blocking: the store is synchronous).

        As for memory commands (``team_services.outcomes``): a repeated change is answered
        from its stored outcome first, and every final refusal of a change is stored.
        """

        action = verified.frame.kind.removeprefix("knowledge.")
        body = verified.raw["body"]
        if self._service is None:
            # No store is open, so nothing can be stored: the setting is read when the team services start.
            return _message_reply("feature_off", KNOWLEDGE_OFF_TEXT)
        check = command_person(verified, self._people, self._logger)
        if check.person_id is None:
            return _message_reply("not_allowed", check.reason)
        request = _Request(self._service, self._repository, _person_actor(check.person_id, self._people.team_id),
                           action, body, self._editor)
        change = _Change(request, person_editor=request.person_editor()) if action in _CHANGES else None
        once = self._once(verified, request, change) if change is not None else None
        if once is not None:
            stored = once.stored_reply()
            if stored is not None:
                return stored
        refused = self._refused_person(check, action, body)
        if refused is not None:
            return refused if once is None else once.final(refused)
        if change is None or once is None:
            try:
                return _fitted(request, _READS[action](request))
            except Exception as error:  # noqa: BLE001 - every failure is answered and logged in _refusal
                return self._refusal(request, error)

        def attempt() -> dict[str, Any]:
            _CHANGERS[action](change, once.outcome)
            self._logger.info("A person changed the knowledge base from Web Chat. event=team_knowledge_changed "
                              "person=%s action=%s space=%s page=%s contribution_id=%s", check.person_id, action,
                              body.get("space"), body.get("page"), body.get("contribution_id"))
            if change.stored is None:
                # Every change path stores its receipt with the change (tests/test_team_knowledge.py checks each).
                raise AnswerUnknown(f"knowledge.{action} changed the knowledge base without storing its receipt.")
            return self._reply_from_receipt(request, change.stored)

        return once.run(attempt, refusal_for=lambda error: self._refusal(request, error))

    def _once(self, verified: VerifiedFrame, request: _Request, change: _Change) -> CommandOnce:
        kind = verified.frame.kind
        outcome = CommandOutcome(
            team_id=self._people.team_id, retry_key=str(verified.raw["idempotency_key"]),
            person_id=request.actor.actor_id, kind=kind, body_digest=str(verified.raw["body_digest"]),
            now=self._clock(), reply_for=change.receipt, logger=self._logger,
        )
        return CommandOnce(
            self._repository, outcome,
            reply_from_receipt=lambda stored: self._reply_from_receipt(request, stored),
            key_taken=lambda taken: _message_reply(
                "invalid_request" if "invalid_request" in KINDS[kind].reply_errors else "runtime_error", str(taken)),
            not_stored=_message_reply("runtime_error", RUNTIME_ERROR_TEXT),
            logger=self._logger,
        )

    def _refused_person(self, check: PersonCheck, action: str, body: dict[str, Any]) -> Optional[dict[str, Any]]:
        """The refusal for a person who may not act, or may not touch these pages or proposals; None when they may."""

        person = check.person
        if person is None:
            return _message_reply("not_allowed", check.reason)
        for space in _access_spaces(body):
            decision = knowledge_command_access(access_payload(action, {**body, "space": space}),
                                                person_id=person.person_id, team_id=person.team_id,
                                                roles=person.roles)
            if not decision.allowed:
                self._logger.warning("Refused a Knowledge page request. event=team_knowledge_command_denied "
                                     "person=%s action=%s space=%s reason=%s", person.person_id, action, space,
                                     decision.reason)
                return _message_reply("not_allowed", NOT_YOURS_TEXT)
        return None

    def _reply_from_receipt(self, request: _Request, stored: dict[str, Any]) -> dict[str, Any]:
        """A committed change's reply, read as the change left it; ``AnswerUnknown`` when that fails."""

        try:
            return _reply_from_receipt(request, stored[RECEIPT_KEY])
        except Exception as error:
            self._logger.error("A person's knowledge change was made, but its reply could not be built; the request "
                               "is answered as unknown. event=team_knowledge_reply_after_change_failed person=%s "
                               "action=%s error_type=%s detail=%s", request.actor.actor_id, request.action,
                               type(error).__name__, error, exc_info=error)
            raise AnswerUnknown(f"knowledge.{request.action} was carried out, but its reply could not be built.") \
                from error

    def _editor(self, kind: str, actor_id: str, agent_type: str) -> dict[str, Any]:
        """Who changed something: an agent by type, a person by reference, or the system.

        A person the team's people records no longer hold (someone who left the team) is a
        person without a reference, which the gateway shows as "A team member".
        """

        if kind == ActorKind.AGENT.value:
            return self._agent_editor(agent_type)
        if kind == ActorKind.PERSON.value:
            reference = self._people.person_ref(actor_id)
            if reference is not None:
                return {"kind": "person", "person": reference}
            if actor_id not in self._unrecorded_people:
                self._unrecorded_people.add(actor_id)
                self._logger.info("A knowledge change names a person the team's people records no longer hold; shown "
                                  "as a team member. event=team_knowledge_editor_unrecorded person=%s", actor_id)
            return {"kind": "person"}
        return {"kind": "system"}

    def _agent_editor(self, agent_type: str) -> dict[str, Any]:
        """An agent by its type, or as ``AGENT_FALLBACK_TYPE`` when the contract cannot carry the type (module docstring)."""

        if _CONTRACT_AGENT_TYPE.fullmatch(agent_type or ""):
            return {"kind": "agent", "agent_type": agent_type}
        if agent_type and agent_type not in self._long_agent_types:
            self._long_agent_types.add(agent_type)
            self._logger.warning("An agent type the message contract cannot carry (at most 24 letters, digits, _ or "
                                 "-) is shown in Web Chat as %r. event=team_knowledge_agent_type_not_shown "
                                 "agent_type=%s", AGENT_FALLBACK_TYPE, agent_type)
        return {"kind": "agent", "agent_type": AGENT_FALLBACK_TYPE}

    def _refusal(self, request: _Request, error: Exception) -> dict[str, Any]:
        kind = f"knowledge.{request.action}"
        allowed = set(KINDS[kind].reply_errors)
        if isinstance(error, NotShownInWebChat):
            self._logger.info("A Knowledge page request met knowledge Web Chat cannot show; nothing was changed. "
                              "event=%s person=%s action=%s size=%s limit=%s", error.event, request.actor.actor_id,
                              request.action, error.size, error.limit)
            return _message_reply("runtime_error", str(error))
        if isinstance(error, KnowledgeError):
            outcome = _OUTCOMES.get(error.code, error.code)
            if outcome == "invalid_request" and "invalid_request" not in allowed and "not_found" in allowed:
                outcome = "not_found"
            if outcome in allowed:
                self._logger.info("A Knowledge page request was refused. event=team_knowledge_refused person=%s "
                                  "action=%s code=%s", request.actor.actor_id, request.action, error.code)
                return self._refusal_reply(request, outcome, error)
        self._logger.error("A Knowledge page request failed. event=team_knowledge_command_failed person=%s action=%s "
                           "error_type=%s", request.actor.actor_id, request.action, type(error).__name__, exc_info=error)
        return _message_reply("runtime_error", RUNTIME_ERROR_TEXT)

    def _refusal_reply(self, request: _Request, outcome: str, error: KnowledgeError) -> dict[str, Any]:
        message = error.message
        details: dict[str, Any] = {}
        if outcome in {"changed_since_read", "page_exists"}:
            details["current_version"] = str(int(error.details.get("current_revision") or 0))
        elif outcome == "merged":
            details["merged_into"] = str(error.details.get("redirects_to") or "")
        elif outcome in {"review_required", "sensitive_content"}:
            flags = self._flags_for(request, error, blocked=outcome == "sensitive_content")
            details["review_flags"] = flags[:FLAGS_MAX]
            if len(flags) > FLAGS_MAX:
                # The contract carries at most FLAGS_MAX; the message says how many there are.
                message = f"{MORE_FLAGS_TEXT.format(shown=FLAGS_MAX, total=len(flags))} {message}"
        return {**_message_reply(outcome, message), **details}

    def _flags_for(self, request: _Request, error: KnowledgeError, *, blocked: bool) -> list[dict[str, Any]]:
        """Where flagged or refused content is, field by field, without its text.

        The service reports kinds (review) or positions in the text it checked
        (refusals); scanning the person's own fields again with the same rules names the
        field as well, so the page can say "the body, line 3".
        """

        flags: list[dict[str, Any]] = []
        for name, value in _checked_fields(request):
            scan = self._rules.scan(value)
            for finding in scan.blocked if blocked else scan.review:
                flags.append(_flag(finding.kind, finding.label, name, finding.line, finding.column))
        if blocked and not flags:
            # Stored text was refused (a revert or a proposal): positions without a field.
            flags = [
                _flag(str(found.get("kind") or ""), str(found.get("kind") or "").replace("_", " "), "",
                      int(found.get("line") or 0), int(found.get("column") or 0))
                for found in error.details.get("found") or []
            ]
        if not blocked and PERSONAL_OVERLAP_KIND in (error.details.get("review_kinds") or []):
            flags.append(_flag(PERSONAL_OVERLAP_KIND, PERSONAL_OVERLAP_LABEL))
        return flags


# -- requests -----------------------------------------------------------------------


def access_payload(action: str, body: dict[str, Any]) -> KnowledgeCommandPayload:
    """The request as the ownership rule reads it (``access_control.knowledge_access``), for one space."""

    return KnowledgeCommandPayload(
        action=KnowledgeCommandAction(action),
        space=body.get("space", "team"),
        page=body.get("page", "") or "",
        contribution_id=body.get("contribution_id", "") or "",
    )


def _access_spaces(body: dict[str, Any]) -> tuple[str, ...]:
    """The spaces a request reads or changes: a list of ``all`` reads the team's pages and the person's own."""

    space = body.get("space", "team")
    return (KnowledgeSpace.TEAM.value, KnowledgeSpace.PERSONAL.value) if space == "all" else (space,)


def _person_actor(person_id: str, team_id: str) -> KnowledgeActor:
    """The person as the knowledge actor: their edits, their own personal space."""

    return KnowledgeActor(kind=ActorKind.PERSON, actor_id=person_id, team_id=team_id, work_person_id=person_id)


def _text(content: Optional[dict[str, Any]]) -> str:
    if content is None:
        return ""
    if content.get("type") != "inline":
        raise KnowledgeError(CODE_INVALID, "Knowledge text is sent inline.")
    return str(content["text"])


def _version(value: Any) -> int:
    text = str(value or "0")
    if not text.isdigit():
        raise KnowledgeError(CODE_INVALID, "A page version is its revision number.")
    return int(text)


def _draft(body: dict[str, Any]) -> DraftInput:
    return DraftInput(title=body.get("title", ""), body=_text(body.get("content")), summary=body.get("summary"),
                      tags=body.get("tags"))


def _checked_fields(request: _Request) -> Iterable[tuple[str, str]]:
    body = request.body
    if request.action == "add_note":
        return (("note", _text(body.get("note"))),)
    return (
        ("title", body.get("title", "")),
        ("summary", body.get("summary") or ""),
        ("body", _text(body.get("content"))),
        ("tags", ", ".join(body.get("tags") or ())),
    )


# -- reads --------------------------------------------------------------------------


def _list(request: _Request) -> dict[str, Any]:
    body = request.body
    hits, total = request.service.browse(
        request.actor, query=body.get("query", ""), space=body["space"], limit=body["limit"],
        offset=body["offset"], status=PageStatus(body["status"]), page_type=body.get("page_type", ""),
        tag=body.get("tag", ""),
    )
    return _paged({"outcome": "ok", "pages": [_summary(hit.page, hit.snippet) for hit in hits]}, "pages",
                  body["offset"], total)


def _read(request: _Request) -> dict[str, Any]:
    return {"outcome": "ok", "page": _page(request, request.reference, revision=int(request.body.get("revision", 0)))}


def _history(request: _Request) -> dict[str, Any]:
    offset, limit = int(request.body["offset"]), int(request.body["limit"])
    page, revisions = request.service.history(request.actor, request.reference, limit=min(HISTORY_MAX, offset + limit))
    shown = [_revision(request, revision) for revision in revisions[offset:offset + limit]]
    # The history is the newest HISTORY_MAX revisions, and its total counts that window (contract README).
    return _paged({"outcome": "ok", "page": _page(request, page.reference()), "revisions": shown}, "revisions",
                  offset, len(revisions) if len(revisions) < offset + limit else min(page.revision, HISTORY_MAX))


def _list_contributions(request: _Request) -> dict[str, Any]:
    body = request.body
    shown, total = request.service.browse_contributions(request.actor, limit=body["limit"], offset=body["offset"])
    return _paged({"outcome": "ok", "contributions": [_contribution(request, c) for c in shown]}, "contributions",
                  body["offset"], total)


def _count_contributions(request: _Request) -> dict[str, Any]:
    return {"outcome": "ok", "total": request.service.count_pending_contributions(request.actor)}


def _read_contribution(request: _Request) -> dict[str, Any]:
    contribution = request.service.get_contribution(request.actor, request.body["contribution_id"])
    return {"outcome": "ok", "contribution": _contribution(request, contribution, full=True)}


_READS: dict[str, Callable[[_Request], dict[str, Any]]] = {
    "list": _list,
    "read": _read,
    "history": _history,
    "list_contributions": _list_contributions,
    "count_contributions": _count_contributions,
    "read_contribution": _read_contribution,
}


def _fitted(request: _Request, reply: dict[str, Any]) -> dict[str, Any]:
    """A read's reply within one frame: a list keeps the rows that fit; anything else fits or is refused."""

    key = _LIST_ROWS.get(request.action)
    if key is not None:
        return _fit_rows(request, reply, key)
    size = encoded_size(reply)
    if size > REPLY_BODY_MAX_BYTES:
        raise ReplyTooLarge(PROPOSAL_LARGE if request.action == "read_contribution" else PAGE_LARGE, size)
    return reply


def _fit_rows(request: _Request, reply: dict[str, Any], key: str) -> dict[str, Any]:
    """The rows that fit, from the first; ``next_offset`` then names the first row left out (contract "Reply size")."""

    rows = list(reply[key])
    if encoded_size(reply) <= REPLY_BODY_MAX_BYTES:
        return reply
    offset = int(request.body["offset"])
    # Measured with the most a continuation adds: next_offset, as large as it can be.
    base = encoded_size({**reply, key: [], "next_offset": offset + len(rows)})
    used, kept = base, 0
    for row in rows:
        added = encoded_size(row) + (1 if kept else 0)
        if used + added > REPLY_BODY_MAX_BYTES:
            break
        used += added
        kept += 1
    if kept == 0:
        # Not even one row fits: a history whose page is too large, or one list entry that is.
        raise ReplyTooLarge(PAGE_LARGE if key == "revisions" else LIST_ROW_LARGE, encoded_size({**reply, key: rows[:1]}))
    fitted = {name: value for name, value in reply.items() if name != "next_offset"}
    fitted[key] = rows[:kept]
    return _paged(fitted, key, offset, int(reply["total"]))


# -- changes ------------------------------------------------------------------------
# Each reads what its reply shows besides the changed page (``_Change``), checks the texts the
# person must be able to open, and makes the change, which stores its receipt (``_Change.receipt``).


def _write(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    body = request.body
    # A new page is named after its title unless the page names itself.
    reference = f"{body['space']}/{body.get('page') or body.get('title', '')}"
    base_revision = _version(body.get("seen_version"))
    if base_revision:
        # An edit replaces the page's text (or keeps it with new details); either way the person must have seen it.
        change.prepare(_require_page_fits(request, reference))
    else:
        change.prepare_new(reference)
    request.service.write(request.actor, reference, _draft(body), base_revision=base_revision,
                          confirm_review=bool(body["confirm_review"]), command_outcome=outcome)


def _add_note(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    # The reply shows the page with the new note.
    change.prepare(_require_page_fits(request, request.reference))
    request.service.add_note(request.actor, request.reference, _text(request.body["note"]),
                             confirm_review=bool(request.body["confirm_review"]), command_outcome=outcome)


def _archive(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    # Archiving takes the page out of lists and searches: never one the person could not open.
    change.prepare(_require_page_fits(request, request.reference))
    request.service.archive(request.actor, request.reference, base_revision=_version(request.body["seen_version"]),
                            reason=request.body["reason"], command_outcome=outcome)


def _revert(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    body = request.body
    # Both the current text and the revision that replaces it must be texts the person could open.
    change.prepare(_require_page_fits(request, request.reference, revision=int(body["to_revision"])))
    request.service.revert(request.actor, request.reference, to_revision=int(body["to_revision"]),
                           base_revision=_version(body["seen_version"]), reason=body["reason"],
                           command_outcome=outcome)


def _publish_contribution(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    body = request.body
    edited = bool(body["edited"])
    contribution = _proposal(request)
    if contribution is not None:
        change.prepare(_require_proposal_fits(request, contribution))
        if contribution.kind is ContributionKind.CREATE:
            name = _published_name(body, contribution)
            if _version(body.get("seen_version")):
                # A proposed new page published as an edit of a page that took its name replaces that page's text.
                change.prepare(_require_page_fits(request, name, subject=PROPOSED_PAGE_TEXT, still=CURATOR_STILL))
            else:
                change.prepare_new(name)
    request.service.publish_contribution(
        request.actor, body["contribution_id"],
        draft=_draft(body) if edited else None,
        # A note proposal publishes this text; a page proposal uses the draft. An edited note
        # with no text is an empty note, which the service refuses rather than publish the original.
        note=(_text(body["note"]) if "note" in body else "") if edited else None,
        # The revision of the team page the person compared the proposal with.
        base_revision=_version(body.get("seen_version")),
        page=body.get("page", ""),
        command_outcome=outcome,
    )


def _withdraw_contribution(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    contribution = _proposal(request)
    if contribution is not None:
        # Nobody drops a proposal they could not read.
        _require_proposal_fits(request, contribution)
    request.service.withdraw_contribution(request.actor, request.body["contribution_id"], command_outcome=outcome)


def _proposal(request: _Request) -> Optional[KnowledgeContribution]:
    """The proposal a decision names, or None when the decision itself refuses it, with its own reason."""

    try:
        return request.service.get_contribution(request.actor, request.body["contribution_id"])
    except KnowledgeError:
        return None


def _published_name(body: dict[str, Any], contribution: KnowledgeContribution) -> str:
    """The team page a proposed new page is published under, named as ``KnowledgeService.publish_contribution``
    names it: the reviewer's ``page``, or else the reviewed title (a title is always a name)."""

    page = str(body.get("page") or "").strip()
    if page:
        return page if "/" in page else f"{KnowledgeSpace.TEAM.value}/{page}"
    title = (body.get("title") if body["edited"] else "") or contribution.title
    return f"{KnowledgeSpace.TEAM.value}/{title}"


_CHANGERS: dict[str, Callable[[_Change, CommandOutcome], None]] = {
    "write": _write,
    "add_note": _add_note,
    "archive": _archive,
    "revert": _revert,
    "publish_contribution": _publish_contribution,
    "withdraw_contribution": _withdraw_contribution,
}


def _reply_from_receipt(request: _Request, receipt: dict[str, Any]) -> dict[str, Any]:
    """A change's reply from its receipt, read as the change left it (module docstring)."""

    if request.action == "withdraw_contribution":
        contribution = request.service.get_contribution(request.actor, receipt["contribution_id"])
        return {"outcome": "ok", "contribution": _contribution(request, contribution)}
    page = request.repository.get_page_by_id(receipt["page_id"])
    if page is None:
        raise KnowledgeError(CODE_NOT_FOUND, "The changed page no longer exists.")
    reply: dict[str, Any] = {"outcome": "ok", "page": _page(request, page.reference())}
    if request.action in {"write", "add_note"}:
        reply["review_flags"] = _review_flags(receipt.get("review_kinds") or ())
    if receipt.get("note_id"):
        note = request.repository.get_note(receipt["note_id"])
        if note is None:
            raise KnowledgeError(CODE_NOT_FOUND, "The added note no longer exists.")
        reply["note"] = _note(request, note)
    size = encoded_size(reply)
    if size > REPLY_BODY_MAX_BYTES:
        raise ReplyTooLarge(PAGE_LARGE, size)
    return reply


# -- what the page shows ------------------------------------------------------------


def _summary(page: KnowledgePage, snippet: str = "") -> dict[str, Any]:
    summary = {
        "space": page.space.value, "page": page.slug, "title": page.title, "page_type": page.page_type,
        "summary": page.summary, "tags": list(page.tags), "status": page.status.value,
        "seen_version": str(page.revision), "updated_at": _timestamp(page.updated_at_utc),
    }
    if snippet:
        summary["snippet"] = {"type": "inline", "text": snippet}
    return summary


def _page(request: _Request, reference: str, *, revision: int = 0) -> dict[str, Any]:
    """The page as the Knowledge page shows it: text, links in, open notes, last change."""

    view = request.service.read(request.actor, reference, revision=revision)
    _require_shown(view)
    _page_latest, latest = request.service.history(request.actor, view.page.reference(), limit=1)
    return _detail(
        request, view.page, shown=view.revision, backlinks=[_summary(linked) for linked in view.backlinks],
        open_notes=[_note(request, note, any_editor=True) for note in view.open_notes],
        pending=_pending(view.pending_contributions), redirected_from=view.redirected_from,
        updated_by=_revision_editor(request, latest[0]) if latest else {"kind": "system"},
    )


def _detail(request: _Request, page: KnowledgePage, *, backlinks: list[dict[str, Any]],
            open_notes: list[dict[str, Any]], pending: list[dict[str, Any]], updated_by: dict[str, Any],
            shown: Optional[KnowledgeRevision] = None, redirected_from: str = "",
            check_links: bool = True) -> dict[str, Any]:
    """One page's detail, around its collections as shown; ``check_links`` False reads nothing (``_Change``)."""

    source = shown or page
    detail = {
        "type": "inline",
        **_summary(page),
        "title": source.title,
        "page_type": source.page_type,
        "summary": source.summary,
        "tags": list(source.tags),
        # Every page has text (the service refuses an empty body).
        "content": {"type": "inline", "text": source.body},
        "created_at": _timestamp(page.created_at_utc),
        "updated_by": updated_by,
        "backlinks": backlinks,
        "open_notes": open_notes,
        "pending_contributions": pending,
        "links": [{"written": link.written, "space": link.space, "page": link.slug, "exists": link.exists}
                  for link in request.service.page_links(page, source.body, check_exists=check_links)],
    }
    if page.frozen_until_utc:
        detail["frozen_until"] = _timestamp(page.frozen_until_utc)
    if shown is not None:
        detail["shown_revision"] = shown.revision
        detail["shown_by"] = _revision_editor(request, shown)
    if redirected_from:
        detail["redirected_from"] = redirected_from
    return detail


def _pending(contributions: Iterable[KnowledgeContribution]) -> list[dict[str, Any]]:
    return [{"contribution_id": c.contribution_id, "kind": c.kind.value, "created_at": _timestamp(c.created_at_utc)}
            for c in contributions]


def _revision_editor(request: _Request, revision: KnowledgeRevision) -> dict[str, Any]:
    return request.editor(revision.actor_kind, revision.actor_id, revision.actor_agent_type)


def _revision(request: _Request, revision: KnowledgeRevision) -> dict[str, Any]:
    shown = {
        "revision": revision.revision, "updated_at": _timestamp(revision.created_at_utc),
        "updated_by": _revision_editor(request, revision), "title": revision.title,
        "status": revision.status.value, "change_kind": revision.change_kind.value,
    }
    if revision.change_summary:
        shown["change_summary"] = revision.change_summary
    return shown


def _note(request: _Request, note: KnowledgeNote, *, any_editor: bool = False) -> dict[str, Any]:
    editor = request.editor(note.actor_kind, note.actor_id, note.actor_agent_type)
    if editor["kind"] == "system" and not any_editor:
        # An added note names who added it; a note the system wrote shows as an open note only.
        editor = request.editor(ActorKind.AGENT.value, "", note.actor_agent_type)
    return _note_shown(note, editor)


def _note_shown(note: KnowledgeNote, editor: dict[str, Any]) -> dict[str, Any]:
    return {"note_id": note.note_id, "content": {"type": "inline", "text": note.body},
            "created_at": _timestamp(note.created_at_utc), "created_by": editor}


def _contribution(request: _Request, contribution: KnowledgeContribution, *, full: bool = False) -> dict[str, Any]:
    """One proposal; ``full`` adds its text and the team page as it is now (for before and after)."""

    shown = {
        "contribution_id": contribution.contribution_id, "kind": contribution.kind.value, "page": contribution.slug,
        "title": contribution.title, "page_type": contribution.page_type, "summary": contribution.summary,
        "tags": list(contribution.tags), "change_summary": contribution.change_summary,
        "base_revision": contribution.base_revision, "state": contribution.status.value,
        "created_at": _timestamp(contribution.created_at_utc),
        "author": request.editor(ActorKind.AGENT.value, "", contribution.actor_agent_type),
        "review_flags": _review_flags(contribution.review_kinds),
    }
    if not full:
        return shown
    _require_proposal_fits(request, contribution)
    if contribution.body:
        shown["content"] = {"type": "inline", "text": contribution.body}
    if contribution.kind is not ContributionKind.CREATE:
        try:
            shown["current"] = _page(request, f"{KnowledgeSpace.TEAM.value}/{contribution.slug}")
        except KnowledgeError as error:
            if error.code != CODE_NOT_FOUND:
                raise
    return shown


# -- text longer than Web Chat can carry (module docstring) ------------------------------


def _require_fits(text: str, subject: str, still: str) -> None:
    if len(text) > INLINE_TEXT_MAX:
        raise PageTooLong(subject, len(text), still)


def _require_shown(view: PageView, *, subject: str = PAGE_TEXT, still: str = AGENTS_STILL) -> None:
    """What ``_page`` shows of a page: the revision asked for, or else the current text, and the open notes."""

    if view.revision is not None:
        _require_fits(view.revision.body, REVISION_TEXT.format(revision=view.revision.revision), still)
    else:
        _require_fits(view.page.body, subject, still)
    for note in view.open_notes:
        _require_fits(note.body, NOTE_TEXT, still)


def _require_page_fits(request: _Request, reference: str, *, revision: int = 0, subject: str = PAGE_TEXT,
                       still: str = AGENTS_STILL) -> Optional[PageView]:
    """Before a change: the page's text and open notes, and the revision a revert restores, all fit.

    Checked before anything is stored, so a person never replaces, hides or reverts
    text they could not open here, and the change's reply can show its result. A page
    that cannot be read is not checked: the change itself then refuses, with its reason.
    Returns the page as read, for sizing the change's reply (``_Change``).
    """

    try:
        view = request.service.read(request.actor, reference, revision=revision)
    except KnowledgeError:
        return None
    if view.revision is not None:
        # A revert replaces the current text with the revision's.
        _require_fits(view.page.body, subject, still)
    _require_shown(view, subject=subject, still=still)
    return view


def _require_proposal_fits(request: _Request, contribution: KnowledgeContribution) -> Optional[PageView]:
    """A proposal as Web Chat shows it (``_contribution``): its text, and the team page it changes, fit.

    Checked before the person reads, publishes or withdraws it, so nobody decides a
    proposal they could not read. Returns the team page it changes, as read.
    """

    _require_fits(contribution.body, PROPOSAL_TEXT, CURATOR_STILL)
    if contribution.kind is ContributionKind.CREATE:
        return None
    return _require_page_fits(request, f"{KnowledgeSpace.TEAM.value}/{contribution.slug}",
                              subject=PROPOSED_PAGE_TEXT, still=CURATOR_STILL)


def _review_flags(kinds: Iterable[str]) -> list[dict[str, Any]]:
    labels = {**REVIEW_KIND_LABELS, PERSONAL_OVERLAP_KIND: PERSONAL_OVERLAP_LABEL}
    return [_flag(kind, labels.get(kind, kind.replace("_", " "))) for kind in kinds][:FLAGS_MAX]


def _flag(kind: str, label: str, field: str = "", line: int = 0, column: int = 0) -> dict[str, Any]:
    return {"kind": kind, "label": label, "field": field, "line": line, "column": column}


def _paged(reply: dict[str, Any], key: str, offset: int, total: int) -> dict[str, Any]:
    reply["total"] = int(total)
    following = int(offset) + len(reply[key])
    if reply[key] and following < total:
        reply["next_offset"] = following
    return reply


def _timestamp(stored: str) -> str:
    return format_timestamp(datetime.fromisoformat(str(stored).replace("Z", "+00:00")))


def _message_reply(outcome: str, message: str) -> dict[str, Any]:
    text = (message or "").strip() or "No details."
    return {"outcome": outcome, "message": text if len(text) <= _MESSAGE_MAX else text[: _MESSAGE_MAX - 1] + "…"}


__all__ = ["KNOWLEDGE_KINDS", "KnowledgeOperations", "access_payload"]

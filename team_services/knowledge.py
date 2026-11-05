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
  opaque person reference; the gateway turns it into "You" or a display name.
- **A change is carried out once.** Its receipt (what it changed, by id) is stored with
  the change, and the full reply right after it commits, because the reply shows the
  page as readers see it then: its links in, open notes and proposals
  (``team_services.outcomes``).

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

Administrators see how much stored knowledge is longer on the Status page
(``custom_agents.long_knowledge``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from access_control.knowledge_access import knowledge_command_access
from access_control.people_directory import PeopleDirectory
from custom_messages.contracts.control_v1 import KnowledgeCommandAction, KnowledgeCommandPayload
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame
from custom_messages.wire.kinds import KINDS
from knowledge_base.records import (
    CODE_DISABLED,
    CODE_INVALID,
    CODE_MERGED,
    CODE_NOT_FOUND,
    INLINE_TEXT_MAX,
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
from storage.operation_outcomes import CommandOutcome
from team_services.outcomes import carried_out_once
from team_services.people import CommandPerson, command_person

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
# The contract's short messages.
_MESSAGE_MAX = 500
# The newest revisions a history shows (the store returns at most 50).
HISTORY_MAX = 50

# The service's refusal codes, as the contract's outcomes.
_OUTCOMES = {CODE_INVALID: "invalid_request", CODE_DISABLED: "feature_off", CODE_MERGED: "merged"}


class PageTooLong(ValueError):
    """Knowledge text longer than the contract's inline text: nothing was changed (module docstring)."""

    def __init__(self, subject: str, length: int, still: str) -> None:
        super().__init__(TOO_LONG_TEXT.format(subject=subject, length=length, limit=INLINE_TEXT_MAX, still=still))
        self.length = length


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

    def handlers(self) -> dict[str, Callable[[VerifiedFrame], Any]]:
        return {kind: self.handle for kind in KNOWLEDGE_KINDS}

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        return await asyncio.to_thread(self.answer, verified)

    def answer(self, verified: VerifiedFrame) -> dict[str, Any]:
        """The reply to one Knowledge page command (blocking: the store is synchronous)."""

        action = verified.frame.kind.removeprefix("knowledge.")
        body = verified.raw["body"]
        person, reason = command_person(verified, self._people, self._logger)
        if person is None:
            return _message_reply("not_allowed", reason)
        if self._service is None:
            return _message_reply("feature_off", KNOWLEDGE_OFF_TEXT)
        decision = knowledge_command_access(access_payload(action, body), person_id=person.person_id,
                                            team_id=person.team_id, roles=person.roles)
        if not decision.allowed:
            self._logger.warning("Refused a Knowledge page request. event=team_knowledge_command_denied person=%s "
                                 "action=%s space=%s reason=%s", person.person_id, action, body.get("space"),
                                 decision.reason)
            return _message_reply("not_allowed", NOT_YOURS_TEXT)
        request = _Request(self._service, self._repository, _person_actor(person), action, body, self._editor)
        if action not in _CHANGES:
            try:
                return _READS[action](request)
            except Exception as error:  # noqa: BLE001 - every failure is answered and logged in _refusal
                return self._refusal(request, person, error)
        outcome = CommandOutcome(
            team_id=person.team_id, retry_key=str(verified.raw["idempotency_key"]), kind=verified.frame.kind,
            body_digest=str(verified.raw["body_digest"]), now=self._clock(), reply_for=_receipt, logger=self._logger,
            complete=False,
        )

        def attempt() -> dict[str, Any]:
            review_kinds, receipt = _CHANGERS[action](request, outcome)
            reply = _full_reply(request, receipt, review_kinds)
            self._repository.complete_command_outcome(outcome, reply)
            self._logger.info("A person changed the knowledge base from Web Chat. event=team_knowledge_changed "
                              "person=%s action=%s space=%s page=%s contribution_id=%s", person.person_id, action,
                              body.get("space"), body.get("page"), body.get("contribution_id"))
            return reply

        return carried_out_once(self._repository, outcome, attempt,
                                refusal_for=lambda error: self._refusal(request, person, error),
                                rebuild=lambda receipt: _full_reply(request, receipt["receipt"], ()),
                                logger=self._logger)

    def _editor(self, kind: str, actor_id: str, agent_type: str) -> dict[str, Any]:
        """Who changed something: an agent by type, a person by reference, or the system."""

        if kind == ActorKind.AGENT.value:
            return {"kind": "agent", "agent_type": agent_type or "Agent"}
        if kind == ActorKind.PERSON.value:
            reference = self._people.person_ref(actor_id)
            if reference is not None:
                return {"kind": "person", "person": reference}
            self._logger.warning("A knowledge change names a person the team's people records do not hold; shown as "
                                 "the system. event=team_knowledge_editor_unknown person=%s", actor_id)
        return {"kind": "system"}

    def _refusal(self, request: _Request, person: CommandPerson, error: Exception) -> dict[str, Any]:
        kind = f"knowledge.{request.action}"
        allowed = set(KINDS[kind].reply_errors)
        if isinstance(error, PageTooLong):
            self._logger.info("A Knowledge page request met text longer than Web Chat can show; nothing was changed. "
                              "event=team_knowledge_too_long person=%s action=%s length=%s limit=%s",
                              person.person_id, request.action, error.length, INLINE_TEXT_MAX)
            return _message_reply("runtime_error", str(error))
        if isinstance(error, KnowledgeError):
            outcome = _OUTCOMES.get(error.code, error.code)
            if outcome == "invalid_request" and "invalid_request" not in allowed and "not_found" in allowed:
                outcome = "not_found"
            if outcome in allowed:
                self._logger.info("A Knowledge page request was refused. event=team_knowledge_refused person=%s "
                                  "action=%s code=%s", person.person_id, request.action, error.code)
                return self._refusal_reply(request, outcome, error)
        self._logger.error("A Knowledge page request failed. event=team_knowledge_command_failed person=%s action=%s "
                           "error_type=%s", person.person_id, request.action, type(error).__name__, exc_info=error)
        return _message_reply("runtime_error", RUNTIME_ERROR_TEXT)

    def _refusal_reply(self, request: _Request, outcome: str, error: KnowledgeError) -> dict[str, Any]:
        reply = _message_reply(outcome, error.message)
        if outcome in {"changed_since_read", "page_exists"}:
            reply["current_version"] = str(int(error.details.get("current_revision") or 0))
        elif outcome == "merged":
            reply["merged_into"] = str(error.details.get("redirects_to") or "")
        elif outcome in {"review_required", "sensitive_content"}:
            reply["review_flags"] = self._flags_for(request, error, blocked=outcome == "sensitive_content")
        return reply

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
    """The request as the ownership rule reads it (``access_control.knowledge_access``)."""

    return KnowledgeCommandPayload(
        action=KnowledgeCommandAction(action),
        space=body.get("space", "team"),
        page=body.get("page", "") or "",
        contribution_id=body.get("contribution_id", "") or "",
    )


def _person_actor(person: CommandPerson) -> KnowledgeActor:
    """The person as the knowledge actor: their edits, their own personal space."""

    return KnowledgeActor(kind=ActorKind.PERSON, actor_id=person.person_id, team_id=person.team_id,
                          work_person_id=person.person_id)


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
        offset=body["offset"], status=PageStatus(body["status"]),
    )
    return _paged({"outcome": "ok", "pages": [_summary(hit.page, hit.snippet) for hit in hits]}, "pages",
                  body["offset"], total)


def _read(request: _Request) -> dict[str, Any]:
    return {"outcome": "ok", "page": _page(request, request.reference, revision=int(request.body.get("revision", 0)))}


def _history(request: _Request) -> dict[str, Any]:
    offset, limit = int(request.body["offset"]), int(request.body["limit"])
    page, revisions = request.service.history(request.actor, request.reference, limit=min(HISTORY_MAX, offset + limit))
    shown = [_revision(request, revision) for revision in revisions[offset:offset + limit]]
    return _paged({"outcome": "ok", "page": _page(request, page.reference()), "revisions": shown}, "revisions",
                  offset, page.revision)


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


# -- changes ------------------------------------------------------------------------
# Each returns the review kinds the person confirmed and the change's receipt.


def _write(request: _Request, outcome: CommandOutcome) -> tuple[tuple[str, ...], dict[str, Any]]:
    body = request.body
    # A new page is named after its title unless the page names itself.
    reference = f"{body['space']}/{body.get('page') or body.get('title', '')}"
    base_revision = _version(body.get("seen_version"))
    if base_revision:
        # An edit replaces the page's text (or keeps it with new details); either way the person must have seen it.
        _require_page_fits(request, reference)
    result = request.service.write(request.actor, reference, _draft(body), base_revision=base_revision,
                                   confirm_review=bool(body["confirm_review"]), command_outcome=outcome)
    return result.review_kinds, _page_receipt(result.page)


def _add_note(request: _Request, outcome: CommandOutcome) -> tuple[tuple[str, ...], dict[str, Any]]:
    # The reply shows the page with the new note.
    _require_page_fits(request, request.reference)
    result = request.service.add_note(request.actor, request.reference, _text(request.body["note"]),
                                      confirm_review=bool(request.body["confirm_review"]), command_outcome=outcome)
    assert result.note is not None and result.page is not None
    return result.review_kinds, {**_page_receipt(result.page), "note_id": result.note.note_id}


def _archive(request: _Request, outcome: CommandOutcome) -> tuple[tuple[str, ...], dict[str, Any]]:
    # Archiving takes the page out of lists and searches: never one the person could not open.
    _require_page_fits(request, request.reference)
    result = request.service.archive(request.actor, request.reference,
                                     base_revision=_version(request.body["seen_version"]),
                                     reason=request.body["reason"], command_outcome=outcome)
    return (), _page_receipt(result.page)


def _revert(request: _Request, outcome: CommandOutcome) -> tuple[tuple[str, ...], dict[str, Any]]:
    body = request.body
    # Both the current text and the revision that replaces it must be texts the person could open.
    _require_page_fits(request, request.reference, revision=int(body["to_revision"]))
    result = request.service.revert(request.actor, request.reference, to_revision=int(body["to_revision"]),
                                    base_revision=_version(body["seen_version"]), reason=body["reason"],
                                    command_outcome=outcome)
    return (), _page_receipt(result.page)


def _publish_contribution(request: _Request, outcome: CommandOutcome) -> tuple[tuple[str, ...], dict[str, Any]]:
    body = request.body
    edited = bool(body["edited"])
    contribution = _proposal(request)
    if contribution is not None:
        _require_proposal_fits(request, contribution)
        if contribution.kind is ContributionKind.CREATE and _version(body.get("seen_version")):
            # A proposed new page published as an edit of a page that took its name replaces that page's text.
            _require_page_fits(request, _published_name(body, contribution), subject=PROPOSED_PAGE_TEXT,
                               still=CURATOR_STILL)
    result = request.service.publish_contribution(
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
    receipt = _page_receipt(result.page)
    if result.note is not None:
        receipt["note_id"] = result.note.note_id
    return (), receipt


def _withdraw_contribution(request: _Request, outcome: CommandOutcome) -> tuple[tuple[str, ...], dict[str, Any]]:
    contribution = _proposal(request)
    if contribution is not None:
        # Nobody drops a proposal they could not read.
        _require_proposal_fits(request, contribution)
    withdrawn = request.service.withdraw_contribution(request.actor, request.body["contribution_id"],
                                                      command_outcome=outcome)
    return (), {"contribution_id": withdrawn.contribution_id}


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


_CHANGERS: dict[str, Callable[[_Request, CommandOutcome], tuple[tuple[str, ...], dict[str, Any]]]] = {
    "write": _write,
    "add_note": _add_note,
    "archive": _archive,
    "revert": _revert,
    "publish_contribution": _publish_contribution,
    "withdraw_contribution": _withdraw_contribution,
}


def _receipt(result: Any) -> dict[str, Any]:
    """What a change stored in its transaction: what it changed, by id (``CommandOutcome.reply_for``)."""

    if isinstance(result, KnowledgePage):
        return {"receipt": _page_receipt(result)}
    if isinstance(result, KnowledgeNote):
        return {"receipt": {"page_id": result.page_id, "note_id": result.note_id}}
    if isinstance(result, KnowledgeContribution):
        return {"receipt": {"contribution_id": result.contribution_id}}
    raise TypeError(f"A knowledge change returned {type(result).__name__}.")


def _page_receipt(page: Optional[KnowledgePage]) -> dict[str, Any]:
    if page is None:
        raise RuntimeError("The knowledge service changed no page for a person's direct change.")
    return {"page_id": page.page_id}


def _full_reply(request: _Request, receipt: dict[str, Any], review_kinds: Iterable[str]) -> dict[str, Any]:
    """A change's reply from its receipt, read as the change left it."""

    if request.action == "withdraw_contribution":
        contribution = request.service.get_contribution(request.actor, receipt["contribution_id"])
        return {"outcome": "ok", "contribution": _contribution(request, contribution)}
    page = request.repository.get_page_by_id(receipt["page_id"])
    if page is None:
        raise KnowledgeError(CODE_NOT_FOUND, "The changed page no longer exists.")
    reply: dict[str, Any] = {"outcome": "ok", "page": _page(request, page.reference())}
    if request.action in {"write", "add_note"}:
        reply["review_flags"] = _review_flags(review_kinds)
    if receipt.get("note_id"):
        notes = request.repository.list_notes(page.page_id, status=None, limit=HISTORY_MAX)
        note = next((note for note in notes if note.note_id == receipt["note_id"]), None)
        if note is not None:
            reply["note"] = _note(request, note)
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
    page, shown = view.page, view.revision
    _page_latest, latest = request.service.history(request.actor, page.reference(), limit=1)
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
        "updated_by": _revision_editor(request, latest[0]) if latest else {"kind": "system"},
        "backlinks": [_summary(linked) for linked in view.backlinks],
        "open_notes": [_note(request, note, any_editor=True) for note in view.open_notes],
        "pending_contributions": [
            {"contribution_id": c.contribution_id, "kind": c.kind.value, "created_at": _timestamp(c.created_at_utc)}
            for c in view.pending_contributions
        ],
        "links": [{"written": link.written, "space": link.space, "page": link.slug, "exists": link.exists}
                  for link in request.service.page_links(page, source.body)],
    }
    if page.frozen_until_utc:
        detail["frozen_until"] = _timestamp(page.frozen_until_utc)
    if shown is not None:
        detail["shown_revision"] = shown.revision
        detail["shown_by"] = _revision_editor(request, shown)
    if view.redirected_from:
        detail["redirected_from"] = view.redirected_from
    return detail


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
        editor = {"kind": "agent", "agent_type": note.actor_agent_type or "Agent"}
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
        "author": {"kind": "agent", "agent_type": contribution.actor_agent_type or "Agent"},
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
                       still: str = AGENTS_STILL) -> None:
    """Before a change: the page's text and open notes, and the revision a revert restores, all fit.

    Checked before anything is stored, so a person never replaces, hides or reverts
    text they could not open here, and the change's reply can show its result. A page
    that cannot be read is not checked: the change itself then refuses, with its reason.
    """

    try:
        view = request.service.read(request.actor, reference, revision=revision)
    except KnowledgeError:
        return
    if view.revision is not None:
        # A revert replaces the current text with the revision's.
        _require_fits(view.page.body, subject, still)
    _require_shown(view, subject=subject, still=still)


def _require_proposal_fits(request: _Request, contribution: KnowledgeContribution) -> None:
    """A proposal as Web Chat shows it (``_contribution``): its text, and the team page it changes, fit.

    Checked before the person reads, publishes or withdraws it, so nobody decides a
    proposal they could not read.
    """

    _require_fits(contribution.body, PROPOSAL_TEXT, CURATOR_STILL)
    if contribution.kind is not ContributionKind.CREATE:
        _require_page_fits(request, f"{KnowledgeSpace.TEAM.value}/{contribution.slug}", subject=PROPOSED_PAGE_TEXT,
                           still=CURATOR_STILL)


def _review_flags(kinds: Iterable[str]) -> list[dict[str, Any]]:
    labels = {**REVIEW_KIND_LABELS, PERSONAL_OVERLAP_KIND: PERSONAL_OVERLAP_LABEL}
    return [_flag(kind, labels.get(kind, kind.replace("_", " "))) for kind in kinds]


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

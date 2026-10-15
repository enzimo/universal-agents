"""The manager's side of the Web Chat Knowledge page (``ManagerKnowledgeCommand``).

People read and change the knowledge base from Web Chat; the store lives in
the manager's database, so the gateway sends each request here. Rules:

- **Who acts.** The person comes only from the command's gateway-signed
  context (``manager_control_runtime.verified_command_person``); the command
  names nobody. The knowledge service runs as that person
  (``ActorKind.PERSON``, ``work_person_id`` the same person), so ``personal/``
  is always their own space and there is no way to name another person's.
- **What they may touch** is decided by ``access_control.knowledge_access``
  (``access_control.ownership``): every team member reads and edits team
  pages; personal pages and pending contributions are the person's own.
- **What the service decides.** A person's team edit publishes directly
  (no contribution) and is not rate limited; the never-stored rules apply to
  every write, and content held for review needs the person's explicit
  confirmation (``confirm_review``). A person publishes or withdraws only
  their own pending contributions.
- **What trusted code writes.** Archive and revert reasons are fixed texts
  from here, never typed by the browser, because they enter shared history.

Refusals carry a stable code and plain words; review flags and refused
content are described by kind, label and place, never by the text itself.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from access_control.knowledge_access import knowledge_command_access
from custom_agents.manager_control_runtime import (
    CommandPerson,
    _send_durable_response,
    verified_command_person,
)
from custom_messages.contracts.control_v1 import (
    KNOWLEDGE_READ_ACTIONS,
    KnowledgeCommandAction,
    KnowledgeCommandPayload,
    KnowledgeCommandResultPayload,
    KnowledgeContributionPayload,
    KnowledgeEditorPayload,
    KnowledgeLinkPayload,
    KnowledgeNotePayload,
    KnowledgePageDetailPayload,
    KnowledgePageSummaryPayload,
    KnowledgeReviewFlagPayload,
    KnowledgeRevisionPayload,
    ManagerKnowledgeCommand,
)
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.enums import MessageMetaType
from custom_tools.providers.knowledge import knowledge_service
from knowledge_base.records import (
    CODE_CONFLICT,
    CODE_DISABLED,
    CODE_EXISTS,
    CODE_MERGED,
    CODE_NOT_FOUND,
    CODE_NOT_PENDING,
    CODE_REVIEW_REQUIRED,
    CODE_SENSITIVE,
    KnowledgeActor,
    KnowledgeContribution,
    KnowledgeError,
    KnowledgeNote,
    KnowledgePage,
    KnowledgeRevision,
)
from knowledge_base.sensitive_content import REVIEW_KIND_LABELS, SensitiveContentRules
from knowledge_base.service import PERSONAL_OVERLAP_KIND, DraftInput, KnowledgeService, PageView
from knowledge_base.vocabulary import ActorKind, ContributionKind, KnowledgeSpace, PageStatus
from runtime_settings.knowledge import knowledge_settings

logger = logging.getLogger(__name__)

# Fixed history texts for changes that need a reason (module docstring).
ARCHIVE_REASON = "Archived on the Web Chat Knowledge page"
REVERT_REASON = "Reverted on the Web Chat Knowledge page"
PERSONAL_OVERLAP_LABEL = "text copied from your own memories or personal pages"
KNOWLEDGE_OFF_TEXT = (
    "The knowledge base is turned off for this team (agent.knowledge.enabled). "
    "Ask an administrator to turn it on."
)
RUNTIME_ERROR_TEXT = (
    "The knowledge base could not finish this request. Nothing was changed. Try again; "
    "if it keeps failing, an administrator can find the reason in the manager's log."
)
NOT_YOURS_TEXT = "Only the person whose pages or proposals these are can open or change them."
_HISTORY_SHOWN = 50

# Service refusal codes, as the command's stable error codes.
_ERROR_CODES = {
    CODE_CONFLICT: "conflict",
    CODE_NOT_PENDING: "conflict",
    CODE_EXISTS: "page_exists",
    CODE_MERGED: "merged",
    CODE_REVIEW_REQUIRED: "review_required",
    CODE_SENSITIVE: "sensitive_content",
    CODE_NOT_FOUND: "not_found",
    CODE_DISABLED: "knowledge_off",
}
_DEFAULT_ERROR_CODE = "invalid_request"


@dataclass(frozen=True)
class _Request:
    service: KnowledgeService
    actor: KnowledgeActor
    payload: KnowledgeCommandPayload

    @property
    def reference(self) -> str:
        return f"{self.payload.space}/{self.payload.page}"


# -- the command -------------------------------------------------------------------


async def handle_manager_knowledge_command(
    agent: Any,
    *,
    command: ManagerKnowledgeCommand,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> None:
    """Run one Knowledge page request for the signed-in person (module docstring) and answer."""

    if command.command_type == MessageMetaType.RESPONSE:
        return
    response = command.model_copy(deep=True)
    response.command_type = MessageMetaType.RESPONSE
    # The answer does not repeat the request's text back.
    response.command = command.command.model_copy(update={"body": "", "note": "", "summary": ""})
    person, refusal = verified_command_person(agent, msg_meta)
    if not getattr(agent, "is_team_lead", False) or person is None:
        _refuse(response, "authorization_denied", refusal or "manager_required")
    else:
        await _run(agent, command, response, person)
    await _send_durable_response(agent, content=response, incoming_header=msg_header, incoming_meta=msg_meta)


async def _run(agent: Any, command: ManagerKnowledgeCommand, response: ManagerKnowledgeCommand, person: CommandPerson) -> None:
    payload = command.command
    decision = knowledge_command_access(
        payload, person_id=person.canonical_user_id, team_id=person.team_id, roles=person.roles
    )
    if not decision.allowed:
        logger.warning(
            "Refused a Knowledge page request. event=manager_knowledge_authorization_denied "
            "command_id=%s person_id=%s action=%s space=%s reason=%s",
            command.command_id,
            person.canonical_user_id,
            payload.action.value,
            payload.space,
            decision.reason,
        )
        _refuse(response, "authorization_denied", NOT_YOURS_TEXT)
        return
    if getattr(agent, "_knowledge_repository", None) is None:
        _refuse(response, "knowledge_off", KNOWLEDGE_OFF_TEXT)
        return
    try:
        request = _Request(knowledge_service(agent), _person_actor(agent, person), payload)
        response.result = await asyncio.to_thread(_ACTIONS[payload.action], request)
        response.success = True
        if payload.action not in KNOWLEDGE_READ_ACTIONS:
            logger.info(
                "A person changed the knowledge base from Web Chat. event=manager_knowledge_changed "
                "command_id=%s person_id=%s action=%s space=%s page=%s contribution_id=%s",
                command.command_id,
                person.canonical_user_id,
                payload.action.value,
                payload.space,
                payload.page,
                payload.contribution_id,
            )
    except KnowledgeError as error:
        _refuse_knowledge_error(agent, response, error, payload)
        logger.info(
            "A Knowledge page request was refused. event=manager_knowledge_refused command_id=%s "
            "person_id=%s action=%s code=%s",
            command.command_id,
            person.canonical_user_id,
            payload.action.value,
            error.code,
        )
    except Exception:
        logger.warning(
            "A Knowledge page request failed. event=manager_knowledge_failed command_id=%s person_id=%s action=%s",
            command.command_id,
            person.canonical_user_id,
            payload.action.value,
            exc_info=True,
        )
        _refuse(response, "runtime_error", RUNTIME_ERROR_TEXT)


def _person_actor(agent: Any, person: CommandPerson) -> KnowledgeActor:
    """The signed-in person as the knowledge actor: their edits, their own personal space."""

    return KnowledgeActor(
        kind=ActorKind.PERSON,
        actor_id=person.canonical_user_id,
        team_id=str(getattr(agent, "team_id", "") or person.team_id),
        work_person_id=person.canonical_user_id,
    )


def _refuse(response: ManagerKnowledgeCommand, code: Any, message: str) -> None:
    response.success = False
    response.error_code = code
    response.error_message = message


def _refuse_knowledge_error(
    agent: Any, response: ManagerKnowledgeCommand, error: KnowledgeError, payload: KnowledgeCommandPayload
) -> None:
    code = _ERROR_CODES.get(error.code, _DEFAULT_ERROR_CODE)
    _refuse(response, code, error.message)
    current = error.details.get("current_revision")
    response.current_revision = int(current) if isinstance(current, int) else 0
    response.merged_into = str(error.details.get("redirects_to") or "") if code == "merged" else ""
    if code in {"review_required", "sensitive_content"}:
        response.review_flags = _flags_for(agent, error, payload, blocked=code == "sensitive_content")


# -- review flags ------------------------------------------------------------------


def _flags_for(
    agent: Any, error: KnowledgeError, payload: KnowledgeCommandPayload, *, blocked: bool
) -> list[KnowledgeReviewFlagPayload]:
    """Where the flagged or refused content is, field by field, without its text.

    The service reports kinds (review) or positions in the text it checked
    (refusals); scanning the person's own fields again with the same rules
    names the field as well, so the page can say "the body, line 3".
    """

    settings = knowledge_settings(getattr(agent, "knowledge_config", None))
    rules = SensitiveContentRules.from_patterns(
        (pattern.name, pattern.pattern) for pattern in settings.sensitive_content.blocked_patterns
    )
    flags: list[KnowledgeReviewFlagPayload] = []
    for name, value in _checked_fields(payload):
        scan = rules.scan(value)
        for finding in scan.blocked if blocked else scan.review:
            flags.append(
                KnowledgeReviewFlagPayload(
                    kind=finding.kind, label=finding.label, field=name, line=finding.line, column=finding.column
                )
            )
    if blocked and not flags:
        # Stored text was refused (a revert or a contribution): positions without a field.
        flags = [
            KnowledgeReviewFlagPayload(
                kind=str(found.get("kind") or ""),
                label=str(found.get("kind") or "").replace("_", " "),
                line=int(found.get("line") or 0),
                column=int(found.get("column") or 0),
            )
            for found in error.details.get("found") or []
        ]
    if not blocked and PERSONAL_OVERLAP_KIND in (error.details.get("review_kinds") or []):
        flags.append(KnowledgeReviewFlagPayload(kind=PERSONAL_OVERLAP_KIND, label=PERSONAL_OVERLAP_LABEL))
    return flags


def _checked_fields(payload: KnowledgeCommandPayload) -> Iterable[tuple[str, str]]:
    if payload.action is KnowledgeCommandAction.ADD_NOTE:
        return (("note", payload.note),)
    return (
        ("title", payload.title),
        ("summary", payload.summary or ""),
        ("body", payload.body),
        ("tags", ", ".join(payload.tags or ())),
    )


def _review_flags(kinds: Iterable[str]) -> list[KnowledgeReviewFlagPayload]:
    labels = {**REVIEW_KIND_LABELS, PERSONAL_OVERLAP_KIND: PERSONAL_OVERLAP_LABEL}
    return [KnowledgeReviewFlagPayload(kind=kind, label=labels.get(kind, kind.replace("_", " "))) for kind in kinds]


# -- what the page shows -----------------------------------------------------------


def _editor(kind: str, actor_id: str, agent_type: str) -> KnowledgeEditorPayload:
    """Who changed something: an agent type, a person (id for the gateway to name), or the system."""

    if kind == ActorKind.AGENT.value:
        return KnowledgeEditorPayload(kind="agent", agent_type=agent_type)
    if kind == ActorKind.PERSON.value:
        return KnowledgeEditorPayload(kind="person", person_id=actor_id)
    return KnowledgeEditorPayload(kind="system")


def _summary(page: KnowledgePage, snippet: str = "") -> KnowledgePageSummaryPayload:
    return KnowledgePageSummaryPayload(
        space=page.space.value,
        page=page.slug,
        title=page.title,
        page_type=page.page_type,
        summary=page.summary,
        tags=list(page.tags),
        revision=page.revision,
        status=page.status.value,
        updated_at_utc=page.updated_at_utc,
        snippet=snippet,
    )


def _note(note: KnowledgeNote) -> KnowledgeNotePayload:
    return KnowledgeNotePayload(
        note_id=note.note_id,
        body=note.body,
        created_at_utc=note.created_at_utc,
        editor=_editor(note.actor_kind, note.actor_id, note.actor_agent_type),
    )


def _revision(revision: KnowledgeRevision) -> KnowledgeRevisionPayload:
    return KnowledgeRevisionPayload(
        revision=revision.revision,
        title=revision.title,
        status=revision.status.value,
        change_kind=revision.change_kind.value,
        change_summary=revision.change_summary,
        created_at_utc=revision.created_at_utc,
        editor=_editor(revision.actor_kind, revision.actor_id, revision.actor_agent_type),
    )


def _detail(request: _Request, reference: str, *, revision: int = 0) -> KnowledgePageDetailPayload:
    """The page as the Knowledge page shows it: text, links in, open notes, last change."""

    view: PageView = request.service.read(request.actor, reference, revision=revision)
    page, shown = view.page, view.revision
    if shown is None:
        _page, latest = request.service.history(request.actor, reference, limit=1)
        shown_editor = _revision(latest[0]).editor if latest else None
    else:
        shown_editor = _revision(shown).editor
    source = shown or page
    return KnowledgePageDetailPayload(
        **_summary(page).model_dump(exclude={"title", "summary", "tags", "page_type"}),
        title=source.title,
        page_type=source.page_type,
        summary=source.summary,
        tags=list(source.tags),
        body=source.body,
        created_at_utc=page.created_at_utc,
        frozen_until_utc=page.frozen_until_utc or "",
        shown_revision=shown.revision if shown is not None else 0,
        last_editor=shown_editor,
        backlinks=[_summary(linked) for linked in view.backlinks],
        open_notes=[_note(note) for note in view.open_notes],
        pending_contributions=len(view.pending_contributions),
        links=[
            KnowledgeLinkPayload(written=link.written, space=link.space, page=link.slug, exists=link.exists)
            for link in request.service.page_links(page, source.body)
        ],
        redirected_from=view.redirected_from,
    )


def _contribution(request: _Request, contribution: KnowledgeContribution, *, full: bool) -> KnowledgeContributionPayload:
    """One proposal; ``full`` adds its text and the team page as it is now (for before and after)."""

    current = None
    if full and contribution.kind is not ContributionKind.CREATE:
        try:
            current = _detail(request, f"{KnowledgeSpace.TEAM.value}/{contribution.slug}")
        except KnowledgeError as error:
            if error.code != CODE_NOT_FOUND:
                raise
    return KnowledgeContributionPayload(
        contribution_id=contribution.contribution_id,
        kind=contribution.kind.value,
        page=contribution.slug,
        title=contribution.title,
        page_type=contribution.page_type,
        summary=contribution.summary,
        tags=list(contribution.tags),
        body=contribution.body if full else "",
        change_summary=contribution.change_summary,
        base_revision=contribution.base_revision,
        agent_type=contribution.actor_agent_type,
        created_at_utc=contribution.created_at_utc,
        review_flags=_review_flags(contribution.review_kinds),
        current=current,
    )


def _draft(payload: KnowledgeCommandPayload) -> DraftInput:
    return DraftInput(title=payload.title, body=payload.body, summary=payload.summary, tags=payload.tags)


# -- actions -----------------------------------------------------------------------


def _list(request: _Request) -> KnowledgeCommandResultPayload:
    payload = request.payload
    hits, total = request.service.browse(
        request.actor, query=payload.query, space=payload.space, limit=payload.limit, offset=payload.offset,
        status=PageStatus(payload.status),
    )
    return KnowledgeCommandResultPayload(
        action=payload.action, pages=[_summary(hit.page, hit.snippet) for hit in hits], total=total
    )


def _read(request: _Request) -> KnowledgeCommandResultPayload:
    detail = _detail(request, request.reference, revision=request.payload.revision)
    return KnowledgeCommandResultPayload(action=request.payload.action, page=detail)


def _history(request: _Request) -> KnowledgeCommandResultPayload:
    page, revisions = request.service.history(request.actor, request.reference, limit=_HISTORY_SHOWN)
    return KnowledgeCommandResultPayload(
        action=request.payload.action,
        pages=[_summary(page)],
        revisions=[_revision(revision) for revision in revisions],
        total=page.revision,
    )


def _write(request: _Request) -> KnowledgeCommandResultPayload:
    payload = request.payload
    # A new page is named after its title unless the page names itself.
    name = payload.page or payload.title
    result = request.service.write(
        request.actor,
        f"{payload.space}/{name}",
        _draft(payload),
        base_revision=payload.base_revision,
        confirm_review=payload.confirm_review,
    )
    return _changed(request, result.page, result.review_kinds)


def _add_note(request: _Request) -> KnowledgeCommandResultPayload:
    payload = request.payload
    result = request.service.add_note(
        request.actor, request.reference, payload.note, confirm_review=payload.confirm_review
    )
    return _changed(request, result.page, result.review_kinds)


def _archive(request: _Request) -> KnowledgeCommandResultPayload:
    result = request.service.archive(
        request.actor, request.reference, base_revision=request.payload.base_revision, reason=ARCHIVE_REASON
    )
    return _changed(request, result.page)


def _revert(request: _Request) -> KnowledgeCommandResultPayload:
    payload = request.payload
    result = request.service.revert(
        request.actor,
        request.reference,
        to_revision=payload.revision,
        base_revision=payload.base_revision,
        reason=REVERT_REASON,
    )
    return _changed(request, result.page)


def _changed(
    request: _Request, page: Optional[KnowledgePage], review_kinds: Iterable[str] = ()
) -> KnowledgeCommandResultPayload:
    if page is None:
        raise RuntimeError("The knowledge service changed no page for a person's direct change.")
    return KnowledgeCommandResultPayload(
        action=request.payload.action,
        page=_detail(request, page.reference()),
        review_flags=_review_flags(review_kinds),
    )


def _list_contributions(request: _Request) -> KnowledgeCommandResultPayload:
    payload = request.payload
    shown, total = request.service.browse_contributions(request.actor, limit=payload.limit, offset=payload.offset)
    return KnowledgeCommandResultPayload(
        action=payload.action,
        contributions=[_contribution(request, contribution, full=False) for contribution in shown],
        total=total,
    )


def _count_contributions(request: _Request) -> KnowledgeCommandResultPayload:
    return KnowledgeCommandResultPayload(
        action=request.payload.action, total=request.service.count_pending_contributions(request.actor)
    )


def _read_contribution(request: _Request) -> KnowledgeCommandResultPayload:
    contribution = request.service.get_contribution(request.actor, request.payload.contribution_id)
    return KnowledgeCommandResultPayload(
        action=request.payload.action, contribution=_contribution(request, contribution, full=True)
    )


def _publish_contribution(request: _Request) -> KnowledgeCommandResultPayload:
    payload = request.payload
    result = request.service.publish_contribution(
        request.actor,
        payload.contribution_id,
        draft=_draft(payload) if payload.edited else None,
        # Note proposals publish this text; page proposals use the draft.
        note=payload.body if payload.edited else None,
        # The revision of the team page the person compared the proposal with.
        base_revision=payload.base_revision,
    )
    return _changed(request, result.page)


def _withdraw_contribution(request: _Request) -> KnowledgeCommandResultPayload:
    withdrawn = request.service.withdraw_contribution(request.actor, request.payload.contribution_id)
    return KnowledgeCommandResultPayload(
        action=request.payload.action, contribution=_contribution(request, withdrawn, full=False)
    )


_ACTIONS: dict[KnowledgeCommandAction, Callable[[_Request], KnowledgeCommandResultPayload]] = {
    KnowledgeCommandAction.LIST: _list,
    KnowledgeCommandAction.READ: _read,
    KnowledgeCommandAction.HISTORY: _history,
    KnowledgeCommandAction.WRITE: _write,
    KnowledgeCommandAction.ADD_NOTE: _add_note,
    KnowledgeCommandAction.ARCHIVE: _archive,
    KnowledgeCommandAction.REVERT: _revert,
    KnowledgeCommandAction.LIST_CONTRIBUTIONS: _list_contributions,
    KnowledgeCommandAction.COUNT_CONTRIBUTIONS: _count_contributions,
    KnowledgeCommandAction.READ_CONTRIBUTION: _read_contribution,
    KnowledgeCommandAction.PUBLISH_CONTRIBUTION: _publish_contribution,
    KnowledgeCommandAction.WITHDRAW_CONTRIBUTION: _withdraw_contribution,
}


__all__ = ["ARCHIVE_REASON", "REVERT_REASON", "handle_manager_knowledge_command"]

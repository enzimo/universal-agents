"""The files a person sent in a conversation, and which of them a task may use.

The gateway stores every upload under ``state/uploads`` and sends its record
(a ``UserTaskAttachment``) with the message: a new request (``Task.attachments``),
guidance while a request runs (``TaskSteering.attachments``) or an answer to a
question. Trusted runtime code saves those records with the request's turn
(``save_turn_attachments``, ``storage.conversation_repository``) and in the
task's history, so a later step, a follow-up or a restarted runtime can name
and open them by id. Nothing here takes a record or a path from a model: models
name files only by ``attachment_id``, and ids are looked up only within the
scope this module computes.

Which files a task may use (``request_files`` then ``attachment_scope``):

- The request the task serves: itself, or for a plan step or an isolated
  recovery task the request it was made for, followed only through the task
  manager's own plan-step and recovery links (never delegation links).
- Always: that request's files, the files the person sent while it ran
  (guidance and answers, from the durable task history) and the task's own.
- The files saved with the request's conversation turn (also those another of
  its plan steps received) and the files of earlier turns only for the team
  lead, only for a top-level request, and only when the person the work is
  verified to be for may read that conversation (``access_control.ownership``).
  Specialists, including a worker's scheduled job run that continues its
  owner's conversation, get only their own task's files and the files the team
  lead chose for them.

An id the scope does not hold is "not found", with no hint that it exists in
another conversation.

Prompts name the same files (``DefaultContextEngine``, ``AttachmentScope.files``):
the request's files its message does not carry go with the current request
(``render_request_files``), and the team lead's prompt lists the newest 20
files of earlier turns in a section of its own (``render_earlier_files``),
whatever part of the conversation's history the prompt shows. Images are not
attached again; the model opens a file by id when it needs it.

Who may add files (``sent_files_refusal``): files that arrive while a
top-level task runs, with guidance or with an answer to a question, are kept
only when the message is verified to come from the person the task works for.
Any process on the team's NATS can address a message to the team lead, and
these files are saved with the person's conversation and offered for later
turns and delegation, so a message without that person's signed context
keeps its text and loses its files. Files with a question's answer are kept
only on the answer to a question the task itself asked, signed for the person
the task works for (``answer_files_refusal``): only the gateway sends them.
That holds for a task with a parent too, such as a specialist's escalation the
team lead asks the person about; its answer's files are saved with the turn of
the request the work serves (``answer_turn_task_id``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional

from pydantic import ValidationError

from access_control.ownership import PRIVATE_READ, RESOURCE_CONVERSATION, private_access
from custom_agents.access_runtime import verified_meta_person_id
from custom_agents.steering_history_events import (
    GUIDANCE_ACCEPTED_EVENT,
    GUIDANCE_STEERING_KEY,
    HISTORY_EVENT_KEY,
    QUERY_ANSWER_FILES_KEY,
)
from custom_messages.contracts.attachments_v1 import UserTaskAttachment
from custom_messages.enums import MessageMetaType, TaskHistoryType
from storage.conversation_repository import (
    ATTACHMENT_TASK_ID_KEY,
    ATTACHMENT_TURN_INDEX_KEY,
    CONVERSATION_ATTACHMENT_LIST_LIMIT,
    turn_attachment_summary,
)

logger = logging.getLogger(__name__)

# Ids the gateway makes (``att_<hex>`` and adapter object ids). Anything else
# is not an id this runtime gave out, so it is never looked up.
_ATTACHMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
# A plan step's recovery task is linked to the step, the step to its request.
_MAX_REQUEST_LINKS = 8
# A delegation names at most this many files; an error names at most this many
# unknown ids and lists at most CONVERSATION_ATTACHMENT_LIST_LIMIT files.
MAX_SELECTED_ATTACHMENTS = 20
_MAX_REPORTED_IDS = 10
_MAX_SHOWN_NAME_CHARS = 120
_NO_VERIFIED_PERSON = "no_verified_person"
# The message is verified for someone other than the person the task works for.
SENDER_OTHER_PERSON = "other_person"
SENDER_NOT_VERIFIED = _NO_VERIFIED_PERSON
# Why files on a question's answer are not kept (``answer_files_refusal``).
ANSWER_NOT_A_RESPONSE = "not_an_answer"
ANSWER_TO_NO_QUESTION = "no_question"

UploadState = Literal["available", "deleted", "remote"]
UPLOAD_AVAILABLE: UploadState = "available"
UPLOAD_DELETED: UploadState = "deleted"
UPLOAD_REMOTE: UploadState = "remote"


def attachment_records(values: Iterable[Any]) -> list[dict[str, Any]]:
    """Validated records (``UserTaskAttachment`` JSON dumps) for saving, each id once, order kept."""

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        attachment = UserTaskAttachment.model_validate(value)
        if attachment.attachment_id not in seen:
            seen.add(attachment.attachment_id)
            records.append(attachment.model_dump(mode="json"))
    return records


def _validated(values: Iterable[Any], *, task_id: str, source: str) -> list[UserTaskAttachment]:
    """Records saved by trusted code; one that no longer validates is reported and skipped."""

    attachments: list[UserTaskAttachment] = []
    for value in values:
        try:
            attachments.append(UserTaskAttachment.model_validate(value))
        except ValidationError:
            logger.warning(
                "A saved file record could not be read; it is skipped. "
                "event=attachment_record_unreadable task_id=%s source=%s",
                task_id,
                source,
            )
    return attachments


def received_attachments(task: Any) -> list[UserTaskAttachment]:
    """Files the person sent while this task ran, from its durable history.

    Accepted guidance keeps its whole ``TaskSteering`` and an answer to a
    question keeps its files (``custom_agents.steering_history_events``), so
    the files are found after the guidance is drained and after a restart.

    Only the two kinds of entry this runtime records for the task itself are
    read: a guidance acceptance (``TASK_EVENT``) and an answer to a question
    (``QUERY_EVENT``). Entries copied from a subtask's result
    (``SUBTASK_EVENT``, ``TaskManager.bubble_subtask_result``) keep the
    details the worker that ran the subtask wrote, possibly in another
    process, so their files never count as sent by the person.
    """

    task_id = str(getattr(task, "task_id", "") or "")
    found: list[UserTaskAttachment] = []
    for entry in list(getattr(task, "execution_history", None) or []):
        entry_type = getattr(entry, "entry_type", None)
        details = getattr(entry, "details", None) or {}
        if entry_type == TaskHistoryType.TASK_EVENT and details.get(HISTORY_EVENT_KEY) == GUIDANCE_ACCEPTED_EVENT:
            steering = details.get(GUIDANCE_STEERING_KEY) or {}
            values = steering.get("attachments") if isinstance(steering, dict) else None
            found.extend(_validated(values or [], task_id=task_id, source="guidance"))
        elif entry_type == TaskHistoryType.QUERY_EVENT and details.get(QUERY_ANSWER_FILES_KEY):
            found.extend(_validated(details[QUERY_ANSWER_FILES_KEY], task_id=task_id, source="answer"))
    return found


async def _sender_refusal(agent: Any, person_id: Optional[str], msg_meta: Any) -> Optional[str]:
    """Why a message is not verified to come for ``person_id``; None when it is.

    The message's signed task context or schedule run identity
    (``access_runtime.verified_meta_person_id``) must name the person.
    ``SENDER_NOT_VERIFIED`` when nothing valid names a person (also for work
    for nobody), ``SENDER_OTHER_PERSON`` for someone else.
    """

    # Reading a schedule run identity may touch storage, so off the event loop.
    sender = await asyncio.to_thread(verified_meta_person_id, agent, msg_meta)
    if not sender:
        return SENDER_NOT_VERIFIED
    if sender != person_id:
        return SENDER_OTHER_PERSON
    return None


async def sent_files_refusal(agent: Any, task_info: Any, msg_meta: Any) -> Optional[str]:
    """Why files guidance carries for this task are not kept as its person's; None when they are.

    For a top-level task (a request, a plan step, a recovery task, a job
    run), whose files are saved with its conversation turn, the message must
    be verified to work for the person the task works for
    (``TaskInfo.owner_person_id``, recorded at admission; plan steps and
    recovery tasks keep their request's). The gateway signs every guidance
    message it sends for a person.

    A delegated subtask is not checked: guidance files for it never reach a
    conversation (``save_turn_attachments`` saves only a top-level request's),
    and the copies of guidance the team lead passes on to its workers carry no
    signed context.
    """

    if getattr(task_info.task, "parent_task_id", None) is not None:
        return None
    return await _sender_refusal(agent, task_info.owner_person_id, msg_meta)


async def answer_files_refusal(
    agent: Any, task_info: Any, query: Any, msg_meta: Any, *, asked_by_task: bool
) -> Optional[str]:
    """Why files on a ``TaskQuery`` are not kept as the person's answer; None when they are.

    Files come only with the person's answer (``RESPONSE``) to a question
    the task itself asked (its ``REQUEST`` is in the task's question list:
    ``asked_by_task``), signed for the person the task works for
    (``TaskInfo.owner_person_id``). That holds for a task with a parent as
    well: a specialist's escalation (the specialists' way to ask the person,
    ``agent_instructions``) is a task the team lead holds with the specialist's
    task as its parent, its question goes to the person's chat
    (``message_routing.requester_bound_primary_route``), and the delegated
    task carries its request's signed context, so it works for that person.
    Only the gateway sends such answers: models cannot set files on a question
    or an answer (``ModelTaskQueryPayload``). So files on a question, on an
    answer to no question asked, or on an answer not signed for the task's
    person are not the person's.
    """

    if query.query_type != MessageMetaType.RESPONSE:
        return ANSWER_NOT_A_RESPONSE
    if not asked_by_task:
        return ANSWER_TO_NO_QUESTION
    return await _sender_refusal(agent, task_info.owner_person_id, msg_meta)


def _request_chain(task_manager: Any, task_id: str) -> list[str]:
    """``task_id`` then each task it was made for, up to the request it serves.

    Follows only the task manager's own plan-step (step to its request) and
    isolated-recovery (recovery task to the task it recovers) links, so a
    delegated subtask never reaches its requester's files this way.
    """

    chain = [task_id]
    for _ in range(_MAX_REQUEST_LINKS):
        current = chain[-1]
        step_link = task_manager.get_plan_step_task_link(current)
        recovery_link = None if step_link is not None else task_manager.get_recovery_link(current)
        linked = (
            getattr(step_link, "root_task_id", None)
            if step_link is not None
            else getattr(recovery_link, "original_task_id", None)
        )
        if not linked or linked in chain:
            return chain
        chain.append(str(linked))
    logger.warning(
        "A task's plan-step and recovery links did not end; its own request is taken as the last one reached. "
        "event=attachment_request_links_too_long task_id=%s last_task_id=%s",
        task_id,
        chain[-1],
    )
    return chain


@dataclass(frozen=True)
class RequestFiles:
    """The request a task serves and the files it holds, read from the task manager.

    Plain data, so it can be read in the task manager's operation lane.
    ``conversation_id`` is the request's own conversation, set only for a
    top-level request (only those are turns of a conversation).
    """

    task_id: str
    request_task_id: str
    conversation_id: Optional[str]
    files: tuple[UserTaskAttachment, ...]


def request_files(task_manager: Any, task_id: str) -> Optional[RequestFiles]:
    """The files a task may use without its conversation, and its request; None for an unknown task.

    The request's message files come first, then the files received while
    each task of the chain ran, each id once.
    """

    if task_manager.get_task_info(task_id) is None:
        return None
    chain = _request_chain(task_manager, task_id)
    infos = [(chain_id, task_manager.get_task_info(chain_id)) for chain_id in chain]
    known = [(chain_id, info) for chain_id, info in infos if info is not None]
    if len(known) != len(infos):
        logger.warning(
            "A task's request is no longer held; only the files of the tasks still held are used. "
            "event=attachment_request_missing task_id=%s missing_task_ids=%s",
            task_id,
            ",".join(chain_id for chain_id, info in infos if info is None),
        )
    request_id, request_info = known[-1]
    files: dict[str, UserTaskAttachment] = {}
    for _chain_id, info in reversed(known):
        for attachment in [*list(info.task.attachments or []), *received_attachments(info.task)]:
            files.setdefault(attachment.attachment_id, attachment)
    top_level = request_id == chain[-1] and getattr(request_info.task, "parent_task_id", None) is None
    conversation_id = getattr(request_info.meta, "conversation_id", None) if top_level else None
    return RequestFiles(
        task_id=task_id,
        request_task_id=request_id,
        conversation_id=str(conversation_id) if conversation_id else None,
        files=tuple(files.values()),
    )


@dataclass(frozen=True)
class SentFile:
    """A file the person sent, and the conversation turn it came with when that is known."""

    attachment: UserTaskAttachment
    turn_index: Optional[int] = None


def _sent_file(record: dict[str, Any], *, conversation_id: str) -> Optional[SentFile]:
    attachments = _validated([record], task_id=str(record.get("task_id") or ""), source=conversation_id)
    if not attachments:
        return None
    turn_index = record.get(ATTACHMENT_TURN_INDEX_KEY)
    return SentFile(attachments[0], turn_index=int(turn_index) if turn_index is not None else None)


@dataclass(frozen=True)
class ScopeFiles:
    """The files a task may name: the current request's, then earlier turns' (``AttachmentScope.files``)."""

    current: tuple[SentFile, ...]
    earlier: tuple[SentFile, ...]

    def available(self, limit: int = CONVERSATION_ATTACHMENT_LIST_LIMIT) -> list[SentFile]:
        """Current files first, then earlier ones, at most ``limit``."""

        return [*self.current, *self.earlier][: max(0, limit)]


@dataclass(frozen=True)
class AttachmentScope:
    """The files one task may name and open by id.

    ``conversation_id`` is set only when the earlier turns of the request's
    conversation may be read (module docstring); ``store`` is then the
    conversation store that holds them.
    """

    request: RequestFiles
    conversation_id: Optional[str] = None
    store: Any = field(default=None, repr=False, compare=False)

    def find(self, attachment_id: str) -> Optional[SentFile]:
        """The file with this id, or None when the id is not one this task may use."""

        wanted = str(attachment_id or "").strip()
        if not _ATTACHMENT_ID_PATTERN.fullmatch(wanted):
            return None
        for attachment in self.request.files:
            if attachment.attachment_id == wanted:
                return SentFile(attachment)
        if self.conversation_id is None:
            return None
        record = self.store.find_conversation_attachment(self.conversation_id, wanted)
        return _sent_file(record, conversation_id=self.conversation_id) if record else None

    def files(self) -> ScopeFiles:
        """The files this task may name, grouped for its prompt, read with one bounded listing.

        ``current``: the request's own files (``request.files``), then the
        files saved with its turn that another of its plan steps or recovery
        tasks received. ``earlier``: the newest ``CONVERSATION_ATTACHMENT_LIST_LIMIT``
        files of the other turns, newest first, however many files the request
        itself holds. Read with one query, so call this once per prompt.
        """

        own = tuple(SentFile(attachment) for attachment in self.request.files)
        if self.conversation_id is None:
            return ScopeFiles(current=own, earlier=())
        this_turn: list[SentFile] = []
        earlier: list[SentFile] = []
        listed = self.store.list_conversation_attachments(
            self.conversation_id,
            limit=CONVERSATION_ATTACHMENT_LIST_LIMIT,
            current_task_id=self.request.request_task_id,
            skip_ids={attachment.attachment_id for attachment in self.request.files},
        )
        for record in listed:
            sent = _sent_file(record, conversation_id=self.conversation_id)
            if sent is None:
                continue
            same_turn = str(record.get(ATTACHMENT_TASK_ID_KEY) or "") == self.request.request_task_id
            (this_turn if same_turn else earlier).append(sent)
        return ScopeFiles(current=(*own, *this_turn), earlier=tuple(earlier))

    def available(self, limit: int = CONVERSATION_ATTACHMENT_LIST_LIMIT) -> list[SentFile]:
        """The files to offer when an id is not found: the current request's, then earlier ones."""

        return self.files().available(limit)


def attachment_scope(
    request: RequestFiles,
    *,
    conversation_store: Any,
    is_team_lead: bool,
    person_id: Optional[str],
) -> AttachmentScope:
    """What a task may open: its request's files, plus its conversation's for the team lead's own person.

    ``person_id`` is the person the task is verified to work for (its signed
    context or schedule run identity), never a field from a message body.
    """

    conversation_id = request.conversation_id
    if not conversation_id or not is_team_lead or conversation_store is None:
        return AttachmentScope(request)
    refusal = _conversation_refusal(conversation_store, conversation_id, person_id)
    if refusal is not None:
        # Work for nobody (system work) never reads a person's conversation;
        # anything else is unexpected for a request's own conversation.
        logger.log(
            logging.INFO if refusal == _NO_VERIFIED_PERSON else logging.WARNING,
            "A task's earlier conversation files were not offered: its person could not be shown to own the "
            "conversation. event=attachment_scope_conversation_refused task_id=%s request_task_id=%s "
            "conversation_id=%s reason=%s",
            request.task_id,
            request.request_task_id,
            conversation_id,
            refusal,
        )
        return AttachmentScope(request)
    return AttachmentScope(request, conversation_id=conversation_id, store=conversation_store)


def task_attachment_scope(
    task_manager: Any,
    task_id: str,
    *,
    conversation_store: Any,
    is_team_lead: bool,
    person_id: Optional[str],
) -> Optional[AttachmentScope]:
    """``attachment_scope`` of a task this agent holds; None for a task it does not hold.

    Reads the task manager, so call it in the task-manager lane.
    """

    request = request_files(task_manager, task_id)
    if request is None:
        return None
    return attachment_scope(
        request, conversation_store=conversation_store, is_team_lead=is_team_lead, person_id=person_id
    )


def _conversation_refusal(conversation_store: Any, conversation_id: str, person_id: Optional[str]) -> Optional[str]:
    """Why the person may not read this conversation (None when they may), decided by ``access_control.ownership``."""

    if not person_id:
        return _NO_VERIFIED_PERSON
    thread = conversation_store.get_thread(conversation_id)
    if not thread:
        return "no_thread"
    decision = private_access(
        person_id=person_id,
        owner_id=thread.get("owner_canonical_user_id"),
        resource_type=RESOURCE_CONVERSATION,
        resource_id=conversation_id,
        action=PRIVATE_READ,
        team_id=thread.get("team_id"),
    )
    return None if decision.allowed else decision.reason


@dataclass(frozen=True)
class UploadLocation:
    """Whether this runtime holds a file, and its real path when it does (``locate_upload``)."""

    state: UploadState
    path: Optional[Path] = None


def locate_upload(attachment: UserTaskAttachment, uploads_dir: Path) -> UploadLocation:
    """Where this runtime holds the file: ``available`` with its real path, ``deleted`` or ``remote``.

    A local copy counts only inside this instance's ``state/uploads``
    (``InstanceLayout.uploads_dir``), compared as real paths. Inside it but
    gone means the upload was removed (its retention ended, or its person was
    removed). A path anywhere else is not a copy this runtime may open: the
    file was stored on another machine, or the record is not one the gateway
    made. Agents cannot write in ``state/uploads``, so the real path checked
    here is the file that is opened.
    """

    path = attachment.local_path()
    if not path:
        return UploadLocation(UPLOAD_REMOTE)
    root = os.path.realpath(uploads_dir)
    resolved = os.path.realpath(path)
    if os.path.commonpath([root, resolved]) != root or resolved == root:
        logger.debug(
            "A file's stored copy is outside this runtime's uploads folder. "
            "event=attachment_outside_uploads attachment_id=%s",
            attachment.attachment_id,
        )
        return UploadLocation(UPLOAD_REMOTE)
    if not os.path.isfile(resolved):
        return UploadLocation(UPLOAD_DELETED)
    return UploadLocation(UPLOAD_AVAILABLE, Path(resolved))


def upload_state(attachment: UserTaskAttachment, uploads_dir: Path) -> UploadState:
    """Whether this runtime holds the file: ``available``, ``deleted`` or ``remote`` (``locate_upload``)."""

    return locate_upload(attachment, uploads_dir).state


def attachment_summary(sent: SentFile | UserTaskAttachment) -> dict[str, Any]:
    """A file as tool results show it: id, name, type, kind, size and its turn when known; never its storage."""

    file = sent if isinstance(sent, SentFile) else SentFile(sent)
    summary = turn_attachment_summary(file.attachment.model_dump(mode="json"))
    if file.turn_index is not None:
        summary[ATTACHMENT_TURN_INDEX_KEY] = file.turn_index
    return summary


def _shown_name(filename: str) -> str:
    """The file name as one line of bounded length (names are the person's text)."""

    single_line = " ".join(str(filename or "").split())
    if len(single_line) > _MAX_SHOWN_NAME_CHARS:
        return single_line[: _MAX_SHOWN_NAME_CHARS - 1] + "…"
    return single_line or "unnamed file"


def _shown_size(size_bytes: int) -> str:
    size = max(0, int(size_bytes or 0))
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def describe_attachment(sent: SentFile | UserTaskAttachment) -> str:
    """One line naming a file for a model: name, id, kind, type, size and its turn when known. Never its path."""

    file = sent if isinstance(sent, SentFile) else SentFile(sent)
    attachment = file.attachment
    turn = f", sent in turn {file.turn_index}" if file.turn_index is not None else ""
    return (
        f"{_shown_name(attachment.filename)} (attachment_id {attachment.attachment_id}, {attachment.kind}, "
        f"{attachment.content_type}, {_shown_size(attachment.size_bytes)}{turn})"
    )


def render_attachment_lines(files: Iterable[SentFile | UserTaskAttachment]) -> list[str]:
    """``- <description>`` for each file, in order."""

    return [f"- {describe_attachment(file)}" for file in files]


# How a prompt names the files of a request and of its conversation's earlier
# turns (``render_request_files``, ``render_earlier_files``).
REQUEST_FILES_HEADING = "Other Files For This Request"
EARLIER_FILES_HEADING = "Earlier Files In This Conversation"
_OPEN_BY_ID = (
    "Open one by attachment_id with inspect_user_attachment, or with load_local_media_for_model_context when "
    "you need to see an image or document yourself."
)


def render_request_files(files: ScopeFiles, *, shown_ids: Iterable[str]) -> list[str]:
    """Prompt lines for the request's files that its message does not show; [] when there are none.

    These are files the person sent with the request a plan step or recovery
    task serves, or while the request ran: with guidance, with an answer to
    a question, or while another of its plan steps ran. The message's own
    files are listed with the message (``Current Request Attachments``).
    """

    shown = set(shown_ids)
    rest = [file for file in files.current if file.attachment.attachment_id not in shown]
    if not rest:
        return []
    return [
        f"{REQUEST_FILES_HEADING} (sent with the request, with guidance or with an answer to a question; "
        f"{_OPEN_BY_ID}):",
        *render_attachment_lines(rest),
    ]


def render_earlier_files(files: ScopeFiles) -> str:
    """The prompt section naming the files of the conversation's earlier turns; "" when there are none.

    At most ``CONVERSATION_ATTACHMENT_LIST_LIMIT`` files, newest first, each
    with its turn (``AttachmentScope.files``). It does not depend on how much
    of the conversation's history the prompt shows, and it names files only:
    images are not attached to the prompt again.
    """

    if not files.earlier:
        return ""
    return "\n".join(
        [
            f"{EARLIER_FILES_HEADING} (newest first, at most {CONVERSATION_ATTACHMENT_LIST_LIMIT}; "
            f"they are not attached again. {_OPEN_BY_ID}):",
            *render_attachment_lines(files.earlier),
        ]
    )


class AttachmentSelectionError(ValueError):
    """A delegation named files the task may not use, or too many; the message says which and what it may use."""

    def __init__(self, message: str, *, unknown_ids: list[str], deleted_ids: list[str]) -> None:
        super().__init__(message)
        self.unknown_ids = unknown_ids
        self.deleted_ids = deleted_ids


def _named_ids(ids: list[str]) -> str:
    shown = ", ".join(ids[:_MAX_REPORTED_IDS])
    more = len(ids) - _MAX_REPORTED_IDS
    return f"{shown} and {more} more" if more > 0 else shown


def select_attachments(scope: AttachmentScope, attachment_ids: list[str], *, uploads_dir: Path) -> list[UserTaskAttachment]:
    """The exact files a delegation names, each once, in the order named; ``[]`` selects none.

    Raises ``AttachmentSelectionError`` when it names more than
    ``MAX_SELECTED_ATTACHMENTS`` files, an id this task may not use, or a file
    whose upload is gone; the message lists the files it may use.
    """

    wanted = list(dict.fromkeys(str(value or "").strip() for value in attachment_ids))
    if len(wanted) > MAX_SELECTED_ATTACHMENTS:
        raise AttachmentSelectionError(
            f"A delegation can pass at most {MAX_SELECTED_ATTACHMENTS} files; {len(wanted)} were named.",
            unknown_ids=[],
            deleted_ids=[],
        )
    selected: list[UserTaskAttachment] = []
    unknown: list[str] = []
    deleted: list[str] = []
    for attachment_id in wanted:
        found = scope.find(attachment_id)
        if found is None:
            unknown.append(attachment_id)
        elif upload_state(found.attachment, uploads_dir) == UPLOAD_DELETED:
            deleted.append(attachment_id)
        else:
            selected.append(found.attachment)
    if not unknown and not deleted:
        return selected
    problems = []
    if unknown:
        problems.append(
            f"No file with attachment_id {_named_ids(unknown)} was sent with this request or earlier in its conversation."
        )
    if deleted:
        problems.append(f"The file with attachment_id {_named_ids(deleted)} is no longer stored.")
    available = scope.available()
    listing = (
        "Files you can pass:\n" + "\n".join(render_attachment_lines(available))
        if available
        else "No files can be passed for this request."
    )
    raise AttachmentSelectionError(" ".join(problems) + "\n" + listing, unknown_ids=unknown, deleted_ids=deleted)


def save_turn_attachments(
    task_id: str,
    attachments: Iterable[Any],
    *,
    task_manager: Any,
    conversation_store: Any,
) -> Optional[list[dict[str, Any]]]:
    """Save files the person sent while ``task_id`` ran with the turn of the request it serves.

    A plan step's or a recovery task's files go to the turn of the request
    it was made for, so later steps and follow-ups find them. Only top-level
    requests are turns of a conversation; a subtask's files stay in its own
    history. The merge adds each id once, so a resent message adds nothing.
    Returns the turn's saved records, or None when nothing was saved. Store
    errors propagate, so the caller records nothing and the message is sent
    again.
    """

    records = attachment_records(attachments)
    if not records or conversation_store is None:
        return None
    request = request_files(task_manager, task_id)
    if request is None or request.conversation_id is None:
        return None
    merged: Optional[list[dict[str, Any]]] = conversation_store.merge_turn_attachments(
        conversation_id=request.conversation_id,
        task_id=request.request_task_id,
        attachments=records,
    )
    if merged is None:
        logger.warning(
            "Files the person sent were not saved with a conversation turn: the request has no turn. "
            "event=conversation_turn_missing_for_attachments task_id=%s request_task_id=%s conversation_id=%s "
            "attachment_ids=%s",
            task_id,
            request.request_task_id,
            request.conversation_id,
            ",".join(record["attachment_id"] for record in records),
        )
    return merged


def answer_turn_task_id(task_manager: Any, task_id: str) -> str:
    """The task whose request's conversation turn keeps the files of an answer given on ``task_id``.

    The person answers the team lead's question in the chat of the request
    the asking task serves, so the files go with that request's turn and a
    later turn can open them. The request is reached through this task
    manager's own links (``TaskManager.resolve_root_task_id``: plan-step,
    recovery and parent links between tasks it holds), for example a
    specialist's escalation, then the task the team lead delegated to that
    specialist, then the person's request. The escalation names its parent
    itself, so the request is used only when it works for the same person as
    the asking task, whom the answer was verified for
    (``answer_files_refusal``). Otherwise, and when the asking task serves no
    request this agent holds (a specialist's own scheduled run), the files
    stay with the asking task's history only. Read in the task-manager lane.
    """

    request_id = task_manager.resolve_root_task_id(task_id) or task_id
    if request_id == task_id:
        return task_id
    asking = task_manager.get_task_info(task_id)
    request = task_manager.get_task_info(request_id)
    if asking is None or request is None:
        # A request no longer held is reported where the files are saved
        # (``request_files``, event=attachment_request_missing).
        return task_id
    if request.owner_person_id == asking.owner_person_id:
        return request_id
    logger.warning(
        "An answer's files are kept with the task that asked, not with its request's turn: the request does not "
        "work for the same person. event=answer_files_request_other_person task_id=%s request_task_id=%s",
        task_id,
        request_id,
    )
    return task_id


async def _save_in_lane(agent: Any, attachments: Iterable[Any], turn_task_id: Callable[[Any], str]) -> None:
    files = list(attachments)
    if not files:
        return
    task_manager = agent.task_manager
    conversation_store = getattr(agent, "_conversation_store", None)

    def save() -> Any:
        return save_turn_attachments(
            turn_task_id(task_manager), files, task_manager=task_manager, conversation_store=conversation_store
        )

    await task_manager.operations_runtime.run(save)


async def save_sent_files(agent: Any, task_id: str, attachments: Iterable[Any]) -> None:
    """Save files the person sent with guidance for ``task_id``, in the agent's task-manager lane.

    Runs before the message is recorded or answered: a store error raises,
    so nothing is recorded and the message comes again (the gateway resends
    held guidance). The merge adds each file once, so the next attempt saves
    only what is missing. One lane operation reads the task's request links
    and writes the turn, in order with the request's own admission.
    """

    await _save_in_lane(agent, attachments, lambda _task_manager: task_id)


async def save_answer_files(agent: Any, task_id: str, attachments: Iterable[Any]) -> None:
    """Save the files of the person's answer to a question asked on ``task_id`` (``answer_turn_task_id``).

    Like ``save_sent_files``: before the answer is recorded, so a store error
    leaves nothing recorded and the answer is redelivered or drained again
    from the runtime mailbox.
    """

    await _save_in_lane(agent, attachments, lambda task_manager: answer_turn_task_id(task_manager, task_id))

"""People's and agents' knowledge base commands at the team's address.

``knowledge.list``, ``.read``, ``.history``, ``.write``, ``.add_note``, ``.archive``,
``.revert``, ``.merge``, ``.list_contributions``, ``.count_contributions``,
``.read_contribution``, ``.publish_contribution`` and ``.withdraw_contribution``.
They run on the team's database through ``knowledge_base.service``:

- **Who acts** comes from the verified authority token only: gateway commands carry a
  command token naming the person by opaque reference (``team_services.people``);
  ``knowledge.merge`` from a lead or worker host carries a ``root_task`` token for either
  a person's personal pages or a system curation run on team pages.
- **What they may touch** is decided by ``access_control.knowledge_access`` for gateway
  commands and by ``knowledge_base.service`` for every operation.
- **Editors are named by reference.** A person who changed something is named by their
  opaque person reference; the gateway turns it into "You" or a display name, and a person
  the people records no longer hold (someone who left) has none and is shown as "A team
  member". An agent is named by its type, capped at 24 characters (plan decision 56); a
  longer one is shown as "Agent" and logged once (``_agent_editor``).
- **A change is carried out once.** Its receipt is stored with the change, in its
  transaction: what it changed, by id, and the review kinds the person confirmed, never
  page text. The reply shows the page as readers see it once the change is committed, so
  it is built from the receipt after the commit, on the first try and on every retry alike
  (``team_services.outcomes``). When building or storing that reply fails after commit,
  the request is answered as unknown (``AnswerUnknown``, ``handler_failed``), never as
  refused.
- **Long text and large pages travel via ``UA1_BLOBS`` (decision 77).** Incoming
  ``text_content`` blobs are verified and fetched before executing the command, with hard
  character ceilings (500,000 for page bodies, 50,000 for notes) and the team's configured
  ``agent.knowledge.limits.max_body_chars`` / ``max_note_chars`` enforced in
  ``KnowledgeService``. Outgoing replies spill any ``text_content`` over 32,768 characters
  (or when combined inline content exceeds ``REPLY_BODY_MAX_BYTES``) to ``text/plain``
  blobs, spill oversized detailed ``page`` objects to ``application/json`` blobs, and
  paginate list replies via ``next_offset``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Optional, cast

from access_control.knowledge_access import knowledge_command_access
from access_control.people_directory import PeopleDirectory
from custom_messages.contracts.control_v1 import KnowledgeCommandAction, KnowledgeCommandPayload
from custom_messages.wire.encoding import format_timestamp
from custom_messages.wire.frame import VerifiedFrame
from custom_messages.wire.ids import AGENT_ID
from custom_messages.wire.kinds import KINDS
from custom_messages.wire.sizes import REPLY_BODY_MAX_BYTES
from custom_transport.protocol1.blobs import BlobOwner, BlobStoreFull, BlobTooLarge
from custom_transport.protocol1.knowledge_blobs import (
    BlobStore,
    KnowledgeBlobUnavailable,
    TeamServicesBlobStore,
    decode_request_body,
    fit_list_rows,
    fit_reply_body,
)
from knowledge_base.records import (
    CODE_DISABLED,
    CODE_INVALID,
    CODE_MERGED,
    CODE_NOT_FOUND,
    ConfirmedChange,
    KnowledgeActor,
    KnowledgeContribution,
    KnowledgeError,
    KnowledgeNote,
    KnowledgePage,
    KnowledgeRevision,
)
from knowledge_base.sensitive_content import REVIEW_KIND_LABELS, SensitiveContentRules
from knowledge_base.service import PERSONAL_OVERLAP_KIND, DraftInput, KnowledgeService, knowledge_service_for
from knowledge_base.vocabulary import ActorKind, ContributionKind, KnowledgeSpace, PageStatus
from runtime_settings.knowledge import KnowledgeSettings
from storage.operation_outcomes import RECEIPT_KEY, CommandOutcome
from team_services.outcomes import AnswerUnknown, CommandOnce
from team_services.people import CommandPerson, PersonCheck, command_person
from team_services.service import OperationFamily

KNOWLEDGE_ACTIONS = (
    "list", "read", "history", "write", "add_note", "archive", "revert", "merge",
    "list_contributions", "count_contributions", "read_contribution",
    "publish_contribution", "withdraw_contribution",
)
KNOWLEDGE_KINDS = tuple(f"knowledge.{action}" for action in KNOWLEDGE_ACTIONS)
_CHANGES = frozenset({
    "write", "add_note", "archive", "revert", "merge",
    "publish_contribution", "withdraw_contribution",
})
_MERGE_ROLES = frozenset({"lead_host", "worker_host"})

PERSONAL_OVERLAP_LABEL = "text copied from your own memories or personal pages"
KNOWLEDGE_OFF_TEXT = (
    "The knowledge base is turned off for this team (agent.knowledge.enabled). Ask an administrator to turn it on."
)
RUNTIME_ERROR_TEXT = (
    "The knowledge base could not finish this request. Nothing was changed. Try again; if it keeps failing, an "
    "administrator can find the reason in the team services' log."
)
BLOB_STORE_FULL_TEXT = (
    "The team's temporary blob store is full, so this large Knowledge reply could not be sent right now. "
    "Ask an administrator to check transport.queues.max_bytes.blobs or agent.blob_retention_days."
)
BLOB_UNAVAILABLE_TEXT = (
    "This Knowledge reply requires the team's blob store (UA1_BLOBS), which is not available in this process."
)
NOT_YOURS_TEXT = "Only the person whose pages or proposals these are can open or change them."
_MESSAGE_MAX = 500
HISTORY_MAX = 50
FLAGS_MAX = 100
MORE_FLAGS_TEXT = "Only the first {shown} of {total:,} flagged places are listed."
_CONTRACT_AGENT_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,23}")
AGENT_FALLBACK_TYPE = "Agent"

_OUTCOMES = {CODE_INVALID: "invalid_request", CODE_DISABLED: "feature_off", CODE_MERGED: "merged"}
_LIST_ROWS = {"list": "pages", "list_contributions": "contributions", "history": "revisions"}


@dataclass(frozen=True)
class _ResolvedActor:
    actor: Optional[KnowledgeActor] = None
    person_check: Optional[PersonCheck] = None
    outcome_person_id: str = ""
    blob_owner: Optional[BlobOwner] = None
    check_gateway_access: bool = True
    refusal: Optional[dict[str, Any]] = None


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


@dataclass
class _Change:
    """One change and its stored receipt."""

    request: _Request
    stored: Optional[dict[str, Any]] = None

    def receipt(self, result: Any) -> dict[str, Any]:
        confirmed: tuple[str, ...] = ()
        if isinstance(result, ConfirmedChange):
            result, confirmed = result.result, tuple(result.review_kinds)
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


def _as_blob_store(blobs: Any) -> Optional[BlobStore]:
    if blobs is None:
        return None
    if hasattr(blobs, "put_and_register"):
        return TeamServicesBlobStore(blobs)
    return cast(BlobStore, blobs)


def _blob_failure_message(error: Exception) -> str:
    if isinstance(error, BlobStoreFull):
        return BLOB_STORE_FULL_TEXT
    if isinstance(error, KnowledgeBlobUnavailable):
        return BLOB_UNAVAILABLE_TEXT
    return RUNTIME_ERROR_TEXT


class KnowledgeOperations:
    """Answers Knowledge commands for one team (module docstring)."""

    def __init__(
        self,
        repository: Any,
        people: PeopleDirectory,
        settings: KnowledgeSettings,
        clock: Callable[[], datetime],
        logger: logging.Logger,
        *,
        personal_memory: Any = None,
        blobs: Any = None,
    ) -> None:
        self._repository = repository
        self._people = people
        self._settings = settings
        self._clock = clock
        self._logger = logger
        self._blobs: Optional[BlobStore] = _as_blob_store(blobs)
        self._service = (
            knowledge_service_for(repository, settings=settings, personal_memory=personal_memory, logger=logger)
            if repository is not None and settings.enabled else None
        )
        self._rules = SensitiveContentRules.from_patterns(
            (pattern.name, pattern.pattern) for pattern in settings.sensitive_content.blocked_patterns
        )
        self._long_agent_types: set[str] = set()
        self._unrecorded_people: set[str] = set()

    def family(self) -> OperationFamily:
        """Its handlers at the team's address and its upkeep; they run in order (module docstring)."""

        return OperationFamily({kind: self.handle for kind in KNOWLEDGE_KINDS}, upkeep=self.purge_outcomes)

    async def handle(self, verified: VerifiedFrame) -> dict[str, Any]:
        """Decode request blobs, execute the command off-loop, and encode reply blobs for the wire."""

        action = verified.frame.kind.removeprefix("knowledge.")
        raw_body = verified.raw["body"]
        if self._service is None:
            return _message_reply("feature_off", KNOWLEDGE_OFF_TEXT)

        resolved = await asyncio.to_thread(self._resolve_actor, verified, action)
        if resolved.refusal is not None:
            return resolved.refusal

        pre_reply = await asyncio.to_thread(self._precheck_before_blobs, verified, action, raw_body, resolved)
        if pre_reply is not None:
            return await self._fit_wire_reply(action, raw_body, resolved, pre_reply)

        decoded_body, decode_refusal = await self._decode_request_blobs(verified, action, raw_body, resolved)
        if decode_refusal is not None:
            return decode_refusal

        reply = await asyncio.to_thread(self._execute_resolved, verified, action, decoded_body, resolved)
        return await self._fit_wire_reply(action, decoded_body, resolved, reply)

    async def purge_outcomes(self) -> None:
        """Delete stored outcomes past their retention, also when no new command stores one."""

        if self._repository is None:
            return
        purged = await asyncio.to_thread(self._repository.purge_command_outcomes, self._clock())
        if purged:
            self._logger.info("Deleted old stored outcomes. event=team_knowledge_outcomes_purged count=%s", purged)

    def answer(self, verified: VerifiedFrame) -> dict[str, Any]:
        """Synchronous handler for inline-only callers and unit tests."""

        action = verified.frame.kind.removeprefix("knowledge.")
        body = verified.raw["body"]
        if self._service is None:
            return _message_reply("feature_off", KNOWLEDGE_OFF_TEXT)
        resolved = self._resolve_actor(verified, action)
        if resolved.refusal is not None:
            return resolved.refusal
        pre_reply = self._precheck_before_blobs(verified, action, body, resolved)
        reply = pre_reply if pre_reply is not None else self._execute_resolved(verified, action, body, resolved)
        if reply.get("outcome") == "ok" and action in _LIST_ROWS:
            return fit_list_rows(reply, _LIST_ROWS[action], offset=int(body["offset"]), max_bytes=REPLY_BODY_MAX_BYTES)
        return reply

    async def _decode_request_blobs(
        self,
        verified: VerifiedFrame,
        action: str,
        raw_body: dict[str, Any],
        resolved: _ResolvedActor,
    ) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        try:
            decoded = await decode_request_body(action, raw_body, self._blobs)
            return decoded, None
        except BlobTooLarge as exc:
            refusal = self._blob_too_large_refusal(action, exc)
            stored_refusal = await asyncio.to_thread(
                self._store_final_refusal, verified, action, raw_body, resolved, refusal
            )
            return raw_body, stored_refusal
        except Exception as exc:  # noqa: BLE001 - pre-execution blob/NATS errors must not become handler_failed
            self._logger.error(
                "Could not read a Knowledge request blob; nothing was changed. "
                "event=team_knowledge_request_blob_failed action=%s error_type=%s detail=%s",
                action, type(exc).__name__, exc, exc_info=exc,
            )
            return raw_body, _message_reply("runtime_error", _blob_failure_message(exc))

    async def _fit_wire_reply(
        self,
        action: str,
        body: dict[str, Any],
        resolved: _ResolvedActor,
        reply: dict[str, Any],
    ) -> dict[str, Any]:
        if reply.get("outcome") != "ok":
            return reply
        assert resolved.blob_owner is not None
        try:
            return await fit_reply_body(
                reply,
                self._blobs,
                resolved.blob_owner,
                max_bytes=REPLY_BODY_MAX_BYTES,
                list_key=_LIST_ROWS.get(action),
                offset=int(body.get("offset", 0)),
            )
        except Exception as exc:
            if action in _CHANGES:
                self._logger.error(
                    "A knowledge change was committed, but encoding its reply blobs failed; "
                    "the request is answered as unknown. event=team_knowledge_reply_blob_failed "
                    "action=%s error_type=%s detail=%s",
                    action, type(exc).__name__, exc, exc_info=exc,
                )
                raise AnswerUnknown(
                    f"knowledge.{action} was carried out, but its reply could not be stored in UA1_BLOBS."
                ) from exc
            self._logger.error(
                "Could not store a Knowledge read reply in UA1_BLOBS. event=team_knowledge_read_blob_failed "
                "action=%s error_type=%s detail=%s",
                action, type(exc).__name__, exc, exc_info=exc,
            )
            return _message_reply("runtime_error", _blob_failure_message(exc))

    def _resolve_actor(self, verified: VerifiedFrame, action: str) -> _ResolvedActor:
        if action == "merge":
            return self._resolve_merge_actor(verified)
        check = command_person(verified, self._people, self._logger)
        if check.person_id is None:
            return _ResolvedActor(refusal=_message_reply("not_allowed", check.reason))
        assert verified.authority is not None and isinstance(verified.authority.token.person, str)
        actor = _person_actor(check.person_id, self._people.team_id)
        owner = BlobOwner(request_id=str(verified.raw["body"]["request_id"]), person_ref=verified.authority.token.person)
        return _ResolvedActor(
            actor=actor,
            person_check=check,
            outcome_person_id=check.person_id,
            blob_owner=owner,
            check_gateway_access=True,
        )

    def _resolve_merge_actor(self, verified: VerifiedFrame) -> _ResolvedActor:
        authority = verified.authority
        cert = verified.certificate
        if cert is None or not (_MERGE_ROLES & set(cert.roles)):
            return _ResolvedActor(refusal=_message_reply("not_allowed", "Only a lead or worker host may merge pages."))
        if authority is None or authority.token.bound_to.type != "root_task":
            return _ResolvedActor(refusal=_message_reply("not_allowed", "knowledge.merge requires a root_task token."))
        bound = authority.token.bound_to
        session = verified.session
        raw_agent = session.agent if session is not None else verified.frame.issuer.agent
        if not isinstance(raw_agent, str) or not AGENT_ID.fits(raw_agent):
            return _ResolvedActor(
                refusal=_message_reply("not_allowed", "knowledge.merge requires a valid agent identity.")
            )
        agent_id = raw_agent
        raw_type = session.agent_type if session is not None else agent_id
        agent_type = raw_type if _CONTRACT_AGENT_TYPE.fullmatch(raw_type) else AGENT_FALLBACK_TYPE
        person_ref = authority.token.person
        if not isinstance(person_ref, str):
            actor = KnowledgeActor(
                kind=ActorKind.AGENT,
                actor_id=agent_id,
                team_id=self._people.team_id,
                agent_type=agent_type,
                work_person_id="",
                task_id=bound.id,
                work_id=bound.request_id,
            )
            return _ResolvedActor(
                actor=actor,
                person_check=None,
                outcome_person_id=f"system:{agent_id}",
                blob_owner=BlobOwner(agent_id=agent_id),
                check_gateway_access=False,
            )
        now = self._people.person_by_ref(person_ref)
        if now is None:
            return _ResolvedActor(refusal=_message_reply("not_allowed", "The command names a person this team does not know."))
        active_person = CommandPerson(now.person_id, self._people.team_id, now.roles) if (now.active and now.roles) else None
        reason = "" if active_person is not None else f"The person is {now.describe()} in this team."
        is_scheduled = isinstance(authority.token.schedule_id, str) and bool(authority.token.schedule_id)
        actor = KnowledgeActor(
            kind=ActorKind.AGENT,
            actor_id=agent_id,
            team_id=self._people.team_id,
            agent_type=agent_type,
            work_person_id=now.person_id,
            task_id=bound.id,
            work_id=bound.request_id,
            request_id="" if is_scheduled else bound.request_id,
        )
        return _ResolvedActor(
            actor=actor,
            person_check=PersonCheck(active_person, reason, now.person_id),
            outcome_person_id=now.person_id,
            blob_owner=BlobOwner(request_id=bound.request_id, person_ref=person_ref),
            check_gateway_access=False,
        )

    def _precheck_before_blobs(
        self,
        verified: VerifiedFrame,
        action: str,
        raw_body: dict[str, Any],
        resolved: _ResolvedActor,
    ) -> Optional[dict[str, Any]]:
        """Check stored retry outcomes and person standing/access before fetching request blobs."""

        assert self._service is not None and resolved.actor is not None
        request = _Request(self._service, self._repository, resolved.actor, action, raw_body, self._editor)
        change = _Change(request) if action in _CHANGES else None
        once = self._once(verified, request, change, resolved.outcome_person_id) if change is not None else None
        if once is not None:
            stored = once.stored_reply()
            if stored is not None:
                return stored
        refused = self._refused_access(resolved, action, raw_body)
        if refused is not None:
            return refused if once is None else once.final(refused)
        return None

    def _store_final_refusal(
        self,
        verified: VerifiedFrame,
        action: str,
        raw_body: dict[str, Any],
        resolved: _ResolvedActor,
        refusal: dict[str, Any],
    ) -> dict[str, Any]:
        if action not in _CHANGES:
            return refusal
        assert self._service is not None and resolved.actor is not None
        request = _Request(self._service, self._repository, resolved.actor, action, raw_body, self._editor)
        once = self._once(verified, request, _Change(request), resolved.outcome_person_id)
        return once.final(refusal)

    def _execute_resolved(
        self,
        verified: VerifiedFrame,
        action: str,
        body: dict[str, Any],
        resolved: _ResolvedActor,
    ) -> dict[str, Any]:
        assert self._service is not None and resolved.actor is not None
        request = _Request(self._service, self._repository, resolved.actor, action, body, self._editor)
        if action not in _CHANGES:
            try:
                return _READS[action](request)
            except Exception as error:  # noqa: BLE001 - every failure is answered and logged in _refusal
                return self._refusal(request, error)

        change = _Change(request)
        once = self._once(verified, request, change, resolved.outcome_person_id)

        def attempt() -> dict[str, Any]:
            _CHANGERS[action](change, once.outcome)
            self._logger.info(
                "Changed the knowledge base via team services. event=team_knowledge_changed "
                "person=%s action=%s space=%s page=%s contribution_id=%s",
                resolved.outcome_person_id, action, body.get("space"), body.get("page"), body.get("contribution_id"),
            )
            if change.stored is None:
                raise AnswerUnknown(f"knowledge.{action} changed the knowledge base without storing its receipt.")
            return self._reply_from_receipt(request, change.stored)

        return once.run(attempt, refusal_for=lambda error: self._refusal(request, error))

    def _once(
        self,
        verified: VerifiedFrame,
        request: _Request,
        change: _Change,
        outcome_person_id: str,
    ) -> CommandOnce:
        kind = verified.frame.kind
        outcome = CommandOutcome(
            team_id=self._people.team_id, retry_key=str(verified.raw["idempotency_key"]),
            person_id=outcome_person_id, kind=kind, body_digest=str(verified.raw["body_digest"]),
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

    def _refused_access(
        self, resolved: _ResolvedActor, action: str, body: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        check = resolved.person_check
        if check is None:
            return None
        person = check.person
        if person is None:
            return _message_reply("not_allowed", check.reason)
        if not resolved.check_gateway_access:
            return None
        for space in _access_spaces(body):
            decision = knowledge_command_access(
                access_payload(action, {**body, "space": space}),
                person_id=person.person_id, team_id=person.team_id, roles=person.roles,
            )
            if not decision.allowed:
                self._logger.warning(
                    "Refused a Knowledge page request. event=team_knowledge_command_denied "
                    "person=%s action=%s space=%s reason=%s", person.person_id, action, space, decision.reason,
                )
                return _message_reply("not_allowed", NOT_YOURS_TEXT)
        return None

    def _blob_too_large_refusal(self, action: str, error: BlobTooLarge) -> dict[str, Any]:
        kind = f"knowledge.{action}"
        allowed = set(KINDS[kind].reply_errors)
        outcome = "invalid_request" if "invalid_request" in allowed else "runtime_error"
        return _message_reply(outcome, str(error))

    def _reply_from_receipt(self, request: _Request, stored: dict[str, Any]) -> dict[str, Any]:
        """A committed change's reply, read as the change left it; ``AnswerUnknown`` when that fails."""

        try:
            return _reply_from_receipt(request, stored[RECEIPT_KEY])
        except Exception as error:
            self._logger.error(
                "A knowledge change was made, but its reply could not be built; the request "
                "is answered as unknown. event=team_knowledge_reply_after_change_failed person=%s "
                "action=%s error_type=%s detail=%s", request.actor.actor_id, request.action,
                type(error).__name__, error, exc_info=error,
            )
            raise AnswerUnknown(f"knowledge.{request.action} was carried out, but its reply could not be built.") \
                from error

    def _editor(self, kind: str, actor_id: str, agent_type: str) -> dict[str, Any]:
        if kind == ActorKind.AGENT.value:
            return self._agent_editor(agent_type)
        if kind == ActorKind.PERSON.value:
            reference = self._people.person_ref(actor_id)
            if reference is not None:
                return {"kind": "person", "person": reference}
            if actor_id not in self._unrecorded_people:
                self._unrecorded_people.add(actor_id)
                self._logger.info(
                    "A knowledge change names a person the team's people records no longer hold; shown "
                    "as a team member. event=team_knowledge_editor_unrecorded person=%s", actor_id,
                )
            return {"kind": "person"}
        return {"kind": "system"}

    def _agent_editor(self, agent_type: str) -> dict[str, Any]:
        if _CONTRACT_AGENT_TYPE.fullmatch(agent_type or ""):
            return {"kind": "agent", "agent_type": agent_type}
        if agent_type and agent_type not in self._long_agent_types:
            self._long_agent_types.add(agent_type)
            self._logger.warning(
                "An agent type the message contract cannot carry (at most 24 letters, digits, _ or "
                "-) is shown in Web Chat as %r. event=team_knowledge_agent_type_not_shown "
                "agent_type=%s", AGENT_FALLBACK_TYPE, agent_type,
            )
        return {"kind": "agent", "agent_type": AGENT_FALLBACK_TYPE}

    def _refusal(self, request: _Request, error: Exception) -> dict[str, Any]:
        kind = f"knowledge.{request.action}"
        allowed = set(KINDS[kind].reply_errors)
        if isinstance(error, KnowledgeError):
            outcome = _OUTCOMES.get(error.code, error.code)
            if outcome == "invalid_request" and "invalid_request" not in allowed and "not_found" in allowed:
                outcome = "not_found"
            if outcome in allowed:
                self._logger.info(
                    "A Knowledge request was refused. event=team_knowledge_refused person=%s "
                    "action=%s code=%s", request.actor.actor_id, request.action, error.code,
                )
                return self._refusal_reply(request, outcome, error)
        self._logger.error(
            "A Knowledge request failed. event=team_knowledge_command_failed person=%s action=%s "
            "error_type=%s", request.actor.actor_id, request.action, type(error).__name__, exc_info=error,
        )
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
                message = f"{MORE_FLAGS_TEXT.format(shown=FLAGS_MAX, total=len(flags))} {message}"
        return {**_message_reply(outcome, message), **details}

    def _flags_for(self, request: _Request, error: KnowledgeError, *, blocked: bool) -> list[dict[str, Any]]:
        flags: list[dict[str, Any]] = []
        for name, value in _checked_fields(request):
            scan = self._rules.scan(value)
            for finding in scan.blocked if blocked else scan.review:
                flags.append(_flag(finding.kind, finding.label, name, finding.line, finding.column))
        if blocked and not flags:
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
    space = body.get("space", "team")
    return (KnowledgeSpace.TEAM.value, KnowledgeSpace.PERSONAL.value) if space == "all" else (space,)


def _person_actor(person_id: str, team_id: str) -> KnowledgeActor:
    return KnowledgeActor(kind=ActorKind.PERSON, actor_id=person_id, team_id=team_id, work_person_id=person_id)


def _text(content: Optional[dict[str, Any]]) -> str:
    if content is None:
        return ""
    if content.get("type") != "inline":
        raise KnowledgeError(CODE_INVALID, "Knowledge text must be resolved before execution.")
    return str(content["text"])


def _version(value: Any) -> int:
    text = str(value or "0")
    if not text.isdigit():
        raise KnowledgeError(CODE_INVALID, "A page version is its revision number.")
    return int(text)


def _draft(body: dict[str, Any]) -> DraftInput:
    return DraftInput(
        title=body.get("title", ""), body=_text(body.get("content")),
        summary=body.get("summary"), tags=body.get("tags"),
    )


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


# -- changes ------------------------------------------------------------------------


def _write(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    body = request.body
    reference = f"{body['space']}/{body.get('page') or body.get('title', '')}"
    base_revision = _version(body.get("seen_version"))
    request.service.write(
        request.actor, reference, _draft(body), base_revision=base_revision,
        confirm_review=bool(body["confirm_review"]), command_outcome=outcome,
    )


def _add_note(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    request.service.add_note(
        request.actor, request.reference, _text(request.body["note"]),
        confirm_review=bool(request.body["confirm_review"]), command_outcome=outcome,
    )


def _archive(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    request.service.archive(
        request.actor, request.reference, base_revision=_version(request.body["seen_version"]),
        reason=request.body["reason"], command_outcome=outcome,
    )


def _revert(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    body = request.body
    request.service.revert(
        request.actor, request.reference, to_revision=int(body["to_revision"]),
        base_revision=_version(body["seen_version"]), reason=body["reason"],
        command_outcome=outcome,
    )


def _merge(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    body = request.body
    source_ref = f"{body['space']}/{body['page']}"
    target_ref = f"{body['space']}/{body['into']}"
    request.service.merge(
        request.actor,
        source_ref,
        target_ref,
        _draft(body),
        base_revision=_version(body["target_seen_version"]),
        source_revision=_version(body["seen_version"]),
        confirm_review=bool(body["confirm_review"]),
        command_outcome=outcome,
    )


def _publish_contribution(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    body = request.body
    edited = bool(body["edited"])
    request.service.publish_contribution(
        request.actor, body["contribution_id"],
        draft=_draft(body) if edited else None,
        note=(_text(body["note"]) if "note" in body else "") if edited else None,
        base_revision=_version(body.get("seen_version")),
        page=body.get("page", ""),
        command_outcome=outcome,
    )


def _withdraw_contribution(change: _Change, outcome: CommandOutcome) -> None:
    request = change.request
    request.service.withdraw_contribution(request.actor, request.body["contribution_id"], command_outcome=outcome)


_CHANGERS: dict[str, Callable[[_Change, CommandOutcome], None]] = {
    "write": _write,
    "add_note": _add_note,
    "archive": _archive,
    "revert": _revert,
    "merge": _merge,
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
    return reply


# -- what the page shows ------------------------------------------------------------


def _summary(page: KnowledgePage, snippet: str = "") -> dict[str, Any]:
    summary: dict[str, Any] = {
        "space": page.space.value, "page": page.slug, "title": page.title, "page_type": page.page_type,
        "summary": page.summary, "tags": list(page.tags), "status": page.status.value,
        "seen_version": str(page.revision), "updated_at": _timestamp(page.updated_at_utc),
    }
    if snippet:
        summary["snippet"] = {"type": "inline", "text": snippet}
    return summary


def _page(request: _Request, reference: str, *, revision: int = 0) -> dict[str, Any]:
    view = request.service.read(request.actor, reference, revision=revision)
    _page_latest, latest = request.service.history(request.actor, view.page.reference(), limit=1)
    return _detail(
        request, view.page, shown=view.revision, backlinks=[_summary(linked) for linked in view.backlinks],
        open_notes=[_note(request, note, any_editor=True) for note in view.open_notes],
        pending=_pending(view.pending_contributions), redirected_from=view.redirected_from,
        updated_by=_revision_editor(request, latest[0]) if latest else {"kind": "system"},
    )


def _detail(
    request: _Request,
    page: KnowledgePage,
    *,
    backlinks: list[dict[str, Any]],
    open_notes: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    updated_by: dict[str, Any],
    shown: Optional[KnowledgeRevision] = None,
    redirected_from: str = "",
    check_links: bool = True,
) -> dict[str, Any]:
    source = shown or page
    detail = {
        "type": "inline",
        **_summary(page),
        "title": source.title,
        "page_type": source.page_type,
        "summary": source.summary,
        "tags": list(source.tags),
        "content": {"type": "inline", "text": source.body},
        "created_at": _timestamp(page.created_at_utc),
        "updated_by": updated_by,
        "backlinks": backlinks,
        "open_notes": open_notes,
        "pending_contributions": pending,
        "links": [
            {"written": link.written, "space": link.space, "page": link.slug, "exists": link.exists}
            for link in request.service.page_links(page, source.body, check_exists=check_links)
        ],
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
    return [
        {"contribution_id": c.contribution_id, "kind": c.kind.value, "created_at": _timestamp(c.created_at_utc)}
        for c in contributions
    ]


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
        editor = request.editor(ActorKind.AGENT.value, "", note.actor_agent_type)
    return _note_shown(note, editor)


def _note_shown(note: KnowledgeNote, editor: dict[str, Any]) -> dict[str, Any]:
    return {
        "note_id": note.note_id,
        "content": {"type": "inline", "text": note.body},
        "created_at": _timestamp(note.created_at_utc),
        "created_by": editor,
    }


def _contribution(request: _Request, contribution: KnowledgeContribution, *, full: bool = False) -> dict[str, Any]:
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
    if contribution.body:
        shown["content"] = {"type": "inline", "text": contribution.body}
    if contribution.kind is not ContributionKind.CREATE:
        try:
            shown["current"] = _page(request, f"{KnowledgeSpace.TEAM.value}/{contribution.slug}")
        except KnowledgeError as error:
            if error.code != CODE_NOT_FOUND:
                raise
    return shown


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

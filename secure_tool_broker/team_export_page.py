"""``/upgrade``: the page where an owner exports the whole team (guided upgrade phase 2).

The gateway opens this page for an owner
(``assistant_gateway.application.team_exports``) and watches it. The owner
types a passphrase twice. This page keeps it in the broker's memory only and
leaves an export request for the supervisor (``team_transfer.export_requests``;
the request holds no secret). The supervisor's export process collects the
passphrase once through ``TEAM_EXPORT_PASSPHRASE_ROUTE`` (operator caller
only, ``secure_tool_broker.defaults``) with a token for that collection,
writes the archive into ``exports/`` and reports through
``TEAM_EXPORT_RESULT_ROUTE`` with the same token, so no other process can end
the export. When the process exits with an error without reporting, the
supervisor says so through ``TEAM_EXPORT_ENDED_ROUTE``. The page then shows
the file, its parts and the next steps, and the gateway posts the same in the
chat.

What the passphrase protects, as the page says: the file's stored sign-ins,
keys and operator secrets are encrypted with it; the rest of the team's
records in the file can be read by anyone holding it, so the file must stay
private.

Why the broker serves it: it already serves the private pages people type
secrets on, and the passphrase must never reach the gateway, the chat, a model
or a file. The broker cannot run the export itself: only the supervisor holds
every operator secret the export carries.

Who may: owners only (``access_control.people.team_export_refusal``), checked
when the page opens (``SecureToolBrokerService``) and again on submission
with the person's current record; the export process checks the stored record
once more (``team_transfer.service.require_active_owner``).

Lifetimes:

- The page stays open for an hour (``TEAM_EXPORT_PAGE_SECONDS``) and has
  Close buttons at the top and the bottom.
- A submitted passphrase waits at most ``PICKUP_SECONDS`` for the export
  process, checked when the process collects it and whenever the page or its
  status is read; then it is dropped and the page says no export started in
  time. Once collected, the broker keeps no copy.
- A broker restart forgets waiting passphrases and running exports. The page
  link still opens (it is signed), but the rebuilt page
  (``SESSION_RESTORED_AFTER_RESTART``) never starts an export: it says the
  broker restarted and that the owner runs ``/upgrade`` again. Remembering
  used pages across restarts would need a store; one rule for every rebuilt
  page needs none.
"""

from __future__ import annotations

import hmac
import html
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Mapping

from aiohttp import web
from pydantic import ValidationError

from access_control.people import team_export_refusal
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker.auth_pages import (
    SECRET_INPUT_TOGGLE_SCRIPT,
    render_broker_auth_page,
    render_credential_input,
)
from secure_tool_broker.defaults import (
    TEAM_EXPORT_ENDED_ROUTE,
    TEAM_EXPORT_PASSPHRASE_ROUTE,
    TEAM_EXPORT_RESULT_ROUTE,
)
from secure_tool_broker.model_key_page import NOT_ACTIVE_MESSAGE, current_person
from secure_tool_broker.sign_in_pages import SESSION_RESTORED_AFTER_RESTART
from secure_tools.chat_commands import UPGRADE_COMMAND
from size_presentation import size_words
from team_transfer.export_requests import (
    EXPORT_PAGE_CLOSED,
    EXPORT_PAGE_CREATED,
    EXPORT_PAGE_EXPORTING,
    EXPORT_PAGE_FAILED,
    EXPORT_PAGE_RESTARTED,
    EXPORT_PAGE_SUCCESS,
    EXPORT_PASSPHRASE_MAX_CHARS,
    NEXT_STEPS,
    PROCESS_ENDED_MESSAGE,
    PROCESS_ENDED_UNCOLLECTED_MESSAGE,
    RESULT_UNCONFIRMED,
    TEAM_EXPORT_PAGE_METHOD,
    TEAM_EXPORT_PAGE_PROVIDER,
    TeamExportPassphraseRequestV1,
    TeamExportPassphraseV1,
    TeamExportProcessEndedV1,
    TeamExportRequestV1,
    TeamExportResultV1,
    new_export_id,
    section_lines,
    supervisor_runs_exports,
    write_export_request,
)
from team_transfer.titles import section_title

LOGGER = logging.getLogger(__name__)

TEAM_EXPORT_PAGE_SECONDS: Final = 60 * 60
PICKUP_SECONDS: Final = 5 * 60
# Page states (``status`` in the session and its status route), shared with the gateway's watch.
STATUS_CREATED: Final = EXPORT_PAGE_CREATED
STATUS_EXPORTING: Final = EXPORT_PAGE_EXPORTING
STATUS_SUCCESS: Final = EXPORT_PAGE_SUCCESS
STATUS_FAILED: Final = EXPORT_PAGE_FAILED
STATUS_CLOSED: Final = EXPORT_PAGE_CLOSED
STATUS_RESTARTED: Final = EXPORT_PAGE_RESTARTED
# Exports run one at a time, so a request can wait behind another export.
NOT_STARTED_MESSAGE: Final = (
    "No export process picked up the passphrase within five minutes, so it was dropped and nothing was written. "
    "Another export may have been running, or the launcher may not be running exports for this team. "
    f"Run {UPGRADE_COMMAND} again."
)
PASSPHRASE_TOO_LONG_MESSAGE: Final = (
    f"Nothing was exported. Use a passphrase of at most {EXPORT_PASSPHRASE_MAX_CHARS:,} characters."
)
UNUSABLE_PASSPHRASE_MESSAGE: Final = (
    f"The passphrase could not be handed to the export process, so nothing was written. Run {UPGRADE_COMMAND} again."
)
RESTARTED_MESSAGE: Final = (
    "The secure broker restarted after this page opened, so this page can no longer start or follow an export. "
    f"Run {UPGRADE_COMMAND} in the chat for a new page. If you started an export before the restart, it may "
    "still have finished: look in the exports folder of this instance's data folder."
)
NOT_LAUNCHED_MESSAGE: Final = (
    "This team was not started by the launcher, so nothing can run the export from this page. "
    "Use assistant_cli team export on the host instead."
)
# What the result and ended routes answer.
ENDED_RECORDED: Final = "recorded"
ENDED_IGNORED: Final = "ignored"
_EXPORT = "export"
_CLOSE = "close"
_FORM_ID = "export-form"
_TOP_ID = "page-top"
_FORM_ERROR = "form_error"
_RESULT = "result"
_EXPORT_ID = "export_id"
_STATUS = "status"


@dataclass
class _WaitingExport:
    """One submitted export: its page and, until collected, its passphrase; then its collection's token."""

    session: Any
    team_id: str
    owner_person_id: str
    requested_at: datetime
    # Never in a text form of this record (logs, tracebacks).
    passphrase: str = field(default="", repr=False)
    collected: bool = False
    collection_token: str = field(default="", repr=False)
    # The process that collected (TeamExportPassphraseRequestV1.process_id).
    process_id: int = 0

    def names(self, export: TeamExportPassphraseRequestV1 | TeamExportResultV1 | TeamExportProcessEndedV1) -> bool:
        return (self.team_id, self.owner_person_id) == (export.team_id, export.owner_person_id)

    def overdue(self, now: datetime) -> bool:
        """Nobody collected the passphrase within the pickup time (module docstring)."""

        return not self.collected and now - self.requested_at >= timedelta(seconds=PICKUP_SECONDS)


class _Exports:
    """Submitted exports by id. Form submissions run in threads, routes on the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, _WaitingExport] = {}

    def add(self, export_id: str, waiting: _WaitingExport) -> None:
        with self._lock:
            self._by_id[export_id] = waiting

    def collect(self, request: TeamExportPassphraseRequestV1, now: datetime) -> TeamExportPassphraseV1:
        """The passphrase for this export and its collection's token, once; the broker keeps no copy.

        The pickup time is enforced here too, so a passphrase nobody read
        the page for is never handed out late. The reply is built before the
        export counts as collected, so a reply that cannot be built leaves
        nothing half-done; its error never repeats the passphrase.
        """

        with self._lock:
            waiting = self._by_id.get(request.export_id)
            if waiting is None or not waiting.names(request):
                raise web.HTTPNotFound(
                    text="No export with this id is waiting: the page expired, or the broker restarted."
                )
            if waiting.collected:
                raise web.HTTPConflict(text="This export's passphrase was already collected.")
            if waiting.overdue(now):
                del self._by_id[request.export_id]
                unusable = False
            else:
                try:
                    reply = TeamExportPassphraseV1(
                        passphrase=waiting.passphrase, collection_token=secrets.token_urlsafe(24)
                    )
                except ValidationError:
                    del self._by_id[request.export_id]
                    unusable = True
                else:
                    waiting.passphrase = ""
                    waiting.collected, waiting.collection_token = True, reply.collection_token
                    waiting.process_id = request.process_id
                    return reply
        if unusable:
            _say_unusable(request.export_id, waiting)
            raise web.HTTPInternalServerError(text="This export's passphrase could not be handed over; it was dropped.")
        _say_not_started(request.export_id, waiting)
        raise web.HTTPGone(text="This export's passphrase waited longer than five minutes and was dropped.")

    def finish(self, result: TeamExportResultV1) -> None:
        """Record how the export ended on its page; the export is forgotten afterwards.

        A collected export ends only with its collection's token, so another
        process's report (a duplicated request) cannot end it. A process that
        failed before collecting may report that failure while nothing has
        been collected.
        """

        with self._lock:
            waiting = self._by_id.get(result.export_id)
            if waiting is None or not waiting.names(result):
                raise web.HTTPNotFound(text="No export with this id is open.")
            if waiting.collected:
                if not hmac.compare_digest(result.collection_token.encode(), waiting.collection_token.encode()):
                    raise web.HTTPConflict(text="This result is not from the process that collected the passphrase.")
            elif result.ok or result.collection_token:
                raise web.HTTPConflict(text="Nothing collected this export's passphrase, so it cannot have finished.")
            del self._by_id[result.export_id]
        waiting.session.state[_RESULT] = result.model_dump(mode="json", exclude={"collection_token"})
        waiting.session.state[_STATUS] = STATUS_SUCCESS if result.ok else STATUS_FAILED

    def process_ended(self, report: TeamExportProcessEndedV1) -> bool:
        """The supervisor saw the export process exit with an error; True when that ended an open export.

        An export that already reported is gone from here, so its own result
        stands. A collected export ends only on a report for the process that
        collected it, so no other process's exit can end it.
        """

        with self._lock:
            waiting = self._by_id.get(report.export_id)
            if waiting is None or not waiting.names(report):
                return False
            if waiting.collected and waiting.process_id != report.process_id:
                mismatch = True
            else:
                mismatch = False
                del self._by_id[report.export_id]
        if mismatch:
            LOGGER.warning(
                "Ignored a report for another process than the one running the export. "
                "event=team_export_process_ended_mismatch export_id=%s reported_process_id=%s",
                report.export_id,
                report.process_id,
            )
            return False
        LOGGER.warning(
            "The export process ended without reporting. event=team_export_process_ended_unreported "
            "export_id=%s exit_code=%s collected=%s",
            report.export_id,
            report.exit_code,
            waiting.collected,
        )
        waiting.session.state[_STATUS] = STATUS_FAILED
        if waiting.collected:
            waiting.session.state[_RESULT] = {
                "ok": False,
                "error": PROCESS_ENDED_MESSAGE.format(exit_code=report.exit_code),
                RESULT_UNCONFIRMED: True,
            }
        else:
            # No passphrase was handed out, so no file can exist: a plain failure.
            waiting.session.state[_RESULT] = {
                "ok": False,
                "error": PROCESS_ENDED_UNCOLLECTED_MESSAGE.format(exit_code=report.exit_code),
            }
        return True

    def drop_unstarted(self, session: Any, now: datetime) -> None:
        """Forget a passphrase nobody collected in time, and say why on the page."""

        export_id = str(session.state.get(_EXPORT_ID) or "")
        with self._lock:
            waiting = self._by_id.get(export_id)
            if waiting is None or not waiting.overdue(now):
                return
            del self._by_id[export_id]
        _say_not_started(export_id, waiting)

    def discard(self, session: Any) -> None:
        export_id = str(session.state.get(_EXPORT_ID) or "")
        with self._lock:
            self._by_id.pop(export_id, None)


def _say_unusable(export_id: str, waiting: _WaitingExport) -> None:
    LOGGER.error(
        "The export's passphrase could not be handed to the export process; it was dropped. "
        "event=team_export_passphrase_unusable export_id=%s",
        export_id,
    )
    waiting.session.state[_STATUS] = STATUS_FAILED
    waiting.session.state[_RESULT] = {"ok": False, "error": UNUSABLE_PASSPHRASE_MESSAGE}


def _say_not_started(export_id: str, waiting: _WaitingExport) -> None:
    LOGGER.warning(
        "No export process collected the passphrase in time; it was dropped. "
        "event=team_export_not_started export_id=%s owner_person_id=%s",
        export_id,
        waiting.owner_person_id,
    )
    waiting.session.state[_STATUS] = STATUS_FAILED
    waiting.session.state[_RESULT] = {"ok": False, "error": NOT_STARTED_MESSAGE}


_EXPORTS = _Exports()


def _requests_dir() -> Any:
    root = str(os.environ.get("TEAM_INSTANCE_ROOT") or "").strip()
    return InstanceLayout.at(root).team_export_requests_dir if root else None


class TeamExportFlow:
    """``/upgrade``: one form, a passphrase typed twice (module docstring)."""

    provider = TEAM_EXPORT_PAGE_PROVIDER
    method = TEAM_EXPORT_PAGE_METHOD
    display_name = "Team export"
    kind = "form"
    page_seconds = TEAM_EXPORT_PAGE_SECONDS

    def opening_refusal(self, roles: tuple[str, ...]) -> str | None:
        """Checked when the page opens, with the roles the gateway vouched for."""

        return team_export_refusal(roles)

    def start(self, session: Any, payload: Mapping[str, Any], logger: logging.Logger) -> None:
        session.state.setdefault(_STATUS, STATUS_CREATED)

    def submit(self, session: Any, form: Mapping[str, Any], logger: logging.Logger) -> None:
        session.state.pop(_FORM_ERROR, None)
        _stop_if_restored(session)
        if str(form.get("action") or "") == _CLOSE:
            # While the export runs, the page stays: it must hear how the
            # export ended. The page offers no Close then.
            if session.state.get(_STATUS) == STATUS_EXPORTING:
                return
            # A closed page answers "already used" from now on; the browser tab closes itself.
            _EXPORTS.discard(session)
            session.used = True
            session.state[_STATUS] = STATUS_CLOSED
            return
        if session.state.get(_STATUS) == STATUS_RESTARTED:
            logger.warning(
                "Team export refused: the page was rebuilt after a broker restart. "
                "event=team_export_submit_refused person_id=%s reason=broker_restarted",
                session.owner_canonical_user_id,
            )
            return
        if session.state.get(_STATUS) != STATUS_CREATED:
            session.state[_FORM_ERROR] = f"This page already ran its export. Run {UPGRADE_COMMAND} for another one."
            return
        problem = _submission_problem(session, form)
        if problem:
            logger.warning(
                "Team export refused at submission. event=team_export_submit_refused person_id=%s reason=%s",
                session.owner_canonical_user_id,
                problem[:80],
            )
            session.state[_FORM_ERROR] = problem
            return
        self._request(session, str(form.get("passphrase") or ""), logger)

    def _request(self, session: Any, passphrase: str, logger: logging.Logger) -> None:
        requests_dir = _requests_dir()
        if requests_dir is None or not supervisor_runs_exports():
            session.state[_FORM_ERROR] = NOT_LAUNCHED_MESSAGE
            return
        now = datetime.now(timezone.utc)
        request = TeamExportRequestV1(
            export_id=new_export_id(),
            team_id=str(session.owner_team_id or ""),
            owner_person_id=str(session.owner_canonical_user_id or ""),
            requested_at_utc=now,
        )
        # Registered before the request is written, so the export process
        # always finds the passphrase it is sent for.
        _EXPORTS.add(
            request.export_id,
            _WaitingExport(
                session=session,
                team_id=request.team_id,
                owner_person_id=request.owner_person_id,
                requested_at=now,
                passphrase=passphrase,
            ),
        )
        try:
            write_export_request(requests_dir, request)
        except OSError as exc:
            _EXPORTS.discard(session)
            logger.error(
                "The export request could not be written. event=team_export_request_write_failed error=%s",
                type(exc).__name__,
            )
            session.state[_FORM_ERROR] = "The export did not start: its request could not be saved. Try again."
            return
        session.state[_EXPORT_ID] = request.export_id
        session.state[_STATUS] = STATUS_EXPORTING

    def render(self, session: Any) -> str:
        _stop_if_restored(session)
        _EXPORTS.drop_unstarted(session, datetime.now(timezone.utc))
        status = str(session.state.get(_STATUS) or STATUS_CREATED)
        if status == STATUS_CLOSED:
            return _closed_page()
        if status == STATUS_EXPORTING:
            page = _intro_panel(closable=False) + _exporting_panel()
            return render_broker_auth_page(self.display_name, page, script=_WAIT_SCRIPT)
        if status == STATUS_RESTARTED:
            body, script = _restarted_panel(), _BUSY_SCRIPT
        elif status in (STATUS_SUCCESS, STATUS_FAILED):
            body, script = _result_panel(session), _BUSY_SCRIPT
        else:
            body, script = _form_panel(session), SECRET_INPUT_TOGGLE_SCRIPT + _BUSY_SCRIPT
        page = _intro_panel(closable=True) + body + f'<section class="content-panel">{_close_form()}</section>'
        return render_broker_auth_page(self.display_name, page, script=script)

    def status_payload(self, session: Any) -> dict[str, Any]:
        _stop_if_restored(session)
        _EXPORTS.drop_unstarted(session, datetime.now(timezone.utc))
        result = session.state.get(_RESULT)
        return {
            "provider": session.provider,
            "method": session.method,
            "status": str(session.state.get(_STATUS) or STATUS_CREATED),
            "result": dict(result) if isinstance(result, dict) else None,
            "expires_at": session.expires_at.isoformat(),
        }

    def cleanup(self, session: Any) -> None:
        _EXPORTS.discard(session)


def _stop_if_restored(session: Any) -> None:
    """A page the broker rebuilt after a restart never starts an export (module docstring)."""

    if session.state.get(SESSION_RESTORED_AFTER_RESTART) and session.state.get(_STATUS) == STATUS_CREATED:
        LOGGER.info(
            "A team export page was rebuilt after a broker restart; it starts no export. "
            "event=team_export_page_restarted person_id=%s",
            session.owner_canonical_user_id,
        )
        session.state[_STATUS] = STATUS_RESTARTED


def _submission_problem(session: Any, form: Mapping[str, Any]) -> str:
    """Why this submission cannot start an export, or "" when it can."""

    from team_transfer.crypto import TransferKeyError, check_passphrase

    person = current_person(str(session.owner_canonical_user_id or ""))
    if person is not None and not person.active:
        return NOT_ACTIVE_MESSAGE
    refusal = team_export_refusal(person.roles if person is not None else session.owner_roles)
    if refusal:
        return refusal
    passphrase = str(form.get("passphrase") or "")
    try:
        check_passphrase(passphrase)
    except TransferKeyError as exc:
        return f"Nothing was exported. {exc}"
    if len(passphrase) > EXPORT_PASSPHRASE_MAX_CHARS:
        return PASSPHRASE_TOO_LONG_MESSAGE
    if passphrase != str(form.get("passphrase_again") or ""):
        return "Nothing was exported. The two passphrases are different; type the same one twice."
    return ""


def add_team_export_routes(app: web.Application, *, team_id: str) -> None:
    """The export's routes; only the operator caller reaches /v1/transfer/ (SecureToolBrokerService)."""

    async def passphrase_handler(request: web.Request) -> web.Response:
        body = await _model(request, TeamExportPassphraseRequestV1)
        if body.team_id != team_id:
            raise web.HTTPForbidden(text="The request names another team.")
        reply = _EXPORTS.collect(body, datetime.now(timezone.utc))
        LOGGER.info(
            "The export process collected its passphrase. event=team_export_passphrase_collected export_id=%s "
            "process_id=%s",
            body.export_id,
            body.process_id,
        )
        return web.json_response(reply.model_dump())

    async def result_handler(request: web.Request) -> web.Response:
        result = await _model(request, TeamExportResultV1)
        if result.team_id != team_id:
            raise web.HTTPForbidden(text="The result names another team.")
        _EXPORTS.finish(result)
        LOGGER.info(
            "Team export finished. event=team_export_finished export_id=%s ok=%s file_name=%s size_bytes=%s",
            result.export_id,
            result.ok,
            result.file_name or "-",
            result.size_bytes,
        )
        return web.json_response({"status": ENDED_RECORDED})

    async def ended_handler(request: web.Request) -> web.Response:
        report = await _model(request, TeamExportProcessEndedV1)
        if report.team_id != team_id:
            raise web.HTTPForbidden(text="The report names another team.")
        return web.json_response({"status": ENDED_RECORDED if _EXPORTS.process_ended(report) else ENDED_IGNORED})

    app.router.add_post(TEAM_EXPORT_PASSPHRASE_ROUTE, passphrase_handler)
    app.router.add_post(TEAM_EXPORT_RESULT_ROUTE, result_handler)
    app.router.add_post(TEAM_EXPORT_ENDED_ROUTE, ended_handler)


async def _model(request: web.Request, model: Any) -> Any:
    try:
        return model.model_validate(await request.json())
    except (ValueError, ValidationError) as exc:
        raise web.HTTPBadRequest(text=f"The request is not valid ({type(exc).__name__}).") from exc


# -- page parts ----------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape(str(value))


def _close_form() -> str:
    return (
        f'<div class="page-actions"><form method="post" action="#{_TOP_ID}">'
        f'<input type="hidden" name="action" value="{_CLOSE}">'
        '<button type="submit" class="secondary-action">Close</button></form></div>'
    )


def _section_items() -> str:
    """What the file holds, from the sections the export writes (``team_transfer.registry``)."""

    from team_transfer.registry import TRANSFER_SECTIONS

    return "".join(
        f"<li>{_e(section_title(section.name))}{' (encrypted)' if section.encrypted else ''}</li>"
        for section in TRANSFER_SECTIONS
    )


def _intro_panel(*, closable: bool) -> str:
    return (
        f'<section class="content-panel" id="{_TOP_ID}"><p class="eyebrow">Team export</p>'
        "<h1>Export the team for an upgrade</h1>"
        "<p class=\"lede\">This writes one file with everything a new version needs to take over this team. "
        "On the new instance you choose what to import; everything starts selected.</p>"
        "<p>Your passphrase encrypts the stored sign-ins, keys and secrets in the file. The rest of the team's "
        "records in it, such as people, access, knowledge pages and schedules, can be read by anyone who has the "
        "file, so keep the file private.</p>"
        f"<p>The file holds:</p><ul>{_section_items()}</ul>"
        "<p>This page stays open for one hour.</p>"
        f"{_close_form() if closable else ''}</section>"
    )


def _form_panel(session: Any) -> str:
    error = str(session.state.get(_FORM_ERROR) or "")
    notice = f'<p class="status status-error" role="alert">{_e(error)}</p>' if error else ""
    return (
        f'<section class="content-panel" id="{_FORM_ID}"><h2>Choose a passphrase</h2>{notice}'
        f'<form method="post" action="#{_FORM_ID}"><input type="hidden" name="action" value="{_EXPORT}">'
        '<div><label for="passphrase">Passphrase</label>'
        + render_credential_input(input_id="passphrase", input_type="password", autocomplete="new-password", required=True)
        + '</div><div><label for="passphrase_again">Type it again</label>'
        + render_credential_input(
            input_id="passphrase_again", input_type="password", autocomplete="new-password", required=True
        )
        + "</div>"
        "<p class=\"note\">At least 12 characters. Keep it somewhere safe: you need it to import the file, and "
        "it is not saved anywhere. Without it nobody can read the stored sign-ins, keys and secrets in the file "
        "or import it. It goes to the secure broker and is never sent to the agent.</p>"
        '<button type="submit">Export the team</button>'
        '<p class="status" role="status" data-export-progress hidden>Exporting. This usually takes under a '
        "minute; a large team can take a few minutes.</p></form></section>"
    )


def _exporting_panel() -> str:
    return (
        f'<section class="content-panel" id="{_FORM_ID}"><h2>Exporting the team</h2>'
        '<p class="status" role="status" aria-busy="true">The launcher is writing the file. This page updates '
        "when it is done; this usually takes under a minute.</p></section>"
    )


def _result_panel(session: Any) -> str:
    raw = session.state.get(_RESULT)
    raw = raw if isinstance(raw, dict) else {}
    if not raw.get("ok"):
        why = _e(raw.get("error") or "The export process stopped without saying why. The launcher's log has details.")
        heading = "The export's outcome is not known" if raw.get(RESULT_UNCONFIRMED) else "The team was not exported"
        return (
            f'<section class="content-panel" id="{_FORM_ID}"><h2>{heading}</h2>'
            f'<p class="status status-error" role="alert">{why}</p>'
            f"<p>Run {_e(UPGRADE_COMMAND)} in the chat to try again.</p></section>"
        )
    result = TeamExportResultV1.model_validate(raw)
    lines = section_lines(result)
    steps = "".join(f"<li>{_e(step)}</li>" for step in NEXT_STEPS)
    note = f'<p class="note">{_e(result.note)}</p>' if result.note else ""
    # Without parts the size and contents could not be read after writing (the note says so).
    size = f" ({_e(size_words(result.size_bytes))})" if lines else ""
    holds = f"<p>It holds:</p><ul>{''.join(f'<li>{_e(line)}</li>' for line in lines)}</ul>" if lines else ""
    return (
        f'<section class="content-panel" id="{_FORM_ID}"><h2>The team is exported</h2>'
        f'<p class="status" role="status">Wrote <code>exports/{_e(result.file_name)}</code> in this instance\'s '
        f"data folder{size}.</p>{note}"
        f"{holds}<h2>Next steps</h2><ol>{steps}</ol></section>"
    )


def _restarted_panel() -> str:
    return (
        f'<section class="content-panel" id="{_FORM_ID}"><h2>This page can no longer export</h2>'
        f'<p class="status status-error" role="alert">{_e(RESTARTED_MESSAGE)}</p></section>'
    )


def _closed_page() -> str:
    body = (
        '<section class="content-panel"><p class="eyebrow">Team export</p><h1>This page is closed.</h1>'
        f"<p>If this tab stays open, close it yourself. Run {_e(UPGRADE_COMMAND)} when you need another export.</p>"
        "</section>"
    )
    return render_broker_auth_page("Team export", body, script="<script>window.close();</script>")


# The export button says it is working and cannot be sent twice (ui/DESIGN.md, broker pages).
_BUSY_SCRIPT = """
<script>
(() => {
  for (const form of document.querySelectorAll("form[method=post]")) {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type=submit]");
      const progress = form.querySelector("[data-export-progress]");
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        button.textContent = progress ? "Exporting\\u2026" : "Working\\u2026";
      }
      if (progress) progress.hidden = false;
    });
  }
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) window.location.reload();
  });
})();
</script>
"""

# While the export runs, ask how it stands and show the result once it ended.
_WAIT_SCRIPT = """
<script>
(() => {
  const statusUrl = `${window.location.pathname}/status`;
  async function poll() {
    try {
      const response = await fetch(statusUrl, {cache: "no-store"});
      if (response.ok) {
        const payload = await response.json();
        if (payload.status !== "__EXPORTING__") {
          window.location.replace(window.location.pathname);
          return;
        }
      }
    } catch (_error) {
      // Try again on the next round.
    }
    window.setTimeout(poll, 2000);
  }
  window.setTimeout(poll, 2000);
})();
</script>
""".replace("__EXPORTING__", EXPORT_PAGE_EXPORTING)


__all__ = [
    "ENDED_IGNORED",
    "ENDED_RECORDED",
    "NOT_STARTED_MESSAGE",
    "PICKUP_SECONDS",
    "RESTARTED_MESSAGE",
    "STATUS_CLOSED",
    "STATUS_CREATED",
    "STATUS_EXPORTING",
    "STATUS_FAILED",
    "STATUS_RESTARTED",
    "STATUS_SUCCESS",
    "TEAM_EXPORT_PAGE_SECONDS",
    "TeamExportFlow",
    "add_team_export_routes",
]

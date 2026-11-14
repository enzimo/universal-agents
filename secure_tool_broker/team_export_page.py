"""``/upgrade``: the page where an owner exports the whole team (guided upgrade phase 2).

The gateway opens this page for an owner
(``assistant_gateway.application.team_exports``) and watches it. The owner
types a passphrase twice. This page keeps it in the broker's memory only and
leaves an export request for the supervisor (``team_transfer.export_requests``;
the request holds no secret). The supervisor's export process collects the
passphrase once through ``TEAM_EXPORT_PASSPHRASE_ROUTE`` (operator caller
only), writes the archive into ``exports/`` and reports through
``TEAM_EXPORT_RESULT_ROUTE``. The page then shows the file, its parts and the
next steps, and the gateway posts the same in the chat.

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
  process; then it is dropped and the page says the launcher did not start
  the export. Once collected, the broker keeps no copy.
- A broker restart forgets waiting passphrases and running exports; the page
  opens again empty and the owner runs ``/upgrade`` again.
"""

from __future__ import annotations

import html
import logging
import os
import threading
from dataclasses import dataclass
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
from secure_tool_broker.model_key_page import NOT_ACTIVE_MESSAGE, current_person
from secure_tools.chat_commands import UPGRADE_COMMAND
from team_transfer.export_requests import (
    EXPORT_PAGE_CLOSED,
    EXPORT_PAGE_CREATED,
    EXPORT_PAGE_EXPORTING,
    EXPORT_PAGE_FAILED,
    EXPORT_PAGE_SUCCESS,
    NEXT_STEPS,
    TEAM_EXPORT_PAGE_METHOD,
    TEAM_EXPORT_PAGE_PROVIDER,
    TEAM_EXPORT_PASSPHRASE_ROUTE,
    TEAM_EXPORT_RESULT_ROUTE,
    TeamExportPassphraseRequestV1,
    TeamExportPassphraseV1,
    TeamExportRequestV1,
    TeamExportResultV1,
    new_export_id,
    section_lines,
    size_words,
    supervisor_runs_exports,
    write_export_request,
)
from team_transfer.titles import SECTION_TITLES

LOGGER = logging.getLogger(__name__)

TEAM_EXPORT_PAGE_SECONDS: Final = 60 * 60
PICKUP_SECONDS: Final = 5 * 60
# Page states (``status`` in the session and its status route), shared with the gateway's watch.
STATUS_CREATED: Final = EXPORT_PAGE_CREATED
STATUS_EXPORTING: Final = EXPORT_PAGE_EXPORTING
STATUS_SUCCESS: Final = EXPORT_PAGE_SUCCESS
STATUS_FAILED: Final = EXPORT_PAGE_FAILED
STATUS_CLOSED: Final = EXPORT_PAGE_CLOSED
NOT_STARTED_MESSAGE: Final = (
    "The launcher did not start the export within five minutes, so the passphrase was dropped. "
    f"Check that this team runs under the launcher, then run {UPGRADE_COMMAND} again."
)
NOT_LAUNCHED_MESSAGE: Final = (
    "This team was not started by the launcher, so nothing can run the export from this page. "
    "Use assistant_cli team export on the host instead."
)
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
    """One submitted export: its page and, until collected, its passphrase."""

    session: Any
    team_id: str
    owner_person_id: str
    requested_at: datetime
    passphrase: str = ""
    collected: bool = False


class _Exports:
    """Submitted exports by id. Form submissions run in threads, routes on the event loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, _WaitingExport] = {}

    def add(self, export_id: str, waiting: _WaitingExport) -> None:
        with self._lock:
            self._by_id[export_id] = waiting

    def collect(self, request: TeamExportPassphraseRequestV1) -> str:
        """The passphrase for this export, once; the broker keeps no copy afterwards."""

        with self._lock:
            waiting = self._by_id.get(request.export_id)
            if waiting is None or (waiting.team_id, waiting.owner_person_id) != (
                request.team_id,
                request.owner_person_id,
            ):
                raise web.HTTPNotFound(
                    text="No export with this id is waiting: the page expired, or the broker restarted."
                )
            if waiting.collected:
                raise web.HTTPConflict(text="This export's passphrase was already collected.")
            passphrase, waiting.passphrase, waiting.collected = waiting.passphrase, "", True
            return passphrase

    def finish(self, result: TeamExportResultV1) -> None:
        """Record how the export ended on its page; the export is forgotten afterwards."""

        with self._lock:
            waiting = self._by_id.get(result.export_id)
            if waiting is None or (waiting.team_id, waiting.owner_person_id) != (
                result.team_id,
                result.owner_person_id,
            ):
                raise web.HTTPNotFound(text="No export with this id is open.")
            del self._by_id[result.export_id]
        waiting.session.state[_RESULT] = result.model_dump(mode="json")
        waiting.session.state[_STATUS] = STATUS_SUCCESS if result.ok else STATUS_FAILED

    def drop_unstarted(self, session: Any, now: datetime) -> None:
        """Forget a passphrase nobody collected in time, and say why on the page."""

        export_id = str(session.state.get(_EXPORT_ID) or "")
        with self._lock:
            waiting = self._by_id.get(export_id)
            if waiting is None or waiting.collected or now - waiting.requested_at < timedelta(seconds=PICKUP_SECONDS):
                return
            del self._by_id[export_id]
        LOGGER.warning(
            "No export process collected the passphrase in time; it was dropped. "
            "event=team_export_not_started export_id=%s owner_person_id=%s",
            export_id,
            waiting.owner_person_id,
        )
        session.state[_STATUS] = STATUS_FAILED
        session.state[_RESULT] = {"ok": False, "error": NOT_STARTED_MESSAGE}

    def discard(self, session: Any) -> None:
        export_id = str(session.state.get(_EXPORT_ID) or "")
        with self._lock:
            self._by_id.pop(export_id, None)


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
        _EXPORTS.drop_unstarted(session, datetime.now(timezone.utc))
        status = str(session.state.get(_STATUS) or STATUS_CREATED)
        if status == STATUS_CLOSED:
            return _closed_page()
        if status == STATUS_EXPORTING:
            page = _intro_panel(closable=False) + _exporting_panel()
            return render_broker_auth_page(self.display_name, page, script=_WAIT_SCRIPT)
        if status in (STATUS_SUCCESS, STATUS_FAILED):
            body, script = _result_panel(session), _BUSY_SCRIPT
        else:
            body, script = _form_panel(session), SECRET_INPUT_TOGGLE_SCRIPT + _BUSY_SCRIPT
        page = _intro_panel(closable=True) + body + f'<section class="content-panel">{_close_form()}</section>'
        return render_broker_auth_page(self.display_name, page, script=script)

    def status_payload(self, session: Any) -> dict[str, Any]:
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
    if passphrase != str(form.get("passphrase_again") or ""):
        return "Nothing was exported. The two passphrases are different; type the same one twice."
    return ""


def add_team_export_routes(app: web.Application, *, team_id: str) -> None:
    """The export process's two routes; only the operator caller reaches /v1/transfer/ (SecureToolBrokerService)."""

    async def passphrase_handler(request: web.Request) -> web.Response:
        body = await _model(request, TeamExportPassphraseRequestV1)
        if body.team_id != team_id:
            raise web.HTTPForbidden(text="The request names another team.")
        passphrase = _EXPORTS.collect(body)
        LOGGER.info(
            "The export process collected its passphrase. event=team_export_passphrase_collected export_id=%s",
            body.export_id,
        )
        return web.json_response(TeamExportPassphraseV1(passphrase=passphrase).model_dump())

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
        return web.json_response({"status": "recorded"})

    app.router.add_post(TEAM_EXPORT_PASSPHRASE_ROUTE, passphrase_handler)
    app.router.add_post(TEAM_EXPORT_RESULT_ROUTE, result_handler)


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


def _intro_panel(*, closable: bool) -> str:
    parts = "".join(f"<li>{_e(title)}</li>" for title in SECTION_TITLES.values())
    return (
        f'<section class="content-panel" id="{_TOP_ID}"><p class="eyebrow">Team export</p>'
        "<h1>Export the team for an upgrade</h1>"
        "<p class=\"lede\">This writes one encrypted file with everything a new version needs to take over this "
        "team. On the new instance you choose what to import; everything starts selected.</p>"
        f"<p>The file holds:</p><ul>{parts}</ul>"
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
        "<p class=\"note\">At least 12 characters. Keep it somewhere safe: you need it to import the file, it is "
        "not saved anywhere, and nobody can open the file without it. It goes to the secure broker and is never "
        "sent to the agent.</p>"
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
        return (
            f'<section class="content-panel" id="{_FORM_ID}"><h2>The team was not exported</h2>'
            f'<p class="status status-error" role="alert">No file was written. {why}</p>'
            f"<p>Run {_e(UPGRADE_COMMAND)} in the chat to try again.</p></section>"
        )
    result = TeamExportResultV1.model_validate(raw)
    parts = "".join(f"<li>{_e(line)}</li>" for line in section_lines(result))
    steps = "".join(f"<li>{_e(step)}</li>" for step in NEXT_STEPS)
    return (
        f'<section class="content-panel" id="{_FORM_ID}"><h2>The team is exported</h2>'
        f'<p class="status" role="status">Wrote <code>exports/{_e(result.file_name)}</code> in this instance\'s '
        f"data folder ({_e(size_words(result.size_bytes))}).</p>"
        f"<p>It holds:</p><ul>{parts}</ul><h2>Next steps</h2><ol>{steps}</ol></section>"
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
    "PICKUP_SECONDS",
    "STATUS_CLOSED",
    "STATUS_CREATED",
    "STATUS_EXPORTING",
    "STATUS_FAILED",
    "STATUS_SUCCESS",
    "TEAM_EXPORT_PAGE_SECONDS",
    "TeamExportFlow",
    "add_team_export_routes",
]

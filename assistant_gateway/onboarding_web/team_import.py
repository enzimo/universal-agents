"""Onboarding's Import: start a fresh instance from a team export.

The guided upgrade (``ua-architecture/plans/guided_upgrade_plan.md``, phase
1): the operator copies an export (``assistant_cli team export``) into the new
data folder's ``import/`` folder (``InstanceLayout.import_dir``), chooses
**Import a team export** on onboarding's Let's Begin step, picks the file and
types its passphrase. The same review the operator's loopback page offers
(``team_transfer.review_session``) then runs inside onboarding's own server:
select and edit records, confirm each selected schedule, validate, import.
The import writes the instance in place of onboarding's normal write, and
onboarding ends as Launch does: the service exits and the container starts
the team.

Why these choices:

- **No browser upload.** Exports with files can be gigabytes and onboarding
  takes 512 KiB request bodies (owner's decision 2026-10-11), so the file is
  copied into ``import/`` on the host.
- **Who may use the review.** Onboarding has no accounts yet. Knowing the
  export's passphrase is the proof: a correct passphrase gives this browser
  tab a random token, sent only as the ``X-Transfer-Token`` header (never a
  cookie, so other sites cannot make the browser send it). Opening is
  serialized, so passphrase guesses cannot run in parallel; each costs one
  Argon2id derivation (about a second and 256 MiB).
- **The passphrase is used once.** It derives the export's keys while
  opening and is never stored, logged or sent back. The credentials, which
  only the running broker stores, are left still encrypted with the export's
  encryption subkey for the first start (``team_transfer.credential_handover``).
- **Where secrets come from** is asked here (``DestinationChoices``), starting
  from the old instance's choice, because the new host may differ.
- **Clean-up.** After a successful import the copied export and its review
  folder are deleted; the original stays wherever it was exported.
- **A team id this version cannot carry** (owner's decision 103,
  ``team_transfer.team_ids``). Opening such an export prepares no review: the
  reply carries a question (why the id does not fit, and a suggested new id),
  and ``POST /api/team-import/team-id`` prepares the review under the id the
  owner confirms, checked like a typed onboarding id. Agent types that do not
  fit are still refused when the export is opened. The review folder is fixed
  per export, so opening the export again offers the id chosen before; another
  id prepares the review again (``prepare_review(restart_for_another_id=True)``),
  because a team id cannot be changed after the import.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web
from pydantic import Field, ValidationError

from runtime_settings.instance_layout import InstanceLayout
from runtime_settings.secrets_source import SECRETS_SOURCE_ENV_FILE, normalize_secrets_source
from team_transfer.container import TransferFileError, TransferReader
from team_transfer.crypto import TransferKeyError
from team_transfer.errors import NewTeamIdRefused, TransferError
from team_transfer.format import TransferModel
from team_transfer.protocol1_ids import require_protocol1_agent_types
from team_transfer.review import ReviewFiles, prepare_review, saved_review_team_id
from team_transfer.review_session import ReviewApplyV1, ReviewSession, ReviewUpdateV1
from team_transfer.sections import DestinationChoices
from team_transfer.team_ids import new_team_ids, suggested_team_id, team_id_explanation

LOGGER = logging.getLogger(__name__)

API_PREFIX = "/api/team-import"
PAGE_PATH = "/team-import"
TOKEN_HEADER = "X-Transfer-Token"
# Onboarding's own assets folder; the server serves the page's script and styles from it too.
PAGE_FILE = Path(__file__).resolve().parent / "assets" / "team_import.html"
_FAILURES = (TransferError, TransferFileError, TransferKeyError, ValidationError, ValueError, OSError)
_WAITING_REASON = "stored when the team starts, by its broker"
_CHOOSE_TEAM_ID_FIRST = "Choose the team's new id first: open the export again from onboarding's Let's Begin step."


class OpenExportV1(TransferModel):
    file_name: str = Field(min_length=1, max_length=255)
    passphrase: str = Field(min_length=1, max_length=1024, repr=False)


class DestinationV1(TransferModel):
    secrets_source: str


class TeamIdV1(TransferModel):
    # Empty is allowed here so the id rule answers it in words by the field.
    team_id: str = Field(max_length=255)


@dataclass
class _OpenImport:
    reader: TransferReader
    archive_path: Path
    review_dir: Path
    token: str = field(repr=False)
    # None until the review is prepared: when the export is opened for a team that keeps its id,
    # after the owner chose a new one (``team_id_question``) for a team whose id does not fit.
    session: ReviewSession | None = None
    exported_secrets_source: str = SECRETS_SOURCE_ENV_FILE
    team_id_question: dict[str, Any] | None = None
    # Set while the review is prepared under a new id, which may replace the review folder; the
    # import refuses meanwhile, and the id step refuses while an import runs.
    preparing: bool = False

    @property
    def review(self) -> ReviewSession:
        if self.session is None:
            raise TransferError(_CHOOSE_TEAM_ID_FIRST)
        return self.session

    def start_review(self, files: ReviewFiles) -> None:
        self.exported_secrets_source = _exported_secrets_source(files.archive)
        self.session = ReviewSession(
            files,
            prepare_credentials=_leave_for_first_start,
            destination=DestinationChoices(secrets_source=self.exported_secrets_source),
        )


class TeamImport:
    """The routes and the one open export of onboarding's Import."""

    def __init__(
        self,
        layout: Callable[[], InstanceLayout],
        *,
        on_imported: Callable[[dict[str, Any]], bool],
        logger: logging.Logger | None = None,
    ) -> None:
        # ``on_imported`` records the completion and says whether the service restarts.
        self._layout = layout
        self._on_imported = on_imported
        self._logger = logger or LOGGER
        self._open: _OpenImport | None = None
        self._opening = asyncio.Lock()
        # Opening another export closes the open one, so never while it imports.
        self._importing = False

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get(PAGE_PATH, self._page)
        app.router.add_get(f"{API_PREFIX}/exports", self._exports)
        app.router.add_post(f"{API_PREFIX}/open", self._open_export)
        app.router.add_post(f"{API_PREFIX}/team-id", self._guarded(self._choose_team_id, needs_review=False))
        app.router.add_get(f"{API_PREFIX}/review", self._guarded(self._review))
        app.router.add_post(f"{API_PREFIX}/record", self._guarded(self._record))
        app.router.add_post(f"{API_PREFIX}/destination", self._guarded(self._destination))
        app.router.add_post(f"{API_PREFIX}/validate", self._guarded(self._validate))
        app.router.add_post(f"{API_PREFIX}/import", self._guarded(self._import))

    async def close(self, _app: web.Application | None = None) -> None:
        if self._open is not None:
            self._open.reader.close()
            self._open = None

    # -- before opening ----------------------------------------------------------------

    async def _page(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(PAGE_FILE, headers=_private_headers())

    async def _exports(self, _request: web.Request) -> web.Response:
        layout = self._layout()
        return _json(
            {
                "import_dir": str(layout.import_dir),
                "problem": _not_fresh(layout),
                "exports": _list_exports(layout.import_dir),
            }
        )

    async def _open_export(self, request: web.Request) -> web.Response:
        try:
            payload = OpenExportV1.model_validate(await request.json())
        except (ValidationError, ValueError):
            return _error("Choose an export file and type its passphrase.")
        if self._importing:
            return _error("An import is running. Wait for it to finish.", status=409)
        layout = self._layout()
        problem = _not_fresh(layout)
        if problem:
            return _error(problem, status=409)
        try:
            path = _export_path(layout.import_dir, payload.file_name)
        except TransferError as exc:
            return _error(str(exc))
        async with self._opening:
            try:
                opened = await asyncio.to_thread(self._open_reader, layout, path, payload.passphrase)
            except _FAILURES as exc:
                self._logger.warning(
                    "A team export could not be opened. event=onboarding_team_import_open_failed "
                    "file_name=%s error_type=%s",
                    path.name,
                    type(exc).__name__,
                )
                return _error(str(exc))
            if self._importing:
                # An import started in another tab while this one was opening.
                opened.reader.close()
                return _error("An import is running. Wait for it to finish.", status=409)
            await self.close()
            self._open = opened
        manifest = opened.reader.manifest
        self._logger.info(
            "Opened a team export for import. event=onboarding_team_import_opened file_name=%s team_id=%r "
            "exported_by_version=%s asks_for_team_id=%s",
            path.name,
            manifest.team_id,
            manifest.application_version,
            opened.team_id_question is not None,
        )
        return _json({"token": opened.token, "page": PAGE_PATH, "team_id_question": opened.team_id_question})

    def _open_reader(self, layout: InstanceLayout, path: Path, passphrase: str) -> _OpenImport:
        reader = TransferReader.open(path, passphrase)
        try:
            # Before anything is written: agent types this version cannot run are refused on the page.
            require_protocol1_agent_types(reader)
            opened = _OpenImport(
                reader=reader,
                archive_path=path,
                review_dir=layout.team_import_review_dir / reader.manifest.mac[:24],
                token=secrets.token_urlsafe(32),
            )
            opened.team_id_question = _team_id_question(reader, opened.review_dir)
            if opened.team_id_question is None:
                _private_folder(opened.review_dir.parent)
                # Reopening the same export keeps the selections and edits made so far.
                opened.start_review(prepare_review(reader, layout.root, opened.review_dir))
        except BaseException:
            reader.close()
            raise
        return opened

    async def _choose_team_id(self, request: web.Request, opened: _OpenImport) -> web.Response:
        """The new team id for an export whose id does not fit; the review is prepared under it."""

        payload = TeamIdV1.model_validate(await request.json())
        if opened.team_id_question is None:
            return _error("This export's team id fits this version, so the team keeps it.", status=409)
        if self._importing:
            return _error("An import is running. Wait for it to finish.", status=409)
        layout = self._layout()
        problem = _not_fresh(layout)
        if problem:
            return _error(problem, status=409)
        async with self._opening:
            if self._open is not opened:
                return _error("Open the export again from onboarding's Let's Begin step.", status=403)
            if self._importing:
                return _error("An import is running. Wait for it to finish.", status=409)
            opened.preparing = True
            try:
                files, previous = await asyncio.to_thread(_review_under_new_id, layout, opened, payload.team_id)
            except NewTeamIdRefused as exc:
                return _error(str(exc), field="team_id")
            finally:
                opened.preparing = False
            opened.start_review(files)
        ids = files.reader.team_ids
        restarted = previous is not None and previous != ids.imported
        self._logger.info(
            "Chose the imported team's new id. event=onboarding_team_import_team_id_chosen team_id=%s "
            "exported_team_id=%r restarted_review=%s",
            ids.imported,
            ids.exported,
            restarted,
        )
        return _json({"page": PAGE_PATH, "team_id": ids.imported, "restarted_review": restarted})

    # -- the review --------------------------------------------------------------------

    def _guarded(
        self,
        handler: Callable[[web.Request, _OpenImport], Awaitable[web.StreamResponse]],
        *,
        needs_review: bool = True,
    ) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
        async def guarded(request: web.Request) -> web.StreamResponse:
            opened = self._open
            if opened is None or not hmac.compare_digest(
                request.headers.get(TOKEN_HEADER, ""), opened.token
            ):
                return _error(
                    "Open the export again from onboarding's Let's Begin step.", status=403
                )
            if needs_review and opened.session is None:
                return _error(_CHOOSE_TEAM_ID_FIRST, status=409)
            try:
                return await handler(request, opened)
            except _FAILURES as exc:
                return _error(str(exc))

        return guarded

    async def _review(self, _request: web.Request, opened: _OpenImport) -> web.Response:
        destination = opened.review.destination or DestinationChoices()
        return _json(
            {
                **opened.review.state(),
                "review_directory": None,
                "destination": {
                    "secrets_source": destination.secrets_source,
                    "exported_secrets_source": opened.exported_secrets_source,
                },
            }
        )

    async def _record(self, request: web.Request, opened: _OpenImport) -> web.Response:
        payload = ReviewUpdateV1.model_validate(await request.json())
        return _json(await opened.review.update(payload))

    async def _destination(self, request: web.Request, opened: _OpenImport) -> web.Response:
        payload = DestinationV1.model_validate(await request.json())
        source = normalize_secrets_source(payload.secrets_source)
        await opened.review.set_destination(DestinationChoices(secrets_source=source))
        return _json({"saved": True, "secrets_source": source})

    async def _validate(self, _request: web.Request, opened: _OpenImport) -> web.Response:
        return _json(await opened.review.validate())

    async def _import(self, request: web.Request, opened: _OpenImport) -> web.Response:
        payload = ReviewApplyV1.model_validate(await request.json())
        if opened.preparing:
            return _error("The review is being prepared under the new team id. Wait for it, then validate again.",
                          status=409)
        session = opened.review
        self._importing = True
        try:
            result = await session.apply(payload)
            layout = self._layout()
            completion = await asyncio.to_thread(_completion, layout, opened)
        finally:
            self._importing = False
        await self.close()
        _remove_import_files(opened, self._logger)
        restarting = self._on_imported(completion)
        ids = session.files.reader.team_ids
        self._logger.info(
            "Team imported during onboarding. event=onboarding_team_imported team_id=%s exported_team_id=%r "
            "records=%s credentials_waiting=%s missing_secrets=%s restarting=%s",
            ids.imported,
            ids.exported,
            result["imported"],
            len(result["credentials"]),
            ",".join(item["name"] for item in completion["missing_secrets"]),
            restarting,
        )
        return _json(
            {
                **result,
                "missing_secrets": completion["missing_secrets"],
                "restarting": restarting,
                "next_url": "/",
                "message": _result_message(completion, restarting),
            }
        )


# -- helpers ---------------------------------------------------------------------------


def _leave_for_first_start(files: Any, layout: Any, keys: list[str]) -> list[dict[str, Any]]:
    """Onboarding's credential step: the broker is not running yet (``credential_handover``)."""

    from team_transfer.credential_handover import write_credential_handover

    write_credential_handover(files.reader, layout, keys)
    return [{"key": key, "imported": False, "waiting": True, "reason": _WAITING_REASON} for key in keys]


def _team_id_question(reader: TransferReader, review_dir: Path) -> dict[str, Any] | None:
    """What the page asks when the export's team id does not fit; None when the team keeps its id.

    The field starts with the id chosen when this export was opened before (its review folder
    recorded it), so an owner who comes back keeps their choice; otherwise with the suggestion.
    """

    exported = reader.manifest.team_id
    explanation = team_id_explanation(exported)
    if explanation is None:
        return None
    chosen_before = saved_review_team_id(reader.manifest, review_dir)
    if chosen_before == exported:
        chosen_before = None
    return {
        "exported_team_id": exported,
        "explanation": explanation,
        "suggestion": chosen_before or suggested_team_id(exported),
        "chosen_before": chosen_before,
    }


def _review_under_new_id(layout: InstanceLayout, opened: _OpenImport, team_id: str) -> tuple[ReviewFiles, str | None]:
    """The review prepared under ``team_id``, and the id an earlier review of this export had recorded."""

    # The rule a typed id must pass, before any folder is created: a refused id writes nothing.
    new_team_ids(opened.reader.manifest.team_id, team_id)
    previous = saved_review_team_id(opened.reader.manifest, opened.review_dir)
    _private_folder(opened.review_dir.parent)
    files = prepare_review(
        opened.reader, layout.root, opened.review_dir, new_team_id=team_id, restart_for_another_id=True
    )
    return files, previous


def _completion(layout: InstanceLayout, opened: _OpenImport) -> dict[str, Any]:
    """What onboarding records after the import, as ``_handle_complete`` does after its write."""

    from assistant_gateway.config import GatewayInstanceConfig
    from assistant_gateway.instance_secrets import environment_secret_values, missing_secrets

    config = GatewayInstanceConfig.load_saved(layout.instance_config_path)
    missing: list[dict[str, Any]] = []
    if config.launch.secrets_from_environment:
        # Secrets the old team.env held must now come from the environment too.
        entered = [
            record.key.split("/", 1)[1]
            for record in opened.review.files.review.records
            if record.section == "operator_secrets"
            and record.selected
            and record.key.startswith("environment/")
        ]
        missing = [
            item.as_dict()
            for item in missing_secrets(config, env=environment_secret_values(), entered_names=entered)
        ]
    return {
        "config_path": str(config.config_path),
        "env_path": None if config.launch.secrets_from_environment else str(
            config.launch.env_file_path or layout.team_env_path
        ),
        "secrets_source": config.launch.secrets_source,
        "missing_secrets": missing,
        "unsaved_secret_names": [],
        "source_env_ignored": False,
        "skills_repos": [],
        "team_import": {
            # The id the instance runs under, and the export's when the import gave the team a new one.
            "team_id": opened.review.files.reader.team_ids.imported,
            "exported_team_id": opened.reader.manifest.team_id,
            "exported_by_version": opened.reader.manifest.application_version,
            "imported_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    }


def _result_message(completion: dict[str, Any], restarting: bool) -> str:
    if completion["missing_secrets"]:
        return (
            "Imported. This instance takes its secrets from the environment, and the ones listed below are "
            "missing. Add them as Docker secrets and restart the container; the team then starts."
        )
    if restarting:
        return "Imported. The team is starting; this page opens Web Chat when it is ready."
    return "Imported. Start the team with the runtime command; it stores the credentials at its first start."


def _remove_import_files(opened: _OpenImport, logger: logging.Logger) -> None:
    """The imported copy and its review folder; the original export stays where it was made."""

    for remove, path in ((os.unlink, opened.archive_path), (shutil.rmtree, opened.review_dir)):
        try:
            remove(path)
        except OSError as exc:
            logger.error(
                "Could not delete an imported export file; delete it by hand. "
                "event=onboarding_team_import_cleanup_failed path=%s error_type=%s",
                path,
                type(exc).__name__,
            )


def _exported_secrets_source(archive: dict[str, dict[str, Any]]) -> str:
    """The old instance's ``launch.secrets_source``: carried when not the default."""

    carried = archive.get("settings", {}).get("launch.secrets_source")
    if carried is None:
        return SECRETS_SOURCE_ENV_FILE
    return normalize_secrets_source(carried["value"])


def _not_fresh(layout: InstanceLayout) -> str:
    """Why this data folder cannot take a first import, or "" when it can."""

    from team_transfer.service import require_fresh_target

    try:
        require_fresh_target(layout)
    except TransferError as exc:
        return (
            f"{exc} Import works only on a fresh data folder: start this version with an empty one, "
            "then copy the export into its import folder."
        )
    return ""


def _list_exports(folder: Path) -> list[dict[str, Any]]:
    try:
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
    except FileNotFoundError:
        return []
    found = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        info = entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            continue
        found.append(
            {
                "file_name": entry.name,
                "size_bytes": info.st_size,
                "modified_at_utc": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
            }
        )
    return found


def _export_path(folder: Path, file_name: str) -> Path:
    """A regular file directly inside ``import/``; never a link or a path elsewhere."""

    if file_name != Path(file_name).name or file_name.startswith("."):
        raise TransferError("Choose one of the listed export files.")
    path = folder / file_name
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise TransferError(f"{file_name} is no longer in the import folder.") from exc
    if not stat.S_ISREG(info.st_mode):
        raise TransferError(f"{file_name} is not a regular file.")
    return path


def _private_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _private_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def _json(value: Any, status: int = 200) -> web.Response:
    return web.json_response(value, status=status, headers=_private_headers())


def _error(message: str, *, status: int = 400, field: str | None = None) -> web.Response:
    """A refusal; ``field`` names the input it belongs to, so the page shows it by that field."""

    body: dict[str, Any] = {"error": message}
    if field is not None:
        body["field"] = field
    return _json(body, status=status)


__all__ = ["API_PREFIX", "PAGE_PATH", "TeamImport"]

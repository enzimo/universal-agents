"""The gateway's side of a person's records when they are disabled, removed or restored.

Implements ``GatewayPersonRecords`` for ``person_lifecycle`` over the stores
the gateway already owns or opens:

- its own database (``GatewayStateStore.forget_person_records``): chat
  sessions, Web Chat history, sign-ins and preferences, and the chat upload
  files of the deleted attachment rows (deleted only inside the uploads
  folder, never through a link);
- the access store: waiting access requests are withdrawn through
  ``AccessRequests.withdraw`` (so pending hand-downs are refused and the Inbox
  card closes); on removal the grants for the person's work, grants to them
  and grants they gave (including shares of their folder) are revoked with
  their hand-downs, their personal account links are revoked, and their
  requests, notices and starter-profile choice are deleted. Revoked grants and
  links stay as inert rows beside the access event log, which is the audit
  record;
- the hold record (``storage.person_departure_repository``) and the person's
  folder (``access_control.held_person_folders``), moved to the holding area
  and back.

Every path comes from the instance layout.
"""

from __future__ import annotations

import logging
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from access_control.folders import FOLDER_ROOT_USERS
from access_control.held_person_folders import hold_folder, purge_folder, restore_folder
from access_control.requests import AccessRequests
from access_control.safe_open import UnsafePath
from assistant_gateway.application.identity_service import GatewayIdentityService
from assistant_gateway.application.manager_commands import GatewayManagerCommandService
from assistant_gateway.application.person_lifecycle import (
    ForgottenRecords,
    GatewayPersonLifecycle,
    GatewayPersonLifecycleSettings,
    HeldPersonData,
)
from assistant_gateway.application.ports import GatewayClock, GatewayPersonSchedules, GatewaySecureBroker
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.personal_setup import GatewayPersonalSetup
from assistant_gateway.person_setup import GatewayPersonSetupStore
from assistant_gateway.store import GatewayStateStore
from custom_messages.contracts.access_v2 import (
    AccessRequestStatus,
    AccessRequestV1,
    GrantKind,
    GrantStatus,
    GrantV1,
    LinkOwnerKind,
    SubjectKind,
    SubjectRefV1,
)
from runtime_settings.instance_layout import InstanceLayout, person_folder_name
from storage.access_repository import AccessRepository
from storage.person_departure_repository import PersonDepartureRepository, PersonHold

logger = logging.getLogger(__name__)

WITHDRAWN_BECAUSE_DEPARTED = "The person this request concerns can no longer use the team."
_LIVE_GRANT_STATES = frozenset({GrantStatus.ACTIVE, GrantStatus.PENDING})


def request_concerns(request: AccessRequestV1, person_id: str) -> bool:
    """Whether a request is for the person's work, made by them, or waits for them to decide it."""

    owner = request.change.owner_person_id if request.change is not None else None
    return person_id in {request.person_id, request.requested_by_person_id, owner}


def grant_concerns(grant: GrantV1, person_id: str) -> bool:
    """Whether an access grant serves the person's work, is theirs, was given by them, or shares their folder.

    Blocks are never revoked here: a block an administrator placed keeps
    protecting the team after that administrator leaves.
    """

    if grant.kind is not GrantKind.ACCESS:
        return False
    person = SubjectRefV1(kind=SubjectKind.USER, id=person_id)
    home = f"{FOLDER_ROOT_USERS}/{person_folder_name(person_id)}"
    in_home = grant.target.id == home or grant.target.id.startswith(home + "/")
    return grant.for_person_id == person_id or grant.subject == person or grant.granted_by == person or in_home


class GatewayPersonDeparture:
    """``GatewayPersonRecords`` over the gateway's stores (module docstring)."""

    def __init__(
        self,
        *,
        state_store: GatewayStateStore,
        access: Callable[[], AccessRepository],
        requests: Callable[[], AccessRequests],
        departures: Callable[[], PersonDepartureRepository],
        layout: InstanceLayout,
        team_id: str,
        clock: GatewayClock,
    ) -> None:
        self._state_store = state_store
        self._access = access
        self._requests = requests
        self._departures = departures
        self._layout = layout
        self._team_id = team_id
        self._clock = clock

    # -- requests ---------------------------------------------------------------------

    def withdraw_requests(self, person_id: str) -> int:
        requests = self._requests()
        waiting = [
            request
            for request in self._access().list_requests(self._team_id, status=AccessRequestStatus.WAITING)
            if request_concerns(request, person_id)
        ]
        for request in waiting:
            requests.withdraw(request.request_id, why=WITHDRAWN_BECAUSE_DEPARTED)
        if waiting:
            logger.info(
                "Withdrew a departing person's waiting access requests. event=person_requests_withdrawn "
                "person_id=%s requests=%d",
                person_id,
                len(waiting),
            )
        return len(waiting)

    # -- removal ----------------------------------------------------------------------

    def forget(self, person_id: str, *, actor_id: str) -> ForgottenRecords:
        deleted = self._state_store.forget_person_records(person_id)
        problems = self._delete_uploads(deleted.attachment_paths)
        access = self._access()
        actor = SubjectRefV1(kind=SubjectKind.USER, id=actor_id)
        now = self._clock.now_utc()
        grants = self._revoke_grants(access, person_id, actor, now)
        links = [
            link
            for link in access.list_links(self._team_id)
            if link.owner_kind is LinkOwnerKind.PERSON and link.owner_id == person_id
        ]
        for link in links:
            access.revoke_link(link.link_id, actor=actor, at=now)
        request_ids = [
            request.request_id for request in access.list_requests(self._team_id) if request_concerns(request, person_id)
        ]
        requests, notices, _choice = access.forget_person_records(self._team_id, person_id, request_ids=request_ids)
        logger.info(
            "Deleted a removed person's gateway records. event=person_records_forgotten person_id=%s actor=%s "
            "sessions=%d transcript_messages=%d sign_ins=%d grants_revoked=%d links_revoked=%d requests=%d notices=%d",
            person_id,
            actor_id,
            deleted.sessions,
            deleted.transcript_messages,
            deleted.sign_ins,
            grants,
            len(links),
            requests,
            notices,
        )
        return ForgottenRecords(
            sessions=deleted.sessions,
            transcript_messages=deleted.transcript_messages,
            sign_ins=deleted.sign_ins,
            grants_revoked=grants,
            links_revoked=len(links),
            requests_deleted=requests,
            notices_deleted=notices,
            credential_keys=tuple(sorted({link.credential_key for link in links})),
            problems=tuple(problems),
        )

    def _revoke_grants(self, access: AccessRepository, person_id: str, actor: SubjectRefV1, now: datetime) -> int:
        revoked: set[str] = set()
        for grant in access.list_team(self._team_id):
            if grant.grant_id in revoked or grant.status not in _LIVE_GRANT_STATES or not grant_concerns(grant, person_id):
                continue
            revoked.update(access.revoke(grant.grant_id, actor=actor, at=now))
        return len(revoked)

    def _delete_uploads(self, paths: tuple[str, ...]) -> list[str]:
        """Delete chat upload files, only regular files inside the uploads folder (never through a link)."""

        base = self._layout.uploads_dir.resolve()
        failed = 0
        for raw in paths:
            path = Path(raw)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or base not in path.resolve().parents:
                continue
            try:
                path.unlink()
            except OSError:
                failed += 1
                logger.exception(
                    "Could not delete a removed person's chat upload. event=person_upload_delete_failed path=%s", raw
                )
        return [f"{failed} of their chat uploads could not be deleted."] if failed else []

    # -- holding ----------------------------------------------------------------------

    def hold(self, person_id: str, *, until: datetime) -> HeldPersonData:
        departures = self._departures()
        now = self._clock.now_utc()
        existing = departures.hold_of(self._team_id, person_id)
        if existing is None or not existing.restorable(now):
            if existing is not None and existing.restored_at is None and existing.purged_at is None:
                self._purge(departures, existing)  # an expired hold not purged yet
            existing = departures.record_hold(self._team_id, person_id, at=now, hold_until=until)
        try:
            moved = hold_folder(self._layout, person_id)
        except UnsafePath as exc:
            logger.error(
                "Could not move a removed person's folder to the holding area; it stays in place. "
                "event=person_folder_hold_failed person_id=%s reason=%s error=%s",
                person_id,
                exc.reason,
                exc,
            )
            return HeldPersonData(
                hold_until=existing.hold_until,
                problem=f"Their folder could not be moved to the holding area ({exc}); it stays in place.",
            )
        return HeldPersonData(hold_until=existing.hold_until, folder_moved=moved)

    def _purge(self, departures: PersonDepartureRepository, hold: PersonHold) -> None:
        purge_folder(self._layout, hold.person_id)
        departures.delete_held_rows(hold.team_id, hold.person_id)
        departures.mark_purged(hold.team_id, hold.person_id, at=self._clock.now_utc())

    def held(self, person_id: str) -> HeldPersonData | None:
        hold = self._departures().hold_of(self._team_id, person_id)
        if hold is None:
            return None
        return HeldPersonData(
            hold_until=hold.hold_until, restored=hold.restored_at is not None, purged=hold.purged_at is not None
        )

    def restore(self, person_id: str) -> HeldPersonData:
        departures = self._departures()
        hold = departures.hold_of(self._team_id, person_id)
        if hold is None:
            raise KeyError(person_id)
        try:
            moved = restore_folder(self._layout, person_id)
        except UnsafePath as exc:
            logger.error(
                "Could not move a person's held folder back. event=person_folder_restore_failed person_id=%s "
                "reason=%s error=%s",
                person_id,
                exc.reason,
                exc,
            )
            return HeldPersonData(
                hold_until=hold.hold_until,
                problem=f"Their held folder could not be moved back ({exc}).",
            )
        departures.mark_restored(self._team_id, person_id, at=self._clock.now_utc())
        return HeldPersonData(hold_until=hold.hold_until, restored=True, folder_moved=moved)




def build_gateway_person_lifecycle(
    config: GatewayInstanceConfig,
    *,
    identity: GatewayIdentityService,
    state_store: GatewayStateStore,
    manager_commands: GatewayManagerCommandService,
    broker: GatewaySecureBroker,
    access_requests: Callable[[], AccessRequests],
    person_schedules: GatewayPersonSchedules,
    clock: GatewayClock,
    personal_setup: GatewayPersonalSetup,
    logger: logging.Logger | None = None,
) -> GatewayPersonLifecycle:
    """The person lifecycle for this gateway: its stores, the manager, the broker and per-person schedules."""

    storage = config.manager_storage_settings
    records = GatewayPersonDeparture(
        state_store=state_store,
        access=lambda: AccessRepository(storage),
        requests=access_requests,
        departures=lambda: PersonDepartureRepository(storage),
        layout=config.layout,
        team_id=config.gateway.team_id,
        clock=clock,
    )
    return GatewayPersonLifecycle(
        settings=GatewayPersonLifecycleSettings(
            team_id=config.gateway.team_id,
            removed_data_retention_days=config.gateway.users.removed_data_retention_days,
        ),
        identity=identity,
        records=records,
        work=manager_commands,
        broker=broker,
        person_schedules=person_schedules,
        clock=clock,
        setup=GatewayPersonSetupStore(
            access=lambda: AccessRepository(storage),
            layout=config.layout,
            team_id=config.gateway.team_id,
            personal_setup=personal_setup,
        ),
        logger=logger,
    )


__all__ = ["GatewayPersonDeparture", "build_gateway_person_lifecycle", "grant_concerns", "request_concerns"]

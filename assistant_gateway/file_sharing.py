"""People sharing files with each other, and sending files into each other's folders (decision D15).

A person's folder (``workspaces/users/<folder>``) is private. Its owner can:

- **share** a file or folder in it with another team member, to read or to
  read and change, until they stop sharing or for a set time. The share is
  a grant to that person on the item (``TargetKind.FILE`` or ``FOLDER``),
  which only the owner may give (``std:owner``; administrators cannot open
  other people's folders). Agents working for the recipient then use the
  item at the shared level (``AccessService.shared_with``);
- **send** a file into another person's folder. That needs the recipient's
  approval or an administrator's, so it is a change request the gateway
  applies when approved (``GatewayFileSharing.apply``): the file's content is
  pinned by its SHA-256 when the request is made, and the gateway copies
  exactly that content, without following links, into
  ``users/<recipient>/received/<sender>/``. A move removes the original after
  the copy. Sending a file to the shared ``team/`` folder needs no approval.

Every file is opened through ``access_control.safe_open`` from the workspaces
root: agents' code can write these folders, so a planted link is refused, not
followed.
"""

from __future__ import annotations

import hashlib
import logging
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath

from access_control import safe_open
from access_control.decisions import HandDownRefused
from access_control.entities import PersonPrincipal
from access_control.folders import FOLDER_ROOT_TEAM, FOLDER_ROOT_USERS, normalize_folder_id
from access_control.requests import FILE_DELIVERY_CHANGE, AccessRequests, DecisionClaim
from access_control.sensitive_paths import sensitive_reason
from access_control.safe_open import UnsafePath
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    AccessNoticeKind,
    AccessNoticeV1,
    AccessRequestStatus,
    AccessRequestV1,
    ChangeProposalV1,
    GrantKind,
    GrantLifetime,
    GrantStatus,
    GrantV1,
    ReviewItemV1,
    SubjectKind,
    SubjectRefV1,
    TargetKind,
    TargetRefV1,
    contract_digest,
    summary_line,
)
from id_generation import new_id
from runtime_settings.instance_layout import person_folder_name
from storage.access_repository import AccessStoreConflict

logger = logging.getLogger(__name__)

FILE_DELIVERY_KIND = FILE_DELIVERY_CHANGE
RECEIVED_DIRNAME = "received"
MAX_DELIVERY_BYTES = 256 * 1024 * 1024
SHARE_LEVELS = {"read": AccessLevel.READ, "write": AccessLevel.WRITE, "change": AccessLevel.WRITE}
MAX_SHARE_HOURS = 24 * 365


class FileSharingRefused(ValueError):
    """Sharing or sending cannot be done; the message says why."""


@dataclass(frozen=True)
class OwnItem:
    """A file or folder in the acting person's own folder, found without following links."""

    parts: tuple[str, ...]  # below the workspaces folder: ("users", <folder>, ...)
    is_file: bool
    size: int

    @property
    def target(self) -> TargetRefV1:
        return TargetRefV1(kind=TargetKind.FILE if self.is_file else TargetKind.FOLDER, id="/".join(self.parts))

    @property
    def shown(self) -> str:
        inside = "/".join(self.parts[2:])
        return (inside + ("" if self.is_file else "/")) if inside else "your whole folder"


def _size_words(size: int) -> str:
    for unit, scale in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} bytes"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class GatewayFileSharing:
    """Sharing and sending for signed-in people (``/share``, ``/send``, the workspace browser)."""

    def __init__(
        self,
        *,
        requests: Callable[[], AccessRequests],
        person: Callable[[str], PersonPrincipal | None],
        workspaces_dir: Path,
        team_people: Callable[[], list[tuple[str, str]]],
        display_name: Callable[[str], str],
        authorization_enabled: bool,
    ) -> None:
        self._requests = requests
        self._person = person
        self._workspaces = Path(workspaces_dir)
        self._team_people = team_people
        self._display_name = display_name
        self._enabled = authorization_enabled

    def display_name(self, person_id: str) -> str:
        return self._display_name(person_id)

    def team_people(self) -> list[tuple[str, str]]:
        """Active team members, (id, name), for share and send pickers."""

        return self._team_people()

    # -- who and what ---------------------------------------------------------------

    def acting(self, person_id: str) -> PersonPrincipal:
        if not self._enabled:
            raise FileSharingRefused(
                "Access control is off on this instance (trusted single-person mode), so there is nobody "
                "to share with. Turn on authorization.enabled to add people and share files."
            )
        person = self._person(person_id)
        if person is None:
            raise FileSharingRefused("You are not an active member of this team.")
        return person

    def find_person(self, text: str, *, excluding: str) -> str:
        """A team member by ``user:<id>``, id or display name (one match only)."""

        wanted = str(text or "").strip()
        if wanted.startswith("user:"):
            wanted = wanted[len("user:"):]
        people = self._team_people()
        matches = [pid for pid, name in people if pid == wanted] or [
            pid for pid, name in people if name.strip().lower() == wanted.lower()
        ]
        if not matches:
            raise FileSharingRefused(f"Nobody in the team is called {text!r}.")
        if len(matches) > 1:
            raise FileSharingRefused(f"More than one person is called {text!r}; use user:<id>.")
        if matches[0] == excluding:
            raise FileSharingRefused("That is you; your own folder is already yours.")
        return matches[0]

    def own_item(self, person: PersonPrincipal, raw: str) -> OwnItem:
        """A path in the person's own folder, relative to it or as ``users/<folder>/…``."""

        home = (FOLDER_ROOT_USERS, person_folder_name(person.person_id))
        text = str(raw or "").strip().strip("/")
        parts = tuple(part for part in PurePosixPath(text).parts if part not in {"", "."}) if text else ()
        if parts[:2] == home:
            parts = parts[2:]
        elif parts[:1] == (FOLDER_ROOT_USERS,):
            raise FileSharingRefused("You can share and send only what is in your own folder.")
        if any(part == ".." or "\\" in part or "\x00" in part for part in parts):
            raise FileSharingRefused(f"{raw!r} is not a path inside your folder.")
        full = home + parts
        reason = sensitive_reason(PurePosixPath(*full))
        if reason:
            raise FileSharingRefused(f"{raw} looks like a credential file ({reason}) and is never shared.")
        try:
            info = safe_open.entry_status(self._workspaces, full)
        except UnsafePath as exc:
            raise FileSharingRefused(f"{raw} cannot be opened safely: {exc}") from exc
        if info is None:
            raise FileSharingRefused(f"There is no {raw} in your folder.")
        if stat.S_ISDIR(info.st_mode):
            return OwnItem(full, False, 0)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise FileSharingRefused(f"{raw} is not a plain file or folder (links are never shared).")
        return OwnItem(full, True, info.st_size)

    # -- sharing ------------------------------------------------------------------------

    def share(self, person_id: str, raw: str, recipient: str, level: str, *, hours: int | None = None) -> GrantV1:
        person = self.acting(person_id)
        item = self.own_item(person, raw)
        recipient_id = self.find_person(recipient, excluding=person.person_id)
        chosen = SHARE_LEVELS.get(str(level or "read").strip().lower())
        if chosen is None:
            raise FileSharingRefused("Share to read, or to read and change (write).")
        if hours is not None and not 1 <= hours <= MAX_SHARE_HOURS:
            raise FileSharingRefused(f"Share for between 1 and {MAX_SHARE_HOURS} hours, or until you stop it.")
        requests = self._requests()
        now = requests.service.clock()
        spec = GrantV1(
            grant_id="new",
            team_id=requests.service.team_id,
            subject=SubjectRefV1(kind=SubjectKind.USER, id=recipient_id),
            target=item.target,
            level=chosen,
            lifetime=GrantLifetime.UNTIL if hours else GrantLifetime.UNTIL_REVOKED,
            expires_at_utc=now + timedelta(hours=hours) if hours else None,
            granted_by=SubjectRefV1(kind=SubjectKind.USER, id=person.person_id),
            reason=f"Shared by {self._display_name(person.person_id)}",
            created_at_utc=now,
        )
        grant = requests.service.grant_by_person(person, spec)
        logger.info(
            "A person shared a file or folder. event=file_shared grant_id=%s by=%s with=%s target=%s level=%s",
            grant.grant_id,
            person.person_id,
            recipient_id,
            item.target.id,
            chosen.value,
        )
        words = "read" if chosen is AccessLevel.READ else "read and change"
        self._notify(
            requests,
            recipient_id,
            AccessNoticeKind.SHARED,
            f"{self._display_name(person.person_id)} shared {item.shown} with you",
            f"You, and agents working for you, can {words} it. Agents find it at {item.target.id}.",
        )
        return grant

    def shares(self, person_id: str) -> dict[str, list[GrantV1]]:
        """What the person shared, and what was shared with them (active shares only)."""

        person = self.acting(person_id)
        requests = self._requests()
        home = f"{FOLDER_ROOT_USERS}/{person_folder_name(person.person_id)}"
        mine: list[GrantV1] = []
        theirs: list[GrantV1] = []
        for grant in requests.service.store.list_team(requests.service.team_id):
            if grant.kind is not GrantKind.ACCESS or grant.status is not GrantStatus.ACTIVE:
                continue
            if grant.target.kind not in {TargetKind.FILE, TargetKind.FOLDER} or grant.subject.kind is not SubjectKind.USER:
                continue
            if not grant.target.id.startswith(f"{FOLDER_ROOT_USERS}/"):
                continue
            if grant.target.id == home or grant.target.id.startswith(home + "/"):
                mine.append(grant)
            elif grant.subject.id == person.person_id:
                theirs.append(grant)
        return {"by_me": mine, "with_me": theirs}

    def stop_sharing(self, person_id: str, grant_id: str) -> list[str]:
        person = self.acting(person_id)
        share = next((grant for grant in self.shares(person_id)["by_me"] if grant.grant_id == grant_id), None)
        if share is None:
            raise FileSharingRefused(f"{grant_id} is not something you shared.")
        return self._requests().service.revoke_by_person(person, grant_id)

    # -- sending ------------------------------------------------------------------------

    def send(self, person_id: str, raw: str, destination: str, *, move: bool = False) -> str:
        """Send a file to the team folder at once, or to a person after their (or an administrator's) approval."""

        person = self.acting(person_id)
        item = self.own_item(person, raw)
        if not item.is_file:
            raise FileSharingRefused("Send one file at a time; share a folder instead.")
        if item.size > MAX_DELIVERY_BYTES:
            raise FileSharingRefused(f"{raw} is larger than {_size_words(MAX_DELIVERY_BYTES)}.")
        target = str(destination or "").strip().strip("/")
        if target == FOLDER_ROOT_TEAM or target.startswith(f"{FOLDER_ROOT_TEAM}/"):
            return self._send_to_team(person, item, normalize_folder_id(target), move=move)
        recipient_id = self.find_person(target, excluding=person.person_id)
        request = self._ask_to_deliver(person, item, recipient_id, move=move)
        return (
            f"Asked to put {item.shown} into {self._display_name(recipient_id)}'s folder. It arrives in "
            f"received/ once they (or an administrator) approve it (request {request.request_id})."
        )

    def _send_to_team(self, person: PersonPrincipal, item: OwnItem, folder_id: str, *, move: bool) -> str:
        requests = self._requests()
        decision = requests.service.decide_folder(
            agent=None, person=person, target_id=folder_id, is_file=False, level=AccessLevel.WRITE, task_id=None
        )
        if not decision.allowed:
            raise FileSharingRefused(f"{decision.reason} {decision.next_step}".strip())
        data = self._read(item)
        name = self._write_new(tuple(folder_id.split("/")), item.parts[-1], data)
        if move:
            self._remove_original(item, _sha256(data))
        logger.info(
            "A person put a file into the team folder. event=file_sent_to_team by=%s source=%s folder=%s moved=%s",
            person.person_id,
            "/".join(item.parts),
            folder_id,
            move,
        )
        return f"{'Moved' if move else 'Copied'} {item.shown} to {folder_id}/{name}."

    def _ask_to_deliver(self, person: PersonPrincipal, item: OwnItem, recipient_id: str, *, move: bool) -> AccessRequestV1:
        requests = self._requests()
        data = self._read(item)
        sender = self._display_name(person.person_id)
        plan = {
            "sender_person_id": person.person_id,
            "recipient_person_id": recipient_id,
            "source_parts": list(item.parts),
            "sha256": _sha256(data),
            "size": len(data),
            "move": bool(move),
        }
        digest = contract_digest(plan)
        change_id = new_id("delivery")
        requests.service.store.save_plan(
            requests.service.team_id, change_id, FILE_DELIVERY_KIND, digest, plan, at=requests.service.clock()
        )
        where = f"received/{person_folder_name(person.person_id)}/ in your folder"
        rows = [
            ReviewItemV1(
                item_id="delivery",
                section="delivery",
                text=f"Put {item.parts[-1]} ({_size_words(len(data))}) from {sender}'s folder into {where}.",
            )
        ]
        if move:
            rows.append(
                ReviewItemV1(item_id="move", section="delivery", text=f"{sender}'s copy is removed after it arrives.")
            )
        proposal = ChangeProposalV1(
            kind=FILE_DELIVERY_KIND,
            change_id=change_id,
            plan_digest=digest,
            title=f"{sender} wants to put {item.parts[-1]} into your folder",
            summary=summary_line(
                f"{sender} wants to {'move' if move else 'copy'} {item.parts[-1]} ({_size_words(len(data))}) "
                "into your folder."
            ),
            effect=tuple(row.text for row in rows),
            owner_person_id=recipient_id,
            items=tuple(rows),
        )
        return requests.submit_person_change(
            requester=person, proposal=proposal, reason=f"{sender} is sending you {item.parts[-1]}."
        )

    # -- applying an approved delivery (AccessRequests.appliers) -------------------------

    def apply(
        self, request: AccessRequestV1, reviewer: PersonPrincipal, finished: AccessRequestV1, claim: DecisionClaim
    ) -> AccessRequestV1:
        """Copy the approved file into the recipient's received/ folder, then record the outcome under ``claim``."""

        assert request.change is not None
        requests = self._requests()
        stored = requests.service.store.get_plan(request.change.change_id)
        if stored is None or stored[1] != FILE_DELIVERY_KIND or stored[2] != request.change.plan_digest:
            raise HandDownRefused("The delivery's plan is missing or changed; ask to send the file again.")
        plan = stored[3]
        if contract_digest(plan) != request.change.plan_digest:
            raise HandDownRefused("The delivery's plan does not match what was reviewed; ask to send it again.")
        item = OwnItem(tuple(plan["source_parts"]), True, int(plan["size"]))
        data = self._read(item)
        if _sha256(data) != plan["sha256"]:
            raise HandDownRefused("The file changed after it was sent for approval; send it again.")
        sender, recipient = str(plan["sender_person_id"]), str(plan["recipient_person_id"])
        folder = (FOLDER_ROOT_USERS, person_folder_name(recipient), RECEIVED_DIRNAME, person_folder_name(sender))
        name = self._write_new(folder, item.parts[-1], data)
        applied = finished.model_copy(update={"applied_at_utc": requests.service.clock()})
        try:
            requests.service.store.finish_request(applied, claim_id=claim.claim_id)
        except AccessStoreConflict as exc:
            self._discard(folder + (name,))
            raise HandDownRefused(str(exc)) from exc
        if plan.get("move"):
            self._remove_original(item, plan["sha256"])
        logger.info(
            "A sent file was delivered. event=file_delivered request_id=%s from=%s to=%s approved_by=%s moved=%s",
            request.request_id,
            sender,
            recipient,
            reviewer.person_id,
            bool(plan.get("move")),
        )
        self._notify(
            requests,
            recipient,
            AccessNoticeKind.FILE_RECEIVED,
            f"{self._display_name(sender)} sent you {item.parts[-1]}",
            f"It is in {'/'.join(folder[2:])}/{name} in your folder.",
        )
        return applied

    # -- files ----------------------------------------------------------------------------

    def _read(self, item: OwnItem) -> bytes:
        try:
            return safe_open.read_bytes(self._workspaces, item.parts, max_bytes=MAX_DELIVERY_BYTES)
        except UnsafePath as exc:
            raise FileSharingRefused(f"{'/'.join(item.parts[2:])} cannot be read safely: {exc}") from exc

    def _write_new(self, folder: tuple[str, ...], name: str, data: bytes) -> str:
        """Write ``data`` as a new file in ``folder``; a taken name gets " (2)", " (3)", ..."""

        stem, suffix = PurePosixPath(name).stem, PurePosixPath(name).suffix
        for attempt in range(1, 100):
            candidate = name if attempt == 1 else f"{stem} ({attempt}){suffix}"
            try:
                with safe_open.writable_file(
                    self._workspaces, folder + (candidate,), create_directories=True, exclusive=True
                ) as writer:
                    writer.write(data)
                return candidate
            except UnsafePath as exc:
                if "Already exists" not in str(exc):
                    raise FileSharingRefused(f"The file could not be written safely: {exc}") from exc
        raise FileSharingRefused(f"Too many files called {name} already; rename it first.")

    def _discard(self, parts: tuple[str, ...]) -> None:
        try:
            safe_open.remove_file(self._workspaces, parts)
        except UnsafePath:
            logger.warning("Could not remove an undelivered copy. event=file_delivery_cleanup_failed path=%s", "/".join(parts))

    def _remove_original(self, item: OwnItem, sha256: str) -> None:
        """Remove the sender's file after a move, only while it still has the content that was sent."""

        try:
            current = safe_open.read_bytes(self._workspaces, item.parts, max_bytes=MAX_DELIVERY_BYTES)
            if _sha256(current) != sha256:
                logger.warning(
                    "The original changed after it was sent, so it was kept. event=file_move_original_kept path=%s",
                    "/".join(item.parts),
                )
                return
            safe_open.remove_file(self._workspaces, item.parts)
        except UnsafePath as exc:
            logger.warning(
                "The original could not be removed after a move. event=file_move_original_kept path=%s reason=%s",
                "/".join(item.parts),
                exc.reason,
            )

    # -- notices --------------------------------------------------------------------------

    @staticmethod
    def _notify(requests: AccessRequests, person_id: str, kind: AccessNoticeKind, title: str, summary: str) -> None:
        try:
            requests.inbox.send_notice(
                AccessNoticeV1(
                    notice_id=new_id("access_notice"),
                    team_id=requests.service.team_id,
                    kind=kind,
                    recipient_person_id=person_id,
                    title=title[:400],
                    summary=summary[:4000],
                    created_at_utc=requests.service.clock(),
                )
            )
        except Exception:  # noqa: BLE001 - the share or delivery stands; the missing notice is logged
            logger.exception("Could not send a file sharing notice. event=file_notice_failed kind=%s", kind.value)


def share_view(grant: GrantV1, display_name: Callable[[str], str]) -> dict[str, object]:
    """One share for pages and chat lists."""

    parts = grant.target.id.split("/")
    owner_folder = parts[1] if len(parts) > 1 else ""
    return {
        "share_id": grant.grant_id,
        "item": "/".join(parts[2:]) or "(whole folder)",
        "target": grant.target.id,
        "is_file": grant.target.kind is TargetKind.FILE,
        "owner_folder": owner_folder,
        "person_id": grant.subject.id,
        "person": display_name(grant.subject.id),
        "shared_by": display_name(grant.granted_by.id),
        "level": grant.level.value,
        "level_words": "read" if grant.level is AccessLevel.READ else "read and change",
        "expires_at_utc": grant.expires_at_utc.isoformat() if grant.expires_at_utc else None,
    }


def delivery_waiting(request: AccessRequestV1) -> bool:
    return (
        request.status is AccessRequestStatus.WAITING
        and request.change is not None
        and request.change.kind == FILE_DELIVERY_KIND
    )


__all__ = [
    "FILE_DELIVERY_KIND",
    "FileSharingRefused",
    "GatewayFileSharing",
    "OwnItem",
    "RECEIVED_DIRNAME",
    "delivery_waiting",
    "share_view",
]

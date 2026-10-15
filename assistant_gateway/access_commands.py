"""The gateway's side of the ``/access`` chat command (``GatewayAccessCommandPort``).

Each call acts as the signed-in person with the team roles they hold right
now (looked up again at every call, never taken from the chat), within their
own authority: the access service decides what they may give or revoke, and
``AccessRequests`` decides blocks. It uses the same helpers as ``assistant_cli
access`` (``access_control.operator_commands``) and the gateway Inbox's access
service, so there is one Cedar evaluator for people's decisions. Refusals come
back as plain text; unexpected errors are logged and reported as failures.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from pathlib import Path

from access_control.data_folders import DataFolderRefused, DataFolderRegistry
from access_control.decisions import HandDownRefused
from access_control.entities import PersonPrincipal
from access_control.operator_commands import (
    GrantRequest,
    OperatorCommandError,
    build_grant_spec,
    describe_request,
    grant_matches,
    list_grants,
    parse_subject,
    parse_target_text,
    summarize_grant,
    summary_lines,
    visible_to,
)
from access_control.requests import AccessRequests
from assistant_gateway.access_inbox import (
    GatewayAccessRequestsSource,
    access_inbox_settings,
    access_review_url,
)
from assistant_gateway.application.commands.access import AccessBlockInput, AccessGrantInput, DataFolderInput
from assistant_gateway.file_sharing import FileSharingRefused, GatewayFileSharing, share_view
from custom_messages.contracts.access_v2 import AccessRequestStatus, TargetKind

logger = logging.getLogger(__name__)

_NOT_A_MEMBER = "You are not an active member of this team, so you cannot give, remove or block access."
_REFUSALS = (HandDownRefused, OperatorCommandError, DataFolderRefused, ValueError)


class GatewayAccessCommands:
    def __init__(
        self,
        *,
        requests: Callable[[], AccessRequests],
        person: Callable[[str], PersonPrincipal | None],
        review_url: Callable[[str | None], str],
        authorization_enabled: bool,
        data_folders: Callable[[], DataFolderRegistry] | None = None,
        protected_locations: tuple[Path, ...] = (),
        file_sharing: GatewayFileSharing | None = None,
    ) -> None:
        self._requests = requests
        self._person = person
        self._review_url = review_url
        self._enabled = authorization_enabled
        self._data_folders = data_folders
        self._protected = protected_locations
        self._sharing = file_sharing

    @classmethod
    def from_config(cls, config: Any, *, roles_of: Callable[[str], tuple[str, ...]]) -> "GatewayAccessCommands":
        """Its own access service, for compositions without the gateway's Inbox.

        Settings are read and the service opened on first use, so building a
        command application does not touch storage or start Cedar.
        """

        opened: list[GatewayAccessRequestsSource] = []

        def source() -> GatewayAccessRequestsSource:
            if not opened:
                opened.append(GatewayAccessRequestsSource(access_inbox_settings(config)))
            return opened[0]

        team_id = config.manager.team_id

        def person(person_id: str) -> PersonPrincipal | None:
            roles = roles_of(person_id)
            return PersonPrincipal(person_id, team_id, roles) if roles else None

        settings = access_inbox_settings(config)
        return cls(
            requests=lambda: source().requests(),
            person=person,
            review_url=lambda request_id=None: access_review_url(settings.inbox_url, request_id),
            authorization_enabled=bool(config.authorization.enabled),
            data_folders=lambda: source().data_folders(),
            protected_locations=tuple(settings.protected_locations),
        )

    def authorization_enabled(self) -> bool:
        return self._enabled

    async def _run(self, action: str, person_id: str, call: Callable[[AccessRequests, PersonPrincipal], str]) -> str:
        def work() -> str:
            person = self._person(person_id)
            if person is None:
                return _NOT_A_MEMBER
            try:
                return call(self._requests(), person)
            except _REFUSALS as exc:
                logger.info(
                    "Access command refused. event=access_command_refused action=%s person_id=%s reason=%s",
                    action,
                    person_id,
                    exc,
                )
                return f"Not done: {exc}"

        return await asyncio.to_thread(work)

    async def list_access(self, person_id: str, *, match: str | None) -> str:
        def call(requests: AccessRequests, person: PersonPrincipal) -> str:
            grants = [
                grant
                for grant in list_grants(requests.service.store, requests.service.team_id)
                if visible_to(person, grant) and grant_matches(grant, match)
            ]
            if not grants:
                about = f" about {match}" if match else ""
                return f"No grants or blocks{about} that you can see."
            lines = summary_lines(requests, grants)
            heading = f"Grants and blocks{f' about {match}' if match else ''} ({len(lines)}):"
            return "\n".join([heading, *(f"- {line}" for line in lines)])

        return await self._run("list", person_id, call)

    async def grant(self, person_id: str, grant: AccessGrantInput) -> str:
        def call(requests: AccessRequests, person: PersonPrincipal) -> str:
            target = parse_target_text(grant.target)
            is_set = target.kind is TargetKind.TOOL_SET
            is_folder = target.kind is TargetKind.FOLDER
            spec = build_grant_spec(
                GrantRequest(
                    to=grant.to,
                    tool=None if is_set or is_folder else target.id,
                    tool_set=target.id if is_set else None,
                    folder=target.id if is_folder else None,
                    level=grant.level,
                    duration=grant.duration,
                    delegate_depth=grant.delegate_depth,
                    delegate_mode=grant.delegate_mode,
                    reason=grant.reason,
                    everyone=grant.everyone,
                ),
                team_id=requests.service.team_id,
                by=person.person_id,
                now=requests.service.clock(),
            )
            cover = requests.service.covering_grant(spec)
            if cover is not None:
                return f"Already given; nothing changed. {summarize_grant(requests, cover)}"
            created = requests.service.grant_by_person(person, spec)
            logger.info(
                "A person gave access from chat. event=access_granted_by_command grant_id=%s by=%s subject=%s target=%s level=%s",
                created.grant_id,
                person.person_id,
                created.subject.id,
                created.target.id,
                created.level.value,
            )
            return f"Done. {summarize_grant(requests, created)}"

        return await self._run("grant", person_id, call)

    async def revoke(self, person_id: str, grant_id: str) -> str:
        def call(requests: AccessRequests, person: PersonPrincipal) -> str:
            grant = requests.service.store.get(grant_id)
            # Remove takes the whole entry (access_control.grant_entries).
            entry = {held.grant_id for held in requests.service.entry_grants(grant)} if grant else {grant_id}
            revoked = requests.service.revoke_by_person(person, grant_id)
            if not revoked:
                return f"{grant_id} was already ended; nothing changed."
            parts = [item for item in revoked if item in entry]
            handed_down = [item for item in revoked if item not in entry]
            removed = f"all of this access ({', '.join(parts)})" if len(parts) > 1 else ", ".join(parts)
            extra = f", and {len(handed_down)} handed down from it ({', '.join(handed_down)})" if handed_down else ""
            return f"Removed {removed}{extra}. It applies from the next action."

        return await self._run("revoke", person_id, call)

    async def block(self, person_id: str, block: AccessBlockInput) -> str:
        def call(requests: AccessRequests, person: PersonPrincipal) -> str:
            created = requests.block(
                person,
                subject=parse_subject(block.subject, requests.service.team_id),
                target=parse_target_text(block.target),
                reason=block.reason,
                hours=block.hours,
                replace_block_id=block.replace_block_id,
            )
            action = f"Replaced block {block.replace_block_id}." if block.replace_block_id else "Blocked."
            return (
                f"{action} {summarize_grant(requests, created)} It applies from the next action; "
                f"remove it with /access unblock {created.grant_id}."
            )

        return await self._run("block", person_id, call)

    async def unblock(self, person_id: str, grant_id: str) -> str:
        def call(requests: AccessRequests, person: PersonPrincipal) -> str:
            removed = requests.unblock(person, grant_id)
            return f"Removed the block {removed.grant_id}. The access it stopped applies again from the next action."

        return await self._run("unblock", person_id, call)

    async def waiting_requests(self, person_id: str) -> str:
        def call(requests: AccessRequests, person: PersonPrincipal) -> str:
            waiting = [
                request
                for request in requests.service.store.list_requests(
                    requests.service.team_id, status=AccessRequestStatus.WAITING
                )
                if requests.may_decide(person, request)
            ]
            if not waiting:
                return "No access requests are waiting for your decision."
            lines = [f"Access requests waiting for your decision ({len(waiting)}):"]
            lines += [f"- {describe_request(requests, request)}" for request in waiting]
            lines.append(f"Decide them in the Inbox: {self._review_url(None)}")
            return "\n".join(lines)

        return await self._run("requests", person_id, call)

    # -- data folders (decision D16) -------------------------------------------------

    async def _run_folders(self, action: str, person_id: str, call: Callable[[DataFolderRegistry, PersonPrincipal], str]) -> str:
        """Like ``_run`` but without the access service, so it works with authorization off."""

        def work() -> str:
            person = self._person(person_id)
            if person is None:
                return _NOT_A_MEMBER
            if self._data_folders is None:
                return "Data folders are managed in the gateway's chat and on the Access page."
            try:
                return call(self._data_folders(), person)
            except _REFUSALS as exc:
                logger.info(
                    "Data folder command refused. event=data_folder_command_refused action=%s person_id=%s reason=%s",
                    action,
                    person_id,
                    exc,
                )
                return f"Not done: {exc}"

        return await asyncio.to_thread(work)

    async def starter_profile(self, person_id: str, name: str | None) -> str:
        """Show or choose TeamLead's standing access for this person's own work (decision D13)."""

        from access_control.starter_profiles import applied_profile, apply_starter_profile, starter_profiles

        def call(requests: AccessRequests, person: PersonPrincipal) -> str:
            profiles = starter_profiles()
            lead = requests.lead_agent_type
            if name is None:
                current = applied_profile(requests.service, person.person_id)
                lines = [f"For your work TeamLead has: {profiles[current].display_name if current else 'no starter profile'}."]
                lines += [f"- {item.name}: {item.display_name} - {item.description}" for item in profiles.values()]
                lines.append("Choose with /access profile <name>.")
                return "\n".join(lines)
            if name not in profiles:
                return f"Not done: choose one of {', '.join(profiles)}."
            apply_starter_profile(requests.service, person, name, lead_agent_type=lead)
            return f"Done. {profiles[name].display_name} for your work: {profiles[name].description}"

        return await self._run("profile", person_id, call)

    # -- sharing files between people (decision D15) --------------------------------

    async def _run_sharing(self, action: str, person_id: str, call: Callable[[GatewayFileSharing], str]) -> str:
        def work() -> str:
            if self._sharing is None:
                return "Sharing files is available in the gateway's chat and workspace browser."
            try:
                return call(self._sharing)
            except (*_REFUSALS, FileSharingRefused) as exc:
                logger.info(
                    "File sharing command refused. event=file_sharing_refused action=%s person_id=%s reason=%s",
                    action,
                    person_id,
                    exc,
                )
                return f"Not done: {exc}"

        return await asyncio.to_thread(work)

    async def share(self, person_id: str, item: str, recipient: str, level: str, hours: int | None) -> str:
        def call(sharing: GatewayFileSharing) -> str:
            grant = sharing.share(person_id, item, recipient, level, hours=hours)
            view = share_view(grant, sharing.display_name)
            until = f" until {view['expires_at_utc']}" if view["expires_at_utc"] else ""
            return (
                f"Shared {view['item']} with {view['person']} ({view['level_words']}){until}. "
                f"Stop with /unshare {grant.grant_id}."
            )

        return await self._run_sharing("share", person_id, call)

    async def unshare(self, person_id: str, share_id: str) -> str:
        def call(sharing: GatewayFileSharing) -> str:
            sharing.stop_sharing(person_id, share_id)
            return f"Stopped sharing ({share_id}). It applies from the next action."

        return await self._run_sharing("unshare", person_id, call)

    async def list_shares(self, person_id: str) -> str:
        def call(sharing: GatewayFileSharing) -> str:
            shares = sharing.shares(person_id)
            lines: list[str] = []
            for grant in shares["by_me"]:
                view = share_view(grant, sharing.display_name)
                lines.append(f"- You share {view['item']} with {view['person']} ({view['level_words']}) - {grant.grant_id}")
            for grant in shares["with_me"]:
                view = share_view(grant, sharing.display_name)
                lines.append(f"- {view['shared_by']} shares {view['target']} with you ({view['level_words']})")
            return "\n".join(["Shared files and folders:", *lines]) if lines else "Nothing is shared by you or with you."

        return await self._run_sharing("shares", person_id, call)

    async def send_file(self, person_id: str, item: str, destination: str, move: bool) -> str:
        return await self._run_sharing(
            "send", person_id, lambda sharing: sharing.send(person_id, item, destination, move=move)
        )

    async def list_data_folders(self, person_id: str) -> str:
        def call(registry: DataFolderRegistry, _person: PersonPrincipal) -> str:
            records = registry.records()
            if not records:
                return (
                    "No data folders are registered. An owner or administrator adds one with "
                    "/access folders add <name> <absolute path>."
                )
            lines = [f"Data folders ({len(records)}):"]
            lines += [
                f"- data/{record.name}: {record.path} ({'read and change' if record.writable else 'read only'})"
                for record in records
            ]
            return "\n".join(lines)

        return await self._run_folders("list", person_id, call)

    async def add_data_folder(self, person_id: str, folder: DataFolderInput) -> str:
        def call(registry: DataFolderRegistry, person: PersonPrincipal) -> str:
            record = registry.register(
                person,
                name=folder.name,
                path=folder.path,
                writable=folder.writable,
                reason=folder.reason,
                protected=self._protected,
                now=datetime.now(timezone.utc),
            )
            return (
                f"Registered data/{record.name} ({record.path}, "
                f"{'read and change' if record.writable else 'read only'}). Agents reach it only with a folder "
                f"grant, for example /access grant agent-type:<Type> data/{record.name} read."
            )

        return await self._run_folders("add", person_id, call)

    async def remove_data_folder(self, person_id: str, name: str) -> str:
        def call(registry: DataFolderRegistry, person: PersonPrincipal) -> str:
            removed = registry.remove(person, name, now=datetime.now(timezone.utc))
            return (
                f"Removed data/{removed.name}. Agents can no longer reach {removed.path}; grants that named it "
                "stay but name nothing until a folder is registered under that name again."
            )

        return await self._run_folders("remove", person_id, call)


__all__ = ["GatewayAccessCommands"]

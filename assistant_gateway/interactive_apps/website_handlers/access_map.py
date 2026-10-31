"""Authenticated HTTP routes for the Access page (the access map).

Reads need the signed-in browser session; changes also need the write checks
every permission action uses (same origin, fetch metadata and the CSRF
header). The person is always the session's user; the browser chooses only
the view, the page offset and what to block. Removing access uses the access
Inbox's revoke route, the same one its notices use. Refusals from the access
service are written for people, so their text is shown as is.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from access_control.data_folders import DataFolderRefused
from access_control.decisions import HandDownRefused
from assistant_gateway.application.access_map_ports import (
    ACCESS_MAP_VIEW_AGENTS,
    ACCESS_MAP_VIEWS,
    AccessBlockInput,
)
from assistant_gateway.file_sharing import share_view
from assistant_gateway.interactive_apps.website_handlers.access_inbox import SignedInAccessHandlers

# The access service's refusals, and invalid choices in the forms (a refused
# share, send or data folder is a ValueError subclass with a plain message).
REFUSALS = (HandDownRefused, DataFolderRefused, ValueError)


def _offset(request: web.Request) -> int:
    text = str(request.query.get("offset") or "0").strip()
    if not text.isdigit():
        raise web.HTTPBadRequest(text="The page offset must be a whole number.")
    return int(text)


def _hours(value: Any) -> int | None:
    """How long a block lasts, in hours; None until someone removes it."""

    if value is None or value == "":
        return None
    if isinstance(value, bool) or not str(value).strip().isdigit():
        raise ValueError("Choose how long the block lasts.")
    return int(str(value).strip())


class WebsiteAccessMapHandlers(SignedInAccessHandlers):
    refusal_event = "web_access_map_refused"

    @property
    def _map(self) -> Any:
        return self.application.access_inbox.access_map

    async def view(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=False)
        view = str(request.query.get("view") or ACCESS_MAP_VIEW_AGENTS).strip()
        if view not in ACCESS_MAP_VIEWS:
            raise web.HTTPBadRequest(text=f"Choose one of the views: {', '.join(ACCESS_MAP_VIEWS)}.")
        payload = await self._map.view(
            person_id,
            view=view,
            offset=_offset(request),
            agent=str(request.query.get("agent") or "").strip(),
        )
        return self.operations._permission_json_response({"status": "ok", **payload})

    async def tool_operations(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=False)
        tool_id = str(request.match_info.get("tool_id") or "").strip()
        try:
            payload = await self._map.tool_operations(person_id, tool_id, offset=_offset(request))
        except REFUSALS as exc:
            return self._refused(exc, action="tool_operations", person_id=person_id)
        return self.operations._permission_json_response({"status": "ok", **payload})

    async def block(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=True)
        payload = await self.operations._json_payload(request)
        try:
            block = AccessBlockInput(
                subject_kind=str(payload.get("subject_kind") or "").strip(),
                subject=str(payload.get("subject") or ""),
                tool_id=str(payload.get("tool_id") or ""),
                operation=str(payload.get("operation") or ""),
                reason=str(payload.get("reason") or "")[:2000],
                hours=_hours(payload.get("hours")),
            )
            result = await self._map.block(person_id, block)
        except REFUSALS as exc:
            return self._refused(exc, action="block", person_id=person_id)
        return self.operations._permission_json_response({"status": "ok", **result})

    async def unblock(self, request: web.Request) -> web.Response:
        person_id = self._person(request, write=True)
        block_id = str(request.match_info.get("block_id") or "").strip()
        try:
            result = await self._map.unblock(person_id, block_id)
        except REFUSALS as exc:
            return self._refused(exc, action="unblock", person_id=person_id)
        return self.operations._permission_json_response({"status": "ok", **result})

    # -- files: shares, sending, data folders (decisions D15 and D16) -------------------

    @property
    def _sharing(self) -> Any:
        return self.application.access_inbox.file_sharing

    async def _change(self, request: web.Request, action: str, call: Any) -> web.Response:
        person_id = self._person(request, write=True)
        payload = await self.operations._json_payload(request)
        try:
            result = await call(person_id, payload)
        except REFUSALS as exc:
            return self._refused(exc, action=action, person_id=person_id)
        return self.operations._permission_json_response({"status": "ok", **result})

    async def set_starter_profile(self, request: web.Request) -> web.Response:
        """TeamLead's standing access for the signed-in person's own work."""

        async def call(person_id: str, payload: dict) -> dict:
            return await self._map.set_starter_profile(person_id, str(payload.get("profile") or ""))

        return await self._change(request, "starter_profile", call)

    async def set_history_cleanup(self, request: web.Request) -> web.Response:
        """Whether TeamLead may clean up the signed-in person's own history without asking each time."""

        async def call(person_id: str, payload: dict) -> dict:
            on = payload.get("on")
            if not isinstance(on, bool):
                raise ValueError("Send on as true or false.")
            return await self._map.set_history_cleanup(person_id, on)

        return await self._change(request, "history_cleanup", call)

    async def stop_sharing(self, request: web.Request) -> web.Response:
        share_id = str(request.match_info.get("share_id") or "").strip()
        return await self._change(request, "stop_sharing", lambda person_id, _payload: self._map.stop_sharing(person_id, share_id))

    async def add_data_folder(self, request: web.Request) -> web.Response:
        async def call(person_id: str, payload: dict) -> dict:
            return await self._map.add_data_folder(
                person_id,
                str(payload.get("name") or "").strip(),
                str(payload.get("path") or "").strip(),
                writable=payload.get("writable") is True,
            )

        return await self._change(request, "add_data_folder", call)

    async def remove_data_folder(self, request: web.Request) -> web.Response:
        name = str(request.match_info.get("name") or "").strip()
        return await self._change(request, "remove_data_folder", lambda person_id, _payload: self._map.remove_data_folder(person_id, name))

    async def team_people(self, request: web.Request) -> web.Response:
        """Team members to share or send to (names only; never their folders)."""

        person_id = self._person(request, write=False)
        people = await asyncio.to_thread(self._sharing.team_people)
        return self.operations._permission_json_response(
            {"status": "ok", "people": [{"person_id": pid, "name": name} for pid, name in people if pid != person_id]}
        )

    async def share(self, request: web.Request) -> web.Response:
        async def call(person_id: str, payload: dict) -> dict:
            hours = payload.get("hours")
            grant = await asyncio.to_thread(
                self._sharing.share,
                person_id,
                str(payload.get("path") or ""),
                str(payload.get("person") or ""),
                str(payload.get("level") or "read"),
                hours=int(hours) if isinstance(hours, int) and not isinstance(hours, bool) else None,
            )
            view = share_view(grant, self._sharing.display_name)
            return {"share": view, "message": f"Shared {view['item']} with {view['person']} ({view['level_words']})."}

        return await self._change(request, "share", call)

    async def send(self, request: web.Request) -> web.Response:
        async def call(person_id: str, payload: dict) -> dict:
            message = await asyncio.to_thread(
                self._sharing.send,
                person_id,
                str(payload.get("path") or ""),
                str(payload.get("to") or ""),
                move=payload.get("move") is True,
            )
            return {"message": message}

        return await self._change(request, "send", call)


__all__ = ["WebsiteAccessMapHandlers"]

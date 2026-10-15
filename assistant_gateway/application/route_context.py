"""The signed-in route that gateway operations run for, and who it belongs to.

Web chat routes, chat commands and Settings views all name the gateway
session and the channel identity they came from (``GatewayRouteContextV1``).
Transport adapters build this contract instead of forwarding keyword
dictionaries; the website fills ``canonical_user_id`` from the signed-in
browser session, chat commands from the linked channel account.

``SignedInPerson`` turns a route into the person it acts for: the session must
exist and belong to the person the route resolves to. Controls that change
something for a person (MCP controls, installed procedures) use it; what that
person may change is decided afterwards from their current team roles
(``assistant_gateway.access_setup.PersonChangeCheck``), never from the route.
"""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from assistant_gateway.application.request_context import GatewayRequestRoute


class GatewayRouteContractV1(BaseModel):
    """Strict immutable base for gateway route-scoped application DTOs."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    schema_version: Literal[1] = 1


class GatewayRouteContextV1(GatewayRouteContractV1):
    """Authenticated gateway route used for one operation."""

    session_key: str = Field(min_length=1, max_length=240)
    channel_name: str | None = Field(default=None, max_length=120)
    user_id: str | None = Field(default=None, max_length=240)
    chat_id: str | None = Field(default=None, max_length=240)
    canonical_user_id: str | None = Field(default=None, max_length=240)


class GatewayRouteValues(TypedDict):
    session_key: str
    channel_name: str | None
    user_id: str | None
    chat_id: str | None
    canonical_user_id: str | None


def route_values(context: GatewayRouteContextV1) -> GatewayRouteValues:
    """The route as keyword values for services that take them separately."""

    return {
        "session_key": context.session_key,
        "channel_name": context.channel_name,
        "user_id": context.user_id,
        "chat_id": context.chat_id,
        "canonical_user_id": context.canonical_user_id,
    }


class _SessionOwnerLookup(Protocol):
    def get_session(self, session_key: str) -> object | None: ...


class _CanonicalUserResolver(Protocol):
    def canonical_user_id(self, route: GatewayRequestRoute) -> str | None: ...


class SignedInPerson:
    """The person a route acts for, when its session is theirs."""

    def __init__(self, *, sessions: _SessionOwnerLookup, request_context: _CanonicalUserResolver) -> None:
        self._sessions = sessions
        self._request_context = request_context

    def person_id(self, context: GatewayRouteContextV1) -> str:
        """The signed-in person; PermissionError when the route has none or the session is someone else's."""

        session = self._sessions.get_session(str(context.session_key or "").strip())
        if session is None:
            raise PermissionError("This action requires an existing gateway session.")
        resolved = self._request_context.canonical_user_id(
            GatewayRequestRoute(
                channel_name=context.channel_name,
                user_id=context.user_id,
                chat_id=context.chat_id,
                canonical_user_id=context.canonical_user_id,
            )
        )
        if not resolved:
            raise PermissionError("This action requires a signed-in person.")
        if str(getattr(session, "owner_canonical_user_id", "") or "").strip() != resolved:
            raise PermissionError("This session belongs to another person.")
        return resolved

    def person_or_none(self, context: GatewayRouteContextV1) -> str | None:
        """The signed-in person, or None (for views that only hide controls)."""

        try:
            return self.person_id(context)
        except PermissionError:
            return None


__all__ = [
    "GatewayRouteContextV1",
    "GatewayRouteContractV1",
    "GatewayRouteValues",
    "SignedInPerson",
    "route_values",
]

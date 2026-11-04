"""The team services in process, for gateway tests that run people's commands without NATS.

``InProcessTeamServices`` implements the gateway's ``GatewayTeamServices`` port with the
real handlers (``team_services.process.team_operations``) on the test's database. It
checks each body and reply against the contract's models and makes the frame's retry
key as the registry says, but it does not sign or verify frames: the live tests
(``tests/test_live_team_memory.py``) cover that path over NATS. It stands in only for
what a handler reads from a verified frame: the kind, body, retry key, body digest, the
command token's person and binding, and the sender's roles.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable

from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.wire.encoding import body_digest
from custom_messages.wire.frame import body_model, idempotency_key
from custom_messages.wire.kinds import KINDS
from custom_transport.protocol1.ids import new_message_id
from team_services.process import team_operations


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InProcessTeamServices:
    def __init__(self, config: GatewayInstanceConfig, *, clock: Callable[[], datetime] = _now,
                 sender_roles: frozenset[str] = frozenset({"gateway"})) -> None:
        self.team = config.manager.team_id
        self.handlers: dict[str, Any] = {}
        for family in team_operations(config, clock):
            self.handlers.update(family)
        self.sender_roles = sender_roles
        self.sent: list[tuple[str, dict[str, Any], str]] = []
        self.replies: list[dict[str, Any]] = []

    async def command(self, kind: str, body: dict[str, Any], *, person_ref: str, request_id: str) -> dict[str, Any]:
        verified = stand_in_frame(self.team, kind, body, person_ref=person_ref, request_id=request_id,
                                  sender_roles=self.sender_roles)
        self.sent.append((kind, body, person_ref))
        reply = checked_reply(kind, await self.handlers[kind](verified))
        self.replies.append(reply)
        return reply


def stand_in_frame(team: str, kind: str, body: dict[str, Any], *, person_ref: str | None, request_id: str,
                   sender_roles: frozenset[str] = frozenset({"gateway"}), message_id: str | None = None,
                   binding: str = "command") -> SimpleNamespace:
    """What a handler reads from a verified frame (module docstring); the body is checked first."""

    body_model(kind)[0].validate_python(body)
    raw = {"message_id": message_id or new_message_id(), "body": body,
           "audience": {"target": "team_services", "id": team, "team": team}}
    parts = [_pointed(raw, pointer) for pointer in KINDS[kind].idempotency]
    raw.update(idempotency_key=idempotency_key(kind, *parts), body_digest=body_digest(body))
    return SimpleNamespace(
        frame=SimpleNamespace(kind=kind, issuer=SimpleNamespace(runtime="gateway", team=team)),
        raw=raw,
        certificate=SimpleNamespace(roles=sender_roles),
        authority=SimpleNamespace(token=SimpleNamespace(
            person=person_ref, bound_to=SimpleNamespace(type=binding, id=request_id))),
    )


def relayed_frame(team: str, kind: str, body: dict[str, Any], *, person_ref: str,
                  request_id: str = "cmd-relayed") -> SimpleNamespace:
    """A person's command as the team services relay it to an agent's host: sent by the team
    services, carrying the gateway's command token (custom_agents.relayed_task_controls)."""

    frame = stand_in_frame(team, kind, {"request_id": request_id, **body}, person_ref=person_ref,
                           request_id=request_id, sender_roles=frozenset({"team_services", "scheduler"}))
    frame.authority.certificate = SimpleNamespace(roles=frozenset({"gateway"}))
    return frame


def checked_reply(kind: str, reply: dict[str, Any]) -> dict[str, Any]:
    """The reply, after checking it against the contract's reply model."""

    body_model(f"{kind}.reply")[0].validate_python(reply)
    return reply


def _pointed(document: dict[str, Any], pointer: str) -> Any:
    value: Any = document
    for step in pointer[1:].split("/"):
        if not isinstance(value, dict) or step not in value:
            return None
        value = value[step]
    return value

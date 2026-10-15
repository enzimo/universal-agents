"""The chat commands for sharing files between people (/share, /unshare, /shares, /send).

They run through the same handler as /access, as the signed-in person; the
gateway's sharing service decides (tests/test_file_sharing.py covers it with
real Cedar). These tests check how the words are read and which operation
runs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from assistant_gateway.application.commands.access import (
    FILE_SHARING_HELP_TEXT,
    AccessCommandHandler,
    AccessCommandRequest,
    AccessCommandUsageError,
    is_access_command,
    parse_send,
    parse_share,
)
from assistant_gateway.application.commands.context import GatewayActorContext


@dataclass
class RecordingPort:
    calls: list[tuple] = field(default_factory=list)

    def authorization_enabled(self) -> bool:
        return True

    async def share(self, person_id, item, recipient, level, hours):
        self.calls.append(("share", person_id, item, recipient, level, hours))
        return "shared"

    async def unshare(self, person_id, share_id):
        self.calls.append(("unshare", person_id, share_id))
        return "stopped"

    async def list_shares(self, person_id):
        self.calls.append(("shares", person_id))
        return "listed"

    async def send_file(self, person_id, item, destination, move):
        self.calls.append(("send", person_id, item, destination, move))
        return "sent"


class KnownPerson:
    def canonical_user_id_for_context(self, **_kwargs):
        return "alice"


def run(port: RecordingPort, command: str) -> str:
    handler = AccessCommandHandler(identity=KnownPerson(), access=port)
    actor = GatewayActorContext(channel_name="web", user_id="u", chat_id="c", thread_id=None, canonical_user_id="alice")
    return asyncio.run(handler.execute(AccessCommandRequest(command=command, actor=actor)))


def test_the_sharing_commands_are_recognized_with_access() -> None:
    for command in ("/share reports with Bob", "/unshare g1", "/shares", "/send a.md to Bob", "/access list"):
        assert is_access_command(command)
    assert not is_access_command("/sharex") and not is_access_command("/sender x")


def test_share_and_send_words_are_read_into_their_parts() -> None:
    assert parse_share(["reports", "with", "Bob", "Smith", "write", "--for", "7d"]) == ("reports", "Bob Smith", "write", 168)
    assert parse_share(["notes/plan.md", "with", "bob"]) == ("notes/plan.md", "bob", "read", None)
    assert parse_send(["a.md", "to", "team/handoff", "--move"]) == ("a.md", "team/handoff", True)
    with pytest.raises(AccessCommandUsageError):
        parse_share(["reports", "Bob"])
    with pytest.raises(AccessCommandUsageError):
        parse_send(["a.md", "Bob"])


def test_each_command_runs_its_operation_as_the_signed_in_person() -> None:
    port = RecordingPort()
    assert run(port, "/share reports with Bob write --for 12h") == "shared"
    assert run(port, "/unshare grant_1") == "stopped"
    assert run(port, "/shares") == "listed"
    assert run(port, '/send "my notes.md" to Bob --move') == "sent"
    assert port.calls == [
        ("share", "alice", "reports", "Bob", "write", 12),
        ("unshare", "alice", "grant_1"),
        ("shares", "alice"),
        ("send", "alice", "my notes.md", "Bob", True),
    ]
    assert run(port, "/share") == FILE_SHARING_HELP_TEXT
    assert run(port, "/send a.md Bob").startswith("Usage: /send")

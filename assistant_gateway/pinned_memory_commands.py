"""Infrastructure adapter for the manager's pinned-memory command grammar."""

from __future__ import annotations

from dataclasses import asdict

from custom_agents.pinned_memory_runtime import parse_pinned_memory_command
from custom_messages.contracts.control_v1 import PinnedMemoryCommandPayload


class RuntimePinnedMemoryCommandParser:
    """Adapt the existing manager grammar to the gateway application port."""

    def parse(self, command: str, *, bank: str) -> PinnedMemoryCommandPayload:
        return PinnedMemoryCommandPayload(
            **asdict(parse_pinned_memory_command(command, bank=bank))
        )

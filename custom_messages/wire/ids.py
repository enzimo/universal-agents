"""Whether a team id, an agent type or an agent id fits protocol 1, and what is wrong with it in words.

Protocol 1 carries these ids in NATS subjects, consumer names, record keys and host
credentials, so the contract caps them: team ids and agent types at 24 characters, agent
ids at 32 (plan decisions 56 and 98), each one subject token of letters, digits, ``-`` and
``_`` starting with a letter or digit (``common.schema.json``; ``models.TeamId``,
``AgentType``, ``AgentId``). The checks validate with the generated types, so they follow
the contract; ``cap`` only words the length in a message, and a test holds it to the
contract's pattern.

Used by the launcher's id check at the first start of 0.10
(``instance_upgrades.protocol1_ids``) and by onboarding for the team id it writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import TypeAdapter, ValidationError

from custom_messages.wire.models import AgentId, AgentType, TeamId

_ALLOWED_CHARACTERS: Final = "letters, digits, '-' and '_', starting with a letter or digit"


@dataclass(frozen=True)
class IdRule:
    """One kind of protocol 1 id: what it is called, its length cap and the contract's type."""

    what: str
    cap: int
    adapter: TypeAdapter[str]

    def fits(self, value: str) -> bool:
        try:
            self.adapter.validate_python(value)
        except ValidationError:
            return False
        return True

    def problem(self, value: str) -> str | None:
        """None when ``value`` fits; otherwise what is wrong, to follow the id in a sentence."""

        if self.fits(value):
            return None
        if len(value) > self.cap:
            return f"is {len(value)} characters long; protocol 1 allows at most {self.cap}"
        return f"has characters protocol 1 does not allow; it allows {_ALLOWED_CHARACTERS}"


TEAM_ID: Final = IdRule("team id", 24, TypeAdapter(TeamId))
AGENT_TYPE: Final = IdRule("agent type", 24, TypeAdapter(AgentType))
AGENT_ID: Final = IdRule("agent id", 32, TypeAdapter(AgentId))

__all__ = ["AGENT_ID", "AGENT_TYPE", "TEAM_ID", "IdRule"]

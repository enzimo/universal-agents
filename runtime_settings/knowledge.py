"""Operator settings for the shared knowledge base (``knowledge_base``).

The knowledge base stores pages, revisions, notes and pending contributions in
the manager's database (``storage.knowledge_repository``). These settings bound
what agents may write so that no agent dominates the knowledge base or rewrites
pages in a loop, and add team-specific patterns to the content that is never
stored. Defaults come from the 2026-10 design review (``ua-architecture``
``docs/memory_and_context_architecture.md``, "Knowledge base").
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KnowledgeWriteLimits(BaseModel):
    """Per-agent write limits, checked inside the store's write transaction.

    The store counts recorded writes, so the limits hold across processes and
    restarts. People editing in the gateway are not rate limited; size limits
    apply to everyone.
    """

    model_config = ConfigDict(extra="forbid")

    window_minutes: int = Field(default=30, ge=1, le=1440)
    # Edits one agent may make to the same page within the window.
    page_edits_per_agent: int = Field(default=5, ge=1, le=1000)
    # All knowledge writes (pages, notes, contributions, archives) one agent may make per hour.
    writes_per_agent_per_hour: int = Field(default=30, ge=1, le=10000)
    # New pages (direct or proposed) one person's request may create.
    new_pages_per_work: int = Field(default=10, ge=1, le=1000)
    # Applied or proposed archives one agent may make per hour.
    archives_per_agent_per_hour: int = Field(default=5, ge=1, le=1000)
    # An edit equal to the current text or one of this many earlier revisions is refused.
    recent_revisions_compared: int = Field(default=5, ge=1, le=100)
    # More than this many edits within the window from at least two agents freezes the page.
    edit_war_edits: int = Field(default=8, ge=2, le=1000)
    # How long a frozen page refuses agent edits (agents add notes instead).
    edit_war_freeze_minutes: int = Field(default=60, ge=1, le=10080)
    max_title_chars: int = Field(default=120, ge=10, le=1000)
    max_summary_chars: int = Field(default=300, ge=20, le=4000)
    max_body_chars: int = Field(default=24000, ge=1000, le=500000)
    max_note_chars: int = Field(default=2000, ge=100, le=50000)
    max_tags: int = Field(default=10, ge=0, le=100)


class KnowledgeBlockedPattern(BaseModel):
    """A team-specific pattern that is never stored, such as an internal code name."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    pattern: str = Field(min_length=1, max_length=500)

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"agent.knowledge.sensitive_content.blocked_patterns: {exc}") from exc
        return value


class KnowledgeSensitiveContentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blocked_patterns: list[KnowledgeBlockedPattern] = Field(default_factory=list)


class KnowledgeCurationSettings(BaseModel):
    """KnowledgeCuratorAgent's schedules, installed by the gateway.

    One run per active person (their pending team contributions and their own
    pages) and one team run (notes, problems, activity). A run with nothing to
    review is skipped without starting the curator. An interval of 0 installs
    no schedule of that kind; changed intervals apply to new installs only, as
    for every maintenance schedule.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    person_interval_minutes: int = Field(default=1440, ge=0, le=10080)
    team_interval_minutes: int = Field(default=1440, ge=0, le=10080)


class KnowledgeSearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_results: int = Field(default=8, ge=1, le=50)


class KnowledgeSettings(BaseModel):
    """``agent.knowledge`` in ``config.yaml`` and ``instance_config.yaml``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    limits: KnowledgeWriteLimits = Field(default_factory=KnowledgeWriteLimits)
    sensitive_content: KnowledgeSensitiveContentSettings = Field(
        default_factory=KnowledgeSensitiveContentSettings
    )
    search: KnowledgeSearchSettings = Field(default_factory=KnowledgeSearchSettings)
    curation: KnowledgeCurationSettings = Field(default_factory=KnowledgeCurationSettings)


def knowledge_settings(raw: object) -> KnowledgeSettings:
    """Validated settings from an agent's ``knowledge_config`` dict (or a settings object)."""

    if isinstance(raw, KnowledgeSettings):
        return raw
    return KnowledgeSettings.model_validate(dict(raw or {}) if isinstance(raw, dict) else {})

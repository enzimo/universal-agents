"""Protected TeamArchitect profile (SOUL.md) replacement.

The profile changes only through an approved runtime settings plan
(``team_lead.soul``, applied by ``RuntimeSettingsService`` through
``apply_change``); the approval is checked there, so this service only
validates the content and replaces the file atomically.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from typing import Any

from custom_agents.runtime_paths import agent_layout
from custom_agents.runtime_setting_registry import MAX_SOUL_BYTES

logger = logging.getLogger(__name__)


class AgentProfileService:
    """Atomically replace only the team lead's SOUL.md."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def update_team_lead_soul(self, content: str) -> dict[str, Any]:
        if not bool(getattr(self.agent, "is_team_lead", False)):
            raise PermissionError("only TeamArchitect may update the TeamArchitect SOUL profile")
        rendered = str(content or "")
        if not rendered.strip():
            raise ValueError("SOUL.md content cannot be empty")
        encoded = rendered.encode("utf-8")
        if len(encoded) > MAX_SOUL_BYTES:
            raise ValueError(f"SOUL.md cannot exceed {MAX_SOUL_BYTES} UTF-8 bytes")

        target = agent_layout(self.agent).soul_path
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = target.read_bytes() if target.exists() else b""
        previous_sha256 = hashlib.sha256(previous).hexdigest()
        content_sha256 = hashlib.sha256(encoded).hexdigest()
        temporary = target.with_name(f".SOUL.md.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        agent_logger = getattr(self.agent, "_logger", None) or logger
        agent_logger.info(
            "Updated protected TeamArchitect profile. event=agent_profile_updated team_id=%s agent_id=%s "
            "previous_sha256=%s sha256=%s bytes_written=%s",
            getattr(self.agent, "team_id", None),
            getattr(self.agent, "id", None),
            previous_sha256,
            content_sha256,
            len(encoded),
        )
        return {
            "status": "updated",
            "path": str(target),
            "bytes_written": len(encoded),
            "previous_sha256": previous_sha256,
            "sha256": content_sha256,
        }

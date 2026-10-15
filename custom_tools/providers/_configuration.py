"""Configuration support for the trusted Strands tool surface."""

import logging
import os
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


def _first_existing_instance_config_path(candidates: List[Any]) -> Optional[str]:
    seen: set[str] = set()
    for candidate in candidates:
        rendered = str(candidate or "").strip()
        if not rendered:
            continue
        try:
            resolved = Path(rendered).expanduser().resolve()
        except Exception:
            continue
        resolved_key = str(resolved)
        if resolved_key in seen:
            continue
        seen.add(resolved_key)
        if resolved.is_file():
            return resolved_key
    return None


def _resolve_notify_user_instance_config_path(agent: Any) -> Optional[str]:
    """Resolve the gateway instance config from live agent, env, or runtime config."""

    candidates: List[Any] = [
        getattr(agent, "instance_config_path", None),
        os.getenv("TEAM_INSTANCE_CONFIG"),
    ]
    root_candidates = [
        getattr(agent, "instance_root", None),
        os.getenv("TEAM_INSTANCE_ROOT"),
    ]

    try:
        from config_loader import get_config

        runtime_config = get_config()
        candidates.extend(
            [
                runtime_config.get("agent.instance_config_path"),
                runtime_config.get("instance_config_path"),
            ]
        )
        root_candidates.append(runtime_config.get("agent.instance_root"))
    except Exception as exc:
        logger.warning(
            "Runtime config lookup failed during notify_user path resolution; "
            "instance-root and environment discovery will continue. "
            "event=notify_user_runtime_config_lookup_failed agent_id=%s "
            "agent_name=%s error_type=%s",
            getattr(agent, "id", None),
            getattr(agent, "name", None),
            type(exc).__name__,
            exc_info=True,
        )

    for root in root_candidates:
        rendered = str(root or "").strip()
        if rendered:
            candidates.append(Path(rendered).expanduser() / "instance_config.yaml")

    return _first_existing_instance_config_path(candidates)

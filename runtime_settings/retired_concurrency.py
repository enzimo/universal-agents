"""Remove the unused per-agent concurrency setting before export or runtime validation.

Worker pools own concurrency. Earlier loaders ignored this key without removing
it from saved files, which let a later team export carry a setting import refuses.
"""

from __future__ import annotations

import logging
from typing import Any, Final

RETIRED_CONCURRENCY_SETTING: Final = ("agent", "max_concurrent_top_level_tasks")
_LOGGER = logging.getLogger(__name__)


def drop_retired_concurrency_setting(config: dict[str, Any]) -> bool:
    section, key = RETIRED_CONCURRENCY_SETTING
    agent = config.get(section)
    if not isinstance(agent, dict) or key not in agent:
        return False
    config[section] = {name: value for name, value in agent.items() if name != key}
    _LOGGER.info("Removed a setting worker pools replaced. event=config_retired_setting_removed setting=%s",
                 ".".join(RETIRED_CONCURRENCY_SETTING))
    return True


def refuse_retired_concurrency_setting(config: Any) -> None:
    section, key = RETIRED_CONCURRENCY_SETTING
    agent = config.get(section) if isinstance(config, dict) else None
    if isinstance(agent, dict) and key in agent:
        raise ValueError(f"{'.'.join(RETIRED_CONCURRENCY_SETTING)} has been removed. "
                         "Remove it from the file; worker pools control concurrency.")

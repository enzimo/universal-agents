"""The Universal Agents tab icon for pages served by side services.

The gateway, onboarding, docs and transfer-review servers serve
``favicon.ico`` as a file. The broker's sign-in pages and the activity
collector's dashboard carry it inside the page instead, so those services gain
no extra route (a new broker route would need its own access level).
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache
from pathlib import Path

LOGGER = logging.getLogger(__name__)

FAVICON_PATH = Path(__file__).resolve().parents[1] / "assistant_gateway" / "onboarding_web" / "assets" / "favicon.ico"


@lru_cache(maxsize=1)
def favicon_link_tag() -> str:
    """A ``<link rel="icon">`` holding the icon itself, or "" when the file is missing."""

    try:
        encoded = base64.b64encode(FAVICON_PATH.read_bytes()).decode("ascii")
    except OSError as exc:
        LOGGER.warning("event=page_icon_unreadable path=%s error=%s", FAVICON_PATH, exc)
        return ""
    return f'<link rel="icon" href="data:image/x-icon;base64,{encoded}" sizes="any">'


__all__ = ["FAVICON_PATH", "favicon_link_tag"]

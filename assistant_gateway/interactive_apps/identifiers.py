"""Stable identifiers shared by interactive-app configuration and discovery."""

from __future__ import annotations

import re


INTERACTIVE_APP_ENTRY_POINT_GROUP = "universal_agents.chat_adapters.v1"

BUILTIN_DISCORD_PLUGIN_ID = "builtin.discord"
BUILTIN_TELEGRAM_PLUGIN_ID = "builtin.telegram"
BUILTIN_WHATSAPP_PLUGIN_ID = "builtin.whatsapp"
BUILTIN_SIGNAL_PLUGIN_ID = "builtin.signal"
BUILTIN_PHOTON_PLUGIN_ID = "builtin.photon"
BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID = "builtin.internal_website"

BUILTIN_INTERACTIVE_APP_PLUGIN_IDS = (
    BUILTIN_DISCORD_PLUGIN_ID,
    BUILTIN_TELEGRAM_PLUGIN_ID,
    BUILTIN_WHATSAPP_PLUGIN_ID,
    BUILTIN_SIGNAL_PLUGIN_ID,
    BUILTIN_PHOTON_PLUGIN_ID,
    BUILTIN_INTERNAL_WEBSITE_PLUGIN_ID,
)

_PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")


def normalize_interactive_app_plugin_id(value: object) -> str:
    """Return a canonical plugin id or raise for an unstable identifier."""
    plugin_id = str(value or "").strip()
    if not _PLUGIN_ID_PATTERN.fullmatch(plugin_id):
        raise ValueError(
            "Interactive app plugin ids must be 1-128 lowercase ASCII letters, "
            "digits, dots, underscores, or hyphens, and must start and end with "
            "a letter or digit."
        )
    return plugin_id


def normalize_distribution_name(value: object) -> str:
    """Normalize a Python distribution name using the PyPA comparison rule."""
    distribution_name = str(value or "").strip()
    if not distribution_name:
        raise ValueError("Interactive app distribution names must not be empty.")
    normalized = re.sub(r"[-_.]+", "-", distribution_name).lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", normalized):
        raise ValueError(
            f"Invalid interactive app distribution name: {distribution_name!r}."
        )
    return normalized

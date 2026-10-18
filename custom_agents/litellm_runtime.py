"""Initialize LiteLLM with the model catalog shipped in this release."""

from __future__ import annotations

import importlib
import logging
import os
from types import ModuleType

_LOGGER = logging.getLogger(__name__)
_LOCAL_CATALOG_ENV = "LITELLM_LOCAL_MODEL_COST_MAP"


def initialize_litellm() -> ModuleType:
    """Use versioned model data before the SDK can start a download retry thread.

    The agent entry point calls this on the main thread before creating agents.
    LiteLLM's import-time download retry can race its own logging imports when
    the network is unavailable. Provider calls and explicit metadata lookups
    remain live; only the library's built-in catalog is tied to its package.
    """
    if os.environ.get(_LOCAL_CATALOG_ENV, "").lower() != "true":
        os.environ[_LOCAL_CATALOG_ENV] = "true"
        _LOGGER.info(
            "Using LiteLLM's bundled model catalog. "
            "event=litellm_bundled_catalog_selected"
        )
    return importlib.import_module("litellm")

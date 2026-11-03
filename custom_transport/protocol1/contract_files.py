"""The message contract's generated files a running host reads: its digest and its transport settings.

``contracts/messages/v1/generated`` is written by ``scripts/export_message_contract.py``
and ships with the runtime image. Reading the files keeps one source for these values.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

GENERATED = Path(__file__).resolve().parents[2] / "contracts" / "messages" / "v1" / "generated"


@lru_cache(maxsize=1)
def contract_sha256() -> str:
    """This build's contract digest (README "Versions"), which joins, renewals and host records carry."""

    return str(json.loads((GENERATED / "compatibility.json").read_text(encoding="utf-8"))["contract_sha256"])


@lru_cache(maxsize=1)
def transport() -> dict[str, Any]:
    """``transport.yaml`` as generated JSON: streams, consumers, buckets and limits."""

    return dict(json.loads((GENERATED / "transport.json").read_text(encoding="utf-8")))

"""The message contract's generated transport settings, which a running host reads.

``contracts/messages/v1/generated`` is written by ``scripts/export_message_contract.py``
and ships with the runtime image. Reading the file keeps one source for these values. The
contract's digest is not read here: the generated code names it
(``custom_messages.wire.kinds.CONTRACT_SHA256``), and every host and the authority use that.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

GENERATED = Path(__file__).resolve().parents[2] / "contracts" / "messages" / "v1" / "generated"


@lru_cache(maxsize=1)
def transport() -> dict[str, Any]:
    """``transport.yaml`` as generated JSON: streams, consumers, buckets and limits."""

    return dict(json.loads((GENERATED / "transport.json").read_text(encoding="utf-8")))

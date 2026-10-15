#!/usr/bin/env python3
"""Regenerate the adapter-host JSON Schema and checksum manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from pydantic import TypeAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from assistant_gateway.contracts.adapter_host_v1 import (  # noqa: E402
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterHostMessageV1,
)


OUTPUT_DIR = REPO_ROOT / "data" / "schemas" / "gateway_adapter" / "v1"
SCHEMA_PATH = OUTPUT_DIR / "adapter_host.schema.json"
FIXTURE_PATH = OUTPUT_DIR / "fixtures" / "conformance_cases.json"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"


def _json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    schema = TypeAdapter(AdapterHostMessageV1).json_schema(mode="validation")
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": (
                "https://universal-agents.local/schemas/gateway_adapter/v1/"
                "adapter_host.schema.json"
            ),
            "title": "Universal Agents Adapter Host Protocol V1",
        }
    )
    schema_text = _json_text(schema)
    SCHEMA_PATH.write_text(schema_text, encoding="utf-8")
    fixture_digest = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    manifest = {
        "contract": "universal-agents-gateway-adapter-host",
        "protocol_version": ADAPTER_HOST_PROTOCOL_VERSION,
        "json_schema_draft": "2020-12",
        "schema": {
            "filename": SCHEMA_PATH.name,
            "sha256": hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
        },
        "fixtures": {
            "filename": str(FIXTURE_PATH.relative_to(OUTPUT_DIR)),
            "sha256": fixture_digest,
        },
    }
    MANIFEST_PATH.write_text(_json_text(manifest), encoding="utf-8")


if __name__ == "__main__":
    main()

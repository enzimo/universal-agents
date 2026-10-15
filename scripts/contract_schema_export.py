"""Shared rendering primitives for tracked versioned contract schemas."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from pydantic import BaseModel


def canonical_json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_contract_schema_files(
    *,
    models: Sequence[type[BaseModel]],
    schema_base_id: str,
    contract: str,
    schema_version: int = 1,
) -> dict[str, str]:
    """Render deterministic JSON Schemas and their checksum manifest."""

    rendered: dict[str, str] = {}
    manifest_entries: list[dict[str, str]] = []
    for model in models:
        filename = f"{model.__name__}.schema.json"
        schema = model.model_json_schema(mode="validation")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"{schema_base_id}/{filename}"
        schema_text = canonical_json_text(schema)
        rendered[filename] = schema_text
        manifest_entries.append(
            {
                "filename": filename,
                "model": model.__name__,
                "sha256": hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
            }
        )
    rendered["manifest.json"] = canonical_json_text(
        {
            "schema_version": schema_version,
            "contract": contract,
            "json_schema_draft": "2020-12",
            "schemas": manifest_entries,
        }
    )
    return rendered

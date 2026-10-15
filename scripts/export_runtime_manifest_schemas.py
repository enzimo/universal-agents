#!/usr/bin/env python3
"""Export/check versioned service runtime-manifest JSON Schemas."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from pydantic import BaseModel


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_hosting.manifests import (  # noqa: E402
    ActivityCollectorRuntimeManifestV1,
    AdapterHostRuntimeManifestV1,
    ExecutionControllerRuntimeManifestV1,
    GatewayRuntimeManifestV1,
    SecureBrokerRuntimeManifestV1,
)
from scripts.contract_schema_export import render_contract_schema_files  # noqa: E402


DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "schemas" / "runtime-manifests" / "v1"
SCHEMA_BASE_ID = "https://universal-agents.dev/schemas/runtime-manifests/v1"
MODELS: tuple[type[BaseModel], ...] = (
    GatewayRuntimeManifestV1,
    ActivityCollectorRuntimeManifestV1,
    SecureBrokerRuntimeManifestV1,
    ExecutionControllerRuntimeManifestV1,
    AdapterHostRuntimeManifestV1,
)


def render_schema_files() -> dict[str, str]:
    return render_contract_schema_files(
        models=MODELS,
        schema_base_id=SCHEMA_BASE_ID,
        contract="universal-agents-service-runtime-manifests",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    rendered = render_schema_files()
    if args.check:
        mismatches = [
            name
            for name, expected in rendered.items()
            if not (output_dir / name).exists()
            or (output_dir / name).read_text(encoding="utf-8") != expected
        ]
        unexpected = sorted(
            path.name for path in output_dir.glob("*.json") if path.name not in rendered
        )
        if mismatches or unexpected:
            parser.error(
                "runtime manifest schemas are stale: "
                + ", ".join(sorted([*mismatches, *unexpected]))
            )
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in rendered.items():
        (output_dir / name).write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

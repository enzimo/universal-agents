#!/usr/bin/env python3
"""Regenerate language-neutral Web Chat transcript contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from assistant_gateway.application.website_application import (  # noqa: E402
    WebChatThreadStatesV1,
    WebTranscriptAttachmentV1,
    WebTranscriptEventPageV1,
    WebTranscriptHistoryPageV1,
    WebTranscriptMessageV1,
)
from scripts.contract_schema_export import (  # noqa: E402
    canonical_json_text,
    render_contract_schema_files,
)


OUTPUT_DIR = REPO_ROOT / "data" / "schemas" / "gateway" / "transcript" / "v1"
FIXTURE_PATH = OUTPUT_DIR / "fixtures" / "conformance_cases.json"
MODELS = (
    WebTranscriptAttachmentV1,
    WebTranscriptMessageV1,
    WebTranscriptHistoryPageV1,
    WebTranscriptEventPageV1,
    WebChatThreadStatesV1,
)


def render_transcript_contract_files() -> dict[str, str]:
    """Schemas and manifest for the transcript models, keyed by filename."""

    rendered = render_contract_schema_files(
        models=MODELS,
        schema_base_id=("https://universal-agents.local/schemas/gateway/transcript/v1"),
        contract="universal-agents-gateway-transcript",
    )
    manifest = json.loads(rendered.pop("manifest.json"))
    manifest["fixtures"] = {
        "filename": str(FIXTURE_PATH.relative_to(OUTPUT_DIR)),
        "sha256": hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(),
    }
    rendered["manifest.json"] = canonical_json_text(manifest)
    return rendered


def stale_transcript_contract_files() -> list[str]:
    """Tracked files that no longer match the models (or are missing)."""

    return [
        filename
        for filename, content in render_transcript_contract_files().items()
        if not (OUTPUT_DIR / filename).exists()
        or (OUTPUT_DIR / filename).read_text(encoding="utf-8") != content
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report stale files without writing; exit non-zero if any drifted.",
    )
    args = parser.parse_args()
    if args.check:
        stale = stale_transcript_contract_files()
        if stale:
            parser.error("Stale gateway transcript schemas: " + ", ".join(stale))
        return
    for filename, content in render_transcript_contract_files().items():
        (OUTPUT_DIR / filename).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()

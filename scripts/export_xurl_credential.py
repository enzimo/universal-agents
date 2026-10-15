#!/usr/bin/env python3
"""Export one operator-authorized X account to a private secure-form upload.

Run on the operator computer after xurl auth oauth2 --headless. This program
never prints credentials, overwrites a file, or changes the source token store.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import yaml


def export_credential(source: Path, app_name: str, username: str, output: Path) -> None:
    from secure_tool_broker.xurl_executor import OAuthCredentials

    store = yaml.safe_load(source.read_text())
    app = store["apps"][app_name]
    token = app["oauth2_tokens"][username]["oauth2"]
    credential = OAuthCredentials.model_validate(
        {
            "client_id": app["client_id"],
            "client_secret": app["client_secret"],
            "username": username,
            **token,
        }
    )
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(credential.model_dump(), handle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path.home() / ".xurl" / "auth.yml"
    )
    parser.add_argument("--app", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        export_credential(args.source, args.app, args.username, args.output)
    except Exception as exc:
        logging.error(
            "X credential export failed (%s); verify app/user and choose a new output file.",
            type(exc).__name__,
        )
        return 1
    logging.info(
        "Private OAuth file created. Upload it through the broker form, then remove the export."
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())

"""Compare a model endpoint's answers to the live tests, step by step, without showing the key.

Usage (inside the container or a development checkout)::

    python -m model_providers.diagnose --base-url https://bifrost.example.com \\
        --model openai/gpt-6-luna --api-mode responses --key-file /path/to/key

The key is read from ``--key-file`` (or the file named by
``MODEL_ENDPOINT_KEY_FILE``) and is never printed: every message passes
through the same redaction as the ``/auth model`` page. It runs, in order:

1. a plain chat request with no tool and no output limit (what a hand-written
   ``curl`` usually sends);
2. the test the ``/auth model`` page and endpoint switching run
   (``check_endpoint_address``), in the request API given, trying the address
   with and without ``/v1``;
3. the same test in the other request API.

Each line names the address, the HTTP status and how long the answer took,
so a failure points at the request shape that causes it (a tool, the output
limit, streaming, or the request API).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

from model_providers.connection_test import (
    CONNECTION_TEST_PROMPT,
    ConnectionTestRequest,
    _bearer,
    chat_completions_url,
    check_endpoint_address,
    failure_explanation,
    post_json,
    run_provider_call,
)
from model_providers.endpoints import EndpointKind, checked_endpoint_url
from runtime_settings.model_request_options import LITELLM_API_MODE_CHAT_COMPLETIONS, LITELLM_API_MODE_RESPONSES

KEY_FILE_ENV = "MODEL_ENDPOINT_KEY_FILE"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-mode", choices=(LITELLM_API_MODE_CHAT_COMPLETIONS, LITELLM_API_MODE_RESPONSES), default=LITELLM_API_MODE_CHAT_COMPLETIONS)
    parser.add_argument("--key-file", default=os.environ.get(KEY_FILE_ENV, ""))
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if not args.key_file:
        print(f"Give the key file with --key-file or {KEY_FILE_ENV}.", file=sys.stderr)
        return 2
    key = Path(args.key_file).read_text(encoding="utf-8").strip()
    base_url = checked_endpoint_url(args.base_url)
    request = ConnectionTestRequest(
        kind=EndpointKind.OPENAI_COMPATIBLE,
        base_url=base_url,
        key=key,
        model=args.model,
        api_mode=args.api_mode,
        timeout_seconds=args.timeout,
    )
    failed = False
    for v1 in (base_url.rstrip("/") + "/v1", base_url):
        url = chat_completions_url(v1)
        plain = run_provider_call(
            lambda url=url: post_json(
                url,
                _bearer(key),
                {"model": args.model, "messages": [{"role": "user", "content": CONNECTION_TEST_PROMPT}]},
                timeout_seconds=args.timeout,
            ),
            label="The endpoint",
            key=key,
            timeout_seconds=args.timeout,
            url=url,
        )
        _show("1. plain chat, no tool, no limit", plain.ok, plain.message, plain.url, plain.elapsed_seconds)
        if plain.ok:
            break
    other_mode = LITELLM_API_MODE_RESPONSES if args.api_mode == LITELLM_API_MODE_CHAT_COMPLETIONS else LITELLM_API_MODE_CHAT_COMPLETIONS
    for step, mode in (("2. the page's test", args.api_mode), ("3. the page's test, other request API", other_mode)):
        outcome = check_endpoint_address(replace(request, api_mode=mode))
        explanation = "" if outcome.ok else f" -> {failure_explanation(outcome)}"
        _show(f"{step} ({mode})", outcome.ok, outcome.result.message + explanation, outcome.result.url, outcome.result.elapsed_seconds)
        failed = failed or (mode == args.api_mode and not outcome.ok)
    return 1 if failed else 0


def _show(step: str, ok: bool, message: str, url: str, elapsed: float | None) -> None:
    took = f" in {elapsed:.1f}s" if elapsed is not None else ""
    print(f"{step}: {'OK' if ok else 'FAILED'} {url}{took}\n    {message}")


if __name__ == "__main__":
    raise SystemExit(main())

"""Redaction helpers for broker command output."""

from __future__ import annotations

import json
import re
from typing import Any


SECRET_KEY_MARKERS = frozenset(
    {
        "password",
        "passwords",
        "token",
        "tokens",
        "secret",
        "secrets",
        "credential",
        "credentials",
        "session",
        "sessions",
        "key",
        "keys",
        "notes",
        # Bitwarden CLI `bw unlock --response` returns the session under
        # `data.raw`; broker diagnostics must never render that value.
        "raw",
        "totp",
        "totps",
        "value",
    }
)
NON_SECRET_KEY_NAMES = frozenset(
    {
        # GitHub Projects v2 stores board status and custom fields here. These
        # are task metadata, not credential values.
        "fieldvaluebyname",
        "fieldvalues",
        # Diagnostic metadata records presence, source, length, or extraction
        # state only. These keys must remain visible in broker failure logs.
        "bwsessionenvpresent",
        "bwsessionfile",
        "bwsessionfilepresent",
        "clientsecretfieldchars",
        "clientsecretfieldpresent",
        "clientsecretpresent",
        "clientsecretsource",
        "credentialstatepersisted",
        "envbwclientsecretpresent",
        "envbwsessionpresent",
        "forcelogin",
        "forceloginclearedbwsessionenv",
        "masterpasswordchars",
        "masterpasswordhassurroundingwhitespace",
        "masterpasswordpresent",
        "sessionfile",
        "unlockrawexitcode",
        "unlockrawsessionextracted",
        "unlockrawstderrbytes",
        "unlockrawstdoutbytes",
    }
)
# Paging bookmarks are named "...token" but are not credentials: an API returns
# them so the caller can ask for the next page, and they are useless without the
# caller's own sign-in. Hiding them stops an agent from reading past the first
# page (gog's Gmail search returns 10 results and a nextPageToken by default).
# Matched on the key's words: Google APIs end with "page token" or "sync token"
# (nextPageToken, pageToken, startPageToken, studentsNextPageToken,
# nextSyncToken); X uses next_token, previous_token and pagination_token.
# Only their final "token" word is exempt; every other secret marker still
# applies so a name such as passwordPageToken cannot expose a credential.
PAGING_BOOKMARK_KEY_ENDINGS = (("page", "token"), ("sync", "token"))
PAGING_BOOKMARK_KEYS = frozenset(
    {("next", "token"), ("previous", "token"), ("pagination", "token")}
)
# Token shapes: GitHub, GitLab, Slack and broker tokens; JWTs; and model
# provider keys, which the broker stores since 2026-10 (secure_tool_broker.
# model_keys): OpenAI-style "sk-" keys (also sk-ant-, sk-or-, sk-proj-),
# Google "AIza" keys and xAI "xai-" keys.
SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:ghp|github_pat|glpat|xox[baprs]|stb|acu|aca)_[A-Za-z0-9_=-]{12,}\b|"
    r"\b[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b|"
    r"\b(?:sk|xai)-[A-Za-z0-9_-]{16,}|"
    r"\bAIza[A-Za-z0-9_-]{30,}"
)
CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
KEY_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def redact_text(value: str) -> str:
    return SECRET_VALUE_RE.sub("[REDACTED]", value)


def _key_tokens(key: str) -> list[str]:
    split_camel = CAMEL_BOUNDARY_RE.sub("_", key)
    return [token.lower() for token in KEY_TOKEN_RE.findall(split_camel)]


def _is_paging_bookmark_key(key_tokens: list[str]) -> bool:
    words = tuple(key_tokens)
    return words in PAGING_BOOKMARK_KEYS or any(
        words[-len(ending):] == ending for ending in PAGING_BOOKMARK_KEY_ENDINGS
    )


def _is_secret_key(key: Any) -> bool:
    key_text = str(key)
    normalized = re.sub(r"[^A-Za-z0-9]+", "", key_text).lower()
    if normalized in NON_SECRET_KEY_NAMES:
        return False
    key_tokens = _key_tokens(key_text)
    if _is_paging_bookmark_key(key_tokens):
        # The bookmark suffix must not override a credential-marked prefix.
        key_tokens = key_tokens[:-1]
    return any(token in SECRET_KEY_MARKERS for token in key_tokens)


def redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            if _is_secret_key(key):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_json(child)
        return redacted
    if isinstance(value, list):
        return [redact_json(child) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def parse_and_redact_stdout(stdout: str) -> tuple[Any | None, str]:
    raw = stdout.strip()
    if not raw:
        return None, ""
    try:
        return redact_json(json.loads(raw)), ""
    except json.JSONDecodeError:
        return None, redact_text(stdout)

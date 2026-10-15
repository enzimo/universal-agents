"""The one list of credential names every file check shares.

``access_control.sensitive_paths`` is used by agent file tools, the shell
tool's path arguments, attachments in both directions, the workspace browser
and data folder registration. These tests pin the list itself; each caller's
own tests check that it refuses these names.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

from access_control.sensitive_paths import (
    REASON_ENV_FILE,
    REASON_FILE_TYPE,
    REASON_PATH,
    is_sensitive_path,
    sensitive_reason,
)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        # Credential folders cover everything inside them.
        (".ssh", REASON_PATH),
        ("notes/.ssh/config", REASON_PATH),
        (".aws/credentials", REASON_PATH),
        (".gnupg/pubring.kbx", REASON_PATH),
        (".gog/token.json", REASON_PATH),
        ("broker_credentials/gh/hosts.yml", REASON_PATH),
        ("secrets/api-token", REASON_PATH),
        ("home/.config/gh/hosts.yml", REASON_PATH),
        # Credential files, including SSH private keys by their usual names.
        (".envrc", REASON_PATH),
        (".netrc", REASON_PATH),
        (".npmrc", REASON_PATH),
        ("id_rsa", REASON_PATH),
        ("keys/id_dsa", REASON_PATH),
        ("id_ecdsa", REASON_PATH),
        ("backup/id_ed25519", REASON_PATH),
        ("ID_RSA", REASON_PATH),
        # Private key and certificate-bundle file types.
        ("server.pem", REASON_FILE_TYPE),
        ("tls.key", REASON_FILE_TYPE),
        ("client.p12", REASON_FILE_TYPE),
        ("client.PFX", REASON_FILE_TYPE),
        # Env files: .env, anything ending in .env, and .env.<variant>.
        (".env", REASON_ENV_FILE),
        ("team.env", REASON_ENV_FILE),
        ("copies/debug.env", REASON_ENV_FILE),
        ("PROD.ENV", REASON_ENV_FILE),
        (".env.local", REASON_ENV_FILE),
        ("config/.env.d/extra", REASON_ENV_FILE),
        # Absolute paths are judged the same way.
        ("/home/person/.ssh/id_ed25519", REASON_PATH),
    ],
)
def test_credential_names_are_sensitive(path: str, reason: str) -> None:
    assert sensitive_reason(PurePosixPath(path)) == reason
    assert is_sensitive_path(PurePosixPath(path))


@pytest.mark.parametrize(
    "path",
    [
        "",
        "README.md",
        "notes/secrets.md",
        "my_secrets_notes.txt",
        # A public key is meant to be shared.
        "id_rsa.pub",
        "environment.yml",
        "src/env/settings.py",
        # Tooling folders are not credentials (the workspace browser hides
        # them for its own reasons).
        ".venv/lib/site.py",
        ".git/config",
        # Only the exact .config/gh folder is the GitHub CLI login.
        ".config/ghostty/config",
        "public.crt",
    ],
)
def test_ordinary_names_are_not_sensitive(path: str) -> None:
    assert sensitive_reason(PurePosixPath(path)) is None
    assert not is_sensitive_path(PurePosixPath(path))

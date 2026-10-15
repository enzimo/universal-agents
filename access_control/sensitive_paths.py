"""Credential file and folder names that every file check refuses.

Credentials and key files sometimes end up in a person's folder or a shared
folder (a copied ``.env``, an SSH key, a ``team.env`` backup). These checks
refuse them by name, as a second line of defence behind the folder rules, and
all of them use this one list so they cannot drift apart:

- agent file tools (``custom_tools.path_policy``), which also judge the path
  arguments of shell commands (``custom_tools.non_interactive_shell`` treats a
  bare credential file name such as ``.env`` or ``id_rsa`` as a path so these
  rules see it, but not ordinary words such as ``process.env``);
- files an agent attaches to a message for a person
  (``assistant_gateway.attachments``);
- files a person uploads in a chat (``assistant_gateway.inbound_attachments``);
- the gateway's workspace file browser
  (``assistant_gateway.application.workspace_browser``);
- data folder registration (``access_control.data_folders``).

Every rule looks at each part of a path, so a credential folder such as
``.ssh`` also covers everything inside it. Matching ignores case.

Only credentials belong here. Folders that are hidden for other reasons (for
example ``.git`` or ``node_modules`` in the workspace browser) stay with the
check that hides them.
"""

from __future__ import annotations

from pathlib import PurePath

# SSH private key files, also refused as bare words in shell commands.
PRIVATE_KEY_FILE_NAMES = frozenset({"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"})
# Credential folders and files, matched against whole path parts. A name with
# "/" matches consecutive parts: ``.config/gh`` is the GitHub CLI's login
# folder, while ``.config/ghostty`` is not sensitive.
SENSITIVE_NAMES = frozenset(
    {
        ".aws",
        ".config/gh",
        ".envrc",
        ".gog",
        ".gnupg",
        ".netrc",
        ".npmrc",
        ".ssh",
        "broker_credentials",
        "secrets",
        *PRIVATE_KEY_FILE_NAMES,
    }
)
# Private key and certificate-bundle file types.
SENSITIVE_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
# Env files: ``.env`` and anything ending in ``.env`` (the instance's own
# ``team.env``, a ``debug.env`` copy), plus ``.env.<something>`` variants
# such as ``.env.local``.
ENV_FILE_SUFFIX = ".env"
ENV_FILE_PREFIX = ".env."

# What sensitive_reason reports; path_policy shows it as "Sensitive <reason>".
REASON_PATH = "path"
REASON_FILE_TYPE = "file type"
REASON_ENV_FILE = "env file"

_NAME_SEQUENCES = tuple(tuple(name.split("/")) for name in sorted(SENSITIVE_NAMES))


def sensitive_reason(path: PurePath) -> str | None:
    """Why ``path`` names a credential (``REASON_*``), or None when it does not."""

    parts = tuple(part.lower() for part in path.parts)
    if any(_contains_sequence(parts, sequence) for sequence in _NAME_SEQUENCES):
        return REASON_PATH
    if any(PurePath(part).suffix in SENSITIVE_SUFFIXES for part in parts):
        return REASON_FILE_TYPE
    if any(part.endswith(ENV_FILE_SUFFIX) or part.startswith(ENV_FILE_PREFIX) for part in parts):
        return REASON_ENV_FILE
    return None


def is_sensitive_path(path: PurePath) -> bool:
    """True when any part of ``path`` names a credential file or folder."""

    return sensitive_reason(path) is not None


def _contains_sequence(parts: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    width = len(sequence)
    return any(parts[index : index + width] == sequence for index in range(len(parts) - width + 1))


__all__ = [
    "ENV_FILE_PREFIX",
    "ENV_FILE_SUFFIX",
    "PRIVATE_KEY_FILE_NAMES",
    "REASON_ENV_FILE",
    "REASON_FILE_TYPE",
    "REASON_PATH",
    "SENSITIVE_NAMES",
    "SENSITIVE_SUFFIXES",
    "is_sensitive_path",
    "sensitive_reason",
]

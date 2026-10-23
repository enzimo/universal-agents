"""Guard: every secret the project reads from the environment has a ``_FILE`` form
and names the processes that receive it.

The owner rules are that any secret environment variable can also come from a
Docker secret file (``<NAME>_FILE``), and that each secret reaches only the
processes that use it. Both live in one list, ``docker/secret_env_vars.yaml``,
which only ``env_secrets.py`` reads; its Pydantic model refuses an entry
without receivers, with an unknown process, or with a bad ``file`` value.

This test scans the source for names that look like secrets and fails when one
is neither in that list nor in ``NOT_SECRET_VALUES`` below. When it fails for a
new variable, add the name to the shared list with the processes that read it
(it then works as a Docker secret everywhere), or, if it is not a secret, add
it here with the reason.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import subprocess

from runtime_settings.security import REMOVED_TOOL_CREDENTIAL_ENV_VARS
from runtime_settings.services import REMOVED_LINKED_KEY_ENV_VARS
from env_secrets import (
    DEFAULT_SECRET_FILE_ENV_VARS,
    LISTED_VARIABLES,
    PROCESS_NAMES,
    PROCESS_NATS,
    PROCESS_OPENSHELL_CLI,
    PROCESS_POSTGRES,
    PROCESS_SEALBOX,
    SECRET_ENV_LIST_PATH,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
# Tests and CI runner code are not part of the shipped runtime.
EXCLUDED_PREFIXES = ("tests/", ".github/")
# Whole segments (between underscores) that mark a secret-looking name.
SECRET_SEGMENTS = frozenset(
    {"TOKEN", "TOKENS", "KEY", "KEYS", "SECRET", "SECRETS", "PASSWORD", "PASS", "PASSPHRASE",
     "DSN", "CREDENTIAL", "CREDENTIALS", "AUTH", "COOKIE", "COOKIES", "SIGNING", "SESSION"}
)
# Words long enough to also count at the end of a segment (BW_CLIENTSECRET,
# PGPASSWORD) without catching KEEPASS or HOTKEY.
SECRET_SEGMENT_ENDINGS = ("TOKEN", "SECRET", "PASSWORD", "PASSPHRASE", "CREDENTIAL", "COOKIE", "SESSION")
# By convention these name a path, directory, address or setting, never the
# secret itself: KEEPASSXC_KEY_FILE, SECURE_TOOL_BROKER_CREDENTIALS_DIR.
NON_SECRET_SUFFIXES = ("_FILE", "_PATH", "_DIR", "_SECONDS", "_BYTES", "_URL")
_UPPER_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_SHELL_NAME = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_NODE_ENV_READ = re.compile(r"process\.env(?:\.|\[\s*[\"'])([A-Z][A-Z0-9_]*)")

# Secret-looking names that are deliberately not in the shared list.
NOT_SECRET_VALUES: dict[str, str] = {
    # Sealbox's own CLI reads SEALBOX_TOKEN_FILE and SEALBOX_*_KEY_FILE, and the
    # broker points them at each owner's key files; copying file contents into
    # the environment would undo that separation.
    "SEALBOX_TOKEN": "Sealbox reads its own SEALBOX_TOKEN_FILE",
    "SEALBOX_PUBLIC_KEY": "Sealbox reads its own SEALBOX_PUBLIC_KEY_FILE",
    "SEALBOX_PRIVATE_KEY": "Sealbox reads its own SEALBOX_PRIVATE_KEY_FILE",
    # Switches and settings, not secret values.
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "Hugging Face CLI switch",
    "KEEPASSXC_NO_PASSWORD": "KeePassXC switch for databases without a password",
    "KEEPASS_NO_PASSWORD": "KeePass switch for databases without a password",
    "OPENAI_CODEX_ALLOW_SHARED_AUTH_READ": "switch allowing a shared Codex sign-in file",
    "ACTIVITY_COLLECTOR_INGEST_TOKEN_ENV_VAR": "names the variable that holds the token",
    "LLM_CONTEXT_WINDOW_TOKENS": "model context window size",
    "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE": "broker storage setting",
    "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE": "broker storage setting",
    "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_NAMESPACE": "broker storage setting",
    "SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_FALLBACK_TO_FILES": "broker storage setting",
    "SECURE_TOOL_BROKER_DEFAULT_CREDENTIAL_WRITE_SOURCE": "broker storage setting",
    "INSTALL_CREDENTIAL_CLIS": "Docker build switch",
    # The Photon trust-store test hands its child process a throwaway TLS key's path.
    "TEST_KEY": "path of a test-only TLS key file, not a secret value",
    # Resolved by the migration script itself (see OWN_FILE_RESOLVERS): it runs
    # inside the old Postgres image, which has no copy of the shared list.
    "POSTGRES_MIGRATION_DSN": "migration script resolves its own _FILE form",
    "POSTGRES_MIGRATION_PASSWORD": "migration script resolves its own _FILE form",
    "POSTGRES_SUPERUSER_DSN": "migration script resolves its own _FILE form",
    "POSTGRES_SUPERUSER_PASSWORD": "migration script resolves its own _FILE form",
}

# Listed names that only a program the broker runs reads, so no project file
# mentions them.
READ_ONLY_BY_BROKER_TOOLS: dict[str, str] = {
    "GH_HOST": "GitHub CLI (gh)",
}
# Tool tokens that no process may take from the environment any more: startup
# refuses them (runtime_settings.security), and code names them only to refuse
# them, to remove them from a child's environment, or to hand a CLI the task
# person's own stored token. The web search and crawling keys are refused too
# (runtime_settings.services): they are linked accounts in the credential
# store, and onboarding names them only to hand an imported key over.
REFUSED_AT_STARTUP = frozenset(REMOVED_TOOL_CREDENTIAL_ENV_VARS) | frozenset(REMOVED_LINKED_KEY_ENV_VARS)
# Third-party servers and tools that must receive no listed variable.
RECEIVE_NOTHING = frozenset({PROCESS_NATS, PROCESS_OPENSHELL_CLI, PROCESS_SEALBOX, PROCESS_POSTGRES})

# Scripts that resolve <NAME>_FILE themselves, with the names they cover.
OWN_FILE_RESOLVERS: dict[str, tuple[str, ...]] = {
    "scripts/migrate_bundled_postgres_15_to_18.sh": (
        "POSTGRES_MIGRATION_DSN",
        "POSTGRES_MIGRATION_PASSWORD",
        "POSTGRES_SUPERUSER_DSN",
        "POSTGRES_SUPERUSER_PASSWORD",
        "PGPASSWORD",
    ),
}


def _looks_secret(name: str) -> bool:
    if not _UPPER_NAME.match(name) or name.endswith(NON_SECRET_SUFFIXES):
        return False
    return any(
        segment in SECRET_SEGMENTS or segment.endswith(SECRET_SEGMENT_ENDINGS)
        for segment in name.split("_")
    )


def _source_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [
        REPO_ROOT / relative
        for relative in listed
        if not relative.startswith(EXCLUDED_PREFIXES)
        and "node_modules/" not in relative
        and (REPO_ROOT / relative).is_file()
    ]


def _exported_names(tree: ast.AST) -> set[int]:
    """ids of the string nodes inside ``__all__``: identifiers, not values."""

    exported: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            exported.update(id(item) for item in ast.walk(node.value))
    return exported


def _python_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    exported = _exported_names(tree)
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in exported
    }


def _names_in(path: Path) -> set[str]:
    if path.suffix == ".py":
        return _python_names(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix == ".sh" or path.name.startswith("Dockerfile"):
        return set(_SHELL_NAME.findall(text))
    if path.suffix in {".js", ".mjs", ".cjs"}:
        # Browser assets never read the environment; only Node processes do.
        return set(_NODE_ENV_READ.findall(text))
    return set()


def _secret_looking_names() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in _source_files():
        for name in _names_in(path):
            if _looks_secret(name):
                found.setdefault(name, set()).add(str(path.relative_to(REPO_ROOT)))
    return found


def test_every_secret_looking_variable_supports_a_secret_file() -> None:
    covered = set(DEFAULT_SECRET_FILE_ENV_VARS)
    for names in OWN_FILE_RESOLVERS.values():
        covered.update(names)
    uncovered = {
        name: sorted(files)
        for name, files in _secret_looking_names().items()
        if name not in covered and name not in NOT_SECRET_VALUES and name not in REFUSED_AT_STARTUP
    }

    assert not uncovered, (
        "These secret-looking names have no <NAME>_FILE support. Add each secret to "
        f"{SECRET_ENV_LIST_PATH.relative_to(REPO_ROOT)}, or add a non-secret name to "
        f"NOT_SECRET_VALUES in this test with the reason: {uncovered}"
    )


def test_allowlisted_names_are_still_used() -> None:
    """Remove an allowlist entry once the code no longer uses the name."""

    found = _secret_looking_names()
    stale = sorted(name for name in NOT_SECRET_VALUES if name not in found)

    assert not stale


def test_allowlist_and_shared_list_do_not_overlap() -> None:
    listed = set(DEFAULT_SECRET_FILE_ENV_VARS)

    assert not listed & set(NOT_SECRET_VALUES)
    # A refused variable must not also be one a process may receive.
    assert not {entry.name for entry in LISTED_VARIABLES} & REFUSED_AT_STARTUP


def test_scripts_with_their_own_resolver_read_each_file_form() -> None:
    for relative, names in OWN_FILE_RESOLVERS.items():
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for name in names:
            assert f"{name}_FILE:{name}" in text, f"{relative} does not read {name}_FILE"


def test_every_listed_name_is_used_somewhere() -> None:
    """Catches typos in the shared list and names the code no longer reads."""

    text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in _source_files()
        if path.suffix in {".py", ".sh", ".mjs", ".js"}
    )
    unused = [
        entry.name
        for entry in LISTED_VARIABLES
        if not entry.is_prefix
        and entry.name not in READ_ONLY_BY_BROKER_TOOLS
        and not re.search(rf"\b{entry.name}\b", text)
    ]

    assert not unused
    assert set(READ_ONLY_BY_BROKER_TOOLS) <= {entry.name for entry in LISTED_VARIABLES}


def test_every_listed_name_goes_only_to_known_processes_that_can_use_it() -> None:
    """Each entry names at least one known process, and never a third-party server or tool."""

    for entry in LISTED_VARIABLES:
        assert entry.recipients, entry.name
        assert entry.recipients <= PROCESS_NAMES, entry.name
        assert not entry.recipients & RECEIVE_NOTHING, entry.name


"""Secret environment variables: their Docker ``*_FILE`` form and who receives them.

Both are defined once, in ``docker/secret_env_vars.yaml``. This module is its
only reader: it loads the file once, at import, through the Pydantic model
below, and refuses unknown keys, unknown process names, bad ``file`` values
and duplicate names with an error that names the entry. The container
entrypoint (``docker/entrypoint.sh``) does not read the list; it runs this
module's ``check-container-env`` command, and ``tests/test_env_secret_names.py``
fails when code reads a secret-looking variable that the list does not cover.
``scripts/export_secret_env_docs.py`` generates the tables in
``user_docs_source/docker.md`` from the same list.

Who receives a variable: each entry names the processes that use it, and
:func:`environment_for` removes every listed variable (and its ``_FILE``
companion) from every other process's environment. The supervisor builds each
process's environment this way, so a leak inside one process (a log line, an
error report, a third-party program it starts) cannot disclose secrets meant
for another. It does not separate processes that run as the same Linux user:
code running outside the sandbox can still read another process's
environment through ``/proc``; the code sandbox is what stops that.

Precedence has two stages:

* At process start, :func:`materialize_secret_file_envs` copies each file's
  contents into ``<NAME>``. A file always beats a direct value, so a Docker
  secret wins over a value that onboarding or an operator wrote to
  ``team.env``. In a container, :func:`container_secret_file_problems` first
  refuses an environment that sets both, because the operator then gave two
  values and only one can win.
* After that, :func:`get_env_with_file_fallback` prefers the direct value. By
  then the direct value is either the file's contents or a value the running
  process set on purpose (for example the broker storing a fresh
  ``BW_SESSION`` after a Bitwarden unlock), which must not be replaced by a stale
  mounted file. Its file fallback covers names chosen in configuration (such
  as ``web_chat_token_env_var``) that the shared list cannot know.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal, Mapping, MutableMapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
import yaml


LOGGER = logging.getLogger(__name__)

SECRET_ENV_LIST_PATH = Path(__file__).resolve().parent / "docker" / "secret_env_vars.yaml"
FILE_SUFFIX = "_FILE"

# Values of an entry's ``file`` key.
FileForm = Literal["value", "none"]
FORM_VALUE: FileForm = "value"  # NAME_FILE names a file holding the value (the default)
FORM_NONE: FileForm = "none"  # no NAME_FILE form; the entry only limits who receives NAME
# A listed name ending in this covers every variable that starts with the rest.
PREFIX_MARKER = "*"

# Processes the supervisor starts. Names match the runtime manifests' service
# names where one exists (runtime_hosting.manifests). The list's ``processes``
# section must name exactly these, each with a one-line description.
PROCESS_GATEWAY = "gateway"
PROCESS_MANAGER = "manager"
PROCESS_AGENT = "agent"
PROCESS_SECURE_BROKER = "secure_broker"
PROCESS_ACTIVITY_COLLECTOR = "activity_collector"
PROCESS_EXECUTION_CONTROLLER = "execution_controller"
PROCESS_ADAPTER_HOST = "adapter_host"
PROCESS_CONSUMER_CHECK = "consumer_check"
PROCESS_OPENSHELL_GATEWAY = "openshell_gateway"
# Third-party servers and tools that receive no listed variable.
PROCESS_NATS = "nats"
PROCESS_OPENSHELL_CLI = "openshell_cli"
PROCESS_SEALBOX = "sealbox"
PROCESS_POSTGRES = "postgres"
PROCESS_NAMES = frozenset(
    {
        PROCESS_GATEWAY,
        PROCESS_MANAGER,
        PROCESS_AGENT,
        PROCESS_SECURE_BROKER,
        PROCESS_ACTIVITY_COLLECTOR,
        PROCESS_EXECUTION_CONTROLLER,
        PROCESS_ADAPTER_HOST,
        PROCESS_CONSUMER_CHECK,
        PROCESS_OPENSHELL_GATEWAY,
        PROCESS_NATS,
        PROCESS_OPENSHELL_CLI,
        PROCESS_SEALBOX,
        PROCESS_POSTGRES,
    }
)

# The one command the entrypoint runs (``python env_secrets.py <command>``).
CHECK_CONTAINER_ENV_COMMAND = "check-container-env"

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class SecretEnvEntry(BaseModel):
    """One variable: the processes that receive it, its ``_FILE`` form, and what it is."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    receivers: tuple[str, ...] = Field(min_length=1)
    file: FileForm = FORM_VALUE
    about: str = Field(min_length=1)


class SecretEnvGroup(BaseModel):
    """A heading in the docs and the variables under it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    variables: dict[str, SecretEnvEntry] = Field(min_length=1)


def _entry_problems(name: str, entry: SecretEnvEntry, processes: Mapping[str, str]) -> list[str]:
    """Why ``name``'s entry is invalid, if it is; each message names the entry."""

    problems: list[str] = []
    is_prefix = name.endswith(PREFIX_MARKER)
    if not _ENV_NAME.match(name[: -len(PREFIX_MARKER)] if is_prefix else name):
        problems.append(f"{name}: not an upper-case environment variable name")
    if is_prefix and entry.file != FORM_NONE:
        problems.append(f"{name}: a name ending in {PREFIX_MARKER!r} must use file: {FORM_NONE}")
    unknown = sorted(set(entry.receivers) - set(processes))
    if unknown:
        problems.append(f"{name}: unknown process {', '.join(unknown)}; add it to processes or fix the name")
    return problems


class SecretEnvList(BaseModel):
    """The whole of ``docker/secret_env_vars.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    processes: dict[str, str] = Field(min_length=1)
    groups: tuple[SecretEnvGroup, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_names(self) -> SecretEnvList:
        problems: list[str] = []
        if set(self.processes) != PROCESS_NAMES:
            problems.append(
                "processes must name exactly the processes in env_secrets.py "
                f"(missing: {sorted(PROCESS_NAMES - set(self.processes))}, "
                f"unknown: {sorted(set(self.processes) - PROCESS_NAMES)})"
            )
        problems.extend(f"process {name}: needs a description" for name, about in self.processes.items() if not about.strip())
        seen: set[str] = set()
        for group in self.groups:
            for name, entry in group.variables.items():
                if name in seen:
                    problems.append(f"{name}: listed more than once")
                seen.add(name)
                problems.extend(_entry_problems(name, entry, self.processes))
        if len({group.name for group in self.groups}) != len(self.groups):
            problems.append("group names must be unique")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def entries(self) -> list[tuple[str, SecretEnvEntry]]:
        """Every variable with its entry, in file order."""

        return [item for group in self.groups for item in group.variables.items()]


class _UniqueKeyLoader(yaml.SafeLoader):
    """A YAML loader that refuses a repeated key instead of keeping the last one."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"{key} is listed more than once", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _validation_message(exc: ValidationError) -> str:
    """One line per problem, each starting with the path to the entry (group, then name)."""

    lines = []
    for error in exc.errors():
        where = " > ".join(str(part) for part in error["loc"]) or "list"
        lines.append(f"{where}: {error['msg']}")
    return "; ".join(lines)


def parse_secret_env_list(text: str, *, source: Path) -> SecretEnvList:
    """Validate the list's YAML text; raise ``ValueError`` naming the bad entry."""

    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid secret variable list {source}: {exc}") from exc
    try:
        return SecretEnvList.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Invalid secret variable list {source}: {_validation_message(exc)}") from exc


def load_secret_env_list(path: Path = SECRET_ENV_LIST_PATH) -> SecretEnvList:
    """Read and validate the shared list."""

    return parse_secret_env_list(path.read_text(encoding="utf-8"), source=path)


@dataclass(frozen=True)
class ListedVariable:
    """One entry of the shared list: a variable and the processes that receive it."""

    name: str
    recipients: frozenset[str]
    form: str = FORM_VALUE

    @property
    def is_prefix(self) -> bool:
        return self.name.endswith(PREFIX_MARKER)

    def covers(self, key: str) -> bool:
        """Whether ``key`` is this variable or its ``_FILE`` companion."""

        if self.is_prefix:
            return key.startswith(self.name[: -len(PREFIX_MARKER)])
        return key in (self.name, f"{self.name}{FILE_SUFFIX}")


def listed_variables(secret_env_list: SecretEnvList) -> tuple[ListedVariable, ...]:
    """The routing view of the list: each name, its receivers and its ``_FILE`` form."""

    return tuple(
        ListedVariable(name=name, recipients=frozenset(entry.receivers), form=entry.file)
        for name, entry in secret_env_list.entries()
    )


def parse_listed_variables(text: str, *, source: Path) -> tuple[ListedVariable, ...]:
    """Every entry of the list's YAML text, in order."""

    return listed_variables(parse_secret_env_list(text, source=source))


def load_listed_variables(path: Path = SECRET_ENV_LIST_PATH) -> tuple[ListedVariable, ...]:
    """Every entry of the shared list, in order."""

    return listed_variables(load_secret_env_list(path))


SECRET_ENV_LIST = load_secret_env_list()
LISTED_VARIABLES = listed_variables(SECRET_ENV_LIST)
DEFAULT_SECRET_FILE_ENV_VARS = tuple(entry.name for entry in LISTED_VARIABLES if entry.form == FORM_VALUE)


def _entries_with_renames(
    listed: Sequence[ListedVariable], renamed: Mapping[str, str]
) -> list[ListedVariable]:
    by_name = {entry.name: entry for entry in listed}
    entries = list(listed)
    for configured, listed_name in renamed.items():
        if listed_name not in by_name:
            raise ValueError(f"{configured} is configured in place of {listed_name}, which is not listed")
        entries.append(ListedVariable(name=configured, recipients=by_name[listed_name].recipients))
    return entries


def withheld_names(
    process: str,
    env: Mapping[str, str],
    *,
    renamed: Mapping[str, str] | None = None,
    listed: Sequence[ListedVariable] = LISTED_VARIABLES,
) -> frozenset[str]:
    """Names in ``env`` that ``process`` must not receive.

    ``renamed`` maps a variable name chosen in configuration to the listed
    name it replaces (for example a custom ``web_chat_token_env_var`` to
    ``WEB_CHAT_AUTH_TOKEN``); the chosen name goes to the same processes.
    """

    if process not in PROCESS_NAMES:
        raise ValueError(f"Unknown process {process!r}; expected one of {sorted(PROCESS_NAMES)}")
    denying = [entry for entry in _entries_with_renames(listed, renamed or {}) if process not in entry.recipients]
    return frozenset(key for key in env if any(entry.covers(key) for entry in denying))


def environment_for(
    process: str,
    env: Mapping[str, str],
    *,
    renamed: Mapping[str, str] | None = None,
    listed: Sequence[ListedVariable] = LISTED_VARIABLES,
) -> dict[str, str]:
    """A copy of ``env`` without the listed variables ``process`` does not use.

    Every process the supervisor starts gets its environment from here, so
    the shared list alone decides who holds each secret.
    """

    withheld = withheld_names(process, env, renamed=renamed, listed=listed)
    return {key: value for key, value in env.items() if key not in withheld}


def is_withheld_from(process: str, name: str, *, listed: Sequence[ListedVariable] = LISTED_VARIABLES) -> bool:
    """Whether the shared list keeps ``name`` away from ``process``."""

    return bool(withheld_names(process, {name: ""}, listed=listed))


def _normalized_env_value(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    return normalized if normalized != "" else None


def _existing_secret_file(path_value: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Secret file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Secret path is not a file: {path}")
    return path


def _read_secret_file(path_value: str) -> str:
    return _existing_secret_file(path_value).read_text(encoding="utf-8").rstrip("\r\n")


def get_env_with_file_fallback(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return ``name``'s value, or the contents of ``<name>_FILE`` when unset.

    The direct value wins here because startup already applied the
    file-first rule (see the module docstring).
    """

    env_mapping = env if env is not None else os.environ
    direct_value = _normalized_env_value(env_mapping.get(name))
    if direct_value is not None:
        return direct_value

    file_path = _normalized_env_value(env_mapping.get(f"{name}{FILE_SUFFIX}"))
    if file_path is None:
        return None
    return _read_secret_file(file_path)


def materialize_secret_file_envs(
    env: MutableMapping[str, str] | None = None,
    *,
    variable_names: Sequence[str] = DEFAULT_SECRET_FILE_ENV_VARS,
) -> dict[str, str]:
    """Copy every listed ``<NAME>_FILE`` into ``<NAME>``; a file beats a direct value.

    Returns every listed name that ends up with a value.
    """

    env_mapping = env if env is not None else os.environ
    resolved: dict[str, str] = {}
    for name in variable_names:
        file_path = _normalized_env_value(env_mapping.get(f"{name}{FILE_SUFFIX}"))
        if file_path is not None:
            env_mapping[name] = _read_secret_file(file_path)
        value = _normalized_env_value(env_mapping.get(name))
        if value is not None:
            resolved[name] = value
    return resolved


def container_secret_file_problems(
    env: Mapping[str, str],
    *,
    listed: Sequence[ListedVariable] = LISTED_VARIABLES,
) -> list[str]:
    """Why a container environment's secret files cannot be used, one message each.

    An operator who sets both ``<NAME>`` and ``<NAME>_FILE`` gave two values
    and the file would silently win, and a ``<NAME>_FILE`` that points to no
    file would stop the first process that reads it. The entrypoint runs this
    once, on the container's own environment, before it starts anything. It
    is not applied to the environments the supervisor builds for its
    processes, because those legitimately hold a ``team.env`` value next to
    its file form. Messages name variables and paths, never values.
    """

    problems: list[str] = []
    for entry in listed:
        if entry.form == FORM_NONE:
            continue
        file_name = f"{entry.name}{FILE_SUFFIX}"
        file_path = _normalized_env_value(env.get(file_name))
        if file_path is None:
            continue
        if _normalized_env_value(env.get(entry.name)) is not None:
            problems.append(f"Both {entry.name} and {file_name} are set; use only one.")
        elif not Path(file_path).expanduser().is_file():
            problems.append(f"{file_name} points to a missing file: {file_path}")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry for the container entrypoint; see ``CHECK_CONTAINER_ENV_COMMAND``."""

    parser = argparse.ArgumentParser(description="Check the secret variables of the container environment.")
    parser.add_argument(
        "command",
        choices=[CHECK_CONTAINER_ENV_COMMAND],
        help="refuse NAME and NAME_FILE set together, or a NAME_FILE naming a missing file",
    )
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    problems = container_secret_file_problems(os.environ)
    for problem in problems:
        LOGGER.error(problem)
    return 2 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The ``operator_secrets`` section: secrets the operator supplied, encrypted in the file.

What travels (owner's decision 2026-10-05: credentials move with the team):

- every secret variable in ``docker/secret_env_vars.yaml`` the operator
  supplied, from ``team.env`` or the environment (chat channel tokens, model
  key overrides, password-manager and cloud settings). Internally generated
  values (``internal_credentials.INTERNAL_ENV_KEYS``: signing keys, the broker
  token, a local NATS server's login, database credentials, collector tokens)
  are not carried: the new instance generates its own, and its database is a
  new one;
- the NATS user and password (``NATS_USER``, ``NATS_PASSWORD``) only when the
  instance's own settings say its NATS server is external: they name only
  non-local servers or set ``NATS_CREDENTIALS_FILE``
  (``should_generate_internal_nats_credentials``). Its own settings are the
  saved ``instance_config.yaml`` plus ``team.env``, or the environment with
  ``environment`` secrets (``_saved_config``, ``_environment``); with
  ``env_file`` secrets the export command's own environment does not decide.
  An external server's login is the operator's, so it
  travels; a local server's is made anew. The server's address travels only
  when ``instance_config.yaml`` names it (with the settings section).
  ``NATS_URL``, ``NATS_SERVERS`` or ``NATS_CREDENTIALS_FILE`` from ``team.env``
  or the environment do not travel, nor does the file ``NATS_CREDENTIALS_FILE``
  names or the image's local-server switch ``TEAM_LOCAL_NATS_ENABLED`` (a
  proposed follow-up: one shared decision of local or external NATS that
  travels). So the export prints what the new instance needs whenever the
  instance's settings name an external server, whether or not a login travels
  (a credentials file or a server without a login carries none), and says
  when the instance, run with the command's environment over ``team.env``,
  would use a NATS address or login the export did not (``_nats_notices``):
  following the guide then puts the login the instance runs with into
  ``team.env`` beside the address. The import refuses the login, with the
  steps, while the new instance's settings name a local server
  (``_generated_reason``), and stops before writing anything when it cannot
  read the new instance's NATS address (``_new_instance_generated``). The
  lines name variables, have one fixed form each and point to the guide
  (``EXTERNAL_NATS_DOCS``), which holds the details;
- the model-provider sign-in files ``auth/openai_codex_auth.json`` and
  ``auth/xai_oauth_auth.json``. Their refresh tokens rotate, so the old
  instance must stop right after the export (the export says so).

The records are encrypted with the transfer passphrase and decrypted only by
the import. They go into the new ``team.env`` (``launch.secrets_source:
env_file``) or, when the new instance takes its secrets from the environment,
are listed by name so the operator sets them there. Values never appear in
reports, logs or messages; only names do.

Who handles the values: the operator's own command, which already holds the
instance's root secrets on the host. The broker's credentials (stored
sign-ins, model keys, credential catalog) are a separate section that only
the running broker decrypts.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Mapping

from pydantic import BaseModel, Field

from team_transfer.errors import TransferError
from team_transfer.format import TransferModel
from team_transfer.sections import (
    ExportSource,
    ImportContext,
    PlannedRecord,
    RecordOutcome,
    SectionExport,
    SectionFiles,
    SectionRecords,
)

logger = logging.getLogger(__name__)

_FILE_SUFFIX = "_FILE"
# The guide section every NATS line of the export and the import ends with; it
# holds the details the lines leave out (user_docs_source/team-transfer.md).
EXTERNAL_NATS_DOCS = "/docs/team-transfer/#an-external-nats-server"
_SEE_THE_GUIDE = f'See "An external NATS server" in the team transfer guide ({EXTERNAL_NATS_DOCS}).'
# Where the import reads the new instance's NATS decision when it takes secrets from the environment.
IMPORT_ENVIRONMENT = "the environment this import runs in"


class SecretKind(StrEnum):
    ENVIRONMENT = "environment"
    AUTH_FILE = "auth_file"


class OperatorSecretV1(TransferModel):
    kind: SecretKind
    # The variable name, or the sign-in file's name under auth/.
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(repr=False)


def _generated_names(
    config: Any = None, environment: Mapping[str, str] | None = None
) -> frozenset[str]:
    from internal_credentials import INTERNAL_ENV_KEYS
    from assistant_gateway.instance_secrets import (
        NATS_CREDENTIAL_ENV_NAMES,
        should_generate_internal_nats_credentials,
    )

    names = set(INTERNAL_ENV_KEYS)
    if config is not None and not should_generate_internal_nats_credentials(
        config, environment or {}
    ):
        names.difference_update(NATS_CREDENTIAL_ENV_NAMES)
    return frozenset(names)


def _process_environment() -> dict[str, str]:
    """This command's process environment, without empty values.

    The export reads it as the old instance's environment, and the import's
    plan as the new instance's, only when that instance takes its secrets
    from the environment: the command then runs where the instance runs (for
    example inside its container, or onboarding's Import inside the new one).
    Otherwise it may hold the operator's own variables, which the instance
    never used.
    """

    return {key: value for key, value in os.environ.items() if value}


def _environment(source: ExportSource) -> dict[str, str]:
    """Where the running instance takes its secrets from (``launch.secrets_source``).

    With ``env_file`` only its ``team.env`` is read: the command's own
    environment may hold the operator's personal variables, which the
    instance never used. With ``environment`` the process environment is the
    instance's (run the export inside its container).
    """

    from internal_credentials import parse_env_file

    if source.config.launch.secrets_from_environment:
        return _process_environment()
    env_path = Path(source.config.launch.env_file_path or source.layout.team_env_path)
    return parse_env_file(env_path) if env_path.is_file() else {}


def _saved_config(source: ExportSource) -> Any:
    """The instance's saved settings (``instance_config.yaml``), with no environment applied.

    Whether the instance generated its NATS user and password is decided from
    these settings plus the instance's environment (``_environment``):
    ``should_generate_internal_nats_credentials`` reads that environment's
    NATS_CREDENTIALS_FILE, NATS_SERVERS and NATS_URL ahead of the saved
    addresses. Onboarding and the import decide the same way.

    ``source.config`` is not used: it has this command's own environment
    applied, and an operator's shell may name a NATS server the instance does
    not use. Applying the instance's environment to the saved settings
    (``load_for_process``) is not needed either: it changes no decision, and
    it fails on values the decision never reads, such as a malformed
    NATS_ACK_WAIT_SECONDS or a NATS_PASSWORD_FILE that exists only inside the
    instance's container.
    """

    from assistant_gateway.config import GatewayInstanceConfig

    return GatewayInstanceConfig.load_saved(source.layout.instance_config_path)


def _nats_notices(
    source: ExportSource,
    config: Any,
    environment: Mapping[str, str],
    values: Mapping[str, str],
) -> list[str]:
    """At most two fixed lines about NATS, each pointing to the guide; names only, as an address may hold a login.

    Line 1 is ``_unused_command_nats``. Line 2, for an external server: the
    login carried and what the new instance needs, because the address
    variables and the image's local-server switch do not travel (without the
    switch the image starts its own server), and the import says nothing when
    it accepts the login.
    """

    from assistant_gateway.instance_secrets import NATS_DECISION_ENV_NAMES, should_generate_internal_nats_credentials
    from docker.nats_service import LOCAL_NATS_ENABLED_ENV

    # The same rule on the same inputs _operator_values used, so it adds no new way to fail.
    instance_generates = should_generate_internal_nats_credentials(config, environment)
    side = "local" if instance_generates else "external"
    lines = []
    if unused := _unused_command_nats(source, config, environment, instance_generates, values):
        lines.append(
            f"NATS: run with this command's environment over team.env, the instance would use {', '.join(unused)}, "
            f"which this export did not use. The export decided from instance_config.yaml and team.env that the NATS "
            f"server is {side}. {_SEE_THE_GUIDE}"
        )
    if not instance_generates:
        needed = [f"{LOCAL_NATS_ENABLED_ENV}=false", *(name for name in NATS_DECISION_ENV_NAMES if environment.get(name))]
        lines.append(
            f"NATS: the server is external. Login carried: {', '.join(_carried_nats_names(values)) or 'none'}. "
            f"Set these on the new instance before importing; they do not travel: {', '.join(needed)}. {_SEE_THE_GUIDE}"
        )
    return lines


def _unused_command_nats(
    source: ExportSource,
    config: Any,
    environment: Mapping[str, str],
    instance_generates: bool,
    values: Mapping[str, str],
) -> list[str]:
    """The NATS variables the instance would use, run with this command's environment, that this export did not.

    Only with ``env_file`` secrets: the export then reads ``team.env``, not this
    command's environment, which may hold the operator's own variables. But the
    launcher lays the container's environment over ``team.env`` name by name,
    an empty value included (``docker/launch_headless.build_process_env``), so
    compose can run the instance with another address or login. Every differing
    name is returned when (a) the command's variables would decide local or
    external otherwise, or cannot be read (that does not stop the export: the
    instance never used them), or (b) the server is external: the
    new instance then needs the address and login the instance really uses. A
    local instance whose command also means local gets []: the new instance
    makes its own login.
    """

    from assistant_gateway.instance_secrets import (
        NATS_CREDENTIAL_ENV_NAMES,
        NATS_DECISION_ENV_NAMES,
        should_generate_internal_nats_credentials,
    )

    if source.config.launch.secrets_from_environment:
        return []
    forms = [*NATS_DECISION_ENV_NAMES, *(name + end for name in NATS_CREDENTIAL_ENV_NAMES for end in ("", _FILE_SUFFIX))]
    overlay = {**environment, **{name: os.environ[name] for name in forms if name in os.environ}}
    names = [name for name in NATS_DECISION_ENV_NAMES if overlay.get(name, "") != environment.get(name, "")]
    for name in NATS_CREDENTIAL_ENV_NAMES:
        used = _login_variable(overlay, name, file_first=True)
        if used != _login_variable(environment, name, file_first=False):
            names.append(used[0] if used else name)
    event = "team_transfer_nats_environment_differs"
    try:
        decides_otherwise = should_generate_internal_nats_credentials(config, overlay) != instance_generates
    except ValueError:  # Only the command's values can fail here: the instance's were read already.
        decides_otherwise, event = True, "team_transfer_nats_environment_unreadable"
    if not decides_otherwise and (instance_generates or not names):
        return []
    logger.warning(
        "The export did not use NATS variables the instance would use with this command's environment; the "
        "instance's own settings decided. event=%s team_id=%s instance_name=%s decided_by=instance_settings "
        "command_names=%s instance_nats=%s carried_names=%s",
        event, source.team_id, source.instance_name, ",".join(names), "local" if instance_generates else "external",
        ",".join(_carried_nats_names(values)) or "none",
    )
    return names


def _login_variable(values: Mapping[str, str], name: str, *, file_first: bool) -> tuple[str, str] | None:
    """The variable, and its value, one side uses for a NATS login name; no file is read.

    The instance starts with ``<name>_FILE`` first (``materialize_secret_file_envs``);
    the export reads ``team.env`` plain value first (``get_env_with_file_fallback``).
    """

    forms = (name + _FILE_SUFFIX, name) if file_first else (name, name + _FILE_SUFFIX)
    return next(((form, values[form]) for form in forms if values.get(form)), None)


def _carried_nats_names(values: Mapping[str, str]) -> list[str]:
    from assistant_gateway.instance_secrets import NATS_CREDENTIAL_ENV_NAMES

    return [name for name in NATS_CREDENTIAL_ENV_NAMES if name in values]


def _operator_values(
    environment: Mapping[str, str], config: Any = None
) -> dict[str, str]:
    from env_secrets import get_env_with_file_fallback, load_listed_variables

    generated = _generated_names(config, environment)
    names = [
        name
        for listed in load_listed_variables()
        for name in _names_of(listed, environment)
    ]
    if config is not None:
        from runtime_settings.secret_names import configured_secret_names

        names.extend(configured_secret_names(config))
    values = {
        name: get_env_with_file_fallback(name, env=environment)
        for name in names
        if name not in generated
    }
    return {name: value for name, value in values.items() if value}


def _names_of(listed: Any, environment: Mapping[str, str]) -> list[str]:
    """The variable names one list entry covers; a prefix entry covers every such name present."""

    if not listed.is_prefix:
        return [listed.name]
    prefix = listed.name[:-1]
    return [
        key
        for key in environment
        if key.startswith(prefix) and not key.endswith(_FILE_SUFFIX)
    ]


def _auth_files(layout: Any) -> list[Any]:
    return [layout.openai_codex_auth_path, layout.xai_oauth_auth_path]


class OperatorSecretsSection:
    name: ClassVar[str] = "operator_secrets"
    schema_version: ClassVar[int] = 2
    encrypted: ClassVar[bool] = True
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = OperatorSecretV1

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        from team_transfer.sections import encrypt_section
        from team_transfer.settings import _relative_to_root

        environment = _environment(source)
        config = _saved_config(source)
        values = _operator_values(environment, config)
        secrets = [
            OperatorSecretV1(
                kind=SecretKind.ENVIRONMENT,
                name=name,
                value=(
                    _relative_to_root(value, str(source.layout.root))
                    if _is_path_variable(name)
                    else value
                ),
            )
            for name, value in sorted(values.items())
        ]
        for path in _auth_files(source.layout):
            if path.is_file() and not path.is_symlink():
                secrets.append(
                    OperatorSecretV1(
                        kind=SecretKind.AUTH_FILE,
                        name=path.name,
                        value=path.read_text(encoding="utf-8"),
                    )
                )
        records = [secret.model_dump(mode="json") for secret in secrets]
        return SectionExport(
            records=records,
            ciphertext=encrypt_section(
                self.name, self.schema_version, records, files.keys
            ),
            notices=_nats_notices(source, config, environment, values),
        )

    def plan(
        self, records: SectionRecords, context: ImportContext
    ) -> list[PlannedRecord]:
        from assistant_gateway.config import GatewayInstanceConfig

        secrets = [OperatorSecretV1.model_validate(record) for record in records]
        environment_mode = False
        config = context.scratch.get("settings.config")
        if isinstance(config, GatewayInstanceConfig):
            environment_mode = bool(config.launch.secrets_from_environment)
        auth_names = {path.name for path in _auth_files(context.layout)}
        existing = _target_env(context)
        # Where the new instance's NATS decision is read; the refusal and the error name it.
        where = IMPORT_ENVIRONMENT if environment_mode else str(_target_env_path(context))
        generated = _new_instance_generated(config, environment_mode, existing, where)
        planned = []
        for secret in secrets:
            key = f"{secret.kind.value}/{secret.name}"
            if secret.kind is SecretKind.AUTH_FILE and secret.name not in auth_names:
                planned.append(
                    PlannedRecord(
                        self.name,
                        key,
                        RecordOutcome.REFUSED,
                        "this version has no such sign-in file",
                    )
                )
            elif (
                secret.kind is SecretKind.AUTH_FILE
                and (context.layout.auth_dir / secret.name).exists()
            ):
                planned.append(
                    PlannedRecord(
                        self.name,
                        key,
                        RecordOutcome.ATTENTION,
                        "a sign-in already exists in the new instance; it is kept",
                    )
                )
            elif secret.kind is SecretKind.ENVIRONMENT and secret.name in generated:
                planned.append(
                    PlannedRecord(
                        self.name,
                        key,
                        RecordOutcome.REFUSED,
                        _generated_reason(secret.name, where),
                    )
                )
            elif secret.kind is SecretKind.ENVIRONMENT and existing.get(secret.name):
                planned.append(
                    PlannedRecord(
                        self.name,
                        key,
                        RecordOutcome.ATTENTION,
                        "the new team.env already has a value; it is kept",
                    )
                )
            elif secret.kind is SecretKind.ENVIRONMENT and environment_mode:
                planned.append(
                    PlannedRecord(
                        self.name,
                        key,
                        RecordOutcome.ATTENTION,
                        "this instance takes secrets from the environment: set it there or as a Docker secret",
                    )
                )
            else:
                planned.append(PlannedRecord(self.name, key, RecordOutcome.IMPORT))
        return planned

    def apply(
        self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext
    ) -> None:
        from internal_credentials import merge_env_file
        from team_transfer.settings import _under_root

        to_write = {
            record.key for record in plan if record.outcome is RecordOutcome.IMPORT
        }
        env_values: dict[str, str] = {}
        auth_by_name = {path.name: path for path in _auth_files(context.layout)}
        for raw in records:
            secret = OperatorSecretV1.model_validate(raw)
            if f"{secret.kind.value}/{secret.name}" not in to_write:
                continue
            if secret.kind is SecretKind.ENVIRONMENT:
                env_values[secret.name] = (
                    _under_root(secret.value, str(context.layout.root))
                    if _is_path_variable(secret.name)
                    else secret.value
                )
            else:
                _write_private(auth_by_name[secret.name], secret.value)
        if env_values:
            config = context.scratch["settings.config"]
            merge_env_file(
                Path(config.launch.env_file_path or context.layout.team_env_path),
                env_values,
                rotate=False,
            )
        logger.warning(
            "Operator secrets imported. event=team_transfer_operator_secrets_imported names=%s",
            ",".join(
                sorted(
                    record.key
                    for record in plan
                    if record.outcome is RecordOutcome.IMPORT
                )
            ),
        )


def _target_env_path(context: ImportContext) -> Path:
    """The new instance's env file: ``launch.env_file_path`` as planned, or its ``team.env``."""

    config = context.scratch.get("settings.config")
    return Path(
        getattr(getattr(config, "launch", None), "env_file_path", None)
        or context.layout.team_env_path
    )


def _target_env(context: ImportContext) -> dict[str, str]:
    """Values the operator already put in the new instance's team.env (they win over imported ones)."""

    from internal_credentials import parse_env_file

    path = _target_env_path(context)
    return parse_env_file(path) if path.is_file() else {}


def _new_instance_generated(
    config: Any, environment_mode: bool, env_file_values: Mapping[str, str], where: str
) -> frozenset[str]:
    """The values the new instance generates, decided from its env file or its environment.

    With secrets from the environment, that is the environment this import
    runs in (the new container for onboarding's Import), as the export reads
    the old instance's (``_environment``). In that mode the import writes
    nothing and the settings section generates nothing, so reading the
    process environment cannot change what the new instance generates.

    A NATS address there that the decision cannot read stops the plan, in
    either mode, with an error that names the variable and ``where``. The plan
    runs whatever is selected, so the import stops before writing anything;
    letting it through would fail later, after the write, where the same
    decision runs again (onboarding's completion, the env file's generated
    credentials) with the host in the error. The error is raised outside the
    ``except`` so it carries no ValueError, whose text may hold the host.
    """

    environment = _process_environment() if environment_mode else env_file_values
    try:
        return _generated_names(config, environment)
    except ValueError:
        if not (unreadable := _unreadable_nats_names(config, environment)):
            raise
    logger.warning(
        "The new instance's NATS address could not be read; the import stops. "
        "event=team_transfer_import_nats_address_unreadable team_id=%s names=%s secrets_source=%s",
        config.gateway.team_id, ",".join(unreadable), "environment" if environment_mode else "env_file",
    )
    raise TransferError(
        f"The import cannot tell whether the new instance's NATS server is external, because it could not read these "
        f"variables in {where}: {', '.join(unreadable)}. Correct or unset them, then validate again. {_SEE_THE_GUIDE}"
    )


def _unreadable_nats_names(config: Any, environment: Mapping[str, str]) -> list[str]:
    """The NATS variables set in ``environment`` that the decision cannot read, each tried alone through it."""

    from assistant_gateway.instance_secrets import NATS_DECISION_ENV_NAMES, should_generate_internal_nats_credentials

    unreadable = []
    for name in NATS_DECISION_ENV_NAMES:
        try:
            if environment.get(name):
                should_generate_internal_nats_credentials(config, {name: environment[name]})
        except ValueError:
            unreadable.append(name)
    return unreadable


def _generated_reason(name: str, where: str) -> str:
    """Why the import refuses a carried value the new instance generates; for the NATS login, what to do.

    The NATS login travels when the old instance's own settings named an
    external server, but the address and the image's local-server switch do
    not, so a new instance whose settings name a local server refuses it until
    the operator names the server in ``where``, which the plan reads.
    """

    from assistant_gateway.instance_secrets import ENV_NATS_SERVERS, ENV_NATS_URL, NATS_CREDENTIAL_ENV_NAMES
    from docker.nats_service import LOCAL_NATS_ENABLED_ENV

    if name not in NATS_CREDENTIAL_ENV_NAMES:
        return "the new instance generates its own"
    return (
        f"the new instance's settings name a local NATS server, so it makes its own login. To keep this one, set "
        f"{ENV_NATS_URL} or {ENV_NATS_SERVERS} to the external server and {LOCAL_NATS_ENABLED_ENV}=false in "
        f"{where}, then validate again; or unselect this record. {_SEE_THE_GUIDE}"
    )


def _is_path_variable(name: str) -> bool:
    return name.endswith(("_DIR", "_PATH", "_DATABASE", "_FILE"))


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


__all__ = ["OperatorSecretV1", "OperatorSecretsSection", "SecretKind"]

"""The ``operator_secrets`` section: secrets the operator supplied, encrypted in the file.

What travels (owner's decision 2026-10-05: credentials move with the team):

- every secret variable in ``docker/secret_env_vars.yaml`` the operator
  supplied, from ``team.env`` or the environment (chat channel tokens, model
  key overrides, password-manager and cloud settings). Internally generated
  values (``internal_credentials.INTERNAL_ENV_KEYS``: signing keys, the broker
  token, NATS and database credentials, collector tokens) are not carried: the
  new instance generates its own, and its database is a new one;
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


def _instance_settings(environment_mode: bool) -> str:
    """Where the notices below say the decision came from."""

    where = "its environment" if environment_mode else "team.env"
    return f"this instance's own settings (instance_config.yaml and {where})"


def _nats_notice(
    source: ExportSource,
    config: Any,
    environment: Mapping[str, str],
    values: Mapping[str, str],
) -> list[str]:
    """At most one line for the operator about the NATS login, names only.

    It is given when this command's NATS variables would have decided
    otherwise (``_command_nats_sentence``) or when the export carries a NATS
    login, which the new instance uses only with the right switch
    (``_keep_server_sentence``). It starts with what happened to the login and
    why, so one login gets one line. Never a value, because an address may
    hold a user and password.
    """

    from assistant_gateway.instance_secrets import should_generate_internal_nats_credentials

    environment_mode = bool(source.config.launch.secrets_from_environment)
    # The same rule on the same inputs _operator_values used, so it adds no new way to fail.
    instance_generates = should_generate_internal_nats_credentials(config, environment)
    told = [
        sentence
        for sentence in (
            _command_nats_sentence(source, config, environment, instance_generates, values),
            _keep_server_sentence(environment, environment_mode, values),
        )
        if sentence
    ]
    if not told:
        return []
    login = _nats_login_outcome(instance_generates, values, environment_mode)
    return [" ".join([f"{login}.", *told])]


def _command_nats_sentence(
    source: ExportSource,
    config: Any,
    environment: Mapping[str, str],
    instance_generates: bool,
    values: Mapping[str, str],
) -> str:
    """What this command's own NATS variables would have decided, when that differs; otherwise "".

    Only with ``env_file`` secrets, where the export reads the instance's
    ``team.env`` and not this command's environment. The launcher lays the
    container's environment over ``team.env`` name by name
    (``docker/launch_headless.build_process_env``), so an export run with
    ``docker exec`` in a container that sets NATS_URL itself, or from a shell
    with a stray NATS_URL, would otherwise decide without saying so. The
    command's side is the instance's environment with the command's values for
    ``NATS_DECISION_ENV_NAMES`` laid over it the same way (an empty value
    replaces too, as in the launcher). The sentence names only the variables
    whose value differs from the instance's.

    A command value the rule cannot read (for example a malformed address) is
    reported and not used; it does not stop the export, because the instance
    never used it.
    """

    from assistant_gateway.instance_secrets import (
        NATS_DECISION_ENV_NAMES,
        should_generate_internal_nats_credentials,
    )

    if source.config.launch.secrets_from_environment:
        return ""
    command = {
        name: os.environ[name]
        for name in NATS_DECISION_ENV_NAMES
        if name in os.environ and os.environ[name] != environment.get(name, "")
    }
    if not command:
        return ""
    names = _join_names(list(command))
    unused = "it was" if len(command) == 1 else "they were"
    try:
        command_generates = should_generate_internal_nats_credentials(
            config, {**environment, **command}
        )
    except ValueError:
        _log_nats_notice(
            "team_transfer_nats_environment_unreadable", source, command, instance_generates, values
        )
        return f"This command's own {names} could not be read; {unused} not used."
    if command_generates == instance_generates:
        return ""
    _log_nats_notice(
        "team_transfer_nats_environment_differs", source, command, instance_generates, values
    )
    if not instance_generates:
        return f"This command's own {names} would mean a local server; {unused} not used."
    one = len(command) == 1
    return (
        f"This command's own {names} would mean an external server; {unused} not used. "
        f"If this instance really runs with {'that value' if one else 'those values'} (for example from "
        f"its Docker container's environment), add {'it' if one else 'them'} to the instance's team.env "
        "and export again, or set the server's address, NATS_USER, NATS_PASSWORD and "
        "TEAM_LOCAL_NATS_ENABLED=false on the new instance yourself. If "
        f"{'it is' if one else 'they are'} left over from another setup, nothing was lost."
    )


def _keep_server_sentence(
    environment: Mapping[str, str], environment_mode: bool, values: Mapping[str, str]
) -> str:
    """What the new instance needs to use the external server whose login travels; "" when none does.

    The login travels, but the image's local-server switch,
    TEAM_LOCAL_NATS_ENABLED, does not, nor do the NATS variables the
    instance's environment sets (``NATS_DECISION_ENV_NAMES``): only the address
    in ``instance_config.yaml`` travels, with the settings section. The switch
    is on unless set to false (``docker.nats_service.local_nats_enabled_from_env``),
    and then the image starts its own NATS server with the carried login, so
    the new instance would stop using the external server without a word. The
    import cannot say this when it accepts the login (it reports reasons only
    for records it refuses or lists), so the export does, on every export that
    carries the login. It is not logged: it is the same instruction each time,
    not a sign that something went wrong.
    """

    from assistant_gateway.instance_secrets import NATS_DECISION_ENV_NAMES

    if not _carried_nats_names(values):
        return ""
    named = [name for name in NATS_DECISION_ENV_NAMES if environment.get(name)]
    # Where the import's plan reads the address (``_new_instance_environment``);
    # the launcher reads the switch there too.
    place = (
        "in its team.env (or its environment, if it takes secrets from there) before importing; "
        "otherwise the image starts its own NATS server."
    )
    if not named:
        return (
            "The server's address travels in instance_config.yaml, but TEAM_LOCAL_NATS_ENABLED does not. "
            f"For the new instance to use that server, set TEAM_LOCAL_NATS_ENABLED=false {place}"
        )
    names = _join_names(named)
    one = len(named) == 1
    where = "environment" if environment_mode else "team.env"
    return (
        f"TEAM_LOCAL_NATS_ENABLED does not travel, and neither {'does' if one else 'do'} the {names} "
        f"in this instance's {where}. For the new instance to use that server, set {names} to the same "
        f"{'value' if one else 'values'} and TEAM_LOCAL_NATS_ENABLED=false {place}"
    )


def _log_nats_notice(
    event: str,
    source: ExportSource,
    command: Mapping[str, str],
    instance_generates: bool,
    values: Mapping[str, str],
) -> None:
    """Names only: the command's variables, which side the instance is on and what was carried."""

    logger.warning(
        "This command's NATS variables were not used; the instance's own settings decided. "
        "event=%s team_id=%s instance_name=%s decided_by=instance_settings command_names=%s "
        "instance_nats=%s carried_names=%s",
        event,
        source.team_id,
        source.instance_name,
        ",".join(command),
        "local" if instance_generates else "external",
        ",".join(_carried_nats_names(values)) or "none",
    )


def _nats_login_outcome(
    instance_generates: bool, values: Mapping[str, str], environment_mode: bool
) -> str:
    """Whether the NATS login travels, and why, in one sentence without its full stop."""

    settings = _instance_settings(environment_mode)
    if instance_generates:
        return (
            f"NATS_USER and NATS_PASSWORD were left out: {settings} say it uses a local "
            "NATS server, and the new instance makes its own login for that"
        )
    carried = _carried_nats_names(values)
    if not carried:
        return (
            f"No NATS_USER or NATS_PASSWORD was carried: {settings} say it uses an external "
            "NATS server, but they hold no such login"
        )
    verb = "was" if len(carried) == 1 else "were"
    return f"{_join_names(carried)} {verb} carried: {settings} say it uses an external NATS server"


def _carried_nats_names(values: Mapping[str, str]) -> list[str]:
    from assistant_gateway.instance_secrets import NATS_CREDENTIAL_ENV_NAMES

    return [name for name in NATS_CREDENTIAL_ENV_NAMES if name in values]


def _join_names(names: list[str]) -> str:
    """``A``, ``A and B``, ``A, B and C``."""

    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


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
            notices=_nats_notice(source, config, environment, values),
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
        env_path = _target_env_path(context)
        existing = _target_env(context)
        generated = _generated_names(
            config, _new_instance_environment(environment_mode, existing)
        )
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
                        _generated_reason(secret.name, environment_mode, env_path),
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


def _new_instance_environment(
    environment_mode: bool, env_file_values: Mapping[str, str]
) -> Mapping[str, str]:
    """The new instance's environment, from which the import decides which values it generates.

    Its env file, or, with secrets from the environment, the environment this
    import runs in (the new container for onboarding's Import), as the export
    reads the old instance's (``_environment``). In that mode the import
    writes nothing and the settings section generates nothing, so reading
    the process environment cannot change what the new instance generates.
    """

    return _process_environment() if environment_mode else env_file_values


def _generated_reason(name: str, environment_mode: bool, env_path: Path) -> str:
    """Why the import refuses a carried value the new instance generates, and for the NATS login what to do.

    The NATS user and password travel when the old instance's own settings
    named an external server, but the server's address and the image's
    local-server switch do not travel. So a new instance whose settings name
    a local server refuses them and makes its own login. If the old instance
    did use an external server, the operator names it where this import reads
    the decision (``plan``), then validates again: the carried login is then
    imported, or listed to set in the environment.
    """

    from assistant_gateway.instance_secrets import NATS_CREDENTIAL_ENV_NAMES

    if name not in NATS_CREDENTIAL_ENV_NAMES:
        return "the new instance generates its own"
    where = (
        "in the new instance's environment. This import reads NATS_URL and NATS_SERVERS from the "
        "environment it runs in (the new container for onboarding's Import, or for the host command your "
        "shell, where a value already set wins over --env), so set them there too"
        if environment_mode
        else f"in the new instance's env file, {env_path}"
    )
    return (
        "the new instance's settings name a local NATS server, and it makes its own login for that. "
        "If the old instance used an external NATS server, set NATS_URL or NATS_SERVERS to that server, "
        f"and TEAM_LOCAL_NATS_ENABLED=false so the image does not start its own server, {where}; then "
        "validate the selected records again. Otherwise unselect this record."
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

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
        should_generate_internal_nats_credentials,
    )

    names = set(INTERNAL_ENV_KEYS)
    if config is not None and not should_generate_internal_nats_credentials(
        config, environment or {}
    ):
        names.difference_update({"NATS_USER", "NATS_PASSWORD"})
    return frozenset(names)


def _environment(source: ExportSource) -> dict[str, str]:
    """Where the running instance takes its secrets from (``launch.secrets_source``).

    With ``env_file`` only its ``team.env`` is read: the command's own
    environment may hold the operator's personal variables, which the
    instance never used. With ``environment`` the process environment is the
    instance's (run the export inside its container).
    """

    from internal_credentials import parse_env_file

    if source.config.launch.secrets_from_environment:
        return {key: value for key, value in os.environ.items() if value}
    env_path = Path(source.config.launch.env_file_path or source.layout.team_env_path)
    return parse_env_file(env_path) if env_path.is_file() else {}


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
            for name, value in sorted(
                _operator_values(_environment(source), source.config).items()
            )
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
            elif (
                secret.kind is SecretKind.ENVIRONMENT
                and secret.name in _generated_names(config, existing)
            ):
                planned.append(
                    PlannedRecord(
                        self.name,
                        key,
                        RecordOutcome.REFUSED,
                        "the new instance generates its own",
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


def _target_env(context: ImportContext) -> dict[str, str]:
    """Values the operator already put in the new instance's team.env (they win over imported ones)."""

    from internal_credentials import parse_env_file

    config = context.scratch.get("settings.config")
    path = Path(
        getattr(getattr(config, "launch", None), "env_file_path", None)
        or context.layout.team_env_path
    )
    return parse_env_file(path) if path.is_file() else {}


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

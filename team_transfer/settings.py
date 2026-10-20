"""The ``settings`` section: the instance configuration choices a team made.

What it carries, and why:

- Only settings that differ from a freshly created configuration
  (``GatewayInstanceConfig.create`` with the same instance name, root and team,
  every other argument at its default). The saved file is a full dump with
  every default spelled out; carrying it whole would freeze the old version's
  defaults into the new one. Carrying only the choices lets the new version's
  defaults apply to everything the team never changed.
- One record per changed setting (a dotted path and its value), so the import
  can accept or refuse each setting on its own: a setting the new version
  removed is refused and listed, the rest still apply.
- Paths under the old instance root are stored relative to the root
  (``${INSTANCE_ROOT}/...``) and land under the new root. Other absolute paths
  (for example an SSH key) are carried as they are; the import flags any that
  do not exist on the new host.
- The configuration file holds no secrets (``_strip_persisted_instance_config_secrets``);
  database connection strings come from the new instance's environment.

The import builds the new configuration from this version's defaults plus the
carried choices, validates it with this version's loader, then runs the same
steps as onboarding (``bootstrap_instance`` and the internal credentials in
``team.env``), so an imported instance starts like an onboarded one.
"""

from __future__ import annotations

import copy
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Iterator

import yaml
from pydantic import BaseModel, Field, JsonValue, ValidationError

from team_transfer.format import TransferModel
from team_transfer.sections import (
    SECTION_UPGRADES,
    ExportSource,
    ImportContext,
    PlannedRecord,
    RecordOutcome,
    SectionExport,
    SectionFiles,
    SectionRecords,
)

if TYPE_CHECKING:
    from assistant_gateway.config import GatewayInstanceConfig

logger = logging.getLogger(__name__)

INSTANCE_ROOT_TOKEN: Final = "${INSTANCE_ROOT}"
# Describe which instance this is; the import sets them itself.
_DERIVED_SETTINGS: Final = frozenset(
    {
        ("schema_version",),
        ("instance_root",),
        ("instance_name",),
        ("gateway", "team_id"),
    }
)
_CONFIG_KEY: Final = "settings.config"
_MAX_VALIDATION_ROUNDS: Final = 50
_POSTGRES_BACKEND: Final = "postgres"
_STORAGE_SECTIONS: Final = (
    ("state", "storage"),
    ("gateway", "storage"),
    ("activity_collector", "storage"),
)


class SettingV1(TransferModel):
    """One setting the team changed from the default."""

    path: tuple[str, ...] = Field(min_length=1, max_length=32)
    value: JsonValue = None

    @property
    def dotted(self) -> str:
        return ".".join(self.path)


class SettingsSection:
    name: ClassVar[str] = "settings"
    # 3: gateway.users.new_people_use_team_model_keys left the configuration.
    # 4: agent.knowledge (the knowledge base's limits and blocked patterns) joined it.
    # 5: agent.max_concurrent_top_level_tasks was removed; values above 1 never had an effect.
    # 6: agent.request_conflict_checks (checks between a person's separate requests) joined it.
    schema_version: ClassVar[int] = 6
    encrypted: ClassVar[bool] = False
    after_start: ClassVar[bool] = False
    record_model: ClassVar[type[BaseModel]] = SettingV1

    # -- export ----------------------------------------------------------------

    def export(self, source: ExportSource, files: SectionFiles) -> SectionExport:
        from assistant_gateway.config import GatewayInstanceConfig

        saved = _read_yaml(source.layout.instance_config_path)
        baseline = GatewayInstanceConfig.create(
            instance_name=source.instance_name,
            instance_root=str(source.layout.root),
            team_id=source.team_id,
            timezone="UTC",
        ).to_yaml_dict()
        root = str(source.layout.root)
        records = [
            SettingV1(path=path, value=_relative_to_root(value, root)).model_dump(
                mode="json"
            )
            for path, value in changed_settings(saved, baseline)
            if path not in _DERIVED_SETTINGS
        ]
        return SectionExport(records=records)

    # -- import ----------------------------------------------------------------

    def plan(
        self, records: SectionRecords, context: ImportContext
    ) -> list[PlannedRecord]:
        from assistant_gateway.config import GatewayInstanceConfig

        manifest = context.reader.manifest
        settings = [SettingV1.model_validate(record) for record in records]
        root = str(context.layout.root)
        base = (
            context.scratch[_CONFIG_KEY].to_yaml_dict()
            if context.existing
            else GatewayInstanceConfig.create(
                instance_name=manifest.instance_name,
                instance_root=root,
                team_id=manifest.team_id,
                timezone="UTC",
            ).to_yaml_dict()
        )
        refused: dict[str, str] = {}
        destination = _destination_settings(context)
        candidate = _valid_candidate(base, settings, destination, refused, context)
        config = _validated(candidate, context.layout.instance_config_path)
        dropped, changed = _kept_by_loader(
            config,
            [setting for setting in settings if setting.path not in destination],
            refused,
            root,
        )
        refused.update(dropped)
        context.scratch["settings.postgres_dsn"] = _require_storage_connection(
            config, context.layout
        )
        context.scratch[_CONFIG_KEY] = config
        replaced = _replaced_by_destination(settings, destination, root)
        return [
            self._outcome(setting, refused, {**changed, **replaced})
            for setting in settings
        ]

    def _outcome(
        self,
        setting: SettingV1,
        refused: dict[str, str],
        changed: dict[str, str],
    ) -> PlannedRecord:
        if setting.dotted in refused:
            return PlannedRecord(
                self.name,
                setting.dotted,
                RecordOutcome.REFUSED,
                refused[setting.dotted],
            )
        if setting.dotted in changed:
            return PlannedRecord(
                self.name,
                setting.dotted,
                RecordOutcome.ATTENTION,
                changed[setting.dotted],
            )
        # Paths under the instance root are created by the import; only paths
        # outside it must already exist on this host.
        missing = [
            path for path in _absolute_paths(setting.value) if not os.path.exists(path)
        ]
        if missing:
            return PlannedRecord(
                self.name,
                setting.dotted,
                RecordOutcome.ATTENTION,
                "points outside the instance to a path this host does not have: "
                + ", ".join(missing),
            )
        return PlannedRecord(self.name, setting.dotted, RecordOutcome.IMPORT)

    def apply(
        self, records: SectionRecords, plan: list[PlannedRecord], context: ImportContext
    ) -> None:
        from assistant_gateway.bootstrap import bootstrap_instance
        from assistant_gateway.instance_secrets import (
            ensure_instance_sealbox_material,
            ensure_internal_credential_env_file,
        )

        config: GatewayInstanceConfig = context.scratch[_CONFIG_KEY]
        if context.existing:
            # Planning validates a new model; preserve the destination revision
            # from which its unchanged fields came, rather than treating it as new.
            config.save(
                context.layout.instance_config_path,
                expected_revision=context.scratch["settings.original_config"].source_revision,
            )
            _refresh_unchanged_model_defaults(context, config)
        else:
            bootstrap_instance(config)
        if not config.launch.secrets_from_environment:
            ensure_internal_credential_env_file(
                config,
                existing_postgres_dsn=context.scratch.get("settings.postgres_dsn"),
            )
        ensure_instance_sealbox_material(config)
        logger.info(
            "Team settings imported. event=team_transfer_settings_imported root=%s settings=%s refused=%s",
            context.layout.root,
            len(plan),
            sum(1 for record in plan if record.outcome is RecordOutcome.REFUSED),
        )



# -- comparing and setting values ----------------------------------------------------


def _destination_settings(context: ImportContext) -> dict[tuple[str, ...], Any]:
    """Settings the person importing chose for the new instance (``DestinationChoices``).

    They win over the carried settings at the same paths. A first import
    only: a later batch cannot change where internal keys are kept.
    """

    chosen = context.destination.secrets_source
    if context.existing or not chosen:
        return {}
    from runtime_settings.secrets_source import (
        SECRETS_SOURCE_ENVIRONMENT,
        normalize_secrets_source,
    )

    source = normalize_secrets_source(chosen)
    values: dict[tuple[str, ...], Any] = {("launch", "secrets_source"): source}
    if source == SECRETS_SOURCE_ENVIRONMENT:
        # Secrets then come only from the environment (LaunchSettings refuses both).
        values[("launch", "env_file_path")] = None
    return values


def _valid_candidate(
    base: dict[str, Any],
    settings: list[SettingV1],
    destination: dict[tuple[str, ...], Any],
    refused: dict[str, str],
    context: ImportContext,
) -> dict[str, Any]:
    """This version's configuration with the carried settings it accepts; ``refused`` gains the rest.

    Each round applies every setting not yet refused and validates with this
    version's loader; the settings an error names are refused, with its
    reason, and the rest are tried again. The destination's own choices win
    over carried settings at the same paths.
    """

    candidate = base
    for _round in range(_MAX_VALIDATION_ROUNDS):
        candidate = _applied(base, settings, destination, refused, context)
        failure = _validation_failure(candidate, context.layout.instance_config_path)
        if failure is None:
            break
        blamed = _blame(failure, settings, refused)
        if not blamed:
            from team_transfer.errors import TransferError

            raise TransferError(
                "The carried settings cannot form a valid configuration for this version, and the problem is "
                f"not in one setting: {failure}"
            )
        refused.update(blamed)
    return candidate


def _applied(
    base: dict[str, Any],
    settings: list[SettingV1],
    destination: dict[tuple[str, ...], Any],
    refused: dict[str, str],
    context: ImportContext,
) -> dict[str, Any]:
    """``base`` with the destination's choices and every carried setting not refused."""

    root = str(context.layout.root)
    candidate = copy.deepcopy(base)
    for path, value in destination.items():
        _set_path(candidate, path, value)
    for setting in settings:
        if setting.dotted in refused or setting.path in destination:
            continue
        problem = _later_storage_problem(setting, base, context) or _set_path(
            candidate, setting.path, _under_root(setting.value, root)
        )
        if problem:
            refused[setting.dotted] = problem
    return candidate


def _replaced_by_destination(
    settings: list[SettingV1], destination: dict[tuple[str, ...], Any], root: str
) -> dict[str, str]:
    """Carried settings the destination's choices replaced, with the reason shown for each."""

    return {
        setting.dotted: "replaced by the secret source chosen for this instance: "
        + str(destination[("launch", "secrets_source")])
        for setting in settings
        if setting.path in destination
        and _under_root(setting.value, root) != destination[setting.path]
    }


def _later_storage_problem(setting, base, context) -> str:
    if not context.existing:
        return ""
    protected = setting.path[:2] in {
        ("state", "storage"),
        ("gateway", "storage"),
    } or setting.path in {
        ("launch", "env_file_path"),
        ("launch", "secrets_source"),
        ("gateway", "named_agent_type"),
    }
    found, current = _lookup(base, setting.path)
    if (
        protected
        and found
        and current != _under_root(setting.value, str(context.layout.root))
    ):
        return "choose database locations and the secret source in the first batch; a later batch cannot change where existing records or internal keys are stored"
    return ""


def _refresh_unchanged_model_defaults(context, config):
    """A later model setting must not lose to unchanged bootstrap overrides."""
    from assistant_gateway.bootstrap import render_bootstrap_control_file
    from runtime_settings.instance_layout import LLM_OVERRIDES_FILENAME

    path = context.layout.llm_overrides_path
    original = context.scratch["settings.original_config"]
    old = render_bootstrap_control_file(original, LLM_OVERRIDES_FILENAME)
    if path.is_file() and path.read_text(encoding="utf-8") == old:
        path.write_text(
            render_bootstrap_control_file(config, LLM_OVERRIDES_FILENAME),
            encoding="utf-8",
        )


def changed_settings(
    saved: dict[str, Any], baseline: dict[str, Any], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Leaves of ``saved`` that differ from ``baseline``; lists count as one value."""

    for key, value in saved.items():
        path = (*prefix, str(key))
        if key not in baseline:
            yield path, value
        elif isinstance(value, dict) and isinstance(baseline[key], dict):
            yield from changed_settings(value, baseline[key], path)
        elif value != baseline[key]:
            yield path, value


def _set_path(document: dict[str, Any], path: tuple[str, ...], value: Any) -> str:
    """Set ``value`` at ``path``; a problem message when the path is not a setting group here."""

    node: Any = document
    for index, key in enumerate(path[:-1]):
        child = node.get(key) if isinstance(node, dict) else None
        if child is None:
            child = {}
            node[key] = child
        if not isinstance(child, dict):
            return f"this version has no setting group {'.'.join(path[: index + 1])}"
        node = child
    node[path[-1]] = value
    return ""


def _relative_to_root(value: Any, root: str) -> Any:
    if isinstance(value, str) and (
        value == root or value.startswith(root.rstrip("/") + "/")
    ):
        return INSTANCE_ROOT_TOKEN + value[len(root.rstrip("/")) :]
    if isinstance(value, list):
        return [_relative_to_root(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _relative_to_root(item, root) for key, item in value.items()}
    return value


def _under_root(value: Any, root: str) -> Any:
    if isinstance(value, str) and value.startswith(INSTANCE_ROOT_TOKEN):
        return root.rstrip("/") + value[len(INSTANCE_ROOT_TOKEN) :]
    if isinstance(value, list):
        return [_under_root(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _under_root(item, root) for key, item in value.items()}
    return value


def _absolute_paths(value: Any) -> Iterator[str]:
    if isinstance(value, str) and value.startswith("/"):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _absolute_paths(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _absolute_paths(item)


# -- validating with this version's loader ---------------------------------------------


def _validated(document: dict[str, Any], config_path: Path) -> "GatewayInstanceConfig":
    from assistant_gateway.config import (
        GatewayInstanceConfig,
        _migrate_instance_config_dict,
    )

    migrated, _steps = _migrate_instance_config_dict(
        copy.deepcopy(document), source_path=config_path
    )
    config = GatewayInstanceConfig.model_validate(migrated)
    config._config_path = config_path
    return config


def _validation_failure(
    document: dict[str, Any], config_path: Path
) -> ValidationError | ValueError | None:
    try:
        _validated(document, config_path)
    except (ValidationError, ValueError) as exc:
        return exc
    return None


def _blame(
    failure: Exception, settings: list[SettingV1], refused: dict[str, str]
) -> dict[str, str]:
    """The carried settings an error names, with the reason, so they can be left out and the rest retried."""

    candidates = [setting for setting in settings if setting.dotted not in refused]
    if isinstance(failure, ValidationError):
        return _blame_locations(failure, candidates)
    message = str(failure)
    return {
        setting.dotted: f"this version does not accept it: {message}"
        for setting in candidates
        if setting.dotted in message
    }


def _blame_locations(
    failure: ValidationError, candidates: list[SettingV1]
) -> dict[str, str]:
    blamed: dict[str, str] = {}
    for error in failure.errors():
        location = tuple(str(part) for part in error.get("loc", ()))
        reason = f"this version does not accept it: {error.get('msg', 'invalid')}"
        blamed.update(
            {
                setting.dotted: reason
                for setting in candidates
                if _overlaps(setting.path, location)
            }
        )
    return blamed


def _kept_by_loader(
    config: "GatewayInstanceConfig",
    settings: list[SettingV1],
    refused: dict[str, str],
    root: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Settings the loader did not keep, and settings it kept with a different value.

    Parts of the configuration ignore unknown keys, so without this check a
    setting would be reported as imported while it silently disappeared.
    """

    saved = config.to_yaml_dict()
    dropped: dict[str, str] = {}
    changed: dict[str, str] = {}
    for setting in settings:
        if setting.dotted in refused:
            continue
        found, value = _lookup(saved, setting.path)
        if not found:
            dropped[setting.dotted] = "this version has no such setting"
        elif value != _under_root(setting.value, root):
            changed[setting.dotted] = f"this version stored it as {value!r}"
    return dropped, changed


def _lookup(document: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    node: Any = document
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def _overlaps(path: tuple[str, ...], location: tuple[str, ...]) -> bool:
    shorter = min(len(path), len(location))
    return shorter > 0 and path[:shorter] == location[:shorter]


def _require_storage_connection(
    config: "GatewayInstanceConfig", layout: Any
) -> str | None:
    """A PostgreSQL instance needs its new, empty database's connection string before the import."""

    from internal_credentials import parse_env_file
    from env_secrets import get_env_with_file_fallback
    from team_transfer.errors import TransferError

    backends = {
        str(
            getattr(getattr(getattr(config, section, None), part, None), "backend", "")
            or ""
        )
        for section, part in _STORAGE_SECTIONS
    }
    if _POSTGRES_BACKEND not in backends:
        return None
    path = Path(config.launch.env_file_path or layout.team_env_path)
    env = (
        {}
        if config.launch.secrets_from_environment
        else parse_env_file(path)
        if path.is_file()
        else {}
    )
    env.update(
        {
            name: os.environ[name]
            for name in ("POSTGRES_DSN", "POSTGRES_DSN_FILE")
            if os.environ.get(name)
        }
    )
    dsn = get_env_with_file_fallback("POSTGRES_DSN", env=env)
    if dsn:
        return dsn
    raise TransferError(
        "This team used PostgreSQL. Create a new, empty database, put its connection string in POSTGRES_DSN "
        f"(in {layout.team_env_path} or the environment), and import again."
    )


@contextmanager
def storage_environment(context: ImportContext):
    """All section writers use the exact new database that the plan checked.

    Repository factories resolve storage from the process environment. The
    operator command temporarily supplies the planned DSN and restores both
    forms afterwards; bootstrap also saves this DSN for the next launch.
    """
    dsn = context.scratch.get("settings.postgres_dsn")
    if not dsn:
        yield
        return
    previous = {
        name: os.environ.get(name) for name in ("POSTGRES_DSN", "POSTGRES_DSN_FILE")
    }
    os.environ["POSTGRES_DSN"] = dsn
    os.environ.pop("POSTGRES_DSN_FILE", None)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def require_empty_postgres(context: ImportContext) -> None:
    """A fresh root must not point at a database holding another instance."""
    if context.existing or not context.scratch.get("settings.postgres_dsn"):
        return
    config = planned_config(context)
    # Resolve under the writer's environment, including the bundle's address.
    # Activity storage may use a separate server; inspect each distinct target.
    with storage_environment(context):
        stores = (
            config.manager_storage_settings,
            config.state_storage_settings,
            config.resolve_activity_collector_storage_settings(env=os.environ),
        )
        for dsn in dict.fromkeys(store.postgres_dsn for store in stores if store.postgres_dsn):
            _require_empty_postgres_database(dsn)


def _require_empty_postgres_database(dsn: str) -> None:
    from storage.postgres_connection import connect_postgres
    from storage.read_only import read_only_repositories
    from team_transfer.errors import TransferError

    try:
        with read_only_repositories(), connect_postgres(dsn) as connection:
            present = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_tables "
                "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
                "AND schemaname NOT LIKE 'pg_toast%')"
            ).fetchone()[0]
    except Exception as exc:
        logger.error(
            "Cannot inspect destination database. event=team_transfer_database_check_failed error_type=%s",
            type(exc).__name__,
        )
        raise TransferError(
            f"The destination PostgreSQL database could not be checked ({type(exc).__name__}). Check its connection settings before importing."
        ) from exc
    if present:
        raise TransferError(
            "The destination PostgreSQL database already holds tables. Create an empty database for the first import; no records were written."
        )


def planned_config(context: ImportContext) -> "GatewayInstanceConfig":
    """The new instance's configuration as this section planned it.

    Later sections read storage locations and settings from it while planning,
    before ``apply`` has written ``instance_config.yaml``, and while applying.
    """

    from assistant_gateway.config import GatewayInstanceConfig

    config = context.scratch.get(_CONFIG_KEY)
    if not isinstance(config, GatewayInstanceConfig):
        from team_transfer.errors import TransferError

        raise TransferError(
            "The settings section must be part of the import and come before this one."
        )
    return config


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a configuration mapping.")
    return data


__all__ = [
    "INSTANCE_ROOT_TOKEN",
    "SettingV1",
    "SettingsSection",
    "changed_settings",
    "planned_config",
]


# V6 adds agent.request_conflict_checks. V5 records (v0.9.4 to v0.9.6) keep the
# same shape and carry only values that differ from the defaults, so the step
# passes them on unchanged and the new setting keeps its default (on).
SECTION_UPGRADES[(SettingsSection.name, 5)] = lambda records: records

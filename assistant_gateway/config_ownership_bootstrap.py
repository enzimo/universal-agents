"""Configuration-edge bootstrap and legacy ownership reconciliation."""

from __future__ import annotations

import logging

from assistant_gateway.access_setup import MembershipRoles, apply_configured_starter_profile
from assistant_gateway.application.ports import GatewayClock
from assistant_gateway.bootstrap import seed_default_gateway_user
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.people_directory_sync import build_people_directory_sync
from assistant_gateway.ports.repositories import IdentityRepository
from assistant_gateway.store import GatewayStateStore
from storage.config import StorageSettings
from storage.multi_user_owner_migration import (
    OwnerMigrationReport,
    migrate_legacy_null_owner_rows,
)


class ConfigGatewayOwnershipBootstrap:
    """Own startup identity seeding and bounded ownership reconciliation."""

    def __init__(
        self,
        *,
        config: GatewayInstanceConfig,
        state_store: GatewayStateStore,
        storage_settings: StorageSettings,
        identity_repository: IdentityRepository,
        clock: GatewayClock,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._storage_settings = storage_settings
        self._identity_repository = identity_repository
        self._logger = logger or logging.getLogger(__name__)
        # The access store's copy of people's standing, which the manager's
        # scheduler reads at every run (assistant_gateway.people_directory_sync).
        # The identity service records each change through it.
        self.people_directory = build_people_directory_sync(
            config, identity=identity_repository, clock=clock, logger=self._logger
        )

    def prepare_people(self) -> None:
        """Seed the instance's user, then copy everyone's standing to the access store.

        Runs once when the gateway is built, so the copy is complete before
        ``UserGateway.connect`` (or a manager-available transition) sends any
        maintenance schedule: scheduled runs then see the first user and every
        change made while the gateway was stopped (onboarding,
        ``assistant_cli``). A failed copy is logged and does not stop the
        gateway.
        """

        self.seed_default_user()
        self.people_directory.sync_all()

    def seed_default_user(self) -> None:
        """Seed the instance's user, then give the team lead its starter profile.

        The profile is applied as that user, so it follows the seeding; it is a
        no-op with authorization off (``assistant_gateway.access_setup``).
        """

        seed_default_gateway_user(self._config, state_store=self._state_store)
        apply_configured_starter_profile(self._config, MembershipRoles(self._identity_repository))

    def reconcile_legacy_ownership(self) -> None:
        canonical_user_id = str(self._config.user.canonical_user_id or "").strip()
        if not canonical_user_id or not self._config.gateway.users.enabled:
            return
        try:
            report = migrate_legacy_null_owner_rows(
                owner_canonical_user_id=canonical_user_id,
                gateway_storage_settings=self._storage_settings,
            )
        except Exception as exc:
            self._logger.warning(
                "Legacy gateway owner migration failed. "
                "event=legacy_gateway_owner_auto_migration_failed instance_name=%s "
                "owner_canonical_user_id=%s error_type=%s",
                self._config.instance_name,
                canonical_user_id,
                type(exc).__name__,
            )
            return
        self._record_report(report)

    def _record_report(self, report: OwnerMigrationReport) -> None:
        if report.updated_rows <= 0:
            return
        self._logger.info(
            "Reconciled legacy private-row ownership for bootstrap admin. "
            "event=legacy_owner_auto_migration_complete "
            "owner_canonical_user_id=%s updated_rows=%d",
            report.owner_canonical_user_id,
            report.updated_rows,
        )
        try:
            self._identity_repository.record_gateway_user_event(
                event_type="legacy_owner_auto_migration",
                canonical_user_id=report.owner_canonical_user_id,
                payload=report.to_dict(),
            )
        except Exception:
            self._logger.debug(
                "Could not record legacy owner migration event.",
                exc_info=True,
            )

"""Composition helpers for the interactive command application."""

from __future__ import annotations

import os
from assistant_gateway.access_commands import GatewayAccessCommands
from assistant_gateway.account_sharing import GatewayAccountSharing
from assistant_gateway.application.account_links import GatewayAccountLinks
from assistant_gateway.application.broker_sign_in import GatewayBrokerSignIns
from assistant_gateway.application.commands import (
    AccessCommandHandler,
    AccountLinkCommandHandler,
    AccountSharingCommandHandler,
    AuthenticationCommandHandler,
    GatewayAccessCommandPort,
    GatewayAccountSharingPort,
    IdentityCommandHandler,
    MemoryTopicCommandHandler,
)
from assistant_gateway.application.link_results import GatewayLinkResultWatches
from assistant_gateway.application.command_operations import (
    GatewayCommandOperationsApplication,
)
from assistant_gateway.application.interactive_commands import (
    GatewayCommandService,
    GatewayCommandSettings,
)
from assistant_gateway.application.ports import (
    GatewayPrivateResourceAuthorizer,
    GatewaySecureBroker,
)
from assistant_gateway.application.manager_control_command_service import (
    GatewayMCPControls,
    GatewayManagerControlCommandService,
)
from assistant_gateway.application.operational_control import (
    GatewayStopControl,
)
from assistant_gateway.application.schedule_management import GatewayScheduleApplication
from assistant_gateway.application.message_submission_command_service import (
    GatewayMessageSubmissionCommandService,
)
from assistant_gateway.application.session_command_service import GatewaySessionCommandService
from assistant_gateway.config import GatewayInstanceConfig
from runtime_settings.network import compose_public_base_url
from assistant_gateway.http_secure_broker import HttpGatewaySecureBroker
from assistant_gateway.model_key_endpoints import configured_model_endpoints
from assistant_gateway.ports.repositories import RequestRepository, SessionRepository
from assistant_gateway.runtime_ports import SystemGatewayClock
from default_values import INTERACTIVE_APP_DEFAULTS, NETWORK_DEFAULTS


def resolve_interactive_command_docs_base_url(config: GatewayInstanceConfig) -> str:
    """Resolve the browser-facing documentation origin at composition time."""

    environment_base_url = os.getenv("INTERNAL_WEBSITE_BASE_URL", "").strip()
    if environment_base_url:
        return environment_base_url.rstrip("/")
    if config.interactive_apps.internal_website.enabled:
        return str(config.internal_website_base_url).rstrip("/")
    return str(
        compose_public_base_url(
            NETWORK_DEFAULTS.public_origin.value,
            INTERACTIVE_APP_DEFAULTS.internal_website_public_port.value,
        )
    ).rstrip("/")


def build_gateway_command_application(
    *,
    config: GatewayInstanceConfig,
    operations: GatewayCommandOperationsApplication,
    session_repository: SessionRepository,
    request_repository: RequestRepository,
    private_authorizer: GatewayPrivateResourceAuthorizer,
    operational_controls: GatewayStopControl,
    link_results: GatewayLinkResultWatches,
    schedule_controls: GatewayScheduleApplication | None = None,
    mcp_controls: GatewayMCPControls | None = None,
    secure_broker: GatewaySecureBroker | None = None,
    account_links: GatewayAccountLinks | None = None,
    access: GatewayAccessCommandPort | None = None,
    account_sharing: GatewayAccountSharingPort | None = None,
) -> GatewayCommandService:
    """Compose the command service from explicit application and outer ports.

    The gateway passes its own broker client, account link opener and access
    commands (on the Inbox's access service); test and adapter compositions
    that omit them, and ``/accounts`` (``account_sharing``), get ones built
    from ``config``. ``link_results`` watches
    the pages ``/link`` opens and posts how they ended; it needs the
    gateway's delivery output, so every composition passes one.
    """

    broker = secure_broker or HttpGatewaySecureBroker.from_gateway_config(config)
    account_link_opener = account_links or GatewayAccountLinks(
        broker=broker,
        team_id=config.manager.team_id,
        clock=SystemGatewayClock(),
    )
    settings = GatewayCommandSettings(
        docs_base_url=resolve_interactive_command_docs_base_url(config),
    )
    return GatewayCommandService(
        settings=settings,
        authentication_commands=AuthenticationCommandHandler(
            identity=operations,
            secure_broker=broker,
            team_id=config.manager.team_id,
            link_results=link_results,
            model_endpoints=lambda: configured_model_endpoints(config),
        ),
        account_link_commands=AccountLinkCommandHandler(
            identity=operations,
            account_links=account_link_opener,
            sign_ins=GatewayBrokerSignIns(
                secure_broker=broker, team_id=config.manager.team_id
            ),
            link_results=link_results,
        ),
        account_sharing_commands=AccountSharingCommandHandler(
            identity=operations,
            sharing=account_sharing or GatewayAccountSharing.from_config(config),
        ),
        access_commands=AccessCommandHandler(
            identity=operations,
            access=access
            or GatewayAccessCommands.from_config(
                config,
                roles_of=lambda person_id: operations.team_roles_for_person(person_id),
            ),
        ),
        identity_commands=IdentityCommandHandler(operations),
        memory_topic_commands=MemoryTopicCommandHandler(operations),
        message_submission_commands=GatewayMessageSubmissionCommandService(operations),
        manager_control_commands=GatewayManagerControlCommandService(
            operational_controls,
            schedules=schedule_controls,
            mcp_controls=mcp_controls,
        ),
        session_commands=GatewaySessionCommandService(
            sessions=session_repository,
            requests=request_repository,
            actors=operations,
            lifecycle=operations,
            authorizer=private_authorizer,
            team_id=config.manager.team_id,
        ),
    )

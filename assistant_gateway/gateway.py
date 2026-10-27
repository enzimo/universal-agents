"""NATS-backed user gateway for user interaction and manager-owned scheduling."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from access_control.identity import ISSUER_GATEWAY, ContextSigner
from assistant_gateway.access_commands import GatewayAccessCommands
from assistant_gateway.account_sharing import GatewayAccountSharing
from assistant_gateway.status_page import StatusPage
from assistant_gateway.file_retention import GatewayFileRetention
from assistant_gateway.inbound_attachments import resolve_inbound_attachment_cache_dir
from assistant_gateway.storage_capacity import StorageCapacity, build_storage_capacity
from assistant_gateway.storage_usage import StorageUsageNotices, build_storage_usage_notices
from assistant_gateway.upload_retention import UploadRetention
from assistant_gateway.application.knowledge_pages import GatewayKnowledgePages
from assistant_gateway.access_inbox import (
    GatewayAccessInbox,
    build_gateway_access_inbox,
)
from assistant_gateway.access_setup import MembershipRoles, PersonChangeCheck
from assistant_gateway.access_workflows_page import create_access_workflow_page
from assistant_gateway.application.access_review_service import (
    GatewayAccessReviewControls,
)
from assistant_gateway.application.account_links import GatewayAccountLinks
from assistant_gateway.application.activity_notices import (
    GatewayActivityNoticeCoordinator,
    GatewayActivityNoticeRoutingApplication,
)
from assistant_gateway.application.attachment_replication import (
    GatewayAttachmentReplicationApplication,
    GatewayAttachmentReplicationSettings,
)
from assistant_gateway.application.automated_submission import (
    GatewayAutomatedSubmissionApplication,
    GatewayAutomatedSubmissionSettings,
)
from assistant_gateway.application.clarification import (
    GatewayClarificationApplication,
    GatewayClarificationCore,
    GatewayClarificationSettings,
)
from assistant_gateway.application.command_operations import (
    GatewayCommandOperationsApplication,
    GatewayCommandOperationsSettings,
)
from assistant_gateway.application.credential_handoff import ToolSetupHandoffs
from assistant_gateway.application.credential_workflow import (
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL,
    REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT,
    GatewayCredentialWorkflow,
    GatewayCredentialWorkflowSettings,
)
from assistant_gateway.application.deferred_delivery import (
    GatewayDeferredDeliveryApplication,
    GatewayDeferredDeliverySettings,
)
from assistant_gateway.application.delivery_presentation import (
    GatewayDeliveryPresentation,
)
from assistant_gateway.application.delivery_projection import (
    GatewayDeliveryProjection,
    GatewayDeliveryProjectionSettings,
)
from assistant_gateway.application.delivery_service import GatewayDeliveryService
from assistant_gateway.application.gateway_application import GatewayDeliveryEvent
from assistant_gateway.application.identity_service import (
    GatewayIdentityService,
    GatewayIdentitySettings,
)
from assistant_gateway.application.interactive_submission import (
    GatewayInteractiveSubmissionService,
    GatewayInteractiveSubmissionSettings,
)
from assistant_gateway.application.local_responses import (
    GatewayLocalResponseFactory,
    GatewayLocalResponseSettings,
)
from assistant_gateway.application.maintenance_schedules import (
    GatewayHeartbeatScheduleSettings,
    GatewayInternalImprovementScheduleSettings,
    GatewayMaintenanceScheduleApplication,
    GatewayMaintenanceScheduleSettings,
    GatewayPersonSystemScheduleDefinition,
    GatewaySpecialistScheduleSettings,
    GatewaySystemScheduleDefinition,
)
from assistant_gateway.application.manager_client import (
    GatewayManagerClient,
    GatewayManagerClientSettings,
)
from assistant_gateway.application.manager_commands import (
    GatewayManagerCommandService,
    GatewayManagerCommandSettings,
)
from assistant_gateway.application.manager_transitions import (
    GatewayManagerTransitionApplication,
    GatewayManagerTransitionSettings,
)
from assistant_gateway.application.operational_control import (
    GatewayOperationalControlApplication,
    GatewayOperationalControlSettings,
)
from assistant_gateway.application.person_maintenance import (
    GatewayPersonMaintenanceSessions,
    GatewayPersonMaintenanceSettings,
)
from assistant_gateway.application.ports import (
    GatewayActivitySource,
    GatewayClock,
    GatewayEventOutput,
    GatewayIdGenerator,
    GatewayManagerRuntime,
    GatewayManagerTransport,
    GatewayPrivateResourceAuthorizer,
    GatewaySecureBroker,
)
from assistant_gateway.application.proactive_delivery import (
    GatewayProactiveDeliveryApplication,
    GatewayProactiveDeliverySettings,
)
from assistant_gateway.application.request_context import (
    GatewayRequestContextService,
    GatewayRequestContextSettings,
)
from assistant_gateway.application.pending_questions import GatewayPendingQuestions
from custom_messages.contracts.control_v1 import AgentStatus
from assistant_gateway.application.request_service import (
    GatewayRequestService,
)
from assistant_gateway.application.response_coordinator import (
    GatewayResponseCoordinator,
    GatewayResponseCoordinatorSettings,
)
from assistant_gateway.application.response_models import GatewayResponse
from assistant_gateway.application.route_context import SignedInPerson
from assistant_gateway.application.runtime_application import (
    GatewayRuntimeApplicationSettings,
)
from assistant_gateway.application.schedule_management import (
    GatewayScheduleApplication,
    GatewayScheduleSettings,
)
from assistant_gateway.application.session_service import (
    GatewaySessionService,
)
from assistant_gateway.application.task_progress import (
    GatewayTaskProgressService,
    GatewayTaskProgressSettings,
)
from assistant_gateway.application.thread_guidance import (
    GatewayThreadGuidance,
    GatewayThreadGuidanceSettings,
)
from assistant_gateway.application.transcript_service import (
    GatewayTranscriptService,
    TranscriptRecordingGatewayEventOutput,
)
from assistant_gateway.application.unsolicited_delivery import (
    GatewayInboundMessageApplication,
    GatewayUnsolicitedDeliveryApplication,
    GatewayUnsolicitedDeliverySettings,
)
from assistant_gateway.application.website_runtime_application import (
    WebsiteRuntimeApplication,
)
from assistant_gateway.application.website_services import (
    WebsiteIdentityApplication,
    WebsiteIdentitySettings,
    WebsiteOperationsApplication,
    WebsiteSessionApplication,
    WebsiteTranscriptApplication,
)
from assistant_gateway.application.workspace_browser import WebsiteWorkspaceBrowser
from assistant_gateway.command_composition import build_gateway_command_application
from assistant_gateway.config import (
    DEFAULT_AVAILABLE_MESSAGE_TEMPLATE,
    DEFAULT_UNAVAILABLE_MESSAGE_TEMPLATE,
    GatewayInstanceConfig,
)
from assistant_gateway.config_delivery_attachments import (
    ConfigGatewayDeliveryAttachmentResolver,
)
from assistant_gateway.config_delivery_preferences import (
    ConfigGatewayDeliveryPreferences,
)
from assistant_gateway.config_manager_route import ConfigGatewayManagerRouteWriter
from assistant_gateway.config_ownership_bootstrap import (
    ConfigGatewayOwnershipBootstrap,
)
from assistant_gateway.http_activity_source import HttpGatewayActivitySource
from assistant_gateway.http_secure_broker import HttpGatewaySecureBroker
from assistant_gateway.onboarding_account_keys import OnboardingAccountKeyHandover
from assistant_gateway.installed_procedures import GatewayInstalledProcedures
from assistant_gateway.mcp_controls import GatewayMCPControls
from assistant_gateway.nats_manager_runtime import NatsGatewayManagerRuntime
from assistant_gateway.person_departure import build_gateway_person_lifecycle
from assistant_gateway.pinned_memory_commands import RuntimePinnedMemoryCommandParser
from assistant_gateway.ports.repositories import (
    DeliveryRepository,
    IdentityRepository,
    RequestRepository,
    SessionRepository,
    TranscriptRepository,
)
from assistant_gateway.runtime_ports import (
    CallbackGatewayEventOutput,
    MembershipRoleIdentityAuthorizer,
    OwnershipPrivateResourceAuthorizer,
    SystemGatewayClock,
    SystemGatewayIdentitySecrets,
    UuidGatewayIdGenerator,
)
from assistant_gateway.store import (
    GatewayStateStore,
)
from assistant_gateway.team_presence import (
    GatewayTeamPresence,
    configured_agent_classes,
)
from assistant_gateway.website_auth import EnvironmentGatewayBootstrapTokenSource
from custom_agents.internal_improvement_runtime import (
    configured_review_interval_seconds,
    internal_improvement_schedule_id,
    internal_improvement_schedule_name,
)
from custom_agents.memory_review_runtime import (
    memory_review_settings,
    review_enabled,
)
from custom_messages.contracts.runtime_v1 import (
    DELIVERY_CHANNEL_DISCORD,
    DELIVERY_CHANNEL_PHOTON,
    DELIVERY_CHANNEL_SIGNAL,
    DELIVERY_CHANNEL_TELEGRAM,
    DELIVERY_CHANNEL_WHATSAPP,
)
from custom_messages.contracts.task_v1 import TaskQuery
from custom_messages.enums import (
    SystemScheduleKind,
)
from custom_messages.system_schedules import (
    COREMEM_AUDIT_SCHEDULE_NAME_PREFIX,
    KNOWLEDGE_CURATION_TEAM_SCHEDULE_NAME_PREFIX,
)
from id_generation import (
    HEARTBEAT_SCHEDULE_ID_PREFIX,
    HEARTBEAT_SOURCE_ID_PREFIX,
    KNOWLEDGE_CURATION_PERSON_ID_PREFIX,
    KNOWLEDGE_CURATION_TEAM_ID_PREFIX,
    MEMORY_REVIEW_CORE_ID_PREFIX,
    MEMORY_REVIEW_STALE_ID_PREFIX,
    MEMORY_REVIEW_USER_ID_PREFIX,
    operational_id,
)
from reply_routing_trace import (
    is_reply_routing_trace_enabled,
)
from storage.config import StorageSettings

logger = logging.getLogger(__name__)
__all__ = [
    "GatewayResponse",
    "REQUEST_STATUS_WAITING_FOR_CREDENTIAL",
    "REQUEST_STATUS_WAITING_FOR_CREDENTIAL_RESULT",
    "UserGateway",
]
MANAGER_LIVENESS_POLL_SECONDS = 5.0
MANAGER_PRESENCE_STALE_SECONDS = 30.0
MANAGER_RECOVERY_DISCOVERY_TIMEOUT_SECONDS = 2.0
MANAGER_CONTROL_TIMEOUT_SECONDS = 15.0
TASK_PROGRESS_STATUS_TIMEOUT_SECONDS = 3.0
ACTIVITY_NOTICE_POLL_SECONDS = 2.0


OutputSink = Callable[[str], Optional[Awaitable[None]] | None]
QueryHandler = Callable[[TaskQuery], Awaitable[str] | str]
EventSink = Callable[[GatewayDeliveryEvent], Optional[Awaitable[None]] | None]


def _gateway_context_signer() -> ContextSigner | None:
    """The gateway's task-context signing key from team.env, if configured.

    Without it, tasks carry no signed context and every enforcement point that
    needs one refuses (access_control.identity).
    """

    signer = ContextSigner.from_env(ISSUER_GATEWAY)
    if signer is None:
        logger.warning(
            "No gateway task-context signing key is configured; access checks that need a "
            "verified person will refuse. Regenerate internal credentials. "
            "event=task_context_signer_missing"
        )
    return signer


# The stored task text of memory-review schedules; the manager replaces it with
# the run's review jobs when a schedule fires
# (custom_agents.scheduled_runtime.prepare_memory_review_schedule).
MEMORY_REVIEW_INSTRUCTION_FRAME = (
    "Memory review maintenance tick for schedule_id={schedule_id}. {instruction} "
    "The local MemReviewAgent handles this schedule internally "
    "and completes the saved memory-review jobs locally."
)


def _memory_review_schedules(config: GatewayInstanceConfig) -> GatewaySpecialistScheduleSettings:
    """Memory review's schedules: the team's core-facts audit and each active person's pair.

    The pair (personal facts and stale conversations) is installed once per
    active person and owned by them (application/maintenance_schedules.py).
    """

    memory_config = dict(config.agent.memory or {})
    review = memory_review_settings(memory_config)
    return GatewaySpecialistScheduleSettings(
        name="memory review",
        enabled=review_enabled(memory_config),
        instruction_frame=MEMORY_REVIEW_INSTRUCTION_FRAME,
        definitions=(
            GatewaySystemScheduleDefinition(
                schedule_id=operational_id(MEMORY_REVIEW_CORE_ID_PREFIX, config.instance_name),
                schedule_name=f"{COREMEM_AUDIT_SCHEDULE_NAME_PREFIX}{config.instance_name}",
                interval_seconds=int(review.get("coremem_audit_interval_seconds") or 86400),
                instruction="Review active coremem facts for validity, overlap, and conflicts.",
                system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_COREMEM,
            ),
        ),
        person_definitions=(
            GatewayPersonSystemScheduleDefinition(
                schedule_id_prefix=MEMORY_REVIEW_USER_ID_PREFIX,
                interval_seconds=int(review.get("usermem_audit_interval_seconds") or 86400),
                instruction="Review this person's active usermem facts for validity, overlap, and conflicts.",
                system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_USERMEM,
            ),
            GatewayPersonSystemScheduleDefinition(
                schedule_id_prefix=MEMORY_REVIEW_STALE_ID_PREFIX,
                interval_seconds=int(review.get("stale_conversation_review_interval_seconds") or 3600),
                instruction="Review this person's stale conversation threads that still have unreviewed turns.",
                system_schedule_kind=SystemScheduleKind.MEMORY_REVIEW_STALE_CONVERSATIONS,
            ),
        ),
    )


# The stored task text of knowledge curation schedules; the curator's runtime
# replaces it with what to review, or skips the run when there is nothing
# (custom_agents.knowledge_curation_runtime).
KNOWLEDGE_CURATION_INSTRUCTION_FRAME = (
    "Knowledge curation tick for schedule_id={schedule_id}. {instruction} "
    "The local KnowledgeCuratorAgent handles this schedule; the runtime fills in what to review."
)


def _knowledge_curation_schedules(config: GatewayInstanceConfig) -> GatewaySpecialistScheduleSettings:
    """KnowledgeCuratorAgent's schedules: the team run (for no person) and one run per active person.

    Settings: ``agent.knowledge.curation``; off when the knowledge base is off.
    """

    knowledge = config.agent.knowledge
    curation = knowledge.curation
    return GatewaySpecialistScheduleSettings(
        name="knowledge curation",
        enabled=knowledge.enabled and curation.enabled,
        instruction_frame=KNOWLEDGE_CURATION_INSTRUCTION_FRAME,
        definitions=(
            GatewaySystemScheduleDefinition(
                schedule_id=operational_id(KNOWLEDGE_CURATION_TEAM_ID_PREFIX, config.instance_name),
                schedule_name=f"{KNOWLEDGE_CURATION_TEAM_SCHEDULE_NAME_PREFIX}{config.instance_name}",
                interval_seconds=int(curation.team_interval_minutes) * 60,
                instruction="Tidy the team knowledge pages: notes, broken links, duplicates and pages agents fight over.",
                system_schedule_kind=SystemScheduleKind.KNOWLEDGE_CURATION_TEAM,
            ),
        ),
        person_definitions=(
            GatewayPersonSystemScheduleDefinition(
                schedule_id_prefix=KNOWLEDGE_CURATION_PERSON_ID_PREFIX,
                interval_seconds=int(curation.person_interval_minutes) * 60,
                instruction="Review the team knowledge changes proposed from this person's work.",
                system_schedule_kind=SystemScheduleKind.KNOWLEDGE_CURATION_PERSON,
            ),
        ),
    )


def _compose_transcript_runtime(
    *,
    state_store: GatewayStateStore,
    session_repository: SessionRepository,
    id_generator: GatewayIdGenerator,
    event_output: GatewayEventOutput,
) -> tuple[TranscriptRepository, GatewayTranscriptService, GatewayEventOutput]:
    """Build the language-neutral transcript seam outside the legacy facade."""

    repository = state_store.transcript_repository
    service = GatewayTranscriptService(
        repository=repository,
        unit_of_work_factory=state_store,
        session_repository=session_repository,
        id_generator=id_generator,
        logger=logger,
    )
    return (
        repository,
        service,
        TranscriptRecordingGatewayEventOutput(
            transcript=service,
            downstream=event_output,
        ),
    )


def _access_maintenance_tasks(
    config: GatewayInstanceConfig,
    *,
    access_inbox: GatewayAccessInbox,
    broker: Any,
    tool_setups: Any,
    credential_workflow: Any,
    status_page: Any,
    stop_event: asyncio.Event,
) -> list[asyncio.Task[Any]]:
    """Access work at start: onboarding key hand-over, form recovery and the Inbox loop.

    The hand-over records the onboarding sharing choices and links the keys
    typed during onboarding (``assistant_gateway.onboarding_account_keys``); setup recovery
    tells tasks whose setup page was open when the gateway stopped that
    nobody watched it (``tool_setup.GatewayToolSetups.recover_unwatched``);
    credential recovery resumes saved broker session watches without storing
    submitted fields or page URLs;
    the Inbox loop (authorization on) sends announcements and decision
    messages; the Status page loop tells administrators about new problems
    (``assistant_gateway.status_page``), from its first check after the
    hand-over, so a model key onboarding handed over is never reported
    missing. With authorization on, administrators
    also hear about blocks naming an operation an update removed, which now
    block their whole tool (``access_control.stale_blocks``).
    """

    handover = OnboardingAccountKeyHandover(
        instance_root=config.instance_root,
        team_id=config.manager.team_id,
        broker=broker,
        roles_of=lambda person_id: (person.roles if (person := access_inbox.person(person_id)) else None),
        send_notice=access_inbox.send_notice,
        store=access_inbox.access_store,
        catalog=access_inbox.catalog,
    )
    handover_task = asyncio.create_task(handover.run(stop_event), name="gateway_onboarding_key_handover")
    tasks = [
        handover_task,
        asyncio.create_task(tool_setups.recover_unwatched(), name="gateway_tool_setup_recovery"),
        asyncio.create_task(credential_workflow.recover_collections(), name="gateway_credential_collection_recovery"),
        # Its first check waits for the hand-over, which saves the model keys from onboarding.
        asyncio.create_task(
            status_page.run(stop_event, first_check_after=(handover_task,)), name="gateway_status_page_loop"
        ),
    ]
    if config.authorization.enabled:
        tasks.append(asyncio.create_task(access_inbox.run(stop_event), name="gateway_access_inbox_loop"))
        tasks.append(
            asyncio.create_task(_report_stale_blocks(access_inbox, config.manager.team_id), name="gateway_stale_blocks")
        )
    return tasks


@dataclass(frozen=True)
class _StoredData:
    """The gateway's work on stored data: cleanup, warnings and their Settings page."""

    uploads: UploadRetention
    usage: StorageUsageNotices
    capacity: StorageCapacity
    settings: GatewayFileRetention


def _stored_data(
    config: GatewayInstanceConfig,
    *,
    state_store: GatewayStateStore,
    access_inbox: GatewayAccessInbox,
    person_changes: PersonChangeCheck,
) -> _StoredData:
    """The chat upload cleanup, the storage warnings and the Settings > Team > File Retention service.

    Uploads older than their retention are deleted at start and every few hours
    (``upload_retention``). People hear as their stored data grows
    (``storage_usage``, a daily check) and administrators before storage fills
    (``storage_capacity``, on the Status page). Administrators change the days,
    scratch retention and the storage warnings in Settings (``file_retention``),
    and the running cleanup and checks follow at once.
    """

    uploads = UploadRetention(
        records=state_store.delivery_repository,
        uploads_dir=resolve_inbound_attachment_cache_dir(config),
        retention_days=config.interactive_apps.internal_website.attachments.retention_days,
    )
    usage = build_storage_usage_notices(
        config,
        chat=state_store,
        access_store=access_inbox.access_store,
        send_notice_once=access_inbox.send_notice_once,
    )
    capacity = build_storage_capacity(config)
    return _StoredData(
        uploads=uploads,
        usage=usage,
        capacity=capacity,
        settings=GatewayFileRetention(
            config_path=config.config_path,
            person_changes=person_changes,
            uploads=uploads,
            storage_usage=usage,
            storage_capacity=capacity,
        ),
    )


async def _report_stale_blocks(access_inbox: GatewayAccessInbox, team_id: str) -> None:
    """Tell administrators once about each block on an operation the catalog no longer has."""

    from access_control.stale_blocks import report_stale_blocks

    try:
        await asyncio.to_thread(report_stale_blocks, access_inbox.catalog(), access_inbox.access_store(), team_id)
    except Exception:  # noqa: BLE001 - the blocks still apply to their whole tool; only the notice is missing
        logger.exception("Could not check blocks against the tool catalog. event=stale_block_check_failed team_id=%s", team_id)


def _agent_presence_observer(
    presence: GatewayTeamPresence,
    guidance: GatewayThreadGuidance,
) -> Callable[[AgentStatus, datetime | None, str], None]:
    """Connect presence recording and held-guidance wakeups during composition."""

    def record(status: AgentStatus, reported_at: datetime | None, source: str) -> None:
        presence.record_agent_presence(status, reported_at, source)
        guidance.lead_reported(status, reported_at, source)

    return record


class UserGateway:
    """User-facing NATS gateway with delivery and autonomy services."""

    def __init__(
        self,
        config: GatewayInstanceConfig,
        *,
        output_sink: Optional[OutputSink] = None,
        event_sink: Optional[EventSink] = None,
        state_store: Optional[GatewayStateStore] = None,
        state_storage_settings: Optional[StorageSettings] = None,
        clock: GatewayClock | None = None,
        id_generator: GatewayIdGenerator | None = None,
        manager_transport: GatewayManagerTransport | None = None,
        manager_runtime: GatewayManagerRuntime | None = None,
        event_output: GatewayEventOutput | None = None,
        private_authorizer: GatewayPrivateResourceAuthorizer | None = None,
        secure_broker: GatewaySecureBroker | None = None,
        activity_source: GatewayActivitySource | None = None,
    ):
        self.config = config
        self._logger = logger
        self._clock = clock or SystemGatewayClock()
        self._id_generator = id_generator or UuidGatewayIdGenerator()
        resolved_manager_runtime = (
            manager_runtime or NatsGatewayManagerRuntime.from_gateway_config(config)
        )
        self._compat_event_output: CallbackGatewayEventOutput | None = None
        if event_output is None:
            self._compat_event_output = CallbackGatewayEventOutput(
                logger=logger,
                output_sink=output_sink,
                event_sink=event_sink,
            )
            self._event_output: GatewayEventOutput = self._compat_event_output
        else:
            self._event_output = event_output
        self._state_storage_settings = state_storage_settings or (
            state_store.storage_settings
            if state_store is not None
            else config.state_storage_settings
        )
        resolved_state_store = state_store or GatewayStateStore(
            self._state_storage_settings.sqlite_path or "",
            logger_=logger,
            storage_settings=self._state_storage_settings,
        )
        self.state_store = resolved_state_store
        self.identity_repository: IdentityRepository = (
            resolved_state_store.identity_repository
        )
        self.session_repository: SessionRepository = (
            resolved_state_store.session_repository
        )
        self.request_repository: RequestRepository = (
            resolved_state_store.request_repository
        )
        self.delivery_repository: DeliveryRepository = (
            resolved_state_store.delivery_repository
        )
        (
            self.transcript_repository,
            self._transcript_service,
            self._event_output,
        ) = _compose_transcript_runtime(
            state_store=resolved_state_store,
            session_repository=self.session_repository,
            id_generator=self._id_generator,
            event_output=self._event_output,
        )
        ownership_bootstrap = ConfigGatewayOwnershipBootstrap(
            config=config,
            state_store=self.state_store,
            storage_settings=self._state_storage_settings,
            identity_repository=self.identity_repository,
            clock=self._clock,
            logger=logger,
        )
        self._private_authorizer = (
            private_authorizer or OwnershipPrivateResourceAuthorizer()
        )
        self._session_service = GatewaySessionService(
            repository=self.session_repository,
            id_generator=self._id_generator,
            authorizer=self._private_authorizer,
            team_id=config.manager.team_id,
            logger=logger,
        )
        identity_users = config.gateway.users
        self._identity_secrets = SystemGatewayIdentitySecrets()
        self._identity_service = GatewayIdentityService(
            repository=self.identity_repository,
            unit_of_work_factory=self.state_store,
            authorizer=MembershipRoleIdentityAuthorizer(),
            secrets=self._identity_secrets,
            clock=self._clock,
            settings=GatewayIdentitySettings(
                enabled=identity_users.enabled,
                allow_newuser_registration=(identity_users.allow_newuser_registration),
                web_user_tokens_enabled=(identity_users.web_user_tokens_enabled),
                web_user_token_ttl_days=(identity_users.web_user_token_ttl_days),
                web_user_invite_ttl_seconds=(
                    identity_users.web_user_invite_ttl_seconds
                ),
                link_code_ttl_seconds=identity_users.link_code_ttl_seconds,
                default_timezone=config.user.timezone,
                team_id=config.gateway.team_id,
                web_chat_url=(
                    f"{config.internal_website_base_url.rstrip('/')}/webchat"
                ),
            ),
            people_directory=ownership_bootstrap.people_directory,
            ensure_default_identity=ownership_bootstrap.seed_default_user,
            logger=logger,
        )
        heartbeat_source_id = config.heartbeat.source_id or operational_id(
            HEARTBEAT_SOURCE_ID_PREFIX,
            config.instance_name,
        )
        heartbeat_schedule_id = config.heartbeat.schedule_id or operational_id(
            HEARTBEAT_SCHEDULE_ID_PREFIX,
            config.instance_name,
        )
        self._request_context_service = GatewayRequestContextService(
            settings=GatewayRequestContextSettings(
                instance_name=config.instance_name,
                team_id=config.manager.team_id,
                default_canonical_user_id=config.user.canonical_user_id,
                default_display_name=config.user.display_name,
                default_timezone=config.user.timezone,
                heartbeat_source_id=heartbeat_source_id,
                users_enabled=identity_users.enabled,
            ),
            identity=self._identity_service,
            context_signer=_gateway_context_signer(),
        )
        self._request_service = GatewayRequestService(
            repository=self.request_repository,
            unit_of_work_factory=self.state_store,
            logger=logger,
        )
        # Agent questions wait in line per chat and expire after the same
        # time the gateway waits for a request's reply.
        self._pending_questions = GatewayPendingQuestions(
            repository=self.request_repository,
            sessions=self.session_repository,
            event_output=self._event_output,
            clock=self._clock,
            timeout_seconds=float(config.manager.request_timeout_seconds),
            logger=logger,
        )
        self._delivery_service = GatewayDeliveryService(
            repository=self.delivery_repository,
            unit_of_work_factory=self.state_store,
        )
        # Seeds the first user and copies everyone's standing for the
        # scheduler before connect() can send a maintenance schedule.
        ownership_bootstrap.prepare_people()
        ownership_bootstrap.reconcile_legacy_ownership()

        self._background_tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()
        self._started = False
        self._delivery_preferences = ConfigGatewayDeliveryPreferences(
            config=config,
            logger=logger,
        )
        transition_settings = GatewayManagerTransitionSettings(
            assistant_display_name=config.assistant.display_name,
            unavailable_message_template=(
                config.assistant.unavailable_message_template
                or DEFAULT_UNAVAILABLE_MESSAGE_TEMPLATE
            ),
            available_message_template=(
                config.assistant.available_message_template
                or DEFAULT_AVAILABLE_MESSAGE_TEMPLATE
            ),
            unavailable_message_fallback=DEFAULT_UNAVAILABLE_MESSAGE_TEMPLATE,
            available_message_fallback=DEFAULT_AVAILABLE_MESSAGE_TEMPLATE,
            enabled_fallback_channels=tuple(
                channel
                for channel, enabled in (
                    (DELIVERY_CHANNEL_DISCORD, config.interactive_apps.discord.enabled),
                    (
                        DELIVERY_CHANNEL_TELEGRAM,
                        config.interactive_apps.telegram.enabled,
                    ),
                    (
                        DELIVERY_CHANNEL_WHATSAPP,
                        config.interactive_apps.whatsapp.enabled,
                    ),
                    (DELIVERY_CHANNEL_SIGNAL, config.interactive_apps.signal.enabled),
                    (DELIVERY_CHANNEL_PHOTON, config.interactive_apps.photon.enabled),
                )
                if enabled
            ),
            telegram_pause_polling=(
                config.interactive_apps.telegram.manager_offline_policy
                == "pause_telegram_polling"
            ),
        )
        self._delivery_presentation = GatewayDeliveryPresentation()
        self._delivery_attachments = ConfigGatewayDeliveryAttachmentResolver(
            config=config,
            logger=logger,
        )
        self._delivery_projection = GatewayDeliveryProjection(
            settings=GatewayDeliveryProjectionSettings(
                default_session_key=config.user.default_session_key,
            ),
            sessions=self.session_repository,
            delivery=self._delivery_service,
            presentation=self._delivery_presentation,
            attachments=self._delivery_attachments,
            logger=logger,
        )
        self._manager_client = GatewayManagerClient(
            settings=GatewayManagerClientSettings(
                instance_name=config.instance_name,
                user_display_name=config.user.display_name,
                team_id=config.manager.team_id,
                manager_named_agent_type=config.manager.named_agent_type,
                response_route=config.manager.response_queue_name,
                initial_manager_id=config.manager.manager_agent_id,
                liveness_poll_seconds=MANAGER_LIVENESS_POLL_SECONDS,
                presence_stale_seconds=MANAGER_PRESENCE_STALE_SECONDS,
                recovery_discovery_timeout_seconds=(
                    MANAGER_RECOVERY_DISCOVERY_TIMEOUT_SECONDS
                ),
            ),
            clock=self._clock,
            id_generator=self._id_generator,
            logger=logger,
            runtime=resolved_manager_runtime,
            outbound_transport=manager_transport,
            write_manager_route=ConfigGatewayManagerRouteWriter(config),
            trace_enabled=lambda: is_reply_routing_trace_enabled(config.agent.debug),
        )
        attachment_settings = config.interactive_apps.internal_website.attachments
        self.attachment_replication = GatewayAttachmentReplicationApplication(
            settings=GatewayAttachmentReplicationSettings(
                enabled=attachment_settings.remote_copy_enabled,
                bucket=attachment_settings.nats_object_bucket,
                ttl_seconds=float(attachment_settings.nats_object_ttl_seconds),
                max_bytes=attachment_settings.nats_object_max_bytes,
            ),
            delivery=self._delivery_service,
            manager=self._manager_client,
        )
        self.proactive_delivery = GatewayProactiveDeliveryApplication(
            settings=GatewayProactiveDeliverySettings(
                instance_name=config.instance_name,
                session_key=config.heartbeat.session_key,
                default_owner_canonical_user_id=config.user.canonical_user_id,
                users_enabled=identity_users.enabled,
            ),
            targets=self._delivery_preferences,
            sessions=self._session_service,
            manager=self._manager_client,
            event_output=self._event_output,
            # Team schedules move to another administrator when the first user leaves.
            team_work_owner=self._identity_service.team_work_owner,
        )
        self._maintenance_schedules = GatewayMaintenanceScheduleApplication(
            settings=GatewayMaintenanceScheduleSettings(
                instance_name=config.instance_name,
                response_route=config.manager.response_queue_name,
                user_display_name=config.user.display_name,
                request_timezone=config.user.timezone,
                default_owner_canonical_user_id=config.user.canonical_user_id,
                heartbeat=GatewayHeartbeatScheduleSettings(
                    enabled=config.heartbeat.enabled,
                    source_id=heartbeat_source_id,
                    schedule_id=heartbeat_schedule_id,
                    interval_seconds=config.heartbeat.interval_seconds,
                ),
                specialist_schedules=(_memory_review_schedules(config), _knowledge_curation_schedules(config)),
                # The manager creates this schedule; the gateway refreshes its
                # person and roles (application/maintenance_schedules.py).
                internal_improvement=GatewayInternalImprovementScheduleSettings(
                    schedule_id=internal_improvement_schedule_id(config.manager.team_id),
                    schedule_name=internal_improvement_schedule_name(config.manager.team_id),
                    interval_seconds=int(
                        configured_review_interval_seconds(config.agent.schedule_maintenance)
                    ),
                ),
            ),
            proactive_sessions=self.proactive_delivery,
            person_sessions=GatewayPersonMaintenanceSessions(
                settings=GatewayPersonMaintenanceSettings(
                    team_id=config.gateway.team_id,
                    users_enabled=identity_users.enabled,
                    default_owner_canonical_user_id=config.user.canonical_user_id,
                ),
                identity=self.identity_repository,
                sessions=self._session_service,
                session_repository=self.session_repository,
                proactive_sessions=self.proactive_delivery,
                logger=logger,
            ),
            request_context=self._request_context_service,
            delivery=self._delivery_service,
            manager=self._manager_client,
            id_generator=self._id_generator,
            logger=logger,
        )
        self._manager_commands = GatewayManagerCommandService(
            settings=GatewayManagerCommandSettings(
                instance_name=config.instance_name,
                response_route=config.manager.response_queue_name,
                timeout_seconds=MANAGER_CONTROL_TIMEOUT_SECONDS,
                unavailable_text=transition_settings.unavailable_notice(),
            ),
            manager=self._manager_client,
            request_context=self._request_context_service,
            private_authorizer=self._private_authorizer,
            id_generator=self._id_generator,
            logger=logger,
        )
        self._schedule_application = GatewayScheduleApplication(
            settings=GatewayScheduleSettings(
                instance_name=config.instance_name,
                response_route=config.manager.response_queue_name,
                user_display_name=config.user.display_name,
                request_timezone=config.user.timezone,
                default_session_key=config.user.default_session_key,
                default_owner_canonical_user_id=config.user.canonical_user_id,
                manager_unavailable_text=transition_settings.unavailable_notice(),
            ),
            sessions=self._session_service,
            request_context=self._request_context_service,
            delivery=self._delivery_service,
            manager=self._manager_client,
            manager_commands=self._manager_commands,
            id_generator=self._id_generator,
            # The schedule, MCP and procedure controls share one check of who
            # may make platform changes directly (PersonChangeCheck).
            person_changes=(
                person_changes := PersonChangeCheck(config, MembershipRoles(self.identity_repository))
            ),
        )
        self._person_changes = person_changes
        self._deferred_delivery_application = GatewayDeferredDeliveryApplication(
            settings=GatewayDeferredDeliverySettings(
                instance_name=config.instance_name,
                response_route=config.manager.response_queue_name,
            ),
            delivery=self._delivery_service,
            sessions=self.session_repository,
            manager=self._manager_client,
            event_output=self._event_output,
            clock=self._clock,
            logger=logger,
        )
        self._unsolicited_delivery = GatewayUnsolicitedDeliveryApplication(
            settings=GatewayUnsolicitedDeliverySettings(
                requester_timezone=config.user.timezone,
            ),
            requests=self._request_service,
            questions=self._pending_questions,
            sessions=self.session_repository,
            projection=self._delivery_projection,
            presentation=self._delivery_presentation,
            delivery=self._delivery_service,
            policies=self._delivery_preferences,
            receipts=self._deferred_delivery_application,
            event_output=self._event_output,
            clock=self._clock,
            logger=logger,
            on_request_finished=lambda: self._thread_guidance.wake(),
        )
        self._local_responses = GatewayLocalResponseFactory(
            settings=GatewayLocalResponseSettings(
                instance_name=config.instance_name,
                response_route=config.manager.response_queue_name,
            ),
            id_generator=self._id_generator,
        )
        # A busy thread's later messages are guidance for its running request.
        self._thread_guidance = GatewayThreadGuidance(
            settings=GatewayThreadGuidanceSettings(
                response_route=config.manager.response_queue_name,
            ),
            requests=self._request_service,
            sessions=self.session_repository,
            manager=self._manager_client,
            request_context=self._request_context_service,
            local_responses=self._local_responses,
            render_envelope=self._delivery_presentation.render_envelope,
            event_output=self._event_output,
            clock=self._clock,
            logger=logger,
        )
        signed_in = SignedInPerson(sessions=self.session_repository, request_context=self._request_context_service)
        self.team_presence = GatewayTeamPresence(
            team_id=config.manager.team_id,
            clock=self._clock,
            stale_after_seconds=MANAGER_PRESENCE_STALE_SECONDS,
            list_agent_classes=lambda: configured_agent_classes(config),
            logger=logger,
        )
        # Approving an MCP server stores grants through the Inbox's access
        # service (created below; looked up when a person approves).
        self.mcp_controls = GatewayMCPControls(
            config,
            person_changes=person_changes,
            signed_in=signed_in,
            access_requests=lambda: self.access_inbox.requests(),
        )
        self.procedures = GatewayInstalledProcedures(config, person_changes=person_changes, signed_in=signed_in)
        # Access reviews and notices (Phase 4 access model); its loop runs
        # with authorization on (_access_maintenance_tasks).
        self.access_inbox = build_gateway_access_inbox(
            config,
            identity_repository=self.identity_repository,
            manager_client=self._manager_client,
            session_repository=self.session_repository,
            proactive_sessions=self.proactive_delivery,
            event_output=self._event_output,
            logger=logger,
        )
        # Upload cleanup, people's storage notices, storage on the Status page
        # and their Settings page (_stored_data).
        self.stored_data = _stored_data(
            config, state_store=self.state_store, access_inbox=self.access_inbox, person_changes=person_changes
        )
        self._task_progress_service = GatewayTaskProgressService(
            settings=GatewayTaskProgressSettings(
                instance_name=config.instance_name,
                response_route=config.manager.response_queue_name,
                manager_named_agent_type=config.manager.named_agent_type,
                request_timezone=config.user.timezone,
                default_session_key=config.user.default_session_key,
                default_owner_canonical_user_id=config.user.canonical_user_id,
                timeout_seconds=TASK_PROGRESS_STATUS_TIMEOUT_SECONDS,
            ),
            sessions=self._session_service,
            request_context=self._request_context_service,
            manager=self._manager_client,
            id_generator=self._id_generator,
            local_responses=self._local_responses,
            logger=logger,
        )
        self._secure_broker = (
            secure_broker
            or HttpGatewaySecureBroker.from_gateway_config(
                config,
                logger=logger,
            )
        )

        # One account link opener serves agent requests and the /link command.
        self._account_links = GatewayAccountLinks(
            broker=self._secure_broker,
            team_id=config.manager.team_id,
            clock=self._clock,
            logger=logger,
        )
        self._credential_workflow = GatewayCredentialWorkflow(
            settings=GatewayCredentialWorkflowSettings(
                gateway_instance_name=config.instance_name,
                user_display_name=config.user.display_name,
                assistant_display_name=config.assistant.display_name,
                team_id=config.manager.team_id,
                response_route=config.manager.response_queue_name,
                request_timezone=config.user.timezone,
            ),
            broker=self._secure_broker,
            account_links=self._account_links,
            team_roles=self._request_context_service.team_roles,
            manager_client=self._manager_client,
            session_repository=self.session_repository,
            tool_setup_repository=resolved_state_store.tool_setup_repository,
            collection_repository=resolved_state_store.credential_collection_repository,
            setup_states=self.access_inbox.setup_states(),
            request_service=self._request_service,
            clock=self._clock,
            event_output=self._event_output,
            logger=logger,
        )
        # An approved access proposal opens its account link forms for the
        # approver; the waiting task resumes once they end.
        self.access_inbox.tool_setups = self._credential_workflow.tool_setups
        self._clarification_core = GatewayClarificationCore(
            settings=GatewayClarificationSettings(
                instance_name=config.instance_name,
                response_route=config.manager.response_queue_name,
                manager_named_agent_type=config.manager.named_agent_type,
                request_timezone=config.user.timezone,
                default_session_key=config.user.default_session_key,
                default_owner_canonical_user_id=config.user.canonical_user_id,
                parent_probe_timeout_seconds=TASK_PROGRESS_STATUS_TIMEOUT_SECONDS,
                manager_unavailable_text=transition_settings.unavailable_notice(),
            ),
            requests=self._request_service,
            questions=self._pending_questions,
            sessions=self._session_service,
            session_repository=self.session_repository,
            request_context=self._request_context_service,
            manager=self._manager_client,
            id_generator=self._id_generator,
            logger=logger,
        )
        self._credential_handoffs = ToolSetupHandoffs(
            workflow=self._credential_workflow,
            requests=self._request_service,
            sessions=self.session_repository,
            transcript=self._transcript_service,
        )
        self._response_coordinator = GatewayResponseCoordinator(
            settings=GatewayResponseCoordinatorSettings(
                gateway_instance_name=config.instance_name,
                user_display_name=config.user.display_name,
                response_route=config.manager.response_queue_name,
                request_timezone=config.user.timezone,
                request_timeout_seconds=(config.manager.request_timeout_seconds),
            ),
            manager_client=self._manager_client,
            request_service=self._request_service,
            session_repository=self.session_repository,
            credential_workflow=self._credential_workflow,
            credential_handoffs=self._credential_handoffs,
            questions=self._pending_questions,
            reply_to_task_query=self._clarification_core.reply_to_task_query,
            render_envelope=self._delivery_presentation.render_envelope,
            resolve_attachments=self._delivery_attachments.resolve,
            manager_unavailable_text=transition_settings.unavailable_notice,
            on_request_finished=self._thread_guidance.wake,
            logger=logger,
        )
        self._clarification_application = GatewayClarificationApplication(
            core=self._clarification_core,
            questions=self._pending_questions,
            manager=self._manager_client,
            responses=self._response_coordinator,
            local_responses=self._local_responses,
            logger=logger,
        )
        self._operational_controls = GatewayOperationalControlApplication(
            settings=GatewayOperationalControlSettings(
                response_route=config.manager.response_queue_name,
                manager_named_agent_type=config.manager.named_agent_type,
                default_session_key=config.user.default_session_key,
                default_owner_canonical_user_id=config.user.canonical_user_id,
            ),
            sessions=self._session_service,
            request_context=self._request_context_service,
            requests=self._request_service,
            manager=self._manager_client,
            manager_commands=self._manager_commands,
            responses=self._response_coordinator,
            id_generator=self._id_generator,
            logger=logger,
        )
        self.manager_transitions = GatewayManagerTransitionApplication(
            settings=transition_settings,
            sessions=self.session_repository,
            targets=self._delivery_preferences,
            proactive_sessions=self.proactive_delivery,
            maintenance=self._maintenance_schedules,
            manager=self._manager_client,
            responses=self._response_coordinator,
            event_output=self._event_output,
            logger=logger,
        )
        self.inbound_messages = GatewayInboundMessageApplication(
            delivery=self._unsolicited_delivery,
            unmatched_credential_handler=(
                self._response_coordinator.handle_unmatched_credential_request
            ),
            late_guidance_status_handler=self._thread_guidance.handle_late_status,
        )
        self._manager_client.bind_inbound_handlers(
            on_unsolicited=self.inbound_messages.handle,
            on_manager_available=self.manager_transitions.handle_available,
            on_manager_unavailable=self.manager_transitions.handle_unavailable,
            on_agent_presence=_agent_presence_observer(self.team_presence, self._thread_guidance),
        )
        self._interactive_submission_service = GatewayInteractiveSubmissionService(
            settings=GatewayInteractiveSubmissionSettings(
                response_route=config.manager.response_queue_name,
                default_session_key=config.user.default_session_key,
                default_owner_canonical_user_id=config.user.canonical_user_id,
            ),
            sessions=self._session_service,
            session_repository=self.session_repository,
            request_context=self._request_context_service,
            requests=self._request_service,
            manager=self._manager_client,
            responses=self._response_coordinator,
            guidance=self._thread_guidance,
            progress=self._task_progress_service,
            local_responses=self._local_responses,
            id_generator=self._id_generator,
            refresh_delivery_preferences=self._delivery_preferences.refresh,
            transcript=self._transcript_service,
            # A newly added person's first-time setup (assistant_gateway.personal_setup).
            personal_setup=self.access_inbox.personal_setup,
            logger=logger,
        )
        self.automated_submission = GatewayAutomatedSubmissionApplication(
            settings=GatewayAutomatedSubmissionSettings(
                instance_name=config.instance_name,
                response_route=config.manager.response_queue_name,
                default_timezone=config.user.timezone,
                default_session_key=config.user.default_session_key,
                default_owner_canonical_user_id=config.user.canonical_user_id,
            ),
            delivery_policy=config.delivery,
            sessions=self._session_service,
            request_context=self._request_context_service,
            requests=self._request_service,
            manager=self._manager_client,
            clock=self._clock,
            id_generator=self._id_generator,
            refresh_delivery_preferences=self._delivery_preferences.refresh,
        )
        self._command_operations = GatewayCommandOperationsApplication(
            settings=GatewayCommandOperationsSettings(
                default_session_key=config.user.default_session_key,
                default_owner_canonical_user_id=config.user.canonical_user_id,
                manager_agent_id=config.manager.manager_agent_id,
            ),
            identity=self._identity_service,
            request_context=self._request_context_service,
            sessions=self._session_service,
            manager_commands=self._manager_commands,
            submission=self._interactive_submission_service,
            pinned_memory_parser=RuntimePinnedMemoryCommandParser(),
            private_authorizer=self._private_authorizer,
            # Disabling, removing and restoring people (assistant_gateway.person_departure).
            person_lifecycle=build_gateway_person_lifecycle(
                config,
                identity=self._identity_service,
                state_store=self.state_store,
                manager_commands=self._manager_commands,
                broker=self._secure_broker,
                access_requests=self.access_inbox.requests,
                person_schedules=self._maintenance_schedules,
                clock=self._clock,
                personal_setup=self.access_inbox.personal_setup,
                logger=logger,
            ),
        )
        # Whose account agents use for each service: the /accounts command and
        # the web chat's Settings > Accounts tab read the same rows.
        self.account_sharing = GatewayAccountSharing(
            store=self.access_inbox.access_store,
            catalog=self.access_inbox.catalog,
            team_id=config.manager.team_id,
            send_notice=self.access_inbox.send_notice,
            setup=self.access_inbox.personal_setup.steps,
            display_name=self.access_inbox.display_name,
        )
        self.command_application = build_gateway_command_application(
            config=config,
            operations=self._command_operations,
            session_repository=self.session_repository,
            request_repository=self.request_repository,
            private_authorizer=self._private_authorizer,
            operational_controls=self._operational_controls,
            link_results=self._credential_workflow.link_results,
            schedule_controls=self._schedule_application,
            mcp_controls=self.mcp_controls,
            secure_broker=self._secure_broker,
            account_links=self._account_links,
            access=GatewayAccessCommands(
                requests=self.access_inbox.requests,
                person=self.access_inbox.person,
                review_url=self.access_inbox.review_url,
                authorization_enabled=config.authorization.enabled,
                data_folders=self.access_inbox.data_folders,
                protected_locations=tuple(self.access_inbox.protected_locations),
                file_sharing=self.access_inbox.file_sharing,
            ),
            account_sharing=self.account_sharing,
        )
        website_identity = WebsiteIdentityApplication(
            identity=self._identity_service,
            bootstrap_tokens=EnvironmentGatewayBootstrapTokenSource(
                config.interactive_apps.internal_website.web_chat_token_env_var
            ),
            settings=WebsiteIdentitySettings(
                bootstrap_canonical_user_id=(
                    str(config.user.canonical_user_id or "").strip() or None
                ),
                bootstrap_display_name=(
                    str(config.user.display_name or "").strip() or None
                ),
            ),
        )
        website_sessions = WebsiteSessionApplication(
            sessions=self._session_service,
            session_repository=self.session_repository,
            delivery_repository=self.delivery_repository,
            request_context=self._request_context_service,
            private_authorizer=self._private_authorizer,
            manager_agent_id=config.manager.manager_agent_id,
            team_id=config.manager.team_id,
            # Sessions whose request still waits for the agent's reply.
            awaiting_reply_session_keys=lambda: self._request_service.session_keys_for(
                self._manager_client.pending_response_correlation_ids()
            ),
            proactive_session_key=config.heartbeat.session_key,
        )
        website_operations = WebsiteOperationsApplication(
            team=self.team_presence,
            mcp=self.mcp_controls,
            event_output=self._event_output,
            operational_controls=self._operational_controls,
            schedules=self._schedule_application,
            memory_operations=self._command_operations,
            workspace_browser=WebsiteWorkspaceBrowser(
                workspace_root=config.workspace_path,
                authorizer=self._private_authorizer,
                team_id=config.manager.team_id,
            ),
        )
        self.application = WebsiteRuntimeApplication(
            workflows=create_access_workflow_page(config, self.access_inbox),
            access_inbox=self.access_inbox,
            settings=GatewayRuntimeApplicationSettings(
                default_session_key=config.user.default_session_key,
                default_owner_canonical_user_id=config.user.canonical_user_id,
            ),
            sessions=self._session_service,
            session_repository=self.session_repository,
            delivery_repository=self.delivery_repository,
            request_context=self._request_context_service,
            manager=self._manager_client,
            commands=self.command_application,
            progress=self._task_progress_service,
            clarification=self._clarification_application,
            schedules=self._schedule_application,
            deferred_delivery=self._deferred_delivery_application,
            submission=self._interactive_submission_service,
            access_reviews=GatewayAccessReviewControls(
                request_context=self._request_context_service,
                decider=self.access_inbox,
                logger=logger,
            ),
            web_identity=website_identity,
            web_sessions=website_sessions,
            web_operations=website_operations,
            web_transcripts=WebsiteTranscriptApplication(
                credential_actions=self._credential_handoffs.pending_for_owner,
                transcript=self._transcript_service,
                session_repository=self.session_repository,
                private_authorizer=self._private_authorizer,
                team_id=config.manager.team_id,
            ),
            procedures=self.procedures,
            # Administrators' Status page; its notice loop starts with the access maintenance tasks.
            status=StatusPage(
                config=config,
                model_key_status=lambda: self._secure_broker.get_model_key_status(),
                agent_statuses=self.team_presence.current_statuses,
                send_notice_once=self.access_inbox.send_notice_once,
                stopped_receivers=resolved_manager_runtime.stopped_receivers,
                storage=self.stored_data.capacity,
                jetstream_usage=resolved_manager_runtime.jetstream_usage,
            ),
            # People's Knowledge page; the knowledge base is in the manager's database.
            knowledge=GatewayKnowledgePages(
                runner=self._command_operations,
                display_name=self.access_inbox.display_name,
                enabled=config.agent.knowledge.enabled,
            ),
            accounts=self.account_sharing,
            file_retention=self.stored_data.settings,
        )
        self._activity_source = (
            activity_source or HttpGatewayActivitySource.from_gateway_config(config)
        )
        activity_notice_routing = GatewayActivityNoticeRoutingApplication(
            sessions=self.session_repository,
            proactive_sessions=self.proactive_delivery,
            event_output=self._event_output,
        )
        self._activity_notices = GatewayActivityNoticeCoordinator(
            source=self._activity_source,
            resolve_session=activity_notice_routing.resolve_session,
            emit_notice=activity_notice_routing.emit_notice,
            poll_interval_seconds=max(
                ACTIVITY_NOTICE_POLL_SECONDS,
                config.activity_collector.flush_interval_seconds,
            ),
            logger=logger,
        )

    def bind_event_sink(self, value: EventSink | None) -> None:
        """Bind the interactive adapter output during runtime composition."""

        if self._compat_event_output is None:
            raise RuntimeError(
                "event_sink cannot replace an explicitly configured GatewayEventOutput"
            )
        self._compat_event_output.event_sink = value

    async def connect(self, *, start_services: bool = True) -> None:
        if self._started:
            return
        await self._manager_client.connect()
        await self._manager_client.refresh_manager_id()
        self._clarification_core.expire_stale_on_startup()
        self._started = True
        self._stop_event.clear()
        self.manager_transitions.start()
        # Message routing, not an optional service: held guidance must reach
        # its request (or the person must hear it did not) in every mode.
        self._background_tasks = [
            asyncio.create_task(
                self._thread_guidance.run(self._stop_event),
                name="gateway_held_guidance_loop",
            )
        ]

        if start_services:
            await self._maintenance_schedules.ensure_all()
            self._background_tasks += [
                asyncio.create_task(
                    self._deferred_delivery_application.run(self._stop_event),
                    name="gateway_delivery_loop",
                ),
                asyncio.create_task(
                    self._manager_client.manager_liveness_loop(self._stop_event),
                    name="gateway_manager_liveness_loop",
                ),
                asyncio.create_task(
                    self.stored_data.uploads.run(self._stop_event),
                    name="gateway_upload_retention_loop",
                ),
                asyncio.create_task(
                    self.stored_data.usage.run(self._stop_event),
                    name="gateway_storage_usage_loop",
                ),
            ]
            self._background_tasks.extend(
                _access_maintenance_tasks(
                    self.config,
                    access_inbox=self.access_inbox,
                    broker=self._secure_broker,
                    tool_setups=self._credential_workflow.tool_setups,
                    credential_workflow=self._credential_workflow,
                    status_page=self.application.status,
                    stop_event=self._stop_event,
                )
            )
            if self._activity_notices.enabled:
                self._background_tasks.append(
                    asyncio.create_task(
                        self._activity_notices.run(self._stop_event),
                        name="gateway_activity_plan_notice_loop",
                    )
                )

    async def close(self) -> None:
        self._stop_event.set()
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks = []
        await self._credential_workflow.close()
        self._credential_handoffs.clear()
        await self._activity_notices.close()
        self.access_inbox.close()
        self._person_changes.close()
        await self._manager_client.close()
        self.manager_transitions.stop()
        self._started = False

    async def __aenter__(self) -> "UserGateway":
        await self.connect()
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await self.close()

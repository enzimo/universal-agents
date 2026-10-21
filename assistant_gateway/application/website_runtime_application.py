"""Composed Website application over core and Website-only use-case owners."""

from __future__ import annotations

from assistant_gateway.application.access_inbox_ports import AccessInboxApplication
from assistant_gateway.application.clarification import GatewayClarificationApplication
from assistant_gateway.application.access_workflows import AccessWorkflowApplication
from assistant_gateway.application.deferred_delivery import (
    GatewayDeferredDeliveryApplication,
)
from assistant_gateway.application.interactive_commands import GatewayCommandApplication
from assistant_gateway.application.interactive_submission import (
    GatewayInteractiveSubmissionService,
)
from assistant_gateway.application.manager_client import GatewayManagerClient
from assistant_gateway.application.request_context import GatewayRequestContextService
from assistant_gateway.application.procedures import ProcedureApplication
from assistant_gateway.application.runtime_application import (
    GatewayAccessReviewDecisions,
    GatewayRuntimeApplication,
    GatewayRuntimeApplicationSettings,
)
from assistant_gateway.application.schedule_management import GatewayScheduleApplication
from assistant_gateway.application.session_service import GatewaySessionService
from assistant_gateway.application.task_progress import GatewayTaskProgressService
from assistant_gateway.application.website_application import (
    AccountsApplication,
    FileRetentionApplication,
    KnowledgeApplication,
    StatusApplication,
    WebIdentityApplication,
    WebOperationsApplication,
    WebSessionApplication,
    WebTranscriptApplication,
)
from assistant_gateway.ports.repositories import DeliveryRepository, SessionRepository


class WebsiteRuntimeApplication(GatewayRuntimeApplication):
    """Core channel use cases plus separately owned Website capabilities."""

    def __init__(
        self,
        *,
        settings: GatewayRuntimeApplicationSettings,
        sessions: GatewaySessionService,
        session_repository: SessionRepository,
        delivery_repository: DeliveryRepository,
        request_context: GatewayRequestContextService,
        manager: GatewayManagerClient,
        commands: GatewayCommandApplication,
        progress: GatewayTaskProgressService,
        clarification: GatewayClarificationApplication,
        schedules: GatewayScheduleApplication,
        deferred_delivery: GatewayDeferredDeliveryApplication,
        submission: GatewayInteractiveSubmissionService,
        access_reviews: GatewayAccessReviewDecisions,
        workflows: AccessWorkflowApplication,
        access_inbox: AccessInboxApplication,
        web_identity: WebIdentityApplication,
        web_sessions: WebSessionApplication,
        web_operations: WebOperationsApplication,
        web_transcripts: WebTranscriptApplication,
        procedures: ProcedureApplication,
        status: StatusApplication,
        knowledge: KnowledgeApplication,
        accounts: AccountsApplication,
        file_retention: FileRetentionApplication,
    ) -> None:
        super().__init__(
            settings=settings,
            sessions=sessions,
            session_repository=session_repository,
            delivery_repository=delivery_repository,
            request_context=request_context,
            manager=manager,
            commands=commands,
            progress=progress,
            clarification=clarification,
            schedules=schedules,
            deferred_delivery=deferred_delivery,
            submission=submission,
            access_reviews=access_reviews,
        )
        self.workflows = workflows
        self.access_inbox = access_inbox
        self._web_identity = web_identity
        self._web_sessions = web_sessions
        self._web_operations = web_operations
        self.transcripts = web_transcripts
        self.procedures = procedures
        self.status = status
        self.knowledge = knowledge
        self.accounts = accounts
        self.file_retention = file_retention

    @property
    def identity(self) -> WebIdentityApplication:
        return self._web_identity

    @property
    def sessions(self) -> WebSessionApplication:
        return self._web_sessions

    @property
    def operations(self) -> WebOperationsApplication:
        return self._web_operations

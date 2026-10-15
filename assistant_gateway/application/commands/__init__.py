"""Cohesive interactive-command handlers and their application ports."""

from assistant_gateway.application.commands.access import (
    ACCESS_HELP_TEXT,
    AccessBlockInput,
    AccessCommandHandler,
    AccessCommandRequest,
    AccessGrantInput,
    GatewayAccessCommandPort,
    GatewayAccessCommands,
    is_access_command,
)
from assistant_gateway.application.commands.account_link import (
    ACCOUNT_LINK_COMMAND_USAGE,
    AccountLinkCommandHandler,
    AccountLinkCommandRequest,
    GatewayAccountLinkActorResolver,
    GatewayAccountLinkCommands,
    is_account_link_command,
)
from assistant_gateway.application.commands.account_sharing import (
    ACCOUNT_SHARING_USAGE,
    AccountSharingCommandHandler,
    AccountSharingCommandRequest,
    GatewayAccountSharingCommands,
    GatewayAccountSharingPort,
    PERSONAL_SETUP_USAGE,
    is_account_sharing_command,
)
from assistant_gateway.application.commands.authentication import (
    AuthenticationCommandHandler,
    AuthenticationCommandRequest,
    GatewayAuthenticationCommands,
)
from assistant_gateway.application.commands.context import (
    DEBUG_ONLY_COMMAND_NOTICE,
    UNLINKED_USER_NOTICE,
    GatewayActorContext,
    GatewayCommandActorResolver,
    resolve_actor_canonical_user_id,
)
from assistant_gateway.application.commands.identity import (
    GatewayIdentityCommandPort,
    GatewayIdentityCommands,
    IdentityCommandHandler,
    IdentityCommandRequest,
)
from assistant_gateway.application.commands.memory_topic import (
    GatewayMemoryTopicCommandPort,
    GatewayMemoryTopicCommands,
    MemoryTopicCommandHandler,
    MemoryTopicCommandRequest,
    MemoryTopicCommandResult,
)
from assistant_gateway.application.commands.manager_control import (
    GatewayManagerControlCommands,
    ManagerControlCommandRequest,
    ManagerControlCommandResult,
)
from assistant_gateway.application.commands.message_submission import (
    GatewayMessageSubmissionCommands,
    MessageSubmissionCommandRequest,
    MessageSubmissionCommandResult,
    tour_start_request_text,
)
from assistant_gateway.application.commands.session import (
    GatewaySessionCommands,
    SessionCommandRequest,
    SessionCommandResult,
    is_session_command,
)

__all__ = [
    "ACCESS_HELP_TEXT",
    "ACCOUNT_LINK_COMMAND_USAGE",
    "AccessBlockInput",
    "AccessCommandHandler",
    "AccessCommandRequest",
    "AccessGrantInput",
    "AccountLinkCommandHandler",
    "AccountLinkCommandRequest",
    "AuthenticationCommandHandler",
    "AuthenticationCommandRequest",
    "DEBUG_ONLY_COMMAND_NOTICE",
    "GatewayAccessCommandPort",
    "GatewayAccessCommands",
    "GatewayAccountLinkActorResolver",
    "GatewayAccountLinkCommands",
    "GatewayActorContext",
    "GatewayAuthenticationCommands",
    "GatewayCommandActorResolver",
    "GatewayIdentityCommandPort",
    "GatewayIdentityCommands",
    "GatewayMemoryTopicCommandPort",
    "GatewayMemoryTopicCommands",
    "GatewayManagerControlCommands",
    "GatewayMessageSubmissionCommands",
    "GatewaySessionCommands",
    "IdentityCommandHandler",
    "IdentityCommandRequest",
    "MemoryTopicCommandHandler",
    "MemoryTopicCommandRequest",
    "MemoryTopicCommandResult",
    "ManagerControlCommandRequest",
    "ManagerControlCommandResult",
    "MessageSubmissionCommandRequest",
    "MessageSubmissionCommandResult",
    "SessionCommandRequest",
    "SessionCommandResult",
    "UNLINKED_USER_NOTICE",
    "is_access_command",
    "is_account_link_command",
    "ACCOUNT_SHARING_USAGE",
    "AccountSharingCommandHandler",
    "AccountSharingCommandRequest",
    "GatewayAccountSharingCommands",
    "GatewayAccountSharingPort",
    "PERSONAL_SETUP_USAGE",
    "is_account_sharing_command",
    "is_session_command",
    "resolve_actor_canonical_user_id",
    "tour_start_request_text",
]

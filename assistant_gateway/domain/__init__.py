"""Gateway-owned domain records."""

from assistant_gateway.domain.delivery import (
    AttachmentRecord,
    DeliveryRecord,
    ScheduledTargetRecord,
)
from assistant_gateway.domain.identity import (
    GatewayUserAppearanceRecord,
    GatewayUserChannelBindingRecord,
    GatewayUserLinkCodeRecord,
    GatewayUserRecord,
    GatewayUserTeamMembershipRecord,
    GatewayUserWebInviteRecord,
    GatewayUserWebTokenRecord,
)
from assistant_gateway.domain.request_state import (
    PendingQueryRecord,
    PendingQueryStatus,
    RequestRecord,
)
from assistant_gateway.domain.sessions import (
    SessionConversationListRecord,
    SessionConversationRecord,
    SessionListRecord,
    SessionRecord,
    SessionSearchRecord,
)

__all__ = [
    "AttachmentRecord",
    "DeliveryRecord",
    "GatewayUserAppearanceRecord",
    "GatewayUserChannelBindingRecord",
    "GatewayUserLinkCodeRecord",
    "GatewayUserRecord",
    "GatewayUserTeamMembershipRecord",
    "GatewayUserWebInviteRecord",
    "GatewayUserWebTokenRecord",
    "PendingQueryRecord",
    "PendingQueryStatus",
    "RequestRecord",
    "ScheduledTargetRecord",
    "SessionConversationListRecord",
    "SessionConversationRecord",
    "SessionListRecord",
    "SessionRecord",
    "SessionSearchRecord",
]

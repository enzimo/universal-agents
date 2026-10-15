"""Gateway-owned vocabulary for private-resource authorization."""

from __future__ import annotations


PRIVATE_ACTION_CREATE = "private.create"
PRIVATE_ACTION_READ = "private.read"
PRIVATE_ACTION_UPDATE = "private.update"
PRIVATE_ACTION_DELETE = "private.delete"

RESOURCE_GATEWAY_SESSION = "GatewaySession"
RESOURCE_GATEWAY_ATTACHMENT = "GatewayAttachment"
RESOURCE_SCHEDULE = "Schedule"
RESOURCE_USER_MEMORY = "UserMemory"
RESOURCE_TEAM_MEMORY = "TeamMemory"
RESOURCE_CONVERSATION = "Conversation"
RESOURCE_OBJECTIVE = "Objective"
RESOURCE_TASK = "Task"

"""Typed browser task controls using the authenticated manager command path."""

from assistant_gateway.application.operational_control import (
    GatewayStopControl,
    GatewayStopControlRequest,
)
from assistant_gateway.application.request_context import GatewayRequestRoute
from assistant_gateway.application.website_application import (
    WebTaskControlRequestV1,
    WebTaskControlResultV1,
    WebTaskListRequestV1,
    WebTaskListV1,
    WebTaskV1,
)
from custom_messages.contracts.control_v1 import ManagerRuntimeWorkCommand
from custom_messages.contracts.common_v1 import contract_sha256


async def task_command(
    controls: GatewayStopControl,
    request: WebTaskListRequestV1 | WebTaskControlRequestV1,
) -> ManagerRuntimeWorkCommand:
    context = request.context
    if isinstance(request, WebTaskListRequestV1):
        action, task_id, digest = "list_tasks", None, None
        if request.view == "finished":
            action = "list_finished_tasks"
        offset, limit = request.offset, request.limit
    else:
        action = {"remove": "remove_task", "purge": "purge_tasks"}[request.action]
        task_id, digest = request.task_id, request.inventory_digest
        offset, limit = 0, 20
        if request.action == "remove" and not task_id:
            raise ValueError("Choose a task to remove.")
    result = await controls.stop(
        GatewayStopControlRequest(
            session_key=context.session_key,
            action=action,
            scope="user",
            target_task_id=task_id,
            target_agent_id=request.agent_id,
            inventory_digest=digest,
            task_offset=offset,
            task_limit=limit,
            command_text="Settings Tasks: " + action,
            route=GatewayRequestRoute(
                channel_name=context.channel_name,
                user_id=context.user_id,
                chat_id=context.chat_id,
                canonical_user_id=context.canonical_user_id,
            ),
            reason="Task removal requested in Settings by its authenticated owner.",
        )
    )
    content = getattr(result.raw_envelope, "msg_content", None)
    if not isinstance(content, ManagerRuntimeWorkCommand):
        raise RuntimeError(
            result.text or "The agent did not return task state. Refresh when it is available."
        )
    return content


async def list_tasks(
    controls: GatewayStopControl, request: WebTaskListRequestV1
) -> WebTaskListV1:
    response = await task_command(controls, request)
    if not response.success:
        raise RuntimeError(response.message or "Tasks could not be loaded.")
    if request.view == "finished":
        return WebTaskListV1(
            tasks=tuple(WebTaskV1(
                root_task_id=row.task_id, state=row.state, problem_statement=row.problem_statement,
                is_current=False, latest_activity_at=row.finished_at, answer=row.answer,
                owner_agent_id=row.owner_agent_id, owner_agent_name=row.owner_agent_name,
            ) for row in response.finished_tasks),
            total=response.task_total, queued_count=0, current_count=0,
            inventory_digest=contract_sha256({"finished_tasks": [row.model_dump(mode="json") for row in response.finished_tasks]}),
        )
    if response.inventory_digest is None:
        raise RuntimeError("The agent returned an incomplete task inventory. Refresh to retry.")
    return WebTaskListV1(
        tasks=tuple(
            WebTaskV1(
                root_task_id=root.root_task_id,
                state=root.state.value,
                problem_statement=root.problem_statement,
                is_current=root.is_current,
                is_parallel=root.is_parallel,
                queue_position=root.queue_position,
                latest_activity_at=root.latest_activity_at,
                latest_activity=root.latest_activity,
                blocker=root.blocker,
            )
            for root in response.roots
        ),
        total=response.task_total,
        queued_count=response.queued_task_count,
        current_count=response.current_task_count,
        inventory_digest=response.inventory_digest,
    )


async def control_tasks(
    controls: GatewayStopControl, request: WebTaskControlRequestV1
) -> WebTaskControlResultV1:
    response = await task_command(controls, request)
    return WebTaskControlResultV1(
        success=bool(response.success),
        message=response.message,
        cancellation_receipts=tuple(
            receipt.model_dump(mode="json")
            for receipt in response.cancellation_receipts
        ),
    )

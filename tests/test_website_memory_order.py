"""Memory pages use current update times, including edits and tied timestamps."""

from types import SimpleNamespace

import pytest

from assistant_gateway.application.command_operations import GatewayPinnedMemoryOperationResult
from assistant_gateway.application.route_context import GatewayRouteContextV1
from assistant_gateway.application.website_application import WebMemoryListRequestV1
from assistant_gateway.application.website_services import WebsiteOperationsApplication
from custom_agents.pinned_memory_runtime import PinnedMemoryCommand, PinnedMemoryService
from custom_messages.contracts.control_v1 import PinnedMemoryCommandResultPayload
from tests.test_pinned_memory_repository import _context, _repo


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["team"])
async def test_memory_pages_show_latest_updates_before_pagination(tmp_path, monkeypatch, scope):
    repository = _repo(tmp_path)
    service = PinnedMemoryService(repository)
    context = _context()
    bank = "usermem" if scope == "personal" else "coremem"
    now = "2026-01-01T00:00:00+00:00"
    monkeypatch.setattr("storage.pinned_memory_repository.utc_now_iso", lambda: now)
    service.execute(PinnedMemoryCommand(bank=bank, action="create", fact_text="Old fact"), context=context)
    now = "2026-01-02T00:00:00+00:00"
    for fact in ("Second fact", "Third fact"):
        service.execute(PinnedMemoryCommand(bank=bank, action="create", fact_text=fact), context=context)
    now = "2026-01-03T00:00:00+00:00"
    service.execute(PinnedMemoryCommand(bank=bank, action="replace", memory_id=1, fact_text="Edited fact"), context=context)

    async def execute(**request):
        result = service.execute(PinnedMemoryCommand(**request["command"].model_dump()), context=context)
        return GatewayPinnedMemoryOperationResult(
            result=PinnedMemoryCommandResultPayload.model_validate(result),
            available_actions=("edit", "delete"),
        )

    app = WebsiteOperationsApplication(
        team=None, mcp=None, event_output=None, operational_controls=None, schedules=None,
        memory_operations=SimpleNamespace(execute_pinned_memory_operation=execute), workspace_browser=None,
    )
    request = WebMemoryListRequestV1(
        context=GatewayRouteContextV1(session_key="session", canonical_user_id=context.canonical_user_id),
        scope=scope, limit=2,
    )
    first = await app.list_web_chat_memories(request)
    second = await app.list_web_chat_memories(request.model_copy(update={"offset": 2}))
    assert [fact.fact_text for fact in first.memories] == ["Edited fact", "Third fact"]
    assert [fact.fact_text for fact in second.memories] == ["Second fact"]
    assert first.total == second.total == 3
    assert first.has_more and not second.has_more
    # Agent hydration and callers that omit a sort retain their stable ID order.
    default = service.execute(PinnedMemoryCommand(bank=bank, action="list"), context=context)
    assert [fact["memory_id"] for fact in default["facts"]] == [1, 2, 3]

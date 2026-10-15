"""The tool catalog classifies every callable operation.

These tests fail when a function tool or broker action has no level, when a
definition names something that does not exist, or when a classifier would give
a lower level than the operation needs. Unclassified operations need full.
"""

from __future__ import annotations

import pytest

from access_catalog.catalog import (
    DEFINITIONS_DIR,
    UNLISTED_WEB_TOOL,
    UNREGISTERED_FUNCTION_TOOL,
    load_catalog,
)
from access_catalog.cli_tables import load_cli_policy
from access_catalog.http_routes import canonical_path_segments, classify_http_request
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    ClassificationSource,
    HttpRouteV1,
    OwnerScope,
    ToolDefinitionV1,
    ToolKind,
)
from custom_tools.provider_contract import tool_name
from custom_tools.registry import TOOL_PROVIDERS
from secure_tools.registry import get_provider, list_providers

READ, WRITE, FULL = AccessLevel.READ, AccessLevel.WRITE, AccessLevel.FULL


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


def test_every_registered_function_has_a_level(catalog):
    registered = {tool_name(tool) for provider in TOOL_PROVIDERS for tool in provider.tools}
    classified = set(catalog.functions) | set(catalog.function_dispatchers)

    assert registered - classified == set(), "functions without a level"
    # The skills plugin registers 'skills' outside the providers.
    assert classified - registered == {"skills"}, "catalog names functions that do not exist"


def test_functions_map_to_the_tool_of_their_provider(catalog):
    for provider in TOOL_PROVIDERS:
        for tool in provider.tools:
            binding = catalog.functions.get(tool_name(tool))
            if binding is None:
                continue
            tool_entry = catalog.tools[binding.tool_id]
            assert tool_entry.kind in {ToolKind.FN, ToolKind.CODE}
            if tool_entry.kind is ToolKind.FN:
                assert binding.tool_id == f"fn:{provider.name}"


def test_action_arguments_exist_and_list_every_supported_value(catalog):
    by_name = {tool_name(tool): tool for provider in TOOL_PROVIDERS for tool in provider.tools}
    expected_values = {
        "python_env": {"create", "write_file", "inspect", "install", "run", "list", "delete"},
        # plan_install and plan_remove only stage changes a person approves; publishing is not offered.
        "hosted_mcp_servers": {"list", "propose", "start", "stop", "plan_install", "plan_remove"},
        "mcp_connections": {"list", "propose"},
        "installed_permissions": {"list", "propose"},
        "specialist_definitions": {"plan", "status", "plan_removal"},
        "objectives": {"get", "list", "update_progress", "update_status"},
        "objective_work_sessions": {"suggest", "enable", "disable", "status", "start"},
        "objective_work_profiles": {"list", "export", "apply", "remove"},
        "maintain_memory_review_jobs": {"archive", "purge"},
        # Action tools (custom_tools.action_inputs) that replaced several functions each.
        "memory": {"list", "create", "replace", "delete", "undo"},
        "topic_tags": {"list", "tag_thread", "tag_turn", "untag", "find"},
    }
    split = {name: binding for name, binding in catalog.functions.items() if binding.argument}
    assert set(split) == set(expected_values)
    for name, binding in split.items():
        properties = by_name[name].tool_spec["inputSchema"]["json"]["properties"]
        assert binding.argument in properties
        assert set(binding.values) == expected_values[name]
        # A tool that lists its actions offers exactly the ones with a level.
        offered = properties[binding.argument].get("enum")
        assert offered is None or set(offered) == set(binding.values), name


def test_memory_actions_keep_the_levels_of_the_functions_they_replaced(catalog):
    from custom_messages.contracts.access_v2 import AccessLevel

    levels = {name: level for name, level in catalog.functions["memory"].values.items()}
    assert levels == {
        "list": AccessLevel.READ, "create": AccessLevel.WRITE, "replace": AccessLevel.WRITE,
        "delete": AccessLevel.FULL, "undo": AccessLevel.WRITE,
    }
    assert catalog.resolve_function("memory", {"action": "delete", "bank": "usermem"}).level is AccessLevel.FULL
    unknown = catalog.resolve_function("memory", {"action": "purge"})
    assert (unknown.operation, unknown.level) == ("memory.purge", AccessLevel.FULL), "an unlisted action needs full"
    assert catalog.resolve_function("topic_tags", {"action": "find"}).level is AccessLevel.READ


def test_every_staged_change_kind_has_a_catalog_level(catalog):
    """A kind of change applied with apply_change (or decided in the gateway) needs its catalog entry."""

    from custom_agents.change_approvals import CHANGE_ADAPTERS
    from custom_agents.installed_permissions import INSTALLED_PROCEDURE_CHANGE_KIND
    from custom_agents.model_choice import MODEL_CHOICE_CHANGE_KIND, MODEL_CHOICE_TOOL_ID

    kinds = {adapter.kind for adapter in CHANGE_ADAPTERS} | {INSTALLED_PROCEDURE_CHANGE_KIND, MODEL_CHOICE_CHANGE_KIND}

    assert kinds - set(catalog.changes) == set(), "change kinds without a catalog level"
    for kind in ("hosted_mcp_server", INSTALLED_PROCEDURE_CHANGE_KIND):
        assert catalog.changes[kind] == ("fn:capabilities", FULL), kind
    # Installing an MCP server is an access proposal an administrator reviews (plan 7d).
    assert "mcp_connection_activation" not in catalog.changes
    assert catalog.changes["access_proposal"] == ("fn:access_proposals", FULL)
    # Model choice is its own administrator-only tool, never a platform one (owner's decision 2026-09-27).
    assert catalog.changes[MODEL_CHOICE_CHANGE_KIND] == (MODEL_CHOICE_TOOL_ID, WRITE)
    assert catalog.tools[MODEL_CHOICE_TOOL_ID].owner_scope.value == "administrators"


def test_unknown_functions_and_values_need_full(catalog):
    unknown = catalog.resolve_function("brand_new_tool")
    odd_value = catalog.resolve_function("python_env", {"action": "exfiltrate"})

    assert (unknown.tool_id, unknown.level, unknown.source) == (
        UNREGISTERED_FUNCTION_TOOL,
        FULL,
        ClassificationSource.UNREVIEWED,
    )
    assert (odd_value.level, odd_value.source) == (FULL, ClassificationSource.UNREVIEWED)


@pytest.mark.parametrize(
    ("function", "arguments", "level"),
    [
        ("file_read", {}, READ),
        ("file_write", {}, WRITE),
        ("memory", {"action": "delete", "bank": "usermem"}, FULL),
        ("shell", {}, WRITE),
        ("python_env", {"action": "list"}, READ),
        ("python_env", {"action": "run"}, WRITE),
        ("apply_change", {}, WRITE),
        # Typing needs a person's approval per session, checked by the SSH broker.
        ("write_execution_ssh_session", {}, WRITE),
        ("plan_execution_ssh_interaction", {}, WRITE),
    ],
)
def test_function_levels(catalog, function, arguments, level):
    assert catalog.resolve_function(function, arguments).level is level


def test_every_broker_action_has_a_level(catalog):
    missing = []
    for provider_name in list_providers():
        provider = get_provider(provider_name)
        for action in provider.actions:
            if catalog.broker_dispatch_providers.get(provider_name):
                continue
            if (provider_name, action) not in catalog.broker_actions:
                missing.append(f"{provider_name}.{action}")
    stale = [
        f"{provider}.{action}"
        for provider, action in catalog.broker_actions
        if get_provider(provider) is None or action not in get_provider(provider).actions
    ]

    assert missing == [], "broker actions without a level"
    assert stale == [], "catalog names broker actions that do not exist"


def test_code_and_platform_tools_are_never_standard(catalog):
    for tool in catalog.tools_in_set("standard"):
        assert tool.kind is not ToolKind.CODE
        assert tool.owner_scope is not OwnerScope.PLATFORM


@pytest.mark.parametrize(
    ("provider", "action", "params", "tool_id", "level"),
    [
        ("google", "gmail_search", {}, "cli:google", READ),
        ("google", "gmail_send", {}, "cli:google", WRITE),
        ("google", "gog.exec", {"argv": ["gmail", "messages", "search", "x"]}, "cli:google", READ),
        ("google", "gog.exec", {"argv": ["drive", "delete", "id"]}, "cli:google", FULL),
        ("google", "gog.exec", {"argv": ["gmail", "settings", "forwarding", "create", "x"]}, "cli:google", FULL),
        ("google", "gog.exec", {"argv": ["brandnew", "command"]}, "cli:google", FULL),
        ("google", "gog.exec", {"argv": ["gmail", "--max", "5", "search", "x"]}, "cli:google", FULL),
        ("github", "gh.exec", {"argv": ["issue", "list"]}, "cli:github", READ),
        ("github", "gh.exec", {"argv": ["api", "repos/o/r"]}, "cli:github", READ),
        ("github", "gh.exec", {"argv": ["api", "graphql", "-f", "query=x"]}, "cli:github", FULL),
        ("github", "gh.exec", {"argv": ["api", "-XDELETE", "x"]}, "cli:github", FULL),
        ("github", "api.post", {}, "cli:github", FULL),
        ("secure_requests", "get", {"url": "https://api.github.com/user"}, "http:github", READ),
        ("secure_requests", "post", {"url": "https://api.github.com/x"}, "http:github", WRITE),
        ("secure_requests", "delete", {"url": "https://api.github.com/x"}, "http:github", FULL),
        ("secure_requests", "get", {"url": "https://example.net/x"}, UNLISTED_WEB_TOOL, FULL),
        # Web search and crawling: searches are POSTs, so their routes are read.
        ("secure_requests", "request", {"method": "POST", "url": "https://api.exa.ai/search"}, "http:exa", READ),
        ("secure_requests", "post", {"url": "https://api.exa.ai/contents"}, "http:exa", READ),
        ("secure_requests", "post", {"url": "https://api.exa.ai/websets"}, "http:exa", WRITE),
        ("secure_requests", "get", {"url": "https://api.search.brave.com/res/v1/web/search?q=x"}, "http:brave-search", READ),
        ("secure_requests", "post", {"url": "http://127.0.0.1:11235/crawl"}, "http:crawl4ai", READ),
        ("secure_requests", "post", {"url": "http://127.0.0.1:11236/crawl"}, UNLISTED_WEB_TOOL, FULL),
        (
            "credential_catalog",
            "credential.api_request",
            {"url": "https://api.github.com/user", "method": "GET"},
            "http:github",
            READ,
        ),
    ],
)
def test_broker_action_resolution(catalog, provider, action, params, tool_id, level):
    resolution = catalog.resolve_broker_action(provider, action, params)

    assert (resolution.tool_id, resolution.level) == (tool_id, level)


@pytest.mark.parametrize("cli", ["gh", "gog"])
def test_command_tables_load_and_blocked_commands_have_no_level(cli):
    policy = load_cli_policy(cli)

    assert policy.table.commands
    for path in policy.table.commands:
        assert policy.blocked_reason(path) is None


def test_gog_read_level_enables_only_read_commands():
    table = load_cli_policy("gog").table
    read_paths = set(table.paths_up_to(READ))

    assert {"gmail.search", "gmail.messages.search", "drive.ls", "docs.cat"} <= read_paths
    assert not {"gmail.send", "drive.delete", "drive.upload", "admin.users.list"} & read_paths


# Web API routes -------------------------------------------------------------

PORTAINER = ToolDefinitionV1(
    tool_id="http:portainer",
    kind=ToolKind.HTTP,
    display_name="Portainer",
    hosts=("portainer.example.test",),
    routes=(
        HttpRouteV1(name="stacks.restart", method="POST", path="/api/stacks/{id}/restart", level=WRITE),
        HttpRouteV1(name="admin", method="GET", path="/api/admin/settings", level=FULL),
    ),
)


@pytest.mark.parametrize(
    ("method", "path", "operation", "level"),
    [
        ("GET", "/api/stacks", "get", READ),
        ("HEAD", "/api/stacks", "head", READ),
        ("POST", "/api/stacks/7/restart", "route:stacks.restart", WRITE),
        ("PUT", "/api/stacks/7", "put", WRITE),
        ("DELETE", "/api/stacks/7", "delete", FULL),
        ("TRACE", "/api/stacks", "trace", FULL),
        ("GET", "/api/admin/settings", "route:admin", FULL),
        # Traversal and encodings cannot make the admin route look like a plain GET.
        ("GET", "/api/public/../admin/settings", "get:unrecognized_path", FULL),
        ("GET", "/api/%2e%2e/admin/settings", "get:unrecognized_path", FULL),
        ("GET", "/api//admin/settings", "get:unrecognized_path", FULL),
        ("GET", "/api/stacks?x=/../admin", "get", READ),
    ],
)
def test_http_route_classification(method, path, operation, level):
    resolution = classify_http_request(PORTAINER, method, path)

    assert (resolution.operation, resolution.level) == (operation, level)


def test_canonical_path_segments():
    assert canonical_path_segments("/a/b/") == ("a", "b")
    assert canonical_path_segments("/") == ()
    assert canonical_path_segments("/a/./b") is None
    assert canonical_path_segments("a/b") is None


def test_instance_definitions_may_only_add_web_api_tools(tmp_path):
    tools_dir = tmp_path / "control" / "access" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "portainer.yaml").write_text(
        "tools:\n"
        "  - tool_id: http:portainer\n"
        "    display_name: Portainer\n"
        "    hosts: [portainer.example.test]\n"
        "    routes:\n"
        "      - {name: stacks.restart, method: POST, path: '/api/stacks/{id}/restart', level: write}\n",
        encoding="utf-8",
    )
    catalog = load_catalog(tmp_path)

    restart = catalog.resolve_http("POST", "https://portainer.example.test/api/stacks/3/restart")
    assert (restart.tool_id, restart.operation, restart.level) == ("http:portainer", "route:stacks.restart", WRITE)

    (tools_dir / "sneaky.yaml").write_text(
        "tools:\n  - tool_id: fn:files\n    display_name: Files\n    functions: {file_read: read}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="only add web API tools"):
        load_catalog(tmp_path)


def test_unreviewed_operations_must_require_full():
    from custom_messages.contracts.access_v2 import OperationDefinitionV1

    with pytest.raises(ValueError, match="must require full"):
        OperationDefinitionV1(name="x", level=READ, source=ClassificationSource.UNREVIEWED)


def test_installed_procedures_use_their_trusted_level(tmp_path):
    from access_catalog.catalog import ToolCatalog

    handlers = tmp_path / "handlers.yaml"
    handlers.write_text(
        "schema_version: 1\n"
        "handlers:\n"
        "  cache.invalidate:\n"
        "    display_name: Invalidate one cache\n"
        "    impact_description: Clears one named cache.\n"
        "    level: write\n",
        encoding="utf-8",
    )
    catalog = ToolCatalog()
    catalog.load_file(DEFINITIONS_DIR / "functions.yaml")
    catalog.load_procedures(handlers)

    known = catalog.resolve_function(
        "invoke_installed_permission", {"definition_id": "installed_permission.cache.invalidate.0123456789abcdef"}
    )
    forged = catalog.resolve_function("invoke_installed_permission", {"definition_id": "something.else"})

    assert (known.tool_id, known.level) == ("procedure:cache.invalidate", WRITE)
    assert (forged.level, forged.source) == (FULL, ClassificationSource.UNREVIEWED)


def test_web_search_tools_use_linked_accounts_and_the_internet_set(catalog):
    """Every agent and person holds the internet set at read, so these need no grant or request."""

    from custom_messages.contracts.access_v2 import AccountMode

    for tool_id, field in (("http:exa", "api_key"), ("http:brave-search", "api_key"), ("http:crawl4ai", "api_token")):
        tool = catalog.tools[tool_id]
        assert tool.account is AccountMode.LINKED_CREDENTIAL
        assert "internet" in tool.tool_sets
        assert [injection.field for injection in tool.credential_injections] == [field]


def test_a_service_tool_matches_only_the_configured_address():
    from access_catalog.catalog import load_catalog

    catalog = load_catalog(None, service_urls={"web_services.crawl4ai.base_url": "https://crawl.example.test"})

    assert catalog.tools["http:crawl4ai"].hosts == ("crawl.example.test",)
    assert catalog.resolve_http("POST", "https://crawl.example.test:443/crawl").tool_id == "http:crawl4ai"
    assert catalog.resolve_http("POST", "https://crawl.example.test/crawl").tool_id == "http:crawl4ai"
    assert catalog.resolve_http("POST", "http://crawl.example.test/crawl").tool_id == UNLISTED_WEB_TOOL
    assert catalog.resolve_http("POST", "https://crawl.example.test:8443/crawl").tool_id == UNLISTED_WEB_TOOL


def test_instance_files_may_not_take_an_address_from_a_setting(tmp_path):
    from access_catalog.catalog import CatalogError, load_catalog

    tools = tmp_path / "control" / "access" / "tools"
    tools.mkdir(parents=True)
    (tools / "own.yaml").write_text(
        "tools:\n  - tool_id: http:own\n    service_url_setting: web_services.crawl4ai.base_url\n",
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="only built-in web API tools"):
        load_catalog(tmp_path)



def test_holding_the_web_search_tools_offers_their_functions_not_the_broker_dispatcher(catalog):
    """Agents reach http:exa and the others through fn:web_research, so internet access adds no secure_cli."""

    from access_catalog.tool_functions import functions_of_tool_sets, functions_of_tools

    assert "secure_cli" not in functions_of_tool_sets(catalog, ["internet"])
    assert "secure_cli" not in functions_of_tools(catalog, ["http:exa", "http:brave-search", "http:crawl4ai"])
    assert "secure_cli" in functions_of_tools(catalog, ["http:github"])
    assert {tool: catalog.reached_through.get(tool) for tool in ("http:exa", "http:brave-search", "http:crawl4ai")} == {
        "http:exa": "fn:web_research",
        "http:brave-search": "fn:web_research",
        "http:crawl4ai": "fn:web_research",
    }


def test_sign_in_tools_match_the_catalog_and_have_a_sign_in_page(catalog):
    """``/link <tool>`` starts a sign-in only where the broker has one (secure_tools.auth_methods)."""

    from secure_tool_broker.auth_flows import get_auth_flow
    from secure_tool_broker.service import FORM_AUTH_METHODS
    from custom_messages.contracts.access_v2 import AccountMode
    from secure_tools.auth_methods import SIGN_IN_TOOLS

    personal = {tool_id for tool_id, tool in catalog.tools.items() if tool.account is AccountMode.PERSONAL_LOGIN}
    assert personal == {tool_id for tool_id, sign_in in SIGN_IN_TOOLS.items() if not sign_in.team_link}
    for tool_id, sign_in in SIGN_IN_TOOLS.items():
        expected = AccountMode.LINKED_CREDENTIAL if sign_in.team_link else AccountMode.PERSONAL_LOGIN
        assert catalog.tools[tool_id].account is expected, tool_id
        assert sign_in.default_method in sign_in.methods
        for method in sign_in.methods:
            has_page = get_auth_flow(sign_in.provider, method) is not None
            assert has_page or method in FORM_AUTH_METHODS.get(sign_in.provider, set()), (tool_id, method)

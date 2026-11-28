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
    UNPARSED_COMMAND_OPERATION,
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


def test_timed_wait_tools_have_expected_catalog_classification(catalog):
    wait_tool = catalog.tools["fn:wait"]
    long_wait_tool = catalog.tools["fn:long_wait"]
    assert wait_tool.tool_sets == ("agent_runtime",)
    assert catalog.resolve_function("wait", {}).tool_id == "fn:wait"
    assert catalog.resolve_function("wait", {}).level is READ
    assert long_wait_tool.tool_sets == ()
    assert catalog.resolve_function("long_wait", {}).tool_id == "fn:long_wait"
    assert catalog.resolve_function("long_wait", {}).level is WRITE


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


def _function_tool_catalog(tmp_path, functions_yaml: str):
    """A catalog of one fn:notes tool whose functions are the given YAML lines."""

    from access_catalog.catalog import ToolCatalog

    path = tmp_path / "notes.yaml"
    path.write_text(
        "tools:\n  - tool_id: fn:notes\n    provider: notes\n    display_name: Notes\n    functions:\n" + functions_yaml,
        encoding="utf-8",
    )
    catalog = ToolCatalog()
    catalog.load_file(path)
    return catalog


def test_record_kinds_name_the_calls_that_change_records(tmp_path):
    from custom_messages.enums import ChangeRecordKind

    catalog = _function_tool_catalog(
        tmp_path,
        "      note:\n"
        "        argument: action\n"
        "        values: {list: read, edit: write, tag: write}\n"
        "        record_kinds: {edit: memory_fact, tag: none}\n"
        "      note_file: {level: write, record_kind: file}\n"
        "      note_search: read\n",
    )

    assert catalog.record_kind("note", {"action": "Edit"}) is ChangeRecordKind.MEMORY_FACT
    assert catalog.record_kind("note", {"action": "tag"}) is None
    assert catalog.record_kind("note", {"action": "list"}) is None
    assert catalog.record_kind("note_file") is ChangeRecordKind.FILE
    assert catalog.record_kind("note_search") is None
    assert catalog.record_kind("unknown_function") is None
    # Levels are decided as before.
    assert catalog.resolve_function("note_file").level is WRITE


@pytest.mark.parametrize(
    ("functions_yaml", "message"),
    [
        # A changing operation in a tool that has record kinds must name one (or none).
        (
            "      note_file: {level: write, record_kind: file}\n      note_delete: full\n",
            r"without a record kind.*note_delete",
        ),
        (
            "      note:\n        argument: action\n        values: {edit: write, drop: full}\n"
            "        record_kinds: {edit: file}\n",
            r"without a record kind.*note\.drop",
        ),
        ("      note_file: {level: write, record_kind: page}\n", "unknown record kind"),
        ("      note_file: {level: write, records: file}\n", "unknown keys"),
        ("      note_file: {record_kind: file}\n", "missing keys"),
        (
            "      note:\n        argument: action\n        values: {edit: write}\n        record_kinds: {move: file}\n",
            "values it does not take",
        ),
        ("      note:\n        argument: action\n        values: {edit: write}\n        level: write\n", "unknown keys"),
    ],
)
def test_the_loader_refuses_unclear_record_kinds(tmp_path, functions_yaml, message):
    from access_catalog.catalog import CatalogError

    with pytest.raises(CatalogError, match=message):
        _function_tool_catalog(tmp_path, functions_yaml)


def test_tools_without_record_kinds_need_none(tmp_path):
    catalog = _function_tool_catalog(tmp_path, "      note_file: write\n      note_delete: full\n")

    assert catalog.record_kind("note_file") is None


def test_execution_ssh_tools_and_change_kinds_have_expected_catalog_levels(catalog):
    expected_functions = {
        "probe_execution_ssh_host_key": READ,
        "plan_execution_ssh_target": WRITE,
        "list_execution_ssh_targets": READ,
        "create_execution_ssh_session": WRITE,
        "open_execution_ssh_session": WRITE,
        "read_execution_ssh_session": READ,
        "plan_execution_ssh_interaction": WRITE,
        "write_execution_ssh_session": WRITE,
        "resize_execution_ssh_session": WRITE,
        "close_execution_ssh_session": WRITE,
        "list_execution_ssh_sessions": READ,
        "run_execution_ssh_command": WRITE,
        "get_execution_ssh_command": READ,
        "enroll_execution_ssh_key": WRITE,
        "run_execution_ssh_sudo": WRITE,
        "upload_execution_ssh_bundle": WRITE,
    }
    for fn_name, expected_level in expected_functions.items():
        resolved = catalog.resolve_function(fn_name)
        assert resolved.tool_id == "fn:execution_control", fn_name
        assert resolved.level is expected_level, (fn_name, resolved.level, expected_level)
    expected_broker_actions = {
        "probe_execution_ssh_host_key": READ,
        "open_execution_ssh_session": WRITE,
        "read_execution_ssh_session": READ,
        "write_execution_ssh_session": WRITE,
        "resize_execution_ssh_session": WRITE,
        "close_execution_ssh_session": WRITE,
        "run_execution_ssh_command": WRITE,
        "get_execution_ssh_command": READ,
        "enroll_execution_ssh_key": WRITE,
        "run_execution_ssh_sudo": WRITE,
        "upload_execution_ssh_bundle": WRITE,
    }
    for action_name, expected_level in expected_broker_actions.items():
        resolved_broker = catalog.resolve_broker_action("execution_ssh", action_name, {})
        assert resolved_broker.tool_id == "fn:execution_control", action_name
        assert resolved_broker.operation == action_name
        assert resolved_broker.level is expected_level, (action_name, resolved_broker.level, expected_level)
    assert catalog.changes["execution_ssh_target_enroll"] == ("fn:execution_control", FULL)
    assert catalog.changes["execution_ssh_interaction"] == ("fn:execution_control", FULL)


# Gate injections, link purpose, canonical injection matching, and tool changes ----


def test_canonical_injections_and_injections_match_across_tool_kinds():
    from custom_messages.contracts.access_v2 import (
        CredentialInjectionV1,
        InjectionPlacement,
        canonical_injections,
        injections_match,
    )

    # HTTP: order-insensitive, header-case-insensitive, explicit default template matches default.
    http_tool = (
        CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-API-Key", field="token"),
        CredentialInjectionV1(placement=InjectionPlacement.QUERY, name="apiKey", field="key", template="v1-{value}"),
    )
    http_link = (
        CredentialInjectionV1(placement=InjectionPlacement.QUERY, name="apiKey", field="key", template="v1-{value}"),
        CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="x-api-key", field="token", template="{value}"),
    )
    assert injections_match(http_link, http_tool)
    assert canonical_injections(http_link) == canonical_injections(http_tool)
    # Query names, templates, and field names are significant.
    assert not injections_match(
        (CredentialInjectionV1(placement=InjectionPlacement.QUERY, name="apikey", field="key"),),
        (CredentialInjectionV1(placement=InjectionPlacement.QUERY, name="apiKey", field="key"),),
    )
    assert not injections_match(
        (CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="Authorization", field="token", template="{value}"),),
        (CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="Authorization", field="token", template="Bearer {value}"),),
    )
    assert not injections_match(
        (CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-API-Key", field="other"),),
        (CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-API-Key", field="token"),),
    )

    # Installed CLI: env and file names are case-sensitive, order is ignored.
    cli_tool = (
        CredentialInjectionV1(placement=InjectionPlacement.ENV, name="GITEA_TOKEN", field="token"),
        CredentialInjectionV1(placement=InjectionPlacement.FILE, name=".config/tea/config.yml", field="cfg", template="token: {value}\n"),
    )
    cli_link = (cli_tool[1], cli_tool[0])
    assert injections_match(cli_link, cli_tool)
    assert not injections_match(
        (CredentialInjectionV1(placement=InjectionPlacement.ENV, name="gitea_token", field="token"),),
        (CredentialInjectionV1(placement=InjectionPlacement.ENV, name="GITEA_TOKEN", field="token"),),
    )

    # Fixed-field built-in tools (empty injections) match empty injections.
    assert injections_match((), ())


def test_gate_injections_and_link_purpose_contract_rules():
    from datetime import datetime, timezone

    from custom_messages.contracts.access_v2 import (
        AccessProposalV1,
        AccountLinkRequestV1,
        AccountLinkV1,
        AccountMode,
        CredentialInjectionV1,
        InjectionPlacement,
        LinkOwnerKind,
        LinkPurpose,
        ProposedAccountLinkV1,
        ProposedHttpToolChangeV1,
        ProposedHttpToolV1,
        SubjectKind,
        SubjectRefV1,
    )
    from custom_messages.contracts.tool_setup_spec_v1 import ExternalSetupStepV1, ToolSetupSpecV1

    gate_hdr = CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate-Secret", field="secret")
    acct_hdr = CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="Authorization", field="token", template="Bearer {value}")
    setup_spec = ToolSetupSpecV1(
        external_steps=(
            ExternalSetupStepV1(step_id="create_secret", title="Create secret", instructions="Copy the Cloudflare gate header."),
        )
    )

    # Allowed on HTTP tool with or without an account, and allows external setup steps without an account.
    no_acct = ToolDefinitionV1(
        tool_id="http:gated-public",
        kind=ToolKind.HTTP,
        display_name="Gated Public",
        hosts=("public.example.com",),
        account=AccountMode.NONE,
        gate_injections=(gate_hdr,),
        setup=setup_spec,
    )
    assert no_acct.gate_injections == (gate_hdr,)

    with_acct = ProposedHttpToolV1(
        tool_id="http:gitea",
        display_name="Gitea",
        hosts=("gitea.example.com",),
        uses_credential=True,
        credential_injections=(acct_hdr,),
        gate_injections=(gate_hdr,),
    )
    assert with_acct.gate_injections == (gate_hdr,)

    # Refused on non-HTTP tools, non-header/query placements, duplicates, and account overlaps.
    with pytest.raises(ValueError, match="Only web API tools"):
        ToolDefinitionV1(tool_id="cli:demo", kind=ToolKind.CLI, display_name="Demo", gate_injections=(gate_hdr,))
    with pytest.raises(ValueError, match="header or query"):
        ToolDefinitionV1(
            tool_id="http:bad",
            kind=ToolKind.HTTP,
            display_name="Bad",
            hosts=("bad.example.com",),
            gate_injections=(CredentialInjectionV1(placement=InjectionPlacement.ENV, name="GATE", field="s"),),
        )
    with pytest.raises(ValueError, match="duplicate gate injection"):
        ToolDefinitionV1(
            tool_id="http:dup",
            kind=ToolKind.HTTP,
            display_name="Dup",
            hosts=("dup.example.com",),
            gate_injections=(
                gate_hdr,
                CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="x-gate-secret", field="s2"),
            ),
        )
    with pytest.raises(ValueError, match="both account and gate"):
        ProposedHttpToolV1(
            tool_id="http:overlap",
            display_name="Overlap",
            hosts=("overlap.example.com",),
            uses_credential=True,
            credential_injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate-Secret", field="a"),),
            gate_injections=(CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="x-gate-secret", field="b"),),
        )
    with pytest.raises(ValueError, match="uses a credential"):
        ProposedHttpToolV1(
            tool_id="http:no-cred",
            display_name="No Cred",
            hosts=("nocred.example.com",),
            uses_credential=False,
            credential_injections=(acct_hdr,),
        )

    # Gate link contracts enforce team ownership, http:* tool_id, and non-empty header/query injections.
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    actor = SubjectRefV1(kind=SubjectKind.USER, id="usr_admin")
    gate_link = AccountLinkV1(
        link_id="lnk_gate_1",
        team_id="team_a",
        tool_id="http:gitea",
        owner_kind=LinkOwnerKind.TEAM,
        owner_id="team_a",
        purpose=LinkPurpose.GATE,
        credential_key="http-gitea-gate",
        injections=(gate_hdr,),
        created_by=actor,
        created_at_utc=now,
    )
    assert gate_link.purpose is LinkPurpose.GATE
    with pytest.raises(ValueError, match="belongs to the team"):
        AccountLinkV1(
            link_id="lnk_bad_1",
            team_id="team_a",
            tool_id="http:gitea",
            owner_kind=LinkOwnerKind.PERSON,
            owner_id="usr_alice",
            purpose=LinkPurpose.GATE,
            credential_key="k",
            injections=(gate_hdr,),
            created_by=actor,
            created_at_utc=now,
        )
    with pytest.raises(ValueError, match="Only web API tools"):
        AccountLinkV1(
            link_id="lnk_bad_2",
            team_id="team_a",
            tool_id="mcp:demo",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id="team_a",
            purpose=LinkPurpose.GATE,
            credential_key="k",
            injections=(gate_hdr,),
            created_by=actor,
            created_at_utc=now,
        )
    with pytest.raises(ValueError, match="at least one gate injection"):
        AccountLinkV1(
            link_id="lnk_bad_3",
            team_id="team_a",
            tool_id="http:gitea",
            owner_kind=LinkOwnerKind.TEAM,
            owner_id="team_a",
            purpose=LinkPurpose.GATE,
            credential_key="k",
            injections=(),
            created_by=actor,
            created_at_utc=now,
        )
    with pytest.raises(ValueError, match="belongs to the team"):
        AccountLinkRequestV1(
            request_id="req_1",
            team_id="team_a",
            tool_id="http:gitea",
            owner_kind=LinkOwnerKind.PERSON,
            purpose=LinkPurpose.GATE,
            person_id="usr_alice",
        )
    with pytest.raises(ValueError, match="belongs to the team"):
        ProposedAccountLinkV1(tool_id="http:gitea", owner="person", purpose="Gate", link_purpose=LinkPurpose.GATE)

    # ProposedHttpToolChangeV1 and AccessProposalV1.tool_changes.
    change = ProposedHttpToolChangeV1(
        tool_id="http:gitea",
        display_name="Gitea",
        hosts=("gitea.example.com",),
        uses_credential=True,
        credential_injections=(acct_hdr,),
        gate_injections=(gate_hdr,),
        previous_definition_digest="a" * 64,
    )
    proposal = AccessProposalV1(title="Add Cloudflare gate", reason="Reach gated Gitea.", tool_changes=(change,))
    assert proposal.installs is True
    with pytest.raises(ValueError):
        ProposedHttpToolChangeV1(
            tool_id="http:gitea",
            display_name="Gitea",
            hosts=("gitea.example.com",),
            previous_definition_digest="not-a-sha256",
        )


def test_instance_tools_round_trip_gate_injections_and_replace_in_candidate_catalog(tmp_path):
    from access_catalog.catalog import CatalogError
    from access_catalog.instance_tools import proposed_tool_path, with_proposed_tools, write_instance_tool
    from custom_messages.contracts.access_v2 import (
        CredentialInjectionV1,
        InjectionPlacement,
        ProposedHttpToolChangeV1,
        ProposedHttpToolV1,
    )

    gate_hdr = CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate-Key", field="secret")
    initial = ProposedHttpToolV1(
        tool_id="http:gitea",
        display_name="Gitea",
        hosts=("gitea.example.com",),
        uses_credential=False,
        gate_injections=(gate_hdr,),
    )
    written = write_instance_tool(tmp_path, initial)
    assert written == proposed_tool_path(tmp_path, "http:gitea")

    loaded = load_catalog(tmp_path)
    assert loaded.tools["http:gitea"].gate_injections == (gate_hdr,)
    assert loaded.resolve_http("GET", "https://gitea.example.com/api/v1/version").tool_id == "http:gitea"

    # with_proposed_tools replaces the tool's definition and hosts in a candidate catalog.
    changed = ProposedHttpToolChangeV1(
        tool_id="http:gitea",
        display_name="Gitea v2",
        hosts=("git.example.com",),
        uses_credential=True,
        credential_injections=(
            CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="Authorization", field="token", template="token {value}"),
        ),
        gate_injections=(gate_hdr,),
    )
    updated = with_proposed_tools(loaded, (), tool_changes=(changed,))
    assert updated.tools["http:gitea"].display_name == "Gitea v2"
    assert updated.resolve_http("GET", "https://gitea.example.com/api").tool_id == UNLISTED_WEB_TOOL
    assert updated.resolve_http("GET", "https://git.example.com/api").tool_id == "http:gitea"

    # Host clashes, unknown tools, and duplicates are refused.
    with pytest.raises(CatalogError, match="belongs to both"):
        with_proposed_tools(
            loaded,
            (),
            tool_changes=(
                ProposedHttpToolChangeV1(tool_id="http:gitea", display_name="Gitea", hosts=("api.github.com",)),
            ),
        )
    with pytest.raises(CatalogError, match="not an existing web API tool"):
        with_proposed_tools(
            loaded,
            (),
            tool_changes=(
                ProposedHttpToolChangeV1(tool_id="http:missing", display_name="Missing", hosts=("m.example.com",)),
            ),
        )
    with pytest.raises(CatalogError, match="more than once"):
        with_proposed_tools(loaded, (), tool_changes=(changed, changed))


def test_pre_upgrade_proposal_json_keeps_its_exact_digest():
    from access_control.proposals import proposal_digest
    from custom_messages.contracts.access_v2 import (
        AccessProposalV1,
        CredentialInjectionV1,
        InjectionPlacement,
        LinkPurpose,
        ProposedAccountLinkV1,
        ProposedHttpToolV1,
        contract_digest,
    )

    pre_upgrade_json = {
        "schema_version": 1,
        "title": "Add Gitea",
        "reason": "Inspect repositories.",
        "tools": [
            {
                "tool_id": "http:gitea",
                "display_name": "Gitea",
                "description": "",
                "hosts": ["gitea.example.com"],
                "routes": [],
                "uses_credential": True,
                "credential_injections": [
                    {"placement": "header", "name": "Authorization", "field": "token", "template": "token {value}"}
                ],
                "setup": None,
            }
        ],
        "clis": [],
        "mcp_servers": [],
        "grants": [],
        "blocks": [],
        "account_links": [
            {"tool_id": "http:gitea", "owner": "team", "purpose": "Gitea API access."}
        ],
        "data_folders": [],
        "revoke_grant_ids": [],
    }
    expected_digest = contract_digest(pre_upgrade_json)
    parsed = AccessProposalV1.model_validate(pre_upgrade_json)
    assert proposal_digest(parsed) == expected_digest

    # Non-default gate_injections or gate link_purpose DO change the digest.
    with_gate = AccessProposalV1(
        title="Add Gitea",
        reason="Inspect repositories.",
        tools=(
            ProposedHttpToolV1(
                tool_id="http:gitea",
                display_name="Gitea",
                hosts=("gitea.example.com",),
                credential_injections=(
                    CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="Authorization", field="token", template="token {value}"),
                ),
                gate_injections=(
                    CredentialInjectionV1(placement=InjectionPlacement.HEADER, name="X-Gate", field="secret"),
                ),
            ),
        ),
        account_links=(
            ProposedAccountLinkV1(tool_id="http:gitea", owner="team", purpose="Gitea gate.", link_purpose=LinkPurpose.GATE),
        ),
    )
    assert proposal_digest(with_gate) != expected_digest


def test_instance_tool_files_load_underscore_and_trailing_dot_hosts_and_guide_unicode_hosts(tmp_path) -> None:
    """C23: existing instance tool files with Docker underscores or a trailing dot load; Unicode IDNs name punycode."""

    from access_catalog.catalog import CatalogError

    tools_dir = tmp_path / "control" / "access" / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "docker-gitea.yaml").write_text(
        """
tools:
  - tool_id: http:gitea_local
    display_name: Local Gitea
    hosts: [gitea_web]
    account: none
  - tool_id: http:fqdn_api
    display_name: FQDN API
    hosts: [api.example.com.]
    account: none
""",
        encoding="utf-8",
    )

    loaded = load_catalog(tmp_path)
    assert loaded.tools["http:gitea_local"].hosts == ("gitea_web",)
    assert loaded.resolve_http("GET", "http://gitea_web/api/v1/user").tool_id == "http:gitea_local"
    assert loaded.tools["http:fqdn_api"].hosts == ("api.example.com",)
    assert loaded.resolve_http("GET", "https://api.example.com/v1/items").tool_id == "http:fqdn_api"

    idn_file = tools_dir / "unicode-host.yaml"
    idn_file.write_text(
        """
tools:
  - tool_id: http:books
    display_name: Books
    hosts: ["b\u00fccher.example.com"]
    account: none
""",
        encoding="utf-8",
    )
    with pytest.raises(CatalogError) as caught:
        load_catalog(tmp_path)
    message = str(caught.value)
    assert str(idn_file) in message
    assert "xn--bcher-kva.example.com" in message


def test_credential_capture_file_action_is_classified_at_write_and_aliases_normalize(catalog) -> None:
    provider = get_provider("credential_catalog")
    spec = provider.actions["credential.capture_file"]
    assert spec.returns_secret is False

    for alias in ("credential.capture_file", "credential_capture_file", "capture_file"):
        validated = provider.validate(alias, {"path": "scratch/token.txt"})
        assert validated.action == "credential.capture_file"

    resolution = catalog.resolve_broker_action("credential_catalog", "credential.capture_file", {})
    assert (resolution.tool_id, resolution.operation, resolution.level) == (
        "procedure:credentials",
        "credential.capture_file",
        WRITE,
    )


# gh api method parsing and classification ----------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["api", "repos/o/r", "-X", "GET", "-X", "DELETE"],
        ["api", "repos/o/r", "--method=GET", "--method=DELETE"],
        ["api", "graphql", "-X", "GET", "-X", "POST", "-f", "query=mutation{x}"],
        ["api", "repos/o/r", "-X", "GET", "--method=GET"],
        ["api", "repos/o/r", "--method=", "-f", "title=x"],
        ["api", "graphql", "--method=", "-f", "query=mutation{x}"],
    ],
)
def test_gh_api_rejects_repeated_or_empty_method_flags(catalog, argv) -> None:
    from access_catalog.cli_tables import CommandLineRejected, parse_command_line, parse_gh_api

    policy = load_cli_policy("gh")
    parsed = parse_command_line(argv, policy.table)
    with pytest.raises(CommandLineRejected):
        parse_gh_api(parsed)

    resolution = catalog.resolve_broker_action("github", "gh.exec", {"argv": argv})
    assert resolution.tool_id == "cli:github"
    assert resolution.operation == UNPARSED_COMMAND_OPERATION == "command:unparsed"
    assert resolution.command_line_rejected
    assert resolution.level is FULL
    assert resolution.source is ClassificationSource.UNREVIEWED
    assert resolution.detail


@pytest.mark.parametrize(
    ("argv", "expected_method"),
    [
        (["api", "repos/o/r/issues", "--template", "--method=GET", "-f", "title=x"], "POST"),
        (["api", "repos/o/r/issues", "--jq", "--method=GET", "-f", "title=x"], "POST"),
        (["api", "repos/o/r/issues", "-H", "--method=GET", "-f", "title=x"], "POST"),
        (["api", "repos/o/r/issues", "-f", "--method=GET"], "POST"),
        (["api", "repos/o/r/issues", "-F", "a=1"], "POST"),
        (["api", "repos/o/r/issues", "--input", "-"], "POST"),
    ],
)
def test_gh_api_hidden_method_in_value_flags_and_implicit_post_classify_full(
    catalog, argv, expected_method
) -> None:
    from access_catalog.cli_tables import parse_command_line, parse_gh_api

    policy = load_cli_policy("gh")
    parsed = parse_command_line(argv, policy.table)
    assert parse_gh_api(parsed).method == expected_method

    resolution = catalog.resolve_broker_action("github", "gh.exec", {"argv": argv})
    assert resolution.tool_id == "cli:github"
    assert resolution.operation == "command:api"
    assert resolution.level is FULL


@pytest.mark.parametrize(
    ("argv", "expected_method"),
    [
        (["api", "repos/o/r"], "GET"),
        (["api", "repos/o/r", "--jq", ".name"], "GET"),
        (["api", "repos/o/r", "-q", ".name"], "GET"),
        (["api", "repos/o/r", "-H", "Accept: application/vnd.github+json"], "GET"),
        (["api", "repos/o/r", "--paginate"], "GET"),
        (["api", "repos/o/r/issues", "--paginate", "--slurp"], "GET"),
        (["api", "repos/o/r", "--allow-escape-sequences"], "GET"),
        (["api", "repos/o/r", "--paginate=true", "--silent=false", "-i"], "GET"),
        (["api", "repos/o/r", "--cache", "1h", "-p", "nebula", "--template", "{{.name}}"], "GET"),
        (["api", "-X", "GET", "repos/o/r"], "GET"),
        (["api", "--method=get", "repos/o/r"], "GET"),
        (["api", "-X", "HEAD", "repos/o/r"], "HEAD"),
        # Explicit GET with -f/-F sends query parameters without a request body.
        (["api", "repos/o/r", "-X", "GET", "-f", "a=b"], "GET"),
        (["api", "repos/o/r", "-X", "GET", "-F", "a=1"], "GET"),
        (["api", "-X", "GET", "search/issues", "-f", "q=repo:o/r is:open"], "GET"),
        (["api", "--method=GET", "search/issues", "--raw-field", "q=x", "-F", "per_page=5", "--paginate"], "GET"),
        # A repo named "graphql" under repos/ is not the GraphQL endpoint.
        (["api", "repos/o/graphql"], "GET"),
    ],
)
def test_gh_api_ordinary_reads_classify_read(catalog, argv, expected_method) -> None:
    from access_catalog.cli_tables import parse_command_line, parse_gh_api

    policy = load_cli_policy("gh")
    parsed = parse_command_line(argv, policy.table)
    assert parse_gh_api(parsed).method == expected_method

    resolution = catalog.resolve_broker_action("github", "gh.exec", {"argv": argv})
    assert resolution.tool_id == "cli:github"
    assert resolution.operation == "command:api"
    assert resolution.level is READ


@pytest.mark.parametrize(
    ("argv", "expected_method"),
    [
        (["api", "repos/o/r/issues", "-X", "POST"], "POST"),
        (["api", "repos/o/r/issues/1", "-X", "PATCH"], "PATCH"),
        (["api", "repos/o/r/issues/1", "-X", "PUT"], "PUT"),
        (["api", "repos/o/r/issues/1", "-X", "DELETE"], "DELETE"),
        (["api", "repos/o/r", "--method= GET "], " GET "),
        # Requests that send a body on GET (--input) or HEAD (--input or -f/-F) never classify as read.
        (["api", "repos/o/r", "-X", "GET", "--input", "-"], "GET"),
        (["api", "repos/o/r", "-X", "HEAD", "--input", "-"], "HEAD"),
        (["api", "repos/o/r", "-X", "HEAD", "-f", "a=b"], "HEAD"),
        (["api", "repos/o/r", "-X", "HEAD", "-F", "a=1"], "HEAD"),
        # GraphQL endpoint always classifies as full regardless of HTTP method.
        (["api", "graphql"], "GET"),
        (["api", "graphql", "-X", "GET"], "GET"),
        (["api", "graphql", "--method=GET"], "GET"),
        (["api", "graphql", "-X", "HEAD"], "HEAD"),
        (["api", "graphql", "-X", "GET", "-f", "query=mutation{x}"], "GET"),
        (["api", "graphql", "-X", "GET", "--input", "-"], "GET"),
        (["api", "/graphql", "-X", "GET"], "GET"),
        (["api", "//graphql", "-X", "GET"], "GET"),
        (["api", "//graphql?query=x", "-X", "GET"], "GET"),
        (["api", "//api/graphql", "-X", "GET"], "GET"),
        (["api", "///GraphQL", "-X", "GET"], "GET"),
        (["api", "https://api.github.com/graphql", "-X", "GET"], "GET"),
        (["api", "https://api.github.com/GRAPHQL?query=x", "-X", "GET"], "GET"),
        (["api", "repos/../graphql", "-X", "GET"], "GET"),
        (["api", "/%67raphql", "-X", "GET"], "GET"),
        (["api", "/api/graphql", "-X", "GET"], "GET"),
        (["api", "https://github.enterprise.example/api/graphql", "-X", "GET"], "GET"),
    ],
)
def test_gh_api_writes_read_bodies_and_graphql_classify_full(catalog, argv, expected_method) -> None:
    from access_catalog.cli_tables import parse_command_line, parse_gh_api

    policy = load_cli_policy("gh")
    parsed = parse_command_line(argv, policy.table)
    assert parse_gh_api(parsed).method == expected_method

    resolution = catalog.resolve_broker_action("github", "gh.exec", {"argv": argv})
    assert resolution.tool_id == "cli:github"
    assert resolution.operation == "command:api"
    assert resolution.level is FULL


@pytest.mark.parametrize(
    "argv",
    [
        ["api", "repos/o/r", "--unknown-flag"],
        ["api", "repos/o/r", "-z"],
        ["api", "repos/o/r", "-5"],
        ["api", "repos/o/r", "-X"],
        ["api", "repos/o/r", "--template"],
        ["api", "repos/o/r", "--jq"],
        ["api", "repos/o/r", "-H"],
        ["api", "repos/o/r", "-f"],
        ["api", "repos/o/r", "--input"],
        ["api", "repos/o/r", "--paginate=maybe"],
        # Endpoint addresses urlsplit cannot read are refused, not raised as ValueError.
        ["api", "https://[::1/graphql", "-X", "GET"],
        ["api", "http://[x]/repos/o/r"],
        ["api", "x://["],
        ["api", "https://a\u2100b/repos/o/r"],
    ],
)
def test_gh_api_rejects_unknown_or_malformed_flags(catalog, argv) -> None:
    from access_catalog.cli_tables import CommandLineRejected, parse_command_line, parse_gh_api

    policy = load_cli_policy("gh")
    parsed = parse_command_line(argv, policy.table)
    with pytest.raises(CommandLineRejected):
        parse_gh_api(parsed)

    resolution = catalog.resolve_broker_action("github", "gh.exec", {"argv": argv})
    assert resolution.tool_id == "cli:github"
    assert resolution.operation == UNPARSED_COMMAND_OPERATION == "command:unparsed"
    assert resolution.command_line_rejected
    assert resolution.level is FULL
    assert resolution.source is ClassificationSource.UNREVIEWED
    assert resolution.detail


def test_gh_api_body_flags_match_gh_yaml_and_position_blind_helpers_are_removed() -> None:
    from access_catalog.cli_commands import ParsedCommandLine
    from access_catalog.cli_tables import _GH_API_BODY_FLAGS

    rules = load_cli_policy("gh").rules_for("api")
    assert _GH_API_BODY_FLAGS == rules.field_flags | rules.stdin_only_flags
    assert not hasattr(ParsedCommandLine, "flag_values")
    assert not hasattr(ParsedCommandLine, "has_flag")


def _closed_loopback_proxy_url() -> str:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _run_offline_gh_api(argv: list[str], tmp_path, *, stdin_text: str = "") -> str:
    import os
    import subprocess

    proxy = _closed_loopback_proxy_url()
    config_dir = tmp_path / "gh-config"
    config_dir.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "GH_TOKEN": "dummy-token-for-offline-test",
        "GH_HOST": "nonexistent.invalid",
        "GH_CONFIG_DIR": str(config_dir),
        "GH_DEBUG": "api",
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "NO_PROXY": "",
    }
    proc = subprocess.run(
        ["gh", *argv],
        input=stdin_text,
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )
    return f"{proc.stdout}\n{proc.stderr}"


def _extract_gh_debug_method(output: str) -> str | None:
    import re

    match = re.search(r"^> ([A-Z]+) \S+ HTTP/1\.1$", output, flags=re.MULTILINE)
    return match.group(1) if match else None


def _gh_debug_request_has_body(output: str) -> bool:
    """Whether GH_DEBUG=api logged a Content-Length header or request body bytes."""

    lines = output.splitlines()
    start = next((idx for idx, line in enumerate(lines) if line.startswith("> ") and line.endswith(" HTTP/1.1")), None)
    if start is None:
        return False
    idx = start + 1
    while idx < len(lines) and lines[idx].startswith("> "):
        if lines[idx].lower().startswith("> content-length:"):
            return True
        idx += 1
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    while idx < len(lines) and not lines[idx].startswith("* "):
        if lines[idx].strip():
            return True
        idx += 1
    return False


@pytest.mark.parametrize(
    ("argv", "stdin_text"),
    [
        (["api", "repos/o/r"], ""),
        (["api", "repos/o/r", "-X", "GET"], ""),
        (["api", "-X", "GET", "search/issues", "-f", "q=repo:o/r", "-F", "per_page=5"], ""),
        (["api", "repos/o/r", "--method=HEAD"], ""),
        (["api", "repos/o/r", "-X", "HEAD", "-f", "a=b"], ""),
        (["api", "repos/o/r", "-X", "GET", "--input", "-"], '{"x":1}'),
        (["api", "repos/o/r", "-X", "DELETE"], ""),
        (["api", "repos/o/r", "--method=PATCH"], ""),
        (["api", "repos/o/r", "--method=PUT"], ""),
        (["api", "repos/o/r/issues", "--template", "--method=GET", "-f", "title=x"], ""),
        (["api", "repos/o/r/issues", "--jq", "--method=GET", "-f", "title=x"], ""),
        (["api", "repos/o/r/issues", "-H", "X-Test: --method=GET", "-f", "title=x"], ""),
        (["api", "repos/o/r/issues", "-f", "--method=GET"], ""),
        (["api", "repos/o/r/issues", "-F", "a=1"], ""),
        (["api", "repos/o/r/issues", "--input", "-"], "{}"),
        (["api", "repos/o/r", "--paginate=true"], ""),
        (["api", "repos/o/r", "--paginate=false"], ""),
        (["api", "repos/o/r", "--jq=.name", "--include=true"], ""),
        (["api", "repos/o/r", "--silent=true"], ""),
        (["api", "graphql", "-f", "query={viewer{login}}"], ""),
        (["api", "graphql", "-X", "GET", "-f", "query=x"], ""),
    ],
)
def test_gh_api_method_matches_real_gh_wire_request(tmp_path, argv, stdin_text) -> None:
    import shutil

    if shutil.which("gh") is None:
        pytest.skip("gh binary not on PATH")

    from access_catalog.cli_tables import gh_api_level, parse_command_line, parse_gh_api

    policy = load_cli_policy("gh")
    parsed = parse_command_line(argv, policy.table)
    call = parse_gh_api(parsed)
    output = _run_offline_gh_api(argv, tmp_path, stdin_text=stdin_text)
    wire_method = _extract_gh_debug_method(output)
    assert wire_method is not None, f"expected gh to emit an HTTP request line, got:\n{output}"
    assert call.method == wire_method
    if gh_api_level(call) is READ:
        assert not _gh_debug_request_has_body(output), (
            f"read-classified gh api call emitted a Content-Length or request body:\n{output}"
        )


def test_gh_api_body_presence_matches_real_gh_for_get_and_head(tmp_path) -> None:
    import shutil

    if shutil.which("gh") is None:
        pytest.skip("gh binary not on PATH")

    from access_catalog.cli_tables import gh_api_level, parse_command_line, parse_gh_api

    policy = load_cli_policy("gh")

    get_fields = ["api", "-X", "GET", "search/issues", "-f", "q=repo:o/r", "-F", "per_page=5"]
    get_fields_out = _run_offline_gh_api(get_fields, tmp_path)
    assert gh_api_level(parse_gh_api(parse_command_line(get_fields, policy.table))) is READ
    assert not _gh_debug_request_has_body(get_fields_out)

    head_fields = ["api", "-X", "HEAD", "repos/o/r", "-f", "a=b"]
    head_fields_out = _run_offline_gh_api(head_fields, tmp_path)
    assert gh_api_level(parse_gh_api(parse_command_line(head_fields, policy.table))) is FULL
    assert _gh_debug_request_has_body(head_fields_out)

    get_input = ["api", "-X", "GET", "repos/o/r", "--input", "-"]
    get_input_out = _run_offline_gh_api(get_input, tmp_path, stdin_text='{"x":1}')
    assert gh_api_level(parse_gh_api(parse_command_line(get_input, policy.table))) is FULL
    assert _gh_debug_request_has_body(get_input_out)


@pytest.mark.parametrize(
    ("argv", "expected_wire_method"),
    [
        (["api", "repos/o/r", "-X", "GET", "-X", "DELETE"], "DELETE"),
        (["api", "repos/o/r", "--method=GET", "--method=DELETE"], "DELETE"),
        (["api", "graphql", "-X", "GET", "-X", "POST", "-f", "query=x"], "POST"),
        (["api", "repos/o/r", "--method=", "-f", "title=x"], "GET"),
    ],
)
def test_gh_api_refuses_audit_probes_that_real_gh_would_send(tmp_path, argv, expected_wire_method) -> None:
    import shutil

    if shutil.which("gh") is None:
        pytest.skip("gh binary not on PATH")

    from access_catalog.cli_tables import CommandLineRejected, parse_command_line, parse_gh_api

    output = _run_offline_gh_api(argv, tmp_path)
    assert _extract_gh_debug_method(output) == expected_wire_method

    policy = load_cli_policy("gh")
    parsed = parse_command_line(argv, policy.table)
    with pytest.raises(CommandLineRejected):
        parse_gh_api(parsed)


@pytest.mark.parametrize(
    "argv",
    [
        ["api", "repos/o/r", "--unknown-flag"],
        ["api", "repos/o/r", "-5"],
        ["api", "repos/o/r", "-X"],
        ["api", "repos/o/r", "--template"],
        ["api", "repos/o/r", "--paginate=maybe"],
    ],
)
def test_gh_api_errors_send_no_request_in_real_gh(tmp_path, argv) -> None:
    import shutil

    if shutil.which("gh") is None:
        pytest.skip("gh binary not on PATH")

    from access_catalog.cli_tables import CommandLineRejected, parse_command_line, parse_gh_api

    output = _run_offline_gh_api(argv, tmp_path)
    assert _extract_gh_debug_method(output) is None

    policy = load_cli_policy("gh")
    parsed = parse_command_line(argv, policy.table)
    with pytest.raises(CommandLineRejected):
        parse_gh_api(parsed)


def test_gh_api_flag_tables_cover_installed_gh_help() -> None:
    import re
    import shutil
    import subprocess

    if shutil.which("gh") is None:
        pytest.skip("gh binary not on PATH")

    from access_catalog.cli_tables import _GH_API_BOOL_FLAGS, _GH_API_VALUE_FLAGS

    proc = subprocess.run(["gh", "api", "--help"], text=True, capture_output=True, timeout=10, check=True)
    in_flags = False
    seen_flags: set[str] = set()
    for line in proc.stdout.splitlines():
        if line in {"FLAGS", "INHERITED FLAGS"}:
            in_flags = True
            continue
        if in_flags and line and not line.startswith(" "):
            in_flags = False
            continue
        if not in_flags or not line.strip():
            continue
        match = re.match(r"^\s+(?:(-[A-Za-z]),\s+)?(--[a-z0-9-]+)(?:\s+(\S+))?\s{2,}", line)
        assert match is not None, f"could not parse gh api --help flag line: {line!r}"
        short_flag, long_flag, value_type = match.groups()
        target = _GH_API_VALUE_FLAGS if value_type else _GH_API_BOOL_FLAGS
        assert long_flag in target, f"{long_flag} missing from expected gh api flag table"
        seen_flags.add(long_flag)
        if short_flag:
            assert short_flag in target, f"{short_flag} missing from expected gh api flag table"
            seen_flags.add(short_flag)
    assert "--method" in seen_flags and "--paginate" in seen_flags



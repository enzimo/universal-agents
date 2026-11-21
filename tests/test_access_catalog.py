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

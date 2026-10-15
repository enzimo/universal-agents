from custom_agents.agent_instructions import (
    ACCESS_PROMPT_RULE,
    shared_agent_base_prompt,
)
from custom_agents.strands_team import (
    RUNTIME_OPERATIONS_WORKFLOWS_SKILL_DIR,
    SECURE_CREDENTIAL_WORKFLOWS_SKILL_DIR,
)


def test_shared_prompt_keeps_secure_cli_details_in_skill() -> None:
    assert "`secure-credential-workflows` skill" in shared_agent_base_prompt
    assert "`runtime-operations-workflows` skill" in shared_agent_base_prompt
    assert "`shell-execution-workflows` skill" in shared_agent_base_prompt
    assert "credential_catalog.credential.collect" not in shared_agent_base_prompt
    assert "GitHub Projects v2" not in shared_agent_base_prompt
    assert "Home Assistant username/password" not in shared_agent_base_prompt
    assert "list_runtime_settings_options" not in shared_agent_base_prompt
    assert "notify_user" not in shared_agent_base_prompt


def test_secure_credential_skill_retains_provider_action_and_recovery_contract() -> None:
    skill_text = (SECURE_CREDENTIAL_WORKFLOWS_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    for required_guidance in (
        "`github` actions: `gh.exec`",
        "`google` actions: `gog.exec`",
        "`bitwarden`, `onepassword`, `keepass`, and `vault` actions",
        "`sealbox` actions: `credential.search`",
        "`secure_requests` actions: `request`",
        "`credential_catalog` actions: `credential.collect`",
        "`api.get` accepts only `path`",
        "`secure_credential_collection_request`",
        "`set_up_tools`",
        "without asking\nthe person whether to start",
        "at most three pages per tool and kind",
        "read-only check such as `auth.status` before a write",
        "canonical_user_required",
    ):
        assert required_guidance in skill_text


def test_runtime_operations_skill_owns_reviewed_skill_installation_contract() -> None:
    skill_text = (RUNTIME_OPERATIONS_WORKFLOWS_SKILL_DIR / "SKILL.md").read_text(
        encoding="utf-8"
    )

    for required_guidance in (
        "`plan_skill_installation",
        "`get_skill_installation_status",
        "every finding",
        "if an administrator let you manage them, the skill\n   installs at once",
        "waits until a person approves the exact install in the Inbox",
        "Inspect `runtime_skill_refresh`",
        "heartbeat repairs that deferred refresh",
        "`plan_runtime_restart",
        "`apply_change(change_id, reason)`",
        "stage a fresh plan and ask",
    ):
        assert required_guidance in skill_text


def test_shared_prompt_uses_provider_neutral_structured_output_tool_guidance() -> None:
    assert "exact structured-output tool registered for the current invocation" in shared_agent_base_prompt
    assert "provider-compatible tool name" in shared_agent_base_prompt
    assert "calling the `ModelAgentTurnDecision` structured-output tool" not in shared_agent_base_prompt


def test_access_prompt_names_the_access_tools_and_the_only_way_to_ask() -> None:
    for tool in ("request_access", "list_my_access", "find_tools", "restrict_access", "revoke_access"):
        assert f"`{tool}`" in ACCESS_PROMPT_RULE
    # Workers get access through their tasks; there is no tool to give it.
    assert "give_access" not in ACCESS_PROMPT_RULE and "Access follows the work" in ACCESS_PROMPT_RULE
    assert "access_denied" in ACCESS_PROMPT_RULE and "Never ask people to grant access in chat" in ACCESS_PROMPT_RULE
    assert ACCESS_PROMPT_RULE in shared_agent_base_prompt
    assert "denial_receipt" not in shared_agent_base_prompt


def test_shared_prompt_prohibits_generated_site_url_guessing() -> None:
    assert "never guess or synthesize the URL" in shared_agent_base_prompt
    assert "exact `site_url` returned by `publish_single_page_site`" in shared_agent_base_prompt
    assert "Do not invent cleaner aliases such as `/sites/<slug>`" in shared_agent_base_prompt
    assert "Do not wrap bare URLs in Markdown emphasis" in shared_agent_base_prompt


def test_shared_prompt_routes_generated_site_kinds_by_information_shape() -> None:
    for site_kind in ("`article`", "`comparison`", "`how_to`", "`app`"):
        assert site_kind in shared_agent_base_prompt

    assert "Default to `article`" in shared_agent_base_prompt
    assert "whose order materially affects success" in shared_agent_base_prompt
    assert "do not by themselves make content a `how_to`" in shared_agent_base_prompt
    assert "workout trackers, habit boards, practice logs, or goal dashboards" in shared_agent_base_prompt
    assert "optional building blocks, not a completion checklist" in shared_agent_base_prompt


def test_shared_prompt_includes_multi_user_onboarding_recipe() -> None:
    assert "/user status" in shared_agent_base_prompt
    assert "/user list" in shared_agent_base_prompt
    assert "newuser_target=..." in shared_agent_base_prompt
    assert "Telegram uses numeric `from_user.id`" in shared_agent_base_prompt
    assert "WhatsApp uses the Twilio sender identity" in shared_agent_base_prompt
    assert '/user token "Device label"' in shared_agent_base_prompt
    assert "/user invite <usr_...>" in shared_agent_base_prompt
    assert '/user invite --new "Display Name"' in shared_agent_base_prompt
    assert "authenticated Web Chat admin" in shared_agent_base_prompt
    assert "/user link` must run on the external platform" in shared_agent_base_prompt
    assert "generates, reveals, copies, and confirms a reusable `uauser_...` token" in shared_agent_base_prompt
    assert "same `/webchat` route" in shared_agent_base_prompt
    assert "/docs/newuser/telegram" in shared_agent_base_prompt
    assert "/docs/newuser/photon" in shared_agent_base_prompt
    assert "/user disable <canonical_user_id>" in shared_agent_base_prompt

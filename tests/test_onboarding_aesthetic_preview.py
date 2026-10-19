from __future__ import annotations

from tests.browser_assets import module_source

from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading

import pytest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from assistant_gateway.application.website_application import (
    WebMemoryListV1,
    WebScheduleListV1,
    WebTaskListV1,
)
from assistant_gateway.onboarding_web.provider_test import ProviderTestResult
from runtime_settings.appearance import DARK_CSS_VARIABLES, dark_appearance_css_declarations
from scripts.sync_setup_guide_renderer import embedded_renderer
from scripts.run_onboarding_aesthetic_preview import (
    CHAT_ASSET_FILENAME,
    DEFAULT_ASSET_ROOT,
    build_arg_parser,
    build_handler,
    provider_test_runtime,
)


def _serve_preview(asset_root: Path, export_dir: Path | None = None):
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), build_handler(asset_root, export_dir=export_dir)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, f"http://{host}:{port}"


def _model_fields(payload: dict[str, object]) -> dict[str, object]:
    """A real route's answer is ``{"status": "ok", **model}``; the model's fields alone."""

    assert payload["status"] == "ok"
    return {key: value for key, value in payload.items() if key != "status"}


def _json_post(url: str, payload: dict[str, object], opener=None) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    open_url = opener.open if opener is not None else urlopen
    with open_url(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_onboarding_aesthetic_preview_parser_defaults_to_local_preview_port() -> None:
    args = build_arg_parser().parse_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 18090
    assert args.asset_root == str(DEFAULT_ASSET_ROOT)
    assert args.export_dir == str(Path.home() / "Downloads")
    assert args.open is False
    assert CHAT_ASSET_FILENAME == "chat.html"


def test_production_onboarding_shell_has_no_preview_export_button() -> None:
    index_text = (DEFAULT_ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    motion_text = (DEFAULT_ASSET_ROOT / "mesh_motion.js").read_text(encoding="utf-8")
    app_text = module_source(DEFAULT_ASSET_ROOT, "app.js")
    chat_html_text = (DEFAULT_ASSET_ROOT / "chat.html").read_text(encoding="utf-8")
    chat_text = module_source(DEFAULT_ASSET_ROOT, "chat.js")
    styles_text = (DEFAULT_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")

    assert "<title>Universal Agents Onboarding</title>" in index_text
    assert "Aesthetic Preview" not in index_text
    assert "export-appearance-button" not in index_text
    assert "Onboarding completion" in index_text
    assert '<script src="/mesh_motion.js"></script>' in index_text
    assert '<script src="/appearance.js"></script>' in index_text
    assert '<script src="/appearance_customizer.js"></script>' in index_text
    assert '<script src="/metallic_mesh.js"></script>' in index_text
    assert 'data-color-mode="dark"' in index_text
    assert "POLYGON_SPEED_MULTIPLIER = 2" in motion_text
    assert "/api/bootstrap" in app_text
    assert "/api/onboarding/complete" in app_text
    assert "/api/onboarding/restart" in app_text
    assert "/api/onboarding/skills-repos/validate" in app_text
    assert "/api/provider-test" in app_text
    assert "color_mode: sharedState.state.colorMode" in app_text
    assert 'legend.textContent = "Color Theme"' in app_text
    assert 'customize.textContent = "Customize Theme"' in app_text
    assert (
        "Shape the planes, metal mix, and motion used across the runtime UI."
        not in app_text
    )
    assert "dark: window.UAAppearance.savedDark(dark)" in app_text
    assert "providerTestHttpErrorMessage(response, text)" in app_text
    assert "function finalizationErrorMessage(text, fallback)" in app_text
    assert 'message + "\\n- " + details.join("\\n- ")' in app_text
    assert (
        'providerByLabel(sharedState.state.runtimeProvider).key === "xai-oauth"'
        in app_text
    )
    assert "reverse proxy forwards POST /api/provider-test" in app_text
    assert (
        "The browser could not reach the onboarding connection-test endpoint"
        in app_text
    )
    assert 'env: "CRAWL4AI_MCP_SERVER_URL"' in app_text
    assert 'label: "Crawl4AI MCP server URL"' in app_text
    assert 'placeholder: "http://crawl4ai:11235/mcp/sse"' in app_text
    assert "CRAWL4AI_BASE_URL" not in app_text
    assert "OpenAI Codex OAuth" in app_text
    assert (
        'model_setupState.providerHelpCopy = "OpenAI uses the public OpenAI API with an API key.'
        in app_text
    )
    assert 'providerLabelWrap.className = "label-with-info";' in app_text
    assert '"Model provider choices information"' in app_text
    assert 'sharedState.DEFAULT_RUNTIME_PROVIDER = "OpenRouter";' in app_text
    assert (
        'sharedState.DEFAULT_RUNTIME_ENDPOINT = "https://openrouter.ai/api/v1";'
        in app_text
    )
    assert 'sharedState.DEFAULT_RUNTIME_MODEL = "deepseek/deepseek-v4.1-flash";' in app_text
    assert "function clearLegacyRuntimeDefaults()" in app_text
    assert 'storedModel === "deepseek/deepseek-v4-flash"' in app_text
    assert "Fresh setups use" in app_text
    assert "with DeepSeek V4.1 Flash by default" in app_text
    assert "https://developers.openai.com/codex/cli" in app_text
    assert "Codex CLI login required" in app_text
    assert "tokenOptional" in app_text
    assert "Supports Images" in app_text
    assert "Supports Audio" in app_text
    assert "Supports Files" in app_text
    assert (
        "supports_image_input: Boolean(sharedState.state.runtimeSupportsImages)"
        in app_text
    )
    assert (
        "supports_audio_input: Boolean(sharedState.state.runtimeSupportsAudio)"
        in app_text
    )
    assert (
        "supports_file_input: Boolean(sharedState.state.runtimeSupportsFiles)"
        in app_text
    )
    assert 'suggestions: ["low", "medium", "high", "max"]' in app_text
    assert '"Context window tokens", "number"' in app_text
    assert "min: 1024" in app_text
    assert '["Rich messages", "toggle", true]' in app_text
    assert "telegram_rich_messages: operations.getChannelToggleValue" in app_text
    assert "function invalidateLightPaneCache()" in app_text
    assert 'lightPaneCacheKey = "";' in app_text
    assert "The team is up and running." in app_text
    assert "The team is up and running with basic options" not in app_text
    assert 'nextButton.hidden = page.id === "standby";' in app_text
    assert "function webChatUrl()" in app_text
    assert "finalization.openStatus();" in app_text
    assert 'operations.webChatUrl() + "?view=status"' in app_text
    assert 'spinner.className = "standby-spinner";' in app_text
    assert 'status.setAttribute("role", "status");' in app_text
    assert "/api/agents/status" not in app_text
    assert "/download/instance_config.yaml" in app_text
    assert "previewConfigYaml" not in app_text
    assert "previewTeamEnv" not in app_text
    assert '<script src="/mesh_motion.js"></script>' in chat_html_text
    assert 'id="chat-version"' in chat_html_text
    assert (
        'versionLabel.textContent = publicVersion ? "v" + publicVersion : "";'
        in chat_text
    )
    assert "copyTextToClipboard" in chat_text
    assert "renameThread" in chat_text
    assert "pendingReplyCount" in chat_text
    assert "messageSubmitInFlight" not in chat_text
    assert "sendButton.disabled" not in chat_text
    assert ".message-copy-button" in styles_text
    assert ".thread-rename-input" in styles_text
    assert ".chat-version {" in styles_text
    assert ".runtime-provider-setup" in styles_text
    assert ".runtime-inline-toggle" in styles_text
    assert ".runtime-capability-list" in styles_text
    assert ".standby-spinner {" in styles_text
    assert "@keyframes ua-standby-spin" in styles_text


def test_light_mesh_keeps_animated_perimeter_on_viewport_edges() -> None:
    app_text = module_source(DEFAULT_ASSET_ROOT, "app.js")
    chat_text = module_source(DEFAULT_ASSET_ROOT, "chat.js")

    assert "pinX: edgeX" in app_text
    assert "pinY: edgeY" in app_text
    assert "x: point.pinX ? point.x : point.x + Math.sin" in app_text
    assert "y: point.pinY ? point.y : point.y + Math.cos" in app_text
    assert (
        "var pinX = column === 0 || column === appearanceState.meshColumns;"
        in chat_text
    )
    assert "var pinY = row === 0 || row === appearanceState.meshRows;" in chat_text
    assert "x: point.pinX ? point.x : point.x + drift" in chat_text
    assert "y: point.pinY ? point.y : point.y +" in chat_text


def test_dark_memory_permission_and_chat_surfaces_share_deep_ocean_roles() -> None:
    styles_text = (DEFAULT_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")

    dark_surface_rule = styles_text.split(
        ':root[data-color-mode="dark"] .memory-list,', maxsplit=1
    )[1].split("}", maxsplit=1)[0]
    assert ':root[data-color-mode="dark"] .permission-empty-state,' in dark_surface_rule
    assert (
        ':root[data-color-mode="dark"] .permission-grants-table-wrap,'
        in dark_surface_rule
    )
    assert (
        ':root[data-color-mode="dark"] .permission-capability-card,'
        in dark_surface_rule
    )
    assert (
        ':root[data-color-mode="dark"] .permission-technical-details'
        in dark_surface_rule
    )
    assert "background: var(--dark-pane);" in dark_surface_rule

    memory_item_rule = styles_text.split(
        ':root[data-color-mode="dark"] .memory-item {', maxsplit=1
    )[1].split("}", maxsplit=1)[0]
    assert "background: var(--dark-control);" in memory_item_rule

    composer_rule = styles_text.split(
        ':root[data-color-mode="dark"] .message-input {', maxsplit=1
    )[1].split("}", maxsplit=1)[0]
    assert "background: transparent;" in composer_rule

    system_rule = styles_text.split(
        ':root[data-color-mode="dark"] .message-bubble.is-system {', maxsplit=1
    )[1].split("}", maxsplit=1)[0]
    assert "border-color: var(--dark-border-subtle);" in system_rule
    assert "background: linear-gradient" in system_rule
    assert "color: var(--dark-system-text);" in system_rule

    chat_body_rule = styles_text.split(
        ':root[data-color-mode="dark"] .message-bubble.is-user .message-content,',
        maxsplit=1,
    )[1].split("}", maxsplit=1)[0]
    assert (
        ':root[data-color-mode="dark"] .message-bubble.is-assistant .message-content'
        in chat_body_rule
    )
    assert "color: var(--muted);" in chat_body_rule


def test_dark_onboarding_surfaces_use_deep_ocean_roles() -> None:
    styles_text = (DEFAULT_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")

    pane_rule = styles_text.split(
        ':root[data-color-mode="dark"] .advanced-section,', maxsplit=1
    )[1].split("}", maxsplit=1)[0]
    for selector in (
        ':root[data-color-mode="dark"] .channel-item,',
        ':root[data-color-mode="dark"] .agent-row,',
        ':root[data-color-mode="dark"] .review-summary,',
        ':root[data-color-mode="dark"] .download-row',
    ):
        assert selector in pane_rule
    assert "background: var(--dark-pane);" in pane_rule

    control_rule = styles_text.split(
        ':root[data-color-mode="dark"] .advanced-section .shim-card,', maxsplit=1
    )[1].split("}", maxsplit=1)[0]
    for selector in (
        ':root[data-color-mode="dark"] .advanced-chevron,',
        ':root[data-color-mode="dark"] .channel-default,',
        ':root[data-color-mode="dark"] .preference-inline-toggle,',
        ':root[data-color-mode="dark"] .runtime-provider-setup,',
        ':root[data-color-mode="dark"] .runtime-note,',
        ':root[data-color-mode="dark"] .configuration-note,',
        ':root[data-color-mode="dark"] .status-docs-note,',
    ):
        assert selector in control_rule
    assert "background: var(--dark-control);" in control_rule

    disabled_rule = styles_text.split(
        ':root[data-color-mode="dark"] .button.runtime-secondary:disabled,',
        maxsplit=1,
    )[1].split("}", maxsplit=1)[0]
    assert ':root[data-color-mode="dark"] .token-mini-button:disabled' in disabled_rule
    assert "background: var(--dark-disabled);" in disabled_rule
    assert "opacity: 1;" in disabled_rule


def test_runtime_connection_gate_tracks_only_provider_connection_fields() -> None:
    app_text = module_source(DEFAULT_ASSET_ROOT, "app.js")

    signature_body = app_text.split(
        "function runtimeConnectionSignature()", maxsplit=1
    )[1].split("function runtimeReadyToProceed()", maxsplit=1)[0]
    for state_key in (
        "state.runtimeProvider",
        "state.runtimeEndpoint",
        "runtimeTokenForSelectedProvider()",
        "state.runtimeModel",
    ):
        assert state_key in signature_body
    for unrelated_state_key in (
        "runtimeReasoningEffort",
        "runtimeReasoningSummary",
        "runtimeLiteLLMApiMode",
        "runtimeContextWindowTokens",
        "runtimeStream",
    ):
        assert unrelated_state_key not in signature_body

    ready_body = app_text.split("function runtimeReadyToProceed()", maxsplit=1)[
        1
    ].split("function runtimeStatusText()", maxsplit=1)[0]
    assert (
        "state.runtimeTestedConnectionSignature === runtimeConnectionSignature()"
        in ready_body
    )

    advanced_body = app_text.split("function renderRuntimeAdvanced()", maxsplit=1)[
        1
    ].split("function renderCodexProviderSetup()", maxsplit=1)[0]
    assert "invalidateRuntimeTest" not in advanced_body

    assert "var connectionUnderTest = runtimeConnectionSignature();" in app_text
    assert "state.runtimeTestedConnectionSignature = connectionUnderTest;" in app_text


def test_deep_ocean_ember_dark_theme_has_accessible_semantic_roles() -> None:
    styles_text = (DEFAULT_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")
    tokens = dict(
        re.findall(
            r"--(dark-[a-z-]+):\s*(#[0-9a-f]{6});",
            styles_text,
            flags=re.IGNORECASE,
        )
    )

    assert tokens["dark-canvas"] == "#070b0e"
    assert tokens["dark-control"] == "#10232d"
    assert tokens["dark-raised"] == "#173440"
    assert tokens["dark-primary"] == "#087697"
    assert tokens["dark-focus"] == "#62d5f3"
    assert tokens["dark-ember"] == "#f0a35a"
    assert tokens["dark-danger"] == "#b94850"
    assert tokens["dark-system-text"] == "#c8cfd2"

    contrast_pairs = (
        ("#f2f7f8", tokens["dark-control"], 4.5),
        ("#adc1c8", tokens["dark-control"], 4.5),
        (tokens["dark-system-text"], tokens["dark-pane"], 4.5),
        ("#f7fbfc", tokens["dark-primary"], 4.5),
        (tokens["dark-ember-ink"], tokens["dark-ember"], 4.5),
        (tokens["dark-danger-ink"], tokens["dark-danger"], 4.5),
        (tokens["dark-border"], tokens["dark-control"], 3.0),
    )
    for foreground, background, minimum in contrast_pairs:
        assert _contrast_ratio(foreground, background) >= minimum

    for selector in (
        ':root[data-color-mode="dark"] .start-option',
        ':root[data-color-mode="dark"] .appearance-control',
        ':root[data-color-mode="dark"] .chat-logout-button',
        ':root[data-color-mode="dark"] .settings-objectives-link',
        ':root[data-color-mode="dark"] .team-table-wrap',
        ':root[data-color-mode="dark"] .runtime-control-tabs .settings-tab',
        ':root[data-color-mode="dark"] .runtime-control-button.is-danger',
        ':root[data-color-mode="dark"] .permission-agent-context',
        ':root[data-color-mode="dark"] .button.danger',
    ):
        assert selector in styles_text


def test_appearance_entry_points_open_selected_theme_customizer() -> None:
    app_text = module_source(DEFAULT_ASSET_ROOT, "app.js")
    chat_text = module_source(DEFAULT_ASSET_ROOT, "chat.js")
    chat_markup = (DEFAULT_ASSET_ROOT / "chat.html").read_text(encoding="utf-8")
    styles_text = (DEFAULT_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")
    customizer_text = (DEFAULT_ASSET_ROOT / "appearance_customizer.js").read_text(
        encoding="utf-8"
    )

    welcome_renderer = app_text.split("function renderWelcome()", maxsplit=1)[1].split(
        "function renderStartOption", maxsplit=1
    )[0]
    assert "stack.appendChild(appearance.renderThemeModeControl());" in welcome_renderer
    assert 'customize.textContent = "Customize Theme";' in welcome_renderer
    assert "window.UAAppearanceCustomizer.open({" in welcome_renderer
    assert "renderDarkAppearancePanel()" not in welcome_renderer
    assert 'pageBody.classList.remove("is-centered");' in app_text

    settings_renderer = chat_text.split(
        "function renderSettingsAppearance()", maxsplit=1
    )[1].split("function normalizeMessageList", maxsplit=1)[0]
    assert 'themeHeading.textContent = "Color Theme";' in settings_renderer
    assert 'customize.textContent = "Customize Theme";' in settings_renderer
    assert "window.UAAppearanceCustomizer.open({" in settings_renderer
    assert "interfaceElement: appearanceState.chatPreview" in settings_renderer
    assert 'if (appearance.mode === "dark") {' in customizer_text
    assert "appendDarkControls(scroll" in customizer_text
    assert "appendLightControls(scroll" in customizer_text
    assert 'actionButton("button ghost theme-customizer-regenerate", "Regenerate Mesh"' in customizer_text
    assert 'actionButton("button ghost", "Cancel", cancelCustomizer)' in customizer_text
    assert 'actionButton("button primary", "Done", save)' in customizer_text
    assert 'var value = document.createElement("span");' in customizer_text
    assert 'value.className = "appearance-control-value";' in customizer_text
    assert 'document.createElement("output")' not in customizer_text
    assert 'input.setAttribute("role", "switch");' in customizer_text
    assert "tune both backgrounds" not in chat_markup

    page_body_style = styles_text.split(".page-body {", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]
    page_actions_style = styles_text.split(".page-actions {", maxsplit=1)[1].split(
        "}", maxsplit=1
    )[0]
    assert "padding: clamp(28px, 3.2vh, 38px) 0 clamp(32px, 3.8vh, 44px);" in (
        page_body_style
    )
    assert "border-top: 1px solid var(--inner-line);" in page_actions_style
    assert "padding-top: 18px;" in page_actions_style


def _contrast_ratio(first: str, second: str) -> float:
    first_luminance = _relative_luminance(first)
    second_luminance = _relative_luminance(second)
    lighter = max(first_luminance, second_luminance)
    darker = min(first_luminance, second_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _relative_luminance(hex_color: str) -> float:
    components = [int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        component / 12.92
        if component <= 0.04045
        else ((component + 0.055) / 1.055) ** 2.4
        for component in components
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def test_runtime_and_docs_share_identical_appearance_assets() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    runtime_assets = repo_root / "assistant_gateway" / "onboarding_web" / "assets"
    docs_assets = repo_root / "user_docs_source" / "assets"

    assert (runtime_assets / "appearance.js").read_bytes() == (
        docs_assets / "appearance.js"
    ).read_bytes()

    appearance_text = (runtime_assets / "appearance.js").read_text(encoding="utf-8")
    for runtime_variable in (
        "--ua-runtime-color-mode",
        "--ua-doc-light-diffusion",
        "--ua-doc-polygon-opacity-low",
        "--ua-doc-polygon-opacity-high",
        "--ua-light-dynamic",
    ):
        assert f'root.style.setProperty("{runtime_variable}"' in appearance_text
    # Dark settings go through one table; the CSS names that Docs and broker
    # pages write must be the ones appearance.js reads back.
    assert "root.style.setProperty(field.css, String(appearance.dark[field.name]))" in appearance_text
    table = dict(re.findall(r'saved: "(\w+)", css: "(--ua-dark-[\w-]+)"', appearance_text))
    assert table == DARK_CSS_VARIABLES


def test_dark_renderer_has_one_source_file() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    renderer = repo_root / "assistant_gateway" / "onboarding_web" / "assets" / "metallic_mesh.js"

    assert not (repo_root / "user_docs_source" / "assets" / "metallic_mesh.js").exists()
    assert "window.UAMetallicMesh = api;" in renderer.read_text(encoding="utf-8")
    # The offline setup guide embeds an exact copy; sync it with
    # scripts/sync_setup_guide_renderer.py.
    guide = (repo_root / "docker-compose-setup.html").read_text(encoding="utf-8")
    assert embedded_renderer(guide) == renderer.read_text(encoding="utf-8")


def test_static_docs_theme_matches_dark_defaults() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    theme = (repo_root / "user_docs_source" / "assets" / "ua-runtime-theme.css").read_text(encoding="utf-8")

    for declaration in dark_appearance_css_declarations(None):
        assert declaration in theme


def test_onboarding_env_upload_populates_visible_state_mappings() -> None:
    app_text = module_source(DEFAULT_ASSET_ROOT, "app.js")

    assert "function parseEnvFileContent(content)" in app_text
    assert "function applyEnvValuesToState(envValues, options)" in app_text
    assert "applySourceEnvContentToState({ overwriteExisting: true });" in app_text
    assert (
        '"NETWORK_PUBLIC_ORIGIN", "networkPublicOrigin", "ua.preview.network.public_origin"'
        in app_text
    )
    assert 'var webChatToken = getEnv("WEB_CHAT_AUTH_TOKEN");' in app_text
    assert 'var embeddingApiKey = getEnv("EMBEDDING_API_KEY");' in app_text
    assert '["EXA_API_KEY", "exa_api_key"]' in app_text
    assert '["BRAVE_SEARCH_API_KEY", "brave_search_api_key"]' in app_text
    assert '["XAI_API_KEY", "xai_api_key"]' in app_text
    assert '["TELEGRAM_BOT_TOKEN", "telegram", "Bot token"]' in app_text
    assert '["DISCORD_BOT_TOKEN", "discord", "Bot token"]' in app_text
    assert "advancedEnvDefinitions.forEach(function (definition)" in app_text
    assert (
        "var modelToken = getEnv(model_setup.providerEnvVar(sharedState.state.runtimeProvider));"
        in app_text
    )
    assert "function clearPersistedSecretDrafts()" in app_text
    assert "function isSensitiveChannelField(channelKey, label)" in app_text
    assert 'window.localStorage.setItem("ua.preview.runtime.token"' not in app_text
    assert 'window.localStorage.setItem("ua.preview.embedding.api_key"' not in app_text
    assert 'window.localStorage.setItem("ua.preview.website.auth.token"' not in app_text
    assert 'window.localStorage.setItem("ua.onboarding.secret."' not in app_text
    assert 'window.localStorage.setItem("ua.onboarding.env."' not in app_text
    assert 'Populated " + populatedCount + " onboarding field' in app_text


def test_onboarding_emits_only_current_sealbox_settings() -> None:
    app_text = module_source(DEFAULT_ASSET_ROOT, "app.js")

    # The single-tenant Sealbox key files were removed; the form neither
    # shows nor sends them.
    assert "legacy_public_key_file" not in app_text
    assert "legacy_private_key_file" not in app_text
    assert "sealboxLegacy" not in app_text
    assert 'api_version: "v2"' in app_text


def test_onboarding_advanced_toggles_use_label_backed_controls() -> None:
    app_text = module_source(DEFAULT_ASSET_ROOT, "app.js")
    advanced_toggle_body = app_text.split("function advancedToggle", maxsplit=1)[
        1
    ].split(
        "function persistFailoverFallbacks",
        maxsplit=1,
    )[0]

    assert 'document.createElement("label")' in advanced_toggle_body
    assert 'input.setAttribute("aria-label", labelText)' in advanced_toggle_body


def test_onboarding_channel_setup_copy_and_validation_are_user_facing() -> None:
    app_text = module_source(DEFAULT_ASSET_ROOT, "app.js")
    styles_text = (DEFAULT_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")

    assert "Photon dashboard" in app_text
    assert "sign in with Google or Discord" in app_text
    assert "--nodejs" not in app_text
    assert "communicationValidationMessages" in app_text
    assert (
        "Signal is enabled. Enter the registered Signal account or number" in app_text
    )
    assert ".communication-validation" in styles_text
    assert ".field-error" in styles_text
    assert ".setup-intro" in styles_text
    assert (
        ".generated-token-input-wrap {\n  position: relative;\n  display: grid;\n  min-width: 0;\n}"
        in styles_text
    )
    assert "@media (max-width: 560px)" in styles_text


def test_onboarding_web_chat_uses_live_public_endpoint_and_nonshrinking_channel_rows() -> (
    None
):
    app_text = module_source(DEFAULT_ASSET_ROOT, "app.js")
    styles_text = (DEFAULT_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")

    assert "function applyLiveWebsiteEndpointSettings(settings)" in app_text
    assert "state.internalWebsitePublicPort" in app_text
    assert (
        "internal_website_bind_port: sharedState.state.internalWebsiteBindPort"
        in app_text
    )
    assert (
        "internal_website_public_port: optionalNumber(sharedState.state.internalWebsitePublicPort)"
        in app_text
    )
    assert "state.documentationPort" not in app_text
    assert (
        ".channel-accordion {\n  display: grid;\n  grid-auto-rows: max-content;\n  align-content: start;"
        in styles_text
    )


def test_config_editor_auth_panel_uses_readable_title_and_contained_controls() -> None:
    editor_text = (DEFAULT_ASSET_ROOT / "config_editor.html").read_text(
        encoding="utf-8"
    )
    script_text = (DEFAULT_ASSET_ROOT / "config-editor.js").read_text(encoding="utf-8")
    styles_text = (DEFAULT_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")

    assert '<h1 id="config-editor-title">Edit Instance Config</h1>' in editor_text
    assert (
        '<a class="brand-lockup" href="/webchat" aria-label="Open Web Chat">'
        in editor_text
    )
    assert "Edit instance_config.yaml" not in editor_text
    assert "function moveSelectionVertically(direction, extendSelection)" in script_text
    assert "function insertNewlineAtSelection()" in script_text
    assert 'event.key === "Enter"' in script_text
    assert 'event.key === "ArrowUp" || event.key === "ArrowDown"' in script_text
    assert "width: min(780px, 100%);" in styles_text
    assert (
        ".config-editor-auth .chat-auth-form,\n.config-editor-auth .credential-input {"
        in styles_text
    )
    assert ".config-editor-auth .button.primary {" in styles_text
    assert "overflow-wrap: anywhere;" in styles_text


def test_config_editor_yaml_text_uses_regular_font_without_label_transform() -> None:
    styles_text = (DEFAULT_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")

    assert ".config-editor-yaml-panel > span,\n.config-editor-side h2" in styles_text
    assert ".config-editor-yaml-panel span,\n.config-editor-side h2" not in styles_text

    shared_metrics = styles_text.split(
        ".config-editor-highlight,\n.config-editor-yaml-panel textarea {",
        maxsplit=1,
    )[1].split("\n}", maxsplit=1)[0]
    highlight_span_metrics = styles_text.split(
        ".config-editor-highlight span {",
        maxsplit=1,
    )[1].split("\n}", maxsplit=1)[0]

    assert (
        "font-family: Inter, ui-sans-serif, system-ui, -apple-system, "
        'BlinkMacSystemFont, "Segoe UI", sans-serif;'
    ) in shared_metrics
    assert "font-size: 0.9rem;" in shared_metrics
    assert "font-weight: 500;" in shared_metrics
    assert "line-height: 1.5;" in shared_metrics
    assert "padding: 12px;" in shared_metrics
    assert "font: inherit;" in highlight_span_metrics
    assert "text-transform: none;" in highlight_span_metrics


def test_settings_distinguish_mcp_credentials_and_reviewer_eligibility() -> None:
    # The Settings dialog and the modules it imports (Team, Inbox, Memories, Access).
    script_text = module_source(DEFAULT_ASSET_ROOT, "settings_controls.mjs")

    assert 'operator_configured: "Set by administrator"' in script_text
    assert 'linked_account: "Uses a linked account"' in script_text
    # The Inbox empty state describes this reviewer, not every request.
    assert "Nothing is waiting for your approval." in script_text
    assert "that you cannot decide are not shown here." in script_text
    assert 'active: "Installed"' in script_text
    assert "Awaiting attachment approval" not in script_text
    assert "Only administrators review procedure proposals." in script_text


def test_dev_preview_serves_production_compatible_endpoints(tmp_path: Path) -> None:
    server, thread, base_url = _serve_preview(DEFAULT_ASSET_ROOT, export_dir=tmp_path)
    try:
        with urlopen(f"{base_url}/", timeout=5) as response:
            index_text = response.read().decode("utf-8")
        with urlopen(f"{base_url}/api/bootstrap", timeout=5) as response:
            bootstrap_payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/health", timeout=5) as response:
            health_payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/api/web-chat/settings", timeout=5) as response:
            chat_settings = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/api/agents/status", timeout=5) as response:
            agent_status = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{base_url}/webchat", timeout=5) as response:
            web_chat_shell = response.read().decode("utf-8")
        completion_payload = _json_post(
            f"{base_url}/api/onboarding/complete", {"instance_name": "preview"}
        )
        skills_validation_payload = _json_post(
            f"{base_url}/api/onboarding/skills-repos/validate",
            {"repositories": ["https://github.com/enzimo/ua-skills"]},
        )
        restart_payload = _json_post(f"{base_url}/api/onboarding/restart", {})
        chat_opener = build_opener(HTTPCookieProcessor(CookieJar()))
        auth_payload = _json_post(
            f"{base_url}/api/web-chat/auth", {"token": "dev-token"}, opener=chat_opener
        )
        sessions_payload = json.loads(
            chat_opener.open(f"{base_url}/api/web-chat/sessions", timeout=5)
            .read()
            .decode("utf-8")
        )
        team_payload = json.loads(
            chat_opener.open(
                f"{base_url}/api/web-chat/team?session_key=website%3Aweb-chat%3Adev",
                timeout=5,
            )
            .read()
            .decode("utf-8")
        )
        objectives_payload = json.loads(
            chat_opener.open(f"{base_url}/api/web-chat/objectives", timeout=5)
            .read()
            .decode("utf-8")
        )
        schedules_payload = json.loads(
            chat_opener.open(f"{base_url}/api/web-chat/schedules", timeout=5)
            .read()
            .decode("utf-8")
        )
        workspace_payload = json.loads(
            chat_opener.open(f"{base_url}/api/web-chat/workspace", timeout=5)
            .read()
            .decode("utf-8")
        )
        workspace_markdown_payload = json.loads(
            chat_opener.open(
                f"{base_url}/api/web-chat/workspace/file?path=README.md",
                timeout=5,
            )
            .read()
            .decode("utf-8")
        )
        workspace_json_payload = json.loads(
            chat_opener.open(
                f"{base_url}/api/web-chat/workspace/file?path="
                "crypto_candidate_dossiers_version_2026_08_26_final.json",
                timeout=5,
            )
            .read()
            .decode("utf-8")
        )
        with chat_opener.open(
            f"{base_url}/api/web-chat/workspace/content?path=demo.html",
            timeout=5,
        ) as response:
            workspace_html = response.read().decode("utf-8")
            workspace_html_csp = response.headers["Content-Security-Policy"]
        objective_control_payload = _json_post(
            f"{base_url}/api/web-chat/objectives/objective-preview-release/control",
            {
                "session_key": "website:web-chat:dev",
                "action": "pause",
                "expected_record_digest": "a" * 64,
            },
            opener=chat_opener,
        )
        schedule_control_payload = _json_post(
            f"{base_url}/api/web-chat/schedules/schedule-preview-release/control",
            {
                "session_key": "website:web-chat:dev",
                "action": "disable",
                "expected_record_digest": "b" * 64,
            },
            opener=chat_opener,
        )
        mcp_control_payload = _json_post(
            f"{base_url}/api/web-chat/mcp/hosted_server/hosted-preview-notes/control",
            {
                "session_key": "website:web-chat:dev",
                "action": "start",
                "expected_digest": "d" * 64,
                "expected_lifecycle": "stopped",
            },
            opener=chat_opener,
        )
        chat_payload = _json_post(
            f"{base_url}/api/web-chat/chat",
            {
                "message": "Hello",
                "session_key": "website:web-chat:dev",
                "client_request_id": "preview-request-1",
            },
            opener=chat_opener,
        )
        with urlopen(
            f"{base_url}/download/instance_config.yaml", timeout=5
        ) as response:
            config_download = response.read().decode("utf-8")
        invalid_test_request = Request(
            f"{base_url}/api/provider-test",
            data=json.dumps({}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(invalid_test_request, timeout=5)
        except HTTPError as exc:
            invalid_test_payload = json.loads(exc.read().decode("utf-8"))
            invalid_test_status = exc.code
        else:
            raise AssertionError("Expected connection test validation failure")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "Universal Agents Onboarding" in index_text
    assert bootstrap_payload["appearance"]["dynamic_background"] is True
    assert bootstrap_payload["internal_website_bind_port"] == int(
        base_url.rsplit(":", 1)[1]
    )
    assert bootstrap_payload["internal_website_public_port"] == int(
        base_url.rsplit(":", 1)[1]
    )
    assert bootstrap_payload["internal_website_base_url"] == base_url
    assert any(
        agent["type"] == "PersonalAdminAgent"
        for agent in bootstrap_payload["agent_catalog"]
    )
    assert health_payload["writes_runtime_files"] is False
    assert "Universal Agents Web Chat" in web_chat_shell
    assert 'id="chat-version"' in web_chat_shell
    assert (
        chat_settings["version"]
        == (Path(__file__).resolve().parents[1] / "VERSION")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert chat_settings["commands"][0]["syntax"] == "/help"
    link_guide = next(
        command
        for command in chat_settings["commands"]
        if command["syntax"].startswith("/link ")
    )
    assert link_guide == {
        "syntax": "/link <tool_id> [ me | team ]",
        "description": "sign in to a tool or choose the stored credential it uses",
        "documentation_url": "/docs/features/#secure-tool-authentication",
        "documentation_label": "see options",
    }
    auth_guide = next(
        command
        for command in chat_settings["commands"]
        if command["syntax"].startswith("/auth ")
    )
    assert auth_guide["syntax"] == "/auth [ status | model | google team_client | x team_client ... ]"
    assert agent_status["agents"][0]["named_agent_type"] == "TeamArchitect"
    assert completion_payload["restart_required"] is False
    assert completion_payload["dev_preview"] is True
    assert skills_validation_payload["dev_preview"] is True
    assert skills_validation_payload["repositories"][0]["ok"] is True
    assert restart_payload["restart_required"] is False
    assert auth_payload["assistant_name"] == "Ava"
    assert auth_payload["canonical_user_id"] == "usr_dev_preview"
    assert auth_payload["is_bootstrap_user"] is True
    assert sessions_payload["event_cursor"].startswith("preview-stream:")
    assert team_payload["members"][0]["named_agent_type"] == "TeamArchitect"
    # Team activity uses the production status shape; with no recent chat
    # message the preview team is idle (it works for a few seconds after one).
    assert {member["status"]["key"] for member in team_payload["members"]} <= {"busy", "available"}
    assert [item["named_agent_type"] for item in team_payload["definitions"]][0] == "TeamArchitect"
    assert team_payload["mcp_connections"][0]["available_actions"] == ["disable"]
    assert team_payload["mcp_connections"][0]["credential_mode"] == (
        "operator_configured"
    )
    assert team_payload["mcp_connections"][1]["lifecycle"] == "pending"
    assert team_payload["mcp_connections"][1]["can_decide"] is True
    assert team_payload["hosted_mcp_servers"][0]["lifecycle"] == "stopped"
    assert objectives_payload["source"] == "dev_preview"
    assert (
        objectives_payload["objectives"][0]["objective_id"]
        == "objective-preview-release"
    )
    assert objectives_payload["objectives"][0]["available_actions"] == [
        "pause",
        "stop",
        "abandon",
        "delete",
    ]
    # The sample schedules use the gateway's own schedule format, so the
    # Schedules tab can read each one's worker assignment.
    listed_schedules = WebScheduleListV1.model_validate(_model_fields(schedules_payload)).schedules
    assert "schedule-preview-release" in {item.schedule_id for item in listed_schedules}
    assert {item.management_class for item in listed_schedules} == {"user", "system"}
    assert {entry["name"] for entry in workspace_payload["entries"]} == {
        "artifacts",
        "crypto_candidate_dossiers_version_2026_08_26_final.json",
        "demo.html",
        "README.md",
        "src",
    }
    assert workspace_markdown_payload["preview_kind"] == "markdown"
    assert workspace_markdown_payload["content"].startswith(
        "# Universal Agents workspace"
    )
    assert workspace_json_payload["syntax_language"] == "JSON"
    assert workspace_json_payload["formatted_content"].startswith("{\n")
    assert "ua-syntax-" in workspace_json_payload["highlighted_html"]
    assert "Rendered HTML preview" in workspace_html
    assert "script-src 'none'" in workspace_html_csp
    assert objective_control_payload["success"] is True
    assert schedule_control_payload["action"] == "disable"
    assert mcp_control_payload["status"] == "updated"
    assert (
        "Development aesthetic preview only" in chat_payload["messages"][-1]["content"]
    )
    assert chat_payload["messages"][0]["client_request_id"] == "preview-request-1"
    assert chat_payload["messages"][-1]["client_request_id"] is None
    assert (
        "Production onboarding writes the real instance_config.yaml" in config_download
    )
    assert invalid_test_status == 400
    assert "Missing required connection fields" in invalid_test_payload["message"]


def test_dev_preview_answers_every_settings_tab_and_the_live_updates_stream() -> None:
    server, thread, base_url = _serve_preview(DEFAULT_ASSET_ROOT)
    chat_opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def get(path: str) -> dict[str, object]:
        with chat_opener.open(f"{base_url}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        _json_post(f"{base_url}/api/web-chat/auth", {"token": "dev-token"}, opener=chat_opener)
        personal = get("/api/web-chat/memories?session_key=website%3Aweb-chat%3Adev&scope=personal")
        team = get("/api/web-chat/memories?session_key=website%3Aweb-chat%3Adev&scope=team")
        lead_tasks = get("/api/web-chat/tasks?session_key=website%3Aweb-chat%3Adev&agent_id=preview-manager")
        finished = get(
            "/api/web-chat/tasks?session_key=website%3Aweb-chat%3Adev&agent_id=preview-manager&view=finished"
        )
        counts = get("/api/web-chat/access-inbox/counts")
        inbox = get("/api/web-chat/access-inbox")
        accounts = get("/api/web-chat/accounts")
        access_agents = get("/api/web-chat/access-map?view=agents")
        access_files = get("/api/web-chat/access-map?view=files")
        access_tools = get("/api/web-chat/access-map?view=tools")
        access_tools_next = get(f"/api/web-chat/access-map?view=tools&offset={access_tools['page_size']}")
        sessions = get("/api/web-chat/sessions")
        # The browser reads one server-sent event at a time; read the first.
        with chat_opener.open(f"{base_url}/api/web-chat/events/stream?after=", timeout=5) as stream:
            content_type = stream.headers["Content-Type"]
            first_event = stream.readline().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # Lists use the gateway's own models, so a format change fails here.
    personal_list = WebMemoryListV1.model_validate(_model_fields(personal))
    assert personal_list.scope == "personal" and personal_list.total == len(personal_list.memories) > 0
    assert any("apples" in memory.fact_text for memory in personal_list.memories)
    assert WebMemoryListV1.model_validate(_model_fields(team)).scope == "team"
    assert WebTaskListV1.model_validate(_model_fields(lead_tasks)).current_count == 1
    assert WebTaskListV1.model_validate(_model_fields(finished)).tasks[0].state == "completed"
    assert counts == {"status": "ok", "decidable_requests": 0, "unread_notices": 1}
    assert inbox["reviews"] == [] and inbox["notices"][0]["read"] is False
    assert {row["subject"] for row in accounts["services"]} >= {"model", "cli:github", "cli:google"}
    assert accounts["share_command"] == "/accounts share"
    assert [item["subject"] for item in access_agents["items"]] == [
        "TeamArchitect",
        "WebResearchAgent",
        "CodeWritingAgent",
    ]
    assert all(item["standard"] for item in access_agents["items"])
    assert access_files["view"] == "files" and access_files["data_folders"] == []
    # By Tool lists catalog tools as tool cards (access_map.mjs renderToolCard).
    assert access_tools["view"] == "tools" and access_tools["total"] >= len(access_tools["items"]) > 0
    assert all(item["tool_id"] and item["display_name"] and item["kind_words"] for item in access_tools["items"])
    assert access_tools_next["offset"] == access_tools["page_size"]
    assert {item["tool_id"] for item in access_tools_next["items"]}.isdisjoint(
        item["tool_id"] for item in access_tools["items"]
    )
    assert content_type == "text/event-stream"
    assert first_event.startswith("data: ")
    page = json.loads(first_event.removeprefix("data: "))
    assert page["status"] == "ok" and page["next_cursor"].startswith("preview-stream:")
    # The thread list and the live updates agree on the revision; otherwise
    # every live page makes Web Chat reload the thread list (chat.js).
    assert page["threads_revision"] == sessions["threads_revision"]
    assert [member["name"] for member in page["team_activity"]["members"]] == ["Ava", "Atlas", "Mira"]


def test_dev_preview_delegates_provider_testing_to_production_helper(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def fake_provider_test(**kwargs: object) -> ProviderTestResult:
        captured.update(kwargs)
        return ProviderTestResult(
            ok=True,
            provider=str(kwargs["provider"]),
            message="Connection test succeeded.",
            sample="ready",
        )

    monkeypatch.setattr(
        provider_test_runtime, "test_provider_connection", fake_provider_test
    )
    server, thread, base_url = _serve_preview(DEFAULT_ASSET_ROOT, export_dir=tmp_path)
    try:
        payload = _json_post(
            f"{base_url}/api/provider-test",
            {
                "provider": "OpenRouter",
                "endpoint": "https://openrouter.example/v1",
                "token": "secret",
                "model": "example/model",
                "reasoning_effort": "high",
                "reasoning_summary": "auto",
                "litellm_api_mode": "responses",
                "stream": True,
                "xai_oauth": {"enabled": False},
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert payload["sample"] == "ready"
    assert captured == {
        "provider": "OpenRouter",
        "endpoint": "https://openrouter.example/v1",
        "token": "secret",
        "model": "example/model",
        "reasoning_effort": "high",
        "reasoning_summary": "auto",
        "litellm_api_mode": "responses",
        "stream": True,
        "xai_oauth": {"enabled": False},
    }


def test_dev_preview_keeps_a_saved_web_chat_appearance_in_memory(tmp_path: Path) -> None:
    """Customize Theme's Done, Cancel and Reset work in the preview's Web Chat."""

    server, thread, base_url = _serve_preview(DEFAULT_ASSET_ROOT, tmp_path)
    try:
        opener = build_opener(HTTPCookieProcessor(CookieJar()))

        def call(method: str, body: dict[str, object] | None = None) -> dict[str, object]:
            request = Request(
                f"{base_url}/api/web-chat/appearance",
                data=json.dumps(body).encode("utf-8") if body is not None else None,
                headers={"Content-Type": "application/json"},
                method=method,
            )
            with opener.open(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        with pytest.raises(HTTPError) as signed_out:
            call("GET")
        assert signed_out.value.code == 401

        _json_post(f"{base_url}/api/web-chat/auth", {"token": "dev-token"}, opener=opener)
        assert call("GET")["appearance"] is None
        saved = call("PUT", {"appearance": {"color_mode": "dark", "dark": {"light_softness": 80}}})
        assert saved["appearance"]["dark"]["light_softness"] == 80
        assert call("GET") == saved
        with pytest.raises(HTTPError) as out_of_range:
            call("PUT", {"appearance": {"dark": {"light_x": 999}}})
        assert out_of_range.value.code == 400
        assert call("DELETE")["appearance"] is None
        assert call("GET")["appearance"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_interface_motion_effects_never_clip_their_elements() -> None:
    """Clipping overflow drops a grid item's content-based minimum height.

    A sent bubble with ``overflow: hidden`` was squeezed to a sliver in the
    scrolling transcript while its arrival effect ran (2026-10-04).
    """

    styles = (DEFAULT_ASSET_ROOT / "styles.css").read_text(encoding="utf-8")
    motion = styles.split("/* ---------- Interface motion", maxsplit=1)[1]
    assert "overflow:" not in motion
    assert ".ua-arrive-mesh {" in motion and "border-radius: inherit;" in motion

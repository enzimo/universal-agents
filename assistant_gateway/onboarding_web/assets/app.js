import { createAccountSharing } from "./app_account_sharing.mjs";
import { createTeamImport } from "./app_team_import.mjs";
import { createAppearance } from "./app_appearance.mjs";
import { createConfiguration } from "./app_configuration.mjs";
import { createFinalization } from "./app_finalization.mjs";
import { createModelSetup } from "./app_model_setup.mjs";
import { createPageLifecycle, createRequestClient } from "./browser_lifecycle.mjs";

function mountPage() {
"use strict";
const lifecycle = createPageLifecycle(window);
const requestClient = createRequestClient(window, lifecycle);
const appearanceState = {};
const finalizationState = {};
const model_setupState = {};
const sharedState = {};
const shellState = {};
const appearance = createAppearance({ appearanceState, sharedState, lifecycle, requestClient, operations: { renderCurrentPage: (...args) => renderCurrentPage(...args) } });
const configuration = createConfiguration({ sharedState, lifecycle, requestClient, operations: { applyAgentCatalog: (...args) => applyAgentCatalog(...args), applyAppearanceSettings: (...args) => appearance.applyAppearanceSettings(...args), channelFieldKey: (...args) => channelFieldKey(...args), clamp: (...args) => appearance.clamp(...args), invalidateRuntimeTest: (...args) => model_setup.invalidateRuntimeTest(...args), persistTeamSkillRepos: (...args) => persistTeamSkillRepos(...args), refreshChannelAvailability: (...args) => refreshChannelAvailability(...args), runtimeConnectionSignature: (...args) => model_setup.runtimeConnectionSignature(...args), selectedAgentCountStorageKey: (...args) => selectedAgentCountStorageKey(...args), selectedAgentStorageKey: (...args) => selectedAgentStorageKey(...args), setChannelEnabled: (...args) => setChannelEnabled(...args), setChannelFieldValue: (...args) => setChannelFieldValue(...args), setDefaultChannel: (...args) => setDefaultChannel(...args), setSafetyValue: (...args) => setSafetyValue(...args), setSecretsFromEnvironment: (...args) => setSecretsFromEnvironment(...args), setVoiceResponseMode: (...args) => setVoiceResponseMode(...args) } });
const model_setup = createModelSetup({ model_setupState, sharedState, lifecycle, requestClient, operations: { advancedInput: (...args) => advancedInput(...args), advancedSecretInput: (...args) => advancedSecretInput(...args), advancedSelect: (...args) => advancedSelect(...args), advancedToggle: (...args) => advancedToggle(...args), composedInstanceName: (...args) => composedInstanceName(...args), composedTeamId: (...args) => composedTeamId(...args), createInfoButton: (...args) => createInfoButton(...args), iconMarkup: (...args) => iconMarkup(...args), optionalBooleanSelect: (...args) => finalization.optionalBooleanSelect(...args), providerTestHttpErrorMessage: (...args) => finalization.providerTestHttpErrorMessage(...args), renderAdvancedGrid: (...args) => renderAdvancedGrid(...args), renderAdvancedSection: (...args) => renderAdvancedSection(...args), renderCurrentPage: (...args) => renderCurrentPage(...args), renderRevealableControl: (...args) => renderRevealableControl(...args), responseErrorMessage: (...args) => finalization.responseErrorMessage(...args) } });
const account_sharing = createAccountSharing({ sharedState, lifecycle, operations: { renderCurrentPage: (...args) => renderCurrentPage(...args) } });
const team_import = createTeamImport({ sharedState, lifecycle, requestClient, operations: { navigate: url => window.location.assign(url), renderCurrentPage: (...args) => renderCurrentPage(...args), renderRevealableControl: (...args) => renderRevealableControl(...args) } });
const finalization = createFinalization({ finalizationState, sharedState, lifecycle, requestClient, operations: { webChatUrl: () => webChatUrl(), accountSharingPayload: (...args) => account_sharing.payload(...args), accountSharingSummary: (...args) => account_sharing.summary(...args), agentDisplayName: (...args) => agentDisplayName(...args), channelSelectPayloadValue: (...args) => channelSelectPayloadValue(...args), clamp: (...args) => appearance.clamp(...args), commaSeparatedFieldValues: (...args) => commaSeparatedFieldValues(...args), communicationValidationText: (...args) => communicationValidationText(...args), composedInstanceName: (...args) => composedInstanceName(...args), composedTeamId: (...args) => composedTeamId(...args), currentAppearanceSettings: (...args) => appearance.currentAppearanceSettings(...args), currentXAIOAuthSettings: (...args) => model_setup.currentXAIOAuthSettings(...args), getChannelFieldValue: (...args) => getChannelFieldValue(...args), getChannelToggleValue: (...args) => getChannelToggleValue(...args), iconMarkup: (...args) => iconMarkup(...args), isTeamArchitect: (...args) => isTeamArchitect(...args), renderCurrentPage: (...args) => renderCurrentPage(...args), runtimeTokenForSelectedProvider: (...args) => model_setup.runtimeTokenForSelectedProvider(...args), setPage: (...args) => setPage(...args), teamSkillRepoUrls: (...args) => teamSkillRepoUrls(...args), validateCommunicationSettings: (...args) => validateCommunicationSettings(...args) } });
window.addEventListener("pagehide", () => lifecycle.dispose(), { once: true });

"use strict";

sharedState.credentialControls = window.UACredentialControls;

appearanceState.brandTextColor = "#f6c84c";

finalizationState.assistantPurposeDefault = "You are my trusty 24/7 assistant. You are always polite, helpful and to the point. You help me solve problems pragmatically, by swiftly tackling simple tasks and breaking down larger problems to be solved by your team. Always warn me of potential issues or security concerns and never try to deceive me. You have high autonomy but be cautious in your execution. Make sure you execute to intent rather than to a written plan. If you find during implementation of the plan that it is drifting from the intent, stop and adjust your approach. Executing to intent is **paramount**. You must make sure to verify that the intent is clear and whenever there are deviations in code or plan, to communicate with me to clarify intent and to change the plan + code to execute to intent.";

shellState.DEFAULT_TEAM_SKILLS_REPO = "https://github.com/enzimo/ua-skills";

model_setupState.CODEX_CLI_DOCS_URL = "https://developers.openai.com/codex/cli";

sharedState.DEFAULT_RUNTIME_PROVIDER = "OpenRouter";

sharedState.SECRETS_SOURCE_ENV_FILE = "env_file";

sharedState.SECRETS_SOURCE_ENVIRONMENT = "environment";

sharedState.SECRETS_FROM_ENVIRONMENT_STORAGE_KEY = "ua.onboarding.secrets.from_environment";

sharedState.SECRETS_FROM_ENVIRONMENT_LABEL = "Secrets come from the environment (Docker secrets). Don't write team.env.";

sharedState.SECRETS_FROM_ENVIRONMENT_HELP = "Turn this on when every secret reaches the container as an environment variable or Docker secret, as in Docker Swarm. Onboarding then writes no team.env and creates no secrets. After you finalize, it lists any secret the environment is missing and how to create it.";

sharedState.DEFAULT_RUNTIME_ENDPOINT = "https://openrouter.ai/api/v1";

sharedState.DEFAULT_RUNTIME_MODEL = "deepseek/deepseek-v4.1-flash";

// The secure tool broker always runs, so Safety has no switch that turns it off.
sharedState.safetyChoices = [
    {
      key: "generate_internal_credentials",
      label: "Generate new internal credentials",
      enabledByDefault: true,
      help: "Creates missing internal credentials for the services your team needs to run. If you uploaded an .env file earlier, any matching credentials already present in that file are preserved.",
    },
  ];

model_setupState.timezoneOptions = [
    "America/Chicago",
    "America/New_York",
    "America/Los_Angeles",
    "America/Denver",
    "America/Phoenix",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Amsterdam",
    "Europe/Stockholm",
    "Europe/Warsaw",
    "Europe/Zurich",
    "Europe/Dublin",
    "Asia/Tokyo",
    "Asia/Kolkata",
    "Asia/Shanghai",
    "Asia/Seoul",
    "Asia/Singapore",
    "Asia/Hong_Kong",
    "Asia/Dubai",
    "Asia/Bangkok",
    "Australia/Sydney",
    "UTC",
  ];

model_setupState.providerDefinitions = [
    { key: "openai", label: "OpenAI", endpoint: "https://api.openai.com/v1" },
    {
      key: "openai-codex",
      label: "OpenAI Codex OAuth",
      endpoint: "https://chatgpt.com/backend-api/codex",
      defaultModel: "gpt-5.5",
      modelPlaceholder: "gpt-5.5",
      tokenLabel: "Access token (optional)",
      tokenOptional: true,
      tokenPlaceholder: "Leave blank after Codex CLI login, or paste OPENAI_CODEX_ACCESS_TOKEN",
    },
    {
      key: "xai-oauth",
      label: "xAI Grok OAuth",
      endpoint: "https://api.x.ai/v1",
      defaultModel: "grok-4.5",
      modelPlaceholder: "grok-4.5",
      tokenOptional: true,
      oauthDevice: true,
    },
    { key: "anthropic", label: "Anthropic", endpoint: "https://api.anthropic.com/v1" },
    { key: "google-ai", label: "Google AI", endpoint: "https://generativelanguage.googleapis.com/v1beta" },
    {
      key: "openrouter",
      label: sharedState.DEFAULT_RUNTIME_PROVIDER,
      endpoint: sharedState.DEFAULT_RUNTIME_ENDPOINT,
      defaultModel: sharedState.DEFAULT_RUNTIME_MODEL,
      modelPlaceholder: sharedState.DEFAULT_RUNTIME_MODEL,
    },
    { key: "openai-compatible", label: "OpenAI Compatible", endpoint: "" },
  ];

model_setupState.providerHelpCopy = "OpenAI uses the public OpenAI API with an API key. OpenAI Codex OAuth uses your Codex subscription login. xAI Grok OAuth uses device login. Anthropic and Google AI connect directly to those providers. OpenRouter provides one API for many hosted models. OpenAI Compatible connects to another Chat Completions-compatible server, such as LiteLLM, vLLM, or Ollama. Choose the option that matches where your model is hosted and how you authenticate.";

sharedState.pages = [
    {
      id: "welcome",
      label: "Welcome",
      kicker: "First Contact",
      title: "Agentic Future Belongs to Everyone",
      copy: "This is why these Universal Agents are free. The team you create will grow with you and your imagination. This requires a bit of patience up front as we work through the details. Most important thing first - make it yours by customizing the look and feel below.",
      kind: "welcome",
    },
    {
      id: "import",
      label: "Let's Begin",
      kicker: "Starting Point",
      title: "Choose Your Starting Point",
      copy: "Start fresh or carry forward existing configuration files. Either way we got you covered!",
      kind: "begin",
    },
    {
      id: "identity",
      label: "Identity",
      kicker: "Human And Assistant",
      title: "Define Your Agent and Purpose",
      copy: "While the full agentic team works on your behalf, you will primarily interact with the team leader. It is important to give your leader an identity and define the style of your interactions.",
      kind: "fields",
      fields: [
        ["Your display name", "text", "Ada"],
        ["User role", "text", "Platform engineer"],
        ["Assistant name", "text", "Lisa"],
        ["Assistant purpose", "textarea", finalizationState.assistantPurposeDefault, "large"],
        ["Response style", "textarea", "Concise and direct. Talk to me normally and do not use jargon unless something requires technical precision.", "large"],
      ],
    },
    {
      id: "runtime",
      label: "Runtime Basics",
      kicker: "Instance Shape",
      title: "Set Up Your Runtime",
      copy: "",
      kind: "runtime",
    },
    {
      id: "safety",
      label: "Safety",
      kicker: "Be Safe",
      title: "Define the Safety Perimeter",
      copy: "Choose how the runtime handles credentials and internal services.",
      kind: "choices",
      choices: sharedState.safetyChoices,
    },
    {
      id: "channels",
      label: "Communication",
      kicker: "Communication Chanels",
      title: "Let's Talk",
      copy: "",
      kind: "channels",
    },
    {
      id: "preferences",
      label: "User Preferences",
      kicker: "Reachability",
      title: "Set Your Preferences",
      copy: "This is your opportunity to customize when and how the team reaches out to you.",
      kind: "preferences",
    },
    {
      id: "agents",
      label: "Team",
      kicker: "Team Composition",
      title: "Select Your Team",
      copy: "",
      kind: "agents",
    },
    {
      id: "secrets",
      label: "Secrets",
      kicker: "Runtime Inputs",
      title: "Give Your Agents Reach",
      copy: "Agents are only as useful as the tools that we give them. To make them most effective, they will need access to the web. We recommend starting with the Exa and (or) Brave free search tiers. These will give you enough runway to get your feet wet. If you'd like to talk to your agent via voice, we recommend xAI API as a solid and a fairly cheap option.",
      kind: "secrets",
    },
    {
      id: "review",
      label: "Review",
      kicker: "Review",
      title: "Let's Get Ready to Launch!",
      copy: "Let's take one final look at your selections. If everything looks good, go ahead and finalize the configuration. Then download your configuration files and save them in a safe place before launching your team.",
      kind: "review",
    },
    {
      id: "standby",
      label: "Launch",
      kicker: "Container Restart",
      title: "We're Launching Your Team!",
      copy: "We're on our way to launch your team. After the system restarts, open Status to sign in and check your team.",
      kind: "standby",
    },
    {
      id: "status",
      label: "Status",
      kicker: "Team Online",
      title: "Your Team Has Started",
      copy: "The team is up and running.",
      kind: "status",
    },
  ];

sharedState.agents = [];

sharedState.channelDefinitions = [
    {
      key: "website",
      label: "Web Chat",
      enabledByDefault: true,
      fields: [
        ["Public IP/URL", "text", ""],
        ["Session policy", "select", "One session per browser"],
        ["Authentication token", "generated-secret", "Optional token"],
      ],
    },
    {
      key: "discord",
      label: "Discord",
      enabledByDefault: false,
      fields: [
        ["Bot token", "password", "Discord bot token"],
        ["Allowed user IDs", "text", "Optional comma-separated user IDs"],
        ["Allowed channel IDs", "text", "Optional comma-separated channel IDs"],
        ["Group policy", "select", "Mention only"],
      ],
    },
    {
      key: "telegram",
      label: "Telegram",
      enabledByDefault: false,
      fields: [
        ["Bot token", "password", "Telegram bot token"],
        ["Allowed user IDs", "text", "Optional comma-separated user IDs"],
        ["Allowed chat IDs", "text", "Optional comma-separated chat IDs"],
        ["Offline policy", "select", "Pause polling"],
        ["Rich messages", "toggle", true],
      ],
    },
    {
      key: "signal",
      label: "Signal",
      enabledByDefault: false,
      fields: [
        ["REST API URL", "text", "http://127.0.0.1:8080"],
        ["Account", "text", "Registered Signal account or number"],
        ["Allowed sender IDs", "text", "Optional comma-separated sender IDs"],
        ["Allowed group IDs", "text", "Optional comma-separated group IDs"],
        ["Group policy", "select", "Mention only"],
        ["Mention markers", "text", "Comma-separated markers, e.g. @Lisa"],
      ],
    },
    {
      key: "photon",
      label: "iMessage (Photon)",
      enabledByDefault: false,
      requiresNodejs: true,
      unavailableNote: "Requires Node.js 24 LTS",
      fields: [
        ["Project ID", "text", "PHOTON_PROJECT_ID"],
        ["Project secret", "password", "PHOTON_PROJECT_SECRET"],
        ["Allowed sender IDs", "text", "Optional comma-separated sender IDs"],
        ["Allowed group IDs", "text", "Optional comma-separated group IDs"],
        ["Group policy", "select", "Mention only"],
        ["Mention markers", "text", "Comma-separated markers, e.g. @Lisa"],
      ],
    },
    {
      key: "whatsapp",
      label: "WhatsApp",
      enabledByDefault: false,
      staticUnavailable: true,
      unavailable: true,
      fields: [
        ["Webhook path", "text", "/webhooks/whatsapp"],
        ["Allowed sender id", "text", "Optional sender"],
      ],
    },
  ];

// The model, embedding, xAI, Exa and Brave keys are never written to team.env
// (a variable there would override every key saved later): onboarding keeps
// them for the gateway, which stores them in the credential store at its first
// start (assistant_gateway.onboarding_account_keys), as the team's account for
// a service shared below and as the administrator's own for a private one.
shellState.CREDENTIAL_STORE_KEYS_NOTE = "These keys and the model key are not written to team.env. When the assistant starts, they are saved in the credential store: a shared service's key as the team's account, a private one's as your own. The model key is always the team's. Change keys later with /auth model and /link, and what is shared with /accounts.";

shellState.secretDefinitions = [
    {
      key: "exa_api_key",
      label: "Exa Search API Key",
      linkText: "get it free here",
      href: "https://dashboard.exa.ai/",
      placeholder: "Exa API key",
    },
    {
      key: "brave_search_api_key",
      label: "Brave Search API Key",
      linkText: "get it free here",
      href: "https://brave.com/search/api/",
      placeholder: "Brave Search API key",
    },
    {
      key: "xai_api_key",
      label: "XAI Voice API Key",
      linkText: "get it here",
      href: "https://console.x.ai/",
      placeholder: "xAI API key",
    },
  ];

shellState.advancedEnvDefinitions = [
    {
      key: "crawl4ai_mcp_server_url",
      env: "CRAWL4AI_MCP_SERVER_URL",
      label: "Crawl4AI MCP server URL",
      type: "url",
      placeholder: "http://crawl4ai:11235/mcp/sse",
    },
    {
      key: "postgres_dsn",
      env: "POSTGRES_DSN",
      label: "Postgres DSN",
      type: "password",
      placeholder: "postgresql://user:password@host:5432/database",
    },
    {
      key: "searxng_base_url",
      env: "SEARXNG_BASE_URL",
      label: "SearXNG base URL",
      type: "url",
      placeholder: "https://searxng.example",
    },
    {
      key: "searxng_verify_tls",
      env: "SEARXNG_VERIFY_TLS",
      label: "SearXNG verify TLS",
      type: "select",
      options: [
        ["", "Use default"],
        ["true", "True"],
        ["false", "False"],
      ],
    },
    {
      key: "searxng_timeout_seconds",
      env: "SEARXNG_TIMEOUT_SECONDS",
      label: "SearXNG timeout seconds",
      type: "number",
      min: 0,
      placeholder: "10",
    },
    {
      key: "searxng_default_num_results",
      env: "SEARXNG_DEFAULT_NUM_RESULTS",
      label: "SearXNG default results",
      type: "number",
      min: 1,
      placeholder: "10",
    },
  ];

sharedState.lightDefinitions = [
    { key: "blue", label: "Light 1", defaultColor: "#082f73", x: 0.24, y: 0.28, phase: 0.4 },
    { key: "yellow", label: "Light 2", defaultColor: "#ffd60a", x: 0.74, y: 0.26, phase: 2.2 },
    { key: "green", label: "Light 3", defaultColor: "#20f05a", x: 0.28, y: 0.76, phase: 4.1 },
    { key: "red", label: "Light 4", defaultColor: "#ff3030", x: 0.76, y: 0.75, phase: 5.6 },
  ];

function channelFieldKey(channelKey, label) {
    return channelKey + "." + String(label || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  }

function isSensitiveChannelField(channelKey, label) {
    return (
      (channelKey === "discord" || channelKey === "telegram") &&
      String(label || "").toLowerCase() === "bot token"
    ) || (
      channelKey === "photon" &&
      String(label || "").toLowerCase() === "project secret"
    ) || (
      channelKey === "website" &&
      String(label || "").toLowerCase() === "authentication token"
    );
  }

function clearPersistedSecretDrafts() {
    [
      "ua.preview.runtime.token",
      "ua.preview.embedding.api_key",
      "ua.preview.website.auth.token",
    ].forEach(function (storageKey) {
      window.localStorage.removeItem(storageKey);
    });
    shellState.secretDefinitions.forEach(function (definition) {
      window.localStorage.removeItem("ua.onboarding.secret." + definition.key);
    });
    shellState.advancedEnvDefinitions.forEach(function (definition) {
      window.localStorage.removeItem("ua.onboarding.env." + definition.key);
    });
    sharedState.channelDefinitions.forEach(function (channel) {
      channel.fields.forEach(function (field) {
        if (isSensitiveChannelField(channel.key, field[0])) {
          window.localStorage.removeItem("ua.onboarding.channel." + channelFieldKey(channel.key, field[0]));
        }
      });
    });
  }

function clearLegacyRuntimeDefaults() {
    var providerKey = "ua.preview.runtime.provider";
    var endpointKey = "ua.preview.runtime.endpoint";
    var modelKey = "ua.preview.runtime.model";
    var storedProvider = window.localStorage.getItem(providerKey);
    var storedEndpoint = window.localStorage.getItem(endpointKey);
    var storedModel = window.localStorage.getItem(modelKey);
    var hasStoredRuntimeValue = [storedProvider, storedEndpoint, storedModel].some(function (value) {
      return value !== null;
    });
    var legacyProvider = storedProvider === null || storedProvider === "OpenAI Compatible";
    var legacyEndpoint = (
      storedEndpoint === null ||
      storedEndpoint === "" ||
      storedEndpoint === "http://localhost:4000"
    );
    var legacyModel = (
      storedModel === null ||
      storedModel === "" ||
      storedModel === "deepseek/deepseek-v4-flash" ||
      storedModel === "minimax/minimax-m3"
    );

    if (!hasStoredRuntimeValue || !legacyProvider || !legacyEndpoint || !legacyModel) {
      return;
    }

    window.localStorage.removeItem(providerKey);
    window.localStorage.removeItem(endpointKey);
    window.localStorage.removeItem(modelKey);
  }

clearPersistedSecretDrafts();

clearLegacyRuntimeDefaults();

function loadOnboardingAppearanceDraft() {
    var shared = window.UAAppearance
      ? window.UAAppearance.current()
      : { mode: "dark", dark: {} };
    try {
      var storedDark = JSON.parse(
        window.localStorage.getItem("ua.preview.appearance.dark") || "null"
      );
      var storedMode = window.localStorage.getItem("ua.preview.color_mode");
      if (window.UAAppearance && (storedDark || storedMode)) {
        return window.UAAppearance.normalize({
          color_mode: storedMode || shared.mode,
          light: shared.light,
          dark: storedDark || shared.dark,
        }, shared);
      }
    } catch (_error) {
      window.localStorage.removeItem("ua.preview.appearance.dark");
    }
    return shared;
  }

sharedState.initialSharedAppearance = loadOnboardingAppearanceDraft();

sharedState.state = {
    pageIndex: appearance.clamp(parseInt(window.localStorage.getItem("ua.preview.page") || "0", 10), 0, sharedState.pages.length - 1),
    furthestPageIndex: appearance.clamp(parseInt(window.localStorage.getItem("ua.preview.furthest_page") || "0", 10), 0, sharedState.pages.length - 1),
    selectedAgents: {},
    selectedAgentCounts: {},
    lightColors: loadLightColors(),
    polygonOpacityLow: appearance.loadNumberSetting("ua.preview.polygon.opacity.low", 20, 0, 100),
    polygonOpacityHigh: appearance.loadNumberSetting("ua.preview.polygon.opacity.high", 70, 0, 100),
    lightDiffusion: appearance.loadNumberSetting("ua.preview.light.diffusion", 58, 0, 100),
    polygonLineColor: appearance.normalizeHexColor(
      window.localStorage.getItem("ua.preview.polygon.line.color"),
      "#202426"
    ),
    polygonLineOpacity: appearance.loadNumberSetting("ua.preview.polygon.line.opacity", 8, 0, 100),
    lightMeshSeed: appearance.loadNumberSetting(
      "ua.preview.polygon.seed",
      sharedState.initialSharedAppearance.light.seed,
      1,
      4294967295
    ),
    backgroundDynamic: window.localStorage.getItem("ua.preview.background.dynamic") !== "false",
    colorMode: sharedState.initialSharedAppearance.mode,
    darkAppearance: sharedState.initialSharedAppearance.dark,
    enabledChannels: loadEnabledChannels(),
    safetyValues: loadSafetyValues(),
    channelFieldValues: loadChannelFieldValues(),
    secretValues: loadSecretValues(),
    advancedEnvValues: loadAdvancedEnvValues(),
    nodejsAvailable: true,
    activeChannel: window.localStorage.getItem("ua.preview.channel.active") || "website",
    defaultChannel: window.localStorage.getItem("ua.preview.channel.default") || "website",
    beginMode: window.localStorage.getItem("ua.preview.begin.mode") || "new",
    beginImportMessage: "",
    beginImportSkipped: [],
    // Let's Begin > Import a team export (app_team_import.mjs).
    teamImport: { loaded: false, loading: false, exports: [], importDir: "", problem: "", selected: "", message: "", failed: false, opening: false },
    secretsFromEnvironment: window.localStorage.getItem(sharedState.SECRETS_FROM_ENVIRONMENT_STORAGE_KEY) === "true",
    sourceEnvFilename: "",
    sourceEnvContent: "",
    userDisplayName: window.localStorage.getItem("ua.onboarding.user.display_name") || "Ada",
    userRole: window.localStorage.getItem("ua.onboarding.user.role") || "",
    assistantName: window.localStorage.getItem("ua.preview.assistant.name") || "Lisa",
    assistantPurpose: window.localStorage.getItem("ua.onboarding.assistant.purpose") || finalizationState.assistantPurposeDefault,
    assistantResponseStyle: window.localStorage.getItem("ua.onboarding.assistant.response_style") || "Concise and direct. Talk to me normally and do not use jargon unless something requires technical precision.",
    runtimeInstanceName: window.localStorage.getItem("ua.preview.runtime.instance.name") || "",
    runtimeTeamId: window.localStorage.getItem("ua.preview.runtime.team.id") || "",
    runtimeTimezone: window.localStorage.getItem("ua.preview.runtime.timezone") || detectTimezone(),
    runtimeProvider: window.localStorage.getItem("ua.preview.runtime.provider") || sharedState.DEFAULT_RUNTIME_PROVIDER,
    runtimeEndpoint: window.localStorage.getItem("ua.preview.runtime.endpoint") || sharedState.DEFAULT_RUNTIME_ENDPOINT,
    runtimeToken: "",
    runtimeModel: window.localStorage.getItem("ua.preview.runtime.model") || sharedState.DEFAULT_RUNTIME_MODEL,
    runtimeOpenAICodex: loadJsonSetting("ua.preview.runtime.openai_codex", null),
    runtimeXAIOAuth: loadJsonSetting("ua.preview.runtime.xai_oauth", null),
    xaiDeviceLoginId: "",
    xaiDeviceLoginStatus: "idle",
    xaiDeviceLoginMessage: "",
    xaiDeviceVerificationUri: "",
    xaiDeviceVerificationUriComplete: "",
    xaiDeviceUserCode: "",
    xaiDeviceExpiresAt: 0,
    xaiDevicePollIntervalSeconds: 3,
    xaiDevicePollTimer: 0,
    xaiDeviceCountdownTimer: 0,
    runtimeReasoningEffort: window.localStorage.getItem("ua.preview.runtime.reasoning_effort") || "medium",
    runtimeReasoningSummary: window.localStorage.getItem("ua.preview.runtime.reasoning_summary") || "",
    runtimeLiteLLMApiMode: window.localStorage.getItem("ua.preview.runtime.litellm_api_mode") || "chat_completions",
    runtimeContextWindowTokens: appearance.loadOptionalNumberSetting("ua.preview.runtime.context_window_tokens"),
    runtimeStream: window.localStorage.getItem("ua.preview.runtime.stream") || "",
    runtimeSupportsImages: window.localStorage.getItem("ua.preview.runtime.supports_image_input") === "true",
    runtimeSupportsAudio: window.localStorage.getItem("ua.preview.runtime.supports_audio_input") === "true",
    runtimeSupportsFiles: window.localStorage.getItem("ua.preview.runtime.supports_file_input") === "true",
    embeddingEnabled: window.localStorage.getItem("ua.preview.embedding.enabled") === "true",
    embeddingProvider: window.localStorage.getItem("ua.preview.embedding.provider") || "openai_compatible",
    embeddingModelId: window.localStorage.getItem("ua.preview.embedding.model_id") || "",
    embeddingApiBase: window.localStorage.getItem("ua.preview.embedding.api_base") || "",
    embeddingApiKey: "",
    embeddingDimensions: appearance.loadOptionalNumberSetting("ua.preview.embedding.dimensions"),
    embeddingTimeoutSeconds: appearance.loadOptionalNumberSetting("ua.preview.embedding.timeout_seconds"),
    embeddingOrganization: window.localStorage.getItem("ua.preview.embedding.organization") || "",
    llmFailoverEnabled: window.localStorage.getItem("ua.preview.failover.enabled") === "true",
    llmFailoverMinTurns: appearance.loadNumberSetting("ua.preview.failover.min_turns", 3, 1, 999),
    llmFailoverMinSeconds: appearance.loadNumberSetting("ua.preview.failover.min_seconds", 900, 1, 604800),
    llmFailoverFailureThreshold: appearance.loadNumberSetting("ua.preview.failover.failure_threshold", 3, 1, 99),
    llmFailoverFailureWindowSeconds: appearance.loadNumberSetting("ua.preview.failover.failure_window_seconds", 3600, 1, 604800),
    llmFailoverDemotionSeconds: appearance.loadNumberSetting("ua.preview.failover.demotion_seconds", 86400, 0, 31536000),
    llmFailoverNotifyUser: window.localStorage.getItem("ua.preview.failover.notify_user") !== "false",
    llmFailoverFallbacks: loadJsonSetting("ua.preview.failover.fallbacks", []),
    runtimeTestStatus: "",
    runtimeTestStatusKind: "pending",
    runtimeTestSucceeded: false,
    runtimeTestedConnectionSignature: "",
    websiteAuthToken: "",
    networkPublicOrigin: window.localStorage.getItem("ua.preview.network.public_origin") || window.location.origin,
    internalWebsiteBindPort: appearance.loadNumberSetting("ua.preview.internal_website.bind_port", 18090, 1, 65535),
    internalWebsitePublicPort: appearance.loadOptionalNumberSetting("ua.preview.documentation.port"),
    quietHoursEnabled: window.localStorage.getItem("ua.preview.quiet.enabled") !== "false",
    quietHoursStart: appearance.loadNumberSetting("ua.preview.quiet.start", 22, 0, 23),
    quietHoursEnd: appearance.loadNumberSetting("ua.preview.quiet.end", 8, 0, 23),
    quietHoursUrgent: window.localStorage.getItem("ua.preview.quiet.urgent") !== "false",
    // Not editable here: "broker" unless an imported or existing configuration says "openshell".
    securityLayerMode: "broker",
    secureToolBrokerInternalBaseUrl: window.localStorage.getItem("ua.preview.security.broker_internal_base_url") || "http://127.0.0.1:18600",
    secureToolBrokerPublicPort: appearance.loadOptionalNumberSetting("ua.preview.security.broker_public_port"),
    secureToolBrokerBindHost: window.localStorage.getItem("ua.preview.security.broker_bind_host") || "127.0.0.1",
    secureToolBrokerBindPort: appearance.loadOptionalNumberSetting("ua.preview.security.broker_bind_port"),
    secureToolBrokerCredentialsDir: window.localStorage.getItem("ua.preview.security.broker_credentials_dir") || "broker_credentials",
    credentialStateBackendMode: window.localStorage.getItem("ua.preview.security.credential_state_backend.mode") || "credential_source",
    credentialStateBackendSource: window.localStorage.getItem("ua.preview.security.credential_state_backend.source") || "sealbox",
    credentialStateBackendNamespace: window.localStorage.getItem("ua.preview.security.credential_state_backend.namespace") || "team-cred-store",
    credentialStateBackendFallbackToFiles: window.localStorage.getItem("ua.preview.security.credential_state_backend.fallback_to_files") === "true",
    credentialCatalogEnabled: window.localStorage.getItem("ua.preview.security.credential_catalog.enabled") !== "false",
    credentialCatalogDatabasePath: window.localStorage.getItem("ua.preview.security.credential_catalog.database_path") || "credential_catalog.db",
    credentialCatalogTemplateOverridesPath: window.localStorage.getItem("ua.preview.security.credential_catalog.template_overrides_path") || "credential_template_overrides.yaml",
    credentialCatalogDefaultWriteSource: window.localStorage.getItem("ua.preview.security.credential_catalog.default_write_source") || "sealbox",
    credentialCatalogCollectionSessionTtlSeconds: appearance.loadNumberSetting("ua.preview.security.credential_catalog.collection_session_ttl_seconds", 900, 1, 86400),
    credentialCatalogSecretCacheTtlSeconds: appearance.loadNumberSetting("ua.preview.security.credential_catalog.secret_cache_ttl_seconds", 300, 0, 86400),
    bitwardenServerUrl: window.localStorage.getItem("ua.preview.security.bitwarden_server_url") || "",
    sealboxEnabled: window.localStorage.getItem("ua.preview.security.sealbox.enabled") !== "false",
    sealboxManageService: window.localStorage.getItem("ua.preview.security.sealbox.manage_service") !== "false",
    sealboxAutoInitialize: window.localStorage.getItem("ua.preview.security.sealbox.auto_initialize") !== "false",
    sealboxBindHost: window.localStorage.getItem("ua.preview.security.sealbox.bind_host") || "127.0.0.1",
    sealboxBindPort: appearance.loadNumberSetting("ua.preview.security.sealbox.bind_port", 8767, 1, 65535),
    sealboxStorePath: window.localStorage.getItem("ua.preview.security.sealbox.store_path") || "dbs/sealbox.db",
    sealboxServiceUrl: window.localStorage.getItem("ua.preview.security.sealbox.service_url") || "",
    sealboxRootTokenFile: window.localStorage.getItem("ua.preview.security.sealbox.root_token_file") || "sealbox/root_token",
    sealboxTenantMaterialDir: window.localStorage.getItem("ua.preview.security.sealbox.tenant_material_dir") || "sealbox/tenants",
    sealboxReadyTimeoutSeconds: appearance.loadNumberSetting("ua.preview.security.sealbox.ready_timeout_seconds", 30, 1, 300),
    activityCollectorBaseUrl: window.localStorage.getItem("ua.preview.activity.base_url") || "http://127.0.0.1:8765",
    activityCollectorConnectTimeoutSeconds: appearance.loadNumericSetting("ua.preview.activity.connect_timeout_seconds", 1, 0.001, 60),
    activityCollectorRequestTimeoutSeconds: appearance.loadNumericSetting("ua.preview.activity.request_timeout_seconds", 2, 0.001, 120),
    activityCollectorBatchMaxEvents: appearance.loadNumberSetting("ua.preview.activity.batch_max_events", 25, 1, 1000),
    activityCollectorFlushIntervalSeconds: appearance.loadNumericSetting("ua.preview.activity.flush_interval_seconds", 1, 0.001, 60),
    activityCollectorMaxBufferedEvents: appearance.loadNumberSetting("ua.preview.activity.max_buffered_events", 500, 1, 100000),
    activityCollectorDropWhenUnavailable: window.localStorage.getItem("ua.preview.activity.drop_when_unavailable") !== "false",
    activityCollectorBindHost: window.localStorage.getItem("ua.preview.activity.bind_host") || "127.0.0.1",
    activityCollectorBindPort: appearance.loadNumberSetting("ua.preview.activity.bind_port", 8765, 1, 65535),
    activityCollectorUserUrlPath: window.localStorage.getItem("ua.preview.activity.user_url_path") || "/user",
    activityCollectorAdminUrlPath: window.localStorage.getItem("ua.preview.activity.admin_url_path") || "/admin",
    activityCollectorOtelTraceBaseUrl: window.localStorage.getItem("ua.preview.activity.otel_trace_base_url") || "",
    activityCollectorIngestTokenEnvVar: window.localStorage.getItem("ua.preview.activity.ingest_token_env_var") || "ACTIVITY_COLLECTOR_INGEST_TOKEN",
    activityCollectorStorageBackend: window.localStorage.getItem("ua.preview.activity.storage.backend") || "sqlite",
    activityCollectorStorageSqlitePath: window.localStorage.getItem("ua.preview.activity.storage.sqlite.path") || "dbs/activity_collector.db",
    voiceEnabled: window.localStorage.getItem("ua.preview.voice.enabled") === "true",
    voiceResponseMode: window.localStorage.getItem("ua.preview.voice.response_mode") || "voice_and_text",
    internalWebsiteDocsRoot: window.localStorage.getItem("ua.preview.internal_website.docs_root") || "",
    internalWebsiteSkillsDir: window.localStorage.getItem("ua.preview.internal_website.skills_dir") || "",
    teamSkillRepos: loadTeamSkillRepos(),
    teamSkillsValidationStatus: "",
    teamSkillsValidationKind: "pending",
    teamSkillsValidationInFlight: false,
    configurationFinalized: false,
    configurationFinalizing: false,
    configurationStatus: "",
    configurationStatusKind: "pending",
    missingSecrets: [],
    unsavedSecretNames: [],
    communicationValidationMessages: [],
    communicationInvalidFields: {},
    standbyStartedAt: 0,
  };

if (sharedState.state.polygonOpacityLow > sharedState.state.polygonOpacityHigh) {
    sharedState.state.polygonOpacityLow = sharedState.state.polygonOpacityHigh;
  }

if (!sharedState.state.runtimeEndpoint && model_setup.providerByLabel(sharedState.state.runtimeProvider).endpoint) {
    sharedState.state.runtimeEndpoint = model_setup.providerByLabel(sharedState.state.runtimeProvider).endpoint;
    window.localStorage.setItem("ua.preview.runtime.endpoint", sharedState.state.runtimeEndpoint);
  }

if (!sharedState.state.runtimeModel.trim() && model_setup.providerByLabel(sharedState.state.runtimeProvider).defaultModel) {
    sharedState.state.runtimeModel = model_setup.providerByLabel(sharedState.state.runtimeProvider).defaultModel;
    window.localStorage.setItem("ua.preview.runtime.model", sharedState.state.runtimeModel);
  }

function refreshChannelAvailability() {
    sharedState.channelDefinitions.forEach(function (channel) {
      channel.unavailable = Boolean(
        channel.staticUnavailable ||
        (channel.requiresNodejs && !sharedState.state.nodejsAvailable)
      );
      if (channel.unavailable) {
        sharedState.state.enabledChannels[channel.key] = false;
        window.localStorage.setItem("ua.preview.channel.enabled." + channel.key, "false");
      }
    });
    if (!sharedState.channelDefinitions.some(function (channel) { return channel.key === sharedState.state.activeChannel && !channel.unavailable; })) {
      var nextActive = sharedState.channelDefinitions.find(function (channel) {
        return !channel.unavailable && sharedState.state.enabledChannels[channel.key];
      });
      sharedState.state.activeChannel = nextActive ? nextActive.key : "website";
      window.localStorage.setItem("ua.preview.channel.active", sharedState.state.activeChannel);
    }
    if (!sharedState.channelDefinitions.some(function (channel) { return channel.key === sharedState.state.defaultChannel && !channel.unavailable; })) {
      var nextDefault = sharedState.channelDefinitions.find(function (channel) {
        return !channel.unavailable && sharedState.state.enabledChannels[channel.key];
      });
      sharedState.state.defaultChannel = nextDefault ? nextDefault.key : "website";
      window.localStorage.setItem("ua.preview.channel.default", sharedState.state.defaultChannel);
    }
  }

function selectedAgentStorageKey(agentType) {
    return "ua.preview.agent.selected." + agentType;
  }

function selectedAgentCountStorageKey(agentType) {
    return "ua.preview.agent.count." + agentType;
  }

function normalizeAgentDefinition(agent) {
    var agentType = String(agent.type || agent.named_agent_type || "").trim();
    if (!agentType) {
      return null;
    }
    return [
      agentType,
      String(agent.summary || agent.role || agent.display_name || agentType),
      Boolean(agent.locked),
      appearance.clamp(Number(agent.default_count === undefined ? 1 : agent.default_count), 0, 8),
      Boolean(agent.default_enabled),
      String(agent.description || agent.summary || ""),
      String(agent.display_name || agentType),
      agent.visible !== false,
    ];
  }

function isTeamArchitect(agent) {
    return agent[0] === "TeamArchitect";
  }

function agentDisplayName(agent) {
    return isTeamArchitect(agent) ? "Team Architect (" + (sharedState.state.assistantName || "Lisa") + ")" : (agent[6] || agent[0]);
  }

function initializeAgentSelections() {
    sharedState.agents.forEach(function (agent) {
      var agentType = agent[0];
      var storedSelected = window.localStorage.getItem(selectedAgentStorageKey(agentType));
      sharedState.state.selectedAgents[agentType] = agent[2]
        ? true
        : (storedSelected === null ? Boolean(agent[4]) : storedSelected === "true");
      sharedState.state.selectedAgentCounts[agentType] = appearance.loadNumberSetting(selectedAgentCountStorageKey(agentType), agent[3], 0, 8);
    });
  }

function applyAgentCatalog(catalog) {
    if (!Array.isArray(catalog)) {
      return false;
    }
    var normalized = catalog.map(normalizeAgentDefinition).filter(function (agent) {
      return agent && agent[7];
    });
    if (!normalized.length) {
      return false;
    }
    sharedState.agents = normalized;
    initializeAgentSelections();
    return true;
  }

refreshChannelAvailability();

if (!sharedState.channelDefinitions.some(function (channel) { return channel.key === sharedState.state.activeChannel && !channel.unavailable; })) {
    sharedState.state.activeChannel = "website";
    window.localStorage.setItem("ua.preview.channel.active", sharedState.state.activeChannel);
  }

if (!sharedState.channelDefinitions.some(function (channel) { return channel.key === sharedState.state.defaultChannel && !channel.unavailable; })) {
    sharedState.state.defaultChannel = "website";
    window.localStorage.setItem("ua.preview.channel.default", sharedState.state.defaultChannel);
  }

sharedState.channelDefinitions.forEach(function (channel) {
    if (channel.unavailable) {
      sharedState.state.enabledChannels[channel.key] = false;
      window.localStorage.setItem("ua.preview.channel.enabled." + channel.key, "false");
    }
  });

appearance.applyBrandColor();

appearanceState.canvas = document.getElementById("mesh-canvas");

appearanceState.ctx = appearanceState.canvas.getContext("2d");

appearanceState.polygonMotion = window.UAPolygonMotion || {
    polygonTime: function (time) {
      return time * 2;
    },
  };

shellState.stepList = document.getElementById("step-list");

shellState.pageKicker = document.getElementById("page-kicker");

shellState.pageTitle = document.getElementById("page-title");

shellState.pageCopy = document.getElementById("page-copy");

shellState.pageBody = document.getElementById("page-body");

shellState.backButton = document.getElementById("back-button");

sharedState.nextButton = document.getElementById("next-button");

shellState.progressFill = document.getElementById("progress-fill");

shellState.progressLabel = document.getElementById("progress-label");

appearanceState.prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

appearanceState.meshPoints = [];

appearanceState.polyTriangles = [];

appearanceState.lightPaneCanvas = document.createElement("canvas");

appearanceState.lightPaneCtx = appearanceState.lightPaneCanvas.getContext("2d");

appearanceState.lightPaneCacheKey = "";

appearanceState.lastWidth = 0;

appearanceState.lastHeight = 0;

appearanceState.animationFrameId = 0;

shellState.activeHoverInfo = null;

finalizationState.standbyMessages = [
    "Final onboarding response delivered.",
    "Waiting for the container process to exit.",
    "Polling for the restarted site.",
    "Runtime surface found. Preparing reload.",
  ];

sharedState.state.pageIndex = Math.min(sharedState.state.pageIndex, sharedState.state.furthestPageIndex);

window.localStorage.setItem("ua.preview.page", String(sharedState.state.pageIndex));

window.localStorage.setItem("ua.preview.furthest_page", String(sharedState.state.furthestPageIndex));

function loadJsonSetting(key, fallback) {
    try {
      var value = JSON.parse(window.localStorage.getItem(key) || "null");
      return value === null ? fallback : value;
    } catch (error) {
      return fallback;
    }
  }

function detectTimezone() {
    try {
      var detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
      return detected || "America/Chicago";
    } catch (error) {
      return "America/Chicago";
    }
  }

function loadBootstrapSettings() {
    requestClient.fetch("/api/bootstrap", { cache: "no-store" }).then(function (response) {
      if (!response.ok) {
        throw new Error("Onboarding settings unavailable");
      }
      return response.json();
    }).then(function (settings) {
      var catalogChanged = applyInteractiveAppCatalog(settings.interactive_app_catalog);
      var sharingChanged = account_sharing.applyServices(settings.account_sharing_services);
      var liveEndpointChanged = applyLiveWebsiteEndpointSettings(settings);
      var changed = configuration.applyImportedSettings(settings, { overwriteExisting: false });
      if ((changed || liveEndpointChanged || catalogChanged || sharingChanged) && ["agents", "channels", "secrets", "review", "status"].indexOf(sharedState.pages[sharedState.state.pageIndex].id) !== -1) {
        renderCurrentPage();
      }
    }).catch(function () {
      return null;
    });
  }

function applyInteractiveAppCatalog(catalog) {
    if (!Array.isArray(catalog)) {
      return false;
    }
    var changed = false;
    catalog.forEach(function (entry) {
      if (!entry || !entry.plugin_id) {
        return;
      }
      var channel = sharedState.channelDefinitions.find(function (candidate) {
        return candidate.key === entry.channel_name || candidate.pluginId === entry.plugin_id;
      });
      if (!channel && entry.channel_name) {
        channel = {
          key: entry.channel_name,
          label: entry.display_name || entry.plugin_id,
          enabledByDefault: Boolean(entry.configured_enabled),
          staticUnavailable: entry.discovery_status !== "registered",
          unavailable: entry.discovery_status !== "registered",
          unavailableNote: entry.diagnostic || entry.installation_hint || "Adapter package unavailable",
          fields: [],
        };
        sharedState.channelDefinitions.push(channel);
        changed = true;
      }
      if (!channel) {
        return;
      }
      var nextLabel = entry.display_name || channel.label;
      changed = changed || channel.label !== nextLabel || channel.pluginId !== entry.plugin_id;
      channel.label = nextLabel;
      channel.pluginId = entry.plugin_id;
      channel.summary = entry.summary || "";
      channel.capabilities = Array.isArray(entry.capabilities) ? entry.capabilities.slice() : [];
      channel.configurationSchema = entry.configuration_schema || null;
      channel.documentationReference = entry.documentation_reference || "";
      channel.secretReferenceNames = Array.isArray(entry.secret_reference_names)
        ? entry.secret_reference_names.slice()
        : [];
      if (entry.discovery_status !== "registered") {
        channel.staticUnavailable = true;
        channel.unavailable = true;
        channel.unavailableNote = entry.diagnostic || entry.installation_hint || "Adapter package unavailable";
      }
    });
    return changed;
  }

function applyLiveWebsiteEndpointSettings(settings) {
    if (!settings || typeof settings !== "object") {
      return false;
    }
    var changed = false;
    if (settings.network_public_origin) {
      var publicOrigin = String(settings.network_public_origin);
      changed = changed || sharedState.state.networkPublicOrigin !== publicOrigin;
      sharedState.state.networkPublicOrigin = publicOrigin;
      window.localStorage.setItem("ua.preview.network.public_origin", sharedState.state.networkPublicOrigin);
    }
    if (settings.internal_website_bind_port !== undefined && settings.internal_website_bind_port !== null) {
      var bindPort = appearance.clamp(parseInt(settings.internal_website_bind_port, 10), 1, 65535);
      changed = changed || sharedState.state.internalWebsiteBindPort !== bindPort;
      sharedState.state.internalWebsiteBindPort = bindPort;
      window.localStorage.setItem("ua.preview.internal_website.bind_port", String(bindPort));
    }
    var hasPublicPort = Object.prototype.hasOwnProperty.call(settings, "internal_website_public_port");
    var rawPublicPort = hasPublicPort ? settings.internal_website_public_port : settings.documentation_port;
    if (rawPublicPort === null || rawPublicPort === "") {
      changed = changed || sharedState.state.internalWebsitePublicPort !== "";
      sharedState.state.internalWebsitePublicPort = "";
      window.localStorage.removeItem("ua.preview.documentation.port");
    } else if (rawPublicPort !== undefined) {
      var publicPort = appearance.clamp(parseInt(rawPublicPort, 10), 1, 65535);
      changed = changed || sharedState.state.internalWebsitePublicPort !== publicPort;
      sharedState.state.internalWebsitePublicPort = publicPort;
      window.localStorage.setItem("ua.preview.documentation.port", String(publicPort));
    }
    return changed;
  }

function setNetworkPublicOrigin(value) {
    sharedState.state.networkPublicOrigin = value;
    if (value) {
      window.localStorage.setItem("ua.preview.network.public_origin", value);
    } else {
      window.localStorage.removeItem("ua.preview.network.public_origin");
    }
  }

function originWithPort(origin, port) {
    var value = String(origin || "").trim().replace(/\/$/, "");
    if (value && !/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) {
      value = "http://" + value;
    }
    try {
      var parsed = new URL(value);
      if (port !== null && port !== undefined && port !== "") {
        parsed.port = String(port);
      }
      parsed.pathname = "";
      parsed.search = "";
      parsed.hash = "";
      return parsed.toString().replace(/\/$/, "");
    } catch (error) {
      return value;
    }
  }

function documentationUrl() {
    return originWithPort(
      sharedState.state.networkPublicOrigin || window.location.origin,
      sharedState.state.internalWebsitePublicPort
    ) + "/docs/";
  }

function webChatUrl() {
    return originWithPort(
      sharedState.state.networkPublicOrigin || window.location.origin,
      sharedState.state.internalWebsitePublicPort
    ) + "/webchat";
  }

function loadLightColors() {
    var colors = {};
    sharedState.lightDefinitions.forEach(function (light) {
      colors[light.key] = appearance.normalizeHexColor(
        window.localStorage.getItem(appearance.lightStorageKey(light.key)),
        light.defaultColor
      );
    });
    return colors;
  }

function loadEnabledChannels() {
    var enabled = {};
    sharedState.channelDefinitions.forEach(function (channel) {
      var stored = window.localStorage.getItem("ua.preview.channel.enabled." + channel.key);
      enabled[channel.key] = channel.unavailable ? false : (stored === null ? channel.enabledByDefault : stored === "true");
    });
    return enabled;
  }

function loadSafetyValues() {
    var values = {};
    sharedState.safetyChoices.forEach(function (choice) {
      var stored = window.localStorage.getItem("ua.preview.safety." + choice.key);
      values[choice.key] = stored === null ? Boolean(choice.enabledByDefault) : stored === "true";
    });
    return values;
  }

function setSafetyValue(key, enabled) {
    sharedState.state.safetyValues[key] = enabled;
    window.localStorage.setItem("ua.preview.safety." + key, enabled ? "true" : "false");
  }

function loadChannelFieldValues() {
    var values = {};
    sharedState.channelDefinitions.forEach(function (channel) {
      channel.fields.forEach(function (field) {
        var key = channelFieldKey(channel.key, field[0]);
        if (isSensitiveChannelField(channel.key, field[0])) {
          values[key] = "";
          return;
        }
        var stored = window.localStorage.getItem("ua.onboarding.channel." + key);
        if (field[1] === "toggle") {
          values[key] = stored === null ? (field[2] ? "true" : "false") : (stored === "true" ? "true" : "false");
          return;
        }
        values[key] = stored || "";
      });
    });
    return values;
  }

function setChannelFieldValue(channelKey, label, value) {
    var key = channelFieldKey(channelKey, label);
    sharedState.state.channelFieldValues[key] = value;
    clearCommunicationFieldError(channelKey, label);
    if (isSensitiveChannelField(channelKey, label)) {
      window.localStorage.removeItem("ua.onboarding.channel." + key);
      return;
    }
    if (value) {
      window.localStorage.setItem("ua.onboarding.channel." + key, value);
    } else {
      window.localStorage.removeItem("ua.onboarding.channel." + key);
    }
  }

function getChannelFieldValue(channelKey, label) {
    return String(sharedState.state.channelFieldValues[channelFieldKey(channelKey, label)] || "").trim();
  }

function getChannelToggleValue(channelKey, label, defaultValue) {
    var value = sharedState.state.channelFieldValues[channelFieldKey(channelKey, label)];
    if (value === undefined || value === null || value === "") {
      return Boolean(defaultValue);
    }
    return value === true || String(value).toLowerCase() === "true";
  }

function channelSelectDisplayValue(label, value) {
    var normalized = String(value || "").trim().toLowerCase();
    if (label === "Group policy") {
      if (normalized === "mention") {
        return "Mention only";
      }
      if (normalized === "open") {
        return "Open channel";
      }
    }
    if (label === "Offline policy") {
      if (normalized === "pause_telegram_polling") {
        return "Pause polling";
      }
      if (normalized === "notify") {
        return "Notify while offline";
      }
    }
    return value;
  }

function channelSelectPayloadValue(label, value) {
    var normalized = String(value || "").trim().toLowerCase();
    if (label === "Group policy") {
      if (!normalized || normalized === "mention only") {
        return "mention";
      }
      if (normalized === "open channel") {
        return "open";
      }
    }
    if (label === "Offline policy") {
      if (!normalized || normalized === "pause polling") {
        return "pause_telegram_polling";
      }
      if (normalized === "notify while offline") {
        return "notify";
      }
    }
    return String(value || "").trim();
  }

function commaSeparatedFieldValues(channelKey, label) {
    return getChannelFieldValue(channelKey, label)
      .split(",")
      .map(function (value) {
        return value.trim();
      })
      .filter(Boolean);
  }

function communicationFieldKey(channelKey, label) {
    return channelKey + "::" + label;
  }

function clearCommunicationFieldError(channelKey, label) {
    if (!sharedState.state.communicationInvalidFields) {
      return;
    }
    delete sharedState.state.communicationInvalidFields[communicationFieldKey(channelKey, label)];
  }

function markCommunicationFieldError(channelKey, label, message) {
    sharedState.state.communicationInvalidFields[communicationFieldKey(channelKey, label)] = message;
  }

function communicationValidationText() {
    return sharedState.state.communicationValidationMessages.join("\n");
  }

function validateCommunicationSettings() {
    var messages = [];
    sharedState.state.communicationInvalidFields = {};

    if (sharedState.state.enabledChannels.signal && !getChannelFieldValue("signal", "Account")) {
      messages.push("Signal is enabled. Enter the registered Signal account or number, or turn Signal off.");
      markCommunicationFieldError("signal", "Account", "Required when Signal is enabled.");
    }

    if (
      sharedState.state.enabledChannels.signal &&
      commaSeparatedFieldValues("signal", "Allowed group IDs").length &&
      channelSelectPayloadValue("Group policy", getChannelFieldValue("signal", "Group policy")) === "mention" &&
      !commaSeparatedFieldValues("signal", "Mention markers").length
    ) {
      messages.push("Signal groups in mention mode need at least one mention marker.");
      markCommunicationFieldError("signal", "Mention markers", "Required when Signal groups use mention mode.");
    }

    if (
      sharedState.state.enabledChannels.photon &&
      commaSeparatedFieldValues("photon", "Allowed group IDs").length &&
      channelSelectPayloadValue("Group policy", getChannelFieldValue("photon", "Group policy")) === "mention" &&
      !commaSeparatedFieldValues("photon", "Mention markers").length
    ) {
      messages.push("iMessage groups in mention mode need at least one mention marker.");
      markCommunicationFieldError("photon", "Mention markers", "Required when iMessage groups use mention mode.");
    }

    sharedState.state.communicationValidationMessages = messages;
    if (messages.length) {
      var firstKey = Object.keys(sharedState.state.communicationInvalidFields)[0] || "";
      var channelKey = firstKey.split("::")[0];
      if (channelKey) {
        sharedState.state.activeChannel = channelKey;
        window.localStorage.setItem("ua.preview.channel.active", channelKey);
      }
      return false;
    }
    return true;
  }

function loadSecretValues() {
    var values = {};
    shellState.secretDefinitions.forEach(function (definition) {
      values[definition.key] = "";
    });
    return values;
  }

function loadAdvancedEnvValues() {
    var values = {};
    shellState.advancedEnvDefinitions.forEach(function (definition) {
      values[definition.key] = "";
    });
    return values;
  }

function loadTeamSkillRepos() {
    var stored = loadJsonSetting("ua.preview.team.skills.repos", [shellState.DEFAULT_TEAM_SKILLS_REPO]);
    if (!Array.isArray(stored)) {
      return [shellState.DEFAULT_TEAM_SKILLS_REPO];
    }
    var repos = stored.map(function (value) {
      return String(value || "").trim();
    });
    return repos.length ? repos : [shellState.DEFAULT_TEAM_SKILLS_REPO];
  }

function persistTeamSkillRepos() {
    if (!sharedState.state.teamSkillRepos.length) {
      sharedState.state.teamSkillRepos = [""];
    }
    window.localStorage.setItem("ua.preview.team.skills.repos", JSON.stringify(sharedState.state.teamSkillRepos));
  }

function teamSkillRepoUrls() {
    var seen = {};
    return sharedState.state.teamSkillRepos.map(function (value) {
      return String(value || "").trim();
    }).filter(function (value) {
      if (!value || seen[value]) {
        return false;
      }
      seen[value] = true;
      return true;
    });
  }

function setSecretValue(secretKey, value) {
    sharedState.state.secretValues[secretKey] = value;
    window.localStorage.removeItem("ua.onboarding.secret." + secretKey);
  }

function setAdvancedEnvValue(envKey, value) {
    sharedState.state.advancedEnvValues[envKey] = value;
    window.localStorage.removeItem("ua.onboarding.env." + envKey);
  }

function parseEnvFileContent(content) {
    var values = {};
    String(content || "").split(/\r?\n/).forEach(function (rawLine, index) {
      var line = index === 0 ? rawLine.replace(/^\uFEFF/, "") : rawLine;
      var trimmed = line.trim();
      if (!trimmed || trimmed.charAt(0) === "#") {
        return;
      }
      if (trimmed.indexOf("export ") === 0) {
        trimmed = trimmed.slice(7).trim();
      }
      var equalsIndex = trimmed.indexOf("=");
      if (equalsIndex <= 0) {
        return;
      }
      var key = trimmed.slice(0, equalsIndex).trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
        return;
      }
      values[key] = parseEnvValue(trimmed.slice(equalsIndex + 1));
    });
    return values;
  }

function parseEnvValue(rawValue) {
    var value = String(rawValue || "").trim();
    if (!value) {
      return "";
    }
    if (value.charAt(0) === '"') {
      var doubleQuoteEnd = closingEnvQuoteIndex(value, '"');
      if (doubleQuoteEnd > 0) {
        return value.slice(1, doubleQuoteEnd).replace(/\\([nrt"\\])/g, function (_match, escaped) {
          if (escaped === "n") {
            return "\n";
          }
          if (escaped === "r") {
            return "\r";
          }
          if (escaped === "t") {
            return "\t";
          }
          return escaped;
        });
      }
    }
    if (value.charAt(0) === "'") {
      var singleQuoteEnd = closingEnvQuoteIndex(value, "'");
      if (singleQuoteEnd > 0) {
        return value.slice(1, singleQuoteEnd);
      }
    }
    return value.replace(/\s+#.*$/, "").trim();
  }

function closingEnvQuoteIndex(value, quote) {
    for (var index = 1; index < value.length; index += 1) {
      if (value.charAt(index) !== quote) {
        continue;
      }
      var backslashCount = 0;
      var previousIndex = index - 1;
      while (previousIndex >= 0 && value.charAt(previousIndex) === "\\") {
        backslashCount += 1;
        previousIndex -= 1;
      }
      if (backslashCount % 2 === 0) {
        return index;
      }
    }
    return -1;
  }

function applyEnvValuesToState(envValues, options) {
    var overwriteExisting = Boolean(options && options.overwriteExisting);
    var runtimeConnectionBeforeImport = model_setup.runtimeConnectionSignature();
    var changed = 0;

    function shouldApply(storageKey) {
      return overwriteExisting || !window.localStorage.getItem(storageKey);
    }

    function shouldApplyCurrent(currentValue) {
      return overwriteExisting || !String(currentValue || "").trim();
    }

    function getEnv(envName) {
      var value = envValues[envName];
      return value === undefined || value === null ? "" : String(value);
    }

    function applyStateString(envName, stateKey, storageKey, setter) {
      var value = getEnv(envName);
      if (!value || !shouldApply(storageKey)) {
        return;
      }
      if (sharedState.state[stateKey] !== value) {
        setter(value);
        changed += 1;
      }
    }

    var modelToken = getEnv(model_setup.providerEnvVar(sharedState.state.runtimeProvider));
    if (modelToken && shouldApplyCurrent(sharedState.state.runtimeToken) && sharedState.state.runtimeToken !== modelToken) {
      model_setup.setRuntimeTokenValue(modelToken);
      changed += 1;
    }

    applyStateString("NETWORK_PUBLIC_ORIGIN", "networkPublicOrigin", "ua.preview.network.public_origin", setNetworkPublicOrigin);
    var webChatToken = getEnv("WEB_CHAT_AUTH_TOKEN");
    if (webChatToken && shouldApplyCurrent(sharedState.state.websiteAuthToken) && sharedState.state.websiteAuthToken !== webChatToken) {
      model_setup.setWebsiteAuthTokenValue(webChatToken);
      changed += 1;
    }
    var embeddingApiKey = getEnv("EMBEDDING_API_KEY");
    if (embeddingApiKey && shouldApplyCurrent(sharedState.state.embeddingApiKey) && sharedState.state.embeddingApiKey !== embeddingApiKey) {
      model_setup.setEmbeddingApiKeyValue(embeddingApiKey);
      changed += 1;
    }

    [
      ["EXA_API_KEY", "exa_api_key"],
      ["BRAVE_SEARCH_API_KEY", "brave_search_api_key"],
      ["XAI_API_KEY", "xai_api_key"],
    ].forEach(function (entry) {
      var value = getEnv(entry[0]);
      if (value && shouldApplyCurrent(sharedState.state.secretValues[entry[1]]) && sharedState.state.secretValues[entry[1]] !== value) {
        setSecretValue(entry[1], value);
        changed += 1;
      }
    });

    [
      ["TELEGRAM_BOT_TOKEN", "telegram", "Bot token"],
      ["DISCORD_BOT_TOKEN", "discord", "Bot token"],
    ].forEach(function (entry) {
      var value = getEnv(entry[0]);
      if (value && shouldApplyCurrent(getChannelFieldValue(entry[1], entry[2])) && getChannelFieldValue(entry[1], entry[2]) !== value) {
        setChannelFieldValue(entry[1], entry[2], value);
        changed += 1;
      }
    });

    shellState.advancedEnvDefinitions.forEach(function (definition) {
      var value = getEnv(definition.env);
      if (value && shouldApplyCurrent(sharedState.state.advancedEnvValues[definition.key]) && sharedState.state.advancedEnvValues[definition.key] !== value) {
        setAdvancedEnvValue(definition.key, value);
        changed += 1;
      }
    });

    if (changed && model_setup.runtimeConnectionSignature() !== runtimeConnectionBeforeImport) {
      model_setup.invalidateRuntimeTest();
    }
    return changed;
  }

function applySourceEnvContentToState(options) {
    if (!sharedState.state.sourceEnvContent.trim()) {
      return 0;
    }
    return applyEnvValuesToState(parseEnvFileContent(sharedState.state.sourceEnvContent), options);
  }

function setChannelEnabled(channelKey, enabled) {
    var channel = sharedState.channelDefinitions.find(function (candidate) {
      return candidate.key === channelKey;
    });
    if (channel && channel.unavailable) {
      enabled = false;
    }
    sharedState.state.enabledChannels[channelKey] = enabled;
    window.localStorage.setItem("ua.preview.channel.enabled." + channelKey, enabled ? "true" : "false");
    if (enabled) {
      sharedState.state.activeChannel = channelKey;
      window.localStorage.setItem("ua.preview.channel.active", channelKey);
      if (!sharedState.state.defaultChannel) {
        setDefaultChannel(channelKey);
      }
    } else if (sharedState.state.activeChannel === channelKey) {
      sharedState.state.activeChannel = "";
      window.localStorage.removeItem("ua.preview.channel.active");
    }
    if (!enabled && sharedState.state.defaultChannel === channelKey) {
      var nextDefault = sharedState.channelDefinitions.find(function (channel) {
        return channel.key !== channelKey && !channel.unavailable && sharedState.state.enabledChannels[channel.key];
      });
      setDefaultChannel(nextDefault ? nextDefault.key : "");
    }
  }

function setActiveChannel(channelKey) {
    var channel = sharedState.channelDefinitions.find(function (candidate) {
      return candidate.key === channelKey;
    });
    if (channel && channel.unavailable) {
      return;
    }
    sharedState.state.activeChannel = sharedState.state.activeChannel === channelKey ? "" : channelKey;
    if (sharedState.state.activeChannel) {
      window.localStorage.setItem("ua.preview.channel.active", sharedState.state.activeChannel);
    } else {
      window.localStorage.removeItem("ua.preview.channel.active");
    }
  }

function setDefaultChannel(channelKey) {
    var channel = sharedState.channelDefinitions.find(function (candidate) {
      return candidate.key === channelKey;
    });
    if (channel && channel.unavailable) {
      channelKey = "";
    }
    sharedState.state.defaultChannel = channelKey;
    if (channelKey) {
      window.localStorage.setItem("ua.preview.channel.default", channelKey);
    } else {
      window.localStorage.removeItem("ua.preview.channel.default");
    }
  }

function setBeginMode(mode) {
    sharedState.state.beginMode = mode === "existing" || mode === "teamImport" ? mode : "new";
    window.localStorage.setItem("ua.preview.begin.mode", sharedState.state.beginMode);
  }

function slugFromAssistantName() {
    var slug = String(sharedState.state.assistantName || "assistant")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "");
    return slug || "assistant";
  }

function composedInstanceName() {
    return "instance_" + slugFromAssistantName();
  }

function composedTeamId() {
    return "team_" + slugFromAssistantName();
  }

function validateTeamSkillReposBeforeNext() {
    var repositories = teamSkillRepoUrls();
    if (!repositories.length) {
      sharedState.state.teamSkillsValidationStatus = "";
      sharedState.state.teamSkillsValidationKind = "pending";
      return Promise.resolve(true);
    }
    sharedState.state.teamSkillsValidationInFlight = true;
    sharedState.state.teamSkillsValidationStatus = "Checking team skills repositories...";
    sharedState.state.teamSkillsValidationKind = "pending";
    window.localStorage.setItem("ua.preview.team.advanced.open", "true");
    renderCurrentPage();

    return requestClient.fetch("/api/onboarding/skills-repos/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repositories: repositories }),
    }).then(function (response) {
      return response.json().then(function (payload) {
        return { ok: response.ok, payload: payload };
      }).catch(function () {
        return { ok: response.ok, payload: {} };
      });
    }).then(function (result) {
      var results = Array.isArray(result.payload.repositories) ? result.payload.repositories : [];
      var warning = model_setup.formatTeamSkillsValidationWarning(results);
      sharedState.state.teamSkillsValidationInFlight = false;
      if (!result.ok || warning) {
        sharedState.state.teamSkillsValidationStatus = warning || result.payload.message || "One or more skills repositories could not be reached.";
        sharedState.state.teamSkillsValidationKind = "warning";
        renderCurrentPage();
        return false;
      }
      sharedState.state.teamSkillsValidationStatus = "All team skills repositories are reachable.";
      sharedState.state.teamSkillsValidationKind = "success";
      renderCurrentPage();
      return true;
    }).catch(function () {
      sharedState.state.teamSkillsValidationInFlight = false;
      sharedState.state.teamSkillsValidationStatus = "Could not validate team skills repositories in this preview. Validation will run in live onboarding.";
      sharedState.state.teamSkillsValidationKind = "warning";
      renderCurrentPage();
      return window.location.protocol === "file:";
    });
  }

function setQuietHoursEnabled(enabled) {
    sharedState.state.quietHoursEnabled = enabled;
    window.localStorage.setItem("ua.preview.quiet.enabled", enabled ? "true" : "false");
  }

function setQuietHoursStart(value) {
    sharedState.state.quietHoursStart = appearance.clamp(parseInt(value, 10), 0, 23);
    window.localStorage.setItem("ua.preview.quiet.start", String(sharedState.state.quietHoursStart));
  }

function setQuietHoursEnd(value) {
    sharedState.state.quietHoursEnd = appearance.clamp(parseInt(value, 10), 0, 23);
    window.localStorage.setItem("ua.preview.quiet.end", String(sharedState.state.quietHoursEnd));
  }

function setQuietHoursUrgent(enabled) {
    sharedState.state.quietHoursUrgent = enabled;
    window.localStorage.setItem("ua.preview.quiet.urgent", enabled ? "true" : "false");
  }

function setVoiceEnabled(enabled) {
    sharedState.state.voiceEnabled = enabled;
    window.localStorage.setItem("ua.preview.voice.enabled", enabled ? "true" : "false");
  }

function setVoiceResponseMode(mode) {
    var normalized = ["voice_only", "voice_and_text", "text_only"].indexOf(mode) >= 0 ? mode : "voice_and_text";
    sharedState.state.voiceResponseMode = normalized;
    window.localStorage.setItem("ua.preview.voice.response_mode", normalized);
  }

function renderSteps() {
    shellState.stepList.innerHTML = "";
    sharedState.pages.forEach(function (page, index) {
      var isReached = index <= sharedState.state.furthestPageIndex;
      var isComplete = index < sharedState.state.furthestPageIndex && index !== sharedState.state.pageIndex;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "step-button" + (isComplete ? " is-complete" : "") + (!isReached ? " is-locked" : "");
      button.disabled = !isReached;
      button.setAttribute("aria-disabled", isReached ? "false" : "true");
      button.setAttribute("aria-current", index === sharedState.state.pageIndex ? "page" : "false");
      lifecycle.listen(button, "click", function () {
        if (!isReached) {
          return;
        }
        setPage(index);
      });
      var indexEl = document.createElement("span");
      indexEl.className = "step-index";
      indexEl.textContent = isComplete ? "OK" : String(index + 1);
      var label = document.createElement("span");
      label.className = "step-label";
      label.textContent = page.label;
      button.appendChild(indexEl);
      button.appendChild(label);
      shellState.stepList.appendChild(button);
    });
  }

function renderWelcome() {
    var stack = document.createElement("div");
    stack.className = "welcome-stack";
    stack.appendChild(appearance.renderThemeModeControl());
    var customize = document.createElement("button");
    customize.className = "button primary onboarding-customize-theme";
    customize.type = "button";
    customize.textContent = "Customize Theme";
    lifecycle.listen(customize, "click", function () {
      if (!window.UAAppearanceCustomizer) {
        return;
      }
      window.UAAppearanceCustomizer.open({
        appearance: appearance.currentAppearanceSettings(),
        interfaceElement: document.querySelector(".preview-shell"),
        returnFocus: customize,
        preview: appearance.previewCustomizerAppearance,
        save: appearance.saveCustomizerAppearance,
        discard: appearance.previewCustomizerAppearance,
        onClose: function () {
          renderCurrentPage();
        },
      });
    });
    stack.appendChild(customize);
    return stack;
  }

function renderStartOption(mode, labelText, disabled) {
    var label = document.createElement("label");
    label.className =
      "start-option" +
      (sharedState.state.beginMode === mode ? " is-selected" : "") +
      (disabled ? " is-disabled" : "");
    var input = document.createElement("input");
    input.type = "radio";
    input.name = "begin-mode";
    input.value = mode;
    input.checked = sharedState.state.beginMode === mode;
    input.disabled = Boolean(disabled);
    lifecycle.listen(input, "change", function () {
      if (disabled) {
        return;
      }
      setBeginMode(mode);
      if (mode === "new") {
        loadDefaultConfigSettings();
        return;
      }
      if (mode === "teamImport") {
        team_import.loadExports();
        return;
      }
      renderCurrentPage();
    });
    var text = document.createElement("strong");
    text.textContent = labelText;
    label.appendChild(input);
    label.appendChild(text);
    return label;
  }

function renderFilePicker(labelText, accept, onFileSelected) {
    var field = document.createElement("div");
    field.className = "shim-card field file-picker-field";
    var label = document.createElement("label");
    var id = "begin-file-" + labelText.toLowerCase().replace(/[^a-z0-9]+/g, "-");
    label.htmlFor = id;
    label.textContent = labelText;
    var picker = document.createElement("div");
    picker.className = "file-picker";
    var display = document.createElement("input");
    display.className = "input file-picker-display";
    display.type = "text";
    display.placeholder = "Choose file";
    display.readOnly = true;
    var browseButton = document.createElement("button");
    browseButton.className = "file-picker-button";
    browseButton.type = "button";
    browseButton.textContent = "Browse";
    var fileInput = document.createElement("input");
    fileInput.id = id;
    fileInput.type = "file";
    fileInput.accept = accept;
    fileInput.className = "file-picker-native";
    lifecycle.listen(browseButton, "click", function () {
      fileInput.click();
    });
    lifecycle.listen(fileInput, "change", function () {
      var file = fileInput.files && fileInput.files[0] ? fileInput.files[0] : null;
      display.value = file ? file.name : "";
      if (file && onFileSelected) {
        onFileSelected(file);
      }
    });
    picker.appendChild(display);
    picker.appendChild(browseButton);
    picker.appendChild(fileInput);
    field.appendChild(label);
    field.appendChild(picker);
    return field;
  }

function loadDefaultConfigSettings() {
    sharedState.state.beginImportMessage = "Loading example configuration defaults...";
    renderCurrentPage();
    requestClient.fetch("/api/bootstrap", { cache: "no-store" }).then(function (response) {
      if (!response.ok) {
        throw new Error("Could not load example configuration defaults.");
      }
      return response.json();
    }).then(function (settings) {
      configuration.applyImportedSettings(settings, { overwriteExisting: true });
      sharedState.state.beginImportMessage = "Loaded example configuration defaults.";
      renderCurrentPage();
    }).catch(function (error) {
      sharedState.state.beginImportMessage = error.message || "Could not load example configuration defaults.";
      renderCurrentPage();
    });
  }

function importConfigFile(file) {
    sharedState.state.beginImportMessage = "Reading " + file.name + "...";
    renderCurrentPage();
    var reader = new FileReader();
    lifecycle.listen(reader, "load", function () {
      requestClient.fetch("/api/onboarding/import-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, content: String(reader.result || "") }),
      }).then(function (response) {
        if (!response.ok) {
          return response.text().then(function (text) {
            throw new Error(finalization.responseErrorMessage(text, "Could not import " + file.name + "."));
          });
        }
        return response.json();
      }).then(function (payload) {
        configuration.applyImportedSettings(payload.settings || {}, { overwriteExisting: true });
        applySourceEnvContentToState({ overwriteExisting: true });
        var skipped = Array.isArray(payload.skipped) ? payload.skipped : [];
        sharedState.state.beginImportSkipped = skipped;
        sharedState.state.beginImportMessage = (payload.kind === "instance_config"
          ? "Imported instance_config.yaml values."
          : "Imported config.yaml values.")
          + (skipped.length ? " Some settings were not imported; see the list below." : "");
        renderCurrentPage();
      }).catch(function (error) {
        sharedState.state.beginImportSkipped = [];
        sharedState.state.beginImportMessage = error.message || "Could not import configuration.";
        renderCurrentPage();
      });
    });
    lifecycle.listen(reader, "error", function () {
      sharedState.state.beginImportMessage = "Could not read " + file.name + ".";
      renderCurrentPage();
    });
    reader.readAsText(file);
  }

function importEnvFile(file) {
    sharedState.state.beginImportMessage = "Reading " + file.name + "...";
    renderCurrentPage();
    var reader = new FileReader();
    lifecycle.listen(reader, "load", function () {
      sharedState.state.sourceEnvFilename = file.name;
      sharedState.state.sourceEnvContent = String(reader.result || "");
      var populatedCount = applySourceEnvContentToState({ overwriteExisting: true });
      sharedState.state.beginImportMessage = "Loaded " + file.name + "."
        + (populatedCount ? " Populated " + populatedCount + " onboarding field" + (populatedCount === 1 ? "" : "s") + "." : "")
        + (sharedState.state.secretsFromEnvironment
          ? " Secrets come from the environment, so this file is not saved. It only filled in the form."
          : " Existing credentials in this file will be preserved when internal credentials are generated.");
      sharedState.state.beginImportSkipped = [];
      renderCurrentPage();
    });
    lifecycle.listen(reader, "error", function () {
      sharedState.state.beginImportMessage = "Could not read " + file.name + ".";
      renderCurrentPage();
    });
    reader.readAsText(file);
  }

function randomToken() {
    return sharedState.credentialControls.secureRandomHexToken("ua_", 24);
  }

function renderRevealableControl(input) {
    var credentialControl = document.createElement("div");
    credentialControl.className = "credential-input";
    var revealButton = document.createElement("button");
    revealButton.className = "credential-reveal-button";
    revealButton.type = "button";
    revealButton.innerHTML = iconMarkup("eye");
    revealButton.title = "Show or hide value";
    revealButton.setAttribute("aria-label", "Show or hide value");
    revealButton.setAttribute("aria-controls", input.id);
    lifecycle.listen(revealButton, "click", function () {
      sharedState.credentialControls.toggleReveal(input, revealButton);
    });
    credentialControl.appendChild(input);
    credentialControl.appendChild(revealButton);
    return credentialControl;
  }

function copyTextToClipboard(text) {
    return sharedState.credentialControls.copyTextToClipboard(text);
  }

function appendLinkedLabel(label, labelText, linkText, href) {
    label.appendChild(document.createTextNode(labelText + " ("));
    var link = document.createElement("a");
    link.className = "copy-link";
    link.href = href;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = linkText;
    label.appendChild(link);
    label.appendChild(document.createTextNode(")"));
  }

function iconMarkup(name) {
    if (name === "copy") {
      return '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="10" height="10" rx="2"></rect><path d="M5 15V7a2 2 0 0 1 2-2h8"></path></svg>';
    }
    if (name === "send") {
      return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 14-7-7 14-2-5-5-2Z"></path><path d="m12 14 7-9"></path></svg>';
    }
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"></path><circle cx="12" cy="12" r="3"></circle></svg>';
  }

function renderGeneratedTokenField(channel, field) {
    var isWebsiteAuthToken = channel.key === "website" && field[0] === "Authentication token";
    var card = document.createElement("div");
    card.className = "field generated-token-field";
    var id = "channel-" + channel.key + "-" + field[0].toLowerCase().replace(/[^a-z0-9]+/g, "-");
    var label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = field[0];
    var tokenWrap = document.createElement("div");
    tokenWrap.className = "generated-token-control";
    var input = document.createElement("input");
    input.id = id;
    input.type = "password";
    input.className = "input";
    input.placeholder = field[2];
    input.value = isWebsiteAuthToken ? sharedState.state.websiteAuthToken : "";
    var inputWrap = document.createElement("div");
    inputWrap.className = "generated-token-input-wrap";
    var revealButton = document.createElement("button");
    revealButton.type = "button";
    revealButton.className = "token-icon-button";
    revealButton.innerHTML = iconMarkup("eye");
    revealButton.title = "Show or hide token";
    revealButton.setAttribute("aria-label", "Show or hide token");
    var generateButton = document.createElement("button");
    generateButton.type = "button";
    generateButton.className = "token-mini-button";
    generateButton.textContent = "Generate";
    var copyButton = document.createElement("button");
    copyButton.type = "button";
    copyButton.className = "token-icon-button";
    copyButton.innerHTML = iconMarkup("copy");
    copyButton.title = "Copy token";
    copyButton.setAttribute("aria-label", "Copy token");
    copyButton.disabled = true;
    var message = document.createElement("p");
    message.className = "generated-token-message";
    message.textContent = "New token generated. Make sure to copy it and save it for later.";
    message.hidden = true;

    function syncCopyState() {
      copyButton.disabled = !input.value;
    }

    function persistTokenValue() {
      if (!isWebsiteAuthToken) {
        return;
      }
      model_setup.setWebsiteAuthTokenValue(input.value);
    }

    lifecycle.listen(input, "input", function () {
      persistTokenValue();
      message.hidden = true;
      syncCopyState();
    });
    lifecycle.listen(revealButton, "click", function () {
      sharedState.credentialControls.toggleReveal(input, revealButton);
    });
    lifecycle.listen(generateButton, "click", function () {
      input.value = randomToken();
      persistTokenValue();
      message.hidden = false;
      syncCopyState();
    });
    lifecycle.listen(copyButton, "click", function () {
      if (!input.value) {
        return;
      }
      copyTextToClipboard(input.value).then(function () {
        sharedState.credentialControls.flashCopied(copyButton);
      }).catch(function () {
        copyButton.classList.remove("is-copied");
      });
    });

    inputWrap.appendChild(input);
    inputWrap.appendChild(revealButton);
    inputWrap.appendChild(copyButton);
    tokenWrap.appendChild(inputWrap);
    tokenWrap.appendChild(generateButton);
    card.appendChild(label);
    card.appendChild(tokenWrap);
    card.appendChild(message);
    syncCopyState();
    return card;
  }

function renderBegin() {
    var stack = document.createElement("div");
    stack.className = "begin-stack";
    var options = document.createElement("div");
    options.className = "begin-options";
    options.appendChild(renderStartOption("new", "I am a new user"));
    options.appendChild(renderStartOption("existing", "I'm migrating from another UA instance"));
    options.appendChild(renderStartOption("teamImport", "Import a team export (upgrade or move a team)"));
    options.appendChild(renderStartOption("openclaw", "I'm migrating from OpenClaw (not yet available)", true));
    options.appendChild(renderStartOption("hermes", "I'm migrating from Hermes Agent (not yet available)", true));
    stack.appendChild(options);
    if (sharedState.state.beginMode === "teamImport") {
      // The import page asks where secrets come from, starting from the export's choice.
      stack.appendChild(team_import.renderPanel());
      return stack;
    }
    stack.appendChild(renderSecretsSourceControl());

    var files = document.createElement("div");
    files.className = "begin-files" + (sharedState.state.beginMode === "existing" ? " is-open" : "");
    var filesInner = document.createElement("div");
    filesInner.className = "begin-files-inner";
    filesInner.appendChild(renderFilePicker("Existing config.yaml or instance_config.yaml", ".yaml,.yml", importConfigFile));
    filesInner.appendChild(renderFilePicker("Existing .env", ".env,text/plain", importEnvFile));
    files.appendChild(filesInner);
    stack.appendChild(files);
    if (sharedState.state.beginImportMessage) {
      var message = document.createElement("p");
      message.className = "begin-import-message";
      message.textContent = sharedState.state.beginImportMessage;
      stack.appendChild(message);
    }
    if (sharedState.state.beginImportSkipped.length) {
      stack.appendChild(renderImportSkipped(sharedState.state.beginImportSkipped));
    }
    return stack;
  }

function renderSecretsSourceControl() {
    var card = document.createElement("div");
    card.className = "shim-card choice-card secrets-source-card";
    var title = document.createElement("strong");
    title.id = "secrets-source-title";
    title.textContent = sharedState.SECRETS_FROM_ENVIRONMENT_LABEL;
    var toggle = document.createElement("label");
    toggle.className = "toggle";
    var input = document.createElement("input");
    input.id = "secrets-from-environment";
    input.type = "checkbox";
    input.checked = sharedState.state.secretsFromEnvironment;
    input.setAttribute("aria-labelledby", "secrets-source-title");
    input.setAttribute("aria-describedby", "secrets-source-help");
    lifecycle.listen(input, "change", function () {
      setSecretsFromEnvironment(input.checked);
      renderCurrentPage();
    });
    var knob = document.createElement("span");
    knob.setAttribute("aria-hidden", "true");
    toggle.appendChild(input);
    toggle.appendChild(knob);
    var help = document.createElement("p");
    help.id = "secrets-source-help";
    help.className = "choice-note";
    help.textContent = sharedState.SECRETS_FROM_ENVIRONMENT_HELP;
    card.appendChild(title);
    card.appendChild(toggle);
    card.appendChild(help);
    return card;
  }

function setSecretsFromEnvironment(enabled) {
    sharedState.state.secretsFromEnvironment = Boolean(enabled);
    window.localStorage.setItem(sharedState.SECRETS_FROM_ENVIRONMENT_STORAGE_KEY, enabled ? "true" : "false");
  }

function renderImportSkipped(skipped) {
    var box = document.createElement("div");
    box.className = "begin-import-skipped";
    var heading = document.createElement("p");
    heading.textContent = "Not imported:";
    var list = document.createElement("ul");
    skipped.forEach(function (item) {
      var entry = document.createElement("li");
      var path = document.createElement("code");
      path.textContent = String(item.path || "");
      var reason = document.createElement("span");
      reason.textContent = " " + String(item.reason || "");
      entry.appendChild(path);
      entry.appendChild(reason);
      list.appendChild(entry);
    });
    box.appendChild(heading);
    box.appendChild(list);
    return box;
  }

function renderFields(page) {
    var grid = document.createElement("div");
    grid.className = "shim-grid";
    page.fields.forEach(function (field) {
      var card = document.createElement("div");
      card.className = "shim-card field";
      var label = document.createElement("label");
      var id = "field-" + page.id + "-" + field[0].toLowerCase().replace(/[^a-z0-9]+/g, "-");
      label.htmlFor = id;
      label.textContent = field[0];
      var input;
      if (field[1] === "textarea") {
        input = document.createElement("textarea");
        input.className = "textarea" + (field[3] === "large" ? " textarea-large" : "");
        input.rows = field[3] === "large" ? 6 : 4;
        input.value = field[2];
        if (page.id === "identity" && field[0] === "Assistant purpose") {
          input.value = sharedState.state.assistantPurpose;
          lifecycle.listen(input, "input", function () {
            sharedState.state.assistantPurpose = input.value;
            window.localStorage.setItem("ua.onboarding.assistant.purpose", sharedState.state.assistantPurpose);
          });
        } else if (page.id === "identity" && field[0] === "Response style") {
          input.value = sharedState.state.assistantResponseStyle;
          lifecycle.listen(input, "input", function () {
            sharedState.state.assistantResponseStyle = input.value;
            window.localStorage.setItem("ua.onboarding.assistant.response_style", sharedState.state.assistantResponseStyle);
          });
        }
      } else if (field[1] === "select") {
        input = document.createElement("select");
        input.className = "select";
        ["Keep current values", "LiteLLM", "OpenAI", "Anthropic", "Gemini", "Skip for now"].forEach(function (optionLabel) {
          var option = document.createElement("option");
          option.textContent = optionLabel;
          input.appendChild(option);
        });
      } else {
        input = document.createElement("input");
        input.type = field[1];
        input.className = "input";
        if (page.id === "identity" && field[0] === "Your display name") {
          input.value = sharedState.state.userDisplayName;
          lifecycle.listen(input, "input", function () {
            sharedState.state.userDisplayName = input.value || "Ada";
            window.localStorage.setItem("ua.onboarding.user.display_name", sharedState.state.userDisplayName);
          });
        } else if (page.id === "identity" && field[0] === "User role") {
          input.value = sharedState.state.userRole;
          lifecycle.listen(input, "input", function () {
            sharedState.state.userRole = input.value;
            window.localStorage.setItem("ua.onboarding.user.role", sharedState.state.userRole);
          });
        } else if (page.id === "identity" && field[0] === "Assistant name") {
          input.value = sharedState.state.assistantName;
          lifecycle.listen(input, "input", function () {
            sharedState.state.assistantName = input.value || "Lisa";
            window.localStorage.setItem("ua.preview.assistant.name", sharedState.state.assistantName);
          });
        }
      }
      input.id = id;
      input.placeholder = field[2];
      card.appendChild(label);
      card.appendChild(input);
      grid.appendChild(card);
    });
    return grid;
  }

function renderAdvancedSection(storageKey, title, copy, renderContent) {
    var details = document.createElement("details");
    details.className = "advanced-section";
    details.open = window.localStorage.getItem(storageKey) === "true";
    lifecycle.listen(details, "toggle", function () {
      window.localStorage.setItem(storageKey, details.open ? "true" : "false");
    });
    var summary = document.createElement("summary");
    var summaryTextWrap = document.createElement("div");
    summaryTextWrap.className = "advanced-summary-text";
    var summaryText = document.createElement("span");
    summaryText.className = "advanced-summary-title";
    summaryText.textContent = title;
    var summaryHint = document.createElement("small");
    summaryHint.textContent = copy;
    var chevron = document.createElement("span");
    chevron.className = "advanced-chevron";
    chevron.setAttribute("aria-hidden", "true");
    summaryTextWrap.appendChild(summaryText);
    summaryTextWrap.appendChild(summaryHint);
    summary.appendChild(summaryTextWrap);
    summary.appendChild(chevron);
    details.appendChild(summary);
    var body = document.createElement("div");
    body.className = "advanced-body";
    renderContent(body);
    details.appendChild(body);
    return details;
  }

function renderAdvancedGrid() {
    var grid = document.createElement("div");
    grid.className = "advanced-grid";
    return grid;
  }

function advancedInput(labelText, type, value, storageKey, stateKey, options) {
    var input = document.createElement("input");
    input.id = storageKey.replace(/[^a-z0-9]+/gi, "-");
    input.type = type;
    input.className = "input";
    input.value = value === null || value === undefined ? "" : String(value);
    if (options && options.placeholder) {
      input.placeholder = options.placeholder;
    }
    if (options && options.min !== undefined) {
      input.min = String(options.min);
    }
    if (options && options.max !== undefined) {
      input.max = String(options.max);
    }
    lifecycle.listen(input, "input", function () {
      var nextValue = type === "number" ? loadNumericInputValue(input.value) : input.value;
      model_setup.persistAdvancedValue(storageKey, stateKey, nextValue);
      if (options && typeof options.onChange === "function") {
        options.onChange();
      }
    });
    var field = model_setup.renderRuntimeField(labelText, input);
    if (options && Array.isArray(options.suggestions) && options.suggestions.length) {
      var dataList = document.createElement("datalist");
      dataList.id = input.id + "-suggestions";
      options.suggestions.forEach(function (suggestion) {
        var option = document.createElement("option");
        option.value = suggestion;
        dataList.appendChild(option);
      });
      input.setAttribute("list", dataList.id);
      field.appendChild(dataList);
    }
    return field;
  }

function advancedSecretInput(labelText, value, storageKey, stateKey, options) {
    var input = document.createElement("input");
    input.id = storageKey.replace(/[^a-z0-9]+/gi, "-");
    input.type = "password";
    input.className = "input";
    input.value = value === null || value === undefined ? "" : String(value);
    if (options && options.placeholder) {
      input.placeholder = options.placeholder;
    }
    lifecycle.listen(input, "input", function () {
      if (storageKey === "ua.preview.embedding.api_key" && stateKey === "embeddingApiKey") {
        model_setup.setEmbeddingApiKeyValue(input.value);
      } else {
        model_setup.persistAdvancedValue(storageKey, stateKey, input.value);
      }
      if (options && typeof options.onChange === "function") {
        options.onChange();
      }
    });
    return model_setup.renderRuntimeSecretField(labelText, input);
  }

function loadNumericInputValue(value) {
    if (value === "") {
      return "";
    }
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : "";
  }

function advancedSelect(labelText, value, optionsList, storageKey, stateKey, onChange) {
    var select = document.createElement("select");
    select.id = (storageKey || ("advanced-" + labelText)).replace(/[^a-z0-9]+/gi, "-");
    select.className = "select";
    optionsList.forEach(function (entry) {
      var option = document.createElement("option");
      option.value = entry[0];
      option.textContent = entry[1];
      option.selected = entry[0] === value;
      select.appendChild(option);
    });
    lifecycle.listen(select, "change", function () {
      if (storageKey && stateKey) {
        model_setup.persistAdvancedValue(storageKey, stateKey, select.value);
      }
      if (typeof onChange === "function") {
        onChange();
      }
    });
    return model_setup.renderRuntimeField(labelText, select);
  }

function advancedToggle(labelText, checked, storageKey, stateKey, onChange) {
    var card = document.createElement("div");
    card.className = "shim-card choice-card";
    var title = document.createElement("strong");
    title.textContent = labelText;
    var toggle = document.createElement("label");
    toggle.className = "toggle";
    var input = document.createElement("input");
    input.id = storageKey.replace(/[^a-z0-9]+/gi, "-");
    input.type = "checkbox";
    input.checked = checked;
    input.setAttribute("aria-label", labelText);
    lifecycle.listen(input, "change", function () {
      model_setup.persistAdvancedValue(storageKey, stateKey, input.checked);
      if (typeof onChange === "function") {
        onChange();
      }
    });
    var knob = document.createElement("span");
    knob.setAttribute("aria-hidden", "true");
    toggle.appendChild(input);
    toggle.appendChild(knob);
    card.appendChild(title);
    card.appendChild(toggle);
    return card;
  }

function renderChoices(page) {
    var stack = document.createElement("div");
    stack.className = "runtime-stack";
    var grid = document.createElement("div");
    grid.className = "shim-grid";
    page.choices.filter(function (choice) {
      // Nothing is generated when secrets come from the environment.
      return !(sharedState.state.secretsFromEnvironment && choice.key === "generate_internal_credentials");
    }).forEach(function (choice, index) {
      var choiceLabel = typeof choice === "string" ? choice : choice.label;
      var choiceHelp = typeof choice === "string" ? "" : choice.help;
      var choiceKey = typeof choice === "string" ? "choice_" + index : choice.key;
      var choiceEnabled = typeof choice === "string" ? index < 2 : Boolean(sharedState.state.safetyValues[choiceKey]);
      var card = document.createElement("div");
      card.className = "shim-card choice-card";
      var title = document.createElement("strong");
      title.textContent = choiceLabel;
      var titleWrap = document.createElement("span");
      titleWrap.className = "choice-title";
      titleWrap.appendChild(title);
      if (choiceHelp) {
        titleWrap.appendChild(createInfoButton(choiceLabel, choiceHelp, choiceLabel + " information"));
      }
      var toggle = document.createElement("span");
      toggle.className = "toggle";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.checked = choiceEnabled;
      lifecycle.listen(input, "change", function () {
        setSafetyValue(choiceKey, input.checked);
      });
      var knob = document.createElement("span");
      toggle.appendChild(input);
      toggle.appendChild(knob);
      card.appendChild(titleWrap);
      card.appendChild(toggle);
      grid.appendChild(card);
    });
    stack.appendChild(grid);
    if (page.id === "safety") {
      stack.appendChild(renderSafetyAdvanced());
    }
    return stack;
  }

function renderSafetyAdvanced() {
    return renderAdvancedSection(
      "ua.preview.safety.advanced.open",
      "Advanced Setup",
      "Credential broker, Bitwarden, Sealbox, and Activity Collector settings.",
      function (body) {
        var brokerGrid = renderAdvancedGrid();
        brokerGrid.appendChild(advancedInput("Broker internal base URL", "url", sharedState.state.secureToolBrokerInternalBaseUrl, "ua.preview.security.broker_internal_base_url", "secureToolBrokerInternalBaseUrl", { placeholder: "http://127.0.0.1:18600" }));
        brokerGrid.appendChild(advancedInput("Broker public port", "number", sharedState.state.secureToolBrokerPublicPort, "ua.preview.security.broker_public_port", "secureToolBrokerPublicPort", { min: 1, max: 65535, placeholder: "18600" }));
        brokerGrid.appendChild(advancedInput("Broker bind host", "text", sharedState.state.secureToolBrokerBindHost, "ua.preview.security.broker_bind_host", "secureToolBrokerBindHost", { placeholder: "127.0.0.1" }));
        brokerGrid.appendChild(advancedInput("Broker bind port", "number", sharedState.state.secureToolBrokerBindPort, "ua.preview.security.broker_bind_port", "secureToolBrokerBindPort", { min: 1, max: 65535, placeholder: "18600" }));
        brokerGrid.appendChild(advancedInput("Broker credentials dir", "text", sharedState.state.secureToolBrokerCredentialsDir, "ua.preview.security.broker_credentials_dir", "secureToolBrokerCredentialsDir", { placeholder: "broker_credentials" }));
        body.appendChild(brokerGrid);

        var credentialGrid = renderAdvancedGrid();
        credentialGrid.appendChild(advancedSelect("Credential state backend", sharedState.state.credentialStateBackendMode, [
          ["files", "Files"],
          ["credential_source", "Credential source"],
        ], "ua.preview.security.credential_state_backend.mode", "credentialStateBackendMode"));
        credentialGrid.appendChild(advancedSelect("Credential source", sharedState.state.credentialStateBackendSource, [
          ["sealbox", "Sealbox"],
          ["bitwarden", "Bitwarden"],
          ["vault", "Vault"],
          ["onepassword", "1Password"],
          ["keepass", "KeePass"],
        ], "ua.preview.security.credential_state_backend.source", "credentialStateBackendSource"));
        credentialGrid.appendChild(advancedInput("Credential namespace", "text", sharedState.state.credentialStateBackendNamespace, "ua.preview.security.credential_state_backend.namespace", "credentialStateBackendNamespace", { placeholder: "team-cred-store" }));
        credentialGrid.appendChild(advancedToggle("Fall back to files", sharedState.state.credentialStateBackendFallbackToFiles, "ua.preview.security.credential_state_backend.fallback_to_files", "credentialStateBackendFallbackToFiles"));
        body.appendChild(credentialGrid);

        var catalogGrid = renderAdvancedGrid();
        catalogGrid.appendChild(advancedToggle("Credential catalog", sharedState.state.credentialCatalogEnabled, "ua.preview.security.credential_catalog.enabled", "credentialCatalogEnabled"));
        catalogGrid.appendChild(advancedInput("Catalog database path", "text", sharedState.state.credentialCatalogDatabasePath, "ua.preview.security.credential_catalog.database_path", "credentialCatalogDatabasePath", { placeholder: "credential_catalog.db" }));
        catalogGrid.appendChild(advancedInput("Template overrides path", "text", sharedState.state.credentialCatalogTemplateOverridesPath, "ua.preview.security.credential_catalog.template_overrides_path", "credentialCatalogTemplateOverridesPath", { placeholder: "credential_template_overrides.yaml" }));
        catalogGrid.appendChild(advancedSelect("Default write source", sharedState.state.credentialCatalogDefaultWriteSource, [
          ["sealbox", "Sealbox"],
          ["bitwarden", "Bitwarden"],
          ["vault", "Vault"],
          ["onepassword", "1Password"],
          ["keepass", "KeePass"],
        ], "ua.preview.security.credential_catalog.default_write_source", "credentialCatalogDefaultWriteSource"));
        catalogGrid.appendChild(advancedInput("Collection session TTL seconds", "number", sharedState.state.credentialCatalogCollectionSessionTtlSeconds, "ua.preview.security.credential_catalog.collection_session_ttl_seconds", "credentialCatalogCollectionSessionTtlSeconds", { min: 1 }));
        catalogGrid.appendChild(advancedInput("Secret cache TTL seconds", "number", sharedState.state.credentialCatalogSecretCacheTtlSeconds, "ua.preview.security.credential_catalog.secret_cache_ttl_seconds", "credentialCatalogSecretCacheTtlSeconds", { min: 0 }));
        body.appendChild(catalogGrid);

        var bitwardenGrid = renderAdvancedGrid();
        bitwardenGrid.appendChild(advancedInput("Bitwarden server URL", "url", sharedState.state.bitwardenServerUrl, "ua.preview.security.bitwarden_server_url", "bitwardenServerUrl", { placeholder: "https://vault.example.com" }));
        body.appendChild(bitwardenGrid);

        var sealboxGrid = renderAdvancedGrid();
        sealboxGrid.appendChild(advancedToggle("Enable Sealbox", sharedState.state.sealboxEnabled, "ua.preview.security.sealbox.enabled", "sealboxEnabled"));
        sealboxGrid.appendChild(advancedToggle("Manage Sealbox service", sharedState.state.sealboxManageService, "ua.preview.security.sealbox.manage_service", "sealboxManageService"));
        sealboxGrid.appendChild(advancedToggle("Auto-initialize Sealbox", sharedState.state.sealboxAutoInitialize, "ua.preview.security.sealbox.auto_initialize", "sealboxAutoInitialize"));
        sealboxGrid.appendChild(advancedInput("Sealbox bind host", "text", sharedState.state.sealboxBindHost, "ua.preview.security.sealbox.bind_host", "sealboxBindHost", { placeholder: "127.0.0.1" }));
        sealboxGrid.appendChild(advancedInput("Sealbox bind port", "number", sharedState.state.sealboxBindPort, "ua.preview.security.sealbox.bind_port", "sealboxBindPort", { min: 1, max: 65535 }));
        sealboxGrid.appendChild(advancedInput("Sealbox store path", "text", sharedState.state.sealboxStorePath, "ua.preview.security.sealbox.store_path", "sealboxStorePath", { placeholder: "dbs/sealbox.db" }));
        sealboxGrid.appendChild(advancedInput("Sealbox service URL", "url", sharedState.state.sealboxServiceUrl, "ua.preview.security.sealbox.service_url", "sealboxServiceUrl", { placeholder: "https://sealbox.internal" }));
        sealboxGrid.appendChild(advancedInput("Sealbox root token file", "text", sharedState.state.sealboxRootTokenFile, "ua.preview.security.sealbox.root_token_file", "sealboxRootTokenFile", { placeholder: "sealbox/root_token" }));
        sealboxGrid.appendChild(advancedInput("Sealbox tenant material directory", "text", sharedState.state.sealboxTenantMaterialDir, "ua.preview.security.sealbox.tenant_material_dir", "sealboxTenantMaterialDir", { placeholder: "sealbox/tenants" }));
        sealboxGrid.appendChild(advancedInput("Sealbox ready timeout seconds", "number", sharedState.state.sealboxReadyTimeoutSeconds, "ua.preview.security.sealbox.ready_timeout_seconds", "sealboxReadyTimeoutSeconds", { min: 1 }));
        body.appendChild(sealboxGrid);

        var activityGrid = renderAdvancedGrid();
        activityGrid.appendChild(advancedInput("Activity collector base URL", "url", sharedState.state.activityCollectorBaseUrl, "ua.preview.activity.base_url", "activityCollectorBaseUrl", { placeholder: "http://127.0.0.1:8765" }));
        activityGrid.appendChild(advancedInput("Connect timeout seconds", "number", sharedState.state.activityCollectorConnectTimeoutSeconds, "ua.preview.activity.connect_timeout_seconds", "activityCollectorConnectTimeoutSeconds", { min: 0.001 }));
        activityGrid.appendChild(advancedInput("Request timeout seconds", "number", sharedState.state.activityCollectorRequestTimeoutSeconds, "ua.preview.activity.request_timeout_seconds", "activityCollectorRequestTimeoutSeconds", { min: 0.001 }));
        activityGrid.appendChild(advancedInput("Batch max events", "number", sharedState.state.activityCollectorBatchMaxEvents, "ua.preview.activity.batch_max_events", "activityCollectorBatchMaxEvents", { min: 1 }));
        activityGrid.appendChild(advancedInput("Flush interval seconds", "number", sharedState.state.activityCollectorFlushIntervalSeconds, "ua.preview.activity.flush_interval_seconds", "activityCollectorFlushIntervalSeconds", { min: 0.001 }));
        activityGrid.appendChild(advancedInput("Max buffered events", "number", sharedState.state.activityCollectorMaxBufferedEvents, "ua.preview.activity.max_buffered_events", "activityCollectorMaxBufferedEvents", { min: 1 }));
        activityGrid.appendChild(advancedToggle("Drop when unavailable", sharedState.state.activityCollectorDropWhenUnavailable, "ua.preview.activity.drop_when_unavailable", "activityCollectorDropWhenUnavailable"));
        activityGrid.appendChild(advancedInput("Collector bind host", "text", sharedState.state.activityCollectorBindHost, "ua.preview.activity.bind_host", "activityCollectorBindHost", { placeholder: "127.0.0.1" }));
        activityGrid.appendChild(advancedInput("Collector bind port", "number", sharedState.state.activityCollectorBindPort, "ua.preview.activity.bind_port", "activityCollectorBindPort", { min: 1, max: 65535 }));
        activityGrid.appendChild(advancedInput("User URL path", "text", sharedState.state.activityCollectorUserUrlPath, "ua.preview.activity.user_url_path", "activityCollectorUserUrlPath", { placeholder: "/user" }));
        activityGrid.appendChild(advancedInput("Admin URL path", "text", sharedState.state.activityCollectorAdminUrlPath, "ua.preview.activity.admin_url_path", "activityCollectorAdminUrlPath", { placeholder: "/admin" }));
        activityGrid.appendChild(advancedInput("OTEL trace base URL", "url", sharedState.state.activityCollectorOtelTraceBaseUrl, "ua.preview.activity.otel_trace_base_url", "activityCollectorOtelTraceBaseUrl", { placeholder: "Optional trace UI URL" }));
        activityGrid.appendChild(advancedInput("Ingest token env var", "text", sharedState.state.activityCollectorIngestTokenEnvVar, "ua.preview.activity.ingest_token_env_var", "activityCollectorIngestTokenEnvVar", { placeholder: "ACTIVITY_COLLECTOR_INGEST_TOKEN" }));
        activityGrid.appendChild(advancedSelect("Collector storage backend", sharedState.state.activityCollectorStorageBackend, [
          ["sqlite", "SQLite"],
          ["postgres", "Postgres"],
        ], "ua.preview.activity.storage.backend", "activityCollectorStorageBackend"));
        activityGrid.appendChild(advancedInput("Collector SQLite path", "text", sharedState.state.activityCollectorStorageSqlitePath, "ua.preview.activity.storage.sqlite.path", "activityCollectorStorageSqlitePath", { placeholder: "dbs/activity_collector.db" }));
        body.appendChild(activityGrid);
      }
    );
  }

function renderChannelField(channel, field) {
    if (field[1] === "generated-secret") {
      return renderGeneratedTokenField(channel, field);
    }
    var id = "channel-" + channel.key + "-" + field[0].toLowerCase().replace(/[^a-z0-9]+/g, "-");
    var fieldError = sharedState.state.communicationInvalidFields[communicationFieldKey(channel.key, field[0])] || "";
    if (field[1] === "toggle") {
      var toggleCard = document.createElement("label");
      toggleCard.className = "field channel-toggle-field" + (fieldError ? " is-invalid" : "");
      var toggleLabel = document.createElement("span");
      toggleLabel.className = "channel-toggle-label";
      toggleLabel.textContent = field[0];
      var toggle = document.createElement("span");
      toggle.className = "toggle";
      var toggleInput = document.createElement("input");
      toggleInput.id = id;
      toggleInput.type = "checkbox";
      toggleInput.checked = getChannelToggleValue(channel.key, field[0], Boolean(field[2]));
      toggleInput.setAttribute("aria-label", field[0]);
      lifecycle.listen(toggleInput, "change", function () {
        setChannelFieldValue(channel.key, field[0], toggleInput.checked ? "true" : "false");
      });
      var toggleKnob = document.createElement("span");
      toggleKnob.setAttribute("aria-hidden", "true");
      toggle.appendChild(toggleInput);
      toggle.appendChild(toggleKnob);
      toggleCard.appendChild(toggleLabel);
      toggleCard.appendChild(toggle);
      return toggleCard;
    }
    var card = document.createElement("div");
    card.className = "field" + (fieldError ? " is-invalid" : "");
    var label = document.createElement("label");
    label.htmlFor = id;
    label.textContent = field[0];
    if (channel.key === "website" && field[0] === "Public IP/URL") {
      label.className = "label-with-info";
      label.appendChild(createInfoButton(
        "Public IP/URL",
        "This should match the public IP address or URL of the computer hosting this container. Universal Agents uses NETWORK_PUBLIC_ORIGIN for this value, and the onboarding interface is expected to be reachable from that origin on port 18090.",
        "Public IP or URL information"
      ));
    }
    var input;
    if (field[1] === "select") {
      input = document.createElement("select");
      input.className = "select";
      [
        field[2],
        "Keep current values",
        "Mention only",
        "Open channel",
        "Pause polling",
        "Notify while offline",
        "One session per browser",
      ].forEach(function (optionLabel, index) {
        if (index > 0 && optionLabel === field[2]) {
          return;
        }
        var option = document.createElement("option");
        option.textContent = optionLabel;
        input.appendChild(option);
      });
      input.value = channelSelectDisplayValue(field[0], getChannelFieldValue(channel.key, field[0]) || field[2]);
      lifecycle.listen(input, "change", function () {
        setChannelFieldValue(channel.key, field[0], input.value);
      });
    } else {
      input = document.createElement("input");
      input.type = field[1];
      input.className = "input";
      input.placeholder = field[2];
      if (channel.key === "website" && field[0] === "Public IP/URL") {
        input.value = sharedState.state.networkPublicOrigin;
        lifecycle.listen(input, "input", function () {
          setNetworkPublicOrigin(input.value);
          setChannelFieldValue(channel.key, field[0], input.value);
        });
      } else {
        input.value = getChannelFieldValue(channel.key, field[0]);
        lifecycle.listen(input, "input", function () {
          setChannelFieldValue(channel.key, field[0], input.value);
        });
      }
    }
    input.id = id;
    if (fieldError) {
      input.setAttribute("aria-invalid", "true");
      input.setAttribute("aria-describedby", id + "-error");
    }
    card.appendChild(label);
    if (field[1] === "password") {
      card.appendChild(renderRevealableControl(input));
    } else {
      card.appendChild(input);
    }
    if (fieldError) {
      var error = document.createElement("p");
      error.id = id + "-error";
      error.className = "field-error";
      error.textContent = fieldError;
      card.appendChild(error);
    }
    return card;
  }

function channelSetupGuide(channel) {
    if (channel.key === "discord") {
      return {
        title: "Set up Discord",
        intro: "Use Discord when you want the assistant reachable in DMs, server channels, or threads through a bot account.",
        steps: [
          {
            text: "Open the Discord Developer Portal and create an application with a bot user.",
            href: "https://discord.com/developers/applications",
            linkText: "Discord Developer Portal",
          },
          {
            text: "Invite the bot to the server or shared channel where your UA team should answer.",
            href: "https://discord.com/developers/docs/topics/oauth2",
            linkText: "Discord OAuth2 docs",
          },
          {
            text: "Enable Developer Mode in Discord, then copy the bot/application ID and the channel ID.",
            href: "https://support.discord.com/hc/en-us/articles/206346498-Where-can-I-find-my-User-Server-Message-ID",
            linkText: "Finding Discord IDs",
          },
          {
            text: "Paste the bot token and allowed channel ID into the fields below.",
          },
        ],
      };
    }
    if (channel.key === "telegram") {
      return {
        title: "Set up Telegram",
        intro: "Telegram setup starts with a BotFather bot token, then optional group IDs if the assistant should answer in groups.",
        steps: [
          {
            text: "Start a chat with BotFather and create a new bot.",
            href: "https://core.telegram.org/bots/features#botfather",
            linkText: "BotFather guide",
          },
          {
            text: "Copy the bot token and bot username from BotFather.",
          },
          {
            text: "Add the bot to a group for shared chat access and send a message in that group.",
            href: "https://core.telegram.org/bots/api#getupdates",
            linkText: "Telegram getUpdates",
          },
          {
            text: "Use getUpdates to find the chat ID, then paste the bot token and allowed chat ID below.",
          },
        ],
      };
    }
    if (channel.key === "signal") {
      return {
        title: "Set up Signal",
        intro: "Signal requires a running signal-cli REST service and the exact account or number registered with that service.",
        steps: [
          {
            text: "Run signal-cli-rest-api with a linked Signal account before enabling this channel.",
            href: "https://github.com/bbernhard/signal-cli-rest-api",
            linkText: "signal-cli-rest-api",
          },
          {
            text: "Paste the REST API URL and registered Signal account below.",
          },
          {
            text: "For groups, add allowed group IDs and at least one mention marker such as @Lisa.",
            href: "https://github.com/bbernhard/signal-cli-rest-api/blob/master/doc/EXAMPLES.md",
            linkText: "Signal REST examples",
          },
        ],
      };
    }
    if (channel.key === "photon") {
      return {
        title: "Set up iMessage",
        intro: "iMessage uses Photon Spectrum. Start in Photon, create a project, then bring the project id and secret back here.",
        steps: [
          {
            text: "Open Photon, sign in with Google or Discord, and create a new Spectrum project.",
            href: "https://app.photon.codes/",
            linkText: "Photon dashboard",
          },
          {
            text: "Create or open your Photon Spectrum project and copy both PHOTON_PROJECT_ID and PHOTON_PROJECT_SECRET.",
          },
          {
            text: "Paste both values below. The project secret is written to team.env and is not stored in browser local storage.",
          },
          {
            text: "For group chats, add allowed group IDs and at least one mention marker such as @Lisa.",
          },
        ],
      };
    }
    if (channel.key === "whatsapp") {
      return {
        title: "Set up WhatsApp",
        intro: "WhatsApp setup depends on a Meta WhatsApp Business app and a webhook that can reach this UA instance.",
        steps: [
          {
            text: "Create or connect a WhatsApp Business app in Meta for Developers.",
            href: "https://developers.facebook.com/docs/whatsapp/cloud-api/get-started",
            linkText: "WhatsApp Cloud API",
          },
          {
            text: "Configure a webhook URL that points to this UA instance.",
            href: "https://developers.facebook.com/docs/whatsapp/cloud-api/guides/set-up-webhooks",
            linkText: "Webhook setup",
          },
          {
            text: "Copy the sender or phone-number ID from the provider dashboard into the fields below.",
          },
        ],
      };
    }
    return {
      title: "Set up channel",
      intro: "Enable the channel only after you know which account, token, or webhook should connect it to Universal Agents.",
      steps: [
        {
          text: "Enable the channel, then provide the required channel-specific details.",
        },
        {
          text: "Set it as the default delivery channel only when you want proactive messages sent there.",
        },
      ],
    };
  }

function openSetupOverlay(channel) {
    var guide = channelSetupGuide(channel);
    var existing = document.querySelector(".setup-overlay");
    if (existing) {
      existing.remove();
    }
    var overlay = document.createElement("div");
    overlay.className = "setup-overlay";
    var dialog = document.createElement("section");
    dialog.className = "setup-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    var closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "setup-close";
    closeButton.textContent = "Close";
    lifecycle.listen(closeButton, "click", function () {
      overlay.remove();
    });
    var title = document.createElement("h2");
    title.textContent = guide.title;
    var intro = null;
    if (guide.intro) {
      intro = document.createElement("p");
      intro.className = "setup-intro";
      intro.textContent = guide.intro;
    }
    var steps = document.createElement("ol");
    steps.className = "setup-steps";
    guide.steps.forEach(function (step) {
      var item = document.createElement("li");
      item.appendChild(document.createTextNode(step.text));
      if (step.href) {
        item.appendChild(document.createTextNode(" "));
        var link = document.createElement("a");
        link.className = "copy-link";
        link.href = step.href;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = step.linkText;
        item.appendChild(link);
      }
      steps.appendChild(item);
    });
    lifecycle.listen(overlay, "click", function (event) {
      if (event.target === overlay) {
        overlay.remove();
      }
    });
    dialog.appendChild(closeButton);
    dialog.appendChild(title);
    if (intro) {
      dialog.appendChild(intro);
    }
    dialog.appendChild(steps);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    lifecycle.frame(function () {
      overlay.classList.add("is-open");
    });
  }

function openInfoOverlay(titleText, bodyText) {
    var existing = document.querySelector(".setup-overlay");
    if (existing) {
      existing.remove();
    }
    var overlay = document.createElement("div");
    overlay.className = "setup-overlay";
    var dialog = document.createElement("section");
    dialog.className = "setup-dialog info-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    var closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "setup-close";
    closeButton.textContent = "Close";
    lifecycle.listen(closeButton, "click", function () {
      overlay.remove();
    });
    var title = document.createElement("h2");
    title.textContent = titleText;
    var body = document.createElement("p");
    body.className = "info-dialog-copy";
    body.textContent = bodyText;
    lifecycle.listen(overlay, "click", function (event) {
      if (event.target === overlay) {
        overlay.remove();
      }
    });
    dialog.appendChild(closeButton);
    dialog.appendChild(title);
    dialog.appendChild(body);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    lifecycle.frame(function () {
      overlay.classList.add("is-open");
    });
  }

function closeHoverInfo() {
    if (shellState.activeHoverInfo) {
      shellState.activeHoverInfo.remove();
      shellState.activeHoverInfo = null;
    }
  }

function openHoverInfo(anchor, titleText, bodyText) {
    closeHoverInfo();
    var popover = document.createElement("aside");
    popover.className = "hover-info-popover";
    popover.setAttribute("role", "tooltip");
    var title = document.createElement("strong");
    title.textContent = titleText;
    var body = document.createElement("p");
    body.textContent = bodyText;
    popover.appendChild(title);
    popover.appendChild(body);
    document.body.appendChild(popover);
    var rect = anchor.getBoundingClientRect();
    var popoverRect = popover.getBoundingClientRect();
    var left = appearance.clamp(rect.left + rect.width / 2 - popoverRect.width / 2, 16, window.innerWidth - popoverRect.width - 16);
    var top = rect.bottom + 10;
    if (top + popoverRect.height > window.innerHeight - 16) {
      top = rect.top - popoverRect.height - 10;
    }
    popover.style.left = left + "px";
    popover.style.top = Math.max(16, top) + "px";
    shellState.activeHoverInfo = popover;
    lifecycle.frame(function () {
      popover.classList.add("is-visible");
    });
  }

function createInfoButton(titleText, bodyText, ariaLabel) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "info-dot info-button";
    button.textContent = "i";
    button.setAttribute("aria-label", ariaLabel || titleText + " information");
    lifecycle.listen(button, "mouseenter", function () {
      openHoverInfo(button, titleText, bodyText);
    });
    lifecycle.listen(button, "mouseleave", closeHoverInfo);
    lifecycle.listen(button, "focus", function () {
      openHoverInfo(button, titleText, bodyText);
    });
    lifecycle.listen(button, "blur", closeHoverInfo);
    lifecycle.listen(button, "click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      closeHoverInfo();
      openInfoOverlay(titleText, bodyText);
    });
    return button;
  }

function syncChannelAccordion(list) {
    sharedState.channelDefinitions.forEach(function (channel) {
      var item = list.querySelector('[data-channel-key="' + channel.key + '"]');
      if (!item) {
        return;
      }
      var enabled = Boolean(sharedState.state.enabledChannels[channel.key]);
      var isOpen = enabled && sharedState.state.activeChannel === channel.key;
      item.classList.toggle("is-enabled", enabled);
      item.classList.toggle("is-open", isOpen);
      var enabledInput = item.querySelector(".channel-enabled-input");
      if (enabledInput) {
        enabledInput.checked = enabled;
      }
      var defaultInput = item.querySelector(".channel-default-input");
      if (defaultInput) {
        defaultInput.checked = sharedState.state.defaultChannel === channel.key;
        defaultInput.disabled = !enabled;
      }
      var setupButton = item.querySelector(".channel-setup-button");
      if (setupButton) {
        setupButton.disabled = !enabled;
      }
    });
  }

function renderVoiceControls() {
    var card = document.createElement("section");
    card.className = "shim-card voice-card";
    var header = document.createElement("div");
    header.className = "voice-header";
    var text = document.createElement("div");
    var title = document.createElement("strong");
    title.textContent = "Use Voice";
    var caption = document.createElement("p");
    caption.textContent = "Enable chatting with your agent via voice.";
    text.appendChild(title);
    text.appendChild(caption);

    var toggle = document.createElement("label");
    toggle.className = "toggle";
    var toggleInput = document.createElement("input");
    toggleInput.type = "checkbox";
    toggleInput.checked = sharedState.state.voiceEnabled;
    var knob = document.createElement("span");
    toggle.appendChild(toggleInput);
    toggle.appendChild(knob);
    header.appendChild(text);
    header.appendChild(toggle);

    var modeRow = document.createElement("div");
    modeRow.className = "voice-mode-row";
    var modeLabel = document.createElement("span");
    modeLabel.textContent = "Response mode";
    var modes = document.createElement("div");
    modes.className = "segmented-control";
    [
      ["voice_only", "Voice only"],
      ["voice_and_text", "Voice + text"],
      ["text_only", "Text only"],
    ].forEach(function (mode) {
      var option = document.createElement("label");
      option.className = "segment-option";
      var input = document.createElement("input");
      input.type = "radio";
      input.name = "voice-response-mode";
      input.value = mode[0];
      input.checked = sharedState.state.voiceResponseMode === mode[0];
      input.disabled = !sharedState.state.voiceEnabled;
      var label = document.createElement("span");
      label.textContent = mode[1];
      lifecycle.listen(input, "change", function () {
        setVoiceResponseMode(input.value);
      });
      option.appendChild(input);
      option.appendChild(label);
      modes.appendChild(option);
    });
    modeRow.appendChild(modeLabel);
    modeRow.appendChild(modes);

    function syncVoiceCard() {
      card.classList.toggle("is-disabled", !sharedState.state.voiceEnabled);
      modeRow.querySelectorAll("input").forEach(function (input) {
        input.disabled = !sharedState.state.voiceEnabled;
      });
    }

    lifecycle.listen(toggleInput, "change", function () {
      setVoiceEnabled(toggleInput.checked);
      syncVoiceCard();
    });

    card.appendChild(header);
    card.appendChild(modeRow);
    syncVoiceCard();
    return card;
  }

function renderChannels() {
    var stack = document.createElement("div");
    stack.className = "communication-stack";
    stack.appendChild(renderVoiceControls());
    if (sharedState.state.communicationValidationMessages.length) {
      var validation = document.createElement("div");
      validation.className = "communication-validation";
      validation.setAttribute("role", "alert");
      var validationTitle = document.createElement("strong");
      validationTitle.textContent = "Finish the enabled channel setup";
      var validationText = document.createElement("p");
      validationText.textContent = communicationValidationText();
      validation.appendChild(validationTitle);
      validation.appendChild(validationText);
      stack.appendChild(validation);
    }
    var list = document.createElement("div");
    list.className = "channel-accordion";
    sharedState.channelDefinitions.forEach(function (channel) {
      var enabled = Boolean(sharedState.state.enabledChannels[channel.key]);
      var isOpen = enabled && sharedState.state.activeChannel === channel.key;
      var item = document.createElement("section");
      item.className = "channel-item" + (enabled ? " is-enabled" : "") + (isOpen ? " is-open" : "") + (channel.unavailable ? " is-unavailable" : "");
      item.dataset.channelKey = channel.key;

      var header = document.createElement("div");
      header.className = "channel-header";
      var title = document.createElement("button");
      title.type = "button";
      title.className = "channel-title";
      title.textContent = channel.label;
      title.disabled = Boolean(channel.unavailable);
      lifecycle.listen(title, "click", function () {
        if (channel.unavailable) {
          return;
        }
        if (!enabled) {
          setChannelEnabled(channel.key, true);
        } else {
          setActiveChannel(channel.key);
        }
        enabled = Boolean(sharedState.state.enabledChannels[channel.key]);
        syncChannelAccordion(list);
      });

      var toggle = document.createElement("label");
      toggle.className = "toggle";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.className = "channel-enabled-input";
      input.checked = enabled;
      input.disabled = Boolean(channel.unavailable);
      lifecycle.listen(input, "change", function () {
        setChannelEnabled(channel.key, input.checked);
        enabled = Boolean(sharedState.state.enabledChannels[channel.key]);
        syncChannelAccordion(list);
      });
      var knob = document.createElement("span");
      toggle.appendChild(input);
      toggle.appendChild(knob);

      header.appendChild(title);
      if (channel.unavailable) {
        var unavailable = document.createElement("span");
        unavailable.className = "channel-unavailable-note";
        unavailable.textContent = channel.unavailableNote || "Not yet available";
        header.appendChild(unavailable);
      }
      header.appendChild(toggle);

      var details = document.createElement("div");
      details.className = "channel-details";
      var detailsInner = document.createElement("div");
      detailsInner.className = "channel-details-inner";

      var defaultControl = document.createElement("label");
      defaultControl.className = "channel-default";
      var defaultText = document.createElement("span");
      defaultText.textContent = "Set as default delivery channel";
      var defaultToggle = document.createElement("span");
      defaultToggle.className = "toggle";
      var defaultInput = document.createElement("input");
      defaultInput.type = "radio";
      defaultInput.className = "channel-default-input";
      defaultInput.name = "default-delivery-channel";
      defaultInput.checked = sharedState.state.defaultChannel === channel.key;
      defaultInput.disabled = !enabled;
      lifecycle.listen(defaultInput, "change", function () {
        setDefaultChannel(channel.key);
        syncChannelAccordion(list);
      });
      var defaultKnob = document.createElement("span");
      defaultToggle.appendChild(defaultInput);
      defaultToggle.appendChild(defaultKnob);
      defaultControl.appendChild(defaultText);
      defaultControl.appendChild(defaultToggle);
      detailsInner.appendChild(defaultControl);

      if (channel.key !== "website") {
        var setupButton = document.createElement("button");
        setupButton.type = "button";
        setupButton.className = "button runtime-secondary channel-setup-button";
        setupButton.textContent = "Set me up";
        setupButton.disabled = !enabled;
        lifecycle.listen(setupButton, "click", function () {
          openSetupOverlay(channel);
        });
        detailsInner.appendChild(setupButton);
      }

      var fields = document.createElement("div");
      fields.className = "channel-fields";
      channel.fields.forEach(function (field) {
        fields.appendChild(renderChannelField(channel, field));
      });
      detailsInner.appendChild(fields);

      details.appendChild(detailsInner);
      item.appendChild(header);
      item.appendChild(details);
      list.appendChild(item);
    });
    lifecycle.frame(function () {
      syncChannelAccordion(list);
    });
    stack.appendChild(list);
    stack.appendChild(renderCommunicationAdvanced());
    return stack;
  }

function renderCommunicationAdvanced() {
    return renderAdvancedSection(
      "ua.preview.communication.advanced.open",
      "Advanced Setup",
      "Internal website and user docs paths.",
      function (body) {
        var grid = renderAdvancedGrid();
        grid.appendChild(advancedInput("User docs root", "text", sharedState.state.internalWebsiteDocsRoot, "ua.preview.internal_website.docs_root", "internalWebsiteDocsRoot", { placeholder: "user_docs/site" }));
        grid.appendChild(advancedInput("Skills docs source", "text", sharedState.state.internalWebsiteSkillsDir, "ua.preview.internal_website.skills_dir", "internalWebsiteSkillsDir", { placeholder: ".skills" }));
        body.appendChild(grid);
      }
    );
  }

function renderHourSelect(id, value, onChange) {
    var select = document.createElement("select");
    select.id = id;
    select.className = "select";
    for (var hour = 0; hour < 24; hour += 1) {
      var option = document.createElement("option");
      option.value = String(hour);
      option.textContent = String(hour).padStart(2, "0") + ":00";
      option.selected = hour === value;
      select.appendChild(option);
    }
    lifecycle.listen(select, "change", function () {
      onChange(select.value);
    });
    return select;
  }

function renderPreferences() {
    var stack = document.createElement("div");
    stack.className = "preferences-stack";

    var quietCard = document.createElement("section");
    quietCard.className = "shim-card preference-card";
    var header = document.createElement("div");
    header.className = "preference-header";
    var text = document.createElement("div");
    text.className = "preference-heading";
    var title = document.createElement("strong");
    title.textContent = "Quiet hours";
    var titleWrap = document.createElement("span");
    titleWrap.className = "choice-title";
    titleWrap.appendChild(title);
    titleWrap.appendChild(createInfoButton(
      "Quiet hours",
      "Quiet hours defer non-urgent proactive messages between the selected start and end times in your runtime timezone. Urgent messages can still be allowed through when that option is enabled.",
      "Quiet hours information"
    ));
    var caption = document.createElement("p");
    caption.textContent = "Defer non-urgent proactive messages while you are offline or resting.";
    text.appendChild(titleWrap);
    text.appendChild(caption);

    var toggle = document.createElement("label");
    toggle.className = "toggle";
    var toggleInput = document.createElement("input");
    toggleInput.type = "checkbox";
    toggleInput.checked = sharedState.state.quietHoursEnabled;
    lifecycle.listen(toggleInput, "change", function () {
      setQuietHoursEnabled(toggleInput.checked);
      quietCard.classList.toggle("is-disabled", !sharedState.state.quietHoursEnabled);
    });
    var knob = document.createElement("span");
    toggle.appendChild(toggleInput);
    toggle.appendChild(knob);
    header.appendChild(text);
    header.appendChild(toggle);

    var grid = document.createElement("div");
    grid.className = "preferences-grid";

    var startField = document.createElement("div");
    startField.className = "field";
    var startLabel = document.createElement("label");
    startLabel.htmlFor = "quiet-hours-start";
    startLabel.textContent = "Start";
    startField.appendChild(startLabel);
    startField.appendChild(renderHourSelect("quiet-hours-start", sharedState.state.quietHoursStart, setQuietHoursStart));

    var endField = document.createElement("div");
    endField.className = "field";
    var endLabel = document.createElement("label");
    endLabel.htmlFor = "quiet-hours-end";
    endLabel.textContent = "End";
    endField.appendChild(endLabel);
    endField.appendChild(renderHourSelect("quiet-hours-end", sharedState.state.quietHoursEnd, setQuietHoursEnd));

    var timezoneField = document.createElement("div");
    timezoneField.className = "field";
    var timezoneLabel = document.createElement("label");
    timezoneLabel.htmlFor = "quiet-hours-timezone";
    timezoneLabel.textContent = "Timezone";
    var timezoneInput = document.createElement("input");
    timezoneInput.id = "quiet-hours-timezone";
    timezoneInput.className = "input";
    timezoneInput.type = "text";
    timezoneInput.value = sharedState.state.runtimeTimezone;
    timezoneInput.readOnly = true;
    timezoneField.appendChild(timezoneLabel);
    timezoneField.appendChild(timezoneInput);

    var urgentField = document.createElement("label");
    urgentField.className = "preference-inline-toggle preference-urgent-toggle";
    var urgentText = document.createElement("span");
    urgentText.textContent = "Allow urgent messages through";
    var urgentToggle = document.createElement("span");
    urgentToggle.className = "toggle";
    var urgentInput = document.createElement("input");
    urgentInput.type = "checkbox";
    urgentInput.checked = sharedState.state.quietHoursUrgent;
    lifecycle.listen(urgentInput, "change", function () {
      setQuietHoursUrgent(urgentInput.checked);
    });
    var urgentKnob = document.createElement("span");
    urgentToggle.appendChild(urgentInput);
    urgentToggle.appendChild(urgentKnob);
    urgentField.appendChild(urgentText);
    urgentField.appendChild(urgentToggle);

    grid.appendChild(startField);
    grid.appendChild(endField);
    grid.appendChild(timezoneField);
    grid.appendChild(urgentField);
    quietCard.appendChild(header);
    quietCard.appendChild(grid);
    quietCard.classList.toggle("is-disabled", !sharedState.state.quietHoursEnabled);
    stack.appendChild(quietCard);
    return stack;
  }

function renderAdvancedEnvField(definition) {
    var currentValue = sharedState.state.advancedEnvValues[definition.key] || "";
    if (definition.type === "select") {
      var select = document.createElement("select");
      select.id = "advanced-env-" + definition.key.replace(/[^a-z0-9]+/gi, "-");
      select.className = "select";
      definition.options.forEach(function (entry) {
        var option = document.createElement("option");
        option.value = entry[0];
        option.textContent = entry[1];
        option.selected = entry[0] === currentValue;
        select.appendChild(option);
      });
      lifecycle.listen(select, "change", function () {
        setAdvancedEnvValue(definition.key, select.value);
      });
      return model_setup.renderRuntimeField(definition.label, select);
    }

    var input = document.createElement("input");
    input.id = "advanced-env-" + definition.key.replace(/[^a-z0-9]+/gi, "-");
    input.type = definition.type || "text";
    input.className = "input";
    input.value = currentValue;
    input.placeholder = definition.placeholder || definition.env;
    if (definition.min !== undefined) {
      input.min = String(definition.min);
    }
    lifecycle.listen(input, "input", function () {
      setAdvancedEnvValue(definition.key, input.value);
    });
    if (definition.type === "password") {
      return model_setup.renderRuntimeSecretField(definition.label, input);
    }
    return model_setup.renderRuntimeField(definition.label, input);
  }

function renderSecretsAdvanced() {
    return renderAdvancedSection(
      "ua.preview.secrets.advanced.open",
      "Advanced .env Setup",
      "Optional service endpoints and integration values written to team.env.",
      function (body) {
        var grid = renderAdvancedGrid();
        shellState.advancedEnvDefinitions.forEach(function (definition) {
          grid.appendChild(renderAdvancedEnvField(definition));
        });
        body.appendChild(grid);
      }
    );
  }

function renderSecrets() {
    var stack = document.createElement("div");
    stack.className = "secret-stack";
    var grid = document.createElement("div");
    grid.className = "shim-grid";
    var warning;
    function syncVoiceWarning() {
      if (warning) {
        warning.hidden = !(sharedState.state.voiceEnabled && !sharedState.state.secretValues.xai_api_key);
      }
    }
    shellState.secretDefinitions.forEach(function (definition) {
      var card = document.createElement("div");
      card.className = "shim-card field";
      var id = "secret-" + definition.label.toLowerCase().replace(/[^a-z0-9]+/g, "-");
      var label = document.createElement("label");
      label.htmlFor = id;
      appendLinkedLabel(label, definition.label, definition.linkText, definition.href);
      var input = document.createElement("input");
      input.id = id;
      input.type = "password";
      input.className = "input";
      input.placeholder = definition.placeholder;
      input.value = sharedState.state.secretValues[definition.key] || "";
      lifecycle.listen(input, "input", function () {
        setSecretValue(definition.key, input.value);
        if (definition.key === "xai_api_key") {
          syncVoiceWarning();
        }
      });
      card.appendChild(label);
      card.appendChild(renderRevealableControl(input));
      grid.appendChild(card);
    });
    stack.appendChild(grid);
    stack.appendChild(account_sharing.renderAccountSharing());
    var storeNote = document.createElement("p");
    storeNote.className = "advanced-copy";
    storeNote.textContent = shellState.CREDENTIAL_STORE_KEYS_NOTE;
    stack.appendChild(storeNote);
    warning = document.createElement("div");
    warning.className = "warning-callout voice-warning";
    var icon = document.createElement("span");
    icon.className = "warning-icon";
    icon.textContent = "!";
    var copy = document.createElement("p");
    copy.textContent = "Voice is enabled, but the xAI Voice API Key is empty. Voice will not work until that key is configured.";
    warning.appendChild(icon);
    warning.appendChild(copy);
    syncVoiceWarning();
    stack.appendChild(warning);
    stack.appendChild(renderSecretsAdvanced());
    return stack;
  }

function teamSkillsStatusClassName() {
    var className = "runtime-test-status team-skills-status";
    if (sharedState.state.teamSkillsValidationKind === "success") {
      className += " is-success";
    }
    if (sharedState.state.teamSkillsValidationKind === "warning" || sharedState.state.teamSkillsValidationKind === "error") {
      className += " is-warning";
    }
    return className;
  }

function renderTeamSkillsStatus() {
    if (!sharedState.state.teamSkillsValidationStatus) {
      return null;
    }
    var status = document.createElement("p");
    status.className = teamSkillsStatusClassName();
    status.textContent = sharedState.state.teamSkillsValidationStatus;
    return status;
  }

function renderTeamSkillsRepositories() {
    var section = document.createElement("div");
    section.className = "advanced-subsection team-skills-repos";
    var description = document.createElement("p");
    description.className = "advanced-copy";
    description.textContent = "Empower your team with skills by supplying public repositories below. Make sure the skills are in a \"skills\" directory at the top level of the repo. Repository URLs are saved so Docker can refresh externally managed skills before a later launch.";
    section.appendChild(description);

    sharedState.state.teamSkillRepos.forEach(function (repo, index) {
      var row = document.createElement("div");
      row.className = "advanced-fallback-row team-skills-repo-row";
      var input = document.createElement("input");
      input.id = "team-skills-repo-" + index;
      input.type = "url";
      input.className = "input";
      input.placeholder = shellState.DEFAULT_TEAM_SKILLS_REPO;
      input.value = repo;
      lifecycle.listen(input, "input", function () {
        sharedState.state.teamSkillRepos[index] = input.value;
        sharedState.state.teamSkillsValidationStatus = "";
        sharedState.state.teamSkillsValidationKind = "pending";
        var status = section.querySelector(".team-skills-status");
        if (status) {
          status.remove();
        }
        persistTeamSkillRepos();
      });
      row.appendChild(model_setup.renderRuntimeField("Team skills repo:", input));

      if (index > 0) {
        var removeButton = document.createElement("button");
        removeButton.type = "button";
        removeButton.className = "button runtime-secondary advanced-remove-button";
        removeButton.textContent = "Remove";
        lifecycle.listen(removeButton, "click", function () {
          sharedState.state.teamSkillRepos.splice(index, 1);
          sharedState.state.teamSkillsValidationStatus = "";
          sharedState.state.teamSkillsValidationKind = "pending";
          persistTeamSkillRepos();
          renderCurrentPage();
        });
        row.appendChild(removeButton);
      }
      section.appendChild(row);
    });

    var actions = document.createElement("div");
    actions.className = "advanced-subsection-header";
    var addButton = document.createElement("button");
    addButton.type = "button";
    addButton.className = "button runtime-secondary advanced-add-button";
    addButton.textContent = "+";
    addButton.setAttribute("aria-label", "Add team skills repository");
    lifecycle.listen(addButton, "click", function () {
      sharedState.state.teamSkillRepos.push("");
      persistTeamSkillRepos();
      renderCurrentPage();
    });
    actions.appendChild(addButton);
    section.appendChild(actions);

    var status = renderTeamSkillsStatus();
    if (status) {
      section.appendChild(status);
    }
    return section;
  }

function renderTeamAdvanced() {
    return renderAdvancedSection(
      "ua.preview.team.advanced.open",
      "Advanced Setup",
      "Skill repositories for the whole team.",
      function (body) {
        body.appendChild(renderTeamSkillsRepositories());
      }
    );
  }

function renderAgents() {
    var stack = document.createElement("div");
    stack.className = "team-stack";
    var list = document.createElement("div");
    list.className = "status-list";
    sharedState.agents.forEach(function (agent) {
      var row = document.createElement("div");
      row.className = "agent-row" + (agent[2] ? " is-locked" : "");
      var text = document.createElement("div");
      var title = document.createElement("p");
      title.className = "agent-title";
      var displayName = agentDisplayName(agent);
      var titleWrap = document.createElement("span");
      titleWrap.className = "agent-title-wrap";
      var titleText = document.createElement("span");
      titleText.textContent = displayName;
      titleWrap.appendChild(titleText);
      titleWrap.appendChild(createInfoButton(displayName, agent[5] || agent[1], displayName + " information"));
      title.appendChild(titleWrap);
      var caption = document.createElement("p");
      caption.className = "agent-caption";
      caption.textContent = agent[1];
      text.appendChild(title);
      text.appendChild(caption);

      var toggle = document.createElement("label");
      toggle.className = "toggle";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.checked = Boolean(sharedState.state.selectedAgents[agent[0]]);
      input.disabled = agent[2];
      lifecycle.listen(input, "change", function () {
        sharedState.state.selectedAgents[agent[0]] = input.checked;
        window.localStorage.setItem(selectedAgentStorageKey(agent[0]), input.checked ? "true" : "false");
      });
      var knob = document.createElement("span");
      toggle.appendChild(input);
      toggle.appendChild(knob);

      row.appendChild(text);
      row.appendChild(toggle);
      if (!agent[2]) {
        var count = document.createElement("input");
        count.type = "number";
        count.min = "0";
        count.max = "8";
        count.value = String(sharedState.state.selectedAgentCounts[agent[0]]);
        count.className = "count-input";
        lifecycle.listen(count, "input", function () {
          var parsed = appearance.clamp(Number(count.value), 0, 8);
          sharedState.state.selectedAgentCounts[agent[0]] = Number.isFinite(parsed) ? parsed : agent[3];
          window.localStorage.setItem(selectedAgentCountStorageKey(agent[0]), String(sharedState.state.selectedAgentCounts[agent[0]]));
        });
        row.appendChild(count);
      }
      list.appendChild(row);
    });
    stack.appendChild(list);
    stack.appendChild(renderTeamAdvanced());
    return stack;
  }

function renderBody(page) {
    if (page.kind === "welcome") {
      return renderWelcome();
    }
    if (page.kind === "begin") {
      return renderBegin();
    }
    if (page.kind === "fields") {
      return renderFields(page);
    }
    if (page.kind === "runtime") {
      return model_setup.renderRuntime();
    }
    if (page.kind === "choices") {
      return renderChoices(page);
    }
    if (page.kind === "channels") {
      return renderChannels();
    }
    if (page.kind === "preferences") {
      return renderPreferences();
    }
    if (page.kind === "secrets") {
      return renderSecrets();
    }
    if (page.kind === "agents") {
      return renderAgents();
    }
    if (page.kind === "review") {
      return finalization.renderReview();
    }
    if (page.kind === "standby") {
      return finalization.renderStandby();
    }
    if (page.kind === "status") {
      return finalization.renderStatus();
    }
    return renderWelcome();
  }

function renderCurrentPage() {
    var page = sharedState.pages[sharedState.state.pageIndex];
    if (page.id !== "runtime") {
      model_setup.stopXAIDeviceLoginTimers();
    }
    shellState.pageKicker.textContent = page.kicker;
    shellState.pageTitle.textContent = page.title;
    if (page.id === "runtime") {
      shellState.pageCopy.innerHTML = "Select your team's location and genAI provider below. In Universal Agents, the litellm provider setting selects an OpenAI API-compatible endpoint; it is equivalent to configuring an OpenAI API, but the endpoint does not need to be operated by OpenAI or run a separate LiteLLM proxy. Fresh setups use <a class=\"copy-link\" href=\"https://openrouter.ai/\" target=\"_blank\" rel=\"noreferrer\">OpenRouter</a> with DeepSeek V4.1 Flash by default. Please note that while OpenAI currently allows subscription-based plans for agentic work, other providers may consider this against their Terms Of Service.";
    } else if (page.id === "channels") {
      shellState.pageCopy.textContent = "Configure how you would like to communicate with " + (sharedState.state.assistantName || "your assistant") + ". We recommend Telegram or Discord as the most integrated methods, but if you're just starting out, web is OK too.";
    } else if (page.id === "agents") {
      shellState.pageCopy.textContent = "Team Architect is always on. Toggle specialists to stand at the ready to help " + (sharedState.state.assistantName || "your assistant") + " when needed. Don't worry, even if you don't create them now, " + (sharedState.state.assistantName || "your assistant") + " will be able to spawn them on demand as necessary.";
    } else {
      shellState.pageCopy.textContent = page.copy;
    }
    shellState.pageBody.innerHTML = "";
    shellState.pageBody.classList.remove("is-centered");
    shellState.pageBody.appendChild(renderBody(page));
    shellState.backButton.disabled = sharedState.state.pageIndex === 0;
    shellState.backButton.hidden = page.id === "status";
    shellState.progressLabel.parentElement.hidden = page.id === "status";
    sharedState.nextButton.hidden = page.id === "standby";
    if (page.id === "status") {
      sharedState.nextButton.textContent = "Open Status";
      sharedState.nextButton.className = "button primary web-chat-button";
    } else if (page.id === "review") {
      sharedState.nextButton.textContent = "Launch!";
      sharedState.nextButton.className = "button launch";
    } else {
      sharedState.nextButton.textContent = "Next >>";
      sharedState.nextButton.className = "button primary";
    }
    sharedState.nextButton.parentElement.classList.toggle("is-status", page.id === "status");
    sharedState.nextButton.disabled =
      // An import continues on its own page (Open export), not through the next steps.
      (page.kind === "begin" && sharedState.state.beginMode === "teamImport") ||
      (page.id === "runtime" && !model_setup.runtimeReadyToProceed()) ||
      (page.id === "agents" && sharedState.state.teamSkillsValidationInFlight) ||
      (page.id === "review" && (!sharedState.state.configurationFinalized || sharedState.state.configurationFinalizing || sharedState.state.missingSecrets.length > 0));
    shellState.progressFill.style.width = Math.round(appearance.completionRatio() * 100) + "%";
    shellState.progressLabel.textContent = Math.round(appearance.completionRatio() * 100) + "% complete";
    renderSteps();
  }

function setPage(index, options) {
    var targetIndex = appearance.clamp(index, 0, sharedState.pages.length - 1);
    var allowFuture = Boolean(options && options.allowFuture);
    var markReached = Boolean(options && options.markReached);
    if (!allowFuture && targetIndex > sharedState.state.furthestPageIndex) {
      targetIndex = sharedState.state.furthestPageIndex;
    }
    sharedState.state.pageIndex = targetIndex;
    if (markReached && targetIndex > sharedState.state.furthestPageIndex) {
      sharedState.state.furthestPageIndex = targetIndex;
    }
    if (sharedState.pages[sharedState.state.pageIndex].id === "standby" && !sharedState.state.standbyStartedAt) {
      sharedState.state.standbyStartedAt = Date.now();
    }
    if (sharedState.pages[sharedState.state.pageIndex].id !== "standby") {
      sharedState.state.standbyStartedAt = 0;
    }
    window.localStorage.setItem("ua.preview.page", String(sharedState.state.pageIndex));
    window.localStorage.setItem("ua.preview.furthest_page", String(sharedState.state.furthestPageIndex));
    renderCurrentPage();
  }

function advanceToNextPage() {
    if (sharedState.state.pageIndex >= sharedState.pages.length - 1) {
      setPage(0);
      return;
    }
    setPage(sharedState.state.pageIndex + 1, {
      allowFuture: true,
      markReached: true,
    });
  }

lifecycle.listen(shellState.backButton, "click", function () {
    setPage(sharedState.state.pageIndex - 1);
  });

lifecycle.listen(sharedState.nextButton, "click", function () {
    var currentPage = sharedState.pages[sharedState.state.pageIndex];
    if (currentPage.id === "status") {
      finalization.openStatus();
      return;
    }
    if (currentPage.id === "runtime" && !model_setup.runtimeReadyToProceed()) {
      return;
    }
    if (currentPage.kind === "begin" && sharedState.state.beginMode === "teamImport") {
      return;
    }
    if (currentPage.id === "channels" && !validateCommunicationSettings()) {
      renderCurrentPage();
      return;
    }
    if (currentPage.id === "agents") {
      if (sharedState.state.teamSkillsValidationInFlight) {
        return;
      }
      validateTeamSkillReposBeforeNext().then(function (canProceed) {
        if (canProceed) {
          advanceToNextPage();
        }
      });
      return;
    }
    if (currentPage.id === "review") {
      if (!sharedState.state.configurationFinalized || sharedState.state.missingSecrets.length) {
        return;
      }
      finalization.launchFinalizedConfiguration();
      return;
    }
    advanceToNextPage();
  });

lifecycle.listen(window, "resize", function () {
    if (sharedState.state.colorMode === "light") {
      appearance.resizeCanvas();
    }
  });

loadBootstrapSettings();

renderCurrentPage();

if (sharedState.state.colorMode === "light") {
    appearance.resizeCanvas();
    appearance.draw(performance.now());
  }

// What the code after mounting needs; the page's state stays inside mountPage.
return { pastWelcome: sharedState.state.furthestPageIndex > 0 };
}
const mounted = mountPage();
// Opening onboarding plays the intro once per page load (ui_motion.js) until
// the person has gone past the Welcome step; after that the browser reopens
// the remembered step without it (owner's request 2026-10-09). Returning from
// the browser's back-forward cache does not play it. `?intro` in the address
// plays it on any step, for example to preview it.
var introRequested = new URLSearchParams(window.location.search).has("intro");
if ((introRequested || !mounted.pastWelcome) && window.UAMotion) {
  window.UAMotion.playIntro({
    shell: document.querySelector(".preview-shell"),
    focusTarget: document.getElementById("next-button"),
  });
}
window.addEventListener("pageshow", (event) => {
  if (event.persisted) mountPage();
});

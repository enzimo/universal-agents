/** Existing app configuration view behavior with explicit state and callback dependencies. */
export function createConfiguration({ sharedState, operations, lifecycle, requestClient }) {
function applyImportedSettings(settings, options) {
    var overwriteExisting = Boolean(options && options.overwriteExisting);
    var runtimeConnectionBeforeImport = operations.runtimeConnectionSignature();
    var changed = false;

    function shouldApply(storageKey) {
      return overwriteExisting || !window.localStorage.getItem(storageKey);
    }

    function applyString(settingKey, stateKey, storageKey) {
      if (settings[settingKey] === undefined || settings[settingKey] === null || settings[settingKey] === "") {
        return;
      }
      if (!shouldApply(storageKey)) {
        return;
      }
      var value = String(settings[settingKey]);
      if (sharedState.state[stateKey] !== value) {
        sharedState.state[stateKey] = value;
        window.localStorage.setItem(storageKey, value);
        changed = true;
      }
    }

    function applyNumber(settingKey, stateKey, storageKey, min, max) {
      if (settings[settingKey] === undefined || settings[settingKey] === null || settings[settingKey] === "") {
        return;
      }
      if (!shouldApply(storageKey)) {
        return;
      }
      var parsed = operations.clamp(parseInt(settings[settingKey], 10), min, max);
      if (sharedState.state[stateKey] !== parsed) {
        sharedState.state[stateKey] = parsed;
        window.localStorage.setItem(storageKey, String(parsed));
        changed = true;
      }
    }

    function applyOptionalNumber(settingKey, stateKey, storageKey, min, max) {
      if (!Object.prototype.hasOwnProperty.call(settings, settingKey)) {
        return;
      }
      if (!shouldApply(storageKey)) {
        return;
      }
      var rawValue = settings[settingKey];
      var value = "";
      if (rawValue !== null && rawValue !== "") {
        value = operations.clamp(parseInt(rawValue, 10), min, max);
      }
      if (sharedState.state[stateKey] !== value) {
        sharedState.state[stateKey] = value;
        if (value === "") {
          window.localStorage.removeItem(storageKey);
        } else {
          window.localStorage.setItem(storageKey, String(value));
        }
        changed = true;
      }
    }

    function applyNumeric(settingKey, stateKey, storageKey, min, max) {
      if (settings[settingKey] === undefined || settings[settingKey] === null || settings[settingKey] === "") {
        return;
      }
      if (!shouldApply(storageKey)) {
        return;
      }
      var parsed = operations.clamp(Number(settings[settingKey]), min, max);
      if (!Number.isFinite(parsed)) {
        return;
      }
      if (sharedState.state[stateKey] !== parsed) {
        sharedState.state[stateKey] = parsed;
        window.localStorage.setItem(storageKey, String(parsed));
        changed = true;
      }
    }

    function applyBoolean(settingKey, stateKey, storageKey) {
      if (settings[settingKey] === undefined || settings[settingKey] === null) {
        return;
      }
      if (!shouldApply(storageKey)) {
        return;
      }
      var value = Boolean(settings[settingKey]);
      if (sharedState.state[stateKey] !== value) {
        sharedState.state[stateKey] = value;
        window.localStorage.setItem(storageKey, value ? "true" : "false");
        changed = true;
      }
    }

    if (operations.applyAgentCatalog(settings.agent_catalog)) {
      changed = true;
    }

    if (Array.isArray(settings.team_skills_repositories) && shouldApply("ua.preview.team.skills.repos")) {
      var importedSkillRepos = settings.team_skills_repositories.map(function (value) {
        return String(value || "").trim();
      }).filter(Boolean);
      sharedState.state.teamSkillRepos = importedSkillRepos.length ? importedSkillRepos : [""];
      operations.persistTeamSkillRepos();
      changed = true;
    }

    if (settings.nodejs_available !== undefined) {
      var nodejsAvailable = Boolean(settings.nodejs_available);
      if (sharedState.state.nodejsAvailable !== nodejsAvailable) {
        sharedState.state.nodejsAvailable = nodejsAvailable;
        changed = true;
      }
      operations.refreshChannelAvailability();
    }

    if (settings.secrets_source && shouldApply(sharedState.SECRETS_FROM_ENVIRONMENT_STORAGE_KEY)) {
      var fromEnvironment = settings.secrets_source === sharedState.SECRETS_SOURCE_ENVIRONMENT;
      if (sharedState.state.secretsFromEnvironment !== fromEnvironment) {
        operations.setSecretsFromEnvironment(fromEnvironment);
        changed = true;
      }
    }

    applyString("network_public_origin", "networkPublicOrigin", "ua.preview.network.public_origin");
    applyNumber(
      "internal_website_bind_port",
      "internalWebsiteBindPort",
      "ua.preview.internal_website.bind_port",
      1,
      65535
    );
    if (Object.prototype.hasOwnProperty.call(settings, "internal_website_public_port")) {
      applyOptionalNumber(
        "internal_website_public_port",
        "internalWebsitePublicPort",
        "ua.preview.documentation.port",
        1,
        65535
      );
    } else {
      applyOptionalNumber(
        "documentation_port",
        "internalWebsitePublicPort",
        "ua.preview.documentation.port",
        1,
        65535
      );
    }
    applyString("internal_website_docs_root", "internalWebsiteDocsRoot", "ua.preview.internal_website.docs_root");
    applyString("internal_website_skills_dir", "internalWebsiteSkillsDir", "ua.preview.internal_website.skills_dir");
    applyString("team_id", "runtimeTeamId", "ua.preview.runtime.team.id");
    applyString("timezone", "runtimeTimezone", "ua.preview.runtime.timezone");
    applyString("user_display_name", "userDisplayName", "ua.onboarding.user.display_name");
    applyString("user_role", "userRole", "ua.onboarding.user.role");
    applyString("assistant_name", "assistantName", "ua.preview.assistant.name");
    applyString("model_provider", "runtimeProvider", "ua.preview.runtime.provider");
    applyString("model_endpoint", "runtimeEndpoint", "ua.preview.runtime.endpoint");
    applyString("model_id", "runtimeModel", "ua.preview.runtime.model");
    if (settings.openai_codex && typeof settings.openai_codex === "object" && shouldApply("ua.preview.runtime.openai_codex")) {
      sharedState.state.runtimeOpenAICodex = settings.openai_codex;
      window.localStorage.setItem("ua.preview.runtime.openai_codex", JSON.stringify(sharedState.state.runtimeOpenAICodex));
      changed = true;
    }
    if (settings.xai_oauth && typeof settings.xai_oauth === "object" && shouldApply("ua.preview.runtime.xai_oauth")) {
      sharedState.state.runtimeXAIOAuth = settings.xai_oauth;
      window.localStorage.setItem("ua.preview.runtime.xai_oauth", JSON.stringify(sharedState.state.runtimeXAIOAuth));
      changed = true;
    }
    applyString("reasoning_effort", "runtimeReasoningEffort", "ua.preview.runtime.reasoning_effort");
    applyString("reasoning_summary", "runtimeReasoningSummary", "ua.preview.runtime.reasoning_summary");
    applyString("litellm_api_mode", "runtimeLiteLLMApiMode", "ua.preview.runtime.litellm_api_mode");
    applyNumber("context_window_tokens", "runtimeContextWindowTokens", "ua.preview.runtime.context_window_tokens", 1024, 10000000);
    if (settings.stream !== undefined && shouldApply("ua.preview.runtime.stream")) {
      sharedState.state.runtimeStream = settings.stream === null ? "" : (settings.stream ? "true" : "false");
      window.localStorage.setItem("ua.preview.runtime.stream", sharedState.state.runtimeStream);
      changed = true;
    }
    applyBoolean("supports_image_input", "runtimeSupportsImages", "ua.preview.runtime.supports_image_input");
    applyBoolean("supports_audio_input", "runtimeSupportsAudio", "ua.preview.runtime.supports_audio_input");
    applyBoolean("supports_file_input", "runtimeSupportsFiles", "ua.preview.runtime.supports_file_input");
    if (settings.llm_failover && typeof settings.llm_failover === "object") {
      var failover = settings.llm_failover;
      if (shouldApply("ua.preview.failover.enabled")) {
        sharedState.state.llmFailoverEnabled = Boolean(failover.enabled);
        window.localStorage.setItem("ua.preview.failover.enabled", sharedState.state.llmFailoverEnabled ? "true" : "false");
        changed = true;
      }
      if (failover.switch_for && typeof failover.switch_for === "object") {
        settings.failover_min_turns = failover.switch_for.min_turns;
        settings.failover_min_seconds = failover.switch_for.min_seconds;
        applyNumber("failover_min_turns", "llmFailoverMinTurns", "ua.preview.failover.min_turns", 1, 999);
        applyNumber("failover_min_seconds", "llmFailoverMinSeconds", "ua.preview.failover.min_seconds", 1, 604800);
      }
      if (failover.demotion && typeof failover.demotion === "object") {
        settings.failover_failure_threshold = failover.demotion.failure_threshold;
        settings.failover_failure_window_seconds = failover.demotion.failure_window_seconds;
        settings.failover_demotion_seconds = failover.demotion.demotion_seconds;
        applyNumber("failover_failure_threshold", "llmFailoverFailureThreshold", "ua.preview.failover.failure_threshold", 1, 99);
        applyNumber("failover_failure_window_seconds", "llmFailoverFailureWindowSeconds", "ua.preview.failover.failure_window_seconds", 1, 604800);
        applyNumber("failover_demotion_seconds", "llmFailoverDemotionSeconds", "ua.preview.failover.demotion_seconds", 0, 31536000);
      }
      if (failover.notify_user !== undefined && shouldApply("ua.preview.failover.notify_user")) {
        sharedState.state.llmFailoverNotifyUser = Boolean(failover.notify_user);
        window.localStorage.setItem("ua.preview.failover.notify_user", sharedState.state.llmFailoverNotifyUser ? "true" : "false");
        changed = true;
      }
      if (Array.isArray(failover.fallbacks) && shouldApply("ua.preview.failover.fallbacks")) {
        sharedState.state.llmFailoverFallbacks = failover.fallbacks.map(function (fallback) {
          return {
            provider: String(fallback.provider || "litellm"),
            model_id: String(fallback.model_id || ""),
          };
        });
        window.localStorage.setItem("ua.preview.failover.fallbacks", JSON.stringify(sharedState.state.llmFailoverFallbacks));
        changed = true;
      }
    }
    if (settings.selected_agents && typeof settings.selected_agents === "object") {
      sharedState.agents.forEach(function (agent) {
        if (settings.selected_agents[agent[0]] === undefined || !shouldApply(operations.selectedAgentStorageKey(agent[0]))) {
          return;
        }
        sharedState.state.selectedAgents[agent[0]] = Boolean(settings.selected_agents[agent[0]]);
        window.localStorage.setItem(operations.selectedAgentStorageKey(agent[0]), sharedState.state.selectedAgents[agent[0]] ? "true" : "false");
        changed = true;
      });
    }
    if (settings.selected_agent_counts && typeof settings.selected_agent_counts === "object") {
      sharedState.agents.forEach(function (agent) {
        if (settings.selected_agent_counts[agent[0]] === undefined || !shouldApply(operations.selectedAgentCountStorageKey(agent[0]))) {
          return;
        }
        var count = operations.clamp(Number(settings.selected_agent_counts[agent[0]]), 0, 8);
        if (!Number.isFinite(count)) {
          return;
        }
        sharedState.state.selectedAgentCounts[agent[0]] = count;
        window.localStorage.setItem(operations.selectedAgentCountStorageKey(agent[0]), String(count));
        changed = true;
      });
    }
    applyBoolean("embedding_enabled", "embeddingEnabled", "ua.preview.embedding.enabled");
    if (settings.embedding && typeof settings.embedding === "object") {
      settings.embedding_provider = settings.embedding.provider;
      settings.embedding_model_id = settings.embedding.model_id;
      settings.embedding_api_base = settings.embedding.api_base;
      settings.embedding_dimensions = settings.embedding.dimensions;
      settings.embedding_timeout_seconds = settings.embedding.timeout_seconds;
      settings.embedding_organization = settings.embedding.organization;
    }
    applyString("embedding_provider", "embeddingProvider", "ua.preview.embedding.provider");
    applyString("embedding_model_id", "embeddingModelId", "ua.preview.embedding.model_id");
    applyString("embedding_api_base", "embeddingApiBase", "ua.preview.embedding.api_base");
    applyNumber("embedding_dimensions", "embeddingDimensions", "ua.preview.embedding.dimensions", 1, 100000);
    applyNumeric("embedding_timeout_seconds", "embeddingTimeoutSeconds", "ua.preview.embedding.timeout_seconds", 0, 600);
    applyString("embedding_organization", "embeddingOrganization", "ua.preview.embedding.organization");
    applyNumber("quiet_start", "quietHoursStart", "ua.preview.quiet.start", 0, 23);
    applyNumber("quiet_end", "quietHoursEnd", "ua.preview.quiet.end", 0, 23);
    applyBoolean("quiet_enabled", "quietHoursEnabled", "ua.preview.quiet.enabled");
    applyBoolean("voice_enabled", "voiceEnabled", "ua.preview.voice.enabled");
    if (settings.voice_response_mode && shouldApply("ua.preview.voice.response_mode")) {
      operations.setVoiceResponseMode(String(settings.voice_response_mode));
      changed = true;
    }

    sharedState.safetyChoices.forEach(function (choice) {
      if (settings[choice.key] !== undefined && shouldApply("ua.preview.safety." + choice.key)) {
        operations.setSafetyValue(choice.key, Boolean(settings[choice.key]));
        changed = true;
      }
    });

    if (settings.security_layer && typeof settings.security_layer === "object") {
      var security = settings.security_layer;
      settings.secure_tool_broker_internal_base_url = security.broker_internal_base_url;
      settings.secure_tool_broker_public_port = security.broker_public_port;
      settings.secure_tool_broker_bind_host = security.broker_bind_host;
      settings.secure_tool_broker_bind_port = security.broker_bind_port;
      settings.secure_tool_broker_credentials_dir = security.broker_credentials_dir;
      // The mode has no control on these pages, so the configuration's value always applies.
      if (security.mode && sharedState.state.securityLayerMode !== String(security.mode)) {
        sharedState.state.securityLayerMode = String(security.mode);
        changed = true;
      }
      applyString("secure_tool_broker_internal_base_url", "secureToolBrokerInternalBaseUrl", "ua.preview.security.broker_internal_base_url");
      applyNumber("secure_tool_broker_public_port", "secureToolBrokerPublicPort", "ua.preview.security.broker_public_port", 1, 65535);
      applyString("secure_tool_broker_bind_host", "secureToolBrokerBindHost", "ua.preview.security.broker_bind_host");
      applyNumber("secure_tool_broker_bind_port", "secureToolBrokerBindPort", "ua.preview.security.broker_bind_port", 1, 65535);
      applyString("secure_tool_broker_credentials_dir", "secureToolBrokerCredentialsDir", "ua.preview.security.broker_credentials_dir");
      if (security.credential_state_backend) {
        settings.credential_state_backend_mode = security.credential_state_backend.mode;
        settings.credential_state_backend_source = security.credential_state_backend.source;
        settings.credential_state_backend_namespace = security.credential_state_backend.namespace;
        settings.credential_state_backend_fallback_to_files = security.credential_state_backend.fallback_to_files;
        applyString("credential_state_backend_mode", "credentialStateBackendMode", "ua.preview.security.credential_state_backend.mode");
        applyString("credential_state_backend_source", "credentialStateBackendSource", "ua.preview.security.credential_state_backend.source");
        applyString("credential_state_backend_namespace", "credentialStateBackendNamespace", "ua.preview.security.credential_state_backend.namespace");
        applyBoolean("credential_state_backend_fallback_to_files", "credentialStateBackendFallbackToFiles", "ua.preview.security.credential_state_backend.fallback_to_files");
      }
      if (security.credential_catalog) {
        settings.credential_catalog_enabled = security.credential_catalog.enabled;
        settings.credential_catalog_database_path = security.credential_catalog.database_path;
        settings.credential_catalog_template_overrides_path = security.credential_catalog.template_overrides_path;
        settings.credential_catalog_default_write_source = security.credential_catalog.default_write_source;
        settings.credential_catalog_collection_session_ttl_seconds = security.credential_catalog.collection_session_ttl_seconds;
        settings.credential_catalog_secret_cache_ttl_seconds = security.credential_catalog.secret_cache_ttl_seconds;
        applyBoolean("credential_catalog_enabled", "credentialCatalogEnabled", "ua.preview.security.credential_catalog.enabled");
        applyString("credential_catalog_database_path", "credentialCatalogDatabasePath", "ua.preview.security.credential_catalog.database_path");
        applyString("credential_catalog_template_overrides_path", "credentialCatalogTemplateOverridesPath", "ua.preview.security.credential_catalog.template_overrides_path");
        applyString("credential_catalog_default_write_source", "credentialCatalogDefaultWriteSource", "ua.preview.security.credential_catalog.default_write_source");
        applyNumber("credential_catalog_collection_session_ttl_seconds", "credentialCatalogCollectionSessionTtlSeconds", "ua.preview.security.credential_catalog.collection_session_ttl_seconds", 1, 86400);
        applyNumber("credential_catalog_secret_cache_ttl_seconds", "credentialCatalogSecretCacheTtlSeconds", "ua.preview.security.credential_catalog.secret_cache_ttl_seconds", 0, 86400);
      }
      settings.bitwarden_server_url = security.bitwarden_server_url;
      applyString("bitwarden_server_url", "bitwardenServerUrl", "ua.preview.security.bitwarden_server_url");
      if (security.sealbox) {
        settings.sealbox_enabled = security.sealbox.enabled;
        settings.sealbox_manage_service = security.sealbox.manage_service;
        settings.sealbox_auto_initialize = security.sealbox.auto_initialize;
        settings.sealbox_bind_host = security.sealbox.bind_host;
        settings.sealbox_bind_port = security.sealbox.bind_port;
        settings.sealbox_store_path = security.sealbox.store_path;
        settings.sealbox_service_url = security.sealbox.service_url;
        settings.sealbox_root_token_file = security.sealbox.root_token_file;
        settings.sealbox_tenant_material_dir = security.sealbox.tenant_material_dir;
        settings.sealbox_ready_timeout_seconds = security.sealbox.ready_timeout_seconds;
        applyBoolean("sealbox_enabled", "sealboxEnabled", "ua.preview.security.sealbox.enabled");
        applyBoolean("sealbox_manage_service", "sealboxManageService", "ua.preview.security.sealbox.manage_service");
        applyBoolean("sealbox_auto_initialize", "sealboxAutoInitialize", "ua.preview.security.sealbox.auto_initialize");
        applyString("sealbox_bind_host", "sealboxBindHost", "ua.preview.security.sealbox.bind_host");
        applyNumber("sealbox_bind_port", "sealboxBindPort", "ua.preview.security.sealbox.bind_port", 1, 65535);
        applyString("sealbox_store_path", "sealboxStorePath", "ua.preview.security.sealbox.store_path");
        applyString("sealbox_service_url", "sealboxServiceUrl", "ua.preview.security.sealbox.service_url");
        applyString("sealbox_root_token_file", "sealboxRootTokenFile", "ua.preview.security.sealbox.root_token_file");
        applyString("sealbox_tenant_material_dir", "sealboxTenantMaterialDir", "ua.preview.security.sealbox.tenant_material_dir");
        applyNumber("sealbox_ready_timeout_seconds", "sealboxReadyTimeoutSeconds", "ua.preview.security.sealbox.ready_timeout_seconds", 1, 300);
      }
    }

    if (settings.activity_collector && typeof settings.activity_collector === "object") {
      var activity = settings.activity_collector;
      settings.activity_base_url = activity.base_url;
      settings.activity_connect_timeout_seconds = activity.connect_timeout_seconds;
      settings.activity_request_timeout_seconds = activity.request_timeout_seconds;
      settings.activity_batch_max_events = activity.batch_max_events;
      settings.activity_flush_interval_seconds = activity.flush_interval_seconds;
      settings.activity_max_buffered_events = activity.max_buffered_events;
      settings.activity_drop_when_unavailable = activity.drop_when_unavailable;
      settings.activity_bind_host = activity.bind_host;
      settings.activity_bind_port = activity.bind_port;
      settings.activity_user_url_path = activity.user_url_path;
      settings.activity_admin_url_path = activity.admin_url_path;
      settings.activity_otel_trace_base_url = activity.otel_trace_base_url;
      settings.activity_ingest_token_env_var = activity.ingest_token_env_var;
      if (activity.storage) {
        settings.activity_storage_backend = activity.storage.backend;
        settings.activity_storage_sqlite_path = activity.storage.sqlite && activity.storage.sqlite.path;
      }
      applyString("activity_base_url", "activityCollectorBaseUrl", "ua.preview.activity.base_url");
      applyNumeric("activity_connect_timeout_seconds", "activityCollectorConnectTimeoutSeconds", "ua.preview.activity.connect_timeout_seconds", 0.001, 60);
      applyNumeric("activity_request_timeout_seconds", "activityCollectorRequestTimeoutSeconds", "ua.preview.activity.request_timeout_seconds", 0.001, 120);
      applyNumber("activity_batch_max_events", "activityCollectorBatchMaxEvents", "ua.preview.activity.batch_max_events", 1, 1000);
      applyNumeric("activity_flush_interval_seconds", "activityCollectorFlushIntervalSeconds", "ua.preview.activity.flush_interval_seconds", 0.001, 60);
      applyNumber("activity_max_buffered_events", "activityCollectorMaxBufferedEvents", "ua.preview.activity.max_buffered_events", 1, 100000);
      applyBoolean("activity_drop_when_unavailable", "activityCollectorDropWhenUnavailable", "ua.preview.activity.drop_when_unavailable");
      applyString("activity_bind_host", "activityCollectorBindHost", "ua.preview.activity.bind_host");
      applyNumber("activity_bind_port", "activityCollectorBindPort", "ua.preview.activity.bind_port", 1, 65535);
      applyString("activity_user_url_path", "activityCollectorUserUrlPath", "ua.preview.activity.user_url_path");
      applyString("activity_admin_url_path", "activityCollectorAdminUrlPath", "ua.preview.activity.admin_url_path");
      applyString("activity_otel_trace_base_url", "activityCollectorOtelTraceBaseUrl", "ua.preview.activity.otel_trace_base_url");
      applyString("activity_ingest_token_env_var", "activityCollectorIngestTokenEnvVar", "ua.preview.activity.ingest_token_env_var");
      applyString("activity_storage_backend", "activityCollectorStorageBackend", "ua.preview.activity.storage.backend");
      applyString("activity_storage_sqlite_path", "activityCollectorStorageSqlitePath", "ua.preview.activity.storage.sqlite.path");
    }

    if (settings.default_delivery_channel) {
      operations.setDefaultChannel(String(settings.default_delivery_channel) === "website" ? "website" : String(settings.default_delivery_channel));
      changed = true;
    }
    [
      ["telegram_enabled", "telegram"],
      ["discord_enabled", "discord"],
      ["signal_enabled", "signal"],
      ["photon_enabled", "photon"],
      ["whatsapp_enabled", "whatsapp"],
      ["web_chat_enabled", "website"],
    ].forEach(function (entry) {
      if (settings[entry[0]] !== undefined && shouldApply("ua.preview.channel.enabled." + entry[1])) {
        operations.setChannelEnabled(entry[1], Boolean(settings[entry[0]]));
        changed = true;
      }
    });
    if (Array.isArray(settings.telegram_allowed_user_ids) && settings.telegram_allowed_user_ids.length) {
      operations.setChannelFieldValue("telegram", "Allowed user IDs", settings.telegram_allowed_user_ids.join(", "));
      changed = true;
    }
    if (Array.isArray(settings.telegram_allowed_chat_ids) && settings.telegram_allowed_chat_ids.length) {
      operations.setChannelFieldValue("telegram", "Allowed chat IDs", settings.telegram_allowed_chat_ids.join(", "));
      changed = true;
    }
    if (settings.telegram_manager_offline_policy) {
      operations.setChannelFieldValue("telegram", "Offline policy", String(settings.telegram_manager_offline_policy));
      changed = true;
    }
    if (
      settings.telegram_rich_messages !== undefined &&
      shouldApply("ua.onboarding.channel." + operations.channelFieldKey("telegram", "Rich messages"))
    ) {
      operations.setChannelFieldValue("telegram", "Rich messages", settings.telegram_rich_messages ? "true" : "false");
      changed = true;
    }
    if (Array.isArray(settings.discord_allowed_user_ids) && settings.discord_allowed_user_ids.length) {
      operations.setChannelFieldValue("discord", "Allowed user IDs", settings.discord_allowed_user_ids.join(", "));
      changed = true;
    }
    if (Array.isArray(settings.discord_allowed_channel_ids) && settings.discord_allowed_channel_ids.length) {
      operations.setChannelFieldValue("discord", "Allowed channel IDs", settings.discord_allowed_channel_ids.join(", "));
      changed = true;
    }
    if (settings.discord_group_policy) {
      operations.setChannelFieldValue("discord", "Group policy", String(settings.discord_group_policy));
      changed = true;
    }
    if (settings.signal_base_url) {
      operations.setChannelFieldValue("signal", "REST API URL", String(settings.signal_base_url));
      changed = true;
    }
    if (settings.signal_account) {
      operations.setChannelFieldValue("signal", "Account", String(settings.signal_account));
      changed = true;
    }
    if (Array.isArray(settings.signal_allowed_sender_ids) && settings.signal_allowed_sender_ids.length) {
      operations.setChannelFieldValue("signal", "Allowed sender IDs", settings.signal_allowed_sender_ids.join(", "));
      changed = true;
    }
    if (Array.isArray(settings.signal_allowed_group_ids) && settings.signal_allowed_group_ids.length) {
      operations.setChannelFieldValue("signal", "Allowed group IDs", settings.signal_allowed_group_ids.join(", "));
      changed = true;
    }
    if (settings.signal_group_policy) {
      operations.setChannelFieldValue("signal", "Group policy", String(settings.signal_group_policy));
      changed = true;
    }
    if (Array.isArray(settings.signal_mention_markers) && settings.signal_mention_markers.length) {
      operations.setChannelFieldValue("signal", "Mention markers", settings.signal_mention_markers.join(", "));
      changed = true;
    }
    if (settings.photon_project_id) {
      operations.setChannelFieldValue("photon", "Project ID", String(settings.photon_project_id));
      changed = true;
    }
    if (Array.isArray(settings.photon_allowed_sender_ids) && settings.photon_allowed_sender_ids.length) {
      operations.setChannelFieldValue("photon", "Allowed sender IDs", settings.photon_allowed_sender_ids.join(", "));
      changed = true;
    }
    if (Array.isArray(settings.photon_allowed_group_ids) && settings.photon_allowed_group_ids.length) {
      operations.setChannelFieldValue("photon", "Allowed group IDs", settings.photon_allowed_group_ids.join(", "));
      changed = true;
    }
    if (settings.photon_group_policy) {
      operations.setChannelFieldValue("photon", "Group policy", String(settings.photon_group_policy));
      changed = true;
    }
    if (Array.isArray(settings.photon_mention_markers) && settings.photon_mention_markers.length) {
      operations.setChannelFieldValue("photon", "Mention markers", settings.photon_mention_markers.join(", "));
      changed = true;
    }
    if (Array.isArray(settings.whatsapp_allowed_sender_ids) && settings.whatsapp_allowed_sender_ids[0]) {
      operations.setChannelFieldValue("whatsapp", "Allowed sender id", String(settings.whatsapp_allowed_sender_ids[0]));
      changed = true;
    }
    if (settings.whatsapp_webhook_path) {
      operations.setChannelFieldValue("whatsapp", "Webhook path", String(settings.whatsapp_webhook_path));
      changed = true;
    }
    if (sharedState.state.networkPublicOrigin) {
      operations.setChannelFieldValue("website", "Public IP/URL", sharedState.state.networkPublicOrigin);
    }

    if (settings.appearance && typeof settings.appearance === "object" && (overwriteExisting || !window.localStorage.getItem("ua.preview.polygon.line.color"))) {
      operations.applyAppearanceSettings(settings.appearance);
      changed = true;
    }
    if (changed && operations.runtimeConnectionSignature() !== runtimeConnectionBeforeImport) {
      operations.invalidateRuntimeTest();
    }
    return changed;
  }
return { applyImportedSettings };
}

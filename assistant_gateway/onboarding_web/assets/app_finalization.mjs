/** Existing app finalization view behavior with explicit state and callback dependencies. */
export function createFinalization({ finalizationState, sharedState, operations, lifecycle, requestClient }) {
function reviewWarnings() {
    var warnings = [];
    if (sharedState.state.voiceEnabled && !sharedState.state.secretValues.xai_api_key) {
      warnings.push("Voice is enabled, but the xAI Voice API Key is empty. Voice will not work until that key is configured.");
    }
    return warnings;
  }

function renderDownloadButtons(disabled) {
    var downloads = document.createElement("div");
    downloads.className = "download-row";
    var label = document.createElement("span");
    label.textContent = "Download:";
    var configButton = document.createElement("button");
    configButton.type = "button";
    configButton.className = "button runtime-secondary download-button";
    configButton.textContent = "instance_config.yaml";
    configButton.disabled = Boolean(disabled);
    lifecycle.listen(configButton, "click", function () {
      downloadRuntimeFile("/download/instance_config.yaml");
    });
    var envButton = document.createElement("button");
    envButton.type = "button";
    envButton.className = "button runtime-secondary download-button";
    envButton.textContent = "team.env";
    envButton.disabled = Boolean(disabled);
    lifecycle.listen(envButton, "click", function () {
      downloadRuntimeFile("/download/team.env");
    });
    downloads.appendChild(label);
    downloads.appendChild(configButton);
    if (!sharedState.state.secretsFromEnvironment) {
      downloads.appendChild(envButton);
    }
    return downloads;
  }

function renderReviewSummary() {
    var selectedAgentNames = sharedState.agents.filter(function (agent) {
      return Boolean(sharedState.state.selectedAgents[agent[0]]);
    }).map(function (agent) {
      if (operations.isTeamArchitect(agent)) {
        return operations.agentDisplayName(agent);
      }
      var count = Number(sharedState.state.selectedAgentCounts[agent[0]] || agent[3] || 1);
      return operations.agentDisplayName(agent) + (count > 1 ? " x" + count : "");
    });
    var enabledChannels = sharedState.channelDefinitions.filter(function (channel) {
      return Boolean(sharedState.state.enabledChannels[channel.key]);
    }).map(function (channel) {
      return channel.label;
    });
    var summary = [
      ["Assistant", sharedState.state.assistantName || "Lisa"],
      ["User role", sharedState.state.userRole || "not set"],
      ["Instance", sharedState.state.runtimeInstanceName || operations.composedInstanceName()],
      ["Team id", sharedState.state.runtimeTeamId || operations.composedTeamId()],
      ["Timezone", sharedState.state.runtimeTimezone],
      ["Provider", sharedState.state.runtimeProvider + " / " + (sharedState.state.runtimeModel || "model not set")],
      ["Default communication", sharedState.state.defaultChannel || "website"],
      ["Channels", enabledChannels.join(", ") || "Web Chat"],
      ["Voice", sharedState.state.voiceEnabled ? sharedState.state.voiceResponseMode.replace(/_/g, " ") : "disabled"],
      ["Team", selectedAgentNames.join(", ")],
      ["Team skills repos", operations.teamSkillRepoUrls().join(", ") || "None"],
      ["Accounts", operations.accountSharingSummary()],
    ];
    var card = document.createElement("section");
    card.className = "review-summary";
    summary.forEach(function (item) {
      var row = document.createElement("div");
      var label = document.createElement("span");
      label.textContent = item[0];
      var value = document.createElement("strong");
      value.textContent = item[1];
      row.appendChild(label);
      row.appendChild(value);
      card.appendChild(row);
    });
    return card;
  }

function configurationStatusClassName() {
    var className = "runtime-test-status configuration-status";
    if (sharedState.state.configurationStatusKind === "success") {
      className += " is-success";
    }
    if (sharedState.state.configurationStatusKind === "error") {
      className += " is-error";
    }
    if (sharedState.state.configurationStatusKind === "warning") {
      className += " is-warning";
    }
    return className;
  }

function renderReview() {
    var stack = document.createElement("div");
    stack.className = "review-stack";
    var warnings = reviewWarnings();
    if (warnings.length) {
      var warningBox = document.createElement("div");
      warningBox.className = "warning-callout review-warning";
      var icon = document.createElement("span");
      icon.className = "warning-icon";
      icon.textContent = "!";
      var list = document.createElement("ul");
      warnings.forEach(function (warning) {
        var item = document.createElement("li");
        item.textContent = warning;
        list.appendChild(item);
      });
      warningBox.appendChild(icon);
      warningBox.appendChild(list);
      stack.appendChild(warningBox);
    }
    if (sharedState.state.missingSecrets.length) {
      stack.appendChild(renderMissingSecrets(sharedState.state.missingSecrets));
    }
    stack.appendChild(renderReviewSummary());
    stack.appendChild(renderDownloadButtons(!sharedState.state.configurationFinalized || sharedState.state.configurationFinalizing));
    var finalizeButton = document.createElement("button");
    finalizeButton.type = "button";
    finalizeButton.className = "button primary finalize-button";
    finalizeButton.textContent = sharedState.state.configurationFinalizing
      ? "Finalizing..."
      : (sharedState.state.configurationFinalized ? "Regenerate Configuration" : "Finalize Configuration");
    finalizeButton.disabled = sharedState.state.configurationFinalizing;
    lifecycle.listen(finalizeButton, "click", function () {
      finalizeConfiguration();
    });
    stack.appendChild(finalizeButton);
    if (sharedState.state.configurationStatus) {
      var status = document.createElement("p");
      status.className = sharedState.state.configurationStatusKind === "success" ? "configuration-note" : configurationStatusClassName();
      status.textContent = sharedState.state.configurationStatus;
      stack.appendChild(status);
    }
    return stack;
  }

function renderMissingSecrets(missing) {
    var box = document.createElement("div");
    box.className = "warning-callout missing-secrets";
    box.setAttribute("role", "alert");
    var icon = document.createElement("span");
    icon.className = "warning-icon";
    icon.textContent = "!";
    var body = document.createElement("div");
    var intro = document.createElement("p");
    intro.textContent = "These secrets are missing from the environment. Add each one as a Docker secret "
      + "(or environment variable) and restart the container; the team then starts. Launch stays off until then.";
    var list = document.createElement("ul");
    missing.forEach(function (item) {
      var entry = document.createElement("li");
      var name = document.createElement("code");
      name.textContent = String(item.name || "");
      var detail = document.createElement("span");
      detail.textContent = " " + String(item.purpose || "") + " " + String(item.how_to_create || "")
        + " Set " + String(item.name || "") + ", or " + String(item.name || "") + "_FILE to a mounted secret file.";
      entry.appendChild(name);
      entry.appendChild(detail);
      list.appendChild(entry);
    });
    body.appendChild(intro);
    body.appendChild(list);
    box.appendChild(icon);
    box.appendChild(body);
    return box;
  }

function environmentSecretsSummary(payload) {
    if (payload.secrets_source !== sharedState.SECRETS_SOURCE_ENVIRONMENT) {
      return "";
    }
    var lines = ["Secrets come from the environment, so no team.env was written."];
    if (payload.source_env_ignored) {
      lines.push("The .env file you chose was not saved; it only filled in the form.");
    }
    if (Array.isArray(payload.unsaved_secret_names) && payload.unsaved_secret_names.length) {
      lines.push("Values you typed for " + payload.unsaved_secret_names.join(", ") + " were not saved; the environment must provide them.");
    }
    return lines.join("\n");
  }

function renderStandby() {
    var meter = document.createElement("div");
    meter.className = "standby-meter";
    meter.setAttribute("aria-busy", "true");
    var elapsed = sharedState.state.standbyStartedAt ? Date.now() - sharedState.state.standbyStartedAt : 0;
    var messageIndex = operations.clamp(Math.floor(elapsed / 1700), 0, finalizationState.standbyMessages.length - 1);
    var width = operations.clamp((elapsed / 6500) * 100, 8, 100);
    var statusRow = document.createElement("div");
    statusRow.className = "standby-status-row";
    var spinner = document.createElement("span");
    spinner.className = "standby-spinner";
    spinner.setAttribute("aria-hidden", "true");
    var status = document.createElement("p");
    status.className = "standby-status";
    status.id = "standby-status";
    status.setAttribute("role", "status");
    status.textContent = finalizationState.standbyMessages[messageIndex];
    var track = document.createElement("div");
    track.className = "progress-track";
    var fill = document.createElement("div");
    fill.className = "progress-fill";
    fill.style.width = width.toFixed(0) + "%";
    statusRow.appendChild(spinner);
    statusRow.appendChild(status);
    track.appendChild(fill);
    meter.appendChild(statusRow);
    meter.appendChild(track);
    pollRuntimeAfterRestart();
    lifecycle.timeout(operations.renderCurrentPage, 900);
    return meter;
  }

function textLooksLikeHtml(text) {
    var normalized = String(text || "").trim().slice(0, 80).toLowerCase();
    return normalized.indexOf("<!doctype") === 0 || normalized.indexOf("<html") === 0 || normalized.indexOf("<!doctype html") >= 0;
  }

function responseErrorMessage(text, fallback) {
    if (textLooksLikeHtml(text)) {
      return fallback;
    }
    try {
      var payload = JSON.parse(String(text || ""));
      if (payload && payload.message) {
        return String(payload.message).trim().slice(0, 360) || fallback;
      }
    } catch (error) {
      // Non-JSON response bodies fall through to the plain text fallback.
    }
    return String(text || "").trim().slice(0, 240) || fallback;
  }

function finalizationErrorMessage(text, fallback) {
    if (textLooksLikeHtml(text)) {
      return fallback;
    }
    try {
      var payload = JSON.parse(String(text || ""));
      var message = payload && payload.message
        ? String(payload.message).trim().slice(0, 360)
        : fallback;
      var details = payload && Array.isArray(payload.details)
        ? payload.details.map(function (detail) {
          return String(detail || "").trim().slice(0, 360);
        }).filter(Boolean)
        : [];
      return details.length ? message + "\n- " + details.join("\n- ") : message;
    } catch (error) {
      return responseErrorMessage(text, fallback);
    }
  }

function providerTestHttpErrorMessage(response, text) {
    var status = response && response.status ? "HTTP " + response.status : "an unknown HTTP status";
    var fallback = "The onboarding connection-test endpoint returned " + status + " without a usable JSON error. Check that the reverse proxy forwards POST /api/provider-test to the internal website service and that the deployed frontend and backend use the same image version.";
    return responseErrorMessage(text, fallback);
  }

function renderStatus() {
    var stack = document.createElement("div");
    stack.className = "status-stack";
    var note = document.createElement("p");
    note.className = "status-docs-note";
    note.textContent = "Sign in to see your team status.";
    stack.appendChild(note);
    return stack;
  }

function openStatus() {
    window.location.href = operations.webChatUrl() + "?view=status";
  }

function optionalNumber(value) {
    if (value === "" || value === null || value === undefined) {
      return null;
    }
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

function numberOrDefault(value, fallback) {
    var parsed = optionalNumber(value);
    return parsed === null ? fallback : parsed;
  }

function optionalBooleanSelect(value) {
    if (value === "true") {
      return true;
    }
    if (value === "false") {
      return false;
    }
    return null;
  }

function currentFailoverSettings() {
    return {
      enabled: Boolean(sharedState.state.llmFailoverEnabled),
      switch_for: {
        min_turns: numberOrDefault(sharedState.state.llmFailoverMinTurns, 3),
        min_seconds: numberOrDefault(sharedState.state.llmFailoverMinSeconds, 900),
      },
      demotion: {
        failure_threshold: numberOrDefault(sharedState.state.llmFailoverFailureThreshold, 3),
        failure_window_seconds: numberOrDefault(sharedState.state.llmFailoverFailureWindowSeconds, 3600),
        demotion_seconds: numberOrDefault(sharedState.state.llmFailoverDemotionSeconds, 86400),
      },
      notify_user: Boolean(sharedState.state.llmFailoverNotifyUser),
      fallbacks: sharedState.state.llmFailoverFallbacks.filter(function (fallback) {
        return fallback && fallback.provider && fallback.model_id;
      }).map(function (fallback) {
        return {
          provider: fallback.provider,
          model_id: fallback.model_id,
        };
      }),
    };
  }

function currentSecurityLayerSettings() {
    return {
      mode: sharedState.state.securityLayerMode,
      broker_internal_base_url: sharedState.state.secureToolBrokerInternalBaseUrl || null,
      broker_public_port: optionalNumber(sharedState.state.secureToolBrokerPublicPort),
      broker_bind_host: sharedState.state.secureToolBrokerBindHost || null,
      broker_bind_port: optionalNumber(sharedState.state.secureToolBrokerBindPort),
      broker_credentials_dir: sharedState.state.secureToolBrokerCredentialsDir || null,
      credential_state_backend: {
        mode: sharedState.state.credentialStateBackendMode,
        source: sharedState.state.credentialStateBackendSource || null,
        namespace: sharedState.state.credentialStateBackendNamespace || "team-cred-store",
        fallback_to_files: Boolean(sharedState.state.credentialStateBackendFallbackToFiles),
      },
      credential_catalog: {
        enabled: Boolean(sharedState.state.credentialCatalogEnabled),
        database_path: sharedState.state.credentialCatalogDatabasePath || "credential_catalog.db",
        template_overrides_path: sharedState.state.credentialCatalogTemplateOverridesPath || "credential_template_overrides.yaml",
        default_write_source: sharedState.state.credentialCatalogDefaultWriteSource || "sealbox",
        collection_session_ttl_seconds: numberOrDefault(sharedState.state.credentialCatalogCollectionSessionTtlSeconds, 900),
        secret_cache_ttl_seconds: numberOrDefault(sharedState.state.credentialCatalogSecretCacheTtlSeconds, 300),
      },
      bitwarden_server_url: sharedState.state.bitwardenServerUrl || null,
      sealbox: {
        enabled: Boolean(sharedState.state.sealboxEnabled),
        manage_service: Boolean(sharedState.state.sealboxManageService),
        auto_initialize: Boolean(sharedState.state.sealboxAutoInitialize),
        bind_host: sharedState.state.sealboxBindHost || "127.0.0.1",
        bind_port: numberOrDefault(sharedState.state.sealboxBindPort, 8767),
        service_url: sharedState.state.sealboxServiceUrl || null,
        store_path: sharedState.state.sealboxStorePath || "dbs/sealbox.db",
        root_token_file: sharedState.state.sealboxRootTokenFile || "sealbox/root_token",
        tenant_material_dir: sharedState.state.sealboxTenantMaterialDir || "sealbox/tenants",
        api_version: "v2",
        ready_timeout_seconds: numberOrDefault(sharedState.state.sealboxReadyTimeoutSeconds, 30),
      },
    };
  }

function currentActivityCollectorSettings() {
    return {
      base_url: sharedState.state.activityCollectorBaseUrl || null,
      connect_timeout_seconds: numberOrDefault(sharedState.state.activityCollectorConnectTimeoutSeconds, 1),
      request_timeout_seconds: numberOrDefault(sharedState.state.activityCollectorRequestTimeoutSeconds, 2),
      batch_max_events: numberOrDefault(sharedState.state.activityCollectorBatchMaxEvents, 25),
      flush_interval_seconds: numberOrDefault(sharedState.state.activityCollectorFlushIntervalSeconds, 1),
      max_buffered_events: numberOrDefault(sharedState.state.activityCollectorMaxBufferedEvents, 500),
      drop_when_unavailable: Boolean(sharedState.state.activityCollectorDropWhenUnavailable),
      bind_host: sharedState.state.activityCollectorBindHost || "127.0.0.1",
      bind_port: numberOrDefault(sharedState.state.activityCollectorBindPort, 8765),
      user_url_path: sharedState.state.activityCollectorUserUrlPath || "/user",
      admin_url_path: sharedState.state.activityCollectorAdminUrlPath || "/admin",
      otel_trace_base_url: sharedState.state.activityCollectorOtelTraceBaseUrl || null,
      ingest_token_env_var: sharedState.state.activityCollectorIngestTokenEnvVar || "ACTIVITY_COLLECTOR_INGEST_TOKEN",
      storage: {
        backend: sharedState.state.activityCollectorStorageBackend || "sqlite",
        sqlite: {
          path: sharedState.state.activityCollectorStorageSqlitePath || "dbs/activity_collector.db",
        },
        postgres: {},
      },
    };
  }

function downloadRuntimeFile(url) {
    var link = document.createElement("a");
    link.href = url;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

function summarizeSkillsRepositoryCompletion(results) {
    if (!Array.isArray(results) || !results.length) {
      return "";
    }
    var imported = results.filter(function (result) {
      return result.ok && result.copied;
    });
    var warnings = results.filter(function (result) {
      return !result.ok;
    });
    var missing = results.filter(function (result) {
      return result.ok && !result.copied;
    });
    var lines = [];
    if (imported.length) {
      lines.push("Imported skills from " + imported.length + " skills repository" + (imported.length === 1 ? "" : "ies") + ".");
    }
    missing.forEach(function (result) {
      lines.push("No SKILL.md beneath a directory named skills was found in " + result.url + ".");
    });
    warnings.forEach(function (result) {
      lines.push("Could not import " + result.url + ": " + (result.message || "Repository could not be reached."));
    });
    return lines.join("\n");
  }

function buildOnboardingPayload() {
    var appearance = operations.currentAppearanceSettings();
    var telegramUserIds = operations.commaSeparatedFieldValues("telegram", "Allowed user IDs");
    var telegramChatIds = operations.commaSeparatedFieldValues("telegram", "Allowed chat IDs");
    var discordUserIds = operations.commaSeparatedFieldValues("discord", "Allowed user IDs");
    var discordChannelIds = operations.commaSeparatedFieldValues("discord", "Allowed channel IDs");
    var signalSenderIds = operations.commaSeparatedFieldValues("signal", "Allowed sender IDs");
    var signalGroupIds = operations.commaSeparatedFieldValues("signal", "Allowed group IDs");
    var signalMentionMarkers = operations.commaSeparatedFieldValues("signal", "Mention markers");
    var photonSenderIds = operations.commaSeparatedFieldValues("photon", "Allowed sender IDs");
    var photonGroupIds = operations.commaSeparatedFieldValues("photon", "Allowed group IDs");
    var photonMentionMarkers = operations.commaSeparatedFieldValues("photon", "Mention markers");
    var whatsappSenderId = operations.getChannelFieldValue("whatsapp", "Allowed sender id");
    var defaultChatId = "";
    if (sharedState.state.defaultChannel === "telegram") {
      defaultChatId = telegramChatIds[0] || "";
    } else if (sharedState.state.defaultChannel === "discord") {
      defaultChatId = discordChannelIds[0] || "";
    } else if (sharedState.state.defaultChannel === "whatsapp") {
      defaultChatId = whatsappSenderId;
    } else if (sharedState.state.defaultChannel === "signal") {
      defaultChatId = signalGroupIds[0] || signalSenderIds[0] || "";
    } else if (sharedState.state.defaultChannel === "photon") {
      defaultChatId = photonGroupIds[0] || photonSenderIds[0] || "";
    } else if (sharedState.state.defaultChannel === "website") {
      defaultChatId = "web-chat";
    }
    return {
      instance_name: sharedState.state.runtimeInstanceName || operations.composedInstanceName(),
      secrets_source: sharedState.state.secretsFromEnvironment
        ? sharedState.SECRETS_SOURCE_ENVIRONMENT
        : sharedState.SECRETS_SOURCE_ENV_FILE,
      team_id: sharedState.state.runtimeTeamId || operations.composedTeamId(),
      timezone: sharedState.state.runtimeTimezone,
      user_display_name: sharedState.state.userDisplayName || "Ada",
      user_role: sharedState.state.userRole || "",
      assistant_name: sharedState.state.assistantName || "Lisa",
      assistant_purpose: sharedState.state.assistantPurpose || finalizationState.assistantPurposeDefault,
      assistant_response_style: sharedState.state.assistantResponseStyle || "Concise and direct.",
      default_delivery_channel: sharedState.state.defaultChannel === "website" ? "website" : sharedState.state.defaultChannel,
      default_delivery_chat_id: defaultChatId,
      quiet_enabled: sharedState.state.quietHoursEnabled,
      quiet_start: sharedState.state.quietHoursStart,
      quiet_end: sharedState.state.quietHoursEnd,
      model_provider: sharedState.state.runtimeProvider,
      model_endpoint: sharedState.state.runtimeEndpoint,
      model_token: operations.runtimeTokenForSelectedProvider(),
      model_id: sharedState.state.runtimeModel,
      openai_codex: sharedState.state.runtimeOpenAICodex,
      xai_oauth: operations.currentXAIOAuthSettings(),
      reasoning_effort: sharedState.state.runtimeReasoningEffort,
      reasoning_summary: sharedState.state.runtimeReasoningSummary,
      litellm_api_mode: sharedState.state.runtimeLiteLLMApiMode,
      context_window_tokens: optionalNumber(sharedState.state.runtimeContextWindowTokens),
      stream: optionalBooleanSelect(sharedState.state.runtimeStream),
      supports_image_input: Boolean(sharedState.state.runtimeSupportsImages),
      supports_audio_input: Boolean(sharedState.state.runtimeSupportsAudio),
      supports_file_input: Boolean(sharedState.state.runtimeSupportsFiles),
      llm_failover: currentFailoverSettings(),
      embedding_enabled: Boolean(sharedState.state.embeddingEnabled),
      embedding_provider: sharedState.state.embeddingProvider,
      embedding_model_id: sharedState.state.embeddingModelId,
      embedding_api_base: sharedState.state.embeddingApiBase,
      embedding_api_key: sharedState.state.embeddingApiKey,
      embedding_dimensions: optionalNumber(sharedState.state.embeddingDimensions),
      embedding_timeout_seconds: optionalNumber(sharedState.state.embeddingTimeoutSeconds),
      embedding_organization: sharedState.state.embeddingOrganization,
      security_layer: currentSecurityLayerSettings(),
      security_layer_mode: sharedState.state.securityLayerMode,
      secure_tool_broker_internal_base_url: sharedState.state.secureToolBrokerInternalBaseUrl,
      secure_tool_broker_public_port: optionalNumber(sharedState.state.secureToolBrokerPublicPort),
      secure_tool_broker_bind_host: sharedState.state.secureToolBrokerBindHost,
      secure_tool_broker_bind_port: optionalNumber(sharedState.state.secureToolBrokerBindPort),
      secure_tool_broker_credentials_dir: sharedState.state.secureToolBrokerCredentialsDir,
      activity_collector: currentActivityCollectorSettings(),
      generate_internal_credentials: Boolean(sharedState.state.safetyValues.generate_internal_credentials),
      source_env_filename: sharedState.state.sourceEnvFilename,
      source_env_content: sharedState.state.sourceEnvContent,
      voice_enabled: Boolean(sharedState.state.voiceEnabled),
      voice_response_mode: sharedState.state.voiceResponseMode,
      public_origin: sharedState.state.networkPublicOrigin,
      internal_website_bind_host: "0.0.0.0",
      internal_website_bind_port: sharedState.state.internalWebsiteBindPort,
      internal_website_public_port: optionalNumber(sharedState.state.internalWebsitePublicPort),
      internal_website_docs_root: sharedState.state.internalWebsiteDocsRoot,
      internal_website_skills_dir: sharedState.state.internalWebsiteSkillsDir,
      telegram_enabled: Boolean(sharedState.state.enabledChannels.telegram),
      telegram_bot_token: operations.getChannelFieldValue("telegram", "Bot token"),
      telegram_allowed_user_ids: telegramUserIds,
      telegram_allowed_chat_ids: telegramChatIds,
      telegram_manager_offline_policy: operations.channelSelectPayloadValue("Offline policy", operations.getChannelFieldValue("telegram", "Offline policy")),
      telegram_rich_messages: operations.getChannelToggleValue("telegram", "Rich messages", true),
      discord_enabled: Boolean(sharedState.state.enabledChannels.discord),
      discord_bot_token: operations.getChannelFieldValue("discord", "Bot token"),
      discord_allowed_user_ids: discordUserIds,
      discord_allowed_channel_ids: discordChannelIds,
      discord_group_policy: operations.channelSelectPayloadValue("Group policy", operations.getChannelFieldValue("discord", "Group policy")),
      signal_enabled: Boolean(sharedState.state.enabledChannels.signal),
      signal_base_url: operations.getChannelFieldValue("signal", "REST API URL") || "http://127.0.0.1:8080",
      signal_account: operations.getChannelFieldValue("signal", "Account"),
      signal_allowed_sender_ids: signalSenderIds,
      signal_allowed_group_ids: signalGroupIds,
      signal_group_policy: operations.channelSelectPayloadValue("Group policy", operations.getChannelFieldValue("signal", "Group policy")),
      signal_mention_markers: signalMentionMarkers,
      photon_enabled: Boolean(sharedState.state.enabledChannels.photon),
      photon_project_id: operations.getChannelFieldValue("photon", "Project ID"),
      photon_project_secret: operations.getChannelFieldValue("photon", "Project secret"),
      photon_allowed_sender_ids: photonSenderIds,
      photon_allowed_group_ids: photonGroupIds,
      photon_group_policy: operations.channelSelectPayloadValue("Group policy", operations.getChannelFieldValue("photon", "Group policy")),
      photon_mention_markers: photonMentionMarkers,
      whatsapp_enabled: Boolean(sharedState.state.enabledChannels.whatsapp),
      whatsapp_allowed_sender_ids: whatsappSenderId ? [whatsappSenderId] : [],
      whatsapp_webhook_path: operations.getChannelFieldValue("whatsapp", "Webhook path") || "/webhooks/whatsapp",
      web_chat_auth_token: sharedState.state.websiteAuthToken,
      account_sharing: operations.accountSharingPayload(),
      exa_api_key: sharedState.state.secretValues.exa_api_key || "",
      brave_search_api_key: sharedState.state.secretValues.brave_search_api_key || "",
      xai_api_key: sharedState.state.secretValues.xai_api_key || "",
      crawl4ai_mcp_server_url: sharedState.state.advancedEnvValues.crawl4ai_mcp_server_url || "",
      postgres_dsn: sharedState.state.advancedEnvValues.postgres_dsn || "",
      searxng_base_url: sharedState.state.advancedEnvValues.searxng_base_url || "",
      searxng_verify_tls: sharedState.state.advancedEnvValues.searxng_verify_tls || "",
      searxng_timeout_seconds: sharedState.state.advancedEnvValues.searxng_timeout_seconds || "",
      searxng_default_num_results: sharedState.state.advancedEnvValues.searxng_default_num_results || "",
      team_skills_repositories: operations.teamSkillRepoUrls(),
      appearance: appearance,
      selected_agents: sharedState.state.selectedAgents,
      selected_agent_counts: sharedState.state.selectedAgentCounts,
    };
  }

function finalizeConfiguration() {
    if (sharedState.state.configurationFinalizing) {
      return Promise.resolve();
    }
    if (!operations.validateCommunicationSettings()) {
      sharedState.state.configurationStatus = operations.communicationValidationText() + "\nReturn to Communication and finish the highlighted channel setup.";
      sharedState.state.configurationStatusKind = "error";
      operations.renderCurrentPage();
      return Promise.resolve();
    }
    var wasFinalized = sharedState.state.configurationFinalized;
    sharedState.state.configurationFinalizing = true;
    var files = sharedState.state.secretsFromEnvironment ? "instance_config.yaml" : "instance_config.yaml and team.env";
    sharedState.state.configurationStatus = wasFinalized
      ? "Regenerating " + files + "..."
      : "Writing " + files + "...";
    sharedState.state.configurationStatusKind = "pending";
    operations.renderCurrentPage();
    return requestClient.fetch("/api/onboarding/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign(buildOnboardingPayload(), { finalize_only: true })),
    }).then(function (response) {
      if (!response.ok) {
        return response.text().then(function (text) {
          throw new Error(finalizationErrorMessage(text, "Configuration finalization failed"));
        });
      }
      return response.json();
    }).then(function (payload) {
      var skillsSummary = summarizeSkillsRepositoryCompletion(payload.skills_repos);
      var secretsSummary = environmentSecretsSummary(payload);
      sharedState.state.missingSecrets = Array.isArray(payload.missing_secrets) ? payload.missing_secrets : [];
      sharedState.state.unsavedSecretNames = Array.isArray(payload.unsaved_secret_names) ? payload.unsaved_secret_names : [];
      sharedState.state.configurationFinalized = true;
      sharedState.state.configurationFinalizing = false;
      sharedState.state.configurationStatus = (sharedState.state.missingSecrets.length
        ? "Configuration finalized. Add the missing secrets listed above before the team can start."
        : "Configuration finalized. Download your files, then launch your team.")
        + (secretsSummary ? "\n" + secretsSummary : "")
        + (skillsSummary ? "\n" + skillsSummary : "");
      sharedState.state.configurationStatusKind = sharedState.state.missingSecrets.length
        || (skillsSummary && skillsSummary.indexOf("Could not import ") >= 0) ? "warning" : "success";
      operations.renderCurrentPage();
    }).catch(function (error) {
      sharedState.state.configurationFinalizing = false;
      sharedState.state.configurationFinalized = wasFinalized;
      sharedState.state.configurationStatus = error.message || "Configuration finalization failed.";
      sharedState.state.configurationStatusKind = "error";
      operations.renderCurrentPage();
    });
  }

function launchFinalizedConfiguration() {
    sharedState.nextButton.disabled = true;
    sharedState.nextButton.textContent = "Launching...";
    return requestClient.fetch("/api/onboarding/restart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }).catch(function () {
      return null;
    }).then(function () {
      sharedState.state.standbyStartedAt = Date.now();
      operations.setPage(sharedState.pages.findIndex(function (page) { return page.id === "standby"; }), {
        allowFuture: true,
        markReached: true,
      });
    });
  }

function pollRuntimeAfterRestart() {
    if (sharedState.state.runtimePollInFlight) {
      return;
    }
    sharedState.state.runtimePollInFlight = true;
    requestClient.fetch("/health", { cache: "no-store" }).then(function (response) {
      if (!response.ok) {
        throw new Error("runtime not ready");
      }
      return response.json();
    }).then(function (health) {
      if (health && health.service === "onboard-web") {
        return;
      }
      operations.setPage(sharedState.pages.length - 1, {
        allowFuture: true,
        markReached: true,
      });
    }).catch(function () {
      return null;
    }).finally(function () {
      sharedState.state.runtimePollInFlight = false;
    });
  }
return { buildOnboardingPayload, configurationStatusClassName, environmentSecretsSummary, currentActivityCollectorSettings, currentFailoverSettings, currentSecurityLayerSettings, downloadRuntimeFile, finalizationErrorMessage, finalizeConfiguration, launchFinalizedConfiguration, numberOrDefault, optionalBooleanSelect, optionalNumber, openStatus, pollRuntimeAfterRestart, providerTestHttpErrorMessage, renderDownloadButtons, renderMissingSecrets, renderReview, renderReviewSummary, renderStandby, renderStatus, responseErrorMessage, reviewWarnings, summarizeSkillsRepositoryCompletion, textLooksLikeHtml };
}

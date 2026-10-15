/** Existing app model_setup view behavior with explicit state and callback dependencies. */
export function createModelSetup({ model_setupState, sharedState, operations, lifecycle, requestClient }) {
function providerEnvVar(providerLabel) {
    var provider = providerByLabel(providerLabel);
    if (provider.key === "openai") {
      return "OPENAI_API_KEY";
    }
    if (provider.key === "openai-codex") {
      return "OPENAI_CODEX_ACCESS_TOKEN";
    }
    if (provider.key === "xai-oauth") {
      return "";
    }
    if (provider.key === "anthropic") {
      return "ANTHROPIC_API_KEY";
    }
    if (provider.key === "google-ai") {
      return "GEMINI_API_KEY";
    }
    return "LITELLM_API_KEY";
  }

function setRuntimeTokenValue(value) {
    sharedState.state.runtimeToken = value;
    window.localStorage.removeItem("ua.preview.runtime.token");
    invalidateRuntimeTest();
  }

function setEmbeddingApiKeyValue(value) {
    sharedState.state.embeddingApiKey = value;
    window.localStorage.removeItem("ua.preview.embedding.api_key");
  }

function setWebsiteAuthTokenValue(value) {
    sharedState.state.websiteAuthToken = value;
    window.localStorage.removeItem("ua.preview.website.auth.token");
  }

function providerByLabel(label) {
    var normalized = String(label || "").trim().toLowerCase().replace(/_/g, "-");
    return model_setupState.providerDefinitions.find(function (provider) {
      return (
        provider.label === label ||
        provider.key === normalized ||
        provider.label.toLowerCase() === normalized ||
        (provider.key === "openai-codex" && [
          "codex",
          "openai codex",
          "openai codex oauth",
          "openai subscription",
        ].indexOf(normalized) !== -1) ||
        (provider.key === "xai-oauth" && [
          "x-ai-oauth",
          "grok-oauth",
          "xai-grok-oauth",
          "xai grok oauth",
        ].indexOf(normalized) !== -1)
      );
    }) || model_setupState.providerDefinitions[model_setupState.providerDefinitions.length - 1];
  }

function setRuntimeProvider(label) {
    var provider = providerByLabel(label);
    sharedState.state.runtimeProvider = provider.label;
    sharedState.state.runtimeEndpoint = provider.endpoint;
    if (provider.key === "xai-oauth") {
      setRuntimeTokenValue("");
    }
    if (provider.defaultModel && (provider.key === "xai-oauth" || !sharedState.state.runtimeModel.trim())) {
      sharedState.state.runtimeModel = provider.defaultModel;
      window.localStorage.setItem("ua.preview.runtime.model", sharedState.state.runtimeModel);
    }
    invalidateRuntimeTest();
    window.localStorage.setItem("ua.preview.runtime.provider", sharedState.state.runtimeProvider);
    window.localStorage.setItem("ua.preview.runtime.endpoint", sharedState.state.runtimeEndpoint);
  }

function runtimeTokenRequired() {
    return !providerByLabel(sharedState.state.runtimeProvider).tokenOptional;
  }

function runtimeTestCanRun() {
    return Boolean(
      sharedState.state.runtimeEndpoint.trim() &&
      (!runtimeTokenRequired() || sharedState.state.runtimeToken.trim()) &&
      sharedState.state.runtimeModel.trim()
    );
  }

function currentXAIOAuthSettings() {
    var settings = Object.assign({}, sharedState.state.runtimeXAIOAuth || {});
    if (providerByLabel(sharedState.state.runtimeProvider).key === "xai-oauth") {
      settings.base_url = sharedState.state.runtimeEndpoint.trim() || "https://api.x.ai/v1";
    } else {
      settings.base_url = String(settings.base_url || "https://api.x.ai/v1").trim();
    }
    return settings;
  }

function runtimeTokenForSelectedProvider() {
    if (providerByLabel(sharedState.state.runtimeProvider).key === "xai-oauth") {
      return "";
    }
    return sharedState.state.runtimeToken.trim();
  }

function runtimeTestPayload() {
    return {
      provider: sharedState.state.runtimeProvider,
      endpoint: sharedState.state.runtimeEndpoint.trim(),
      token: runtimeTokenForSelectedProvider(),
      model: sharedState.state.runtimeModel.trim(),
      reasoning_effort: sharedState.state.runtimeReasoningEffort,
      reasoning_summary: sharedState.state.runtimeReasoningSummary,
      litellm_api_mode: sharedState.state.runtimeLiteLLMApiMode,
      stream: operations.optionalBooleanSelect(sharedState.state.runtimeStream),
      xai_oauth: currentXAIOAuthSettings(),
    };
  }

function runtimeConnectionSignature() {
    return JSON.stringify({
      provider: sharedState.state.runtimeProvider,
      endpoint: sharedState.state.runtimeEndpoint.trim(),
      token: runtimeTokenForSelectedProvider(),
      model: sharedState.state.runtimeModel.trim(),
    });
  }

function runtimeReadyToProceed() {
    return Boolean(
      runtimeTestCanRun() &&
      sharedState.state.runtimeTestSucceeded &&
      sharedState.state.runtimeTestedConnectionSignature === runtimeConnectionSignature()
    );
  }

function runtimeStatusText() {
    return sharedState.state.runtimeTestStatus || "Please test your connection to proceed.";
  }

function runtimeStatusClassName() {
    var className = "runtime-test-status";
    if (sharedState.state.runtimeTestStatusKind === "success") {
      return className + " is-success";
    }
    if (sharedState.state.runtimeTestStatusKind === "error") {
      return className + " is-error";
    }
    return className;
  }

function formatTeamSkillsValidationWarning(results) {
    var failures = results.filter(function (result) {
      return !result.ok;
    });
    if (!failures.length) {
      return "";
    }
    return "Some skills repositories could not be reached:\n" + failures.map(function (result) {
      return "- " + result.url + ": " + (result.message || "Repository could not be reached.");
    }).join("\n");
  }

function updateRuntimeNavigation() {
    if (sharedState.nextButton && sharedState.pages[sharedState.state.pageIndex] && sharedState.pages[sharedState.state.pageIndex].id === "runtime") {
      sharedState.nextButton.disabled = !runtimeReadyToProceed();
    }
  }

function invalidateRuntimeTest() {
    sharedState.state.runtimeTestSucceeded = false;
    sharedState.state.runtimeTestedConnectionSignature = "";
    sharedState.state.runtimeTestStatus = "";
    sharedState.state.runtimeTestStatusKind = "pending";
    updateRuntimeNavigation();
  }

function renderRuntimeField(labelText, input) {
    var card = document.createElement("div");
    card.className = "shim-card field";
    var label = document.createElement("label");
    label.textContent = labelText;
    label.htmlFor = input.id;
    card.appendChild(label);
    card.appendChild(input);
    return card;
  }

function renderRuntimeSecretField(labelText, input) {
    var card = document.createElement("div");
    card.className = "shim-card field";
    var label = document.createElement("label");
    label.textContent = labelText;
    label.htmlFor = input.id;
    card.appendChild(label);
    card.appendChild(operations.renderRevealableControl(input));
    return card;
  }

function persistAdvancedValue(key, stateKey, value) {
    sharedState.state[stateKey] = value;
    if (value === "" || value === null || value === undefined) {
      window.localStorage.removeItem(key);
    } else if (typeof value === "boolean") {
      window.localStorage.setItem(key, value ? "true" : "false");
    } else {
      window.localStorage.setItem(key, String(value));
    }
  }

function persistFailoverFallbacks() {
    sharedState.state.llmFailoverFallbacks = sharedState.state.llmFailoverFallbacks.filter(function (fallback) {
      return fallback && (fallback.provider || fallback.model_id);
    });
    window.localStorage.setItem("ua.preview.failover.fallbacks", JSON.stringify(sharedState.state.llmFailoverFallbacks));
  }

function renderFailoverFallbacks() {
    var section = document.createElement("div");
    section.className = "advanced-subsection";
    var header = document.createElement("div");
    header.className = "advanced-subsection-header";
    var title = document.createElement("strong");
    title.textContent = "Failover models";
    var addButton = document.createElement("button");
    addButton.type = "button";
    addButton.className = "button runtime-secondary advanced-add-button";
    addButton.textContent = "+";
    addButton.setAttribute("aria-label", "Add failover model");
    lifecycle.listen(addButton, "click", function () {
      sharedState.state.llmFailoverFallbacks.push({ provider: "litellm", model_id: "" });
      persistFailoverFallbacks();
      operations.renderCurrentPage();
    });
    header.appendChild(title);
    header.appendChild(addButton);
    section.appendChild(header);
    if (!sharedState.state.llmFailoverFallbacks.length) {
      var empty = document.createElement("p");
      empty.className = "advanced-empty";
      empty.textContent = "No fallback models configured yet.";
      section.appendChild(empty);
    }
    sharedState.state.llmFailoverFallbacks.forEach(function (fallback, index) {
      var row = document.createElement("div");
      row.className = "advanced-fallback-row";
      var providerCard = operations.advancedSelect(
        "Provider",
        fallback.provider || "litellm",
        [
          ["litellm", "LiteLLM"],
          ["openai", "OpenAI"],
          ["openai-codex", "OpenAI Codex OAuth"],
          ["xai-oauth", "xAI Grok OAuth"],
          ["anthropic", "Anthropic"],
          ["gemini", "Gemini"],
        ],
        "ua.preview.failover.fallback." + index + ".provider",
        null,
        function () {
          fallback.provider = row.querySelector("select").value;
          persistFailoverFallbacks();
        }
      );
      var modelInput = document.createElement("input");
      modelInput.type = "text";
      modelInput.className = "input";
      modelInput.value = fallback.model_id || "";
      modelInput.placeholder = "Fallback model id";
      lifecycle.listen(modelInput, "input", function () {
        fallback.model_id = modelInput.value;
        persistFailoverFallbacks();
      });
      var modelCard = renderRuntimeField("Model id", modelInput);
      var removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "button runtime-secondary advanced-remove-button";
      removeButton.textContent = "Remove";
      lifecycle.listen(removeButton, "click", function () {
        sharedState.state.llmFailoverFallbacks.splice(index, 1);
        persistFailoverFallbacks();
        operations.renderCurrentPage();
      });
      row.appendChild(providerCard);
      row.appendChild(modelCard);
      row.appendChild(removeButton);
      section.appendChild(row);
    });
    return section;
  }

function renderRuntimeAdvanced() {
    return operations.renderAdvancedSection(
      "ua.preview.runtime.advanced.open",
      "Advanced Setup",
      "Embedding, model behavior, and opt-in failover settings.",
      function (body) {
        var llmGrid = operations.renderAdvancedGrid();
        llmGrid.appendChild(operations.advancedInput(
          "Reasoning effort",
          "text",
          sharedState.state.runtimeReasoningEffort,
          "ua.preview.runtime.reasoning_effort",
          "runtimeReasoningEffort",
          {
            placeholder: "Provider default; e.g. low, medium, high, max",
            suggestions: ["low", "medium", "high", "max"],
          }
        ));
        llmGrid.appendChild(operations.advancedSelect("Reasoning summary", sharedState.state.runtimeReasoningSummary, [
          ["", "Off"],
          ["auto", "Auto"],
          ["concise", "Concise"],
          ["detailed", "Detailed"],
        ], "ua.preview.runtime.reasoning_summary", "runtimeReasoningSummary"));
        llmGrid.appendChild(operations.advancedSelect("LiteLLM API mode", sharedState.state.runtimeLiteLLMApiMode, [
          ["chat_completions", "Chat Completions"],
          ["responses", "Responses"],
        ], "ua.preview.runtime.litellm_api_mode", "runtimeLiteLLMApiMode"));
        llmGrid.appendChild(operations.advancedInput("Context window tokens", "number", sharedState.state.runtimeContextWindowTokens, "ua.preview.runtime.context_window_tokens", "runtimeContextWindowTokens", { min: 1024, placeholder: "220000" }));
        llmGrid.appendChild(operations.advancedSelect("Stream responses", sharedState.state.runtimeStream, [
          ["", "Provider default"],
          ["true", "On"],
          ["false", "Off"],
        ], "ua.preview.runtime.stream", "runtimeStream"));
        body.appendChild(llmGrid);

        var embeddingGrid = operations.renderAdvancedGrid();
        embeddingGrid.appendChild(operations.advancedToggle("Use embeddings", sharedState.state.embeddingEnabled, "ua.preview.embedding.enabled", "embeddingEnabled"));
        embeddingGrid.appendChild(operations.advancedSelect("Embedding provider", sharedState.state.embeddingProvider, [
          ["local", "Local deterministic"],
          ["openai_compatible", "OpenAI compatible"],
        ], "ua.preview.embedding.provider", "embeddingProvider"));
        embeddingGrid.appendChild(operations.advancedInput("Embedding model id", "text", sharedState.state.embeddingModelId, "ua.preview.embedding.model_id", "embeddingModelId", { placeholder: "harrier-oss-v1-0.6b" }));
        embeddingGrid.appendChild(operations.advancedInput("Embedding API base", "url", sharedState.state.embeddingApiBase, "ua.preview.embedding.api_base", "embeddingApiBase", { placeholder: "https://provider.example/v1" }));
        embeddingGrid.appendChild(operations.advancedSecretInput("Embedding API key", sharedState.state.embeddingApiKey, "ua.preview.embedding.api_key", "embeddingApiKey", { placeholder: "EMBEDDING_API_KEY" }));
        embeddingGrid.appendChild(operations.advancedInput("Embedding dimensions", "number", sharedState.state.embeddingDimensions, "ua.preview.embedding.dimensions", "embeddingDimensions", { min: 1, placeholder: "48" }));
        embeddingGrid.appendChild(operations.advancedInput("Embedding timeout seconds", "number", sharedState.state.embeddingTimeoutSeconds, "ua.preview.embedding.timeout_seconds", "embeddingTimeoutSeconds", { min: 0, placeholder: "10" }));
        embeddingGrid.appendChild(operations.advancedInput("Embedding organization", "text", sharedState.state.embeddingOrganization, "ua.preview.embedding.organization", "embeddingOrganization", { placeholder: "Optional organization id" }));
        body.appendChild(embeddingGrid);

        var failoverGrid = operations.renderAdvancedGrid();
        failoverGrid.appendChild(operations.advancedToggle("Enable model failover (off by default)", sharedState.state.llmFailoverEnabled, "ua.preview.failover.enabled", "llmFailoverEnabled"));
        failoverGrid.appendChild(operations.advancedInput("Minimum fallback turns", "number", sharedState.state.llmFailoverMinTurns, "ua.preview.failover.min_turns", "llmFailoverMinTurns", { min: 1 }));
        failoverGrid.appendChild(operations.advancedInput("Minimum fallback seconds", "number", sharedState.state.llmFailoverMinSeconds, "ua.preview.failover.min_seconds", "llmFailoverMinSeconds", { min: 1 }));
        failoverGrid.appendChild(operations.advancedInput("Failure threshold", "number", sharedState.state.llmFailoverFailureThreshold, "ua.preview.failover.failure_threshold", "llmFailoverFailureThreshold", { min: 1 }));
        failoverGrid.appendChild(operations.advancedInput("Failure window seconds", "number", sharedState.state.llmFailoverFailureWindowSeconds, "ua.preview.failover.failure_window_seconds", "llmFailoverFailureWindowSeconds", { min: 1 }));
        failoverGrid.appendChild(operations.advancedInput("Demotion seconds", "number", sharedState.state.llmFailoverDemotionSeconds, "ua.preview.failover.demotion_seconds", "llmFailoverDemotionSeconds", { min: 0 }));
        failoverGrid.appendChild(operations.advancedToggle("Notify user on model switch", sharedState.state.llmFailoverNotifyUser, "ua.preview.failover.notify_user", "llmFailoverNotifyUser"));
        body.appendChild(failoverGrid);
        body.appendChild(renderFailoverFallbacks());
      }
    );
  }

function renderCodexProviderSetup() {
    var panel = document.createElement("div");
    panel.className = "runtime-provider-setup";

    var title = document.createElement("strong");
    title.textContent = "Codex CLI login required";
    panel.appendChild(title);

    var copy = document.createElement("p");
    copy.appendChild(document.createTextNode("Install "));
    var link = document.createElement("a");
    link.className = "copy-link";
    link.href = model_setupState.CODEX_CLI_DOCS_URL;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = "Codex CLI";
    copy.appendChild(link);
    copy.appendChild(document.createTextNode(", then run "));
    var loginCode = document.createElement("code");
    loginCode.textContent = "codex login";
    copy.appendChild(loginCode);
    copy.appendChild(document.createTextNode(" and choose ChatGPT sign-in. For terminal-only hosts, run "));
    var deviceCode = document.createElement("code");
    deviceCode.textContent = "codex login --device-auth";
    copy.appendChild(deviceCode);
    copy.appendChild(document.createTextNode(". Leave the token field blank after login, or paste "));
    var tokenCode = document.createElement("code");
    tokenCode.textContent = "OPENAI_CODEX_ACCESS_TOKEN";
    copy.appendChild(tokenCode);
    copy.appendChild(document.createTextNode(" for a one-off test."));
    panel.appendChild(copy);

    return panel;
  }

function stopXAIDeviceLoginTimers() {
    if (sharedState.state.xaiDevicePollTimer) {
      lifecycle.clearTimeout(sharedState.state.xaiDevicePollTimer);
      sharedState.state.xaiDevicePollTimer = 0;
    }
    if (sharedState.state.xaiDeviceCountdownTimer) {
      lifecycle.clearTimeout(sharedState.state.xaiDeviceCountdownTimer);
      sharedState.state.xaiDeviceCountdownTimer = 0;
    }
  }

function formatXAICountdown() {
    var remaining = Math.max(0, Math.ceil(sharedState.state.xaiDeviceExpiresAt - Date.now() / 1000));
    var minutes = Math.floor(remaining / 60);
    var seconds = remaining % 60;
    return minutes + ":" + String(seconds).padStart(2, "0");
  }

function scheduleXAICountdown() {
    if (!sharedState.state.xaiDeviceLoginId || sharedState.state.xaiDeviceLoginStatus !== "pending") {
      return;
    }
    if (sharedState.state.xaiDeviceCountdownTimer) {
      lifecycle.clearTimeout(sharedState.state.xaiDeviceCountdownTimer);
      sharedState.state.xaiDeviceCountdownTimer = 0;
    }
    var countdown = document.getElementById("xai-oauth-countdown");
    if (countdown) {
      countdown.textContent = formatXAICountdown();
    }
    if (sharedState.state.xaiDeviceExpiresAt <= Date.now() / 1000) {
      return;
    }
    sharedState.state.xaiDeviceCountdownTimer = lifecycle.timeout(scheduleXAICountdown, 1000);
  }

function applyXAIDeviceLoginPayload(payload) {
    sharedState.state.xaiDeviceLoginStatus = String(payload.status || "error");
    sharedState.state.xaiDeviceLoginMessage = String(payload.message || "");
    if (payload.login_id) {
      sharedState.state.xaiDeviceLoginId = String(payload.login_id);
    }
    if (payload.verification_uri) {
      sharedState.state.xaiDeviceVerificationUri = String(payload.verification_uri);
    }
    if (payload.verification_uri_complete) {
      sharedState.state.xaiDeviceVerificationUriComplete = String(payload.verification_uri_complete);
    }
    if (payload.user_code) {
      sharedState.state.xaiDeviceUserCode = String(payload.user_code);
    }
    if (payload.expires_at_epoch_seconds) {
      sharedState.state.xaiDeviceExpiresAt = Number(payload.expires_at_epoch_seconds);
    }
    if (payload.interval) {
      sharedState.state.xaiDevicePollIntervalSeconds = Math.max(1, Number(payload.interval));
    }
  }

function readXAIDeviceResponse(response) {
    return response.text().then(function (text) {
      var payload = {};
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch (error) {
          payload = { message: operations.responseErrorMessage(text, "xAI OAuth endpoint is unavailable.") };
        }
      }
      if (!response.ok) {
        throw new Error(payload.message || response.statusText || "xAI OAuth request failed.");
      }
      return payload;
    });
  }

function scheduleXAIDeviceLoginPoll() {
    if (!sharedState.state.xaiDeviceLoginId || sharedState.state.xaiDeviceLoginStatus !== "pending") {
      return;
    }
    if (sharedState.state.xaiDevicePollTimer) {
      lifecycle.clearTimeout(sharedState.state.xaiDevicePollTimer);
    }
    sharedState.state.xaiDevicePollTimer = lifecycle.timeout(function () {
      sharedState.state.xaiDevicePollTimer = 0;
      requestClient.fetch("/api/xai-oauth/device-login/" + encodeURIComponent(sharedState.state.xaiDeviceLoginId), {
        cache: "no-store",
      }).then(readXAIDeviceResponse).then(function (payload) {
        applyXAIDeviceLoginPayload(payload);
        if (sharedState.state.xaiDeviceLoginStatus === "complete") {
          stopXAIDeviceLoginTimers();
          invalidateRuntimeTest();
          sharedState.state.runtimeTestStatus = "xAI OAuth login succeeded. Test the connection to continue.";
          sharedState.state.runtimeTestStatusKind = "pending";
        }
        operations.renderCurrentPage();
        if (sharedState.state.xaiDeviceLoginStatus === "pending") {
          scheduleXAIDeviceLoginPoll();
          scheduleXAICountdown();
        }
      }).catch(function (error) {
        stopXAIDeviceLoginTimers();
        sharedState.state.xaiDeviceLoginStatus = "error";
        sharedState.state.xaiDeviceLoginMessage = error.message || "Could not check xAI OAuth login status.";
        operations.renderCurrentPage();
      });
    }, Math.max(1000, sharedState.state.xaiDevicePollIntervalSeconds * 1000));
  }

function startXAIDeviceLogin(openedWindow) {
    stopXAIDeviceLoginTimers();
    sharedState.state.xaiDeviceLoginStatus = "starting";
    sharedState.state.xaiDeviceLoginMessage = "Requesting an xAI verification code...";
    sharedState.state.xaiDeviceLoginId = "";
    sharedState.state.xaiDeviceVerificationUri = "";
    sharedState.state.xaiDeviceVerificationUriComplete = "";
    sharedState.state.xaiDeviceUserCode = "";
    sharedState.state.xaiDeviceExpiresAt = 0;
    invalidateRuntimeTest();
    operations.renderCurrentPage();
    requestClient.fetch("/api/xai-oauth/device-login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ xai_oauth: currentXAIOAuthSettings() }),
    }).then(readXAIDeviceResponse).then(function (payload) {
      applyXAIDeviceLoginPayload(payload);
      if (openedWindow && !openedWindow.closed) {
        openedWindow.location = sharedState.state.xaiDeviceVerificationUriComplete || sharedState.state.xaiDeviceVerificationUri;
      }
      operations.renderCurrentPage();
      scheduleXAIDeviceLoginPoll();
      scheduleXAICountdown();
    }).catch(function (error) {
      if (openedWindow && !openedWindow.closed) {
        openedWindow.close();
      }
      sharedState.state.xaiDeviceLoginStatus = "error";
      sharedState.state.xaiDeviceLoginMessage = error.message || "Could not start xAI OAuth login.";
      operations.renderCurrentPage();
    });
  }

function renderXAIOAuthProviderSetup() {
    var panel = document.createElement("div");
    panel.className = "runtime-provider-setup xai-oauth-setup";

    var title = document.createElement("strong");
    title.textContent = "Sign in with xAI";
    panel.appendChild(title);

    var copy = document.createElement("p");
    copy.textContent = "Authorize this Universal Agents instance with your xAI account. Access and refresh tokens stay on the onboarding server and are never returned to this browser.";
    panel.appendChild(copy);

    if (sharedState.state.xaiDeviceVerificationUriComplete) {
      var verification = document.createElement("p");
      verification.appendChild(document.createTextNode("Open "));
      var link = document.createElement("a");
      link.className = "copy-link";
      link.href = sharedState.state.xaiDeviceVerificationUriComplete;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = "the xAI authorization page";
      verification.appendChild(link);
      verification.appendChild(document.createTextNode(" and enter code "));
      var code = document.createElement("code");
      code.textContent = sharedState.state.xaiDeviceUserCode;
      verification.appendChild(code);
      if (sharedState.state.xaiDeviceLoginStatus === "pending") {
        verification.appendChild(document.createTextNode(". Time remaining: "));
        var countdown = document.createElement("strong");
        countdown.id = "xai-oauth-countdown";
        countdown.textContent = formatXAICountdown();
        verification.appendChild(countdown);
      }
      panel.appendChild(verification);
    }

    if (sharedState.state.xaiDeviceLoginMessage) {
      var status = document.createElement("p");
      status.className = "xai-oauth-login-status" + (
        sharedState.state.xaiDeviceLoginStatus === "complete" ? " is-success" :
        sharedState.state.xaiDeviceLoginStatus === "error" ? " is-error" : ""
      );
      status.textContent = sharedState.state.xaiDeviceLoginMessage;
      panel.appendChild(status);
    }

    var actions = document.createElement("div");
    actions.className = "runtime-actions";
    var loginButton = document.createElement("button");
    loginButton.className = "button runtime-secondary";
    loginButton.type = "button";
    loginButton.disabled = ["starting", "pending"].indexOf(sharedState.state.xaiDeviceLoginStatus) !== -1;
    loginButton.textContent = sharedState.state.xaiDeviceLoginStatus === "complete" ? "Reconnect xAI" : "Connect xAI";
    lifecycle.listen(loginButton, "click", function () {
      var openedWindow = null;
      try {
        openedWindow = window.open("about:blank", "_blank");
      } catch (error) {
        openedWindow = null;
      }
      startXAIDeviceLogin(openedWindow);
    });
    actions.appendChild(loginButton);
    panel.appendChild(actions);
    return panel;
  }

function renderRuntime() {
    var stack = document.createElement("div");
    stack.className = "runtime-stack";
    var selectedProvider = providerByLabel(sharedState.state.runtimeProvider);

    var identityGrid = document.createElement("div");
    identityGrid.className = "runtime-grid";

    var instanceInput = document.createElement("input");
    instanceInput.id = "runtime-instance-name";
    instanceInput.type = "text";
    instanceInput.className = "input";
    instanceInput.value = sharedState.state.runtimeInstanceName || operations.composedInstanceName();
    lifecycle.listen(instanceInput, "input", function () {
      sharedState.state.runtimeInstanceName = instanceInput.value;
      window.localStorage.setItem("ua.preview.runtime.instance.name", sharedState.state.runtimeInstanceName);
    });
    identityGrid.appendChild(renderRuntimeField("Instance name", instanceInput));

    var teamInput = document.createElement("input");
    teamInput.id = "runtime-team-id";
    teamInput.type = "text";
    teamInput.className = "input";
    teamInput.value = sharedState.state.runtimeTeamId || operations.composedTeamId();
    lifecycle.listen(teamInput, "input", function () {
      sharedState.state.runtimeTeamId = teamInput.value;
      window.localStorage.setItem("ua.preview.runtime.team.id", sharedState.state.runtimeTeamId);
    });
    identityGrid.appendChild(renderRuntimeField("Team id", teamInput));

    var timezoneSelect = document.createElement("select");
    timezoneSelect.id = "runtime-timezone";
    timezoneSelect.className = "select";
    model_setupState.timezoneOptions.forEach(function (timezone) {
      var option = document.createElement("option");
      option.textContent = timezone;
      option.value = timezone;
      option.selected = timezone === sharedState.state.runtimeTimezone;
      timezoneSelect.appendChild(option);
    });
    lifecycle.listen(timezoneSelect, "change", function () {
      sharedState.state.runtimeTimezone = timezoneSelect.value;
      window.localStorage.setItem("ua.preview.runtime.timezone", sharedState.state.runtimeTimezone);
    });
    identityGrid.appendChild(renderRuntimeField("Timezone", timezoneSelect));
    stack.appendChild(identityGrid);

    var providerCard = document.createElement("div");
    providerCard.className = "shim-card field runtime-provider-card";
    var providerLabel = document.createElement("label");
    providerLabel.htmlFor = "runtime-provider";
    providerLabel.textContent = "Model provider";
    var providerLabelWrap = document.createElement("div");
    providerLabelWrap.className = "label-with-info";
    providerLabelWrap.appendChild(providerLabel);
    providerLabelWrap.appendChild(operations.createInfoButton(
      "Model provider choices",
      model_setupState.providerHelpCopy,
      "Model provider choices information"
    ));
    var providerSelect = document.createElement("select");
    providerSelect.id = "runtime-provider";
    providerSelect.className = "select";
    model_setupState.providerDefinitions.forEach(function (provider) {
      var option = document.createElement("option");
      option.value = provider.label;
      option.textContent = provider.label;
      option.selected = provider.label === sharedState.state.runtimeProvider;
      providerSelect.appendChild(option);
    });
    lifecycle.listen(providerSelect, "change", function () {
      setRuntimeProvider(providerSelect.value);
      operations.renderCurrentPage();
    });
    providerCard.appendChild(providerLabelWrap);
    providerCard.appendChild(providerSelect);
    stack.appendChild(providerCard);
    if (selectedProvider.key === "openai-codex") {
      stack.appendChild(renderCodexProviderSetup());
    }
    if (selectedProvider.key === "xai-oauth") {
      stack.appendChild(renderXAIOAuthProviderSetup());
      if (sharedState.state.xaiDeviceLoginStatus === "pending") {
        scheduleXAIDeviceLoginPoll();
        scheduleXAICountdown();
      }
    } else {
      stopXAIDeviceLoginTimers();
    }

    var authGrid = document.createElement("div");
    authGrid.className = "runtime-grid runtime-auth-grid";

    var endpointInput = document.createElement("input");
    endpointInput.id = "runtime-endpoint";
    endpointInput.type = "url";
    endpointInput.className = "input";
    endpointInput.value = sharedState.state.runtimeEndpoint || providerByLabel(sharedState.state.runtimeProvider).endpoint;
    endpointInput.placeholder = "https://provider.example/v1";
    lifecycle.listen(endpointInput, "input", function () {
      sharedState.state.runtimeEndpoint = endpointInput.value;
      window.localStorage.setItem("ua.preview.runtime.endpoint", sharedState.state.runtimeEndpoint);
      invalidateVisibleRuntimeTest();
    });
    authGrid.appendChild(renderRuntimeField("API Base URL (endpoint)", endpointInput));

    var tokenInput = document.createElement("input");
    tokenInput.id = "runtime-token";
    tokenInput.type = "password";
    tokenInput.className = "input";
    tokenInput.value = sharedState.state.runtimeToken;
    tokenInput.placeholder = selectedProvider.tokenPlaceholder || "Provider token";
    lifecycle.listen(tokenInput, "input", function () {
      setRuntimeTokenValue(tokenInput.value);
      syncRuntimeStatus();
    });
    var tokenCard = document.createElement("div");
    tokenCard.className = "shim-card field";
    var tokenLabel = document.createElement("label");
    tokenLabel.htmlFor = tokenInput.id;
    tokenLabel.textContent = selectedProvider.tokenLabel || "Token";
    var credentialControl = document.createElement("div");
    credentialControl.className = "credential-input";
    var revealButton = document.createElement("button");
    revealButton.className = "credential-reveal-button";
    revealButton.type = "button";
    revealButton.innerHTML = operations.iconMarkup("eye");
    revealButton.title = "Show or hide value";
    revealButton.setAttribute("aria-label", "Show or hide value");
    revealButton.setAttribute("aria-controls", tokenInput.id);
    lifecycle.listen(revealButton, "click", function () {
      sharedState.credentialControls.toggleReveal(tokenInput, revealButton);
    });
    credentialControl.appendChild(tokenInput);
    credentialControl.appendChild(revealButton);
    tokenCard.appendChild(tokenLabel);
    tokenCard.appendChild(credentialControl);
    if (selectedProvider.key !== "xai-oauth") {
      authGrid.appendChild(tokenCard);
    }

    var modelInput = document.createElement("input");
    modelInput.id = "runtime-model";
    modelInput.type = "text";
    modelInput.className = "input";
    modelInput.value = sharedState.state.runtimeModel;
    modelInput.placeholder = selectedProvider.modelPlaceholder || "Model name";
    lifecycle.listen(modelInput, "input", function () {
      sharedState.state.runtimeModel = modelInput.value;
      window.localStorage.setItem("ua.preview.runtime.model", sharedState.state.runtimeModel);
      invalidateVisibleRuntimeTest();
    });
    var modelCard = renderRuntimeField("Model name", modelInput);
    var capabilityList = document.createElement("div");
    capabilityList.className = "runtime-capability-list";
    [
      {
        id: "runtime-supports-images",
        label: "Supports Images",
        stateKey: "runtimeSupportsImages",
        storageKey: "ua.preview.runtime.supports_image_input",
      },
      {
        id: "runtime-supports-audio",
        label: "Supports Audio",
        stateKey: "runtimeSupportsAudio",
        storageKey: "ua.preview.runtime.supports_audio_input",
      },
      {
        id: "runtime-supports-files",
        label: "Supports Files",
        stateKey: "runtimeSupportsFiles",
        storageKey: "ua.preview.runtime.supports_file_input",
      },
    ].forEach(function (capability) {
      var toggleLabel = document.createElement("label");
      toggleLabel.className = "runtime-inline-toggle";
      var toggleText = document.createElement("span");
      toggleText.className = "toggle-label";
      toggleText.textContent = capability.label;
      var toggleSwitch = document.createElement("span");
      toggleSwitch.className = "toggle";
      var toggleInput = document.createElement("input");
      toggleInput.id = capability.id;
      toggleInput.type = "checkbox";
      toggleInput.checked = Boolean(sharedState.state[capability.stateKey]);
      toggleInput.setAttribute("aria-label", capability.label);
      lifecycle.listen(toggleInput, "change", function () {
        sharedState.state[capability.stateKey] = toggleInput.checked;
        window.localStorage.setItem(
          capability.storageKey,
          sharedState.state[capability.stateKey] ? "true" : "false"
        );
      });
      var toggleKnob = document.createElement("span");
      toggleKnob.setAttribute("aria-hidden", "true");
      toggleSwitch.appendChild(toggleInput);
      toggleSwitch.appendChild(toggleKnob);
      toggleLabel.appendChild(toggleText);
      toggleLabel.appendChild(toggleSwitch);
      capabilityList.appendChild(toggleLabel);
    });
    modelCard.appendChild(capabilityList);
    authGrid.appendChild(modelCard);
    stack.appendChild(authGrid);

    var actionRow = document.createElement("div");
    actionRow.className = "runtime-actions";
    if (sharedState.state.runtimeProvider === "OpenAI") {
      var loginButton = document.createElement("button");
      loginButton.className = "button runtime-secondary";
      loginButton.type = "button";
      loginButton.disabled = true;
      loginButton.textContent = "Login via OpenAI";
      actionRow.appendChild(loginButton);
    }
    var testButton = document.createElement("button");
    testButton.className = "button primary";
    testButton.type = "button";
    testButton.textContent = "Test Connection";
    testButton.disabled = !runtimeTestCanRun();
    var testInFlight = false;
    lifecycle.listen(testButton, "click", function () {
      if (!runtimeTestCanRun() || testInFlight) {
        return;
      }
      testInFlight = true;
      sharedState.state.runtimeTestStatus = "Testing connection...";
      sharedState.state.runtimeTestStatusKind = "pending";
      var connectionUnderTest = runtimeConnectionSignature();
      status.textContent = sharedState.state.runtimeTestStatus;
      status.className = runtimeStatusClassName();
      updateTestButton();
      updateRuntimeNavigation();
      requestClient.fetch("/api/provider-test", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(runtimeTestPayload()),
      }).then(function (response) {
        return response.text().then(function (text) {
          var payload = {};
          var invalidResponseMessage = "";
          if (!text) {
            invalidResponseMessage = operations.providerTestHttpErrorMessage(response, text);
          } else {
            try {
              payload = JSON.parse(text);
            } catch (error) {
              invalidResponseMessage = operations.providerTestHttpErrorMessage(response, text);
            }
          }
          if (invalidResponseMessage) {
            throw new Error(invalidResponseMessage);
          }
          if (!response.ok) {
            throw new Error(payload.message || operations.providerTestHttpErrorMessage(response, text));
          }
          return payload;
        });
      }).then(function (payload) {
        if (connectionUnderTest !== runtimeConnectionSignature()) {
          testInFlight = false;
          syncRuntimeStatus();
          return;
        }
        var message = payload.message || "Connection test succeeded.";
        if (payload.sample) {
          message += "\n\nSample response: " + payload.sample;
        }
        sharedState.state.runtimeTestStatus = message;
        sharedState.state.runtimeTestStatusKind = "success";
        sharedState.state.runtimeTestSucceeded = true;
        sharedState.state.runtimeTestedConnectionSignature = connectionUnderTest;
        testInFlight = false;
        syncRuntimeStatus();
      }, function (error) {
        if (connectionUnderTest !== runtimeConnectionSignature()) {
          testInFlight = false;
          syncRuntimeStatus();
          return;
        }
        var errorMessage = String(error && error.message || error || "Unknown browser error.");
        if (errorMessage === "Failed to fetch" || errorMessage.indexOf("NetworkError") >= 0) {
          errorMessage = "The browser could not reach the onboarding connection-test endpoint. Check TLS, the public URL, and reverse-proxy routing for POST /api/provider-test.";
        }
        var message = "Connection test failed: " + errorMessage;
        sharedState.state.runtimeTestStatus = message;
        sharedState.state.runtimeTestStatusKind = "error";
        sharedState.state.runtimeTestSucceeded = false;
        sharedState.state.runtimeTestedConnectionSignature = "";
        testInFlight = false;
        syncRuntimeStatus();
      });
    });
    actionRow.appendChild(testButton);
    stack.appendChild(actionRow);

    var status = document.createElement("p");
    status.className = runtimeStatusClassName();
    status.textContent = runtimeStatusText();
    stack.appendChild(status);

    var noteText = "Once you are comfortable interacting with your team, you can configure additional providers and failover options in the generated instance configuration file.";
    var note = document.createElement("p");
    note.className = "runtime-note";
    note.textContent = noteText;
    note.hidden = !sharedState.state.runtimeTestSucceeded;
    stack.appendChild(note);
    stack.appendChild(renderRuntimeAdvanced());

    function invalidateVisibleRuntimeTest() {
      invalidateRuntimeTest();
      syncRuntimeStatus();
    }

    function syncRuntimeStatus() {
      status.className = runtimeStatusClassName();
      status.textContent = runtimeStatusText();
      note.hidden = !sharedState.state.runtimeTestSucceeded;
      updateTestButton();
      updateRuntimeNavigation();
    }

    function updateTestButton() {
      testButton.disabled = testInFlight || !runtimeTestCanRun();
    }

    return stack;
  }
return { applyXAIDeviceLoginPayload, currentXAIOAuthSettings, formatTeamSkillsValidationWarning, formatXAICountdown, invalidateRuntimeTest, persistAdvancedValue, persistFailoverFallbacks, providerByLabel, providerEnvVar, readXAIDeviceResponse, renderCodexProviderSetup, renderFailoverFallbacks, renderRuntime, renderRuntimeAdvanced, renderRuntimeField, renderRuntimeSecretField, renderXAIOAuthProviderSetup, runtimeConnectionSignature, runtimeReadyToProceed, runtimeStatusClassName, runtimeStatusText, runtimeTestCanRun, runtimeTestPayload, runtimeTokenForSelectedProvider, runtimeTokenRequired, scheduleXAICountdown, scheduleXAIDeviceLoginPoll, setEmbeddingApiKeyValue, setRuntimeProvider, setRuntimeTokenValue, setWebsiteAuthTokenValue, startXAIDeviceLogin, stopXAIDeviceLoginTimers, updateRuntimeNavigation };
}

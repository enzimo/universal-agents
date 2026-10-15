/** Existing chat diagnostics view behavior with explicit state and callback dependencies. */
export function createDiagnostics({ diagnosticsState, sharedState, operations, lifecycle, requestClient }) {
function normalizeContextDumpMode(value) {
    var normalized = String(value || "").trim().toLowerCase();
    if (["always", "on_error", "on_budget_warning"].indexOf(normalized) === -1) {
      return "on_error";
    }
    return normalized;
  }

function formatContextDumpTimestamp(value) {
    if (!value) {
      return "Never";
    }
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return "Never";
    }
    if (window.Intl && typeof window.Intl.DateTimeFormat === "function") {
      return new window.Intl.DateTimeFormat(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
        timeZoneName: "short",
      }).format(date);
    }
    return date.toLocaleString();
  }

function applyRuntimeDebugSettings(payload) {
    var contextDump = payload && payload.context_dump;
    if (!contextDump || typeof contextDump !== "object") {
      renderContextDumpDebugControls();
      scheduleContextDumpStatusPoll();
      return;
    }
    diagnosticsState.contextDumpDebug.controlsVisible = Boolean(contextDump.controls_visible);
    diagnosticsState.contextDumpDebug.enabled = Boolean(contextDump.enabled);
    diagnosticsState.contextDumpDebug.mode = normalizeContextDumpMode(contextDump.mode);
    if (Object.prototype.hasOwnProperty.call(contextDump, "last_dump")) {
      diagnosticsState.contextDumpDebug.lastDump = contextDump.last_dump || null;
    }
    renderContextDumpDebugControls();
    scheduleContextDumpStatusPoll();
  }

function renderContextDumpDebugControls() {
    if (!diagnosticsState.debugContextControls || !diagnosticsState.debugContextToggle || !diagnosticsState.debugContextMode) {
      return;
    }
    diagnosticsState.debugContextControls.hidden = !diagnosticsState.contextDumpDebug.controlsVisible;
    diagnosticsState.debugContextToggle.checked = diagnosticsState.contextDumpDebug.enabled;
    diagnosticsState.debugContextMode.value = diagnosticsState.contextDumpDebug.mode;
    if (diagnosticsState.debugContextLastDump && diagnosticsState.debugContextLastDumpValue) {
      var dumpedAt = diagnosticsState.contextDumpDebug.lastDump && diagnosticsState.contextDumpDebug.lastDump.available
        ? diagnosticsState.contextDumpDebug.lastDump.dumped_at
        : "";
      var dumpId = diagnosticsState.contextDumpDebug.lastDump && diagnosticsState.contextDumpDebug.lastDump.available
        ? String(diagnosticsState.contextDumpDebug.lastDump.dump_id || diagnosticsState.contextDumpDebug.lastDump.task_id || "")
        : "";
      diagnosticsState.debugContextLastDump.hidden = !diagnosticsState.contextDumpDebug.controlsVisible;
      diagnosticsState.debugContextLastDumpValue.textContent = formatContextDumpTimestamp(dumpedAt);
      if (diagnosticsState.debugContextDumpId) {
        diagnosticsState.debugContextDumpId.textContent = dumpId ? "Dump ID: " + dumpId : "";
        diagnosticsState.debugContextDumpId.hidden = !diagnosticsState.contextDumpDebug.controlsVisible || !dumpId;
      }
    }
  }

function setContextDumpDebugBusy(isBusy) {
    if (diagnosticsState.debugContextToggle) {
      diagnosticsState.debugContextToggle.disabled = Boolean(isBusy);
    }
    if (diagnosticsState.debugContextMode) {
      diagnosticsState.debugContextMode.disabled = Boolean(isBusy);
    }
  }

function setContextDumpDebugStatus(message, isError) {
    if (!diagnosticsState.debugContextStatus) {
      return;
    }
    diagnosticsState.debugContextStatus.textContent = message || "";
    diagnosticsState.debugContextStatus.hidden = !message;
    diagnosticsState.debugContextStatus.classList.toggle("is-error", Boolean(isError));
  }

function updateContextDumpDebugSettings() {
    if (!diagnosticsState.debugContextToggle || !diagnosticsState.debugContextMode) {
      return;
    }
    var previous = {
      enabled: diagnosticsState.contextDumpDebug.enabled,
      mode: diagnosticsState.contextDumpDebug.mode,
    };
    var thread = operations.activeThread();
    var nextEnabled = Boolean(diagnosticsState.debugContextToggle.checked);
    var nextMode = normalizeContextDumpMode(diagnosticsState.debugContextMode.value);
    diagnosticsState.contextDumpDebug.enabled = nextEnabled;
    diagnosticsState.contextDumpDebug.mode = nextMode;
    setContextDumpDebugBusy(true);
    setContextDumpDebugStatus("Saving...", false);
    requestClient.fetch("/api/web-chat/debug/context-dump", {
      method: "PATCH",
      headers: {
        "Content-Type": "application/json",
      },
      credentials: "same-origin",
      body: JSON.stringify({
        enabled: nextEnabled,
        mode: nextMode,
        client_id: sharedState.clientId,
        session_key: thread ? thread.sessionKey : "",
        conversation_id: thread ? thread.conversationId : "",
      }),
    }).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (payload) {
        if (!response.ok) {
          throw new Error(payload.message || payload.error || response.statusText || "Debug setting update failed.");
        }
        return payload;
      });
    }).then(function (payload) {
      applyRuntimeDebugSettings({ context_dump: payload.context_dump });
      setContextDumpDebugStatus(payload.live_applied ? "Applied live." : "Saved for manager restart.", false);
    }).catch(function (error) {
      diagnosticsState.contextDumpDebug.enabled = previous.enabled;
      diagnosticsState.contextDumpDebug.mode = previous.mode;
      renderContextDumpDebugControls();
      setContextDumpDebugStatus(error.message || "Debug setting update failed.", true);
    }).finally(function () {
      setContextDumpDebugBusy(false);
    });
  }

function refreshContextDumpStatus() {
    if (!sharedState.authSessionActive || diagnosticsState.contextDumpStatusInFlight) {
      return Promise.resolve();
    }
    diagnosticsState.contextDumpStatusInFlight = true;
    return requestClient.fetch("/api/web-chat/debug/context-dump", {
      cache: "no-store",
      credentials: "same-origin",
    }).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (payload) {
        if (!response.ok) {
          throw new Error(payload.message || payload.error || response.statusText || "Debug status unavailable.");
        }
        return payload;
      });
    }).then(function (payload) {
      applyRuntimeDebugSettings({ context_dump: payload.context_dump });
    }).catch(function () {
      return null;
    }).finally(function () {
      diagnosticsState.contextDumpStatusInFlight = false;
    });
  }

function scheduleContextDumpStatusPoll() {
    if (sharedState.contextDumpStatusTimer) {
      lifecycle.clearInterval(sharedState.contextDumpStatusTimer);
      sharedState.contextDumpStatusTimer = 0;
    }
    if (!sharedState.authSessionActive || !diagnosticsState.contextDumpDebug.controlsVisible) {
      return;
    }
    sharedState.contextDumpStatusTimer = lifecycle.interval(refreshContextDumpStatus, 10000);
  }
return { applyRuntimeDebugSettings, formatContextDumpTimestamp, normalizeContextDumpMode, refreshContextDumpStatus, renderContextDumpDebugControls, scheduleContextDumpStatusPoll, setContextDumpDebugBusy, setContextDumpDebugStatus, updateContextDumpDebugSettings };
}

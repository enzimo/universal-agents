import { ACCESS_VIEW, INBOX_VIEW } from "./chat_links.mjs";

export const SESSION_RESTORE_TIMEOUT_MS = 5000;

/** Existing chat authentication view behavior with explicit state and callback dependencies. */
export function createAuthentication({ authenticationState, sharedState, operations, lifecycle, requestClient }) {
function setEnrollmentMessage(message, isError) {
    authenticationState.enrollmentMessage.textContent = message || "";
    authenticationState.enrollmentMessage.classList.toggle("is-error", Boolean(isError));
  }

function clearEnrollmentToken() {
    authenticationState.enrollmentToken.value = "";
    authenticationState.enrollmentToken.type = "password";
    authenticationState.enrollmentReveal.setAttribute("aria-pressed", "false");
    authenticationState.enrollmentReveal.disabled = true;
    authenticationState.enrollmentCopyButton.disabled = true;
    authenticationState.enrollmentCompleteButton.disabled = true;
    authenticationState.enrollmentCopyButton.classList.remove("is-copied");
  }

function hideEnrollment() {
    authenticationState.enrollmentOverlay.classList.remove("is-open");
    authenticationState.enrollmentOverlay.setAttribute("aria-hidden", "true");
    authenticationState.enrollmentOverlay.hidden = true;
    clearEnrollmentToken();
    authenticationState.enrollmentRequestInFlight = false;
  }

function showEnrollment(payload) {
    sharedState.authSessionGeneration = (sharedState.authSessionGeneration || 0) + 1;
    sharedState.authSessionActive = false;
    authenticationState.authPanel.hidden = true;
    authenticationState.chatApp.hidden = true;
    authenticationState.objectivesApp.hidden = true;
    authenticationState.workspaceApp.hidden = true;
    authenticationState.statusApp.hidden = true;
    authenticationState.knowledgeApp.hidden = true;
    clearEnrollmentToken();
    var displayName = String(payload.display_name || payload.canonical_user_id || "this user");
    var label = String(payload.enrollment_label || "Initial browser");
    authenticationState.enrollmentCopy.textContent = "Generate a permanent access token for "
      + displayName
      + ". Save it somewhere secure; its device label is “"
      + label
      + "”.";
    if (payload.permanent_token_status === "pending_confirmation") {
      authenticationState.enrollmentGenerateButton.textContent = "Generate Replacement";
      setEnrollmentMessage(
        "A token was generated earlier but cannot be shown again. Generate a replacement to continue.",
        false
      );
    } else {
      authenticationState.enrollmentGenerateButton.textContent = "Generate";
      setEnrollmentMessage(
        "Generate your token, copy it somewhere safe, then confirm that you saved it.",
        false
      );
    }
    authenticationState.enrollmentOverlay.hidden = false;
    authenticationState.enrollmentOverlay.setAttribute("aria-hidden", "false");
    lifecycle.frame(function () {
      authenticationState.enrollmentOverlay.classList.add("is-open");
      authenticationState.enrollmentGenerateButton.focus();
    });
  }

function applyAuthenticatedSession(payload) {
    operations.stopSignIn();
    operations.stopLiveEvents();
    operations.setAssistantName(payload.assistant_name);
    operations.configurePrincipalState(payload);
    sharedState.settingsControls.setCsrfToken(payload.permission_csrf_token);
    sharedState.runtimeControlsCsrfToken = String(payload.permission_csrf_token || "");
    authenticationState.knowledgeClient.setCsrfToken(payload.permission_csrf_token);
    if (window.UAAppearance && typeof window.UAAppearance.loadServerPreference === "function") {
      window.UAAppearance.loadServerPreference().then(function () {
        operations.renderSettingsAppearance();
      }).catch(function () {
        return null;
      });
    }
    authenticationState.authInput.value = "";
    authenticationState.authError.hidden = true;
    if (payload.permanent_token_required) {
      showEnrollment(payload);
      return;
    }
    hideEnrollment();
    sharedState.authSessionGeneration = (sharedState.authSessionGeneration || 0) + 1;
    sharedState.authSessionActive = true;
    unlockChat();
    if (authenticationState.requestedView === INBOX_VIEW) {
      // An Inbox link (/webchat?view=inbox#access_request=<id>) only chooses
      // which request to highlight; it never decides anything.
      var accessRequestId = authenticationState.pendingAccessRequestId;
      authenticationState.pendingAccessRequestId = "";
      var inboxThread = operations.activeThread();
      sharedState.settingsControls.open(
        "inbox",
        inboxThread ? inboxThread.sessionKey : "",
        { accessRequestId: accessRequestId }
      ).catch(function () {
        return null;
      });
    } else if (authenticationState.requestedView === ACCESS_VIEW) {
      var accessAgentType = authenticationState.pendingAccessAgentType || "";
      authenticationState.pendingAccessAgentType = "";
      sharedState.settingsControls.openAccess(accessAgentType).catch(function () {
        return null;
      });
    }
  }

function generatePermanentToken() {
    if (authenticationState.enrollmentRequestInFlight) {
      return;
    }
    authenticationState.enrollmentRequestInFlight = true;
    authenticationState.enrollmentGenerateButton.disabled = true;
    authenticationState.enrollmentCompleteButton.disabled = true;
    setEnrollmentMessage("Generating a permanent access token...", false);
    requestClient.fetch("/api/web-chat/enrollment-token", {
      method: "POST",
      credentials: "same-origin",
    }).then(function (response) {
      return operations.parseApiResponse(response, "Permanent token generation failed.");
    }).then(function (payload) {
      authenticationState.enrollmentToken.value = String(payload.token || "");
      if (!authenticationState.enrollmentToken.value) {
        throw new Error("The server did not return a permanent token.");
      }
      authenticationState.enrollmentReveal.disabled = false;
      authenticationState.enrollmentCopyButton.disabled = false;
      authenticationState.enrollmentCompleteButton.disabled = false;
      authenticationState.enrollmentGenerateButton.textContent = "Generate Replacement";
      setEnrollmentMessage(
        "This token is shown only once. Copy it now, then confirm that you saved it.",
        false
      );
      authenticationState.enrollmentToken.focus();
      authenticationState.enrollmentToken.select();
    }).catch(function (error) {
      setEnrollmentMessage(error.message || "Permanent token generation failed.", true);
    }).finally(function () {
      authenticationState.enrollmentRequestInFlight = false;
      authenticationState.enrollmentGenerateButton.disabled = false;
    });
  }

function completePermanentTokenEnrollment() {
    if (authenticationState.enrollmentRequestInFlight || !authenticationState.enrollmentToken.value) {
      return;
    }
    authenticationState.enrollmentRequestInFlight = true;
    authenticationState.enrollmentGenerateButton.disabled = true;
    authenticationState.enrollmentCompleteButton.disabled = true;
    setEnrollmentMessage("Activating your permanent token...", false);
    requestClient.fetch("/api/web-chat/enrollment-complete", {
      method: "POST",
      credentials: "same-origin",
    }).then(function (response) {
      return operations.parseApiResponse(response, "Permanent token activation failed.");
    }).then(function (payload) {
      applyAuthenticatedSession(payload);
    }).catch(function (error) {
      setEnrollmentMessage(error.message || "Permanent token activation failed.", true);
      authenticationState.enrollmentCompleteButton.disabled = false;
    }).finally(function () {
      authenticationState.enrollmentRequestInFlight = false;
      authenticationState.enrollmentGenerateButton.disabled = false;
    });
  }

function resumeWebChatSession() {
    // Bound the entire restore, including reading the response body. Until
    // this settles the greeting and form deliberately stay hidden.
    var controller = new AbortController();
    var deadline = lifecycle.timeout(function () {
      controller.abort(new DOMException("Web Chat session restoration timed out.", "TimeoutError"));
    }, SESSION_RESTORE_TIMEOUT_MS);
    return requestClient.fetch("/api/web-chat/session", {
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
    }).then(function (response) {
      if (response.status === 401) {
        return null;
      }
      return operations.parseApiResponse(response, "Unable to restore the Web Chat session.");
    }).then(function (payload) {
      if (lifecycle.signal.aborted) return;
      if (payload) {
        applyAuthenticatedSession(payload);
      } else {
        // Nobody is signed in: the sign-in page greets them.
        operations.showSignIn();
      }
    }).catch(function (error) {
      if (lifecycle.signal.aborted) return;
      console.warn("ua.web_chat: the Web Chat session could not be restored; showing sign-in", error);
      operations.showSignIn();
    }).finally(function () {
      lifecycle.clearTimeout(deadline);
    });
  }

function lockChat(message) {
    sharedState.authSessionGeneration = (sharedState.authSessionGeneration || 0) + 1;
    sharedState.authSessionActive = false;
    operations.stopLiveEvents();
    lifecycle.clearInterval(sharedState.contextDumpStatusTimer);
    sharedState.contextDumpStatusTimer = 0;
    lifecycle.clearInterval(authenticationState.objectivesRefreshTimer);
    authenticationState.objectivesRefreshTimer = 0;
    sharedState.objectivesRequestInFlight = false;
    sharedState.objectivesSnapshotSignature = "";
    sharedState.runtimeControlSnapshots.clear();
    sharedState.runtimeControlsRequest = null;
    sharedState.runtimeControlsSessionKey = "";
    sharedState.runtimeControlsSessionLookup = null;
    sharedState.runtimeControlsCsrfToken = "";
    sharedState.runtimeControlsView = "objectives";
    // An open Customize Theme panel closes without saving.
    if (window.UAAppearanceCustomizer) {
      window.UAAppearanceCustomizer.close();
    }
    if (window.UAAppearance && typeof window.UAAppearance.clearUserPreference === "function") {
      window.UAAppearance.endPreview();
      window.UAAppearance.clearUserPreference();
    }
    operations.clearOptimisticDeliveries();
    sharedState.threads = [];
    sharedState.activeThreadId = "";
    sharedState.editingThreadId = "";
    sharedState.clientId = "";
    sharedState.clientIdKey = "";
    sharedState.storageKey = "";
    operations.clearPendingAttachments();
    sharedState.asyncEventCursor = "";
    sharedState.threadsRevision = "";
    sharedState.historyHydrationError = null;
    sharedState.settingsControls.clear();
    authenticationState.workspaceControls.clear();
    authenticationState.settingsWorkspaceLink.hidden = true;
    authenticationState.settingsStatusLink.hidden = true;
    authenticationState.settingsKnowledgeLink.hidden = true;
    document.getElementById("status-content").replaceChildren();
    authenticationState.knowledgeClient.setCsrfToken("");
    authenticationState.knowledgeControls.clear();
    sharedState.accountNameLabel.textContent = "Web Chat user";
    sharedState.threadList.innerHTML = "";
    sharedState.messageList.innerHTML = "";
    sharedState.messageInput.value = "";
    sharedState.objectivesContent.replaceChildren();
    sharedState.objectivesUpdatedAt.textContent = "";
    operations.setObjectivesStatus("", false);
    hideEnrollment();
    authenticationState.chatApp.hidden = true;
    authenticationState.objectivesApp.hidden = true;
    authenticationState.workspaceApp.hidden = true;
    authenticationState.statusApp.hidden = true;
    authenticationState.knowledgeApp.hidden = true;
    authenticationState.authPanel.hidden = false;
    if (message) {
      authenticationState.authError.textContent = message;
      authenticationState.authError.hidden = false;
    } else {
      authenticationState.authError.textContent = "";
      authenticationState.authError.hidden = true;
    }
    // The greeting plays again and focuses the token field once the window shows.
    operations.showSignIn();
  }

function handleUnauthorizedResponse(response) {
    if (response.status !== 401) {
      return false;
    }
    lockChat("Your Web Chat session ended. Sign in again to continue.");
    return true;
  }

function logoutChat() {
    authenticationState.logoutButton.disabled = true;
    authenticationState.objectivesLogout.disabled = true;
    authenticationState.workspaceLogout.disabled = true;
    authenticationState.statusLogout.disabled = true;
    authenticationState.knowledgeLogout.disabled = true;
    authenticationState.enrollmentCancelButton.disabled = true;
    requestClient.fetch("/api/web-chat/logout", {
      method: "POST",
      credentials: "same-origin",
    }).catch(function () {
      return null;
    }).finally(function () {
      authenticationState.logoutButton.disabled = false;
      authenticationState.objectivesLogout.disabled = false;
      authenticationState.workspaceLogout.disabled = false;
      authenticationState.statusLogout.disabled = false;
      authenticationState.knowledgeLogout.disabled = false;
      authenticationState.enrollmentCancelButton.disabled = false;
      lockChat("");
    });
  }

function unlockChat() {
    if (authenticationState.objectivesMode) {
      unlockObjectives();
      return;
    }
    if (authenticationState.workspaceMode) {
      unlockWorkspace();
      return;
    }
    if (authenticationState.statusMode) {
      unlockStatus();
      return;
    }
    if (authenticationState.knowledgeMode) {
      unlockKnowledge();
      return;
    }
    authenticationState.objectivesApp.hidden = true;
    authenticationState.workspaceApp.hidden = true;
    authenticationState.statusApp.hidden = true;
    authenticationState.knowledgeApp.hidden = true;
    operations.renderThreads();
    operations.renderMessages();
    authenticationState.authPanel.hidden = true;
    authenticationState.chatApp.hidden = false;
    operations.refreshContextDumpStatus();
    operations.fetchSessions().then(function () {
      if (!sharedState.authSessionActive) {
        return;
      }
      operations.renderThreads();
      operations.renderMessages();
      operations.startLiveEvents();
      sharedState.messageInput.focus();
    });
  }

function unlockObjectives() {
    authenticationState.authPanel.hidden = true;
    authenticationState.chatApp.hidden = true;
    authenticationState.workspaceApp.hidden = true;
    authenticationState.statusApp.hidden = true;
    authenticationState.knowledgeApp.hidden = true;
    authenticationState.objectivesApp.hidden = false;
    lifecycle.clearInterval(authenticationState.objectivesRefreshTimer);
    authenticationState.objectivesRefreshTimer = lifecycle.interval(operations.loadObjectives, 10000);
    ensureRuntimeControlsSession().then(operations.loadObjectives).catch(function (error) {
      operations.setObjectivesStatus(error.message || "Unable to initialize Runtime Controls.", true);
    });
  }

function unlockWorkspace() {
    authenticationState.authPanel.hidden = true;
    authenticationState.chatApp.hidden = true;
    authenticationState.objectivesApp.hidden = true;
    authenticationState.workspaceApp.hidden = false;
    authenticationState.workspaceControls.load();
  }

function unlockStatus() {
    authenticationState.authPanel.hidden = true;
    authenticationState.chatApp.hidden = true;
    authenticationState.objectivesApp.hidden = true;
    authenticationState.workspaceApp.hidden = true;
    authenticationState.statusApp.hidden = false;
    authenticationState.statusControls.load(0);
  }

function unlockKnowledge() {
    authenticationState.authPanel.hidden = true;
    authenticationState.chatApp.hidden = true;
    authenticationState.objectivesApp.hidden = true;
    authenticationState.workspaceApp.hidden = true;
    authenticationState.statusApp.hidden = true;
    authenticationState.knowledgeApp.hidden = false;
    authenticationState.knowledgeControls.load();
  }

function ensureRuntimeControlsSession() {
    const generation = sharedState.authSessionGeneration;
    const requireCurrentSession = () => {
      if (generation !== sharedState.authSessionGeneration) {
        throw new DOMException("The session ended.", "AbortError");
      }
    };
    if (sharedState.runtimeControlsSessionKey) {
      return Promise.resolve(sharedState.runtimeControlsSessionKey);
    }
    // Callers that ask at the same time (a page's list and its badge count)
    // share one lookup, so a person without a conversation gets one, not two.
    const inFlight = sharedState.runtimeControlsSessionLookup;
    if (inFlight && inFlight.generation === generation) {
      return inFlight.promise;
    }
    const lookup = { generation, promise: null };
    lookup.promise = requestClient.fetch("/api/web-chat/sessions", {
      cache: "no-store",
      credentials: "same-origin",
    }).then(function (response) {
      requireCurrentSession();
      return operations.parseApiResponse(response, "Unable to initialize Runtime Controls.", requireCurrentSession);
    }).then(function (payload) {
      // A late lookup from before logout must not replace the new person's
      // conversation or start another conversation in their session.
      requireCurrentSession();
      var sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
      sharedState.runtimeControlsSessionKey = sessions.length
        ? String(sessions[0].session_key || "")
        : "";
      if (sharedState.runtimeControlsSessionKey) {
        return sharedState.runtimeControlsSessionKey;
      }
      return requestClient.fetch("/api/web-chat/sessions", {
        method: "POST",
        credentials: "same-origin",
      }).then(function (response) {
        requireCurrentSession();
        return operations.parseApiResponse(response, "Unable to create a Runtime Controls session.", requireCurrentSession);
      }).then(function (created) {
        requireCurrentSession();
        sharedState.runtimeControlsSessionKey = String(
          created.session && created.session.session_key || ""
        );
        if (!sharedState.runtimeControlsSessionKey) {
          throw new Error("The Runtime Controls session was not created.");
        }
        return sharedState.runtimeControlsSessionKey;
      });
    }).finally(function () {
      if (sharedState.runtimeControlsSessionLookup === lookup) {
        sharedState.runtimeControlsSessionLookup = null;
      }
    });
    sharedState.runtimeControlsSessionLookup = lookup;
    return lookup.promise;
  }
return { applyAuthenticatedSession, clearEnrollmentToken, completePermanentTokenEnrollment, ensureRuntimeControlsSession, generatePermanentToken, handleUnauthorizedResponse, hideEnrollment, lockChat, logoutChat, resumeWebChatSession, setEnrollmentMessage, showEnrollment, unlockChat, unlockKnowledge, unlockObjectives, unlockStatus, unlockWorkspace };
}

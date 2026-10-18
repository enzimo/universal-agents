import { mountWorkflowCard } from "./workflow_card.mjs";
import { createPageLifecycle, createRequestClient } from "./browser_lifecycle.mjs";
import { createAppearance } from "./chat_appearance.mjs";
import { createDiagnostics } from "./chat_diagnostics.mjs";
import { createAuthentication } from "./chat_authentication.mjs";
import { setAssistantName } from "./chat_assistant_name.mjs";
import { createSignInGreeting } from "./chat_sign_in.mjs";
import { createRuntimeViews } from "./chat_runtime_views.mjs";
import { createLiveEvents } from "./chat_live_events.mjs";
import { createChatAttention } from "./chat_attention.mjs";
import { KNOWLEDGE_VIEW, readChatPageLink } from "./chat_links.mjs";
import { isAccessReviewMessage } from "./chat_access_review.mjs";
import { createSettingsControls } from "./settings_controls.mjs";
import { createWorkspaceFileActions } from "./workspace_file_actions.mjs";
import { createTranscriptView } from "./chat_transcript_view.mjs";
import { createStatusPage } from "./status_page.mjs";
import { createKnowledgeClient, createKnowledgePage, readPendingProposals } from "./knowledge_page.mjs";
import { createActivityMesh } from "./activity_mesh.mjs";
import { createTeamActivity } from "./team_activity.mjs";
import { createSettingsClient } from "./settings_support.mjs";

function mountPage() {
"use strict";
const lifecycle = createPageLifecycle(window);
const requestClient = createRequestClient(window, lifecycle);
// Read Inbox and Access links before anything checks the address: they only
// choose a panel to open after sign-in, so the address goes back to plain chat
// and a reload stays on the chat.
const pageLink = readChatPageLink(window.location);
if (pageLink.cleanUrl) {
  window.history.replaceState(null, document.title, pageLink.cleanUrl);
}
const appearanceState = {};
const authenticationState = {};
const diagnosticsState = {};
const runtime_viewsState = {};
const sharedState = {};
const shellState = {};
// What the page calls a thread that nothing has named yet (the server sends
// no title until the thread's first ordinary message, web_chat_threads.py).
const NEW_CHAT_TITLE = "New Chat";
const attention = createChatAttention({
  browser: window, document, lifecycle,
  isChatView: !new URLSearchParams(window.location.search).has("view"),
  getThreads: () => sharedState.threads || [],
  isAuthenticated: () => sharedState.authSessionActive && !new URLSearchParams(window.location.search).has("view"),
  openThread: thread => {
    if (!(sharedState.threads || []).includes(thread)) return;
    sharedState.settingsControls.close();
    sharedState.activeThreadId = thread.id;
    saveThreads(); renderThreads(); renderMessages(); ensureActiveThreadHistory();
  },
});
const appearance = createAppearance({ appearanceState, sharedState, lifecycle, requestClient, operations: { handleUnauthorizedResponse: (...args) => authentication.handleUnauthorizedResponse(...args) } });
const diagnostics = createDiagnostics({ diagnosticsState, sharedState, lifecycle, requestClient, operations: { activeThread: (...args) => activeThread(...args) } });
const authentication = createAuthentication({ authenticationState, sharedState, lifecycle, requestClient, operations: { activeThread: (...args) => activeThread(...args), clearOptimisticDeliveries: (...args) => clearOptimisticDeliveries(...args), clearPendingAttachments: (...args) => clearPendingAttachments(...args), configurePrincipalState: (...args) => configurePrincipalState(...args), fetchSessions: (...args) => fetchSessions(...args), loadObjectives: (...args) => runtime_views.loadObjectives(...args), parseApiResponse: (...args) => parseApiResponse(...args), refreshContextDumpStatus: (...args) => diagnostics.refreshContextDumpStatus(...args), renderMessages: (...args) => renderMessages(...args), renderSettingsAppearance: (...args) => appearance.renderSettingsAppearance(...args), renderThreads: (...args) => renderThreads(...args), setAssistantName: (name) => setAssistantName(sharedState, document, name), showSignIn: () => signInGreeting.show(), stopSignIn: () => signInGreeting.stop(), startLiveEvents: () => { liveEvents.start(); activityMesh.start(); sharedState.settingsControls.startInboxCount(); }, stopLiveEvents: () => { liveEvents.stop(); attention.clear(); activityMesh.stop(); teamActivity.clear(); }, setObjectivesStatus: (...args) => runtime_views.setObjectivesStatus(...args) } });
const runtime_views = createRuntimeViews({ runtime_viewsState, sharedState, lifecycle, requestClient, operations: { ensureRuntimeControlsSession: (...args) => authentication.ensureRuntimeControlsSession(...args), formatShortLocalTimestamp: (...args) => formatShortLocalTimestamp(...args), parseApiResponse: (...args) => parseApiResponse(...args), timestampElement: (...args) => timestampElement(...args), uiElement: (...args) => uiElement(...args) } });
// The team's live status, carried by every live page and shown by the
// activity mesh, Settings → Team and the Tasks view (team_activity.mjs).
const teamActivity = createTeamActivity();
const liveEvents = createLiveEvents({
  lifecycle,
  isAuthenticated: () => sharedState.authSessionActive,
  getCursor: () => sharedState.asyncEventCursor,
  applyPage: payload => {
    payload.messages.forEach(applyTranscriptEvent);
    // Access requests and stuck-work reports arrive with a chat message.
    if (payload.messages.some(message => message.role && message.role !== "user")) {
      sharedState.settingsControls.noteAgentMessage();
    }
    applyCredentialActions(payload.transient_actions);
    applyThreadStates(payload);
    teamActivity.publish(payload.team_activity);
    sharedState.asyncEventCursor = String(payload.next_cursor);
  },
  resetHistory: () => fetchSessions({ resetHistory: true }).then(loaded => {
    if (!loaded) throw new Error("Unable to restore chat history.");
  }),
  checkSession: () => requestClient.fetch("/api/web-chat/session", {
    credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(10000),
  }).then(response => response.status !== 401 && response.status !== 403),
  onUnauthorized: () => authentication.handleUnauthorizedResponse({ status: 401 }),
  onStatus: text => {
    const status = document.getElementById("chat-connection-status");
    if (status.textContent !== text) status.textContent = text;
  },
});

window.addEventListener("pagehide", () => lifecycle.dispose(), { once: true });

"use strict";

shellState.credentialControls = window.UACredentialControls;

shellState.operationalIds = window.UAOperationalIds;

sharedState.storageKey = "";

sharedState.clientIdKey = "";

appearanceState.canvas = document.getElementById("chat-mesh-canvas");

appearanceState.ctx = appearanceState.canvas.getContext("2d");

appearanceState.polygonMotion = window.UAPolygonMotion || {
    polygonTime: function (time) {
      return time * 2;
    },
  };

authenticationState.authPanel = document.getElementById("chat-auth");

appearanceState.chatPreview = document.querySelector(".chat-preview");

authenticationState.chatApp = document.getElementById("chat-app");

authenticationState.objectivesApp = document.getElementById("objectives-app");

authenticationState.workspaceApp = document.getElementById("workspace-app");

authenticationState.statusApp = document.getElementById("status-app");

authenticationState.statusLogout = document.getElementById("status-logout");

authenticationState.knowledgeApp = document.getElementById("knowledge-app");

authenticationState.knowledgeLogout = document.getElementById("knowledge-logout");

sharedState.workspaceRefresh = document.getElementById("workspace-refresh");

authenticationState.workspaceLogout = document.getElementById("workspace-logout");

sharedState.workspaceStatus = document.getElementById("workspace-status");

sharedState.workspaceTree = document.getElementById("workspace-tree");

sharedState.workspaceViewer = document.getElementById("workspace-viewer");

sharedState.workspaceViewerTitle = document.getElementById("workspace-viewer-title");

sharedState.workspaceViewerMeta = document.getElementById("workspace-viewer-meta");

sharedState.workspaceViewerTools = document.getElementById("workspace-viewer-tools");

runtime_viewsState.objectivesRefresh = document.getElementById("objectives-refresh");

authenticationState.objectivesLogout = document.getElementById("objectives-logout");

runtime_viewsState.objectivesStatus = document.getElementById("objectives-status");

sharedState.objectivesUpdatedAt = document.getElementById("objectives-updated-at");

sharedState.objectivesContent = document.getElementById("objectives-content");

runtime_viewsState.runtimeObjectivesTab = document.getElementById("runtime-objectives-tab");

runtime_viewsState.runtimeSchedulesTab = document.getElementById("runtime-schedules-tab");

runtime_viewsState.runtimeMcpTab = document.getElementById("runtime-mcp-tab");

authenticationState.requestedView = pageLink.view;

authenticationState.objectivesMode = authenticationState.requestedView === "objectives";

authenticationState.workspaceMode = authenticationState.requestedView === "files";

authenticationState.statusMode = authenticationState.requestedView === "status";

authenticationState.knowledgeMode = authenticationState.requestedView === KNOWLEDGE_VIEW;

sharedState.authForm = document.getElementById("chat-auth-form");

authenticationState.authInput = document.getElementById("chat-auth-token");

sharedState.authReveal = document.getElementById("chat-auth-reveal");

authenticationState.authError = document.getElementById("chat-auth-error");

authenticationState.enrollmentOverlay = document.getElementById("chat-enrollment-overlay");

authenticationState.enrollmentCopy = document.getElementById("chat-enrollment-copy");

authenticationState.enrollmentToken = document.getElementById("chat-enrollment-token");

authenticationState.enrollmentReveal = document.getElementById("chat-enrollment-reveal");

authenticationState.enrollmentCopyButton = document.getElementById("chat-enrollment-copy-button");

authenticationState.enrollmentGenerateButton = document.getElementById("chat-enrollment-generate-button");

authenticationState.enrollmentCompleteButton = document.getElementById("chat-enrollment-complete-button");

authenticationState.enrollmentCancelButton = document.getElementById("chat-enrollment-cancel-button");

authenticationState.enrollmentMessage = document.getElementById("chat-enrollment-message");

sharedState.accountNameLabel = document.getElementById("chat-account-name");

authenticationState.logoutButton = document.getElementById("settings-logout-button");

shellState.versionLabel = document.getElementById("chat-version");

sharedState.newThreadButton = document.getElementById("new-thread-button");

sharedState.settingsButton = document.getElementById("settings-button");

authenticationState.settingsWorkspaceLink = document.getElementById("settings-workspace-link");

authenticationState.settingsStatusLink = document.getElementById("settings-status-link");

authenticationState.settingsKnowledgeLink = document.getElementById("settings-knowledge-link");

appearanceState.settingsAppearanceContent = document.getElementById("settings-appearance-content");

sharedState.settingsAppearanceReset = document.getElementById("settings-appearance-reset");

appearanceState.settingsAppearanceStatus = document.getElementById("settings-appearance-status");

sharedState.threadList = document.getElementById("thread-list");

diagnosticsState.debugContextControls = document.getElementById("debug-context-controls");

diagnosticsState.debugContextToggle = document.getElementById("debug-context-toggle");

diagnosticsState.debugContextMode = document.getElementById("debug-context-mode");

diagnosticsState.debugContextLastDump = document.getElementById("debug-context-last-dump");

diagnosticsState.debugContextLastDumpValue = document.getElementById("debug-context-last-dump-value");

diagnosticsState.debugContextDumpId = document.getElementById("debug-context-dump-id");

diagnosticsState.debugContextStatus = document.getElementById("debug-context-status");

sharedState.messageList = document.getElementById("message-list");

// Keeps the person at the latest message, or on the rows they are reading,
// across renders and later size changes (cards filling in, images loading).
shellState.transcriptView = createTranscriptView({ list: sharedState.messageList, lifecycle: lifecycle });

sharedState.messageForm = document.getElementById("message-form");

sharedState.messageInput = document.getElementById("message-input");

sharedState.attachmentInput = document.getElementById("message-attachment-input");

sharedState.attachButton = document.getElementById("message-attach-button");

shellState.attachmentPreview = document.getElementById("message-attachment-preview");

sharedState.authSessionActive = false;

authenticationState.enrollmentRequestInFlight = false;

sharedState.contextDumpStatusTimer = 0;

diagnosticsState.contextDumpStatusInFlight = false;

authenticationState.objectivesRefreshTimer = 0;

sharedState.objectivesRequestInFlight = false;

sharedState.objectivesSnapshotSignature = "";

sharedState.runtimeControlSnapshots = new Map();

sharedState.runtimeControlsRequest = null;

sharedState.runtimeControlsSessionKey = "";

// The session lookup in flight, shared by callers that ask at the same time (chat_authentication.mjs).
sharedState.runtimeControlsSessionLookup = null;

sharedState.runtimeControlsCsrfToken = "";

appearanceState.settingsAppearanceMutationChain = Promise.resolve();

sharedState.runtimeControlsView = "objectives";

sharedState.asyncEventCursor = "";

// Server digest of the thread list; a live page with another value reloads it.
sharedState.threadsRevision = "";

shellState.SYSTEM_ERROR_SOURCE = Object.freeze({
    ARCHIVE_THREAD: "archive_thread",
    CREATE_THREAD: "create_thread",
    FETCH_SESSIONS: "fetch_sessions",
    HYDRATE_THREAD: "hydrate_thread",
    OPEN_SETTINGS: "open_settings",
    RENAME_THREAD: "rename_thread",
    SEND_MESSAGE: "send_message",
  });

sharedState.historyHydrationError = null;

sharedState.editingThreadId = "";

shellState.threadTitleTooltip = null;

shellState.threadTitleTooltipTimer = 0;

shellState.threadTitleTooltipOwner = null;

shellState.pendingAttachments = [];

shellState.pendingAttachmentSeq = 0;

sharedState.clientId = "";

// Access Inbox links (/webchat?view=inbox#access_request=<id>) only select a
// request to highlight after sign-in; they never decide anything. Access page
// links (/webchat?view=access#agent=<type>) only choose which agent type to
// show after sign-in; they never change access.
authenticationState.pendingAccessRequestId = pageLink.accessRequestId;

authenticationState.pendingAccessAgentType = pageLink.accessAgentType;

// The command guide comes only from the server (/api/web-chat/settings,
// assistant_gateway.application.command_help.WEB_CHAT_COMMAND_GUIDE), so the
// browser never shows a second, outdated copy of the commands.
shellState.commandLines = [];

sharedState.threads = [];

sharedState.activeThreadId = "";

appearanceState.meshPoints = [];

appearanceState.meshColumns = 0;

appearanceState.meshRows = 0;

appearanceState.lastWidth = 0;

appearanceState.lastHeight = 0;

appearanceState.lightPaneCanvas = document.createElement("canvas");

appearanceState.lightPaneCtx = appearanceState.lightPaneCanvas.getContext("2d");

appearanceState.lightPaneCacheKey = "";

sharedState.assistantName = window.localStorage.getItem("ua.preview.assistant.name") || "Lisa";

shellState.markdownRenderer = window.UAWebChatMarkdown;

shellState.optimisticMessageSequence = 0;

shellState.transientCountdownTimer = 0;

shellState.expiredSecureFormMessage = "This secure form link has expired. A submitted request may still be finishing.";

shellState.deliveryTracker = window.UAChatDelivery.create({
    onTimeout: markMessageNotDelivered,
  });

// Share… and Send… in Workspace Files; writes carry the signed-in session's CSRF token.
shellState.workspaceFileActions = createWorkspaceFileActions({
    browser: window,
    onUnauthorized: authentication.handleUnauthorizedResponse,
    getCsrfToken: function () { return sharedState.runtimeControlsCsrfToken; },
  });

authenticationState.workspaceControls = window.UAWorkspaceBrowser.create({
    tree: sharedState.workspaceTree,
    viewer: sharedState.workspaceViewer,
    viewerTitle: sharedState.workspaceViewerTitle,
    viewerMeta: sharedState.workspaceViewerMeta,
    viewerTools: sharedState.workspaceViewerTools,
    itemActions: document.getElementById("workspace-item-actions"),
    status: sharedState.workspaceStatus,
    refreshButton: sharedState.workspaceRefresh,
    markdownRenderer: shellState.markdownRenderer,
    onUnauthorized: authentication.handleUnauthorizedResponse,
    titleNodes: [
      document.getElementById("workspace-title"),
      document.getElementById("workspace-tree-heading"),
    ],
    fileActions: shellState.workspaceFileActions,
  });

authenticationState.statusClient = createSettingsClient({
    browser: window,
    onUnauthorized: authentication.handleUnauthorizedResponse,
  });

authenticationState.statusControls = createStatusPage({
    document,
    content: document.getElementById("status-content"),
    status: document.getElementById("status-status"),
    refreshButton: document.getElementById("status-refresh"),
    updatedAt: document.getElementById("status-updated-at"),
    client: authenticationState.statusClient,
  });

// The Knowledge page (knowledge_page.mjs). Its writes carry the signed-in
// session's CSRF token (set in chat_authentication.mjs), and requests act in
// the same Web Chat thread Runtime Controls use.
authenticationState.knowledgeClient = createKnowledgeClient({
    browser: window,
    onUnauthorized: authentication.handleUnauthorizedResponse,
  });

authenticationState.knowledgeControls = createKnowledgePage({
    browser: window,
    signal: lifecycle.signal,
    client: authenticationState.knowledgeClient,
    getSessionKey: () => authentication.ensureRuntimeControlsSession(),
    // Page text goes through the same escaped Markdown renderer as chat messages.
    renderMarkdown: (node, markdown) => { node.innerHTML = shellState.markdownRenderer.render(markdown); },
    nodes: {
      status: document.getElementById("knowledge-status"),
      refreshButton: document.getElementById("knowledge-refresh"),
      newButton: document.getElementById("knowledge-new"),
      homeButton: document.getElementById("knowledge-home"),
      browseButton: document.getElementById("knowledge-browse"),
      layout: document.getElementById("knowledge-layout"),
      directory: document.getElementById("knowledge-directory"),
      pagination: document.getElementById("knowledge-pagination"),
      tabs: {
        team: document.getElementById("knowledge-team-tab"),
        personal: document.getElementById("knowledge-personal-tab"),
        proposals: document.getElementById("knowledge-proposals-tab"),
      },
      proposalsCount: document.getElementById("knowledge-proposals-count"),
      searchForm: document.getElementById("knowledge-search"),
      searchInput: document.getElementById("knowledge-query"),
      pageStatus: document.getElementById("knowledge-page-status"),
      list: document.getElementById("knowledge-list"),
      viewerTitle: document.getElementById("knowledge-viewer-title"),
      viewerMeta: document.getElementById("knowledge-viewer-meta"),
      viewerTools: document.getElementById("knowledge-viewer-tools"),
      viewer: document.getElementById("knowledge-viewer"),
    },
  });

// The sign-in page's greeting and window (chat_sign_in.mjs), played every
// time the sign-in page shows.
const signInGreeting = createSignInGreeting({
    browser: window,
    stage: document.getElementById("chat-auth"),
    heading: document.getElementById("chat-title"),
    card: document.getElementById("chat-auth-card"),
    focusTarget: authenticationState.authInput,
    forgetButton: document.getElementById("chat-auth-forget"),
  });

lifecycle.listen(document.getElementById("chat-auth-forget"), "click", signInGreeting.forget);

// Team activity in the sidebar (activity_mesh.mjs); runs while signed in.
const activityMesh = createActivityMesh({
    browser: window,
    lifecycle,
    host: document.getElementById("chat-activity"),
    teamActivity,
  });

sharedState.settingsControls = createSettingsControls({
    teamActivity,
    onClose: function () { renderThreads(); },
    returnFocus: sharedState.settingsButton,
    onUnauthorized: authentication.handleUnauthorizedResponse,
    onAppearanceOpen: appearance.renderSettingsAppearance,
  });

lifecycle.signal.addEventListener("abort", () => sharedState.settingsControls.close(), { once: true });

appearanceState.prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

diagnosticsState.contextDumpDebug = {
    controlsVisible: false,
    enabled: false,
    mode: "on_error",
    lastDump: null,
  };

appearanceState.lightDefinitions = [
    { key: "blue", defaultColor: "#082f73", x: 0.24, y: 0.28 },
    { key: "yellow", defaultColor: "#ffd60a", x: 0.74, y: 0.26 },
    { key: "green", defaultColor: "#20f05a", x: 0.28, y: 0.76 },
    { key: "red", defaultColor: "#ff3030", x: 0.76, y: 0.75 },
  ];

sharedState.appearance = appearance.loadAppearanceSettings();

setAssistantName(sharedState, document, sharedState.assistantName);

// The greeting is the same on every page; the sign-in window's button names
// the page it opens.
if (authenticationState.objectivesMode) {
    document.title = "Runtime Controls · Universal Agents";
    document.getElementById("chat-auth-submit").textContent = "Open Runtime Controls";
    document.getElementById("chat-auth-copy").textContent =
      "Use your Web Chat access to review and control your runtime resources.";
  } else if (authenticationState.statusMode) {
    document.title = "Status · Universal Agents";
    document.getElementById("chat-auth-submit").textContent = "Open Status";
    document.getElementById("chat-auth-copy").textContent =
      "Use your Web Chat access to see what needs attention.";
  } else if (authenticationState.knowledgeMode) {
    document.title = "Knowledge · Universal Agents";
    document.getElementById("chat-auth-submit").textContent = "Open Knowledge";
    document.getElementById("chat-auth-copy").textContent =
      "Use your Web Chat access to read and write knowledge pages.";
  } else if (authenticationState.workspaceMode) {
    document.title = "Workspace Files · Universal Agents";
    document.getElementById("chat-auth-submit").textContent = "Open Workspace Files";
    document.getElementById("chat-auth-copy").textContent =
      "Use your Web Chat access to see your files.";
  }

loadRuntimeSettings();

function normalizeMessageList(messages) {
    if (!Array.isArray(messages)) {
      return [];
    }
    return messages.map(function (message) {
      return {
        messageId: String(message.messageId || message.message_id || ""),
        clientRequestId: String(message.clientRequestId || message.client_request_id || ""),
        accessRequestId: String(message.accessRequestId || message.access_request_id || ""),
        sessionSequence: Number(message.sessionSequence || message.session_sequence || 0),
        eventSequence: Number(message.eventSequence || message.event_sequence || 0),
        role: String(message.role || "assistant"),
        content: String(message.content || ""),
        attachments: Array.isArray(message.attachments) ? message.attachments : [],
        messageType: String(message.messageType || message.message_type || ""),
        messageLevel: normalizeMessageLevel(message.messageLevel || message.message_level),
        correlationId: String(message.correlationId || message.correlation_id || ""),
        createdAt: String(message.createdAt || message.created_at_utc || ""),
        optimistic: Boolean(message.optimistic),
        localDeliveryState: String(message.localDeliveryState || ""),
        localSequence: Number(message.localSequence || 0),
        anchorSessionSequence: Number(message.anchorSessionSequence || 0),
      };
    }).filter(function (message) {
      return Boolean(message.messageId && (message.sessionSequence || message.optimistic));
    });
  }

function mergeTranscriptMessages(thread, messages) {
    var reconciliation = window.UAChatDelivery.reconcileMessages(
      normalizeMessageList(thread.messages),
      normalizeMessageList(messages)
    );
    reconciliation.acknowledgedMessages.forEach(function (message) {
      shellState.deliveryTracker.acknowledge(message.clientRequestId);
      releaseMessageAttachmentPreviews(message);
    });
    if (reconciliation.acknowledgedMessages.length) {
      clearSystemError(thread, shellState.SYSTEM_ERROR_SOURCE.SEND_MESSAGE);
    }
    thread.messages = reconciliation.messages;
    thread.transientItems = window.UAChatDelivery.reconcileTransientItems(
      thread.transientItems,
      thread.messages
    );
  }

function optimisticMessage(
    clientRequestId,
    content,
    attachments,
    anchorSessionSequence
  ) {
    shellState.optimisticMessageSequence += 1;
    return {
      messageId: "optimistic:" + clientRequestId,
      clientRequestId: clientRequestId,
      sessionSequence: 0,
      eventSequence: 0,
      role: "user",
      content: content,
      attachments: attachments.map(normalizeAttachmentPayload),
      messageType: "user_message",
      messageLevel: "normal",
      createdAt: new Date().toISOString(),
      optimistic: true,
      localDeliveryState: "pending",
      localSequence: shellState.optimisticMessageSequence,
      anchorSessionSequence: Number(anchorSessionSequence || 0),
    };
  }

function markMessageNotDelivered(clientRequestId) {
    var affectedThread = sharedState.threads.find(function (thread) {
      return thread.messages.some(function (message) {
        if (!message.optimistic || message.clientRequestId !== clientRequestId) {
          return false;
        }
        message.localDeliveryState = "not_delivered";
        return true;
      });
    });
    if (affectedThread && affectedThread.id === sharedState.activeThreadId) {
      renderMessages();
    }
  }

function releaseMessageAttachmentPreviews(message) {
    var releasedUrls = Object.create(null);
    (Array.isArray(message.attachments) ? message.attachments : []).forEach(function (attachment) {
      [attachment.preview_url, attachment.url].forEach(function (value) {
        var url = String(value || "");
        if (url.indexOf("blob:") !== 0 || releasedUrls[url]) {
          return;
        }
        releasedUrls[url] = true;
        window.URL.revokeObjectURL(url);
      });
    });
  }

function clearThreadOptimisticDeliveries(thread) {
    (thread && Array.isArray(thread.messages) ? thread.messages : []).forEach(function (message) {
      if (!message.optimistic) {
        return;
      }
      shellState.deliveryTracker.acknowledge(message.clientRequestId);
      releaseMessageAttachmentPreviews(message);
    });
  }

function clearOptimisticDeliveries() {
    sharedState.threads.forEach(clearThreadOptimisticDeliveries);
    shellState.deliveryTracker.clear();
  }

function loadRuntimeSettings() {
    requestClient.fetch("/api/web-chat/settings", { cache: "no-store" }).then(function (response) {
      if (!response.ok) {
        throw new Error("settings unavailable");
      }
      return response.json();
    }).then(function (settings) {
      setAssistantName(sharedState, document, settings.assistant_name);
      var publicVersion = String(settings.version || "").trim().replace(/^v/i, "");
      shellState.versionLabel.textContent = publicVersion ? "v" + publicVersion : "";
      shellState.versionLabel.hidden = !publicVersion;
      if (Array.isArray(settings.commands)) {
        shellState.commandLines = settings.commands;
      }
      appearance.applyRuntimeAppearance(settings.appearance);
      diagnostics.applyRuntimeDebugSettings(settings.debug);
      renderMessages();
    }).catch(function (error) {
      console.warn("ua.web_chat: settings could not be loaded; the command guide stays empty", error);
      return null;
    });
  }

function configurePrincipalState(authPayload) {
    var canonicalUserId = String(authPayload.canonical_user_id || "").trim();
    var isBootstrapUser = Boolean(authPayload.is_bootstrap_user);
    var principalScope = canonicalUserId || (isBootstrapUser ? "bootstrap" : "");
    if (!principalScope) {
      throw new Error("The authenticated Web Chat session did not identify a user.");
    }
    attention.configure(principalScope);
    var keySuffix = encodeURIComponent(principalScope);
    sharedState.storageKey = "ua.webchat.ui." + keySuffix;
    sharedState.clientIdKey = "ua.webchat.client_id." + keySuffix;
    window.localStorage.removeItem("ua.webchat.threads");
    window.localStorage.removeItem("ua.webchat.threads." + keySuffix);
    sharedState.clientId = window.localStorage.getItem(sharedState.clientIdKey)
      || shellState.operationalIds.newId("browser");
    window.localStorage.setItem(sharedState.clientIdKey, sharedState.clientId);
    sharedState.threads = loadThreads();
    sharedState.activeThreadId = String(
      window.localStorage.getItem(sharedState.storageKey + ".selected")
      || (sharedState.threads[0] && sharedState.threads[0].id)
      || ""
    );
    signInGreeting.remember(authPayload);
    var displayName = String(authPayload.display_name || canonicalUserId || "Web Chat user");
    sharedState.accountNameLabel.textContent = canonicalUserId && displayName !== canonicalUserId
      ? displayName + " · " + canonicalUserId
      : displayName;
    // Every signed-in person has a folder to browse; the gateway decides what each one sees.
    authenticationState.settingsWorkspaceLink.hidden = false;
    // Only administrators get the Status link; the gateway checks again on every read.
    authenticationState.statusClient.get("/api/web-chat/status/access", "").then(function (payload) {
      authenticationState.settingsStatusLink.hidden = !(payload && payload.allowed === true);
    }).catch(function () {
      authenticationState.settingsStatusLink.hidden = true;
    });
    // The Knowledge link shows while the team's knowledge base is on.
    authenticationState.knowledgeClient.get("/api/web-chat/knowledge/access", "").then(function (payload) {
      var enabled = Boolean(payload && payload.enabled === true);
      authenticationState.settingsKnowledgeLink.hidden = !enabled;
      // On the chat page, the link and the cog count proposals waiting for this
      // person (ui/DESIGN.md, "Attention counts"); the Knowledge page keeps its own.
      var separatePage = authenticationState.objectivesMode || authenticationState.workspaceMode
        || authenticationState.statusMode || authenticationState.knowledgeMode;
      if (enabled && !separatePage) {
        // It asks in the open conversation, like Settings; with none open it
        // waits rather than starting a conversation just to count.
        sharedState.settingsControls.startKnowledgeCount(function () {
          var thread = activeThread();
          if (!thread || !thread.sessionKey) {
            return Promise.resolve(null);
          }
          return readPendingProposals(authenticationState.knowledgeClient, thread.sessionKey);
        });
      }
    }).catch(function () {
      authenticationState.settingsKnowledgeLink.hidden = true;
    });
  }

function loadThreads() {
    if (!sharedState.storageKey) {
      return [];
    }
    try {
      var parsed = JSON.parse(window.localStorage.getItem(sharedState.storageKey) || "[]");
      if (!Array.isArray(parsed)) {
        return [];
      }
      return parsed.map(function (thread) {
        var id = String(thread.id || shellState.operationalIds.newId("thread"));
        var title = String(thread.title || NEW_CHAT_TITLE);
        return {
          id: id,
          sessionKey: String(thread.sessionKey || thread.session_key || ""),
          conversationId: String(thread.conversationId || thread.conversation_id || ""),
          title: title,
          titleFull: String(thread.titleFull || thread.title_full || title),
          sessionAlias: String(thread.sessionAlias || thread.session_alias || ""),
          persisted: Boolean(thread.persisted),
          pendingReply: false,
          pendingReplyCount: 0,
          hasUnreadReply: Boolean(thread.hasUnreadReply || thread.has_unread_reply),
          unreadReplyLevel: String(thread.unreadReplyLevel || thread.unread_reply_level || "normal"),
          messages: [],
          historyBeforeCursor: "",
          hasMoreHistory: false,
          historyLoaded: false,
          transientError: null,
          transientItems: [],
          updatedAt: Number(thread.updatedAt || Date.now()),
        };
      });
    } catch (error) {
      return [];
    }
  }

function saveThreads() {
    if (!sharedState.storageKey) {
      return;
    }
    var uiThreads = sharedState.threads.map(function (thread) {
      return {
        id: thread.id,
        sessionKey: thread.sessionKey,
        conversationId: thread.conversationId,
        title: thread.title,
        titleFull: thread.titleFull,
        sessionAlias: thread.sessionAlias,
        persisted: thread.persisted,
        hasUnreadReply: thread.hasUnreadReply,
        unreadReplyLevel: thread.unreadReplyLevel,
        updatedAt: thread.updatedAt,
      };
    });
    window.localStorage.setItem(sharedState.storageKey, JSON.stringify(uiThreads));
    window.localStorage.setItem(sharedState.storageKey + ".selected", sharedState.activeThreadId);
  }

function createThread(seed) {
    var thread = threadFromSeed(seed);
    sharedState.threads.unshift(thread);
    saveThreads();
    return thread;
  }

function threadFromSeed(seed) {
    seed = seed || {};
    var id = String(seed.id || seed.sessionKey || seed.session_key || "");
    if (!id) {
      throw new Error("Server-created threads require a session identity.");
    }
    var messages = normalizeMessageList(seed.messages);
    var title = String(seed.title || NEW_CHAT_TITLE);
    return {
      id: id,
      sessionKey: String(seed.sessionKey || seed.session_key || ""),
      conversationId: String(seed.conversationId || seed.conversation_id || ""),
      title: title,
      titleFull: recoveredFullThreadTitle(seed, title, messages),
      sessionAlias: String(seed.sessionAlias || seed.session_alias || ""),
      persisted: Boolean(seed.persisted),
      pendingReply: false,
      pendingReplyCount: 0,
      hasUnreadReply: Boolean(seed.hasUnreadReply || seed.has_unread_reply),
      unreadReplyLevel: String(seed.unreadReplyLevel || seed.unread_reply_level || "normal"),
      messages: messages,
      historyBeforeCursor: String(seed.historyBeforeCursor || ""),
      hasMoreHistory: Boolean(seed.hasMoreHistory),
      historyLoaded: Boolean(seed.historyLoaded),
      transientError: null,
      transientItems: [],
      updatedAt: Number(seed.updatedAt || Date.now()),
    };
  }

function archiveThread(threadId) {
    var archivedThread = sharedState.threads.find(function (thread) {
      return thread.id === threadId;
    });
    if (!archivedThread || !archivedThread.sessionKey || !sharedState.authSessionActive) {
      return;
    }
    requestClient.fetch(
      "/api/web-chat/sessions/" + encodeURIComponent(archivedThread.sessionKey) + "/archive",
      { method: "POST", credentials: "same-origin" }
    ).then(function (response) {
      return parseApiResponse(response, "Archive failed.");
    }).then(function () {
      clearSystemError(archivedThread, shellState.SYSTEM_ERROR_SOURCE.ARCHIVE_THREAD);
      removeArchivedThread(threadId);
    }).catch(function (error) {
      appendSystemError(
        archivedThread,
        shellState.SYSTEM_ERROR_SOURCE.ARCHIVE_THREAD,
        error.message || "Archive failed."
      );
      renderMessages();
    });
  }

function removeArchivedThread(threadId) {
    var archivedThread = sharedState.threads.find(function (thread) {
      return thread.id === threadId;
    });
    clearThreadOptimisticDeliveries(archivedThread);
    sharedState.threads = sharedState.threads.filter(function (thread) {
      return thread.id !== threadId;
    });
    if (sharedState.threads.length === 0) {
      sharedState.activeThreadId = "";
    } else if (sharedState.activeThreadId === threadId) {
      sharedState.activeThreadId = sharedState.threads[0].id;
    }
    saveThreads();
    renderThreads();
    renderMessages();
    ensureActiveThreadHistory();
  }

function activeThread() {
    return sharedState.threads.find(function (thread) {
      return thread.id === sharedState.activeThreadId;
    }) || sharedState.threads[0];
  }

function threadForSession(sessionKey) {
    return sharedState.threads.find(function (thread) {
      return thread.sessionKey === sessionKey;
    });
  }

function setThreadPending(thread, isPending) {
    if (!thread) {
      return;
    }
    thread.pendingReply = Boolean(isPending);
  }

// pendingReply is this page's own wait for a reply it sent; serverBusy is the
// server's view, so pages on other devices show the same busy dot.
function threadIsBusy(thread) {
    return Boolean(thread && (thread.pendingReply || thread.serverBusy));
  }

// Live thread-list state rides on every event page because renames, archives,
// threads started elsewhere and running tasks write no transcript row.
function applyThreadStates(payload) {
    if (!sharedState.authSessionActive || typeof payload.threads_revision !== "string") {
      return;
    }
    var busySessions = new Set(Array.isArray(payload.busy_session_keys) ? payload.busy_session_keys : []);
    var busyChanged = false;
    sharedState.threads.forEach(function (thread) {
      var isBusy = busySessions.has(thread.sessionKey);
      if (Boolean(thread.serverBusy) !== isBusy) {
        thread.serverBusy = isBusy;
        busyChanged = true;
      }
    });
    // Redrawing the list replaces an open rename field; wait until it closes.
    // The revision stays stale, so the next page still reloads the list.
    if (sharedState.editingThreadId) {
      return;
    }
    if (payload.threads_revision !== sharedState.threadsRevision) {
      fetchSessions();
    } else if (busyChanged) {
      renderThreads();
    }
  }

function pendingReplyCount(thread) {
    return thread ? Math.max(0, Number(thread.pendingReplyCount || 0)) : 0;
  }

function startTerminalReplyWait(thread) {
    if (!thread) {
      return;
    }
    thread.pendingReplyCount = pendingReplyCount(thread) + 1;
    setThreadPending(thread, true);
  }

function finishTerminalReplyWait(thread) {
    if (!thread) {
      return;
    }
    thread.pendingReplyCount = Math.max(0, pendingReplyCount(thread) - 1);
    setThreadPending(thread, thread.pendingReplyCount > 0);
  }

function isReadingThread(thread) {
    return thread.id === sharedState.activeThreadId && !attention.away()
      && document.getElementById("chat-settings-overlay").hidden;
  }

function setThreadUnread(thread, isUnread, level) {
    if (!thread) {
      return;
    }
    thread.hasUnreadReply = Boolean(isUnread);
    thread.unreadReplyLevel = thread.hasUnreadReply && (normalizeMessageLevel(level) === "attention" || thread.unreadReplyLevel === "attention")
      ? "attention"
      : "normal";
  }

function normalizeMessageLevel(value) {
    var normalized = String(value || "normal").trim().toLowerCase();
    return ["attention", "warning", "error"].indexOf(normalized) !== -1 ? "attention" : "normal";
  }

function messageLevelFromPayload(payload) {
    return normalizeMessageLevel(payload && payload.message_level);
  }

function fullThreadTitle(thread) {
    if (!thread) {
      return NEW_CHAT_TITLE;
    }
    return normalizeThreadTitle(thread.sessionAlias || thread.titleFull || thread.title) || NEW_CHAT_TITLE;
  }

function isCompressedThreadTitle(value) {
    return /\.\.\.$/.test(String(value || "").trim()) || /…$/.test(String(value || "").trim());
  }

function firstUserMessageTitle(messages) {
    var values = Array.isArray(messages) ? messages : [];
    for (var index = 0; index < values.length; index += 1) {
      if (String(values[index].role || "") === "user") {
        var title = normalizeThreadTitle(values[index].content);
        if (title) {
          return title;
        }
      }
    }
    return "";
  }

function recoveredFullThreadTitle(source, title, messages) {
    var explicit = normalizeThreadTitle(source && (source.titleFull || source.title_full));
    if (explicit) {
      return explicit;
    }
    var normalizedTitle = normalizeThreadTitle(title);
    if (isCompressedThreadTitle(normalizedTitle)) {
      return firstUserMessageTitle(messages) || normalizedTitle || NEW_CHAT_TITLE;
    }
    return normalizedTitle || NEW_CHAT_TITLE;
  }

function hideThreadTitleTooltip() {
    if (shellState.threadTitleTooltipTimer) {
      lifecycle.clearTimeout(shellState.threadTitleTooltipTimer);
      shellState.threadTitleTooltipTimer = 0;
    }
    shellState.threadTitleTooltipOwner = null;
    if (shellState.threadTitleTooltip) {
      shellState.threadTitleTooltip.hidden = true;
      shellState.threadTitleTooltip.removeAttribute("style");
    }
  }

function ensureThreadTitleTooltip() {
    if (shellState.threadTitleTooltip) {
      return shellState.threadTitleTooltip;
    }
    shellState.threadTitleTooltip = document.createElement("div");
    shellState.threadTitleTooltip.className = "thread-title-tooltip";
    shellState.threadTitleTooltip.id = "thread-title-tooltip";
    shellState.threadTitleTooltip.setAttribute("role", "tooltip");
    shellState.threadTitleTooltip.hidden = true;
    document.body.appendChild(shellState.threadTitleTooltip);
    return shellState.threadTitleTooltip;
  }

function showThreadTitleTooltip(button, title) {
    if (!button || shellState.threadTitleTooltipOwner !== button || button.scrollWidth <= button.clientWidth) {
      return;
    }
    var tooltip = ensureThreadTitleTooltip();
    tooltip.textContent = title;
    tooltip.hidden = false;
    var rect = button.getBoundingClientRect();
    var tooltipRect = tooltip.getBoundingClientRect();
    var left = Math.min(Math.max(rect.left, 12), window.innerWidth - tooltipRect.width - 12);
    var top = rect.bottom + 8;
    if (top + tooltipRect.height > window.innerHeight - 12) {
      top = Math.max(12, rect.top - tooltipRect.height - 8);
    }
    tooltip.style.left = left + "px";
    tooltip.style.top = top + "px";
  }

function scheduleThreadTitleTooltip(button, title) {
    hideThreadTitleTooltip();
    shellState.threadTitleTooltipOwner = button;
    shellState.threadTitleTooltipTimer = lifecycle.timeout(function () {
      shellState.threadTitleTooltipTimer = 0;
      showThreadTitleTooltip(button, title);
    }, 1000);
  }

function applyCredentialActions(actions) {
    var changed = false;
    (Array.isArray(actions) ? actions : []).forEach(function (payload) {
      var thread = threadForSession(String(payload.session_key || ""));
      if (!thread) {
        return;
      }
      var item = window.UAChatDelivery.normalizeTransientItem(payload, window.location.origin);
      if (!item) {
        return;
      }
      // Event pages repeat live actions so a page reload can recover the form. A
      // dismissed action stays dismissed for this browser session.
      thread.dismissedTransientItems = thread.dismissedTransientItems || new Set();
      if (thread.dismissedTransientItems.has(item.itemId)) {
        return;
      }
      if (thread.transientItems.some(function (current) { return current.itemId === item.itemId; })) {
        return;
      }
      thread.transientItems = window.UAChatDelivery.reconcileTransientItems(
        window.UAChatDelivery.upsertTransientItem(thread.transientItems, item),
        thread.messages
      );
      changed = true;
    });
    if (changed) {
      renderMessages();
    }
  }

function applyTranscriptEvent(event) {
    var sessionKey = String(event.session_key || "");
    if (!sessionKey || !event.message_id) {
      return;
    }
    var thread = threadForSession(sessionKey);
    if (!thread) {
      thread = createThread({
        id: sessionKey.replace(/[^A-Za-z0-9_-]+/g, "_"),
        sessionKey: sessionKey,
        conversationId: event.conversation_id || "",
        title: NEW_CHAT_TITLE,
        titleFull: NEW_CHAT_TITLE,
        persisted: true,
        historyLoaded: false,
      });
    }
    var isFocusedThread = thread.id === sharedState.activeThreadId;
    thread.sessionKey = sessionKey;
    thread.conversationId = String(event.conversation_id || thread.conversationId || "");
    thread.persisted = true;
    // Durable live delivery includes the accepted user row and can race the POST that
    // is still waiting for the agent's terminal reply.  The initiating request
    // owns pending-reply state; transcript events only merge presentation rows.
    if (attention.receive(event, thread)) {
      setThreadUnread(thread, !isReadingThread(thread), messageLevelFromPayload(event));
    }
    // An event is not a history page. Unopened threads keep metadata/unread
    // state and load their newest page when selected.
    if (thread.historyLoaded || isFocusedThread || thread.historyRequest) {
      mergeTranscriptMessages(thread, [event]);
    }
    thread.updatedAt = Date.now();
    sharedState.threads.sort(function (left, right) {
      return right.updatedAt - left.updatedAt;
    });
    saveThreads();
    renderThreads();
    if (isFocusedThread) {
      renderMessages();
    }
  }

function revealPassword(input, button) {
    shellState.credentialControls.toggleReveal(input, button);
  }

function trashIconMarkup() {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"></path><path d="M8 6V4h8v2"></path><path d="m19 6-1 14H6L5 6"></path><path d="M10 11v5"></path><path d="M14 11v5"></path></svg>';
  }

function pencilIconMarkup() {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m18 2 4 4-12 12-5 1 1-5L18 2Z"></path><path d="m14 6 4 4"></path></svg>';
  }

function checkIconMarkup() {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m20 6-11 11-5-5"></path></svg>';
  }

function closeIconMarkup() {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"></path><path d="m6 6 12 12"></path></svg>';
  }

function copyIconMarkup() {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>';
  }

function fileIconMarkup() {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"></path><path d="M14 2v6h6"></path></svg>';
  }

function formatFileSize(bytes) {
    var value = Number(bytes || 0);
    if (!value) {
      return "0 B";
    }
    var units = ["B", "KB", "MB", "GB"];
    var unitIndex = 0;
    while (value >= 1024 && unitIndex < units.length - 1) {
      value = value / 1024;
      unitIndex += 1;
    }
    return (unitIndex === 0 ? String(Math.round(value)) : value.toFixed(value >= 10 ? 1 : 2)) + " " + units[unitIndex];
  }

function inferAttachmentKind(fileOrAttachment) {
    var kind = String(fileOrAttachment.kind || "").toLowerCase();
    if (kind) {
      return kind;
    }
    var type = String(fileOrAttachment.type || fileOrAttachment.content_type || "").toLowerCase();
    if (type.indexOf("image/") === 0) {
      return "image";
    }
    if (type.indexOf("audio/") === 0) {
      return "audio";
    }
    if (type.indexOf("video/") === 0) {
      return "video";
    }
    return "document";
  }

function normalizeAttachmentPayload(attachment) {
    return {
      attachment_id: String(attachment.attachment_id || attachment.id || ""),
      filename: String(attachment.filename || attachment.name || "attachment"),
      content_type: String(attachment.content_type || attachment.type || "application/octet-stream"),
      kind: inferAttachmentKind(attachment),
      size_bytes: Number(attachment.size_bytes || attachment.size || 0),
      sha256: String(attachment.sha256 || ""),
      url: String(attachment.url || attachment.preview_url || ""),
      preview_url: String(attachment.preview_url || attachment.url || ""),
    };
  }

function renderAttachmentList(container, attachments) {
    var normalized = (Array.isArray(attachments) ? attachments : []).map(normalizeAttachmentPayload);
    if (!normalized.length) {
      return;
    }
    var rail = document.createElement("div");
    rail.className = "message-attachments";
    normalized.forEach(function (attachment) {
      var href = attachment.url || attachment.preview_url || "";
      if (attachment.kind === "image" && href) {
        var link = document.createElement("a");
        link.className = "message-image-attachment";
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener";
        link.title = attachment.filename;
        var image = document.createElement("img");
        image.src = href;
        image.alt = attachment.filename;
        link.appendChild(image);
        rail.appendChild(link);
        return;
      }
      if (attachment.kind === "audio" && href) {
        var audio = document.createElement("audio");
        audio.className = "message-media-attachment";
        audio.controls = true;
        audio.src = href;
        rail.appendChild(audio);
        return;
      }
      if (attachment.kind === "video" && href) {
        var video = document.createElement("video");
        video.className = "message-media-attachment";
        video.controls = true;
        video.src = href;
        rail.appendChild(video);
        return;
      }
      var chip = document.createElement(href ? "a" : "span");
      chip.className = "message-file-attachment";
      if (href) {
        chip.href = href;
        chip.target = "_blank";
        chip.rel = "noopener";
      }
      var icon = document.createElement("span");
      icon.className = "message-file-icon";
      icon.innerHTML = fileIconMarkup();
      var label = document.createElement("span");
      label.className = "message-file-label";
      label.textContent = attachment.filename;
      var meta = document.createElement("span");
      meta.className = "message-file-meta";
      meta.textContent = [attachment.kind, formatFileSize(attachment.size_bytes)].filter(Boolean).join(" · ");
      chip.appendChild(icon);
      chip.appendChild(label);
      chip.appendChild(meta);
      rail.appendChild(chip);
    });
    container.appendChild(rail);
  }

function addPendingFiles(files) {
    Array.prototype.slice.call(files || []).forEach(function (file) {
      if (!file || !file.name) {
        return;
      }
      var item = {
        id: "pending_" + (++shellState.pendingAttachmentSeq),
        file: file,
        filename: file.name,
        content_type: file.type || "application/octet-stream",
        kind: inferAttachmentKind(file),
        size_bytes: file.size || 0,
        preview_url: "",
      };
      if (item.kind === "image") {
        item.preview_url = window.URL.createObjectURL(file);
      }
      shellState.pendingAttachments.push(item);
    });
    renderPendingAttachments();
  }

function removePendingAttachment(id) {
    var removed = shellState.pendingAttachments.find(function (item) { return item.id === id; });
    shellState.pendingAttachments = shellState.pendingAttachments.filter(function (item) { return item.id !== id; });
    if (removed && removed.preview_url) {
      window.URL.revokeObjectURL(removed.preview_url);
    }
    renderPendingAttachments();
  }

function clearPendingAttachments() {
    shellState.pendingAttachments.forEach(function (item) {
      if (item.preview_url) {
        window.URL.revokeObjectURL(item.preview_url);
      }
    });
    shellState.pendingAttachments = [];
    renderPendingAttachments();
  }

function detachPendingAttachmentsForSend() {
    var attachments = shellState.pendingAttachments.slice();
    shellState.pendingAttachments = [];
    renderPendingAttachments();
    return attachments;
  }

function pendingAttachmentsForMessage() {
    return shellState.pendingAttachments.map(function (item) {
      return normalizeAttachmentPayload(item);
    });
  }

function renderPendingAttachments() {
    if (!shellState.attachmentPreview) {
      return;
    }
    shellState.attachmentPreview.innerHTML = "";
    shellState.attachmentPreview.hidden = shellState.pendingAttachments.length === 0;
    shellState.pendingAttachments.forEach(function (item) {
      var chip = document.createElement("div");
      chip.className = "composer-attachment";
      if (item.kind === "image" && item.preview_url) {
        var image = document.createElement("img");
        image.className = "composer-attachment-thumb";
        image.src = item.preview_url;
        image.alt = item.filename;
        chip.appendChild(image);
      } else {
        var icon = document.createElement("span");
        icon.className = "composer-attachment-icon";
        icon.innerHTML = fileIconMarkup();
        chip.appendChild(icon);
      }
      var text = document.createElement("span");
      text.className = "composer-attachment-text";
      var name = document.createElement("strong");
      name.textContent = item.filename;
      var meta = document.createElement("small");
      meta.textContent = [item.kind, formatFileSize(item.size_bytes)].join(" · ");
      text.appendChild(name);
      text.appendChild(meta);
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "composer-attachment-remove";
      remove.innerHTML = closeIconMarkup();
      remove.title = "Remove attachment";
      remove.setAttribute("aria-label", "Remove " + item.filename);
      lifecycle.listen(remove, "click", function () {
        removePendingAttachment(item.id);
      });
      chip.appendChild(text);
      chip.appendChild(remove);
      shellState.attachmentPreview.appendChild(chip);
    });
  }

function buildChatRequestBody(content, thread, attachments, clientRequestId) {
    if (!attachments.length) {
      return {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: content,
          session_key: thread.sessionKey,
          conversation_id: thread.conversationId,
          client_request_id: clientRequestId,
        }),
      };
    }
    var formData = new window.FormData();
    formData.append("message", content);
    formData.append("session_key", thread.sessionKey);
    formData.append("conversation_id", thread.conversationId);
    formData.append("client_request_id", clientRequestId);
    attachments.forEach(function (item) {
      formData.append("attachments[]", item.file, item.filename);
    });
    return { headers: {}, body: formData };
  }

function normalizeThreadTitle(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

function copyTextToClipboard(value) {
    return shellState.credentialControls.copyTextToClipboard(value);
  }

function parseApiResponse(response, fallbackMessage, requireCurrentRequest) {
    return response.json().catch(function () {
      return {};
    }).then(function (payload) {
      if (requireCurrentRequest) requireCurrentRequest();
      if (!response.ok) {
        authentication.handleUnauthorizedResponse(response);
        throw new Error(payload.message || payload.error || response.statusText || fallbackMessage);
      }
      return payload;
    });
  }

function fetchSessions(options) {
    options = options || {};
    if (!sharedState.authSessionActive) {
      return Promise.resolve();
    }
    var authGeneration = sharedState.authSessionGeneration;
    if (shellState.sessionsRequest && shellState.sessionsRequestGeneration === authGeneration) {
      if (options.resetHistory && !shellState.sessionsResetting) {
        return shellState.sessionsRequest.then(function () {
          if (sharedState.authSessionActive && authGeneration === sharedState.authSessionGeneration) {
            return fetchSessions(options);
          }
        });
      }
      return shellState.sessionsRequest;
    }
    shellState.sessionsRequestGeneration = authGeneration;
    shellState.sessionsResetting = Boolean(options.resetHistory);
    var request = requestClient.fetch("/api/web-chat/sessions", {
      signal: AbortSignal.timeout(10000),
      cache: "no-store",
      credentials: "same-origin",
    }).then(function (response) {
      if (!sharedState.authSessionActive || authGeneration !== sharedState.authSessionGeneration) {
        return null;
      }
      if (!response.ok) {
        authentication.handleUnauthorizedResponse(response);
        throw new Error("Unable to load thread history.");
      }
      return response.json();
    }).then(function (payload) {
      if (!sharedState.authSessionActive || authGeneration !== sharedState.authSessionGeneration) {
        return;
      }
      if (!payload || !Array.isArray(payload.sessions) || !payload.event_cursor) {
        throw new Error("Unable to load valid thread history.");
      }
      sharedState.threadsRevision = String(payload.threads_revision || "");
      // Metadata refresh must not skip events that arrived since the last applied event page.
      if (!sharedState.asyncEventCursor || options.resetHistory) {
        sharedState.asyncEventCursor = String(payload.event_cursor || "");
      }
      var previousBySession = {};
      sharedState.threads.forEach(function (thread) {
        previousBySession[thread.sessionKey] = thread;
      });
      sharedState.threads = payload.sessions.map(function (session) {
        var sessionKey = String(session.session_key || "");
        if (!sessionKey) {
          return null;
        }
        var existing = previousBySession[sessionKey];
        var alias = String(session.session_alias || "");
        // The server's name for the thread is fixed once given: a rename,
        // else its first ordinary message (later messages never rename it).
        var title = String(alias || normalizeThreadTitle(session.title) || NEW_CHAT_TITLE);
        if (existing) {
          if (options.resetHistory) {
            existing.historyRevision = (existing.historyRevision || 0) + 1;
            existing.historyLoaded = false;
            existing.historyBeforeCursor = "";
            existing.hasMoreHistory = false;
            existing.messages = existing.messages.filter(function (message) { return message.optimistic; });
          }
          existing.title = title;
          existing.titleFull = title;
          existing.sessionAlias = alias;
          existing.conversationId = String(session.conversation_id || existing.conversationId || "");
          existing.updatedAt = Date.parse(session.updated_at || "") || existing.updatedAt || Date.now();
          existing.persisted = true;
          return existing;
        }
        return threadFromSeed({
          id: sessionKey.replace(/[^A-Za-z0-9_-]+/g, "_"),
          sessionKey: sessionKey,
          conversationId: String(session.conversation_id || ""),
          title: title,
          titleFull: title,
          sessionAlias: alias,
          persisted: true,
          updatedAt: Date.parse(session.updated_at || "") || Date.now(),
        });
      }).filter(Boolean);
      sharedState.threads.sort(function (left, right) {
        return right.updatedAt - left.updatedAt;
      });
      if (!sharedState.activeThreadId || !sharedState.threads.find(function (thread) { return thread.id === sharedState.activeThreadId; })) {
        sharedState.activeThreadId = sharedState.threads[0] ? sharedState.threads[0].id : "";
      }
      saveThreads();
      // Opening Chat is a read. Create a conversation only for an explicit
      // New Chat action or the first submitted message.
      renderThreads();
      return ensureActiveThreadHistory();
    }).then(function () {
      if (!sharedState.authSessionActive || authGeneration !== sharedState.authSessionGeneration) {
        return;
      }
      if (clearSystemError(null, shellState.SYSTEM_ERROR_SOURCE.FETCH_SESSIONS)) {
        renderMessages();
      }
      return true;
    }).catch(function (error) {
      if (!sharedState.authSessionActive || authGeneration !== sharedState.authSessionGeneration) {
        return;
      }
      appendSystemError(
        null,
        shellState.SYSTEM_ERROR_SOURCE.FETCH_SESSIONS,
        error.message || "Unable to load durable thread history."
      );
      renderMessages();
      return false;
    }).finally(function () {
      if (shellState.sessionsRequest === request) {
        shellState.sessionsRequest = null;
      }
    });
    shellState.sessionsRequest = request;
    return request;
  }

function createServerThread() {
    var authGeneration = sharedState.authSessionGeneration;
    if (!sharedState.authSessionActive) { return Promise.resolve(null); }
    if (shellState.threadCreationRequest && shellState.threadCreationGeneration === authGeneration) {
      return shellState.threadCreationRequest;
    }
    var previousThread = activeThread();
    shellState.threadCreationGeneration = authGeneration;
    var request = requestClient.fetch("/api/web-chat/sessions", {
      method: "POST",
      credentials: "same-origin",
    }).then(function (response) {
      return parseApiResponse(response, "Unable to create a chat thread.");
    }).then(function (payload) {
      if (!sharedState.authSessionActive || sharedState.authSessionGeneration !== authGeneration) { return null; }
      var session = payload.session || {};
      var sessionKey = String(session.session_key || "");
      if (!sessionKey) {
        throw new Error("The server did not return a chat session.");
      }
      var thread = threadForSession(sessionKey) || createThread({
        id: sessionKey.replace(/[^A-Za-z0-9_-]+/g, "_"),
        sessionKey: sessionKey,
        conversationId: String(session.conversation_id || ""),
        title: NEW_CHAT_TITLE,
        titleFull: NEW_CHAT_TITLE,
        persisted: true,
        historyLoaded: true,
      });
      sharedState.activeThreadId = thread.id;
      clearSystemError(previousThread, shellState.SYSTEM_ERROR_SOURCE.CREATE_THREAD);
      clearSystemError(null, shellState.SYSTEM_ERROR_SOURCE.CREATE_THREAD);
      saveThreads();
      renderThreads();
      renderMessages();
      return thread;
    }).finally(function () {
      if (shellState.threadCreationRequest === request) { shellState.threadCreationRequest = null; }
    });
    shellState.threadCreationRequest = request;
    return request;
  }

function hydrateThread(thread, beforeCursor) {
    if (!thread || !thread.sessionKey) {
      return Promise.resolve();
    }
    var authGeneration = sharedState.authSessionGeneration;
    var revision = thread.historyRevision || 0;
    if (thread.historyRequest && thread.historyRequest.authGeneration === authGeneration && thread.historyRequest.revision === revision) {
      return thread.historyRequest.promise;
    }
    var initialPage = !thread.historyLoaded;
    var request = { authGeneration: authGeneration, revision: revision };
    var query = "?limit=50";
    if (beforeCursor) {
      query += "&before=" + encodeURIComponent(beforeCursor);
    }
    request.promise = requestClient.fetch(
      "/api/web-chat/sessions/" + encodeURIComponent(thread.sessionKey) + "/messages" + query,
      { cache: "no-store", credentials: "same-origin", signal: AbortSignal.timeout(10000) }
    ).then(function (response) {
      if (!historyRequestIsCurrent(thread, authGeneration, revision)) {
        return null;
      }
      return parseApiResponse(response, "Unable to load durable thread history.");
    }).then(function (payload) {
      if (!payload || !historyRequestIsCurrent(thread, authGeneration, revision)) {
        return;
      }
      mergeTranscriptMessages(thread, payload.messages || []);
      if (initialPage || beforeCursor) {
        thread.historyBeforeCursor = String(payload.before_cursor || "");
        thread.hasMoreHistory = Boolean(payload.has_more);
      }
      thread.historyLoaded = true;
      clearSystemError(thread, shellState.SYSTEM_ERROR_SOURCE.HYDRATE_THREAD);
      saveThreads();
    }).catch(function (error) {
      if (historyRequestIsCurrent(thread, authGeneration, revision)) {
        appendSystemError(thread, shellState.SYSTEM_ERROR_SOURCE.HYDRATE_THREAD,
          error.message || "Unable to load durable thread history.");
      }
    }).finally(function () {
      if (thread.historyRequest === request) {
        thread.historyRequest = null;
      }
      if (historyRequestIsCurrent(thread, authGeneration, revision) && thread === activeThread()) {
        renderMessages({ prepend: Boolean(beforeCursor), scrollToBottom: initialPage });
      }
    });
    thread.historyRequest = request;
    return request.promise;
  }

function historyRequestIsCurrent(thread, authGeneration, revision) {
    return sharedState.authSessionActive
      && sharedState.authSessionGeneration === authGeneration
      && (thread.historyRevision || 0) === revision
      && sharedState.threads.indexOf(thread) !== -1;
  }

function ensureActiveThreadHistory() {
    var thread = activeThread();
    return thread && !thread.historyLoaded ? hydrateThread(thread, "") : Promise.resolve();
  }

function loadEarlierHistory() {
    var thread = activeThread();
    if (!thread || !thread.historyLoaded || !thread.hasMoreHistory || !thread.historyBeforeCursor || thread.historyRequest) {
      return Promise.resolve();
    }
    var request = hydrateThread(thread, thread.historyBeforeCursor);
    renderMessages();
    return request;
  }

function renderThreads() {
    hideThreadTitleTooltip();
    sharedState.threadList.innerHTML = "";
    var unreadStateChanged = false;
    sharedState.threads.forEach(function (thread) {
      if (isReadingThread(thread) && thread.hasUnreadReply) {
        setThreadUnread(thread, false);
        unreadStateChanged = true;
      }
      var row = document.createElement("div");
      row.className = "thread-row"
        + (thread.id === sharedState.activeThreadId ? " is-active" : "")
        + (thread.id === sharedState.editingThreadId ? " is-renaming" : "")
        + (threadIsBusy(thread) ? " is-pending" : "")
        + (thread.hasUnreadReply ? " has-unread" : "")
        + (thread.hasUnreadReply && thread.unreadReplyLevel === "attention" ? " has-attention" : "");
      if (thread.id === sharedState.editingThreadId) {
        var form = document.createElement("form");
        form.className = "thread-rename-form";
        var input = document.createElement("input");
        input.className = "thread-rename-input";
        input.type = "text";
        input.maxLength = 80;
        input.value = fullThreadTitle(thread);
        input.setAttribute("aria-label", "Thread name");
        form.appendChild(input);
        lifecycle.listen(form, "submit", function (event) {
          event.preventDefault();
          renameThread(thread.id, input.value);
        });
        row.appendChild(form);
        lifecycle.timeout(function () {
          input.focus();
          input.select();
        }, 0);
      } else {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "thread-button";
        button.textContent = fullThreadTitle(thread);
        lifecycle.listen(button, "mouseenter", function () {
          scheduleThreadTitleTooltip(button, fullThreadTitle(thread));
        });
        lifecycle.listen(button, "mouseleave", hideThreadTitleTooltip);
        lifecycle.listen(button, "focus", function () {
          scheduleThreadTitleTooltip(button, fullThreadTitle(thread));
        });
        lifecycle.listen(button, "blur", hideThreadTitleTooltip);
        lifecycle.listen(button, "click", function () {
          sharedState.activeThreadId = thread.id;
          sharedState.editingThreadId = "";
          setThreadUnread(thread, false);
          hideThreadTitleTooltip();
          saveThreads();
          renderThreads();
          renderMessages();
          ensureActiveThreadHistory();
        });
        row.appendChild(button);
      }
      var pendingIndicator = document.createElement("span");
      pendingIndicator.className = "thread-active-indicator";
      if (threadIsBusy(thread)) {
        pendingIndicator.setAttribute("aria-label", "Agent is replying in this thread");
      } else if (thread.hasUnreadReply && thread.unreadReplyLevel === "attention") {
        pendingIndicator.setAttribute("aria-label", "Reply needs attention in this thread");
      } else if (thread.hasUnreadReply) {
        pendingIndicator.setAttribute("aria-label", "New reply in this thread");
      } else {
        pendingIndicator.setAttribute("aria-hidden", "true");
      }
      row.appendChild(pendingIndicator);
      var renameButton = document.createElement("button");
      renameButton.type = thread.id === sharedState.editingThreadId ? "submit" : "button";
      renameButton.className = "thread-action-button thread-rename-button";
      renameButton.innerHTML = thread.id === sharedState.editingThreadId ? checkIconMarkup() : pencilIconMarkup();
      renameButton.title = thread.id === sharedState.editingThreadId ? "Save thread name" : "Rename thread";
      renameButton.setAttribute("aria-label", (thread.id === sharedState.editingThreadId ? "Save thread name for " : "Rename ") + fullThreadTitle(thread));
      lifecycle.listen(renameButton, "click", function (event) {
        event.stopPropagation();
        if (thread.id === sharedState.editingThreadId) {
          var form = row.querySelector(".thread-rename-form");
          if (form) {
            form.requestSubmit();
          }
          return;
        }
        sharedState.editingThreadId = thread.id;
        hideThreadTitleTooltip();
        renderThreads();
      });
      var archiveButton = document.createElement("button");
      archiveButton.type = "button";
      archiveButton.className = "thread-action-button thread-delete-button";
      archiveButton.innerHTML = thread.id === sharedState.editingThreadId ? closeIconMarkup() : trashIconMarkup();
      archiveButton.title = thread.id === sharedState.editingThreadId ? "Cancel rename" : "Archive thread";
      archiveButton.setAttribute("aria-label", (thread.id === sharedState.editingThreadId ? "Cancel rename for " : "Archive ") + fullThreadTitle(thread));
      lifecycle.listen(archiveButton, "click", function (event) {
        event.stopPropagation();
        if (thread.id === sharedState.editingThreadId) {
          sharedState.editingThreadId = "";
          renderThreads();
          return;
        }
        archiveThread(thread.id);
      });
      row.appendChild(renameButton);
      row.appendChild(archiveButton);
      sharedState.threadList.appendChild(row);
    });
    // Once the server says there is no thread yet, one unsaved row stands
    // for the chat the first message will start (owner's request,
    // 2026-10-09). Opening Web Chat still creates nothing.
    if (!sharedState.threads.length && sharedState.threadsRevision) {
      sharedState.threadList.appendChild(unsavedThreadRow());
    }
    if (unreadStateChanged) {
      saveThreads();
    }
    attention.refresh();
  }

// The unsaved "New Chat" row: selected, with no rename or archive, since
// there is nothing to keep yet. Choosing it puts the cursor in the composer.
function unsavedThreadRow() {
    var row = document.createElement("div");
    row.className = "thread-row is-active is-unsaved";
    var button = document.createElement("button");
    button.type = "button";
    button.className = "thread-button";
    button.textContent = NEW_CHAT_TITLE;
    button.setAttribute("aria-current", "true");
    lifecycle.listen(button, "click", function () {
      sharedState.messageInput.focus();
    });
    row.appendChild(button);
    return row;
  }

function persistThreadAlias(thread) {
    if (!sharedState.authSessionActive || !thread.persisted || !thread.sessionKey || !thread.sessionAlias) {
      return Promise.resolve({ persisted: false });
    }
    return requestClient.fetch("/api/web-chat/sessions/" + encodeURIComponent(thread.sessionKey), {
      method: "PATCH",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        session_alias: thread.sessionAlias,
      }),
    }).then(function (response) {
      return response.text().then(function (body) {
        var payload = {};
        try {
          payload = body ? JSON.parse(body) : {};
        } catch (error) {
          payload = {};
        }
        if (!response.ok) {
          throw new Error(payload.message || payload.error || body || response.statusText || "Rename failed.");
        }
        return payload;
      });
    });
  }

function renameThread(threadId, value) {
    var thread = sharedState.threads.find(function (candidate) {
      return candidate.id === threadId;
    });
    if (!thread) {
      sharedState.editingThreadId = "";
      return;
    }
    var title = normalizeThreadTitle(value);
    if (!title) {
      return;
    }
    var previousTitle = thread.title;
    var previousTitleFull = thread.titleFull;
    var previousAlias = thread.sessionAlias;
    thread.title = title;
    thread.titleFull = title;
    thread.sessionAlias = title;
    thread.updatedAt = Date.now();
    sharedState.editingThreadId = "";
    saveThreads();
    renderThreads();
    if (!thread.persisted) {
      return;
    }
    persistThreadAlias(thread).then(function (payload) {
      thread.sessionAlias = String(payload.session_alias || thread.sessionAlias || "");
      thread.title = thread.sessionAlias || thread.title;
      thread.titleFull = thread.sessionAlias || thread.titleFull || thread.title;
      clearSystemError(thread, shellState.SYSTEM_ERROR_SOURCE.RENAME_THREAD);
      saveThreads();
      renderThreads();
      renderMessages();
    }).catch(function (error) {
      thread.title = previousTitle;
      thread.titleFull = previousTitleFull;
      thread.sessionAlias = previousAlias;
      appendSystemError(
        thread,
        shellState.SYSTEM_ERROR_SOURCE.RENAME_THREAD,
        error.message || "Rename failed."
      );
      saveThreads();
      renderThreads();
      renderMessages();
    });
  }

function uiElement(tagName, className, text) {
    var node = document.createElement(tagName);
    if (className) {
      node.className = className;
    }
    if (typeof text === "string") {
      node.textContent = text;
    }
    return node;
  }

function commandDefinition(line) {
    if (!line || typeof line !== "object") {
      return null;
    }
    var syntax = String(line.syntax || "").trim();
    var documentationUrl = String(line.documentation_url || "").trim();
    if (syntax.charAt(0) !== "/" || !documentationUrl.startsWith("/docs/")) {
      return null;
    }
    return {
      syntax: syntax,
      description: String(line.description || "").trim(),
      documentationUrl: documentationUrl,
      documentationLabel: String(line.documentation_label || "").trim(),
    };
  }

function commandDocumentationLink(definition, className, text, accessibleName) {
    var link = uiElement("a", className, text);
    link.href = definition.documentationUrl;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.setAttribute("aria-label", accessibleName);
    return link;
  }

function commandInvocation(definition) {
    var invocation = uiElement("span", "command-guide-invocation");
    var syntaxMatch = definition.syntax.match(/^(\/\S+)(?:\s+(.+))?$/);
    var commandName = syntaxMatch ? syntaxMatch[1] : definition.syntax;
    var argumentsText = syntaxMatch ? String(syntaxMatch[2] || "") : "";
    invocation.appendChild(commandDocumentationLink(
      definition,
      "command-guide-name",
      commandName,
      "Open documentation for " + commandName
    ));
    if (argumentsText) {
      var argumentsNode = uiElement("span", "command-guide-arguments");
      var normalized = argumentsText
        .replace(/\|/g, " | ")
        .replace(/\[/g, "[ ")
        .replace(/\]/g, " ]")
        .replace(/\s+/g, " ")
        .trim();
      var tokens = normalized.match(/<[^>]+>|\||\[|\]|[^\s]+/g) || [];
      tokens.forEach(function (token, index) {
        if (index) {
          argumentsNode.appendChild(document.createTextNode(" "));
        }
        var tokenClass = "is-option";
        if (token === "[" || token === "]") {
          tokenClass = "is-group";
        } else if (token === "|") {
          tokenClass = "is-separator";
        } else if (token === "...") {
          tokenClass = "is-ellipsis";
        } else if (/^<[^>]+>$/.test(token)) {
          tokenClass = "is-placeholder";
        }
        argumentsNode.appendChild(uiElement(
          "span",
          "command-guide-token " + tokenClass,
          token
        ));
      });
      invocation.appendChild(document.createTextNode(" "));
      invocation.appendChild(argumentsNode);
    }
    if (definition.documentationLabel) {
      invocation.appendChild(document.createTextNode(" "));
      invocation.appendChild(commandDocumentationLink(
        definition,
        "command-guide-doc-link",
        "(" + definition.documentationLabel + ")",
        definition.documentationLabel + " for " + commandName
      ));
    }
    return invocation;
  }

function createWelcomeGuidance() {
    var guidance = uiElement("aside", "message-guidance");
    guidance.setAttribute("aria-label", "Web Chat guidance");
    guidance.appendChild(uiElement("strong", "message-guidance-title", "Web Chat guide"));
    guidance.appendChild(uiElement(
      "p",
      "command-guide-intro",
      "Start a conversation with your agent team."
    ));
    var commandList = uiElement("ul", "command-guide-list");
    shellState.commandLines.forEach(function (line) {
      var definition = commandDefinition(line);
      if (!definition) {
        return;
      }
      var item = uiElement("li", "command-guide-item");
      item.appendChild(commandInvocation(definition));
      item.appendChild(uiElement(
        "span",
        "command-guide-description",
        definition.description
      ));
      commandList.appendChild(item);
    });
    guidance.appendChild(commandList);
    return guidance;
  }

function parsedTimestamp(value) {
    var date = new Date(value || "");
    return Number.isNaN(date.getTime()) ? null : date;
  }

function formatShortLocalTimestamp(value) {
    var date = parsedTimestamp(value);
    if (!date) {
      return "";
    }
    var now = new Date();
    var sameDay = date.getFullYear() === now.getFullYear()
      && date.getMonth() === now.getMonth()
      && date.getDate() === now.getDate();
    var options = {
      hour: "numeric",
      minute: "2-digit",
    };
    if (!sameDay) {
      options.month = "short";
      options.day = "numeric";
      if (date.getFullYear() !== now.getFullYear()) {
        options.year = "numeric";
      }
    }
    return new window.Intl.DateTimeFormat(undefined, options).format(date);
  }

function formatFullLocalTimestamp(value) {
    var date = parsedTimestamp(value);
    if (!date) {
      return "";
    }
    return new window.Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short",
    }).format(date);
  }

function timestampElement(value, className) {
    var shortValue = formatShortLocalTimestamp(value);
    if (!shortValue) {
      return null;
    }
    var time = uiElement("time", className || "", shortValue);
    time.dateTime = String(value || "");
    time.setAttribute("aria-label", formatFullLocalTimestamp(value));
    return time;
  }

function createMessageBubble(message) {
    var bubble = document.createElement("article");
    bubble.setAttribute("data-message-id", message.messageId);
    bubble.className = "message-bubble " + (message.role === "user" ? "is-user" : message.role === "system" ? "is-system" : "is-assistant");
    var header = document.createElement("div");
    header.className = "message-header";
    var identity = uiElement("div", "message-identity");
    var role = uiElement("strong", "message-role");
    role.textContent = message.role === "user" ? "You" : message.role === "system" ? "System" : sharedState.assistantName;
    var copyButton = document.createElement("button");
    copyButton.type = "button";
    copyButton.className = "message-copy-button";
    copyButton.innerHTML = copyIconMarkup();
    copyButton.title = "Copy raw message";
    copyButton.setAttribute("aria-label", "Copy raw " + role.textContent + " message");
    lifecycle.listen(copyButton, "click", function () {
      copyTextToClipboard(message.content).then(function () {
        copyButton.classList.add("is-copied");
        copyButton.title = "Copied";
        copyButton.setAttribute("aria-label", "Copied raw message");
        lifecycle.timeout(function () {
          copyButton.classList.remove("is-copied");
          copyButton.title = "Copy raw message";
          copyButton.setAttribute("aria-label", "Copy raw " + role.textContent + " message");
        }, 1200);
      }).catch(function () {
        copyButton.title = "Copy failed";
        copyButton.setAttribute("aria-label", "Copy failed");
      });
    });
    var text = document.createElement("div");
    text.className = "message-content";
    if (!message.content) {
      text.hidden = true;
    } else if (shellState.markdownRenderer && typeof shellState.markdownRenderer.render === "function") {
      text.innerHTML = shellState.markdownRenderer.render(message.content);
    } else {
      text.textContent = message.content;
    }
    identity.appendChild(role);
    var messageTimestamp = timestampElement(message.createdAt, "message-timestamp");
    if (messageTimestamp) {
      identity.appendChild(messageTimestamp);
    }
    header.appendChild(identity);
    header.appendChild(copyButton);
    bubble.appendChild(header);
    bubble.appendChild(text);
    if (isAccessReviewMessage(message)) {
      // The Inbox card replaces the announcement text; Copy raw message keeps it.
      text.hidden = true;
      bubble.classList.add("has-access-review");
      sharedState.settingsControls.mountAccessReview(bubble, message);
    }
    if (message.role === "assistant") {
      var workflowAuthGeneration = sharedState.authSessionGeneration;
      mountWorkflowCard(bubble, { lifecycle: lifecycle, requestClient: requestClient, loaded: loadedWorkflows(),
        isCurrent: function () { return sharedState.authSessionActive && sharedState.authSessionGeneration === workflowAuthGeneration; } });
    }
    renderAttachmentList(bubble, message.attachments);
    if (message.localDeliveryState === "not_delivered") {
      var deliveryStatus = uiElement("p", "message-delivery-status is-error");
      deliveryStatus.textContent = "not delivered";
      deliveryStatus.setAttribute("role", "status");
      bubble.appendChild(deliveryStatus);
    }
    // New messages arrive with a short effect (ui_motion.js). A sent message
    // is keyed by its request id, which survives the server's confirmation.
    if (message.role === "user" && message.clientRequestId) {
      window.UAMotion.arrive(bubble, "sent:" + message.clientRequestId, "sent", message.createdAt);
    } else if (message.role !== "user" && message.messageId) {
      window.UAMotion.arrive(bubble, "reply:" + message.messageId, "reply", message.createdAt);
    }
    return bubble;
  }

// Workflows read for workflow cards, kept for the current sign-in only, so a
// re-rendered card keeps its size while it reads the current status again.
function loadedWorkflows() {
    var generation = sharedState.authSessionGeneration;
    if (!shellState.loadedWorkflows || shellState.loadedWorkflows.generation !== generation) {
      shellState.loadedWorkflows = { generation: generation, records: new Map() };
    }
    return shellState.loadedWorkflows.records;
  }

function stopTransientCountdown() {
    lifecycle.clearInterval(shellState.transientCountdownTimer);
    shellState.transientCountdownTimer = 0;
  }

function replaceTransientActionWithExpiredState(countdown) {
    var notice = countdown.closest(".message-transient-notice");
    var actions = notice && notice.querySelector(".message-transient-actions");
    if (!actions) {
      return;
    }
    var openAction = actions.querySelector(".message-transient-open");
    if (openAction) {
      openAction.remove();
    }
    countdown.remove();
    if (actions.querySelector(".message-transient-expired")) {
      return;
    }
    var expiredStatus = document.createElement("span");
    expiredStatus.className = "message-transient-expired";
    expiredStatus.setAttribute("role", "status");
    expiredStatus.textContent = shellState.expiredSecureFormMessage;
    var dismissAction = actions.querySelector(".message-transient-dismiss");
    actions.insertBefore(expiredStatus, dismissAction);
  }

function updateTransientCountdown() {
    var countdowns = Array.from(
      sharedState.messageList.querySelectorAll(".message-transient-countdown")
    );
    if (!countdowns.length) {
      stopTransientCountdown();
      return false;
    }
    var shouldContinue = false;
    countdowns.forEach(function (countdown) {
      var expiresAtUtc = String(countdown.getAttribute("data-expires-at") || "");
      var rendered = shellState.credentialControls.formatSessionCountdown(expiresAtUtc);
      countdown.textContent = rendered;
      countdown.hidden = !rendered;
      if (rendered !== "expired") {
        shouldContinue = shouldContinue || Boolean(rendered);
        return;
      }
      replaceTransientActionWithExpiredState(countdown);
    });
    if (!shouldContinue) {
      stopTransientCountdown();
    }
    return shouldContinue;
  }

function createTransientItemCard(thread, item) {
    var transientNotice = document.createElement("div");
    transientNotice.className = "message-transient-notice";
    transientNotice.setAttribute("data-item-id", item.itemId);
    if (item.createdAt) {
      transientNotice.setAttribute("data-created-at", item.createdAt);
    }
    var transientText = document.createElement("pre");
    transientText.textContent = item.content;
    var transientActions = document.createElement("div");
    transientActions.className = "message-transient-actions";
    if (item.url) {
      var transientOpen = document.createElement("a");
      transientOpen.className = "button primary small message-transient-open";
      transientOpen.textContent = item.label;
      transientOpen.href = item.url;
      transientOpen.target = "_blank";
      transientOpen.rel = "noopener noreferrer";
      transientActions.appendChild(transientOpen);
      if (item.expiresAtUtc) {
        var transientCountdown = document.createElement("span");
        transientCountdown.className = "message-transient-countdown";
        transientCountdown.setAttribute("data-expires-at", item.expiresAtUtc);
        transientCountdown.setAttribute("role", "status");
        transientActions.appendChild(transientCountdown);
      }
    } else {
      var transientCopy = document.createElement("button");
      transientCopy.type = "button";
      transientCopy.className = "button secondary small";
      transientCopy.textContent = "Copy";
      lifecycle.listen(transientCopy, "click", function () {
        copyTextToClipboard(item.content).then(function () {
          transientCopy.textContent = "Copied";
        });
      });
      transientActions.appendChild(transientCopy);
    }
    var transientDismiss = document.createElement("button");
    transientDismiss.type = "button";
    transientDismiss.className = "button secondary small message-transient-dismiss";
    transientDismiss.textContent = "Dismiss";
    lifecycle.listen(transientDismiss, "click", function () {
      thread.dismissedTransientItems = thread.dismissedTransientItems || new Set();
      thread.dismissedTransientItems.add(item.itemId);
      thread.transientItems = thread.transientItems.filter(function (candidate) {
        return candidate.itemId !== item.itemId;
      });
      renderMessages();
    });
    transientNotice.appendChild(transientText);
    transientActions.appendChild(transientDismiss);
    transientNotice.appendChild(transientActions);
    return transientNotice;
  }

function renderMessages(options) {
    options = options || {};
    var viewport = shellState.transcriptView.capture();
    stopTransientCountdown();
    var thread = activeThread();
    sharedState.messageList.innerHTML = "";
    var errorThread = thread && thread.transientError ? thread : null;
    var errorNotice = errorThread ? errorThread.transientError : sharedState.historyHydrationError;
    if (errorNotice) {
      var errorBanner = document.createElement("div");
      errorBanner.className = "message-empty is-error";
      errorBanner.textContent = errorNotice.message + " ";
      var retryButton = document.createElement("button");
      retryButton.type = "button";
      retryButton.className = "button secondary small";
      retryButton.textContent = "Retry";
      lifecycle.listen(retryButton, "click", function () {
        clearSystemError(errorThread, errorNotice.source);
        if (errorThread && errorNotice.source === shellState.SYSTEM_ERROR_SOURCE.HYDRATE_THREAD) {
          if (errorThread.historyLoaded) {
            loadEarlierHistory();
          } else {
            ensureActiveThreadHistory();
          }
        } else {
          fetchSessions();
        }
      });
      errorBanner.appendChild(retryButton);
      sharedState.messageList.appendChild(errorBanner);
    }
    sharedState.messageList.appendChild(createWelcomeGuidance());
    if (thread && thread.hasMoreHistory && thread.historyBeforeCursor) {
      var earlierButton = document.createElement("button");
      earlierButton.type = "button";
      earlierButton.className = "button secondary small history-load-earlier";
      earlierButton.textContent = thread.historyRequest ? "Loading earlier messages…" : "Load earlier messages";
      earlierButton.disabled = Boolean(thread.historyRequest);
      lifecycle.listen(earlierButton, "click", function () {
        loadEarlierHistory();
      });
      sharedState.messageList.appendChild(earlierButton);
    }
    if (thread) {
      window.UAChatDelivery.buildTimeline(
        thread.messages,
        thread.transientItems
      ).forEach(function (item) {
        if (item.kind === "transient_item") {
          sharedState.messageList.appendChild(createTransientItemCard(thread, item.value));
          return;
        }
        sharedState.messageList.appendChild(createMessageBubble(item.value));
      });
    }
    var shouldRunTransientCountdown = updateTransientCountdown();
    if (shouldRunTransientCountdown && shellState.transientCountdownTimer === 0) {
      shellState.transientCountdownTimer = lifecycle.interval(updateTransientCountdown, 1000);
    }
    shellState.transcriptView.restore(viewport, {
      threadId: thread && thread.id,
      scrollToBottom: Boolean(options.scrollToBottom),
      prepend: Boolean(options.prepend),
    });
  }

function appendSystemError(thread, source, message) {
    var notice = window.UAChatDelivery.createErrorNotice(source, message);
    if (thread) {
      thread.transientError = notice;
    } else {
      sharedState.historyHydrationError = notice;
    }
  }

function clearSystemError(thread, recoveredSource) {
    if (thread) {
      var threadNotice = thread.transientError;
      thread.transientError = window.UAChatDelivery.clearErrorNotice(
        threadNotice,
        recoveredSource
      );
      return threadNotice !== thread.transientError;
    }
    var historyNotice = sharedState.historyHydrationError;
    sharedState.historyHydrationError = window.UAChatDelivery.clearErrorNotice(
      historyNotice,
      recoveredSource
    );
    return historyNotice !== sharedState.historyHydrationError;
  }

function runCommand(command) {
    var commandName = command.split(/\s+/)[0];
    if (commandName === "/new") {
      createServerThread().then(function () {
        sharedState.messageInput.focus();
      }).catch(function (error) {
        appendSystemError(
          activeThread(),
          shellState.SYSTEM_ERROR_SOURCE.CREATE_THREAD,
          error.message || "Unable to create a chat thread."
        );
        renderMessages();
      });
      return;
    }
    sendMessage(command, { routedCommand: true });
  }

function sendMessage(content, options) {
    options = options || {};
    var attachments = Array.isArray(options.attachments) ? options.attachments : [];
    if (content.charAt(0) === "/" && !attachments.length && !options.routedCommand) {
      runCommand(content);
      return;
    }
    var thread = activeThread();
    if (!thread) {
      createServerThread().then(function (created) {
        if (created) { sendMessage(content, options); }
      }).catch(function (error) {
        appendSystemError(
          null,
          shellState.SYSTEM_ERROR_SOURCE.CREATE_THREAD,
          error.message || "Unable to create a chat thread."
        );
        renderMessages();
      });
      return;
    }
    // The first ordinary message names the thread, as the server will;
    // slash commands never do (web_chat_threads.py).
    if (thread.title === NEW_CHAT_TITLE && content.trim().charAt(0) !== "/") {
      var titleSource = content.trim() || (attachments[0] && attachments[0].filename) || "Attachment";
      thread.title = titleSource;
      thread.titleFull = normalizeThreadTitle(titleSource) || "Attachment";
    }
    setThreadUnread(thread, false);
    clearSystemError(thread, shellState.SYSTEM_ERROR_SOURCE.SEND_MESSAGE);
    var clientRequestId = shellState.operationalIds.newId("web_request");
    var anchorSessionSequence = window.UAChatDelivery.latestDurableSessionSequence(
      thread.messages
    );
    thread.messages.push(optimisticMessage(
      clientRequestId,
      content,
      attachments,
      anchorSessionSequence
    ));
    shellState.deliveryTracker.watch(clientRequestId);
    var wasWaitingForReply = Boolean(thread.pendingReply || pendingReplyCount(thread) > 0);
    var startsTerminalReplyWait = !wasWaitingForReply;
    if (startsTerminalReplyWait) {
      startTerminalReplyWait(thread);
    } else {
      setThreadPending(thread, true);
    }
    thread.updatedAt = Date.now();
    sharedState.threads.sort(function (left, right) {
      return right.updatedAt - left.updatedAt;
    });
    saveThreads();
    renderThreads();
    // Sending shows the person's own message even when they had scrolled up,
    // and they follow the latest message from here; replies never do this.
    renderMessages({ scrollToBottom: true });
    var requestBody = buildChatRequestBody(
      content,
      thread,
      attachments,
      clientRequestId
    );
    var requestPromise = requestClient.fetch("/api/web-chat/chat", {
      method: "POST",
      headers: requestBody.headers,
      credentials: "same-origin",
      body: requestBody.body,
    });
    liveEvents.start();
    requestPromise.then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (payload) {
        if (!response.ok) {
          authentication.handleUnauthorizedResponse(response);
          throw new Error(payload.message || payload.error || response.statusText || "Message failed.");
        }
        return payload;
      });
    }).then(function (payload) {
      var responseSessionKey = String(payload.session_key || thread.sessionKey);
      clearSystemError(thread, shellState.SYSTEM_ERROR_SOURCE.SEND_MESSAGE);
      thread.conversationId = String(payload.conversation_id || thread.conversationId || "");
      thread.persisted = true;
      if (startsTerminalReplyWait) {
        finishTerminalReplyWait(thread);
      } else if (String(payload.message_type || "") !== "task_steering") {
        setThreadPending(thread, pendingReplyCount(thread) > 0);
      }
      var responseMessages = Array.isArray(payload.messages) ? payload.messages : [];
      mergeTranscriptMessages(thread, responseMessages);
      if (payload.transient && typeof payload.transient === "object") {
        var transientItem = window.UAChatDelivery.normalizeTransientItem(
          payload.transient,
          window.location.origin
        );
        thread.transientItems = window.UAChatDelivery.upsertTransientItem(
          thread.transientItems,
          transientItem
        );
        thread.transientItems = window.UAChatDelivery.reconcileTransientItems(
          thread.transientItems,
          thread.messages
        );
      }
      var lastMessage = responseMessages.length
        ? responseMessages[responseMessages.length - 1]
        : {};
      var hasNewResponse = false;
      responseMessages.forEach(function (message) {
        if (attention.receive({ ...message, session_key: thread.sessionKey }, thread)) hasNewResponse = true;
      });
      if (hasNewResponse) setThreadUnread(thread, !isReadingThread(thread), messageLevelFromPayload(lastMessage));
      thread.updatedAt = Date.now();
      saveThreads();
      if (thread.sessionAlias) {
        persistThreadAlias(thread).catch(function () {
          return null;
        });
      }
      liveEvents.start();
      renderThreads();
      renderMessages();
      if (responseSessionKey !== thread.sessionKey) {
        fetchSessions();
      }
    }).catch(function (error) {
      if (startsTerminalReplyWait) {
        finishTerminalReplyWait(thread);
      } else if (pendingReplyCount(thread) === 0) {
        setThreadPending(thread, false);
      }
      appendSystemError(
        thread,
        shellState.SYSTEM_ERROR_SOURCE.SEND_MESSAGE,
        error.message || "Message failed."
      );
      thread.updatedAt = Date.now();
      saveThreads();
      renderThreads();
      renderMessages();
    });
  }

lifecycle.listen(sharedState.authReveal, "click", function () {
    revealPassword(authenticationState.authInput, sharedState.authReveal);
  });

lifecycle.listen(sharedState.messageList, "scroll", function () {
    var top = sharedState.messageList.scrollTop;
    var movingUp = shellState.transcriptView.noteScroll();
    var thread = activeThread();
    if (movingUp && top <= 160 && thread && !thread.transientError) {
      loadEarlierHistory();
    }
  }, { passive: true });

lifecycle.listen(authenticationState.enrollmentReveal, "click", function () {
    revealPassword(authenticationState.enrollmentToken, authenticationState.enrollmentReveal);
  });

lifecycle.listen(authenticationState.enrollmentCopyButton, "click", function () {
    if (!authenticationState.enrollmentToken.value) {
      return;
    }
    copyTextToClipboard(authenticationState.enrollmentToken.value).then(function () {
      shellState.credentialControls.flashCopied(authenticationState.enrollmentCopyButton);
      authentication.setEnrollmentMessage(
        "Token copied. Save it in your password manager, then confirm that you saved it.",
        false
      );
    }).catch(function () {
      authentication.setEnrollmentMessage("Copy failed. Select the token and copy it manually.", true);
    });
  });

lifecycle.listen(authenticationState.enrollmentGenerateButton, "click", authentication.generatePermanentToken);

lifecycle.listen(authenticationState.enrollmentCompleteButton, "click", authentication.completePermanentTokenEnrollment);

lifecycle.listen(authenticationState.enrollmentCancelButton, "click", authentication.logoutChat);

lifecycle.listen(sharedState.authForm, "submit", function (event) {
    event.preventDefault();
    var token = authenticationState.authInput.value.trim();
    if (!token) {
      authenticationState.authError.textContent = "Enter your Web Chat token or invitation code.";
      authenticationState.authError.hidden = false;
      return;
    }
    requestClient.fetch("/api/web-chat/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ token: token }),
    }).then(function (response) {
      return parseApiResponse(response, "Authentication failed.");
    }).then(function (payload) {
      authentication.applyAuthenticatedSession(payload);
    }).catch(function (error) {
      authenticationState.authError.textContent = error.message || "Authentication failed.";
      authenticationState.authError.hidden = false;
    });
  });

lifecycle.listen(authenticationState.logoutButton, "click", authentication.logoutChat);

lifecycle.listen(authenticationState.objectivesLogout, "click", authentication.logoutChat);

lifecycle.listen(authenticationState.workspaceLogout, "click", authentication.logoutChat);

lifecycle.listen(authenticationState.statusLogout, "click", authentication.logoutChat);

lifecycle.listen(authenticationState.knowledgeLogout, "click", authentication.logoutChat);

lifecycle.listen(runtime_viewsState.objectivesRefresh, "click", runtime_views.loadObjectives);

lifecycle.listen(runtime_viewsState.runtimeObjectivesTab, "click", function () {
    runtime_views.selectRuntimeControlsView("objectives");
  });

lifecycle.listen(runtime_viewsState.runtimeSchedulesTab, "click", function () {
    runtime_views.selectRuntimeControlsView("schedules");
  });

lifecycle.listen(runtime_viewsState.runtimeMcpTab, "click", function () {
    runtime_views.selectRuntimeControlsView("mcp");
  });

lifecycle.listen(sharedState.newThreadButton, "click", function () {
    createServerThread().then(function () {
      sharedState.messageInput.focus();
    }).catch(function (error) {
      appendSystemError(
        activeThread(),
        shellState.SYSTEM_ERROR_SOURCE.CREATE_THREAD,
        error.message || "Unable to create a chat thread."
      );
      renderMessages();
    });
  });

lifecycle.listen(sharedState.settingsButton, "click", function () {
    var thread = activeThread();
    if (!thread) {
      createServerThread().then(function (created) {
        if (created) { sharedState.settingsButton.click(); }
      }).catch(function (error) {
        appendSystemError(
          null,
          shellState.SYSTEM_ERROR_SOURCE.CREATE_THREAD,
          error.message || "Unable to create a chat thread."
        );
        renderMessages();
      });
      return;
    }
    sharedState.settingsControls.open(null, thread.sessionKey)
      .then(function () {
        if (clearSystemError(thread, shellState.SYSTEM_ERROR_SOURCE.OPEN_SETTINGS)) {
          renderMessages();
        }
      })
      .catch(function (error) {
        appendSystemError(
          thread,
          shellState.SYSTEM_ERROR_SOURCE.OPEN_SETTINGS,
          error.message || "Unable to open Settings."
        );
        saveThreads();
        renderMessages();
      });
  });

lifecycle.listen(sharedState.settingsAppearanceReset, "click", function () {
    sharedState.settingsAppearanceReset.disabled = true;
    sharedState.settingsAppearanceReset.setAttribute("aria-busy", "true");
    sharedState.settingsAppearanceReset.textContent = "Resetting...";
    appearanceState.settingsAppearanceMutationChain = appearanceState.settingsAppearanceMutationChain
      .catch(function () { return null; })
      .then(function () {
        return requestClient.fetch("/api/web-chat/appearance", {
          method: "DELETE",
          credentials: "same-origin",
          headers: {
            "X-UA-Permission-CSRF": sharedState.runtimeControlsCsrfToken,
          },
        });
      })
      .then(function (response) {
        if (!response.ok) {
          authentication.handleUnauthorizedResponse(response);
          throw new Error("The theme could not be reset. Try again.");
        }
        window.UAAppearance.clearUserPreference();
        appearance.setSettingsAppearanceStatus("");
        appearance.renderSettingsAppearance();
      })
      .catch(function (error) {
        appearance.setSettingsAppearanceStatus(error.message || "The theme could not be reset. Try again.");
      })
      .finally(function () {
        sharedState.settingsAppearanceReset.disabled = false;
        sharedState.settingsAppearanceReset.removeAttribute("aria-busy");
        sharedState.settingsAppearanceReset.textContent = "Reset to Defaults";
      });
  });

if (diagnosticsState.debugContextToggle) {
    lifecycle.listen(diagnosticsState.debugContextToggle, "change", diagnostics.updateContextDumpDebugSettings);
  }

if (diagnosticsState.debugContextMode) {
    lifecycle.listen(diagnosticsState.debugContextMode, "change", diagnostics.updateContextDumpDebugSettings);
  }

authentication.resumeWebChatSession();

lifecycle.listen(sharedState.messageForm, "submit", function (event) {
    event.preventDefault();
    var content = sharedState.messageInput.value.trim();
    if (!content && !shellState.pendingAttachments.length) {
      return;
    }
    var attachments = detachPendingAttachmentsForSend();
    sharedState.messageInput.value = "";
    sendMessage(content, { attachments: attachments });
  });

// Match the platform's primary shortcut modifier and keep its visual cue local
// to this page's lifecycle. Blur clears it when keyup happens in another app.
const sendModifier = /Mac|iPhone|iPad|iPod/.test(navigator.platform) ? "metaKey" : "ctrlKey";
sharedState.messageForm.querySelector('[type="submit"]').setAttribute(
    "aria-keyshortcuts", sendModifier === "metaKey" ? "Meta+Enter" : "Control+Enter"
  );

function updateSendModifier(event) {
    sharedState.messageForm.classList.toggle("is-send-modifier-held", event[sendModifier]);
  }

lifecycle.listen(window, "keydown", updateSendModifier, true);
lifecycle.listen(window, "keyup", updateSendModifier, true);
lifecycle.listen(window, "blur", function () {
    sharedState.messageForm.classList.remove("is-send-modifier-held");
  });
lifecycle.listen(window, "focus", function () { renderThreads(); });
lifecycle.listen(document, "visibilitychange", function () {
    renderThreads();
    if (document.hidden) {
      sharedState.messageForm.classList.remove("is-send-modifier-held");
    }
  });

lifecycle.listen(sharedState.messageInput, "keydown", function (event) {
    if (event.key === "Enter" && event[sendModifier] && !event.isComposing && !event.repeat) {
      event.preventDefault();
      sharedState.messageForm.requestSubmit();
    }
  });

if (sharedState.attachButton && sharedState.attachmentInput) {
    lifecycle.listen(sharedState.attachButton, "click", function () {
      sharedState.attachmentInput.click();
    });
    lifecycle.listen(sharedState.attachmentInput, "change", function () {
      addPendingFiles(sharedState.attachmentInput.files);
      sharedState.attachmentInput.value = "";
    });
  }

lifecycle.listen(sharedState.messageInput, "paste", function (event) {
    var items = event.clipboardData && event.clipboardData.items;
    if (!items) {
      return;
    }
    var files = [];
    Array.prototype.slice.call(items).forEach(function (item) {
      if (item.kind === "file" && String(item.type || "").indexOf("image/") === 0) {
        var file = item.getAsFile();
        if (file) {
          files.push(file);
        }
      }
    });
    if (files.length) {
      addPendingFiles(files);
    }
  });

lifecycle.listen(sharedState.messageForm, "dragover", function (event) {
    if (event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files.length) {
      event.preventDefault();
      sharedState.messageForm.classList.add("is-drag-over");
    }
  });

lifecycle.listen(sharedState.messageForm, "dragleave", function () {
    sharedState.messageForm.classList.remove("is-drag-over");
  });

lifecycle.listen(sharedState.messageForm, "drop", function (event) {
    if (!event.dataTransfer || !event.dataTransfer.files || !event.dataTransfer.files.length) {
      return;
    }
    event.preventDefault();
    sharedState.messageForm.classList.remove("is-drag-over");
    addPendingFiles(event.dataTransfer.files);
  });

lifecycle.listen(document, "ua:appearance-change", function (event) {
    var next = event.detail && event.detail.appearance;
    if (!next) {
      return;
    }
    sharedState.appearance.lightColors.blue = next.light.colors[0];
    sharedState.appearance.lightColors.yellow = next.light.colors[1];
    sharedState.appearance.lightColors.green = next.light.colors[2];
    sharedState.appearance.lightColors.red = next.light.colors[3];
    sharedState.appearance.lightDiffusion = next.light.diffusion;
    sharedState.appearance.polygonOpacityLow = next.light.opacityLow;
    sharedState.appearance.polygonOpacityHigh = next.light.opacityHigh;
    sharedState.appearance.polygonLineColor = next.light.lineColor;
    sharedState.appearance.polygonLineOpacity = next.light.lineOpacity;
    sharedState.appearance.polygonSeed = next.light.seed;
    sharedState.appearance.backgroundDynamic = next.light.dynamic;
    appearanceState.lightPaneCacheKey = "";
    if (next.mode === "light") {
      appearanceState.lastWidth = 0;
      appearanceState.lastHeight = 0;
      lifecycle.frame(appearance.draw);
    }
  });

lifecycle.listen(window, "resize", function () {
    if (!window.UAAppearance || window.UAAppearance.current().mode === "light") {
      appearance.resizeCanvas();
    }
  });

if (!window.UAAppearance || window.UAAppearance.current().mode === "light") {
    appearance.resizeCanvas();
    lifecycle.frame(appearance.draw);
  }
}
mountPage();
window.addEventListener("pageshow", (event) => {
  if (event.persisted) mountPage();
});

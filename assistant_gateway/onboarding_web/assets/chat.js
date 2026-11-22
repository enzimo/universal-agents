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
import { FOLLOW_UP_AFTER_REVIEW } from "./access_inbox.mjs";
import { createSettingsControls } from "./settings_controls.mjs";
import { createWorkspaceFileActions } from "./workspace_file_actions.mjs";
import { createTranscriptView } from "./chat_transcript_view.mjs";
import { createStatusPage } from "./status_page.mjs";
import { createKnowledgeClient, createKnowledgePage, readPendingProposals } from "./knowledge_page.mjs";
import { createActivityMesh } from "./activity_mesh.mjs";
import { createTeamActivity } from "./team_activity.mjs";
import { createSettingsClient } from "./settings_support.mjs";
import { createChatSubmission } from "./chat_submission.mjs";
import { brokerCardElement, createBrokerCards, replaceChildrenKeeping } from "./broker_card.mjs";
import {
  LIST_STATE,
  THREAD_PLACEMENT_ACTION,
  createSectionCollapse,
  defaultThread,
  formatThreadAge,
  formatThreadAgeWords,
  groupThreadSections,
  messageSnippet,
  plainSnippet,
  renderThreadSections,
  threadDetail,
} from "./chat_thread_list.mjs";
import { THREAD_MENU_ACTION, attachThreadMenuTriggers, createThreadMenu } from "./chat_thread_menu.mjs";
import { createThreadPreview } from "./chat_thread_preview.mjs";
import { renderTrail, trailKey, trailView, workingLabel, workingLine } from "./agent_trail.mjs";
import { THREAD_CHANGE_DONE, TRY_AGAIN, createThreadListStatus } from "./chat_thread_status.mjs";

function mountPage() {
"use strict";
const lifecycle = createPageLifecycle(window);
const requestClient = createRequestClient(window, lifecycle);
// Broker pages shown inside their chat cards as sandboxed frames (broker_card.mjs).
// A broker card that held focus and closed hands focus to the message box, which the
// chat's re-renders never rebuild (broker_card.mjs).
function focusMessageBox() {
  var input = document.getElementById("message-input");
  if (input) {
    input.focus({ preventScroll: true });
  }
}
const brokerCards = createBrokerCards({ browser: window, document, lifecycle, onFocusLost: focusMessageBox });
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
// The hidden sentence (chat.html) each thread row points to: the row has a menu.
const THREAD_ROW_MENU_HINT_ID = "thread-row-menu-hint";
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
const authentication = createAuthentication({ authenticationState, sharedState, lifecycle, requestClient, operations: { activeThread: (...args) => activeThread(...args), clearOptimisticDeliveries: (...args) => clearOptimisticDeliveries(...args), clearPendingAttachments: (...args) => clearPendingAttachments(...args), clearThreadListOverlays: () => clearThreadListOverlays(), configurePrincipalState: (...args) => configurePrincipalState(...args), fetchSessions: (...args) => fetchSessions(...args), loadObjectives: (...args) => runtime_views.loadObjectives(...args), parseApiResponse: (...args) => parseApiResponse(...args), refreshContextDumpStatus: (...args) => diagnostics.refreshContextDumpStatus(...args), renderMessages: (...args) => renderMessages(...args), renderSettingsAppearance: (...args) => appearance.renderSettingsAppearance(...args), renderThreads: (...args) => renderThreads(...args), setAssistantName: (name) => setAssistantName(sharedState, document, name), showSignIn: () => signInGreeting.show(), stopSignIn: () => signInGreeting.stop(), startLiveEvents: () => { liveEvents.start(); activityMesh.start(); sharedState.settingsControls.startInboxCount(); }, stopLiveEvents: () => { liveEvents.stop(); attention.clear(); activityMesh.stop(); teamActivity.clear(); brokerCards.closeAll(); }, setObjectivesStatus: (...args) => runtime_views.setObjectivesStatus(...args) } });
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
// Each row's time since its last message moves on once a minute.
lifecycle.interval(refreshThreadAges, 60 * 1000);
// One menu for every row, keyed by the thread's session key (chat_thread_menu.mjs).
shellState.threadMenu = createThreadMenu({
  document, window, lifecycle,
  threadList: sharedState.threadList,
  onAction: (...args) => threadMenuAction(...args),
  focusRow: sessionKey => {
    var button = threadRowButton(sessionKey);
    if (button) button.focus();
  },
});
// What happened to a change made from a thread's menu (chat_thread_status.mjs).
shellState.threadListStatus = createThreadListStatus({
  document, lifecycle,
  element: document.getElementById("thread-list-status"),
  // Close and Retry vanish with the message; focus goes to the open chat's row.
  restoreFocus: () => {
    var thread = activeThread();
    var button = thread ? threadRowButton(thread.sessionKey) : null;
    (button && !button.closest("[hidden]") ? button : sharedState.newThreadButton).focus();
  },
});
// A mouse resting on a row previews the thread's last turns (chat_thread_preview.mjs).
shellState.threadPreview = createThreadPreview({
  document, window, lifecycle, requestClient,
  list: sharedState.threadList,
  threadFor: sessionKey => threadForSession(sessionKey),
  assistantName: () => sharedState.assistantName,
  // Not for the chat already open beside it, nor under an open menu (C30).
  canPreview: sessionKey => {
    var thread = threadForSession(sessionKey);
    return Boolean(thread) && thread.id !== sharedState.activeThreadId && !shellState.threadMenu.isOpen();
  },
  // The footer's trail pulses like the row's (threadTrailView).
  trailFor: thread => threadTrailView(thread),
});
// Right-click, long press, the context-menu key or Shift+F10 on a row open its
// menu; tracked once for the whole list so a redraw cannot orphan a press.
attachThreadMenuTriggers(sharedState.threadList, (sessionKey, point) => {
  var thread = threadForSession(sessionKey);
  if (thread) openThreadMenu(thread, point);
}, { lifecycle, window });

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

shellState.separateRequest = document.getElementById("separate-request-toggle");

shellState.separateRequestMessage = document.getElementById("separate-request-message");

// The switch's position while no chat exists yet; the first message's new
// chat takes it once created (chat_submission.mjs). A chat keeps its own.
shellState.newThreadParallel = false;

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
    CREATE_THREAD: "create_thread",
    FETCH_SESSIONS: "fetch_sessions",
    HYDRATE_THREAD: "hydrate_thread",
    OPEN_SETTINGS: "open_settings",
    RENAME_THREAD: "rename_thread",
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
    onFollowUpPages: function (requestId, links, placement) { return addFollowUpPages(requestId, links, placement); },
    onShowFollowUpPages: function (requestId) { showFollowUpPages(requestId); },
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
      thread.pendingDeliveries.delete(message.clientRequestId);
      releaseMessageAttachmentPreviews(message);
    });
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
    if (thread) thread.pendingDeliveries.clear();
    (thread && Array.isArray(thread.messages) ? thread.messages : []).forEach(function (message) {
      if (!message.optimistic) {
        return;
      }
      shellState.deliveryTracker.acknowledge(message.clientRequestId);
      releaseMessageAttachmentPreviews(message);
    });
  }

function clearOptimisticDeliveries() {
    if (shellState.chatSubmission) shellState.chatSubmission.reset();
    shellState.newThreadParallel = false;
    renderParallelSwitch();
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
    // Which sections the person collapsed, in this browser (chat_thread_list.mjs).
    sharedState.sectionCollapse = createSectionCollapse(window.localStorage, sharedState.storageKey + ".sections");
    var firstThread = defaultThread(sharedState.threads);
    sharedState.activeThreadId = String(
      window.localStorage.getItem(sharedState.storageKey + ".selected")
      || (firstThread && firstThread.id)
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
        // Restore metadata through the same constructor as server-created
        // chats, so all page-only delivery state starts fresh after reload.
        return threadFromSeed({
          id: id,
          sessionKey: String(thread.sessionKey || thread.session_key || ""),
          conversationId: String(thread.conversationId || thread.conversation_id || ""),
          title: title,
          titleFull: String(thread.titleFull || thread.title_full || title),
          sessionAlias: String(thread.sessionAlias || thread.session_alias || ""),
          persisted: Boolean(thread.persisted),
          hasUnreadReply: Boolean(thread.hasUnreadReply || thread.has_unread_reply),
          unreadReplyLevel: String(thread.unreadReplyLevel || thread.unread_reply_level || "normal"),
          messages: [],
          updatedAt: Number(thread.updatedAt || Date.now()),
          // Where the thread sat, so the cached list draws it in its section
          // before the server answers (plan review C09). No message text is kept.
          pinned: Boolean(thread.pinned),
          listState: String(thread.listState || ""),
          needsYou: String(thread.needsYou || ""),
          snoozedUntil: String(thread.snoozedUntil || ""),
          lastActivityAt: Number(thread.lastActivityAt || 0),
        });
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
        pinned: thread.pinned,
        listState: thread.listState,
        needsYou: thread.needsYou,
        snoozedUntil: thread.snoozedUntil,
        lastActivityAt: thread.lastActivityAt,
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
      // File objects and retry callbacks belong to this page's sign-in only.
      // Keep them outside the disposable error notice shared by chat operations.
      pendingDeliveries: new Map(),
      hasUnreadReply: Boolean(seed.hasUnreadReply || seed.has_unread_reply),
      unreadReplyLevel: String(seed.unreadReplyLevel || seed.unread_reply_level || "normal"),
      messages: messages,
      historyBeforeCursor: String(seed.historyBeforeCursor || ""),
      hasMoreHistory: Boolean(seed.hasMoreHistory),
      historyLoaded: Boolean(seed.historyLoaded),
      transientError: null,
      transientItems: [],
      // The thread's Run concurrently setting as the server saved it, and
      // whether a request is open in its conversation (every event page).
      parallelThread: Boolean(seed.parallelThread || seed.parallel_thread),
      parallelSaving: false,
      parallelError: "",
      serverRequestOpen: false,
      updatedAt: Number(seed.updatedAt || Date.now()),
      // The thread list's row (chat_thread_list.mjs): where the thread sits,
      // what it waits on, its latest message and when anything was posted.
      pinned: Boolean(seed.pinned),
      listState: String(seed.listState || ""),
      needsYou: String(seed.needsYou || ""),
      snoozedUntil: String(seed.snoozedUntil || ""),
      lastMessage: seed.lastMessage || null,
      lastActivityAt: Number(seed.lastActivityAt || 0),
      lastSequence: Number(seed.lastSequence || 0),
      // The agent trail (agent_trail.mjs): the server's trail and who works on
      // the thread's request now (every list and live read), and the trail as
      // it was when this page sent its message, so the row can show the new
      // turn until the server's trail changes (plan review C10).
      trail: seed.trail || null,
      working: [],
      trailAtSend: null,
      // A clear_woke for its Woke mark was sent and the list has not caught up.
      restoreRequested: false,
      // Whether this page has the server's row for the thread in this sign-in.
      // A thread drawn from the browser's saved list has none yet: its place
      // may be out of date and its last message number is unknown, so
      // Snooze, Dismiss and clearing a Woke mark wait for the server's list
      // (review F1, F2).
      hasServerRow: false,
    };
  }

function archiveThread(threadId) {
    var archivedThread = sharedState.threads.find(function (thread) {
      return thread.id === threadId;
    });
    if (!archivedThread || !archivedThread.sessionKey || !sharedState.authSessionActive) {
      return;
    }
    // An answer after sign-out, or for someone who signed in since, is dropped (review F3).
    var authGeneration = sharedState.authSessionGeneration;
    var current = function () {
      if (!sharedState.authSessionActive || sharedState.authSessionGeneration !== authGeneration) {
        throw new Error("Chat sign-in changed while deleting the chat.");
      }
    };
    var named = quotedThreadTitle(archivedThread);
    threadChangeRequest(
      "/api/web-chat/sessions/" + encodeURIComponent(archivedThread.sessionKey) + "/archive",
      { method: "POST", credentials: "same-origin" }
    ).then(function () {
      current();
      removeArchivedThread(threadId);
      shellState.threadListStatus.announce(THREAD_CHANGE_DONE[THREAD_MENU_ACTION.DELETE] + " " + named + ".");
    }).catch(function (error) {
      if (!sharedState.authSessionActive || sharedState.authSessionGeneration !== authGeneration) {
        return;
      }
      // Shown beside the list, in view, with Retry only when it can help (review F5).
      shellState.threadListStatus.fail(threadChangeFailure(named, "deleted", error), {
        retry: error.refused ? null : function () { archiveThread(threadId); },
      });
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
      sharedState.activeThreadId = defaultThread(sharedState.threads).id;
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
    renderParallelSwitch();
  }

// pendingReply is this page's own wait for a reply it sent; serverBusy is the
// server's view, so pages on other devices see the same work. A busy row
// always pulses at least one triangle of its agent trail.
function threadIsBusy(thread) {
    return Boolean(thread && (thread.pendingReply || thread.serverBusy));
  }

// The row's agent trail: its triangles, which pulse, and who works in words.
function threadTrailView(thread) {
    var sentBeforeTrail = Boolean(thread.pendingReply && thread.trailAtSend !== null
      && thread.trailAtSend === trailKey(thread));
    return trailView(thread, { busy: threadIsBusy(thread), sentBeforeTrail: sentBeforeTrail });
  }

// Who works on each listed thread now, as one comparable string per thread.
function workingKey(working) {
    return JSON.stringify((Array.isArray(working) ? working : []).map(function (agent) {
      return [agent.agent_type, Boolean(agent.lead), agent.name];
    }));
  }

// Live thread-list state rides on every event page because renames, archives,
// threads started elsewhere and running tasks write no transcript row.
function applyThreadStates(payload) {
    if (!sharedState.authSessionActive || typeof payload.threads_revision !== "string") {
      return;
    }
    var busySessions = new Set(Array.isArray(payload.busy_session_keys) ? payload.busy_session_keys : []);
    var openSessions = new Set(Array.isArray(payload.open_request_session_keys) ? payload.open_request_session_keys : []);
    var workingSessions = new Map((Array.isArray(payload.working) ? payload.working : []).map(function (item) {
      return [item.session_key, Array.isArray(item.agents) ? item.agents : []];
    }));
    var busyChanged = false;
    var openChanged = false;
    sharedState.threads.forEach(function (thread) {
      // Who works now changes the row's pulsing triangles without a reload.
      var working = workingSessions.get(thread.sessionKey) || [];
      if (workingKey(thread.working) !== workingKey(working)) {
        thread.working = working;
        busyChanged = true;
      }
      var isBusy = busySessions.has(thread.sessionKey);
      if (Boolean(thread.serverBusy) !== isBusy) {
        thread.serverBusy = isBusy;
        busyChanged = true;
      }
      var isOpen = openSessions.has(thread.sessionKey);
      if (thread.serverRequestOpen !== isOpen) {
        thread.serverRequestOpen = isOpen;
        openChanged = true;
      }
    });
    if (busyChanged || openChanged) renderParallelSwitch();
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

// Run concurrently is a setting of each thread (owner, 2026-10-29). It is
// locked while a request is open in the thread: a message sent then is
// guidance for that request and reaches it where it runs, so a change could
// not apply to anything the person sees. The server refuses such a change too.
function threadRequestOpen(thread) {
    return Boolean(thread && (threadIsBusy(thread) || pendingReplyCount(thread) > 0 || thread.serverRequestOpen));
  }

function renderParallelSwitch() {
    var toggle = shellState.separateRequest;
    var message = shellState.separateRequestMessage;
    if (!toggle || !message) {
      return;
    }
    var thread = activeThread();
    var locked = threadRequestOpen(thread);
    var preparingChat = Boolean(shellState.chatSubmission && shellState.chatSubmission.isPreparing());
    toggle.checked = thread ? Boolean(thread.parallelThread) : Boolean(shellState.newThreadParallel);
    toggle.disabled = preparingChat || locked || Boolean(thread && thread.parallelSaving);
    var error = thread ? thread.parallelError : "";
    message.textContent = error || (locked ? "Available after this chat replies." : "");
    message.classList.toggle("is-error", Boolean(error));
    if (error) {
      message.setAttribute("role", "alert");
    } else {
      message.removeAttribute("role");
    }
    message.hidden = !message.textContent;
  }

// Save the switch for one thread. The page shows the new position at once
// and puts the saved one back, with the reason, when the server refuses.
function saveParallelSetting(thread, value) {
    var generation = sharedState.authSessionGeneration;
    var current = function () {
      return sharedState.authSessionActive && sharedState.authSessionGeneration === generation;
    };
    var previous = Boolean(thread.parallelThread);
    thread.parallelThread = Boolean(value);
    thread.parallelSaving = true;
    thread.parallelError = "";
    renderParallelSwitch();
    return requestClient.fetch("/api/web-chat/sessions/" + encodeURIComponent(thread.sessionKey), {
      method: "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parallel_thread: Boolean(value), conversation_id: thread.conversationId }),
      signal: AbortSignal.timeout(15000),
    }).then(function (response) {
      return response.text().then(function (body) {
        if (response.ok || !current()) {
          return;
        }
        authentication.handleUnauthorizedResponse(response);
        var payload = {};
        try {
          payload = body ? JSON.parse(body) : {};
        } catch (error) {
          payload = {};
        }
        throw new Error(payload.message || payload.error || body || response.statusText || "Try again.");
      });
    }).catch(function (error) {
      if (current()) {
        thread.parallelThread = previous;
        thread.parallelError = "Run concurrently was not changed. " + (error.message || "Try again.");
      }
      throw error;
    }).finally(function () {
      thread.parallelSaving = false;
      renderParallelSwitch();
    });
  }

// Whether a message sent now starts a new request, which adds a turn to the
// agent trail: not a slash command, and not guidance for a request already
// open, from this page or another (plan review C18).
function sendStartsRequest(thread, content) {
    return String(content || "").trim().charAt(0) !== "/" && !threadRequestOpen(thread);
  }

// `startsRequest`: the message starts a new request, so the row shows its
// turn until the server's trail changes (agent_trail.mjs sentBeforeTrail).
function startTerminalReplyWait(thread, startsRequest) {
    if (!thread) {
      return;
    }
    if (pendingReplyCount(thread) === 0) {
      thread.trailAtSend = startsRequest ? trailKey(thread) : null;
    }
    thread.pendingReplyCount = pendingReplyCount(thread) + 1;
    setThreadPending(thread, true);
  }

function finishTerminalReplyWait(thread) {
    if (!thread) {
      return;
    }
    thread.pendingReplyCount = Math.max(0, pendingReplyCount(thread) - 1);
    if (thread.pendingReplyCount === 0) {
      thread.trailAtSend = null;
    }
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
    applyEventToThreadRow(thread, event);
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
    if (shellState.chatSubmission.isPreparing()) return;
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
    if (shellState.chatSubmission.isPreparing()) return;
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
      if (options.fresh) {
        // The read in flight may have left before the change this caller
        // just saved, so read again after it; a read started meanwhile, after
        // that change, is reused (review F16).
        return shellState.sessionsRequest.then(function () {
          if (sharedState.authSessionActive && authGeneration === sharedState.authSessionGeneration) {
            return fetchSessions();
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
          // A save in flight owns the switch until the server answers it.
          if (!existing.parallelSaving) existing.parallelThread = Boolean(session.parallel_thread);
          applyServerThreadRow(existing, session);
          return existing;
        }
        var created = threadFromSeed({
          id: sessionKey.replace(/[^A-Za-z0-9_-]+/g, "_"),
          sessionKey: sessionKey,
          conversationId: String(session.conversation_id || ""),
          title: title,
          titleFull: title,
          sessionAlias: alias,
          persisted: true,
          parallelThread: Boolean(session.parallel_thread),
          updatedAt: Date.parse(session.updated_at || "") || Date.now(),
        });
        applyServerThreadRow(created, session);
        return created;
      }).filter(Boolean);
      sharedState.threads.sort(function (left, right) {
        return right.updatedAt - left.updatedAt;
      });
      if (!sharedState.activeThreadId || !sharedState.threads.find(function (thread) { return thread.id === sharedState.activeThreadId; })) {
        var firstThread = defaultThread(sharedState.threads);
        sharedState.activeThreadId = firstThread ? firstThread.id : "";
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
    var previousSelection = sharedState.activeThreadId;
    shellState.threadCreationGeneration = authGeneration;
    var init = {
      method: "POST",
      credentials: "same-origin",
      signal: AbortSignal.timeout(15000),
    };
    var request = requestClient.fetch("/api/web-chat/sessions", init).then(function (response) {
      if (!sharedState.authSessionActive || sharedState.authSessionGeneration !== authGeneration) { return null; }
      return parseApiResponse(response, "Unable to create a chat thread.", function () {
        if (!sharedState.authSessionActive || sharedState.authSessionGeneration !== authGeneration) {
          throw new Error("Chat sign-in changed while creating the thread.");
        }
      });
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
        parallelThread: Boolean(session.parallel_thread),
      });
      // Creating a destination does not replace a conversation the person
      // selected while the request was in flight. Its sender keeps `thread`.
      if (sharedState.activeThreadId === previousSelection) sharedState.activeThreadId = thread.id;
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
    // The list is drawn again on many events; keyboard focus stays on the
    // same thread's row or section header (plan review C03, C25).
    var focusTarget = focusedThreadListTarget();
    var unreadStateChanged = false;
    sharedState.threads.forEach(function (thread) {
      if (isReadingThread(thread) && thread.hasUnreadReply) {
        setThreadUnread(thread, false);
        unreadStateChanged = true;
      }
      restoreWokeThreadWhenRead(thread);
    });
    if (!sharedState.threads.length) {
      sharedState.threadList.innerHTML = "";
      // Once the server says there is no thread yet, one unsaved row stands
      // for the chat the first message will start (owner's request,
      // 2026-10-09). Opening Web Chat still creates nothing.
      if (sharedState.threadsRevision) {
        sharedState.threadList.appendChild(unsavedThreadRow());
      }
      restoreThreadListFocus(focusTarget);
    } else {
      renderThreadSections({
        document: document,
        container: sharedState.threadList,
        groups: groupThreadSections(sharedState.threads),
        collapse: threadSectionCollapse(),
        renderRow: threadRow,
      });
      restoreThreadListFocus(focusTarget);
    }
    // The preview stays beside the same thread's row, or closes when it is gone.
    shellState.threadPreview.refresh();
    if (unreadStateChanged) {
      saveThreads();
    }
    attention.refresh();
  }

// One two-line row: the title, a Woke mark and the time since anything was
// last posted, then the agent trail (agent_trail.mjs) and what the thread
// waits on or its latest message. Rename and Delete live in the row's menu;
// while renaming, the row shows the name field with Save and Cancel. Its name
// for screen readers says the same in words: who works now ("Working: team
// lead") or the reply marks the dot shows, and that the row has a menu
// (review F9, F12, F18).
function threadRow(thread) {
    var renaming = thread.id === sharedState.editingThreadId;
    var row = document.createElement("div");
    row.className = "thread-row"
      + (thread.id === sharedState.activeThreadId ? " is-active" : "")
      + (renaming ? " is-renaming" : "")
      + (thread.hasUnreadReply ? " has-unread" : "")
      + (thread.hasUnreadReply && thread.unreadReplyLevel === "attention" ? " has-attention" : "")
      + (thread.listState === LIST_STATE.WOKE ? " is-woke" : "");
    row.dataset.sessionKey = thread.sessionKey;
    row.appendChild(threadActivityIndicator(thread));
    if (renaming) {
      appendRenameControls(row, thread);
      return row;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.className = "thread-button";
    if (thread.id === sharedState.activeThreadId) button.setAttribute("aria-current", "true");
    // The menu is the only way to rename or delete; Enter opens the chat, so
    // no aria-haspopup (review F9).
    button.setAttribute("aria-keyshortcuts", "Shift+F10 ContextMenu");
    button.setAttribute("aria-describedby", THREAD_ROW_MENU_HINT_ID);
    var titleLine = uiElement("span", "thread-line thread-title-line");
    var title = uiElement("span", "thread-title", fullThreadTitle(thread));
    titleLine.appendChild(title);
    var mark = threadReplyMark(thread);
    if (mark) {
      titleLine.appendChild(uiElement("span", "sr-only", mark));
    }
    if (thread.listState === LIST_STATE.WOKE) {
      titleLine.appendChild(uiElement("span", "thread-woke-badge", "Woke"));
    }
    var at = lastActivityTimestamp(thread);
    if (at) {
      var age = uiElement("time", "thread-age", formatThreadAge(at, Date.now()));
      age.dataset.at = String(at);
      age.dateTime = new Date(at).toISOString();
      age.setAttribute("aria-hidden", "true");
      titleLine.appendChild(age);
      var ageWords = uiElement("span", "sr-only thread-age-words", threadAgeWords(at, Date.now()));
      ageWords.dataset.at = String(at);
      titleLine.appendChild(ageWords);
    }
    button.appendChild(titleLine);
    // Line 2: the agent trail, then what the thread waits on, else who works
    // now (lead ruling on review C22), else its latest message.
    var trail = threadTrailView(thread);
    var detail = threadDetail(thread, sharedState.assistantName);
    if ((!detail || detail.kind !== "waiting") && trail.workingNames.length) {
      detail = { kind: "working", text: workingLine(trail), description: workingLabel(trail) };
    }
    if (detail || trail.triangles.length) {
      var detailLine = uiElement("span", "thread-line thread-detail" + (detail ? " is-" + detail.kind : ""));
      if (trail.triangles.length) detailLine.appendChild(renderTrail(document, trail));
      if (detail) {
        var detailText = uiElement("span", "thread-detail-text", detail.text);
        // The row's hidden mark already says who works (threadReplyMark), so the
        // visible words are not read twice.
        if (detail.kind === "working") detailText.setAttribute("aria-hidden", "true");
        detailLine.appendChild(detailText);
        if (detail.description) detailLine.title = detail.description;
      }
      button.appendChild(detailLine);
    }
    // The full title shows on keyboard focus; mouse users get the hover
    // card, which shows it in full (plan review C03). Focus the list moves
    // after a mouse Dismiss or Delete is not keyboard focus, so no tooltip
    // pops up over the next row then (review C16).
    lifecycle.listen(button, "focus", function () {
      if (typeof button.matches === "function" && !button.matches(":focus-visible")) {
        return;
      }
      scheduleThreadTitleTooltip(title, fullThreadTitle(thread));
    });
    lifecycle.listen(button, "blur", hideThreadTitleTooltip);
    // The open chat's row gets no hover card, and Web Chat has no chat
    // header, so a mouse resting on that row shows its full title (review F6).
    // Not right after a click opened it, while the person reads: like the
    // hover card, a row just pressed waits until the pointer leaves it (C18).
    lifecycle.listen(button, "pointerdown", function (event) {
      if (event.pointerType === "mouse") shellState.threadTitlePressedKey = thread.sessionKey;
    });
    lifecycle.listen(button, "mouseenter", function () {
      if (thread.id === sharedState.activeThreadId && shellState.threadTitlePressedKey !== thread.sessionKey) {
        scheduleThreadTitleTooltip(title, fullThreadTitle(thread));
      }
    });
    lifecycle.listen(button, "mouseleave", function () {
      if (shellState.threadTitlePressedKey === thread.sessionKey) {
        shellState.threadTitlePressedKey = "";
      }
      if (shellState.threadTitleTooltipOwner === title) {
        hideThreadTitleTooltip();
      }
    });
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
    return row;
  }

function openThreadMenu(thread, point) {
    if (!thread.persisted || !thread.sessionKey) {
      return;
    }
    hideThreadTitleTooltip();
    shellState.threadPreview.hide();
    shellState.threadMenu.open({
      sessionKey: thread.sessionKey,
      title: fullThreadTitle(thread),
      pinned: thread.pinned,
      listState: thread.listState,
    }, point);
  }

// What a menu choice does. The thread is looked up again by its session key:
// the list may have been drawn again, or the thread archived elsewhere.
function threadMenuAction(sessionKey, action, detail) {
    var thread = threadForSession(sessionKey);
    if (!thread) {
      return;
    }
    if (action === THREAD_MENU_ACTION.RENAME) {
      sharedState.editingThreadId = thread.id;
      sharedState.renameDraft = null;
      renderThreads();
    } else if (action === THREAD_MENU_ACTION.DELETE) {
      archiveThread(thread.id);
    } else if (action === THREAD_MENU_ACTION.SNOOZE) {
      changeThreadPlacement(thread, action, { snooze_until: detail.until.toISOString() });
    } else {
      changeThreadPlacement(thread, action);
    }
  }

// The dot beside a row. What it means is read inside the row's button
// (threadReplyMark), where a screen reader announces it with the row.
function threadActivityIndicator() {
    var indicator = document.createElement("span");
    indicator.className = "thread-active-indicator";
    indicator.setAttribute("aria-hidden", "true");
    return indicator;
  }

// Who works on the thread in words ("Working: team lead"), else the reply
// dot in words, or "" when the row shows neither.
function threadReplyMark(thread) {
    var working = workingLabel(threadTrailView(thread));
    var unread = !thread.hasUnreadReply ? ""
      : thread.unreadReplyLevel === "attention" ? "Reply needs attention" : "New reply";
    // Both, when someone works and a reply is unread: sighted people see the
    // dot beside the pulsing triangles (review F5).
    return [working, unread].filter(Boolean).join(". ");
  }

// "active 5 minutes ago": the row's age as part of its name (review F12, F18).
function threadAgeWords(at, now) {
    return "active " + formatThreadAgeWords(at, now);
  }

// The name field with Save and Cancel; Enter saves and Escape cancels. What
// the person typed and the caret live in sharedState.renameDraft, so a list
// drawn again while they type keeps both (plan review C25).
function appendRenameControls(row, thread) {
    var draft = sharedState.renameDraft;
    if (!draft || draft.threadId !== thread.id) {
      draft = sharedState.renameDraft = { threadId: thread.id, value: fullThreadTitle(thread), start: -1, end: -1 };
    }
    var form = document.createElement("form");
    form.className = "thread-rename-form";
    // While someone works the row keeps pulsing during a rename: the working
    // triangles stand before the field (review F7).
    var working = threadTrailView(thread).triangles.filter(function (triangle) {
      return triangle.working;
    }).map(function (triangle) {
      return Object.assign({}, triangle, { hollow: false, down: false });
    });
    if (working.length) {
      form.appendChild(renderTrail(document, { triangles: working }));
    }
    var input = document.createElement("input");
    input.className = "thread-rename-input";
    input.type = "text";
    input.maxLength = 80;
    input.value = draft.value;
    input.setAttribute("aria-label", "Thread name");
    form.appendChild(input);
    var keepDraft = function () {
      draft.value = input.value;
      draft.start = input.selectionStart;
      draft.end = input.selectionEnd;
    };
    lifecycle.listen(input, "input", keepDraft);
    lifecycle.listen(input, "keyup", keepDraft);
    lifecycle.listen(input, "select", keepDraft);
    lifecycle.listen(form, "submit", function (event) {
      event.preventDefault();
      renameThread(thread.id, input.value);
    });
    lifecycle.listen(input, "keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        cancelThreadRename();
      }
    });
    row.appendChild(form);
    var save = document.createElement("button");
    save.type = "submit";
    save.className = "thread-action-button thread-rename-button";
    save.innerHTML = checkIconMarkup();
    save.title = "Save thread name";
    save.setAttribute("aria-label", "Save thread name for " + fullThreadTitle(thread));
    lifecycle.listen(save, "click", function (event) {
      event.stopPropagation();
      form.requestSubmit();
    });
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "thread-action-button thread-delete-button";
    cancel.innerHTML = closeIconMarkup();
    cancel.title = "Cancel rename";
    cancel.setAttribute("aria-label", "Cancel rename for " + fullThreadTitle(thread));
    lifecycle.listen(cancel, "click", function (event) {
      event.stopPropagation();
      cancelThreadRename();
    });
    row.appendChild(save);
    row.appendChild(cancel);
    lifecycle.timeout(function () {
      input.focus();
      if (draft.start < 0) {
        input.select();
      } else {
        input.setSelectionRange(draft.start, draft.end);
      }
    }, 0);
  }

// At sign-out: close the thread menu and the hover card (they live in the
// page body, outside the list lockChat empties) and forget the preview's
// message text and a half-typed name (plan review C29).
function clearThreadListOverlays() {
    shellState.threadMenu.close({ restoreFocus: false });
    shellState.threadPreview.clear();
    sharedState.renameDraft = null;
    // Changes still waiting, and what the status line says, belong to the
    // person who signed out (review F3).
    shellState.placementChains = new Map();
    shellState.threadListStatus.clear();
  }

function cancelThreadRename() {
    sharedState.editingThreadId = "";
    sharedState.renameDraft = null;
    renderThreads();
  }

function threadSectionCollapse() {
    if (!sharedState.sectionCollapse) {
      sharedState.sectionCollapse = createSectionCollapse(window.localStorage, (sharedState.storageKey || "ua.webchat.ui") + ".sections");
    }
    return sharedState.sectionCollapse;
  }

// What has keyboard focus in the list, read before the list is drawn again:
// a section header (by section), or a thread's row or rename field (by
// session key) with the rows shown just after and before it and its section,
// so focus has somewhere to go when the row moves into a collapsed section
// or goes away (review F4).
function focusedThreadListTarget() {
    var active = document.activeElement;
    if (!active || typeof active.closest !== "function" || !sharedState.threadList.contains(active)) {
      return null;
    }
    var header = active.closest(".thread-section-header");
    if (header) {
      var section = header.closest(".thread-section");
      return section ? { section: String(section.dataset.section || "") } : null;
    }
    var row = active.closest(".thread-row");
    if (!row || !row.dataset.sessionKey) {
      return null;
    }
    var shown = visibleThreadRows();
    var index = shown.indexOf(row);
    var rowSection = row.closest(".thread-section");
    return {
      sessionKey: String(row.dataset.sessionKey),
      next: index >= 0 && shown[index + 1] ? String(shown[index + 1].dataset.sessionKey || "") : "",
      previous: index > 0 ? String(shown[index - 1].dataset.sessionKey || "") : "",
      rowSection: rowSection ? String(rowSection.dataset.section || "") : "",
    };
  }

// Focus what `target` named, once the list is drawn again. A row still being
// renamed is left alone: its field takes focus itself. A row that moved out
// of view or went away hands focus to the row that took its place, else the
// one before it, else its section's header, else New Chat, never the page.
function restoreThreadListFocus(target) {
    if (!target) {
      return;
    }
    if (target.section) {
      // A section that is no longer drawn (its last chat moved) hands focus to New Chat.
      (sectionHeader(target.section) || sharedState.newThreadButton).focus();
      return;
    }
    var row = threadRowElement(target.sessionKey);
    if (row && row.classList.contains("is-renaming")) {
      return;
    }
    var candidates = [target.sessionKey, target.next, target.previous];
    for (var index = 0; index < candidates.length; index += 1) {
      var button = candidates[index] ? threadRowButton(candidates[index]) : null;
      if (button && !button.closest("[hidden]")) {
        button.focus();
        return;
      }
    }
    var fallback = target.rowSection ? sectionHeader(target.rowSection) : null;
    (fallback || sharedState.newThreadButton).focus();
  }

// Rows a person can see now: not inside a collapsed section.
function visibleThreadRows() {
    return Array.prototype.filter.call(sharedState.threadList.querySelectorAll(".thread-row"), function (row) {
      return Boolean(row.dataset.sessionKey) && !row.closest("[hidden]");
    });
  }

function sectionHeader(sectionId) {
    var sections = sharedState.threadList.querySelectorAll(".thread-section");
    for (var index = 0; index < sections.length; index += 1) {
      if (sections[index].dataset.section === sectionId) {
        return sections[index].querySelector(".thread-section-header");
      }
    }
    return null;
  }

function threadRowElement(sessionKey) {
    var rows = sharedState.threadList.querySelectorAll(".thread-row");
    for (var index = 0; index < rows.length; index += 1) {
      if (rows[index].dataset.sessionKey === sessionKey) {
        return rows[index];
      }
    }
    return null;
  }

function threadRowButton(sessionKey) {
    var row = threadRowElement(sessionKey);
    return row ? row.querySelector(".thread-button") : null;
  }

function lastActivityTimestamp(thread) {
    return Number(thread.lastActivityAt || 0);
  }

// Ages ("5m", and "active 5 minutes ago" for screen readers) move on once a
// minute, in place, without drawing the list again.
function refreshThreadAges() {
    var now = Date.now();
    var ages = sharedState.threadList.querySelectorAll(".thread-age");
    for (var index = 0; index < ages.length; index += 1) {
      ages[index].textContent = formatThreadAge(Number(ages[index].dataset.at), now);
    }
    var words = sharedState.threadList.querySelectorAll(".thread-age-words");
    for (var wordIndex = 0; wordIndex < words.length; wordIndex += 1) {
      words[wordIndex].textContent = threadAgeWords(Number(words[wordIndex].dataset.at), now);
    }
  }

// The server's row for a thread: where it sits and, when the server's copy
// is at least as new as what live events showed, its latest message.
function applyServerThreadRow(thread, session) {
    thread.hasServerRow = true;
    thread.pinned = Boolean(session.pinned);
    thread.listState = String(session.list_state || "");
    thread.needsYou = String(session.needs_you || "");
    thread.snoozedUntil = String(session.snoozed_until || "");
    thread.trail = session.trail || null;
    thread.working = Array.isArray(session.working) ? session.working : [];
    if (thread.listState !== LIST_STATE.WOKE) {
      thread.restoreRequested = false;
    }
    var serverSequence = Number(session.last_sequence || 0);
    if (serverSequence >= Number(thread.lastSequence || 0)) {
      thread.lastSequence = serverSequence;
      thread.lastActivityAt = Date.parse(session.last_activity_at || "") || Date.parse(session.updated_at || "") || 0;
      var message = session.last_message;
      thread.lastMessage = message && message.text
        ? { role: String(message.role || ""), text: plainSnippet(message.text) }
        : null;
    }
  }

// A live message moves the row's time and, when ordinary, its second line.
function applyEventToThreadRow(thread, event) {
    var sequence = Number(event.session_sequence || 0);
    if (sequence < Number(thread.lastSequence || 0)) {
      return;
    }
    thread.lastSequence = sequence;
    thread.lastActivityAt = Date.parse(event.created_at_utc || "") || Date.now();
    var snippet = messageSnippet({ role: event.role, content: event.content, attachments: event.attachments });
    if (snippet) {
      thread.lastMessage = snippet;
    }
  }

// A woke thread loses its mark once the person reads it (the rule the
// unread dot uses), with one clear_woke until the list shows it cleared.
// Only for a thread the server's list has shown in this sign-in: a mark drawn
// from the saved list may be older than a snooze made on another device
// (review F1). The person did not ask for it, so a failure shows nothing and
// is tried again after half a minute, never on every draw (plan review C23);
// so is a clear the gateway answered but left woke (review C05).
function restoreWokeThreadWhenRead(thread) {
    var retryAfterMs = 30 * 1000;
    if (thread.listState !== LIST_STATE.WOKE || !thread.hasServerRow || thread.restoreRequested
      || !isReadingThread(thread) || Date.now() < Number(thread.restoreRetryAt || 0)) {
      return;
    }
    thread.restoreRequested = true;
    var tryLater = function () {
      thread.restoreRequested = false;
      thread.restoreRetryAt = Date.now() + retryAfterMs;
    };
    sendThreadPlacement(thread, THREAD_PLACEMENT_ACTION.CLEAR_WOKE).then(function () {
      return fetchSessions({ fresh: true });
    }).then(function () {
      if (thread.listState === LIST_STATE.WOKE) tryLater();
    }).catch(tryLater);
  }

// Pin, unpin, snooze, dismiss or restore a thread on the server, then read
// the list again; every open page follows through the list's revision.
// Changes for one thread go out one after another, in the order the person
// chose them, so a change that waits for the list (below) is never sent after
// a later one (review C06). Resolves true when the change was saved.
function changeThreadPlacement(thread, action, extra) {
    if (!thread || !thread.persisted || !thread.sessionKey || !sharedState.authSessionActive) {
      return Promise.resolve(false);
    }
    var sessionKey = thread.sessionKey;
    // The sign-in the person chose this in: a change still queued when it
    // ends is dropped, even if the same person signs in again (review C13).
    var authGeneration = sharedState.authSessionGeneration;
    var chains = shellState.placementChains || (shellState.placementChains = new Map());
    var previous = chains.get(sessionKey) || Promise.resolve();
    var start = function () {
      return runThreadPlacement(thread, action, extra, authGeneration);
    };
    // A rejection before this one (not expected: each change resolves) never
    // stops the changes queued after it.
    var run = previous.then(start, start);
    chains.set(sessionKey, run);
    run.then(function () {
      if (chains.get(sessionKey) === run) chains.delete(sessionKey);
    });
    return run;
  }

// One placement change for the thread with `thread`'s session key, made
// while sign-in `authGeneration` lasts. Snooze and dismiss send the last
// message this page showed, so a message it never showed brings the thread
// back; a thread only known from the saved list waits for the server's list
// first, since its saved number could be 0 (review F2). A thread gone from
// the list by then is left alone. A refusal keeps the row and shows why.
// Nothing is shown once the person signed out or someone else signed in
// (review F3). A failure is shown in the status line beside the list, naming
// the chat that was not moved, with Retry only when the request did not get
// through (review F5).
function runThreadPlacement(thread, action, extra, authGeneration) {
    var sessionKey = thread.sessionKey;
    var current = function () {
      return sharedState.authSessionActive && sharedState.authSessionGeneration === authGeneration;
    };
    var named = quotedThreadTitle(thread);
    var needsSeen = action === THREAD_MENU_ACTION.SNOOZE || action === THREAD_MENU_ACTION.DISMISS;
    var listed = threadForSession(sessionKey);
    var ready = needsSeen && listed && !listed.hasServerRow ? fetchSessions() : Promise.resolve();
    return ready.then(function () {
      var target = current() ? threadForSession(sessionKey) : null;
      if (!target) {
        return false;
      }
      if (needsSeen && !target.hasServerRow) {
        throw new Error("The chat list has not loaded yet.");
      }
      return sendThreadPlacement(target, action, extra).then(function () {
        if (!current()) {
          return false;
        }
        shellState.threadListStatus.announce(THREAD_CHANGE_DONE[action] + " " + named + ".");
        return fetchSessions({ fresh: true }).then(function () { return true; });
      });
    }).catch(function (error) {
      if (!current()) {
        return false;
      }
      // The gateway's refusal says why, and repeating it cannot help; a
      // request that did not get through gets Retry for the same change.
      // Shown beside the list, in view and announced (review F5).
      shellState.threadListStatus.fail(threadChangeFailure(named, "moved", error), {
        retry: error.refused ? null : function () { return changeThreadPlacement(threadForSession(sessionKey) || thread, action, extra); },
      });
      return false;
    });
  }

// A chat's full name in quotes, as the status line names it.
function quotedThreadTitle(thread) {
    return "\u201c" + fullThreadTitle(thread) + "\u201d";
  }

// One placement request; rejects with the gateway's reason when refused.
function sendThreadPlacement(thread, action, extra) {
    var body = Object.assign({ thread_action: action }, extra || {});
    if (action === THREAD_MENU_ACTION.SNOOZE || action === THREAD_MENU_ACTION.DISMISS) {
      body.seen_sequence = Number(thread.lastSequence || 0);
    }
    return threadChangeRequest("/api/web-chat/sessions/" + encodeURIComponent(thread.sessionKey), {
      method: "PATCH",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

// One thread change from the menu (a placement or a delete). On failure it
// rejects with `refused` set when the gateway looked at the change and turned
// it down (4xx), and `reason` holding what it said, which may be empty.
function threadChangeRequest(url, init) {
    return requestClient.fetch(url, init).then(function (response) {
      return response.text().then(function (text) {
        if (!response.ok) {
          authentication.handleUnauthorizedResponse(response);
          var failure = new Error("Thread change failed.");
          failure.refused = response.status >= 400 && response.status < 500;
          failure.reason = refusalReason(text);
          throw failure;
        }
      });
    });
  }

// The gateway answers a refused change with its reason as plain text (some
// routes as JSON); "" when it gave none.
function refusalReason(text) {
    var reason = String(text || "").trim();
    if (reason.charAt(0) === "{") {
      try {
        var payload = JSON.parse(reason);
        reason = String(payload.message || payload.error || "");
      } catch (error) {
        reason = "";
      }
    }
    return reason;
  }

// The status line's sentence for a failed change: the gateway's reason for a
// refusal (or nothing more when it gave none, since repeating cannot help),
// otherwise "Try again." beside the Retry button (review C14).
function threadChangeFailure(named, verb, error) {
    var sentence = named + " was not " + verb + ".";
    if (error && error.refused) {
      return error.reason ? sentence + " " + error.reason : sentence;
    }
    return sentence + " " + TRY_AGAIN;
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
    sharedState.renameDraft = null;
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

function createMessageBubble(message, thread) {
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
    var delivery = thread.pendingDeliveries.get(message.clientRequestId);
    if (delivery && delivery.error) {
      var failure = uiElement("div", "message-delivery-status is-error", delivery.error + " ");
      failure.setAttribute("role", "status");
      var retry = uiElement("button", "button secondary small", "Retry");
      retry.type = "button";
      lifecycle.listen(retry, "click", delivery.retry);
      failure.appendChild(retry);
      bubble.appendChild(failure);
    } else if (message.localDeliveryState === "not_delivered") {
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
    if (notice && notice.hasAttribute("data-broker-card")) {
      // An expired broker card closes its frame and keeps the expired line (no Dismiss).
      var itemId = notice.getAttribute("data-item-id") || "";
      setBrokerCardState(activeThread(), itemId, "expired");
      brokerCards.close(itemId);
      lifecycle.timeout(renderMessages, 0);
      return;
    }
    var actions = notice && notice.querySelector(".message-transient-actions");
    if (!actions) {
      return;
    }
    var badge = notice.querySelector(".message-transient-badge");
    if (badge) {
      badge.textContent = "Expired";
      badge.classList.add("is-expired");
    }
    notice.classList.add("is-expired");
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

// An approved access proposal's credential forms become broker cards
// (access_inbox.mjs followUpPages). A decision on the card in the conversation
// (FOLLOW_UP_AFTER_REVIEW) puts them right after that review message, in the
// thread that holds it even if another thread is open by now. A decision in
// the Inbox panel (FOLLOW_UP_AFTER_LATEST) puts them after the open
// conversation's latest message, where the person is; an empty conversation
// gets them at its start. Returns the links shown as cards; the decided card
// keeps any other as a link.
function isReviewOf(requestId) {
    return function (message) {
      return String(message.accessRequestId || "") === String(requestId || "");
    };
  }

function addFollowUpPages(requestId, links, placement) {
    var reviewThread = placement === FOLLOW_UP_AFTER_REVIEW
      ? sharedState.threads.find(function (candidate) { return candidate.messages.some(isReviewOf(requestId)); })
      : null;
    var thread = reviewThread || activeThread();
    if (!thread) {
      return [];
    }
    var review = reviewThread ? reviewThread.messages.find(isReviewOf(requestId)) : null;
    var position = review
      ? Number(review.sessionSequence || 0)
      : window.UAChatDelivery.latestDurableSessionSequence(thread.messages);
    var pages = window.UAChatDelivery.followUpPageItems(requestId, position, links, window.location.origin);
    if (!pages.length) {
      console.warn("An approved request's credential forms could not be shown as cards; they stay links.", requestId);
      return [];
    }
    pages.forEach(function (page) {
      thread.transientItems = window.UAChatDelivery.upsertTransientItem(thread.transientItems, page.item);
    });
    thread.transientItems = window.UAChatDelivery.reconcileTransientItems(thread.transientItems, thread.messages);
    // Drawn on the next tick: the caller first saves the decided card that says where the forms are.
    lifecycle.timeout(renderMessages, 0);
    return pages.map(function (page) { return page.link; });
  }

// "Show in the conversation" on the Inbox panel: after Settings closed, bring the
// request's first form card into view (the person asked for it, so the
// transcript may move).
function showFollowUpPages(requestId) {
    lifecycle.timeout(function () {
      var prefix = "account_link:" + String(requestId || "") + ":";
      var card = Array.from(sharedState.messageList.querySelectorAll("[data-broker-card]")).find(function (node) {
        return String(node.getAttribute("data-item-id") || "").indexOf(prefix) === 0;
      });
      if (card) {
        card.scrollIntoView({ block: "center" });
      }
    }, 0);
  }

function setBrokerCardState(thread, itemId, state) {
    if (!thread || !itemId) {
      return;
    }
    thread.brokerCardStates = thread.brokerCardStates || new Map();
    thread.brokerCardStates.set(itemId, state);
  }

function createTransientItemCard(thread, item) {
    if (item.frameUrl) {
      // A broker page: shown inside the card (broker_card.mjs), never with Dismiss.
      return brokerCardElement({
        document: document,
        cards: brokerCards,
        item: item,
        state: thread.brokerCardStates ? thread.brokerCardStates.get(item.itemId) || "" : "",
        expiredMessage: shellState.expiredSecureFormMessage,
        onState: function (state) {
          setBrokerCardState(thread, item.itemId, state);
          renderMessages();
        },
      });
    }
    var transientNotice = document.createElement("div");
    transientNotice.className = item.url
      ? "message-transient-notice is-link-card"
      : "message-transient-notice";
    transientNotice.setAttribute("data-item-id", item.itemId);
    if (item.createdAt) {
      transientNotice.setAttribute("data-created-at", item.createdAt);
    }
    var transientText = document.createElement("pre");
    transientText.textContent = item.content;
    var transientActions = document.createElement("div");
    transientActions.className = "message-transient-actions";
    if (item.url) {
      var transientHeader = document.createElement("div");
      transientHeader.className = "message-transient-header";
      var transientBadge = document.createElement("span");
      transientBadge.className = "message-transient-badge";
      transientBadge.textContent = "Action needed";
      transientHeader.appendChild(transientBadge);
      if (item.expiresAtUtc) {
        var transientCountdown = document.createElement("span");
        transientCountdown.className = "message-transient-countdown";
        transientCountdown.setAttribute("data-expires-at", item.expiresAtUtc);
        transientCountdown.setAttribute("role", "status");
        transientHeader.appendChild(transientCountdown);
      }
      transientNotice.appendChild(transientHeader);
      var transientOpen = document.createElement("a");
      transientOpen.className = "button primary small message-transient-open";
      transientOpen.textContent = item.label;
      transientOpen.href = item.url;
      transientOpen.target = "_blank";
      transientOpen.rel = "noopener noreferrer";
      transientActions.appendChild(transientOpen);
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
    transientDismiss.className = item.url
      ? "button ghost small message-transient-dismiss"
      : "button secondary small message-transient-dismiss";
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
    // The switch shows the selected thread's setting.
    renderParallelSwitch();
    // Built first, then placed: a live broker card stays in the page while the
    // rest is replaced around it, because a frame that leaves the page reloads
    // and loses what was typed (broker_card.mjs replaceChildrenKeeping).
    var nodes = [];
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
        if (errorNotice.retry) {
          errorNotice.retry();
        } else if (errorThread && errorNotice.source === shellState.SYSTEM_ERROR_SOURCE.HYDRATE_THREAD) {
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
      nodes.push(errorBanner);
    }
    nodes.push(createWelcomeGuidance());
    if (thread && thread.hasMoreHistory && thread.historyBeforeCursor) {
      var earlierButton = document.createElement("button");
      earlierButton.type = "button";
      earlierButton.className = "button secondary small history-load-earlier";
      earlierButton.textContent = thread.historyRequest ? "Loading earlier messages…" : "Load earlier messages";
      earlierButton.disabled = Boolean(thread.historyRequest);
      lifecycle.listen(earlierButton, "click", function () {
        loadEarlierHistory();
      });
      nodes.push(earlierButton);
    }
    if (thread) {
      window.UAChatDelivery.buildTimeline(
        thread.messages,
        thread.transientItems
      ).forEach(function (item) {
        if (item.kind === "transient_item") {
          nodes.push(createTransientItemCard(thread, item.value));
          return;
        }
        nodes.push(createMessageBubble(item.value, thread));
      });
    }
    // Frames of cards no longer shown (the item left, another thread is open) close here.
    brokerCards.retainOnly(nodes.filter(function (node) {
      return node.hasAttribute && node.hasAttribute("data-broker-frame");
    }).map(function (node) {
      return node.getAttribute("data-item-id");
    }));
    var list = sharedState.messageList;
    // Focus on a broker card's control (such as "Open in its own tab") that this
    // render replaces goes to the message box, not to the page body.
    var focused = document.activeElement;
    var focusedInCard = Boolean(focused && focused.closest && list.contains(focused) && focused.closest("[data-broker-card]"));
    var live = new Set(nodes.filter(function (node) { return node.parentNode === list; }));
    if (!replaceChildrenKeeping(list, nodes, live)) {
      console.warn(
        "Web Chat rebuilt the conversation around open secure pages; their forms reloaded.",
        Array.from(live).map(function (node) { return node.getAttribute("data-item-id"); })
      );
    }
    if (focusedInCard && !focused.isConnected) {
      focusMessageBox();
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

function appendSystemError(thread, source, message, retry) {
    var notice = window.UAChatDelivery.createErrorNotice(source, message);
    if (retry) notice.retry = retry;
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

function runCommand(command, options) {
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
    sendMessage(command, { ...options, routedCommand: true });
  }

function sendMessage(content, options) {
    options = options || {};
    var attachments = Array.isArray(options.attachments) ? options.attachments : [];
    if (content.charAt(0) === "/" && !attachments.length && !options.routedCommand) {
      runCommand(content, options);
      return;
    }
    var thread = options.thread || activeThread();
    if (!thread) {
      createServerThread().then(function (created) {
        if (created) { sendMessage(content, { ...options, thread: created }); }
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
    var authGeneration = sharedState.authSessionGeneration;
    var current = function () {
      return sharedState.authSessionActive && sharedState.authSessionGeneration === authGeneration
        && sharedState.threads.includes(thread);
    };
    if (!current()) return;
    // Retain both the destination and identity across an uncertain HTTP outcome.
    // A replay reaches the server's existing exchange before it saves files.
    var delivery = options.delivery || { clientRequestId: shellState.operationalIds.newId("web_request") };
    if (delivery.inFlight) return;
    delivery.inFlight = true;
    delivery.error = "";
    var clientRequestId = delivery.clientRequestId;
    thread.pendingDeliveries.set(clientRequestId, delivery);
    delivery.retry = function () {
      if (current() && thread.pendingDeliveries.get(clientRequestId) === delivery) {
        sendMessage(content, { ...options, thread: thread, delivery: delivery });
      }
    };
    // The first ordinary message names the thread, as the server will;
    // slash commands never do (web_chat_threads.py).
    if (thread.title === NEW_CHAT_TITLE && content.trim().charAt(0) !== "/") {
      var titleSource = content.trim() || (attachments[0] && attachments[0].filename) || "Attachment";
      thread.title = titleSource;
      thread.titleFull = normalizeThreadTitle(titleSource) || "Attachment";
    }
    if (isReadingThread(thread)) setThreadUnread(thread, false);
    var anchorSessionSequence = window.UAChatDelivery.latestDurableSessionSequence(
      thread.messages
    );
    if (!thread.messages.some(function (message) { return message.clientRequestId === clientRequestId; })) {
      thread.messages.push(optimisticMessage(clientRequestId, content, attachments, anchorSessionSequence));
    }
    shellState.deliveryTracker.watch(clientRequestId);
    var wasWaitingForReply = Boolean(thread.pendingReply || pendingReplyCount(thread) > 0);
    var startsTerminalReplyWait = !wasWaitingForReply;
    var startsRequest = sendStartsRequest(thread, content);
    if (startsTerminalReplyWait) {
      startTerminalReplyWait(thread, startsRequest);
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
    if (activeThread() === thread) renderMessages({ scrollToBottom: true });
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
      if (!current()) return null;
      return response.json().catch(function () {
        return {};
      }).then(function (payload) {
        if (!current()) return null;
        if (!response.ok) {
          authentication.handleUnauthorizedResponse(response);
          throw new Error(payload.message || payload.error || response.statusText || "Message failed.");
        }
        return payload;
      });
    }).then(function (payload) {
      if (!current()) return;
      var responseSessionKey = String(payload.session_key || thread.sessionKey);
      thread.pendingDeliveries.delete(clientRequestId);
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
      if (!current()) return;
      if (startsTerminalReplyWait) {
        finishTerminalReplyWait(thread);
      } else if (pendingReplyCount(thread) === 0) {
        setThreadPending(thread, false);
      }
      // An SSE acknowledgement may precede a lost HTTP reply. Only the
      // still-unaccepted request can fail, never another message in the chat.
      if (thread.pendingDeliveries.get(clientRequestId) === delivery) {
        delivery.error = error.message || "Message failed.";
      }
      thread.updatedAt = Date.now();
      saveThreads();
      renderThreads();
      renderMessages();
    }).finally(function () { delivery.inFlight = false; });
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

lifecycle.listen(sharedState.settingsButton, "click", async function () {
    var generation = sharedState.authSessionGeneration;
    var current = () => sharedState.authSessionActive && sharedState.authSessionGeneration === generation;
    try {
      var thread = activeThread();
      if (!thread) {
        // Sign-in shows the shell before its saved conversations arrive.
        // Absence during that lookup must not create a fresh conversation.
        var loaded = await fetchSessions();
        if (!current()) return;
        if (!loaded) throw new Error("Your conversations could not be loaded. Select Settings to retry.");
        thread = activeThread() || await createServerThread();
      }
      if (!thread || !current()) return;
      await sharedState.settingsControls.open(null, thread.sessionKey);
      if (current()) {
        if (clearSystemError(thread, shellState.SYSTEM_ERROR_SOURCE.OPEN_SETTINGS)) {
          renderMessages();
        }
      }
    } catch (error) {
      if (current()) {
        appendSystemError(
          thread || activeThread(),
          shellState.SYSTEM_ERROR_SOURCE.OPEN_SETTINGS,
          error.message || "Unable to open Settings."
        );
        saveThreads();
        renderMessages();
      }
    }
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

shellState.chatSubmission = createChatSubmission({
  readDraft: () => ({
    content: sharedState.messageInput.value.trim(),
    hasAttachments: shellState.pendingAttachments.length > 0,
    parallel: Boolean(shellState.newThreadParallel),
  }),
  context: () => ({
    authenticated: sharedState.authSessionActive,
    generation: sharedState.authSessionGeneration,
    thread: activeThread(),
  }),
  createThread: createServerThread,
  setParallel: (thread, value) => saveParallelSetting(thread, value).then(() => { shellState.newThreadParallel = false; }),
  takeDraft: () => {
    sharedState.messageInput.value = "";
    return detachPendingAttachmentsForSend();
  },
  send: sendMessage,
  preparing: (pending) => {
    sharedState.messageForm.querySelectorAll("input, textarea, button").forEach(control => { control.disabled = pending; });
    sharedState.newThreadButton.disabled = pending;
    sharedState.messageForm.setAttribute("aria-busy", String(pending));
    const status = document.getElementById("composer-status");
    status.hidden = !pending;
    status.textContent = pending ? "Preparing your chat…" : "";
    // Re-enabling the form must not unlock a switch that is locked.
    renderParallelSwitch();
  },
  failed: (error, retry) => {
    appendSystemError(activeThread(), shellState.SYSTEM_ERROR_SOURCE.CREATE_THREAD,
      error.message || "Unable to create a chat thread.", retry);
    renderMessages();
  },
});

authentication.resumeWebChatSession();

lifecycle.listen(sharedState.messageForm, "submit", function (event) {
    event.preventDefault();
    shellState.chatSubmission.submit();
  });

lifecycle.listen(shellState.separateRequest, "change", function () {
    var thread = activeThread();
    var value = shellState.separateRequest.checked;
    if (!thread) {
      shellState.newThreadParallel = value;
      renderParallelSwitch();
      return;
    }
    // The error is shown by the switch (renderParallelSwitch).
    saveParallelSetting(thread, value).catch(function () {});
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

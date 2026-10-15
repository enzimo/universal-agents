/**
 * The Web Chat Settings dialog: its tab rail and the views behind it.
 *
 * Views, in rail order: Inbox (access requests and notices, access_inbox.mjs),
 * Access (who can use what, access_map.mjs), Accounts (whose account agents
 * use for each service, read only, accounts_view.mjs), Team (team_controls.mjs), Tasks
 * (task_controls.js), Memories (memory_controls.mjs) and Appearance (drawn by
 * chat_appearance.mjs through `onAppearanceOpen`).
 *
 * The cog reopens the last selected view in this signed-in page session; the
 * first view is Inbox, and sign-out resets it. Links choose their own view:
 * an Inbox link (`#access_request=<id>`) highlights that request and an Access
 * link (`#agent=<type>`) opens that agent type. Opening a view never decides
 * or changes anything.
 *
 * Attention counts (ui/DESIGN.md, "Attention counts"): the Inbox tab counts
 * requests this person can decide plus their unread notices, kept by one
 * page-wide count (access_inbox.mjs `createInboxCounts`) that runs while
 * signed in. The Knowledge link counts proposals waiting for this person
 * (`startKnowledgeCount`, while the knowledge base is on). The cog shows the
 * sum of both, and its accessible name says what each part counts.
 *
 * Elements come from chat.html by id. Every read and write uses one Settings
 * client (settings_support.mjs) with the signed-in session and CSRF token;
 * the chat's access review cards (chat_access_review.mjs) use it too, so they
 * decide with the same session as the Inbox.
 */

import {
  createAccessInbox, createInboxCounts, inboxBadgeLabel, inboxCountWords, inboxCounts as emptyInboxCounts,
} from "./access_inbox.mjs";
import { createAccessMap } from "./access_map.mjs";
import { createAccountsView } from "./accounts_view.mjs";
import { createChatAccessReviews } from "./chat_access_review.mjs";
import { createMemoryControls } from "./memory_controls.mjs";
import { proposalCountLabel } from "./knowledge_page.mjs";
import {
  createAttentionCount, createSettingsClient, failureMessage, nameWithCount, setStatusLine, showCountBadge,
} from "./settings_support.mjs";
import { createTeamControls } from "./team_controls.mjs";

export const SETTINGS_VIEWS = Object.freeze({
  INBOX: "inbox", ACCESS: "access", ACCOUNTS: "accounts", TEAM: "team", TASKS: "tasks", MEMORIES: "memories",
  APPEARANCE: "appearance",
});
const VIEW_ORDER = Object.freeze(Object.values(SETTINGS_VIEWS));
// Tasks and Memories belong to a conversation; the other views do not.
const VIEWS_NEEDING_CONVERSATION = new Set([SETTINGS_VIEWS.TASKS, SETTINGS_VIEWS.MEMORIES]);
export const NEEDS_CONVERSATION_MESSAGE = "Open a conversation before managing tasks or memories.";

const ACCESS_READ_FALLBACK = "Access could not be loaded because the gateway returned no explanation. Refresh and try again.";
const ACCESS_WRITE_FALLBACK = "The change was not saved because the gateway returned no explanation. Refresh and try again.";

/**
 * Creates the Settings controls for chat.html.
 *
 * - `returnFocus`: the element that gets focus back when Settings closes.
 * - `teamActivity`: the page's copy of the team's live status (team_activity.mjs) for Team and Tasks.
 * - `onClose()`, `onUnauthorized(response)`, `onAppearanceOpen()`: page callbacks.
 * - `createInbox`, `createCounts`, `createMap`, `createTasks`, `createChatReviews`: replaceable in tests.
 */
export function createSettingsControls({
  browser = globalThis, returnFocus, onClose, onUnauthorized, onAppearanceOpen, teamActivity,
  createInbox = createAccessInbox, createCounts = createInboxCounts, createMap = createAccessMap,
  createChatReviews = createChatAccessReviews,
  createTasks = options => browser.UATaskControls.createTeam(options),
}) {
  const document = browser.document;
  const byId = id => {
    const node = document.getElementById(id);
    if (!node) throw new Error(`Settings cannot start: chat.html has no element #${id}.`);
    return node;
  };
  const overlay = byId("chat-settings-overlay");
  const dialog = byId("settings-dialog");
  const tabs = Object.fromEntries(VIEW_ORDER.map(view => [view, byId(`settings-${view}-tab`)]));
  const panels = Object.fromEntries(VIEW_ORDER.map(view => [view, byId(`settings-${view}-panel`)]));
  const inboxCount = byId("settings-inbox-count");
  const cogCount = byId("settings-button-count");
  const knowledgeLink = byId("settings-knowledge-link");
  const knowledgeCount = byId("settings-knowledge-count");
  const inboxRefresh = byId("settings-inbox-refresh");
  const inboxStatus = byId("settings-inbox-status");
  const accessRefresh = byId("settings-access-refresh");
  const accountsRefresh = byId("settings-accounts-refresh");
  const memoriesStatus = byId("settings-memories-status");

  const client = createSettingsClient({ browser, onUnauthorized });
  const state = { view: SETTINGS_VIEWS.INBOX, sessionKey: "" };
  const getSessionKey = () => state.sessionKey;

  const badges = { inbox: emptyInboxCounts(null), proposals: 0 };
  const inboxCounts = createCounts({
    get: path => client.get(path, ACCESS_READ_FALLBACK),
    onChange: counts => { badges.inbox = counts; renderBadges(); },
    browser,
  });
  // Proposals waiting for this person; the page starts it with its read once it knows the knowledge base is on.
  let readProposals = null;
  const proposalCount = createAttentionCount({
    read: () => readProposals(),
    onChange: count => { badges.proposals = count; renderBadges(); },
    warning: "ua.settings: the Knowledge proposal count could not be read; retrying.",
    browser,
  });
  const accessInbox = createInbox({
    container: byId("settings-access-inbox"),
    get: path => client.get(path, ACCESS_READ_FALLBACK),
    post: (path, body) => client.post(path, body, ACCESS_WRITE_FALLBACK),
    beginAction: client.beginAction,
    failureMessage,
    onCounts: inboxCounts.set,
    paginate: client.paginate,
    browser,
  });
  const accessMap = createMap({
    container: byId("settings-access-map"),
    get: path => client.get(path, ACCESS_READ_FALLBACK),
    post: (path, body) => client.post(path, body, ACCESS_WRITE_FALLBACK),
    beginAction: client.beginAction,
    beginRefresh: () => client.beginRefresh(accessRefresh),
    failureMessage,
    paginate: client.paginate,
    browser,
  });
  const accounts = createAccountsView({
    container: byId("settings-accounts-content"),
    status: byId("settings-accounts-status"),
    get: path => client.get(path, ACCESS_READ_FALLBACK),
    beginRefresh: () => client.beginRefresh(accountsRefresh),
    failureMessage,
    browser,
  });
  const team = createTeamControls({
    panel: panels.team,
    content: byId("settings-team-content"),
    proceduresContainer: byId("settings-procedures"),
    status: byId("settings-team-status"),
    refreshButton: byId("settings-team-refresh"),
    client, getSessionKey, teamActivity, browser,
  });
  const memories = createMemoryControls({
    content: byId("settings-memories-content"),
    status: memoriesStatus,
    refreshButton: byId("settings-memories-refresh"),
    newButton: byId("settings-memories-new"),
    personalTab: byId("memory-personal-tab"),
    teamTab: byId("memory-team-tab"),
    client, getSessionKey, browser,
    isVisible: () => !overlay.hidden && !panels.memories.hidden,
  });
  const tasks = createTasks({ panel: panels.tasks, headers: client.writeHeaders, onUnauthorized, teamActivity });
  const chatReviews = createChatReviews({
    get: path => client.get(path, ACCESS_READ_FALLBACK),
    post: (path, body) => client.post(path, body, ACCESS_WRITE_FALLBACK),
    beginAction: client.beginAction,
    failureMessage,
    // Opening the Inbox at the request never decides anything.
    openInbox: requestId => open(SETTINGS_VIEWS.INBOX, state.sessionKey, { accessRequestId: requestId }),
    // A decision in chat changes the Inbox list and its badge.
    onDecided: () => { accessInbox.load({ keepOutcomes: true }); },
    browser,
  });

  /** The Inbox tab, Knowledge link and cog badges (module docstring). */
  function renderBadges() {
    const inboxTotal = badges.inbox.decidableRequests + badges.inbox.unreadNotices;
    const inboxLabel = inboxBadgeLabel(badges.inbox);
    const proposalsLabel = proposalCountLabel(badges.proposals);
    showCountBadge(inboxCount, inboxTotal, inboxLabel);
    showCountBadge(knowledgeCount, badges.proposals, proposalsLabel && `Knowledge: ${proposalsLabel}`);
    const label = [inboxLabel, proposalsLabel && `Knowledge: ${proposalsLabel}`].filter(Boolean).join("; ");
    showCountBadge(cogCount, inboxTotal + badges.proposals, label);
    // Each control's name says what its number counts; the cog's badge itself is hidden from screen readers.
    nameWithCount(tabs[SETTINGS_VIEWS.INBOX], "Inbox", inboxCountWords(badges.inbox));
    nameWithCount(knowledgeLink, "Knowledge", proposalsLabel);
    if (returnFocus && typeof returnFocus.setAttribute === "function") nameWithCount(returnFocus, "Open Settings", label);
  }

  function refreshInbox() {
    const finish = client.beginRefresh(inboxRefresh);
    return accessInbox.load().finally(finish);
  }

  function setPrimaryTab(view) {
    state.view = view;
    VIEW_ORDER.forEach(name => {
      const selected = name === view;
      tabs[name].classList.toggle("is-active", selected);
      tabs[name].setAttribute("aria-selected", selected ? "true" : "false");
      panels[name].hidden = !selected;
    });
    if (view === SETTINGS_VIEWS.TASKS) tasks.activate(state.sessionKey);
    else tasks.deactivate();
    // Team follows the team's live status only while it is on screen.
    if (view !== SETTINGS_VIEWS.TEAM || overlay.hidden) team.deactivate();
  }

  /** Switches to a view and reads its content. Resolves when the first read finishes. */
  function selectView(view, { accessRequestId = "", agentType } = {}) {
    setPrimaryTab(view);
    switch (view) {
      case SETTINGS_VIEWS.INBOX:
        if (accessRequestId) accessInbox.focusRequest(accessRequestId);
        return refreshInbox();
      case SETTINGS_VIEWS.ACCESS:
        return agentType === undefined ? accessMap.load() : accessMap.open(agentType);
      case SETTINGS_VIEWS.ACCOUNTS:
        return accounts.load();
      case SETTINGS_VIEWS.TEAM:
        return team.activate();
      case SETTINGS_VIEWS.MEMORIES:
        if (!state.sessionKey) {
          setStatusLine(memoriesStatus, "Open a conversation before managing memories.", true);
          return Promise.resolve();
        }
        return memories.load();
      case SETTINGS_VIEWS.APPEARANCE:
        if (typeof onAppearanceOpen === "function") onAppearanceOpen();
        return Promise.resolve();
      default:
        return Promise.resolve();
    }
  }

  function show() {
    overlay.hidden = false;
    overlay.setAttribute("aria-hidden", "false");
    browser.requestAnimationFrame(() => {
      overlay.classList.add("is-open");
      dialog.focus();
    });
  }

  function close() {
    tasks.deactivate();
    team.deactivate();
    overlay.classList.remove("is-open");
    if (overlay.contains(document.activeElement) && returnFocus && typeof returnFocus.focus === "function") {
      returnFocus.focus();
    }
    overlay.setAttribute("aria-hidden", "true");
    overlay.hidden = true;
    setStatusLine(inboxStatus, "", false);
    team.resetStatus();
    memories.resetStatus();
    if (typeof onClose === "function") onClose();
  }

  /**
   * Opens Settings at `view` (or the last view) for the conversation
   * `sessionKey`. `accessRequestId` highlights one request in the Inbox.
   */
  function open(view, sessionKey, { accessRequestId = "" } = {}) {
    const requested = VIEW_ORDER.includes(view) ? view : state.view;
    const key = String(sessionKey || "");
    if (VIEWS_NEEDING_CONVERSATION.has(requested) && !key) {
      return Promise.reject(new Error(NEEDS_CONVERSATION_MESSAGE));
    }
    state.sessionKey = key;
    show();
    return selectView(requested, { accessRequestId });
  }

  /** Opens the Access page at one agent type (Access links: `?view=access#agent=<type>`). */
  function openAccess(agentType) {
    show();
    return selectView(SETTINGS_VIEWS.ACCESS, { agentType: String(agentType || "") });
  }

  VIEW_ORDER.forEach(view => tabs[view].addEventListener("click", () => { selectView(view); }));
  inboxRefresh.addEventListener("click", () => { refreshInbox(); });
  accessRefresh.addEventListener("click", () => { accessMap.load(); });
  accountsRefresh.addEventListener("click", () => { accounts.load(); });
  byId("settings-dialog-close").addEventListener("click", close);
  overlay.addEventListener("click", event => {
    if (event.target === overlay) close();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !overlay.hidden) close();
  });

  return {
    open,
    openAccess,
    close,
    setCsrfToken: client.setCsrfToken,
    /** Signed in: keep the cog's Inbox badge current. */
    startInboxCount: inboxCounts.start,
    /**
     * The knowledge base is on: keep the Knowledge link's badge (and its part
     * of the cog's) current. `read()` resolves the number of proposals waiting
     * for this person (knowledge_page.mjs `readPendingProposals`).
     */
    startKnowledgeCount(read) {
      readProposals = read;
      proposalCount.start();
    },
    /**
     * A message arrived from an agent. Access requests and stuck-work reports
     * come with one, so the badge is read again shortly (several messages
     * close together give one read).
     */
    noteAgentMessage: inboxCounts.refreshSoon,
    /** Adds the access review card to a chat message bubble (chat_access_review.mjs). */
    mountAccessReview: (bubble, message) => chatReviews.mount(bubble, message),
    /** Sign-out: forget the session, every view's content and the last view. */
    clear() {
      state.view = SETTINGS_VIEWS.INBOX;
      state.sessionKey = "";
      client.setCsrfToken("");
      tasks.clear();
      chatReviews.clear();
      accessInbox.clear();
      inboxCounts.stop();
      proposalCount.stop();
      accessMap.clear();
      accounts.clear();
      team.clear();
      memories.clear();
      close();
    },
  };
}

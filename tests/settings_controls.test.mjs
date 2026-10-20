import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  NEEDS_CONVERSATION_MESSAGE, SETTINGS_VIEWS, createSettingsControls,
} from "../assistant_gateway/onboarding_web/assets/settings_controls.mjs";
import { CSRF_HEADER } from "../assistant_gateway/onboarding_web/assets/settings_support.mjs";
import { answer, byClass, fakeBrowser, jsonResponse, openRequests, settle } from "./settings_fake_dom.mjs";

const CHAT_HTML = readFileSync(new URL("../assistant_gateway/onboarding_web/assets/chat.html", import.meta.url), "utf8");
const CHAT_IDS = [...CHAT_HTML.matchAll(/\sid="([^"]+)"/g)].map(match => match[1]);
const VIEWS = Object.values(SETTINGS_VIEWS);

/** Every element chat.html declares, keyed by id (flat: Settings only looks elements up by id). */
function pageFromChatHtml(skip = []) {
  const env = fakeBrowser();
  CHAT_IDS.filter(id => !skip.includes(id)).forEach(id => {
    const node = env.document.createElement("div");
    node.id = id;
    env.document.body.append(node);
  });
  env.document.getElementById("chat-settings-overlay").hidden = true;
  return env;
}

function fixture() {
  const env = pageFromChatHtml();
  const node = id => env.document.getElementById(id);
  const tasks = [], maps = [], closes = [], appearance = [];
  const returnFocus = env.document.createElement("button");
  env.document.body.append(returnFocus);
  const subscribers = new Set();
  const teamActivity = {
    subscribe(listener) { subscribers.add(listener); return () => subscribers.delete(listener); },
    current: () => null,
    listeners: () => subscribers.size,
  };
  const settings = createSettingsControls({
    browser: env.browser, returnFocus, teamActivity,
    onClose: () => closes.push(true),
    onAppearanceOpen: () => appearance.push(true),
    createMap: () => ({
      load() { maps.push(["load"]); return Promise.resolve(); },
      open(agentType) { maps.push(["open", agentType]); return Promise.resolve(); },
      clear() { maps.push(["clear"]); },
    }),
    createTasks: options => {
      tasks.push(["create", options.panel.id, options.headers]);
      return {
        activate: key => tasks.push(["activate", key]),
        deactivate: () => tasks.push(["deactivate"]),
        clear: () => tasks.push(["clear"]),
      };
    },
  });
  settings.setCsrfToken("csrf-3");
  return { ...env, node, settings, tasks, maps, closes, appearance, returnFocus, teamActivity,
    overlay: node("chat-settings-overlay") };
}

function selectedView(f) {
  const selected = VIEWS.filter(view => f.node(`settings-${view}-tab`).getAttribute("aria-selected") === "true");
  const visible = VIEWS.filter(view => !f.node(`settings-${view}-panel`).hidden);
  assert.deepEqual(selected, visible, "the selected tab and the visible panel match");
  assert.equal(selected.length, 1);
  return selected[0];
}

function review(overrides = {}) {
  return { request_id: "req-1", title: "An agent asks for access", can_decide: true,
    choices: [{ choice: "approve", label: "Approve" }, { choice: "deny", label: "Deny" }],
    created_at_utc: "2026-09-25T15:04:00Z", ...overrides };
}

async function answerTeam(f) {
  await answer(f.requests, "/api/web-chat/team", jsonResponse({ members: [], definitions: [], mcp_connections: [], hosted_mcp_servers: [] }));
  // The first procedure read for a conversation confirms its session.
  if (openRequests(f.requests).some(request => request.path === "/api/web-chat/session-context")) {
    await answer(f.requests, "/api/web-chat/session-context", jsonResponse({ status: "ok" }));
  }
  await answer(f.requests, "/api/web-chat/procedures/proposals", jsonResponse({ proposals: [] }));
  await answer(f.requests, "/api/web-chat/procedures/installed", jsonResponse({ definitions: [] }));
  // Team > File Retention (file_retention_controls.mjs) is read with the rest of the tab.
  await answer(f.requests, "/api/web-chat/file-retention",
    jsonResponse({ upload_days: 90, scratch_days: 21, max_days: 3650, can_change: false }));
}

test("Settings finds every element it needs in chat.html, and the Permissions tab is gone", () => {
  assert.doesNotThrow(() => fixture());
  for (const removed of ["settings-permissions-tab", "settings-permissions-panel", "settings-inbox-content",
    "inbox-pending-tab", "permission-pending-tab", "permission-installed-tab"]) {
    assert.ok(!CHAT_IDS.includes(removed), `${removed} was removed from chat.html`);
  }
  assert.doesNotMatch(CHAT_HTML, /permission_controls\.js/);
  const env = pageFromChatHtml(["settings-procedures"]);
  assert.throws(() => createSettingsControls({ browser: env.browser, createMap: () => ({}), createTasks: () => ({}) }),
    /chat\.html has no element #settings-procedures/);
});

test("tabs switch panels in place and read each view", async () => {
  const f = fixture();
  const [, panelId, taskHeaders] = f.tasks[0];
  assert.equal(panelId, "settings-tasks-panel");
  assert.deepEqual(taskHeaders(), { "Content-Type": "application/json", [CSRF_HEADER]: "csrf-3" },
    "task writes use the current CSRF token");
  const opened = f.settings.open("team", "session-1");
  assert.equal(f.overlay.hidden, false);
  assert.equal(f.overlay.getAttribute("aria-hidden"), "false");
  assert.equal(selectedView(f), "team");
  await answerTeam(f);
  await opened;
  assert.match(f.requests[0].path, /^\/api\/web-chat\/team\?session_key=session-1$/);

  f.node("settings-memories-tab").click();
  assert.equal(selectedView(f), "memories");
  const memories = await answer(f.requests, "/api/web-chat/memories?", jsonResponse({ memories: [], total: 0 }));
  assert.match(memories.path, /session_key=session-1&scope=personal/);

  f.node("settings-access-tab").click();
  assert.equal(selectedView(f), "access");
  assert.deepEqual(f.maps.at(-1), ["load"]);

  f.node("settings-accounts-tab").click();
  assert.equal(selectedView(f), "accounts");
  await answer(f.requests, "/api/web-chat/accounts", jsonResponse({
    services: [{ subject: "model", name: "Model keys", sharing: "shared", fixed: false }], can_change: false,
  }));
  assert.match(f.node("settings-accounts-content").textContent, /Model keys/);
  f.node("settings-accounts-refresh").click();
  await answer(f.requests, "/api/web-chat/accounts", jsonResponse({ services: [] }));
  assert.match(f.node("settings-accounts-content").textContent, /No service uses an account/);

  f.node("settings-tasks-tab").click();
  assert.equal(selectedView(f), "tasks");
  assert.deepEqual(f.tasks.at(-1), ["activate", "session-1"]);

  f.node("settings-appearance-tab").click();
  assert.equal(selectedView(f), "appearance");
  assert.equal(f.appearance.length, 1);
  assert.deepEqual(f.tasks.at(-1), ["deactivate"], "Tasks stop refreshing when another tab opens");
});

test("Team follows the live team status only while it is open", async () => {
  const f = fixture();
  const opened = f.settings.open("team", "session-1");
  await answerTeam(f);
  await opened;
  assert.equal(f.teamActivity.listeners(), 1, "the open Team tab follows the live status");
  f.node("settings-inbox-tab").click();
  assert.equal(f.teamActivity.listeners(), 0, "switching tabs stops it");
  await answer(f.requests, "/api/web-chat/access-inbox", jsonResponse({ reviews: [], notices: [] }));
  f.node("settings-team-tab").click();
  await answerTeam(f);
  assert.equal(f.teamActivity.listeners(), 1);
  f.settings.close();
  assert.equal(f.teamActivity.listeners(), 0, "closing Settings stops it");
  assert.equal(f.overlay.hidden, true);
  assert.equal(f.closes.length, 1);
});

test("the cog and Inbox tab badges count requests to decide plus unread notices", async () => {
  const f = fixture();
  const opened = f.settings.open("inbox", "session-1");
  assert.equal(selectedView(f), "inbox");
  const inbox = await answer(f.requests, "/api/web-chat/access-inbox", jsonResponse({
    reviews: [review(), review({ request_id: "req-2" }), review({ request_id: "req-3", can_decide: false, choices: [] })],
    notices: [{ notice_id: "n-1", title: "Notice", read: false }], unread_notices: 1, notices_total: 1,
  }));
  await opened;
  assert.equal(inbox.method, "GET");
  const badge = f.node("settings-inbox-count");
  const cogBadge = f.node("settings-button-count");
  assert.equal(badge.hidden, false);
  assert.equal(badge.textContent, "3");
  assert.equal(cogBadge.textContent, "3");
  assert.equal(cogBadge.getAttribute("title"), "Inbox: 2 requests to decide, 1 unread notice");
  assert.equal(f.returnFocus.getAttribute("aria-label"), "Open Settings (Inbox: 2 requests to decide, 1 unread notice)");
  assert.equal(f.node("settings-inbox-tab").getAttribute("aria-label"), "Inbox (2 requests to decide, 1 unread notice)");

  f.node("settings-inbox-refresh").click();
  await answer(f.requests, "/api/web-chat/access-inbox", jsonResponse({ reviews: [], notices: [] }));
  assert.equal(badge.hidden, true);
  assert.equal(cogBadge.hidden, true);
  assert.equal(f.returnFocus.getAttribute("aria-label"), "Open Settings");
  assert.equal(f.node("settings-inbox-tab").getAttribute("aria-label"), "Inbox");
});

test("signed in, the cog badge is read without opening Settings and cleared at sign-out", async () => {
  const f = fixture();
  f.settings.startInboxCount();
  await answer(f.requests, "/api/web-chat/access-inbox/counts", jsonResponse({ decidable_requests: 0, unread_notices: 5 }));
  const cogBadge = f.node("settings-button-count");
  assert.equal(cogBadge.textContent, "5");
  assert.equal(f.overlay.hidden, true, "Settings stays closed");
  assert.ok([...f.intervals.values()].some(timer => timer.delay === 30000), "The count is read again every 30 seconds");

  f.settings.noteAgentMessage();
  const soon = f.timeouts.find(timer => timer.delay === 1500);
  soon.callback();
  await answer(f.requests, "/api/web-chat/access-inbox/counts", jsonResponse({ decidable_requests: 1, unread_notices: 5 }));
  assert.equal(cogBadge.textContent, "6");

  f.settings.clear();
  assert.equal(cogBadge.hidden, true);
  assert.ok(![...f.intervals.values()].some(timer => timer.delay === 30000), "Sign-out stops the reads");
});

test("the Knowledge link counts waiting proposals and the cog adds them to the Inbox count", async () => {
  const f = fixture();
  let answerWith = 2;
  const reads = [];
  f.settings.startInboxCount();
  await answer(f.requests, "/api/web-chat/access-inbox/counts", jsonResponse({ decidable_requests: 1, unread_notices: 0 }));
  f.settings.startKnowledgeCount(() => { reads.push(answerWith); return Promise.resolve(answerWith); });
  await settle();
  const link = f.node("settings-knowledge-link");
  const badge = f.node("settings-knowledge-count");
  const cogBadge = f.node("settings-button-count");
  assert.equal(badge.textContent, "2");
  assert.equal(badge.getAttribute("title"), "Knowledge: 2 proposals waiting for you");
  assert.equal(link.getAttribute("aria-label"), "Knowledge (2 proposals waiting for you)");
  assert.equal(f.node("settings-inbox-count").textContent, "1", "The Inbox tab counts only the Inbox");
  assert.equal(cogBadge.textContent, "3");
  assert.equal(f.returnFocus.getAttribute("aria-label"),
    "Open Settings (Inbox: 1 request to decide; Knowledge: 2 proposals waiting for you)");

  // No conversation open yet: the read has nothing to ask with and keeps the number.
  answerWith = null;
  const polls = [...f.intervals.values()].filter(timer => timer.delay === 30000);
  assert.equal(polls.length, 2, "Both counts are read again every 30 seconds");
  polls.forEach(timer => timer.callback());
  await settle();
  assert.equal(badge.textContent, "2");

  answerWith = 0;
  polls.forEach(timer => timer.callback());
  await answer(f.requests, "/api/web-chat/access-inbox/counts", jsonResponse({ decidable_requests: 1, unread_notices: 0 }));
  await settle();
  assert.equal(badge.hidden, true);
  assert.equal(link.getAttribute("aria-label"), "Knowledge");
  assert.equal(cogBadge.textContent, "1");

  answerWith = 5;
  f.settings.clear();
  assert.equal(cogBadge.hidden, true);
  assert.ok(![...f.intervals.values()].some(timer => timer.delay === 30000), "Sign-out stops both reads");
  const before = reads.length;
  f.settings.startKnowledgeCount(() => { reads.push("next"); return Promise.resolve(1); });
  await settle();
  assert.equal(reads.length, before + 1, "The next sign-in reads with its own read");
  assert.equal(reads.at(-1), "next");
  assert.equal(badge.textContent, "1");
});

test("an Inbox link highlights its request after the Inbox loads", async () => {
  const f = fixture();
  const opened = f.settings.open("inbox", "", { accessRequestId: "req-2" });
  await answer(f.requests, "/api/web-chat/access-inbox", jsonResponse({ reviews: [review(), review({ request_id: "req-2" })], notices: [] }));
  await opened;
  const highlighted = byClass(f.node("settings-access-inbox"), "is-highlighted");
  assert.equal(highlighted.length, 1);
  assert.equal(highlighted[0].getAttribute("data-request-id"), "req-2");
});

test("Access links open the Access page at their agent type", async () => {
  const f = fixture();
  await f.settings.openAccess("WebResearchAgent");
  assert.equal(selectedView(f), "access");
  assert.deepEqual(f.maps.at(-1), ["open", "WebResearchAgent"]);
});

test("Tasks and Memories need a conversation; opening without one changes nothing", async () => {
  const f = fixture();
  await assert.rejects(f.settings.open("memories", ""), new RegExp(NEEDS_CONVERSATION_MESSAGE));
  await assert.rejects(f.settings.open("tasks", ""), new RegExp(NEEDS_CONVERSATION_MESSAGE));
  assert.equal(f.overlay.hidden, true);
  assert.equal(f.requests.length, 0);

  const opened = f.settings.open("inbox", "");
  await answer(f.requests, "/api/web-chat/access-inbox", jsonResponse({ reviews: [], notices: [] }));
  await opened;
  f.node("settings-memories-tab").click();
  await settle();
  assert.equal(selectedView(f), "memories");
  assert.equal(f.node("settings-memories-status").textContent, "Open a conversation before managing memories.");
  assert.equal(f.requests.filter(request => request.path.startsWith("/api/web-chat/memories")).length, 0);
});

test("the cog reopens the last view; sign-out starts again at Inbox", async () => {
  const f = fixture();
  const first = f.settings.open(null, "session-1");
  assert.equal(selectedView(f), "inbox", "the first view is Inbox");
  await answer(f.requests, "/api/web-chat/access-inbox", jsonResponse({ reviews: [], notices: [] }));
  await first;
  f.node("settings-memories-tab").click();
  await answer(f.requests, "/api/web-chat/memories?", jsonResponse({ memories: [], total: 0 }));
  f.settings.close();

  const reopened = f.settings.open(null, "session-1");
  assert.equal(selectedView(f), "memories");
  await answer(f.requests, "/api/web-chat/memories?", jsonResponse({ memories: [], total: 0 }));
  await reopened;

  f.settings.clear();
  assert.equal(f.overlay.hidden, true);
  assert.equal(f.node("settings-accounts-content").children.length, 0, "sign-out forgets the Accounts tab");
  assert.ok(f.tasks.some(call => call[0] === "clear"));
  assert.ok(f.maps.some(call => call[0] === "clear"));
  const afterSignOut = f.settings.open(null, "session-2");
  assert.equal(selectedView(f), "inbox");
  await answer(f.requests, "/api/web-chat/access-inbox", jsonResponse({ reviews: [], notices: [] }));
  await afterSignOut;
});

test("Escape and the backdrop close Settings and return focus to the cog", async () => {
  const f = fixture();
  const opened = f.settings.open("inbox", "session-1");
  await answer(f.requests, "/api/web-chat/access-inbox", jsonResponse({ reviews: [], notices: [] }));
  await opened;
  assert.equal(f.document.activeElement, f.node("settings-dialog"), "the dialog takes focus");
  // Focus inside the dialog: chat.html nests it in the overlay.
  f.overlay.append(f.node("settings-dialog"));
  f.document.dispatchEvent(Object.assign(new Event("keydown"), { key: "Escape" }));
  assert.equal(f.overlay.hidden, true);
  assert.equal(f.document.activeElement, f.returnFocus);

  const again = f.settings.open("inbox", "session-1");
  await answer(f.requests, "/api/web-chat/access-inbox", jsonResponse({ reviews: [], notices: [] }));
  await again;
  const click = new Event("click");
  f.overlay.dispatchEvent(click);
  assert.equal(f.overlay.hidden, true);
});

test("sign-out forgets the CSRF token, so later writes are refused locally", async () => {
  const f = fixture();
  f.settings.clear();
  const opened = f.settings.open("inbox", "session-1");
  await answer(f.requests, "/api/web-chat/access-inbox", jsonResponse({ reviews: [review()], notices: [] }));
  await opened;
  const approve = byClass(f.node("settings-access-inbox"), "permission-choice-button")[0];
  approve.click();
  await settle();
  assert.equal(f.requests.filter(request => request.method === "POST").length, 0);
  assert.match(f.node("settings-access-inbox").textContent, /sign-in session expired/);
});

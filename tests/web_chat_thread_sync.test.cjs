// Live thread-list state from event pages: busy dots and renames reach every open page.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname,
  "../assistant_gateway/onboarding_web/assets/chat.js"), "utf8");

// The page's name for an unnamed thread, read from chat.js itself.
const NEW_CHAT_TITLE = source.match(/const NEW_CHAT_TITLE = "([^"]+)";/)[1];

function loadFunctions(sandbox, names) {
  if (!("NEW_CHAT_TITLE" in sandbox)) sandbox.NEW_CHAT_TITLE = NEW_CHAT_TITLE;
  vm.createContext(sandbox);
  for (const name of names) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `chat.js defines ${name}`);
    const end = source.indexOf("\nfunction ", start + 1);
    vm.runInContext(source.slice(start, end < 0 ? undefined : end), sandbox);
  }
}

function harness() {
  const server = {
    revision: "rev-1",
    sessions: [
      { session_key: "alpha", session_alias: "Repair Memory", updated_at: "2026-09-12T00:00:02Z" },
      { session_key: "beta", session_alias: "Control-Center", updated_at: "2026-09-12T00:00:01Z" },
    ],
  };
  const calls = [];
  let renders = 0;
  const sandbox = {
    AbortSignal,
    Set,
    sharedState: { authSessionActive: true, authSessionGeneration: 1, activeThreadId: "alpha",
      editingThreadId: "", threads: [], asyncEventCursor: "", threadsRevision: "" },
    shellState: { SYSTEM_ERROR_SOURCE: { FETCH_SESSIONS: "sessions" } },
    authentication: { handleUnauthorizedResponse() {} },
    normalizeThreadTitle: value => String(value || ""),
    threadFromSeed: seed => ({ ...seed, messages: [], pendingReply: false, pendingReplyCount: 0 }),
    saveThreads() {},
    renderThreads() { renders += 1; },
    renderMessages() {},
    clearSystemError() { return false; },
    appendSystemError() {},
    ensureActiveThreadHistory: () => Promise.resolve(),
    requestClient: { async fetch(url) {
      calls.push(url);
      return { ok: true, json: async () => ({
        sessions: server.sessions, event_cursor: "snapshot", threads_revision: server.revision,
      }) };
    } },
  };
  loadFunctions(sandbox, ["fetchSessions", "threadIsBusy", "applyThreadStates"]);
  const thread = key => sandbox.sharedState.threads.find(item => item.sessionKey === key);
  const page = (busy = [], revision = server.revision) => ({ threads_revision: revision, busy_session_keys: busy });
  const settle = () => sandbox.fetchSessions();
  return { sandbox, server, calls, thread, page, settle, renders: () => renders };
}

test("a task running for another page shows the busy dot here too", async () => {
  const { sandbox, thread, page, renders } = harness();
  await sandbox.fetchSessions();
  const before = renders();
  sandbox.applyThreadStates(page(["beta"]));
  assert.equal(sandbox.threadIsBusy(thread("beta")), true);
  assert.equal(sandbox.threadIsBusy(thread("alpha")), false);
  assert.equal(renders(), before + 1);
  // An unchanged heartbeat does not redraw the list.
  sandbox.applyThreadStates(page(["beta"]));
  assert.equal(renders(), before + 1);
  sandbox.applyThreadStates(page([]));
  assert.equal(sandbox.threadIsBusy(thread("beta")), false);
  assert.equal(renders(), before + 2);
});

test("the server clearing its busy state keeps this page's own reply wait", async () => {
  const { sandbox, thread, page } = harness();
  await sandbox.fetchSessions();
  thread("alpha").pendingReply = true;
  sandbox.applyThreadStates(page(["alpha"]));
  sandbox.applyThreadStates(page([]));
  assert.equal(sandbox.threadIsBusy(thread("alpha")), true);
});

test("a rename on another device reloads the thread list once", async () => {
  const { sandbox, server, calls, thread, page, settle } = harness();
  await sandbox.fetchSessions();
  sandbox.applyThreadStates(page());
  assert.equal(calls.length, 1, "matching revision needs no reload");
  server.sessions = [{ ...server.sessions[0] }, { ...server.sessions[1], session_alias: "Ops" }];
  server.revision = "rev-2";
  sandbox.applyThreadStates(page());
  await settle();
  assert.equal(calls.length, 2);
  assert.equal(thread("beta").title, "Ops");
  assert.equal(sandbox.sharedState.threadsRevision, "rev-2");
  sandbox.applyThreadStates(page());
  assert.equal(calls.length, 2);
});

test("an open rename field is never redrawn; the list reloads after it closes", async () => {
  const { sandbox, server, calls, thread, page, renders } = harness();
  await sandbox.fetchSessions();
  sandbox.sharedState.editingThreadId = "alpha";
  server.revision = "rev-2";
  const before = renders();
  sandbox.applyThreadStates(page(["beta"]));
  assert.equal(renders(), before);
  assert.equal(calls.length, 1);
  assert.equal(sandbox.threadIsBusy(thread("beta")), true, "state is kept for the next draw");
  sandbox.sharedState.editingThreadId = "";
  sandbox.applyThreadStates(page(["beta"]));
  assert.equal(calls.length, 2);
});

test("pages without thread state and signed-out pages change nothing", async () => {
  const { sandbox, calls, thread } = harness();
  await sandbox.fetchSessions();
  sandbox.applyThreadStates({ messages: [] });
  sandbox.sharedState.authSessionActive = false;
  sandbox.applyThreadStates({ threads_revision: "other", busy_session_keys: ["alpha"] });
  assert.equal(calls.length, 1);
  assert.equal(sandbox.threadIsBusy(thread("alpha")), false);
});

test("a thread keeps the server's fixed name while later messages arrive", async () => {
  const { sandbox, server, calls, thread, page } = harness();
  server.sessions = [{ session_key: "gamma", title: "Plan my week", updated_at: "2026-09-12T00:00:03Z" }];
  await sandbox.fetchSessions();
  assert.equal(thread("gamma").title, "Plan my week");
  // Later messages leave the server's name and revision alone, so nothing reloads or renames.
  const reads = calls.length;
  sandbox.applyThreadStates(page());
  sandbox.applyThreadStates(page());
  assert.equal(calls.length, reads);
  assert.equal(thread("gamma").title, "Plan my week");
  // A thread the server has not named yet shows "New Chat".
  server.sessions = [{ session_key: "delta", title: null, updated_at: "2026-09-12T00:00:04Z" }];
  await sandbox.fetchSessions();
  assert.equal(thread("delta").title, NEW_CHAT_TITLE);
});

// A small element stand-in for drawing the thread list.
function element(tagName) {
  return {
    tagName, className: "", textContent: "", children: [], attributes: {},
    setAttribute(name, value) { this.attributes[name] = String(value); },
    appendChild(child) { this.children.push(child); return child; },
    set innerHTML(value) { if (value === "") this.children = []; },
  };
}

function listHarness() {
  const listeners = [];
  let focused = 0;
  const sandbox = {
    document: { createElement: element },
    lifecycle: { listen(target, type, handler) { listeners.push({ target, type, handler }); } },
    sharedState: { threads: [], threadsRevision: "", activeThreadId: "", editingThreadId: "",
      threadList: element("div"), messageInput: { focus() { focused += 1; } } },
    attention: { refresh() {} },
    hideThreadTitleTooltip() {},
    saveThreads() {},
  };
  loadFunctions(sandbox, ["renderThreads", "unsavedThreadRow"]);
  const click = target => listeners.filter(item => item.target === target && item.type === "click")
    .forEach(item => item.handler());
  return { sandbox, rows: () => sandbox.sharedState.threadList.children, click, focused: () => focused };
}

test("with no thread yet the list shows one unsaved New Chat row, once the server has answered", () => {
  const { sandbox, rows, click, focused } = listHarness();
  sandbox.renderThreads();
  assert.equal(rows().length, 0, "nothing before the server answers, so no row flashes over real threads");
  sandbox.sharedState.threadsRevision = "rev-empty";
  sandbox.renderThreads();
  assert.equal(rows().length, 1);
  const [row] = rows();
  assert.equal(row.className, "thread-row is-active is-unsaved");
  assert.equal(row.children.length, 1, "no rename or archive: there is nothing to keep yet");
  assert.equal(row.children[0].textContent, NEW_CHAT_TITLE);
  click(row.children[0]);
  assert.equal(focused(), 1, "choosing it puts the cursor in the composer");
  // Drawing again replaces it rather than adding a second one.
  sandbox.renderThreads();
  assert.equal(rows().length, 1);
});

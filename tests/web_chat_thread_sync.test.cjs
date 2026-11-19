// Live thread-list state from event pages: busy dots and renames reach every open page.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname,
  "../assistant_gateway/onboarding_web/assets/chat.js"), "utf8");

// The thread list module chat.js imports (sections, snippets, default thread).
const threadList = require("../assistant_gateway/onboarding_web/assets/chat_thread_list.mjs");

// The page's name for an unnamed thread, read from chat.js itself.
const NEW_CHAT_TITLE = source.match(/const NEW_CHAT_TITLE = "([^"]+)";/)[1];

function loadFunctions(sandbox, names) {
  if (!("NEW_CHAT_TITLE" in sandbox)) sandbox.NEW_CHAT_TITLE = NEW_CHAT_TITLE;
  for (const [name, value] of Object.entries(threadList)) if (!(name in sandbox)) sandbox[name] = value;
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
  const patches = [];
  const errors = [];
  let renders = 0, switchRenders = 0;
  const sandbox = {
    AbortSignal,
    Set,
    sharedState: { authSessionActive: true, authSessionGeneration: 1, activeThreadId: "alpha",
      editingThreadId: "", threads: [], asyncEventCursor: "", threadsRevision: "" },
    shellState: { SYSTEM_ERROR_SOURCE: { FETCH_SESSIONS: "sessions", THREAD_PLACEMENT: "thread_placement" } },
    authentication: { handleUnauthorizedResponse() {} },
    // The person is reading the selected thread.
    isReadingThread: item => item.id === sandbox.sharedState.activeThreadId,
    normalizeThreadTitle: value => String(value || ""),
    threadFromSeed: seed => ({ ...seed, messages: [], pendingReply: false, pendingReplyCount: 0 }),
    saveThreads() {},
    renderThreads() { renders += 1; },
    renderMessages() {},
    renderParallelSwitch() { switchRenders += 1; },
    clearSystemError() { return false; },
    appendSystemError(target, source, message) { errors.push({ target, source, message }); },
    ensureActiveThreadHistory: () => Promise.resolve(),
    requestClient: { async fetch(url, init = {}) {
      if (init.method === "PATCH") {
        patches.push({ url, body: JSON.parse(init.body) });
        const refusal = server.refusal;
        return { ok: !refusal, status: refusal ? 400 : 200, text: async () => refusal || "{}" };
      }
      calls.push(url);
      return { ok: true, json: async () => ({
        sessions: server.sessions, event_cursor: "snapshot", threads_revision: server.revision,
      }) };
    } },
  };
  loadFunctions(sandbox, [
    "fetchSessions", "threadIsBusy", "applyThreadStates", "applyServerThreadRow", "applyEventToThreadRow",
    "restoreWokeThreadWhenRead", "changeThreadPlacement", "placementRefusal",
  ]);
  const thread = key => sandbox.sharedState.threads.find(item => item.sessionKey === key);
  const page = (busy = [], revision = server.revision, open = []) => ({
    threads_revision: revision, busy_session_keys: busy, open_request_session_keys: open });
  const settle = () => sandbox.fetchSessions();
  return { sandbox, server, calls, patches, errors, thread, page, settle,
    renders: () => renders, switchRenders: () => switchRenders };
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

test("each thread shows its saved Run concurrently setting and is locked while a request is open", async () => {
  const { sandbox, server, thread, page, settle, switchRenders } = harness();
  server.sessions = [{ ...server.sessions[0], parallel_thread: true }, server.sessions[1]];
  await sandbox.fetchSessions();
  assert.equal(thread("alpha").parallelThread, true);
  assert.equal(thread("beta").parallelThread, false);
  const before = switchRenders();
  sandbox.applyThreadStates(page([], server.revision, ["beta"]));
  assert.equal(thread("beta").serverRequestOpen, true);
  assert.equal(thread("alpha").serverRequestOpen, false);
  assert.equal(switchRenders(), before + 1, "the switch is redrawn when its lock changes");
  sandbox.applyThreadStates(page([], server.revision, ["beta"]));
  assert.equal(switchRenders(), before + 1);
  sandbox.applyThreadStates(page([], server.revision, []));
  assert.equal(thread("beta").serverRequestOpen, false);
  // A save in flight keeps its position when the list reloads meanwhile.
  thread("beta").parallelSaving = true;
  thread("beta").parallelThread = true;
  await settle();
  assert.equal(thread("beta").parallelThread, true);
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
  loadFunctions(sandbox, ["renderThreads", "unsavedThreadRow", "focusedThreadRowKey", "restoreWokeThreadWhenRead"]);
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


// Pin, snooze and dismiss on the page (A5, plan review C02, C09, C10).

const SNOOZED = { session_key: "gamma", title: "Renew the certificate", updated_at: "2026-11-19T10:00:00Z",
  list_state: "snoozed", snoozed_until: "2026-11-19T12:00:00+00:00", last_sequence: 4,
  last_activity_at: "2026-11-19T10:00:00Z", last_message: { role: "assistant", text: "**Staging** expires soon" } };

test("the server's row fields reach the thread, with the plain snippet", async () => {
  const { sandbox, server, thread } = harness();
  server.sessions = [SNOOZED, { session_key: "delta", updated_at: "2026-11-19T09:00:00Z", pinned: true, needs_you: "question" }];
  await sandbox.fetchSessions();
  const gamma = thread("gamma");
  assert.equal(gamma.listState, "snoozed");
  assert.equal(gamma.snoozedUntil, SNOOZED.snoozed_until);
  assert.equal(gamma.lastSequence, 4);
  assert.deepEqual({ ...gamma.lastMessage }, { role: "assistant", text: "Staging expires soon" });
  assert.equal(gamma.lastActivityAt, Date.parse(SNOOZED.last_activity_at));
  assert.equal(thread("delta").pinned, true);
  assert.equal(thread("delta").needsYou, "question");
});

test("with no selection the page opens the first visible thread, never a hidden one (C02)", async () => {
  const { sandbox, server } = harness();
  sandbox.sharedState.activeThreadId = "";
  server.sessions = [
    { session_key: "hidden", updated_at: "2026-11-19T12:00:00Z", list_state: "dismissed" },
    { session_key: "older", updated_at: "2026-11-19T08:00:00Z" },
  ];
  await sandbox.fetchSessions();
  assert.equal(sandbox.sharedState.activeThreadId, "older");
});

test("a snooze that wakes moves the thread to Recent marked Woke, and reading it restores it once (A5)", async () => {
  const { sandbox, server, calls, patches, thread, page } = harness();
  server.sessions = [SNOOZED, { session_key: "alpha", updated_at: "2026-11-19T09:00:00Z" }];
  await sandbox.fetchSessions();
  assert.equal(threadList.threadSection(thread("gamma")), threadList.THREAD_SECTION.SNOOZED);

  // The wake time passes: the next live page carries a new revision and the list reloads.
  server.sessions = [{ ...SNOOZED, list_state: "woke", snoozed_until: null }, server.sessions[1]];
  server.revision = "rev-woke";
  sandbox.applyThreadStates(page());
  await new Promise(resolve => setImmediate(resolve));
  await sandbox.fetchSessions();
  assert.equal(calls.length >= 2, true);
  assert.equal(thread("gamma").listState, "woke");
  assert.equal(threadList.threadSection(thread("gamma")), threadList.THREAD_SECTION.RECENT);

  // Not read yet: nothing is sent.
  sandbox.restoreWokeThreadWhenRead(thread("gamma"));
  assert.equal(patches.length, 0);
  // The person opens it: one restore, however often the list is drawn meanwhile.
  sandbox.sharedState.activeThreadId = "gamma";
  sandbox.restoreWokeThreadWhenRead(thread("gamma"));
  sandbox.restoreWokeThreadWhenRead(thread("gamma"));
  assert.equal(patches.length, 1);
  assert.deepEqual(patches[0].body, { thread_action: "restore" });
  assert.match(patches[0].url, /\/api\/web-chat\/sessions\/gamma$/);
});

test("snooze and dismiss send the last message this page showed", async () => {
  const { sandbox, server, patches, thread } = harness();
  server.sessions = [{ ...SNOOZED, list_state: null }];
  await sandbox.fetchSessions();
  // A live reply arrives after the list was read.
  sandbox.applyEventToThreadRow(thread("gamma"), { session_sequence: 5, role: "assistant", content: "Renewed.", created_at_utc: "2026-11-19T10:05:00Z" });
  // A card later in the thread moves the time but not the line.
  sandbox.applyEventToThreadRow(thread("gamma"), { session_sequence: 6, role: "system", content: "Status: idle", created_at_utc: "2026-11-19T10:06:00Z" });
  // An older event never moves it back.
  sandbox.applyEventToThreadRow(thread("gamma"), { session_sequence: 3, role: "user", content: "old", created_at_utc: "2026-11-19T09:00:00Z" });
  assert.deepEqual({ ...thread("gamma").lastMessage }, { role: "assistant", text: "Renewed." });
  assert.equal(thread("gamma").lastSequence, 6);
  assert.equal(await sandbox.changeThreadPlacement(thread("gamma"), "dismiss"), true);
  assert.deepEqual(patches[0].body, { thread_action: "dismiss", seen_sequence: 6 });
  await sandbox.changeThreadPlacement(thread("gamma"), "snooze", { snooze_until: "2026-11-20T09:00:00+01:00" });
  assert.deepEqual(patches[1].body, { thread_action: "snooze", snooze_until: "2026-11-20T09:00:00+01:00", seen_sequence: 6 });
  await sandbox.changeThreadPlacement(thread("gamma"), "pin");
  assert.deepEqual(patches[2].body, { thread_action: "pin" });
});

test("a refused change keeps the row where it was and tells the person why", async () => {
  const { sandbox, server, calls, errors, thread } = harness();
  server.sessions = [{ ...SNOOZED, list_state: null }];
  await sandbox.fetchSessions();
  const reads = calls.length;
  server.refusal = "Choose a time in the future to snooze until.";
  assert.equal(await sandbox.changeThreadPlacement(thread("gamma"), "snooze", { snooze_until: "2020-01-01T00:00:00Z" }), false);
  assert.equal(thread("gamma").listState, "");
  assert.equal(calls.length, reads, "the list is not reloaded");
  assert.deepEqual(errors.map(item => [item.target.id, item.source, item.message]), [
    ["gamma", "thread_placement", "This chat was not moved. Choose a time in the future to snooze until."],
  ]);
});

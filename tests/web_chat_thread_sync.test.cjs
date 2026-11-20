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
const { THREAD_MENU_ACTION } = require("../assistant_gateway/onboarding_web/assets/chat_thread_menu.mjs");
// The status line's wording (chat.js imports it from chat_thread_status.mjs).
const threadStatus = require("../assistant_gateway/onboarding_web/assets/chat_thread_status.mjs");

// The page's name for an unnamed thread, read from chat.js itself.
const NEW_CHAT_TITLE = source.match(/const NEW_CHAT_TITLE = "([^"]+)";/)[1];

function loadFunctions(sandbox, names) {
  if (!("NEW_CHAT_TITLE" in sandbox)) sandbox.NEW_CHAT_TITLE = NEW_CHAT_TITLE;
  if (!("THREAD_MENU_ACTION" in sandbox)) sandbox.THREAD_MENU_ACTION = THREAD_MENU_ACTION;
  for (const [name, value] of Object.entries({ ...threadList, ...threadStatus })) if (!(name in sandbox)) sandbox[name] = value;
  vm.createContext(sandbox);
  for (const name of names) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `chat.js defines ${name}`);
    const end = source.indexOf("\nfunction ", start + 1);
    vm.runInContext(source.slice(start, end < 0 ? undefined : end), sandbox);
  }
}

// Where a failed thread change is shown: the status line beside the list (review F5).
const STATUS_LINE = "thread_list_status";

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
  const announced = [];
  let renders = 0, switchRenders = 0;
  const sandbox = {
    AbortSignal,
    Set,
    sharedState: { authSessionActive: true, authSessionGeneration: 1, activeThreadId: "alpha",
      editingThreadId: "", threads: [], asyncEventCursor: "", threadsRevision: "" },
    shellState: {
      SYSTEM_ERROR_SOURCE: { FETCH_SESSIONS: "sessions" },
      // The thread list's status line: failures join `errors`, confirmations `announced`.
      threadListStatus: {
        fail(message, { retry = null } = {}) { errors.push({ target: "status line", source: STATUS_LINE, message, retry }); },
        announce(message) { announced.push(message); },
        clear() {},
      },
    },
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
    appendSystemError(target, source, message, retry) { errors.push({ target, source, message, retry }); },
    ensureActiveThreadHistory: () => Promise.resolve(),
    requestClient: { async fetch(url, init = {}) {
      if (init.method === "PATCH") {
        patches.push({ url, body: JSON.parse(init.body) });
        if (server.offline) throw new TypeError("Failed to fetch");
        const refusal = server.refusal;
        return { ok: !refusal, status: refusal ? 400 : 200, text: async () => refusal || "{}" };
      }
      calls.push(url);
      // A slow list read waits for the test to open its gate; a failed one answers 503.
      if (server.gate) await server.gate;
      if (server.failReads) return { ok: false, status: 503, json: async () => ({}) };
      return { ok: true, json: async () => ({
        sessions: server.sessions, event_cursor: "snapshot", threads_revision: server.revision,
      }) };
    } },
  };
  loadFunctions(sandbox, [
    "fetchSessions", "threadIsBusy", "applyThreadStates", "applyServerThreadRow", "applyEventToThreadRow",
    "restoreWokeThreadWhenRead", "changeThreadPlacement", "runThreadPlacement", "sendThreadPlacement",
    "threadChangeRequest", "refusalReason", "threadChangeFailure", "threadForSession", "quotedThreadTitle",
  ]);
  sandbox.fullThreadTitle = item => item.title;
  const thread = key => sandbox.sharedState.threads.find(item => item.sessionKey === key);
  const page = (busy = [], revision = server.revision, open = []) => ({
    threads_revision: revision, busy_session_keys: busy, open_request_session_keys: open });
  const settle = () => sandbox.fetchSessions();
  return { sandbox, server, calls, patches, errors, announced, thread, page, settle,
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
    shellState: { threadPreview: { refresh() {} } },
  };
  loadFunctions(sandbox, ["renderThreads", "unsavedThreadRow", "focusedThreadListTarget", "restoreWokeThreadWhenRead"]);
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
  assert.deepEqual(patches[0].body, { thread_action: "clear_woke" });
  assert.match(patches[0].url, /\/api\/web-chat\/sessions\/gamma$/);
});

// A gate the test opens to let a slow list read answer.
function gate() {
  let open;
  const promise = new Promise(resolve => { open = resolve; });
  return { promise, open };
}
const flush = async () => { for (let i = 0; i < 6; i += 1) await new Promise(resolve => setImmediate(resolve)); };
// A thread as the page draws it from the browser's saved list after a reload: no server row yet.
const cachedThread = fields => ({ id: "gamma", sessionKey: "gamma", title: "Renew the certificate", persisted: true,
  lastSequence: 0, listState: "", hasServerRow: false, messages: [], ...fields });

test("a Woke mark from the saved list is cleared only after the server's list shows it (F1)", async () => {
  const { sandbox, server, patches, thread } = harness();
  sandbox.sharedState.activeThreadId = "gamma";
  sandbox.sharedState.threads = [cachedThread({ listState: "woke" })];
  sandbox.restoreWokeThreadWhenRead(thread("gamma"));
  assert.equal(patches.length, 0, "nothing is sent from the saved list");
  server.sessions = [{ ...SNOOZED, list_state: "woke", snoozed_until: null }];
  await sandbox.fetchSessions();
  sandbox.restoreWokeThreadWhenRead(thread("gamma"));
  assert.deepEqual(patches.map(item => item.body), [{ thread_action: "clear_woke" }]);
});

test("a clear the gateway answered but left woke is asked again later, not never (C05)", async () => {
  const { sandbox, server, patches, thread } = harness();
  sandbox.sharedState.activeThreadId = "gamma";
  server.sessions = [{ ...SNOOZED, list_state: "woke", snoozed_until: null }];
  await sandbox.fetchSessions();
  sandbox.restoreWokeThreadWhenRead(thread("gamma"));
  await flush();
  assert.equal(patches.length, 1);
  assert.equal(thread("gamma").listState, "woke", "the server's list still says woke");
  assert.equal(thread("gamma").restoreRequested, false);
  assert.ok(thread("gamma").restoreRetryAt > Date.now(), "a retry is due later");
  sandbox.restoreWokeThreadWhenRead(thread("gamma"));
  assert.equal(patches.length, 1, "not on the next draw");
});

test("Dismiss on a thread from the saved list waits for the server's list and sends its real number (F2)", async () => {
  const { sandbox, server, patches, thread } = harness();
  sandbox.sharedState.activeThreadId = "alpha";
  sandbox.sharedState.threads = [cachedThread()];
  server.sessions = [{ ...SNOOZED, list_state: null }];
  const slow = gate();
  server.gate = slow.promise;
  const dismissed = sandbox.changeThreadPlacement(thread("gamma"), "dismiss");
  await flush();
  assert.equal(patches.length, 0, "no PATCH with the saved list's number");
  server.gate = null;
  slow.open();
  assert.equal(await dismissed, true);
  assert.deepEqual(patches.map(item => item.body), [{ thread_action: "dismiss", seen_sequence: 4 }]);
});

test("a later choice is sent after a Dismiss that waits for the list; a thread gone by then is left alone (C06)", async () => {
  const { sandbox, server, patches, errors, thread } = harness();
  sandbox.sharedState.threads = [cachedThread()];
  server.sessions = [{ ...SNOOZED, list_state: null }];
  const slow = gate();
  server.gate = slow.promise;
  const first = sandbox.changeThreadPlacement(thread("gamma"), "dismiss");
  const second = sandbox.changeThreadPlacement(thread("gamma"), "pin");
  await flush();
  assert.equal(patches.length, 0, "the pin waits behind the dismiss");
  server.gate = null;
  slow.open();
  await Promise.all([first, second]);
  assert.deepEqual(patches.map(item => item.body.thread_action), ["dismiss", "pin"]);

  // Deleted on another device while the list was read: no PATCH, nothing shown.
  const other = harness();
  other.sandbox.sharedState.threads = [cachedThread()];
  other.server.sessions = [{ session_key: "alpha", updated_at: "2026-11-19T09:00:00Z" }];
  assert.equal(await other.sandbox.changeThreadPlacement(other.thread("gamma"), "dismiss"), false);
  assert.equal(other.patches.length, 0);
  assert.equal(other.errors.length, 0);
  assert.equal(errors.length, 0);
});

test("a change still queued when the sign-in ends is dropped, even for the same person signing in again (C13)", async () => {
  const { sandbox, server, patches, errors, thread } = harness();
  sandbox.sharedState.threads = [cachedThread()];
  server.sessions = [{ ...SNOOZED, list_state: null }];
  const slow = gate();
  server.gate = slow.promise;
  const first = sandbox.changeThreadPlacement(thread("gamma"), "dismiss");
  const second = sandbox.changeThreadPlacement(thread("gamma"), "pin");
  await flush();
  // The session ends and the same person signs in again before the slow read answers.
  sandbox.sharedState.authSessionGeneration += 1;
  sandbox.sharedState.threads = [cachedThread()];
  server.gate = null;
  slow.open();
  assert.deepEqual(await Promise.all([first, second]), [false, false]);
  assert.equal(patches.length, 0);
  assert.equal(errors.length, 0);
});

test("Delete reports in the status line: the gateway's reason, Retry only when it did not get through, and a confirmation (F5, F13, C14)", async () => {
  const { sandbox, server, errors, announced, thread } = harness();
  server.sessions = [{ ...SNOOZED, list_state: null }];
  await sandbox.fetchSessions();
  loadFunctions(sandbox, ["archiveThread"]);
  const removed = [];
  sandbox.removeArchivedThread = threadId => removed.push(threadId);
  // As the archive route answers: plain text for a refusal, an empty body
  // with no status text behind an HTTP/2 proxy, JSON on success.
  const answers = [
    { ok: false, status: 503, statusText: "", text: async () => "" },
    { ok: false, status: 404, statusText: "Not Found", text: async () => "Thread not found." },
    { ok: false, status: 404, statusText: "", text: async () => "" },
    { ok: true, status: 200, text: async () => JSON.stringify({ status: "ok" }) },
  ];
  sandbox.requestClient.fetch = async () => answers.shift();
  sandbox.archiveThread(thread("gamma").id);
  await flush();
  assert.equal(errors[0].message, "\u201cRenew the certificate\u201d was not deleted. Try again.");
  assert.equal(typeof errors[0].retry, "function");
  errors[0].retry();
  await flush();
  assert.deepEqual([errors[1].message, errors[1].retry], ["\u201cRenew the certificate\u201d was not deleted. Thread not found.", null]);
  sandbox.archiveThread(thread("gamma").id);
  await flush();
  assert.deepEqual([errors[2].message, errors[2].retry], ["\u201cRenew the certificate\u201d was not deleted.", null],
    "a refusal without a reason does not say Try again");
  sandbox.archiveThread(thread("gamma").id);
  await flush();
  assert.deepEqual(removed, ["gamma"]);
  assert.deepEqual(announced, ["Deleted \u201cRenew the certificate\u201d."]);
});

test("Dismiss on a thread from the saved list fails with Retry when the list cannot be read (F2)", async () => {
  const { sandbox, server, patches, errors, thread } = harness();
  sandbox.sharedState.activeThreadId = "alpha";
  sandbox.sharedState.threads = [cachedThread()];
  server.failReads = true;
  assert.equal(await sandbox.changeThreadPlacement(thread("gamma"), "dismiss"), false);
  assert.equal(patches.length, 0);
  const notice = errors.find(item => item.source === STATUS_LINE);
  assert.equal(notice.message, "\u201cRenew the certificate\u201d was not moved. Try again.");
  assert.equal(typeof notice.retry, "function");
  server.failReads = false;
  server.sessions = [{ ...SNOOZED, list_state: null }];
  await notice.retry();
  assert.deepEqual(patches.map(item => item.body), [{ thread_action: "dismiss", seen_sequence: 4 }]);
});

test("a change answered after sign-out shows nothing and offers no Retry (F3)", async () => {
  const { sandbox, server, errors, thread } = harness();
  server.sessions = [{ ...SNOOZED, list_state: null }];
  await sandbox.fetchSessions();
  const gamma = thread("gamma");
  const original = sandbox.requestClient.fetch;
  sandbox.requestClient.fetch = async (url, init = {}) => {
    if (init.method === "PATCH") {
      // The session ends while Pin is in flight; another person signs in on this tab.
      sandbox.sharedState.authSessionGeneration += 1;
      return { ok: false, status: 401, text: async () => "Sign in again." };
    }
    return original(url, init);
  };
  assert.equal(await sandbox.changeThreadPlacement(gamma, "pin"), false);
  assert.equal(errors.length, 0);
});

test("a change saved while a list read is in flight is followed by a fresh read (F16)", async () => {
  const { sandbox, server, calls, thread } = harness();
  server.sessions = [{ ...SNOOZED, list_state: null }];
  await sandbox.fetchSessions();
  const slow = gate();
  server.gate = slow.promise;
  const heartbeat = sandbox.fetchSessions();
  await flush();
  const readsBefore = calls.length;
  // The pin is saved; the read in flight left before it and still says unpinned.
  const pinned = sandbox.changeThreadPlacement(thread("gamma"), "pin");
  await flush();
  server.sessions = [{ ...SNOOZED, list_state: null, pinned: true }];
  server.gate = null;
  slow.open();
  await Promise.all([heartbeat, pinned]);
  assert.equal(calls.length, readsBefore + 1, "one more read after the one in flight");
  assert.equal(thread("gamma").pinned, true);
});

test("snooze and dismiss send the last message this page showed", async () => {
  const { sandbox, server, patches, announced, thread } = harness();
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
  assert.deepEqual(announced, ["Dismissed \u201cRenew the certificate\u201d."], "the change is announced (F4, F5)");
  await sandbox.changeThreadPlacement(thread("gamma"), "snooze", { snooze_until: "2026-11-20T09:00:00+01:00" });
  assert.deepEqual(patches[1].body, { thread_action: "snooze", snooze_until: "2026-11-20T09:00:00+01:00", seen_sequence: 6 });
  await sandbox.changeThreadPlacement(thread("gamma"), "pin");
  assert.deepEqual(patches[2].body, { thread_action: "pin" });
});

test("a refused change keeps the row where it was and tells the person why", async () => {
  const { sandbox, server, calls, errors, thread } = harness();
  server.sessions = [{ ...SNOOZED, list_state: null }, { session_key: "alpha", updated_at: "2026-11-19T09:00:00Z" }];
  await sandbox.fetchSessions();
  assert.equal(sandbox.sharedState.activeThreadId, "alpha");
  const reads = calls.length;
  server.refusal = "Choose a time in the future to snooze until.";
  assert.equal(await sandbox.changeThreadPlacement(thread("gamma"), "snooze", { snooze_until: "2020-01-01T00:00:00Z" }), false);
  assert.equal(thread("gamma").listState, "");
  assert.equal(calls.length, reads, "the list is not reloaded");
  // Shown in the status line beside the list, naming the chat that was not moved, with no Retry (F5).
  assert.deepEqual(errors.map(item => [item.source, item.message, item.retry]), [
    [STATUS_LINE, "\u201cRenew the certificate\u201d was not moved. Choose a time in the future to snooze until.", null],
  ]);
  // The open chat is named too: the line sits beside the list, not in the chat.
  sandbox.sharedState.activeThreadId = "gamma";
  await sandbox.changeThreadPlacement(thread("gamma"), "dismiss");
  assert.equal(errors[1].message, "\u201cRenew the certificate\u201d was not moved. Choose a time in the future to snooze until.");
});

test("a failed automatic restore shows nothing and is tried again later, not on every draw (C23)", async () => {
  const { sandbox, server, patches, errors, thread } = harness();
  server.sessions = [{ ...SNOOZED, list_state: "woke", snoozed_until: null }];
  sandbox.sharedState.activeThreadId = "gamma";
  await sandbox.fetchSessions();
  server.refusal = "Thread not found.";
  sandbox.restoreWokeThreadWhenRead(thread("gamma"));
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(patches.length, 1);
  assert.equal(errors.length, 0, "the person did not ask for it, so nothing is shown");
  sandbox.restoreWokeThreadWhenRead(thread("gamma"));
  assert.equal(patches.length, 1, "no retry on the next draw");
  thread("gamma").restoreRetryAt = 0;
  server.refusal = "";
  sandbox.restoreWokeThreadWhenRead(thread("gamma"));
  assert.equal(patches.length, 2, "tried again once the wait is over");
});

// Keyboard focus and a rename draft survive the list being drawn again (C25).

const fakeDom = require("./fake_dom.mjs");

function domHarness() {
  const document = fakeDom.fakeDocument();
  const proto = Object.getPrototypeOf(document.body);
  const matches = (node, selector) => selector === "[hidden]" ? node.hidden : node.classList.contains(selector.slice(1));
  if (!proto.closest) {
    proto.closest = function (selector) {
      for (let node = this; node; node = node.parentNode) if (node.classList && matches(node, selector)) return node;
      return null;
    };
    proto.querySelectorAll = function (selector) {
      return fakeDom.descendants(this).filter(node => matches(node, selector));
    };
    proto.querySelector = function (selector) { return this.querySelectorAll(selector)[0] || null; };
  }
  // The rename buttons' icons are trusted markup from chat.js; this stand-in ignores it.
  Object.defineProperty(proto, "innerHTML", { configurable: true, set() {}, get() { return ""; } });
  const list = document.createElement("div");
  document.body.appendChild(list);
  const timers = [];
  const sandbox = {
    document,
    sharedState: { threadList: list, threads: [] },
    lifecycle: { listen: (target, type, handler) => target.addEventListener(type, handler), timeout: fn => timers.push(fn) },
    fullThreadTitle: item => item.title,
    renameThread() {},
    renderThreads() {},
    checkIconMarkup: () => "",
    closeIconMarkup: () => "",
  };
  loadFunctions(sandbox, ["focusedThreadListTarget", "restoreThreadListFocus", "threadRowButton", "appendRenameControls"]);
  return { sandbox, document, list, runTimers: () => timers.splice(0).forEach(fn => fn()) };
}

function drawSections(document, list, sessionKeys) {
  list.replaceChildren();
  for (const id of ["recent", "snoozed"]) {
    const section = Object.assign(document.createElement("section"), { className: "thread-section" });
    section.dataset.section = id;
    const header = Object.assign(document.createElement("button"), { className: "thread-section-header" });
    section.appendChild(header);
    for (const key of sessionKeys[id] || []) {
      const row = Object.assign(document.createElement("div"), { className: "thread-row" });
      row.dataset.sessionKey = key;
      row.appendChild(Object.assign(document.createElement("button"), { className: "thread-button" }));
      section.appendChild(row);
    }
    list.appendChild(section);
  }
}

test("a focused section header or row is focused again after the list is drawn again", () => {
  const { sandbox, document, list } = domHarness();
  drawSections(document, list, { recent: ["alpha"], snoozed: ["beta"] });
  list.children[1].children[0].focus();
  const header = sandbox.focusedThreadListTarget();
  assert.deepEqual({ ...header }, { section: "snoozed" });
  drawSections(document, list, { recent: ["alpha"], snoozed: ["beta"] });
  sandbox.restoreThreadListFocus(header);
  assert.equal(document.activeElement, list.children[1].children[0]);

  list.children[0].children[1].children[0].focus();
  const row = sandbox.focusedThreadListTarget();
  assert.deepEqual({ ...row }, { sessionKey: "alpha" });
  drawSections(document, list, { recent: ["alpha"], snoozed: ["beta"] });
  sandbox.restoreThreadListFocus(row);
  assert.equal(document.activeElement, list.children[0].children[1].children[0]);
});

test("what the person typed in a rename field survives a redraw", () => {
  const { sandbox, document, runTimers } = domHarness();
  const thread = { id: "alpha", title: "Old name" };
  const firstRow = document.createElement("div");
  sandbox.appendRenameControls(firstRow, thread);
  const input = firstRow.children[0].children[0];
  input.setSelectionRange = (start, end) => { input.selected = [start, end]; };
  input.select = () => { input.selected = "all"; };
  runTimers();
  assert.equal(input.selected, "all", "a new rename starts with the name selected");
  input.value = "New na";
  input.selectionStart = 6;
  input.selectionEnd = 6;
  input.dispatchEvent(new Event("input"));

  const secondRow = document.createElement("div");
  sandbox.appendRenameControls(secondRow, thread);
  const redrawn = secondRow.children[0].children[0];
  redrawn.setSelectionRange = (start, end) => { redrawn.selected = [start, end]; };
  runTimers();
  assert.equal(redrawn.value, "New na");
  assert.deepEqual(redrawn.selected, [6, 6], "the caret stays where it was");
  assert.equal(document.activeElement, redrawn);
});

test("a change that did not get through offers Retry for the same change; a refusal does not (C28)", async () => {
  const { sandbox, server, patches, errors, thread } = harness();
  server.sessions = [{ ...SNOOZED, list_state: null }, { session_key: "alpha", updated_at: "2026-11-19T09:00:00Z" }];
  await sandbox.fetchSessions();
  server.offline = true;
  assert.equal(await sandbox.changeThreadPlacement(thread("gamma"), "snooze", { snooze_until: "2026-11-20T09:00:00+00:00" }), false);
  assert.equal(errors[0].message, "\u201cRenew the certificate\u201d was not moved. Try again.");
  server.offline = false;
  await errors[0].retry();
  assert.deepEqual(patches.map(item => item.body.thread_action), ["snooze", "snooze"]);
  assert.equal(patches[1].body.snooze_until, "2026-11-20T09:00:00+00:00");

  server.refusal = "Thread not found.";
  await sandbox.changeThreadPlacement(thread("gamma"), "pin");
  assert.equal(errors[1].message, "\u201cRenew the certificate\u201d was not moved. Thread not found.");
  assert.equal(errors[1].retry, null, "repeating a refusal cannot help");
});

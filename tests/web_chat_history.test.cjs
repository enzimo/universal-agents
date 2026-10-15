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
    if (start < 0) continue;
    const end = source.indexOf("\nfunction ", start + 1);
    vm.runInContext(source.slice(start, end < 0 ? undefined : end), sandbox);
  }
}

function harness() {
  const calls = [], renders = [], errors = [];
  const sessions = Array.from({ length: 50 }, (_, i) => ({
    session_key: `session_${i}`, title: `Thread ${i}`,
    updated_at: "2026-09-12T00:00:00Z",
  }));
  const sandbox = {
    AbortSignal,
    attention: { receive: () => true, away: () => false },
    document: { getElementById: () => ({ hidden: true }) },
    sharedState: { authSessionActive: true, authSessionGeneration: 1,
      activeThreadId: "session_17", threads: [], asyncEventCursor: "" },
    shellState: { SYSTEM_ERROR_SOURCE: { FETCH_SESSIONS: "sessions", HYDRATE_THREAD: "history" } },
    authentication: { handleUnauthorizedResponse() {} },
    normalizeThreadTitle: value => String(value || ""),
    threadFromSeed: seed => ({ ...seed, messages: [], transientItems: [], historyLoaded: false }),
    saveThreads() {}, renderThreads() {}, clearSystemError() { return false; },
    renderMessages(options) { renders.push({ active: sandbox.sharedState.activeThreadId, options }); },
    appendSystemError(...args) { errors.push(args); },
    mergeTranscriptMessages(thread, messages) { thread.messages.push(...messages); },
    requestClient: { async fetch(url) {
      calls.push(url);
      const payload = url === "/api/web-chat/sessions"
        ? { sessions, event_cursor: "snapshot" }
        : { messages: [{ message_id: url, session_sequence: 1000 }], before_cursor: "older", has_more: true };
      return { ok: true, json: async () => payload };
    } },
  };
  sandbox.activeThread = () => sandbox.sharedState.threads.find(t => t.id === sandbox.sharedState.activeThreadId);
  loadFunctions(sandbox, ["parseApiResponse", "fetchSessions", "hydrateThread", "ensureActiveThreadHistory",
    "historyRequestIsCurrent", "loadEarlierHistory"]);
  return { sandbox, calls, renders, errors, sessions };
}

test("startup hydrates only the selected thread among 50 listed threads", async () => {
  const { sandbox, calls } = harness();
  await sandbox.fetchSessions();
  assert.deepEqual(calls.filter(url => url.includes("/messages")), [
    "/api/web-chat/sessions/session_17/messages?limit=50",
  ]);
  assert.equal(sandbox.sharedState.threads.filter(t => t.messages.length).length, 1);
});

test("opening Chat with no sessions does not create an unwanted conversation", async () => {
  const { sandbox, sessions, errors } = harness();
  sessions.length = 0;
  let creations = 0;
  sandbox.createServerThread = async () => { creations++; };
  await sandbox.fetchSessions();
  await sandbox.fetchSessions();
  assert.equal(creations, 0);
  assert.equal(sandbox.sharedState.threads.length, 0);
  assert.equal(sandbox.sharedState.activeThreadId, "");
  assert.equal(errors.length, 0);
});

test("concurrent first-message creation shares one session request", async () => {
  const { sandbox } = harness();
  loadFunctions(sandbox, ["createServerThread", "createThread", "threadForSession"]);
  const response = deferredResponse(sandbox);
  const first = sandbox.createServerThread();
  const second = sandbox.createServerThread();
  assert.equal(response.requests, 1);
  response.finish({ session: { session_key: "new_session" } });
  const [left, right] = await Promise.all([first, second]);
  assert.equal(left, right);
  assert.equal(sandbox.sharedState.threads.length, 1);
});

test("session creation finishing after sign-out cannot restore the old user's thread", async () => {
  const { sandbox } = harness();
  loadFunctions(sandbox, ["createServerThread", "createThread", "threadForSession"]);
  const response = deferredResponse(sandbox);
  const pending = sandbox.createServerThread();
  sandbox.sharedState.authSessionGeneration++;
  response.finish({ session: { session_key: "old_owner_session" } });
  assert.equal(await pending, null);
  assert.equal(sandbox.sharedState.threads.length, 0);
});

test("metadata refresh preserves the event cursor and does not reload history", async () => {
  const { sandbox, calls } = harness();
  await sandbox.fetchSessions();
  sandbox.sharedState.asyncEventCursor = "last-delivered-event";
  await sandbox.fetchSessions();
  assert.equal(calls.filter(url => url.includes("/messages")).length, 1);
  assert.equal(sandbox.sharedState.asyncEventCursor, "last-delivered-event");
});

test("selecting an unopened thread loads exactly its newest page", async () => {
  const { sandbox, calls } = harness();
  await sandbox.fetchSessions();
  sandbox.sharedState.activeThreadId = "session_3";
  await sandbox.ensureActiveThreadHistory();
  await sandbox.ensureActiveThreadHistory();
  assert.equal(calls.filter(url => url.includes("/messages")).length, 2);
  assert.equal(calls.at(-1), "/api/web-chat/sessions/session_3/messages?limit=50");
});

function deferredResponse(sandbox) {
  let finish;
  let requests = 0;
  sandbox.requestClient.fetch = () => {
    requests++;
    return new Promise(resolve => { finish = resolve; });
  };
  return {
    finish(payload) { finish({ ok: true, json: async () => payload }); },
    get requests() { return requests; },
  };
}

test("repeated older-page requests share one in-flight request and retain pagination", async () => {
  const { sandbox } = harness();
  await sandbox.fetchSessions();
  const response = deferredResponse(sandbox);
  const thread = sandbox.activeThread();
  const first = sandbox.loadEarlierHistory();
  await sandbox.loadEarlierHistory();
  assert.equal(response.requests, 1);
  response.finish({ messages: [{ message_id: "earlier" }], before_cursor: "earliest", has_more: true });
  await first;
  assert.equal(thread.historyBeforeCursor, "earliest");
  assert.equal(thread.historyRequest, null);
  assert.equal(thread.messages.length, 2);
});

test("late history for an inactive thread never rerenders the selected thread", async () => {
  const { sandbox, renders } = harness();
  await sandbox.fetchSessions();
  const response = deferredResponse(sandbox);
  const pending = sandbox.loadEarlierHistory();
  sandbox.sharedState.activeThreadId = "session_2";
  renders.length = 0;
  response.finish({ messages: [{ message_id: "earlier" }], has_more: false });
  await pending;
  assert.equal(renders.length, 0);
});

test("history and session-list responses are discarded after authentication changes", async () => {
  const { sandbox, renders } = harness();
  await sandbox.fetchSessions();
  const thread = sandbox.activeThread();
  const response = deferredResponse(sandbox);
  const pending = sandbox.loadEarlierHistory();
  sandbox.sharedState.authSessionGeneration++;
  renders.length = 0;
  response.finish({ messages: [{ message_id: "old-owner-secret" }], has_more: false });
  await pending;
  assert.equal(thread.messages.length, 1);
  assert.equal(renders.length, 0);

  const sessionsResponse = deferredResponse(sandbox);
  const sessionRequest = sandbox.fetchSessions();
  sandbox.sharedState.authSessionGeneration++;
  sandbox.sharedState.threads = [];
  sessionsResponse.finish({ sessions: [{ session_key: "old-owner-session" }] });
  await sessionRequest;
  assert.equal(sandbox.sharedState.threads.length, 0);
});

test("cursor reset invalidates cached histories and refreshes only the active thread", async () => {
  const { sandbox, calls } = harness();
  await sandbox.fetchSessions();
  sandbox.sharedState.activeThreadId = "session_3";
  await sandbox.ensureActiveThreadHistory();
  const previous = sandbox.sharedState.threads.find(t => t.id === "session_17");
  calls.length = 0;
  await sandbox.fetchSessions({ resetHistory: true });
  assert.equal(previous.historyLoaded, false);
  assert.equal(previous.messages.length, 0);
  assert.deepEqual(calls.filter(url => url.includes("/messages")), [
    "/api/web-chat/sessions/session_3/messages?limit=50",
  ]);
});

test("failed pagination retains its cursor and can be retried explicitly", async () => {
  const { sandbox, errors } = harness();
  await sandbox.fetchSessions();
  const fetch = sandbox.requestClient.fetch;
  sandbox.requestClient.fetch = async () => { throw new Error("offline"); };
  await sandbox.loadEarlierHistory();
  assert.equal(sandbox.activeThread().historyBeforeCursor, "older");
  assert.equal(sandbox.activeThread().historyRequest, null);
  assert.equal(errors.length, 1);
  sandbox.requestClient.fetch = fetch;
  await sandbox.loadEarlierHistory();
  assert.equal(sandbox.activeThread().messages.length, 2);
});

test("archiving the active thread hydrates its successor", async () => {
  const { sandbox, calls } = harness();
  await sandbox.fetchSessions();
  sandbox.clearThreadOptimisticDeliveries = () => {};
  loadFunctions(sandbox, ["removeArchivedThread"]);
  sandbox.removeArchivedThread("session_17");
  await sandbox.ensureActiveThreadHistory();
  assert.equal(sandbox.sharedState.activeThreadId, "session_0");
  assert.equal(calls.at(-1), "/api/web-chat/sessions/session_0/messages?limit=50");
});

test("event-only discovery does not claim to have loaded a thread's history", async () => {
  const { sandbox, calls, renders } = harness();
  await sandbox.fetchSessions();
  sandbox.setThreadUnread = (thread, unread) => { thread.hasUnreadReply = unread; };
  sandbox.messageLevelFromPayload = () => "normal";
  loadFunctions(sandbox, ["applyTranscriptEvent", "createThread", "threadForSession", "isReadingThread"]);
  renders.length = 0;
  sandbox.applyTranscriptEvent({ session_key: "new_session", message_id: "event", role: "assistant" });
  const thread = sandbox.threadForSession("new_session");
  assert.equal(thread.historyLoaded, false);
  assert.equal(thread.messages.length, 0);
  assert.equal(thread.hasUnreadReply, true);
  assert.equal(renders.length, 0);
  sandbox.sharedState.activeThreadId = thread.id;
  await sandbox.ensureActiveThreadHistory();
  assert.equal(calls.at(-1), "/api/web-chat/sessions/new_session/messages?limit=50");
});

test("a cursor reset discards an older page that arrives from the previous history revision", async () => {
  const { sandbox } = harness();
  await sandbox.fetchSessions();
  const fetch = sandbox.requestClient.fetch;
  const response = deferredResponse(sandbox);
  const pending = sandbox.loadEarlierHistory();
  sandbox.requestClient.fetch = fetch;
  await sandbox.fetchSessions({ resetHistory: true });
  const thread = sandbox.activeThread();
  response.finish({ messages: [{ message_id: "obsolete-page" }], before_cursor: "obsolete", has_more: true });
  await pending;
  assert.equal(thread.messages.length, 1);
  assert.equal(thread.historyBeforeCursor, "older");
});

test("concurrent session refreshes share one request", async () => {
  const { sandbox, calls } = harness();
  await Promise.all([sandbox.fetchSessions(), sandbox.fetchSessions()]);
  assert.equal(calls.filter(url => url === "/api/web-chat/sessions").length, 1);
  assert.equal(calls.filter(url => url.includes("/messages")).length, 1);
});

test("a cursor reset arriving during metadata refresh still invalidates history", async () => {
  const { sandbox, sessions } = harness();
  await sandbox.fetchSessions();
  const fetch = sandbox.requestClient.fetch;
  const response = deferredResponse(sandbox);
  const refresh = sandbox.fetchSessions();
  const reset = sandbox.fetchSessions({ resetHistory: true });
  sandbox.requestClient.fetch = fetch;
  response.finish({ sessions, event_cursor: "new-snapshot" });
  await Promise.all([refresh, reset]);
  assert.equal(sandbox.activeThread().historyRevision, 1);
  assert.equal(sandbox.activeThread().historyLoaded, true);
});

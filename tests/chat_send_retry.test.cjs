// Run production send handling with controlled HTTP outcomes, including lost replies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
require("../assistant_gateway/onboarding_web/assets/chat_delivery.js");

const source = fs.readFileSync(path.join(__dirname,
  "../assistant_gateway/onboarding_web/assets/chat.js"), "utf8");

function harness() {
  const original = { id: "original", sessionKey: "original", messages: [], title: "Earlier request", pendingDeliveries: new Map() };
  const destination = { id: "destination", sessionKey: "destination", conversationId: "conversation", messages: [], title: "New Chat", pendingDeliveries: new Map() };
  const requests = [], renders = [];
  const failures = () => Array.from(destination.pendingDeliveries.values()).filter(delivery => delivery.error);
  let nextId = 0;
  const sandbox = {
    NEW_CHAT_TITLE: "New Chat", Date,
    sharedState: { authSessionActive: true, authSessionGeneration: 1, threads: [original, destination] },
    shellState: { operationalIds: { newId: () => `request_${++nextId}` },
      deliveryTracker: { watch() {}, acknowledge() {} }, SYSTEM_ERROR_SOURCE: {} },
    window: { UAChatDelivery: globalThis.UAChatDelivery, URL: { revokeObjectURL() {} } },
    activeThread: () => original,
    isReadingThread: thread => thread === original,
    normalizeThreadTitle: text => text,
    setThreadUnread() {}, clearSystemError() {}, saveThreads() {}, renderThreads() {},
    optimisticMessage: (clientRequestId, content, attachments) => ({ messageId: `optimistic:${clientRequestId}`, optimistic: true, clientRequestId, content, attachments }),
    pendingReplyCount: () => 0, startTerminalReplyWait() {}, finishTerminalReplyWait() {}, setThreadPending() {},
    renderMessages: options => renders.push(options),
    buildChatRequestBody: (content, thread, attachments, id) => ({ body: { content, thread, attachments, id } }),
    liveEvents: { start() {} }, attention: { receive: () => false },
    authentication: { handleUnauthorizedResponse() { throw new Error("Must not sign out a later session"); } },
    normalizeMessageLevel: value => value || "normal",
    requestClient: { fetch(url, options) {
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      requests.push({ ...options.body, resolve, reject });
      return promise;
    } },
  };
  vm.createContext(sandbox);
  for (const name of ["normalizeMessageList", "mergeTranscriptMessages", "releaseMessageAttachmentPreviews", "clearThreadOptimisticDeliveries"]) {
    const start = source.indexOf(`function ${name}(`);
    const end = source.indexOf("\nfunction ", start + 1);
    vm.runInContext(source.slice(start, end), sandbox);
  }
  const start = source.indexOf("function sendMessage(");
  const end = source.indexOf('\nlifecycle.listen(sharedState.authReveal', start);
  vm.runInContext(source.slice(start, end), sandbox);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const succeed = request => request.resolve({ ok: true, json: async () => ({ messages: [
    { message_id: `accepted:${request.id}`, client_request_id: request.id, session_sequence: 1, role: "user", content: request.content },
    { message_id: `reply:${request.id}`, session_sequence: 2, role: "assistant", content: "Completed in the separate chat" },
  ] }) });
  return { sandbox, requests, failures, renders, original, destination, settle, succeed };
}

test("Retry reuses the request, files and destination and does not add another optimistic row", async () => {
  const h = harness();
  const attachments = [{ file: new Blob(["notes"]), filename: "notes.txt" }];
  h.sandbox.sendMessage("New work", { thread: h.destination, attachments });
  assert.equal(h.original.messages.length, 0);
  assert.equal(h.destination.messages.length, 1);
  assert.equal(h.renders.length, 0, "a background send must not scroll the selected chat");
  h.requests[0].reject(new Error("Lost HTTP reply"));
  await h.settle();
  const failed = h.failures()[0];
  failed.retry();
  failed.retry();
  assert.equal(h.requests.length, 2, "double-click Retry sends once");
  assert.equal(h.requests[1].id, h.requests[0].id);
  assert.equal(h.requests[1].thread, h.destination);
  assert.equal(h.requests[1].attachments, attachments);
  assert.equal(h.destination.messages.length, 1);
  h.succeed(h.requests[1]);
  await h.settle();
  assert.equal(h.original.messages.length, 0);
  assert.equal(h.destination.messages.at(-1).content, "Completed in the separate chat");
  assert.equal(h.destination.pendingDeliveries.size, 0);
});

test("a lost reply arriving after another sign-in cannot modify the new person's chat", async () => {
  const h = harness();
  h.sandbox.sendMessage("New work", { thread: h.destination });
  h.sandbox.sharedState.authSessionGeneration++;
  h.succeed(h.requests[0]);
  await h.settle();
  assert.equal(h.destination.messages.length, 1);
  assert.equal(h.failures().length, 0);
});

test("a Retry from an earlier sign-in cannot submit work as the new person", async () => {
  const h = harness();
  h.sandbox.sendMessage("New work", { thread: h.destination });
  h.requests[0].reject(new Error("Connection lost"));
  await h.settle();
  h.sandbox.sharedState.authSessionGeneration++;
  h.failures()[0].retry();
  assert.equal(h.requests.length, 1);
});

test("an old unauthorized HTTP reply cannot sign out a later session", async () => {
  const h = harness();
  h.sandbox.sendMessage("New work", { thread: h.destination });
  h.sandbox.sharedState.authSessionGeneration++;
  let parsed = false;
  h.requests[0].resolve({ ok: false, status: 401, json: async () => { parsed = true; return {}; } });
  await h.settle();
  assert.equal(parsed, false);
  assert.equal(h.failures().length, 0);
});

test("signing in while an unauthorized response body loads still discards the old reply", async () => {
  const h = harness();
  let resolveBody, bodyStarted, unauthorized = 0;
  const started = new Promise(resolve => { bodyStarted = resolve; });
  const body = new Promise(resolve => { resolveBody = resolve; });
  h.sandbox.authentication.handleUnauthorizedResponse = () => { unauthorized++; };
  h.sandbox.sendMessage("New work", { thread: h.destination });
  h.requests[0].resolve({ ok: false, status: 401, json: () => { bodyStarted(); return body; } });
  await started;
  h.sandbox.sharedState.authSessionGeneration++;
  resolveBody({});
  await h.settle();
  assert.equal(unauthorized, 0);
  assert.equal(h.failures().length, 0);
});

test("later sends and live acknowledgements preserve each failed request's files and Retry", async () => {
  const h = harness();
  const attachments = [{ file: new Blob(["notes"]), filename: "notes.txt" }];
  h.sandbox.sendMessage("First work", { thread: h.destination, attachments });
  h.requests[0].reject(new Error("Connection lost"));
  await h.settle();
  const first = h.failures()[0];
  h.sandbox.sendMessage("Later work", { thread: h.destination });
  h.succeed(h.requests[1]);
  await h.settle();
  assert.deepEqual(h.failures(), [first]);
  h.sandbox.sendMessage("Other failed work", { thread: h.destination });
  h.requests[2].reject(new Error("Offline"));
  await h.settle();
  assert.equal(h.failures().length, 2);
  first.retry();
  assert.equal(h.requests[3].id, h.requests[0].id);
  assert.equal(h.requests[3].attachments, attachments);
  assert.equal(h.requests[3].content, "First work");
  h.succeed(h.requests[3]);
  await h.settle();
  assert.equal(h.failures().length, 1);
  assert.equal(h.failures()[0].clientRequestId, h.requests[2].id);
});

test("live acceptance removes only its Retry and a late HTTP failure cannot restore it", async () => {
  const h = harness();
  h.sandbox.sendMessage("Accepted live", { thread: h.destination });
  h.sandbox.mergeTranscriptMessages(h.destination, [{
    message_id: "accepted-live", client_request_id: h.requests[0].id,
    session_sequence: 1, role: "user", content: "Accepted live",
  }]);
  h.requests[0].reject(new Error("Lost HTTP reply"));
  await h.settle();
  assert.equal(h.failures().length, 0);
  assert.equal(h.destination.pendingDeliveries.size, 0);
});

test("archive or sign-out cleanup drops retained files and disables stale Retry callbacks", async () => {
  const h = harness();
  h.sandbox.sendMessage("Unsent work", { thread: h.destination });
  h.requests[0].reject(new Error("Offline"));
  await h.settle();
  const retry = h.failures()[0].retry;
  h.sandbox.clearThreadOptimisticDeliveries(h.destination);
  assert.equal(h.destination.pendingDeliveries.size, 0);
  retry();
  assert.equal(h.requests.length, 1);
});

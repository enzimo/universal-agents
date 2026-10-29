import assert from "node:assert/strict";
import { test } from "node:test";
import { createChatSubmission } from "../assistant_gateway/onboarding_web/assets/chat_submission.mjs";

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// `draft.parallel` is the switch's position while no chat exists yet; a chat
// that exists keeps its own saved setting, which a message never carries.
function harness() {
  const creation = deferred(), saving = deferred();
  const original = { id: "original" }, destination = { id: "destination" };
  const draft = { content: "Research the options", hasAttachments: true, parallel: true };
  const state = { authenticated: true, generation: 1, thread: original };
  const files = [{ file: new Blob(["notes"]), filename: "notes.txt" }];
  const sends = [], failures = [], saves = [];
  let creations = 0, taken = 0, busy = false;
  const creationOptions = [];
  const submission = createChatSubmission({
    readDraft: () => ({ ...draft }), context: () => ({ ...state }),
    createThread(options) { creations++; creationOptions.push(options); return creation.promise; },
    setParallel(thread, value) { saves.push({ thread, value }); return saving.promise; },
    takeDraft() { taken++; draft.content = ""; return files; },
    send(content, options) { sends.push({ content, ...options }); },
    preparing(value) { busy = value; }, failed(error, retry) { failures.push({ error, retry }); },
  });
  return { submission, creation, saving, state, draft, files, original, destination, sends, failures, saves,
    creationOptions, creations: () => creations, taken: () => taken, busy: () => busy };
}

test("a reply keeps the selected conversation and carries no setting", async () => {
  const h = harness();
  const sending = h.submission.submit();
  assert.equal(h.creations(), 0, "a reply must not create an empty conversation");
  await sending;
  assert.equal(h.sends[0].thread, h.original);
  assert.equal("parallelThread" in h.sends[0], false);
  assert.deepEqual(h.saves, [], "an existing chat keeps its own saved setting");
});

test("a new chat takes the switch's position before its first message is sent", async () => {
  const h = harness();
  h.state.thread = null;
  const sending = h.submission.submit();
  await h.submission.submit();
  assert.equal(h.creations(), 1);
  assert.equal(h.creationOptions[0], undefined, "the chat itself is created ordinary");
  assert.equal(h.taken(), 0);
  assert.equal(h.busy(), true);
  h.state.thread = { id: "selected-during-creation" };
  h.creation.resolve(h.destination);
  await Promise.resolve();
  await Promise.resolve();
  assert.deepEqual(h.saves, [{ thread: h.destination, value: true }]);
  assert.equal(h.sends.length, 0, "the message waits for the setting");
  h.saving.resolve();
  await sending;
  assert.equal(h.sends.length, 1);
  assert.equal(h.sends[0].thread, h.destination);
  assert.equal(h.sends[0].attachments, h.files);
  assert.equal(h.busy(), false);
});

test("a refused setting keeps the draft and files and offers Retry", async () => {
  const h = harness();
  h.state.thread = null;
  const sending = h.submission.submit();
  h.creation.resolve(h.destination);
  h.saving.reject(new Error("Run concurrently was not changed."));
  await sending;
  assert.equal(h.taken(), 0);
  assert.equal(h.draft.content, "Research the options");
  assert.equal(h.sends.length, 0);
  assert.equal(typeof h.failures[0].retry, "function");
});

test("a failed creation preserves the draft and files and offers Retry", async () => {
  const h = harness();
  h.state.thread = null;
  const sending = h.submission.submit();
  h.creation.reject(new Error("Connection lost"));
  await sending;
  assert.equal(h.taken(), 0);
  assert.equal(h.draft.content, "Research the options");
  assert.deepEqual(h.saves, []);
  assert.equal(h.sends.length, 0);
  assert.equal(h.busy(), false);
  assert.equal(typeof h.failures[0].retry, "function");
});

test("with the switch off a new chat stays ordinary and a files-only message still starts one", async () => {
  const h = harness();
  h.draft.parallel = false;
  h.draft.content = "";
  h.state.thread = null;
  const sending = h.submission.submit();
  h.creation.resolve(h.destination);
  await sending;
  assert.deepEqual(h.saves, [], "nothing to save for an ordinary chat");
  assert.equal(h.sends[0].thread, h.destination);
  assert.equal(h.sends[0].content, "");
});

test("slash commands retain their existing routing even with the switch on", async () => {
  const h = harness();
  h.draft.content = "/new";
  h.draft.hasAttachments = false;
  await h.submission.submit();
  assert.equal(h.creations(), 0);
  assert.deepEqual(h.saves, []);
  assert.equal(h.sends[0].thread, h.original);
});

test("a creation completing after sign-out cannot consume another person's draft", async () => {
  const h = harness();
  h.state.thread = null;
  const sending = h.submission.submit();
  h.state.generation++;
  h.creation.resolve(h.destination);
  await sending;
  assert.deepEqual(h.saves, [], "nor change the new chat's setting");
  assert.equal(h.taken(), 0);
  assert.equal(h.sends.length, 0);
});

test("reset cancels preparation ownership even before an old creation settles", async () => {
  const h = harness();
  h.state.thread = null;
  const sending = h.submission.submit();
  h.submission.reset();
  assert.equal(h.busy(), false);
  h.creation.resolve(h.destination);
  await sending;
  assert.equal(h.taken(), 0);
  assert.equal(h.sends.length, 0);
});

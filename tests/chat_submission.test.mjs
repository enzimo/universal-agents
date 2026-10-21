import assert from "node:assert/strict";
import { test } from "node:test";
import { createChatSubmission } from "../assistant_gateway/onboarding_web/assets/chat_submission.mjs";

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness() {
  const creation = deferred();
  const original = { id: "original" }, destination = { id: "destination" };
  const draft = { content: "Research the options", hasAttachments: true, separate: true };
  const state = { authenticated: true, generation: 1, thread: original };
  const files = [{ file: new Blob(["notes"]), filename: "notes.txt" }];
  const sends = [], failures = [];
  let creations = 0, taken = 0, busy = false;
  const submission = createChatSubmission({
    readDraft: () => ({ ...draft }), context: () => ({ ...state }),
    createThread() { creations++; return creation.promise; },
    takeDraft() { taken++; draft.content = ""; draft.separate = false; return files; },
    send(content, options) { sends.push({ content, ...options }); },
    preparing(value) { busy = value; }, failed(error, retry) { failures.push({ error, retry }); },
  });
  return { submission, creation, state, draft, files, original, destination, sends, failures,
    creations: () => creations, taken: () => taken, busy: () => busy };
}

test("create the destination before consuming text and files; repeated Send creates one request", async () => {
  const h = harness();
  const sending = h.submission.submit();
  await h.submission.submit();
  assert.equal(h.creations(), 1);
  assert.equal(h.taken(), 0);
  assert.equal(h.busy(), true);
  h.state.thread = { id: "selected-during-creation" };
  h.creation.resolve(h.destination);
  await sending;
  assert.equal(h.sends.length, 1);
  assert.equal(h.sends[0].thread, h.destination);
  assert.equal(h.sends[0].attachments, h.files);
  assert.equal(h.draft.separate, false);
  assert.equal(h.busy(), false);
});

test("a failed creation preserves the draft and files and offers Retry", async () => {
  const h = harness();
  const sending = h.submission.submit();
  h.creation.reject(new Error("Connection lost"));
  await sending;
  assert.equal(h.taken(), 0);
  assert.equal(h.draft.content, "Research the options");
  assert.equal(h.draft.separate, true);
  assert.equal(h.sends.length, 0);
  assert.equal(h.busy(), false);
  assert.equal(typeof h.failures[0].retry, "function");
});

test("an unchecked switch keeps the selected thread and a files-only message still starts a chat", async () => {
  const h = harness();
  h.draft.separate = false;
  await h.submission.submit();
  assert.equal(h.creations(), 0);
  assert.equal(h.sends[0].thread, h.original);
  h.draft.content = "";
  h.state.thread = null;
  const sending = h.submission.submit();
  h.creation.resolve(h.destination);
  await sending;
  assert.equal(h.sends[1].thread, h.destination);
  assert.equal(h.sends[1].content, "");
});

test("slash commands retain their existing routing even with the switch selected", async () => {
  const h = harness();
  h.draft.content = "/new";
  h.draft.hasAttachments = false;
  await h.submission.submit();
  assert.equal(h.creations(), 0);
  assert.equal(h.sends[0].thread, h.original);
});

test("a creation completing after sign-out cannot consume another person's draft", async () => {
  const h = harness();
  const sending = h.submission.submit();
  h.state.generation++;
  h.creation.resolve(h.destination);
  await sending;
  assert.equal(h.taken(), 0);
  assert.equal(h.sends.length, 0);
});

test("reset cancels preparation ownership even before an old creation settles", async () => {
  const h = harness();
  const sending = h.submission.submit();
  h.submission.reset();
  assert.equal(h.busy(), false);
  h.creation.resolve(h.destination);
  await sending;
  assert.equal(h.taken(), 0);
  assert.equal(h.sends.length, 0);
});

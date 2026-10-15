import assert from "node:assert/strict";
import test from "node:test";
import { createAuthentication, SESSION_RESTORE_TIMEOUT_MS } from "../assistant_gateway/onboarding_web/assets/chat_authentication.mjs";
import { createPageLifecycle, createRequestClient } from "../assistant_gateway/onboarding_web/assets/browser_lifecycle.mjs";

function browserClock() {
  let next = 0;
  const pending = new Map();
  const schedule = (callback) => { const id = ++next; pending.set(id, callback); return id; };
  const clear = (id) => pending.delete(id);
  return {
    AbortController, pending,
    setTimeout: schedule, setInterval: schedule, requestAnimationFrame: schedule,
    clearTimeout: clear, clearInterval: clear, cancelAnimationFrame: clear,
  };
}

test("disposal removes listeners, cancels queued work and aborts requests", async () => {
  const browser = browserClock();
  const lifecycle = createPageLifecycle(browser);
  let calls = 0;
  const target = new EventTarget();
  lifecycle.listen(target, "change", () => calls++);
  target.dispatchEvent(new Event("change"));
  assert.equal(calls, 1);
  lifecycle.timeout(() => calls++, 10);
  lifecycle.interval(() => calls++, 10);
  lifecycle.frame(() => calls++);
  const queued = [...browser.pending.values()];
  browser.fetch = (_url, {signal}) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason), {once: true});
  });
  const request = createRequestClient(browser, lifecycle).fetch("/pending");
  const rejected = assert.rejects(request, {name: "AbortError"});
  lifecycle.dispose();
  lifecycle.dispose();
  await rejected;
  target.dispatchEvent(new Event("change"));
  queued.forEach(callback => callback());
  assert.equal(calls, 1);
  assert.equal(browser.pending.size, 0);
  assert.equal(lifecycle.timeout(() => calls++, 1), null);
});

test("a new page owns fresh subscriptions after the old page is disposed", () => {
  const browser = browserClock();
  const target = new EventTarget();
  let oldCalls = 0;
  let newCalls = 0;
  const oldPage = createPageLifecycle(browser);
  oldPage.listen(target, "change", () => oldCalls++);
  oldPage.dispose();
  const newPage = createPageLifecycle(browser);
  newPage.listen(target, "change", () => newCalls++, true);
  target.dispatchEvent(new Event("change"));
  assert.equal(oldCalls, 0);
  assert.equal(newCalls, 1);
  newPage.dispose();
});


test("session restoration times out visibly but page disposal stays quiet", async () => {
  for (const disposed of [false, true]) {
    const browser = browserClock();
    const lifecycle = createPageLifecycle(browser);
    let shown = 0;
    let requestSignal;
    browser.fetch = (_url, {signal}) => new Promise((_resolve, reject) => {
      requestSignal = signal;
      signal.addEventListener("abort", () => reject(signal.reason), {once: true});
    });
    const authentication = createAuthentication({
      authenticationState: {}, sharedState: {}, lifecycle,
      requestClient: createRequestClient(browser, lifecycle),
      operations: { showSignIn() { shown++; } },
    });
    const restore = authentication.resumeWebChatSession();
    assert.equal(shown, 0);
    assert.ok(SESSION_RESTORE_TIMEOUT_MS > 0);
    if (disposed) lifecycle.dispose();
    else [...browser.pending.values()].forEach(callback => callback());
    await restore;
    assert.equal(requestSignal.aborted, true);
    assert.equal(requestSignal.reason.name, disposed ? "AbortError" : "TimeoutError");
    assert.equal(shown, disposed ? 0 : 1);
    assert.equal(browser.pending.size, 0);
  }
});

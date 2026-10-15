import assert from "node:assert/strict";
import test from "node:test";
import { createPageLifecycle } from "../assistant_gateway/onboarding_web/assets/browser_lifecycle.mjs";
import { createLiveEvents } from "../assistant_gateway/onboarding_web/assets/chat_live_events.mjs";

function harness(overrides = {}) {
  const timers = new Map(), sources = [], applied = [], statuses = [];
  let id = 0, cursor = "saved", unauthorized = 0;
  const browser = new EventTarget();
  Object.assign(browser, {
    AbortController, document: new EventTarget(), console: { warn() {} },
    setTimeout(callback, delay) { timers.set(++id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    EventSource: class extends EventTarget {
      constructor(url) { super(); this.url = url; sources.push(this); }
      close() { this.closed = true; }
      page(payload) { this.onmessage({ data: JSON.stringify(payload) }); }
    },
  });
  const lifecycle = createPageLifecycle(browser);
  const options = { lifecycle, browser, getCursor: () => cursor,
    isAuthenticated: () => true,
    applyPage(payload) { applied.push(payload); cursor = payload.next_cursor; },
    resetHistory: async () => { cursor = "reset"; }, checkSession: async () => true,
    onUnauthorized() { unauthorized++; }, onStatus: status => statuses.push(status),
    ...overrides,
  };
  const live = createLiveEvents(options);
  function fire(delay) {
    const found = [...timers].find(([, timer]) => timer.delay === delay);
    assert.ok(found, `missing ${delay}ms timer`);
    timers.delete(found[0]); found[1].callback();
  }
  return { live, lifecycle, sources, applied, statuses, fire, options, browser,
    get cursor() { return cursor; }, get unauthorized() { return unauthorized; } };
}
const page = (cursor, messages = []) => ({ status: "ok", messages, next_cursor: cursor });
const settle = () => new Promise(resolve => setImmediate(resolve));

test("a silent or stalled connection is closed and retried without a page reload", () => {
  const h = harness(); h.live.start(); h.live.start();
  assert.equal(h.sources.length, 1);
  h.fire(15000);
  assert.equal(h.sources[0].closed, true);
  assert.match(h.statuses.at(-1), /disconnected/);
  h.fire(1000);
  h.sources[1].page(page("delivered", [{ message_type: "access_review" }]));
  assert.equal(h.cursor, "delivered");
  assert.match(h.statuses.at(-1), /connected/);
});

test("reconnection replays from the applied cursor and ignores late events from the old connection", () => {
  const h = harness(); h.live.start();
  h.sources[0].page(page("one"));
  h.sources[0].onerror(); h.fire(1000);
  assert.match(h.sources[1].url, /after=one$/);
  h.sources[0].page(page("stale"));
  assert.equal(h.cursor, "one");
  h.sources[1].page(page("two"));
  assert.equal(h.applied.length, 2);
});

test("heartbeat pages keep the connection healthy without creating messages", () => {
  const h = harness(); h.live.start();
  h.sources[0].page(page("saved"));
  assert.deepEqual(h.applied[0].messages, []);
  assert.equal(h.sources.length, 1);
});

test("a replaced durable stream reloads history before connecting with its new cursor", async () => {
  const h = harness(); h.live.start();
  h.sources[0].page({ status: "cursor_reset_required" });
  assert.equal(h.sources[0].closed, true);
  await settle();
  assert.match(h.sources[1].url, /after=reset$/);
});

test("sign-out and page disposal close the connection and reject old-user events", () => {
  const h = harness(); h.live.start(); h.live.stop();
  h.sources[0].page(page("old-user"));
  assert.equal(h.applied.length, 0);
  h.live.start(); h.lifecycle.dispose();
  assert.equal(h.sources[1].closed, true);
  h.sources[1].page(page("disposed"));
  assert.equal(h.applied.length, 0);
});

test("authentication revocation closes the stream and returns to sign-in", () => {
  const h = harness(); h.live.start();
  h.sources[0].dispatchEvent(new Event("unauthorized"));
  assert.equal(h.unauthorized, 1);
  assert.equal(h.sources[0].closed, true);
});

test("malformed events do not advance the replay cursor", () => {
  const h = harness(); h.live.start();
  h.sources[0].onmessage({ data: "broken" }); h.fire(1000);
  assert.match(h.sources[1].url, /after=saved$/);
  assert.equal(h.applied.length, 0);
});

test("an expired initial handshake stops retrying even when session checking spans reconnects", async () => {
  let resolve;
  const h = harness({ checkSession: () => new Promise(done => { resolve = done; }) });
  h.live.start(); h.sources[0].onerror();
  await settle();
  h.fire(1000);
  resolve(false);
  await settle();
  assert.equal(h.unauthorized, 1);
  assert.equal(h.sources[1].closed, true);
});

test("a delayed old login check cannot log out the next authenticated session", async () => {
  let resolve;
  const h = harness({ checkSession: () => new Promise(done => { resolve = done; }) });
  h.live.start(); h.sources[0].onerror();
  await settle();
  h.live.stop(); h.live.start(); resolve(false);
  await settle();
  assert.equal(h.unauthorized, 0);
  assert.equal(h.sources[1].closed, undefined);
  h.sources[0].page({ ...page("old-user"), transient_actions: [{ action: { url: "old-secret" } }] });
  assert.equal(h.applied.length, 0);
});

test("a failed page application replays the same cursor instead of losing its messages", () => {
  const h = harness({ applyPage() { throw new Error("render failed"); } });
  h.live.start(); h.sources[0].page(page("not-applied")); h.fire(1000);
  assert.match(h.sources[1].url, /after=saved$/);
});

test("failed history recovery waits before reconnecting", async () => {
  const h = harness({ resetHistory: async () => { throw new Error("unavailable"); } });
  h.live.start(); h.sources[0].page({ status: "cursor_reset_required" });
  await settle();
  assert.equal(h.sources.length, 1);
  assert.match(h.statuses.at(-1), /disconnected/);
  h.fire(1000);
  assert.match(h.sources[1].url, /after=saved$/);
});

test("focus during history recovery cannot race the replacement snapshot", async () => {
  let finish;
  const h = harness({ resetHistory: () => new Promise(resolve => { finish = resolve; }) });
  h.live.start(); h.sources[0].page({ status: "cursor_reset_required" });
  await settle();
  h.browser.dispatchEvent(new Event("focus"));
  h.browser.dispatchEvent(new Event("online"));
  assert.equal(h.sources.length, 1);
  finish(); await settle();
  assert.equal(h.sources.length, 2);
});

test("a late send completion cannot restart delivery after sign-out", () => {
  let signedIn = true;
  const h = harness({ isAuthenticated: () => signedIn });
  h.live.start(); h.live.stop(); signedIn = false;
  h.live.start();
  assert.equal(h.sources.length, 1);
  assert.equal(h.sources[0].closed, true);
});

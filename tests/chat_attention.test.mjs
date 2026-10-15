import test from "node:test";
import assert from "node:assert/strict";
import { createChatAttention } from "../assistant_gateway/onboarding_web/assets/chat_attention.mjs";

function fixture() {
  // The Chat Alerts switch: a checkbox that reports "change".
  const toggle = new EventTarget(), status = {};
  const document = { title: "Chat", hidden: true, hasFocus: () => false,
    getElementById: id => id.endsWith("toggle") ? toggle : status };
  const controller = new AbortController(), notices = [], preferences = new Map();
  let authenticated = true, opened = 0;
  class Notification {
    static permission = "granted";
    constructor(title, options) { this.title = title; this.options = options; notices.push(this); }
    close() { this.closed = true; }
  }
  const browser = { document, Notification, isSecureContext: true, focus() {},
    localStorage: { getItem: key => preferences.get(key), setItem: (key, value) => preferences.set(key, value) } };
  const attention = createChatAttention({ browser, document,
    lifecycle: { signal: controller.signal, listen: (target, event, fn) => target.addEventListener(event, fn) },
    getThreads: () => [], isAuthenticated: () => authenticated, openThread: () => opened++ });
  attention.configure("owner");
  return { attention, browser, toggle, notices, preferences, controller,
    get opened() { return opened; }, signOut: () => { authenticated = false; attention.clear(); } };
}
const event = (id, sequence = id) => ({ session_key: "session", message_id: String(id), session_sequence: sequence, role: "assistant" });

test("alerts require opt-in, are private, and deduplicate replay beyond the ID cache", async () => {
  const f = fixture();
  f.attention.receive(event(1), {});
  assert.equal(f.notices.length, 0);
  f.toggle.dispatchEvent(new Event("change"));
  assert.equal(f.toggle.checked, true, "the switch shows alerts are on");
  f.attention.receive(event(2), {});
  assert.equal(f.notices.length, 1);
  assert.equal(f.notices[0].options.body, "Open Universal Agents to read it.");
  f.attention.receive({ ...event(3), role: "user" }, {});
  assert.equal(f.notices.length, 1);
  for (let i = 3; i < 2010; i++) f.attention.receive(event(i), {});
  const count = f.notices.length;
  assert.equal(f.attention.receive(event(2), {}), false);
  assert.equal(f.notices.length, count);
  f.signOut();
  assert.ok(f.notices.every(notice => notice.closed));
  f.notices[0].onclick();
  assert.equal(f.opened, 0, "A previous account's notification cannot open a thread");
});

test("sign-out invalidates an outstanding browser permission choice", async () => {
  const f = fixture();
  f.browser.Notification.permission = "default";
  let resolve;
  f.browser.Notification.requestPermission = () => new Promise(done => { resolve = done; });
  f.toggle.dispatchEvent(new Event("change"));
  f.signOut();
  f.attention.configure("different-owner");
  resolve("granted");
  await new Promise(done => setImmediate(done));
  assert.equal(f.preferences.size, 0);
  f.attention.receive(event(1), {});
  assert.equal(f.notices.length, 0);
});

test("unsupported or denied notifications do not break message processing", async () => {
  const f = fixture();
  f.browser.Notification.permission = "denied";
  f.browser.Notification.requestPermission = async () => "denied";
  f.toggle.dispatchEvent(new Event("change"));
  await new Promise(done => setImmediate(done));
  assert.equal(f.attention.receive(event(1), {}), true);
  assert.equal(f.notices.length, 0);
  delete f.browser.Notification;
  f.attention.refresh();
  assert.equal(f.toggle.disabled, true);
  assert.equal(f.attention.receive(event(2), {}), true);
});

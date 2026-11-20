// The broker card view's bridge (secure_tool_broker/page_assets/card_bridge.js)
// runs inside a sandboxed iframe in Web Chat. It may tell the chat only the
// page's height and, at a confirmed end, its outcome: never a field value.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const BRIDGE = readFileSync(
  new URL("../secure_tool_broker/page_assets/card_bridge.js", import.meta.url),
  "utf8",
);
const CHAT_ORIGIN = "https://chat.example.test";
const TYPED_SECRET = "ghp_typed_secret_value";

function runBridge({ parentOrigin = CHAT_ORIGIN, outcome = "", framed = true, readyState = "complete" } = {}) {
  const posted = [];
  const listeners = [];
  const observers = [];
  let height = 240;
  const attributes = { "data-card-parent-origin": parentOrigin, "data-card-outcome": outcome };
  // A form the person typed into: the bridge must never read or send it. Every
  // way to reach a field records the attempt.
  const touched = [];
  const input = {
    name: "token",
    get value() {
      touched.push("input.value");
      return TYPED_SECRET;
    },
  };
  const record = (name, result) => (...args) => {
    touched.push(name);
    return result(...args);
  };
  const document = {
    readyState,
    documentElement: { getAttribute: (name) => attributes[name] || null },
    body: { getBoundingClientRect: () => ({ height }) },
    get forms() {
      touched.push("document.forms");
      return [{ elements: [input] }];
    },
    querySelector: record("querySelector", () => input),
    querySelectorAll: record("querySelectorAll", () => [input]),
    getElementById: record("getElementById", () => input),
    getElementsByTagName: record("getElementsByTagName", () => [input]),
    addEventListener: (type, callback) => listeners.push({ target: "document", type, callback }),
  };
  // Messages are copied out of the script's realm, as postMessage would clone them.
  const parent = {
    postMessage: (message, targetOrigin) => posted.push({ message: JSON.parse(JSON.stringify(message)), targetOrigin }),
  };
  const window = {
    addEventListener: (type, callback) => listeners.push({ target: "window", type, callback }),
    ResizeObserver: class {
      constructor(callback) {
        this.callback = callback;
        observers.push(this);
      }
      observe() {}
    },
  };
  window.parent = framed ? parent : window;
  window.window = window;
  vm.runInNewContext(BRIDGE, { window, document, Math, Number });
  return {
    posted,
    listeners,
    touched,
    resize(next) {
      height = next;
      observers.forEach((observer) => observer.callback());
    },
    load() {
      listeners.filter((entry) => entry.type === "DOMContentLoaded").forEach((entry) => entry.callback());
    },
  };
}

function assertOnlyHeightAndOutcome(bridge) {
  assert.deepEqual(bridge.touched, [], "the bridge reads no form field");
  for (const { message, targetOrigin } of bridge.posted) {
    assert.equal(targetOrigin, CHAT_ORIGIN, "posts only to the Web Chat's own address");
    assert.equal(message.type, "ua-broker-card");
    const keys = Object.keys(message).sort();
    assert.ok(
      JSON.stringify(keys) === JSON.stringify(["height", "type"])
        || JSON.stringify(keys) === JSON.stringify(["outcome", "type"]),
      `unexpected message shape ${JSON.stringify(message)}`,
    );
    if ("height" in message) {
      assert.ok(Number.isInteger(message.height) && message.height > 0, "height is a whole number of pixels");
    } else {
      assert.ok(["finished", "declined"].includes(message.outcome), "outcome is one of the two ends");
    }
    assert.ok(!JSON.stringify(message).includes(TYPED_SECRET), "never sends a typed value");
  }
}

test("reports the page height, again when it changes, and nothing else", () => {
  const bridge = runBridge();
  assert.deepEqual(bridge.posted.map((entry) => entry.message), [{ type: "ua-broker-card", height: 240 }]);
  bridge.resize(240);
  bridge.resize(312.4);
  assert.deepEqual(bridge.posted.map((entry) => entry.message.height), [240, 313]);
  assertOnlyHeightAndOutcome(bridge);
});

test("reports a finished or declined page once, and ignores any other marker", () => {
  for (const outcome of ["finished", "declined"]) {
    const bridge = runBridge({ outcome });
    assert.deepEqual(
      bridge.posted.map((entry) => entry.message).filter((message) => "outcome" in message),
      [{ type: "ua-broker-card", outcome }],
    );
    assertOnlyHeightAndOutcome(bridge);
  }
  const unknown = runBridge({ outcome: "stored" });
  assert.equal(unknown.posted.filter((entry) => "outcome" in entry.message).length, 0);
});

test("listens to no message and sends nothing outside a frame or without the chat's address", () => {
  const bridge = runBridge({ outcome: "finished" });
  assert.equal(bridge.listeners.filter((entry) => entry.type === "message").length, 0);
  assert.deepEqual(runBridge({ framed: false }).posted, []);
  assert.deepEqual(runBridge({ parentOrigin: "" }).posted, []);
});

test("waits for the document when it is still loading", () => {
  const bridge = runBridge({ readyState: "loading", outcome: "declined" });
  assert.deepEqual(bridge.posted, []);
  bridge.load();
  assert.equal(bridge.posted.length, 2);
  assertOnlyHeightAndOutcome(bridge);
});

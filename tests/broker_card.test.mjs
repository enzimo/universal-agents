// Web Chat's broker card frames (assistant_gateway/onboarding_web/assets/broker_card.mjs):
// the sandbox, which messages count, the height, the outcome, the fallback to
// "Open in its own tab", and that a closed card leaves nothing behind (A2, A3, A5).
import assert from "node:assert/strict";
import test from "node:test";

import {
  BROKER_CARD_NOTES,
  CARD_FRAME_SANDBOX,
  FIRST_MESSAGE_AFTER_LOAD_MS,
  LOAD_LIMIT_MS,
  MIN_FRAME_HEIGHT,
  OPEN_IN_OWN_TAB_LABEL,
  brokerCardElement,
  createBrokerCards,
  isCardMessage,
  replaceChildrenKeeping,
} from "../assistant_gateway/onboarding_web/assets/broker_card.mjs";
import { buttonLabels, byClass, byTag, fakeDocument } from "./fake_dom.mjs";

const FRAME_URL = "https://broker.example.test/credential-collection/s1?view=card";

function harness() {
  const document = fakeDocument();
  const browser = new EventTarget();
  browser.innerHeight = 1000;
  const timers = [];
  const warnings = [];
  const lifecycle = {
    listen: (target, type, callback) => target.addEventListener(type, callback),
    timeout(callback, delay) {
      const timer = { callback, delay, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimeout(timer) {
      if (timer) timer.cleared = true;
    },
  };
  const cards = createBrokerCards({ browser, document, lifecycle, logger: { warn: (...args) => warnings.push(args) } });
  const events = [];
  function open(itemId = "secure_credential_collection:req-1") {
    const element = document.createElement("div");
    const frame = cards.open({
      itemId,
      frameUrl: FRAME_URL,
      title: "Portainer API token",
      element,
      onOutcome: (outcome) => events.push(["outcome", itemId, outcome]),
      onUnavailable: () => events.push(["unavailable", itemId]),
    });
    // The frame's window, as the browser gives it once the frame is in the page.
    frame.contentWindow = { name: `window of ${itemId}` };
    element.appendChild(frame);
    document.body.appendChild(element);
    return { frame, element };
  }
  function post(source, data) {
    const event = new Event("message");
    Object.assign(event, { source, data, origin: "null" });
    browser.dispatchEvent(event);
  }
  function fire(delay) {
    for (const timer of timers.filter((entry) => entry.delay === delay && !entry.cleared)) {
      timer.cleared = true;
      timer.callback();
    }
  }
  return { document, cards, events, warnings, timers, open, post, fire };
}

test("the frame is sandboxed without allow-same-origin and points at the card view", () => {
  const { open } = harness();
  const { frame } = open();
  assert.equal(frame.getAttribute("sandbox"), CARD_FRAME_SANDBOX);
  assert.equal(CARD_FRAME_SANDBOX, "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox");
  assert.ok(!frame.getAttribute("sandbox").includes("allow-same-origin"));
  assert.ok(!frame.getAttribute("sandbox").includes("allow-top-navigation"));
  assert.equal(frame.getAttribute("src"), FRAME_URL);
  assert.equal(frame.getAttribute("referrerpolicy"), "no-referrer");
  assert.equal(frame.getAttribute("title"), "Portainer API token");
});

test("only the card's own frame is heard, and only in the bridge's two shapes", () => {
  const { open, post, events } = harness();
  const { frame } = open();
  post({ name: "another frame" }, { type: "ua-broker-card", outcome: "finished" });
  post(null, { type: "ua-broker-card", outcome: "finished" });
  for (const data of [
    { type: "ua-broker-card", outcome: "stored" },
    { type: "ua-broker-card", outcome: "finished", value: "ghp_typed" },
    { type: "other", outcome: "finished" },
    { type: "ua-broker-card", height: "300" },
    { type: "ua-broker-card", height: Infinity },
    ["ua-broker-card", "finished"],
    "finished",
  ]) {
    post(frame.contentWindow, data);
  }
  assert.deepEqual(events, []);
  assert.equal(frame.style.height, "160px");

  post(frame.contentWindow, { type: "ua-broker-card", outcome: "declined" });
  assert.deepEqual(events, [["outcome", "secure_credential_collection:req-1", "declined"]]);
});

test("isCardMessage accepts exactly the bridge's messages", () => {
  assert.ok(isCardMessage({ type: "ua-broker-card", height: 240 }));
  assert.ok(isCardMessage({ type: "ua-broker-card", outcome: "finished" }));
  assert.ok(!isCardMessage({ type: "ua-broker-card", height: 0 }));
  assert.ok(!isCardMessage({ type: "ua-broker-card", height: 240, outcome: "finished" }));
  assert.ok(!isCardMessage(null));
});

test("the frame follows the page's height within limits", () => {
  const { open, post } = harness();
  const { frame } = open();
  post(frame.contentWindow, { type: "ua-broker-card", height: 312.2 });
  assert.equal(frame.style.height, "313px");
  post(frame.contentWindow, { type: "ua-broker-card", height: 5 });
  assert.equal(frame.style.height, `${MIN_FRAME_HEIGHT}px`);
  post(frame.contentWindow, { type: "ua-broker-card", height: 50000 });
  assert.equal(frame.style.height, "700px", "at most 70% of the window; a longer page scrolls inside");
});

test("an outcome closes the card once and removes its frame", () => {
  const { open, post, events, cards } = harness();
  const { frame, element } = open();
  post(frame.contentWindow, { type: "ua-broker-card", outcome: "finished" });
  post(frame.contentWindow, { type: "ua-broker-card", outcome: "declined" });
  assert.deepEqual(events, [["outcome", "secure_credential_collection:req-1", "finished"]]);
  assert.ok(!element.contains(frame));
  assert.deepEqual(cards.openItemIds(), []);
});

test("a frame that loads but never speaks falls back after a short wait", () => {
  const { open, fire, events, warnings, cards } = harness();
  const { frame } = open();
  fire(FIRST_MESSAGE_AFTER_LOAD_MS);
  assert.deepEqual(events, [], "the short wait starts only when the frame has loaded");
  frame.dispatchEvent(new Event("load"));
  fire(FIRST_MESSAGE_AFTER_LOAD_MS);
  assert.deepEqual(events, [["unavailable", "secure_credential_collection:req-1"]]);
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0][1], "secure_credential_collection:req-1");
  assert.deepEqual(cards.openItemIds(), []);
});

test("a frame that never loads falls back at the load limit, and a frame that speaks never does", () => {
  const quiet = harness();
  quiet.open();
  quiet.fire(LOAD_LIMIT_MS);
  assert.deepEqual(quiet.events, [["unavailable", "secure_credential_collection:req-1"]]);

  const working = harness();
  const { frame } = working.open();
  frame.dispatchEvent(new Event("load"));
  working.post(frame.contentWindow, { type: "ua-broker-card", height: 200 });
  working.fire(FIRST_MESSAGE_AFTER_LOAD_MS);
  working.fire(LOAD_LIMIT_MS);
  assert.deepEqual(working.events, []);
});

test("a card that leaves the conversation, the thread or the session is closed and no longer heard", () => {
  const { open, post, events, cards } = harness();
  const first = open("tool_setup:a");
  const second = open("tool_setup:b");
  assert.equal(cards.live("tool_setup:a"), first.element);

  cards.retainOnly(["tool_setup:b"]);
  assert.equal(cards.live("tool_setup:a"), null);
  assert.ok(!first.element.contains(first.frame));
  post(first.frame.contentWindow, { type: "ua-broker-card", outcome: "finished" });
  assert.deepEqual(events, [], "a removed frame's message is ignored");

  cards.closeAll();
  assert.deepEqual(cards.openItemIds(), []);
  assert.ok(!second.element.contains(second.frame));
  post(second.frame.contentWindow, { type: "ua-broker-card", outcome: "finished" });
  assert.deepEqual(events, []);
});

test("re-rendering keeps a live card in the page and places the rest around it", () => {
  const document = fakeDocument();
  const list = document.createElement("div");
  const make = (name) => Object.assign(document.createElement("div"), { name });
  const before = make("old message");
  const card = make("card");
  const after = make("old reply");
  list.append(before, card, after);
  // An iframe that leaves the document reloads: the live card must never be detached.
  card.remove = () => {
    throw new Error("the live card was detached");
  };

  const earlier = make("earlier message");
  const message = make("message");
  const reply = make("reply");
  const kept = replaceChildrenKeeping(list, [earlier, message, card, reply], new Set([card]));

  assert.equal(kept, true);
  assert.deepEqual(list.children.map((node) => node.name), ["earlier message", "message", "card", "reply"]);
  assert.equal(card.parentNode, list);
  assert.equal(before.parentNode, null);
  assert.equal(after.parentNode, null);
});

test("when kept cards would have to move, the list is rebuilt and the caller is told", () => {
  const document = fakeDocument();
  const make = (name) => Object.assign(document.createElement("div"), { name });

  const reordered = document.createElement("div");
  const first = make("first card");
  const second = make("second card");
  reordered.append(first, second);
  const swapped = [second, make("message"), first];
  assert.equal(replaceChildrenKeeping(reordered, swapped, new Set([first, second])), false);
  assert.deepEqual(reordered.children.map((node) => node.name), ["second card", "message", "first card"]);

  const elsewhere = document.createElement("div");
  const card = make("card from another list");
  const list = document.createElement("div");
  list.append(make("old"));
  const nodes = [make("message"), card];
  assert.equal(replaceChildrenKeeping(list, nodes, new Set([card])), false);
  assert.deepEqual(list.children.map((node) => node.name), ["message", "card from another list"]);
  assert.equal(elsewhere.children.length, 0);
});

// -- the card element the chat shows --------------------------------------------------

const ITEM = {
  itemId: "tool_setup:req-9",
  label: "Choose your account for Portainer",
  content: "To list your stacks, choose your Portainer account.",
  url: "https://broker.example.test/account-link/s9",
  frameUrl: "https://broker.example.test/account-link/s9?view=card",
  expiresAtUtc: "2026-11-19T16:00:00Z",
  createdAt: "2026-11-19T15:30:00Z",
};

function cardHarness() {
  const h = harness();
  const states = [];
  const render = (state) =>
    brokerCardElement({
      document: h.document,
      cards: h.cards,
      item: ITEM,
      state,
      expiredMessage: "This secure form link has expired.",
      onState: (next) => states.push(next),
    });
  return { ...h, states, render };
}

test("an open broker card is the frame with the reason, countdown and Open in its own tab, and no Dismiss", () => {
  const { render } = cardHarness();
  const card = render("");
  assert.ok(card.hasAttribute("data-broker-frame"));
  assert.equal(card.getAttribute("data-item-id"), ITEM.itemId);
  const [frame] = byTag(card, "iframe");
  assert.equal(frame.getAttribute("src"), ITEM.frameUrl);
  assert.equal(frame.getAttribute("sandbox"), CARD_FRAME_SANDBOX);
  assert.equal(byClass(card, "broker-card-reason")[0].textContent, ITEM.content);
  const [open] = byTag(card, "a");
  assert.equal(open.textContent, OPEN_IN_OWN_TAB_LABEL);
  assert.equal(open.getAttribute("href"), ITEM.url, "its own tab opens the full page, not the card view");
  assert.equal(open.getAttribute("target"), "_blank");
  assert.equal(open.getAttribute("rel"), "noopener noreferrer");
  assert.equal(byClass(card, "message-transient-countdown")[0].getAttribute("data-expires-at"), ITEM.expiresAtUtc);
  assert.deepEqual(buttonLabels(card), [], "Decline is inside the frame; the card has no Dismiss");
});

test("a re-render reuses the open card instead of loading a second frame", () => {
  const { render, cards } = cardHarness();
  const first = render("");
  assert.equal(render(""), first);
  assert.deepEqual(cards.openItemIds(), [ITEM.itemId]);
});

test("the frame's outcome and silence reach the chat as card states", () => {
  const finished = cardHarness();
  const [frame] = byTag(finished.render(""), "iframe");
  frame.contentWindow = { name: "card frame" };
  finished.post(frame.contentWindow, { type: "ua-broker-card", outcome: "finished" });
  assert.deepEqual(finished.states, ["finished"]);

  const silent = cardHarness();
  const [quiet] = byTag(silent.render(""), "iframe");
  quiet.dispatchEvent(new Event("load"));
  silent.fire(FIRST_MESSAGE_AFTER_LOAD_MS);
  assert.deepEqual(silent.states, ["unavailable"]);
});

test("a closed card is one line, and an unavailable one offers its own tab", () => {
  const { render, cards } = cardHarness();
  for (const [state, line] of [
    ["finished", BROKER_CARD_NOTES.finished],
    ["declined", BROKER_CARD_NOTES.declined],
    ["expired", "This secure form link has expired."],
  ]) {
    const closed = render(state);
    assert.equal(closed.textContent, line);
    assert.equal(byTag(closed, "iframe").length, 0);
    assert.equal(byTag(closed, "a").length, 0);
  }
  const unavailable = render("unavailable");
  assert.equal(byTag(unavailable, "iframe").length, 0);
  assert.ok(unavailable.textContent.includes(BROKER_CARD_NOTES.unavailable));
  assert.equal(byTag(unavailable, "a")[0].getAttribute("href"), ITEM.url);
  assert.deepEqual(cards.openItemIds(), [], "no closed or unavailable card opens a frame");
});

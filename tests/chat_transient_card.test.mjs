import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

import { byClass, byTag, fakeDocument } from "./fake_dom.mjs";
import { FOLLOW_UP_AFTER_LATEST, FOLLOW_UP_AFTER_REVIEW } from "../assistant_gateway/onboarding_web/assets/access_inbox.mjs";
import {
  BROKER_CARD_FINISHED,
  CARD_MESSAGE_TYPE,
  brokerCardElement,
  createBrokerCards,
} from "../assistant_gateway/onboarding_web/assets/broker_card.mjs";
import "../assistant_gateway/onboarding_web/assets/chat_delivery.js";

const CHAT_JS = readFileSync(new URL("../assistant_gateway/onboarding_web/assets/chat.js", import.meta.url), "utf8");
const EXPIRE_FN = CHAT_JS.split("function replaceTransientActionWithExpiredState(countdown)")[1]
  .split("function updateTransientCountdown()")[0];
const ADD_FOLLOW_UP_FNS = CHAT_JS.split("function isReviewOf(requestId)")[1]
  .split("function showFollowUpPages(requestId)")[0];
const SET_BROKER_STATE_FN = CHAT_JS.split("function setBrokerCardState(thread, itemId, state)")[1]
  .split("function createTransientItemCard(thread, item)")[0];
const CREATE_FN = CHAT_JS.split("function createTransientItemCard(thread, item)")[1]
  .split("function renderMessages(options)")[0];

function transientHarness() {
  const document = fakeDocument();
  let renders = 0;
  const copied = [];
  const context = {
    document,
    lifecycle: { listen(node, event, handler) { node.addEventListener(event, handler); } },
    shellState: { expiredSecureFormMessage: "This secure form link has expired." },
    copyTextToClipboard: text => { copied.push(text); return Promise.resolve(); },
    renderMessages: () => { renders += 1; },
  };
  vm.runInNewContext(
    `function replaceTransientActionWithExpiredState(countdown)${EXPIRE_FN}\n`
    + `function createTransientItemCard(thread, item)${CREATE_FN}\n`
    + `this.createTransientItemCard = createTransientItemCard;\n`
    + `this.replaceTransientActionWithExpiredState = replaceTransientActionWithExpiredState;`,
    context,
  );
  return {
    document,
    createTransientItemCard: context.createTransientItemCard,
    replaceTransientActionWithExpiredState: context.replaceTransientActionWithExpiredState,
    copied,
    get renders() { return renders; },
  };
}

test("link transient card renders compact header with badge and countdown, primary open link, and ghost dismiss", () => {
  const h = transientHarness();
  const thread = { transientItems: [{ itemId: "t-1" }], dismissedTransientItems: new Set() };
  const card = h.createTransientItemCard(thread, {
    itemId: "t-1",
    createdAt: "2026-11-21T12:00:00Z",
    content: "Connect your Portainer account to finish setup.",
    label: "Open secure form",
    url: "https://broker.example/form/1",
    expiresAtUtc: "2026-11-21T12:15:00Z",
  });
  h.document.body.append(card);

  assert.ok(card.classList.contains("message-transient-notice"));
  assert.ok(card.classList.contains("is-link-card"));
  const header = byClass(card, "message-transient-header")[0];
  assert.equal(card.children[0], header);
  const badge = byClass(header, "message-transient-badge")[0];
  assert.equal(badge.textContent, "Action needed");
  const countdown = byClass(header, "message-transient-countdown")[0];
  assert.equal(countdown.getAttribute("data-expires-at"), "2026-11-21T12:15:00Z");
  assert.equal(countdown.getAttribute("role"), "status");

  assert.equal(byTag(card, "pre")[0].textContent, "Connect your Portainer account to finish setup.");
  const open = byClass(card, "message-transient-open")[0];
  assert.equal(open.tagName, "A");
  assert.equal(open.textContent, "Open secure form");
  assert.equal(open.href, "https://broker.example/form/1");
  assert.equal(open.target, "_blank");
  assert.equal(open.rel, "noopener noreferrer");

  const dismiss = byClass(card, "message-transient-dismiss")[0];
  assert.ok(dismiss.classList.contains("ghost") && dismiss.classList.contains("small"));

  // Expiring switches badge to Expired, removes link & countdown, and inserts status before Dismiss.
  h.replaceTransientActionWithExpiredState(countdown);
  assert.ok(card.classList.contains("is-expired"));
  assert.equal(badge.textContent, "Expired");
  assert.ok(badge.classList.contains("is-expired"));
  assert.equal(byClass(card, "message-transient-open").length, 0);
  assert.equal(byClass(card, "message-transient-countdown").length, 0);
  const expired = byClass(card, "message-transient-expired")[0];
  assert.equal(expired.getAttribute("role"), "status");
  assert.equal(expired.textContent, "This secure form link has expired.");
  const actions = byClass(card, "message-transient-actions")[0];
  assert.deepEqual(actions.children, [expired, dismiss]);

  dismiss.click();
  assert.equal(thread.dismissedTransientItems.has("t-1"), true);
  assert.equal(thread.transientItems.length, 0);
  assert.equal(h.renders, 1);
});

test("copy-only transient card keeps compact copy/dismiss actions without link header", () => {
  const h = transientHarness();
  const thread = { transientItems: [{ itemId: "t-2" }], dismissedTransientItems: new Set() };
  const card = h.createTransientItemCard(thread, {
    itemId: "t-2",
    content: "One-time code: 123456",
  });
  assert.ok(!card.classList.contains("is-link-card"));
  assert.equal(byClass(card, "message-transient-header").length, 0);
  const [copy, dismiss] = byTag(card, "button");
  assert.equal(copy.textContent, "Copy");
  assert.ok(dismiss.classList.contains("secondary") && dismiss.classList.contains("small"));
});

test("fake_dom matches throws on unsupported selectors instead of returning false", () => {
  const document = fakeDocument();
  const link = document.createElement("a");
  link.className = "alpha beta";
  link.id = "only";
  assert.equal(link.matches(".alpha"), true);
  assert.equal(link.matches("#only"), true);
  assert.equal(link.matches("a"), true);
  assert.throws(() => link.matches("a[href]"), /Unsupported selector/);
  assert.throws(() => link.matches(".alpha.beta"), /Unsupported selector/);
  assert.throws(() => link.matches(".alpha .beta"), /Unsupported selector/);
});

function followUpPagesHarness({ threads, active }) {
  let scheduledRenders = 0;
  const warnings = [];
  const context = {
    FOLLOW_UP_AFTER_REVIEW,
    sharedState: { threads },
    activeThread: () => active(),
    window: {
      UAChatDelivery: globalThis.UAChatDelivery,
      location: { origin: "http://127.0.0.1:18090" },
    },
    console: { warn: (...args) => warnings.push(args) },
    lifecycle: { timeout(fn) { fn(); } },
    renderMessages: () => { scheduledRenders += 1; },
  };
  vm.runInNewContext(
    `function isReviewOf(requestId)${ADD_FOLLOW_UP_FNS}\n`
    + `this.addFollowUpPages = addFollowUpPages;`,
    context,
  );
  return {
    addFollowUpPages: context.addFollowUpPages,
    warnings,
    get scheduledRenders() { return scheduledRenders; },
  };
}

test("addFollowUpPages guards duplicates by URL in the same thread and keeps stable ids across partial subsets", () => {
  const formA = {
    label: "Choose the credential for Portainer", url: "http://127.0.0.1:18600/account-link/token-a",
    frameUrl: "http://127.0.0.1:18600/account-link/token-a?view=card", expiresAtUtc: "2026-11-21T12:15:00Z",
  };
  const formB = {
    label: "Choose the credential for Gitea", url: "http://127.0.0.1:18600/account-link/token-b",
    frameUrl: "http://127.0.0.1:18600/account-link/token-b?view=card", expiresAtUtc: "2026-11-21T12:15:00Z",
  };
  const thread1 = {
    id: "t1",
    messages: [
      { messageId: "m1", sessionSequence: 3, accessRequestId: "req-1" },
      { messageId: "m2", sessionSequence: 8 },
    ],
    transientItems: [],
    brokerCardStates: new Map(),
  };
  const h = followUpPagesHarness({ threads: [thread1], active: () => thread1 });

  // 1) Inbox approves while t1 is open: both cards land at latest sequence (8).
  const shownInbox = h.addFollowUpPages("req-1", [formA, formB], FOLLOW_UP_AFTER_LATEST);
  assert.deepEqual(shownInbox, [formA, formB]);
  assert.equal(thread1.transientItems.length, 2);
  assert.equal(h.scheduledRenders, 1);
  const itemA = thread1.transientItems.find(item => item.url === formA.url);
  const itemB = thread1.transientItems.find(item => item.url === formB.url);
  assert.equal(itemA.sessionSequence, 8);
  assert.equal(itemB.sessionSequence, 8);

  // Form A finishes in its inline card.
  thread1.brokerCardStates.set(itemA.itemId, "finished");

  // 2) Chat card re-reads in t1 and hands off the remaining subset [formB] with FOLLOW_UP_AFTER_REVIEW:
  //    existing card B stays in place (position 8, no extra render), and B's itemId never collides with A's finished state.
  const shownChat = h.addFollowUpPages("req-1", [formB], FOLLOW_UP_AFTER_REVIEW);
  assert.deepEqual(shownChat, [formB]);
  assert.equal(thread1.transientItems.length, 2);
  assert.equal(h.scheduledRenders, 1, "no redraw or frame relocation when URL is already in the thread");
  assert.equal(thread1.transientItems.find(item => item.url === formB.url).sessionSequence, 8);
  assert.equal(thread1.brokerCardStates.get(itemB.itemId), undefined, "B does not inherit A's finished state");
});

test("addFollowUpPages scopes duplicate checks to the target thread (Inbox in T1, review card in T2)", () => {
  const formA = {
    label: "Choose the credential for Portainer", url: "http://127.0.0.1:18600/account-link/token-a",
    frameUrl: "http://127.0.0.1:18600/account-link/token-a?view=card", expiresAtUtc: "2026-11-21T12:15:00Z",
  };
  const formB = {
    label: "Choose the credential for Gitea", url: "http://127.0.0.1:18600/account-link/token-b",
    frameUrl: "http://127.0.0.1:18600/account-link/token-b?view=card", expiresAtUtc: "2026-11-21T12:15:00Z",
  };
  const thread1 = {
    id: "t1",
    messages: [{ messageId: "m1", sessionSequence: 5 }],
    transientItems: [],
  };
  const thread2 = {
    id: "t2",
    messages: [{ messageId: "m2", sessionSequence: 4, accessRequestId: "req-1" }],
    transientItems: [],
  };
  let current = thread1;
  const h = followUpPagesHarness({ threads: [thread1, thread2], active: () => current });

  // Inbox approves while T1 is open -> cards land in T1.
  assert.deepEqual(h.addFollowUpPages("req-1", [formA, formB], FOLLOW_UP_AFTER_LATEST), [formA, formB]);
  assert.equal(thread1.transientItems.length, 2);
  assert.equal(thread2.transientItems.length, 0);

  // Person switches to T2 where the review message lives; form A already finished so T2 hands off [formB].
  current = thread2;
  assert.deepEqual(h.addFollowUpPages("req-1", [formB], FOLLOW_UP_AFTER_REVIEW), [formB]);
  assert.equal(thread2.transientItems.length, 1);
  assert.equal(thread2.transientItems[0].url, formB.url);
  assert.equal(thread2.transientItems[0].sessionSequence, 4, "placed right after the review message in T2");
  assert.equal(
    thread2.transientItems[0].itemId,
    thread1.transientItems.find(item => item.url === formB.url).itemId,
    "same form gets the same stable itemId across threads",
  );
});

test("cross-thread broker card outcome and expiry close the form in every thread holding its itemId", () => {
  const document = fakeDocument();
  const messageListeners = [];
  const browser = {
    innerHeight: 800,
    console: { warn() {} },
    addEventListener(event, handler) {
      if (event === "message") messageListeners.push(handler);
    },
    removeEventListener() {},
  };
  let nextTimer = 1;
  const lifecycle = {
    listen(node, event, handler) { node.addEventListener(event, handler); },
    timeout(fn, delayMs) {
      if (delayMs === 0) fn();
      return nextTimer++;
    },
    clearTimeout() {},
  };
  const brokerCards = createBrokerCards({ browser, document, lifecycle });
  const formItem = {
    itemId: "account_link:req-1:0123456789abcdef",
    content: "Choose the credential for Portainer.",
    label: "Choose the credential for Portainer",
    url: "http://127.0.0.1:18600/account-link/token-a",
    frameUrl: "http://127.0.0.1:18600/account-link/token-a?view=card",
    expiresAtUtc: "2026-11-21T12:15:00Z",
  };
  const thread1 = { id: "t1", transientItems: [{ ...formItem }], brokerCardStates: new Map() };
  const thread2 = { id: "t2", transientItems: [{ ...formItem }], brokerCardStates: new Map() };
  const thread3 = { id: "t3", transientItems: [{ itemId: "account_link:req-2:fedcba9876543210" }], brokerCardStates: new Map() };
  let current = thread2;
  let renders = 0;

  const context = {
    document,
    brokerCards,
    brokerCardElement,
    lifecycle,
    sharedState: { threads: [thread1, thread2, thread3] },
    activeThread: () => current,
    shellState: { expiredSecureFormMessage: "This secure form link has expired." },
    copyTextToClipboard: () => Promise.resolve(),
    renderMessages: () => { renders += 1; },
  };
  vm.runInNewContext(
    `function replaceTransientActionWithExpiredState(countdown)${EXPIRE_FN}\n`
    + `function setBrokerCardState(thread, itemId, state)${SET_BROKER_STATE_FN}\n`
    + `function createTransientItemCard(thread, item)${CREATE_FN}\n`
    + `this.createTransientItemCard = createTransientItemCard;\n`
    + `this.replaceTransientActionWithExpiredState = replaceTransientActionWithExpiredState;`,
    context,
  );

  // 1) T2 renders first, opening the live frame whose onState closes over T2.
  const t2Card = context.createTransientItemCard(thread2, thread2.transientItems[0]);
  const frame = byTag(t2Card, "iframe")[0];
  const frameWindow = {};
  frame.contentWindow = frameWindow;
  assert.deepEqual(brokerCards.openItemIds(), [formItem.itemId]);

  // 2) Person switches back to T1; T1 reuses T2's live element from the shared registry.
  current = thread1;
  const t1Live = context.createTransientItemCard(thread1, thread1.transientItems[0]);
  assert.equal(t1Live, t2Card, "T1 reuses the live card element for the same itemId");

  // 3) Person finishes the form while viewing T1; the message fires T2's onState handler.
  messageListeners.forEach(fn => fn({ source: frameWindow, data: { type: CARD_MESSAGE_TYPE, outcome: BROKER_CARD_FINISHED } }));
  assert.equal(renders, 1);
  assert.equal(thread1.brokerCardStates.get(formItem.itemId), BROKER_CARD_FINISHED);
  assert.equal(thread2.brokerCardStates.get(formItem.itemId), BROKER_CARD_FINISHED);
  assert.equal(thread3.brokerCardStates.get(formItem.itemId), undefined, "unrelated thread is untouched");
  assert.deepEqual(brokerCards.openItemIds(), [], "live frame closed on outcome");

  // 4) Re-rendering T1 and T2 produces closed cards immediately with zero new frames opened.
  const t1Closed = context.createTransientItemCard(thread1, thread1.transientItems[0]);
  const t2Closed = context.createTransientItemCard(thread2, thread2.transientItems[0]);
  assert.ok(t1Closed.classList.contains("is-closed"));
  assert.ok(t2Closed.classList.contains("is-closed"));
  assert.deepEqual(brokerCards.openItemIds(), [], "neither thread reopens the used form");

  // 5) Expiry via replaceTransientActionWithExpiredState also updates every thread holding the itemId.
  const expItem = {
    itemId: "account_link:req-3:1111222233334444",
    content: "Choose the credential for Gitea.",
    label: "Choose the credential for Gitea",
    url: "http://127.0.0.1:18600/account-link/token-c",
    frameUrl: "http://127.0.0.1:18600/account-link/token-c?view=card",
    expiresAtUtc: "2026-11-21T12:15:00Z",
  };
  thread1.transientItems.push({ ...expItem });
  thread2.transientItems.push({ ...expItem });
  const expCard = context.createTransientItemCard(thread1, expItem);
  document.body.append(expCard);
  const expCountdown = byClass(expCard, "message-transient-countdown")[0];
  context.replaceTransientActionWithExpiredState(expCountdown);
  assert.equal(thread1.brokerCardStates.get(expItem.itemId), "expired");
  assert.equal(thread2.brokerCardStates.get(expItem.itemId), "expired");
  assert.equal(thread3.brokerCardStates.get(expItem.itemId), undefined);
});

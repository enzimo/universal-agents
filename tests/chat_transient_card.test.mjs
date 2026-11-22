import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

import { byClass, byTag, fakeDocument } from "./fake_dom.mjs";
import { FOLLOW_UP_AFTER_LATEST, FOLLOW_UP_AFTER_REVIEW } from "../assistant_gateway/onboarding_web/assets/access_inbox.mjs";
import {
  BROKER_CARD_CLOSED,
  BROKER_CARD_DECLINED,
  BROKER_CARD_EXPIRED,
  BROKER_CARD_FINISHED,
  BROKER_CARD_NOTES,
  BROKER_CARD_UNAVAILABLE,
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
const CLOSE_FOLLOW_UP_FNS = CHAT_JS.split("function isTerminalBrokerCardState(state)")[1]
  .split("function setBrokerCardState(thread, itemId, state)")[0];
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
    BROKER_CARD_EXPIRED,
    lifecycle,
    sharedState: {
      threads: [thread1, thread2, thread3],
      settingsControls: { syncFollowUpPages() {} },
    },
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

test("closeFollowUpPages uses neutral closed note before expiry, expired state past expiry, and preserves frame-reported states (C15)", () => {
  const document = fakeDocument();
  const browser = {
    innerHeight: 800,
    console: { warn() {} },
    addEventListener() {},
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
  const formA = {
    label: "Choose the credential for Portainer",
    url: "http://127.0.0.1:18600/account-link/token-a",
    frameUrl: "http://127.0.0.1:18600/account-link/token-a?view=card",
    expiresAtUtc: "2026-11-21T12:15:00Z",
  };
  const formB = {
    label: "Choose the credential for Gitea",
    url: "http://127.0.0.1:18600/account-link/token-b",
    frameUrl: "http://127.0.0.1:18600/account-link/token-b?view=card",
    expiresAtUtc: "2026-11-21T12:15:00Z",
  };
  const formC = {
    label: "Choose the credential for Forgejo",
    url: "http://127.0.0.1:18600/account-link/token-c",
    frameUrl: "http://127.0.0.1:18600/account-link/token-c?view=card",
    expiresAtUtc: "2026-11-21T12:15:00Z",
  };
  const formD = {
    label: "Choose the credential for GitHub",
    url: "http://127.0.0.1:18600/account-link/token-d",
    frameUrl: "http://127.0.0.1:18600/account-link/token-d?view=card",
    expiresAtUtc: "2026-11-21T12:10:00Z",
  };
  const formE = {
    label: "Choose the credential for Linear",
    url: "http://127.0.0.1:18600/account-link/token-e",
    frameUrl: "http://127.0.0.1:18600/account-link/token-e?view=card",
    expiresAtUtc: "2026-11-21T12:15:00Z",
  };
  const [pageA, pageB, pageC, pageD, pageE] = globalThis.UAChatDelivery.followUpPageItems(
    "req-1", 4, [formA, formB, formC, formD, formE], "http://127.0.0.1:18090",
  );
  const slashLinkItem = globalThis.UAChatDelivery.normalizeTransientItem({
    kind: "account_link",
    item_id: "account_link:cmd-1",
    correlation_id: "cmd-1",
    session_sequence: 5,
    content: "Link Portainer",
    action: { label: "Link", url: "http://127.0.0.1:18600/account-link/cmd", frame_url: "http://127.0.0.1:18600/account-link/cmd?view=card" },
  }, "http://127.0.0.1:18090");
  const thread1 = {
    id: "t1",
    transientItems: [{ ...pageA.item }, { ...pageB.item }, { ...pageC.item }, { ...pageE.item }, slashLinkItem],
    brokerCardStates: new Map([
      [pageB.item.itemId, BROKER_CARD_UNAVAILABLE],
      [pageC.item.itemId, BROKER_CARD_DECLINED],
      [pageE.item.itemId, BROKER_CARD_FINISHED],
    ]),
  };
  // Thread 2 holds Form A (shared with t1) and Form D (off-screen thread whose expiry passes before switching back).
  const thread2 = {
    id: "t2",
    transientItems: [{ ...pageA.item }, { ...pageC.item }, { ...pageD.item }],
    brokerCardStates: new Map([
      [pageC.item.itemId, BROKER_CARD_DECLINED],
    ]),
  };
  let renders = 0;
  let syncs = 0;
  const context = {
    document,
    brokerCards,
    brokerCardElement,
    BROKER_CARD_CLOSED,
    BROKER_CARD_DECLINED,
    BROKER_CARD_EXPIRED,
    BROKER_CARD_FINISHED,
    lifecycle,
    sharedState: {
      authSessionActive: true,
      threads: [thread1, thread2],
      settingsControls: { syncFollowUpPages: () => { syncs += 1; } },
    },
    activeThread: () => thread1,
    window: {
      UAChatDelivery: globalThis.UAChatDelivery,
      location: { origin: "http://127.0.0.1:18090" },
    },
    shellState: { expiredSecureFormMessage: "This secure form link has expired." },
    copyTextToClipboard: () => Promise.resolve(),
    renderMessages: () => { renders += 1; },
  };
  vm.runInNewContext(
    `function isTerminalBrokerCardState(state)${CLOSE_FOLLOW_UP_FNS}\n`
    + `function setBrokerCardState(thread, itemId, state)${SET_BROKER_STATE_FN}\n`
    + `function createTransientItemCard(thread, item)${CREATE_FN}\n`
    + `this.openFollowUpPages = openFollowUpPages;\n`
    + `this.closeFollowUpPages = closeFollowUpPages;\n`
    + `this.createTransientItemCard = createTransientItemCard;`,
    context,
  );

  // openFollowUpPages lists only non-terminal proposal follow-up cards (deduplicated across threads, excluding /link).
  assert.deepEqual(
    Array.from(context.openFollowUpPages(), page => `${page.requestId}|${page.url}`).sort(),
    [`req-1|${formA.url}`, `req-1|${formB.url}`, `req-1|${formD.url}`].sort(),
  );

  // Open Form A as a live iframe in thread1.
  const liveCardA = context.createTransientItemCard(thread1, thread1.transientItems[0]);
  assert.equal(byTag(liveCardA, "iframe").length, 1);
  assert.deepEqual(brokerCards.openItemIds(), [pageA.item.itemId]);

  // 1) Before expiry (12:12): external ending closes Form A and unavailable Form B with neutral BROKER_CARD_CLOSED,
  //    while Form D (expired at 12:10 in off-screen thread2) closes as BROKER_CARD_EXPIRED.
  const nowAt1212 = Date.parse("2026-11-21T12:12:00Z");
  context.closeFollowUpPages("req-1", [formA.url, formB.url, formD.url], nowAt1212);
  assert.deepEqual(brokerCards.openItemIds(), [], "live iframe closed immediately");
  assert.equal(thread1.brokerCardStates.get(pageA.item.itemId), BROKER_CARD_CLOSED);
  assert.equal(thread2.brokerCardStates.get(pageA.item.itemId), BROKER_CARD_CLOSED);
  assert.equal(thread1.brokerCardStates.get(pageB.item.itemId), BROKER_CARD_CLOSED,
    "unavailable fallback card before expiry closes with neutral closed state");
  assert.equal(thread2.brokerCardStates.get(pageD.item.itemId), BROKER_CARD_EXPIRED,
    "card past its expiresAtUtc closes as expired even when off-screen");
  assert.equal(renders, 1);
  assert.ok(syncs >= 1, "closing cards syncs the follow-up watcher");
  assert.equal(context.openFollowUpPages().length, 0, "all proposal cards are now terminal");

  // Re-rendering Form A shows the neutral closed note (never "Done."), and Form D shows the expired message.
  const closedA = context.createTransientItemCard(thread1, thread1.transientItems[0]);
  assert.ok(closedA.classList.contains("is-closed"));
  assert.equal(byTag(closedA, "iframe").length, 0);
  assert.equal(closedA.textContent, BROKER_CARD_NOTES[BROKER_CARD_CLOSED]);
  assert.equal(closedA.textContent, "This secure page is closed.");

  const expiredD = context.createTransientItemCard(thread2, thread2.transientItems[2]);
  assert.ok(expiredD.classList.contains("is-closed"));
  assert.equal(expiredD.textContent, "This secure form link has expired.");

  // 2) Re-calling for already-closed, frame-reported finished, or declined forms preserves state with no extra render.
  context.closeFollowUpPages("req-1", [formA.url, formC.url, formE.url], nowAt1212);
  assert.equal(thread1.brokerCardStates.get(pageC.item.itemId), BROKER_CARD_DECLINED,
    "existing declined state is preserved");
  assert.equal(thread2.brokerCardStates.get(pageC.item.itemId), BROKER_CARD_DECLINED);
  assert.equal(thread1.brokerCardStates.get(pageE.item.itemId), BROKER_CARD_FINISHED,
    "frame-reported finished state keeps Done. wording");
  assert.equal(renders, 1, "no redundant render when cards are already in terminal states");

  // 3) Signed out: openFollowUpPages returns empty even if a non-terminal item exists.
  thread1.brokerCardStates.delete(pageA.item.itemId);
  thread2.brokerCardStates.delete(pageA.item.itemId);
  assert.equal(context.openFollowUpPages().length, 1);
  context.sharedState.authSessionActive = false;
  assert.equal(context.openFollowUpPages().length, 0);
});

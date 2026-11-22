import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

import { byClass, byTag, fakeDocument } from "./fake_dom.mjs";

const CHAT_JS = readFileSync(new URL("../assistant_gateway/onboarding_web/assets/chat.js", import.meta.url), "utf8");
const EXPIRE_FN = CHAT_JS.split("function replaceTransientActionWithExpiredState(countdown)")[1]
  .split("function updateTransientCountdown()")[0];
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

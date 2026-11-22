import assert from "node:assert/strict";
import test from "node:test";
import "../assistant_gateway/onboarding_web/assets/chat_delivery.js";

const delivery = globalThis.UAChatDelivery;

// The /link card as normalizeTransientItem builds it from the chat response.
function linkCard(kind, correlationId, sessionSequence) {
  return delivery.normalizeTransientItem(
    {
      kind,
      item_id: `${kind}:${correlationId}`,
      correlation_id: correlationId,
      session_sequence: sessionSequence,
      created_at_utc: "2026-09-27T13:32:00Z",
      content: "Link an account to mcp:crawl4ai (the team's account).",
      action: {
        label: "Open account link form",
        url: "https://broker.example.test/account-link/bearer",
        expires_at_utc: "2026-09-27T13:47:00Z",
      },
    },
    "https://agents.example.test",
  );
}

const message = (fields) => ({ optimistic: false, ...fields });

test("the gateway's /link result removes the account link card it names", () => {
  const card = linkCard("account_link", "req_link", 4);
  assert.ok(card, "the card is valid");

  const stillOpen = delivery.reconcileTransientItems([card], [
    message({ correlationId: "req_other", messageType: "link_result", sessionSequence: 5 }),
    message({ correlationId: "req_link", messageType: "task", sessionSequence: 6 }),
    message({ correlationId: "req_link", messageType: "command_account_link", sessionSequence: 4 }),
  ]);
  assert.deepEqual(stillOpen.map((item) => item.itemId), ["account_link:req_link"]);

  const finished = delivery.reconcileTransientItems(stillOpen, [
    message({ correlationId: "req_link", messageType: delivery.LINK_RESULT_MESSAGE_TYPE, sessionSequence: 7 }),
  ]);
  assert.deepEqual(finished, []);
});

test("a /link sign-in card is removed by its result too", () => {
  const card = linkCard("broker_authentication", "req_sign_in", 2);
  const finished = delivery.reconcileTransientItems([card], [
    message({ correlationId: "req_sign_in", messageType: "link_result", sessionSequence: 3 }),
  ]);
  assert.deepEqual(finished, []);
});

test("a result older than the card, or not yet saved, leaves the card", () => {
  const card = linkCard("account_link", "req_link", 9);
  const kept = delivery.reconcileTransientItems([card], [
    message({ correlationId: "req_link", messageType: "link_result", sessionSequence: 8 }),
    { optimistic: true, correlationId: "req_link", messageType: "link_result", sessionSequence: 10 },
  ]);
  assert.deepEqual(kept.map((item) => item.itemId), ["account_link:req_link"]);
});

test("after a reload the saved result is shown and no card comes back", () => {
  // A reload rebuilds the thread from history only: the /link card lived in
  // the page's memory, so the history's result line is all that is left, and
  // reconciling history never recreates a card.
  const history = [
    message({ correlationId: "req_link", messageType: "command_account_link", sessionSequence: 4 }),
    message({ correlationId: "req_link", messageType: "link_result", sessionSequence: 5 }),
  ];
  assert.deepEqual(delivery.reconcileTransientItems([], history), []);
  const timeline = delivery.buildTimeline(history, []);
  assert.deepEqual(timeline.map((entry) => entry.value.messageType), ["command_account_link", "link_result"]);
});

test("forms an agent task waits on still close only on the task's answer", () => {
  const card = linkCard("tool_setup", "req_task", 2);
  const kept = delivery.reconcileTransientItems([card], [
    message({ correlationId: "req_task", messageType: "link_result", sessionSequence: 3 }),
  ]);
  assert.equal(kept.length, 1);
  const finished = delivery.reconcileTransientItems(kept, [
    message({ correlationId: "req_task", messageType: "task", sessionSequence: 4 }),
  ]);
  assert.deepEqual(finished, []);
});

// A setup page a set_up_tools run opened: several per task share the chat
// request correlation, so each card is finished by its own page id.
function setupCard(pageId, correlationId, sessionSequence) {
  return delivery.normalizeTransientItem(
    {
      kind: "tool_setup",
      item_id: `tool_setup:${pageId}`,
      request_id: pageId,
      correlation_id: correlationId,
      session_sequence: sessionSequence,
      created_at_utc: "2026-09-28T08:05:56Z",
      content: "Sign in to GitHub so I can list your private repos.",
      action: {
        label: "Sign in to GitHub",
        url: "https://broker.example.test/auth/github/x",
        expires_at_utc: "2026-09-28T08:20:56Z",
      },
    },
    "https://agents.example.test",
  );
}

test("a setup card closes as soon as its page ends, not on another page's result", () => {
  const github = setupCard("tool_setup_req_github", "req_chat", 3);
  const google = setupCard("tool_setup_req_google", "req_chat", 4);
  assert.ok(github && google, "both cards are valid");

  const afterGithub = delivery.reconcileTransientItems([github, google], [
    message({ correlationId: "tool_setup_req_github", messageType: delivery.TOOL_SETUP_RESULT_MESSAGE_TYPE, sessionSequence: 6 }),
    message({ correlationId: "req_chat", messageType: "access_review", sessionSequence: 5 }),
  ]);

  assert.deepEqual(afterGithub.map((item) => item.itemId), ["tool_setup:tool_setup_req_google"]);
});

test("a re-offered page's card is not removed by the expired page's result", () => {
  const offeredAgain = setupCard("tool_setup_req_second", "req_chat", 8);
  const kept = delivery.reconcileTransientItems([offeredAgain], [
    message({ correlationId: "tool_setup_req_first", messageType: "tool_setup_result", sessionSequence: 7 }),
    message({ correlationId: "tool_setup_req_first", messageType: "tool_setup_result", sessionSequence: 9 }),
  ]);
  assert.equal(kept.length, 1);
});

test("the task's final answer still closes every setup card of its chat request", () => {
  const card = setupCard("tool_setup_req_github", "req_chat", 3);
  assert.deepEqual(delivery.reconcileTransientItems([card], [
    message({ correlationId: "req_chat", messageType: "task", sessionSequence: 10 }),
  ]), []);
});

// The secure credential form card, as the chat response carries it: its own id
// is the credential request id.
function credentialCard(requestId, correlationId, sessionSequence) {
  return delivery.normalizeTransientItem(
    {
      kind: "secure_credential_collection",
      item_id: `secure_credential_collection:${requestId}`,
      request_id: requestId,
      correlation_id: correlationId,
      session_sequence: sessionSequence,
      created_at_utc: "2026-10-11T19:05:00Z",
      content: "Secure credential collection is ready for `bifrost_virtual_key`.",
      action: {
        label: "Open secure credential form",
        url: "https://broker.example.test/credential-collection/session_123",
        expires_at_utc: "2026-10-11T19:20:00Z",
      },
    },
    "https://agents.example.test",
  );
}

test("a credential form card closes when its form ends, while the agent still works", () => {
  const card = credentialCard("cred_req_1", "req_chat", 3);
  assert.ok(card, "the card is valid");
  const stillOpen = delivery.reconcileTransientItems([card], [
    // Another form's line, and the agent's progress, leave it.
    message({ correlationId: "cred_req_other", messageType: delivery.SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE, sessionSequence: 4 }),
    message({ correlationId: "req_chat", messageType: "task_progress_status", sessionSequence: 5 }),
  ]);
  assert.equal(stillOpen.length, 1);
  const finished = delivery.reconcileTransientItems(stillOpen, [
    message({ correlationId: "cred_req_1", messageType: "secure_credential_result", sessionSequence: 6 }),
  ]);
  assert.deepEqual(finished, []);
});

test("the task's final answer still closes a credential form card", () => {
  const card = credentialCard("cred_req_1", "req_chat", 3);
  assert.deepEqual(delivery.reconcileTransientItems([card], [
    message({ correlationId: "req_chat", messageType: "task", sessionSequence: 7 }),
  ]), []);
});

// A broker page's card view (frame_url) is kept only when it is the same page as its link.
function brokerItem(frameUrl) {
  return delivery.normalizeTransientItem(
    {
      kind: "tool_setup",
      item_id: "tool_setup:req_page",
      request_id: "req_page",
      correlation_id: "corr_page",
      session_sequence: 3,
      created_at_utc: "2026-11-19T13:32:00Z",
      content: "Choose your account for Portainer.",
      action: {
        label: "Choose your account for Portainer",
        url: "https://broker.example.test/account-link/bearer",
        expires_at_utc: "2026-11-19T13:47:00Z",
        frame_url: frameUrl,
      },
    },
    "https://agents.example.test",
  );
}

test("a broker page's card view is kept only when it is the same page", () => {
  assert.equal(
    brokerItem("https://broker.example.test/account-link/bearer?view=card").frameUrl,
    "https://broker.example.test/account-link/bearer?view=card",
  );
  for (const other of [
    "https://evil.example.test/account-link/bearer?view=card",
    "https://broker.example.test/account-link/other?view=card",
    "javascript:alert(1)",
    undefined,
  ]) {
    const item = brokerItem(other);
    assert.ok(item, "the card itself stays");
    assert.equal(item.frameUrl, undefined, `${other} must not be framed`);
    assert.equal(item.url, "https://broker.example.test/account-link/bearer");
  }
});

// An approved proposal's credential forms as broker cards (review finding F2).
test("an approval's credential forms get stable URL-derived account link card ids right after the review, http included", () => {
  const portainer = {
    label: "Choose the credential for Portainer", url: "http://127.0.0.1:18600/account-link/secret-token-a",
    frameUrl: "http://127.0.0.1:18600/account-link/secret-token-a?view=card", expiresAtUtc: "2026-11-20T10:15:00Z",
  };
  const gitea = {
    label: "Choose the credential for Gitea", url: "http://127.0.0.1:18600/account-link/secret-token-b",
    frameUrl: "http://127.0.0.1:18600/account-link/secret-token-b?view=card", expiresAtUtc: "2026-11-20T10:15:00Z",
  };
  const forms = [
    portainer,
    // A frame address on another page is not shown as a card; neither is a page without one.
    { label: "Choose the credential for Evil", url: "https://broker.example.test/account-link/b",
      frameUrl: "https://evil.example.test/account-link/b?view=card" },
    { label: "Choose the credential for Jira", url: "https://broker.example.test/account-link/c" },
    { label: "Not a page", url: "javascript:alert(1)", frameUrl: "javascript:alert(1)" },
    gitea,
  ];
  const pages = delivery.followUpPageItems("acc_req_1", 7, forms, "http://127.0.0.1:18090");

  assert.equal(pages.length, 2);
  const [{ item: firstItem, link: firstLink }, { item: secondItem, link: secondLink }] = pages;
  assert.equal(firstLink, portainer);
  assert.equal(secondLink, gitea);
  assert.match(firstItem.itemId, /^account_link:acc_req_1:[0-9a-f]{16}$/);
  assert.match(secondItem.itemId, /^account_link:acc_req_1:[0-9a-f]{16}$/);
  assert.notEqual(firstItem.itemId, secondItem.itemId);
  assert.ok(!firstItem.itemId.includes("secret-token-a"), "itemId must not expose the form URL token");
  assert.equal(firstItem.kind, "account_link");
  assert.equal(firstItem.sessionSequence, 7);
  assert.equal(firstItem.correlationId, "acc_req_1");
  assert.equal(firstItem.url, portainer.url);
  assert.equal(firstItem.frameUrl, portainer.frameUrl);
  assert.equal(firstItem.expiresAtUtc, portainer.expiresAtUtc);
  assert.equal(firstItem.content, "Choose the credential for Portainer");

  // Dropping the first form after it finishes keeps the remaining form's exact itemId.
  const remaining = delivery.followUpPageItems("acc_req_1", 7, [gitea], "http://127.0.0.1:18090");
  assert.equal(remaining.length, 1);
  assert.equal(remaining[0].item.itemId, secondItem.itemId);

  // Placed after the review message (sequence 7) and before anything later.
  const timeline = delivery.buildTimeline(
    [message({ messageId: "m7", sessionSequence: 7 }), message({ messageId: "m8", sessionSequence: 8 })], [firstItem],
  );
  assert.deepEqual(timeline.map(entry => entry.value.messageId || entry.value.itemId), ["m7", firstItem.itemId, "m8"]);
  assert.deepEqual(delivery.followUpPageItems("", 7, forms, "http://127.0.0.1:18090"), []);
  // An Inbox approval while the open conversation has no messages yet (critique C13): the card starts it.
  const [first] = delivery.followUpPageItems("acc_req_1", 0, [portainer], "http://127.0.0.1:18090");
  assert.equal(first.item.sessionSequence, 1);

  // isFollowUpPageItem identifies proposal follow-up items and excludes /link account_link items.
  assert.equal(delivery.isFollowUpPageItem(firstItem), true);
  const slashLinkItem = delivery.normalizeTransientItem({
    kind: "account_link",
    item_id: "account_link:cmd-1",
    correlation_id: "cmd-1",
    session_sequence: 3,
    content: "Link Portainer",
    action: { label: "Link", url: portainer.url, frame_url: portainer.frameUrl },
  }, "http://127.0.0.1:18090");
  assert.equal(delivery.isFollowUpPageItem(slashLinkItem), false);
});

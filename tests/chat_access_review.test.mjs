import assert from "node:assert/strict";
import test from "node:test";
import {
  DETAILS_LABEL, NEEDS_DECISION, WAITING_FOR_ADMINISTRATOR, decisionRequestBody, localTimeFormatter,
  storedDecisionOutcome,
} from "../assistant_gateway/onboarding_web/assets/access_inbox.mjs";
import {
  LOADING_TEXT, OPEN_INBOX_LABEL, REFRESH_WAITING_AFTER_MS, createChatAccessReviews, isAccessReviewMessage, reviewPath,
} from "../assistant_gateway/onboarding_web/assets/chat_access_review.mjs";
import { byClass, byTag, buttonLabels, fakeDocument, settle } from "./fake_dom.mjs";

const formatTime = localTimeFormatter({ locale: "en-US", timeZone: "UTC" });
const SUMMARY = "TeamArchitect asks for approval: Switch TeamArchitect to deepseek/deepseek-v4.1-flash via "
  + "openrouter (images on). Takes effect now.";

function changeReview(overrides = {}) {
  return {
    request_id: "req-1", kind: "change", change_kind: "runtime_settings",
    title: "TeamArchitect asks to make a change: Change runtime settings: current_agent.llm.provider",
    summary: SUMMARY,
    effect: ["LLM provider override ...: current_agent.llm.provider for TeamArchitect changes from 'litellm' to 'openrouter'."],
    rows: [], agent_type: "TeamArchitect", for_whose_work: "your work", reason: "The person asked for a faster model.",
    status: "waiting", created_at_utc: "2026-09-25T15:04:00+00:00", decision: null, decided_by_name: null,
    decided_at_utc: null, can_decide: true,
    choices: [{ choice: "approve", label: "Approve" }, { choice: "deny", label: "Deny" }],
    ...overrides,
  };
}

function accessReview(overrides = {}) {
  return changeReview({
    kind: "access", change_kind: null, title: "Coder asks for full access to Unconfined code",
    summary: "Coder asks for full access to Unconfined code (for this task).", effect: [],
    rows: [{ item_id: "access", section: "access", text: "Coder: full access to Unconfined code",
      flags: ["full_access", "runs_code"], keepable: true }],
    choices: [{ choice: "approve", label: "Approve" }, { choice: "approve_read", label: "Approve reading only" },
      { choice: "deny", label: "Deny" }],
    ...overrides,
  });
}

function message(id = "req-1") {
  return { role: "system", messageType: "access_review", accessRequestId: id, content: "announcement" };
}

function harness({ reviews = { "req-1": changeReview() }, decide, readError } = {}) {
  const document = fakeDocument();
  const calls = { gets: [], posts: [], opened: [], decided: [] };
  let clock = 1_000_000;
  const reviews_ = { ...reviews };
  const reviewer = createChatAccessReviews({
    browser: { document, console: { warn() {} } },
    formatTime,
    now: () => clock,
    get: path => {
      calls.gets.push(path);
      if (readError) return Promise.reject(readError);
      const id = decodeURIComponent(path.split("/").at(-1));
      return Promise.resolve({ status: "ok", review: reviews_[id] });
    },
    post: (path, body) => {
      calls.posts.push({ path, body });
      return decide ? decide(path, body) : Promise.resolve({ status: "ok", review: { ...reviews_["req-1"], status: "granted" } });
    },
    beginAction: (button, _busyLabel, group) => {
      byTag(group, "button").forEach(item => { item.disabled = true; });
      return () => byTag(group, "button").forEach(item => { item.disabled = false; });
    },
    failureMessage: (error, fallback) => (error && error.message) || fallback,
    openInbox: id => calls.opened.push(id),
    onDecided: id => calls.decided.push(id),
  });
  const bubble = document.createElement("article");
  document.body.append(bubble);
  return { document, reviewer, bubble, calls, reviews: reviews_, advance: ms => { clock += ms; } };
}

test("only system access review messages with a request id get a card", () => {
  assert.equal(isAccessReviewMessage(message()), true);
  assert.equal(isAccessReviewMessage({ ...message(), role: "assistant" }), false);
  assert.equal(isAccessReviewMessage({ ...message(), accessRequestId: "" }), false);
  assert.equal(isAccessReviewMessage({ ...message(), messageType: "system_notification" }), false);
  assert.equal(reviewPath("a/b"), "/api/web-chat/access-inbox/requests/a%2Fb");
});

test("the card shows the one-line summary and decision buttons, with the rest folded under Details", async () => {
  const { reviewer, bubble, calls } = harness();
  const slot = reviewer.mount(bubble, message());
  assert.equal(slot.textContent, LOADING_TEXT);
  await settle();
  assert.deepEqual(calls.gets, ["/api/web-chat/access-inbox/requests/req-1"]);
  const card = byClass(slot, "access-review-card")[0];
  assert.ok(card.classList.contains("is-compact"));
  assert.equal(byClass(card, "permission-operation")[0].textContent, SUMMARY);
  assert.equal(byClass(card, "access-inbox-status")[0].textContent, NEEDS_DECISION);
  assert.deepEqual(buttonLabels(card), ["Approve", "Deny"]);
  const fold = byTag(card, "details")[0];
  assert.equal(byTag(fold, "summary")[0].textContent, DETAILS_LABEL);
  assert.equal(fold.open, false);
  // The full title, the effect list, the reason and the note are inside Details, not beside the buttons.
  assert.match(fold.textContent, /Change runtime settings/);
  assert.match(fold.textContent, /changes from 'litellm' to 'openrouter'/);
  assert.match(fold.textContent, /The person asked for a faster model/);
  assert.equal(byTag(fold, "textarea").length, 1);
  assert.equal(byTag(fold, "button").length, 0);
});

test("warning chips stay visible next to the summary; the rows and Keep box fold away", async () => {
  const { reviewer, bubble } = harness({ reviews: { "req-1": accessReview() } });
  const slot = reviewer.mount(bubble, message());
  await settle();
  const flags = byClass(slot, "access-review-flags")[0];
  assert.deepEqual(byClass(flags, "access-review-flag").map(chip => chip.textContent), ["Full access", "Runs code"]);
  assert.ok(!byTag(slot, "details")[0].contains(flags));
  assert.equal(byClass(byTag(slot, "details")[0], "access-review-keep-box").length, 1);
});

test("a decision goes to the Inbox decide API with the chosen key and shows the outcome", async () => {
  const { reviewer, bubble, calls } = harness();
  const slot = reviewer.mount(bubble, message());
  await settle();
  const note = byTag(slot, "textarea")[0];
  note.value = "Fine for now.";
  byTag(slot, "button").find(button => button.textContent === "Approve").click();
  await settle();
  assert.deepEqual(calls.posts, [{
    path: "/api/web-chat/access-inbox/requests/req-1/decision", body: { choice: "approve", note: "Fine for now." },
  }]);
  const decided = byClass(slot, "access-review-card")[0];
  assert.ok(decided.classList.contains("is-decided"));
  assert.equal(byClass(decided, "permission-operation")[0].textContent, SUMMARY);
  assert.equal(byClass(decided, "access-inbox-status")[0].textContent, "Approved");
  assert.equal(byTag(decided, "button").length, 0);
  assert.deepEqual(calls.decided, ["req-1"]);
  // A re-render shows the saved outcome without reading the request again.
  const again = reviewer.mount(bubble, message());
  await settle();
  assert.equal(calls.gets.length, 1);
  assert.equal(byClass(again, "access-inbox-status")[0].textContent, "Approved");
});

test("after a reload the card shows the stored decision and who made it, without buttons", async () => {
  const stored = changeReview({ status: "granted", decision: "approve_task", decided_by_name: "Dana",
    decided_at_utc: "2026-09-25T16:00:00+00:00", can_decide: false, choices: [] });
  const { reviewer, bubble } = harness({ reviews: { "req-1": stored } });
  const slot = reviewer.mount(bubble, message());
  await settle();
  assert.equal(byClass(slot, "access-inbox-status")[0].textContent, "Approved for this task");
  assert.match(slot.textContent, /Decided by Dana on Sep 25, 2026/);
  assert.equal(byTag(slot, "button").length, 0);
});

test("a person who may not decide sees that it waits for an administrator, with no buttons", async () => {
  const { reviewer, bubble } = harness({ reviews: { "req-1": changeReview({ can_decide: false, choices: [] }) } });
  const slot = reviewer.mount(bubble, message());
  await settle();
  assert.equal(byClass(slot, "access-inbox-status")[0].textContent, WAITING_FOR_ADMINISTRATOR);
  assert.equal(byTag(slot, "button").length, 0);
  assert.equal(byTag(slot, "textarea").length, 0);
});

test("a waiting request is read again only after a while; a decision elsewhere then shows", async () => {
  const { reviewer, bubble, calls, reviews, advance } = harness();
  reviewer.mount(bubble, message());
  await settle();
  reviewer.mount(bubble, message());
  await settle();
  assert.equal(calls.gets.length, 1);
  reviews["req-1"] = changeReview({ status: "denied", decision: "deny", decided_by_name: "Sam", can_decide: false, choices: [] });
  advance(REFRESH_WAITING_AFTER_MS);
  const slot = reviewer.mount(bubble, message());
  await settle();
  assert.equal(calls.gets.length, 2);
  assert.equal(byClass(slot, "access-inbox-status")[0].textContent, "Denied");
  advance(REFRESH_WAITING_AFTER_MS * 4);
  reviewer.mount(bubble, message());
  await settle();
  assert.equal(calls.gets.length, 2, "a decided request is never read again");
});

test("a refused decision shows the reason, and a decision made elsewhere replaces the buttons", async () => {
  let harnessReviews;
  const { reviewer, bubble, reviews } = harness({
    decide: () => {
      harnessReviews["req-1"] = changeReview({ status: "granted", decision: "approve", decided_by_name: "Sam",
        can_decide: false, choices: [] });
      return Promise.reject(new Error("This request was already decided."));
    },
  });
  harnessReviews = reviews;
  const slot = reviewer.mount(bubble, message());
  await settle();
  const card = byClass(slot, "access-review-card")[0];
  const messageLine = byClass(card, "access-inbox-card-message")[0];
  byTag(slot, "button")[0].click();
  await settle();
  assert.equal(messageLine.textContent, "This request was already decided.");
  assert.equal(byClass(slot, "access-inbox-status")[0].textContent, "Approved");
  assert.match(slot.textContent, /Decided by Sam/);
});

test("a request that cannot be read says so and offers the Inbox", async () => {
  const { reviewer, bubble, calls } = harness({ readError: new Error("This request is not available to you.") });
  const slot = reviewer.mount(bubble, message());
  await settle();
  assert.match(slot.textContent, /not available to you/);
  const open = byTag(slot, "button")[0];
  assert.equal(open.textContent, OPEN_INBOX_LABEL);
  open.click();
  assert.deepEqual(calls.opened, ["req-1"]);
});

test("sign-out forgets requests and ignores a reply to an earlier read", async () => {
  const { reviewer, bubble, calls } = harness();
  const slot = reviewer.mount(bubble, message());
  reviewer.clear();
  await settle();
  assert.equal(slot.textContent, LOADING_TEXT);
  reviewer.mount(bubble, message());
  await settle();
  assert.equal(calls.gets.length, 2);
});

test("stored outcomes name the decision; a waiting request has none", () => {
  assert.equal(storedDecisionOutcome(changeReview()), null);
  assert.deepEqual(storedDecisionOutcome(changeReview({ status: "granted", decision: "approve_read" }), formatTime),
    { label: "Approved reading only", tone: "granted", explanation: "Decided." });
  assert.equal(storedDecisionOutcome(changeReview({ status: "withdrawn" })).label, "Withdrawn");
  assert.match(storedDecisionOutcome(changeReview({ status: "denied", decided_by_name: "Ana" })).explanation,
    /^Decided by Ana\. No access was given\.$/);
});

test("the decision body is shared with the Inbox: keep for access requests, kept rows for proposals", () => {
  const box = (checked, id = "a") => ({ checked, getAttribute: () => id });
  const choice = { choice: "approve" };
  assert.deepEqual(decisionRequestBody({ review: accessReview(), choice, note: { value: " " }, keepBoxes: [box(true)],
    keepSelects: [{ value: "2" }] }), { body: { choice: "approve", keep: true, keep_hours: 168 }, kept: true });
  assert.deepEqual(decisionRequestBody({ review: changeReview(), choice, note: { value: "ok" },
    keepBoxes: [box(true, "r1"), box(false, "r2")], handOnBoxes: [box(true)] }).body,
  { choice: "approve", note: "ok", kept_item_ids: ["r1"], hand_on: true });
});

import assert from "node:assert/strict";
import test from "node:test";
import {
  DETAILS_LABEL, FOLLOW_UP_AFTER_REVIEW, FOLLOW_UP_EXPIRY_GRACE_MS, FOLLOW_UP_PAGES_BELOW, FOLLOW_UP_POLL_MS,
  FOLLOW_UP_SETTLING_TEXT, NEEDS_DECISION, WAITING_FOR_ADMINISTRATOR, decisionRequestBody, localTimeFormatter,
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

function harness({ reviews = { "req-1": changeReview() }, decide, readError, onFollowUpPages, startClock = 1_000_000 } = {}) {
  const document = fakeDocument();
  document.hidden = false;
  const docListeners = new Map();
  document.addEventListener = (type, fn) => {
    if (!docListeners.has(type)) docListeners.set(type, []);
    docListeners.get(type).push(fn);
  };
  const winListeners = new Map();
  const intervals = new Map();
  let nextTimerId = 0;
  const calls = { gets: [], posts: [], opened: [], decided: [] };
  let clock = startClock;
  const reviews_ = { ...reviews };
  let currentReadError = readError;
  const browser = {
    document,
    console: { warn() {} },
    addEventListener(type, fn) {
      if (!winListeners.has(type)) winListeners.set(type, []);
      winListeners.get(type).push(fn);
    },
    setInterval(callback, delay) {
      nextTimerId += 1;
      intervals.set(nextTimerId, { callback, delay });
      return nextTimerId;
    },
    clearInterval(id) { intervals.delete(id); },
  };
  const reviewer = createChatAccessReviews({
    browser,
    formatTime,
    now: () => clock,
    get: path => {
      calls.gets.push(path);
      if (currentReadError) return Promise.reject(currentReadError);
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
    onFollowUpPages,
  });
  const bubble = document.createElement("article");
  document.body.append(bubble);
  return {
    document, browser, reviewer, bubble, calls, reviews: reviews_, intervals,
    advance: ms => { clock += ms; },
    setReadError: err => { currentReadError = err; },
    fireDoc: type => (docListeners.get(type) || []).forEach(fn => fn()),
    fireWin: type => (winListeners.get(type) || []).forEach(fn => fn()),
  };
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

// An approved proposal's credential forms (review finding F2).
const FORMS = [{ label: "Choose the credential for Portainer", url: "http://127.0.0.1:18600/account-link/a",
  frame_url: "http://127.0.0.1:18600/account-link/a?view=card", expires_at_utc: "2026-11-20T10:15:00Z" }];
const approveWithForms = () => Promise.resolve({ status: "ok", review: { ...changeReview(), status: "granted", follow_up_links: FORMS } });

test("an approval's credential forms become broker cards in the conversation, not new-tab links", async () => {
  const handed = [];
  const { reviewer, bubble } = harness({
    decide: approveWithForms,
    onFollowUpPages: (id, links, placement) => { handed.push({ id, links, placement }); return links; },
  });
  const slot = reviewer.mount(bubble, message());
  await settle();
  byTag(slot, "button").find(button => button.textContent === "Approve").click();
  await settle();

  assert.equal(handed.length, 1);
  assert.equal(handed[0].id, "req-1");
  assert.equal(handed[0].links[0].frameUrl, FORMS[0].frame_url);
  assert.equal(handed[0].placement, FOLLOW_UP_AFTER_REVIEW, "right below the review card");
  const decided = byClass(slot, "access-review-card")[0];
  assert.equal(byTag(decided, "a").length, 0);
  assert.equal(byClass(decided, "access-review-follow-up-note")[0].textContent, FOLLOW_UP_PAGES_BELOW);
});

test("forms the conversation cannot show as cards stay links", async () => {
  const { reviewer, bubble } = harness({ decide: approveWithForms, onFollowUpPages: () => [] });
  const slot = reviewer.mount(bubble, message());
  await settle();
  byTag(slot, "button").find(button => button.textContent === "Approve").click();
  await settle();

  const decided = byClass(slot, "access-review-card")[0];
  assert.deepEqual(byTag(decided, "a").map(anchor => anchor.getAttribute("href")), [FORMS[0].url]);
  assert.equal(byClass(decided, "access-review-follow-up-note").length, 0);
});

test("compact card combines status and flags in one row, folds choice help in Details before buttons, and keeps aria-describedby", async () => {
  const choice_effects = {
    approve: { task: "Coder can use it for this task.", kept: "TeamLead keeps it for you." },
    approve_read: { task: "Read only for this task.", kept: "Read only kept." },
    deny: { any: "Nothing is given." },
  };
  const { reviewer, bubble } = harness({ reviews: { "req-1": accessReview({ choice_effects, choice_effects_default: "kept" }) } });
  const slot = reviewer.mount(bubble, message());
  await settle();
  const card = byClass(slot, "access-review-card")[0];
  const heading = byClass(card, "permission-request-heading")[0];
  assert.equal(byClass(heading, "access-inbox-status").length, 0, "status pill moves out of the heading in compact mode");

  const statusRow = byClass(card, "access-review-status-row")[0];
  assert.ok(statusRow, "status pill and warning chips share one inline row");
  assert.equal(byClass(statusRow, "access-inbox-status")[0].textContent, NEEDS_DECISION);
  assert.deepEqual(byClass(statusRow, "access-review-flag").map(chip => chip.textContent), ["Full access", "Runs code"]);

  const fold = byTag(card, "details")[0];
  const decision = byClass(card, "access-review-decision")[0];
  assert.ok(card.children.indexOf(fold) < card.children.indexOf(decision),
    "Details (with Keep/Note/choice help) precedes the decision buttons in DOM and keyboard order");
  assert.equal(byTag(fold, "textarea")[0].rows, 1);

  const helpList = byClass(fold, "access-review-choice-help")[0];
  assert.ok(helpList, "What each choice does is folded inside Details on compact chat cards");
  assert.equal(byClass(decision, "access-review-choice-help").length, 0);
  const approveBtn = byTag(decision, "button").find(btn => btn.textContent === "Approve");
  const approveHelp = byClass(helpList, "access-review-choice-help-item").find(li => li.getAttribute("data-choice") === "approve");
  assert.equal(approveBtn.getAttribute("aria-describedby"), approveHelp.id);
  approveBtn.dispatchEvent(new Event("focus"));
  assert.equal(approveHelp.classList.contains("is-active"), true);
  approveBtn.dispatchEvent(new Event("blur"));
  assert.equal(approveHelp.classList.contains("is-active"), false);
});

test("decided chat cards are compact and dismiss local-decision follow-up links across 50 transcript redraws", async () => {
  const start = Date.parse("2026-11-21T12:00:00Z");
  const expires = "2026-11-21T12:15:00+00:00";
  const withLink = changeReview({
    status: "granted", decision: "approve", can_decide: false, choices: [],
    follow_up_links: [{ label: "Choose the credential for Portainer", url: "https://broker.example/link/1", expires_at_utc: expires }],
    follow_up_outcomes: [],
  });
  const h = harness({
    startClock: start,
    decide: () => Promise.resolve({ status: "ok", review: withLink }),
  });
  let slot = h.reviewer.mount(h.bubble, message());
  await settle();
  byTag(slot, "button").find(btn => btn.textContent === "Approve").click();
  await settle();

  let card = byClass(slot, "access-review-card")[0];
  assert.ok(card.classList.contains("is-compact") && card.classList.contains("is-decided"));
  assert.deepEqual(byTag(card, "a").map(a => a.textContent), ["Choose the credential for Portainer"]);
  assert.equal(h.intervals.size, 1, "bounded poll starts while a follow-up link is visible");
  assert.equal([...h.intervals.values()][0].delay, FOLLOW_UP_POLL_MS);

  // Simulate 50 transcript redraws in chat.js renderMessages order: clear list,
  // build each bubble detached, call mount, then append bubble to the document.
  for (let i = 0; i < 50; i += 1) {
    h.document.body.replaceChildren();
    const detached = h.document.createElement("article");
    slot = h.reviewer.mount(detached, message());
    h.document.body.append(detached);
  }
  await settle();
  assert.equal(byTag(slot, "a").length, 1);
  assert.equal(h.intervals.size, 1, "poll timer stays running after detached-bubble transcript redraws");

  // Form finishes on the server: focus/poll refreshes and clears the button on the connected slot.
  h.reviews["req-1"] = {
    ...withLink,
    follow_up_links: [],
    follow_up_outcomes: ["The account link for Portainer is done."],
  };
  h.fireWin("focus");
  await settle();

  card = byClass(slot, "access-review-card")[0];
  assert.ok(card.classList.contains("is-compact") && card.classList.contains("is-decided"));
  assert.equal(byTag(card, "a").length, 0, "credential button disappears once the form finishes");
  const statusLine = byClass(card, "access-review-follow-up-outcomes")[0];
  assert.equal(statusLine.getAttribute("role"), "status");
  assert.equal(statusLine.textContent, "The account link for Portainer is done.");
  assert.equal(h.intervals.size, 0, "poll timer stops immediately once no pending links remain");
});

test("reloaded decided card hides expired follow-up link client-side, stops after expiry+grace, pauses when hidden, and stops on sign-out or 403", async () => {
  const start = Date.parse("2026-11-21T12:00:00Z");
  const expires = "2026-11-21T12:15:00+00:00";
  const reloaded = changeReview({
    status: "granted", decision: "approve", decided_by_name: "Ana", can_decide: false, choices: [],
    follow_up_links: [{ label: "Choose the credential for Portainer", url: "https://broker.example/link/1", expires_at_utc: expires }],
    follow_up_outcomes: [],
  });
  const h = harness({ startClock: start, reviews: { "req-1": reloaded } });
  const slot = h.reviewer.mount(h.bubble, message());
  await settle();

  assert.ok(byClass(slot, "access-review-card")[0].classList.contains("is-compact"));
  assert.equal(byTag(slot, "a").length, 1);
  assert.equal(h.intervals.size, 1);

  // Hidden tab stops polling; becoming visible triggers a refresh and resumes polling.
  h.document.hidden = true;
  h.fireDoc("visibilitychange");
  assert.equal(h.intervals.size, 0, "no polling while tab is hidden");

  const getsBefore = h.calls.gets.length;
  h.document.hidden = false;
  h.fireDoc("visibilitychange");
  await settle();
  assert.equal(h.calls.gets.length, getsBefore + 1, "visibilitychange refreshes immediately");
  assert.equal(h.intervals.size, 1);

  // Passing expires_at_utc hides the link button immediately client-side, while staying active in grace.
  h.advance(15 * 60 * 1000);
  [...h.intervals.values()][0].callback();
  await settle();
  assert.equal(byTag(slot, "a").length, 0, "expired link is hidden client-side immediately");
  assert.equal(h.intervals.size, 1, "watcher stays active during grace for final server outcome");

  // Past expiry + grace: watcher stops polling automatically, yet returning
  // focus still reads the server once so the expired outcome line appears.
  h.advance(FOLLOW_UP_EXPIRY_GRACE_MS + 1000);
  [...h.intervals.values()][0].callback();
  await settle();
  assert.equal(h.intervals.size, 0, "watcher stops after expiry + grace");

  h.reviews["req-1"] = {
    ...reloaded,
    follow_up_links: [],
    follow_up_outcomes: ["The account link for Portainer expired."],
  };
  h.fireWin("focus");
  await settle();
  assert.equal(byTag(slot, "a").length, 0);
  assert.match(byClass(slot, "access-review-follow-up-outcomes")[0].textContent,
    /The account link for Portainer expired\./);
  assert.equal(h.intervals.size, 0);

  // 403/404 during follow-up refresh stops polling without replacing the decided card with an error card.
  const h403 = harness({ startClock: start, reviews: { "req-1": reloaded } });
  const slot403 = h403.reviewer.mount(h403.bubble, message());
  await settle();
  assert.equal(h403.intervals.size, 1);
  h403.setReadError(Object.assign(new Error("Forbidden"), { status: 403 }));
  await h403.reviewer.refreshFollowUps();
  await settle();
  assert.equal(h403.intervals.size, 0, "403 stops follow-up polling");
  assert.ok(byClass(slot403, "access-review-card")[0].classList.contains("is-decided"),
    "decided card is kept intact on follow-up refresh error");

  // Sign-out stops polling immediately.
  const hSignOut = harness({ startClock: start, reviews: { "req-1": reloaded } });
  hSignOut.reviewer.mount(hSignOut.bubble, message());
  await settle();
  assert.equal(hSignOut.intervals.size, 1);
  hSignOut.reviewer.clear();
  assert.equal(hSignOut.intervals.size, 0);
});

test("Web Chat card keeps polling while a form is settling in watch grace and replaces settling with completion (F3)", async () => {
  const start = Date.parse("2026-11-21T12:00:00Z");
  const expires = "2026-11-21T12:15:00+00:00";
  const settlingUntil = "2026-11-21T12:16:00+00:00";
  const withLink = changeReview({
    status: "granted", decision: "approve", decided_by_name: "Ana", can_decide: false, choices: [],
    follow_up_links: [{ label: "Choose the credential for Portainer", url: "https://broker.example/link/1", expires_at_utc: expires }],
    follow_up_outcomes: [],
    follow_up_settling: false,
    follow_up_settling_until_utc: null,
  });
  const h = harness({ startClock: start, reviews: { "req-1": withLink } });
  const slot = h.reviewer.mount(h.bubble, message());
  await settle();
  assert.equal(byTag(slot, "a").length, 1);
  assert.equal(h.intervals.size, 1);

  // Clock crosses expires_at_utc; server reports settling during WATCH_GRACE instead of premature expiry.
  h.advance(15 * 60 * 1000 + 2000);
  h.reviews["req-1"] = {
    ...withLink,
    follow_up_links: [],
    follow_up_outcomes: [],
    follow_up_settling: true,
    follow_up_settling_until_utc: settlingUntil,
  };
  [...h.intervals.values()][0].callback();
  await settle();

  assert.equal(byTag(slot, "a").length, 0, "expired link button is hidden while settling");
  assert.equal(byClass(slot, "access-review-follow-up-outcomes")[0].textContent, FOLLOW_UP_SETTLING_TEXT);
  assert.equal(h.intervals.size, 1, "watcher keeps polling while follow_up_settling is true");

  // Late submission finishes during WATCH_GRACE: next poll replaces settling text with completion and stops polling.
  h.advance(4000);
  h.reviews["req-1"] = {
    ...withLink,
    follow_up_links: [],
    follow_up_outcomes: ["The account link for Portainer is done."],
    follow_up_settling: false,
    follow_up_settling_until_utc: null,
  };
  [...h.intervals.values()][0].callback();
  await settle();

  assert.equal(byTag(slot, "a").length, 0);
  assert.equal(byClass(slot, "access-review-follow-up-outcomes")[0].textContent,
    "The account link for Portainer is done.");
  assert.equal(h.intervals.size, 0, "watcher stops once settling finishes");

  // Reloading directly into the settling window also renders the settling state and polls until completion.
  const hReload = harness({
    startClock: Date.parse("2026-11-21T12:15:10Z"),
    reviews: {
      "req-1": {
        ...withLink,
        follow_up_links: [],
        follow_up_outcomes: [],
        follow_up_settling: true,
        follow_up_settling_until_utc: settlingUntil,
      },
    },
  });
  const reloadSlot = hReload.reviewer.mount(hReload.bubble, message());
  await settle();
  assert.equal(byClass(reloadSlot, "access-review-follow-up-outcomes")[0].textContent, FOLLOW_UP_SETTLING_TEXT);
  assert.equal(hReload.intervals.size, 1, "reloaded settling card starts follow-up watcher");
});

test("reloaded decided card hands off active framed forms once, clears pages prompt when finished while keeping non-handed-off link, and ignores non-approver empty links", async () => {
  const start = Date.parse("2026-11-21T12:00:00Z");
  const expires = "2026-11-21T12:15:00+00:00";
  const framedForm = {
    label: "Choose the credential for Portainer", url: "http://127.0.0.1:18600/account-link/a",
    frame_url: "http://127.0.0.1:18600/account-link/a?view=card", expires_at_utc: expires,
  };
  const plainForm = {
    label: "Choose the credential for Gitea", url: "http://127.0.0.1:18600/account-link/b",
    expires_at_utc: expires,
  };
  const handed = [];
  const h = harness({
    startClock: start,
    reviews: {
      "req-1": changeReview({
        status: "granted", decision: "approve", decided_by_name: "Ana", can_decide: false, choices: [],
        follow_up_links: [framedForm, plainForm], follow_up_outcomes: [],
      }),
    },
    onFollowUpPages: (id, links, placement) => {
      handed.push({ id, urls: links.map(link => link.url), placement });
      return links;
    },
  });

  const slot = h.reviewer.mount(h.bubble, message());
  await settle();

  assert.deepEqual(handed, [{ id: "req-1", urls: [framedForm.url], placement: FOLLOW_UP_AFTER_REVIEW }],
    "reload hands off framed link once");
  assert.equal(byClass(slot, "access-review-follow-up-note")[0].textContent, FOLLOW_UP_PAGES_BELOW);
  assert.deepEqual(byTag(slot, "a").map(a => a.textContent), ["Choose the credential for Gitea"]);

  // Re-mounting during transcript re-renders never calls onFollowUpPages inside render.
  h.reviewer.mount(h.bubble, message());
  await settle();
  assert.equal(handed.length, 1);

  // Framed form finishes on the server: pages prompt clears while plain link remains a button.
  h.reviews["req-1"] = changeReview({
    status: "granted", decision: "approve", decided_by_name: "Ana", can_decide: false, choices: [],
    follow_up_links: [plainForm], follow_up_outcomes: ["The account link for Portainer is done."],
  });
  [...h.intervals.values()][0].callback();
  await settle();
  assert.equal(byClass(slot, "access-review-follow-up-note").length, 0, "pages prompt clears once handed-off form finishes");
  assert.deepEqual(byTag(slot, "a").map(a => a.textContent), ["Choose the credential for Gitea"]);
  assert.match(byClass(slot, "access-review-follow-up-outcomes")[0].textContent, /Portainer is done\./);

  // Plain form finishes too: button disappears and watcher stops.
  h.reviews["req-1"] = changeReview({
    status: "granted", decision: "approve", decided_by_name: "Ana", can_decide: false, choices: [],
    follow_up_links: [], follow_up_outcomes: ["The account link for Portainer is done.", "The account link for Gitea is done."],
  });
  [...h.intervals.values()][0].callback();
  await settle();
  assert.equal(byTag(slot, "a").length, 0);
  assert.equal(byClass(slot, "access-review-follow-up-note").length, 0);
  assert.equal(h.intervals.size, 0);

  // Non-approver reload (server returns empty follow_up_links): no hand-off, no prompt, no buttons.
  const otherHanded = [];
  const hOther = harness({
    startClock: start,
    reviews: {
      "req-1": changeReview({
        status: "granted", decision: "approve", decided_by_name: "Ana", can_decide: false, choices: [],
        follow_up_links: [], follow_up_outcomes: [],
      }),
    },
    onFollowUpPages: (id, links) => { otherHanded.push({ id, links }); return links; },
  });
  const otherSlot = hOther.reviewer.mount(hOther.bubble, message());
  await settle();
  assert.equal(otherHanded.length, 0);
  assert.equal(byClass(otherSlot, "access-review-follow-up-note").length, 0);
  assert.equal(byTag(otherSlot, "a").length, 0);
});

test("waiting-to-decided re-read hands off remaining subset after one form finished and clears prompt on client-side expiry", async () => {
  const start = Date.parse("2026-11-21T12:00:00Z");
  const expires = "2026-11-21T12:15:00+00:00";
  const formB = {
    label: "Choose the credential for Gitea", url: "http://127.0.0.1:18600/account-link/b",
    frame_url: "http://127.0.0.1:18600/account-link/b?view=card", expires_at_utc: expires,
  };
  const handed = [];
  const h = harness({
    startClock: start,
    onFollowUpPages: (id, links, placement) => {
      handed.push({ id, urls: links.map(link => link.url), placement });
      return links;
    },
  });

  // Initial mount sees waiting request.
  h.reviewer.mount(h.bubble, message());
  await settle();
  assert.equal(handed.length, 0);

  // Approved elsewhere and form A already finished, leaving partial subset [formB].
  h.reviews["req-1"] = changeReview({
    status: "granted", decision: "approve", decided_by_name: "Ana", can_decide: false, choices: [],
    follow_up_links: [formB], follow_up_outcomes: ["The account link for Portainer is done."],
  });
  h.advance(REFRESH_WAITING_AFTER_MS);
  const slot = h.reviewer.mount(h.bubble, message());
  await settle();

  assert.deepEqual(handed, [{ id: "req-1", urls: [formB.url], placement: FOLLOW_UP_AFTER_REVIEW }]);
  assert.equal(byClass(slot, "access-review-follow-up-note")[0].textContent, FOLLOW_UP_PAGES_BELOW);
  assert.equal(byTag(slot, "a").length, 0);
  assert.equal(h.intervals.size, 1);

  // Crossing expiry hides the pages prompt client-side immediately.
  h.advance(15 * 60 * 1000);
  [...h.intervals.values()][0].callback();
  await settle();
  assert.equal(byClass(slot, "access-review-follow-up-note").length, 0, "expired handed-off form clears pages prompt client-side");
});

test("refreshFollowUps redraws the decided card when a hand-off succeeds with an unchanged followUpSignature", async () => {
  const start = Date.parse("2026-11-21T12:00:00Z");
  const expires = "2026-11-21T12:15:00+00:00";
  const formA = {
    label: "Choose the credential for Portainer", url: "http://127.0.0.1:18600/account-link/a",
    frame_url: "http://127.0.0.1:18600/account-link/a?view=card", expires_at_utc: expires,
  };
  let allowHandOff = false;
  const decidedReview = changeReview({
    status: "granted", decision: "approve", decided_by_name: "Ana", can_decide: false, choices: [],
    follow_up_links: [formA], follow_up_outcomes: [],
  });
  const h = harness({
    startClock: start,
    reviews: { "req-1": decidedReview },
    onFollowUpPages: (_id, links) => (allowHandOff ? links : []),
  });

  const slot = h.reviewer.mount(h.bubble, message());
  await settle();
  assert.equal(byTag(slot, "a").length, 1, "starts as fallback button when initial hand-off returns empty");
  assert.equal(byClass(slot, "access-review-follow-up-note").length, 0);

  // Next poll returns the identical review (same followUpSignature), but hand-off now succeeds.
  allowHandOff = true;
  [...h.intervals.values()][0].callback();
  await settle();
  assert.equal(byTag(slot, "a").length, 0, "card redraws to remove fallback button");
  assert.equal(byClass(slot, "access-review-follow-up-note")[0].textContent, FOLLOW_UP_PAGES_BELOW);
});

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import {
  ACCESS_REMOVED_LABEL, LACKS_ACCESS_NOTE, NEEDS_DECISION, WAITING_FOR_APPROVER,
  INBOX_COUNTS_PATH, NOTICES_READ_PATH, createAccessInbox, createInboxCounts, decidableReviewCount, decisionOutcome,
  emptyNoticesText, followUpLinks, inboxBadgeLabel, inboxBadgeText, inboxCounts, inboxPath, localTimeFormatter,
  markedText, parseAccessRequestLink, renderNoticeRow, renderReviewCard, reviewCardModel, unreadNoticeCount,
} from "../assistant_gateway/onboarding_web/assets/access_inbox.mjs";

// A small DOM stand-in. innerHTML throws, so any HTML parsing path fails the tests.
class FakeElement extends EventTarget {
  constructor(document, tagName) {
    super();
    Object.assign(this, { ownerDocument: document, tagName: tagName.toUpperCase(), children: [],
      parentNode: null, attributes: new Map(), className: "", ownText: "", hidden: false,
      disabled: false, readOnly: false, value: "", tabIndex: 0, scrolled: 0 });
  }
  get classList() {
    const names = () => this.className.split(/\s+/).filter(Boolean);
    const set = values => { this.className = [...new Set(values)].join(" "); };
    return {
      add: (...added) => set([...names(), ...added]),
      remove: (...removed) => set(names().filter(name => !removed.includes(name))),
      toggle: (name, force) => {
        const on = force === undefined ? !names().includes(name) : Boolean(force);
        set(on ? [...names(), name] : names().filter(value => value !== name));
      },
      contains: name => names().includes(name),
    };
  }
  set innerHTML(_value) { throw new Error("innerHTML must not be used"); }
  get innerHTML() { throw new Error("innerHTML must not be used"); }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this.replaceChildren(); this.ownText = String(value); }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children.at(-1) || null; }
  get isConnected() {
    for (let node = this; node; node = node.parentNode) if (node === this.ownerDocument.body) return true;
    return false;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  removeAttribute(name) { this.attributes.delete(name); }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  appendChild(node) {
    node.remove();
    node.parentNode = this;
    this.children.push(node);
    return node;
  }
  replaceChildren(...nodes) {
    this.children.forEach(child => { child.parentNode = null; });
    this.children = [];
    this.ownText = "";
    this.append(...nodes);
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter(child => child !== this);
    this.parentNode = null;
  }
  replaceWith(node) {
    const parent = this.parentNode;
    if (!parent) return;
    node.remove();
    parent.children.splice(parent.children.indexOf(this), 1, node);
    node.parentNode = parent;
    this.parentNode = null;
  }
  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true;
    return false;
  }
  querySelectorAll(tagName) { return descendants(this).filter(node => node.tagName === tagName.toUpperCase()); }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() { this.scrolled += 1; }
  click() { this.dispatchEvent(new Event("click")); }
}

function fakeDocument() {
  const document = {};
  document.createElement = tagName => new FakeElement(document, tagName);
  document.body = new FakeElement(document, "body");
  document.activeElement = document.body;
  return document;
}

function descendants(node) {
  return node.children.flatMap(child => [child, ...descendants(child)]);
}
const byTag = (node, tagName) => descendants(node).filter(child => child.tagName === tagName.toUpperCase());
const byClass = (node, name) => descendants(node).filter(child => child.classList.contains(name));
const buttonLabels = node => byTag(node, "button").map(button => button.textContent);
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };

const formatTime = localTimeFormatter({ locale: "en-US", timeZone: "UTC" });

const SERVER_CHOICES = [
  { choice: "approve", label: "Approve" },
  { choice: "approve_read", label: "Approve read only" },
  { choice: "approve_task", label: "Approve for this task only" },
  { choice: "deny", label: "Deny" },
];

function review(overrides = {}) {
  return {
    request_id: "req-1", title: "WebResearchAgent asks for read-and-change access to Heartbeat",
    agent_type: "WebResearchAgent", recipient: "WebResearchAgent", target: "file:heartbeat",
    target_name: "Heartbeat", level: "write", level_words: "read-and-change", lifetime: "until_revoked",
    lifetime_words: "until someone removes it", expires_at_utc: null,
    reason: "I need to update the heartbeat file.", working_for: "person-1", person_lacks_access: false,
    task_id: "task-1", status: "waiting", created_at_utc: "2026-09-25T15:04:00+00:00",
    can_decide: true, choices: SERVER_CHOICES, ...overrides,
  };
}

function notice(overrides = {}) {
  return {
    notice_id: "n-1", kind: "hand_down", title: "An agent passed on its access",
    summary: "WebResearchAgent gave Summarizer read access to Heartbeat.", grant_id: null,
    created_at_utc: "2026-09-24T08:30:00+00:00", read: false, ...overrides,
  };
}

test("Inbox links select an access request only from the access_request fragment key", () => {
  assert.equal(parseAccessRequestLink("#access_request=req-1"), "req-1");
  assert.equal(parseAccessRequestLink("#access_request=a%2Fb%20c"), "a/b c");
  assert.equal(parseAccessRequestLink("#permission=token&access_request=req-2"), "req-2");
  assert.equal(parseAccessRequestLink("access_request=req-3"), "req-3");
  assert.equal(parseAccessRequestLink("#interaction=old-1"), "");
  assert.equal(parseAccessRequestLink("#access_request="), "");
  assert.equal(parseAccessRequestLink(""), "");
  assert.equal(parseAccessRequestLink(undefined), "");
});

test("the badge counts requests the person can decide plus unread notices", () => {
  const reviews = [review(), review({ request_id: "req-2", can_decide: false, choices: [] }), review({ request_id: "req-3" })];
  assert.equal(decidableReviewCount(reviews), 2);
  assert.equal(decidableReviewCount(undefined), 0);
  assert.deepEqual(inboxCounts({ decidable_requests: 2, unread_notices: 3 }), { decidableRequests: 2, unreadNotices: 3 });
  assert.deepEqual(inboxCounts({ decidable_requests: -1 }), { decidableRequests: 0, unreadNotices: 0 });
  assert.equal(inboxBadgeText({ decidableRequests: 2, unreadNotices: 3 }), "5");
  assert.equal(inboxBadgeText({ decidableRequests: 0, unreadNotices: 0 }), "");
  assert.equal(inboxBadgeText(undefined), "");
  assert.equal(inboxBadgeLabel({ decidableRequests: 1, unreadNotices: 2 }), "Inbox: 1 request to decide, 2 unread notices");
  assert.equal(inboxBadgeLabel({ decidableRequests: 0, unreadNotices: 1 }), "Inbox: 1 unread notice");
  assert.equal(inboxBadgeLabel({ decidableRequests: 0, unreadNotices: 0 }), "");
});

test("notice reads name the view and page, and the empty list says what to do", () => {
  assert.equal(inboxPath("unread", 0), "/api/web-chat/access-inbox?notices=unread&offset=0&limit=20");
  assert.equal(inboxPath("all", 40), "/api/web-chat/access-inbox?notices=all&offset=40&limit=20");
  assert.equal(emptyNoticesText("unread", 3), "No unread notices. Choose All to see earlier ones.");
  assert.equal(emptyNoticesText("unread", 0), "No notices.");
  assert.equal(emptyNoticesText("all", 0), "No notices.");
  assert.equal(markedText(0), "Nothing new to mark; those notices were already read.");
  assert.equal(markedText(1), "1 notice marked as read.");
  assert.equal(markedText(4), "4 notices marked as read.");
});

test("a decidable card shows access, lifetime, reason and one button per server choice", () => {
  const document = fakeDocument();
  const card = renderReviewCard(document, review(), { formatTime });
  const content = card.textContent;
  assert.equal(card.getAttribute("data-request-id"), "req-1");
  assert.equal(byTag(card, "h5")[0].textContent, "WebResearchAgent asks for read-and-change access to Heartbeat");
  assert.match(content, /Accessread-and-change/);
  assert.match(content, /How longuntil someone removes it/);
  assert.match(content, /Asked\s*Sep 25, 2026, 3:04\sPM UTC/);
  assert.doesNotMatch(content, /2026-09-25T/, "Raw ISO times are never shown");
  assert.match(content, /Why \(from WebResearchAgent\)I need to update the heartbeat file\./);
  assert.equal(byClass(card, "access-inbox-status")[0].textContent, NEEDS_DECISION);
  assert.doesNotMatch(content, new RegExp(LACKS_ACCESS_NOTE.slice(0, 30)));

  const buttons = byTag(card, "button");
  assert.deepEqual(buttons.map(button => button.textContent),
    ["Approve", "Approve read only", "Approve for this task only", "Deny"]);
  assert.deepEqual(buttons.map(button => button.getAttribute("data-choice")),
    ["approve", "approve_read", "approve_task", "deny"]);
  assert.ok(buttons[0].classList.contains("primary"));
  assert.ok(buttons[1].classList.contains("secondary"));
  assert.ok(buttons[2].classList.contains("secondary"));
  assert.ok(buttons[3].classList.contains("danger"));
  assert.ok(buttons.every(button => button.type === "button"));

  const [note] = byTag(card, "textarea");
  const [label] = byTag(card, "label");
  assert.equal(label.textContent, "Note (optional)");
  assert.equal(label.htmlFor, note.id);
});

test("button text comes from the server label, and unknown choice keys are left out and reported", () => {
  const document = fakeDocument();
  const warnings = [];
  const card = renderReviewCard(document, review({ choices: [
    { choice: "approve", label: "Yes, give access" },
    { choice: "approve_forever", label: "Always" },
    { choice: "deny", label: "No" },
  ] }), { formatTime, warn: (...args) => warnings.push(args) });
  assert.deepEqual(buttonLabels(card), ["Yes, give access", "No"]);
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0][0], "access_inbox_unknown_choice");
  assert.equal(warnings[0][1].request_id, "req-1");
});

test("a card the person cannot decide shows who it waits for and has no controls", () => {
  const document = fakeDocument();
  for (const choices of [[], SERVER_CHOICES]) {
    const card = renderReviewCard(document, review({ can_decide: false, choices }), { formatTime });
    assert.equal(byClass(card, "access-inbox-status")[0].textContent, WAITING_FOR_APPROVER);
    assert.equal(byTag(card, "button").length, 0, "A stale choice list never creates buttons");
    assert.equal(byTag(card, "textarea").length, 0);
  }
});

test("a card warns when the person the task works for lacks the access too", () => {
  const document = fakeDocument();
  const card = renderReviewCard(document, review({ person_lacks_access: true }), { formatTime });
  const [warning] = byClass(card, "access-review-warning");
  assert.ok(warning.textContent.includes(LACKS_ACCESS_NOTE));
  assert.equal(byClass(warning, "access-review-warning-icon")[0].getAttribute("aria-hidden"), "true");
});

test("agent-written reasons and titles stay text, even when they contain markup", () => {
  const document = fakeDocument();
  const reason = "<script>alert(1)</script><b>trust me</b>";
  const card = renderReviewCard(document, review({ reason, title: "<img src=x onerror=alert(1)>" }), { formatTime });
  assert.equal(byTag(card, "script").length, 0);
  assert.equal(byTag(card, "b").length, 0);
  assert.equal(byTag(card, "img").length, 0);
  assert.ok(byTag(card, "p").some(node => node.textContent === reason));
  assert.equal(byTag(card, "h5")[0].textContent, "<img src=x onerror=alert(1)>");
});

test("a set end time is shown as a readable local time next to the lifetime", () => {
  const document = fakeDocument();
  const card = renderReviewCard(document, review({
    lifetime: "until", lifetime_words: "until a set time", expires_at_utc: "2026-09-30T17:00:00+00:00",
  }), { formatTime });
  assert.match(card.textContent, /How longuntil a set time \(ends Sep 30, 2026, 5:00\sPM UTC\)/);
  assert.doesNotMatch(card.textContent, /2026-09-30T/);
});

test("decision outcomes follow the saved status and the chosen approval", () => {
  assert.equal(decisionOutcome("approve", { status: "granted" }).label, "Approved");
  assert.equal(decisionOutcome("approve_read", { status: "granted" }).label, "Approved read only");
  assert.equal(decisionOutcome("approve_task", { status: "granted" }).label, "Approved for this task");
  assert.equal(decisionOutcome("approve", { status: "granted" }, false).label, "Approved for this task");
  assert.equal(decisionOutcome("approve", { status: "granted" }, true).label, "Approved and kept");
  assert.equal(decisionOutcome("approve_all", { status: "granted" }).label, "Approved as proposed");
  assert.equal(decisionOutcome("deny", { status: "denied" }).label, "Denied");
  assert.equal(decisionOutcome("approve", { status: "withdrawn" }).label, "Withdrawn");
  assert.equal(decisionOutcome("approve", undefined).label, "Decision saved");
});

test("notice rows show unread state in words, a readable time, and the right actions", () => {
  const document = fakeDocument();
  const unread = renderNoticeRow(document, notice(), { formatTime });
  assert.ok(unread.classList.contains("is-unread"));
  assert.ok(byClass(unread, "access-inbox-status").some(node => node.textContent === "New"));
  assert.deepEqual(buttonLabels(unread), ["Mark as read"]);
  const [time] = byTag(unread, "time");
  assert.equal(time.getAttribute("datetime"), "2026-09-24T08:30:00+00:00");
  assert.match(time.textContent, /Sep 24, 2026, 8:30\sAM UTC/);
  assert.match(unread.textContent, /WebResearchAgent gave Summarizer read access to Heartbeat\./);

  const read = renderNoticeRow(document, notice({ read: true, grant_id: "g-1" }), { formatTime });
  assert.equal(read.classList.contains("is-unread"), false);
  assert.equal(byClass(read, "access-inbox-status").length, 0);
  assert.deepEqual(buttonLabels(read), ["Remove access"]);
  assert.ok(byTag(read, "button")[0].classList.contains("danger"));

  const removed = renderNoticeRow(document, notice({ read: true, grant_id: "g-1" }),
    { formatTime, removedGrants: new Set(["g-1"]) });
  assert.deepEqual(buttonLabels(removed), []);
  assert.ok(byClass(removed, "access-inbox-status").some(node => node.textContent === ACCESS_REMOVED_LABEL));
});

test("unread notice counts prefer the server total", () => {
  assert.equal(unreadNoticeCount({ unread_notices: 4, notices: [notice()] }), 4);
  assert.equal(unreadNoticeCount({ notices: [notice(), notice({ read: true })] }), 1);
  assert.equal(unreadNoticeCount(undefined), 0);
});

function controllerFixture() {
  const document = fakeDocument();
  const container = document.createElement("div");
  document.body.append(container);
  // chat.html loads the real busy-button helper before this module.
  const window = {};
  vm.runInNewContext(
    readFileSync(new URL("../assistant_gateway/onboarding_web/assets/action_feedback.js", import.meta.url), "utf8"),
    { window },
  );
  const gets = [], posts = [], timers = [], warnings = [];
  const counts = [], pages = [];
  const browser = {
    document, console: { warn: (...args) => warnings.push(args) },
    requestAnimationFrame: callback => callback(),
    setTimeout: (callback, delay) => timers.push({ callback, delay }),
  };
  const inbox = createAccessInbox({
    container, browser, formatTime,
    get: path => new Promise((resolve, reject) => gets.push({ path, resolve, reject })),
    post: (path, body) => new Promise((resolve, reject) => posts.push({ path, body, resolve, reject })),
    beginAction: (button, busyLabel, group) => window.UAActionFeedback.begin(button, { busyLabel, group }),
    failureMessage: (error, fallback) => (error && error.message) || fallback,
    onCounts: value => counts.push(value),
    paginate: options => { pages.push(options); return document.createElement("nav"); },
  });
  const [status, requestsSection, noticesSection] = container.children;
  const requestList = requestsSection.lastChild;
  const noticeList = byClass(noticesSection, "access-inbox-list")[0];
  const toolbar = byClass(noticesSection, "access-notice-toolbar")[0];
  return { document, container, inbox, gets, posts, timers, warnings, status, requestList, noticeList,
    noticesSection, toolbar, counts, pages,
    noticeMessage: byClass(noticesSection, "access-inbox-card-message")[0],
    pager: byClass(noticesSection, "access-notice-pager")[0],
    viewTab: label => byClass(noticesSection, "permission-tab").find(tab => tab.textContent === label),
    toolbarButton: label => byTag(toolbar, "button").find(button => button.textContent.startsWith(label)) };
}

async function loaded(f, payload) {
  const load = f.inbox.load();
  await settle();
  f.gets.at(-1).resolve({ status: "ok", ...payload });
  await load;
  await settle();
}

test("the access sections load reviews and unread notices, and report both counts", async () => {
  const f = controllerFixture();
  await loaded(f, {
    reviews: [review(), review({ request_id: "req-2", can_decide: false, choices: [] })],
    notices: [notice(), notice({ notice_id: "n-2" })], unread_notices: 2, notices_total: 2, notices_all_total: 5,
  });
  assert.equal(f.gets[0].path, "/api/web-chat/access-inbox?notices=unread&offset=0&limit=20");
  assert.equal(f.requestList.children.length, 2);
  assert.equal(f.noticeList.children.length, 2);
  assert.equal(byClass(f.noticesSection, "access-inbox-unread")[0].textContent, "2 unread");
  assert.deepEqual(f.counts.at(-1), { decidableRequests: 1, unreadNotices: 2 });
  assert.equal(f.viewTab("Unread").getAttribute("aria-selected"), "true");
  assert.equal(f.pages.length, 0, "One page needs no page controls");
});

test("a decision disables the choices while sending, then shows the outcome and refreshes", async () => {
  const f = controllerFixture();
  await loaded(f, { reviews: [review()], notices: [] });
  const card = f.requestList.children[0];
  const [note] = byTag(card, "textarea");
  note.value = "  Fine for this week.  ";
  const buttons = byTag(card, "button");
  buttons[0].click();
  await settle();
  assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].path, "/api/web-chat/access-inbox/requests/req-1/decision");
  assert.deepEqual(f.posts[0].body, { choice: "approve", note: "Fine for this week." });
  assert.ok(buttons.every(button => button.disabled), "Every choice is disabled while one is sent");
  assert.equal(buttons[0].textContent, "Approving…");
  assert.equal(buttons[0].getAttribute("aria-busy"), "true");
  assert.equal(note.readOnly, true);
  buttons[3].click();
  await settle();
  assert.equal(f.posts.length, 1, "A second click cannot send another decision");

  f.posts[0].resolve({ status: "ok", review: { ...review(), status: "granted", can_decide: false, choices: [] } });
  await settle();
  const decided = f.requestList.children[0];
  assert.ok(decided.classList.contains("is-decided"));
  assert.equal(byClass(decided, "access-inbox-status")[0].textContent, "Approved");
  assert.equal(f.document.activeElement, decided);
  assert.equal(f.gets.length, 2, "The list refreshes after a decision");

  f.gets[1].resolve({ status: "ok", reviews: [], notices: [] });
  await settle();
  const kept = f.requestList.children[0];
  assert.equal(byClass(kept, "access-inbox-status")[0].textContent, "Approved",
    "The outcome stays visible after the refresh");
  assert.equal(f.document.activeElement, kept, "Keyboard focus stays on the same request");
  assert.deepEqual(f.counts.at(-1), { decidableRequests: 0, unreadNotices: 0 });
});

test("a refused decision shows the server's reason in the card and restores the choices", async () => {
  const f = controllerFixture();
  await loaded(f, { reviews: [review()], notices: [] });
  const card = f.requestList.children[0];
  const buttons = byTag(card, "button");
  buttons[3].click();
  assert.equal(buttons[3].textContent, "Denying…");
  await settle();
  f.posts[0].reject(new Error("Someone else already decided this request."));
  await settle();
  const [message] = byClass(card, "access-inbox-card-message");
  assert.equal(message.hidden, false);
  assert.ok(message.classList.contains("is-error"));
  assert.equal(message.textContent, "Someone else already decided this request.");
  assert.ok(buttons.every(button => !button.disabled));
  assert.equal(buttons[3].textContent, "Deny");
  assert.equal(byTag(card, "textarea")[0].readOnly, false);
  assert.equal(f.gets.length, 1, "A refused decision does not replace the card");
});

test("an Inbox link highlights and focuses its request after loading", async () => {
  const f = controllerFixture();
  f.inbox.focusRequest("req-2");
  await loaded(f, { reviews: [review(), review({ request_id: "req-2" })], notices: [] });
  const target = f.requestList.children[1];
  assert.ok(target.classList.contains("is-highlighted"));
  assert.equal(target.tabIndex, -1);
  assert.equal(target.scrolled, 1);
  assert.equal(f.document.activeElement, target);
  assert.equal(f.status.hidden, true);

  f.inbox.focusRequest("req-gone");
  await loaded(f, { reviews: [review()], notices: [] });
  assert.equal(f.status.hidden, false);
  assert.match(f.status.textContent, /not waiting for you any more/);
});

test("marking one notice read sends its id, then re-reads the page so it leaves the Unread view", async () => {
  const f = controllerFixture();
  await loaded(f, { reviews: [], notices: [notice()], unread_notices: 1, notices_total: 1, notices_all_total: 1 });
  const row = f.noticeList.children[0];
  byTag(row, "button")[0].click();
  await settle();
  assert.equal(f.posts[0].path, NOTICES_READ_PATH);
  assert.deepEqual(f.posts[0].body, { notice_ids: ["n-1"] });
  f.posts[0].resolve({ status: "ok", marked: 1, unread_notices: 0 });
  await settle();
  assert.deepEqual(f.counts.at(-1), { unreadNotices: 0 }, "The badge drops at once");
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-inbox?notices=unread&offset=0&limit=20");
  f.gets.at(-1).resolve({ status: "ok", notices: [], unread_notices: 0, notices_total: 0, notices_all_total: 1 });
  await settle();
  assert.equal(f.noticeList.textContent, "No unread notices. Choose All to see earlier ones.");
  assert.equal(byClass(f.noticesSection, "access-inbox-unread")[0].hidden, true);
  assert.equal(f.toolbar.hidden, true, "Nothing is left to mark");
});

test("selected notices are marked read together, and Select all covers this page only", async () => {
  const f = controllerFixture();
  const page = [notice(), notice({ notice_id: "n-2" }), notice({ notice_id: "n-3", read: true })];
  await loaded(f, { reviews: [], notices: page, unread_notices: 30, notices_total: 25, notices_all_total: 40 });
  assert.equal(f.pages.length, 1, "More than one page shows the page controls");
  assert.deepEqual([f.pages[0].offset, f.pages[0].total, f.pages[0].pageSize], [0, 25, 20]);
  const boxes = byClass(f.noticeList, "access-notice-select");
  assert.equal(boxes.length, 2, "Only unread notices can be selected");
  assert.equal(boxes[0].getAttribute("aria-label"), "Select notice: An agent passed on its access");
  const markSelected = f.toolbarButton("Mark selected as read");
  assert.equal(markSelected.disabled, true);

  boxes[1].checked = true;
  boxes[1].dispatchEvent(new Event("change"));
  assert.equal(markSelected.textContent, "Mark selected as read (1)");
  assert.equal(markSelected.disabled, false);
  const [selectPage] = byTag(byClass(f.toolbar, "access-notice-select-page")[0], "input");
  selectPage.checked = true;
  selectPage.dispatchEvent(new Event("change"));
  assert.ok(byClass(f.noticeList, "access-notice-select").every(box => box.checked));
  markSelected.click();
  await settle();
  assert.deepEqual(f.posts[0].body, { notice_ids: ["n-1", "n-2"] });
  f.posts[0].resolve({ status: "ok", marked: 2, unread_notices: 28 });
  await settle();
  f.gets.at(-1).resolve({ status: "ok", notices: [notice({ notice_id: "n-4" })], unread_notices: 28,
    notices_total: 23, notices_all_total: 40 });
  await settle();
  assert.equal(f.noticeMessage.textContent, "2 notices marked as read.");
  assert.equal(f.toolbarButton("Mark selected as read").textContent, "Mark selected as read");
});

test("Mark all as read asks once more, then marks every page", async () => {
  const f = controllerFixture();
  await loaded(f, { reviews: [], notices: [notice()], unread_notices: 30, notices_total: 30, notices_all_total: 30 });
  const markAll = f.toolbarButton("Mark all as read");
  markAll.click();
  await settle();
  assert.equal(f.posts.length, 0, "The first click only asks for confirmation");
  assert.equal(markAll.textContent, "Confirm: mark all as read");
  assert.match(f.noticeMessage.textContent, /mark all 30 unread notices as read, on every page/);
  markAll.click();
  await settle();
  assert.deepEqual(f.posts[0].body, { all: true });
  f.posts[0].resolve({ status: "ok", marked: 30, unread_notices: 0 });
  await settle();
  assert.equal(markAll.textContent, "Mark all as read");
  f.gets.at(-1).resolve({ status: "ok", notices: [], unread_notices: 0, notices_total: 0, notices_all_total: 30 });
  await settle();
  assert.equal(f.noticeMessage.textContent, "30 notices marked as read.");
  assert.equal(markAll.hidden, true);
});

test("All shows read notices too, and a page left empty steps back one page", async () => {
  const f = controllerFixture();
  await loaded(f, { reviews: [review()], notices: [notice()], unread_notices: 1, notices_total: 1, notices_all_total: 21 });
  const requestCard = f.requestList.children[0];
  f.viewTab("All").click();
  await settle();
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-inbox?notices=all&offset=0&limit=20");
  f.gets.at(-1).resolve({ status: "ok", notices: [notice({ read: true })], unread_notices: 1, notices_total: 21,
    notices_all_total: 21, notices_offset: 0 });
  await settle();
  assert.equal(f.viewTab("All").getAttribute("aria-selected"), "true");
  assert.equal(f.requestList.children[0], requestCard, "Paging notices never redraws the requests");

  f.pages.at(-1).onPage(20);
  await settle();
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-inbox?notices=all&offset=20&limit=20");
  f.gets.at(-1).resolve({ status: "ok", notices: [], unread_notices: 0, notices_total: 20, notices_all_total: 20,
    notices_offset: 20 });
  await settle();
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-inbox?notices=all&offset=0&limit=20");
});

test("removing access asks for confirmation, then marks the notice read", async () => {
  const f = controllerFixture();
  await loaded(f, { reviews: [], notices: [notice({ grant_id: "g-1" })], unread_notices: 1 });
  const row = f.noticeList.children[0];
  const remove = byTag(row, "button").find(button => button.textContent === "Remove access");
  remove.click();
  await settle();
  assert.equal(f.posts.length, 0, "The first click only asks for confirmation");
  assert.equal(remove.textContent, "Confirm removal");
  assert.match(byClass(row, "access-inbox-card-message")[0].textContent, /Confirm removal/);
  assert.equal(f.timers[0].delay, 5000);

  remove.click();
  assert.equal(remove.textContent, "Removing…");
  await settle();
  assert.equal(f.posts[0].path, "/api/web-chat/access-inbox/grants/g-1/revoke");
  f.posts[0].resolve({ status: "ok", revoked_grant_ids: ["g-1", "g-2"] });
  await settle();
  assert.equal(f.posts[1].path, NOTICES_READ_PATH);
  assert.deepEqual(f.posts[1].body, { notice_ids: ["n-1"] });
  f.posts[1].resolve({ status: "ok", marked: 1, unread_notices: 0 });
  await settle();
  const next = f.noticeList.children[0];
  assert.deepEqual(buttonLabels(next), []);
  assert.ok(byClass(next, "access-inbox-status").some(node => node.textContent === ACCESS_REMOVED_LABEL));
  assert.ok(!byClass(next, "access-inbox-status").some(node => node.textContent === "New"));
  assert.equal(byClass(next, "access-inbox-card-message")[0].textContent, "Access removed.");
});

test("a refused removal shows the reason and resets the button", async () => {
  const f = controllerFixture();
  await loaded(f, { reviews: [], notices: [notice({ grant_id: "g-1", read: true })] });
  const row = f.noticeList.children[0];
  const [remove] = byTag(row, "button");
  remove.click();
  remove.click();
  await settle();
  f.posts[0].reject(new Error("This access was already removed."));
  await settle();
  assert.equal(remove.textContent, "Remove access");
  assert.equal(remove.disabled, false);
  assert.equal(byClass(row, "access-inbox-card-message")[0].textContent, "This access was already removed.");
});

test("sign-out clears the sections and ignores a reply to an earlier read", async () => {
  const f = controllerFixture();
  const load = f.inbox.load();
  await settle();
  f.inbox.clear();
  f.gets[0].resolve({ status: "ok", reviews: [review()], notices: [notice()] });
  await load;
  await settle();
  assert.equal(f.requestList.children.length, 0);
  assert.equal(f.noticeList.children.length, 0);
  assert.deepEqual(f.counts, [], "A reply after sign-out reports nothing");
});

test("the page-wide count reads once, polls while visible, coalesces bursts and clears on sign-out", async () => {
  const document = fakeDocument();
  document.hidden = false;
  document.addEventListener = (type, listener) => { document[`on${type}`] = listener; };
  const reads = [], timers = [], intervals = [], changes = [];
  const browser = {
    document, console: { warn() {} },
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeout() {},
    setInterval: (callback, delay) => { intervals.push({ callback, delay }); return intervals.length; },
    clearInterval() {},
  };
  const counts = createInboxCounts({
    browser, onChange: value => changes.push(value),
    get: path => new Promise(resolve => reads.push({ path, resolve })),
  });
  counts.refreshSoon();
  assert.equal(timers.length, 0, "Nothing is read before sign-in");
  counts.start();
  counts.start();
  counts.refresh();
  await settle();
  assert.equal(reads.length, 1, "A second start and a read in flight share one read");
  assert.equal(reads[0].path, INBOX_COUNTS_PATH);
  assert.equal(intervals[0].delay, 30000);
  reads[0].resolve({ status: "ok", decidable_requests: 1, unread_notices: 4 });
  await settle();
  assert.deepEqual(changes.at(-1), { decidableRequests: 1, unreadNotices: 4 });

  counts.refreshSoon();
  counts.refreshSoon();
  assert.equal(timers.length, 1, "A burst of messages gives one read");
  timers[0].callback();
  await settle();
  assert.equal(reads.length, 2);
  reads[1].resolve({ status: "ok", decidable_requests: 1, unread_notices: 4 });
  await settle();
  assert.equal(changes.length, 1, "Unchanged counts do not redraw");

  document.hidden = true;
  intervals[0].callback();
  await settle();
  assert.equal(reads.length, 2, "A hidden page does not poll");
  document.hidden = false;
  document.onvisibilitychange();
  await settle();
  assert.equal(reads.length, 3, "Coming back reads at once");

  counts.set({ unreadNotices: 0 });
  assert.deepEqual(changes.at(-1), { decidableRequests: 1, unreadNotices: 0 });
  counts.stop();
  assert.deepEqual(changes.at(-1), { decidableRequests: 0, unreadNotices: 0 });
  reads[2].resolve({ status: "ok", decidable_requests: 9, unread_notices: 9 });
  await settle();
  assert.deepEqual(counts.current(), { decidableRequests: 0, unreadNotices: 0 }, "A late reply is ignored");
});

test("a failed refresh keeps the last list and says it may be out of date", async () => {
  const f = controllerFixture();
  await loaded(f, { reviews: [review()], notices: [] });
  const load = f.inbox.load();
  await settle();
  f.gets[1].reject(new Error("The request did not reach the gateway."));
  await load;
  assert.equal(f.requestList.children.length, 1);
  assert.ok(f.status.classList.contains("is-error"));
  assert.match(f.status.textContent, /may be out of date.*did not reach the gateway/);
});


test("a staged change lists exactly what it changes and offers only its choices", () => {
  const model = reviewCardModel({
    request_id: "access_req_change",
    kind: "change",
    title: "TeamArchitect asks to make a change: Restart manager",
    effect: ["Restarts the supervised processes: manager.", "<b>not markup</b>"],
    created_at_utc: "2026-09-25T10:00:00Z",
    can_decide: true,
    choices: [{ choice: "approve", label: "Approve" }, { choice: "deny", label: "Deny" }],
  }, () => "10:00");
  assert.deepEqual(model.effect, ["Restarts the supervised processes: manager.", "<b>not markup</b>"]);
  assert.deepEqual(model.details.map(row => row.label), ["Asked"]);
  assert.deepEqual(model.choices.map(choice => choice.choice), ["approve", "deny"]);
});

test("an access request row has a Keep for later tasks box, ticked as the agent asked, sent as keep", async () => {
  const rows = [{ item_id: "access", section: "access", text: "WebResearchAgent: full access to Shell commands",
    flags: ["full_access", "runs_code", "unknown_flag"], keepable: true }];
  const f = controllerFixture();
  await loaded(f, { reviews: [review({ rows, working_for_you: true, for_whose_work: "your work" })], notices: [] });
  const card = f.requestList.children[0];
  assert.match(card.textContent, /What you are approving/);
  assert.match(card.textContent, /For whose workyour work/);
  assert.deepEqual(byClass(card, "access-review-flag").map(node => node.textContent), ["Full access", "Runs code"]);
  const [box] = byTag(card, "input");
  assert.equal(box.type, "checkbox");
  assert.equal(box.checked, true, "the row asked to keep the access, so Keep starts ticked");
  assert.match(card.textContent, /Keep for later tasks/);
  assert.match(card.textContent, /whenever WebResearchAgent works for you, until it is removed/);
  box.checked = false;
  byTag(card, "button")[0].click();
  await settle();
  assert.deepEqual(f.posts[0].body, { choice: "approve", keep: false });
  f.posts[0].resolve({ status: "ok", review: { ...review(), status: "granted", can_decide: false, choices: [] } });
  await settle();
  assert.equal(byClass(f.requestList.children[0], "access-inbox-status")[0].textContent, "Approved for this task");
});

test("ticking Keep sends keep true, and a proposal sends the rows it keeps", async () => {
  const f = controllerFixture();
  const proposal = review({
    request_id: "req-p", kind: "change", title: "TeamArchitect asks to make a change: add Portainer",
    rows: [
      { item_id: "tool-1", section: "tool", text: "New tool Portainer at portainer.example.test", flags: ["new_host"] },
      { item_id: "grant-1", section: "access", text: "TeamArchitect: read and change Portainer", keepable: true,
        flags: ["can_hand_on"] },
      { item_id: "grant-2", section: "access", text: "BackgroundOperationsAgent: read and change Portainer",
        keepable: true },
      { item_id: "account-1", section: "account", text: "After approval you choose the credential for Portainer" },
    ],
    choices: [{ choice: "approve", label: "Approve" }, { choice: "deny", label: "Deny" }],
  });
  await loaded(f, { reviews: [proposal], notices: [] });
  const card = f.requestList.children[0];
  const sections = byClass(card, "access-review-row-section").map(node => node.textContent);
  assert.deepEqual(sections, ["New tools", "Access", "Credentials to link"]);
  const boxes = byTag(card, "input");
  assert.equal(boxes.length, 2);
  assert.deepEqual(boxes.map(box => box.checked), [true, true], "each row asked to keep");
  boxes[0].checked = false;
  byTag(card, "button")[0].click();
  await settle();
  assert.deepEqual(f.posts[0].body, { choice: "approve", kept_item_ids: ["grant-2"] });
});

test("after approving a proposal, the credential forms it opened are offered as links", () => {
  const links = followUpLinks({ follow_up_links: [
    { label: "Choose the credential for Portainer", url: "https://broker.example/form/1" },
    { label: "Not safe", url: "javascript:alert(1)" },
  ] });
  assert.deepEqual(links, [{ label: "Choose the credential for Portainer", url: "https://broker.example/form/1" }]);
});

test("kept access can be limited in time, and TeamLead's own request can be handed on", async () => {
  const f = controllerFixture();
  const rows = [{ item_id: "access", section: "access", text: "TeamArchitect: read access to Schedules",
    keepable: true, can_hand_on: true }];
  await loaded(f, { reviews: [review({ agent_type: "TeamArchitect", rows, working_for_you: true })], notices: [] });
  const card = f.requestList.children[0];
  const [keep, handOn] = byTag(card, "input");
  const [duration] = byTag(card, "select");
  assert.equal(duration.disabled, false, "Keep starts ticked, so the duration can be chosen");
  keep.checked = false;
  keep.dispatchEvent(new Event("change"));
  assert.equal(duration.disabled, true, "the duration waits for Keep");
  keep.checked = true;
  keep.dispatchEvent(new Event("change"));
  assert.equal(duration.disabled, false);
  duration.value = "2";
  handOn.checked = true;
  byTag(card, "button")[0].click();
  await settle();
  assert.deepEqual(f.posts[0].body, { choice: "approve", keep: true, keep_hours: 168, hand_on: true });
});

test("TeamLead's hand-on box starts ticked, and unticking it is sent", async () => {
  const f = controllerFixture();
  const rows = [{ item_id: "access", section: "access", text: "TeamArchitect: read access to Schedules",
    keepable: true, can_hand_on: true }];
  await loaded(f, { reviews: [review({ agent_type: "TeamArchitect", rows, working_for_you: true })], notices: [] });
  const card = f.requestList.children[0];
  const [, handOn] = byTag(card, "input");
  assert.equal(handOn.checked, true, "workers get access only from TeamLead, so handing on starts ticked");
  handOn.checked = false;
  byTag(card, "button")[0].click();
  await settle();
  assert.deepEqual(f.posts[0].body, { choice: "approve", keep: true, hand_on: false });
});

test("each choice says what it does, follows the Keep box, and is marked while its button is pointed at", async () => {
  const f = controllerFixture();
  const rows = [{ item_id: "access", section: "access", text: "WebResearchAgent: read access to Schedules", keepable: true }];
  const choice_effects = {
    approve: { task: "WebResearchAgent can use Schedules for this task only.", kept: "TeamArchitect keeps it for you." },
    deny: { any: "Nothing is given." },
  };
  await loaded(f, { reviews: [review({ rows, choice_effects, choice_effects_default: "kept",
    choices: [{ choice: "approve", label: "Approve" }, { choice: "deny", label: "Deny" }] })], notices: [] });
  const card = f.requestList.children[0];
  const lines = () => byClass(card, "access-review-choice-help-item").map(node => node.textContent);
  assert.match(card.textContent, /What each choice does/);
  assert.deepEqual(lines(), ["Approve: TeamArchitect keeps it for you.", "Deny: Nothing is given."]);

  const [keep] = byTag(card, "input");
  keep.checked = false;
  keep.dispatchEvent(new Event("change"));
  assert.deepEqual(lines(), ["Approve: WebResearchAgent can use Schedules for this task only.", "Deny: Nothing is given."]);

  const [approve] = byTag(card, "button");
  const [line] = byClass(card, "access-review-choice-help-item");
  assert.equal(approve.getAttribute("aria-describedby"), line.id, "the button points to its line");
  approve.dispatchEvent(new Event("mouseenter"));
  assert.equal(line.classList.contains("is-active"), true);
  approve.dispatchEvent(new Event("mouseleave"));
  assert.equal(line.classList.contains("is-active"), false);
});

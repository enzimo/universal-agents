// The thread list's sections, rows and snippets (chat_thread_list.mjs): A2, A3.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  LIST_STATE,
  NEEDS_YOU_LABELS,
  SECTION_ORDER,
  THREAD_SECTION,
  createSectionCollapse,
  defaultThread,
  formatThreadAge,
  groupThreadSections,
  isOrdinaryMessage,
  messageSnippet,
  plainSnippet,
  renderThreadSections,
  snoozeChoices,
  threadDetail,
} from "../assistant_gateway/onboarding_web/assets/chat_thread_list.mjs";
import { byClass, fakeDocument } from "./fake_dom.mjs";

const EXAMPLES = JSON.parse(readFileSync(new URL("./fixtures/web_chat_ordinary_messages.json", import.meta.url), "utf8")).rows;
const MINUTE = 60 * 1000;

const thread = (id, fields = {}) => ({ id, sessionKey: id, title: id, lastActivityAt: 1000, ...fields });

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), values };
}

test("sections come in the order Needs you, Pinned, Recent, Snoozed, Dismissed, newest first", () => {
  const threads = [
    thread("dismissed", { listState: LIST_STATE.DISMISSED }),
    thread("recent_old", { lastActivityAt: 10 }),
    thread("snoozed", { listState: LIST_STATE.SNOOZED }),
    thread("pinned", { pinned: true }),
    thread("waiting", { needsYou: "question", listState: LIST_STATE.SNOOZED }),
    thread("recent_new", { lastActivityAt: 20 }),
    thread("woke", { listState: LIST_STATE.WOKE, lastActivityAt: 15 }),
    thread("pinned_snoozed", { pinned: true, listState: LIST_STATE.SNOOZED }),
  ];
  const groups = groupThreadSections(threads);
  assert.deepEqual(groups.map(group => group.id), SECTION_ORDER);
  assert.deepEqual(groups.map(group => group.threads.map(item => item.id)), [
    ["waiting"],
    ["pinned"],
    ["recent_new", "woke", "recent_old"],
    ["snoozed", "pinned_snoozed"],
    ["dismissed"],
  ]);
});

test("only Recent shows when every other section is empty", () => {
  assert.deepEqual(groupThreadSections([]).map(group => group.id), [THREAD_SECTION.RECENT]);
});

test("the default thread is the first visible row, a hidden one only when nothing else exists", () => {
  const hidden = thread("hidden", { listState: LIST_STATE.DISMISSED, lastActivityAt: 99 });
  const pinned = thread("pinned", { pinned: true, lastActivityAt: 1 });
  assert.equal(defaultThread([hidden, thread("recent", { lastActivityAt: 50 }), pinned]), pinned);
  assert.equal(defaultThread([hidden]), hidden);
  assert.equal(defaultThread([]), null);
});

test("sections render with counts, and Snoozed and Dismissed start collapsed and stay as the person left them", () => {
  const document = fakeDocument();
  const container = document.createElement("div");
  const storage = memoryStorage();
  const collapse = createSectionCollapse(storage, "ua.webchat.sections.owner");
  const groups = groupThreadSections([
    thread("a", { needsYou: "setup" }),
    thread("b", { pinned: true }),
    thread("c"),
    thread("d", { listState: LIST_STATE.SNOOZED }),
    thread("e", { listState: LIST_STATE.DISMISSED }),
    thread("f", { listState: LIST_STATE.DISMISSED }),
  ]);
  const toggled = [];
  const draw = () => renderThreadSections({
    document, container, groups, collapse,
    renderRow: item => Object.assign(document.createElement("div"), { className: "thread-row", textContent: item.id }),
    onToggle: id => { toggled.push(id); draw(); },
  });
  draw();
  const headers = byClass(container, "thread-section-header");
  assert.deepEqual(headers.map(header => header.textContent), ["Needs you1", "Pinned1", "Recent1", "Snoozed1", "Dismissed2"]);
  assert.deepEqual(headers.map(header => header.getAttribute("aria-expanded")), ["true", "true", "true", "false", "false"]);
  assert.deepEqual(byClass(container, "thread-section-rows").map(rows => rows.hidden), [false, false, false, true, true]);
  assert.equal(headers[4].getAttribute("aria-label"), "Dismissed, 2 chats");

  headers[4].click();
  assert.deepEqual(toggled, [THREAD_SECTION.DISMISSED]);
  assert.equal(byClass(container, "thread-section-rows")[4].hidden, false);
  // A reload in the same browser keeps the choice.
  const again = createSectionCollapse(storage, "ua.webchat.sections.owner");
  assert.equal(again.isCollapsed(THREAD_SECTION.DISMISSED), false);
  assert.equal(again.isCollapsed(THREAD_SECTION.SNOOZED), true);
});

test("a broken saved choice falls back to the defaults", () => {
  const collapse = createSectionCollapse(memoryStorage({ key: "{not json" }), "key");
  assert.equal(collapse.isCollapsed(THREAD_SECTION.SNOOZED), true);
  assert.equal(collapse.isCollapsed(THREAD_SECTION.RECENT), false);
});

test("the shared example rows are ordinary and give the same snippet the store's text gives", () => {
  for (const row of EXAMPLES) {
    const message = { role: row.role, content: row.content, attachments: (row.attachments || []).map(filename => ({ filename })) };
    assert.equal(isOrdinaryMessage(message), row.ordinary, row.content);
    const live = messageSnippet(message);
    if (!row.ordinary) {
      assert.equal(live, null, row.content);
      continue;
    }
    assert.deepEqual(live, { role: row.role, text: row.snippet }, row.content);
    // The gateway sends the stored text (or the file name) and the page makes the same line from it.
    const stored = row.content.trim() ? row.content : row.attachments[0];
    assert.equal(plainSnippet(stored), row.snippet, row.content);
  }
});

test("long messages are cut to one line", () => {
  const snippet = plainSnippet("word ".repeat(200));
  assert.equal(snippet.length, 160);
  assert.ok(snippet.endsWith("…"));
});

test("a row's second line is the waiting reason, else the latest message with who wrote it", () => {
  assert.deepEqual(threadDetail(thread("a", { needsYou: "credential", lastMessage: { role: "assistant", text: "Hi" } }), "Ava"),
    { kind: "waiting", text: NEEDS_YOU_LABELS.credential });
  assert.deepEqual(threadDetail(thread("b", { lastMessage: { role: "assistant", text: "Draft ready" } }), "Ava"),
    { kind: "message", text: "Ava: Draft ready" });
  assert.deepEqual(threadDetail(thread("c", { lastMessage: { role: "user", text: "Thanks" } }), "Ava"),
    { kind: "message", text: "You: Thanks" });
  assert.equal(threadDetail(thread("d"), "Ava"), null);
  for (const reason of ["question", "reply", "setup", "credential"]) assert.ok(NEEDS_YOU_LABELS[reason]);
});

test("ages read now, minutes, hours, days, weeks and years", () => {
  const now = Date.UTC(2026, 10, 19, 12);
  assert.equal(formatThreadAge(now - 20 * 1000, now), "now");
  assert.equal(formatThreadAge(now - 5 * MINUTE, now), "5m");
  assert.equal(formatThreadAge(now - 3 * 60 * MINUTE, now), "3h");
  assert.equal(formatThreadAge(now - 2 * 24 * 60 * MINUTE, now), "2d");
  assert.equal(formatThreadAge(now - 15 * 24 * 60 * MINUTE, now), "2w");
  assert.equal(formatThreadAge(now - 400 * 24 * 60 * MINUTE, now), "1y");
  assert.equal(formatThreadAge(0, now), "");
});

test("snooze choices are worked out in local time; this evening only before 5 PM", () => {
  const hint = until => until.toISOString();
  const thursdayMorning = new Date(2026, 10, 19, 10, 30);
  const choices = snoozeChoices(thursdayMorning, hint);
  assert.deepEqual(choices.map(choice => choice.id), ["hour", "evening", "tomorrow", "next_week"]);
  const byId = Object.fromEntries(choices.map(choice => [choice.id, choice.until]));
  assert.equal(byId.hour.getTime(), thursdayMorning.getTime() + 60 * MINUTE);
  assert.deepEqual([byId.evening.getDate(), byId.evening.getHours()], [19, 18]);
  assert.deepEqual([byId.tomorrow.getDate(), byId.tomorrow.getHours()], [20, 9]);
  assert.deepEqual([byId.next_week.getDay(), byId.next_week.getDate(), byId.next_week.getHours()], [1, 23, 9]);
  assert.equal(choices[0].hint, "");
  assert.equal(choices[1].hint, byId.evening.toISOString());

  const evening = snoozeChoices(new Date(2026, 10, 19, 17, 5), hint);
  assert.deepEqual(evening.map(choice => choice.id), ["hour", "tomorrow", "next_week"]);
  const monday = snoozeChoices(new Date(2026, 10, 23, 8, 0), hint).find(choice => choice.id === "next_week");
  assert.equal(monday.until.getDate(), 30, "on a Monday, next week is the following Monday");
});

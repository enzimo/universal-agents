// The hover preview (chat_thread_preview.mjs): A9 and plan review C03, C08.
import test from "node:test";
import assert from "node:assert/strict";
import {
  PREVIEW_DELAY_MS,
  createThreadPreview,
  previewTurns,
} from "../assistant_gateway/onboarding_web/assets/chat_thread_preview.mjs";
import { ATTACHMENT_SNIPPET } from "../assistant_gateway/onboarding_web/assets/chat_thread_list.mjs";
import { FakeElement, byClass, fakeDocument, settle } from "./fake_dom.mjs";

FakeElement.prototype.getBoundingClientRect = function () {
  return { left: 10, top: 100, right: 300, bottom: 140, width: 290, height: 40 };
};
FakeElement.prototype.closest = function (selector) {
  for (let node = this; node; node = node.parentNode) if (node.classList && node.classList.contains(selector.slice(1))) return node;
  return null;
};
FakeElement.prototype.querySelectorAll = function (selector) {
  const all = node => node.children.flatMap(child => [child, ...all(child)]);
  return all(this).filter(node => node.classList.contains(selector.slice(1)));
};

const NOW = Date.UTC(2026, 10, 19, 12);
const at = minutes => new Date(NOW - minutes * 60000).toISOString();
const message = (role, content, minutes, extra = {}) => ({ role, content, created_at_utc: at(minutes), attachments: [], ...extra });

function fixture({ pages = {}, canPreview = () => true } = {}) {
  const document = fakeDocument();
  // The card listens for Escape on the document.
  const documentEvents = new EventTarget();
  for (const name of ["addEventListener", "removeEventListener", "dispatchEvent"]) document[name] = documentEvents[name].bind(documentEvents);
  const window = new EventTarget();
  Object.assign(window, { innerWidth: 1280, innerHeight: 880 });
  const timers = new Map();
  let nextTimer = 1;
  const lifecycle = {
    listen: (target, type, handler) => target.addEventListener(type, handler),
    timeout(callback, delay) { const id = nextTimer++; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const runTimers = () => [...timers].forEach(([id, timer]) => { timers.delete(id); timer.callback(); });
  const list = document.createElement("div");
  document.body.appendChild(list);
  const threads = new Map();
  const addRow = (sessionKey, fields = {}) => {
    threads.set(sessionKey, { sessionKey, title: "Plan the team offsite", titleFull: "Plan the team offsite", lastSequence: 12, lastActivityAt: NOW - 24 * 3600000, ...fields });
    const row = document.createElement("div");
    row.className = "thread-row";
    row.dataset.sessionKey = sessionKey;
    const button = document.createElement("button");
    row.appendChild(button);
    list.appendChild(row);
    return row;
  };
  const requests = [];
  const answers = [];
  const requestClient = {
    fetch(url) {
      requests.push(url);
      return new Promise(resolve => answers.push(() => {
        const before = new URL(url, "http://x").searchParams.get("before") || "";
        resolve({ ok: !pages.fail, json: async () => pages[before] || { messages: [], has_more: false, turn_count: 0 } });
      }));
    },
  };
  const preview = createThreadPreview({
    document, window, lifecycle, requestClient, list,
    threadFor: sessionKey => threads.get(sessionKey),
    assistantName: () => "Ava",
    canPreview: sessionKey => canPreview(sessionKey),
    now: () => NOW,
  });
  // This stand-in does not bubble: deliver to the list as if from `element`.
  const fire = (type, element, pointerType = "mouse") => {
    const event = Object.assign(new Event(type), { pointerType });
    Object.defineProperty(event, "target", { value: element });
    list.dispatchEvent(event);
  };
  const over = (element, pointerType = "mouse") => fire("pointerover", element, pointerType);
  const answerAll = async () => { while (answers.length) { answers.shift()(); await settle(); } };
  const card = () => byClass(document.body, "thread-preview")[0];
  return { document, window, list, threads, addRow, requests, runTimers, timers, preview, over, fire, answerAll, card };
}

const FIRST_PAGE = {
  messages: [
    message("user", "Can you find three venues within two hours of the office for 20 people in March?", 1500),
    message("assistant", "Five venues fit; three are within **budget**: Lakeside Lodge, The Old Mill and Pine Hollow.", 1490),
    message("system", "Status: idle", 1485),
    message("user", "/status", 1484),
    message("user", "Yes, and keep the shortlist somewhere the team can see it.", 1480),
    message("assistant", "I saved the venue shortlist to team/offsite.md.", 1470),
  ],
  has_more: false,
  turn_count: 12,
};

test("the last four ordinary turns, oldest first, as plain text", () => {
  const turns = previewTurns([message("user", "first", 9), ...FIRST_PAGE.messages]);
  assert.deepEqual(turns.map(turn => [turn.role, turn.text]), [
    ["user", "Can you find three venues within two hours of the office for 20 people in March?"],
    ["assistant", "Five venues fit; three are within budget: Lakeside Lodge, The Old Mill and Pine Hollow."],
    ["user", "Yes, and keep the shortlist somewhere the team can see it."],
    ["assistant", "I saved the venue shortlist to team/offsite.md."],
  ]);
});

test("after about 400 ms over a row the card shows four turns with who and when and the turn count", async () => {
  const { addRow, requests, timers, runTimers, over, answerAll, card } = fixture({ pages: { "": FIRST_PAGE } });
  const row = addRow("alpha");
  over(row.children[0]);
  assert.equal(card(), undefined, "nothing before the delay");
  assert.deepEqual([...timers.values()].map(timer => timer.delay), [PREVIEW_DELAY_MS]);
  runTimers();
  assert.equal(byClass(card(), "thread-preview-status")[0].textContent, "Loading…");
  assert.deepEqual(requests, ["/api/web-chat/sessions/alpha/messages?limit=12&turn_count=1"], "the existing history route, a small page");
  await answerAll();
  assert.equal(byClass(card(), "thread-preview-title")[0].textContent, "Plan the team offsite");
  assert.equal(byClass(card(), "thread-preview-age")[0].textContent, "active 1d ago");
  assert.deepEqual(byClass(card(), "thread-preview-who").map(node => node.textContent),
    ["You · 1d", "Ava · 1d", "You · 1d", "Ava · 1d"]);
  assert.equal(byClass(card(), "thread-preview-turn").length, 4);
  assert.equal(byClass(card(), "thread-preview-count")[0].textContent, "12 turns");
  assert.equal(card().getAttribute("aria-hidden"), "true");
});

test("the footer shows the thread's agent trail and names the agent types that worked in it (A7)", async () => {
  const { addRow, runTimers, over, answerAll, card } = fixture({ pages: { "": FIRST_PAGE } });
  const trail = {
    entries: [{ by: "person" }, { by: "agent", agent_type: "TeamArchitect", lead: true, name: "Team lead" },
      { by: "agent", agent_type: "WebResearchAgent", lead: false, name: "Web Research Agent" }],
    has_older: true,
    agents: [{ agent_type: "TeamArchitect", lead: true, name: "Team lead" },
      { agent_type: "WebResearchAgent", lead: false, name: "Web Research Agent" },
      { agent_type: "MemReviewAgent", lead: false, name: "Memory Review Agent" }],
  };
  over(addRow("alpha", { trail }).children[0]);
  runTimers();
  await answerAll();
  const [footer] = byClass(card(), "thread-preview-footer");
  assert.deepEqual(footer.children.map(child => child.className), ["thread-trail", "thread-preview-count"]);
  assert.equal(footer.children[0].children.length, 3);
  assert.equal(footer.children[1].textContent, "Team lead, Web Research Agent, Memory Review Agent · 12 turns");
});

test("touch and pen pointers never get a preview", () => {
  const { addRow, timers, over, card } = fixture({ pages: { "": FIRST_PAGE } });
  const row = addRow("alpha");
  over(row.children[0], "touch");
  over(row.children[0], "pen");
  assert.equal(timers.size, 0);
  assert.equal(card(), undefined);
});

test("leaving the list before the delay shows nothing; leaving after hides the card", async () => {
  const { list, addRow, runTimers, over, answerAll, card, requests } = fixture({ pages: { "": FIRST_PAGE } });
  const row = addRow("alpha");
  over(row.children[0]);
  list.dispatchEvent(new Event("pointerleave"));
  runTimers();
  assert.equal(requests.length, 0);
  over(row.children[0]);
  runTimers();
  await answerAll();
  assert.ok(card());
  list.dispatchEvent(new Event("pointerleave"));
  assert.equal(card(), undefined);
});

test("a cached card is shown again until the thread's last message changes", async () => {
  const { list, threads, addRow, runTimers, over, answerAll, requests } = fixture({ pages: { "": FIRST_PAGE } });
  const row = addRow("alpha");
  const hoverOnce = async () => {
    over(row.children[0]);
    runTimers();
    await answerAll();
    list.dispatchEvent(new Event("pointerleave"));
  };
  await hoverOnce();
  await hoverOnce();
  assert.equal(requests.length, 1, "the second hover uses the cache");
  threads.get("alpha").lastSequence = 13;
  await hoverOnce();
  assert.equal(requests.length, 2, "a new message reloads it");
});

test("an answer for a row the pointer has left is dropped", async () => {
  const { addRow, runTimers, over, answerAll, card } = fixture({ pages: { "": FIRST_PAGE } });
  const alpha = addRow("alpha");
  const beta = addRow("beta", { title: "Other", titleFull: "Other" });
  over(alpha.children[0]);
  runTimers();
  over(beta.children[0]);
  await answerAll();
  assert.equal(card(), undefined, "alpha's late answer is not drawn for beta");
  runTimers();
  await answerAll();
  assert.equal(byClass(card(), "thread-preview-title")[0].textContent, "Other");
});

test("the list drawn again between hovering and the delay still previews the same thread (C03)", async () => {
  const { list, addRow, runTimers, over, answerAll, card, preview } = fixture({ pages: { "": FIRST_PAGE } });
  const first = addRow("alpha");
  over(first.children[0]);
  list.replaceChildren();
  const redrawn = addRow("alpha");
  over(redrawn.children[0]);
  runTimers();
  await answerAll();
  assert.ok(card(), "the timer kept running for the same thread");
  // Drawn again with the row still there: the card stays beside it.
  list.replaceChildren();
  addRow("alpha");
  preview.refresh();
  assert.ok(card());
  // Its row disappears (archived elsewhere): the card closes on the next draw.
  list.replaceChildren();
  preview.refresh();
  assert.equal(card(), undefined);
});

test("with fewer than four turns on the first page, one earlier page is read (C08)", async () => {
  const cards = { messages: [message("system", "Card", 5), message("user", "/help", 4), message("assistant", "Latest", 3)], has_more: true, before_cursor: "cursor-1", turn_count: 6 };
  const earlier = { messages: [message("user", "One", 30), message("assistant", "Two", 29), message("user", "Three", 28)], has_more: true, before_cursor: "cursor-2" };
  const { addRow, runTimers, over, answerAll, card, requests } = fixture({ pages: { "": cards, "cursor-1": earlier } });
  const row = addRow("alpha");
  over(row.children[0]);
  runTimers();
  await answerAll();
  assert.deepEqual(requests, [
    "/api/web-chat/sessions/alpha/messages?limit=12&turn_count=1",
    "/api/web-chat/sessions/alpha/messages?limit=12&turn_count=1&before=cursor-1",
  ]);
  assert.deepEqual(byClass(card(), "thread-preview-text").map(node => node.textContent), ["One", "Two", "Three", "Latest"]);
  assert.equal(byClass(card(), "thread-preview-count")[0].textContent, "6 turns");
});

test("a failed read says the preview is not available", async () => {
  const { addRow, runTimers, over, answerAll, card } = fixture({ pages: { fail: true } });
  const row = addRow("alpha");
  over(row.children[0]);
  runTimers();
  await answerAll();
  assert.equal(byClass(card(), "thread-preview-status")[0].textContent, "Preview not available.");
});

test("a thread with something posted in the last minute reads 'active just now' (C31, F12)", async () => {
  const { addRow, runTimers, over, answerAll, card } = fixture({ pages: { "": FIRST_PAGE } });
  const row = addRow("alpha", { lastActivityAt: NOW - 12000 });
  over(row.children[0]);
  runTimers();
  await answerAll();
  assert.equal(byClass(card(), "thread-preview-age")[0].textContent, "active just now");
});

test("the open chat, a row just pressed and rows under an open menu get no card (C30)", async () => {
  let open = "alpha", menuOpen = false;
  const f = fixture({ pages: { "": FIRST_PAGE }, canPreview: key => key !== open && !menuOpen });
  const alpha = f.addRow("alpha");
  const beta = f.addRow("beta");
  f.over(alpha.children[0]);
  f.runTimers();
  assert.equal(f.requests.length, 0, "the chat open beside the list is not previewed");

  // Pressing beta opens it; the list is drawn again under the resting mouse.
  f.over(beta.children[0]);
  f.fire("pointerdown", beta.children[0]);
  open = "beta";
  f.list.replaceChildren();
  const redrawnBeta = f.addRow("beta");
  f.over(redrawnBeta.children[0]);
  f.runTimers();
  assert.equal(f.card(), undefined, "no card comes back over the chat just opened");

  // The menu opens on beta while the mouse rests on alpha.
  open = "";
  menuOpen = true;
  f.list.replaceChildren();
  const redrawnAlpha = f.addRow("alpha");
  f.over(redrawnAlpha.children[0]);
  f.runTimers();
  assert.equal(f.card(), undefined, "no card behind an open menu");
  menuOpen = false;
  f.over(f.addRow("gamma").children[0]);
  f.runTimers();
  await f.answerAll();
  assert.ok(f.card(), "with the menu closed a card shows again");
});

test("sign-out removes the card and forgets the cached text (C29)", async () => {
  const { addRow, runTimers, over, answerAll, card, preview, requests, list } = fixture({ pages: { "": FIRST_PAGE } });
  const row = addRow("alpha");
  over(row.children[0]);
  runTimers();
  await answerAll();
  assert.ok(card());
  preview.clear();
  assert.equal(card(), undefined);
  list.dispatchEvent(new Event("pointerleave"));
  over(row.children[0]);
  runTimers();
  await answerAll();
  assert.equal(requests.length, 2, "a new hover reads the thread again");
});

test("Escape closes the card, and it stays closed until the pointer leaves that row (F7)", async () => {
  const { document, addRow, runTimers, over, answerAll, card, list } = fixture({ pages: { "": FIRST_PAGE } });
  const alpha = addRow("alpha");
  const beta = addRow("beta");
  over(alpha.children[0]);
  runTimers();
  await answerAll();
  assert.ok(card());
  document.dispatchEvent(Object.assign(new Event("keydown"), { key: "Escape" }));
  assert.equal(card(), undefined);
  over(alpha.children[0]);
  runTimers();
  assert.equal(card(), undefined, "still over the same row: no card");
  over(beta.children[0]);
  runTimers();
  await answerAll();
  assert.ok(card(), "another row previews again");
  // A card about to show is cancelled too.
  list.dispatchEvent(new Event("pointerleave"));
  over(alpha.children[0]);
  document.dispatchEvent(Object.assign(new Event("keydown"), { key: "Escape" }));
  runTimers();
  assert.equal(card(), undefined);
});

test("a preview answering after sign-out is not kept in page memory (F15)", async () => {
  const { addRow, runTimers, over, answerAll, card, preview, requests, list } = fixture({ pages: { "": FIRST_PAGE } });
  const row = addRow("alpha");
  over(row.children[0]);
  runTimers();
  assert.equal(requests.length, 1, "the history read is still running");
  preview.clear();
  await answerAll();
  assert.equal(card(), undefined);
  list.dispatchEvent(new Event("pointerleave"));
  over(row.children[0]);
  runTimers();
  assert.equal(requests.length, 2, "nothing was cached: the next person's hover reads again");
});

test("a message with only an unnamed file reads as the list's attachment word (F13)", () => {
  const turns = previewTurns([message("user", "", 3, { attachments: [{ filename: "" }] })]);
  assert.deepEqual(turns.map(turn => turn.text), [ATTACHMENT_SNIPPET]);
});

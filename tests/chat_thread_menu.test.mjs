// The thread menu (chat_thread_menu.mjs): A8 and plan review C03, C04.
import test from "node:test";
import assert from "node:assert/strict";
import {
  LONG_PRESS_MS,
  THREAD_MENU_ACTION,
  attachThreadMenuTriggers,
  createThreadMenu,
  localInputValue,
  threadMenuItems,
} from "../assistant_gateway/onboarding_web/assets/chat_thread_menu.mjs";
import { FakeElement, byClass, byTag, fakeDocument } from "./fake_dom.mjs";

// What the menu needs beyond the shared stand-in: positions and styles.
FakeElement.prototype.getBoundingClientRect = function () {
  return { left: 10, top: 20, right: 190, bottom: 60, width: 180, height: 40 };
};
FakeElement.prototype.closest = function (selector) {
  const names = selector.split(",").map(part => part.trim().slice(1));
  for (let node = this; node; node = node.parentNode) {
    if (node.classList && names.some(name => node.classList.contains(name))) return node;
  }
  return null;
};
Object.defineProperty(FakeElement.prototype, "style", {
  configurable: true,
  get() { return (this._style ||= {}); },
});

const NOW = new Date(2026, 10, 19, 10, 30); // a Thursday morning
const THREAD = { sessionKey: "alpha", title: "Plan the offsite", pinned: false, listState: "" };

function fixture() {
  const document = fakeDocument();
  // The page listens on the document for presses outside the menu.
  const events = new EventTarget();
  for (const name of ["addEventListener", "removeEventListener", "dispatchEvent"]) document[name] = events[name].bind(events);
  const window = new EventTarget();
  Object.assign(window, { innerWidth: 1280, innerHeight: 880 });
  const timers = new Map();
  let nextTimer = 1;
  const lifecycle = {
    listen: (target, type, handler, options) => target.addEventListener(type, handler, options),
    timeout(callback, delay) { const id = nextTimer++; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const runTimers = delay => [...timers].filter(([, timer]) => timer.delay === delay).forEach(([id, timer]) => {
    timers.delete(id);
    timer.callback();
  });
  const actions = [], focused = [];
  // The row the menu gives focus back to is looked up by session key each time.
  const rows = new Map();
  // The thread list the triggers listen on, with one row per session key.
  const list = document.createElement("div");
  document.body.appendChild(list);
  const addRow = sessionKey => {
    const row = document.createElement("div");
    row.className = "thread-row";
    row.dataset.sessionKey = sessionKey;
    const button = document.createElement("button");
    button.className = "thread-button";
    row.appendChild(button);
    list.appendChild(row);
    rows.set(sessionKey, button);
    return row;
  };
  const menu = createThreadMenu({
    document, window, lifecycle, now: () => NOW, threadList: list,
    onAction: (sessionKey, action, detail) => actions.push({ sessionKey, action, detail }),
    focusRow: sessionKey => { focused.push(sessionKey); if (rows.get(sessionKey)) rows.get(sessionKey).focus(); },
  });
  const opened = [];
  const wire = () => attachThreadMenuTriggers(list, (sessionKey, point) => {
    opened.push({ sessionKey, point });
    menu.open({ ...THREAD, sessionKey }, point);
  }, { lifecycle, window });
  // This stand-in does not bubble events: deliver one to the list as if it
  // came from `element`, the way the page's listeners receive it.
  const send = (element, event) => {
    Object.defineProperty(event, "target", { value: element });
    list.dispatchEvent(event);
    return event;
  };
  return { document, window, lifecycle, runTimers, actions, focused, rows, menu, list, addRow, wire, opened, send };
}

const openMenus = document => byClass(document.body, "thread-menu");
const labels = menu => byClass(menu, "thread-menu-item").map(item => byClass(item, "thread-menu-label")[0].textContent);
const keyEvent = (name, extra = {}) => Object.assign(new Event("keydown", { cancelable: true, bubbles: true }), { key: name, ...extra });
const key = (target, name, extra = {}) => {
  const event = keyEvent(name, extra);
  target.dispatchEvent(event);
  return event;
};

test("the menu offers Pin, Snooze, Dismiss, Rename and Delete, or Unpin and Restore", () => {
  assert.deepEqual(threadMenuItems(THREAD, NOW, () => "").map(item => item.label),
    ["Pin to top", "Snooze", "Dismiss", "Rename", "Delete"]);
  assert.deepEqual(threadMenuItems({ ...THREAD, pinned: true, listState: "snoozed" }, NOW, () => "").map(item => item.label),
    ["Unpin", "Snooze", "Restore", "Rename", "Delete"]);
  assert.deepEqual(threadMenuItems({ ...THREAD, listState: "dismissed" }, NOW, () => "")[2].id, THREAD_MENU_ACTION.RESTORE);
  assert.deepEqual(threadMenuItems({ ...THREAD, listState: "woke" }, NOW, () => "")[2].id, THREAD_MENU_ACTION.DISMISS);
  const snooze = threadMenuItems(THREAD, NOW, () => "x")[1];
  assert.deepEqual(snooze.submenu.map(item => item.label),
    ["For 1 hour", "Until this evening", "Until tomorrow", "Until next week", "Pick a time…"]);
  assert.equal(snooze.note, "Comes back early if the team posts.");
});

test("a right-click opens the menu with the first item focused, and Escape closes it back to the row", () => {
  const { document, focused, addRow, wire, send } = fixture();
  const row = addRow("alpha");
  wire();
  const event = new Event("contextmenu", { cancelable: true, bubbles: true });
  Object.assign(event, { clientX: 40, clientY: 50 });
  send(row.children[0], event);
  assert.equal(event.defaultPrevented, true, "the browser's own menu does not open");
  const [opened] = openMenus(document);
  assert.equal(opened.getAttribute("role"), "menu");
  assert.equal(opened.getAttribute("aria-label"), "Chat actions for Plan the offsite");
  assert.deepEqual(labels(opened), ["Pin to top", "Snooze", "Dismiss", "Rename", "Delete"]);
  assert.equal(document.activeElement, byClass(opened, "thread-menu-item")[0]);
  key(opened, "Escape");
  assert.equal(openMenus(document).length, 0);
  assert.deepEqual(focused, ["alpha"]);
});

test("arrow keys, Home and End move through the items, and Enter-style clicks act", () => {
  const { document, actions, menu } = fixture();
  menu.open(THREAD, { x: 0, y: 0 });
  const [opened] = openMenus(document);
  const items = byClass(opened, "thread-menu-item");
  key(opened, "ArrowDown");
  assert.equal(document.activeElement, items[1]);
  key(opened, "ArrowUp");
  key(opened, "ArrowUp");
  assert.equal(document.activeElement, items[4], "moving up from the first item wraps to the last");
  key(opened, "Home");
  assert.equal(document.activeElement, items[0]);
  key(opened, "End");
  assert.equal(document.activeElement, items[4]);
  items[0].click();
  assert.deepEqual(actions, [{ sessionKey: "alpha", action: "pin", detail: {} }]);
  assert.equal(openMenus(document).length, 0);
});

test("Snooze opens its choices with the right arrow; left arrow and Escape go back to Snooze", () => {
  const { document, actions, menu } = fixture();
  menu.open(THREAD, { x: 0, y: 0 });
  const [opened] = openMenus(document);
  const snooze = byClass(opened, "thread-menu-item")[1];
  snooze.focus();
  key(opened, "ArrowRight");
  const submenu = openMenus(document)[1];
  assert.equal(snooze.getAttribute("aria-expanded"), "true");
  assert.deepEqual(labels(submenu), ["For 1 hour", "Until this evening", "Until tomorrow", "Until next week", "Pick a time…"]);
  assert.equal(document.activeElement, byClass(submenu, "thread-menu-item")[0]);
  key(submenu, "ArrowLeft");
  assert.equal(openMenus(document).length, 1);
  assert.equal(document.activeElement, snooze);
  snooze.click();
  key(openMenus(document)[1], "Escape");
  assert.equal(document.activeElement, snooze, "Escape in the choices closes only them");
  snooze.click();
  byClass(openMenus(document)[1], "thread-menu-item")[2].click();
  assert.equal(actions[0].action, "snooze");
  assert.equal(actions[0].detail.until.getTime(), new Date(2026, 10, 20, 9).getTime());
  assert.equal(openMenus(document).length, 0);
});

test("the menu still acts on its thread and finds the row again after the list is drawn again (C03)", () => {
  const { document, actions, focused, rows, menu } = fixture();
  const first = document.createElement("button");
  document.body.appendChild(first);
  rows.set("alpha", first);
  menu.open(THREAD, { x: 0, y: 0 });
  // A message arrives: the list replaces every row.
  first.remove();
  const redrawn = document.createElement("button");
  document.body.appendChild(redrawn);
  rows.set("alpha", redrawn);
  byClass(openMenus(document)[0], "thread-menu-item")[2].click();
  assert.deepEqual(actions.map(item => [item.sessionKey, item.action]), [["alpha", "dismiss"]]);
  assert.deepEqual(focused, ["alpha"]);
  assert.equal(document.activeElement, redrawn);
});

const pointer = (type, fields = {}) => Object.assign(new Event(type, { bubbles: true }),
  { pointerType: "touch", pointerId: 7, clientX: 30, clientY: 40, ...fields });

test("a long press on a touch screen opens the menu once, even with the browser's own contextmenu (C04)", () => {
  const { document, window, runTimers, addRow, wire, opened, send } = fixture();
  const row = addRow("alpha");
  wire();
  send(row.children[0], pointer("pointerdown"));
  runTimers(LONG_PRESS_MS);
  assert.deepEqual(opened.map(item => item.sessionKey), ["alpha"]);
  const contextmenu = Object.assign(new Event("contextmenu", { cancelable: true, bubbles: true }), { clientX: 30, clientY: 40 });
  send(row, contextmenu);
  assert.equal(opened.length, 1, "Android's contextmenu after a long press does not open it again");
  assert.equal(contextmenu.defaultPrevented, true);

  // The finger lifting would also click the row: only that click is dropped,
  // and a tap inside the menu still works (C27).
  window.dispatchEvent(pointer("pointerup"));
  const item = byClass(openMenus(document)[0], "thread-menu-item")[0];
  const inside = new Event("click", { cancelable: true });
  Object.defineProperty(inside, "target", { value: item });
  window.dispatchEvent(inside);
  assert.equal(inside.defaultPrevented, false);
  const onRow = new Event("click", { cancelable: true });
  Object.defineProperty(onRow, "target", { value: row.children[0] });
  window.dispatchEvent(onRow);
  assert.equal(onRow.defaultPrevented, true);
  const later = new Event("click", { cancelable: true });
  Object.defineProperty(later, "target", { value: row.children[0] });
  window.dispatchEvent(later);
  assert.equal(later.defaultPrevented, false, "only one click is dropped");
});

test("a tap while the list is drawn again opens nothing (C27)", () => {
  const { window, runTimers, list, addRow, wire, opened, send } = fixture();
  const first = addRow("alpha");
  wire();
  send(first.children[0], pointer("pointerdown"));
  // A message arrives and every row is replaced before the finger lifts.
  list.replaceChildren();
  const redrawn = addRow("alpha");
  send(redrawn.children[0], pointer("pointerup"));
  window.dispatchEvent(pointer("pointerup"));
  runTimers(LONG_PRESS_MS);
  assert.equal(opened.length, 0);
});

test("a press that moves or ends early, another finger, a mouse press or a press off the rows opens nothing", () => {
  const { window, runTimers, list, addRow, wire, opened, send } = fixture();
  const row = addRow("alpha");
  wire();
  send(row.children[0], pointer("pointerdown"));
  window.dispatchEvent(pointer("pointermove", { clientX: 30, clientY: 80 }));
  runTimers(LONG_PRESS_MS);
  send(row.children[0], pointer("pointerdown"));
  window.dispatchEvent(pointer("pointercancel"));
  runTimers(LONG_PRESS_MS);
  send(row.children[0], pointer("pointerdown", { pointerType: "mouse" }));
  runTimers(LONG_PRESS_MS);
  send(list, pointer("pointerdown"));
  runTimers(LONG_PRESS_MS);
  assert.equal(opened.length, 0);
  // A second finger lifting does not end the first one's press.
  send(row.children[0], pointer("pointerdown"));
  window.dispatchEvent(pointer("pointerup", { pointerId: 8 }));
  runTimers(LONG_PRESS_MS);
  assert.equal(opened.length, 1);
});

test("the keyboard's context-menu key and Shift+F10 open the menu on a focused row", () => {
  const { addRow, wire, opened, send } = fixture();
  const row = addRow("alpha");
  wire();
  assert.equal(send(row.children[0], keyEvent("ContextMenu")).defaultPrevented, true);
  assert.equal(opened.length, 1);
  assert.deepEqual(opened[0].point, { x: 26, y: 60 }, "placed under the row");
  send(row.children[0], keyEvent("F10"));
  assert.equal(opened.length, 1, "F10 alone is not the menu key");
  assert.equal(send(row.children[0], keyEvent("F10", { shiftKey: true })).defaultPrevented, true);
  assert.equal(opened.length, 2);
});

test("a right-click or the keyboard right after closing the menu opens it again", () => {
  const { addRow, wire, opened, send } = fixture();
  const row = addRow("alpha");
  wire();
  const contextmenu = () => Object.assign(new Event("contextmenu", { cancelable: true, bubbles: true }), { clientX: 1, clientY: 1 });
  send(row, contextmenu());
  send(row, keyEvent("F10", { shiftKey: true }));
  send(row, contextmenu());
  assert.equal(opened.length, 3, "only the contextmenu that follows a long press is ignored");
});

test("Pick a time opens a small dialog that refuses a past time and snoozes until the chosen one (C04)", () => {
  const { document, actions, focused, menu } = fixture();
  menu.open(THREAD, { x: 0, y: 0 });
  byClass(openMenus(document)[0], "thread-menu-item")[1].click();
  const pick = byClass(openMenus(document)[1], "thread-menu-item").at(-1);
  pick.click();
  assert.equal(openMenus(document).length, 0, "the menu closes first");
  const [dialog] = byClass(document.body, "thread-snooze-dialog");
  assert.equal(dialog.getAttribute("role"), "dialog");
  const [input] = byClass(dialog, "thread-snooze-input");
  assert.equal(document.activeElement, input);
  assert.equal(input.min, localInputValue(NOW));

  input.value = "2026-11-19T09:00";
  dialog.dispatchEvent(new Event("submit", { cancelable: true }));
  const [message] = byClass(dialog, "thread-snooze-message");
  assert.equal(message.hidden, false);
  assert.equal(message.textContent, "Choose a time in the future.");
  assert.equal(actions.length, 0);

  input.value = "2026-11-21T14:30";
  dialog.dispatchEvent(new Event("submit", { cancelable: true }));
  assert.equal(actions[0].action, "snooze");
  assert.equal(actions[0].detail.until.getTime(), new Date(2026, 10, 21, 14, 30).getTime());
  assert.equal(byClass(document.body, "thread-snooze-dialog").length, 0);
  assert.deepEqual(focused, ["alpha"]);
});

test("Escape closes the time dialog back to the row; a press outside closes the menu", () => {
  const { document, focused, menu } = fixture();
  menu.open(THREAD, { x: 0, y: 0 });
  byClass(openMenus(document)[0], "thread-menu-item")[1].click();
  byClass(openMenus(document)[1], "thread-menu-item").at(-1).click();
  const [dialog] = byClass(document.body, "thread-snooze-dialog");
  key(dialog, "Escape");
  assert.equal(byClass(document.body, "thread-snooze-dialog").length, 0);
  assert.deepEqual(focused, ["alpha"]);

  menu.open(THREAD, { x: 0, y: 0 });
  const outside = document.createElement("div");
  document.body.appendChild(outside);
  const press = new Event("pointerdown");
  Object.defineProperty(press, "target", { value: outside });
  document.dispatchEvent(press);
  assert.equal(menu.isOpen(), false);
});

test("the open chat scrolling leaves the menu open; the thread list scrolling or a resize closes it back to the row (C26)", () => {
  const { document, window, focused, rows, list, addRow, menu } = fixture();
  addRow("alpha");
  const messages = document.createElement("div");
  document.body.appendChild(messages);
  const scroll = target => {
    const event = new Event("scroll");
    Object.defineProperty(event, "target", { value: target });
    document.dispatchEvent(event);
  };
  menu.open(THREAD, { x: 0, y: 0 });
  scroll(messages);
  assert.equal(menu.isOpen(), true, "a message arriving in the open chat does not close it");
  scroll(list);
  assert.equal(menu.isOpen(), false);
  assert.equal(document.activeElement, rows.get("alpha"), "focus goes back to the row");

  menu.open(THREAD, { x: 0, y: 0 });
  window.dispatchEvent(new Event("resize"));
  assert.equal(menu.isOpen(), false);
  assert.equal(document.activeElement, rows.get("alpha"));
  assert.deepEqual(focused, ["alpha", "alpha"]);
});

test("Tab closes the menu and gives focus back to the row, so the page moves on from there (C26)", () => {
  const { document, rows, addRow, menu } = fixture();
  addRow("alpha");
  menu.open(THREAD, { x: 0, y: 0 });
  const tab = key(openMenus(document)[0], "Tab");
  assert.equal(tab.defaultPrevented, false, "the browser still moves focus on");
  assert.equal(menu.isOpen(), false);
  assert.equal(document.activeElement, rows.get("alpha"));
});

test("switching to another app closes the menu but keeps Pick a time and what was chosen (F17)", () => {
  const { document, window, menu } = fixture();
  menu.open(THREAD, { x: 0, y: 0 });
  window.dispatchEvent(new Event("blur"));
  assert.equal(menu.isOpen(), false, "the menu closes");
  menu.open(THREAD, { x: 0, y: 0 });
  byClass(openMenus(document)[0], "thread-menu-item")[1].click();
  byClass(openMenus(document)[1], "thread-menu-item").at(-1).click();
  const [input] = byClass(document.body, "thread-snooze-input");
  input.value = "2026-11-21T14:30";
  window.dispatchEvent(new Event("blur"));
  assert.equal(byClass(document.body, "thread-snooze-dialog").length, 1, "the dialog stays");
  assert.equal(byClass(document.body, "thread-snooze-input")[0].value, "2026-11-21T14:30");
});

test("Tab and Shift+Tab stay in the Pick a time dialog through its guards (F17, C03)", () => {
  const { document, menu } = fixture();
  menu.open(THREAD, { x: 0, y: 0 });
  byClass(openMenus(document)[0], "thread-menu-item")[1].click();
  byClass(openMenus(document)[1], "thread-menu-item").at(-1).click();
  const [dialog] = byClass(document.body, "thread-snooze-dialog");
  const guards = byClass(dialog, "thread-snooze-guard");
  assert.equal(guards.length, 2);
  assert.equal(dialog.children[0], guards[0], "one guard before the controls");
  assert.equal(dialog.children.at(-1), guards[1], "one after them");
  assert.ok(guards.every(guard => guard.tabIndex === 0));
  const [input] = byClass(dialog, "thread-snooze-input");
  const submit = byTag(dialog, "button").find(button => button.textContent === "Snooze");
  // Tab past Snooze lands on the end guard, which sends focus to the field.
  guards[1].dispatchEvent(new Event("focus"));
  assert.equal(document.activeElement, input);
  // Shift+Tab before the field lands on the start guard, which sends it to Snooze.
  guards[0].dispatchEvent(new Event("focus"));
  assert.equal(document.activeElement, submit);
});

test("each Snooze choice is described by the note, which arrow keys never reach (F18)", () => {
  const { document, menu } = fixture();
  menu.open(THREAD, { x: 0, y: 0 });
  byClass(openMenus(document)[0], "thread-menu-item")[1].click();
  const submenu = openMenus(document)[1];
  const [note] = byClass(submenu, "thread-menu-note");
  assert.equal(note.textContent, "Comes back early if the team posts.");
  assert.ok(note.id);
  for (const item of byClass(submenu, "thread-menu-item")) assert.equal(item.getAttribute("aria-describedby"), note.id);
  assert.equal(byClass(openMenus(document)[0], "thread-menu-item")[0].getAttribute("aria-describedby"), null);
});

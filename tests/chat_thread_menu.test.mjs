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
import { FakeElement, byClass, fakeDocument } from "./fake_dom.mjs";

// What the menu needs beyond the shared stand-in: positions and styles.
FakeElement.prototype.getBoundingClientRect = function () {
  return { left: 10, top: 20, right: 190, bottom: 60, width: 180, height: 40 };
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
  const menu = createThreadMenu({
    document, window, lifecycle, now: () => NOW,
    onAction: (sessionKey, action, detail) => actions.push({ sessionKey, action, detail }),
    focusRow: sessionKey => { focused.push(sessionKey); if (rows.get(sessionKey)) rows.get(sessionKey).focus(); },
  });
  return { document, window, lifecycle, runTimers, actions, focused, rows, menu };
}

const openMenus = document => byClass(document.body, "thread-menu");
const labels = menu => byClass(menu, "thread-menu-item").map(item => byClass(item, "thread-menu-label")[0].textContent);
const key = (target, name, extra = {}) => {
  const event = new Event("keydown", { cancelable: true, bubbles: true });
  Object.assign(event, { key: name, ...extra });
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
  const { document, lifecycle, window, focused, menu } = fixture();
  const row = document.createElement("div");
  document.body.appendChild(row);
  attachThreadMenuTriggers(row, point => menu.open(THREAD, point), { lifecycle, window });
  const event = new Event("contextmenu", { cancelable: true });
  Object.assign(event, { clientX: 40, clientY: 50 });
  row.dispatchEvent(event);
  assert.equal(event.defaultPrevented, true, "the browser's own menu does not open");
  const [opened] = openMenus(document);
  assert.equal(opened.getAttribute("role"), "menu");
  assert.equal(opened.getAttribute("aria-label"), "Chat actions for Plan the offsite");
  assert.deepEqual(labels(opened), ["Pin to top", "Snooze", "Dismiss", "Rename", "Delete"]);
  assert.equal(document.activeElement, byClass(opened, "thread-menu-item")[0]);
  assert.equal(menu.openFor(), "alpha");
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

test("a long press on a touch screen opens the menu once, even with the browser's own contextmenu (C04)", () => {
  const { document, lifecycle, window, runTimers, menu } = fixture();
  const row = document.createElement("div");
  let opens = 0;
  attachThreadMenuTriggers(row, point => { opens += 1; menu.open(THREAD, point); }, { lifecycle, window });
  const pointer = (type, fields) => Object.assign(new Event(type), { pointerType: "touch", clientX: 30, clientY: 40, ...fields });
  row.dispatchEvent(pointer("pointerdown"));
  runTimers(LONG_PRESS_MS);
  assert.equal(opens, 1);
  const contextmenu = Object.assign(new Event("contextmenu", { cancelable: true }), { clientX: 30, clientY: 40 });
  row.dispatchEvent(contextmenu);
  assert.equal(opens, 1, "Android's contextmenu after a long press does not open it again");
  assert.equal(contextmenu.defaultPrevented, true);

  // The finger lifting would also click the row: that click is dropped.
  const click = new Event("click", { cancelable: true });
  window.dispatchEvent(click);
  assert.equal(click.defaultPrevented, true);
});

test("a press that moves or ends early, or a mouse press, opens nothing", () => {
  const { document, lifecycle, window, runTimers } = fixture();
  const row = document.createElement("div");
  let opens = 0;
  attachThreadMenuTriggers(row, () => { opens += 1; }, { lifecycle, window });
  const pointer = (type, fields = {}) => Object.assign(new Event(type), { pointerType: "touch", clientX: 30, clientY: 40, ...fields });
  row.dispatchEvent(pointer("pointerdown"));
  row.dispatchEvent(pointer("pointermove", { clientX: 30, clientY: 80 }));
  runTimers(LONG_PRESS_MS);
  row.dispatchEvent(pointer("pointerdown"));
  row.dispatchEvent(pointer("pointerup"));
  runTimers(LONG_PRESS_MS);
  row.dispatchEvent(pointer("pointerdown", { pointerType: "mouse" }));
  runTimers(LONG_PRESS_MS);
  assert.equal(opens, 0);
});

test("the keyboard's context-menu key and Shift+F10 open the menu on a focused row", () => {
  const { document, lifecycle, window } = fixture();
  const row = document.createElement("div");
  const points = [];
  attachThreadMenuTriggers(row, point => points.push(point), { lifecycle, window });
  assert.equal(key(row, "ContextMenu").defaultPrevented, true);
  assert.equal(points.length, 1);
  assert.deepEqual(points[0], { x: 26, y: 60 }, "placed under the row");
  key(row, "F10");
  assert.equal(points.length, 1, "F10 alone is not the menu key");
});

test("a right-click or the keyboard right after closing the menu opens it again", () => {
  const { document, lifecycle, window } = fixture();
  const row = document.createElement("div");
  let opens = 0;
  attachThreadMenuTriggers(row, () => { opens += 1; }, { lifecycle, window });
  const contextmenu = () => Object.assign(new Event("contextmenu", { cancelable: true }), { clientX: 1, clientY: 1 });
  row.dispatchEvent(contextmenu());
  key(row, "F10", { shiftKey: true });
  row.dispatchEvent(contextmenu());
  assert.equal(opens, 3, "only the contextmenu that follows a long press is ignored");
});

test("Shift+F10 opens it too", () => {
  const { document, lifecycle, window } = fixture();
  const row = document.createElement("div");
  let opens = 0;
  attachThreadMenuTriggers(row, () => { opens += 1; }, { lifecycle, window });
  assert.equal(key(row, "F10", { shiftKey: true }).defaultPrevented, true);
  assert.equal(opens, 1);
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

// The thread list's menu: Pin or Unpin, Snooze (with its choices), Dismiss or
// Restore, Rename and Delete for one thread.
//
// It opens with a right-click, a long press on touch screens, and the
// keyboard's context-menu key or Shift+F10 on a focused row. It is keyed by the
// thread's session key, not by a row element: the list is drawn again on many
// events, so the menu lives in the page body and finds the row again when it
// gives focus back. "Pick a time…" closes the menu and opens a small dialog,
// because a date field inside a menu would fight the menu's keys.

import { LIST_STATE, snoozeChoices } from "./chat_thread_list.mjs";

export const THREAD_MENU_ACTION = Object.freeze({
  PIN: "pin",
  UNPIN: "unpin",
  SNOOZE: "snooze",
  DISMISS: "dismiss",
  RESTORE: "restore",
  RENAME: "rename",
  DELETE: "delete",
});

// How long a finger rests on a row before the menu opens, and how far it may move.
export const LONG_PRESS_MS = 500;
const LONG_PRESS_SLOP_PX = 10;
// The browser's own contextmenu event that Android sends after a long press
// is ignored for this long; right-clicks and the keyboard always open.
const SAME_GESTURE_MS = 800;
const VIEWPORT_MARGIN_PX = 8;

// After a long press opens the menu, the click its own finger makes on
// release is dropped for this long (it would select the row under it).
const RELEASE_CLICK_MS = 400;

const ROW_SELECTOR = ".thread-row";
const MENU_SELECTOR = ".thread-menu, .thread-snooze-dialog";

// Wire the ways a row opens its menu, once for the whole list: the list is
// drawn again on many events, so a press is tracked by the row's session key
// and its pointer id, never by a row element (plan review C27).
// `open(sessionKey, point)` receives the thread and where to place the menu.
export function attachThreadMenuTriggers(list, open, { lifecycle, window }) {
  let press = null; // { pointerId, sessionKey, x, y, timer, opened }
  let longPressAt = 0;
  const rowFor = event => {
    const row = event.target && typeof event.target.closest === "function" ? event.target.closest(ROW_SELECTOR) : null;
    return row && row.dataset.sessionKey ? row : null;
  };
  const endPress = () => {
    if (press && press.timer) lifecycle.clearTimeout(press.timer);
    press = null;
  };
  lifecycle.listen(list, "contextmenu", event => {
    const row = rowFor(event);
    if (!row) return;
    event.preventDefault();
    // Android sends its own contextmenu after a long press already opened it.
    if (Date.now() - longPressAt < SAME_GESTURE_MS) return;
    open(row.dataset.sessionKey, { x: event.clientX, y: event.clientY });
  });
  lifecycle.listen(list, "pointerdown", event => {
    const row = rowFor(event);
    if (event.pointerType !== "touch" || !row) return;
    endPress();
    const current = { pointerId: event.pointerId, sessionKey: row.dataset.sessionKey, x: event.clientX, y: event.clientY, timer: 0, opened: false };
    current.timer = lifecycle.timeout(() => {
      current.timer = 0;
      current.opened = true;
      longPressAt = Date.now();
      open(current.sessionKey, { x: current.x, y: current.y });
    }, LONG_PRESS_MS);
    press = current;
  });
  lifecycle.listen(window, "pointermove", event => {
    if (!press || event.pointerId !== press.pointerId || press.opened) return;
    if (Math.hypot(event.clientX - press.x, event.clientY - press.y) > LONG_PRESS_SLOP_PX) endPress();
  });
  ["pointerup", "pointercancel"].forEach(type => lifecycle.listen(window, type, event => {
    if (!press || event.pointerId !== press.pointerId) return;
    if (press.opened && type === "pointerup") dropReleaseClick(window, lifecycle);
    endPress();
  }));
  lifecycle.listen(list, "keydown", event => {
    if (event.key !== "ContextMenu" && !(event.key === "F10" && event.shiftKey)) return;
    const row = rowFor(event);
    if (!row) return;
    event.preventDefault();
    const rect = row.getBoundingClientRect();
    open(row.dataset.sessionKey, { x: rect.left + 16, y: rect.bottom });
  });
}

// The finger lifting after a long press would also "click" the row under it.
// Only that click is dropped: it is armed on that pointer's release, lasts
// RELEASE_CLICK_MS, and never touches a click inside the menu.
function dropReleaseClick(window, lifecycle) {
  const swallow = event => {
    const target = event.target;
    if (target && typeof target.closest === "function" && target.closest(MENU_SELECTOR)) return;
    event.preventDefault();
    event.stopPropagation();
    window.removeEventListener("click", swallow, { capture: true });
  };
  window.addEventListener("click", swallow, { capture: true });
  lifecycle.timeout(() => window.removeEventListener("click", swallow, { capture: true }), RELEASE_CLICK_MS);
}

// The items for one thread, in menu order. Snoozed and dismissed threads get
// Restore; every other thread gets Dismiss.
export function threadMenuItems(thread, now, formatHint) {
  const hidden = thread.listState === LIST_STATE.SNOOZED || thread.listState === LIST_STATE.DISMISSED;
  return [
    thread.pinned
      ? { id: THREAD_MENU_ACTION.UNPIN, label: "Unpin" }
      : { id: THREAD_MENU_ACTION.PIN, label: "Pin to top" },
    {
      id: THREAD_MENU_ACTION.SNOOZE,
      label: "Snooze",
      submenu: [
        ...snoozeChoices(now, formatHint).map(choice => ({ id: THREAD_MENU_ACTION.SNOOZE, label: choice.label, hint: choice.hint, until: choice.until })),
        { id: "pick_time", label: "Pick a time…", separatorBefore: true },
      ],
      note: "Comes back early if the team posts.",
    },
    hidden
      ? { id: THREAD_MENU_ACTION.RESTORE, label: "Restore" }
      : { id: THREAD_MENU_ACTION.DISMISS, label: "Dismiss" },
    { id: THREAD_MENU_ACTION.RENAME, label: "Rename", separatorBefore: true },
    { id: THREAD_MENU_ACTION.DELETE, label: "Delete", danger: true },
  ];
}

// "6 PM", "Fri 9 AM": when a snooze choice ends, in the person's own time.
export function snoozeHint(until, now) {
  const sameDay = until.toDateString() === now.toDateString();
  const time = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: until.getMinutes() ? "2-digit" : undefined }).format(until);
  if (sameDay) return time;
  return new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(until) + " " + time;
}

// The value a datetime-local field needs for a moment, in local time.
export function localInputValue(date) {
  const pad = value => String(value).padStart(2, "0");
  return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate())
    + "T" + pad(date.getHours()) + ":" + pad(date.getMinutes());
}

// `threadList` is the element whose scrolling moves the rows the menu sits beside.
export function createThreadMenu({ document, window, lifecycle, onAction, focusRow, threadList, now = () => new Date() }) {
  let state = null; // { sessionKey, menu, submenu, dialog }

  function close({ restoreFocus = true } = {}) {
    if (!state) return;
    const { sessionKey } = state;
    [state.menu, state.submenu, state.dialog].forEach(node => node && node.remove());
    state = null;
    if (restoreFocus) focusRow(sessionKey);
  }

  function isOpen() {
    return Boolean(state);
  }

  function openFor() {
    return state ? state.sessionKey : "";
  }

  function choose(sessionKey, item) {
    if (item.id === "pick_time") {
      const anchor = state.menu.getBoundingClientRect();
      close({ restoreFocus: false });
      openTimeDialog(sessionKey, { x: anchor.left, y: anchor.top });
      return;
    }
    close();
    onAction(sessionKey, item.id, item.until ? { until: item.until } : {});
  }

  function itemButton(sessionKey, item, list) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "thread-menu-item" + (item.danger ? " is-danger" : "") + (item.submenu ? " has-submenu" : "");
    button.setAttribute("role", "menuitem");
    button.tabIndex = -1;
    const label = document.createElement("span");
    label.className = "thread-menu-label";
    label.textContent = item.label;
    button.appendChild(label);
    if (item.hint) {
      const hint = document.createElement("span");
      hint.className = "thread-menu-hint";
      hint.textContent = item.hint;
      button.appendChild(hint);
    }
    if (item.submenu) {
      button.setAttribute("aria-haspopup", "menu");
      button.setAttribute("aria-expanded", "false");
      lifecycle.listen(button, "click", () => openSubmenu(sessionKey, item, button));
    } else {
      lifecycle.listen(button, "click", () => choose(sessionKey, item));
    }
    if (item.separatorBefore && list.children.length) {
      const separator = document.createElement("div");
      separator.className = "thread-menu-separator";
      separator.setAttribute("role", "separator");
      list.appendChild(separator);
    }
    list.appendChild(button);
    return button;
  }

  function menuElement(label) {
    const menu = document.createElement("div");
    menu.className = "thread-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", label);
    return menu;
  }

  function items(menu) {
    return [...menu.children].filter(child => child.getAttribute("role") === "menuitem");
  }

  function moveFocus(menu, step) {
    const all = items(menu);
    const index = all.indexOf(document.activeElement);
    const next = step === "first" ? 0 : step === "last" ? all.length - 1 : (index + step + all.length) % all.length;
    if (all[next]) all[next].focus();
  }

  function handleKeys(menu, isSubmenu) {
    lifecycle.listen(menu, "keydown", event => {
      const keys = {
        ArrowDown: () => moveFocus(menu, 1),
        ArrowUp: () => moveFocus(menu, -1),
        Home: () => moveFocus(menu, "first"),
        End: () => moveFocus(menu, "last"),
        Escape: () => (isSubmenu ? closeSubmenu() : close()),
        ArrowLeft: () => isSubmenu && closeSubmenu(),
        ArrowRight: () => {
          const active = document.activeElement;
          if (!isSubmenu && active && active.classList.contains("has-submenu")) active.click();
        },
        // Focus goes back to the row first, so Tab moves on from there.
        Tab: () => close(),
      };
      const action = keys[event.key];
      if (!action) return;
      if (event.key !== "Tab") event.preventDefault();
      action();
    });
  }

  function place(menu, point, { beside = null } = {}) {
    document.body.appendChild(menu);
    const rect = menu.getBoundingClientRect();
    let left = point.x;
    let top = point.y;
    if (beside) {
      left = beside.right + 4;
      if (left + rect.width > window.innerWidth - VIEWPORT_MARGIN_PX) left = beside.left - rect.width - 4;
    }
    left = Math.max(VIEWPORT_MARGIN_PX, Math.min(left, window.innerWidth - rect.width - VIEWPORT_MARGIN_PX));
    top = Math.max(VIEWPORT_MARGIN_PX, Math.min(top, window.innerHeight - rect.height - VIEWPORT_MARGIN_PX));
    menu.style.left = left + "px";
    menu.style.top = top + "px";
  }

  function openSubmenu(sessionKey, item, owner) {
    closeSubmenu({ focusOwner: false });
    const submenu = menuElement(item.label);
    submenu.classList.add("is-submenu");
    item.submenu.forEach(child => itemButton(sessionKey, child, submenu));
    if (item.note) {
      const note = document.createElement("p");
      note.className = "thread-menu-note";
      note.textContent = item.note;
      submenu.appendChild(note);
    }
    handleKeys(submenu, true);
    state.submenu = submenu;
    state.submenuOwner = owner;
    owner.setAttribute("aria-expanded", "true");
    place(submenu, { x: 0, y: owner.getBoundingClientRect().top - 4 }, { beside: state.menu.getBoundingClientRect() });
    moveFocus(submenu, "first");
  }

  function closeSubmenu({ focusOwner = true } = {}) {
    if (!state || !state.submenu) return;
    state.submenu.remove();
    state.submenu = null;
    state.submenuOwner.setAttribute("aria-expanded", "false");
    if (focusOwner) state.submenuOwner.focus();
  }

  // Open the menu for one thread at `point`. Opening it again moves it.
  function open(thread, point) {
    close({ restoreFocus: false });
    const menu = menuElement("Chat actions for " + thread.title);
    state = { sessionKey: thread.sessionKey, menu, submenu: null, dialog: null };
    threadMenuItems(thread, now(), snoozeHint).forEach(item => itemButton(thread.sessionKey, item, menu));
    handleKeys(menu, false);
    place(menu, point);
    moveFocus(menu, "first");
  }

  // "Pick a time…": a small labelled dialog with a date and time field.
  function openTimeDialog(sessionKey, point) {
    const dialog = document.createElement("form");
    dialog.className = "thread-snooze-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-label", "Snooze until");
    const label = document.createElement("label");
    label.className = "thread-snooze-label";
    label.textContent = "Snooze until";
    const input = document.createElement("input");
    input.type = "datetime-local";
    input.className = "thread-snooze-input";
    input.required = true;
    const start = now();
    input.min = localInputValue(start);
    input.value = localInputValue(new Date(start.getTime() + 60 * 60 * 1000));
    label.appendChild(input);
    const message = document.createElement("p");
    message.className = "thread-snooze-message";
    message.hidden = true;
    const actions = document.createElement("div");
    actions.className = "thread-snooze-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "button secondary small";
    cancel.textContent = "Cancel";
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "button primary small";
    submit.textContent = "Snooze";
    actions.append(cancel, submit);
    dialog.append(label, message, actions);
    state = { sessionKey, menu: null, submenu: null, dialog };
    lifecycle.listen(cancel, "click", () => close());
    lifecycle.listen(dialog, "keydown", event => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    });
    lifecycle.listen(dialog, "submit", event => {
      event.preventDefault();
      const until = new Date(input.value);
      if (!input.value || Number.isNaN(until.getTime()) || until <= now()) {
        message.textContent = "Choose a time in the future.";
        message.hidden = false;
        message.setAttribute("role", "alert");
        return;
      }
      close();
      onAction(sessionKey, THREAD_MENU_ACTION.SNOOZE, { until });
    });
    place(dialog, point);
    input.focus();
  }

  const holdsFocus = () => [state.menu, state.submenu, state.dialog]
    .some(node => node && node.contains(document.activeElement));
  // Closing for a reason the person did not point at gives focus back to the
  // row when it was in the menu, so keyboard users are not left on the page.
  const closeKeepingFocus = () => {
    if (state) close({ restoreFocus: holdsFocus() });
  };
  // A press outside closes it and leaves focus where the person pressed.
  lifecycle.listen(document, "pointerdown", event => {
    if (!state) return;
    const inside = [state.menu, state.submenu, state.dialog].some(node => node && node.contains(event.target));
    if (!inside) close({ restoreFocus: false });
  }, { capture: true });
  lifecycle.listen(window, "resize", closeKeepingFocus);
  lifecycle.listen(window, "blur", closeKeepingFocus);
  // Only the thread list scrolling (or the page around it) leaves the menu
  // beside the wrong row; the open chat scrolls itself as messages arrive,
  // and that must not close it (plan review C26).
  lifecycle.listen(document, "scroll", event => {
    if (!state || !threadList) return;
    const target = event.target;
    const movesList = target === document || target === threadList
      || (target && typeof target.contains === "function" && target.contains(threadList));
    if (movesList) closeKeepingFocus();
  }, { capture: true });

  return { open, close, isOpen, openFor };
}

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

// Wire the ways a row opens its menu. `open(point)` receives where to place it.
export function attachThreadMenuTriggers(element, open, { lifecycle, window }) {
  let pressTimer = 0;
  let pressStart = null;
  let longPressAt = 0;
  const cancelPress = () => {
    if (pressTimer) lifecycle.clearTimeout(pressTimer);
    pressTimer = 0;
    pressStart = null;
  };
  lifecycle.listen(element, "contextmenu", event => {
    event.preventDefault();
    if (Date.now() - longPressAt < SAME_GESTURE_MS) return;
    open({ x: event.clientX, y: event.clientY });
  });
  lifecycle.listen(element, "pointerdown", event => {
    if (event.pointerType !== "touch") return;
    cancelPress();
    pressStart = { x: event.clientX, y: event.clientY };
    pressTimer = lifecycle.timeout(() => {
      const point = pressStart;
      cancelPress();
      longPressAt = Date.now();
      open(point);
      suppressNextClick(window, lifecycle);
    }, LONG_PRESS_MS);
  });
  lifecycle.listen(element, "pointermove", event => {
    if (!pressStart) return;
    if (Math.hypot(event.clientX - pressStart.x, event.clientY - pressStart.y) > LONG_PRESS_SLOP_PX) cancelPress();
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach(type => lifecycle.listen(element, type, cancelPress));
  lifecycle.listen(element, "keydown", event => {
    if (event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey)) {
      event.preventDefault();
      const rect = element.getBoundingClientRect();
      open({ x: rect.left + 16, y: rect.bottom });
    }
  });
}

// The finger lifting after a long press would also "click" the row (and may
// land on a row drawn again meanwhile), so the next click anywhere is dropped.
function suppressNextClick(window, lifecycle) {
  const swallow = event => {
    event.preventDefault();
    event.stopPropagation();
  };
  window.addEventListener("click", swallow, { capture: true, once: true });
  lifecycle.timeout(() => window.removeEventListener("click", swallow, { capture: true }), SAME_GESTURE_MS);
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

export function createThreadMenu({ document, window, lifecycle, onAction, focusRow, now = () => new Date() }) {
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
        Tab: () => close({ restoreFocus: false }),
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

  // A press outside the menu or dialog, or the page scrolling or resizing,
  // closes it; focus stays where the person put it.
  lifecycle.listen(document, "pointerdown", event => {
    if (!state) return;
    const inside = [state.menu, state.submenu, state.dialog].some(node => node && node.contains(event.target));
    if (!inside) close({ restoreFocus: false });
  }, { capture: true });
  lifecycle.listen(window, "resize", () => close({ restoreFocus: false }));
  // Scrolling the list or the page would leave the menu beside the wrong row.
  lifecycle.listen(document, "scroll", event => {
    if (state && ![state.menu, state.submenu, state.dialog].some(node => node && node.contains(event.target))) {
      close({ restoreFocus: false });
    }
  }, { capture: true });
  lifecycle.listen(window, "blur", () => close({ restoreFocus: false }));

  return { open, close, isOpen, openFor };
}

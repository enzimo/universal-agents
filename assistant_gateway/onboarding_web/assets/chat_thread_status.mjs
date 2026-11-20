// The thread list's status line: what happened to a change the person made
// from a thread's menu (pin, snooze, dismiss, restore, delete).
//
// It sits above the list, outside the part that scrolls, so a failure is in
// view where the person was working, not at the top of a long open chat
// (review F5, ui/DESIGN.md). The element is a polite live region that exists
// from page load, so screen readers read what is put in it. A failure is
// shown in the danger colour with Close and, when repeating the same change
// can help, Retry; it stays until the person closes it, another change ends,
// or they sign out (no timer). A change that worked is only announced, in
// visually hidden text, because keyboard focus moves away from a row that
// moved and the person should hear why.

import { THREAD_MENU_ACTION } from "./chat_thread_menu.mjs";

const ERROR_CLASS = "is-error";

// What is announced after each change worked, before the chat's name.
export const THREAD_CHANGE_DONE = Object.freeze({
  [THREAD_MENU_ACTION.PIN]: "Pinned",
  [THREAD_MENU_ACTION.UNPIN]: "Unpinned",
  [THREAD_MENU_ACTION.SNOOZE]: "Snoozed",
  [THREAD_MENU_ACTION.DISMISS]: "Dismissed",
  [THREAD_MENU_ACTION.RESTORE]: "Restored",
  [THREAD_MENU_ACTION.DELETE]: "Deleted",
});
// How a failure that did not get through ends, and an unexplained refusal.
export const TRY_AGAIN = "Try again.";

// `element` is the live region; `restoreFocus()` is called after Close or
// Retry, whose own button disappears with the message.
export function createThreadListStatus({ document, lifecycle, element, restoreFocus = () => {} }) {
  function clear() {
    element.replaceChildren();
    element.classList.remove(ERROR_CLASS);
  }

  function button(label, onPress) {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "button secondary small";
    node.textContent = label;
    lifecycle.listen(node, "click", () => {
      clear();
      restoreFocus();
      onPress();
    });
    return node;
  }

  // A change that did not happen. `retry` is given only when the change did
  // not get through; a refusal says why, and repeating it cannot help.
  function fail(message, { retry = null } = {}) {
    clear();
    element.classList.add(ERROR_CLASS);
    const text = document.createElement("span");
    text.className = "thread-list-status-text";
    text.textContent = message;
    element.appendChild(text);
    if (retry) element.appendChild(button("Retry", retry));
    element.appendChild(button("Close", () => {}));
  }

  // A change that worked, for screen readers only.
  function announce(message) {
    clear();
    const text = document.createElement("span");
    text.className = "sr-only";
    text.textContent = message;
    element.appendChild(text);
  }

  return { fail, announce, clear };
}

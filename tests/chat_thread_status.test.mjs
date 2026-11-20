// The thread list's status line (chat_thread_status.mjs): review F5.
import test from "node:test";
import assert from "node:assert/strict";
import { THREAD_CHANGE_DONE, TRY_AGAIN, createThreadListStatus } from "../assistant_gateway/onboarding_web/assets/chat_thread_status.mjs";
import { THREAD_MENU_ACTION } from "../assistant_gateway/onboarding_web/assets/chat_thread_menu.mjs";
import { buttonLabels, byTag, fakeDocument } from "./fake_dom.mjs";

function fixture() {
  const document = fakeDocument();
  const element = document.createElement("div");
  document.body.appendChild(element);
  let focused = 0;
  const status = createThreadListStatus({
    document,
    lifecycle: { listen: (target, type, handler) => target.addEventListener(type, handler) },
    element,
    restoreFocus: () => { focused += 1; },
  });
  return { status, element, focused: () => focused };
}

test("a change that did not get through is shown with Retry and Close", () => {
  const { status, element, focused } = fixture();
  let retried = 0;
  status.fail("“Check the backups” was not moved. Try again.", { retry: () => { retried += 1; } });
  assert.equal(element.classList.contains("is-error"), true);
  assert.equal(element.children[0].textContent, "“Check the backups” was not moved. Try again.");
  assert.deepEqual(buttonLabels(element), ["Retry", "Close"]);
  byTag(element, "button")[0].click();
  assert.equal(retried, 1);
  assert.equal(element.children.length, 0, "Retry clears the message");
  assert.equal(element.classList.contains("is-error"), false);
  assert.equal(focused(), 1, "focus goes back to the list, not the page");
});

test("a refusal has no Retry, and Close clears it", () => {
  const { status, element, focused } = fixture();
  status.fail("“Check the backups” was not moved. Thread not found.");
  assert.deepEqual(buttonLabels(element), ["Close"]);
  byTag(element, "button")[0].click();
  assert.equal(element.children.length, 0);
  assert.equal(focused(), 1);
});

test("a change that worked is announced in hidden text and replaces a failure", () => {
  const { status, element } = fixture();
  status.fail("Not moved.");
  status.announce(THREAD_CHANGE_DONE[THREAD_MENU_ACTION.DISMISS] + " “Check the backups”.");
  assert.equal(element.classList.contains("is-error"), false);
  assert.equal(element.children.length, 1);
  assert.equal(element.children[0].className, "sr-only");
  assert.equal(element.textContent, "Dismissed “Check the backups”.");
  status.clear();
  assert.equal(element.textContent, "");
});

test("every menu change but Rename has its own confirmation", () => {
  const announced = Object.values(THREAD_MENU_ACTION).filter(action => action !== THREAD_MENU_ACTION.RENAME);
  assert.deepEqual(Object.keys(THREAD_CHANGE_DONE).sort(), announced.sort());
  assert.equal(TRY_AGAIN, "Try again.");
});

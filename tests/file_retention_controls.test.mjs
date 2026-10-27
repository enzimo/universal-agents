// Settings > Team > File Retention (file_retention_controls.mjs): how long chat
// uploads, scratch files and saved tool outputs are kept; the form only when
// the gateway says so.
import assert from "node:assert/strict";
import test from "node:test";
import {
  FILE_RETENTION_ADMIN_ONLY, FILE_RETENTION_API, FILE_RETENTION_SAVED, createFileRetentionControls, parsedDays,
  retentionText,
} from "../assistant_gateway/onboarding_web/assets/file_retention_controls.mjs";
import { createSettingsClient } from "../assistant_gateway/onboarding_web/assets/settings_support.mjs";
import {
  answer, buttonLabels, byClass, byTag, errorResponse, fakeBrowser, jsonResponse, openRequests, settle,
} from "./settings_fake_dom.mjs";

// The page as the gateway sends it (website_handlers.file_retention).
const PAGE = { status: "ok", upload_days: 90, scratch_days: 21, output_days: 90, max_days: 3650 };

function setup() {
  const { browser, document, requests } = fakeBrowser();
  const container = document.createElement("section");
  document.body.append(container);
  const client = createSettingsClient({ browser });
  client.setCsrfToken("csrf-test");
  const controls = createFileRetentionControls({ container, client, browser });
  return { browser, document, requests, container, controls };
}

const status = container => byClass(container, "file-retention-status")[0];
const inputs = container => byClass(container, "file-retention-days");

function submit(container) {
  const form = byClass(container, "file-retention-form")[0];
  form.dispatchEvent(new Event("submit", { cancelable: true }));
}

test("days read as plain words, and only whole numbers within the limit are accepted", () => {
  assert.deepEqual([0, 1, 90].map(retentionText), ["Kept forever", "1 day", "90 days"]);
  assert.deepEqual(["0", " 30 ", "3650"].map(value => parsedDays(value, 3650)), [0, 30, 3650]);
  assert.deepEqual(["", "-1", "1.5", "3651", "ten"].map(value => parsedDays(value, 3650)),
    [null, null, null, null, null]);
});

test("people who may not change the numbers see them as a list, with no form", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  const request = await answer(requests, FILE_RETENTION_API, jsonResponse({ ...PAGE, can_change: false }));

  assert.equal(request.method, "GET");
  assert.deepEqual(byTag(container, "dt").map(node => node.textContent),
    ["Chat uploads", "Scratch files", "Saved tool outputs"]);
  assert.deepEqual(byTag(container, "dd").map(node => node.textContent), ["90 days", "21 days", "90 days"]);
  assert.ok(byTag(container, "p").some(node => node.textContent === FILE_RETENTION_ADMIN_ONLY));
  assert.equal(byClass(container, "file-retention-form").length, 0);
  assert.deepEqual(buttonLabels(container), []);
});

test("saving sends the three numbers with the write check and shows when each applies", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  await answer(requests, FILE_RETENTION_API, jsonResponse({ ...PAGE, can_change: true }));
  const [uploads, scratch, outputs] = inputs(container);
  assert.deepEqual([uploads.value, scratch.value, outputs.value], ["90", "21", "90"]);

  uploads.value = "30";
  scratch.value = "0";
  outputs.value = "180";
  submit(container);
  await settle();
  const save = openRequests(requests)[0];
  assert.equal(save.method, "PUT");
  assert.equal(save.path, FILE_RETENTION_API);
  assert.deepEqual(save.body, { upload_days: 30, scratch_days: 0, output_days: 180 });
  assert.equal(save.headers["X-UA-Permission-CSRF"], "csrf-test");
  const button = byTag(container, "button")[0];
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "Saving…");

  await answer(requests, FILE_RETENTION_API,
    jsonResponse({ ...PAGE, upload_days: 30, scratch_days: 0, output_days: 180, can_change: true }));
  assert.equal(status(container).textContent, FILE_RETENTION_SAVED);
  assert.equal(status(container).getAttribute("role"), "status");
  assert.deepEqual(inputs(container).map(input => input.value), ["30", "0", "180"]);
});

test("a number out of range is refused in place and nothing is sent", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  await answer(requests, FILE_RETENTION_API, jsonResponse({ ...PAGE, can_change: true }));
  const [, scratch, outputs] = inputs(container);
  scratch.value = "-3";
  submit(container);
  await settle();

  assert.deepEqual(openRequests(requests), []);
  assert.match(status(container).textContent, /whole number of days from 0 \(keep forever\) to 3650\. Nothing was saved\./);
  assert.equal(status(container).getAttribute("role"), "alert");
  assert.ok(scratch.focused > 0);

  scratch.value = "21";
  outputs.value = "1.5";
  submit(container);
  await settle();
  assert.deepEqual(openRequests(requests), []);
  assert.ok(outputs.focused > 0);
});

test("a refused save is an alert and keeps what the person typed", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  await answer(requests, FILE_RETENTION_API, jsonResponse({ ...PAGE, can_change: true }));
  const [uploads] = inputs(container);
  uploads.value = "45";
  submit(container);
  await answer(requests, FILE_RETENTION_API, errorResponse(403, "Only team administrators can change how long files are kept."));

  assert.equal(status(container).textContent, "Only team administrators can change how long files are kept.");
  assert.equal(status(container).getAttribute("role"), "alert");
  assert.equal(inputs(container)[0].value, "45");
  const button = byTag(container, "button")[0];
  assert.deepEqual([button.disabled, button.textContent], [false, "Save"]);
});

test("sign-out forgets the numbers and ignores a reply to an earlier read", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  controls.clear();
  await answer(requests, FILE_RETENTION_API, jsonResponse({ ...PAGE, can_change: true }));

  assert.equal(byClass(container, "file-retention-content")[0].children.length, 0);
  assert.equal(status(container).hidden, true);
});

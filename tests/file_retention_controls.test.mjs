// Settings > Team > File Retention (file_retention_controls.mjs): how long chat
// uploads, scratch files and saved tool outputs are kept, and the storage
// warnings; the form only when the gateway says so.
import assert from "node:assert/strict";
import test from "node:test";
import {
  FILE_RETENTION_ADMIN_ONLY, FILE_RETENTION_API, FILE_RETENTION_SAVED, SAVED_OUTPUTS_HELP, createFileRetentionControls,
  parsedDays,
  parsedWhole, retentionText, stepText, storageWarningsProblem,
} from "../assistant_gateway/onboarding_web/assets/file_retention_controls.mjs";
import { createSettingsClient } from "../assistant_gateway/onboarding_web/assets/settings_support.mjs";
import {
  answer, buttonLabels, byClass, byTag, errorResponse, fakeBrowser, jsonResponse, openRequests, settle,
} from "./settings_fake_dom.mjs";

// The page as the gateway sends it (website_handlers.file_retention).
const PAGE = {
  status: "ok", upload_days: 90, scratch_days: 21, output_days: 90, max_days: 3650, person_step_gb: 5,
  nearing_limit_percent: 80, nearly_full_percent: 95, max_step_gb: 10000,
};
const WARNINGS = { person_step_gb: 5, nearing_limit_percent: 80, nearly_full_percent: 95 };

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
const warningInputs = container => byClass(container, "storage-warning-number");

function submit(container) {
  const form = byClass(container, "file-retention-form")[0];
  form.dispatchEvent(new Event("submit", { cancelable: true }));
}

test("days read as plain words, and only whole numbers within the limit are accepted", () => {
  assert.deepEqual([0, 1, 90].map(retentionText), ["Kept forever", "1 day", "90 days"]);
  assert.deepEqual(["0", " 30 ", "3650"].map(value => parsedDays(value, 3650)), [0, 30, 3650]);
  assert.deepEqual(["", "-1", "1.5", "3651", "ten"].map(value => parsedDays(value, 3650)),
    [null, null, null, null, null]);
  assert.deepEqual([0, 5].map(stepText), ["Off", "Every 5 GB"]);
  assert.deepEqual(["0", "1", "100", "101"].map(value => parsedWhole(value, 1, 100)), [null, 1, 100, null]);
});

test("storage warning numbers are checked before anything is sent", () => {
  assert.equal(storageWarningsProblem(WARNINGS, 10000), "");
  assert.match(storageWarningsProblem({ ...WARNINGS, person_step_gb: null }, 10000), /from 0 \(no notices\) to 10000/);
  assert.match(storageWarningsProblem({ ...WARNINGS, nearly_full_percent: null }, 10000), /whole percent from 1 to 100/);
  assert.match(storageWarningsProblem({ ...WARNINGS, nearing_limit_percent: 95 }, 10000), /below the problem level/);
});

test("people who may not change the numbers see them as a list, with no form", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  const request = await answer(requests, FILE_RETENTION_API, jsonResponse({ ...PAGE, can_change: false }));

  assert.equal(request.method, "GET");
  assert.deepEqual(byTag(container, "dt").map(node => node.textContent), [
    "Chat uploads", "Scratch files", "Saved tool outputs", "Storage notice to each person", "Warn administrators",
    "Report a problem",
  ]);
  assert.deepEqual(byTag(container, "dd").map(node => node.textContent), [
    "90 days", "21 days", "90 days", "Every 5 GB", "At 80% full", "At 95% full",
  ]);
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
  // Lowering the number deletes older outputs at the next run; the first time, after a week.
  assert.ok(byTag(container, "small").some(node => node.textContent === SAVED_OUTPUTS_HELP));
  assert.match(SAVED_OUTPUTS_HELP, /Lowering the number deletes the older outputs at the next maintenance run/);

  uploads.value = "30";
  scratch.value = "0";
  outputs.value = "180";
  submit(container);
  await settle();
  const save = openRequests(requests)[0];
  assert.equal(save.method, "PUT");
  assert.equal(save.path, FILE_RETENTION_API);
  assert.deepEqual(save.body, { upload_days: 30, scratch_days: 0, output_days: 180, ...WARNINGS });
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

test("the storage warnings are saved with the days, and a warning level at or above the problem level is refused", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  await answer(requests, FILE_RETENTION_API, jsonResponse({ ...PAGE, can_change: true }));
  const [step, nearing, full] = warningInputs(container);
  assert.deepEqual([step.value, nearing.value, full.value], ["5", "80", "95"]);

  nearing.value = "95";
  submit(container);
  await settle();
  assert.deepEqual(openRequests(requests), []);
  assert.match(status(container).textContent, /warning level must be below the problem level\. Nothing was saved\./);
  assert.ok(full.focused > 0);

  step.value = "0";
  nearing.value = "70";
  full.value = "90";
  submit(container);
  await settle();
  const save = openRequests(requests)[0];
  assert.deepEqual(save.body, {
    upload_days: 90, scratch_days: 21, output_days: 90, person_step_gb: 0, nearing_limit_percent: 70,
    nearly_full_percent: 90,
  });
  await answer(requests, FILE_RETENTION_API, jsonResponse({
    ...PAGE, person_step_gb: 0, nearing_limit_percent: 70, nearly_full_percent: 90, can_change: true,
  }));
  assert.equal(status(container).textContent, FILE_RETENTION_SAVED);
  assert.deepEqual(warningInputs(container).map(input => input.value), ["0", "70", "90"]);
});

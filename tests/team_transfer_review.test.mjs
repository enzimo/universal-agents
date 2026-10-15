import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";
import { answer, fakeBrowser, jsonResponse, openRequests, settle, withElements } from "./settings_fake_dom.mjs";

const HTML = readFileSync(new URL("../team_transfer/review_assets/review.html", import.meta.url), "utf8");
const SCRIPT = readFileSync(new URL("../assistant_gateway/onboarding_web/assets/team_review.js", import.meta.url), "utf8");
const SAVED = "value: old\n";

async function fixture(section = "settings") {
  const env = fakeBrowser();
  const nodes = withElements(env.document, [...HTML.matchAll(/\sid="([^"]+)"/g)].map(match => [match[1]]));
  env.document.documentElement = { dataset: {} };
  vm.runInNewContext(SCRIPT, {
    document: env.document, fetch: env.browser.fetch, window: {},
    location: { hash: "#test-token", pathname: "/" }, history: { replaceState() {} },
  });
  await answer(env.requests, "/api/review", jsonResponse({
    records: [{ section, key: "llm.model_id", selected: true, text: SAVED, details: {} }],
    team_id: "team_a", target_root: "/tmp/new", review_directory: "/tmp/review",
  }));
  const editor = () => nodes.records.querySelector("textarea");
  const edit = text => { editor().value = text; editor().dispatchEvent(new Event("input")); };
  const save = () => nodes.records.querySelector("button").click();
  return { ...env, nodes, editor, edit, save };
}

const validation = digest => jsonResponse({ records: [], valid: true, skipped: 0, digest });

test("an edit during validation cannot restore an outdated import digest", async () => {
  const f = await fixture();
  f.nodes.validate.click(); await settle();
  f.edit("value: newer unsaved edit\n");
  await answer(f.requests, "/api/validate", validation("old-digest"));
  assert.equal(f.nodes.import.disabled, true);
  assert.match(f.nodes.status.textContent, /changed while validating/);
  // The handler guards dirty state even if invoked despite its disabled button.
  f.nodes.import.click(); await settle();
  assert.equal(f.requests.filter(r => r.path === "/api/import").length, 0);
  f.save();
  const saved = await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
  assert.equal(saved.body.text, "value: newer unsaved edit\n");
  f.nodes.validate.click();
  await answer(f.requests, "/api/validate", validation("new-digest"));
  assert.equal(f.nodes.import.disabled, false);
  f.nodes.import.click();
  const imported = await answer(f.requests, "/api/import", jsonResponse({ message: "Imported", credentials: [] }));
  assert.equal(imported.body.digest, "new-digest");
});

test("a save response keeps newer edits dirty and blocks validation until saved", async () => {
  const f = await fixture();
  f.edit("value: first edit\n"); f.save(); await settle();
  assert.equal(f.nodes.validate.disabled, true);
  f.edit("value: later edit\n");
  const earlier = await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
  assert.equal(earlier.body.text, "value: first edit\n");
  assert.equal(f.editor().value, "value: later edit\n");
  f.nodes.validate.click(); await settle();
  assert.equal(f.requests.filter(r => r.path === "/api/validate").length, 0);
  assert.match(f.nodes.status.textContent, /Save your edits/);
  assert.equal(f.nodes.import.disabled, true);
  f.save();
  const latest = await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
  assert.equal(latest.body.text, "value: later edit\n");
  f.nodes.validate.click();
  await answer(f.requests, "/api/validate", validation("latest-digest"));
  assert.equal(f.nodes.import.disabled, false);
});

test("pending saves run in order and keep validation disabled until all finish", async () => {
  const f = await fixture();
  f.edit("value: first\n"); f.save(); await settle();
  f.edit("value: second\n"); f.save(); await settle();
  assert.equal(openRequests(f.requests).length, 1);
  assert.equal(openRequests(f.requests)[0].body.text, "value: first\n");
  await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
  assert.equal(f.nodes.validate.disabled, true);
  assert.equal(openRequests(f.requests)[0].body.text, "value: second\n");
  await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
  assert.equal(f.nodes.validate.disabled, false);
  f.nodes.validate.click();
  await answer(f.requests, "/api/validate", validation("second-digest"));
  assert.equal(f.nodes.import.disabled, false);
});

test("failed saves remain dirty and a corrected save can proceed", async () => {
  const f = await fixture();
  f.edit("value: first\n"); f.save();
  await answer(f.requests, "/api/record", jsonResponse({ error: "Save failed" }, 400));
  assert.match(f.nodes.status.textContent, /Save failed/);
  f.nodes.validate.click(); await settle();
  assert.equal(f.requests.filter(r => r.path === "/api/validate").length, 0);
  f.edit("value: corrected\n"); f.save();
  await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
  f.nodes.validate.click();
  await answer(f.requests, "/api/validate", validation("corrected-digest"));
  assert.equal(f.nodes.import.disabled, false);
});

for (const failed of [false, true]) {
  test(`a queued ${failed ? "failed" : "successful"} save cannot clear an edit restored to an earlier value`, async () => {
    const f = await fixture();
    const visible = "value: first\n";
    f.edit(visible); f.save(); await settle();
    f.edit("value: second\n"); f.save();
    f.edit(visible);
    await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
    assert.equal(f.nodes.validate.disabled, true);
    const queued = await answer(f.requests, "/api/record", failed
      ? jsonResponse({ error: "Save failed" }, 400) : jsonResponse({ saved: true }));
    assert.equal(queued.body.text, "value: second\n");
    assert.equal(f.editor().value, visible);
    f.nodes.validate.click(); await settle();
    assert.equal(f.requests.filter(r => r.path === "/api/validate").length, 0);
    assert.equal(f.nodes.import.disabled, true);
    f.save();
    const corrected = await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
    assert.equal(corrected.body.text, visible);
    f.nodes.validate.click();
    await answer(f.requests, "/api/validate", validation("visible-digest"));
    assert.equal(f.nodes.import.disabled, false);
  });
}

test("saving a schedule clears both its stored and visible confirmation", async () => {
  const f = await fixture("schedules");
  const confirmation = f.nodes.records.querySelector(".schedule-confirm");
  confirmation.checked = true; confirmation.dispatchEvent(new Event("change"));
  const confirmed = await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
  assert.equal(confirmed.body.confirmed, true);
  f.save();
  assert.equal(confirmation.checked, false);
  const saved = await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
  assert.equal(saved.body.confirmed, false);
});

test("selection changes during validation invalidate its response", async () => {
  const f = await fixture();
  f.nodes.validate.click(); await settle();
  const selection = f.nodes.records.querySelector("input");
  selection.checked = false; selection.dispatchEvent(new Event("change"));
  await answer(f.requests, "/api/validate", validation("selected-digest"));
  assert.equal(f.nodes.import.disabled, true);
  const saved = await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
  assert.equal(saved.body.selected, false);
  assert.equal(f.nodes.import.disabled, true);
});

// Onboarding's Import page (team_import.html) runs the same script on its own API.
const IMPORT_HTML = readFileSync(new URL("../assistant_gateway/onboarding_web/assets/team_import.html", import.meta.url), "utf8");

async function importFixture() {
  const env = fakeBrowser();
  const nodes = withElements(env.document, [...IMPORT_HTML.matchAll(/\sid="([^"]+)"/g)].map(match => [match[1]]));
  env.document.documentElement = { dataset: {} };
  env.document.body.dataset = { reviewApi: "/api/team-import" };
  const timers = [], location = { hash: "#import-token", pathname: "/team-import", href: "/team-import" };
  vm.runInNewContext(SCRIPT, {
    document: env.document, fetch: env.browser.fetch, window: {}, location, history: { replaceState() {} },
    setTimeout: callback => timers.push(callback),
  });
  const loaded = await answer(env.requests, "/api/team-import/review", jsonResponse({
    team_id: "team_a", target_root: "/data", review_directory: null,
    sections: [{ name: "people", title: "People" }, { name: "pinned_memory", title: "Pinned memories" }],
    records: [
      { section: "people", key: "usr_ana", title: "Ana Lima (owner)", selected: true, text: null, details: {} },
      { section: "pinned_memory", key: "core/team/m1", title: "Keep reports short", selected: true, text: "Keep reports short", details: {} },
    ],
    destination: { secrets_source: "environment", exported_secrets_source: "environment" },
  }));
  const choice = nodes.destination.querySelector("input");
  const choose = checked => { choice.checked = checked; choice.dispatchEvent(new Event("change")); };
  return { ...env, nodes, timers, location, loaded, choice, choose };
}

test("onboarding's page uses its own API and groups readable names under section titles", async () => {
  const f = await importFixture();
  assert.equal(f.loaded.headers["X-Transfer-Token"], "import-token");
  const headings = f.nodes.records.querySelectorAll("h2").map(node => node.textContent);
  assert.deepEqual(headings, ["People", "Pinned memories"]);
  const names = f.nodes.records.querySelectorAll("summary").map(node => node.textContent);
  assert.deepEqual(names, ["Ana Lima (owner)", "Keep reports short"]);
  assert.match(f.nodes.destination.textContent, /old instance took its secrets from the environment/);
  assert.equal(f.nodes.destination.querySelector("input").checked, true);
});

test("changing where secrets come from needs a new validation", async () => {
  const f = await importFixture();
  f.nodes.validate.click();
  await answer(f.requests, "/api/team-import/validate", validation("first-digest"));
  assert.equal(f.nodes.import.disabled, false);
  const choice = f.nodes.destination.querySelector("input");
  choice.checked = false; choice.dispatchEvent(new Event("change"));
  const saved = await answer(f.requests, "/api/team-import/destination", jsonResponse({ saved: true, secrets_source: "env_file" }));
  assert.deepEqual(saved.body, { secrets_source: "env_file" });
  assert.equal(f.nodes.import.disabled, true);
});

test("a destination change during validation is saved and invalidates its response", async () => {
  const f = await importFixture();
  f.nodes.validate.click(); await settle();
  f.choose(false); await settle();
  assert.equal(openRequests(f.requests).filter(r => r.path === "/api/team-import/destination").length, 1);
  await answer(f.requests, "/api/team-import/validate", validation("old-digest"));
  assert.equal(f.nodes.import.disabled, true);
  assert.match(f.nodes.status.textContent, /changed while validating/);
  const saved = await answer(f.requests, "/api/team-import/destination", jsonResponse({ saved: true }));
  assert.deepEqual(saved.body, { secrets_source: "env_file" });
  assert.equal(f.choice.checked, false);
  assert.equal(f.nodes.import.disabled, true);
  f.nodes.validate.click();
  await answer(f.requests, "/api/team-import/validate", validation("current-digest"));
  assert.equal(f.nodes.import.disabled, false);
});

test("rapid destination changes save in order and keep the visible choice pending until the last save", async () => {
  const f = await importFixture();
  f.choose(false); await settle();
  f.choose(true); f.choose(false); await settle();
  assert.equal(openRequests(f.requests).length, 1);
  for (const [index, secrets_source] of ["env_file", "environment", "env_file"].entries()) {
    assert.equal(f.nodes.validate.disabled, true);
    assert.equal(f.nodes.import.disabled, true);
    const saved = await answer(f.requests, "/api/team-import/destination", jsonResponse({ saved: true }));
    assert.deepEqual(saved.body, { secrets_source });
    assert.equal(f.choice.checked, false);
    assert.equal(f.nodes.validate.disabled, index < 2);
    assert.equal(openRequests(f.requests).length, index < 2 ? 1 : 0);
  }
  f.nodes.validate.click();
  await answer(f.requests, "/api/team-import/validate", validation("last-choice-digest"));
  assert.equal(f.nodes.import.disabled, false);
});

test("a failed destination save blocks import and validation retries the visible choice before checking records", async () => {
  const f = await importFixture();
  f.nodes.validate.click();
  await answer(f.requests, "/api/team-import/validate", validation("old-digest"));
  f.choose(false);
  await answer(f.requests, "/api/team-import/destination", jsonResponse({ error: "Save failed" }, 400));
  assert.equal(f.choice.checked, false);
  const failureMessage = f.nodes.status.textContent;
  assert.equal(f.nodes.import.disabled, true);
  f.nodes.import.click(); await settle();
  assert.equal(f.requests.filter(r => r.path === "/api/team-import/import").length, 0);
  f.nodes.validate.click(); await settle();
  assert.equal(f.nodes.validate.disabled, true);
  assert.equal(f.requests.filter(r => r.path === "/api/team-import/validate").length, 1);
  assert.match(failureMessage, /failed.*Validate selected records.*Save failed/);
  const failedRetry = await answer(f.requests, "/api/team-import/destination", jsonResponse({ error: "Still unavailable" }, 400));
  assert.deepEqual(failedRetry.body, { secrets_source: "env_file" });
  assert.equal(f.requests.filter(r => r.path === "/api/team-import/validate").length, 1);
  assert.equal(f.nodes.import.disabled, true);
  f.nodes.validate.click();
  const savedRetry = await answer(f.requests, "/api/team-import/destination", jsonResponse({ saved: true }));
  assert.deepEqual(savedRetry.body, { secrets_source: "env_file" });
  assert.equal(f.choice.checked, false);
  await answer(f.requests, "/api/team-import/validate", validation("saved-choice-digest"));
  assert.equal(f.nodes.import.disabled, false);
  f.nodes.import.click();
  const imported = await answer(f.requests, "/api/team-import/import", jsonResponse({ message: "Imported", credentials: [] }));
  assert.equal(imported.body.digest, "saved-choice-digest");
});

test("destination and record changes share the same ordered save queue", async () => {
  const f = await importFixture();
  const selection = f.nodes.records.querySelector("input");
  selection.checked = false; selection.dispatchEvent(new Event("change")); await settle();
  f.choose(false); await settle();
  assert.equal(openRequests(f.requests).length, 1);
  assert.equal(openRequests(f.requests)[0].path, "/api/team-import/record");
  await answer(f.requests, "/api/team-import/record", jsonResponse({ saved: true }));
  assert.equal(f.nodes.validate.disabled, true);
  const saved = await answer(f.requests, "/api/team-import/destination", jsonResponse({ saved: true }));
  assert.deepEqual(saved.body, { secrets_source: "env_file" });
  assert.equal(f.nodes.validate.disabled, false);
});

test("changing the destination during a validation retry requires the latest choice to finish saving", async () => {
  const f = await importFixture();
  f.choose(false);
  await answer(f.requests, "/api/team-import/destination", jsonResponse({ error: "Save failed" }, 400));
  f.nodes.validate.click(); await settle();
  f.choose(true); await settle();
  const retried = await answer(f.requests, "/api/team-import/destination", jsonResponse({ saved: true }));
  assert.deepEqual(retried.body, { secrets_source: "env_file" });
  assert.equal(f.requests.filter(r => r.path === "/api/team-import/validate").length, 0);
  assert.equal(f.nodes.validate.disabled, true);
  assert.equal(f.nodes.import.disabled, true);
  const current = await answer(f.requests, "/api/team-import/destination", jsonResponse({ saved: true }));
  assert.deepEqual(current.body, { secrets_source: "environment" });
  assert.equal(f.choice.checked, true);
  f.nodes.validate.click();
  await answer(f.requests, "/api/team-import/validate", validation("current-choice-digest"));
  assert.equal(f.nodes.import.disabled, false);
});

for (const failed of [false, true]) {
  test(`review changes are disabled during import and enabled after ${failed ? "a failure" : "a response"}`, async () => {
    const f = await importFixture();
    const controls = () => [f.choice, f.nodes.select, f.nodes.unselect,
      ...["input", "textarea", "button"].flatMap(tag => f.nodes.records.querySelectorAll(tag))];
    f.nodes.validate.click(); await settle();
    assert.ok(controls().every(control => !control.disabled), "validation leaves review controls editable");
    await answer(f.requests, "/api/team-import/validate", validation("approved-digest"));
    f.nodes.import.click(); await settle();
    assert.ok(controls().every(control => control.disabled));
    f.choose(false);
    assert.equal(f.choice.checked, true, "a forced event cannot change the importing destination");
    const selection = f.nodes.records.querySelector("input");
    selection.checked = false; selection.dispatchEvent(new Event("change"));
    assert.equal(selection.checked, true);
    const editor = f.nodes.records.querySelector("textarea");
    editor.value = "Changed after import started"; editor.dispatchEvent(new Event("input"));
    assert.equal(editor.value, "Keep reports short");
    f.nodes.records.querySelector("button").click();
    f.nodes.select.click(); f.nodes.unselect.click();
    // Filtering can redraw the records during import, but new controls stay disabled.
    f.nodes.filter.dispatchEvent(new Event("input")); await settle();
    assert.ok(controls().every(control => control.disabled));
    assert.deepEqual(openRequests(f.requests).map(request => request.path), ["/api/team-import/import"]);
    const imported = await answer(f.requests, "/api/team-import/import", failed
      ? jsonResponse({ error: "Import failed" }, 400)
      : jsonResponse({ message: "Imported", credentials: [] }));
    assert.equal(imported.body.digest, "approved-digest");
    assert.ok(controls().every(control => !control.disabled));
    f.choose(false);
    const changed = await answer(f.requests, "/api/team-import/destination", jsonResponse({ saved: true }));
    assert.deepEqual(changed.body, { secrets_source: "env_file" });
  });
}

test("schedule confirmation and text cannot change while its import request is running", async () => {
  const f = await fixture("schedules");
  const confirm = f.nodes.records.querySelector(".schedule-confirm");
  confirm.checked = true; confirm.dispatchEvent(new Event("change"));
  await answer(f.requests, "/api/record", jsonResponse({ saved: true }));
  f.nodes.validate.click();
  await answer(f.requests, "/api/validate", validation("confirmed-digest"));
  f.nodes.import.click(); await settle();
  assert.equal(confirm.disabled, true);
  assert.equal(f.editor().disabled, true);
  confirm.checked = false; confirm.dispatchEvent(new Event("change"));
  f.edit("Schedule changed after import started"); f.save(); await settle();
  assert.equal(confirm.checked, true);
  assert.equal(f.editor().value, SAVED);
  assert.deepEqual(openRequests(f.requests).map(request => request.path), ["/api/import"]);
  await answer(f.requests, "/api/import", jsonResponse({ message: "Imported", credentials: [] }));
  assert.equal(confirm.disabled, false);
  assert.equal(f.editor().disabled, false);
});

test("after an onboarding import the page waits for the team, then opens Web Chat", async () => {
  const f = await importFixture();
  f.nodes.validate.click();
  await answer(f.requests, "/api/team-import/validate", validation("digest"));
  f.nodes.import.click();
  await answer(f.requests, "/api/team-import/import", jsonResponse({
    imported: 3, message: "Imported. The team is starting.", restarting: true, next_url: "/", missing_secrets: [],
    credentials: [{ key: "person:usr_ana/stored_value/github.sign_in", imported: false, waiting: true, reason: "stored when the team starts, by its broker" }],
  }));
  assert.equal(f.nodes.import.hidden, true);
  assert.match(f.nodes.results.textContent, /stored when the team starts/);
  f.timers.shift()();
  await answer(f.requests, "/health", jsonResponse({ service: "onboard-web" }));
  assert.equal(f.location.href, "/team-import", "still onboarding: keep waiting");
  f.timers.shift()();
  await answer(f.requests, "/health", jsonResponse({ service: "gateway" }));
  assert.equal(f.location.href, "/");
});

test("missing environment secrets are listed and the page does not wait for a start", async () => {
  const f = await importFixture();
  f.nodes.validate.click();
  await answer(f.requests, "/api/team-import/validate", validation("digest"));
  f.nodes.import.click();
  await answer(f.requests, "/api/team-import/import", jsonResponse({
    imported: 3, message: "Imported. Add the missing secrets.", restarting: false, credentials: [],
    missing_secrets: [{ name: "TELEGRAM_BOT_TOKEN", purpose: "Telegram bot token.", how_to_create: "Create a bot with BotFather." }],
  }));
  assert.match(f.nodes.results.textContent, /TELEGRAM_BOT_TOKEN: Telegram bot token\. Create a bot with BotFather\. Set TELEGRAM_BOT_TOKEN, or TELEGRAM_BOT_TOKEN_FILE/);
  assert.equal(f.timers.length, 0);
});

import assert from "node:assert/strict";
import test from "node:test";
import { CSRF_HEADER, createSettingsClient, localTimeFormatter } from "../assistant_gateway/onboarding_web/assets/settings_support.mjs";
import {
  DELETE_CONFIRM_MS, createMemoryControls, localInputValue, memoryMeta, parseTags,
} from "../assistant_gateway/onboarding_web/assets/memory_controls.mjs";
import {
  answer, buttonLabels, byClass, byTag, errorResponse, fakeBrowser, jsonResponse, openRequests, settle, withElements,
} from "./settings_fake_dom.mjs";

const formatTime = localTimeFormatter({ locale: "en-US", timeZone: "UTC" });
const DIGEST = "c".repeat(64);

function memory(overrides = {}) {
  return {
    memory_id: 7, scope: "personal", fact_text: "Prefers short answers.", tags: ["style", "tone"],
    updated_at_utc: "2026-09-20T12:00:00Z", invalid_after_utc: null,
    available_actions: ["edit", "delete"], record_digest: DIGEST, ...overrides,
  };
}

function fixture() {
  const env = fakeBrowser();
  const nodes = withElements(env.document, [
    ["settings-memories-panel", "section"],
    ["settings-memories-new", "button", "settings-memories-panel"],
    ["settings-memories-refresh", "button", "settings-memories-panel"],
    ["memory-personal-tab", "button", "settings-memories-panel"],
    ["memory-team-tab", "button", "settings-memories-panel"],
    ["settings-memories-status", "p", "settings-memories-panel"],
    ["settings-memories-content", "div", "settings-memories-panel"],
  ]);
  const client = createSettingsClient({ browser: env.browser });
  client.setCsrfToken("csrf-2");
  const memories = createMemoryControls({
    content: nodes["settings-memories-content"], status: nodes["settings-memories-status"],
    refreshButton: nodes["settings-memories-refresh"], newButton: nodes["settings-memories-new"],
    personalTab: nodes["memory-personal-tab"], teamTab: nodes["memory-team-tab"],
    client, getSessionKey: () => "session-9", isVisible: () => true, browser: env.browser, formatTime,
  });
  return { ...env, memories, content: nodes["settings-memories-content"], status: nodes["settings-memories-status"],
    newButton: nodes["settings-memories-new"], teamTab: nodes["memory-team-tab"], personalTab: nodes["memory-personal-tab"] };
}

async function loaded(f, payload) {
  const done = f.memories.load();
  const request = await answer(f.requests, "/api/web-chat/memories?", jsonResponse({ status: "ok", ...payload }));
  await done;
  return request;
}

const items = f => byClass(f.content, "memory-item");

test("small helpers: tags, local input values and the update line", () => {
  assert.deepEqual(parseTags(" alpha, beta ,, gamma "), ["alpha", "beta", "gamma"]);
  assert.equal(localInputValue(""), "");
  assert.match(localInputValue("2099-01-01T12:00:00Z"), /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
  // The space before AM/PM depends on the ICU version, so match any space.
  assert.match(memoryMeta(memory({ invalid_after_utc: "2026-10-01T08:00:00Z" }), formatTime),
    /^Updated Sep 20, 2026, 12:00\sPM UTC · Expires Oct 1, 2026, 8:00\sAM UTC$/);
  assert.match(memoryMeta(memory(), formatTime), /^Updated Sep 20, 2026, 12:00\sPM UTC$/);
});

test("Personal and Team each load their own list for this conversation", async () => {
  const f = fixture();
  const personal = await loaded(f, { memories: [memory()], total: 1, has_more: false, can_create: true });
  assert.equal(personal.path, "/api/web-chat/memories?session_key=session-9&scope=personal&offset=0&limit=20");
  assert.equal(items(f).length, 1);
  assert.match(items(f)[0].textContent, /Prefers short answers\./);
  assert.deepEqual(byClass(f.content, "memory-tag").map(tag => tag.textContent), ["style", "tone"]);
  assert.match(items(f)[0].textContent, /Updated Sep 20, 2026, 12:00\sPM UTC/);
  assert.doesNotMatch(items(f)[0].textContent, /2026-09-20T/, "raw ISO times are never shown");
  assert.deepEqual(buttonLabels(items(f)[0]), ["Edit", "Delete"]);
  assert.equal(f.newButton.hidden, false, "New shows when the gateway allows adding");

  f.teamTab.click();
  assert.equal(f.teamTab.getAttribute("aria-selected"), "true");
  assert.equal(f.personalTab.getAttribute("aria-selected"), "false");
  assert.equal(f.newButton.hidden, true, "New hides until the gateway answers for the new category");
  const team = await answer(f.requests, "/api/web-chat/memories?", jsonResponse({
    memories: [memory({ memory_id: 9, scope: "team", fact_text: "Deploys happen on Fridays.", available_actions: [] })],
    total: 1, can_create: false,
  }));
  assert.match(team.path, /&scope=team&/);
  assert.equal(items(f).length, 1);
  assert.deepEqual(buttonLabels(items(f)[0]), []);
  assert.match(items(f)[0].textContent, /Admin access required/);
  assert.equal(f.newButton.hidden, true);
});

test("an empty category says so", async () => {
  const f = fixture();
  await loaded(f, { memories: [], total: 0, can_create: true });
  assert.match(f.content.textContent, /No active personal memories are stored\./);
});

test("Edit saves the text, tags and expiration with the record digest and CSRF header", async () => {
  const f = fixture();
  await loaded(f, { memories: [memory()], total: 1, can_create: true });
  const item = items(f)[0];
  byTag(item, "button").find(button => button.textContent === "Edit").click();
  const [editor] = byClass(item, "memory-editor");
  const [factInput] = byTag(editor, "textarea");
  const [tagInput, expirationInput] = byTag(editor, "input");
  assert.equal(factInput.value, "Prefers short answers.");
  assert.equal(tagInput.value, "style, tone");
  assert.equal(expirationInput.type, "datetime-local");
  factInput.value = "  Prefers very short answers.  ";
  tagInput.value = "style, brevity";
  expirationInput.value = "2099-01-01T12:00";
  editor.dispatchEvent(new Event("submit", { cancelable: true }));
  const save = byTag(editor, "button").find(button => button.type === "submit");
  assert.equal(save.textContent, "Saving…");

  const patch = await answer(f.requests, "/api/web-chat/memories/personal/7", jsonResponse({
    memory: memory({ fact_text: "Prefers very short answers.", tags: ["style", "brevity"] }),
  }));
  assert.equal(patch.method, "PATCH");
  assert.equal(patch.headers[CSRF_HEADER], "csrf-2");
  assert.equal(patch.body.session_key, "session-9");
  assert.equal(patch.body.record_digest, DIGEST);
  assert.equal(patch.body.fact_text, "Prefers very short answers.");
  assert.deepEqual(patch.body.tags, ["style", "brevity"]);
  assert.equal(patch.body.invalid_after_utc, new Date("2099-01-01T12:00").toISOString());
  assert.match(items(f)[0].textContent, /Prefers very short answers\./);
  assert.equal(byClass(f.content, "memory-editor").length, 0);
  assert.equal(f.status.textContent, "Memory updated.");
});

test("empty memory text is refused before anything is sent", async () => {
  const f = fixture();
  await loaded(f, { memories: [memory()], total: 1 });
  const item = items(f)[0];
  byTag(item, "button").find(button => button.textContent === "Edit").click();
  const [editor] = byClass(item, "memory-editor");
  byTag(editor, "textarea")[0].value = "   ";
  editor.dispatchEvent(new Event("submit", { cancelable: true }));
  await settle();
  assert.equal(openRequests(f.requests).length, 0);
  assert.equal(f.status.textContent, "Memory text cannot be empty.");
  assert.ok(f.status.classList.contains("is-error"));
});

test("a refused save keeps the form and shows the gateway's explanation", async () => {
  const f = fixture();
  await loaded(f, { memories: [memory()], total: 1 });
  const item = items(f)[0];
  byTag(item, "button").find(button => button.textContent === "Edit").click();
  const [editor] = byClass(item, "memory-editor");
  byTag(editor, "textarea")[0].value = "Changed text";
  editor.dispatchEvent(new Event("submit", { cancelable: true }));
  await answer(f.requests, "/api/web-chat/memories/personal/7",
    errorResponse(409, "This memory changed after you opened it.", "Refresh and try again."));
  assert.equal(f.status.textContent, "This memory changed after you opened it. Refresh and try again.");
  assert.equal(byClass(item, "memory-editor").length, 1, "the form stays open");
  assert.equal(byTag(editor, "textarea")[0].value, "Changed text", "entered text is kept");
});

test("Delete asks for confirmation on the same button, then deletes with the digest and reloads", async () => {
  const f = fixture();
  await loaded(f, { memories: [memory(), memory({ memory_id: 8, fact_text: "Second" })], total: 2 });
  const remove = byTag(items(f)[0], "button").find(button => button.textContent === "Delete");
  remove.click();
  await settle();
  assert.equal(openRequests(f.requests).length, 0, "the first click only asks for confirmation");
  assert.equal(remove.textContent, "Confirm delete");
  assert.equal(f.timeouts.at(-1).delay, DELETE_CONFIRM_MS);

  remove.click();
  assert.equal(remove.textContent, "Deleting…");
  const deletion = await answer(f.requests, "/api/web-chat/memories/personal/7", jsonResponse({ deleted: true }));
  assert.equal(deletion.method, "DELETE");
  assert.equal(deletion.headers[CSRF_HEADER], "csrf-2");
  assert.deepEqual(deletion.body, { session_key: "session-9", record_digest: DIGEST });
  const reload = await answer(f.requests, "/api/web-chat/memories?", jsonResponse({ memories: [memory({ memory_id: 8, fact_text: "Second" })], total: 1 }));
  assert.match(reload.path, /&offset=0&limit=20$/);
  assert.equal(items(f).length, 1);
  assert.equal(f.status.textContent, "Memory deleted. Its audit event remains available for recovery.");
});

test("New adds a memory in the selected category with an inline form", async () => {
  const f = fixture();
  await loaded(f, { memories: [], total: 0, can_create: true });
  f.newButton.click();
  assert.equal(f.newButton.disabled, true);
  const [item] = items(f);
  assert.equal(byTag(item, "h3")[0].textContent, "New personal memory");
  const [editor] = byClass(item, "memory-editor");
  byTag(editor, "textarea")[0].value = "Uses metric units.";
  byTag(editor, "input")[0].value = "units";
  editor.dispatchEvent(new Event("submit", { cancelable: true }));
  const create = await answer(f.requests, "/api/web-chat/memories/personal", jsonResponse({ memory: memory({ memory_id: 11 }) }));
  assert.equal(create.method, "POST");
  assert.deepEqual(create.body, { session_key: "session-9", record_digest: "", fact_text: "Uses metric units.",
    tags: ["units"], invalid_after_utc: null });
  await answer(f.requests, "/api/web-chat/memories?", jsonResponse({ memories: [memory({ memory_id: 11, fact_text: "Uses metric units." })], total: 1, can_create: true }));
  assert.equal(byClass(f.content, "memory-editor").length, 0);
  assert.match(f.content.textContent, /Uses metric units\./);
  assert.equal(f.status.textContent, "Memory added.");
});

test("memory text stays text, even when it contains markup", async () => {
  const f = fixture();
  await loaded(f, { memories: [memory({ fact_text: "<script>alert(1)</script>", tags: ["<b>x</b>"] })], total: 1 });
  assert.equal(byTag(f.content, "script").length, 0);
  assert.equal(byTag(f.content, "b").length, 0);
  assert.equal(byClass(f.content, "memory-fact")[0].textContent, "<script>alert(1)</script>");
});

test("pages hold 20 memories and the pager reads the chosen page", async () => {
  const f = fixture();
  await loaded(f, { memories: [memory()], total: 45, has_more: true });
  const [pager] = byClass(f.content, "list-pagination");
  assert.equal(pager.settings.total, 45);
  const next = pager.settings.onPage(40);
  const request = await answer(f.requests, "/api/web-chat/memories?", jsonResponse({ memories: [memory()], total: 45 }));
  await next;
  assert.match(request.path, /&offset=40&limit=20$/);
});

test("sign-out returns to Personal with an empty list", async () => {
  const f = fixture();
  await loaded(f, { memories: [memory()], total: 1 });
  f.teamTab.click();
  await answer(f.requests, "/api/web-chat/memories?", jsonResponse({ memories: [], total: 0 }));
  f.memories.clear();
  assert.equal(f.personalTab.getAttribute("aria-selected"), "true");
  assert.equal(f.content.children.length, 0);
});

import assert from "node:assert/strict";
import test from "node:test";
import { createSettingsClient, localTimeFormatter } from "../assistant_gateway/onboarding_web/assets/settings_support.mjs";
import {
  EMPTY_TEXT, FORBIDDEN_TEXT, STATUS_API, createStatusPage, severityCounts,
} from "../assistant_gateway/onboarding_web/assets/status_page.mjs";
import { answer, byClass, byTag, errorResponse, fakeBrowser, jsonResponse, withElements } from "./settings_fake_dom.mjs";

const formatTime = localTimeFormatter({ locale: "en-US", timeZone: "UTC" });

function fixture() {
  const env = fakeBrowser();
  const nodes = withElements(env.document, [
    ["status-app", "section"],
    ["status-refresh", "button", "status-app"],
    ["status-updated-at", "span", "status-app"],
    ["status-status", "p", "status-app"],
    ["status-content", "div", "status-app"],
  ]);
  const client = createSettingsClient({ browser: env.browser });
  const page = createStatusPage({
    document: env.document, content: nodes["status-content"], status: nodes["status-status"],
    refreshButton: nodes["status-refresh"], updatedAt: nodes["status-updated-at"], client, formatTime,
  });
  return { ...env, page, content: nodes["status-content"], status: nodes["status-status"], refresh: nodes["status-refresh"] };
}

const PROBLEM = {
  finding_id: "model_key_rejected:https://bifrost.example.com", severity: "problem",
  title: "bifrost.example.com rejected the saved key", summary: "Requests fail until the key is replaced.",
  action: "An administrator saves a working key with /auth model.", source: "manager",
  since: "2026-10-01T10:05:00Z", details: "model openai/gpt-6-luna",
};
const WARNING = {
  finding_id: "environment_overrides_setting:LITELLM_API_BASE", severity: "warning",
  title: "LITELLM_API_BASE overrides a saved setting", summary: "", action: "", source: "launcher", since: null, details: "",
};

test("severity counts read as words and leave out empty severities", () => {
  assert.deepEqual(severityCounts({ problem: 2, warning: 1, note: 0 }).map(item => item.label), ["2 Problems", "1 Warning"]);
});

test("findings render as text with their severity word, what to do, source and collapsed details", async () => {
  const f = fixture();
  const done = f.page.load(0);
  const request = await answer(f.requests, STATUS_API, jsonResponse({
    status: "ok", checked_at: "2026-10-01T10:06:00Z", unavailable_sources: [],
    counts: { problem: 1, warning: 1, note: 0 }, findings: [PROBLEM, WARNING], total: 2, has_more: false,
  }));
  await done;

  assert.equal(request.path, STATUS_API + "?offset=0");
  const items = byClass(f.content, "status-finding");
  assert.equal(items.length, 2);
  assert.match(items[0].textContent, /^Problembifrost\.example\.com rejected the saved key/);
  assert.match(items[0].textContent, /What to do: An administrator saves a working key with \/auth model\./);
  assert.match(items[0].textContent, /Reported by manager · since Oct 1, 2026, 10:05 AM UTC/);
  assert.equal(byTag(items[0], "details").length, 1);
  assert.equal(byTag(items[0], "details")[0].open, false);
  assert.equal(byTag(items[1], "details").length, 0);
  assert.deepEqual(byClass(f.content, "status-severity").slice(0, 2).map(node => node.textContent), ["1 Problem", "1 Warning"]);
  assert.equal(f.refresh.disabled, false);
});

test("an empty report says nothing needs attention, and unanswered sources are an alert", async () => {
  const f = fixture();
  const done = f.page.load(0);
  await answer(f.requests, STATUS_API, jsonResponse({
    status: "ok", checked_at: "2026-10-01T10:06:00Z", unavailable_sources: ["secure tool broker"],
    counts: {}, findings: [], total: 0,
  }));
  await done;

  const callout = byClass(f.content, "warning-callout")[0];
  assert.equal(callout.getAttribute("role"), "alert");
  assert.match(callout.textContent, /secure tool broker/);
  assert.match(f.content.textContent, new RegExp(EMPTY_TEXT.slice(0, 30)));
});

test("more than one page of findings gets the shared pager", async () => {
  const f = fixture();
  const done = f.page.load(0);
  await answer(f.requests, STATUS_API, jsonResponse({
    status: "ok", checked_at: "2026-10-01T10:06:00Z", unavailable_sources: [],
    counts: { warning: 25 }, findings: Array.from({ length: 20 }, (_, i) => ({ ...WARNING, finding_id: "w" + i })), total: 25,
  }));
  await done;

  const pager = byClass(f.content, "list-pagination")[0];
  assert.equal(pager.settings.total, 25);
  assert.equal(pager.settings.pageSize, 20);
});

test("a member sees why the page is empty, and a failure is shown as an error line", async () => {
  const f = fixture();
  const forbidden = f.page.load(0);
  await answer(f.requests, STATUS_API, errorResponse(403, "Only team administrators and owners can see the Status page."));
  await forbidden;
  assert.equal(f.content.textContent, FORBIDDEN_TEXT);

  const failed = f.page.load(0);
  await answer(f.requests, STATUS_API, errorResponse(500, "The gateway could not read the status."));
  await failed;
  assert.equal(f.status.hidden, false);
  assert.ok(f.status.classList.contains("is-error"));
});

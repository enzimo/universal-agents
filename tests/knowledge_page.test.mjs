import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { localTimeFormatter } from "../assistant_gateway/onboarding_web/assets/settings_support.mjs";
import {
  KNOWLEDGE_API, KNOWLEDGE_COUNTS_PATH, TEXT, createKnowledgeClient, createKnowledgePage, mergedText, pendingProposals,
  proposalCountLabel,
} from "../assistant_gateway/onboarding_web/assets/knowledge_page.mjs";
import { describeFlag, lineDiff } from "../assistant_gateway/onboarding_web/assets/knowledge_render.mjs";
import { createAuthentication } from "../assistant_gateway/onboarding_web/assets/chat_authentication.mjs";
import {
  answer, buttonLabels, byClass, byTag, fakeBrowser, jsonResponse, openRequests, settle, withElements,
} from "./settings_fake_dom.mjs";

const formatTime = localTimeFormatter({ locale: "en-US", timeZone: "UTC" });

// A small stand-in for the Markdown renderer: one paragraph of text nodes,
// with `code` spans as <code> elements, so link walking sees real nodes.
function renderWithNodes(document, node, markdown) {
  const paragraph = document.createElement("p");
  markdown.split(/(`[^`]*`)/).forEach(part => {
    if (part.startsWith("`")) {
      const code = document.createElement("code");
      code.append(document.createTextNode(part.slice(1, -1)));
      paragraph.append(code);
    } else if (part) paragraph.append(document.createTextNode(part));
  });
  node.append(paragraph);
}

function fixture({ getSessionKey = () => Promise.resolve("sess_1"), onUnauthorized,
  url = "http://example.test/webchat?view=knowledge" } = {}) {
  const env = fakeBrowser();
  const events = new EventTarget();
  const lifecycle = new AbortController();
  const addresses = [];
  env.browser.addEventListener = events.addEventListener.bind(events);
  env.browser.dispatchEvent = events.dispatchEvent.bind(events);
  env.browser.location = new URL(url);
  env.browser.history = Object.fromEntries(["pushState", "replaceState"].map(method => [method, (_state, _title, address) => {
    addresses.push({ method, address });
    env.browser.location = new URL(address);
  }]));
  const nodes = withElements(env.document, [
    ["knowledge-app", "section"],
    ["knowledge-status", "p", "knowledge-app"],
    ["knowledge-refresh", "button", "knowledge-app"],
    ["knowledge-new", "button", "knowledge-app"],
    ["knowledge-home", "button", "knowledge-app"],
    ["knowledge-browse", "button", "knowledge-app"],
    ["knowledge-layout", "div", "knowledge-app"],
    ["knowledge-directory", "aside", "knowledge-layout"],
    ["knowledge-pagination", "div", "knowledge-directory"],
    ["knowledge-team-tab", "button", "knowledge-app"],
    ["knowledge-personal-tab", "button", "knowledge-app"],
    ["knowledge-proposals-tab", "button", "knowledge-app"],
    ["knowledge-proposals-count", "span", "knowledge-proposals-tab"],
    ["knowledge-search", "form", "knowledge-app"],
    ["knowledge-query", "input", "knowledge-search"],
    ["knowledge-page-status", "select", "knowledge-search"],
    ["knowledge-list", "div", "knowledge-app"],
    ["knowledge-viewer-title", "h2", "knowledge-app"],
    ["knowledge-viewer-meta", "p", "knowledge-app"],
    ["knowledge-viewer-tools", "div", "knowledge-app"],
    ["knowledge-viewer", "div", "knowledge-app"],
  ]);
  const client = createKnowledgeClient({ browser: env.browser, onUnauthorized });
  client.setCsrfToken("csrf_1");
  const rendered = [];
  const page = createKnowledgePage({
    browser: env.browser, client, formatTime, signal: lifecycle.signal,
    getSessionKey,
    renderMarkdown: (node, markdown) => { rendered.push(markdown); renderWithNodes(env.document, node, markdown); },
    nodes: {
      status: nodes["knowledge-status"], refreshButton: nodes["knowledge-refresh"], newButton: nodes["knowledge-new"],
      homeButton: nodes["knowledge-home"], browseButton: nodes["knowledge-browse"], layout: nodes["knowledge-layout"],
      directory: nodes["knowledge-directory"], pagination: nodes["knowledge-pagination"],
      tabs: { team: nodes["knowledge-team-tab"], personal: nodes["knowledge-personal-tab"], proposals: nodes["knowledge-proposals-tab"] },
      searchForm: nodes["knowledge-search"], searchInput: nodes["knowledge-query"], list: nodes["knowledge-list"],
      pageStatus: nodes["knowledge-page-status"], proposalsCount: nodes["knowledge-proposals-count"],
      viewerTitle: nodes["knowledge-viewer-title"], viewerMeta: nodes["knowledge-viewer-meta"],
      viewerTools: nodes["knowledge-viewer-tools"], viewer: nodes["knowledge-viewer"],
    },
  });
  return { ...env, nodes, page, rendered, client, addresses, lifecycle };
}

const summary = (name, title, extra = {}) => ({
  space: "team", page: name, title, page_type: "how-to", summary: "", tags: [], revision: 1, status: "active",
  updated_at_utc: "2026-10-10T12:00:00Z", snippet: "", ...extra,
});

const detail = (extra = {}) => ({
  ...summary("deploys", "Deploys"), revision: 2, body: "Deploy on **Tuesdays**.", summary: "How we ship.",
  tags: ["release"], created_at_utc: "2026-10-09T12:00:00Z", frozen_until_utc: "", shown_revision: 0,
  last_editor: { kind: "person", label: "You" }, backlinks: [summary("on-call", "On call")],
  open_notes: [{ note_id: "n1", body: "Fridays need two reviewers.", created_at_utc: "2026-10-10T13:00:00Z", editor: { kind: "agent", agent_type: "KnowledgeCuratorAgent", label: "KnowledgeCuratorAgent" } }],
  pending_contributions: 0, ...extra,
});

const refusal = (status, code, message, extra = {}) =>
  jsonResponse({ status: "refused", error: { code, message }, ...extra }, status);

function submit(form) {
  form.dispatchEvent(new Event("submit", { cancelable: true }));
}

function click(node) {
  node.dispatchEvent(new Event("click"));
}

const buttonNamed = (node, label) => byTag(node, "button").find(item => item.textContent === label);

async function openDeploys(f, extra = {}) {
  f.page.openPage("team", "deploys");
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys?`, jsonResponse({ status: "ok", page: detail(extra) }));
}

test("the list shows one space, asks with the session's thread and pages twenty at a time", async () => {
  const f = fixture();
  const loading = f.page.load();
  const request = await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({
    status: "ok", pages: [summary("deploys", "Deploys", { snippet: "Deploy on [Tuesdays]" })], total: 23,
  }));
  await loading;

  const query = new URL(request.path, "http://x").searchParams;
  assert.equal(query.get("session_key"), "sess_1");
  assert.equal(query.get("space"), "team");
  assert.equal(query.get("status"), "active");
  assert.equal(query.get("offset"), "0");
  assert.match(f.nodes["knowledge-list"].textContent, /DeploysDeploy on \[Tuesdays\]Changed Oct 10, 2026/);
  const pager = byClass(f.nodes["knowledge-pagination"], "list-pagination")[0];
  assert.deepEqual([pager.settings.total, pager.settings.pageSize], [23, 20]);
  assert.equal(f.nodes["knowledge-viewer-title"].textContent, "Home");
});

test("Home is the reading view and All pages reveals a bounded directory with a separate pager", async () => {
  const f = fixture();
  const loading = f.page.load();
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/home?`, jsonResponse({ status: "ok",
    page: detail({ page: "home", title: "Home", body: "Start with [[deploys]].", links: [
      { written: "deploys", space: "team", page: "deploys", exists: true },
    ] }) }));
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok",
    pages: Array.from({ length: 20 }, (_, i) => summary(`page-${i}`, `Page ${i}`)), total: 3000 }));
  await loading;
  assert.equal(f.nodes["knowledge-viewer-title"].textContent, "Home");
  assert.equal(f.nodes["knowledge-directory"].hidden, true);
  assert.equal(byClass(f.nodes["knowledge-viewer"], "knowledge-page-link").length, 1);
  click(f.nodes["knowledge-browse"]);
  assert.equal(f.nodes["knowledge-directory"].hidden, false);
  assert.equal(f.nodes["knowledge-browse"].getAttribute("aria-expanded"), "true");
  assert.equal(byClass(f.nodes["knowledge-list"], "knowledge-list-button").length, 20);
  assert.equal(byClass(f.nodes["knowledge-list"], "list-pagination").length, 0);
  assert.equal(byClass(f.nodes["knowledge-pagination"], "list-pagination")[0].settings.total, 3000);
});

test("a missing personal Home offers an explicit ordinary page creation, while service failures stay errors", async () => {
  const f = fixture({ url: "http://example.test/webchat?view=knowledge&space=personal" });
  f.page.load();
  await answer(f.requests, `${KNOWLEDGE_API}/pages/personal/home?`, refusal(404, "not_found", "No page."));
  assert.match(f.nodes["knowledge-viewer"].textContent, /no Home page yet/);
  assert.equal(f.requests.some(request => request.method === "POST"), false);
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Create Home page"));
  const editor = byClass(f.nodes["knowledge-viewer"], "knowledge-editor")[0];
  const title = byTag(editor, "input")[0];
  assert.equal(title.value, "Home");
  assert.equal(title.readOnly, true);
  byTag(editor, "textarea")[0].value = "My topics: [[travel]].";
  submit(editor);
  const saved = await answer(f.requests, `${KNOWLEDGE_API}/pages/personal`, jsonResponse({ status: "ok",
    page: detail({ space: "personal", page: "home", title: "Home" }) }));
  assert.deepEqual([saved.method, saved.body.title, saved.body.base_revision], ["POST", "Home", 0]);
  assert.equal(f.browser.location.searchParams.get("page"), "personal/home");
  click(f.nodes["knowledge-home"]);
  await answer(f.requests, `${KNOWLEDGE_API}/pages/personal/home?`, refusal(503, "runtime_error", "The manager did not answer."));
  assert.match(f.nodes["knowledge-viewer"].textContent, /manager did not answer/);
  assert.equal(buttonNamed(f.nodes["knowledge-viewer-tools"], "Create Home page"), undefined);
});

test("article addresses restore space and revision and browser navigation cannot fetch after sign-out", async () => {
  const f = fixture({ url: "http://example.test/webchat?view=knowledge&space=personal&page=team%2Fdeploys&revision=1" });
  f.page.load();
  const read = await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys?`, jsonResponse({ status: "ok",
    page: detail({ shown_revision: 1 }) }));
  assert.equal(new URL(read.path, "http://x").searchParams.get("revision"), "1");
  assert.equal(f.nodes["knowledge-personal-tab"].getAttribute("aria-selected"), "true");
  assert.match(f.nodes["knowledge-viewer"].textContent, /looking at revision 1/);
  f.browser.location = new URL("http://example.test/webchat?view=knowledge&space=team&page=team%2Fhome");
  f.browser.dispatchEvent(new Event("popstate"));
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/home?`, jsonResponse({ status: "ok",
    page: detail({ page: "home", title: "Home" }) }));
  assert.equal(f.nodes["knowledge-viewer-title"].textContent, "Home");
  assert.equal(f.nodes["knowledge-team-tab"].getAttribute("aria-selected"), "true");
  assert.equal(f.addresses.length, 0, "Back and Forward do not append history entries");
  f.page.clear();
  const count = f.requests.length;
  f.browser.dispatchEvent(new Event("popstate"));
  await settle();
  assert.equal(f.requests.length, count);
});

test("reverting an addressed historical revision makes reload open the new current page", async () => {
  const f = fixture({ url: "http://example.test/webchat?view=knowledge&space=team&page=team%2Fdeploys&revision=1" });
  f.page.load();
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys?`, jsonResponse({
    status: "ok", page: detail({ shown_revision: 1 }),
  }));
  const revert = buttonNamed(f.nodes["knowledge-viewer-tools"], "Revert to this revision");
  click(revert);
  click(revert);
  const request = await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys/revert`, jsonResponse({
    status: "ok", page: detail({ revision: 3 }),
  }));
  assert.deepEqual([request.body.revision, request.body.base_revision], [1, 2]);
  assert.match(f.nodes["knowledge-viewer-meta"].textContent, /Revision 3/);
  assert.equal(f.browser.location.searchParams.has("revision"), false);
  assert.deepEqual(f.addresses.map(entry => entry.method), ["replaceState"]);

  const reloaded = fixture({ url: f.browser.location.href });
  reloaded.page.load();
  const read = await answer(reloaded.requests, `${KNOWLEDGE_API}/pages/team/deploys?`, jsonResponse({
    status: "ok", page: detail({ revision: 3 }),
  }));
  assert.equal(new URL(read.path, "http://x").searchParams.has("revision"), false);
  assert.deepEqual(buttonLabels(reloaded.nodes["knowledge-viewer-tools"]), ["Edit", "History", "Archive"]);
  f.page.clear();
  reloaded.page.clear();
});

test("removing the last row on the final directory page returns to a reachable page", async () => {
  const f = fixture();
  f.page.load();
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [summary("first", "First")], total: 21 }));
  byClass(f.nodes["knowledge-pagination"], "list-pagination")[0].settings.onPage(20);
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 20 }));
  const corrected = await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok",
    pages: [summary("first", "First")], total: 20 }));
  assert.equal(new URL(corrected.path, "http://x").searchParams.get("offset"), "0");
  assert.match(f.nodes["knowledge-list"].textContent, /First/);
  assert.equal(f.nodes["knowledge-pagination"].hidden, true);
});

test("inactive Knowledge cleanup preserves Chat and incoming bookmarks, and disposal retires pending navigation", async () => {
  for (const url of ["http://example.test/webchat", "http://example.test/webchat?view=knowledge&page=team%2Fdeploys"]) {
    const f = fixture({ url });
    f.page.clear();
    assert.equal(f.browser.location.href, url);
  }
  const f = fixture({ url: "http://example.test/webchat?view=knowledge&page=team%2Fdeploys" });
  f.page.load();
  await settle();
  const url = f.browser.location.href;
  f.lifecycle.abort();
  const requests = f.requests.length;
  f.browser.dispatchEvent(new Event("popstate"));
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys?`, jsonResponse({ status: "ok", page: detail() }));
  assert.equal(f.browser.location.href, url);
  assert.equal(f.requests.length, requests);
  assert.doesNotMatch(f.nodes["knowledge-viewer"].textContent, /Deploy on/);
  assert.equal([...f.intervals.values()].some(timer => timer.delay === 30000), false);
});

test("an article opened from My proposals has an address that restores the article beside that list", async () => {
  const f = fixture();
  f.page.switchTab("proposals");
  await openDeploys(f);
  const url = f.browser.location.href;
  assert.equal(new URL(url).searchParams.get("page"), "team/deploys");
  const reloaded = fixture({ url });
  reloaded.page.load();
  await answer(reloaded.requests, `${KNOWLEDGE_API}/pages/team/deploys?`, jsonResponse({ status: "ok", page: detail() }));
  assert.equal(reloaded.nodes["knowledge-proposals-tab"].getAttribute("aria-selected"), "true");
  assert.equal(reloaded.nodes["knowledge-viewer-title"].textContent, "Deploys");
  assert.equal(reloaded.nodes["knowledge-directory"].hidden, false);
});

test("a page shows its text through the Markdown renderer, who changed it, links in and open notes", async () => {
  const f = fixture();
  await openDeploys(f, { status: "archived", frozen_until_utc: "2026-10-11T09:00:00Z", pending_contributions: 1 });

  assert.equal(f.nodes["knowledge-viewer-title"].textContent, "Deploys");
  assert.equal(f.nodes["knowledge-viewer-meta"].textContent,
    "Team page · How-to · Revision 2 · Last changed by You Oct 10, 2026, 12:00 PM UTC");
  assert.deepEqual(f.rendered, ["Deploy on **Tuesdays**."]);
  const viewer = f.nodes["knowledge-viewer"].textContent;
  assert.match(viewer, new RegExp(TEXT.ARCHIVED.slice(0, 30)));
  assert.match(viewer, /Agents cannot edit this page until Oct 11, 2026/);
  assert.match(viewer, /waits in My proposals/);
  assert.match(viewer, /Linked fromOn call/);
  assert.match(viewer, /Fridays need two reviewers\.KnowledgeCuratorAgent · Oct 10, 2026/);
  // An archived page offers Restore instead of Edit, and no Archive.
  assert.deepEqual(buttonLabels(f.nodes["knowledge-viewer-tools"]), ["Restore", "History"]);
});

test("a save carries the revision the person saw; a conflict keeps their text and shows the latest version", async () => {
  const f = fixture();
  await openDeploys(f);
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Edit"));
  const editor = byClass(f.nodes["knowledge-viewer"], "knowledge-editor")[0];
  const body = byTag(editor, "textarea")[0];
  body.value = "Deploy on Tuesdays and Fridays.";
  submit(editor);
  const first = await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys/save`,
    refusal(409, "conflict", "changed since you read it", { current_revision: 3 }));

  assert.equal(first.method, "POST");
  assert.equal(first.headers["X-UA-Permission-CSRF"], "csrf_1");
  assert.deepEqual([first.body.session_key, first.body.base_revision, first.body.confirm_review], ["sess_1", 2, false]);
  assert.equal(body.value, "Deploy on Tuesdays and Fridays.");
  const message = byClass(editor, "knowledge-message")[0];
  assert.equal(message.getAttribute("role"), "alert");
  assert.match(message.textContent, /Someone else changed this page/);

  click(buttonNamed(message, "Show the latest version"));
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys?`, jsonResponse({
    status: "ok", page: detail({ revision: 3, body: "Deploy on Wednesdays." }),
  }));
  assert.match(byClass(editor, "knowledge-latest")[0].textContent, /Latest version \(revision 3\)Deploy on Wednesdays\./);
  submit(editor);
  const second = await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys/save`, jsonResponse({
    status: "ok", page: detail({ revision: 4, body: "Deploy on Tuesdays and Fridays." }),
  }));
  assert.equal(second.body.base_revision, 3);
  assert.match(f.nodes["knowledge-viewer"].textContent, /Saved\./);
});

test("content held for review is listed by place and saved only with Save anyway", async () => {
  const f = fixture();
  f.page.openEditor(null);
  const editor = byClass(f.nodes["knowledge-viewer"], "knowledge-editor")[0];
  const [title] = byTag(editor, "input");
  title.value = "Contacts";
  byTag(editor, "textarea")[0].value = "Write to ops@example.com";
  submit(editor);
  const first = await answer(f.requests, `${KNOWLEDGE_API}/pages/team`, refusal(409, "review_required",
    "This team page change contains email addresses. Everyone on the team will see it.", {
      review_flags: [{ kind: "email_address", label: "an email address", field: "body", line: 1, column: 10 }],
    }));

  assert.equal(first.path, `${KNOWLEDGE_API}/pages/team`);
  assert.equal(first.body.base_revision, 0);
  const message = byClass(editor, "knowledge-message")[0];
  assert.match(message.textContent, /an email address \(the body, line 1, column 10\)/);
  assert.doesNotMatch(message.textContent, /ops@example\.com/);
  click(buttonNamed(message, "Save anyway"));
  const second = await answer(f.requests, `${KNOWLEDGE_API}/pages/team`, jsonResponse({
    status: "ok", page: detail({ title: "Contacts", page: "contacts" }),
  }));
  assert.equal(second.body.confirm_review, true);
  assert.match(f.nodes["knowledge-viewer"].textContent, /Page created\./);
});

test("never-stored content is refused with its places and no Save anyway", async () => {
  const f = fixture();
  f.page.openEditor(null);
  const editor = byClass(f.nodes["knowledge-viewer"], "knowledge-editor")[0];
  byTag(editor, "input")[0].value = "Database";
  byTag(editor, "textarea")[0].value = "password: hunter2";
  submit(editor);
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team`, refusal(422, "sensitive_content", "never stores credentials", {
    review_flags: [{ kind: "password", label: "a password or secret value", field: "body", line: 1, column: 11 }],
  }));

  const message = byClass(editor, "knowledge-message")[0];
  assert.match(message.textContent, new RegExp(TEXT.SENSITIVE.slice(0, 40)));
  assert.match(message.textContent, /a password or secret value \(the body, line 1, column 11\)/);
  assert.equal(buttonNamed(message, "Save anyway"), undefined);
});

test("archive asks for a second click on the same button before it sends", async () => {
  const f = fixture();
  await openDeploys(f);
  const archive = buttonNamed(f.nodes["knowledge-viewer-tools"], "Archive");
  click(archive);
  await settle();
  assert.equal(archive.textContent, "Confirm archive");
  assert.equal(openRequests(f.requests).length, 0);
  click(archive);
  const request = await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys/archive`, jsonResponse({
    status: "ok", page: detail({ status: "archived", revision: 3 }),
  }));
  assert.deepEqual(request.body, { session_key: "sess_1", base_revision: 2 });
  assert.match(f.nodes["knowledge-viewer"].textContent, /Archived\. Every revision is kept/);
});

test("history lists revisions with who and when, and Revert needs a second click", async () => {
  const f = fixture();
  await openDeploys(f);
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "History"));
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys/history?`, jsonResponse({
    status: "ok", pages: [summary("deploys", "Deploys", { revision: 2 })], total: 2,
    revisions: [
      { revision: 2, title: "Deploys", status: "active", change_kind: "edit", change_summary: "", created_at_utc: "2026-10-10T12:00:00Z", editor: { kind: "person", label: "Beatrix" } },
      { revision: 1, title: "Deploys", status: "active", change_kind: "create", change_summary: "", created_at_utc: "2026-10-09T12:00:00Z", editor: { kind: "person", label: "You" } },
    ],
  }));

  const rows = byClass(f.nodes["knowledge-viewer"], "knowledge-revision");
  assert.match(rows[0].textContent, /Revision 2 · EditedBy Beatrix/);
  assert.match(rows[0].textContent, /Current/);
  const revert = buttonNamed(rows[1], "Revert");
  click(revert);
  await settle();
  assert.equal(openRequests(f.requests).length, 0);
  click(revert);
  const request = await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys/revert`, jsonResponse({
    status: "ok", page: detail({ revision: 3 }),
  }));
  assert.deepEqual([request.body.revision, request.body.base_revision], [1, 2]);
});

const PROPOSAL = {
  contribution_id: "kb_contrib_1", kind: "edit", page: "deploys", title: "Deploys", page_type: "how-to",
  summary: "How we ship.", tags: ["release"], body: "Deploy on Tuesdays.\nDeploy on Fridays.", change_summary: "Fridays too.",
  base_revision: 2, agent_type: "WebResearchAgent", created_at_utc: "2026-10-10T14:00:00Z",
  review_flags: [{ kind: "email_address", label: "an email address", field: "", line: 0, column: 0 }],
  current: detail({ body: "Deploy on Tuesdays." }),
};

async function openProposal(f) {
  f.page.switchTab("proposals");
  await answer(f.requests, `${KNOWLEDGE_API}/contributions?`, jsonResponse({
    status: "ok", contributions: [{ ...PROPOSAL, body: "", current: null }], total: 1,
  }));
  click(byClass(f.nodes["knowledge-list"], "knowledge-list-button")[0]);
  await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1?`, jsonResponse({ status: "ok", contribution: PROPOSAL }));
}

test("a proposal shows what it would change, marked line by line, with its flags and the agent's reason", async () => {
  const f = fixture();
  await openProposal(f);

  assert.equal(f.nodes["knowledge-new"].hidden, true);
  assert.equal(f.nodes["knowledge-search"].hidden, true);
  const viewer = f.nodes["knowledge-viewer"];
  assert.match(viewer.textContent, /details that need your decision.*an email address/);
  assert.match(viewer.textContent, /Why \(from WebResearchAgent\)Fridays too\./);
  assert.deepEqual(byClass(viewer, "knowledge-diff-line").map(line => line.textContent),
    ["  Deploy on Tuesdays.\n", "+ Deploy on Fridays.\n"]);
  assert.deepEqual(buttonLabels(f.nodes["knowledge-viewer-tools"]), ["Publish", "Edit, then publish", "Withdraw"]);
});

test("publishing flagged content needs a second click and sends the revision the person compared with", async () => {
  const f = fixture();
  await openProposal(f);
  const publish = buttonNamed(f.nodes["knowledge-viewer-tools"], "Publish");
  click(publish);
  await settle();
  assert.equal(openRequests(f.requests).length, 0);
  click(publish);
  const request = await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1/publish`, jsonResponse({
    status: "ok", page: detail({ revision: 3 }),
  }));
  assert.deepEqual(request.body, { session_key: "sess_1", base_revision: 2 });
  assert.match(f.nodes["knowledge-viewer"].textContent, /Published to the team page “Deploys”/);
});

test("edit, then publish sends the person's version; withdraw needs a second click", async () => {
  const f = fixture();
  await openProposal(f);
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Edit, then publish"));
  const editor = byClass(f.nodes["knowledge-viewer"], "knowledge-editor")[0];
  byTag(editor, "textarea")[0].value = "Deploy on Tuesdays and Fridays.";
  submit(editor);
  const published = await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1/publish`,
    refusal(409, "conflict", "changed"));
  assert.equal(published.body.edited, true);
  assert.equal(published.body.body, "Deploy on Tuesdays and Fridays.");
  assert.equal(published.body.base_revision, 2);
  assert.equal(byTag(editor, "textarea")[0].value, "Deploy on Tuesdays and Fridays.");

  // Back to the proposal from the list (the tab is still My proposals).
  click(byClass(f.nodes["knowledge-list"], "knowledge-list-button")[0]);
  await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1?`, jsonResponse({ status: "ok", contribution: PROPOSAL }));
  const withdraw = buttonNamed(f.nodes["knowledge-viewer-tools"], "Withdraw");
  click(withdraw);
  await settle();
  assert.equal(openRequests(f.requests).length, 0);
  click(withdraw);
  await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1/withdraw`, jsonResponse({ status: "ok" }));
  await answer(f.requests, `${KNOWLEDGE_API}/contributions?`, jsonResponse({ status: "ok", contributions: [], total: 0 }));
  assert.match(f.nodes["knowledge-viewer"].textContent, /Proposal withdrawn/);
  assert.match(f.nodes["knowledge-list"].textContent, /No proposals are waiting/);
});

test("a list answer that arrives after switching tabs is ignored", async () => {
  const f = fixture();
  f.page.load();
  await settle();
  f.page.switchTab("personal");
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [summary("team-only", "Team only")], total: 1 }));
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));

  assert.doesNotMatch(f.nodes["knowledge-list"].textContent, /Team only/);
  assert.match(f.nodes["knowledge-list"].textContent, /no personal pages yet/);
  assert.equal(f.nodes["knowledge-personal-tab"].getAttribute("aria-selected"), "true");
});

test("sign-out clears the page", async () => {
  const f = fixture();
  await openDeploys(f);
  f.page.clear();
  assert.equal(f.nodes["knowledge-viewer"].children.length, 0);
  assert.equal(f.nodes["knowledge-list"].children.length, 0);
  assert.equal(f.nodes["knowledge-team-tab"].getAttribute("aria-selected"), "true");
  assert.equal(f.nodes["knowledge-page-status"].value, "active");
});

test("Refresh keeps the displayed page's space after following a cross-space link", async () => {
  const f = fixture();
  f.page.switchTab("personal");
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
  f.page.openPage("personal", "deploys");
  await answer(f.requests, `${KNOWLEDGE_API}/pages/personal/deploys?`, jsonResponse({
    status: "ok", page: detail({ space: "personal", body: "See [[team/deploys]].", links: [
      { written: "team/deploys", space: "team", page: "deploys", exists: true },
    ] }),
  }));
  click(byClass(f.nodes["knowledge-viewer"], "knowledge-page-link")[0]);
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys?`, jsonResponse({ status: "ok", page: detail() }));
  click(f.nodes["knowledge-refresh"]);
  const pageRequest = await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys?`, jsonResponse({
    status: "ok", page: detail({ revision: 3, body: "Latest team text." }),
  }));
  const listRequest = await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
  assert.equal(new URL(listRequest.path, "http://x").searchParams.get("space"), "personal");
  assert.equal(f.nodes["knowledge-personal-tab"].getAttribute("aria-selected"), "true");
  assert.match(pageRequest.path, /\/pages\/team\/deploys\?/);
  assert.match(f.nodes["knowledge-viewer"].textContent, /Latest team text/);
});

test("Refresh keeps a page followed from an archive proposal while My proposals stays selected", async () => {
  const f = fixture();
  f.page.switchTab("proposals");
  await answer(f.requests, `${KNOWLEDGE_API}/contributions?`, jsonResponse({
    status: "ok", contributions: [{ ...PROPOSAL, kind: "archive", body: "", current: null }], total: 1,
  }));
  click(byClass(f.nodes["knowledge-list"], "knowledge-list-button")[0]);
  await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1?`, jsonResponse({
    status: "ok", contribution: { ...PROPOSAL, kind: "archive", current: detail({ body: "See [[team/on-call]].", links: [
      { written: "team/on-call", space: "team", page: "on-call", exists: true },
    ] }) },
  }));
  click(byClass(f.nodes["knowledge-viewer"], "knowledge-page-link")[0]);
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/on-call?`, jsonResponse({
    status: "ok", page: detail({ page: "on-call", title: "On call", body: "On-call guidance." }),
  }));
  click(f.nodes["knowledge-refresh"]);
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/on-call?`, jsonResponse({
    status: "ok", page: detail({ page: "on-call", title: "On call", revision: 3, body: "Updated on-call guidance." }),
  }));
  await answer(f.requests, `${KNOWLEDGE_API}/contributions?`, jsonResponse({ status: "ok", contributions: [], total: 0 }));
  assert.equal(f.nodes["knowledge-proposals-tab"].getAttribute("aria-selected"), "true");
  assert.match(f.nodes["knowledge-viewer"].textContent, /Updated on-call guidance/);
  assert.equal(f.requests.some(request => request.path.includes("/contributions/team%2Fon-call")), false);
});

test("archived pages stay searchable and paginated, can be restored, and reset on sign-out", async () => {
  const f = fixture();
  f.page.load();
  await settle();
  f.nodes["knowledge-page-status"].value = "archived";
  f.nodes["knowledge-page-status"].dispatchEvent(new Event("change"));
  // The earlier active response must not replace the filtered list.
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [summary("current", "Current")], total: 1 }));
  const filtered = await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({
    status: "ok", pages: [summary("deploys", "Deploys", { status: "archived" })], total: 21,
  }));
  assert.equal(new URL(filtered.path, "http://x").searchParams.get("status"), "archived");
  assert.doesNotMatch(f.nodes["knowledge-list"].textContent, /Current/);
  assert.match(f.nodes["knowledge-list"].textContent, /Archived/);
  byClass(f.nodes["knowledge-pagination"], "list-pagination")[0].settings.onPage(20);
  const secondPage = await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 21 }));
  assert.deepEqual(["status", "offset"].map(key => new URL(secondPage.path, "http://x").searchParams.get(key)), ["archived", "20"]);
  f.nodes["knowledge-query"].value = "deploy";
  submit(f.nodes["knowledge-search"]);
  const searched = await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({
    status: "ok", pages: [summary("deploys", "Deploys", { status: "archived" })], total: 1,
  }));
  assert.deepEqual(["status", "offset", "query"].map(key => new URL(searched.path, "http://x").searchParams.get(key)), ["archived", "0", "deploy"]);
  click(byClass(f.nodes["knowledge-list"], "knowledge-list-button")[0]);
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys?`, jsonResponse({ status: "ok", page: detail({ status: "archived" }) }));
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Restore"));
  submit(byClass(f.nodes["knowledge-viewer"], "knowledge-editor")[0]);
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys/save`, jsonResponse({ status: "ok", page: detail({ revision: 3 }) }));
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
  assert.match(f.nodes["knowledge-list"].textContent, /No pages match this search/);
  assert.deepEqual(buttonLabels(f.nodes["knowledge-viewer-tools"]), ["Edit", "History", "Archive"]);
  f.nodes["knowledge-query"].value = "";
  submit(f.nodes["knowledge-search"]);
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
  assert.match(f.nodes["knowledge-list"].textContent, /No archived pages/);
  f.page.switchTab("proposals");
  assert.equal(f.nodes["knowledge-search"].hidden, true);
  f.page.clear();
  assert.equal(f.nodes["knowledge-page-status"].value, "active");
});

test("publication edits explicitly send redacted note text and an empty summary", async () => {
  for (const kind of ["note", "edit"]) {
    const f = fixture();
    f.page.openProposal("kb_contrib_1");
    await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1?`, jsonResponse({
      status: "ok", contribution: { ...PROPOSAL, kind, summary: "Private contact.", body: "Ask ops@example.com" },
    }));
    click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Edit, then publish"));
    const editor = byClass(f.nodes["knowledge-viewer"], "knowledge-editor")[0];
    byTag(editor, "input")[1].value = "";
    byTag(editor, "textarea")[0].value = "Ask the operations team.";
    submit(editor);
    const request = await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1/publish`, jsonResponse({ status: "ok", page: detail() }));
    assert.equal(request.body.edited, true);
    assert.equal(request.body.summary, "");
    assert.equal(request.body.body, "Ask the operations team.");
  }
});

test("a stale archive proposal explains that publishing preserves the current text", async () => {
  const f = fixture();
  f.page.openProposal("kb_contrib_1");
  await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1?`, jsonResponse({
    status: "ok", contribution: { ...PROPOSAL, kind: "archive", current: detail({ revision: 3, body: "Newer text." }) },
  }));
  assert.match(f.nodes["knowledge-viewer"].textContent, /page changed after this was proposed/i);
  assert.match(f.nodes["knowledge-viewer"].textContent, /archive the current page and keep its text and details/);
  assert.match(f.nodes["knowledge-viewer"].textContent, /Newer text/);
  assert.equal(byClass(f.nodes["knowledge-viewer"], "knowledge-diff").length, 0);
  assert.deepEqual(buttonLabels(f.nodes["knowledge-viewer-tools"]), ["Publish", "Withdraw"]);
});

test("late page, list, conflict and archive replies do not alter a later session", async () => {
  for (const action of ["open", "list", "conflict", "archive"]) {
    const f = fixture();
    let oldRequest;
    let oldEditor;
    if (action === "open") f.page.openPage("team", "old-private-page");
    else if (action === "list") f.page.load();
    else {
      await openDeploys(f);
      if (action === "archive") {
        const archive = buttonNamed(f.nodes["knowledge-viewer-tools"], "Archive");
        click(archive); click(archive);
      } else {
        click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Edit"));
        oldEditor = byClass(f.nodes["knowledge-viewer"], "knowledge-editor")[0];
        submit(oldEditor);
        await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys/save`, refusal(409, "conflict", "changed"));
        click(buttonNamed(oldEditor, "Show the latest version"));
      }
    }
    await settle();
    oldRequest = openRequests(f.requests)[0];
    oldRequest.answered = true;
    f.page.clear();
    await openDeploys(f, { body: "New session's page." });
    oldRequest.respond(action === "archive" ? refusal(409, "conflict", "Old session failed") : jsonResponse({
      status: "ok", page: detail({ body: "Old private text." }), pages: [summary("old", "Old private text")], total: 1,
    }));
    await settle();
    assert.match(f.nodes["knowledge-viewer"].textContent, /New session's page/);
    assert.doesNotMatch(f.nodes["knowledge-viewer"].textContent + f.nodes["knowledge-list"].textContent + f.nodes["knowledge-status"].textContent, /Old private text|Old session|Someone else changed/);
    if (oldEditor) assert.equal(byClass(oldEditor, "knowledge-latest").length, 0);
  }
});

test("a delayed session lookup cannot send a request after sign-out", async () => {
  let resolveSession;
  const f = fixture({ getSessionKey: () => new Promise(resolve => { resolveSession = resolve; }) });
  f.page.openPage("personal", "private");
  f.page.clear();
  resolveSession("old_session");
  await settle();
  assert.equal(f.requests.length, 0);
  assert.equal(f.nodes["knowledge-viewer"].children.length, 0);

  // The Settings client starts fetch in another microtask after the session
  // lookup. Sign-out between those steps must also prevent the old request.
  const queued = fixture();
  queued.page.openPage("personal", "private");
  queueMicrotask(() => queued.page.clear());
  await settle();
  assert.equal(queued.requests.length, 0);

  for (const method of ["get", "post", "send"]) {
    const f = fixture();
    const request = method === "get" ? f.client.get("/old-session", "Failed")
      : method === "post" ? f.client.post("/old-session", {}, "Failed")
      : f.client.send("POST", "/old-session", {}, "Failed");
    f.client.invalidateSession();
    await assert.rejects(request, { name: "AbortError" });
    assert.equal(f.requests.length, 0, method + " sent a request queued before logout");
  }
});

test("an old Knowledge 401 cannot sign out a later authenticated session", async () => {
  for (const pending of ["fetch", "json"]) {
    let unauthorized = 0;
    const f = fixture({ onUnauthorized: () => { unauthorized += 1; f.page.clear(); } });
    f.page.openPage("personal", "old-private-page");
    await settle();
    const oldRequest = openRequests(f.requests)[0];
    oldRequest.answered = true;
    let resolveBody;
    if (pending === "json") {
      const response = refusal(401, "expired_session", "Old session expired");
      response.json = () => new Promise(resolve => { resolveBody = resolve; });
      oldRequest.respond(response);
      await settle();
    }
    f.client.setCsrfToken("");
    f.page.clear();
    f.client.setCsrfToken("csrf_new_session");
    await openDeploys(f, { body: "New session's page." });
    if (pending === "fetch") oldRequest.respond(refusal(401, "expired_session", "Old session expired"));
    else resolveBody({ error: { code: "expired_session", message: "Old session expired" } });
    await settle();
    assert.equal(unauthorized, 0, "an old " + pending + " response signed out the new session");
    assert.match(f.nodes["knowledge-viewer"].textContent, /New session's page/);
    // A response for the current session still follows normal sign-out handling.
    f.page.openPage("personal", "new-private-page");
    await answer(f.requests, `${KNOWLEDGE_API}/pages/personal/new-private-page?`, refusal(401, "expired_session", "Current session expired"));
    assert.equal(unauthorized, 1);
    assert.equal(f.nodes["knowledge-viewer"].children.length, 0);
  }
});

test("a conversation lookup from before Knowledge logout cannot replace the next session's conversation", async () => {
  const originalWindow = globalThis.window;
  const originalDocument = globalThis.document;
  try {
    for (const pending of ["list", "create", "json"]) {
      let authentication;
      const f = fixture({ getSessionKey: () => authentication.ensureRuntimeControlsSession() });
      globalThis.window = {};
      globalThis.document = f.document;
      const state = {
        knowledgeClient: f.client, knowledgeControls: f.page, workspaceControls: { clear() {} },
        knowledgeApp: f.nodes["knowledge-app"],
      };
      for (const name of ["logoutButton", "objectivesLogout", "workspaceLogout", "statusLogout", "knowledgeLogout",
        "enrollmentCancelButton", "enrollmentOverlay", "enrollmentToken", "enrollmentReveal", "enrollmentCopyButton",
        "enrollmentCompleteButton", "authPanel", "authError", "chatApp", "objectivesApp", "workspaceApp", "statusApp",
        "settingsWorkspaceLink", "settingsStatusLink", "settingsKnowledgeLink"]) state[name] = f.document.createElement("div");
      const shared = {
        authSessionActive: true, authSessionGeneration: 1, runtimeControlsSessionKey: "",
        runtimeControlSnapshots: new Map(), settingsControls: { clear() {} },
        threadList: { innerHTML: "" }, messageList: { innerHTML: "" },
        accountNameLabel: f.document.createElement("span"), messageInput: f.document.createElement("input"),
        objectivesContent: f.document.createElement("div"), objectivesUpdatedAt: f.document.createElement("span"),
      };
      withElements(f.document, [["status-content", "div"]]);
      // Use chat.js's actual response parser so its unauthorized callback is
      // exercised even when the response's JSON completes after sign-out.
      const source = readFileSync(new URL("../assistant_gateway/onboarding_web/assets/chat.js", import.meta.url), "utf8");
      const parserStart = source.indexOf("function parseApiResponse(");
      const parserSource = source.slice(parserStart, source.indexOf("\nfunction fetchSessions(", parserStart));
      const parserContext = { authentication: null };
      const parseApiResponse = vm.runInNewContext("(" + parserSource + ")", parserContext);
      authentication = createAuthentication({ authenticationState: state, sharedState: shared,
        lifecycle: { clearInterval() {} }, requestClient: { fetch: f.browser.fetch }, operations: {
          parseApiResponse, stopLiveEvents() {}, clearOptimisticDeliveries() {},
          clearPendingAttachments() {}, setObjectivesStatus() {}, showSignIn() {},
        },
      });
      parserContext.authentication = authentication;
      f.page.load();
      if (pending === "create") await answer(f.requests, "/api/web-chat/sessions", jsonResponse({ sessions: [] }));
      await settle();
      const oldLookup = openRequests(f.requests)[0];
      oldLookup.answered = true;
      let resolveOldBody;
      if (pending === "json") {
        const response = refusal(401, "expired_session", "Old session expired");
        response.json = () => new Promise(resolve => { resolveOldBody = resolve; });
        oldLookup.respond(response);
        await settle();
      }
      authentication.logoutChat();
      await answer(f.requests, "/api/web-chat/logout", jsonResponse({ status: "ok" }));
      assert.equal(shared.authSessionActive, false);
      // A successful later sign-in marks a fresh generation and supplies its CSRF token.
      shared.authSessionGeneration += 1;
      shared.authSessionActive = true;
      f.client.setCsrfToken("csrf_new_session");
      f.page.load();
      await answer(f.requests, "/api/web-chat/sessions", jsonResponse({ sessions: [{ session_key: "new_session" }] }));
      await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
      if (pending === "json") resolveOldBody({ message: "Old session expired" });
      else oldLookup.respond(jsonResponse(pending === "list" ? { sessions: [{ session_key: "old_session" }] }
        : { session: { session_key: "old_session" } }));
      await settle();
      assert.equal(shared.authSessionActive, true, "an old session reply signed out the next person");
      click(f.nodes["knowledge-refresh"]);
      const refreshed = await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
      assert.equal(new URL(refreshed.path, "http://x").searchParams.get("session_key"), "new_session");
      assert.equal(shared.runtimeControlsSessionKey, "new_session");
    }
  } finally {
    globalThis.window = originalWindow;
    globalThis.document = originalDocument;
  }
});

test("the line diff marks added and removed lines and gives up on very long texts", () => {
  assert.deepEqual(lineDiff("a\nb\nc", "a\nc\nd"), [
    { kind: "same", line: "a" }, { kind: "removed", line: "b" }, { kind: "same", line: "c" }, { kind: "added", line: "d" },
  ]);
  assert.equal(lineDiff("x\n".repeat(600), "y\n".repeat(600)), null);
  assert.equal(describeFlag({ label: "a birth date", field: "summary", line: 1, column: 4 }), "a birth date (the summary, line 1, column 4)");
});

test("links in a page's text open their page; a missing page and code stay text", async () => {
  const f = fixture();
  await openDeploys(f, {
    body: "See [[ci-pipeline]], [[old-notes|the old notes]], [[team/missing]] and `[[ci-pipeline]]`.",
    links: [
      { written: "ci-pipeline", space: "team", page: "ci-pipeline", exists: true },
      { written: "old-notes", space: "team", page: "old-notes", exists: true },
      { written: "team/missing", space: "team", page: "missing", exists: false },
    ],
  });
  const body = byClass(f.nodes["knowledge-viewer"], "knowledge-body")[0];
  const links = byClass(body, "knowledge-page-link");
  assert.deepEqual(links.map(link => link.textContent), ["ci-pipeline", "the old notes"]);
  assert.equal(links[1].getAttribute("aria-label"), "Open the page the old notes");
  assert.equal(byClass(body, "knowledge-missing-link")[0].textContent, "team/missing (no page yet)");
  // The code span keeps its link syntax as written.
  assert.equal(byTag(body, "code")[0].textContent, "[[ci-pipeline]]");
  assert.match(body.textContent, /^See ci-pipeline, the old notes, team\/missing \(no page yet\) and \[\[ci-pipeline\]\]\.$/);

  click(links[1]);
  const opened = await answer(f.requests, `${KNOWLEDGE_API}/pages/team/old-notes?`, jsonResponse({
    status: "ok", page: detail({ page: "ci-pipeline", title: "CI pipeline", redirected_from: "team/old-notes", links: [] }),
  }));
  assert.equal(new URL(opened.path, "http://x").searchParams.get("session_key"), "sess_1");
  assert.equal(f.nodes["knowledge-viewer-title"].textContent, "CI pipeline");
  assert.match(f.nodes["knowledge-viewer"].textContent, /old-notes was merged into this page\./);
});

test("a new page under a merged page's name says where that name leads now", async () => {
  const f = fixture();
  f.page.openEditor(null);
  const editor = byClass(f.nodes["knowledge-viewer"], "knowledge-editor")[0];
  byTag(editor, "input")[0].value = "Old notes";
  byTag(editor, "textarea")[0].value = "Fresh text.";
  submit(editor);
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team`, refusal(409, "merged", "was merged", { merged_into: "team/ci-pipeline" }));

  const message = byClass(editor, "knowledge-message")[0];
  assert.equal(message.getAttribute("role"), "alert");
  assert.match(message.textContent, /That name now leads to ci-pipeline, because that page was merged; edit that page or choose another name\./);
  assert.equal(mergedText("team/ci-pipeline").includes("ci-pipeline"), true);
  assert.equal(byTag(editor, "textarea")[0].value, "Fresh text.");
});

test("history names merges plainly", async () => {
  const f = fixture();
  await openDeploys(f);
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "History"));
  await answer(f.requests, `${KNOWLEDGE_API}/pages/team/deploys/history?`, jsonResponse({
    status: "ok", pages: [summary("deploys", "Deploys", { revision: 3 })], total: 3,
    revisions: [{
      revision: 3, title: "Deploys", status: "active", change_kind: "merge",
      change_summary: "Merged team/old-notes into this page", created_at_utc: "2026-10-10T12:00:00Z",
      editor: { kind: "agent", agent_type: "KnowledgeCuratorAgent", label: "KnowledgeCuratorAgent" },
    }],
  }));
  const row = byClass(f.nodes["knowledge-viewer"], "knowledge-revision")[0];
  assert.match(row.textContent, /Revision 3 · MergedBy KnowledgeCuratorAgent/);
  assert.match(row.textContent, /Merged team\/old-notes into this page/);
});


test("the My proposals badge counts waiting proposals, follows the list and decisions, and clears at sign-out", async () => {
  const f = fixture();
  const badge = f.nodes["knowledge-proposals-count"];
  const loading = f.page.load();
  const counted = await answer(f.requests, `${KNOWLEDGE_COUNTS_PATH}?`, jsonResponse({ status: "ok", pending_proposals: 2 }));
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
  await loading;
  await settle();
  assert.equal(new URL(counted.path, "http://x").searchParams.get("session_key"), "sess_1");
  assert.equal(badge.hidden, false);
  assert.equal(badge.textContent, "2");
  assert.equal(badge.getAttribute("title"), "2 proposals waiting for you");
  assert.equal(f.nodes["knowledge-proposals-tab"].getAttribute("aria-label"), "My proposals (2 proposals waiting for you)");
  assert.ok([...f.intervals.values()].some(timer => timer.delay === 30000), "The count is read again every 30 seconds");

  // The proposals list carries the count, so no separate read is needed there.
  await openProposal(f);
  assert.equal(badge.textContent, "1");
  assert.equal(badge.getAttribute("title"), "1 proposal waiting for you");
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Withdraw"));
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Confirm withdraw"));
  await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1/withdraw`, jsonResponse({ status: "ok" }));
  await answer(f.requests, `${KNOWLEDGE_API}/contributions?`, jsonResponse({ status: "ok", contributions: [], total: 0 }));
  await settle();
  assert.equal(badge.hidden, true, "Nothing waiting: no badge");
  assert.equal(f.nodes["knowledge-proposals-tab"].getAttribute("aria-label"), "My proposals");
  assert.equal(badge.textContent, "");

  f.page.clear();
  assert.ok(![...f.intervals.values()].some(timer => timer.delay === 30000), "Sign-out stops the reads");
});

test("a count read that fails or answers nonsense keeps the last number", async () => {
  assert.throws(() => pendingProposals({ status: "ok" }), new RegExp(TEXT.COUNT_FAILED));
  assert.throws(() => pendingProposals({ pending_proposals: -1 }));
  assert.equal(pendingProposals({ pending_proposals: 0 }), 0);
  assert.equal(proposalCountLabel(0), "");

  const f = fixture();
  const loading = f.page.load();
  await answer(f.requests, `${KNOWLEDGE_COUNTS_PATH}?`, jsonResponse({ status: "ok", pending_proposals: 4 }));
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
  await loading;
  await settle();
  const poll = [...f.intervals.values()].find(timer => timer.delay === 30000);
  f.document.hidden = false;
  poll.callback();
  await answer(f.requests, `${KNOWLEDGE_COUNTS_PATH}?`, jsonResponse({ status: "ok" }));
  await settle();
  poll.callback();
  await answer(f.requests, `${KNOWLEDGE_COUNTS_PATH}?`, refusal(503, "knowledge_off", "Off."));
  await settle();
  assert.equal(f.nodes["knowledge-proposals-count"].textContent, "4");
});

async function loadCountedPage(f) {
  const loaded = f.page.load();
  await answer(f.requests, `${KNOWLEDGE_COUNTS_PATH}?`, jsonResponse({ status: "ok", pending_proposals: 1 }));
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
  await loaded;
}

test("a delayed count poll cannot restore a proposal withdrawn from the refreshed list", async () => {
  const f = fixture();
  await loadCountedPage(f);
  await openProposal(f);
  const poll = [...f.intervals.values()].find(timer => timer.delay === 30000);
  poll.callback();
  await settle();
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Withdraw"));
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Confirm withdraw"));
  await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1/withdraw`, jsonResponse({ status: "ok" }));
  await answer(f.requests, `${KNOWLEDGE_API}/contributions?`, jsonResponse({ status: "ok", contributions: [], total: 0 }));
  assert.equal(f.nodes["knowledge-proposals-count"].hidden, true);
  await answer(f.requests, `${KNOWLEDGE_COUNTS_PATH}?`, jsonResponse({ status: "ok", pending_proposals: 1 }));
  assert.equal(f.nodes["knowledge-proposals-count"].hidden, true);
  f.page.clear();
});

test("a decision refreshes the count after switching tabs during the decision or its list refresh", async t => {
  for (const decision of ["withdraw", "publish"]) {
    for (const stage of ["decision", "list"]) {
      await t.test(`${decision}, switch during ${stage}`, async () => {
        const f = fixture();
        await loadCountedPage(f);
        await openProposal(f);
        const label = decision === "withdraw" ? "Withdraw" : "Publish";
        click(buttonNamed(f.nodes["knowledge-viewer-tools"], label));
        click(buttonNamed(f.nodes["knowledge-viewer-tools"], `Confirm ${decision}`));
        await settle();
        const reply = jsonResponse({ status: "ok", ...(decision === "publish" ? { page: detail({ revision: 3 }) } : {}) });
        const path = `${KNOWLEDGE_API}/contributions/kb_contrib_1/${decision}`;
        if (stage === "list") await answer(f.requests, path, reply);
        f.page.switchTab("team");
        await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
        if (stage === "decision") await answer(f.requests, path, reply);
        else await answer(f.requests, `${KNOWLEDGE_API}/contributions?`, jsonResponse({ status: "ok", contributions: [], total: 0 }));
        await answer(f.requests, `${KNOWLEDGE_COUNTS_PATH}?`, jsonResponse({ status: "ok", pending_proposals: 0 }));
        assert.equal(f.nodes["knowledge-proposals-count"].hidden, true);
        assert.equal(f.nodes["knowledge-team-tab"].getAttribute("aria-selected"), "true");
        assert.equal(f.nodes["knowledge-viewer-title"].textContent, "Home");
        f.page.clear();
      });
    }
  }
});

test("a successful withdrawal updates the count when the refreshed list fails", async () => {
  const f = fixture();
  await loadCountedPage(f);
  await openProposal(f);
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Withdraw"));
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Confirm withdraw"));
  await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1/withdraw`, jsonResponse({ status: "ok" }));
  await answer(f.requests, `${KNOWLEDGE_API}/contributions?`, refusal(503, "runtime_error", "The list could not be loaded."));
  await answer(f.requests, `${KNOWLEDGE_COUNTS_PATH}?`, jsonResponse({ status: "ok", pending_proposals: 0 }));
  assert.equal(f.nodes["knowledge-proposals-count"].hidden, true);
  assert.match(f.nodes["knowledge-status"].textContent, /The list could not be loaded/);
  f.page.clear();
});

test("a decision parsed after sign-out cannot refresh or change the next person's count", async () => {
  const f = fixture();
  await loadCountedPage(f);
  await openProposal(f);
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Withdraw"));
  click(buttonNamed(f.nodes["knowledge-viewer-tools"], "Confirm withdraw"));
  let finishBody;
  const response = jsonResponse({ status: "ok" });
  response.json = () => new Promise(resolve => { finishBody = resolve; });
  await answer(f.requests, `${KNOWLEDGE_API}/contributions/kb_contrib_1/withdraw`, response);
  assert.equal(typeof finishBody, "function");
  f.page.clear();
  const loaded = f.page.load();
  await answer(f.requests, `${KNOWLEDGE_COUNTS_PATH}?`, jsonResponse({ status: "ok", pending_proposals: 2 }));
  await answer(f.requests, `${KNOWLEDGE_API}/pages?`, jsonResponse({ status: "ok", pages: [], total: 0 }));
  await loaded;
  const before = f.requests.length;
  finishBody({ status: "ok" });
  await settle();
  assert.equal(f.requests.length, before, "An old decision cannot trigger a read in the next session");
  assert.equal(f.nodes["knowledge-proposals-count"].textContent, "2");
  f.page.clear();
});

test("lookups of the conversation asked at the same time share one request and create at most one conversation", async () => {
  const f = fakeBrowser();
  const shared = { authSessionGeneration: 1, runtimeControlsSessionKey: "", runtimeControlsSessionLookup: null };
  const authentication = createAuthentication({
    authenticationState: {}, sharedState: shared, lifecycle: {}, requestClient: { fetch: f.browser.fetch },
    operations: { parseApiResponse: response => response.json() },
  });
  const first = authentication.ensureRuntimeControlsSession();
  const second = authentication.ensureRuntimeControlsSession();
  await settle();
  assert.equal(openRequests(f.requests).length, 1, "One lookup for both callers");
  await answer(f.requests, "/api/web-chat/sessions", jsonResponse({ sessions: [] }));
  await answer(f.requests, "/api/web-chat/sessions", jsonResponse({ session: { session_key: "created_1" } }));
  assert.deepEqual(await Promise.all([first, second]), ["created_1", "created_1"]);
  assert.equal(f.requests.filter(request => request.method === "POST").length, 1, "One conversation was created");
  assert.equal(shared.runtimeControlsSessionLookup, null, "A finished lookup is not kept");

  // A new sign-in does not share a lookup that started before it.
  shared.runtimeControlsSessionKey = "";
  const old = authentication.ensureRuntimeControlsSession();
  shared.authSessionGeneration += 1;
  const fresh = authentication.ensureRuntimeControlsSession();
  await settle();
  assert.equal(openRequests(f.requests).length, 2);
  old.catch(() => {});
  fresh.catch(() => {});
});

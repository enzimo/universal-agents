// Workspace Files: the Share… and Send… dialogs (workspace_file_actions.mjs)
// and the file tree that offers them (workspace_browser.js, a classic script
// loaded here the way chat.html loads it).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import {
  ADMIN_VIEW_TITLE, DOWNLOAD_ACTION_LABEL, MEMBER_VIEW_TITLE, SEND_NOTES, createWorkspaceFileActions, focusStops,
  renderShareDialog, rowActions, sendRequestBody, shareRequestBody, teamFolderTarget, workspaceViewTitle,
} from "../assistant_gateway/onboarding_web/assets/workspace_file_actions.mjs";
import { stripWorkspaceDownloadParam } from "../assistant_gateway/onboarding_web/assets/chat_links.mjs";
import { createAuthentication } from "../assistant_gateway/onboarding_web/assets/chat_authentication.mjs";
import {
  answer, byClass, byTag, descendants, errorResponse, fakeBrowser, fakeDocument, jsonResponse,
  openRequests, settle,
} from "./settings_fake_dom.mjs";

const OWN = "alice-3f9a1b2c4d5e";
const CSRF = "csrf-token-1";
const PEOPLE = { status: "ok", people: [{ person_id: "usr_dana", name: "Dana" }, { person_id: "usr_bob", name: "Bob" }] };
const NOTES = { path: `users/${OWN}/notes.md`, name: "notes.md", kind: "file", size_bytes: 12 };
const REPORTS = { path: `users/${OWN}/reports`, name: "reports", kind: "directory" };

// Icon buttons carry their word as the tooltip (title), not as text.
const buttonNamed = (node, label) => byTag(node, "button").find(button => button.textContent === label || button.title === label);
const submitForm = form => form.dispatchEvent(new Event("submit", { cancelable: true }));
const key = (name, extra = {}) => Object.assign(new Event("keydown", { cancelable: true }), { key: name, ...extra });

function choose(inputs, value) {
  inputs.forEach(input => { input.checked = input.value === value; });
  const chosen = inputs.find(input => input.value === value);
  chosen.dispatchEvent(new Event("change"));
}

function actionsFixture() {
  const harness = fakeBrowser();
  const warnings = [];
  harness.browser.console = { warn: (...args) => warnings.push(args) };
  const unauthorized = [];
  const actions = createWorkspaceFileActions({
    browser: harness.browser, getCsrfToken: () => CSRF, onUnauthorized: response => unauthorized.push(response.status),
  });
  const row = harness.document.createElement("button");
  harness.document.body.append(row);
  return { ...harness, actions, row, warnings, unauthorized };
}

const openDialog = document => byClass(document.body, "workspace-file-overlay")[0] || null;

test("the page title follows what the gateway lists at the top", () => {
  assert.equal(workspaceViewTitle([{ name: "users" }]), MEMBER_VIEW_TITLE);
  assert.equal(workspaceViewTitle([{ name: "team" }, { name: "users" }]), MEMBER_VIEW_TITLE);
  assert.equal(workspaceViewTitle([]), MEMBER_VIEW_TITLE);
  assert.equal(workspaceViewTitle([{ name: "agents" }, { name: "team" }, { name: "users" }]), ADMIN_VIEW_TITLE);
});

test("every readable file gets Download, only own items get Share, and only own files get Send", () => {
  const own = new Set([OWN]);
  assert.deepEqual(rowActions(NOTES, own), { download: true, share: true, send: true });
  assert.deepEqual(rowActions(REPORTS, own), { download: false, share: true, send: false });
  assert.deepEqual(rowActions({ path: `users/${OWN}`, kind: "directory" }, own),
    { download: false, share: false, send: false },
    "the folder itself has no actions");
  assert.deepEqual(rowActions({ path: "users/someone-else/a.md", kind: "file" }, own),
    { download: true, share: false, send: false });
  assert.deepEqual(rowActions({ path: "team/a.md", kind: "file" }, own),
    { download: true, share: false, send: false });
  assert.deepEqual(rowActions({ path: "team", kind: "directory" }, own),
    { download: false, share: false, send: false });
});

test("request bodies carry the path, the person or folder, the level, how long and copy or move", () => {
  assert.deepEqual(shareRequestBody({ path: NOTES.path, person: "usr_dana", level: "write", duration: "seven_days" }),
    { path: NOTES.path, person: "usr_dana", level: "write", hours: 168 });
  assert.deepEqual(shareRequestBody({ path: NOTES.path, person: "usr_dana", level: "admin", duration: "forever" }),
    { path: NOTES.path, person: "usr_dana", level: "read", hours: null }, "unknown choices fall back to the safest one");
  assert.deepEqual(sendRequestBody({ path: NOTES.path, destination: "person", person: "usr_bob", mode: "copy" }),
    { path: NOTES.path, to: "usr_bob", move: false });
  assert.deepEqual(sendRequestBody({ path: NOTES.path, destination: "team", teamFolder: "reports/", mode: "move" }),
    { path: NOTES.path, to: "team/reports", move: true });
  assert.equal(teamFolderTarget(""), "team");
  assert.equal(teamFolderTarget("/team/"), "team");
  assert.equal(teamFolderTarget("team/q1"), "team/q1");
});

test("Share opens a titled modal dialog, fills the person picker and sends the chosen share", async () => {
  const f = actionsFixture();
  const done = [];
  const view = f.actions.openShare({ entry: NOTES, returnFocus: f.row, onDone: result => done.push(result) });
  const overlay = openDialog(f.document);
  assert.ok(overlay, "the dialog is on the page");
  assert.ok(overlay.classList.contains("is-open"));
  assert.equal(view.dialog.getAttribute("role"), "dialog");
  assert.equal(view.dialog.getAttribute("aria-modal"), "true");
  const title = byTag(view.dialog, "h2")[0];
  assert.equal(title.textContent, "notes.md");
  assert.equal(view.dialog.getAttribute("aria-labelledby"), title.id);
  assert.equal(view.submit.disabled, true, "Share waits for the people list");
  assert.deepEqual(byTag(view.dialog, "legend").map(node => node.textContent), ["Level", "How long"]);
  assert.deepEqual(byClass(view.dialog, "segment-option").map(node => node.textContent),
    ["Read", "Read and change", "Until I stop sharing", "1 day", "7 days"]);

  const people = await answer(f.requests, "/api/web-chat/files/people", jsonResponse(PEOPLE));
  assert.equal(people.method, "GET");
  assert.deepEqual(byTag(view.fields.person, "option").map(option => option.textContent), ["Choose a person", "Dana", "Bob"]);
  assert.equal(view.submit.disabled, false);
  assert.equal(f.document.activeElement, view.fields.person, "focus moves to the picker once it can be used");

  submitForm(view.form);
  assert.equal(view.message.textContent, "Choose who to share with.");
  assert.equal(openRequests(f.requests).length, 0);

  view.fields.person.value = "usr_dana";
  choose(view.fields.levels, "write");
  choose(view.fields.durations, "one_day");
  submitForm(view.form);
  assert.equal(view.submit.textContent, "Sharing…");
  assert.equal(view.submit.getAttribute("aria-busy"), "true");
  assert.equal(view.cancel.disabled, true);
  const sent = await answer(f.requests, "/api/web-chat/files/share",
    jsonResponse({ status: "ok", message: "Shared notes.md with Dana (read and change)." }));
  assert.equal(sent.method, "POST");
  assert.equal(sent.headers["X-UA-Permission-CSRF"], CSRF);
  assert.deepEqual(sent.body, { path: NOTES.path, person: "usr_dana", level: "write", hours: 24 });
  assert.equal(openDialog(f.document), null, "the dialog closes after sharing");
  assert.equal(f.document.activeElement, f.row, "focus returns to the row");
  assert.deepEqual(done, [{ message: "Shared notes.md with Dana (read and change).", shared: true, share: null }]);
});

test("a refused share keeps the dialog and the choices, and shows the gateway's reason", async () => {
  const f = actionsFixture();
  const view = f.actions.openShare({ entry: NOTES, returnFocus: f.row, onDone: () => assert.fail("not shared") });
  await answer(f.requests, "/api/web-chat/files/people", jsonResponse(PEOPLE));
  view.fields.person.value = "usr_bob";
  submitForm(view.form);
  await answer(f.requests, "/api/web-chat/files/share",
    jsonResponse({ status: "refused", error: "notes.md looks like a credential file and is never shared." }, 409));
  assert.ok(openDialog(f.document));
  assert.ok(view.message.classList.contains("is-error"));
  assert.equal(view.message.textContent, "notes.md looks like a credential file and is never shared.");
  assert.equal(view.fields.person.value, "usr_bob");
  assert.equal(view.submit.textContent, "Share");
  assert.equal(view.submit.disabled, false);
  assert.equal(view.cancel.disabled, false);
});

test("with nobody else in the team, or no team list, Share stays off and says why", async () => {
  const f = actionsFixture();
  const view = f.actions.openShare({ entry: NOTES, returnFocus: f.row });
  await answer(f.requests, "/api/web-chat/files/people", jsonResponse({ status: "ok", people: [] }));
  assert.equal(view.submit.disabled, true);
  assert.equal(view.fields.person.disabled, true);
  assert.equal(view.message.textContent, "Nobody else is in the team yet.");

  const failed = f.actions.openShare({ entry: NOTES, returnFocus: f.row });
  await answer(f.requests, "/api/web-chat/files/people", errorResponse(403, "You are not an active member of this team."));
  assert.equal(failed.submit.disabled, true);
  assert.match(failed.message.textContent, /not an active member/);
  assert.equal(byClass(f.document.body, "workspace-file-overlay").length, 1, "one dialog at a time");
});

test("Escape and Cancel close the dialog and return focus; Tab stays inside", async () => {
  const f = actionsFixture();
  const view = f.actions.openShare({ entry: NOTES, returnFocus: f.row });
  await answer(f.requests, "/api/web-chat/files/people", jsonResponse(PEOPLE));
  const stops = focusStops(view.dialog);
  assert.equal(stops[0], view.fields.person);
  assert.equal(stops.at(-1), view.cancel);
  assert.equal(stops.filter(node => node.type === "radio").length, 2, "each radio group is one stop");

  stops.at(-1).focus();
  const forward = key("Tab");
  f.document.dispatchEvent(forward);
  assert.equal(forward.defaultPrevented, true);
  assert.equal(f.document.activeElement, stops[0], "Tab from the last control wraps to the first");
  f.document.dispatchEvent(key("Tab", { shiftKey: true }));
  assert.equal(f.document.activeElement, stops.at(-1), "Shift+Tab from the first wraps to the last");

  f.document.dispatchEvent(key("Escape"));
  assert.equal(openDialog(f.document), null);
  assert.equal(f.document.activeElement, f.row);

  const again = f.actions.openShare({ entry: NOTES, returnFocus: f.row });
  again.cancel.click();
  assert.equal(openDialog(f.document), null);
  assert.equal(f.document.activeElement, f.row);
  // A late people list for a closed dialog changes nothing.
  await answer(f.requests, "/api/web-chat/files/people", jsonResponse(PEOPLE));
  assert.equal(byTag(again.fields.person, "option").length, 1);
});

test("Escape does not close the dialog while its request is on the way", async () => {
  const f = actionsFixture();
  const view = f.actions.openShare({ entry: NOTES, returnFocus: f.row });
  await answer(f.requests, "/api/web-chat/files/people", jsonResponse(PEOPLE));
  view.fields.person.value = "usr_dana";
  submitForm(view.form);
  f.document.dispatchEvent(key("Escape"));
  assert.ok(openDialog(f.document));
  await answer(f.requests, "/api/web-chat/files/share", jsonResponse({ status: "ok", message: "Shared." }));
  assert.equal(openDialog(f.document), null);
});

test("Send to a person asks them first; Send to the team folder can move the file", async () => {
  const f = actionsFixture();
  const done = [];
  const view = f.actions.openSend({ entry: NOTES, returnFocus: f.row, onDone: result => done.push(result) });
  assert.equal(byTag(view.dialog, "h2")[0].textContent, "notes.md");
  assert.deepEqual(byTag(view.dialog, "legend").map(node => node.textContent), ["Send to", "Copy or move"]);
  assert.equal(view.note.textContent, SEND_NOTES.person);
  assert.match(view.note.textContent, /They, or an administrator, must approve before the file arrives in their received\/ folder/);
  assert.equal(view.teamField.hidden, true);
  assert.equal(view.submit.disabled, true, "sending to a person waits for the people list");
  await answer(f.requests, "/api/web-chat/files/people", jsonResponse(PEOPLE));
  view.fields.person.value = "usr_bob";
  submitForm(view.form);
  const toPerson = await answer(f.requests, "/api/web-chat/files/send", jsonResponse({ status: "ok",
    message: "Asked to put notes.md into Bob's folder. It arrives in received/ once they (or an administrator) approve it (request r-1)." }));
  assert.deepEqual(toPerson.body, { path: NOTES.path, to: "usr_bob", move: false });
  assert.equal(toPerson.headers["X-UA-Permission-CSRF"], CSRF);
  assert.equal(done[0].movedAway, false);
  assert.match(done[0].message, /once they \(or an administrator\) approve it/);

  const team = f.actions.openSend({ entry: NOTES, returnFocus: f.row, onDone: result => done.push(result) });
  choose(team.fields.destinations, "team");
  assert.equal(team.personField.hidden, true);
  assert.equal(team.teamField.hidden, false);
  assert.equal(team.note.textContent, "The file arrives in the team folder at once.");
  assert.equal(team.fields.teamFolder.value, "team");
  assert.equal(team.submit.disabled, false, "the team folder needs no people list");
  team.fields.teamFolder.value = "reports";
  choose(team.fields.modes, "move");
  submitForm(team.form);
  assert.equal(team.submit.textContent, "Sending…");
  const toTeam = await answer(f.requests, "/api/web-chat/files/send",
    jsonResponse({ status: "ok", message: "Moved notes.md to team/reports/notes.md." }));
  assert.deepEqual(toTeam.body, { path: NOTES.path, to: "team/reports", move: true });
  assert.deepEqual(done[1], { message: "Moved notes.md to team/reports/notes.md.", movedAway: true });
});

test("a refused send stays open with the reason", async () => {
  const f = actionsFixture();
  const view = f.actions.openSend({ entry: NOTES, returnFocus: f.row });
  view.chooseDestination("team");
  submitForm(view.form);
  await answer(f.requests, "/api/web-chat/files/send",
    jsonResponse({ status: "refused", error: "You cannot write to team/." }, 409));
  assert.ok(openDialog(f.document));
  assert.equal(view.message.textContent, "You cannot write to team/.");
});

test("Shared chips come from the Access page's files view; a failed read shows none and warns", async () => {
  const f = actionsFixture();
  const shared = f.actions.sharedPaths();
  await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [
    { share_id: "s-1", target: `users/${OWN}/reports` }, { share_id: "s-2", target: NOTES.path },
  ] }));
  assert.deepEqual([...await shared], [`users/${OWN}/reports`, NOTES.path]);

  const failed = f.actions.sharedPaths();
  await answer(f.requests, "/api/web-chat/access-map?view=files", errorResponse(500, "Broken."));
  assert.equal((await failed).size, 0);
  assert.equal(f.warnings.length, 1);
});

test("server text in the dialogs stays text", async () => {
  const document = fakeDocument();
  const view = renderShareDialog(document, { ...NOTES, name: "<img src=x onerror=alert(1)>.md" });
  view.fillPeople([{ person_id: "usr_x", name: "<b>Mallory</b>" }]);
  assert.equal(byTag(view.dialog, "img").length, 0);
  assert.equal(byTag(view.dialog, "b").length, 0);
  assert.equal(byTag(view.dialog, "h2")[0].textContent, "<img src=x onerror=alert(1)>.md");
});

/* -- the file tree (workspace_browser.js) ---------------------------------- */

function treeFixture({ useDefaultDownload = false, onUnauthorized = null } = {}) {
  const harness = actionsFixture();
  const { document, browser } = harness;
  const clickedAnchors = [];
  const downloads = [];
  const unauthorizedResponses = [];
  // The tree sets its indent with style.setProperty; the stand-in keeps it.
  const create = document.createElement;
  document.createElement = tagName => {
    const node = create(tagName);
    node.style = { values: {}, setProperty(name, value) { this.values[name] = value; } };
    if (String(tagName).toLowerCase() === "a") {
      const origClick = node.click.bind(node);
      node.click = () => {
        clickedAnchors.push({
          href: node.href,
          download: node.download,
          downloadAttribute: node.getAttribute("download"),
          rel: node.rel,
          connectedDuringClick: node.isConnected,
        });
        origClick();
      };
    }
    return node;
  };
  document.title = "Workspace Files · Universal Agents";
  const window = { ...browser, addEventListener() {}, innerWidth: 1280, innerHeight: 800 };
  vm.runInNewContext(
    readFileSync(new URL("../assistant_gateway/onboarding_web/assets/workspace_browser.js", import.meta.url), "utf8"),
    { window, document },
  );
  const nodes = {};
  ["tree", "viewer", "viewerTitle", "viewerMeta", "viewerTools", "itemActions", "status", "refreshButton", "title",
    "heading"]
    .forEach(name => {
      nodes[name] = document.createElement(name === "refreshButton" ? "button" : "div");
      document.body.append(nodes[name]);
    });
  const createOptions = {
    ...nodes, titleNodes: [nodes.title, nodes.heading], fileActions: harness.actions,
    markdownRenderer: { render: () => "" },
    onUnauthorized: response => {
      unauthorizedResponses.push(response.status);
      return typeof onUnauthorized === "function" ? onUnauthorized(response) : false;
    },
  };
  if (!useDefaultDownload) {
    createOptions.onDownload = (url, entryObj) => downloads.push({ url, path: entryObj.path, name: entryObj.name });
  }
  const controls = window.UAWorkspaceBrowser.create(createOptions);
  return { ...harness, ...nodes, controls, downloads, clickedAnchors, unauthorizedResponses };
}

const headResponse = (status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  text: async () => "",
});

const entry = (path, kind = "directory", sizeBytes = null) => ({
  path,
  name: path.split("/").at(-1),
  kind,
  size_bytes: kind === "file" ? (sizeBytes ?? 12) : null,
  modified_at_utc: "2026-09-25T10:00:00+00:00",
});
const listing = (path, entries) => jsonResponse({ status: "ok", path, entries, truncated: false, entry_limit: 2000 });
// A tree item is <div treeitem> > [<div line> > <button row>, <div group>?]; it carries no buttons.
const treeItem = (f, name) => descendants(f.tree).find(node => node.classList.contains("workspace-tree-item")
  && byClass(node.children[0], "workspace-tree-name")[0].textContent === name);
const rowOf = item => item.children[0].children[0];
// Download, Share… and Send… for the selected item, at the top right of the preview pane.
const actionLabels = f => (f.itemActions.hidden ? [] : byClass(f.itemActions, "workspace-item-action").map(button => button.textContent));
const fileReply = (path, name, extra = {}) => jsonResponse({
  status: "ok", name, path, preview_kind: "text", size_bytes: 12, content: "hi", ...extra,
});
const ROOT_LISTING = "/api/web-chat/workspace?path=";

async function memberTree(rootEntries = [entry("team"), entry("users")]) {
  const f = treeFixture();
  const loading = f.controls.load();
  await answer(f.requests, ROOT_LISTING, listing("", rootEntries));
  await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok",
    shared_by_you: [{ share_id: "s-1", target: `users/${OWN}/reports/q1.md` }] }));
  await loading;
  return f;
}

async function openOwnFolder(f, entries) {
  rowOf(treeItem(f, "users")).click();
  await answer(f.requests, "/api/web-chat/workspace?path=users", listing("users", [entry(`users/${OWN}`)]));
  rowOf(treeItem(f, OWN)).click();
  await answer(f.requests, `/api/web-chat/workspace?path=users%2F${OWN}`, listing(`users/${OWN}`, entries));
}

/** Selects a file in the person's own folder (it opens in the preview). */
async function selectOwnFile(f, name) {
  rowOf(treeItem(f, name)).click();
  await answer(f.requests, `/api/web-chat/workspace/file?path=users%2F${OWN}%2F${name}`, fileReply(`users/${OWN}/${name}`, name));
}

test("members see 'Your files'; the selected item in their own folder offers Share… and Send… above the preview", async () => {
  const f = await memberTree();
  assert.equal(f.title.textContent, MEMBER_VIEW_TITLE);
  assert.equal(f.heading.textContent, MEMBER_VIEW_TITLE);
  assert.equal(f.document.title, "Your files · Universal Agents");
  assert.equal(f.tree.getAttribute("aria-label"), MEMBER_VIEW_TITLE);
  assert.deepEqual(actionLabels(f), [], "nothing shows before an item is selected");

  await openOwnFolder(f, [entry(`users/${OWN}/reports`), entry(`users/${OWN}/notes.md`, "file")]);
  assert.deepEqual(actionLabels(f), [], "the person's folder itself has no actions");
  assert.equal(f.viewerTitle.textContent, OWN, "a selected folder is named in the preview pane");
  assert.equal(f.viewerMeta.textContent, "Folder");
  assert.equal(byClass(f.tree, "workspace-item-action").length, 0, "the tree carries no buttons");

  await selectOwnFile(f, "notes.md");
  assert.deepEqual(actionLabels(f), [DOWNLOAD_ACTION_LABEL, "Share…", "Send…"]);
  assert.equal(f.itemActions.getAttribute("aria-label"), "Actions for notes.md");
  const download = buttonNamed(f.itemActions, DOWNLOAD_ACTION_LABEL);
  assert.equal(download.getAttribute("aria-label"), "Download notes.md");
  assert.equal(byTag(download, "svg").length, 1, "Download is a labeled button with its icon");
  download.click();
  const preflight = await answer(
    f.requests,
    `/api/web-chat/workspace/download?path=users%2F${OWN}%2Fnotes.md`,
    headResponse(200),
  );
  assert.equal(preflight.method, "HEAD");
  assert.deepEqual(f.downloads, [{
    url: `/api/web-chat/workspace/download?path=users%2F${OWN}%2Fnotes.md`,
    path: `users/${OWN}/notes.md`,
    name: "notes.md",
  }]);
  const share = buttonNamed(f.itemActions, "Share…");
  assert.equal(share.getAttribute("aria-label"), "Share notes.md");
  assert.equal(byTag(share, "svg").length, 1, "a labeled button with its icon");
  assert.ok(rowOf(treeItem(f, "notes.md")).classList.contains("is-selected"));

  rowOf(treeItem(f, "reports")).click();
  await answer(f.requests, `/api/web-chat/workspace?path=users%2F${OWN}%2Freports`,
    listing(`users/${OWN}/reports`, [entry(`users/${OWN}/reports/q1.md`, "file")]));
  assert.deepEqual(actionLabels(f), ["Share…"], "a folder can be shared, not downloaded or sent");
  assert.equal(f.viewerTitle.textContent, "reports");
  assert.equal(rowOf(treeItem(f, "notes.md")).classList.contains("is-selected"), false, "one selected item at a time");
  assert.deepEqual(byClass(treeItem(f, "q1.md"), "workspace-tree-chip").map(node => node.textContent), ["Shared"],
    "items the person already shares carry the Shared chip");
  assert.equal(byClass(treeItem(f, "notes.md"), "workspace-tree-chip").length, 0);
});

test("sharing the selected item marks its row Shared and says so in the page status", async () => {
  const f = await memberTree();
  await openOwnFolder(f, [entry(`users/${OWN}/notes.md`, "file")]);
  await selectOwnFile(f, "notes.md");
  const item = treeItem(f, "notes.md");
  const share = buttonNamed(f.itemActions, "Share…");
  share.click();
  const dialog = byClass(f.document.body, "workspace-file-dialog")[0];
  assert.ok(dialog);
  await answer(f.requests, "/api/web-chat/files/people", jsonResponse(PEOPLE));
  byClass(dialog, "workspace-file-person")[0].value = "usr_dana";
  submitForm(byTag(dialog, "form")[0]);
  const sent = await answer(f.requests, "/api/web-chat/files/share",
    jsonResponse({ status: "ok", message: "Shared notes.md with Dana (read)." }));
  assert.deepEqual(sent.body, { path: `users/${OWN}/notes.md`, person: "usr_dana", level: "read", hours: null });
  assert.equal(f.document.activeElement, share, "focus returns to the Share button");
  assert.deepEqual(byClass(item, "workspace-tree-chip").map(node => node.textContent), ["Shared"]);
  assert.equal(f.status.hidden, false);
  assert.equal(f.status.textContent, "Shared notes.md with Dana (read).");
});

test("moving the selected file to the team folder removes its row and clears the preview; sending to a person keeps it", async () => {
  const f = await memberTree();
  await openOwnFolder(f, [entry(`users/${OWN}/a.md`, "file"), entry(`users/${OWN}/b.md`, "file")]);

  await selectOwnFile(f, "a.md");
  buttonNamed(f.itemActions, "Send…").click();
  let dialog = byClass(f.document.body, "workspace-file-dialog")[0];
  await answer(f.requests, "/api/web-chat/files/people", jsonResponse(PEOPLE));
  byClass(dialog, "workspace-file-person")[0].value = "usr_bob";
  submitForm(byTag(dialog, "form")[0]);
  await answer(f.requests, "/api/web-chat/files/send", jsonResponse({ status: "ok",
    message: "Asked to put a.md into Bob's folder. It arrives in received/ once they (or an administrator) approve it (request r-2)." }));
  assert.match(f.status.textContent, /once they \(or an administrator\) approve it/);
  assert.ok(treeItem(f, "a.md"), "a file sent to a person stays until it is approved");

  await selectOwnFile(f, "b.md");
  buttonNamed(f.itemActions, "Send…").click();
  dialog = byClass(f.document.body, "workspace-file-dialog")[0];
  const radios = byTag(dialog, "input").filter(node => node.type === "radio");
  choose(radios.slice(0, 2), "team");
  choose(radios.slice(2), "move");
  submitForm(byTag(dialog, "form")[0]);
  const moved = await answer(f.requests, "/api/web-chat/files/send",
    jsonResponse({ status: "ok", message: "Moved b.md to team/b.md." }));
  assert.deepEqual(moved.body, { path: `users/${OWN}/b.md`, to: "team", move: true });
  assert.equal(treeItem(f, "b.md"), undefined, "the moved file left the folder");
  assert.equal(f.status.textContent, "Moved b.md to team/b.md.");
  assert.equal(f.viewerTitle.textContent, "Select a file", "the preview of the moved file is cleared");
  assert.deepEqual(actionLabels(f), []);
});

test("administrators see 'Workspace' and Download on files outside their own folder, no Share/Send", async () => {
  const f = treeFixture();
  const loading = f.controls.load();
  await answer(f.requests, ROOT_LISTING, listing("", [entry("agents"), entry("team"), entry("users")]));
  await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
  await loading;
  assert.equal(f.title.textContent, ADMIN_VIEW_TITLE);
  assert.equal(f.heading.textContent, ADMIN_VIEW_TITLE);
  assert.equal(f.document.title, "Workspace · Universal Agents");
  rowOf(treeItem(f, "team")).click();
  await answer(f.requests, "/api/web-chat/workspace?path=team", listing("team", [entry("team/plan.md", "file")]));
  assert.deepEqual(actionLabels(f), [], "the team folder has no actions");
  rowOf(treeItem(f, "plan.md")).click();
  await answer(f.requests, "/api/web-chat/workspace/file?path=team%2Fplan.md", fileReply("team/plan.md", "plan.md"));
  assert.deepEqual(actionLabels(f), [DOWNLOAD_ACTION_LABEL]);
  await openOwnFolder(f, [entry(`users/${OWN}/notes.md`, "file")]);
  await selectOwnFile(f, "notes.md");
  assert.deepEqual(actionLabels(f), [DOWNLOAD_ACTION_LABEL, "Share…", "Send…"], "their own folder works as for members");
});

test("a member with no files is told so in plain words", async () => {
  const f = treeFixture();
  const loading = f.controls.load();
  await answer(f.requests, ROOT_LISTING, listing("", []));
  await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
  await loading;
  assert.equal(f.title.textContent, MEMBER_VIEW_TITLE);
  assert.equal(f.tree.textContent, "You have no files yet.");
});

test("signing out resets the title and closes an open dialog", async () => {
  const f = await memberTree();
  await openOwnFolder(f, [entry(`users/${OWN}/notes.md`, "file")]);
  await selectOwnFile(f, "notes.md");
  buttonNamed(f.itemActions, "Share…").click();
  assert.equal(f.actions.isOpen(), true);
  f.document.title = "(2) Chat · Universal Agents";
  f.controls.clear();
  assert.equal(f.actions.isOpen(), false);
  assert.equal(byClass(f.document.body, "workspace-file-overlay").length, 0);
  assert.equal(f.title.textContent, "Workspace Files");
  assert.equal(f.document.title, "(2) Chat · Universal Agents", "sign-out on the chat page keeps the chat's tab title");
  assert.equal(f.tree.children.length, 0);
  assert.deepEqual(f.actions.actionsFor(NOTES), { download: true, share: false, send: false },
    "the person's own folder is forgotten");
  await settle();
});

test("Download preflights with HEAD, omits the download attribute, handles 401/403/404, and works for >5 MB and unsupported files", async () => {
  const f = treeFixture({ useDefaultDownload: true });
  const loading = f.controls.load();
  await answer(f.requests, ROOT_LISTING, listing("", [entry("team"), entry("users")]));
  await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
  await loading;
  assert.equal(f.title.textContent, MEMBER_VIEW_TITLE, "members seeing team/ and users/ still see 'Your files'");

  rowOf(treeItem(f, "team")).click();
  await answer(
    f.requests,
    "/api/web-chat/workspace?path=team",
    listing("team", [
      entry("team/recording.mp3", "file", 12 * 1024 * 1024),
      entry("team/archive.zip", "file", 48 * 1024),
      entry("team/unreadable.zip", "file", 9 * 1024 * 1024),
    ]),
  );
  assert.deepEqual(actionLabels(f), [], "folders get no Download button");

  rowOf(treeItem(f, "recording.mp3")).click();
  await answer(
    f.requests,
    "/api/web-chat/workspace/file?path=team%2Frecording.mp3",
    fileReply("team/recording.mp3", "recording.mp3", {
      preview_kind: "too_large",
      size_bytes: 12 * 1024 * 1024,
      message: "This file is larger than the 5 MB preview limit.",
    }),
  );
  assert.deepEqual(actionLabels(f), [DOWNLOAD_ACTION_LABEL]);
  const downloadBtn = buttonNamed(f.itemActions, DOWNLOAD_ACTION_LABEL);
  assert.equal(downloadBtn.getAttribute("aria-label"), "Download recording.mp3");

  // 200 OK on HEAD triggers in-place download without the download attribute.
  downloadBtn.click();
  const headOk = await answer(
    f.requests,
    "/api/web-chat/workspace/download?path=team%2Frecording.mp3",
    headResponse(200),
  );
  assert.equal(headOk.method, "HEAD");
  assert.equal(headOk.init.cache, "no-store");
  assert.equal(headOk.init.credentials, "same-origin");
  assert.deepEqual(f.clickedAnchors, [{
    href: "/api/web-chat/workspace/download?path=team%2Frecording.mp3",
    download: undefined,
    downloadAttribute: null,
    rel: "noopener",
    connectedDuringClick: true,
  }]);
  assert.equal(byTag(f.document.body, "a").length, 0, "temporary download anchor is removed after click");

  // 404 and 403 on HEAD show plain-language errors in status and do not trigger a download.
  downloadBtn.click();
  await answer(f.requests, "/api/web-chat/workspace/download?path=team%2Frecording.mp3", headResponse(404));
  assert.equal(f.clickedAnchors.length, 1);
  assert.ok(f.status.classList.contains("is-error"));
  assert.match(f.status.textContent, /could not be found/i);

  downloadBtn.click();
  await answer(f.requests, "/api/web-chat/workspace/download?path=team%2Frecording.mp3", headResponse(403));
  assert.equal(f.clickedAnchors.length, 1);
  assert.ok(f.status.classList.contains("is-error"));
  assert.match(f.status.textContent, /permission/i);

  // Small unsupported binary file also offers Download.
  rowOf(treeItem(f, "archive.zip")).click();
  await answer(
    f.requests,
    "/api/web-chat/workspace/file?path=team%2Farchive.zip",
    fileReply("team/archive.zip", "archive.zip", {
      preview_kind: "unsupported",
      size_bytes: 48 * 1024,
      message: "Preview is not available for .zip files.",
    }),
  );
  assert.deepEqual(actionLabels(f), [DOWNLOAD_ACTION_LABEL]);

  // 401 on HEAD notifies onUnauthorized and aborts without downloading HTML.
  buttonNamed(f.itemActions, DOWNLOAD_ACTION_LABEL).click();
  await answer(f.requests, "/api/web-chat/workspace/download?path=team%2Farchive.zip", headResponse(401));
  assert.deepEqual(f.unauthorizedResponses, [401]);
  assert.equal(f.clickedAnchors.length, 1, "expired session never clicks the download anchor");
  assert.match(f.status.textContent, /session ended/i);

  rowOf(treeItem(f, "unreadable.zip")).click();
  await answer(
    f.requests,
    "/api/web-chat/workspace/file?path=team%2Funreadable.zip",
    errorResponse(403, "This workspace path is not available."),
  );
  assert.deepEqual(actionLabels(f), [], "item actions are hidden if fetchFile fails");
});

test("deep-link expands parent folders, selects the file, triggers auto-download once, and refresh clears it", async () => {
  const f = treeFixture();
  let consumedCount = 0;
  let currentUrl = "/webchat?view=files&path=team%2Fguides%2Fhandbook.pdf&download=1";
  const loading = f.controls.load({
    initialPath: "team/guides/handbook.pdf",
    autoDownload: true,
    onDownloadConsumed: () => {
      consumedCount += 1;
      const [pathname, searchPart = ""] = currentUrl.split("?");
      const cleaned = stripWorkspaceDownloadParam({ pathname, search: searchPart ? `?${searchPart}` : "", hash: "" });
      if (cleaned) currentUrl = cleaned;
    },
  });
  await answer(f.requests, ROOT_LISTING, listing("", [entry("team"), entry("users")]));
  await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
  await answer(f.requests, "/api/web-chat/workspace?path=team", listing("team", [entry("team/guides")]));
  await answer(
    f.requests,
    "/api/web-chat/workspace?path=team%2Fguides",
    listing("team/guides", [entry("team/guides/handbook.pdf", "file", 8 * 1024 * 1024)]),
  );
  await answer(
    f.requests,
    "/api/web-chat/workspace/file?path=team%2Fguides%2Fhandbook.pdf",
    fileReply("team/guides/handbook.pdf", "handbook.pdf", {
      preview_kind: "too_large",
      size_bytes: 8 * 1024 * 1024,
    }),
  );
  const headAuto = await answer(
    f.requests,
    "/api/web-chat/workspace/download?path=team%2Fguides%2Fhandbook.pdf",
    headResponse(200),
  );
  assert.equal(headAuto.method, "HEAD");
  await loading;

  assert.equal(f.viewerTitle.textContent, "handbook.pdf");
  assert.ok(rowOf(treeItem(f, "handbook.pdf")).classList.contains("is-selected"));
  assert.deepEqual(actionLabels(f), [DOWNLOAD_ACTION_LABEL]);
  assert.equal(consumedCount, 1);
  assert.equal(currentUrl, "/webchat?view=files&path=team%2Fguides%2Fhandbook.pdf");
  assert.deepEqual(f.downloads, [{
    url: "/api/web-chat/workspace/download?path=team%2Fguides%2Fhandbook.pdf",
    path: "team/guides/handbook.pdf",
    name: "handbook.pdf",
  }]);

  // Clicking Refresh calls load() cleanly without re-running deep-link or download.
  f.refreshButton.click();
  await answer(f.requests, ROOT_LISTING, listing("", [entry("team"), entry("users")]));
  await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
  assert.equal(f.downloads.length, 1);
  assert.equal(f.viewerTitle.textContent, "Select a file");
});

test("deep-link keeps late-arriving Shared chips and aborts auto-download when the user selects another row", async () => {
  // 1. Late-arriving Shared chips after deep-link expansion and file selection still render.
  const f1 = treeFixture();
  const deepLinkLoad = f1.controls.load({
    initialPath: `users/${OWN}/reports/q1.md`,
    autoDownload: false,
  });
  await answer(f1.requests, ROOT_LISTING, listing("", [entry("team"), entry("users")]));
  // Deliberately leave /api/web-chat/access-map?view=files pending while deep-link expands and selects.
  await answer(f1.requests, "/api/web-chat/workspace?path=users", listing("users", [entry(`users/${OWN}`)]));
  await answer(
    f1.requests,
    `/api/web-chat/workspace?path=users%2F${OWN}`,
    listing(`users/${OWN}`, [entry(`users/${OWN}/reports`)]),
  );
  await answer(
    f1.requests,
    `/api/web-chat/workspace?path=users%2F${OWN}%2Freports`,
    listing(`users/${OWN}/reports`, [entry(`users/${OWN}/reports/q1.md`, "file")]),
  );
  await answer(
    f1.requests,
    `/api/web-chat/workspace/file?path=users%2F${OWN}%2Freports%2Fq1.md`,
    fileReply(`users/${OWN}/reports/q1.md`, "q1.md"),
  );
  assert.equal(f1.viewerTitle.textContent, "q1.md");
  await answer(
    f1.requests,
    "/api/web-chat/access-map?view=files",
    jsonResponse({ status: "ok", shared_by_you: [{ share_id: "s-1", target: `users/${OWN}/reports/q1.md` }] }),
  );
  await deepLinkLoad;
  assert.deepEqual(
    byClass(treeItem(f1, "q1.md"), "workspace-tree-chip").map(node => node.textContent),
    ["Shared"],
    "selecting a deep-linked file does not drop late-arriving Shared chips",
  );

  // 2. Clicking another row while openDeepLink is expanding aborts auto-download and still consumes download=1.
  const f2 = treeFixture();
  let consumed = 0;
  const interruptedLoad = f2.controls.load({
    initialPath: "team/guides/handbook.pdf",
    autoDownload: true,
    onDownloadConsumed: () => { consumed += 1; },
  });
  await answer(f2.requests, ROOT_LISTING, listing("", [entry("team"), entry("users")]));
  await answer(f2.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
  // While /api/web-chat/workspace?path=team is still in flight, the user clicks users/.
  rowOf(treeItem(f2, "users")).click();
  await answer(f2.requests, "/api/web-chat/workspace?path=users", listing("users", [entry(`users/${OWN}`)]));
  // Now the team/ expansion finishes; deep-link should notice selection changed, consume download=1, and stop.
  await answer(f2.requests, "/api/web-chat/workspace?path=team", listing("team", [entry("team/guides")]));
  await interruptedLoad;
  assert.equal(consumed, 1, "onDownloadConsumed still runs so download=1 is stripped");
  assert.equal(f2.downloads.length, 0, "auto-download is aborted after user clicks another row");
  assert.equal(f2.viewerTitle.textContent, "users", "the user's selected row stays active");
  assert.equal(openRequests(f2.requests).length, 0, "no further deep-link requests are sent");
});

test("invalid, missing, and folder deep-links show status and strip download without downloading", async () => {
  // Invalid traversal path
  const f1 = treeFixture();
  let consumed = 0;
  const loadInvalid = f1.controls.load({
    initialPath: "../secret.txt",
    autoDownload: true,
    onDownloadConsumed: () => { consumed += 1; },
  });
  await answer(f1.requests, ROOT_LISTING, listing("", [entry("team"), entry("users")]));
  await answer(f1.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
  await loadInvalid;
  assert.equal(consumed, 1);
  assert.equal(f1.downloads.length, 0);
  assert.match(f1.status.textContent, /not valid/i);
  assert.ok(f1.status.classList.contains("is-error"));

  // Missing target file falls back to direct fetchFile and shows not found after 404.
  const f2 = treeFixture();
  consumed = 0;
  const loadMissing = f2.controls.load({
    initialPath: "team/missing.mp3",
    autoDownload: true,
    onDownloadConsumed: () => { consumed += 1; },
  });
  await answer(f2.requests, ROOT_LISTING, listing("", [entry("team")]));
  await answer(f2.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
  await answer(f2.requests, "/api/web-chat/workspace?path=team", listing("team", []));
  await answer(
    f2.requests,
    "/api/web-chat/workspace/file?path=team%2Fmissing.mp3",
    errorResponse(404, "Workspace path was not found."),
  );
  await loadMissing;
  assert.equal(consumed, 1);
  assert.equal(f2.downloads.length, 0);
  assert.match(f2.status.textContent, /could not be found/i);

  // Folder deep-link with autoDownload=true selects folder, strips download, does not download
  const f3 = treeFixture();
  consumed = 0;
  const loadFolder = f3.controls.load({
    initialPath: "team/guides",
    autoDownload: true,
    onDownloadConsumed: () => { consumed += 1; },
  });
  await answer(f3.requests, ROOT_LISTING, listing("", [entry("team")]));
  await answer(f3.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
  await answer(f3.requests, "/api/web-chat/workspace?path=team", listing("team", [entry("team/guides")]));
  await loadFolder;
  assert.equal(consumed, 1);
  assert.equal(f3.downloads.length, 0);
  assert.equal(f3.viewerTitle.textContent, "guides");
  assert.deepEqual(actionLabels(f3), []);
  assert.match(f3.status.textContent, /Folders cannot be downloaded/i);
});

test("unlockWorkspace passes deep-link options once and strips download=1 with history.replaceState", () => {
  const prevWindow = globalThis.window;
  const prevDocument = globalThis.document;
  const replaced = [];
  const loadCalls = [];
  globalThis.window = {
    location: {
      pathname: "/webchat",
      search: "?view=files&path=team%2Fclip.mp4&download=1",
      hash: "",
    },
    history: {
      replaceState(_state, _title, url) {
        replaced.push(url);
        const [pathname, searchPart = ""] = url.split("?");
        globalThis.window.location = { pathname, search: searchPart ? `?${searchPart}` : "", hash: "" };
      },
    },
  };
  globalThis.document = { title: "Workspace Files · Universal Agents" };
  try {
    const authenticationState = {
      authPanel: { hidden: false },
      chatApp: { hidden: false },
      objectivesApp: { hidden: false },
      statusApp: { hidden: false },
      knowledgeApp: { hidden: false },
      workspaceApp: { hidden: true },
      pendingWorkspacePath: "team/clip.mp4",
      pendingWorkspaceDownload: true,
      workspaceControls: {
        load(opts) {
          loadCalls.push(opts);
        },
      },
    };
    const auth = createAuthentication({
      authenticationState,
      sharedState: {},
      operations: {},
      lifecycle: {},
      requestClient: {},
    });
    auth.unlockWorkspace();
    assert.equal(authenticationState.workspaceApp.hidden, false);
    assert.equal(loadCalls.length, 1);
    assert.equal(loadCalls[0].initialPath, "team/clip.mp4");
    assert.equal(loadCalls[0].autoDownload, true);
    assert.equal(replaced.length, 0, "download=1 stays until onDownloadConsumed fires");

    loadCalls[0].onDownloadConsumed();
    assert.deepEqual(replaced, ["/webchat?view=files&path=team%2Fclip.mp4"]);

    // Subsequent unlock in the same page session does not re-trigger download.
    auth.unlockWorkspace();
    assert.equal(loadCalls.length, 2);
    assert.equal(loadCalls[1].initialPath, "");
    assert.equal(loadCalls[1].autoDownload, false);
  } finally {
    globalThis.window = prevWindow;
    globalThis.document = prevDocument;
  }
});

test("F5: mid-load 401 preserves pending deep-link state and download=1 across re-sign-in; explicit logout clears it", async () => {
  const prevWindow = globalThis.window;
  const prevDocument = globalThis.document;
  const replaced = [];
  globalThis.window = {
    location: {
      pathname: "/webchat",
      search: "?view=files&path=team%2Fguides%2Fclip.mp4&download=1",
      hash: "",
    },
    history: {
      replaceState(_state, _title, url) {
        replaced.push(url);
        const [pathname, searchPart = ""] = url.split("?");
        globalThis.window.location = { pathname, search: searchPart ? `?${searchPart}` : "", hash: "" };
      },
    },
  };
  globalThis.document = {
    title: "Workspace Files · Universal Agents",
    getElementById: () => ({ replaceChildren() {} }),
  };
  try {
    // 1. Test 401 during ancestor expansion, during fetchFile, and during HEAD preflight.
    for (const failStage of ["expand", "file", "head"]) {
      replaced.length = 0;
      globalThis.window.location = {
        pathname: "/webchat",
        search: "?view=files&path=team%2Fguides%2Fclip.mp4&download=1",
        hash: "",
      };
      let controlsRef = null;
      const f = treeFixture({
        onUnauthorized: () => {
          controlsRef.clear();
          return true;
        },
      });
      controlsRef = f.controls;
      const authenticationState = {
        authPanel: { hidden: false },
        authError: { textContent: "", hidden: true },
        chatApp: { hidden: false },
        objectivesApp: { hidden: false },
        statusApp: { hidden: false },
        knowledgeApp: { hidden: false },
        workspaceApp: { hidden: true },
        enrollmentOverlay: { classList: { remove() {} }, setAttribute() {}, hidden: true },
        enrollmentToken: { value: "", type: "password" },
        enrollmentReveal: { setAttribute() {}, disabled: false },
        enrollmentCopyButton: { disabled: false, classList: { remove() {} } },
        enrollmentCompleteButton: { disabled: false },
        logoutButton: { disabled: false },
        objectivesLogout: { disabled: false },
        workspaceLogout: { disabled: false },
        statusLogout: { disabled: false },
        knowledgeLogout: { disabled: false },
        enrollmentCancelButton: { disabled: false },
        settingsWorkspaceLink: { hidden: false },
        settingsStatusLink: { hidden: false },
        settingsKnowledgeLink: { hidden: false },
        knowledgeClient: { setCsrfToken() {} },
        knowledgeControls: { clear() {} },
        pendingWorkspacePath: "team/guides/clip.mp4",
        pendingWorkspaceDownload: true,
        workspaceControls: f.controls,
      };
      const auth = createAuthentication({
        authenticationState,
        sharedState: {
          runtimeControlSnapshots: new Map(),
          settingsControls: { clear() {} },
          accountNameLabel: { textContent: "" },
          threadList: { innerHTML: "" },
          messageList: { innerHTML: "" },
          messageInput: { value: "" },
          objectivesContent: { replaceChildren() {} },
          objectivesUpdatedAt: { textContent: "" },
        },
        operations: {
          stopLiveEvents() {},
          clearOptimisticDeliveries() {},
          clearPendingAttachments() {},
          setObjectivesStatus() {},
          showSignIn() {},
        },
        lifecycle: { clearInterval() {} },
        requestClient: { fetch: () => Promise.resolve({ ok: true }) },
      });

      auth.unlockWorkspace();
      await answer(f.requests, ROOT_LISTING, listing("", [entry("team")]));
      await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
      if (failStage === "expand") {
        await answer(f.requests, "/api/web-chat/workspace?path=team", errorResponse(401, "Session expired."));
      } else {
        await answer(f.requests, "/api/web-chat/workspace?path=team", listing("team", [entry("team/guides")]));
        await answer(
          f.requests,
          "/api/web-chat/workspace?path=team%2Fguides",
          listing("team/guides", [entry("team/guides/clip.mp4", "file", 8 * 1024 * 1024)]),
        );
        if (failStage === "file") {
          await answer(
            f.requests,
            "/api/web-chat/workspace/file?path=team%2Fguides%2Fclip.mp4",
            errorResponse(401, "Session expired."),
          );
        } else {
          await answer(
            f.requests,
            "/api/web-chat/workspace/file?path=team%2Fguides%2Fclip.mp4",
            fileReply("team/guides/clip.mp4", "clip.mp4", { preview_kind: "too_large" }),
          );
          await answer(
            f.requests,
            "/api/web-chat/workspace/download?path=team%2Fguides%2Fclip.mp4",
            headResponse(401),
          );
        }
      }
      await settle();

      // Pending state and download=1 URL parameter must survive the mid-load 401.
      assert.equal(authenticationState.pendingWorkspacePath, "team/guides/clip.mp4", failStage);
      assert.equal(authenticationState.pendingWorkspaceDownload, true, failStage);
      assert.deepEqual(replaced, [], `download=1 must not be stripped on 401 (${failStage})`);
      assert.equal(f.downloads.length, 0, failStage);

      // Re-signing in completes the preserved deep-link and strips download=1 only after HEAD 200.
      auth.unlockWorkspace();
      await answer(f.requests, ROOT_LISTING, listing("", [entry("team")]));
      await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
      await answer(f.requests, "/api/web-chat/workspace?path=team", listing("team", [entry("team/guides")]));
      await answer(
        f.requests,
        "/api/web-chat/workspace?path=team%2Fguides",
        listing("team/guides", [entry("team/guides/clip.mp4", "file", 8 * 1024 * 1024)]),
      );
      await answer(
        f.requests,
        "/api/web-chat/workspace/file?path=team%2Fguides%2Fclip.mp4",
        fileReply("team/guides/clip.mp4", "clip.mp4", { preview_kind: "too_large" }),
      );
      await answer(
        f.requests,
        "/api/web-chat/workspace/download?path=team%2Fguides%2Fclip.mp4",
        headResponse(200),
      );
      await settle();
      assert.equal(f.downloads.length, 1, failStage);
      assert.equal(authenticationState.pendingWorkspacePath, "", failStage);
      assert.equal(authenticationState.pendingWorkspaceDownload, false, failStage);
      assert.deepEqual(replaced, ["/webchat?view=files&path=team%2Fguides%2Fclip.mp4"], failStage);

      // Explicit logout clears pending deep-link state.
      authenticationState.pendingWorkspacePath = "team/other.pdf";
      authenticationState.pendingWorkspaceDownload = true;
      auth.logoutChat();
      await settle();
      assert.equal(authenticationState.pendingWorkspacePath, "");
      assert.equal(authenticationState.pendingWorkspaceDownload, false);
    }
  } finally {
    globalThis.window = prevWindow;
    globalThis.document = prevDocument;
  }
});

test("F6: deep-link falls back to direct fetchFile when an ancestor folder exceeds the 2,000-entry cap", async () => {
  const f = treeFixture();
  let consumed = 0;
  const loadTruncated = f.controls.load({
    initialPath: "team/z_beyond_cap/report.pdf",
    autoDownload: true,
    onDownloadConsumed: () => { consumed += 1; },
  });
  await answer(f.requests, ROOT_LISTING, listing("", [entry("team")]));
  await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
  // team/ listing is truncated and omits z_beyond_cap.
  await answer(
    f.requests,
    "/api/web-chat/workspace?path=team",
    jsonResponse({
      status: "ok",
      path: "team",
      entries: [entry("team/a_first.txt", "file")],
      truncated: true,
      entry_limit: 2000,
    }),
  );
  // Falls back directly to /api/web-chat/workspace/file for the target path.
  await answer(
    f.requests,
    "/api/web-chat/workspace/file?path=team%2Fz_beyond_cap%2Freport.pdf",
    fileReply("team/z_beyond_cap/report.pdf", "report.pdf", {
      preview_kind: "pdf",
      size_bytes: 2048,
      content_type: "application/pdf",
    }),
  );
  const headResp = await answer(
    f.requests,
    "/api/web-chat/workspace/download?path=team%2Fz_beyond_cap%2Freport.pdf",
    headResponse(200),
  );
  assert.equal(headResp.method, "HEAD");
  await loadTruncated;

  assert.equal(f.viewerTitle.textContent, "report.pdf");
  assert.deepEqual(actionLabels(f), [DOWNLOAD_ACTION_LABEL]);
  assert.equal(consumed, 1);
  assert.deepEqual(f.downloads, [{
    url: "/api/web-chat/workspace/download?path=team%2Fz_beyond_cap%2Freport.pdf",
    path: "team/z_beyond_cap/report.pdf",
    name: "report.pdf",
  }]);
});

test("F9: Download button disables while HEAD preflight is in flight and guards status on selectedPath", async () => {
  const f = await memberTree();
  await openOwnFolder(f, [
    entry(`users/${OWN}/first.bin`, "file"),
    entry(`users/${OWN}/second.bin`, "file"),
  ]);
  await selectOwnFile(f, "first.bin");
  const downloadBtn = buttonNamed(f.itemActions, DOWNLOAD_ACTION_LABEL);

  // Double-clicking while HEAD is in flight sends only one HEAD request.
  downloadBtn.click();
  assert.equal(downloadBtn.disabled, true);
  assert.equal(downloadBtn.getAttribute("aria-busy"), "true");
  downloadBtn.click();
  assert.equal(
    openRequests(f.requests).filter(req => req.path.includes("/api/web-chat/workspace/download")).length,
    1,
  );
  await answer(
    f.requests,
    `/api/web-chat/workspace/download?path=users%2F${OWN}%2Ffirst.bin`,
    headResponse(200),
  );
  assert.equal(downloadBtn.disabled, false);
  assert.equal(downloadBtn.getAttribute("aria-busy"), null);
  assert.equal(f.downloads.length, 1);

  // Switching to second.bin while first.bin's failing HEAD is in flight must not clobber status.
  downloadBtn.click();
  await selectOwnFile(f, "second.bin");
  f.status.textContent = "";
  f.status.hidden = true;
  await answer(
    f.requests,
    `/api/web-chat/workspace/download?path=users%2F${OWN}%2Ffirst.bin`,
    headResponse(404),
  );
  assert.equal(f.status.textContent, "");
  assert.equal(f.status.hidden, true);
});

test("F13: collapsing a folder while its initial listing is in flight hides the resolved children group", async () => {
  const f = await memberTree();
  const teamItem = treeItem(f, "team");
  const teamRow = rowOf(teamItem);

  // Click once to expand (starts fetchDirectory), then click again before it resolves to collapse.
  teamRow.click();
  assert.equal(teamRow.getAttribute("aria-expanded"), "true");
  teamRow.click();
  assert.equal(teamRow.getAttribute("aria-expanded"), "false");

  await answer(
    f.requests,
    "/api/web-chat/workspace?path=team",
    listing("team", [entry("team/guide.md", "file")]),
  );
  const group = byClass(teamItem, "workspace-tree-group")[0];
  assert.ok(group);
  assert.equal(teamRow.getAttribute("aria-expanded"), "false");
  assert.equal(group.hidden, true, "children stay hidden when collapsed mid-load");

  // Re-expanding shows the already-loaded group without re-fetching.
  teamRow.click();
  assert.equal(teamRow.getAttribute("aria-expanded"), "true");
  assert.equal(group.hidden, false);
  assert.equal(openRequests(f.requests).length, 0);
});

test("C15: non-401 HEAD failure during auto-download consumes pending deep-link state and download=1", async () => {
  for (const failureMode of ["http_503", "network_error"]) {
    const f = treeFixture();
    let deepLinkConsumed = 0;
    let downloadConsumed = 0;
    const loading = f.controls.load({
      initialPath: "team/guides/handbook.pdf",
      autoDownload: true,
      onDeepLinkConsumed: () => { deepLinkConsumed += 1; },
      onDownloadConsumed: () => { downloadConsumed += 1; },
    });
    await answer(f.requests, ROOT_LISTING, listing("", [entry("team")]));
    await answer(f.requests, "/api/web-chat/access-map?view=files", jsonResponse({ status: "ok", shared_by_you: [] }));
    await answer(f.requests, "/api/web-chat/workspace?path=team", listing("team", [entry("team/guides")]));
    await answer(
      f.requests,
      "/api/web-chat/workspace?path=team%2Fguides",
      listing("team/guides", [entry("team/guides/handbook.pdf", "file", 8 * 1024 * 1024)]),
    );
    await answer(
      f.requests,
      "/api/web-chat/workspace/file?path=team%2Fguides%2Fhandbook.pdf",
      fileReply("team/guides/handbook.pdf", "handbook.pdf", {
        preview_kind: "too_large",
        size_bytes: 8 * 1024 * 1024,
      }),
    );
    if (failureMode === "http_503") {
      await answer(
        f.requests,
        "/api/web-chat/workspace/download?path=team%2Fguides%2Fhandbook.pdf",
        headResponse(503),
      );
    } else {
      await settle();
      const pending = f.requests.find(
        req => !req.answered && req.path === "/api/web-chat/workspace/download?path=team%2Fguides%2Fhandbook.pdf",
      );
      assert.ok(pending, "expected HEAD download request");
      pending.answered = true;
      pending.respond(Promise.reject(new Error("Network error")));
      await settle();
    }
    await loading;

    assert.equal(deepLinkConsumed, 1, `onDeepLinkConsumed must run on ${failureMode}`);
    assert.equal(downloadConsumed, 1, `onDownloadConsumed must run on ${failureMode}`);
    assert.equal(f.downloads.length, 0, failureMode);
    assert.equal(f.viewerTitle.textContent, "handbook.pdf", failureMode);
    assert.deepEqual(actionLabels(f), [DOWNLOAD_ACTION_LABEL], failureMode);
    assert.match(f.status.textContent, /Could not download "handbook\.pdf"\./, failureMode);
    assert.ok(f.status.classList.contains("is-error"), failureMode);
  }
});

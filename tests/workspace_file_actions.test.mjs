// Workspace Files: the Share… and Send… dialogs (workspace_file_actions.mjs)
// and the file tree that offers them (workspace_browser.js, a classic script
// loaded here the way chat.html loads it).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import {
  ADMIN_VIEW_TITLE, MEMBER_VIEW_TITLE, SEND_NOTES, createWorkspaceFileActions, focusStops, renderShareDialog,
  rowActions, sendRequestBody, shareRequestBody, teamFolderTarget, workspaceViewTitle,
} from "../assistant_gateway/onboarding_web/assets/workspace_file_actions.mjs";
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
  assert.equal(workspaceViewTitle([]), MEMBER_VIEW_TITLE);
  assert.equal(workspaceViewTitle([{ name: "agents" }, { name: "team" }, { name: "users" }]), ADMIN_VIEW_TITLE);
});

test("only items inside the person's own folder get Share, and only files get Send", () => {
  const own = new Set([OWN]);
  assert.deepEqual(rowActions(NOTES, own), { share: true, send: true });
  assert.deepEqual(rowActions(REPORTS, own), { share: true, send: false });
  assert.deepEqual(rowActions({ path: `users/${OWN}`, kind: "directory" }, own), { share: false, send: false },
    "the folder itself is not shared from the tree");
  assert.deepEqual(rowActions({ path: "users/someone-else/a.md", kind: "file" }, own), { share: false, send: false });
  assert.deepEqual(rowActions({ path: "team/a.md", kind: "file" }, own), { share: false, send: false });
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

function treeFixture() {
  const harness = actionsFixture();
  const { document, browser } = harness;
  // The tree sets its indent with style.setProperty; the stand-in keeps it.
  const create = document.createElement;
  document.createElement = tagName => {
    const node = create(tagName);
    node.style = { values: {}, setProperty(name, value) { this.values[name] = value; } };
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
  const controls = window.UAWorkspaceBrowser.create({
    ...nodes, titleNodes: [nodes.title, nodes.heading], fileActions: harness.actions,
    markdownRenderer: { render: () => "" }, onUnauthorized: () => false,
  });
  return { ...harness, ...nodes, controls };
}

const entry = (path, kind = "directory") => ({ path, name: path.split("/").at(-1), kind, size_bytes: kind === "file" ? 12 : null,
  modified_at_utc: "2026-09-25T10:00:00+00:00" });
const listing = (path, entries) => jsonResponse({ status: "ok", path, entries, truncated: false, entry_limit: 2000 });
// A tree item is <div treeitem> > [<div line> > <button row>, <div group>?]; it carries no buttons.
const treeItem = (f, name) => descendants(f.tree).find(node => node.classList.contains("workspace-tree-item")
  && byClass(node.children[0], "workspace-tree-name")[0].textContent === name);
const rowOf = item => item.children[0].children[0];
// Share… and Send… for the selected item, at the top right of the preview pane.
const actionLabels = f => (f.itemActions.hidden ? [] : byClass(f.itemActions, "workspace-item-action").map(button => button.textContent));
const fileReply = (path, name) => jsonResponse({ status: "ok", name, path, kind: "text", content: "hi" });
const ROOT_LISTING = "/api/web-chat/workspace?path=";

async function memberTree() {
  const f = treeFixture();
  const loading = f.controls.load();
  await answer(f.requests, ROOT_LISTING, listing("", [entry("users")]));
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
  assert.deepEqual(actionLabels(f), ["Share…", "Send…"]);
  assert.equal(f.itemActions.getAttribute("aria-label"), "Actions for notes.md");
  const share = buttonNamed(f.itemActions, "Share…");
  assert.equal(share.getAttribute("aria-label"), "Share notes.md");
  assert.equal(byTag(share, "svg").length, 1, "a labeled button with its icon");
  assert.ok(rowOf(treeItem(f, "notes.md")).classList.contains("is-selected"));

  rowOf(treeItem(f, "reports")).click();
  await answer(f.requests, `/api/web-chat/workspace?path=users%2F${OWN}%2Freports`,
    listing(`users/${OWN}/reports`, [entry(`users/${OWN}/reports/q1.md`, "file")]));
  assert.deepEqual(actionLabels(f), ["Share…"], "a folder can be shared, not sent");
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

test("administrators see 'Workspace' and no actions outside their own folder", async () => {
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
  assert.deepEqual(actionLabels(f), []);
  await openOwnFolder(f, [entry(`users/${OWN}/notes.md`, "file")]);
  await selectOwnFile(f, "notes.md");
  assert.deepEqual(actionLabels(f), ["Share…", "Send…"], "their own folder works as for members");
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
  assert.deepEqual(f.actions.actionsFor(NOTES), { share: false, send: false }, "the person's folder is forgotten");
  await settle();
});

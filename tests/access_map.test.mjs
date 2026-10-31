import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { localTimeFormatter } from "../assistant_gateway/onboarding_web/assets/access_inbox.mjs";
import {
  ACCESS_OFF_TITLE, ADD_BLOCK_LABEL, BLOCKED_LABEL, EVERYONE_WORK_TITLE, STANDARD_ACCESS_LABEL,
  STANDARD_GROUP_TITLE, STARTER_PROFILE_TITLE, TEAM_BLOCK_LABEL, WAITING_LABEL, YOUR_WORK_TITLE,
  accessMapPath, chainText, createAccessMap, grantGroups, grantRowModel, isToolSet, lifetimeText, operationsPath,
  parseAccessMapLink, removeDataFolderPath, renderAgentCard, renderBlockForm, renderFilesView, renderPersonCard,
  renderHistoryCleanup, renderStarterProfile, renderToolCard, revokePath, stopSharingPath,
  HISTORY_CLEANUP_LABEL, HISTORY_CLEANUP_TITLE,
} from "../assistant_gateway/onboarding_web/assets/access_map.mjs";

// A small DOM stand-in (as in access_inbox.test.mjs). innerHTML throws, so any
// HTML parsing path fails the tests.
class FakeElement extends EventTarget {
  constructor(document, tagName) {
    super();
    Object.assign(this, { ownerDocument: document, tagName: tagName.toUpperCase(), children: [],
      parentNode: null, attributes: new Map(), className: "", ownText: "", hidden: false,
      disabled: false, readOnly: false, value: "", tabIndex: 0, scrolled: 0, id: "" });
  }
  get classList() {
    const names = () => this.className.split(/\s+/).filter(Boolean);
    const set = values => { this.className = [...new Set(values)].join(" "); };
    return {
      add: (...added) => set([...names(), ...added]),
      remove: (...removed) => set(names().filter(name => !removed.includes(name))),
      toggle: (name, force) => {
        const on = force === undefined ? !names().includes(name) : Boolean(force);
        set(on ? [...names(), name] : names().filter(value => value !== name));
      },
      contains: name => names().includes(name),
    };
  }
  set innerHTML(_value) { throw new Error("innerHTML must not be used"); }
  get innerHTML() { throw new Error("innerHTML must not be used"); }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this.replaceChildren(); this.ownText = String(value); }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children.at(-1) || null; }
  get isConnected() {
    for (let node = this; node; node = node.parentNode) if (node === this.ownerDocument.body) return true;
    return false;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  removeAttribute(name) { this.attributes.delete(name); }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  appendChild(node) {
    node.remove();
    node.parentNode = this;
    this.children.push(node);
    return node;
  }
  replaceChildren(...nodes) {
    this.children.forEach(child => { child.parentNode = null; });
    this.children = [];
    this.ownText = "";
    this.append(...nodes);
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter(child => child !== this);
    this.parentNode = null;
  }
  replaceWith(node) {
    const parent = this.parentNode;
    if (!parent) return;
    node.remove();
    parent.children.splice(parent.children.indexOf(this), 1, node);
    node.parentNode = parent;
    this.parentNode = null;
  }
  contains(node) {
    for (let current = node; current; current = current.parentNode) if (current === this) return true;
    return false;
  }
  querySelectorAll(tagName) { return descendants(this).filter(node => node.tagName === tagName.toUpperCase()); }
  focus() { this.ownerDocument.activeElement = this; }
  scrollIntoView() { this.scrolled += 1; }
  click() {
    this.dispatchEvent(new Event("click"));
    if (this.tagName === "BUTTON" && this.type === "submit" && !this.disabled) {
      let form = this.parentNode;
      while (form && form.tagName !== "FORM") form = form.parentNode;
      if (form) form.dispatchEvent(new Event("submit", { cancelable: true }));
    }
  }
}

function fakeDocument() {
  const document = {};
  document.createElement = tagName => new FakeElement(document, tagName);
  document.body = new FakeElement(document, "body");
  document.activeElement = document.body;
  return document;
}

function descendants(node) {
  return node.children.flatMap(child => [child, ...descendants(child)]);
}
const byTag = (node, tagName) => descendants(node).filter(child => child.tagName === tagName.toUpperCase());
const byClass = (node, name) => descendants(node).filter(child => child.classList.contains(name));
const buttonLabels = node => byTag(node, "button").map(button => button.textContent);
const buttonNamed = (node, label) => byTag(node, "button").find(button => button.textContent === label);
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };

const formatTime = localTimeFormatter({ locale: "en-US", timeZone: "UTC" });

function grant(overrides = {}) {
  return {
    grant_id: "g-web", for_person_id: "owner-1", for_whose_work: "your work", for_your_work: true,
    status: "active", target: "fn:web_research", target_kind: "tool",
    target_kind_words: "tool", target_name: "Web research", level: "read", level_words: "read",
    lifetime: "until_revoked", lifetime_words: "until someone removes it", expires_at_utc: null, task_id: null,
    hand_on_words: "Cannot hand it on.", chain: ["You", "TeamArchitect", "WebResearchAgent"],
    given_by: "TeamArchitect", reason: "Research", created_at_utc: "2026-09-25T10:00:00+00:00",
    can_revoke: true, ...overrides,
  };
}

// Access that serves someone else's work, or everyone's (only administrators see the former).
const forDana = overrides => grant({ grant_id: "g-dana", for_person_id: "dana-1", for_whose_work: "Dana's work",
  for_your_work: false, target_name: "Google Workspace (gog)", ...overrides });
const forBob = overrides => grant({ grant_id: "g-bob", for_person_id: "bob-1", for_whose_work: "Bob's work",
  for_your_work: false, target_name: "GitHub API", ...overrides });
const forEveryone = overrides => grant({ grant_id: "g-all", for_person_id: null, for_whose_work: "everyone's work",
  for_your_work: false, target_name: "Weather", ...overrides });

function block(overrides = {}) {
  return {
    block_id: "b-1", target: "cli:google", target_kind: "tool", target_kind_words: "tool",
    target_name: "Google Workspace (gog)", lifetime: "until_revoked", lifetime_words: "until someone removes it",
    expires_at_utc: null, blocked_by: "You", reason: "Too much mail", created_at_utc: "2026-09-25T11:00:00+00:00",
    can_unblock: true, ...overrides,
  };
}

function holder(overrides = {}) {
  return {
    key: "agent_type:WebResearchAgent", subject_kind: "agent_type", subject: "WebResearchAgent",
    title: "WebResearchAgent", role_words: "Worker",
    standard: [{ target: "agent_runtime", target_kind: "tool_set", target_kind_words: "tool set",
      target_name: "Agent runtime tools", level: "full", level_words: "full" }],
    grants: [grant()], blocks: [], can_block: true, ...overrides,
  };
}

const BLOCK_TOOLS = [
  { tool_id: "cli:google", display_name: "Google Workspace (gog)" },
  { tool_id: "fn:web_research", display_name: "Web research" },
];
// Administrators also get tool sets, such as all internet access, first.
const ADMIN_BLOCK_TOOLS = [{ tool_id: "internet", display_name: "Internet access (all of it)" }, ...BLOCK_TOOLS];

const STARTER_PROFILE = {
  current: "careful",
  choices: [
    { name: "trusted", display_name: "Trusted team",
      description: "TeamLead gets full access to the standard tool set and may give it to workers without asking you." },
    { name: "careful", display_name: "Careful team",
      description: "TeamLead gets read access to the standard tool set and may give it to workers." },
    { name: "manual", display_name: "Manual", description: "TeamLead gets nothing; every access is asked for." },
  ],
};

function share(overrides = {}) {
  return {
    share_id: "s-1", item: "reports/q1.md", target: "users/owner-1/reports/q1.md", is_file: true,
    owner_folder: "owner-1", person_id: "dana-1", person: "Dana", shared_by: "You", level: "read",
    level_words: "read", expires_at_utc: null, ...overrides,
  };
}

function filesPayload(overrides = {}) {
  return {
    status: "ok", view: "files", enabled: true, notice: "",
    shared_by_you: [share(), share({ share_id: "s-2", item: "drafts/", target: "users/owner-1/drafts",
      is_file: false, person: "Bob", level: "write", level_words: "read and change",
      expires_at_utc: "2026-10-02T09:00:00+00:00" })],
    shared_with_you: [share({ share_id: "s-9", item: "plan.md", target: "users/dana-1/plan.md", person: "You",
      shared_by: "Dana" })],
    data_folders: [{ name: "reports", folder_id: "data/reports", path: "/srv/shared/reports", writable: false,
      access_words: "read only", registered_by: "Olivia Owner", created_at_utc: "2026-09-20T09:00:00+00:00" }],
    can_manage_folders: true,
    ...overrides,
  };
}

test("Access links choose an agent type only from the agent fragment key", () => {
  assert.equal(parseAccessMapLink("#agent=WebResearchAgent"), "WebResearchAgent");
  assert.equal(parseAccessMapLink("#access_request=r-1&agent=Team%20Lead"), "Team Lead");
  assert.equal(parseAccessMapLink("#access_request=r-1"), "");
  assert.equal(parseAccessMapLink(undefined), "");
});

test("paths carry only the view, the offset and ids, all encoded", () => {
  assert.equal(accessMapPath("people", 20, ""), "/api/web-chat/access-map?view=people&offset=20");
  assert.equal(accessMapPath("files", 0, ""), "/api/web-chat/access-map?view=files&offset=0");
  assert.equal(accessMapPath("agents", -5, "Web Agent"), "/api/web-chat/access-map?view=agents&offset=0&agent=Web+Agent");
  assert.equal(operationsPath("cli:google", 40), "/api/web-chat/access-map/tools/cli%3Agoogle/operations?offset=40");
  assert.equal(revokePath("g/1"), "/api/web-chat/access-inbox/grants/g%2F1/revoke");
  assert.equal(stopSharingPath("s/1"), "/api/web-chat/access-map/shares/s%2F1/stop");
  assert.equal(removeDataFolderPath("q1 reports"), "/api/web-chat/access-map/data-folders/q1%20reports/remove");
});

test("tool sets are told apart from tools the way the gateway parses targets", () => {
  assert.equal(isToolSet({ tool_id: "internet" }), true);
  assert.equal(isToolSet({ tool_id: "cli:google" }), false);
  assert.equal(isToolSet({ tool_id: "" }), false);
});

test("grants are grouped by whose work they serve: yours, each person's by name, then everyone's", () => {
  const groups = grantGroups([forEveryone(), forDana(), grant(), forBob(), forDana({ grant_id: "g-dana-2" })]);
  assert.deepEqual(groups.map(group => group.title),
    [YOUR_WORK_TITLE, "For Bob's work", "For Dana's work", EVERYONE_WORK_TITLE]);
  assert.deepEqual(groups.map(group => group.grants.map(item => item.grant_id)),
    [["g-web"], ["g-bob"], ["g-dana", "g-dana-2"], ["g-all"]]);
  // "For your work" stays even when empty; the other groups appear only with rows.
  assert.deepEqual(grantGroups([]).map(group => group.title), [YOUR_WORK_TITLE]);
});

test("chains read from the person to the agent, and end times are local and readable", () => {
  assert.equal(chainText(["You", "TeamArchitect", "WebResearchAgent"]), "You → TeamArchitect → WebResearchAgent");
  assert.equal(chainText(["", " You ", null]), "You");
  assert.equal(chainText(undefined), "");
  const expiring = { lifetime_words: "until a set time", expires_at_utc: "2026-09-30T17:00:00+00:00" };
  assert.equal(lifetimeText(expiring, formatTime).replace(/\s/g, " "), "until a set time (ends Sep 30, 2026, 5:00 PM UTC)");
  assert.doesNotMatch(lifetimeText(expiring, formatTime), /2026-09-30T/);
});

test("an agent card shows standard access first, then access for your work with its chain, and blocks", () => {
  const document = fakeDocument();
  const card = renderAgentCard(document, holder({ blocks: [block()] }), { formatTime });
  assert.equal(card.getAttribute("data-key"), "agent_type:WebResearchAgent");
  assert.equal(byTag(card, "h5")[0].textContent, "WebResearchAgent");
  assert.deepEqual(byTag(card, "h6").map(node => node.textContent), [STANDARD_GROUP_TITLE, YOUR_WORK_TITLE, "Blocks"]);
  const content = card.textContent;
  assert.match(content, /Came fromYou → TeamArchitect → WebResearchAgent/);
  assert.match(content, /Accessread/);
  assert.match(content, /How longuntil someone removes it/);
  assert.match(content, /Blocked byYou/);
  assert.match(content, /Added\s*Sep 25, 2026, 11:00\sAM UTC/);
  assert.doesNotMatch(content, /2026-09-25T/, "Raw ISO times are never shown");

  const standardRow = byClass(card, "access-map-row")
    .find(row => byClass(row, "access-inbox-status").some(node => node.textContent === STANDARD_ACCESS_LABEL));
  assert.ok(standardRow, "standard access is labeled as such");
  assert.equal(byTag(standardRow, "button").length, 0, "standard access has no remove button");
  assert.match(standardRow.textContent, /Comes withthe worker role/);
  assert.match(card.textContent, /a block can stop it/);
  assert.ok(byClass(card, "access-inbox-status").some(node => node.textContent === BLOCKED_LABEL));
  assert.deepEqual(buttonLabels(card), ["Remove", "Remove block", ADD_BLOCK_LABEL]);
  assert.ok(buttonNamed(card, "Remove").classList.contains("danger"));
});

test("an administrator's card groups access for each person's work and for everyone's work", () => {
  const document = fakeDocument();
  const card = renderAgentCard(document, holder({
    grants: [forDana(), grant(), forEveryone({ can_revoke: false }), forBob()],
  }), { formatTime });
  assert.deepEqual(byTag(card, "h6").map(node => node.textContent),
    [STANDARD_GROUP_TITLE, YOUR_WORK_TITLE, "For Bob's work", "For Dana's work", EVERYONE_WORK_TITLE]);
  const everyone = byClass(card, "access-map-section").find(node => node.getAttribute("data-group") === "everyone");
  assert.match(everyone.textContent, /Weather/);
  assert.equal(byTag(everyone, "button").length, 0, "Remove appears only where the gateway allows it");
  const dana = byClass(card, "access-map-section").find(node => node.getAttribute("data-group") === "person:dana-1");
  assert.deepEqual(buttonLabels(dana), ["Remove"]);
  assert.match(dana.textContent, /Came fromYou → TeamArchitect → WebResearchAgent/);
});

test("a member's card says when nothing serves their work and still lists everyone's", () => {
  const document = fakeDocument();
  const card = renderAgentCard(document, holder({ grants: [forEveryone({ can_revoke: false })], can_block: false }),
    { formatTime });
  assert.deepEqual(byTag(card, "h6").map(node => node.textContent),
    [STANDARD_GROUP_TITLE, YOUR_WORK_TITLE, EVERYONE_WORK_TITLE]);
  assert.match(card.textContent, /No access is given for your work\./);
  assert.deepEqual(buttonLabels(card), []);
});

test("buttons appear only when the gateway offers them", () => {
  const document = fakeDocument();
  const card = renderAgentCard(document, holder({
    grants: [grant({ can_revoke: false }), grant({ grant_id: "g-2", status: "pending", can_revoke: true })],
    blocks: [block({ can_unblock: false })],
    can_block: false,
  }), { formatTime });
  assert.deepEqual(buttonLabels(card), [], "a stale can_revoke on a pending hand-down never creates a button");
  assert.ok(byClass(card, "access-inbox-status").some(node => node.textContent === WAITING_LABEL));
  assert.equal(grantRowModel(grant({ status: "pending" })).canRevoke, false);
});

test("people rows say who received what, and an empty person says so", () => {
  const document = fakeDocument();
  const card = renderPersonCard(document, {
    person_id: "owner-1", title: "You", is_you: true,
    given: [{ ...grant(), kind: "access", recipient: "TeamArchitect", chain: [] },
      { ...block(), kind: "block", recipient: "WebResearchAgent" }],
  }, { formatTime });
  assert.match(card.textContent, /ToTeamArchitect/);
  assert.match(card.textContent, /WhoWebResearchAgent/);
  assert.doesNotMatch(card.textContent, /Came from/);
  assert.deepEqual(buttonLabels(card), ["Remove", "Remove block"]);
  const empty = renderPersonCard(document, { person_id: "member-1", title: "You", is_you: true, given: [] }, { formatTime });
  assert.match(empty.textContent, /You have not given any access or added any blocks\./);
});

test("tool cards show levels and whose accounts it uses, never account details", () => {
  const document = fakeDocument();
  const card = renderToolCard(document, {
    tool_id: "http:github", display_name: "GitHub API", description: "<b>Issues</b> and pull requests",
    kind_words: "Web API", account_words: "Uses a linked account", uses_accounts: true, tool_sets: ["GitHub kit"],
    operation_count: 7, operations_words: "7 operations: 4 need read, 3 need read-and-change.", unreviewed_count: 0,
    links: [{ link_id: "l-1", owner_kind: "team", owner_words: "The team's account", linked_by: "Olivia Owner",
      created_at_utc: "2026-09-20T09:00:00+00:00" }],
  }, { formatTime });
  assert.equal(byTag(card, "b").length, 0, "descriptions stay text");
  assert.match(card.textContent, /<b>Issues<\/b> and pull requests/);
  assert.match(card.textContent, /Operations7 operations: 4 need read, 3 need read-and-change\./);
  assert.match(card.textContent, /The team's account/);
  assert.match(card.textContent, /Linked by Olivia Owner on Sep 20, 2026/);
  const toggle = buttonNamed(card, "Show operations");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(toggle.getAttribute("aria-controls"), byClass(card, "access-map-operations")[0].id);
});

test("the Block form checks the tool and reason before sending", () => {
  const document = fakeDocument();
  const sent = [];
  const view = renderBlockForm(document, holder(), BLOCK_TOOLS, { onSubmit: parts => sent.push(parts) });
  assert.deepEqual(byTag(view.fields.tool, "option").map(option => option.value), ["", "cli:google", "fn:web_research"]);
  assert.deepEqual(byTag(view.fields.duration, "option").map(option => option.value), ["", "1", "24", "168", "720"]);
  view.submit.click();
  assert.equal(sent.length, 0);
  assert.equal(view.message.textContent, "Choose the tool to block.");
  view.fields.tool.value = "cli:google";
  view.submit.click();
  assert.equal(view.message.textContent, "Say why you are blocking it; people read the reason.");
  view.fields.reason.value = "  Sending too much mail ";
  view.fields.operation.value = " command:gmail.send ";
  view.fields.duration.value = "24";
  view.submit.click();
  assert.deepEqual(sent[0].values, { subject_kind: "agent_type", subject: "WebResearchAgent", tool_id: "cli:google",
    operation: "command:gmail.send", hours: "24", reason: "Sending too much mail" });
  assert.equal(view.fields.who, null, "without allowTeam the form blocks only this agent type");
});

test("the Block form can block everyone in the team from a tool set", () => {
  const document = fakeDocument();
  const sent = [];
  const view = renderBlockForm(document, holder(), ADMIN_BLOCK_TOOLS, { allowTeam: true, onSubmit: parts => sent.push(parts) });
  assert.deepEqual(byTag(view.fields.who, "option").map(option => option.textContent), ["WebResearchAgent", TEAM_BLOCK_LABEL]);
  assert.deepEqual(byTag(view.fields.tool, "option").map(option => option.textContent),
    ["Choose a tool", "Internet access (all of it)", "Google Workspace (gog) (cli:google)", "Web research (fn:web_research)"]);
  assert.match(view.form.textContent, /Choose Everyone in the team to stop every agent/);

  view.fields.who.value = "team";
  view.fields.tool.value = "internet";
  view.fields.operation.value = "fetch";
  view.fields.reason.value = "No internet this week";
  view.submit.click();
  assert.equal(sent.length, 0);
  assert.match(view.message.textContent, /A tool set is blocked as a whole/);

  view.fields.operation.value = "";
  view.submit.click();
  assert.deepEqual(sent[0].values, { subject_kind: "team", subject: "", tool_id: "internet", operation: "",
    hours: "", reason: "No internet this week" });

  view.fields.who.value = "holder";
  view.submit.click();
  assert.deepEqual([sent[1].values.subject_kind, sent[1].values.subject], ["agent_type", "WebResearchAgent"]);
});

test("the Files view lists what you share, what is shared with you, and data folders", () => {
  const document = fakeDocument();
  const sections = renderFilesView(document, filesPayload(), { formatTime });
  assert.deepEqual(sections.map(node => byTag(node, "h4")[0].textContent),
    ["Shared by you", "Shared with you", "Data folders"]);
  const [mine, theirs, folders] = sections;
  assert.deepEqual(byTag(mine, "th").map(node => node.textContent), ["Item", "Person", "Level", "Until", "Actions"]);
  const [first, second] = byTag(mine, "tr").slice(1);
  assert.deepEqual(byTag(first, "td").map(node => node.textContent),
    ["reports/q1.md", "Dana", "read", "Until you stop sharing", "Stop sharing"]);
  assert.equal(byTag(second, "td")[3].textContent.replace(/\s/g, " "), "Oct 2, 2026, 9:00 AM UTC");
  assert.doesNotMatch(mine.textContent, /2026-10-02T/, "Raw ISO times are never shown");
  assert.equal(byTag(first, "td")[0].getAttribute("data-label"), "Item", "narrow screens label each cell");

  assert.deepEqual(byTag(theirs, "th").map(node => node.textContent), ["Item", "Owner", "Level"]);
  assert.deepEqual(byTag(byTag(theirs, "tr")[1], "td").map(node => node.textContent),
    ["users/dana-1/plan.md", "Dana", "read"]);
  assert.deepEqual(buttonLabels(theirs), [], "only the owner stops a share");

  assert.deepEqual(byTag(byTag(folders, "tr")[1], "td").map(node => node.textContent),
    ["data/reports", "/srv/shared/reports", "Read-only", "Remove"]);
  assert.deepEqual(buttonLabels(folders), ["Remove", "Add folder"]);
  const readOnly = byClass(folders, "access-files-read-only")[0];
  assert.equal(readOnly.checked, true, "new data folders start read-only");
});

test("the Files view says so when nothing is shared, and members get no folder controls", () => {
  const document = fakeDocument();
  const sections = renderFilesView(document, filesPayload({
    shared_by_you: [], shared_with_you: [],
    data_folders: [{ name: "media", path: "/mnt/media", writable: true }], can_manage_folders: false,
  }), { formatTime });
  const text = sections.map(node => node.textContent).join("\n");
  assert.match(text, /You have not shared anything\. Use Share… on a file or folder in Workspace Files\./);
  assert.match(text, /Nobody has shared anything with you\./);
  assert.match(text, /Only administrators add or remove data folders\./);
  assert.match(text, /data\/media\/mnt\/mediaWritable/);
  assert.deepEqual(sections.flatMap(buttonLabels), []);
  assert.equal(sections.flatMap(node => byTag(node, "form")).length, 0);
  const empty = renderFilesView(document, filesPayload({ data_folders: [], can_manage_folders: false }), { formatTime });
  assert.match(empty[2].textContent, /No data folders are registered\./);
});

function controllerFixture() {
  const document = fakeDocument();
  const container = document.createElement("div");
  document.body.append(container);
  // chat.html loads the real busy-button helper before this module.
  const window = {};
  vm.runInNewContext(
    readFileSync(new URL("../assistant_gateway/onboarding_web/assets/action_feedback.js", import.meta.url), "utf8"),
    { window },
  );
  const gets = [], posts = [], timers = [], pages = [];
  let refreshes = 0, finished = 0;
  const browser = {
    document, console: { warn: () => {} },
    requestAnimationFrame: callback => callback(),
    setTimeout: (callback, delay) => timers.push({ callback, delay }),
  };
  const map = createAccessMap({
    container, browser, formatTime,
    get: path => new Promise((resolve, reject) => gets.push({ path, resolve, reject })),
    post: (path, body) => new Promise((resolve, reject) => posts.push({ path, body, resolve, reject })),
    beginAction: (button, busyLabel, group) => window.UAActionFeedback.begin(button, { busyLabel, group }),
    beginRefresh: () => { refreshes += 1; return () => { finished += 1; }; },
    failureMessage: (error, fallback) => (error && error.message) || fallback,
    paginate: options => { pages.push(options); return document.createElement("nav"); },
  });
  const [tabs, status, intro, off, list, pager] = container.children;
  return { document, container, map, gets, posts, timers, pages, tabs, status, intro, off, list, pager,
    get refreshes() { return refreshes; }, get finished() { return finished; } };
}

async function loaded(f, payload, load = () => f.map.load()) {
  const pending = load();
  await settle();
  f.gets.at(-1).resolve({ status: "ok", enabled: true, notice: "", total: (payload.items || []).length,
    offset: 0, page_size: 20, has_more: false, ...payload });
  await pending;
  await settle();
}

test("the page loads By Agent Type first and switches views on page one", async () => {
  const f = controllerFixture();
  await loaded(f, { view: "agents", items: [holder()], block_tools: BLOCK_TOOLS });
  assert.equal(f.gets[0].path, "/api/web-chat/access-map?view=agents&offset=0");
  assert.deepEqual(buttonLabels(f.tabs), ["By Agent Type", "By Person", "By Tool", "Files"]);
  assert.equal(f.tabs.children[0].getAttribute("aria-selected"), "true");
  assert.equal(f.list.children.length, 1);
  assert.match(f.intro.textContent, /Standard access comes with an agent's role/);
  assert.equal(f.refreshes, 1);
  assert.equal(f.finished, 1, "Refresh shows busy only while reading");

  buttonNamed(f.tabs, "By Person").click();
  await settle();
  assert.equal(f.gets[1].path, "/api/web-chat/access-map?view=people&offset=0");
  f.gets[1].resolve({ status: "ok", enabled: true, view: "people", sees_everyone: false, total: 1, offset: 0,
    page_size: 20, items: [{ person_id: "member-1", title: "You", is_you: true, given: [] }] });
  await settle();
  assert.equal(f.tabs.children[1].getAttribute("aria-selected"), "true");
  assert.match(f.intro.textContent, /visible only to them and to administrators/);
});

test("more than 20 items get the shared page controls, which read the chosen page", async () => {
  const f = controllerFixture();
  await loaded(f, { view: "tools", items: [], total: 42, has_more: true });
  assert.equal(f.pages.length, 1);
  assert.equal(f.pages[0].total, 42);
  assert.equal(f.pages[0].pageSize, 20);
  const next = f.pages[0].onPage(20);
  await settle();
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-map?view=agents&offset=20");
  f.gets.at(-1).resolve({ status: "ok", enabled: true, items: [], total: 42, offset: 20, page_size: 20 });
  await next;
});

test("with access control off, the page says so and shows only the data folders", async () => {
  const f = controllerFixture();
  await loaded(f, { enabled: false, notice: "Access control is off on this instance.", items: [] });
  // The other views have nothing to show, so the page reads the Files view.
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-map?view=files&offset=0");
  await loaded(f, filesPayload({ enabled: false, notice: "Access control is off on this instance.",
    shared_by_you: [], shared_with_you: [] }), () => Promise.resolve());
  assert.equal(f.tabs.hidden, true);
  assert.equal(f.off.hidden, false);
  assert.equal(byTag(f.off, "h4")[0].textContent, ACCESS_OFF_TITLE);
  assert.match(f.off.textContent, /Access control is off on this instance\./);
  assert.equal(f.list.hidden, false);
  assert.deepEqual(Array.from(f.list.children).map(node => byTag(node, "h4")[0].textContent), ["Data folders"]);
  assert.match(f.list.textContent, /With access control off, every agent can use them at the access shown\./);
  assert.deepEqual(buttonLabels(f.list), ["Remove", "Add folder"]);
});

test("an empty or refused view shows the gateway's words", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [], notice: "You are not an active member of this team." });
  assert.equal(f.list.children[0].textContent, "You are not an active member of this team.");
});

test("an Access link opens and focuses its agent type, or says it is not listed", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder({ key: "agent_type:TeamArchitect", title: "TeamArchitect" }), holder()],
    focus_key: "agent_type:WebResearchAgent" }, () => f.map.open("WebResearchAgent"));
  assert.equal(f.gets[0].path, "/api/web-chat/access-map?view=agents&offset=0&agent=WebResearchAgent");
  const target = f.list.children[1];
  assert.ok(target.classList.contains("is-highlighted"));
  assert.equal(target.scrolled, 1);
  assert.equal(f.document.activeElement, target);

  await loaded(f, { items: [holder()], focus_missing: true }, () => f.map.open("NobodyAgent"));
  assert.equal(f.status.hidden, false);
  assert.equal(f.status.textContent, "No access or blocks are listed for NobodyAgent.");
});

test("removing access asks for confirmation, then reports the result and refreshes", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder()] });
  const remove = buttonNamed(f.list, "Remove");
  remove.click();
  await settle();
  assert.equal(f.posts.length, 0, "the first click only asks for confirmation");
  assert.equal(remove.textContent, "Confirm removal");
  assert.equal(f.timers[0].delay, 5000);
  remove.click();
  assert.equal(remove.textContent, "Removing…");
  assert.equal(remove.getAttribute("aria-busy"), "true");
  await settle();
  assert.equal(f.posts[0].path, "/api/web-chat/access-inbox/grants/g-web/revoke");
  assert.deepEqual(f.posts[0].body, {});
  f.posts[0].resolve({ status: "ok", revoked_grant_ids: ["g-web", "g-child"] });
  await settle();
  assert.equal(f.gets.length, 2, "the page is read again after a change");
  f.gets[1].resolve({ status: "ok", enabled: true, items: [holder({ grants: [] })], total: 1, offset: 0, page_size: 20 });
  await settle();
  assert.equal(f.status.hidden, false);
  assert.equal(f.status.textContent,
    "Access removed: Web research. Access handed on from it was removed too (1 more). It applies from the next action.");
  assert.match(f.list.textContent, /No access is given for your work\./);
});

test("one entry per tool: a step-up and a note share one row, and removal counts only what was handed on", async () => {
  const entry = grant({
    grant_ids: ["g-web", "g-step-up"], level_words: "read-and-change, and full while one task runs",
    entry_note: "An administrator also gave this agent access to this for everyone's work. Removing it here does not remove that.",
  });
  const model = grantRowModel(entry, { formatTime });
  assert.deepEqual(model.details.map(row => row.label), ["Access", "How long", "Handing on", "Came from", "Why", "Note"]);
  assert.equal(model.details[0].value, "read-and-change, and full while one task runs");

  const f = controllerFixture();
  await loaded(f, { items: [holder({ grants: [entry] })] });
  const remove = buttonNamed(f.list, "Remove");
  remove.click();
  await settle();
  assert.match(f.list.textContent, /remove all of this access/);
  remove.click();
  await settle();
  f.posts[0].resolve({ status: "ok", revoked_grant_ids: ["g-web", "g-step-up", "g-child"] });
  await settle();
  f.gets[1].resolve({ status: "ok", enabled: true, items: [holder({ grants: [] })], total: 1, offset: 0, page_size: 20 });
  await settle();
  assert.equal(f.status.textContent,
    "Access removed: Web research. Access handed on from it was removed too (1 more). It applies from the next action.");
});

test("a refused removal shows the gateway's reason in the row and resets the button", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder()] });
  const remove = buttonNamed(f.list, "Remove");
  remove.click();
  remove.click();
  await settle();
  f.posts[0].reject(new Error("Only the person who gave this access, or someone who could give it, can revoke it."));
  await settle();
  assert.equal(remove.textContent, "Remove");
  assert.equal(remove.disabled, false);
  const [message] = byClass(remove.parentNode.parentNode, "access-inbox-card-message");
  assert.ok(message.classList.contains("is-error"));
  assert.equal(message.textContent, "Only the person who gave this access, or someone who could give it, can revoke it.");
  assert.equal(f.gets.length, 1, "a refusal does not replace the list");
});

test("adding a block sends the form, keeps values after a refusal, and reports success", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder()], block_tools: BLOCK_TOOLS });
  const add = buttonNamed(f.list, ADD_BLOCK_LABEL);
  add.click();
  assert.equal(add.disabled, true);
  const [form] = byTag(f.list, "form");
  assert.equal(f.document.activeElement, byTag(form, "select")[0], "the form opens at its first field");
  const [tool, duration] = byTag(form, "select");
  tool.value = "cli:google";
  duration.value = "168";
  byTag(form, "textarea")[0].value = "Sending too much mail";
  buttonNamed(form, "Block").click();
  await settle();
  assert.equal(f.posts[0].path, "/api/web-chat/access-map/blocks");
  assert.deepEqual(f.posts[0].body, { subject_kind: "agent_type", subject: "WebResearchAgent", tool_id: "cli:google",
    operation: "", hours: "168", reason: "Sending too much mail" });
  assert.ok(byTag(form, "button").every(button => button.disabled), "Block and Cancel wait for the reply");
  assert.equal(buttonNamed(form, "Blocking…").getAttribute("aria-busy"), "true");

  f.posts[0].reject(new Error("You can block only access you could give, and you cannot give access to Google Workspace (gog)."));
  await settle();
  assert.match(form.textContent, /you cannot give access to Google Workspace/);
  assert.equal(tool.value, "cli:google", "entered values stay after a refusal");
  assert.equal(buttonNamed(form, "Block").disabled, false);

  buttonNamed(form, "Block").click();
  await settle();
  f.posts[1].resolve({ status: "ok", message: "WebResearchAgent is blocked from Google Workspace (gog). It applies from the next action." });
  await settle();
  assert.equal(f.status.textContent, "WebResearchAgent is blocked from Google Workspace (gog). It applies from the next action.");
  assert.equal(f.gets.length, 2);
});

test("administrators can block the whole team from the page, for example all internet access", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder()], block_tools: ADMIN_BLOCK_TOOLS });
  buttonNamed(f.list, ADD_BLOCK_LABEL).click();
  const [form] = byTag(f.list, "form");
  const [who, tool] = byTag(form, "select");
  assert.equal(f.document.activeElement, who, "the form opens at Who when the team can be chosen");
  who.value = "team";
  tool.value = "internet";
  byTag(form, "textarea")[0].value = "Internet off during the audit";
  buttonNamed(form, "Block").click();
  await settle();
  assert.deepEqual(f.posts[0].body, { subject_kind: "team", subject: "", tool_id: "internet", operation: "",
    hours: "", reason: "Internet off during the audit" });
});

test("the team choice follows the gateway's By Person answer and is not offered to members", async () => {
  const member = controllerFixture();
  await loaded(member, { items: [holder()], block_tools: BLOCK_TOOLS });
  buttonNamed(member.list, ADD_BLOCK_LABEL).click();
  assert.equal(byTag(member.list, "select").length, 2, "Tool and How long only");

  const admin = controllerFixture();
  await loaded(admin, { view: "people", sees_everyone: true, items: [] }, () => admin.map.setView("people"));
  await loaded(admin, { items: [holder()], block_tools: BLOCK_TOOLS }, () => admin.map.setView("agents"));
  buttonNamed(admin.list, ADD_BLOCK_LABEL).click();
  assert.deepEqual(byTag(byTag(admin.list, "select")[0], "option").map(option => option.textContent),
    ["WebResearchAgent", TEAM_BLOCK_LABEL]);
});

test("Cancel closes the Block form and returns focus to its button", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder()], block_tools: BLOCK_TOOLS });
  const add = buttonNamed(f.list, ADD_BLOCK_LABEL);
  add.click();
  buttonNamed(f.list, "Cancel").click();
  assert.equal(byTag(f.list, "form").length, 0);
  assert.equal(add.disabled, false);
  assert.equal(f.document.activeElement, add);
});

test("removing a block asks for confirmation and shows the gateway's result", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder({ blocks: [block()] })] });
  const unblock = buttonNamed(f.list, "Remove block");
  unblock.click();
  assert.equal(unblock.textContent, "Confirm removal");
  assert.equal(f.posts.length, 0);
  unblock.click();
  assert.equal(unblock.textContent, "Removing…");
  await settle();
  assert.equal(f.posts[0].path, "/api/web-chat/access-map/blocks/b-1/remove");
  f.posts[0].resolve({ status: "ok", message: "The block on WebResearchAgent for Google Workspace (gog) is removed." });
  await settle();
  assert.equal(f.status.textContent, "The block on WebResearchAgent for Google Workspace (gog) is removed.");
});

test("TeamLead's standing access shows each choice with its description and the one in use", () => {
  const document = fakeDocument();
  const saved = [];
  const view = renderStarterProfile(document, STARTER_PROFILE, { onSave: parts => saved.push(parts) });
  assert.equal(byTag(view.node, "h4")[0].textContent, STARTER_PROFILE_TITLE);
  assert.deepEqual(view.inputs.map(input => [input.value, input.checked]),
    [["trusted", false], ["careful", true], ["manual", false]]);
  assert.match(view.node.textContent, /Careful teamIn use/);
  assert.match(view.node.textContent, /TeamLead gets nothing; every access is asked for\./);
  assert.match(view.node.textContent, /TeamLead can hand this on to workers/);
  view.inputs[1].checked = false;
  view.inputs[2].checked = true;
  view.submit.click();
  assert.equal(saved[0].name, "manual");

  const none = renderStarterProfile(document, { ...STARTER_PROFILE, current: null }, { onSave: parts => saved.push(parts) });
  assert.match(none.node.textContent, /None of these is in place for you now\./);
  assert.ok(none.inputs.every(input => !input.checked));
  none.submit.click();
  assert.equal(saved.length, 1, "nothing is sent without a choice");
  assert.equal(none.message.textContent, "Choose how much TeamLead may do for your work.");
});

test("saving TeamLead's standing access posts the profile name, then reloads the view", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder()], starter_profile: STARTER_PROFILE });
  const [control] = f.list.children;
  assert.equal(byTag(control, "h4")[0].textContent, STARTER_PROFILE_TITLE, "the control sits above the cards");
  assert.equal(f.list.children.length, 2);
  const trusted = byTag(control, "input").find(input => input.value === "trusted");
  byTag(control, "input").forEach(input => { input.checked = input === trusted; });
  const save = buttonNamed(control, "Save");
  save.click();
  assert.equal(save.textContent, "Saving…");
  assert.equal(save.getAttribute("aria-busy"), "true");
  await settle();
  assert.equal(f.posts[0].path, "/api/web-chat/access-map/starter-profile");
  assert.deepEqual(f.posts[0].body, { profile: "trusted" });

  f.posts[0].reject(new Error("You can give only access you hold yourself."));
  await settle();
  assert.match(control.textContent, /You can give only access you hold yourself\./);
  assert.equal(trusted.checked, true, "the choice stays after a refusal");
  assert.equal(save.disabled, false);

  save.click();
  await settle();
  f.posts[1].resolve({ status: "ok", message: "Trusted team for your work: TeamLead gets full access.",
    starter_profile: { ...STARTER_PROFILE, current: "trusted" } });
  await settle();
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-map?view=agents&offset=0", "the view is read again");
  f.gets.at(-1).resolve({ status: "ok", enabled: true, items: [holder()], total: 1, offset: 0, page_size: 20,
    starter_profile: { ...STARTER_PROFILE, current: "trusted" } });
  await settle();
  assert.equal(f.status.textContent, "Trusted team for your work: TeamLead gets full access.");
  assert.match(f.list.children[0].textContent, /Trusted teamIn use/);
});

const HISTORY_CLEANUP = {
  available: true, on: false, grant_id: null,
  description: "When you ask TeamLead to delete your old chats or finished tasks, it does so without asking you. "
    + "Remembered facts are kept; you manage them under Memories.",
};

test("the history clean-up switch says what it does and reports each flip", () => {
  const document = fakeDocument();
  const flips = [];
  const view = renderHistoryCleanup(document, HISTORY_CLEANUP, { onToggle: parts => flips.push(parts) });
  assert.equal(byTag(view.node, "h4")[0].textContent, HISTORY_CLEANUP_TITLE);
  assert.match(view.node.textContent, new RegExp(HISTORY_CLEANUP_LABEL));
  assert.match(view.node.textContent, /Remembered facts are kept/);
  assert.equal(view.input.getAttribute("role"), "switch");
  assert.equal(view.input.checked, false);
  view.input.checked = true;
  view.input.dispatchEvent(new Event("change"));
  assert.equal(flips.length, 1);
  assert.equal(flips[0].on, true);
  assert.equal(renderHistoryCleanup(document, { ...HISTORY_CLEANUP, on: true }).input.checked, true);
});

test("flipping the history clean-up switch posts it, and a refusal puts the switch back", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder()], starter_profile: STARTER_PROFILE, history_cleanup: HISTORY_CLEANUP });
  const [profile, cleanup] = f.list.children;
  assert.equal(byTag(profile, "h4")[0].textContent, STARTER_PROFILE_TITLE);
  assert.equal(byTag(cleanup, "h4")[0].textContent, HISTORY_CLEANUP_TITLE, "the switch sits below the profile");
  const input = byTag(cleanup, "input")[0];
  input.checked = true;
  input.dispatchEvent(new Event("change"));
  await settle();
  assert.equal(f.posts[0].path, "/api/web-chat/access-map/history-cleanup");
  assert.deepEqual(f.posts[0].body, { on: true });
  assert.equal(input.disabled, true, "no second flip while saving");
  f.posts[0].reject(new Error("Only you can give this."));
  await settle();
  assert.equal(input.checked, false, "the switch shows what is stored");
  assert.equal(input.disabled, false);
  assert.match(cleanup.textContent, /Only you can give this\./);

  input.checked = true;
  input.dispatchEvent(new Event("change"));
  await settle();
  f.posts[1].resolve({ status: "ok", message: "TeamLead may now clean up your old chats." });
  await settle();
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-map?view=agents&offset=0", "the view is read again");
});

test("the history clean-up switch is left out when the catalog has no such tool", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder()], starter_profile: STARTER_PROFILE, history_cleanup: { ...HISTORY_CLEANUP, available: false } });
  assert.doesNotMatch(f.list.textContent, new RegExp(HISTORY_CLEANUP_TITLE));
});

test("the standing access control belongs to By Agent Type only", async () => {
  const f = controllerFixture();
  await loaded(f, { view: "tools", items: [], starter_profile: STARTER_PROFILE }, () => f.map.setView("tools"));
  assert.doesNotMatch(f.list.textContent, /standing access/);
});

async function filesLoaded(f, payload = filesPayload()) {
  const pending = f.map.setView("files");
  await settle();
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-map?view=files&offset=0");
  f.gets.at(-1).resolve(payload);
  await pending;
  await settle();
}

const filesSection = (f, key) => byTag(f.list, "section").find(node => node.getAttribute("data-section") === key);
const sectionStatus = section => byClass(section, "access-files-status")[0];

test("the Files tab reads the files view without page controls", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [] });
  await filesLoaded(f);
  assert.equal(f.tabs.children[3].getAttribute("aria-selected"), "true");
  assert.match(f.intro.textContent, /What you share from your folder/);
  assert.equal(f.pager.children.length, 0);
  assert.equal(byTag(f.list, "section").length, 3);
});

test("Stop sharing asks first, posts nothing but the share id, and reports in its section", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [] });
  await filesLoaded(f);
  const mine = filesSection(f, "shared_by_you");
  const stop = buttonNamed(mine, "Stop sharing");
  stop.click();
  await settle();
  assert.equal(f.posts.length, 0, "the first click only asks for confirmation");
  assert.equal(stop.textContent, "Confirm stop sharing");
  assert.match(sectionStatus(mine).textContent, /stop sharing reports\/q1\.md with Dana/);
  stop.click();
  assert.equal(stop.textContent, "Stopping…");
  assert.equal(stop.getAttribute("aria-busy"), "true");
  await settle();
  assert.equal(f.posts[0].path, "/api/web-chat/access-map/shares/s-1/stop");
  assert.deepEqual(f.posts[0].body, {});
  f.posts[0].resolve({ status: "ok", share_id: "s-1", message: "Stopped sharing. It applies from the next action." });
  await settle();
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-map?view=files&offset=0", "the list is read again");
  f.gets.at(-1).resolve(filesPayload({ shared_by_you: [] }));
  await settle();
  const after = filesSection(f, "shared_by_you");
  assert.equal(sectionStatus(after).hidden, false);
  assert.equal(sectionStatus(after).textContent, "Stopped sharing. It applies from the next action.");
  assert.match(after.textContent, /You have not shared anything/);
});

test("a refused Stop sharing keeps the list, shows the reason and restores the button", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [] });
  await filesLoaded(f);
  const mine = filesSection(f, "shared_by_you");
  const stop = buttonNamed(mine, "Stop sharing");
  stop.click();
  stop.click();
  await settle();
  f.posts[0].reject(new Error("s-1 is not something you shared."));
  await settle();
  assert.equal(stop.textContent, "Stop sharing");
  assert.equal(stop.disabled, false);
  assert.ok(sectionStatus(mine).classList.contains("is-error"));
  assert.equal(sectionStatus(mine).textContent, "s-1 is not something you shared.");
  assert.equal(f.gets.length, 2, "a refusal does not reread the view");
});

test("Add folder sends the name, path and access, and keeps the values after a refusal", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [] });
  await filesLoaded(f);
  const folders = filesSection(f, "data_folders");
  const [form] = byTag(folders, "form");
  const [name, path] = byTag(form, "input");
  buttonNamed(form, "Add folder").click();
  await settle();
  assert.equal(f.posts.length, 0);
  assert.match(sectionStatus(folders).textContent, /Give the folder a name/);
  name.value = " finance ";
  buttonNamed(form, "Add folder").click();
  assert.match(sectionStatus(folders).textContent, /absolute path/);
  path.value = "/srv/finance";
  buttonNamed(form, "Add folder").click();
  await settle();
  assert.equal(f.posts[0].path, "/api/web-chat/access-map/data-folders");
  assert.deepEqual(f.posts[0].body, { name: "finance", path: "/srv/finance", writable: false });
  assert.equal(buttonNamed(form, "Adding…").getAttribute("aria-busy"), "true");
  f.posts[0].reject(new Error("/srv/finance is inside the instance folder, which agents must not reach this way."));
  await settle();
  assert.match(sectionStatus(folders).textContent, /inside the instance folder/);
  assert.equal(path.value, "/srv/finance", "entered values stay after a refusal");

  byClass(form, "access-files-read-only")[0].checked = false;
  buttonNamed(form, "Add folder").click();
  await settle();
  assert.deepEqual(f.posts[1].body, { name: "finance", path: "/srv/finance", writable: true });
  f.posts[1].resolve({ status: "ok", name: "finance", message: "Registered /srv/finance as data/finance." });
  await settle();
  f.gets.at(-1).resolve(filesPayload());
  await settle();
  assert.equal(sectionStatus(filesSection(f, "data_folders")).textContent, "Registered /srv/finance as data/finance.");
});

test("removing a data folder asks first and posts to its own route", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [] });
  await filesLoaded(f);
  const folders = filesSection(f, "data_folders");
  const remove = buttonNamed(folders, "Remove");
  remove.click();
  assert.equal(remove.textContent, "Confirm removal");
  assert.match(sectionStatus(folders).textContent, /remove data\/reports\. Agents can no longer reach \/srv\/shared\/reports/);
  remove.click();
  await settle();
  assert.equal(f.posts[0].path, "/api/web-chat/access-map/data-folders/reports/remove");
  assert.deepEqual(f.posts[0].body, {});
  f.posts[0].resolve({ status: "ok", message: "Removed data/reports. Agents can no longer reach /srv/shared/reports." });
  await settle();
  f.gets.at(-1).resolve(filesPayload({ data_folders: [] }));
  await settle();
  assert.equal(sectionStatus(filesSection(f, "data_folders")).textContent,
    "Removed data/reports. Agents can no longer reach /srv/shared/reports.");
});

test("a Files notice from the gateway replaces the sections", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [] });
  await filesLoaded(f, filesPayload({ notice: "You are not an active member of this team, so there is no access to show or change." }));
  assert.equal(byTag(f.list, "section").length, 0);
  assert.match(f.list.textContent, /not an active member/);
});

test("a tool's operations load when shown, a page at a time", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [] });
  buttonNamed(f.tabs, "By Tool").click();
  await settle();
  f.gets.at(-1).resolve({ status: "ok", enabled: true, view: "tools", total: 1, offset: 0, page_size: 20,
    items: [{ tool_id: "cli:google", display_name: "Google Workspace (gog)", operation_count: 576, links: [] }] });
  await settle();
  const toggle = buttonNamed(f.list, "Show operations");
  toggle.click();
  await settle();
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  assert.equal(toggle.textContent, "Hide operations");
  assert.equal(f.gets.at(-1).path, "/api/web-chat/access-map/tools/cli%3Agoogle/operations?offset=0");
  f.gets.at(-1).resolve({ status: "ok", tool_id: "cli:google", total: 576, offset: 0, page_size: 20, items: [
    { name: "command:gmail.send", level: "write", level_words: "read-and-change", description: "Send mail", unreviewed: false },
    { name: "odd.command", level: "full", level_words: "full", description: "", unreviewed: true },
  ] });
  await settle();
  const panel = byClass(f.list, "access-map-operations")[0];
  assert.equal(panel.hidden, false);
  assert.match(panel.textContent, /command:gmail\.sendNeeds read-and-changeSend mail/);
  assert.match(panel.textContent, /Not reviewed yet, so it needs full access\./);
  assert.equal(f.pages.at(-1).total, 576, "operations use the shared page controls");
  toggle.click();
  assert.equal(panel.hidden, true);
});

test("a failed refresh keeps the last list and says it may be out of date", async () => {
  const f = controllerFixture();
  await loaded(f, { items: [holder()] });
  const load = f.map.load();
  await settle();
  f.gets[1].reject(new Error("The request did not reach the gateway."));
  await load;
  assert.equal(f.list.children.length, 1);
  assert.ok(f.status.classList.contains("is-error"));
  assert.match(f.status.textContent, /may be out of date.*did not reach the gateway/);
});

test("sign-out clears the page and ignores a reply to an earlier read", async () => {
  const f = controllerFixture();
  const load = f.map.load();
  await settle();
  f.map.clear();
  f.gets[0].resolve({ status: "ok", enabled: true, items: [holder()], total: 1, offset: 0, page_size: 20 });
  await load;
  await settle();
  assert.equal(f.list.children.length, 0);
  assert.equal(f.tabs.children[0].getAttribute("aria-selected"), "true");
});

test("server and agent-written text stays text, even when it contains markup", () => {
  const document = fakeDocument();
  const card = renderAgentCard(document, holder({
    title: "<img src=x onerror=alert(1)>",
    grants: [grant({ reason: "<script>alert(1)</script>", target_name: "<b>tool</b>" })],
  }), { formatTime });
  assert.equal(byTag(card, "script").length, 0);
  assert.equal(byTag(card, "img").length, 0);
  assert.equal(byTag(card, "b").length, 0);
  assert.equal(byTag(card, "h5")[0].textContent, "<img src=x onerror=alert(1)>");
  assert.match(card.textContent, /<script>alert\(1\)<\/script>/);
});

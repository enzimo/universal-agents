/**
 * Access page in Web Chat Settings: the access map (access plan section 9.2,
 * ui/DESIGN.md "Access Page").
 *
 * Four views chosen by the gateway:
 * - By Agent Type starts with "TeamLead's standing access for your work": the
 *   starter profile choices (how much TeamLead may do for the signed-in
 *   person's own work without asking, which it can hand on to workers) with
 *   Save. Then one card per agent type. Its access is grouped by whose
 *   work it serves: "Standard for its role" (comes with the role, never
 *   removable here), "For your work", and, when the gateway lists them (it
 *   shows members only their own work and everyone's), "For <name>'s work"
 *   and "For everyone's work". Each grant row says where it came from ("You →
 *   TeamArchitect → WebResearchAgent"), until when and whether it can be
 *   handed on. Then the blocks. Actions: Remove, Add a block, Remove block.
 * - By Person: what each person has given and to whom. Members see only
 *   their own; the gateway applies that rule.
 * - By Tool: each tool, the access its operations need, and whose accounts it
 *   uses (never the account's details).
 * - Files: what the person shared from their own folder (Stop sharing), what
 *   others shared with them, and the team's data folders (Add folder and
 *   Remove for the people the gateway lets manage them).
 * The first three are one page of at most 20 items each; Files is one list.
 *
 * The gateway decides which actions to offer (`can_revoke`, `can_unblock`,
 * `can_block`, `block_tools`, `can_manage_folders`) and decides again when an
 * action is sent; a refusal is shown as the gateway wrote it. Every server
 * text is rendered with `textContent`, never as HTML. The view-model and
 * rendering functions are exported so node tests can check them without a
 * browser; `createAccessMap` connects them to the page.
 */

import {
  ACCESS_INBOX_API, CONFIRM_REMOVAL_LABEL,
  confirmOnSecondClick, localTimeFormatter, setCardMessage,
} from "./access_inbox.mjs";

export const ACCESS_MAP_API = "/api/web-chat/access-map";
export const ACCESS_MAP_LINK_KEY = "agent";
export const CHAIN_SEPARATOR = " → ";
export const STANDARD_ACCESS_LABEL = "Standard access";
export const BLOCKED_LABEL = "Blocked";
export const WAITING_LABEL = "Waiting for approval";
export const ADD_BLOCK_LABEL = "Add a block";
export const BLOCK_LABEL = "Block";
export const REMOVE_LABEL = "Remove";
export const REMOVE_BLOCK_LABEL = "Remove block";
export const STOP_SHARING_LABEL = "Stop sharing";
export const CONFIRM_STOP_SHARING_LABEL = "Confirm stop sharing";
export const ADD_FOLDER_LABEL = "Add folder";
export const SHOW_OPERATIONS_LABEL = "Show operations";
export const HIDE_OPERATIONS_LABEL = "Hide operations";
export const ACCESS_OFF_TITLE = "Access Control Is Off";

// By Agent Type group titles, in the order ui/DESIGN.md gives.
export const STANDARD_GROUP_TITLE = "Standard for its role";
export const YOUR_WORK_TITLE = "For your work";
export const EVERYONE_WORK_TITLE = "For everyone's work";
export const BLOCKS_TITLE = "Blocks";

// The By Agent Type view's starter profile control (access_control.starter_profiles).
export const STARTER_PROFILE_TITLE = "TeamLead's standing access for your work";
export const STARTER_PROFILE_API = `${ACCESS_MAP_API}/starter-profile`;
export const SAVE_LABEL = "Save";
export const IN_USE_LABEL = "In use";

// Files view section titles and the keys their status lines are kept under.
export const FILES_SECTIONS = Object.freeze({
  sharedByYou: Object.freeze({ key: "shared_by_you", title: "Shared by you" }),
  sharedWithYou: Object.freeze({ key: "shared_with_you", title: "Shared with you" }),
  dataFolders: Object.freeze({ key: "data_folders", title: "Data folders" }),
});

// The Block form's "Who" choices. The team is blocked with subject_kind
// "team" and an empty subject (the gateway's BLOCK_SUBJECT_PREFIXES).
export const BLOCK_WHO_HOLDER = "holder";
export const BLOCK_WHO_TEAM = "team";
export const TEAM_BLOCK_LABEL = "Everyone in the team";
export const TEAM_SUBJECT_KIND = "team";

export const ACCESS_VIEWS = Object.freeze([
  Object.freeze({ key: "agents", label: "By Agent Type" }),
  Object.freeze({ key: "people", label: "By Person" }),
  Object.freeze({ key: "tools", label: "By Tool" }),
  Object.freeze({ key: "files", label: "Files" }),
]);
export const FILES_VIEW = "files";
const PAGED_VIEWS = new Set(["agents", "people", "tools"]);

// "" means the block lasts until someone removes it; the gateway checks the range.
export const BLOCK_DURATIONS = Object.freeze([
  Object.freeze({ hours: "", label: "Until someone removes it" }),
  Object.freeze({ hours: "1", label: "1 hour" }),
  Object.freeze({ hours: "24", label: "1 day" }),
  Object.freeze({ hours: "168", label: "7 days" }),
  Object.freeze({ hours: "720", label: "30 days" }),
]);

const INTRO = Object.freeze({
  agents: "What each agent type can use, whose work that access serves, where it came from, and what is blocked. "
    + "Standard access comes with an agent's role and cannot be removed; a block can stop it. "
    + "Changes apply from the next action.",
  people_everyone: "What each person has given, and to whom.",
  people_own: "What you have given, and to whom. What other people give is visible only to them and to administrators.",
  tools: "Every tool, the access each of its operations needs, and whose accounts it uses. "
    + "Only who owns an account is shown, never its details.",
  files: "What you share from your folder, what other people share with you, and the data folders on this "
    + "computer that agents can be given. Changes apply from the next action.",
});

const EMPTY = Object.freeze({
  agents: "No agent has access to show.",
  people: "Nobody has given access or added a block yet.",
  tools: "The tool catalog has no tools.",
});

const FILES_EMPTY = Object.freeze({
  shared_by_you: "You have not shared anything. Use Share… on a file or folder in Workspace Files.",
  shared_with_you: "Nobody has shared anything with you.",
  data_folders: "No data folders are registered.",
});

const localTime = localTimeFormatter();

function text(value) {
  return value === undefined || value === null ? "" : String(value).trim();
}

/** Reads the agent type from a link such as `/webchat?view=access#agent=<type>`. */
export function parseAccessMapLink(hash) {
  const fragment = text(hash);
  const params = new URLSearchParams(fragment.startsWith("#") ? fragment.slice(1) : fragment);
  return text(params.get(ACCESS_MAP_LINK_KEY));
}

/** "You → TeamArchitect → WebResearchAgent" from the gateway's chain of names. */
export function chainText(chain) {
  return (Array.isArray(chain) ? chain : []).map(text).filter(Boolean).join(CHAIN_SEPARATOR);
}

export function accessMapPath(view, offset, agent) {
  const params = new URLSearchParams({ view: text(view) || "agents", offset: String(Math.max(0, Number(offset) || 0)) });
  if (text(agent)) params.set("agent", text(agent));
  return `${ACCESS_MAP_API}?${params}`;
}

export function operationsPath(toolId, offset) {
  return `${ACCESS_MAP_API}/tools/${encodeURIComponent(text(toolId))}/operations?offset=${Math.max(0, Number(offset) || 0)}`;
}

/** Removing access uses the access Inbox's route, the same one its notices use. */
export function revokePath(grantId) {
  return `${ACCESS_INBOX_API}/grants/${encodeURIComponent(text(grantId))}/revoke`;
}

export function unblockPath(blockId) {
  return `${ACCESS_MAP_API}/blocks/${encodeURIComponent(text(blockId))}/remove`;
}

export function stopSharingPath(shareId) {
  return `${ACCESS_MAP_API}/shares/${encodeURIComponent(text(shareId))}/stop`;
}

export const DATA_FOLDERS_API = `${ACCESS_MAP_API}/data-folders`;

export function removeDataFolderPath(name) {
  return `${DATA_FOLDERS_API}/${encodeURIComponent(text(name))}/remove`;
}

/**
 * Whether a Block form choice is a tool set. The gateway's target parser
 * (access_control.operator_commands.parse_target_text) reads an id with ":"
 * as a tool and one without as a tool set; the form follows the same rule.
 */
export function isToolSet(item) {
  const id = text(item && item.tool_id);
  return Boolean(id) && !id.includes(":");
}

/**
 * The grant groups of one agent card, in page order: "For your work", one
 * group per other person ("For <name>'s work", by name), then "For
 * everyone's work". "For your work" is always present so a member can see
 * that nothing serves their work yet; the others only when they have rows.
 */
export function grantGroups(grants) {
  const yours = [];
  const everyone = [];
  const people = new Map();
  (Array.isArray(grants) ? grants : []).forEach(grant => {
    if (grant.for_your_work === true) {
      yours.push(grant);
      return;
    }
    const personId = text(grant.for_person_id);
    if (!personId) {
      everyone.push(grant);
      return;
    }
    if (!people.has(personId)) {
      const words = text(grant.for_whose_work) || `${personId}'s work`;
      people.set(personId, { key: `person:${personId}`, title: `For ${words}`, grants: [] });
    }
    people.get(personId).grants.push(grant);
  });
  const others = [...people.values()].sort((left, right) => left.title.localeCompare(right.title));
  return [
    { key: "yours", title: YOUR_WORK_TITLE, grants: yours },
    ...others,
    ...(everyone.length ? [{ key: "everyone", title: EVERYONE_WORK_TITLE, grants: everyone }] : []),
  ];
}

/** "until a set time (ends Sep 30, 2026, 5:00 PM UTC)"; never a raw ISO time. */
export function lifetimeText(record, formatTime = localTime) {
  const words = text(record && record.lifetime_words);
  const ends = record && record.expires_at_utc ? formatTime(record.expires_at_utc) : "";
  return ends ? `${words || "until a set time"} (ends ${ends})` : words;
}

export function introText(view, payload) {
  if (view === "people") return payload && payload.sees_everyone === true ? INTRO.people_everyone : INTRO.people_own;
  return INTRO[view] || "";
}

/** One grant (access) row: what, how much, how long, handing on, and where it came from. */
export function grantRowModel(grant, { formatTime = localTime, recipient = false } = {}) {
  const pending = text(grant.status) === "pending";
  const details = [
    recipient ? { label: "To", value: text(grant.recipient) } : null,
    { label: "Access", value: text(grant.level_words) },
    { label: "How long", value: lifetimeText(grant, formatTime) },
    { label: "Handing on", value: text(grant.hand_on_words) },
    recipient ? null : { label: "Came from", value: chainText(grant.chain) },
    { label: "Why", value: text(grant.reason) },
    { label: "Note", value: text(grant.entry_note) },
  ].filter(row => row && row.value);
  return {
    id: text(grant.grant_id),
    title: text(grant.target_name) || text(grant.target),
    kind: text(grant.target_kind_words),
    badge: pending ? { label: WAITING_LABEL, tone: "waiting" } : null,
    details,
    canRevoke: !pending && grant.can_revoke === true,
  };
}

export function blockRowModel(block, { formatTime = localTime, recipient = false } = {}) {
  const added = block.created_at_utc ? formatTime(block.created_at_utc) : "";
  return {
    id: text(block.block_id),
    title: text(block.target_name) || text(block.target),
    kind: text(block.target_kind_words),
    badge: { label: BLOCKED_LABEL, tone: "blocked" },
    details: [
      recipient ? { label: "Who", value: text(block.recipient) } : null,
      { label: "How long", value: lifetimeText(block, formatTime) },
      { label: "Blocked by", value: text(block.blocked_by) },
      { label: "Added", value: added },
      { label: "Why", value: text(block.reason) },
    ].filter(row => row && row.value),
    canUnblock: block.can_unblock === true,
  };
}

let idSequence = 0;
function uniqueId(prefix) {
  idSequence += 1;
  return `${prefix}-${idSequence}`;
}

function element(document, tagName, className, value) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function badge(document, label, tone) {
  return element(document, "span", `access-inbox-status is-${tone}`, label);
}

function detailRow(document, label, value) {
  const row = element(document, "div", "permission-detail-row");
  row.append(element(document, "span", "permission-detail-label", label),
    element(document, "strong", "permission-detail-value", value));
  return row;
}

function messageLine(document) {
  const message = element(document, "p", "access-inbox-card-message");
  message.setAttribute("role", "status");
  message.setAttribute("aria-live", "polite");
  message.hidden = true;
  return message;
}

function emptyState(document, value) {
  return element(document, "p", "permission-empty-state", value);
}

function button(document, label, style) {
  const node = element(document, "button", `button ${style} permission-choice-button`, label);
  node.type = "button";
  return node;
}

function section(document, title, className = "") {
  const node = element(document, "section", `access-map-section${className ? ` ${className}` : ""}`);
  const heading = element(document, "h6", "permission-section-title", title);
  heading.id = uniqueId("access-map-section");
  node.setAttribute("aria-labelledby", heading.id);
  node.append(heading);
  return node;
}

function cardShell(document, title, badges, attributes) {
  const card = element(document, "article", "permission-request-card access-map-card");
  Object.entries(attributes).forEach(([name, value]) => card.setAttribute(name, value));
  const titleId = uniqueId("access-map-title");
  const heading = element(document, "div", "permission-request-heading");
  const copy = element(document, "div", "permission-request-heading-copy");
  const titleNode = element(document, "h5", "permission-operation", title);
  titleNode.id = titleId;
  copy.append(titleNode);
  heading.append(copy);
  const tags = badges.filter(Boolean);
  if (tags.length) {
    const group = element(document, "span", "access-notice-badges");
    group.append(...tags);
    heading.append(group);
  }
  card.setAttribute("aria-labelledby", titleId);
  card.append(heading);
  return { card, copy };
}

/**
 * One row inside a card. `action` is `{ label, style, handler }` or null;
 * the handler receives `{ button, group, message, row }`.
 */
function recordRow(document, model, attribute, action) {
  const row = element(document, "div", "access-map-row");
  if (model.id) row.setAttribute(attribute, model.id);
  const heading = element(document, "div", "access-map-row-heading");
  const title = element(document, "p", "access-map-row-title", model.title);
  if (model.kind) title.append(element(document, "span", "access-map-row-kind", ` (${model.kind})`));
  heading.append(title);
  if (model.badge) heading.append(badge(document, model.badge.label, model.badge.tone));
  row.append(heading);
  const details = element(document, "div", "access-review-details");
  model.details.forEach(detail => details.append(detailRow(document, detail.label, detail.value)));
  row.append(details);
  const message = messageLine(document);
  if (action) {
    const group = element(document, "div", "permission-choice-row access-map-row-actions");
    const control = button(document, action.label, action.style);
    control.addEventListener("click", () => {
      if (control.disabled || typeof action.handler !== "function") return;
      action.handler({ button: control, group, message, row });
    });
    group.append(control);
    row.append(group);
  }
  row.append(message);
  return row;
}

export function renderGrantRow(document, grant, { formatTime = localTime, recipient = false, onRemove } = {}) {
  const model = grantRowModel(grant, { formatTime, recipient });
  const action = model.canRevoke
    ? { label: REMOVE_LABEL, style: "danger", handler: parts => onRemove && onRemove({ ...parts, grant, model }) }
    : null;
  return recordRow(document, model, "data-grant-id", action);
}

export function renderBlockRow(document, block, { formatTime = localTime, recipient = false, onUnblock } = {}) {
  const model = blockRowModel(block, { formatTime, recipient });
  const action = model.canUnblock
    ? { label: REMOVE_BLOCK_LABEL, style: "secondary", handler: parts => onUnblock && onUnblock({ ...parts, block, model }) }
    : null;
  return recordRow(document, model, "data-block-id", action);
}

export function renderStandardRow(document, row, roleWords) {
  const role = text(roleWords).toLowerCase();
  return recordRow(document, {
    id: "",
    title: text(row.target_name) || text(row.target),
    kind: text(row.target_kind_words),
    badge: { label: STANDARD_ACCESS_LABEL, tone: "standard" },
    details: [
      { label: "Access", value: text(row.level_words) },
      { label: "Comes with", value: role ? `the ${role} role` : "the agent's role" },
    ],
  }, "data-standard", null);
}

/**
 * One agent type (or agent, role, or the whole team). Handlers:
 * `onRemove`, `onUnblock` (row parts plus the record) and `onAddBlock`
 * (`{ holder, button, card, formSlot, message }`).
 */
export function renderAgentCard(document, holder, { formatTime = localTime, onRemove, onUnblock, onAddBlock } = {}) {
  const roleWords = text(holder.role_words);
  const { card } = cardShell(document, text(holder.title) || text(holder.subject),
    [roleWords ? badge(document, roleWords, "role") : null], { "data-key": text(holder.key) });

  const standard = Array.isArray(holder.standard) ? holder.standard : [];
  if (standard.length) {
    const block = section(document, STANDARD_GROUP_TITLE, "is-standard");
    block.append(element(document, "p", "access-map-note",
      "Comes with the role. It cannot be removed here; a block can stop it."));
    block.append(...standard.map(row => renderStandardRow(document, row, roleWords)));
    card.append(block);
  }

  grantGroups(holder.grants).forEach(group => {
    const given = section(document, group.title, "is-given");
    given.setAttribute("data-group", group.key);
    given.append(...(group.grants.length
      ? group.grants.map(grant => renderGrantRow(document, grant, { formatTime, onRemove }))
      : [element(document, "p", "access-map-none", "No access is given for your work.")]));
    card.append(given);
  });

  const blocks = Array.isArray(holder.blocks) ? holder.blocks : [];
  if (blocks.length) {
    const blocked = section(document, BLOCKS_TITLE, "is-blocked");
    blocked.append(...blocks.map(item => renderBlockRow(document, item, { formatTime, onUnblock })));
    card.append(blocked);
  }

  const message = messageLine(document);
  const formSlot = element(document, "div", "access-map-form-slot");
  if (holder.can_block === true) {
    const actions = element(document, "div", "permission-choice-row access-map-card-actions");
    const add = button(document, ADD_BLOCK_LABEL, "secondary");
    add.addEventListener("click", () => {
      if (add.disabled || typeof onAddBlock !== "function") return;
      onAddBlock({ holder, button: add, card, formSlot, message });
    });
    actions.append(add);
    card.append(actions);
  }
  card.append(formSlot, message);
  return card;
}

export function renderPersonCard(document, person, { formatTime = localTime, onRemove, onUnblock } = {}) {
  const { card } = cardShell(document, text(person.title) || text(person.person_id), [],
    { "data-key": `person:${text(person.person_id)}` });
  const rows = Array.isArray(person.given) ? person.given : [];
  const given = section(document, "Given");
  given.append(...(rows.length
    ? rows.map(row => (text(row.kind) === "block"
      ? renderBlockRow(document, row, { formatTime, recipient: true, onUnblock })
      : renderGrantRow(document, row, { formatTime, recipient: true, onRemove })))
    : [element(document, "p", "access-map-none", person.is_you === true
      ? "You have not given any access or added any blocks."
      : "Nothing given.")]));
  card.append(given, messageLine(document));
  return card;
}

/** One tool. `onToggleOperations` receives `{ tool, button, panel }`. */
export function renderToolCard(document, tool, { formatTime = localTime, onToggleOperations } = {}) {
  const { card, copy } = cardShell(document, text(tool.display_name) || text(tool.tool_id),
    [text(tool.kind_words) ? badge(document, text(tool.kind_words), "role") : null],
    { "data-key": `tool:${text(tool.tool_id)}` });
  if (text(tool.description)) copy.append(element(document, "p", "access-map-description", text(tool.description)));
  const details = element(document, "div", "access-review-details");
  const sets = Array.isArray(tool.tool_sets) ? tool.tool_sets.map(text).filter(Boolean) : [];
  const unreviewed = Number(tool.unreviewed_count) || 0;
  [
    { label: "Tool ID", value: text(tool.tool_id) },
    { label: "Account", value: text(tool.account_words) },
    { label: "Tool sets", value: sets.join(", ") },
    { label: "Operations", value: text(tool.operations_words) },
    { label: "Not reviewed", value: unreviewed
      ? `${unreviewed} ${unreviewed === 1 ? "operation has" : "operations have"} not been reviewed yet, so they need full access.`
      : "" },
  ].filter(row => row.value).forEach(row => details.append(detailRow(document, row.label, row.value)));
  card.append(details);

  if (tool.uses_accounts === true) {
    const accounts = section(document, "Accounts");
    const links = Array.isArray(tool.links) ? tool.links : [];
    accounts.append(...(links.length
      ? links.map(link => {
        const row = element(document, "div", "access-map-row");
        row.setAttribute("data-link-id", text(link.link_id));
        const linked = link.created_at_utc ? formatTime(link.created_at_utc) : "";
        row.append(element(document, "p", "access-map-row-title", text(link.owner_words)),
          element(document, "p", "access-map-note",
            `Linked by ${text(link.linked_by) || "someone"}${linked ? ` on ${linked}` : ""}.`));
        return row;
      })
      : [element(document, "p", "access-map-none", "No account you can see is linked yet.")]));
    card.append(accounts);
  }

  if ((Number(tool.operation_count) || 0) > 0) {
    const panel = element(document, "div", "access-map-operations");
    panel.id = uniqueId("access-map-operations");
    panel.hidden = true;
    const toggle = button(document, SHOW_OPERATIONS_LABEL, "ghost");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", panel.id);
    toggle.addEventListener("click", () => {
      if (toggle.disabled || typeof onToggleOperations !== "function") return;
      onToggleOperations({ tool, button: toggle, panel });
    });
    const actions = element(document, "div", "permission-choice-row access-map-card-actions");
    actions.append(toggle);
    card.append(actions, panel);
  }
  return card;
}

export function renderOperationRow(document, operation) {
  const row = element(document, "div", "access-map-operation");
  row.append(element(document, "span", "access-map-operation-name", text(operation.name)),
    element(document, "span", "access-map-operation-level", `Needs ${text(operation.level_words)}`));
  const note = operation.unreviewed === true
    ? "Not reviewed yet, so it needs full access."
    : text(operation.description);
  if (note) row.append(element(document, "span", "access-map-note", note));
  return row;
}

function labeledField(document, label, control, help) {
  const field = element(document, "label", "memory-editor-field");
  field.append(element(document, "span", "memory-editor-label", label), control);
  if (help) field.append(element(document, "small", "memory-editor-help", help));
  return field;
}

/**
 * The inline Block form for one agent type or agent. `onSubmit` receives
 * `{ values, submit, group, message, form }`; `onCancel` runs on Cancel.
 * `allowTeam` adds a "Who" choice with "Everyone in the team" (for example to
 * turn internet access off for every agent); the gateway decides again
 * whether this person may block the whole team.
 */
export function renderBlockForm(document, holder, blockTools, { allowTeam = false, onSubmit, onCancel } = {}) {
  const form = element(document, "form", "access-map-block-form");
  const who = text(holder.title) || text(holder.subject);
  form.setAttribute("aria-label", `Block ${who}`);
  form.append(element(document, "p", "access-map-note",
    `A block stops ${who} from using the tool, whatever access it holds. It applies from the next action.`
    + (allowTeam ? ` Choose ${TEAM_BLOCK_LABEL} to stop every agent, for example to turn internet access off.` : "")));

  let whoChoice = null;
  if (allowTeam) {
    whoChoice = element(document, "select", "memory-editor-input access-map-block-who");
    [[BLOCK_WHO_HOLDER, who], [BLOCK_WHO_TEAM, TEAM_BLOCK_LABEL]].forEach(([value, label]) => {
      const option = element(document, "option", "", label);
      option.value = value;
      whoChoice.append(option);
    });
    whoChoice.value = BLOCK_WHO_HOLDER;
  }

  const items = Array.isArray(blockTools) ? blockTools : [];
  const tool = element(document, "select", "memory-editor-input access-map-block-tool");
  const choose = element(document, "option", "", "Choose a tool");
  choose.value = "";
  tool.append(choose, ...items.map(item => {
    // A tool set's name already says what it covers ("Internet access (all of it)").
    const label = isToolSet(item) ? text(item.display_name) : `${text(item.display_name)} (${text(item.tool_id)})`;
    const option = element(document, "option", "", label);
    option.value = text(item.tool_id);
    return option;
  }));
  const operation = element(document, "input", "memory-editor-input access-map-block-operation");
  operation.type = "text";
  operation.maxLength = 200;
  operation.placeholder = "As listed under By Tool";
  const duration = element(document, "select", "memory-editor-input access-map-block-duration");
  BLOCK_DURATIONS.forEach(item => {
    const option = element(document, "option", "", item.label);
    option.value = item.hours;
    duration.append(option);
  });
  const reason = element(document, "textarea", "memory-editor-text access-map-block-reason");
  reason.rows = 3;
  reason.maxLength = 2000;

  form.append(
    ...(whoChoice ? [labeledField(document, "Who", whoChoice, "")] : []),
    labeledField(document, "Tool", tool, ""),
    labeledField(document, "Operation (optional)", operation,
      "Leave empty to block the whole tool. By Tool lists each tool's operations. A tool set is blocked as a whole."),
    labeledField(document, "How long", duration, ""),
    labeledField(document, "Why", reason, "People who see this block read the reason."),
  );
  const message = messageLine(document);
  const group = element(document, "div", "permission-choice-row access-map-block-actions");
  const submit = element(document, "button", "button danger permission-choice-button", BLOCK_LABEL);
  submit.type = "submit";
  const cancel = button(document, "Cancel", "ghost");
  cancel.addEventListener("click", () => { if (!cancel.disabled && typeof onCancel === "function") onCancel(); });
  group.append(submit, cancel);
  form.append(group, message);
  form.addEventListener("submit", event => {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    if (submit.disabled || typeof onSubmit !== "function") return;
    const wholeTeam = Boolean(whoChoice) && whoChoice.value === BLOCK_WHO_TEAM;
    const values = {
      subject_kind: wholeTeam ? TEAM_SUBJECT_KIND : text(holder.subject_kind),
      subject: wholeTeam ? "" : text(holder.subject),
      tool_id: text(tool.value),
      operation: text(operation.value),
      hours: text(duration.value),
      reason: text(reason.value),
    };
    if (!values.tool_id) {
      setCardMessage(message, "Choose the tool to block.", true);
      tool.focus();
      return;
    }
    if (values.operation && isToolSet({ tool_id: values.tool_id })) {
      setCardMessage(message, "A tool set is blocked as a whole. Leave Operation empty, or choose one tool.", true);
      operation.focus();
      return;
    }
    if (!values.reason) {
      setCardMessage(message, "Say why you are blocking it; people read the reason.", true);
      reason.focus();
      return;
    }
    onSubmit({ values, submit, group, message, form });
  });
  return { form, fields: { who: whoChoice, tool, operation, duration, reason }, submit, cancel, message };
}

/* -- TeamLead's standing access ---------------------------------------------- */

/**
 * The starter profile control at the top of By Agent Type: how much TeamLead
 * may do for the signed-in person's own work without asking them each time
 * (TeamLead can hand that on to workers). `profile` is the gateway's
 * `starter_profile` (`current` and `choices`); `onSave` receives
 * `{ name, choice, submit, group, message }`. The gateway applies the choice
 * within the person's own authority and decides again when it is sent.
 */
export function renderStarterProfile(document, profile, { onSave } = {}) {
  const choices = Array.isArray(profile && profile.choices) ? profile.choices : [];
  const current = text(profile && profile.current);
  const node = element(document, "section", "access-map-profile");
  const heading = element(document, "h4", "settings-subsection-title is-first", STARTER_PROFILE_TITLE);
  heading.id = uniqueId("access-map-profile-title");
  node.setAttribute("aria-labelledby", heading.id);
  node.append(heading, element(document, "p", "settings-subsection-copy",
    "How much TeamLead may do for your own work without asking you each time. TeamLead can hand this on "
    + "to workers. Other people choose their own. Access you give by hand stays as it is."
    + (current ? "" : " None of these is in place for you now.")));

  const form = element(document, "form", "access-map-profile-form");
  const fieldset = element(document, "fieldset", "access-map-profile-choices");
  fieldset.append(element(document, "legend", "sr-only", STARTER_PROFILE_TITLE));
  const name = uniqueId("access-map-profile");
  const inputs = choices.map(choice => {
    const label = element(document, "label", "access-map-profile-choice");
    const input = element(document, "input", "access-map-profile-input");
    input.type = "radio";
    input.name = name;
    input.value = text(choice.name);
    input.checked = Boolean(current) && input.value === current;
    const copy = element(document, "span", "access-map-profile-copy");
    const title = element(document, "span", "access-map-profile-name", text(choice.display_name) || input.value);
    if (input.checked) title.append(badge(document, IN_USE_LABEL, "role"));
    copy.append(title, element(document, "span", "access-map-profile-description", text(choice.description)));
    label.append(input, copy);
    fieldset.append(label);
    return input;
  });
  const group = element(document, "div", "permission-choice-row access-map-profile-actions");
  const submit = element(document, "button", "button primary permission-choice-button", SAVE_LABEL);
  submit.type = "submit";
  group.append(submit);
  const message = messageLine(document);
  form.append(fieldset, group, message);
  form.addEventListener("submit", event => {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    if (submit.disabled || typeof onSave !== "function") return;
    const chosen = inputs.find(input => input.checked);
    if (!chosen) {
      setCardMessage(message, "Choose how much TeamLead may do for your work.", true);
      if (inputs[0]) inputs[0].focus();
      return;
    }
    const choice = choices.find(item => text(item.name) === chosen.value) || {};
    onSave({ name: chosen.value, choice, submit, group, message });
  });
  node.append(form);
  return { node, form, inputs, submit, message };
}

/* -- Files view ------------------------------------------------------------ */

function tableCell(document, row, label, content) {
  const cell = element(document, "td");
  cell.setAttribute("data-label", label);
  if (content && typeof content === "object") cell.append(content);
  else cell.textContent = text(content) || "—";
  row.append(cell);
  return cell;
}

function filesTable(document, labels) {
  const wrap = element(document, "div", "team-table-wrap access-files-table-wrap");
  const table = element(document, "table", "team-table access-files-table");
  const head = element(document, "thead");
  const headRow = element(document, "tr");
  labels.forEach(label => {
    const cell = element(document, "th", "", label);
    cell.setAttribute("scope", "col");
    headRow.append(cell);
  });
  head.append(headRow);
  const body = element(document, "tbody");
  table.append(head, body);
  wrap.append(table);
  return { wrap, body };
}

/** One share for the Files tables; "Until" is local and readable, never a raw ISO time. */
export function shareRowModel(share, { formatTime = localTime } = {}) {
  return {
    id: text(share.share_id),
    item: text(share.item) || text(share.target),
    target: text(share.target),
    person: text(share.person) || text(share.person_id),
    sharedBy: text(share.shared_by),
    level: text(share.level_words) || text(share.level),
    until: share.expires_at_utc ? formatTime(share.expires_at_utc) : "Until you stop sharing",
  };
}

/** One data folder: its name as agents see it (data/<name>) and whether agents may change it. */
export function dataFolderRowModel(folder) {
  return {
    name: text(folder.name),
    shownName: `data/${text(folder.name)}`,
    path: text(folder.path),
    access: folder.writable === true ? "Writable" : "Read-only",
  };
}

function filesSection(document, part, status) {
  const node = element(document, "section", "access-files-section");
  node.setAttribute("data-section", part.key);
  const heading = element(document, "h4", "settings-subsection-title", part.title);
  heading.id = uniqueId("access-files-title");
  node.setAttribute("aria-labelledby", heading.id);
  const line = element(document, "p", "settings-status access-files-status");
  line.setAttribute("role", "status");
  line.setAttribute("aria-live", "polite");
  const shown = status && status[part.key];
  line.textContent = shown ? shown.text : "";
  line.hidden = !shown;
  line.classList.toggle("is-error", Boolean(shown && shown.isError));
  node.append(heading, line);
  return { node, status: line };
}

function actionButton(document, label, style, handler) {
  const control = button(document, label, style);
  control.addEventListener("click", () => {
    if (!control.disabled && typeof handler === "function") handler(control);
  });
  return control;
}

/**
 * The Add folder form under the Data folders table. `onSubmit` receives
 * `{ values: { name, path, writable }, submit, group, form, fields }`;
 * `status` is the section's status line for problems found before sending.
 */
export function renderAddFolderForm(document, { status, onSubmit } = {}) {
  const form = element(document, "form", "access-files-form");
  form.setAttribute("aria-label", "Add a data folder");
  const name = element(document, "input", "memory-editor-input access-files-name");
  name.type = "text";
  name.maxLength = 64;
  name.placeholder = "reports";
  const path = element(document, "input", "memory-editor-input access-files-path");
  path.type = "text";
  path.maxLength = 4096;
  path.placeholder = "/srv/shared/reports";
  const readOnlyId = uniqueId("access-files-read-only");
  const readOnly = element(document, "input", "access-files-read-only");
  readOnly.type = "checkbox";
  readOnly.id = readOnlyId;
  readOnly.checked = true;
  const toggleLabel = element(document, "label", "access-files-toggle");
  toggleLabel.htmlFor = readOnlyId;
  const toggle = element(document, "span", "toggle");
  toggle.append(readOnly, element(document, "span"));
  toggleLabel.append(element(document, "span", "memory-editor-label", "Read-only"), toggle);

  const group = element(document, "div", "permission-choice-row access-files-form-actions");
  const submit = element(document, "button", "button primary permission-choice-button", ADD_FOLDER_LABEL);
  submit.type = "submit";
  group.append(submit);
  form.append(
    labeledField(document, "Name", name, "Agents see it as data/<name>."),
    labeledField(document, "Absolute path", path, "A folder on this computer, outside the workspace."),
    toggleLabel,
    group,
  );
  form.addEventListener("submit", event => {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    if (submit.disabled || typeof onSubmit !== "function") return;
    const values = { name: text(name.value), path: text(path.value), writable: readOnly.checked !== true };
    if (!values.name) {
      if (status) setCardMessage(status, "Give the folder a name. Agents see it as data/<name>.", true);
      name.focus();
      return;
    }
    if (!values.path) {
      if (status) setCardMessage(status, "Enter the folder's absolute path on this computer.", true);
      path.focus();
      return;
    }
    onSubmit({ values, submit, group, form, fields: { name, path, readOnly } });
  });
  return { form, fields: { name, path, readOnly }, submit };
}

/**
 * The Files view: Shared by you, Shared with you and Data folders. Handlers
 * receive the record, the pressed button and the section's status line:
 * `onStopSharing({ share, model, button, status })`,
 * `onRemoveFolder({ folder, model, button, status })` and
 * `onAddFolder({ values, submit, group, form, fields, status })`.
 * `status` maps a section key to `{ text, isError }` kept from an action.
 */
export function renderFilesView(document, payload, {
  formatTime = localTime, status = {}, onStopSharing, onRemoveFolder, onAddFolder,
} = {}) {
  const data = renderDataFoldersSection(document, payload, { status, onRemoveFolder, onAddFolder });
  // With access control off there are no shares: only the data folders apply.
  if (payload.enabled === false) return [data];
  const byYou = Array.isArray(payload.shared_by_you) ? payload.shared_by_you : [];
  const withYou = Array.isArray(payload.shared_with_you) ? payload.shared_with_you : [];

  const mine = filesSection(document, FILES_SECTIONS.sharedByYou, status);
  if (byYou.length) {
    const { wrap, body } = filesTable(document, ["Item", "Person", "Level", "Until", "Actions"]);
    byYou.forEach(share => {
      const model = shareRowModel(share, { formatTime });
      const row = element(document, "tr");
      row.setAttribute("data-key", `share:${model.id}`);
      tableCell(document, row, "Item", model.item);
      tableCell(document, row, "Person", model.person);
      tableCell(document, row, "Level", model.level);
      tableCell(document, row, "Until", model.until);
      tableCell(document, row, "Actions", actionButton(document, STOP_SHARING_LABEL, "danger",
        control => onStopSharing && onStopSharing({ share, model, button: control, status: mine.status })));
      body.append(row);
    });
    mine.node.append(wrap);
  } else {
    mine.node.append(emptyState(document, FILES_EMPTY.shared_by_you));
  }

  const theirs = filesSection(document, FILES_SECTIONS.sharedWithYou, status);
  if (withYou.length) {
    const { wrap, body } = filesTable(document, ["Item", "Owner", "Level"]);
    withYou.forEach(share => {
      const model = shareRowModel(share, { formatTime });
      const row = element(document, "tr");
      row.setAttribute("data-key", `shared:${model.id}`);
      // Agents working for this person find the item at this path.
      tableCell(document, row, "Item", model.target || model.item);
      tableCell(document, row, "Owner", model.sharedBy);
      tableCell(document, row, "Level", model.level);
      body.append(row);
    });
    theirs.node.append(wrap);
  } else {
    theirs.node.append(emptyState(document, FILES_EMPTY.shared_with_you));
  }

  return [mine.node, theirs.node, data];
}

/**
 * The Data folders section, in both authorization modes: with access control
 * on, agents reach a folder only with access to it; with it off, every agent
 * uses every registered folder at the access shown.
 */
export function renderDataFoldersSection(document, payload, { status = {}, onRemoveFolder, onAddFolder } = {}) {
  const folders = Array.isArray(payload.data_folders) ? payload.data_folders : [];
  const canManage = payload.can_manage_folders === true;
  const data = filesSection(document, FILES_SECTIONS.dataFolders, status);
  const reach = payload.enabled === false
    ? "With access control off, every agent can use them at the access shown."
    : "Agents reach one only when someone gives them access to it.";
  data.node.append(element(document, "p", "settings-subsection-copy",
    `Folders on this computer, outside the workspace. ${reach}`
    + (canManage ? "" : " Only administrators add or remove data folders.")));
  if (folders.length) {
    const labels = ["Name", "Path", "Access", ...(canManage ? ["Actions"] : [])];
    const { wrap, body } = filesTable(document, labels);
    folders.forEach(folder => {
      const model = dataFolderRowModel(folder);
      const row = element(document, "tr");
      row.setAttribute("data-key", `folder:${model.name}`);
      tableCell(document, row, "Name", model.shownName);
      tableCell(document, row, "Path", model.path);
      tableCell(document, row, "Access", model.access);
      if (canManage) {
        tableCell(document, row, "Actions", actionButton(document, REMOVE_LABEL, "danger",
          control => onRemoveFolder && onRemoveFolder({ folder, model, button: control, status: data.status })));
      }
      body.append(row);
    });
    data.node.append(wrap);
  } else {
    data.node.append(emptyState(document, FILES_EMPTY.data_folders));
  }
  if (canManage) {
    const add = renderAddFolderForm(document, {
      status: data.status,
      onSubmit: parts => onAddFolder && onAddFolder({ ...parts, status: data.status }),
    });
    data.node.append(add.form);
  }
  return data.node;
}

function findRecord(list, attribute, id) {
  return Array.from(list.children).find(child => child.getAttribute(attribute) === id) || null;
}

function recordKey(node, stop) {
  for (let current = node; current && current !== stop; current = current.parentNode) {
    const value = current.getAttribute && current.getAttribute("data-key");
    if (value) return value;
  }
  return "";
}

/**
 * Connects the Access page to its Settings panel.
 *
 * `get(path)` and `post(path, body)` resolve with the parsed JSON body and
 * reject with an Error whose message is shown to the person; the Settings
 * controls supply them with the signed-in session and the CSRF header.
 * `beginAction(button, busyLabel, group)` is the shared busy-button helper;
 * `beginRefresh()` marks the panel's Refresh button busy during each read and
 * returns its finish function; `paginate(options)` builds the shared page
 * controls (`pagination.js`).
 */
export function createAccessMap({
  container, get, post, beginAction, beginRefresh, failureMessage, paginate,
  browser = globalThis, formatTime = localTime,
}) {
  const document = browser.document;
  const schedule = (callback, delay) => browser.setTimeout(callback, delay);
  // `seesEveryone` comes from the gateway's By Person read and only chooses
  // whether the Block form offers the whole team; the gateway decides again.
  // `sectionStatus` keeps a Files action's result across the reread it causes.
  const state = {
    view: "agents", offset: 0, focusAgent: "", version: 0, loaded: false, payload: null, openForm: null,
    seesEveryone: false, sectionStatus: {},
  };

  const tabs = element(document, "nav", "permission-tabs access-map-tabs");
  tabs.setAttribute("aria-label", "Access views");
  tabs.setAttribute("role", "tablist");
  const list = element(document, "div", "access-map-list");
  list.id = uniqueId("access-map-list");
  list.setAttribute("role", "tabpanel");
  const tabButtons = ACCESS_VIEWS.map(view => {
    const tab = element(document, "button", "permission-tab", view.label);
    tab.type = "button";
    tab.id = uniqueId("access-map-tab");
    tab.setAttribute("role", "tab");
    tab.setAttribute("data-view", view.key);
    tab.setAttribute("aria-controls", list.id);
    tab.addEventListener("click", () => setView(view.key));
    return tab;
  });
  tabs.append(...tabButtons);
  const status = element(document, "p", "settings-status access-map-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.hidden = true;
  const intro = element(document, "p", "settings-subsection-copy access-map-intro");
  const off = element(document, "section", "access-map-off");
  off.hidden = true;
  const pager = element(document, "div", "access-map-pager");
  container.replaceChildren(tabs, status, intro, off, list, pager);

  function setStatus(value, isError) {
    status.textContent = value || "";
    status.hidden = !value;
    status.classList.toggle("is-error", Boolean(value && isError));
  }

  function renderTabs() {
    tabButtons.forEach(tab => {
      const active = tab.getAttribute("data-view") === state.view;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
      if (active) list.setAttribute("aria-labelledby", tab.id);
    });
  }

  function focusRecord(node) {
    node.tabIndex = -1;
    node.focus({ preventScroll: true });
  }

  function renderOff(payload) {
    tabs.hidden = true;
    intro.hidden = true;
    pager.replaceChildren();
    off.hidden = false;
    off.replaceChildren(element(document, "h4", "settings-subsection-title is-first", ACCESS_OFF_TITLE),
      element(document, "p", "settings-subsection-copy", text(payload.notice)));
    // Data folders apply in both modes, so they stay manageable below the explanation.
    list.hidden = state.view !== FILES_VIEW;
    if (state.view === FILES_VIEW) list.replaceChildren(...renderFilesView(document, payload, filesActions()));
  }

  function filesActions() {
    return {
      formatTime, status: state.sectionStatus,
      onStopSharing: stopSharing, onRemoveFolder: removeFolder, onAddFolder: addFolder,
    };
  }

  function renderCard(item) {
    if (state.view === "people") return renderPersonCard(document, item, { formatTime, onRemove: removeAccess, onUnblock: unblock });
    if (state.view === "tools") return renderToolCard(document, item, { formatTime, onToggleOperations: toggleOperations });
    return renderAgentCard(document, item, { formatTime, onRemove: removeAccess, onUnblock: unblock, onAddBlock: openBlockForm });
  }

  function renderPager(payload) {
    const total = Number(payload.total) || 0;
    const pageSize = Number(payload.page_size) || 20;
    if (!PAGED_VIEWS.has(state.view) || total <= pageSize || typeof paginate !== "function") {
      pager.replaceChildren();
      return;
    }
    pager.replaceChildren(paginate({
      total, pageSize, offset: Number(payload.offset) || 0, label: "Access pages",
      onPage: next => { state.offset = next; return load(); },
    }));
  }

  function applyFocus(payload, agent) {
    const key = text(payload.focus_key);
    const card = key ? findRecord(list, "data-key", key) : null;
    if (card) {
      card.classList.add("is-highlighted");
      browser.requestAnimationFrame(() => {
        if (!card.isConnected) return;
        card.scrollIntoView({ block: "center" });
        focusRecord(card);
      });
    } else if (agent && payload.focus_missing === true) {
      setStatus(`No access or blocks are listed for ${agent}.`, false);
    }
  }

  function render(agent) {
    const payload = state.payload || {};
    renderTabs();
    state.openForm = null;
    if (payload.enabled === false) {
      if (state.view !== FILES_VIEW) {
        // The other views have nothing to show without access control.
        state.view = FILES_VIEW;
        state.offset = 0;
        load();
        return;
      }
      renderOff(payload);
      return;
    }
    const active = document.activeElement;
    const focusedKey = active && container.contains(active) ? recordKey(active, container) : "";
    off.hidden = true;
    tabs.hidden = false;
    list.hidden = false;
    intro.hidden = false;
    intro.textContent = introText(state.view, payload);
    if (state.view === FILES_VIEW) {
      list.replaceChildren(...(text(payload.notice)
        ? [emptyState(document, text(payload.notice))]
        : renderFilesView(document, payload, filesActions())));
    } else {
      const items = Array.isArray(payload.items) ? payload.items : [];
      const cards = text(payload.notice) ? [] : items.map(renderCard);
      const profile = payload.starter_profile;
      const top = state.view === "agents" && !text(payload.notice) && profile && Array.isArray(profile.choices)
        && profile.choices.length ? [renderStarterProfile(document, profile, { onSave: saveStarterProfile }).node] : [];
      list.replaceChildren(...top,
        ...(cards.length ? cards : [emptyState(document, text(payload.notice) || EMPTY[state.view])]));
    }
    renderPager(payload);
    const refocus = focusedKey && findRecord(list, "data-key", focusedKey);
    if (refocus) focusRecord(refocus);
    applyFocus(payload, agent);
  }

  /** Reads the current page. `keepStatus` keeps an action's result visible. */
  function load({ keepStatus = false } = {}) {
    const version = ++state.version;
    const agent = state.view === "agents" ? state.focusAgent : "";
    state.focusAgent = "";
    if (!keepStatus) state.sectionStatus = {};
    const finish = typeof beginRefresh === "function" ? beginRefresh() : () => {};
    return Promise.resolve()
      .then(() => get(accessMapPath(state.view, state.offset, agent)))
      .then(payload => {
        if (version !== state.version) return;
        state.payload = payload || {};
        state.offset = Number(state.payload.offset) || 0;
        state.loaded = true;
        if (state.view === "people") state.seesEveryone = state.payload.sees_everyone === true;
        if (!keepStatus) setStatus("", false);
        render(agent);
      })
      .catch(error => {
        if (version !== state.version) return;
        const reason = failureMessage(error, "Access could not be loaded. Refresh and try again.");
        setStatus(state.loaded ? `The list below may be out of date because it could not be refreshed. ${reason}` : reason, true);
      })
      .finally(finish);
  }

  function setView(view) {
    if (!ACCESS_VIEWS.some(item => item.key === view)) return Promise.resolve();
    if (view !== state.view) {
      state.view = view;
      state.offset = 0;
      setStatus("", false);
    }
    renderTabs();
    return load();
  }

  function finishedAction(messageText) {
    setStatus(messageText, false);
    return load({ keepStatus: true });
  }

  function removeAccess({ grant, model, button: control, group, message }) {
    if (!confirmOnSecondClick(control, { idleLabel: REMOVE_LABEL, confirmLabel: CONFIRM_REMOVAL_LABEL, schedule })) {
      setCardMessage(message, "Select Confirm removal to remove all of this access. Access handed on from it is removed too.", false);
      return Promise.resolve();
    }
    const finish = beginAction(control, "Removing…", group);
    setCardMessage(message, "", false);
    return Promise.resolve()
      .then(() => post(revokePath(grant.grant_id), {}))
      .then(payload => {
        finish();
        // Remove takes the whole entry (every grant in grant_ids); the rest was handed on from it.
        const removed = Array.isArray(payload && payload.revoked_grant_ids) ? payload.revoked_grant_ids.length : 1;
        const own = Array.isArray(grant.grant_ids) && grant.grant_ids.length ? grant.grant_ids.length : 1;
        const handedOn = removed > own
          ? ` Access handed on from it was removed too (${removed - own} more).` : "";
        return finishedAction(`Access removed: ${model.title}.${handedOn} It applies from the next action.`);
      }, error => {
        finish();
        control.textContent = REMOVE_LABEL;
        setCardMessage(message, failureMessage(error, "The access was not removed. Refresh and try again."), true);
      });
  }

  function unblock({ block, button: control, group, message }) {
    if (!confirmOnSecondClick(control, { idleLabel: REMOVE_BLOCK_LABEL, confirmLabel: CONFIRM_REMOVAL_LABEL, schedule })) {
      setCardMessage(message, "Select Confirm removal to remove this block. The access it stopped applies again.", false);
      return Promise.resolve();
    }
    const finish = beginAction(control, "Removing…", group);
    setCardMessage(message, "", false);
    return Promise.resolve()
      .then(() => post(unblockPath(block.block_id), {}))
      .then(payload => {
        finish();
        return finishedAction(text(payload && payload.message) || "The block is removed.");
      }, error => {
        finish();
        control.textContent = REMOVE_BLOCK_LABEL;
        setCardMessage(message, failureMessage(error, "The block was not removed. Refresh and try again."), true);
      });
  }

  function saveStarterProfile({ name, choice, submit, group, message }) {
    const finish = beginAction(submit, "Saving…", group);
    setCardMessage(message, "", false);
    return Promise.resolve()
      .then(() => post(STARTER_PROFILE_API, { profile: name }))
      .then(payload => {
        finish();
        return finishedAction(text(payload && payload.message)
          || `${text(choice.display_name) || name} is in place for your work. It applies from the next action.`);
      }, error => {
        // The choice stays selected so the person can try again.
        finish();
        setCardMessage(message, failureMessage(error, "The choice was not saved. Refresh and try again."), true);
      });
  }

  /** A Files action finished: keep its result in its section and read the view again. */
  function finishedFilesAction(sectionKey, messageText) {
    state.sectionStatus = { [sectionKey]: { text: messageText, isError: false } };
    return load({ keepStatus: true });
  }

  /**
   * A two-step Files action on one table row: the first click asks, the
   * second sends. Refusals stay in the section's status line and restore the
   * button; success rereads the view with the gateway's message.
   */
  function confirmedFilesAction({ control, status, sectionKey, idleLabel, confirmLabel, ask, busyLabel, send, fallback }) {
    if (!confirmOnSecondClick(control, { idleLabel, confirmLabel, schedule })) {
      setCardMessage(status, ask, false);
      return Promise.resolve();
    }
    const finish = beginAction(control, busyLabel, null);
    setCardMessage(status, "", false);
    return Promise.resolve()
      .then(send)
      .then(payload => {
        finish();
        return finishedFilesAction(sectionKey, text(payload && payload.message) || fallback.done);
      }, error => {
        finish();
        control.textContent = idleLabel;
        setCardMessage(status, failureMessage(error, fallback.failed), true);
      });
  }

  function stopSharing({ share, model, button: control, status }) {
    return confirmedFilesAction({
      control, status, sectionKey: FILES_SECTIONS.sharedByYou.key,
      idleLabel: STOP_SHARING_LABEL, confirmLabel: CONFIRM_STOP_SHARING_LABEL, busyLabel: "Stopping…",
      ask: `Select ${CONFIRM_STOP_SHARING_LABEL} to stop sharing ${model.item} with ${model.person}. `
        + "Their agents lose it from their next action.",
      send: () => post(stopSharingPath(share.share_id), {}),
      fallback: { done: "Stopped sharing. It applies from the next action.",
        failed: "Sharing was not stopped. Refresh and try again." },
    });
  }

  function removeFolder({ folder, model, button: control, status }) {
    return confirmedFilesAction({
      control, status, sectionKey: FILES_SECTIONS.dataFolders.key,
      idleLabel: REMOVE_LABEL, confirmLabel: CONFIRM_REMOVAL_LABEL, busyLabel: "Removing…",
      ask: `Select ${CONFIRM_REMOVAL_LABEL} to remove ${model.shownName}. Agents can no longer reach ${model.path}; `
        + "the folder itself stays on this computer.",
      send: () => post(removeDataFolderPath(folder.name), {}),
      fallback: { done: `Removed ${model.shownName}.`, failed: "The data folder was not removed. Refresh and try again." },
    });
  }

  function addFolder({ values, submit, group, status }) {
    const finish = beginAction(submit, "Adding…", group);
    setCardMessage(status, "", false);
    return Promise.resolve()
      .then(() => post(DATA_FOLDERS_API, values))
      .then(payload => {
        finish();
        return finishedFilesAction(FILES_SECTIONS.dataFolders.key,
          text(payload && payload.message) || `Registered data/${values.name}.`);
      }, error => {
        // The entered values stay so the person can correct and resend them.
        finish();
        setCardMessage(status, failureMessage(error, "The data folder was not added. Check the form and try again."), true);
      });
  }

  function closeBlockForm({ restoreFocus = true } = {}) {
    const open = state.openForm;
    state.openForm = null;
    if (!open) return;
    open.formSlot.replaceChildren();
    open.opener.disabled = false;
    if (restoreFocus && open.opener.isConnected) open.opener.focus();
  }

  function openBlockForm({ holder, button: opener, formSlot }) {
    if (state.openForm) closeBlockForm({ restoreFocus: false });
    const blockTools = state.payload && Array.isArray(state.payload.block_tools) ? state.payload.block_tools : [];
    // The gateway offers tool sets only to administrators, who may also block the whole team.
    const allowTeam = state.seesEveryone || blockTools.some(isToolSet);
    const view = renderBlockForm(document, holder, blockTools, {
      allowTeam,
      onCancel: () => closeBlockForm(),
      onSubmit: ({ values, submit, group, message }) => {
        const finish = beginAction(submit, "Blocking…", group);
        setCardMessage(message, "", false);
        return Promise.resolve()
          .then(() => post(`${ACCESS_MAP_API}/blocks`, values))
          .then(payload => {
            finish();
            closeBlockForm({ restoreFocus: false });
            return finishedAction(text(payload && payload.message) || "Blocked. It applies from the next action.");
          }, error => {
            // The entered values stay so the person can correct and resend them.
            finish();
            setCardMessage(message, failureMessage(error, "The block was not added. Check the form and try again."), true);
          });
      },
    });
    formSlot.replaceChildren(view.form);
    opener.disabled = true;
    state.openForm = { formSlot, opener };
    (view.fields.who || view.fields.tool).focus();
    return view;
  }

  function toggleOperations({ tool, button: toggle, panel }) {
    const expanded = toggle.getAttribute("aria-expanded") === "true";
    if (expanded) {
      toggle.setAttribute("aria-expanded", "false");
      toggle.textContent = SHOW_OPERATIONS_LABEL;
      panel.hidden = true;
      return Promise.resolve();
    }
    toggle.setAttribute("aria-expanded", "true");
    toggle.textContent = HIDE_OPERATIONS_LABEL;
    panel.hidden = false;
    return loadOperations(tool, panel, 0);
  }

  function loadOperations(tool, panel, offset) {
    const version = state.version;
    return Promise.resolve()
      .then(() => get(operationsPath(tool.tool_id, offset)))
      .then(payload => {
        if (version !== state.version || !panel.isConnected) return;
        const items = Array.isArray(payload && payload.items) ? payload.items : [];
        const rows = element(document, "div", "access-map-operation-list");
        rows.append(...(items.length ? items.map(item => renderOperationRow(document, item))
          : [emptyState(document, "No operations are listed for this tool.")]));
        const parts = [rows];
        const total = Number(payload && payload.total) || 0;
        const pageSize = Number(payload && payload.page_size) || 20;
        if (total > pageSize && typeof paginate === "function") {
          parts.push(paginate({
            total, pageSize, offset: Number(payload.offset) || 0,
            label: `${text(tool.display_name) || "Tool"} operation pages`,
            onPage: next => loadOperations(tool, panel, next),
          }));
        }
        panel.replaceChildren(...parts);
      })
      .catch(error => {
        if (version !== state.version || !panel.isConnected) return;
        const message = element(document, "p", "access-inbox-card-message is-error",
          failureMessage(error, "The operations could not be loaded. Try again."));
        message.setAttribute("role", "status");
        panel.replaceChildren(message);
      });
  }

  renderTabs();

  return {
    load,
    setView,
    /** Opens By Agent Type at one agent type (Access links: `#agent=<type>`). */
    open(agentType) {
      state.view = "agents";
      state.offset = 0;
      state.focusAgent = text(agentType);
      setStatus("", false);
      renderTabs();
      return load();
    },
    /** Sign-out: forget everything and ignore replies to earlier reads. */
    clear() {
      state.version += 1;
      Object.assign(state, { view: "agents", offset: 0, focusAgent: "", loaded: false, payload: null, openForm: null,
        seesEveryone: false, sectionStatus: {} });
      setStatus("", false);
      intro.textContent = "";
      off.hidden = true;
      off.replaceChildren();
      tabs.hidden = false;
      list.hidden = false;
      list.replaceChildren();
      pager.replaceChildren();
      renderTabs();
    },
  };
}

/**
 * Settings → Team in Web Chat: live team members, defined agent classes, MCP
 * connections, hosted MCP servers and installed procedures.
 *
 * - Members follow the team's live status (team_activity.mjs, about every two
 *   seconds) while the Team tab is open: the same copy the sidebar activity
 *   mesh and the Tasks view show, so the page reads it once for all three.
 *   An update whose content did not change only updates "Last seen", so
 *   tables, open skill lists and scroll positions stay where they are
 *   (ui/DESIGN.md). Everything else on the tab (agent classes, MCP servers,
 *   procedures) is read when the tab opens and on Refresh.
 * - MCP connection rows show Approve and Reject only when the gateway says
 *   this person can decide that pending connection (`can_decide`). The
 *   decision is bound to the row's record digest. Approving installs the
 *   server and, with authorization on, lets the agent types it names use all
 *   of its tools; who else may use it is set on the Access page (grants on
 *   `mcp:<name>`).
 * - Installed procedures: an installed procedure is a fixed operation a
 *   developer built into the runtime. Agents may propose using one;
 *   administrators add or reject those proposals here. Adding one gives no
 *   agent access by itself: every use is still checked like any other tool.
 *   The card heading, effect and risk come from the runtime's registry; the
 *   agent's own words (the name it gave and its reason) are labeled as the
 *   agent's. Only the proposal id goes back to the gateway, never a handler,
 *   schema or risk.
 *
 * All server and agent text is set with textContent, never as HTML.
 */

import {
  LIST_PAGE_SIZE, element, failureMessage, isForbidden, localTimeFormatter, setStatusLine, text,
} from "./settings_support.mjs";
import { activitySignature } from "./team_activity.mjs";

export const TEAM_API = "/api/web-chat/team";
export const MCP_CONNECTIONS_API = "/api/web-chat/mcp-connections";
export const PROCEDURE_PROPOSALS_API = "/api/web-chat/procedures/proposals";
export const INSTALLED_PROCEDURES_API = "/api/web-chat/procedures/installed";

export const PROCEDURES_TITLE = "Installed procedures";
export const PROCEDURES_INTRO = "An installed procedure is a fixed operation a developer built into the runtime. "
  + "Agents may propose using one, and administrators add or reject those proposals here. "
  + "Adding one gives no agent access by itself: each use is checked like any other tool.";
export const PROCEDURES_ADMIN_ONLY = "Only administrators review procedure proposals.";
export const INSTALLED_ADMIN_ONLY = "Only administrators can see installed procedures.";
export const PROCEDURES_NEED_CONVERSATION = "Open a conversation to review procedure proposals.";
export const UNREGISTERED_PROCEDURE_TITLE = "Procedure not built into the runtime";

// Fixed text the gateway expects with an administrator's decision.
const INSTALL_EXPLANATION = "Administrator reviewed the trusted handler registration.";
const REJECT_REASON = "administrator_rejected";
const TECHNICAL_MODE = "technical";

const MCP_LIFECYCLE_LABELS = Object.freeze({
  pending: "Waiting for approval",
  active: "Installed",
  disabled: "Disabled",
  removed: "Removed",
  failed: "Unavailable",
});

const MCP_CREDENTIAL_LABELS = Object.freeze({
  none: "None",
  operator_configured: "Set by administrator",
  linked_account: "Uses a linked account",
});

const PROCEDURE_STATUS_LABELS = Object.freeze({
  pending_registration: "Not built into the runtime yet",
  ready_for_admin: "Ready for review",
  installed: "Added",
  rejected: "Rejected",
});
const CLOSED_PROCEDURE_STATUSES = new Set(["installed", "rejected"]);

const RISK_LABELS = Object.freeze({ low: "Low", medium: "Medium", high: "High", critical: "Critical" });

const localTime = localTimeFormatter();

let idSequence = 0;
function uniqueId(prefix) {
  idSequence += 1;
  return `${prefix}-${idSequence}`;
}

function list(value) {
  return Array.isArray(value) ? value : [];
}

/** "Just now", "12 seconds ago", "3 minutes ago", then a local date and time. */
export function relativeLastSeen(value, now = Date.now(), formatTime = localTime) {
  const timestamp = new Date(text(value)).getTime();
  if (!Number.isFinite(timestamp)) return "Unknown";
  const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
  if (seconds < 5) return "Just now";
  if (seconds < 60) return `${seconds} seconds ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return minutes === 1 ? "1 minute ago" : `${minutes} minutes ago`;
  return formatTime(value);
}

/** What a presence update compares: everything shown except "Last seen". */
export function stableTeamSignature(members, connections, hostedServers, definitions) {
  return JSON.stringify([activitySignature(members), connections, hostedServers, definitions]);
}

/** Approve and Reject appear only for a pending connection this person may decide. */
export function canDecideConnection(connection) {
  return text(connection && connection.lifecycle) === "pending" && connection.can_decide === true;
}

/** Installed says only that the server is set up; grants on its tool decide who may use it. */
export function connectionStatusLabel(connection) {
  const lifecycle = text(connection.lifecycle) || "unknown";
  return MCP_LIFECYCLE_LABELS[lifecycle] || "Unavailable";
}

/** The sentence shown after an MCP decision was saved. */
export function connectionDecisionText(connection, decision, payload, presentationMode) {
  const serverName = text(connection.connection_name) || text(connection.connection_id) || "this MCP server";
  const serverLabel = /mcp server$/i.test(serverName) ? serverName : `${serverName} MCP server`;
  const approved = decision === "approve";
  const sentence = `${serverLabel} was ${approved ? "approved" : "rejected"}.`;
  if (presentationMode !== TECHNICAL_MODE) return sentence;
  const response = payload || {};
  return `${sentence} Request digest: ${text(response.request_digest) || text(connection.record_digest) || "unknown"}.`
    + ` Decision digest: ${text(response.response_digest) || "unknown"}.`;
}

export function procedureStatusLabel(status) {
  return PROCEDURE_STATUS_LABELS[text(status)] || "Unknown";
}

export function riskLabel(risk) {
  return RISK_LABELS[text(risk)] || "Unknown";
}

/** Whether a proposal still takes a decision. `Add` also needs registered, matching code. */
export function procedureChoices(proposal) {
  if (!proposal || CLOSED_PROCEDURE_STATUSES.has(text(proposal.status))) return [];
  return proposal.administrator_installable === true ? ["reject", "install"] : ["reject"];
}

function tableCell(document, row, label, childOrText, className) {
  const cell = element(document, "td", className || "");
  cell.setAttribute("data-label", label);
  if (childOrText && typeof childOrText === "object") cell.append(childOrText);
  else cell.textContent = text(childOrText) || "—";
  row.append(cell);
  return cell;
}

function table(document, wrapClass, tableClass, labels) {
  const wrap = element(document, "div", wrapClass);
  const node = element(document, "table", tableClass);
  const head = element(document, "thead");
  const headRow = element(document, "tr");
  labels.forEach(label => headRow.append(element(document, "th", "", label)));
  head.append(headRow);
  const body = element(document, "tbody");
  node.append(head, body);
  wrap.append(node);
  return { wrap, body };
}

function emptyState(document, value) {
  return element(document, "p", "permission-empty-state", value);
}

function nameWithNote(document, name, note) {
  const node = element(document, "span", "team-agent-name", name);
  if (note) node.append(element(document, "small", "team-lead-label", note));
  return node;
}

function definitionList(document, rows) {
  const details = element(document, "dl", "permission-capability-details");
  rows.filter(row => text(row[1])).forEach(([label, value, className]) => {
    details.append(element(document, "dt", "", label), element(document, "dd", className || "", text(value)));
  });
  return details;
}

/**
 * One procedure proposal card. `onAction({ proposal, action, button, group })`
 * runs when Add (`install`) or Reject (`reject`) is clicked.
 */
export function renderProcedureProposal(document, proposal, { formatTime = localTime, onAction } = {}) {
  const trusted = proposal.trusted_handler && typeof proposal.trusted_handler === "object"
    ? proposal.trusted_handler : null;
  const status = text(proposal.status) || "unknown";
  const agent = text(proposal.proposer_named_agent_type) || "the agent";
  const card = element(document, "article", "permission-capability-card procedure-proposal-card");
  card.setAttribute("data-proposal-id", text(proposal.proposal_id));

  const heading = element(document, "div", "permission-capability-heading");
  heading.append(
    element(document, "strong", "", trusted ? text(trusted.display_name) || "Procedure" : UNREGISTERED_PROCEDURE_TITLE),
    element(document, "span", `permission-capability-state is-${status}`, procedureStatusLabel(status)),
  );
  card.append(heading);

  // Registry facts first; the agent's chosen name is only a labeled detail.
  card.append(definitionList(document, [
    ["What it does", trusted && trusted.description],
    ["Effect", trusted && trusted.impact_description],
    ["Risk", trusted && riskLabel(trusted.risk)],
    ["Built into the runtime", proposal.handler_registered === true
      ? "Yes" : "No. A developer must add it to the runtime before it can be added here."],
    ["Name the agent gave", proposal.display_name],
    ["Proposed", proposal.created_at_utc && formatTime(proposal.created_at_utc)],
  ]));

  const why = element(document, "section", "permission-agent-context");
  why.append(element(document, "h6", "permission-section-title", `Why (from ${agent})`));
  [proposal.purpose, proposal.justification].map(text).filter(Boolean)
    .forEach(value => why.append(element(document, "p", "", value)));
  card.append(why);

  const technical = element(document, "details", "permission-technical-details");
  technical.append(element(document, "summary", "", "Technical details"), definitionList(document, [
    ["Proposal ID", proposal.proposal_id, "is-monospace"],
    ["Requested handler", proposal.requested_handler_id, "is-monospace"],
    ["Access level needed to use it", trusted && trusted.level],
    ["Handler descriptor digest", trusted && trusted.handler_descriptor_digest, "is-monospace"],
    ["Input schema digest", trusted && trusted.input_schema_digest, "is-monospace"],
  ]));
  card.append(technical);

  const choices = procedureChoices(proposal);
  if (choices.length) {
    const group = element(document, "div", "permission-capability-actions");
    choices.forEach(action => {
      const install = action === "install";
      const button = element(document, "button", install ? "button primary" : "button ghost", install ? "Add" : "Reject");
      button.type = "button";
      button.setAttribute("data-action", action);
      button.addEventListener("click", () => {
        if (!button.disabled && typeof onAction === "function") onAction({ proposal, action, button, group });
      });
      group.append(button);
    });
    card.append(group);
  }
  return card;
}

/** Installed procedures as a compact table read from each entry's `definition`. */
export function renderInstalledProcedures(document, entries, { formatTime = localTime } = {}) {
  const { wrap, body } = table(document, "permission-grants-table-wrap", "permission-grants-table procedures-table",
    ["Procedure", "Risk", "Team lead may pass it on", "Built-in code", "Added", "Definition ID"]);
  entries.forEach(entry => {
    const definition = entry && entry.definition && typeof entry.definition === "object" ? entry.definition : {};
    const row = element(document, "tr");
    row.setAttribute("data-definition-id", text(definition.definition_id));
    tableCell(document, row, "Procedure", nameWithNote(document,
      text(definition.display_name) || "Unnamed procedure", text(definition.handler_id)));
    tableCell(document, row, "Risk", riskLabel(definition.risk));
    tableCell(document, row, "Team lead may pass it on", definition.manager_delegable === true ? "Yes" : "No");
    tableCell(document, row, "Built-in code",
      entry && entry.handler_registration_valid === true ? "Matches the runtime" : "Does not match the runtime");
    tableCell(document, row, "Added", formatTime(definition.installed_at_utc));
    tableCell(document, row, "Definition ID", definition.definition_id, "is-monospace");
    body.append(row);
  });
  return wrap;
}

/**
 * Connects the Team panel. `client` is the Settings client from
 * settings_support.mjs; `getSessionKey()` returns the open conversation.
 * Settings calls `activate()` when the Team tab opens and `deactivate()` when
 * it closes or another tab opens.
 */
export function createTeamControls({
  panel, content, proceduresContainer, status, refreshButton, client, getSessionKey, teamActivity,
  browser = globalThis, formatTime = localTime, now = () => Date.now(),
}) {
  const document = browser.document;
  // `rest`: the last full read's other tables, redrawn with each members update.
  const state = {
    version: 0, unsubscribe: null, loadPromise: null, signature: "", decisionBusy: false, rest: null,
    presentationMode: "friendly", expandedSkills: Object.create(null), lastSeenCells: new Map(),
  };
  // `confirmedSession`: the conversation whose gateway session the server has
  // confirmed for procedure reads (a new thread gets one on the first read).
  const procedures = { version: 0, offset: 0, busy: false, loaded: false, confirmedSession: "" };

  const proceduresTitle = element(document, "h4", "settings-subsection-title", PROCEDURES_TITLE);
  proceduresTitle.id = uniqueId("settings-procedures-title");
  proceduresContainer.setAttribute("aria-labelledby", proceduresTitle.id);
  const proceduresStatus = element(document, "p", "settings-status procedures-status");
  proceduresStatus.setAttribute("role", "status");
  proceduresStatus.setAttribute("aria-live", "polite");
  proceduresStatus.hidden = true;
  const proceduresBody = element(document, "div", "procedures-content");
  proceduresContainer.replaceChildren(proceduresTitle,
    element(document, "p", "settings-subsection-copy", PROCEDURES_INTRO), proceduresStatus, proceduresBody);

  const setStatus = (message, isError) => setStatusLine(status, message, isError);
  const setProceduresStatus = (message, isError) => setStatusLine(proceduresStatus, message, isError);

  function skillSummary(skills, agentId) {
    const names = list(skills);
    if (!names.length) return element(document, "span", "team-skill-empty", "No skills reported");
    const details = element(document, "details", "team-skills-details");
    // Keep a skill list open across automatic refreshes until the agent leaves.
    details.open = Boolean(state.expandedSkills[agentId]);
    details.addEventListener("toggle", () => {
      if (details.open) state.expandedSkills[agentId] = true;
      else delete state.expandedSkills[agentId];
    });
    details.append(element(document, "summary", "", `${names.length} ${names.length === 1 ? "skill" : "skills"}`));
    const items = element(document, "ul", "team-skills-list");
    names.forEach(name => items.append(element(document, "li", "", String(name))));
    details.append(items);
    return details;
  }

  function membersSection(members) {
    const nodes = [element(document, "h4", "settings-subsection-title is-first", "Team Members")];
    state.lastSeenCells = new Map();
    if (!members.length) {
      nodes.push(emptyState(document,
        "No live team presence is available yet. Agents appear after the gateway receives their status heartbeat."));
      return nodes;
    }
    const { wrap, body } = table(document, "team-table-wrap", "team-table team-members-table",
      ["Agent", "Type", "Status", "Skills", "Last seen"]);
    members.forEach(member => {
      const agentId = text(member.agent_id) || text(member.name);
      const row = element(document, "tr");
      row.setAttribute("data-agent-id", agentId);
      tableCell(document, row, "Agent", nameWithNote(document,
        text(member.name) || text(member.agent_id) || "Unknown", member.is_team_lead ? "Team lead" : ""));
      tableCell(document, row, "Type", member.named_agent_type);
      const presence = member.status || {};
      tableCell(document, row, "Status", element(document, "span",
        `team-presence is-${text(presence.tone) || "neutral"}`, text(presence.label) || "Unavailable"));
      tableCell(document, row, "Skills", skillSummary(member.available_skills, agentId));
      const lastSeen = tableCell(document, row, "Last seen",
        relativeLastSeen(member.last_seen_at, now(), formatTime), "team-last-seen");
      lastSeen.setAttribute("data-agent-id", agentId);
      state.lastSeenCells.set(agentId, lastSeen);
      body.append(row);
    });
    nodes.push(wrap);
    return nodes;
  }

  function classesSection(definitions) {
    const nodes = [element(document, "h4", "settings-subsection-title", "Defined agent classes")];
    if (!definitions.length) {
      nodes.push(emptyState(document, "No agent classes are defined."));
      return nodes;
    }
    const { wrap, body } = table(document, "team-table-wrap", "team-table team-classes-table", ["Class", "Description"]);
    definitions.forEach(definition => {
      const row = element(document, "tr");
      tableCell(document, row, "Class", nameWithNote(document, text(definition.display_name), text(definition.named_agent_type)));
      tableCell(document, row, "Description", definition.description);
      body.append(row);
    });
    nodes.push(wrap);
    return nodes;
  }

  function connectionActions(connection) {
    const group = element(document, "div", "mcp-connection-actions");
    if (!canDecideConnection(connection)) {
      group.append(element(document, "span", "team-skill-empty", "No action"));
      return group;
    }
    [["approve", "button primary mcp-connection-action", "Approve"],
      ["reject", "button ghost mcp-connection-action", "Reject"]].forEach(([decision, className, label]) => {
      const button = element(document, "button", className, label);
      button.type = "button";
      button.setAttribute("data-decision", decision);
      button.addEventListener("click", () => decideConnection(connection, decision, button, group));
      group.append(button);
    });
    return group;
  }

  function connectionsSection(connections) {
    const nodes = [
      element(document, "h4", "settings-subsection-title", "MCP Connections"),
      element(document, "p", "settings-subsection-copy",
        "Approving an MCP server installs it and lets the agent types it names use all of its tools. The Access page shows who else may use it."),
    ];
    if (!connections.length) {
      nodes.push(emptyState(document, "No MCP connections are installed or awaiting review."));
      return nodes;
    }
    const { wrap, body } = table(document, "team-table-wrap", "team-table mcp-connections-table",
      ["Connection", "Status", "Transport", "Agents", "Tools", "Credentials", "Actions"]);
    connections.forEach(connection => {
      const row = element(document, "tr");
      row.setAttribute("data-connection-id", text(connection.connection_id));
      tableCell(document, row, "Connection", nameWithNote(document,
        text(connection.connection_name) || text(connection.connection_id) || "Unknown", text(connection.source) || "unknown source"));
      tableCell(document, row, "Status", connectionStatusLabel(connection));
      tableCell(document, row, "Transport", connection.legacy_sse ? "Legacy SSE" : connection.transport);
      // The agent types the server was proposed for; grants decide who may use it.
      tableCell(document, row, "Agents", nameWithNote(document,
        list(connection.target_agent_types).join(", ") || "None requested",
        `Who may use it: grants on ${text(connection.tool_id) || "its tool"} (Access page)`));
      tableCell(document, row, "Tools", connection.operation_count ? String(connection.operation_count) : "Tool list not checked");
      tableCell(document, row, "Credentials", MCP_CREDENTIAL_LABELS[text(connection.credential_mode)] || "Unknown");
      tableCell(document, row, "Actions", connectionActions(connection));
      body.append(row);
    });
    nodes.push(wrap);
    return nodes;
  }

  function hostedSection(hostedServers) {
    const nodes = [element(document, "h4", "settings-subsection-title", "Hosted MCP Servers")];
    if (!hostedServers.length) {
      nodes.push(emptyState(document, "No declarative MCP servers are hosted by this team."));
      return nodes;
    }
    const { wrap, body } = table(document, "team-table-wrap", "team-table",
      ["Server", "Status", "Agents", "Tools", "Internal listener"]);
    hostedServers.forEach(server => {
      const row = element(document, "tr");
      tableCell(document, row, "Server", server.server_name);
      tableCell(document, row, "Status", server.lifecycle);
      tableCell(document, row, "Agents", list(server.target_agent_types).join(", ") || "None");
      tableCell(document, row, "Tools", list(server.tools).join(", ") || "None");
      tableCell(document, row, "Internal listener", `${text(server.bind_host)}:${text(server.port)}`);
      body.append(row);
    });
    nodes.push(wrap);
    return nodes;
  }

  function renderTeam(members, connections, hostedServers, definitions) {
    const current = new Set(members.map(member => text(member.agent_id) || text(member.name)));
    Object.keys(state.expandedSkills).forEach(agentId => {
      if (!current.has(agentId)) delete state.expandedSkills[agentId];
    });
    const previousScrollTop = content.scrollTop;
    const previousPanelScrollTop = panel.scrollTop;
    // Replace all tables at once so a refresh never shows a half-built panel.
    content.replaceChildren(...membersSection(members), ...classesSection(definitions),
      ...connectionsSection(connections), ...hostedSection(hostedServers));
    content.scrollTop = previousScrollTop;
    panel.scrollTop = previousPanelScrollTop;
    state.signature = stableTeamSignature(members, connections, hostedServers, definitions);
  }

  function updateLastSeen(members) {
    members.forEach(member => {
      const cell = state.lastSeenCells.get(text(member.agent_id) || text(member.name));
      if (cell) cell.textContent = relativeLastSeen(member.last_seen_at, now(), formatTime);
    });
  }

  /** Shows members with the other tables from the last full read; unchanged content only moves "Last seen". */
  function showMembers(members) {
    const { connections, hostedServers, definitions } = state.rest;
    const signature = stableTeamSignature(members, connections, hostedServers, definitions);
    if (signature === state.signature && content.firstChild) updateLastSeen(members);
    else renderTeam(members, connections, hostedServers, definitions);
  }

  /** A team status update while the tab is open (none until the first full read; none during a decision). */
  function receiveMembers(members) {
    if (!members || !state.rest || state.decisionBusy || state.loadPromise) return;
    showMembers(members);
  }

  /** Reads the team. Resolves true when the tables show the current state. */
  function load() {
    if (state.decisionBusy || state.loadPromise) return state.loadPromise || Promise.resolve(false);
    const version = state.version;
    const finish = client.beginRefresh(refreshButton);
    state.loadPromise = client.get(`${TEAM_API}?session_key=${encodeURIComponent(getSessionKey())}`,
      "The team could not be loaded because the gateway returned no explanation. Refresh and try again.")
      .then(payload => {
        if (version !== state.version) return false;
        state.presentationMode = text(payload.authorization_presentation_mode) || state.presentationMode;
        state.rest = {
          definitions: list(payload.definitions),
          connections: list(payload.mcp_connections),
          hostedServers: list(payload.hosted_mcp_servers),
        };
        showMembers(list(payload.members));
        setStatus("", false);
        return true;
      })
      .catch(error => {
        if (version !== state.version) return false;
        setStatus(failureMessage(error, "The team could not be loaded. Refresh and try again."), true);
        return false;
      })
      .finally(() => {
        state.loadPromise = null;
        finish();
      });
    return state.loadPromise;
  }

  function decideConnection(connection, decision, button, group) {
    if (state.decisionBusy) return Promise.resolve();
    const sessionKey = getSessionKey();
    if (!sessionKey) {
      setStatus("Open a conversation before deciding MCP connections.", true);
      return Promise.resolve();
    }
    state.decisionBusy = true;
    const version = state.version;
    const finish = client.beginAction(button, decision === "approve" ? "Approving…" : "Rejecting…", group);
    setStatus("", false);
    let outcome = "";
    return client.ensureSessionContext(sessionKey)
      .then(() => client.post(`${MCP_CONNECTIONS_API}/${encodeURIComponent(text(connection.connection_id))}/decision`, {
        session_key: sessionKey,
        decision,
        expected_record_digest: text(connection.record_digest),
      }, "The MCP server decision was not saved because the gateway returned no explanation. Refresh and try again."))
      .then(payload => { outcome = connectionDecisionText(connection, decision, payload, state.presentationMode); })
      .catch(error => {
        if (version === state.version) {
          setStatus(failureMessage(error, "The MCP server decision was not saved. Refresh and try again."), true);
        }
      })
      .finally(() => {
        state.decisionBusy = false;
        finish();
      })
      .then(() => {
        if (!outcome || version !== state.version) return undefined;
        return load().then(loaded => {
          if (version !== state.version) return;
          setStatus(loaded ? outcome : `${outcome} The list below may be out of date. Select Refresh to update it.`, !loaded);
        });
      });
  }

  function proceduresPage(offset, hasMore, total) {
    if (!offset && !hasMore) return [];
    return [client.paginate({ offset, total, pageSize: LIST_PAGE_SIZE, onPage: next => loadProcedures(next) })];
  }

  function proposalsNodes(result) {
    const nodes = [element(document, "h5", "permission-section-title", "Proposals")];
    if (result.status !== "fulfilled") return [...nodes, emptyState(document, PROCEDURES_ADMIN_ONLY)];
    const proposals = list(result.value && result.value.proposals);
    if (!proposals.length) return [...nodes, emptyState(document, "No procedure proposals are waiting for review.")];
    return [...nodes, ...proposals.map(proposal =>
      renderProcedureProposal(document, proposal, { formatTime, onAction: decideProcedure }))];
  }

  function installedNodes(result) {
    const nodes = [element(document, "h5", "permission-section-title", "Installed")];
    if (result.status !== "fulfilled") return [...nodes, emptyState(document, INSTALLED_ADMIN_ONLY)];
    const definitions = list(result.value && result.value.definitions);
    if (!definitions.length) return [...nodes, emptyState(document, "No procedures are installed.")];
    return [...nodes, renderInstalledProcedures(document, definitions, { formatTime })];
  }

  /**
   * Procedure reviews act as the person of this conversation's gateway
   * session, so the first read for a conversation confirms that session (a
   * thread that has not sent a message yet gets one). A refusal there is
   * reported for both lists, like a refused read.
   */
  function confirmSession(sessionKey) {
    if (procedures.confirmedSession === sessionKey) return Promise.resolve();
    return client.ensureSessionContext(sessionKey).then(() => { procedures.confirmedSession = sessionKey; });
  }

  /**
   * Reads both procedure lists for one page. The two lists page together, so
   * the page count follows the longer list. A 403 means this person is not an
   * administrator: the section says so instead of showing an error.
   */
  function loadProcedures(offset = 0) {
    const version = ++procedures.version;
    const sessionKey = getSessionKey();
    if (!sessionKey) {
      procedures.loaded = false;
      setProceduresStatus("", false);
      proceduresBody.replaceChildren(emptyState(document, PROCEDURES_NEED_CONVERSATION));
      return Promise.resolve(false);
    }
    const query = `?session_key=${encodeURIComponent(sessionKey)}&offset=${offset}&limit=${LIST_PAGE_SIZE}`;
    const finish = client.beginRefresh(refreshButton);
    const reads = () => Promise.allSettled([
      client.get(PROCEDURE_PROPOSALS_API + query,
        "Procedure proposals could not be loaded because the gateway returned no explanation. Refresh and try again."),
      client.get(INSTALLED_PROCEDURES_API + query,
        "Installed procedures could not be loaded because the gateway returned no explanation. Refresh and try again."),
    ]);
    const refused = error => [{ status: "rejected", reason: error }, { status: "rejected", reason: error }];
    return confirmSession(sessionKey).then(reads, refused).then(results => {
      if (version !== procedures.version) return false;
      const failed = results.find(result => result.status === "rejected" && !isForbidden(result.reason));
      if (failed) {
        const reason = failureMessage(failed.reason, "Installed procedures could not be loaded. Refresh and try again.");
        setProceduresStatus(procedures.loaded ? `The list below may be out of date. ${reason}` : reason, true);
        return false;
      }
      procedures.offset = offset;
      procedures.loaded = true;
      setProceduresStatus("", false);
      if (results.every(result => result.status === "rejected")) {
        proceduresBody.replaceChildren(emptyState(document, PROCEDURES_ADMIN_ONLY));
        return true;
      }
      const pages = results.filter(result => result.status === "fulfilled").map(result => result.value || {});
      const hasMore = pages.some(page => Boolean(page.has_more));
      const total = Math.max(0, ...pages.map(page => Number(page.total) || 0));
      proceduresBody.replaceChildren(...proposalsNodes(results[0]), ...installedNodes(results[1]),
        ...proceduresPage(offset, hasMore, total));
      return true;
    }).finally(finish);
  }

  function decideProcedure({ proposal, action, button, group }) {
    if (procedures.busy) return Promise.resolve();
    const sessionKey = getSessionKey();
    if (!sessionKey) {
      setProceduresStatus("Open a conversation before deciding procedure proposals.", true);
      return Promise.resolve();
    }
    procedures.busy = true;
    const version = state.version;
    const install = action === "install";
    const finish = client.beginAction(button, install ? "Adding…" : "Rejecting…", group);
    setProceduresStatus("", false);
    const body = install
      ? { session_key: sessionKey, explanation: INSTALL_EXPLANATION }
      : { session_key: sessionKey, reason: REJECT_REASON };
    let saved = false;
    return client.ensureSessionContext(sessionKey)
      .then(() => client.post(
        `${PROCEDURE_PROPOSALS_API}/${encodeURIComponent(text(proposal.proposal_id))}/${install ? "install" : "reject"}`,
        body, "The proposal was not updated because the gateway returned no explanation. Refresh and try again."))
      .then(() => { saved = true; })
      .catch(error => {
        if (version === state.version) {
          setProceduresStatus(failureMessage(error, "The proposal was not updated. Refresh and try again."), true);
        }
      })
      .finally(() => {
        procedures.busy = false;
        finish();
      })
      .then(() => {
        if (!saved || version !== state.version) return undefined;
        const outcome = install ? "Procedure added." : "Proposal rejected.";
        return loadProcedures(procedures.offset).then(loaded => {
          if (version !== state.version) return;
          setProceduresStatus(loaded ? outcome : `${outcome} The list below may be out of date. Select Refresh to update it.`, !loaded);
        });
      });
  }

  function refresh() {
    return Promise.all([load(), loadProcedures(0)]);
  }

  refreshButton.addEventListener("click", () => { refresh(); });

  return {
    load,
    loadProcedures,
    /** Team tab opened: read everything, then follow the team's live status. */
    activate() {
      const loaded = refresh();
      if (!state.unsubscribe && teamActivity) state.unsubscribe = teamActivity.subscribe(receiveMembers);
      return loaded;
    },
    /** Team tab closed or hidden: stop following the live status. */
    deactivate() {
      if (state.unsubscribe) state.unsubscribe();
      state.unsubscribe = null;
    },
    resetStatus() {
      setStatus("", false);
      setProceduresStatus("", false);
    },
    /** Sign-out: forget everything and ignore replies to earlier reads. */
    clear() {
      this.deactivate();
      state.version += 1;
      procedures.version += 1;
      Object.assign(state, { signature: "", decisionBusy: false, presentationMode: "friendly", rest: null,
        expandedSkills: Object.create(null), lastSeenCells: new Map() });
      Object.assign(procedures, { offset: 0, busy: false, loaded: false, confirmedSession: "" });
      content.replaceChildren();
      proceduresBody.replaceChildren();
      this.resetStatus();
    },
  };
}

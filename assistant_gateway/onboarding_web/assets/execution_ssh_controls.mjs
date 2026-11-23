/**
 * Settings > Tasks > Brokered SSH: administrator view of enrolled SSH hosts
 * and delegated SSH sessions with one-click Revoke / Terminate
 * (assistant_gateway.execution_ssh).
 *
 * Shown only when the gateway says this person may manage brokered SSH
 * (`can_manage`, decided by PersonChangeCheck on the server), never from
 * client state. All server text is set with textContent, never as HTML.
 */

import {
  element,
  failureMessage,
  isForbidden,
  localTimeFormatter,
  setStatusLine,
  text,
} from "./settings_support.mjs";

export const EXECUTION_SSH_API = "/api/web-chat/execution-ssh";
export const EXECUTION_SSH_TITLE = "Brokered SSH Access";
export const EXECUTION_SSH_INTRO = "Enrolled SSH hosts and delegated agent sessions for this team. "
  + "Revoking a host or terminating a session stops active remote commands and closes live connections immediately.";
export const EXECUTION_SSH_DISABLED_NOTE = "Brokered SSH is currently turned off in instance configuration "
  + "(execution_control.enabled or execution_control.ssh.enabled is false). Existing records can still be reviewed and revoked.";
export const EXECUTION_SSH_SECRET_BOUNDARY_NOTE = "Broker secret recipes run broker-owned scripts and do not grant unrestricted root commands "
  + "unless Broker sudo (unrestricted root) is also shown. Any secret sent to a remote host can still be observed by whoever controls that host, "
  + "and shell, typing, or file-upload access on the same remote account can compromise later broker sudo or secret-recipe runs; rotate bootstrap passwords afterward.";
export const CAPABILITY_LABELS = Object.freeze({
  commands: "Commands",
  typing: "Typing",
  password_bootstrap: "Password bootstrap",
  broker_sudo: "Broker sudo (unrestricted root)",
  secret_recipes: "Secret recipes (no unrestricted root sudo)",
  workspace_transfer: "Workspace transfer",
});

const LOAD_FAILED = "Brokered SSH access could not be loaded because the gateway returned no explanation. "
  + "Select Refresh to try again.";
const REVOKE_TARGET_FAILED = "The enrolled SSH host was not revoked because the gateway returned no explanation. "
  + "Refresh and try again.";
const REVOKE_SESSION_FAILED = "The SSH session was not terminated because the gateway returned no explanation. "
  + "Refresh and try again.";

let idSequence = 0;
function uniqueId(prefix) {
  idSequence += 1;
  return `${prefix}-${idSequence}`;
}

export function statusLabel(value) {
  const raw = text(value).toLowerCase();
  if (!raw) return "Unknown";
  return raw.charAt(0).toUpperCase() + raw.slice(1).replaceAll("_", " ");
}

export function statusBadgeClass(value) {
  const raw = text(value).toLowerCase();
  if (raw === "active" || raw === "open" || raw === "connecting" || raw === "pending") {
    return "execution-ssh-badge is-active";
  }
  if (raw === "revoked") return "execution-ssh-badge is-revoked";
  if (raw === "expired") return "execution-ssh-badge is-expired";
  return "execution-ssh-badge is-inactive";
}

export function capabilityLabel(name) {
  const key = text(name);
  return Object.hasOwn(CAPABILITY_LABELS, key)
    ? CAPABILITY_LABELS[key]
    : key.replaceAll("_", " ");
}

function normalizedCapabilities(session) {
  return Array.isArray(session && session.capabilities)
    ? session.capabilities.map(text).filter(Boolean)
    : [];
}

function normalizedSecretRecipes(session) {
  if (!Array.isArray(session && session.allowed_secret_recipes)) return [];
  return [...new Set(session.allowed_secret_recipes.map(text).filter(Boolean))];
}

export function sessionCapabilityLabels(session) {
  const caps = normalizedCapabilities(session);
  const recipes = normalizedSecretRecipes(session);
  const hasBrokerSudo = caps.includes("broker_sudo");
  const labels = [];
  let recipeLabelAdded = false;
  const formatRecipesBadge = () => {
    if (recipes.length) {
      return hasBrokerSudo
        ? `Secret recipes: ${recipes.join(", ")}`
        : `Secret recipes: ${recipes.join(", ")} (no unrestricted root sudo)`;
    }
    return hasBrokerSudo ? "Secret recipes" : CAPABILITY_LABELS.secret_recipes;
  };
  caps.forEach(cap => {
    if (cap === "secret_recipes") {
      if (!recipeLabelAdded) {
        labels.push(formatRecipesBadge());
        recipeLabelAdded = true;
      }
      return;
    }
    labels.push(capabilityLabel(cap));
  });
  if (!recipeLabelAdded && recipes.length > 0) {
    labels.push(formatRecipesBadge());
  }
  return labels;
}

function sessionUsesRemoteHostSecrets(session) {
  const caps = normalizedCapabilities(session);
  const recipes = normalizedSecretRecipes(session);
  return (
    recipes.length > 0
    || caps.includes("secret_recipes")
    || caps.includes("broker_sudo")
    || caps.includes("password_bootstrap")
  );
}

function tableHead(document, headers) {
  const thead = element(document, "thead");
  const row = element(document, "tr");
  headers.forEach(label => row.append(element(document, "th", "", label)));
  thead.append(row);
  return thead;
}

// Narrow-screen .team-table layout uses a 2-column grid (::before data-label + 1 value child).
function tableCell(document, label, ...children) {
  const cell = element(document, "td");
  cell.setAttribute("data-label", label);
  if (children.length === 1) {
    cell.append(children[0]);
  } else if (children.length > 1) {
    const content = element(document, "div", "execution-ssh-cell-content");
    content.append(...children);
    cell.append(content);
  }
  return cell;
}

/**
 * Connects the Brokered SSH section inside Settings > Tasks.
 * `client` is the Settings client from settings_support.mjs.
 */
export function createExecutionSSHControls({ container, client, browser = globalThis }) {
  const document = browser.document;
  const formatTime = localTimeFormatter();
  const state = { epoch: 0, version: 0, actionInFlight: false };

  const header = element(document, "div", "execution-ssh-header");
  const title = element(document, "h4", "settings-subsection-title is-first", EXECUTION_SSH_TITLE);
  title.id = uniqueId("settings-execution-ssh-title");
  container.setAttribute("aria-labelledby", title.id);
  const refreshButton = element(document, "button", "button ghost settings-refresh-button execution-ssh-refresh", "Refresh");
  refreshButton.type = "button";
  header.append(title, refreshButton);

  const status = element(document, "p", "settings-status execution-ssh-status");
  status.setAttribute("aria-live", "polite");
  status.hidden = true;
  const body = element(document, "div", "execution-ssh-content");

  container.hidden = true;
  container.replaceChildren(
    header,
    element(document, "p", "settings-subsection-copy", EXECUTION_SSH_INTRO),
    status,
    body,
  );

  // ui/DESIGN.md: a failure is an alert, a success a neutral status.
  function setStatus(message, isError) {
    status.setAttribute("role", message && isError ? "alert" : "status");
    setStatusLine(status, message, isError);
  }

  function renderTargets(targets) {
    const section = element(document, "div", "execution-ssh-group");
    section.append(element(document, "h5", "execution-ssh-group-title", "Enrolled Hosts"));
    if (!targets.length) {
      section.append(element(document, "p", "permission-empty-state", "No enrolled SSH hosts."));
      return section;
    }
    const wrap = element(document, "div", "team-table-wrap");
    const table = element(document, "table", "team-table execution-ssh-table");
    table.append(tableHead(document, ["Host", "Host Key", "Credentials & Lifecycle", "Status", "Action"]));
    const tbody = element(document, "tbody");
    targets.forEach(target => {
      const row = element(document, "tr");
      row.setAttribute("data-target-id", text(target.target_id));

      const hostLines = [
        element(document, "div", "execution-ssh-primary", `${text(target.username)}@${text(target.host)}:${Number(target.port)}`),
        element(document, "div", "execution-ssh-meta", `${text(target.platform)} · ${text(target.target_id)}`),
      ];
      if (text(target.reason)) {
        hostLines.push(element(document, "div", "execution-ssh-meta", text(target.reason)));
      }
      const hostCell = tableCell(document, "Host", ...hostLines);

      const keyCell = tableCell(
        document,
        "Host Key",
        element(document, "div", "execution-ssh-mono", text(target.host_key_fingerprint)),
        element(document, "div", "execution-ssh-meta", text(target.host_key_algorithm)),
      );

      const lifecycleText = text(target.lifecycle) === "temporary" && text(target.expires_at_utc)
        ? `Temporary · expires ${formatTime(target.expires_at_utc)}`
        : statusLabel(target.lifecycle);
      const credLines = [
        element(document, "div", "execution-ssh-primary", `Key: ${text(target.credential_key)}`),
        element(document, "div", "execution-ssh-meta", lifecycleText),
      ];
      if (text(target.bootstrap_credential_key)) {
        const bootstrapNote = text(target.password_bootstrap_completed_at_utc)
          ? `Bootstrap: ${text(target.bootstrap_credential_key)} (completed)`
          : `Bootstrap: ${text(target.bootstrap_credential_key)}`;
        credLines.push(element(document, "div", "execution-ssh-meta", bootstrapNote));
      }
      const credCell = tableCell(document, "Credentials & Lifecycle", ...credLines);

      const statusLines = [element(document, "span", statusBadgeClass(target.status), statusLabel(target.status))];
      if (text(target.revoked_at_utc)) {
        const who = text(target.revoked_by) ? ` by ${text(target.revoked_by)}` : "";
        statusLines.push(element(document, "div", "execution-ssh-meta", `Revoked ${formatTime(target.revoked_at_utc)}${who}`));
      }
      const statusCell = tableCell(document, "Status", ...statusLines);

      let actionContent;
      if (target.can_revoke === true) {
        const button = element(document, "button", "button danger mcp-connection-action execution-ssh-revoke-target", "Revoke");
        button.type = "button";
        button.setAttribute("aria-label", `Revoke enrolled host ${text(target.username)}@${text(target.host)}:${Number(target.port)}`);
        button.addEventListener("click", () => { revokeTarget(target, button); });
        actionContent = button;
      } else {
        actionContent = element(document, "span", "execution-ssh-meta", "—");
      }
      const actionCell = tableCell(document, "Action", actionContent);

      row.append(hostCell, keyCell, credCell, statusCell, actionCell);
      tbody.append(row);
    });
    table.append(tbody);
    wrap.append(table);
    section.append(wrap);
    return section;
  }

  function renderSessions(sessions) {
    const section = element(document, "div", "execution-ssh-group");
    section.append(element(document, "h5", "execution-ssh-group-title", "Delegated Sessions"));
    if (!sessions.length) {
      section.append(element(document, "p", "permission-empty-state", "No delegated SSH sessions."));
      return section;
    }
    if (sessions.some(sessionUsesRemoteHostSecrets)) {
      section.append(element(document, "p", "execution-ssh-note", EXECUTION_SSH_SECRET_BOUNDARY_NOTE));
    }
    const wrap = element(document, "div", "team-table-wrap");
    const table = element(document, "table", "team-table execution-ssh-table");
    table.append(tableHead(document, ["Session", "Delegation & Capabilities", "Expires", "Status", "Action"]));
    const tbody = element(document, "tbody");
    sessions.forEach(session => {
      const row = element(document, "tr");
      row.setAttribute("data-session-id", text(session.session_id));

      const sessionCell = tableCell(
        document,
        "Session",
        element(document, "div", "execution-ssh-primary", text(session.target_label) || `${text(session.username)}@${text(session.host)}:${Number(session.port)}`),
        element(document, "div", "execution-ssh-meta", `${text(session.session_id)} · ${text(session.platform)}`),
      );

      const agentName = text(session.delegated_agent_type) || text(session.owner_agent_type);
      const agentText = agentName ? `Agent: ${agentName}` : "Agent: Unknown";
      const taskText = text(session.delegated_task_id) ? ` · Task: ${text(session.delegated_task_id)}` : "";
      const capLabels = sessionCapabilityLabels(session);
      const capRow = element(document, "div", "execution-ssh-capabilities");
      if (capLabels.length) {
        capLabels.forEach(label => capRow.append(element(document, "span", "execution-ssh-cap", label)));
      } else {
        capRow.append(element(document, "span", "execution-ssh-meta", "Connect only"));
      }
      const delegationCell = tableCell(
        document,
        "Delegation & Capabilities",
        element(document, "div", "execution-ssh-primary", `${agentText}${taskText}`),
        capRow,
      );

      const expiresCell = tableCell(
        document,
        "Expires",
        element(document, "div", "execution-ssh-meta", text(session.expires_at_utc) ? formatTime(session.expires_at_utc) : "No expiry"),
      );

      const statusLines = [element(document, "span", statusBadgeClass(session.status), statusLabel(session.status))];
      if (text(session.revoked_at_utc)) {
        const who = text(session.revoked_by) ? ` by ${text(session.revoked_by)}` : "";
        statusLines.push(element(document, "div", "execution-ssh-meta", `Revoked ${formatTime(session.revoked_at_utc)}${who}`));
      }
      const statusCell = tableCell(document, "Status", ...statusLines);

      let actionContent;
      if (session.can_revoke === true) {
        const liveStatus = text(session.status).toLowerCase();
        const isLiveConnection = liveStatus === "open" || liveStatus === "connecting";
        const label = isLiveConnection ? "Terminate" : "Revoke";
        const button = element(document, "button", "button danger mcp-connection-action execution-ssh-revoke-session", label);
        button.type = "button";
        button.setAttribute("aria-label", `${label} SSH session ${text(session.session_id)}`);
        button.addEventListener("click", () => { revokeSession(session, button, isLiveConnection); });
        actionContent = button;
      } else {
        actionContent = element(document, "span", "execution-ssh-meta", "—");
      }
      const actionCell = tableCell(document, "Action", actionContent);

      row.append(sessionCell, delegationCell, expiresCell, statusCell, actionCell);
      tbody.append(row);
    });
    table.append(tbody);
    wrap.append(table);
    section.append(wrap);
    return section;
  }

  function render(payload) {
    if (!payload || payload.can_manage !== true) {
      container.hidden = true;
      body.replaceChildren();
      return;
    }
    container.hidden = false;
    const children = [];
    if (payload.ssh_enabled === false) {
      children.push(element(document, "p", "execution-ssh-note", EXECUTION_SSH_DISABLED_NOTE));
    }
    const targets = Array.isArray(payload.targets) ? payload.targets : [];
    const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
    children.push(renderTargets(targets), renderSessions(sessions));
    body.replaceChildren(...children);
  }

  function revoke(path, button, busyLabel, fallbackMessage, defaultSuccess) {
    if (state.actionInFlight || button.disabled) return Promise.resolve();
    state.actionInFlight = true;
    state.version += 1;
    const epoch = state.epoch;
    // Scope busy disabling to `body` so `refreshButton` in `header` is never snapshotted
    // while disabled by an overlapping load() refresh; load() already guards on actionInFlight.
    const finish = client.beginAction(button, busyLabel, body);
    setStatus("", false);
    return Promise.resolve()
      .then(() => client.post(path, {}, fallbackMessage))
      .then(payload => {
        if (epoch !== state.epoch) return;
        finish();
        state.actionInFlight = false;
        render(payload);
        setStatus(text(payload && payload.message) || defaultSuccess, false);
      }, error => {
        if (epoch !== state.epoch) return;
        finish();
        state.actionInFlight = false;
        setStatus(failureMessage(error, fallbackMessage), true);
      });
  }

  function revokeTarget(target, button) {
    const id = encodeURIComponent(text(target.target_id));
    return revoke(
      `${EXECUTION_SSH_API}/targets/${id}/revoke`,
      button,
      "Revoking…",
      REVOKE_TARGET_FAILED,
      "Enrolled SSH host revoked.",
    );
  }

  function revokeSession(session, button, isLiveConnection) {
    const id = encodeURIComponent(text(session.session_id));
    const busy = isLiveConnection ? "Terminating…" : "Revoking…";
    return revoke(
      `${EXECUTION_SSH_API}/sessions/${id}/revoke`,
      button,
      busy,
      REVOKE_SESSION_FAILED,
      isLiveConnection ? "SSH session terminated." : "SSH session revoked.",
    );
  }

  function load() {
    if (state.actionInFlight) return Promise.resolve();
    state.version += 1;
    const version = state.version;
    const epoch = state.epoch;
    const finishRefresh = client.beginRefresh(refreshButton);
    return Promise.resolve()
      .then(() => client.get(EXECUTION_SSH_API, LOAD_FAILED))
      .then(payload => {
        finishRefresh();
        if (epoch !== state.epoch || version !== state.version || state.actionInFlight) return;
        setStatus("", false);
        render(payload || {});
      }, error => {
        finishRefresh();
        if (epoch !== state.epoch || version !== state.version || state.actionInFlight) return;
        if (isForbidden(error)) {
          container.hidden = true;
          body.replaceChildren();
          setStatus("", false);
          return;
        }
        container.hidden = false;
        body.replaceChildren();
        setStatus(failureMessage(error, LOAD_FAILED), true);
      });
  }

  refreshButton.addEventListener("click", () => { load(); });

  return {
    load,
    resetStatus() { setStatus("", false); },
    /** Sign-out: hide the section, clear state and ignore replies to earlier reads. */
    clear() {
      state.epoch += 1;
      state.version += 1;
      state.actionInFlight = false;
      container.hidden = true;
      body.replaceChildren();
      setStatus("", false);
    },
  };
}

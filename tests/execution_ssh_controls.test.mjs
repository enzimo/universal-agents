// Settings > Tasks > Brokered SSH (execution_ssh_controls.mjs): administrator
// view of enrolled SSH hosts and delegated sessions with one-click Revoke / Terminate.
import assert from "node:assert/strict";
import test from "node:test";
import {
  EXECUTION_SSH_API,
  EXECUTION_SSH_DISABLED_NOTE,
  capabilityLabel,
  createExecutionSSHControls,
  statusBadgeClass,
  statusLabel,
} from "../assistant_gateway/onboarding_web/assets/execution_ssh_controls.mjs";
import { createSettingsClient } from "../assistant_gateway/onboarding_web/assets/settings_support.mjs";
import {
  answer,
  buttonLabels,
  byClass,
  byTag,
  errorResponse,
  fakeBrowser,
  jsonResponse,
  openRequests,
  settle,
} from "./settings_fake_dom.mjs";

const SAMPLE_TARGET = {
  target_id: "ssh_tgt_alpha",
  host: "198.51.100.42",
  port: 22,
  username: "ops",
  platform: "linux",
  host_key_algorithm: "ssh-ed25519",
  host_key_fingerprint: "SHA256:abc123fingerprint",
  credential_key: "prod-ssh-key",
  bootstrap_credential_key: "prod-bootstrap",
  lifecycle: "temporary",
  status: "active",
  can_revoke: true,
  expires_at_utc: "2026-12-01T12:00:00Z",
  revoked_at_utc: null,
  revoked_by: null,
  password_bootstrap_completed_at_utc: "2026-11-22T18:00:00Z",
  reason: "Bootstrap production worker host",
  owner_scope_kind: "team",
  owner_id: "team_main",
  created_at_utc: "2026-11-22T17:00:00Z",
};

const SAMPLE_SESSION = {
  session_id: "ssh_sess_01",
  target_id: "ssh_tgt_alpha",
  target_label: "ops@198.51.100.42:22",
  host: "198.51.100.42",
  port: 22,
  username: "ops",
  platform: "linux",
  status: "open",
  is_active: true,
  can_revoke: true,
  delegated_agent_type: "InfraSpecialist",
  delegated_task_id: "task_bootstrap_9",
  capabilities: ["broker_sudo", "workspace_transfer"],
  expires_at_utc: "2026-11-22T20:00:00Z",
  revoked_at_utc: null,
  revoked_by: null,
  owner_scope_kind: "team",
  owner_id: "team_main",
  created_at_utc: "2026-11-22T18:10:00Z",
  updated_at_utc: "2026-11-22T18:15:00Z",
};

function setup() {
  const { browser, document, requests } = fakeBrowser();
  const container = document.createElement("section");
  document.body.append(container);
  const client = createSettingsClient({ browser });
  client.setCsrfToken("csrf-ssh");
  const controls = createExecutionSSHControls({ container, client, browser });
  return { browser, document, requests, container, controls };
}

const statusLine = container => byClass(container, "execution-ssh-status")[0];

test("helpers format status labels, badge classes, and capability labels cleanly", () => {
  assert.equal(statusLabel("active"), "Active");
  assert.equal(statusLabel("revoked"), "Revoked");
  assert.equal(statusLabel(""), "Unknown");
  assert.equal(statusBadgeClass("open"), "execution-ssh-badge is-active");
  assert.equal(statusBadgeClass("revoked"), "execution-ssh-badge is-revoked");
  assert.equal(statusBadgeClass("expired"), "execution-ssh-badge is-expired");
  assert.equal(statusBadgeClass("closed"), "execution-ssh-badge is-inactive");
  assert.equal(capabilityLabel("broker_sudo"), "Broker sudo");
  assert.equal(capabilityLabel("workspace_transfer"), "Workspace transfer");
  assert.equal(capabilityLabel("password_bootstrap"), "Password bootstrap");
  assert.equal(capabilityLabel("typing"), "Typing");
});

test("non-administrators do not see the Brokered SSH section", async () => {
  const { requests, container, controls } = setup();
  assert.equal(container.hidden, true);

  controls.load();
  const req = await answer(requests, EXECUTION_SSH_API, jsonResponse({
    status: "ok", can_manage: false, ssh_enabled: false, targets: [], sessions: [],
  }));
  assert.equal(req.method, "GET");
  assert.equal(container.hidden, true);
  assert.equal(byClass(container, "execution-ssh-content")[0].children.length, 0);

  controls.load();
  await answer(requests, EXECUTION_SSH_API, errorResponse(403, "Only team administrators"));
  assert.equal(container.hidden, true);
  assert.equal(statusLine(container).hidden, true);
});

test("administrators see enrolled hosts, delegated sessions, and disabled notice when SSH is off", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  await answer(requests, EXECUTION_SSH_API, jsonResponse({
    status: "ok",
    can_manage: true,
    ssh_enabled: false,
    targets: [SAMPLE_TARGET],
    sessions: [SAMPLE_SESSION],
  }));

  assert.equal(container.hidden, false);
  assert.match(container.textContent, new RegExp(EXECUTION_SSH_DISABLED_NOTE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(container.textContent, /ops@198\.51\.100\.42:22/);
  assert.match(container.textContent, /SHA256:abc123fingerprint/);
  assert.match(container.textContent, /Key: prod-ssh-key/);
  assert.match(container.textContent, /Bootstrap: prod-bootstrap \(completed\)/);
  assert.match(container.textContent, /Agent: InfraSpecialist · Task: task_bootstrap_9/);
  assert.match(container.textContent, /Broker sudo/);
  assert.match(container.textContent, /Workspace transfer/);
  assert.deepEqual(buttonLabels(container), ["Refresh", "Revoke", "Terminate"]);
});

test("one-click Revoke on a host sends CSRF POST, disables while busy, and refreshes in place", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  await answer(requests, EXECUTION_SSH_API, jsonResponse({
    status: "ok",
    can_manage: true,
    ssh_enabled: true,
    targets: [SAMPLE_TARGET],
    sessions: [SAMPLE_SESSION],
  }));

  const revokeBtn = byClass(container, "execution-ssh-revoke-target")[0];
  revokeBtn.click();
  await settle();

  const post = openRequests(requests)[0];
  assert.equal(post.method, "POST");
  assert.equal(post.path, `${EXECUTION_SSH_API}/targets/ssh_tgt_alpha/revoke`);
  assert.equal(post.headers["X-UA-Permission-CSRF"], "csrf-ssh");
  assert.equal(revokeBtn.disabled, true);
  assert.equal(revokeBtn.textContent, "Revoking…");

  await answer(requests, `${EXECUTION_SSH_API}/targets/ssh_tgt_alpha/revoke`, jsonResponse({
    status: "ok",
    kind: "target",
    resource_id: "ssh_tgt_alpha",
    revoked: true,
    broker_notified: true,
    terminated_live: false,
    message: "Revoked enrolled host ops@198.51.100.42:22. Any live sessions and remote commands using it were stopped.",
    can_manage: true,
    ssh_enabled: true,
    targets: [{ ...SAMPLE_TARGET, status: "revoked", can_revoke: false, revoked_at_utc: "2026-11-22T18:30:00Z", revoked_by: "usr_admin" }],
    sessions: [{ ...SAMPLE_SESSION, status: "revoked", is_active: false, can_revoke: false, revoked_at_utc: "2026-11-22T18:30:00Z", revoked_by: "usr_admin" }],
  }));

  assert.equal(statusLine(container).getAttribute("role"), "status");
  assert.match(statusLine(container).textContent, /Revoked enrolled host ops@198\.51\.100\.42:22/);
  assert.equal(byClass(container, "execution-ssh-revoke-target").length, 0);
  assert.equal(byClass(container, "execution-ssh-revoke-session").length, 0);
});

test("one-click Terminate on an active session sends CSRF POST and handles failures with role=alert", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  await answer(requests, EXECUTION_SSH_API, jsonResponse({
    status: "ok",
    can_manage: true,
    ssh_enabled: true,
    targets: [],
    sessions: [SAMPLE_SESSION],
  }));

  const termBtn = byClass(container, "execution-ssh-revoke-session")[0];
  termBtn.click();
  await settle();
  const post = openRequests(requests)[0];
  assert.equal(post.method, "POST");
  assert.equal(post.path, `${EXECUTION_SSH_API}/sessions/ssh_sess_01/revoke`);
  assert.equal(termBtn.textContent, "Terminating…");

  await answer(requests, `${EXECUTION_SSH_API}/sessions/ssh_sess_01/revoke`, errorResponse(403, "Only team administrators can revoke."));
  assert.equal(statusLine(container).getAttribute("role"), "alert");
  assert.equal(statusLine(container).textContent, "Only team administrators can revoke.");
  assert.equal(termBtn.disabled, false);
  assert.equal(termBtn.textContent, "Terminate");
});

test("server values are rendered strictly as text and sign-out discards late responses", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  await answer(requests, EXECUTION_SSH_API, jsonResponse({
    status: "ok",
    can_manage: true,
    ssh_enabled: true,
    targets: [{ ...SAMPLE_TARGET, reason: "<img src=x onerror=alert(1)>" }],
    sessions: [],
  }));

  assert.equal(byTag(container, "img").length, 0);
  assert.match(container.textContent, /<img src=x onerror=alert\(1\)>/);

  controls.load();
  controls.clear();
  await answer(requests, EXECUTION_SSH_API, jsonResponse({
    status: "ok",
    can_manage: true,
    ssh_enabled: true,
    targets: [SAMPLE_TARGET],
    sessions: [SAMPLE_SESSION],
  }));
  assert.equal(container.hidden, true);
  assert.equal(byClass(container, "execution-ssh-content")[0].children.length, 0);
});

test("in-flight revoke ignores stale earlier load response and prevents duplicate clicks", async () => {
  const { requests, container, controls } = setup();
  controls.load();
  await answer(requests, EXECUTION_SSH_API, jsonResponse({
    status: "ok",
    can_manage: true,
    ssh_enabled: true,
    targets: [SAMPLE_TARGET],
    sessions: [],
  }));

  // Start a refresh, then click Revoke before the refresh resolves.
  controls.load();
  await settle();
  const staleLoad = openRequests(requests)[0];
  assert.equal(staleLoad.method, "GET");

  const revokeBtn = byClass(container, "execution-ssh-revoke-target")[0];
  revokeBtn.click();
  await settle();
  // Second click while in flight must not fire a second POST.
  revokeBtn.click();
  await settle();
  const posts = openRequests(requests).filter(r => r.method === "POST");
  assert.equal(posts.length, 1);

  // Stale GET resolves first: must be ignored.
  await answer(requests, EXECUTION_SSH_API, jsonResponse({
    status: "ok",
    can_manage: true,
    ssh_enabled: true,
    targets: [SAMPLE_TARGET],
    sessions: [],
  }));
  assert.equal(byClass(container, "execution-ssh-revoke-target")[0].disabled, true);

  // POST resolves with revoked state.
  await answer(requests, `${EXECUTION_SSH_API}/targets/ssh_tgt_alpha/revoke`, jsonResponse({
    status: "ok",
    kind: "target",
    resource_id: "ssh_tgt_alpha",
    revoked: true,
    broker_notified: true,
    terminated_live: false,
    message: "Revoked.",
    can_manage: true,
    ssh_enabled: true,
    targets: [{ ...SAMPLE_TARGET, status: "revoked", can_revoke: false }],
    sessions: [],
  }));
  assert.equal(byClass(container, "execution-ssh-revoke-target").length, 0);
  assert.equal(statusLine(container).textContent, "Revoked.");
});

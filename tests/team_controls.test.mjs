import assert from "node:assert/strict";
import test from "node:test";
import { CSRF_HEADER, createSettingsClient, localTimeFormatter } from "../assistant_gateway/onboarding_web/assets/settings_support.mjs";
import {
  INSTALLED_ADMIN_ONLY, PROCEDURES_ADMIN_ONLY, PROCEDURES_NEED_CONVERSATION, UNREGISTERED_PROCEDURE_TITLE,
  canDecideConnection, connectionDecisionText, connectionStatusLabel, createTeamControls, procedureChoices,
  relativeLastSeen,
} from "../assistant_gateway/onboarding_web/assets/team_controls.mjs";
import { createTeamActivity } from "../assistant_gateway/onboarding_web/assets/team_activity.mjs";
import {
  answer, buttonLabels, byClass, byTag, errorResponse, fakeBrowser, jsonResponse, openRequests, settle, withElements,
} from "./settings_fake_dom.mjs";

const formatTime = localTimeFormatter({ locale: "en-US", timeZone: "UTC" });
const DIGEST = "d".repeat(64);

function connection(overrides = {}) {
  return {
    connection_id: "conn-1", connection_name: "Crawl4AI", source: "catalog", lifecycle: "pending",
    can_decide: true, transport: "streamable_http", target_agent_types: ["WebResearchAgent"],
    tool_id: "mcp:crawl4ai", operation_count: 3, credential_mode: "operator_configured",
    record_digest: DIGEST, ...overrides,
  };
}

function member(overrides = {}) {
  return {
    agent_id: "lead", name: "Lead", named_agent_type: "TeamArchitect", is_team_lead: true,
    status: { label: "Available", tone: "success" }, available_skills: ["research", "writing"],
    last_seen_at: "2026-09-25T15:00:00Z", ...overrides,
  };
}

function teamPayload(overrides = {}) {
  return {
    members: [member()],
    definitions: [{ named_agent_type: "WebResearchAgent", display_name: "Web researcher", description: "Finds sources." }],
    mcp_connections: [connection()],
    hosted_mcp_servers: [],
    authorization_presentation_mode: "friendly",
    ...overrides,
  };
}

function proposal(overrides = {}) {
  return {
    proposal_id: "prop-1", proposer_named_agent_type: "TeamArchitect", requested_handler_id: "reports.weekly",
    display_name: "Weekly report (agent's name)", purpose: "Send the weekly report.",
    justification: "The team asked for it every Monday.", status: "ready_for_admin",
    created_at_utc: "2026-09-24T09:00:00Z", handler_registered: true, administrator_installable: true,
    trusted_handler: {
      handler_id: "reports.weekly", display_name: "Weekly report", description: "Builds the weekly report.",
      impact_description: "Reads task history and writes one report file.", risk: "medium", level: "write",
      handler_descriptor_digest: "a".repeat(64), input_schema_digest: "b".repeat(64),
    },
    ...overrides,
  };
}

function installedEntry(overrides = {}) {
  return {
    definition: {
      definition_id: "ipdef_1", handler_id: "reports.weekly", display_name: "Weekly report", risk: "high",
      manager_delegable: true, installed_at_utc: "2026-09-20T12:30:00Z",
    },
    handler_registration_valid: true,
    ...overrides,
  };
}

function fixture({ sessionKey = "session-1", retention = null } = {}) {
  const env = fakeBrowser();
  const nodes = withElements(env.document, [
    ["settings-team-panel", "section"],
    ["settings-team-refresh", "button", "settings-team-panel"],
    ["settings-team-status", "p", "settings-team-panel"],
    ["settings-team-content", "div", "settings-team-panel"],
    ["settings-procedures", "section", "settings-team-panel"],
  ]);
  const client = createSettingsClient({ browser: env.browser });
  client.setCsrfToken("csrf-1");
  let key = sessionKey;
  const teamActivity = createTeamActivity();
  const team = createTeamControls({
    panel: nodes["settings-team-panel"], content: nodes["settings-team-content"],
    proceduresContainer: nodes["settings-procedures"], status: nodes["settings-team-status"],
    refreshButton: nodes["settings-team-refresh"], client, getSessionKey: () => key, teamActivity, retention,
    browser: env.browser, formatTime, now: () => Date.parse("2026-09-25T15:00:30Z"),
  });
  return { ...env, ...nodes, team, client, teamActivity, content: nodes["settings-team-content"], procedures: nodes["settings-procedures"],
    status: nodes["settings-team-status"], setSessionKey(value) { key = value; } };
}

async function activated(f, { team = teamPayload(), proposals = [], definitions = [] } = {}) {
  const done = f.team.activate();
  await answer(f.requests, "/api/web-chat/team", jsonResponse(team));
  await answer(f.requests, "/api/web-chat/session-context", jsonResponse({ status: "ok", session_key: "session-1" }));
  await answer(f.requests, "/api/web-chat/procedures/proposals", jsonResponse({ status: "ok", proposals, has_more: false, total: proposals.length }));
  await answer(f.requests, "/api/web-chat/procedures/installed", jsonResponse({ status: "ok", definitions, has_more: false, total: definitions.length }));
  await done;
}

const connectionRow = (f, id) => byTag(f.content, "tr").find(row => row.getAttribute("data-connection-id") === id);
const proposalCard = (f, id) => byClass(f.procedures, "procedure-proposal-card").find(card => card.getAttribute("data-proposal-id") === id);

test("Approve and Reject appear only on pending connections this person can decide", async () => {
  assert.equal(canDecideConnection(connection()), true);
  assert.equal(canDecideConnection(connection({ can_decide: false })), false);
  assert.equal(canDecideConnection(connection({ can_decide: "true" })), false, "only the literal true counts");
  assert.equal(canDecideConnection(connection({ lifecycle: "active" })), false);

  const f = fixture();
  await activated(f, { team: teamPayload({ mcp_connections: [
    connection(),
    connection({ connection_id: "conn-2", can_decide: false }),
    connection({ connection_id: "conn-3", lifecycle: "active" }),
  ] }) });
  assert.deepEqual(buttonLabels(connectionRow(f, "conn-1")), ["Approve", "Reject"]);
  assert.deepEqual(buttonLabels(connectionRow(f, "conn-2")), []);
  assert.match(connectionRow(f, "conn-2").textContent, /No action/);
  assert.deepEqual(buttonLabels(connectionRow(f, "conn-3")), []);
  assert.match(connectionRow(f, "conn-1").textContent, /Set by administrator/);
  assert.match(connectionRow(f, "conn-1").textContent, /Who may use it: grants on mcp:crawl4ai \(Access page\)/);
});

test("an installed connection reads as installed; grants decide who may use it", () => {
  assert.equal(connectionStatusLabel(connection({ lifecycle: "active" })), "Installed");
  assert.equal(connectionStatusLabel(connection({ lifecycle: "pending" })), "Waiting for approval");
  assert.equal(connectionStatusLabel(connection({ lifecycle: "surprise" })), "Unavailable");
});

test("an MCP decision confirms the session, then posts the decision with the CSRF header and the row digest", async () => {
  const f = fixture();
  await activated(f);
  const buttons = byTag(connectionRow(f, "conn-1"), "button");
  buttons[0].click();
  await settle();
  assert.ok(buttons.every(button => button.disabled), "both choices are disabled while one is saved");
  assert.equal(buttons[0].textContent, "Approving…");
  buttons[1].click();

  const context = await answer(f.requests, "/api/web-chat/session-context", jsonResponse({ status: "ok", session_key: "session-1" }));
  assert.equal(context.method, "POST");
  assert.deepEqual(context.body, { session_key: "session-1" });
  assert.equal(context.headers[CSRF_HEADER], "csrf-1");

  const decision = await answer(f.requests, "/api/web-chat/mcp-connections/conn-1/decision",
    jsonResponse({ status: "ok", request_digest: DIGEST, response_digest: "e".repeat(64) }));
  assert.equal(decision.method, "POST");
  assert.equal(decision.headers[CSRF_HEADER], "csrf-1");
  assert.deepEqual(decision.body, { session_key: "session-1", decision: "approve", expected_record_digest: DIGEST });
  assert.equal(f.requests.filter(request => request.path.includes("/decision")).length, 1, "a second click sent nothing");

  await answer(f.requests, "/api/web-chat/team", jsonResponse(teamPayload({ mcp_connections: [connection({ lifecycle: "active" })] })));
  assert.equal(f.status.hidden, false);
  assert.equal(f.status.textContent, "Crawl4AI MCP server was approved.");
  assert.equal(f.status.classList.contains("is-error"), false);
  assert.match(connectionRow(f, "conn-1").textContent, /Installed/);
});

test("technical mode adds the request and decision digests to the saved-decision sentence", () => {
  const friendly = connectionDecisionText(connection(), "reject", {}, "friendly");
  assert.equal(friendly, "Crawl4AI MCP server was rejected.");
  const technical = connectionDecisionText(connection({ connection_name: "Search MCP server" }), "approve",
    { request_digest: "r1", response_digest: "r2" }, "technical");
  assert.equal(technical, "Search MCP server was approved. Request digest: r1. Decision digest: r2.");
});

test("a refused MCP decision shows the gateway's explanation and restores the choices", async () => {
  const f = fixture();
  await activated(f);
  const buttons = byTag(connectionRow(f, "conn-1"), "button");
  buttons[1].click();
  assert.equal(buttons[1].textContent, "Rejecting…");
  await answer(f.requests, "/api/web-chat/session-context", jsonResponse({ status: "ok" }));
  await answer(f.requests, "/api/web-chat/mcp-connections/conn-1/decision",
    errorResponse(409, "Someone else already decided this connection.", "Refresh the Team page."));
  assert.equal(f.status.textContent, "Someone else already decided this connection. Refresh the Team page.");
  assert.ok(f.status.classList.contains("is-error"));
  assert.ok(buttons.every(button => !button.disabled));
  assert.equal(buttons[1].textContent, "Reject");
  assert.equal(openRequests(f.requests).length, 0, "a refused decision does not reload the team");
});

test("without a sign-in token no decision is sent", async () => {
  const f = fixture();
  await activated(f);
  f.client.setCsrfToken("");
  const [approve] = byTag(connectionRow(f, "conn-1"), "button");
  approve.click();
  await settle();
  assert.equal(openRequests(f.requests).length, 0);
  assert.equal(f.status.textContent, "Your sign-in session expired. Sign in again, then retry.");
  assert.equal(approve.disabled, false);
  assert.equal(approve.textContent, "Approve");
});

test("a live status update with unchanged content only updates Last seen and keeps open skill lists", async () => {
  const f = fixture();
  await activated(f);
  const [skills] = byClass(f.content, "team-skills-details");
  assert.equal(byTag(skills, "summary")[0].textContent, "2 skills");
  skills.open = true;
  skills.dispatchEvent(new Event("toggle"));
  const [lastSeen] = byClass(f.content, "team-last-seen");
  assert.equal(lastSeen.textContent, "30 seconds ago");

  f.teamActivity.publish({ members: [member({ last_seen_at: "2026-09-25T15:00:28Z" })] });
  assert.equal(byClass(f.content, "team-skills-details")[0], skills, "the table was not rebuilt");
  assert.equal(lastSeen.textContent, "Just now");
  assert.equal(openRequests(f.requests).length, 0, "a live update reads nothing");

  f.teamActivity.publish({ members: [member({ status: { label: "Busy", tone: "warning" } })] });
  const [again] = byClass(f.content, "team-skills-details");
  assert.notEqual(again, skills, "changed content is rebuilt");
  assert.equal(again.open, true, "an open skill list stays open for the same agent");
  assert.ok(connectionRow(f, "conn-1"), "the MCP table from the last full read is drawn again");
});

test("Last seen reads as recent words, then as a local time", () => {
  const now = Date.parse("2026-09-25T15:00:00Z");
  assert.equal(relativeLastSeen("2026-09-25T14:59:58Z", now, formatTime), "Just now");
  assert.equal(relativeLastSeen("2026-09-25T14:59:00Z", now, formatTime), "1 minute ago");
  assert.equal(relativeLastSeen("2026-09-25T14:45:00Z", now, formatTime), "15 minutes ago");
  assert.match(relativeLastSeen("2026-09-25T12:00:00Z", now, formatTime), /Sep 25, 2026, 12:00\sPM UTC/);
  assert.equal(relativeLastSeen("", now, formatTime), "Unknown");
});

test("the Team tab follows the live status only while it is active, and never reads on a timer", async () => {
  const f = fixture();
  await activated(f);
  assert.equal(f.intervals.size, 0, "no timer of its own");
  f.teamActivity.publish({ members: [member({ name: "Lead two" })] });
  assert.match(f.content.textContent, /Lead two/);
  f.team.deactivate();
  f.teamActivity.publish({ members: [member({ name: "Lead three" })] });
  assert.doesNotMatch(f.content.textContent, /Lead three/, "a closed tab ignores updates");
  assert.equal(openRequests(f.requests).length, 0);
});

test("proposals show Add only when the gateway says the code is installable; closed ones have no choices", async () => {
  assert.deepEqual(procedureChoices(proposal()), ["reject", "install"]);
  assert.deepEqual(procedureChoices(proposal({ administrator_installable: false })), ["reject"]);
  assert.deepEqual(procedureChoices(proposal({ status: "installed" })), []);
  assert.deepEqual(procedureChoices(proposal({ status: "rejected", administrator_installable: true })), []);

  const f = fixture();
  await activated(f, { proposals: [
    proposal(),
    proposal({ proposal_id: "prop-2", status: "pending_registration", handler_registered: false,
      administrator_installable: false, trusted_handler: null }),
    proposal({ proposal_id: "prop-3", status: "installed", administrator_installable: false }),
  ] });
  const ready = proposalCard(f, "prop-1");
  assert.deepEqual(buttonLabels(ready), ["Reject", "Add"]);
  assert.equal(byTag(ready, "strong")[0].textContent, "Weekly report", "the heading comes from the registry");
  assert.match(ready.textContent, /Ready for review/);
  assert.match(ready.textContent, /Name the agent gaveWeekly report \(agent's name\)/);
  assert.match(ready.textContent, /RiskMedium/);
  assert.match(ready.textContent, /EffectReads task history and writes one report file\./);
  assert.match(ready.textContent, /Why \(from TeamArchitect\)Send the weekly report\.The team asked for it every Monday\./);
  assert.match(ready.textContent, /Sep 24, 2026, 9:00\sAM UTC/);
  assert.doesNotMatch(ready.textContent, /2026-09-24T/, "raw ISO times are never shown");

  const unregistered = proposalCard(f, "prop-2");
  assert.deepEqual(buttonLabels(unregistered), ["Reject"]);
  assert.equal(byTag(unregistered, "strong")[0].textContent, UNREGISTERED_PROCEDURE_TITLE);
  assert.match(unregistered.textContent, /Not built into the runtime yet/);
  assert.match(unregistered.textContent, /A developer must add it to the runtime/);

  assert.deepEqual(buttonLabels(proposalCard(f, "prop-3")), []);
  assert.match(proposalCard(f, "prop-3").textContent, /Added/);
});

test("agent-written proposal text stays text, even when it contains markup", async () => {
  const f = fixture();
  await activated(f, { proposals: [proposal({ purpose: "<img src=x onerror=alert(1)>", display_name: "<b>bold</b>" })] });
  const card = proposalCard(f, "prop-1");
  assert.equal(byTag(card, "img").length, 0);
  assert.equal(byTag(card, "b").length, 0);
  assert.ok(byTag(card, "p").some(node => node.textContent === "<img src=x onerror=alert(1)>"));
});

test("installed procedures are read from each entry's definition", async () => {
  const f = fixture();
  await activated(f, { definitions: [
    installedEntry(),
    installedEntry({ definition: { definition_id: "ipdef_2", display_name: "Cleanup", risk: "low",
      manager_delegable: false, installed_at_utc: "bad" }, handler_registration_valid: false }),
  ] });
  const [table] = byClass(f.procedures, "procedures-table");
  const rows = byTag(byTag(table, "tbody")[0], "tr");
  assert.equal(rows.length, 2);
  const first = byTag(rows[0], "td").map(cell => cell.textContent);
  assert.deepEqual(first.slice(1, 4), ["High", "Yes", "Matches the runtime"]);
  assert.equal(first[0], "Weekly reportreports.weekly");
  assert.match(first[4], /Sep 20, 2026, 12:30\sPM UTC/);
  assert.equal(first[5], "ipdef_1");
  assert.equal(byTag(rows[0], "td")[5].classList.contains("is-monospace"), true);
  assert.deepEqual(byTag(rows[0], "td").map(cell => cell.getAttribute("data-label")),
    ["Procedure", "Risk", "Team lead may pass it on", "Built-in code", "Added", "Definition ID"]);
  const second = byTag(rows[1], "td").map(cell => cell.textContent);
  assert.deepEqual(second.slice(1, 5), ["Low", "No", "Does not match the runtime", "Unknown"]);
});

test("people who are not administrators see a short note instead of an error", async () => {
  const f = fixture();
  const done = f.team.activate();
  await answer(f.requests, "/api/web-chat/team", jsonResponse(teamPayload()));
  await answer(f.requests, "/api/web-chat/session-context", jsonResponse({ status: "ok" }));
  await answer(f.requests, "/api/web-chat/procedures/proposals", errorResponse(403, "Only administrators can do this."));
  await answer(f.requests, "/api/web-chat/procedures/installed", errorResponse(403, "Only administrators can do this."));
  await done;
  assert.match(f.procedures.textContent, new RegExp(PROCEDURES_ADMIN_ONLY.replace(".", "\\.")));
  assert.doesNotMatch(f.procedures.textContent, /Only administrators can do this/);
  assert.equal(byClass(f.procedures, "procedures-status")[0].hidden, true, "no error is shown");
  assert.equal(f.status.hidden, true);
});

test("a list that only administrators may read says so while the other list still shows", async () => {
  const f = fixture();
  const done = f.team.activate();
  await answer(f.requests, "/api/web-chat/team", jsonResponse(teamPayload()));
  await answer(f.requests, "/api/web-chat/session-context", jsonResponse({ status: "ok" }));
  await answer(f.requests, "/api/web-chat/procedures/proposals", jsonResponse({ proposals: [proposal()], total: 1 }));
  await answer(f.requests, "/api/web-chat/procedures/installed", errorResponse(403, "No."));
  await done;
  assert.ok(proposalCard(f, "prop-1"));
  assert.match(f.procedures.textContent, new RegExp(INSTALLED_ADMIN_ONLY.replace(".", "\\.")));
});

test("a failed procedures read shows the reason and keeps the last list", async () => {
  const f = fixture();
  await activated(f, { proposals: [proposal()] });
  const reload = f.team.loadProcedures();
  await answer(f.requests, "/api/web-chat/procedures/proposals", errorResponse(503, "The gateway is restarting.", "Try again shortly."));
  await answer(f.requests, "/api/web-chat/procedures/installed", jsonResponse({ definitions: [] }));
  await reload;
  const [status] = byClass(f.procedures, "procedures-status");
  assert.equal(status.hidden, false);
  assert.equal(status.textContent, "The list below may be out of date. The gateway is restarting. Try again shortly.");
  assert.ok(proposalCard(f, "prop-1"), "the last list stays");
});

test("Add confirms the session, installs the proposal by id and reloads the lists", async () => {
  const f = fixture();
  await activated(f, { proposals: [proposal()] });
  const card = proposalCard(f, "prop-1");
  const add = byTag(card, "button").find(button => button.textContent === "Add");
  add.click();
  await settle();
  assert.equal(add.textContent, "Adding…");
  assert.ok(byTag(card, "button").every(button => button.disabled));
  const context = await answer(f.requests, "/api/web-chat/session-context", jsonResponse({ status: "ok" }));
  assert.deepEqual(context.body, { session_key: "session-1" });
  const install = await answer(f.requests, "/api/web-chat/procedures/proposals/prop-1/install",
    jsonResponse({ status: "installed" }));
  assert.equal(install.headers[CSRF_HEADER], "csrf-1");
  assert.deepEqual(install.body, { session_key: "session-1", explanation: "Administrator reviewed the trusted handler registration." });
  const reread = await answer(f.requests, "/api/web-chat/procedures/proposals",
    jsonResponse({ proposals: [proposal({ status: "installed", administrator_installable: false })] }));
  assert.match(reread.path, /\?session_key=session-1&offset=0&limit=20$/);
  await answer(f.requests, "/api/web-chat/procedures/installed", jsonResponse({ definitions: [installedEntry()] }));
  const [status] = byClass(f.procedures, "procedures-status");
  assert.equal(status.textContent, "Procedure added.");
  assert.deepEqual(buttonLabels(proposalCard(f, "prop-1")), []);
});

test("Reject sends the rejection reason and no install explanation", async () => {
  const f = fixture();
  await activated(f, { proposals: [proposal()] });
  byTag(proposalCard(f, "prop-1"), "button").find(button => button.textContent === "Reject").click();
  await answer(f.requests, "/api/web-chat/session-context", jsonResponse({ status: "ok" }));
  const reject = await answer(f.requests, "/api/web-chat/procedures/proposals/prop-1/reject", jsonResponse({ status: "ok" }));
  assert.deepEqual(reject.body, { session_key: "session-1", reason: "administrator_rejected" });
  await answer(f.requests, "/api/web-chat/procedures/proposals", jsonResponse({ proposals: [] }));
  await answer(f.requests, "/api/web-chat/procedures/installed", jsonResponse({ definitions: [] }));
  assert.equal(byClass(f.procedures, "procedures-status")[0].textContent, "Proposal rejected.");
});

test("the two procedure lists page together, following the longer list", async () => {
  const f = fixture();
  const done = f.team.activate();
  await answer(f.requests, "/api/web-chat/team", jsonResponse(teamPayload()));
  await answer(f.requests, "/api/web-chat/session-context", jsonResponse({ status: "ok" }));
  await answer(f.requests, "/api/web-chat/procedures/proposals", jsonResponse({ proposals: [proposal()], has_more: false, total: 1 }));
  await answer(f.requests, "/api/web-chat/procedures/installed", jsonResponse({ definitions: [installedEntry()], has_more: true, total: 45 }));
  await done;
  const [pager] = byClass(f.procedures, "list-pagination");
  assert.equal(pager.settings.total, 45);
  assert.equal(pager.settings.pageSize, 20);
  const next = pager.settings.onPage(20);
  const proposals = await answer(f.requests, "/api/web-chat/procedures/proposals", jsonResponse({ proposals: [], total: 1 }));
  const installed = await answer(f.requests, "/api/web-chat/procedures/installed", jsonResponse({ definitions: [installedEntry()], total: 45, has_more: true }));
  await next;
  assert.match(proposals.path, /&offset=20&limit=20$/);
  assert.match(installed.path, /&offset=20&limit=20$/);
});

test("sign-out clears the panel and ignores replies to earlier reads", async () => {
  const f = fixture();
  const done = f.team.activate();
  await settle();
  f.team.clear();
  assert.equal(f.intervals.size, 0);
  await answer(f.requests, "/api/web-chat/team", jsonResponse(teamPayload()));
  await answer(f.requests, "/api/web-chat/session-context", jsonResponse({ status: "ok" }));
  await answer(f.requests, "/api/web-chat/procedures/proposals", jsonResponse({ proposals: [proposal()] }));
  await answer(f.requests, "/api/web-chat/procedures/installed", jsonResponse({ definitions: [] }));
  await done;
  assert.equal(f.content.children.length, 0);
  assert.equal(proposalCard(f, "prop-1"), undefined);
});

test("procedure reads confirm the conversation's session once, and need a conversation", async () => {
  const f = fixture();
  await activated(f, { proposals: [proposal()] });
  const reload = f.team.loadProcedures();
  await answer(f.requests, "/api/web-chat/procedures/proposals", jsonResponse({ proposals: [proposal()] }));
  await answer(f.requests, "/api/web-chat/procedures/installed", jsonResponse({ definitions: [] }));
  await reload;
  assert.equal(f.requests.filter(request => request.path === "/api/web-chat/session-context").length, 1,
    "the same conversation is confirmed once");

  f.setSessionKey("");
  assert.equal(await f.team.loadProcedures(), false);
  assert.match(f.procedures.textContent, new RegExp(PROCEDURES_NEED_CONVERSATION.replace(".", "\\.")));
  assert.equal(openRequests(f.requests).length, 0, "nothing is read without a conversation");
});

test("a refused session confirmation is shown like a refused read", async () => {
  const f = fixture();
  const done = f.team.activate();
  await answer(f.requests, "/api/web-chat/team", jsonResponse(teamPayload()));
  await answer(f.requests, "/api/web-chat/session-context", errorResponse(503, "The gateway is restarting.", "Try again shortly."));
  await done;
  const [status] = byClass(f.procedures, "procedures-status");
  assert.equal(status.hidden, false);
  assert.equal(status.textContent, "The gateway is restarting. Try again shortly.");
  assert.equal(f.requests.filter(request => request.path.startsWith("/api/web-chat/procedures")).length, 0);
});

test("File Retention is read with each refresh and cleared at sign-out", async () => {
  const calls = [];
  const retention = {
    load() { calls.push("load"); return Promise.resolve(); },
    resetStatus() { calls.push("resetStatus"); },
    clear() { calls.push("clear"); },
  };
  const f = fixture({ retention });
  await activated(f);
  assert.deepEqual(calls, ["load"]);
  f.team.clear();
  assert.deepEqual(calls, ["load", "clear", "resetStatus"]);
});

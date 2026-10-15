import assert from "node:assert/strict";
import test from "node:test";

import {
  BLOCKED_LABEL, STEP_STATE_LABELS, graphColumns, requestLineModel, requestStatusLabel, setupLineModel,
  MARK_DONE_LABEL, stepStatusText,
} from "../assistant_gateway/onboarding_web/assets/workflow_graph.mjs";
import {
  CHOICE_APPROVE, CHOICE_DENY, batchResultText, bulkDecisions, reviewStatusText,
} from "../assistant_gateway/onboarding_web/assets/workflow.mjs";

const change = (outcome, extra = {}) => ({ request_id: "access_req_1", kind: "change", title: "Attach the crawler", outcome, ...extra });

test("request status words follow the access store's outcome", () => {
  assert.equal(requestStatusLabel(change("pending")), "Waiting for a decision");
  assert.equal(requestStatusLabel(change("approved")), "Approved, not applied yet");
  assert.equal(requestStatusLabel({ ...change("approved"), kind: "access" }), "Approved");
  assert.equal(requestStatusLabel(change("applied")), "Approved and applied");
  assert.equal(requestStatusLabel(change("apply_failed")), "Approved, but applying it failed");
  assert.equal(requestStatusLabel(change("denied")), "Denied");
  assert.equal(requestStatusLabel(change("withdrawn")), "Withdrawn: the task ended first");
  assert.equal(requestStatusLabel({}), "No longer available");
});

test("only a waiting request links to its Inbox review, and only within the site", () => {
  const waiting = requestLineModel(change("pending", { review_url: "/webchat?view=inbox#access_request=access_req_1" }));
  assert.equal(waiting.reviewUrl, "/webchat?view=inbox#access_request=access_req_1");
  assert.equal(requestLineModel(change("applied", { review_url: "/webchat?view=inbox" })).reviewUrl, "");
  assert.equal(requestLineModel(change("pending", { review_url: "https://elsewhere.example/steal" })).reviewUrl, "");
  assert.equal(requestLineModel(change("pending", { review_url: "//elsewhere.example/steal" })).reviewUrl, "");
});

test("a worker's request names the worker on the parent's step", () => {
  const line = requestLineModel({ request_id: "r", title: "read access to cli:google", outcome: "pending",
    from_worker: true, agent_type: "BOA" });
  assert.equal(line.label, "BOA: read access to cli:google");
  assert.equal(requestLineModel({ request_id: "r", title: "t", outcome: "pending", from_worker: false, agent_type: "BOA" }).label, "t");
});

test("a step the person denied says so before any other status", () => {
  assert.equal(stepStatusText({ blocked_by_person: true, review_status: "Waiting for 1 decision(s)", state: "failed" }), BLOCKED_LABEL);
  assert.equal(stepStatusText({ review_status: "Waiting for 1 decision(s); 0 of 1 approved", state: "waiting" }),
    "Waiting for 1 decision(s); 0 of 1 approved");
  assert.equal(stepStatusText({ state: "running" }), STEP_STATE_LABELS.running);
});

test("columns follow dependency depth", () => {
  const steps = [
    { step_id: "account", depends_on: [] },
    { step_id: "attach", depends_on: ["account"] },
    { step_id: "restart", depends_on: ["attach"] },
    { step_id: "docs", depends_on: [] },
  ];
  assert.deepEqual(graphColumns(steps).map((column) => column.map((step) => step.step_id)),
    [["account", "docs"], ["attach"], ["restart"]]);
});

test("approve all and deny all cover only what this person can decide with that choice", () => {
  const reviews = [
    { request_id: "a", can_decide: true, choices: [{ choice: "approve" }, { choice: "deny" }] },
    { request_id: "b", can_decide: false, choices: [] },
    { request_id: "c", can_decide: true, choices: [{ choice: "approve" }, { choice: "approve_read" }, { choice: "deny" }] },
  ];
  assert.deepEqual(bulkDecisions(reviews, CHOICE_APPROVE), [
    { request_id: "a", choice: "approve" }, { request_id: "c", choice: "approve" },
  ]);
  assert.deepEqual(bulkDecisions(reviews, CHOICE_DENY).map((item) => item.request_id), ["a", "c"]);
  assert.deepEqual(bulkDecisions(null, CHOICE_APPROVE), []);
});

test("status lines say what waits and report partial batches", () => {
  assert.match(reviewStatusText({ waiting_request_count: 2 }), /^2 request\(s\) wait/);
  assert.match(reviewStatusText({ waiting_request_count: 0 }), /No request waits/);
  assert.match(reviewStatusText({ waiting_request_count: 3, task_status: { state: "failed" } }), /source task has ended/);
  assert.equal(batchResultText({ all_saved: true, results: [{ saved: true }, { saved: true }] }),
    "2 decision(s) saved. The agent will be told.");
  assert.match(batchResultText({ all_saved: false, results: [{ saved: true }, { saved: false }] }), /^1 of 2 decisions saved/);
});

test("a setup page shows the plain status trusted code wrote, with the request row styles", () => {
  const done = setupLineModel({ setup_request_id: "tool_setup_req_1", title: "GitHub sign-in", status: "completed", status_text: "Signed in as exampleuser" });
  assert.deepEqual(done, {
    id: "tool_setup_req_1", label: "GitHub sign-in", status: "Signed in as exampleuser", style: "approved", canMarkDone: false,
  });
  assert.equal(setupLineModel({ status: "waiting", status_text: "Waiting for the person to finish the page" }).style, "pending");
  assert.equal(setupLineModel({ status: "declined" }).style, "denied");
  assert.equal(setupLineModel({}).status, "Status not known");
});

test("only a waiting step done outside Universal Agents can be marked done", () => {
  const step = { setup_request_id: "tool_setup_req_2", kind: "external_step", status: "waiting", title: "Gitea: Create a token" };
  assert.equal(setupLineModel(step).canMarkDone, true);
  assert.equal(setupLineModel({ ...step, status: "completed" }).canMarkDone, false);
  assert.equal(setupLineModel({ ...step, kind: "sign_in" }).canMarkDone, false);
  assert.equal(MARK_DONE_LABEL, "Mark Done");
});

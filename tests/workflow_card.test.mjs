import assert from "node:assert/strict";
import test from "node:test";

import { byClass, fakeDocument, settle } from "./fake_dom.mjs";

const ORIGIN = "http://chat.test";
const WORKFLOW_ID = "access_flow_demo";

globalThis.document = fakeDocument();
globalThis.location = { origin: ORIGIN };
globalThis.requestAnimationFrame = () => 0;
const { mountWorkflowCard } = await import("../assistant_gateway/onboarding_web/assets/workflow_card.mjs");

function workflow(state) {
  return { workflow_id: WORKFLOW_ID, title: "Read the shared drive", target_agent_type: "Researcher",
    goal: "Read the drive.", steps: [{ step_id: "s1", title: "Sign in", kind: "access", state,
      reason: "Needed.", success_criterion: "Signed in.", depends_on: [] }] };
}

/** A transcript bubble with a workflow link, and a request client whose reads the test answers. */
function harness() {
  const reads = [];
  const requestClient = {
    fetch: url => new Promise((resolve, reject) => reads.push({ url, resolve, reject })),
  };
  const answer = (index, payload, ok = true) => reads[index].resolve({ ok, json: async () => payload });
  const mount = ({ loaded, isCurrent = () => true } = {}) => {
    const bubble = document.createElement("article");
    bubble.querySelectorAll = () => [{ href: `${ORIGIN}/workflows/${WORKFLOW_ID}` }];
    document.body.append(bubble);
    mountWorkflowCard(bubble, { lifecycle: { listen() {} }, requestClient, isCurrent, loaded });
    const card = byClass(bubble, "workflow-inline-card")[0];
    return { bubble, card, heading: card.children[0], status: card.children[1], nodes: () => byClass(card, "workflow-node") };
  };
  return { reads, answer, mount };
}

test("a re-rendered card shows the last loaded workflow at once, then its current status", async () => {
  const { reads, answer, mount } = harness();
  const loaded = new Map();
  const first = mount({ loaded });
  assert.equal(first.status.textContent, "Loading the current workflow…");
  assert.equal(first.nodes().length, 0);
  answer(0, { workflow: workflow("running") });
  await settle();
  assert.equal(first.nodes()[0].dataset.state, "running");

  // The transcript is rebuilt: the new card has its full content before any read returns.
  first.bubble.remove();
  const second = mount({ loaded });
  assert.equal(second.heading.textContent, "Read the shared drive");
  assert.equal(second.nodes()[0].dataset.state, "running");
  assert.equal(reads.length, 2, "every mount reads the current status again");
  answer(1, { workflow: workflow("complete") });
  await settle();
  assert.equal(second.nodes()[0].dataset.state, "complete");
  assert.equal(loaded.get(WORKFLOW_ID).steps[0].state, "complete");
});

test("a failed read shows its message and later cards do not show the old copy", async () => {
  const { answer, mount } = harness();
  const loaded = new Map([[WORKFLOW_ID, workflow("running")]]);
  const card = mount({ loaded });
  answer(0, {}, false);
  await settle();
  assert.equal(card.status.textContent, "Open the workflow to review its current status.");
  assert.equal(loaded.has(WORKFLOW_ID), false);
});

test("a read that returns after sign-out is not kept or shown", async () => {
  const { answer, mount } = harness();
  const loaded = new Map();
  let signedIn = true;
  const card = mount({ loaded, isCurrent: () => signedIn });
  signedIn = false;
  answer(0, { workflow: workflow("running") });
  await settle();
  assert.equal(loaded.size, 0);
  assert.equal(card.nodes().length, 0);
});

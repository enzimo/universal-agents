import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ASSISTANT_NAME_SELECTOR, setAssistantName } from "../assistant_gateway/onboarding_web/assets/chat_assistant_name.mjs";

const chatHtml = readFileSync(new URL("../assistant_gateway/onboarding_web/assets/chat.html", import.meta.url), "utf8");

function pageWithHeaders(count) {
  const labels = Array.from({ length: count }, () => ({ textContent: "Lisa" }));
  return {
    labels,
    querySelectorAll(selector) {
      assert.equal(selector, ASSISTANT_NAME_SELECTOR);
      return labels;
    },
  };
}

test("the assistant's name reaches every page header, not only the chat header", () => {
  const sharedState = { assistantName: "Lisa" };
  const page = pageWithHeaders(5);
  setAssistantName(sharedState, page, "Ava");
  assert.equal(sharedState.assistantName, "Ava");
  assert.deepEqual(page.labels.map(label => label.textContent), ["Ava", "Ava", "Ava", "Ava", "Ava"]);
});

test("an empty name from the server keeps the name already shown", () => {
  const sharedState = { assistantName: "Ava" };
  const page = pageWithHeaders(2);
  setAssistantName(sharedState, page, "");
  assert.equal(sharedState.assistantName, "Ava");
  assert.deepEqual(page.labels.map(label => label.textContent), ["Ava", "Ava"]);
});

test("every page header's name in chat.html is marked to receive the assistant's name", () => {
  const brandLinks = chatHtml.match(/<a class="brand-lockup[^"]*"[\s\S]*?<\/a>/g) || [];
  assert.equal(brandLinks.length, 5, "Chat, Objectives, Files, Status and Knowledge each have a brand header");
  for (const link of brandLinks) {
    assert.match(link, /<span data-assistant-name>/, link);
  }
});

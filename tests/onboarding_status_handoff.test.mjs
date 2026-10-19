import assert from "node:assert/strict";
import test from "node:test";
import { createFinalization } from "../assistant_gateway/onboarding_web/assets/app_finalization.mjs";
import { fakeDocument } from "./settings_fake_dom.mjs";

test("onboarding completion opens authenticated status without polling private agent data", async () => {
  globalThis.document = fakeDocument();
  globalThis.window = { location: { href: "" } };
  const requests = [];
  const sharedState = { pages: [{ id: "standby" }, { id: "status" }], state: { pageIndex: 0 } };
  const finalization = createFinalization({
    finalizationState: {}, sharedState,
    lifecycle: { timeout() { throw Error("Completion must not poll"); } },
    operations: {
      setPage(index) { sharedState.state.pageIndex = index; },
      renderCurrentPage() {},
      webChatUrl: () => "https://example.test:18443/webchat",
    },
    requestClient: { async fetch(path) {
      requests.push(path);
      assert.equal(path, "/health");
      return { ok: true, json: async () => ({ service: "assistant-gateway" }) };
    } },
  });
  finalization.pollRuntimeAfterRestart();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sharedState.state.pageIndex, 1);
  assert.match(finalization.renderStatus().textContent, /Sign in.*team status/);
  assert.deepEqual(requests, ["/health"]);
  finalization.openStatus();
  assert.equal(window.location.href, "https://example.test:18443/webchat?view=status");
});

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { answer, byTag, fakeBrowser, jsonResponse, openRequests, settle, withElements } from "./settings_fake_dom.mjs";

const SCRIPT = readFileSync(new URL("../assistant_gateway/onboarding_web/assets/config-editor.js", import.meta.url), "utf8");

test("editor saves and restores use the displayed revision; a conflict preserves the edited text", async () => {
  const { browser, document, requests } = fakeBrowser();
  const ids = [...SCRIPT.matchAll(/document.getElementById\("([^"]+)"\)/g)].map(match => match[1]);
  const nodes = withElements(document, ids.map(id => [id]));
  // The existing editor deliberately renders its escaped syntax highlighting.
  for (const id of ["config-editor-highlight", "config-editor-backups"]) {
    Object.defineProperty(nodes[id], "innerHTML", {
      set(value) { this.html = value; this.replaceChildren(); }, get() { return this.html || ""; },
    });
  }
  const editor = nodes["config-editor-yaml"];
  vm.runInNewContext(SCRIPT, { window: browser, document });
  await answer(requests, "/api/web-chat/settings", jsonResponse({}));
  await answer(requests, "/api/config-editor/config", jsonResponse({ content: "original", revision: "revision-1" }));

  editor.value = "edited";
  nodes["config-editor-save"].click();
  await settle();
  assert.deepEqual(openRequests(requests)[0].body, { content: "edited", revision: "revision-1" });
  const backup = { name: "instance_config.yaml.bak.test" };
  await answer(requests, "/api/config-editor/config", jsonResponse({ revision: "revision-2", backup, backups: [backup] }));

  editor.value = "keep these edits";
  nodes["config-editor-save"].click();
  await settle();
  assert.equal(openRequests(requests)[0].body.revision, "revision-2");
  await answer(requests, "/api/config-editor/config", new Response("Reload the settings and try again.", { status: 409 }));
  assert.equal(editor.value, "keep these edits");
  assert.equal(nodes["config-editor-status"].textContent, "Reload the settings and try again.");

  byTag(nodes["config-editor-backups"], "button")[0].click();
  await settle();
  const restore = openRequests(requests)[0];
  assert.equal(restore.method, "POST");
  assert.deepEqual(restore.body, { revision: "revision-2" });
  await answer(requests, "/api/config-editor/backups/", jsonResponse({ revision: "revision-3" }));
  await answer(requests, "/api/config-editor/config", jsonResponse({ content: "restored", revision: "revision-3" }));
  assert.equal(editor.value, "restored");
});

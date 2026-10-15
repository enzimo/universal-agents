// Let's Begin > Import a team export (app_team_import.mjs): listing the copied
// exports, opening one with its passphrase, and where the browser goes next.
import assert from "node:assert/strict";
import test from "node:test";
import { createTeamImport, formatExportSize } from "../assistant_gateway/onboarding_web/assets/app_team_import.mjs";
import { answer, byTag, fakeBrowser, jsonResponse, settle } from "./settings_fake_dom.mjs";

const EXPORT = { file_name: "team.uatransfer", size_bytes: 3 * 1024 * 1024, modified_at_utc: "2026-10-12T09:30:00+00:00" };

function panelFixture() {
  const env = fakeBrowser();
  globalThis.document = env.document;
  const sharedState = { state: { teamImport: { loaded: false, loading: false, exports: [], importDir: "", problem: "", selected: "", message: "", opening: false } } };
  const visited = [], timeouts = [];
  let rendered = null;
  const lifecycle = {
    listen: (node, event, handler) => node.addEventListener(event, handler),
    timeout: (callback) => timeouts.push(callback),
  };
  const teamImport = createTeamImport({
    sharedState, lifecycle, requestClient: { fetch: env.browser.fetch },
    operations: {
      navigate: url => visited.push(url),
      renderCurrentPage: () => { rendered = teamImport.renderPanel(); },
      renderRevealableControl: input => input,
    },
  });
  rendered = teamImport.renderPanel();
  return { ...env, sharedState, visited, timeouts, teamImport, panel: () => rendered };
}

const buttons = panel => Object.fromEntries(byTag(panel, "button").map(button => [button.textContent, button]));
const passphraseInput = panel => byTag(panel, "input").find(input => input.id === "team-import-passphrase");

async function listed(f, payload) {
  f.timeouts.shift()();
  await answer(f.requests, "/api/team-import/exports", jsonResponse(payload));
  await settle();
}

test("the copied export is listed and chosen when it is the only one", async () => {
  const f = panelFixture();
  await listed(f, { import_dir: "/data/import", problem: "", exports: [EXPORT] });

  const panel = f.panel();
  assert.match(panel.textContent, /\/data\/import/);
  assert.match(panel.textContent, /team\.uatransfer · 3\.0 MB · copied 2026-10-12 09:30 UTC/);
  assert.equal(f.sharedState.state.teamImport.selected, "team.uatransfer");
  assert.equal(buttons(panel)["Open export"].disabled, false);
});

test("a data folder that already holds an instance cannot open an export", async () => {
  const f = panelFixture();
  await listed(f, { import_dir: "/data/import", problem: "Import works only on a fresh data folder.", exports: [EXPORT] });

  const panel = f.panel();
  assert.match(panel.textContent, /fresh data folder/);
  assert.equal(buttons(panel)["Open export"].disabled, true);
});

test("opening sends the passphrase once and continues on the import page", async () => {
  const f = panelFixture();
  await listed(f, { import_dir: "/data/import", problem: "", exports: [EXPORT] });
  const input = passphraseInput(f.panel());
  input.value = "correct horse battery staple";
  input.dispatchEvent(new Event("input"));

  buttons(f.panel())["Open export"].click();
  const sent = await answer(f.requests, "/api/team-import/open", jsonResponse({ token: "tok en", page: "/team-import" }));
  await settle();

  assert.deepEqual(sent.body, { file_name: "team.uatransfer", passphrase: "correct horse battery staple" });
  assert.deepEqual(f.visited, ["/team-import#tok%20en"]);
  assert.equal(passphraseInput(f.panel()).value, "", "the passphrase is not kept after sending");
});

test("a wrong passphrase is reported and the person can try again", async () => {
  const f = panelFixture();
  await listed(f, { import_dir: "/data/import", problem: "", exports: [EXPORT] });

  buttons(f.panel())["Open export"].click();
  await answer(f.requests, "/api/team-import/open", jsonResponse({ error: "The passphrase is wrong, or the file was changed after it was exported." }, 400));
  await settle();

  assert.match(f.panel().textContent, /The export was not opened\. The passphrase is wrong/);
  const alert = byTag(f.panel(), "p").find(node => node.getAttribute("role") === "alert");
  assert.ok(alert && alert.classList.contains("warning-callout"));
  assert.deepEqual(f.visited, []);
  assert.equal(buttons(f.panel())["Open export"].disabled, false);
});

test("export sizes read in plain units", () => {
  assert.equal(formatExportSize(512), "1 KB");
  assert.equal(formatExportSize(5 * 1024 * 1024), "5.0 MB");
  assert.equal(formatExportSize(2.5 * 1024 ** 3), "2.5 GB");
});

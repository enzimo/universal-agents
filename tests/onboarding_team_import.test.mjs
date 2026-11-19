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

// A team whose id this version cannot carry (owner's decision 103). The explanation and the
// refusal below are the sentences the server sends (team_transfer.team_id_choice, onboarding_engine).
const QUESTION = {
  exported_team_id: "team_research_assistant_pro",
  explanation: "This version's messages (protocol 1) carry the team id in their addresses, and this team's id 'team_research_assistant_pro' is 27 characters long; protocol 1 allows at most 24. Choose a new team id: the team is imported under it, and the export file stays as it is.",
  suggestion: "team_research_assistant",
  chosen_before: null,
};
const REFUSED_ID = "The team id 'team research' has characters protocol 1 does not allow; it allows letters, digits, '-' and '_', starting with a letter or digit. Messages carry it in their addresses.";
const teamIdInput = panel => byTag(panel, "input").find(input => input.id === "team-import-team-id");

async function askedForTeamId(f, question = QUESTION) {
  await listed(f, { import_dir: "/data/import", problem: "", exports: [EXPORT] });
  buttons(f.panel())["Open export"].click();
  await answer(f.requests, "/api/team-import/open", jsonResponse({ token: "tok en", page: "/team-import", team_id_question: question }));
  await settle();
}

test("a team id this version cannot carry is asked for, starting from the suggestion", async () => {
  const f = panelFixture();
  await askedForTeamId(f);

  const panel = f.panel();
  assert.deepEqual(f.visited, [], "the review waits for the new id");
  assert.ok(panel.textContent.includes(QUESTION.explanation), "the explanation is shown as the server words it");
  assert.equal(teamIdInput(panel).value, "team_research_assistant");
  assert.equal(buttons(panel)["Open export"], undefined, "the export is open");
  const input = teamIdInput(panel);
  input.value = "research_team";
  input.dispatchEvent(new Event("input"));

  buttons(f.panel())["Continue"].click();
  const sent = await answer(f.requests, "/api/team-import/team-id", jsonResponse({ page: "/team-import", team_id: "research_team" }));
  await settle();

  assert.deepEqual(sent.body, { team_id: "research_team" });
  assert.equal(sent.headers["X-Transfer-Token"], "tok en");
  assert.deepEqual(f.visited, ["/team-import#tok%20en"]);
});

test("a refused new team id shows by the field and keeps what was typed", async () => {
  const f = panelFixture();
  await askedForTeamId(f);
  const input = teamIdInput(f.panel());
  input.value = "team research";
  input.dispatchEvent(new Event("input"));

  buttons(f.panel())["Continue"].click();
  await answer(f.requests, "/api/team-import/team-id", jsonResponse({
    error: REFUSED_ID, field: "team_id" }, 400));
  await settle();

  const panel = f.panel();
  const error = byTag(panel, "p").find(node => node.id === "team-import-team-id-error");
  assert.ok(error && error.classList.contains("field-error") && error.getAttribute("role") === "alert");
  assert.equal(error.textContent, `This id was not used. ${REFUSED_ID}`);
  assert.equal(teamIdInput(panel).value, "team research");
  assert.equal(teamIdInput(panel).getAttribute("aria-invalid"), "true");
  assert.deepEqual(f.visited, []);
  assert.equal(buttons(panel)["Continue"].disabled, false, "the person can correct it and try again");
});

test("an id chosen when the export was opened before is offered, saying what another id does", async () => {
  const f = panelFixture();
  await askedForTeamId(f, { ...QUESTION, suggestion: "research_team", chosen_before: "research_team" });

  const panel = f.panel();
  assert.equal(teamIdInput(panel).value, "research_team");
  assert.match(panel.textContent, /You chose research_team when you opened this export before\. Keep it to keep the selections and edits you made; another id starts the review again with everything selected\./);

  buttons(panel)["Choose another export"].click();
  assert.equal(teamIdInput(f.panel()), undefined);
  assert.equal(buttons(f.panel())["Open export"].disabled, false, "back to the export list");
});

test("export sizes read in plain units", () => {
  assert.equal(formatExportSize(512), "1 KB");
  assert.equal(formatExportSize(5 * 1024 * 1024), "5.0 MB");
  assert.equal(formatExportSize(2.5 * 1024 ** 3), "2.5 GB");
});

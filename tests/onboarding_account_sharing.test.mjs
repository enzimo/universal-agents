// The onboarding Secrets page's sharing section (app_account_sharing.mjs):
// a card that shares accounts with people the administrator adds, one row per
// catalog service plus the model keys, the answer sent with onboarding, and
// the Review line.
import assert from "node:assert/strict";
import test from "node:test";
import {
  ACCOUNT_SHARING_TITLE,
  MODEL_KEYS_SUBJECT,
  createAccountSharing,
} from "../assistant_gateway/onboarding_web/assets/app_account_sharing.mjs";
import { byClass, byTag, fakeDocument } from "./settings_fake_dom.mjs";

// What /api/bootstrap sends (assistant_gateway.onboarding_account_keys.onboarding_sharing_services).
const SERVICES = [
  { subject: "http:exa", display_name: "Exa web search", description: "Search the web.", sharing: "shared", fixed: false, note: "" },
  { subject: "cli:github", display_name: "GitHub (gh)", description: "Repositories.", sharing: "private", fixed: true,
    note: "GitHub (gh) uses each person's own sign-in and has no team account, so it is always private." },
  { subject: "cli:x", display_name: "X (xurl)", description: "Read and post on X.", sharing: "private", fixed: false, note: "" },
  { subject: "http:crawl4ai", display_name: "Crawl4AI web crawler", description: "Screenshots.", sharing: "shared", fixed: true,
    note: "Crawl4AI web crawler uses only the team's account, so it is always shared." },
];

function setUp({ saved = null } = {}) {
  const storage = new Map(saved ? [["ua.onboarding.account_sharing", JSON.stringify(saved)]] : []);
  globalThis.document = fakeDocument();
  globalThis.window = {
    localStorage: {
      getItem: key => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
  };
  const sharedState = { state: { runtimeProvider: "OpenRouter", runtimeModel: "deepseek/deepseek-v4.1-flash" } };
  const renders = [];
  const lifecycle = { listen: (target, type, handler) => target.addEventListener(type, handler) };
  const sharing = createAccountSharing({ sharedState, lifecycle, operations: { renderCurrentPage: () => renders.push(1) } });
  sharing.applyServices(SERVICES);
  return { sharing, sharedState, storage, renders };
}

function rows(section) {
  return byClass(section, "sharing-row").map(row => ({
    subject: row.getAttribute("data-subject"),
    shared: byTag(row, "input")[0].checked,
    locked: byTag(row, "input")[0].disabled,
    state: byClass(row, "sharing-state")[0].textContent,
  }));
}

function flip(section, subject) {
  const row = byClass(section, "sharing-row").find(item => item.getAttribute("data-subject") === subject);
  const input = byTag(row, "input")[0];
  input.checked = !input.checked;
  input.dispatchEvent(new Event("change"));
}

test("sharing is on by default: model keys and resource services start shared, GitHub is locked private", () => {
  const { sharing } = setUp();

  const section = sharing.renderAccountSharing();

  const card = byClass(section, "account-sharing-card")[0];
  assert.match(card.textContent, new RegExp(ACCOUNT_SHARING_TITLE));
  assert.equal(byTag(card, "input")[0].checked, true);
  assert.deepEqual(rows(section).map(row => [row.subject, row.shared, row.locked]), [
    [MODEL_KEYS_SUBJECT, true, false],
    ["http:exa", true, false],
    ["cli:x", false, false],
    ["cli:github", false, true],
    ["http:crawl4ai", true, true],
  ]);
  const [model, exa, x, github, crawl4ai] = rows(section);
  assert.match(model.state, /people you add may use the team's model keys/);
  assert.match(exa.state, /^Shared: everyone's agents may use the account you set up/);
  assert.match(x.state, /^Private: only your own work uses it/);
  assert.equal(github.state, "Always private.");
  assert.equal(crawl4ai.state, "Always shared.");
  assert.match(byClass(section, "sharing-row").at(-2).textContent, /always private/);
  assert.match(byClass(section, "sharing-row").at(-1).textContent, /always shared/);
  assert.match(byClass(section, "sharing-row")[0].textContent, /OpenRouter \/ deepseek/);
});

test("the answer lists every choice the person can make and leaves fixed services out", () => {
  const { sharing } = setUp();

  assert.deepEqual(sharing.payload(), {
    share_with_team: true,
    choices: { [MODEL_KEYS_SUBJECT]: "shared", "http:exa": "shared", "cli:x": "private" },
  });
});

test("changing a row updates its explanation, the answer and the saved choice", () => {
  const { sharing, storage } = setUp();
  const section = sharing.renderAccountSharing();

  flip(section, "http:exa");
  flip(section, MODEL_KEYS_SUBJECT);
  flip(section, "cli:x");

  const byId = Object.fromEntries(rows(section).map(row => [row.subject, row]));
  assert.match(byId["http:exa"].state, /^Private:/);
  assert.match(byId[MODEL_KEYS_SUBJECT].state, /set up their own model/);
  assert.match(byId["cli:x"].state, /^Shared:/);
  assert.deepEqual(sharing.payload().choices, { [MODEL_KEYS_SUBJECT]: "private", "http:exa": "private", "cli:x": "shared" });
  assert.deepEqual(JSON.parse(storage.get("ua.onboarding.account_sharing")).choices, {
    "http:exa": "private", [MODEL_KEYS_SUBJECT]: "private", "cli:x": "shared",
  });
});

test("turning sharing off hides the rows, makes everything private and redraws the page", () => {
  const { sharing, renders } = setUp();
  const section = sharing.renderAccountSharing();
  const input = byTag(byClass(section, "account-sharing-card")[0], "input")[0];

  input.checked = false;
  input.dispatchEvent(new Event("change"));
  const redrawn = sharing.renderAccountSharing();

  assert.equal(renders.length, 1);
  assert.deepEqual(rows(redrawn), []);
  assert.deepEqual(sharing.payload(), {
    share_with_team: false,
    choices: { [MODEL_KEYS_SUBJECT]: "private", "http:exa": "private", "cli:x": "private" },
  });
  assert.equal(sharing.summary(), "Not shared: everything serves only your own work");
});

test("the Review line names what is shared and what is private", () => {
  const { sharing } = setUp({ saved: { shareWithTeam: true, choices: { "http:exa": "private" } } });

  assert.equal(sharing.summary(), "Shared: Model provider keys. Private: Exa web search, X (xurl)");
});

test("a damaged saved value falls back to the defaults, and the services list is replaced only when it changed", () => {
  const { sharing } = setUp({ saved: "not an object" });

  assert.equal(sharing.payload().share_with_team, true);
  assert.equal(sharing.applyServices(SERVICES), false);
  assert.equal(sharing.applyServices(SERVICES.slice(0, 1)), true);
  assert.equal(sharing.applyServices("nothing"), false);
});

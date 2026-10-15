// Settings > Accounts (accounts_view.mjs): the /accounts rows as a table, read only.
import assert from "node:assert/strict";
import test from "node:test";
import {
  ACCOUNTS_API, COLUMN_LABELS, EMPTY_TEXT, accountRowModel, changeParts, createAccountsView, renderAccounts,
} from "../assistant_gateway/onboarding_web/assets/accounts_view.mjs";
import { answer, byClass, byTag, errorResponse, fakeBrowser, jsonResponse, settle } from "./settings_fake_dom.mjs";

// Rows as the gateway sends them (website_handlers.accounts.service_payload).
const row = fields => ({
  subject: "http:exa", name: "Exa web search", sharing: "shared", fixed: false, team_account: "linked",
  own_account: "not_linked", setup_command: "/link http:exa", signs_in: false, ...fields,
});
const MODEL = row({ subject: "model", name: "Model keys", sharing: "private", team_account: null, own_account: null,
  setup_command: "/auth model me" });
const GITHUB = row({ subject: "cli:github", name: "GitHub (gh)", sharing: "private", fixed: true,
  team_account: null, own_account: null, setup_command: "/link cli:github", signs_in: true });
const CRAWL = row({ subject: "http:crawl4ai", name: "Crawl4AI web crawler", fixed: true, team_account: "not_linked",
  own_account: null, setup_command: "/link http:crawl4ai" });
const PAGE = { status: "ok", services: [MODEL, row(), GITHUB, CRAWL], can_change: true,
  share_command: "/accounts share", private_command: "/accounts private" };

test("each service reads as its sharing, the team's account and the person's own, with the command to send", () => {
  assert.deepEqual(accountRowModel(row()), {
    name: "Exa web search", subject: "http:exa", sharing: "Shared", sharingNote: "", team: "Linked",
    own: "Not linked: link it with", command: "/link http:exa",
  });
  assert.deepEqual(accountRowModel(row({ sharing: "private", team_account: "not_used", own_account: "linked" })), {
    name: "Exa web search", subject: "http:exa", sharing: "Private", sharingNote: "",
    team: "Linked, not used while private", own: "Linked", command: "",
  });
  const x = accountRowModel(row({ subject: "cli:x", sharing: "private", own_account: "sign_in", setup_command: "/link cli:x" }));
  assert.equal(`${x.own} ${x.command}`, "Sign in with /link cli:x");
});

test("always-private and always-shared services, and the model keys, say what they mean", () => {
  const github = accountRowModel(GITHUB);
  assert.deepEqual([github.sharing, github.sharingNote, github.team, github.own, github.command],
    ["Always private", "Each person uses their own account.", "—", "Sign in with", "/link cli:github"]);
  const crawl = accountRowModel(CRAWL);
  assert.deepEqual([crawl.sharing, crawl.sharingNote, crawl.team, crawl.own, crawl.command],
    ["Always shared", "It uses only the team's account.", "Not linked", "—", ""]);
  const model = accountRowModel(MODEL);
  assert.deepEqual([model.subject, model.sharing, model.sharingNote, model.own, model.command],
    ["", "Private", "People added from now on set up their own model.", "New people set up their own with", "/auth model me"]);
  const shared = accountRowModel({ ...MODEL, sharing: "shared", setup_command: "" });
  assert.deepEqual([shared.sharingNote, shared.own], ["People added from now on may use the team's model keys.", "—"]);
});

test("the table labels every cell, shows ids and commands as code, and builds no HTML", () => {
  const { document } = fakeBrowser();
  const view = renderAccounts(document, PAGE);
  assert.deepEqual(byTag(view, "th").map(cell => cell.textContent), COLUMN_LABELS);
  const rows = byTag(view, "tbody")[0].children;
  assert.equal(rows.length, 4);
  const exa = rows[1].children;
  assert.deepEqual(exa.map(cell => cell.getAttribute("data-label")), COLUMN_LABELS);
  assert.equal(byClass(exa[0], "accounts-service-name")[0].textContent, "Exa web search");
  assert.deepEqual(byClass(rows[1], "accounts-command").map(node => node.textContent), ["http:exa", "/link http:exa"]);
  // Each cell's content sits in one wrapper, so narrow screens keep it beside its label.
  assert.ok(exa.every(cell => cell.children.length === 1 && cell.children[0].classList.contains("accounts-cell")));
  const [note] = byClass(view, "accounts-change-note");
  assert.equal(note.textContent, "Change it in chat with /accounts share <tool_id|model> or /accounts private <tool_id|model>.");
});

test("members are told who changes sharing; an empty list says so", () => {
  assert.deepEqual(changeParts({ can_change: false }).map(part => part.text || part.code),
    ["Owners and administrators change this in chat with ", "/accounts", "."]);
  const { document } = fakeBrowser();
  assert.equal(renderAccounts(document, { services: [] }).textContent, EMPTY_TEXT);
});

function viewFixture() {
  const env = fakeBrowser();
  const container = env.document.createElement("div");
  const status = env.document.createElement("p");
  status.hidden = true;
  const refreshes = [];
  const view = createAccountsView({
    container, status,
    get: path => env.browser.fetch(path).then(response => response.json().then(body => {
      if (!response.ok) throw new Error(body.error);
      return body;
    })),
    beginRefresh: () => { refreshes.push("busy"); return () => refreshes.push("done"); },
    failureMessage: (error, fallback) => error.message || fallback,
    browser: env.browser,
  });
  return { ...env, container, status, view, refreshes };
}

test("loading reads the signed-in person's accounts and shows the table", async () => {
  const f = viewFixture();
  const loaded = f.view.load();
  assert.equal(f.requests[0].path, ACCOUNTS_API);
  await answer(f.requests, ACCOUNTS_API, jsonResponse(PAGE));
  await loaded;
  assert.equal(byTag(f.container, "tbody")[0].children.length, 4);
  assert.deepEqual(f.refreshes, ["busy", "done"]);
  assert.equal(f.status.hidden, true);
});

test("a refusal is shown on the status line and the old table goes", async () => {
  const f = viewFixture();
  const first = f.view.load();
  await answer(f.requests, ACCOUNTS_API, jsonResponse(PAGE));
  await first;
  const second = f.view.load();
  await answer(f.requests, ACCOUNTS_API, errorResponse(403, "You are not an active member of this team."));
  await second;
  assert.equal(f.container.children.length, 0);
  assert.equal(f.status.hidden, false);
  assert.ok(f.status.classList.contains("is-error"));
});

test("a read that ends after sign-out or a newer read is dropped", async () => {
  const f = viewFixture();
  const stale = f.view.load();
  f.view.clear();
  await answer(f.requests, ACCOUNTS_API, jsonResponse(PAGE));
  await stale;
  await settle();
  assert.equal(f.container.children.length, 0, "nothing shows after sign-out");
});

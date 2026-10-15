/**
 * Settings > Accounts in Web Chat: whose account agents use for each service
 * (owner's request, 2026-10-11; ui/DESIGN.md "Accounts Page").
 *
 * It shows what the `/accounts` chat command lists, from the same rows
 * (assistant_gateway.account_sharing.ServiceAccounts): the team's model keys
 * first, then every service that acts with an account, with whether the team
 * shares it, whether the team's account is linked and whether the signed-in
 * person set up their own, and the command that sets it up. The page is read
 * only: administrators change sharing in chat (`/accounts share|private`),
 * where the gateway checks them and tells the other administrators.
 *
 * The gateway answers for the signed-in person only and decides whether they
 * may change sharing (`can_change`). Server text is shown with `textContent`.
 * The view-model and rendering functions are exported for node tests;
 * `createAccountsView` connects them to the page.
 */

import { element, setStatusLine, text } from "./settings_support.mjs";

export const ACCOUNTS_API = "/api/web-chat/accounts";
export const MODEL_KEYS_SUBJECT = "model";
export const COLUMN_LABELS = Object.freeze(["Service", "Sharing", "Team account", "Your own"]);
export const EMPTY_TEXT = "No service uses an account.";
const READ_FALLBACK = "Accounts could not be loaded because the gateway returned no explanation. Refresh and try again.";
const NOT_APPLICABLE = "—";
const SHARED = "shared";

// What each account state reads as (assistant_gateway.account_sharing.AccountStatus).
const TEAM_ACCOUNT_WORDS = Object.freeze({
  linked: "Linked",
  not_linked: "Not linked",
  not_used: "Linked, not used while private",
});
const OWN_ACCOUNT_WORDS = Object.freeze({
  linked: "Linked",
  not_linked: "Not linked",
  sign_in: "Sign in",
});

function sharingWords(row) {
  const shared = text(row.sharing) === SHARED;
  if (row.fixed) return shared ? "Always shared" : "Always private";
  return shared ? "Shared" : "Private";
}

// One line under the sharing word, saying what it means where that is not obvious.
function sharingNote(row) {
  const shared = text(row.sharing) === SHARED;
  if (text(row.subject) === MODEL_KEYS_SUBJECT) {
    return shared
      ? "People added from now on may use the team's model keys."
      : "People added from now on set up their own model.";
  }
  if (row.fixed) return shared ? "It uses only the team's account." : "Each person uses their own account.";
  return "";
}

// The "Your own" cell: its words and the command to send, when there is something to do.
function ownAccount(row) {
  const command = text(row.setup_command);
  if (text(row.subject) === MODEL_KEYS_SUBJECT) {
    return command ? { words: "New people set up their own with", command } : { words: NOT_APPLICABLE, command: "" };
  }
  const state = text(row.own_account);
  if (state === "linked") return { words: OWN_ACCOUNT_WORDS.linked, command: "" };
  if (state === "not_linked") return { words: `${OWN_ACCOUNT_WORDS.not_linked}: link it with`, command };
  if (state === "sign_in") return { words: `${OWN_ACCOUNT_WORDS.sign_in} with`, command };
  // An always-private service takes each person's own account and says how here.
  if (row.fixed && text(row.sharing) !== SHARED && command) {
    return { words: row.signs_in ? "Sign in with" : "Link yours with", command };
  }
  return { words: NOT_APPLICABLE, command: "" };
}

/** One service as the table shows it. */
export function accountRowModel(row) {
  const subject = text(row.subject);
  const own = ownAccount(row);
  return {
    name: text(row.name) || subject,
    subject: subject === MODEL_KEYS_SUBJECT ? "" : subject,
    sharing: sharingWords(row),
    sharingNote: sharingNote(row),
    team: TEAM_ACCOUNT_WORDS[text(row.team_account)] || NOT_APPLICABLE,
    own: own.words,
    command: own.command,
  };
}

/**
 * The line under the table: how sharing changes, and who may change it, as
 * parts; a part with `code` is a command shown as code.
 */
export function changeParts(payload) {
  if (payload && payload.can_change) {
    return [
      { text: "Change it in chat with " },
      { code: `${text(payload.share_command)} <tool_id|model>` },
      { text: " or " },
      { code: `${text(payload.private_command)} <tool_id|model>` },
      { text: "." },
    ];
  }
  return [{ text: "Owners and administrators change this in chat with " }, { code: "/accounts" }, { text: "." }];
}

// One cell; its content sits in one wrapper so narrow screens, which show
// each cell as its label beside its content, keep the content together.
function cell(document, row, label, ...content) {
  const node = element(document, "td");
  node.setAttribute("data-label", label);
  const wrapper = element(document, "div", "accounts-cell");
  wrapper.append(...content);
  node.append(wrapper);
  row.append(node);
  return node;
}

function code(document, value) {
  return element(document, "code", "accounts-command", value);
}

// A secondary line under a cell's main words.
// `content` is words or one element, such as a command shown as code.
function note(document, content) {
  if (typeof content === "string") return element(document, "span", "accounts-note", content);
  const line = element(document, "span", "accounts-note");
  line.append(content);
  return line;
}

function renderRow(document, model) {
  const row = element(document, "tr");
  const service = [element(document, "strong", "accounts-service-name", model.name)];
  if (model.subject) service.push(note(document, code(document, model.subject)));
  cell(document, row, COLUMN_LABELS[0], ...service);
  const sharing = [element(document, "span", "", model.sharing)];
  if (model.sharingNote) sharing.push(note(document, model.sharingNote));
  cell(document, row, COLUMN_LABELS[1], ...sharing);
  cell(document, row, COLUMN_LABELS[2], element(document, "span", "", model.team));
  const own = [element(document, "span", "", model.own)];
  if (model.command) own.push(note(document, code(document, model.command)));
  cell(document, row, COLUMN_LABELS[3], ...own);
  return row;
}

/** The whole tab's content: the table of services and the line about changing it. */
export function renderAccounts(document, payload) {
  const fragment = element(document, "div", "accounts-view");
  const services = Array.isArray(payload && payload.services) ? payload.services : [];
  if (!services.length) {
    fragment.append(element(document, "p", "permission-empty-state", EMPTY_TEXT));
    return fragment;
  }
  const wrap = element(document, "div", "team-table-wrap accounts-table-wrap");
  const table = element(document, "table", "team-table accounts-table");
  const head = element(document, "thead");
  const headRow = element(document, "tr");
  COLUMN_LABELS.forEach(label => {
    const th = element(document, "th", "", label);
    th.setAttribute("scope", "col");
    headRow.append(th);
  });
  head.append(headRow);
  const body = element(document, "tbody");
  services.forEach(row => body.append(renderRow(document, accountRowModel(row))));
  table.append(head, body);
  wrap.append(table);
  const change = element(document, "p", "accounts-change-note");
  changeParts(payload).forEach(part => {
    change.append(part.code ? code(document, part.code) : document.createTextNode(part.text));
  });
  fragment.append(wrap, change);
  return fragment;
}

/**
 * The tab's controller. `get(path)` reads through the Settings client;
 * `beginRefresh()` shows the Refresh button busy and returns its finish.
 * A read that finishes after a newer one, or after sign-out, is dropped.
 */
export function createAccountsView({ container, status, get, beginRefresh, failureMessage, browser = globalThis }) {
  const document = browser.document;
  let generation = 0;

  function load() {
    generation += 1;
    const current = generation;
    const finish = typeof beginRefresh === "function" ? beginRefresh() : () => {};
    setStatusLine(status, "", false);
    return get(ACCOUNTS_API)
      .then(payload => {
        if (current !== generation) return;
        container.replaceChildren(renderAccounts(document, payload));
      })
      .catch(error => {
        if (current !== generation) return;
        container.replaceChildren();
        setStatusLine(status, failureMessage(error, READ_FALLBACK), true);
      })
      .finally(finish);
  }

  return {
    load,
    /** Sign-out: forget the content and drop any read still on its way. */
    clear() {
      generation += 1;
      container.replaceChildren();
      setStatusLine(status, "", false);
    },
  };
}

// The Secrets page's sharing section (owner's decisions 2026-10-08,
// access_control.account_sharing): whether the accounts and keys the first
// administrator sets up serve the people they add, or only their own work.
//
// One toggle card, "Share accounts with people you add" (on by default). While
// it is on, one row per service from the tool catalog (sent by /api/bootstrap,
// never a list in this script) plus a row for the model keys: the toggle on
// means shared, and a line under each row says what the choice does. Services
// whose sharing cannot change show a locked toggle and the reason: GitHub and
// Google are always private, the team's own Crawl4AI server always shared.
// Turning the card off makes everything else private. The answer is sent as
// account_sharing with the other onboarding answers and checked on the server.

export const MODEL_KEYS_SUBJECT = "model_keys";
export const SHARED = "shared";
export const PRIVATE = "private";
const STORAGE_KEY = "ua.onboarding.account_sharing";

export const ACCOUNT_SHARING_TITLE = "Share accounts with people you add";
export const ACCOUNT_SHARING_HELP = "On: choose below which accounts and keys everyone's agents may use. "
  + "Off: the accounts and keys you set up here serve only your own work, and people you add set up their own. "
  + "The team's background jobs still use the model key, and the team's own Crawl4AI server stays shared.";
const FIXED_STATE_WORDS = {
  [SHARED]: "Always shared.",
  [PRIVATE]: "Always private.",
};
const MODEL_KEYS_NAME = "Model provider keys";
const STATE_WORDS = {
  [SHARED]: "Shared: everyone's agents may use the account you set up, unless a person links their own.",
  [PRIVATE]: "Private: only your own work uses it. People you add link their own.",
};
const MODEL_STATE_WORDS = {
  [SHARED]: "Shared: people you add may use the team's model keys.",
  [PRIVATE]: "Private: people you add set up their own model. Your own work and the team's background jobs keep this key.",
};

export function createAccountSharing({ sharedState, lifecycle, operations }) {
  function load() {
    try {
      const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "null");
      if (saved && typeof saved === "object") {
        return {
          shareWithTeam: saved.shareWithTeam !== false,
          choices: saved.choices && typeof saved.choices === "object" ? { ...saved.choices } : {},
        };
      }
    } catch (error) {
      // A damaged saved value falls back to the defaults the page shows.
    }
    return { shareWithTeam: true, choices: {} };
  }

  function current() {
    if (!sharedState.state.accountSharing) {
      sharedState.state.accountSharing = load();
    }
    return sharedState.state.accountSharing;
  }

  function save() {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(current()));
  }

  /** Services from the catalog: the ones a team may choose first, then the fixed ones, each by name. */
  function services() {
    const list = Array.isArray(sharedState.accountSharingServices) ? sharedState.accountSharingServices : [];
    return [...list.filter(service => !service.fixed), ...list.filter(service => service.fixed)];
  }

  /** Keep the services /api/bootstrap sent; true when the list changed. */
  function applyServices(list) {
    if (!Array.isArray(list)) {
      return false;
    }
    const valid = list.filter(service => service && typeof service.subject === "string" && service.subject);
    const changed = JSON.stringify(valid) !== JSON.stringify(sharedState.accountSharingServices || []);
    sharedState.accountSharingServices = valid;
    return changed;
  }

  function choiceFor(service) {
    if (service.fixed) {
      return service.sharing === SHARED ? SHARED : PRIVATE;
    }
    if (!current().shareWithTeam) {
      return PRIVATE;
    }
    return current().choices[service.subject] || (service.sharing === PRIVATE ? PRIVATE : SHARED);
  }

  function modelChoice() {
    if (!current().shareWithTeam) {
      return PRIVATE;
    }
    return current().choices[MODEL_KEYS_SUBJECT] === PRIVATE ? PRIVATE : SHARED;
  }

  function setChoice(subject, shared) {
    current().choices[subject] = shared ? SHARED : PRIVATE;
    save();
  }

  /** The onboarding answer: whether accounts are shared at all, and each choice the person can make. */
  function payload() {
    const choices = { [MODEL_KEYS_SUBJECT]: modelChoice() };
    services().filter(service => !service.fixed).forEach(service => {
      choices[service.subject] = choiceFor(service);
    });
    return { share_with_team: Boolean(current().shareWithTeam), choices };
  }

  /** One line for the Review page. */
  function summary() {
    if (!current().shareWithTeam) {
      return "Not shared: everything serves only your own work";
    }
    const shared = [];
    const kept = [];
    (modelChoice() === SHARED ? shared : kept).push(MODEL_KEYS_NAME);
    services().filter(service => !service.fixed).forEach(service => {
      (choiceFor(service) === SHARED ? shared : kept).push(service.display_name);
    });
    const parts = [];
    if (shared.length) {
      parts.push("Shared: " + shared.join(", "));
    }
    if (kept.length) {
      parts.push("Private: " + kept.join(", "));
    }
    return parts.join(". ");
  }

  function modelCaption() {
    const provider = String(sharedState.state.runtimeProvider || "").trim();
    const model = String(sharedState.state.runtimeModel || "").trim();
    const name = [provider, model].filter(Boolean).join(" / ");
    return (name ? name + ": " : "") + "the key you tested on Runtime Basics.";
  }

  /** The standard slider switch; ``attributes`` name it for screen readers. */
  function toggle(checked, disabled, attributes, onChange) {
    const wrap = document.createElement("label");
    wrap.className = "toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = checked;
    input.disabled = disabled;
    Object.entries(attributes).forEach(([name, value]) => input.setAttribute(name, value));
    if (onChange) {
      lifecycle.listen(input, "change", () => onChange(input.checked));
    }
    const knob = document.createElement("span");
    knob.setAttribute("aria-hidden", "true");
    wrap.appendChild(input);
    wrap.appendChild(knob);
    return wrap;
  }

  function row({ subject, name, caption, choice, fixed, words }) {
    const item = document.createElement("div");
    item.className = "agent-row sharing-row" + (fixed ? " is-locked" : "");
    item.setAttribute("data-subject", subject);
    const text = document.createElement("div");
    const title = document.createElement("p");
    title.className = "agent-title";
    title.textContent = name;
    const description = document.createElement("p");
    description.className = "agent-caption";
    description.textContent = caption;
    const state = document.createElement("p");
    state.className = "sharing-state";
    state.textContent = fixed ? FIXED_STATE_WORDS[choice] : words[choice];
    text.appendChild(title);
    if (caption) {
      text.appendChild(description);
    }
    text.appendChild(state);
    item.appendChild(text);
    item.appendChild(toggle(choice === SHARED, fixed, { "aria-label": "Share " + name + " with people you add" }, fixed ? null : shared => {
      setChoice(subject, shared);
      state.textContent = words[shared ? SHARED : PRIVATE];
    }));
    return item;
  }

  function renderCard() {
    const card = document.createElement("div");
    card.className = "shim-card choice-card account-sharing-card";
    const title = document.createElement("strong");
    title.id = "account-sharing-title";
    title.textContent = ACCOUNT_SHARING_TITLE;
    const attributes = {
      id: "account-sharing-enabled",
      "aria-labelledby": "account-sharing-title",
      "aria-describedby": "account-sharing-help",
    };
    const control = toggle(Boolean(current().shareWithTeam), false, attributes, checked => {
      current().shareWithTeam = checked;
      save();
      operations.renderCurrentPage();
    });
    const help = document.createElement("p");
    help.id = "account-sharing-help";
    help.className = "choice-note";
    help.textContent = ACCOUNT_SHARING_HELP;
    card.appendChild(title);
    card.appendChild(control);
    card.appendChild(help);
    return card;
  }

  function renderAccountSharing() {
    const section = document.createElement("section");
    section.className = "account-sharing";
    section.setAttribute("aria-label", "Who uses these accounts");
    section.appendChild(renderCard());
    if (!current().shareWithTeam) {
      return section;
    }
    const list = document.createElement("div");
    list.className = "status-list account-sharing-list";
    list.appendChild(row({
      subject: MODEL_KEYS_SUBJECT,
      name: MODEL_KEYS_NAME,
      caption: modelCaption(),
      choice: modelChoice(),
      fixed: false,
      words: MODEL_STATE_WORDS,
    }));
    services().forEach(service => {
      list.appendChild(row({
        subject: service.subject,
        name: String(service.display_name || service.subject),
        caption: service.fixed ? String(service.note || "") : String(service.description || ""),
        choice: choiceFor(service),
        fixed: Boolean(service.fixed),
        words: STATE_WORDS,
      }));
    });
    section.appendChild(list);
    return section;
  }

  return { applyServices, payload, renderAccountSharing, summary };
}

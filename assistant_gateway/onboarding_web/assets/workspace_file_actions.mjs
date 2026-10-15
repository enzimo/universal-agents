/**
 * Share… and Send… on the Workspace Files page (ui/DESIGN.md "Workspace
 * Files"), and the page rules that go with them.
 *
 * The file tree (workspace_browser.js, a classic script) reports each folder
 * listing here and asks which actions a row gets. This module owns:
 * - the person's own folder: the gateway lists only that folder under
 *   `users/`, so its name is learned from that listing (a folder name can be
 *   a hashed slug the browser cannot work out from the person's id);
 * - the page title: "Your files" when the top level lists only `users/`
 *   (members), "Workspace" when it also lists `team/` or `agents/`
 *   (administrators). The title is display only; the gateway decides access;
 * - Share (files and folders in the person's own folder): POST
 *   /api/web-chat/files/share `{path, person, level, hours}`. It gives that
 *   person, and agents working for them, access to the item. Sharing is
 *   stopped on the Access page (Files);
 * - Send (files only): POST /api/web-chat/files/send `{path, to, move}`. To a
 *   person it asks them, or an administrator, to approve (a delivery request
 *   in their Inbox) before the file arrives in their `received/` folder; to
 *   the team folder (`team` or `team/<folder>`) it arrives at once.
 * The person pickers read GET /api/web-chat/files/people, which already
 * leaves out the signed-in person. "Shared" chips come from the Access page's
 * Files view (`shared_by_you`).
 *
 * The browser only fills the forms. The gateway checks every request again
 * (own folder, team membership, level, destination) and its refusal is shown
 * as written. Writes carry the session's CSRF token. All server text is set
 * with textContent, never as HTML.
 *
 * Share… and Send… are buttons at the top right of the preview pane for the
 * selected item (workspace_browser.js). Dialogs follow the frosted modal
 * rules: titled with the item's name, focus stays inside while open, Escape
 * and Cancel close, and focus returns to the button that opened it.
 */

import { FILES_VIEW, accessMapPath } from "./access_map.mjs";
import { createSettingsClient, element, failureMessage, text } from "./settings_support.mjs";

export const FILES_API = "/api/web-chat/files";
export const FILES_PEOPLE_PATH = `${FILES_API}/people`;
export const FILES_SHARE_PATH = `${FILES_API}/share`;
export const FILES_SEND_PATH = `${FILES_API}/send`;
// Top-level folders below workspaces/ (runtime_settings.instance_layout).
export const USERS_FOLDER = "users";
export const TEAM_FOLDER = "team";

export const PAGE_NAME = "Workspace Files";
export const MEMBER_VIEW_TITLE = "Your files";
export const ADMIN_VIEW_TITLE = "Workspace";

export const SHARE_ACTION_LABEL = "Share…";
export const SEND_ACTION_LABEL = "Send…";
export const SHARED_CHIP_LABEL = "Shared";
export const SHARE_BUTTON_LABEL = "Share";
export const SEND_BUTTON_LABEL = "Send";
export const CANCEL_LABEL = "Cancel";

export const SHARE_LEVELS = Object.freeze([
  Object.freeze({ value: "read", label: "Read" }),
  Object.freeze({ value: "write", label: "Read and change" }),
]);
// `hours` is null until the person stops sharing; the gateway checks the range.
export const SHARE_DURATIONS = Object.freeze([
  Object.freeze({ value: "until_stopped", hours: null, label: "Until I stop sharing" }),
  Object.freeze({ value: "one_day", hours: 24, label: "1 day" }),
  Object.freeze({ value: "seven_days", hours: 168, label: "7 days" }),
]);
export const SEND_TO_PERSON = "person";
export const SEND_TO_TEAM = "team";
export const SEND_DESTINATIONS = Object.freeze([
  Object.freeze({ value: SEND_TO_PERSON, label: "To a person" }),
  Object.freeze({ value: SEND_TO_TEAM, label: "To the team folder" }),
]);
export const SEND_COPY = "copy";
export const SEND_MOVE = "move";
export const SEND_MODES = Object.freeze([
  Object.freeze({ value: SEND_COPY, label: "Copy" }),
  Object.freeze({ value: SEND_MOVE, label: "Move" }),
]);

export const SEND_NOTES = Object.freeze({
  [SEND_TO_PERSON]: "They, or an administrator, must approve before the file arrives in their received/ folder. "
    + "The request waits in their Inbox.",
  [SEND_TO_TEAM]: "The file arrives in the team folder at once.",
});

const NO_PEOPLE_TEXT = "Nobody else is in the team yet.";
const PEOPLE_FAILED_TEXT = "The team list could not be loaded. Close this dialog and try again.";

let idSequence = 0;
function uniqueId(prefix) {
  idSequence += 1;
  return `${prefix}-${idSequence}`;
}

export function pathParts(path) {
  return text(path).split("/").filter(Boolean);
}

/** "Your files" for members (only users/ at the top), "Workspace" for administrators. */
export function workspaceViewTitle(rootEntries) {
  const names = (Array.isArray(rootEntries) ? rootEntries : []).map(entry => text(entry && entry.name));
  return names.some(name => name && name !== USERS_FOLDER) ? ADMIN_VIEW_TITLE : MEMBER_VIEW_TITLE;
}

/**
 * Which actions a tree row offers: Share on files and folders inside the
 * person's own folder, Send on files there. The folder itself gets neither.
 */
export function rowActions(entry, ownFolders) {
  const parts = pathParts(entry && entry.path);
  const own = parts.length >= 3 && parts[0] === USERS_FOLDER && ownFolders.has(parts[1]);
  return { share: own, send: own && text(entry.kind) === "file" };
}

/** `team`, `team/reports` from what the person typed ("reports", "/team/reports/"). */
export function teamFolderTarget(value) {
  const cleaned = text(value).replace(/^\/+|\/+$/g, "");
  if (!cleaned || cleaned === TEAM_FOLDER) return TEAM_FOLDER;
  return cleaned.startsWith(`${TEAM_FOLDER}/`) ? cleaned : `${TEAM_FOLDER}/${cleaned}`;
}

export function shareRequestBody({ path, person, level, duration }) {
  const chosen = SHARE_DURATIONS.find(item => item.value === duration) || SHARE_DURATIONS[0];
  return {
    path: text(path),
    person: text(person),
    level: SHARE_LEVELS.some(item => item.value === level) ? level : SHARE_LEVELS[0].value,
    hours: chosen.hours,
  };
}

export function sendRequestBody({ path, destination, person, teamFolder, mode }) {
  return {
    path: text(path),
    to: destination === SEND_TO_TEAM ? teamFolderTarget(teamFolder) : text(person),
    move: mode === SEND_MOVE,
  };
}

export function setMessage(node, value, isError) {
  node.textContent = value || "";
  node.hidden = !value;
  node.classList.toggle("is-error", Boolean(value && isError));
}

function labeledField(document, label, control, help) {
  const field = element(document, "label", "memory-editor-field");
  field.append(element(document, "span", "memory-editor-label", label), control);
  if (help) field.append(element(document, "small", "memory-editor-help", help));
  return field;
}

/** A labelled group of radio buttons drawn as the shared segmented control. */
function choiceGroup(document, legend, options, checked) {
  const fieldset = element(document, "fieldset", "workspace-file-fieldset");
  fieldset.append(element(document, "legend", "memory-editor-label", legend));
  const group = element(document, "div", "segmented-control workspace-file-choices");
  const name = uniqueId("workspace-file-choice");
  const inputs = options.map(option => {
    const label = element(document, "label", "segment-option");
    const input = element(document, "input");
    input.type = "radio";
    input.name = name;
    input.value = option.value;
    input.checked = option.value === checked;
    label.append(input, element(document, "span", "", option.label));
    group.append(label);
    return input;
  });
  fieldset.append(group);
  return {
    fieldset,
    inputs,
    value: () => (inputs.find(input => input.checked) || inputs[0]).value,
    choose(value) { inputs.forEach(input => { input.checked = input.value === value; }); },
  };
}

/** The team member picker; filled once the people list arrives. */
function personPicker(document) {
  const select = element(document, "select", "memory-editor-input workspace-file-person");
  const loading = element(document, "option", "", "Loading people…");
  loading.value = "";
  select.append(loading);
  select.disabled = true;
  return {
    select,
    fill(people) {
      const rows = (Array.isArray(people) ? people : []).filter(person => text(person && person.person_id));
      if (!rows.length) {
        const none = element(document, "option", "", NO_PEOPLE_TEXT);
        none.value = "";
        select.replaceChildren(none);
        select.disabled = true;
        return false;
      }
      const choose = element(document, "option", "", "Choose a person");
      choose.value = "";
      select.replaceChildren(choose, ...rows.map(person => {
        const option = element(document, "option", "", text(person.name) || text(person.person_id));
        option.value = text(person.person_id);
        return option;
      }));
      select.value = "";
      select.disabled = false;
      return true;
    },
  };
}

/** The frosted modal frame both dialogs share. */
function dialogShell(document, { kicker, title, intro, submitLabel }) {
  const overlay = element(document, "section", "setup-overlay workspace-file-overlay");
  overlay.setAttribute("aria-hidden", "false");
  const dialog = element(document, "div", "setup-dialog workspace-file-dialog");
  const titleId = uniqueId("workspace-file-title");
  const introId = uniqueId("workspace-file-intro");
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", titleId);
  dialog.setAttribute("aria-describedby", introId);
  const heading = element(document, "h2", "workspace-file-title", title);
  heading.id = titleId;
  const introNode = element(document, "p", "setup-intro workspace-file-intro", intro);
  introNode.id = introId;
  const form = element(document, "form", "workspace-file-form");
  const message = element(document, "p", "access-inbox-card-message workspace-file-message");
  message.setAttribute("role", "status");
  message.setAttribute("aria-live", "polite");
  message.hidden = true;
  const actions = element(document, "div", "permission-choice-row workspace-file-dialog-actions");
  const submit = element(document, "button", "button primary permission-choice-button", submitLabel);
  submit.type = "submit";
  const cancel = element(document, "button", "button ghost permission-choice-button", CANCEL_LABEL);
  cancel.type = "button";
  actions.append(submit, cancel);
  dialog.append(element(document, "p", "page-kicker", kicker), heading, introNode, form);
  overlay.append(dialog);
  return { overlay, dialog, form, message, actions, submit, cancel };
}

function bindForm(shell, { onSubmit, onCancel, collect }) {
  shell.cancel.addEventListener("click", () => {
    if (!shell.cancel.disabled && typeof onCancel === "function") onCancel();
  });
  shell.form.addEventListener("submit", event => {
    if (event && typeof event.preventDefault === "function") event.preventDefault();
    if (shell.submit.disabled || typeof onSubmit !== "function") return;
    const values = collect();
    if (!values) return;
    onSubmit({ values, submit: shell.submit, group: shell.actions, message: shell.message });
  });
}

/**
 * The Share dialog for one file or folder. `onSubmit` receives `{ values,
 * submit, group, message }` with the request body; `fillPeople(people)` or
 * `peopleFailed()` finishes the picker.
 */
export function renderShareDialog(document, entry, { onSubmit, onCancel } = {}) {
  const name = text(entry && entry.name);
  const isFile = text(entry && entry.kind) === "file";
  const shell = dialogShell(document, {
    kicker: "Share",
    title: name,
    intro: `The person you choose, and agents working for them, can use ${isFile ? "this file" : "this folder and everything in it"} `
      + "at the level you pick. You can stop sharing at any time in Settings → Access → Files.",
    submitLabel: SHARE_BUTTON_LABEL,
  });
  const picker = personPicker(document);
  const levels = choiceGroup(document, "Level", SHARE_LEVELS, SHARE_LEVELS[0].value);
  const durations = choiceGroup(document, "How long", SHARE_DURATIONS, SHARE_DURATIONS[0].value);
  shell.form.append(labeledField(document, "Person", picker.select, ""), levels.fieldset, durations.fieldset,
    shell.message, shell.actions);
  shell.submit.disabled = true;
  bindForm(shell, {
    onSubmit, onCancel,
    collect: () => {
      if (!text(picker.select.value)) {
        setMessage(shell.message, "Choose who to share with.", true);
        picker.select.focus();
        return null;
      }
      return shareRequestBody({
        path: entry.path, person: picker.select.value, level: levels.value(), duration: durations.value(),
      });
    },
  });
  return {
    ...shell,
    fields: { person: picker.select, levels: levels.inputs, durations: durations.inputs },
    fillPeople(people) {
      const ready = picker.fill(people);
      shell.submit.disabled = !ready;
      if (!ready) setMessage(shell.message, NO_PEOPLE_TEXT, false);
      return ready;
    },
    peopleFailed(reason) {
      picker.fill([]);
      shell.submit.disabled = true;
      setMessage(shell.message, reason || PEOPLE_FAILED_TEXT, true);
    },
  };
}

/**
 * The Send dialog for one file: to a person (after their or an
 * administrator's approval) or to the team folder (at once), as a copy or a
 * move. `onSubmit` receives `{ values, submit, group, message }`.
 */
export function renderSendDialog(document, entry, { onSubmit, onCancel } = {}) {
  const shell = dialogShell(document, {
    kicker: "Send",
    title: text(entry && entry.name),
    intro: "Put a copy of this file into someone else's folder or the team folder, or move it there.",
    submitLabel: SEND_BUTTON_LABEL,
  });
  const destinations = choiceGroup(document, "Send to", SEND_DESTINATIONS, SEND_TO_PERSON);
  const picker = personPicker(document);
  const personField = labeledField(document, "Person", picker.select, "");
  const teamFolder = element(document, "input", "memory-editor-input workspace-file-team-folder");
  teamFolder.type = "text";
  teamFolder.maxLength = 1024;
  teamFolder.value = TEAM_FOLDER;
  const teamField = labeledField(document, "Team folder", teamFolder,
    "The team folder or a folder in it, such as team/reports. A missing folder is created.");
  const note = element(document, "p", "workspace-file-note");
  note.setAttribute("aria-live", "polite");
  const modes = choiceGroup(document, "Copy or move", SEND_MODES, SEND_COPY);
  const modeHelp = element(document, "small", "memory-editor-help", "Move removes your copy once the file arrives.");
  modes.fieldset.append(modeHelp);
  shell.form.append(destinations.fieldset, personField, teamField, note, modes.fieldset, shell.message, shell.actions);
  let peopleReady = false;

  function sync() {
    const toTeam = destinations.value() === SEND_TO_TEAM;
    personField.hidden = toTeam;
    teamField.hidden = !toTeam;
    note.textContent = SEND_NOTES[toTeam ? SEND_TO_TEAM : SEND_TO_PERSON];
    shell.submit.disabled = !toTeam && !peopleReady;
  }
  destinations.inputs.forEach(input => input.addEventListener("change", sync));
  sync();

  bindForm(shell, {
    onSubmit, onCancel,
    collect: () => {
      const destination = destinations.value();
      if (destination === SEND_TO_PERSON && !text(picker.select.value)) {
        setMessage(shell.message, "Choose who to send it to.", true);
        picker.select.focus();
        return null;
      }
      return sendRequestBody({
        path: entry.path, destination, person: picker.select.value, teamFolder: teamFolder.value, mode: modes.value(),
      });
    },
  });
  return {
    ...shell,
    fields: { destinations: destinations.inputs, person: picker.select, teamFolder, modes: modes.inputs },
    personField,
    teamField,
    note,
    /** Chooses a destination as a click would (for keyboard shortcuts and tests). */
    chooseDestination(value) {
      destinations.choose(value);
      sync();
    },
    fillPeople(people) {
      peopleReady = picker.fill(people);
      sync();
      return peopleReady;
    },
    peopleFailed(reason) {
      picker.fill([]);
      peopleReady = false;
      sync();
      setMessage(shell.message, reason || PEOPLE_FAILED_TEXT, true);
    },
  };
}

/**
 * Keyboard stops inside the dialog, in order, skipping hidden parts and
 * disabled controls. A radio group is one stop: its checked button.
 */
export function focusStops(root) {
  const stops = [];
  const walk = node => {
    (node.children ? Array.from(node.children) : []).forEach(child => {
      if (child.hidden) return;
      const tag = child.tagName;
      const control = tag === "BUTTON" || tag === "SELECT" || tag === "TEXTAREA" || tag === "INPUT";
      if (control && !child.disabled && !(tag === "INPUT" && child.type === "radio" && !child.checked)) {
        stops.push(child);
      }
      walk(child);
    });
  };
  walk(root);
  return stops;
}

/**
 * The page's Share and Send actions. `getCsrfToken()` returns the signed-in
 * session's token for writes; `onUnauthorized(response)` signs the page out
 * on 401. `browser` supplies `document`, `fetch`, `console`,
 * `requestAnimationFrame` and `UAActionFeedback`.
 */
export function createWorkspaceFileActions({ browser = globalThis, onUnauthorized, getCsrfToken = () => "" } = {}) {
  const document = browser.document;
  const client = createSettingsClient({ browser, onUnauthorized });
  const ownFolders = new Set();
  let open = null;

  function post(path, body, fallback) {
    client.setCsrfToken(getCsrfToken());
    return client.post(path, body, fallback);
  }

  function close({ restoreFocus = true } = {}) {
    if (!open) return;
    const current = open;
    open = null;
    document.removeEventListener("keydown", current.onKeydown);
    current.view.overlay.remove();
    if (restoreFocus && current.returnFocus && current.returnFocus.isConnected) current.returnFocus.focus();
  }

  function show(view, returnFocus) {
    close({ restoreFocus: false });
    const current = { view, returnFocus, busy: false, onKeydown: null, initialFocus: null };
    current.onKeydown = event => {
      if (open !== current) return;
      if (event.key === "Escape") {
        if (typeof event.preventDefault === "function") event.preventDefault();
        if (!current.busy) close();
        return;
      }
      if (event.key !== "Tab") return;
      // Keep keyboard focus inside the dialog while it is open.
      const stops = focusStops(view.dialog);
      if (!stops.length) return;
      const first = stops[0];
      const last = stops[stops.length - 1];
      const active = document.activeElement;
      const inside = view.dialog.contains(active);
      if (event.shiftKey && (active === first || !inside)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !inside)) {
        event.preventDefault();
        first.focus();
      }
    };
    view.overlay.addEventListener("click", event => {
      if (event.target === view.overlay && open === current && !current.busy) close();
    });
    document.addEventListener("keydown", current.onKeydown);
    document.body.append(view.overlay);
    open = current;
    browser.requestAnimationFrame(() => {
      if (open !== current) return;
      view.overlay.classList.add("is-open");
    });
    const stops = focusStops(view.dialog);
    current.initialFocus = stops[0] || null;
    if (current.initialFocus) current.initialFocus.focus();
    return current;
  }

  /**
   * Fills the person picker. With `focusPicker`, focus moves to it once it
   * can be used, unless the person already moved elsewhere in the dialog.
   */
  function loadPeople(view, current, { focusPicker = false } = {}) {
    return client.get(FILES_PEOPLE_PATH, PEOPLE_FAILED_TEXT)
      .then(payload => {
        if (open !== current) return;
        const untouched = document.activeElement === current.initialFocus;
        view.fillPeople(payload && payload.people);
        if (focusPicker && untouched && !view.fields.person.disabled) view.fields.person.focus();
      })
      .catch(error => {
        if (open !== current) return;
        view.peopleFailed(failureMessage(error, PEOPLE_FAILED_TEXT));
      });
  }

  /** Sends a dialog's request; on success the dialog closes and `onDone` gets the gateway's words. */
  function submitter(current, { path, busyLabel, fallback, done }) {
    return ({ values, submit, group, message }) => {
      const finish = client.beginAction(submit, busyLabel, group);
      current.busy = true;
      setMessage(message, "", false);
      return post(path, values, fallback)
        .then(payload => {
          finish();
          current.busy = false;
          close();
          done(values, payload || {});
        }, error => {
          // The dialog stays open with the person's choices so they can retry.
          finish();
          current.busy = false;
          setMessage(message, failureMessage(error, fallback), true);
        });
    };
  }

  function openShare({ entry, returnFocus, onDone }) {
    const holder = {};
    const view = renderShareDialog(document, entry, {
      onCancel: () => close(),
      onSubmit: parts => holder.submit(parts),
    });
    const current = show(view, returnFocus);
    holder.submit = submitter(current, {
      path: FILES_SHARE_PATH, busyLabel: "Sharing…", fallback: `${text(entry.name)} was not shared. Try again.`,
      done: (_values, payload) => {
        if (typeof onDone === "function") {
          onDone({ message: text(payload.message) || `Shared ${text(entry.name)}.`, shared: true, share: payload.share || null });
        }
      },
    });
    loadPeople(view, current, { focusPicker: true });
    return view;
  }

  function openSend({ entry, returnFocus, onDone }) {
    const holder = {};
    const view = renderSendDialog(document, entry, {
      onCancel: () => close(),
      onSubmit: parts => holder.submit(parts),
    });
    const current = show(view, returnFocus);
    holder.submit = submitter(current, {
      path: FILES_SEND_PATH, busyLabel: "Sending…", fallback: `${text(entry.name)} was not sent. Try again.`,
      done: (values, payload) => {
        if (typeof onDone === "function") {
          const toTeam = values.to === TEAM_FOLDER || values.to.startsWith(`${TEAM_FOLDER}/`);
          // A move to the team folder removes the original at once; to a person only after approval.
          onDone({ message: text(payload.message) || `Sent ${text(entry.name)}.`, movedAway: values.move && toTeam });
        }
      },
    });
    loadPeople(view, current);
    return view;
  }

  /** The items this person shares now (their paths below workspaces/), for the tree's "Shared" chips. */
  function sharedPaths() {
    return client.get(accessMapPath(FILES_VIEW, 0), "Your shares could not be read.")
      .then(payload => new Set((Array.isArray(payload && payload.shared_by_you) ? payload.shared_by_you : [])
        .map(share => text(share && share.target)).filter(Boolean)))
      .catch(error => {
        // The chips are a convenience; the tree works without them.
        if (browser.console && typeof browser.console.warn === "function") {
          browser.console.warn("Workspace Files could not read your shares; Shared chips are not shown.", error);
        }
        return new Set();
      });
  }

  return {
    labels: Object.freeze({
      share: SHARE_ACTION_LABEL, send: SEND_ACTION_LABEL, shared: SHARED_CHIP_LABEL, pageName: PAGE_NAME,
      memberTitle: MEMBER_VIEW_TITLE,
    }),
    /** Records a folder listing; the users/ listing names the person's own folder. */
    noteListing(path, entries) {
      if (text(path) !== USERS_FOLDER) return;
      (Array.isArray(entries) ? entries : []).forEach(entry => {
        if (text(entry && entry.kind) === "directory" && text(entry.name)) ownFolders.add(text(entry.name));
      });
    },
    actionsFor: entry => rowActions(entry, ownFolders),
    viewTitle: workspaceViewTitle,
    sharedPaths,
    openShare,
    openSend,
    close,
    isOpen: () => Boolean(open),
    /** Sign-out: close any dialog and forget the person's folder. */
    reset() {
      close({ restoreFocus: false });
      ownFolders.clear();
    },
  };
}

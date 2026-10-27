/**
 * Settings > Team > File Retention: how long the team keeps chat uploads,
 * scratch files and saved tool outputs (assistant_gateway.file_retention).
 *
 * Every signed-in member sees the numbers. The form appears only when the
 * gateway says this person may change them (`can_change`, from their current
 * roles), never from client state. Chat uploads follow a change at the
 * gateway's next cleanup, within six hours; the manager reads scratch and
 * saved tool output retention when it starts, so those numbers apply after
 * the next restart.
 *
 * All server text is set with textContent, never as HTML.
 */

import { element, failureMessage, setStatusLine, text } from "./settings_support.mjs";

export const FILE_RETENTION_API = "/api/web-chat/file-retention";
export const FILE_RETENTION_TITLE = "File Retention";
export const FILE_RETENTION_INTRO = "How long the team keeps files people send in chat, the temporary "
  + "files agents leave in scratch folders (which include copies of files sent to people), and the "
  + "tool outputs agents save while they work. Older files are deleted; conversations themselves are kept. "
  + "0 keeps them forever.";
export const FILE_RETENTION_ADMIN_ONLY = "Only administrators change how long files are kept.";
export const FILE_RETENTION_SAVED = "Saved. Chat uploads follow the new number at the next cleanup, "
  + "within six hours. Scratch files and saved tool outputs follow it after the next restart.";
export const SAVED_OUTPUTS_HELP = "Large tool results, long tool output from conversations, research results "
  + "and run folders. Applies after the next restart.";
const LOAD_FAILED = "File retention could not be loaded because the gateway returned no explanation. "
  + "Select Refresh to try again.";
const SAVE_FAILED = "File retention was not saved. Check the numbers and try again.";
const DEFAULT_MAX_DAYS = 3650;

let idSequence = 0;
function uniqueId(prefix) {
  idSequence += 1;
  return `${prefix}-${idSequence}`;
}

/** "Kept forever", "1 day" or "90 days". */
export function retentionText(days) {
  const value = Number(days);
  if (value === 0) return "Kept forever";
  return `${value} ${value === 1 ? "day" : "days"}`;
}

/** A whole number of days from 0 to `max`, or null. */
export function parsedDays(value, max) {
  const raw = text(value);
  if (!/^\d+$/.test(raw)) return null;
  const days = Number(raw);
  return days <= max ? days : null;
}

function labeledField(document, label, control, help) {
  const field = element(document, "label", "memory-editor-field");
  field.append(element(document, "span", "memory-editor-label", label), control);
  if (help) field.append(element(document, "small", "memory-editor-help", help));
  return field;
}

function daysInput(document, value, max) {
  const input = element(document, "input", "memory-editor-input file-retention-days");
  input.type = "number";
  input.min = "0";
  input.max = String(max);
  input.step = "1";
  input.inputMode = "numeric";
  input.value = String(value);
  return input;
}

/**
 * Connects the File Retention section. `client` is the Settings client from
 * settings_support.mjs. The Team panel calls `load()` with each refresh and
 * `clear()` at sign-out.
 */
export function createFileRetentionControls({ container, client, browser = globalThis }) {
  const document = browser.document;
  const state = { version: 0 };

  const title = element(document, "h4", "settings-subsection-title", FILE_RETENTION_TITLE);
  title.id = uniqueId("settings-file-retention-title");
  container.setAttribute("aria-labelledby", title.id);
  const status = element(document, "p", "settings-status file-retention-status");
  status.setAttribute("aria-live", "polite");
  status.hidden = true;
  const body = element(document, "div", "file-retention-content");
  container.replaceChildren(title, element(document, "p", "settings-subsection-copy", FILE_RETENTION_INTRO),
    status, body);

  // ui/DESIGN.md: a failure is an alert, a success a neutral status.
  function setStatus(message, isError) {
    status.setAttribute("role", message && isError ? "alert" : "status");
    setStatusLine(status, message, isError);
  }

  function readOnlyView(payload) {
    const rows = element(document, "dl", "file-retention-values");
    rows.append(
      element(document, "dt", "", "Chat uploads"), element(document, "dd", "", retentionText(payload.upload_days)),
      element(document, "dt", "", "Scratch files"), element(document, "dd", "", retentionText(payload.scratch_days)),
      element(document, "dt", "", "Saved tool outputs"), element(document, "dd", "", retentionText(payload.output_days)),
    );
    return [rows, element(document, "p", "settings-subsection-copy", FILE_RETENTION_ADMIN_ONLY)];
  }

  function form(payload) {
    const max = Number(payload.max_days) || DEFAULT_MAX_DAYS;
    const node = element(document, "form", "file-retention-form");
    node.setAttribute("aria-label", "File retention");
    const uploads = daysInput(document, payload.upload_days, max);
    const scratch = daysInput(document, payload.scratch_days, max);
    const outputs = daysInput(document, payload.output_days, max);
    const group = element(document, "div", "permission-choice-row file-retention-actions");
    const submit = element(document, "button", "button primary permission-choice-button", "Save");
    submit.type = "submit";
    group.append(submit);
    node.append(
      labeledField(document, "Days to keep chat uploads", uploads, "Files people send in any chat app."),
      labeledField(document, "Days to keep scratch files", scratch, "Applies after the next restart."),
      labeledField(document, "Days to keep saved tool outputs", outputs, SAVED_OUTPUTS_HELP),
      group,
    );
    node.addEventListener("submit", event => {
      if (event && typeof event.preventDefault === "function") event.preventDefault();
      if (submit.disabled) return;
      const values = {
        upload_days: parsedDays(uploads.value, max),
        scratch_days: parsedDays(scratch.value, max),
        output_days: parsedDays(outputs.value, max),
      };
      const wrong = values.upload_days === null ? uploads
        : values.scratch_days === null ? scratch
          : values.output_days === null ? outputs : null;
      if (wrong) {
        setStatus(`Enter a whole number of days from 0 (keep forever) to ${max}. Nothing was saved.`, true);
        wrong.focus();
        return;
      }
      save(values, submit, group);
    });
    return node;
  }

  function render(payload) {
    body.replaceChildren(...(payload.can_change === true ? [form(payload)] : readOnlyView(payload)));
  }

  function save(values, submit, group) {
    const version = state.version;
    const finish = client.beginAction(submit, "Saving…", group);
    setStatus("", false);
    return Promise.resolve()
      .then(() => client.send("PUT", FILE_RETENTION_API, values, SAVE_FAILED))
      .then(payload => {
        finish();
        if (version !== state.version) return;
        render(payload);
        setStatus(FILE_RETENTION_SAVED, false);
      }, error => {
        // The typed numbers stay so the person can correct and save again.
        finish();
        if (version !== state.version) return;
        setStatus(failureMessage(error, SAVE_FAILED), true);
      });
  }

  function load() {
    state.version += 1;
    const version = state.version;
    return Promise.resolve()
      .then(() => client.get(FILE_RETENTION_API, LOAD_FAILED))
      .then(payload => {
        if (version !== state.version) return;
        setStatus("", false);
        render(payload || {});
      }, error => {
        if (version !== state.version) return;
        body.replaceChildren();
        setStatus(failureMessage(error, LOAD_FAILED), true);
      });
  }

  return {
    load,
    resetStatus() { setStatus("", false); },
    /** Sign-out: forget the numbers and ignore replies to earlier reads. */
    clear() {
      state.version += 1;
      body.replaceChildren();
      setStatus("", false);
    },
  };
}

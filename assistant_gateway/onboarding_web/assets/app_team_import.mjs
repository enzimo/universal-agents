// Let's Begin > Import a team export (onboarding_web/team_import.py, guided
// upgrade phase 1). The operator copies an export into the new data folder's
// import/ folder; this panel lists those files, takes the export's passphrase
// and opens the export. A correct passphrase returns a private token, and the
// browser continues on the import page (team_import.html) with that token in
// the address's fragment. The passphrase lives only in this closure while
// the person types it: never in localStorage, and it is cleared once sent.
// Failures show as a red alert in the panel (ui/DESIGN.md, Errors and Results).
//
// A team whose id this version cannot carry (owner's decision 103): the open
// reply carries a question instead, and the panel asks for a new team id,
// starting from the suggestion or the id chosen when the export was opened
// before. Continue sends it with the token (kept in this closure, like the
// passphrase) to /api/team-import/team-id, which prepares the review under it;
// a refused id shows by the field and keeps what was typed.

export function formatExportSize(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1024 ** 3) return (value / 1024 ** 3).toFixed(1) + " GB";
  if (value >= 1024 ** 2) return (value / 1024 ** 2).toFixed(1) + " MB";
  return Math.max(1, Math.round(value / 1024)) + " KB";
}

export function createTeamImport({ sharedState, lifecycle, requestClient, operations }) {
  let passphrase = "";
  let opened = null; // { token, page } of an export waiting for its new team id
  const state = () => sharedState.state.teamImport;

  function clearQuestion() {
    const teamImport = state();
    opened = null;
    teamImport.question = null;
    teamImport.newTeamId = "";
    teamImport.teamIdError = "";
    teamImport.choosing = false;
  }

  async function reply(response, fallback) {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw Object.assign(new Error(payload.error || fallback), { field: payload.field });
    return payload;
  }

  const reviewUrl = (page, token) => String(page || "/team-import") + "#" + encodeURIComponent(String(token || ""));

  function loadExports() {
    const teamImport = state();
    teamImport.loading = true;
    teamImport.message = "";
    teamImport.failed = false;
    operations.renderCurrentPage();
    return requestClient.fetch("/api/team-import/exports", { cache: "no-store" }).then(response => {
      if (!response.ok) throw new Error("Could not list the export files.");
      return response.json();
    }).then(payload => {
      teamImport.exports = Array.isArray(payload.exports) ? payload.exports : [];
      teamImport.importDir = String(payload.import_dir || "");
      teamImport.problem = String(payload.problem || "");
      const names = teamImport.exports.map(item => item.file_name);
      if (!names.includes(teamImport.selected)) teamImport.selected = names.length === 1 ? names[0] : "";
    }).catch(error => {
      teamImport.message = "The export files could not be listed. " + (error.message || "") + " Select Look again.";
      teamImport.failed = true;
    }).finally(() => {
      teamImport.loading = false;
      teamImport.loaded = true;
      operations.renderCurrentPage();
    });
  }

  function open() {
    const teamImport = state();
    if (teamImport.opening || !teamImport.selected) return Promise.resolve();
    const sent = passphrase;
    passphrase = "";
    clearQuestion();
    teamImport.opening = true;
    teamImport.failed = false;
    teamImport.message = "Checking the passphrase and the export. This takes a few seconds...";
    operations.renderCurrentPage();
    return requestClient.fetch("/api/team-import/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_name: teamImport.selected, passphrase: sent }),
    }).then(response => reply(response, "Could not open the export.")).then(payload => {
      if (payload.team_id_question) {
        opened = { token: payload.token, page: payload.page };
        teamImport.question = payload.team_id_question;
        teamImport.newTeamId = String(payload.team_id_question.suggestion || "");
        teamImport.opening = false;
        teamImport.message = "";
        operations.renderCurrentPage();
        return;
      }
      operations.navigate(reviewUrl(payload.page, payload.token));
    }).catch(error => {
      teamImport.opening = false;
      teamImport.failed = true;
      teamImport.message = "The export was not opened. " + (error.message || "Try again.");
      operations.renderCurrentPage();
    });
  }

  function chooseTeamId() {
    const teamImport = state();
    if (teamImport.choosing || !opened) return Promise.resolve();
    teamImport.choosing = true;
    teamImport.teamIdError = "";
    teamImport.failed = false;
    teamImport.message = "Preparing the review under the new team id...";
    operations.renderCurrentPage();
    return requestClient.fetch("/api/team-import/team-id", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Transfer-Token": opened.token },
      body: JSON.stringify({ team_id: teamImport.newTeamId }),
    }).then(response => reply(response, "Could not prepare the review.")).then(payload => {
      operations.navigate(reviewUrl(payload.page || opened.page, opened.token));
    }).catch(error => {
      teamImport.choosing = false;
      teamImport.message = "";
      if (error.field === "team_id") {
        teamImport.teamIdError = "This id was not used. " + error.message;
      } else {
        teamImport.failed = true;
        teamImport.message = "The review was not prepared. " + (error.message || "Try again.");
      }
      operations.renderCurrentPage();
    });
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderExport(item) {
    const teamImport = state();
    const label = element("label", "start-option" + (teamImport.selected === item.file_name ? " is-selected" : ""));
    const input = element("input");
    input.type = "radio";
    input.name = "team-import-export";
    input.value = item.file_name;
    input.checked = teamImport.selected === item.file_name;
    lifecycle.listen(input, "change", () => {
      teamImport.selected = item.file_name;
      operations.renderCurrentPage();
    });
    const copied = String(item.modified_at_utc || "").slice(0, 16).replace("T", " ");
    label.append(input, element("strong", "", `${item.file_name} · ${formatExportSize(item.size_bytes)} · copied ${copied} UTC`));
    return label;
  }

  function renderPassphrase() {
    const field = element("div", "field");
    const label = element("label", "", "Export passphrase");
    label.htmlFor = "team-import-passphrase";
    const input = element("input", "input");
    input.id = "team-import-passphrase";
    input.type = "password";
    input.autocomplete = "off";
    input.value = passphrase;
    lifecycle.listen(input, "input", () => { passphrase = input.value; });
    lifecycle.listen(input, "keydown", event => { if (event.key === "Enter") open(); });
    field.append(label, operations.renderRevealableControl(input));
    return field;
  }

  function renderTeamIdQuestion() {
    const teamImport = state();
    const question = teamImport.question;
    const card = element("div", "team-import-team-id");
    card.append(element("p", "choice-note", question.explanation));
    if (question.chosen_before) {
      card.append(element("p", "choice-note",
        `You chose ${question.chosen_before} when you opened this export before. Keep it to keep the selections `
        + "and edits you made; another id starts the review again with everything selected."));
    }
    const field = element("div", "field");
    const label = element("label", "", "New team id");
    label.htmlFor = "team-import-team-id";
    const input = element("input", "input");
    input.id = "team-import-team-id";
    input.type = "text";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.value = teamImport.newTeamId;
    lifecycle.listen(input, "input", () => { teamImport.newTeamId = input.value; });
    lifecycle.listen(input, "keydown", event => { if (event.key === "Enter") chooseTeamId(); });
    field.append(label, input);
    if (teamImport.teamIdError) {
      input.setAttribute("aria-invalid", "true");
      input.setAttribute("aria-describedby", "team-import-team-id-error");
      const error = element("p", "field-error", teamImport.teamIdError);
      error.id = "team-import-team-id-error";
      error.setAttribute("role", "alert");
      field.append(error);
    }
    const back = element("button", "button secondary", "Choose another export");
    back.type = "button";
    back.disabled = teamImport.choosing;
    lifecycle.listen(back, "click", () => { clearQuestion(); teamImport.message = ""; operations.renderCurrentPage(); });
    const next = element("button", "button primary", teamImport.choosing ? "Preparing..." : "Continue");
    next.type = "button";
    next.disabled = teamImport.choosing;
    lifecycle.listen(next, "click", chooseTeamId);
    card.append(field, back, next);
    return card;
  }

  function renderMessage(panel) {
    const teamImport = state();
    if (!teamImport.message) return;
    const message = element("p", teamImport.failed ? "warning-callout" : "begin-import-message", teamImport.message);
    message.setAttribute("role", teamImport.failed ? "alert" : "status");
    panel.append(message);
  }

  function renderPanel() {
    const teamImport = state();
    if (!teamImport.loaded && !teamImport.loading) lifecycle.timeout(loadExports, 0);
    const panel = element("div", "shim-card team-import-panel");
    panel.append(element("p", "choice-note",
      "Export the team from the old instance first, then copy the export file into this folder on the host: "
      + (teamImport.importDir || "the import folder of this instance's data folder")
      + ". Choose it below and type the passphrase you chose for the export."));
    if (teamImport.problem) {
      const problem = element("p", "field-error", teamImport.problem);
      problem.setAttribute("role", "alert");
      panel.append(problem);
    }
    if (teamImport.question) {
      panel.append(renderTeamIdQuestion());
      renderMessage(panel);
      return panel;
    }
    const list = element("div", "begin-options team-import-exports");
    list.setAttribute("role", "radiogroup");
    list.setAttribute("aria-label", "Export files");
    teamImport.exports.forEach(item => list.append(renderExport(item)));
    if (!teamImport.exports.length) {
      list.append(element("p", "choice-note", teamImport.loading ? "Looking for export files..." : "No export files are in the import folder yet."));
    }
    const refresh = element("button", "button secondary", "Look again");
    refresh.type = "button";
    refresh.disabled = teamImport.loading;
    lifecycle.listen(refresh, "click", loadExports);
    const openButton = element("button", "button primary", teamImport.opening ? "Opening..." : "Open export");
    openButton.type = "button";
    openButton.disabled = teamImport.opening || !teamImport.selected || Boolean(teamImport.problem);
    lifecycle.listen(openButton, "click", open);
    panel.append(list, refresh, renderPassphrase(), openButton);
    renderMessage(panel);
    return panel;
  }

  return { chooseTeamId, loadExports, open, renderPanel };
}

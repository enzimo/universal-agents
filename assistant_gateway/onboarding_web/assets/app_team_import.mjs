// Let's Begin > Import a team export (onboarding_web/team_import.py, guided
// upgrade phase 1). The operator copies an export into the new data folder's
// import/ folder; this panel lists those files, takes the export's passphrase
// and opens the export. A correct passphrase returns a private token, and the
// browser continues on the import page (team_import.html) with that token in
// the address's fragment. The passphrase lives only in this closure while
// the person types it: never in localStorage, and it is cleared once sent.
// Failures show as a red alert in the panel (ui/DESIGN.md, Errors and Results).

export function formatExportSize(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1024 ** 3) return (value / 1024 ** 3).toFixed(1) + " GB";
  if (value >= 1024 ** 2) return (value / 1024 ** 2).toFixed(1) + " MB";
  return Math.max(1, Math.round(value / 1024)) + " KB";
}

export function createTeamImport({ sharedState, lifecycle, requestClient, operations }) {
  let passphrase = "";
  const state = () => sharedState.state.teamImport;

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
    teamImport.opening = true;
    teamImport.failed = false;
    teamImport.message = "Checking the passphrase and the export. This takes a few seconds...";
    operations.renderCurrentPage();
    return requestClient.fetch("/api/team-import/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_name: teamImport.selected, passphrase: sent }),
    }).then(response => response.json().catch(() => ({})).then(payload => {
      if (!response.ok) throw new Error(payload.error || "Could not open the export.");
      return payload;
    })).then(payload => {
      operations.navigate(String(payload.page || "/team-import") + "#" + encodeURIComponent(String(payload.token || "")));
    }).catch(error => {
      teamImport.opening = false;
      teamImport.failed = true;
      teamImport.message = "The export was not opened. " + (error.message || "Try again.");
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
    if (teamImport.message) {
      const message = element("p", teamImport.failed ? "warning-callout" : "begin-import-message", teamImport.message);
      message.setAttribute("role", teamImport.failed ? "alert" : "status");
      panel.append(message);
    }
    return panel;
  }

  return { loadExports, open, renderPanel };
}

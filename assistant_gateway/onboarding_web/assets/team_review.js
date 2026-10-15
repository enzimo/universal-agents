"use strict";
// The team import review (team_transfer.review_session), shared by the
// operator's loopback page (team_transfer/review_assets/review.html) and
// onboarding's Import page (team_import.html). The page names its API with
// data-review-api on <body>; the private token arrives in the address's
// fragment, is sent only as the X-Transfer-Token header and is never kept in
// browser storage (ui/DESIGN.md). After a reload the person opens the export
// again; the review folder keeps their selections and edits.
(() => {
  const token = decodeURIComponent(location.hash.slice(1));
  history.replaceState(null, "", location.pathname);
  const api = (document.body && document.body.dataset && document.body.dataset.reviewApi) || "/api";
  const byId = id => document.getElementById(id);
  let records = [], sections = [], destinationChoice = null, digest = null, busy = false, revision = 0, pendingSaves = 0;
  let importing = false;
  let saveQueue = Promise.resolve();
  const dirty = new Set();
  const status = text => { byId("status").textContent = text; };
  function refreshActions() {
    byId("validate").disabled = busy || pendingSaves > 0;
    byId("import").disabled = busy || pendingSaves > 0 || dirty.size > 0 || !digest;
  }
  function refreshMutationControls() {
    for (const id of ["select", "unselect", "secrets-from-environment"]) {
      const control = byId(id); if (control) control.disabled = importing;
    }
    for (const tag of ["input", "textarea", "button"]) {
      for (const control of byId("records").querySelectorAll(tag)) control.disabled = importing;
    }
  }
  function invalidate() { revision++; digest = null; refreshActions(); }
  async function request(path, body) {
    const response = await fetch(api + path, {method: body ? "POST" : "GET", headers: {
      "X-Transfer-Token": token, ...(body ? {"Content-Type": "application/json"} : {})
    }, ...(body ? {body: JSON.stringify(body)} : {})});
    const value = response.headers.get("Content-Type")?.includes("json") ? await response.json() : {error: await response.text()};
    if (!response.ok) throw new Error(value.error || `Request failed (${response.status}).`);
    return value;
  }
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  const titleOf = record => record.title || `${record.section} · ${record.key}`;
  const sectionTitle = name => (sections.find(section => section.name === name) || {}).title || name;
  async function saveReviewChange(item, path, payloadOf) {
    invalidate();
    const payload = payloadOf(item), submitted = JSON.stringify(payload);
    dirty.add(item); pendingSaves++; refreshActions();
    // Preserve the order of selections, edits, confirmations, and destination
    // choices. A failed save must not strand later corrections.
    const send = () => request(path, payload);
    const operation = saveQueue.then(send, send); saveQueue = operation;
    try {
      await operation;
      const current = JSON.stringify(payloadOf(item)) === submitted;
      if (current) dirty.delete(item); else dirty.add(item);
      return current;
    } catch (error) {
      // A prior queued save may have cleared this item. Failed writes have
      // an unknown outcome, so require a successful save of the visible value.
      dirty.add(item); throw error;
    } finally { pendingSaves--; refreshActions(); }
  }
  const save = record => saveReviewChange(record, "/record", recordPayload);
  function recordPayload(record) {
    return {section: record.section, key: record.key, selected: record.selected,
      text: record.text, confirmed: !!record.confirmed_digest};
  }
  async function saveDestination() {
    try {
      return await saveReviewChange(destinationChoice, "/destination",
        destination => ({secrets_source: destination.secrets_source}));
    } catch (error) {
      throw new Error(`Saving where secrets come from failed. Select Validate selected records to retry. ${error.message}`);
    }
  }
  function shown() {
    const section = byId("section").value, query = byId("filter").value.toLowerCase();
    return records.filter(r => (!section || r.section === section)
      && `${sectionTitle(r.section)} ${titleOf(r)} ${r.key}`.toLowerCase().includes(query));
  }
  function renderRecord(record) {
    const details = node("details", undefined, "review-record"), summary = node("summary");
    const pick = node("input"); pick.type = "checkbox"; pick.checked = record.selected;
    pick.setAttribute("aria-label", `Import ${sectionTitle(record.section)}: ${titleOf(record)}`);
    pick.addEventListener("click", event => event.stopPropagation());
    pick.addEventListener("change", async () => {
      if (importing) { pick.checked = record.selected; return; }
      record.selected = pick.checked;
      try { await save(record); render(); } catch (error) { status(error.message); }
    });
    summary.append(pick, node("span", titleOf(record))); details.append(summary);
    if (record.title) details.append(node("p", record.key, "review-record-key"));
    if (record.text !== null) {
      if (record.section === "schedules") details.append(node("p", `Runs remaining in backup: ${record.remaining_count ?? "unlimited"}. Changing repeat_count sets a new number of remaining runs.`));
      const label = node("label", record.section === "schedules" ? "Schedule Markdown" : record.section === "settings" ? "Setting value in YAML" : "Memory text");
      const editor = node("textarea"); editor.value = record.text; editor.spellcheck = false;
      editor.setAttribute("aria-label", `${label.textContent}: ${titleOf(record)}`); label.append(editor); details.append(label);
      editor.addEventListener("input", () => {
        if (importing) { editor.value = record.text; return; }
        record.text = editor.value; dirty.add(record); invalidate(); record.confirmed_digest = null;
        const checkbox = details.querySelector(".schedule-confirm"); if (checkbox) checkbox.checked = false;
      });
      const saveButton = node("button", "Save edits", "button secondary"); saveButton.type = "button";
      saveButton.addEventListener("click", async () => {
        if (importing) return;
        record.text = editor.value; record.confirmed_digest = null;
        const checkbox = details.querySelector(".schedule-confirm"); if (checkbox) checkbox.checked = false;
        try { const current = await save(record); status(current ? `Saved edits to ${titleOf(record)}. Validate before importing.` : "Newer edits are still unsaved. Save them before validating."); } catch (error) { status(error.message); }
      }); details.append(saveButton);
      if (record.section === "schedules") {
        const confirmLabel = node("label"), confirm = node("input", undefined, "schedule-confirm");
        confirm.type = "checkbox"; confirm.checked = !!record.confirmed_digest;
        confirmLabel.append(confirm, node("span", " I have reviewed this schedule and its timing, instructions, owner, and delivery route are correct.")); details.append(confirmLabel);
        confirm.addEventListener("change", async () => {
          if (importing) { confirm.checked = !!record.confirmed_digest; return; }
          record.text = editor.value; record.confirmed_digest = confirm.checked ? "confirmed" : null;
          try { const current = await save(record); status(current ? (confirm.checked ? `Confirmed ${titleOf(record)}.` : `Confirmation removed for ${titleOf(record)}.`) : "Newer edits are still unsaved. Save and confirm them before validating."); } catch (error) { status(error.message); }
        });
      }
    } else { details.append(node("pre", JSON.stringify(record.details, null, 2))); }
    return details;
  }
  function render() {
    byId("records").replaceChildren();
    byId("count").textContent = `${records.filter(r => r.selected).length} of ${records.length} records selected.`;
    const visible = shown();
    // Grouped under each section's title, in the order the import applies them.
    for (const section of sections) {
      const inSection = visible.filter(r => r.section === section.name);
      if (!inSection.length) continue;
      byId("records").append(node("h2", section.title, "review-section-title"));
      for (const record of inSection) byId("records").append(renderRecord(record));
    }
    refreshMutationControls();
  }
  async function action(callback) {
    if (busy) return; busy = true;
    refreshActions();
    try { await callback(); } catch (error) { status(error.message); }
    finally { busy = false; refreshActions(); }
  }
  // Onboarding only: where the new instance's secrets come from (DestinationChoices).
  function renderDestination(destination) {
    const box = byId("destination");
    if (!box || !destination) return;
    destinationChoice = destination;
    const choice = node("input"); choice.type = "checkbox"; choice.id = "secrets-from-environment";
    choice.checked = destination.secrets_source === "environment";
    const label = node("label"); label.htmlFor = choice.id;
    label.append(choice, node("span", " Secrets come from the environment (Docker secrets). Don't write team.env."));
    const old = destination.exported_secrets_source === "environment" ? "the environment" : "its team.env";
    box.replaceChildren(label, node("p", `The old instance took its secrets from ${old}. Choose environment when every secret reaches this container as an environment variable or Docker secret, as in Docker Swarm.`, "choice-note"));
    choice.addEventListener("change", async () => {
      if (importing) { choice.checked = destination.secrets_source === "environment"; return; }
      destination.secrets_source = choice.checked ? "environment" : "env_file";
      try {
        const current = await saveDestination();
        status(current ? "Saved where secrets come from. Validate before importing." : "Where secrets come from changed again. Waiting for the latest choice to save.");
      } catch (error) { status(error.message); }
    });
  }
  // Onboarding only: after the import the service restarts into the team.
  function waitForTeam(nextUrl) {
    const poll = () => fetch("/health", {cache: "no-store"})
      .then(response => response.ok ? response.json() : null)
      .catch(() => null)
      .then(health => {
        if (health && health.service !== "onboard-web") { location.href = nextUrl; return; }
        setTimeout(poll, 2000);
      });
    setTimeout(poll, 2000);
  }
  function showImportResult(result) {
    status(result.message);
    const lines = (result.credentials || []).map(r => node("p", `${r.key}: ${r.imported ? (r.reason || "stored") : r.reason}`, `review-result ${r.imported ? "" : r.waiting ? "attention" : "refused"}`));
    // Named like the onboarding finalize list: what it is for and how to create it, never a value.
    for (const secret of result.missing_secrets || []) lines.push(node("p", `${secret.name}: ${[secret.purpose, secret.how_to_create].filter(Boolean).join(" ")} Set ${secret.name}, or ${secret.name}_FILE to a mounted secret file.`, "review-result attention"));
    byId("results").replaceChildren(...lines);
    if (result.restarting) { byId("import").hidden = true; byId("validate").hidden = true; waitForTeam(result.next_url || "/"); }
  }
  byId("section").addEventListener("change", render); byId("filter").addEventListener("input", render);
  for (const [id, selected] of [["select", true], ["unselect", false]]) {
    byId(id).addEventListener("click", () => action(async () => { for (const r of shown()) { r.selected = selected; await save(r); } render(); }));
  }
  byId("validate").addEventListener("click", () => action(async () => {
    if (pendingSaves || [...dirty].some(item => item !== destinationChoice)) throw new Error("Save your edits before validating the selected records.");
    // Destination choices save on change and have no separate save button.
    // Retry before validating, so the visible choice can stay unchanged.
    if (dirty.has(destinationChoice)) await saveDestination();
    if (dirty.size || pendingSaves) throw new Error("Save your edits before validating the selected records.");
    const validatingRevision = revision;
    status("Checking selected records and their dependencies…");
    const result = await request("/validate", {});
    if (revision !== validatingRevision || dirty.size || pendingSaves) {
      status("The review changed while validating. Save your edits and validate again."); return;
    }
    digest = result.digest;
    byId("results").replaceChildren(...result.records.map(r => node("p", `${sectionTitle(r.section)} · ${r.title || r.key}: ${r.outcome}${r.reason ? ". " + r.reason : ""}`, `review-result ${r.outcome}`)));
    for (const section of result.unknown_sections || []) byId("results").append(node("p", `This release cannot import section ${section}.`, "review-result refused"));
    status(result.valid ? `Validated. ${result.skipped} records are unselected or already imported. Review the results, then import.` : "Some selected records were refused. Correct or unselect them and validate again.");
  }));
  byId("import").addEventListener("click", () => action(async () => {
    if (dirty.size || pendingSaves || !digest) throw new Error("Save your edits and validate the current review before importing.");
    // Import applies this digest. Keep the visible review fixed until that
    // request finishes; validation still permits edits.
    importing = true; refreshMutationControls();
    try {
      status("Importing selected records…"); const result = await request("/import", {digest}); digest = null;
      showImportResult(result);
    } finally { importing = false; refreshMutationControls(); }
  }));
  byId("theme").addEventListener("click", () => {
    const mode = document.documentElement.dataset.colorMode === "dark" ? "light" : "dark";
    document.documentElement.dataset.colorMode = mode;
    if (window.UAAppearance) window.UAAppearance.preview({...window.UAAppearance.current(), mode});
  });
  request("/review").then(value => {
    records = value.records;
    sections = value.sections || [...new Set(records.map(r => r.section))].map(name => ({name, title: name}));
    byId("source").textContent = `Team ${value.team_id} → ${value.target_root}`;
    if (byId("files")) byId("files").textContent = `Editable files are saved in ${value.review_directory}.`;
    for (const section of sections) { const option = node("option", section.title); option.value = section.name; byId("section").append(option); }
    renderDestination(value.destination);
    render(); status("Select records, review schedules, then validate.");
  }).catch(error => status(error.message));
})();

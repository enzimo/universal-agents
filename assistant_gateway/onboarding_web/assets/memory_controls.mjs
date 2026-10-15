/**
 * Settings → Memories in Web Chat: the durable facts kept for this person
 * (Personal) and for the team (Team).
 *
 * - The gateway decides who may change what: each memory lists its
 *   `available_actions`, and a list says whether a new memory may be added
 *   (`can_create`). The browser never infers administrator access.
 * - Editing and adding use one inline form (text, comma-separated tags and an
 *   optional local expiration). Saves and deletes send the memory's record
 *   digest, so an outdated page cannot overwrite a newer fact.
 * - Delete asks for confirmation on the same button.
 *
 * Memory text is set with textContent, never as HTML.
 */

import { LIST_PAGE_SIZE, element, failureMessage, localTimeFormatter, setStatusLine, text } from "./settings_support.mjs";

export const MEMORIES_API = "/api/web-chat/memories";
export const MEMORY_SCOPES = Object.freeze({ PERSONAL: "personal", TEAM: "team" });
export const DELETE_CONFIRM_MS = 6000;

const localTime = localTimeFormatter();

/** The value a `datetime-local` input needs for a stored UTC time; "" when there is none. */
export function localInputValue(value) {
  const date = new Date(text(value));
  if (Number.isNaN(date.getTime())) return "";
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

export function memoryMeta(memory, formatTime = localTime) {
  const details = [`Updated ${formatTime(memory.updated_at_utc)}`];
  if (memory.invalid_after_utc) details.push(`Expires ${formatTime(memory.invalid_after_utc)}`);
  return details.join(" · ");
}

export function parseTags(value) {
  return String(value || "").split(",").map(tag => tag.trim()).filter(Boolean);
}

/**
 * Connects the Memories panel. `client` is the Settings client from
 * settings_support.mjs; `getSessionKey()` returns the open conversation and
 * `isVisible()` whether the panel is on screen.
 */
export function createMemoryControls({
  content, status, refreshButton, newButton, personalTab, teamTab, client, getSessionKey, isVisible,
  browser = globalThis, formatTime = localTime,
}) {
  const document = browser.document;
  const state = { scope: MEMORY_SCOPES.PERSONAL, rows: [], total: 0, hasMore: false, offset: 0, loadPromise: null };

  const setStatus = (message, isError) => setStatusLine(status, message, isError);

  function setCategory(scope) {
    newButton.hidden = true;
    state.scope = scope === MEMORY_SCOPES.TEAM ? MEMORY_SCOPES.TEAM : MEMORY_SCOPES.PERSONAL;
    const personal = state.scope === MEMORY_SCOPES.PERSONAL;
    personalTab.classList.toggle("is-active", personal);
    personalTab.setAttribute("aria-selected", personal ? "true" : "false");
    teamTab.classList.toggle("is-active", !personal);
    teamTab.setAttribute("aria-selected", personal ? "false" : "true");
  }

  function mutate(memory, method, body) {
    const scope = text(memory.scope) || state.scope;
    const path = `${MEMORIES_API}/${encodeURIComponent(scope)}`
      + (method === "POST" ? "" : `/${encodeURIComponent(String(memory.memory_id))}`);
    return client.send(method, path, {
      session_key: getSessionKey(),
      record_digest: text(memory.record_digest),
      ...(body || {}),
    }, "The memory was not changed because the gateway returned no explanation. Refresh and try again.");
  }

  function field(label, input, help) {
    const wrap = element(document, "label", "memory-editor-field");
    wrap.append(element(document, "span", "memory-editor-label", label), input);
    if (help) wrap.append(element(document, "small", "memory-editor-help", help));
    return wrap;
  }

  /** The inline editor for a new (`memory.memory_id` missing) or existing memory. */
  function openEditor(memory, item, actions) {
    if (item.querySelector(".memory-editor")) return;
    actions.hidden = true;
    const creating = !memory.memory_id;
    const editorSession = getSessionKey();
    const editor = element(document, "form", "memory-editor");
    const factInput = element(document, "textarea", "memory-editor-text");
    factInput.rows = 4;
    factInput.maxLength = 12000;
    factInput.value = String(memory.fact_text || "");
    const tagInput = element(document, "input", "memory-editor-input");
    tagInput.type = "text";
    tagInput.value = (Array.isArray(memory.tags) ? memory.tags : []).join(", ");
    tagInput.placeholder = "Comma-separated tags";
    const expirationInput = element(document, "input", "memory-editor-input");
    expirationInput.type = "datetime-local";
    expirationInput.value = localInputValue(memory.invalid_after_utc);
    const editorActions = element(document, "div", "memory-editor-actions");
    const save = element(document, "button", "button primary memory-action-button", "Save");
    save.type = "submit";
    const cancel = element(document, "button", "button ghost memory-action-button", "Cancel");
    cancel.type = "button";
    editorActions.append(save, cancel);
    editor.append(field("Memory", factInput), field("Tags", tagInput),
      field("Expires", expirationInput, "Leave blank to keep this memory until it is changed or deleted."), editorActions);

    // A reply that arrives after sign-out or a category switch is ignored.
    const stillCurrent = () => editorSession === getSessionKey() && text(memory.scope) === state.scope;

    cancel.addEventListener("click", () => {
      if (creating) {
        item.remove();
        newButton.disabled = false;
      } else {
        editor.remove();
        actions.hidden = false;
      }
    });
    editor.addEventListener("submit", event => {
      event.preventDefault();
      const factText = String(factInput.value || "").trim();
      if (!factText) {
        setStatus("Memory text cannot be empty.", true);
        factInput.focus();
        return;
      }
      let invalidAfter = null;
      if (expirationInput.value) {
        const expiration = new Date(expirationInput.value);
        if (Number.isNaN(expiration.getTime())) {
          setStatus("Enter a valid expiration date and time.", true);
          expirationInput.focus();
          return;
        }
        invalidAfter = expiration.toISOString();
      }
      const finish = client.beginAction(save, "Saving…", editorActions);
      setStatus("", false);
      mutate(memory, creating ? "POST" : "PATCH", {
        fact_text: factText, tags: parseTags(tagInput.value), invalid_after_utc: invalidAfter,
      }).then(payload => {
        if (!stillCurrent()) return undefined;
        if (creating) {
          // The write succeeded. Close this form even if the following read
          // fails, so a retried refresh cannot submit the same fact twice.
          item.remove();
          return load({ offset: Math.floor(state.total / LIST_PAGE_SIZE) * LIST_PAGE_SIZE }).then(loaded => {
            if (!stillCurrent()) return;
            setStatus(loaded ? "Memory added." : "Memory added, but the list could not be refreshed. Select Refresh to see it.", !loaded);
          });
        }
        if (payload && payload.memory) replaceRow(payload.memory);
        render();
        setStatus("Memory updated.", false);
        return undefined;
      }).catch(error => {
        if (stillCurrent()) setStatus(failureMessage(error, "The memory was not changed. Refresh and try again."), true);
      }).finally(finish);
    });
    item.append(editor);
    factInput.focus();
  }

  function replaceRow(memory) {
    if (text(memory.scope) !== state.scope) return;
    state.rows = state.rows.map(row => (Number(row.memory_id) === Number(memory.memory_id) ? memory : row));
  }

  function deleteButton(memory, actions) {
    const remove = element(document, "button", "button danger memory-action-button is-danger", "Delete");
    remove.type = "button";
    let confirmTimer = 0;
    const reset = () => {
      remove.classList.remove("is-confirming");
      remove.textContent = "Delete";
    };
    remove.addEventListener("click", () => {
      if (!remove.classList.contains("is-confirming")) {
        remove.classList.add("is-confirming");
        remove.textContent = "Confirm delete";
        browser.clearTimeout(confirmTimer);
        confirmTimer = browser.setTimeout(reset, DELETE_CONFIRM_MS);
        return;
      }
      browser.clearTimeout(confirmTimer);
      const finish = client.beginAction(remove, "Deleting…", actions);
      setStatus("", false);
      mutate(memory, "DELETE").then(() => {
        if (text(memory.scope) !== state.scope) return undefined;
        state.rows = state.rows.filter(row => Number(row.memory_id) !== Number(memory.memory_id));
        state.total = Math.max(0, state.total - 1);
        const lastOffset = Math.max(0, Math.ceil(state.total / LIST_PAGE_SIZE) - 1) * LIST_PAGE_SIZE;
        state.offset = Math.min(state.offset, lastOffset);
        render();
        return load({ offset: state.offset }).then(loaded => {
          setStatus(loaded
            ? "Memory deleted. Its audit event remains available for recovery."
            : "Memory deleted, but the list could not be refreshed. Select Refresh to reload it.", !loaded);
        });
      }).catch(error => {
        setStatus(failureMessage(error, "The memory was not deleted. Refresh and try again."), true);
      }).finally(() => {
        finish();
        if (remove.isConnected) reset();
      });
    });
    return remove;
  }

  function memoryActions(memory, item) {
    const actions = element(document, "div", "memory-item-actions");
    const available = Array.isArray(memory.available_actions) ? memory.available_actions : [];
    if (available.includes("edit")) {
      const edit = element(document, "button", "button primary memory-action-button", "Edit");
      edit.type = "button";
      edit.addEventListener("click", () => openEditor(memory, item, actions));
      actions.append(edit);
    }
    if (available.includes("delete")) actions.append(deleteButton(memory, actions));
    if (!available.length) {
      actions.append(element(document, "span", "memory-admin-note",
        state.scope === MEMORY_SCOPES.TEAM ? "Admin access required" : "No actions available"));
    }
    return actions;
  }

  function memoryItem(memory) {
    const item = element(document, "article", "memory-item");
    item.setAttribute("role", "listitem");
    item.setAttribute("data-memory-id", String(memory.memory_id));
    const copy = element(document, "div", "memory-item-copy");
    copy.append(element(document, "p", "memory-fact", String(memory.fact_text || "")));
    if (Array.isArray(memory.tags) && memory.tags.length) {
      const tags = element(document, "div", "memory-tags");
      memory.tags.forEach(tag => tags.append(element(document, "span", "memory-tag", String(tag))));
      copy.append(tags);
    }
    copy.append(element(document, "p", "memory-meta", memoryMeta(memory, formatTime)));
    item.append(copy, memoryActions(memory, item));
    return item;
  }

  function render() {
    newButton.disabled = false;
    const nodes = [];
    if (!state.rows.length) {
      nodes.push(element(document, "p", "permission-empty-state", state.scope === MEMORY_SCOPES.TEAM
        ? "No active team memories are stored." : "No active personal memories are stored."));
    } else {
      const items = element(document, "div", "memory-list");
      items.setAttribute("role", "list");
      state.rows.forEach(memory => items.append(memoryItem(memory)));
      nodes.push(items);
    }
    if (state.offset || state.hasMore) {
      nodes.push(client.paginate({ offset: state.offset, total: state.total, pageSize: LIST_PAGE_SIZE,
        onPage: offset => load({ offset }) }));
    }
    content.replaceChildren(...nodes);
    content.scrollTop = 0;
  }

  /** Reads one page of the selected category. Resolves true when the list is current. */
  function load({ offset = 0 } = {}) {
    if (state.loadPromise) return state.loadPromise;
    const scope = state.scope;
    const finish = client.beginRefresh(refreshButton);
    newButton.disabled = true;
    const query = `?session_key=${encodeURIComponent(getSessionKey())}&scope=${encodeURIComponent(scope)}`
      + `&offset=${encodeURIComponent(String(offset))}&limit=${LIST_PAGE_SIZE}`;
    state.loadPromise = client.get(MEMORIES_API + query,
      "Memories could not be loaded because the gateway returned no explanation. Refresh and try again.")
      .then(payload => {
        if (scope !== state.scope) return false;
        state.rows = Array.isArray(payload.memories) ? payload.memories : [];
        state.offset = offset;
        state.total = Number(payload.total || 0);
        state.hasMore = Boolean(payload.has_more);
        newButton.hidden = !payload.can_create;
        render();
        setStatus("", false);
        return true;
      })
      .catch(error => {
        setStatus(failureMessage(error, "Memories could not be loaded. Refresh and try again."), true);
        return false;
      })
      .finally(() => {
        state.loadPromise = null;
        finish();
        newButton.disabled = false;
        // The category changed while this read ran: read the new one.
        if (scope !== state.scope && isVisible()) load();
      });
    return state.loadPromise;
  }

  function switchCategory(scope) {
    if (state.scope === scope) return;
    setCategory(scope);
    state.rows = [];
    state.hasMore = false;
    state.offset = 0;
    render();
    load();
  }

  personalTab.addEventListener("click", () => switchCategory(MEMORY_SCOPES.PERSONAL));
  teamTab.addEventListener("click", () => switchCategory(MEMORY_SCOPES.TEAM));
  refreshButton.addEventListener("click", () => { load(); });
  newButton.addEventListener("click", () => {
    if (newButton.hidden || newButton.disabled) return;
    newButton.disabled = true;
    const item = element(document, "article", "memory-item");
    const actions = element(document, "div", "memory-item-actions");
    item.append(element(document, "h3", "", `New ${state.scope} memory`), actions);
    content.prepend(item);
    openEditor({ scope: state.scope }, item, actions);
    content.scrollTop = 0;
  });

  return {
    load,
    resetStatus() { setStatus("", false); },
    /** Sign-out: back to Personal with an empty list. */
    clear() {
      setCategory(MEMORY_SCOPES.PERSONAL);
      Object.assign(state, { rows: [], total: 0, hasMore: false, offset: 0 });
      content.replaceChildren();
      setStatus("", false);
    },
  };
}

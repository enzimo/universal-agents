/**
 * The Knowledge page (`/webchat?view=knowledge`): the team's pages and the
 * person's own, written together with agents.
 *
 * - **Team / Personal** list one space, 20 pages at a time, newest change
 *   first or by search match. A page shows its text (Web Chat's escaped
 *   Markdown renderer), pages that link to it, open notes and who changed it
 *   last; Edit, New page, Add note, History, Revert and Archive change it.
 * - **My proposals** lists the person's own pending team changes that agents
 *   proposed while working for them. Each shows what it would change; the
 *   person publishes it (as proposed or after editing) or withdraws it. The
 *   tab's badge counts them (ui/DESIGN.md, "Attention counts"), kept current
 *   while the page is open; Settings' Knowledge link and the cog show the
 *   same count (settings_controls.mjs).
 *
 * The gateway and the manager decide everything (who may do what, conflicts,
 * content never stored, content held for review); this module only shows
 * their answers. Saves carry the revision the person saw, so a page someone
 * else changed meanwhile is never overwritten: the person sees the latest
 * version beside their own text and saves again. Content held for review is
 * saved only after **Save anyway**. Archive, Revert, Withdraw and publishing
 * flagged content ask for a second click on the same button.
 *
 * All text is set with textContent; only page bodies go through the Markdown
 * renderer the caller passes (`renderMarkdown(node, text)`).
 */
import {
  LIST_PAGE_SIZE, createAttentionCount, createSettingsClient, element, failureMessage, localTimeFormatter, nameWithCount,
  setStatusLine, showCountBadge, text,
} from "./settings_support.mjs";
import {
  CHANGE_WORDS, PROPOSAL_WORDS, SPACE_WORDS, diffBlock, fieldChanges, flagList, linkify, pageEditor, pageTypeWord,
} from "./knowledge_render.mjs";

export const KNOWLEDGE_API = "/api/web-chat/knowledge";
// How many proposals wait for the signed-in person: `{status, pending_proposals}`.
export const KNOWLEDGE_COUNTS_PATH = `${KNOWLEDGE_API}/counts`;
export const TABS = Object.freeze({ TEAM: "team", PERSONAL: "personal", PROPOSALS: "proposals" });
export const CONFIRM_MS = 6000;
export const TEXT = Object.freeze({
  CONFLICT: "Nothing was saved. Someone else changed this page while you were editing it. "
    + "Your text is still here: select Show the latest version, merge what you need, then save again.",
  LATEST_SHOWN: "The latest version is shown above your text. Merge what you need, then save again.",
  PAGE_EXISTS: "Nothing was saved. A page with this name already exists. Change the title, or open that page and edit it.",
  NOT_FOUND: "This page or proposal no longer exists. Refresh the list.",
  SENSITIVE: "Nothing was saved. The knowledge base never stores passwords, keys, tokens or identity numbers. Remove these and save again:",
  REVIEW: "Everyone on the team will see these. Remove them, or select Save anyway:",
  PUBLISH_FLAGGED: "This proposal contains details that need your decision before the team sees them:",
  STALE_PROPOSAL: "The page changed after this was proposed. The change below is shown against the page as it is now.",
  STALE_ARCHIVE: "The page changed after this was proposed. The current version is shown below.",
  ARCHIVE_PROPOSAL: "Publishing will archive the current page and keep its text and details.",
  EMPTY_TEAM: "No team pages yet. Select New page to write the first one.",
  EMPTY_PERSONAL: "You have no personal pages yet. Select New page to write one only you and your agents can read.",
  EMPTY_SEARCH: "No pages match this search.",
  EMPTY_ARCHIVED: "No archived pages in this space.",
  EMPTY_PROPOSALS: "No proposals are waiting. When an agent working for you wants to change a team page, the change waits here for you or the knowledge curator.",
  SELECT_PAGE: "Select a page to read it.",
  SELECT_PROPOSAL: "Select a proposal to see what it would change.",
  ARCHIVED: "This page is archived. Find it by selecting Archived pages in the list, or select Restore to make it current again.",
  LOAD_FAILED: "The Knowledge page could not be loaded. Refresh to try again.",
  COUNT_FAILED: "The number of waiting proposals could not be read.",
});

/** The proposal count from a counts answer; a malformed answer is an error, so the badge keeps its last number. */
export function pendingProposals(payload) {
  const count = payload && payload.pending_proposals;
  if (!Number.isInteger(count) || count < 0) throw new Error(TEXT.COUNT_FAILED);
  return count;
}

/** What the proposals badge counts, in words (tooltip and accessible names); "" for none. */
export function proposalCountLabel(count) {
  if (!count) return "";
  return count === 1 ? "1 proposal waiting for you" : `${count} proposals waiting for you`;
}

/** Reads the proposal count in the browser's Web Chat thread `sessionKey` (Settings' link and cog). */
export function readPendingProposals(client, sessionKey) {
  const query = new URLSearchParams({ session_key: sessionKey });
  return client.get(`${KNOWLEDGE_COUNTS_PATH}?${query.toString()}`, TEXT.COUNT_FAILED).then(pendingProposals);
}

/** The name in a page reference: `team/old-notes` → `old-notes`. */
export function pageName(reference) {
  return text(reference).split("/").slice(1).join("/") || text(reference);
}

/** The editor's words when a new page would take a merged page's name. */
export function mergedText(reference) {
  return "Nothing was saved. That name now leads to " + pageName(reference)
    + ", because that page was merged; edit that page or choose another name.";
}

/**
 * Keep Knowledge's old requests from signing out a later session. A response
 * may start fetching or finish parsing after sign-out. Each signed-in session
 * gets its own client, with guards before fetch, after fetch and at the
 * unauthorized callback.
 */
export function createKnowledgeClient({ browser = globalThis, onUnauthorized } = {}) {
  let session = 0;
  function sessionClient() {
    const requestedSession = session;
    const requireSession = () => {
      if (requestedSession !== session) throw new DOMException("The session ended.", "AbortError");
    };
    return createSettingsClient({
      browser: {
        UAActionFeedback: browser.UAActionFeedback,
        UAPagination: browser.UAPagination,
        async fetch(path, options) {
          requireSession();
          const response = await browser.fetch(path, options);
          requireSession();
          return response;
        },
      },
      onUnauthorized(response) {
        if (requestedSession === session && typeof onUnauthorized === "function") onUnauthorized(response);
      },
    });
  }
  let client = sessionClient();
  return {
    ...client,
    setCsrfToken: value => client.setCsrfToken(value),
    writeHeaders: () => client.writeHeaders(),
    get: (...args) => client.get(...args),
    send: (...args) => client.send(...args),
    post: (...args) => client.post(...args),
    ensureSessionContext: (...args) => client.ensureSessionContext(...args),
    invalidateSession() {
      session += 1;
      client = sessionClient();
    },
  };
}

/** Plain words for refusal codes whose server text is written for agents. */
const REFUSAL_TEXT = Object.freeze({
  conflict: TEXT.CONFLICT,
  page_exists: TEXT.PAGE_EXISTS,
  not_found: TEXT.NOT_FOUND,
  sensitive_content: TEXT.SENSITIVE,
});
const FLAGGED_CODES = new Set(["review_required", "sensitive_content"]);

/**
 * Connects the page. `nodes` holds the elements from chat.html, `client` is
 * the Settings client (settings_support.mjs), `getSessionKey()` resolves the
 * browser's Web Chat thread that requests act in.
 */
export function createKnowledgePage({
  nodes, client, getSessionKey, renderMarkdown, browser = globalThis, formatTime = localTimeFormatter(),
}) {
  const document = browser.document;
  const state = { tab: TABS.TEAM, pageStatus: "active", query: "", offset: 0, total: 0, rows: [],
    selected: "", selectedPage: null, generation: 0, session: 0, view: 0 };
  nodes.pageStatus.value = state.pageStatus;

  // -- small helpers -------------------------------------------------------------

  const setPageStatus = (message, isError) => setStatusLine(nodes.status, message, isError);
  const isProposals = () => state.tab === TABS.PROPOSALS;

  function button(label, className, onClick) {
    const node = element(document, "button", "button " + className, label);
    node.type = "button";
    if (onClick) node.addEventListener("click", onClick);
    return node;
  }

  /** A button that asks for a second click before it runs (Archive, Revert, Withdraw). */
  function confirmButton(label, confirmLabel, className, run) {
    const node = button(label, className);
    let timer = 0;
    const reset = () => { node.classList.remove("is-confirming"); node.textContent = label; };
    node.addEventListener("click", () => {
      if (!node.classList.contains("is-confirming")) {
        node.classList.add("is-confirming");
        node.textContent = confirmLabel;
        browser.clearTimeout(timer);
        timer = browser.setTimeout(reset, CONFIRM_MS);
        return;
      }
      browser.clearTimeout(timer);
      Promise.resolve(run()).finally(() => { if (node.isConnected) reset(); });
    });
    return node;
  }

  /** One message at the top of the part the person worked in: red with role=alert for failures. */
  function showMessage(target, message, { error = false, extra = [] } = {}) {
    target.replaceChildren();
    target.hidden = !message;
    target.className = "knowledge-message " + (error ? "warning-callout" : "settings-status");
    if (!message) return;
    target.setAttribute("role", error ? "alert" : "status");
    const body = element(document, "div", "knowledge-message-body");
    body.append(element(document, "p", "", message), ...extra);
    if (error) target.append(element(document, "span", "warning-icon", "!"));
    target.append(body);
    // The part's message line may sit above the visible part of a long form.
    if (typeof target.scrollIntoView === "function") target.scrollIntoView({ block: "nearest" });
  }

  function refusalText(error, fallback) {
    const mergedInto = text(error && error.payload && error.payload.merged_into);
    if (text(error && error.code) === "merged" && mergedInto) return mergedText(mergedInto);
    return REFUSAL_TEXT[text(error && error.code)] || failureMessage(error, fallback);
  }

  function refusalFlags(error) {
    const payload = error && error.payload;
    return payload && Array.isArray(payload.review_flags) ? payload.review_flags : [];
  }

  /** Who made a change, as the gateway labelled it (an agent type, "You" or a name). */
  function editorWord(editor) {
    return text(editor && typeof editor === "object" ? editor.label || editor.agent_type : editor) || "someone";
  }

  function pagePath(space, page, suffix = "") {
    return `${KNOWLEDGE_API}/pages/${encodeURIComponent(space)}/${encodeURIComponent(page)}${suffix}`;
  }

  function get(path, params, fallback) {
    const session = state.session;
    return getSessionKey().then(sessionKey => {
      if (session !== state.session) throw new DOMException("The session ended.", "AbortError");
      const query = new URLSearchParams({ session_key: sessionKey, ...params });
      return client.get(`${path}?${query.toString()}`, fallback);
    });
  }

  function post(path, body, fallback) {
    const session = state.session;
    return getSessionKey().then(sessionKey => {
      if (session !== state.session) throw new DOMException("The session ended.", "AbortError");
      return client.post(path, { session_key: sessionKey, ...body }, fallback);
    });
  }

  // The My proposals badge. List reads of the tab and decisions there update it too.
  const proposalCount = createAttentionCount({
    read: () => get(KNOWLEDGE_COUNTS_PATH, {}, TEXT.COUNT_FAILED).then(pendingProposals),
    onChange: count => {
      showCountBadge(nodes.proposalsCount, count, proposalCountLabel(count));
      nameWithCount(nodes.tabs.proposals, "My proposals", proposalCountLabel(count));
    },
    warning: "ua.knowledge_page: the proposal count could not be read; retrying.",
    browser,
  });

  /** Replaces the viewer; returns a check that is false once something else replaced it. */
  function replaceViewer(title, meta, tools, content) {
    state.view += 1;
    const view = state.view;
    nodes.viewerTitle.textContent = title;
    nodes.viewerMeta.textContent = meta || "";
    nodes.viewerTools.replaceChildren(...(tools || []));
    nodes.viewerTools.hidden = !(tools && tools.length);
    nodes.viewer.replaceChildren(...(content || []));
    nodes.viewer.scrollTop = 0;
    return () => view === state.view;
  }

  function emptyViewer() {
    replaceViewer(isProposals() ? "Select a proposal" : "Select a page", "", [], [
      element(document, "p", "workspace-empty-state", isProposals() ? TEXT.SELECT_PROPOSAL : TEXT.SELECT_PAGE),
    ]);
  }

  function editorOpen() {
    return Boolean(nodes.viewer.querySelector(".knowledge-editor"));
  }

  // -- the list ------------------------------------------------------------------

  function setTab(tab) {
    state.tab = tab;
    Object.entries(nodes.tabs).forEach(([name, tabButton]) => {
      tabButton.classList.toggle("is-active", name === tab);
      tabButton.setAttribute("aria-selected", name === tab ? "true" : "false");
    });
    nodes.searchForm.hidden = isProposals();
    nodes.newButton.hidden = isProposals();
  }

  function listItem(label, detail, selectedKey, onOpen) {
    const item = element(document, "li", "knowledge-list-item");
    const open = button("", "ghost knowledge-list-button", onOpen);
    open.append(element(document, "strong", "knowledge-list-title", label));
    detail.filter(Boolean).forEach(line => open.append(element(document, "span", "knowledge-list-detail", line)));
    if (selectedKey === state.selected) {
      open.classList.add("is-selected");
      open.setAttribute("aria-current", "true");
    }
    item.append(open);
    return item;
  }

  function pageListItem(page) {
    const detail = [page.status === "archived" ? "Archived" : "", text(page.snippet) || text(page.summary),
      "Changed " + formatTime(page.updated_at_utc)];
    return listItem(text(page.title), detail, text(page.space) + "/" + text(page.page),
      () => openPage(text(page.space), text(page.page)));
  }

  function proposalListItem(proposal) {
    const detail = [
      (PROPOSAL_WORDS[proposal.kind] || text(proposal.kind)) + " · team/" + text(proposal.page),
      "From " + editorWord(proposal.agent_type) + " · " + formatTime(proposal.created_at_utc),
    ];
    return listItem(text(proposal.title), detail, text(proposal.contribution_id),
      () => openProposal(text(proposal.contribution_id)));
  }

  function emptyListText() {
    if (isProposals()) return TEXT.EMPTY_PROPOSALS;
    if (state.query) return TEXT.EMPTY_SEARCH;
    if (state.pageStatus === "archived") return TEXT.EMPTY_ARCHIVED;
    return state.tab === TABS.TEAM ? TEXT.EMPTY_TEAM : TEXT.EMPTY_PERSONAL;
  }

  function renderList() {
    const rows = state.rows.map(row => (isProposals() ? proposalListItem(row) : pageListItem(row)));
    const children = [];
    if (rows.length) {
      const list = element(document, "ul", "knowledge-list-items");
      list.setAttribute("aria-label", isProposals() ? "Your proposals" : "Pages");
      list.append(...rows);
      children.push(list);
    } else {
      children.push(element(document, "p", "permission-empty-state", emptyListText()));
    }
    if (state.total > LIST_PAGE_SIZE) {
      children.push(client.paginate({
        total: state.total, offset: state.offset, pageSize: LIST_PAGE_SIZE,
        label: isProposals() ? "Proposal pages" : "Knowledge list pages", onPage: offset => loadList(offset),
      }));
    }
    nodes.list.replaceChildren(...children);
  }

  /** Reads one page of the current list. Resolves true when the list is current. */
  function loadList(offset = state.offset) {
    const generation = ++state.generation;
    const done = client.beginRefresh(nodes.refreshButton);
    const request = isProposals()
      ? get(`${KNOWLEDGE_API}/contributions`, { offset: String(offset) }, TEXT.LOAD_FAILED)
      : get(`${KNOWLEDGE_API}/pages`, { space: state.tab, status: state.pageStatus,
        query: state.query, offset: String(offset) }, TEXT.LOAD_FAILED);
    return request.then(payload => {
      if (generation !== state.generation) return false;
      state.rows = (isProposals() ? payload.contributions : payload.pages) || [];
      state.total = Number(payload.total || 0);
      if (isProposals()) proposalCount.set(state.total);
      state.offset = offset;
      renderList();
      setPageStatus("", false);
      return true;
    }).catch(error => {
      if (generation === state.generation) setPageStatus(failureMessage(error, TEXT.LOAD_FAILED), true);
      return false;
    }).finally(done);
  }

  function switchTab(tab) {
    if (state.tab === tab) return;
    state.generation += 1;
    Object.assign(state, { pageStatus: "active", query: "", offset: 0, total: 0, rows: [], selected: "", selectedPage: null });
    nodes.searchInput.value = "";
    nodes.pageStatus.value = state.pageStatus;
    setTab(tab);
    renderList();
    emptyViewer();
    loadList(0);
  }

  // -- a page --------------------------------------------------------------------

  function pageMeta(page) {
    const parts = [SPACE_WORDS[page.space] || "", pageTypeWord(page.page_type), "Revision " + page.revision];
    if (page.last_editor) parts.push("Last changed by " + editorWord(page.last_editor) + " " + formatTime(page.updated_at_utc));
    return parts.filter(Boolean).join(" · ");
  }

  function noticeLine(message) {
    return element(document, "p", "knowledge-notice", message);
  }

  function pageNotices(page) {
    const notices = [];
    if (page.status === "archived") notices.push(noticeLine(TEXT.ARCHIVED));
    if (text(page.frozen_until_utc)) {
      notices.push(noticeLine("Agents cannot edit this page until " + formatTime(page.frozen_until_utc)
        + " because several agents changed it many times in a short time; they add notes instead. You can still edit it."));
    }
    if (Number(page.pending_contributions) > 0) {
      const count = Number(page.pending_contributions);
      notices.push(noticeLine(count === 1
        ? "An agent working for you proposed a change to this page. It waits in My proposals."
        : count + " changes proposed by agents working for you wait in My proposals."));
    }
    if (Number(page.shown_revision) > 0) {
      notices.push(noticeLine("You are looking at revision " + page.shown_revision + ", not the current one."));
    }
    if (text(page.redirected_from)) {
      notices.push(noticeLine(pageName(page.redirected_from) + " was merged into this page."));
    }
    return notices;
  }

  /** Page text through the Markdown renderer, then its `[[links]]` made clickable. */
  function bodyNode(markdown, links = [], className = "workspace-markdown knowledge-body") {
    const node = element(document, "article", className);
    renderMarkdown(node, String(markdown || ""));
    linkify(document, node, links, (space, page) => openPage(space, page));
    return node;
  }

  function tagsNode(tags) {
    const wrap = element(document, "div", "memory-tags");
    (tags || []).forEach(tag => wrap.append(element(document, "span", "memory-tag", text(tag))));
    return (tags || []).length ? wrap : null;
  }

  function backlinksSection(page) {
    const section = element(document, "section", "knowledge-section");
    section.append(element(document, "h3", "", "Linked from"));
    if (!(page.backlinks || []).length) {
      section.append(element(document, "p", "memory-meta", "No other page links here."));
      return section;
    }
    const list = element(document, "ul", "knowledge-links");
    page.backlinks.forEach(linked => {
      const item = element(document, "li");
      item.append(button(text(linked.title), "ghost knowledge-link-button", () => openPage(text(linked.space), text(linked.page))));
      list.append(item);
    });
    section.append(list);
    return section;
  }

  function noteItem(note) {
    const item = element(document, "li", "knowledge-note");
    item.append(element(document, "p", "memory-fact", text(note.body)),
      element(document, "p", "memory-meta", editorWord(note.editor) + " · " + formatTime(note.created_at_utc)));
    return item;
  }

  function notesSection(page, isCurrent) {
    const section = element(document, "section", "knowledge-section");
    section.append(element(document, "h3", "", "Open notes"));
    const notes = page.open_notes || [];
    if (notes.length) {
      const list = element(document, "ul", "knowledge-notes");
      notes.forEach(note => list.append(noteItem(note)));
      section.append(list);
    } else {
      section.append(element(document, "p", "memory-meta", "No open notes. A note records something to fold into the page later."));
    }
    section.append(noteForm(page, isCurrent));
    return section;
  }

  function noteForm(page, isCurrent) {
    const form = element(document, "form", "memory-editor knowledge-note-form");
    const message = element(document, "div", "knowledge-message");
    message.hidden = true;
    const input = element(document, "textarea", "memory-editor-text");
    input.rows = 3;
    input.setAttribute("aria-label", "New note");
    const actions = element(document, "div", "memory-editor-actions");
    const add = element(document, "button", "button primary memory-action-button", "Add note");
    add.type = "submit";
    actions.append(add);
    form.append(message, input, actions);
    const submit = confirmReview => {
      const note = String(input.value || "").trim();
      if (!note) return showMessage(message, "Write the note first.", { error: true });
      const done = client.beginAction(add, "Adding…", actions);
      return post(pagePath(page.space, page.page, "/notes"), { note, confirm_review: confirmReview }, "The note was not added.")
        .then(payload => {
          if (!isCurrent()) return;
          showPage(payload.page, "Note added.");
        })
        .catch(error => { if (isCurrent()) showRefusal(message, error, "The note was not added.", () => submit(true)); })
        .finally(done);
    };
    form.addEventListener("submit", event => { event.preventDefault(); submit(false); });
    return form;
  }

  /** Shows a refusal in `target`; review flags get Save anyway (`retry` with confirmation). */
  function showRefusal(target, error, fallback, retry, extra = []) {
    const flags = refusalFlags(error);
    const more = [...extra];
    if (FLAGGED_CODES.has(text(error && error.code)) && flags.length) more.push(flagList(document, flags));
    if (text(error && error.code) === "review_required" && retry) {
      const message = text(error.message) || TEXT.REVIEW;
      const anyway = button("Save anyway", "secondary memory-action-button", () => {
        const done = client.beginAction(anyway, "Saving…", null);
        Promise.resolve(retry()).finally(done);
      });
      showMessage(target, message, { error: true, extra: [...more, anyway] });
      return;
    }
    showMessage(target, refusalText(error, fallback), { error: true, extra: more });
  }

  function pageTools(page, isCurrent) {
    if (Number(page.shown_revision) > 0) {
      return [
        button("Back to the current version", "ghost", () => openPage(page.space, page.page)),
        confirmButton("Revert to this revision", "Confirm revert", "secondary", () => revert(page, page.shown_revision, isCurrent)),
      ];
    }
    const tools = [
      button(page.status === "archived" ? "Restore" : "Edit", "primary", () => openEditor(page)),
      button("History", "ghost", () => openHistory(page.space, page.page)),
    ];
    if (page.status !== "archived") {
      tools.push(confirmButton("Archive", "Confirm archive", "danger is-danger", () => archive(page, isCurrent)));
    }
    return tools;
  }

  function showPage(page, successMessage = "") {
    state.selectedPage = { space: text(page.space), page: text(page.page) };
    state.selected = state.selectedPage.space + "/" + state.selectedPage.page;
    renderList();
    const message = element(document, "div", "knowledge-message");
    message.hidden = true;
    let isCurrent = () => false;
    const content = [message, ...pageNotices(page)];
    if (text(page.summary)) content.push(element(document, "p", "knowledge-summary", text(page.summary)));
    const tags = tagsNode(page.tags);
    if (tags) content.push(tags);
    content.push(bodyNode(page.body, page.links));
    const current = () => isCurrent();
    if (!Number(page.shown_revision)) content.push(backlinksSection(page), notesSection(page, current));
    isCurrent = replaceViewer(text(page.title), pageMeta(page), pageTools(page, current), content);
    if (successMessage) showMessage(message, successMessage);
    state.message = message;
  }

  function openPage(space, page, { revision = 0 } = {}) {
    state.selectedPage = { space, page };
    state.selected = space + "/" + page;
    const params = revision ? { revision: String(revision) } : {};
    const isCurrent = replaceViewer("Loading…", "", [], []);
    return get(pagePath(space, page), params, "The page could not be loaded.")
      .then(payload => { if (isCurrent()) showPage(payload.page); })
      .catch(error => {
        if (!isCurrent()) return;
        const message = element(document, "div", "knowledge-message");
        nodes.viewer.replaceChildren(message);
        nodes.viewerTitle.textContent = "The page could not be loaded";
        showMessage(message, refusalText(error, "The page could not be loaded."), { error: true });
      });
  }

  function changeFailed(error, fallback) {
    if (state.message && state.message.isConnected) showRefusal(state.message, error, fallback);
    else setPageStatus(refusalText(error, fallback), true);
  }

  function archive(page, isCurrent) {
    return post(pagePath(page.space, page.page, "/archive"), { base_revision: page.revision }, "The page was not archived.")
      .then(payload => {
        if (!isCurrent()) return;
        showPage(payload.page, "Archived. Every revision is kept; select Restore to bring it back.");
        loadList();
      })
      .catch(error => { if (isCurrent()) changeFailed(error, "The page was not archived. Refresh and try again."); });
  }

  function revert(page, revision, isCurrent) {
    return post(pagePath(page.space, page.page, "/revert"), { revision, base_revision: page.revision }, "The page was not reverted.")
      .then(payload => {
        if (!isCurrent()) return;
        showPage(payload.page, "Reverted to revision " + revision + ". This is a new revision; nothing in the history was removed.");
        loadList();
      })
      .catch(error => { if (isCurrent()) changeFailed(error, "The page was not reverted. Refresh and try again."); });
  }

  // -- history -------------------------------------------------------------------

  function revisionItem(page, revision, isCurrent) {
    const item = element(document, "li", "knowledge-revision");
    const copy = element(document, "div", "memory-item-copy");
    const word = CHANGE_WORDS[revision.change_kind] || text(revision.change_kind);
    copy.append(
      element(document, "strong", "", "Revision " + revision.revision + " · " + word),
      element(document, "p", "memory-meta", "By " + editorWord(revision.editor) + " · " + formatTime(revision.created_at_utc)),
    );
    if (text(revision.change_summary)) copy.append(element(document, "p", "memory-fact", text(revision.change_summary)));
    const actions = element(document, "div", "memory-item-actions");
    if (Number(revision.revision) !== Number(page.revision)) {
      actions.append(
        button("View", "ghost memory-action-button", () => openPage(page.space, page.page, { revision: revision.revision })),
        confirmButton("Revert", "Confirm revert", "secondary memory-action-button", () => revert(page, revision.revision, isCurrent)),
      );
    } else {
      actions.append(element(document, "span", "memory-admin-note", "Current"));
    }
    item.append(copy, actions);
    return item;
  }

  function openHistory(space, page) {
    const isLoading = replaceViewer("Loading history…", "", [], []);
    return get(pagePath(space, page, "/history"), {}, "The history could not be loaded.")
      .then(payload => {
        if (!isLoading()) return;
        const summary = (payload.pages || [])[0];
        const message = element(document, "div", "knowledge-message");
        message.hidden = true;
        const list = element(document, "ol", "knowledge-revisions");
        let isCurrent = () => false;
        const current = () => isCurrent();
        (payload.revisions || []).forEach(revision => list.append(revisionItem(summary, revision, current)));
        isCurrent = replaceViewer("History of " + text(summary.title), "Newest first · the last 50 revisions",
          [button("Back to the page", "ghost", () => openPage(space, page))], [message, list]);
        state.message = message;
      })
      .catch(error => {
        if (isLoading()) setPageStatus(refusalText(error, "The history could not be loaded."), true);
      });
  }

  // -- the editor ----------------------------------------------------------------

  function showLatest(editor, page, isCurrent) {
    return get(pagePath(page.space, page.page), {}, "The latest version could not be loaded.").then(payload => {
      if (!isCurrent()) return;
      const latest = payload.page;
      const details = element(document, "details", "knowledge-latest");
      details.open = true;
      details.append(element(document, "summary", "", "Latest version (revision " + latest.revision + ")"), bodyNode(latest.body, latest.links));
      const old = editor.form.querySelector(".knowledge-latest");
      if (old) old.remove();
      editor.form.prepend(details);
      editor.baseRevision = Number(latest.revision);
      showMessage(editor.message, TEXT.LATEST_SHOWN);
    }).catch(error => {
      if (isCurrent()) showMessage(editor.message, refusalText(error, "The latest version could not be loaded."), { error: true });
    });
  }

  /** Edit (or Restore) `page`, or write a new page when `page` is null. */
  function openEditor(page) {
    const creating = !page;
    const space = creating ? (state.tab === TABS.PERSONAL ? TABS.PERSONAL : TABS.TEAM) : page.space;
    const heading = creating ? (space === TABS.TEAM ? "New team page" : "New personal page") : "";
    const editor = pageEditor(document, page || {}, { saveLabel: creating ? "Create page" : "Save", heading });
    editor.baseRevision = creating ? 0 : Number(page.revision);
    const isCurrent = replaceViewer(creating ? heading : "Editing " + text(page.title),
      creating ? "The page's name is made from its title." : pageMeta(page), [], [editor.form]);
    editor.cancel.addEventListener("click", () => {
      if (creating) emptyViewer();
      else openPage(page.space, page.page);
    });
    const save = confirmReview => {
      const values = editor.values();
      if (!values.title || !values.body.trim()) {
        return showMessage(editor.message, "A page needs a title and some text.", { error: true });
      }
      const done = client.beginAction(editor.save, "Saving…", editor.actions);
      const path = creating ? `${KNOWLEDGE_API}/pages/${encodeURIComponent(space)}` : pagePath(space, page.page, "/save");
      return post(path, { ...values, base_revision: editor.baseRevision, confirm_review: confirmReview }, "The page was not saved.")
        .then(payload => {
          if (!isCurrent()) return;
          showPage(payload.page, creating ? "Page created." : "Saved.");
          loadList(creating ? 0 : state.offset);
        })
        .catch(error => {
          if (!isCurrent()) return;
          const extra = text(error.code) === "conflict" && !creating
            ? [button("Show the latest version", "secondary memory-action-button", () => showLatest(editor, page, isCurrent))]
            : [];
          showRefusal(editor.message, error, "The page was not saved.", () => save(true), extra);
        })
        .finally(done);
    };
    editor.form.addEventListener("submit", event => { event.preventDefault(); save(false); });
    editor.focus();
    return editor;
  }

  // -- proposals -----------------------------------------------------------------

  function proposalChange(proposal) {
    const current = proposal.current;
    const parts = [];
    if (proposal.kind === "note") {
      parts.push(element(document, "h3", "", "Note to add to team/" + text(proposal.page)), element(document, "p", "memory-fact", text(proposal.body)));
      return parts;
    }
    if (proposal.kind === "archive") {
      if (current && Number(current.revision) !== Number(proposal.base_revision)) parts.push(noticeLine(TEXT.STALE_ARCHIVE));
      parts.push(element(document, "h3", "", "Archive team/" + text(proposal.page)));
      parts.push(noticeLine(TEXT.ARCHIVE_PROPOSAL));
      if (current) parts.push(bodyNode(current.body, current.links));
      return parts;
    }
    if (!current) {
      parts.push(element(document, "h3", "", "The new page"), bodyNode(proposal.body));
      return parts;
    }
    if (Number(current.revision) !== Number(proposal.base_revision)) parts.push(noticeLine(TEXT.STALE_PROPOSAL));
    const fields = fieldChanges(document, current, proposal);
    if (fields) parts.push(element(document, "h3", "", "Details that change"), fields);
    parts.push(element(document, "h3", "", "Text changes"), diffBlock(document, current.body, proposal.body));
    return parts;
  }

  function proposalMeta(proposal) {
    return [
      PROPOSAL_WORDS[proposal.kind] || text(proposal.kind),
      "From " + editorWord(proposal.agent_type) + " working for you",
      formatTime(proposal.created_at_utc),
    ].join(" · ");
  }

  function decide(proposal, decision, body, isCurrent, fallback) {
    const session = state.session;
    const path = `${KNOWLEDGE_API}/contributions/${encodeURIComponent(proposal.contribution_id)}/${decision}`;
    return post(path, body, fallback).then(payload => {
      if (session !== state.session) return payload;
      // A decision changes this person's count even after they leave its viewer.
      if (!isCurrent()) {
        proposalCount.refresh({ force: true });
        return payload;
      }
      state.selected = "";
      state.selectedPage = null;
      loadList(0).then(current => {
        // The list normally supplies the total. If it fails or navigation
        // discards it, read the count without changing the new viewer.
        if (!current && session === state.session) proposalCount.refresh({ force: true });
      });
      return payload;
    });
  }

  function publishBody(proposal, edited) {
    const current = proposal.current;
    return { base_revision: current ? Number(current.revision) : 0, ...(edited ? { ...edited, edited: true } : {}) };
  }

  function published(payload, message) {
    const page = payload && payload.page;
    const open = page ? [button("Open the page", "secondary memory-action-button", () => {
      switchTab(TABS.TEAM);
      openPage(page.space, page.page);
    })] : [];
    replaceViewer("Published", "", [], [message]);
    showMessage(message, page ? "Published to the team page “" + text(page.title) + "”." : "Published.", { extra: open });
  }

  function proposalTools(proposal, message, isCurrent) {
    const flagged = (proposal.review_flags || []).length > 0;
    const publish = () => decide(proposal, "publish", publishBody(proposal, null), isCurrent, "The proposal was not published.")
      .then(payload => { if (isCurrent()) published(payload, message); })
      .catch(error => { if (isCurrent()) showRefusal(message, error, "The proposal was not published. Refresh and try again."); });
    return [
      flagged ? confirmButton("Publish", "Confirm publish", "primary", publish) : button("Publish", "primary", publish),
      ...(proposal.kind === "archive" ? [] : [button("Edit, then publish", "ghost", () => openProposalEditor(proposal))]),
      confirmButton("Withdraw", "Confirm withdraw", "danger is-danger", () =>
        decide(proposal, "withdraw", {}, isCurrent, "The proposal was not withdrawn.")
          .then(() => {
            if (!isCurrent()) return;
            replaceViewer("Withdrawn", "", [], [message]);
            showMessage(message, "Proposal withdrawn. Nothing changed on the team page.");
          })
          .catch(error => { if (isCurrent()) showRefusal(message, error, "The proposal was not withdrawn. Refresh and try again."); })),
    ];
  }

  function showProposal(proposal) {
    state.selected = text(proposal.contribution_id);
    state.selectedPage = null;
    renderList();
    const message = element(document, "div", "knowledge-message");
    message.hidden = true;
    const content = [message];
    if ((proposal.review_flags || []).length) {
      const warning = element(document, "div", "knowledge-notice is-attention");
      warning.append(element(document, "p", "", TEXT.PUBLISH_FLAGGED), flagList(document, proposal.review_flags));
      content.push(warning);
    }
    if (text(proposal.change_summary)) {
      const why = element(document, "section", "knowledge-section");
      why.append(element(document, "h3", "", "Why (from " + editorWord(proposal.agent_type) + ")"),
        element(document, "p", "memory-fact", text(proposal.change_summary)));
      content.push(why);
    }
    content.push(...proposalChange(proposal));
    let isCurrent = () => false;
    const current = () => isCurrent();
    isCurrent = replaceViewer(text(proposal.title), proposalMeta(proposal), proposalTools(proposal, message, current), content);
  }

  function openProposal(contributionId) {
    const isLoading = replaceViewer("Loading…", "", [], []);
    return get(`${KNOWLEDGE_API}/contributions/${encodeURIComponent(contributionId)}`, {}, "The proposal could not be loaded.")
      .then(payload => { if (isLoading()) showProposal(payload.contribution); })
      .catch(error => { if (isLoading()) setPageStatus(refusalText(error, "The proposal could not be loaded."), true); });
  }

  function openProposalEditor(proposal) {
    const editor = pageEditor(document, proposal, { saveLabel: "Publish", heading: "Edit before publishing" });
    const isCurrent = replaceViewer("Edit, then publish: " + text(proposal.title), proposalMeta(proposal), [], [editor.form]);
    editor.cancel.addEventListener("click", () => showProposal(proposal));
    editor.form.addEventListener("submit", event => {
      event.preventDefault();
      const done = client.beginAction(editor.save, "Publishing…", editor.actions);
      decide(proposal, "publish", publishBody(proposal, editor.values()), isCurrent, "The proposal was not published.")
        .then(payload => { if (isCurrent()) published(payload, editor.message); })
        .catch(error => { if (isCurrent()) showRefusal(editor.message, error, "The proposal was not published."); })
        .finally(done);
    });
    editor.focus();
  }

  // -- wiring --------------------------------------------------------------------

  nodes.tabs.team.addEventListener("click", () => switchTab(TABS.TEAM));
  nodes.tabs.personal.addEventListener("click", () => switchTab(TABS.PERSONAL));
  nodes.tabs.proposals.addEventListener("click", () => switchTab(TABS.PROPOSALS));
  nodes.searchForm.addEventListener("submit", event => {
    event.preventDefault();
    state.query = String(nodes.searchInput.value || "").trim();
    state.offset = 0;
    loadList(0);
  });
  nodes.pageStatus.addEventListener("change", () => {
    state.pageStatus = nodes.pageStatus.value;
    state.query = String(nodes.searchInput.value || "").trim();
    state.offset = 0;
    loadList(0);
  });
  nodes.newButton.addEventListener("click", () => openEditor(null));
  nodes.refreshButton.addEventListener("click", () => {
    loadList();
    // The proposals list carries the count; the other tabs read it.
    if (!isProposals()) proposalCount.refresh();
    // An open editor keeps what the person typed; only the list reloads.
    if (editorOpen() || !state.selected) return;
    if (state.selectedPage) openPage(state.selectedPage.space, state.selectedPage.page);
    else if (isProposals()) openProposal(state.selected);
  });

  return {
    /** Opens the page on Team with the first page of the list, and keeps the proposals badge current. */
    load() {
      setTab(state.tab);
      emptyViewer();
      proposalCount.start();
      return loadList(0);
    },
    openPage,
    openEditor,
    openProposal,
    switchTab,
    /** Sign-out: back to Team with nothing shown. */
    clear() {
      proposalCount.stop();
      client.invalidateSession();
      state.generation += 1;
      state.session += 1;
      Object.assign(state, { tab: TABS.TEAM, pageStatus: "active", query: "", offset: 0, total: 0, rows: [],
        selected: "", selectedPage: null, message: null });
      setTab(TABS.TEAM);
      nodes.list.replaceChildren();
      nodes.searchInput.value = "";
      nodes.pageStatus.value = state.pageStatus;
      replaceViewer("Select a page", "", [], []);
      setPageStatus("", false);
    },
  };
}

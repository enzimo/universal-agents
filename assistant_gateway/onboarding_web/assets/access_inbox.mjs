/**
 * Access Inbox in Web Chat Settings: agents' access requests and access notices
 * (access plan section 9.1).
 *
 * - Access requests: an agent asked for access to something. The gateway decides
 *   who may approve (`can_decide`), which choices exist and what they are called.
 *   The browser shows the server's labels and sends back only the chosen key.
 * - Notices: updates about access changes and stuck work (for example, an
 *   agent passed its access on, or a schedule is not running). They need no
 *   decision and are never shown as chat pop-ups. The list opens on unread
 *   notices (an Unread / All switch shows read ones too), comes in pages of
 *   20, and notices are marked read one at a time, by selection, or all at
 *   once.
 *
 * The Settings cog and the Inbox tab carry one badge: requests this person can
 * decide plus their unread notices (owner's decision 2026-10-04, so notices no
 * longer pile up unseen). `createInboxCounts` keeps that count for the whole
 * page: one small read every 30 seconds while the page is visible, and right
 * away when something may have changed; every Inbox read and change updates
 * it too.
 *
 * All server and agent-written text is rendered with `textContent`, never as
 * HTML. The view-model and rendering functions are exported so node tests can
 * check them without a browser; `createAccessInbox` connects them to the page.
 */
import { ATTENTION_COUNT_POLL_MS, ATTENTION_COUNT_SOON_MS, createAttentionCount } from "./settings_support.mjs";

export const ACCESS_INBOX_API = "/api/web-chat/access-inbox";
/** System messages announcing an access request (assistant_gateway.domain.message_types.ACCESS_REVIEW_MESSAGE_TYPE). */
export const ACCESS_REVIEW_MESSAGE_TYPE = "access_review";
export const ACCESS_REQUEST_LINK_KEY = "access_request";

export const WAITING_FOR_APPROVER = "Waiting for someone who can approve it";
// The chat card's badge for a person who may not decide (ui/DESIGN.md, "Access Review Cards in Web Chat").
export const WAITING_FOR_ADMINISTRATOR = "Waiting for an administrator";
export const NEEDS_DECISION = "Needs your decision";
export const DETAILS_LABEL = "Details";
export const LACKS_ACCESS_NOTE =
  "The person this task works for does not have this access either; approving gives it to both.";
export const LACKS_ACCESS_YOU_NOTE = "You don't have this access either; approving gives it to you as well.";
export const KEEP_LABEL = "Keep for later tasks";
export const HAND_ON_LABEL = "TeamLead may hand this on to workers without asking you";
// How long kept access lasts; hours, or null for "until I remove it".
export const KEEP_DURATIONS = Object.freeze([
  { label: "Until I remove it", hours: null },
  { label: "For 1 day", hours: 24 },
  { label: "For 7 days", hours: 168 },
  { label: "For 30 days", hours: 720 },
]);
// Section headings and warning chips for review rows (ui/DESIGN.md, "Inbox").
export const ROW_SECTIONS = Object.freeze({
  tool: "New tools", access: "Access", restriction: "Restrictions", folder: "Folders to register",
  account: "Credentials to link", revocation: "Access to remove", delivery: "Delivery",
});
export const ROW_FLAGS = Object.freeze({
  new_host: "New host", private_address: "Private address", full_access: "Full access",
  can_hand_on: "Can hand on", runs_code: "Runs code", new_folder: "New folder", setup_steps: "Steps for people",
  new_program: "New program", download_host: "Download", team_account: "Team account",
});
export const REMOVE_ACCESS_LABEL = "Remove access";
// Notices: the Unread / All switch (the server's `?notices=` values), page size and bulk actions.
export const NOTICE_VIEWS = Object.freeze({ UNREAD: "unread", ALL: "all" });
export const NOTICE_PAGE_SIZE = 20;
export const NOTICES_READ_PATH = `${ACCESS_INBOX_API}/notices/read`;
export const INBOX_COUNTS_PATH = `${ACCESS_INBOX_API}/counts`;
export const INBOX_COUNTS_POLL_MS = ATTENTION_COUNT_POLL_MS;
// Coalesces a burst of chat messages into one count read.
export const INBOX_COUNTS_SOON_MS = ATTENTION_COUNT_SOON_MS;
export const MARK_READ_LABEL = "Mark as read";
export const SELECT_PAGE_LABEL = "Select all on this page";
export const MARK_SELECTED_LABEL = "Mark selected as read";
export const MARK_ALL_LABEL = "Mark all as read";
export const CONFIRM_MARK_ALL_LABEL = "Confirm: mark all as read";
export const CONFIRM_REMOVAL_LABEL = "Confirm removal";
export const ACCESS_REMOVED_LABEL = "Access removed";

// Presentation for the choice keys the gateway sends. The visible label always
// comes from the server; style, busy wording and outcome text are local.
const CHOICE_PRESENTATION = Object.freeze({
  approve: { style: "primary", busyLabel: "Approving…", outcome: "Approved" },
  approve_read: { style: "secondary", busyLabel: "Approving…", outcome: "Approved read only" },
  approve_task: { style: "secondary", busyLabel: "Approving…", outcome: "Approved for this task" },
  approve_all: { style: "primary", busyLabel: "Approving…", outcome: "Approved as proposed" },
  deny: { style: "danger", busyLabel: "Denying…", outcome: "Denied" },
});

const TIME_FORMAT = Object.freeze({
  year: "numeric", month: "short", day: "numeric",
  hour: "numeric", minute: "2-digit", timeZoneName: "short",
});

function text(value) {
  return value === undefined || value === null ? "" : String(value).trim();
}

/** Reads the request id from a link such as `/webchat?view=inbox#access_request=<id>`. */
export function parseAccessRequestLink(hash) {
  const fragment = text(hash);
  const params = new URLSearchParams(fragment.startsWith("#") ? fragment.slice(1) : fragment);
  return text(params.get(ACCESS_REQUEST_LINK_KEY));
}

/** Local, readable time such as "Sep 25, 2026, 3:04 PM EDT"; "" when the value is missing or invalid. */
export function localTimeFormatter({ locale, timeZone } = {}) {
  const format = new Intl.DateTimeFormat(locale, timeZone ? { ...TIME_FORMAT, timeZone } : TIME_FORMAT);
  return value => {
    const date = new Date(text(value));
    return Number.isNaN(date.getTime()) ? "" : format.format(date);
  };
}

const localTime = localTimeFormatter();

export function decidableReviewCount(reviews) {
  return (Array.isArray(reviews) ? reviews : []).filter(review => review && review.can_decide === true).length;
}

/** The counts as the gateway sends them (`decidable_requests`, `unread_notices`); missing values count as 0. */
export function inboxCounts(payload) {
  const count = value => (Number.isInteger(value) && value >= 0 ? value : 0);
  return {
    decidableRequests: count(payload && payload.decidable_requests),
    unreadNotices: count(payload && payload.unread_notices),
  };
}

/** The badge on the Settings cog and the Inbox tab: requests to decide plus unread notices; "" for none. */
export function inboxBadgeText(counts) {
  const total = (counts ? counts.decidableRequests + counts.unreadNotices : 0) || 0;
  return total ? String(total) : "";
}

/** What the Inbox count stands for, in words ("2 requests to decide, 1 unread notice"); "" for none. */
export function inboxCountWords(counts) {
  const parts = [];
  const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;
  if (counts && counts.decidableRequests) parts.push(plural(counts.decidableRequests, "request to decide", "requests to decide"));
  if (counts && counts.unreadNotices) parts.push(plural(counts.unreadNotices, "unread notice", "unread notices"));
  return parts.join(", ");
}

/** What the badge stands for, in words, for screen readers and the tooltip. */
export function inboxBadgeLabel(counts) {
  const words = inboxCountWords(counts);
  return words ? `Inbox: ${words}` : "";
}

export function unreadNoticeCount(payload) {
  const count = payload && payload.unread_notices;
  if (Number.isInteger(count) && count >= 0) return count;
  const notices = payload && Array.isArray(payload.notices) ? payload.notices : [];
  return notices.filter(notice => notice && notice.read !== true).length;
}

export function unreadNoticesText(count) {
  return count > 0 ? `${count} unread` : "";
}

/** The Inbox read for one page of notices in one view. */
export function inboxPath(view, offset) {
  return `${ACCESS_INBOX_API}?notices=${encodeURIComponent(view)}&offset=${offset}&limit=${NOTICE_PAGE_SIZE}`;
}

/** What the notice list says when it is empty. */
export function emptyNoticesText(view, allTotal) {
  if (view !== NOTICE_VIEWS.UNREAD) return "No notices.";
  return allTotal > 0 ? "No unread notices. Choose All to see earlier ones." : "No notices.";
}

/** What the first click on Mark all as read says (the second click confirms). */
export function markAllPrompt(unread) {
  const what = unread === 1 ? "the 1 unread notice" : `all ${unread} unread notices`;
  return `Select ${CONFIRM_MARK_ALL_LABEL} to mark ${what} as read, on every page.`;
}

/** What a bulk Mark as read says afterwards. */
export function markedText(marked) {
  if (!marked) return "Nothing new to mark; those notices were already read.";
  return marked === 1 ? "1 notice marked as read." : `${marked} notices marked as read.`;
}

/** Server choices this page knows how to show, in server order. Unknown keys are left out. */
export function reviewChoices(review) {
  if (!review || review.can_decide !== true || !Array.isArray(review.choices)) return [];
  return review.choices.flatMap(choice => {
    const key = text(choice && choice.choice);
    const label = text(choice && choice.label);
    const presentation = Object.hasOwn(CHOICE_PRESENTATION, key) ? CHOICE_PRESENTATION[key] : null;
    if (!presentation || !label) return [];
    return [{ choice: key, label, style: presentation.style, busyLabel: presentation.busyLabel }];
  });
}

/**
 * What one choice does, for the card's boxes as they are now (the gateway's
 * `choice_effects`, assistant_gateway/access_choice_effects.py): `kept` is
 * whether Keep for later tasks is on, `handOn` whether TeamLead may hand it on
 * (undefined when the card has no such box).
 */
export function choiceEffectText(effects, choice, { kept = false, handOn } = {}) {
  const variants = effects && typeof effects === "object" && !Array.isArray(effects) ? effects[choice] : null;
  if (!variants || typeof variants !== "object") return "";
  if (variants.any) return text(variants.any);
  const base = kept ? "kept" : "task";
  return text((handOn === false && variants[`${base}_no_hand_on`]) || variants[base]);
}

/** The keep helper text: the gateway's words for this request, or whose work the kept access serves. */
export function keepHelpText(review) {
  if (text(review.keep_help)) return text(review.keep_help);
  const agent = text(review.agent_type) || "the agent";
  const whose = review.working_for_you === true ? "you" : (text(review.working_for_name) || "this person");
  return `Off: only for this task. On: whenever ${agent} works for ${whose}, until it is removed.`;
}

/** Rows to approve, grouped by section in server order; unknown flags are left out. */
export function reviewRows(review) {
  const rows = Array.isArray(review && review.rows) ? review.rows : [];
  return rows.flatMap(row => {
    const id = text(row && row.item_id);
    const value = text(row && row.text);
    if (!id || !value) return [];
    const section = Object.hasOwn(ROW_SECTIONS, text(row.section)) ? text(row.section) : "access";
    const flags = (Array.isArray(row.flags) ? row.flags : []).map(text).filter(flag => Object.hasOwn(ROW_FLAGS, flag));
    return [{ id, text: value, section, sectionLabel: ROW_SECTIONS[section], flags: flags.map(flag => ROW_FLAGS[flag]),
      keepable: row.keepable === true, canHandOn: row.can_hand_on === true }];
  });
}

export function reviewCardModel(review, formatTime = localTime) {
  const agent = text(review.agent_type) || text(review.requested_by) || "the agent";
  const expires = review.expires_at_utc ? formatTime(review.expires_at_utc) : "";
  const lifetime = text(review.lifetime_words);
  const canDecide = review.can_decide === true;
  // A staged change (kind "change") shows exactly what it changes instead of an access level.
  const effect = Array.isArray(review.effect) ? review.effect.map(line => text(line)).filter(Boolean) : [];
  const isChange = text(review.kind) === "change";
  const title = text(review.title) || (isChange ? "Change request" : "Access request");
  return {
    id: text(review.request_id),
    title,
    // One line written by the gateway (a change's trusted summary); the chat card leads with it.
    summary: text(review.summary) || title,
    effect,
    details: (isChange
      ? [
        { label: "For whose work", value: text(review.for_whose_work) },
        { label: "Asked", value: formatTime(review.created_at_utc) },
      ]
      : [
        { label: "Access", value: text(review.level_words) || text(review.level) },
        { label: "For whose work", value: text(review.for_whose_work) },
        { label: "How long", value: expires ? `${lifetime || "until a set time"} (ends ${expires})` : lifetime },
        { label: "Asked", value: formatTime(review.created_at_utc) },
      ]).filter(row => row.value),
    rows: reviewRows(review),
    keepHelp: keepHelpText(review),
    choiceEffects: review.choice_effects && typeof review.choice_effects === "object" ? review.choice_effects : {},
    effectsKeptByDefault: text(review.choice_effects_default) === "kept",
    reasonLabel: `Why (from ${agent})`,
    reason: text(review.reason) || "No reason was given.",
    lacksAccessNote: review.person_lacks_access !== true ? ""
      : review.working_for_you === true ? LACKS_ACCESS_YOU_NOTE
        : text(review.working_for_name)
          ? `${text(review.working_for_name)} doesn't have this access; approving gives it to them too.`
          : LACKS_ACCESS_NOTE,
    canDecide,
    status: canDecide
      ? { label: NEEDS_DECISION, tone: "needs-decision" }
      : { label: WAITING_FOR_APPROVER, tone: "waiting" },
    choices: canDecide ? reviewChoices(review) : [],
  };
}

/** What a saved decision did, from the review the server returned. `kept` is the Keep box. */
export function decisionOutcome(choice, review, kept) {
  const status = text(review && review.status);
  if (status === "denied") {
    return { label: "Denied", tone: "denied", explanation: "Your decision was saved. No access was given, and the agent will be told." };
  }
  if (status === "granted") {
    let approved = choice !== "deny" && Object.hasOwn(CHOICE_PRESENTATION, choice)
      ? CHOICE_PRESENTATION[choice].outcome : "Approved";
    if (kept === false && choice !== "approve_all") approved = `${approved} for this task`;
    else if (kept === true && choice === "approve") approved = "Approved and kept";
    return { label: approved, tone: "granted", explanation: "Your decision was saved, and the agent will be told." };
  }
  if (status === "withdrawn") {
    return { label: "Withdrawn", tone: "waiting", explanation: "The task that asked for this access ended first, so no access was given." };
  }
  return { label: "Decision saved", tone: "waiting", explanation: "Refresh the Inbox to see the current state of this request." };
}

// Outcome words for a decision stored on the server, by the choice that was made.
const STORED_APPROVALS = Object.freeze({
  approve: "Approved", approve_read: "Approved reading only", approve_task: "Approved for this task",
  approve_all: "Approved as proposed",
});

/**
 * The outcome of a request that is no longer waiting, from the stored decision
 * (a chat card after a reload, or after someone decided on another device).
 * Returns null while the request still waits.
 */
export function storedDecisionOutcome(review, formatTime = localTime) {
  const status = text(review && review.status);
  if (!status || status === "waiting") return null;
  const who = text(review.decided_by_name);
  const when = review.decided_at_utc ? formatTime(review.decided_at_utc) : "";
  const decided = `Decided${who ? ` by ${who}` : ""}${when ? ` on ${when}` : ""}.`;
  if (status === "granted") {
    const choice = text(review.decision);
    const label = Object.hasOwn(STORED_APPROVALS, choice) ? STORED_APPROVALS[choice] : "Approved";
    return { label, tone: "granted", explanation: decided };
  }
  if (status === "denied") return { label: "Denied", tone: "denied", explanation: `${decided} No access was given.` };
  if (status === "withdrawn") {
    return { label: "Withdrawn", tone: "waiting", explanation: "The task that asked for this ended first, so nothing was given." };
  }
  return { label: "Decided", tone: "waiting", explanation: decided };
}

export function noticeRowModel(notice, formatTime = localTime, removedGrants = new Set()) {
  const grantId = text(notice.grant_id);
  const unread = notice.read !== true;
  return {
    id: text(notice.notice_id),
    title: text(notice.title) || "Notice",
    summary: text(notice.summary),
    time: formatTime(notice.created_at_utc),
    timeValue: text(notice.created_at_utc),
    unread,
    canMarkRead: unread,
    grantId,
    accessRemoved: Boolean(grantId) && removedGrants.has(grantId),
    canRemoveAccess: Boolean(grantId) && !removedGrants.has(grantId),
  };
}

let idSequence = 0;
function uniqueId(prefix) {
  idSequence += 1;
  return `${prefix}-${idSequence}`;
}

function element(document, tagName, className, value) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function statusBadge(document, label, tone) {
  return element(document, "span", `access-inbox-status is-${tone}`, label);
}

function detailRow(document, label, value) {
  const row = element(document, "div", "permission-detail-row");
  row.append(element(document, "span", "permission-detail-label", label),
    element(document, "strong", "permission-detail-value", value));
  return row;
}

function cardMessage(document) {
  const message = element(document, "p", "access-inbox-card-message");
  message.setAttribute("role", "status");
  message.setAttribute("aria-live", "polite");
  message.hidden = true;
  return message;
}

export function setCardMessage(message, value, isError) {
  message.textContent = value || "";
  message.hidden = !value;
  message.classList.toggle("is-error", Boolean(value && isError));
}

function cardHeading(document, card, title, badge, lead = null) {
  const titleId = uniqueId("access-inbox-title");
  const heading = element(document, "div", "permission-request-heading");
  const copy = element(document, "div", "permission-request-heading-copy");
  const titleNode = element(document, "h5", "permission-operation", title);
  titleNode.id = titleId;
  copy.append(titleNode);
  if (lead) heading.append(lead);
  heading.append(copy);
  if (badge) heading.append(badge);
  card.setAttribute("aria-labelledby", titleId);
  card.append(heading);
  return copy;
}

/**
 * The compact card's folded part: the full title and everything the Inbox
 * card shows, closed until the person opens **Details**.
 */
function foldedDetails(document, model) {
  const fold = element(document, "details", "access-review-more");
  fold.append(element(document, "summary", "access-review-more-toggle", DETAILS_LABEL),
    element(document, "p", "access-review-full-title", model.title));
  return fold;
}

/** Warning chips from every row, shown next to a compact card's summary so they are never folded away. */
function compactFlags(document, model) {
  const flags = [...new Set(model.rows.flatMap(row => row.flags))];
  if (!flags.length) return null;
  const line = element(document, "p", "access-review-flags");
  flags.forEach(flag => line.append(element(document, "span", "access-inbox-status is-warning access-review-flag", flag)));
  return line;
}

function effectSection(document, model) {
  const changes = element(document, "section", "access-review-effect");
  const list = element(document, "ul", "access-review-effect-list");
  model.effect.forEach(line => list.append(element(document, "li", "", line)));
  changes.append(element(document, "h6", "permission-section-title", "What this changes"), list);
  return changes;
}

function lacksAccessWarning(document, model) {
  // The icon is decoration; the sentence carries the meaning.
  const icon = element(document, "span", "access-review-warning-icon", "!");
  icon.setAttribute("aria-hidden", "true");
  const note = element(document, "p", "access-review-warning");
  note.append(icon, element(document, "span", "", model.lacksAccessNote));
  return note;
}

/**
 * One access request card. `onChoose` receives
 * `{ review, choice, button, note, card, group, message }` when a choice is clicked.
 *
 * `compact` is the Web Chat card: the heading is the one-line summary, warning
 * chips and the lacks-access warning stay visible, everything else (full
 * title, details, rows with their Keep boxes, reason and note) is folded under
 * **Details**, and a person who may not decide sees "Waiting for an
 * administrator". The choices and what they send are the same as the Inbox's.
 */
export function renderReviewCard(document, review, { formatTime = localTime, onChoose, warn, compact = false } = {}) {
  const model = reviewCardModel(review, formatTime);
  const card = element(document, "article", `permission-request-card access-review-card${compact ? " is-compact" : ""}`);
  card.setAttribute("data-request-id", model.id);
  const badgeLabel = compact && !model.canDecide ? WAITING_FOR_ADMINISTRATOR : model.status.label;
  cardHeading(document, card, compact ? model.summary : model.title, statusBadge(document, badgeLabel, model.status.tone));
  const fold = compact ? foldedDetails(document, model) : null;
  const body = fold || card;
  if (compact) {
    const flags = compactFlags(document, model);
    if (flags) card.append(flags);
    if (model.lacksAccessNote) card.append(lacksAccessWarning(document, model));
    card.append(fold);
  }
  const details = element(document, "div", "access-review-details");
  model.details.forEach(row => details.append(detailRow(document, row.label, row.value)));
  body.append(details);
  const keepBoxes = [];
  const handOnBoxes = [];
  const keepSelects = [];
  if (model.rows.length) {
    body.append(renderReviewRows(document, model, { canDecide: model.canDecide, keepBoxes, handOnBoxes, keepSelects }));
  } else if (model.effect.length) {
    body.append(effectSection(document, model));
  }
  if (model.lacksAccessNote && !compact) card.append(lacksAccessWarning(document, model));
  const why = element(document, "section", "permission-agent-context access-review-reason");
  why.append(element(document, "h6", "permission-section-title", model.reasonLabel),
    element(document, "p", "", model.reason));
  body.append(why);
  const message = cardMessage(document);
  const offered = Array.isArray(review.choices) ? review.choices.length : 0;
  if (model.canDecide && model.choices.length < offered && typeof warn === "function") {
    warn("access_inbox_unknown_choice", { request_id: model.id,
      choices: review.choices.map(choice => text(choice && choice.choice)) });
  }
  if (model.canDecide && model.choices.length) {
    const decision = element(document, "div", "access-review-decision");
    const noteId = uniqueId("access-review-note");
    const label = element(document, "label", "permission-section-title", "Note (optional)");
    label.htmlFor = noteId;
    const note = element(document, "textarea", "textarea access-review-note");
    note.id = noteId;
    note.rows = 2;
    note.placeholder = "Add a note for the record, if you like.";
    const group = element(document, "div", "permission-choice-row access-review-choices");
    const help = choiceHelp(document, model, { keepBoxes, handOnBoxes });
    model.choices.forEach(choice => {
      const button = element(document, "button", `button ${choice.style} permission-choice-button`, choice.label);
      button.type = "button";
      button.setAttribute("data-choice", choice.choice);
      button.addEventListener("click", () => {
        if (button.disabled || typeof onChoose !== "function") return;
        onChoose({ review, choice, button, note, card, group, message, keepBoxes, handOnBoxes, keepSelects });
      });
      help.describe(button, choice.choice);
      group.append(button);
    });
    // The compact card folds the optional note with the details; the buttons
    // and what each one does stay visible.
    if (fold) fold.append(label, note);
    else decision.append(label, note);
    decision.append(group);
    if (help.list) decision.append(help.title, help.list);
    card.append(decision);
  } else if (model.canDecide) {
    setCardMessage(message, "No choices are available for this request here. Refresh the Inbox and try again.", true);
  }
  card.append(message);
  return card;
}

/**
 * "What each choice does" (ui/DESIGN.md): one always-visible line per button,
 * in the gateway's words, following the Keep and hand-on boxes. A button
 * points to its line (aria-describedby), and hovering or focusing it marks
 * the line; no native title tooltip.
 */
function choiceHelp(document, model, { keepBoxes, handOnBoxes }) {
  const lines = new Map();
  const title = element(document, "p", "permission-section-title", "What each choice does");
  const list = element(document, "ul", "access-review-choice-help");
  const refresh = () => {
    const kept = keepBoxes.length ? keepBoxes.some(box => box.checked) : model.effectsKeptByDefault;
    const handOn = handOnBoxes.length ? handOnBoxes.some(box => box.checked) : undefined;
    lines.forEach(({ item, body }, choice) => {
      body.textContent = choiceEffectText(model.choiceEffects, choice, { kept, handOn });
      item.hidden = !body.textContent;
    });
  };
  [...keepBoxes, ...handOnBoxes].forEach(box => box.addEventListener("change", refresh));
  return {
    title,
    get list() { return lines.size ? list : null; },
    describe(button, choice) {
      if (!choiceEffectText(model.choiceEffects, choice, { kept: true }) && !choiceEffectText(model.choiceEffects, choice)) return;
      const item = element(document, "li", "access-review-choice-help-item");
      item.id = uniqueId("access-review-choice-help");
      item.setAttribute("data-choice", choice);
      const label = button.textContent;
      const body = element(document, "span", "access-review-choice-help-text", "");
      item.append(element(document, "span", "access-review-choice-help-label", `${label}: `), body);
      list.append(item);
      lines.set(choice, { item, body });
      button.setAttribute("aria-describedby", item.id);
      const mark = on => () => item.classList.toggle("is-active", on);
      button.addEventListener("mouseenter", mark(true));
      button.addEventListener("focus", mark(true));
      button.addEventListener("mouseleave", mark(false));
      button.addEventListener("blur", mark(false));
      refresh();
    },
  };
}

/**
 * "What you are approving": one row per item, grouped by section, with warning
 * chips and, for grant rows, the Keep for later tasks box (ticked: the row asked to keep).
 */
function keepDurationSelect(document, box) {
  const select = element(document, "select", "select access-review-keep-for");
  KEEP_DURATIONS.forEach((option, index) => {
    const node = element(document, "option", "", option.label);
    node.value = String(index);
    select.append(node);
  });
  select.setAttribute("aria-label", "How long to keep it");
  select.disabled = !box.checked;
  box.addEventListener("change", () => { select.disabled = !box.checked; });
  return select;
}

export function renderReviewRows(document, model, { canDecide, keepBoxes, handOnBoxes = [], keepSelects = [] }) {
  const section = element(document, "section", "access-review-effect access-review-rows");
  section.append(element(document, "h6", "permission-section-title", "What you are approving"));
  let list = null;
  let current = "";
  model.rows.forEach(row => {
    if (row.section !== current) {
      current = row.section;
      section.append(element(document, "p", "access-review-row-section", row.sectionLabel));
      list = element(document, "ul", "access-review-effect-list");
      section.append(list);
    }
    const item = element(document, "li", "access-review-row");
    item.setAttribute("data-item-id", row.id);
    item.append(element(document, "span", "access-review-row-text", row.text));
    row.flags.forEach(flag => item.append(element(document, "span", "access-inbox-status is-warning access-review-flag", flag)));
    if (row.keepable && canDecide) {
      const boxId = uniqueId("access-review-keep");
      const keep = element(document, "div", "access-review-keep");
      const box = element(document, "input", "access-review-keep-box");
      box.type = "checkbox";
      box.id = boxId;
      // A row is keepable only when the agent asked to keep the access, so the
      // box starts ticked: approving with the defaults gives what was asked
      // (owner's decision 2026-10-01). The person can untick it.
      box.checked = true;
      box.setAttribute("data-item-id", row.id);
      const label = element(document, "label", "access-review-keep-label", KEEP_LABEL);
      label.htmlFor = boxId;
      const help = element(document, "p", "access-review-keep-help", model.keepHelp);
      help.id = `${boxId}-help`;
      box.setAttribute("aria-describedby", help.id);
      const duration = keepDurationSelect(document, box);
      keep.append(box, label, duration, help);
      item.append(keep);
      keepBoxes.push(box);
      keepSelects.push(duration);
    }
    if (row.canHandOn && canDecide) {
      const handOnId = uniqueId("access-review-hand-on");
      const handOn = element(document, "div", "access-review-keep");
      const box = element(document, "input", "access-review-keep-box access-review-hand-on-box");
      box.type = "checkbox";
      box.id = handOnId;
      // Workers get access only from the team lead, so handing on starts ticked
      // (owner's decision 2026-09-30); the person can untick it.
      box.checked = true;
      const label = element(document, "label", "access-review-keep-label", HAND_ON_LABEL);
      label.htmlFor = handOnId;
      handOn.append(box, label);
      item.append(handOn);
      handOnBoxes.push(box);
    }
    list.append(item);
  });
  return section;
}

/** Credential forms an approved proposal opened: only https links from the server are shown. */
export function followUpLinks(review) {
  const links = Array.isArray(review && review.follow_up_links) ? review.follow_up_links : [];
  return links.flatMap(link => {
    const label = text(link && link.label);
    const url = text(link && link.url);
    return label && /^https:\/\//i.test(url) ? [{ label, url }] : [];
  });
}

/** The card that replaces a request after a decision was saved in this page. */
export function renderDecidedCard(document, { id, title, outcome, links = [] }) {
  const card = element(document, "article", `permission-request-card access-review-card is-decided is-${outcome.tone}`);
  card.setAttribute("data-request-id", id);
  cardHeading(document, card, title, statusBadge(document, outcome.label, outcome.tone));
  card.append(element(document, "p", "permission-effect-summary", outcome.explanation));
  if (links.length) {
    const group = element(document, "div", "permission-choice-row access-review-follow-up");
    links.forEach(link => {
      const anchor = element(document, "a", "button primary permission-choice-button", link.label);
      anchor.setAttribute("href", link.url);
      anchor.setAttribute("target", "_blank");
      anchor.setAttribute("rel", "noopener noreferrer");
      group.append(anchor);
    });
    card.append(group);
  }
  return card;
}

/**
 * One notice row. `onMarkRead` and `onRemoveAccess` receive
 * `{ notice, button, row, group, message }`. With `onSelect`, an unread row
 * starts with a checkbox for the bulk Mark selected as read;
 * `onSelect(checked)` runs when it changes.
 */
export function renderNoticeRow(document, notice, {
  formatTime = localTime, removedGrants, onMarkRead, onRemoveAccess, onSelect, selected = false,
} = {}) {
  const model = noticeRowModel(notice, formatTime, removedGrants);
  const row = element(document, "article",
    `permission-request-card access-notice-row${model.unread ? " is-unread" : ""}`);
  row.setAttribute("data-notice-id", model.id);
  const badges = element(document, "span", "access-notice-badges");
  if (model.unread) badges.append(statusBadge(document, "New", "new"));
  if (model.accessRemoved) badges.append(statusBadge(document, ACCESS_REMOVED_LABEL, "removed"));
  let box = null;
  if (model.unread && typeof onSelect === "function") {
    box = element(document, "input", "access-notice-select");
    box.type = "checkbox";
    box.checked = Boolean(selected);
    box.setAttribute("aria-label", `Select notice: ${model.title}`);
    box.addEventListener("change", () => onSelect(box.checked));
  }
  const copy = cardHeading(document, row, model.title, badges.firstChild ? badges : null, box);
  if (model.summary) copy.append(element(document, "p", "access-notice-summary", model.summary));
  if (model.time) {
    const time = element(document, "time", "access-notice-time", model.time);
    time.setAttribute("datetime", model.timeValue);
    row.append(time);
  }
  const message = cardMessage(document);
  const group = element(document, "div", "permission-choice-row access-notice-actions");
  const action = (label, style, handler) => {
    const button = element(document, "button", `button ${style} permission-choice-button`, label);
    button.type = "button";
    button.addEventListener("click", () => {
      if (button.disabled || typeof handler !== "function") return;
      handler({ notice, button, row, group, message });
    });
    group.append(button);
  };
  if (model.canMarkRead) action(MARK_READ_LABEL, "secondary", onMarkRead);
  if (model.canRemoveAccess) action(REMOVE_ACCESS_LABEL, "danger", onRemoveAccess);
  if (group.firstChild) row.append(group);
  row.append(message);
  return row;
}

/**
 * Two-step confirmation on the same button (ui/DESIGN.md: memory Delete works
 * the same way). Returns true on the confirming click.
 */
export function confirmOnSecondClick(button, { idleLabel, confirmLabel, schedule, resetAfterMs = 5000 }) {
  if (button.getAttribute("data-confirming") === "true") {
    button.removeAttribute("data-confirming");
    return true;
  }
  button.setAttribute("data-confirming", "true");
  button.textContent = confirmLabel;
  schedule(() => {
    if (button.isConnected && button.getAttribute("data-confirming") === "true") {
      button.removeAttribute("data-confirming");
      button.textContent = idleLabel;
    }
  }, resetAfterMs);
  return false;
}

/**
 * What a decision sends: the chosen key, the note, and the card's boxes. An
 * access request has at most one Keep box (`keep`); a proposal sends the rows
 * it keeps (`kept_item_ids`). One "how long" applies to the card: the first
 * ticked row's choice. Returns `{ body, kept }` (`kept` is the Keep box of an
 * access request, for the outcome wording).
 */
export function decisionRequestBody({ review, choice, note, keepBoxes = [], handOnBoxes = [], keepSelects = [] }) {
  const body = { choice: choice.choice };
  const noteText = text(note && note.value);
  if (noteText) body.note = noteText;
  let kept;
  if (keepBoxes.length && text(review.kind) === "change") {
    body.kept_item_ids = keepBoxes.filter(box => box.checked).map(box => box.getAttribute("data-item-id"));
  } else if (keepBoxes.length) {
    kept = keepBoxes[0].checked;
    body.keep = kept;
  }
  const firstKept = keepBoxes.findIndex(box => box.checked);
  if (firstKept >= 0 && keepSelects[firstKept]) {
    const hours = KEEP_DURATIONS[Number(keepSelects[firstKept].value) || 0].hours;
    if (hours !== null) body.keep_hours = hours;
  }
  if (handOnBoxes.length) body.hand_on = handOnBoxes.some(box => box.checked);
  return { body, kept };
}

/** Where a decision is sent (the same API for the Inbox and the chat card). */
export function decisionPath(requestId) {
  return `${ACCESS_INBOX_API}/requests/${encodeURIComponent(text(requestId))}/decision`;
}

function emptyState(document, value) {
  return element(document, "p", "permission-empty-state", value);
}

function recordId(node, stop, attribute) {
  for (let current = node; current && current !== stop; current = current.parentNode) {
    const value = current.getAttribute && current.getAttribute(attribute);
    if (value) return value;
  }
  return "";
}

/**
 * The Inbox count for the whole page (the Settings cog and Inbox tab badges):
 * one small read (`INBOX_COUNTS_PATH`) kept current by the shared attention
 * count (settings_support.mjs `createAttentionCount`). `set()` takes counts an
 * Inbox read or change already returned, one field or both.
 */
export function createInboxCounts({ get, onChange, browser = globalThis, pollMs = INBOX_COUNTS_POLL_MS }) {
  return createAttentionCount({
    read: () => Promise.resolve(get(INBOX_COUNTS_PATH)).then(inboxCounts),
    onChange,
    empty: inboxCounts(null),
    merge: (current, next) => ({ ...current, ...next }),
    same: (a, b) => a.decidableRequests === b.decidableRequests && a.unreadNotices === b.unreadNotices,
    warning: "ua.access_inbox: the Inbox count could not be read; retrying.",
    browser,
    pollMs,
    soonMs: INBOX_COUNTS_SOON_MS,
  });
}

/**
 * Connects the access sections to the Inbox panel.
 *
 * `get(path)` and `post(path, body)` must resolve with the parsed JSON body and
 * reject with an Error whose message is shown to the person; the Settings
 * controls supply them with the signed-in session and the permission CSRF
 * header. `beginAction(button, busyLabel, group)` is the shared busy-button
 * helper and `paginate(options)` the shared page controls (pagination.js).
 * `onCounts(counts)` receives the Inbox counts after every read and change,
 * with only the parts that read knows (`decidableRequests`, `unreadNotices`).
 */
export function createAccessInbox({
  container, get, post, beginAction, failureMessage, onCounts, paginate,
  browser = globalThis, formatTime = localTime,
}) {
  const document = browser.document;
  const warn = (event, detail) => browser.console && browser.console.warn(event, detail);
  const schedule = (callback, delay) => browser.setTimeout(callback, delay);
  const state = {
    reviews: [], notices: [], unreadNotices: 0, loaded: false, version: 0, noticesVersion: 0, noticesBusyToken: 0,
    noticeView: NOTICE_VIEWS.UNREAD, noticeOffset: 0, noticeTotal: 0, noticeAllTotal: 0, selected: new Set(),
    outcomes: new Map(), removedGrants: new Set(), focusId: "", highlightId: "",
  };
  const reportCounts = counts => { if (typeof onCounts === "function") onCounts(counts); };

  const status = element(document, "p", "settings-status access-inbox-status-line");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.hidden = true;

  const requestsSection = element(document, "section", "access-inbox-section");
  const requestsTitle = element(document, "h4", "settings-subsection-title is-first", "Access requests");
  requestsTitle.id = uniqueId("access-inbox-requests-title");
  requestsSection.setAttribute("aria-labelledby", requestsTitle.id);
  const requestList = element(document, "div", "access-inbox-list");
  requestsSection.append(requestsTitle,
    element(document, "p", "settings-subsection-copy", "Agents ask here when they need access. Nothing is approved until someone chooses."),
    requestList);

  const noticesSection = element(document, "section", "access-inbox-section");
  const noticesHeading = element(document, "div", "access-inbox-section-heading");
  const noticesTitle = element(document, "h4", "settings-subsection-title", "Notices");
  noticesTitle.id = uniqueId("access-inbox-notices-title");
  noticesSection.setAttribute("aria-labelledby", noticesTitle.id);
  const unreadLabel = element(document, "span", "access-inbox-status is-new access-inbox-unread");
  unreadLabel.hidden = true;
  noticesHeading.append(noticesTitle, unreadLabel);

  // Unread / All switch: the same tab style as Memories' Personal / Team.
  const viewTabs = element(document, "nav", "permission-tabs access-notice-views");
  viewTabs.setAttribute("role", "tablist");
  viewTabs.setAttribute("aria-label", "Which notices to show");
  const viewButtons = [[NOTICE_VIEWS.UNREAD, "Unread"], [NOTICE_VIEWS.ALL, "All"]].map(([view, label]) => {
    const button = element(document, "button", "permission-tab", label);
    button.type = "button";
    button.setAttribute("role", "tab");
    button.setAttribute("data-view", view);
    button.addEventListener("click", () => {
      if (view !== state.noticeView) loadNotices({ view, offset: 0 });
    });
    viewTabs.append(button);
    return button;
  });

  const toolbar = element(document, "div", "access-notice-toolbar");
  const selectPageLabel = element(document, "label", "access-notice-select-page");
  const selectPage = element(document, "input", "access-notice-select");
  selectPage.type = "checkbox";
  selectPageLabel.append(selectPage, element(document, "span", "", SELECT_PAGE_LABEL));
  const markSelected = element(document, "button", "button secondary access-notice-mark-selected", MARK_SELECTED_LABEL);
  markSelected.type = "button";
  const markAll = element(document, "button", "button ghost access-notice-mark-all", MARK_ALL_LABEL);
  markAll.type = "button";
  toolbar.append(selectPageLabel, markSelected, markAll);
  const noticeMessage = cardMessage(document);
  const noticeList = element(document, "div", "access-inbox-list");
  const noticePager = element(document, "div", "access-notice-pager");
  noticesSection.append(noticesHeading,
    element(document, "p", "settings-subsection-copy", "Updates about access and work that needs attention. They do not need a decision."),
    viewTabs, toolbar, noticeMessage, noticeList, noticePager);

  container.replaceChildren(status, requestsSection, noticesSection);

  function setStatus(value, isError) {
    status.textContent = value || "";
    status.hidden = !value;
    status.classList.toggle("is-error", Boolean(value && isError));
  }

  function findRecord(list, attribute, id) {
    return Array.from(list.children).find(child => child.getAttribute(attribute) === id) || null;
  }

  function focusRecord(node) {
    node.tabIndex = -1;
    node.focus({ preventScroll: true });
  }

  // Re-rendering replaces the cards; keep keyboard focus on the same request.
  function focusedRequestId() {
    const active = document.activeElement;
    return active && container.contains(active) ? recordId(active, container, "data-request-id") : "";
  }

  function renderRequests() {
    const focusedId = focusedRequestId();
    const listed = new Set(state.reviews.map(review => text(review.request_id)));
    const cards = [];
    // Decisions saved in this page stay visible (as their outcome) until the
    // next Inbox open or Refresh, even though the server no longer lists them.
    state.outcomes.forEach((outcome, id) => {
      if (!listed.has(id)) cards.push(renderDecidedCard(document, outcome));
    });
    state.reviews.forEach(review => cards.push(
      renderReviewCard(document, review, { formatTime, onChoose: decide, warn })));
    requestList.replaceChildren(...(cards.length ? cards : [emptyState(document,
      "Nothing is waiting for your approval. Requests for other people's tasks that you cannot decide are not shown here.")]));
    if (state.highlightId) {
      const highlighted = findRecord(requestList, "data-request-id", state.highlightId);
      if (highlighted) highlighted.classList.add("is-highlighted");
    }
    const refocus = focusedId && findRecord(requestList, "data-request-id", focusedId);
    if (refocus) focusRecord(refocus);
  }

  /** The unread notices on this page (the ones a checkbox can select). */
  function unreadOnPage() {
    return state.notices.filter(notice => notice.read !== true).map(notice => text(notice.notice_id));
  }

  function noticeRow(notice) {
    const id = text(notice.notice_id);
    return renderNoticeRow(document, notice, {
      formatTime, removedGrants: state.removedGrants, onMarkRead: markRead, onRemoveAccess: removeAccess,
      selected: state.selected.has(id),
      onSelect: checked => {
        if (checked) state.selected.add(id);
        else state.selected.delete(id);
        renderToolbar();
      },
    });
  }

  function renderUnread() {
    unreadLabel.textContent = unreadNoticesText(state.unreadNotices);
    unreadLabel.hidden = !state.unreadNotices;
    renderToolbar();
  }

  function renderToolbar() {
    const selectable = unreadOnPage();
    const selected = selectable.filter(id => state.selected.has(id));
    selectPageLabel.hidden = !selectable.length;
    selectPage.checked = Boolean(selectable.length) && selected.length === selectable.length;
    markSelected.hidden = !selectable.length;
    if (markSelected.getAttribute("aria-busy") !== "true") {
      markSelected.disabled = !selected.length;
      markSelected.textContent = selected.length ? `${MARK_SELECTED_LABEL} (${selected.length})` : MARK_SELECTED_LABEL;
    }
    markAll.hidden = !state.unreadNotices;
    toolbar.hidden = selectPageLabel.hidden && markAll.hidden;
  }

  function renderNotices() {
    viewButtons.forEach(button => {
      const selected = button.getAttribute("data-view") === state.noticeView;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-selected", selected ? "true" : "false");
    });
    noticeList.replaceChildren(...(state.notices.length
      ? state.notices.map(noticeRow) : [emptyState(document, emptyNoticesText(state.noticeView, state.noticeAllTotal))]));
    noticePager.replaceChildren(...(state.noticeTotal > NOTICE_PAGE_SIZE && typeof paginate === "function"
      ? [paginate({ offset: state.noticeOffset, total: state.noticeTotal, pageSize: NOTICE_PAGE_SIZE,
        label: "Notice pages", onPage: offset => loadNotices({ offset }) })]
      : []));
    renderUnread();
  }

  /** Takes the notices part of an Inbox read. Selections on rows no longer shown are dropped. */
  function applyNotices(payload) {
    state.notices = Array.isArray(payload && payload.notices) ? payload.notices : [];
    state.noticeOffset = Number.isInteger(payload && payload.notices_offset) ? payload.notices_offset : state.noticeOffset;
    state.noticeTotal = Number(payload && payload.notices_total) || 0;
    state.noticeAllTotal = Number(payload && payload.notices_all_total) || 0;
    state.unreadNotices = unreadNoticeCount(payload);
    const shown = new Set(unreadOnPage());
    state.selected.forEach(id => { if (!shown.has(id)) state.selected.delete(id); });
    renderNotices();
  }

  function applyPendingFocus() {
    const id = state.focusId;
    state.focusId = "";
    if (!id) return;
    const card = findRecord(requestList, "data-request-id", id);
    if (!card) {
      setStatus("That access request is not waiting for you any more. It may already be decided, or only someone else can decide it.", false);
      return;
    }
    state.highlightId = id;
    card.classList.add("is-highlighted");
    browser.requestAnimationFrame(() => {
      if (!card.isConnected) return;
      card.scrollIntoView({ block: "center" });
      focusRecord(card);
    });
  }

  /** Reads the current reviews and notices. `keepOutcomes` keeps cards for decisions saved in this page. */
  function load({ keepOutcomes = false } = {}) {
    const version = ++state.version;
    state.noticesVersion += 1;
    if (!keepOutcomes) {
      state.outcomes.clear();
      state.highlightId = "";
    }
    return Promise.resolve().then(() => get(inboxPath(state.noticeView, state.noticeOffset))).then(payload => {
      if (version !== state.version) return;
      state.reviews = Array.isArray(payload && payload.reviews) ? payload.reviews : [];
      state.loaded = true;
      setStatus("", false);
      renderRequests();
      applyNotices(payload);
      applyPendingFocus();
      reportCounts({ decidableRequests: decidableReviewCount(state.reviews), unreadNotices: state.unreadNotices });
    }).catch(error => {
      if (version !== state.version) return;
      state.focusId = "";
      const reason = failureMessage(error, "Access requests could not be loaded. Refresh and try again.");
      setStatus(state.loaded ? `The list below may be out of date because it could not be refreshed. ${reason}` : reason, true);
    });
  }

  /**
   * Reads one page of notices in one view without redrawing the requests (a
   * note being typed on a request card stays). A page left empty by marking
   * notices read steps back one page.
   */
  function loadNotices({ view = state.noticeView, offset = state.noticeOffset } = {}) {
    const version = state.version;
    const noticesVersion = ++state.noticesVersion;
    const finish = noticeBusy();
    return Promise.resolve().then(() => get(inboxPath(view, offset))).then(payload => {
      if (version !== state.version || noticesVersion !== state.noticesVersion) return undefined;
      const notices = Array.isArray(payload && payload.notices) ? payload.notices : [];
      // The step back's own read keeps the switch disabled until it finishes.
      if (!notices.length && offset > 0) return loadNotices({ view, offset: Math.max(0, offset - NOTICE_PAGE_SIZE) });
      if (view !== state.noticeView) state.selected.clear();
      state.noticeView = view;
      applyNotices(payload);
      reportCounts({ unreadNotices: state.unreadNotices });
      return undefined;
    }).catch(error => {
      if (version !== state.version || noticesVersion !== state.noticesVersion) return;
      setCardMessage(noticeMessage, failureMessage(error, "Notices could not be loaded. Try again."), true);
    }).finally(finish);
  }

  /** While notices load, the switch is disabled so a second click waits for the first. */
  function noticeBusy() {
    const token = state.noticesBusyToken + 1;
    state.noticesBusyToken = token;
    noticeList.setAttribute("aria-busy", "true");
    viewButtons.forEach(button => { button.disabled = true; });
    // Called after this read and any read it started, so only the last one re-enables.
    return () => {
      if (state.noticesBusyToken !== token) return;
      noticeList.removeAttribute("aria-busy");
      viewButtons.forEach(button => { button.disabled = false; });
    };
  }

  function decide({ review, choice, button, note, card, group, message, keepBoxes = [], handOnBoxes = [], keepSelects = [] }) {
    const id = text(review.request_id);
    const { body, kept } = decisionRequestBody({ review, choice, note, keepBoxes, handOnBoxes, keepSelects });
    const finish = beginAction(button, choice.busyLabel, group);
    note.readOnly = true;
    setCardMessage(message, "", false);
    let saved = false;
    return Promise.resolve()
      .then(() => post(decisionPath(id), body))
      .then(payload => {
        saved = true;
        const outcome = { id, title: text(review.title) || "Access request",
          outcome: decisionOutcome(choice.choice, payload && payload.review, kept),
          links: followUpLinks(payload && payload.review) };
        state.outcomes.set(id, outcome);
        const decided = renderDecidedCard(document, outcome);
        card.replaceWith(decided);
        focusRecord(decided);
      })
      .catch(error => {
        setCardMessage(message, failureMessage(error, "Your decision was not saved. Refresh and try again."), true);
      })
      .finally(() => {
        finish();
        if (!saved) note.readOnly = false;
      })
      .then(() => (saved ? load({ keepOutcomes: true }) : undefined));
  }

  /** Marks notices read on the server (these ids, or all with null) and returns how many changed. */
  function postRead(ids) {
    return Promise.resolve().then(() => post(NOTICES_READ_PATH, ids === null ? { all: true } : { notice_ids: ids }))
      .then(payload => {
        state.unreadNotices = unreadNoticeCount(payload);
        if (ids === null) state.selected.clear();
        else ids.forEach(id => state.selected.delete(id));
        reportCounts({ unreadNotices: state.unreadNotices });
        return Number(payload && payload.marked) || 0;
      });
  }

  /** After marking: re-read the page (read notices leave the Unread view) and keep focus in the section. */
  function afterMarked(messageText) {
    const hadFocus = container.contains(document.activeElement);
    return loadNotices().then(() => {
      setCardMessage(noticeMessage, messageText, false);
      const active = document.activeElement;
      if (hadFocus && (!active || !container.contains(active) || active.hidden || !active.isConnected)) {
        focusRecord(noticesTitle);
      }
    });
  }

  function markRead({ notice, button, group, message }) {
    const finish = beginAction(button, "Marking as read…", group);
    setCardMessage(message, "", false);
    setCardMessage(noticeMessage, "", false);
    return postRead([text(notice.notice_id)])
      .then(() => {
        finish();
        return afterMarked("");
      })
      .catch(error => {
        finish();
        setCardMessage(message, failureMessage(error, "The notice was not marked as read. Try again."), true);
      });
  }

  function markSelectedRead() {
    const ids = unreadOnPage().filter(id => state.selected.has(id));
    if (!ids.length) return Promise.resolve();
    const finish = beginAction(markSelected, "Marking as read…", toolbar);
    setCardMessage(noticeMessage, "", false);
    return postRead(ids)
      .then(marked => {
        finish();
        return afterMarked(markedText(marked));
      })
      .catch(error => {
        finish();
        renderToolbar();
        setCardMessage(noticeMessage, failureMessage(error, "The notices were not marked as read. Try again."), true);
      });
  }

  function markAllRead() {
    if (!confirmOnSecondClick(markAll, { idleLabel: MARK_ALL_LABEL, confirmLabel: CONFIRM_MARK_ALL_LABEL, schedule })) {
      setCardMessage(noticeMessage, markAllPrompt(state.unreadNotices), false);
      return Promise.resolve();
    }
    const finish = beginAction(markAll, "Marking all as read…", toolbar);
    setCardMessage(noticeMessage, "", false);
    return postRead(null)
      .then(marked => {
        finish();
        markAll.textContent = MARK_ALL_LABEL;
        return afterMarked(markedText(marked));
      })
      .catch(error => {
        finish();
        markAll.textContent = MARK_ALL_LABEL;
        renderToolbar();
        setCardMessage(noticeMessage, failureMessage(error, "The notices were not marked as read. Try again."), true);
      });
  }

  selectPage.addEventListener("change", () => {
    unreadOnPage().forEach(id => (selectPage.checked ? state.selected.add(id) : state.selected.delete(id)));
    renderNotices();
  });
  markSelected.addEventListener("click", () => { if (!markSelected.disabled) markSelectedRead(); });
  markAll.addEventListener("click", () => { if (!markAll.disabled) markAllRead(); });

  function replaceNoticeRow(row, notice, messageText, isError) {
    const next = noticeRow(notice);
    row.replaceWith(next);
    // renderNoticeRow always ends the row with its message line.
    if (messageText) setCardMessage(next.lastChild, messageText, isError);
    focusRecord(next);
  }

  function removeAccess({ notice, button, row, group, message }) {
    if (!confirmOnSecondClick(button, { idleLabel: REMOVE_ACCESS_LABEL, confirmLabel: CONFIRM_REMOVAL_LABEL, schedule })) {
      setCardMessage(message, "Select Confirm removal to remove this access.", false);
      return Promise.resolve();
    }
    const grantId = text(notice.grant_id);
    const finish = beginAction(button, "Removing…", group);
    setCardMessage(message, "", false);
    return Promise.resolve()
      .then(() => post(`${ACCESS_INBOX_API}/grants/${encodeURIComponent(grantId)}/revoke`, {}))
      .then(payload => {
        finish();
        state.removedGrants.add(grantId);
        (Array.isArray(payload && payload.revoked_grant_ids) ? payload.revoked_grant_ids : [])
          .forEach(id => state.removedGrants.add(text(id)));
        if (notice.read === true) {
          replaceNoticeRow(row, notice, "Access removed.", false);
          return undefined;
        }
        // The row stays in view (even under Unread) so its outcome can be read.
        return postRead([text(notice.notice_id)]).then(() => {
          notice.read = true;
          renderUnread();
          replaceNoticeRow(row, notice, "Access removed.", false);
        }, error => {
          replaceNoticeRow(row, notice, `Access removed. The notice could not be marked as read: ${
            failureMessage(error, "try again.")}`, true);
        });
      }, error => {
        finish();
        button.textContent = REMOVE_ACCESS_LABEL;
        setCardMessage(message, failureMessage(error, "The access was not removed. Refresh and try again."), true);
      });
  }

  renderNotices();

  return {
    load,
    /** Highlight and focus this request after the next successful load (Inbox links). */
    focusRequest(id) { state.focusId = text(id); },
    setVisible(visible) { container.hidden = !visible; },
    /** Sign-out: forget everything and ignore replies to earlier reads. */
    clear() {
      state.version += 1;
      state.noticesVersion += 1;
      Object.assign(state, { reviews: [], notices: [], unreadNotices: 0, loaded: false,
        noticeView: NOTICE_VIEWS.UNREAD, noticeOffset: 0, noticeTotal: 0, noticeAllTotal: 0,
        focusId: "", highlightId: "" });
      state.outcomes.clear();
      state.removedGrants.clear();
      state.selected.clear();
      setStatus("", false);
      setCardMessage(noticeMessage, "", false);
      requestList.replaceChildren();
      renderNotices();
      noticeList.replaceChildren();
    },
  };
}

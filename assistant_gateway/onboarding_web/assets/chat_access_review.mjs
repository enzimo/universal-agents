/**
 * Access review cards in the Web Chat transcript (ui/DESIGN.md, "Access Review
 * Cards in Web Chat").
 *
 * When an agent's request waits for a decision, the gateway announces it in
 * the chat as a System message of type `access_review` that carries the
 * request id (`access_request_id`). This module turns that message into the
 * Inbox's own card in its compact form: the one-line summary, warning chips,
 * the decision buttons, and everything else folded under **Details**.
 *
 * - The card reads the request from the gateway
 *   (`GET /api/web-chat/access-inbox/requests/<id>`), so it shows the stored
 *   decision after a reload or a decision made on another device. The gateway
 *   decides what this person may see and whether they may decide.
 * - A decision goes through the Inbox decide API with the signed-in session
 *   and CSRF header; the gateway checks the person's authority as it does for
 *   the Inbox. The browser sends only the chosen key, the Keep boxes and the
 *   note (`decisionRequestBody`, shared with the Inbox).
 * - The transcript re-renders often, so reads are cached per request: a
 *   decided request is never read again, and a waiting one is read again at
 *   most every `REFRESH_WAITING_AFTER_MS` when its message is drawn.
 *
 * All server and agent text is rendered as text, never as HTML.
 */

import {
  ACCESS_INBOX_API, ACCESS_REVIEW_MESSAGE_TYPE, decisionOutcome, decisionPath, decisionRequestBody, followUpLinks, renderDecidedCard,
  renderReviewCard, setCardMessage, storedDecisionOutcome,
} from "./access_inbox.mjs";

export const REFRESH_WAITING_AFTER_MS = 15000;
export const LOADING_TEXT = "Loading this request…";
export const OPEN_INBOX_LABEL = "Open Inbox";
const READ_FAILED = "This request could not be loaded. Open the Inbox to review it.";
const DECISION_FAILED = "Your decision was not saved. Try again, or open the Inbox.";

function text(value) {
  return value === undefined || value === null ? "" : String(value).trim();
}

function element(document, tagName, className, value) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

/** Whether a normalized transcript message is an access review announcement with its request id. */
export function isAccessReviewMessage(message) {
  return Boolean(message) && message.role === "system"
    && text(message.messageType) === ACCESS_REVIEW_MESSAGE_TYPE && Boolean(text(message.accessRequestId));
}

export function reviewPath(requestId) {
  return `${ACCESS_INBOX_API}/requests/${encodeURIComponent(text(requestId))}`;
}

/**
 * Creates the chat's access review cards.
 *
 * `get(path)` and `post(path, body)` resolve with the parsed JSON body and
 * reject with an Error whose message is shown to the person (the Settings
 * client). `beginAction(button, busyLabel, group)` is the shared busy-button
 * helper. `openInbox(requestId)` opens the Inbox at the request.
 * `onDecided(requestId)` runs after a saved decision.
 */
export function createChatAccessReviews({
  get, post, beginAction, failureMessage, openInbox, onDecided,
  browser = globalThis, formatTime, now = () => Date.now(),
}) {
  const document = browser.document;
  const warn = (event, detail) => browser.console && browser.console.warn(event, detail);
  // requestId -> { review, outcome, error, fetchedAt, pending }
  const entries = new Map();
  let generation = 0;

  function entryFor(id) {
    if (!entries.has(id)) entries.set(id, { review: null, outcome: null, error: "", fetchedAt: 0, pending: null });
    return entries.get(id);
  }

  function isDecided(entry) {
    return Boolean(entry.outcome) || Boolean(entry.review && text(entry.review.status) !== "waiting");
  }

  function needsRead(entry) {
    if (entry.pending) return false;
    if (!entry.review && !entry.error) return true;
    return !isDecided(entry) && now() - entry.fetchedAt >= REFRESH_WAITING_AFTER_MS;
  }

  /** Reads the request once at a time; replies from before a sign-out are ignored. */
  function read(id) {
    const entry = entryFor(id);
    if (entry.pending) return entry.pending;
    const readGeneration = generation;
    entry.pending = Promise.resolve()
      .then(() => get(reviewPath(id)))
      .then(payload => {
        if (readGeneration !== generation) return;
        entry.review = (payload && payload.review) || null;
        entry.error = entry.review ? "" : READ_FAILED;
      }, error => {
        if (readGeneration !== generation) return;
        entry.error = failureMessage(error, READ_FAILED);
        warn("chat_access_review_read_failed", { request_id: id, status: error && error.status });
      })
      .finally(() => {
        entry.fetchedAt = now();
        entry.pending = null;
      });
    return entry.pending;
  }

  function summaryOf(entry, id) {
    return text(entry.review && (entry.review.summary || entry.review.title)) || `Request ${id}`;
  }

  function statusCard(id, value, { isError = false, withInboxButton = false } = {}) {
    const card = element(document, "article", "permission-request-card access-review-card is-compact is-status");
    card.setAttribute("data-request-id", id);
    const line = element(document, "p", `access-inbox-card-message${isError ? " is-error" : ""}`, value);
    line.setAttribute("role", "status");
    card.append(line);
    if (withInboxButton && typeof openInbox === "function") {
      const button = element(document, "button", "button secondary permission-choice-button", OPEN_INBOX_LABEL);
      button.type = "button";
      button.addEventListener("click", () => openInbox(id));
      card.append(button);
    }
    return card;
  }

  function cardFor(id) {
    const entry = entryFor(id);
    if (entry.outcome) return renderDecidedCard(document, entry.outcome);
    if (!entry.review) {
      return entry.error ? statusCard(id, entry.error, { isError: true, withInboxButton: true }) : statusCard(id, LOADING_TEXT);
    }
    const stored = storedDecisionOutcome(entry.review, formatTime);
    if (stored) return renderDecidedCard(document, { id, title: summaryOf(entry, id), outcome: stored });
    return renderReviewCard(document, entry.review, {
      compact: true, formatTime, warn, onChoose: chosen => decide(id, chosen),
    });
  }

  function draw(slot, id) {
    slot.replaceChildren(cardFor(id));
  }

  function decide(id, { review, choice, button, note, card, group, message, keepBoxes, handOnBoxes, keepSelects }) {
    const entry = entryFor(id);
    const { body, kept } = decisionRequestBody({ review, choice, note, keepBoxes, handOnBoxes, keepSelects });
    const finish = beginAction(button, choice.busyLabel, group);
    note.readOnly = true;
    setCardMessage(message, "", false);
    return Promise.resolve()
      .then(() => post(decisionPath(id), body))
      .then(payload => {
        finish();
        const saved = (payload && payload.review) || review;
        entry.review = saved;
        entry.fetchedAt = now();
        entry.outcome = { id, title: summaryOf(entry, id), outcome: decisionOutcome(choice.choice, saved, kept),
          links: followUpLinks(saved) };
        if (card.isConnected) card.replaceWith(renderDecidedCard(document, entry.outcome));
        if (typeof onDecided === "function") onDecided(id);
      }, error => {
        finish();
        note.readOnly = false;
        setCardMessage(message, failureMessage(error, DECISION_FAILED), true);
        // Someone may have decided it elsewhere first: read it again, and
        // show the stored decision instead of buttons that can only fail.
        return read(id).then(() => {
          const stored = entry.review && storedDecisionOutcome(entry.review, formatTime);
          if (stored && card.isConnected) {
            card.replaceWith(renderDecidedCard(document, { id, title: summaryOf(entry, id), outcome: stored }));
          }
        });
      });
  }

  return {
    /**
     * Adds the card for an access review message to its bubble. The message
     * text stays in the transcript (Copy raw message) but is hidden by the
     * caller while the card is shown.
     */
    mount(bubble, message) {
      const id = text(message && message.accessRequestId);
      if (!id) return null;
      const slot = element(document, "div", "chat-access-review");
      slot.setAttribute("data-access-request-id", id);
      bubble.append(slot);
      draw(slot, id);
      const entry = entryFor(id);
      if (needsRead(entry) || entry.pending) {
        read(id).then(() => { if (slot.isConnected) draw(slot, id); });
      }
      return slot;
    },
    /** Sign-out: forget every request and ignore replies to earlier reads. */
    clear() {
      generation += 1;
      entries.clear();
    },
  };
}

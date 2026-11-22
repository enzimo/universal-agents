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
  ACCESS_INBOX_API, ACCESS_REVIEW_MESSAGE_TYPE, FOLLOW_UP_AFTER_REVIEW, FOLLOW_UP_PAGES_BELOW, activeFollowUpLinks,
  closeEndedFollowUpLinks, createFollowUpWatcher, decisionOutcome, decisionPath, decisionRequestBody,
  followUpCardParts, followUpOutcomes, followUpSignature, followUpWatchTargets, handOffFollowUpLinks,
  hasPendingFollowUp, isFollowUpSettling, renderDecidedCard, renderReviewCard, setCardMessage, storedDecisionOutcome,
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

function findReviewSlots(document, id) {
  const root = document && document.body;
  if (!root) return [];
  return Array.from(root.querySelectorAll(".chat-access-review"))
    .filter(slot => slot.isConnected && slot.getAttribute("data-access-request-id") === id);
}

/**
 * Creates the chat's access review cards.
 *
 * `get(path)` and `post(path, body)` resolve with the parsed JSON body and
 * reject with an Error whose message is shown to the person (the Settings
 * client). `beginAction(button, busyLabel, group)` is the shared busy-button
 * helper. `openInbox(requestId)` opens the Inbox at the request.
 * `onDecided(requestId)` runs after a saved decision.
 * `onFollowUpPages(requestId, links, placement)` hands an approved proposal's
 * credential forms to the conversation as broker cards and returns the links
 * it took (``access_inbox.handOffFollowUpLinks``).
 * `onCloseFollowUpPages(requestId, urls)` closes any inline broker cards for
 * previously handed-off links that are no longer pending on the server.
 */
export function createChatAccessReviews({
  get, post, beginAction, failureMessage, openInbox, onDecided, onFollowUpPages, onCloseFollowUpPages,
  browser = globalThis, formatTime, now = () => Date.now(),
}) {
  const document = browser.document;
  const warn = (event, detail) => browser.console && browser.console.warn(event, detail);
  // requestId -> { review, localOutcome, error, fetchedAt, pending, followUpStop, lastFollowUpSignature, handedOffUrls }
  const entries = new Map();
  let generation = 0;
  let syncQueued = false;

  // Coalesces post-mount watcher sync into one microtask so chat.js
  // renderMessages finishes attaching detached bubbles before we check them.
  function queueSync() {
    if (syncQueued) return;
    syncQueued = true;
    const queuedGeneration = generation;
    Promise.resolve().then(() => {
      if (queuedGeneration !== generation) return;
      syncQueued = false;
      followUpWatcher.sync();
    });
  }

  function entryFor(id) {
    if (!entries.has(id)) {
      entries.set(id, {
        review: null, localOutcome: null, error: "", fetchedAt: 0, pending: null,
        followUpStop: false, lastFollowUpSignature: "", handedOffUrls: new Set(),
      });
    }
    return entries.get(id);
  }

  function isDecided(entry) {
    return Boolean(entry.localOutcome) || Boolean(entry.review && text(entry.review.status) !== "waiting");
  }

  function hasPendingFollowUps(entry) {
    return Boolean(!entry.followUpStop && entry.review && hasPendingFollowUp(entry.review));
  }

  function needsRead(entry) {
    if (entry.pending) return false;
    if (!entry.review && !entry.error) return true;
    return !isDecided(entry) && now() - entry.fetchedAt >= REFRESH_WAITING_AFTER_MS;
  }

  /** Reads the request once at a time; replies from before a sign-out are ignored. */
  function read(id, { refreshFollowUp = false } = {}) {
    const entry = entryFor(id);
    if (entry.pending) return entry.pending;
    const readGeneration = generation;
    entry.pending = Promise.resolve()
      .then(() => get(reviewPath(id)))
      .then(payload => {
        if (readGeneration !== generation) return;
        const nextReview = (payload && payload.review) || null;
        if (refreshFollowUp) {
          if (!nextReview) return;
          entry.review = nextReview;
          const handedOff = isDecided(entry) && handOffFollowUpLinks(
            id, activeFollowUpLinks(entry.review, now()), onFollowUpPages, FOLLOW_UP_AFTER_REVIEW, entry.handedOffUrls,
          );
          if (isDecided(entry)) {
            closeEndedFollowUpLinks(id, entry.review, onCloseFollowUpPages, entry.handedOffUrls);
          }
          if (handedOff || followUpSignature(entry.review, now()) !== entry.lastFollowUpSignature) drawAll(id);
          return;
        }
        entry.review = nextReview;
        entry.error = entry.review ? "" : READ_FAILED;
        if (isDecided(entry) && entry.review) {
          handOffFollowUpLinks(
            id, activeFollowUpLinks(entry.review, now()), onFollowUpPages, FOLLOW_UP_AFTER_REVIEW, entry.handedOffUrls,
          );
          closeEndedFollowUpLinks(id, entry.review, onCloseFollowUpPages, entry.handedOffUrls);
        }
      }, error => {
        if (readGeneration !== generation) return;
        if (refreshFollowUp) {
          if (error && (error.status === 403 || error.status === 404)) entry.followUpStop = true;
          warn("chat_access_review_follow_up_read_failed", { request_id: id, status: error && error.status });
          return;
        }
        entry.error = failureMessage(error, READ_FAILED);
        warn("chat_access_review_read_failed", { request_id: id, status: error && error.status });
      })
      .finally(() => {
        if (readGeneration !== generation) return;
        entry.fetchedAt = now();
        entry.pending = null;
        followUpWatcher.sync();
      });
    return entry.pending;
  }

  function summaryOf(entry, id) {
    return text(entry.review && (entry.review.summary || entry.review.title)) || `Request ${id}`;
  }

  /** Single source of truth for decided-card rendering across local and stored decisions. */
  function decidedCardProps(entry, id) {
    const outcome = entry.localOutcome || (entry.review && storedDecisionOutcome(entry.review, formatTime));
    if (!outcome) return null;
    const { links, pages } = followUpCardParts(entry.review, entry.handedOffUrls, now(), {
      note: FOLLOW_UP_PAGES_BELOW,
    });
    return {
      id,
      title: summaryOf(entry, id),
      outcome,
      links,
      pages,
      outcomes: followUpOutcomes(entry.review),
      settling: isFollowUpSettling(entry.review),
      compact: true,
    };
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
    const decided = decidedCardProps(entry, id);
    if (decided) {
      entry.lastFollowUpSignature = followUpSignature(entry.review, now());
      return renderDecidedCard(document, decided);
    }
    if (!entry.review) {
      return entry.error ? statusCard(id, entry.error, { isError: true, withInboxButton: true }) : statusCard(id, LOADING_TEXT);
    }
    return renderReviewCard(document, entry.review, {
      compact: true, formatTime, warn, onChoose: chosen => decide(id, chosen),
    });
  }

  function draw(slot, id) {
    slot.replaceChildren(cardFor(id));
  }

  function drawAll(id) {
    findReviewSlots(document, id).forEach(slot => draw(slot, id));
  }

  function refreshFollowUps() {
    const reads = [];
    entries.forEach((entry, id) => {
      if (!hasPendingFollowUps(entry) || !findReviewSlots(document, id).length) return;
      reads.push(read(id, { refreshFollowUp: true }));
    });
    return Promise.all(reads);
  }

  const followUpWatcher = createFollowUpWatcher({
    getPendingLinks() {
      const links = [];
      entries.forEach((entry, id) => {
        if (hasPendingFollowUps(entry) && findReviewSlots(document, id).length) {
          links.push(...followUpWatchTargets(entry.review, id));
        }
      });
      return links;
    },
    onRefresh: refreshFollowUps,
    onExpire() {
      entries.forEach((entry, id) => {
        if (!hasPendingFollowUps(entry)) return;
        const next = followUpSignature(entry.review, now());
        if (next !== entry.lastFollowUpSignature) drawAll(id);
      });
    },
    browser,
    now,
  });

  function decide(id, { review, choice, button, note, card, group, message, keepBoxes, handOnBoxes, keepSelects }) {
    const entry = entryFor(id);
    const decideGeneration = generation;
    const { body, kept } = decisionRequestBody({ review, choice, note, keepBoxes, handOnBoxes, keepSelects });
    const finish = beginAction(button, choice.busyLabel, group);
    note.readOnly = true;
    setCardMessage(message, "", false);
    return Promise.resolve()
      .then(() => post(decisionPath(id), body))
      .then(payload => {
        finish();
        if (decideGeneration !== generation) return;
        const saved = (payload && payload.review) || review;
        entry.review = saved;
        entry.fetchedAt = now();
        entry.followUpStop = false;
        entry.localOutcome = decisionOutcome(choice.choice, saved, kept);
        handOffFollowUpLinks(
          id, activeFollowUpLinks(saved, now()), onFollowUpPages, FOLLOW_UP_AFTER_REVIEW, entry.handedOffUrls,
        );
        if (card.isConnected) card.replaceWith(cardFor(id));
        followUpWatcher.sync();
        if (typeof onDecided === "function") onDecided(id);
      }, error => {
        finish();
        if (decideGeneration !== generation) return;
        note.readOnly = false;
        setCardMessage(message, failureMessage(error, DECISION_FAILED), true);
        // Someone may have decided it elsewhere first: read it again, and
        // show the stored decision instead of buttons that can only fail.
        return read(id).then(() => {
          if (decideGeneration !== generation) return;
          if (decidedCardProps(entry, id) && card.isConnected) card.replaceWith(cardFor(id));
          followUpWatcher.sync();
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
      } else {
        queueSync();
      }
      return slot;
    },
    /** Re-reads decided cards that still have open proposal follow-up links. */
    refreshFollowUps,
    /** Sign-out: forget every request and ignore replies to earlier reads. */
    clear() {
      generation += 1;
      syncQueued = false;
      entries.clear();
      followUpWatcher.stop();
    },
  };
}

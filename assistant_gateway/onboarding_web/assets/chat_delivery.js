(function (root) {
  "use strict";

  var DELIVERY_TIMEOUT_MS = 15000;
  var SENSITIVE_COMMAND_RESULT_KIND = "sensitive_command_result";
  // The gateway posts "link_result" when a page opened with /link ends
  // (assistant_gateway.domain.message_types.LINK_RESULT_MESSAGE_TYPE).
  var LINK_RESULT_MESSAGE_TYPE = "link_result";
  // Card kinds that a later message finishes, and that message's type. A form
  // or setup page an agent task waits on ("secure_credential_collection",
  // "tool_setup") disappears once the task answers ("task"), or earlier when
  // the gateway posts how that page ended (FINISHING_PAGE_MESSAGE_TYPES); a /link card
  // ("account_link", or "broker_authentication" for a sign-in) and an /auth
  // team sign-in app card disappear once the gateway posts how the page ended.
  // Other /auth cards (password managers) share the "broker_authentication"
  // kind but get no result, so they stay until dismissed.
  var FINISHING_MESSAGE_TYPES = Object.freeze({
    secure_credential_collection: "task",
    tool_setup: "task",
    account_link: LINK_RESULT_MESSAGE_TYPE,
    broker_authentication: LINK_RESULT_MESSAGE_TYPE,
  });
  // How one setup page ended, posted under the page's own id (its setup
  // request id) by assistant_gateway.application.tool_setup; it closes only
  // that page's card. A task's pages share one chat request, and a page
  // offered again gets a new id, so the task's answer ("task") still closes
  // them all, and nothing else closes a card of another page.
  var TOOL_SETUP_RESULT_MESSAGE_TYPE = "tool_setup_result";
  // How a secure credential form ended, posted by the gateway under the form's
  // own id (its credential request id) as soon as the broker reports it
  // (assistant_gateway.domain.message_types.SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE),
  // so its countdown stops while the agent works on.
  var SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE = "secure_credential_result";
  var FINISHING_PAGE_MESSAGE_TYPES = Object.freeze({
    tool_setup: TOOL_SETUP_RESULT_MESSAGE_TYPE,
    secure_credential_collection: SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE,
  });
  var TRANSIENT_ITEM_KINDS = Object.freeze(
    [SENSITIVE_COMMAND_RESULT_KIND].concat(Object.keys(FINISHING_MESSAGE_TYPES))
  );

  function nonNegativeInteger(value) {
    var normalized = Number(value || 0);
    return Number.isInteger(normalized) && normalized >= 0 ? normalized : 0;
  }

  function messagePositionSequence(message) {
    if (message && message.optimistic) {
      return nonNegativeInteger(message.anchorSessionSequence);
    }
    return nonNegativeInteger(message && message.sessionSequence);
  }

  function latestDurableSessionSequence(messages) {
    return (Array.isArray(messages) ? messages : []).reduce(function (latest, message) {
      if (!message || message.optimistic) {
        return latest;
      }
      return Math.max(latest, nonNegativeInteger(message.sessionSequence));
    }, 0);
  }

  function createErrorNotice(source, message) {
    return {
      source: String(source || ""),
      message: String(message || "An unexpected Web Chat error occurred."),
    };
  }

  function clearErrorNotice(notice, recoveredSource) {
    // One banner slot can receive overlapping failures. Only the operation
    // that owns the visible notice may dismiss it after recovery.
    if (!notice || notice.source !== String(recoveredSource || "")) {
      return notice || null;
    }
    return null;
  }

  function messageSort(left, right) {
    var positionDifference = messagePositionSequence(left) - messagePositionSequence(right);
    if (positionDifference) {
      return positionDifference;
    }
    var leftIsOptimistic = Boolean(left && left.optimistic);
    var rightIsOptimistic = Boolean(right && right.optimistic);
    if (leftIsOptimistic !== rightIsOptimistic) {
      return leftIsOptimistic ? 1 : -1;
    }
    if (leftIsOptimistic) {
      var localDifference = nonNegativeInteger(left.localSequence)
        - nonNegativeInteger(right.localSequence);
      if (localDifference) {
        return localDifference;
      }
    }
    return String(left && left.messageId || "").localeCompare(
      String(right && right.messageId || "")
    );
  }

  function reconcileMessages(currentMessages, incomingMessages) {
    var acknowledgedRequestSequences = Object.create(null);
    (Array.isArray(incomingMessages) ? incomingMessages : []).forEach(function (message) {
      var requestId = String(message && message.clientRequestId || "");
      if (requestId && !message.optimistic) {
        acknowledgedRequestSequences[requestId] = Math.max(
          nonNegativeInteger(acknowledgedRequestSequences[requestId]),
          nonNegativeInteger(message.sessionSequence)
        );
      }
    });

    var byMessageId = Object.create(null);
    var acknowledgedMessages = [];
    (Array.isArray(currentMessages) ? currentMessages : []).forEach(function (message) {
      var requestId = String(message && message.clientRequestId || "");
      if (message.optimistic && requestId && acknowledgedRequestSequences[requestId]) {
        acknowledgedMessages.push(message);
        return;
      }
      if (message && message.messageId) {
        byMessageId[message.messageId] = message;
      }
    });
    (Array.isArray(incomingMessages) ? incomingMessages : []).forEach(function (message) {
      if (message && message.messageId) {
        byMessageId[message.messageId] = message;
      }
    });

    var messages = Object.keys(byMessageId).map(function (messageId) {
      return byMessageId[messageId];
    });
    messages.forEach(function (message) {
      if (!message.optimistic) {
        return;
      }
      acknowledgedMessages.forEach(function (acknowledgedMessage) {
        if (
          nonNegativeInteger(acknowledgedMessage.localSequence)
          >= nonNegativeInteger(message.localSequence)
        ) {
          return;
        }
        var acknowledgedSequence = nonNegativeInteger(
          acknowledgedRequestSequences[acknowledgedMessage.clientRequestId]
        );
        message.anchorSessionSequence = Math.max(
          nonNegativeInteger(message.anchorSessionSequence),
          acknowledgedSequence
        );
      });
    });

    return {
      messages: messages.sort(messageSort),
      acknowledgedMessages: acknowledgedMessages,
    };
  }

  function finishingMessageType(kind) {
    return Object.prototype.hasOwnProperty.call(FINISHING_MESSAGE_TYPES, kind)
      ? FINISHING_MESSAGE_TYPES[kind]
      : "";
  }

  function normalizeTransientItem(value, origin) {
    if (!value || typeof value !== "object") {
      return null;
    }
    var itemId = String(value.item_id || "").trim();
    var sessionSequence = Number(value.session_sequence || 0);
    var kind = String(value.kind || "").trim();
    if (
      !itemId
      || !Number.isInteger(sessionSequence)
      || sessionSequence < 1
      || TRANSIENT_ITEM_KINDS.indexOf(kind) === -1
      || itemId.indexOf(kind + ":") !== 0
    ) {
      return null;
    }
    var item = {
      itemId: itemId,
      kind: kind,
      content: String(value.content || ""),
      sessionSequence: sessionSequence,
      createdAt: String(value.created_at_utc || ""),
    };
    if (kind === SENSITIVE_COMMAND_RESULT_KIND) {
      return item;
    }
    var action = value.action && typeof value.action === "object" ? value.action : {};
    var rawUrl = String(action.url || "").trim();
    var label = String(action.label || "Open secure form").trim();
    var correlationId = String(value.correlation_id || "").trim();
    if (
      !rawUrl
      || !label
      || !correlationId
      || itemId !== kind + ":" + String(value.request_id || correlationId)
    ) {
      return null;
    }
    try {
      var parsedUrl = new URL(rawUrl, String(origin || "http://localhost"));
      if (["http:", "https:"].indexOf(parsedUrl.protocol) === -1) {
        return null;
      }
      item.label = label;
      item.url = parsedUrl.href;
      item.expiresAtUtc = String(action.expires_at_utc || "");
      item.correlationId = correlationId;
      var pageId = String(value.request_id || "").trim();
      if (pageId && FINISHING_PAGE_MESSAGE_TYPES[kind]) {
        item.pageId = pageId;
      }
      return item;
    } catch (error) {
      return null;
    }
  }

  function reconcileTransientItems(currentItems, messages) {
    var durableMessages = Array.isArray(messages) ? messages : [];
    return (Array.isArray(currentItems) ? currentItems : []).filter(function (item) {
      var itemId = String(item && item.itemId || "");
      var itemKind = String(item && item.kind || "");
      var positionSequence = Number(item && item.sessionSequence || 0);
      if (
        !itemId
        || !positionSequence
        || TRANSIENT_ITEM_KINDS.indexOf(itemKind) === -1
      ) {
        return false;
      }
      var finishingType = finishingMessageType(itemKind);
      if (!finishingType) {
        return true;
      }
      var correlationId = String(item && item.correlationId || "");
      if (!correlationId) {
        return false;
      }
      var pageId = String(item && item.pageId || "");
      var pageFinishingType = FINISHING_PAGE_MESSAGE_TYPES[itemKind] || "";
      return !durableMessages.some(function (message) {
        if (message.optimistic || Number(message.sessionSequence || 0) <= positionSequence) {
          return false;
        }
        var messageCorrelation = String(message.correlationId || "");
        var messageType = String(message.messageType || "");
        return (messageCorrelation === correlationId && messageType === finishingType)
          || (Boolean(pageId) && messageCorrelation === pageId && messageType === pageFinishingType);
      });
    });
  }

  function upsertTransientItem(currentItems, nextItem) {
    var itemId = String(nextItem && nextItem.itemId || "");
    if (!itemId) {
      return Array.isArray(currentItems) ? currentItems.slice() : [];
    }
    var items = (Array.isArray(currentItems) ? currentItems : []).filter(
      function (item) {
        return String(item && item.itemId || "") !== itemId;
      }
    );
    items.push(nextItem);
    return items;
  }

  function timelineSort(left, right) {
    var leftValue = left && left.value || {};
    var rightValue = right && right.value || {};
    var leftOptimistic = left.kind === "message" && Boolean(leftValue.optimistic);
    var rightOptimistic = right.kind === "message" && Boolean(rightValue.optimistic);
    var leftPosition = left.kind === "transient_item"
      ? nonNegativeInteger(leftValue.sessionSequence)
      : messagePositionSequence(leftValue);
    var rightPosition = right.kind === "transient_item"
      ? nonNegativeInteger(rightValue.sessionSequence)
      : messagePositionSequence(rightValue);
    var sequenceDifference = leftPosition - rightPosition;
    if (sequenceDifference) {
      return sequenceDifference;
    }
    var leftSubsequence = leftOptimistic
      ? 2
      : left.kind === "transient_item" ? 1 : 0;
    var rightSubsequence = rightOptimistic
      ? 2
      : right.kind === "transient_item" ? 1 : 0;
    if (leftSubsequence !== rightSubsequence) {
      return leftSubsequence - rightSubsequence;
    }
    if (leftOptimistic && rightOptimistic) {
      var localDifference = nonNegativeInteger(leftValue.localSequence)
        - nonNegativeInteger(rightValue.localSequence);
      if (localDifference) {
        return localDifference;
      }
    }
    return String(leftValue.itemId || leftValue.messageId || "").localeCompare(
      String(rightValue.itemId || rightValue.messageId || "")
    );
  }

  function buildTimeline(messages, transientItems) {
    var timeline = [];
    (Array.isArray(messages) ? messages : []).forEach(function (message) {
      timeline.push({ kind: "message", value: message });
    });
    (Array.isArray(transientItems) ? transientItems : []).forEach(function (item) {
      timeline.push({ kind: "transient_item", value: item });
    });
    return timeline.sort(timelineSort);
  }

  function create(options) {
    options = options || {};
    var schedule = options.setTimeout || root.setTimeout.bind(root);
    var cancel = options.clearTimeout || root.clearTimeout.bind(root);
    var onTimeout = typeof options.onTimeout === "function"
      ? options.onTimeout
      : function () {};
    var configuredTimeout = Number(options.timeoutMs || DELIVERY_TIMEOUT_MS);
    var timeoutMs = Number.isFinite(configuredTimeout) && configuredTimeout > 0
      ? configuredTimeout
      : DELIVERY_TIMEOUT_MS;
    var timers = Object.create(null);

    function acknowledge(clientRequestId) {
      var requestId = String(clientRequestId || "");
      if (!requestId || !Object.prototype.hasOwnProperty.call(timers, requestId)) {
        return false;
      }
      cancel(timers[requestId]);
      delete timers[requestId];
      return true;
    }

    function watch(clientRequestId) {
      var requestId = String(clientRequestId || "");
      if (!requestId) {
        throw new Error("A client request id is required to watch message delivery.");
      }
      acknowledge(requestId);
      timers[requestId] = schedule(function () {
        delete timers[requestId];
        onTimeout(requestId);
      }, timeoutMs);
    }

    function clear() {
      Object.keys(timers).forEach(function (requestId) {
        cancel(timers[requestId]);
        delete timers[requestId];
      });
    }

    return Object.freeze({
      acknowledge: acknowledge,
      clear: clear,
      watch: watch,
    });
  }

  root.UAChatDelivery = Object.freeze({
    DELIVERY_TIMEOUT_MS: DELIVERY_TIMEOUT_MS,
    LINK_RESULT_MESSAGE_TYPE: LINK_RESULT_MESSAGE_TYPE,
    TOOL_SETUP_RESULT_MESSAGE_TYPE: TOOL_SETUP_RESULT_MESSAGE_TYPE,
    SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE: SECURE_CREDENTIAL_RESULT_MESSAGE_TYPE,
    buildTimeline: buildTimeline,
    clearErrorNotice: clearErrorNotice,
    create: create,
    createErrorNotice: createErrorNotice,
    latestDurableSessionSequence: latestDurableSessionSequence,
    normalizeTransientItem: normalizeTransientItem,
    reconcileMessages: reconcileMessages,
    reconcileTransientItems: reconcileTransientItems,
    upsertTransientItem: upsertTransientItem,
  });
})(typeof window === "undefined" ? globalThis : window);

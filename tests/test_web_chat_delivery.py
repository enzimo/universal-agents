from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
DELIVERY_ASSET = (
    REPO_ROOT / "assistant_gateway" / "onboarding_web" / "assets" / "chat_delivery.js"
)


def _run_delivery_scenario(scenario: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the Web Chat delivery tracker")

    harness = r"""
const fs = require("fs");
const vm = require("vm");

const scheduled = new Map();
let nextTimerId = 0;
const sandbox = {
  URL,
  setTimeout(callback, delay) {
    nextTimerId += 1;
    scheduled.set(nextTimerId, { callback, delay });
    return nextTimerId;
  },
  clearTimeout(timerId) {
    scheduled.delete(timerId);
  },
  testClock: {
    delays() {
      return Array.from(scheduled.values()).map((timer) => timer.delay);
    },
    runNext() {
      const next = scheduled.entries().next();
      if (next.done) {
        return false;
      }
      const [timerId, timer] = next.value;
      scheduled.delete(timerId);
      timer.callback();
      return true;
    },
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox, {
  filename: "chat_delivery.js",
});
const result = vm.runInContext(fs.readFileSync(0, "utf8"), sandbox, {
  filename: "delivery_scenario.js",
});
process.stdout.write(JSON.stringify(result));
"""
    result = subprocess.run(
        [node, "-e", harness, str(DELIVERY_ASSET)],
        input=scenario,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_delivery_tracker_waits_fifteen_seconds_and_cancels_on_ack() -> None:
    result = _run_delivery_scenario(
        """
(() => {
  const timedOut = [];
  const tracker = UAChatDelivery.create({
    onTimeout(clientRequestId) {
      timedOut.push(clientRequestId);
    },
  });
  tracker.watch("request_1");
  const scheduledDelays = testClock.delays();
  const acknowledged = tracker.acknowledge("request_1");
  const ranCancelledTimer = testClock.runNext();
  tracker.watch("request_2");
  const ranTimeout = testClock.runNext();
  return {
    scheduledDelays,
    acknowledged,
    ranCancelledTimer,
    ranTimeout,
    timedOut,
  };
})()
"""
    )

    assert result == {
        "scheduledDelays": [15000],
        "acknowledged": True,
        "ranCancelledTimer": False,
        "ranTimeout": True,
        "timedOut": ["request_2"],
    }


def test_error_notice_clears_only_after_matching_operation_recovers() -> None:
    result = _run_delivery_scenario(
        """
(() => {
  const renameFailure = UAChatDelivery.createErrorNotice(
    "rename_thread",
    "Rename failed."
  );
  const afterSettingsRecovery = UAChatDelivery.clearErrorNotice(
    renameFailure,
    "open_settings"
  );
  const afterRenameRecovery = UAChatDelivery.clearErrorNotice(
    afterSettingsRecovery,
    "rename_thread"
  );
  return {
    renameFailure,
    unrelatedRecoveryPreservedNotice: afterSettingsRecovery === renameFailure,
    matchingRecovery: afterRenameRecovery,
  };
})()
"""
    )

    assert result == {
        "renameFailure": {
            "source": "rename_thread",
            "message": "Rename failed.",
        },
        "unrelatedRecoveryPreservedNotice": True,
        "matchingRecovery": None,
    }


def test_delivery_reconciliation_replaces_only_the_exact_optimistic_message() -> None:
    result = _run_delivery_scenario(
        """
(() => {
  const current = [
    { messageId: "server_1", sessionSequence: 1, optimistic: false },
    {
      messageId: "optimistic:request_1",
      clientRequestId: "request_1",
      optimistic: true,
      localDeliveryState: "not_delivered",
      localSequence: 1,
      anchorSessionSequence: 1,
    },
    {
      messageId: "optimistic:request_2",
      clientRequestId: "request_2",
      optimistic: true,
      localDeliveryState: "pending",
      localSequence: 2,
      anchorSessionSequence: 1,
    },
  ];
  const incoming = [
    {
      messageId: "server_2",
      clientRequestId: "request_1",
      sessionSequence: 2,
      optimistic: false,
    },
  ];
  const reconciled = UAChatDelivery.reconcileMessages(current, incoming);
  return {
    messageIds: reconciled.messages.map((message) => message.messageId),
    acknowledgedIds: reconciled.acknowledgedMessages.map(
      (message) => message.clientRequestId
    ),
    remainingLocalState: reconciled.messages[2].localDeliveryState,
    remainingAnchor: reconciled.messages[2].anchorSessionSequence,
  };
})()
"""
    )

    assert result == {
        "messageIds": [
            "server_1",
            "server_2",
            "optimistic:request_2",
        ],
        "acknowledgedIds": ["request_1"],
        "remainingLocalState": "pending",
        "remainingAnchor": 2,
    }


def test_timeline_orders_memory_only_items_between_durable_messages() -> None:
    result = _run_delivery_scenario(
        """
(() => {
  const timeline = UAChatDelivery.buildTimeline(
    [
      { messageId: "message_1", sessionSequence: 1, optimistic: false },
      { messageId: "message_2", sessionSequence: 2, optimistic: false },
      {
        messageId: "optimistic:request_1",
        sessionSequence: 0,
        optimistic: true,
        localSequence: 1,
        anchorSessionSequence: 2,
      },
    ],
    [
      {
        itemId: "secure_credential_collection:corr_1",
        kind: "secure_credential_collection",
        correlationId: "corr_1",
        sessionSequence: 1,
      },
      {
        itemId: "sensitive_command_result:message_2",
        kind: "sensitive_command_result",
        sessionSequence: 2,
      },
    ]
  );
  return timeline.map((item) => ({
    kind: item.kind,
    id: item.value.messageId || item.value.itemId,
  }));
})()
"""
    )

    assert result == [
        {"kind": "message", "id": "message_1"},
        {
            "kind": "transient_item",
            "id": "secure_credential_collection:corr_1",
        },
        {"kind": "message", "id": "message_2"},
        {
            "kind": "transient_item",
            "id": "sensitive_command_result:message_2",
        },
        {"kind": "message", "id": "optimistic:request_1"},
    ]


def test_failed_optimistic_message_keeps_its_position_before_later_messages() -> None:
    result = _run_delivery_scenario(
        """
(() => {
  const messagesAtSubmission = [
    {
      messageId: "user_719",
      sessionSequence: 1,
      createdAt: "2026-08-30T19:19:00Z",
    },
    {
      messageId: "assistant_719",
      sessionSequence: 2,
      createdAt: "2026-08-30T19:19:30Z",
    },
    {
      messageId: "assistant_727",
      sessionSequence: 3,
      createdAt: "2026-08-30T19:27:00Z",
    },
  ];
  const failedFollowup = {
    messageId: "optimistic:continue",
    clientRequestId: "continue_request",
    optimistic: true,
    localSequence: 1,
    anchorSessionSequence: UAChatDelivery.latestDurableSessionSequence(
      messagesAtSubmission
    ),
    createdAt: "2026-08-30T19:49:00Z",
    localDeliveryState: "not_delivered",
  };
  const laterFollowup = {
    messageId: "optimistic:site_request",
    clientRequestId: "site_request",
    optimistic: true,
    localSequence: 2,
    anchorSessionSequence: failedFollowup.anchorSessionSequence,
    createdAt: "2026-08-30T20:12:00Z",
    localDeliveryState: "pending",
  };
  const reconciled = UAChatDelivery.reconcileMessages(
    messagesAtSubmission.concat([failedFollowup, laterFollowup]),
    [
      {
        messageId: "user_812",
        clientRequestId: "site_request",
        sessionSequence: 4,
        createdAt: "2026-08-30T20:12:00Z",
      },
      {
        messageId: "assistant_814",
        sessionSequence: 5,
        createdAt: "2026-08-30T20:14:00Z",
      },
    ]
  );
  return {
    timeline: UAChatDelivery.buildTimeline(reconciled.messages, []).map((item) => ({
      id: item.value.messageId,
      deliveryState: item.value.localDeliveryState || "",
    })),
    acknowledgedIds: reconciled.acknowledgedMessages.map(
      (message) => message.clientRequestId
    ),
  };
})()
"""
    )

    assert result == {
        "timeline": [
            {"id": "user_719", "deliveryState": ""},
            {"id": "assistant_719", "deliveryState": ""},
            {"id": "assistant_727", "deliveryState": ""},
            {"id": "optimistic:continue", "deliveryState": "not_delivered"},
            {"id": "user_812", "deliveryState": ""},
            {"id": "assistant_814", "deliveryState": ""},
        ],
        "acknowledgedIds": ["site_request"],
    }


@pytest.mark.parametrize(
    "kind",
    [
        "secure_credential_collection",
        "tool_setup",
        "broker_authentication",
        "account_link",
    ],
)
def test_transient_item_normalization_preserves_notice_and_validates_action(
    kind: str,
) -> None:
    result = _run_delivery_scenario(
        """
(() => {
  const notice = UAChatDelivery.normalizeTransientItem({
    kind: "sensitive_command_result",
    item_id: "sensitive_command_result:message_1",
    session_sequence: 1,
    created_at_utc: "2026-08-30T01:00:00Z",
    content: "Invite code: uainvite_secret",
  }, "https://chat.example.test");
  const action = UAChatDelivery.normalizeTransientItem({
    kind: "secure_credential_collection",
    item_id: "secure_credential_collection:corr_1",
    correlation_id: "corr_1",
    session_sequence: 2,
    created_at_utc: "2026-08-30T01:01:00Z",
    content: "Open the secure form.",
    action: {
      label: "Open secure credential form",
      url: "/secure/form",
      expires_at_utc: "2026-08-30T01:16:00Z",
    },
  }, "https://chat.example.test");
  const wrongIdentity = UAChatDelivery.normalizeTransientItem({
    kind: "secure_credential_collection",
    item_id: "secure_credential_collection:corr_wrong",
    correlation_id: "corr_1",
    session_sequence: 2,
    action: { label: "Open", url: "/secure/form" },
  }, "https://chat.example.test");
  const unsafeUrl = UAChatDelivery.normalizeTransientItem({
    kind: "secure_credential_collection",
    item_id: "secure_credential_collection:corr_1",
    correlation_id: "corr_1",
    session_sequence: 2,
    action: { label: "Open", url: "javascript:alert(1)" },
  }, "https://chat.example.test");
  return {
    notice,
    action,
    wrongIdentity,
    unsafeUrl,
  };
})()
""".replace("secure_credential_collection", kind)
    )

    assert result == {
        "notice": {
            "itemId": "sensitive_command_result:message_1",
            "kind": "sensitive_command_result",
            "content": "Invite code: uainvite_secret",
            "sessionSequence": 1,
            "createdAt": "2026-08-30T01:00:00Z",
        },
        "action": {
            "itemId": f"{kind}:corr_1",
            "kind": kind,
            "content": "Open the secure form.",
            "sessionSequence": 2,
            "createdAt": "2026-08-30T01:01:00Z",
            "label": "Open secure credential form",
            "url": "https://chat.example.test/secure/form",
            "expiresAtUtc": "2026-08-30T01:16:00Z",
            "correlationId": "corr_1",
        },
        "wrongIdentity": None,
        "unsafeUrl": None,
    }


def test_transient_item_reconciliation_is_correlation_and_terminal_state_bound() -> (
    None
):
    result = _run_delivery_scenario(
        """
(() => {
  const items = [
    {
      itemId: "sensitive_command_result:notice",
      kind: "sensitive_command_result",
      sessionSequence: 1,
    },
    {
      itemId: "secure_credential_collection:corr_collection",
      kind: "secure_credential_collection",
      correlationId: "corr_collection",
      sessionSequence: 2,
    },
    {
      itemId: "account_link:corr_command",
      kind: "account_link",
      correlationId: "corr_command",
      sessionSequence: 3,
    },
    {
      itemId: "tool_setup:corr_link",
      kind: "tool_setup",
      correlationId: "corr_link",
      sessionSequence: 4,
    },
  ];
  const unrelated = UAChatDelivery.reconcileTransientItems(items, [
    {
      messageId: "message_unrelated",
      correlationId: "corr_other",
      messageType: "task",
      sessionSequence: 5,
    },
    {
      messageId: "message_progress",
      correlationId: "corr_collection",
      messageType: "system_status",
      sessionSequence: 3,
    },
  ]);
  const terminalFailure = UAChatDelivery.reconcileTransientItems(unrelated, [
    {
      messageId: "message_terminal_failure",
      correlationId: "corr_collection",
      messageType: "task",
      messageLevel: "attention",
      sessionSequence: 6,
    },
  ]);
  const staleTerminal = UAChatDelivery.reconcileTransientItems(terminalFailure, [
    {
      messageId: "message_stale",
      correlationId: "corr_link",
      messageType: "task",
      sessionSequence: 4,
    },
  ]);
  const terminalSuccess = UAChatDelivery.reconcileTransientItems(staleTerminal, [
    {
      messageId: "message_terminal_success",
      correlationId: "corr_link",
      messageType: "task",
      sessionSequence: 7,
    },
    {
      // A task answer never closes a /link card; only its link_result does.
      messageId: "message_after_command",
      correlationId: "corr_command",
      messageType: "task",
      sessionSequence: 8,
    },
  ]);
  return {
    afterUnrelated: unrelated.map((item) => item.itemId),
    afterFailure: terminalFailure.map((item) => item.itemId),
    afterStale: staleTerminal.map((item) => item.itemId),
    afterSuccess: terminalSuccess.map((item) => item.itemId),
  };
})()
"""
    )

    assert result == {
        "afterUnrelated": [
            "sensitive_command_result:notice",
            "secure_credential_collection:corr_collection",
            "account_link:corr_command",
            "tool_setup:corr_link",
        ],
        "afterFailure": [
            "sensitive_command_result:notice",
            "account_link:corr_command",
            "tool_setup:corr_link",
        ],
        "afterStale": [
            "sensitive_command_result:notice",
            "account_link:corr_command",
            "tool_setup:corr_link",
        ],
        "afterSuccess": [
            "sensitive_command_result:notice",
            "account_link:corr_command",
        ],
    }


def test_transient_item_upsert_keeps_concurrent_items_and_replaces_retries() -> None:
    result = _run_delivery_scenario(
        """
(() => {
  let items = UAChatDelivery.upsertTransientItem([], {
    itemId: "secure_credential_collection:corr_1",
    kind: "secure_credential_collection",
    correlationId: "corr_1",
    label: "First",
    sessionSequence: 2,
  });
  items = UAChatDelivery.upsertTransientItem(items, {
    itemId: "sensitive_command_result:notice",
    kind: "sensitive_command_result",
    label: "Copy",
    sessionSequence: 3,
  });
  items = UAChatDelivery.upsertTransientItem(items, {
    itemId: "tool_setup:corr_2",
    kind: "tool_setup",
    correlationId: "corr_2",
    label: "Second",
    sessionSequence: 4,
  });
  items = UAChatDelivery.upsertTransientItem(items, {
    itemId: "secure_credential_collection:corr_1",
    kind: "secure_credential_collection",
    correlationId: "corr_1",
    label: "First retry",
    sessionSequence: 2,
  });
  return items.map((item) => ({ id: item.itemId, label: item.label }));
})()
"""
    )

    assert result == [
        {"id": "sensitive_command_result:notice", "label": "Copy"},
        {"id": "tool_setup:corr_2", "label": "Second"},
        {
            "id": "secure_credential_collection:corr_1",
            "label": "First retry",
        },
    ]


def test_async_credential_controls_reconcile_poll_retries_and_dismissal():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the real chat polling helper")
    harness = r"""
const fs = require("fs"), vm = require("vm");
const thread = { messages: [], transientItems: [] };
let renders = 0;
const sandbox = { URL, Set, window: { location: { origin: "https://gateway.example" } },
    threadForSession: key => key === "session" ? thread : null,
    renderMessages: () => { renders++; } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
const source = fs.readFileSync(process.argv[2], "utf8");
vm.runInContext(source.slice(source.indexOf("function applyCredentialActions("),
    source.indexOf("function applyTranscriptEvent(")), sandbox);
const action = { kind: "tool_setup", item_id: "tool_setup:request",
    request_id: "request", correlation_id: "correlation", session_key: "session",
    session_sequence: 3, content: "Choose a credential", action: {
        label: "Open account link form", url: "https://broker.example/form/secret",
        expires_at_utc: "2099-01-01T00:00:00Z" } };
sandbox.applyCredentialActions([action]);
if (thread.transientItems.length !== 1) throw new Error("Missing credential control");
sandbox.applyCredentialActions([action]);
if (renders !== 1) throw new Error("Polling replaced existing control");
thread.dismissedTransientItems.add(thread.transientItems[0].itemId);
thread.transientItems = [];
sandbox.applyCredentialActions([action]);
if (thread.transientItems.length) throw new Error("Dismissed form resurrected");
// A genuinely new request for the same task may still appear.
action.request_id = "fresh"; action.item_id = "tool_setup:fresh";
sandbox.applyCredentialActions([action]);
if (thread.transientItems.length !== 1) throw new Error("Fresh form was suppressed");
thread.messages.push({ correlationId: "correlation", messageType: "task", sessionSequence: 4 });
thread.transientItems = [];
sandbox.applyCredentialActions([action]);
if (thread.transientItems.length) throw new Error("Completed form resurrected");
"""
    subprocess.run([node, "-e", harness, str(DELIVERY_ASSET), str(DELIVERY_ASSET.with_name("chat.js"))],
        capture_output=True, text=True, check=True)

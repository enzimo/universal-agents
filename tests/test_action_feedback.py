from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ACTION_FEEDBACK_ASSET = (
    Path(__file__).resolve().parents[1]
    / "assistant_gateway"
    / "onboarding_web"
    / "assets"
    / "action_feedback.js"
)


def test_action_feedback_disables_group_and_restores_original_state() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the browser helper test")
    harness = r"""
const fs = require("fs");
const vm = require("vm");

class ClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class Button {
  constructor(text, disabled) {
    this.textContent = text;
    this.disabled = Boolean(disabled);
    this.isConnected = true;
    this.classList = new ClassList();
    this.attributes = new Map();
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  getAttribute(name) { return this.attributes.get(name) || null; }
}

const primary = new Button("Approve", false);
const reject = new Button("Reject", false);
const unavailable = new Button("Unavailable", true);
const group = { querySelectorAll: () => [primary, reject, unavailable] };
const context = { window: {} };
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);

const finish = context.window.UAActionFeedback.begin(primary, {
  busyLabel: "Approving…",
  group,
});
const pending = {
  primaryDisabled: primary.disabled,
  rejectDisabled: reject.disabled,
  unavailableDisabled: unavailable.disabled,
  label: primary.textContent,
  ariaBusy: primary.getAttribute("aria-busy"),
  classPresent: primary.classList.contains("is-action-pending"),
  helperPending: context.window.UAActionFeedback.isPending(primary),
};
finish();
const restored = {
  primaryDisabled: primary.disabled,
  rejectDisabled: reject.disabled,
  unavailableDisabled: unavailable.disabled,
  label: primary.textContent,
  ariaBusy: primary.getAttribute("aria-busy"),
  classPresent: primary.classList.contains("is-action-pending"),
  helperPending: context.window.UAActionFeedback.isPending(primary),
};
process.stdout.write(JSON.stringify({ pending, restored }));
"""

    completed = subprocess.run(
        [node, "-e", harness, str(ACTION_FEEDBACK_ASSET)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["pending"] == {
        "primaryDisabled": True,
        "rejectDisabled": True,
        "unavailableDisabled": True,
        "label": "Approving…",
        "ariaBusy": "true",
        "classPresent": True,
        "helperPending": True,
    }
    assert result["restored"] == {
        "primaryDisabled": False,
        "rejectDisabled": False,
        "unavailableDisabled": True,
        "label": "Approve",
        "ariaBusy": None,
        "classPresent": False,
        "helperPending": False,
    }

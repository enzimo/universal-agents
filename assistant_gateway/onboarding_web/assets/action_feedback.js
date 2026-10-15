(function () {
  "use strict";

  var activeButtons = new WeakMap();
  var refreshes = new WeakMap();

  function beginRefresh(button) {
    // Tab changes can overlap reads that share one Refresh button.
    var state = refreshes.get(button);
    if (!state) {
      state = { count: 0, disabled: button.disabled };
      refreshes.set(button, state);
    }
    state.count++;
    button.disabled = true;
    button.classList.add("is-refreshing");
    button.setAttribute("aria-busy", "true");
    var finished = false;
    return function () {
      if (finished) return;
      finished = true;
      if (--state.count) return;
      refreshes.delete(button);
      button.disabled = state.disabled;
      button.classList.remove("is-refreshing");
      button.removeAttribute("aria-busy");
    };
  }

  function uniqueButtons(primaryButton, relatedButtons) {
    var buttons = [primaryButton];
    if (relatedButtons && typeof relatedButtons.forEach === "function") {
      relatedButtons.forEach(function (button) {
        if (button && buttons.indexOf(button) === -1) {
          buttons.push(button);
        }
      });
    }
    return buttons;
  }

  function begin(primaryButton, options) {
    var settings = options || {};
    if (!primaryButton || activeButtons.has(primaryButton)) {
      return function () {};
    }

    var relatedButtons = settings.relatedButtons;
    if (!relatedButtons && settings.group && settings.group.querySelectorAll) {
      relatedButtons = settings.group.querySelectorAll("button");
    }
    var buttons = uniqueButtons(primaryButton, relatedButtons);
    var originalStates = buttons.map(function (button) {
      return { button: button, disabled: Boolean(button.disabled) };
    });
    var originalText = primaryButton.textContent;
    var state = { finish: null };

    originalStates.forEach(function (entry) {
      entry.button.disabled = true;
    });
    primaryButton.classList.add("is-action-pending");
    primaryButton.setAttribute("aria-busy", "true");
    if (settings.busyLabel) {
      primaryButton.textContent = String(settings.busyLabel);
    }

    state.finish = function () {
      if (activeButtons.get(primaryButton) !== state) {
        return;
      }
      activeButtons.delete(primaryButton);
      primaryButton.classList.remove("is-action-pending");
      primaryButton.removeAttribute("aria-busy");
      primaryButton.textContent = originalText;
      originalStates.forEach(function (entry) {
        if (entry.button.isConnected) {
          entry.button.disabled = entry.disabled;
        }
      });
    };
    activeButtons.set(primaryButton, state);
    return state.finish;
  }

  function isPending(button) {
    return Boolean(button && activeButtons.has(button));
  }

  window.UAActionFeedback = Object.freeze({
    begin: begin,
    beginRefresh: beginRefresh,
    isPending: isPending,
  });
})();

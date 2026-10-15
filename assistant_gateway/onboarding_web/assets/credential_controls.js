(function () {
  "use strict";

  function toggleReveal(input, button) {
    var isVisible = input.type === "text";
    input.type = isVisible ? "password" : "text";
    button.setAttribute("aria-pressed", isVisible ? "false" : "true");
  }

  function fallbackCopyText(text) {
    return new Promise(function (resolve, reject) {
      var textarea = document.createElement("textarea");
      textarea.value = String(text || "");
      textarea.setAttribute("readonly", "readonly");
      textarea.style.position = "fixed";
      textarea.style.top = "-1000px";
      textarea.style.left = "-1000px";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      textarea.setSelectionRange(0, textarea.value.length);
      try {
        if (document.execCommand("copy")) {
          resolve();
        } else {
          reject(new Error("Copy command was rejected."));
        }
      } catch (error) {
        reject(error);
      } finally {
        textarea.remove();
      }
    });
  }

  function copyTextToClipboard(value) {
    var text = String(value || "");
    if (window.navigator.clipboard && window.isSecureContext) {
      return window.navigator.clipboard.writeText(text).catch(function () {
        return fallbackCopyText(text);
      });
    }
    return fallbackCopyText(text);
  }

  function flashCopied(button) {
    button.classList.add("is-copied");
    window.setTimeout(function () {
      button.classList.remove("is-copied");
    }, 1400);
  }

  function secureRandomHexToken(prefix, byteLength) {
    if (!window.crypto || typeof window.crypto.getRandomValues !== "function") {
      throw new Error("Secure token generation is unavailable in this browser.");
    }
    var bytes = new Uint8Array(byteLength || 24);
    window.crypto.getRandomValues(bytes);
    return String(prefix || "") + Array.from(bytes).map(function (byte) {
      return byte.toString(16).padStart(2, "0");
    }).join("");
  }

  function formatSessionCountdown(expiresAtUtc, nowMilliseconds) {
    var expiresAtMilliseconds = Date.parse(String(expiresAtUtc || ""));
    if (!Number.isFinite(expiresAtMilliseconds)) {
      return "";
    }
    var currentMilliseconds = Number.isFinite(Number(nowMilliseconds))
      ? Number(nowMilliseconds)
      : Date.now();
    var remainingSeconds = Math.max(
      0,
      Math.ceil((expiresAtMilliseconds - currentMilliseconds) / 1000)
    );
    if (remainingSeconds <= 0) {
      return "expired";
    }
    var minutes = Math.floor(remainingSeconds / 60);
    var seconds = String(remainingSeconds % 60).padStart(2, "0");
    return minutes + ":" + seconds + " remaining";
  }

  window.UACredentialControls = Object.freeze({
    copyTextToClipboard: copyTextToClipboard,
    flashCopied: flashCopied,
    formatSessionCountdown: formatSessionCountdown,
    secureRandomHexToken: secureRandomHexToken,
    toggleReveal: toggleReveal,
  });
})();

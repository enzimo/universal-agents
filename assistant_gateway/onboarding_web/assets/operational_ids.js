(function () {
  "use strict";

  var MAX_OPERATIONAL_ID_PREFIX_LENGTH = 15;

  function newId(prefix) {
    var normalizedPrefix = String(prefix || "");
    if (!normalizedPrefix) {
      throw new Error("Identifier prefix must be a non-empty string.");
    }
    if (normalizedPrefix.length > MAX_OPERATIONAL_ID_PREFIX_LENGTH) {
      throw new Error("Identifier prefix must be at most 15 characters.");
    }
    if (!window.crypto || typeof window.crypto.getRandomValues !== "function") {
      throw new Error("Secure identifier generation is unavailable in this browser.");
    }
    var bytes = new Uint8Array(6);
    window.crypto.getRandomValues(bytes);
    var suffix = Array.from(bytes).map(function (byte) {
      return byte.toString(16).padStart(2, "0");
    }).join("");
    return normalizedPrefix + "_" + suffix;
  }

  window.UAOperationalIds = Object.freeze({
    newId: newId,
  });
})();

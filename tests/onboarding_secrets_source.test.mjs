// Onboarding page behavior for secrets that come from the environment
// (launch.secrets_source: environment) and for settings imports.
import assert from "node:assert/strict";
import test from "node:test";
import { createConfiguration } from "../assistant_gateway/onboarding_web/assets/app_configuration.mjs";
import { createFinalization } from "../assistant_gateway/onboarding_web/assets/app_finalization.mjs";
import { byTag, descendants, fakeDocument } from "./settings_fake_dom.mjs";

const SECRET_VALUE = "value-that-must-never-be-shown";

function sharedStateWith(state) {
  return {
    SECRETS_SOURCE_ENV_FILE: "env_file",
    SECRETS_SOURCE_ENVIRONMENT: "environment",
    SECRETS_FROM_ENVIRONMENT_STORAGE_KEY: "ua.onboarding.secrets.from_environment",
    agents: [],
    safetyChoices: [],
    state: { secretsFromEnvironment: false, missingSecrets: [], ...state },
  };
}

function installBrowser() {
  const storage = new Map();
  globalThis.document = fakeDocument();
  globalThis.window = {
    localStorage: {
      getItem: key => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
  };
  return storage;
}

function finalizationFor(state) {
  installBrowser();
  const sharedState = sharedStateWith(state);
  const lifecycle = { listen() {} };
  return createFinalization({ finalizationState: {}, sharedState, operations: {}, lifecycle, requestClient: {} });
}

test("missing secrets are listed by name with how to add them", () => {
  const finalization = finalizationFor({ secretsFromEnvironment: true });

  const box = finalization.renderMissingSecrets([
    { name: "ACCESS_GATEWAY_SIGNING_KEY", purpose: "Signs tasks.", how_to_create: "Run the script." },
  ]);

  assert.ok(box.classList.contains("warning-callout"));
  assert.equal(box.getAttribute("role"), "alert");
  const text = box.textContent;
  assert.match(text, /ACCESS_GATEWAY_SIGNING_KEY/);
  assert.match(text, /ACCESS_GATEWAY_SIGNING_KEY_FILE/);
  assert.match(text, /Docker secret/);
  assert.doesNotMatch(text, new RegExp(SECRET_VALUE));
});

test("the finalize summary says what was not saved", () => {
  const finalization = finalizationFor({ secretsFromEnvironment: true });

  const summary = finalization.environmentSecretsSummary({
    secrets_source: "environment",
    source_env_ignored: true,
    unsaved_secret_names: ["XAI_API_KEY"],
  });

  assert.match(summary, /no team\.env was written/);
  assert.match(summary, /\.env file you chose was not saved/);
  assert.match(summary, /XAI_API_KEY/);
  assert.equal(finalization.environmentSecretsSummary({ secrets_source: "env_file" }), "");
});

test("there is no team.env download when secrets come from the environment", () => {
  const labels = secretsFromEnvironment => {
    const finalization = finalizationFor({ secretsFromEnvironment });
    return byTag(finalization.renderDownloadButtons(false), "button").map(button => button.textContent);
  };

  assert.deepEqual(labels(true), ["instance_config.yaml"]);
  assert.deepEqual(labels(false), ["instance_config.yaml", "team.env"]);
});

test("an import sets the secrets switch and never the old instance folder", () => {
  installBrowser();
  const sharedState = sharedStateWith({ networkPublicOrigin: "" });
  const switched = [];
  const operations = new Proxy(
    { setSecretsFromEnvironment: value => { switched.push(value); sharedState.state.secretsFromEnvironment = value; } },
    { get: (target, name) => target[name] || (() => "") },
  );
  const configuration = createConfiguration({ sharedState, operations, lifecycle: { listen() {} }, requestClient: {} });

  configuration.applyImportedSettings(
    { secrets_source: "environment", instance_root: "/srv/old-instance" },
    { overwriteExisting: true },
  );

  assert.deepEqual(switched, [true]);
  assert.equal(sharedState.state.instanceRoot, undefined);
  assert.equal(window.localStorage.getItem("ua.onboarding.instance.root"), null);
  assert.equal(descendants(document.body).length, 0);
});

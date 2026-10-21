import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { createIsolatedOtel } from "@photon-ai/otel";
import {
  Spectrum,
  attachment,
  markdown,
  text,
  typing,
  voice,
} from "@spectrum-ts/core";
import { imessage } from "@spectrum-ts/imessage";

import { isSupportedNodeVersion } from "./spectrum-runtime.mjs";

test("Spectrum v12 exposes the sidecar's required public API", () => {
  assert.equal(typeof Spectrum, "function");
  assert.equal(typeof attachment, "function");
  assert.equal(typeof markdown, "function");
  assert.equal(typeof text, "function");
  assert.equal(typeof typing, "function");
  assert.equal(typeof voice, "function");
  assert.equal(typeof imessage, "function");
  assert.equal(typeof imessage.config, "function");
});

test("iMessage uses Spectrum v12's canonical provider id", () => {
  const provider = imessage.config();
  assert.equal(provider.__tag, "PlatformProviderConfig");
  assert.equal(provider.__name, "imessage");
  assert.deepEqual(provider.config, {});
});

test("Spectrum v12 accepts providers and rejects the removed platforms key", async () => {
  const clientConfig = {
    address: "http://127.0.0.1:1",
    token: "spectrum-contract-test",
    phone: "+15551234567",
  };

  await assert.rejects(
    Spectrum({
      platforms: [imessage.config({ clients: clientConfig })],
      telemetry: false,
    }),
    /providers/
  );

  const app = await Spectrum({
    providers: [imessage.config({ clients: clientConfig })],
    options: { flattenGroups: true },
    telemetry: false,
  });
  try {
    const provider = imessage(app);
    assert.equal(typeof app.messages[Symbol.asyncIterator], "function");
    assert.equal(typeof app.stop, "function");
    assert.equal(typeof provider.space.create, "function");
    assert.equal(typeof provider.space.get, "function");
    assert.equal(typeof provider.getMessage, "function");
  } finally {
    await app.stop();
  }
});

test("text, markdown, and typing builders retain the sidecar contract", async () => {
  assert.deepEqual(await text("hello").build(), {
    type: "text",
    text: "hello",
  });
  assert.deepEqual(await markdown("[Photon](https://photon.codes)").build(), {
    type: "markdown",
    markdown: "[Photon](https://photon.codes)",
  });
  assert.deepEqual(await typing("start").build(), {
    type: "typing",
    state: "start",
  });
});

test("attachment and voice builders retain the sidecar contract", async () => {
  const fixturePath = fileURLToPath(new URL("./package.json", import.meta.url));
  const builtAttachment = await attachment(fixturePath, {
    name: "package.json",
    mimeType: "application/json",
  }).build();
  const builtVoice = await voice(fixturePath, {
    name: "sample.caf",
    mimeType: "audio/x-caf",
  }).build();

  assert.equal(builtAttachment.type, "attachment");
  assert.equal(builtAttachment.name, "package.json");
  assert.equal(builtAttachment.mimeType, "application/json");
  assert.equal(typeof builtAttachment.id, "string");
  assert.equal(builtAttachment.size > 0, true);
  assert.equal(builtVoice.type, "voice");
  assert.equal(builtVoice.name, "sample.caf");
  assert.equal(builtVoice.mimeType, "audio/x-caf");
  assert.equal(builtVoice.size > 0, true);
});

test("the runtime enforces the repository's Node floor", () => {
  assert.equal(isSupportedNodeVersion("18.20.0"), false);
  assert.equal(isSupportedNodeVersion("20.6.0"), false);
  assert.equal(isSupportedNodeVersion("22.22.0"), false);
  assert.equal(isSupportedNodeVersion("23.11.1"), false);
  assert.equal(isSupportedNodeVersion("24.0.0"), false);
  assert.equal(isSupportedNodeVersion("24.4.9"), false);
  assert.equal(isSupportedNodeVersion("24.5.0"), true);
  assert.equal(isSupportedNodeVersion("25.0.0"), true);
  assert.equal(isSupportedNodeVersion("garbage"), false);
});

test("the patched Photon telemetry dependency constructs and shuts down", async () => {
  // Photon OTel 3.7 still pins the pre-fix exporter line. Its scoped core 2.8
  // override fixes GHSA-8988-4f7v-96qf without crossing the exporter's breaking
  // 0.220 constructor change; exercise that boundary rather than import alone.
  const handle = createIsolatedOtel({
    endpoint: "http://127.0.0.1:1",
    serviceName: "universal-agents-photon-contract-test",
    traceparentHeader: "x-ua-traceparent",
  });

  assert.equal(typeof handle.shutdown, "function");
  await handle.shutdown();
});

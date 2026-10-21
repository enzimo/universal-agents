import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

const fixture = new URL("../../../tests/fixtures/tls/distrusted-emsign-g3.crt", import.meta.url);
const moduleUrl = new URL("./trust-store.mjs", import.meta.url).href;
let directory, certificate, key, extraRoots;

before(() => {
  directory = mkdtempSync(join(tmpdir(), "ua-photon-trust-"));
  certificate = join(directory, "private-ca.crt");
  key = join(directory, "private-ca.key");
  execFileSync("openssl", [
    "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
    "-keyout", key, "-out", certificate,
  ], { stdio: "ignore" });
  extraRoots = join(directory, "extra.crt");
  writeFileSync(extraRoots, readFileSync(fixture, "utf8") + readFileSync(certificate, "utf8"));
});

after(() => rmSync(directory, { recursive: true, force: true }));

function run(code, environment = {}, nodeOptions = []) {
  const env = { ...process.env, ...environment };
  delete env.GRPC_DEFAULT_SSL_ROOTS_FILE_PATH;
  Object.assign(env, environment);
  return spawnSync(process.execPath, [...nodeOptions, "--input-type=module", "-e", `
    import { configureTrustStore } from ${JSON.stringify(moduleUrl)};
    ${code}
  `], { env, encoding: "utf8", timeout: 15000 });
}

test("effective Node roots lose only G3, including an extra copy, and private TLS still connects", () => {
  const result = run(`
    import assert from "node:assert/strict";
    import tls from "node:tls";
    import { X509Certificate } from "node:crypto";
    import { readFileSync } from "node:fs";
    import { once } from "node:events";
    const fingerprint = pem => new X509Certificate(pem).fingerprint256.replaceAll(":", "").toLowerCase();
    const blocked = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b";
    const before = new Set(tls.getCACertificates("default").map(fingerprint));
    assert(before.has(blocked), "test must reproduce trust in G3 before filtering");
    assert(configureTrustStore() > 0);
    const after = new Set(tls.getCACertificates("default").map(fingerprint));
    before.delete(blocked);
    assert.deepEqual(after, before);
    const server = tls.createServer({
      key: readFileSync(process.env.TEST_KEY), cert: readFileSync(process.env.TEST_CERT),
    }, socket => socket.end("trusted"));
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    try {
      const client = tls.connect({port: server.address().port, host: "127.0.0.1", servername: "localhost"});
      await once(client, "secureConnect");
      assert(client.authorized);
      client.destroy();
    } finally { server.close(); }
  `, { NODE_EXTRA_CA_CERTS: extraRoots, TEST_KEY: key, TEST_CERT: certificate });
  assert.equal(result.status, 0, result.stderr);
});

test("explicit gRPC roots are preserved when valid and refused when distrusted, invalid or unreadable", () => {
  const invalid = join(directory, "invalid.crt");
  writeFileSync(invalid, "not a certificate");
  const trusted = join(directory, "trusted.crt");
  const trustedPem = execFileSync("openssl", [
    "x509", "-in", fixture.pathname, "-trustout", "-addtrust", "serverAuth",
  ], { encoding: "utf8" });
  // Valid first, trusted G3 second reproduces the skipped-block bypass.
  writeFileSync(trusted, readFileSync(certificate, "utf8") + trustedPem);
  const legacy = join(directory, "legacy.crt");
  writeFileSync(legacy, readFileSync(fixture, "utf8").replaceAll("CERTIFICATE", "X509 CERTIFICATE"));
  for (const [path, accepted] of [
    [certificate, true], [extraRoots, false], [trusted, false], [legacy, false],
    [invalid, false], [join(directory, "missing.crt"), false],
  ]) {
    const result = run("configureTrustStore();", { GRPC_DEFAULT_SSL_ROOTS_FILE_PATH: path });
    assert.equal(result.status === 0, accepted, result.stderr);
  }
});

test("sidecar refuses a distrusted gRPC bundle before loading Spectrum", () => {
  const result = spawnSync(process.execPath, [new URL("./index.mjs", import.meta.url).pathname], {
    env: {
      ...process.env, PHOTON_PROJECT_ID: "test-project", PHOTON_PROJECT_SECRET: "test-secret",
      PHOTON_SIDECAR_TOKEN: "test-token", GRPC_DEFAULT_SSL_ROOTS_FILE_PATH: extraRoots,
    },
    encoding: "utf8", timeout: 10000,
  });
  assert.equal(result.status, 3, result.stderr);
  assert.match(result.stderr, /could not secure TLS trust store/);
  assert.doesNotMatch(result.stderr, /test-secret|test-token/);
});

test("opaque OpenSSL defaults are refused before replacing their private CA trust", () => {
  const result = run(`
    import assert from "node:assert/strict";
    import tls from "node:tls";
    import { readFileSync } from "node:fs";
    import { once } from "node:events";
    const server = tls.createServer({
      key: readFileSync(process.env.TEST_KEY), cert: readFileSync(process.env.TEST_CERT),
    }, socket => socket.end());
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
    try {
      const client = tls.connect({port: server.address().port, host: "127.0.0.1", servername: "localhost"});
      await once(client, "secureConnect");
      assert(client.authorized, "OpenSSL must trust the private root before configuration");
      client.destroy();
      assert.throws(() => configureTrustStore(), /Cannot enumerate OpenSSL CA defaults safely/);
    } finally { server.close(); }
  `, {
    SSL_CERT_FILE: certificate, SSL_CERT_DIR: directory, NODE_EXTRA_CA_CERTS: "",
    TEST_KEY: key, TEST_CERT: certificate,
  }, ["--use-openssl-ca"]);
  assert.equal(result.status, 0, result.stderr);
});

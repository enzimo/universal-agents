import { X509Certificate } from "node:crypto";
import { readFileSync } from "node:fs";
import tls from "node:tls";

const rootsFile = new URL("../../../data/security/distrusted_roots.json", import.meta.url);
const blockedRoots = new Set(
  JSON.parse(readFileSync(rootsFile, "utf8")).map((root) => root.sha256)
);

function isDistrusted(pem) {
  const certificate = new X509Certificate(pem);
  return blockedRoots.has(certificate.fingerprint256.replaceAll(":", "").toLowerCase());
}

export function configureTrustStore() {
  // grpc-js supplies an explicit `ca` when this override is set. Check it before
  // loading the SDK, which otherwise bypasses Node's filtered default roots.
  const grpcRoots = process.env.GRPC_DEFAULT_SSL_ROOTS_FILE_PATH;
  if (grpcRoots) {
    const certificates = readFileSync(grpcRoots, "utf8").match(
      /-----BEGIN [^-]+-----[\s\S]*?-----END [^-]+-----/g
    );
    if (!certificates?.length || certificates.some(isDistrusted)) {
      throw new Error("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH must contain valid certificates without a distrusted root");
    }
  }
  // Include effective defaults, preserving private CAs from NODE_EXTRA_CA_CERTS
  // and any enabled system store. Run before any TLS connections are created.
  const roots = tls.getCACertificates("default");
  const extra = tls.getCACertificates("extra");
  // In --use-openssl-ca mode Node omits lazy OpenSSL roots from this API,
  // exposing only extras. Custom builds can add another unenumerated store.
  // Refuse these modes rather than silently dropping or broadening trust.
  const opaqueDefaults = roots.length === extra.length &&
    roots.every((pem, index) => pem === extra[index]);
  const customOpenSSLStore = process.config.target_defaults.defines.some(
    (definition) => definition.split("=", 1)[0] === "NODE_OPENSSL_CERT_STORE" ||
      (definition.startsWith("NODE_OPENSSL_SYSTEM_CERT_PATH=") &&
        !definition.endsWith('=""'))
  );
  if (opaqueDefaults || customOpenSSLStore) {
    throw new Error("Cannot enumerate OpenSSL CA defaults safely; use an official Node build in bundled or system CA mode, with NODE_EXTRA_CA_CERTS for private roots");
  }
  const retained = roots.filter((pem) => !isDistrusted(pem));
  tls.setDefaultCACertificates(retained);
  return roots.length - retained.length;
}

These are public CA certificates, with no private keys. G3 reproduces the
distrusted root; G1 proves that other roots from the same issuer remain trusted.
Both come from Debian's `ca-certificates` Mozilla directory. The exact G3 DER
SHA-256 is recorded in `data/security/distrusted_roots.json`.

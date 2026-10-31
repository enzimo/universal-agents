# Temporary ZIP 9 source patches

The registry releases of AnyDoc 0.1.9 and Calamine 0.36.1 require ZIP 8.6,
which is yanked and affected by
[RUSTSEC-2026-0367](https://rustsec.org/advisories/RUSTSEC-2026-0367.html),
[RUSTSEC-2026-0368](https://rustsec.org/advisories/RUSTSEC-2026-0368.html) and
[RUSTSEC-2026-0369](https://rustsec.org/advisories/RUSTSEC-2026-0369.html).
The published AnyDoc 0.2.4 still requires ZIP 8. These local Cargo patches keep
the existing conversion API and document formats while using ZIP 9.1.3.
The dependency audit remains mandatory and has no new advisory exceptions.

Both directories come from the crates.io `.crate` archives. Original archive
SHA-256 checksums and the hashes of modified files are in `sources.json`.
The MIT licenses, examples, changelog and benchmark files are retained.
Only `Cargo.lock`, `Cargo.toml.orig` and `.cargo_vcs_info.json` are omitted:
the workspace owns the lock, uses the normalized manifest, and records
provenance here. These crates remain dependencies, outside our workspace members.

The changes are limited to:

- Both normalized `Cargo.toml` files require ZIP 9.1.3.
- Calamine propagates ZIP filename decoding errors in its path cache and
  picture readers. This backports the Rust changes in upstream commit
  [`c15fefe995e890da206b58542b4e45190f564f30`](https://github.com/tafia/calamine/commit/c15fefe995e890da206b58542b4e45190f564f30).
  Its existing quick-xml version is retained. No AnyDoc Rust source is changed.

Run `python scripts/check_vendored_rust.py` to download the original archives,
verify their checksums, and compare every copied file against the explicit
patch hashes. CI runs this check before Rust quality checks. Intentional patch
changes must update `sources.json` and be reviewed against the upstream source.
`cargo test --locked --workspace` also converts deflated DOCX, PPTX, ODT, ODS,
EPUB and XLSX documents through the CLI, preserves legacy non-UTF-8 filenames,
and rejects truncated archives with a normal conversion error.
Upstream fixture-dependent tests are not included in the published crates.

Remove each Cargo patch and its copied source when a reviewed registry release
supports ZIP 9 or later. Verify the complete conversion API and formats, regenerate
the workspace lock, and run the archive tests and dependency audit before removal.
Remove this checker, manifest and Docker copy when neither patch is needed.

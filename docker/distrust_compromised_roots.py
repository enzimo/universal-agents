"""Remove release-distrusted roots from Debian's configured certificate store.

This uses only the standard library so image builders can run it before Python
dependencies are installed. Match certificate bytes, not filenames or subjects:
other roots from the same issuer must remain trusted.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
import subprocess

LOGGER = logging.getLogger(__name__)
ROOTS_FILE = Path(__file__).resolve().parents[1] / "data/security/distrusted_roots.json"
PEM_BLOCK = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL
)


def certificate_fingerprints(path: Path) -> set[str]:
    fingerprints = set()
    for pem in PEM_BLOCK.findall(path.read_text(encoding="ascii")):
        # OpenSSL also accepts TRUSTED CERTIFICATE with auxiliary trust data.
        # Normalize with the same parser the Debian store uses, hashing only
        # the certificate DER rather than its wrapper or auxiliary data.
        result = subprocess.run(
            ["openssl", "x509", "-outform", "DER"], input=pem.encode("ascii"),
            check=True, capture_output=True,
        )
        fingerprints.add(hashlib.sha256(result.stdout).hexdigest())
    return fingerprints


def _verify_store(output: Path, blocked: set[str]) -> None:
    """Check the bundle and every file OpenSSL can use through CApath."""
    # OpenSSL rehash also accepts .crt/.cer files, and CApath can read a
    # manually installed hash entry. Do not report success while one survives.
    store_files = {
        path.resolve()
        for pattern in ("*.pem", "*.crt", "*.cer", "*.[0-9]*")
        for path in output.glob(pattern)
        if path.is_file()
    }
    for path in sorted(store_files):
        if certificate_fingerprints(path) & blocked:
            raise RuntimeError(f"Distrusted root remains in system certificate store: {path}")
    bundle = output / "ca-certificates.crt"
    if certificate_fingerprints(bundle) & blocked:
        raise RuntimeError("Distrusted root remains in system certificate bundle")


def distrust_roots(
    *,
    config: Path = Path("/etc/ca-certificates.conf"),
    certificates: Path = Path("/usr/share/ca-certificates"),
    local_certificates: Path = Path("/usr/local/share/ca-certificates"),
    output: Path = Path("/etc/ssl/certs"),
    hooks: Path = Path("/etc/ca-certificates/update.d"),
) -> None:
    blocked = {root["sha256"] for root in json.loads(ROOTS_FILE.read_text())}
    # Local certificates bypass ca-certificates.conf. Refuse rather than delete
    # an operator's files or leave a second copy trusted after reporting success.
    for path in local_certificates.rglob("*.crt"):
        if certificate_fingerprints(path) & blocked:
            raise RuntimeError(f"Remove the distrusted local certificate first: {path}")

    lines = config.read_text().splitlines()
    for index, line in enumerate(lines):
        entry = line.strip()
        if not entry or entry.startswith(("#", "!")):
            continue
        path = certificates / entry
        if path.is_file() and certificate_fingerprints(path) & blocked:
            lines[index] = f"!{entry}"
            LOGGER.warning("Disabled distrusted system certificate path=%s", path)
    config.write_text("\n".join(lines) + "\n")
    subprocess.run(
        [
            "update-ca-certificates", "--fresh",
            "--certsconf", str(config), "--certsdir", str(certificates),
            "--localcertsdir", str(local_certificates), "--etccertsdir", str(output),
            "--hooksdir", str(hooks),
        ],
        check=True,
    )
    _verify_store(output, blocked)
    LOGGER.info("System certificate store excludes release-distrusted roots")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    distrust_roots()

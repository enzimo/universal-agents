#!/bin/sh
# Both images consume the same upstream binary and checksum contract.
set -eu

version="${XURL_VERSION:-1.3.1}"
case "$(dpkg --print-architecture)" in
  amd64) arch=x86_64; checksum="${XURL_SHA256_AMD64:-ccfc4b3ca5240565da650ef003b51d7f887eabe0c6e3797802410240289d4feb}" ;;
  arm64) arch=arm64; checksum="${XURL_SHA256_ARM64:-22474398fb55fe2ae242cc1b940dd5d92c0083508d108ec95922d1adacd2bb76}" ;;
  *) echo 'xurl supports only amd64 and arm64 images' >&2; exit 2 ;;
esac
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  "https://github.com/xdevplatform/xurl/releases/download/v${version}/xurl_Linux_${arch}.tar.gz" \
  -o "$work_dir/xurl.tar.gz"
printf '%s  %s\n' "$checksum" "$work_dir/xurl.tar.gz" | sha256sum --check --status
tar -xzf "$work_dir/xurl.tar.gz" -C "$work_dir" --no-same-owner --no-same-permissions xurl LICENSE
install -m 0755 "$work_dir/xurl" /usr/local/bin/xurl
install -D -m 0644 "$work_dir/LICENSE" /usr/local/share/licenses/xurl/LICENSE
xurl --version

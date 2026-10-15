#!/usr/bin/env bash
set -euo pipefail

raw_selection="${INSTALL_CREDENTIAL_CLIS:-}"
normalized_selection="$(printf '%s' "${raw_selection}" | tr '[:upper:]' '[:lower:]' | tr '[];' ',,,')"
normalized_selection="${normalized_selection// /,}"

selected_clis=()

add_cli() {
    local cli="$1"
    local existing
    for existing in "${selected_clis[@]}"; do
        if [[ "${existing}" == "${cli}" ]]; then
            return 0
        fi
    done
    selected_clis+=("${cli}")
}

if [[ -n "${normalized_selection}" ]]; then
    IFS=',' read -r -a requested_clis <<< "${normalized_selection}"
    for requested_cli in "${requested_clis[@]}"; do
        requested_cli="${requested_cli//$'\t'/}"
        requested_cli="${requested_cli//$'\n'/}"
        if [[ -z "${requested_cli}" || "${requested_cli}" == "none" || "${requested_cli}" == "false" || "${requested_cli}" == "no" ]]; then
            continue
        fi
        case "${requested_cli}" in
            all)
                add_cli "bw"
                add_cli "op"
                add_cli "keepassxc"
                add_cli "vault"
                add_cli "sealbox"
                ;;
            bw|bitwarden)
                add_cli "bw"
                ;;
            op|1password|onepassword|1password-cli|onepassword-cli)
                add_cli "op"
                ;;
            keepass|keepassxc|keepassxc-cli)
                add_cli "keepassxc"
                ;;
            vault|hashicorp-vault)
                add_cli "vault"
                ;;
            sealbox|sealbox-cli)
                add_cli "sealbox"
                ;;
            *)
                echo "Unsupported credential CLI in INSTALL_CREDENTIAL_CLIS: ${requested_cli}" >&2
                echo "Supported values: bw, op, keepassxc, vault, sealbox, all, none" >&2
                exit 2
                ;;
        esac
    done
fi

has_cli() {
    local cli="$1"
    local selected
    for selected in "${selected_clis[@]}"; do
        if [[ "${selected}" == "${cli}" ]]; then
            return 0
        fi
    done
    return 1
}

if [[ "${#selected_clis[@]}" -eq 0 ]]; then
    echo "No credential-source CLIs requested by INSTALL_CREDENTIAL_CLIS=${raw_selection:-<empty>}."
    exit 0
fi

echo "Installing credential-source CLIs: ${selected_clis[*]}"

container_arch="$(dpkg --print-architecture)"
default_bw_version="2025.10.0"
effective_bw_version="${BW_VERSION:-${default_bw_version}}"

apt_prereqs=(ca-certificates curl)
if has_cli "bw"; then
    apt_prereqs+=(file unzip)
fi
if has_cli "op" || has_cli "vault"; then
    apt_prereqs+=(gnupg)
fi
if has_cli "vault"; then
    apt_prereqs+=(libcap2-bin)
fi

apt-get update
apt-get install -y --no-install-recommends "${apt_prereqs[@]}"

apt_packages=()
apt_update_required=false

if has_cli "op"; then
    install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d
    curl -fsSL https://downloads.1password.com/linux/keys/1password.asc \
        | gpg --batch --yes --dearmor --output /usr/share/keyrings/1password-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/1password-archive-keyring.gpg] https://downloads.1password.com/linux/debian/$(dpkg --print-architecture) stable main" \
        > /etc/apt/sources.list.d/1password.list
    install -d -m 0755 /etc/debsig/policies/AC2D62742012EA22 /usr/share/debsig/keyrings/AC2D62742012EA22
    curl -fsSL https://downloads.1password.com/linux/debian/debsig/1password.pol \
        -o /etc/debsig/policies/AC2D62742012EA22/1password.pol
    curl -fsSL https://downloads.1password.com/linux/keys/1password.asc \
        | gpg --batch --yes --dearmor --output /usr/share/debsig/keyrings/AC2D62742012EA22/debsig.gpg
    apt_packages+=(1password-cli)
    apt_update_required=true
fi

if has_cli "vault"; then
    install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d
    curl -fsSL https://apt.releases.hashicorp.com/gpg \
        | gpg --batch --yes --dearmor --output /usr/share/keyrings/hashicorp-archive-keyring.gpg
    . /etc/os-release
    vault_release_codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
    if [[ -z "${vault_release_codename}" ]]; then
        echo "Unable to determine OS release codename for HashiCorp apt repository." >&2
        exit 2
    fi
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com ${vault_release_codename} main" \
        > /etc/apt/sources.list.d/hashicorp.list
    apt_packages+=(vault)
    apt_update_required=true
fi

if has_cli "keepassxc"; then
    apt_packages+=(keepassxc)
fi

if [[ "${apt_update_required}" == "true" ]]; then
    apt-get update
fi
if [[ "${#apt_packages[@]}" -gt 0 ]]; then
    apt-get install -y --no-install-recommends "${apt_packages[@]}"
fi

if has_cli "vault" && command -v vault >/dev/null 2>&1 && command -v setcap >/dev/null 2>&1; then
    # HashiCorp's Debian package may set cap_ipc_lock on the binary. Docker
    # build execution can reject file capabilities, so clear them before the
    # smoke check and leave runtime memory-lock behavior to container policy.
    setcap -r "$(command -v vault)" 2>/dev/null || true
fi

if has_cli "bw"; then
    # Prefer pinned official release archives. Bitwarden does not publish a
    # durable standalone Linux arm64 archive for 2025.10.0, so native arm64
    # builds use the official npm package for that pinned CLI version.
    default_bw_linux_amd64_sha256="0544c64d3e9932bb5f2a70e819695ea78186a44ac87a0b1d753e9c55217041d9"
    bw_version="${effective_bw_version}"
    bw_file_url="${BW_FILE_URL:-}"
    bw_file_url_source="BW_FILE_URL"
    bw_arch_specific_url_var=""
    bw_arch_specific_sha256_var=""
    bw_default_url=""
    bw_file_sha256="${BW_FILE_SHA256:-}"
    bw_install_with_npm=false
    case "${container_arch}" in
        amd64)
            bw_arch_specific_url_var="BW_FILE_URL_AMD64"
            bw_arch_specific_sha256_var="BW_FILE_SHA256_AMD64"
            bw_default_url="https://github.com/bitwarden/clients/releases/download/cli-v${bw_version}/bw-linux-${bw_version}.zip"
            if [[ -n "${BW_FILE_URL_AMD64:-}" ]]; then
                bw_file_url="${BW_FILE_URL_AMD64}"
                bw_file_url_source="BW_FILE_URL_AMD64"
            fi
            if [[ -z "${bw_file_sha256}" && -n "${BW_FILE_SHA256_AMD64:-}" ]]; then
                bw_file_sha256="${BW_FILE_SHA256_AMD64}"
            fi
            if [[ -z "${bw_file_sha256}" && -z "${BW_FILE_URL:-}" && -z "${BW_FILE_URL_AMD64:-}" && "${bw_version}" == "${default_bw_version}" ]]; then
                bw_file_sha256="${default_bw_linux_amd64_sha256}"
            fi
            ;;
        arm64)
            bw_arch_specific_url_var="BW_FILE_URL_ARM64"
            bw_arch_specific_sha256_var="BW_FILE_SHA256_ARM64"
            if [[ "${bw_version}" != "${default_bw_version}" ]]; then
                bw_default_url="https://github.com/bitwarden/clients/releases/download/cli-v${bw_version}/bw-linux-arm64-${bw_version}.zip"
            fi
            if [[ -n "${BW_FILE_URL_ARM64:-}" ]]; then
                bw_file_url="${BW_FILE_URL_ARM64}"
                bw_file_url_source="BW_FILE_URL_ARM64"
            fi
            if [[ -z "${bw_file_sha256}" && -n "${BW_FILE_SHA256_ARM64:-}" ]]; then
                bw_file_sha256="${BW_FILE_SHA256_ARM64}"
            fi
            if [[ -z "${bw_file_url}" && "${bw_version}" == "${default_bw_version}" ]]; then
                bw_install_with_npm=true
            fi
            ;;
        *)
            echo "Unsupported Debian architecture for bw artifact validation: ${container_arch}" >&2
            echo "Supported architectures: amd64, arm64" >&2
            exit 2
            ;;
    esac
    if [[ -z "${bw_file_url}" && -n "${bw_default_url}" ]]; then
        bw_file_url="${bw_default_url}"
        bw_file_url_source="BW_VERSION ${bw_version} default"
    fi
    if [[ "${bw_install_with_npm}" == "true" ]]; then
        if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
            echo "Bitwarden CLI ${bw_version} on Debian arm64 requires npm because no durable official arm64 zip is available for this version." >&2
            echo "Rebuild with --nodejs or --build-arg INSTALL_NODEJS=true, or provide BW_FILE_URL_ARM64 for a matching Bitwarden CLI archive." >&2
            exit 2
        fi
        npm install -g --omit=dev --no-audit --no-fund "@bitwarden/cli@${bw_version}"
    elif [[ -z "${bw_file_url}" ]]; then
        echo "BW_FILE_URL or ${bw_arch_specific_url_var} is required when INSTALL_CREDENTIAL_CLIS includes bw." >&2
        exit 2
    else
        cookie_file="$(mktemp)"
        curl -fsSL -c "${cookie_file}" -b "${cookie_file}" "${bw_file_url}" -o /tmp/bw.zip
        if [[ -n "${bw_file_sha256}" ]]; then
            printf '%s  /tmp/bw.zip\n' "${bw_file_sha256}" | sha256sum -c -
        else
            echo "Warning: no SHA-256 checksum configured for bw artifact from ${bw_file_url_source}; set BW_FILE_SHA256 or ${bw_arch_specific_sha256_var} to pin it." >&2
        fi
        unzip -j /tmp/bw.zip bw -d /usr/local/bin
        chmod +x /usr/local/bin/bw
        rm -f /tmp/bw.zip "${cookie_file}"
        bw_file_info="$(file -b /usr/local/bin/bw)"
        case "${container_arch}:${bw_file_info}" in
            amd64:*x86-64*|amd64:*x86_64*|arm64:*aarch64*|arm64:*ARM64*)
                ;;
            *)
                echo "Downloaded bw artifact from ${bw_file_url_source} is incompatible with Debian architecture ${container_arch}." >&2
                echo "file reported: ${bw_file_info}" >&2
                echo "Provide ${bw_arch_specific_url_var} for this image architecture, set BW_FILE_URL to a matching artifact, or build an amd64 image when using an amd64 bw artifact." >&2
                exit 2
                ;;
        esac
    fi
fi

if has_cli "bw"; then bw --version; fi
if has_cli "op"; then op --version; fi
if has_cli "keepassxc"; then keepassxc-cli --version; fi
if has_cli "vault"; then vault version; fi
if has_cli "sealbox"; then echo "Sealbox CLI/server selected; Dockerfile installs them in the shared Rust layer."; fi

rm -rf /var/lib/apt/lists/*

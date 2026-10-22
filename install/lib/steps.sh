#!/usr/bin/env bash

# Installation steps for install.sh, one function per runbook section. Every
# function reads only node.env keys (sourced by the installer) and the
# installer's own paths; none prompts, and none starts a service. Steps are
# idempotent: a rerun verifies what exists and creates only what is missing.

SERVICE_USER=seismic
SERVICE_GROUP=seismic
SERVICE_HOME=/var/lib/seismic
CONFIG_DIR=/etc/seismic
LIBEXEC_DIR=/usr/local/libexec/seismic
UNIT_DIR=/etc/systemd/system

# --- helpers ----------------------------------------------------------------

# Create or verify a directory with fixed ownership and mode. Refuses to
# adopt a symlink or a non-directory at the path.
ensure_directory() {
    local path=$1 owner=$2 mode=$3 description=$4
    [[ ! -L "$path" ]] || die "$description must not be a symbolic link: $path"
    [[ ! -e "$path" || -d "$path" ]] || die "$description is not a directory: $path"
    if [[ -d "$path" ]]; then
        info "Verifying $description: $path"
    else
        info "Creating $description: $path"
    fi
    install -d -o "${owner%%:*}" -g "${owner##*:}" -m "$mode" -- "$path" \
        || die "Could not prepare $description: $path"
}

# Install a file only when its content differs, so reruns report no change.
# Prints "installed", "updated", or "unchanged" through info/success.
install_file() {
    local source=$1 target=$2 mode=$3 description=$4
    [[ -f "$source" ]] || die "$description source is missing: $source"
    [[ ! -L "$target" ]] || die "$description target must not be a symbolic link: $target"
    if [[ -f "$target" ]] && cmp -s -- "$source" "$target"; then
        chmod "$mode" -- "$target"
        chown root:root -- "$target"
        success "$description unchanged: $target"
        return
    fi
    local verb=installed
    [[ ! -e "$target" ]] || verb=updated
    install -o root -g root -m "$mode" -- "$source" "$target" \
        || die "Could not install $description: $target"
    success "$description $verb: $target"
}

needs_download() {
    [[ "$SUMMIT_INSTALL_METHOD" == download || "$RETH_INSTALL_METHOD" == download ||
        "$CUSTODIAN_INSTALL_METHOD" == download ||
        ("$CHECKPOINTER_ENABLED" == true && "$CHECKPOINTER_INSTALL_METHOD" == download) ]]
}

needs_source_build() {
    [[ "$SUMMIT_INSTALL_METHOD" == source || "$RETH_INSTALL_METHOD" == source ||
        "$CUSTODIAN_INSTALL_METHOD" == source ||
        ("$CHECKPOINTER_ENABLED" == true && "$CHECKPOINTER_INSTALL_METHOD" == source) ]]
}

tls_managed() {
    [[ "$TLS_MODE" == custodian-only || "$TLS_MODE" == full ]]
}

# --- 3.1 packages -------------------------------------------------------------

collect_packages() {
    PACKAGES=(python3 curl jq ca-certificates openssl)
    # gh verifies release attestations in download mode; no login is needed.
    needs_download && PACKAGES+=(gh)
    if needs_source_build; then
        PACKAGES+=(git build-essential clang pkg-config libssl-dev)
    elif tls_managed; then
        # lua-resty-auto-ssl builds its sockproc helper with make and a C compiler.
        PACKAGES+=(git build-essential)
    fi
    tls_managed && PACKAGES+=(gnupg lsb-release luarocks)
    return 0
}

# Ubuntu 24.04 packages gh 2.45, which predates `gh attestation` (2.49).
# Download mode needs GitHub's own apt repository. Its keyring ships every
# signing key GitHub has used (older ones expired); the current one is pinned
# here by fingerprint and must be present.
GH_APT_KEY_URL=https://cli.github.com/packages/githubcli-archive-keyring.gpg
GH_APT_KEY_FINGERPRINT=7F38BBB59D064DBCB3D84D725612B36462313325 # rsa4096 2026-04-07
GH_APT_KEYRING=/etc/apt/keyrings/githubcli-archive-keyring.gpg
GH_APT_LIST=/etc/apt/sources.list.d/github-cli.list

add_github_cli_repository() {
    local staging fingerprint
    ensure_directory /etc/apt/keyrings root:root 0755 "apt keyring directory"
    staging=$(mktemp)
    curl -fsSL --retry 3 -o "$staging" "$GH_APT_KEY_URL" \
        || die "Could not download the GitHub CLI apt signing key"
    fingerprint=$(gpg --show-keys --with-colons "$staging" 2>/dev/null | awk -F: '/^fpr:/ {print $10}' | paste -sd' ')
    [[ " $fingerprint " == *" $GH_APT_KEY_FINGERPRINT "* ]] \
        || die "GitHub CLI apt keyring does not contain the pinned key $GH_APT_KEY_FINGERPRINT (found: ${fingerprint:-none})"
    install_file "$staging" "$GH_APT_KEYRING" 0644 "GitHub CLI apt keyring"
    rm -f -- "$staging"
    printf 'deb [arch=%s signed-by=%s] https://cli.github.com/packages stable main\n' \
        "$(dpkg --print-architecture)" "$GH_APT_KEYRING" >"$staging"
    install_file "$staging" "$GH_APT_LIST" 0644 "GitHub CLI apt source"
    rm -f -- "$staging"
}

step_packages() {
    collect_packages
    _out "Packages: ${PACKAGES[*]}"
    export DEBIAN_FRONTEND=noninteractive
    if needs_download; then
        # gpg is needed to check the key fingerprint before trusting the repo.
        command -v gpg >/dev/null || apt-get install -y -qq gnupg >>"$LOG_FILE" 2>&1 || die "Could not install gnupg; see $LOG_FILE"
        add_github_cli_repository
    fi
    info "Updating apt package metadata..."
    apt-get update >>"$LOG_FILE" 2>&1 || die "apt-get update failed; see $LOG_FILE"
    info "Installing packages..."
    apt-get install -y -- "${PACKAGES[@]}" >>"$LOG_FILE" 2>&1 \
        || die "Package installation failed; see $LOG_FILE"
    if needs_download; then
        gh attestation --help >/dev/null 2>&1 \
            || die "Installed gh ($(gh --version | head -1)) lacks 'gh attestation'; the GitHub CLI apt repository did not take effect"
    fi
    success "Packages installed"
}

# --- 3.2 account --------------------------------------------------------------

step_account() {
    if getent passwd "$SERVICE_USER" >/dev/null; then
        success "Service account exists: $SERVICE_USER (uid $(id -u "$SERVICE_USER"))"
    else
        info "Creating service account: $SERVICE_USER"
        useradd --system --home "$SERVICE_HOME" --shell /usr/sbin/nologin \
            --user-group "$SERVICE_USER" >>"$LOG_FILE" 2>&1 \
            || die "Could not create the $SERVICE_USER account; see $LOG_FILE"
        success "Service account created: $SERVICE_USER"
    fi
    [[ "$(id -u "$SERVICE_USER")" != 0 ]] || die "$SERVICE_USER must not be root"
    getent group "$SERVICE_GROUP" >/dev/null || die "Group $SERVICE_GROUP is missing"
}

# --- 3.3 directories ----------------------------------------------------------

step_directories() {
    local svc="$SERVICE_USER:$SERVICE_GROUP"
    ensure_directory "$CONFIG_DIR" root:root 0755 "configuration directory"
    ensure_directory "$LIBEXEC_DIR" root:root 0755 "libexec directory"
    ensure_directory "$SERVICE_HOME" "$svc" 0750 "service home"
    ensure_directory "$RETH_DATA_DIR" "$svc" 0750 "Reth data directory"
    ensure_directory "$SUMMIT_DATA_DIR" "$svc" 0750 "Summit data directory"
    ensure_directory "$SUMMIT_CRITICAL_LOG_DIR" "$svc" 0750 "Summit critical log directory"
    ensure_directory "$KEYS_DIR" "$svc" 0700 "keys directory"
    ensure_directory "$(dirname "$RETH_P2P_KEY_PATH")" "$svc" 0700 "Reth key directory"
    ensure_directory "$SUMMIT_KEYS_DIR" "$svc" 0700 "Summit key directory"
    ensure_directory "$CUSTODIAN_DATA_DIR" "$svc" 0700 "Custodian data directory"
    ensure_directory "$CUSTODIAN_DATA_DIR/deliveries" "$svc" 0700 "Custodian deliveries directory"
    if [[ "$CHECKPOINTER_ENABLED" == true ]]; then
        ensure_directory "$CHECKPOINTS_DIR" "$svc" 0750 "checkpointer output directory"
    fi
    success "Directories ready; no keys or data were created."
}

# --- 3.6 config ---------------------------------------------------------------

step_config() {
    install_file "$CONFIG" "$NODE_ENV_PATH" 0640 "node.env"
    # The genesis file is identical on every host; GENESIS_PATH names where
    # the units read it. Install the repository copy there unless the operator
    # already placed one.
    if [[ "$GENESIS_PATH" == "$CONFIG_DIR/genesis.toml" ]]; then
        install_file "$REPO_ROOT/internal_testnet_genesis.toml" "$GENESIS_PATH" 0644 "genesis file"
    else
        [[ -s "$GENESIS_PATH" && ! -L "$GENESIS_PATH" ]] \
            || die "GENESIS_PATH is not an existing regular file: $GENESIS_PATH"
        success "Genesis file supplied by operator: $GENESIS_PATH"
    fi
    # Re-validate the installed copy with ownership checks on, as the CLI will.
    (cd "$TOOLS_DIR" && /usr/bin/python3 -m seismic_node.nodeenv --check "$NODE_ENV_PATH" >>"$LOG_FILE" 2>&1) \
        || die "Installed $NODE_ENV_PATH failed validation; see $LOG_FILE"
}

# --- 3.10 units ---------------------------------------------------------------

step_units() {
    local unit name changed=false
    for unit in "$TEMPLATES_DIR"/systemd/seismic-*.service; do
        name=$(basename "$unit")
        if ! cmp -s -- "$unit" "$UNIT_DIR/$name" 2>/dev/null; then changed=true; fi
        install_file "$unit" "$UNIT_DIR/$name" 0644 "unit $name"
        ensure_directory "$UNIT_DIR/$name.d" root:root 0755 "drop-in directory for $name"
        if ! cmp -s -- "$TEMPLATES_DIR/systemd/seismic-hardening.conf" "$UNIT_DIR/$name.d/10-hardening.conf" 2>/dev/null; then changed=true; fi
        install_file "$TEMPLATES_DIR/systemd/seismic-hardening.conf" \
            "$UNIT_DIR/$name.d/10-hardening.conf" 0644 "hardening drop-in for $name"
    done
    # Units are static; host-specific values come from node.env at start.
    # None is enabled: first start goes through seismic-node, and the
    # runbook (section 4) explains enabling at boot.
    systemctl daemon-reload >>"$LOG_FILE" 2>&1 || die "systemctl daemon-reload failed; see $LOG_FILE"
    if [[ "$changed" == true ]]; then
        success "Units installed and systemd reloaded; nothing is enabled or started."
    else
        success "Units unchanged; systemd reloaded."
    fi
    systemd-analyze verify "$UNIT_DIR"/seismic-*.service >>"$LOG_FILE" 2>&1 \
        || warn "systemd-analyze verify reported issues; see $LOG_FILE (missing binaries are expected before step 'binaries')."
}

# --- 3.4 binaries ------------------------------------------------------------
#
# Component table: name, GitHub repository, workspace crate/bin build command,
# binaries in the release tarball. The install method and version come from
# the matching <NAME>_INSTALL_METHOD / <NAME>_VERSION keys.

SUMMIT_REPO=summit
RETH_REPO=seismic-reth
CUSTODIAN_REPO=enclave
CHECKPOINTER_REPO=seismic-checkpointer

SUMMIT_BIN=summit
RETH_BIN=seismic-reth
CUSTODIAN_BIN=seismic-centralized-custodian-service
CHECKPOINTER_BIN=seismic-checkpointer
MDBX_COPY_BIN=mdbx_copy

# Mirrors each repository's release workflow so a source build yields the
# same binary the release ships.
SUMMIT_BUILD='cargo build --release --locked --bin summit --features prom'
RETH_BUILD='cargo build --locked --profile maxperf --bin seismic-reth --features jemalloc,asm-keccak'
CUSTODIAN_BUILD='cargo build --release --locked -p seismic-centralized-custodian-service'
CHECKPOINTER_BUILD='cargo build --release --locked'

RETH_BUILD_OUTPUT=target/maxperf
DEFAULT_BUILD_OUTPUT=target/release

GITHUB_OWNER=SeismicSystems
DOWNLOAD_DIR="$SERVICE_HOME/downloads"

# Download the release tarball, bundle, and SHA256SUMS for one component,
# verify, and install the named binaries. The attestation bundle makes the
# verification possible without a GitHub login.
download_component() {
    local name=$1 repo=$2 version=$3
    shift 3
    local bins=("$@")
    local asset="${bins[0]}_${version}_linux_amd64"
    local base="https://github.com/$GITHUB_OWNER/$repo/releases/download/$version"
    local dir="$DOWNLOAD_DIR/$repo-$version"

    command -v gh >/dev/null || die "gh (GitHub CLI) is required for download mode; step 'packages' installs it."
    ensure_directory "$DOWNLOAD_DIR" root:root 0700 "download directory"
    rm -rf -- "$dir"
    install -d -o root -g root -m 0700 -- "$dir"

    info "Downloading $name $version from $base"
    local file
    for file in "$asset.tar.gz" "$asset.sigstore.json" SHA256SUMS; do
        curl -fsSL --retry 3 -o "$dir/$file" "$base/$file" \
            || die "Could not download $file for $name $version (no release for this version?)"
    done
    (cd "$dir" && sha256sum --quiet --ignore-missing -c SHA256SUMS >>"$LOG_FILE" 2>&1) \
        || die "$name $version: SHA256SUMS mismatch; see $LOG_FILE"
    tar -xzf "$dir/$asset.tar.gz" -C "$dir" -- "${bins[@]}" \
        || die "$name $version: tarball does not contain ${bins[*]}"

    local bin
    for bin in "${bins[@]}"; do
        # --bundle verifies against the downloaded attestation; no login needed.
        gh attestation verify "$dir/$bin" --owner "$GITHUB_OWNER" --bundle "$dir/$asset.sigstore.json" \
            >>"$LOG_FILE" 2>&1 \
            || die "$name $version: build-provenance attestation failed for $bin; see $LOG_FILE"
        success "$name $version: attestation verified for $bin"
        install_binary_target "$name" "$dir/$bin" "/usr/local/bin/$bin"
    done
    rm -rf -- "$dir"
}

# Build one component from its repository at a tag or branch ref.
build_component() {
    local name=$1 repo=$2 version=$3 build=$4 output_dir=$5
    shift 5
    local bins=("$@")
    local source_dir="$SERVICE_HOME/src/$repo"
    local ref=$version
    [[ "$ref" == refs/* ]] || ref="refs/tags/$ref"

    install_rust_for_service_user
    prepare_source_checkout "$name" "https://github.com/$GITHUB_OWNER/$repo.git" "$source_dir" "$ref"
    info "Building $name from $ref ($build)..."
    # shellcheck disable=SC2016
    run_as_service_user bash -c 'source "$HOME/.cargo/env"; cd "$1"; eval "$2"' _ "$source_dir" "$build" \
        >>"$LOG_FILE" 2>&1 || die "$name build failed; see $LOG_FILE"
    local bin
    for bin in "${bins[@]}"; do
        install_binary_target "$name" "$source_dir/$output_dir/$bin" "/usr/local/bin/$bin"
    done
}

# Dispatch on <NAME>_INSTALL_METHOD for one component.
install_component() {
    local name=$1 key=$2 repo=$3 build=$4 output_dir=$5
    shift 5
    local bins=("$@")
    local method_var="${key}_INSTALL_METHOD" version_var="${key}_VERSION" prebuilt_var="${key}_PREBUILT_PATH"
    local method=${!method_var} version=${!version_var}
    case "$method" in
        download) download_component "$name" "$repo" "$version" "${bins[@]}" ;;
        source) build_component "$name" "$repo" "$version" "$build" "$output_dir" "${bins[@]}" ;;
        prebuilt)
            [[ "${#bins[@]}" == 1 ]] || die "$name: prebuilt supports a single binary; install ${bins[*]:1} by hand"
            install_binary_target "$name" "${!prebuilt_var}" "/usr/local/bin/${bins[0]}"
            ;;
        deferred) warn "$name: deferred; place the executable at /usr/local/bin/${bins[0]} before starting" ;;
        *) die "$name: unknown install method $method" ;;
    esac
}

step_binaries() {
    install_component Summit SUMMIT "$SUMMIT_REPO" "$SUMMIT_BUILD" "$DEFAULT_BUILD_OUTPUT" "$SUMMIT_BIN"
    if [[ "$CHECKPOINTER_ENABLED" == true ]]; then
        # mdbx_copy must match Reth's vendored libmdbx; it ships in Reth's
        # tarball and builds from Reth's checkout.
        if [[ "$RETH_INSTALL_METHOD" == source ]]; then
            install_component Reth RETH "$RETH_REPO" "$RETH_BUILD" "$RETH_BUILD_OUTPUT" "$RETH_BIN"
            build_mdbx_copy
        else
            install_component Reth RETH "$RETH_REPO" "$RETH_BUILD" "$RETH_BUILD_OUTPUT" "$RETH_BIN" "$MDBX_COPY_BIN"
        fi
        install_component Checkpointer CHECKPOINTER "$CHECKPOINTER_REPO" "$CHECKPOINTER_BUILD" "$DEFAULT_BUILD_OUTPUT" "$CHECKPOINTER_BIN"
    else
        install_component Reth RETH "$RETH_REPO" "$RETH_BUILD" "$RETH_BUILD_OUTPUT" "$RETH_BIN"
    fi
    install_component Custodian CUSTODIAN "$CUSTODIAN_REPO" "$CUSTODIAN_BUILD" "$DEFAULT_BUILD_OUTPUT" "$CUSTODIAN_BIN"
    success "Binaries installed."
}

build_mdbx_copy() {
    local libmdbx_dir="$SERVICE_HOME/src/$RETH_REPO/crates/storage/libmdbx-rs/mdbx-sys/libmdbx"
    [[ -f "$libmdbx_dir/GNUmakefile" ]] || die "Reth's vendored libmdbx is missing at $libmdbx_dir"
    info "Building mdbx_copy from Reth's vendored libmdbx..."
    run_as_service_user make -C "$libmdbx_dir" mdbx_copy >>"$LOG_FILE" 2>&1 \
        || die "mdbx_copy build failed; see $LOG_FILE"
    install_binary_target mdbx_copy "$libmdbx_dir/mdbx_copy" "/usr/local/bin/$MDBX_COPY_BIN"
}

# --- 3.5 runner -------------------------------------------------------------

step_runner() {
    install_file "$TOOLS_DIR/checkpoint-start/summit-checkpoint-runner.py" \
        "$LIBEXEC_DIR/summit-checkpoint-runner" 0755 "checkpoint runner"
}

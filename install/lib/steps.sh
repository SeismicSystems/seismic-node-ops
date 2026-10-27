#!/usr/bin/env bash

# Installation steps for install.sh, one function per runbook section. Every
# function reads only node.env keys (exported by the Python parser) and the
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
    PACKAGES=(python3 curl jq ca-certificates openssl gettext-base)
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
        # Adding GitHub's repository needs curl for its key and gpg to check
        # the fingerprint before trusting it. Neither is guaranteed on a
        # minimal image, so install them from Ubuntu's own repositories
        # first, with an index refresh so that works on a fresh host too.
        info "Updating apt package metadata..."
        apt-get update >>"$LOG_FILE" 2>&1 || die "apt-get update failed; see $LOG_FILE"
        info "Installing bootstrap packages for the GitHub CLI repository..."
        apt-get install -y -- curl ca-certificates gnupg >>"$LOG_FILE" 2>&1 \
            || die "Could not install curl, ca-certificates and gnupg; see $LOG_FILE"
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
    # the units read it. At the default path the repository copy is installed
    # and kept current; an operator-supplied file lives at a different
    # GENESIS_PATH and is only checked to exist.
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
        || warn "systemd-analyze verify reported issues; see $LOG_FILE (for a partial --only run, install binaries first; disabled optional services may lack binaries)."
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
    # These are the expected release workflows, not identities taken from the
    # downloaded bundle. Refuse unknown repositories rather than weaken policy.
    local workflow
    case "$repo" in
        seismic-reth) workflow=seismic-release.yml ;;
        summit | enclave | seismic-checkpointer) workflow=release.yml ;;
        *) die "No trusted release workflow configured for $GITHUB_OWNER/$repo" ;;
    esac
    local identity="https://github.com/$GITHUB_OWNER/$repo/.github/workflows/$workflow@refs/tags/$version"

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
        # Require the component repository AND the exact release workflow/tag
        # certificate identity. --bundle needs no GitHub login.
        gh attestation verify "$dir/$bin" --repo "$GITHUB_OWNER/$repo" \
            --cert-identity "$identity" --bundle "$dir/$asset.sigstore.json" \
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
            # Only Reth carries a second binary (mdbx_copy); step_binaries
            # routes that through install_prebuilt_mdbx_copy.
            [[ "${#bins[@]}" == 1 ]] || die "$name: prebuilt supports a single binary; cannot install ${bins[*]:1}"
            check_prebuilt_executable "$name" "${!prebuilt_var}"
            install_binary_target "$name" "${!prebuilt_var}" "/usr/local/bin/${bins[0]}"
            ;;
        *) die "$name: unknown install method $method" ;;
    esac
}

step_binaries() {
    install_component Summit SUMMIT "$SUMMIT_REPO" "$SUMMIT_BUILD" "$DEFAULT_BUILD_OUTPUT" "$SUMMIT_BIN"
    if [[ "$CHECKPOINTER_ENABLED" == true ]]; then
        # mdbx_copy must match Reth's vendored libmdbx: it ships in Reth's
        # tarball, builds from Reth's checkout, and for prebuilt binaries the
        # operator supplies it and the installer checks the libmdbx versions.
        case "$RETH_INSTALL_METHOD" in
            source)
                install_component Reth RETH "$RETH_REPO" "$RETH_BUILD" "$RETH_BUILD_OUTPUT" "$RETH_BIN"
                build_mdbx_copy
                ;;
            prebuilt)
                check_prebuilt_mdbx_copy "$RETH_PREBUILT_PATH" "$RETH_MDBX_COPY_PREBUILT_PATH"
                install_component Reth RETH "$RETH_REPO" "$RETH_BUILD" "$RETH_BUILD_OUTPUT" "$RETH_BIN"
                install_binary_target mdbx_copy "$RETH_MDBX_COPY_PREBUILT_PATH" "/usr/local/bin/$MDBX_COPY_BIN"
                ;;
            *)
                install_component Reth RETH "$RETH_REPO" "$RETH_BUILD" "$RETH_BUILD_OUTPUT" "$RETH_BIN" "$MDBX_COPY_BIN"
                ;;
        esac
        install_component Checkpointer CHECKPOINTER "$CHECKPOINTER_REPO" "$CHECKPOINTER_BUILD" "$DEFAULT_BUILD_OUTPUT" "$CHECKPOINTER_BIN"
    else
        install_component Reth RETH "$RETH_REPO" "$RETH_BUILD" "$RETH_BUILD_OUTPUT" "$RETH_BIN"
    fi
    install_component Custodian CUSTODIAN "$CUSTODIAN_REPO" "$CUSTODIAN_BUILD" "$DEFAULT_BUILD_OUTPUT" "$CUSTODIAN_BIN"
    success "Binaries installed."
}

check_prebuilt_executable() {
    local name=$1 path=$2 reason
    reason=$(check_service_executable_security "$path") \
        || die "$name: $path is not safe to install ($reason)"
}

# A prebuilt mdbx_copy is only usable if it was built from the same libmdbx
# revision the prebuilt Reth links: libmdbx refuses databases written by a
# different version (MDBX_VERSION_MISMATCH) and a near-miss could still read
# the pages differently. Both binaries embed libmdbx's git-describe, so the
# match is checked here before either file is installed.
check_prebuilt_mdbx_copy() {
    local reth_path=$1 mdbx_copy_path=$2 reth_libmdbx mdbx_copy_libmdbx
    warn "Prebuilt Reth with the checkpointer: $mdbx_copy_path must be built from the same seismic-reth checkout (vendored libmdbx) as $reth_path."
    check_prebuilt_executable Reth "$reth_path"
    check_prebuilt_executable mdbx_copy "$mdbx_copy_path"
    reth_libmdbx=$(libmdbx_describe "$reth_path")
    mdbx_copy_libmdbx=$(libmdbx_describe "$mdbx_copy_path")
    [[ -n "$reth_libmdbx" ]] \
        || die "Reth: no libmdbx version found in $reth_path; cannot confirm it matches $mdbx_copy_path"
    [[ -n "$mdbx_copy_libmdbx" ]] \
        || die "mdbx_copy: no libmdbx version found in $mdbx_copy_path; is it libmdbx's mdbx_copy?"
    [[ "$reth_libmdbx" != *$'\n'* ]] \
        || die "Reth: $reth_path carries several libmdbx versions (${reth_libmdbx//$'\n'/, }); cannot confirm which one mdbx_copy must match"
    [[ "$mdbx_copy_libmdbx" != *$'\n'* ]] \
        || die "mdbx_copy: $mdbx_copy_path carries several libmdbx versions (${mdbx_copy_libmdbx//$'\n'/, })"
    [[ "$reth_libmdbx" == "$mdbx_copy_libmdbx" ]] \
        || die "mdbx_copy: $mdbx_copy_path is built from libmdbx $mdbx_copy_libmdbx but $reth_path links libmdbx $reth_libmdbx; build mdbx_copy from the same seismic-reth checkout (make -C crates/storage/libmdbx-rs/mdbx-sys/libmdbx mdbx_copy)"
    info "Prebuilt Reth and mdbx_copy both link libmdbx $reth_libmdbx."
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

# --- 3.7 keys ---------------------------------------------------------------
#
# Existing keys are never regenerated or replaced. A complete key set is
# adopted (re-owned to the service account, mode 0600); an incomplete set
# stops the installer, because generating half a pair would silently change
# the node's identity.

key_present() { [[ -e "$1" || -L "$1" ]]; }

validate_reth_p2p_key() {
    local path=$1
    local value

    [[ ! -L "$path" ]] \
        || die "Reth P2P key must not be a symbolic link: $path"
    [[ -f "$path" ]] \
        || die "Reth P2P key path exists but is not a regular file: $path"
    [[ $(stat -c %s -- "$path") == "64" ]] \
        || die "Reth P2P key must contain exactly 64 hexadecimal characters: $path"
    value=$(<"$path")
    [[ "$value" =~ ^[0-9a-fA-F]{64}$ ]] \
        || die "Reth P2P key contains invalid data: $path"
}

adopt_key_file() {
    local path=$1 description=$2
    [[ ! -L "$path" ]] || die "$description must not be a symbolic link: $path"
    [[ -f "$path" && -s "$path" ]] || die "$description is missing or empty: $path"
    chown "$SERVICE_USER:$SERVICE_GROUP" -- "$path"
    chmod 0600 -- "$path"
}

setup_reth_p2p_key() {
    local staging
    if key_present "$RETH_P2P_KEY_PATH"; then
        validate_reth_p2p_key "$RETH_P2P_KEY_PATH"
        adopt_key_file "$RETH_P2P_KEY_PATH" "Reth P2P key"
        success "Reth P2P key preserved: $RETH_P2P_KEY_PATH"
        return
    fi
    staging=$(mktemp "$(dirname "$RETH_P2P_KEY_PATH")/.p2p-key.XXXXXX")
    chmod 0600 -- "$staging"
    openssl rand -hex 32 | tr -d '\n' >"$staging" || {
        rm -f -- "$staging"
        die "Could not generate the Reth P2P key"
    }
    validate_reth_p2p_key "$staging"
    chown "$SERVICE_USER:$SERVICE_GROUP" -- "$staging"
    mv -- "$staging" "$RETH_P2P_KEY_PATH"
    success "Reth P2P key generated: $RETH_P2P_KEY_PATH"
}

summit_keys_show_public_key() {
    run_as_service_user "/usr/local/bin/$SUMMIT_BIN" keys show --key-store-path "$SUMMIT_KEYS_DIR" 2>>"$LOG_FILE" \
        | awk -F': ' '/Node Public Key \(ed25519\)/ {print tolower($2); exit}' | sed 's/^0x//'
}

# Generate into a private staging store and move only the wanted files.
summit_generate_into() {
    local staging=$1
    [[ -x "/usr/local/bin/$SUMMIT_BIN" ]] \
        || die "Summit is not installed at /usr/local/bin/$SUMMIT_BIN; run step 'binaries' first"
    run_as_service_user "/usr/local/bin/$SUMMIT_BIN" keys generate --key-store-path "$staging" -y \
        >>"$LOG_FILE" 2>&1 || die "Summit key generation failed; see $LOG_FILE"
}

setup_validator_summit_keys() {
    local node_key="$SUMMIT_KEYS_DIR/node_key.pem" consensus_key="$SUMMIT_KEYS_DIR/consensus_key.pem"
    local staging
    if key_present "$node_key" && key_present "$consensus_key"; then
        adopt_key_file "$node_key" "Summit node key"
        adopt_key_file "$consensus_key" "Summit consensus key"
        success "Summit validator keys preserved: $SUMMIT_KEYS_DIR"
        return
    fi
    if key_present "$node_key" || key_present "$consensus_key"; then
        die "Summit key set is incomplete in $SUMMIT_KEYS_DIR; refusing to regenerate half of a validator identity"
    fi
    staging=$(mktemp -d "$KEYS_DIR/.summit-keys.XXXXXX")
    chown "$SERVICE_USER:$SERVICE_GROUP" -- "$staging"
    chmod 0700 -- "$staging"
    info "Generating Summit validator keys..."
    summit_generate_into "$staging"
    [[ -s "$staging/node_key.pem" && -s "$staging/consensus_key.pem" ]] \
        || {
            rm -rf -- "$staging"
            die "Summit did not produce a complete key set"
        }
    install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 -- "$staging/node_key.pem" "$node_key"
    install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 -- "$staging/consensus_key.pem" "$consensus_key"
    rm -rf -- "$staging"
    success "Summit validator keys generated: $SUMMIT_KEYS_DIR"
}

# An observer uses the parent validator's node_key.pem (provisioned out of
# band) and its own consensus key. The assignment marker records which
# parent and index the keys belong to; the CLI checks it on every start.
setup_observer_summit_keys() {
    local node_key="$SUMMIT_KEYS_DIR/node_key.pem" consensus_key="$SUMMIT_KEYS_DIR/consensus_key.pem"
    local assignment="$SUMMIT_KEYS_DIR/observer-assignment"
    local expected="$OBSERVER_PARENT_NODE_PUBLIC_KEY:$OBSERVER_INDEX"
    local staging existing

    if key_present "$assignment"; then
        [[ ! -L "$assignment" && -f "$assignment" ]] || die "Observer assignment must be a regular file: $assignment"
        existing=$(<"$assignment")
        [[ "$existing" == "$expected" ]] \
            || die "Keys in $SUMMIT_KEYS_DIR are assigned to $existing, not $expected; use a different KEYS_DIR"
    elif key_present "$node_key" || key_present "$consensus_key"; then
        die "Summit keys exist in $SUMMIT_KEYS_DIR without an observer assignment; refusing to reuse or replace them"
    fi

    if key_present "$consensus_key"; then
        adopt_key_file "$consensus_key" "observer consensus key"
        success "Observer consensus key preserved: $consensus_key"
    else
        staging=$(mktemp -d "$KEYS_DIR/.observer-keys.XXXXXX")
        chown "$SERVICE_USER:$SERVICE_GROUP" -- "$staging"
        chmod 0700 -- "$staging"
        info "Generating observer consensus key..."
        summit_generate_into "$staging"
        [[ -s "$staging/consensus_key.pem" ]] || {
            rm -rf -- "$staging"
            die "Summit did not produce a consensus key"
        }
        install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 -- "$staging/consensus_key.pem" "$consensus_key"
        rm -rf -- "$staging"
        success "Observer consensus key generated: $consensus_key"
    fi

    if key_present "$node_key"; then
        adopt_key_file "$node_key" "provisioned parent node key"
        local public_key
        public_key=$(summit_keys_show_public_key)
        [[ "$public_key" == "$OBSERVER_PARENT_NODE_PUBLIC_KEY" ]] \
            || die "Provisioned node_key.pem has public key ${public_key:-unreadable}, expected OBSERVER_PARENT_NODE_PUBLIC_KEY=$OBSERVER_PARENT_NODE_PUBLIC_KEY"
        success "Parent node key matches OBSERVER_PARENT_NODE_PUBLIC_KEY"
    else
        warn "No parent node key at $node_key yet; install the one provisioned by the parent's operator before starting (runbook 3.7)."
    fi

    printf '%s\n' "$expected" >"$assignment.tmp"
    chown "$SERVICE_USER:$SERVICE_GROUP" -- "$assignment.tmp"
    chmod 0600 -- "$assignment.tmp"
    mv -- "$assignment.tmp" "$assignment"
}

setup_bootstrappers_file() {
    local target="$SUMMIT_KEYS_DIR/bootstrappers.toml"
    if [[ -z "$SUMMIT_BOOTSTRAPPERS_FILE" ]]; then
        [[ ! -e "$target" ]] || warn "A bootstrappers file exists at $target but SUMMIT_BOOTSTRAPPERS_FILE is empty; Summit will not use it."
        return
    fi
    [[ "$SUMMIT_BOOTSTRAPPERS_FILE" == "$target" ]] \
        || die "SUMMIT_BOOTSTRAPPERS_FILE must be $target (the copy inside SUMMIT_KEYS_DIR); place the file there first"
    [[ -f "$target" && -s "$target" && ! -L "$target" ]] \
        || die "SUMMIT_BOOTSTRAPPERS_FILE names $target but no such regular file exists; copy the bootstrappers TOML there (runbook 3.7)"
    chown "$SERVICE_USER:$SERVICE_GROUP" -- "$target"
    chmod 0640 -- "$target"
    success "Bootstrappers file ready: $target"
}

step_keys() {
    setup_reth_p2p_key
    if [[ "$SEISMIC_ROLE" == validator ]]; then
        setup_validator_summit_keys
    else
        setup_observer_summit_keys
    fi
    setup_bootstrappers_file
    # Everything under KEYS_DIR belongs to the service account.
    chown -R "$SERVICE_USER:$SERVICE_GROUP" -- "$KEYS_DIR"
    success "Keys ready. Back up $KEYS_DIR securely; it is the node's identity."
}

# --- 3.8 checkpointer config -------------------------------------------------

# Render a template by substituting ${KEY} for the listed node.env keys only,
# failing on anything left unresolved.
render_template() {
    local source=$1 target=$2 mode=$3 description=$4
    shift 4
    local vars=("$@") staging unresolved
    staging=$(mktemp)
    if ! envsubst "$(printf '${%s} ' "${vars[@]}")" <"$source" >"$staging"; then
        rm -f -- "$staging"
        die "$description template rendering failed"
    fi
    # Diagnose only placeholders that remain after substitution, including
    # ones introduced by a substituted value, before removing the staging file.
    if grep -q '\${[A-Za-z_][A-Za-z0-9_]*}' "$staging"; then
        unresolved=$(grep -o '\${[A-Za-z_][A-Za-z0-9_]*}' "$staging" | sort -u | paste -sd' ')
        rm -f -- "$staging"
        die "$description template has unresolved variables: $unresolved"
    fi
    install_file "$staging" "$target" "$mode" "$description"
    rm -f -- "$staging"
}

step_checkpointer() {
    if [[ "$CHECKPOINTER_ENABLED" != true ]]; then
        success "Checkpointer disabled; nothing to render."
        return
    fi
    # Derived paths the template needs that are not node.env keys.
    export RETH_DB_PATH="$RETH_DATA_DIR/db"
    export CHECKPOINTER_STATE_FILE="$CHECKPOINTS_DIR/checkpointer-state.cbor"
    render_template "$TEMPLATES_DIR/config/seismic-checkpointer.toml" "$CHECKPOINTER_CONFIG_PATH" 0644 \
        "checkpointer config" RETH_DB_PATH CHECKPOINTS_DIR CHECKPOINTER_STATE_FILE
}

# --- 3.11 tls ---------------------------------------------------------------
#
# lib/openresty.sh predates node.env and reads the installer's old variables.
# This step derives them from TLS_MODE and friends, in one place, and calls
# its install and deploy functions unchanged.
#
#   TLS_MODE        OPENRESTY_MODE  EXPOSE_CUSTODIAN  managed?
#   disabled        disabled        false             no
#   external        external        true              no (operator's proxy)
#   custodian-only  custodian       true              yes: /custodian only
#   full            full            true              yes: /custodian + JWT-protected node routes
#
# MONITORING_MODE=pull adds JWT-protected /prom-* routes to either managed mode.

# lib/openresty.sh calls this before rendering. Exposure consistency is
# guaranteed by the node.env schema and the TLS_MODE mapping above; only the
# loopback listen address remains to check.
validate_custodian_exposure() {
    [[ "$COUNCIL_LISTEN" =~ ^127\.0\.0\.1:[0-9]+$ ]]
}

step_tls() {
    case "$TLS_MODE" in
        disabled)
            success "TLS_MODE=disabled; no public endpoint, OpenResty untouched."
            return
            ;;
        external)
            success "TLS_MODE=external; Custodian is served by the operator's terminator at $CUSTODIAN_PUBLIC_URL (see install/CUSTODIAN_TLS.md)."
            if [[ "${MONITORING_MODE:-off}" == pull ]]; then
                # The operator's proxy must enforce the same JWT check as the
                # managed /prom-* routes; give it the secret the token tool signs with.
                setup_openresty_jwt_secret
                persist_openresty_jwt_secret_path
                warn "MONITORING_MODE=pull: protect the /prom-* routes on your proxy with the JWT secret at $OPENRESTY_JWT_SECRET_PATH (runbook 3.12), then mint Seismic's token with tools/generate-openresty-jwt.sh."
            fi
            return
            ;;
        custodian-only) OPENRESTY_MODE=custodian ;;
        full) OPENRESTY_MODE=full ;;
        *) die "Unknown TLS_MODE: $TLS_MODE" ;;
    esac
    CONFIGURE_PUBLIC_ENDPOINT=true
    EXPOSE_CUSTODIAN=true
    INSTALL_CUSTODIAN=true
    COUNCIL_LISTEN="127.0.0.1:$CUSTODIAN_COUNCIL_PORT"
    CUSTODIAN_BASE_URL="https://$DOMAIN/custodian"
    export OPENRESTY_MODE CONFIGURE_PUBLIC_ENDPOINT EXPOSE_CUSTODIAN INSTALL_CUSTODIAN COUNCIL_LISTEN CUSTODIAN_BASE_URL

    install_openresty
    deploy_openresty_configuration
    print_https_activation_instructions
}

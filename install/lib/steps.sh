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

step_packages() {
    collect_packages
    _out "Packages: ${PACKAGES[*]}"
    export DEBIAN_FRONTEND=noninteractive
    info "Updating apt package metadata..."
    apt-get update >>"$LOG_FILE" 2>&1 || die "apt-get update failed; see $LOG_FILE"
    info "Installing packages..."
    apt-get install -y -- "${PACKAGES[@]}" >>"$LOG_FILE" 2>&1 \
        || die "Package installation failed; see $LOG_FILE"
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

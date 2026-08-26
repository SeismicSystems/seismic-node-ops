#!/usr/bin/env bash

# Observer key generation, assignment preservation, and bootstrapper configuration.

read_observer_node_public_key() {
    local output
    local public_key

    output=$(run_as_service_user "$SUMMIT_TARGET_BIN" keys show \
        --key-store-path "$SUMMIT_KEYS_DIR" 2>>"$LOG_FILE") \
        || return 1
    public_key=$(printf '%s\n' "$output" \
        | awk -F': ' '/Node Public Key \(ed25519\)/ {print tolower($2); exit}')
    public_key=${public_key#0x}
    [[ "$public_key" =~ ^[0-9a-f]{64}$ ]] || return 1
    printf '%s\n' "$public_key"
}

validate_observer_parent_node_key() {
    local node_key="$SUMMIT_KEYS_DIR/node_key.pem"
    local installed_public_key

    validate_summit_key_file "$node_key" "Observer parent Summit node key"
    if [[ ! -x "$SUMMIT_TARGET_BIN" ]]; then
        warn "The parent node key cannot be checked against its expected public key until Summit is installed."
        return
    fi

    installed_public_key=$(read_observer_node_public_key) \
        || die "Could not read the public key from the provisioned observer parent node key; see $LOG_FILE"
    [[ "$installed_public_key" == "$OBSERVER_PARENT_NODE_PUBLIC_KEY" ]] \
        || die "Provisioned node_key.pem does not match parent public key $OBSERVER_PARENT_NODE_PUBLIC_KEY"
    success "Provisioned observer parent node key matches the configured public key"
}

write_observer_assignment() {
    local expected="$OBSERVER_PARENT_NODE_PUBLIC_KEY:$OBSERVER_INDEX"
    local staging

    staging=$(mktemp "$SUMMIT_KEYS_DIR/.observer-assignment.XXXXXX")
    printf '%s\n' "$expected" >"$staging"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$staging"
    chmod 0600 "$staging"
    mv -- "$staging" "$OBSERVER_ASSIGNMENT_FILE"
}

configure_observer_bootstrapper_file() {
    local staging

    if [[ -z "$OBSERVER_BOOTSTRAPPERS_SOURCE" ]]; then
        if [[ -L "$OBSERVER_BOOTSTRAPPERS_FILE" ]]; then
            die "Observer bootstrapper configuration must not be a symbolic link: $OBSERVER_BOOTSTRAPPERS_FILE"
        fi
        rm -f -- "$OBSERVER_BOOTSTRAPPERS_FILE"
        return
    fi

    [[ ! -L "$OBSERVER_BOOTSTRAPPERS_SOURCE" &&
        -s "$OBSERVER_BOOTSTRAPPERS_SOURCE" ]] \
        || die "Summit bootstrappers source became invalid: $OBSERVER_BOOTSTRAPPERS_SOURCE"
    run_as_service_user test -r "$OBSERVER_BOOTSTRAPPERS_SOURCE" \
        || die "Service user cannot read the Summit bootstrappers source: $OBSERVER_BOOTSTRAPPERS_SOURCE"
    if [[ -L "$OBSERVER_BOOTSTRAPPERS_FILE" ]]; then
        die "Observer bootstrapper configuration must not be a symbolic link: $OBSERVER_BOOTSTRAPPERS_FILE"
    fi

    staging=$(mktemp "$SUMMIT_KEYS_DIR/.bootstrappers.toml.XXXXXX")
    if ! cp -- "$OBSERVER_BOOTSTRAPPERS_SOURCE" "$staging"; then
        rm -f -- "$staging"
        die "Could not install the Summit bootstrappers TOML."
    fi
    chown "$SERVICE_USER:$SERVICE_GROUP" "$staging"
    chmod 0640 "$staging"
    mv -- "$staging" "$OBSERVER_BOOTSTRAPPERS_FILE"
    success "Summit bootstrappers TOML installed: $OBSERVER_BOOTSTRAPPERS_FILE"
}

setup_summit_observer_keys() {
    local node_key="$SUMMIT_KEYS_DIR/node_key.pem"
    local consensus_key="$SUMMIT_KEYS_DIR/consensus_key.pem"
    local expected_assignment="$OBSERVER_PARENT_NODE_PUBLIC_KEY:$OBSERVER_INDEX"
    local existing_assignment=""
    local staging

    prepare_runtime_directory "$SUMMIT_KEYS_DIR" 0700 "Summit observer keys directory"

    if [[ -e "$OBSERVER_ASSIGNMENT_FILE" || -L "$OBSERVER_ASSIGNMENT_FILE" ]]; then
        [[ ! -L "$OBSERVER_ASSIGNMENT_FILE" && -f "$OBSERVER_ASSIGNMENT_FILE" ]] \
            || die "Observer assignment marker must be a regular file: $OBSERVER_ASSIGNMENT_FILE"
        existing_assignment=$(<"$OBSERVER_ASSIGNMENT_FILE")
        [[ "$existing_assignment" == "$expected_assignment" ]] \
            || die "Observer keys are assigned to $existing_assignment, not $expected_assignment; use separate persistent paths for a different assignment."
        chown "$SERVICE_USER:$SERVICE_GROUP" "$OBSERVER_ASSIGNMENT_FILE"
        chmod 0600 "$OBSERVER_ASSIGNMENT_FILE"
    elif [[ -e "$node_key" || -L "$node_key" ||
        -e "$consensus_key" || -L "$consensus_key" ]]; then
        die "Summit keys exist without an observer assignment marker; refusing to reuse or replace them."
    else
        write_observer_assignment
    fi

    if [[ -e "$consensus_key" || -L "$consensus_key" ]]; then
        validate_summit_key_file "$consensus_key" "Observer Summit consensus key"
    else
        if [[ ! -f "$SUMMIT_TARGET_BIN" || ! -x "$SUMMIT_TARGET_BIN" ]]; then
            warn "Observer consensus key was not generated because Summit is unavailable at $SUMMIT_TARGET_BIN."
            warn "Install Summit and rerun the observer installer before provisioning the parent node key."
            configure_observer_bootstrapper_file
            return
        fi
        if ! run_as_service_user "$SUMMIT_TARGET_BIN" run --help 2>&1 \
            | grep -q -- '--observer'; then
            die "Installed Summit binary does not support observer mode: $SUMMIT_TARGET_BIN"
        fi

        staging=$(mktemp -d /run/seismic-observer-keys.XXXXXX)
        chown "$SERVICE_USER:$SERVICE_GROUP" "$staging"
        chmod 0700 "$staging"
        info "Generating a fresh observer-only Summit consensus key..."
        if ! run_as_service_user "$SUMMIT_TARGET_BIN" keys generate \
            --key-store-path "$staging" -y >>"$LOG_FILE" 2>&1; then
            rm -rf -- "$staging"
            die "Observer Summit consensus-key generation failed; see $LOG_FILE"
        fi
        if [[ ! -s "$staging/consensus_key.pem" ||
            -L "$staging/consensus_key.pem" ]]; then
            rm -rf -- "$staging"
            die "Summit did not produce an observer consensus key."
        fi
        install -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0600 \
            -- "$staging/consensus_key.pem" "$consensus_key"
        rm -rf -- "$staging"
        success "Observer Summit consensus key generated: $consensus_key"
    fi

    if [[ -e "$node_key" || -L "$node_key" ]]; then
        validate_observer_parent_node_key
    else
        warn "Parent node_key.pem is not installed yet. Provision it securely before starting the observer or observer Custodian."
    fi

    configure_observer_bootstrapper_file
}

setup_observer_keys() {
    section "Setting up observer keys"
    setup_reth_p2p_key
    setup_summit_observer_keys
    success "Observer key phase complete."
}

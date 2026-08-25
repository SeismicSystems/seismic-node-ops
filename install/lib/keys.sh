#!/usr/bin/env bash

# Reth P2P and Summit validator key generation and preservation.

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

setup_reth_p2p_key() {
    local key_dir
    local staging

    key_dir=$(dirname -- "$RETH_P2P_KEY_PATH")
    prepare_runtime_directory "$key_dir" 0700 "Reth key directory"

    if [[ -e "$RETH_P2P_KEY_PATH" || -L "$RETH_P2P_KEY_PATH" ]]; then
        validate_reth_p2p_key "$RETH_P2P_KEY_PATH"
        chown "$SERVICE_USER:$SERVICE_GROUP" "$RETH_P2P_KEY_PATH"
        chmod 0600 "$RETH_P2P_KEY_PATH"
        success "Existing Reth P2P key preserved: $RETH_P2P_KEY_PATH"
        return
    fi

    staging=$(mktemp "$key_dir/.p2p-key.XXXXXX")
    chmod 0600 "$staging"
    if ! openssl rand -hex 32 | tr -d '\n' >"$staging"; then
        rm -f -- "$staging"
        die "Could not generate the Reth P2P key."
    fi
    validate_reth_p2p_key "$staging"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$staging"
    mv -- "$staging" "$RETH_P2P_KEY_PATH"
    success "Reth P2P key generated: $RETH_P2P_KEY_PATH"
}

validate_summit_key_file() {
    local path=$1
    local description=$2

    [[ ! -L "$path" ]] \
        || die "$description must not be a symbolic link: $path"
    [[ -s "$path" ]] \
        || die "$description is missing or empty: $path"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$path"
    chmod 0600 "$path"
}

setup_summit_validator_keys() {
    local node_key="$SUMMIT_KEYS_DIR/node_key.pem"
    local consensus_key="$SUMMIT_KEYS_DIR/consensus_key.pem"
    local node_present=false
    local consensus_present=false
    local staging

    prepare_runtime_directory "$SUMMIT_KEYS_DIR" 0700 "Summit validator keys directory"

    [[ ! -e "$node_key" && ! -L "$node_key" ]] || node_present=true
    [[ ! -e "$consensus_key" && ! -L "$consensus_key" ]] \
        || consensus_present=true

    if [[ "$node_present" == true && "$consensus_present" == true ]]; then
        validate_summit_key_file "$node_key" "Summit node key"
        validate_summit_key_file "$consensus_key" "Summit consensus key"
        success "Existing Summit validator keys preserved: $SUMMIT_KEYS_DIR"
        return
    fi

    if [[ "$node_present" == true || "$consensus_present" == true ]]; then
        die "Summit validator key set is incomplete; refusing to replace or regenerate keys in $SUMMIT_KEYS_DIR"
    fi

    if [[ ! -f "$SUMMIT_TARGET_BIN" || ! -x "$SUMMIT_TARGET_BIN" ]]; then
        warn "Summit validator keys were not generated because the Summit executable is unavailable at $SUMMIT_TARGET_BIN."
        warn "Generate the keys before starting Summit."
        return
    fi

    staging=$(mktemp -d "$VALIDATOR_KEYS_DIR/.summit-keys.XXXXXX")
    chown "$SERVICE_USER:$SERVICE_GROUP" "$staging"
    chmod 0700 "$staging"

    info "Generating Summit validator keys..."
    if ! run_as_service_user "$SUMMIT_TARGET_BIN" keys generate \
        --key-store-path "$staging" -y >>"$LOG_FILE" 2>&1; then
        rm -rf -- "$staging"
        die "Summit validator key generation failed; see $LOG_FILE"
    fi

    if [[ ! -s "$staging/node_key.pem" \
        || ! -s "$staging/consensus_key.pem" \
        || -L "$staging/node_key.pem" \
        || -L "$staging/consensus_key.pem" ]]; then
        rm -rf -- "$staging"
        die "Summit reported success but did not produce a complete validator key set."
    fi

    install \
        -o "$SERVICE_USER" \
        -g "$SERVICE_GROUP" \
        -m 0600 \
        -- "$staging/node_key.pem" "$node_key"
    install \
        -o "$SERVICE_USER" \
        -g "$SERVICE_GROUP" \
        -m 0600 \
        -- "$staging/consensus_key.pem" "$consensus_key"
    rm -rf -- "$staging"

    success "Summit validator keys generated: $SUMMIT_KEYS_DIR"
}

setup_validator_keys() {
    section "Setting up validator keys"
    setup_reth_p2p_key
    setup_summit_validator_keys
    success "Validator key phase complete."
}

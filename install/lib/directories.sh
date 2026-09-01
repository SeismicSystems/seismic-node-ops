#!/usr/bin/env bash

# Runtime directory creation without recursive ownership changes.

prepare_runtime_directory() {
    local path=$1
    local mode=$2
    local description=$3

    if [[ -L "$path" ]]; then
        die "$description became a symbolic link after configuration: $path"
    fi
    if [[ -e "$path" && ! -d "$path" ]]; then
        die "$description is no longer a directory: $path"
    fi

    if [[ -d "$path" ]]; then
        info "Verifying existing $description: $path"
    else
        info "Creating $description: $path"
    fi

    if ! install -d \
        -o "$SERVICE_USER" \
        -g "$SERVICE_GROUP" \
        -m "$mode" \
        -- "$path"; then
        die "Could not prepare $description: $path"
    fi

    success "$description ready: $path ($SERVICE_USER:$SERVICE_GROUP, mode $mode)"
}

setup_runtime_directories() {
    section "Setting up runtime directories"

    prepare_runtime_directory "$RETH_DATA_DIR" 0750 "Reth data directory"
    prepare_runtime_directory "$SUMMIT_DATA_DIR" 0750 "Summit data directory"
    prepare_runtime_directory "$VALIDATOR_KEYS_DIR" 0700 "Node keys directory"

    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        prepare_runtime_directory \
            "$CHECKPOINTS_DIR" 0750 "Checkpointer output directory"
    fi

    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        prepare_runtime_directory \
            "$CUSTODIAN_DATA_DIR" 0700 "Custodian data directory"
        prepare_runtime_directory \
            "$CUSTODIAN_DATA_DIR/deliveries" 0700 \
            "Custodian epoch-key deliveries directory"
    fi

    success "Runtime directories prepared; no keys or data files were created."
}

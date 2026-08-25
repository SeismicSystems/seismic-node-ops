#!/usr/bin/env bash

# Centralized Custodian binary and persistent root-key installation.

CUSTODIAN_REPO="https://github.com/SeismicSystems/enclave"
CUSTODIAN_SHARED_DEFAULT_ROOT_KEY_HEX="31fb870214e3b05b6ca9e24c3ebb0c0dd381ff7800b387b3dad816afcb0ff7a0"

install_custodian_binary() {
    local source_dir="$SERVICE_HOME/src/enclave"
    local build_output="$source_dir/target/release/seismic-centralized-custodian-service"

    install_rust_for_service_user
    prepare_source_checkout \
        "Centralized Custodian" \
        "$CUSTODIAN_REPO" \
        "$source_dir" \
        "$CUSTODIAN_SOURCE_REF"

    info "Building Centralized Custodian from $CUSTODIAN_SOURCE_REF..."
    # shellcheck disable=SC2016
    if ! run_as_service_user bash -c \
        'source "$HOME/.cargo/env"; cd "$1"; cargo build --release -p seismic-centralized-custodian-service' \
        _ "$source_dir" >>"$LOG_FILE" 2>&1; then
        die "Centralized Custodian build failed; see $LOG_FILE"
    fi

    install_binary_target \
        "Centralized Custodian" \
        "$build_output" \
        "$CUSTODIAN_TARGET_BIN"
}

read_binary_file_hex() {
    local path=$1
    od -An -tx1 -v -- "$path" | tr -d '[:space:]'
}

validate_existing_custodian_root_key() {
    local path=$1

    [[ ! -L "$path" ]] \
        || die "Custodian root key must not be a symbolic link: $path"
    [[ -f "$path" ]] \
        || die "Custodian root key path exists but is not a regular file: $path"
    [[ $(stat -c %s -- "$path") == "32" ]] \
        || die "Existing Custodian root key is not exactly 32 bytes: $path"
}

install_custodian_root_key() {
    local target="$CUSTODIAN_DATA_DIR/root.key"
    local existing_hex

    if [[ ! -e "$target" && ! -L "$target" ]]; then
        info "No Custodian root key file will be written; Custodian will pin the shared default on first start."
        return
    fi

    validate_existing_custodian_root_key "$target"
    existing_hex=$(read_binary_file_hex "$target") \
        || die "Could not read the existing Custodian root key."
    [[ "$existing_hex" == "$CUSTODIAN_SHARED_DEFAULT_ROOT_KEY_HEX" ]] \
        || die "Existing Custodian root key differs from the required shared default; refusing to replace it."

    chown "$SERVICE_USER:$SERVICE_GROUP" "$target"
    chmod 0600 "$target"
    success "Existing Custodian root key matches the required shared default"
}

install_custodian() {
    if [[ "$INSTALL_CUSTODIAN" != true ]]; then
        info "Centralized Custodian disabled; skipping Custodian installation."
        return
    fi

    section "Installing Centralized Custodian"
    install_custodian_binary
    install_custodian_root_key
    success "Centralized Custodian binary and persistent root-key state are ready."
}

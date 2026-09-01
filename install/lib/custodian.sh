#!/usr/bin/env bash

# Centralized Custodian binary and persistent root-key installation.

CUSTODIAN_REPO="https://github.com/SeismicSystems/enclave"
CUSTODIAN_SHARED_DEFAULT_ROOT_KEY_HEX="31fb870214e3b05b6ca9e24c3ebb0c0dd381ff7800b387b3dad816afcb0ff7a0"

install_custodian_binary() {
    local source_dir="$SERVICE_HOME/src/enclave"
    local build_output="$source_dir/target/release/seismic-centralized-custodian-service"

    case "$CUSTODIAN_INSTALL_METHOD" in
        prebuilt)
            install_binary_target \
                "Centralized Custodian" \
                "$CUSTODIAN_BINARY" \
                "$CUSTODIAN_TARGET_BIN"
            ;;
        source)
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
            ;;
        deferred)
            info "Centralized Custodian installation deferred; expected executable: $CUSTODIAN_TARGET_BIN"
            ;;
        *)
            die "Unknown Centralized Custodian installation method: $CUSTODIAN_INSTALL_METHOD"
            ;;
    esac
}

validate_custodian_cli_support() {
    local help_output
    local security_error

    if [[ ! -e "$CUSTODIAN_TARGET_BIN" && ! -L "$CUSTODIAN_TARGET_BIN" ]]; then
        return 1
    fi
    if ! security_error=$(check_service_executable_security "$CUSTODIAN_TARGET_BIN"); then
        die "Custodian executable is not safe for service use: $security_error"
    fi

    help_output=$("$CUSTODIAN_TARGET_BIN" --help 2>&1) \
        || die "Could not inspect the installed Custodian command-line interface."
    grep -q -- '--summit-key-dir' <<<"$help_output" \
        || die "Installed Custodian does not support --summit-key-dir."
    if [[ "${NODE_ROLE:-validator}" == "observer" ]]; then
        grep -q -- '--observer' <<<"$help_output" \
            || die "Installed Custodian does not support --observer."
        grep -q -- '--parent-custodian' <<<"$help_output" \
            || die "Installed Custodian does not support --parent-custodian."
    fi

    success "Custodian command-line compatibility validated"
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
    if ! validate_custodian_cli_support; then
        warn "Custodian executable is deferred and not present at $CUSTODIAN_TARGET_BIN."
        warn "Provide a compatible executable before starting Custodian."
    fi
    install_custodian_root_key
    success "Centralized Custodian installation and persistent root-key preparation are complete."
}

prepare_observer_custodian_root_key() {
    local target="$CUSTODIAN_DATA_DIR/root.key"

    if [[ ! -e "$target" && ! -L "$target" ]]; then
        info "No observer Custodian root key is present; it will be fetched from the parent Custodian on first start."
        return
    fi

    validate_existing_custodian_root_key "$target"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$target"
    chmod 0600 "$target"
    success "Existing observer Custodian root key preserved for parent verification on startup"
}

install_observer_custodian() {
    if [[ "$INSTALL_CUSTODIAN" != true ]]; then
        info "Centralized Custodian disabled; skipping observer Custodian installation."
        return
    fi

    section "Installing observer Centralized Custodian"
    install_custodian_binary
    if ! validate_custodian_cli_support; then
        warn "Observer Custodian executable is deferred and not present at $CUSTODIAN_TARGET_BIN."
        warn "Provide a compatible executable before starting Custodian."
    fi
    prepare_observer_custodian_root_key
    success "Observer Custodian installation and persistent root-key preparation are complete."
}

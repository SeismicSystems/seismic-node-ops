#!/usr/bin/env bash

# summit-checkpointer and Reth-compatible mdbx_copy installation.

CHECKPOINTER_REPO="https://github.com/SeismicSystems/summit-checkpointer.git"

install_checkpointer_binary() {
    local source_dir="$SERVICE_HOME/src/summit-checkpointer"
    local build_output="$source_dir/target/release/summit-checkpointer"

    case "$CHECKPOINTER_INSTALL_METHOD" in
        prebuilt)
            install_binary_target \
                "summit-checkpointer" \
                "$CHECKPOINTER_BINARY" \
                "$CHECKPOINTER_TARGET_BIN"
            ;;
        source)
            install_rust_for_service_user
            prepare_source_checkout \
                "summit-checkpointer" \
                "$CHECKPOINTER_REPO" \
                "$source_dir" \
                "$CHECKPOINTER_SOURCE_REF"
            info "Building summit-checkpointer from $CHECKPOINTER_SOURCE_REF..."
            # shellcheck disable=SC2016
            if ! run_as_service_user bash -c \
                'source "$HOME/.cargo/env"; cd "$1"; cargo build --release' \
                _ "$source_dir" >>"$LOG_FILE" 2>&1; then
                die "summit-checkpointer build failed; see $LOG_FILE"
            fi
            install_binary_target \
                "summit-checkpointer" \
                "$build_output" \
                "$CHECKPOINTER_TARGET_BIN"
            ;;
        deferred)
            info "summit-checkpointer installation deferred; expected executable: $CHECKPOINTER_TARGET_BIN"
            ;;
        *)
            die "Unknown summit-checkpointer installation method: $CHECKPOINTER_INSTALL_METHOD"
            ;;
    esac
}

install_mdbx_copy() {
    local reth_source_dir
    local libmdbx_dir
    local build_output

    if [[ "$RETH_INSTALL_METHOD" == "source" ]]; then
        reth_source_dir="$SERVICE_HOME/src/seismic-reth"
    elif [[ -n "$RETH_SOURCE_DIR_FOR_MDBX" ]]; then
        reth_source_dir="$RETH_SOURCE_DIR_FOR_MDBX"
    else
        warn "Skipping mdbx_copy installation because no matching seismic-reth source checkout was provided."
        warn "Before starting summit-checkpointer, provide a compatible executable at $MDBX_COPY_TARGET_BIN."
        return
    fi

    libmdbx_dir="$reth_source_dir/crates/storage/libmdbx-rs/mdbx-sys/libmdbx"
    build_output="$libmdbx_dir/mdbx_copy"

    [[ ! -L "$reth_source_dir" ]] \
        || die "seismic-reth source checkout for mdbx_copy must not be a symbolic link: $reth_source_dir"
    [[ -f "$libmdbx_dir/VERSION.json" && -f "$libmdbx_dir/Makefile" ]] \
        || die "Reth's vendored libmdbx build files are missing at $libmdbx_dir"

    if ! run_as_service_user test -r "$libmdbx_dir/Makefile"; then
        die "Service user cannot read the vendored libmdbx Makefile: $libmdbx_dir/Makefile"
    fi
    if ! run_as_service_user test -w "$libmdbx_dir"; then
        die "Service user cannot write to the vendored libmdbx directory: $libmdbx_dir"
    fi

    info "Building mdbx_copy from the selected seismic-reth vendored libmdbx revision..."
    if ! run_as_service_user make -C "$libmdbx_dir" mdbx_copy \
        >>"$LOG_FILE" 2>&1; then
        die "mdbx_copy build failed; see $LOG_FILE"
    fi

    install_binary_target "mdbx_copy" "$build_output" "$MDBX_COPY_TARGET_BIN"
    if ! "$MDBX_COPY_TARGET_BIN" -V >>"$LOG_FILE" 2>&1; then
        die "mdbx_copy validation failed; see $LOG_FILE"
    fi
    success "Reth-compatible mdbx_copy installed from $reth_source_dir"
}

install_checkpointer() {
    if [[ "$INSTALL_CHECKPOINTER" != true ]]; then
        info "summit-checkpointer disabled; skipping checkpointer installation."
        return
    fi

    section "Installing summit-checkpointer"
    install_checkpointer_binary
    install_mdbx_copy
    success "summit-checkpointer binary and compatible mdbx_copy installation phase complete."
}

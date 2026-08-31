#!/usr/bin/env bash

# Summit and seismic-reth binary installation.

SUMMIT_REPO="https://github.com/SeismicSystems/summit.git"
RETH_REPO="https://github.com/SeismicSystems/seismic-reth.git"

run_as_service_user() {
    command -v runuser >/dev/null 2>&1 \
        || die "runuser is required for service-user operations."
    runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" "$@"
}

check_service_executable_security() {
    local path=$1
    local parent
    local owner_uid
    local mode
    local mode_value

    if [[ -L "$path" ]]; then
        printf '%s\n' "path is a symbolic link"
        return 1
    fi
    if [[ ! -f "$path" ]]; then
        printf '%s\n' "path is not a regular file"
        return 1
    fi
    if [[ ! -x "$path" ]]; then
        printf '%s\n' "file is not executable"
        return 1
    fi

    if ! owner_uid=$(stat -c %u -- "$path"); then
        printf '%s\n' "could not inspect file ownership"
        return 1
    fi
    if [[ "$owner_uid" != "0" ]]; then
        printf '%s\n' "file is not owned by root"
        return 1
    fi
    if ! mode=$(stat -c %a -- "$path"); then
        printf '%s\n' "could not inspect file permissions"
        return 1
    fi
    mode_value=$((8#$mode))
    if ((mode_value & 0022)); then
        printf '%s\n' "file is group- or world-writable"
        return 1
    fi
    if run_as_service_user test -w "$path"; then
        printf '%s\n' "file is writable by service user $SERVICE_USER"
        return 1
    fi

    parent=$(dirname -- "$path")
    while true; do
        if [[ -L "$parent" ]]; then
            printf '%s\n' "parent path is a symbolic link: $parent"
            return 1
        fi
        if [[ ! -d "$parent" ]]; then
            printf '%s\n' "parent directory is missing: $parent"
            return 1
        fi
        if ! owner_uid=$(stat -c %u -- "$parent"); then
            printf '%s\n' "could not inspect parent directory ownership: $parent"
            return 1
        fi
        if [[ "$owner_uid" != "0" ]]; then
            printf '%s\n' "parent directory is not owned by root: $parent"
            return 1
        fi
        if ! mode=$(stat -c %a -- "$parent"); then
            printf '%s\n' "could not inspect parent directory permissions: $parent"
            return 1
        fi
        mode_value=$((8#$mode))
        if ((mode_value & 0022)); then
            printf '%s\n' "parent directory is group- or world-writable: $parent"
            return 1
        fi
        if run_as_service_user test -w "$parent"; then
            printf '%s\n' "parent directory is writable by service user $SERVICE_USER: $parent"
            return 1
        fi
        [[ "$parent" == "/" ]] && break
        parent=$(dirname -- "$parent")
    done

    return 0
}

install_rust_for_service_user() {
    # shellcheck disable=SC2016
    if run_as_service_user bash -c \
        'test -x "$HOME/.cargo/bin/cargo" && test -f "$HOME/.cargo/env"'; then
        info "Rust is already installed for $SERVICE_USER."
        return
    fi

    info "Installing Rust for $SERVICE_USER with the minimal rustup profile..."
    if ! run_as_service_user bash -c \
        'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal' \
        >>"$LOG_FILE" 2>&1; then
        die "Rust installation failed; see $LOG_FILE"
    fi

    # shellcheck disable=SC2016
    run_as_service_user bash -c \
        'source "$HOME/.cargo/env" && cargo --version' \
        >>"$LOG_FILE" 2>&1 \
        || die "Rust was installed but cargo validation failed; see $LOG_FILE"
    success "Rust installed for $SERVICE_USER"
}

prepare_source_root() {
    local source_root="$SERVICE_HOME/src"

    if [[ -L "$source_root" ]]; then
        die "Source root must not be a symbolic link: $source_root"
    fi
    if [[ -e "$source_root" && ! -d "$source_root" ]]; then
        die "Source root exists but is not a directory: $source_root"
    fi

    install -d \
        -o "$SERVICE_USER" \
        -g "$SERVICE_GROUP" \
        -m 0755 \
        -- "$source_root" \
        || die "Could not prepare source root: $source_root"
}

clean_reth_mdbx_build_artifacts() {
    local source_dir=$1
    local libmdbx_dir="$source_dir/crates/storage/libmdbx-rs/mdbx-sys/libmdbx"
    local tracked_status
    local status

    [[ -d "$source_dir/.git" && -f "$libmdbx_dir/Makefile" ]] || return

    tracked_status=$(run_as_service_user git -C "$source_dir" \
        status --porcelain --untracked-files=no) \
        || die "Could not inspect tracked seismic-reth source changes."
    [[ -z "$tracked_status" ]] || return

    status=$(run_as_service_user git -C "$source_dir" status --porcelain) \
        || die "Could not inspect generated seismic-reth build artifacts."
    [[ -n "$status" ]] || return

    info "Cleaning generated libmdbx artifacts from the seismic-reth checkout..."
    run_as_service_user make -C "$libmdbx_dir" clean \
        >>"$LOG_FILE" 2>&1 \
        || die "Could not clean generated seismic-reth libmdbx artifacts; see $LOG_FILE"
}

prepare_source_checkout() {
    local description=$1
    local repository=$2
    local source_dir=$3
    local branch=$4
    local origin
    local commit
    local status

    [[ -n "$branch" ]] || die "$description source branch is not configured."
    prepare_source_root

    if [[ ! -e "$source_dir" ]]; then
        info "Cloning $description branch $branch into $source_dir..."
        if ! run_as_service_user git clone \
            --branch "$branch" \
            --single-branch \
            "$repository" \
            "$source_dir" >>"$LOG_FILE" 2>&1; then
            die "Could not clone $description; see $LOG_FILE"
        fi
    else
        [[ ! -L "$source_dir" ]] \
            || die "$description source directory must not be a symbolic link: $source_dir"
        [[ -d "$source_dir/.git" ]] \
            || die "$description source path exists but is not a Git checkout: $source_dir"

        origin=$(run_as_service_user git -C "$source_dir" config --get remote.origin.url) \
            || die "Could not read $description Git origin."
        [[ "$origin" == "$repository" ]] \
            || die "$description checkout has unexpected origin: $origin"

        if [[ "$description" == "seismic-reth" ]]; then
            clean_reth_mdbx_build_artifacts "$source_dir"
        fi

        status=$(run_as_service_user git -C "$source_dir" status --porcelain) \
            || die "Could not inspect the $description working tree."
        if [[ -n "$status" ]]; then
            die "$description checkout has local changes; refusing to overwrite them: $source_dir"
        fi

        info "Updating $description branch $branch with fast-forward only..."
        run_as_service_user git -C "$source_dir" fetch origin "$branch" \
            >>"$LOG_FILE" 2>&1 \
            || die "Could not fetch $description branch $branch; see $LOG_FILE"

        if run_as_service_user git -C "$source_dir" show-ref \
            --verify --quiet "refs/heads/$branch"; then
            run_as_service_user git -C "$source_dir" checkout "$branch" \
                >>"$LOG_FILE" 2>&1 \
                || die "Could not check out $description branch $branch."
        else
            run_as_service_user git -C "$source_dir" checkout \
                -b "$branch" --track "origin/$branch" \
                >>"$LOG_FILE" 2>&1 \
                || die "Could not create local $description branch $branch."
        fi

        run_as_service_user git -C "$source_dir" merge \
            --ff-only "origin/$branch" >>"$LOG_FILE" 2>&1 \
            || die "$description branch cannot be updated with fast-forward only."
    fi

    commit=$(run_as_service_user git -C "$source_dir" rev-parse HEAD) \
        || die "Could not determine the installed $description source revision."
    success "$description source ready: $branch at $commit"
}

install_binary_target() {
    local description=$1
    local source_path=$2
    local target_path=$3
    local normalized_source
    local normalized_target

    [[ -f "$source_path" && -x "$source_path" ]] \
        || die "$description build output is missing or not executable: $source_path"
    [[ ! -L "$target_path" ]] \
        || die "$description target must not be a symbolic link: $target_path"
    [[ -d $(dirname -- "$target_path") ]] \
        || die "$description target parent directory does not exist: $(dirname -- "$target_path")"

    normalized_source=$(realpath -- "$source_path")
    normalized_target=$(realpath -m -- "$target_path")
    if [[ "$normalized_source" == "$normalized_target" ]]; then
        chown root:root "$target_path"
        chmod 0755 "$target_path"
    else
        install -o root -g root -m 0755 -- "$source_path" "$target_path" \
            || die "Could not install $description binary at $target_path"
    fi

    [[ -f "$target_path" && -x "$target_path" ]] \
        || die "$description target is not executable after installation: $target_path"
    success "$description binary installed: $target_path"
}

install_summit_binary() {
    local source_dir="$SERVICE_HOME/src/summit"
    local build_output="$source_dir/target/release/summit"

    case "$SUMMIT_INSTALL_METHOD" in
        prebuilt)
            install_binary_target "Summit" "$SUMMIT_BINARY" "$SUMMIT_TARGET_BIN"
            ;;
        source)
            install_rust_for_service_user
            prepare_source_checkout \
                "Summit" "$SUMMIT_REPO" "$source_dir" "$SUMMIT_SOURCE_REF"
            info "Building Summit from $SUMMIT_SOURCE_REF..."
            # shellcheck disable=SC2016
            if ! run_as_service_user bash -c \
                'source "$HOME/.cargo/env"; cd "$1"; cargo build --release --features prom' \
                _ "$source_dir" >>"$LOG_FILE" 2>&1; then
                die "Summit build failed; see $LOG_FILE"
            fi
            install_binary_target "Summit" "$build_output" "$SUMMIT_TARGET_BIN"
            ;;
        deferred)
            info "Summit installation deferred; expected executable: $SUMMIT_TARGET_BIN"
            ;;
        *) die "Unknown Summit installation method: $SUMMIT_INSTALL_METHOD" ;;
    esac
}

install_reth_binary() {
    local source_dir="$SERVICE_HOME/src/seismic-reth"
    local build_output="$source_dir/target/release/seismic-reth"

    case "$RETH_INSTALL_METHOD" in
        prebuilt)
            install_binary_target "seismic-reth" "$RETH_BINARY" "$RETH_TARGET_BIN"
            ;;
        source)
            install_rust_for_service_user
            prepare_source_checkout \
                "seismic-reth" "$RETH_REPO" "$source_dir" "$RETH_SOURCE_REF"
            info "Building seismic-reth from $RETH_SOURCE_REF..."
            # shellcheck disable=SC2016
            if ! run_as_service_user bash -c \
                'source "$HOME/.cargo/env"; cd "$1"; cargo build --release' \
                _ "$source_dir" >>"$LOG_FILE" 2>&1; then
                die "seismic-reth build failed; see $LOG_FILE"
            fi
            install_binary_target \
                "seismic-reth" "$build_output" "$RETH_TARGET_BIN"
            ;;
        deferred)
            info "seismic-reth installation deferred; expected executable: $RETH_TARGET_BIN"
            ;;
        *) die "Unknown seismic-reth installation method: $RETH_INSTALL_METHOD" ;;
    esac
}

install_node_binaries() {
    section "Installing node binaries"
    install_summit_binary
    install_reth_binary
    success "Summit and seismic-reth installation methods completed."
}

install_validator_binaries() {
    install_node_binaries
}

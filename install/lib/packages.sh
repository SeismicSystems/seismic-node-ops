#!/usr/bin/env bash

# Conditional system package planning and installation.

collect_system_packages() {
    local needs_source_build=false
    local needs_mdbx_build=false
    local needs_git=false

    SYSTEM_PACKAGES=(
        curl
        jq
        ca-certificates
        openssl
    )

    # Preserve the package and daemon lifecycle on installer reruns. Installing
    # Supervisor for the first time is safe because Seismic program files have
    # not been deployed yet; an already-installed package is deliberately not
    # upgraded or reinstalled here.
    if ! dpkg-query -W -f='${Status}' supervisor 2>/dev/null \
        | grep -q '^install ok installed$'; then
        SYSTEM_PACKAGES=(supervisor "${SYSTEM_PACKAGES[@]}")
    fi

    if [[ "$SUMMIT_INSTALL_METHOD" == "source" \
        || "$RETH_INSTALL_METHOD" == "source" \
        || ("$INSTALL_CHECKPOINTER" == true \
            && "$CHECKPOINTER_INSTALL_METHOD" == "source") \
        || ("$INSTALL_CUSTODIAN" == true \
            && "$CUSTODIAN_INSTALL_METHOD" == "source") ]]; then
        needs_source_build=true
        needs_git=true
    fi

    if [[ "$INSTALL_CHECKPOINTER" == true \
        && ("$RETH_INSTALL_METHOD" == "source" \
            || -n "$RETH_SOURCE_DIR_FOR_MDBX") ]]; then
        needs_mdbx_build=true
    fi

    if [[ "$CONFIGURE_PUBLIC_ENDPOINT" == true ]]; then
        needs_git=true
    fi

    if [[ "$needs_git" == true ]]; then
        SYSTEM_PACKAGES+=(git)
    fi

    if [[ "$needs_source_build" == true ]]; then
        SYSTEM_PACKAGES+=(
            build-essential
            clang
            pkg-config
            libssl-dev
        )
    elif [[ "$needs_mdbx_build" == true ]]; then
        SYSTEM_PACKAGES+=(build-essential)
    fi

    if [[ "$CONFIGURE_PUBLIC_ENDPOINT" == true ]]; then
        SYSTEM_PACKAGES+=(
            gnupg
            lsb-release
            luarocks
        )
    fi
}

print_system_package_plan() {
    collect_system_packages

    _out "System packages:"
    _out "  ${SYSTEM_PACKAGES[*]}"
}

install_system_packages() {
    collect_system_packages
    section "Installing system packages"
    _out "Packages: ${SYSTEM_PACKAGES[*]}"

    export DEBIAN_FRONTEND=noninteractive
    info "Updating apt package metadata..."
    if ! apt-get update >>"$LOG_FILE" 2>&1; then
        die "apt-get update failed; see $LOG_FILE"
    fi

    info "Installing selected system packages..."
    if ! apt-get install -y -- "${SYSTEM_PACKAGES[@]}" >>"$LOG_FILE" 2>&1; then
        die "System package installation failed; see $LOG_FILE"
    fi

    success "System packages installed"
}

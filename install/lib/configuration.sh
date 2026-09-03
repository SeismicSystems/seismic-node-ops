#!/usr/bin/env bash

# Interactive configuration helpers shared by node installers.

prompt() {
    local variable_name=$1
    local question=$2
    local default=${3:-}
    local answer

    if [[ -n "$default" ]]; then
        read -r -p "$question [$default]: " answer
        printf -v "$variable_name" '%s' "${answer:-$default}"
    else
        read -r -p "$question: " answer
        printf -v "$variable_name" '%s' "$answer"
    fi
}

contains_unsafe_path_characters() {
    [[ "$1" =~ [[:space:]\;\|\&\$\`\"\'\\\%\#] ]]
}

confirm() {
    local question=$1
    local answer

    while true; do
        read -r -p "$question [y/N]: " answer
        case "$answer" in
            [Yy] | [Yy][Ee][Ss]) return 0 ;;
            "" | [Nn] | [Nn][Oo]) return 1 ;;
            *) error "Please answer yes or no." ;;
        esac
    done
}

confirm_installation_inventory_overwrite() {
    local path=$1

    [[ -e "$path" || -L "$path" ]] || return 0

    warn "An installation inventory already exists: $path"
    warn "The installer will not read or reuse its contents."
    if ! confirm "Overwrite it after a successful installation?"; then
        info "Installation cancelled; the existing inventory was not changed."
        exit 0
    fi
}

install_installation_inventory() {
    local path=$1
    local parent
    local resolved_parent
    local name
    local staging

    parent=$(dirname -- "$path")
    resolved_parent=$(realpath -m -- "$parent")
    [[ "$resolved_parent" == "$parent" ]] \
        || die "Installation inventory parent must not contain symbolic links: $parent"

    if [[ ! -e "$parent" ]]; then
        install -d -o root -g root -m 0755 -- "$parent"
    fi
    [[ -d "$parent" && ! -L "$parent" ]] \
        || die "Installation inventory parent is not a safe directory: $parent"

    name=${path##*/}
    staging=$(mktemp "$parent/.${name}.XXXXXX") \
        || die "Could not create the installation inventory staging file."
    if ! cat >"$staging"; then
        rm -f -- "$staging"
        die "Could not render the installation inventory."
    fi
    if ! chown root:root "$staging" || ! chmod 0644 "$staging"; then
        rm -f -- "$staging"
        die "Could not secure the installation inventory staging file."
    fi
    if ! mv -fT -- "$staging" "$path"; then
        rm -f -- "$staging"
        die "Could not install the installation inventory: $path"
    fi

    success "Installation inventory written: $path"
}

configure_service_user() {
    local default_user=${SUDO_USER:-ubuntu}

    section "Service user configuration"
    [[ "$default_user" != "root" ]] || default_user="ubuntu"

    while true; do
        prompt SERVICE_USER "User to run node services as" "$default_user"

        if [[ ! "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*\$?$ ]]; then
            error "Invalid service username: $SERVICE_USER"
            continue
        fi
        if [[ "$SERVICE_USER" == "root" ]]; then
            error "Node services must not run as root."
            continue
        fi
        if ! id "$SERVICE_USER" >/dev/null 2>&1; then
            error "Service user does not exist: $SERVICE_USER"
            continue
        fi

        SERVICE_HOME=$(getent passwd "$SERVICE_USER" | cut -d: -f6)
        if ! SERVICE_GROUP=$(id -gn "$SERVICE_USER"); then
            error "Could not determine the primary group for service user: $SERVICE_USER"
            continue
        fi
        if [[ "$SERVICE_HOME" != /* ]]; then
            error "Service user has no valid home directory: $SERVICE_USER"
            continue
        fi
        if [[ ! -d "$SERVICE_HOME" ]]; then
            error "Service user home directory does not exist: $SERVICE_HOME"
            continue
        fi
        break
    done

    success "Service user selected: $SERVICE_USER"
    _out "Service group: $SERVICE_GROUP"
    _out "Service home: $SERVICE_HOME"
}

print_available_disk_space() {
    local path=$1
    local probe=$path

    while [[ ! -e "$probe" ]]; do
        local parent
        parent=$(dirname -- "$probe")
        [[ "$parent" != "$probe" ]] || break
        probe=$parent
    done

    if [[ "$probe" != "$path" ]]; then
        info "Directory does not exist yet; using nearest existing parent: $probe"
    fi

    _out ""
    _out "Available disk space for $path:"
    df -h --output=source,size,used,avail,pcent,target -- "$probe"
}

directory_conflicts_with_existing() {
    local selected=$1
    local variable_name=$2
    local description=$3
    local configured_variables=(
        RETH_DATA_DIR
        SUMMIT_DATA_DIR
        VALIDATOR_KEYS_DIR
        CHECKPOINTS_DIR
        CUSTODIAN_DATA_DIR
    )
    local configured_descriptions=(
        "Reth data directory"
        "Summit data directory"
        "Validator keys directory"
        "Checkpointer output directory"
        "Custodian data directory"
    )
    local i configured_variable configured_path

    for ((i = 0; i < ${#configured_variables[@]}; i++)); do
        configured_variable=${configured_variables[i]}
        [[ "$configured_variable" != "$variable_name" ]] || continue
        declare -p "$configured_variable" >/dev/null 2>&1 || continue
        configured_path=${!configured_variable}
        [[ -n "$configured_path" ]] || continue

        if [[ "$selected" == "$configured_path" ||
            "$selected" == "$configured_path/"* ||
            "$configured_path" == "$selected/"* ]]; then
            error "$description and ${configured_descriptions[i]} must not be identical or nested."
            return 0
        fi
    done

    return 1
}

configure_directory() {
    local variable_name=$1
    local description=$2
    local default=$3
    local contents=$4
    local selected

    _out "$description:"
    _out "  $contents"
    _out ""

    while true; do
        prompt "$variable_name" "$description" "$default"
        selected=${!variable_name}

        if [[ "$selected" != /* ]]; then
            error "$description must be an absolute path."
            continue
        fi
        if [[ "$selected" == "/" ]]; then
            error "The filesystem root cannot be used for $description."
            continue
        fi
        if contains_unsafe_path_characters "$selected"; then
            error "$description contains unsupported whitespace or shell characters."
            continue
        fi
        if [[ -e "$selected" && ! -d "$selected" ]]; then
            error "$description exists but is not a directory: $selected"
            continue
        fi

        selected=$(realpath -m -- "$selected")
        if directory_conflicts_with_existing "$selected" "$variable_name" "$description"; then
            continue
        fi
        break
    done

    printf -v "$variable_name" '%s' "$selected"
    success "$description selected: $selected"
    print_available_disk_space "$selected"
    _out ""
}

configure_file_path() {
    local variable_name=$1
    local description=$2
    local default=$3
    local selected
    local parent
    local probe
    local current
    local owner_uid
    local mode
    local mode_value
    local parent_safe

    while true; do
        prompt "$variable_name" "$description" "$default"
        selected=${!variable_name}

        if [[ "$selected" != /* ]]; then
            error "$description must be an absolute path."
            continue
        fi
        if [[ "$selected" == "/" ]]; then
            error "The filesystem root cannot be used for $description."
            continue
        fi
        if contains_unsafe_path_characters "$selected"; then
            error "$description contains unsupported whitespace or shell characters."
            continue
        fi
        if [[ -L "$selected" ]]; then
            error "$description must not be a symbolic link: $selected"
            continue
        fi
        if [[ -e "$selected" && ! -f "$selected" ]]; then
            error "$description exists but is not a regular file: $selected"
            continue
        fi

        parent=$(dirname -- "$selected")
        probe=$parent
        while [[ ! -e "$probe" ]]; do
            probe=$(dirname -- "$probe")
        done
        if [[ ! -d "$probe" ]]; then
            error "$description has a non-directory parent: $probe"
            continue
        fi
        if [[ -L "$probe" ]]; then
            error "$description parent must not be a symbolic link: $probe"
            continue
        fi

        current=$probe
        parent_safe=true
        while true; do
            if [[ -L "$current" ]]; then
                error "$description parent chain contains a symbolic link: $current"
                parent_safe=false
                break
            fi
            owner_uid=$(stat -c %u -- "$current")
            if [[ "$owner_uid" != "0" ]]; then
                error "$description parent must be root-owned: $current"
                parent_safe=false
                break
            fi
            mode=$(stat -c %a -- "$current")
            mode_value=$((8#$mode))
            if ((mode_value & 0022)); then
                error "$description parent must not be group- or world-writable: $current"
                parent_safe=false
                break
            fi
            [[ "$current" == "/" ]] && break
            current=$(dirname -- "$current")
        done
        [[ "$parent_safe" == true ]] || continue

        selected=$(realpath -m -- "$selected")
        break
    done

    printf -v "$variable_name" '%s' "$selected"
    success "$description selected: $selected"
}

configure_prebuilt_binary() {
    local variable_name=$1
    local description=$2
    local selected

    while true; do
        prompt "$variable_name" "Path to pre-built $description binary" ""
        selected=${!variable_name}

        if [[ "$selected" != /* ]]; then
            error "$description binary path must be absolute."
            continue
        fi
        if [[ ! -f "$selected" ]]; then
            error "$description binary is not an existing regular file: $selected"
            continue
        fi
        if [[ ! -x "$selected" ]]; then
            error "$description binary is not executable: $selected"
            continue
        fi

        selected=$(realpath -- "$selected")
        printf -v "$variable_name" '%s' "$selected"
        success "$description binary selected: $selected"
        return
    done
}

configure_deferred_binary_path() {
    local target_variable=$1
    local description=$2
    local default_path=$3
    local selected
    local security_error

    while true; do
        prompt selected "Path where the manually built $description binary will be provided" "$default_path"

        if [[ "$selected" != /* ]]; then
            error "$description deferred binary path must be absolute."
            continue
        fi
        if [[ "$selected" == "/" ]]; then
            error "The filesystem root cannot be used as the $description binary path."
            continue
        fi
        if contains_unsafe_path_characters "$selected"; then
            error "$description deferred binary path contains unsupported whitespace or shell characters."
            continue
        fi
        if [[ -L "$selected" ]]; then
            error "$description deferred binary path must not be a symbolic link: $selected"
            continue
        fi
        if [[ -e "$selected" && ! -f "$selected" ]]; then
            error "$description deferred binary path exists but is not a regular file: $selected"
            continue
        fi
        if [[ -e "$selected" ]] \
            && ! security_error=$(check_service_executable_security "$selected"); then
            error "$description deferred binary is not safe for service use: $security_error"
            continue
        fi

        selected=$(realpath -m -- "$selected")
        printf -v "$target_variable" '%s' "$selected"
        success "$description deferred binary path selected: $selected"
        return
    done
}

configure_component_installation() {
    local method_variable=$1
    local binary_variable=$2
    local description=$3
    local target_variable=$4
    local target_path=${!target_variable}
    local selection
    local target_name=${target_path##*/}

    while true; do
        _out "$description installation method:"
        _out "  1) Use a pre-built binary"
        _out "  2) Build from source during installation"
        _out "  3) Defer installation and provide the binary later"
        prompt selection "Select $description installation method" "2"

        case "$selection" in
            1)
                printf -v "$method_variable" '%s' "prebuilt"
                configure_prebuilt_binary "$binary_variable" "$description"
                return
                ;;
            2)
                printf -v "$method_variable" '%s' "source"
                printf -v "$binary_variable" '%s' ""
                _out "$description will be built from source and installed at $target_path."
                return
                ;;
            3)
                printf -v "$method_variable" '%s' "deferred"
                printf -v "$binary_variable" '%s' ""
                configure_deferred_binary_path \
                    "$target_variable" "$description" "$target_path"
                target_path=${!target_variable}
                info "$description installation deferred."
                _out "Before starting services, provide an executable at: $target_path"
                _out "After building it, install the binary with:"
                _out "  sudo install -o root -g root -m 0755 /path/to/$target_name $target_path"
                return
                ;;
            *)
                error "Select 1, 2, or 3."
                ;;
        esac
    done
}

print_component_installation() {
    local description=$1
    local method=$2
    local binary=$3
    local target_path=$4

    case "$method" in
        prebuilt) _out "  $description: pre-built $binary -> $target_path" ;;
        source) _out "  $description: build from source -> $target_path" ;;
        deferred) _out "  $description: deferred; provide executable at $target_path" ;;
    esac
}

configure_node_software() {
    section "Node software configuration"

    SUMMIT_TARGET_BIN="/usr/local/bin/summit"
    RETH_TARGET_BIN="/usr/local/bin/seismic-reth"
    SUMMIT_SOURCE_REF="m/metrics"
    RETH_SOURCE_REF="feat/purpose-key-rotation-reth"
    SUMMIT_INSTALL_METHOD=""
    RETH_INSTALL_METHOD=""
    SUMMIT_BINARY=""
    RETH_BINARY=""

    configure_component_installation \
        SUMMIT_INSTALL_METHOD SUMMIT_BINARY "Summit" SUMMIT_TARGET_BIN
    _out ""
    configure_component_installation \
        RETH_INSTALL_METHOD RETH_BINARY "seismic-reth" RETH_TARGET_BIN

    _out ""
    _out "Node software:"
    print_component_installation \
        "Summit" "$SUMMIT_INSTALL_METHOD" "$SUMMIT_BINARY" "$SUMMIT_TARGET_BIN"
    print_component_installation \
        "Seismic Reth" "$RETH_INSTALL_METHOD" "$RETH_BINARY" "$RETH_TARGET_BIN"
}

configure_validator_software() {
    configure_node_software
}

configure_public_endpoint() {
    section "Public endpoint configuration"

    CONFIGURE_PUBLIC_ENDPOINT=false
    DOMAIN=""
    RATE_LIMIT_RPS=""
    RATE_LIMIT_BURST=""
    OPENRESTY_JWT_SECRET_PATH=${OPENRESTY_JWT_SECRET_PATH:-/etc/seismic/openresty-jwt-secret}

    if ! confirm "Configure a public HTTPS endpoint with OpenResty?"; then
        _out "Public HTTPS endpoint: disabled"
        return
    fi

    CONFIGURE_PUBLIC_ENDPOINT=true
    if [[ "${OPENRESTY_JWT_SECRET_PATH_CONFIGURED:-false}" != true ]] \
        && load_persisted_openresty_jwt_secret_path; then
        OPENRESTY_JWT_SECRET_PATH=$PERSISTED_OPENRESTY_JWT_SECRET_PATH
        info "Using the previously installed OpenResty JWT secret path as the default."
    fi

    while true; do
        prompt DOMAIN "Public domain" ""
        DOMAIN=${DOMAIN,,}

        if [[ ${#DOMAIN} -gt 253 ]]; then
            error "Public domain is longer than 253 characters."
            continue
        fi
        if [[ ! "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
            error "Invalid public domain: $DOMAIN"
            continue
        fi
        break
    done

    while true; do
        prompt RATE_LIMIT_RPS "Requests per second" "20"
        if [[ "$RATE_LIMIT_RPS" =~ ^[1-9][0-9]*$ ]]; then
            break
        fi
        error "Requests per second must be a positive integer."
    done

    while true; do
        prompt RATE_LIMIT_BURST "Rate-limit burst" "40"
        if [[ ! "$RATE_LIMIT_BURST" =~ ^[1-9][0-9]*$ ]]; then
            error "Rate-limit burst must be a positive integer."
            continue
        fi
        if ((RATE_LIMIT_BURST < RATE_LIMIT_RPS)); then
            error "Rate-limit burst must be greater than or equal to requests per second."
            continue
        fi
        break
    done

    configure_file_path \
        OPENRESTY_JWT_SECRET_PATH \
        "OpenResty JWT secret file" \
        "$OPENRESTY_JWT_SECRET_PATH"
    OPENRESTY_JWT_SECRET_PATH_CONFIGURED=true

    _out "Public HTTPS endpoint enabled: $CONFIGURE_PUBLIC_ENDPOINT"
    success "Public HTTPS endpoint configured: https://$DOMAIN"
    _out "Rate limit: $RATE_LIMIT_RPS requests/sec, burst $RATE_LIMIT_BURST"
    _out "JWT secret: $OPENRESTY_JWT_SECRET_PATH (contents hidden)"
}

validate_http_url() {
    local url=$1
    local port

    [[ "$url" =~ ^https?://(\[[0-9a-fA-F:]+\]|[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?)(:([0-9]{1,5}))?(/[^[:space:]\;\|\&\$\`]*)?$ ]] \
        || return 1

    port=${BASH_REMATCH[4]}
    [[ -z "$port" || (port -ge 1 && port -le 65535) ]]
}

validate_network_bootstrap_configuration() {
    if [[ -L "$GENESIS_PATH" ]]; then
        error "Summit genesis path must not be a symbolic link: $GENESIS_PATH"
        return 1
    fi
    if [[ ! -s "$GENESIS_PATH" ]]; then
        error "Summit genesis file does not exist or is empty: $GENESIS_PATH"
        return 1
    fi
    if ! run_as_service_user test -r "$GENESIS_PATH"; then
        error "Service user $SERVICE_USER cannot read the Summit genesis file: $GENESIS_PATH"
        return 1
    fi

    # Used later by Supervisor rendering after all libraries have been sourced.
    # shellcheck disable=SC2034
    BOOTNODE_ENODE=""
    if [[ -n "$BOOTNODE_RPC" ]] && ! fetch_bootnode_enode; then
        return 1
    fi
    return 0
}

configure_network_bootstrap() {
    section "Network bootstrap configuration"

    while true; do
        prompt GENESIS_PATH "Summit genesis TOML path" ""
        if [[ "$GENESIS_PATH" != /* ]]; then
            error "Summit genesis path must be absolute."
            continue
        fi
        if contains_unsafe_path_characters "$GENESIS_PATH"; then
            error "Summit genesis path contains unsupported whitespace or shell characters."
            continue
        fi
        if [[ -L "$GENESIS_PATH" ]]; then
            error "Summit genesis path must not be a symbolic link: $GENESIS_PATH"
            continue
        fi
        if [[ ! -s "$GENESIS_PATH" ]]; then
            error "Summit genesis file does not exist or is empty: $GENESIS_PATH"
            continue
        fi
        GENESIS_PATH=$(realpath -- "$GENESIS_PATH")
        if ! run_as_service_user test -r "$GENESIS_PATH"; then
            error "Service user $SERVICE_USER cannot read the Summit genesis file: $GENESIS_PATH"
            continue
        fi
        break
    done
    success "Summit genesis selected: $GENESIS_PATH"

    # Used later by Supervisor rendering after all libraries have been sourced.
    # shellcheck disable=SC2034
    BOOTNODE_ENODE=""
    while true; do
        prompt BOOTNODE_RPC "Bootnode RPC URL (blank for none)" ""
        if [[ -z "$BOOTNODE_RPC" ]]; then
            break
        fi
        if ! validate_http_url "$BOOTNODE_RPC"; then
            error "Bootnode RPC must be a valid http:// or https:// URL."
            continue
        fi
        BOOTNODE_RPC=${BOOTNODE_RPC%/}
        if ! fetch_bootnode_enode; then
            error "Enter another bootnode RPC URL, or leave it blank for no bootnode."
            continue
        fi
        success "Bootnode RPC selected and verified: $BOOTNODE_RPC"
        break
    done
}

configure_mdbx_reth_source() {
    local selected
    local libmdbx_dir

    RETH_SOURCE_DIR_FOR_MDBX=""
    _out "summit-checkpointer requires mdbx_copy built from the same vendored libmdbx revision as seismic-reth."
    if [[ "$RETH_INSTALL_METHOD" == "source" ]]; then
        _out "mdbx_copy will be built from $SERVICE_HOME/src/seismic-reth."
        return
    fi

    while true; do
        prompt selected \
            "Path to the seismic-reth source checkout used for this Reth binary (blank to skip mdbx_copy)" \
            ""
        if [[ -z "$selected" ]]; then
            warn "mdbx_copy will not be installed by this installer."
            warn "Before starting summit-checkpointer, provide a compatible executable at $MDBX_COPY_TARGET_BIN."
            return
        fi
        if [[ "$selected" != /* ]]; then
            error "seismic-reth source checkout path must be absolute."
            continue
        fi
        if [[ ! -d "$selected" ]]; then
            error "seismic-reth source checkout directory not found: $selected"
            continue
        fi

        selected=$(realpath -- "$selected")
        libmdbx_dir="$selected/crates/storage/libmdbx-rs/mdbx-sys/libmdbx"
        if [[ ! -e "$selected/.git" ]]; then
            error "Path is not a seismic-reth Git checkout: $selected"
            continue
        fi
        if [[ ! -f "$libmdbx_dir/VERSION.json" || ! -f "$libmdbx_dir/Makefile" ]]; then
            error "seismic-reth checkout does not contain the vendored libmdbx build files."
            continue
        fi

        RETH_SOURCE_DIR_FOR_MDBX="$selected"
        success "seismic-reth source checkout selected for mdbx_copy"
        return
    done
}

print_mdbx_copy_plan() {
    if [[ "$RETH_INSTALL_METHOD" == "source" ]]; then
        _out "  mdbx_copy: build from $SERVICE_HOME/src/seismic-reth -> $MDBX_COPY_TARGET_BIN"
    elif [[ -n "$RETH_SOURCE_DIR_FOR_MDBX" ]]; then
        _out "  mdbx_copy: build from $RETH_SOURCE_DIR_FOR_MDBX -> $MDBX_COPY_TARGET_BIN"
    else
        _out "  mdbx_copy: not installed; provide a compatible executable at $MDBX_COPY_TARGET_BIN before starting checkpointer"
    fi
}

configure_checkpointer() {
    section "Summit-checkpointer configuration"

    INSTALL_CHECKPOINTER=false
    CHECKPOINTER_TARGET_BIN="/usr/local/bin/summit-checkpointer"
    MDBX_COPY_TARGET_BIN="/usr/local/bin/mdbx_copy"
    CHECKPOINTER_CONFIG_PATH=${CHECKPOINTER_CONFIG_PATH:-/etc/seismic/summit-checkpointer.toml}
    CHECKPOINTER_SOURCE_REF="main"
    CHECKPOINTER_INSTALL_METHOD=""
    CHECKPOINTS_DIR=""
    CHECKPOINTER_BINARY=""
    RETH_SOURCE_DIR_FOR_MDBX=""
    if confirm "Enable summit-checkpointer?"; then
        INSTALL_CHECKPOINTER=true
        configure_directory \
            CHECKPOINTS_DIR \
            "Checkpointer output directory" \
            "/persistence/checkpoints" \
            "Stores Reth snapshots and Summit verification bundles produced by summit-checkpointer."
        configure_file_path \
            CHECKPOINTER_CONFIG_PATH \
            "Checkpointer configuration file" \
            "$CHECKPOINTER_CONFIG_PATH"
        configure_component_installation \
            CHECKPOINTER_INSTALL_METHOD \
            CHECKPOINTER_BINARY \
            "summit-checkpointer" \
            CHECKPOINTER_TARGET_BIN
        configure_mdbx_reth_source
    fi

    _out "Summit-checkpointer: $INSTALL_CHECKPOINTER"
    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        print_component_installation \
            "Checkpointer" \
            "$CHECKPOINTER_INSTALL_METHOD" \
            "$CHECKPOINTER_BINARY" \
            "$CHECKPOINTER_TARGET_BIN"
        if [[ "$CHECKPOINTER_INSTALL_METHOD" == "source" ]]; then
            _out "  Checkpointer source ref: $CHECKPOINTER_SOURCE_REF"
        fi
        _out "  Checkpointer config: $CHECKPOINTER_CONFIG_PATH"
        print_mdbx_copy_plan
    fi
}

validate_host_port() {
    local value=$1
    local port

    [[ "$value" =~ ^(\[[0-9a-fA-F:]+\]|[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?):([0-9]{1,5})$ ]] \
        || return 1

    port=${BASH_REMATCH[3]}
    ((port >= 1 && port <= 65535))
}

configure_custodian() {
    section "Custodian configuration"

    INSTALL_CUSTODIAN=false
    CUSTODIAN_DATA_DIR=""
    CUSTODIAN_TARGET_BIN="/usr/local/bin/seismic-centralized-custodian-service"
    CUSTODIAN_INSTALL_METHOD=""
    CUSTODIAN_BINARY=""
    CUSTODIAN_SOCKET=""
    COUNCIL_LISTEN=""
    COUNCIL_ADDRESS=""
    CUSTODIAN_CHAIN_ID=""
    PARENT_CUSTODIAN=""
    CUSTODIAN_REQUIRED_SUMMIT_REF="m/metrics"
    CUSTODIAN_REQUIRED_RETH_REF="feat/purpose-key-rotation-reth"
    CUSTODIAN_SOURCE_REF="d/centralized-custodian"

    if ! confirm "Enable Centralized Custodian?"; then
        _out "Centralized Custodian: $INSTALL_CUSTODIAN"
        return
    fi
    INSTALL_CUSTODIAN=true

    configure_directory \
        CUSTODIAN_DATA_DIR \
        "Custodian data directory" \
        "/persistence/custodian" \
        "Stores the Custodian root key and epoch-key deliveries."

    while true; do
        prompt CUSTODIAN_SOCKET "Custodian Unix socket path" "/tmp/custodian.sock"
        if [[ "$CUSTODIAN_SOCKET" != /* ]]; then
            error "Custodian Unix socket path must be absolute."
            continue
        fi
        if [[ "$CUSTODIAN_SOCKET" == "/" ]]; then
            error "The filesystem root cannot be used as the Custodian Unix socket path."
            continue
        fi
        if contains_unsafe_path_characters "$CUSTODIAN_SOCKET"; then
            error "Custodian Unix socket path contains unsupported whitespace or shell characters."
            continue
        fi
        CUSTODIAN_SOCKET=$(realpath -m -- "$CUSTODIAN_SOCKET")
        if [[ ${#CUSTODIAN_SOCKET} -gt 107 ]]; then
            error "Custodian Unix socket path exceeds the Linux limit of 107 characters."
            continue
        fi
        if [[ -e "$CUSTODIAN_SOCKET" && ! -S "$CUSTODIAN_SOCKET" ]]; then
            error "Custodian Unix socket path exists but is not a socket: $CUSTODIAN_SOCKET"
            continue
        fi
        break
    done
    success "Custodian Unix socket selected: $CUSTODIAN_SOCKET"

    if [[ "${NODE_ROLE:-validator}" == "observer" ]]; then
        _out "Enter the Custodian council endpoint running on the parent validator."
        _out "The observer uses it to fetch or verify the root key and synchronize epoch-key deliveries."
        while true; do
            prompt PARENT_CUSTODIAN \
                "Parent validator Custodian council endpoint (host:port; default port 7876)" \
                ""
            if validate_host_port "$PARENT_CUSTODIAN"; then
                break
            fi
            error "Parent validator Custodian council endpoint must be host:port with a valid port."
        done
        _out "The observer Custodian will fetch and verify its root key through $PARENT_CUSTODIAN."
        warn "The parent Custodian connection transports root-key and epoch-key material."
        warn "Use a private network or protect the connection with a TLS tunnel."
    else
        _out "Custodian will use the publicly known shared default root key."
        warn "The shared default makes epoch-0 purpose keys public."
    fi

    while true; do
        prompt COUNCIL_LISTEN "Custodian council listen address" "0.0.0.0:7876"
        if validate_host_port "$COUNCIL_LISTEN"; then
            break
        fi
        error "Custodian council listen address must be host:port with a valid port."
    done

    while true; do
        prompt COUNCIL_ADDRESS \
            "Custodian council address" \
            "0xd412c5ecd343e264381ff15afc0ad78a67b79f35"
        if [[ "$COUNCIL_ADDRESS" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
            break
        fi
        error "Custodian council address must be a 20-byte 0x-prefixed EVM address."
    done

    _out "The Custodian chain ID is the execution-layer EVM chain ID used by seismic-reth."
    while true; do
        prompt CUSTODIAN_CHAIN_ID \
            "EVM chain ID for the seismic-reth network" ""
        if [[ "$CUSTODIAN_CHAIN_ID" =~ ^[1-9][0-9]*$ ]]; then
            break
        fi
        error "Custodian chain ID must be a positive integer."
    done

    configure_component_installation \
        CUSTODIAN_INSTALL_METHOD \
        CUSTODIAN_BINARY \
        "Centralized Custodian" \
        CUSTODIAN_TARGET_BIN

    warn "Custodian requires Summit compatible with $CUSTODIAN_REQUIRED_SUMMIT_REF."
    warn "Custodian requires seismic-reth compatible with $CUSTODIAN_REQUIRED_RETH_REF."

    _out "Centralized Custodian: $INSTALL_CUSTODIAN"
    _out "  Data:       $CUSTODIAN_DATA_DIR"
    _out "  Socket:     $CUSTODIAN_SOCKET"
    if [[ "${NODE_ROLE:-validator}" == "observer" ]]; then
        _out "  Root key:   fetched and verified through parent Custodian"
        _out "  Parent:     $PARENT_CUSTODIAN"
    else
        _out "  Root key:   publicly known shared default"
    fi
    _out "  Council:    $COUNCIL_LISTEN ($COUNCIL_ADDRESS)"
    _out "  Chain ID:   $CUSTODIAN_CHAIN_ID"
    print_component_installation \
        "Custodian" "$CUSTODIAN_INSTALL_METHOD" \
        "$CUSTODIAN_BINARY" "$CUSTODIAN_TARGET_BIN"
    if [[ "$CUSTODIAN_INSTALL_METHOD" == "source" ]]; then
        _out "  Source ref: $CUSTODIAN_SOURCE_REF"
    fi
}

configure_directories() {
    section "Data directory configuration"

    configure_directory \
        RETH_DATA_DIR \
        "Reth data directory" \
        "/persistence/reth" \
        "Stores the Reth execution database, static files, and other execution-layer state."
    configure_directory \
        SUMMIT_DATA_DIR \
        "Summit data directory" \
        "/persistence/summit" \
        "Stores Summit's mutable consensus store and finalized consensus state."
    configure_directory \
        VALIDATOR_KEYS_DIR \
        "Validator keys directory" \
        "/persistence/keys" \
        "Stores the Reth P2P identity and Summit validator node and consensus keys."

    RETH_P2P_KEY_PATH="$VALIDATOR_KEYS_DIR/reth/p2p-key"
    SUMMIT_KEYS_DIR="$VALIDATOR_KEYS_DIR/summit"
}

print_configuration_summary() {
    section "Validator configuration summary"

    _out "Service user:"
    _out "  User:  $SERVICE_USER"
    _out "  Group: $SERVICE_GROUP"
    _out "  Home:  $SERVICE_HOME"

    _out "Directories:"
    _out "  Reth data:      $RETH_DATA_DIR"
    _out "  Summit data:    $SUMMIT_DATA_DIR"
    _out "  Validator keys: $VALIDATOR_KEYS_DIR"
    _out "    Reth P2P key:  $RETH_P2P_KEY_PATH"
    _out "    Summit keys:   $SUMMIT_KEYS_DIR"
    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        _out "  Checkpointer:   $CHECKPOINTS_DIR"
    else
        _out "  Checkpointer:   disabled"
    fi
    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        _out "  Custodian:      $CUSTODIAN_DATA_DIR"
    else
        _out "  Custodian:      disabled"
    fi

    _out "Public endpoint:"
    if [[ "$CONFIGURE_PUBLIC_ENDPOINT" == true ]]; then
        _out "  URL:        https://$DOMAIN"
        _out "  Rate limit: $RATE_LIMIT_RPS requests/sec, burst $RATE_LIMIT_BURST"
        _out "  JWT secret: $OPENRESTY_JWT_SECRET_PATH (contents hidden)"
    else
        _out "  Disabled"
    fi

    _out "Network bootstrap:"
    _out "  Genesis: $GENESIS_PATH"
    if [[ -n "$BOOTNODE_RPC" ]]; then
        _out "  Bootnode RPC: $BOOTNODE_RPC"
        _out "  Reth enode:   verified during configuration and revalidated during installation"
    else
        _out "  Bootnode RPC: none"
        _out "  Reth enode:   none"
    fi

    _out "Validator software:"
    print_component_installation \
        "Summit" "$SUMMIT_INSTALL_METHOD" "$SUMMIT_BINARY" "$SUMMIT_TARGET_BIN"
    print_component_installation \
        "Seismic Reth" "$RETH_INSTALL_METHOD" "$RETH_BINARY" "$RETH_TARGET_BIN"
    if [[ "$SUMMIT_INSTALL_METHOD" == "source" ]]; then
        _out "  Summit source ref: ${SUMMIT_SOURCE_REF:-repository default branch}"
    fi
    if [[ "$RETH_INSTALL_METHOD" == "source" ]]; then
        _out "  Reth source ref:   ${RETH_SOURCE_REF:-repository default branch}"
    fi

    _out "Summit-checkpointer:"
    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        _out "  Enabled: true"
        print_component_installation \
            "Checkpointer" \
            "$CHECKPOINTER_INSTALL_METHOD" \
            "$CHECKPOINTER_BINARY" \
            "$CHECKPOINTER_TARGET_BIN"
        if [[ "$CHECKPOINTER_INSTALL_METHOD" == "source" ]]; then
            _out "  Source ref: $CHECKPOINTER_SOURCE_REF"
        fi
        _out "  Config: $CHECKPOINTER_CONFIG_PATH"
        print_mdbx_copy_plan
    else
        _out "  Enabled: false"
    fi

    _out "Centralized Custodian:"
    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        _out "  Enabled: true"
        _out "  Data:    $CUSTODIAN_DATA_DIR"
        _out "  Socket:  $CUSTODIAN_SOCKET"
        if [[ "${NODE_ROLE:-validator}" == "observer" ]]; then
            _out "  Root key: fetched and verified through parent Custodian"
            _out "  Parent Custodian: $PARENT_CUSTODIAN"
        else
            _out "  Root key: publicly known shared default"
        fi
        _out "  Council listen:  $COUNCIL_LISTEN"
        _out "  Council address: $COUNCIL_ADDRESS"
        _out "  Chain ID:        $CUSTODIAN_CHAIN_ID"
        print_component_installation \
            "Custodian" "$CUSTODIAN_INSTALL_METHOD" \
            "$CUSTODIAN_BINARY" "$CUSTODIAN_TARGET_BIN"
        _out "  Required Summit compatibility: $CUSTODIAN_REQUIRED_SUMMIT_REF"
        _out "  Required Reth compatibility:   $CUSTODIAN_REQUIRED_RETH_REF"
        if [[ "$CUSTODIAN_INSTALL_METHOD" == "source" ]]; then
            _out "  Custodian source ref:          $CUSTODIAN_SOURCE_REF"
        fi
    else
        _out "  Enabled: false"
    fi

    print_system_package_plan
}

review_configuration() {
    local selection

    while true; do
        print_configuration_summary
        _out ""
        _out "What would you like to do?"
        _out "  1) Edit service user"
        _out "  2) Edit directories"
        _out "  3) Edit public endpoint"
        _out "  4) Edit network bootstrap"
        _out "  5) Edit validator software"
        _out "  6) Edit summit-checkpointer"
        _out "  7) Edit Centralized Custodian"
        _out "  8) Accept configuration"
        _out "  9) Cancel"
        prompt selection "Select an action" "8"

        case "$selection" in
            1) configure_service_user ;;
            2) configure_directories ;;
            3) configure_public_endpoint ;;
            4) configure_network_bootstrap ;;
            5) configure_validator_software ;;
            6) configure_checkpointer ;;
            7) configure_custodian ;;
            8)
                if ! validate_network_bootstrap_configuration; then
                    warn "Network bootstrap validation failed; please configure it again."
                    configure_network_bootstrap
                    continue
                fi
                if confirm "Accept this configuration?"; then
                    success "Configuration accepted"
                    return
                fi
                ;;
            9)
                info "Configuration cancelled; no installation changes were made."
                exit 0
                ;;
            *) error "Select a number from 1 through 9." ;;
        esac
    done
}

configure() {
    NODE_ROLE="validator"
    section "Configuration"
    configure_service_user
    configure_directories
    configure_public_endpoint
    configure_network_bootstrap
    configure_validator_software
    configure_checkpointer
    configure_custodian
    review_configuration
}

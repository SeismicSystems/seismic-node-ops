#!/usr/bin/env bash

# Observer-specific interactive configuration.

validate_observer_socket_address() {
    local value=$1

    python3 - "$value" <<'PY' >/dev/null 2>&1
import ipaddress
import sys

value = sys.argv[1]
try:
    if value.startswith("["):
        end = value.index("]")
        host = value[1:end]
        if value[end + 1 : end + 2] != ":":
            raise ValueError
        port = int(value[end + 2 :])
    else:
        host, raw_port = value.rsplit(":", 1)
        port = int(raw_port)
    ipaddress.ip_address(host)
    if not 1 <= port <= 65535:
        raise ValueError
except (ValueError, IndexError):
    raise SystemExit(1)
PY
}

normalize_observer_public_key() {
    local value=$1

    value=${value#0x}
    printf '%s\n' "${value,,}"
}

configure_observer_assignment() {
    local selected

    section "Observer assignment configuration"

    while true; do
        prompt OBSERVER_PARENT_NODE_PUBLIC_KEY \
            "Parent validator Summit node public key" ""
        if [[ "$OBSERVER_PARENT_NODE_PUBLIC_KEY" =~ ^(0x)?[0-9a-fA-F]{64}$ ]]; then
            OBSERVER_PARENT_NODE_PUBLIC_KEY=$(normalize_observer_public_key \
                "$OBSERVER_PARENT_NODE_PUBLIC_KEY")
            break
        fi
        error "Parent node public key must be a 32-byte hexadecimal value."
    done

    while true; do
        prompt OBSERVER_INDEX "Observer derivation index" "0"
        if [[ "$OBSERVER_INDEX" =~ ^[0-9]{1,3}$ ]] \
            && ((10#$OBSERVER_INDEX <= 255)); then
            OBSERVER_INDEX=$((10#$OBSERVER_INDEX))
            break
        fi
        error "Observer index must be an integer from 0 through 255."
    done

    while true; do
        prompt OBSERVER_PUBLIC_ADDRESS \
            "Observer public Summit P2P address (IP:port; default Summit port 18551)" ""
        if validate_observer_socket_address "$OBSERVER_PUBLIC_ADDRESS"; then
            break
        fi
        error "Observer public address must be an IPv4:port or [IPv6]:port socket address."
    done
    OBSERVER_P2P_PORT=${OBSERVER_PUBLIC_ADDRESS##*:}

    OBSERVER_BOOTSTRAPPERS_SOURCE=""
    while true; do
        prompt selected "Summit bootstrappers TOML path (blank for none)" ""
        if [[ -z "$selected" ]]; then
            break
        fi
        if [[ "$selected" != /* ]]; then
            error "Summit bootstrappers TOML path must be absolute."
            continue
        fi
        if contains_unsafe_path_characters "$selected"; then
            error "Summit bootstrappers TOML path contains unsupported whitespace or shell characters."
            continue
        fi
        if [[ -L "$selected" || ! -s "$selected" ]]; then
            error "Summit bootstrappers TOML must be a non-empty regular file and not a symbolic link."
            continue
        fi
        selected=$(realpath -- "$selected")
        if ! run_as_service_user test -r "$selected"; then
            error "Service user $SERVICE_USER cannot read the Summit bootstrappers TOML: $selected"
            continue
        fi
        OBSERVER_BOOTSTRAPPERS_SOURCE="$selected"
        break
    done

    _out "Observer assignment:"
    _out "  Parent key: $OBSERVER_PARENT_NODE_PUBLIC_KEY"
    _out "  Index:      $OBSERVER_INDEX"
    _out "  Public P2P: $OBSERVER_PUBLIC_ADDRESS"
    _out "  Bootstrappers: ${OBSERVER_BOOTSTRAPPERS_SOURCE:-none}"
    warn "The installer does not verify whether the parent is a genesis or current validator."
}

configure_observer_directories() {
    section "Observer data directory configuration"

    configure_directory \
        RETH_DATA_DIR \
        "Reth data directory" \
        "/persistence/reth" \
        "Stores the Reth execution database, static files, and other execution-layer state."
    configure_directory \
        SUMMIT_DATA_DIR \
        "Summit data directory" \
        "/persistence/summit" \
        "Stores the observer's mutable Summit consensus state."
    configure_directory \
        VALIDATOR_KEYS_DIR \
        "Observer keys directory" \
        "/persistence/keys" \
        "Stores the Reth P2P identity, observer consensus key, and provisioned parent node key."

    RETH_P2P_KEY_PATH="$VALIDATOR_KEYS_DIR/reth/p2p-key"
    SUMMIT_KEYS_DIR="$VALIDATOR_KEYS_DIR/summit"
    OBSERVER_ASSIGNMENT_FILE="$SUMMIT_KEYS_DIR/observer-assignment"
    OBSERVER_BOOTSTRAPPERS_FILE="$SUMMIT_KEYS_DIR/bootstrappers.toml"
    OBSERVER_STORE_DIR="$SUMMIT_DATA_DIR"
    OBSERVER_CRITICAL_LOG_DIR="/var/log/seismic-observer/critical"
}

print_observer_configuration_summary() {
    section "Observer configuration summary"

    _out "Service user: $SERVICE_USER ($SERVICE_GROUP)"
    _out "Service home: $SERVICE_HOME"
    _out "Directories:"
    _out "  Reth data:             $RETH_DATA_DIR"
    _out "  Summit data:           $SUMMIT_DATA_DIR"
    _out "  Observer keys:         $VALIDATOR_KEYS_DIR"
    _out "  Summit store:          $OBSERVER_STORE_DIR"

    _out "Observer assignment:"
    _out "  Parent key: $OBSERVER_PARENT_NODE_PUBLIC_KEY"
    _out "  Index:      $OBSERVER_INDEX"
    _out "  Public P2P: $OBSERVER_PUBLIC_ADDRESS"
    _out "  Bootstrappers: ${OBSERVER_BOOTSTRAPPERS_SOURCE:-none}"

    _out "Network bootstrap:"
    _out "  Genesis: $GENESIS_PATH"
    _out "  Reth bootnode RPC: ${BOOTNODE_RPC:-none}"

    _out "Public endpoint:"
    if [[ "$CONFIGURE_PUBLIC_ENDPOINT" == true ]]; then
        _out "  https://$DOMAIN"
        _out "  Rate limit: $RATE_LIMIT_RPS requests/sec, burst $RATE_LIMIT_BURST"
    else
        _out "  Disabled"
    fi

    _out "Node software:"
    print_component_installation \
        "Summit" "$SUMMIT_INSTALL_METHOD" "$SUMMIT_BINARY" "$SUMMIT_TARGET_BIN"
    print_component_installation \
        "Seismic Reth" "$RETH_INSTALL_METHOD" "$RETH_BINARY" "$RETH_TARGET_BIN"

    _out "Summit-checkpointer: $INSTALL_CHECKPOINTER"
    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        print_component_installation \
            "Checkpointer" "$CHECKPOINTER_INSTALL_METHOD" \
            "$CHECKPOINTER_BINARY" "$CHECKPOINTER_TARGET_BIN"
        _out "  Config: $CHECKPOINTER_CONFIG_PATH"
        print_mdbx_copy_plan
    fi

    _out "Centralized Custodian: $INSTALL_CUSTODIAN"
    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        _out "  Parent Custodian: $PARENT_CUSTODIAN"
        _out "  Root key: fetched and verified through the parent"
        _out "  Council: $COUNCIL_LISTEN ($COUNCIL_ADDRESS)"
        _out "  Chain ID: $CUSTODIAN_CHAIN_ID"
        print_component_installation \
            "Custodian" "$CUSTODIAN_INSTALL_METHOD" \
            "$CUSTODIAN_BINARY" "$CUSTODIAN_TARGET_BIN"
        if [[ "$CUSTODIAN_INSTALL_METHOD" == "source" ]]; then
            _out "  Source ref: $CUSTODIAN_SOURCE_REF"
        fi
    fi

    print_system_package_plan
}

review_observer_configuration() {
    local selection

    while true; do
        print_observer_configuration_summary
        _out ""
        _out "What would you like to do?"
        _out "  1) Edit service user"
        _out "  2) Edit directories"
        _out "  3) Edit public endpoint"
        _out "  4) Edit network bootstrap"
        _out "  5) Edit observer assignment"
        _out "  6) Edit node software"
        _out "  7) Edit summit-checkpointer"
        _out "  8) Edit Centralized Custodian"
        _out "  9) Accept configuration"
        _out " 10) Cancel"
        prompt selection "Select an action" "9"

        case "$selection" in
            1) configure_service_user ;;
            2) configure_observer_directories ;;
            3) configure_public_endpoint ;;
            4) configure_network_bootstrap ;;
            5) configure_observer_assignment ;;
            6) configure_node_software ;;
            7) configure_checkpointer ;;
            8) configure_custodian ;;
            9)
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
            10)
                info "Configuration cancelled; no installation changes were made."
                exit 0
                ;;
            *) error "Select a number from 1 through 10." ;;
        esac
    done
}

configure_observer() {
    NODE_ROLE="observer"
    section "Configuration"
    configure_service_user
    configure_observer_directories
    configure_public_endpoint
    configure_network_bootstrap
    configure_observer_assignment
    configure_node_software
    configure_checkpointer
    configure_custodian
    review_observer_configuration
}

#!/usr/bin/env bash

# Supervisor configuration rendering without service activation.

SUPERVISOR_CONFIG_PATH="/etc/supervisor/conf.d/seismic-validator.conf"
CHECKPOINTER_CONFIG_PATH="/etc/seismic/summit-checkpointer.toml"
SUPERVISOR_LOG_DIR="/var/log/seismic-validator"

normalize_bootnode_rpc_base() {
    local url=$1
    local scheme=${url%%:*}
    local remainder=${url#*://}
    local authority=${remainder%%/*}

    printf '%s://%s\n' "$scheme" "$authority"
}

fetch_bootnode_enode() {
    local base
    local response
    local enode
    local enode_port
    local enode_pattern='"enode"[[:space:]]*:[[:space:]]*"([^"]*)"'

    [[ -n "$BOOTNODE_RPC" ]] || return
    if ! command -v curl >/dev/null 2>&1; then
        error "curl is required to verify the bootnode RPC before installation."
        return 1
    fi
    base=$(normalize_bootnode_rpc_base "$BOOTNODE_RPC")
    info "Fetching the Reth enode from $base/rpc..."

    if ! response=$(curl -fsS --max-time 30 -X POST "$base/rpc" \
        -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","method":"seismic_nodeInfo","params":[],"id":1}'); then
        error "Could not query the bootnode Reth RPC at $base/rpc"
        return 1
    fi
    if [[ "$response" =~ $enode_pattern ]]; then
        enode=${BASH_REMATCH[1]}
    else
        error "Bootnode RPC did not return result.enode."
        return 1
    fi
    if [[ ! "$enode" =~ ^enode://[0-9a-fA-F]{128}@[A-Za-z0-9.:-]+:[0-9]+$ ]]; then
        error "Bootnode RPC returned a malformed Reth enode."
        return 1
    fi
    enode_port=${enode##*:}
    if ((enode_port < 1 || enode_port > 65535)); then
        error "Bootnode RPC returned a Reth enode with an invalid port."
        return 1
    fi

    BOOTNODE_ENODE="$enode"
    success "Bootnode Reth enode discovered: ${BOOTNODE_ENODE:0:60}..."
}

validate_supervisor_runtime_inputs() {
    [[ ! -L "$GENESIS_PATH" ]] \
        || die "Summit genesis path became a symbolic link: $GENESIS_PATH"
    [[ -s "$GENESIS_PATH" ]] \
        || die "Summit genesis file is missing or empty: $GENESIS_PATH"
    run_as_service_user test -r "$GENESIS_PATH" \
        || die "Service user cannot read the Summit genesis file: $GENESIS_PATH"

    [[ -d /etc/supervisor/conf.d ]] \
        || die "Supervisor configuration directory is missing: /etc/supervisor/conf.d"
}

validate_supervisor_templates() {
    local template_root="$TEMPLATES_DIR/supervisor"
    local required=(
        "$template_root/reth.conf"
        "$template_root/summit-validator.conf"
        "$template_root/deposit-rpc.conf"
        "$template_root/checkpointer.conf"
        "$template_root/custodian.conf"
        "$template_root/checkpointer.toml"
    )
    local path

    for path in "${required[@]}"; do
        [[ -f "$path" ]] || die "Required Supervisor template not found: $path"
    done
}

render_reth_supervisor_config() {
    local template="$TEMPLATES_DIR/supervisor/reth.conf"
    local conf
    local bootnode_argument=""
    local purpose_key_arguments="--seismic.purpose-keys-source built-in"
    local rpc_bind="127.0.0.1"

    [[ -z "$BOOTNODE_ENODE" ]] \
        || bootnode_argument="--bootnodes $BOOTNODE_ENODE"
    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        purpose_key_arguments="--seismic.purpose-keys-source custodian
    --seismic.custodian.socket $CUSTODIAN_SOCKET"
    fi

    conf=$(<"$template")
    conf=${conf//RETH_BINARY_PLACEHOLDER/$RETH_TARGET_BIN}
    conf=${conf//RETH_P2P_KEY_PLACEHOLDER/$RETH_P2P_KEY_PATH}
    conf=${conf//RETH_RPC_BIND_PLACEHOLDER/$rpc_bind}
    conf=${conf//RETH_DATA_DIR_PLACEHOLDER/$RETH_DATA_DIR}
    conf=${conf//PURPOSE_KEYS_ARGUMENTS_PLACEHOLDER/$purpose_key_arguments}
    conf=${conf//BOOTNODE_ARGUMENT_PLACEHOLDER/$bootnode_argument}
    conf=${conf//SERVICE_USER_PLACEHOLDER/$SERVICE_USER}
    conf=${conf//SUPERVISOR_LOG_DIR_PLACEHOLDER/$SUPERVISOR_LOG_DIR}
    printf '%s\n' "$conf"
}

render_summit_validator_supervisor_config() {
    local template="$TEMPLATES_DIR/supervisor/summit-validator.conf"
    local conf
    local summit_bind="127.0.0.1"

    conf=$(<"$template")
    conf=${conf//SUMMIT_BINARY_PLACEHOLDER/$SUMMIT_TARGET_BIN}
    conf=${conf//GENESIS_PATH_PLACEHOLDER/$GENESIS_PATH}
    conf=${conf//SUMMIT_KEYS_DIR_PLACEHOLDER/$SUMMIT_KEYS_DIR}
    conf=${conf//SUMMIT_DATA_DIR_PLACEHOLDER/$SUMMIT_DATA_DIR}
    conf=${conf//SUMMIT_RPC_BIND_PLACEHOLDER/$summit_bind}
    conf=${conf//SUMMIT_PROM_BIND_PLACEHOLDER/$summit_bind}
    conf=${conf//SERVICE_USER_PLACEHOLDER/$SERVICE_USER}
    conf=${conf//SUPERVISOR_LOG_DIR_PLACEHOLDER/$SUPERVISOR_LOG_DIR}
    printf '%s\n' "$conf"
}

render_validator_supervisor_config() {
    render_reth_supervisor_config
    render_summit_validator_supervisor_config
}

render_deposit_rpc_supervisor_config() {
    local template="$TEMPLATES_DIR/supervisor/deposit-rpc.conf"
    local conf

    conf=$(<"$template")
    conf=${conf//SUMMIT_BINARY_PLACEHOLDER/$SUMMIT_TARGET_BIN}
    conf=${conf//GENESIS_PATH_PLACEHOLDER/$GENESIS_PATH}
    conf=${conf//SUMMIT_KEYS_DIR_PLACEHOLDER/$SUMMIT_KEYS_DIR}
    conf=${conf//SERVICE_USER_PLACEHOLDER/$SERVICE_USER}
    conf=${conf//SUPERVISOR_LOG_DIR_PLACEHOLDER/$SUPERVISOR_LOG_DIR}
    printf '%s\n' "$conf"
}

render_checkpointer_supervisor_config() {
    local template="$TEMPLATES_DIR/supervisor/checkpointer.conf"
    local conf

    conf=$(<"$template")
    conf=${conf//CHECKPOINTER_BINARY_PLACEHOLDER/$CHECKPOINTER_TARGET_BIN}
    conf=${conf//CHECKPOINTER_CONFIG_PLACEHOLDER/$CHECKPOINTER_CONFIG_PATH}
    conf=${conf//SERVICE_USER_PLACEHOLDER/$SERVICE_USER}
    conf=${conf//SUPERVISOR_LOG_DIR_PLACEHOLDER/$SUPERVISOR_LOG_DIR}
    printf '%s\n' "$conf"
}

render_custodian_supervisor_config() {
    local template="$TEMPLATES_DIR/supervisor/custodian.conf"
    local conf

    conf=$(<"$template")
    conf=${conf//CUSTODIAN_BINARY_PLACEHOLDER/$CUSTODIAN_TARGET_BIN}
    conf=${conf//CUSTODIAN_SOCKET_PLACEHOLDER/$CUSTODIAN_SOCKET}
    conf=${conf//CUSTODIAN_ROOT_KEY_PLACEHOLDER/$CUSTODIAN_DATA_DIR/root.key}
    conf=${conf//CUSTODIAN_DELIVERY_DIR_PLACEHOLDER/$CUSTODIAN_DATA_DIR/deliveries}
    conf=${conf//COUNCIL_LISTEN_PLACEHOLDER/$COUNCIL_LISTEN}
    conf=${conf//COUNCIL_ADDRESS_PLACEHOLDER/$COUNCIL_ADDRESS}
    conf=${conf//CUSTODIAN_CHAIN_ID_PLACEHOLDER/$CUSTODIAN_CHAIN_ID}
    conf=${conf//SUMMIT_KEYS_DIR_PLACEHOLDER/$SUMMIT_KEYS_DIR}
    conf=${conf//SERVICE_USER_PLACEHOLDER/$SERVICE_USER}
    conf=${conf//SUPERVISOR_LOG_DIR_PLACEHOLDER/$SUPERVISOR_LOG_DIR}
    printf '%s\n' "$conf"
}

render_checkpointer_toml() {
    local template="$TEMPLATES_DIR/supervisor/checkpointer.toml"
    local conf

    conf=$(<"$template")
    conf=${conf//RETH_DB_PATH_PLACEHOLDER/$RETH_DATA_DIR/db}
    conf=${conf//CHECKPOINTS_DIR_PLACEHOLDER/$CHECKPOINTS_DIR}
    conf=${conf//MDBX_COPY_BINARY_PLACEHOLDER/$MDBX_COPY_TARGET_BIN}
    conf=${conf//RETH_BINARY_PLACEHOLDER/$RETH_TARGET_BIN}
    conf=${conf//CHECKPOINTER_STATE_FILE_PLACEHOLDER/$CHECKPOINTS_DIR/checkpointer-state.cbor}
    printf '%s\n' "$conf"
}

find_conflicting_supervisor_config() {
    local path

    while IFS= read -r -d '' path; do
        [[ "$path" == "$SUPERVISOR_CONFIG_PATH" ]] && continue
        if grep -Eq '^\[program:(reth|summit|summit-observer|summit-observer-checkpoint|summit-deposit-rpc|checkpointer|custodian)\]' "$path"; then
            printf '%s\n' "$path"
            return 0
        fi
    done < <(find /etc/supervisor/conf.d -maxdepth 1 -type f -print0)
    return 1
}

prepare_supervisor_logs() {
    local names=(summit-deposit-rpc reth summit)
    local name

    [[ "$INSTALL_CHECKPOINTER" != true ]] || names+=(checkpointer)
    [[ "$INSTALL_CUSTODIAN" != true ]] || names+=(custodian)

    [[ ! -L "$SUPERVISOR_LOG_DIR" ]] \
        || die "Supervisor log directory must not be a symbolic link: $SUPERVISOR_LOG_DIR"
    install -d -o root -g root -m 0755 "$SUPERVISOR_LOG_DIR"
    for name in "${names[@]}"; do
        [[ ! -L "$SUPERVISOR_LOG_DIR/$name.log" &&
            ! -L "$SUPERVISOR_LOG_DIR/$name.err" ]] \
            || die "Supervisor log files must not be symbolic links for service: $name"
        touch "$SUPERVISOR_LOG_DIR/$name.log" "$SUPERVISOR_LOG_DIR/$name.err"
        chown root:root "$SUPERVISOR_LOG_DIR/$name.log" "$SUPERVISOR_LOG_DIR/$name.err"
        chmod 0644 "$SUPERVISOR_LOG_DIR/$name.log" "$SUPERVISOR_LOG_DIR/$name.err"
    done
}

deploy_supervisor_configuration() {
    local staging
    local conflict

    section "Deploying Supervisor configuration"
    validate_supervisor_runtime_inputs
    validate_supervisor_templates

    BOOTNODE_ENODE=""
    if [[ -n "$BOOTNODE_RPC" ]] && ! fetch_bootnode_enode; then
        die "Bootnode RPC validation failed after configuration acceptance."
    fi

    conflict=$(find_conflicting_supervisor_config || true)
    [[ -z "$conflict" ]] \
        || die "Another Supervisor file already defines Seismic services: $conflict"
    [[ ! -L "$SUPERVISOR_CONFIG_PATH" ]] \
        || die "Supervisor target must not be a symbolic link: $SUPERVISOR_CONFIG_PATH"
    [[ ! -L "$CHECKPOINTER_CONFIG_PATH" ]] \
        || die "Checkpointer configuration target must not be a symbolic link: $CHECKPOINTER_CONFIG_PATH"

    staging=$(mktemp -d)
    render_deposit_rpc_supervisor_config >"$staging/seismic-validator.conf"
    render_reth_supervisor_config >>"$staging/seismic-validator.conf"
    render_summit_validator_supervisor_config >>"$staging/seismic-validator.conf"
    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        render_checkpointer_supervisor_config \
            >>"$staging/seismic-validator.conf"
        render_checkpointer_toml >"$staging/summit-checkpointer.toml"
    fi
    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        render_custodian_supervisor_config \
            >>"$staging/seismic-validator.conf"
    fi

    if grep -R -n '_PLACEHOLDER' "$staging" >>"$LOG_FILE" 2>&1; then
        rm -rf -- "$staging"
        die "Rendered Supervisor configuration still contains placeholders; see $LOG_FILE"
    fi

    prepare_supervisor_logs
    install -d -o root -g root -m 0755 /etc/seismic
    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        install -o root -g root -m 0644 \
            "$staging/summit-checkpointer.toml" "$CHECKPOINTER_CONFIG_PATH"
    else
        rm -f -- "$CHECKPOINTER_CONFIG_PATH"
    fi
    install -o root -g root -m 0644 \
        "$staging/seismic-validator.conf" "$SUPERVISOR_CONFIG_PATH"
    rm -rf -- "$staging"

    success "Supervisor configuration deployed: $SUPERVISOR_CONFIG_PATH"
    info "Supervisor was not started, enabled, reread, or updated."
}

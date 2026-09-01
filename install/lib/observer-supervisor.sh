#!/usr/bin/env bash

# Observer Supervisor rendering without service activation.

SUPERVISOR_CONFIG_PATH="/etc/supervisor/conf.d/seismic-observer.conf"
SUPERVISOR_LOG_DIR="/var/log/seismic-observer"

validate_observer_supervisor_templates() {
    local template_root="$TEMPLATES_DIR/supervisor"
    local required=(
        "$template_root/reth.conf"
        "$template_root/observer.conf"
        "$template_root/observer-custodian.conf"
        "$template_root/checkpointer.conf"
        "$template_root/checkpointer.toml"
    )
    local path

    for path in "${required[@]}"; do
        [[ -f "$path" ]] || die "Required observer Supervisor template not found: $path"
    done
}

validate_observer_supervisor_runtime_inputs() {
    validate_supervisor_runtime_inputs

    [[ -f "$OBSERVER_ASSIGNMENT_FILE" && ! -L "$OBSERVER_ASSIGNMENT_FILE" ]] \
        || die "Observer assignment marker is missing: $OBSERVER_ASSIGNMENT_FILE"
    [[ "$(<"$OBSERVER_ASSIGNMENT_FILE")" == "$OBSERVER_PARENT_NODE_PUBLIC_KEY:$OBSERVER_INDEX" ]] \
        || die "Observer assignment marker does not match the accepted configuration."
}

render_observer_supervisor_program() {
    local program_name=$1
    local store_dir=$2
    local template="$TEMPLATES_DIR/supervisor/observer.conf"
    local conf
    local bootstrappers_argument=""

    if [[ -n "$OBSERVER_BOOTSTRAPPERS_SOURCE" ]]; then
        bootstrappers_argument="    --bootstrappers $OBSERVER_BOOTSTRAPPERS_FILE"
    fi

    conf=$(<"$template")
    conf=${conf//OBSERVER_PROGRAM_PLACEHOLDER/$program_name}
    conf=${conf//SUMMIT_BINARY_PLACEHOLDER/$SUMMIT_TARGET_BIN}
    conf=${conf//OBSERVER_INDEX_PLACEHOLDER/$OBSERVER_INDEX}
    conf=${conf//OBSERVER_PUBLIC_ADDRESS_PLACEHOLDER/$OBSERVER_PUBLIC_ADDRESS}
    conf=${conf//OBSERVER_P2P_PORT_PLACEHOLDER/$OBSERVER_P2P_PORT}
    conf=${conf//GENESIS_PATH_PLACEHOLDER/$GENESIS_PATH}
    conf=${conf//SUMMIT_KEYS_DIR_PLACEHOLDER/$SUMMIT_KEYS_DIR}
    conf=${conf//OBSERVER_STORE_DIR_PLACEHOLDER/$store_dir}
    conf=${conf//BOOTSTRAPPERS_ARGUMENT_PLACEHOLDER/$bootstrappers_argument}
    conf=${conf//OBSERVER_CRITICAL_LOG_DIR_PLACEHOLDER/$OBSERVER_CRITICAL_LOG_DIR}
    conf=${conf//SERVICE_USER_PLACEHOLDER/$SERVICE_USER}
    conf=${conf//SUPERVISOR_LOG_DIR_PLACEHOLDER/$SUPERVISOR_LOG_DIR}
    printf '%s\n' "$conf"
}

render_observer_custodian_supervisor_config() {
    local template="$TEMPLATES_DIR/supervisor/observer-custodian.conf"
    local conf

    conf=$(<"$template")
    conf=${conf//CUSTODIAN_BINARY_PLACEHOLDER/$CUSTODIAN_TARGET_BIN}
    conf=${conf//CUSTODIAN_SOCKET_PLACEHOLDER/$CUSTODIAN_SOCKET}
    conf=${conf//CUSTODIAN_ROOT_KEY_PLACEHOLDER/$CUSTODIAN_DATA_DIR/root.key}
    conf=${conf//CUSTODIAN_DELIVERY_DIR_PLACEHOLDER/$CUSTODIAN_DATA_DIR/deliveries}
    conf=${conf//COUNCIL_LISTEN_PLACEHOLDER/$COUNCIL_LISTEN}
    conf=${conf//COUNCIL_ADDRESS_PLACEHOLDER/$COUNCIL_ADDRESS}
    conf=${conf//CUSTODIAN_CHAIN_ID_PLACEHOLDER/$CUSTODIAN_CHAIN_ID}
    conf=${conf//OBSERVER_INDEX_PLACEHOLDER/$OBSERVER_INDEX}
    conf=${conf//PARENT_CUSTODIAN_PLACEHOLDER/$PARENT_CUSTODIAN}
    conf=${conf//SUMMIT_KEYS_DIR_PLACEHOLDER/$SUMMIT_KEYS_DIR}
    conf=${conf//SERVICE_USER_PLACEHOLDER/$SERVICE_USER}
    conf=${conf//SUPERVISOR_LOG_DIR_PLACEHOLDER/$SUPERVISOR_LOG_DIR}
    printf '%s\n' "$conf"
}

prepare_observer_supervisor_logs() {
    local names=(reth summit-observer)
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

deploy_observer_supervisor_configuration() {
    local staging

    section "Deploying observer Supervisor configuration"
    validate_observer_supervisor_runtime_inputs
    validate_observer_supervisor_templates

    BOOTNODE_ENODE=""
    if [[ -n "$BOOTNODE_RPC" ]] && ! fetch_bootnode_enode; then
        die "Bootnode RPC validation failed after configuration acceptance."
    fi

    replace_conflicting_supervisor_config
    [[ ! -L "$SUPERVISOR_CONFIG_PATH" ]] \
        || die "Supervisor target must not be a symbolic link: $SUPERVISOR_CONFIG_PATH"
    [[ ! -L "$CHECKPOINTER_CONFIG_PATH" ]] \
        || die "Checkpointer configuration target must not be a symbolic link: $CHECKPOINTER_CONFIG_PATH"

    staging=$(mktemp -d)
    render_reth_supervisor_config >"$staging/seismic-observer.conf"
    render_observer_supervisor_program \
        "summit-observer" "$OBSERVER_STORE_DIR" \
        >>"$staging/seismic-observer.conf"

    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        render_checkpointer_supervisor_config \
            >>"$staging/seismic-observer.conf"
        render_checkpointer_toml >"$staging/summit-checkpointer.toml"
    fi
    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        render_observer_custodian_supervisor_config \
            >>"$staging/seismic-observer.conf"
    fi

    if grep -R -n '_PLACEHOLDER' "$staging" >>"$LOG_FILE" 2>&1; then
        rm -rf -- "$staging"
        die "Rendered observer Supervisor configuration still contains placeholders; see $LOG_FILE"
    fi

    prepare_observer_supervisor_logs
    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        prepare_checkpointer_config_parent
        install -o root -g root -m 0644 \
            "$staging/summit-checkpointer.toml" "$CHECKPOINTER_CONFIG_PATH"
    else
        rm -f -- "$CHECKPOINTER_CONFIG_PATH"
    fi
    install -o root -g root -m 0644 \
        "$staging/seismic-observer.conf" "$SUPERVISOR_CONFIG_PATH"
    rm -rf -- "$staging"

    success "Observer Supervisor configuration deployed: $SUPERVISOR_CONFIG_PATH"
    info "Supervisor was not started, enabled, reread, or updated."
}

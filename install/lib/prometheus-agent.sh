#!/usr/bin/env bash

# Shared optional telemetry component. Never source credentials or saved settings.
PROMETHEUS_AGENT_HELPER="$SCRIPT_DIR/lib/prometheus_agent.py"
PROMETHEUS_AGENT_ACTION=keep
PROMETHEUS_AGENT_ENABLED=false

agent_saved_field() {
    python3 "$PROMETHEUS_AGENT_HELPER" show --field "$1"
}

agent_arguments() {
    AGENT_ARGUMENTS=(
        --node "$PROMETHEUS_AGENT_NODE"
        --role "$NODE_ROLE"
        --url "$PROMETHEUS_AGENT_URL"
        --data-dir "$PROMETHEUS_AGENT_DATA_DIR"
        --token-source "$PROMETHEUS_AGENT_TOKEN_SOURCE"
    )
    local name
    for name in RETH_DATA_DIR RETH_P2P_KEY_PATH SUMMIT_DATA_DIR SUMMIT_KEYS_DIR VALIDATOR_KEYS_DIR CHECKPOINTS_DIR CUSTODIAN_DATA_DIR; do
        [[ -z "${!name:-}" ]] || AGENT_ARGUMENTS+=(--exclude-path "${!name}")
    done
}

configure_prometheus_agent() {
    local existing selection saved_role
    section "Optional Prometheus Agent"
    existing=$(agent_saved_field enabled) || die "Could not read existing agent settings."
    if [[ -n "$existing" ]]; then
        saved_role=$(agent_saved_field role)
        [[ "$saved_role" == "$NODE_ROLE" ]] || die "Existing agent belongs to a different node role."
        PROMETHEUS_AGENT_ENABLED=$existing
        _out "Existing agent enabled: $existing"
        prompt selection "Agent action: keep, update, disable" "keep"
        case "$selection" in
            keep)
                PROMETHEUS_AGENT_ACTION=keep
                return
                ;;
            disable)
                confirm "Stop and disable this agent? Credentials and WAL will be preserved." || return
                PROMETHEUS_AGENT_ACTION=disable
                PROMETHEUS_AGENT_ENABLED=false
                return
                ;;
            update) ;;
            *) die "Select keep, update, or disable." ;;
        esac
    elif ! confirm "Install Prometheus Agent for authenticated remote write?"; then
        PROMETHEUS_AGENT_ENABLED=false
        PROMETHEUS_AGENT_ACTION=keep
        return
    fi
    PROMETHEUS_AGENT_NODE=$(agent_saved_field node)
    PROMETHEUS_AGENT_URL=$(agent_saved_field url)
    PROMETHEUS_AGENT_DATA_DIR=$(agent_saved_field data_dir)
    prompt PROMETHEUS_AGENT_NODE "Stable monitoring node name (lowercase hostname)" "${PROMETHEUS_AGENT_NODE:-$(hostname -f)}"
    prompt PROMETHEUS_AGENT_URL "Remote-write HTTPS URL ending in /api/v1/write" "$PROMETHEUS_AGENT_URL"
    prompt PROMETHEUS_AGENT_DATA_DIR "Agent WAL directory" "${PROMETHEUS_AGENT_DATA_DIR:-/var/lib/seismic-prometheus-agent}"
    local default_token=""
    [[ -z "$existing" ]] || default_token=/etc/seismic/prometheus-agent/token
    prompt PROMETHEUS_AGENT_TOKEN_SOURCE "Root-only token handoff file (contents will not be displayed)" "$default_token"
    agent_arguments
    python3 "$PROMETHEUS_AGENT_HELPER" check "${AGENT_ARGUMENTS[@]}" || die "Invalid agent configuration."
    PROMETHEUS_AGENT_ENABLED=true
    PROMETHEUS_AGENT_ACTION=install
}

print_prometheus_agent_plan() {
    _out "Prometheus Agent: $PROMETHEUS_AGENT_ENABLED (action: $PROMETHEUS_AGENT_ACTION)"
    if [[ "$PROMETHEUS_AGENT_ACTION" == install ]]; then
        _out "  Version: 3.5.0; node: $PROMETHEUS_AGENT_NODE; role: $NODE_ROLE"
        _out "  Remote write: $PROMETHEUS_AGENT_URL"
        _out "  WAL: $PROMETHEUS_AGENT_DATA_DIR (maximum sample age 6h; not a disk-size limit)"
        _out "  No services start during installation. Token contents are never printed."
    fi
}

validate_prometheus_agent_plan() {
    local name
    if [[ "$PROMETHEUS_AGENT_ACTION" == install ]]; then
        agent_arguments
        python3 "$PROMETHEUS_AGENT_HELPER" check "${AGENT_ARGUMENTS[@]}"
    elif [[ "$PROMETHEUS_AGENT_ACTION" == keep ]]; then
        local excluded=()
        for name in RETH_DATA_DIR RETH_P2P_KEY_PATH SUMMIT_DATA_DIR SUMMIT_KEYS_DIR VALIDATOR_KEYS_DIR CHECKPOINTS_DIR CUSTODIAN_DATA_DIR; do
            [[ -z "${!name:-}" ]] || excluded+=(--exclude-path "${!name}")
        done
        python3 "$PROMETHEUS_AGENT_HELPER" check-existing "${excluded[@]}"
    fi
}

install_prometheus_agent() {
    case "$PROMETHEUS_AGENT_ACTION" in
        keep) return ;;
        disable) python3 "$PROMETHEUS_AGENT_HELPER" disable ;;
        install)
            agent_arguments
            python3 "$PROMETHEUS_AGENT_HELPER" install "${AGENT_ARGUMENTS[@]}"
            ;;
    esac
}

write_prometheus_agent_inventory() {
    python3 "$PROMETHEUS_AGENT_HELPER" inventory
}

print_prometheus_agent_instructions() {
    [[ "$PROMETHEUS_AGENT_ENABLED" == true ]] || return 0
    _out "Prometheus Agent is configured. Successful seismic-node $NODE_ROLE startup ensures it is running."
    _out "Node stop leaves monitoring running. Explicit management:"
    _out "  sudo ./tools/seismic-node.py monitoring status --role $NODE_ROLE"
    _out "  sudo ./tools/seismic-node.py monitoring start --role $NODE_ROLE"
    _out "  sudo ./tools/seismic-node.py monitoring stop --role $NODE_ROLE"
    _out "Register this node as PUSH_NODE on the monitoring VM; do not also pull-scrape it."
}

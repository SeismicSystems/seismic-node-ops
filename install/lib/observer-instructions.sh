#!/usr/bin/env bash

# Observer readiness notes and manual service-start commands.

print_missing_observer_prerequisites() {
    local missing=false
    local security_error

    if ! security_error=$(check_service_executable_security "$RETH_TARGET_BIN"); then
        warn "seismic-reth cannot start until its executable is secured at $RETH_TARGET_BIN: $security_error."
        missing=true
    fi
    if ! security_error=$(check_service_executable_security "$SUMMIT_TARGET_BIN"); then
        warn "Summit cannot start until its executable is secured at $SUMMIT_TARGET_BIN: $security_error."
        missing=true
    else
        if ! run_as_service_user "$SUMMIT_TARGET_BIN" run --help 2>&1 \
            | grep -q -- '--observer'; then
            warn "Summit does not support the required --observer option: $SUMMIT_TARGET_BIN."
            missing=true
        fi
        if ! check_summit_checkpoint_cli_support; then
            warn "Summit cannot start from a checkpoint because it does not support --checkpoint-path and --weak-subjectivity-path: $SUMMIT_TARGET_BIN."
            missing=true
        fi
    fi
    if [[ ! -f "$RETH_P2P_KEY_PATH" ]]; then
        warn "seismic-reth cannot start until its P2P key exists at $RETH_P2P_KEY_PATH."
        missing=true
    fi
    if [[ ! -f "$SUMMIT_KEYS_DIR/consensus_key.pem" ]]; then
        warn "Summit cannot start until the observer consensus key exists at $SUMMIT_KEYS_DIR/consensus_key.pem."
        missing=true
    fi
    if [[ ! -f "$SUMMIT_KEYS_DIR/node_key.pem" ]]; then
        warn "Summit and observer Custodian cannot start until the parent node key is provisioned at $SUMMIT_KEYS_DIR/node_key.pem."
        missing=true
    fi

    if [[ "$INSTALL_CUSTODIAN" == true ]] \
        && ! security_error=$(check_service_executable_security "$CUSTODIAN_TARGET_BIN"); then
        warn "Custodian cannot start until its executable is secured at $CUSTODIAN_TARGET_BIN: $security_error."
        missing=true
    fi

    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        if ! security_error=$(check_service_executable_security "$CHECKPOINTER_TARGET_BIN"); then
            warn "summit-checkpointer cannot start until its executable is secured at $CHECKPOINTER_TARGET_BIN: $security_error."
            missing=true
        elif ! check_checkpointer_cli_support; then
            warn "summit-checkpointer cannot start until its executable supports --bind-address: $CHECKPOINTER_TARGET_BIN."
            missing=true
        fi
        if ! security_error=$(check_service_executable_security "$MDBX_COPY_TARGET_BIN"); then
            warn "summit-checkpointer cannot start until a compatible mdbx_copy is secured at $MDBX_COPY_TARGET_BIN: $security_error."
            missing=true
        fi
    fi

    if [[ "$missing" == true ]]; then
        warn "Resolve the missing prerequisites above before running the start commands."
    else
        success "All enabled observer service executables and keys are present."
    fi
}

print_observer_manual_start_instructions() {
    section "Manual observer service start"
    print_missing_observer_prerequisites

    _out "The installer did not start, enable, reread, or update observer services."
    _out "Before starting the observer, provide the parent validator's private node_key.pem at:"
    _out "  $SUMMIT_KEYS_DIR/node_key.pem"
    _out "It must be owned by $SERVICE_USER:$SERVICE_GROUP with mode 0600."
    _out ""
    _out "After resolving all warnings, load the Supervisor configuration:"
    _out ""
    _out "  sudo systemctl enable --now supervisor"
    _out "  sudo supervisorctl reread"
    _out "  sudo supervisorctl update"
    _out ""
    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        _out "Start the observer Custodian first. It will fetch or verify its root key"
        _out "through the parent Custodian at $PARENT_CUSTODIAN:"
        _out "  sudo supervisorctl start custodian"
    fi
    _out "  sudo supervisorctl start reth"
    _out "  sudo supervisorctl start summit-observer"
    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        _out "  sudo supervisorctl start checkpointer"
    fi

    if [[ "$CONFIGURE_PUBLIC_ENDPOINT" == true ]]; then
        _out ""
        _out "Start or reload OpenResty explicitly:"
        _out "  sudo systemctl enable openresty"
        _out "  if sudo systemctl is-active --quiet openresty; then"
        _out "      sudo systemctl reload openresty"
        _out "  else"
        _out "      sudo systemctl start openresty"
        _out "  fi"
    fi

    _out ""
    _out "Inspect status with:"
    _out "  sudo supervisorctl status"
    _out ""
    warn "Supervisor programs use autostart=false and autorestart=false."
    warn "Start them manually again after a server or Supervisor restart."
    warn "Do not start summit-observer-checkpoint until a verified checkpoint-start configuration has been installed."
    warn "The summit-observer and summit-observer-checkpoint programs are mutually exclusive."
    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        warn "This host needs outbound TCP access to parent Custodian $PARENT_CUSTODIAN."
        warn "The parent Custodian firewall must allow this observer's source IP."
        warn "Root-key and plaintext epoch-key material transit this connection; use a private network or TLS tunnel."
    fi
}

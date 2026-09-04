#!/usr/bin/env bash

# Final readiness notes and manual service-start commands.

print_missing_service_prerequisites() {
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
        if ! run_as_service_user "$SUMMIT_TARGET_BIN" deposit-rpc --help \
            >/dev/null 2>&1; then
            warn "Summit cannot complete validator registration because it does not support the deposit-rpc subcommand: $SUMMIT_TARGET_BIN."
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
    if [[ ! -f "$SUMMIT_KEYS_DIR/node_key.pem" ||
        ! -f "$SUMMIT_KEYS_DIR/consensus_key.pem" ]]; then
        warn "Summit cannot start until node_key.pem and consensus_key.pem exist under $SUMMIT_KEYS_DIR."
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
        success "All enabled validator service executables and keys are present."
    fi
}

print_manual_start_instructions() {
    section "Manual service start"
    print_missing_service_prerequisites

    _out "The installer did not start, enable, reread, or update validator services."
    _out "After resolving any warnings above, load the Supervisor configuration:"
    _out ""
    _out "  sudo systemctl enable --now supervisor"
    _out "  sudo supervisorctl reread"
    _out "  sudo supervisorctl update"
    _out ""
    _out "Use the Python node tool for the deposit-signature and checkpoint-onboarding flow:"
    _out ""
    _out "  sudo $SCRIPT_DIR/../tools/seismic-node.py validator deposit-signature --output /root/deposit-signature.json"
    _out "  sudo $SCRIPT_DIR/../tools/seismic-node.py validator onboard --help"
    _out "When onboarding authorizes full startup, it enables and starts Supervisor,"
    _out "then runs supervisorctl reread and supervisorctl update."
    _out ""
    _out "For manual troubleshooting, start Summit's localhost-only"
    _out "deposit-signature RPC program:"
    _out ""
    _out "  sudo supervisorctl start summit-deposit-rpc"
    _out ""
    _out "From another local shell, request the signature and save the complete"
    _out "JSON-RPC response as deposit-signature.json. Detailed commands are in:"
    _out "  $SCRIPT_DIR/README.md"
    _out ""
    _out "The request must call getDepositSignature on http://127.0.0.1:3031"
    _out "with params [32000000000, \"0xd412c5ecd343e264381ff15afc0ad78a67b79f35\"]."
    _out ""
    warn "Use this exact withdrawal address; do not substitute another value."
    _out "After obtaining the file, stop the temporary RPC program:"
    _out ""
    _out "  sudo supervisorctl stop summit-deposit-rpc"
    _out ""
    _out "Send deposit-signature.json to Seismic operations through the agreed secure channel."
    warn "Do not send validator keys, wallet private keys, seed phrases, or JWT secrets."
    _out "Wait for Seismic operations to confirm a successful staking transaction before"
    _out "starting the full validator services:"
    _out ""

    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        _out "  sudo supervisorctl start custodian"
    fi
    _out "  sudo supervisorctl start reth"
    _out "  sudo supervisorctl start summit"
    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        _out "  sudo supervisorctl start checkpointer"
    fi
    if [[ "$CONFIGURE_PUBLIC_ENDPOINT" == true ]]; then
        _out ""
        _out "Start or reload OpenResty separately:"
        _out "  sudo systemctl enable openresty"
        _out "  if sudo systemctl is-active --quiet openresty; then"
        _out "      sudo systemctl reload openresty"
        _out "  else"
        _out "      sudo systemctl start openresty"
        _out "  fi"
    fi
    _out ""
    _out "Stop all validator node programs in reverse dependency order with:"
    _out "  sudo $SCRIPT_DIR/../tools/seismic-node.py validator stop"
    _out "Supervisor and OpenResty remain running."
    _out ""
    _out "Manual stop commands:"
    if [[ "$INSTALL_CHECKPOINTER" == true ]]; then
        _out "  sudo supervisorctl stop checkpointer"
    fi
    _out "  sudo supervisorctl stop summit-deposit-rpc"
    _out "  sudo supervisorctl stop summit"
    _out "  sudo supervisorctl stop summit-checkpoint"
    _out "  sudo supervisorctl stop reth"
    if [[ "$INSTALL_CUSTODIAN" == true ]]; then
        _out "  sudo supervisorctl stop custodian"
    fi

    _out ""
    _out "Then inspect status with:"
    _out "  sudo supervisorctl status"
    if [[ "$CONFIGURE_PUBLIC_ENDPOINT" == true ]]; then
        _out "  sudo systemctl status openresty --no-pager"
    fi
    _out ""
    warn "Supervisor programs use autostart=false and autorestart=false."
    warn "Start them manually again after a server or Supervisor restart."
    warn "Do not start summit-checkpoint until a verified checkpoint-start configuration has been installed."
    warn "The summit and summit-checkpoint programs are mutually exclusive."
}

#!/usr/bin/env bash

# Install and configure a Seismic observer node.
#
# The installer reuses the validator installer's shared node components while
# keeping observer assignment, parent-key provisioning, and Custodian parent
# synchronization explicit. It never starts node services.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
LOG_FILE="${LOG_FILE:-/var/log/seismic-observer-install.log}"

# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
trap 'unexpected_error "$?" "$LINENO"' ERR
# shellcheck source=lib/preflight.sh
source "$SCRIPT_DIR/lib/preflight.sh"
# shellcheck source=lib/packages.sh
source "$SCRIPT_DIR/lib/packages.sh"
# shellcheck source=lib/openresty.sh
source "$SCRIPT_DIR/lib/openresty.sh"
# shellcheck source=lib/directories.sh
source "$SCRIPT_DIR/lib/directories.sh"
# shellcheck source=lib/observer-directories.sh
source "$SCRIPT_DIR/lib/observer-directories.sh"
# shellcheck source=lib/binaries.sh
source "$SCRIPT_DIR/lib/binaries.sh"
# shellcheck source=lib/checkpointer.sh
source "$SCRIPT_DIR/lib/checkpointer.sh"
# shellcheck source=lib/custodian.sh
source "$SCRIPT_DIR/lib/custodian.sh"
# shellcheck source=lib/keys.sh
source "$SCRIPT_DIR/lib/keys.sh"
# shellcheck source=lib/observer-keys.sh
source "$SCRIPT_DIR/lib/observer-keys.sh"
# shellcheck source=lib/supervisor.sh
source "$SCRIPT_DIR/lib/supervisor.sh"
# shellcheck source=lib/observer-supervisor.sh
source "$SCRIPT_DIR/lib/observer-supervisor.sh"
# shellcheck source=lib/configuration.sh
source "$SCRIPT_DIR/lib/configuration.sh"
# shellcheck source=lib/observer-configuration.sh
source "$SCRIPT_DIR/lib/observer-configuration.sh"
# shellcheck source=lib/observer-instructions.sh
source "$SCRIPT_DIR/lib/observer-instructions.sh"

main() {
    info "Seismic observer installer"
    preflight
    command -v python3 >/dev/null \
        || die "python3 is required for observer socket-address validation."
    configure_observer
    install_system_packages
    install_openresty
    setup_observer_runtime_directories
    install_node_binaries
    install_checkpointer
    install_observer_custodian
    setup_observer_keys
    deploy_openresty_configuration
    deploy_observer_supervisor_configuration
    print_observer_manual_start_instructions
    success "Observer installation complete; services were not started."
}

main "$@"

#!/usr/bin/env bash

# Install and configure a Seismic validator node.
#
# The installer collects and confirms configuration, installs the selected
# validator components, prepares persistent state, and deploys service
# configuration without starting validator services.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
LOG_FILE="${LOG_FILE:-/var/log/seismic-validator-install.log}"

# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=lib/preflight.sh
source "$SCRIPT_DIR/lib/preflight.sh"
# shellcheck source=lib/packages.sh
source "$SCRIPT_DIR/lib/packages.sh"
# shellcheck source=lib/openresty.sh
source "$SCRIPT_DIR/lib/openresty.sh"
# shellcheck source=lib/directories.sh
source "$SCRIPT_DIR/lib/directories.sh"
# shellcheck source=lib/binaries.sh
source "$SCRIPT_DIR/lib/binaries.sh"
# shellcheck source=lib/checkpointer.sh
source "$SCRIPT_DIR/lib/checkpointer.sh"
# shellcheck source=lib/custodian.sh
source "$SCRIPT_DIR/lib/custodian.sh"
# shellcheck source=lib/keys.sh
source "$SCRIPT_DIR/lib/keys.sh"
# shellcheck source=lib/supervisor.sh
source "$SCRIPT_DIR/lib/supervisor.sh"
# shellcheck source=lib/instructions.sh
source "$SCRIPT_DIR/lib/instructions.sh"
# shellcheck source=lib/configuration.sh
source "$SCRIPT_DIR/lib/configuration.sh"

main() {
    info "Seismic validator installer"
    preflight
    configure
    install_system_packages
    install_openresty
    setup_runtime_directories
    install_node_binaries
    install_checkpointer
    install_custodian
    setup_validator_keys
    deploy_openresty_configuration
    deploy_supervisor_configuration
    print_manual_start_instructions
    success "Validator installation complete; services were not started."
}

main "$@"

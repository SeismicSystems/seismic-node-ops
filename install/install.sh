#!/usr/bin/env bash

# Seismic node installer: performs section 3 of docs/RUNBOOK.md from a
# prepared node.env. Non-interactive by design: every decision is a key in
# the config file, which the installer validates before touching the host and
# installs unchanged as /etc/seismic/node.env.
#
#   install.sh --config PATH [--dry-run] [--only STEP[,STEP...]]
#
# Nothing is started. First start is always `seismic-node ... start`, which
# runs the identity and checkpoint checks a bare `systemctl start` does not.

set -euo pipefail
set -o errtrace # ERR trap applies inside functions

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
TEMPLATES_DIR="$SCRIPT_DIR/templates"
TOOLS_DIR="$REPO_ROOT/tools"
LOG_FILE=${LOG_FILE:-/var/log/seismic-install.log}
NODE_ENV_PATH=/etc/seismic/node.env

# Installation steps in canonical order. Each is a function below; the
# second column is the runbook section it implements. --only selects a
# subset but never reorders.
STEPS=(
    "packages:3.1"
    "account:3.2"
    "directories:3.3"
    "binaries:3.4"
    "runner:3.5"
    "config:3.6"
    "keys:3.7"
    "checkpointer:3.8"
    "units:3.10"
    "tls:3.11"
)

# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

CONFIG=""
DRY_RUN=false
ONLY=""

usage() {
    cat <<EOF
Usage: ${0##*/} --config PATH [--dry-run] [--only STEP[,STEP...]]

Install a Seismic validator or observer from a prepared node.env (see
install/templates/node.env.example and docs/RUNBOOK.md section 3).

  --config PATH   node.env to install; validated first, then copied to
                  $NODE_ENV_PATH
  --dry-run       validate, render, and print the plan; change nothing
  --only STEPS    run only these steps, in canonical order

Steps: $(step_names | paste -sd,)
EOF
}

step_names() {
    local entry
    for entry in "${STEPS[@]}"; do
        printf '%s\n' "${entry%%:*}"
    done
}

parse_arguments() {
    while (($# > 0)); do
        case "$1" in
            --config)
                [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || die "--config requires a path."
                CONFIG=$2
                shift 2
                ;;
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --only)
                [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || die "--only requires a step list."
                ONLY=$2
                shift 2
                ;;
            -h | --help)
                usage
                exit 0
                ;;
            *) die "Unknown option: $1 (see --help)" ;;
        esac
    done
    [[ -n "$CONFIG" ]] || die "--config is required; there is no interactive mode."
    [[ "$CONFIG" == /* ]] || CONFIG=$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")
    [[ -f "$CONFIG" ]] || die "Config file not found: $CONFIG"
    if [[ -n "$ONLY" ]]; then
        local requested known
        for requested in ${ONLY//,/ }; do
            known=false
            while read -r name; do
                [[ "$name" == "$requested" ]] && known=true
            done < <(step_names)
            [[ "$known" == true ]] || die "Unknown step in --only: $requested (steps: $(step_names | paste -sd,))"
        done
    fi
}

step_selected() {
    local name=$1
    [[ -z "$ONLY" ]] && return 0
    [[ ",$ONLY," == *",$name,"* ]]
}

# --- validation -------------------------------------------------------------

validate_config() {
    section "Validating $CONFIG"
    # The same parser systemd's EnvironmentFile= semantics are mirrored by, so
    # what passes here is what the units will read.
    if ! (cd "$TOOLS_DIR" && /usr/bin/python3 -m seismic_node.nodeenv --check --no-ownership "$CONFIG"); then
        die "node.env validation failed; nothing was changed."
    fi
    # shellcheck disable=SC1090
    set -a && source "$CONFIG" && set +a
    success "Role: $SEISMIC_ROLE"
}

# --- steps ------------------------------------------------------------------
# Implemented in lib/steps.sh; the remaining stubs are filled in by later
# commits.

# shellcheck source=lib/binaries.sh
source "$SCRIPT_DIR/lib/binaries.sh"
# shellcheck source=lib/openresty.sh
source "$SCRIPT_DIR/lib/openresty.sh"
# shellcheck source=lib/steps.sh
source "$SCRIPT_DIR/lib/steps.sh"

# --- driver -----------------------------------------------------------------

print_plan() {
    local entry name section_ref mark
    section "Plan ($([[ "$DRY_RUN" == true ]] && echo "dry run" || echo "install"))"
    for entry in "${STEPS[@]}"; do
        name=${entry%%:*}
        section_ref=${entry#*:}
        mark=$(step_selected "$name" && echo "*" || echo " ")
        _out "  [$mark] $name (runbook $section_ref)"
    done
}

run_steps() {
    local entry name
    for entry in "${STEPS[@]}"; do
        name=${entry%%:*}
        step_selected "$name" || continue
        section "Step: $name"
        "step_$name"
    done
}

print_next_steps() {
    section "Installation complete; no services were started"
    _out "Validate the installed configuration:"
    _out "  (cd $TOOLS_DIR && sudo python3 -m seismic_node.nodeenv --check)"
    _out "Then start the node (docs/RUNBOOK.md section 4):"
    if [[ "$SEISMIC_ROLE" == validator ]]; then
        _out "  sudo $TOOLS_DIR/seismic-node.py validator start --mode normal     # listed in genesis"
        _out "  sudo $TOOLS_DIR/seismic-node.py validator deposit-signature ...   # joining by deposit"
    else
        _out "  sudo $TOOLS_DIR/seismic-node.py observer start --mode normal"
    fi
    if [[ "$MONITORING_ENABLED" == true ]]; then
        _out "Prometheus Agent (runbook 3.12):"
        _out "  sudo $TOOLS_DIR/seismic-node.py monitoring install --token-source /root/monitoring-token"
    fi
}

main() {
    parse_arguments "$@"
    if [[ "$DRY_RUN" != true ]]; then
        [[ $EUID -eq 0 ]] || die "This installer must be run as root (use sudo), or use --dry-run."
        touch "$LOG_FILE" 2>/dev/null || die "Cannot write the installation log: $LOG_FILE"
    else
        LOG_FILE=/dev/null
    fi
    trap 'unexpected_error $? $LINENO' ERR

    validate_config
    print_plan
    if [[ "$DRY_RUN" == true ]]; then
        success "Dry run: no changes were made."
        return
    fi
    run_steps
    print_next_steps
}

main "$@"

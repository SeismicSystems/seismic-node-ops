#!/usr/bin/env bash

# Run install.sh on a disposable test VM over SSH.
#
#   tests/vm/run.sh [--config FILE] [-- INSTALLER_ARGS...]
#
# Syncs this checkout to $VM_HOST:~/seismic-node-ops (excluding .git and
# caches), uploads the config, and runs the installer as root. Defaults to a
# test-only validator config: chain ID 5124, domain seismic-vm.invalid, and
# monitoring off. This does not test public certificates or telemetry. Use
# --config FILE for real network settings; supplied files are not rewritten.
# Validates locally with Python 3 before any remote operations. Set VM_HOST
# (user@host) and optionally VM_SSH_KEY in the environment.
#
# The VM is treated as disposable except for /persistence/keys, which the
# installer preserves by design and this script never touches.

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
: "${VM_HOST:?set VM_HOST=user@host}"
SSH=(ssh -o BatchMode=yes ${VM_SSH_KEY:+-i "$VM_SSH_KEY"} "$VM_HOST")
REMOTE_REPO=seismic-node-ops

CONFIG=""
while (($# > 0)); do
    case "$1" in
        --config)
            CONFIG=$2
            shift 2
            ;;
        --)
            shift
            break
            ;;
        *) break ;;
    esac
done

if [[ -z "$CONFIG" ]]; then
    CONFIG=$(mktemp)
    trap 'rm -f "$CONFIG"' EXIT
    # Install-only fixture: a reserved domain, no telemetry credentials, and
    # no remote-write destination. The installer does not activate services.
    sed -e 's/^CUSTODIAN_CHAIN_ID=$/CUSTODIAN_CHAIN_ID=5124/' \
        -e 's/^DOMAIN=$/DOMAIN=seismic-vm.invalid/' \
        -e 's/^MONITORING_MODE=$/MONITORING_MODE=off/' \
        "$REPO_ROOT/install/templates/node.env.example" >"$CONFIG"
fi

echo "==> validating $CONFIG locally"
PYTHONPATH="$REPO_ROOT/tools" python3 -m seismic_node.nodeenv --check --no-ownership "$CONFIG"

echo "==> syncing $REPO_ROOT to $VM_HOST:~/$REMOTE_REPO"
rsync -az --delete \
    --exclude .git --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'target' \
    -e "ssh -o BatchMode=yes ${VM_SSH_KEY:+-i $VM_SSH_KEY}" \
    "$REPO_ROOT/" "$VM_HOST:$REMOTE_REPO/"
scp -q ${VM_SSH_KEY:+-i "$VM_SSH_KEY"} "$CONFIG" "$VM_HOST:$REMOTE_REPO/node.env"

echo "==> running install.sh $*"
"${SSH[@]}" "cd $REMOTE_REPO && sudo ./install/install.sh --config \$PWD/node.env $*"

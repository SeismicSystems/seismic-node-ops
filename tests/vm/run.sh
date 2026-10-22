#!/usr/bin/env bash

# Run install.sh on a disposable test VM over SSH.
#
#   tests/vm/run.sh [--config FILE] [-- INSTALLER_ARGS...]
#
# Syncs this checkout to $VM_HOST:~/seismic-node-ops (excluding .git and
# caches), uploads the config, and runs the installer as root. Defaults to the
# example config with the network values a validator needs filled in. Set
# VM_HOST (user@host) and optionally VM_SSH_KEY in the environment.
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
    # The example is a valid validator once the chain ID is set.
    sed 's/^CUSTODIAN_CHAIN_ID=$/CUSTODIAN_CHAIN_ID=5124/' \
        "$REPO_ROOT/install/templates/node.env.example" >"$CONFIG"
fi

echo "==> syncing $REPO_ROOT to $VM_HOST:~/$REMOTE_REPO"
rsync -az --delete \
    --exclude .git --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'target' \
    -e "ssh -o BatchMode=yes ${VM_SSH_KEY:+-i $VM_SSH_KEY}" \
    "$REPO_ROOT/" "$VM_HOST:$REMOTE_REPO/"
scp -q ${VM_SSH_KEY:+-i "$VM_SSH_KEY"} "$CONFIG" "$VM_HOST:$REMOTE_REPO/node.env"

echo "==> running install.sh $*"
"${SSH[@]}" "cd $REMOTE_REPO && sudo ./install/install.sh --config \$PWD/node.env $*"

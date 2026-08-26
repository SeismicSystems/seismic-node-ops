#!/usr/bin/env bash
# shellcheck disable=SC2029

# Securely stream a parent validator's Summit node_key.pem over SSH to an
# already-installed observer. The private key is sent through stdin and is
# never placed in command arguments, environment variables, or log output.

set -euo pipefail

TARGET=""
PARENT_NODE_KEY_FILE=""
PARENT_NODE_PUBLIC_KEY=""
OBSERVER_INDEX=""
SUMMIT_KEYS_DIR="/persistence/keys/summit"
SUMMIT_BIN="/usr/local/bin/summit"
SERVICE_USER="ubuntu"
SSH_KEY=""
REPLACE=false
YES=false

usage() {
    cat <<'EOF'
Usage: provision-observer-key.sh \
  --target USER@HOST \
  --parent-node-key-file PATH \
  --parent-node-public-key HEX \
  --observer-index N \
  [--summit-keys-dir /persistence/keys/summit] \
  [--summit-bin /usr/local/bin/summit] \
  [--service-user ubuntu] [--ssh-key PATH] [--replace] [--yes]

Run this helper on the parent validator host. The observer installer must have
already created the matching observer-assignment marker and consensus key.
EOF
}

require_option_value() {
    local option=$1
    local value=${2:-}
    [[ -n "$value" ]] || {
        echo "$option requires a value" >&2
        exit 1
    }
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target) require_option_value "$1" "${2:-}"; TARGET=$2; shift 2 ;;
        --parent-node-key-file) require_option_value "$1" "${2:-}"; PARENT_NODE_KEY_FILE=$2; shift 2 ;;
        --parent-node-public-key) require_option_value "$1" "${2:-}"; PARENT_NODE_PUBLIC_KEY=$2; shift 2 ;;
        --observer-index) require_option_value "$1" "${2:-}"; OBSERVER_INDEX=$2; shift 2 ;;
        --summit-keys-dir) require_option_value "$1" "${2:-}"; SUMMIT_KEYS_DIR=$2; shift 2 ;;
        --summit-bin) require_option_value "$1" "${2:-}"; SUMMIT_BIN=$2; shift 2 ;;
        --service-user) require_option_value "$1" "${2:-}"; SERVICE_USER=$2; shift 2 ;;
        --ssh-key) require_option_value "$1" "${2:-}"; SSH_KEY=$2; shift 2 ;;
        --replace) REPLACE=true; shift ;;
        --yes) YES=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

[[ -n "$TARGET" && "$TARGET" != -* && ! "$TARGET" =~ [[:space:]\;\|\&\$\`] ]] \
    || { echo "--target is required and must be a safe SSH destination" >&2; exit 1; }
[[ -f "$PARENT_NODE_KEY_FILE" && ! -L "$PARENT_NODE_KEY_FILE" ]] \
    || { echo "Parent node key must be a regular non-symbolic-link file: $PARENT_NODE_KEY_FILE" >&2; exit 1; }
[[ "$PARENT_NODE_PUBLIC_KEY" =~ ^(0x)?[0-9a-fA-F]{64}$ ]] \
    || { echo "--parent-node-public-key must be a 32-byte hexadecimal value" >&2; exit 1; }
if [[ ! "$OBSERVER_INDEX" =~ ^[0-9]{1,3}$ ]] \
    || ((10#$OBSERVER_INDEX > 255)); then
    echo "--observer-index must be an integer from 0 through 255" >&2
    exit 1
fi
[[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*\$?$ && "$SERVICE_USER" != "root" ]] \
    || { echo "--service-user is malformed or unsafe" >&2; exit 1; }
for path in "$SUMMIT_KEYS_DIR" "$SUMMIT_BIN"; do
    [[ "$path" == /* && ! "$path" =~ [[:space:]\;\|\&\$\`\"\'\\] ]] \
        || { echo "Remote paths must be absolute and contain no shell metacharacters: $path" >&2; exit 1; }
done
if [[ -n "$SSH_KEY" ]]; then
    [[ -f "$SSH_KEY" ]] || { echo "SSH key not found: $SSH_KEY" >&2; exit 1; }
fi

PARENT_NODE_PUBLIC_KEY=${PARENT_NODE_PUBLIC_KEY#0x}
PARENT_NODE_PUBLIC_KEY=${PARENT_NODE_PUBLIC_KEY,,}
OBSERVER_INDEX=$((10#$OBSERVER_INDEX))
REMOTE_KEY="$SUMMIT_KEYS_DIR/node_key.pem"
CANDIDATE_KEY="$SUMMIT_KEYS_DIR/node_key.pem.candidate"
CONSENSUS_KEY="$SUMMIT_KEYS_DIR/consensus_key.pem"
ASSIGNMENT_FILE="$SUMMIT_KEYS_DIR/observer-assignment"
EXPECTED_ASSIGNMENT="$PARENT_NODE_PUBLIC_KEY:$OBSERVER_INDEX"

ssh_opts=(-o BatchMode=yes)
if [[ -n "$SSH_KEY" ]]; then
    ssh_opts+=(-i "$SSH_KEY")
fi

identity=$(ssh "${ssh_opts[@]}" -- "$TARGET" \
    'printf "hostname="; hostname; printf "service_user="; id -un')
printf 'Target identity:\n%s\n' "$identity"
printf 'Destination: %s\n' "$REMOTE_KEY"
printf 'Local key SHA-256: %s\n' "$(sha256sum "$PARENT_NODE_KEY_FILE" | awk '{print $1}')"

if [[ "$YES" != true ]]; then
    read -r -p "Provision this parent key to the displayed observer? [y/N]: " answer
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || { echo "Aborted."; exit 1; }
fi

remote_assignment_check=$(printf \
    'sudo -n test -f %q && sudo -n grep -Fxq %q %q' \
    "$ASSIGNMENT_FILE" "$EXPECTED_ASSIGNMENT" "$ASSIGNMENT_FILE")
ssh "${ssh_opts[@]}" -- "$TARGET" "$remote_assignment_check" \
    || { echo "Observer assignment marker is missing or does not match; run install-observer.sh first." >&2; exit 1; }

remote_consensus_check=$(printf 'sudo -n test -s %q' "$CONSENSUS_KEY")
ssh "${ssh_opts[@]}" -- "$TARGET" "$remote_consensus_check" \
    || { echo "Observer consensus key is missing; rerun install-observer.sh after installing Summit." >&2; exit 1; }

remote_user_check=$(printf 'id %q >/dev/null 2>&1' "$SERVICE_USER")
ssh "${ssh_opts[@]}" -- "$TARGET" "$remote_user_check" \
    || { echo "Observer service user does not exist: $SERVICE_USER" >&2; exit 1; }
SERVICE_GROUP=$(ssh "${ssh_opts[@]}" -- "$TARGET" \
    "id -gn $(printf '%q' "$SERVICE_USER")")
[[ "$SERVICE_GROUP" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*\$?$ ]] \
    || { echo "Could not determine a safe observer service group" >&2; exit 1; }

local_digest=$(sha256sum "$PARENT_NODE_KEY_FILE" | awk '{print $1}')
# shellcheck disable=SC2016
remote_digest_command=$(printf \
    'sudo -n sha256sum %q 2>/dev/null | awk '\''{print $1}'\''' "$REMOTE_KEY")
remote_digest=$(ssh "${ssh_opts[@]}" -- "$TARGET" "$remote_digest_command" || true)

if [[ "$remote_digest" == "$local_digest" ]]; then
    echo "Remote parent key already matches; no upload needed."
elif [[ -n "$remote_digest" && "$REPLACE" != true ]]; then
    echo "Refusing to replace a different existing key at $REMOTE_KEY without --replace." >&2
    exit 1
else
    remote_script=$(cat <<EOF
set -eu
install -d -o '$SERVICE_USER' -g '$SERVICE_GROUP' -m 0700 '$SUMMIT_KEYS_DIR'
tmp=\$(mktemp '$SUMMIT_KEYS_DIR/.node_key.pem.partial.XXXXXX')
trap 'rm -f "\$tmp"' EXIT
cat >"\$tmp"
chmod 0600 "\$tmp"
chown '$SERVICE_USER:$SERVICE_GROUP' "\$tmp"
mv -f "\$tmp" '$CANDIDATE_KEY'
trap - EXIT
EOF
)
    remote_command=$(printf 'sudo -n sh -c %q' "$remote_script")
    ssh "${ssh_opts[@]}" -- "$TARGET" "$remote_command" <"$PARENT_NODE_KEY_FILE"

    verify_script=$(cat <<EOF
set -eu
tmp=\$(mktemp -d /tmp/summit-observer-verify.XXXXXX)
trap 'rm -rf "\$tmp"' EXIT
cp '$CANDIDATE_KEY' "\$tmp/node_key.pem"
cp '$CONSENSUS_KEY' "\$tmp/consensus_key.pem"
chown -R '$SERVICE_USER:$SERVICE_GROUP' "\$tmp"
chmod 0700 "\$tmp"
chmod 0600 "\$tmp/node_key.pem" "\$tmp/consensus_key.pem"
runuser -u '$SERVICE_USER' -- '$SUMMIT_BIN' keys show --key-store-path "\$tmp"
EOF
)
    verify_command=$(printf 'sudo -n sh -c %q' "$verify_script")
    candidate_output=$(ssh "${ssh_opts[@]}" -- "$TARGET" "$verify_command")
    candidate_public_key=$(printf '%s\n' "$candidate_output" \
        | awk -F': ' '/Node Public Key \(ed25519\)/ {print tolower($2); exit}')
    candidate_public_key=${candidate_public_key#0x}
    if [[ "$candidate_public_key" != "$PARENT_NODE_PUBLIC_KEY" ]]; then
        cleanup_command=$(printf 'sudo -n rm -f -- %q' "$CANDIDATE_KEY")
        ssh "${ssh_opts[@]}" -- "$TARGET" "$cleanup_command" || true
        echo "Supplied node key does not match the expected parent public key." >&2
        exit 1
    fi

    install_command=$(printf \
        'sudo -n mv -f -- %q %q' "$CANDIDATE_KEY" "$REMOTE_KEY")
    ssh "${ssh_opts[@]}" -- "$TARGET" "$install_command"
fi

permissions_command=$(printf \
    'sudo -n chown %q:%q %q %q %q %q && sudo -n chmod 0700 %q && sudo -n chmod 0600 %q %q %q' \
    "$SERVICE_USER" "$SERVICE_GROUP" "$SUMMIT_KEYS_DIR" "$REMOTE_KEY" \
    "$CONSENSUS_KEY" "$ASSIGNMENT_FILE" "$SUMMIT_KEYS_DIR" "$REMOTE_KEY" \
    "$CONSENSUS_KEY" "$ASSIGNMENT_FILE")
ssh "${ssh_opts[@]}" -- "$TARGET" "$permissions_command"

installed_digest=$(ssh "${ssh_opts[@]}" -- "$TARGET" "$remote_digest_command")
[[ "$installed_digest" == "$local_digest" ]] \
    || { echo "Remote key fingerprint verification failed" >&2; exit 1; }

show_command=$(printf \
    'sudo -n -u %q %q keys show --key-store-path %q' \
    "$SERVICE_USER" "$SUMMIT_BIN" "$SUMMIT_KEYS_DIR")
key_output=$(ssh "${ssh_opts[@]}" -- "$TARGET" "$show_command")
installed_public_key=$(printf '%s\n' "$key_output" \
    | awk -F': ' '/Node Public Key \(ed25519\)/ {print tolower($2); exit}')
installed_public_key=${installed_public_key#0x}
installed_consensus_key=$(printf '%s\n' "$key_output" \
    | awk -F': ' '/Consensus Public Key \(BLS\)/ {print tolower($2); exit}')
[[ "$installed_public_key" == "$PARENT_NODE_PUBLIC_KEY" ]] \
    || { echo "Installed node key does not match the expected parent public key." >&2; exit 1; }
[[ -n "$installed_consensus_key" ]] \
    || { echo "Could not read the observer consensus public key." >&2; exit 1; }

echo "Observer parent key provisioned and verified successfully."
echo "Services remain stopped; start Reth and summit-observer explicitly."

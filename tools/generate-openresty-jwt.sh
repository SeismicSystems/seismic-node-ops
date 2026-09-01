#!/usr/bin/env bash

set -Eeuo pipefail

DEFAULT_SECRET_PATH="/etc/seismic/openresty-jwt-secret"
SECRET_PATH_FILE=${OPENRESTY_JWT_SECRET_PATH_FILE:-/etc/seismic/openresty-jwt-secret.path}
SECRET_PATH=""
TTL_SECONDS=3600
MAX_TTL_SECONDS=15552000
SUBJECT="seismic-operator"

usage() {
    cat <<EOF
Usage: ${0##*/} [OPTIONS]

Generate an HS256 bearer token for OpenResty-protected Seismic endpoints.
The token is printed to standard output; the JWT secret is never printed.

Options:
  --secret-path PATH    Override the installed JWT secret path
  --ttl-seconds NUMBER  Token lifetime from 1 to $MAX_TTL_SECONDS seconds, or -1
                        for no expiration (default: 3600)
  --subject SUBJECT     JWT subject claim (default: seismic-operator)
  --help                Show this help

Secret-path lookup order:
  1. --secret-path
  2. $SECRET_PATH_FILE
  3. $DEFAULT_SECRET_PATH

Run with sudo when the protected secret is not readable by your user.
EOF
}

die() {
    printf '[ERROR] %s\n' "$*" >&2
    exit 1
}

contains_control_characters() {
    [[ "$1" =~ [[:cntrl:]] ]]
}

validate_root_owned_nonwritable_file() {
    local description=$1
    local path=$2
    local owner_uid
    local mode
    local mode_value

    [[ ! -L "$path" ]] || die "$description must not be a symbolic link: $path"
    [[ -f "$path" ]] || die "$description is not a regular file: $path"
    owner_uid=$(stat -c %u -- "$path") || die "Could not inspect $description ownership."
    [[ "$owner_uid" == "0" ]] || die "$description must be root-owned: $path"
    mode=$(stat -c %a -- "$path") || die "Could not inspect $description permissions."
    mode_value=$((8#$mode))
    ((!(mode_value & 0022))) || die "$description must not be group- or world-writable: $path"
}

read_installed_secret_path() {
    local lines=()
    local selected

    validate_root_owned_nonwritable_file "JWT secret path metadata" "$SECRET_PATH_FILE"
    mapfile -t lines <"$SECRET_PATH_FILE"
    ((${#lines[@]} == 1)) || die "JWT secret path metadata must contain exactly one line."
    selected=${lines[0]}
    [[ "$selected" == /* && "$selected" != "/" ]] \
        || die "JWT secret path metadata does not contain a valid absolute path."
    [[ $(realpath -m -- "$selected") == "$selected" ]] \
        || die "JWT secret path metadata is not normalized: $selected"
    SECRET_PATH=$selected
}

validate_secret_file_security() {
    local mode
    local mode_value

    validate_root_owned_nonwritable_file "JWT secret" "$SECRET_PATH"
    mode=$(stat -c %a -- "$SECRET_PATH") || die "Could not inspect JWT secret permissions."
    mode_value=$((8#$mode))
    ((!(mode_value & 0007))) \
        || die "JWT secret must not be accessible by users outside its owner and group: $SECRET_PATH"
}

validate_secret_parent_chain() {
    local parent
    local resolved_parent
    local current
    local owner_uid
    local mode
    local mode_value

    parent=$(dirname -- "$SECRET_PATH")
    resolved_parent=$(realpath -m -- "$parent")
    [[ "$resolved_parent" == "$parent" ]] \
        || die "JWT secret parent must not contain symbolic links: $parent"

    current=$parent
    while true; do
        [[ -d "$current" && ! -L "$current" ]] \
            || die "JWT secret parent chain contains an unsafe directory: $current"
        owner_uid=$(stat -c %u -- "$current") \
            || die "Could not inspect JWT secret parent ownership: $current"
        [[ "$owner_uid" == "0" ]] \
            || die "JWT secret parent must be root-owned: $current"
        mode=$(stat -c %a -- "$current") \
            || die "Could not inspect JWT secret parent permissions: $current"
        mode_value=$((8#$mode))
        ((!(mode_value & 0022))) \
            || die "JWT secret parent must not be group- or world-writable: $current"
        [[ "$current" == "/" ]] && break
        current=$(dirname -- "$current")
    done
}

while (($# > 0)); do
    case "$1" in
        --secret-path)
            (($# >= 2)) || die "--secret-path requires a value."
            SECRET_PATH=$2
            shift 2
            ;;
        --ttl-seconds)
            (($# >= 2)) || die "--ttl-seconds requires a value."
            TTL_SECONDS=$2
            shift 2
            ;;
        --subject)
            (($# >= 2)) || die "--subject requires a value."
            SUBJECT=$2
            shift 2
            ;;
        --help | -h)
            usage
            exit 0
            ;;
        *)
            die "Unknown option: $1"
            ;;
    esac
done

command -v python3 >/dev/null 2>&1 || die "python3 is required."
command -v realpath >/dev/null 2>&1 || die "realpath is required."

if [[ "$TTL_SECONDS" != "-1" ]]; then
    [[ "$TTL_SECONDS" =~ ^[1-9][0-9]*$ ]] \
        || die "--ttl-seconds must be -1 or a positive integer."
    ((TTL_SECONDS <= MAX_TTL_SECONDS)) \
        || die "--ttl-seconds must not exceed $MAX_TTL_SECONDS (180 days)."
fi
[[ -n "$SUBJECT" ]] || die "--subject must not be empty."
((${#SUBJECT} <= 128)) || die "--subject must not exceed 128 characters."
contains_control_characters "$SUBJECT" && die "--subject must not contain control characters."

if [[ -z "$SECRET_PATH" ]]; then
    if [[ -e "$SECRET_PATH_FILE" || -L "$SECRET_PATH_FILE" ]]; then
        read_installed_secret_path
    else
        SECRET_PATH=$DEFAULT_SECRET_PATH
    fi
fi

[[ "$SECRET_PATH" == /* && "$SECRET_PATH" != "/" ]] \
    || die "JWT secret path must be an absolute file path."
NORMALIZED_SECRET_PATH=$(realpath -m -- "$SECRET_PATH")
[[ "$NORMALIZED_SECRET_PATH" == "$SECRET_PATH" ]] \
    || die "JWT secret path must be normalized and must not contain symbolic links: $SECRET_PATH"
validate_secret_parent_chain
validate_secret_file_security
[[ -s "$SECRET_PATH" ]] || die "JWT secret is empty: $SECRET_PATH"
[[ -r "$SECRET_PATH" ]] \
    || die "JWT secret is not readable; run this command with sudo: $SECRET_PATH"

python3 - "$SECRET_PATH" "$TTL_SECONDS" "$SUBJECT" <<'PY'
import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


secret_path = Path(sys.argv[1])
ttl_seconds = int(sys.argv[2])
subject = sys.argv[3]
secret = secret_path.read_bytes().rstrip()
if not secret:
    raise SystemExit("JWT secret is empty after trimming trailing whitespace")

now = int(time.time())
header = {"alg": "HS256", "typ": "JWT"}
payload = {
    "sub": subject,
    "iat": now,
    "nbf": now - 5,
}
if ttl_seconds != -1:
    payload["exp"] = now + ttl_seconds
encoded_header = base64url(json.dumps(header, separators=(",", ":")).encode())
encoded_payload = base64url(json.dumps(payload, separators=(",", ":")).encode())
signing_input = f"{encoded_header}.{encoded_payload}".encode()
signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
print(f"{encoded_header}.{encoded_payload}.{base64url(signature)}")
PY

#!/usr/bin/env bash

# Shared provider-independent installer preflight checks.

validate_system_python() {
    local python_version

    [[ -x /usr/bin/python3 ]] \
        || die "System Python is required at /usr/bin/python3."
    if ! python_version=$(
        /usr/bin/python3 - <<'PY'
import sys
import tomllib

if sys.version_info < (3, 12):
    raise SystemExit(1)

print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
    ); then
        die "Python 3.12 or newer with the standard-library tomllib module is required at /usr/bin/python3."
    fi

    success "System Python validated: $python_version with tomllib"
}

preflight() {
    local os_id os_version

    section "Preflight checks"

    [[ $EUID -eq 0 ]] \
        || die "This script must be run as root (use sudo)."

    [[ -r /etc/os-release ]] \
        || die "Cannot determine the operating system because /etc/os-release is unavailable."
    os_id=$(awk -F= '$1 == "ID" { gsub(/"/, "", $2); print tolower($2); exit }' /etc/os-release)
    [[ "$os_id" == "ubuntu" ]] \
        || die "This installer supports Ubuntu only; detected operating system ID: ${os_id:-unknown}."
    os_version=$(awk -F= '$1 == "VERSION_ID" { gsub(/"/, "", $2); print $2; exit }' /etc/os-release)
    [[ "$os_version" == "24.04" ]] \
        || die "This installer supports Ubuntu 24.04 LTS only; detected version: ${os_version:-unknown}."

    command -v apt-get >/dev/null \
        || die "apt-get not found; this installer requires Ubuntu with apt-get."

    validate_system_python

    [[ -d "$TEMPLATES_DIR" ]] \
        || die "Templates not found at $TEMPLATES_DIR."

    touch "$LOG_FILE" 2>/dev/null \
        || die "Cannot create or write installation log: $LOG_FILE"

    success "Preflight checks passed"
}

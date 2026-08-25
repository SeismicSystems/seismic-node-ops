#!/usr/bin/env bash

# Shared provider-independent installer preflight checks.

preflight() {
    local os_id

    section "Preflight checks"

    [[ $EUID -eq 0 ]] \
        || die "This script must be run as root (use sudo)."

    [[ -r /etc/os-release ]] \
        || die "Cannot determine the operating system because /etc/os-release is unavailable."
    os_id=$(awk -F= '$1 == "ID" { gsub(/"/, "", $2); print tolower($2); exit }' /etc/os-release)
    [[ "$os_id" == "ubuntu" ]] \
        || die "This installer supports Ubuntu only; detected operating system ID: ${os_id:-unknown}."

    command -v apt-get >/dev/null \
        || die "apt-get not found; this installer requires Ubuntu with apt-get."

    [[ -d "$TEMPLATES_DIR" ]] \
        || die "Templates not found at $TEMPLATES_DIR."

    touch "$LOG_FILE" 2>/dev/null \
        || die "Cannot create or write installation log: $LOG_FILE"

    success "Preflight checks passed"
}

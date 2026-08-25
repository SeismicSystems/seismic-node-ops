#!/usr/bin/env bash

# Shared provider-independent installer preflight checks.

preflight() {
    section "Preflight checks"

    [[ $EUID -eq 0 ]] \
        || die "This script must be run as root (use sudo)."

    command -v apt-get >/dev/null \
        || die "apt-get not found; this installer supports Ubuntu/Debian only."

    [[ -d "$TEMPLATES_DIR" ]] \
        || die "Templates not found at $TEMPLATES_DIR."

    touch "$LOG_FILE" 2>/dev/null \
        || die "Cannot create or write installation log: $LOG_FILE"

    success "Preflight checks passed"
}

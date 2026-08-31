#!/usr/bin/env bash

# Shared output and error helpers for node installers.

_out() {
    { tee -a "$LOG_FILE" 2>/dev/null || cat; } <<<"$*"
}

info() {
    _out "$(printf '\033[0;34m[INFO]\033[0m') $*"
}

success() {
    _out "$(printf '\033[0;32m[OK]\033[0m') $*"
}

warn() {
    _out "$(printf '\033[0;33m[WARN]\033[0m') $*"
}

error() {
    _out "$(printf '\033[0;31m[ERROR]\033[0m') $*" >&2
}

section() {
    _out ""
    _out "$(printf '\033[1m==> %s\033[0m' "$*")"
}

die() {
    error "$*"
    exit 1
}

unexpected_error() {
    local exit_code=$1
    local line=$2

    trap - ERR
    error "Installer stopped unexpectedly at line $line with exit status $exit_code. See $LOG_FILE."
    exit "$exit_code"
}

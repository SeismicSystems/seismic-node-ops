#!/usr/bin/env bash

# Observer runtime directories in addition to the shared node directories.

setup_observer_runtime_directories() {
    setup_runtime_directories

    if [[ -L "$OBSERVER_CRITICAL_LOG_DIR" ]]; then
        die "Observer critical-log directory must not be a symbolic link: $OBSERVER_CRITICAL_LOG_DIR"
    fi
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 \
        -- "$OBSERVER_CRITICAL_LOG_DIR"
}

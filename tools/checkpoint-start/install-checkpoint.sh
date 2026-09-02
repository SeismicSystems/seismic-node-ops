#!/usr/bin/env bash

# Stable operator entry point for the Python checkpoint installer.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "$SCRIPT_DIR/install-checkpoint.py" "$@"

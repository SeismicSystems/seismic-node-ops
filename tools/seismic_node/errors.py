"""Exception hierarchy for the seismic-node CLI."""

from __future__ import annotations


class NodeError(Exception):
    """An operator-actionable failure.

    The CLI entry point prints the message without a traceback and exits 1.
    Modules raise it directly for ordinary failures and subclass it when a
    caller needs to tell one failure category from another.
    """

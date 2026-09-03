"""Select and start the observer's normal or checkpoint Summit program.

Observers have no validator-account lifecycle gate, but normal and checkpoint
Summit still share mutable state and therefore remain mutually exclusive.
"""

from __future__ import annotations

from typing import Any

from . import checkpoint, supervisor


def start_observer(args: Any) -> None:
    """Validate the selected mode and delegate ordered startup to Supervisor."""
    inventory_path = args.inventory or checkpoint.DEFAULT_INVENTORY_PATHS["observer"]
    checkpoint.load_inventory("observer", inventory_path)
    if args.mode == "checkpoint":
        checkpoint.validate_checkpoint_start_configuration("observer")
        summit_program = "summit-observer-checkpoint"
        conflicting_program = "summit-observer"
    else:
        # The normal Supervisor program does not read checkpoint-start config.
        # Mutual exclusion is enforced against the checkpoint program instead.
        summit_program = "summit-observer"
        conflicting_program = "summit-observer-checkpoint"
    supervisor.start_node(
        summit_program,
        conflicting_program,
        startup_timeout=args.startup_timeout,
    )
    print(f"Observer {args.mode} startup requested successfully.")

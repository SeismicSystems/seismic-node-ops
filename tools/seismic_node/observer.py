"""Start or stop observer services through conservative Supervisor workflows.

Observers have no validator-account lifecycle gate, but normal and checkpoint
Summit still share mutable state and therefore remain mutually exclusive.
"""

from __future__ import annotations

from typing import Any

from . import checkpoint, monitoring, systemd


def start_observer(args: Any) -> None:
    """Validate the selected mode and delegate ordered startup to systemd."""
    inventory = checkpoint.load_node("observer", checkpoint.node_env_path(args))
    if args.mode == "checkpoint":
        checkpoint.validate_checkpoint_start_configuration("observer")
        summit_program = "summit-observer-checkpoint"
        conflicting_program = "summit-observer"
    else:
        # The normal unit does not read checkpoint-start config. Mutual
        # exclusion is enforced against the checkpoint unit instead.
        summit_program = "summit-observer"
        conflicting_program = "summit-observer-checkpoint"
    systemd.reload_units()
    systemd.start_node(
        summit_program, conflicting_program, custodian_program="observer-custodian"
    )
    monitoring.ensure_running(inventory, "observer")
    print(f"Observer {args.mode} startup requested successfully.")


def stop_observer(args: Any) -> None:
    """Validate observer identity and stop its services in dependency order."""
    checkpoint.load_node("observer", checkpoint.node_env_path(args))
    systemd.stop_node(
        ("summit-observer", "summit-observer-checkpoint"),
        custodian_program="observer-custodian",
    )
    print("Observer services stopped successfully.")
    print("OpenResty and any running Prometheus Agent remain running.")

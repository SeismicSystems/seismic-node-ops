"""Independent, optional Prometheus Agent lifecycle.

Automatic startup is best effort and happens only after node startup succeeds.
Monitoring is deliberately absent from node shutdown/rollback sequences.
"""

from __future__ import annotations

import sys
from typing import Any

from . import checkpoint, supervisor

PROGRAM = "prometheus-agent"


def configured(inventory: dict[str, Any], role: str) -> bool:
    settings = inventory.get("monitoring", {"enabled": False})
    if not isinstance(settings, dict) or type(settings.get("enabled")) is not bool:
        raise checkpoint.CheckpointError("Invalid monitoring inventory settings")
    if not settings["enabled"]:
        return False
    required = {"enabled", "node", "role", "url", "data_dir", "version"}
    if set(settings) != required or settings["role"] != role:
        raise checkpoint.CheckpointError(
            "Monitoring inventory does not match node role"
        )
    if any(
        not isinstance(settings[key], str) or not settings[key]
        for key in required - {"enabled"}
    ):
        raise checkpoint.CheckpointError("Invalid monitoring inventory values")
    return True


def ensure_running(inventory: dict[str, Any], role: str, timeout: float) -> None:
    """Do not let telemetry errors undo authorized node startup."""
    try:
        if configured(inventory, role):
            supervisor.start_program(PROGRAM, timeout)
            print("Prometheus Agent is running.")
    except (checkpoint.CheckpointError, supervisor.SupervisorError, OSError):
        print(
            "WARNING: Prometheus Agent could not be started or verified. "
            "Node startup was not rolled back. Inspect monitoring status and agent logs.",
            file=sys.stderr,
        )


def prepare_agent() -> None:
    """Load only this program; a monitoring command must not update node groups."""
    supervisor.require_command_success(
        supervisor.run_systemctl("enable", "--now", "supervisor"),
        "Enabling and starting Supervisor",
    )
    supervisor.require_command_success(
        supervisor.run_supervisorctl("reread"), "Rereading Supervisor configuration"
    )
    supervisor.require_command_success(
        supervisor.run_supervisorctl("update", PROGRAM), "Updating agent configuration"
    )


def handle(args: Any) -> None:
    inventory_path = args.inventory or checkpoint.DEFAULT_INVENTORY_PATHS[args.role]
    # Monitoring can be inspected even when node keys/state are unavailable.
    inventory = checkpoint.read_toml(
        inventory_path, "Installation inventory", root_managed=True
    )
    if not configured(inventory, args.role):
        if args.monitoring_command == "status":
            print("Prometheus Agent is not configured/enabled in this inventory.")
            return
        raise checkpoint.CheckpointError("Prometheus Agent is not configured/enabled")
    if args.monitoring_command == "start":
        prepare_agent()
        supervisor.start_program(PROGRAM, args.startup_timeout)
        print("Prometheus Agent is running.")
    elif args.monitoring_command == "stop":
        supervisor.stop_autorestarting_program(PROGRAM)
        print("Prometheus Agent stopped. Node services were not changed.")
    else:
        value = supervisor.status(PROGRAM)
        print(f"{PROGRAM}: {value.state}")

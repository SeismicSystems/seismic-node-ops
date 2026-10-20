"""Independent, optional Prometheus Agent lifecycle.

Automatic startup is best effort and happens only after node startup succeeds.
Monitoring is deliberately absent from node shutdown/rollback sequences.
"""

from __future__ import annotations

import sys
from typing import Any

from . import agent, checkpoint, fsutil, nodeenv, systemd
from .errors import NodeError

PROGRAM = "prometheus-agent"


def configured(inventory: dict[str, Any], role: str) -> bool:
    """Return whether node.env enables the agent for this role."""
    env = inventory.get("env")
    if not isinstance(env, nodeenv.NodeEnv):
        raise NodeError("Node configuration was not loaded")
    if env.role != role:
        raise NodeError("Monitoring command does not match node role")
    return env.flag("MONITORING_ENABLED")


def enabled(env: nodeenv.NodeEnv) -> bool:
    return env.flag("MONITORING_ENABLED")


def ensure_running(inventory: dict[str, Any], role: str) -> None:
    """Do not let telemetry errors undo authorized node startup."""
    try:
        if configured(inventory, role):
            systemd.start_service(PROGRAM)
            print("Prometheus Agent is running.")
    except (NodeError, OSError):
        print(
            "WARNING: Prometheus Agent could not be started or verified. "
            "Node startup was not rolled back. Inspect monitoring status and agent logs.",
            file=sys.stderr,
        )


def handle(args: Any) -> None:
    fsutil.require_root()
    # Monitoring can be inspected even when node keys/state are unavailable,
    # so node.env is read without the on-disk identity checks.
    env = nodeenv.load(checkpoint.node_env_path(args))
    command = args.monitoring_command
    if command == "install":
        fsutil.require_absolute(args.token_source, "Token source")
        agent.install(env, args.token_source)
        return
    if command == "check":
        agent.check_existing(env)
        print("Prometheus Agent installation matches node.env.")
        return
    if not enabled(env):
        if command == "status":
            print("Prometheus Agent is not enabled in node.env.")
            return
        raise NodeError("Prometheus Agent is not enabled in node.env")
    if command == "start":
        agent.check_existing(env)
        systemd.reload_units()
        systemd.start_service(PROGRAM)
        print("Prometheus Agent is running.")
    elif command == "stop":
        systemd.stop_service(PROGRAM)
        print("Prometheus Agent stopped. Node services were not changed.")
    else:
        value = systemd.status(PROGRAM)
        print(f"{systemd.unit_for(PROGRAM)}: {value.state} {value.detail}".rstrip())

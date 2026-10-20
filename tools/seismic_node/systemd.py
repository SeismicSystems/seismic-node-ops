"""Conservative, command-scoped systemd service control.

Node services are static units under /etc/systemd/system that read
/etc/seismic/node.env. The CLI starts them in dependency order, records which
start requests the invocation issued, and reverses only those requests if a
later step fails. Explicit shutdown stops services in reverse order.

``systemctl start`` and ``systemctl stop`` block until the unit reaches its
target state or fails, so no polling loop is needed; ``Restart=`` is inhibited
by an administrative stop, so stopping is always quiescent.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import NodeError

SYSTEMCTL = Path("/usr/bin/systemctl")

# Program names used by the CLI and the unit that implements each one.
UNITS = {
    "reth": "seismic-reth",
    "summit": "seismic-summit",
    "summit-checkpoint": "seismic-summit-checkpoint",
    "summit-deposit-rpc": "seismic-deposit-rpc",
    "summit-observer": "seismic-observer",
    "summit-observer-checkpoint": "seismic-observer-checkpoint",
    "custodian": "seismic-custodian",
    "observer-custodian": "seismic-observer-custodian",
    "checkpointer": "seismic-checkpointer",
    "prometheus-agent": "seismic-prometheus-agent",
}

# ActiveState values. ``activating`` and ``deactivating`` are transient and
# ``reloading`` cannot occur for Type=simple units, but all are rejected as
# non-quiescent to be safe.
STOPPED_STATES = {"inactive", "failed"}
RUNNING_STATE = "active"
TRANSIENT_STATES = {"activating", "deactivating", "reloading"}


class ServiceError(NodeError):
    """Service state or process control was unsafe or unsuccessful."""


@dataclass(frozen=True)
class ServiceStatus:
    """Parsed state of one unit from ``systemctl show``."""

    name: str
    state: str
    detail: str
    exists: bool = True

    @property
    def running(self) -> bool:
        return self.state == RUNNING_STATE


def unit_for(name: str) -> str:
    try:
        return f"{UNITS[name]}.service"
    except KeyError:
        raise ServiceError(f"Unknown node program: {name}") from None


def run_systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the fixed systemctl binary without invoking a shell."""
    if not SYSTEMCTL.is_file():
        raise ServiceError(f"systemctl is required at {SYSTEMCTL}")
    return subprocess.run(
        [str(SYSTEMCTL), "--no-ask-password", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def require_command_success(
    result: subprocess.CompletedProcess[str], description: str
) -> None:
    """Raise a concise error when a service-control command fails."""
    if result.returncode == 0:
        return
    output = f"{result.stdout}\n{result.stderr}".strip()
    detail = f": {output}" if output else ""
    raise ServiceError(f"{description} failed{detail}")


def reload_units() -> None:
    """Pick up unit files installed or changed since the last reload."""
    require_command_success(run_systemctl("daemon-reload"), "Reloading systemd units")


def status(name: str) -> ServiceStatus:
    """Read and strictly parse one unit's state."""
    unit = unit_for(name)
    result = run_systemctl(
        "show", unit, "--property=LoadState,ActiveState,SubState,Result"
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        raise ServiceError(f"Could not determine status for {name}: {output}")
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    load_state = properties.get("LoadState", "")
    if load_state == "not-found":
        return ServiceStatus(name, "missing", unit, exists=False)
    state = properties.get("ActiveState", "")
    if (
        load_state != "loaded"
        or state not in STOPPED_STATES | {RUNNING_STATE} | TRANSIENT_STATES
    ):
        raise ServiceError(f"systemd returned an unexpected state for {name}: {output}")
    detail = properties.get("SubState", "")
    if state == "failed" and properties.get("Result"):
        detail = f"{detail} ({properties['Result']})"
    return ServiceStatus(name, state, detail)


def require_service(name: str) -> ServiceStatus:
    """Require a named unit to be installed."""
    value = status(name)
    if not value.exists:
        raise ServiceError(f"Required service is not installed: {unit_for(name)}")
    return value


def require_stopped(names: tuple[str, ...], *, allow_missing: bool = True) -> None:
    """Reject services in states that could indicate an active process."""
    unsafe: list[str] = []
    for name in names:
        value = status(name)
        if not value.exists and allow_missing:
            continue
        if not value.exists or value.state not in STOPPED_STATES:
            unsafe.append(f"{name}={value.state}")
    if unsafe:
        raise ServiceError(
            "The following services must be stopped: " + ", ".join(unsafe)
        )


def start_service(
    name: str,
    *,
    on_start_requested: Callable[[], None] | None = None,
) -> bool:
    """Start one stopped service; return whether a start was issued.

    ``systemctl start`` returns once the unit is active or has failed. For
    Type=simple that means the process was exec'd; readiness is the caller's
    concern. ``on_start_requested`` runs as soon as the request is issued so
    the caller owns cleanup even if the start itself fails.
    """
    before = require_service(name)
    if before.running:
        return False
    if before.state not in STOPPED_STATES:
        raise ServiceError(f"Cannot start {name} while it is {before.state}")
    if before.state == "failed":
        # Clear the failed marker so a fresh start is not confused with the
        # previous failure in the status output.
        run_systemctl("reset-failed", unit_for(name))
    if on_start_requested is not None:
        on_start_requested()
    result = run_systemctl("start", unit_for(name))
    if result.returncode != 0:
        after = status(name)
        output = f"{result.stdout}\n{result.stderr}".strip()
        raise ServiceError(
            f"{name} failed during startup: {after.state} {after.detail} {output}".strip()
        )
    after = require_service(name)
    if not after.running:
        raise ServiceError(f"{name} did not stay running: {after.state} {after.detail}")
    return True


def stop_service(name: str) -> bool:
    """Stop a service, returning whether a stop request was necessary."""
    current = status(name)
    if not current.exists or current.state in STOPPED_STATES:
        return False
    result = run_systemctl("stop", unit_for(name))
    if result.returncode != 0:
        output = f"{result.stdout}\n{result.stderr}".strip()
        raise ServiceError(f"Could not stop {name}: {output}")
    return True


def optional_services(custodian_program: str) -> tuple[bool, bool]:
    """Detect optional Custodian and checkpointer units."""
    return status(custodian_program).exists, status("checkpointer").exists


def start_node(
    summit_program: str,
    conflicting_summit_program: str,
    *,
    custodian_program: str = "custodian",
) -> list[str]:
    """Start a node in dependency order and reverse-clean partial startup."""
    # Normal and checkpoint Summit units declare Conflicts= with each other,
    # but refusing here keeps the CLI's own safety check independent of unit
    # metadata and supplements the shared kernel lock.
    require_stopped((conflicting_summit_program, "summit-deposit-rpc"))
    require_service("reth")
    require_service(summit_program)
    has_custodian, has_checkpointer = optional_services(custodian_program)
    sequence = [
        *([custodian_program] if has_custodian else []),
        "reth",
        summit_program,
        *(["checkpointer"] if has_checkpointer else []),
    ]
    started: list[str] = []
    try:
        for name in sequence:
            if start_service(
                name, on_start_requested=lambda name=name: started.append(name)
            ):
                print(f"Started service: {unit_for(name)}")
        return started
    except Exception:
        # Preserve services that were already running before this command;
        # only unwind start requests recorded in ``started``.
        cleanup_errors: list[str] = []
        for name in reversed(started):
            try:
                stop_service(name)
            except ServiceError as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            print("Startup cleanup errors: " + "; ".join(cleanup_errors))
        raise


def stop_node(
    summit_programs: tuple[str, ...], *, custodian_program: str = "custodian"
) -> list[str]:
    """Stop configured node services in reverse dependency order."""
    sequence = ["checkpointer", *summit_programs, "reth", custodian_program]
    stopped: list[str] = []
    for name in sequence:
        if stop_service(name):
            stopped.append(name)
            print(f"Stopped service: {unit_for(name)}")
    return stopped

"""Conservative, command-scoped Supervisor process control.

This module never reloads Supervisor configuration.  It starts only the selected
node programs, records which start requests this invocation issued, and stops
those programs in reverse order if a later startup step fails.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SUPERVISORCTL = Path("/usr/bin/supervisorctl")
# BACKOFF and STARTING can still spawn or transition to RUNNING. Only terminal
# states that cannot start without a new Supervisor command are safe here.
# Supervisor groups UNKNOWN with stopped states, but we reject it deliberately
# because the CLI cannot prove that an unknown process state is quiescent.
STOPPED_STATES = {"STOPPED", "EXITED", "FATAL"}
MAY_RESUME_STATES = {"STARTING", "BACKOFF", "RUNNING", "STOPPING"}
STATUS_EXIT_RUNNING = 0
STATUS_EXIT_NOT_RUNNING = 3


class SupervisorError(Exception):
    """Supervisor state or process control was unsafe or unsuccessful."""


@dataclass(frozen=True)
class ProgramStatus:
    """Parsed state returned by ``supervisorctl status``."""

    name: str
    state: str
    detail: str
    exists: bool = True

    @property
    def running(self) -> bool:
        """Return whether Supervisor reports the program as RUNNING."""
        return self.state == "RUNNING"


def run_supervisorctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the fixed system supervisorctl binary without invoking a shell."""
    if not SUPERVISORCTL.is_file():
        raise SupervisorError(f"Supervisor is required at {SUPERVISORCTL}")
    return subprocess.run(
        [str(SUPERVISORCTL), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def status(name: str) -> ProgramStatus:
    """Read and strictly parse one configured program's status."""
    result = run_supervisorctl("status", name)
    output = f"{result.stdout}\n{result.stderr}".strip()
    if "no such process" in output.lower():
        return ProgramStatus(name, "MISSING", output, exists=False)
    line = result.stdout.strip()
    parts = line.split(maxsplit=2)
    if (
        result.returncode not in {STATUS_EXIT_RUNNING, STATUS_EXIT_NOT_RUNNING}
        or len(parts) < 2
        or parts[0] != name
    ):
        raise SupervisorError(
            f"Could not determine Supervisor status for {name}: {output}"
        )
    state = parts[1]
    if state in STOPPED_STATES and result.returncode != STATUS_EXIT_NOT_RUNNING:
        raise SupervisorError(
            f"Supervisor returned an inconsistent status for {name}: {output}"
        )
    if state in MAY_RESUME_STATES and result.returncode != STATUS_EXIT_RUNNING:
        raise SupervisorError(
            f"Supervisor returned an inconsistent status for {name}: {output}"
        )
    if state not in STOPPED_STATES | MAY_RESUME_STATES:
        raise SupervisorError(
            f"Supervisor returned an unknown state for {name}: {output}"
        )
    return ProgramStatus(name, state, parts[2] if len(parts) == 3 else "")


def require_program(name: str) -> ProgramStatus:
    """Require a named Supervisor program to be configured."""
    value = status(name)
    if not value.exists:
        raise SupervisorError(f"Required Supervisor program is not configured: {name}")
    return value


def require_stopped(names: tuple[str, ...], *, allow_missing: bool = True) -> None:
    """Reject programs in states that could indicate an active process."""
    unsafe: list[str] = []
    for name in names:
        value = status(name)
        if not value.exists and allow_missing:
            continue
        if not value.exists or value.state not in STOPPED_STATES:
            unsafe.append(f"{name}={value.state}")
    if unsafe:
        raise SupervisorError(
            "The following Supervisor programs must be stopped: " + ", ".join(unsafe)
        )


def start_program(
    name: str,
    timeout: float,
    *,
    on_start_requested: Callable[[], None] | None = None,
) -> bool:
    """Start one stopped program and wait until it reaches RUNNING.

    ``on_start_requested`` runs immediately after Supervisor accepts the start
    command.  Callers use it to retain cleanup ownership even if polling later
    times out or observes BACKOFF/FATAL.
    """
    before = require_program(name)
    if before.running:
        return False
    if before.state not in STOPPED_STATES:
        raise SupervisorError(f"Cannot start {name} while it is {before.state}")
    result = run_supervisorctl("start", name)
    if result.returncode != 0:
        output = f"{result.stdout}\n{result.stderr}".strip()
        raise SupervisorError(f"Could not start {name}: {output}")
    if on_start_requested is not None:
        on_start_requested()
    deadline = time.monotonic() + timeout
    while True:
        current = require_program(name)
        if current.running:
            return True
        if current.state in {"BACKOFF", "FATAL", "EXITED"}:
            raise SupervisorError(
                f"{name} failed during startup: {current.state} {current.detail}"
            )
        if time.monotonic() >= deadline:
            raise SupervisorError(f"Timed out waiting for {name} to reach RUNNING")
        time.sleep(0.5)


def stop_program(name: str) -> None:
    """Stop a program unless it is missing or already in a stopped state."""
    current = status(name)
    if not current.exists or current.state in STOPPED_STATES:
        return
    result = run_supervisorctl("stop", name)
    if result.returncode != 0:
        output = f"{result.stdout}\n{result.stderr}".strip()
        raise SupervisorError(f"Could not stop {name}: {output}")


def optional_programs() -> tuple[bool, bool]:
    """Detect optional Custodian and checkpointer Supervisor programs."""
    return status("custodian").exists, status("checkpointer").exists


def start_node(
    summit_program: str,
    conflicting_summit_program: str,
    *,
    startup_timeout: float,
) -> list[str]:
    """Start a node in dependency order and reverse-clean partial startup."""
    # Normal and checkpoint Summit programs share state and ports.  Refusing a
    # conflicting program here supplements their shared kernel lock.
    require_stopped((conflicting_summit_program, "summit-deposit-rpc"))
    require_program("reth")
    require_program(summit_program)
    has_custodian, has_checkpointer = optional_programs()
    sequence = [
        *(["custodian"] if has_custodian else []),
        "reth",
        summit_program,
        *(["checkpointer"] if has_checkpointer else []),
    ]
    started: list[str] = []
    try:
        for name in sequence:
            if start_program(
                name,
                startup_timeout,
                on_start_requested=lambda name=name: started.append(name),
            ):
                print(f"Started Supervisor program: {name}")
        return started
    except Exception:
        # Preserve programs that were already running before this command; only
        # unwind start requests recorded in ``started``.
        cleanup_errors: list[str] = []
        for name in reversed(started):
            try:
                stop_program(name)
            except SupervisorError as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            print("Startup cleanup errors: " + "; ".join(cleanup_errors))
        raise

#!/usr/bin/env python3
"""Configure only telemetry for an existing node; never run node installation."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import prometheus_agent as agent

LOCK = Path("/run/seismic-prometheus-agent/install.lock")
NODE_PATHS = (
    "reth_data_dir",
    "reth_p2p_key_path",
    "summit_data_dir",
    "summit_keys_dir",
)
OBSERVER_KEYS = {"observer_parent_node_public_key", "observer_index"}
MONITORING_HEADER = re.compile(r"(?m)^[ \t]*\[monitoring\][ \t]*(?:#[^\r\n]*)?\r?$")
TABLE_HEADER = re.compile(r"(?m)^[ \t]*\[")


@dataclass
class Inventory:
    path: Path
    contents: bytes
    values: dict
    identity: tuple
    mode: int
    gid: int
    excluded: list[Path]


def file_identity(meta):
    return (
        meta.st_dev,
        meta.st_ino,
        meta.st_uid,
        meta.st_gid,
        meta.st_mode,
        meta.st_size,
        meta.st_mtime_ns,
    )


def read_inventory(path: Path, role: str) -> Inventory:
    try:
        meta = agent.root_file(path)
    except FileNotFoundError:
        raise agent.AgentError(
            "Existing installation inventory is required; check --inventory or complete node installation first"
        ) from None
    if meta.st_size > 1024 * 1024:
        raise agent.AgentError("Installation inventory exceeds the size limit")
    contents = path.read_bytes()
    try:
        values = tomllib.loads(contents.decode())
    except (ValueError, UnicodeError):
        raise agent.AgentError(
            "Invalid installation inventory TOML; contents omitted"
        ) from None
    required = {"schema_version", *NODE_PATHS}
    if role == "observer":
        required |= OBSERVER_KEYS
    if (
        set(values) - {"monitoring"} != required
        or type(values.get("schema_version")) is not int
        or values["schema_version"] != 1
    ):
        raise agent.AgentError(
            "Installation inventory schema does not match the selected node role"
        )
    paths = []
    for key in NODE_PATHS:
        value = values[key]
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
            or value == "/"
            or str(Path(value)) != value
            or os.path.normpath(value) != value
        ):
            raise agent.AgentError(
                "Installation inventory contains an unsafe node path"
            )
        paths.append(Path(value))
    for index, first in enumerate(paths):
        for second in paths[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                raise agent.AgentError(
                    "Installation inventory node state and key paths overlap"
                )
    if role == "observer":
        parent = values["observer_parent_node_public_key"]
        index = values["observer_index"]
        if (
            not isinstance(parent, str)
            or not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", parent)
            or type(index) is not int
            or not 0 <= index <= 255
        ):
            raise agent.AgentError(
                "Invalid observer assignment in installation inventory"
            )
    old = values.get("monitoring")
    if old is not None and (
        not isinstance(old, dict)
        or type(old.get("enabled")) is not bool
        or ("role" in old and old["role"] != role)
    ):
        raise agent.AgentError(
            "Existing monitoring inventory does not match the node role"
        )
    # Protect recorded paths, their key directories, and the conventional optional
    # component stores. No node data or key contents are read or modified.
    excluded = [
        *paths,
        paths[1].parent,
        paths[3].parent,
        Path("/persistence/custodian"),
        Path("/persistence/checkpointer"),
        Path("/persistence/checkpoints"),
    ]
    excluded += [p.resolve() for p in excluded]
    return Inventory(
        path,
        contents,
        values,
        file_identity(meta),
        stat.S_IMODE(meta.st_mode),
        meta.st_gid,
        excluded,
    )


def require_unchanged(inventory: Inventory) -> None:
    meta = agent.root_file(inventory.path)
    if (
        file_identity(meta) != inventory.identity
        or inventory.path.read_bytes() != inventory.contents
    ):
        raise agent.AgentError(
            "Installation inventory changed concurrently; refusing to overwrite it. Inspect agent settings before retrying"
        )


def render_inventory(inventory: Inventory, settings: dict) -> bytes:
    """Replace the generated table only, preserving node fields and comments."""
    text = inventory.contents.decode()
    headers = list(MONITORING_HEADER.finditer(text))
    block = "[monitoring]\n" + "".join(
        f"{key} = {json.dumps(value)}\n" for key, value in settings.items()
    )
    if "monitoring" in inventory.values:
        if len(headers) != 1:
            raise agent.AgentError(
                "Monitoring inventory must use a single [monitoring] table; refusing to rewrite other fields"
            )
        start = headers[0].start()
        next_table = TABLE_HEADER.search(text, headers[0].end())
        end = next_table.start() if next_table else len(text)
        rendered = text[:start] + block + text[end:]
    else:
        rendered = text + ("" if text.endswith("\n") else "\n") + "\n" + block
    expected = {**inventory.values, "monitoring": settings}
    try:
        if tomllib.loads(rendered) != expected:
            raise ValueError()
    except ValueError:
        raise agent.AgentError(
            "Cannot update monitoring without changing other inventory fields"
        ) from None
    return rendered.encode()


@contextlib.contextmanager
def installation_lock():
    # A shared agent-only lock also covers custom inventories and both roles.
    agent.directory(LOCK.parent, mode=0o700)
    agent.safe_path(LOCK)
    if LOCK.exists():
        agent.root_file(LOCK)
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise agent.AgentError(
                "Another monitoring-only installation is in progress"
            ) from None
        yield
    finally:
        os.close(fd)


def prompt(label: str, default: str = "") -> str:
    answer = input(label + (f" [{default}]" if default else "") + ": ").strip()
    return answer or default


def confirm(label: str) -> bool:
    while True:
        answer = input(label + " [y/N] ").strip().lower()
        if answer in ("", "n", "no"):
            return False
        if answer in ("y", "yes"):
            return True
        print("Enter yes or no.")


def validate_plan(
    settings: dict, inventory: Inventory, token_source: Path | None = None
) -> None:
    agent.validate(settings, inventory.excluded)
    agent.safe_path(Path(settings["data_dir"]))
    if token_source is not None:
        if any(
            token_source == p or p in token_source.parents for p in inventory.excluded
        ):
            raise agent.AgentError(
                "Token handoff must be outside node data and key directories"
            )
        agent.read_token(token_source)


def require_installable(previous: dict | None) -> None:
    agent.require_managed_or_new()
    # Also detect a loaded but otherwise unmanaged program on a fresh install.
    state = agent.supervisor_state()
    if previous is None and state != "MISSING":
        raise agent.AgentError(
            "An unmanaged prometheus-agent program is loaded in Supervisor"
        )
    if state not in ("MISSING", "STOPPED", "FATAL"):
        raise agent.AgentError(
            "Stop prometheus-agent before updating it; node services may remain running"
        )


def configure(path: Path, role: str) -> None:
    inventory = read_inventory(path, role)
    with installation_lock():
        require_unchanged(inventory)
        previous = agent.load_settings()
        if previous and previous["role"] != role:
            raise agent.AgentError("Existing agent belongs to a different node role")
        if previous is None and inventory.values.get("monitoring", {}).get("enabled"):
            raise agent.AgentError(
                "Inventory enables monitoring but managed agent settings are missing; repair before installing"
            )
        print(f"Monitoring-only setup for {role}: {path}")
        print(
            "Node binaries, keys, data, OpenResty and node Supervisor configuration will not be changed."
        )
        if previous is None:
            agent.require_managed_or_new()
            if not confirm("Install Prometheus Agent for authenticated remote write?"):
                print("No changes made to agent or installation inventory.")
                return
            action = "install"
        else:
            action = prompt("Agent action: keep, update, disable", "keep")
            if action not in ("keep", "update", "disable"):
                raise agent.AgentError("Select keep, update, or disable")
        token_source = None
        settings = dict(previous) if previous else {}
        if action in ("install", "update"):
            require_installable(previous)
            settings = {
                "enabled": True,
                "node": prompt(
                    "Stable monitoring node name",
                    settings.get("node", socket.getfqdn()),
                ),
                "role": role,
                "url": prompt(
                    "Remote-write HTTPS URL ending in /api/v1/write",
                    settings.get("url", ""),
                ),
                "data_dir": prompt(
                    "Agent WAL directory",
                    settings.get("data_dir", "/var/lib/seismic-prometheus-agent"),
                ),
                "version": agent.VERSION,
            }
            token_source = Path(
                prompt(
                    "Root-only token handoff file", str(agent.TOKEN) if previous else ""
                )
            )
            validate_plan(settings, inventory, token_source)
        elif action == "disable":
            settings["enabled"] = False
        elif settings["enabled"]:
            validate_plan(settings, inventory)
            agent.read_token(agent.TOKEN)
            agent.root_file(agent.CONFIG)
            agent.root_file(agent.SUPERVISOR_CONFIG)
        if action == "keep" and inventory.values.get("monitoring") == settings:
            print("Agent and installation inventory kept unchanged.")
            return
        # Render and validate the inventory before mutating the agent at all.
        rendered = render_inventory(inventory, settings)
        print(f"Agent action: {action}")
        print(json.dumps(settings, indent=2))
        print(
            "Only disabling stops the agent. No node services will be stopped, started or reloaded."
        )
        if not confirm("Apply monitoring-only changes?"):
            print("Cancelled; agent and installation inventory were not changed.")
            return
        require_unchanged(inventory)
        # Refuse a settings race with an independently invoked installer/helper.
        if agent.load_settings() != previous:
            raise agent.AgentError(
                "Agent settings changed concurrently; retry after the other operation completes"
            )
        if action in ("install", "update"):
            require_installable(previous)
            agent.install(settings, token_source)
        elif action == "disable":
            agent.disable()
        require_unchanged(inventory)
        if agent.load_settings() != settings:
            raise agent.AgentError(
                "Agent settings changed before the inventory update; inspect and retry"
            )
        agent.atomic_write(path, rendered, inventory.mode, inventory.gid)
        print(
            "Monitoring inventory updated atomically. Node services were not changed."
        )
        if settings["enabled"]:
            print("To start explicitly when the metrics receiver is ready:")
            print(
                f"  sudo ./tools/seismic-node.py monitoring start --role {role} "
                f"--inventory {shlex.quote(str(path))}"
            )
            print("Successful node startup also ensures the enabled agent is running.")
            print(
                "Register this identity as PUSH_NODE centrally; do not also pull-scrape it."
            )
        else:
            print(
                "Monitoring disabled. Token and WAL retained; central token revocation/inventory changes are separate."
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--role", required=True, choices=("validator", "observer"))
    parser.add_argument("--inventory", required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise agent.AgentError("Run the installer as root")
    if not Path("/usr/bin/supervisorctl").is_file() or shutil.which("useradd") is None:
        raise agent.AgentError(
            "An existing node installation with Supervisor and useradd is required; no system packages are installed in monitoring-only mode"
        )
    configure(args.inventory, args.role)


if __name__ == "__main__":
    try:
        main()
    except agent.AgentError as error:
        print(f"Monitoring-only: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (EOFError, KeyboardInterrupt):
        print(
            "Cancelled. If interrupted while applying, inspect agent state and rerun monitoring-only to reconcile the inventory.",
            file=sys.stderr,
        )
        raise SystemExit(130) from None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print(
            "Monitoring-only operation failed; diagnostic output withheld. Inspect agent state and rerun to reconcile the inventory if installation completed before the failure.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

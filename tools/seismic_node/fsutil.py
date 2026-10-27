"""Filesystem safety helpers shared by every seismic-node module.

Paths handed to the CLI are validated before use: they must be absolute and
normalized, must not traverse symbolic links, and configuration files must be
root-controlled. Readers open files with ``O_NOFOLLOW`` and reject unexpected
schemas so a misspelled safety setting cannot be silently ignored.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from pathlib import Path
from typing import Any

from .errors import NodeError

HEX_DIGEST = re.compile(r"^0x[0-9a-fA-F]{64}$")
PUBLIC_KEY = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Path, ownership, and input-file validation
# ---------------------------------------------------------------------------


def require_root() -> None:
    """Require root because node state and receipts are root-managed."""
    if os.geteuid() != 0:
        raise NodeError("This command must be run as root (use sudo).")


def require_absolute(path: Path, description: str) -> None:
    """Reject relative, root, non-normalized, and symlink-traversing paths."""
    if not path.is_absolute() or path == Path("/"):
        raise NodeError(f"{description} must be an absolute non-root path: {path}")
    if path.resolve(strict=False) != path:
        raise NodeError(
            f"{description} must be normalized and must not traverse symbolic links: {path}"
        )


def lstat_path(path: Path, description: str) -> os.stat_result:
    """Inspect a path itself rather than following a final symlink."""
    require_absolute(path, description)
    try:
        return path.lstat()
    except FileNotFoundError as error:
        raise NodeError(f"{description} is missing: {path}") from error
    except OSError as error:
        raise NodeError(f"Could not inspect {description}: {path}: {error}") from error


def require_directory(path: Path, description: str) -> os.stat_result:
    """Require an existing, non-symlinked directory."""
    metadata = lstat_path(path, description)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise NodeError(f"{description} is not a safe directory: {path}")
    return metadata


def require_regular_file(
    path: Path,
    description: str,
    *,
    nonempty: bool = True,
) -> os.stat_result:
    """Require an existing, non-symlinked regular file."""
    metadata = lstat_path(path, description)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise NodeError(f"{description} is not a safe regular file: {path}")
    if nonempty and metadata.st_size == 0:
        raise NodeError(f"{description} is empty: {path}")
    return metadata


def require_secure_root_parent(path: Path, description: str) -> None:
    """Verify every existing parent is a non-writable root-owned directory."""
    current = path.parent
    while True:
        try:
            metadata = current.lstat()
        except OSError as error:
            raise NodeError(
                f"Could not inspect {description} parent {current}: {error}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise NodeError(f"{description} parent is not a safe directory: {current}")
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise NodeError(
                f"{description} parent must be root-owned and not group- or "
                f"world-writable: {current}"
            )
        if current == Path("/"):
            return
        current = current.parent


def require_root_managed_file(path: Path, description: str) -> os.stat_result:
    """Require a file and its path chain to be controlled by root."""
    metadata = require_regular_file(path, description)
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise NodeError(
            f"{description} must be root-owned and not group- or world-writable: {path}"
        )
    require_secure_root_parent(path, description)
    return metadata


def is_relative_to(path: Path, parent: Path) -> bool:
    """Return whether ``path`` is contained by ``parent`` without raising."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def require_separate_paths(paths: dict[str, Path]) -> None:
    """Ensure state, keys, checkpoints, and backups cannot overlap or nest."""
    resolved = {name: path.resolve(strict=False) for name, path in paths.items()}
    names = list(resolved)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            left = resolved[left_name]
            right = resolved[right_name]
            if (
                left == right
                or is_relative_to(left, right)
                or is_relative_to(right, left)
            ):
                raise NodeError(
                    f"{left_name} and {right_name} must not be identical or nested: "
                    f"{left}, {right}"
                )


def filesystem_device(path: Path, description: str) -> int:
    """Return the device ID of a path or its nearest existing ancestor."""
    current = path
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if current == Path("/"):
                raise NodeError(
                    f"Could not find an existing filesystem ancestor for {description}: {path}"
                )
            current = current.parent
            continue
        except OSError as error:
            raise NodeError(
                f"Could not inspect filesystem for {description}: {path}: {error}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise NodeError(
                f"Filesystem path for {description} must not contain a symbolic link: "
                f"{current}"
            )
        return metadata.st_dev


def require_shared_state_filesystem(paths: dict[str, Path]) -> None:
    """Require one filesystem so all state moves remain atomic."""
    devices = {name: filesystem_device(path, name) for name, path in paths.items()}
    if len(set(devices.values())) == 1:
        return
    details = ", ".join(f"{name}=device-{device}" for name, device in devices.items())
    raise NodeError(
        "Reth, Summit, checkpoint, and rollback paths must share a filesystem so "
        f"state moves are atomic ({details})"
    )


def read_toml(
    path: Path, description: str, *, root_managed: bool = False
) -> dict[str, Any]:
    """Safely read TOML after applying the requested ownership policy."""
    if root_managed:
        require_root_managed_file(path, description)
    else:
        require_regular_file(path, description)

    try:
        file_descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise NodeError(
            f"Could not safely open {description}: {path}: {error}"
        ) from error

    try:
        with os.fdopen(file_descriptor, "rb") as file:
            value = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise NodeError(f"{description} is not valid TOML: {path}: {error}") from error
    if not isinstance(value, dict):
        raise NodeError(f"{description} must contain a TOML table: {path}")
    return value


def require_exact_keys(
    value: dict[str, Any], expected: set[str], description: str
) -> None:
    """Reject both missing and unexpected fields in a parsed object."""
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    details = []
    if missing:
        details.append(f"missing: {', '.join(missing)}")
    if unexpected:
        details.append(f"unexpected: {', '.join(unexpected)}")
    raise NodeError(f"{description} has an invalid schema ({'; '.join(details)})")


def read_json(
    path: Path, description: str, *, root_managed: bool = False
) -> dict[str, Any]:
    """Safely read JSON after applying the requested ownership policy."""
    if root_managed:
        require_root_managed_file(path, description)
    else:
        require_regular_file(path, description)
    try:
        file_descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise NodeError(
            f"Could not safely open {description}: {path}: {error}"
        ) from error
    try:
        with os.fdopen(file_descriptor, "rb") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise NodeError(f"Could not read {description}: {path}: {error}") from error
    if not isinstance(value, dict):
        raise NodeError(f"{description} must contain a JSON object: {path}")
    return value


def require_int(value: Any, description: str) -> int:
    """Require a non-negative JSON/TOML integer, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NodeError(f"{description} must be a non-negative integer")
    return value
